"""Post-application pipeline and continuous monitoring (FR-401..403, FR-421..425,
FR-444, NFR-305).

Everything here runs against a throwaway SQLite file with no network and no
LLM.  That is not a convenience: FR-422, FR-423, FR-424 and FR-444 each have a
deterministic path that has to work when the model or the calendar cannot be
reached, and those paths are what these tests exercise.

The properties being pinned down:

* a card is opened once per sent application and reply detection moves it
  forwards only (FR-421);
* the multilingual rule classifier answers in the same vocabulary the model is
  asked for (FR-422);
* a drafted reply is written in ``draft`` status and approving it does not send
  it (NFR-305);
* relative dates in four languages resolve against the date the reply arrived,
  not against today (FR-423);
* a confirmed interview produces an ``.ics`` with the briefing attached even
  with no calendar connected (FR-423);
* a mock interview completes and ends with weak spots without the model
  (FR-424);
* every learned effect carries the sample it rests on, and a small sample
  changes nothing (FR-425);
* the digest picks exactly one next action, by the documented ladder (FR-403).
"""

from __future__ import annotations

import base64
import os
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dreamjob.config import get_settings

_ENV_KEYS = ("DREAMJOB_DATA_DIR", "DREAMJOB_DB_PATH", "DREAMJOB_MASTER_KEY", "DREAMJOB_ENV")


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_ENV"] = "development"
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
# Fixture data: one seeker, one company, one opportunity, one sent application
# ---------------------------------------------------------------------------


def _iso(days: float = 0.0) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat(timespec="seconds")


