"""Contacts at scale and the Apply Browser's list query
(FR-301, FR-303, FR-304, FR-305, FR-306, FR-324, NFR-302, NFR-502, CR-405).

Everything runs against a throwaway SQLite file.  The two things that would
reach the outside world - the MX lookup and the home-page fetch - are stubbed,
which is the point rather than a convenience: the rules worth testing are that
a derived domain is *not* accepted on MX alone (CR-405), that an FR-304
``invalid`` verdict removes an address from the ladder, and that a company the
ladder cannot reach is recorded as unreachable instead of being given a made-up
address.
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
from dreamjob.db.repositories import apply as repo
from dreamjob.db.repositories import company_domains as domain_repo
from dreamjob.db.repositories import contacts as contacts_repo
from dreamjob.db.repositories import registry_identity as identity_repo
from dreamjob.pipeline import apply_contacts as pipeline
from dreamjob.pipeline import email_patterns as patterns
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


def _seeker() -> dict[str, str]:
    now = utcnow()
    seeker_id = insert_row(
        "job_seeker",
        {"email": "seeker@example.org", "display_name": "Test", "created_at": now,
         "updated_at": now},
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": {}, "created_at": now},
    )
    directive_id = insert_row(
        "directive_set", {"job_seeker_id": seeker_id, "name": "default", "created_at": now}
    )
    campaign_id = insert_row(
        "campaign",
        {"job_seeker_id": seeker_id, "directive_set_id": directive_id,
         "profile_version_id": profile_id, "name": "spring", "status": "running",
         "created_at": now},
    )
    return {"seeker_id": seeker_id, "campaign_id": campaign_id}


def _company(name: str, *, domain: str | None = None, country: str = "BE", **extra: Any) -> str:
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


def _vacancy(company_id: str, title: str = "Data Engineer", **extra: Any) -> str:
    return insert_row(
        "vacancy",
        {
            "company_id": company_id,
            "title": title,
            "country": "BE",
            "posted_at": utcnow(),
            "collected_at": utcnow(),
            **extra,
        },
    )


def _opportunity(seed: dict[str, str], company_id: str, **extra: Any) -> str:
    now = utcnow()
    return insert_row(
        "opportunity",
        {
            "job_seeker_id": seed["seeker_id"],
            "campaign_id": seed["campaign_id"],
            "company_id": company_id,
            "kind": "vacancy",
            "title": "Lead Data Engineer",
            "created_at": now,
            "updated_at": now,
            **extra,
        },
    )


def _offline(monkeypatch: pytest.MonkeyPatch, *, mx: bool = True) -> None:
    """No DNS and no SMTP; FR-304 then answers from syntax and MX alone."""
    monkeypatch.setattr(
        pipeline.validation, "_resolve_mx",
        lambda domain, timeout=5.0: validation.MXResult(has_mx=mx, hosts=["mx.test"] if mx else []),
    )
    monkeypatch.setattr(validation.limits.__class__, "smtp_enabled", property(lambda self: False))


class _Page:
    """The shape of the egress client's answer, without the egress client."""

    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class _Egress:
    """A stub egress client: one canned page per URL, and a request counter."""

    def __init__(self, pages: dict[str, str], status: int = 200) -> None:
        self.pages = pages
        self.status = status
        self.requested: list[str] = []

    async def fetch(self, url: str, **_: Any) -> _Page:
        self.requested.append(url)
        for key, body in self.pages.items():
            if url.rstrip("/").endswith(key.rstrip("/")) or key in url:
                return _Page(body, self.status)
        return _Page("", 404)


# ---------------------------------------------------------------------------
# Spelling a domain from a name
# ---------------------------------------------------------------------------


def test_name_tokens_drop_the_legal_form() -> None:
    assert pipeline.name_tokens("NOEL FRANKLIN BV") == ["noel", "franklin"]
    assert pipeline.name_tokens("Rügamer & Steiner Consulting GmbH") == [
        "rugamer", "steiner", "consulting"
    ]
    # A name that is only a legal form keeps its words; there is nothing else.
    assert pipeline.name_tokens("Holding Group NV") == ["holding", "group", "nv"]
    # A one-letter word is part of the identity: dropping it spelled energy.de.
    assert pipeline.name_tokens("Q Energy") == ["q", "energy"]
    assert pipeline.name_tokens("") == []


def test_candidate_domains_never_spell_from_the_first_word_alone() -> None:
    """CR-405: the spelling that produced ``house.com`` is not offered."""
    candidates = pipeline.candidate_domains("HOUSE OF RECRUITMENT SOLUTIONS BV", "BE")
    assert "house.com" not in candidates
    assert "house.be" not in candidates
    assert candidates[0] == "houserecruitmentsolutions.be"


def test_a_one_letter_word_is_not_thrown_away() -> None:
    """CR-405: dropping the "Q" spelled ``energy.de``, which confirmed itself."""
    candidates = pipeline.candidate_domains("Q Energy", "DE")
    assert "energy.de" not in candidates
    assert candidates[0] == "qenergy.de"


def test_a_single_short_word_spells_no_domain() -> None:
    """``fsc.de`` and ``alan.fr`` are somebody's; a three-letter name cannot say whose."""
    assert pipeline.candidate_domains("Fsc", "DE") == []
    assert pipeline.candidate_domains("Alan", "FR") == []
    assert pipeline.candidate_domains("Seccl", "GB")[0] == "seccl.co.uk"


def test_a_one_word_company_is_confirmed_by_the_title_not_the_copy() -> None:
    body = "<title>Strom und Gas</title><body>" + "energy for everyone. " * 20 + "</body>"
    titled = "<title>Energy - home</title><body>" + "we sell things. " * 20 + "</body>"
    assert pipeline.company_named_on_page("Energy", body)[0] is False
    assert pipeline.company_named_on_page("Energy", titled)[0] is True


