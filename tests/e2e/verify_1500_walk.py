"""The verification walk: sign in as the persona and photograph the product.

Not a test suite - a *witness*.  The product owner asked for proof that the
Apply Browser, the employer-kind tag and the dry-run guard are live and behave
as described, and the honest form of that proof is a browser doing what a
person would do, in order, leaving a picture of each screen behind.

Run it against the built SPA that FastAPI serves:

    PYTHONPATH=backend python3 tests/e2e/verify_1500_walk.py

Everything it presses is read-only or explicitly guarded: the two Send controls
are pressed on purpose, because the point of the walk is to show that pressing
them assembles a message and refuses to send it (FR-325, RK-05).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("DREAMJOB_E2E_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
EMAIL = os.environ.get("DREAMJOB_VERIFY_EMAIL", "thibault.casteleyn@example.com")
PASSWORD = os.environ.get("DREAMJOB_VERIFY_PASSWORD", "Dreamjob-Verify-2026!")
OUT = Path(__file__).resolve().parent / "out" / "screenshots" / "verify-1500"

_index = 0
_notes: list[dict] = []
_console: list[str] = []


def shot(page, name: str) -> str:
    global _index
    _index += 1
    slug = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    path = OUT / f"{_index:02d}-{slug}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"  shot {path.name}")
    return path.name


def note(step: str, ok: bool, detail: str) -> None:
    _notes.append({"step": step, "ok": ok, "detail": detail})
    print(f"  {'OK  ' if ok else 'FAIL'} {step}: {detail}")


def text_of(page, selector: str, default: str = "") -> str:
    node = page.query_selector(selector)
    return (node.inner_text().strip() if node else default)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="en-GB",
            timezone_id="Europe/Brussels",
        )
        page = ctx.new_page()
        page.set_default_timeout(30_000)
        page.on(
            "console",
            lambda m: _console.append(f"{m.type}: {m.text}") if m.type == "error" else None,
        )

        # --- 1. Sign in -----------------------------------------------------
        print("1. sign in")
        page.goto(f"{BASE}/", wait_until="networkidle")
        shot(page, "sign in screen")
        page.fill('input[type="email"]', EMAIL)
        page.fill('input[type="password"]', PASSWORD)
        page.click('button.btn-primary')
        page.wait_for_url("**/overview", timeout=30_000)
        page.wait_for_load_state("networkidle")
        note("sign in", True, f"{EMAIL} reached /overview")

        # --- 2. The journey map --------------------------------------------
        print("2. journey map")
        page.wait_for_selector(".wfmap", timeout=30_000)
        shot(page, "journey map")
        stages = page.query_selector_all(".wfmap-stage-label")
        note("journey map", bool(stages), f"{len(stages)} stages painted in the workflow map")

        # --- 3. The Apply Browser list and its counts -----------------------
        print("3. apply browser")
        page.goto(f"{BASE}/apply", wait_until="networkidle")
        page.wait_for_selector(".apl-row", timeout=45_000)
        rows = page.query_selector_all(".apl-row")
        chips = [c.inner_text().replace("\n", " ").strip() for c in page.query_selector_all(".chip")]
        shot(page, "apply browser list with counts")
        note("apply browser list", bool(rows), f"{len(rows)} rows on page 1; facet chips: {chips}")

        # The dry-run banner is above the list, before anything is pressed.
        banner = text_of(page, ".alert")
        note(
            "dry-run banner on the list",
            "DREAMJOB_MAIL_DRY_RUN" in banner or "not be sent" in banner or "dry run" in banner.lower(),
            banner[:200].replace("\n", " ") or "(no .alert found)",
        )

        # --- 4. One job through all five tabs -------------------------------
        print("4. five tabs")
        # An *approved* row, so that pressing Send in step 5 gets past FR-324's
        # approval gate and reaches the transport - which is the step that has
        # to be seen refusing.
        chip = page.query_selector('.chip:has-text("Approved")') or page.query_selector(
            '.chip:has-text("Generated")'
        )
        if chip:
            chip.click()
            page.wait_for_timeout(1500)
        page.wait_for_selector(".apl-row", timeout=30_000)
        page.query_selector(".apl-row .apl-row-main").click()
        page.wait_for_selector(".tabs .tab", timeout=45_000)
        page.wait_for_timeout(2500)
        job_title = text_of(page, ".apl-row.on .apl-row-title") or "(selected job)"
        for key, label in [
            ("email", "Email"),
            ("cv", "CV"),
            ("briefing", "Briefing"),
            ("motivation", "Motivation"),
            ("checks", "Checks"),
        ]:
            tab = page.query_selector(f'.tabs .tab:has-text("{label}")')
            if tab is None:
                note(f"tab {label}", False, "tab button not found")
                continue
            tab.click()
            page.wait_for_timeout(2200)
            shot(page, f"tab {key}")
            note(f"tab {label}", True, f"rendered for {job_title[:60]}")

        # --- 5. Both send controls show the dry run -------------------------
        print("5. send controls")
        send_btn = page.query_selector('button:has-text("Send email with CV attached")')
        if send_btn:
            send_btn.click()
            page.wait_for_timeout(8000)
            # The send report is the one alert that names the recipient and the
            # file on disk; the guard banner is a different alert on the page.
            report = ""
            for node in page.query_selector_all(".alert"):
                body = node.inner_text()
                if "Recipient" in body or "Written to" in body:
                    report = body
                    break
            shot(page, "single send shows the dry run")
            note(
                "single Send",
                "Written to" in report or "not sent" in report.lower(),
                report[:400].replace("\n", " | ") or "(no send report rendered)",
            )
        else:
            note("single Send", False, "the Send button was not found")

        page.goto(f"{BASE}/apply", wait_until="networkidle")
        page.wait_for_selector(".apl-row", timeout=45_000)
        # "Send all" is disabled until rows are chosen, and only an approved
        # package with a contact and a CV may be chosen at all - so tick the
        # boxes the screen actually leaves enabled.
        boxes = [b for b in page.query_selector_all('.apl-row input[type="checkbox"]') if b.is_enabled()]
        for box in boxes[:3]:
            box.click()
        page.wait_for_timeout(800)
        note("bulk selection", bool(boxes), f"{len(boxes)} rows are eligible for a bulk send; ticked {min(3, len(boxes))}")
        all_btn = page.query_selector('button:has-text("Send all with attachment")')
        if all_btn and all_btn.is_enabled():
            all_btn.click()
            page.wait_for_timeout(2500)
            modal = text_of(page, ".modal, [role=dialog]")
            shot(page, "send all confirmation with the dry run stated")
            note(
                "Send all",
                "not" in modal.lower() or "dry" in modal.lower(),
                modal[:260].replace("\n", " ") or "(modal empty)",
            )
            esc = page.query_selector('.modal button:has-text("Cancel"), [role=dialog] button:has-text("Cancel")')
            if esc:
                esc.click()
            else:
                page.keyboard.press("Escape")
        else:
            note(
                "Send all",
                False,
                "the Send all button is disabled: no row on this page is an approved package "
                "with a contact and a CV",
            )

        # --- 6. The employer-kind badge and its evidence --------------------
        # Two opportunities, because the badge has two states worth seeing: the
        # agency posting whose employer is not named, and the one the resolver
        # could not judge.  A direct employer deliberately carries no badge, so
        # there is nothing to photograph for that case.
        print("6. employer-kind badge")
        targets = [
            (os.environ.get("DREAMJOB_VERIFY_AGENCY_ID", ""), "agency"),
            (os.environ.get("DREAMJOB_VERIFY_CANNOT_TELL_ID", ""), "cannot_tell"),
        ]
        for opp_id, expected in targets:
            if not opp_id:
                note(f"employer-kind badge ({expected})", False, "no opportunity id was given")
                continue
            page.goto(f"{BASE}/opportunities/{opp_id}", wait_until="networkidle")
            page.wait_for_timeout(3500)
            shot(page, f"opportunity employer-kind {expected}")
            badge = page.query_selector("button.ek-badge")
            if badge is None:
                note(f"employer-kind badge ({expected})", False, "no button.ek-badge on the page")
                continue
            label = badge.inner_text().strip()
            badge.click()
            page.wait_for_timeout(2500)
            pop = page.query_selector(".ek-pop")
            shot(page, f"employer-kind evidence {expected}")
            note(
                f"employer-kind badge ({expected})",
                pop is not None,
                f"badge {label!r}; popover {'opened' if pop else 'did NOT open'}"
                + (f"; {pop.inner_text()[:240]}" if pop else ""),
            )

        # --- 7. The employer coverage screen --------------------------------
        page.goto(f"{BASE}/companies", wait_until="networkidle")
        page.wait_for_timeout(3000)
        shot(page, "companies with employer tags")

        ctx.close()
        browser.close()

    report = {"base_url": BASE, "email": EMAIL, "steps": _notes, "console_errors": _console}
    (OUT / "walk.json").write_text(json.dumps(report, indent=2))
    failed = [n for n in _notes if not n["ok"]]
    print(f"\n{len(_notes) - len(failed)}/{len(_notes)} steps ok; {len(_console)} console errors")
    for f in failed:
        print(f"  FAILED {f['step']}: {f['detail']}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
