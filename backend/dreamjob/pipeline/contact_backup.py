"""Backup contact discovery when the normal ladder finds nothing (FR-303).

Measured on the live corpus: 3,350 of 5,960 companies (56%) have no usable
contact e-mail, 3,324 of them are recorded ``unreachable``, and 97.7% of those
have no employer domain at all.  The ladder in
:mod:`dreamjob.pipeline.apply_contacts` is right to refuse to spell an address
on a board host or on a domain the identity gate never confirmed - but it also
gives up, while two sources still hold an address the employer itself wrote
down: the postings the corpus already stores, and the applicant-tracking board
the employer uses.  This module reads those, and only those, when everything
else has failed.  It is a *source*, not a second ladder: nothing is stored and
nothing is decided here, the findings go back to the caller's FR-301 ranking,
FR-304 validation and NFR-302 checks.

Sources, in the order tried:

``stored_document``
    The ``application_target`` and ``description`` of the company's stored
    postings, plus the URL fields (``source_url``, ``application_target`` and a
    JSON ``application_url``) for the employer's registrable domain.  No
    network: the text is already in the database.
``ats_board``
    The career board resolved from ``company.careers_url``, from
    ``detect.board_url(ats_vendor, ats_slug)``, or from the most common vacancy
    ``source_url`` host.  At most ``max_pages`` pages, fetched through the
    shared :class:`~dreamjob.egress.client.EgressClient` so robots.txt, pacing
    and caching apply exactly as everywhere else (FR-182, CR-402).  E-mails are
    read from the text and from JSON-LD, and an employer domain is recovered
    from ``hiringOrganization.url``/``sameAs`` and from links that are not
    boards, free mailboxes or page-chrome noise.
``website`` / ``pattern_inference``
    When a domain was recovered and ``crawl_site`` is on, the ordinary
    :func:`~dreamjob.pipeline.email_patterns.collect_from_site` and
    :func:`~dreamjob.pipeline.email_patterns.generic_candidates` run on it and
    keep their own labels.  An address printed on the site is published and is
    not uncertain; a composed generic stays ``pattern_inference`` and stays
    uncertain (FR-303, migration 151).

Bounds and politeness: one egress client, ``max_pages`` board pages and
``max_pages`` crawl pages per company, at most ``STORED_POSTING_LIMIT`` stored
postings, one log line per company.  Nothing is written.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from dreamjob.adapters.ats import detect
from dreamjob.adapters.website import crawler
from dreamjob.db.repositories import apply as repo
from dreamjob.db.repositories import contacts as contacts_repo
from dreamjob.egress.client import EgressClient, RobotsDisallowed
from dreamjob.pipeline import email_patterns as patterns

log = logging.getLogger(__name__)

#: How many pages of a board are read, ever, per company.
DEFAULT_MAX_PAGES = 4

#: How many stored postings are read (the repository bound).
STORED_POSTING_LIMIT = 50

#: JSON keys an ATS payload uses for the employer's own site.
_EMPLOYER_URL_KEYS = ("application_url", "applicationUrl", "url")

#: Hosts that appear in every page's chrome - socials, CDNs, analytics - and
#: are never the employer's own domain.  A link to one of these is not evidence
#: that the employer owns it.
_LINK_NOISE_DOMAINS = frozenset(
    {
        "google.com", "googleapis.com", "gstatic.com", "google-analytics.com",
        "googletagmanager.com", "doubleclick.net", "recaptcha.net", "hcaptcha.com",
        "facebook.com", "fb.com", "twitter.com", "x.com", "youtube.com",
        "instagram.com", "linkedin.com", "pinterest.com", "tiktok.com", "vimeo.com",
        "wordpress.org", "jquery.com", "cloudflare.com", "cloudflareinsights.com",
        "bootstrapcdn.com", "fontawesome.com", "w3.org", "schema.org",
        "apple.com", "microsoft.com", "amazon.com", "amazonaws.com", "sentry.io",
    }
)


@dataclass
class BackupFinding:
    """One address the backup stage found, and why it believes it."""

    address: patterns.FoundAddress
    rationale: str


def _employer_domain(value: str | None) -> str:
    """The registrable employer domain of a URL, or ``""``.

    :func:`~dreamjob.pipeline.apply_contacts.usable_employer_domain` owns the
    aggregator and free-mailbox exclusions, and it is imported lazily because
    ``apply_contacts`` imports this module.
    """
    from dreamjob.pipeline.apply_contacts import usable_employer_domain  # noqa: PLC0415

    raw = str(value or "").strip()
    if not raw.lower().startswith(("http://", "https://", "//")):
        return ""
    host = patterns.domain_of(raw)
    if not host:
        return ""
    return usable_employer_domain(crawler.registrable_domain(host) or host)


def _is_employer_address(address: patterns.FoundAddress) -> bool:
    """Whether an address on a shared source is plausibly the employer's.

    The same rule rung 2 of the ladder applies to a posting body: a board's own
    ``help@`` or an EU-portal address is boilerplate, while a small employer
    really does apply from a free mailbox, so free mail is kept.
    """
    from dreamjob.pipeline.apply_contacts import (  # noqa: PLC0415
        FREEMAIL_DOMAINS,
        usable_employer_domain,
    )

    domain = address.domain.lower()
    return bool(usable_employer_domain(domain)) or domain in FREEMAIL_DOMAINS


def _vendor_domain(domain: str | None) -> str:
    """The board/aggregator domain an address host belongs to, or ``""``."""
    from dreamjob.pipeline.apply_contacts import AGGREGATOR_DOMAINS  # noqa: PLC0415

    value = (domain or "").strip().lower().lstrip(".")
    if not value:
        return ""
    registrable = crawler.registrable_domain(value)
    if value in AGGREGATOR_DOMAINS:
        return value
    if registrable in AGGREGATOR_DOMAINS:
        return registrable
    return ""


def _known_employer_domain(company: dict[str, Any]) -> str:
    """The employer's own domain already on the company row, if it has one.

    This is the exemption the vendor filter uses: an address on a board's own
    domain is dropped *unless* that domain is the employer's, which is the one
    case where the board and the employer are the same organisation.
    """
    from dreamjob.pipeline.apply_contacts import usable_employer_domain  # noqa: PLC0415

    for raw in (
        company.get("domain"),
        company.get("company_domain"),
        company.get("careers_url"),
    ):
        value = str(raw or "").strip()
        if not value:
            continue
        if not value.lower().startswith(("http://", "https://", "//")):
            value = f"https://{value}"
        host = patterns.domain_of(value)
        if not host:
            continue
        known = usable_employer_domain(crawler.registrable_domain(host) or host)
        if known:
            return known
    return ""


def _attachable(address: patterns.FoundAddress, employer_domain: str) -> bool:
    """Whether an address may be attributed to the employer.

    An address on a board or aggregator host is the platform's own boilerplate
    - Personio's ``privacy@``, Greenhouse's ``help@`` - and attaching it to a
    tenant would send the job seeker to the vendor.  The one exception is a
    board host that *is* the employer's own domain, which
    :func:`_known_employer_domain` can only say when the company record already
    carries it.
    """
    vendor = _vendor_domain(address.domain)
    return bool(not vendor or (employer_domain and vendor == employer_domain))


def _url_strings(value: Any, depth: int = 0) -> list[str]:
    """Every URL-ish string in a JSON-LD node, links included."""
    if depth > 4:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for key in ("url", "@id", "sameAs"):
            item = value.get(key)
            if isinstance(item, str):
                out.append(item)
        for item in value.values():
            if isinstance(item, (dict, list)):
                out.extend(_url_strings(item, depth + 1))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_url_strings(item, depth + 1))
        return out
    return []


def _embedded_urls(text: str) -> list[str]:
    """``application_url`` and friends out of a JSON payload stored as text."""
    stripped = (text or "").strip()
    if not stripped.startswith(("{", "[")):
        return []
    try:
        payload = json.loads(stripped)
    except (ValueError, TypeError):
        return []

    def walk(node: Any, depth: int = 0) -> list[str]:
        if depth > 4:
            return []
        if isinstance(node, dict):
            out: list[str] = []
            for key, value in node.items():
                if key in _EMPLOYER_URL_KEYS and isinstance(value, str):
                    out.append(value)
                elif isinstance(value, (dict, list)):
                    out.extend(walk(value, depth + 1))
            return out
        if isinstance(node, list):
            out = []
            for item in node:
                out.extend(walk(item, depth + 1))
            return out
        return []

    return walk(payload)


def _domain_from_vacancy(row: dict[str, Any]) -> str:
    """The employer domain a stored posting's URL fields spell, if any."""
    for value in (row.get("source_url"), row.get("application_target")):
        domain = _employer_domain(value if isinstance(value, str) else "")
        if domain:
            return domain
    for raw in (row.get("application_target"), row.get("description")):
        if isinstance(raw, str):
            for value in _embedded_urls(raw):
                domain = _employer_domain(value)
                if domain:
                    return domain
    return ""