def test_candidate_domains_use_the_market_tld_and_a_name_that_is_a_domain() -> None:
    assert pipeline.candidate_domains("Acme Data", "DE")[0] == "acmedata.de"
    assert pipeline.candidate_domains("Acme Data", None)[0] == "acmedata.com"
    assert pipeline.candidate_domains("Taxtalente.de", None)[0] == "taxtalente.de"
    assert pipeline.candidate_domains("BV", "BE") == []


def test_aggregator_and_freemail_domains_are_not_employer_domains() -> None:
    assert pipeline.usable_employer_domain("job-boards.greenhouse.io") == ""
    assert pipeline.usable_employer_domain("gmail.com") == ""
    assert pipeline.usable_employer_domain("europa.eu") == ""
    assert pipeline.usable_employer_domain("acme-data.example") == "acme-data.example"


# ---------------------------------------------------------------------------
# CR-405: the confirmation gate
# ---------------------------------------------------------------------------


def test_a_page_that_names_the_company_confirms_it() -> None:
    html = (
        "<title>Noel Franklin</title><body><p>Noel Franklin is a family business in "
        "Ghent employing forty people across three sites in Flanders. We have made "
        "industrial fasteners for the automotive trade since nineteen seventy four "
        "and supply customers throughout Belgium, the Netherlands and northern "
        "France from our own warehouse.</p></body>"
    )
    ok, why = pipeline.company_named_on_page("NOEL FRANKLIN BV", html)
    assert ok is True
    assert "noel" in why


def test_a_page_that_only_repeats_its_own_url_does_not_confirm() -> None:
    """The circular evidence that would make every candidate confirm itself."""
    html = (
        "<title>Welcome</title><body><p>Visit https://noelfranklin.be or write to "
        "info@noelfranklin.be. We sell industrial fasteners to the automotive trade "
        "across Europe and have done so since nineteen seventy four, from premises "
        "that we have occupied for the whole of that time.</p></body>"
    )
    ok, why = pipeline.company_named_on_page("NOEL FRANKLIN BV", html)
    assert ok is False
    assert "does not name" in why


def test_a_parked_page_does_not_confirm() -> None:
    html = "<title>noelfranklin.be</title><body><p>This domain is for sale. " \
           "Noel Franklin. Buy it now from our partner registrar today.</p></body>"
    ok, why = pipeline.company_named_on_page("NOEL FRANKLIN BV", html)
    assert ok is False
    assert "placeholder" in why


def test_confirm_domain_rejects_mx_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mail exchanger is necessary and nowhere near sufficient (CR-405)."""
    _offline(monkeypatch)
    egress = _Egress({"someoneelse.be": "<title>A different company</title>" + "x " * 200})
    verdict = asyncio.run(
        pipeline.confirm_domain("someoneelse.be", "NOEL FRANKLIN BV", egress=egress)
    )
    assert verdict.outcome == "rejected"
    assert repo.domain_probe("someoneelse.be")["outcome"] == "rejected"


def test_confirm_domain_skips_http_when_the_domain_cannot_receive_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-305: the cheap check first, so nobody's server is fetched for nothing."""
    _offline(monkeypatch, mx=False)
    egress = _Egress({"acme.be": "<html>Acme</html>"})
    verdict = asyncio.run(pipeline.confirm_domain("acme.be", "Acme BV", egress=egress))
    assert verdict.outcome == "no_mx"
    assert egress.requested == []


