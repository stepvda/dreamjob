"""LinkedIn automation, strictly on the plan's target list (FR-205, FR-206, FR-207, CR-401).

This is the most legally sensitive module in Dream Job, and it is written to
be reviewed rather than to be clever:

* **CR-401** - LinkedIn's user agreement prohibits automated access. The
  warning is shown before the first run and the acknowledgement is recorded as
  the ``linkedin_automation`` consent by the auth slice; :func:`start_run`
  refuses to do anything without it.
* **FR-205** - the run walks an explicit allowlist built from the campaign's
  own plan items. :class:`Allowlist` refuses any other URL, so a page that
  mentions a thousand other profiles cannot turn the run into a crawl. Caps on
  profiles and companies (FR-165, FR-186, CR-406) truncate the list before the
  browser opens.
* **FR-207 / NFR-303** - everything collected here is tagged
  ``access_method = 'browser'``; people records additionally get
  ``shareable = 0``, the owning campaign and a retention date.
* **FR-206** - the user watches their own browser window, and can pause,
  cancel or skip a single target while the run is going.
* **FR-203 / RK-01** - pacing, scrolling and an immediate stop on any
  challenge page are provided by :mod:`dreamjob.browser.pacing`.

Extraction is deterministic first (JSON-LD, then DOM selectors); the LLM is a
fallback for a page whose markup has changed, and it sees the page only as
isolated untrusted data (NFR-205, FR-183).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from dreamjob.adapters.base import NormalisedRecord
from dreamjob.browser import pacing as pacing_mod
from dreamjob.browser.session import (
    SITES,
    BrowserSession,
    BrowserUnavailable,
    driver_choice,
    json_ld,
    json_ld_of_type,
    page_text,
    safe_url,
    text_of,
    tidy,
)
from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import browser as repo
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import knowledge as kb_repo
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.llm.client import LLMClient
from dreamjob.pipeline.knowledge_base import KnowledgeBaseWriter
from dreamjob.security import auth_service as auth
from dreamjob.security.audit import record_audit

try:  # selectolax is a declared dependency; the fallback keeps imports safe
    from selectolax.parser import HTMLParser
except ImportError:  # pragma: no cover
    HTMLParser = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

SITE = "linkedin"
JOB_KIND = "browser"
ADAPTER_KEY = "linkedin_network"
CONSENT_KIND = "linkedin_automation"

#: CR-406: tens to low hundreds of targets, never thousands.
MAX_TARGETS_PER_RUN = 200
DEFAULT_MAX_PROFILES = 60
DEFAULT_MAX_COMPANIES = 40


class TargetRefused(RuntimeError):
    """FR-205: the URL is not on the plan's list, so the run will not open it."""


class ConfirmationRequired(RuntimeError):
    """FR-204: the estimated duration must be confirmed before the run starts."""


def terms_warning() -> str:
    """The CR-401 text shown before the first run."""
    return SITES[SITE].terms_warning


# ---------------------------------------------------------------------------
# The allowlist (FR-205)
# ---------------------------------------------------------------------------

_ALLOWED_HOSTS = SITES[SITE].allowed_hosts

#: The only page shapes the automation is allowed to open.
TARGET_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("profile", re.compile(r"^/in/[^/]+/?$", re.IGNORECASE)),
    ("company", re.compile(r"^/company/[^/]+(?:/(?:about|people|jobs)/?)?$", re.IGNORECASE)),
    ("people_search", re.compile(r"^/search/results/people/?$", re.IGNORECASE)),
    ("company_search", re.compile(r"^/search/results/companies/?$", re.IGNORECASE)),
    ("job_posting", re.compile(r"^/jobs/view/[^/]+/?$", re.IGNORECASE)),
    ("job_search", re.compile(r"^/jobs/search/?$", re.IGNORECASE)),
    ("school", re.compile(r"^/school/[^/]+(?:/people/?)?$", re.IGNORECASE)),
)

_TRACKING_PARAMS = re.compile(r"^(utm_|trk|trackingId|originalSubdomain|lipi|midToken|refId)")


