-- Per-business cuisine/eatery-type ("burger", "pizza", "asian", ...) -
-- prep for a future map-pin/list-view icon feature (discussed
-- 2026-09-17). No collector produces this today, so unlike most
-- business_override fields there's nothing to "correct" - override is
-- simply the only place this data lives at all, same treatment as
-- address_raw in migration 006.
--
-- cuisine_type_source records *how* the value was set, mirroring the
-- location_source/location_confidence pattern already used for
-- lat/lng: a name-keyword guess and a Google-subheading-confirmed
-- value must stay distinguishable so a better signal can safely
-- overwrite a weaker one later without losing track of confidence.
alter table business_override add column if not exists cuisine_type text;
alter table business_override add column if not exists cuisine_type_source text;

alter table business_override drop constraint if exists business_override_cuisine_type_source_ck;
alter table business_override add constraint business_override_cuisine_type_source_ck check (
    cuisine_type_source is null
    or cuisine_type_source in ('name_heuristic', 'sanity_check', 'admin')
);

-- cuisine_type_source describes cuisine_type specifically, not the row
-- as a whole - it should never be set without an actual cuisine_type,
-- and (like every other override field) shouldn't dangle if the value
-- itself is later cleared.
alter table business_override drop constraint if exists business_override_cuisine_type_pair_ck;
alter table business_override add constraint business_override_cuisine_type_pair_ck check (
    (cuisine_type is null) = (cuisine_type_source is null)
);

alter table business_override drop constraint if exists business_override_not_empty_ck;
alter table business_override add constraint business_override_not_empty_ck check (
    lat is not null or lng is not null or name_clean is not null
    or branch is not null or category_canonical is not null
    or kosher_type is not null or supervision_level is not null
    or phone is not null or address_raw is not null
    or cuisine_type is not null
);