def test_confirm_domain_is_cached_and_not_probed_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-305: one verdict per domain."""
    _offline(monkeypatch)
    page = "<title>Acme Data</title><body>" + "Acme Data builds pipelines. " * 20 + "</body>"
    egress = _Egress({"acmedata.be": page})
    first = asyncio.run(pipeline.confirm_domain("acmedata.be", "Acme Data BV", egress=egress))
    second = asyncio.run(pipeline.confirm_domain("acmedata.be", "Acme Data BV", egress=egress))
    assert first.confirmed and second.confirmed
    assert first.from_cache is False and second.from_cache is True
    assert len(egress.requested) == 1
    assert repo.domain_probe("acmedata.be")["probe_count"] == 1


# ---------------------------------------------------------------------------
# FR-303: the vacancy's own application target comes before the network
# ---------------------------------------------------------------------------


def test_the_application_target_is_read_before_anything_is_fetched() -> None:
    hints = [
        {"application_target": "jobs@acme-data.example", "source_url": "https://board/1",
         "description": None},
        {"application_target": "https://job-boards.greenhouse.io/x", "source_url": "",
         "description": "Stuur je cv naar sollicitatie@acme-data.example."},
    ]
    found = {a.email: a for a in pipeline.addresses_in_vacancies(hints)}
    assert set(found) == {"jobs@acme-data.example", "sollicitatie@acme-data.example"}
    assert all(a.method == patterns.METHOD_VACANCY for a in found.values())


def test_an_aggregator_address_in_the_body_is_not_the_employers_inbox() -> None:
    hints = [{"application_target": None, "source_url": "",
              "description": "Questions? Write to helpdesk@europa.eu about this portal."}]
    assert pipeline.addresses_in_vacancies(hints) == []


# ---------------------------------------------------------------------------
# FR-304: validation decides, and never invents
# ---------------------------------------------------------------------------


def test_first_usable_skips_an_invalid_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _offline(monkeypatch)
    monkeypatch.setattr(pipeline.validation, "is_disposable", lambda d: d == "throwaway.example")
    candidates = [
        pipeline.discovery.ContactCandidate(
            full_name=None, role_title=None, tier="hr", source="s", confidence=0.9,
            email="hr@throwaway.example", email_source_method=patterns.METHOD_WEBSITE,
        ),
        pipeline.discovery.ContactCandidate(
            full_name=None, role_title=None, tier="generic_mailbox", source="s", confidence=0.4,
            email="jobs@acme-data.example", email_source_method=patterns.METHOD_PATTERN,
            is_generic_mailbox=True,
        ),
    ]
    chosen = pipeline.first_usable(candidates)
    assert chosen is not None
    assert chosen[0].email == "jobs@acme-data.example"
    assert chosen[1].result != validation.INVALID


def test_first_usable_returns_none_when_everything_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _offline(monkeypatch, mx=False)
    candidates = [
        pipeline.discovery.ContactCandidate(
            full_name=None, role_title=None, tier="generic_mailbox", source="s", confidence=0.4,
            email="jobs@nowhere.example", email_source_method=patterns.METHOD_PATTERN,
        )
    ]
    assert pipeline.first_usable(candidates) is None


# ---------------------------------------------------------------------------
# The whole ladder, per company
# ---------------------------------------------------------------------------


def _work_row(company_id: str, name: str, count: int = 3) -> dict[str, Any]:
    return {
        "company_id": company_id,
        "company_name": name,
        "company_domain": None,
        "careers_url": None,
        "company_country": "BE",
        "vacancy_count": count,
    }


def test_a_known_contact_is_reused_and_costs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _offline(monkeypatch)
    company_id = _company("Acme Data BV", domain="acme-data.example")
    _vacancy(company_id)
    contacts_repo.upsert_contact(
        {
            "company_id": company_id,
            "full_name": "Sofie Claes",
            "role_title": "Talent Acquisition Manager",
            "email": "sofie.claes@acme-data.example",
            "email_source_method": patterns.METHOD_WEBSITE,
            "email_validation": "valid",
            "collected_at": utcnow(),
            "confidence": 0.8,
        }
    )
    egress = _Egress({})
    outcome = asyncio.run(
        pipeline.resolve_company(_work_row(company_id, "Acme Data BV"), egress=egress)
    )
    assert outcome.reachable and outcome.reused
    assert outcome.email == "sofie.claes@acme-data.example"
    assert egress.requested == []
    assert repo.get_resolution(company_id)["status"] == "reachable"


def test_an_address_in_the_vacancy_beats_a_crawl(monkeypatch: pytest.MonkeyPatch) -> None:
    _offline(monkeypatch)
    company_id = _company("Acme Data BV", domain="acme-data.example")
    _vacancy(company_id, application_channel="email", application_target="jobs@acme-data.example")
    egress = _Egress({})
    outcome = asyncio.run(
        pipeline.resolve_company(
            _work_row(company_id, "Acme Data BV"), egress=egress, crawl_site=False
        )
    )
    assert outcome.reachable
    assert outcome.email == "jobs@acme-data.example"
    assert outcome.method == patterns.METHOD_VACANCY
    row = query_one("SELECT * FROM contact WHERE email = ?", ("jobs@acme-data.example",))
    assert row is not None
    assert row["email_source_method"] == patterns.METHOD_VACANCY
    # FR-306: nothing beyond the minimum was stored about anybody.
    assert row["full_name"] is None


def test_an_unreachable_company_is_recorded_not_invented(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rule the brief puts above the target: never make an address up."""
    _offline(monkeypatch)
    company_id = _company("Obscure Family Bakery BV", domain=None)
    _vacancy(company_id)
    # Every candidate domain answers 404, so nothing is confirmed.
    egress = _Egress({}, status=404)
    outcome = asyncio.run(
        pipeline.resolve_company(_work_row(company_id, "Obscure Family Bakery BV"), egress=egress)
    )
    assert outcome.status == "unreachable"
    assert outcome.email is None
    assert query_all("SELECT id FROM contact WHERE company_id = ?", (company_id,)) == []
    resolution = repo.get_resolution(company_id)
    assert resolution["status"] == "unreachable"
    assert "none confirmed" in resolution["reason"]
    assert pipeline._reason_bucket(resolution["reason"]) == "no domain could be confirmed"


