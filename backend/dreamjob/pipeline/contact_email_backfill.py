"""Fill in e-mail addresses for stored contacts that have none (FR-303, FR-304).

The contacts pass in :mod:`dreamjob.pipeline.apply_contacts` answers "does this
company have anybody to write to" and stops at the first address it can justify.
By the time it is done, the corpus also holds *people* - a hiring manager read
off a team page, a department head from the company profile - whose row was
created without an address.  This module is the second question, asked of those
rows: each one already names a person and a company, so the machinery that has
already resolved the domain and learned its convention can be pointed at the
person rather than the company.

It is deliberately the same vocabulary as the first pass, reused rather than
re-implemented:

* :func:`apply_contacts.resolve_domain` - so a company whose record carries no
  domain is judged by the full CR-405 gate, not by a second, weaker spelling;
* :func:`email_patterns.collect_from_site` - the published addresses, read once
  per domain and cached for the whole pass (FR-305);
* :func:`email_patterns.learn_domain_pattern` and
  :func:`email_patterns.candidates_for_person` - the convention, learned from
  the addresses that exist, applied to the person;
* :func:`contacts._first_usable` and :func:`email_validate.validate` - FR-304,
  so an ``invalid`` address is never stored.

**Idempotent and resumable.**  The work list is a query for rows with no
address; the write is a single guarded ``UPDATE ... WHERE email IS NULL`` (see
:func:`repositories.contacts.set_contact_email`).  A second run therefore
selects nothing that the first filled, and a run resumed after a crash picks up
exactly where it stopped.  A row that gained an address, or an objection, while
the pass was running is left alone and reported as such.

**Nothing invented, nothing overwritten.**  An address is stored only when
FR-304 does not call it ``invalid``; a composed address is marked uncertain
unless it is ``valid``; an existing address is never touched; and NFR-302
objections are honoured in the selection and again in the write.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import contacts as contacts_repo
from dreamjob.egress.client import EgressClient
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.pipeline import apply_contacts as apply
from dreamjob.pipeline import contacts as discovery
from dreamjob.pipeline import email_patterns as patterns
from dreamjob.pipeline import email_validate as validation

log = logging.getLogger(__name__)

#: Companies resolved at once.  Every one is a different domain, so the shared
#: egress client's per-domain rate limit is not shared between them (FR-182).
CONCURRENCY = 8

#: Job kind for a backfill started from the API.
BACKFILL_JOB_KIND = "contact_email_backfill"


@dataclass
class BackfillReport:
    """What one pass achieved, in the terms the brief asks for."""

    considered: int = 0
    updated: int = 0
    uncertain: int = 0
    already_had_email: int = 0
    skipped_no_company: int = 0
    skipped_no_domain: int = 0
    skipped_unresolved: int = 0
    objected: int = 0
    skipped_invalid: int = 0
    companies_visited: int = 0
    by_method: Counter[str] = field(default_factory=Counter)
    by_validation: Counter[str] = field(default_factory=Counter)
    started_at: str = field(default_factory=utcnow)
    finished_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "updated": self.updated,
            "uncertain": self.uncertain,
            "already_had_email": self.already_had_email,
            "skipped_no_company": self.skipped_no_company,
            "skipped_no_domain": self.skipped_no_domain,
            "skipped_unresolved": self.skipped_unresolved,
            "objected": self.objected,
            "skipped_invalid": self.skipped_invalid,
            "companies_visited": self.companies_visited,
            "by_method": dict(self.by_method),
            "by_validation": dict(self.by_validation),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def progress(self) -> dict[str, Any]:
        """The counters a progress tick needs; mirrors :meth:`as_dict`."""
        return {
            "considered": self.considered,
            "updated": self.updated,
            "uncertain": self.uncertain,
            "already_had_email": self.already_had_email,
            "skipped_no_company": self.skipped_no_company,
            "skipped_no_domain": self.skipped_no_domain,
            "skipped_unresolved": self.skipped_unresolved,
            "objected": self.objected,
            "skipped_invalid": self.skipped_invalid,
            "companies_visited": self.companies_visited,
            "by_method": dict(self.by_method),
            "by_validation": dict(self.by_validation),
        }


@dataclass
class _CompanyResult:
    """One company's contribution, merged once so the shared report is safe."""

    updated: int = 0
    uncertain: int = 0
    already_had_email: int = 0
    skipped_no_company: int = 0
    skipped_no_domain: int = 0
    skipped_unresolved: int = 0
    objected: int = 0
    skipped_invalid: int = 0
    companies_visited: int = 0
    by_method: Counter[str] = field(default_factory=Counter)
    by_validation: Counter[str] = field(default_factory=Counter)


