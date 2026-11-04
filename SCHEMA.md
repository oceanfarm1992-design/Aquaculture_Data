# Data schema

`run_all.py` writes eleven tables to `data/`, each as `.csv` and `.json`. Every row ends with five provenance columns, so any value can be traced back and cited:

| Column | Meaning |
|---|---|
| `source` | Name of the source, e.g. "UN Comtrade (public preview)" |
| `source_url` | The exact API request the row came from, query string included. Opening it shows the raw data. For Statistics Norway the query is sent in the request body, so this is the table's API address; use `reference_url` to view the data |
| `record_url` | A link to this one record, where the source has one (Brønnøysund companies, ArcGIS features); otherwise empty |
| `reference_url` | The page to cite for the dataset as a whole (see below) |
| `retrieved_at` | When the row was downloaded (UTC) |

Reference pages:

| Table | `reference_url` |
|---|---|
| `facilities_norway` | https://api.fiskeridir.no/pub-aqua/api/v1/sites |
| `companies_norway` | https://data.brreg.no/enhetsregisteret/api/docs/index.html |
| `prices_norway_salmon_export` | https://www.ssb.no/en/statbank/table/03024 |
| `production_by_country` | https://data.worldbank.org/indicator/ER.FSH.AQUA.MT |
| `trade_fish_by_country` | https://comtradeplus.un.org/ |
| `fao_aquaculture_production` | https://www.fao.org/fishery/en/collection/aquaculture |
| `eurostat_aquaculture_production` | https://ec.europa.eu/eurostat/databrowser/view/fish_aq2a/default/table |
| `facilities_europe` | https://emodnet.ec.europa.eu/en/human-activities |
| `research_publications` | https://openalex.org/ (each row's `record_url` is its DOI) |
| `research_datasets` | https://zenodo.org/ (each row's `record_url` is its Zenodo page) |
| `news_articles` | https://www.gdeltproject.org/ (each row's `record_url` is the article) |

Override it for any run with `--reference-url`.

## Database keys

In Supabase (`supabase/migrations/`), each table has a primary key that the loader upserts on:

| Table | Primary key |
|---|---|
| `facilities_norway` | `site_number` |
| `companies_norway` | `org_number` |
| `prices_norway_salmon_export` | `period`, `product_code`, `measure_code` |
| `production_by_country` | `country_iso3`, `year`, `indicator_id` |
| `trade_fish_by_country` | `year`, `reporter_code`, `flow_code`, `partner_code`, `hs_code` |
| `fao_aquaculture_production` | `cell_key` (country, species, area, environment, measure, year) |
| `eurostat_aquaculture_production` | `cell_key` (every dimension code of the cell) |
| `facilities_europe` | `layer`, `feature_id` |
| `research_publications` | `openalex_id` |
| `research_datasets` | `zenodo_id` |
| `news_articles` | `url` |

Every table also has `first_seen_at`, set by the database when a row first arrives and never overwritten. Nothing is ever deleted.

## How the tables link

```
companies_norway.org_number ──< facilities_norway.org_numbers   (a site lists its licence holders' org numbers)

production_by_country.country_iso3 ── trade_fish_by_country.reporter_iso3   (NOR, ARE)
```

`facilities_norway.org_numbers` can hold several org numbers joined with `; `. Split on `; ` before joining to `companies_norway`.

## Tables

### `facilities_norway` — Norwegian Aquaculture Register (Fiskeridirektoratet)
One row per registered site. Categories covered: 1 (identity), 2 (species), 8 (licences), 11 (location).

| Column | Meaning |
|---|---|
| `facility_id` | Register's internal site ID |
| `site_number` | Official site number |
| `facility_name` | Site name |
| `species` | Permitted species (`; ` separated) |
| `latitude`, `longitude` | Position, decimal degrees |
| `capacity`, `capacity_unit` | Permitted capacity (MTB) and unit |
| `company` | Licence holder name(s) |
| `org_numbers` | Licence holders' org numbers (key to `companies_norway`) |
| `license_numbers` | Licence numbers linked to the site |
| `water_type`, `placement_type` | Salt/fresh water; sea/land |
| `municipality`, `county`, `production_area` | Location |
| `status`, `registered_date` | Site status and clearance date |

### `companies_norway` — Brønnøysund Register Centre
One row per aquaculture company, selected by industry code (NACE 03.211, 03.212, 03.221, 03.222). Category covered: 14 (company).

| Column | Meaning |
|---|---|
| `org_number` | Organisation number (key) |
| `company_name`, `legal_form` | Name and company type |
| `nace_code`, `nace_description` | Industry code |
| `employees` | Number of employees |
| `founded_date`, `registered_date` | Founding and registration dates |
| `website`, `address`, `postcode`, `city`, `municipality`, `country` | Contact and address |
| `parent_org_number` | Parent unit, if any |
| `bankrupt`, `under_liquidation` | Status flags |

### `prices_norway_salmon_export` — Statistics Norway table 03024
Long format: one row per week × product × measure. Category covered: 7 (prices).

| Column | Meaning |
|---|---|
| `period` | Week, e.g. `2025U07` |
| `product_code`, `product` | `01` fresh salmon, `02` frozen salmon |
| `measure_code`, `measure` | `Vekt` export weight (tonnes), `Kilopris` price (NOK/kg) |
| `value` | The number |

### `production_by_country` — World Bank fisheries indicators (FAO data)
One row per country (and World Bank region) × year × indicator, since 1990. Category covered: 3 (production).

