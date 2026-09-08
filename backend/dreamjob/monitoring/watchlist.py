"""Watched companies, rechecked on a schedule (FR-401, FR-402).

A watch is a standing instruction: recheck this company every ``n`` days on
four channels - the careers page and its ATS endpoint, the newsroom, the
hiring signals those produce, and new filings - notify the job seeker of
anything new, and put matching vacancies straight into the ranked list.

Design points that matter:

*Channels are independent.*  A company with no newsroom, no ATS and no filed
accounts still gets its careers page read, and a failure on one channel never
stops the others.  Each channel reports what it found and what it could not
reach, and that report is stored on the entry so a silent watch can be told
apart from a broken one.

*New means new since the last look.*  The comparison is against
``last_checked_at``, and the notification carries a ``dedup_key`` so a vacancy
that stays open for six weeks is announced once, not six times.

*Adding to the ranked list is not the same as scoring it.*  A vacancy found
here is synthesised into an opportunity for the campaign the watch names, with
the campaign's own directives applied as a filter (FR-142..145).  It arrives
unscored, and the scorer picks it up on its next pass - which keeps FR-401's
"automatically" honest without letting a background job silently reorder the
seeker's list.

*Timing intelligence rides along* (FR-402): every pass refreshes the company's
hiring signals and recomputes its application window, so a favourable moment
reaches the ranked list as a ``timing_flag`` without a separate job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from dreamjob.adapters.ats import detect as ats_detect
from dreamjob.adapters.base import PlanItem, get_adapter
from dreamjob.adapters.vacancy_source import (
    build_vacancy,
    jobposting_to_fields,
    jsonld_jobpostings,
)
from dreamjob.db.connection import to_json, utcnow
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import knowledge as kb_repo
from dreamjob.db.repositories import opportunities as opp_repo
from dreamjob.db.repositories import pipeline_cards as repo
from dreamjob.egress.client import EgressClient, RobotsDisallowed
from dreamjob.pipeline import directives as dir_mod
from dreamjob.pipeline import knowledge_base
from dreamjob.pipeline import opportunities as opp_pipeline
from dreamjob.pipeline import signals as signals_mod
from dreamjob.security.audit import record_audit

log = logging.getLogger(__name__)

#: FR-401: "periodically (configurable, default weekly)".
DEFAULT_INTERVAL_DAYS = 7
MIN_INTERVAL_DAYS = 1
MAX_INTERVAL_DAYS = 90

#: The channels FR-401 names.  A watch may switch any of them off.
CHANNELS: tuple[str, ...] = ("careers", "ats", "news", "signals", "filings")
DEFAULT_CHANNELS = CHANNELS

#: A registry is only re-read when the last filing is this old; annual accounts
#: do not appear weekly and the registries are rate-limited.
FILING_REFRESH_DAYS = 90


class WatchError(RuntimeError):
    """Raised for a watch that cannot be created, never for one that fails a pass."""


@dataclass
class ChannelResult:
    channel: str
    checked: bool = False
    found: int = 0
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "checked": self.checked,
            "found": self.found,
            "error": self.error,
            "detail": self.detail,
        }


@dataclass
class WatchReport:
    """What one pass over one company did (FR-401)."""

    entry_id: str
    company_id: str
    company_name: str = ""
    channels: list[ChannelResult] = field(default_factory=list)
    new_vacancies: list[dict] = field(default_factory=list)
    new_signals: list[dict] = field(default_factory=list)
    new_filings: list[dict] = field(default_factory=list)
    opportunities_added: list[str] = field(default_factory=list)
    timing: dict[str, Any] = field(default_factory=dict)
    notifications: int = 0
    checked_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "company_id": self.company_id,
            "company_name": self.company_name,
            "channels": [c.as_dict() for c in self.channels],
            "new_vacancies": self.new_vacancies,
            "new_signals": self.new_signals,
            "new_filings": self.new_filings,
            "opportunities_added": self.opportunities_added,
            "timing": self.timing,
            "notifications": self.notifications,
            "checked_at": self.checked_at,
        }

    @property
    def anything_new(self) -> bool:
        return bool(self.new_vacancies or self.new_signals or self.new_filings)


# ---------------------------------------------------------------------------
# Managing the list
# ---------------------------------------------------------------------------


def add(
    job_seeker_id: str,
    company_id: str,
    *,
    interval_days: int = DEFAULT_INTERVAL_DAYS,
    campaign_id: str | None = None,
    channels: list[str] | None = None,
    reason: str | None = None,
) -> dict:
    """Put a company on the watchlist (FR-401).  Idempotent; due immediately."""
    company = opp_repo.get_company(company_id)
    if company is None:
        raise WatchError(f"No company {company_id} in the knowledge base")
    unknown = set(channels or []) - set(CHANNELS)
    if unknown:
        raise WatchError(f"Unknown channels: {', '.join(sorted(unknown))}")

    entry_id, created = repo.upsert_watch(
        job_seeker_id,
        company_id,
        {
            "check_interval_days": max(MIN_INTERVAL_DAYS, min(MAX_INTERVAL_DAYS, interval_days)),
            "campaign_id": campaign_id,
            "check_sources": to_json(list(channels) if channels else None),
            "reason": reason,
            "active": 1,
            # No next_check_at: a fresh watch runs on the next cycle rather
            # than in a week's time.
            "next_check_at": None,
        },
    )
    record_audit(
        "watchlist.added" if created else "watchlist.updated",
        "watchlist_entry",
        entry_id,
        seeker_id=job_seeker_id,
        detail={"company_id": company_id, "interval_days": interval_days},
    )
    return repo.get_watch(entry_id, job_seeker_id) or {}


def update(job_seeker_id: str, entry_id: str, values: dict[str, Any]) -> dict:
    entry = repo.get_watch(entry_id, job_seeker_id)
    if entry is None:
        raise LookupError(f"No watchlist entry {entry_id} for this job seeker")
    payload: dict[str, Any] = {}
    if "interval_days" in values and values["interval_days"] is not None:
        payload["check_interval_days"] = max(
            MIN_INTERVAL_DAYS, min(MAX_INTERVAL_DAYS, int(values["interval_days"]))
        )
    if "active" in values and values["active"] is not None:
        payload["active"] = 1 if values["active"] else 0
    if "campaign_id" in values:
        payload["campaign_id"] = values["campaign_id"]
    if "channels" in values:
        channels = values["channels"]
        unknown = set(channels or []) - set(CHANNELS)
        if unknown:
            raise WatchError(f"Unknown channels: {', '.join(sorted(unknown))}")
        payload["check_sources"] = to_json(list(channels) if channels else None)
    if "reason" in values:
        payload["reason"] = values["reason"]
    repo.update_watch(entry_id, payload)
    return repo.get_watch(entry_id, job_seeker_id) or {}


def remove(job_seeker_id: str, entry_id: str) -> bool:
    removed = repo.delete_watch(entry_id, job_seeker_id) > 0
    if removed:
        record_audit(
            "watchlist.removed", "watchlist_entry", entry_id, seeker_id=job_seeker_id
        )
    return removed


def entries(job_seeker_id: str, *, active_only: bool = False) -> list[dict]:
    return repo.list_watches(job_seeker_id, active_only=active_only)


def channels_of(entry: dict) -> tuple[str, ...]:
    stored = entry.get("check_sources")
    if isinstance(stored, list) and stored:
        return tuple(c for c in stored if c in CHANNELS)
    return DEFAULT_CHANNELS


# ---------------------------------------------------------------------------
# The channels
# ---------------------------------------------------------------------------


async def _ats_board(
    entry: dict, egress: EgressClient, *, vendor: str, slug: str
) -> tuple[int, str | None]:
    """Read one company's ATS board and write what it lists (FR-401)."""
    adapter_key = ats_detect.adapter_key_for(vendor)
    if not adapter_key:
        return 0, f"no adapter for ATS vendor {vendor!r}"
    try:
        adapter = get_adapter(adapter_key, egress)
    except KeyError:
        return 0, f"adapter {adapter_key} is not registered"

    item = PlanItem(
        adapter_key=adapter_key,
        native_query={
            "slug": slug,
            "company_id": entry["company_id"],
            "company_name": entry.get("company_name"),
            "max_records": 200,
        },
        rationale=f"watchlist recheck of {entry.get('company_name')}",
    )
    records = await adapter.run(item)
    writer = knowledge_base.KnowledgeBaseWriter(adapter_key=adapter_key)
    written = writer.write_many(records)
    return len(written), None


