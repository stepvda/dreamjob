"""Actiris adapter - the Brussels public employment service (FR-181, FR-183, FR-261).

Actiris is the public employment service of the Brussels-Capital Region and is
the best source of Brussels SME employers the product has: 22,950 offers were
listed on 2026-09-09 and 25 of 25 sampled offers named a *distinct* employer,
where a commercial aggregator repeats the same staffing agencies
(docs/Data_Gathering_Plan.md 2.3).

Route (no key, no search API - the site publishes its own index):

    GET https://www.actiris.brussels/sitemapoffers-{nl|fr}.xml
    -> <urlset><url><loc>.../?reference=5948551</loc>
                    <lastmod>2026-09-09</lastmod></url>...

    GET https://www.actiris.brussels/nl/burgers/jobadvertentie/?reference=...
    GET https://www.actiris.brussels/fr/citoyens/detail-offre-d-emploi/?reference=...
    -> a server-rendered advert page

``robots.txt`` lists the six sitemaps and disallows ``/media/`` only, so both
routes are permitted (FR-182).

Selection, not crawling.  The sitemap is the whole index, so the adapter never
walks links: it filters ``lastmod`` to the last ``max_age_days`` (60 by
default), orders newest first, and takes one 50-offer slice per plan item -
which is what makes a plan item a fixed, resumable unit of work and keeps the
1-request-per-offer cost visible in the plan (a 4-hour campaign at 0.5 req/s
can afford roughly 7,200 offers, plan N8).

Extraction is deterministic and language-independent wherever the page allows
it: the working time, place and occupation family are read off the ``icon-*``
classes rather than off the Dutch or French label next to them, and the
contract type is read off ``<main data-gtmContractType="CDD">``.  The employer
is the row of the "how to apply" table, which is the field the whole company
side of the knowledge base is built from.  ``llm_fallback`` is False: nothing
here needs a model, so collection costs 0 tokens (plan 6.9).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin
from xml.etree import ElementTree

from dreamjob.adapters.base import (
    AccessMethod,
    AdapterCapabilities,
    PlanItem,
    RawRecord,
    SourceType,
    ToSStatus,
    register_adapter,
)
from dreamjob.adapters.vacancy_source import (
    SourceUnavailable,
    VacancySourceAdapter,
    application_route,
    country_from_location,
    fte_percentage,
    html_to_text,
    keywords_from,
    normalise_contract_type,
    normalise_language,
    normalise_work_arrangement,
    parse_datetime,
    parse_salary_text,
    requested_page,
    split_skills,
    title_matches,
)

try:  # pragma: no cover - selectolax is a declared dependency
    from selectolax.parser import HTMLParser
except ImportError:  # pragma: no cover
    HTMLParser = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

SITEMAP = "https://www.actiris.brussels/sitemapoffers-{language}.xml"
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
#: One plan item is one slice of the index; 50 offers is one request for the
#: sitemap plus fifty for the adverts (plan 7: "50 offers for board.actiris").
OFFERS_PER_PAGE = 50
#: Offers older than this are dropped before the slice is taken: 18,073 of the
#: 22,950 listed offers had been modified since July, and the tail goes back to
#: 2023 (plan 2.3).
DEFAULT_MAX_AGE_DAYS = 60
LANGUAGES = ("nl", "fr")

#: "Gecreëerd op 09 september 2026" / "Créé le 09 septembre 2026".
_STATED_DATE_RE = re.compile(r"(\d{1,2})\s+([^\W\d_]{3,12})\s+(\d{4})", re.UNICODE)
_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11,
    "december": 12,
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "aout": 8, "août": 8, "septembre": 9, "octobre": 10,
    "novembre": 11, "decembre": 12, "décembre": 12,
    # English, for the site's /en/ pages; "april", "september", "november" and
    # "december" are spelled the same in Dutch and are already keys above.
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7,
    "august": 8, "october": 10,
}
#: ``<main data-gtmContractType="...">`` - the one contract statement on the
#: page that is not written in the page's own language.  The three codes below
#: are the ones eight sampled live offers used (CDD, CDI, INTERIM); an
#: unrecognised code falls through to the page's own wording.
_GTM_CONTRACT = {"cdd": "fixed_term", "cdi": "permanent", "interim": "interim"}
#: The "how to apply" table labels that name the employer, in all three of the
#: site's languages.
_EMPLOYER_LABELS = (
    "naam van de werkgever", "nom de l'employeur", "nom de l’employeur",
    "name of the employer",
)


@register_adapter
class ActirisAdapter(VacancySourceAdapter):
    key = "board.actiris"
    display_name = "Actiris (Brussels public employment service)"
    source_type = SourceType.JOB_BOARD
    access_method = AccessMethod.HTTP
    coverage_countries = ["BE"]
    tos_status = ToSStatus.PERMITTED
    rate_limit_rps = 0.5
    llm_fallback = False
    base_confidence = 0.85
    legal_notes = (
        "Public employment service of the Brussels-Capital Region; public-sector "
        "information, no key required. robots.txt publishes the offer sitemaps and "
        "disallows /media/ only."
    )
    capabilities = AdapterCapabilities(
        keyword_search=False,        # the index is a sitemap; filtering is client-side
        location_filter=False,       # every offer is in the Brussels region
        company_lookup=False,
        pagination=True,
        max_results_per_query=OFFERS_PER_PAGE,
    )

    # -- plan (FR-162, FR-186) ----------------------------------------------
    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        keywords = keywords_from(directives, composite_profile)
        pages = max(1, int(caps.get("max_pages_per_source") or 2))
        language = str(directives.get("language") or "nl").lower()
        if language not in LANGUAGES:
            language = "nl"
        # One item for the whole search, paged through by the pipeline.
        #
        # This used to return one item *per page*, each carrying its own
        # ``page``.  It never worked: ``collection._run_page`` re-issues a plan
        # item once per page with its own counter written into
        # ``native_query["page"]`` (see :func:`requested_page`), so every one of
        # those twenty items was fetched as page 1.  The same fifty adverts were
        # read twenty times, offers 51-1000 were never read at all, and the rows
        # were indistinguishable on the collection screen because the only thing
        # separating them - the page - had been overwritten before the first
        # request went out.  ``estimated_pages`` is how an adapter asks for
        # depth; one item is how it asks for one search.
        return [
            PlanItem(
                adapter_key=self.key,
                native_query={
                    "language": language,
                    "offers_per_page": OFFERS_PER_PAGE,
                    "max_age_days": DEFAULT_MAX_AGE_DAYS,
                    "keywords": keywords,
                    "title_filter": bool(caps.get("title_filter")),
                },
                rationale=(
                    "Actiris is the Brussels public employment service; its offers "
                    "name distinct SME employers that no ATS board carries. "
                    f"The {pages * OFFERS_PER_PAGE} most recently modified offers, "
                    "newest first"
                ),
                estimated_pages=pages,
                # one sitemap read plus one advert per offer, at 2 s each
                estimated_seconds=2 * (OFFERS_PER_PAGE + 1) * pages,
                caps={"max_records": OFFERS_PER_PAGE * pages},
            )
        ]

    # -- fetch (IR-102, FR-182, FR-185) -------------------------------------
    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        query = self.native_query(item)
        language = str(query.get("language") or "nl").lower()
        if language not in LANGUAGES:
            language = "nl"
        per_page = max(1, int(query.get("offers_per_page") or OFFERS_PER_PAGE))
        page = requested_page(query) or 1
        max_age = int(query.get("max_age_days") or DEFAULT_MAX_AGE_DAYS)

        sitemap_url = SITEMAP.format(language=language)
        sitemap = await self._get(sitemap_url, headers={"Accept": "application/xml"})
        if sitemap is None:
            # Blocked, or the index itself is unreachable: settle() turns both
            # into the right exception rather than "no offers today".
            return self.settle([], nothing_to_fetch=self._nothing_to_fetch())

        offers = self.recent_offers(sitemap.text, max_age)
        window = offers[(page - 1) * per_page : page * per_page]
        if not window:
            log.info(
                "[%s] page %d is past the end of the %s index (%d offers newer than %d days)",
                self.key, page, language, len(offers), max_age,
            )
            return []

        records: list[RawRecord] = []
        for offer in window:
            advert = await self._get(offer["url"])
            if advert is None:
                continue
            records.append(
                RawRecord(
                    url=offer["url"],
                    content=advert.text,
                    content_type="text/html",
                    raw_document_id=advert.raw_document_id,
                    meta={
                        "language": language,
                        "reference": offer["reference"],
                        "lastmod": offer["lastmod"],
                        "keywords": query.get("keywords") or [],
                        "title_filter": bool(query.get("title_filter")),
                    },
                )
            )
        if not records:
            # Fifty adverts were asked for and none answered: that is a broken
            # source, not a page with nothing on it (FR-185).
            raise SourceUnavailable(f"[{self.key}] {self.fetch_outcome.summary()}")
        return self.settle(records, nothing_to_fetch=self._nothing_to_fetch())

    def _nothing_to_fetch(self) -> str:
        return "the offer sitemap could not be read, so no advert was named"

    # -- the index ----------------------------------------------------------
    @classmethod
    def recent_offers(cls, sitemap_xml: str, max_age_days: int = DEFAULT_MAX_AGE_DAYS,
                      today: datetime | None = None) -> list[dict[str, Any]]:
        """Offers modified within the window, newest first.

        The index reaches back to 2023 and is not ordered, so taking it as it
        comes would spend a campaign's whole request budget on adverts that
        closed years ago.
        """
        text = sitemap_xml.lstrip("﻿ \t\r\n")
        root = ElementTree.fromstring(text)
        cutoff = ((today or datetime.now(UTC)) - timedelta(days=max_age_days)).date()
        offers: list[dict[str, Any]] = []
        for node in root.iter(f"{SITEMAP_NS}url"):
            loc = (node.findtext(f"{SITEMAP_NS}loc") or "").strip()
            lastmod = (node.findtext(f"{SITEMAP_NS}lastmod") or "").strip()
            if not loc:
                continue
            try:
                modified = datetime.fromisoformat(lastmod[:10]).date()
            except ValueError:
                continue
            if modified < cutoff:
                continue
            offers.append(
                {"url": loc, "lastmod": lastmod[:10], "reference": cls.reference_of(loc)}
            )
        # Newest first, and within one day the highest reference first, so the
        # slice a plan item takes is stable between the plan and the run.
        offers.sort(key=lambda o: (o["lastmod"], o["reference"]), reverse=True)
        return offers

    @staticmethod
    def reference_of(url: str) -> str:
        match = re.search(r"reference=(\d+)", url)
        return match.group(1) if match else ""

    # -- parse (FR-183) -----------------------------------------------------
    def parse(self, raw: RawRecord) -> list[dict]:
        if HTMLParser is None:  # pragma: no cover - declared dependency
            raise RuntimeError(
                f"[{self.key}] selectolax is required to parse Actiris advert pages"
            )
        tree = HTMLParser(raw.content)
        main = tree.css_first("main#main-content") or tree.css_first("main")
        if main is None:
            return []
        title_node = main.css_first(".bloc-title h1") or main.css_first("h1")
        title = title_node.text(strip=True) if title_node else ""
        if not title:
            return []
        keywords = raw.meta.get("keywords") or []
        if raw.meta.get("title_filter") and not title_matches(title, keywords):
            return []

        facts = self._facts(main)
        table = self._apply_table(main)
        description = self._description(main)
        language = normalise_language(raw.meta.get("language"), description)

        employer = next(
            (v for label, v in table.items() if label in _EMPLOYER_LABELS), ""
        ) or next(iter(table.values()), "")
        apply_url = self._apply_url(main, raw.url)
        channel, target = application_route(apply_url, description)
        required, desirable = split_skills(description)
        salary_min, salary_max, currency = parse_salary_text(description[:6000])
        # The page states the contract twice - once as a language-neutral code
        # on <main>, once as a Dutch/French phrase - and the advert body says
        # it a third time, less reliably. They are read in that order.
        contract = (
            _GTM_CONTRACT.get(self._attr(main, "data-gtmContractType").lower())
            or normalise_contract_type(facts.get("contract"))
            or normalise_contract_type(description[:2000])
        )

        return [
            {
                "company_name_raw": employer or None,
                "title": title,
                "function_family": facts.get("occupation"),
                "description": description,
                "required_skills": required,
                "desirable_skills": desirable,
                "location": facts.get("place"),
                # <main data-gtmCountry="België"> is the page's own statement;
                # BE is the fallback because Actiris is the Brussels PES and
                # its coverage is declared as BE.
                "country": country_from_location(
                    self._attr(main, "data-gtmCountry"), facts.get("place")
                ) or "BE",
                "work_arrangement": normalise_work_arrangement(description[:2000]),
                "contract_type": contract,
                "fte_percentage": fte_percentage(facts.get("working_time"))
                or fte_percentage(description[:1500]),
                "salary_min": salary_min,
                "salary_max": salary_max,
                "salary_currency": currency,
                "posted_at": self._stated_date(main) or parse_datetime(raw.meta.get("lastmod")),
                "language": language,
                "application_channel": channel,
                "application_target": target,
                "source_url": raw.url,
            }
        ]

    # -- page readers -------------------------------------------------------
    @staticmethod
    def _attr(node: Any, name: str) -> str:
        """One attribute of a node, however the parser cased its name.

        HTML attribute names are case-insensitive and the page writes
        ``data-gtmContractType``; the parser hands it back lowercased, so an
        exact-key lookup silently found nothing and the language-neutral
        contract and country statements were never read.
        """
        wanted = name.lower()
        for key, value in (node.attributes or {}).items():
            if key.lower() == wanted and value:
                return str(value)
        return ""

    @staticmethod
    def _facts(main: Any) -> dict[str, str]:
        """The four ``<ul class="picto">`` facts, keyed by their icon.

        The label next to each icon is Dutch on ``/nl/`` and French on ``/fr/``;
        the icon class is the same on both, so reading the icon is what keeps
        one parser working for both halves of the site.
        """
        icons = {
            "icon-place": "place",
            "icon-clock": "working_time",
            "icon-support": "contract",
            "icon-objectif": "occupation",
        }
        facts: dict[str, str] = {}
        for item in main.css("ul.picto li"):
            icon = item.css_first("i")
            classes = (icon.attributes.get("class") or "") if icon else ""
            key = next((v for k, v in icons.items() if k in classes), None)
            text_node = item.css_first("span.text")
            if key is None or text_node is None:
                continue
            whole = " ".join(text_node.text().split())
            # The label is the <span> *inside* <span class="text">; asking the
            # node itself for "span" returns that same node, so the label
            # swallowed the whole line and every fact came out empty.
            label_node = next(
                (child for child in text_node.iter() if child.tag == "span"), None
            )
            label = " ".join(label_node.text().split()) if label_node else ""
            if label and whole.startswith(label):
                value = whole[len(label) :]
            elif ":" in whole:
                value = whole.split(":", 1)[1]
            else:
                value = whole
            value = value.strip().strip(":").strip()
            if value:
                facts[key] = value
        return facts

    @staticmethod
    def _description(main: Any) -> str:
        blocks = [node.html or "" for node in main.css(".bloc-emploi__text")]
        return html_to_text("".join(blocks))

    @staticmethod
    def _apply_table(main: Any) -> dict[str, str]:
        """The "how to apply" table as ``{lowercased label: value}``."""
        table: dict[str, str] = {}
        for row in main.css("table.bloc-emploi__table tr"):
            cells = row.css("td")
            if len(cells) < 2:
                continue
            label = " ".join(cells[0].text().split()).lower().rstrip(":").strip()
            value = " ".join(cells[1].text().split()).strip()
            if label and label not in table:
                table[label] = value
        return table

    @staticmethod
    def _apply_url(main: Any, page_url: str) -> str:
        for link in main.css("table.bloc-emploi__table a"):
            href = (link.attributes.get("href") or "").strip()
            if href and not href.startswith("javascript:"):
                return urljoin(page_url, href)
        return ""

    @staticmethod
    def _stated_date(main: Any) -> str | None:
        """"Gecreëerd op 09 september 2026" -> an ISO timestamp."""
        state = main.css_first("p.state") or main.css_first(".info-under-title")
        if state is None:
            return None
        match = _STATED_DATE_RE.search(" ".join(state.text().split()))
        if not match:
            return None
        month = _MONTHS.get(match.group(2).lower())
        if not month:
            return None
        try:
            return datetime(
                int(match.group(3)), month, int(match.group(1)), tzinfo=UTC
            ).isoformat(timespec="seconds")
        except ValueError:
            return None
