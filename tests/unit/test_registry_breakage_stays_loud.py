"""A resolver that cannot tell a clean miss from its own breakage (FR-181, NFR-403).

``test_registry_clean_miss`` proves the half that was wanted: the Belgian
register saying "I hold no Swedish kitchen manufacturer" now settles the plan
item as read-and-empty instead of "the source layout has probably changed".
This file is the other half, and it is the one that can go wrong quietly - the
change is only safe if a *real* breakage still reports as ``extracted_nothing``.

The trap every JSON resolver fell into is the negative read: "the list of hits
came back empty, so the source holds none".  An empty list is also what a
renamed field produces, so a register that reshaped its response was reported as
answering "nothing to collect" - the NFR-403 alarm silenced by the very change
meant to sharpen it.  ``registries.common.stated_none`` reads the source's own
count instead, and each resolver below is given the same two bodies: one where
the source states it found nothing, and one where it states it found several and
the field they arrive in has moved.  The first must be a clean miss; the second
must stay a breakage.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from dreamjob.adapters.directories.opencorporates import OpenCorporatesAdapter
from dreamjob.adapters.registries.companies_house import CompaniesHouseAdapter
from dreamjob.adapters.registries.kbo import KBOAdapter
from dreamjob.adapters.registries.kvk import KvKAdapter
from dreamjob.adapters.registries.nbb import NBBAdapter
from dreamjob.adapters.registries.sec_edgar import SECEdgarAdapter
from dreamjob.pipeline import collection


class StubResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code
        self.content = text.encode()
        self.url = "https://register.test/search"
        self.raw_document_id = None
        self.from_cache = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class StubEgress:
    """One body, however it is asked for."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.urls: list[str] = []

    async def fetch(self, url: str, **kwargs: Any) -> StubResponse:
        self.urls.append(url)
        return StubResponse(self.body)


def _adapter(cls: type, body: dict | list) -> tuple[Any, StubEgress]:
    """An adapter that will be answered with ``body``, and holds no key it needs."""
    egress = StubEgress(json.dumps(body))
    adapter = cls(egress=egress)
    # The keys are per-deployment and read from the settings table; the register
    # is stubbed, so what matters is only that the header can be built.
    adapter.api_key = lambda: "test-key"  # type: ignore[method-assign]
    return adapter, egress


async def _search(adapter: Any, egress: StubEgress) -> None:
    """Whichever call this resolver reaches its source's index with."""
    if isinstance(adapter, NBBAdapter):
        await adapter.references("0403170701", egress=egress)
    elif isinstance(adapter, SECEdgarAdapter):
        await adapter.resolve_cik({"name": "Nobia"}, egress=egress)
    else:
        await adapter.search("Nobia", egress=egress)


#: One row per resolver: the body in which the source states it found nothing,
#: and the body in which it states it found several under a field that has
#: moved.  The second is a genuine layout change and nothing else - the counts
#: are non-zero and the records are real.
RESOLVERS: list[tuple[str, type, Any, Any]] = [
    (
        "kvk",
        KvKAdapter,
        {"pagina": 1, "totaal": 0, "resultaten": []},
        {"pagina": 1, "totaal": 3, "items": [{"kvkNummer": "69599084", "naam": "Adyen N.V."}]},
    ),
    (
        "companies_house",
        CompaniesHouseAdapter,
        {"total_results": 0, "items": []},
        {"total_results": 2, "results": [{"company_number": "00445790", "title": "TESCO PLC"}]},
    ),
    (
        "opencorporates",
        OpenCorporatesAdapter,
        {"results": {"total_count": 0, "companies": []}},
        {"data": {"total_count": 2, "companies": [{"company": {"name": "NOBIA AB"}}]}},
    ),
    (
        # The deposit list has no count of its own, so the container's presence
        # is the evidence: an object without ``References`` is a body this
        # adapter can no longer read, not an enterprise that has filed nothing.
        "nbb",
        NBBAdapter,
        {"References": []},
        {"references": [{"ReferenceNumber": "23123456", "ExerciseDates": {"endDate": "2024-12-31"}}]},
    ),
    (
        # EDGAR's index is keyed on two fields.  If only one still parses the
        # index looks usable, and every lookup against the missing half reads as
        # "not an SEC filer" - for the life of the process, because it is cached.
        "sec_edgar",
        SECEdgarAdapter,
        {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}},
        {"0": {"cik_str": 320193, "ticker": "AAPL", "name": "Apple Inc."}},
    ),
]


