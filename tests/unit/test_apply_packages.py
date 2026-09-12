"""The Apply Browser's package generation (FR-321..FR-324, NFR-104, NFR-502).

Everything here runs against a throw-away SQLite file with no network and no
model: the fixture clears the provider key, so every generator takes its
deterministic path and the tests assert on what the Apply Browser produces when
the model is unavailable - which is also the state a first-time user is in.

The last group is the one that matters most.  No e-mail may leave the machine,
so the dry-run guard is tested from both sides: with neither switch thrown, with
only one thrown, and with the environment variable set but the administration
setting off.  A send is possible only when both are on.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from dreamjob.config import get_settings

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR", "DREAMJOB_DB_PATH", "DREAMJOB_MASTER_KEY", "DREAMJOB_ENV",
    "DEEPSEEK_API_KEY", "DREAMJOB_LOCAL_LLM_BASE_URL", "DREAMJOB_APPLY_ALLOW_SEND",
    "DREAMJOB_MAIL_FROM",
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_ENV"] = "development"
    os.environ["DEEPSEEK_API_KEY"] = ""
    os.environ["DREAMJOB_LOCAL_LLM_BASE_URL"] = ""
    os.environ.pop("DREAMJOB_APPLY_ALLOW_SEND", None)
    os.environ["DREAMJOB_MAIL_FROM"] = "stephane@stepvda.com"
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
# One job seeker, one company, one vacancy, one speculative opening
# ---------------------------------------------------------------------------

SECTIONS = {
    "contact": {
        "name": "Stephane van der Aa", "headline": "Data & AI",
        "email": "stephane@stepvda.com", "phone": "+32 470 11 22 33", "location": "Gent",
        "linkedin_url": "linkedin.com/in/svda", "websites": [],
    },
    "summary": "Twintig jaar ervaring in data en analytics.",
    "top_skills": ["Python", "SQL"],
    "languages": [{"language": "Nederlands", "level": "moedertaal"}],
    "experience": [
        {"company": "Acme N.V.", "title": "Head of Data", "start": "2019-05", "end": None,
         "current": True, "location": "Gent",
         "description": "Team van 12 opgebouwd.\nKosten met 30% verlaagd."},
        {"company": "Beta BV", "title": "Lead Data Engineer", "start": "2014-01",
         "end": "2019-04", "location": "Antwerpen", "description": "Realtime pijplijn gebouwd."},
    ],
    "education": [
        {"school": "Universiteit Gent", "degree": "MSc", "field": "Informatica",
         "start_year": "2003", "end_year": "2008", "description": None}
    ],
    "certifications": [{"title": "Azure Data Engineer", "start": "2022"}],
}


def seed() -> dict[str, str]:
    from dreamjob.db.connection import insert_row, to_json, utcnow

    now = utcnow()
    seeker = insert_row(
        "job_seeker",
        {"email": "stephane@stepvda.com", "display_name": "Stephane van der Aa",
         "locale": "nl", "created_at": now, "updated_at": now},
    )
    version = insert_row(
        "profile_version",
        {"job_seeker_id": seeker, "version": 3, "sections": to_json(SECTIONS),
         "source_note": "merged", "created_at": now},
    )
    for skill in ("Python", "SQL", "Databricks", "Teamleiding"):
        insert_row(
            "profile_skill",
            {"job_seeker_id": seeker, "profile_version_id": version, "raw_label": skill,
             "normalised_label": skill, "proficiency": 4},
        )
    directives = insert_row(
        "directive_set", {"job_seeker_id": seeker, "name": "Standaard", "created_at": now}
    )
    campaign = insert_row(
        "campaign",
        {"job_seeker_id": seeker, "directive_set_id": directives, "profile_version_id": version,
         "name": "Najaar", "created_at": now},
    )
    company = insert_row(
        "company",
        {"normalised_name": "zenith analytics", "name": "Zenith Analytics NV", "country": "BE",
         "domain": "zenith.example",
         "business_summary": "Zenith bouwt analytics software voor de zorgsector.",
         "size_fte": 180, "size_band": "100-250", "stage": "scaleup", "trajectory": "growing",
         "values_culture": to_json([{"name": "Ownership"}]),
         "collected_at": now, "refreshed_at": now, "source": "website"},
    )
    for index, (revenue, ebitda, headcount) in enumerate(
        [(8e6, 6e5, 80), (9.5e6, 8e5, 95), (11e6, 9e5, 120), (13.2e6, 1.1e6, 150),
         (15.8e6, 1.4e6, 180)]
    ):
        insert_row(
            "financial_year",
            {"company_id": company, "fiscal_year": 2020 + index, "currency": "EUR",
             "revenue": revenue, "ebitda": ebitda, "headcount_fte": headcount,
             "source": "NBB", "collected_at": now},
        )
    insert_row(
        "hiring_signal",
        {"company_id": company, "signal_type": "funding", "description": "Serie B",
         "occurred_at": "2026-07-02", "collected_at": now},
    )
    contact = insert_row(
        "contact",
        {"company_id": company, "full_name": "Els Peeters", "role_title": "CTO",
         "email": "els.peeters@zenith.example", "email_validation": "valid",
         "collected_at": now, "confidence": 0.9},
    )
    vacancy = insert_row(
        "vacancy",
        {"company_id": company, "title": "Head of Data",
         "description": "Wij zoeken een Head of Data.",
         "required_skills": to_json(["Python", "Databricks", "Teamleiding"]),
         "location": "Gent", "language": "nl", "collected_at": now},
    )
    opportunity = insert_row(
        "opportunity",
        {"job_seeker_id": seeker, "campaign_id": campaign, "company_id": company,
         "vacancy_id": vacancy, "kind": "vacancy", "title": "Head of Data", "language": "nl",
         "selected": 1, "description": "Wij zoeken een Head of Data.",
         "required_skills": to_json(["Python", "Databricks", "Teamleiding"]),
         "created_at": now, "updated_at": now},
    )
    speculative = insert_row(
        "opportunity",
        {"job_seeker_id": seeker, "campaign_id": campaign, "company_id": company,
         "kind": "speculative", "title": "Director of Data Platform", "language": "nl",
         "selected": 1, "speculative_rationale": "Zenith groeit snel.", "plausibility": 0.62,
         "created_at": now, "updated_at": now},
    )
    return {
        "seeker": seeker, "campaign": campaign, "company": company, "contact": contact,
        "opportunity": opportunity, "speculative": speculative,
    }


# ---------------------------------------------------------------------------
# FR-321: the four artefacts
# ---------------------------------------------------------------------------


def test_generate_package_produces_the_four_artefacts() -> None:
    """FR-321: CV (DOCX and PDF), briefing PDF, motivation PDF and the e-mail."""
    from dreamjob.pipeline import apply_packages

    ids = seed()
    result = apply_packages.generate_package(ids["seeker"], ids["opportunity"])

    assert result.package_id and not result.reused
    for kind in ("cv_docx", "cv_pdf", "briefing", "motivation"):
        path = result.documents[kind]
        assert path and Path(path).is_file(), f"{kind} was not produced"
    assert Path(result.documents["cv_pdf"]).stat().st_size > 1500
    assert result.email_subject and result.email_subject.startswith("Sollicitatie")
    assert result.recipient_email == "els.peeters@zenith.example"
    assert result.language == "nl"
    # FR-322: the check ran, and its verdict is on the package.
    assert result.consistency_status == "pass"
    assert result.ready and not result.blockers
    # NFR-104: the cost of the package is reported, even when it is nothing.
    assert result.spend["cost_eur"] == 0.0
    assert result.degradation, "a run without a model must say so"


def test_apply_email_refers_to_the_cv() -> None:
    """The e-mail is a brief motivation letter that points at the attachment."""
    from dreamjob.db.repositories import applications as repo
    from dreamjob.documents import intro_email
    from dreamjob.pipeline import apply_packages

    ids = seed()
    result = apply_packages.generate_package(ids["seeker"], ids["opportunity"])
    package = repo.get_package(result.package_id, ids["seeker"])
    body = package["email_body"]

    assert intro_email.ATTACHMENT_LINE["nl"] in body, "the e-mail must point at the CV"
    # NFR-302: the objection sentence is present exactly once, in the language
    # of the opportunity, and is written by the system rather than the model.
    assert body.count(intro_email.OBJECTION_SENTENCE["nl"]) == 1
    assert "Els Peeters" in body and "Stephane van der Aa" in body
    assert len(body.split()) < 220, "a covering note, not a second CV"
    stored = package["generation_notes"]["email"]
    assert stored["prompt_template"] == "apply_email"
    assert stored["prompt_version"]


def test_speculative_apply_email_asserts_no_vacancy() -> None:
    """FR-263 / FR-323: a spontaneous application never claims a vacancy exists."""
    from dreamjob.db.repositories import applications as repo
    from dreamjob.documents.intro_email import vacancy_assertions
    from dreamjob.pipeline import apply_packages

    ids = seed()
    result = apply_packages.generate_package(ids["seeker"], ids["speculative"])
    package = repo.get_package(result.package_id, ids["seeker"])

    assert package["email_subject"].startswith("Spontane sollicitatie")
    assert vacancy_assertions(package["email_body"], "nl") == []
    assert package["generation_notes"]["email"]["speculative"] is True
    assert result.consistency_status == "pass"


def test_regenerating_is_opt_in() -> None:
    """NFR-104: a complete package is reused, not paid for twice."""
    from dreamjob.pipeline import apply_packages

    ids = seed()
    first = apply_packages.generate_package(ids["seeker"], ids["opportunity"])
    stamp = Path(first.documents["cv_pdf"]).stat().st_mtime_ns

    again = apply_packages.generate_package(ids["seeker"], ids["opportunity"])
    assert again.reused and again.status == "reused"
    assert again.package_id == first.package_id
    assert Path(again.documents["cv_pdf"]).stat().st_mtime_ns == stamp

    regenerated = apply_packages.generate_package(
        ids["seeker"], ids["opportunity"], regenerate=True
    )
    assert not regenerated.reused
    assert regenerated.package_id == first.package_id


def test_a_failed_consistency_check_blocks_and_is_shown() -> None:
    """FR-322: the failure blocks approval and is reported, not hidden."""
    from dreamjob.db.repositories import applications as repo
    from dreamjob.pipeline import apply_packages

    ids = seed()
    result = apply_packages.generate_package(ids["seeker"], ids["opportunity"])
    repo.update_package(
        result.package_id,
        ids["seeker"],
        {"consistency_status": "fail",
         "consistency_report": {"findings": [{"severity": "high", "claim": "PhD"}]}},
    )
    package = repo.get_package(result.package_id, ids["seeker"])
    blocked = apply_packages._result(package, reused=False)

    assert not blocked.ready and blocked.status == "blocked"
    kinds = {b["kind"] for b in blocked.blockers}
    assert "consistency" in kinds


# ---------------------------------------------------------------------------
# NFR-302: an objection blocks generation, approval and send
# ---------------------------------------------------------------------------


def test_generating_for_an_objected_contact_is_refused() -> None:
    """An objected address is never chosen as a package's recipient."""
    from dreamjob.db.repositories import applications as repo
    from dreamjob.db.repositories import contacts as contacts_repo
    from dreamjob.documents.package import GenerationError
    from dreamjob.pipeline import apply_packages

    ids = seed()
    contacts_repo.record_objection(
        "els.peeters@zenith.example", reason="asked not to be contacted"
    )

    with pytest.raises(GenerationError, match="NFR-302"):
        apply_packages.generate_package(
            ids["seeker"],
            ids["opportunity"],
            options=apply_packages.Options(contact_id=ids["contact"]),
        )
    assert repo.latest_for_opportunity(ids["seeker"], ids["opportunity"]) is None


