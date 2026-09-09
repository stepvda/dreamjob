# End-to-end tests

These tests drive the **running** application with a real browser: Playwright
(chromium, headless) against the SPA on `:5173`, which proxies to the API on
`:8000`. Nothing is mocked, nothing is faked, and the database they touch is
the development one.

They exist to answer the questions the 528 backend tests cannot: does the
lazy-loaded screen actually paint, does the session cookie survive a reload,
does the console stay clean while a job seeker walks through the pipeline —
and does the app *log* what NFR-701 says it must while doing it.

Every run leaves a report behind (`out/report.md`) that reads as a narrative of
what was exercised, with a screenshot at each step.

## Running them

```bash
./scripts/dev.sh                                  # in one terminal: API + SPA
python3 tests/e2e/persona/generate.py             # once — invents the job seeker
python3 tests/e2e/persona/render.py               # once — renders their CV etc.
PYTHONPATH=backend python3 -m pytest tests/e2e -v
```

The suite **skips itself with an explanation** if the app is not up, or if the
persona has not been generated — an e2e suite that cannot reach the app has
nothing useful to say, and twenty connection errors would only bury the one
fact that matters.

Watch it happen, or slow it down:

```bash
DREAMJOB_E2E_HEADED=1 DREAMJOB_E2E_SLOWMO_MS=300 PYTHONPATH=backend \
  python3 -m pytest tests/e2e -v -s
```

`-s` shows the ▶ narrative live; without it the same lines are still written to
`out/run.log`.

### Environment

| Variable | Default | Meaning |
|---|---|---|
| `DREAMJOB_E2E_BASE_URL` | `http://127.0.0.1:5173` | Where the SPA is served |
| `DREAMJOB_E2E_API_URL` | `http://127.0.0.1:8000` | Where the API answers `/api/health` |
| `DREAMJOB_E2E_HEADED` | unset | `1` shows the browser |
| `DREAMJOB_E2E_SLOWMO_MS` | `0` | Pause between actions, for watching |
| `DREAMJOB_E2E_TIMEOUT_MS` | `15000` | Default locator/navigation timeout |
| `DREAMJOB_E2E_KEEP_OUT` | unset | `1` keeps the previous run's `out/` |
| `DREAMJOB_LOG_DIR` | `logs` | Where the log assertions look (NFR-701) |

Point `DREAMJOB_E2E_BASE_URL` at `http://127.0.0.1:8000` to run the same suites
against the production build FastAPI serves from `frontend/dist`.

## What a run leaves behind

```
tests/e2e/out/
├── report.md          the narrative: steps, outcomes, durations, screenshots,
│                      console errors and failed requests grouped by screen
├── report.json        the same run, normalised, for diffing or graphing
├── run.json           the raw record (report.py regenerates the reports from it)
├── run.log            the ▶ narrative, whether or not pytest captured stdout
└── screenshots/       001-…png, 002-…png — numbered in run order
```

Regenerate the reports without re-running the browser:

```bash
python3 tests/e2e/report.py            # or: python3 tests/e2e/report.py path/to/run.json
```

`out/` is wiped at the start of each session so the report is always *this*
run; set `DREAMJOB_E2E_KEEP_OUT=1` when running one suite at a time.

## Writing a suite

```python
from harness import (
    assert_log_contains, expect_no_console_errors, open_screen,
    register_fresh_account, screenshot, step,
)


def test_a_job_seeker_starts_a_campaign(page, persona, log_tail):
    with step(page, "Register a fresh account from the persona"):
        register_fresh_account(page, persona)

    with step(page, "Open the directives screen"):
        open_screen(page, "Directives")
        assert page.get_by_role("heading", name="Search directives").is_visible()

    with step(page, "Save a directive set"):
        page.get_by_role("button", name="New directive set").click()
        page.get_by_role("button", name="Save").click()
        assert page.get_by_text("Saved").is_visible()

    # The feature is not done until it is observable (NFR-701).
    log_tail.assert_contains("directive_set", what="the directive set was logged")
    expect_no_console_errors(page)
```

Three conventions, because they are what keeps these tests alive across a
restyling:

1. **One step per meaningful action.** The step name becomes a line in the
   report, so write what a person would say they were doing — "Approve the
   letter", not "click #approve-btn". Each step is timed, screenshotted at the
   end, and recorded with its outcome.