def normalise_target_url(url: str) -> str:
    """Canonical form used for allowlist comparison.

    Search URLs keep the query - it *is* the query - but tracking parameters
    are dropped everywhere, both to make matching stable and because they
    carry session-scoped identifiers (NFR-203).
    """
    parsed = urlparse((url or "").strip())
    host = (parsed.netloc or "").lower()
    if host in {"linkedin.com", "be.linkedin.com", "nl.linkedin.com"}:
        host = "www.linkedin.com"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = "&".join(
        part
        for part in (parsed.query or "").split("&")
        if part and not _TRACKING_PARAMS.match(part.split("=", 1)[0])
    )
    rebuilt = f"https://{host}{path}"
    return f"{rebuilt}?{query}" if query else rebuilt


def classify_target(url: str) -> str | None:
    """The kind of page, or ``None`` when the URL is not an allowed shape."""
    parsed = urlparse(normalise_target_url(url))
    if parsed.netloc.lower() not in _ALLOWED_HOSTS:
        return None
    path = parsed.path or "/"
    for kind, pattern in TARGET_SHAPES:
        if pattern.match(path):
            return kind
    return None


class Allowlist:
    """The closed set of pages one run may open (FR-205)."""

    def __init__(self, targets: list[pacing_mod.Target]):
        self.targets = targets
        self._index = {normalise_target_url(t.url): t for t in targets}

    def __len__(self) -> int:
        return len(self.targets)

    def __iter__(self) -> Any:
        return iter(self.targets)

    def allows(self, url: str) -> bool:
        return normalise_target_url(url) in self._index

    def check(self, url: str) -> pacing_mod.Target:
        target = self._index.get(normalise_target_url(url))
        if target is None:
            raise TargetRefused(
                f"{safe_url(url)} is not on this campaign's LinkedIn target list; "
                "open-ended crawling is not permitted (FR-205)"
            )
        return target

    def as_dict(self) -> list[dict]:
        return [t.as_dict() for t in self.targets]


def make_target(url: str, *, label: str = "", plan_item_id: str | None = None,
                meta: dict | None = None) -> pacing_mod.Target:
    kind = classify_target(url)
    if kind is None:
        raise TargetRefused(
            f"{safe_url(url)} is not an allowed LinkedIn page shape "
            f"(profiles, company pages, people searches, job postings only) - FR-205"
        )
    return pacing_mod.Target(
        url=normalise_target_url(url),
        kind=kind,
        label=label or safe_url(url),
        plan_item_id=plan_item_id,
        meta=meta or {},
    )


_URL_KEYS = ("search_urls", "profile_urls", "company_urls", "job_urls", "urls")


def targets_from_plan_item(item: dict) -> list[pacing_mod.Target]:
    """Read one plan item's native query as a target list (FR-162, FR-166)."""
    query = item.get("native_query") or {}
    if isinstance(query, str):
        try:
            query = json.loads(query)
        except ValueError:
            query = {}
    out: list[pacing_mod.Target] = []
    for key in _URL_KEYS:
        for url in query.get(key) or []:
            try:
                out.append(targets_label(make_target(str(url), plan_item_id=item.get("id")), key))
            except TargetRefused as exc:
                log.info("Plan item %s: %s", item.get("id"), exc)
    for entry in query.get("targets") or []:
        if not isinstance(entry, dict) or not entry.get("url"):
            continue
        try:
            out.append(
                make_target(
                    str(entry["url"]),
                    label=str(entry.get("label") or ""),
                    plan_item_id=item.get("id"),
                    meta={"facet": entry.get("facet")},
                )
            )
        except TargetRefused as exc:
            log.info("Plan item %s: %s", item.get("id"), exc)
    return out


def targets_label(target: pacing_mod.Target, key: str) -> pacing_mod.Target:
    labels = {
        "search_urls": "people search",
        "profile_urls": "profile",
        "company_urls": "company page",
        "job_urls": "job posting",
    }
    return pacing_mod.Target(
        url=target.url,
        kind=target.kind,
        label=labels.get(key, target.label),
        plan_item_id=target.plan_item_id,
        meta=target.meta,
    )


def plan_caps(items: list[dict]) -> dict[str, int]:
    """FR-165 / FR-186 caps, taken from the plan and hard-limited by CR-406."""
    max_profiles = DEFAULT_MAX_PROFILES
    max_companies = DEFAULT_MAX_COMPANIES
    for item in items:
        caps = item.get("caps") or {}
        if isinstance(caps, dict):
            max_profiles = max(max_profiles, int(caps.get("max_profiles") or 0))
            max_companies = max(max_companies, int(caps.get("max_companies") or 0))
    return {
        "max_profiles": min(max_profiles, MAX_TARGETS_PER_RUN * 5),
        "max_companies": min(max_companies, MAX_TARGETS_PER_RUN * 5),
        "max_targets": MAX_TARGETS_PER_RUN,
    }


