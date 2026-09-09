"""Resolving a company's domain at scale (FR-301, FR-303, FR-304, FR-305, CR-405).

The contact ladder cannot spell an address before it knows a domain, and this
module is what gives it one for the 867 companies that carry none.  Everything
here runs against a throwaway SQLite file with DNS and HTTP stubbed, because
the rules worth testing are refusals: that a domain is never spelled from a
subset of a one-word name, that a page which only carries its own title is not
evidence, that a one-identifying-word name is not confirmed off a generic
top-level domain - the shape every namesake in the corpus had - and that a
company whose ladder produces nothing is written down as unresolved rather than
given a domain somebody else owns.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_all, query_one, utcnow
from dreamjob.db.repositories import company_domains as repo
from dreamjob.pipeline import company_domains as pipeline
from dreamjob.pipeline import email_validate as validation

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_SESSION_SECRET",
    "DREAMJOB_ENV",
    "DEEPSEEK_API_KEY",
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_SESSION_SECRET"] = secrets.token_urlsafe(32)
    os.environ["DREAMJOB_ENV"] = "development"
    os.environ["DEEPSEEK_API_KEY"] = ""
    get_settings.cache_clear()

    from dreamjob.db.migrator import migrate

    migrate()
    yield

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _company(name: str, *, country: str = "BE", domain: str | None = None, **extra: Any) -> str:
    return insert_row(
        "company",
        {
            "name": name,
            "normalised_name": name.lower(),
            "domain": domain,
            "country": country,
            "collected_at": utcnow(),
            **extra,
        },
    )


def _vacancy(company_id: str, **extra: Any) -> str:
    return insert_row(
        "vacancy",
        {
            "company_id": company_id,
            "title": "Data Engineer",
            "country": "BE",
            "posted_at": utcnow(),
            "collected_at": utcnow(),
            **extra,
        },
    )


def _page(body: str, *, title: str = "", lang: str = "nl") -> str:
    return f"<html lang='{lang}'><head><title>{title}</title></head><body>{body}</body></html>"


#: Enough prose that a page counts as one, plus the Belgian marker the gate
#: looks for when a domain is not on the market's own top-level domain.
_PROSE = (
    "<p>Wij zijn een onderneming in Belgie met kantoren in Gent en Antwerpen. "
    "Bel ons op +32 9 123 45 67 of kom langs. " * 4
    + "</p>"
)


def _offline(monkeypatch: pytest.MonkeyPatch, *, mx: bool = True) -> None:
    monkeypatch.setattr(
        pipeline.validation,
        "_resolve_mx",
        lambda domain, timeout=5.0: validation.MXResult(
            has_mx=mx, hosts=["mx.test"] if mx else []
        ),
    )


class _Page:
    def __init__(self, text: str, status_code: int = 200, url: str = "") -> None:
        self.text = text
        self.status_code = status_code
        self.url = url

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class _Egress:
    """One canned page per domain; anything else answers 404."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    async def fetch(self, url: str, **_: Any) -> _Page:
        self.requested.append(url)
        for key, body in self.pages.items():
            if key in url:
                return _Page(body, 200, url)
        return _Page("", 404, url)


def _resolve(company_id: str, egress: _Egress, **kwargs: Any) -> pipeline.DomainOutcome:
    company = repo.companies_without_domain(10)[0]
    assert company["company_id"] == company_id
    return asyncio.run(
        pipeline.resolve_company(company, egress=egress, use_register=False, **kwargs)
    )


# ---------------------------------------------------------------------------
# Spelling
# ---------------------------------------------------------------------------


def test_a_subset_of_a_one_word_name_is_never_spelled() -> None:
    """CR-405: ``recruitment.be`` and ``absolute.be`` belong to somebody else."""
    house = pipeline.spellings("HOUSE OF RECRUITMENT SOLUTIONS BV", "BE")
    assert "recruitment.be" not in house
    assert "recruitment.com" not in house
    assert house[0] == "houserecruitmentsolutions.be"

    absolute = pipeline.spellings("ABSOLUTE@WORK BV", "BE")
    assert "absolute.be" not in absolute
    assert "absolutework.be" in absolute


def test_generic_words_are_dropped_only_when_two_identifying_words_survive() -> None:
    isar = pipeline.spellings("ISAR Aerospace Technologies GmbH", "DE")
    assert "isaraerospace.de" in isar
    # "Quality" alone is a word, not an identity, so the subset is not offered.
    assert "quality.be" not in pipeline.spellings("Quality Jobs@work BV", "BE")


def test_german_names_are_spelled_the_way_german_writes_them() -> None:
    spellings = pipeline.spellings("Rügamer & Steiner Consulting GmbH", "DE")
    assert "ruegamer-steiner.de" in spellings
    assert "rugamer-steiner.de" in spellings


