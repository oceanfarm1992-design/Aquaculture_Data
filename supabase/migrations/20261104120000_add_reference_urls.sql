-- Reference links for every row (see SCHEMA.md):
--   source_url     the exact API request the row came from (open it to see the raw data)
--   record_url     a link to this one record, where the source has one
--   reference_url  the page to cite for the dataset as a whole

alter table public.facilities_norway
    add column if not exists source_url text,
    add column if not exists record_url text,
    add column if not exists reference_url text;

alter table public.companies_norway
    add column if not exists source_url text,
    add column if not exists record_url text,
    add column if not exists reference_url text;

alter table public.prices_norway_salmon_export
    add column if not exists source_url text,
    add column if not exists record_url text,
    add column if not exists reference_url text;

alter table public.production_by_country
    add column if not exists source_url text,
    add column if not exists record_url text,
    add column if not exists reference_url text;

alter table public.trade_fish_by_country
    add column if not exists source_url text,
    add column if not exists record_url text,
    add column if not exists reference_url text;
