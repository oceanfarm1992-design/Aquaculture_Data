-- More public aquaculture data: FAO and Eurostat production, European sites (EMODnet),
-- research (OpenAlex, Zenodo) and news (GDELT). Also adds first_seen_at to every table.
--
-- Nothing is ever deleted by the loader: rows are inserted or updated (upsert).
--   first_seen_at  set by the database when a row first arrives; never overwritten
--   retrieved_at   updated on every run that still sees the row
-- So a row whose retrieved_at stops advancing has disappeared from its source.

alter table public.facilities_norway           add column if not exists first_seen_at timestamptz not null default now();
alter table public.companies_norway            add column if not exists first_seen_at timestamptz not null default now();
alter table public.prices_norway_salmon_export add column if not exists first_seen_at timestamptz not null default now();
alter table public.production_by_country       add column if not exists first_seen_at timestamptz not null default now();
alter table public.trade_fish_by_country       add column if not exists first_seen_at timestamptz not null default now();

create table if not exists public.fao_aquaculture_production (
    cell_key          text primary key,   -- country|species|area|environment|measure|year
    country_un_code   text,
    country_iso3      text,
    country           text,
    species_code      text,               -- FAO ASFIS 3-alpha code
    species           text,
    scientific_name   text,
    species_group     text,               -- ISSCAAP group
    area_code         text,
    area              text,
    environment_code  text,
    environment       text,               -- marine, brackish, freshwater
    measure           text,               -- e.g. Q_tlw (tonnes live weight), V_USD_1000
    year              integer,
    value             double precision,
    status            text,               -- FAO data quality flag
    source            text,
    source_url        text,
    record_url        text,
    reference_url     text,
    retrieved_at      timestamptz,
    first_seen_at     timestamptz not null default now()
);

create table if not exists public.eurostat_aquaculture_production (
    cell_key          text primary key,   -- every dimension code of the cell
    geo               text,
    country           text,
    species_code      text,
    species           text,
    area_code         text,
    area              text,
    environment_code  text,
    environment       text,
    unit_code         text,
    unit              text,
    year              integer,
    value             double precision,
    source            text,
    source_url        text,
    record_url        text,
    reference_url     text,
    retrieved_at      timestamptz,
    first_seen_at     timestamptz not null default now()
);

create table if not exists public.facilities_europe (
    layer          text not null,
    feature_id     text not null,
    layer_title    text,
    name           text,
    country        text,
    species        text,
    status         text,
    geometry_type  text,
    latitude       double precision,   -- the point, or the rough centre of an area
    longitude      double precision,
    properties     jsonb,              -- every attribute the layer publishes
    geometry       jsonb,              -- GeoJSON geometry (points, lease polygons)
    source         text,
    source_url     text,
    record_url     text,
    reference_url  text,
    retrieved_at   timestamptz,
    first_seen_at  timestamptz not null default now(),
    primary key (layer, feature_id)
);

create table if not exists public.research_publications (
    openalex_id       text primary key,
    doi               text,
    title             text,
    publication_year  integer,
    publication_date  date,
    type              text,
    language          text,
    journal           text,
    authors           text,
    institutions      text,
    countries         text,
    topics            text,
    cited_by_count    integer,
    is_open_access    boolean,
    open_access_url   text,
    landing_page_url  text,
    source            text,
    source_url        text,
    record_url        text,
    reference_url     text,
    retrieved_at      timestamptz,
    first_seen_at     timestamptz not null default now()
);

create table if not exists public.research_datasets (
    zenodo_id         text primary key,
    doi               text,
    title             text,
    publication_date  text,
    resource_type     text,
    creators          text,
    keywords          text,
    license           text,
    landing_page_url  text,
    source            text,
    source_url        text,
    record_url        text,
    reference_url     text,
    retrieved_at      timestamptz,
    first_seen_at     timestamptz not null default now()
);

create table if not exists public.news_articles (
    url             text primary key,
    title           text,
    seen_date       text,               -- GDELT format, e.g. 20251101T080000Z
    domain          text,
    language        text,
    source_country  text,
    image_url       text,
    source          text,
    source_url      text,
    record_url      text,
    reference_url   text,
    retrieved_at    timestamptz,
    first_seen_at   timestamptz not null default now()
);

alter table public.fao_aquaculture_production      enable row level security;
alter table public.eurostat_aquaculture_production enable row level security;
alter table public.facilities_europe               enable row level security;
alter table public.research_publications           enable row level security;
alter table public.research_datasets               enable row level security;
alter table public.news_articles                   enable row level security;