def _merge(report: BackfillReport, result: _CompanyResult) -> None:
    report.updated += result.updated
    report.uncertain += result.uncertain
    report.already_had_email += result.already_had_email
    report.skipped_no_company += result.skipped_no_company
    report.skipped_no_domain += result.skipped_no_domain
    report.skipped_unresolved += result.skipped_unresolved
    report.objected += result.objected
    report.skipped_invalid += result.skipped_invalid
    report.companies_visited += result.companies_visited
    report.by_method.update(result.by_method)
    report.by_validation.update(result.by_validation)


def _notify(cb: Callable[[dict[str, Any]], None] | None, payload: dict[str, Any]) -> None:
    """Hand a progress payload to the caller; progress must never fail a pass."""
    if cb is None:
        return
    try:
        cb(payload)
    except Exception:  # noqa: BLE001 - progress must never fail a pass
        log.debug("progress callback failed", exc_info=True)


async def _harvest(
    domain: str,
    *,
    careers_url: str | None,
    egress: EgressClient,
    crawl_site: bool,
    harvest_cache: dict[str, asyncio.Future[list[patterns.FoundAddress]]],
    cache_lock: asyncio.Lock,
) -> list[patterns.FoundAddress]:
    """The published addresses of one domain, crawled at most once per pass.

    The future is published in the cache before the crawl starts, so a second
    company that spells to the same domain waits for the first instead of
    fetching the same site again (FR-305).
    """
    if not crawl_site:
        return []
    loop = asyncio.get_running_loop()
    async with cache_lock:
        future = harvest_cache.get(domain)
        owner = future is None
        if owner:
            future = loop.create_future()
            harvest_cache[domain] = future
    if not owner and future is not None:
        return await future
    try:
        found = await patterns.collect_from_site(
            domain, careers_url=careers_url, egress=egress, max_pages=12
        )
    except Exception:  # noqa: BLE001 - an unreadable site is not a failed pass
        log.info("Could not read %s for addresses", domain, exc_info=True)
        found = []
    if future is not None:
        future.set_result(found)
    return found


async def _address_guesses(
    person: dict[str, Any],
    *,
    domain: str,
    inference: patterns.PatternInference | None,
    published: dict[str, patterns.FoundAddress],
    company: dict[str, Any],
    egress: EgressClient,
    use_lookup_service: bool,
) -> list[patterns.FoundAddress]:
    """The addresses to try for one person, published evidence first."""
    full_name = (person.get("full_name") or "").strip()
    guesses: list[patterns.FoundAddress] = []

    # A published address that matches the person by name is the strongest
    # evidence there is, so it is validated before anything composed (FR-303).
    if full_name:
        match = discovery._published_for(full_name, published)
        if match is not None:
            guesses.append(match)
        guesses.extend(
            patterns.candidates_for_person(full_name, domain, inference=inference, limit=3)
        )
        if use_lookup_service and patterns.lookup_service_enabled():
            try:
                looked_up = await patterns.lookup_via_service(full_name, domain, egress=egress)
                guesses = looked_up + guesses
            except patterns.LookupServiceDisabled as exc:
                log.info("Lookup service disabled: %s", exc)
            except Exception:  # noqa: BLE001 - one source must not stop the person
                log.info("Lookup service failed for %s", full_name, exc_info=True)
    else:
        # A row with no usable name - a department label, a role - can still be
        # reached through the company's conventional mailbox (FR-301 tier 3).
        guesses.extend(
            patterns.generic_candidates(domain, careers_url=company.get("careers_url"))
        )
    return guesses


