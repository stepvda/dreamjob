"""Reads for the free employer signals (rung 0b of the agency ladder).

Knowledge-base tables only - ``company`` and ``vacancy`` - so no statement here
takes a ``job_seeker_id`` (FR-344): whether a company is an agency is a fact
about the company, established once and read by every job seeker (FR-341).

``pipeline/employer_signals.py`` holds the statistics; the SQL lives here
(CR-408).
"""

from __future__ import annotations

from typing import Any

from dreamjob.db.connection import query_all, query_one

#: How many advertisements one employer is judged on.  The shingle comparison
#: uses at most twelve (``employer_signals.DESCRIPTION_LIMIT``); the shares are
#: taken over everything fetched, so the ceiling is generous but finite - GitLab
#: has 225 postings and the mean pairwise Jaccard is quadratic in the count.
POSTING_LIMIT = 60


def employer(company_id: str) -> dict[str, Any] | None:
    """Name, country and domain: the name is a signal, the other two pick the next rung."""
    return query_one(
        "SELECT id, name, normalised_name, country, domain, legal_id, sector_codes "
        "  FROM company WHERE id = ?",
        (company_id,),
    )


def employer_postings(company_id: str, limit: int = POSTING_LIMIT) -> list[dict[str, Any]]:
    """The employer's own advertisements, newest first.

    ``description IS NOT NULL`` is not filtered here: a posting without a
    description still counts towards the spread statistics and towards how much a
    verdict on this employer is worth, and the text signals skip it themselves.
    """
    return query_all(
        "SELECT id, title, description, language, seniority, function_family, contract_type, "
        "       source_adapter, source_url, location, posted_at "
        "  FROM vacancy WHERE company_id = ? "
        " ORDER BY COALESCE(posted_at, collected_at) DESC LIMIT ?",
        (company_id, int(limit)),
    )


def employers_with_postings(min_postings: int = 1, limit: int = 200) -> list[dict[str, Any]]:
    """Employers that have advertised, widest reach first - the work queue's input."""
    return query_all(
        "SELECT c.id, c.name, c.country, c.domain, COUNT(v.id) AS n_postings "
        "  FROM company c JOIN vacancy v ON v.company_id = c.id "
        " GROUP BY c.id HAVING COUNT(v.id) >= ? "
        " ORDER BY COUNT(v.id) DESC LIMIT ?",
        (int(min_postings), int(limit)),
    )