def _stored_findings(rows: list[dict[str, Any]]) -> tuple[list[BackupFinding], str]:
    """Addresses and a domain already in the database.  No network."""
    findings: list[BackupFinding] = []
    domain = ""
    for row in rows:
        source_url = str(row.get("source_url") or "")
        target = str(row.get("application_target") or "")
        for text in (target, str(row.get("description") or "")):
            if not text or "@" not in text:
                continue
            for address in patterns.addresses_in_text(
                text,
                source_url=source_url,
                method=patterns.METHOD_STORED_DOCUMENT,
            ):
                if not patterns.is_plausible_address(address.email):
                    continue
                if not _is_employer_address(address):
                    continue
                findings.append(
                    BackupFinding(
                        address,
                        "Published in the employer's stored posting (FR-303 stored_document)",
                    )
                )
        if not domain:
            domain = _domain_from_vacancy(row)
    return findings, domain


def _most_common_source_host(rows: list[dict[str, Any]]) -> str:
    counter: Counter[str] = Counter()
    for row in rows:
        url = str(row.get("source_url") or "")
        if url.lower().startswith(("http://", "https://")):
            host = urlparse(url).netloc.lower()
            if host:
                counter[host] += 1
    return counter.most_common(1)[0][0] if counter else ""


def _ats_vendor_slug(company: dict[str, Any]) -> tuple[str, str]:
    vendor = str(company.get("ats_vendor") or "").strip()
    slug = str(company.get("ats_slug") or "").strip()
    if (vendor and slug) or not company.get("company_id"):
        return vendor, slug
    row = contacts_repo.get_company(str(company["company_id"])) or {}
    return (
        vendor or str(row.get("ats_vendor") or "").strip(),
        slug or str(row.get("ats_slug") or "").strip(),
    )


