"""The standardised company profile (FR-222, FR-223, FR-226, FR-384, NFR-205, NFR-402).

One company, one schema, whatever the source.  :func:`build_profile` crawls the
company's own site (FR-221), reads what can be read deterministically, and asks
the model to synthesise the rest; :func:`standardised_profile` assembles the
view FR-222 prescribes - identity and legal identifiers, business summary,
sector codes, size, locations, structure and departments, key people,
references, financial summary, hiring signals, competitors and sources.

Three requirements shape the code more than the schema does:

* **NFR-205** - a company website is exactly the injection vector the
  requirement is about.  Crawled text never reaches the instruction string; it
  goes through the ``untrusted`` blocks of :meth:`LLMClient.complete_json`, one
  block per page kind, and anything in it that addressed the model comes back
  in ``anomalies`` rather than changing what was extracted.
* **NFR-402** - every field carries a confidence and a provenance link.  The
  model is required to attribute each value to the URL it read it on; that URL
  is resolved back to the ``raw_document`` the crawler stored, and one
  ``provenance`` row per field path is written.  Fields below
  :data:`LOW_CONFIDENCE` are listed for the UI to flag.
* **FR-226** - profiles are shared and refreshed incrementally.  A profile
  younger than the configured staleness age is reused untouched; only an older
  one is re-crawled.  Nothing here stores a job seeker id (FR-344).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from dreamjob.adapters.website import crawler
from dreamjob.config import get_settings
from dreamjob.db.connection import from_json, utcnow
from dreamjob.db.repositories import companies as repo
from dreamjob.db.repositories import knowledge as kb_repo
from dreamjob.egress.client import EgressClient
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError
from dreamjob.pipeline import knowledge_base
from dreamjob.pipeline.enrichment import load_prompt

log = logging.getLogger(__name__)

PROMPT_NAME = "company_profile"
LLM_TASK = "extract.company"

#: NFR-402: fields below this confidence are flagged in the profile view.
LOW_CONFIDENCE = 0.5

#: FR-223: the departmental map is expected of companies of this size upwards.
DEPARTMENT_MAP_MIN_FTE = 150

MAX_JOB_AD_CHARS = 8_000
MAX_REVIEW_CHARS = 6_000

SIZE_BANDS: tuple[tuple[int, str], ...] = (
    (10, "1-10"),
    (50, "11-50"),
    (200, "51-200"),
    (500, "201-500"),
    (1000, "501-1000"),
    (5000, "1001-5000"),
)

STAGES = {"startup", "scaleup", "established", "listed", "public", "nonprofit"}
OWNERSHIPS = {"founder_led", "pe_backed", "subsidiary", "cooperative", "family_owned", "listed"}

#: Identity columns a website crawl may fill but must never overwrite: a legal
#: identifier from a registry beats one read off a page footer (DR-101).
PROTECTED_IDENTITY = ("legal_id", "legal_id_type", "vat_number", "country", "jurisdiction")

#: The fixed schema of FR-222, as it maps onto the ``company`` columns.
PROFILE_SECTIONS: tuple[str, ...] = (
    "identity",
    "business_summary",
    "products_services",
    "markets",
    "sector_codes",
    "size",
    "locations",
    "structure",
    "key_people",
    "references",
    "tech_stack",
    "values_culture",
    "financial_summary",
    "hiring_signals",
    "competitors",
    "news",
    "sources",
)

_JSON_COLUMNS = (
    "products_services", "markets", "sector_codes", "locations", "structure",
    "key_people", "reference_customers", "tech_stack", "values_culture", "news",
)


# ---------------------------------------------------------------------------
# FR-226: reuse or re-crawl
# ---------------------------------------------------------------------------


def profile_age_days(company: dict) -> float | None:
    stamp = company.get("refreshed_at") or company.get("collected_at")
    if not stamp:
        return None
    try:
        collected = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if collected.tzinfo is None:
        collected = collected.replace(tzinfo=UTC)
    return (datetime.now(UTC) - collected).total_seconds() / 86_400.0


def needs_refresh(company: dict, max_age_days: int | None = None) -> bool:
    """FR-226: older than the configured age means re-crawl, otherwise reuse.

    The age comes from the shared staleness policy (FR-343), so an
    administrator changing "company websites: 90 days" changes this too.
    """
    if not company.get("business_summary"):
        return True
    if max_age_days is None:
        return knowledge_base.is_stale(
            "company", company.get("refreshed_at") or company.get("collected_at")
        )
    age = profile_age_days(company)
    return age is None or age >= max_age_days


def size_band_for(fte: Any) -> str | None:
    try:
        value = int(float(fte))
    except (TypeError, ValueError):
        return None
    for ceiling, band in SIZE_BANDS:
        if value <= ceiling:
            return band
    return "5000+"


def home_url_for(company: dict) -> str | None:
    domain = (company.get("domain") or "").strip()
    if domain:
        if domain.startswith(("http://", "https://")):
            return domain
        return f"https://{domain}"
    source = (company.get("source") or "").strip()
    if source.startswith(("http://", "https://")):
        return source
    return None


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class ProfileOutcome:
    """What one profiling pass did, for the job dashboard and the API."""

    company_id: str
    reused: bool
    pages_crawled: int = 0
    fields_written: list[str] = field(default_factory=list)
    confidence: dict[str, float] = field(default_factory=dict)
    anomalies: list[str] = field(default_factory=list)
    llm_used: bool = False
    feeds: list[str] = field(default_factory=list)
    reason: str = ""
    #: FR-224: companies the pages themselves named as peers or comparables.
    competitor_mentions: list[dict] = field(default_factory=list)
    #: The crawl itself, so a caller that goes on to detect signals reads the
    #: pages this pass already fetched instead of fetching the site again.
    crawl: crawler.CrawlResult | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "reused": self.reused,
            "pages_crawled": self.pages_crawled,
            "fields_written": self.fields_written,
            "confidence": self.confidence,
            "anomalies": self.anomalies,
            "llm_used": self.llm_used,
            "feeds": self.feeds,
            "reason": self.reason,
            "competitor_mentions": self.competitor_mentions,
        }


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def _value(node: Any, key: str = "text") -> Any:
    """Unwrap the ``{"value", "confidence", "source"}`` envelopes the prompt uses."""
    if isinstance(node, dict):
        for candidate in (key, "text", "value", "name", "url"):
            if candidate in node:
                return node[candidate]
    return node


def _confidence(node: Any, default: float = 0.5) -> float:
    if isinstance(node, dict):
        try:
            return max(0.0, min(1.0, float(node.get("confidence", default))))
        except (TypeError, ValueError):
            return default
    return default


def _source(node: Any) -> str | None:
    if isinstance(node, dict):
        url = node.get("source") or node.get("url")
        return str(url) if url else None
    return None


def _list_confidence(items: Any, default: float = 0.5) -> float:
    """A list's confidence is the mean of its entries', so a mixed list reads as mixed."""
    if not isinstance(items, list) or not items:
        return 0.0
    scores = [_confidence(i, default) for i in items if isinstance(i, dict)]
    return round(sum(scores) / len(scores), 3) if scores else default


def _list_sources(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    return [s for s in (_source(i) for i in items) if s]


def _clean_list(items: Any, limit: int = 40) -> list[dict]:
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict) and any(i.values())][:limit]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(str(value).replace(",", "").replace(" ", "")))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The profiling pass
# ---------------------------------------------------------------------------


async def build_profile(
    company: dict,
    *,
    force: bool = False,
    max_age_days: int | None = None,
    max_pages: int = crawler.DEFAULT_MAX_PAGES,
    max_depth: int = crawler.DEFAULT_MAX_DEPTH,
    use_llm: bool = True,
    campaign_id: str | None = None,
    job_seeker_id: str | None = None,
    source_plan_item_id: str | None = None,
    employer_reviews: list[str] | None = None,
    egress: EgressClient | None = None,
) -> ProfileOutcome:
    """Crawl, synthesise and store one standardised company profile.

    ``job_seeker_id`` is used only for the LLM audit trail (FR-364); nothing it
    touches is written to a shared row (FR-344).
    """
    company_id = company["id"]

    if not force and not needs_refresh(company, max_age_days):
        return ProfileOutcome(
            company_id=company_id,
            reused=True,
            reason="profile is within the configured staleness age (FR-226)",
        )

    home = home_url_for(company)
    if not home:
        return ProfileOutcome(
            company_id=company_id,
            reused=False,
            reason="no website known for this company; nothing to crawl",
        )

    crawl = await crawler.crawl_site(
        home, max_pages=max_pages, max_depth=max_depth, egress=egress
    )
    if not crawl.pages:
        return ProfileOutcome(
            company_id=company_id,
            reused=False,
            reason=f"crawl of {home} returned no readable pages",
            feeds=crawl.feeds,
        )

    repo.clear_crawled_pages(company_id)
    for page in crawl.pages:
        repo.record_crawled_page(
            company_id,
            page.kind,
            raw_document_id=page.raw_document_id,
            confidence=0.9 if page.status_code == 200 else 0.5,
            source_plan_item_id=source_plan_item_id,
        )

    document_by_url = {p.url: p.raw_document_id for p in crawl.pages}
    deterministic = crawler.identity_from_jsonld(crawl.jsonld)

    synthesis: dict[str, Any] = {}
    llm_used = False
    if use_llm:
        synthesis, llm_used = _synthesise(
            company,
            crawl,
            employer_reviews=employer_reviews,
            campaign_id=campaign_id,
            job_seeker_id=job_seeker_id,
        )

    values, meta = _assemble(company, crawl, deterministic, synthesis, document_by_url)
    if values:
        kb_repo.update_company(company_id, values)
    if meta:
        # ``record_field_provenance`` replaces this adapter's whole set, so
        # writing an empty one deletes the confidences and sources of fields
        # that are still on the row.  A degraded run (no LLM, budget spent,
        # LLMError - see ``_synthesise``) produces exactly that empty set, and
        # NFR-402 would go blank for a company that is perfectly well profiled.
        repo.record_field_provenance(company_id, meta, source_plan_item_id=source_plan_item_id)
    elif values:
        log.info(
            "Profile of %s was written without per-field provenance (degraded synthesis); "
            "the provenance already on record is kept (NFR-402)",
            company_id,
        )

    anomalies = [str(a)[:300] for a in (synthesis.get("anomalies") or []) if a][:10]
    return ProfileOutcome(
        company_id=company_id,
        reused=False,
        pages_crawled=len(crawl.pages),
        fields_written=sorted(values),
        confidence={k: round(v["confidence"], 3) for k, v in meta.items()},
        anomalies=anomalies,
        llm_used=llm_used,
        feeds=crawl.feeds,
        reason=f"crawled {len(crawl.pages)} pages of {crawl.domain}",
        competitor_mentions=_competitor_mentions(synthesis),
        crawl=crawl,
    )


def _competitor_mentions(synthesis: dict) -> list[dict]:
    """FR-224: peers the company's own pages name, for the competitor pass.

    A company named on a customers or products page as a comparable provider is
    an observation about the market, not a guess, so it is handed to
    :mod:`dreamjob.pipeline.competitors` rather than left in the raw synthesis.
    """
    out: list[dict] = []
    for entry in _clean_list(synthesis.get("competitor_mentions"), limit=15):
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        out.append(
            {
                "name": name[:200],
                "basis": str(entry.get("basis") or "press").lower(),
                "evidence": str(entry.get("evidence") or "")[:300],
                "source": _source(entry),
                "confidence": _confidence(entry, 0.5),
            }
        )
    return out


def _synthesise(
    company: dict,
    crawl: crawler.CrawlResult,
    *,
    employer_reviews: list[str] | None,
    campaign_id: str | None,
    job_seeker_id: str | None,
) -> tuple[dict[str, Any], bool]:
    """LLM synthesis over the crawled pages, passed as untrusted data (NFR-205)."""
    settings = get_settings()
    if not settings.deepseek_api_key and not settings.local_llm_base_url:
        log.info("No LLM configured; company %s profiled deterministically", company.get("name"))
        return {}, False

    llm = LLMClient(campaign_id=campaign_id, job_seeker_id=job_seeker_id)
    if llm.budget.should_degrade():
        log.info("Token budget nearly spent; skipping LLM synthesis for %s", company.get("name"))
        return {}, False

    untrusted = dict(crawl.text_blocks())
    ads = _job_ad_block(company["id"])
    if ads:
        untrusted["job_ads"] = ads
    if employer_reviews:
        untrusted["employer_reviews"] = "\n\n".join(employer_reviews)[:MAX_REVIEW_CHARS]
    if not untrusted:
        return {}, False

    template = load_prompt(PROMPT_NAME)
    system, user = template.render(
        language="English",
        company_name=company.get("name") or crawl.domain,
        domain=company.get("domain") or crawl.domain,
    )
    try:
        data = llm.complete_json(
            LLM_TASK,
            system=system,
            user=user,
            untrusted=untrusted,
            prefer_strong=False,
            # 6000 was the whole of the model's answer budget and the answers
            # were landing at 5,585 on average - so a third of them were cut off
            # mid-JSON, complete_json raised, and the company was stored with no
            # business summary and therefore no sector, size, stage or
            # trajectory: everything scoring reads about an employer.  The
            # schema this prompt asks for does not fit in 6,000 tokens.
            max_tokens=8000,
            entity_type="company",
            entity_id=company["id"],
            prompt_template=template.name,
            prompt_version=template.version,
        )
    except (BudgetExhausted, LLMError) as exc:
        # NFR-104: the deterministic profile is still worth storing.
        log.warning("Company synthesis unavailable for %s: %s", company.get("name"), exc)
        return {}, False
    if not isinstance(data, dict):
        log.warning("Company synthesis returned %s, not an object", type(data).__name__)
        return {}, False
    return data, True


def _job_ad_block(company_id: str) -> str:
    """FR-384: the language of a company's own job adverts is a culture signal."""
    chunks: list[str] = []
    total = 0
    for advert in repo.recent_vacancy_texts(company_id, limit=8):
        body = re.sub(r"\s+", " ", advert.get("description") or "")[:1500]
        if not body:
            continue
        title = advert.get("title") or "Vacancy"
        chunk = f"### {title}\nURL: {advert.get('source_url') or ''}\n{body}\n"
        if total + len(chunk) > MAX_JOB_AD_CHARS:
            break
        chunks.append(chunk)
        total += len(chunk)
    return "\n".join(chunks)


