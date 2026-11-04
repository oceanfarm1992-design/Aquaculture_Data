#!/usr/bin/env python3
"""
aquaculture_scraper.py — polite, fault-tolerant scraper for aquaculture JSON APIs.

Writes a clean, flat dataset to:
    aquaculture_data.csv   (UTF-8)
    aquaculture_data.json  (array of records, UTF-8)

Built-in sources (choose with --source):

  fiskeridir  Norwegian Directorate of Fisheries, Aquaculture Register (open, no key).
              Every registered fish-farm site: name, species, lat/lon, capacity,
              licence holder (company), municipality / county / production area.
              https://api.fiskeridir.no/pub-aqua/api/v1/sites

  obis        OBIS (Ocean Biodiversity Information System) occurrence records for one
              species, e.g. a farmed species: --species "Salmo salar".
              https://api.obis.org/v3/occurrence  (cursor pagination)

  arcgis      Any ArcGIS REST FeatureServer/MapServer layer, e.g. the FAO or a national
              aquaculture-site layer: --url ".../FeatureServer/0"
              (paged with resultOffset/resultRecordCount)

  custom      Any JSON API: set --url, --pagination, --records-path, and optionally
              --field-map (a JSON file mapping output columns to field paths).

Field paths use dot notation with list support:
    "placement.countyName"          nested key
    "connections[0].licenseNr"      first item of a list
    "species[*].name"               every item of a list (values are joined with "; ")

Run with --raw first on a new source. It keeps every field (flattened with
pandas.json_normalize), which shows you the real field names to map.

Examples:
    python aquaculture_scraper.py --source fiskeridir --contact you@example.org
    python aquaculture_scraper.py --source obis --species "Sparus aurata" --max-pages 5
    python aquaculture_scraper.py --source arcgis --url "https://<host>/arcgis/rest/services/<svc>/FeatureServer/0" --param "where=COUNTRY='NO'"
    python aquaculture_scraper.py --source custom --url https://api.example.org/v1/farms \\
        --pagination page --records-path data --field-map my_fields.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

__version__ = "1.0.0"

log = logging.getLogger("aquaculture_scraper")

MIN_DELAY_SECONDS = 1.0
DEFAULT_TIMEOUT = (10, 30)  # (connect, read) seconds
RETRY_STATUSES = (429, 500, 502, 503, 504)

# Output column -> candidate field paths. The first path that resolves to a value is used.
FieldMap = dict[str, list[str]]


# --------------------------------------------------------------------------------------
# Source configuration
# --------------------------------------------------------------------------------------
@dataclass
class SourceConfig:
    name: str
    url: str
    pagination: str  # "page" | "offset" | "range" | "cursor" | "arcgis" | "none"
    records_path: str | None = None  # path to the record list in the body; None = body is the list
    page_size: int = 100
    params: dict[str, Any] = field(default_factory=dict)
    field_map: FieldMap | None = None  # None = keep every field (json_normalize)
    id_field: str | None = None  # used for de-duplication and cursor pagination
    total_path: str | None = None  # optional path to a "total records" count, used for progress logs
    transform: Callable[[dict], dict] | None = None  # optional per-record pre-processing
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
    "license_numbers": ["connections[*].licenseNr", "connections[*].licenseNumber"],
    "water_type": ["waterType", "waterEnvironment"],
    "placement_type": ["placementType", "placement.placementType"],
    "municipality": ["placement.municipalityName", "municipalityName"],
    "county": ["placement.countyName", "countyName"],
    "production_area": ["placement.prodAreaName", "productionAreaName", "prodAreaName"],
    "status": ["status", "siteStatus"],
    "registered_date": ["firstClearanceTime", "registeredDate", "clearanceDate"],
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


def build_source(args: argparse.Namespace) -> SourceConfig:
    """Build the SourceConfig for the chosen preset, then apply CLI overrides."""
    if args.source == "fiskeridir":
        cfg = SourceConfig(
            name="Fiskeridirektoratet Aquaculture Register",
            url="https://api.fiskeridir.no/pub-aqua/api/v1/sites",
            pagination="range",
            records_path=None,
            page_size=100,
            field_map=FISKERIDIR_FIELDS,
            id_field="siteId",
        )
    elif args.source == "obis":
        cfg = SourceConfig(
            name="OBIS occurrences",
            url="https://api.obis.org/v3/occurrence",
            pagination="cursor",
            records_path="results",
            page_size=1000,  # OBIS accepts up to 10000
            params={"scientificname": args.species or "Salmo salar"},
            field_map=OBIS_FIELDS,
            id_field="id",
            total_path="total",
            size_param="size",
            cursor_param="after",
        )
    elif args.source == "arcgis":
        if not args.url:
            raise SystemExit("--source arcgis needs --url (a FeatureServer/MapServer layer URL)")
        url = args.url.rstrip("/")
        if not url.endswith("/query"):
            url += "/query"
        cfg = SourceConfig(
            name="ArcGIS REST layer",
            url=url,
            pagination="arcgis",
            records_path="features",
            page_size=1000,
            params={"where": "1=1", "outFields": "*", "returnGeometry": "true",
                    "outSR": "4326", "f": "json"},
            transform=_arcgis_feature_to_record,
            id_field="OBJECTID",
        )
    else:  # custom
        if not args.url:
            raise SystemExit("--source custom needs --url")
        cfg = SourceConfig(name="Custom JSON API", url=args.url, pagination="page",
                           records_path="data", id_field="id")

    overrides: dict[str, Any] = {}
    if args.url and args.source in ("fiskeridir", "obis", "custom"):
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
        allowed_methods=frozenset({"GET", "HEAD"}),
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


def fetch_json(session: requests.Session, url: str, params: dict, timeout=DEFAULT_TIMEOUT) -> Any:
    """GET a URL and decode JSON. Raises FetchError with a readable reason on any failure."""
    try:
        resp = session.get(url, params=params, timeout=timeout)
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
    # ArcGIS and some other APIs report errors inside an HTTP 200 body
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        raise FetchError(f"API error {err.get('code')}: {err.get('message')} {err.get('details') or ''}")
    return body


# --------------------------------------------------------------------------------------
# Pagination strategies
# --------------------------------------------------------------------------------------
class Paginator:
    """Each paginator returns the query params for the current page and moves forward."""
    can_skip_failed_page = True

    def __init__(self, cfg: SourceConfig):
        self.cfg = cfg

    def params(self) -> dict:
        return {}

    def advance(self, raw_records: list, body: Any) -> bool:
        """Move to the next page. Return False when there are no more pages."""
        return False

    def skip(self) -> None:
        """Skip a page that failed."""
        self.advance([None] * self.cfg.page_size, None)


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
        return True  # stops at the first empty page


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


PAGINATORS: dict[str, type[Paginator]] = {
    "none": NoPagination, "page": PagePaginator, "offset": OffsetPaginator,
    "range": RangePaginator, "cursor": CursorPaginator, "arcgis": ArcGISPaginator,
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


def extract_records(body: Any, records_path: str | None) -> list | None:
    data = body if records_path is None else get_path(body, records_path)
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
            log.info("Fetching page %d...", page_no)

            try:
                body = fetch_json(session, cfg.url, params)
                raw_records = extract_records(body, cfg.records_path)
                if raw_records is None:
                    keys = list(body)[:10] if isinstance(body, dict) else type(body).__name__
                    raise FetchError(f"no list of records at path {cfg.records_path!r} "
                                     f"(top-level keys: {keys}); set --records-path")
            except FetchError as exc:
                result.pages_failed += 1
                consecutive_failures += 1
                log.error("Page %d failed: %s", page_no, exc)
                if not pager.can_skip_failed_page:
                    log.error("This pagination type cannot skip a failed page; stopping.")
                    break
                if consecutive_failures >= max_consecutive_failures:
                    log.error("%d pages in a row failed; stopping.", consecutive_failures)
                    break
                pager.skip()
                continue

            consecutive_failures = 0
            if cfg.total_path and total_hint is None:
                total_hint = get_path(body, cfg.total_path)
                if total_hint is not None:
                    log.info("Server reports %s matching records in total.", total_hint)

            if not raw_records:
                log.info("Page %d returned no records — reached the end.", page_no)
                break

            parsed = skipped = new = 0
            for i, raw in enumerate(raw_records):
                try:
                    row, key = parse_record(raw, cfg)
                except Exception as exc:  # one bad record must never stop the run
                    skipped += 1
                    log.warning("Skipping record %d on page %d: %s: %s",
                                i, page_no, type(exc).__name__, exc)
                    continue
                parsed += 1
                if key is not None:
                    if key in seen_keys:
                        result.duplicates += 1
                        continue
                    seen_keys.add(key)
                result.records.append(row)
                new += 1

            result.pages_ok += 1
            result.records_skipped += skipped
            progress = f" of {total_hint}" if total_hint is not None else ""
            log.info("Successfully parsed %d records on page %d (%d skipped, %d new). "
                     "Running total: %d%s", parsed, page_no, skipped, new,
                     len(result.records), progress)

            if new == 0 and parsed > 0:
                log.warning("Page %d added no new records; the API is probably ignoring the "
                            "pagination parameters. Stopping to avoid a loop.", page_no)
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
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scrape aquaculture data from a JSON API to CSV + JSON.",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split("Examples:", 1)[1] if __doc__ else None)
    p.add_argument("--source", choices=["fiskeridir", "obis", "arcgis", "custom"],
                   default="fiskeridir", help="built-in source preset (default: fiskeridir)")
    p.add_argument("--url", help="endpoint URL (required for arcgis/custom; overrides a preset)")
    p.add_argument("--species", help="OBIS: scientific name, e.g. 'Salmo salar'")
    p.add_argument("--param", action="append", metavar="KEY=VALUE",
                   help="extra query parameter (repeat for more)")
    p.add_argument("--pagination", choices=sorted(PAGINATORS), help="override pagination type")
    p.add_argument("--records-path", help="dot path to the record list ('' = body is the list)")
    p.add_argument("--id-field", help="field path used for de-dup / cursor pagination")
    p.add_argument("--page-size", type=int, help="records per request")
    p.add_argument("--page-param"); p.add_argument("--size-param")
    p.add_argument("--offset-param"); p.add_argument("--cursor-param")
    p.add_argument("--field-map", help="JSON file: {output_column: path or [paths]}")
    p.add_argument("--raw", action="store_true",
                   help="keep every field (flattened) instead of applying the field map")
    p.add_argument("--max-pages", type=int, default=500, help="safety cap (default 500)")
    p.add_argument("--delay", type=float, default=1.5,
                   help=f"seconds between requests, minimum {MIN_DELAY_SECONDS} (default 1.5)")
    p.add_argument("--contact", default=os.environ.get("AQUA_SCRAPER_CONTACT"),
                   help="email/URL put in the User-Agent (or set AQUA_SCRAPER_CONTACT)")
    p.add_argument("--bearer-token-env", metavar="ENV_VAR",
                   help="name of an env var holding an API token (sent as Authorization: Bearer)")
    p.add_argument("--ignore-robots", action="store_true", help="skip the robots.txt check")
    p.add_argument("--out-dir", default=".", help="output folder (default: current folder)")
    p.add_argument("--basename", default="aquaculture_data", help="output file name without extension")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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
    if not args.ignore_robots and not robots_allows(session, cfg.url, user_agent):
        log.error("robots.txt disallows %s for this bot. Stopping (use --ignore-robots only "
                  "if the data owner has given you permission).", cfg.url)
        return 2

    started = time.monotonic()
    result = scrape(cfg, session, max_pages=args.max_pages, delay=delay)

    if not result.records:
        log.error("No records were collected (%d pages failed). Nothing written.", result.pages_failed)
        return 2

    df = build_dataframe(result.records)
    csv_path, json_path = save_outputs(df, Path(args.out_dir), args.basename)
    log.info("Saved %d records × %d columns to %s and %s", len(df), df.shape[1], csv_path, json_path)
    log.info("Summary: %d pages ok, %d pages failed, %d records skipped, %d duplicates removed, "
             "%.1fs elapsed.", result.pages_ok, result.pages_failed, result.records_skipped,
             result.duplicates, time.monotonic() - started)

    return 1 if (result.pages_failed or result.interrupted) else 0


if __name__ == "__main__":
    sys.exit(main())
