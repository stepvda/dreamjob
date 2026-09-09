"""The Apply Browser and the dry-run transport guard (FR-321..FR-325, RK-05).

The test that matters in this slice is
:func:`test_no_smtp_or_http_call_is_made_while_the_dry_run_is_on`.  The product
owner asked for the whole pipeline up to a generated e-mail with the tailored
CV attached and explicitly *not* the send, so what has to be proved is not that
a button is hidden but that nothing reaches a socket: ``httpx``, ``smtplib``
and ``socket.create_connection`` are replaced with tripwires that fail the test
if they are touched at all, and the whole dispatch path is then run through
them - twice, once through the Apply Browser and once by calling
``dispatcher.send_package`` directly, which is what a future caller that has
never heard of the guard would do.

Everything else here runs against a throwaway SQLite file with no network and
no model: the packages are inserted directly, so the generators are not
exercised a second time (they have their own suite in ``test_documents.py``).
"""

from __future__ import annotations

import base64
import os
import secrets
from collections.abc import Iterator
from email import message_from_bytes, policy
from pathlib import Path
from typing import Any, ClassVar

import pytest
from dreamjob.config import get_settings
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_SESSION_SECRET",
    "DREAMJOB_ENV",
    "DREAMJOB_MAIL_DRY_RUN",
    "DREAMJOB_MAIL_FROM",
    "DREAMJOB_MAIL_BACKEND",
    "RESEND_API_KEY",
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_SESSION_SECRET"] = secrets.token_urlsafe(32)
    os.environ["DREAMJOB_ENV"] = "development"
    # The default is true; it is set explicitly so a test that turns it off
    # cannot leak that state into the next one.
    os.environ["DREAMJOB_MAIL_DRY_RUN"] = "true"
    os.environ["DREAMJOB_MAIL_BACKEND"] = "resend"
    os.environ["DREAMJOB_MAIL_FROM"] = "seeker@example.test"
    # A key on purpose: an unconfigured backend would refuse before reaching
    # the transport, which would prove nothing about the guard.
    os.environ["RESEND_API_KEY"] = "re_test_key"
    os.environ["GMAIL_CLIENT_ID"] = ""
    os.environ["GMAIL_CLIENT_SECRET"] = ""
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


def _set_dry_run(value: bool) -> None:
    os.environ["DREAMJOB_MAIL_DRY_RUN"] = "true" if value else "false"
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Tripwires: anything that could put bytes on a wire
# ---------------------------------------------------------------------------


class TransportTouched(AssertionError):
    """Raised the moment anything opens a connection.  Failing the test is the point."""


