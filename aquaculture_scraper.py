#!/usr/bin/env python3
"""
aquaculture_scraper.py — polite, fault-tolerant scraper for aquaculture data APIs.
Every source is open: no API key, account or login is needed.

Run one source with --source. By default each run writes:
    aquaculture_data.csv   (UTF-8)
    aquaculture_data.json  (array of records, UTF-8)
To build every linked table at once (Norway + UAE), use run_all.py; see SCHEMA.md.

Sources (--source):

  Norway — facility level
    fiskeridir     Aquaculture Register: every fish-farm site (name, species, lat/lon,
                   capacity, licence holder). Open, no key.
    brreg          Brønnøysund company register: aquaculture companies by industry code
                   (org number, name, employees, address, founding date). Open, no key.
    ssb_salmon     Statistics Norway table 03024: weekly export volume and price of
                   farmed salmon. Open, no key.

  Any country — national level (default: Norway + UAE)
    worldbank      Fisheries indicators per country per year (World Bank WDI, FAO data),
                   e.g. capture fisheries ER.FSH.CAPT.MT. Open, no key.
    comtrade       UN Comtrade fish and seafood trade (HS chapter 03) per country,
                   year, flow and product. Uses the keyless public preview (max 500
                   rows per request, so it sends one request per year).

  Global & regional bulk statistics
    fao_aquaculture  FAO FishStat global aquaculture production: every country × species ×
                     area × environment × year since 1950, tonnes and USD (yearly zip).
    eurostat         Eurostat fish_aq2a: aquaculture production for EU countries, Norway and
                     Iceland by species, area and environment (one request per country).

  European sites
    emodnet          EMODnet Human Activities map service: every aquaculture layer
                     (finfish, shellfish, ...) with site attributes and geometry.

  Research & news
    openalex         OpenAlex: scientific publications and datasets matching --query
                     (default "aquaculture") published in the last --since-days days.
    gdelt            GDELT: news articles about aquaculture from the last --timespan.

  Generic
    obis           OBIS species occurrence records, e.g. --species "Sparus aurata".
    arcgis         Any ArcGIS REST map layer: --url ".../FeatureServer/0".
    custom         Any JSON API: --url, --pagination, --records-path, --field-map.

Field paths use dot notation with list support:
    "placement.countyName"      nested key
    "connections[0].licenseNr"  first item of a list
    "species[*].name"           every item of a list (values joined with "; ")

Run with --raw first on a new source. It keeps every field (flattened with
pandas.json_normalize), which shows you the real field names to map.

Examples:
    python aquaculture_scraper.py --source fiskeridir --contact you@example.org
    python aquaculture_scraper.py --source worldbank --countries "NOR;ARE" --years 2000:2024
    python aquaculture_scraper.py --source comtrade --reporters 578,784 --years 2022,2023
    python aquaculture_scraper.py --source ssb_salmon --last-weeks 104
    python aquaculture_scraper.py --source custom --url https://api.example.org/v1/farms \\
        --pagination page --records-path data --field-map my_fields.json
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

__version__ = "1.1.0"

log = logging.getLogger("aquaculture_scraper")

MIN_DELAY_SECONDS = 1.0
DEFAULT_TIMEOUT = (10, 30)  # (connect, read) seconds
RETRY_STATUSES = (429, 500, 502, 503, 504)
DEFAULT_BASENAME = "aquaculture_data"

# Output column -> candidate field paths. The first path that resolves to a value is used.
FieldMap = dict[str, list[str]]

# Columns added to every row so each value can be traced back and cited
PROVENANCE_COLUMNS = ["source", "source_url", "record_url", "reference_url", "retrieved_at"]

COUNTRY_CODES = {  # ISO3 -> UN M49 code (used by UN Comtrade)
    "NOR": "578", "ARE": "784",
}


# --------------------------------------------------------------------------------------
# Source configuration
# --------------------------------------------------------------------------------------
@dataclass
class SourceConfig:
    name: str
    url: str  # may contain {year}/{week} placeholders for week pagination
    pagination: str  # "page" | "offset" | "range" | "cursor" | "arcgis" | "week" | "values" | "none"
    records_path: str | None = None  # path to the record list; None = body is the list
    page_size: int = 100
    params: dict[str, Any] = field(default_factory=dict)
    field_map: FieldMap | None = None  # None = keep every field (json_normalize)
    id_field: str | None = None  # used for de-duplication and cursor pagination
    total_path: str | None = None  # path to a "total records" count, for progress logs
    pages_path: str | None = None  # path to a "total pages" count; stops page pagination
    missing_records_is_empty: bool = False  # a missing record list means "no more data"
    transform: Callable[[dict], dict] | None = None  # per-record pre-processing
    parser: str = "records"  # "records" | "jsonstat" (JSON-stat 2.0 cube -> rows)
    method: str = "GET"
    json_body: dict | None = None  # request body for POST APIs
    week_from: tuple[int, int] | None = None  # ISO (year, week), for week pagination
    week_to: tuple[int, int] | None = None
    iterate_param: str | None = None  # "values" pagination: one request per value ...
    iterate_values: list[str] = field(default_factory=list)  # ... of this parameter
    table: str = DEFAULT_BASENAME  # table name used by run_all.py / SCHEMA.md
    reference_url: str | None = None  # page to cite for the dataset (reference_url column)
    record_url: Callable[[dict], str | None] | None = None  # link to one record (record_url column)
    # Fetches everything itself instead of paging a JSON API: returns ([(record, url)], failures)
    custom_fetch: Callable[..., tuple[list[tuple[dict, str]], int]] | None = None
    next_cursor_path: str | None = None  # cursor pagination: next-page token in the response
    cursor_start: str | None = None  # cursor pagination: token for the first page (e.g. "*")
    max_consecutive_failures: int = 3
    drop_if_empty: str | None = None  # skip rows whose mapped column is empty (placeholders)
    retry_on_429: bool = True  # let the HTTP layer retry 429s at once; False for strict APIs
    rate_limit_wait: float = 30.0  # after a 429 that retries didn't fix: wait, then try again
    rate_limit_retries: int = 2
    # Query-parameter names used by the paginators
    page_param: str = "page"
    size_param: str = "per_page"
    offset_param: str = "offset"
    cursor_param: str = "after"
    range_param: str = "range"
    page_start: int = 1


def _arcgis_feature_to_record(feature: dict) -> dict:
    """Flatten an ArcGIS feature: attributes become columns; point geometry becomes lat/lon."""
    if not isinstance(feature, dict):
        raise TypeError(f"expected a feature object, got {type(feature).__name__}")
    record = dict(feature.get("attributes") or {})
    geom = feature.get("geometry") or {}
    if isinstance(geom, dict) and "x" in geom and "y" in geom:
        record.setdefault("longitude", geom.get("x"))
        record.setdefault("latitude", geom.get("y"))
    return record


FISKERIDIR_FIELDS: FieldMap = {
    "facility_id": ["siteId", "id"],
    "site_number": ["siteNr", "siteNumber"],
    "facility_name": ["name", "siteName"],
    "species": ["species[*].name", "species[*]", "speciesType", "connections[*].species[*].name"],
    "latitude": ["latitude", "lat", "coordinates.latitude", "position.latitude"],
    "longitude": ["longitude", "lon", "lng", "coordinates.longitude", "position.longitude"],
    "capacity": ["capacity", "maxCapacity", "biomassCapacity"],
    "capacity_unit": ["capacityUnitType", "capacityUnit"],
    "company": ["connections[*].legalEntityName", "connections[*].ownerName",
                "connections[*].licenseHolder.name", "ownerName", "companyName"],
    "org_numbers": ["connections[*].legalEntityNr", "connections[*].orgNr",
                    "connections[*].organizationNumber", "connections[*].licenseHolder.orgNr"],
    "license_numbers": ["connections[*].licenseNr", "connections[*].licenseNumber"],
    "water_type": ["waterType", "waterEnvironment"],
    "placement_type": ["placementType", "placement.placementType"],
    "municipality": ["placement.municipalityName", "municipalityName"],
    "county": ["placement.countyName", "countyName"],
    "production_area": ["placement.prodAreaName", "productionAreaName", "prodAreaName"],
    "status": ["status", "siteStatus"],
    "registered_date": ["firstClearanceTime", "registeredDate", "clearanceDate"],
}

BRREG_FIELDS: FieldMap = {
    "org_number": ["organisasjonsnummer"],
    "company_name": ["navn"],
    "legal_form": ["organisasjonsform.beskrivelse", "organisasjonsform.kode"],
    "nace_code": ["naeringskode1.kode"],
    "nace_description": ["naeringskode1.beskrivelse"],
    "employees": ["antallAnsatte"],
    "founded_date": ["stiftelsesdato"],
    "registered_date": ["registreringsdatoEnhetsregisteret"],
    "website": ["hjemmeside"],
    "address": ["forretningsadresse.adresse[*]"],
    "postcode": ["forretningsadresse.postnummer"],
    "city": ["forretningsadresse.poststed"],
    "municipality": ["forretningsadresse.kommune"],
    "country": ["forretningsadresse.land"],
    "parent_org_number": ["overordnetEnhet"],
    "bankrupt": ["konkurs"],
    "under_liquidation": ["underAvvikling"],
}

WORLDBANK_FIELDS: FieldMap = {
    "country_iso3": ["countryiso3code", "country.id"],  # some regions have no ISO3 code
    "country": ["country.value"],
    "year": ["date"],
    "indicator_id": ["indicator.id"],
    "indicator": ["indicator.value"],
    "value": ["value"],
}

COMTRADE_FIELDS: FieldMap = {
    "year": ["refYear", "period"],
    "reporter_code": ["reporterCode"],
    "reporter_iso3": ["reporterISO"],
    "reporter": ["reporterDesc"],
    "flow_code": ["flowCode"],
    "flow": ["flowDesc"],
    "partner_code": ["partnerCode"],
    "partner": ["partnerDesc"],
    "hs_code": ["cmdCode"],
    "commodity": ["cmdDesc"],
    "net_weight_kg": ["netWgt"],
    "quantity": ["qty"],
    "quantity_unit": ["qtyUnitAbbr"],
    "trade_value_usd": ["primaryValue"],
}

SSB_SALMON_FIELDS: FieldMap = {
    "period": ["Tid"],
    "product_code": ["VareGrupper2"],
    "product": ["VareGrupper2_label"],
    "measure_code": ["ContentsCode"],
    "measure": ["ContentsCode_label"],
    "value": ["value"],
}

FAO_FIELDS: FieldMap = {k: [k] for k in (
    "country_un_code", "country_iso3", "country", "species_code", "species", "scientific_name",
    "species_group", "area_code", "area", "environment_code", "environment", "measure", "year",
    "value", "status", "cell_key")}

EUROSTAT_FIELDS: FieldMap = {
    "geo": ["geo"], "country": ["geo_label"],
    "species_code": ["species"], "species": ["species_label"],
    "area_code": ["fishreg"], "area": ["fishreg_label"],
    "environment_code": ["aquaenv"], "environment": ["aquaenv_label"],
    "unit_code": ["unit"], "unit": ["unit_label"],
    "year": ["time"], "value": ["value"], "cell_key": ["cell_key"],
}

EMODNET_FIELDS: FieldMap = {
    "layer": ["layer"], "layer_title": ["layer_title"], "feature_id": ["feature_id"],
    "name": ["properties.name", "properties.NAME", "properties.site_name", "properties.sitename",
             "properties.SITE_NAME", "properties.farm_name"],
    "country": ["properties.country", "properties.COUNTRY", "properties.country_code",
                "properties.countrycode", "properties.CNTR_CODE"],
    "species": ["properties.species", "properties.SPECIES", "properties.species_name"],
    "status": ["properties.status", "properties.STATUS"],
    "geometry_type": ["geometry_type"], "latitude": ["latitude"], "longitude": ["longitude"],
    "properties": ["properties"], "geometry": ["geometry"],
}

OPENALEX_FIELDS: FieldMap = {
    "openalex_id": ["id"], "doi": ["doi"], "title": ["display_name", "title"],
    "publication_year": ["publication_year"], "publication_date": ["publication_date"],
    "type": ["type"], "language": ["language"],
    "journal": ["primary_location.source.display_name"],
    "authors": ["authorships[*].author.display_name"],
    "institutions": ["authorships[*].institutions[*].display_name"],
    "countries": ["authorships[*].countries[*]"],
    "topics": ["topics[*].display_name", "concepts[*].display_name"],
    "cited_by_count": ["cited_by_count"],
    "is_open_access": ["open_access.is_oa"], "open_access_url": ["open_access.oa_url"],
    "landing_page_url": ["primary_location.landing_page_url"],
}

GDELT_FIELDS: FieldMap = {
    "url": ["url"], "title": ["title"], "seen_date": ["seendate"], "domain": ["domain"],
    "language": ["language"], "source_country": ["sourcecountry"], "image_url": ["socialimage"],
}

def eurostat_periods(today: date | None = None, start: int = 2008, block: int = 4) -> list[tuple[int, int]]:
    """Year blocks for Eurostat requests: one country's full cube is over the API's
    5-million-cell limit, so each request asks for one country and a few years."""
    end = (today or date.today()).year
    return [(y, min(y + block - 1, end)) for y in range(start, end + 1, block)]


# EU-27 plus Norway and Iceland (Eurostat geo codes; Greece is EL)
EUROSTAT_GEO = ("AT BE BG CY CZ DE DK EE EL ES FI FR HR HU IE IT LT LU LV MT NL PL PT RO SE "
                "SI SK NO IS").split()

# Capture fisheries only: aquaculture (ER.FSH.AQUA.MT) and total production (ER.FSH.PROD.MT)
# are FAO numbers already in fao_aquaculture_production, in more detail
WORLDBANK_FISHERY_INDICATORS = "ER.FSH.CAPT.MT"

OBIS_FIELDS: FieldMap = {
    "record_id": ["id"],
    "species": ["scientificName", "species"],
    "latitude": ["decimalLatitude"],
    "longitude": ["decimalLongitude"],
    "year": ["date_year", "year"],
    "event_date": ["eventDate"],
    "country": ["country"],
    "locality": ["locality"],
    "depth_m": ["depth", "minimumDepthInMeters"],
    "basis_of_record": ["basisOfRecord"],
    "dataset_name": ["datasetName"],
    "institution": ["institutionCode", "ownerInstitutionCode"],
}

# Norwegian industry codes (SN2007) for fish farming: sea food fish, sea juveniles,
# freshwater food fish, freshwater juveniles. Override with --nace if the register changes.
NORWAY_AQUACULTURE_NACE = "03.211,03.212,03.221,03.222"


def parse_iso_week(text: str) -> tuple[int, int]:
    """Parse '2025-W07' or '2025-7' into (2025, 7)."""
    m = re.fullmatch(r"(\d{4})-?W?(\d{1,2})", text.strip(), flags=re.IGNORECASE)
    if not m:
        raise SystemExit(f"Week must look like 2025-W07, got {text!r}")
    year, week = int(m.group(1)), int(m.group(2))
    try:
        date.fromisocalendar(year, week, 1)
    except ValueError as exc:
        raise SystemExit(f"{text!r} is not a valid ISO week") from exc
    return year, week


def parse_years(text: str) -> list[str]:
    """Parse '2022,2023' or '2019:2023' into a list of years."""
    years: list[str] = []
    for part in text.split(","):
        part = part.strip()
        if ":" in part:
            start, end = (int(x) for x in part.split(":", 1))
            years += [str(y) for y in range(start, end + 1)]
        elif part:
            years.append(str(int(part)))
    return years


def next_iso_week(year: int, week: int) -> tuple[int, int]:
    y, w, _ = (date.fromisocalendar(year, week, 1) + timedelta(weeks=1)).isocalendar()
    return y, w


def build_source(args: argparse.Namespace) -> SourceConfig:
    """Build the SourceConfig for the chosen preset, then apply CLI overrides."""
    s = args.source
    if s == "fiskeridir":
        cfg = SourceConfig(
            name="Fiskeridirektoratet Aquaculture Register",
            url="https://api.fiskeridir.no/pub-aqua/api/v1/sites",
            pagination="range", page_size=100, field_map=FISKERIDIR_FIELDS,
            id_field="siteId", table="facilities_norway",
            reference_url="https://api.fiskeridir.no/pub-aqua/api/v1/sites")
    elif s == "brreg":
        cfg = SourceConfig(
            name="Brønnøysund Register Centre (Enhetsregisteret)",
            url="https://data.brreg.no/enhetsregisteret/api/enheter",
            pagination="page", page_param="page", size_param="size", page_start=0,
            page_size=100, params={"naeringskode": args.nace or NORWAY_AQUACULTURE_NACE},
            records_path="_embedded.enheter", pages_path="page.totalPages",
            total_path="page.totalElements", missing_records_is_empty=True,
            field_map=BRREG_FIELDS, id_field="organisasjonsnummer", table="companies_norway",
            reference_url="https://data.brreg.no/enhetsregisteret/api/docs/index.html",
            record_url=lambda r: get_path(r, "_links.self.href"))
    elif s == "ssb_salmon":
        cfg = SourceConfig(
            name="Statistics Norway table 03024 (salmon exports)",
            url="https://data.ssb.no/api/v0/en/table/03024",
            pagination="none", method="POST", parser="jsonstat",
            json_body={
                "query": [
                    {"code": "VareGrupper2", "selection": {"filter": "item", "values": ["01", "02"]}},
                    {"code": "ContentsCode", "selection": {"filter": "item", "values": ["Vekt", "Kilopris"]}},
                    {"code": "Tid", "selection": {"filter": "top", "values": [str(args.last_weeks)]}},
                ],
                "response": {"format": "json-stat2"},
            },
            field_map=SSB_SALMON_FIELDS, table="prices_norway_salmon_export",
            reference_url="https://www.ssb.no/en/statbank/table/03024")
    elif s == "worldbank":
        countries = args.countries or "NOR;ARE"
        indicator = args.indicator or "ER.FSH.AQUA.MT"
        params = {"format": "json", "date": args.years or "2000:2024"}
        if ";" in indicator:
            params["source"] = "2"  # several indicators in one call must name their source (2 = WDI)
        cfg = SourceConfig(
            name=f"World Bank WDI {indicator} (FAO data)",
            url=f"https://api.worldbank.org/v2/country/{countries}/indicator/{indicator}",
            pagination="page", page_size=1000, records_path="[1]", pages_path="[0].pages",
            total_path="[0].total", missing_records_is_empty=True, params=params,
            field_map=WORLDBANK_FIELDS, table="production_by_country", drop_if_empty="value",
            reference_url=f"https://data.worldbank.org/indicator/{indicator.split(';')[0]}")
    elif s == "fao_aquaculture":
        import bulk_sources
        cfg = SourceConfig(
            name="FAO FishStat Global Aquaculture Production",
            url="https://www.fao.org/fishery/static/Data/", pagination="none",
            custom_fetch=bulk_sources.fetch_fao_zip, field_map=FAO_FIELDS, id_field="cell_key",
            table="fao_aquaculture_production",
            reference_url="https://www.fao.org/fishery/en/collection/aquaculture")
    elif s == "eurostat":
        geo = args.geo.split(",") if args.geo else EUROSTAT_GEO
        requests_ = [{"geo": g, "sinceTimePeriod": str(a), "untilTimePeriod": str(b)}
                     for g in geo for a, b in eurostat_periods()]
        cfg = SourceConfig(
            name="Eurostat fish_aq2a (aquaculture production)",
            url="https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/fish_aq2a",
            pagination="values", iterate_values=requests_, parser="jsonstat",
            params={"format": "JSON", "lang": "EN"}, field_map=EUROSTAT_FIELDS, id_field="cell_key",
            max_consecutive_failures=30, table="eurostat_aquaculture_production",
            reference_url="https://ec.europa.eu/eurostat/databrowser/view/fish_aq2a/default/table")
    elif s == "emodnet":
        import bulk_sources
        cfg = SourceConfig(
            name="EMODnet Human Activities (aquaculture layers)",
            url="https://ows.emodnet-humanactivities.eu/wfs", pagination="none", page_size=1000,
            params={"layer_pattern": args.layer_pattern or
                    r"aquacult|finfish|shellfish|mussel|oyster|mollus|fish.?farm|seaweed|algae",
                    "layer_exclude": r"advisory"},  # advisory-council areas are not farm sites
            custom_fetch=bulk_sources.fetch_wfs_layers, field_map=EMODNET_FIELDS,
            id_field="feature_id", table="facilities_europe",
            reference_url="https://emodnet.ec.europa.eu/en/human-activities")
    elif s == "openalex":
        since = (date.today() - timedelta(days=args.since_days)).isoformat()
        params = {"search": args.query or "aquaculture",
                  "filter": f"from_publication_date:{since}", "sort": "publication_date:desc"}
        if args.contact and "@" in args.contact:
            params["mailto"] = args.contact  # OpenAlex's "polite pool" for identified clients
        # Page numbers, not cursors: cursor paging past page 1 needs an API key, while
        # page=N works without one for the first 10,000 results
        cfg = SourceConfig(
            name="OpenAlex (scholarly works)", url="https://api.openalex.org/works",
            pagination="page", records_path="results", page_size=200, page_param="page",
            size_param="per-page", total_path="meta.count", missing_records_is_empty=True,
            params=params, field_map=OPENALEX_FIELDS, id_field="id",
            table="research_publications", reference_url="https://openalex.org/",
            record_url=lambda r: r.get("doi") or r.get("id"))
    elif s == "gdelt":
        query = args.query or '(aquaculture OR "fish farm" OR "fish farming" OR mariculture OR "salmon farming")'
        cfg = SourceConfig(
            name="GDELT DOC 2.0 (news articles)", url="https://api.gdeltproject.org/api/v2/doc/doc",
            pagination="none", records_path="articles", missing_records_is_empty=True,
            params={"query": query, "mode": "artlist", "format": "json", "maxrecords": "250",
                    "timespan": args.timespan, "sort": "DateDesc"},
            field_map=GDELT_FIELDS, id_field="url", table="news_articles",
            retry_on_429=False, rate_limit_wait=10.0, rate_limit_retries=3,  # max 1 request / 5 s
            reference_url="https://www.gdeltproject.org/", record_url=lambda r: r.get("url"))
    elif s == "comtrade":
        reporters = args.reporters or ",".join(COUNTRY_CODES.values())
        this_year = date.today().year
        years = parse_years(args.years) if args.years else [str(y) for y in range(this_year - 6, this_year - 1)]
        cfg = SourceConfig(
            name="UN Comtrade (public preview)",
            url="https://comtradeapi.un.org/public/v1/preview/C/A/HS",
            pagination="values", iterate_param="period", iterate_values=years,
            records_path="data", missing_records_is_empty=True,
            params={"reporterCode": reporters,
                    "cmdCode": args.hs_codes or "03,0301,0302,0303,0304,0305,0306,0307",
                    "flowCode": "M,X", "partnerCode": "0", "includeDesc": "true"},
            field_map=COMTRADE_FIELDS, table="trade_fish_by_country",
            reference_url="https://comtradeplus.un.org/")
    elif s == "obis":
        cfg = SourceConfig(
            name="OBIS occurrences", url="https://api.obis.org/v3/occurrence",
            pagination="cursor", records_path="results", page_size=1000,
            params={"scientificname": args.species or "Salmo salar"}, field_map=OBIS_FIELDS,
            id_field="id", total_path="total", size_param="size", cursor_param="after",
            table="species_occurrences", reference_url="https://obis.org/")
    elif s == "arcgis":
        if not args.url:
            raise SystemExit("--source arcgis needs --url (a FeatureServer/MapServer layer URL)")
        url = args.url.rstrip("/")
        if not url.endswith("/query"):
            url += "/query"
        layer_url = url[: -len("/query")]
        cfg = SourceConfig(
            name="ArcGIS REST layer", url=url, pagination="arcgis", records_path="features",
            page_size=1000, params={"where": "1=1", "outFields": "*", "returnGeometry": "true",
                                    "outSR": "4326", "f": "json"},
            transform=_arcgis_feature_to_record, id_field="OBJECTID", table="map_layer",
            reference_url=layer_url)
    else:  # custom
        if not args.url:
            raise SystemExit("--source custom needs --url")
        cfg = SourceConfig(name="Custom JSON API", url=args.url, pagination="page",
                           records_path="data", id_field="id", reference_url=args.url)

    overrides: dict[str, Any] = {}
    if args.url and s not in ("arcgis",):
        overrides["url"] = args.url
    for attr in ("pagination", "page_size", "id_field", "page_param", "size_param",
                 "offset_param", "cursor_param"):
        value = getattr(args, attr)
        if value is not None:
            overrides[attr] = value
    if args.weeks_from or args.weeks_to:
        if not (args.weeks_from and args.weeks_to):
            raise SystemExit("give both --weeks-from and --weeks-to")
        overrides["week_from"] = parse_iso_week(args.weeks_from)
        overrides["week_to"] = parse_iso_week(args.weeks_to)
    if args.records_path is not None:
        overrides["records_path"] = args.records_path or None  # "" means the body is the list
    if args.param:
        overrides["params"] = {**cfg.params, **dict(_parse_kv(p) for p in args.param)}
    if args.field_map:
        overrides["field_map"] = load_field_map(args.field_map)
    if args.raw:
        overrides["field_map"] = None
    if args.reference_url:
        overrides["reference_url"] = args.reference_url
    cfg = replace(cfg, **overrides)
    if s == "arcgis" and cfg.id_field:  # each ArcGIS feature has its own URL: <layer>/<object id>
        layer, oid_field = cfg.url[: -len("/query")], cfg.id_field
        cfg = replace(cfg, record_url=lambda r: (f"{layer}/{r[oid_field]}"
                                                 if r.get(oid_field) is not None else None))
    return cfg


def _parse_kv(text: str) -> tuple[str, str]:
    if "=" not in text:
        raise SystemExit(f"--param must look like key=value, got {text!r}")
    key, value = text.split("=", 1)
    return key.strip(), value


def load_field_map(path: str) -> FieldMap:
    """Load {"column": "path"} or {"column": ["path1", "path2"]} from a JSON file."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Could not read field map {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Field map {path} must be a JSON object")
    return {str(k): [v] if isinstance(v, str) else list(v) for k, v in data.items()}


