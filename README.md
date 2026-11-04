# Aquaculture_Data

[![Tests](https://github.com/oceanfarm1992-design/Aquaculture_Data/actions/workflows/tests.yml/badge.svg)](https://github.com/oceanfarm1992-design/Aquaculture_Data/actions/workflows/tests.yml)

A polite, fault-tolerant Python scraper that collects aquaculture data from public APIs for **Norway** and the **UAE**, and saves it as clean, flat CSV (UTF-8) and JSON tables.

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
| `facilities_norway` | Every fish-farm site: name, species, lat/lon, capacity, licence holder | Fiskeridirektoratet Aquaculture Register |
| `companies_norway` | Aquaculture companies: org number, employees, address, founding date | Brønnøysund Register Centre |
| `prices_norway_salmon_export` | Weekly salmon export volume and price | Statistics Norway (table 03024) |
| `production_by_country` | Aquaculture production per year, Norway + UAE | World Bank (FAO data) |
| `trade_fish_by_country` | Fish and seafood imports and exports, Norway + UAE | UN Comtrade (public preview) |

UN Comtrade's keyless preview returns at most 500 rows per request, so the scraper sends one request per year. With the default products and both countries that is about 32 rows per year.

## Running one source

```bash
python aquaculture_scraper.py --source fiskeridir --contact you@example.org
python aquaculture_scraper.py --source worldbank --countries "NOR;ARE" --years 2000:2024
python aquaculture_scraper.py --source comtrade --reporters 578,784 --years 2019:2023
python aquaculture_scraper.py --source ssb_salmon --last-weeks 104
python aquaculture_scraper.py --source brreg --nace 03.211
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

- **Tests** (`.github/workflows/tests.yml`): runs all offline tests on Python 3.10, 3.12 and 3.13 on every push to `main` and every pull request.
- **Scrape data** (`.github/workflows/scrape.yml`): runs `run_all.py` every Monday at 03:17 UTC, and whenever you start it by hand. If Supabase is set up, it also loads the tables into your database.

To run a scrape now:

1. Open the **Actions** tab and pick **Scrape data**.
2. Click **Run workflow**. To build only some tables, list them in the box, e.g. `production_by_country trade_fish_by_country`.
3. When the run finishes, download the `aquaculture-data-<number>` artifact from the run's page. It holds every table as CSV and JSON and is kept for 90 days. The run summary shows the row count of each table.

Optional: add a repository variable `SCRAPER_CONTACT` with your email or website (Settings → Secrets and variables → Actions → Variables). It is sent in the User-Agent so data owners can reach you. It is not a key, and the scrape runs without it.

A run is marked failed if any table fails or is only partly collected; the files that were collected are still uploaded.

## Storing data in Supabase

The weekly **Scrape data** workflow can also save every table into your Supabase database. Rows are upserted on each table's primary key, so re-running a scrape updates rows instead of duplicating them.

**1. Create the tables.** The SQL is in [`supabase/migrations/`](supabase/migrations/); there is one file per change, applied in filename order. Either:

- Link this repo in Supabase: Project → **Settings → Integrations → GitHub**, choose this repo, set the Supabase directory to `supabase`, and turn on **Deploy to production** for `main`. The Supabase GitHub app then applies the migration when it is merged into `main`.
- Or open the Supabase **SQL Editor**, then paste and run each migration file, oldest first. Each file is safe to run again.

**2. Give GitHub Actions access.** In GitHub: Settings → Secrets and variables → Actions.

| Name | Where | Value |
|---|---|---|
| `SUPABASE_URL` | Variables (or Secrets) | `https://<project-ref>.supabase.co` (Supabase → Project Settings → Data API) |
| `SUPABASE_SERVICE_ROLE_KEY` | **Secrets** | The `service_role` key or a `sb_secret_...` secret key (Supabase → Project Settings → API Keys) |

This key can read and write the whole database, so keep it only in GitHub Secrets. Never commit it, and don't use the `anon` / publishable key, which can't write.

**3. Run Scrape data.** The run now has a **Load into Supabase** step. Without the two settings, that step is skipped and everything else works as before.

To load from your own computer instead:

```bash
export SUPABASE_URL="https://<project-ref>.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="<key>"
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

All connectors are built from each API's documented response format and tested against mock servers, but **none has been run against the live API yet**. The field names most likely to need adjusting are Fiskeridir's. If Brønnøysund has moved to the new SN2025 industry codes, pass the new codes with `--nace`. Run each source once with `--raw --max-pages 1` to check.

## License

MIT. See [LICENSE](LICENSE).

The license covers this code only. Data you collect stays under its source's own terms (for example the NLOD licence for Norwegian government data, or UN Comtrade's terms of use).
