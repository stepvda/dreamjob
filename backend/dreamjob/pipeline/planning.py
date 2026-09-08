"""Campaign planning: which sources, and what to ask each of them (FR-161..166).

Planning is the step between "here is my dream job" and "go and collect".  It
runs in four passes:

1. **Select** (FR-161, FR-164) - read the source catalogue and keep only the
   sources whose coverage, terms of service and access method fit this search.
   A Benelux search never plans an Asian job board, and every rejection is kept
   with its reason so the plan screen can explain itself.
2. **Translate** (FR-162) - the LLM turns the composite profile, the directives
   and the dream job model into each surviving source's *native* query form:
   keyword strings and filter parameters for a job board, a board slug for an
   ATS, a sector code for a registry, crawl seeds for a website crawler, search
   URLs for LinkedIn.  Sources are translated in small batches, so a failed
   call costs only its own sources.  When the model is unavailable or the token
   budget is nearly spent, a deterministic query is built from the directives
   instead (NFR-104).
3. **Estimate and persist** (FR-163, FR-166) - pages, duration and cost per
   source, written as ``source_plan_item`` rows that the user reviews, edits or
   excludes before anything is fetched, and that every collected record is
   later linked back to.
4. **Reuse** (FR-342) - the knowledge base is queried and plan items that are
   already covered by fresh data are skipped, with the saving reported.

The LinkedIn network strategy (FR-165) is planned as its own item: first-degree
contacts, people in similar roles and alumni of the same employers and schools,
under an explicit maximum number of profiles and companies.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dreamjob.config import get_settings
from dreamjob.db.connection import from_json, utcnow
from dreamjob.db.repositories import campaigns as repo
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError, redact
from dreamjob.pipeline import knowledge_base

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "llm" / "prompts" / "campaign_plan.md"

# Per-source ceilings applied before the user ever sees the plan (FR-186).
DEFAULT_CAPS: dict[str, int] = {
    "max_pages": 200,
    "max_pages_per_source": 20,
    "max_companies": 200,
    "max_people": 100,
    "max_duration_seconds": 4 * 3600,
}

# FR-165: the network crawl is slow and intrusive, so it is capped hard.
LINKEDIN_NETWORK_ADAPTER = "linkedin_network"
DEFAULT_NETWORK_CAPS = {"max_profiles": 50, "max_companies": 40}

# Cost model for the extraction the collected pages will trigger (FR-163).
PLAN_MAX_TOKENS = 6_000
PLAN_BATCH_SIZE = 3
TOKENS_PER_PAGE_IN = 2_500
TOKENS_PER_PAGE_OUT = 400
SECONDS_PER_PAGE_OVERHEAD = 4

_COUNTRY_BY_NAME = {
    "belgium": "BE", "belgie": "BE", "belgique": "BE", "belgië": "BE",
    "netherlands": "NL", "nederland": "NL", "holland": "NL", "the netherlands": "NL",
    "luxembourg": "LU", "luxemburg": "LU",
    "france": "FR", "germany": "DE", "deutschland": "DE", "duitsland": "DE",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB", "england": "GB",
    "ireland": "IE", "spain": "ES", "portugal": "PT", "italy": "IT",
    "denmark": "DK", "sweden": "SE", "norway": "NO", "finland": "FI",
    "poland": "PL", "czechia": "CZ", "austria": "AT", "switzerland": "CH",
    "united states": "US", "usa": "US", "canada": "CA",
}
_COUNTRY_GROUPS = {
    "benelux": ["BE", "NL", "LU"],
    "dach": ["DE", "AT", "CH"],
    "nordics": ["DK", "SE", "NO", "FI"],
}
_ISO2 = re.compile(r"^[A-Z]{2}$")

_KEYWORD_KEYS = (
    "roles", "target_roles", "titles", "job_titles", "functions", "function_families",
    "keywords", "role_families", "specialisations", "specializations", "domains",
)
_SKILL_KEYS = ("skills", "technologies", "tools", "core_competencies", "competencies")


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------


@dataclass
class PlannedSource:
    """One source plan item before it is persisted (FR-162, FR-163)."""

    adapter_key: str
    native_query: dict[str, Any]
    rationale: str = ""
    estimated_pages: int = 1
    estimated_seconds: int = 0
    estimated_cost_eur: float = 0.0
    caps: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict:
        return {
            "adapter_key": self.adapter_key,
            "native_query": self.native_query,
            "rationale": self.rationale,
            "caps": self.caps,
            "estimated_pages": self.estimated_pages,
            "estimated_seconds": self.estimated_seconds,
            "estimated_cost_eur": round(self.estimated_cost_eur, 4),
        }


@dataclass
class SourceSelection:
    """The outcome of FR-164 source selection, rejections included."""

    selected: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    def reject(self, entry: dict, reason: str) -> None:
        self.rejected.append(
            {
                "adapter_key": entry.get("adapter_key"),
                "display_name": entry.get("display_name"),
                "source_type": entry.get("source_type"),
                "reason": reason,
            }
        )


# ---------------------------------------------------------------------------
# Directive reading
# ---------------------------------------------------------------------------


def _walk(node: Any, keys: tuple[str, ...], out: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in keys:
                _flatten_strings(value, out)
            else:
                _walk(value, keys, out)
    elif isinstance(node, list):
        for value in node:
            _walk(value, keys, out)


def _flatten_strings(node: Any, out: list[str]) -> None:
    if isinstance(node, str):
        text = node.strip()
        if text:
            out.append(text)
    elif isinstance(node, dict):
        for value in node.values():
            _flatten_strings(value, out)
    elif isinstance(node, list):
        for value in node:
            _flatten_strings(value, out)


def _unique(values: list[str], limit: int | None = None) -> list[str]:
    seen: dict[str, str] = {}
    for value in values:
        key = value.strip().lower()
        if key and key not in seen:
            seen[key] = value.strip()
    result = list(seen.values())
    return result[:limit] if limit else result


def directive_json(directives: dict | None, do_not_disclose: set[str] | None = None) -> dict:
    """The directive set as nested JSON, ready to ground the prompt (FR-141..146).

    Directives are the job seeker's own instructions, but they still leave the
    machine, so do-not-disclose and special-category fields are dropped first
    (CR-410).  Under discretion mode the excluded employers are reduced to a
    count: the planner needs to know the list exists, not who is on it, and
    naming a current employer to the model is the one thing FR-385 exists to
    prevent.
    """
    if not directives:
        return {}
    out: dict[str, Any] = {}
    for column in ("job_content", "company_type", "location", "work_arrangement", "compensation"):
        value = from_json(directives.get(column), None)
        if value:
            out[column] = value
    if directives.get("notes_to_ai"):
        out["notes_to_ai"] = directives["notes_to_ai"]
    out["spontaneous_only"] = bool(directives.get("spontaneous_only"))
    out = redact(out, do_not_disclose)
    if directives.get("discretion_mode"):
        excluded = from_json(directives.get("discretion_excluded_companies"), []) or []
        out["discretion_mode"] = True
        out["excluded_company_count"] = len(excluded) if isinstance(excluded, list) else 0
    return out


def target_countries(directives: dict | None) -> list[str]:
    """ISO-2 countries this campaign targets (FR-164).

    An empty result means "no geographic constraint stated", which lets a
    globally-covering source through but never a regional one from elsewhere.
    """
    location = from_json((directives or {}).get("location"), {}) or {}
    words: list[str] = []
    _flatten_strings(location, words)
    countries: list[str] = []
    for word in words:
        text = word.strip()
        if _ISO2.match(text.upper()) and text.upper() not in {"EU", "UE"}:
            countries.append(text.upper())
            continue
        lowered = text.lower()
        if lowered in _COUNTRY_GROUPS:
            countries.extend(_COUNTRY_GROUPS[lowered])
        elif lowered in _COUNTRY_BY_NAME:
            countries.append(_COUNTRY_BY_NAME[lowered])
        else:
            for name, code in _COUNTRY_BY_NAME.items():
                if re.search(rf"\b{re.escape(name)}\b", lowered):
                    countries.append(code)
    return _unique(countries)


def campaign_keywords(
    directives: dict | None, dream: dict | None, composite: dict | None
) -> list[str]:
    """Role words to search with, taken only from what the user actually stated (CR-405)."""
    found: list[str] = []
    _walk(directive_json(directives), _KEYWORD_KEYS, found)
    for source in (dream, composite):
        if not source:
            continue
        for column in (
            "target_roles", "role_families", "responsibilities", "core_competencies",
            "adjacent_competencies", "domains",
        ):
            _flatten_strings(from_json(source.get(column), None), found)
    return _unique(found, 25)


def campaign_skills(composite: dict | None) -> list[str]:
    found: list[str] = []
    if composite:
        for column in ("core_competencies", "adjacent_competencies"):
            _flatten_strings(from_json(composite.get(column), None), found)
        _walk(from_json(composite.get("evidence_refs"), {}) or {}, _SKILL_KEYS, found)
    return _unique(found, 20)


def _locations(directives: dict | None) -> list[str]:
    location = from_json((directives or {}).get("location"), {}) or {}
    words: list[str] = []
    _flatten_strings(location, words)
    return _unique([w for w in words if len(w) > 2], 8)


def employers_and_schools(
    composite: dict | None, profile: dict | None
) -> tuple[list[str], list[str]]:
    """Seeds for the FR-165 alumni strategy, read from the profile only."""
    employers: list[str] = []
    schools: list[str] = []
    if composite:
        trajectory = from_json(composite.get("career_trajectory"), None)
        _walk(trajectory, ("company", "employer"), employers)
    sections = from_json((profile or {}).get("sections"), {}) or {}
    for key, value in sections.items() if isinstance(sections, dict) else []:
        lowered = key.lower()
        if "experience" in lowered or "position" in lowered:
            _walk(value, ("company", "employer", "organisation", "organization"), employers)
        if "education" in lowered or "school" in lowered:
            _walk(value, ("school", "institution", "university"), schools)
    return _unique(employers, 12), _unique(schools, 6)


# ---------------------------------------------------------------------------
# Source selection (FR-161, FR-164)
# ---------------------------------------------------------------------------


def select_sources(
    catalogue: list[dict],
    *,
    countries: list[str],
    spontaneous_only: bool = False,
    linkedin_allowed: bool = True,
    industries: list[str] | None = None,
) -> SourceSelection:
    """Keep only the sources whose catalogue metadata fits this search (FR-164)."""
    selection = SourceSelection()
    wanted = {c.upper() for c in countries}
    industries = [i.lower() for i in (industries or [])]

    for entry in catalogue:
        if not entry.get("enabled", 1):
            selection.reject(entry, "disabled in the source catalogue")
            continue
        if entry.get("tos_status") == "prohibited" and not entry.get("acknowledged_at"):
            selection.reject(entry, "terms of service prohibit automated access (IR-101)")
            continue
        if entry.get("requires_ack") and not entry.get("acknowledged_at"):
            selection.reject(entry, "awaiting administrator acknowledgement (IR-101)")
            continue

        source_type = entry.get("source_type") or ""
        if spontaneous_only and source_type in ("job_board", "ats"):
            selection.reject(entry, "spontaneous-application mode skips vacancy sources (FR-149)")
            continue
        if source_type == "linkedin" and not linkedin_allowed:
            selection.reject(entry, "no consent recorded for LinkedIn automation (CR-403)")
            continue

        coverage = [c.upper() for c in (entry.get("coverage_countries") or [])]
        if wanted and coverage and not (wanted & set(coverage)):
            selection.reject(
                entry,
                "coverage " + "/".join(sorted(coverage)) + " does not include "
                + "/".join(sorted(wanted)),
            )
            continue

        covered_industries = [i.lower() for i in (entry.get("coverage_industries") or [])]
        if industries and covered_industries and not (set(industries) & set(covered_industries)):
            selection.reject(entry, "industry coverage does not match the directives")
            continue

        selection.selected.append(entry)
    return selection


# ---------------------------------------------------------------------------
# Native query generation (FR-162)
# ---------------------------------------------------------------------------


def load_prompt(path: Path = PROMPT_PATH) -> tuple[dict, str, str]:
    """Read a versioned prompt template: front matter, system part, user part (NFR-602)."""
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    if text.startswith("---"):
        _, front, text = text.split("---", 2)
        for line in front.strip().splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip()
    system, _, user = text.partition("## User")
    system = system.replace("## System", "").strip()
    return meta, system, user.strip()


def render(template: str, values: dict[str, Any]) -> str:
    """``{{name}}`` substitution - JSON braces in the template stay untouched."""
    out = template
    for key, value in values.items():
        rendered = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
        )
        out = out.replace("{{" + key + "}}", rendered)
    return out


def _native_query_shape(entry: dict) -> dict:
    """What the LLM is told this source's query looks like (FR-162)."""
    source_type = entry.get("source_type")
    shapes: dict[str, dict] = {
        "job_board": {"keywords": ["str"], "location": "str", "filters": {"...": "..."}, "page": 1},
        "ats": {"vendor": "str", "board_slugs": ["str"], "keywords": ["str"]},
        "directory": {"query": "str", "sector_codes": ["str"], "country": "str"},
        "registry": {"country": "str", "legal_ids": ["str"], "sector_codes": ["str"]},
        "website": {"crawl_seeds": ["https://..."], "paths": ["/careers", "/jobs"]},
        "linkedin": {"search_urls": ["https://www.linkedin.com/search/results/..."]},
        "compensation": {"job_titles": ["str"], "region": "str"},
        "news": {"query": "str", "companies": ["str"], "since_days": 90},
        "events": {"topics": ["str"], "region": "str"},
    }
    return shapes.get(source_type or "", {"query": "str"})