def seed(*, sent_days_ago: float = 3.0, email_body: str = "short letter") -> dict:
    from dreamjob.db.connection import insert_row, to_json, utcnow

    seeker_id = insert_row(
        "job_seeker",
        {
            "email": "seeker@example.test",
            "display_name": "Stephane van der Aa",
            "locale": "en",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    directive_id = insert_row(
        "directive_set",
        {
            "job_seeker_id": seeker_id,
            "name": "Default",
            "compensation": to_json({"minimum_package": 85000, "currency": "EUR"}),
            "created_at": utcnow(),
        },
    )
    profile_id = insert_row(
        "profile_version",
        {
            "job_seeker_id": seeker_id,
            "version": 1,
            "sections": to_json({"summary": "Data platform lead"}),
            "created_at": utcnow(),
        },
    )
    campaign_id = insert_row(
        "campaign",
        {
            "job_seeker_id": seeker_id,
            "directive_set_id": directive_id,
            "profile_version_id": profile_id,
            "name": "Autumn",
            "status": "running",
            "created_at": utcnow(),
        },
    )
    company_id = insert_row(
        "company",
        {
            "normalised_name": "northwind",
            "name": "Northwind Analytics",
            "domain": "northwind.test",
            "country": "BE",
            "size_fte": 120,
            "collected_at": utcnow(),
        },
    )
    insert_row(
        "financial_analysis",
        {
            "company_id": company_id,
            "ability_to_pay": 72,
            "ability_to_pay_rationale": "Revenue EUR 24m, equity EUR 9m, no net debt.",
            "personnel_cost_per_fte": 78000,
            "headcount_cagr": 0.14,
            "trajectory": "growing",
            "computed_at": utcnow(),
        },
    )
    vacancy_id = insert_row(
        "vacancy",
        {
            "company_id": company_id,
            "title": "Lead Data Engineer",
            "description": "You will own the Databricks platform and lead two engineers.",
            "required_skills": to_json(["Databricks", "Python", "dbt"]),
            "location": "Ghent",
            "country": "BE",
            "collected_at": utcnow(),
        },
    )
    opportunity_id = insert_row(
        "opportunity",
        {
            "job_seeker_id": seeker_id,
            "campaign_id": campaign_id,
            "company_id": company_id,
            "vacancy_id": vacancy_id,
            "kind": "vacancy",
            "title": "Lead Data Engineer",
            "description": "Own the data platform.",
            "required_skills": to_json(["Databricks", "Python", "dbt"]),
            "country": "BE",
            "language": "en",
            "score": 82.0,
            "user_status": "applied",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    contact_id = insert_row(
        "contact",
        {
            "company_id": company_id,
            "full_name": "Ann Peeters",
            "role_title": "Head of Data",
            "email": "ann.peeters@northwind.test",
            "collected_at": utcnow(),
        },
    )
    briefing = Path(get_settings().generated_dir) / "briefing.pdf"
    briefing.parent.mkdir(parents=True, exist_ok=True)
    briefing.write_bytes(b"%PDF-1.4 fake briefing for the attachment test")
    package_id = insert_row(
        "application_package",
        {
            "job_seeker_id": seeker_id,
            "opportunity_id": opportunity_id,
            "contact_id": contact_id,
            "language": "en",
            "cv_template": "classic",
            "email_body": email_body,
            "briefing_pdf_path": str(briefing),
            "status": "sent",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    dispatch_id = insert_row(
        "dispatch",
        {
            "job_seeker_id": seeker_id,
            "application_package_id": package_id,
            "backend": "resend",
            "recipient_email": "ann.peeters@northwind.test",
            "subject": "Application: Lead Data Engineer",
            "message_id": "<original@dreamjob>",
            "thread_id": "thread-1",
            "sent_at": _iso(-sent_days_ago),
            "delivery_status": "sent",
            "follow_up_due_at": _iso(-1),
            "created_at": utcnow(),
        },
    )
    return {
        "seeker_id": seeker_id,
        "campaign_id": campaign_id,
        "company_id": company_id,
        "vacancy_id": vacancy_id,
        "opportunity_id": opportunity_id,
        "package_id": package_id,
        "dispatch_id": dispatch_id,
        "contact_id": contact_id,
        "briefing": str(briefing),
    }


def add_reply(data: dict, body: str, subject: str = "Re: Application", **kw) -> str:
    from dreamjob.db.connection import insert_row, utcnow

    return insert_row(
        "incoming_reply",
        {
            "job_seeker_id": data["seeker_id"],
            "dispatch_id": data["dispatch_id"],
            "from_address": kw.get("from_address", "Ann Peeters <ann@northwind.test>"),
            "subject": subject,
            "body": body,
            "received_at": kw.get("received_at", utcnow()),
            "message_id": kw.get("message_id", "<reply-1@northwind.test>"),
            "in_reply_to": "<original@dreamjob>",
            "created_at": utcnow(),
        },
    )


# ---------------------------------------------------------------------------
# FR-421: the board
# ---------------------------------------------------------------------------


def test_board_opens_one_card_per_sent_application_and_is_idempotent() -> None:
    from dreamjob.postapp import board

    data = seed()
    first = board.sync_sent_applications(data["seeker_id"])
    second = board.sync_sent_applications(data["seeker_id"])

    assert first["created"] == 1
    assert second["created"] == 0, "syncing twice must not open a second card"

    view = board.board(data["seeker_id"])
    sent_column = next(s for s in view["stages"] if s["stage"] == "sent")
    assert sent_column["count"] == 1
    card = sent_column["cards"][0]
    assert card["company_name"] == "Northwind Analytics"
    assert card["next_action"], "FR-421: every card carries a next action"
    # FR-425: the variables were frozen at send time.
    stored = board.decode_variables(
        next(c for c in board.repo.list_cards(data["seeker_id"]))
    )
    assert stored["cv_template"] == "classic"
    assert stored["contact_type"] == "hiring_manager"


def test_reply_detection_moves_the_card_forwards_only() -> None:
    from dreamjob.postapp import board

    data = seed()
    board.sync_sent_applications(data["seeker_id"])
    card = board.repo.list_cards(data["seeker_id"])[0]

    first_id = add_reply(data, "We would like to invite you for an interview.")
    invitation = board.apply_reply(
        data["seeker_id"],
        {**board.repo.get_reply(first_id, data["seeker_id"])},
        classification="interview_invitation",
    )
    assert invitation is not None
    assert invitation.to_stage == "interview"
    assert invitation.trigger == "reply_detection"

    # A later "we will be in touch" must not drag an interview back to replied.
    second_id = add_reply(data, "Thanks, we will be in touch.", message_id="<r2@x>")
    later = board.apply_reply(
        data["seeker_id"],
        {**board.repo.get_reply(second_id, data["seeker_id"])},
        classification="interest",
    )
    assert later is not None and later.changed is False
    assert board.repo.get_card(card["id"], data["seeker_id"])["stage"] == "interview"

    # The seeker, however, may move it anywhere (NFR-305).
    back = board.transition(data["seeker_id"], card["id"], "replied", trigger="user")
    assert back.to_stage == "replied"

    events = board.repo.card_events(card["id"], data["seeker_id"])
    triggers = [e["trigger"] for e in events]
    assert "system" in triggers and "reply_detection" in triggers and "user" in triggers


def test_an_outcome_needs_a_closed_card() -> None:
    from dreamjob.postapp import board

    data = seed()
    board.sync_sent_applications(data["seeker_id"])
    card_id = board.repo.list_cards(data["seeker_id"])[0]["id"]

    with pytest.raises(board.StageError):
        board.transition(data["seeker_id"], card_id, "interview", outcome="rejected")

    closed = board.transition(
        data["seeker_id"], card_id, "closed", outcome="rejected", note="not selected"
    )
    assert closed.outcome == "rejected"
    stored = board.repo.get_card(card_id, data["seeker_id"])
    assert stored["outcome"] == "rejected" and stored["outcome_at"]
    assert "not selected" in stored["notes"]


# ---------------------------------------------------------------------------
# FR-422: classification and drafting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Unfortunately we have decided to proceed with another candidate.", "rejection"),
        ("Helaas kunnen we u niet weerhouden voor deze functie.", "rejection"),
        ("Malheureusement, votre candidature n'a pas ete retenue.", "rejection"),
        ("Leider koennen wir Ihre Bewerbung nicht beruecksichtigen.", "rejection"),
        ("We would like to invite you for an interview next week.", "interview_invitation"),
        ("Graag nodigen we u uit voor een kennismakingsgesprek.", "interview_invitation"),
        ("Nous aimerions vous rencontrer pour un entretien.", "interview_invitation"),
        ("I am currently out of office until 5 September.", "automatic_reply"),
        ("Ik ben afwezig, dit is een automatisch antwoord.", "automatic_reply"),
        ("Could you please send your salary expectations?", "request_for_information"),
        ("Kunt u ons uw loonverwachting bezorgen?", "request_for_information"),
        ("I am not the right person; my colleague Jan handles this.", "referral"),
        ("Interesting profile, we will be in touch shortly.", "interest"),
    ],
)
def test_reply_heuristics_match_the_llm_vocabulary(body: str, expected: str) -> None:
    """FR-422's six classes, recognised in all four languages without a model."""
    from dreamjob.postapp import reply_classifier

    result = reply_classifier.classify_heuristically("Re: Application", body)
    assert result["classification"] == expected
    assert result["classification"] in reply_classifier.CLASSES


def test_reply_draft_falls_back_to_template_and_is_never_sent() -> None:
    """FR-422 drafts an answer in the same thread; NFR-305 keeps it unsent."""
    from dreamjob.postapp import board, reply_classifier

    data = seed()
    board.sync_sent_applications(data["seeker_id"])
    reply_id = add_reply(
        data, "Unfortunately we have decided to proceed with another candidate."
    )

    result = reply_classifier.process_reply(data["seeker_id"], reply_id, llm=None)

    assert result["classification"]["classification"] == "rejection"
    assert result["transition"]["to_stage"] == "closed"
    draft = reply_classifier.repo.get_draft(result["draft_id"], data["seeker_id"])
    assert draft["status"] == "draft", "a draft must never leave draft status on its own"
    assert draft["sent_at"] is None
    # Threading: the answer belongs in the same conversation (FR-422).
    assert draft["in_reply_to"] == "<reply-1@northwind.test>"
    assert draft["subject"].startswith("Re:")
    assert draft["thread_id"] == "thread-1"

    approved = reply_classifier.approve_draft(data["seeker_id"], result["draft_id"])
    assert approved["status"] == "approved"
    assert approved["sent_at"] is None, "approval is not sending (NFR-305)"
    assert reply_classifier.repo.get_reply(reply_id, data["seeker_id"])["handled"] == 1


def test_an_automatic_reply_neither_moves_the_card_nor_gets_a_draft() -> None:
    from dreamjob.postapp import board, reply_classifier

    data = seed()
    board.sync_sent_applications(data["seeker_id"])
    reply_id = add_reply(data, "I am currently out of office until 5 September.")

    result = reply_classifier.process_reply(data["seeker_id"], reply_id, llm=None)
    assert result["classification"]["classification"] == "automatic_reply"
    assert result["transition"] is None
    assert result["draft_id"] is None
    assert board.repo.list_cards(data["seeker_id"])[0]["stage"] == "sent"


# ---------------------------------------------------------------------------
# FR-423: slots, availability and the .ics fallback
# ---------------------------------------------------------------------------


def test_relative_dates_resolve_against_the_day_the_reply_arrived() -> None:
    """FR-423: 'next Tuesday' read on Friday is not next Tuesday read today."""
    from dreamjob.postapp import calendar_sync

    # A Friday.
    received = "2026-09-11T09:00:00+00:00"
    slots = calendar_sync.parse_slots(
        "Could you do next Tuesday at 10:00? Otherwise Thursday at 14h30 works.",
        received_at=received,
    )
    assert len(slots) == 2
    first, second = slots
    assert first.start.strftime("%A %H:%M") == "Tuesday 10:00"
    assert first.start.date().isoformat() == "2026-09-15"
    assert second.start.strftime("%A %H:%M") == "Thursday 14:30"
    assert all(s.end > s.start for s in slots)


@pytest.mark.parametrize(
    ("text", "weekday", "hour"),
    [
        ("Would Wednesday at 11:00 suit you?", "Wednesday", 11),
        ("Past woensdag om 11u?", "Wednesday", 11),
        ("Mercredi a 11h vous convient ?", "Wednesday", 11),
        ("Passt Ihnen Mittwoch um 11 Uhr?", "Wednesday", 11),
    ],
)
def test_slots_are_read_in_four_languages(text: str, weekday: str, hour: int) -> None:
    from dreamjob.postapp import calendar_sync

    slots = calendar_sync.parse_slots(text, received_at="2026-09-11T09:00:00+00:00")
    assert slots, f"no slot parsed from {text!r}"
    assert slots[0].start.strftime("%A") == weekday
    assert slots[0].start.hour == hour


def test_slots_without_a_calendar_stay_unknown_and_are_still_proposed() -> None:
    from dreamjob.postapp import calendar_sync

    data = seed()
    slots = calendar_sync.parse_slots(
        "Tuesday at 10:00 or Thursday at 14:00?", received_at="2026-09-11T09:00:00+00:00"
    )
    import asyncio

    checked, used_calendar = asyncio.run(
        calendar_sync.check_availability(data["seeker_id"], slots)
    )
    assert used_calendar is False
    assert {s.availability for s in checked} == {"unknown"}
    assert len(calendar_sync.propose(checked)) == 2


def test_confirming_writes_an_ics_with_the_briefing_attached() -> None:
    """FR-423 degraded: no calendar grant, but the entry and its attachment exist."""
    import asyncio

    from dreamjob.db.connection import to_json
    from dreamjob.postapp import board, calendar_sync

    data = seed()
    board.sync_sent_applications(data["seeker_id"])
    card = board.repo.list_cards(data["seeker_id"])[0]
    start = _iso(4)
    appointment_id = calendar_sync.repo.create_appointment(
        data["seeker_id"],
        {
            "opportunity_id": data["opportunity_id"],
            "pipeline_card_id": card["id"],
            "recommended": to_json(
                [{"start": start, "end": _iso(4.05), "timezone": "Europe/Brussels"}]
            ),
            "briefing_path": data["briefing"],
            "title": "Interview - Lead Data Engineer",
            "status": "proposed",
        },
    )

    result = asyncio.run(calendar_sync.confirm(data["seeker_id"], appointment_id))

    assert result["calendar_event"] is None, "no grant is configured in this environment"
    assert result["briefing_attached"] is True
    assert "No calendar is connected" in result["degraded_reason"]

    ics = Path(result["ics_path"]).read_text()
    assert "BEGIN:VEVENT" in ics and "SUMMARY:Interview" in ics
    assert "ATTACH;FMTTYPE=application/pdf;ENCODING=BASE64" in ics
    assert base64.b64encode(b"%PDF-1.4 fake briefing").decode()[:20] in ics.replace("\r\n ", "")

    # The board followed the confirmation (FR-421).
    assert board.repo.get_card(card["id"], data["seeker_id"])["stage"] == "interview"


# ---------------------------------------------------------------------------
# FR-424: the mock interview
# ---------------------------------------------------------------------------


def test_mock_interview_runs_without_the_model_and_ends_with_weak_spots() -> None:
    from dreamjob.postapp import mock_interview

    data = seed()
    session = mock_interview.start(
        data["seeker_id"], data["opportunity_id"], questions=4, llm=None
    )
    assert session["awaiting_answer"] is True
    assert session["round_number"] == 1
    assert session["current_question"]

    weak_answer = "Yes."
    strong_answer = (
        "When we migrated the reporting stack in 2024 I led a team of three, moved "
        "40 dbt models to Databricks in eleven weeks, and the result was a nightly "
        "run that dropped from four hours to fifty minutes."
    )
    for index in range(4):
        session = mock_interview.answer(
            data["seeker_id"],
            session["id"],
            strong_answer if index % 2 else weak_answer,
            llm=None,
        )

    assert session["status"] == "finished"
    assert session["weak_spots"], "FR-424 ends with a list of weak spots to rehearse"
    assert all("rehearse" in spot for spot in session["weak_spots"])
    scores = [t["feedback"]["score"] for t in session["turns"]]
    assert min(scores) < max(scores), "the structural feedback must discriminate"

    # Repeatable: a second round opens on what went badly (FR-424).
    second = mock_interview.start(
        data["seeker_id"], data["opportunity_id"], questions=4, llm=None
    )
    assert second["round_number"] == 2
    assert len(mock_interview.history(data["seeker_id"])) == 2


def test_answering_a_finished_session_is_refused() -> None:
    from dreamjob.postapp import mock_interview

    data = seed()
    session = mock_interview.start(
        data["seeker_id"], data["opportunity_id"], questions=3, llm=None
    )
    mock_interview.finish(data["seeker_id"], session["id"], llm=None)
    with pytest.raises(mock_interview.SessionClosed):
        mock_interview.answer(data["seeker_id"], session["id"], "too late", llm=None)


# ---------------------------------------------------------------------------
# FR-444: the negotiation brief
# ---------------------------------------------------------------------------


def test_negotiation_brief_needs_the_interview_stage() -> None:
    from dreamjob.postapp import board, negotiation

    data = seed()
    board.sync_sent_applications(data["seeker_id"])
    ok, reason = negotiation.eligible(data["seeker_id"], data["opportunity_id"])
    assert ok is False and "interview stage" in reason
    with pytest.raises(negotiation.NotEligible):
        negotiation.build(data["seeker_id"], data["opportunity_id"], llm=None)


def test_negotiation_brief_without_llm_uses_the_filed_figures() -> None:
    """FR-444: ability to pay, personnel cost per FTE, market data and directives."""
    from dreamjob.postapp import board, negotiation

    data = seed()
    board.sync_sent_applications(data["seeker_id"])
    card_id = board.repo.list_cards(data["seeker_id"])[0]["id"]
    board.transition(data["seeker_id"], card_id, "interview", trigger="user")

    result = negotiation.build(data["seeker_id"], data["opportunity_id"], llm=None)
    figures = result["figures"]

    assert figures["ability_to_pay"] == 72
    assert figures["personnel_cost_per_fte"] == 78000
    assert figures["directive_minimum"] == 85000
    assert figures["ask_min"] is not None and figures["ask_min"] >= figures["walk_away"]
    assert result["case"]["generated_by"] == "computed"
    assert any("78" in a["evidence"] for a in result["case"]["arguments"])
    assert result["case"]["fallbacks"], "FR-444 asks for fallback positions"
    assert result["case"]["risks"], "a brief with no risks has not been thought about"
    assert Path(result["pdf_path"]).exists()
    assert Path(result["pdf_path"]).stat().st_size > 1000

    stored = negotiation.get(data["seeker_id"], data["opportunity_id"])
    assert stored["ask_min"] == figures["ask_min"]
    assert stored["inputs"]["ability_to_pay"] == 72


# ---------------------------------------------------------------------------
# FR-425: outcomes, reported with their sample size
# ---------------------------------------------------------------------------


def test_wilson_interval_is_wide_on_small_samples() -> None:
    from dreamjob.postapp.outcomes import wilson_interval

    low, high = wilson_interval(2, 5)
    assert low < 0.2 and high > 0.7, "a 2-of-5 rate must not look precise"
    tight_low, tight_high = wilson_interval(200, 500)
    assert tight_high - tight_low < high - low


def test_effects_carry_their_sample_and_a_small_one_changes_nothing() -> None:
    """FR-425 reports the learned effect transparently, with the n it rests on."""
    from dreamjob.db.connection import to_json, utcnow
    from dreamjob.db.repositories import pipeline_cards as repo
    from dreamjob.postapp import outcomes

    data = seed()
    # Six resolved applications: two of three "short" letters were answered and
    # none of the three "long" ones. A large effect on paper, far too small to
    # act on - which is exactly what FR-425 has to say out loud.
    for index, (style, replied) in enumerate(
        [
            ("short", True), ("short", True), ("short", False),
            ("long", False), ("long", False), ("long", False),
        ]
    ):
        repo.create_card(
            data["seeker_id"],
            {
                "opportunity_id": data["opportunity_id"],
                "stage": "replied" if replied else "closed",
                "outcome": None if replied else "no_response",
                "reached_replied_at": utcnow() if replied else None,
                "variables": to_json({"email_style": style, "cv_template": "classic"}),
                "created_at": (datetime.now(UTC) - timedelta(days=40 + index)).isoformat(
                    timespec="seconds"
                ),
            },
        )

    report = outcomes.report(data["seeker_id"])
    effects = {(e["variable"], e["value"]): e for e in report["effects"]}
    short = effects[("email_style", "short")]

    assert short["n"] == 3, "the effect must state the sample it rests on"
    assert short["successes"] == 2
    assert short["evidence"] == "indicative", "three applications is not evidence of anything"
    assert short["interval"][1] - short["interval"][0] > 0.3, "the interval must show the doubt"
    assert report["defaults"] == {}, "nothing may be adopted on this sample"
    assert any("significant" in c for c in report["caveats"])
    assert all(str(e["n"]) in s for e, s in zip(report["effects"], report["readable"], strict=True))

    applied = outcomes.apply_learning(data["seeker_id"])
    assert applied["applied"] is False
    assert outcomes.generation_defaults(data["seeker_id"]) == {}


def test_capture_variables_records_what_fr425_names() -> None:
    from dreamjob.postapp import outcomes

    data = seed(email_body="word " * 300)
    variables = outcomes.capture_variables(data["seeker_id"], data["package_id"])

    for key in ("email_style", "cv_template", "contact_type", "send_hour_band",
                "day_of_week", "opportunity_kind"):
        assert key in variables, f"FR-425 names {key} as a recorded variable"
    assert variables["email_style"] == "long"
    assert variables["cv_template"] == "classic"
    assert variables["opportunity_kind"] == "vacancy"


# ---------------------------------------------------------------------------
# FR-401: the watchlist
# ---------------------------------------------------------------------------


def test_a_new_watch_is_due_at_once_and_then_on_its_interval() -> None:
    from dreamjob.db.connection import utcnow
    from dreamjob.db.repositories import pipeline_cards as repo
    from dreamjob.monitoring import watchlist

    data = seed()
    entry = watchlist.add(
        data["seeker_id"], data["company_id"], interval_days=14, campaign_id=data["campaign_id"]
    )
    assert entry["check_interval_days"] == 14
    assert [e["id"] for e in repo.watches_due(utcnow())] == [entry["id"]]

    repo.update_watch(entry["id"], {"next_check_at": _iso(7), "last_checked_at": utcnow()})
    assert repo.watches_due(utcnow()) == []

    # Adding the same company twice updates the watch rather than duplicating it.
    watchlist.add(data["seeker_id"], data["company_id"], interval_days=3)
    assert len(watchlist.entries(data["seeker_id"])) == 1


def test_a_finding_is_announced_once() -> None:
    """FR-401 notifies about new vacancies; a weekly recheck must not repeat itself."""
    from dreamjob.db.repositories import pipeline_cards as repo

    data = seed()
    payload = {
        "kind": "new_vacancy",
        "title": "Northwind Analytics: Lead Data Engineer",
        "dedup_key": f"vacancy:{data['vacancy_id']}",
        "severity": "action",
    }
    assert repo.notify(data["seeker_id"], payload) is not None
    assert repo.notify(data["seeker_id"], payload) is None
    assert len(repo.list_notifications(data["seeker_id"])) == 1
    assert repo.unread_count(data["seeker_id"]) == 1


def test_new_vacancies_join_the_ranked_list_under_the_campaign_directives() -> None:
    from dreamjob.db.connection import insert_row, to_json, utcnow
    from dreamjob.db.repositories import opportunities as opp_repo
    from dreamjob.monitoring import watchlist

    data = seed()
    fresh_id = insert_row(
        "vacancy",
        {
            "company_id": data["company_id"],
            "title": "Analytics Engineer",
            "description": "Own the dbt project.",
            "required_skills": to_json(["dbt"]),
            "country": "BE",
            "collected_at": utcnow(),
        },
    )
    fresh = opp_repo.get_vacancy(fresh_id)

    added = watchlist.add_to_ranked_list(data["seeker_id"], data["campaign_id"], [fresh])
    assert len(added) == 1
    opportunity = opp_repo.get_opportunity(added[0], data["seeker_id"])
    assert opportunity["title"] == "Analytics Engineer"
    assert opportunity["kind"] == "vacancy"
    # The watchlist scores what it adds. It used to leave the row unscored for a
    # separate pass to pick up, which meant a freshly watched vacancy sorted to
    # the bottom of the ranked list and read as broken until someone pressed a
    # button. A new opportunity is scored when it appears (FR-401, FR-281).
    assert opportunity["score"] is not None, "a watched vacancy is scored as it is added"

    # Re-running finds it already there rather than adding it twice.
    assert watchlist.add_to_ranked_list(data["seeker_id"], data["campaign_id"], [fresh]) == []


# ---------------------------------------------------------------------------
# FR-403: the digest
# ---------------------------------------------------------------------------


def test_digest_collects_the_five_sections_and_picks_one_action() -> None:
    from dreamjob.monitoring import digest
    from dreamjob.postapp import board, reply_classifier

    data = seed()
    board.sync_sent_applications(data["seeker_id"])
    reply_id = add_reply(data, "We would like to invite you for an interview next week.")
    reply_classifier.process_reply(data["seeker_id"], reply_id, llm=None)

    built = digest.build(data["seeker_id"])
    for section in ("new_opportunities", "replies", "follow_ups_due", "watchlist_changes"):
        assert section in built.sections
    assert built.counts["replies"] == 1
    assert built.action is not None
    assert built.action.key == "answer_interview_invitation", (
        "an invitation with a date attached outranks everything else on the ladder"
    )

    stored = digest.generate(data["seeker_id"])
    assert stored["recommended_action"]["key"] == "answer_interview_invitation"
    assert "NEXT ACTION" in stored["text"]
    assert "advisory" in stored["text"]

    # One digest per seeker per period: regenerating refreshes it.
    again = digest.generate(data["seeker_id"])
    assert again["id"] == stored["id"]
    assert len(digest.repo.list_digests(data["seeker_id"])) == 1


def test_digest_falls_down_the_ladder_when_nothing_needs_an_answer() -> None:
    from dreamjob.db.connection import execute
    from dreamjob.monitoring import digest

    data = seed()
    # No cards, no replies: the highest rung left is the new opportunity.
    execute("UPDATE dispatch SET follow_up_due_at = NULL WHERE job_seeker_id = ?",
            (data["seeker_id"],))
    built = digest.build(data["seeker_id"])
    assert built.action is not None
    assert built.action.key == "review_opportunity"
    assert "advisory" in built.action.reason or "decide" in built.action.reason


def test_an_empty_digest_says_so() -> None:
    from dreamjob.db.connection import execute, insert_row, utcnow
    from dreamjob.monitoring import digest

    seeker_id = insert_row(
        "job_seeker",
        {
            "email": "quiet@example.test",
            "display_name": "Quiet Seeker",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    execute("SELECT 1")
    built = digest.build(seeker_id)
    assert built.empty is True
    assert built.action is None
    assert "Nothing new this week" in digest.render_text(built)


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------


def test_scheduler_tasks_are_due_until_they_run() -> None:
    import asyncio

    from dreamjob.monitoring import scheduler as scheduler_mod

    calls: list[str] = []

    async def fake() -> dict:
        calls.append("ran")
        return {"ok": True}

    loop = scheduler_mod.Scheduler(
        tasks=[scheduler_mod.Task("unit_test", 3600, fake, "a test task")],
        jitter_seconds=0,
    )
    status = loop.status()
    assert status["running"] is False
    assert status["tasks"][0]["due"] is True

    asyncio.run(loop.run_due())
    assert calls == ["ran"]
    assert loop.status()["tasks"][0]["due"] is False, "an hourly task is not due again at once"
    assert loop.status()["tasks"][0]["last_run"]["error"] is None

    asyncio.run(loop.run_due())
    assert calls == ["ran"], "running again inside the interval must be a no-op"


def test_a_failing_task_is_recorded_and_does_not_stop_the_others() -> None:
    import asyncio

    from dreamjob.monitoring import scheduler as scheduler_mod

    ran: list[str] = []

    async def boom() -> dict:
        raise RuntimeError("registry unreachable")

    async def fine() -> dict:
        ran.append("fine")
        return {}

    loop = scheduler_mod.Scheduler(
        tasks=[
            scheduler_mod.Task("boom", 60, boom),
            scheduler_mod.Task("fine", 60, fine),
        ],
        jitter_seconds=0,
    )
    outcomes = asyncio.run(loop.run_due())
    assert "registry unreachable" in outcomes["boom"]["error"]
    assert ran == ["fine"]
    assert loop.status()["tasks"][0]["last_run"]["error"]


def test_a_slow_enrichment_tick_does_not_block_the_event_loop(monkeypatch) -> None:
    """The 309 s stall: the sweep may block its worker, never the API loop.

    ``company_enrichment`` looks async but its leaves are not - the profile
    synthesis calls the synchronous LLM client and the signals pass is plain
    database work - so a stubbed slow call is exactly what the serving loop
    used to eat.  The heartbeat below has to keep beating throughout.
    """
    import asyncio
    import time

    from dreamjob.monitoring import scheduler as scheduler_mod
    from dreamjob.pipeline import company_enrichment

    data = seed()
    calls: list[list[str]] = []

    async def slow(company_ids: list[str], **kwargs: object) -> object:
        calls.append(list(company_ids))
        time.sleep(0.3)  # synchronous, like llm/client.py's complete_json
        return company_enrichment.EnrichmentReport(companies=len(company_ids))

    monkeypatch.setattr(company_enrichment, "enrich_companies", slow)

    async def scenario() -> int:
        beats = 0

        async def heartbeat() -> None:
            nonlocal beats
            while True:
                beats += 1
                await asyncio.sleep(0.005)

        beat = asyncio.create_task(heartbeat())
        try:
            result = await scheduler_mod._run_company_enrichment()
        finally:
            beat.cancel()
        assert result["companies"] == 1
        return beats

    beats = asyncio.run(scenario())
    assert calls == [[data["company_id"]]]
    assert beats >= 5, (
        f"the event loop beat only {beats} time(s) while the sweep ran; running the "
        "sweep on the serving loop is the 309 s stall this test exists for"
    )


def test_a_raising_task_does_not_stop_the_scheduler_loop() -> None:
    """A task that raises is retried on its next tick; the loop survives."""
    import asyncio

    from dreamjob.monitoring import scheduler as scheduler_mod

    async def boom() -> dict:
        raise RuntimeError("registry unreachable")

    async def scenario() -> None:
        loop = scheduler_mod.Scheduler(
            tasks=[scheduler_mod.Task("boom", 0, boom)],
            tick_seconds=0.05,
            jitter_seconds=0,
        )
        loop.start()
        try:
            await asyncio.sleep(0.3)
            assert loop.running, "the scheduler loop died with its task"
            assert loop.runs >= 2, "the scheduler stopped ticking after the failure"
        finally:
            await loop.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["collection", "contacts_discovery"])
def test_enrichment_defers_while_a_heavy_job_is_running(monkeypatch, kind: str) -> None:
    import asyncio

    from dreamjob.db.connection import insert_row, utcnow
    from dreamjob.monitoring import scheduler as scheduler_mod
    from dreamjob.pipeline import company_enrichment

    seed()
    insert_row("job_run", {"kind": kind, "status": "running", "created_at": utcnow()})
    called: list[str] = []

    async def never(*args: object, **kwargs: object) -> object:
        called.append("enrich")
        return company_enrichment.EnrichmentReport()

    monkeypatch.setattr(company_enrichment, "enrich_companies", never)

    result = asyncio.run(scheduler_mod._run_company_enrichment())

    assert called == [], "the sweep ran while a heavy job owned the resources"
    assert kind in result["deferred"]

    task = scheduler_mod.Task(
        "company_enrichment", 6 * 3600, scheduler_mod._run_company_enrichment
    )
    asyncio.run(scheduler_mod.Scheduler(tasks=[task], jitter_seconds=0).run_due())
    assert scheduler_mod._last_run("company_enrichment") is None, (
        "a deferred sweep must stay due, so the next tick can try again"
    )


def test_enrichment_yields_between_companies_when_a_job_starts(monkeypatch) -> None:
    import asyncio

    from dreamjob.db.connection import insert_row, utcnow
    from dreamjob.monitoring import scheduler as scheduler_mod
    from dreamjob.pipeline import company_enrichment

    data = seed()
    other_company = insert_row(
        "company",
        {"normalised_name": "contoso", "name": "Contoso", "collected_at": utcnow()},
    )
    insert_row(
        "opportunity",
        {
            "job_seeker_id": data["seeker_id"],
            "campaign_id": data["campaign_id"],
            "company_id": other_company,
            "kind": "vacancy",
            "title": "Data Engineer",
            "description": "Build pipelines.",
            "user_status": "new",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    calls: list[str] = []

    async def one_then_busy(company_ids: list[str], **kwargs: object) -> object:
        calls.append(company_ids[0])
        insert_row(
            "job_run",
            {"kind": "collection", "status": "running", "created_at": utcnow()},
        )
        return company_enrichment.EnrichmentReport(companies=1)

    monkeypatch.setattr(company_enrichment, "enrich_companies", one_then_busy)

    result = asyncio.run(scheduler_mod._run_company_enrichment())

    assert len(calls) == 1, "the sweep kept going after a heavy job started"
    assert result["companies"] == 1
    assert "collection" in result["deferred"]


def test_follow_up_reminders_notify_but_never_send() -> None:
    import asyncio

    from dreamjob.db.repositories import pipeline_cards as repo
    from dreamjob.monitoring import scheduler as scheduler_mod
    from dreamjob.postapp import board

    data = seed()
    board.sync_sent_applications(data["seeker_id"])

    result = asyncio.run(scheduler_mod._run_follow_ups())
    assert result["notifications"] == 1
    notification = repo.list_notifications(data["seeker_id"], kind="follow_up_due")[0]
    assert notification["severity"] == "action"
    assert notification["payload"]["dispatch_id"] == data["dispatch_id"]

    # The dispatch itself is untouched: sending is the seeker's act (NFR-305).
    from dreamjob.db.connection import query_one

    dispatch = query_one("SELECT * FROM dispatch WHERE id = ?", (data["dispatch_id"],))
    assert dispatch["follow_up_sent_at"] is None

    # And the reminder is raised once, not on every tick.
    assert asyncio.run(scheduler_mod._run_follow_ups())["notifications"] == 0


# ---------------------------------------------------------------------------
# The HTTP layer: authorisation and the shapes the UI actually sends
# ---------------------------------------------------------------------------


def _api_client(seeker_id: str):
    """A TestClient carrying a live session for ``seeker_id``."""
    from dreamjob.db.connection import insert_row, utcnow
    from dreamjob.main import create_app
    from dreamjob.security.crypto import hash_token
    from fastapi.testclient import TestClient

    token = secrets.token_urlsafe(24)
    insert_row(
        "session",
        {
            "job_seeker_id": seeker_id,
            "token_hash": hash_token(token),
            "expires_at": _iso(1),
            "created_at": utcnow(),
        },
    )
    client = TestClient(create_app())
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def test_a_draft_can_be_approved_unchanged() -> None:
    """Approving without edits is the normal case, so an empty body is valid."""
    from dreamjob.db.repositories import pipeline_cards as repo
    from dreamjob.postapp import board, reply_classifier

    data = seed()
    board.sync_sent_applications(data["seeker_id"])
    reply_id = add_reply(data, "We would like to invite you for an interview.")
    reply_classifier.process_reply(data["seeker_id"], reply_id, llm=None)
    draft_id = repo.list_drafts(data["seeker_id"])[0]["id"]

    client = _api_client(data["seeker_id"])
    assert client.post(f"/api/pipeline/drafts/{draft_id}/approve", json={}).status_code == 200
    assert client.post(f"/api/pipeline/drafts/{draft_id}/approve").status_code == 200

    approved = repo.get_draft(draft_id, data["seeker_id"])
    assert approved["status"] == "approved"
    # NFR-305 again, through the API this time: approval is not sending.
    assert approved["sent_at"] is None


def test_a_sent_draft_is_never_re_queued_by_approving_it_again() -> None:
    """The mail slice drains status='approved'; re-approving would send twice."""
    import pytest
    from dreamjob.db.connection import utcnow
    from dreamjob.db.repositories import pipeline_cards as repo
    from dreamjob.postapp import board, reply_classifier

    data = seed()
    board.sync_sent_applications(data["seeker_id"])
    reply_id = add_reply(data, "Could you send us your availability?")
    reply_classifier.process_reply(data["seeker_id"], reply_id, llm=None)
    draft_id = repo.list_drafts(data["seeker_id"])[0]["id"]
    reply_classifier.approve_draft(data["seeker_id"], draft_id)

    # What the mail slice does after a successful send.
    repo.update_draft(draft_id, {"status": "sent", "sent_at": utcnow()})

    with pytest.raises(reply_classifier.AlreadySent):
        reply_classifier.approve_draft(data["seeker_id"], draft_id)
    assert repo.get_draft(draft_id, data["seeker_id"])["status"] == "sent"

    client = _api_client(data["seeker_id"])
    assert client.post(f"/api/pipeline/drafts/{draft_id}/approve", json={}).status_code == 409


def test_a_card_can_be_closed_as_accepted() -> None:
    """FR-421 closes a card with an outcome, and 'accepted' is one of them."""
    from dreamjob.db.repositories import pipeline_cards as repo
    from dreamjob.postapp import board

    data = seed()
    board.sync_sent_applications(data["seeker_id"])
    card_id = repo.list_cards(data["seeker_id"])[0]["id"]

    client = _api_client(data["seeker_id"])
    response = client.post(
        f"/api/pipeline/cards/{card_id}/outcome",
        json={"outcome": "accepted", "note": "signed"},
    )
    assert response.status_code == 200, response.text

    card = repo.get_card(card_id, data["seeker_id"])
    assert card["stage"] == "closed"
    assert card["outcome"] == "accepted"

    assert (
        client.post(f"/api/pipeline/cards/{card_id}/outcome", json={"outcome": "maybe"})
    ).status_code == 422


def test_an_id_is_not_an_authorisation() -> None:
    """FR-101, FR-344: none of this slice's private rows answer to another seeker."""
    from dreamjob.db.connection import insert_row, utcnow
    from dreamjob.db.repositories import pipeline_cards as repo
    from dreamjob.monitoring import digest, watchlist
    from dreamjob.postapp import board, mock_interview, reply_classifier

    data = seed()
    owner = data["seeker_id"]
    board.sync_sent_applications(owner)
    card_id = repo.list_cards(owner)[0]["id"]
    reply_id = add_reply(data, "Can you come next Tuesday at 14:00?")
    reply_classifier.process_reply(owner, reply_id, llm=None)
    draft_id = repo.list_drafts(owner)[0]["id"]
    session = mock_interview.start(owner, opportunity_id=data["opportunity_id"], llm=None)
    watch = watchlist.add(owner, data["company_id"], campaign_id=data["campaign_id"])
    digest_id = digest.generate(owner)["id"]
    notification_id = repo.notify(owner, {"kind": "test", "title": "private", "body": "x"})

    stranger = insert_row(
        "job_seeker",
        {
            "email": "stranger@example.test",
            "display_name": "Stranger",
            "locale": "en",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    client = _api_client(stranger)
    attempts = [
        ("get", f"/api/pipeline/cards/{card_id}", None),
        ("get", f"/api/pipeline/cards/{card_id}/events", None),
        ("post", f"/api/pipeline/cards/{card_id}/stage", {"stage": "closed"}),
        ("patch", f"/api/pipeline/cards/{card_id}", {"notes": "hijacked"}),
        ("get", f"/api/pipeline/replies/{reply_id}", None),
        ("post", f"/api/pipeline/replies/{reply_id}/process", {"use_llm": False}),
        ("get", f"/api/pipeline/drafts/{draft_id}", None),
        ("patch", f"/api/pipeline/drafts/{draft_id}", {"body": "hijacked"}),
        ("post", f"/api/pipeline/drafts/{draft_id}/approve", {}),
        ("post", f"/api/pipeline/drafts/{draft_id}/discard", None),
        ("get", f"/api/pipeline/mock-interviews/{session['id']}", None),
        ("post", f"/api/pipeline/mock-interviews/{session['id']}/answer",
         {"answer": "hijacked", "use_llm": False}),
        ("get", f"/api/monitoring/digests/{digest_id}", None),
        ("patch", f"/api/monitoring/watchlist/{watch['id']}", {"check_interval_days": 30}),
        ("delete", f"/api/monitoring/watchlist/{watch['id']}", None),
        ("post", f"/api/monitoring/notifications/{notification_id}/read", None),
    ]
    leaked = []
    for method, url, body in attempts:
        call = getattr(client, method)
        response = call(url, json=body) if body is not None else call(url)
        if response.status_code < 400:
            leaked.append((method, url, response.status_code))
    assert not leaked, leaked

    # Nothing of the owner's moved.
    assert repo.get_card(card_id, owner)["stage"] == "sent"
    assert repo.get_draft(draft_id, owner)["status"] == "draft"
    assert repo.get_watch(watch["id"], owner) is not None
    assert repo.list_notifications(owner)[0]["read_at"] is None


def test_the_recommended_action_never_links_to_a_card_that_does_not_exist() -> None:
    """FR-403's one action is only trustworthy if its link opens something."""
    from dreamjob.monitoring import digest

    # A dispatch can be due a follow-up before sync_sent_applications has
    # opened its card, which is exactly when the link used to read ".../None".
    data = seed()
    built = digest.build(data["seeker_id"])
    assert built.action is not None
    assert built.action.key == "send_follow_up"
    assert built.action.url == "/pipeline/board"

    from dreamjob.postapp import board

    board.sync_sent_applications(data["seeker_id"])
    with_card = digest.build(data["seeker_id"])
    assert with_card.action is not None
    assert with_card.action.url.startswith("/pipeline/cards/")
    assert "None" not in with_card.action.url
