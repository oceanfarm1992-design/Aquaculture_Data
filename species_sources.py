"""
species_sources.py — species-level data for aquaculture species, used by aquaculture_scraper.py.

Which species: every name in species.txt (edit it to add your own), plus the top
--top-species aquaculture species by FAO production in the last five years.

Sources (all open, no API key):
  WoRMS        World Register of Marine Species REST API: accepted name, authority,
               classification, synonyms, common names, distribution (native / introduced /
               invasive), traits (e.g. IUCN category, body size) and NCBI / FishBase / ITIS IDs.
  GBIF         Global Biodiversity Information Facility API: taxon match, IUCN Red List
               category, recent georeferenced occurrence records, record counts by country.
  NCBI         NCBI Datasets API: sequenced genome assemblies.
  FishBase     The FishBase database, published as Parquet files on Hugging Face (the
               source the rfishbase R package uses): biology, life history, growth,
               length-weight, maturity, spawning, fecundity, ecology, distribution, and
               which species are used in aquaculture.

Each fetcher returns (pairs, failed_requests) like bulk_sources: pairs are (row, source_url).
Name lookups are cached, so several tables built in one run share one lookup per species.
"""
from __future__ import annotations

import io
import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests

import aquaculture_scraper as aq
from fingerprint import row_fingerprint

log = logging.getLogger("aquaculture_scraper")

WORMS = "https://www.marinespecies.org/rest"
GBIF = "https://api.gbif.org/v1"
NCBI = "https://api.ncbi.nlm.nih.gov/datasets/v2"
HF_DATASET = "https://huggingface.co/api/datasets/cboettig/fishbase/tree/main/data/fb"
HF_FILES = "https://huggingface.co/datasets/cboettig/fishbase/resolve/main/"

BINOMIAL = re.compile(r"^[A-Z][a-z]+ (?!spp?\.?$)[a-z][a-z-]+$")
FISHBASE_AQUACULTURE = {"commercial", "experimental", "likely future use"}
DEFAULT_FISHBASE_TABLES = ("popgrowth", "poplw", "popchar", "maturity", "spawning", "fecundity",
                           "ecology", "country", "introductions", "genetics", "oxygen", "stocks")

# Module-level caches: run_all.py builds every table in one process
_species_cache: dict[tuple, list[dict]] = {}
_worms_cache: dict[str, tuple[dict | None, str | None]] = {}
_gbif_cache: dict[str, tuple[dict | None, str | None]] = {}
_fishbase_cache: dict[str, Any] = {}


def clear_caches() -> None:
    for c in (_species_cache, _worms_cache, _gbif_cache, _fishbase_cache):
        c.clear()


# --------------------------------------------------------------------------------------
# Which species
# --------------------------------------------------------------------------------------
def read_species_file(path: str | None) -> list[str]:
    if not path or not Path(path).exists():
        return []
    names = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        name = line.split("#", 1)[0].strip()
        if name:
            names.append(" ".join(name.split()))
    return names


def top_fao_species(rows: list[dict], top: int, years: int = 5) -> list[dict]:
    """The biggest aquaculture species by tonnes over the last `years` years in FAO data.
    Only proper species (Genus species), not groups such as 'Cyprinidae' or 'Osteichthyes'."""
    if top <= 0 or not rows:
        return []
    df = pd.DataFrame(rows)
    need = {"scientific_name", "measure", "year", "value"}
    if not need <= set(df.columns):
        return []
    df = df[df["measure"].astype(str).str.upper().str.startswith("Q")]  # quantities, not USD
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df[df["year"] >= df["year"].max() - years + 1]
    df = df[df["scientific_name"].astype(str).str.match(BINOMIAL)]
    agg = {"value": "sum"}
    for col in ("species_code", "species"):
        if col in df.columns:
            agg[col] = "first"
    ranked = df.groupby("scientific_name").agg(agg).sort_values("value", ascending=False).head(top)
    return [{"scientific_name": name, "fao_code": r.get("species_code"), "fao_name": r.get("species"),
             "recent_tonnes": float(r["value"])} for name, r in ranked.iterrows()]


