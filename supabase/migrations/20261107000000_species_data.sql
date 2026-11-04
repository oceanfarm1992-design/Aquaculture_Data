-- Species data for aquaculture species (species.txt + FAO's top species by production):
-- taxonomy, names, distribution and traits (WoRMS), IUCN category and occurrences (GBIF),
-- genomes (NCBI) and biology (FishBase). Same conventions as the other tables: upserted on
-- the primary key, row_hash skips unchanged rows, first_seen_at set once, nothing deleted.

-- WoRMS: one row per species looked up (species.txt + FAO's top species)
create table if not exists public.species_taxonomy (
    aphia_id                 text not null,
    queried_name             text,
    scientific_name          text,
    authority                text,
    status                   text,
    rank                     text,
    valid_aphia_id           text,
    valid_name               text,
    kingdom                  text,
    phylum                   text,
    class_name               text,
    order_name               text,
    family                   text,
    genus                    text,
    is_marine                integer,
    is_brackish              integer,
    is_freshwater            integer,
    is_terrestrial           integer,
    is_extinct               integer,
    fao_code                 text,
    fao_recent_tonnes        double precision,
    ncbi_taxid               text,
    fishbase_id              text,
    itis_tsn                 text,
    citation                 text,
    source                   text,
    source_url               text,
    record_url               text,
    reference_url            text,
    retrieved_at             timestamptz,
    row_hash                 text,
    first_seen_at            timestamptz not null default now(),
    primary key (aphia_id)
);

-- WoRMS: common names by language
create table if not exists public.species_common_names (
    cell_key                 text not null,
    aphia_id                 text,
    scientific_name          text,
    common_name              text,
    language_code            text,
    language                 text,
    source                   text,
    source_url               text,
    record_url               text,
    reference_url            text,
    retrieved_at             timestamptz,
    row_hash                 text,
    first_seen_at            timestamptz not null default now(),
    primary key (cell_key)
);

-- WoRMS: synonyms of each accepted species
create table if not exists public.species_synonyms (
    aphia_id                 text not null,
    synonym_aphia_id         text not null,
    scientific_name          text,
    synonym_name             text,
    synonym_authority        text,
    synonym_status           text,
    unaccept_reason          text,
    rank                     text,
    source                   text,
    source_url               text,
    record_url               text,
    reference_url            text,
    retrieved_at             timestamptz,
    row_hash                 text,
    first_seen_at            timestamptz not null default now(),
    primary key (aphia_id, synonym_aphia_id)
);

-- WoRMS: where each species occurs; establishment_means = native / alien / ...
create table if not exists public.species_distribution (
    cell_key                 text not null,
    aphia_id                 text,
    scientific_name          text,
    locality                 text,
    location_id              text,
    higher_geography         text,
    establishment_means      text,
    invasiveness             text,
    occurrence               text,
    record_status            text,
    type_status              text,
    quality_status           text,
    latitude                 double precision,
    longitude                double precision,
    source                   text,
    source_url               text,
    record_url               text,
    reference_url            text,
    retrieved_at             timestamptz,
    row_hash                 text,
    first_seen_at            timestamptz not null default now(),
    primary key (cell_key)
);

-- WoRMS: traits, e.g. IUCN Red List Category, body size, functional group
create table if not exists public.species_attributes (
    cell_key                 text not null,
    aphia_id                 text,
    scientific_name          text,
    measurement_type         text,
    measurement_value        text,
    reference                text,
    quality_status           text,
    details                  jsonb,
    source                   text,
    source_url               text,
    record_url               text,
    reference_url            text,
    retrieved_at             timestamptz,
    row_hash                 text,
    first_seen_at            timestamptz not null default now(),
    primary key (cell_key)
);

-- GBIF: IUCN category and occurrence counts (countries = {country code: records})
create table if not exists public.species_gbif_summary (
    gbif_key                 text not null,
    queried_name             text,
    scientific_name          text,
    canonical_name           text,
    rank                     text,
    taxonomic_status         text,
    match_type               text,
    confidence               integer,
    iucn_category            text,
    iucn_code                text,
    occurrence_count         bigint,
    countries                jsonb,
    source                   text,
    source_url               text,
    record_url               text,
    reference_url            text,
    retrieved_at             timestamptz,
    row_hash                 text,
    first_seen_at            timestamptz not null default now(),
    primary key (gbif_key)
);

-- GBIF: recent georeferenced occurrence records
create table if not exists public.species_occurrences (
    gbif_id                  text not null,
    scientific_name          text,
    taxon_key                text,
    latitude                 double precision,
    longitude                double precision,
    event_date               text,
    year                     integer,
    country_code             text,
    basis_of_record          text,
    occurrence_status        text,
    individual_count         double precision,
    depth_m                  double precision,
    dataset_name             text,
    institution_code         text,
    source                   text,
    source_url               text,
    record_url               text,
    reference_url            text,
    retrieved_at             timestamptz,
    row_hash                 text,
    first_seen_at            timestamptz not null default now(),
    primary key (gbif_id)
);

-- NCBI Datasets: genome assemblies (RefSeq copy kept when both exist)
create table if not exists public.species_genomes (
    accession                text not null,
    paired_accession         text,
    organism                 text,
    tax_id                   bigint,
    assembly_name            text,
    assembly_level           text,
    assembly_status          text,
    refseq_category          text,
    release_date             text,
    bioproject               text,
    total_sequence_length    double precision,
    contig_n50               double precision,
    scaffold_n50             double precision,
    gc_percent               double precision,
    chromosomes              integer,
    annotation_name          text,
    annotation_provider      text,
    source                   text,
    source_url               text,
    record_url               text,
    reference_url            text,
    retrieved_at             timestamptz,
    row_hash                 text,
    first_seen_at            timestamptz not null default now(),
    primary key (accession)
);

-- FishBase species + estimate tables; data = every FishBase column (flags: -1 = yes)
create table if not exists public.species_profile (
    spec_code                text not null,
    scientific_name          text,
    author                   text,
    common_name              text,
    family                   text,
    order_name               text,
    class_name               text,
    body_shape               text,
    freshwater               integer,
    brackish                 integer,
    saltwater                integer,
    habitat                  text,
    migration                text,
    depth_min_m              double precision,
    depth_max_m              double precision,
    max_length_cm            double precision,
    max_length_type          text,
    common_length_cm         double precision,
    max_weight_g             double precision,
    max_age_years            double precision,
    trophic_level            double precision,
    temp_pref_min_c          double precision,
    temp_pref_mean_c         double precision,
    temp_pref_max_c          double precision,
    growth_k                 double precision,
    vulnerability            double precision,
    dangerous                text,
    electrogenic             text,
    importance               text,
    used_for_aquaculture     text,
    aquarium                 text,
    price_category           text,
    data                     jsonb,
    source                   text,
    source_url               text,
    record_url               text,
    reference_url            text,
    retrieved_at             timestamptz,
    row_hash                 text,
    first_seen_at            timestamptz not null default now(),
    primary key (spec_code)
);

-- FishBase biology tables (growth, length-weight, maturity, ...); data = the row
create table if not exists public.fishbase_records (
    fb_table                 text not null,
    row_key                  text not null,
    spec_code                text,
    scientific_name          text,
    data                     jsonb,
    source                   text,
    source_url               text,
    record_url               text,
    reference_url            text,
    retrieved_at             timestamptz,
    row_hash                 text,
    first_seen_at            timestamptz not null default now(),
    primary key (fb_table, row_key)
);

alter table public.species_taxonomy         enable row level security;
alter table public.species_common_names     enable row level security;
alter table public.species_synonyms         enable row level security;
alter table public.species_distribution     enable row level security;
alter table public.species_attributes       enable row level security;
alter table public.species_gbif_summary     enable row level security;
alter table public.species_occurrences      enable row level security;
alter table public.species_genomes          enable row level security;
alter table public.species_profile          enable row level security;
alter table public.fishbase_records         enable row level security;