def _board_urls(company: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    """The board/careers pages to read, in the order the brief fixes."""
    candidates: list[str] = []
    careers = str(company.get("careers_url") or "").strip()
    if careers.lower().startswith(("http://", "https://")):
        candidates.append(careers)
    vendor, slug = _ats_vendor_slug(company)
    board = detect.board_url(vendor, slug)
    if board:
        candidates.append(board)
    host = _most_common_source_host(rows)
    if host:
        candidates.append(f"https://{host}")
    ordered: list[str] = []
    for url in candidates:
        if url not in ordered:
            ordered.append(url)
    return ordered


def _name_match(domain: str, company_name: str | None) -> int:
    """Whether the domain's label carries a word of the company's name."""
    tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", (company_name or "").lower())
        if len(token) >= 3
    }
    label = domain.split(".")[0]
    return 1 if any(token in label for token in tokens) else 0


def _page_domains(html: str, page_url: str, company_name: str | None) -> list[tuple[str, int]]:
    """``(domain, weight)`` pairs a board page names as its employer."""
    out: list[tuple[str, int]] = []
    board = crawler.registrable_domain(urlparse(page_url).netloc)
    for node in patterns.jsonld_payloads(html):
        if not isinstance(node, dict):
            continue
        for key in ("hiringOrganization", "sameAs", "url"):
            for raw in _url_strings(node.get(key)):
                domain = _employer_domain(raw)
                if domain and domain != board and domain not in _LINK_NOISE_DOMAINS:
                    out.append((domain, 3))
    for url, _anchor in crawler.extract_links(html, page_url):
        domain = crawler.registrable_domain(urlparse(url).netloc)
        if not domain or domain == board or domain in _LINK_NOISE_DOMAINS:
            continue
        domain = _employer_domain(f"https://{domain}")
        if domain:
            out.append((domain, 1))
    return out