async def _backfill_person(
    person: dict[str, Any],
    *,
    domain: str,
    inference: patterns.PatternInference | None,
    published: dict[str, patterns.FoundAddress],
    company: dict[str, Any],
    egress: EgressClient,
    allow_smtp: bool,
    use_lookup_service: bool,
    result: _CompanyResult,
) -> None:
    """Find one address, validate it and store it if it survives FR-304."""
    contact_id = str(person.get("id") or "")
    if not contact_id:
        result.skipped_unresolved += 1
        return

    guesses = await _address_guesses(
        person,
        domain=domain,
        inference=inference,
        published=published,
        company=company,
        egress=egress,
        use_lookup_service=use_lookup_service,
    )
    if not guesses:
        result.skipped_unresolved += 1
        return

    chosen = await asyncio.to_thread(
        discovery._first_usable, guesses, allow_smtp=allow_smtp
    )
    if chosen is None:
        # Candidates existed and every one was ruled out (FR-304).
        result.skipped_invalid += 1
        return

    address, verdict = chosen
    if verdict.result == validation.INVALID:
        result.skipped_invalid += 1
        return

    changed = await asyncio.to_thread(
        contacts_repo.set_contact_email,
        contact_id,
        email=address.email,
        method=address.method,
        validation_result=verdict.result,
        validation_detail=verdict.detail,
        confidence=address.confidence,
    )
    if not changed:
        current = await asyncio.to_thread(contacts_repo.get_contact, contact_id)
        if current and current.get("objected"):
            result.objected += 1
        else:
            # The row gained an address after it was selected.
            result.already_had_email += 1
        return

    result.updated += 1
    result.by_method[address.method] += 1
    result.by_validation[verdict.result] += 1
    if address.method == patterns.METHOD_PATTERN and verdict.result != validation.VALID:
        result.uncertain += 1


async def _backfill_company(
    company_id: str,
    people: list[dict[str, Any]],
    *,
    egress: EgressClient,
    allow_smtp: bool,
    crawl_site: bool,
    use_lookup_service: bool,
    harvest_cache: dict[str, asyncio.Future[list[patterns.FoundAddress]]],
    cache_lock: asyncio.Lock,
) -> _CompanyResult:
    """One company: resolve its domain once, crawl once, then serve its people."""
    result = _CompanyResult(companies_visited=1)
    if not company_id:
        result.skipped_no_company += len(people)
        return result

    head = people[0]
    company: dict[str, Any] = {
        "company_id": company_id,
        "company_name": head.get("company_name") or "",
        "company_domain": head.get("company_domain"),
        "careers_url": head.get("company_careers_url"),
        "country": head.get("company_country") or "",
        "vacancy_count": 0,
    }

    # The company's own domain, or the full CR-405 derivation when it has none.
    domain, _source, _note = await apply.resolve_domain(company, [], [], egress=egress)
    if not domain:
        result.skipped_no_domain += len(people)
        return result

    harvested = await _harvest(
        domain,
        careers_url=company.get("careers_url"),
        egress=egress,
        crawl_site=crawl_site,
        harvest_cache=harvest_cache,
        cache_lock=cache_lock,
    )
    published = {a.email: a for a in harvested}
    observations = [
        (a.full_name, a.email)
        for a in harvested
        if a.full_name and not validation.is_role_address(a.email)
    ]
    inference = await asyncio.to_thread(
        patterns.learn_domain_pattern, domain, observations
    )

    for person in people:
        await _backfill_person(
            person,
            domain=domain,
            inference=inference,
            published=published,
            company=company,
            egress=egress,
            allow_smtp=allow_smtp,
            use_lookup_service=use_lookup_service,
            result=result,
        )
    return result