def test_the_ladder_does_not_re_spell_what_the_domain_gate_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-405: the weaker spelling never runs behind the stronger one's back.

    ``apply_contacts`` used to spell its own candidates and judge them with
    :func:`confirm_domain`, which does not have the four rules the domain pass
    grew out of live evidence (the market top-level domain a one-word name has
    to sit on, the 180-character floor, the five-character base, the
    revocations).  A company that pass has already refused must therefore not
    be spelled again here, or every namesake it took back comes back.
    """
    _offline(monkeypatch)
    company_id = _company("Adéquat Belgium NV", domain=None)
    _vacancy(company_id)
    domain_repo.record_resolution(
        {
            "company_id": company_id,
            "company_name": "Adéquat Belgium NV",
            "status": "unresolved",
            "reason": "adequat.eu is not a BE domain, which is the shape every namesake had",
            "market": "BE",
            "market_source": "company",
            "vacancy_count": 1,
        }
    )
    egress = _Egress({"adequat.eu": "<title>Adéquat</title>" + "Belgium " * 60})
    outcome = asyncio.run(
        pipeline.resolve_company(_work_row(company_id, "Adéquat Belgium NV"), egress=egress)
    )
    assert outcome.status == "unreachable"
    assert outcome.domain is None
    # Nothing was fetched at all: the verdict was already on record.
    assert egress.requested == []
    assert "the domain ladder judged this company" in repo.get_resolution(company_id)["reason"]


def test_a_domain_in_a_posting_is_not_the_employers_domain_on_sight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rung that wrote ``jobs@just.fgov.be`` for a company it was taken from.

    A posting carries the employer's address, and it also carries the town's,
    the government portal's and the agency's.  Reading the domain off the first
    one and then *spelling* a careers mailbox on it is how the ladder invented
    an address at a domain the identity gate had already revoked as a namesake
    for that very company.  The domain question - revocations included - belongs
    to :mod:`company_domains`, and a domain whose label carries no word of the
    company's name has to be judged before anything is spelled on it.
    """
    _offline(monkeypatch)
    company_id = _company("Nationaal Instituut voor Criminalistiek", domain=None)
    _vacancy(
        company_id,
        description="Solliciteren kan via selecties@just.fgov.be voor 1 oktober.",
    )
    identity_repo.revoke_domain(
        company_id,
        company_name="Nationaal Instituut voor Criminalistiek",
        domain="just.fgov.be",
        reason="namesake",
    )
    egress = _Egress({"just.fgov.be": "<title>FOD Justitie</title>" + "justitie " * 60})
    outcome = asyncio.run(
        pipeline.resolve_company(
            _work_row(company_id, "Nationaal Instituut voor Criminalistiek"), egress=egress
        )
    )
    # The invariant: a domain the gate took back is never this company's domain
    # again, however the posting spells it.
    assert outcome.domain != "just.fgov.be", (
        f"a revoked domain came back as the company's own ({outcome.domain_source})"
    )
    spelled = query_all(
        "SELECT email FROM contact WHERE company_id = ? AND email_source_method != 'vacancy'",
        (company_id,),
    )
    assert spelled == [], f"an address was spelled on a revoked domain: {spelled}"


def test_a_verdict_reached_without_a_domain_is_retried_once_one_arrives() -> None:
    """The pool phase 1 unlocked: unreachable *because* there was no domain."""
    seed = _seeker()
    without = _company("Still Nameless BV", domain=None)
    _vacancy(without)
    repo.record_resolution(without, {"status": "unreachable", "reason": "no domain"})
    later = _company("Acme Data BV", domain=None)
    _vacancy(later)
    repo.record_resolution(later, {"status": "unreachable", "reason": "no domain"})

    work = repo.companies_needing_contact(50, job_seeker_id=seed["seeker_id"])
    assert [row["company_id"] for row in work] == []

    # The domain pass writes a domain long after the ladder gave up.
    repo.set_company_domain(later, "acmedata.be")
    work = repo.companies_needing_contact(50, job_seeker_id=seed["seeker_id"])
    assert [row["company_id"] for row in work] == [later]


def test_the_work_list_can_be_walked_biggest_first() -> None:
    """A target counted in vacancies is filled by the companies that carry them."""
    seed = _seeker()
    small = _company("Small BV")
    _vacancy(small)
    big = _company("Big BV")
    for _ in range(4):
        _vacancy(big)
    by_size = repo.companies_needing_contact(
        50, job_seeker_id=seed["seeker_id"], order="vacancies"
    )
    assert [row["company_id"] for row in by_size] == [big, small]
    assert by_size[0]["vacancy_count"] == 4


def test_an_address_in_an_older_posting_is_still_the_employers_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-303 rung 2 reads every posting that carries an "@", not the newest six."""
    _offline(monkeypatch)
    company_id = _company("Acme Data BV", domain="acme-data.example")
    _vacancy(
        company_id, title="Oldest", application_channel="email",
        application_target="sollicitatie@acme-data.example",
    )
    for index in range(8):
        _vacancy(company_id, title=f"Newer {index}")
    egress = _Egress({})
    outcome = asyncio.run(
        pipeline.resolve_company(
            _work_row(company_id, "Acme Data BV"), egress=egress, crawl_site=False,
        )
    )
    assert outcome.reachable
    assert outcome.email == "sollicitatie@acme-data.example"
    assert outcome.method == patterns.METHOD_VACANCY


def test_the_pass_forwards_the_backup_flag_to_the_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    """``backup=True`` reaches the backup stage through the whole batch pass."""
    _offline(monkeypatch)
    seed = _seeker()
    company_id = _company("Nowhere BV")
    _vacancy(company_id)
    seen: list[bool] = []

    async def _backup(company: dict, **kwargs: Any) -> Any:
        seen.append(True)
        return [], None, "backup ran; no address found in the stored postings or on the ATS board"

    monkeypatch.setattr(pipeline.contact_backup, "harvest_backup_addresses", _backup)
    report = asyncio.run(
        pipeline.ensure_apply_contacts(
            seed["seeker_id"], limit=10, concurrency=1, crawl_site=False,
            derive_domains=False, backup=True,
        )
    )
    assert report.companies_visited == 1
    assert seen == [True]
    # The backup attempt is visible in the report the screen reads.
    assert any(bucket.startswith("backup") for bucket in report.unreachable_reasons)


def test_a_confirmed_domain_yields_the_published_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _offline(monkeypatch)
    company_id = _company("Acme Data BV", domain=None)
    _vacancy(company_id)
    home = (
        "<title>Acme Data</title><body><p>Acme Data helps Belgian manufacturers get "
        "value out of their production data. We build the pipelines, the warehouse "
        "and the reporting, and we train the team that has to live with it after we "
        "have gone. Founded in Ghent, working across Flanders and the Netherlands.</p>"
        "<a href='https://acmedata.be/contact'>Contact</a></body>"
    )
    contact = "<body><p>Write to jobs@acmedata.be or call us.</p></body>"
    egress = _Egress({"https://acmedata.be/contact": contact, "acmedata.be": home})
    outcome = asyncio.run(
        pipeline.resolve_company(_work_row(company_id, "Acme Data BV"), egress=egress)
    )
    assert outcome.reachable
    assert outcome.domain == "acmedata.be"
    assert outcome.domain_source == pipeline.SOURCE_DERIVED
    assert outcome.email == "jobs@acmedata.be"
    # The confirmed domain is written back, so the next pass is free.
    assert query_one("SELECT domain FROM company WHERE id = ?", (company_id,))["domain"] == (
        "acmedata.be"
    )


def test_an_objection_keeps_a_company_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """NFR-302 outranks the coverage target."""
    _offline(monkeypatch)
    company_id = _company("Acme Data BV", domain="acme-data.example")
    _vacancy(company_id, application_channel="email", application_target="jobs@acme-data.example")
    contacts_repo.record_objection("jobs@acme-data.example", reason="asked not to be contacted")
    egress = _Egress({})
    outcome = asyncio.run(
        pipeline.resolve_company(
            _work_row(company_id, "Acme Data BV"), egress=egress, crawl_site=False,
            allow_generic=False,
        )
    )
    assert outcome.status == "unreachable"


# ---------------------------------------------------------------------------
# The Contacts screen's per-company control (FR-301)
# ---------------------------------------------------------------------------


def test_one_company_can_be_walked_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The screen's "Find contacts for this company" is the same ladder, one id."""
    _offline(monkeypatch)
    company_id = _company("Acme Data BV", domain="acme-data.example")
    _vacancy(company_id, application_channel="email", application_target="jobs@acme-data.example")
    outcome = asyncio.run(
        pipeline.resolve_company_by_id(
            company_id, crawl_site=False, derive_domains=False, allow_generic=False
        )
    )
    assert outcome.reachable
    assert outcome.email == "jobs@acme-data.example"
    # The verdict is recorded, so the next batch pass knows about it (FR-305).
    assert repo.get_resolution(company_id)["status"] == "reachable"