| Column | Meaning |
|---|---|
| `country_iso3`, `country` | Country or region |
| `year` | Year |
| `indicator_id`, `indicator` | `ER.FSH.AQUA.MT` aquaculture, `ER.FSH.CAPT.MT` capture, `ER.FSH.PROD.MT` total fisheries |
| `value` | Tonnes |

National totals for all species. For species detail, use `fao_aquaculture_production`.

### `fao_aquaculture_production` — FAO FishStat Global Aquaculture Production
One row per country × species × area × environment × measure × year, since 1950. Category covered: 3 (production, by species).

| Column | Meaning |
|---|---|
| `country_un_code`, `country_iso3`, `country` | Country |
| `species_code`, `species`, `scientific_name`, `species_group` | FAO ASFIS code, common and scientific name, ISSCAAP group |
| `area_code`, `area` | FAO fishing area (e.g. 27 = Atlantic, Northeast) |
| `environment_code`, `environment` | Marine, brackish or freshwater |
| `measure` | e.g. `Q_tlw` (tonnes live weight) or `V_USD_1000` (thousand USD) |
| `year`, `value`, `status` | Year, value, FAO data-quality flag (e.g. estimated) |

Licence: CC BY-NC-SA 3.0 IGO (non-commercial).

### `eurostat_aquaculture_production` — Eurostat `fish_aq2a`
One row per filled cell, for EU-27 + Norway + Iceland. Category covered: 3.

| Column | Meaning |
|---|---|
| `geo`, `country` | Country code and name |
| `species_code`, `species` | Species |
| `area_code`, `area`, `environment_code`, `environment` | Fishing area and water environment |
| `unit_code`, `unit` | e.g. tonnes live weight, euro |
| `year`, `value` | Year and value |
| `cell_key` | Every dimension code of the cell (the key) |

### `facilities_europe` — EMODnet Human Activities
One row per site or area in every aquaculture layer (finfish, shellfish, …). Categories covered: 1 (identity), 11 (spatial).

| Column | Meaning |
|---|---|
| `layer`, `layer_title`, `feature_id` | Which map layer and feature |
| `name`, `country`, `species`, `status` | Picked from the layer's attributes when present |
| `geometry_type`, `latitude`, `longitude` | Point, or the rough centre of an area |
| `properties` | Every attribute the layer publishes (JSON) |
| `geometry` | The full GeoJSON shape, e.g. lease polygons (JSON) |

### `research_publications` — OpenAlex
One row per scholarly work matching "aquaculture". Each run fetches the last 35 days; the table accumulates. Category covered: 10.

Columns: `openalex_id`, `doi`, `title`, `publication_year`, `publication_date`, `type`, `language`, `journal`, `authors`, `institutions`, `countries` (authors' countries), `topics`, `cited_by_count`, `is_open_access`, `open_access_url`, `landing_page_url`.

For a one-off backfill of older papers, run `python aquaculture_scraper.py --source openalex --since-days 3650 --basename research_publications --out-dir data` and load it.

### `research_datasets` — Zenodo
One row per dataset matching "aquaculture", the newest 1,000 each run. Category covered: 10.

Columns: `zenodo_id`, `doi`, `title`, `publication_date`, `resource_type`, `creators`, `keywords`, `license`, `landing_page_url`.

### `news_articles` — GDELT
One row per news article about aquaculture, fish farming, mariculture or salmon farming from the last 8 days. Up to 250 articles per run; the table accumulates. Category covered: 12.

Columns: `url`, `title`, `seen_date`, `domain`, `language`, `source_country`, `image_url`.

### `trade_fish_by_country` — UN Comtrade
One row per country × year × flow × HS product, with the world as partner. Category covered: 7 (trade).

| Column | Meaning |
|---|---|
| `year` | Year |
| `reporter_code`, `reporter_iso3`, `reporter` | Reporting country (578 Norway, 784 UAE) |
| `flow_code`, `flow` | `M` import, `X` export |
| `partner_code`, `partner` | `0` = World |
| `hs_code`, `commodity` | HS 03 (all fish and seafood) and 0301–0307 |
| `net_weight_kg`, `quantity`, `quantity_unit` | Volume |
| `trade_value_usd` | Value in US dollars |

HS 03 covers wild-caught and farmed products together; trade statistics don't separate them. It uses the keyless public preview, capped at 500 rows per request, so the scraper sends one request per year.

## Coverage of the 15 categories

| # | Category | Global | Europe | Norway | UAE |
|---|---|---|---|---|---|
| 1 | Facility identity | — | `facilities_europe` | `facilities_norway` | Not public |
| 2 | Species | FAO species codes and names | Species | Species per site | In FAO data |
| 3 | Production | FAO by species, World Bank totals | Eurostat | In all three | FAO by species, World Bank |
| 7 | Economics & market | FAO production value (USD) | Eurostat value | Salmon prices, trade | Trade |
| 8 | Regulatory | — | — | Licence numbers | — |
| 10 | Research | OpenAlex papers, Zenodo datasets | | | |
| 11 | Spatial | — | Site points and lease polygons | Site positions | — |
| 12 | Media | GDELT news | | | |
| 13 | Time series | Yearly series since 1950 | Yearly | Weekly prices | Yearly |
| 14 | Company | — | — | `companies_norway` | No open register |

Norway's site-level lice and disease data is published only through the BarentsWatch API, which needs a free registered client, so it is left out to keep the project keyless.

Not covered: 4 (water conditions at farm level), 5 (health), 6 (feed), 9 (traceability) and 15 (sustainability). These are private, need an account, or are only published as PDF reports. A keyless candidate for regional ocean conditions is NOAA ERDDAP.
