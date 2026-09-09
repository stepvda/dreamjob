"""What the Apply Browser screen relies on (FR-321, FR-324, FR-325, RK-05).

The screen makes three promises to the person reading it, and all three are
kept by the server rather than by the interface.  These are the tests that
would fail if any of them stopped being true:

* pressing **Send** builds the whole message, attaches the tailored CV and
  nothing else, and sends nothing while the dry run is on;
* the banner above the send controls can always say *which* setting is holding
  the message and what would have to change;
* **Regenerate** never turns a model that spent its budget reasoning into a
  500 - the task is routed to the chat model instead (CR-409).

No network, no LLM and no mail provider: the guard is exercised for real
against a throwaway database, which is the only honest way to test a rule whose
whole content is "nothing left the machine".
"""

from __future__ import annotations

import base64
import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from dreamjob.config import get_settings

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_SESSION_SECRET",
    "DREAMJOB_ENV",
    "DREAMJOB_MAIL_DRY_RUN",
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
    # The default, stated: the screen's whole premise is that this is on.
    os.environ["DREAMJOB_MAIL_DRY_RUN"] = "true"
    os.environ["RESEND_API_KEY"] = ""
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


# ---------------------------------------------------------------------------
# One approved application, addressed to a Belgian company
# ---------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path) -> dict:
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
            "created_at": now,
            "updated_at": now,
        },
    )

    # The three generated PDFs. Only one of them may ever be attached.
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4 tailored cv")
    briefing = tmp_path / "briefing.pdf"
    briefing.write_bytes(b"%PDF-1.4 briefing")
    motivation = tmp_path / "motivation.pdf"
    motivation.write_bytes(b"%PDF-1.4 motivation")

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
            "email_body": "Dear Marie,\n\nMy CV is attached.",
            "consistency_status": "pass",
            "leak_scan_status": "pass",
            "status": "approved",
            "approved_at": now,
            "approved_by": seeker_id,
            "profile_version_id": profile_id,
            "created_at": now,
            "updated_at": now,
        },
    )
    return {
        "seeker_id": seeker_id,
        "opportunity_id": opportunity_id,
        "package_id": package_id,
        "contact_id": contact_id,
        "company_id": company_id,
        "briefing": briefing,
        "motivation": motivation,
    }


@pytest.fixture
def client(world):
    from dreamjob.api.deps import CurrentSeeker, current_seeker
    from dreamjob.api.routers import apply as apply_router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(apply_router.router, prefix="/api/apply")
    seeker = CurrentSeeker(
        id=world["seeker_id"],
        email="seeker@example.test",
        display_name="Stephane van der Aa",
        is_admin=True,
        locale="en",
    )
    app.dependency_overrides[current_seeker] = lambda: seeker
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# The banner: what the screen shows before either send control is pressed
# ---------------------------------------------------------------------------


def test_send_status_names_the_setting_and_says_what_would_have_to_change(client):
    """The banner has to be able to explain itself, not merely say "no"."""
    body = client.get("/api/apply/send-status").json()

    assert body["dry_run"] is True
    assert body["sending_armed"] is False
    # The banner prints these three fields verbatim.
    assert body["setting"]["name"] == "DREAMJOB_MAIL_DRY_RUN"
    assert body["setting"]["default"] is True
    assert "restart" in body["setting"]["how_to_arm_sending"].lower()
    # And it says where the guard is, because "the interface" would be a lie.
    assert "dry_run" in body["setting"]["enforced_in"]
    assert any("DREAMJOB_MAIL_DRY_RUN" in line for line in body["blocked_because"])
    # The three FR-325 rails the banner reports next to it.
    assert body["daily_cap"]["cap"] > 0
    assert body["rate"]["min_interval_seconds"] > 0
    assert body["send_window"]["start"] and body["send_window"]["end"]


# ---------------------------------------------------------------------------
# The button that runs and reports (FR-321, FR-324, RK-05)
# ---------------------------------------------------------------------------


def test_send_assembles_the_message_with_the_cv_and_sends_nothing(client, world):
    """"Send email with CV attached", pressed, in dry run."""
    response = client.post(f"/api/apply/{world['opportunity_id']}/send", json={})
    assert response.status_code == 201, response.text
    body = response.json()

    # What the screen reports back to the person who pressed it.
    assert body["sent"] is False
    assert body["dry_run"] is True
    assert body["status"] == "dry_run"
    assert body["recipient"] == "marie@acme.test"
    assert body["subject"] == "Head of Analytics at Acme"

    # FR-321: the tailored CV, and only the tailored CV.
    assert len(body["attachments"]) == 1
    assert body["attachments"][0].lower().endswith(".pdf")
    assert "briefing" not in " ".join(body["attachments"]).lower()
    assert "motivation" not in " ".join(body["attachments"]).lower()

    # The assembled message is on disk, byte for byte, and holds the CV.
    written = Path(body["mime_path"])
    assert written.is_file()
    raw = written.read_bytes()
    assert b"marie@acme.test" in raw
    assert b"Head of Analytics at Acme" in raw
    # Neither seeker-only document reached the message.
    assert world["briefing"].name.encode() not in raw
    assert world["motivation"].name.encode() not in raw