def linkedin_plan_items(campaign_id: str) -> list[dict]:
    """Plan items whose adapter is a LinkedIn source (FR-161, FR-205)."""
    catalogue = {c["adapter_key"]: c for c in campaign_repo.list_catalogue(enabled_only=False)}
    items = []
    for item in campaign_repo.list_plan_items(campaign_id, include_excluded=False):
        entry = catalogue.get(item["adapter_key"], {})
        if entry.get("source_type") == "linkedin" or item["adapter_key"] == ADAPTER_KEY:
            items.append(item)
    return items


def build_allowlist(campaign_id: str, extra_urls: list[str] | None = None) -> Allowlist:
    """The complete, closed target list for a campaign's LinkedIn run (FR-205)."""
    items = linkedin_plan_items(campaign_id)
    caps = plan_caps(items)
    targets: list[pacing_mod.Target] = []
    seen: set[str] = set()
    for item in items:
        for target in targets_from_plan_item(item):
            if target.url not in seen:
                seen.add(target.url)
                targets.append(target)
    for url in extra_urls or []:
        target = make_target(str(url), label="user-selected")
        if target.url not in seen:
            seen.add(target.url)
            targets.append(target)
    if len(targets) > caps["max_targets"]:
        log.info(
            "Truncating the LinkedIn target list from %d to %d (CR-406)",
            len(targets), caps["max_targets"],
        )
        targets = targets[: caps["max_targets"]]
    return Allowlist(targets)


# ---------------------------------------------------------------------------
# Extraction (FR-203, FR-183)
# ---------------------------------------------------------------------------

def _flatten_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "")
    if isinstance(value, list) and value:
        return _flatten_name(value[0])
    return str(value or "")


def parse_profile(html: str, url: str) -> dict:
    """A person's public profile page (FR-203)."""
    objects = json_ld(html)
    person = json_ld_of_type(objects, "Person") or {}
    works_for = person.get("worksFor")
    address = person.get("address") if isinstance(person.get("address"), dict) else {}
    data = {
        "profile_url": normalise_target_url(url),
        "full_name": tidy(str(person.get("name") or "")) or text_of(html, "h1"),
        "headline": tidy(str(person.get("jobTitle") or person.get("description") or ""))
        or text_of(html, "div.text-body-medium", ".top-card-layout__headline"),
        "company": _flatten_name(works_for),
        "location": tidy(
            str(address.get("addressLocality") or "")
            or str(address.get("addressRegion") or "")
        )
        or text_of(html, "span.text-body-small.inline", ".top-card__subline-item"),
        "country": str(address.get("addressCountry") or "") if address else "",
    }
    if isinstance(data["headline"], str) and "\n" in data["headline"]:
        data["headline"] = data["headline"].split("\n", 1)[0]
    return data


def parse_company(html: str, url: str) -> dict:
    """A company page: what LinkedIn shows about the organisation (FR-221 fields)."""
    objects = json_ld(html)
    org = json_ld_of_type(objects, "Organization", "Corporation") or {}
    address = org.get("address") if isinstance(org.get("address"), dict) else {}
    employees = org.get("numberOfEmployees")
    if isinstance(employees, dict):
        employees = employees.get("value") or employees.get("maxValue")
    size_text = text_of(
        html,
        ".org-top-card-summary-info-list__info-item",
        ".org-about-company-module__company-size-definition-text",
    )
    return {
        "linkedin_url": normalise_target_url(url),
        "name": tidy(str(org.get("name") or "")) or text_of(html, "h1"),
        "business_summary": tidy(str(org.get("description") or ""))
        or text_of(html, "p.break-words", ".org-about-us-organization-description__text"),
        "website": str(org.get("url") or "")
        or text_of(html, "a.org-top-card-primary-actions__action"),
        "size_fte": _int_or_none(employees),
        "size_text": size_text,
        "locality": str(address.get("addressLocality") or "") if address else "",
        "country": str(address.get("addressCountry") or "") if address else "",
        "industry": text_of(html, ".org-top-card-summary-info-list__info-item"),
    }


