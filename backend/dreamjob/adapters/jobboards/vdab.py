"""VDAB adapter - Flemish public employment service (FR-181, IR-101, FR-182).

VDAB is the single most relevant board for a Belgian job seeker: it is the
public employment service for Flanders and carries the largest share of
Flemish vacancies.

What is actually reachable, and why the adapter looks like this:

* ``https://www.vdab.be/vindeenjob/vacatures`` and the ``/vindeenjob/vacatures/{id}``
  detail pages are **allowed** by VDAB's robots.txt, as is the vacancy sitemap
  at ``/sitemap/vindeenjob/vacatures/``, which lists thousands of current
  vacancy URLs.
* ``https://www.vdab.be/api/vindeenjob/*`` - the JSON search API the site's own
  single-page app calls - is **disallowed by robots.txt** and additionally
  answers HTTP 403 to non-browser clients.  FR-182 makes robots.txt binding, so
  that route is not used: no API mode, no bypass.
* Every ``/vindeenjob/`` page - the search listing **and** each vacancy detail
  page - is the Angular application shell: ~46 KB of ``<app-root>`` and
  "Toepassing laden...", with no JSON-LD and no JobPosting markup anywhere.
  Measured against two live vacancy pages taken from VDAB's own sitemap.

So there is nothing an HTTP client can extract here, whatever selectors it
carries, and this adapter is catalogued as **disabled** until an operator gives
it ``start_urls`` that a rendering client has already resolved (or acknowledges
it deliberately).  The selectors below are kept for that case and for the day
VDAB serves markup again; what is *not* kept is a bare ``h2``/``main``
fallback, which on the loading shell matched a "Nieuw!" badge as the employer
and "Toepassing laden..." as the advert - a wrong record is worse than none
(FR-183, NFR-403).
"""

from __future__ import annotations

from typing import Any

from dreamjob.adapters.base import AdapterCapabilities, ToSStatus, register_adapter
from dreamjob.adapters.jobboards.generic_html import HtmlBoardAdapter


@register_adapter
class VdabAdapter(HtmlBoardAdapter):
    key = "board.vdab"
    display_name = "VDAB (Vind een job)"
    coverage_countries = ["BE"]
    tos_status = ToSStatus.RESTRICTED
    requires_ack = False
    rate_limit_rps = 0.3
    legal_notes = (
        "Public employment service. robots.txt disallows /api/vindeenjob/, so the "
        "JSON search API is never called; only the public vindeenjob pages are read, "
        "at a reduced rate. Those pages are rendered client-side, so this source is "
        "catalogued as disabled until an operator supplies advert URLs a rendering "
        "client has resolved."
    )
    capabilities = AdapterCapabilities(
        keyword_search=True, location_filter=True, radius_filter=True,
        contract_type_filter=True, pagination=True, max_results_per_query=100,
    )

    defaults: dict[str, Any] = {
        "url_template": (
            "https://www.vdab.be/vindeenjob/vacatures"
            "?trefwoord={query}&locatie={location}&pagina={page}"
        ),
        "pages": 2,
        "country": "BE",
        "language": "nl",
        "detail": True,
        "selectors": {
            "list_item": "article, li.c-vacature, .c-vacature-card",
            "url": 'a[href*="/vindeenjob/vacatures/"]@href',
            "title": "h2 || h3 || .c-vacature-card__titel",
            "company": ".c-vacature-card__werkgever || .werkgever",
            "location": ".c-vacature-card__plaats || .plaats",
            "posted_at": "time@datetime || time",
        },
        "detail_selectors": {
            "title": "h1",
            "company": '[data-cy="werkgever"] || .werkgever',
            "location": '[data-cy="plaats"] || .plaats',
            "description": "article || .vacature-detail",
            "posted_at": "time@datetime",
            "apply_url": 'a[href^="mailto:"]@href || a.solliciteer@href',
        },
    }

    def has_route(self, cfg: dict[str, Any] | None = None) -> bool:
        """Only advert URLs resolved elsewhere are readable over HTTP (NFR-403)."""
        cfg = self.defaults if cfg is None else cfg
        return bool(cfg.get("start_urls"))
