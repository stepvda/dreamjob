"""Advice on where to redirect the search (extends FR-285 and FR-425).

The product owner's ask, in their words: *if a lot of rejections are received
for a certain type of job then the AI should advise on how to redirect the type
of job/company to get higher acceptance rates.*

That is a genuinely useful thing to automate and a genuinely easy thing to do
badly, so this module is built around three rules:

1. **The numbers come first, the model second.**  Segment rates, sample sizes
   and Wilson intervals are computed deterministically in ``segments.py``.  The
   model is given those figures and asked to turn them into advice - it is
   never asked to find the pattern itself, because a language model asked to
   spot trends in a table will find one whether or not it is there.

2. **Nothing is applied automatically.**  Advice becomes a proposed change to a
   directive set that the job seeker reviews and accepts.  Accepting creates a
   new directive-set version; the old one is never mutated (FR-148, NFR-305).

3. **Advice states what it rests on.**  Every proposal carries the two rates,
   both sample sizes and the confidence label.  Where the evidence is thin the
   advice says so rather than sounding confident anyway - a job seeker acting
   on six data points deserves to know that is what they are doing.
"""

from __future__ import annotations

import logging
from typing import Any

from dreamjob.db.connection import from_json, insert_row, update_row, utcnow
from dreamjob.db.repositories import directives as directive_repo
from dreamjob.db.repositories import learning as repo
from dreamjob.llm.client import LLMClient
from dreamjob.pipeline.directives import DirectiveSetPayload
from dreamjob.postapp.segments import (
    DIMENSIONS,
    MIN_SEGMENT_TO_ADVISE,
    SegmentAnalysis,
    analyse_segments,
)

log = logging.getLogger(__name__)

PROMPT_VERSION = "1.0"

SYSTEM = """You advise a job seeker on where to redirect their search, based on \
measured outcome rates from applications they have already sent.

You are given a table of segments. Each row is a kind of job or company, the \
number of applications that resolved in that segment, how many reached the \
target outcome, the observed rate, and a plausible range for that rate. You are \
also given the job seeker's current search directives and their dream-job model.

Your job is to turn those figures into advice a person can act on this week.

Rules you must follow:
- Base every claim on the figures given. Never invent a rate, a count or a segment.
- Always name the sample size in the rationale. "3 of 14" is honest; "performs \
poorly" alone is not.
- Where the evidence is labelled thin or indicative, say the pattern is not yet \
established and frame the suggestion as something to test, not something to do.
- Never recommend abandoning something central to the dream-job model just \
because its numbers are weak so far. Say plainly when the data conflicts with \
what the person said they want, and let them decide.
- Prefer redirection to retreat: pair a weak segment with a stronger one the \
person is already succeeding in, rather than only saying what to stop.
- A rejection is information about fit, timing or targeting - not about worth. \
Write as a level-headed colleague would, not as a coach.

Return JSON only."""

SCHEMA_HINT = """{
  "proposals": [
    {
      "dimension": "function_family | seniority | size_band | company_stage | sector | work_arrangement | country | opportunity_kind | language",
      "from_value": "the segment doing badly, or null if this is purely an expansion",
      "to_value": "the segment to move towards, or null if this is purely a reduction",
      "headline": "one sentence, imperative, under 90 characters",
      "rationale": "2-4 sentences naming the actual counts and rates on both sides",
      "confidence": "indicative | suggestive",
      "expected_effect_points": 12.5,
      "conflicts_with_dream_job": false,
      "conflict_note": "only if conflicts_with_dream_job is true",
      "directive_patch": {"group": "job_content | company_type | location | work_arrangement", "field": "…", "action": "add | remove | replace", "value": "…"}
    }
  ],
  "summary": "2-3 sentences on the overall picture, including what the data cannot yet tell them",
  "insufficient_data": false
}"""


