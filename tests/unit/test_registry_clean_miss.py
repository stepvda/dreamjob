"""A register that answers "I hold no such company" has not broken (FR-181, NFR-403).

The Belgian enterprise register was asked about Nobia (Swedish), Zara Home
(Spanish) and xneelo (South African), correctly held none of them, and 180 of
200 plan items were written down as "fetched 1 page(s) and extracted no record -
the source layout has probably changed".  The adapter was working perfectly.

What was missing is the signal: ``stated_empty`` existed, but it lived on the
vacancy adapter and only EURES ever set it, so no registry could reach the
``no_matches`` state at all.  These tests pin down both halves - the resolvers
saying "answered, and the answer was no", and the collection layer reading it -
and, just as important, the three things that must *not* be read that way: an
outage, a bot wall, and an answer we could not parse.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dreamjob.adapters.base import PlanItem
from dreamjob.adapters.registries.common import RegistryWall
from dreamjob.adapters.registries.kbo import KBOAdapter
from dreamjob.pipeline import collection

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "registries"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class StubResponse:
    def __init__(self, url: str, text: str, status_code: int = 200) -> None:
        self.url = url
        self.text = text
        self.status_code = status_code
        self.content = text.encode()
        self.raw_document_id = None
        self.from_cache = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class StubEgress:
    """The register, as this adapter sees it: one URL fragment, one body."""

    def __init__(self, pages: dict[str, str], *, raises: Exception | None = None) -> None:
        self.pages = pages
        self.raises = raises
        self.urls: list[str] = []

    async def fetch(self, url: str, **kwargs: object) -> StubResponse:
        self.urls.append(url)
        if self.raises is not None:
            raise self.raises
        for fragment, text in self.pages.items():
            if fragment in url:
                return StubResponse(url, text)
        return StubResponse(url, "", 404)


def _item(name: str) -> PlanItem:
    return PlanItem(
        adapter_key="registry.kbo",
        native_query={"company": {"id": "c1", "name": name}, "years": 1},
    )


def _outcome(adapter: KBOAdapter, egress: StubEgress) -> collection.ItemOutcome:
    """The plan item's outcome, assembled the way ``_run_page`` assembles it."""
    step = collection.ItemOutcome(requests=len(egress.urls))
    step.stated_empty = collection._stated_empty(adapter)
    step.pages = 1 if step.requests else 0
    return step


# ---------------------------------------------------------------------------
# The clean miss, end to end
# ---------------------------------------------------------------------------


async def test_a_register_that_states_no_result_is_read_not_broken() -> None:
    """The exact page the register served for 89 of the 180 (stored 2026-09-10)."""
    egress = StubEgress({"zoeknaamfonetisch": fixture("kbo_name_search_no_result.html")})
    adapter = KBOAdapter(egress=egress)

    records = await adapter.run(_item("Nobia"))

    assert records == []
    assert adapter.stated_empty == 1
    outcome = _outcome(adapter, egress)
    assert outcome.state() == "no_matches"
    assert collection._STATE_STATUS[outcome.state()] == "done"
    assert outcome.state() in collection.NON_FAILURE_STATES


async def test_a_hit_list_that_names_no_such_company_is_an_answer() -> None:
    """Rows were parsed and none of them is the company: the register answered."""
    egress = StubEgress({"zoeknaamfonetisch": fixture("kbo_name_search_touring.html")})
    adapter = KBOAdapter(egress=egress)

    assert await adapter.run(_item("TOURING NV")) == []
    assert adapter.stated_empty == 1
    assert _outcome(adapter, egress).state() == "no_matches"


async def test_the_message_names_what_the_source_holds() -> None:
    """A register holds records, not vacancies, and says so."""
    outcome = collection.ItemOutcome(requests=1, pages=1, stated_empty=1)
    assert "holds no record for this query" in (
        collection._state_message(outcome, holds="record") or ""
    )
    assert "holds no vacancy for this query" in (
        collection._state_message(outcome, holds="vacancy") or ""
    )


# ---------------------------------------------------------------------------
# What must never be read as an answer
# ---------------------------------------------------------------------------


async def test_an_answer_we_could_not_parse_is_still_a_breakage() -> None:
    """NFR-403's guarantee: an empty parse with no marker keeps the alarm."""
    egress = StubEgress({"zoeknaamfonetisch": "<html><body><p>nothing here</p></body></html>"})
    adapter = KBOAdapter(egress=egress)

    assert await adapter.run(_item("Nobia")) == []
    assert adapter.stated_empty == 0
    outcome = _outcome(adapter, egress)
    assert outcome.state() == "extracted_nothing"
    assert collection._STATE_STATUS[outcome.state()] == "failed"