_PROFILE_HREF = re.compile(r'href="(https://[^"]*linkedin\.com)?(/in/[^"?#]+)', re.IGNORECASE)


def parse_people_search(html: str, url: str) -> list[dict]:
    """People-search results: name, headline and profile URL only (CR-402 minimisation)."""
    results: list[dict] = []
    seen: set[str] = set()
    if HTMLParser is None or not html:  # pragma: no cover
        return results
    tree = HTMLParser(html)
    containers = (
        tree.css("li.reusable-search__result-container")
        or tree.css("div.entity-result")
        or tree.css("li")
    )
    for node in containers:
        anchor = node.css_first('a[href*="/in/"]')
        if anchor is None:
            continue
        href = anchor.attributes.get("href") or ""
        match = _PROFILE_HREF.search(f'href="{href}"')
        if match is None:
            continue
        profile_url = normalise_target_url(f"https://www.linkedin.com{match.group(2)}")
        if profile_url in seen:
            continue
        name = tidy(anchor.text())
        if not name or name.lower() in {"linkedin member", "view profile"}:
            name = tidy((node.css_first("span[aria-hidden='true']") or anchor).text())
        headline_node = (
            node.css_first(".entity-result__primary-subtitle")
            or node.css_first(".subline-level-1")
            or node.css_first("div.t-14")
        )
        seen.add(profile_url)
        results.append(
            {
                "profile_url": profile_url,
                "full_name": name,
                "headline": tidy(headline_node.text()) if headline_node is not None else "",
                "source_search": safe_url(url),
            }
        )
    return results


def parse_job(html: str, url: str) -> dict:
    """A job posting page (FR-261 fields, collected through the browser)."""
    objects = json_ld(html)
    posting = json_ld_of_type(objects, "JobPosting") or {}
    location = posting.get("jobLocation")
    if isinstance(location, list) and location:
        location = location[0]
    address = {}
    if isinstance(location, dict):
        address = location.get("address") if isinstance(location.get("address"), dict) else {}
    return {
        "source_url": safe_url(url),
        "title": tidy(str(posting.get("title") or "")) or text_of(html, "h1"),
        "company_name_raw": _flatten_name(posting.get("hiringOrganization"))
        or text_of(html, ".topcard__org-name-link", "a.topcard__org-name-link"),
        "description": tidy(re.sub(r"<[^>]+>", " ", str(posting.get("description") or "")))
        or text_of(html, "div.description__text", "div.show-more-less-html__markup"),
        "location": tidy(str(address.get("addressLocality") or ""))
        or text_of(html, "span.topcard__flavor--bullet"),
        "country": str(address.get("addressCountry") or "") if address else "",
        "posted_at": str(posting.get("datePosted") or ""),
        "contract_type": _contract_type(str(posting.get("employmentType") or "")),
    }


def _contract_type(employment_type: str) -> str:
    mapping = {
        "FULL_TIME": "permanent",
        "PART_TIME": "permanent",
        "CONTRACTOR": "freelance",
        "TEMPORARY": "interim",
        "INTERN": "fixed_term",
    }
    return mapping.get(employment_type.upper(), "")


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# LLM fallback for changed markup (FR-183, NFR-205, NFR-403)
# ---------------------------------------------------------------------------

_LLM_SCHEMAS = {
    "profile": '{"full_name": str, "headline": str, "company": str, "location": str}',
    "company": '{"name": str, "business_summary": str, "industry": str, "size_text": str}',
    "job_posting": '{"title": str, "company_name_raw": str, "location": str, "description": str}',
}


def llm_extract(kind: str, text: str, llm: LLMClient | None) -> dict:
    """Last-resort extraction when the selectors miss (FR-183).

    The page is passed as isolated untrusted data, never concatenated into the
    instruction (NFR-205), and any failure degrades to "nothing extracted"
    rather than stopping the run (NFR-104).
    """
    if llm is None or kind not in _LLM_SCHEMAS or not text.strip():
        return {}
    try:
        if llm.budget.should_degrade():
            return {}
        data = llm.complete_json(
            f"extract.browser_{kind}",
            system=(
                "You extract structured facts from one page of a professional network. "
                "Use only what block 1 contains; never infer, never invent, and return "
                "empty strings for anything the page does not state."
            ),
            user=f"Extract the {kind.replace('_', ' ')} described in block 1.",
            untrusted={"page": text[:12_000]},
            schema_hint=_LLM_SCHEMAS[kind],
            prompt_template="browser.extract",
            prompt_version="v1",
            max_tokens=700,
        )
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 - extraction is best-effort
        log.info("LLM fallback for a %s page did not produce anything: %s", kind, exc)
        return {}