def _table(analysis: SegmentAnalysis) -> str:
    """The figures, as a compact table the model reads but cannot embellish."""
    lines = [
        f"Outcome analysed: {analysis.outcome}",
        f"Resolved applications: {analysis.resolved_size} "
        f"(of {analysis.sample_size} sent)",
        f"Overall {analysis.outcome} rate: {analysis.baseline_rate:.0%}",
        "",
        "dimension | value | n | reached | rate | plausible range | vs overall | evidence",
        "--- | --- | --- | --- | --- | --- | --- | ---",
    ]
    for dim, items in analysis.segments.items():
        for s in items:
            lines.append(
                f"{DIMENSIONS.get(dim, dim)} | {s.value} | {s.n} | {s.successes} | "
                f"{s.rate:.0%} | {s.interval[0]:.0%}–{s.interval[1]:.0%} | "
                f"{s.lift:+.0%} pts | {s.evidence}"
            )
    if analysis.caveats:
        lines += ["", "Caveats that must shape your advice:"]
        lines += [f"- {c}" for c in analysis.caveats]
    return "\n".join(lines)


def generate_advice(
    job_seeker_id: str,
    *,
    outcome: str = "reply",
    campaign_id: str | None = None,
    analysis: SegmentAnalysis | None = None,
    store: bool = True,
) -> dict[str, Any]:
    """Produce redirection proposals from measured outcomes.

    Returns the proposals plus the analysis they came from, so the UI can show
    the advice and the figures side by side.
    """
    analysis = analysis or analyse_segments(job_seeker_id, outcome=outcome)

    if analysis.resolved_size < MIN_SEGMENT_TO_ADVISE:
        return {
            "proposals": [],
            "summary": (
                f"Only {analysis.resolved_size} applications have resolved so far. "
                f"Send and resolve at least {MIN_SEGMENT_TO_ADVISE} before reading "
                "anything into which kinds of role reply — at this size the "
                "difference between segments is chance."
            ),
            "insufficient_data": True,
            "analysis": analysis.as_dict(),
        }

    sets = directive_repo.list_for_seeker(job_seeker_id)
    directive_set = sets[0] if sets else None
    dream = repo.dream_job_summary(job_seeker_id)

    llm = LLMClient(campaign_id=campaign_id, job_seeker_id=job_seeker_id)
    try:
        result = llm.complete_json(
            "score.opportunity",
            SYSTEM,
            (
                "Measured outcomes:\n\n"
                f"{_table(analysis)}\n\n"
                "Current search directives (JSON):\n"
                f"{_directives_digest(directive_set)}\n\n"
                "Dream-job model (JSON):\n"
                f"{dream}\n\n"
                "Propose at most five redirections, strongest evidence first."
            ),
            schema_hint=SCHEMA_HINT,
            prompt_template="redirection_advice",
            prompt_version=PROMPT_VERSION,
            entity_type="job_seeker",
            entity_id=job_seeker_id,
            temperature=0.3,
        )
    except Exception as exc:  # noqa: BLE001 - advice is optional, figures are not
        log.exception("Redirection advice failed for %s", job_seeker_id)
        return {
            "proposals": [],
            "summary": (
                "The figures below were computed, but the advice step could not "
                f"run ({exc}). The segment table is still usable on its own."
            ),
            "insufficient_data": False,
            "analysis": analysis.as_dict(),
            "error": str(exc),
        }

    proposals = _validate(result.get("proposals") or [], analysis)

    if store:
        for p in proposals:
            try:
                p["id"] = insert_row(
                    "redirection_advice",
                    {
                        "job_seeker_id": job_seeker_id,
                        "outcome": outcome,
                        "dimension": p["dimension"],
                        "from_value": p.get("from_value"),
                        "to_value": p.get("to_value"),
                        "headline": p["headline"],
                        "rationale": p.get("rationale"),
                        "evidence": p.get("evidence"),
                        "confidence": p.get("confidence", "indicative"),
                        "expected_effect": p.get("expected_effect_points"),
                        "directive_patch": p.get("directive_patch"),
                        "status": "open",
                        "computed_at": analysis.computed_at,
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception("Could not store redirection advice for %s", job_seeker_id)

    return {
        "proposals": proposals,
        "summary": result.get("summary", ""),
        "insufficient_data": bool(result.get("insufficient_data")),
        "analysis": analysis.as_dict(),
    }


def _validate(raw: list[dict], analysis: SegmentAnalysis) -> list[dict]:
    """Keep only proposals whose segments actually exist in the analysis.

    This is the guard against the failure mode that matters here: a fluent
    recommendation about a segment the job seeker never applied to. Each
    surviving proposal is re-attached to the real figures, so the UI shows
    measured numbers rather than the model's recollection of them.
    """
    by_dim = {
        dim: {s.value: s for s in items} for dim, items in analysis.segments.items()
    }
    out: list[dict] = []

    for p in raw:
        dim = p.get("dimension")
        if dim not in DIMENSIONS:
            log.info("Dropping proposal for unknown dimension %r", dim)
            continue

        known = by_dim.get(dim, {})
        frm = p.get("from_value")
        to = p.get("to_value")

        if frm and frm not in known:
            log.info("Dropping proposal: %s=%r was never applied to", dim, frm)
            continue
        if to and to not in known:
            # Moving towards something unmeasured is a legitimate suggestion,
            # but it is a hypothesis, not an observation - label it as such.
            p["confidence"] = "indicative"
            p["extrapolated_target"] = True

        evidence: dict[str, Any] = {}
        if frm and frm in known:
            s = known[frm]
            evidence["from"] = {
                "value": frm, "n": s.n, "successes": s.successes,
                "rate": round(s.rate, 3),
                "interval": [round(s.interval[0], 3), round(s.interval[1], 3)],
                "evidence": s.evidence,
                "rejections": s.rejections, "no_response": s.no_response,
            }
            if s.n < MIN_SEGMENT_TO_ADVISE:
                p["confidence"] = "indicative"
        if to and to in known:
            s = known[to]
            evidence["to"] = {
                "value": to, "n": s.n, "successes": s.successes,
                "rate": round(s.rate, 3),
                "interval": [round(s.interval[0], 3), round(s.interval[1], 3)],
                "evidence": s.evidence,
            }
        evidence["baseline"] = {
            "rate": round(analysis.baseline_rate, 3),
            "n": analysis.resolved_size,
            "outcome": analysis.outcome,
        }

        # Recompute the effect from the stored figures rather than trusting the
        # model's arithmetic.
        if "from" in evidence and "to" in evidence:
            p["expected_effect_points"] = round(
                (evidence["to"]["rate"] - evidence["from"]["rate"]) * 100, 1
            )

        p["evidence"] = evidence
        p["dimension_label"] = DIMENSIONS[dim]
        out.append(p)

    return out


def _directives_digest(directive_set: dict | None) -> str:
    """The directive groups only - no notes, no compensation figures."""
    if not directive_set:
        return "{}"
    keep = ("job_content", "company_type", "location", "work_arrangement")
    return str(
        {k: from_json(directive_set.get(k), {}) for k in keep if directive_set.get(k)}
    )


def apply_advice(job_seeker_id: str, advice_id: str) -> dict[str, Any]:
    """Accept a proposal by writing a NEW directive-set version (FR-148).

    The previous version is untouched, so a campaign that ran under it stays
    explainable and the change can be undone by reverting to it.
    """
    advice = repo.advice_by_id(job_seeker_id, advice_id)
    if advice is None:
        raise KeyError("No such advice")
    if advice["status"] != "open":
        raise ValueError(f"Advice is already {advice['status']}")

    patch = from_json(advice["directive_patch"], {}) or {}
    if not patch:
        raise ValueError("This proposal carries no directive change to apply")

    sets = directive_repo.list_for_seeker(job_seeker_id)
    if not sets:
        raise ValueError("There is no directive set to redirect")
    current = directive_repo.get(sets[0]["id"], job_seeker_id)
    if current is None:
        raise ValueError("The current directive set could not be read")

    payload = _patched_payload(current, patch, advice["headline"])
    new_id = directive_repo.create(job_seeker_id, payload)
    new_set = directive_repo.get(new_id, job_seeker_id) or {"id": new_id}

    update_row(
        "redirection_advice",
        advice_id,
        {
            "status": "applied",
            "applied_directive_set_id": new_set["id"],
            "resolved_at": utcnow(),
        },
    )
    return {"advice_id": advice_id, "directive_set": new_set}


def dismiss_advice(job_seeker_id: str, advice_id: str, reason: str = "") -> None:
    """Dismiss a proposal. The reason is kept — it is useful feedback."""
    advice = repo.advice_by_id(job_seeker_id, advice_id)
    if advice is None:
        raise KeyError("No such advice")
    update_row(
        "redirection_advice",
        advice_id,
        {"status": "dismissed", "dismissed_reason": reason, "resolved_at": utcnow()},
    )


# --- Applying a patch -------------------------------------------------------

# Which list field each patch group may touch. Restricting this here rather
# than trusting the model's ``field`` keeps a malformed proposal from writing
# an arbitrary key into a directive set.
PATCHABLE: dict[str, set[str]] = {
    "job_content": {
        "target_titles", "function_families", "must_have_skills",
        "nice_to_have_skills", "industries_of_interest", "industries_excluded",
        "keywords_to_avoid",
    },
    "company_type": {"size_bands", "stages", "trajectories", "ownership", "excluded_companies"},
    "location": {"countries", "regions"},
    "work_arrangement": {"arrangements", "contract_types"},
}


def _patched_payload(
    current: dict[str, Any], patch: dict[str, Any], headline: str
) -> DirectiveSetPayload:
    """Build the next directive-set version with one change applied.

    Only list membership is changed - add a value, remove one, or swap one for
    another. Anything structural is left to the directive editor, where the
    job seeker can see what they are doing.
    """
    group = patch.get("group")
    field_name = patch.get("field")
    action = patch.get("action", "add")
    value = patch.get("value")

    if group not in PATCHABLE:
        raise ValueError(f"Cannot patch directive group {group!r}")
    if field_name not in PATCHABLE[group]:
        raise ValueError(f"Cannot patch {group}.{field_name}")
    if value in (None, ""):
        raise ValueError("The proposed change carries no value")

    data = {
        "name": current.get("name") or "Untitled directives",
        "persona_id": current.get("persona_id"),
        "job_content": from_json(current.get("job_content"), {}) or {},
        "company_type": from_json(current.get("company_type"), {}) or {},
        "location": from_json(current.get("location"), {}) or {},
        "work_arrangement": from_json(current.get("work_arrangement"), {}) or {},
        "compensation": from_json(current.get("compensation"), {}) or {},
        "notes_to_ai": current.get("notes_to_ai"),
        "spontaneous_only": bool(current.get("spontaneous_only")),
        "discretion_mode": bool(current.get("discretion_mode")),
        "discretion_excluded_companies": from_json(
            current.get("discretion_excluded_companies"), []
        ) or [],
        "discretion_excluded_contacts": from_json(
            current.get("discretion_excluded_contacts"), []
        ) or [],
    }

    items = list(data[group].get(field_name) or [])
    if action == "add" and value not in items:
        items.append(value)
    elif action == "remove":
        items = [i for i in items if i != value]
    elif action == "replace":
        old = patch.get("replaces")
        items = [i for i in items if i != old]
        if value not in items:
            items.append(value)
    data[group][field_name] = items

    note = (data.get("notes_to_ai") or "").strip()
    marker = f"Redirected from outcome data: {headline}"
    data["notes_to_ai"] = f"{note}\n{marker}".strip() if note else marker

    return DirectiveSetPayload.model_validate(data)