def load_fao_rows(session: requests.Session, cfg: "aq.SourceConfig", delay: float) -> list[dict]:
    path = Path(cfg.params.get("fao_json") or "")
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    import bulk_sources  # not built in this run: download the FAO release
    fao_cfg = aq.SourceConfig(name="FAO", url="https://www.fao.org/fishery/static/Data/", pagination="none")
    pairs, _ = bulk_sources.fetch_fao_zip(session, fao_cfg, delay)
    return [row for row, _ in pairs]


def species_list(session: requests.Session, cfg: "aq.SourceConfig", delay: float) -> list[dict]:
    """[{scientific_name, fao_code, fao_name, recent_tonnes}], file names first, no repeats."""
    key = (cfg.params.get("species_file"), cfg.params.get("top_species"), cfg.params.get("fao_json"))
    if key in _species_cache:
        return _species_cache[key]
    listed = read_species_file(cfg.params.get("species_file"))
    top = int(cfg.params.get("top_species") or 0)
    ranked: list[dict] = []
    if top:
        try:
            ranked = top_fao_species(load_fao_rows(session, cfg, delay), top)
        except aq.FetchError as exc:
            log.warning("Could not rank species by FAO production (%s); using species.txt only.", exc)
    out, seen = [], set()
    by_name = {r["scientific_name"]: r for r in ranked}
    for name in listed + [r["scientific_name"] for r in ranked]:
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(by_name.get(name, {"scientific_name": name, "fao_code": None, "fao_name": None,
                                      "recent_tonnes": None}))
    log.info("Species to look up: %d (%d from species.txt, %d from FAO's top %d)",
             len(out), len(listed), len(ranked), top)
    _species_cache[key] = out
    return out


# --------------------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------------------
class _Counter:
    def __init__(self):
        self.failed = 0


def _get(session, url: str, params: dict | None, delay: float, counter: _Counter,
         what: str) -> tuple[Any, str | None]:
    aq.polite_sleep(delay)
    try:
        return aq.fetch_json(session, url, params or {})
    except aq.FetchError as exc:
        if exc.status == 404:  # "not found" is an answer, not a failure
            return None, url
        counter.failed += 1
        log.error("%s failed: %s", what, exc)
        return None, None


# --------------------------------------------------------------------------------------
# WoRMS
# --------------------------------------------------------------------------------------
def worms_record(session, name: str, delay: float, counter: _Counter) -> tuple[dict | None, str | None]:
    """The best WoRMS record for a name (accepted first), cached per run."""
    if name in _worms_cache:
        return _worms_cache[name]
    body, url = _get(session, f"{WORMS}/AphiaRecordsByName/{quote(name)}",
                     {"like": "false", "marine_only": "false"}, delay, counter, f"WoRMS lookup of {name}")
    records = [r for r in body if isinstance(r, dict)] if isinstance(body, list) else []
    best = next((r for r in records if r.get("status") == "accepted"), records[0] if records else None)
    if best is None and url:
        log.info("WoRMS has no record for %s", name)
    _worms_cache[name] = (best, url)
    return best, url


def _valid_id(rec: dict) -> Any:
    return rec.get("valid_AphiaID") or rec.get("AphiaID")


def _worms_each(session, cfg, delay):
    """Yield (species, accepted AphiaID, record, lookup url) for every species found in WoRMS."""
    counter = _Counter()
    items = []
    for sp in species_list(session, cfg, delay):
        rec, url = worms_record(session, sp["scientific_name"], delay, counter)
        if rec:
            items.append((sp, _valid_id(rec), rec, url))
    return items, counter