async def _careers_page(
    entry: dict, egress: EgressClient
) -> tuple[int, str | None, dict[str, Any]]:
    """Read the careers page: detect the ATS behind it, then its JobPosting blocks.

    Most careers pages are a shell around an ATS widget, so detection is worth
    more than parsing; where the postings are actually on the page they are
    almost always marked up as schema.org ``JobPosting``, which is exact.
    """
    url = entry.get("careers_url") or (
        f"https://{entry['company_domain']}/careers" if entry.get("company_domain") else None
    )
    if not url:
        return 0, "no careers page on record", {}
    try:
        result = await egress.fetch(url, access_method="http")
    except RobotsDisallowed as exc:
        return 0, f"robots.txt disallows the careers page ({exc})", {}
    except Exception as exc:  # noqa: BLE001 - one dead page is not a failed watch
        return 0, str(exc)[:300], {}
    if not result.ok:
        return 0, f"careers page returned HTTP {result.status_code}", {}

    detail: dict[str, Any] = {"url": url, "status": result.status_code}
    vendor, slug = ats_detect.detect_ats(result.text, url)
    if vendor and slug and not entry.get("ats_vendor"):
        # Learning where a company's board lives is worth keeping (FR-343).
        kb_repo.update_company(
            entry["company_id"], {"ats_vendor": vendor, "ats_slug": slug}
        )
        detail["ats_discovered"] = {"vendor": vendor, "slug": slug}

    postings = jsonld_jobpostings(result.text)
    if not postings:
        return 0, None, detail
    writer = knowledge_base.KnowledgeBaseWriter(adapter_key="careers_page")
    rows = []
    for posting in postings[:100]:
        fields = jobposting_to_fields(posting, result.url)
        fields.setdefault("company_id", entry["company_id"])
        row = build_vacancy(fields, adapter_key="careers_page", access_method="http")
        if row:
            row["company_id"] = entry["company_id"]
            row["raw_document_id"] = result.raw_document_id
            rows.append({"entity_type": "vacancy", "data": row, "confidence": 0.8})
    written = writer.write_many(rows)
    detail["jsonld_postings"] = len(postings)
    return len(written), None, detail