@pytest.fixture
def no_transport(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace every way out of this process with something that fails loudly."""
    touched: list[str] = []

    def tripwire(name: str):
        def boom(*args: Any, **kwargs: Any):
            touched.append(name)
            raise TransportTouched(f"{name} was called while the dry run was on")

        return boom

    import smtplib
    import socket

    import httpx

    monkeypatch.setattr(httpx, "Client", tripwire("httpx.Client"))
    monkeypatch.setattr(httpx, "AsyncClient", tripwire("httpx.AsyncClient"))
    monkeypatch.setattr(httpx, "post", tripwire("httpx.post"), raising=False)
    monkeypatch.setattr(smtplib, "SMTP", tripwire("smtplib.SMTP"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", tripwire("smtplib.SMTP_SSL"))
    monkeypatch.setattr(socket, "create_connection", tripwire("socket.create_connection"))
    return touched


# ---------------------------------------------------------------------------
# One seeker, one approved package addressed to a Belgian company
# ---------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    from dreamjob.db.connection import insert_row, utcnow

    now = utcnow()
    seeker_id = insert_row(
        "job_seeker",
        {
            "email": "seeker@example.test",
            "display_name": "Stephane van der Aa",
            "locale": "en",
            "created_at": now,
            "updated_at": now,
        },
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": "{}", "created_at": now},
    )
    directive_id = insert_row(
        "directive_set", {"job_seeker_id": seeker_id, "name": "d", "created_at": now}
    )
    campaign_id = insert_row(
        "campaign",
        {
            "job_seeker_id": seeker_id,
            "directive_set_id": directive_id,
            "profile_version_id": profile_id,
            "name": "c",
            "created_at": now,
        },
    )
    company_id = insert_row(
        "company",
        {
            "normalised_name": "acme",
            "name": "Acme NV",
            "domain": "acme.test",
            "country": "BE",
            "locations": '["Brussels, Belgium"]',
            "collected_at": now,
        },
    )
    contact_id = insert_row(
        "contact",
        {
            "company_id": company_id,
            "full_name": "Marie Dupont",
            "role_title": "Head of Data",
            "email": "marie@acme.test",
            "email_validation": "valid",
            "confidence": 0.9,
            "collected_at": now,
        },
    )
    opportunity_id = insert_row(
        "opportunity",
        {
            "job_seeker_id": seeker_id,
            "campaign_id": campaign_id,
            "company_id": company_id,
            "kind": "speculative",
            "title": "Head of Analytics",
            "language": "en",
            "selected": 1,
            "score": 88.0,
            "created_at": now,
            "updated_at": now,
        },
    )

    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4 tailored cv")
    briefing = tmp_path / "briefing.pdf"
    briefing.write_bytes(b"%PDF-1.4 company briefing")
    motivation = tmp_path / "motivation.pdf"
    motivation.write_bytes(b"%PDF-1.4 motivation and fit")

    package_id = insert_row(
        "application_package",
        {
            "job_seeker_id": seeker_id,
            "opportunity_id": opportunity_id,
            "contact_id": contact_id,
            "language": "en",
            "cv_pdf_path": str(cv),
            "briefing_pdf_path": str(briefing),
            "motivation_pdf_path": str(motivation),
            "email_subject": "Head of Analytics at Acme",
            "email_body": "Dear Marie,\n\nI am writing about the analytics team you are building.",
            "status": "approved",
            "consistency_status": "pass",
            "leak_scan_status": "pass",
            "approved_at": now,
            "approved_by": seeker_id,
            "profile_version_id": profile_id,
            "created_at": now,
            "updated_at": now,
        },
    )
    return {
        "seeker_id": seeker_id,
        "campaign_id": campaign_id,
        "company_id": company_id,
        "contact_id": contact_id,
        "opportunity_id": opportunity_id,
        "package_id": package_id,
        "cv": cv,
        "briefing": briefing,
        "motivation": motivation,
    }


@pytest.fixture
def client(world: dict[str, Any]) -> Iterator[TestClient]:
    """Only this slice's router, with the session dependency stubbed out."""
    from dreamjob.api.deps import CurrentSeeker, current_seeker
    from dreamjob.api.routers import apply as apply_router

    app = FastAPI()
    app.include_router(apply_router.router, prefix="/api/apply")
    app.dependency_overrides[current_seeker] = lambda: CurrentSeeker(
        id=world["seeker_id"],
        email="seeker@example.test",
        display_name="Stephane van der Aa",
        is_admin=True,
        locale="en",
    )
    with TestClient(app) as http:
        yield http


# ---------------------------------------------------------------------------
# RK-05: nothing leaves the machine
# ---------------------------------------------------------------------------


def test_no_smtp_or_http_call_is_made_while_the_dry_run_is_on(world, no_transport):
    """The test this slice exists for.

    The whole path runs - recipient, guard rails, MIME with the CV attached -
    and the tripwires are never touched.  Both entry points are exercised: the
    Apply Browser's own, and ``dispatcher.send_package``, which is what a
    caller that has never heard of the guard would reach for.
    """
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import dry_run
    from dreamjob.mail.dispatcher import send_package

    assert dry_run.is_armed() is True

    # The guard is not in the router.  A caller that has never heard of it and
    # reaches straight for the dispatcher composes the whole message, writes
    # its dispatch row, and is refused by the backend itself.
    with pytest.raises(dry_run.TransportBlocked):
        send_package(
            world["package_id"],
            world["seeker_id"],
            approved_by=world["seeker_id"],
            ignore_window=True,
        )
    assert no_transport == []

    result = dry_run.send_one(
        world["package_id"], world["seeker_id"], approved_by=world["seeker_id"]
    )

    assert result["sent"] is False
    assert result["status"] == "dry_run"
    assert "DREAMJOB_MAIL_DRY_RUN" in result["message"]
    assert Path(result["mime_path"]).is_file()
    assert no_transport == []

    row = repo.get_dispatch(result["dispatch_id"], world["seeker_id"])
    assert row["delivery_status"] == "dry_run"
    assert row["sent_at"] is None

    # Nothing anywhere in the send log claims to have left.
    dispatches = repo.list_dispatches(world["seeker_id"])
    assert len(dispatches) == 2
    assert all(d["sent_at"] is None for d in dispatches)
    assert {d["delivery_status"] for d in dispatches} == {"dry_run", "failed"}


def test_the_assembled_message_carries_the_cv_and_never_the_briefing(world, no_transport):
    """FR-321 and NFR-302 hold in the file that was written, not just in theory."""
    from dreamjob.mail import dry_run

    result = dry_run.send_one(
        world["package_id"], world["seeker_id"], approved_by=world["seeker_id"]
    )
    mime = message_from_bytes(Path(result["mime_path"]).read_bytes(), policy=policy.default)

    attachments = [p for p in mime.walk() if p.get_filename()]
    assert [p.get_filename() for p in attachments] == ["CV-Stephane-van-der-Aa.pdf"]
    assert attachments[0].get_payload(decode=True) == world["cv"].read_bytes()

    raw = Path(result["mime_path"]).read_bytes()
    assert b"company briefing" not in raw
    assert b"motivation and fit" not in raw

    body = mime.get_body(preferencelist=("plain",)).get_content()
    # NFR-302: the objection sentence, naming an address that is actually read.
    assert "no thank you" in " ".join(body.lower().split())
    assert "seeker@example.test" in body
    # RK-05: plain text only, no HTML alternative.
    assert not any(p.get_content_type() == "text/html" for p in mime.walk())
    assert mime["To"].endswith("<marie@acme.test>")
    assert no_transport == []


def test_a_dry_run_costs_nothing_against_the_cap_and_marks_nothing_sent(world, no_transport):
    """FR-325: a message that never left may not consume the day's allowance."""
    from dreamjob.db.repositories import applications as packages_repo
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import dry_run

    before = dry_run.send_status(world["seeker_id"])
    dry_run.send_one(world["package_id"], world["seeker_id"], approved_by=world["seeker_id"])
    after = dry_run.send_status(world["seeker_id"])

    assert before["daily_cap"]["remaining"] == after["daily_cap"]["remaining"]
    assert repo.last_sent_at(world["seeker_id"]) is None

    package = packages_repo.get_package(world["package_id"], world["seeker_id"])
    assert package["status"] == "approved"  # not 'sent'
    assert no_transport == []


def test_the_same_package_can_be_prepared_again(world, no_transport):
    """A dry run is for inspection, so it has to be repeatable."""
    from dreamjob.mail import dry_run

    first = dry_run.send_one(
        world["package_id"], world["seeker_id"], approved_by=world["seeker_id"]
    )
    second = dry_run.send_one(
        world["package_id"], world["seeker_id"], approved_by=world["seeker_id"]
    )
    assert first["mime_path"] != second["mime_path"]
    assert Path(first["mime_path"]).is_file() and Path(second["mime_path"]).is_file()
    assert no_transport == []


def test_an_objection_refuses_the_dry_run_too(world, no_transport):
    """NFR-302: an objected address is not written to, not even onto disk."""
    from dreamjob.db.repositories import contacts as contacts_repo
    from dreamjob.mail import dry_run
    from dreamjob.mail.dispatcher import SendRefused

    contacts_repo.record_objection("marie@acme.test", source="reply")
    with pytest.raises(SendRefused, match="objected"):
        dry_run.send_one(world["package_id"], world["seeker_id"], approved_by=world["seeker_id"])
    assert no_transport == []


def test_an_unvalidated_address_refuses_the_dry_run_too(world, no_transport):
    """FR-304: only 'valid' or 'risky' addresses are used, dry run included."""
    from dreamjob.db.connection import execute
    from dreamjob.mail import dry_run
    from dreamjob.mail.dispatcher import SendRefused

    execute(
        "UPDATE contact SET email_validation = 'invalid' WHERE id = ?", (world["contact_id"],)
    )
    with pytest.raises(SendRefused, match="FR-304"):
        dry_run.send_one(world["package_id"], world["seeker_id"], approved_by=world["seeker_id"])
    assert no_transport == []


def test_an_unapproved_package_is_refused(world, no_transport):
    """FR-324: a person approves the material before anything is assembled."""
    from dreamjob.db.connection import execute
    from dreamjob.mail import dry_run
    from dreamjob.mail.dispatcher import SendRefused

    execute(
        "UPDATE application_package SET status = 'draft' WHERE id = ?", (world["package_id"],)
    )
    with pytest.raises(SendRefused, match="FR-324"):
        dry_run.send_one(world["package_id"], world["seeker_id"], approved_by=world["seeker_id"])
    assert no_transport == []


def test_send_all_prepares_the_approved_selection_and_sends_none(world, no_transport):
    from dreamjob.mail import dry_run

    result = dry_run.send_all(world["seeker_id"], approved_by=world["seeker_id"])

    assert result["dry_run"] is True
    assert result["sent"] == 0
    assert result["prepared"] == 1
    assert "None were sent" in result["message"]
    assert no_transport == []


def test_send_status_explains_the_guard(world, no_transport):
    from dreamjob.mail import dry_run

    state = dry_run.send_status(world["seeker_id"])

    assert state["sending_armed"] is False
    assert state["dry_run"] is True
    assert any("DREAMJOB_MAIL_DRY_RUN" in reason for reason in state["blocked_because"])
    assert state["setting"]["name"] == "DREAMJOB_MAIL_DRY_RUN"
    assert state["setting"]["default"] is True
    # The guard rails FR-325 asks about, reported before anything is attempted.
    assert state["daily_cap"]["cap"] == 25
    assert state["daily_cap"]["remaining"] == 25
    assert state["send_window"]["start"] == "08:30"
    assert state["send_window"]["end"] == "17:30"
    assert "gmail_oauth" in state["guarded_backends"]
    assert "resend" in state["guarded_backends"]
    assert no_transport == []


def test_turning_the_guard_off_hands_the_send_back_to_the_dispatcher(world):
    """The switch is a switch: with it off, the ordinary path runs untouched."""
    from dreamjob.db.connection import insert_row, utcnow
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import dry_run
    from dreamjob.mail import gmail as _gmail  # noqa: F401 - populates the registry
    from dreamjob.mail.base import (
        BackendCapabilities,
        MailBackend,
        SendResult,
        register_backend,
    )

    @register_backend
    class RecordingBackend(MailBackend):
        capabilities: ClassVar[BackendCapabilities] = BackendCapabilities(
            key="recording", display_name="Recording"
        )
        sent: ClassVar[list] = []

        def sender_address(self) -> str:
            return "seeker@example.test"

        def status(self) -> dict:
            return {"backend": "recording", "configured": True, "reason": None}

        def send(self, message):
            self.check_message(message)
            RecordingBackend.sent.append(message)
            return SendResult(message_id=message.message_id, thread_id="t1", status="sent")

    RecordingBackend.sent = []
    insert_row(
        "mail_account",
        {
            "job_seeker_id": world["seeker_id"],
            "backend": "recording",
            "address": "seeker@example.test",
            "credentials_enc": b"x",
            "connected_at": utcnow(),
        },
    )

    _set_dry_run(False)
    try:
        assert dry_run.is_armed() is False
        result = dry_run.send_one(
            world["package_id"],
            world["seeker_id"],
            approved_by=world["seeker_id"],
            backend_key="recording",
            ignore_window=True,
        )
    finally:
        _set_dry_run(True)

    assert result["status"] == "sent"
    assert len(RecordingBackend.sent) == 1
    assert repo.get_dispatch(result["dispatch_id"], world["seeker_id"])["sent_at"]


# ---------------------------------------------------------------------------
# The browser itself (FR-284, FR-321, FR-324)
# ---------------------------------------------------------------------------


def test_browser_lists_the_selection_with_its_package_and_contact(client, world):
    body = client.get("/api/apply/browser").json()

    assert body["total"] == 1
    assert body["sending"]["dry_run"] is True
    row = body["rows"][0]
    assert row["opportunity_id"] == world["opportunity_id"]
    assert row["company_name"] == "Acme NV"
    assert row["contact_email"] == "marie@acme.test"
    assert row["package_status"] == "approved"
    assert row["state"] == "approved"
    # Per-artefact availability, which is what the screen offers to open.
    assert row["has_cv"] is True
    assert row["has_briefing"] is True
    assert row["has_motivation"] is True
    assert row["has_email"] is True


def test_browser_filters_and_pages(client, world):
    assert client.get("/api/apply/browser", params={"filters": "generated"}).json()["total"] == 1
    assert (
        client.get("/api/apply/browser", params={"filters": "not_generated"}).json()["total"] == 0
    )
    assert client.get("/api/apply/browser", params={"q": "Acme"}).json()["total"] == 1
    assert client.get("/api/apply/browser", params={"q": "nothing"}).json()["total"] == 0

    paged = client.get("/api/apply/browser", params={"limit": 1, "offset": 1}).json()
    assert paged["rows"] == [] and paged["total"] == 1

    bad = client.get("/api/apply/browser", params={"filters": "made_up"})
    assert bad.status_code == 400
    assert "made_up" in bad.json()["detail"]


def test_browser_shows_a_prepared_message_as_dry_run_not_as_sent(client, world, no_transport):
    from dreamjob.mail import dry_run

    dry_run.send_one(world["package_id"], world["seeker_id"], approved_by=world["seeker_id"])

    row = client.get("/api/apply/browser").json()["rows"][0]
    assert row["state"] == "dry_run"
    assert row["dry_run"]["mime_path"].endswith(".eml")
    assert row["dispatched_at"] is None


def test_select_and_deselect(client, world):
    from dreamjob.db.connection import query_one

    off = client.post(
        "/api/apply/select",
        json={"opportunity_ids": [world["opportunity_id"]], "selected": False},
    )
    assert off.status_code == 200 and off.json()["changed"] == 0  # never selected here before
    assert query_one(
        "SELECT selected FROM opportunity WHERE id = ?", (world["opportunity_id"],)
    )["selected"] == 0

    on = client.post(
        "/api/apply/select",
        json={"opportunity_ids": [world["opportunity_id"]], "selected": True},
    )
    assert on.json()["changed"] == 1
    assert on.json()["selection_counts"] == {"selected": 1}

    unknown = client.post(
        "/api/apply/select", json={"opportunity_ids": ["not-a-real-id"], "selected": True}
    )
    assert unknown.status_code == 404


def test_one_application_in_full(client, world):
    body = client.get(f"/api/apply/{world['opportunity_id']}").json()

    assert body["email"]["subject"] == "Head of Analytics at Acme"
    assert body["email"]["body"].startswith("Dear Marie")
    assert body["contact"]["email"] == "marie@acme.test"
    assert body["consistency"]["status"] == "pass"
    assert body["state"] == "approved"
    assert body["sending"]["dry_run"] is True
    # FR-321: the two job-seeker-only documents are downloadable and marked.
    assert body["documents"]["briefing"]["never_sent"] is True
    assert body["documents"]["motivation"]["never_sent"] is True
    assert body["documents"]["cv_pdf"]["never_sent"] is False
    assert body["documents"]["cv_pdf"]["available"] is True
    assert body["send_check"]["code"] in {"ok", "outside_window"}


def test_editing_the_email_reopens_the_package(client, world):
    """FR-324: an approved message cannot be changed and stay approved."""
    response = client.put(
        f"/api/apply/{world['opportunity_id']}/email",
        json={"subject": "Analytics leadership at Acme", "body": "Dear Marie,\n\nRewritten."},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["email_subject"] == "Analytics leadership at Acme"
    assert body["status"] == "draft"

    empty = client.put(f"/api/apply/{world['opportunity_id']}/email", json={})
    assert empty.status_code == 400


def test_documents_download_and_unknown_kinds_are_refused(client, world):
    ok = client.get(f"/api/apply/{world['opportunity_id']}/document/cv_pdf")
    assert ok.status_code == 200
    assert ok.content == world["cv"].read_bytes()

    # The briefing is the job seeker's own material: downloadable here (FR-329),
    # never attachable to a message (FR-321).
    briefing = client.get(f"/api/apply/{world['opportunity_id']}/document/briefing")
    assert briefing.status_code == 200

    assert client.get(f"/api/apply/{world['opportunity_id']}/document/nonsense").status_code == 404


def test_send_and_send_all_over_http_report_that_nothing_was_sent(client, world, no_transport):
    one = client.post(f"/api/apply/{world['opportunity_id']}/send", json={})
    assert one.status_code == 201
    assert one.json()["sent"] is False
    assert one.json()["status"] == "dry_run"

    every = client.post("/api/apply/send-all", json={})
    assert every.status_code == 202
    assert every.json()["sent"] == 0

    state = client.get("/api/apply/send-status").json()
    assert state["sending_armed"] is False
    assert state["dry_run"] is True
    assert no_transport == []


def test_a_missing_package_is_a_404_not_a_500(client, world):
    from dreamjob.db.connection import execute

    execute("DELETE FROM application_package WHERE id = ?", (world["package_id"],))
    assert client.get(f"/api/apply/{world['opportunity_id']}").status_code == 404
    assert client.post(f"/api/apply/{world['opportunity_id']}/send", json={}).status_code == 404
    assert client.get("/api/apply/does-not-exist").status_code == 404