def _source_brief(entry: dict, per_source_pages: int) -> dict:
    return {
        "adapter_key": entry["adapter_key"],
        "display_name": entry.get("display_name"),
        "source_type": entry.get("source_type"),
        "coverage_countries": entry.get("coverage_countries") or "global",
        "query_capabilities": entry.get("query_capabilities") or {},
        "native_query_shape": _native_query_shape(entry),
        "max_pages": per_source_pages,
    }


def fallback_query(
    entry: dict, keywords: list[str], locations: list[str], countries: list[str]
) -> dict:
    """Deterministic native query used when the LLM is unavailable (NFR-104)."""
    source_type = entry.get("source_type")
    capabilities = entry.get("query_capabilities") or {}
    query: dict[str, Any] = {}
    if source_type in ("job_board", "ats", "linkedin", "compensation"):
        query["keywords"] = keywords[:6] or ["*"]
    else:
        query["query"] = " OR ".join(keywords[:4]) if keywords else ""
    if capabilities.get("location_filter") or source_type in ("job_board", "directory", "registry"):
        query["location"] = locations[0] if locations else ""
        query["countries"] = countries
    if source_type == "website":
        query = {"crawl_seeds": [], "paths": ["/careers", "/jobs", "/vacatures", "/emplois"]}
    query["page"] = 1
    return query


