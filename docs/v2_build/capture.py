"""Capture screenshots of every Dream Job screen from the live application.

Headed Chrome via Playwright. Logs in as the product owner, then walks every
route (and the important sub-views) and writes PNGs into docs/v2_build/shots.

Usage:
    python capture.py [shot-name ...]     # no args = everything
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
SHOTS.mkdir(exist_ok=True)
STATE = ROOT / "state.json"

VIEWPORT = {"width": 1920, "height": 1080}


def settle(page, ms=1600):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    # Wait for transient spinners to clear, then a fixed beat for charts/fonts.
    deadline = time.time() + 6
    while time.time() < deadline:
        try:
            spinners = page.locator(".spinner").count()
        except Exception:
            spinners = 0
        if spinners == 0:
            break
        page.wait_for_timeout(200)
    page.wait_for_timeout(ms)


def shoot(page, name, full=False):
    page.wait_for_timeout(250)
    path = SHOTS / f"{name}.png"
    page.screenshot(path=str(path), full_page=full)
    print(f"  shot {name}")


def goto(page, route, name=None, ms=1600, full=False):
    page.goto(BASE + route, wait_until="domcontentloaded")
    settle(page, ms)
    shoot(page, name or route.strip("/").replace("/", "_") or "home", full=full)


def login(page):
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    if page.locator('input[type=password]').count():
        page.fill('input[type=email]', EMAIL)
        page.fill('input[type=password]', PASSWORD)
        page.click('button:has-text("Sign in")')
        page.wait_for_timeout(3000)
    return page


def first_id(page, endpoint, key="id"):
    js = """async (ep) => {
        const r = await fetch(ep, {credentials: 'same-origin'});
        if (!r.ok) return null;
        const d = await r.json();
        const arr = Array.isArray(d) ? d : (d.items || d.results || d.rows || []);
        if (arr.length && arr[0] && arr[0].id) return arr[0].id;
        return null;
    }"""
    try:
        return page.evaluate(js, endpoint)
    except Exception:
        return None


def click_text(page, text, exact=False):
    try:
        loc = page.get_by_text(text, exact=exact).first
        loc.click(timeout=4000)
        page.wait_for_timeout(1200)
        return True
    except Exception as e:
        print(f"    ! could not click {text!r}: {e}")
        return False


def run(only):
    def want(n):
        return not only or n in only

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False, args=["--window-size=1920,1080"])
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
        page = context.new_page()

        # Sign-in screen, before authenticating.
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        if want("signin"):
            shoot(page, "signin")

        login(page)
        context.storage_state(path=str(STATE))

        routes = [
            ("/home", "home"),
            ("/overview", "overview"),
            ("/profile", "profile"),
            ("/composite", "composite"),
            ("/dream-job", "dream-job"),
            ("/directives", "directives"),
            ("/campaigns", "campaigns"),
            ("/browser", "browser"),
            ("/opportunities", "opportunities"),
            ("/companies", "companies"),
            ("/intelligence", "intelligence"),
            ("/contacts", "contacts"),
            ("/apply", "apply"),
            ("/networking", "networking"),
            ("/applications", "applications"),
            ("/pipeline", "pipeline"),
            ("/responses", "responses"),
            ("/insights", "insights"),
            ("/monitoring", "monitoring"),
            ("/mail", "mail"),
            ("/admin", "admin"),
        ]
        for route, name in routes:
            if want(name):
                goto(page, route, name)

        # Detail screens from the first available instance.
        if want("campaign-detail"):
            cid = first_id(page, "/api/campaigns")
            if cid:
                goto(page, f"/campaigns/{cid}", "campaign-detail")
        if want("company-detail"):
            cid = first_id(page, "/api/companies")
            if cid:
                goto(page, f"/companies/{cid}", "company-detail")
        if want("opportunity-detail"):
            oid = first_id(page, "/api/opportunities")
            if oid:
                goto(page, f"/opportunities/{oid}", "opportunity-detail")

        browser.close()


if __name__ == "__main__":
    run(set(sys.argv[1:]))