async def _board_findings(
    company: dict[str, Any],
    urls: list[str],
    *,
    egress: EgressClient,
    max_pages: int,
) -> tuple[list[BackupFinding], str]:
    """Read at most ``max_pages`` board pages; return addresses and a domain."""
    findings: list[BackupFinding] = []
    domains: Counter[str] = Counter()
    pages = 0
    for url in urls[: max(0, max_pages)]:
        try:
            page = await egress.fetch(url)
        except RobotsDisallowed:
            log.info("robots.txt disallows %s; backup board skipped (CR-402)", url)
            continue
        except Exception as exc:  # noqa: BLE001 - one dead page must not stop the rest
            log.debug("Backup board fetch failed for %s: %s", url, exc)
            continue
        if not page.ok or patterns.looks_like_challenge(page.text):
            continue
        pages += 1
        html = page.text
        text = crawler.extract_text(html, drop_chrome=False)
        board = crawler.registrable_domain(urlparse(url).netloc)
        page_addresses: list[patterns.FoundAddress] = []
        for address in patterns.addresses_in_text(
            f"{html}\n{text}", source_url=url, method=patterns.METHOD_ATS_BOARD
        ):
            page_addresses.append(address)
            if not patterns.is_plausible_address(address.email):
                continue
            if _is_employer_address(address):
                findings.append(
                    BackupFinding(
                        address,
                        "Published on the employer's ATS board (FR-303 ats_board)",
                    )
                )
        for address in patterns.jsonld_contacts(html, source_url=url):
            page_addresses.append(address)
            if not patterns.is_plausible_address(address.email):
                continue
            if not _is_employer_address(address):
                continue
            address.method = patterns.METHOD_ATS_BOARD
            address.confidence = patterns.METHOD_CONFIDENCE[patterns.METHOD_ATS_BOARD]
            findings.append(
                BackupFinding(
                    address,
                    "Published in the board's structured data (FR-303 ats_board)",
                )
            )
        # An address the board publishes is the strongest domain evidence it
        # carries: ``jobs@dck.com`` names ``dck.com`` even when every link on
        # the page points at a platform rebrand.  It outweighs the JSON-LD URLs
        # and the ordinary links, and a vendor address - rejected by
        # ``_employer_domain`` - can never name the employer.
        for address in page_addresses:
            if not patterns.is_plausible_address(address.email):
                continue
            domain = _employer_domain(f"https://{address.domain}")
            if domain and domain != board:
                domains[domain] += 4
        for domain, weight in _page_domains(html, url, company.get("company_name")):
            domains[domain] += weight

    recovered = ""
    if domains:
        recovered = max(
            domains.items(),
            key=lambda item: (
                item[1],
                _name_match(item[0], company.get("company_name")),
            ),
        )[0]
    log.debug("Backup board for %s: %d page(s), domain %r", company.get("company_id"), pages, recovered)
    return findings, recovered


