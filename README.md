# Aquaculture_Data

A polite, fault-tolerant Python scraper that collects aquaculture data from public APIs for **Norway** and the **UAE**, and saves it as clean, flat CSV (UTF-8) and JSON tables.

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

| Table | Data | Source | Key needed? |
|---|---|---|---|
| `facilities_norway` | Every fish-farm site: name, species, lat/lon, capacity, licence holder | Fiskeridirektoratet Aquaculture Register | No |
| `health_norway_weekly` | Weekly lice, PD/ILA disease, treatments, fallowing, sea temperature per site | BarentsWatch Fish Health | Free API client |
| `companies_norway` | Aquaculture companies: org number, employees, address, founding date | Brønnøysund Register Centre | No |
| `prices_norway_salmon_export` | Weekly salmon export volume and price | Statistics Norway (table 03024) | No |
| `production_by_country` | Aquaculture production per year, Norway + UAE | World Bank (FAO data) | No |
| `trade_fish_by_country` | Fish and seafood imports and exports, Norway + UAE | UN Comtrade | Optional |

## API keys

- **BarentsWatch** (needed for `health_norway_weekly`): register for free on BarentsWatch and create an API client. Then set:

  ```bash
  export BW_CLIENT_ID="your-client-id"
  export BW_CLIENT_SECRET="your-client-secret"
  ```

  Without them, `run_all.py` skips this table and builds the others.
- **UN Comtrade** (optional): without a key, the public preview is used, capped at 500 rows per request. For the full API, get a free key from the Comtrade developer portal and set `COMTRADE_API_KEY`.

Keep keys in environment variables, not in files committed to the repo.

## Running one source

```bash
python aquaculture_scraper.py --source fiskeridir --contact you@example.org
python aquaculture_scraper.py --source worldbank --countries "NOR;ARE" --years 2000:2024
python aquaculture_scraper.py --source comtrade --reporters 578,784 --years 2022,2023
python aquaculture_scraper.py --source barentswatch --weeks-from 2025-W01 --weeks-to 2025-W52
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
- **Polite:** at least 1s between requests (default 1.5s, with jitter), a descriptive User-Agent with your contact, and a robots.txt check (skipped for registered API clients, which follow the API's own terms).
- **Clean output:** nested JSON is flattened with `pandas.json_normalize`; strings are trimmed; out-of-range coordinates are blanked; duplicates are removed; every row records its `source` and `retrieved_at`.
- **Fault-tolerant:** bad records are skipped and logged; failed pages are skipped (it stops after 3 in a row); a loop guard stops if the API ignores pagination; Ctrl+C saves partial results; files are written atomically.
- **Supports:** page, offset, range, cursor, ArcGIS and week-by-week pagination; GET and POST; JSON-stat 2.0 (statistics offices); OAuth2 client-credentials login; API-key headers.

Exit codes: `0` success, `1` partial (some pages failed or interrupted), `2` no data, login failed or blocked by robots.txt.

## Tests

The tests run offline against a local mock API:

```bash
python -m unittest -v test_aquaculture_scraper.py
```

## Status

All connectors are built from each API's documented response format and tested against mock servers, but **none has been run against the live API yet**. The field names most likely to need adjusting are Fiskeridir's and BarentsWatch's. If Brønnøysund has moved to the new SN2025 industry codes, pass the new codes with `--nace`. Run each source once with `--raw --max-pages 1` to check.

## License

MIT. See [LICENSE](LICENSE).

The license covers this code only. Data you collect stays under its source's own terms (for example the NLOD licence for Norwegian government data, or UN Comtrade's terms of use).
