-- Follow-up to 004_row_level_security.sql: that migration covered the
-- three tables the app actually owns (business, business_override,
-- collection_run) but missed schema_migrations, flagged by Supabase's
-- own security advisor on 2026-08-31 as publicly readable/writable via
-- the REST API.
--
-- schema_migrations: db/migrate.py's own bookkeeping table (which
-- migration files have been applied). Not sensitive data, but a
-- writable-by-anyone row here is a real problem regardless: someone
-- could insert a fake filename to trick a future migrate run into
-- believing a security migration already ran and skipping it.
--
-- spatial_ref_sys - also flagged, also public-schema, but NOT handled
-- here: it's a PostGIS system table owned by whatever role installed
-- the extension, and this connection's `postgres` role doesn't have
-- ALTER rights over it ("must be owner of table spatial_ref_sys" on
-- 2026-08-31, confirmed by testing). See the comment where this
-- migration is invoked (db/migrate.py callers / project notes) for
-- what to do about it - it needs either Supabase's dashboard SQL
-- editor (may run with a more privileged role) or moving the postgis
-- extension to a dedicated schema, and isn't a real risk in the
-- meantime since the table only holds public, non-sensitive spatial
-- reference system metadata.

alter table schema_migrations enable row level security;
create policy "admin_select_schema_migrations" on schema_migrations
    for select
    to authenticated
    using (auth.uid() = '843600e8-e8d0-48a8-9f95-fa77a86103f4');
