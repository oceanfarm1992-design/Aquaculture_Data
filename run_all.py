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
                              "All countries: capture fisheries, tonnes per year (aquaculture is in the FAO table)"),
    "trade_fish_by_country": (["--source", "comtrade", "--reporters", "578,784"],
                              "Norway + UAE: fish and seafood imports/exports (HS 03)"),
    "fao_aquaculture_production": (["--source", "fao_aquaculture"],
                                   "All countries: FAO production by species, area and environment since 1950"),
    "eurostat_aquaculture_production": (["--source", "eurostat"],
                                        "EU-27 + Norway + Iceland: production by species (Eurostat)"),
    "facilities_europe": (["--source", "emodnet"],
                          "Europe: aquaculture sites and areas with geometry (EMODnet)"),
    "research_publications": (["--source", "openalex", "--since-days", "35", "--max-pages", "50"],
                              "Scientific papers and datasets on aquaculture from the last 35 days (OpenAlex)"),
    "news_articles": (["--source", "gdelt", "--timespan", "2d"],
                      "Aquaculture news from the last 2 days (GDELT; runs daily)"),
}

# Species data: one set of rows per species (species.txt + FAO's top 100). These change
# slowly and take ~30 minutes, so the workflow builds them monthly (--group species).
SPECIES_JOBS: dict[str, tuple[list[str], str]] = {
    "species_taxonomy": (["--source", "worms_taxonomy"],
                         "Accepted name, authority, classification, NCBI/FishBase/ITIS IDs (WoRMS)"),
    "species_common_names": (["--source", "worms_common_names"],
                             "Common names in many languages (WoRMS)"),
    "species_synonyms": (["--source", "worms_synonyms"], "Synonyms and their status (WoRMS)"),
    "species_distribution": (["--source", "worms_distribution"],
                             "Where each species lives: native, introduced, invasive (WoRMS)"),
    "species_attributes": (["--source", "worms_attributes"],
                           "Traits such as IUCN category, body size, functional group (WoRMS)"),
    "species_gbif_summary": (["--source", "gbif_species"],
                             "IUCN Red List category and occurrence counts by country (GBIF)"),
    "species_occurrences": (["--source", "gbif_occurrences"],
                            "Recent georeferenced occurrence records, 300 per species (GBIF)"),
    "species_genomes": (["--source", "ncbi_genomes"], "Sequenced genome assemblies (NCBI)"),
    "species_profile": (["--source", "fishbase_profile"],
                        "Biology: size, age, depth, habitat, trophic level, temperature, aquaculture use (FishBase)"),
    "fishbase_records": (["--source", "fishbase_records"],
                         "Growth, length-weight, maturity, spawning, fecundity, ecology, range (FishBase)"),
}

ALL_JOBS = {**JOBS, **SPECIES_JOBS}
GROUPS = {"weekly": JOBS, "species": SPECIES_JOBS, "all": ALL_JOBS}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build the aquaculture tables.")
    p.add_argument("--contact", default=os.environ.get("AQUA_SCRAPER_CONTACT"),
                   help="email/URL put in the User-Agent (or set AQUA_SCRAPER_CONTACT)")
    p.add_argument("--out-dir", default="data", help="output folder (default: data)")
    p.add_argument("--only", nargs="+", choices=sorted(ALL_JOBS), metavar="TABLE",
                   help="build only these tables")
    p.add_argument("--group", choices=sorted(GROUPS), default="weekly",
                   help="weekly: statistics, sites, research, news (default); species: per-species "
                        "data; all: both")
    p.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    p.add_argument("--list", action="store_true", help="list the tables and exit")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    log = logging.getLogger("run_all")

    if args.list:
        for group in ("weekly", "species"):
            print(f"[{group}]")
            for table, (_, desc) in GROUPS[group].items():
                print(f"  {table:32s} {desc}")
        return 0

    common = ["--out-dir", args.out_dir, "--delay", str(args.delay)]
    if args.contact:
        common += ["--contact", args.contact]

    results: dict[str, str] = {}
    for table in args.only or GROUPS[args.group]:
        job_args, desc = ALL_JOBS[table]
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
