-- Initial schema: business + collection_run, per
-- kosher-map-technical-spec-v0.1.md section 5.
--
-- Design notes:
-- - Natural identity for a business is (source_id, source_record_id);
--   `id` is a surrogate key so other tables (none yet) can reference a
--   short stable integer instead of a composite key.
-- - `location` is a PostGIS geography column generated from lat/lng, so
--   it's always in sync with them and never set directly - it exists
--   purely to make "what's near me" queries (spec section 12) use a
--   GIST index instead of scanning every row's lat/lng by hand.
-- - Every enum-shaped column gets a CHECK constraint mirroring
--   collectors/common/schema.py's Python-side validation, so a bug that
--   slips past a collector's own validate_record() still can't reach
--   the database in a shape the rest of the system doesn't expect.
-- - category_raw, category_canonical, and location fields are nullable
--   even though a collector normally supplies them -
--   collectors/tlv/collector.py (a mall-address listing with no
--   resolvable coordinates) and the revoked-feed collector (no category
--   or location published at all) both produce real, valid records
--   missing these fields. See collectors/common/schema.py's
--   REQUIRED_UNLESS_REVOKED comment and the lat/location_source
--   consistency note for the full reasoning.

create extension if not exists postgis;

create table if not exists business (
    id                   bigserial primary key,
    source_id            text not null,
    source_record_id     text not null,
    source_url           text,
    name_raw             text not null,
    name_clean           text,
    branch               text,
    category_raw         text,
    category_canonical   text,
    kosher_type          text[],
    supervision_level    text,
    certifying_authority text not null,
    additional_hechsher  text,
    address_raw          text not null,
    city                 text not null,
    lat                  double precision,
    lng                  double precision,
    location             geography(point, 4326)
                         generated always as (
                             case when lat is not null and lng is not null
                                 then st_setsrid(st_makepoint(lng, lat), 4326)::geography
                             end
                         ) stored,
    location_source      text,
    location_confidence  text,
    phone                text,
    supervisor_name      text,
    supervisor_phone     text,
    first_seen           timestamptz not null default now(),
    last_seen            timestamptz not null default now(),
    status               text not null,
    status_changed_at    timestamptz,

    constraint business_source_record_uk unique (source_id, source_record_id),

    constraint business_kosher_type_ck check (
        kosher_type is null or kosher_type <@ array['meat', 'dairy', 'parve']
    ),
    constraint business_supervision_level_ck check (
        supervision_level is null
        or supervision_level in ('regular', 'mehadrin', 'badatz', 'unknown')
    ),
    constraint business_location_source_ck check (
        location_source is null or location_source in ('published', 'geocoded')
    ),
    constraint business_location_confidence_ck check (
        location_confidence is null
        or location_confidence in ('high', 'medium', 'low')
    ),
    constraint business_status_ck check (
        status in ('active', 'absent', 'revoked')
    ),
    -- Mirrors schema.py's validate_record: coordinates never appear
    -- without a location_source explaining where they came from.
    constraint business_location_source_consistency_ck check (
        (lat is null and lng is null) or location_source is not null
    )
);

create index if not exists business_status_idx on business (status);
create index if not exists business_category_canonical_idx on business (category_canonical);
create index if not exists business_source_id_idx on business (source_id);
create index if not exists business_location_gix on business using gist (location);

create table if not exists collection_run (
    id             bigserial primary key,
    source_id      text not null,
    started_at     timestamptz not null,
    finished_at    timestamptz,
    outcome        text not null,
    records_found  integer,
    records_new    integer,
    records_absent integer,
    error_detail   text,

    constraint collection_run_outcome_ck check (
        outcome in ('success', 'partial', 'failed')
    )
);

create index if not exists collection_run_source_id_idx on collection_run (source_id, started_at desc);