@pytest.fixture(autouse=True)
def _forget_the_edgar_index() -> Any:
    """The ticker index is cached on the class, so it outlives one test."""
    SECEdgarAdapter._ticker_index = None
    yield
    SECEdgarAdapter._ticker_index = None


@pytest.mark.parametrize(
    ("name", "cls", "empty", "_moved"), RESOLVERS, ids=[r[0] for r in RESOLVERS]
)
async def test_a_source_that_states_it_holds_nothing_is_a_clean_miss(
    name: str, cls: type, empty: Any, _moved: Any
) -> None:
    adapter, egress = _adapter(cls, empty)

    await _search(adapter, egress)

    assert adapter.stated_empty == 1, f"[{name}] the source answered, and the answer was none"
    outcome = collection.ItemOutcome(requests=1, pages=1, stated_empty=adapter.stated_empty)
    assert outcome.state() == "no_matches"
    assert collection._STATE_STATUS[outcome.state()] == "done"


@pytest.mark.parametrize(
    ("name", "cls", "_empty", "moved"), RESOLVERS, ids=[r[0] for r in RESOLVERS]
)
async def test_a_source_that_reshaped_its_answer_is_still_a_breakage(
    name: str, cls: type, _empty: Any, moved: Any
) -> None:
    """The regression this whole change risks: NFR-403 silenced by its own fix."""
    adapter, egress = _adapter(cls, moved)

    await _search(adapter, egress)

    assert adapter.stated_empty == 0, (
        f"[{name}] the source stated it holds several and the field they arrive in moved; "
        "reporting that as 'read successfully, nothing to collect' is the alarm going quiet"
    )
    outcome = collection.ItemOutcome(requests=1, pages=1, stated_empty=adapter.stated_empty)
    assert outcome.state() == "extracted_nothing"
    assert collection._STATE_STATUS[outcome.state()] == "failed"
    assert outcome.extraction_rate == 0.0, "and it still feeds the breakage detector"


# ---------------------------------------------------------------------------
# The same trap in KBO's HTML, where there is no count to read
# ---------------------------------------------------------------------------

#: Two result rows as Public Search prints them.  ``ENT`` is a registered
#: entity, ``VE`` an establishment unit of one; the register prints ``EU`` too.
_ROW = (
    '<tr><td>1</td><td>{kind}<br/>Actief</td>'
    '<td class="benaming"><a href="/kbopub/toonondernemingps.html?ondernemingsnummer=473191041">'
    "FORUM JOBS AALTER NV</a></td><td>2020</td>"
    "<td>Dumortierlaan 70 8300 Knokke-Heist</td></tr>"
)


def _hit_list(kind: str) -> str:
    return f"<html><body><table>{_ROW.format(kind=kind)}</table></body></html>"


def test_a_hit_list_of_establishments_only_is_a_real_answer() -> None:
    """5 of the stored result pages genuinely are this: establishments, no entity."""
    match = KBOAdapter.match_search_result(_hit_list("VE"), "SOME OTHER COMPANY NV")

    assert match.decision == "no_match"
    assert match.answered is True


def test_a_hit_list_whose_type_column_moved_is_not_an_answer() -> None:
    """The one place KBO could still have confused the two.

    An unrecognised marker used to be read as an establishment unit, so a
    results table whose columns were relabelled looked exactly like the register
    answering "establishments of this name, but no registered entity" - which is
    a real answer.  ``_row_kind`` keeps them apart.
    """
    match = KBOAdapter.match_search_result(_hit_list("ONDERNEMING"), "FORUM JOBS AALTER NV")

    assert match.answered is False, "a row we cannot classify is not evidence of an answer"
    assert "nothing could be read" in match.reason


def test_the_markers_the_register_actually_prints_are_all_recognised() -> None:
    """1072 ``ENT``, 549 ``EU`` and 75 ``VE`` rows are stored; none may go unread."""
    for kind in ("ENT", "EU", "VE"):
        rows = KBOAdapter.parse_search_results(_hit_list(kind))
        assert rows and rows[0]["kind"] == kind
    assert KBOAdapter.parse_search_results(_hit_list("ONDERNEMING"))[0]["kind"] == ""
