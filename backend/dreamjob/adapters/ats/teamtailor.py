"""Teamtailor job-board adapter (FR-181, FR-261, FR-183, IR-102).

Endpoint (the tenant's own public RSS feed, keyless):

    GET https://{slug}.teamtailor.com/jobs.rss
    -> <rss xmlns:tt="https://teamtailor.com/locations"><channel>
         <title>{the employer}</title>
         <item>
           <title/> <description/>            (the advert, HTML-escaped HTML)
           <pubDate/> <link/> <guid/>
           <remoteStatus>hybrid</remoteStatus>
           <tt:locations><tt:location><tt:name/><tt:address/><tt:zip/>
             <tt:city/><tt:country/></tt:location></tt:locations>
           <tt:department/> <tt:role/>
         </item>

One request reads the whole board, which is why Campaign A prices Teamtailor at
one request per tenant (docs/Data_Gathering_Plan.md 2.4).  Each tenant is its
own host, so the per-domain limiter never makes two tenants wait for each
other - the vendor still sees one client IP, so the campaign must run them
inside one vendor-group bucket (plan N5, 6.5); this adapter does not fan out on
its own.

``{slug}.teamtailor.com/robots.txt`` disallows ``/app/``, ``/messages/``,
``/messenger/``, ``/facebook/tab/`` and ``/jobs/internal/`` only, so
``/jobs.rss`` is permitted (FR-182).

Two shapes here are not what the shared helpers expect and are converted before
they reach them:

* ``pubDate`` is RFC 822 (``"Wed, 24 Dec 2025 12:13:31 +0100"``).
  :func:`~dreamjob.adapters.vacancy_source.parse_datetime` reads ISO-8601 and
  epochs, so an unconverted feed date fell through to the relative-date parser
  and every Teamtailor posting was stored with ``posted_at`` NULL.
* ``<tt:role>`` states Teamtailor's own employment vocabulary ("Fixed contract",
  "Internship", ...), in which "Fixed contract" is not a phrase the shared
  contract map knows; it is translated here first and only then normalised.

``llm_fallback`` is False: the feed carries the full advert, so collection costs
0 tokens (plan 6.9).
"""

from __future__ import annotations

import logging
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

from dreamjob.adapters.ats.common import ATSAdapter
from dreamjob.adapters.base import PlanItem, RawRecord, register_adapter
from dreamjob.adapters.vacancy_source import (
    application_route,
    country_from_location,
    fte_percentage,
    html_to_text,
    normalise_contract_type,
    normalise_language,
    normalise_work_arrangement,
    parse_datetime,
    parse_salary_text,
    split_skills,
    title_matches,
)

log = logging.getLogger(__name__)

FEED = "https://{slug}.teamtailor.com/jobs.rss"
NS = "{https://teamtailor.com/locations}"

#: Teamtailor's ``remoteStatus`` vocabulary, spelled the way
#: ``normalise_work_arrangement`` reads it.
_REMOTE_WORDS = {
    "none": "on site",
    "hybrid": "hybrid",
    "temporary": "hybrid",
    "fully": "remote",
}
#: Teamtailor's ``<tt:role>`` vocabulary; "Fixed contract" and "Consultant"
#: are the two the shared contract map cannot read on its own.
_ROLE_WORDS = {
    "fixed contract": "fixed term",
    "consultant": "contractor",
    "temporary": "temporary",
    "internship": "internship",
    "full-time": "full-time",
    "part-time": "part-time",
    "volunteer": "volunteer",
}