# ---------------------------------------------------------------------------
# Normalisation (FR-207, NFR-303)
# ---------------------------------------------------------------------------


def company_record(data: dict, url: str) -> NormalisedRecord | None:
    if not data.get("name"):
        return None
    return NormalisedRecord(
        entity_type="company",
        data={
            "name": data["name"],
            "business_summary": data.get("business_summary") or None,
            "country": (data.get("country") or "").upper()[:2] or None,
            "size_fte": data.get("size_fte"),
            "domain": _domain(data.get("website")),
            "source": f"linkedin:{safe_url(url)}",
            "access_method": "browser",  # FR-207
            "collected_at": utcnow(),
        },
        confidence=0.6,
    )


def vacancy_record(data: dict, url: str) -> NormalisedRecord | None:
    if not data.get("title"):
        return None
    return NormalisedRecord(
        entity_type="vacancy",
        data={
            "title": data["title"],
            "company_name_raw": data.get("company_name_raw") or None,
            "description": data.get("description") or None,
            "location": data.get("location") or None,
            "country": (data.get("country") or "").upper()[:2] or None,
            "contract_type": data.get("contract_type") or None,
            "posted_at": data.get("posted_at") or None,
            "source_url": safe_url(url),
            "source_adapter": ADAPTER_KEY,
            "access_method": "browser",  # FR-207
            "collected_at": utcnow(),
        },
        confidence=0.65,
    )


def contact_payload(
    person: dict, *, campaign_id: str, retention_until: str, company_id: str | None = None
) -> dict:
    """A person found in the browser: professional details only (CR-402, NFR-302).

    NFR-303 in three columns: not shareable, owned by this campaign, and with
    the date it must be gone by.
    """
    return {
        "full_name": person.get("full_name") or None,
        "role_title": person.get("headline") or None,
        "linkedin_url": person.get("profile_url") or None,
        "company_id": company_id,
        "source": person.get("source_search") or f"linkedin:{SITE}",
        "access_method": "browser",     # FR-207
        "shareable": 0,                 # NFR-303
        "owning_campaign_id": campaign_id,
        "retention_until": retention_until,
        "collected_at": utcnow(),
        "confidence": 0.5,
    }


def _domain(website: str | None) -> str | None:
    if not website:
        return None
    host = urlparse(website if "://" in website else f"https://{website}").netloc.lower()
    return host[4:] if host.startswith("www.") else host or None


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class RunTally:
    """Caps enforced while the run walks its list (FR-165, FR-186)."""

    max_profiles: int
    max_companies: int
    people: int = 0
    companies: int = 0
    vacancies: int = 0
    skipped_by_cap: int = 0

    def room_for_person(self) -> bool:
        return self.people < self.max_profiles

    def room_for_company(self) -> bool:
        return self.companies < self.max_companies

    def as_dict(self) -> dict[str, int]:
        return {
            "people": self.people,
            "companies": self.companies,
            "vacancies": self.vacancies,
            "skipped_by_cap": self.skipped_by_cap,
            "max_profiles": self.max_profiles,
            "max_companies": self.max_companies,
        }