def test_regenerating_after_an_objection_is_refused() -> None:
    """An objection that arrives later stops the package being rebuilt."""
    from dreamjob.db.repositories import contacts as contacts_repo
    from dreamjob.documents.package import GenerationError
    from dreamjob.pipeline import apply_packages

    ids = seed()
    first = apply_packages.generate_package(ids["seeker"], ids["opportunity"])
    contacts_repo.record_objection("els.peeters@zenith.example", source="reply")

    with pytest.raises(GenerationError, match="NFR-302"):
        apply_packages.generate_package(ids["seeker"], ids["opportunity"], regenerate=True)

    # Reusing the untouched package is allowed, but it now shows the block.
    reused = apply_packages.generate_package(ids["seeker"], ids["opportunity"])
    assert reused.package_id == first.package_id and reused.reused
    assert not reused.ready
    assert {b["kind"] for b in reused.blockers} >= {"objection"}


def test_objecting_after_the_package_exists_blocks_approval_and_send() -> None:
    """The gates re-read the block list, so a later objection still stops it."""
    from dreamjob.db.repositories import applications as repo
    from dreamjob.db.repositories import contacts as contacts_repo
    from dreamjob.documents import package as package_module
    from dreamjob.pipeline import apply_packages

    ids = seed()
    result = apply_packages.generate_package(ids["seeker"], ids["opportunity"])
    # Approve before the objection, so it is NFR-302 that refuses the send and
    # not the FR-324 approval gate.
    assert repo.approve_packages(
        ids["seeker"], [result.package_id], "stephane@stepvda.com"
    ) == [result.package_id]

    contacts_repo.record_objection("els.peeters@zenith.example", source="reply")

    approval = package_module.approve(
        ids["seeker"], [result.package_id], actor="stephane@stepvda.com"
    )
    assert approval["approved"] == []
    assert {"objection"} <= {b["kind"] for b in approval["refused"][0]["blockers"]}

    sent = apply_packages.send_package(
        ids["seeker"], result.package_id, actor="stephane@stepvda.com"
    )
    assert sent["sent"] is False
    assert "NFR-302" in sent["refused"]
    assert "message" not in sent, "nothing may be assembled for an objected recipient"


