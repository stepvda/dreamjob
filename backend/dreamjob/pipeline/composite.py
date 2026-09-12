"""Composite profile synthesis (FR-121, FR-125, FR-126, CR-410, NFR-205).

The composite profile is the single description of the job seeker that every
later stage reads: campaign planning, company discovery, speculative openings,
scoring and generation.  It merges what the job seeker supplied - LinkedIn
export, CV, manual edits, dream-job statement - with the online findings whose
identity was established (FR-124).

Traceability is the point of the data model here (FR-125, RK-02).  Every block
is a list of *statements*, each with a stable id, and ``evidence_refs`` maps
that id to the source that supports it::

    {
      "career_trajectory": [
        {"id": "career_trajectory:1", "text": "Moved from ... to ..."},
        ...
      ],
      "evidence_refs": {
        "career_trajectory:1": {
          "source_type": "linkedin_export",
          "source_ref": "experience[2]",
          "quote": "Head of Data, 2019-2023"
        }
      }
    }

``source_type`` is one of ``user_input``, ``linkedin_export``, ``cv``,
``profile``, ``web`` or ``unsupported``.  A ``web`` reference must name a URL
that is actually among this job seeker's usable findings; anything else is
demoted to ``unsupported`` before storage, because a citation the pipeline
cannot verify is exactly how a homonym's fact reaches a CV.  ``unsupported``
statements are kept but flagged, so the UI can show them as needing the job
seeker's confirmation rather than silently presenting them as sourced.

CR-410 is enforced on the way out: consent for ``llm_transfer`` is checked
before any profile data is sent to DeepSeek, and the payload passes through
``llm.client.redact`` with the job seeker's do-not-disclose paths (FR-106) and
the shared special-category hints (FR-127).

FR-126: with enrichment disabled, no finding is read and the composite is built
from user-supplied data alone.  With no LLM configured at all, a structural
composite is assembled directly from the profile - fewer sentences, same
provenance guarantees, no invented facts (CR-405).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from dreamjob.db.repositories import enrichment as repo
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError, redact
from dreamjob.pipeline.enrichment import default_llm, enrichment_allowed, load_prompt

log = logging.getLogger(__name__)

LIST_BLOCKS: tuple[str, ...] = (
    "career_trajectory",
    "core_competencies",
    "adjacent_competencies",
    "domains",
    "achievements",
    "public_footprint",
    "inferred_preferences",
    "constraints",
)
SOURCE_TYPES = frozenset({"user_input", "linkedin_export", "cv", "profile", "web", "unsupported"})
UNSUPPORTED = {"source_type": "unsupported", "source_ref": None, "quote": None}


class ConsentRequired(PermissionError):
    """CR-410: profile data may not be sent to the provider without consent."""


class ProfileMissing(LookupError):
    """No profile version exists yet, so there is nothing to synthesise."""


# ---------------------------------------------------------------------------
# Cross-slice lookups, with a fallback while neighbouring slices land
# ---------------------------------------------------------------------------


def has_consent_for(job_seeker_id: str, kind: str) -> bool:
    """CR-410 consent check, resolved through the auth slice when it is present."""
    try:
        from dreamjob.security.auth_service import has_consent  # noqa: PLC0415
    except ImportError:
        return repo.consent_granted(job_seeker_id, kind)
    return bool(has_consent(job_seeker_id, kind))


def do_not_disclose_paths(job_seeker_id: str) -> set[str]:
    """FR-106 field paths, resolved through the profile slice when it is present."""
    for module in ("dreamjob.db.repositories.profiles", "dreamjob.db.repositories.campaigns"):
        try:
            owner = __import__(module, fromlist=["do_not_disclose_paths"])
            return set(owner.do_not_disclose_paths(job_seeker_id))
        except (ImportError, AttributeError):
            continue
    return repo.disclosure_paths(job_seeker_id)


# ---------------------------------------------------------------------------
# Input assembly
# ---------------------------------------------------------------------------


def _profile_payload(version: dict, do_not_disclose: set[str]) -> dict[str, Any]:
    """The trusted half of the input, redacted for egress (CR-410, FR-106)."""
    payload = {
        "source_note": version.get("source_note"),
        "sections": version.get("sections") or {},
        "dream_job_statement": version.get("dream_job_statement"),
    }
    return redact(payload, do_not_disclose)


def _findings_payload(findings: list[dict]) -> list[dict[str, Any]]:
    out = []
    for finding in findings:
        facts = finding.get("extracted_facts") or {}
        out.append(
            {
                "url": finding["url"],
                "title": finding.get("title"),
                "classification": finding.get("classification"),
                "identity_score": finding.get("identity_score"),
                "page_kind": facts.get("page_kind"),
                "facts": facts.get("facts", []),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Output validation (FR-125, RK-02, CR-405)
# ---------------------------------------------------------------------------


def _as_statement(item: Any, block: str, index: int) -> dict[str, Any]:
    if isinstance(item, str):
        item = {"text": item}
    elif not isinstance(item, dict):
        item = {"text": str(item)}
    statement = dict(item)
    statement["id"] = str(statement.get("id") or f"{block}:{index}")
    statement["text"] = str(statement.get("text") or statement.get("statement") or "").strip()
    statement.pop("statement", None)
    return statement


def normalise_composite(data: dict, allowed_urls: set[str]) -> dict[str, Any]:
    """Give every statement an id and a verifiable provenance ref (FR-125).

    Model output is not trusted to be well-formed: ids go missing, blocks come
    back as bare strings, and a citation may name a URL that was never fetched.
    Everything is repaired here rather than at read time, so the stored row is
    the contract other slices can rely on.
    """
    raw_refs = data.get("evidence_refs")
    refs: dict[str, Any] = dict(raw_refs) if isinstance(raw_refs, dict) else {}
    out: dict[str, Any] = {}
    ids: list[str] = []

    narrative = data.get("narrative")
    if isinstance(narrative, dict):
        narrative_id = str(narrative.get("id") or "narrative:1")
        out["narrative"] = str(narrative.get("text") or "").strip()
    else:
        narrative_id = "narrative:1"
        out["narrative"] = str(narrative or "").strip()
    if out["narrative"]:
        ids.append(narrative_id)

    seniority = data.get("seniority")
    if isinstance(seniority, str):
        seniority = {"level": seniority, "text": seniority}
    if isinstance(seniority, dict) and (seniority.get("level") or seniority.get("text")):
        seniority = dict(seniority)
        seniority["id"] = str(seniority.get("id") or "seniority:1")
        out["seniority"] = seniority
        ids.append(seniority["id"])
    else:
        out["seniority"] = None

    for block in LIST_BLOCKS:
        items = data.get(block) or []
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            items = []
        statements = []
        for index, item in enumerate(items, start=1):
            statement = _as_statement(item, block, index)
            if not statement["text"]:
                continue
            statements.append(statement)
            ids.append(statement["id"])
        out[block] = statements

    clean_refs: dict[str, Any] = {}
    unsupported: list[str] = []
    for statement_id in ids:
        ref = refs.get(statement_id)
        if not isinstance(ref, dict):
            clean_refs[statement_id] = dict(UNSUPPORTED)
            unsupported.append(statement_id)
            continue
        source_type = str(ref.get("source_type") or "").strip().lower()
        source_ref = ref.get("source_ref")
        if source_type not in SOURCE_TYPES or source_type == "unsupported":
            clean_refs[statement_id] = dict(UNSUPPORTED)
            unsupported.append(statement_id)
            continue
        if source_type == "web" and str(source_ref) not in allowed_urls:
            # A citation to a page this job seeker's enrichment never confirmed.
            clean_refs[statement_id] = {
                "source_type": "unsupported",
                "source_ref": None,
                "quote": None,
                "rejected_ref": source_ref,
            }
            unsupported.append(statement_id)
            continue
        clean_refs[statement_id] = {
            "source_type": source_type,
            "source_ref": source_ref,
            "quote": ref.get("quote"),
        }

    clean_refs["_meta"] = {
        "statement_ids": ids,
        "unsupported": unsupported,
        "source_types": sorted(SOURCE_TYPES),
    }
    out["evidence_refs"] = clean_refs
    return out


def statements(composite: dict) -> list[dict[str, Any]]:
    """Flatten a stored composite into (id, block, text, source) rows for the UI."""
    refs = composite.get("evidence_refs") or {}
    rows: list[dict[str, Any]] = []
    if composite.get("narrative"):
        rows.append(
            {
                "id": "narrative:1",
                "block": "narrative",
                "text": composite["narrative"],
                "source": refs.get("narrative:1", UNSUPPORTED),
            }
        )
    seniority = composite.get("seniority")
    if isinstance(seniority, dict):
        rows.append(
            {
                "id": seniority.get("id", "seniority:1"),
                "block": "seniority",
                "text": seniority.get("text") or seniority.get("level") or "",
                "source": refs.get(seniority.get("id", "seniority:1"), UNSUPPORTED),
            }
        )
    for block in LIST_BLOCKS:
        for statement in composite.get(block) or []:
            rows.append(
                {
                    "id": statement.get("id"),
                    "block": block,
                    "text": statement.get("text", ""),
                    "source": refs.get(statement.get("id"), UNSUPPORTED),
                    "extra": {
                        k: v for k, v in statement.items() if k not in ("id", "text")
                    },
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Structural fallback (no LLM available)
# ---------------------------------------------------------------------------


def _iter_experience(sections: Any) -> list[dict]:
    if not isinstance(sections, dict):
        return []
    for key in ("experience", "Experience", "positions", "work_experience"):
        value = sections.get(key)
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
    return []


def structural_composite(version: dict, findings: list[dict]) -> dict[str, Any]:
    """Assemble a composite without an LLM (NFR-104 degradation, CR-405).

    Nothing here is generated: each statement restates one field of the profile
    and cites the field path it came from.  Thinner than the synthesised
    version, and honest about it.
    """
    sections = version.get("sections") or {}
    source_note = (version.get("source_note") or "profile").lower()
    source_type = "linkedin_export" if "linkedin" in source_note else (
        "cv" if source_note == "cv" else "profile"
    )

    trajectory: list[dict] = []
    domains: list[dict] = []
    refs: dict[str, Any] = {}
    employers: list[str] = []

    for index, role in enumerate(_iter_experience(sections), start=1):
        title = str(role.get("title") or role.get("position") or "").strip()
        company = str(role.get("company") or role.get("organisation") or "").strip()
        period = " - ".join(
            str(role[k]) for k in ("start", "end") if role.get(k)
        ) or str(role.get("period") or "")
        if not (title or company):
            continue
        statement_id = f"career_trajectory:{index}"
        trajectory.append(
            {"id": statement_id, "text": " at ".join(p for p in (title, company) if p)
             + (f" ({period})" if period else "")}
        )
        refs[statement_id] = {
            "source_type": source_type,
            "source_ref": f"experience[{index - 1}]",
            "quote": title or company,
        }
        if company:
            employers.append(company)

    skills = sections.get("skills") if isinstance(sections, dict) else None
    competencies: list[dict] = []
    if isinstance(skills, list):
        for index, skill in enumerate(skills[:20], start=1):
            label = skill if isinstance(skill, str) else str(
                (skill or {}).get("name") or (skill or {}).get("label") or ""
            )
            if not label.strip():
                continue
            statement_id = f"core_competencies:{index}"
            competencies.append({"id": statement_id, "text": label.strip()})
            refs[statement_id] = {
                "source_type": source_type,
                "source_ref": f"skills[{index - 1}]",
                "quote": label.strip(),
            }

    footprint: list[dict] = []
    for index, finding in enumerate(findings, start=1):
        statement_id = f"public_footprint:{index}"
        footprint.append(
            {
                "id": statement_id,
                "text": finding.get("title") or finding["url"],
                "kind": (finding.get("extracted_facts") or {}).get("page_kind", "site"),
                "url": finding["url"],
            }
        )
        refs[statement_id] = {
            "source_type": "web",
            "source_ref": finding["url"],
            "quote": finding.get("title"),
        }

    narrative = ""
    if trajectory:
        narrative = (
            f"{len(trajectory)} recorded role(s)"
            + (f" across {len(set(employers))} employer(s)" if employers else "")
            + ". Assembled directly from the profile; no narrative was generated."
        )
        refs["narrative:1"] = {
            "source_type": source_type,
            "source_ref": "experience",
            "quote": None,
        }

    return normalise_composite(
        {
            "narrative": {"id": "narrative:1", "text": narrative},
            "career_trajectory": trajectory,
            "core_competencies": competencies,
            "domains": domains,
            "public_footprint": footprint,
            "evidence_refs": refs,
        },
        allowed_urls={f["url"] for f in findings},
    )


def _has_synthesised_content(blocks: dict[str, Any]) -> bool:
    """Did the model answer carry at least one statement (CR-405)?

    A schema-valid but empty object parses like any other; without this check
    it would be stored as a normal LLM run and leave every screen and
    directive looking at a composite with nothing in it.
    """
    if str(blocks.get("narrative") or "").strip():
        return True
    if blocks.get("seniority"):
        return True
    return any(blocks.get(block) for block in LIST_BLOCKS)


def _structural_fallback(version: dict, findings: list[dict], reason: str) -> dict[str, Any]:
    """Structural composite, with the degraded path recorded on it (CR-405).

    The mode and the reason travel with the row and with the returned blocks:
    an assembly the synthesis never wrote otherwise looks like any other run,
    and the screens and the directive proposal have no way to tell that the
    function families and competencies they are reading are missing because the
    model answer was unusable.  ``reason`` is bounded because it names a model
    failure, not the profile.
    """
    blocks = structural_composite(version, findings)
    blocks["_generation"] = {"mode": "structural", "reason": reason}
    blocks["synthesis_note"] = (
        "Synthesised profile unavailable; assembled structurally from the profile. "
        f"Reason: {reason}"
    )
    return blocks


# ---------------------------------------------------------------------------
# Build and persist
# ---------------------------------------------------------------------------


def build_composite(
    job_seeker_id: str,
    *,
    profile_version_id: str | None = None,
    persona_id: str | None = None,
    campaign_id: str | None = None,
    include_enrichment: bool | None = None,
    language: str = "en",
    llm: LLMClient | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Synthesise and store one composite profile version (FR-121, FR-125)."""
    version = (
        repo.get_profile_version(job_seeker_id, profile_version_id)
        if profile_version_id
        else repo.latest_profile_version(job_seeker_id, persona_id)
    )
    if version is None:
        raise ProfileMissing(f"job seeker {job_seeker_id} has no profile version yet")

    if include_enrichment is None:
        include_enrichment = enrichment_allowed(job_seeker_id)
    findings = repo.usable_findings(job_seeker_id) if include_enrichment else []
    allowed_urls = {f["url"] for f in findings}

    if llm is None:
        llm = default_llm(job_seeker_id, campaign_id)

    if llm is None:
        blocks = _structural_fallback(version, findings, "no LLM configured")
    else:
        # CR-410: consent is checked at the last moment before egress, not at
        # the start of the pipeline, so a consent withdrawn mid-run still bites.
        if not has_consent_for(job_seeker_id, "llm_transfer"):
            raise ConsentRequired(
                "Consent for transferring profile data to the LLM provider (CR-410) "
                "has not been recorded for this job seeker."
            )
        blocks = _synthesise(
            llm,
            job_seeker_id=job_seeker_id,
            version=version,
            findings=findings,
            allowed_urls=allowed_urls,
            include_enrichment=include_enrichment,
            language=language,
        )

    generation = blocks.get("_generation") or {"mode": "llm"}
    synthesis_note = blocks.get("synthesis_note")
    if not persist:
        return blocks

    # The mode travels with the row, not just with this response: a composite
    # assembled without the synthesis reads like any other one on the screen,
    # and the job seeker has to be told that what they are looking at is the
    # degraded assembly (NFR-104, CR-405) rather than the synthesis they asked
    # for.  ``evidence_refs._meta`` already round-trips as JSON, so this needs
    # no new column.
    refs = dict(blocks.get("evidence_refs") or {})
    meta = {**(refs.get("_meta") or {}), "generation": generation}
    if synthesis_note:
        meta["synthesis_note"] = synthesis_note
    refs["_meta"] = meta

    composite_id = repo.insert_composite(
        job_seeker_id,
        {
            "persona_id": persona_id or version.get("persona_id"),
            "profile_version_id": version["id"],
            # The version is allocated inside the insert (see insert_composite).
            "narrative": blocks.get("narrative"),
            "career_trajectory": blocks.get("career_trajectory"),
            "core_competencies": blocks.get("core_competencies"),
            "adjacent_competencies": blocks.get("adjacent_competencies"),
            "seniority": blocks.get("seniority"),
            "domains": blocks.get("domains"),
            "achievements": blocks.get("achievements"),
            "public_footprint": blocks.get("public_footprint"),
            "inferred_preferences": blocks.get("inferred_preferences"),
            "constraints": blocks.get("constraints"),
            "evidence_refs": refs,
            "edited_by_user": 0,
        },
    )
    stored = repo.get_composite(job_seeker_id, composite_id) or {}
    stored["_generation"] = generation
    if synthesis_note:
        stored["synthesis_note"] = synthesis_note
    return stored


