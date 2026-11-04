# Aquaculture_Data

[![Tests](https://github.com/oceanfarm1992-design/Aquaculture_Data/actions/workflows/tests.yml/badge.svg)](https://github.com/oceanfarm1992-design/Aquaculture_Data/actions/workflows/tests.yml)

A polite, fault-tolerant Python bot that collects public aquaculture data — global production statistics, European and Norwegian farm sites, companies, prices, trade, research and news — and keeps it as clean, flat CSV (UTF-8) and JSON tables, a permanent archive and (optionally) a Supabase database.

**No API key, account or login is needed to scrape.** Every source is open data. Saving to your own Supabase database is optional; see [Storing data in Supabase](#storing-data-in-supabase).

## Quick start

```bash
pip install -r requirements.txt
```

Build every table into `data/`:

```bash
python run_all.py --contact you@example.org
```

`python run_all.py --list` shows the tables, and `--only TABLE ...` builds just some of them. The tables, their columns and how they link are described in [SCHEMA.md](SCHEMA.md).

## Tables

| Table | Data | Source |
|---|---|---|
| `fao_aquaculture_production` | Every country × species × area × environment × year since 1950, tonnes and USD | FAO FishStat |
| `production_by_country` | Capture fisheries production, every country and region, since 1990 | World Bank (FAO data) |
| `eurostat_aquaculture_production` | EU-27, Norway, Iceland: production by species, area and environment | Eurostat (`fish_aq2a`) |
| `facilities_europe` | European aquaculture sites and areas (finfish, shellfish, …) with attributes and geometry | EMODnet Human Activities |
| `facilities_norway` | Every Norwegian fish-farm site: name, species, lat/lon, capacity, licence holder | Fiskeridirektoratet Aquaculture Register |
| `companies_norway` | Norwegian aquaculture companies: org number, employees, address, founding date | Brønnøysund Register Centre |
| `prices_norway_salmon_export` | Weekly salmon export volume and price | Statistics Norway (table 03024) |
| `trade_fish_by_country` | Fish and seafood imports and exports, Norway + UAE | UN Comtrade (public preview) |
| `research_publications` | Scientific papers and datasets on aquaculture (last 35 days each run; accumulates) | OpenAlex |
| `news_articles` | Aquaculture and fish-farming news (last 2 days, daily; accumulates) | GDELT |

UN Comtrade's keyless preview returns at most 500 rows per request, so the scraper sends one request per year. With the default products and both countries that is about 32 rows per year.

### What is not collected

- **Private data:** daily feeding and mortality logs, batch and traceability records, costs and buyer contracts. These belong to farms and companies.
- **Sources that need an account or key:** BarentsWatch (Norwegian lice and disease per site), USDA NASS, the full UN Comtrade API, Copernicus Marine. The bot is kept keyless.
- **Social media and job sites:** their terms forbid scraping.
- **Zenodo:** its robots.txt does not allow bots on its API. Zenodo datasets still appear in `research_publications`, which OpenAlex fills with papers and datasets alike (`type` = `dataset`).
- **The same numbers twice:** the World Bank's aquaculture and total-production figures are FAO data already in `fao_aquaculture_production`, so only capture fisheries is taken from the World Bank. World Bank rows for years with no value yet are dropped. Eurostat overlaps FAO for European countries but adds euro values and EU-specific breakdowns, so both are kept.

## How data is kept

Each piece of data is stored once. Runs re-check their sources, but only what is new or has changed gets saved.

1. **Supabase (optional, recommended).** Every row has a `row_hash`, a fingerprint of its data that ignores the download time and request URL. Before writing, the loader reads the stored hashes and sends only rows that are new or changed. The log reports this per table, for example `0 new, 2 changed, 193771 unchanged (not written)`. Nothing is deleted; `first_seen_at` records when each row first arrived.
2. **GitHub Releases (permanent).** A weekly run stores only the tables whose content changed since the last archive, as zipped CSV. Each release's `manifest.json` lists every table, its fingerprint and the release that holds its latest version, so an unchanged FAO table, for example, is kept once and referenced afterwards. If nothing changed, no release is made. Daily runs (news and research) are archived this way only when Supabase is not set up.
3. **7-day artifact.** Uploaded only when a run's data was not saved in either place, for example if the database load failed.

JSON files are still written next to the CSVs during a run (the loader reads them), but the archive keeps one format.

## Running one source

```bash
python aquaculture_scraper.py --source fiskeridir --contact you@example.org
python aquaculture_scraper.py --source worldbank --countries "NOR;ARE" --years 2000:2024
python aquaculture_scraper.py --source comtrade --reporters 578,784 --years 2019:2023
python aquaculture_scraper.py --source ssb_salmon --last-weeks 104
python aquaculture_scraper.py --source brreg --nace 03.211
python aquaculture_scraper.py --source fao_aquaculture
python aquaculture_scraper.py --source eurostat --geo NO,ES,EL
python aquaculture_scraper.py --source emodnet
python aquaculture_scraper.py --source openalex --query "salmon lice" --since-days 365
python aquaculture_scraper.py --source gdelt --timespan 30d
```

A single-source run writes `aquaculture_data.csv` and `aquaculture_data.json` to the current folder. Change that with `--out-dir` and `--basename`.

Generic sources are also available:

- `obis`: species occurrences, e.g. `--species "Sparus aurata"`
- `arcgis`: any ArcGIS REST map layer, e.g. `--url .../FeatureServer/0`
- `custom`: any JSON API, with `--url`, `--pagination`, `--records-path` and `--field-map`

Run `python aquaculture_scraper.py --help` for every option.

### Checking field names

On a new source, run with `--raw --max-pages 1` first. It keeps every field the API returns, so you can see the real field names. If a mapped column comes back empty in every record, the script warns you; fix the field map in `aquaculture_scraper.py` or pass `--field-map`.

A field map is a JSON file mapping output columns to field paths. The first path that has a value wins:

```json
{
  "facility_name": "name",
  "species": ["species[*].name", "speciesType"],
  "latitude": "location.lat"
}
```

Path syntax: `a.b` (nested key), `a[0].b` (list item), `a[*].b` (every item, joined with `; `).

## Features

- **Robust:** retries on 429/500/502/503/504 with exponential backoff and `Retry-After` support; (10s, 30s) timeouts; invalid JSON and API errors reported inside a 200 response are logged, not raised.
- **Polite:** at least 1s between requests (default 1.5s, with jitter), a descriptive User-Agent with your contact, and a robots.txt check.
- **Clean output:** nested JSON is flattened with `pandas.json_normalize`; strings are trimmed; out-of-range coordinates are blanked; duplicates are removed.
- **Referenced:** every row ends with `source`, `source_url` (the exact API request it came from), `record_url` (a link to the record, where available), `reference_url` (the dataset page to cite) and `retrieved_at`. See [SCHEMA.md](SCHEMA.md).
- **Fault-tolerant:** bad records are skipped and logged; failed pages are skipped (it stops after 3 in a row); a loop guard stops if the API ignores pagination; Ctrl+C saves partial results; files are written atomically.
- **Supports:** page, offset, range, cursor, ArcGIS, week-by-week and one-request-per-value pagination; GET and POST; JSON-stat 2.0 (statistics offices).

Exit codes: `0` success, `1` partial (some pages failed or interrupted), `2` no data or blocked by robots.txt.

## Automated runs (GitHub Actions)

- **Tests** (`.github/workflows/tests.yml`): runs all offline tests on Python 3.11, 3.12, 3.13 and 3.14 on every push to `main` and every pull request.
- **Scrape data** (`.github/workflows/scrape.yml`): builds every table every Monday at 03:17 UTC, refreshes news and research on the other days at 05:43 UTC, and runs whenever you start it by hand. It archives every run as a GitHub Release and, if Supabase is set up, loads the tables into your database (see [How data is kept](#how-data-is-kept)).

To run a scrape now:

1. Open the **Actions** tab and pick **Scrape data**.
2. Click **Run workflow**. To build only some tables, list them in the box, e.g. `production_by_country trade_fish_by_country`.
3. When the run finishes, download the `aquaculture-data-<number>` artifact from the run's page. It holds every table as CSV and JSON and is kept for 90 days. The run summary shows the row count of each table.

Optional: add a repository variable `SCRAPER_CONTACT` with your email or website (Settings → Secrets and variables → Actions → Variables). It is sent in the User-Agent so data owners can reach you. It is not a key, and the scrape runs without it.

A run is marked failed if any table fails or is only partly collected; the files that were collected are still uploaded.

## Staying up to date

- **Data:** news and research refresh daily, and every other table weekly. FAO publishes a new release once a year; the scraper finds the newest one by itself.
- **Libraries and actions:** `requirements.txt` pins exact versions, so every run uses a tested set. Each Tuesday, [Dependabot](.github/dependabot.yml) checks for new versions of the Python libraries and GitHub Actions and opens a pull request. The **Dependabot auto-merge** workflow tests it on Python 3.11 and 3.14 and merges it when the tests pass and Dependabot reports a minor or patch update. Major updates, and grouped updates that Dependabot can't classify, get a comment and wait for you.
- **When something breaks:** if a source changes its format, that table's step fails and GitHub emails you about the failed scheduled run. Paste the log into a new session to get it fixed.

## Storing data in Supabase

The weekly **Scrape data** workflow can also save every table into your Supabase database. Rows are upserted on each table's primary key, so re-running a scrape updates rows instead of duplicating them.

**1. Create the tables.** The SQL is in [`supabase/migrations/`](supabase/migrations/); there is one file per change, applied in filename order. Either:

- Link this repo in Supabase: Project → **Settings → Integrations → GitHub**, choose this repo, set the Supabase directory to `supabase`, and turn on **Deploy to production** for `main`. The Supabase GitHub app then applies the migration when it is merged into `main`.
- Or open the Supabase **SQL Editor**, then paste and run each migration file, oldest first. Each file is safe to run again.

**2. Give GitHub Actions access.** In GitHub: Settings → Secrets and variables → Actions.

| Name | Where | Value |
|---|---|---|
| `SUPABASE_URL` | Variables (or Secrets) | `https://<project-ref>.supabase.co` (Supabase → Project Settings → Data API) |
| `SUPABASE_SECRET_KEY` | **Secrets** | A `sb_secret_...` secret key (Supabase → Project Settings → API Keys). The legacy `service_role` key also works, under this name or `SUPABASE_SERVICE_ROLE_KEY` |

This key can read and write the whole database, so keep it only in GitHub Secrets. Never commit it, and don't use the `anon` / publishable key, which can't write.

**3. Run Scrape data.** The run now has a **Load into Supabase** step. Without the two settings, that step is skipped and everything else works as before.

To load from your own computer instead:

```bash
export SUPABASE_URL="https://<project-ref>.supabase.co"
export SUPABASE_SECRET_KEY="<sb_secret_... key>"
python run_all.py --contact you@example.org
python load_to_supabase.py
```

Row Level Security is on for every table with no policies, so only the service key can read the tables. To let your app or dashboard read them with the public key, add a policy, for example:

```sql
create policy "public read" on public.production_by_country for select using (true);
```

Facilities and companies are snapshots: each run overwrites them with the latest version. Prices, production and trade keep one row per period.

## Tests

The tests run offline against local mock APIs:

```bash
python -m unittest discover -v -p "test_*.py"
```

## Status

The first live run (4 Nov 2026) collected FAO (193,773 rows), EMODnet (29,256), World Bank, Fiskeridir, Brønnøysund, Statistics Norway and UN Comtrade. Eurostat, OpenAlex past page 1, Zenodo and GDELT failed, and have since been fixed or removed. Things that may still need adjusting:

- **Fiskeridir:** species, company, org numbers and status came back empty in the first live run. The next run logs the first raw record of each source, which shows the real field names.
- **FAO:** the release zip is looked up at `https://www.fao.org/fishery/static/Data/Aquaculture_<year>.1.<n>.zip` for the last four years. If FAO has renamed it, pass the zip link from the [download page](https://www.fao.org/fishery/en/collection/aquaculture) with `--url`. Columns and code lists are matched by name, and the log lists what was found if anything is missing.
- **EMODnet:** layers are found by matching names and titles (`--layer-pattern`), so a renamed layer is still picked up.
- **Brønnøysund:** if it has moved to the new SN2025 industry codes, pass them with `--nace`.

Run a source once with `--raw --max-pages 1` to see the real field names.

## License

MIT. See [LICENSE](LICENSE).

The license covers this code only. Data you collect stays under its source's own terms. Notable ones:

- **FAO FishStat:** CC BY-NC-SA 3.0 IGO. Non-commercial use only, with attribution.
- **Norwegian government data:** NLOD.
- **OpenAlex:** CC0.
- **EMODnet:** CC BY 4.0.
- **GDELT and UN Comtrade:** their own terms of use.

Check the source's terms before republishing.