def fetch_worms_taxonomy(session, cfg, delay):
    items, counter = _worms_each(session, cfg, delay)
    pairs = []
    for sp, aphia, rec, url in items:
        ids = {}
        for kind in ("ncbi", "fishbase", "tsn"):
            body, _ = _get(session, f"{WORMS}/AphiaExternalIDByAphiaID/{aphia}", {"type": kind},
                           delay, counter, f"WoRMS {kind} ID for {sp['scientific_name']}")
            ids[kind] = body[0] if isinstance(body, list) and body else None
        pairs.append(({
            "aphia_id": str(rec.get("AphiaID")), "queried_name": sp["scientific_name"],
            "scientific_name": rec.get("scientificname"), "authority": rec.get("authority"),
            "status": rec.get("status"), "rank": rec.get("rank"),
            "valid_aphia_id": str(aphia) if aphia else None, "valid_name": rec.get("valid_name"),
            "kingdom": rec.get("kingdom"), "phylum": rec.get("phylum"), "class_name": rec.get("class"),
            "order_name": rec.get("order"), "family": rec.get("family"), "genus": rec.get("genus"),
            "is_marine": rec.get("isMarine"), "is_brackish": rec.get("isBrackish"),
            "is_freshwater": rec.get("isFreshwater"), "is_terrestrial": rec.get("isTerrestrial"),
            "is_extinct": rec.get("isExtinct"), "fao_code": sp.get("fao_code"),
            "fao_recent_tonnes": sp.get("recent_tonnes"), "ncbi_taxid": ids["ncbi"],
            "fishbase_id": ids["fishbase"], "itis_tsn": ids["tsn"], "worms_url": rec.get("url"),
            "citation": rec.get("citation"),
        }, url))
    return pairs, counter.failed


def fetch_worms_common_names(session, cfg, delay):
    items, counter = _worms_each(session, cfg, delay)
    pairs = []
    for sp, aphia, rec, _ in items:
        body, url = _get(session, f"{WORMS}/AphiaVernacularsByAphiaID/{aphia}", None, delay, counter,
                         f"WoRMS common names of {sp['scientific_name']}")
        for v in body if isinstance(body, list) else []:
            if not isinstance(v, dict) or not v.get("vernacular"):
                continue
            pairs.append(({
                "cell_key": f"{aphia}|{v['vernacular'].strip().lower()}|{v.get('language_code') or ''}",
                "aphia_id": str(aphia), "scientific_name": rec.get("valid_name") or rec.get("scientificname"),
                "common_name": v["vernacular"].strip(), "language_code": v.get("language_code"),
                "language": v.get("language"),
            }, url))
    return pairs, counter.failed


def fetch_worms_synonyms(session, cfg, delay):
    items, counter = _worms_each(session, cfg, delay)
    pairs = []
    for sp, aphia, rec, _ in items:
        offset = 1
        while True:  # WoRMS returns 50 synonyms per page
            body, url = _get(session, f"{WORMS}/AphiaSynonymsByAphiaID/{aphia}", {"offset": offset},
                             delay, counter, f"WoRMS synonyms of {sp['scientific_name']}")
            page = [s for s in body if isinstance(s, dict)] if isinstance(body, list) else []
            for syn in page:
                pairs.append(({
                    "aphia_id": str(aphia), "synonym_aphia_id": str(syn.get("AphiaID")),
                    "scientific_name": rec.get("valid_name") or rec.get("scientificname"),
                    "synonym_name": syn.get("scientificname"), "synonym_authority": syn.get("authority"),
                    "synonym_status": syn.get("status"), "unaccept_reason": syn.get("unacceptreason"),
                    "rank": syn.get("rank"),
                }, url))
            if len(page) < 50:
                break
            offset += 50
    return pairs, counter.failed


def fetch_worms_distribution(session, cfg, delay):
    items, counter = _worms_each(session, cfg, delay)
    pairs = []
    for sp, aphia, rec, _ in items:
        body, url = _get(session, f"{WORMS}/AphiaDistributionsByAphiaID/{aphia}", None, delay, counter,
                         f"WoRMS distribution of {sp['scientific_name']}")
        for d in body if isinstance(body, list) else []:
            if not isinstance(d, dict):
                continue
            place = d.get("locationID") or d.get("locality") or ""
            pairs.append(({
                "cell_key": f"{aphia}|{place}|{d.get('establishmentMeans') or ''}|{d.get('recordStatus') or ''}",
                "aphia_id": str(aphia), "scientific_name": rec.get("valid_name") or rec.get("scientificname"),
                "locality": d.get("locality"), "location_id": d.get("locationID"),
                "higher_geography": d.get("higherGeography"),
                "establishment_means": d.get("establishmentMeans"),  # native, alien, ...
                "invasiveness": d.get("invasiveness"), "occurrence": d.get("occurrence"),
                "record_status": d.get("recordStatus"), "type_status": d.get("typeStatus"),
                "quality_status": d.get("qualityStatus"),
                "latitude": d.get("decimalLatitude"), "longitude": d.get("decimalLongitude"),
            }, url))
    return pairs, counter.failed