def generate_native_queries(
    llm: LLMClient,
    entries: list[dict],
    *,
    directives: dict,
    dream: dict,
    composite_public: dict,
    caps: dict,
    per_source_pages: int,
) -> dict[str, dict]:
    """Generate every selected source's native query (FR-162).

    Sources are planned in small batches: a reasoning model spends its output
    budget thinking, and a batch that is too large returns nothing at all.
    Batching also means a failed call costs only its own sources, which fall
    back to the deterministic query below instead of losing the whole plan.
    """
    out: dict[str, dict] = {}
    for start in range(0, len(entries), PLAN_BATCH_SIZE):
        batch = entries[start : start + PLAN_BATCH_SIZE]
        keys = [e["adapter_key"] for e in batch]
        # Translating a catalogue entry into its native query is a structured
        # rewrite, not an open-ended analysis: the cheap model answers it
        # reliably in JSON mode, while the configured reasoning model spends
        # its whole output budget deliberating and returns nothing.  The strong
        # model is therefore the fallback, not the first attempt.
        for prefer_strong in (False, True):
            try:
                planned = _plan_batch(
                    llm,
                    batch,
                    directives=directives,
                    dream=dream,
                    composite_public=composite_public,
                    caps=caps,
                    per_source_pages=per_source_pages,
                    prefer_strong=prefer_strong,
                )
            except BudgetExhausted:
                raise
            except (LLMError, ValueError, KeyError, TypeError) as exc:
                log.warning("Planning batch %s failed on attempt %s: %s", keys, prefer_strong, exc)
                planned = {}
            if planned:
                out.update(planned)
                break
        else:
            log.warning("Sources %s fall back to deterministic queries", keys)
    return out


