"""Glassdoor automation for compensation and employer reviews (FR-203..207, FR-264, FR-265).

Same shape as :mod:`dreamjob.browser.linkedin` - an explicit allowlist built
from the campaign's plan, human pacing, an immediate stop on a challenge page,
and everything tagged ``access_method = 'browser'`` (FR-207) - with one
deliberate difference: **what is collected here is advisory**.

Ratings, review themes and salary ranges are crowd-sourced opinion under
restrictive terms.  They are therefore kept with the campaign that collected
them (in the run's own record) rather than written into the shared knowledge
base as fact, and every payload carries ``advisory: true`` so the scoring and
briefing slices present them as an indication with a source, never as a
number Dream Job asserts.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from dreamjob.browser import pacing as pacing_mod
from dreamjob.browser.session import (
    SITES,
    BrowserSession,
    BrowserUnavailable,
    driver_choice,
    json_ld,
    json_ld_of_type,
    safe_url,
    text_of,
    tidy,
)
from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import browser as repo
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.security.audit import record_audit

try:
    from selectolax.parser import HTMLParser
except ImportError:  # pragma: no cover
    HTMLParser = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

SITE = "glassdoor"
JOB_KIND = "browser"
ADAPTER_KEY = "glassdoor"

#: CR-406 again: Glassdoor pages are slow and defended; keep the list short.
MAX_TARGETS_PER_RUN = 60

_SITE_PROFILE = SITES[SITE]

TARGET_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("employer", re.compile(r"^/Overview/", re.IGNORECASE)),
    ("reviews", re.compile(r"^/Reviews/", re.IGNORECASE)),
    ("salaries", re.compile(r"^/Salary/|^/Salaries/", re.IGNORECASE)),
)


class TargetRefused(RuntimeError):
    """FR-205: only the plan's Glassdoor pages may be opened."""


def terms_warning() -> str:
    return SITES[SITE].terms_warning


# ---------------------------------------------------------------------------
# Allowlist (FR-205)
# ---------------------------------------------------------------------------


def normalise_target_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    host = (parsed.netloc or "").lower()
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return f"https://{host}{path}"


def classify_target(url: str) -> str | None:
    parsed = urlparse(normalise_target_url(url))
    if not _SITE_PROFILE.allows_host(parsed.netloc):
        return None
    for kind, pattern in TARGET_SHAPES:
        if pattern.match(parsed.path or "/"):
            return kind
    return None


def make_target(url: str, *, label: str = "", plan_item_id: str | None = None,
                company_name: str = "") -> pacing_mod.Target:
    kind = classify_target(url)
    if kind is None:
        raise TargetRefused(
            f"{safe_url(url)} is not an allowed Glassdoor page shape "
            "(employer overview, reviews or salaries only) - FR-205"
        )
    return pacing_mod.Target(
        url=normalise_target_url(url),
        kind=kind,
        label=label or company_name or safe_url(url),
        plan_item_id=plan_item_id,
        meta={"company_name": company_name} if company_name else {},
    )


def targets_from_plan_item(item: dict) -> list[pacing_mod.Target]:
    query = item.get("native_query") or {}
    if isinstance(query, str):
        try:
            query = json.loads(query)
        except ValueError:
            query = {}
    out: list[pacing_mod.Target] = []
    for key in ("urls", "employer_urls", "salary_urls", "review_urls"):
        for url in query.get(key) or []:
            try:
                out.append(make_target(str(url), plan_item_id=item.get("id")))
            except TargetRefused as exc:
                log.info("Plan item %s: %s", item.get("id"), exc)
    for entry in query.get("targets") or []:
        if isinstance(entry, dict) and entry.get("url"):
            try:
                out.append(
                    make_target(
                        str(entry["url"]),
                        label=str(entry.get("label") or ""),
                        plan_item_id=item.get("id"),
                        company_name=str(entry.get("company_name") or ""),
                    )
                )
            except TargetRefused as exc:
                log.info("Plan item %s: %s", item.get("id"), exc)
    return out


def glassdoor_plan_items(campaign_id: str) -> list[dict]:
    catalogue = {c["adapter_key"]: c for c in campaign_repo.list_catalogue(enabled_only=False)}
    items = []
    for item in campaign_repo.list_plan_items(campaign_id, include_excluded=False):
        entry = catalogue.get(item["adapter_key"], {})
        if item["adapter_key"] == ADAPTER_KEY or (
            entry.get("source_type") == "compensation" and entry.get("access_method") == "browser"
        ):
            items.append(item)
    return items


