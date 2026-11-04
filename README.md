# Aquaculture_Data

A polite, fault-tolerant Python scraper that collects aquaculture data from public APIs for **Norway** and the **UAE**, and saves it as clean, flat CSV (UTF-8) and JSON tables.

**No API key, account or login is needed.** Every source is open data.

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
- **Clean output:** nested JSON is flattened with `pandas.json_normalize`; strings are trimmed; out-of-range coordinates are blanked; duplicates are removed; every row records its `source` and `retrieved_at`.
- **Fault-tolerant:** bad records are skipped and logged; failed pages are skipped (it stops after 3 in a row); a loop guard stops if the API ignores pagination; Ctrl+C saves partial results; files are written atomically.
- **Supports:** page, offset, range, cursor, ArcGIS, week-by-week and one-request-per-value pagination; GET and POST; JSON-stat 2.0 (statistics offices).

Exit codes: `0` success, `1` partial (some pages failed or interrupted), `2` no data or blocked by robots.txt.

## Tests

The tests run offline against a local mock API:

```bash
python -m unittest -v test_aquaculture_scraper.py
```

## Status

All connectors are built from each API's documented response format and tested against mock servers, but **none has been run against the live API yet**. The field names most likely to need adjusting are Fiskeridir's. If Brønnøysund has moved to the new SN2025 industry codes, pass the new codes with `--nace`. Run each source once with `--raw --max-pages 1` to check.

## License

MIT. See [LICENSE](LICENSE).

The license covers this code only. Data you collect stays under its source's own terms (for example the NLOD licence for Norwegian government data, or UN Comtrade's terms of use).