async def backfill_missing_emails(
    job_seeker_id: str,
    *,
    scope: str = "mine",
    limit: int = 500,
    max_companies: int | None = None,
    allow_smtp: bool = False,
    crawl_site: bool = True,
    use_lookup_service: bool = False,
    egress: EgressClient | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> BackfillReport:
    """Give an address to every stored contact that has none (FR-303, FR-304).

    ``scope='mine'`` selects the seeker's own and shared contacts and applies
    the NFR-303 campaign scope; ``scope='all'`` is the administrator's sweep of
    the whole ``contact`` table and is not scoped to a seeker.

    Contacts are grouped by company, because the expensive part - resolving the
    domain, reading its pages, learning its convention - is a property of the
    company, not of the person.  A company is visited once for all of its rows;
    ``max_companies`` caps how many are visited.  Companies are processed
    concurrently behind a semaphore and a single shared
    :class:`~dreamjob.egress.client.EgressClient`, whose per-domain pacing keeps
    the pass polite (FR-182, FR-305).
    """
    seeker = job_seeker_id if scope == "mine" else None
    rows = await asyncio.to_thread(
        contacts_repo.contacts_missing_email,
        limit,
        job_seeker_id=seeker,
        order="recent",
    )
    report = BackfillReport(considered=len(rows))

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("company_id") or ""), []).append(row)
    companies = list(grouped.items())
    if max_companies is not None:
        companies = companies[: max(0, int(max_companies))]

    total = len(companies)
    if not companies:
        report.finished_at = utcnow()
        log.info("Contact e-mail backfill (%s): nothing to do", scope)
        _notify(
            on_progress,
            {"phase": "done", "done": 1, "total": 1, "report": report.progress()},
        )
        return report

    _notify(
        on_progress,
        {"phase": "start", "done": 0, "total": total, "report": report.progress()},
    )

    harvest_cache: dict[str, asyncio.Future[list[patterns.FoundAddress]]] = {}
    cache_lock = asyncio.Lock()
    gate = asyncio.Semaphore(max(1, CONCURRENCY))

    async def run(client: EgressClient) -> None:
        async def one(company_id: str, people: list[dict[str, Any]]) -> None:
            async with gate:
                try:
                    result = await _backfill_company(
                        company_id,
                        people,
                        egress=client,
                        allow_smtp=allow_smtp,
                        crawl_site=crawl_site,
                        use_lookup_service=use_lookup_service,
                        harvest_cache=harvest_cache,
                        cache_lock=cache_lock,
                    )
                except Exception:  # noqa: BLE001 - one company never stops a pass
                    log.exception("E-mail backfill failed for company %s", company_id)
                    result = _CompanyResult(skipped_unresolved=len(people))
                _merge(report, result)
                _notify(
                    on_progress,
                    {
                        "phase": "company",
                        "done": report.companies_visited,
                        "total": total,
                        "report": report.progress(),
                    },
                )

        await asyncio.gather(*(one(cid, people) for cid, people in companies))

    if egress is not None:
        await run(egress)
    else:
        async with EgressClient(store_raw=False) as client:
            await run(client)

    report.finished_at = utcnow()
    log.info(
        "Contact e-mail backfill (%s): %d/%d contacts given an address across %d "
        "companies (%d uncertain, %d already had one, %d no domain, %d invalid, "
        "%d unresolved)",
        scope, report.updated, report.considered, report.companies_visited,
        report.uncertain, report.already_had_email, report.skipped_no_domain,
        report.skipped_invalid, report.skipped_unresolved,
    )
    return report


def run_contact_email_backfill(job_seeker_id: str, **kwargs: Any) -> dict[str, Any]:
    """Synchronous entry point, for scripts and the job runner."""
    return asyncio.run(backfill_missing_emails(job_seeker_id, **kwargs)).as_dict()


async def contact_email_backfill_worker(ctx: JobContext):
    """Run the backfill for a seeker, reporting what it stored.

    The caller stores the pass options in the job checkpoint (``{"options":
    {...}}``), which is what makes the worker resumable without a closure.
    """
    options = dict((ctx.checkpoint or {}).get("options") or {})
    limit = int(options.pop("limit", 500) or 500)

    def on_progress(payload: dict[str, Any]) -> None:
        total = payload.get("total")
        if total:
            ctx.progress(int(payload.get("done") or 0), int(total))
        if payload.get("report"):
            ctx.save_checkpoint(report=payload["report"])

    report = await backfill_missing_emails(
        ctx.job_seeker_id or "", limit=limit, on_progress=on_progress, **options
    )
    payload = report.as_dict()
    ctx.save_checkpoint(report=payload)
    ctx.progress(1, 1)
    yield payload


runner.register_worker(BACKFILL_JOB_KIND, contact_email_backfill_worker)
