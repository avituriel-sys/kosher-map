-- Sanity-check feature: cross-reference eateries against outside
-- sources (Google Maps, the business's own site, etc.), record what's
-- found, and let an admin decide what to do about any discrepancy.
--
-- business_override gains address_raw: the original design only
-- covered position (lat/lng) and a handful of fields most likely wrong
-- from our own mapping/parsing, not the source's own address text. The
-- sanity check needs to be able to correct the address itself, not
-- just where the pin sits, so this is a necessary extension, not
-- scope creep - see the "not empty" CHECK constraint update below too.
alter table business_override add column if not exists address_raw text;
alter table business_override drop constraint if exists business_override_not_empty_ck;
alter table business_override add constraint business_override_not_empty_ck check (
    lat is not null or lng is not null or name_clean is not null
    or branch is not null or category_canonical is not null
    or kosher_type is not null or supervision_level is not null
    or phone is not null or address_raw is not null
);

-- One row per check *event* - this is deliberately also the "when was
-- this business last verified" ledger (spec discussion: always record
-- the matched link, even when nothing was wrong), so a business with a
-- 'matched' check and no linked business_anomaly rows just means
-- "verified clean", no separate bookkeeping needed for that case.
-- 'not_found' means the business couldn't be located online at all -
-- itself a signal worth a human's attention (could mean closed), but
-- deliberately NOT run through the same field-by-field override flow
-- below: there's no sensible business_override field for "does this
-- business still exist", and status changes belong to the weekly
-- collector's own change-detection logic, not this system.
create table if not exists business_sanity_check (
    id                bigserial primary key,
    source_id         text not null,
    source_record_id  text not null,
    checked_at        timestamptz not null default now(),
    match_status      text not null,
    external_url      text,
    notes             text,

    constraint business_sanity_check_business_fk foreign key (source_id, source_record_id)
        references business (source_id, source_record_id) on delete cascade,
    constraint business_sanity_check_match_status_ck check (
        match_status in ('matched', 'not_found', 'ambiguous')
    ),
    -- A 'matched' or 'ambiguous' check should have a link (that's the
    -- whole point of recording it); 'not_found' shouldn't, since there
    -- was nothing to link to.
    constraint business_sanity_check_url_consistency_ck check (
        (match_status = 'not_found' and external_url is null)
        or (match_status in ('matched', 'ambiguous') and external_url is not null)
    )
);
create index if not exists business_sanity_check_source_idx
    on business_sanity_check (source_id, source_record_id);
create index if not exists business_sanity_check_checked_at_idx
    on business_sanity_check (checked_at);

-- One row per actual field discrepancy found during a check. field_name
-- is deliberately limited to columns business_override can actually
-- hold (name/address/phone) - kashrut-specific fields (kosher_type,
-- supervision_level, certifying_authority) aren't things a generic web
-- search can verify, so they're out of scope for this feature by
-- construction, not by convention.
create table if not exists business_anomaly (
    id                bigint generated always as identity primary key,
    sanity_check_id   bigint not null references business_sanity_check (id) on delete cascade,
    source_id         text not null,
    source_record_id  text not null,
    field_name        text not null,
    our_value         text,
    external_value    text,
    status            text not null default 'pending',
    resolution_note   text,
    resolved_at       timestamptz,
    created_at        timestamptz not null default now(),

    constraint business_anomaly_business_fk foreign key (source_id, source_record_id)
        references business (source_id, source_record_id) on delete cascade,
    constraint business_anomaly_field_name_ck check (
        field_name in ('name', 'address', 'phone')
    ),
    constraint business_anomaly_status_ck check (
        status in ('pending', 'accepted_external', 'kept_ours', 'manual_fix')
    ),
    -- Resolving is what sets resolved_at - the two must move together,
    -- or "pending" items could silently carry a stale timestamp and
    -- vice versa.
    constraint business_anomaly_resolved_at_consistency_ck check (
        (status = 'pending' and resolved_at is null)
        or (status != 'pending' and resolved_at is not null)
    )
);
create index if not exists business_anomaly_pending_idx
    on business_anomaly (created_at) where status = 'pending';
create index if not exists business_anomaly_source_idx
    on business_anomaly (source_id, source_record_id);

-- Same single-admin RLS pattern as business_override
-- (004_row_level_security.sql) - full CRUD for the admin, nothing for
-- anyone else, from the moment these tables exist rather than as a
-- follow-up fix (005_rls_remaining_tables.sql was exactly that kind of
-- follow-up, for tables that shouldn't have needed one).
alter table business_sanity_check enable row level security;
create policy "admin_all_business_sanity_check" on business_sanity_check
    for all
    to authenticated
    using (auth.uid() = '843600e8-e8d0-48a8-9f95-fa77a86103f4')
    with check (auth.uid() = '843600e8-e8d0-48a8-9f95-fa77a86103f4');

alter table business_anomaly enable row level security;
create policy "admin_all_business_anomaly" on business_anomaly
    for all
    to authenticated
    using (auth.uid() = '843600e8-e8d0-48a8-9f95-fa77a86103f4')
    with check (auth.uid() = '843600e8-e8d0-48a8-9f95-fa77a86103f4');
