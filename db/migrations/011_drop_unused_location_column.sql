-- Preparation for moving PostGIS out of the `public` schema (2026-09-24).
--
-- Supabase's security advisor flags public.spatial_ref_sys, the PostGIS
-- reference table, because the extension lives in `public` and is owned by
-- supabase_admin: we can neither enable row-level security on it nor revoke
-- the API roles' rights on it. The fix is to recreate the extension in the
-- `extensions` schema, which only the dashboard (Database > Extensions:
-- disable PostGIS, then enable it choosing the `extensions` schema) can do.
--
-- The only thing in this project that uses PostGIS is business.location, a
-- generated geography column (and its GiST index) built from lat/lng. Nothing
-- reads it - the public map is a static JSON file using lat/lng and
-- "near me" is computed in the browser - so it is dropped here first. That
-- way disabling the extension has nothing of ours to cascade over.
--
-- Safe to run at any time and idempotent. If a spatial feature is ever
-- needed it can be re-added from lat/lng after PostGIS is back.
drop index if exists business_location_gix;
alter table business drop column if exists location;