def fetch_worms_attributes(session, cfg, delay):
    items, counter = _worms_each(session, cfg, delay)
    pairs = []
    for sp, aphia, rec, _ in items:
        body, url = _get(session, f"{WORMS}/AphiaAttributesByAphiaID/{aphia}",
                         {"include_inherited": "false"}, delay, counter,
                         f"WoRMS attributes of {sp['scientific_name']}")
        for a in body if isinstance(body, list) else []:
            if not isinstance(a, dict) or not a.get("measurementType"):
                continue
            value = a.get("measurementValue")
            pairs.append(({
                "cell_key": f"{aphia}|{a.get('measurementTypeID') or a['measurementType']}|{value}|{a.get('source_id') or ''}",
                "aphia_id": str(aphia), "scientific_name": rec.get("valid_name") or rec.get("scientificname"),
                "measurement_type": a["measurementType"], "measurement_value": value,
                "reference": a.get("reference"), "quality_status": a.get("qualitystatus"),
                # sub-attributes (e.g. assessment year), kept as JSON so the list stays intact
                "details": json.dumps(a["children"], ensure_ascii=False) if a.get("children") else None,
            }, url))
    return pairs, counter.failed


# --------------------------------------------------------------------------------------
# GBIF
# --------------------------------------------------------------------------------------
def gbif_match(session, name: str, delay: float, counter: _Counter) -> tuple[dict | None, str | None]:
    if name in _gbif_cache:
        return _gbif_cache[name]
    body, url = _get(session, f"{GBIF}/species/match", {"name": name, "strict": "true"}, delay, counter,
                     f"GBIF match of {name}")
    found = body if isinstance(body, dict) and body.get("usageKey") and body.get("matchType") != "NONE" else None
    _gbif_cache[name] = (found, url)
    return found, url


def _gbif_key(match: dict) -> Any:
    return match.get("acceptedUsageKey") or match.get("usageKey")


def fetch_gbif_summary(session, cfg, delay):
    counter, pairs = _Counter(), []
    for sp in species_list(session, cfg, delay):
        match, url = gbif_match(session, sp["scientific_name"], delay, counter)
        if not match:
            continue
        key = _gbif_key(match)
        iucn, _ = _get(session, f"{GBIF}/species/{key}/iucnRedListCategory", None, delay, counter,
                       f"GBIF IUCN category of {sp['scientific_name']}")
        facets, facet_url = _get(session, f"{GBIF}/occurrence/search",
                                 {"taxonKey": key, "limit": 0, "facet": "country", "facetLimit": 300},
                                 delay, counter, f"GBIF occurrence counts of {sp['scientific_name']}")
        countries = None
        if isinstance(facets, dict):
            for f in facets.get("facets") or []:
                if str(f.get("field", "")).upper() == "COUNTRY":
                    countries = {c["name"]: c["count"] for c in f.get("counts", []) if "name" in c}
        pairs.append(({
            "gbif_key": str(key), "queried_name": sp["scientific_name"],
            "scientific_name": match.get("scientificName"), "canonical_name": match.get("canonicalName"),
            "rank": match.get("rank"), "taxonomic_status": match.get("status"),
            "match_type": match.get("matchType"), "confidence": match.get("confidence"),
            "iucn_category": iucn.get("category") if isinstance(iucn, dict) else None,
            "iucn_code": iucn.get("code") if isinstance(iucn, dict) else None,
            "occurrence_count": facets.get("count") if isinstance(facets, dict) else None,
            "countries": countries,
        }, facet_url or url))
    return pairs, counter.failed