# The model may name the list differently from one run to the next.
_PLAN_LIST_KEYS = ("plans", "plan", "plan_entries", "entries", "sources", "results", "items")


def _plan_batch(
    llm: LLMClient,
    entries: list[dict],
    *,
    directives: dict,
    dream: dict,
    composite_public: dict,
    caps: dict,
    per_source_pages: int,
    prefer_strong: bool | None = None,
) -> dict[str, dict]:
    """One LLM call for a handful of sources.

    The directives are the job seeker's own instructions and stay in the
    instruction channel; the composite profile is derived from uploaded and
    enriched material and is passed as untrusted data (NFR-205).
    """
    meta, system, user_template = load_prompt()
    briefs = [_source_brief(e, per_source_pages) for e in entries]
    user = render(
        user_template,
        {"directives": directives, "dream_job": dream, "sources": briefs, "caps": caps},
    )
    data = llm.complete_json(
        "plan.campaign",
        system=system,
        user=user,
        untrusted={"composite_profile": json.dumps(composite_public, ensure_ascii=False)},
        schema_hint=(
            '{"plans": [{"adapter_key": str, "native_query": object, "rationale": str, '
            '"estimated_pages": int}]}'
        ),
        prompt_template=meta.get("id", "campaign_plan"),
        prompt_version=meta.get("version", "1"),
        entity_type="campaign",
        entity_id=llm.campaign_id,
        # The reasoning model returns an empty answer when it is also given a
        # json_object response format, so JSON is requested in the prompt and
        # parsed from the text.  The ceiling covers reasoning plus answer.
        prefer_strong=prefer_strong,
        json_mode=prefer_strong is False,
        max_tokens=PLAN_MAX_TOKENS,
    )
    return _read_plans(data, {e["adapter_key"] for e in entries}, per_source_pages)


