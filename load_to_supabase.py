#!/usr/bin/env python3
"""
load_to_supabase.py — upsert the tables built by run_all.py into a Supabase database.

    export SUPABASE_URL="https://<project-ref>.supabase.co"
    export SUPABASE_SERVICE_ROLE_KEY="<service_role or sb_secret_... key>"
    python load_to_supabase.py                    # loads data/*.json
    python load_to_supabase.py --only production_by_country

The tables must already exist: they are created by
supabase/migrations/20261104000000_create_aquaculture_tables.sql.

Rows are upserted on each table's primary key, so loading the same scrape twice
updates rows instead of duplicating them. Uses Supabase's REST API (PostgREST) over
HTTPS, so no database driver is needed. The key bypasses Row Level Security: keep it
in a GitHub secret or an environment variable, never in a file in the repo.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from pathlib import Path
from typing import Any

import requests

import aquaculture_scraper as aq

log = logging.getLogger("load_to_supabase")

BATCH_SIZE = 500

# table -> (primary-key columns, all columns). Must match the migration (checked by tests).
TABLES: dict[str, tuple[list[str], list[str]]] = {
    "facilities_norway": (
        ["site_number"],
        ["site_number", "facility_id", "facility_name", "species", "latitude", "longitude",
         "capacity", "capacity_unit", "company", "org_numbers", "license_numbers", "water_type",
         "placement_type", "municipality", "county", "production_area", "status",
         "registered_date", "source", "retrieved_at"]),
    "companies_norway": (
        ["org_number"],
        ["org_number", "company_name", "legal_form", "nace_code", "nace_description", "employees",
         "founded_date", "registered_date", "website", "address", "postcode", "city",
         "municipality", "country", "parent_org_number", "bankrupt", "under_liquidation",
         "source", "retrieved_at"]),
    "prices_norway_salmon_export": (
        ["period", "product_code", "measure_code"],
        ["period", "product_code", "product", "measure_code", "measure", "value",
         "source", "retrieved_at"]),
    "production_by_country": (
        ["country_iso3", "year", "indicator_id"],
        ["country_iso3", "year", "indicator_id", "country", "indicator", "value", "unit",
         "source", "retrieved_at"]),
    "trade_fish_by_country": (
        ["year", "reporter_code", "flow_code", "partner_code", "hs_code"],
        ["year", "reporter_code", "flow_code", "partner_code", "hs_code", "reporter_iso3",
         "reporter", "flow", "partner", "commodity", "net_weight_kg", "quantity",
         "quantity_unit", "trade_value_usd", "source", "retrieved_at"]),
}


class LoadError(Exception):
    pass


def _clean_value(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        if value.is_integer():  # pandas stores integers with gaps as 10.0; Postgres integer needs 10
            return int(value)
    return value


def prepare_rows(table: str, records: list[dict]) -> list[dict]:
    """Keep only the table's columns, drop rows missing a key, and de-duplicate on the key."""
    key_cols, columns = TABLES[table]
    extra = sorted({c for r in records for c in r} - set(columns))
    if extra:
        log.info("%s: ignoring columns not in the database table: %s", table, extra)

    by_key: dict[tuple, dict] = {}
    missing_key = 0
    for record in records:
        row = {c: _clean_value(record.get(c)) for c in columns}
        key = tuple(row[c] for c in key_cols)
        if any(k is None or k == "" for k in key):
            missing_key += 1
            continue
        by_key[tuple(str(k) for k in key)] = row  # later rows win
    if missing_key:
        log.warning("%s: skipped %d rows with an empty key column (%s). If this is every row, "
                    "the scraper's field names need fixing.", table, missing_key, ", ".join(key_cols))
    if len(by_key) < len(records) - missing_key:
        log.info("%s: merged %d rows that shared a key.", table, len(records) - missing_key - len(by_key))
    return list(by_key.values())


def auth_headers(api_key: str) -> dict[str, str]:
    headers = {"apikey": api_key}
    if api_key.startswith("eyJ"):  # legacy service_role JWT; new sb_secret_ keys go in apikey only
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def upsert(session: requests.Session, base_url: str, api_key: str, table: str,
           rows: list[dict], batch_size: int = BATCH_SIZE) -> int:
    key_cols, _ = TABLES[table]
    url = f"{base_url}/rest/v1/{table}"
    headers = {**auth_headers(api_key), "Content-Type": "application/json",
               "Prefer": "resolution=merge-duplicates,return=minimal"}
    sent = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        try:
            resp = session.post(url, params={"on_conflict": ",".join(key_cols)}, headers=headers,
                                data=json.dumps(batch, ensure_ascii=False).encode("utf-8"),
                                timeout=aq.DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            raise LoadError(f"network error after {sent} rows: {exc}") from exc
        if resp.status_code >= 300:
            hint = ""
            if resp.status_code == 404 or "PGRST205" in resp.text or "does not exist" in resp.text:
                hint = " (has the migration been applied to this project?)"
            elif resp.status_code in (401, 403):
                hint = " (check the key: it must be the service_role / secret key, not the anon key)"
            raise LoadError(f"HTTP {resp.status_code} after {sent} rows: {resp.text[:300]!r}{hint}")
        sent += len(batch)
        log.info("%s: upserted %d/%d rows", table, sent, len(rows))
    return sent


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Upsert scraped tables into Supabase.")
    p.add_argument("--data-dir", default="data", help="folder with the run_all.py output (default: data)")
    p.add_argument("--only", nargs="+", choices=sorted(TABLES), metavar="TABLE",
                   help="load only these tables")
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = p.parse_args(argv)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                            datefmt="%H:%M:%S")

    base_url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    api_key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SECRET_KEY") or "").strip()
    if not base_url or not api_key:
        log.error("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_SECRET_KEY).")
        return 2
    if not base_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        log.error("SUPABASE_URL should look like https://<project-ref>.supabase.co")
        return 2

    session = aq.create_session(f"AquacultureDataBot/{aq.__version__} (Supabase loader)")
    results: dict[str, str] = {}
    for table in args.only or TABLES:
        path = Path(args.data_dir) / f"{table}.json"
        if not path.exists():
            log.warning("%s: %s not found (table not scraped this run); skipping.", table, path)
            results[table] = "skipped (no file)"
            continue
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                raise LoadError(f"{path} is not a JSON list of records")
            rows = prepare_rows(table, records)
            if not rows:
                # rows that all lack a key mean the field names need fixing: report it loudly
                results[table] = "failed (no rows had a key)" if records else "skipped (empty file)"
                continue
            sent = upsert(session, base_url, api_key, table, rows, args.batch_size)
            results[table] = f"ok ({sent} rows)"
        except (LoadError, ValueError) as exc:
            log.error("%s: load failed: %s", table, exc)
            results[table] = "failed"

    log.info("=== Supabase load summary ===")
    for table, status in results.items():
        log.info("%-30s %s", table, status)
    return 1 if any(s.startswith("failed") for s in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
