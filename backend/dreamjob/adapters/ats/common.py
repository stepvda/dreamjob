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

Two shapes of plan item arrive here and both must work (they are the same work
item, only differently packaged):

* a :class:`~dreamjob.adapters.base.PlanItem` from ``plan()``, whose payload is
  the ``native_query`` attribute, carrying ``slug``;
* the ``source_plan_item`` row the collection worker reads back from the
  database as a plain dict, whose payload is ``row["native_query"]`` and which
  the campaign planner writes as ``board_slugs`` (a list).

:meth:`ATSAdapter.native_query` is therefore the only place any of these
adapters reads a plan item, and every one of them names *every* slug in the
list rather than only the first.
"""

from __future__ import annotations

import logging
from typing import Any

from dreamjob.adapters.base import AccessMethod, AdapterCapabilities, PlanItem, SourceType
from dreamjob.adapters.vacancy_source import (
    UnusableQuery,
    VacancySourceAdapter,
    keywords_from,
)

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
                # FR-162: keyword filtering over a whole board is opt-in, because
                # a board is the employer's own list and scoring happens later.
                # It is honoured whenever the plan item asks for it.
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
    #: Every read of a plan item goes through
    #: :meth:`VacancySourceAdapter.native_query`, so these adapters are
    #: indifferent to whether the item arrived as a ``PlanItem`` or as the plain
    #: ``source_plan_item`` row.  A half-converted adapter is what produced the
    #: recorded crash "'dict' object has no attribute 'native_query'", which
    #: failed a whole collection job.
    _native_query = VacancySourceAdapter.native_query

    @classmethod
    def slugs_of(cls, item: PlanItem | dict[str, Any]) -> list[str]:
        """Every board slug this plan item names, whatever shape the planner used.

        The adapter-native ``plan()`` writes ``slug``; an LLM-generated plan
        writes ``board_slugs`` (a list), which is the shape the planner's prompt
        advertises.  Returning the whole list matters: reading only
        ``board_slugs[0]`` silently dropped every board after the first and
        still reported the plan item as a success.
        """
        query = cls.native_query(item)
        slugs: list[str] = []
        raw: Any = query.get("slug") or query.get("board_slugs") or query.get("slugs") or []
        if isinstance(raw, str):
            raw = [raw]
        elif isinstance(raw, dict):
            # A mapping ({"primary": "acme"}) used to raise KeyError out of the
            # worker and take the whole collection job down with it.
            raw = list(raw.values())
        elif not isinstance(raw, (list, tuple, set)):
            raw = [raw]
        for entry in raw:
            if isinstance(entry, dict):
                entry = entry.get("slug") or entry.get("ats_slug")
            if not isinstance(entry, str):
                continue          # a number or a null is not a board name
            slug = entry.strip().strip("/")
            if slug and slug not in slugs:
                slugs.append(slug)
        return slugs

    @classmethod
    def slug_of(cls, item: PlanItem | dict[str, Any]) -> str:
        """The first board slug of a plan item; raises when it names none."""
        slugs = cls.slugs_of(item)
        if not slugs:
            raise UnusableQuery("ATS plan item is missing a board slug")
        return slugs[0]

    @classmethod
    def has_slug(cls, item: PlanItem | dict[str, Any]) -> bool:
        """Can this plan item name a board to read?

        An ATS plan item with no slug - no company was specified to look up, or
        the LLM left ``board_slugs`` empty - has nothing to fetch.  This runs in
        the collection worker outside any exception handling of its own, so it
        must never raise, whatever malformed payload the planner stored.
        """
        try:
            return bool(cls.slugs_of(item))
        except Exception:  # noqa: BLE001 - a bad payload is "no slug", not a crashed job
            log.warning("[%s] could not read a board slug from the plan item", cls.key,
                        exc_info=True)
            return False

    @classmethod
    def limit_of(cls, item: PlanItem | dict[str, Any]) -> int:
        return int(cls.native_query(item).get("max_records") or DEFAULT_MAX_RECORDS)

    def board_meta(self, item: PlanItem | dict[str, Any], slug: str) -> dict[str, Any]:
        """The ``RawRecord.meta`` every ATS adapter attaches to a board response."""
        query = self.native_query(item)
        return {
            "slug": slug,
            "company_id": query.get("company_id"),
            "company_name": query.get("company_name"),
            "max_records": self.limit_of(item),
            "keywords": query.get("keywords") or [],
            "title_filter": bool(query.get("title_filter")),
        }

    def no_board_named(self) -> str:
        return (
            "no board slug in the plan item: no company with a known "
            f"{self.vendor or self.display_name} board is in scope yet"
        )


def company_targets(directives: dict, caps: dict, vendor: str) -> list[dict[str, Any]]:
    """Companies in the plan that use ``vendor``.

    The planner may hand companies over in several shapes; all of them reduce to
    a slug plus optional identity, so the adapters do not care which was used:

    * ``caps["companies"]`` / ``directives["companies"]`` - rows shaped like the
      ``company`` table (``id``, ``name``, ``ats_vendor``, ``ats_slug``);
    * ``directives["ats"][vendor]`` - a bare list of board slugs;
    * a plain string, taken as a slug for this vendor.

    A company row must name its vendor: a row with a slug but no
    ``ats_vendor`` would otherwise become a plan item for all seven adapters,
    six of which would 404 against a slug that is not theirs.
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
            if entry_vendor != vendor:
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