def fetch_gbif_occurrences(session, cfg, delay):
    counter, pairs = _Counter(), []
    per_species = int(cfg.params.get("occurrences_per_species") or 300)
    this_year = date.today().year
    for sp in species_list(session, cfg, delay):
        match, _ = gbif_match(session, sp["scientific_name"], delay, counter)
        if not match:
            continue
        offset = 0
        while offset < per_species:
            limit = min(300, per_species - offset)  # GBIF serves at most 300 per page
            body, url = _get(session, f"{GBIF}/occurrence/search", {
                "taxonKey": _gbif_key(match), "hasCoordinate": "true", "hasGeospatialIssue": "false",
                "year": f"{this_year - 10},{this_year}", "limit": limit, "offset": offset,
            }, delay, counter, f"GBIF occurrences of {sp['scientific_name']}")
            results = body.get("results") if isinstance(body, dict) else None
            for o in results or []:
                if not isinstance(o, dict) or o.get("key") is None:
                    continue
                pairs.append(({
                    "gbif_id": str(o["key"]), "scientific_name": o.get("species") or o.get("scientificName"),
                    "taxon_key": str(o.get("taxonKey") or _gbif_key(match)),
                    "latitude": o.get("decimalLatitude"), "longitude": o.get("decimalLongitude"),
                    "event_date": o.get("eventDate"), "year": o.get("year"),
                    "country_code": o.get("countryCode"), "basis_of_record": o.get("basisOfRecord"),
                    "occurrence_status": o.get("occurrenceStatus"), "individual_count": o.get("individualCount"),
                    "depth_m": o.get("depth"), "dataset_name": o.get("datasetName"),
                    "institution_code": o.get("institutionCode"),
                }, url))
            if not results or len(results) < limit or (isinstance(body, dict) and body.get("endOfRecords")):
                break
            offset += len(results)
    return pairs, counter.failed


# --------------------------------------------------------------------------------------
# NCBI genomes
# --------------------------------------------------------------------------------------
def fetch_ncbi_genomes(session, cfg, delay):
    counter, reports = _Counter(), []
    for sp in species_list(session, cfg, delay):
        token = None
        while True:
            params = {"page_size": 100}
            if token:
                params["page_token"] = token
            body, url = _get(session, f"{NCBI}/genome/taxon/{quote(sp['scientific_name'])}/dataset_report",
                             params, delay, counter, f"NCBI genomes of {sp['scientific_name']}")
            if not isinstance(body, dict):
                break
            reports += [(r, url) for r in body.get("reports") or [] if isinstance(r, dict)]
            token = body.get("next_page_token")
            if not token:
                break
    # GenBank (GCA_) and RefSeq (GCF_) copies of one assembly: keep the RefSeq one only
    refseq = {r.get("accession") for r, _ in reports if str(r.get("accession", "")).startswith("GCF_")}
    pairs = []
    for r, url in reports:
        acc = r.get("accession")
        if not acc or (acc.startswith("GCA_") and r.get("paired_accession") in refseq):
            continue
        info, stats = r.get("assembly_info") or {}, r.get("assembly_stats") or {}
        org, ann = r.get("organism") or {}, r.get("annotation_info") or {}
        pairs.append(({
            "accession": acc, "paired_accession": r.get("paired_accession"),
            "organism": org.get("organism_name"), "tax_id": org.get("tax_id"),
            "assembly_name": info.get("assembly_name"), "assembly_level": info.get("assembly_level"),
            "assembly_status": info.get("assembly_status"), "refseq_category": info.get("refseq_category"),
            "release_date": info.get("release_date"), "bioproject": info.get("bioproject_accession"),
            "total_sequence_length": stats.get("total_sequence_length"),
            "contig_n50": stats.get("contig_n50"), "scaffold_n50": stats.get("scaffold_n50"),
            "gc_percent": stats.get("gc_percent"), "chromosomes": stats.get("total_number_of_chromosomes"),
            "annotation_name": ann.get("name"), "annotation_provider": ann.get("provider"),
        }, url))
    return pairs, counter.failed


