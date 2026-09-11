"""Mine the collected postings into market compensation observations (FR-264).

The postings the pipeline already holds are a compensation survey nobody read.
``compensation_observation`` sat empty while 5,536 vacancies in a single
campaign carried a stated range, so the estimator fell back to the built-in
prior for every opportunity.  This distils the corpus into one observation per
(function family, seniority, country), at ``posted_vacancies`` weight - the
second-highest in the blend - and rebuilds it idempotently.

It is a *market* reading: the rows carry no company, so a posted range is the
market for that role, not a claim about one employer.  Only EUR figures are
mined, because the blend discards other currencies rather than converting them,
and a segment needs a minimum sample before it is worth publishing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

SOURCE = "posted_vacancies"
DEFAULT_MIN_SAMPLE = 5


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _midpoint(minimum: float | None, maximum: float | None) -> float | None:
    if minimum is not None and maximum is not None:
        return (minimum + maximum) / 2
    return minimum if minimum is not None else maximum


def mine_posted_ranges(*, min_sample: int = DEFAULT_MIN_SAMPLE, apply: bool = True) -> dict[str, Any]:
    """Rebuild the ``posted_vacancies`` observations from the vacancy corpus."""
    from dreamjob.db.connection import execute, query_all  # noqa: PLC0415
    from dreamjob.db.repositories import opportunities as repo  # noqa: PLC0415

    rows = query_all(
        "SELECT function_family, seniority, country, salary_currency, "
        "       salary_min, salary_max "
        "FROM vacancy "
        "WHERE (salary_min IS NOT NULL OR salary_max IS NOT NULL) "
        "  AND function_family IS NOT NULL AND TRIM(function_family) <> '' "
        "  AND UPPER(IFNULL(salary_currency, 'EUR')) = 'EUR'"
    )
    segments: dict[tuple[str, str, str], list[tuple[float, float | None, float | None]]] = {}
    for row in rows:
        family = str(row["function_family"]).strip()
        seniority = str(row["seniority"] or "").strip()
        if not seniority:
            # The blend looks observations up by family *and* seniority, so a
            # row without one is unreachable; it is not worth publishing.
            continue
        key = (family, seniority, str(row["country"] or "").upper())
        segments.setdefault(key, []).append(
            (row["salary_min"], row["salary_max"], None)
        )

    observations: list[dict[str, Any]] = []
    as_of = datetime.now(UTC).date().isoformat()
    for (family, seniority, country), entries in segments.items():
        mids = [
            mid
            for minimum, maximum, _ in entries
            for mid in [_midpoint(minimum, maximum)]
            if mid is not None
        ]
        if len(mids) < min_sample:
            continue
        lows = [minimum for minimum, _, _ in entries if minimum is not None]
        highs = [maximum for _, maximum, _ in entries if maximum is not None]
        observations.append(
            {
                "source": SOURCE,
                "source_name": "Collected job postings",
                "role_title": f"{family} ({seniority})",
                "normalised_title": family,
                "function_family": family,
                "seniority": seniority,
                "country": country or None,
                "currency": "EUR",
                "period": "annual",
                "amount_min": min(lows) if lows else None,
                "amount_p25": _percentile(mids, 0.25),
                "amount_median": _percentile(mids, 0.50),
                "amount_p75": _percentile(mids, 0.75),
                "amount_max": max(highs) if highs else None,
                "sample_size": len(mids),
                "as_of": as_of,
                "access_method": "corpus",
                # A corpus range is evidence, but a posting is a wish as much as
                # a fact; confidence rises with the sample and is capped below
                # a company's own posted range.
                "confidence": round(min(0.75, 0.4 + 0.05 * len(mids)), 3),
            }
        )

    if apply:
        execute("DELETE FROM compensation_observation WHERE source = ?", (SOURCE,))
        for observation in observations:
            repo.record_observation(observation)
    return {
        "segments": len(observations),
        "vacancies_read": len(rows),
        "min_sample": min_sample,
        "applied": apply,
    }
