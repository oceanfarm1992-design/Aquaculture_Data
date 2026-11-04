# Data schema

`run_all.py` writes five linked tables to `data/`, each as `.csv` and `.json`. Every row also has two provenance columns:

- `source`: which source the row came from
- `retrieved_at`: when it was downloaded (UTC)

## Database keys

In Supabase (`supabase/migrations/`), each table has a primary key that the loader upserts on:

| Table | Primary key |
|---|---|
| `facilities_norway` | `site_number` |
| `companies_norway` | `org_number` |
| `prices_norway_salmon_export` | `period`, `product_code`, `measure_code` |
| `production_by_country` | `country_iso3`, `year`, `indicator_id` |
| `trade_fish_by_country` | `year`, `reporter_code`, `flow_code`, `partner_code`, `hs_code` |

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

### `production_by_country` — World Bank indicator ER.FSH.AQUA.MT (FAO data)
One row per country per year, Norway and UAE. Category covered: 3 (production).

| Column | Meaning |
|---|---|
| `country_iso3`, `country` | Country |
| `year` | Year |
| `value`, `indicator` | Aquaculture production in tonnes |

This is a national total for all species. For a species breakdown, FAO FishStat is the next source to add.

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

| # | Category | Norway | UAE |
|---|---|---|---|
| 1 | Facility identity | `facilities_norway` | Not public |
| 2 | Species & stocking | Species names only | Not public |
| 3 | Production | National total | National total |
| 4 | Water & environment | — | — |
| 5 | Health & biosecurity | Not available without an API key | Not public |
| 7 | Economics & market | Salmon prices, trade | Trade |
| 8 | Regulatory | Licence numbers | — |
| 13 | Time series | Weekly salmon prices | — |
| 14 | Company | `companies_norway` | No open register |

Norway's site-level lice and disease data is published only through the BarentsWatch API, which needs a free registered client, so it is left out to keep the project keyless.

Not covered yet: 6 (feed), 9 (traceability), 10 (research), 12 (media) and 15 (sustainability). These are mostly private or only published as PDF reports. Possible keyless next sources: FAO FishStat (species-level production), NOAA ERDDAP (ocean conditions), and Zenodo/OpenAlex (research).