def test_a_non_objected_contact_still_generates_normally() -> None:
    from dreamjob.db.repositories import contacts as contacts_repo
    from dreamjob.pipeline import apply_packages

    ids = seed()
    contacts_repo.record_objection("someone.else@zenith.example", source="manual")

    result = apply_packages.generate_package(ids["seeker"], ids["opportunity"])
    assert result.ready
    assert result.recipient_email == "els.peeters@zenith.example"


def test_an_objected_address_is_not_offered_at_recipient_selection() -> None:
    """NFR-302: the block list removes the contact before a package picks one.

    The row flag is cleared after the objection to prove the shared predicate
    is what excludes the address, not the flag alone.
    """
    from dreamjob.db.connection import execute
    from dreamjob.db.repositories import applications as repo
    from dreamjob.db.repositories import contacts as contacts_repo

    ids = seed()
    contacts_repo.record_objection("els.peeters@zenith.example", source="reply")
    execute("UPDATE contact SET objected = 0 WHERE id = ?", (ids["contact"],))

    inputs = repo.generation_inputs(ids["seeker"], ids["opportunity"])
    assert "els.peeters@zenith.example" not in {c.get("email") for c in inputs["contacts"]}


# ---------------------------------------------------------------------------
# FR-321: the briefing and the motivation document are never attached
# ---------------------------------------------------------------------------