# --------------------------------------------------------------------------------------
# FishBase (Parquet on Hugging Face)
# --------------------------------------------------------------------------------------
def _version_key(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def fishbase_files(session, delay: float) -> tuple[dict[str, str], str]:
    """{table name: download URL} for the newest FishBase release, and its version."""
    if "files" in _fishbase_cache:
        return _fishbase_cache["files"]
    aq.polite_sleep(delay)
    try:
        body, _ = aq.fetch_json(session, HF_DATASET, {"recursive": "true"})
    except aq.FetchError as exc:
        raise aq.FetchError(f"could not list the FishBase files on Hugging Face: {exc}") from exc
    by_version: dict[str, dict[str, str]] = {}
    for f in body if isinstance(body, list) else []:
        path = str(f.get("path", "")) if isinstance(f, dict) else ""
        m = re.search(r"/(v\d+[\d.]*)/parquet/([^/]+)\.parquet$", path)
        if m:
            by_version.setdefault(m.group(1), {})[m.group(2).lower()] = HF_FILES + path
    if not by_version:
        raise aq.FetchError("no FishBase Parquet files found on Hugging Face")
    version = max(by_version, key=_version_key)
    _fishbase_cache["files"] = (by_version[version], version)
    log.info("FishBase release %s: %d tables available", version, len(by_version[version]))
    return _fishbase_cache["files"]


def fishbase_table(session, name: str, delay: float) -> pd.DataFrame | None:
    cache_key = f"table:{name}"
    if cache_key in _fishbase_cache:
        return _fishbase_cache[cache_key]
    files, _ = fishbase_files(session, delay)
    url = files.get(name.lower())
    if not url:
        log.warning("FishBase has no table named %s; skipped.", name)
        _fishbase_cache[cache_key] = None
        return None
    aq.polite_sleep(delay)
    resp = session.get(url, timeout=(15, 300))
    if resp.status_code >= 400:
        raise aq.FetchError(f"FishBase table {name}: HTTP {resp.status_code}", status=resp.status_code)
    df = pd.read_parquet(io.BytesIO(resp.content))
    log.info("FishBase %s: %d rows", name, len(df))
    _fishbase_cache[cache_key] = df
    return df


def _records(df: pd.DataFrame) -> list[dict]:
    """DataFrame rows as plain JSON-ready dicts (numpy types and NaN removed)."""
    return json.loads(df.to_json(orient="records", date_format="iso"))


def fishbase_species(session, cfg, delay) -> tuple[pd.DataFrame, str]:
    """FishBase species rows for our species list plus every species FishBase marks as used in
    aquaculture, with family / order / class names added."""
    if "species" in _fishbase_cache:
        return _fishbase_cache["species"]
    species = fishbase_table(session, "species", delay)
    if species is None:
        raise aq.FetchError("FishBase has no species table")
    species = species.copy()
    species["scientific_name"] = (species["Genus"].astype(str).str.strip() + " "
                                  + species["Species"].astype(str).str.strip())
    wanted = {s["scientific_name"].lower() for s in species_list(session, cfg, delay)}
    used = species.get("UsedforAquaculture")
    mask = species["scientific_name"].str.lower().isin(wanted)
    if used is not None:
        mask |= used.astype(str).str.strip().str.lower().isin(FISHBASE_AQUACULTURE)
    chosen = species[mask]
    families = fishbase_table(session, "families", delay)
    if families is not None and "FamCode" in families.columns and "FamCode" in chosen.columns:
        keep = [c for c in ("FamCode", "Family", "Order", "Class") if c in families.columns]
        chosen = chosen.merge(families[keep], on="FamCode", how="left")
    _, version = fishbase_files(session, delay)
    log.info("FishBase species selected: %d (species list + species used in aquaculture)", len(chosen))
    _fishbase_cache["species"] = (chosen, version)
    return chosen, version


def fetch_fishbase_profile(session, cfg, delay):
    chosen, version = fishbase_species(session, cfg, delay)
    estimate = fishbase_table(session, "estimate", delay)
    merged = chosen
    if estimate is not None and "SpecCode" in estimate.columns:
        extra = estimate.drop(columns=[c for c in estimate.columns if c in chosen.columns and c != "SpecCode"])
        merged = chosen.merge(extra, on="SpecCode", how="left")
    url = HF_FILES + f"data/fb/{version}/parquet/species.parquet"
    pairs = []
    for row in _records(merged):
        pairs.append(({**row, "data": row}, url))
    return pairs, 0


def fetch_fishbase_records(session, cfg, delay):
    chosen, version = fishbase_species(session, cfg, delay)
    codes = set(chosen["SpecCode"].tolist())
    names = dict(zip(chosen["SpecCode"], chosen["scientific_name"]))
    tables = [t.strip() for t in (cfg.params.get("fishbase_tables") or ",".join(DEFAULT_FISHBASE_TABLES)).split(",") if t.strip()]
    pairs, failed = [], 0
    for table in tables:
        try:
            df = fishbase_table(session, table, delay)
        except (aq.FetchError, requests.RequestException, ValueError) as exc:
            failed += 1
            log.error("FishBase table %s failed: %s", table, exc)
            continue
        if df is None or "SpecCode" not in df.columns:
            continue
        df = df[df["SpecCode"].isin(codes)]
        id_col = next((c for c in df.columns if c.lower() in ("autoctr", "autocounter")), None)
        url = HF_FILES + f"data/fb/{version}/parquet/{table}.parquet"
        for row in _records(df):
            row_key = str(row[id_col]) if id_col and row.get(id_col) is not None else row_fingerprint(row)
            pairs.append(({"fb_table": table, "row_key": row_key, "spec_code": row.get("SpecCode"),
                           "scientific_name": names.get(row.get("SpecCode")), "data": row}, url))
    return pairs, failed


# --------------------------------------------------------------------------------------
# Source definitions (used by aquaculture_scraper.build_source)
# --------------------------------------------------------------------------------------
def _cols(*names: str) -> dict[str, list[str]]:
    return {n: [n] for n in names}


PROFILE_FIELDS = {
    "spec_code": ["SpecCode"], "scientific_name": ["scientific_name"], "author": ["Author"],
    "common_name": ["FBname"], "family": ["Family"], "order_name": ["Order"], "class_name": ["Class"],
    "body_shape": ["BodyShapeI"], "freshwater": ["Fresh"], "brackish": ["Brack"], "saltwater": ["Saltwater"],
    "habitat": ["DemersPelag"], "migration": ["AnaCat"],
    "depth_min_m": ["DepthRangeShallow"], "depth_max_m": ["DepthRangeDeep"],
    "max_length_cm": ["Length"], "max_length_type": ["LTypeMaxM"], "common_length_cm": ["CommonLength"],
    "max_weight_g": ["Weight"], "max_age_years": ["LongevityWild"],
    "trophic_level": ["Troph", "FoodTroph", "DietTroph"],
    "temp_pref_min_c": ["TempPrefMin"], "temp_pref_mean_c": ["TempPrefMean"], "temp_pref_max_c": ["TempPrefMax"],
    "growth_k": ["K"], "vulnerability": ["Vulnerability"], "dangerous": ["Dangerous"],
    "electrogenic": ["Electrogenic"], "importance": ["Importance"],
    "used_for_aquaculture": ["UsedforAquaculture"], "aquarium": ["Aquarium"], "price_category": ["PriceCateg"],
    "data": ["data"],
}

SPECIES_SOURCES: dict[str, dict] = {
    "worms_taxonomy": dict(
        fetch=fetch_worms_taxonomy, table="species_taxonomy", id_field="aphia_id",
        name="WoRMS (taxonomy)", url=WORMS, reference_url="https://www.marinespecies.org/",
        record_url=lambda r: r.get("worms_url"),
        fields=_cols("aphia_id", "queried_name", "scientific_name", "authority", "status", "rank",
                     "valid_aphia_id", "valid_name", "kingdom", "phylum", "class_name", "order_name",
                     "family", "genus", "is_marine", "is_brackish", "is_freshwater", "is_terrestrial",
                     "is_extinct", "fao_code", "fao_recent_tonnes", "ncbi_taxid", "fishbase_id",
                     "itis_tsn", "citation")),
    "worms_common_names": dict(
        fetch=fetch_worms_common_names, table="species_common_names", id_field="cell_key",
        name="WoRMS (common names)", url=WORMS, reference_url="https://www.marinespecies.org/",
        fields=_cols("cell_key", "aphia_id", "scientific_name", "common_name", "language_code", "language")),
    "worms_synonyms": dict(
        fetch=fetch_worms_synonyms, table="species_synonyms", id_field=None,
        name="WoRMS (synonyms)", url=WORMS, reference_url="https://www.marinespecies.org/",
        fields=_cols("aphia_id", "synonym_aphia_id", "scientific_name", "synonym_name",
                     "synonym_authority", "synonym_status", "unaccept_reason", "rank")),
    "worms_distribution": dict(
        fetch=fetch_worms_distribution, table="species_distribution", id_field="cell_key",
        name="WoRMS (distribution)", url=WORMS, reference_url="https://www.marinespecies.org/",
        fields=_cols("cell_key", "aphia_id", "scientific_name", "locality", "location_id",
                     "higher_geography", "establishment_means", "invasiveness", "occurrence",
                     "record_status", "type_status", "quality_status", "latitude", "longitude")),
    "worms_attributes": dict(
        fetch=fetch_worms_attributes, table="species_attributes", id_field="cell_key",
        name="WoRMS (attributes)", url=WORMS, reference_url="https://www.marinespecies.org/",
        fields=_cols("cell_key", "aphia_id", "scientific_name", "measurement_type", "measurement_value",
                     "reference", "quality_status", "details")),
    "gbif_species": dict(
        fetch=fetch_gbif_summary, table="species_gbif_summary", id_field="gbif_key",
        name="GBIF (species summary)", url=GBIF, reference_url="https://www.gbif.org/",
        record_url=lambda r: f"https://www.gbif.org/species/{r['gbif_key']}" if r.get("gbif_key") else None,
        fields=_cols("gbif_key", "queried_name", "scientific_name", "canonical_name", "rank",
                     "taxonomic_status", "match_type", "confidence", "iucn_category", "iucn_code",
                     "occurrence_count", "countries")),
    "gbif_occurrences": dict(
        fetch=fetch_gbif_occurrences, table="species_occurrences", id_field="gbif_id",
        name="GBIF (occurrences)", url=GBIF, reference_url="https://www.gbif.org/",
        record_url=lambda r: f"https://www.gbif.org/occurrence/{r['gbif_id']}" if r.get("gbif_id") else None,
        fields=_cols("gbif_id", "scientific_name", "taxon_key", "latitude", "longitude", "event_date",
                     "year", "country_code", "basis_of_record", "occurrence_status", "individual_count",
                     "depth_m", "dataset_name", "institution_code")),
    "ncbi_genomes": dict(
        fetch=fetch_ncbi_genomes, table="species_genomes", id_field="accession",
        name="NCBI Datasets (genomes)", url=NCBI, reference_url="https://www.ncbi.nlm.nih.gov/datasets/genome/",
        record_url=lambda r: f"https://www.ncbi.nlm.nih.gov/datasets/genome/{r['accession']}/" if r.get("accession") else None,
        fields=_cols("accession", "paired_accession", "organism", "tax_id", "assembly_name",
                     "assembly_level", "assembly_status", "refseq_category", "release_date", "bioproject",
                     "total_sequence_length", "contig_n50", "scaffold_n50", "gc_percent", "chromosomes",
                     "annotation_name", "annotation_provider")),
    "fishbase_profile": dict(
        fetch=fetch_fishbase_profile, table="species_profile", id_field="SpecCode",
        name="FishBase (species profile)", url=HF_DATASET, reference_url="https://www.fishbase.se/",
        record_url=lambda r: f"https://www.fishbase.se/summary/{r['SpecCode']}" if r.get("SpecCode") else None,
        fields=PROFILE_FIELDS),
    "fishbase_records": dict(
        fetch=fetch_fishbase_records, table="fishbase_records", id_field=None,
        name="FishBase (biology tables)", url=HF_DATASET, reference_url="https://www.fishbase.se/",
        fields=_cols("fb_table", "row_key", "spec_code", "scientific_name", "data")),
}


def build(source: str, args) -> "aq.SourceConfig":
    spec = SPECIES_SOURCES[source]
    return aq.SourceConfig(
        name=spec["name"], url=args.url or spec["url"], pagination="none",
        custom_fetch=spec["fetch"], field_map=spec["fields"], id_field=spec["id_field"],
        table=spec["table"], reference_url=spec["reference_url"], record_url=spec.get("record_url"),
        params={"species_file": args.species_file, "top_species": args.top_species,
                "fao_json": str(Path(args.out_dir) / "fao_aquaculture_production.json"),
                "occurrences_per_species": args.occurrences_per_species,
                "fishbase_tables": args.fishbase_tables},
    )