def test_the_market_top_level_domain_is_tried_before_com() -> None:
    spellings = pipeline.spellings("Hoorcentrum Aerts BV", "BE")
    assert spellings.index("hoorcentrumaerts.be") < spellings.index("hoorcentrumaerts.com")


def test_a_board_is_never_a_candidate() -> None:
    assert pipeline._employer_domain("arbeitnow.fr") == ""
    assert pipeline._employer_domain("youtube.com") == ""
    assert pipeline._employer_domain("gmail.com") == ""
    assert pipeline._employer_domain("acmedata.be") == "acmedata.be"


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_a_derived_domain_needs_more_than_a_mail_exchanger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-405: MX plus a page that is somebody else's is a rejection."""
    _offline(monkeypatch)
    company_id = _company("Acme Data BV")
    _vacancy(company_id)
    egress = _Egress({"acmedata.be": _page(_PROSE, title="A different company entirely")})

    outcome = _resolve(company_id, egress)

    assert outcome.status == "unresolved"
    assert outcome.domain is None
    assert any(j.outcome == "rejected" for j in outcome.judged)


def test_a_derived_domain_whose_page_names_the_company_is_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _offline(monkeypatch)
    company_id = _company("Acme Data BV")
    _vacancy(company_id)
    egress = _Egress({"acmedata.be": _page(f"<h1>Acme Data</h1>{_PROSE}", title="Acme Data")})

    outcome = _resolve(company_id, egress)

    assert outcome.status == "resolved"
    assert outcome.domain == "acmedata.be"
    assert outcome.source == pipeline.SOURCE_DERIVED


def test_a_page_that_is_only_a_title_is_not_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """``konverthr.com`` answers 200 with its own name and nothing else."""
    _offline(monkeypatch)
    company_id = _company("Konvert HR NV")
    _vacancy(company_id)
    egress = _Egress({"konverthr": _page("", title="KonvertHR")})

    outcome = _resolve(company_id, egress)

    assert outcome.status == "unresolved"
    assert any("characters of text" in j.reason for j in outcome.judged)


def test_a_one_word_name_is_not_confirmed_off_a_generic_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``adequat.eu`` rule: every namesake in the corpus had this shape."""
    _offline(monkeypatch)
    company_id = _company("Adéquat Belgium NV")
    _vacancy(company_id)
    # The page is titled exactly for the company and does show Belgium - and it
    # is a fencing shop.  Only the top-level domain separates the two.
    egress = _Egress({"adequat.eu": _page(_PROSE, title="Adéquat")})

    outcome = _resolve(company_id, egress)

    assert outcome.status == "unresolved"
    assert any("is not a BE domain" in j.reason for j in outcome.judged)


def test_a_one_word_name_is_confirmed_on_the_market_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _offline(monkeypatch)
    company_id = _company("Weerwerk VZW")
    _vacancy(company_id)
    egress = _Egress({"weerwerk.be": _page(_PROSE, title="Weerwerk")})

    outcome = _resolve(company_id, egress)

    assert outcome.status == "resolved"
    assert outcome.domain == "weerwerk.be"


def test_a_company_that_cannot_be_resolved_is_recorded_as_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _offline(monkeypatch, mx=False)
    company_id = _company("Nowhere Data Systems BV")
    _vacancy(company_id)

    report = asyncio.run(
        pipeline.resolve_domains(10, concurrency=2, use_register=False, egress=_Egress({}))
    )

    assert report.companies_resolved == 0
    assert report.companies_unresolved == 1
    row = repo.resolution_for(company_id)
    assert row is not None
    assert row["status"] == "unresolved"
    assert query_one("SELECT domain FROM company WHERE id = ?", (company_id,))["domain"] is None


# ---------------------------------------------------------------------------
# The rungs above derivation
# ---------------------------------------------------------------------------


def test_an_address_the_employer_published_is_taken_without_a_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _offline(monkeypatch)
    company_id = _company("Somewhere Else BV")
    _vacancy(company_id, description="Stuur je cv naar jobs@somewhere-else.be alstublieft.")
    egress = _Egress({})

    outcome = _resolve(company_id, egress)

    assert outcome.status == "resolved"
    assert outcome.domain == "somewhere-else.be"
    assert outcome.source == pipeline.SOURCE_VACANCY_TEXT
    assert egress.requested == []


def test_a_link_in_a_posting_is_a_candidate_and_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A posting links partners and video as readily as it links its employer."""
    _offline(monkeypatch)
    company_id = _company("Partner Free Systems BV")
    _vacancy(company_id, description="Zie ook www.somebodyelse.be voor onze partner.")
    egress = _Egress({"somebodyelse.be": _page(_PROSE, title="Somebody Else")})

    outcome = _resolve(company_id, egress, derive=False)

    assert outcome.status == "unresolved"
    assert [j.outcome for j in outcome.judged] == ["rejected"]


