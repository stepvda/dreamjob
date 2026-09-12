"""Topbar counters: companies, jobs, contacts, opportunities (FR-361, NFR-502).

The shell keeps these four numbers on screen and redraws them while collection
runs, so this module answers them in one statement.  The definitions are fixed
here and repeated in the endpoint docstring; a screen that invents its own
count drifts from the others, which is the whole reason the numbers were moved
behind one endpoint.

``companies``      the global knowledge base: every row of ``company``.  A
                   ``suppressed`` column did not exist when this was written
                   (``PRAGMA table_info(company)``); if one arrives, rows it
                   marks suppressed are excluded, decided per call through
                   :func:`dreamjob.db.repositories.knowledge.columns`.
``jobs``           advertised vacancies in the shared corpus:
                   ``COUNT(*) FROM vacancy``.
``contacts``       the seeker's reachable contacts, exactly
                   :func:`dreamjob.db.repositories.contacts.count_visible_contacts`:
                   the ``usable_contact`` view (NFR-302 objections and FR-304
                   ``invalid`` addresses already removed), visible under the
                   NFR-303 scope ``shareable = 1 OR owning_campaign_id IS NULL
                   OR owning campaign belongs to the seeker``, with a
                   non-empty address.
``opportunities``  the seeker's own opportunities, exactly
                   :func:`dreamjob.db.repositories.opportunities.count_opportunities`
                   with no filters.

The four scalar subqueries are one ``SELECT`` because the shell polls, not
because the statement is clever; the unfiltered SQL of the per-module count
functions is reproduced here verbatim where it fits, and
``tests/unit/test_overview.py`` pins each number equal to its owning function
so the two cannot quietly diverge.  Read-only: nothing here takes a write gate
(NFR-102).
"""

from __future__ import annotations

from dreamjob.db.connection import query_one
from dreamjob.db.repositories import knowledge as _knowledge


def _company_scope() -> str:
    """`` WHERE suppressed = 0`` when the column exists, otherwise nothing.

    As of this writing ``company`` has no ``suppressed`` column, so this is the
    empty string and the count is the whole table.  The check goes through
    ``knowledge.columns`` because it caches the ``PRAGMA``; the schema is
    append-only, so the answer does not change within a process.

    Caveat, the same one ``knowledge.columns`` carries: a migration that adds
    the column while the process is running is not seen until it restarts.
    """
    return " WHERE suppressed = 0" if "suppressed" in _knowledge.columns("company") else ""


def counters(job_seeker_id: str) -> dict[str, int]:
    """The four topbar numbers for one seeker, in a single round trip."""
    row = query_one(
        "SELECT"
        f" (SELECT COUNT(*) FROM company{_company_scope()}) AS companies,"
        "       (SELECT COUNT(*) FROM vacancy) AS jobs,"
        "       (SELECT COUNT(*) FROM usable_contact c"
        "         WHERE c.email IS NOT NULL AND c.email <> ''"
        "           AND (c.shareable = 1 OR c.owning_campaign_id IS NULL"
        "                OR c.owning_campaign_id IN"
        "                   (SELECT id FROM campaign WHERE job_seeker_id = ?))) AS contacts,"
        "       (SELECT COUNT(*) FROM opportunity o"
        "          LEFT JOIN company c ON c.id = o.company_id"
        "         WHERE o.job_seeker_id = ?) AS opportunities",
        (job_seeker_id, job_seeker_id),
    ) or {}
    return {
        "companies": int(row.get("companies") or 0),
        "jobs": int(row.get("jobs") or 0),
        "contacts": int(row.get("contacts") or 0),
        "opportunities": int(row.get("opportunities") or 0),
    }