# --------------------------------------------------------------------------------------
# Safe nested access
# --------------------------------------------------------------------------------------
_TOKEN = re.compile(r"([^.\[\]]+)|\[(\*|-?\d+)\]")


def get_path(obj: Any, path: str) -> Any:
    """Read a value at a dotted path. Never raises; returns None when any step is missing."""
    tokens = [(m.group(1), m.group(2)) for m in _TOKEN.finditer(path)]
    return _walk(obj, tokens)


def _walk(obj: Any, tokens: list[tuple[str | None, str | None]]) -> Any:
    if obj is None or not tokens:
        return obj
    (key, index), rest = tokens[0], tokens[1:]
    if key is not None:
        return _walk(obj.get(key), rest) if isinstance(obj, dict) else None
    if not isinstance(obj, list):
        return None
    if index == "*":
        values: list[Any] = []
        for item in obj:
            value = _walk(item, rest)
            if isinstance(value, list):
                values.extend(value)
            elif value is not None:
                values.append(value)
        return values or None
    i = int(index)
    return _walk(obj[i], rest) if -len(obj) <= i < len(obj) else None


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _to_cell(value: Any) -> Any:
    """Turn a resolved value into a flat cell: lists are de-duplicated and joined."""
    if isinstance(value, list):
        seen: dict[str, None] = {}
        for v in value:
            if not _is_empty(v):
                seen.setdefault(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False), None)
        return "; ".join(seen) or None
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return value


