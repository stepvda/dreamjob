"""Company enrichment: the passes that make an employer more than a name.

Eleven rungs of company research already existed - the employer-kind ladder,
the website crawl, competitors, hiring signals, the financial filings and the
two financial scores - and **nothing ran them automatically**. A campaign that
collected 43,000 vacancies left every employer in it a bare name: no website
profile, no filings, no signals, no employer kind, and the ranked list showing
"employer type not verified" on every row. The passes were reachable only from
the administration and campaign screens, so they ran when someone remembered
to press them.

This module is the missing scheduler-side orchestrator. It is deliberately
thin: every pass already exists, is bounded and already degrades on its own, so
all that is added here is the order, a shared budget, and one report.

The order matters
-----------------
1. **Employer kind** first. Scoring reads it, the badge shows it on every row,
   and it decides what the product may do with the employer (a recruitment
   agency posting is not the employer's own vacancy).
2. **Website profile** next; it is the source the other passes read.
3. **Competitors and signals** after the crawl, because both read the pages it
   stored.
4. **Financials** last: the most expensive, and the one whose absence still
   leaves a usable record.

Every pass is incremental (FR-226, FR-343): each checks its own staleness
first, so running this on a campaign that is already enriched costs a few
queries rather than a re-crawl.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import opportunities as opp_repo
from dreamjob.pipeline import company_profile, employer_resolver, financial

log = logging.getLogger(__name__)

#: How many companies one automated enrichment pass covers.  Bounded because
#: this runs at the end of every collection: a campaign that touched 1,600
#: companies should still show its list promptly, and enrichment continues on
#: the next run or on the nightly sweep.
DEFAULT_COMPANY_LIMIT = 15

#: The nightly sweep is allowed to work through more of the backlog.
SWEEP_COMPANY_LIMIT = 60


@dataclass
class EnrichmentReport:
    campaign_id: str | None = None
    companies: int = 0
    domains: dict[str, Any] = field(default_factory=dict)
    employer_kind: dict[str, Any] = field(default_factory=dict)
    profiles: dict[str, Any] = field(default_factory=dict)
    signals: dict[str, Any] = field(default_factory=dict)
    financials: dict[str, Any] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "companies": self.companies,
            "domains": self.domains,
            "employer_kind": self.employer_kind,
            "profiles": self.profiles,
            "signals": self.signals,
            "financials": self.financials,
            "skipped": self.skipped,
        }


async def enrich_campaign(
    campaign_id: str,
    job_seeker_id: str,
    *,
    limit: int = DEFAULT_COMPANY_LIMIT,
    do_employer_kind: bool = True,
    do_domains: bool = True,
    do_profiles: bool = True,
    do_signals: bool = True,
    do_financials: bool = True,
) -> EnrichmentReport:
    """Run the company-enrichment passes for one campaign's companies.

    Never raises for a failed pass: each is caught and recorded, because a
    campaign that collected thousands of vacancies is worth keeping even if the
    register was unreachable that evening.
    """
    report = EnrichmentReport(campaign_id=campaign_id)
    company_ids = _campaign_companies(campaign_id, limit)
    report.companies = len(company_ids)
    if not company_ids:
        report.skipped.append("no companies in this campaign")
        return report

    if do_domains:
        # Before the crawl: the crawl needs a URL, and most of these companies
        # have no domain recorded even though a vacancy URL or an ATS tenant
        # name says what it is. Without this the profile pass reports success
        # and stores nothing.
        report.domains = await _guarded("domains", _fill_domains(company_ids), report)

    if do_employer_kind:
        report.employer_kind = await _guarded(
            "employer_kind",
            employer_resolver.resolve_many(len(company_ids), company_ids=company_ids),
            report,
        )

    if do_profiles:
        report.profiles = await _guarded(
            "profiles",
            company_profile.rerun(campaign_id, job_seeker_id, limit=limit),
            report,
        )

    if do_signals:
        # Signals are derived from the pages the crawl just stored, the postings
        # already collected and the filings; the pass itself is synchronous and
        # cheap, so it is not a module of its own to call.
        report.signals = await _guarded(
            "signals",
            _refresh_signals(company_ids),
            report,
        )

    if do_financials:
        report.financials = await _guarded(
            "financials",
            financial.rerun(campaign_id, job_seeker_id, limit=limit),
            report,
        )

    return report


async def enrich_companies(
    company_ids: list[str],
    *,
    campaign_id: str | None = None,
    job_seeker_id: str | None = None,
    limit: int = DEFAULT_COMPANY_LIMIT,
) -> EnrichmentReport:
    """Enrich a named set of companies, with no campaign in particular.

    Used by the backfill and by the sweep: the campaign-scoped passes fall back
    to the newest campaign the companies appear in, and the employer-kind and
    signal passes work from the company ids alone.
    """
    ids = [str(c) for c in company_ids if c][:limit]
    if campaign_id is None and job_seeker_id:
        campaign_id = _campaign_for_companies(job_seeker_id, ids)
    if campaign_id is None or job_seeker_id is None:
        # Only the passes that need no campaign can run.
        report = EnrichmentReport(campaign_id=None, companies=len(ids))
        report.employer_kind = await _guarded(
            "employer_kind",
            employer_resolver.resolve_many(len(ids), company_ids=ids),
            report,
        )
        report.signals = await _guarded("signals", _refresh_signals(ids), report)
        return report
    return await enrich_campaign(campaign_id, job_seeker_id, limit=len(ids))


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _fill_domains(company_ids: list[str]) -> dict:
    """Find the websites of the companies about to be profiled (FR-221)."""
    from dreamjob.pipeline import domain_resolver  # noqa: PLC0415

    return await domain_resolver.backfill(len(company_ids), company_ids=company_ids)


async def _guarded(name: str, awaitable: Any, report: EnrichmentReport) -> dict:
    """Await one pass, recording a failure instead of propagating it."""
    try:
        result = await awaitable
    except Exception as exc:  # noqa: BLE001 - enrichment must never fail a run
        reason = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("Company enrichment pass %s failed: %s", name, reason)
        return {"error": reason}
    if hasattr(result, "as_dict"):
        return result.as_dict()
    if isinstance(result, dict):
        return result
    return {"result": str(result)[:200]}


def _campaign_companies(campaign_id: str, limit: int) -> list[str]:
    """The companies behind the campaign's opportunities, busiest first.

    Falls back to every company the campaign touched when it produced no
    opportunities at all - a spontaneous-application campaign, or one whose
    postings were all rejected - because then there is no shortlist to serve
    and the employer research is still worth having.
    """
    try:
        ids = opp_repo.campaign_opportunity_companies(campaign_id, limit)
        if not ids:
            ids = opp_repo.campaign_company_ids(campaign_id)
    except Exception:  # noqa: BLE001
        log.exception("Could not read companies for campaign %s", campaign_id)
        return []
    return [str(c) for c in (ids or [])][:limit]


def _campaign_for_companies(job_seeker_id: str, company_ids: list[str]) -> str | None:
    """The newest campaign of this seeker that holds one of these companies."""
    for campaign in campaign_repo.list_campaigns(job_seeker_id):
        if set(_campaign_companies(campaign["id"], 500)) & set(company_ids):
            return str(campaign["id"])
    return None


async def _refresh_signals(company_ids: list[str]) -> dict:
    """Derive signals for each company from what is already stored.

    Pure corpus work: the pages, postings, financials and competitors are read
    from the database, so this costs no requests and runs for every company
    every time rather than being rationed.
    """
    from dreamjob.pipeline import signals as signals_mod  # noqa: PLC0415

    total = 0
    with_signals = 0
    for company_id in company_ids:
        found = 0
        for producer in (
            signals_mod.signals_from_postings,
            signals_mod.signals_from_financials,
            signals_mod.signals_from_competitors,
        ):
            try:
                produced = producer(company_id)
            except Exception:  # noqa: BLE001 - one producer must not stop the rest
                continue
            found += len(produced or [])
        if found:
            with_signals += 1
        total += found
    return {"signals": total, "companies_with_signals": with_signals}