def build_targets(campaign_id: str, extra_urls: list[str] | None = None) -> list[pacing_mod.Target]:
    targets: list[pacing_mod.Target] = []
    seen: set[str] = set()
    for item in glassdoor_plan_items(campaign_id):
        for target in targets_from_plan_item(item):
            if target.url not in seen:
                seen.add(target.url)
                targets.append(target)
    for url in extra_urls or []:
        target = make_target(str(url), label="user-selected")
        if target.url not in seen:
            seen.add(target.url)
            targets.append(target)
    return targets[:MAX_TARGETS_PER_RUN]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

_MONEY = re.compile(
    r"(?P<currency>[€$£]|EUR|USD|GBP)?\s*(?P<value>\d[\d.,]*)\s*(?P<suffix>[kK])?"
)
_CURRENCY_CODES = {"€": "EUR", "$": "USD", "£": "GBP"}


def parse_money(text: str) -> tuple[float | None, float | None, str | None]:
    """``"€55K - €70K"`` -> ``(55000.0, 70000.0, "EUR")``.  Pure and testable."""
    if not text:
        return None, None, None
    values: list[float] = []
    currency: str | None = None
    for match in _MONEY.finditer(text):
        raw = match.group("value")
        if not raw or not any(ch.isdigit() for ch in raw):
            continue
        # "70,000" and "70.000" are thousands separators; "70,5" is not a range bound.
        cleaned = raw.replace(".", "").replace(",", "")
        try:
            value = float(cleaned)
        except ValueError:
            continue
        if match.group("suffix"):
            value *= 1000
        values.append(value)
        symbol = match.group("currency")
        if symbol and currency is None:
            currency = _CURRENCY_CODES.get(symbol, symbol.upper())
    if not values:
        return None, None, currency
    return min(values), max(values), currency


@dataclass
class EmployerSnapshot:
    """FR-265: what employees say, kept as an indication (advisory)."""

    url: str
    company_name: str = ""
    rating: float | None = None
    review_count: int | None = None
    recommend_pct: float | None = None
    ceo_approval_pct: float | None = None
    themes: dict[str, list[str]] = field(default_factory=dict)
    collected_at: str = field(default_factory=utcnow)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "employer_reviews",
            "advisory": True,
            "access_method": "browser",  # FR-207
            "source": "glassdoor",
            "url": self.url,
            "company_name": self.company_name,
            "rating": self.rating,
            "review_count": self.review_count,
            "recommend_pct": self.recommend_pct,
            "ceo_approval_pct": self.ceo_approval_pct,
            "themes": self.themes,
            "collected_at": self.collected_at,
        }


@dataclass
class SalarySnapshot:
    """FR-264: a compensation range as reported, never as an assertion."""

    url: str
    company_name: str = ""
    job_title: str = ""
    low: float | None = None
    high: float | None = None
    currency: str | None = None
    sample_size: int | None = None
    collected_at: str = field(default_factory=utcnow)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "compensation",
            "advisory": True,
            "access_method": "browser",  # FR-207
            "source": "glassdoor",
            "url": self.url,
            "company_name": self.company_name,
            "job_title": self.job_title,
            "low": self.low,
            "high": self.high,
            "currency": self.currency,
            "sample_size": self.sample_size,
            "collected_at": self.collected_at,
        }


def parse_employer(html: str, url: str) -> EmployerSnapshot:
    objects = json_ld(html)
    org = json_ld_of_type(objects, "Organization", "Corporation") or {}
    rating_obj = json_ld_of_type(objects, "AggregateRating", "EmployerAggregateRating") or {}
    rating = _float(rating_obj.get("ratingValue")) or _float(
        text_of(html, '[data-test="rating-headline"]', ".rating-headline")
    )
    count = _int(rating_obj.get("ratingCount") or rating_obj.get("reviewCount")) or _int(
        text_of(html, '[data-test="reviewCount"]')
    )
    return EmployerSnapshot(
        url=safe_url(url),
        company_name=tidy(str(org.get("name") or ""))
        or text_of(html, "h1", '[data-test="employer-name"]'),
        rating=rating,
        review_count=count,
        recommend_pct=_float(text_of(html, '[data-test="recommend-percent"]')),
        ceo_approval_pct=_float(text_of(html, '[data-test="ceo-approval"]')),
        themes=parse_review_themes(html),
    )


