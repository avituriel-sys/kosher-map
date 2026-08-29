-- Manual corrections that survive the weekly collector.
--
-- Every field the weekly collector writes to `business` gets
-- unconditionally overwritten on the next run (see
-- db/change_detection.py's _upsert_batch) - that's correct for scraped
-- data, but it means a manual fix written directly onto a `business`
-- row would silently revert the following Monday. This table holds
-- corrections separately, keyed by the same (source_id,
-- source_record_id) identity, so nothing here is ever touched by the
-- collector. Anything that reads business data for display (map
-- export today; the real app's API eventually) is responsible for
-- left-joining this table and preferring an override value when
-- present - see db/export_map_data.py for the current example.
--
-- Only columns that have actually come up as needing correction are
-- included (map position, and the fields most likely to be wrong from
-- our own mapping/parsing rather than the source's own data - category,
-- kosher type, supervision, name, phone). Extend as new cases come up
-- rather than overriding everything up front.
create table if not exists business_override (
    id                  bigserial primary key,
    source_id           text not null,
    source_record_id    text not null,
    lat                 double precision,
    lng                 double precision,
    name_clean          text,
    branch              text,
    category_canonical  text,
    kosher_type         text[],
    supervision_level   text,
    phone               text,
    note                text,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),

    constraint business_override_uk unique (source_id, source_record_id),
    constraint business_override_business_fk foreign key (source_id, source_record_id)
        references business (source_id, source_record_id) on delete cascade,

    -- Mirrors business's own CHECK constraints (001_initial_schema.sql)
    -- so an override can't introduce a value the rest of the system
    -- doesn't expect.
    constraint business_override_kosher_type_ck check (
        kosher_type is null or kosher_type <@ array['meat', 'dairy', 'parve']
    ),
    constraint business_override_supervision_level_ck check (
        supervision_level is null
        or supervision_level in ('regular', 'mehadrin', 'badatz', 'unknown')
    ),
    -- At least one field must actually be overridden - an all-null row
    -- would silently do nothing and just be confusing to find later.
    constraint business_override_not_empty_ck check (
        lat is not null or lng is not null or name_clean is not null
        or branch is not null or category_canonical is not null
        or kosher_type is not null or supervision_level is not null
        or phone is not null
    )
);

create index if not exists business_override_source_idx
    on business_override (source_id, source_record_id);
