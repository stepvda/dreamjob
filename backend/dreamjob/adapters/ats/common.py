"""Shared behaviour for applicant-tracking-system adapters (FR-181, FR-261, IR-102).

An ATS board is the freshest and cleanest description of what a company is
actually hiring for: it is the employer's own system of record, it is public,
and it is JSON.  Every vendor here exposes a keyless read-only endpoint, so the
adapters are ``AccessMethod.API`` and cost nothing per call.

All seven work the same way: the planner hands over a company and its board
slug (``company_lookup``), the adapter reads the whole board, and each posting
becomes one ``vacancy`` row.  Slugs come from ``company.ats_vendor`` /
``company.ats_slug``, which the company-profiling slice fills in using
:func:`dreamjob.adapters.ats.detect.detect_ats`.
"""

from __future__ import annotations

import logging
from typing import Any

from dreamjob.adapters.base import AccessMethod, AdapterCapabilities, PlanItem, SourceType
from dreamjob.adapters.vacancy_source import VacancySourceAdapter, keywords_from
from dreamjob.egress.client import FetchResult, RobotsDisallowed

log = logging.getLogger(__name__)

DEFAULT_MAX_RECORDS = 500


class ATSAdapter(VacancySourceAdapter):
    """Company-board adapter: one plan item per company slug."""

    source_type = SourceType.ATS
    access_method = AccessMethod.API
    capabilities = AdapterCapabilities(
        keyword_search=False,       # a board lists everything; filtering is client-side
        location_filter=False,
        company_lookup=True,
        pagination=False,
        max_results_per_query=DEFAULT_MAX_RECORDS,
    )
    rate_limit_rps = 1.0
    llm_fallback = False            # the payload is already structured
    base_confidence = 0.92

    #: Value written to ``company.ats_vendor`` and matched against it.
    vendor: str = ""

    # -- plan (FR-162) ------------------------------------------------------
    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        keywords = keywords_from(directives, composite_profile)
        max_records = int(caps.get("max_records_per_source") or DEFAULT_MAX_RECORDS)
        items: list[PlanItem] = []
        for target in company_targets(directives, caps, self.vendor):
            native: dict[str, Any] = {
                "slug": target["slug"],
                "company_id": target.get("company_id"),
                "company_name": target.get("company_name"),
                "max_records": max_records,
                "keywords": keywords,
                "title_filter": bool(target.get("title_filter") or caps.get("title_filter")),
            }
            native.update(target.get("extra") or {})
            items.append(
                PlanItem(
                    adapter_key=self.key,
                    native_query=native,
                    rationale=(
                        f"{self.display_name} board of "
                        f"{target.get('company_name') or target['slug']}: the employer's own "
                        "vacancy list, public and structured"
                    ),
                    estimated_pages=1,
                    estimated_seconds=8,
                    caps={"max_records": max_records},
                )
            )
        return items

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def slug_of(item: PlanItem) -> str:
        slug = str(item.native_query.get("slug") or "").strip().strip("/")
        if not slug:
            raise ValueError("ATS plan item is missing a board slug")
        return slug

    @staticmethod
    def limit_of(item: PlanItem) -> int:
        return int(item.native_query.get("max_records") or DEFAULT_MAX_RECORDS)

    def _egress(self):
        if self.egress is None:
            raise RuntimeError(f"[{self.key}] fetch() needs an EgressClient (IR-102)")
        return self.egress

    async def _get(self, url: str, **kwargs: Any) -> FetchResult | None:
        """One request through the egress layer (IR-102).

        Returns the whole result rather than the body so that the record can
        carry its ``raw_document_id`` (FR-183 provenance).  A board that is
        unreachable, rate-limited or disallowed by robots.txt must not abort the
        campaign, so the failure is logged and the plan item collects nothing.
        """
        try:
            result = await self._egress().fetch(
                url, access_method=self.access_method.value, **kwargs
            )
        except RobotsDisallowed:
            log.warning("[%s] robots.txt disallows %s (FR-182)", self.key, url)
            return None
        except Exception as exc:  # noqa: BLE001 - one dead board must not stop the job
            log.warning("[%s] fetch failed for %s: %s", self.key, url, exc)
            return None
        if not result.ok:
            log.info("[%s] %s returned HTTP %s", self.key, url, result.status_code)
            return None
        return result


def company_targets(directives: dict, caps: dict, vendor: str) -> list[dict[str, Any]]:
    """Companies in the plan that use ``vendor``.

    The planner may hand companies over in several shapes; all of them reduce to
    a slug plus optional identity, so the adapters do not care which was used:

    * ``caps["companies"]`` / ``directives["companies"]`` - rows shaped like the
      ``company`` table (``id``, ``name``, ``ats_vendor``, ``ats_slug``);
    * ``directives["ats"][vendor]`` - a bare list of board slugs;
    * a plain string, taken as a slug for this vendor.
    """
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(slug: str | None, company: dict[str, Any] | None = None) -> None:
        slug = (slug or "").strip().strip("/")
        if not slug or slug.lower() in seen:
            return
        seen.add(slug.lower())
        company = company or {}
        targets.append(
            {
                "slug": slug,
                "company_id": company.get("id") or company.get("company_id"),
                "company_name": company.get("name") or company.get("company_name"),
                "title_filter": company.get("title_filter"),
                "extra": company.get("native_query") or {},
            }
        )

    for source in (caps.get("companies"), directives.get("companies"), directives.get("targets")):
        for entry in source or []:
            if isinstance(entry, str):
                add(entry)
                continue
            if not isinstance(entry, dict):
                continue
            entry_vendor = str(entry.get("ats_vendor") or entry.get("vendor") or "").lower()
            if entry_vendor and entry_vendor != vendor:
                continue
            if not entry_vendor and not entry.get("ats_slug"):
                continue
            add(entry.get("ats_slug") or entry.get("slug"), entry)

    per_vendor = directives.get("ats") or caps.get("ats") or {}
    if isinstance(per_vendor, dict):
        for entry in per_vendor.get(vendor) or []:
            if isinstance(entry, str):
                add(entry)
            elif isinstance(entry, dict):
                add(entry.get("ats_slug") or entry.get("slug"), entry)
    return targets
