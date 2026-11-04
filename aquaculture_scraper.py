#!/usr/bin/env python3
"""
aquaculture_scraper.py — polite, fault-tolerant scraper for aquaculture data APIs.

Run one source with --source. By default each run writes:
    aquaculture_data.csv   (UTF-8)
    aquaculture_data.json  (array of records, UTF-8)
To build every linked table at once (Norway + UAE), use run_all.py; see SCHEMA.md.

Sources (--source):

  Norway — facility level
    fiskeridir     Aquaculture Register: every fish-farm site (name, species, lat/lon,
                   capacity, licence holder). Open, no key.
    barentswatch   Weekly fish health per site: sea lice, PD/ILA disease, treatments,
                   fallowing, sea temperature. Needs a free BarentsWatch API client
                   (env BW_CLIENT_ID and BW_CLIENT_SECRET).
    brreg          Brønnøysund company register: aquaculture companies by industry code
                   (org number, name, employees, address, founding date). Open, no key.
    ssb_salmon     Statistics Norway table 03024: weekly export volume and price of
                   farmed salmon. Open, no key.

  Any country — national level (default: Norway + UAE)
    worldbank      Aquaculture production in tonnes per country per year
                   (World Bank indicator ER.FSH.AQUA.MT, sourced from FAO). Open, no key.
    comtrade       UN Comtrade fish and seafood trade (HS chapter 03) per country,
                   year, flow and product. Works without a key (preview, max 500 rows);
                   set COMTRADE_API_KEY for the full API.

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
    python aquaculture_scraper.py --source barentswatch --weeks-from 2025-W01 --weeks-to 2025-W52
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

COUNTRY_CODES = {  # ISO3 -> UN M49 code (used by UN Comtrade)
    "NOR": "578", "ARE": "784",
}


# --------------------------------------------------------------------------------------
# Source configuration
# --------------------------------------------------------------------------------------
@dataclass
class OAuthConfig:
    token_url: str
    client_id_env: str
    client_secret_env: str
    scope: str | None = None


@dataclass
class SourceConfig:
    name: str
    url: str  # may contain {year}/{week} placeholders for week pagination
    pagination: str  # "page" | "offset" | "range" | "cursor" | "arcgis" | "week" | "none"
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
    api_key_env: str | None = None  # env var holding an API key ...
    api_key_header: str | None = None  # ... sent in this header
    oauth: OAuthConfig | None = None  # client-credentials login before scraping
    week_from: tuple[int, int] | None = None  # ISO (year, week), for week pagination
    week_to: tuple[int, int] | None = None
    table: str = DEFAULT_BASENAME  # table name used by run_all.py / SCHEMA.md
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

BARENTSWATCH_FIELDS: FieldMap = {
    "site_number": ["localityNo", "localityNumber"],
    "facility_name": ["name", "localityName"],
    "avg_adult_female_lice": ["avgAdultFemaleLice"],
    "avg_mobile_lice": ["avgMobileLice"],
    "avg_stationary_lice": ["avgStationaryLice"],
    "has_reported_lice": ["hasReportedLice"],
    "sea_temperature_c": ["seaTemperature"],
    "is_fallow": ["isFallow"],
    "has_pd": ["hasPd"],
    "has_ila": ["hasIla"],
    "has_cleaner_fish": ["hasCleanerfishDeployed"],
    "has_mechanical_removal": ["hasMechanicalRemoval"],
    "has_medicinal_treatment": ["hasSubstanceTreatments"],
    "has_salmonids": ["hasSalmonoids"],
    "is_on_land": ["isOnLand"],
    "municipality": ["municipality"],
    "latitude": ["lat", "latitude"],
    "longitude": ["lon", "longitude"],
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
    "country_iso3": ["countryiso3code"],
    "country": ["country.value"],
    "year": ["date"],
    "indicator_id": ["indicator.id"],
    "indicator": ["indicator.value"],
    "value": ["value"],
    "unit": ["unit"],
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
            id_field="siteId", table="facilities_norway")
    elif s == "barentswatch":
        last_full_week = (date.today() - timedelta(weeks=1)).isocalendar()
        week_to = parse_iso_week(args.weeks_to) if args.weeks_to else (last_full_week[0], last_full_week[1])
        if args.weeks_from:
            week_from = parse_iso_week(args.weeks_from)
        else:
            y, w, _ = (date.fromisocalendar(*week_to, 1) - timedelta(weeks=51)).isocalendar()
            week_from = (y, w)
        cfg = SourceConfig(
            name="BarentsWatch Fish Health",
            url="https://www.barentswatch.no/bwapi/v1/geodata/fishhealth/{year}/{week}",
            pagination="week", records_path="localities", field_map=BARENTSWATCH_FIELDS,
            missing_records_is_empty=True, week_from=week_from, week_to=week_to,
            oauth=OAuthConfig(token_url="https://id.barentswatch.no/connect/token",
                              client_id_env="BW_CLIENT_ID", client_secret_env="BW_CLIENT_SECRET",
                              scope="api"),
            table="health_norway_weekly")
    elif s == "brreg":
        cfg = SourceConfig(
            name="Brønnøysund Register Centre (Enhetsregisteret)",
            url="https://data.brreg.no/enhetsregisteret/api/enheter",
            pagination="page", page_param="page", size_param="size", page_start=0,
            page_size=100, params={"naeringskode": args.nace or NORWAY_AQUACULTURE_NACE},
            records_path="_embedded.enheter", pages_path="page.totalPages",
            total_path="page.totalElements", missing_records_is_empty=True,
            field_map=BRREG_FIELDS, id_field="organisasjonsnummer", table="companies_norway")
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
            field_map=SSB_SALMON_FIELDS, table="prices_norway_salmon_export")
    elif s == "worldbank":
        countries = args.countries or "NOR;ARE"
        indicator = args.indicator or "ER.FSH.AQUA.MT"
        cfg = SourceConfig(
            name=f"World Bank WDI {indicator} (FAO data)",
            url=f"https://api.worldbank.org/v2/country/{countries}/indicator/{indicator}",
            pagination="page", page_size=1000, records_path="[1]", pages_path="[0].pages",
            total_path="[0].total", missing_records_is_empty=True,
            params={"format": "json", "date": args.years or "2000:2024"},
            field_map=WORLDBANK_FIELDS, table="production_by_country")
    elif s == "comtrade":
        reporters = args.reporters or ",".join(COUNTRY_CODES.values())
        years = (args.years or ",".join(str(y) for y in range(date.today().year - 6, date.today().year - 1)))
        key_set = bool(os.environ.get("COMTRADE_API_KEY"))
        cfg = SourceConfig(
            name="UN Comtrade" + ("" if key_set else " (public preview)"),
            url=("https://comtradeapi.un.org/data/v1/get/C/A/HS" if key_set
                 else "https://comtradeapi.un.org/public/v1/preview/C/A/HS"),
            pagination="none", records_path="data",
            params={"reporterCode": reporters, "period": years.replace(":", ","),
                    "cmdCode": args.hs_codes or "03,0301,0302,0303,0304,0305,0306,0307",
                    "flowCode": "M,X", "partnerCode": "0", "includeDesc": "true"},
            api_key_env="COMTRADE_API_KEY", api_key_header="Ocp-Apim-Subscription-Key",
            field_map=COMTRADE_FIELDS, table="trade_fish_by_country")
    elif s == "obis":
        cfg = SourceConfig(
            name="OBIS occurrences", url="https://api.obis.org/v3/occurrence",
            pagination="cursor", records_path="results", page_size=1000,
            params={"scientificname": args.species or "Salmo salar"}, field_map=OBIS_FIELDS,
            id_field="id", total_path="total", size_param="size", cursor_param="after",
            table="species_occurrences")
    elif s == "arcgis":
        if not args.url:
            raise SystemExit("--source arcgis needs --url (a FeatureServer/MapServer layer URL)")
        url = args.url.rstrip("/")
        if not url.endswith("/query"):
            url += "/query"
        cfg = SourceConfig(
            name="ArcGIS REST layer", url=url, pagination="arcgis", records_path="features",
            page_size=1000, params={"where": "1=1", "outFields": "*", "returnGeometry": "true",
                                    "outSR": "4326", "f": "json"},
            transform=_arcgis_feature_to_record, id_field="OBJECTID", table="map_layer")
    else:  # custom
        if not args.url:
            raise SystemExit("--source custom needs --url")
        cfg = SourceConfig(name="Custom JSON API", url=args.url, pagination="page",
                           records_path="data", id_field="id")

    overrides: dict[str, Any] = {}
    if args.url and s not in ("arcgis",):
        overrides["url"] = args.url
    for attr in ("pagination", "page_size", "id_field", "page_param", "size_param",
                 "offset_param", "cursor_param"):
        value = getattr(args, attr)
        if value is not None:
            overrides[attr] = value
    if args.records_path is not None:
        overrides["records_path"] = args.records_path or None  # "" means the body is the list
    if args.param:
        overrides["params"] = {**cfg.params, **dict(_parse_kv(p) for p in args.param)}
    if args.field_map:
        overrides["field_map"] = load_field_map(args.field_map)
    if args.raw:
        overrides["field_map"] = None
    return replace(cfg, **overrides)


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
    into long-format rows: one row per cell, with each dimension's code and label."""
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
    records = []
    for flat, combo in enumerate(itertools.product(*categories)):
        value = values.get(str(flat)) if isinstance(values, dict) else (
            values[flat] if flat < len(values) else None)
        row: dict[str, Any] = {}
        for dim, (code, label) in zip(ids, combo):
            row[dim] = code
            row[f"{dim}_label"] = label
        row["value"] = value
        records.append(row)
    return records


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------
def create_session(user_agent: str, bearer_token: str | None = None) -> requests.Session:
    retries = Retry(
        total=5,
        connect=3,
        read=3,
        backoff_factor=1.5,          # waits about 0s, 3s, 6s, 12s, 24s between attempts
        backoff_jitter=0.5,
        status_forcelist=RETRY_STATUSES,
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
    if bearer_token:
        session.headers["Authorization"] = f"Bearer {bearer_token}"
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
    pass


def get_oauth_token(session: requests.Session, oauth: OAuthConfig) -> str:
    """OAuth2 client-credentials login. Raises FetchError with a readable reason."""
    client_id = os.environ.get(oauth.client_id_env)
    client_secret = os.environ.get(oauth.client_secret_env)
    if not client_id or not client_secret:
        raise FetchError(f"set {oauth.client_id_env} and {oauth.client_secret_env} "
                         f"(register a free API client with the data provider)")
    data = {"grant_type": "client_credentials", "client_id": client_id,
            "client_secret": client_secret}
    if oauth.scope:
        data["scope"] = oauth.scope
    try:
        resp = session.post(oauth.token_url, data=data, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        raise FetchError(f"login request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise FetchError(f"login failed with HTTP {resp.status_code}: {resp.text[:200]!r}")
    try:
        token = resp.json().get("access_token")
    except ValueError:
        token = None
    if not token:
        raise FetchError("login response had no access_token")
    return token


def fetch_json(session: requests.Session, url: str, params: dict, *, method: str = "GET",
               json_body: dict | None = None, timeout=DEFAULT_TIMEOUT) -> Any:
    """Send a request and decode JSON. Raises FetchError with a readable reason on any failure."""
    try:
        resp = session.request(method, url, params=params, json=json_body, timeout=timeout)
    except requests.exceptions.RetryError as exc:
        raise FetchError(f"gave up after retries: {exc}") from exc
    except requests.RequestException as exc:
        raise FetchError(f"network error: {exc}") from exc

    if resp.status_code >= 400:
        raise FetchError(f"HTTP {resp.status_code} from {resp.url}: {resp.text[:200]!r}")
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
    return body


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
        if not cfg.id_field:
            raise SystemExit("cursor pagination needs --id-field")
        self.cursor: Any = None

    def params(self):
        p = {self.cfg.size_param: self.cfg.page_size}
        if self.cursor is not None:
            p[self.cfg.cursor_param] = self.cursor
        return p

    def advance(self, raw_records, body):
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


PAGINATORS: dict[str, type[Paginator]] = {
    "none": NoPagination, "page": PagePaginator, "offset": OffsetPaginator,
    "range": RangePaginator, "cursor": CursorPaginator, "arcgis": ArcGISPaginator,
    "week": WeekPaginator,
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
    interrupted: bool = False


def extract_records(body: Any, cfg: SourceConfig) -> list | None:
    if cfg.parser == "jsonstat":
        return jsonstat_to_records(body)
    data = body if cfg.records_path is None else get_path(body, cfg.records_path)
    if data is None and cfg.missing_records_is_empty:
        return []
    return data if isinstance(data, list) else None


def parse_record(raw: Any, cfg: SourceConfig) -> tuple[dict, Any]:
    """Return (output row, dedup key). Raises on records that cannot be used."""
    record = cfg.transform(raw) if cfg.transform else raw
    if not isinstance(record, dict):
        raise TypeError(f"expected an object, got {type(record).__name__}: {str(record)[:80]!r}")
    key = get_path(record, cfg.id_field) if cfg.id_field else None
    row = apply_field_map(record, cfg.field_map) if cfg.field_map else record
    return row, key


def polite_sleep(delay: float) -> None:
    if delay > 0:
        time.sleep(delay + random.uniform(0, delay * 0.5))  # jitter spreads load


def scrape(cfg: SourceConfig, session: requests.Session, *, max_pages: int,
           delay: float, max_consecutive_failures: int = 3) -> ScrapeResult:
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
                body = fetch_json(session, url, params, method=cfg.method, json_body=cfg.json_body)
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

            parsed = skipped = new = 0
            for i, raw in enumerate(raw_records):
                try:
                    row, key = parse_record(raw, cfg)
                except Exception as exc:  # one bad record must never stop the run
                    skipped += 1
                    log.warning("Skipping record %d on %s: %s: %s", i, label, type(exc).__name__, exc)
                    continue
                parsed += 1
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


def add_provenance(df: pd.DataFrame, source_name: str) -> pd.DataFrame:
    """Record where and when every row came from, so merged tables stay traceable."""
    df = df.copy()
    df["source"] = source_name
    df["retrieved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return df


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
SOURCES = ["fiskeridir", "barentswatch", "brreg", "ssb_salmon", "worldbank", "comtrade",
           "obis", "arcgis", "custom"]


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
    g.add_argument("--years", help="worldbank: 2000:2024 | comtrade: 2022,2023")
    g.add_argument("--reporters", help="comtrade: UN M49 codes, e.g. 578,784 (Norway, UAE)")
    g.add_argument("--hs-codes", help="comtrade: HS codes (default 03 and 0301-0307)")
    g.add_argument("--nace", help=f"brreg: industry codes (default {NORWAY_AQUACULTURE_NACE})")
    g.add_argument("--weeks-from", help="barentswatch: first ISO week, e.g. 2025-W01")
    g.add_argument("--weeks-to", help="barentswatch: last ISO week (default: last full week)")
    g.add_argument("--last-weeks", type=int, default=52, help="ssb_salmon: number of recent weeks")
    g.add_argument("--species", help="obis: scientific name, e.g. 'Salmo salar'")

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
    a.add_argument("--raw", action="store_true",
                   help="keep every field (flattened) instead of applying the field map")

    r = p.add_argument_group("run")
    r.add_argument("--max-pages", type=int, default=1000, help="safety cap (default 1000)")
    r.add_argument("--delay", type=float, default=1.5,
                   help=f"seconds between requests, minimum {MIN_DELAY_SECONDS} (default 1.5)")
    r.add_argument("--contact", default=os.environ.get("AQUA_SCRAPER_CONTACT"),
                   help="email/URL put in the User-Agent (or set AQUA_SCRAPER_CONTACT)")
    r.add_argument("--bearer-token-env", metavar="ENV_VAR",
                   help="name of an env var holding an API token (sent as Authorization: Bearer)")
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

    token = None
    if args.bearer_token_env:
        token = os.environ.get(args.bearer_token_env)
        if not token:
            raise SystemExit(f"Environment variable {args.bearer_token_env} is not set")

    session = create_session(user_agent, token)
    if cfg.api_key_env and cfg.api_key_header and os.environ.get(cfg.api_key_env):
        session.headers[cfg.api_key_header] = os.environ[cfg.api_key_env]
    if cfg.oauth:
        try:
            session.headers["Authorization"] = f"Bearer {get_oauth_token(session, cfg.oauth)}"
            log.info("Logged in to %s.", urlparse(cfg.oauth.token_url).netloc)
        except FetchError as exc:
            log.error("Could not log in for %s: %s", cfg.name, exc)
            return 2

    # robots.txt governs crawlers; a registered API client is governed by the API's own terms
    check_robots = not args.ignore_robots and not cfg.oauth
    if check_robots and not robots_allows(session, cfg.url.split("{", 1)[0], user_agent):
        log.error("robots.txt disallows %s for this bot. Stopping (use --ignore-robots only "
                  "if the data owner has given you permission).", cfg.url)
        return 2

    started = time.monotonic()
    result = scrape(cfg, session, max_pages=args.max_pages, delay=delay)

    if not result.records:
        log.error("No records were collected (%d pages failed). Nothing written.", result.pages_failed)
        return 2

    df = add_provenance(build_dataframe(result.records), cfg.name)
    csv_path, json_path = save_outputs(df, Path(args.out_dir), args.basename)
    log.info("Saved %d records × %d columns to %s and %s", len(df), df.shape[1], csv_path, json_path)
    log.info("Summary: %d pages ok, %d pages failed, %d records skipped, %d duplicates removed, "
             "%.1fs elapsed.", result.pages_ok, result.pages_failed, result.records_skipped,
             result.duplicates, time.monotonic() - started)

    return 1 if (result.pages_failed or result.interrupted) else 0


if __name__ == "__main__":
    sys.exit(main())