class LinkedInCollector:
    """Turns visited pages into knowledge-base records, under the plan's caps."""

    def __init__(
        self,
        *,
        campaign_id: str,
        job_seeker_id: str,
        tally: RunTally,
        llm: LLMClient | None = None,
    ):
        self.campaign_id = campaign_id
        self.job_seeker_id = job_seeker_id
        self.tally = tally
        self.llm = llm
        self.writer = KnowledgeBaseWriter(
            adapter_key=ADAPTER_KEY, campaign_id=campaign_id
        )
        self.retention_until = repo.retention_until(campaign_id)

    async def handle(self, visit: pacing_mod.Visit) -> int:
        handler = {
            "profile": self._profile,
            "company": self._company,
            "company_search": self._people,
            "people_search": self._people,
            "school": self._people,
            "job_posting": self._job,
            "job_search": self._noop,
        }.get(visit.target.kind, self._noop)
        self.writer.plan_item_id = visit.target.plan_item_id
        return handler(visit)

    # -- per page kind ------------------------------------------------------
    def _noop(self, visit: pacing_mod.Visit) -> int:
        log.debug("Nothing to extract from %s", safe_url(visit.url))
        return 0

    def _profile(self, visit: pacing_mod.Visit) -> int:
        data = parse_profile(visit.html, visit.url)
        if not data.get("full_name"):
            data.update(llm_extract("profile", page_text(visit.html), self.llm))
        if not data.get("full_name"):
            return 0
        written = 0
        company_id = None
        if data.get("company") and self.tally.room_for_company():
            record = company_record({"name": data["company"]}, visit.url)
            outcome = self.writer.write(record) if record else None
            if outcome:
                company_id = outcome.entity_id
                self.tally.companies += 1
                written += 1
        if not self.tally.room_for_person():
            self.tally.skipped_by_cap += 1
            return written
        contact_id, created = repo.upsert_browser_contact(
            contact_payload(
                {
                    "full_name": data["full_name"],
                    "headline": data.get("headline"),
                    "profile_url": data["profile_url"],
                    "source_search": "linkedin:profile",
                },
                campaign_id=self.campaign_id,
                retention_until=self.retention_until,
                company_id=company_id,
            )
        )
        if contact_id:
            self.tally.people += 1 if created else 0
            written += 1
            kb_repo.record_provenance(
                "contact",
                contact_id,
                source_plan_item_id=visit.target.plan_item_id,
                raw_document_id=visit.raw_document_id,
                adapter_key=ADAPTER_KEY,
                confidence=0.5,
            )
        return written

    def _company(self, visit: pacing_mod.Visit) -> int:
        data = parse_company(visit.html, visit.url)
        if not data.get("name"):
            data.update(llm_extract("company", page_text(visit.html), self.llm))
        record = company_record(data, visit.url)
        if record is None or not self.tally.room_for_company():
            self.tally.skipped_by_cap += 0 if record is None else 1
            return 0
        outcome = self.writer.write(record)
        if outcome is None:
            return 0
        self.tally.companies += 1
        return 1

    def _people(self, visit: pacing_mod.Visit) -> int:
        """FR-165: the people behind a search - first degree, peers, alumni."""
        written = 0
        for person in parse_people_search(visit.html, visit.url):
            if not self.tally.room_for_person():
                self.tally.skipped_by_cap += 1
                break
            if not person.get("full_name"):
                continue
            contact_id, created = repo.upsert_browser_contact(
                contact_payload(
                    person,
                    campaign_id=self.campaign_id,
                    retention_until=self.retention_until,
                )
            )
            if not contact_id:
                continue
            self.tally.people += 1 if created else 0
            written += 1
            kb_repo.record_provenance(
                "contact",
                contact_id,
                source_plan_item_id=visit.target.plan_item_id,
                raw_document_id=visit.raw_document_id,
                adapter_key=ADAPTER_KEY,
                confidence=0.45,
            )
        return written

    def _job(self, visit: pacing_mod.Visit) -> int:
        data = parse_job(visit.html, visit.url)
        if not data.get("title"):
            data.update(llm_extract("job_posting", page_text(visit.html), self.llm))
        record = vacancy_record(data, visit.url)
        if record is None:
            return 0
        outcome = self.writer.write(record)
        if outcome is None:
            return 0
        self.tally.vacancies += 1
        return 1


# ---------------------------------------------------------------------------
# Confirmation, start, control (FR-204, FR-206, CR-401)
# ---------------------------------------------------------------------------


def acknowledgement_state(job_seeker_id: str) -> dict[str, Any]:
    """Whether the CR-401 warning has been acknowledged, and the text of it."""
    state = auth.consent_state(job_seeker_id).get(CONSENT_KIND, {})
    return {
        "kind": CONSENT_KIND,
        "acknowledged": bool(state.get("granted")),
        "decided_at": state.get("decided_at"),
        "warning": terms_warning(),
        "consent_text": state.get("text") or auth.CONSENT_KINDS[CONSENT_KIND],
    }