def _read_plans(data: Any, known: set[str], per_source_pages: int) -> dict[str, dict]:
    """Validate the model's answer before any of it is persisted (NFR-205)."""
    plans: Any = data
    if isinstance(data, dict):
        plans = next((data[k] for k in _PLAN_LIST_KEYS if isinstance(data.get(k), list)), None)
        if plans is None:
            lists = [v for v in data.values() if isinstance(v, list)]
            plans = lists[0] if len(lists) == 1 else []
    out: dict[str, dict] = {}
    for plan in plans or []:
        if not isinstance(plan, dict):
            continue
        key = plan.get("adapter_key")
        if key not in known:
            log.info("Discarding plan for unknown adapter %r", key)
            continue
        native = plan.get("native_query")
        if not isinstance(native, dict) or not native:
            continue
        out[key] = {
            "native_query": native,
            "rationale": str(plan.get("rationale") or "")[:1000],
            "estimated_pages": _clamp_int(plan.get("estimated_pages"), 1, per_source_pages),
        }
    return out


def _clamp_int(value: Any, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(high, number))


# ---------------------------------------------------------------------------
# Estimation (FR-163)
# ---------------------------------------------------------------------------


def estimate(entry: dict, pages: int) -> tuple[int, float]:
    """Duration in seconds and cost in EUR for fetching ``pages`` from a source."""
    settings = get_settings()
    rps = float(entry.get("rate_limit_rps") or settings.per_domain_rps or 0.5)
    per_page = (1.0 / rps if rps > 0 else 2.0) + SECONDS_PER_PAGE_OVERHEAD
    if entry.get("access_method") == "browser":
        per_page += (settings.browser_min_delay_ms + settings.browser_max_delay_ms) / 2000.0
    seconds = int(pages * per_page)
    api_cost = pages * float(entry.get("cost_per_call_eur") or 0.0)
    llm_cost = pages * (
        TOKENS_PER_PAGE_IN / 1e6 * settings.llm_cost_per_1m_input_eur
        + TOKENS_PER_PAGE_OUT / 1e6 * settings.llm_cost_per_1m_output_eur
    )
    return seconds, round(api_cost + llm_cost, 4)


