"""The structured dream job model (FR-109, FR-128, CR-410, NFR-205).

The job seeker writes what they really want in their own words and language,
with no length limit (FR-109).  This module turns that text into the structure
the rest of the system can act on, and stores it in ``dream_job_model``.

The model is a **first-class input to four other slices** - campaign planning
(FR-162), company discovery (FR-224/225), speculative openings (FR-262) and
scoring (FR-281/FR-383).  They read the stored row directly, so the JSON shapes
below are a contract.  Add keys if you must; do not rename or retype them.

Stored shapes
-------------

``target_roles``  (JSON array, strongest first)::

    {"title": str, "seniority": str|null, "priority": 1..5, "rationale": str,
     "source": "stated"|"inferred", "quote": str|null}

``role_families``::

    {"family": str, "example_titles": [str], "confidence": 0.0..1.0}

``responsibilities``::

    {"activity": str, "importance": "must"|"strong"|"nice",
     "source": "stated"|"inferred", "quote": str|null}

``company_characteristics``::

    {"attribute": "size"|"stage"|"ownership"|"sector"|"geography"|
                  "work_arrangement"|"mission"|"maturity"|"team_structure",
     "value": str, "importance": "must"|"strong"|"nice",
     "source": "stated"|"inferred", "quote": str|null}

``culture_values``::

    {"cue": str, "polarity": "seek"|"avoid", "why": str, "quote": str|null}

``deal_breakers``::

    {"constraint": str, "hard": bool, "detectable_from": [str],
     "quote": str|null}

``implicit_preferences``::

    {"preference": str, "basis": str, "confidence": 0.0..1.0}

Scalar columns: ``statement`` holds the raw FR-109 text verbatim, ``version``
increments per job seeker, and ``confirmed_by_user`` stays 0 until the job
seeker approves the model (FR-128).  Planning may read an unconfirmed model,
but scoring and generation should prefer ``latest(confirmed_only=True)``.

The free-text statement is passed to the model as an ``untrusted`` block
(NFR-205): it is user-supplied text that will be pasted from elsewhere as often
as it is typed, and it must never be able to redirect the extraction.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from dreamjob.db.repositories import enrichment as repo
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError, redact
from dreamjob.pipeline.composite import (
    ConsentRequired,
    do_not_disclose_paths,
    has_consent_for,
)
from dreamjob.pipeline.enrichment import default_llm, load_prompt

log = logging.getLogger(__name__)

IMPORTANCE = ("must", "strong", "nice")
SOURCES = ("stated", "inferred")
POLARITIES = ("seek", "avoid")
ATTRIBUTES = (
    "size", "stage", "ownership", "sector", "geography",
    "work_arrangement", "mission", "maturity", "team_structure",
)

BLOCKS: tuple[str, ...] = (
    "target_roles",
    "role_families",
    "responsibilities",
    "company_characteristics",
    "culture_values",
    "deal_breakers",
    "implicit_preferences",
)


class StatementMissing(LookupError):
    """FR-109 has not been filled in, so there is nothing to model."""


# ---------------------------------------------------------------------------
# Validation - the shapes above are a contract for four other slices
# ---------------------------------------------------------------------------


def _choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _confidence(value: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _string_list(value: Any, limit: int = 12) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()][:limit]


def _items(data: dict, block: str) -> list[dict]:
    value = data.get(block)
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    return [v if isinstance(v, dict) else {"text": str(v)} for v in value]


def normalise_model(data: dict) -> dict[str, Any]:
    """Coerce LLM output into the documented shapes, dropping what will not fit."""
    out: dict[str, Any] = {}

    out["target_roles"] = [
        {
            "title": str(item.get("title") or item.get("text") or "").strip(),
            "seniority": (str(item["seniority"]).strip() if item.get("seniority") else None),
            "priority": _priority(item.get("priority")),
            "rationale": str(item.get("rationale") or "").strip(),
            "source": _choice(item.get("source"), SOURCES, "inferred"),
            "quote": _quote(item.get("quote")),
        }
        for item in _items(data, "target_roles")
        if str(item.get("title") or item.get("text") or "").strip()
    ]

    out["role_families"] = [
        {
            "family": str(item.get("family") or item.get("text") or "").strip(),
            "example_titles": _string_list(item.get("example_titles") or item.get("examples")),
            "confidence": _confidence(item.get("confidence")),
        }
        for item in _items(data, "role_families")
        if str(item.get("family") or item.get("text") or "").strip()
    ]

    out["responsibilities"] = [
        {
            "activity": str(item.get("activity") or item.get("text") or "").strip(),
            "importance": _choice(item.get("importance"), IMPORTANCE, "strong"),
            "source": _choice(item.get("source"), SOURCES, "inferred"),
            "quote": _quote(item.get("quote")),
        }
        for item in _items(data, "responsibilities")
        if str(item.get("activity") or item.get("text") or "").strip()
    ]

    out["company_characteristics"] = [
        {
            "attribute": _choice(item.get("attribute"), ATTRIBUTES, "mission"),
            "value": str(item.get("value") or item.get("text") or "").strip(),
            "importance": _choice(item.get("importance"), IMPORTANCE, "strong"),
            "source": _choice(item.get("source"), SOURCES, "inferred"),
            "quote": _quote(item.get("quote")),
        }
        for item in _items(data, "company_characteristics")
        if str(item.get("value") or item.get("text") or "").strip()
    ]

    out["culture_values"] = [
        {
            "cue": str(item.get("cue") or item.get("text") or "").strip(),
            "polarity": _choice(item.get("polarity"), POLARITIES, "seek"),
            "why": str(item.get("why") or "").strip(),
            "quote": _quote(item.get("quote")),
        }
        for item in _items(data, "culture_values")
        if str(item.get("cue") or item.get("text") or "").strip()
    ]

    out["deal_breakers"] = [
        {
            "constraint": str(item.get("constraint") or item.get("text") or "").strip(),
            "hard": bool(item.get("hard", True)),
            "detectable_from": _string_list(item.get("detectable_from")),
            "quote": _quote(item.get("quote")),
        }
        for item in _items(data, "deal_breakers")
        if str(item.get("constraint") or item.get("text") or "").strip()
    ]

    out["implicit_preferences"] = [
        {
            "preference": str(item.get("preference") or item.get("text") or "").strip(),
            "basis": str(item.get("basis") or "").strip(),
            "confidence": _confidence(item.get("confidence")),
        }
        for item in _items(data, "implicit_preferences")
        if str(item.get("preference") or item.get("text") or "").strip()
    ]

    out["summary"] = str(data.get("summary") or "").strip()
    return out


def _priority(value: Any) -> int:
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return 3


def _quote(value: Any) -> str | None:
    text = str(value).strip() if value else ""
    return text[:300] or None


# ---------------------------------------------------------------------------
# Build and persist
# ---------------------------------------------------------------------------


def resolve_statement(job_seeker_id: str, persona_id: str | None = None) -> str:
    """The FR-109 text, from the newest profile version."""
    version = repo.latest_profile_version(job_seeker_id, persona_id)
    statement = (version or {}).get("dream_job_statement") or ""
    return str(statement).strip()


def build_dream_job_model(
    job_seeker_id: str,
    *,
    statement: str | None = None,
    persona_id: str | None = None,
    campaign_id: str | None = None,
    composite: dict | None = None,
    language: str = "en",
    llm: LLMClient | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Turn the dream-job statement into the structured model (FR-128)."""
    text = (statement or resolve_statement(job_seeker_id, persona_id)).strip()
    if not text:
        raise StatementMissing(
            f"job seeker {job_seeker_id} has not written a dream-job statement (FR-109)"
        )

    if llm is None:
        llm = default_llm(job_seeker_id, campaign_id)

    if llm is None:
        blocks = {block: [] for block in BLOCKS}
        blocks["summary"] = text[:400]
        generation = {
            "mode": "unparsed",
            "reason": "no LLM configured; the statement is stored verbatim and "
            "no structure was inferred from it",
        }
    else:
        if not has_consent_for(job_seeker_id, "llm_transfer"):
            raise ConsentRequired(
                "Consent for transferring profile data to the LLM provider (CR-410) "
                "has not been recorded for this job seeker."
            )
        blocks, generation = _extract(
            llm,
            job_seeker_id=job_seeker_id,
            statement=text,
            composite=composite,
            language=language,
        )

    if not persist:
        return {**blocks, "statement": text, "_generation": generation}

    model_id = repo.insert_dream_model(
        job_seeker_id,
        {
            "persona_id": persona_id,
            # The version is allocated inside insert_dream_model, in the same
            # transaction as the insert; computing it here was a dead read.
            "statement": text,
            "confirmed_by_user": 0,
            **{block: blocks.get(block, []) for block in BLOCKS},
        },
    )
    stored = repo.get_dream_model(job_seeker_id, model_id) or {}
    stored["summary"] = blocks.get("summary", "")
    stored["_generation"] = generation
    return stored