def _assemble(
    company: dict,
    crawl: crawler.CrawlResult,
    deterministic: dict[str, Any],
    synthesis: dict[str, Any],
    document_by_url: dict[str, str | None],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Merge deterministic finds and LLM synthesis onto the ``company`` columns.

    Returns the column values to write and, per field path, the confidence and
    the raw document the value came from (NFR-402).
    """
    values: dict[str, Any] = {}
    meta: dict[str, dict[str, Any]] = {}

    def put(column: str, value: Any, confidence: float, source: str | None) -> None:
        if value in (None, "", [], {}):
            return
        values[column] = value
        meta[column] = {
            "confidence": round(max(0.0, min(1.0, confidence)), 3),
            "raw_document_id": document_by_url.get(source or "") if source else None,
        }

    # --- deterministic: schema.org and the crawl itself --------------------
    for column in PROTECTED_IDENTITY:
        if deterministic.get(column) and not company.get(column):
            put(column, deterministic[column], 0.85, crawl.home_url)
    if deterministic.get("locations"):
        put("locations", deterministic["locations"], 0.85, crawl.home_url)
    if deterministic.get("size_fte"):
        put("size_fte", deterministic["size_fte"], 0.8, crawl.home_url)

    careers = crawl.careers_url or _value(synthesis.get("careers_url"), "url")
    if careers:
        put("careers_url", str(careers)[:500], 0.9 if crawl.careers_url else 0.6, careers)
    if crawl.ats_vendor:
        put("ats_vendor", crawl.ats_vendor, 0.9, crawl.home_url)
        if crawl.ats_slug:
            put("ats_slug", crawl.ats_slug, 0.9, crawl.home_url)
    if not company.get("domain") and crawl.domain:
        put("domain", crawl.domain, 0.95, crawl.home_url)

    # --- synthesis ---------------------------------------------------------
    summary = synthesis.get("business_summary")
    if summary:
        put(
            "business_summary",
            str(_value(summary))[:6000],
            _confidence(summary, 0.7),
            _source(summary),
        )

    for key, column in (
        ("products_services", "products_services"),
        ("markets", "markets"),
        ("sector_codes", "sector_codes"),
        ("key_people", "key_people"),
        ("reference_customers", "reference_customers"),
        ("tech_stack", "tech_stack"),
    ):
        items = _clean_list(synthesis.get(key))
        if items:
            put(column, items, _list_confidence(items, 0.6), (_list_sources(items) or [None])[0])

    locations = _clean_list(synthesis.get("locations"))
    if locations and "locations" not in values:
        put(
            "locations",
            locations,
            _list_confidence(locations, 0.6),
            (_list_sources(locations) or [None])[0],
        )

    size = synthesis.get("size") or {}
    if isinstance(size, dict):
        fte = _int_or_none(size.get("fte"))
        if fte and "size_fte" not in values:
            put("size_fte", fte, _confidence(size, 0.5), _source(size))
        known_fte = fte or values.get("size_fte") or company.get("size_fte")
        band = size.get("band") or size_band_for(known_fte)
        if band:
            put("size_band", str(band), _confidence(size, 0.5), _source(size))
        if str(size.get("stage") or "").lower() in STAGES:
            put("stage", str(size["stage"]).lower(), _confidence(size, 0.5), _source(size))
        if str(size.get("ownership") or "").lower() in OWNERSHIPS:
            put("ownership", str(size["ownership"]).lower(), _confidence(size, 0.5), _source(size))

    structure = _structure(synthesis, company, values)
    if structure:
        put("structure", structure, structure.get("confidence", 0.5), structure.get("source"))

    culture = _values_culture(synthesis, crawl)
    if culture:
        put("values_culture", culture, culture.get("confidence", 0.5), crawl.home_url)

    identity = synthesis.get("identity") or {}
    if isinstance(identity, dict):
        for key, column in (
            ("legal_id", "legal_id"),
            ("legal_id_type", "legal_id_type"),
            ("vat_number", "vat_number"),
            ("country", "country"),
            ("jurisdiction", "jurisdiction"),
        ):
            if identity.get(key) and not company.get(column) and column not in values:
                value = str(identity[key])[:60]
                put(column, value.upper() if column in ("country", "jurisdiction") else value,
                    min(0.7, _confidence(identity, 0.5)), _source(identity))

    news = _news_items(synthesis, crawl)
    if news:
        put("news", news, 0.6, crawl.home_url)

    if values:
        values["refreshed_at"] = utcnow()
        values["source"] = crawl.home_url
        values["access_method"] = "http"
        values["confidence"] = round(
            sum(m["confidence"] for m in meta.values()) / max(1, len(meta)), 3
        )
    return values, meta


def _structure(synthesis: dict, company: dict, pending: dict) -> dict[str, Any] | None:
    """FR-223: the departmental map, with the head of each unit where known."""
    raw = synthesis.get("structure")
    if not isinstance(raw, dict):
        return None
    units = _clean_list(raw.get("business_units"), limit=30)
    functions = _clean_list(raw.get("functions"), limit=30)
    if not units and not functions:
        return None
    fte = pending.get("size_fte") or company.get("size_fte")
    heads = [u for u in units if isinstance(u.get("head"), dict) and u["head"].get("name")]
    return {
        "business_units": units,
        "functions": functions,
        "notes": str(raw.get("notes") or "")[:1000],
        "heads_known": len(heads),
        "expected_for_size": bool(fte and int(fte) >= DEPARTMENT_MAP_MIN_FTE),
        "confidence": _list_confidence(units or functions, 0.5),
        "source": (_list_sources(units) or _list_sources(functions) or [None])[0],
    }


def _values_culture(synthesis: dict, crawl: crawler.CrawlResult) -> dict[str, Any] | None:
    """FR-384: values and working style as structured, comparable cues.

    The intelligence slice compares these against the dream-job statement, so
    each cue keeps its evidence and the material it was derived from.
    """
    raw = synthesis.get("values_culture")
    if not isinstance(raw, dict):
        return None
    stated = _clean_list(raw.get("stated_values"), limit=20)
    style = _clean_list(raw.get("working_style"), limit=20)
    leadership = _clean_list(raw.get("leadership_statements"), limit=10)
    ads = _clean_list(raw.get("job_ad_language"), limit=15)
    reviews = _clean_list(raw.get("employer_review_themes"), limit=15)
    if not any((stated, style, leadership, ads, reviews)):
        return None

    derived: list[str] = []
    if stated or style or crawl.by_kind("values"):
        derived.append("website")
    if leadership:
        derived.append("leadership")
    if ads:
        derived.append("job_ads")
    if reviews:
        derived.append("reviews")

    return {
        "stated_values": stated,
        "working_style": style,
        "leadership_statements": leadership,
        "job_ad_language": ads,
        "employer_review_themes": reviews,
        "derived_from": derived,
        "confidence": round(
            (_list_confidence(stated, 0.6) + _list_confidence(style, 0.5)) / 2 or 0.5, 3
        ),
    }


def _news_items(synthesis: dict, crawl: crawler.CrawlResult) -> list[dict]:
    """Headline news for the profile view; the dated signals live in ``hiring_signal``."""
    items = []
    for indicator in _clean_list(synthesis.get("hiring_indicators"), limit=15):
        items.append(
            {
                "title": str(indicator.get("observation") or "")[:300],
                "signal_type": indicator.get("signal_type"),
                "occurred_at": indicator.get("occurred_at"),
                "url": indicator.get("source"),
            }
        )
    for page in crawl.by_kind("news")[:5]:
        items.append({"title": page.title[:300], "url": page.url, "signal_type": None})
    return [i for i in items if i.get("title")][:20]


# ---------------------------------------------------------------------------
# FR-222: the fixed-schema view
# ---------------------------------------------------------------------------


def standardised_profile(company_id: str) -> dict[str, Any] | None:
    """Assemble the fixed-schema profile the UI renders (FR-222, NFR-402)."""
    company = repo.get_company(company_id)
    if company is None:
        return None

    decoded = {c: from_json(company.get(c), None) for c in _JSON_COLUMNS}
    provenance = repo.field_provenance(company_id)
    confidence = {
        row["field_path"]: round(float(row["confidence"] or 0), 3)
        for row in provenance
        if row.get("field_path")
    }
    sources_by_field = {
        row["field_path"]: row.get("source_url") for row in provenance if row.get("field_path")
    }
    pages = repo.crawled_pages(company_id)
    signals = repo.list_signals(company_id, limit=60)

    from dreamjob.pipeline import signals as signals_module  # noqa: PLC0415 - avoids a cycle

    timing = signals_module.recommend_window(company_id, signals=signals).as_dict()

    freshness = company.get("refreshed_at") or company.get("collected_at")
    return {
        "id": company_id,
        "name": company.get("name"),
        "identity": {
            "name": company.get("name"),
            "normalised_name": company.get("normalised_name"),
            "legal_id": company.get("legal_id"),
            "legal_id_type": company.get("legal_id_type"),
            "vat_number": company.get("vat_number"),
            "domain": company.get("domain"),
            "country": company.get("country"),
            "jurisdiction": company.get("jurisdiction"),
            "careers_url": company.get("careers_url"),
            "ats_vendor": company.get("ats_vendor"),
            "ats_slug": company.get("ats_slug"),
        },
        "business_summary": company.get("business_summary"),
        "products_services": decoded["products_services"] or [],
        "markets": decoded["markets"] or [],
        "sector_codes": decoded["sector_codes"] or [],
        "size": {
            "fte": company.get("size_fte"),
            "band": company.get("size_band"),
            "stage": company.get("stage"),
            "ownership": company.get("ownership"),
            "trajectory": company.get("trajectory"),
        },
        "locations": decoded["locations"] or [],
        "structure": decoded["structure"] or {"business_units": [], "functions": []},
        "key_people": decoded["key_people"] or [],
        "references": decoded["reference_customers"] or [],
        "tech_stack": decoded["tech_stack"] or [],
        "values_culture": decoded["values_culture"] or {},
        "financial_summary": financial_summary(company_id),
        "hiring_signals": signals,
        "timing": timing,
        "competitors": _competitor_view(company_id),
        "news": decoded["news"] or [],
        "sources": _sources_view(company, pages),
        "freshness": {
            "collected_at": company.get("collected_at"),
            "refreshed_at": company.get("refreshed_at"),
            "age_days": round(profile_age_days(company) or 0.0, 1),
            "stale": knowledge_base.is_stale("company", freshness),
            "access_method": company.get("access_method"),
            "last_crawl_at": repo.last_crawl_at(company_id),
        },
        "confidence": confidence,
        "field_sources": sources_by_field,
        "low_confidence_fields": sorted(
            p for p, c in confidence.items() if c < LOW_CONFIDENCE
        ),
        "overall_confidence": company.get("confidence"),
    }


def financial_summary(company_id: str) -> dict[str, Any]:
    """The financial block of FR-222, read from whatever the filings slice stored."""
    years = repo.financial_years(company_id, limit=5)
    analysis = repo.financial_analysis(company_id) or {}
    return {
        "years": [
            {
                "fiscal_year": y.get("fiscal_year"),
                "currency": y.get("currency"),
                "revenue": y.get("revenue"),
                "ebit": y.get("ebit"),
                "net_result": y.get("net_result"),
                "equity": y.get("equity"),
                "headcount_fte": y.get("headcount_fte"),
                "is_estimated": bool(y.get("is_estimated")),
            }
            for y in years
        ],
        "trajectory": analysis.get("trajectory"),
        "trajectory_confidence": analysis.get("trajectory_confidence"),
        "revenue_cagr": analysis.get("revenue_cagr"),
        "headcount_cagr": analysis.get("headcount_cagr"),
        "ability_to_pay": analysis.get("ability_to_pay"),
        "investment_capacity": analysis.get("investment_capacity"),
        "available": bool(years),
    }


def _competitor_view(company_id: str) -> list[dict]:
    """One entry per peer, carrying every basis that produced it (FR-224)."""
    from dreamjob.pipeline import competitors  # noqa: PLC0415 - avoids a cycle

    return competitors.stored_suggestions(company_id)


def _sources_view(company: dict, pages: list[dict]) -> list[dict]:
    sources = [
        {
            "url": page.get("url"),
            "kind": (page.get("field_path") or "").removeprefix(repo.PAGE_PROVENANCE_PREFIX),
            "fetched_at": page.get("fetched_at") or page.get("created_at"),
            "access_method": "http",
            "raw_document_id": page.get("raw_document_id"),
        }
        for page in pages
    ]
    if not sources and company.get("source"):
        sources.append(
            {
                "url": company["source"],
                "kind": "website",
                "fetched_at": company.get("refreshed_at") or company.get("collected_at"),
                "access_method": company.get("access_method"),
                "raw_document_id": None,
            }
        )
    return sources


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def refresh_company(
    company_id: str,
    *,
    force: bool = False,
    max_pages: int = crawler.DEFAULT_MAX_PAGES,
    use_llm: bool = True,
    campaign_id: str | None = None,
    job_seeker_id: str | None = None,
) -> dict[str, Any]:
    """Profile, signals and competitors for one company, in that order.

    Signals need the crawl's page inventory and the newsroom feed it found;
    competitors need the sector codes and references the profile produced.  A
    failure in one stage is reported, not fatal - a company with a profile and
    no competitor suggestions is still useful (NFR-104).
    """
    company = repo.get_company(company_id)
    if company is None:
        raise KeyError(f"No such company {company_id}")

    outcome = await build_profile(
        company,
        force=force,
        max_pages=max_pages,
        use_llm=use_llm,
        campaign_id=campaign_id,
        job_seeker_id=job_seeker_id,
    )

    refreshed = repo.get_company(company_id) or company
    report: dict[str, Any] = {"profile": outcome.as_dict()}

    from dreamjob.pipeline import competitors as competitors_module  # noqa: PLC0415
    from dreamjob.pipeline import signals as signals_module  # noqa: PLC0415

    try:
        signals = await signals_module.refresh_signals(
            refreshed,
            pages=list(outcome.crawl.pages) if outcome.crawl else None,
            feeds=outcome.feeds or None,
        )
        report["signals"] = {"count": len(signals)}
    except Exception as exc:  # noqa: BLE001 - a missing newsroom must not fail the refresh
        log.exception("Signal refresh failed for %s", company_id)
        report["signals"] = {"error": str(exc)[:300]}

    try:
        suggestions = competitors_module.suggest_competitors(
            refreshed,
            campaign_id=campaign_id,
            job_seeker_id=job_seeker_id,
            use_llm=use_llm,
            mentioned=outcome.competitor_mentions,
        )
        report["competitors"] = {"count": len(suggestions)}
    except Exception as exc:  # noqa: BLE001
        log.exception("Competitor discovery failed for %s", company_id)
        report["competitors"] = {"error": str(exc)[:300]}

    return report


async def rerun(campaign_id: str, job_seeker_id: str, **options: Any) -> dict[str, Any]:
    """Profile the campaign's companies, with their signals and competitors.

    ``dreamjob.pipeline.collection`` reserves the ``company_profile`` stage for
    this module and resolves it by name; until this function existed the stage
    silently did not exist, ``refresh_company`` had no caller anywhere in the
    backend, and ``hiring_signal`` and ``competitor_link`` stayed empty however
    well collection ran (FR-221..226, NFR-603).
    """
    force = bool(options.get("force", False))
    companies = campaign_companies(
        campaign_id, limit=int(options.get("limit") or 25), include_fresh=force
    )
    if not companies:
        return {
            "companies": 0,
            "profiled": 0,
            "note": "No company is in scope yet; collection has produced none",
        }

    use_llm = bool(options.get("use_llm", True))
    max_pages = int(options.get("max_pages") or crawler.DEFAULT_MAX_PAGES)
    report: dict[str, Any] = {
        "companies": len(companies),
        "profiled": 0,
        "reused": 0,
        "signals": 0,
        "competitors": 0,
        "failed": 0,
    }
    for company_id in companies:
        try:
            outcome = await refresh_company(
                company_id,
                force=force,
                max_pages=max_pages,
                use_llm=use_llm,
                campaign_id=campaign_id,
                job_seeker_id=job_seeker_id,
            )
        except Exception:  # noqa: BLE001 - one company must not stop the stage
            log.exception("Company refresh failed for %s", company_id)
            report["failed"] += 1
            continue
        profile = outcome.get("profile") or {}
        report["reused" if profile.get("reused") else "profiled"] += 1
        report["signals"] += int((outcome.get("signals") or {}).get("count") or 0)
        report["competitors"] += int((outcome.get("competitors") or {}).get("count") or 0)
    return report


def campaign_companies(
    campaign_id: str, limit: int = 25, *, include_fresh: bool = False
) -> list[str]:
    """Companies this campaign collected, then any shared profile that is stale.

    Provenance (FR-166) is the campaign's own answer; it is empty whenever the
    campaign's collection wrote nothing, and a company row created without
    provenance would then never be profiled at all.  So the staleness policy
    (FR-343) fills the rest, and a company that has never been profiled at all
    is included whatever its age - staleness is about *re*-profiling.

    ``include_fresh`` is what a forced re-run needs: the user has asked for the
    crawl to happen again, so the freshness gate is not the one to consult.
    """
    from dreamjob.db.repositories import opportunities as opp_repo  # noqa: PLC0415 - avoids a cycle

    out: list[str] = []
    try:
        out.extend(opp_repo.campaign_company_ids(campaign_id))
    except Exception:  # noqa: BLE001 - provenance is an optimisation, not a gate
        log.exception("Could not read the campaign's company provenance")
    seen = set(out)
    if len(out) < limit:
        for row in due_for_refresh(limit=limit - len(out)):
            if row["id"] not in seen:
                seen.add(row["id"])
                out.append(str(row["id"]))
    if len(out) < limit:
        # A company collected an hour ago is not stale, but it has never been
        # profiled either - and FR-226 staleness is about *re*-profiling.
        # ``build_profile`` still reuses anything that is genuinely fresh.
        for row in repo.companies_in_country(None, limit=200):
            if row["id"] in seen:
                continue
            if not include_fresh and row.get("business_summary"):
                continue
            seen.add(row["id"])
            out.append(str(row["id"]))
            if len(out) >= limit:
                break
    return out[:limit]


def due_for_refresh(limit: int = 50, max_age_days: int | None = None) -> list[dict]:
    """Shared profiles that have aged past the staleness policy (FR-226, FR-343)."""
    if max_age_days is None:
        cutoff = knowledge_base.cutoff_for("company")
    else:
        cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    return kb_repo.stale_rows("company", cutoff, limit=limit)
