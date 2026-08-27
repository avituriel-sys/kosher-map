-- 001 declared category_canonical not null, but collectors/common/schema.py
-- (REQUIRED_UNLESS_REVOKED) always allowed it to be absent for a
-- status='revoked' record - Tel Aviv's revoked-certification feed
-- genuinely never publishes a category. The Python-side validation was
-- right; the DDL just didn't match it. Loosening the column here rather
-- than forcing every revoked record to carry a fabricated category.
alter table business alter column category_canonical drop not null;
