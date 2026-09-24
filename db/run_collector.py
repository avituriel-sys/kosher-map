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
import os
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


def _raanana():
    from collectors.raanana.collector import SOURCE_ID, collect

    return SOURCE_ID, collect(), True


def _givat_shmuel():
    from collectors.drts.collector import collect
    from collectors.drts.councils import GIVAT_SHMUEL

    return GIVAT_SHMUEL.source_id, collect(GIVAT_SHMUEL), True


def _petah_tikva():
    from collectors.drts.collector import collect
    from collectors.drts.councils import PETAH_TIKVA

    return PETAH_TIKVA.source_id, collect(PETAH_TIKVA), True


# 2026-09-23 pilot of the national m-datit portal: Tel Mond, Beit Shean,
# Kfar Saba, Givat Shmuel (a "list B" council the survey expected to be
# missing), and the three big zero-certificate councils (Petah Tikva,
# Rishon LeZion, Ashdod). Deliberately NOT in the weekly workflow yet - run
# by hand (python -m db.run_collector mdatit_pilot). The first run geocodes
# ~1,800 addresses at 1/second, so it takes roughly half an hour.
MDATIT_PILOT_AUTHORITY_IDS = [80, 72, 16, 56, 6, 5, 7]


def _mdatit_pilot():
    from collectors.mdatit.collector import collect

    records = collect(MDATIT_PILOT_AUTHORITY_IDS)
    by_source: dict[str, list[dict]] = {}
    for record in records:
        by_source.setdefault(record["source_id"], []).append(record)
    return [(source_id, rows, True) for source_id, rows in sorted(by_source.items())]


SOURCES = {
    "netanya": _netanya,
    "tlv": _tlv,
    "tlv_revoked": _tlv_revoked,
    "raanana": _raanana,
    "givat_shmuel": _givat_shmuel,
    "petah_tikva": _petah_tikva,
    "mdatit_pilot": _mdatit_pilot,
}


def main(source_name: str) -> int:
    if source_name not in SOURCES:
        print(
            f"unknown source {source_name!r}; choose from {sorted(SOURCES)}",
            file=sys.stderr,
        )
        return 2

    try:
        collected = SOURCES[source_name]()
    except CollectorError as exc:
        logger.error("%s: collector failed before producing any records: %s", source_name, exc)
        return 1

    # Most sources are one (source_id, records, full_census) triple; the
    # national portal yields one triple per council, each applied on its own
    # so "absent" detection never crosses councils.
    runs = collected if isinstance(collected, list) else [collected]

    exit_code = 0
    for source_id, records, is_full_census in runs:
        conn = get_connection()
        try:
            result = apply_collection_run(
                conn, source_id, records, is_full_census,
                allow_large_drop=os.environ.get("ALLOW_LARGE_DROP") == "1",
            )
        except Exception:
            logger.exception("%s: applying collection_run for %s failed", source_name, source_id)
            exit_code = 1
            continue
        finally:
            conn.close()

        logger.info(
            "%s: found=%d new=%d absent=%d notable_changes=%d (collection_run id=%d)",
            source_id,
            result.records_found,
            result.records_new,
            result.records_absent,
            result.notable_changes,
            result.run_id,
        )
    return exit_code


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"usage: python -m db.run_collector <{'|'.join(SOURCES)}>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
