"""Entrypoint that runs one collector and applies its output to the
database. This is what a daily cron job (spec section 12: "scheduled
once daily per source") would actually invoke.

    python -m db.run_collector netanya
    python -m db.run_collector tlv
    python -m db.run_collector tlv_revoked

Exits non-zero on any failure - fetch failure, validation failure, or a
database error - so a scheduler's own failure notification (spec section
10, not built yet) has something to trigger on. A failed run is recorded
in collection_run either way (with outcome="failed") except when the
collector itself raises before ever reaching the database, in which case
there's nothing to write a row for yet - that failure mode is exactly
what spec section 10's "run failed or did not run at all" check is for.
"""

from __future__ import annotations

import logging
import sys

from collectors.common.schema import CollectorError
from db.change_detection import apply_collection_run
from db.connection import get_connection

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _netanya():
    from collectors.netanya.collector import SOURCE_ID, collect

    return SOURCE_ID, collect(), True


def _tlv():
    from collectors.tlv.collector import SOURCE_ID, collect

    return SOURCE_ID, collect(), True


def _tlv_revoked():
    from collectors.tlv.revoked_collector import SOURCE_ID, collect_revoked

    return SOURCE_ID, collect_revoked(), False


SOURCES = {
    "netanya": _netanya,
    "tlv": _tlv,
    "tlv_revoked": _tlv_revoked,
}


def main(source_name: str) -> int:
    if source_name not in SOURCES:
        print(
            f"unknown source {source_name!r}; choose from {sorted(SOURCES)}",
            file=sys.stderr,
        )
        return 2

    try:
        source_id, records, is_full_census = SOURCES[source_name]()
    except CollectorError as exc:
        logger.error("%s: collector failed before producing any records: %s", source_name, exc)
        return 1

    conn = get_connection()
    try:
        result = apply_collection_run(conn, source_id, records, is_full_census)
    except Exception:
        logger.exception("%s: applying collection_run failed", source_name)
        return 1
    finally:
        conn.close()

    logger.info(
        "%s: found=%d new=%d absent=%d notable_changes=%d (collection_run id=%d)",
        source_name,
        result.records_found,
        result.records_new,
        result.records_absent,
        result.notable_changes,
        result.run_id,
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"usage: python -m db.run_collector <{'|'.join(SOURCES)}>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