async def _news_and_signals(entry: dict, company: dict) -> tuple[int, str | None]:
    """Refresh the newsroom and the hiring signals it produces (FR-225, FR-402)."""
    try:
        await signals_mod.refresh_signals(company, fetch_news=True)
    except Exception as exc:  # noqa: BLE001 - a newsroom outage is not a failed watch
        return 0, str(exc)[:300]
    return 0, None


async def _filings(entry: dict, company: dict, *, refresh: bool) -> tuple[int, str | None]:
    """Ask the registries again, but only when the last filing is old enough."""
    if not refresh:
        return 0, None
    try:
        from dreamjob.pipeline import financial  # noqa: PLC0415 - heavy import, rare path

        report = await financial.collect_company_financials(company)
        return int(report.get("years_written") or 0), None
    except Exception as exc:  # noqa: BLE001 - registries are optional (RK-06)
        return 0, str(exc)[:300]


def _needs_filing_refresh(company_id: str) -> bool:
    rows = repo.filings_since(company_id, None, limit=1)
    if not rows:
        return True
    last = rows[0].get("collected_at")
    if not last:
        return True
    try:
        age = datetime.now(UTC) - datetime.fromisoformat(last)
    except ValueError:
        return True
    return age.days >= FILING_REFRESH_DAYS


# ---------------------------------------------------------------------------
# Adding what was found to the ranked list (FR-401)
# ---------------------------------------------------------------------------


def add_to_ranked_list(
    job_seeker_id: str, campaign_id: str, vacancies: list[dict]
) -> list[str]:
    """Synthesise new vacancies into opportunities for one campaign (FR-261, FR-401).

    The campaign's directives still apply: a watch is a reason to look, not a
    reason to accept a role the seeker excluded.
    """
    campaign = campaign_repo.get_campaign(campaign_id, job_seeker_id)
    if campaign is None:
        log.info("Watch names campaign %s, which this seeker does not own", campaign_id)
        return []

    inputs = campaign_repo.load_planning_inputs(campaign)
    directive_row = inputs.get("directives")
    directive_set = dir_mod.coerce_directive_set(directive_row) if directive_row else None

    added: list[str] = []
    for vacancy in vacancies:
        company = opp_repo.get_company(vacancy["company_id"]) if vacancy.get("company_id") else None
        record = opp_pipeline.normalise_vacancy(vacancy, company)
        reason = opp_pipeline.rejection_reason(record, company, directive_set)
        if reason:
            log.debug("Watched vacancy %s not added: %s", vacancy.get("id"), reason)
            continue
        if vacancy.get("company_id"):
            record["timing_flag"] = signals_mod.timing_flag_for(vacancy["company_id"])
        existing = opp_repo.find_by_vacancy(campaign_id, vacancy["id"])
        opportunity_id, created = opp_repo.upsert_synthesised(
            job_seeker_id, campaign_id, record, existing
        )
        if created:
            added.append(opportunity_id)
    return added


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------