def test_only_the_cv_is_ever_attached() -> None:
    from dreamjob.db.repositories import dispatch as dispatch_repo
    from dreamjob.mail.composer import AttachmentRefused
    from dreamjob.pipeline import apply_packages

    ids = seed()
    result = apply_packages.generate_package(ids["seeker"], ids["opportunity"])
    package = dispatch_repo.package_for_dispatch(result.package_id, ids["seeker"])

    names = [a.filename for a in apply_packages.email_attachments(package)]
    assert len(names) == 1 and names[0].lower().endswith(".pdf")
    assert "briefing" not in names[0].lower()
    assert "motivation" not in names[0].lower()
    assert result.never_sent == ["briefing_pdf_path", "motivation_pdf_path"]

    # Even a package whose CV column has been pointed at a seeker-only document
    # is refused rather than sent.
    poisoned = {**package, "cv_pdf_path": package["briefing_pdf_path"], "cv_docx_path": None}
    with pytest.raises(AttachmentRefused):
        apply_packages.email_attachments(poisoned)


# ---------------------------------------------------------------------------
# The absolute rule: no e-mail leaves the machine
# ---------------------------------------------------------------------------


def _approved(ids: dict[str, str]) -> str:
    from dreamjob.db.repositories import applications as repo
    from dreamjob.pipeline import apply_packages

    result = apply_packages.generate_package(ids["seeker"], ids["opportunity"])
    approved = repo.approve_packages(ids["seeker"], [result.package_id], "stephane@stepvda.com")
    assert approved == [result.package_id]
    return result.package_id