def test_the_resolution_work_row_carries_the_ladder_inputs() -> None:
    company_id = _company("Acme Data BV", domain="acme-data.example")
    row = repo.company_for_resolution(company_id)
    assert row is not None
    assert row["company_id"] == company_id
    assert row["company_name"] == "Acme Data BV"
    assert row["company_domain"] == "acme-data.example"
    assert row["vacancy_count"] == 0
    assert row["has_contact"] is False


def test_an_unknown_company_is_a_lookup_error() -> None:
    with pytest.raises(LookupError):
        asyncio.run(pipeline.resolve_company_by_id("no-such-company"))


def test_the_discovery_job_is_registered_and_runs_a_bounded_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-185/NFR-401: the whole-shortlist pass is a job, driven from a checkpoint."""
    from dreamjob.jobs.runner import runner

    _offline(monkeypatch)
    seed = _seeker()
    company_id = _company("Acme Data BV", domain="acme-data.example")
    _vacancy(company_id, application_channel="email", application_target="jobs@acme-data.example")

    assert runner._workers[pipeline.DISCOVERY_JOB_KIND] is pipeline.contacts_discovery_worker

    class _Ctx:
        job_seeker_id = seed["seeker_id"]
        campaign_id = seed["campaign_id"]
        checkpoint = {
            "options": {"limit": 1, "crawl_site": False, "derive_domains": False}
        }

        def __init__(self) -> None:
            self.saved: dict | None = None

        def save_checkpoint(self, **kwargs: Any) -> None:
            self.saved = kwargs

        def progress(self, done: int, total: int | None = None) -> None:
            pass

    ctx = _Ctx()

    async def run() -> None:
        async for _ in pipeline.contacts_discovery_worker(ctx):  # type: ignore[arg-type]
            pass

    asyncio.run(run())
    assert ctx.saved is not None
    assert ctx.saved["report"]["companies_reachable"] >= 1


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def test_the_pass_stops_once_enough_vacancies_are_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``limit`` counts vacancies, not companies (that is the whole brief)."""
    _offline(monkeypatch)
    seed = _seeker()
    for index in range(6):
        company_id = _company(f"Acme {index} BV", domain=f"acme{index}.example")
        for _ in range(4):
            _vacancy(
                company_id,
                application_channel="email",
                application_target=f"jobs@acme{index}.example",
            )

    report = asyncio.run(
        pipeline.ensure_apply_contacts(
            seed["seeker_id"], limit=8, concurrency=1, crawl_site=False, derive_domains=False
        )
    )
    assert report.already_covered == 0
    assert report.shortfall == 8
    assert report.vacancies_covered >= 8
    # Two companies of four vacancies is enough; the other four are untouched.
    assert report.companies_visited < 6
    assert report.by_method[patterns.METHOD_VACANCY] == report.companies_reachable