def test_a_dry_run_never_looks_like_a_send(client, world):
    """Nothing may be recorded as having left, because nothing did."""
    from dreamjob.db.repositories import applications as packages_repo
    from dreamjob.db.repositories import dispatch as dispatch_repo

    client.post(f"/api/apply/{world['opportunity_id']}/send", json={})

    rows = dispatch_repo.dispatches_for_package(world["package_id"], world["seeker_id"])
    assert len(rows) == 1
    assert rows[0]["delivery_status"] == "dry_run"
    # No sent_at: the FR-325 daily cap and the pacing rule count real sends.
    assert not rows[0]["sent_at"]
    assert dispatch_repo.sent_since(world["seeker_id"], "2000-01-01T00:00:00+00:00") == 0

    # And the package is still approved rather than sent, so it can be
    # re-read, re-edited and re-assembled as often as the job seeker likes.
    package = packages_repo.get_package(world["package_id"], world["seeker_id"])
    assert package["status"] == "approved"


def test_send_all_reports_every_message_and_delivers_none(client, world):
    """FR-324's bulk path, behind the summary the screen shows first."""
    response = client.post(
        "/api/apply/send-all", json={"package_ids": [world["package_id"]], "limit": 10}
    )
    assert response.status_code == 202, response.text
    body = response.json()

    assert body["dry_run"] is True
    assert body["sent"] == 0
    assert body["prepared"] == 1
    assert "None were sent" in body["message"]
    assert body["results"][0]["status"] == "dry_run"
    assert body["results"][0]["recipient"] == "marie@acme.test"


def test_the_guard_holds_even_when_the_dispatcher_is_called_directly(world):
    """The rule is not "this router will not send"; it is "nothing can".

    The Apply Browser leaves both send buttons enabled on purpose, so the thing
    that stops a message has to be somewhere an interface cannot step around.
    This calls the dispatcher itself - the code path a future router, a script
    or a queue worker would use - with the send window stepped over so that it
    reaches the backend, and the transport refuses it there.
    """
    from dreamjob.db.repositories import dispatch as dispatch_repo
    from dreamjob.mail import dry_run  # importing it is what arms the guard
    from dreamjob.mail.dispatcher import send_package

    assert dry_run.is_armed() is True

    with pytest.raises(dry_run.TransportBlocked) as excinfo:
        send_package(
            world["package_id"],
            world["seeker_id"],
            approved_by=world["seeker_id"],
            backend_key="resend",
            ignore_window=True,
        )
    assert dry_run.SETTING_NAME in str(excinfo.value)

    # And the refusal happened before the wire, so nothing counts as sent.
    assert dispatch_repo.sent_since(world["seeker_id"], "2000-01-01T00:00:00+00:00") == 0


# ---------------------------------------------------------------------------
# Regenerate must not 500 when the model spends its budget thinking (CR-409)
# ---------------------------------------------------------------------------


class _CutOffOnce:
    """A model that reasons its whole budget away unless it is the chat model."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete_json(self, task: str, system: str, user: str, **kwargs: object) -> dict:
        from dreamjob.llm.client import TruncatedResponse

        self.calls.append(dict(kwargs))
        if kwargs.get("prefer_strong") is False:
            return {"ok": True}
        raise TruncatedResponse(
            "deepseek-reasoner spent its whole 8000-token budget on reasoning "
            "(8000 completion tokens, 18202 characters of it reasoning) and returned "
            f"no answer for task {task!r}. Raise max_tokens, or use the chat model."
        )


def test_a_truncated_reasoning_answer_is_routed_to_the_chat_model() -> None:
    """The generators degrade rather than raising, so no route returns a 500.

    ``TruncatedResponse`` is a distinct class, and the fallback recognises it
    as such.  Matching on the wording of the message - which is what happened
    before - meant a cut-off reasoning call sailed past the retry and surfaced
    as an unhandled error under a button labelled "Regenerate".
    """
    from dreamjob.documents._llm import complete_json

    llm = _CutOffOnce()
    result = complete_json(llm, "generate.email", "system", "user")

    assert result == {"ok": True}
    assert len(llm.calls) == 2
    assert "prefer_strong" not in llm.calls[0]
    assert llm.calls[1]["prefer_strong"] is False
