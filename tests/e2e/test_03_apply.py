"""End-to-end: applying — opportunities, contacts, documents, dispatch controls.

This is the half of the pipeline where the machine stops suggesting and starts
producing something a stranger will read, so the screens here carry most of the
product's promises:

* **FR-263** a speculative opening is never dressed up as an advertised
  vacancy — badge *and* styling, in every view;
* **FR-282 / FR-383** the number is explained: seven sub-scores, a written
  rationale, and a dream-job fit meter kept separate from the overall score;
* **FR-284** the seeker's order is the seeker's — a recalculation writes scores
  and nothing else;
* **NFR-302** an objection blocks an address permanently, for everyone;
* **FR-321** four artefacts per opportunity, two of which are never sent;
* **FR-322 / NFR-206** nothing may be approved until the checks pass, or until
  the override is on the record;
* **FR-324** no approval, single or bulk, happens without a summary of what
  goes to whom;
* **FR-325 / NFR-204** the two mail backends, neither of which can send on this
  installation — which is a state to *report*, not a screen to break.

Nothing here sends mail. The suite never touches ``POST /api/mail/send``.

**What is seeded, and why.**  A campaign that has actually collected vacancies
is the job of the discovery suite and of a live network.  This one starts from
a fixture that creates the account, the profile version, the directive set and
the campaign *through the API*, and then writes one company, one vacancy, two
opportunities and three contacts *straight into the database*, because the API
exposes no route that creates a company or an opportunity — synthesis derives
them from collected material.  The fixture is deliberately loud about that: it
is set-up, not evidence.  Everything the suite actually asserts is driven
through the interface.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from harness import (
    expect_no_console_errors,
    failed_requests,
    narrate,
    open_screen,
    screenshot,
    sign_in,
    step,
    wait_for_ready,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "backend") not in sys.path:  # so the fixture can seed the db
    sys.path.insert(0, str(REPO_ROOT / "backend"))

# The two opportunities the whole suite works on. They are named rather than
# found by position so a failure says which row it was looking at.
VACANCY_TITLE = "Lead AI Engineer, Document Intelligence"
SPECULATIVE_TITLE = "Head of AI Platform (speculative)"

#: Two calls the Contacts screen makes and swallows. Both are defects, both are
#: asserted on by ``test_the_ranked_contacts_view_shows_its_validation``, and
#: both are named here so that a *different* console error on that screen still
#: fails the tests that only pass through it:
#:   /api/companies?limit=200      422 — the route caps `limit` at 100
#:   /api/contacts/retention/due   403 — the route is administrator-only
CONTACTS_KNOWN_FAILURES = ("/api/companies?limit=200", "/api/contacts/retention/due")

#: The browser's own log shipper (NFR-701). It belongs to no screen, and its
#: endpoint rate-limits per client IP rather than per session, so any second
#: browser on the same machine — another e2e suite, a developer's own tab —
#: shares the bucket and starts collecting 429s. A screen is not broken
#: because its telemetry was throttled, so it is excluded by name.
TELEMETRY = ("/api/logs/client",)

#: Generation is deterministic here (see the fixture note on CR-410 consent),
#: but it still renders four documents; give it room without hiding a hang.
GENERATE_TIMEOUT_MS = 180_000
#: A scoring pass calls the model once per opportunity.
RECALCULATE_TIMEOUT_MS = 300_000


# ---------------------------------------------------------------------------
# Set-up: the API for what has a route, the database for what has none
# ---------------------------------------------------------------------------


def _call(
    api_url: str,
    method: str,
    path: str,
    body: Any = None,
    token: str | None = None,
    timeout: float = 60.0,
) -> Any:
    """One JSON call to the running API, as the set-up client.

    The bearer form rather than the cookie, because NFR-202 binds a session to
    the client that created it and this client is not the browser.
    """
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{api_url}/api{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "dreamjob-e2e-setup")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:  # pragma: no cover - a broken fixture
        detail = exc.read().decode(errors="replace")[:400]
        raise AssertionError(f"Set-up call {method} {path} failed ({exc.code}): {detail}") from exc


def _profile_sections(persona: Any) -> dict[str, Any]:
    """The persona as an FR-102 profile, in the shape the CV generator reads."""
    identity = persona.get("identity", default={}) or {}
    experience = []
    for entry in persona.get("experience", default=[]) or []:
        experience.append(
            {
                "company": entry.get("employer"),
                "title": entry.get("title"),
                "start": entry.get("start"),
                "end": entry.get("end"),
                "current": bool(entry.get("current")),
                "location": entry.get("location"),
                "description": entry.get("summary") or "",
            }
        )
    education = [
        {
            "school": entry.get("school"),
            "degree": entry.get("degree"),
            "field": entry.get("field"),
            "start_year": entry.get("start_year"),
            "end_year": entry.get("end_year"),
        }
        for entry in persona.get("education", default=[]) or []
    ]
    return {
        "contact": {
            "name": identity.get("name") or persona.display_name,
            "headline": identity.get("headline"),
            "location": identity.get("location_short"),
            "email": identity.get("email"),
            "phone": identity.get("phone"),
            "linkedin_url": identity.get("linkedin_url"),
            "websites": [],
        },
        "summary": persona.get("summary", default="") or "",
        "top_skills": list(persona.get("top_skills", default=[]) or [])[:8],
        "languages": persona.get("languages", default=[]) or [],
        "experience": experience,
        "education": education,
        "certifications": persona.get("certifications", default=[]) or [],
        "publications": [],
        "projects": [],
        "honors": [],
        "courses": [],
        "other": {},
    }


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _seed_knowledge_and_opportunities(seeker_id: str, campaign_id: str, tag: str) -> dict[str, str]:
    """Write the company, the vacancy, two opportunities and three contacts.

    Straight through ``db.repositories`` — **not** through the API, because
    there is no route that creates any of them: a company arrives from a
    profiling pass and an opportunity from the synthesis pass, both of which
    need a campaign that has been out on the network.  Doing it here keeps the
    dependency honest and visible instead of hiding it behind a mocked
    campaign run.

    Everything is tagged with the run's timestamp so that one run's objection
    (which NFR-302 makes permanent and global) can never block another run's
    address.
    """
    from dreamjob.db.connection import insert_row, utcnow  # noqa: PLC0415

    domain = f"havenstad-{tag}.example"
    company_id = insert_row(
        "company",
        {
            "name": f"Havenstad Analytics (e2e {tag})",
            "normalised_name": f"havenstad analytics e2e {tag}",
            "domain": domain,
            "country": "BE",
            "jurisdiction": "BE",
            "business_summary": (
                "Document-intelligence software for port and logistics operators in the "
                "Benelux. Fictional company, written for the Dream Job end-to-end suite."
            ),
            "sector_codes": ["62010"],
            "size_fte": 180,
            "size_band": "b50_250",
            "stage": "scaleup",
            "ownership": "founder_led",
            "trajectory": "growing",
            "locations": [{"city": "Antwerp", "country": "BE"}],
            "structure": {"departments": ["engineering", "data", "operations"]},
            "key_people": [{"name": "Marieke De Smet", "role": "Head of Engineering"}],
            "careers_url": f"https://{domain}/careers",
            "collected_at": utcnow(),
            "source": "e2e-fixture",
            "access_method": "manual",
            "confidence": 0.9,
        },
    )

    vacancy_id = insert_row(
        "vacancy",
        {
            "company_id": company_id,
            "company_name_raw": f"Havenstad Analytics (e2e {tag})",
            "title": VACANCY_TITLE,
            "function_family": "data & analytics",
            "seniority": "senior",
            "description": (
                "Lead the team that builds our document-understanding platform: retrieval, "
                "evaluation and the production plumbing around it. Python, SQL, cloud."
            ),
            "required_skills": ["Python", "SQL", "LLM"],
            "location": "Antwerp, Belgium",
            "country": "BE",
            "work_arrangement": "hybrid",
            "contract_type": "permanent",
            "posted_at": _iso(9),
            "source_url": f"https://{domain}/careers/lead-ai-engineer",
            "source_adapter": "e2e-fixture",
            "language": "en",
            "collected_at": utcnow(),
            "access_method": "manual",
            "confidence": 0.9,
        },
    )

    def fit_detail(score: float, met: list[str], partial: list[str], violated: list[str]) -> dict:
        return {
            "score": score,
            "counts": {"met": len(met), "partial": len(partial), "violated": len(violated)},
            "met": [{"id": f"m{i}", "criterion": c} for i, c in enumerate(met)],
            "partially_met": [{"id": f"p{i}", "criterion": c} for i, c in enumerate(partial)],
            "violated": [{"id": f"v{i}", "criterion": c} for i, c in enumerate(violated)],
            "unknown": [],
            "note": "Assessed against the dream-job model recorded for this account.",
        }

    common = {
        "job_seeker_id": seeker_id,
        "campaign_id": campaign_id,
        "company_id": company_id,
        "language": "en",
        "country": "BE",
        "location": "Antwerp, Belgium",
        "work_arrangement": "hybrid",
        "contract_type": "permanent",
        "function_family": "data & analytics",
        "seniority": "senior",
        "comp_currency": "EUR",
        "scored_at": utcnow(),
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }

    vacancy_opportunity = insert_row(
        "opportunity",
        {
            **common,
            "vacancy_id": vacancy_id,
            "kind": "vacancy",
            "title": VACANCY_TITLE,
            "description": (
                "Lead the team that builds our document-understanding platform: retrieval, "
                "evaluation and the production plumbing around it."
            ),
            "required_skills": ["Python", "SQL", "LLM"],
            "posted_at": _iso(9),
            "source_url": f"https://{domain}/careers/lead-ai-engineer",
            "comp_min": 82000,
            "comp_max": 98000,
            "comp_is_stated": 1,
            "comp_confidence": 0.8,
            "score": 82,
            "score_profile_fit": 88,
            "score_dream_fit": 61,
            "score_directive_fit": 79,
            "score_company": 74,
            "score_compensation": 70,
            "score_plausibility": 95,
            "score_reachability": 66,
            "rationale": (
                "An advertised role that matches the platform work in your last two positions, "
                "at a company of the size you asked for and inside your commute. The dream-job "
                "fit is lower than the overall score because the team is smaller than the one "
                "you want to lead."
            ),
            "dream_fit_detail": fit_detail(
                61,
                ["Technical leadership of a small team", "LLM systems in production"],
                ["Ownership of the architecture"],
                ["Team of five or more engineers"],
            ),
            "timing_flag": "apply_now",
        },
    )

    speculative_opportunity = insert_row(
        "opportunity",
        {
            **common,
            "kind": "speculative",
            "title": SPECULATIVE_TITLE,
            "description": (
                "No such role is advertised. The company's hiring pattern, its recent funding "
                "and the gap in its engineering structure suggest one could exist."
            ),
            "speculative_rationale": (
                "Three data-engineering hires in six months, no platform lead in the "
                "department map, and a public commitment to move document processing in-house."
            ),
            "plausibility": 0.62,
            "comp_min": 90000,
            "comp_max": 110000,
            "comp_is_stated": 0,
            "comp_confidence": 0.4,
            "score": 71,
            "score_profile_fit": 80,
            "score_dream_fit": 84,
            "score_directive_fit": 72,
            "score_company": 74,
            "score_compensation": 76,
            "score_plausibility": 62,
            "score_reachability": 55,
            "rationale": (
                "A speculative opening: nothing is advertised. It scores lower overall than the "
                "vacancy because plausibility and reachability drag it down, but it is much "
                "closer to the job you described wanting."
            ),
            "dream_fit_detail": fit_detail(
                84,
                [
                    "Technical leadership of a small team",
                    "LLM systems in production",
                    "Ownership of the architecture",
                ],
                ["Team of five or more engineers"],
                [],
            ),
        },
    )

    contacts = {
        "hiring_manager": insert_row(
            "contact",
            {
                "company_id": company_id,
                "full_name": "Marieke De Smet",
                "role_title": "Head of Engineering",
                "department": "engineering",
                "email": f"marieke.desmet@{domain}",
                "email_source_method": "website",
                "email_validation": "valid",
                "email_validated_at": utcnow(),
                "is_generic_mailbox": 0,
                "source": f"https://{domain}/about",
                "access_method": "http",
                "shareable": 1,
                "collected_at": utcnow(),
                "confidence": 0.9,
            },
        ),
        "talent": insert_row(
            "contact",
            {
                "company_id": company_id,
                "full_name": "Tom Peeters",
                "role_title": "Talent Acquisition Partner",
                "department": "people",
                "email": f"tom.peeters@{domain}",
                "email_source_method": "pattern_inference",
                "email_validation": "risky",
                "email_validated_at": utcnow(),
                "is_generic_mailbox": 0,
                "source": "inferred from the domain pattern",
                "access_method": "http",
                "shareable": 1,
                "collected_at": utcnow(),
                "confidence": 0.6,
            },
        ),
        "mailbox": insert_row(
            "contact",
            {
                "company_id": company_id,
                "full_name": None,
                "role_title": "Careers mailbox",
                "email": f"careers@{domain}",
                "email_source_method": "website",
                "email_validation": "unknown",
                "is_generic_mailbox": 1,
                "source": f"https://{domain}/careers",
                "access_method": "http",
                "shareable": 1,
                "collected_at": utcnow(),
                "confidence": 0.5,
            },
        ),
    }

    return {
        "company_id": company_id,
        "domain": domain,
        "vacancy_id": vacancy_id,
        "vacancy_opportunity_id": vacancy_opportunity,
        "speculative_opportunity_id": speculative_opportunity,
        **contacts,
    }


@pytest.fixture(scope="session")
def workspace(api_url: str, persona: Any, app_running: dict) -> dict[str, Any]:
    """One account with something to apply *to*, shared by every test here.

    Session-scoped on purpose: a package generated by the generation test is
    the subject of the approval tests, and re-registering for each of them
    would either hide that or triple the set-up.  Each test still gets a fresh
    browser context and signs in for itself.
    """
    tag = datetime.now(UTC).strftime("%H%M%S")
    email = persona.fresh_email("apply")
    password = persona.password

    account = _call(
        api_url,
        "POST",
        "/auth/register",
        {"email": email, "display_name": persona.display_name, "password": password},
    )
    token = account["session"]["token"]
    seeker_id = account["id"]
    narrate(f"  · set-up: registered {email} through the API (suite 01 covers registering)")

    profile = _call(
        api_url,
        "PUT",
        "/profile/",
        {
            "sections": _profile_sections(persona),
            "dream_job_statement": persona.get("dream_job.statement", default="") or "",
        },
        token=token,
    )

    directives = _call(
        api_url,
        "POST",
        "/directives/",
        {
            "name": "Benelux AI leadership",
            "job_content": {
                "target_titles": ["Lead AI Engineer", "Head of AI Platform"],
                "function_families": ["data & analytics"],
                "seniority_min": "senior",
                "must_have_skills": ["Python", "SQL"],
            },
            "company_type": {"size_bands": ["b50_250"], "stages": ["scaleup"]},
            "location": {"countries": ["BE", "NL"]},
            "work_arrangement": {"arrangements": ["hybrid", "remote"]},
        },
        token=token,
    )

    campaign = _call(
        api_url,
        "POST",
        "/campaigns",
        {
            "name": f"Apply suite {tag}",
            "directive_set_id": directives["id"],
            "profile_version_id": profile["id"],
        },
        token=token,
    )
    narrate("  · set-up: profile version, directive set and campaign created through the API")

    seeded = _seed_knowledge_and_opportunities(seeker_id, campaign["id"], tag)
    narrate(
        "  · set-up: one company, one vacancy, two opportunities and three contacts written "
        "directly to the database — the API has no route that creates them"
    )

    return {
        "tag": tag,
        "email": email,
        "password": password,
        "token": token,
        "seeker_id": seeker_id,
        "campaign_id": campaign["id"],
        "campaign_name": campaign["name"],
        "profile_version_id": profile["id"],
        **seeded,
    }


@pytest.fixture
def signed_in(page: Any, workspace: dict[str, Any]) -> dict[str, Any]:
    """This test's browser, signed in as the suite's job seeker."""
    sign_in(page, workspace["email"], workspace["password"])
    return workspace


# ---------------------------------------------------------------------------
# Small helpers shared by the tests
# ---------------------------------------------------------------------------


def opportunity_row(page: Any, title: str) -> Any:
    """The ranked-list row for one opportunity, found by its title link."""
    return page.locator(".opp-row").filter(has=page.get_by_role("link", name=title, exact=True))


def help_drawer_opens_with_content(page: Any, expected_title: str) -> None:
    """The "? Help" control opens the drawer, and the drawer has this screen in it."""
    page.locator("header.topbar").get_by_role("button", name="Help", exact=True).click()
    drawer = page.get_by_role("dialog", name="Help")
    drawer.wait_for(state="visible")
    heading = drawer.get_by_role("heading", level=3).first.inner_text().strip()
    assert heading == expected_title, (
        f"the help drawer opened on {heading!r}, not on this screen's own help "
        f"({expected_title!r})"
    )
    steps = drawer.locator("ol.help-steps li")
    assert steps.count() >= 3, (
        f"the help for {expected_title!r} lists {steps.count()} steps; a screen this "
        "dense needs more than a title"
    )
    assert drawer.get_by_role("heading", name="Glossary").is_visible()
    screenshot(page, f"help drawer — {expected_title}")
    page.keyboard.press("Escape")
    drawer.wait_for(state="hidden")


def no_screen_level_failure(page: Any, screen: str, *, allow: tuple[str, ...] = ()) -> None:
    """Fail with the network evidence when a screen quietly rendered nothing.

    An empty state is a pass; an empty state caused by a 4xx the screen
    swallowed is not, and this is the difference between the two.
    """
    allow = tuple(allow) + TELEMETRY
    problems = [
        request
        for request in failed_requests(page)
        if not any(fragment in request["request_url"] for fragment in allow)
    ]
    if problems:
        lines = "\n".join(
            f"  {r['method']} {r['request_url']} -> {r['status'] or r['reason']}"
            f" (during {r['step'] or 'no step'})"
            for r in problems
        )
        raise AssertionError(
            f"{screen} made {len(problems)} call(s) that did not succeed, so whatever it "
            f"rendered cannot be trusted:\n{lines}"
        )


def set_shortlist(page: Any, titles: set[str]) -> None:
    """Make the server-side shortlist exactly ``titles`` (FR-284)."""
    for title in (VACANCY_TITLE, SPECULATIVE_TITLE):
        box = page.get_by_role("checkbox", name=f"Select {title}", exact=True)
        wanted = title in titles
        if box.is_checked() != wanted:
            box.click()
    page.get_by_text(f"{len(titles)} on the shortlist", exact=True).wait_for(state="visible")


def generate_package_through_the_ui(page: Any, title: str) -> None:
    """Select exactly one opportunity in the ranked list and generate for it."""
    open_screen(page, "Opportunities")
    set_shortlist(page, {title})
    page.get_by_role("button", name="Generate applications for the selected").click()
    modal = page.locator(".modal")
    modal.wait_for(state="visible")
    assert title in modal.inner_text(), "the confirmation did not name what it will write for"
    modal.get_by_role("button", name="Generate", exact=True).click()
    page.wait_for_url("**/applications", timeout=GENERATE_TIMEOUT_MS)
    wait_for_ready(page, timeout=GENERATE_TIMEOUT_MS)
    page.get_by_role("button", name=title).first.wait_for(
        state="visible", timeout=GENERATE_TIMEOUT_MS
    )


def html5_drag(page: Any, handle: Any, target: Any) -> None:
    """Drag one row onto another the way the browser does it.

    The ranked list uses HTML5 drag-and-drop, and a mouse-driven drag does not
    start one on this handle, so the three events the row actually listens for
    are dispatched with a single shared ``DataTransfer`` — which is exactly
    what a real drag carries from ``dragstart`` through to ``drop``.
    """
    transfer = page.evaluate_handle("() => new DataTransfer()")
    handle.dispatch_event("dragstart", {"dataTransfer": transfer})
    target.dispatch_event("dragover", {"dataTransfer": transfer})
    target.dispatch_event("drop", {"dataTransfer": transfer})


def package_exists(page: Any, title: str) -> bool:
    """True when the Applications list already holds a package for ``title``."""
    open_screen(page, "Applications")
    try:
        page.get_by_role("button", name=title).first.wait_for(state="visible", timeout=4000)
    except PlaywrightTimeoutError:
        return False
    return True


def open_package(page: Any, title: str) -> None:
    page.get_by_role("button", name=title).first.click()
    page.get_by_role("heading", name=title).wait_for(state="visible")


def package_tab(page: Any, label: str) -> Any:
    """One of the five review tabs.

    Scoped to the tab bar rather than matched by name alone: the Checks tab
    grows a count badge as soon as the checker has something to say, so its
    accessible name is "Checks" on a clean package and "Checks 3" on a dirty
    one, and "Re-run the checks" sits on the same screen.
    """
    return page.locator(".tabs").get_by_role("button", name=label)


# ---------------------------------------------------------------------------
# Opportunities (FR-263, FR-282, FR-283, FR-284, FR-383)
# ---------------------------------------------------------------------------


def test_the_ranked_list_explains_its_own_ranking(page, signed_in, log_tail) -> None:
    """FR-282/FR-383: sub-scores, a rationale, and two meters that are not one."""
    with step(page, "Open the ranked list of opportunities"):
        open_screen(page, "Opportunities")
        assert page.get_by_role("heading", name="Opportunities").first.is_visible()
        # The screen's own content, not an error boundary and not a spinner.
        assert page.get_by_text("Scores are advisory").first.is_visible()
        assert page.get_by_role("link", name=VACANCY_TITLE, exact=True).is_visible()
        assert page.get_by_role("link", name=SPECULATIVE_TITLE, exact=True).is_visible()
        no_screen_level_failure(page, "Opportunities")

    with step(page, "Read the rationale written next to each score (FR-282)"):
        for title in (VACANCY_TITLE, SPECULATIVE_TITLE):
            rationale = opportunity_row(page, title).locator(".opp-rationale")
            assert rationale.count() == 1, f"{title} carries no rationale"
            text = rationale.inner_text().strip()
            assert len(text) > 60, f"the rationale for {title} is too short to explain anything"

    with step(page, "Check the dream-job fit meter is separate from the overall score (FR-383)"):
        row = opportunity_row(page, VACANCY_TITLE)
        assert row.get_by_text("Dream fit", exact=True).is_visible()
        values = row.locator(".meter .meter-value").all_inner_texts()
        assert len(values) == 2, (
            f"expected two meters on the row — the overall score and the dream-job fit — "
            f"found {len(values)}: {values}"
        )
        overall, dream = (int(v) for v in values)
        assert overall == 82 and dream == 61, (
            f"the two meters read {overall} and {dream}; the seeded opportunity scores 82 "
            "overall and 61 on dream-job fit, so one meter is showing the other's number"
        )
        assert row.get_by_text("2 met", exact=True).is_visible()
        assert row.get_by_text("1 violated", exact=True).is_visible()

    with step(page, "Open 'Why this rank?' for the seven sub-scores (FR-282)"):
        opportunity_row(page, VACANCY_TITLE).get_by_role(
            "button", name="Why this rank?"
        ).click()
        breakdown = page.locator(".subscores").first
        breakdown.wait_for(state="visible")
        labels = breakdown.locator(".label").all_inner_texts()
        for expected in (
            "Profile fit",
            "Dream-job fit",
            "Directive fit",
            "Company",
            "Compensation",
            "Plausibility",
            "Reachability",
        ):
            assert expected in labels, f"the breakdown does not show {expected!r}: {labels}"
        assert page.get_by_role("heading", name="Dream-job fit").first.is_visible()
        assert page.get_by_text("Team of five or more engineers").first.is_visible()

    with step(page, "Check the help for this screen"):
        help_drawer_opens_with_content(page, "Opportunities")

    log_tail.assert_contains(
        "GET /api/opportunities",
        what="the ranked list was served and logged (NFR-701)",
    )
    expect_no_console_errors(page, ignore=TELEMETRY)


def test_a_speculative_opening_never_looks_like_a_vacancy(page, signed_in) -> None:
    """FR-263: the badge *and* the styling, because a badge alone is missable."""
    with step(page, "Open the ranked list"):
        open_screen(page, "Opportunities")
        page.get_by_role("link", name=SPECULATIVE_TITLE, exact=True).wait_for(state="visible")

    with step(page, "Read the badge on each kind of row (FR-263)"):
        vacancy = opportunity_row(page, VACANCY_TITLE)
        speculative = opportunity_row(page, SPECULATIVE_TITLE)
        assert vacancy.get_by_text("Advertised vacancy", exact=True).is_visible()
        assert speculative.get_by_text("Speculative opening", exact=True).is_visible()
        assert speculative.get_by_text("Advertised vacancy").count() == 0, (
            "the speculative row also claims to be an advertised vacancy"
        )

    with step(page, "Check the two rows are visually distinct, not only labelled (FR-263)"):
        style = "el => { const s = getComputedStyle(el); return [s.borderLeftWidth, s.borderLeftColor] }"
        vacancy_style = vacancy.evaluate(style)
        speculative_style = speculative.evaluate(style)
        assert speculative_style != vacancy_style, (
            "an advertised vacancy and a speculative opening are drawn identically "
            f"({vacancy_style}); FR-263 asks for a distinction that survives a glance"
        )
        assert speculative.evaluate("el => el.className").find("speculative") >= 0

    with step(page, "Open the breakdown and read the disclosure note"):
        speculative.get_by_role("button", name="Why this rank?").click()
        page.locator(".subscores").first.wait_for(state="visible")
        note = page.locator(".opp-row.speculative + div p.small.muted").first
        assert note.count() == 1 and note.inner_text().strip(), (
            "the speculative row's breakdown carries no disclosure note (FR-263)"
        )
        narrate(f"  · disclosure note: {note.inner_text().strip()[:120]}")

    with step(page, "Filter the list down to speculative openings only"):
        page.get_by_role("combobox").filter(
            has=page.get_by_role("option", name="Advertised vacancies only")
        ).select_option("speculative")
        # The list blanks while it refetches, so wait for what should be there
        # rather than only for what should be gone.
        page.get_by_role("link", name=VACANCY_TITLE, exact=True).wait_for(state="hidden")
        page.get_by_role("link", name=SPECULATIVE_TITLE, exact=True).wait_for(state="visible")
        assert page.locator(".opp-row").count() == 1, (
            "filtering to speculative openings left the advertised vacancy in the list"
        )
        page.get_by_role("button", name="Clear filters").click()
        page.get_by_role("link", name=VACANCY_TITLE, exact=True).wait_for(state="visible")

    expect_no_console_errors(page, ignore=TELEMETRY)


@pytest.mark.llm
def test_my_manual_order_survives_a_recalculation(page, signed_in, log_tail) -> None:
    """FR-284: pinning and hand-ordering belong to the seeker, not to the scorer.

    Marked ``llm``: the recalculation is a real scoring pass, and the scoring
    pass calls the model once per opportunity.
    """
    campaign_name = signed_in["campaign_name"]

    with step(page, "Open the ranked list and pick the campaign hand-ordering needs"):
        open_screen(page, "Opportunities")
        campaign_select = page.get_by_role("combobox").filter(
            has=page.get_by_role("option", name="Every campaign")
        )
        campaign_select.select_option(label=f"{campaign_name} · draft")
        page.get_by_text("Drag the ⠿ handle to place a row by hand.").wait_for(state="visible")

    with step(page, "Pin the speculative opening, and watch it climb"):
        assert page.locator(".opp-title").all_inner_texts()[0] == VACANCY_TITLE, (
            "the higher-scoring vacancy should lead the list before anything is pinned"
        )
        opportunity_row(page, SPECULATIVE_TITLE).get_by_role("button", name="Pin").click()
        opportunity_row(page, SPECULATIVE_TITLE).get_by_text("Pinned", exact=True).wait_for(
            state="visible"
        )
        # The row is updated in place rather than re-sorted, so the promotion
        # only shows on the next load of the list — which is where the server's
        # ordering is the one being read.
        page.reload()
        wait_for_ready(page)
        campaign_select = page.get_by_role("combobox").filter(
            has=page.get_by_role("option", name="Every campaign")
        )
        campaign_select.select_option(label=f"{campaign_name} · draft")
        page.wait_for_function(
            "t => document.querySelector('.opp-title')?.textContent.trim() === t",
            arg=SPECULATIVE_TITLE,
        )

    with step(page, "Drag the vacancy back above it: the hand beats the pin (FR-284)"):
        html5_drag(
            page,
            opportunity_row(page, VACANCY_TITLE).locator(".drag-handle"),
            opportunity_row(page, SPECULATIVE_TITLE),
        )
        # The confirmation is the server's own sentence about what it just did.
        page.get_by_text(
            "manual order overrides the computed order and survives recalculation"
        ).wait_for(state="visible", timeout=20_000)
        opportunity_row(page, VACANCY_TITLE).get_by_text(
            "Your position #1", exact=True
        ).wait_for(state="visible")

    with step(page, "Confirm the order on a fresh load of the screen"):
        page.reload()
        wait_for_ready(page)
        titles = page.locator(".opp-title").all_inner_texts()
        assert titles[0] == VACANCY_TITLE, (
            f"after a reload the hand-placed row is not first: {titles}"
        )

    with step(page, "Recalculate every score for this campaign"):
        campaign_select = page.get_by_role("combobox").filter(
            has=page.get_by_role("option", name="Every campaign")
        )
        campaign_select.select_option(label=f"{campaign_name} · draft")
        page.get_by_role("button", name="Recalculate scores").click()
        page.get_by_text("Recalculating the scores.").wait_for(state="visible")
        assert page.get_by_text(
            "Your manual order, pins, tags and selections are not touched by a recalculation"
        ).is_visible()

    with step(page, "Wait for the pass to finish and assert the manual order survived (FR-284)"):
        page.get_by_text("Scores refreshed.").wait_for(
            state="visible", timeout=RECALCULATE_TIMEOUT_MS
        )
        titles = page.locator(".opp-title").all_inner_texts()
        assert titles[0] == VACANCY_TITLE, (
            "the recalculation moved the row the job seeker placed by hand — FR-284 says a "
            f"scoring pass writes scores and nothing else. Order is now {titles}"
        )
        assert opportunity_row(page, VACANCY_TITLE).get_by_text(
            "Your position #1", exact=True
        ).is_visible()
        assert opportunity_row(page, SPECULATIVE_TITLE).get_by_text(
            "Pinned", exact=True
        ).is_visible(), "the recalculation dropped the pin"
        scores = [
            int(v)
            for v in page.locator(".opp-row .meter .meter-value").all_inner_texts()
        ]
        narrate(f"  · scores after the pass: {scores}")

    log_tail.assert_contains(
        "POST /api/opportunities/reorder",
        what="the seeker's own ordering was recorded (NFR-701)",
    )
    expect_no_console_errors(page, ignore=TELEMETRY)


# ---------------------------------------------------------------------------
# Applications (FR-321, FR-322, FR-324, NFR-206)
# ---------------------------------------------------------------------------


def test_generating_a_package_produces_four_artefacts(page, signed_in, log_tail) -> None:
    """FR-321: a CV, a briefing, a motivation document and an email — and two
    of the four are never sent."""
    with step(page, "Generate an application package for the advertised vacancy"):
        generate_package_through_the_ui(page, VACANCY_TITLE)
        assert page.get_by_role("heading", name=VACANCY_TITLE).is_visible()
        no_screen_level_failure(page, "Applications")

    with step(page, "Check the package header names the recipient and the source data"):
        assert page.get_by_text("Recipient", exact=True).is_visible()
        assert page.get_by_text(f"marieke.desmet@{signed_in['domain']}").is_visible()
        assert page.get_by_text("profile version").is_visible()
        # NFR-104: this account granted no CR-410 consent, so the documents are
        # written without the model — and the screen has to say so.
        degradation = page.get_by_text("Written without the model:")
        assert degradation.is_visible(), (
            "the package was generated without an LLM (no CR-410 consent on this account) "
            "but the screen does not say so — NFR-104 asks for the degradation to be named"
        )
        narrate(f"  · degraded as designed: {degradation.inner_text().strip()[:160]}")

    with step(page, "The email tab: the one artefact a recipient reads (FR-321)"):
        package_tab(page, "Email").click()
        subject = page.locator("input[type='text']").first
        body = page.locator("textarea.apl-editor")
        assert subject.input_value().strip(), "the introduction email has no subject"
        assert len(body.input_value().strip()) > 120, "the introduction email is near-empty"
        attached = page.get_by_text("Attached:").first.inner_text()
        assert ".pdf" in attached, f"nothing is attached to the message: {attached!r}"
        assert "briefing" not in attached.lower() and "motivation" not in attached.lower(), (
            f"FR-321 forbids attaching the seeker-only documents, but: {attached!r}"
        )

    with step(page, "The CV tab: the only document that is attached"):
        package_tab(page, "CV").click()
        assert page.get_by_text("The only document attached to the email.").is_visible()
        assert page.get_by_role("button", name="Download PDF").is_enabled()
        assert page.get_by_role("button", name="Download DOCX").is_enabled()

    for tab, title in (
        ("Briefing", "The briefing"),
        ("Motivation", "The motivation document"),
    ):
        with step(page, f"The {tab.lower()} tab is labelled as never sent (FR-321)"):
            package_tab(page, tab).click()
            assert page.get_by_text("This document is never sent").is_visible()
            notice = page.locator(".alert").filter(
                has_text="This document is never sent"
            ).first.inner_text()
            assert "not attached to the introduction email" in notice, notice
            assert "no recipient ever sees it" in notice, notice
            assert page.get_by_role("heading", name=title).count() >= 0
            assert page.get_by_role("button", name="Download PDF").is_enabled(), (
                f"{title} was not generated, so FR-321's four artefacts are only three"
            )

    with step(page, "The checks tab shows the consistency report (FR-322, NFR-206)"):
        package_tab(page, "Checks").click()
        assert page.get_by_text("Factual consistency").is_visible()
        assert page.get_by_text("Leak scan").first.is_visible()
        assert page.get_by_text("claims checked").is_visible()
        report_line = page.locator(".row.row-wrap.small.muted").filter(
            has_text="claims checked"
        ).first.inner_text()
        assert "deterministic checks only" in report_line, (
            f"the checks report does not say how it was run: {report_line!r}"
        )
        assert page.get_by_text("Claims in the documents").is_visible()
        assert page.get_by_text("Leak scan", exact=True).count() >= 1
        narrate(f"  · consistency report: {' '.join(report_line.split())}")

    with step(page, "Check the help for this screen"):
        help_drawer_opens_with_content(page, "Applications")

    log_tail.assert_contains(
        "POST /api/applications/generate",
        what="the generation request was logged (NFR-701)",
    )
    expect_no_console_errors(page, ignore=TELEMETRY)


def test_a_package_that_fails_its_checks_cannot_be_approved(page, signed_in, log_tail) -> None:
    """FR-322: a claim the profile does not support blocks the approval."""
    with step(page, "Open the package for the advertised vacancy"):
        if not package_exists(page, VACANCY_TITLE):
            generate_package_through_the_ui(page, VACANCY_TITLE)
        open_package(page, VACANCY_TITLE)

    with step(page, "Write a claim the profile cannot support into the email"):
        package_tab(page, "Email").click()
        body = page.locator("textarea.apl-editor")
        original = body.input_value()
        body.fill(
            original
            + "\n\nFor the record: I personally led a team of 412 engineers at Northwind "
            "Aerospace and cut latency by 97% across 38 countries."
        )
        page.get_by_role("button", name="Save the email").click()
        # The save re-runs the checks server-side and replaces the package; the
        # "Undo my edits" control is the tell that the editor is clean again.
        page.get_by_role("button", name="Undo my edits").wait_for(state="hidden", timeout=60_000)
        package_tab(page, "Checks").click()

    with step(page, "The checks tab names the unsupported claims"):
        detail = page.locator(".apl-layout > div").nth(1)
        detail.get_by_text("claims checked").wait_for(state="visible")
        # The second card in the detail pane is the tab bar and its panel; the
        # first row inside it is the checks panel's own verdict line.
        verdict = " ".join(
            detail.locator(".card").nth(1).locator(".row.row-wrap").first.inner_text().split()
        )
        narrate(f"  · verdict after the edit: {verdict}")
        assert "Claims supported" not in verdict, (
            "the checker accepted 'a team of 412 engineers at Northwind Aerospace' as "
            f"supported by the profile (FR-322): {verdict}"
        )
        severities = detail.locator("table td .badge").all_inner_texts()
        assert "high" in severities, (
            "the unsupported figures were not raised to a blocking severity; the checks "
            f"tab shows {severities or 'no findings at all'}"
        )
        assert detail.get_by_text("Northwind Aerospace").count() >= 1, (
            "the report does not quote the claim it objected to, so a reader cannot act on it"
        )
        screenshot(page, "checks tab after an unsupported claim")

    with step(page, "Try to approve it, and be refused (FR-322)"):
        approve = page.get_by_role("button", name="Approve for dispatch")
        if not approve.is_enabled():
            # A hard blocker: the button itself is closed, with its reason.
            reason = page.get_by_text("Cannot be approved:").inner_text()
            narrate(f"  · approval refused outright: {' '.join(reason.split())}")
            assert reason.strip() != "Cannot be approved:", "refused without saying why"
        else:
            warning = page.get_by_text("Approving this one will ask you to record why:")
            assert warning.is_visible(), (
                "a package whose claims are unsupported offers approval with no warning"
            )
            narrate(f"  · approval warned about first: {' '.join(warning.inner_text().split())}")
            approve.click()
            modal = page.locator(".modal")
            modal.wait_for(state="visible")
            confirm = modal.get_by_role("button", name="Approve these 1")
            assert not confirm.is_enabled(), (
                "FR-322 lets a failed consistency check be overridden *on the record*; the "
                "modal accepted the approval without a reason"
            )
            assert modal.get_by_text("Why you are overriding a failed check").is_visible()
            screenshot(page, "approval held until the override is recorded")
            modal.get_by_role("button", name="Cancel").click()
            modal.wait_for(state="hidden")

    with step(page, "Undo the edit so the package is usable again"):
        open_package(page, VACANCY_TITLE)
        package_tab(page, "Email").click()
        body = page.locator("textarea.apl-editor")
        body.fill(body.input_value().split("\n\nFor the record:")[0].rstrip())
        page.get_by_role("button", name="Save the email").click()
        page.get_by_role("button", name="Undo my edits").wait_for(state="hidden", timeout=60_000)
        package_tab(page, "Checks").click()
        detail = page.locator(".apl-layout > div").nth(1)
        detail.get_by_text("claims checked").wait_for(state="visible")
        assert detail.get_by_text("Claims supported").count() == 1, (
            "removing the unsupported claim did not bring the package back to a passing "
            "check, so the rest of the suite has nothing approvable to work with"
        )

    log_tail.assert_contains(
        "POST /api/applications/",
        what="the re-check was logged (NFR-701)",
    )
    expect_no_console_errors(page, ignore=TELEMETRY)


def test_bulk_approval_says_what_goes_to_whom_first(page, signed_in) -> None:
    """FR-324: the summary is not optional, and it names recipient, company and role."""
    with step(page, "Make sure there are two packages to approve at once"):
        if not package_exists(page, VACANCY_TITLE):
            generate_package_through_the_ui(page, VACANCY_TITLE)
        if not package_exists(page, SPECULATIVE_TITLE):
            generate_package_through_the_ui(page, SPECULATIVE_TITLE)
        open_screen(page, "Applications")
        no_screen_level_failure(page, "Applications")

    with step(page, "Select every approvable package"):
        select_all = page.get_by_role("button", name="Select all")
        assert select_all.count() == 1, (
            "no package can be approved, so there is no bulk approval to gate"
        )
        select_all.click()
        page.get_by_role("button", name="Review and approve").click()

    with step(page, "The modal states what would be sent, to whom (FR-324)"):
        modal = page.locator(".modal")
        modal.wait_for(state="visible")
        modal.locator("table").wait_for(state="visible")
        # inner_text() honours the stylesheet's text-transform, so compare on
        # the word rather than on its casing.
        headers = [h.strip().lower() for h in modal.locator("table thead th").all_inner_texts()]
        for column in ("recipient", "company", "role", "kind", "subject", "attached", "checks"):
            assert column in headers, f"the summary has no {column!r} column: {headers}"

        first_row = modal.locator("table tbody tr").first.inner_text()
        assert signed_in["domain"] in first_row, (
            f"the summary does not name the recipient address: {first_row!r}"
        )
        assert "Havenstad Analytics" in first_row, "the summary does not name the company"
        assert VACANCY_TITLE in first_row or SPECULATIVE_TITLE in first_row, (
            "the summary does not name the role"
        )
        assert modal.get_by_text(
            "The briefing and the motivation document are yours and are never attached."
        ).is_visible()
        screenshot(page, "the bulk approval summary")

    with step(page, "Emptying the summary closes the approval (FR-324, NFR-702)"):
        statement = modal.locator("textarea").first
        prefilled = statement.input_value()
        assert signed_in["domain"] in prefilled, (
            f"the recorded sentence does not name the recipients: {prefilled!r}"
        )
        assert "Havenstad Analytics" in prefilled, (
            "the recorded sentence does not name the company it is about"
        )
        statement.fill("")
        approve = modal.get_by_role("button", name="Approve these")
        assert not approve.is_enabled(), (
            "FR-324 makes the summary mandatory, but the approval button is live with an "
            "empty summary"
        )
        assert modal.get_by_text(
            "This sentence is written to the audit trail with your name and the time"
        ).is_visible()

    with step(page, "Leave without approving — this suite sends nothing"):
        modal.get_by_role("button", name="Cancel").click()
        modal.wait_for(state="hidden")

    expect_no_console_errors(page, ignore=TELEMETRY)


# ---------------------------------------------------------------------------
# Contacts (FR-301..306, NFR-302)
# ---------------------------------------------------------------------------


def test_the_ranked_contacts_view_shows_its_validation(page, signed_in, log_tail) -> None:
    """FR-301/FR-304: who to write to, and how much the address can be trusted."""
    with step(page, "Open the contacts screen"):
        open_screen(page, "Contacts")
        assert page.get_by_role("heading", name="Hiring contacts").first.is_visible()
        assert page.get_by_role("button", name="Introduction routes").is_visible()
        screenshot(page, "the contacts screen on arrival")

    with step(page, "Check the help for this screen"):
        # Read before the company lookup, because that lookup is where this
        # screen currently fails and the help is worth checking either way.
        help_drawer_opens_with_content(page, "Hiring contacts")

    with step(page, "Look for the company this campaign collected"):
        # The chooser is fed by GET /api/companies?limit=200, and the screen
        # swallows a failure with .catch(() => []) — so an empty chooser and a
        # refused request look identical to the eye. They are not the same thing.
        refused = [r for r in failed_requests(page) if "/api/companies" in r["request_url"]]
        assert not refused, (
            "the Contacts screen asks for 200 companies but GET /api/companies caps `limit` "
            "at 100, so the call comes back "
            f"{refused[0]['status']} and the screen's .catch(() => []) hides it: the company "
            "chooser is empty for every job seeker, on every installation, and no contact "
            "can ever be reached from this screen "
            f"({refused[0]['request_url']})"
        )
        chooser = page.get_by_role("combobox").filter(
            has=page.get_by_role("option", name="Choose a company…")
        )
        assert chooser.count() == 1, "the Contacts screen offers no company to choose"
        chooser.select_option(label=f"Havenstad Analytics (e2e {signed_in['tag']})")

    with step(page, "Read the ranked contacts and their validation badges (FR-304)"):
        page.get_by_role("heading", name="Ranked contacts").wait_for(state="visible")
        table = page.locator("table").first.inner_text()
        assert "Marieke De Smet" in table, "the hiring manager is missing from the ranking"
        assert "Tom Peeters" in table
        assert "valid" in table and "risky" in table, (
            f"the validation verdicts are not shown: {table!r}"
        )
        assert "role address" in table, "the generic careers mailbox is not marked as one"
        assert "pattern_inference" in table, (
            "the screen does not say an inferred address was inferred (FR-303)"
        )
        screenshot(page, "ranked contacts with validation badges")

    log_tail.assert_contains("GET /api/contacts/companies/", what="the lookup was logged")
    expect_no_console_errors(page, ignore=TELEMETRY)


def test_an_objection_blocks_the_address_everywhere(page, signed_in, log_tail) -> None:
    """NFR-302: one refusal, permanent, and visible wherever the address is used.

    The order is the one a job seeker would actually live through: the
    application is written first, and the objection arrives afterwards.
    """
    with step(page, "Find the address this application would go to"):
        if not package_exists(page, VACANCY_TITLE):
            generate_package_through_the_ui(page, VACANCY_TITLE)
        open_package(page, VACANCY_TITLE)
        recipient_block = page.locator(".apl-meta").first.inner_text()
        recipient = next(
            (word for word in recipient_block.split() if "@" in word and word.endswith("example")),
            "",
        )
        assert recipient, f"the package names no recipient at all: {recipient_block!r}"
        narrate(f"  · this application is addressed to {recipient}")

    with step(page, "Record that person's objection (NFR-302)"):
        open_screen(page, "Contacts")
        page.get_by_role("button", name="Objections & retention").click()
        assert page.get_by_text(
            "An objection is permanent and applies to everyone."
        ).is_visible()
        page.locator("input[type='email']").fill(recipient)
        page.get_by_role("button", name="Block").click()
        page.get_by_text(recipient, exact=True).wait_for(state="visible")
        screenshot(page, "the address is on the blocked list")

    log_tail.assert_contains(
        "contacts.objection_recorded",
        what="the objection is in the audit trail (NFR-702)",
    )

    with step(page, "The application addressed to it says so, and can no longer be sent"):
        open_screen(page, "Applications")
        open_package(page, VACANCY_TITLE)
        header = page.locator(".apl-meta").first.inner_text()
        assert recipient in header, "the package no longer names its recipient"
        assert "objected to contact" in header, (
            "the recipient of this application has objected, but the review screen still "
            "presents the address as an ordinary recipient (NFR-302)"
        )
        approve = page.get_by_role("button", name="Approve for dispatch")
        assert not approve.is_enabled(), (
            "an application addressed to someone who has objected can still be approved "
            "for dispatch — NFR-302 makes that blocker un-overridable"
        )
        assert page.get_by_text(
            "The contact objected to being contacted (NFR-302)."
        ).is_visible(), "the refusal does not say that the objection is the reason"
        screenshot(page, "dispatch refused because the contact objected")

    expect_no_console_errors(page, ignore=CONTACTS_KNOWN_FAILURES + TELEMETRY)


# ---------------------------------------------------------------------------
# Mail setup (FR-325, FR-326, NFR-204)
# ---------------------------------------------------------------------------


def test_mail_setup_reports_that_neither_backend_can_send(page, signed_in) -> None:
    """FR-325/NFR-204: two backends, both unusable here — stated, not broken."""
    with step(page, "Open mail setup"):
        open_screen(page, "Mail setup")
        assert page.get_by_role("heading", name="Mail setup").first.is_visible()
        no_screen_level_failure(page, "Mail setup")

    with step(page, "The first-run card offers a Gmail connection this installation cannot make"):
        first_run = page.locator(".first-run")
        assert first_run.get_by_text("No mailbox is connected yet").is_visible()
        connect = first_run.get_by_role("button", name="Connect Gmail")
        assert connect.count() == 1
        # Degraded, as designed — up to a point. The Mailboxes tab below knows
        # there is no OAuth client; this card offers the button anyway, so the
        # only way to learn is to press it. Pressing it is safe: it starts no
        # handshake, and sends nothing.
        connect.click()
        banner = page.locator(".alert-danger").first
        banner.wait_for(state="visible")
        message = " ".join(banner.inner_text().split())
        assert "GMAIL_CLIENT_ID" in message and "OAuth client" in message, (
            f"the refusal does not say what is missing or how to fix it: {message!r}"
        )
        narrate(f"  · degraded as designed: {message[:180]}")
        screenshot(page, "connecting gmail is refused, with instructions")

    with step(page, "Gmail reports itself as not connected, with the reason (NFR-204)"):
        gmail = page.locator(".card").filter(has_text="Gmail — your own mailbox").first
        gmail.wait_for(state="visible")
        assert gmail.get_by_text("Not connected", exact=True).is_visible()
        assert gmail.get_by_text("Gmail is not set up on this installation.").is_visible()
        assert gmail.get_by_text(
            "GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET are not set in .env"
        ).is_visible()
        assert gmail.get_by_role("button", name="Connect Gmail").count() == 0, (
            "the Gmail card offers 'Connect Gmail' although the installation has no OAuth "
            "client, so the button can only produce an error"
        )
        screenshot(page, "gmail is not connected")

    with step(page, "Resend reports a missing key as a state, not a fault (FR-325)"):
        resend = page.locator(".card").filter(has_text="Resend — relay from stepvda.com").first
        assert resend.get_by_text("API key not available yet", exact=True).is_visible()
        assert resend.get_by_text(
            "Nothing is broken — the key simply does not exist yet."
        ).is_visible()
        steps = resend.locator("ol.help-steps li")
        assert steps.count() >= 4, (
            f"the card says the key is missing but not what to do about it ({steps.count()} "
            "steps)"
        )
        assert resend.get_by_text("Cannot be checked yet").is_visible()
        assert resend.get_by_text("not set", exact=True).is_visible()
        assert resend.get_by_text("Not detected", exact=True).is_visible()
        screenshot(page, "resend has no key yet")

    with step(page, "The sending rules are reported, not offered as sliders (FR-325)"):
        page.get_by_role("button", name="Sending rules").click()
        wait_for_ready(page)
        assert page.locator(".content-wide").inner_text().strip(), "the rules tab rendered nothing"
        screenshot(page, "sending rules")

    with step(page, "The dispatch log is empty, and says so rather than failing (FR-326)"):
        page.get_by_role("button", name="Dispatch log").click()
        wait_for_ready(page)
        body = page.locator(".content-wide").inner_text()
        assert body.strip(), "the dispatch log rendered nothing at all"
        # The authorize call was refused on purpose, two steps ago.
        no_screen_level_failure(page, "Mail setup", allow=("/api/mail/gmail/authorize",))

    with step(page, "Check the help for this screen"):
        help_drawer_opens_with_content(page, "Mail setup")

    # The one refusal in this test is the one it asked for.
    expect_no_console_errors(page, ignore=TELEMETRY + ("/api/mail/gmail/authorize",))


# ---------------------------------------------------------------------------
# A last look at what the whole session did to the browser
# ---------------------------------------------------------------------------


def test_the_apply_screens_leave_the_console_clean(page, signed_in) -> None:
    """One walk through all four screens, checking nothing throws on the way."""
    for label, heading in (
        ("Opportunities", "Opportunities"),
        ("Contacts", "Hiring contacts"),
        ("Applications", "Applications"),
        ("Mail setup", "Mail setup"),
    ):
        with step(page, f"Visit {label} and confirm it painted its own content"):
            open_screen(page, label)
            assert page.get_by_role("heading", name=heading).first.is_visible()
            content = page.locator(".content").inner_text()
            assert "Something went wrong" not in content, f"{label} rendered an error boundary"
            assert len(content.strip()) > 200, f"{label} rendered almost nothing"
            try:
                assert page.locator(".spinner").count() == 0, f"{label} is still loading"
            except PlaywrightError:  # pragma: no cover - page navigated under us
                pass

    # The two Contacts-screen failures are defects with a test of their own; a
    # third error on any of these screens still fails here.
    expect_no_console_errors(page, ignore=CONTACTS_KNOWN_FAILURES + TELEMETRY)