def test_the_limit_is_a_corpus_target_not_a_quota_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asking for 500 twice must not do the work twice."""
    _offline(monkeypatch)
    seed = _seeker()
    company_id = _company("Acme Data BV", domain="acme-data.example")
    _vacancy(company_id, application_channel="email", application_target="jobs@acme-data.example")

    first = asyncio.run(
        pipeline.ensure_apply_contacts(
            seed["seeker_id"], limit=1, concurrency=1, crawl_site=False, derive_domains=False
        )
    )
    assert first.shortfall == 1
    assert first.companies_visited == 1

    second = asyncio.run(
        pipeline.ensure_apply_contacts(
            seed["seeker_id"], limit=1, concurrency=1, crawl_site=False, derive_domains=False
        )
    )
    assert second.already_covered == 1
    assert second.shortfall == 0
    assert second.companies_visited == 0


def test_the_report_breaks_the_result_down_by_method(monkeypatch: pytest.MonkeyPatch) -> None:
    _offline(monkeypatch)
    seed = _seeker()
    stated = _company("Stated BV", domain="stated.example")
    _vacancy(stated, application_channel="email", application_target="jobs@stated.example")
    known = _company("Known BV", domain="known.example")
    _vacancy(known)
    contacts_repo.upsert_contact(
        {"company_id": known, "email": "hr@known.example",
         "email_source_method": patterns.METHOD_WEBSITE, "collected_at": utcnow()}
    )
    report = asyncio.run(
        pipeline.ensure_apply_contacts(
            seed["seeker_id"], limit=50, concurrency=1, crawl_site=False, derive_domains=False
        )
    )
    payload = report.as_dict()
    assert payload["companies_reachable"] == 2
    assert payload["by_method"][patterns.METHOD_VACANCY] == 1
    assert payload["by_method"][patterns.METHOD_WEBSITE] == 1
    assert payload["corpus"]["with_contact"] == 2


def test_a_resolved_company_is_not_walked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-305: the second pass is cheap because the first one recorded itself."""
    _offline(monkeypatch)
    seed = _seeker()
    company_id = _company("Nowhere BV", domain=None)
    _vacancy(company_id)
    first = asyncio.run(
        pipeline.ensure_apply_contacts(
            seed["seeker_id"], limit=10, concurrency=1, derive_domains=False
        )
    )
    assert first.companies_visited == 1
    second = asyncio.run(
        pipeline.ensure_apply_contacts(
            seed["seeker_id"], limit=10, concurrency=1, derive_domains=False
        )
    )
    assert second.companies_visited == 0


# ---------------------------------------------------------------------------
# FR-324 / NFR-502: the browser's selection and its one-round-trip list
# ---------------------------------------------------------------------------


def test_a_selection_survives_the_ranked_list_clearing_the_flag() -> None:
    seed = _seeker()
    company_id = _company("Acme Data BV")
    opportunity_id = _opportunity(seed, company_id)

    repo.select_opportunity(seed["seeker_id"], opportunity_id)
    assert query_one("SELECT selected FROM opportunity WHERE id = ?", (opportunity_id,))[
        "selected"
    ] == 1

    # A re-scoring pass clears the ranked list's own flag.
    from dreamjob.db.connection import execute

    execute("UPDATE opportunity SET selected = 0 WHERE id = ?", (opportunity_id,))
    rows = repo.list_jobs(seed["seeker_id"])
    assert [row["opportunity_id"] for row in rows] == [opportunity_id]
    assert rows[0]["selection_status"] == "selected"

    repo.deselect_opportunity(seed["seeker_id"], opportunity_id)
    assert repo.list_jobs(seed["seeker_id"]) == []


def test_the_list_shows_unreachable_as_a_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    _offline(monkeypatch)
    seed = _seeker()
    company_id = _company("Nowhere BV")
    opportunity_id = _opportunity(seed, company_id, selected=1)
    repo.record_resolution(
        company_id, {"status": "unreachable", "reason": "no candidate domain could be confirmed"}
    )
    rows = repo.list_jobs(seed["seeker_id"])
    assert rows[0]["reachability"] == "unreachable"
    assert rows[0]["has_contact"] is False
    assert repo.count_jobs(seed["seeker_id"], filters=["unreachable"]) == 1
    assert repo.facet_counts(seed["seeker_id"])["unreachable"] == 1
    assert opportunity_id  # the row under test


def test_the_list_is_one_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """NFR-502: opportunity, company, contact, package and dispatch in one query."""
    seed = _seeker()
    statements: list[str] = []
    for index in range(5):
        company_id = _company(f"Acme {index} BV")
        opportunity_id = _opportunity(seed, company_id, selected=1)
        contact_id, _ = contacts_repo.upsert_contact(
            {"company_id": company_id, "email": f"jobs@acme{index}.example",
             "email_source_method": patterns.METHOD_WEBSITE, "collected_at": utcnow()}
        )
        package_id = insert_row(
            "application_package",
            {"job_seeker_id": seed["seeker_id"], "opportunity_id": opportunity_id,
             "contact_id": contact_id, "status": "draft", "created_at": utcnow(),
             "updated_at": utcnow()},
        )
        insert_row(
            "dispatch",
            {"job_seeker_id": seed["seeker_id"], "application_package_id": package_id,
             "backend": "resend", "recipient_email": f"jobs@acme{index}.example",
             "sent_at": utcnow(), "created_at": utcnow()},
        )

    from dreamjob.db.connection import get_connection

    conn = get_connection()
    conn.set_trace_callback(statements.append)
    try:
        rows = repo.list_jobs(seed["seeker_id"], limit=10)
    finally:
        conn.set_trace_callback(None)

    assert len(rows) == 5
    assert len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) == 1
    assert all(row["contact_email"] for row in rows)
    assert all(row["dispatched_at"] for row in rows)
    assert monkeypatch  # fixture kept for symmetry with the other list tests


def test_coverage_counts_a_published_role_mailbox_apart_from_a_guessed_one() -> None:
    """A coverage figure that merges the two overstates what is known (FR-303)."""
    published = _company("Published BV")
    _vacancy(published)
    _vacancy(published)
    contacts_repo.upsert_contact(
        {"company_id": published, "email": "info@published.example",
         "email_source_method": patterns.METHOD_WEBSITE, "is_generic_mailbox": 1,
         "email_validation": "risky", "collected_at": utcnow()}
    )
    guessed = _company("Guessed BV")
    _vacancy(guessed)
    contacts_repo.upsert_contact(
        {"company_id": guessed, "email": "jobs@guessed.example",
         "email_source_method": patterns.METHOD_PATTERN, "is_generic_mailbox": 1,
         "email_validation": "risky", "collected_at": utcnow()}
    )

    coverage = repo.coverage_by_method()
    assert coverage["vacancies_with_contact"] == 3
    # ``info@`` printed on a contact page is published evidence, role or not.
    assert coverage["by_method"]["website"]["vacancies"] == 2
    assert coverage["by_method"]["conventional_mailbox"]["vacancies"] == 1
    assert coverage["by_method"]["website"]["by_validation"] == {"risky": 2}


