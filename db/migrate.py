"""Apply every .sql file in db/migrations/, in filename order, that
hasn't been applied yet. Tracks what's been applied in a
schema_migrations table so re-running this script is a no-op once
everything's up to date.

Deliberately simple - no down-migrations, no third-party migration
framework. Phase 1 has one schema; this just needs to be idempotent and
safe to run repeatedly.
"""

from __future__ import annotations

import sys
from pathlib import Path

from db.connection import get_connection

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def migrate() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                create table if not exists schema_migrations (
                    filename text primary key,
                    applied_at timestamptz not null default now()
                )
                """
            )
            cur.execute("select filename from schema_migrations")
            applied = {row[0] for row in cur.fetchall()}

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                print(f"skip  {path.name} (already applied)", file=sys.stderr)
                continue
            print(f"apply {path.name}", file=sys.stderr)
            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "insert into schema_migrations (filename) values (%s)",
                    (path.name,),
                )
            conn.commit()


if __name__ == "__main__":
    migrate()
    print("done", file=sys.stderr)
