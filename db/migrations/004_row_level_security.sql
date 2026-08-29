-- Locks down Supabase's auto-generated public REST API (PostgREST),
-- which exposes every table in the `public` schema by default. Until
-- this ran, business/business_override/collection_run had row level
-- security *disabled*, meaning anyone with this project's public API
-- key - which is meant to be safe to embed in client-side code, but
-- only once RLS is configured - had full read/write access to all
-- three tables via the REST API, independent of whether any UI existed
-- yet to make use of it.
--
-- This migration connects and runs as the `postgres` role (or table
-- owner), which bypasses RLS by default unless FORCE ROW LEVEL
-- SECURITY is also set (it isn't here) - so collectors/, db/migrate.py,
-- and scripts/export_map_data.py, all of which connect directly via
-- DATABASE_URL, are unaffected by anything below. This only changes
-- what's reachable through the public API using the `anon` (no login)
-- and `authenticated` (logged-in) PostgREST roles.
--
-- Single admin today (avituriel@gmail.com, auth.users.id below) rather
-- than a role/table-based scheme - simplest thing that's actually
-- correct for one person. If a second admin is ever needed, replace
-- the hardcoded UUID comparisons with a lookup against a small `admins`
-- table instead of hand-editing every policy.

alter table business enable row level security;
alter table business_override enable row level security;
alter table collection_run enable row level security;

-- business: readable by the admin (to look up what to correct);
-- nothing else granted - no anon access, no write access via the API.
-- The public map doesn't need this either, since it reads the static
-- web/businesses.json export, not the API.
create policy "admin_select_business" on business
    for select
    to authenticated
    using (auth.uid() = '843600e8-e8d0-48a8-9f95-fa77a86103f4');

-- business_override: full CRUD for the admin, nothing for anyone else.
-- This is the only table the admin UI ever writes to.
create policy "admin_select_business_override" on business_override
    for select
    to authenticated
    using (auth.uid() = '843600e8-e8d0-48a8-9f95-fa77a86103f4');

create policy "admin_insert_business_override" on business_override
    for insert
    to authenticated
    with check (auth.uid() = '843600e8-e8d0-48a8-9f95-fa77a86103f4');

create policy "admin_update_business_override" on business_override
    for update
    to authenticated
    using (auth.uid() = '843600e8-e8d0-48a8-9f95-fa77a86103f4')
    with check (auth.uid() = '843600e8-e8d0-48a8-9f95-fa77a86103f4');

create policy "admin_delete_business_override" on business_override
    for delete
    to authenticated
    using (auth.uid() = '843600e8-e8d0-48a8-9f95-fa77a86103f4');

-- collection_run: not needed by the admin UI today, but it was equally
-- exposed and equally unintentional - lock it down the same way rather
-- than leaving one table's gap open because nothing uses it yet.
create policy "admin_select_collection_run" on collection_run
    for select
    to authenticated
    using (auth.uid() = '843600e8-e8d0-48a8-9f95-fa77a86103f4');