def test_an_objection_removes_a_vacancy_from_the_coverage_figure() -> None:
    """NFR-302: the figure is counted off ``usable_contact``, so it moves."""
    company_id = _company("Acme Data BV")
    _vacancy(company_id)
    contacts_repo.upsert_contact(
        {"company_id": company_id, "email": "jobs@acme-data.example",
         "email_source_method": patterns.METHOD_WEBSITE, "collected_at": utcnow()}
    )
    assert repo.coverage_by_method()["vacancies_with_contact"] == 1
    contacts_repo.record_objection("jobs@acme-data.example", reason="unsubscribe")
    assert repo.coverage_by_method()["vacancies_with_contact"] == 0


def test_selecting_a_page_and_moving_it_through_the_workflow() -> None:
    seed = _seeker()
    ids = [_opportunity(seed, _company(f"Acme {i} BV")) for i in range(3)]
    assert repo.select_many(seed["seeker_id"], ids) == 3
    assert repo.selection_counts(seed["seeker_id"]) == {"selected": 3}

    repo.set_selection_status(seed["seeker_id"], ids[0], "ready")
    assert repo.selection_counts(seed["seeker_id"]) == {"selected": 2, "ready": 1}

    # "skipped" is an explicit no, and clears the ranked list's own flag too.
    repo.select_opportunity(seed["seeker_id"], ids[1], status="skipped")
    assert query_one("SELECT selected FROM opportunity WHERE id = ?", (ids[1],))["selected"] == 0
    # Order is the ranked list's, which these rows do not have; compare as a set.
    assert {row["opportunity_id"] for row in repo.list_jobs(seed["seeker_id"])} == {
        ids[0], ids[2]
    }


# ---------------------------------------------------------------------------
# NFR-502: the Contacts bar moves while the pass works, not once at the end
# ---------------------------------------------------------------------------


def test_the_pass_reports_each_company_as_it_is_visited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start tick names the work list and every company moves the bar on."""
    _offline(monkeypatch)
    seed = _seeker()
    for index in range(4):
        company_id = _company(f"Acme {index} BV", domain=f"acme{index}.example")
        _vacancy(
            company_id,
            application_channel="email",
            application_target=f"jobs@acme{index}.example",
        )

    events: list[dict[str, Any]] = []
    report = asyncio.run(
        pipeline.ensure_apply_contacts(
            seed["seeker_id"],
            limit=50,
            concurrency=1,
            crawl_site=False,
            derive_domains=False,
            on_progress=events.append,
        )
    )

    start = next(event for event in events if event["phase"] == "start")
    assert start["done"] == 0
    assert start["total"] == report.companies_visited == 4
    assert start["report"]["companies_visited"] == 0

    company_events = [event for event in events if event["phase"] == "company"]
    assert [event["done"] for event in company_events] == [1, 2, 3, 4]
    assert company_events[-1]["total"] == 4
    assert len(company_events) == report.companies_visited
    assert company_events[-1]["report"]["companies_reachable"] == report.companies_reachable


def test_the_pass_reports_a_terminal_done_when_there_is_nothing_to_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass with no shortfall still ends 1 / 1 rather than hanging at 0 / 1."""
    _offline(monkeypatch)
    seed = _seeker()
    company_id = _company("Acme Data BV", domain="acme-data.example")
    _vacancy(
        company_id,
        application_channel="email",
        application_target="jobs@acme-data.example",
    )
    asyncio.run(
        pipeline.ensure_apply_contacts(
            seed["seeker_id"], limit=1, concurrency=1, crawl_site=False,
            derive_domains=False,
        )
    )

    events: list[dict[str, Any]] = []
    report = asyncio.run(
        pipeline.ensure_apply_contacts(
            seed["seeker_id"], limit=1, concurrency=1, crawl_site=False,
            derive_domains=False, on_progress=events.append,
        )
    )
    assert report.shortfall == 0
    # The "preparing" tick now precedes the corpus pre-scan, even when it turns
    # out there is nothing to do; the terminal "done" is still last.
    assert [event["phase"] for event in events] == ["preparing", "done"]
    assert events[0]["total"] is None
    assert events[-1]["done"] == 1 and events[-1]["total"] == 1


