"""The employer-kind verdict as a shared fact, and the private correction beside it.

All SQL for ``company_employer_kind`` and ``employer_kind_correction`` lives
here (CR-408).  Four jobs:

* **the verdict** - read it, and replace it when a rung has a better answer.
  A row is never edited in place by a later rung: it is replaced and the old
  one is kept in ``audit_event`` with the diff, so "why did this change"
  is answerable (NFR-702, Agency_Research_Design.md section 6).
* **the queue** - which companies are worth another rung's request, ordered by
  how much they matter, which is vacancy count.  Half the corpus's agency rows
  sit at eleven employers; resolving those eleven first is most of the value
  for a few minutes of requests.
* **the coverage summary** - by rung and, for the non-answers, by reason, so an
  operator reading "30% unverified" can tell that it means "mostly no domain"
  rather than "the classifier is unsure" (research note section 7.5).
* **the joins the badge needs** - :data:`BADGE_COLUMNS` and :data:`BADGE_JOIN`
  are spliced into the ranked-list and Apply Browser queries so a badge on 200
  rows costs no request of its own (NFR-502, FR-282).

The verdict carries no job-seeker id: a company does not stop being an agency
between job seekers, so it is knowledge base (FR-341, FR-344).  A correction
does carry one, because it is that seeker's override until it is promoted -
and promotion drops the link.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from dreamjob.db.connection import (
    from_json,
    insert_row,
    new_id,
    query_all,
    query_one,
    to_json,
    utcnow,
    write_tx,
)
from dreamjob.pipeline.employer_kind import (
    CONSENSUS_THRESHOLD,
    DECIDED_CONFIDENCE,
    REASON_ALIASES,
    RUNG_AUTHORITY,
    Evidence,
    Kind,
    Reason,
    Rung,
    Verdict,
    coerce_reason,
    correction_conflicts,
    effective_verdict,
    expires_at_for,
    retry_after_for,
    supersedes,
)

log = logging.getLogger(__name__)

TABLE = "company_employer_kind"
CORRECTIONS = "employer_kind_correction"

_JSON_COLUMNS = ("evidence", "identity_evidence", "audiences", "anomalies")

#: The columns the ranked list and the Apply Browser add to their own SELECT.
#: Everything a badge renders - the state, the role that decides what the
#: product may do, the confidence, the one-sentence summary and the reason a
#: non-answer is a non-answer - travels with the row (NFR-502).
BADGE_COLUMNS = """
           ek.kind          AS employer_kind,
           ek.employer_role AS employer_role,
           ek.confidence    AS employer_kind_confidence,
           ek.service_model AS employer_service_model,
           ek.tier          AS employer_kind_tier,
           ek.reason        AS employer_kind_reason,
           ek.summary       AS employer_kind_summary
"""

#: ``company_id`` is the verdict table's primary key, so this is a rowid lookup
#: per row already on screen.
BADGE_JOIN = "LEFT JOIN company_employer_kind ek ON ek.company_id = {company_column}"


def badge_correction_column(
    *, company_column: str = "o.company_id", seeker_column: str = "o.job_seeker_id"
) -> str:
    """A correlated look-up of the correction that applies to *this* reader.

    A LEFT JOIN would multiply rows for a company that has both a private and a
    shared correction; this returns one value.  ``ORDER BY job_seeker_id IS
    NULL`` puts the seeker's own correction first, which is the precedence the
    design asks for: their correction applies to them immediately, the promoted
    one applies to everybody else.
    """
    return (
        "(SELECT ekc.kind FROM employer_kind_correction ekc "
        f"WHERE ekc.company_id = {company_column} "
        f"AND (ekc.job_seeker_id = {seeker_column} OR ekc.job_seeker_id IS NULL) "
        "ORDER BY ekc.job_seeker_id IS NULL LIMIT 1) AS employer_kind_correction"
    )


def decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Parse the JSON columns; the caller never sees a string that is a list."""
    if row is None:
        return None
    out = dict(row)
    for column in _JSON_COLUMNS:
        if column in out:
            default: Any = [] if column in ("evidence", "anomalies") else None
            out[column] = from_json(out[column], default)
    return out


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get(company_id: str) -> dict[str, Any] | None:
    return decode(query_one(f"SELECT * FROM {TABLE} WHERE company_id = ?", (company_id,)))