def apply_field_map(record: dict, field_map: FieldMap) -> dict:
    out: dict[str, Any] = {}
    for column, paths in field_map.items():
        out[column] = None
        for path in paths:
            value = get_path(record, path)
            if not _is_empty(value):
                out[column] = _to_cell(value)
                break
    return out


def jsonstat_to_records(body: Any) -> list[dict] | None:
    """Turn a JSON-stat 2.0 dataset (used by SSB, Eurostat and other statistics offices)
    into long-format rows: one row per cell that has a value, with each dimension's code
    and label, the value, and a cell_key that uniquely identifies the cell."""
    if not isinstance(body, dict) or "id" not in body or "dimension" not in body:
        return None
    ids, values = body["id"], body.get("value", [])
    categories = []
    for dim in ids:
        cat = body["dimension"][dim].get("category", {})
        index, labels = cat.get("index"), cat.get("label", {})
        if index is None:
            codes = list(labels)
        elif isinstance(index, list):
            codes = index
        else:
            codes = sorted(index, key=index.get)
        categories.append([(code, labels.get(code, code)) for code in codes])
    sizes = [len(c) for c in categories]

    if isinstance(values, dict):  # sparse cube: only filled cells are listed
        cells = sorted(((int(k), v) for k, v in values.items()), key=lambda kv: kv[0])
    else:
        cells = list(enumerate(values))
    records = []
    for flat, value in cells:
        if value is None:
            continue
        position, rest = [], flat
        for size in reversed(sizes):  # row-major: the last dimension changes fastest
            position.append(rest % size if size else 0)
            rest //= size or 1
        position.reverse()
        row: dict[str, Any] = {}
        key_parts = []
        for dim, idx, cats in zip(ids, position, categories):
            code, label = cats[idx]
            row[dim] = code
            row[f"{dim}_label"] = label
            key_parts.append(f"{dim}={code}")
        row["value"] = value
        row["cell_key"] = "|".join(key_parts)
        records.append(row)
    return records


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------
def create_session(user_agent: str, retry_on_429: bool = True) -> requests.Session:
    retries = Retry(
        total=5,
        connect=3,
        read=3,
        backoff_factor=1.5,          # waits about 0s, 3s, 6s, 12s, 24s between attempts
        backoff_jitter=0.5,
        status_forcelist=RETRY_STATUSES if retry_on_429 else tuple(c for c in RETRY_STATUSES if c != 429),
        # POST is included because the POST APIs used here are read-only queries
        allowed_methods=frozenset({"GET", "HEAD", "POST"}),
        respect_retry_after_header=True,  # honour 429/503 Retry-After from the server
        raise_on_status=False,       # return the last response so we can log it
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=2, pool_maxsize=2)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    })
    return session


