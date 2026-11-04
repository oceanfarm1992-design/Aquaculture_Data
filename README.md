# Aquaculture_Data

A polite, fault-tolerant Python scraper that collects aquaculture data from public JSON APIs and saves it as clean, flat `aquaculture_data.csv` (UTF-8) and `aquaculture_data.json`.

## Sources

| `--source` | Data | Pagination |
|---|---|---|
| `fiskeridir` (default) | Norwegian Aquaculture Register: every fish-farm site with name, species, lat/lon, capacity, licence holder, municipality/county/production area | `range=0-99` |
| `obis` | OBIS occurrence records for one species, e.g. `--species "Sparus aurata"` | cursor (`after`) |
| `arcgis` | Any ArcGIS REST FeatureServer/MapServer layer: `--url .../FeatureServer/0` | `resultOffset` / `exceededTransferLimit` |
| `custom` | Any JSON API: `--url`, `--pagination`, `--records-path`, `--field-map file.json` | page, offset, range, cursor or none |

## Quick start

```bash
pip install -r requirements.txt
```

Check the field names first. `--raw` keeps every field the API returns:

```bash
python aquaculture_scraper.py --source fiskeridir --raw --max-pages 1 --contact you@example.org
```

Then run the full scrape:

```bash
python aquaculture_scraper.py --source fiskeridir --contact you@example.org
```

More examples:

```bash
python aquaculture_scraper.py --source obis --species "Salmo salar" --max-pages 5
python aquaculture_scraper.py --source arcgis --url "https://<host>/arcgis/rest/services/<svc>/FeatureServer/0" --param "where=COUNTRY='NO'"
python aquaculture_scraper.py --source custom --url https://api.example.org/v1/farms --pagination page --records-path data --field-map my_fields.json
```

Run `python aquaculture_scraper.py --help` for every option.

## Field maps

A field map is a JSON file that maps output columns to field paths. The first path that has a value wins:

```json
{
  "facility_name": "name",
  "species": ["species[*].name", "speciesType"],
  "latitude": "location.lat",
  "company": "operator.company_name"
}
```

Path syntax: `a.b` (nested key), `a[0].b` (list item), `a[*].b` (every item, joined with `; `).

## Features

- **Robust:** retries on 429/500/502/503/504 with exponential backoff and `Retry-After` support; (10s, 30s) timeouts; invalid JSON and ArcGIS in-body errors are logged, not raised.
- **Polite:** at least 1s between requests (default 1.5s, with jitter), a descriptive User-Agent with your contact, and a robots.txt check.
- **Clean output:** nested JSON is flattened with `pandas.json_normalize`; strings are trimmed; out-of-range coordinates are blanked; duplicates are removed.
- **Fault-tolerant:** bad records are skipped and logged; failed pages are skipped (it stops after 3 in a row); a loop guard stops if the API ignores pagination; Ctrl+C saves partial results; files are written atomically.
- **Auth:** `--bearer-token-env VAR` reads an API token from an environment variable.

Exit codes: `0` success, `1` partial (some pages failed or interrupted), `2` no data or blocked by robots.txt.

## Tests

The tests run offline against a local mock API:

```bash
python -m unittest -v test_aquaculture_scraper.py
```

## Status

The built-in Fiskeridir field names and `range` pagination are best-effort and have not yet been verified against the live API. Run with `--raw` first and adjust `FISKERIDIR_FIELDS` (or pass `--field-map`) if any column comes back empty.

## License

MIT. See [LICENSE](LICENSE).

The license covers this code only. Data you collect stays under its source's own terms (for example the NLOD licence for Fiskeridirektoratet, or the per-dataset licences in OBIS).