async def check_entry(
    entry: dict,
    *,
    egress: EgressClient | None = None,
    notify: bool = True,
) -> WatchReport:
    """Recheck one watched company on every enabled channel (FR-401, FR-402)."""
    job_seeker_id = entry["job_seeker_id"]
    company_id = entry["company_id"]
    since = entry.get("last_checked_at")
    started = utcnow()
    report = WatchReport(
        entry_id=entry["id"],
        company_id=company_id,
        company_name=entry.get("company_name") or "",
        checked_at=started,
    )
    company = opp_repo.get_company(company_id) or {"id": company_id}
    enabled = channels_of(entry)

    own_egress = egress is None
    client = egress or EgressClient()
    if own_egress:
        await client.__aenter__()
    try:
        vendor = entry.get("ats_vendor")
        slug = entry.get("ats_slug")

        if "careers" in enabled:
            found, error, detail = await _careers_page(entry, client)
            report.channels.append(ChannelResult("careers", True, found, error, detail))
            # The careers page is where a company's board is discovered, so the
            # ATS channel runs against what this pass just learned.
            discovered = detail.get("ats_discovered") or {}
            vendor = vendor or discovered.get("vendor")
            slug = slug or discovered.get("slug")

        if "ats" in enabled:
            if vendor and slug:
                try:
                    found, error = await _ats_board(entry, client, vendor=vendor, slug=slug)
                except Exception as exc:  # noqa: BLE001 - a dead board is not a failed watch
                    found, error = 0, str(exc)[:300]
                report.channels.append(ChannelResult("ats", True, found, error,
                                                     {"vendor": vendor, "slug": slug}))
            else:
                report.channels.append(
                    ChannelResult("ats", False, 0, "no ATS vendor known for this company")
                )

        if "news" in enabled or "signals" in enabled:
            _, error = await _news_and_signals(entry, company)
            report.channels.append(ChannelResult("news", True, 0, error))

        if "filings" in enabled:
            refresh = _needs_filing_refresh(company_id)
            found, error = await _filings(entry, company, refresh=refresh)
            report.channels.append(
                ChannelResult("filings", refresh, found, error, {"registry_reread": refresh})
            )
    finally:
        if own_egress:
            await client.__aexit__(None, None, None)

    # What is actually new, measured against the previous pass.
    report.new_vacancies = [
        {
            "id": v["id"],
            "title": v.get("title"),
            "location": v.get("location"),
            "source_url": v.get("source_url"),
            "posted_at": v.get("posted_at") or v.get("collected_at"),
        }
        for v in repo.vacancies_since(company_id, since)
    ]
    report.new_signals = [
        {
            "id": s["id"],
            "signal_type": s.get("signal_type"),
            "description": s.get("description"),
            "occurred_at": s.get("occurred_at"),
            "source_url": s.get("source_url"),
            "strength": s.get("strength"),
        }
        for s in repo.signals_since(company_id, since)
    ]
    report.new_filings = repo.filings_since(company_id, since, limit=5)

    # FR-402: recompute the window while we are here.
    try:
        report.timing = signals_mod.recommend_window(company_id).as_dict()
    except Exception:  # noqa: BLE001 - timing is advisory
        log.debug("Could not recompute the timing window for %s", company_id)

    if entry.get("campaign_id") and report.new_vacancies:
        vacancy_rows = repo.vacancies_since(company_id, since)
        report.opportunities_added = add_to_ranked_list(
            job_seeker_id, entry["campaign_id"], vacancy_rows
        )

    if notify:
        report.notifications = _notify(job_seeker_id, entry, report)

    failures = [c for c in report.channels if c.error]
    interval = int(entry.get("check_interval_days") or DEFAULT_INTERVAL_DAYS)
    repo.update_watch(
        entry["id"],
        {
            "last_checked_at": started,
            "next_check_at": (
                datetime.now(UTC) + timedelta(days=interval)
            ).isoformat(timespec="seconds"),
            "last_result": to_json(
                {
                    "channels": [c.as_dict() for c in report.channels],
                    "new_vacancies": len(report.new_vacancies),
                    "new_signals": len(report.new_signals),
                    "new_filings": len(report.new_filings),
                    "opportunities_added": len(report.opportunities_added),
                    "timing_flag": report.timing.get("timing_flag"),
                }
            ),
            "last_error": failures[0].error if failures else None,
            "consecutive_failures": (
                int(entry.get("consecutive_failures") or 0) + 1
                if len(failures) == len([c for c in report.channels if c.checked])
                and report.channels
                else 0
            ),
        },
    )
    return report


