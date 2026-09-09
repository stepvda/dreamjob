"""SQL for response capture and redirection advice (CR-408).

Everything here is private data, so every query filters on ``job_seeker_id``
(FR-101, FR-344).
"""

from __future__ import annotations

from typing import Any

from dreamjob.db.connection import from_json, query_all, query_one

_JSON_COLS = ("variables", "evidence", "directive_patch", "segments", "caveats")


def _decode(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for col in _JSON_COLS:
        if col in out:
            out[col] = from_json(out[col], None)
    return out


def _decode_all(rows: list[dict]) -> list[dict]:
    return [_decode(r) for r in rows]  # type: ignore[misc]


# --- Segment analysis inputs ------------------------------------------------

def segment_rows(job_seeker_id: str, limit: int = 1000) -> list[dict]:
    """One row per sent application, with the job and company attributes.

    Wider than ``pipeline_cards.outcome_rows``: that one carries the variables
    describing *how* the application was written, this one carries what it was
    written *about* - sector, language and the opportunity's own fields - which
    is what the segment analysis groups by.
    """
    rows = query_all(
        """
        SELECT k.id, k.stage, k.outcome, k.variables, k.created_at, k.updated_at,
               k.reached_replied_at, k.reached_interview_at, k.reached_offer_at,
               o.id            AS opportunity_id,
               o.kind          AS opportunity_kind,
               o.score, o.function_family, o.seniority, o.country,
               o.work_arrangement, o.language,
               c.size_band, c.stage AS company_stage, c.sector_codes,
               c.name          AS company_name
        FROM pipeline_card k
        LEFT JOIN opportunity o ON o.id = k.opportunity_id
        LEFT JOIN company     c ON c.id = o.company_id
        WHERE k.job_seeker_id = ?
        ORDER BY k.created_at DESC
        LIMIT ?
        """,
        (job_seeker_id, int(limit)),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["variables"] = from_json(d.get("variables"), {})
        d["sector_codes"] = from_json(d.get("sector_codes"), [])
        out.append(d)
    return out


def dream_job_summary(job_seeker_id: str) -> str:
    """The confirmed dream-job model as a compact string for a prompt."""
    row = query_one(
        """
        SELECT target_roles, role_families, company_characteristics,
               culture_values, deal_breakers
        FROM dream_job_model
        WHERE job_seeker_id = ?
        ORDER BY version DESC LIMIT 1
        """,
        (job_seeker_id,),
    )
    if row is None:
        return "{}"
    return str({k: from_json(v, None) for k, v in dict(row).items() if v})


# --- Advice -----------------------------------------------------------------

def advice_by_id(job_seeker_id: str, advice_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM redirection_advice WHERE id = ? AND job_seeker_id = ?",
            (advice_id, job_seeker_id),
        )
    )


def open_advice(job_seeker_id: str, outcome: str | None = None) -> list[dict]:
    if outcome:
        rows = query_all(
            """
            SELECT * FROM redirection_advice
            WHERE job_seeker_id = ? AND status = 'open' AND outcome = ?
            ORDER BY ABS(COALESCE(expected_effect, 0)) DESC, computed_at DESC
            """,
            (job_seeker_id, outcome),
        )
    else:
        rows = query_all(
            """
            SELECT * FROM redirection_advice
            WHERE job_seeker_id = ? AND status = 'open'
            ORDER BY ABS(COALESCE(expected_effect, 0)) DESC, computed_at DESC
            """,
            (job_seeker_id,),
        )
    return _decode_all(rows)


def advice_history(job_seeker_id: str, limit: int = 100) -> list[dict]:
    return _decode_all(
        query_all(
            """
            SELECT * FROM redirection_advice
            WHERE job_seeker_id = ? AND status != 'open'
            ORDER BY resolved_at DESC LIMIT ?
            """,
            (job_seeker_id, int(limit)),
        )
    )


def latest_segment_run(job_seeker_id: str, outcome: str) -> dict | None:
    return _decode(
        query_one(
            """
            SELECT * FROM outcome_segment_run
            WHERE job_seeker_id = ? AND outcome = ?
            ORDER BY computed_at DESC LIMIT 1
            """,
            (job_seeker_id, outcome),
        )
    )


# --- Manual responses -------------------------------------------------------

def dispatch_for_seeker(job_seeker_id: str, dispatch_id: str) -> dict | None:
    return query_one(
        "SELECT * FROM dispatch WHERE id = ? AND job_seeker_id = ?",
        (dispatch_id, job_seeker_id),
    )


def opportunity_of_dispatch(job_seeker_id: str, dispatch_id: str) -> str | None:
    row = query_one(
        """
        SELECT p.opportunity_id
        FROM dispatch d
        JOIN application_package p ON p.id = d.application_package_id
        WHERE d.id = ? AND d.job_seeker_id = ?
        """,
        (dispatch_id, job_seeker_id),
    )
    return row["opportunity_id"] if row else None


def manual_response_by_id(job_seeker_id: str, manual_response_id: str) -> dict | None:
    return query_one(
        "SELECT * FROM manual_response WHERE id = ? AND job_seeker_id = ?",
        (manual_response_id, job_seeker_id),
    )


def responses_for_seeker(job_seeker_id: str, limit: int = 200) -> list[dict]:
    """Every response, detected or entered by hand, newest first.

    The union is deliberate: a job seeker reviewing what came back should not
    have to care which ones the system found on its own.
    """
    return query_all(
        """
        SELECT r.id, r.dispatch_id, r.from_address, r.subject, r.body,
               r.received_at, r.classification, r.classification_confidence,
               r.handled, r.created_at,
               m.id            AS manual_response_id,
               m.channel, m.stated_outcome, m.notes,
               COALESCE(m.opportunity_id, p.opportunity_id) AS opportunity_id,
               o.title         AS opportunity_title,
               o.kind          AS opportunity_kind,
               c.name          AS company_name
        FROM incoming_reply r
        LEFT JOIN manual_response    m ON m.incoming_reply_id = r.id
        LEFT JOIN dispatch           d ON d.id = r.dispatch_id
        LEFT JOIN application_package p ON p.id = d.application_package_id
        LEFT JOIN opportunity        o ON o.id = COALESCE(m.opportunity_id, p.opportunity_id)
        LEFT JOIN company            c ON c.id = o.company_id
        WHERE r.job_seeker_id = ?
        ORDER BY COALESCE(r.received_at, r.created_at) DESC
        LIMIT ?
        """,
        (job_seeker_id, int(limit)),
    )


def sent_awaiting_response(job_seeker_id: str, limit: int = 200) -> list[dict]:
    """Applications with no response recorded yet.

    This is the picking list for entering a response by hand, so it carries
    enough to recognise an application: who it went to, for what, and when.
    """
    return query_all(
        """
        SELECT d.id AS dispatch_id, d.recipient_email, d.recipient_name,
               d.subject, d.sent_at, d.delivery_status,
               p.opportunity_id,
               o.title AS opportunity_title, o.kind AS opportunity_kind,
               c.name  AS company_name
        FROM dispatch d
        JOIN application_package p ON p.id = d.application_package_id
        LEFT JOIN opportunity o ON o.id = p.opportunity_id
        LEFT JOIN company     c ON c.id = o.company_id
        WHERE d.job_seeker_id = ?
          AND d.sent_at IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM incoming_reply r WHERE r.dispatch_id = d.id
          )
        ORDER BY d.sent_at DESC
        LIMIT ?
        """,
        (job_seeker_id, int(limit)),
    )


def card_for_opportunity(job_seeker_id: str, opportunity_id: str) -> dict | None:
    return query_one(
        "SELECT * FROM pipeline_card WHERE job_seeker_id = ? AND opportunity_id = ?",
        (job_seeker_id, opportunity_id),
    )


def reply_row(job_seeker_id: str, reply_id: str) -> dict | None:
    return query_one(
        "SELECT * FROM incoming_reply WHERE id = ? AND job_seeker_id = ?",
        (reply_id, job_seeker_id),
    )


# --- Journey state (overview screen) ---------------------------------------

def journey_counts(job_seeker_id: str) -> dict[str, Any]:
    """Counts behind the workflow map, in one round trip."""
    def scalar(sql: str, params: tuple) -> int:
        row = query_one(sql, params)
        return int(row["n"]) if row else 0

    return {
        "companies": scalar(
            """
            SELECT COUNT(DISTINCT o.company_id) AS n FROM opportunity o
            WHERE o.job_seeker_id = ? AND o.company_id IS NOT NULL
            """,
            (job_seeker_id,),
        ),
        "opportunities": scalar(
            "SELECT COUNT(*) AS n FROM opportunity WHERE job_seeker_id = ?",
            (job_seeker_id,),
        ),
        "scored": scalar(
            "SELECT COUNT(*) AS n FROM opportunity WHERE job_seeker_id = ? AND score IS NOT NULL",
            (job_seeker_id,),
        ),
        "speculative": scalar(
            "SELECT COUNT(*) AS n FROM opportunity WHERE job_seeker_id = ? AND kind = 'speculative'",
            (job_seeker_id,),
        ),
        "packages": scalar(
            "SELECT COUNT(*) AS n FROM application_package WHERE job_seeker_id = ?",
            (job_seeker_id,),
        ),
        "approved": scalar(
            "SELECT COUNT(*) AS n FROM application_package WHERE job_seeker_id = ? AND status = 'approved'",
            (job_seeker_id,),
        ),
        "sent": scalar(
            "SELECT COUNT(*) AS n FROM dispatch WHERE job_seeker_id = ? AND sent_at IS NOT NULL",
            (job_seeker_id,),
        ),
        "replies": scalar(
            "SELECT COUNT(*) AS n FROM incoming_reply WHERE job_seeker_id = ?",
            (job_seeker_id,),
        ),
        "cards": scalar(
            "SELECT COUNT(*) AS n FROM pipeline_card WHERE job_seeker_id = ?",
            (job_seeker_id,),
        ),
        "interviews": scalar(
            "SELECT COUNT(*) AS n FROM pipeline_card WHERE job_seeker_id = ? AND stage IN ('interview','offer')",
            (job_seeker_id,),
        ),
        "contacts": scalar(
            """
            SELECT COUNT(DISTINCT p.contact_id) AS n FROM application_package p
            WHERE p.job_seeker_id = ? AND p.contact_id IS NOT NULL
            """,
            (job_seeker_id,),
        ),
        "watchlist": scalar(
            "SELECT COUNT(*) AS n FROM watchlist_entry WHERE job_seeker_id = ? AND active = 1",
            (job_seeker_id,),
        ),
        "open_advice": scalar(
            "SELECT COUNT(*) AS n FROM redirection_advice WHERE job_seeker_id = ? AND status = 'open'",
            (job_seeker_id,),
        ),
    }


def journey_state(job_seeker_id: str) -> dict[str, Any]:
    """The rows the workflow map needs to decide what is done and what is next."""
    return {
        "profile": query_one(
            "SELECT id, version, dream_job_statement, photo_path FROM profile_version "
            "WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
            (job_seeker_id,),
        ),
        # Count unresolved conflicts for the LATEST version only. The queue is
        # version-scoped: older versions keep their own rows, and a resolution
        # made on one version is carried forward by replace_conflicts onto the
        # next. Counting every historical row across all versions inflates the
        # figure (old versions never lose their rows), so the "100 conflicts to
        # resolve" the journey map shows would never reflect what the conflicts
        # tab actually lists.
        "unresolved_conflicts": (
            query_one(
                "SELECT COUNT(*) AS n FROM profile_conflict "
                "WHERE job_seeker_id = ? AND profile_version_id = ("
                "  SELECT id FROM profile_version WHERE job_seeker_id = ? "
                "  ORDER BY version DESC LIMIT 1"
                ") AND (resolution IS NULL OR resolution = 'unresolved')",
                (job_seeker_id, job_seeker_id),
            )
            or {"n": 0}
        )["n"],
        "composite": query_one(
            "SELECT id, version, edited_by_user FROM composite_profile "
            "WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
            (job_seeker_id,),
        ),
        "pending_findings": (
            query_one(
                "SELECT COUNT(*) AS n FROM enrichment_finding "
                "WHERE job_seeker_id = ? AND status = 'pending'",
                (job_seeker_id,),
            )
            or {"n": 0}
        )["n"],
        "dream_job": query_one(
            "SELECT id, version, confirmed_by_user FROM dream_job_model "
            "WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
            (job_seeker_id,),
        ),
        "directives": query_one(
            "SELECT id, name, version, discretion_mode FROM directive_set "
            "WHERE job_seeker_id = ? ORDER BY created_at DESC LIMIT 1",
            (job_seeker_id,),
        ),
        "campaign": query_one(
            "SELECT id, name, status, stage, tokens_used, token_budget, cost_eur "
            "FROM campaign WHERE job_seeker_id = ? ORDER BY created_at DESC LIMIT 1",
            (job_seeker_id,),
        ),
    }