def _synthesise(
    llm: LLMClient,
    *,
    job_seeker_id: str,
    version: dict,
    findings: list[dict],
    allowed_urls: set[str],
    include_enrichment: bool,
    language: str,
) -> dict[str, Any]:
    prompt = load_prompt("composite_profile")
    system, user = prompt.render(
        language=language,
        enrichment_state="enabled" if include_enrichment else "disabled (FR-126)",
    )
    do_not_disclose = do_not_disclose_paths(job_seeker_id)
    profile_payload = _profile_payload(version, do_not_disclose)
    findings_payload = redact(_findings_payload(findings), do_not_disclose)

    # NFR-205: the profile blob is parsed from an uploaded PDF/DOCX and the
    # findings from scraped pages - both are data, never instructions.
    untrusted = {"profile": json.dumps(profile_payload, ensure_ascii=False, default=str)}
    if findings_payload:
        untrusted["web_findings"] = json.dumps(findings_payload, ensure_ascii=False, default=str)

    try:
        data = llm.complete_json(
            "profile.composite",
            system=system,
            user=user,
            untrusted=untrusted,
            entity_type="composite_profile",
            entity_id=version["id"],
            prompt_template=prompt.name,
            prompt_version=prompt.version,
            # The reasoning model spends this budget on its private reasoning
            # *and* on the answer, and this is the longest answer the product
            # asks for: ten blocks of statements plus an evidence_refs entry
            # for every one of them.  Measured over e2e runs, one synthesis of
            # this profile spent 21k tokens reasoning and 6k answering, and a
            # 24,000 budget truncated the JSON mid-statement - which is not a
            # failed call but a *silent* one, because the fallback below then
            # produces a composite with no narrative in it.  Syntheses of the
            # same profile have spent 17,012, 23,693 and 26,481 tokens; 48,000
            # leaves the worst of those at 55% of the budget.
            max_tokens=48_000,
        )
    except (LLMError, BudgetExhausted) as exc:
        reason = str(exc)[:300]
        log.warning("Composite synthesis failed (%s); falling back to structural", reason)
        return _structural_fallback(version, findings, reason)

    if not isinstance(data, dict):
        reason = f"the model returned {type(data).__name__} for a JSON object"
        log.warning("Composite synthesis unusable (%s); falling back to structural", reason)
        return _structural_fallback(version, findings, reason)

    blocks = normalise_composite(data, allowed_urls)
    if not _has_synthesised_content(blocks):
        reason = "the model answer contained no usable statements"
        log.warning("Composite synthesis unusable (%s); falling back to structural", reason)
        return _structural_fallback(version, findings, reason)
    blocks["_generation"] = {
        "mode": "llm",
        "prompt": prompt.name,
        "prompt_version": prompt.version,
    }
    return blocks