def _notify(job_seeker_id: str, entry: dict, report: WatchReport) -> int:
    """One notification per genuinely new thing (FR-401), de-duplicated."""
    company = report.company_name or "a watched company"
    sent = 0
    for vacancy in report.new_vacancies[:10]:
        if repo.notify(
            job_seeker_id,
            {
                "kind": "new_vacancy",
                "title": f"{company}: {vacancy['title']}",
                "body": " - ".join(
                    p for p in (vacancy.get("location"), vacancy.get("posted_at")) if p
                ),
                "payload": {
                    "company_id": report.company_id,
                    "vacancy_id": vacancy["id"],
                    "source_url": vacancy.get("source_url"),
                    "added_to_ranked_list": bool(report.opportunities_added),
                },
                "severity": "action",
                "dedup_key": f"vacancy:{vacancy['id']}",
            }
        ):
            sent += 1
    for signal in report.new_signals[:5]:
        if float(signal.get("strength") or 0) < 0.4:
            continue
        if repo.notify(
            job_seeker_id,
            {
                "kind": "signal",
                "title": f"{company}: {str(signal.get('signal_type') or '').replace('_', ' ')}",
                "body": signal.get("description") or "",
                "payload": {
                    "company_id": report.company_id,
                    "signal_id": signal["id"],
                    "source_url": signal.get("source_url"),
                    "timing": report.timing.get("timing_flag"),
                },
                "severity": "info",
                "dedup_key": f"signal:{signal['id']}",
            }
        ):
            sent += 1
    for filing in report.new_filings[:2]:
        if repo.notify(
            job_seeker_id,
            {
                "kind": "signal",
                "title": f"{company}: accounts filed for {filing.get('fiscal_year')}",
                "body": "New financial year on record; the briefing and the negotiation "
                        "brief will use it.",
                "payload": {"company_id": report.company_id, "fiscal_year": filing.get(
                    "fiscal_year"
                )},
                "severity": "info",
                "dedup_key": f"filing:{filing['id']}",
            }
        ):
            sent += 1
    return sent


async def run_cycle(
    job_seeker_id: str | None = None, *, limit: int = 50, notify: bool = True
) -> dict[str, Any]:
    """Recheck every watch that is due (FR-401).  One failure never stops the rest."""
    due = repo.watches_due(utcnow(), job_seeker_id=job_seeker_id, limit=limit)
    reports: list[dict] = []
    errors: list[dict] = []
    if not due:
        return {"checked": 0, "due": 0, "reports": [], "errors": []}

    async with EgressClient() as egress:
        for entry in due:
            try:
                report = await check_entry(entry, egress=egress, notify=notify)
                reports.append(report.as_dict())
            except Exception as exc:  # noqa: BLE001 - one company must not stop the cycle
                log.exception("Watchlist check failed for company %s", entry.get("company_id"))
                errors.append({"entry_id": entry["id"], "error": str(exc)[:300]})
                repo.update_watch(
                    entry["id"],
                    {
                        "last_error": str(exc)[:500],
                        "consecutive_failures": int(entry.get("consecutive_failures") or 0) + 1,
                        "next_check_at": (
                            datetime.now(UTC) + timedelta(days=1)
                        ).isoformat(timespec="seconds"),
                    },
                )
    return {
        "checked": len(reports),
        "due": len(due),
        "new_vacancies": sum(len(r["new_vacancies"]) for r in reports),
        "new_signals": sum(len(r["new_signals"]) for r in reports),
        "opportunities_added": sum(len(r["opportunities_added"]) for r in reports),
        "reports": reports,
        "errors": errors,
    }