async def test_an_outage_is_not_an_answer() -> None:
    egress = StubEgress({}, raises=RuntimeError("connection reset"))
    adapter = KBOAdapter(egress=egress)

    assert await adapter.run(_item("Nobia")) == []
    assert adapter.stated_empty == 0
    assert _outcome(adapter, egress).state() == "extracted_nothing"


async def test_a_non_2xx_is_not_an_answer() -> None:
    egress = StubEgress({"nothing-matches": ""})   # every URL answers 404
    adapter = KBOAdapter(egress=egress)

    assert await adapter.run(_item("Nobia")) == []
    assert adapter.stated_empty == 0


async def test_a_bot_wall_is_neither_an_answer_nor_a_layout_change() -> None:
    """The register serves its CAPTCHA with HTTP 200, so nothing below sees it."""
    egress = StubEgress({"zoeknaamfonetisch": fixture("kbo_bot_wall.html")})
    adapter = KBOAdapter(egress=egress)

    with pytest.raises(RegistryWall) as raised:
        await adapter.run(_item("cloudbridge-consulting-gmbh"))

    assert "CAPTCHA" in str(raised.value)
    assert adapter.stated_empty == 0


async def test_ambiguity_is_not_emptiness() -> None:
    """The register holds two of them; "nothing to collect" would be false."""
    egress = StubEgress(
        {"zoeknaamfonetisch": fixture("kbo_name_search_house_of_recruitment.html")}
    )
    adapter = KBOAdapter(egress=egress)

    assert await adapter.run(_item("HOUSE OF RECRUITMENT SOLUTIONS")) == []
    assert adapter.stated_empty == 0
    assert _outcome(adapter, egress).state() == "extracted_nothing"


async def test_a_name_the_register_cannot_be_asked_spends_no_request() -> None:
    """16 of the 200 were board slugs and legal forms, not names."""
    egress = StubEgress({"zoeknaamfonetisch": fixture("kbo_name_search_barco.html")})
    adapter = KBOAdapter(egress=egress)

    assert await adapter.run(_item("BV")) == []
    assert egress.urls == [], "no request may be spent on a name with no identity in it"
    assert adapter.stated_empty == 0
    assert _outcome(adapter, egress).state() == "no_work"
    assert collection._STATE_STATUS["no_work"] == "skipped"


# ---------------------------------------------------------------------------
# NFR-403: a stated-empty page is not an extraction attempt
# ---------------------------------------------------------------------------


def test_a_stated_empty_page_is_left_out_of_the_extraction_rate() -> None:
    empty = collection.ItemOutcome(requests=1, pages=1, stated_empty=1)
    assert empty.extraction_rate is None, "there was nothing to extract from"

    mixed = collection.ItemOutcome(requests=2, pages=2, productive_pages=1, stated_empty=1)
    assert mixed.extraction_rate == 1.0, "the one page that held records yielded records"

    broken = collection.ItemOutcome(requests=1, pages=1)
    assert broken.extraction_rate == 0.0, "NFR-403 still fires on a page that parsed to nothing"


def test_a_clean_miss_neither_dents_the_catalogue_rate_nor_raises_the_alarm(monkeypatch) -> None:
    """The half of this that the plan-item status alone does not fix."""
    written: list[tuple] = []
    audited: list[str] = []
    monkeypatch.setattr(
        collection.repo, "record_extraction_rate",
        lambda key, rate, had_success: written.append((key, rate)),
    )
    monkeypatch.setattr(
        collection.repo, "record_audit",
        lambda action, **kwargs: audited.append(action),
    )
    monkeypatch.setattr(collection.repo, "bump_plan_item", lambda *a, **k: None)

    unit = collection._Unit(
        item={"id": "i1", "adapter_key": "registry.kbo"},
        adapter=KBOAdapter(),
        writer=None,
        pages=1,
    )
    unit.outcome = collection.ItemOutcome(requests=1, pages=1, stated_empty=1)

    assert collection._record_extraction(unit, "campaign-1") is None
    assert written == [], "a register that holds nothing says nothing about extraction"
    assert audited == [], "and it is not evidence that the layout changed"


def test_the_noun_follows_the_source_type() -> None:
    unit = collection._Unit(
        item={"id": "i1", "adapter_key": "registry.kbo"},
        adapter=KBOAdapter(),
        writer=None,
        pages=1,
    )
    assert unit.holds == "record"
