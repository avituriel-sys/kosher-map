-- 'not_found' and 'ambiguous' sanity checks skip business_anomaly
-- entirely (no field-by-field resolution makes sense for either - see
-- migration 006's comments), so they had no durable "an admin looked at
-- this and it's handled" signal of their own. The admin UI's first cut
-- improvised one by overloading `notes` (set on acknowledge), which
-- silently breaks the moment notes is used for its actual purpose -
-- recording what was tried/found during research, which a 'not_found'
-- or 'ambiguous' check very much wants to keep.
alter table business_sanity_check add column if not exists acknowledged_at timestamptz;
