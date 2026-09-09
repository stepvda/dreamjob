#!/usr/bin/env python3
"""Turn the named-employer seed into companies and verified ATS boards (FR-181, FR-222).

    python3 scripts/resolve_employer_seed.py                 # everything not yet resolved
    python3 scripts/resolve_employer_seed.py --country BE    # one country
    python3 scripts/resolve_employer_seed.py --dry-run       # fetch and report, write nothing
    python3 scripts/resolve_employer_seed.py --limit 20      # bound the run

Why this exists
---------------
The board registry is built from public URL indexes and is therefore shaped
like them: Personio and Recruitee SMEs, Teamtailor's Nordic base, and the
Greenhouse/Ashby boards that appear in Hacker News "Who is hiring" threads.
The large IT employers a Benelux search is judged on run Workday, SuccessFactors
or their own careers site and appear in none of those indexes, so no campaign
could surface them however long it ran - a 1,337-company corpus contained not
one of Accenture, Capgemini, Cegeka, delaware, Proximus or IBM.

This closes that gap the only honest way: from a list of employers named by a
human, resolve each one's *actual* board by reading its own careers page. No
slug is ever guessed - a guessed slug is a 404 that gets stored, re-probed and
counted (docs/Data_Gathering_Plan.md section 6.7) - and every slug that is
found is confirmed with one request before it is written (FR-222).

An employer whose board does not resolve is still written to ``company``. That
is not a failure: an employer with no readable board is precisely the case the
speculative track exists for (FR-262), and it belongs in the inventory the
scoring and spontaneous-application passes read.

Cost and manners
----------------
Two requests per employer at most (careers page, then one board confirmation),
paced by the egress layer, robots.txt honoured (FR-182). ~95 employers is
under 200 requests - a few minutes, once. This is an **operator-run script**,
not a campaign step: campaigns read what it wrote.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from dreamjob.adapters.ats import detect  # noqa: E402
from dreamjob.adapters.ats.detect import detect_ats_verified  # noqa: E402
from dreamjob.db.connection import utcnow  # noqa: E402
from dreamjob.db.migrator import migrate  # noqa: E402
from dreamjob.db.repositories import board_registry as board_repo  # noqa: E402
from dreamjob.db.repositories import campaigns as campaign_repo  # noqa: E402
from dreamjob.db.repositories import knowledge as kb_repo  # noqa: E402
from dreamjob.egress.client import EgressClient  # noqa: E402
from dreamjob.pipeline import dedup  # noqa: E402

log = logging.getLogger("resolve_employer_seed")


def _importer():
    """The board importer's slug hygiene, loaded from its script.

    ``is_plausible_slug`` filters the junk the URL indexes produce, and a slug
    read off a careers page is no cleaner: this is the same rule, not a second
    one that could disagree with it.
    """
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location(
        "import_board_registry", ROOT / "scripts" / "import_board_registry.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


imp = _importer()

SEED_PATH = ROOT / "backend" / "dreamjob" / "pipeline" / "data" / "employer_seed.json"
REGISTRY_PATH = ROOT / "backend" / "dreamjob" / "pipeline" / "data" / "board_registry.json"

#: Where a careers page lives, in the order worth trying.  The home page is
#: last and not first: it is the least likely to name the board and the most
#: likely to be a heavy marketing bundle.
CAREERS_PATHS = ("/careers", "/en/careers", "/jobs", "/en/jobs", "/vacatures", "/werken-bij", "")

SOURCE_LABEL = imp.SOURCE_EMPLOYER_SEED


def storable_board(vendor: str | None, slug: str | None) -> bool:
    """May this (vendor, slug) become a plan item at all?

    Three ways a detected board is worse than no board:

    * **The vendor has no adapter, or its adapter is off.**  SmartRecruiters is
      the case that matters: api.smartrecruiters.com allows LinkedInBot only, so
      the adapter is catalogued disabled (IR-101) and storing its slugs would
      manufacture targets nothing in this product is permitted to fetch - which
      is exactly what the board importer refuses to do.  A careers page that
      names one is not a licence the index did not have.
    * **The slug is junk.**  ``https://careers.soprasteria.be/`` yields
      ``smartrecruiters/ni`` from a URL fragment; two characters is a plausible
      string and an implausible company.
    * **Nothing owns it.**  Workable and Teamtailor are seeded ahead of their
      adapters (N8), so their boards are real but unreadable today.

    The vendor is still written to ``company.ats_vendor`` in every case: knowing
    which ATS an employer runs is real profiling information even when the board
    cannot be read (``detect`` says so, and means it).
    """
    if not vendor or not slug:
        return False
    adapter_key = detect.adapter_key_for(vendor)
    if adapter_key is None:
        return False
    entry = campaign_repo.get_catalogue_entry(adapter_key)
    if entry is not None and not entry.get("enabled", 1):
        return False
    return bool(imp.is_plausible_slug(vendor, slug))


def load_seed(path: Path = SEED_PATH) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("employers") if isinstance(payload, dict) else payload
    return [r for r in (rows or []) if isinstance(r, dict) and r.get("name") and r.get("domain")]


async def _first_page_that_answers(egress: EgressClient, domain: str) -> tuple[str, str] | None:
    """The first careers-ish URL on this domain that answers, with its body.

    Stops at the first 2xx: the point is to find *a* page that names the board,
    and every extra path is a request spent on an employer already answered.
    """
    for path in CAREERS_PATHS:
        url = f"https://{domain}{path}"
        try:
            result = await egress.fetch(url)
        except Exception as exc:  # noqa: BLE001 - a refusal is an answer about this URL only
            log.debug("%s: %s", url, exc)
            continue
        if getattr(result, "ok", False) and getattr(result, "text", ""):
            return url, result.text
    return None


def _upsert_company(row: dict[str, Any], vendor: str | None, slug: str | None) -> str:
    """Write the employer, merging into an existing row rather than duplicating it."""
    domain = str(row["domain"]).lower()
    existing = kb_repo.company_by_domain(domain)
    values: dict[str, Any] = {
        "name": row["name"],
        "normalised_name": dedup.normalise_company_name(row["name"]),
        "domain": domain,
        "country": row.get("country"),
        "source": SOURCE_LABEL,
        "access_method": "http",
        # A human named this employer and its board was confirmed by request;
        # that is better evidence than a crawl, and worse than a registry
        # identifier, which this row does not carry.
        "confidence": 0.8,
        "refreshed_at": utcnow(),
    }
    if vendor:
        values["ats_vendor"] = vendor
    if slug:
        values["ats_slug"] = slug
    if existing:
        kb_repo.update_company(existing["id"], values)
        return str(existing["id"])
    values["collected_at"] = utcnow()
    return kb_repo.insert_company(values)


def _merge_into_registry_file(found: list[dict[str, Any]], path: Path = REGISTRY_PATH) -> int:
    """Add the resolved boards to the shipped registry, keeping it the seed it is.

    The table is the live register; the file is what a fresh checkout starts
    from. A board resolved here belongs in both, or the next install of this
    repository would have to pay for the same requests again.
    """
    if not found:
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("Registry file %s could not be read; not merging", path)
        return 0
    boards = payload.get("boards")
    if not isinstance(boards, list):
        log.warning("Registry file %s has no boards list; not merging", path)
        return 0
    known = {(str(b.get("vendor", "")).lower(), str(b.get("slug", ""))) for b in boards}
    added = 0
    for row in found:
        key = (row["vendor"], row["slug"])
        if key in known:
            continue
        boards.append(
            {
                "vendor": row["vendor"],
                "slug": row["slug"],
                "name": row.get("name"),
                "first_seen": utcnow()[:10],
                # Never a date here, even for a slug this run confirmed: the
                # file is what every *other* installation starts from, and none
                # of them has fetched this board.  Liveness is the table's.
                "last_verified": None,
                "source": SOURCE_LABEL,
            }
        )
        known.add(key)
        added += 1
    if added:
        payload["boards"] = boards
        # The counts are asserted against the board list, so a merge that does
        # not maintain them ships a file that describes itself wrongly.
        counts = payload.setdefault("counts", {})
        counts["total"] = len(boards)
        by_vendor: dict[str, int] = {}
        by_source: dict[str, int] = {}
        for board in boards:
            by_vendor[board["vendor"]] = by_vendor.get(board["vendor"], 0) + 1
            for source in str(board.get("source") or "").split("+"):
                if source:
                    by_source[source] = by_source.get(source, 0) + 1
        counts["by_vendor"] = dict(sorted(by_vendor.items()))
        counts["by_source"] = dict(sorted(by_source.items()))

        provenance = payload.setdefault("provenance", {})
        sources = provenance.setdefault("sources", [])
        label = f"{SOURCE_LABEL}:{added}-boards"
        if label not in sources:
            sources.append(label)
        composition = provenance.setdefault("composition", [])
        note = (
            "Boards resolved from named employers' own careers pages by "
            "scripts/resolve_employer_seed.py. The URL indexes above do not name the large "
            "employers a Benelux search is judged on - they run Workday or their own careers "
            "site - so those rows can only come from reading the employer's page. No slug is "
            "guessed; last_verified stays null because no other installation has fetched them."
        )
        if note not in composition:
            composition.append(note)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return added


async def resolve(
    seed: list[dict[str, Any]], *, dry_run: bool = False, limit: int | None = None
) -> dict[str, Any]:
    resolved: list[dict[str, Any]] = []
    companies = 0
    unreachable: list[str] = []
    no_board: list[str] = []

    async with EgressClient() as egress:
        for row in seed[: limit or len(seed)]:
            name, domain = row["name"], str(row["domain"]).lower()
            page = await _first_page_that_answers(egress, domain)
            if page is None:
                unreachable.append(name)
                log.info("%-42s no careers page answered", name)
                if not dry_run:
                    _upsert_company(row, None, None)
                    companies += 1
                continue

            url, html = page
            vendor, slug = await detect_ats_verified(html, url, egress)
            keep = storable_board(vendor, slug)
            # ``detect_ats_verified`` proves a slug only where a one-GET probe
            # exists (greenhouse, lever, ashby, recruitee, personio).  Workday's
            # listing is a POST and SmartRecruiters may not be fetched at all,
            # so their slugs come back plausible-but-unproven.  Recording those
            # as "live" would be a claim no request supports.
            proven = detect.verification_url(vendor, slug) is not None

            if keep:
                log.info("%-42s %s/%s%s", name, vendor, slug, "" if proven else "  (unverified)")
                resolved.append({"vendor": vendor, "slug": slug, "name": name})
            else:
                no_board.append(name)
                if vendor and slug:
                    reason = f"{vendor}/{slug} not storable"
                elif vendor:
                    reason = f"{vendor} (no board named)"
                else:
                    reason = "no ATS"
                log.info("%-42s %s", name, reason)

            if not dry_run:
                # The vendor is written even when the board is not: which ATS an
                # employer runs is profiling information in its own right.
                _upsert_company(row, vendor, slug if keep else None)
                companies += 1
                if keep:
                    board_repo.upsert(
                        {
                            "vendor": vendor,
                            "slug": slug,
                            "name": name,
                            "source": SOURCE_LABEL,
                            "state": "live" if proven else "unverified",
                            "last_verified": utcnow() if proven else None,
                            "last_status": 200 if proven else None,
                            "consecutive_failures": 0,
                        }
                    )

    merged = 0 if dry_run else _merge_into_registry_file(resolved)
    return {
        "considered": len(seed[: limit or len(seed)]),
        "companies_written": companies,
        "boards_resolved": len(resolved),
        "boards_added_to_file": merged,
        "no_board": no_board,
        "unreachable": unreachable,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--country", action="append", default=None,
                        help="only employers in this country (repeatable)")
    parser.add_argument("--sector", action="append", default=None,
                        help="only employers in this sector (repeatable)")
    parser.add_argument("--limit", type=int, default=None, help="stop after N employers")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and report, write nothing")
    parser.add_argument("--seed", type=Path, default=SEED_PATH)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    migrate()

    seed = load_seed(args.seed)
    if args.country:
        wanted = {c.upper() for c in args.country}
        seed = [r for r in seed if str(r.get("country", "")).upper() in wanted]
    if args.sector:
        wanted = {s.lower() for s in args.sector}
        seed = [r for r in seed if str(r.get("sector", "")).lower() in wanted]
    if not seed:
        print("No employer in the seed matches those filters.")
        return 1

    report = asyncio.run(resolve(seed, dry_run=args.dry_run, limit=args.limit))

    print()
    print(f"  considered            {report['considered']}")
    print(f"  companies written     {report['companies_written']}")
    print(f"  boards resolved       {report['boards_resolved']}")
    print(f"  added to registry     {report['boards_added_to_file']}")
    print(f"  no confirmed board    {len(report['no_board'])}")
    print(f"  no careers page       {len(report['unreachable'])}")
    if args.dry_run:
        print("\n  (dry run: nothing was written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