def parse_review_themes(html: str) -> dict[str, list[str]]:
    """Recurring pros and cons - the themes FR-265 asks to summarise."""
    themes: dict[str, list[str]] = {"pros": [], "cons": []}
    if HTMLParser is None or not html:  # pragma: no cover
        return themes
    tree = HTMLParser(html)
    for key, selectors in (
        ("pros", ('[data-test="pros"]', "span.pros", ".v2__EIReviewDetailsV2__fullWidth")),
        ("cons", ('[data-test="cons"]', "span.cons")),
    ):
        for selector in selectors:
            for node in tree.css(selector):
                text = tidy(node.text())
                if text and text not in themes[key]:
                    themes[key].append(text[:300])
            if themes[key]:
                break
    themes["pros"] = themes["pros"][:10]
    themes["cons"] = themes["cons"][:10]
    return themes


def parse_salaries(html: str, url: str, company_name: str = "") -> list[SalarySnapshot]:
    """Reported ranges per job title (FR-264).  Advisory, with the page as source."""
    out: list[SalarySnapshot] = []
    if HTMLParser is None or not html:  # pragma: no cover
        return out
    tree = HTMLParser(html)
    rows = (
        tree.css('[data-test="salaries-list-item"]')
        or tree.css("div.salarylist-item")
        or tree.css('[data-test="salary-row"]')
    )
    for row in rows:
        title_node = row.css_first('[data-test="job-title"]') or row.css_first("h3")
        range_node = (
            row.css_first('[data-test="salary-range"]')
            or row.css_first('[data-test="amount"]')
            or row
        )
        title = tidy(title_node.text()) if title_node is not None else ""
        low, high, currency = parse_money(tidy(range_node.text()))
        if not title or low is None:
            continue
        out.append(
            SalarySnapshot(
                url=safe_url(url),
                company_name=company_name,
                job_title=title[:200],
                low=low,
                high=high,
                currency=currency,
                sample_size=_int(text_of(row.html or "", '[data-test="salary-count"]')),
            )
        )
    return out[:50]