def test_the_discovery_worker_drives_the_bar_from_the_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker turns each callback tick into ``ctx.progress`` and a checkpoint."""
    _offline(monkeypatch)
    seed = _seeker()
    for index in range(3):
        company_id = _company(f"Acme {index} BV", domain=f"acme{index}.example")
        _vacancy(
            company_id,
            application_channel="email",
            application_target=f"jobs@acme{index}.example",
        )

    class _Ctx:
        job_seeker_id = seed["seeker_id"]
        campaign_id = seed["campaign_id"]
        checkpoint = {
            "options": {
                "limit": 50, "crawl_site": False, "derive_domains": False, "concurrency": 1
            }
        }

        def __init__(self) -> None:
            self.progress_calls: list[tuple[int, int | None]] = []
            self.checkpoints: list[dict[str, Any]] = []

        def save_checkpoint(self, **kwargs: Any) -> None:
            self.checkpoints.append(kwargs)

        def progress(self, done: int, total: int | None = None) -> None:
            self.progress_calls.append((done, total))

    ctx = _Ctx()

    async def run() -> None:
        async for _ in pipeline.contacts_discovery_worker(ctx):  # type: ignore[arg-type]
            pass

    asyncio.run(run())
    assert (0, 3) in ctx.progress_calls
    assert (3, 3) in ctx.progress_calls
    assert ctx.progress_calls[-1] == (1, 1)


# ---------------------------------------------------------------------------
# NFR-401: the sweep resumes across restarts
# ---------------------------------------------------------------------------


def _reachable_company(index: int) -> str:
    company_id = _company(f"Acme {index} BV", domain=f"acme{index}.example")
    _vacancy(
        company_id,
        application_channel="email",
        application_target=f"jobs@acme{index}.example",
    )
    return company_id


def test_skip_company_ids_excludes_them_from_the_shortlist_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _offline(monkeypatch)
    seed = _seeker()
    companies = [_reachable_company(index) for index in range(4)]

    events: list[dict[str, Any]] = []
    report = asyncio.run(
        pipeline.ensure_apply_contacts(
            seed["seeker_id"], limit=4, concurrency=1, crawl_site=False,
            derive_domains=False, skip_company_ids={companies[0], companies[1]},
            on_progress=events.append,
        )
    )
    assert report.companies_visited == 2
    assert {outcome.company_id for outcome in report.outcomes} == set(companies[2:])
    assert [event for event in events if event["phase"] == "company"]

    # Everything selected is skipped: the pass ends terminally instead of erroring.
    events.clear()
    empty = asyncio.run(
        pipeline.ensure_apply_contacts(
            seed["seeker_id"], limit=4, concurrency=1, crawl_site=False,
            derive_domains=False, skip_company_ids=set(companies),
            on_progress=events.append,
        )
    )
    assert empty.companies_visited == 0
    assert [event["phase"] for event in events] == ["preparing", "done"]
    assert events[-1]["done"] == 1 and events[-1]["total"] == 1


def test_skip_company_ids_excludes_them_from_the_all_companies_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _offline(monkeypatch)
    seed = _seeker()
    companies = [_company(f"Standalone {index} BV") for index in range(3)]

    report = asyncio.run(
        pipeline.ensure_apply_contacts(
            seed["seeker_id"], limit=3, scope="all", concurrency=1, crawl_site=False,
            derive_domains=False, skip_company_ids={companies[0]},
        )
    )
    assert report.companies_visited == 2
    assert {outcome.company_id for outcome in report.outcomes} == set(companies[1:])


def test_the_company_progress_payload_carries_the_company_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker needs the id to persist which companies a run already visited."""
    _offline(monkeypatch)
    seed = _seeker()
    companies = [_reachable_company(index) for index in range(3)]

    events: list[dict[str, Any]] = []
    asyncio.run(
        pipeline.ensure_apply_contacts(
            seed["seeker_id"], limit=50, concurrency=1, crawl_site=False,
            derive_domains=False, on_progress=events.append,
        )
    )
    company_events = [event for event in events if event["phase"] == "company"]
    assert len(company_events) == len(companies)
    assert {event["company_id"] for event in company_events} == set(companies)


class _WorkerCtx:
    """The slice of JobContext the discovery worker uses, with the checkpoint read."""

    def __init__(self, seed: dict[str, str], checkpoint: dict[str, Any]) -> None:
        self.job_seeker_id = seed["seeker_id"]
        self.campaign_id = seed["campaign_id"]
        self.checkpoint = checkpoint
        self.checkpoints: list[dict[str, Any]] = []
        self.progress_calls: list[tuple[int, int | None]] = []

    def save_checkpoint(self, **kwargs: Any) -> None:
        self.checkpoints.append(kwargs)

    def progress(self, done: int, total: int | None = None) -> None:
        self.progress_calls.append((done, total))


def _run_discovery_worker(ctx: _WorkerCtx) -> None:
    async def run() -> None:
        async for _ in pipeline.contacts_discovery_worker(ctx):  # type: ignore[arg-type]
            pass

    asyncio.run(run())


def _worker_checkpoint(visited: list[str] | None = None) -> dict[str, Any]:
    checkpoint: dict[str, Any] = {
        "options": {"limit": 10, "crawl_site": False, "derive_domains": False, "concurrency": 1},
    }
    if visited is not None:
        checkpoint["visited_company_ids"] = visited
    return checkpoint


def test_the_discovery_worker_resumes_from_the_visited_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NFR-401: a restarted worker skips what an earlier run already visited."""
    _offline(monkeypatch)
    seed = _seeker()
    visited_company = _reachable_company(0)
    fresh_company = _reachable_company(1)

    ctx = _WorkerCtx(seed, _worker_checkpoint(visited=[visited_company]))
    _run_discovery_worker(ctx)

    assert repo.get_resolution(visited_company) is None
    assert repo.get_resolution(fresh_company)["status"] == "reachable"
    persisted = [
        values["visited_company_ids"]
        for values in ctx.checkpoints
        if "visited_company_ids" in values
    ]
    assert persisted and persisted[-1] == [visited_company, fresh_company]
    # The bar counts from the visited base: one remaining company, one behind it.
    assert (1, 2) in ctx.progress_calls
    assert (2, 2) in ctx.progress_calls
    assert ctx.progress_calls[-1] == (1, 1)


def test_the_worker_progress_counts_from_the_visited_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed run keeps moving forward instead of restarting the UI at zero."""
    _offline(monkeypatch)
    seed = _seeker()
    already_visited = [_company(f"Earlier {index} BV") for index in range(4)]
    _reachable_company(4)

    ctx = _WorkerCtx(seed, _worker_checkpoint(visited=already_visited))
    _run_discovery_worker(ctx)

    assert (4, 5) in ctx.progress_calls
    assert (5, 5) in ctx.progress_calls
