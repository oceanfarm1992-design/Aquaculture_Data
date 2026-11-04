#!/usr/bin/env python3
"""
run_all.py — build every table (global, regional, Norway, UAE, research, news) into one folder.

    python run_all.py --contact you@example.org             # all tables into ./data
    python run_all.py --contact you@example.org --only production_by_country trade_fish_by_country
    python run_all.py --list                                 # show the tables

Tables and how they link are described in SCHEMA.md. Every source is open:
no API key, account or login is needed.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date

import aquaculture_scraper as aq

# table name -> (scraper arguments, description)
JOBS: dict[str, tuple[list[str], str]] = {
    "facilities_norway": (["--source", "fiskeridir"],
                          "Norway: every registered fish-farm site"),
    "companies_norway": (["--source", "brreg"],
                         "Norway: aquaculture companies (company register)"),
    "prices_norway_salmon_export": (["--source", "ssb_salmon", "--last-weeks", "104"],
                                    "Norway: weekly salmon export volume and price"),
    "production_by_country": (["--source", "worldbank", "--countries", "all",
                               "--indicator", aq.WORLDBANK_FISHERY_INDICATORS,
                               "--years", f"1990:{date.today().year}"],
                              "All countries: aquaculture, capture and total fisheries, tonnes per year"),
    "trade_fish_by_country": (["--source", "comtrade", "--reporters", "578,784"],
                              "Norway + UAE: fish and seafood imports/exports (HS 03)"),
    "fao_aquaculture_production": (["--source", "fao_aquaculture"],
                                   "All countries: FAO production by species, area and environment since 1950"),
    "eurostat_aquaculture_production": (["--source", "eurostat"],
                                        "EU-27 + Norway + Iceland: production by species (Eurostat)"),
    "facilities_europe": (["--source", "emodnet"],
                          "Europe: aquaculture sites and areas with geometry (EMODnet)"),
    "research_publications": (["--source", "openalex", "--since-days", "35"],
                              "Scientific papers on aquaculture from the last 35 days (OpenAlex)"),
    "research_datasets": (["--source", "zenodo", "--max-pages", "40"],
                          "The 1,000 newest aquaculture datasets on Zenodo"),
    "news_articles": (["--source", "gdelt", "--timespan", "8d"],
                      "Aquaculture news from the last 8 days (GDELT)"),
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build all aquaculture tables (Norway + UAE).")
    p.add_argument("--contact", default=os.environ.get("AQUA_SCRAPER_CONTACT"),
                   help="email/URL put in the User-Agent (or set AQUA_SCRAPER_CONTACT)")
    p.add_argument("--out-dir", default="data", help="output folder (default: data)")
    p.add_argument("--only", nargs="+", choices=sorted(JOBS), metavar="TABLE",
                   help="build only these tables")
    p.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    p.add_argument("--list", action="store_true", help="list the tables and exit")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    log = logging.getLogger("run_all")

    if args.list:
        for table, (_, desc) in JOBS.items():
            print(f"{table:30s} {desc}")
        return 0

    common = ["--out-dir", args.out_dir, "--delay", str(args.delay)]
    if args.contact:
        common += ["--contact", args.contact]

    results: dict[str, str] = {}
    for table in args.only or JOBS:
        job_args, desc = JOBS[table]
        log.info("=== %s — %s ===", table, desc)
        started = time.monotonic()
        try:
            code = aq.main(job_args + common + ["--basename", table])
        except SystemExit as exc:  # configuration errors inside one job
            log.error("%s stopped: %s", table, exc)
            code = 2
        except Exception:
            log.exception("%s crashed", table)
            code = 2
        results[table] = {0: "ok", 1: "partial (some pages failed)", 2: "failed"}.get(code, f"exit {code}")
        results[table] += f", {time.monotonic() - started:.0f}s"

    log.info("=== Summary (output in %s/) ===", args.out_dir)
    for table, status in results.items():
        log.info("%-30s %s", table, status)
    return 0 if all(s.startswith("ok") for s in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
