-- Two related admin features (2026-09-23), built together because the
-- triage outcomes for a not_found/ambiguous sanity check need the first
-- one to exist:
--
-- 1. `hidden` on business_override - the only admin-facing way to make a
--    business stop showing on the public map. `business.status` is
--    collector-controlled (active/absent/revoked) and can't be overridden,
--    so "this place closed" or "this listing is a stale duplicate" had no
--    home. Kept on the override layer so a collector re-run never
--    un-hides it. `hidden` is a real boolean (false = no opinion), and
--    only `hidden = true` counts as override content.
--
-- 2. `resolution` / `resolution_note` on business_sanity_check - what an
--    admin decided about a not_found/ambiguous check, alongside the
--    existing acknowledged_at ("handled") marker. NULL resolution on an
--    acknowledged row is the legacy plain "reviewed" acknowledgement.
alter table business_override add column if not exists hidden boolean not null default false;
alter table business_override add column if not exists hidden_reason text;

alter table business_override drop constraint if exists business_override_hidden_reason_ck;
alter table business_override add constraint business_override_hidden_reason_ck check (
    hidden or hidden_reason is null
);

alter table business_override drop constraint if exists business_override_not_empty_ck;
alter table business_override add constraint business_override_not_empty_ck check (
    lat is not null or lng is not null or name_clean is not null
    or branch is not null or category_canonical is not null
    or kosher_type is not null or supervision_level is not null
    or phone is not null or address_raw is not null
    or cuisine_type is not null
    or hidden
);

alter table business_sanity_check add column if not exists resolution text;
alter table business_sanity_check add column if not exists resolution_note text;

alter table business_sanity_check drop constraint if exists business_sanity_check_resolution_ck;
alter table business_sanity_check add constraint business_sanity_check_resolution_ck check (
    resolution is null
    or (resolution in ('confirmed_exists', 'corrected', 'closed') and acknowledged_at is not null)
);
