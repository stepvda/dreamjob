"""Capture sub-view screenshots (tabs, panels, dialogs) from the live app.

Second pass: opens the tabs behind each main screen so the deck can cover the
complete functionality.  Writes into docs/v2_build/shots.

Usage: python capture2.py [name ...]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5173"
EMAIL = "stephane@stepvda.com"
PASSWORD = "Adv39281#"
ROOT = Path(__file__).resolve().parent
SHOTS = ROOT / "shots"
VIEWPORT = {"width": 1920, "height": 1080}


def settle(page, ms=1500):
    deadline = time.time() + 6
    while time.time() < deadline:
        try:
            if page.locator(".spinner").count() == 0:
                break
        except Exception:
            break
        page.wait_for_timeout(200)
    page.wait_for_timeout(ms)


def shoot(page, name):
    page.wait_for_timeout(250)
    page.screenshot(path=str(SHOTS / f"{name}.png"))
    print(f"  shot {name}")


def goto(page, route):
    page.goto(BASE + route, wait_until="domcontentloaded")
    settle(page)


def tab(page, label, exact=True):
    """Click a tab button by its visible label."""
    for sel in [f'button.tab:text-is("{label}")',
                f'button.tab:has-text("{label}")',
                f'button:has-text("{label}")']:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=8000)
            loc.click(timeout=4000)
            page.wait_for_timeout(1500)
            return True
        except Exception:
            continue
    print(f"    ! tab not found: {label}")
    return False


def login(page):
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    if page.locator('input[type=password]').count():
        page.fill('input[type=email]', EMAIL)
        page.fill('input[type=password]', PASSWORD)
        page.click('button:has-text("Sign in")')
        page.wait_for_timeout(3000)


def run(only):
    def want(n):
        return not only or n in only

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False, args=["--window-size=1920,1080"])
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
        page = context.new_page()
        login(page)

        # ---- Profile sub-views -----------------------------------------
        if want("profile-documents") or want("profile-conflicts") or want("profile-more"):
            goto(page, "/profile")
            for lbl, nm in [("Documents", "profile-documents"),
                            ("Conflicts", "profile-conflicts"),
                            ("Dream job", "profile-dreamjob-tab")]:
                if want(nm):
                    tab(page, lbl); shoot(page, nm)
            if want("profile-more"):
                try:
                    page.get_by_text("More profile detail", exact=False).first.click()
                    page.wait_for_timeout(900)
                except Exception as e:
                    print("    ! more:", e)
                for lbl, nm in [("Sections", "profile-sections"), ("Skills", "profile-skills"),
                                ("Evidence", "profile-evidence"), ("Personas", "profile-personas"),
                                ("Privacy", "profile-privacy")]:
                    if tab(page, lbl):
                        shoot(page, nm)

        # ---- Composite sub-views ---------------------------------------
        if want("composite-tab") or want("composite-findings") or want("composite-enrichment"):
            goto(page, "/composite")
            for lbl, nm in [("Composite profile", "composite-tab"),
                            ("Online findings", "composite-findings"),
                            ("Enrichment", "composite-enrichment")]:
                if want(nm):
                    tab(page, lbl); shoot(page, nm)

        # ---- Dream job -------------------------------------------------
        if want("dream-job"):
            goto(page, "/dream-job"); shoot(page, "dream-job")

        # ---- Directives ------------------------------------------------
        if want("directives"):
            goto(page, "/directives"); shoot(page, "directives")

        # ---- Campaign detail sub-views ---------------------------------
        if want("campaign-plan") or want("campaign-live") or want("campaign-rerun"):
            goto(page, "/campaigns")
            cid = page.evaluate("""async () => {
                const r = await fetch('/api/campaigns', {credentials:'same-origin'});
                const d = await r.json(); const a = Array.isArray(d)?d:(d.items||[]);
                return a.length? a[0].id : null;
            }""")
            if cid:
                goto(page, f"/campaigns/{cid}")
                for lbl, nm in [("Plan review", "campaign-plan"),
                                ("Live dashboard", "campaign-live"),
                                ("Re-run a stage", "campaign-rerun")]:
                    if want(nm):
                        tab(page, lbl); shoot(page, nm)

        # ---- Company detail sub-views ----------------------------------
        if want("company-profile") or want("company-financials") or want("company-market") or want("company-values"):
            goto(page, "/companies")
            cid = page.evaluate("""async () => {
                const r = await fetch('/api/companies?limit=50', {credentials:'same-origin'});
                const d = await r.json(); const a = Array.isArray(d)?d:(d.items||[]);
                return a.length? a[0].id : null;
            }""")
            if cid:
                goto(page, f"/companies/{cid}")
                for lbl, nm in [("Profile", "company-profile"),
                                ("Financials", "company-financials"),
                                ("Market and timing", "company-market"),
                                ("Values and culture", "company-values")]:
                    if want(nm):
                        tab(page, lbl); shoot(page, nm)

        # ---- Contacts sub-views ----------------------------------------
        if want("contacts-tab") or want("contacts-browse") or want("contacts-intros") or want("contacts-network") or want("contacts-retention"):
            goto(page, "/contacts")
            for lbl, nm in [("Contacts", "contacts-tab"), ("Browse all", "contacts-browse"),
                            ("Introduction routes", "contacts-intros"), ("My network", "contacts-network"),
                            ("Objections & retention", "contacts-retention")]:
                if want(nm):
                    tab(page, lbl); shoot(page, nm)

        # ---- Apply / Applications package detail tabs ------------------
        if want("apply-tab") or want("package-email") or want("package-cv"):
            goto(page, "/apply")
            shoot(page, "apply")
            for lbl, nm in [("Email", "package-email"), ("CV", "package-cv"),
                            ("Briefing", "package-briefing"), ("Motivation", "package-motivation"),
                            ("Checks", "package-checks")]:
                if want(nm):
                    tab(page, lbl); shoot(page, nm)

        # ---- Pipeline sub-views ----------------------------------------
        if want("pipeline"):
            goto(page, "/pipeline"); shoot(page, "pipeline")

        # ---- Responses -------------------------------------------------
        if want("responses"):
            goto(page, "/responses"); shoot(page, "responses")

        # ---- Insights --------------------------------------------------
        if want("insights"):
            goto(page, "/insights"); shoot(page, "insights")

        # ---- Intelligence sub-views ------------------------------------
        if want("intel-gaps") or want("intel-stepping") or want("intel-fit") or want("intel-values") or want("intel-linkedin"):
            goto(page, "/intelligence")
            for lbl, nm in [("Gaps", "intel-gaps"), ("Stepping stones", "intel-stepping"),
                            ("Fit across the market", "intel-fit"), ("Values conflicts", "intel-values"),
                            ("LinkedIn profile", "intel-linkedin")]:
                if want(nm):
                    tab(page, lbl); shoot(page, nm)

        # ---- Networking sub-views --------------------------------------
        if want("networking-intros") or want("networking-events") or want("networking-export"):
            goto(page, "/networking")
            for lbl, nm in [("Introduction routes", "networking-intros"), ("Event radar", "networking-events"),
                            ("Campaign export", "networking-export")]:
                if want(nm):
                    tab(page, lbl); shoot(page, nm)

        # ---- Mail sub-views --------------------------------------------
        if want("mail-mailboxes") or want("mail-rules") or want("mail-log") or want("mail-followups"):
            goto(page, "/mail")
            for lbl, nm in [("Mailboxes", "mail-mailboxes"), ("Sending rules", "mail-rules"),
                            ("Dispatch log", "mail-log"), ("Follow-ups", "mail-followups")]:
                if want(nm):
                    tab(page, lbl); shoot(page, nm)

        # ---- Admin sub-views -------------------------------------------
        if want("admin-models") or want("admin-sources") or want("admin-activity") or want("admin-continuous") or want("admin-audit") or want("admin-logs") or want("admin-data") or want("admin-users") or want("admin-employer"):
            goto(page, "/admin")
            for lbl, nm in [("Users", "admin-users"), ("Models", "admin-models"), ("Sources", "admin-sources"),
                            ("Activity", "admin-activity"), ("Continuous", "admin-continuous"),
                            ("Audit", "admin-audit"), ("Logs", "admin-logs"), ("Data", "admin-data"),
                            ("Employer kind", "admin-employer")]:
                if want(nm):
                    tab(page, lbl); shoot(page, nm)

        # ---- Help drawer -----------------------------------------------
        if want("help"):
            goto(page, "/opportunities")
            page.keyboard.press("?")
            page.wait_for_timeout(1100)
            shoot(page, "help")

        browser.close()


if __name__ == "__main__":
    run(set(sys.argv[1:]))