def robots_allows(session: requests.Session, url: str, user_agent: str) -> bool:
    """Check robots.txt. A missing or unreadable robots.txt counts as allowed."""
    parts = urlparse(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        resp = session.get(robots_url, timeout=10, headers={"Accept": "text/plain"})
    except requests.RequestException as exc:
        log.debug("robots.txt not reachable (%s); assuming allowed", exc)
        return True
    if resp.status_code != 200:
        return True
    parser = RobotFileParser()
    parser.parse(resp.text.splitlines())
    return parser.can_fetch(user_agent, url)


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def fetch_json(session: requests.Session, url: str, params: dict, *, method: str = "GET",
               json_body: dict | None = None, timeout=DEFAULT_TIMEOUT) -> tuple[Any, str]:
    """Send a request and decode JSON. Returns (body, final URL including the query string).
    Raises FetchError with a readable reason on any failure."""
    try:
        resp = session.request(method, url, params=params, json=json_body, timeout=timeout)
    except requests.exceptions.RetryError as exc:
        raise FetchError(f"gave up after retries: {exc}") from exc
    except requests.RequestException as exc:
        raise FetchError(f"network error: {exc}") from exc

    if resp.status_code >= 400:
        raise FetchError(f"HTTP {resp.status_code} from {resp.url}: {resp.text[:200]!r}",
                         status=resp.status_code)
    try:
        body = resp.json()
    except ValueError as exc:
        ctype = resp.headers.get("Content-Type", "?")
        raise FetchError(f"response was not valid JSON (Content-Type: {ctype}): "
                         f"{resp.text[:200]!r}") from exc
    # Some APIs report errors inside an HTTP 200 body
    if isinstance(body, dict) and isinstance(body.get("error"), dict):  # ArcGIS
        err = body["error"]
        raise FetchError(f"API error {err.get('code')}: {err.get('message')} {err.get('details') or ''}")
    if isinstance(body, dict) and isinstance(body.get("error"), str) and body["error"].strip():  # Comtrade
        raise FetchError(f"API error: {body['error']}")
    if (isinstance(body, list) and len(body) == 1 and isinstance(body[0], dict)
            and "message" in body[0]):  # World Bank
        raise FetchError(f"API error: {body[0]['message']}")
    return body, resp.url


# --------------------------------------------------------------------------------------
# Pagination strategies
# --------------------------------------------------------------------------------------
class Paginator:
    """Each paginator returns the query params for the current page and moves forward."""
    can_skip_failed_page = True
    stop_on_empty = True  # an empty page means the end of the data

    def __init__(self, cfg: SourceConfig):
        self.cfg = cfg

    def params(self) -> dict:
        return {}

    def path_vars(self) -> dict:
        """Values for {placeholders} in the URL."""
        return {}

    def extra_fields(self) -> dict:
        """Columns added to every record from this page (e.g. the week it belongs to)."""
        return {}

    def advance(self, raw_records: list, body: Any) -> bool:
        """Move to the next page. Return False when there are no more pages."""
        return False

    def skip(self) -> bool:
        """Skip a page that failed. Return False if there is no next page."""
        return self.advance([None] * self.cfg.page_size, None)


class NoPagination(Paginator):
    can_skip_failed_page = False


class PagePaginator(Paginator):  # ?page=1&per_page=100
    def __init__(self, cfg):
        super().__init__(cfg)
        self.page = cfg.page_start

    def params(self):
        return {self.cfg.page_param: self.page, self.cfg.size_param: self.cfg.page_size}

    def advance(self, raw_records, body):
        self.page += 1
        if self.cfg.pages_path and body is not None:
            total_pages = get_path(body, self.cfg.pages_path)
            if isinstance(total_pages, (int, float)):
                return self.page - self.cfg.page_start < total_pages
        return True  # otherwise stop at the first empty page


class OffsetPaginator(Paginator):  # ?offset=0&limit=100
    def __init__(self, cfg):
        super().__init__(cfg)
        self.offset = 0

    def params(self):
        return {self.cfg.offset_param: self.offset, self.cfg.size_param: self.cfg.page_size}

    def advance(self, raw_records, body):
        self.offset += len(raw_records)
        return True


class RangePaginator(Paginator):  # ?range=0-99 (Fiskeridir style, inclusive)
    def __init__(self, cfg):
        super().__init__(cfg)
        self.start = 0

    def params(self):
        return {self.cfg.range_param: f"{self.start}-{self.start + self.cfg.page_size - 1}"}

    def advance(self, raw_records, body):
        self.start += self.cfg.page_size
        return True


class CursorPaginator(Paginator):  # ?after=<id of last record>
    can_skip_failed_page = False

    def __init__(self, cfg):
        super().__init__(cfg)
        if not cfg.id_field and not cfg.next_cursor_path:
            raise SystemExit("cursor pagination needs --id-field")
        self.cursor: Any = cfg.cursor_start

    def params(self):
        p = {self.cfg.size_param: self.cfg.page_size}
        if self.cursor is not None:
            p[self.cfg.cursor_param] = self.cursor
        return p

    def advance(self, raw_records, body):
        if self.cfg.next_cursor_path:  # the API hands out the next page's token
            nxt = get_path(body, self.cfg.next_cursor_path)
            if not nxt or nxt == self.cursor:
                return False
            self.cursor = nxt
            return True
        last = raw_records[-1] if raw_records else None
        cursor = get_path(last, self.cfg.id_field) if isinstance(last, dict) else None
        if cursor is None or cursor == self.cursor:
            return False
        self.cursor = cursor
        return True


class ArcGISPaginator(Paginator):  # resultOffset / resultRecordCount + exceededTransferLimit
    def __init__(self, cfg):
        super().__init__(cfg)
        self.offset = 0

    def params(self):
        return {"resultOffset": self.offset, "resultRecordCount": self.cfg.page_size}

    def advance(self, raw_records, body):
        self.offset += len(raw_records)
        return bool(isinstance(body, dict) and body.get("exceededTransferLimit"))

    def skip(self):
        self.offset += self.cfg.page_size  # unknown whether more pages exist; try the next one
        return True


class WeekPaginator(Paginator):  # one request per ISO week: .../{year}/{week}
    stop_on_empty = False  # a week with no reports is normal; keep going

    def __init__(self, cfg):
        super().__init__(cfg)
        if not cfg.week_from or not cfg.week_to:
            raise SystemExit("week pagination needs a start and end week")
        if cfg.week_from > cfg.week_to:
            raise SystemExit(f"start week {cfg.week_from} is after end week {cfg.week_to}")
        self.year, self.week = cfg.week_from

    def path_vars(self):
        return {"year": self.year, "week": self.week}

    def extra_fields(self):
        return {"year": self.year, "week": self.week}

    def advance(self, raw_records, body):
        self.year, self.week = next_iso_week(self.year, self.week)
        return (self.year, self.week) <= self.cfg.week_to

    def skip(self):
        return self.advance([], None)


class ValuesPaginator(Paginator):  # one request per value, e.g. per year: ?period=2023
    stop_on_empty = False  # a value with no data (e.g. a year not yet reported) is normal

    def __init__(self, cfg):
        super().__init__(cfg)
        if not cfg.iterate_values or (not cfg.iterate_param and
                                      not all(isinstance(v, dict) for v in cfg.iterate_values)):
            raise SystemExit("values pagination needs a parameter and a list of values")
        self.i = 0

    def params(self):
        value = self.cfg.iterate_values[self.i]
        return dict(value) if isinstance(value, dict) else {self.cfg.iterate_param: value}

    def extra_fields(self):
        return {}

    def advance(self, raw_records, body):
        self.i += 1
        return self.i < len(self.cfg.iterate_values)

    def skip(self):
        return self.advance([], None)


PAGINATORS: dict[str, type[Paginator]] = {
    "none": NoPagination, "page": PagePaginator, "offset": OffsetPaginator,
    "range": RangePaginator, "cursor": CursorPaginator, "arcgis": ArcGISPaginator,
    "week": WeekPaginator, "values": ValuesPaginator,
}


# --------------------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------------------
@dataclass
class ScrapeResult:
    records: list[dict] = field(default_factory=list)
    pages_ok: int = 0
    pages_failed: int = 0
    records_skipped: int = 0
    duplicates: int = 0
    empty_rows: int = 0  # placeholder rows without a value (e.g. years not yet reported)
    interrupted: bool = False


def extract_records(body: Any, cfg: SourceConfig) -> list | None:
    if cfg.parser == "jsonstat":
        return jsonstat_to_records(body)
    data = body if cfg.records_path is None else get_path(body, cfg.records_path)
    if data is None and cfg.missing_records_is_empty:
        return []
    return data if isinstance(data, list) else None


def parse_record(raw: Any, cfg: SourceConfig, page_url: str | None = None) -> tuple[dict, Any]:
    """Return (output row, dedup key). Raises on records that cannot be used.

    Adds source_url (the API request this row came from) and, when the source has
    per-record links, record_url."""
    record = cfg.transform(raw) if cfg.transform else raw
    if not isinstance(record, dict):
        raise TypeError(f"expected an object, got {type(record).__name__}: {str(record)[:80]!r}")
    key = get_path(record, cfg.id_field) if cfg.id_field else None
    row = apply_field_map(record, cfg.field_map) if cfg.field_map else dict(record)
    row["source_url"] = page_url
    if cfg.record_url:
        try:
            row["record_url"] = cfg.record_url(record)
        except Exception:  # a missing link must not lose the record
            row["record_url"] = None
    return row, key


def polite_sleep(delay: float) -> None:
    if delay > 0:
        time.sleep(delay + random.uniform(0, delay * 0.5))  # jitter spreads load


def _fetch_with_rate_limit(session, cfg: SourceConfig, url: str, params: dict) -> tuple[Any, str]:
    """fetch_json, but when the API still says 429 (too many requests), wait and try again."""
    for attempt in range(cfg.rate_limit_retries + 1):
        try:
            return fetch_json(session, url, params, method=cfg.method, json_body=cfg.json_body)
        except FetchError as exc:
            if exc.status != 429 or attempt == cfg.rate_limit_retries:
                raise
            wait = cfg.rate_limit_wait * (attempt + 1)
            log.warning("Rate limited (HTTP 429); waiting %.0fs before trying again.", wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


def _log_raw_sample(cfg: SourceConfig, raw_records: list) -> None:
    """Log the first raw record once per run, so field names can be checked from the log."""
    if cfg.field_map and raw_records:
        sample = json.dumps(raw_records[0], ensure_ascii=False, default=str)
        log.info("First raw record (to check field names): %s%s", sample[:2000],
                 " ..." if len(sample) > 2000 else "")


def _scrape_bulk(cfg: SourceConfig, session: requests.Session, delay: float) -> ScrapeResult:
    """Sources with their own fetcher (bulk files, map services)."""
    result = ScrapeResult()
    seen_keys: set[Any] = set()
    log.info("Source: %s — %s (bulk download)", cfg.name, cfg.url)
    try:
        pairs, failed = cfg.custom_fetch(session, cfg, delay)
    except FetchError as exc:
        log.error("Download failed: %s", exc)
        result.pages_failed = 1
        return result
    except KeyboardInterrupt:
        result.interrupted = True
        return result
    result.pages_failed = failed
    result.pages_ok = 1
    _log_raw_sample(cfg, [raw for raw, _ in pairs[:1]])
    for i, (raw, url) in enumerate(pairs):
        try:
            row, key = parse_record(raw, cfg, url)
        except Exception as exc:  # one bad record must never stop the run
            result.records_skipped += 1
            log.warning("Skipping record %d: %s: %s", i, type(exc).__name__, exc)
            continue
        if key is not None:
            if key in seen_keys:
                result.duplicates += 1
                continue
            seen_keys.add(key)
        result.records.append(row)
    log.info("Successfully parsed %d records (%d skipped, %d duplicates).",
             len(result.records), result.records_skipped, result.duplicates)
    return result


def scrape(cfg: SourceConfig, session: requests.Session, *, max_pages: int,
           delay: float, max_consecutive_failures: int | None = None) -> ScrapeResult:
    if cfg.custom_fetch:
        return _scrape_bulk(cfg, session, delay)
    if max_consecutive_failures is None:
        max_consecutive_failures = cfg.max_consecutive_failures
    pager = PAGINATORS[cfg.pagination](cfg)
    result = ScrapeResult()
    seen_keys: set[Any] = set()
    consecutive_failures = 0
    total_hint: Any = None

    log.info("Source: %s — %s (pagination: %s, page size: %d)",
             cfg.name, cfg.url, cfg.pagination, cfg.page_size)
    try:
        for page_no in range(1, max_pages + 1):
            if page_no > 1:
                polite_sleep(delay)
            params = {**cfg.params, **pager.params()}
            extra = pager.extra_fields()
            url = cfg.url.format(**pager.path_vars()) if pager.path_vars() else cfg.url
            label = f"page {page_no}" + (f" ({', '.join(f'{k} {v}' for k, v in extra.items())})" if extra else "")
            log.info("Fetching %s...", label)

            try:
                body, page_url = _fetch_with_rate_limit(session, cfg, url, params)
                raw_records = extract_records(body, cfg)
                if raw_records is None:
                    keys = list(body)[:10] if isinstance(body, dict) else type(body).__name__
                    raise FetchError(f"no list of records at path {cfg.records_path!r} "
                                     f"(top-level keys: {keys}); set --records-path")
            except FetchError as exc:
                result.pages_failed += 1
                consecutive_failures += 1
                log.error("%s failed: %s", label.capitalize(), exc)
                if not pager.can_skip_failed_page:
                    log.error("This pagination type cannot skip a failed page; stopping.")
                    break
                if consecutive_failures >= max_consecutive_failures:
                    log.error("%d pages in a row failed; stopping.", consecutive_failures)
                    break
                if not pager.skip():
                    break
                continue

            consecutive_failures = 0
            if cfg.total_path and total_hint is None:
                total_hint = get_path(body, cfg.total_path)
                if total_hint is not None:
                    log.info("Server reports %s matching records in total.", total_hint)

            if not raw_records:
                if pager.stop_on_empty:
                    log.info("%s returned no records — reached the end.", label.capitalize())
                    break
                log.info("%s returned no records.", label.capitalize())
                result.pages_ok += 1
                if not pager.advance(raw_records, body):
                    log.info("No more pages.")
                    break
                continue

            if page_no == 1:
                _log_raw_sample(cfg, raw_records)
            parsed = skipped = new = 0
            for i, raw in enumerate(raw_records):
                try:
                    row, key = parse_record(raw, cfg, page_url)
                except Exception as exc:  # one bad record must never stop the run
                    skipped += 1
                    log.warning("Skipping record %d on %s: %s: %s", i, label, type(exc).__name__, exc)
                    continue
                parsed += 1
                if cfg.drop_if_empty and _is_empty(row.get(cfg.drop_if_empty)):
                    result.empty_rows += 1
                    new += 1  # a real record, just without a value yet
                    continue
                if key is not None:
                    if key in seen_keys:
                        result.duplicates += 1
                        continue
                    seen_keys.add(key)
                result.records.append({**extra, **row} if extra else row)
                new += 1

            result.pages_ok += 1
            result.records_skipped += skipped
            progress = f" of {total_hint}" if total_hint is not None else ""
            log.info("Successfully parsed %d records on %s (%d skipped, %d new). "
                     "Running total: %d%s", parsed, label, skipped, new,
                     len(result.records), progress)

            if new == 0 and parsed > 0:
                log.warning("%s added no new records; the API is probably ignoring the "
                            "pagination parameters. Stopping to avoid a loop.", label.capitalize())
                break
            if not pager.advance(raw_records, body):
                log.info("No more pages.")
                break
        else:
            log.warning("Stopped at --max-pages=%d; more data may be available.", max_pages)
    except KeyboardInterrupt:
        result.interrupted = True
        log.warning("Interrupted — saving the %d records collected so far.", len(result.records))
    return result


# --------------------------------------------------------------------------------------
# Cleaning & output
# --------------------------------------------------------------------------------------
def build_dataframe(records: list[dict]) -> pd.DataFrame:
    df = pd.json_normalize(records, sep=".")

    for col in df.columns:  # trim strings; blank strings become missing
        if df[col].dtype == object or pd.api.types.is_string_dtype(df[col]):
            df[col] = df[col].map(lambda v: (v.strip() or None) if isinstance(v, str) else v)

    for col, limit in (("latitude", 90), ("longitude", 180)):
        if col in df.columns:
            numeric = pd.to_numeric(df[col], errors="coerce")
            bad = numeric.notna() & (numeric.abs() > limit)
            if bad.any():
                log.warning("%d rows have an out-of-range %s; set to empty.", int(bad.sum()), col)
            df[col] = numeric.mask(bad)

    hashable = df.map(lambda v: json.dumps(v, sort_keys=True) if isinstance(v, (list, dict)) else v)
    dupes = hashable.duplicated()
    if dupes.any():
        log.info("Dropped %d exact duplicate rows.", int(dupes.sum()))
        df = df.loc[~dupes].reset_index(drop=True)

    empty_cols = [c for c in df.columns if df[c].isna().all()]
    if empty_cols:
        log.warning("These columns are empty in every record: %s. The field paths may not "
                    "match this API. Run with --raw to see the real field names.", empty_cols)
    return df


def add_provenance(df: pd.DataFrame, cfg: SourceConfig) -> pd.DataFrame:
    """Record where and when every row came from, so merged tables stay traceable and citable.
    The provenance columns are placed last, in a fixed order."""
    df = df.copy()
    df["source"] = cfg.name
    if "source_url" not in df.columns:
        df["source_url"] = None
    if "record_url" not in df.columns:
        df["record_url"] = None
    df["reference_url"] = cfg.reference_url
    df["retrieved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data_cols = [c for c in df.columns if c not in PROVENANCE_COLUMNS]
    return df[data_cols + PROVENANCE_COLUMNS]


def _atomic_write(path: Path, write: Callable[[Path], None]) -> None:
    """Write to a temporary file, then rename, so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    try:
        write(Path(tmp))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def save_outputs(df: pd.DataFrame, out_dir: Path, basename: str) -> tuple[Path, Path]:
    csv_path = out_dir / f"{basename}.csv"
    json_path = out_dir / f"{basename}.json"
    # CSV cells must be scalars: store any remaining lists/dicts as JSON text
    csv_df = df.map(lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
    _atomic_write(csv_path, lambda p: csv_df.to_csv(p, index=False, encoding="utf-8"))
    _atomic_write(json_path, lambda p: df.to_json(p, orient="records", indent=2,
                                                  force_ascii=False, date_format="iso"))
    return csv_path, json_path


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
SOURCES = ["fiskeridir", "brreg", "ssb_salmon", "worldbank", "comtrade", "fao_aquaculture",
           "eurostat", "emodnet", "openalex", "gdelt", "obis", "arcgis", "custom"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scrape aquaculture data from a JSON API to CSV + JSON.",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split("Examples:", 1)[1] if __doc__ else None)
    p.add_argument("--source", choices=SOURCES, default="fiskeridir",
                   help="source preset (default: fiskeridir)")
    p.add_argument("--url", help="endpoint URL (required for arcgis/custom; overrides a preset)")

    g = p.add_argument_group("source options")
    g.add_argument("--countries", help="worldbank: ISO3 codes separated by ';' (default NOR;ARE)")
    g.add_argument("--indicator", help="worldbank: indicator id (default ER.FSH.AQUA.MT)")
    g.add_argument("--years", help="worldbank: 2000:2024 | comtrade: 2022,2023 or 2019:2023")
    g.add_argument("--reporters", help="comtrade: UN M49 codes, e.g. 578,784 (Norway, UAE)")
    g.add_argument("--hs-codes", help="comtrade: HS codes (default 03 and 0301-0307)")
    g.add_argument("--nace", help=f"brreg: industry codes (default {NORWAY_AQUACULTURE_NACE})")
    g.add_argument("--weeks-from", help="week pagination: first ISO week, e.g. 2025-W01")
    g.add_argument("--weeks-to", help="week pagination: last ISO week, e.g. 2025-W52")
    g.add_argument("--last-weeks", type=int, default=52, help="ssb_salmon: number of recent weeks")
    g.add_argument("--species", help="obis: scientific name, e.g. 'Salmo salar'")
    g.add_argument("--geo", help="eurostat: country codes, e.g. NO,ES,EL (default EU-27 + NO + IS)")
    g.add_argument("--layer-pattern", help="emodnet: regex for the layers to download")
    g.add_argument("--query", help="openalex / gdelt: search terms")
    g.add_argument("--since-days", type=int, default=30,
                   help="openalex: publications from the last N days (default 30)")
    g.add_argument("--timespan", default="7d", help="gdelt: how far back, e.g. 7d, 2w, 3m (default 7d)")

    a = p.add_argument_group("advanced")
    a.add_argument("--param", action="append", metavar="KEY=VALUE",
                   help="extra query parameter (repeat for more)")
    a.add_argument("--pagination", choices=sorted(PAGINATORS), help="override pagination type")
    a.add_argument("--records-path", help="dot path to the record list ('' = body is the list)")
    a.add_argument("--id-field", help="field path used for de-dup / cursor pagination")
    a.add_argument("--page-size", type=int, help="records per request")
    a.add_argument("--page-param"); a.add_argument("--size-param")
    a.add_argument("--offset-param"); a.add_argument("--cursor-param")
    a.add_argument("--field-map", help="JSON file: {output_column: path or [paths]}")
    a.add_argument("--reference-url", help="page to cite for this dataset (reference_url column)")
    a.add_argument("--raw", action="store_true",
                   help="keep every field (flattened) instead of applying the field map")

    r = p.add_argument_group("run")
    r.add_argument("--max-pages", type=int, default=1000, help="safety cap (default 1000)")
    r.add_argument("--delay", type=float, default=1.5,
                   help=f"seconds between requests, minimum {MIN_DELAY_SECONDS} (default 1.5)")
    r.add_argument("--contact", default=os.environ.get("AQUA_SCRAPER_CONTACT"),
                   help="email/URL put in the User-Agent (or set AQUA_SCRAPER_CONTACT)")
    r.add_argument("--ignore-robots", action="store_true", help="skip the robots.txt check")
    r.add_argument("--out-dir", default=".", help="output folder (default: current folder)")
    r.add_argument("--basename", default=DEFAULT_BASENAME,
                   help=f"output file name without extension (default {DEFAULT_BASENAME})")
    r.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                            format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    cfg = build_source(args)
    if args.page_size is not None and args.page_size < 1:
        raise SystemExit("--page-size must be at least 1")

    delay = args.delay
    if delay < MIN_DELAY_SECONDS:
        log.warning("--delay %.2fs is below the polite minimum; using %.1fs.", delay, MIN_DELAY_SECONDS)
        delay = MIN_DELAY_SECONDS

    if not args.contact:
        log.warning("No --contact given. Please add an email or URL so the data owner can reach you.")
    user_agent = (f"AquacultureDataBot/{__version__} (research data collection; "
                  f"contact: {args.contact or 'not provided'}) python-requests/{requests.__version__}")

    session = create_session(user_agent, retry_on_429=cfg.retry_on_429)
    if not args.ignore_robots and not robots_allows(session, cfg.url.split("{", 1)[0], user_agent):
        log.error("robots.txt disallows %s for this bot. Stopping (use --ignore-robots only "
                  "if the data owner has given you permission).", cfg.url)
        return 2

    started = time.monotonic()
    result = scrape(cfg, session, max_pages=args.max_pages, delay=delay)

    if not result.records:
        log.error("No records were collected (%d pages failed). Nothing written.", result.pages_failed)
        return 2

    df = add_provenance(build_dataframe(result.records), cfg)
    csv_path, json_path = save_outputs(df, Path(args.out_dir), args.basename)
    log.info("Saved %d records × %d columns to %s and %s", len(df), df.shape[1], csv_path, json_path)
    log.info("Summary: %d pages ok, %d pages failed, %d records skipped, %d duplicates removed, "
             "%d empty placeholder rows dropped, %.1fs elapsed.", result.pages_ok, result.pages_failed,
             result.records_skipped, result.duplicates, result.empty_rows, time.monotonic() - started)

    return 1 if (result.pages_failed or result.interrupted) else 0


if __name__ == "__main__":
    sys.exit(main())
