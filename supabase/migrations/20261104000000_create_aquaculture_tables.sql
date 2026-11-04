-- Tables written by load_to_supabase.py (see SCHEMA.md for column meanings).
-- Each table has a natural primary key, so re-loading a scrape updates rows instead of
-- duplicating them. Row Level Security is on with no policies: only the service-role /
-- secret key (used by the loader) can read or write. Add a SELECT policy to publish.

create table if not exists public.facilities_norway (
    site_number      text primary key,
    facility_id      text,
    facility_name    text,
    species          text,
    latitude         double precision,
    longitude        double precision,
    capacity         double precision,
    capacity_unit    text,
    company          text,
    org_numbers      text,            -- '; ' separated, join to companies_norway.org_number
    license_numbers  text,
    water_type       text,
    placement_type   text,
    municipality     text,
    county           text,
    production_area  text,
    status           text,
    registered_date  text,            -- kept as text: the register's date format is unverified
    source           text,
    retrieved_at     timestamptz
);

create table if not exists public.companies_norway (
    org_number         text primary key,
    company_name       text,
    legal_form         text,
    nace_code          text,
    nace_description   text,
    employees          integer,
    founded_date       date,
    registered_date    date,
    website            text,
    address            text,
    postcode           text,
    city               text,
    municipality       text,
    country            text,
    parent_org_number  text,
    bankrupt           boolean,
    under_liquidation  boolean,
    source             text,
    retrieved_at       timestamptz
);

create table if not exists public.prices_norway_salmon_export (
    period        text not null,      -- ISO week, e.g. 2025U07
    product_code  text not null,      -- 01 fresh, 02 frozen
    product       text,
    measure_code  text not null,      -- Vekt (tonnes) or Kilopris (NOK/kg)
    measure       text,
    value         double precision,
    source        text,
    retrieved_at  timestamptz,
    primary key (period, product_code, measure_code)
);

create table if not exists public.production_by_country (
    country_iso3  text not null,
    year          integer not null,
    indicator_id  text not null,
    country       text,
    indicator     text,
    value         double precision,   -- tonnes
    unit          text,
    source        text,
    retrieved_at  timestamptz,
    primary key (country_iso3, year, indicator_id)
);

create table if not exists public.trade_fish_by_country (
    year             integer not null,
    reporter_code    text not null,
    flow_code        text not null,   -- M import, X export
    partner_code     text not null,   -- 0 = World
    hs_code          text not null,
    reporter_iso3    text,
    reporter         text,
    flow             text,
    partner          text,
    commodity        text,
    net_weight_kg    double precision,
    quantity         double precision,
    quantity_unit    text,
    trade_value_usd  double precision,
    source           text,
    retrieved_at     timestamptz,
    primary key (year, reporter_code, flow_code, partner_code, hs_code)
);

alter table public.facilities_norway           enable row level security;
alter table public.companies_norway            enable row level security;
alter table public.prices_norway_salmon_export enable row level security;
alter table public.production_by_country       enable row level security;
alter table public.trade_fish_by_country       enable row level security;