@register_adapter
class TeamtailorAdapter(ATSAdapter):
    key = "ats.teamtailor"
    display_name = "Teamtailor"
    vendor = "teamtailor"
    coverage_countries: list[str] = []
    legal_notes = (
        "Public per-tenant RSS careers feed, no key required, read-only. "
        "robots.txt on a tenant host disallows /app/, /messages/, /messenger/, "
        "/facebook/tab/ and /jobs/internal/ only; /jobs.rss is permitted."
    )

    # -- fetch (IR-102, FR-182, FR-185) -------------------------------------
    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        records: list[RawRecord] = []
        for slug in self.slugs_of(item):
            url = FEED.format(slug=slug)
            result = await self._get(
                url, headers={"Accept": "application/rss+xml, application/xml"}
            )
            if result is None:
                continue
            if "<rss" not in result.text[:2000] and "<channel" not in result.text[:2000]:
                # A tenant that has moved answers 200 with an HTML shell; that
                # is a broken board, not a board with no vacancies (FR-185).
                log.warning("[%s] %s did not answer an RSS feed", self.key, url)
                self.fetch_outcome.ok -= 1
                self.fetch_outcome.failures.append((url, "not RSS"))
                continue
            records.append(
                RawRecord(
                    url=url,
                    content=result.text,
                    content_type="application/rss+xml",
                    raw_document_id=result.raw_document_id,
                    meta=self.board_meta(item, slug),
                )
            )
        return self.settle(records, nothing_to_fetch=self.no_board_named())

    # -- parse (FR-183) -----------------------------------------------------
    def parse(self, raw: RawRecord) -> list[dict]:
        root = ElementTree.fromstring(raw.content)
        channel = root.find("channel")
        if channel is None:
            return []
        # <channel><title> is the employer name, which is the one identity the
        # per-tenant feed carries and the plan item may not know.
        employer = (channel.findtext("title") or "").strip() or raw.meta.get("company_name")
        items = channel.findall("item")
        keywords = raw.meta.get("keywords") or []
        filtering = bool(raw.meta.get("title_filter"))
        out: list[dict] = []
        for entry in items[: raw.meta.get("max_records") or len(items)]:
            title = (entry.findtext("title") or "").strip()
            if not title:
                continue
            if filtering and not title_matches(title, keywords):
                continue
            out.append(self._one(entry, raw, employer))
        return out

    def _one(
        self, entry: ElementTree.Element, raw: RawRecord, employer: str | None
    ) -> dict[str, Any]:
        description = html_to_text(entry.findtext("description"))
        city, country_name, place = self._place(entry)
        role = (entry.findtext(f"{NS}role") or "").strip()
        role_word = _ROLE_WORDS.get(role.lower(), role)
        remote_status = (entry.findtext("remoteStatus") or "").strip().lower()
        link = (entry.findtext("link") or "").strip()
        channel_name, target = application_route(link, description)
        required, desirable = split_skills(description)
        salary_min, salary_max, currency = parse_salary_text(description[:6000])

        return {
            "company_id": raw.meta.get("company_id"),
            "company_name_raw": employer,
            "title": (entry.findtext("title") or "").strip(),
            "function_family": (entry.findtext(f"{NS}department") or "").strip() or None,
            "description": description,
            "required_skills": required,
            "desirable_skills": desirable,
            "location": place or None,
            "country": country_from_location(country_name, city),
            # The feed's own `remoteStatus` wins over prose in the advert.
            "work_arrangement": normalise_work_arrangement(
                _REMOTE_WORDS.get(remote_status, remote_status)
            ) or normalise_work_arrangement(description[:2000]),
            # <tt:role> is the employer's own statement; the advert body is a
            # fallback, never an override.
            "contract_type": normalise_contract_type(role_word)
            or normalise_contract_type(description[:2000]),
            "fte_percentage": fte_percentage(role_word) or fte_percentage(description[:1500]),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency,
            "posted_at": self._published(entry.findtext("pubDate")),
            "language": normalise_language(None, description),
            "application_channel": channel_name,
            "application_target": target,
            "source_url": link,
        }

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _place(entry: ElementTree.Element) -> tuple[str, str, str]:
        """``(city, country, "City, Country")`` from the first ``tt:location``."""
        holder = entry.find(f"{NS}locations")
        locations = list(holder) if holder is not None else []
        for location in locations:
            city = (location.findtext(f"{NS}city") or "").strip()
            country = (location.findtext(f"{NS}country") or "").strip()
            name = (location.findtext(f"{NS}name") or "").strip()
            place = ", ".join(part for part in (city or name, country) if part)
            if place:
                return city or name, country, place
        return "", "", ""

    @staticmethod
    def _published(value: str | None) -> str | None:
        """RFC 822 first, then whatever ``parse_datetime`` already understands."""
        text = (value or "").strip()
        if not text:
            return None
        try:
            return parse_datetime(parsedate_to_datetime(text).isoformat())
        except (TypeError, ValueError):
            return parse_datetime(text)
