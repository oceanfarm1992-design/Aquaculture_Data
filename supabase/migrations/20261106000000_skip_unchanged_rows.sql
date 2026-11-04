-- Stop storing the same data twice.
--
-- row_hash: a fingerprint of each row's data (not its download time). The loader reads
-- the stored hashes first and only sends rows that are new or have changed, so unchanged
-- rows are not rewritten on every run.
--
-- research_datasets is removed: Zenodo's robots.txt does not allow bots on its API, and
-- the same datasets are already in research_publications (OpenAlex lists them, type =
-- 'dataset'). The table was never loaded, so it is empty.

alter table public.facilities_norway                add column if not exists row_hash text;
alter table public.companies_norway                 add column if not exists row_hash text;
alter table public.prices_norway_salmon_export      add column if not exists row_hash text;
alter table public.production_by_country            add column if not exists row_hash text;
alter table public.trade_fish_by_country            add column if not exists row_hash text;
alter table public.fao_aquaculture_production       add column if not exists row_hash text;
alter table public.eurostat_aquaculture_production  add column if not exists row_hash text;
alter table public.facilities_europe                add column if not exists row_hash text;
alter table public.research_publications            add column if not exists row_hash text;
alter table public.news_articles                    add column if not exists row_hash text;

drop table if exists public.research_datasets;