async def _crawl_findings(
    domain: str,
    company: dict[str, Any],
    *,
    egress: EgressClient,
    max_pages: int,
) -> list[BackupFinding]:
    """The employer site's published addresses and its conventional mailboxes."""
    findings: list[BackupFinding] = []
    try:
        harvested = await patterns.collect_from_site(
            domain,
            careers_url=company.get("careers_url"),
            egress=egress,
            max_pages=max(0, max_pages),
        )
    except Exception:  # noqa: BLE001 - the site is optional evidence
        log.info("Backup could not read %s for addresses", domain, exc_info=True)
        harvested = []
    for address in harvested:
        findings.append(
            BackupFinding(
                address,
                f"Published on the employer's own site {domain} (FR-303 website)",
            )
        )
    for guess in patterns.generic_candidates(domain, careers_url=company.get("careers_url")):
        findings.append(
            BackupFinding(
                guess,
                f"Conventional careers mailbox on {domain}, unverified (FR-303 inference)",
            )
        )
    return findings


def _dedupe(findings: list[BackupFinding]) -> list[BackupFinding]:
    """One finding per address, keeping the method with the most confidence."""
    out: dict[str, BackupFinding] = {}
    for finding in findings:
        current = out.get(finding.address.email)
        if current is None or finding.address.confidence > current.address.confidence:
            out[finding.address.email] = finding
    return list(out.values())


async def harvest_backup_addresses(
    company: dict[str, Any],
    *,
    egress: EgressClient | None = None,
    crawl_site: bool = True,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> tuple[list[BackupFinding], str | None, str]:
    """Find addresses the normal ladder could not, from the backup sources.

    Returns ``(findings, employer_domain, note)``.  The caller decides what to
    validate and what to store; this function fetches through ``egress`` (or a
    short-lived client when none is given), never writes, and logs exactly one
    summary line per company.
    """
    max_pages = max(0, int(max_pages))
    company_id = str(company.get("company_id") or "")
    rows: list[dict[str, Any]] = []
    if company_id:
        try:
            rows = await asyncio.to_thread(
                repo.vacancies_for_company, company_id, STORED_POSTING_LIMIT
            )
        except Exception:  # noqa: BLE001 - backup must never fail a caller
            log.info("Backup could not read stored postings for %s", company_id, exc_info=True)

    findings, stored_domain = _stored_findings(rows)
    urls = _board_urls(company, rows)

    if urls:
        if egress is not None:
            board_findings, board_domain = await _board_findings(
                company, urls, egress=egress, max_pages=max_pages
            )
        else:
            async with EgressClient(store_raw=False) as client:
                board_findings, board_domain = await _board_findings(
                    company, urls, egress=client, max_pages=max_pages
                )
        findings.extend(board_findings)
    else:
        board_domain = ""

    domain = board_domain or stored_domain
    if domain and crawl_site:
        if egress is not None:
            findings.extend(
                await _crawl_findings(domain, company, egress=egress, max_pages=max_pages)
            )
        else:
            async with EgressClient(store_raw=False) as client:
                findings.extend(
                    await _crawl_findings(domain, company, egress=client, max_pages=max_pages)
                )

    # One last gate over every source: nothing an extraction turned into junk,
    # and nothing that belongs to a board or aggregator the employer does not
    # own - ``privacy@personio.com`` must not attach to a Personio tenant just
    # because the board published it (FR-303).
    employer_domain = _known_employer_domain(company)
    findings = [
        finding
        for finding in findings
        if patterns.is_plausible_address(finding.address.email)
        and _attachable(finding.address, employer_domain)
    ]

    findings = _dedupe(findings)
    counts = Counter(finding.address.method for finding in findings)
    summary = ", ".join(f"{method}={count}" for method, count in sorted(counts.items()))
    log.info(
        "Backup contacts for %s: %s%s",
        company_id or company.get("company_name") or "?",
        summary or "nothing",
        f" (employer domain {domain})" if domain else "",
    )
    if findings:
        note = f"backup found {summary}"
        if domain:
            note += f" (employer domain {domain})"
    else:
        note = "backup ran; no address found in the stored postings or on the ATS board"
        if domain:
            note += f" (employer domain {domain})"
    return findings, (domain or None), note
