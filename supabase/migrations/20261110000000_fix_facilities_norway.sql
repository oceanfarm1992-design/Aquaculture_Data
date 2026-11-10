-- facilities_norway: use the field names the Norwegian register actually returns (seen in
-- the first live runs).
--
-- Removed (always empty: the register's site records carry no company or status fields):
--   company, org_numbers, status
-- Added:
--   species_common_names    English names of the species the site may farm
--   species_fao_codes       their FAO 3-alpha codes (join to fao_aquaculture_production)
--   temporary_capacity      temporary permitted capacity
--   production_area_status  the area's traffic light: GRØNN (green), GUL (yellow), RØD (red)
--   license_status          status of each licence connected to the site

alter table public.facilities_norway
    drop column if exists company,
    drop column if exists org_numbers,
    drop column if exists status;

alter table public.facilities_norway
    add column if not exists species_common_names text,
    add column if not exists species_fao_codes text,
    add column if not exists temporary_capacity double precision,
    add column if not exists production_area_status text,
    add column if not exists license_status text;