def test_send_is_a_dry_run_and_writes_the_message_to_disk() -> None:
    from dreamjob.pipeline import apply_packages

    ids = seed()
    package_id = _approved(ids)
    outcome = apply_packages.send_package(
        ids["seeker"], package_id, actor="stephane@stepvda.com"
    )

    assert outcome["sent"] is False and outcome["dry_run"] is True
    message = outcome["message"]
    assert message["rendered"] is True
    eml = Path(message["path"])
    assert eml.is_file() and eml.name == "email.eml"
    raw = eml.read_text(encoding="utf-8", errors="replace")
    assert "els.peeters@zenith.example" in raw
    assert len(message["attachments"]) == 1
    assert message["never_attached"] == ["briefing_pdf_path", "motivation_pdf_path"]


def test_the_guard_needs_both_switches() -> None:
    """A single switch - the one an interface could reach - is not enough."""
    from dreamjob.db.repositories import admin as admin_repo
    from dreamjob.pipeline import apply_packages

    assert apply_packages.send_guard().dry_run is True

    admin_repo.set_setting(apply_packages.SEND_ENABLED_SETTING, True)
    guard = apply_packages.send_guard()
    assert guard.dry_run is True and guard.setting_allows and not guard.env_allows

    admin_repo.set_setting(apply_packages.SEND_ENABLED_SETTING, False)
    os.environ[apply_packages.SEND_ENABLED_ENV] = "1"
    guard = apply_packages.send_guard()
    assert guard.dry_run is True and guard.env_allows and not guard.setting_allows

    with pytest.raises(apply_packages.SendBlocked):
        apply_packages.assert_send_allowed()

    admin_repo.set_setting(apply_packages.SEND_ENABLED_SETTING, True)
    assert apply_packages.send_guard().dry_run is False
    apply_packages.assert_send_allowed()  # the only way through, by hand, twice


def test_dry_run_send_never_reaches_the_dispatcher(monkeypatch: pytest.MonkeyPatch) -> None:
    from dreamjob.mail import dispatcher
    from dreamjob.pipeline import apply_packages

    ids = seed()
    package_id = _approved(ids)

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the dispatcher must not be reached in a dry run")

    monkeypatch.setattr(dispatcher, "send_package", explode)
    outcome = apply_packages.send_all(
        ids["seeker"], [package_id], actor="stephane@stepvda.com"
    )
    assert outcome["dry_run"] is True and outcome["sent"] == 0
    assert outcome["prepared"] == 1


def test_an_unapproved_package_is_refused() -> None:
    """FR-324: nothing is sent that the job seeker has not approved."""
    from dreamjob.pipeline import apply_packages

    ids = seed()
    result = apply_packages.generate_package(ids["seeker"], ids["opportunity"])
    outcome = apply_packages.send_package(
        ids["seeker"], result.package_id, actor="stephane@stepvda.com"
    )
    assert outcome["sent"] is False
    assert "FR-324" in outcome["refused"]


# ---------------------------------------------------------------------------
# NFR-502: bulk, with progress
# ---------------------------------------------------------------------------


async def test_generate_many_reports_progress_and_is_resumable() -> None:
    from dreamjob.pipeline import apply_packages

    ids = seed()
    started = await apply_packages.generate_many(
        ids["seeker"],
        [ids["opportunity"], ids["speculative"]],
        concurrency=2,
        campaign_id=ids["campaign"],
        actor="stephane@stepvda.com",
    )
    job_id = started["job_id"]
    for _ in range(600):
        if not apply_packages.progress(job_id)["running"]:
            break
        await asyncio.sleep(0.1)

    state = apply_packages.progress(job_id)
    assert state["status"] == "done", state["last_error"]
    assert state["done"] == state["total"] == 2
    assert state["percent"] == 100.0
    assert state["errors"] == 0
    assert {p["status"] for p in state["packages"]} == {"ready"}

    # NFR-401: the checkpoint carries what a restart would need to resume.
    from dreamjob.db.connection import from_json, query_one

    row = query_one("SELECT checkpoint FROM job_run WHERE id = ?", (job_id,))
    checkpoint = from_json(row["checkpoint"], {})
    assert sorted(checkpoint["done"]) == sorted([ids["opportunity"], ids["speculative"]])
    assert checkpoint["opportunity_ids"]

    # A second pass over the same opportunities reuses the packages (NFR-104).
    again = await apply_packages.generate_many(
        ids["seeker"], [ids["opportunity"]], concurrency=1
    )
    for _ in range(600):
        if not apply_packages.progress(again["job_id"])["running"]:
            break
        await asyncio.sleep(0.1)
    assert apply_packages.progress(again["job_id"])["counts"] == {"reused": 1}