def record_acknowledgement(job_seeker_id: str, granted: bool, detail: str | None = None) -> dict:
    """Record the CR-401 acknowledgement before the first run."""
    auth.record_consent(job_seeker_id, CONSENT_KIND, granted, detail or terms_warning())
    record_audit(
        "browser.linkedin_terms_acknowledged" if granted else "browser.linkedin_terms_withdrawn",
        "job_seeker",
        job_seeker_id,
        seeker_id=job_seeker_id,
        detail={"site": SITE},
    )
    return acknowledgement_state(job_seeker_id)


def prepare_run(
    campaign_id: str, job_seeker_id: str, *, extra_urls: list[str] | None = None
) -> dict[str, Any]:
    """What the user confirms: the exact list, the warning and the duration (FR-204)."""
    campaign = campaign_repo.get_campaign(campaign_id, job_seeker_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id} for this job seeker")
    allowlist = build_allowlist(campaign_id, extra_urls)
    pacing = pacing_mod.Pacing.from_settings()
    estimator = pacing_mod.DurationEstimator(pacing, site=SITE)
    estimate = estimator.estimate(len(allowlist))
    return {
        "campaign_id": campaign_id,
        "site": SITE,
        "targets": allowlist.as_dict(),
        "target_count": len(allowlist),
        "caps": plan_caps(linkedin_plan_items(campaign_id)),
        "estimate": estimate.as_dict(),
        "pacing": {
            "min_delay_ms": pacing.min_delay_ms,
            "max_delay_ms": pacing.max_delay_ms,
            "scroll_steps": pacing.scroll_steps,
        },
        "acknowledgement": acknowledgement_state(job_seeker_id),
        "confirmation_required": True,  # FR-204
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
    """Start the run after the two gates: acknowledgement (CR-401) and confirmation (FR-204).

    ``driver``/``browser_family`` choose between the Chromium CDP attachment and
    the Playwright persistent context Firefox needs (FR-208).
    """
    auth.require_consent(job_seeker_id, CONSENT_KIND)  # CR-401: refuse without it
    if not confirmed:
        raise ConfirmationRequired(
            "The estimated duration must be confirmed before a browser run starts (FR-204)"
        )
    prepared = prepare_run(campaign_id, job_seeker_id, extra_urls=extra_urls)
    if not prepared["target_count"]:
        raise ValueError(
            "This campaign's plan holds no LinkedIn targets; open-ended crawling is not "
            "permitted, so there is nothing to run (FR-205)"
        )
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
            "caps": prepared["caps"],
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
        detail={
            "site": SITE,
            "campaign_id": campaign_id,
            "targets": prepared["target_count"],
            "estimated_seconds": prepared["estimate"]["seconds"],
        },
    )
    await runner.start(job_id, worker)
    return {"job_id": job_id, "driver": chosen_driver, "browser_family": family, **prepared}


