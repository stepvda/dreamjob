"""SmartRecruiters job-board adapter (FR-181, FR-261, IR-102).

Endpoints (public "Posting API", keyless):

    GET https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset=0
    GET https://api.smartrecruiters.com/v1/companies/{slug}/postings/{id}

The list call is paginated (``offset``/``limit``/``totalFound``) and carries
structured metadata only; the advert itself lives on the detail call under
``jobAd.sections`` (companyDescription, jobDescription, qualifications,
additionalInformation).  Qualifications are a genuine "required skills"
section, so they drive ``required_skills`` rather than a whole-page guess.

**This source is catalogued as disabled** (FR-182, IR-101).
``api.smartrecruiters.com/robots.txt`` reads ``User-agent: * / Disallow: /``
and allows ``/v1/companies/`` to ``LinkedInBot`` alone, so the egress layer
refuses every call above and the adapter can only ever collect nothing.  It
stayed in the catalogue as an enabled, keyless API that quietly returned no
vacancies, which is the one thing the FR-185 dashboard must not say; see
:meth:`SmartRecruitersAdapter.register`.  The ~1,000 boards behind this vendor
are reachable only by writing an HTML adapter against
``jobs.smartrecruiters.com/{slug}`` (whose robots.txt is a 404, i.e. permitted)
or by an arrangement with the ATS or the employer.  Sending a ``LinkedInBot``
User-Agent to get at the API is not an option this product takes
(docs/Data_Gathering_Plan.md section 6.2).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from dreamjob.adapters.ats.common import ATSAdapter
from dreamjob.adapters.base import (
    AdapterCapabilities,
    PlanItem,
    RawRecord,
    ToSStatus,
    register_adapter,
)
from dreamjob.adapters.vacancy_source import (
    application_route,
    country_from_location,
    extract_skills,
    fte_percentage,
    html_to_text,
    normalise_contract_type,
    normalise_language,
    normalise_work_arrangement,
    parse_datetime,
    parse_salary_text,
    requested_page,
    split_skills,
    title_matches,
)
from dreamjob.db.connection import execute

log = logging.getLogger(__name__)

LIST_API = (
    "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    "?limit={limit}&offset={offset}"
)
DETAIL_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}"
PAGE_SIZE = 100


@register_adapter
class SmartRecruitersAdapter(ATSAdapter):
    key = "ats.smartrecruiters"
    display_name = "SmartRecruiters"
    vendor = "smartrecruiters"
    tos_status = ToSStatus.RESTRICTED
    requires_ack = True
    legal_notes = (
        "Disabled: api.smartrecruiters.com/robots.txt reads 'User-agent: * / Disallow: /' "
        "and allows /v1/companies/ for LinkedInBot only. FR-182 makes that binding, so "
        "every call is refused at the egress layer and this source collects nothing. It "
        "stays off until an administrator has an arrangement with the ATS or the employer "
        "and acknowledges it (IR-101); jobs.smartrecruiters.com serves no robots.txt and "
        "would need an HTML adapter."
    )
    capabilities = AdapterCapabilities(
        keyword_search=False, company_lookup=True, pagination=True, max_results_per_query=500
    )

    def register(self) -> None:
        """FR-161/FR-182: catalogue this source as disabled, with the reason.

        ``register()`` writes the terms-of-service status and the legal notes but
        leaves ``enabled`` at its default of 1, so the FR-185 dashboard listed a
        keyless API that every campaign planned and no campaign could ever read:
        a source that reports zero vacancies because robots.txt refuses it looks
        exactly like a source with no vacancies.

        The administrator's own decision wins: once the source is acknowledged
        (IR-101) this leaves ``enabled`` alone, so an arrangement with the ATS or
        the employer is not undone by the next restart.
        """
        super().register()
        changed = execute(
            "UPDATE source_catalogue SET enabled = 0 "
            "WHERE adapter_key = ? AND acknowledged_at IS NULL AND enabled = 1",
            (self.key,),
        )
        if changed:
            log.info(
                "[%s] catalogued as disabled: api.smartrecruiters.com/robots.txt disallows "
                "every user agent but LinkedInBot, so the egress layer refuses these calls "
                "(FR-182, IR-101)", self.key,
            )

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        query = self.native_query(item)
        limit = self.limit_of(item)
        keywords = query.get("keywords") or []
        filtering = bool(query.get("title_filter"))
        want_details = query.get("fetch_details", True) is not False
        one_page = requested_page(query)

        records: list[RawRecord] = []
        for slug in self.slugs_of(item):
            meta_base = self.board_meta(item, slug)

            # Each summary keeps its listing page's document id so a record built
            # from the summary alone still carries provenance (FR-183).
            summaries: list[tuple[dict, str | None]] = []
            offset = (one_page - 1) * PAGE_SIZE if one_page else 0
            budget = min(limit, PAGE_SIZE) if one_page else limit
            while len(summaries) < budget:
                page = min(PAGE_SIZE, budget - len(summaries))
                url = LIST_API.format(slug=slug, limit=page, offset=offset)
                page_result = await self._get(url)
                if page_result is None:
                    break
                try:
                    payload = json.loads(page_result.text)
                except ValueError:
                    break
                content = payload.get("content") or []
                summaries.extend((posting, page_result.raw_document_id) for posting in content)
                offset += page
                if len(content) < page or offset >= int(payload.get("totalFound") or 0):
                    break
                if one_page:
                    break

            if filtering:
                summaries = [
                    (summary, doc_id)
                    for summary, doc_id in summaries
                    if title_matches(str(summary.get("name") or ""), keywords)
                ]

            for summary, listing_doc_id in summaries[:budget]:
                detail_url = DETAIL_API.format(slug=slug, posting_id=summary.get("id"))
                if not want_details:
                    records.append(
                        RawRecord(
                            url=detail_url,
                            content=json.dumps(summary),
                            content_type="application/json",
                            raw_document_id=listing_doc_id,
                            meta={**meta_base, "kind": "summary"},
                        )
                    )
                    continue
                detail = await self._get(detail_url)
                if detail is None:
                    continue
                records.append(
                    RawRecord(
                        url=detail_url,
                        content=detail.text,
                        content_type="application/json",
                        raw_document_id=detail.raw_document_id,
                        meta={**meta_base, "kind": "detail", "summary": summary},
                    )
                )
        return self.settle(records, nothing_to_fetch=self.no_board_named())

    def parse(self, raw: RawRecord) -> list[dict]:
        posting = json.loads(raw.content)
        if raw.meta.get("kind") == "summary":
            return [self._one(posting, {}, raw)]
        return [self._one(posting, raw.meta.get("summary") or {}, raw)]

    @staticmethod
    def _sections(posting: dict) -> dict[str, str]:
        sections = ((posting.get("jobAd") or {}).get("sections") or {})
        return {
            name: html_to_text((block or {}).get("text"))
            for name, block in sections.items()
            if isinstance(block, dict)
        }

    def _one(self, posting: dict, summary: dict, raw: RawRecord) -> dict[str, Any]:
        sections = self._sections(posting)
        description = "\n\n".join(
            part
            for part in (
                sections.get("jobDescription"),
                sections.get("qualifications"),
                sections.get("additionalInformation"),
            )
            if part
        )
        qualifications = sections.get("qualifications") or ""
        location = posting.get("location") or summary.get("location") or {}
        location_text = location.get("fullLocation") or ", ".join(
            str(p) for p in (location.get("city"), location.get("region")) if p
        )
        employment = (posting.get("typeOfEmployment") or summary.get("typeOfEmployment") or {})
        department = (posting.get("department") or summary.get("department") or {})
        experience = (posting.get("experienceLevel") or summary.get("experienceLevel") or {})
        language = (posting.get("language") or {}).get("code")

        apply_url = posting.get("applyUrl") or posting.get("postingUrl") or raw.url
        channel, target = application_route(apply_url, description)
        required, desirable = split_skills(description)
        if qualifications:
            stated = extract_skills(qualifications)
            required = stated or required
            desirable = [s for s in desirable if s not in required]
        salary_min, salary_max, currency = parse_salary_text(description[:6000])

        remote_flag = "remote" if location.get("remote") else ""
        hybrid_flag = "hybrid" if location.get("hybrid") else ""
        return {
            "company_id": raw.meta.get("company_id"),
            "company_name_raw": (posting.get("company") or {}).get("name")
            or raw.meta.get("company_name"),
            "title": posting.get("name") or summary.get("name"),
            "function_family": department.get("label"),
            "seniority": experience.get("label"),
            "description": description,
            "required_skills": required,
            "desirable_skills": desirable,
            "location": location_text or None,
            "country": country_from_location(location.get("country"), location_text),
            "work_arrangement": normalise_work_arrangement(
                hybrid_flag, remote_flag, location_text, description[:2000]
            ),
            "contract_type": normalise_contract_type(
                employment.get("label"), employment.get("id"), description[:2000]
            ),
            "fte_percentage": fte_percentage(employment.get("label"), description[:1500]),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency,
            "posted_at": parse_datetime(
                posting.get("releasedDate") or summary.get("releasedDate")
            ),
            "application_channel": channel,
            "application_target": target,
            "source_url": posting.get("postingUrl") or apply_url,
            "language": normalise_language(language, description),
        }