def _extract(
    llm: LLMClient,
    *,
    job_seeker_id: str,
    statement: str,
    composite: dict | None,
    language: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = load_prompt("dream_job_model")
    system, user = prompt.render(language=language)

    do_not_disclose = do_not_disclose_paths(job_seeker_id)
    untrusted = {"statement": statement}
    if composite:
        context = redact(
            {
                "narrative": composite.get("narrative"),
                "seniority": composite.get("seniority"),
                "core_competencies": composite.get("core_competencies"),
                "domains": composite.get("domains"),
            },
            do_not_disclose,
        )
        untrusted["composite"] = json.dumps(context, ensure_ascii=False, default=str)

    try:
        data = llm.complete_json(
            "profile.dreamjob",
            system=system,
            user=user,
            untrusted=untrusted,
            entity_type="dream_job_model",
            entity_id=job_seeker_id,
            prompt_template=prompt.name,
            prompt_version=prompt.version,
            # DeepSeek's reasoning model spends its reasoning tokens out of the
            # same budget, so a cap sized for the answer alone truncates the
            # JSON and loses the whole extraction.  Seven blocks read out of a
            # single page of prose is not a long answer - roughly 2,500 tokens
            # of it - but the deliberation in front of it is: extractions of
            # the same statement have spent 1,307, 16,842 and 26,050 tokens.
            # The last of those would have been lost under a 24,000 cap, so
            # the budget is sized for the deliberation, not for the answer.
            max_tokens=48_000,
        )
    except (LLMError, BudgetExhausted) as exc:
        log.warning("Dream job model extraction failed: %s", exc)
        blocks: dict[str, Any] = {block: [] for block in BLOCKS}
        blocks["summary"] = statement[:400]
        return blocks, {"mode": "unparsed", "reason": str(exc)[:300]}

    if not isinstance(data, dict):
        raise LLMError("dream job model extraction did not return a JSON object")
    return normalise_model(data), {
        "mode": "llm",
        "prompt": prompt.name,
        "prompt_version": prompt.version,
    }


# ---------------------------------------------------------------------------
# Reads for the other slices
# ---------------------------------------------------------------------------


def latest(
    job_seeker_id: str, persona_id: str | None = None, *, confirmed_only: bool = False
) -> dict | None:
    """The current dream job model.  One import point for planning and scoring."""
    return repo.latest_dream_model(job_seeker_id, persona_id, confirmed_only=confirmed_only)


def confirm(job_seeker_id: str, model_id: str) -> dict | None:
    """FR-128: the job seeker approves the model before it drives a campaign."""
    return repo.confirm_dream_model(job_seeker_id, model_id)


def edit(job_seeker_id: str, model_id: str, patch: dict) -> dict | None:
    """Apply the job seeker's corrections, re-validating the shapes."""
    current = repo.get_dream_model(job_seeker_id, model_id)
    if current is None:
        return None
    merged = {block: current.get(block) or [] for block in BLOCKS}
    merged.update({k: v for k, v in patch.items() if k in BLOCKS})
    normalised = normalise_model(merged)
    values: dict[str, Any] = {block: normalised[block] for block in BLOCKS}
    if isinstance(patch.get("statement"), str) and patch["statement"].strip():
        values["statement"] = patch["statement"].strip()
    return repo.update_dream_model(job_seeker_id, model_id, values)


def as_query_terms(model: dict | None, limit: int = 24) -> list[str]:
    """Role titles and families for campaign planning and discovery (FR-162)."""
    if not model:
        return []
    terms: list[str] = []
    for role in model.get("target_roles") or []:
        if role.get("title"):
            terms.append(role["title"])
    for family in model.get("role_families") or []:
        terms += [t for t in (family.get("example_titles") or []) if t]
        if family.get("family"):
            terms.append(family["family"])
    seen: set[str] = set()
    return [t for t in terms if not (t.lower() in seen or seen.add(t.lower()))][:limit]


def hard_deal_breakers(model: dict | None) -> list[dict]:
    """Deal-breakers that must veto an opportunity outright (FR-383)."""
    if not model:
        return []
    return [d for d in (model.get("deal_breakers") or []) if d.get("hard")]


def must_haves(model: dict | None) -> list[dict]:
    """Responsibilities and company characteristics marked 'must' (FR-281)."""
    if not model:
        return []
    out = [r for r in (model.get("responsibilities") or []) if r.get("importance") == "must"]
    out += [
        c for c in (model.get("company_characteristics") or []) if c.get("importance") == "must"
    ]
    return out
