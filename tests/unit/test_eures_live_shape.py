"""EURES against the shape the live service actually answers with (FR-181..186).

The adapter spent every campaign in the database posting to
``https://europa.eu/eures/eures-apps/searchengine/page``, a path that was
withdrawn when the portal became a single-page application.  It answered 404
with an HTML stub, the adapter read "no vacancies" out of it, and the plan item
was recorded as ``done`` with zero records and zero errors - the single most
expensive line in the corpus, because EURES is the only pan-European source
that needs no key and it alone carries 232,496 Belgian vacancies.

Everything asserted here was measured against the live service on 2026-09-09
with this product's own User-Agent, honouring europa.eu's ``Crawl-delay: 10``
(docs/Data_Gathering_Plan.md C1, C2, appendix C):

===============================================  ==============================
request                                          answer
===============================================  ==============================
``locationCodes: ["BE"]``, ``resultsPerPage: 50``  HTTP 200, ``numberRecords``
                                                 232,496, fifty summaries
``resultsPerPage: 100`` and ``200``               HTTP 400 "Too many results
                                                 per page were requested"
``["BE1"]`` + ``publicationPeriod: LAST_WEEK``    HTTP 200, 1,593
``... + sectorCodes: ["N"]``                      HTTP 200, 482
===============================================  ==============================

``tests/fixtures/boards/eures_search_be1_last_week.json`` is the third of those
answers, recorded verbatim (two of its fifty summaries), so the next portal
migration breaks a test rather than a campaign.  Nothing here touches the
network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from dreamjob.adapters.base import PlanItem, RawRecord
from dreamjob.adapters.jobboards.eures import (
    DEFAULT_API_BASE,
    PAGE_SIZE,
    EuresAdapter,
)
from dreamjob.adapters.vacancy_source import SourceUnavailable

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RECORDED_SEARCH = (FIXTURES / "boards" / "eures_search_be1_last_week.json").read_text(
    encoding="utf-8"
)

#: What the portal answered on the retired path, for every campaign in the
#: database: an HTML error page, with an HTML 404 status.
RETIRED_PATH_BODY = (
    "<!DOCTYPE html><html><head><title>404 Not Found</title></head>"
    "<body><h1>The requested page could not be found</h1></body></html>"
)


@dataclass
class StubResponse:
    url: str
    text: str
    status_code: int = 200
    raw_document_id: str | None = "raw-1"

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@dataclass
class StubEgress:
    """Serves one recorded body and records every request and every body sent."""

    body: str = RECORDED_SEARCH
    status_code: int = 200
    calls: list[str] = field(default_factory=list)
    posted: list[dict] = field(default_factory=list)

    async def fetch(self, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append(url)
        self.posted.append(dict(kwargs.get("json") or {}))
        return StubResponse(url=url, text=self.body, status_code=self.status_code)


def _fetch(adapter: EuresAdapter, native_query: dict) -> list[RawRecord]:
    import asyncio

    return asyncio.run(adapter.fetch(PlanItem("board.eures", native_query)))


# ---------------------------------------------------------------------------
# C1: the endpoint
# ---------------------------------------------------------------------------


def test_the_search_goes_to_the_service_that_answers_and_not_the_retired_one():
    """C1: the withdrawn path answered 404 to every search this product ever made."""
    egress = StubEgress()
    adapter = EuresAdapter(egress)
    _fetch(adapter, {"keyword": "data engineer", "country_codes": ["BE"], "pages": 1})

    assert egress.calls == [f"{DEFAULT_API_BASE}/jv-search/search?lang=en"]
    assert "eures-apps/searchengine" not in egress.calls[0]
    assert egress.posted[0]["keywords"] == [
        {"keyword": "data engineer", "specificSearchCode": "EVERYWHERE"}
    ]
    assert egress.posted[0]["locationCodes"] == ["BE"]


def test_the_recorded_answer_still_carries_everything_the_parser_reads():
    """The fixture is a verbatim live answer; drifting off it must break here.

    The claim this pins is the one the whole plan rests on: a *listing* row is
    already a complete vacancy - employer name, advert text, region and dates -
    so no detail request is needed to write it (C2).
    """
    adapter = EuresAdapter()
    rows = adapter.parse(
        RawRecord(
            url=f"{DEFAULT_API_BASE}/jv-search/search?lang=en&page=1",
            content=RECORDED_SEARCH,
            content_type="application/json",
            meta={"kind": "list", "lang": "en", "emit": True},
        )
    )

    assert len(rows) == 2
    # Employer, advert and dates are inline in the listing.
    assert [r["company_name_raw"] for r in rows] == [
        "Federale Overheidsdienst Beleid en Ondersteuning FOD",
        "SCHOLENGROEP 8 : BRUSSEL AV",
    ]
    assert all(r["title"] for r in rows)
    assert all(len(r["description"]) > 300 for r in rows)
    assert {r["country"] for r in rows} == {"BE"}          # from locationMap's key
    assert all(r["posted_at"] and r["posted_at"].startswith("2026-") for r in rows)
    assert all(r["source_url"].startswith("https://europa.eu/eures/portal/") for r in rows)

    payload = json.loads(RECORDED_SEARCH)
    assert payload["numberRecords"] == 1593                # BE1, last week
    assert len(payload["jvs"]) == 2
    assert set(payload["facets"]) == {"NACE_CODE", "POSITION_LOCATION"}


# ---------------------------------------------------------------------------
# C2: no detail request per summary
# ---------------------------------------------------------------------------


def test_one_listing_page_is_one_request_and_not_fifty_one():
    """C2: ``jv-details`` is a 404 on this base and the summary needs nothing from it."""
    egress = StubEgress()
    adapter = EuresAdapter(egress)
    records = _fetch(adapter, {"keyword": "", "country_codes": ["BE"], "pages": 1})

    assert len(egress.calls) == 1, egress.calls
    assert not [c for c in egress.calls if "/jv/id/" in c or "jv-details" in c]
    assert [r.meta["kind"] for r in records] == ["list"]


def test_a_planned_item_does_not_ask_for_a_detail_per_summary():
    """C2 at the source: ``plan()`` is what the campaign persists."""
    items = EuresAdapter().plan(
        {"countries": ["BE"], "keywords": ["data engineer"]}, {}, {"max_pages_per_source": 3}
    )
    assert items
    for item in items:
        assert item.native_query["fetch_details"] is False
        assert item.native_query["results_per_page"] == PAGE_SIZE


# ---------------------------------------------------------------------------
# The fifty-result ceiling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("requested", [51, 100, 200, 500])
def test_a_plan_item_asking_for_more_than_fifty_results_is_clamped(requested):
    """``resultsPerPage`` 100 and 200 are both HTTP 400 "Too many results per page".

    ``capabilities.max_results_per_query`` said 200, so a plan item written from
    the catalogue asked for 200 - and every page of it was refused, which the
    worker then reported as a source that could not be reached.
    """
    egress = StubEgress()
    adapter = EuresAdapter(egress)
    _fetch(
        adapter,
        {"keyword": "", "country_codes": ["BE"], "pages": 1, "results_per_page": requested},
    )
    assert egress.posted[0]["resultsPerPage"] == PAGE_SIZE

    # The public body builder clamps too: it is what a partitioned sweep calls.
    assert EuresAdapter.search_body("", ["BE"], 1, requested)["resultsPerPage"] == PAGE_SIZE
    assert EuresAdapter.capabilities.max_results_per_query == PAGE_SIZE


# ---------------------------------------------------------------------------
# Partitioning (the sweep in section 2.4 is 60 partitions, not 60 keywords)
# ---------------------------------------------------------------------------


def test_a_partitioned_item_reaches_the_service_as_region_period_and_sector():
    """Measured: BE 232,496 -> BE1 + LAST_WEEK 1,593 -> + NACE section N 482."""
    egress = StubEgress()
    adapter = EuresAdapter(egress)
    _fetch(
        adapter,
        {
            "keyword": "",
            "country_codes": ["BE"],
            "nuts_codes": ["BE1"],
            "sector_codes": ["n"],
            "publication_period": "last_week",
            "pages": 1,
        },
    )
    body = egress.posted[0]
    # A NUTS code replaces the country: sending both would ask for the whole
    # country as well as the region and undo the partition.
    assert body["locationCodes"] == ["BE1"]
    assert body["publicationPeriod"] == "LAST_WEEK"
    assert body["sectorCodes"] == ["N"]


def test_an_unknown_publication_period_is_reported_rather_than_quietly_dropped(caplog):
    """Measured on BE1: LAST_DAY 191, LAST_WEEK 1,593, LAST_MONTH 4,651; anything else 400.

    Dropping an unrecognised period would turn sixty partitions into sixty
    copies of the same unfiltered query - the silent duplicate work this slice
    exists to remove - so it is sent, warned about, and fails visibly.
    """
    egress = StubEgress()
    adapter = EuresAdapter(egress)
    with caplog.at_level("WARNING"):
        _fetch(
            adapter,
            {"keyword": "", "nuts_codes": ["BE1"], "publication_period": "LAST_FORTNIGHT",
             "pages": 1},
        )
    assert egress.posted[0]["publicationPeriod"] == "LAST_FORTNIGHT"
    assert "publicationPeriod" in caplog.text
    assert EuresAdapter.publication_period({"publication_period": "last_month"}) == "LAST_MONTH"


def test_an_unpartitioned_item_sends_exactly_the_body_that_was_measured():
    """No empty ``publicationPeriod``/``sectorCodes`` keys: the 200 was measured without them."""
    body = EuresAdapter.search_body("engineer", ["BE"], 2, PAGE_SIZE)
    assert body == {
        "keywords": [{"keyword": "engineer", "specificSearchCode": "EVERYWHERE"}],
        "locationCodes": ["BE"],
        "sortSearch": "BEST_MATCH",
        "resultsPerPage": 50,
        "page": 2,
    }


# ---------------------------------------------------------------------------
# FR-185: a dead endpoint is a failure, not "no vacancies"
# ---------------------------------------------------------------------------


def test_a_404_from_the_search_service_fails_the_plan_item():
    """The regression that hid C1 for the whole life of the corpus.

    The retired path answered 404 with HTML and the adapter returned ``[]``,
    which the collection worker cannot tell apart from "Belgium has no
    vacancies matching this search".  It must raise instead.
    """
    egress = StubEgress(body=RETIRED_PATH_BODY, status_code=404)
    adapter = EuresAdapter(egress)
    with pytest.raises(SourceUnavailable) as raised:
        _fetch(adapter, {"keyword": "data", "country_codes": ["BE"], "pages": 1})
    assert "404" in str(raised.value)
    assert egress.calls, "the search was actually attempted"


def test_an_html_answer_on_a_200_also_fails_the_plan_item():
    """A portal that serves its SPA shell with HTTP 200 is not a source of zero jobs."""
    egress = StubEgress(body=RETIRED_PATH_BODY, status_code=200)
    adapter = EuresAdapter(egress)
    with pytest.raises(SourceUnavailable) as raised:
        _fetch(adapter, {"keyword": "data", "country_codes": ["BE"], "pages": 1})
    assert "not JSON" in str(raised.value)