# ---------------------------------------------------------------------------
# The model path: the prompt, the scaffolding, and a budget spent on reasoning
# ---------------------------------------------------------------------------


class _StubBudget:
    def should_degrade(self) -> bool:
        return False


class _StubLLM:
    """Duck-typed ``LLMClient`` - the generators only call ``complete_json``."""

    def __init__(self, payload: dict, *, truncate: int = 0):
        self.payload = payload
        self.truncate = truncate
        self.calls: list[dict] = []
        self.budget = _StubBudget()

    def complete_json(self, task: str, system: str, user: str, **kw: object) -> dict:
        from dreamjob.llm.client import TruncatedResponse

        self.calls.append({"task": task, "system": system, "user": user, **kw})
        if len(self.calls) <= self.truncate:
            raise TruncatedResponse("spent its whole budget on reasoning and returned no answer")
        return dict(self.payload)


def _draft(llm: _StubLLM, key: str = "opportunity"):
    from dreamjob.db.repositories import applications as repo
    from dreamjob.documents.cv_generator import build_base_document
    from dreamjob.pipeline import apply_packages

    ids = seed()
    inputs = repo.generation_inputs(ids["seeker"], ids[key])
    document = build_base_document(inputs, language="nl")
    return apply_packages.compose_apply_email(inputs, document, language="nl", llm=llm)


def test_the_model_writes_the_body_and_the_system_writes_the_rest() -> None:
    from dreamjob.documents import intro_email
    from dreamjob.pipeline import apply_packages

    llm = _StubLLM({"subject": "Head of Data bij Zenith", "body": "Uw groei in de zorgsector."})
    draft = _draft(llm)

    assert draft.used_llm and draft.subject == "Head of Data bij Zenith"
    assert "Uw groei in de zorgsector." in draft.body
    # Greeting, attachment line, sign-off and the NFR-302 sentence are the
    # system's, so a regeneration cannot drop them.
    assert draft.body.startswith("Beste Els Peeters,")
    assert intro_email.ATTACHMENT_LINE["nl"] in draft.body
    assert draft.body.count(intro_email.OBJECTION_SENTENCE["nl"]) == 1

    call = llm.calls[0]
    assert call["task"] == "generate.email"
    assert call["prompt_template"] == "apply_email" and call["prompt_version"]
    assert call["max_tokens"] == apply_packages.EMAIL_MAX_TOKENS
    # NFR-205: the opening and the company text are data, not instructions.
    assert set(call["untrusted"]) == {"opening", "company"}
    assert "Zenith" not in call["system"]
    assert "objection" in call["system"].lower()


def test_a_budget_spent_on_reasoning_is_retried_on_the_chat_model() -> None:
    """TruncatedResponse must never surface; the task is re-routed instead."""
    llm = _StubLLM({"subject": "S", "body": "Een korte motivatie."}, truncate=1)
    draft = _draft(llm)

    assert draft.used_llm and "Een korte motivatie." in draft.body
    assert len(llm.calls) == 2
    assert llm.calls[0].get("prefer_strong") is None
    assert llm.calls[1]["prefer_strong"] is False


def test_a_model_that_never_answers_degrades_to_the_assembled_email() -> None:
    llm = _StubLLM({"subject": "S", "body": "B"}, truncate=5)
    draft = _draft(llm)

    assert not draft.used_llm
    assert "Ik solliciteer naar de functie van Head of Data" in draft.body
    assert any("cut off" in note for note in draft.notes)