def edit_composite(job_seeker_id: str, composite_id: str, patch: dict) -> dict | None:
    """Apply the job seeker's edits (FR-125).

    Blocks are re-normalised so hand-edited statements keep their ids and their
    provenance refs, and any statement added by hand is attributed to the job
    seeker rather than silently inheriting a citation.
    """
    current = repo.get_composite(job_seeker_id, composite_id)
    if current is None:
        return None

    merged: dict[str, Any] = {block: current.get(block) for block in LIST_BLOCKS}
    merged["narrative"] = current.get("narrative")
    merged["seniority"] = current.get("seniority")
    merged["evidence_refs"] = {
        k: v for k, v in (current.get("evidence_refs") or {}).items() if k != "_meta"
    }
    for key, value in patch.items():
        if key in LIST_BLOCKS or key in ("narrative", "seniority"):
            merged[key] = value
        elif key == "evidence_refs" and isinstance(value, dict):
            merged["evidence_refs"].update(value)

    known_ids = set(merged["evidence_refs"])
    normalised = normalise_composite(merged, allowed_urls=_finding_urls(job_seeker_id))
    for statement_id, ref in normalised["evidence_refs"].items():
        if statement_id == "_meta":
            continue
        if ref["source_type"] == "unsupported" and statement_id not in known_ids:
            normalised["evidence_refs"][statement_id] = {
                "source_type": "user_input",
                "source_ref": "edited by the job seeker",
                "quote": None,
            }

    return repo.update_composite(
        job_seeker_id,
        composite_id,
        {
            "narrative": normalised.get("narrative"),
            "seniority": normalised.get("seniority"),
            "evidence_refs": normalised.get("evidence_refs"),
            **{block: normalised.get(block) for block in LIST_BLOCKS},
        },
    )


def _finding_urls(job_seeker_id: str) -> set[str]:
    return {f["url"] for f in repo.usable_findings(job_seeker_id)}