def _float(value: Any) -> float | None:
    match = re.search(r"\d+(?:[.,]\d+)?", str(value or ""))
    if match is None:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    match = re.search(r"\d[\d,.]*", str(value or ""))
    if match is None:
        return None
    try:
        return int(match.group(0).replace(",", "").replace(".", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


class GlassdoorCollector:
    """Accumulates advisory snapshots; writes nothing to the shared knowledge base."""

    def __init__(self, campaign_id: str):
        self.campaign_id = campaign_id
        self.employers: list[EmployerSnapshot] = []
        self.salaries: list[SalarySnapshot] = []

    async def handle(self, visit: pacing_mod.Visit) -> int:
        company_name = (visit.target.meta or {}).get("company_name", "")
        if visit.target.kind in ("employer", "reviews"):
            snapshot = parse_employer(visit.html, visit.url)
            if company_name and not snapshot.company_name:
                snapshot.company_name = company_name
            if snapshot.rating is None and not snapshot.themes["pros"]:
                return 0
            self.employers.append(snapshot)
            return 1
        rows = parse_salaries(visit.html, visit.url, company_name)
        self.salaries.extend(rows)
        return len(rows)

    def as_dict(self) -> dict[str, Any]:
        return {
            "advisory": True,
            "employers": [e.as_dict() for e in self.employers],
            "salaries": [s.as_dict() for s in self.salaries],
        }


def prepare_run(
    campaign_id: str, job_seeker_id: str, *, extra_urls: list[str] | None = None
) -> dict[str, Any]:
    """Target list, warning and duration estimate, for confirmation (FR-204)."""
    campaign = campaign_repo.get_campaign(campaign_id, job_seeker_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id} for this job seeker")
    targets = build_targets(campaign_id, extra_urls)
    pacing = pacing_mod.Pacing.from_settings()
    estimator = pacing_mod.DurationEstimator(pacing, site=SITE)
    return {
        "campaign_id": campaign_id,
        "site": SITE,
        "targets": [t.as_dict() for t in targets],
        "target_count": len(targets),
        "estimate": estimator.estimate(len(targets)).as_dict(),
        "warning": terms_warning(),
        "advisory_only": True,
        "confirmation_required": True,
    }


async def start_run(
    campaign_id: str,
    job_seeker_id: str,
    *,
    confirmed: bool,
    extra_urls: list[str] | None = None,
    driver: str | None = None,
    browser_family: str | None = None,
) -> dict[str, Any]:
    from dreamjob.browser.linkedin import ConfirmationRequired

    if not confirmed:
        raise ConfirmationRequired(
            "The estimated duration must be confirmed before a browser run starts (FR-204)"
        )
    prepared = prepare_run(campaign_id, job_seeker_id, extra_urls=extra_urls)
    if not prepared["target_count"]:
        raise ValueError("This campaign's plan holds no Glassdoor targets (FR-205)")
    chosen_driver, family = driver_choice(driver, browser_family)  # FR-208
    job_id = runner.create(
        JOB_KIND,
        campaign_id=campaign_id,
        job_seeker_id=job_seeker_id,
        adapter_key=ADAPTER_KEY,
        total=prepared["target_count"],
        estimated_seconds=int(prepared["estimate"]["seconds"]),
    )
    repo.save_job_state(
        job_id,
        {
            "site": SITE,
            "campaign_id": campaign_id,
            "targets": prepared["targets"],
            "estimate": prepared["estimate"],
            "driver": chosen_driver,
            "browser_family": family,
            "done": 0,
        },
    )
    record_audit(
        "browser.run_started",
        "job_run",
        job_id,
        seeker_id=job_seeker_id,
        detail={"site": SITE, "campaign_id": campaign_id, "targets": prepared["target_count"]},
    )
    await runner.start(job_id, worker)
    return {"job_id": job_id, "driver": chosen_driver, "browser_family": family, **prepared}


async def worker(ctx: JobContext) -> None:
    """Execute one Glassdoor run (FR-203..FR-207)."""
    state = repo.job_state(ctx.job_id)
    campaign_id = ctx.campaign_id or state.get("campaign_id") or ""
    job_seeker_id = ctx.job_seeker_id or ""
    targets = [
        pacing_mod.Target(
            url=t["url"],
            kind=t.get("kind") or classify_target(t["url"]) or "employer",
            label=t.get("label", ""),
            plan_item_id=t.get("plan_item_id"),
            meta=t.get("meta") or {},
        )
        for t in state.get("targets") or []
    ]
    done = int(state.get("done") or 0)
    control = pacing_mod.register_run(ctx.job_id, SITE)
    pacing = pacing_mod.Pacing.from_settings()
    estimator = pacing_mod.DurationEstimator(pacing, site=SITE)
    collector = GlassdoorCollector(campaign_id)

    chosen_driver, family = driver_choice(state.get("driver"), state.get("browser_family"))
    try:
        async with BrowserSession(driver=chosen_driver, family=family) as session:
            await session.bring_to_front()
            run = pacing_mod.PacedRun(
                site=SITE,
                session=session,
                pacing=pacing,
                estimator=estimator,
                ctx=ctx,
                control=control,
                job_seeker_id=job_seeker_id,
                done_offset=done,  # NFR-401: checkpoint absolute positions
            )
            report = await run.execute(targets[done:], collector.handle)
    except BrowserUnavailable as exc:
        repo.notify(
            job_seeker_id,
            "browser_unavailable",
            "The Glassdoor run could not start",
            str(exc),
            {"site": SITE, "campaign_id": campaign_id},
        )
        raise
    finally:
        pacing_mod.release_run(ctx.job_id)

    repo.save_job_state(
        ctx.job_id,
        {
            **state,
            **repo.job_state(ctx.job_id),
            "done": done + len(report.outcomes),
            "report": report.as_dict(),
            "snapshots": collector.as_dict(),
            "finished_at": utcnow(),
        },
    )
    record_audit(
        "browser.run_finished",
        "job_run",
        ctx.job_id,
        seeker_id=job_seeker_id,
        detail={
            "site": SITE,
            "campaign_id": campaign_id,
            "employers": len(collector.employers),
            "salaries": len(collector.salaries),
            "stopped_reason": report.stopped_reason,
        },
        actor="system",
    )


def snapshots_for_campaign(campaign_id: str) -> dict[str, Any]:
    """The advisory payload for the scoring and briefing slices (FR-264, FR-265).

    Campaign-scoped on purpose: opinion data collected under restrictive terms
    is not promoted into the shared knowledge base.
    """
    job = repo.latest_browser_job(campaign_id, adapter_key=ADAPTER_KEY)
    if job is None:
        return {"advisory": True, "employers": [], "salaries": []}
    state = repo.job_state(job["id"])
    if state.get("site") != SITE:
        return {"advisory": True, "employers": [], "salaries": []}
    snapshots = state.get("snapshots") or {}
    return {
        "advisory": True,
        "employers": snapshots.get("employers") or [],
        "salaries": snapshots.get("salaries") or [],
        "job_id": job["id"],
        "collected_at": job.get("finished_at") or job.get("started_at"),
    }