def test_a_model_that_claims_a_vacancy_is_caught() -> None:
    """FR-263 / FR-323: the model's word is not taken for it."""
    llm = _StubLLM({"subject": "S", "body": "Naar aanleiding van uw vacature schrijf ik u."})
    draft = _draft(llm, "speculative")

    assert draft.speculative and draft.assertions
    assert any("FR-323" in note for note in draft.notes)


async def test_a_batch_can_be_paused_and_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """NFR-502 / FR-185: progress, a pause that holds, and a cancel that stops."""
    import time

    from dreamjob.pipeline import apply_packages

    def slow(job_seeker_id: str, opportunity_id: str, *args: object) -> dict:
        time.sleep(0.05)
        return {"opportunity_id": opportunity_id, "package_id": None, "status": "ready",
                "spend": {"cost_eur": 0.0}}

    monkeypatch.setattr(apply_packages, "_generate_one", slow)
    ids = seed()
    started = await apply_packages.generate_many(
        ids["seeker"], [f"opportunity-{n}" for n in range(30)], concurrency=1
    )
    job_id = started["job_id"]

    await asyncio.sleep(0.2)
    assert apply_packages.pause(job_id) is True
    held = apply_packages.progress(job_id)
    assert held["status"] == "paused"
    await asyncio.sleep(0.4)
    assert apply_packages.progress(job_id)["done"] <= held["done"] + 1

    assert apply_packages.resume(job_id) is True
    await asyncio.sleep(0.1)
    apply_packages.cancel(job_id)
    for _ in range(100):
        if not apply_packages.progress(job_id)["running"]:
            break
        await asyncio.sleep(0.05)

    final = apply_packages.progress(job_id)
    assert final["status"] == "cancelled"
    assert 0 < final["done"] < 30, "a cancelled batch stops where it was"


def test_send_all_refuses_one_package_without_abandoning_the_batch() -> None:
    from dreamjob.pipeline import apply_packages

    ids = seed()
    package_id = _approved(ids)
    outcome = apply_packages.send_all(
        ids["seeker"], ["does-not-exist", package_id], actor="stephane@stepvda.com"
    )

    assert outcome["requested"] == 2 and outcome["sent"] == 0
    assert outcome["prepared"] == 1
    assert len(outcome["refused"]) == 1
    assert outcome["refused"][0]["package_id"] == "does-not-exist"


def test_a_cut_off_generator_degrades_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TruncatedResponse from a generator must never reach the API as a 500."""
    from dreamjob.documents import package as package_module
    from dreamjob.llm.client import TruncatedResponse
    from dreamjob.pipeline import apply_packages

    ids = seed()
    real = package_module.generate
    calls: list[bool] = []

    def flaky(seeker_id: str, opportunity_id: str, options=None, **kw: object):
        calls.append(bool(options and options.use_llm))
        if len(calls) == 1:
            raise TruncatedResponse("spent its whole budget on reasoning")
        return real(seeker_id, opportunity_id, options, **kw)

    monkeypatch.setattr(package_module, "generate", flaky)
    result = apply_packages.generate_package(ids["seeker"], ids["opportunity"])

    assert result.ready and result.documents["cv_pdf"]
    assert any("cut off" in reason for reason in result.degradation)
    assert len(calls) == 2, "the group is retried once"
    assert calls[-1] is False, "and the retry runs without the model"


def test_a_generic_unverified_recipient_is_advisory_not_blocking():
    """FR-304/NFR-305: applying to a shared, unverified mailbox is a choice.

    The apply screen should say so before Send - it is not a blocker, because
    the address is a legitimate last-resort target.
    """
    from dreamjob.api.routers.apply import _recipient_advisories

    out = _recipient_advisories(
        {
            "contact_email": "jobs@acme.com",
            "contact_email_validation": "risky",
            "contact_is_generic": 1,
            "reachability": "reachable",
        }
    )
    kinds = {row["kind"] for row in out}
    assert kinds == {"generic_mailbox", "unverified_email"}

    # A named, verified person raises nothing.
    assert (
        _recipient_advisories(
            {
                "contact_email": "jane@acme.com",
                "contact_email_validation": "valid",
                "contact_is_generic": 0,
                "reachability": "reachable",
            }
        )
        == []
    )