# ---------------------------------------------------------------------------
# LinkedIn network strategy (FR-165)
# ---------------------------------------------------------------------------


def linkedin_network_plan(
    *,
    adapter_key: str,
    keywords: list[str],
    employers: list[str],
    schools: list[str],
    locations: list[str],
    caps: dict,
) -> PlannedSource:
    """First-degree, similar-role and alumni crawling under an explicit cap (FR-165)."""
    max_profiles = int(caps.get("max_people") or DEFAULT_NETWORK_CAPS["max_profiles"])
    max_companies = int(caps.get("max_companies") or DEFAULT_NETWORK_CAPS["max_companies"])
    max_profiles = min(max_profiles, DEFAULT_NETWORK_CAPS["max_profiles"] * 4)
    titles = keywords[:5]
    facets = [
        {"facet": "first_degree", "description": "existing first-degree contacts"},
        {
            "facet": "similar_roles",
            "description": "people holding the target roles",
            "titles": titles,
        },
        {
            "facet": "alumni_employers",
            "description": "alumni of former employers",
            "seeds": employers,
        },
        {"facet": "alumni_schools", "description": "alumni of the same schools", "seeds": schools},
    ]
    search_urls = [
        "https://www.linkedin.com/search/results/people/?keywords="
        + re.sub(r"\s+", "%20", title)
        for title in titles
    ]
    native_query = {
        "strategy": "network",
        "facets": facets,
        "search_urls": search_urls,
        "locations": locations,
        "max_profiles": max_profiles,
        "max_companies": max_companies,
    }
    pages = max(1, min(10, max_profiles // 10))
    return PlannedSource(
        adapter_key=adapter_key,
        native_query=native_query,
        rationale=(
            f"Map up to {max_profiles} profiles and {max_companies} companies through "
            "first-degree contacts, people in the target roles and alumni of the same "
            "employers and schools (FR-165)."
        ),
        estimated_pages=pages,
        estimated_seconds=pages * 45,
        estimated_cost_eur=0.0,
        caps={"max_profiles": max_profiles, "max_companies": max_companies},
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def resolve_caps(campaign: dict, directives: dict | None) -> dict:
    """Campaign caps: defaults, then anything the directives or the user set (FR-186)."""
    caps = dict(DEFAULT_CAPS)
    stated = from_json((directives or {}).get("job_content"), {}) or {}
    if isinstance(stated, dict):
        for key in DEFAULT_CAPS:
            if isinstance(stated.get(key), int):
                caps[key] = int(stated[key])
    existing = campaign.get("caps") or {}
    if isinstance(existing, dict):
        for key, value in existing.items():
            if isinstance(value, (int, float)):
                caps[key] = int(value)
    return caps


def generate_plan(
    campaign_id: str,
    job_seeker_id: str,
    *,
    use_llm: bool = True,
    llm: LLMClient | None = None,
    assess_knowledge_base: bool = True,
) -> dict:
    """Produce, persist and summarise a campaign plan (FR-161..166, FR-342)."""
    campaign = repo.get_campaign(campaign_id, job_seeker_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id} for this job seeker")
    if campaign.get("status") == "running":
        # Re-planning deletes the plan items collected records point back to
        # (FR-166), so it waits until collection has stopped.
        raise ValueError("Pause or cancel the running collection before re-planning")

    inputs = repo.load_planning_inputs(campaign)
    directives = inputs["directives"] or {}
    composite = inputs["composite_profile"] or {}
    dream = inputs["dream_job_model"] or {}
    profile = inputs["profile_version"] or {}

    countries = target_countries(directives)
    keywords = campaign_keywords(directives, dream, composite)
    locations = _locations(directives)
    caps = resolve_caps(campaign, directives)
    linkedin_allowed = repo.has_consent(job_seeker_id, "linkedin_automation")

    catalogue = repo.list_catalogue(enabled_only=True)
    selection = select_sources(
        catalogue,
        countries=countries,
        spontaneous_only=bool(directives.get("spontaneous_only")),
        linkedin_allowed=linkedin_allowed,
    )

    per_source_pages = int(caps.get("max_pages_per_source", 20))
    native_by_key: dict[str, dict] = {}
    llm_used = False
    degraded_reason: str | None = None

    if selection.selected and use_llm:
        client = llm or LLMClient(campaign_id=campaign_id, job_seeker_id=job_seeker_id)
        if client.budget.should_degrade():
            degraded_reason = "token budget nearly exhausted; deterministic queries used (NFR-104)"
        else:
            try:
                native_by_key = generate_native_queries(
                    client,
                    selection.selected,
                    directives=directive_json(directives, inputs["do_not_disclose"]),
                    dream=_public_dream(dream, inputs["do_not_disclose"]),
                    composite_public=_public_composite(composite, inputs["do_not_disclose"]),
                    caps=caps,
                    per_source_pages=per_source_pages,
                )
                llm_used = bool(native_by_key)
                if len(native_by_key) < len(selection.selected):
                    degraded_reason = (
                        f"{len(selection.selected) - len(native_by_key)} of "
                        f"{len(selection.selected)} sources fell back to deterministic queries"
                    )
            except BudgetExhausted as exc:
                degraded_reason = f"token budget exhausted: {exc} (NFR-104)"
            except (LLMError, ValueError, KeyError) as exc:
                log.warning("Falling back to deterministic queries: %s", exc)
                degraded_reason = f"LLM unavailable: {exc}"

    planned: list[PlannedSource] = []
    for entry in selection.selected:
        generated = native_by_key.get(entry["adapter_key"])
        if generated:
            native_query = generated["native_query"]
            area = "/".join(countries) or "the stated geography"
            rationale = generated["rationale"] or f"Selected from the source catalogue for {area}."
            pages = generated["estimated_pages"]
        else:
            native_query = fallback_query(entry, keywords, locations, countries)
            rationale = (
                f"{entry.get('display_name') or entry['adapter_key']} covers "
                f"{'/'.join(countries) or 'the requested area'}; deterministic keyword query."
            )
            pages = min(per_source_pages, 3)
        native_query.setdefault("countries", countries)
        seconds, cost = estimate(entry, pages)
        planned.append(
            PlannedSource(
                adapter_key=entry["adapter_key"],
                native_query=native_query,
                rationale=rationale,
                estimated_pages=pages,
                estimated_seconds=seconds,
                estimated_cost_eur=cost,
                caps={"max_pages": pages, "records_per_page": _per_page(entry)},
            )
        )

    network_entry = next(
        (e for e in selection.selected if e.get("source_type") == "linkedin"), None
    )
    if network_entry and linkedin_allowed:
        employers, schools = employers_and_schools(composite, profile)
        planned.append(
            linkedin_network_plan(
                adapter_key=network_entry.get("adapter_key") or LINKEDIN_NETWORK_ADAPTER,
                keywords=keywords,
                employers=employers,
                schools=schools,
                locations=locations,
                caps=caps,
            )
        )

    item_ids = persist_plan(campaign_id, planned)
    repo.update_campaign(
        campaign_id,
        {
            "caps": caps,
            "status": "planned" if planned else campaign.get("status", "draft"),
            "stage": "planning",
        },
    )
    repo.record_audit(
        "campaign.plan_generated",
        job_seeker_id=job_seeker_id,
        entity_type="campaign",
        entity_id=campaign_id,
        detail={"sources": len(planned), "llm_used": llm_used, "degraded": degraded_reason},
    )

    if assess_knowledge_base and planned:
        knowledge_base.assess_reuse(campaign_id, countries=countries)

    summary = plan_summary(campaign_id, job_seeker_id)
    summary.update(
        {
            "plan_item_ids": item_ids,
            "rejected_sources": selection.rejected,
            "countries": countries,
            "keywords": keywords,
            "llm_used": llm_used,
            "degraded_reason": degraded_reason,
        }
    )
    return summary


def _per_page(entry: dict) -> int:
    capabilities = entry.get("query_capabilities") or {}
    try:
        return max(1, min(100, int(capabilities.get("max_results_per_query"))))
    except (TypeError, ValueError):
        return knowledge_base.DEFAULT_RECORDS_PER_PAGE


def _public_composite(composite: dict, do_not_disclose: set[str]) -> dict:
    """CR-410/FR-106: strip do-not-disclose and special-category fields before egress."""
    payload = {
        key: from_json(composite.get(key), composite.get(key))
        for key in (
            "narrative", "career_trajectory", "core_competencies", "adjacent_competencies",
            "seniority", "domains", "achievements", "inferred_preferences", "constraints",
        )
        if composite.get(key)
    }
    return redact(payload, do_not_disclose)


def _public_dream(dream: dict, do_not_disclose: set[str] | None = None) -> dict:
    """CR-410: the dream job model leaves the machine too, so it is redacted too."""
    payload = {
        key: from_json(dream.get(key), dream.get(key))
        for key in (
            "statement", "target_roles", "role_families", "responsibilities",
            "company_characteristics", "culture_values", "deal_breakers",
        )
        if dream.get(key)
    }
    return redact(payload, do_not_disclose)


def persist_plan(campaign_id: str, planned: list[PlannedSource]) -> list[str]:
    """Replace the campaign's plan, preserving exclusions and provenance (FR-163, FR-166).

    A source that survives a re-plan keeps its plan-item row rather than being
    deleted and re-inserted: the provenance rows written by an earlier
    collection point at that id, and FR-166 asks for that link to hold.  A
    source the new plan drops is deleted only when nothing points at it; when
    something does, the row is retired as ``skipped`` so it stops being
    collected without breaking the records it already produced.
    """
    existing = {_plan_key(i["adapter_key"], i.get("native_query")): i
                for i in repo.list_plan_items(campaign_id)}
    referenced = repo.plan_items_with_provenance(campaign_id)
    ids: list[str] = []
    for item in planned:
        row = item.to_row()
        previous = existing.pop(_plan_key(item.adapter_key, item.native_query), None)
        if previous is None:
            ids.append(repo.insert_plan_item(campaign_id, row))
            continue
        row["excluded_by_user"] = 1 if previous.get("excluded_by_user") else 0
        row["status"] = "planned"
        row["last_error"] = None
        repo.update_plan_item(previous["id"], row)
        ids.append(previous["id"])

    retired = [i["id"] for i in existing.values() if i["id"] in referenced]
    for retired_id in retired:
        repo.update_plan_item(
            retired_id, {"status": "skipped", "last_error": "no longer part of the plan"}
        )
    repo.delete_plan_items(campaign_id, keep_ids=[*ids, *retired])
    return ids


def _plan_key(adapter_key: str, native_query: Any) -> tuple[str, str]:
    strategy = (native_query or {}).get("strategy", "") if isinstance(native_query, dict) else ""
    return adapter_key, str(strategy)


def plan_summary(campaign_id: str, job_seeker_id: str) -> dict:
    """The review screen's payload: sources, queries, volume, duration, cost (FR-163)."""
    campaign = repo.get_campaign(campaign_id, job_seeker_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id} for this job seeker")
    items = repo.list_plan_items(campaign_id)
    active = [i for i in items if not i["excluded_by_user"] and i["status"] != "skipped"]
    catalogue = {c["adapter_key"]: c for c in repo.list_catalogue(enabled_only=False)}
    for item in items:
        entry = catalogue.get(item["adapter_key"], {})
        item["display_name"] = entry.get("display_name") or item["adapter_key"]
        item["source_type"] = entry.get("source_type")
        item["access_method"] = entry.get("access_method")
        item["tos_status"] = entry.get("tos_status")
    return {
        "campaign_id": campaign_id,
        "status": campaign.get("status"),
        "stage": campaign.get("stage"),
        "caps": campaign.get("caps") or DEFAULT_CAPS,
        "generated_at": utcnow(),
        "totals": {
            "sources": len(active),
            "sources_excluded": sum(1 for i in items if i["excluded_by_user"]),
            "sources_skipped_by_reuse": sum(1 for i in items if i["status"] == "skipped"),
            "estimated_pages": sum(int(i["estimated_pages"] or 0) for i in active),
            "estimated_seconds": sum(int(i["estimated_seconds"] or 0) for i in active),
            "estimated_cost_eur": round(
                sum(float(i["estimated_cost_eur"] or 0.0) for i in active), 4
            ),
        },
        "reuse_report": campaign.get("reuse_report"),
        "items": items,
    }