async def worker(ctx: JobContext) -> None:
    """Execute one LinkedIn run (FR-203..FR-207)."""
    state = repo.job_state(ctx.job_id)
    campaign_id = ctx.campaign_id or state.get("campaign_id") or ""
    job_seeker_id = ctx.job_seeker_id or ""
    auth.require_consent(job_seeker_id, CONSENT_KIND)  # CR-401, re-checked at run time

    targets = [
        pacing_mod.Target(
            url=t["url"],
            kind=t.get("kind") or classify_target(t["url"]) or "profile",
            label=t.get("label", ""),
            plan_item_id=t.get("plan_item_id"),
            meta=t.get("meta") or {},
        )
        for t in state.get("targets") or []
    ]
    allowlist = Allowlist(targets)
    caps = state.get("caps") or {}
    tally = RunTally(
        max_profiles=int(caps.get("max_profiles") or DEFAULT_MAX_PROFILES),
        max_companies=int(caps.get("max_companies") or DEFAULT_MAX_COMPANIES),
    )
    done = int(state.get("done") or 0)  # NFR-401: resume where the run stopped
    remaining = list(allowlist)[done:]

    control = pacing_mod.register_run(ctx.job_id, SITE)
    pacing = pacing_mod.Pacing.from_settings()
    estimator = pacing_mod.DurationEstimator(pacing, site=SITE)
    collector = LinkedInCollector(
        campaign_id=campaign_id,
        job_seeker_id=job_seeker_id,
        tally=tally,
        llm=_llm_or_none(campaign_id, job_seeker_id),
    )

    chosen_driver, family = driver_choice(state.get("driver"), state.get("browser_family"))
    try:
        async with BrowserSession(driver=chosen_driver, family=family) as session:
            login = await session.check_login(SITE)
            if login["logged_in"] is False:
                raise BrowserUnavailable(
                    "The automation window is not signed in to LinkedIn. Sign in by hand "
                    "in that window and start the run again (FR-202)."
                )
            await session.bring_to_front()  # FR-206: the user watches
            run = pacing_mod.PacedRun(
                site=SITE,
                session=session,
                pacing=pacing,
                estimator=estimator,
                ctx=ctx,
                control=control,
                job_seeker_id=job_seeker_id,
                screenshots=True,
                done_offset=done,  # NFR-401: checkpoint absolute positions
            )
            report = await run.execute(remaining, collector.handle)
    except BrowserUnavailable as exc:
        repo.notify(
            job_seeker_id,
            "browser_unavailable",
            "The browser run could not start",
            str(exc),
            {"site": SITE, "campaign_id": campaign_id},
        )
        raise
    finally:
        pacing_mod.release_run(ctx.job_id)

    payload = {
        # The paced run has been checkpointing progress and the refined
        # estimate all along (NFR-401, FR-204); keep those, add the summary.
        **state,
        **repo.job_state(ctx.job_id),
        "done": done + len(report.outcomes),
        "report": report.as_dict(),
        "tally": tally.as_dict(),
        "writer": collector.writer.summary(),
        "finished_at": utcnow(),
    }
    repo.save_job_state(ctx.job_id, payload)
    record_audit(
        "browser.run_finished",
        "job_run",
        ctx.job_id,
        seeker_id=job_seeker_id,
        detail={
            "site": SITE,
            "campaign_id": campaign_id,
            "records": report.records,
            "stopped_reason": report.stopped_reason,
            "tally": tally.as_dict(),
        },
        actor="system",
    )


def _llm_or_none(campaign_id: str, job_seeker_id: str) -> LLMClient | None:
    """The LLM fallback is optional: no key, no consent, no fallback (NFR-104, CR-410)."""
    try:
        if not auth.has_consent(job_seeker_id, "llm_transfer"):
            return None
        return LLMClient(campaign_id=campaign_id, job_seeker_id=job_seeker_id)
    except Exception:  # noqa: BLE001
        log.info("No LLM fallback available for this run")
        return None


# ---------------------------------------------------------------------------
# Live status (FR-204, FR-206, NFR-502)
# ---------------------------------------------------------------------------


def run_status(job_id: str, job_seeker_id: str) -> dict[str, Any]:
    job = repo.get_job(job_id, job_seeker_id)
    if job is None:
        raise LookupError(f"No browser run {job_id} for this job seeker")
    state = repo.job_state(job_id)
    control = pacing_mod.control_for(job_id)
    return {
        "job_id": job_id,
        "site": state.get("site") or SITE,
        "campaign_id": job.get("campaign_id"),
        "status": job.get("status"),
        "progress_done": job.get("progress_done"),
        "progress_total": job.get("progress_total"),
        "error_count": job.get("error_count"),
        "last_error": job.get("last_error"),
        "estimate": state.get("estimate"),
        "outcomes": state.get("outcomes") or (state.get("report") or {}).get("outcomes") or [],
        "report": state.get("report"),
        "tally": state.get("tally"),
        "running": runner.is_running(job_id),
        "current_url": control.current_url if control else None,
        "skipped": sorted(control.skipped) if control else [],
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


@dataclass
class ControlResult:
    action: str
    applied: bool
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "applied": self.applied, "detail": self.detail, **self.extra}


def pause(job_id: str) -> ControlResult:
    return ControlResult("pause", runner.pause(job_id), "The run pauses before the next target")


def resume(job_id: str) -> ControlResult:
    return ControlResult("resume", runner.resume(job_id))


def cancel(job_id: str) -> ControlResult:
    return ControlResult("cancel", runner.cancel(job_id))


def skip(job_id: str, url: str) -> ControlResult:
    """FR-206: leave one target alone without stopping the run."""
    applied = pacing_mod.request_skip(job_id, normalise_target_url(url))
    return ControlResult(
        "skip",
        applied,
        "Skipped" if applied else "That run is not active in this process",
        {"url": normalise_target_url(url)},
    )