# ---------------------------------------------------------------------------
# What is written down
# ---------------------------------------------------------------------------


def test_the_pass_writes_the_domain_the_refusals_and_the_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _offline(monkeypatch)
    company_id = _company("Acme Data BV")
    _vacancy(company_id)
    _vacancy(company_id)
    egress = _Egress({"acmedata.be": _page(f"<h1>Acme Data</h1>{_PROSE}", title="Acme Data")})

    report = asyncio.run(
        pipeline.resolve_domains(10, concurrency=2, use_register=False, egress=egress)
    )

    assert report.companies_resolved == 1
    assert report.domains_written == 1
    assert report.vacancies_unlocked == 2
    assert report.candidates_rejected == report.candidates_judged - 1

    assert query_one("SELECT domain FROM company WHERE id = ?", (company_id,))[
        "domain"
    ] == "acmedata.be"
    resolution = repo.resolution_for(company_id)
    assert resolution["status"] == "resolved"
    assert resolution["source"] == pipeline.SOURCE_DERIVED
    assert resolution["gate_version"] == "v2"
    assert resolution["vacancy_count"] == 2

    judged = query_all(
        "SELECT domain, outcome FROM company_domain_candidate WHERE company_id = ?",
        (company_id,),
    )
    assert ("acmedata.be", "confirmed") in [(r["domain"], r["outcome"]) for r in judged]
    assert repo.summary()["companies_with_domain"] == 1


def test_an_identity_verdict_never_reaches_the_domain_keyed_probe_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-305: two companies can spell to one domain, and the answer differs.

    ``apply_domain_probe`` is keyed on the domain, so only what is true of the
    domain - it does not resolve, it did not answer - may be cached there.
    """
    _offline(monkeypatch)
    company_id = _company("Acme Data BV")
    _vacancy(company_id)
    egress = _Egress({"acmedata.be": _page(f"<h1>Acme Data</h1>{_PROSE}", title="Acme Data")})

    _resolve(company_id, egress)

    outcomes = {
        row["domain"]: row["outcome"]
        for row in query_all("SELECT domain, outcome FROM apply_domain_probe")
    }
    assert "acmedata.be" not in outcomes
    assert set(outcomes.values()) <= {"no_mx", "unreachable"}


def test_a_revoked_domain_is_never_handed_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """NFR-402: the gate took this domain away once; it does not return it."""
    _offline(monkeypatch)
    company_id = _company("Acme Data BV")
    _vacancy(company_id)
    egress = _Egress({"acmedata.be": _page(f"<h1>Acme Data</h1>{_PROSE}", title="Acme Data")})

    outcome = asyncio.run(
        pipeline.resolve_company(
            repo.companies_without_domain(10)[0],
            egress=egress,
            use_register=False,
            revoked={(company_id, "acmedata.be")},
        )
    )

    assert outcome.status == "unresolved"
    assert any(j.outcome == "revoked" for j in outcome.judged)


def test_a_published_address_off_the_company_domain_is_not_taken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``student@work.be`` is the government's portal, not Natuurwerk's domain."""
    _offline(monkeypatch)
    company_id = _company("Natuurwerk VZW")
    _vacancy(
        company_id,
        description="Ben je student? Schrijf je in via student@work.be. Zie www.natuurwerk.be.",
    )
    egress = _Egress({"natuurwerk.be": _page(f"<h1>Natuurwerk</h1>{_PROSE}", title="Natuurwerk")})

    outcome = _resolve(company_id, egress)

    assert outcome.domain == "natuurwerk.be"
    assert not pipeline.names_the_company("work.be", "Natuurwerk VZW")
    assert pipeline.names_the_company("absolutejobs.be", "ABSOLUTE@WORK BV")


def test_an_acronym_is_not_spelled_into_a_three_letter_domain() -> None:
    """``prs.com`` is titled "PRS" and belongs to somebody else entirely."""
    assert "prs.com" not in pipeline.spellings("P.R.S. NV", "BE")
    assert "prs.be" not in pipeline.spellings("P.R.S. NV", "BE")


def test_a_link_whose_page_calls_itself_the_company_is_taken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _offline(monkeypatch)
    company_id = _company("Knowbe4")
    _vacancy(company_id, description="Lees meer op www.knowbe4.com over ons.")
    egress = _Egress({"knowbe4.com": _page(f"<h1>KnowBe4</h1>{_PROSE}", title="KnowBe4")})

    outcome = _resolve(company_id, egress, derive=False)

    assert outcome.status == "resolved"
    assert outcome.source == pipeline.SOURCE_VACANCY_LINK