def get_verdict(company_id: str) -> Verdict | None:
    row = get(company_id)
    return Verdict.from_row(row) if row else None


def get_many(company_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Verdicts for a batch of companies, in one statement."""
    if not company_ids:
        return {}
    unique = list(dict.fromkeys(company_ids))
    out: dict[str, dict[str, Any]] = {}
    # SQLite's parameter limit is 999 on old builds; batch rather than assume.
    for start in range(0, len(unique), 500):
        chunk = unique[start:start + 500]
        marks = ", ".join("?" for _ in chunk)
        for row in query_all(f"SELECT * FROM {TABLE} WHERE company_id IN ({marks})", tuple(chunk)):
            decoded = decode(row)
            if decoded:
                out[str(decoded["company_id"])] = decoded
    return out


def for_seeker(company_id: str, job_seeker_id: str | None = None) -> Verdict | None:
    """The verdict as this job seeker sees it, with their correction applied.

    Their own correction wins for them the moment they make it - it is their
    list - and a promoted one applies where they have not corrected it
    themselves.  Neither silently overturns a register: that case comes back
    with ``conflicts_with`` set so both sentences can be shown.
    """
    verdict = get_verdict(company_id)
    correction = correction_for(company_id, job_seeker_id)
    if correction is None:
        return verdict
    return effective_verdict(verdict, correction)


def badges(company_ids: list[str], job_seeker_id: str | None = None) -> dict[str, dict[str, Any]]:
    """What a badge needs for a set of companies, for surfaces that own no SQL."""
    rows = get_many(company_ids)
    if not rows:
        return {}
    corrections = corrections_for_companies(list(rows), job_seeker_id)
    out: dict[str, dict[str, Any]] = {}
    for company_id, row in rows.items():
        verdict = Verdict.from_row(row)
        effective = effective_verdict(verdict, corrections.get(company_id)) or verdict
        out[company_id] = {
            "kind": str(effective.kind),
            "employer_role": str(effective.employer_role),
            "confidence": effective.confidence,
            "service_model": str(effective.service_model),
            "tier": str(effective.tier) if effective.tier else None,
            "reason": str(effective.reason) if effective.reason else None,
            "summary": effective.summary,
            "corrected": effective.rung is Rung.MANUAL and verdict.rung is not Rung.MANUAL,
            "conflicts_with": str(effective.conflicts_with) if effective.conflicts_with else None,
            "established_at": row.get("established_at"),
        }
    return out


def companies_by_role(roles: tuple[str, ...] = ("agency", "board")) -> list[str]:
    """The company ids the profiling, financial and speculative gates exclude."""
    marks = ", ".join("?" for _ in roles)
    rows = query_all(
        f"SELECT company_id FROM {TABLE} WHERE employer_role IN ({marks})", tuple(roles)
    )
    return [str(r["company_id"]) for r in rows]


def companies_needing_resolution(
    *,
    for_rung: Rung | str | None = None,
    limit: int = 200,
    min_vacancies: int = 1,
    country: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Companies worth another rung's request, the ones that matter most first.

    "Matter most" is vacancy count: 11 names hold 515 of the corpus's
    vacancies, so the queue is ordered by how many postings a wrong answer
    would mislabel rather than by when the company was seen.

    The predicate is the SQL half of
    :func:`dreamjob.pipeline.employer_kind.needs_resolution` and has to keep
    saying the same thing: no verdict at all; a verdict that has gone stale; a
    decided verdict still under the 0.85 bar that a more authoritative rung
    could improve; a non-answer whose retry has come round; and the
    event-driven case - a ``no_domain`` non-answer for a company that now has a
    domain.

    ``for_rung`` is the rung the caller is about to walk, and a company whose
    non-answer already came from that rung or a dearer one is not in the list -
    which is what stops a monthly pass from re-spending 32 minutes of register
    requests on names the register is silent about.  With no ``for_rung`` the
    ceiling is the human rung, so the list is the operator's queue: everything
    a machine could not settle, including the refusals that say so.  A company
    with no vacancy is never here whatever ``min_vacancies`` says - the tag
    exists to label postings, and an employer with nothing on the board costs a
    request for a badge nobody will see.
    """
    moment = _stamp(now)
    ceiling = RUNG_AUTHORITY[Rung(for_rung)] if for_rung else max(RUNG_AUTHORITY.values())
    # The authorities are a closed, code-owned vocabulary, so this CASE is
    # written from the mapping rather than from anything a caller supplied.
    authority_case = "CASE k.rung " + " ".join(
        f"WHEN '{rung}' THEN {level}" for rung, level in RUNG_AUTHORITY.items()
    ) + " ELSE 0 END"

    params: list[Any] = []
    where_country = ""
    if country:
        where_country = "AND c.country = ?"
        params.append(country)

    sql = f"""
        SELECT c.id AS company_id, c.name, c.domain, c.country,
               COUNT(v.id) AS vacancy_count,
               k.kind, k.rung, k.confidence, k.reason, k.tier,
               k.retry_after, k.expires_at, k.attempts, k.established_at
        FROM company c
        JOIN vacancy v ON v.company_id = c.id
        LEFT JOIN {TABLE} k ON k.company_id = c.id
        WHERE 1 = 1 {where_country}
          AND (
                k.company_id IS NULL
             OR (k.expires_at IS NOT NULL AND k.expires_at <= ?)
             OR (k.kind = 'cannot_tell' AND (
                    (k.retry_after IS NOT NULL AND k.retry_after <= ?)
                 OR (k.reason = ? AND c.domain IS NOT NULL AND TRIM(c.domain) != '')
                 OR {authority_case} < ?))
             OR (k.kind != 'cannot_tell' AND k.confidence < ? AND {authority_case} < ?)
          )
        GROUP BY c.id
        HAVING COUNT(v.id) >= ?
        ORDER BY vacancy_count DESC, c.name COLLATE NOCASE ASC
        LIMIT ?
    """
    params.extend(
        [
            moment,
            moment,
            str(Reason.NO_DOMAIN),
            ceiling,
            DECIDED_CONFIDENCE,
            ceiling,
            int(min_vacancies),
            int(limit),
        ]
    )
    return query_all(sql, tuple(params))


def flagged_for_review(limit: int = 200) -> list[dict[str, Any]]:
    """Rows with anomalies, whatever the verdict says (NFR-205).

    A page that addressed the classifier, asked it to change its task or
    claimed to be an instruction is recorded here as data about the page.  The
    verdict stands; the row is looked at.
    """
    rows = query_all(
        f"SELECT * FROM {TABLE} WHERE anomalies IS NOT NULL AND anomalies NOT IN ('', '[]') "
        "ORDER BY established_at DESC LIMIT ?",
        (int(limit),),
    )
    return [r for r in (decode(row) for row in rows) if r]


def coverage() -> dict[str, Any]:
    """Verdicts by kind, role and rung, and non-answers by reason (section 7.5)."""

    def _counts(sql: str, params: tuple = ()) -> dict[str, int]:
        return {str(r["value"]): int(r["n"]) for r in query_all(sql, params) if r["value"]}

    def _fold_aliases(counts: dict[str, int]) -> dict[str, int]:
        # One state must not read as two buckets because two slices spell it
        # differently (``js_rendered_or_empty``).
        folded: dict[str, int] = {}
        for reason, count in counts.items():
            folded[REASON_ALIASES.get(reason, reason)] = (
                folded.get(REASON_ALIASES.get(reason, reason), 0) + count
            )
        return folded

    employers = query_one(
        "SELECT COUNT(DISTINCT c.id) AS n FROM company c JOIN vacancy v ON v.company_id = c.id"
    )
    total = query_one(f"SELECT COUNT(*) AS n FROM {TABLE}")
    vacancies = query_all(
        f"SELECT k.employer_role AS value, COUNT(v.id) AS n "
        f"FROM {TABLE} k JOIN vacancy v ON v.company_id = k.company_id GROUP BY k.employer_role"
    )
    corrections = query_all(
        f"SELECT scope AS value, COUNT(*) AS n FROM {CORRECTIONS} GROUP BY scope"
    )
    return {
        "employers_with_vacancies": int(employers["n"]) if employers else 0,
        "verdicts": int(total["n"]) if total else 0,
        "by_kind": _counts(f"SELECT kind AS value, COUNT(*) AS n FROM {TABLE} GROUP BY kind"),
        "by_role": _counts(
            f"SELECT employer_role AS value, COUNT(*) AS n FROM {TABLE} GROUP BY employer_role"
        ),
        "by_rung": _counts(f"SELECT rung AS value, COUNT(*) AS n FROM {TABLE} GROUP BY rung"),
        "by_tier": _counts(f"SELECT tier AS value, COUNT(*) AS n FROM {TABLE} GROUP BY tier"),
        "cannot_tell_by_reason": _fold_aliases(
            _counts(
                f"SELECT reason AS value, COUNT(*) AS n FROM {TABLE} "
                "WHERE kind = 'cannot_tell' GROUP BY reason"
            )
        ),
        "vacancies_by_role": {str(r["value"]): int(r["n"]) for r in vacancies if r["value"]},
        "corrections": {str(r["value"]): int(r["n"]) for r in corrections if r["value"]},
        "flagged": len(flagged_for_review(limit=1000)),
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _stamp(now: datetime | None) -> str:
    """The one timestamp format every TEXT date column holds, so ``<=`` sorts."""
    if now is None:
        return utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(UTC).isoformat(timespec="seconds")


def record_verdict(
    company_id: str,
    verdict: Verdict,
    *,
    now: datetime | None = None,
    force: bool = False,
    actor: str = "system",
) -> dict[str, Any]:
    """Store a verdict, keeping the more authoritative one when they disagree.

    Returns ``{"written": bool, "kept": str | None}``.  A refusal is not an
    error: it is the ladder working - a website read does not overwrite a
    register that lists a regulated temporary-employment activity, and the
    caller is told which rung held the row so it can stop spending requests on
    that company.
    """
    existing_row = get(company_id)
    existing = Verdict.from_row(existing_row) if existing_row else None
    if not force and not supersedes(verdict, existing):
        log.debug(
            "employer_kind: keeping the %s verdict for %s over the %s one",
            existing.rung if existing else "?", company_id, verdict.rung,
        )
        return {"written": False, "kept": str(existing.rung) if existing else None}

    moment = _stamp(now)
    same_answer = (
        existing is not None
        and existing.kind == verdict.kind
        and existing.employer_role == verdict.employer_role
        and existing.rung == verdict.rung
        and existing.reason == verdict.reason
    )
    attempts = int(existing_row["attempts"]) + 1 if existing_row else 1
    row = verdict.to_row(
        # A verdict that has not changed keeps the date it was first reached;
        # "established" is when this became known, not when it was last checked.
        established_at=str(existing_row["established_at"]) if same_answer else moment,
        expires_at=expires_at_for(verdict.rung, now=now),
        retry_after=retry_after_for(verdict.reason, attempts=attempts, now=now),
    )
    row["company_id"] = company_id
    row["attempts"] = attempts
    row["refreshed_at"] = moment
    payload = {
        key: (to_json(value) if key in _JSON_COLUMNS else value) for key, value in row.items()
    }

    columns = ", ".join(payload)
    marks = ", ".join(f":{k}" for k in payload)
    updates = ", ".join(f"{k}=excluded.{k}" for k in payload if k != "company_id")
    with write_tx() as conn:
        conn.execute(
            f"INSERT INTO {TABLE} ({columns}) VALUES ({marks}) "
            f"ON CONFLICT(company_id) DO UPDATE SET {updates}",
            payload,
        )

    if existing is not None and not same_answer:
        # NFR-702: the previous verdict, kept whole, so a change is explainable.
        _audit(
            "employer_kind.replaced",
            company_id,
            actor=actor,
            detail={
                "from": {
                    "kind": str(existing.kind),
                    "rung": str(existing.rung),
                    "confidence": existing.confidence,
                    "reason": str(existing.reason) if existing.reason else None,
                    "established_at": existing_row.get("established_at"),
                    "evidence": [e.to_dict() for e in existing.evidence],
                },
                "to": {
                    "kind": str(verdict.kind),
                    "rung": str(verdict.rung),
                    "confidence": verdict.confidence,
                    "reason": str(verdict.reason) if verdict.reason else None,
                },
            },
        )
    return {"written": True, "kept": None}


def mark_refreshed(company_id: str, *, now: datetime | None = None) -> bool:
    """Renew a verdict whose evidence has not changed (no LLM call, no re-read).

    A refresh whose page ``content_hash`` is unchanged renews the row without
    asking the model anything (research note section 5).
    """
    row = get(company_id)
    if row is None:
        return False
    moment = _stamp(now)
    with write_tx() as conn:
        conn.execute(
            f"UPDATE {TABLE} SET refreshed_at = ?, expires_at = ? WHERE company_id = ?",
            (moment, expires_at_for(Rung(str(row["rung"])), now=now), company_id),
        )
    return True


def record_attempt(
    company_id: str, reason: Reason | str, *, now: datetime | None = None
) -> bool:
    """Push a non-answer's retry out by one attempt without changing the verdict.

    ``unreachable`` is 3 days and then 30: a site that has not answered twice
    is not going to answer on the third day either.
    """
    row = get(company_id)
    if row is None or str(row["kind"]) != Kind.CANNOT_TELL:
        return False
    attempts = int(row["attempts"]) + 1
    with write_tx() as conn:
        conn.execute(
            f"UPDATE {TABLE} SET attempts = ?, reason = ?, retry_after = ?, refreshed_at = ? "
            "WHERE company_id = ?",
            (
                attempts,
                str(coerce_reason(reason)),
                retry_after_for(coerce_reason(reason), attempts=attempts, now=now),
                _stamp(now),
                company_id,
            ),
        )
    return True


def delete_verdict(company_id: str) -> bool:
    with write_tx() as conn:
        cur = conn.execute(f"DELETE FROM {TABLE} WHERE company_id = ?", (company_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Corrections: private by default, shared by promotion
# ---------------------------------------------------------------------------


def correction_for(company_id: str, job_seeker_id: str | None) -> dict[str, Any] | None:
    """The correction that applies to this reader - their own first, then shared."""
    if job_seeker_id:
        return query_one(
            f"SELECT * FROM {CORRECTIONS} WHERE company_id = ? "
            "AND (job_seeker_id = ? OR job_seeker_id IS NULL) "
            "ORDER BY job_seeker_id IS NULL LIMIT 1",
            (company_id, job_seeker_id),
        )
    return query_one(
        f"SELECT * FROM {CORRECTIONS} WHERE company_id = ? AND job_seeker_id IS NULL",
        (company_id,),
    )


def corrections_for_companies(
    company_ids: list[str], job_seeker_id: str | None = None
) -> dict[str, dict[str, Any]]:
    if not company_ids:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(company_ids), 500):
        chunk = company_ids[start:start + 500]
        marks = ", ".join("?" for _ in chunk)
        params: list[Any] = list(chunk)
        clause = "job_seeker_id IS NULL"
        if job_seeker_id:
            clause = "(job_seeker_id = ? OR job_seeker_id IS NULL)"
            params.append(job_seeker_id)
        rows = query_all(
            f"SELECT * FROM {CORRECTIONS} WHERE company_id IN ({marks}) AND {clause} "
            "ORDER BY job_seeker_id IS NULL",
            tuple(params),
        )
        for row in rows:
            out.setdefault(str(row["company_id"]), dict(row))
    return out


def corrections_for_company(company_id: str) -> list[dict[str, Any]]:
    return query_all(
        f"SELECT * FROM {CORRECTIONS} WHERE company_id = ? ORDER BY created_at DESC",
        (company_id,),
    )


def record_correction(
    company_id: str,
    job_seeker_id: str,
    kind: Kind | str,
    note: str,
    *,
    evidence_url: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One job seeker's "this is wrong", applied to their view immediately.

    The note is required, as a rejection reason is (FR-285): a correction with
    no stated because cannot be reviewed, cannot be promoted, and is not
    evidence.  Nothing shared changes here - see :func:`promote_correction` for
    the two ways that happens, and why they are the only two.
    """
    kind = Kind(str(kind))
    if kind is Kind.CANNOT_TELL:
        raise ValueError("a correction asserts agency or employer; it cannot assert cannot_tell")
    note = (note or "").strip()
    if not note:
        raise ValueError("a correction needs a note saying why (FR-285)")
    if not job_seeker_id:
        raise ValueError("a private correction belongs to a job seeker (FR-344)")

    moment = _stamp(now)
    existing = query_one(
        f"SELECT id FROM {CORRECTIONS} WHERE company_id = ? AND job_seeker_id = ?",
        (company_id, job_seeker_id),
    )
    if existing:
        with write_tx() as conn:
            conn.execute(
                f"UPDATE {CORRECTIONS} SET kind = ?, note = ?, evidence_url = ?, created_at = ? "
                "WHERE id = ?",
                (str(kind), note, evidence_url, moment, existing["id"]),
            )
        correction_id = str(existing["id"])
    else:
        correction_id = insert_row(
            CORRECTIONS,
            {
                "id": new_id(),
                "company_id": company_id,
                "job_seeker_id": job_seeker_id,
                "kind": str(kind),
                "note": note,
                "evidence_url": evidence_url,
                "scope": "private",
                "created_at": moment,
            },
        )
    _audit(
        "employer_kind.corrected",
        company_id,
        job_seeker_id=job_seeker_id,
        actor=job_seeker_id,
        detail={"kind": str(kind), "note": note, "evidence_url": evidence_url},
    )

    promoted = _promote_on_consensus(company_id, kind, now=now)
    return {
        "id": correction_id,
        "scope": "private",
        "promoted": promoted,
        "conflicts_with_registry": correction_conflicts(get_verdict(company_id), kind),
    }


def withdraw_correction(
    company_id: str, job_seeker_id: str | None, *, actor: str = "system"
) -> bool:
    """Withdraw a correction.  The row goes; the withdrawal is an audit event."""
    row = query_one(
        f"SELECT * FROM {CORRECTIONS} WHERE company_id = ? AND job_seeker_id IS ?",
        (company_id, job_seeker_id),
    )
    if row is None:
        return False
    with write_tx() as conn:
        conn.execute(f"DELETE FROM {CORRECTIONS} WHERE id = ?", (row["id"],))
    _audit(
        "employer_kind.correction_withdrawn",
        company_id,
        job_seeker_id=job_seeker_id,
        actor=actor,
        detail={"kind": row["kind"], "note": row["note"], "scope": row["scope"]},
    )
    return True


def promote_correction(
    company_id: str, promoted_by: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Make a correction everybody's, by an operator's review or by consensus.

    Promotion copies the correction with ``job_seeker_id = NULL``: the shared
    record does not point back at the person whose campaign produced it
    (FR-344).  It refuses in one case - when the register says the opposite.
    The register is occasionally stale and the human is occasionally wrong, and
    neither should win silently, so the row comes back as a conflict for the
    operator instead.
    """
    everything = [
        r for r in corrections_for_company(company_id) if r["job_seeker_id"] is not None
    ]
    if not everything:
        return {"promoted": False, "reason": "no correction to promote"}
    # Seekers can disagree with each other as well as with the machine.  The
    # kind the most of them asserted is promoted, most recent breaking a tie;
    # the minority's corrections stay private and keep working for them.
    ranked = sorted(
        {str(r["kind"]) for r in everything},
        key=lambda k: (sum(1 for r in everything if str(r["kind"]) == k), k),
        reverse=True,
    )
    kind = Kind(ranked[0])
    candidates = [r for r in everything if str(r["kind"]) == str(kind)]
    verdict = get_verdict(company_id)
    if correction_conflicts(verdict, kind):
        _audit(
            "employer_kind.correction_conflict",
            company_id,
            actor=promoted_by,
            detail={
                "correction": str(kind),
                "registry_verdict": str(verdict.kind) if verdict else None,
                "note": candidates[0]["note"],
            },
        )
        return {"promoted": False, "reason": "registry_conflict", "kind": str(kind)}

    moment = _stamp(now)
    note = "; ".join(str(r["note"]) for r in candidates[:CONSENSUS_THRESHOLD])
    evidence_url = next((r["evidence_url"] for r in candidates if r["evidence_url"]), None)
    with write_tx() as conn:
        conn.execute(
            f"DELETE FROM {CORRECTIONS} WHERE company_id = ? AND job_seeker_id IS NULL",
            (company_id,),
        )
        conn.execute(
            f"INSERT INTO {CORRECTIONS} "
            "(id, company_id, job_seeker_id, kind, note, evidence_url, scope, promoted_by, "
            " promoted_at, created_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, 'shared', ?, ?, ?)",
            (new_id(), company_id, str(kind), note, evidence_url, promoted_by, moment, moment),
        )
    _audit(
        "employer_kind.correction_promoted",
        company_id,
        actor=promoted_by,
        detail={"kind": str(kind), "note": note, "corrections": len(candidates)},
    )
    return {"promoted": True, "kind": str(kind), "promoted_by": promoted_by}


def _promote_on_consensus(
    company_id: str, kind: Kind, *, now: datetime | None = None
) -> bool:
    """Two seekers, independently, with a note each: consensus without an operator."""
    rows = [
        r
        for r in corrections_for_company(company_id)
        if r["job_seeker_id"] is not None and str(r["kind"]) == str(kind)
    ]
    seekers = {str(r["job_seeker_id"]) for r in rows}
    if len(seekers) < CONSENSUS_THRESHOLD:
        return False
    return bool(promote_correction(company_id, "consensus", now=now).get("promoted"))


def pending_review(limit: int = 100) -> list[dict[str, Any]]:
    """Private corrections waiting for an operator, with the machine's evidence.

    They appear beside what the machine said, because that is the comparison
    the reviewer has to make: a sentence from a job seeker against a quote from
    a page or a code from a register.
    """
    rows = query_all(
        f"SELECT ekc.*, c.name AS company_name, k.kind AS machine_kind, k.rung AS machine_rung, "
        f"k.confidence AS machine_confidence, k.summary AS machine_summary, "
        f"k.evidence AS machine_evidence "
        f"FROM {CORRECTIONS} ekc "
        "JOIN company c ON c.id = ekc.company_id "
        f"LEFT JOIN {TABLE} k ON k.company_id = ekc.company_id "
        "WHERE ekc.scope = 'private' ORDER BY ekc.created_at DESC LIMIT ?",
        (int(limit),),
    )
    out = []
    for row in rows:
        item = dict(row)
        item["machine_evidence"] = [
            Evidence.from_dict(e).to_dict() for e in from_json(item.get("machine_evidence"), [])
        ]
        item["disagrees"] = bool(item["machine_kind"]) and item["machine_kind"] != item["kind"]
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _audit(
    action: str,
    company_id: str,
    *,
    actor: str = "system",
    job_seeker_id: str | None = None,
    detail: dict | None = None,
) -> None:
    insert_row(
        "audit_event",
        {
            "job_seeker_id": job_seeker_id,
            "actor": actor,
            "action": action,
            "entity_type": "company_employer_kind",
            "entity_id": company_id,
            "detail": detail,
            "created_at": utcnow(),
        },
    )