2. **Role- and text-based selectors.** `get_by_role("link", name="Campaigns")`,
   `get_by_text(...)`, `get_by_label(...)`. A CSS class is a styling decision;
   an accessible name is a product decision. The harness itself breaks this
   rule exactly twice — the loading spinner and the error banner have neither a
   role nor stable text — and says so where it does.
3. **Wait for content, never sleep.** The SPA is route-split (NFR-101), so
   arriving at a URL renders a spinner while the chunk downloads. `open_app`,
   `open_screen` and `wait_for_ready` already wait for the screen to settle;
   for anything else, wait for the thing you are about to assert on.

Suites import `harness` directly — `tests/e2e` is on `sys.path` while pytest
runs. The `e2e` fixture returns the same module if you prefer a fixture.

## Fixtures (`conftest.py`)

| Fixture | Scope | What it gives you |
|---|---|---|
| `page` | test | A page with a recorder attached (console, errors, requests, correlation ids) |
| `context` | test | A fresh 1440x900 browsing context — no cookies from another test |
| `browser` | session | One headless chromium |
| `persona` | session | The generated job seeker (`persona/out/persona.json`) |
| `log_tail` | test | The backend's log directory, snapshotted at the start of this test |
| `base_url`, `api_url`, `e2e_config` | session | Addresses and browser options |
| `app_running` | session, autouse | Skips everything, with the reason, if the app is down |
| `e2e` | test | The `harness` module |

## Harness reference (`harness.py`)

**Navigating**

- `open_app(page, path="/")` — load the SPA and wait for it to be interactive.
- `open_screen(page, "Opportunities")` — click a sidebar entry by its label,
  then wait. Navigating the way a person would keeps the test honest: if the
  entry is renamed or dropped, the suite says so.
- `wait_for_ready(page)` — wait until no spinner is left on the screen.

**Accounts**

- `register_fresh_account(page, persona)` — create a new account from the
  persona and land in the app. Every run gets its own `+e2e-<timestamp>`
  address, so the suite starts from an empty private space (FR-101) rather
  than yesterday's leftovers. Returns the credentials.
- `sign_in(page, email, password, totp=None)` — sign in, answering the MFA
  prompt if the server asks for one (NFR-202). A refusal fails in about a
  second, quoting the message the app showed.
- `sign_out(page)`, `is_signed_in(page)`.

**Evidence**

- `step(page, "what I am doing")` — the narrative unit; context manager.
- `screenshot(page, name)` — full-page, numbered, into `out/screenshots/`.
- `expect_no_console_errors(page, ignore=())` — fails the test on an uncaught
  client error and reports what it was, where, and during which step. A React
  render that throws leaves the screen half-drawn rather than blank, so without
  this check a broken screen can still satisfy every locator on it. `ignore`
  matches the message *and* its source, so a call you expect to fail is named
  by its URL: `ignore=("/api/profile/",)`.
- `expect_no_failed_requests(page, ignore=())` — same for 4xx/5xx and requests
  that never landed.
- `console_errors(page)`, `console_warnings(page)`, `failed_requests(page)`,
  `correlation_ids(page)` — the raw record, if a suite wants to assert on it.

Two classes of noise are recorded but not counted, because otherwise the checks
above would be useless: Chromium's console echo of an *expected* HTTP failure
(the SPA probes `/auth/me` on boot precisely to learn there is no session), and
`net::ERR_ABORTED`, which is how a request cancelled by navigation or page
close is reported — the client log ships on `pagehide`, exactly then. Both
appear in `report.json` under `expected`.

**The server's own logs (NFR-701)**

- `log_tail.assert_contains(pattern, what=..., timeout=5.0)` — assert the
  backend logged something *because of this test*. The tail is anchored to a
  byte offset taken when the test started, so a line from yesterday cannot
  satisfy it. `pattern` is a substring or a regex.
- `assert_log_contains(pattern)` — the same, scanning whole files, when any
  recent occurrence will do.

A feature is not finished when it works; it is finished when someone can tell
from the outside that it worked. Asserting on the log is how these tests hold
that line — and the report lists every such assertion, met or unmet.

## Notes

- Each run registers one account in `data/dreamjob.db`. That is deliberate —
  isolation between runs is worth more than a tidy dev database — and the
  address makes them easy to spot.
- The suites are read-write against the development data. Do not point
  `DREAMJOB_E2E_BASE_URL` at anything you would mind writing to.
- The harness records the correlation id from any of `x-correlation-id`,
  `x-request-id` or `x-trace-id`; the report lists what it saw, and says so
  when it saw nothing.
- `out/` holds artefacts, not sources — keep it out of commits.
