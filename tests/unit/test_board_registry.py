"""The ATS board registry: the seed file, the importer, and the liveness table.

The registry is how a campaign gets from "no targets" to 7,500 companies
(N3, docs/Data_Gathering_Plan.md section 5.2).  Four things can go wrong with
it quietly, and each has a test here that makes the failure visible:

* **A robots-disallowed fetch inside the product.**  The importer reads the
  Common Crawl URL index, which publishes ``User-agent: * / Disallow: /``.
  It therefore runs outside the egress layer, as an operator-run script, for
  exactly two named hosts - and it still checks robots.txt for every other
  host it touches.  If that ever stops being true, FR-182 stops being an
  invariant and starts being a habit.
* **Junk slugs.**  The Wayback index for ``jobs.ashbyhq.com`` yields 937
  non-slugs among 7,333 strings: ``"..."``, ``".sitemap.xml"``, 400-character
  base64 blobs.  Each one would become a plan item, a 404, a stored
  ``raw_document`` and a re-probe on the next run (section 6.7).
* **A failed import that reports success.**  A run whose sources 500 must not
  write a shorter registry and exit 0; that is the exact shape of defect the
  gathering plan was written to remove (section 3, step 8).
* **A re-import that resets liveness.**  ``last_verified`` belongs to the
  nightly job.  An importer that overwrites it makes every board look
  unverified again and buys the next nightly run 14,600 pointless requests.

Nothing here touches the network: the HTTP tests drive the importer's real
client through an ``httpx.MockTransport``, and the seed tests read the file
committed to the repository.
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import json
import os
import re
import secrets
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from dreamjob.adapters import discover
from dreamjob.adapters.ats import detect
from dreamjob.adapters.base import get_adapter
from dreamjob.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "import_board_registry.py"
REGISTRY = REPO_ROOT / "backend" / "dreamjob" / "pipeline" / "data" / "board_registry.json"


def _load_importer():
    spec = importlib.util.spec_from_file_location("import_board_registry", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module        # dataclasses resolve types through it
    spec.loader.exec_module(module)
    return module


imp = _load_importer()


@pytest.fixture
def registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The seed registry shipped in the repository
# ---------------------------------------------------------------------------


def test_seed_registry_rows_are_the_documented_tuple(registry: dict) -> None:
    """(vendor, slug, name, first_seen, last_verified, source), and nothing else.

    A fresh install has to have targets before anyone runs the importer, so
    this file is part of the product, not a fixture.
    """
    assert registry["kind"] == "dreamjob.board_registry"
    assert registry["schema_version"] == imp.SCHEMA_VERSION
    assert registry["staleness_days"] == 30      # C8: liveness is re-verified monthly

    boards = registry["boards"]
    assert len(boards) >= 3_000, "the seed is the route to thousands of companies"
    fields = {"vendor", "slug", "name", "first_seen", "last_verified", "source"}
    seen: set[tuple[str, str]] = set()
    for board in boards:
        assert set(board) == fields, board
        assert board["vendor"] in imp.VENDORS, board
        assert imp.is_plausible_slug(board["vendor"], board["slug"]), board
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", board["first_seen"]), board
        assert board["first_seen"] <= registry["generated_at"]
        # Nothing in a shipped file has been fetched by this installation, so
        # claiming a verification date would be a lie the nightly job trusts.
        assert board["last_verified"] is None, board
        assert board["source"] and set(board["source"].split("+")) <= set(imp.SOURCES), board
        key = (board["vendor"], board["slug"])
        assert key not in seen, f"duplicate board {key}"
        seen.add(key)
    assert registry["counts"]["total"] == len(boards)


def test_seed_registry_is_eu_weighted(registry: dict) -> None:
    """Recruitee (83% EU) and Personio (55% EU) carry the file, by design.

    Greenhouse is 8% EU and Ashby 18%; taking them whole would triple the file
    for boards a Benelux campaign will mostly discard.
    """
    by_vendor = registry["counts"]["by_vendor"]
    eu_heavy = sum(by_vendor.get(v, 0) for v in ("recruitee", "personio", "teamtailor"))
    assert eu_heavy / registry["counts"]["total"] > 0.7

    # IR-101: api.smartrecruiters.com allows LinkedInBot only, so the adapter
    # is disabled (C6).  Shipping its slugs would manufacture targets that
    # nothing in the product is allowed to read.
    assert "smartrecruiters" not in by_vendor
    assert not imp.VENDORS["smartrecruiters"].enabled

    # Workday's slug is "<host>/<site>"; the URL indexes name only the tenant
    # host, and a guessed site is a 404 that gets stored and re-probed.
    assert "workday" not in by_vendor


def test_seed_registry_documents_its_own_provenance(registry: dict) -> None:
    """A reader must be able to tell where each row came from, and when."""
    provenance = registry["provenance"]
    assert provenance["generated_at"] == registry["generated_at"]
    assert provenance["generated_by"] == "scripts/import_board_registry.py"
    assert provenance["common_crawl_terms"] == imp.COMMON_CRAWL_TERMS
    assert provenance["partial"] is False and provenance["failures"] == []
    assert any("EU" in note for note in provenance["composition"])
    assert any("Workday" in note for note in provenance["composition"])
    # Liveness predicts from provenance (2026-crawl slugs 87.5% live,
    # Wayback-only 27.5%), so the source is per row, not only per file.
    assert set(registry["counts"]["by_source"]) <= set(imp.SOURCES)


def test_every_seed_row_is_a_fetchable_plan_item(registry: dict) -> None:
    """A registry row must reach an adapter without further translation.

    This is the whole point of N3: ``slug`` is what ``company.ats_slug`` holds
    and what ``ATSAdapter.slugs_of()`` reads, so discovery can emit one plan
    item per (vendor, slug) with no LLM and no guessing.

    Teamtailor and Workable are seeded ahead of their adapters (N8): a row
    whose vendor has no adapter yet must still name a real board URL, so the
    registry does not have to be rebuilt when the adapters land.
    """
    discover()                                       # @register_adapter runs on import
    sample = {b["vendor"]: b for b in registry["boards"]}          # one per vendor
    assert len(sample) >= 5
    exercised = 0
    for vendor, board in sample.items():
        assert detect.board_url(vendor, board["slug"]), vendor
        adapter_key = detect.adapter_key_for(vendor)
        if adapter_key is None:                      # adapter still to be written (N8)
            assert vendor in {"teamtailor", "workable"}, vendor
            continue
        adapter = get_adapter(adapter_key)
        assert adapter.vendor == vendor
        item = {"native_query": {"board_slugs": [board["slug"]]}}
        assert adapter.slugs_of(item) == [board["slug"]]
        exercised += 1
    assert exercised >= 4


# ---------------------------------------------------------------------------
# Slug hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "junk",
    [
        "...",                                   # a relative path, not a board
        ".sitemap.xml",
        "sitemap.xml",
        "robots.txt",
        "embed",                                 # greenhouse's own embed path
        "careers-analytics",                     # N9: Recruitee's analytics host, 403
        "cdn",
        "static",
        "assets",
        "app",
        "a1%20garage%20door%20service",          # URL-encoded, never a slug
        "-v-_wgco6x2t2oy5f0j17eyrjtke2hottdkum01taolctauecpjo77qu3nlugzg3kjem3b7by0bfnkech1g",
        "j",                                     # one character
        "",
    ],
)
def test_implausible_slugs_never_enter_the_registry(junk: str) -> None:
    """937 of 7,333 Wayback strings for one vendor are not board names.

    Before this filter every one of them was a plan item, a request, a 404 and
    a stored document - and the plan item still reported success.
    """
    assert not imp.is_plausible_slug("ashby", junk)


@pytest.mark.parametrize(
    ("vendor", "slug"),
    [
        ("recruitee", "12build"),
        ("personio", "1komma5grad"),
        ("ashby", "0x"),
        ("greenhouse", "103644278"),
        ("lever", "11855760-canada-inc"),
        ("workable", "1-stopasia"),
        ("workday", "acme.wd3.myworkdayjobs.com/External"),
    ],
)
def test_real_slugs_survive_the_filter(vendor: str, slug: str) -> None:
    assert imp.is_plausible_slug(vendor, slug)


def test_a_workday_tenant_without_a_site_yields_no_row() -> None:
    """``acme.wd3.myworkdayjobs.com`` is not a slug; ``.../External`` is.

    The adapter needs "<host>/<site>"; inventing the site would be exactly the
    hallucinated slug rule 3 of the planning prompt forbids (section 6.7).
    """
    workday = imp.VENDORS["workday"]
    assert list(workday.slug_from("https://acme.wd3.myworkdayjobs.com")) == []
    assert list(workday.slug_from("https://acme.wd3.myworkdayjobs.com/External")) == [
        "acme.wd3.myworkdayjobs.com/External"
    ]
    assert list(workday.slug_from("https://acme.wd3.myworkdayjobs.com/en-US/External")) == [
        "acme.wd3.myworkdayjobs.com/External"
    ]


def test_greenhouse_embed_urls_name_the_board_not_the_embed_path() -> None:
    greenhouse = imp.VENDORS["greenhouse"]
    assert list(greenhouse.slug_from("https://boards.greenhouse.io/embed/job_board?for=acme")) == [
        "acme"
    ]
    assert list(greenhouse.slug_from("https://job-boards.greenhouse.io/collibra")) == ["collibra"]


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


def test_merge_keeps_the_oldest_sighting_and_every_source() -> None:
    """``source`` predicts liveness, so a board seen twice keeps both sources."""
    old = imp.Board(vendor="recruitee", slug="acme", first_seen="2026-01-01",
                    last_verified="2026-08-01", source="wayback")
    new = imp.Board(vendor="recruitee", slug="ACME", first_seen="2026-09-09",
                    source="commoncrawl", name="Acme NV")
    (merged,) = imp.merge_boards([old], [new])
    assert merged.first_seen == "2026-01-01"
    assert merged.last_verified == "2026-08-01"      # the importer never verifies
    assert merged.source == "commoncrawl+wayback"
    assert merged.name == "Acme NV"


def test_merge_cannot_delete_a_known_board() -> None:
    """A partial import must only ever add.  A Common Crawl outage is not
    evidence that 3,000 boards stopped existing."""
    existing = [imp.Board(vendor="personio", slug=f"b{i}", first_seen="2026-01-01",
                          source="commoncrawl") for i in range(50)]
    merged = imp.merge_boards(existing, [imp.Board(vendor="lever", slug="new",
                                                   first_seen="2026-09-09", source="hackernews")])
    assert len(merged) == 51


# ---------------------------------------------------------------------------
# Compliance: the importer is outside the egress layer, on purpose and narrowly
# ---------------------------------------------------------------------------


def test_the_importer_never_reaches_the_egress_layer() -> None:
    """FR-182 stays an invariant of the *application*.

    The importer is the one place that knowingly fetches a robots-disallowed
    host, so it must not be reachable from - or reach into - the code a
    campaign runs.  Two hosts, named, with the terms of use cited in the
    header.
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(name.startswith("dreamjob.egress") for name in imported), imported
    assert not any(name.startswith("dreamjob.adapters") for name in imported), imported

    assert imp.COMMON_CRAWL_HOSTS == {"index.commoncrawl.org", "data.commoncrawl.org"}
    assert imp.COMMON_CRAWL_TERMS in (imp.__doc__ or "")
    assert "Disallow: /" in (imp.__doc__ or "")

    # And no module inside the product may quietly grow the same exception.
    package = REPO_ROOT / "backend" / "dreamjob"
    offenders = [
        p for p in package.rglob("*.py")
        if "commoncrawl.org" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_robots_is_honoured_for_every_host_but_the_two_named_ones() -> None:
    """web.archive.org and hn.algolia.com are checked; Common Crawl is not.

    Measured 2026-09-09: both return 404 for /robots.txt, so both are
    permitted - but the check happens, and a future ``Disallow: /`` stops the
    importer rather than being noticed by nobody.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(200, text="ok")

    with _client(handler) as client:
        fetcher = imp.Fetcher(client=client, delay=0.0)
        with pytest.raises(imp.RobotsDisallowed):
            fetcher.get("https://web.archive.org/cdx/search/cdx?url=jobs.lever.co/*")
        assert fetcher.get("https://index.commoncrawl.org/CC-MAIN-2026-34-index?url=x") is not None

    assert "https://web.archive.org/robots.txt" in seen
    assert not any("index.commoncrawl.org/robots.txt" in url for url in seen)


def test_an_unreadable_robots_txt_refuses_the_fetch() -> None:
    """RFC 9309 2.3.1.4, and the same line the egress layer takes (FR-182)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(503)
        return httpx.Response(200, text="ok")

    with _client(handler) as client:
        fetcher = imp.Fetcher(client=client, delay=0.0)
        with pytest.raises(imp.RobotsDisallowed):
            fetcher.get("https://hn.algolia.com/api/v1/items/1")


# ---------------------------------------------------------------------------
# Reading the sources
# ---------------------------------------------------------------------------


def test_common_crawl_pages_are_walked_and_dead_urls_ignored() -> None:
    """``showNumPages``/``page``; only rows the crawl actually fetched count."""
    pages = {
        "0": [
            {"url": "https://acme.recruitee.com/o/data-engineer", "status": "200"},
            {"url": "https://gone.recruitee.com/", "status": "404"},
            {"url": "https://www.recruitee.com/blog", "status": "200"},
        ],
        "1": [{"url": "https://beta.recruitee.com/", "status": "200"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.params.get("showNumPages"):
            return httpx.Response(200, json={"pages": 2})
        page = request.url.params.get("page")
        return httpx.Response(200, text="\n".join(json.dumps(r) for r in pages[page]))

    with _client(handler) as client:
        fetcher = imp.Fetcher(client=client, delay=0.0)
        boards = imp.collect_common_crawl(
            fetcher, [imp.VENDORS["recruitee"]], ["CC-MAIN-2026-34"], "2026-09-09"
        )

    assert [b.slug for b in boards] == ["acme", "beta"]     # not "gone", not "www"
    assert all(b.source == "commoncrawl" and b.last_verified is None for b in boards)


def test_hackernews_reads_replies_at_any_depth() -> None:
    """Boards are posted in replies as often as in top-level comments.

    Reading only ``item["children"]`` loses every nested one, silently.
    """
    thread = {
        "text": "Acme | Brussels | https://acme.recruitee.com",
        "children": [
            {"text": "no board here", "children": [
                {"text": "Deep &amp; Co | https://deepco.jobs.personio.de/", "children": []},
            ]},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if "search_by_date" in request.url.path:
            return httpx.Response(200, json={"hits": [{"objectID": "1", "title": "Who is hiring?"}]})
        return httpx.Response(200, json=thread)

    vendors = [imp.VENDORS["recruitee"], imp.VENDORS["personio"]]
    with _client(handler) as client:
        boards = imp.collect_hackernews(imp.Fetcher(client=client, delay=0.0), vendors, "2026-09-09")

    assert {(b.vendor, b.slug) for b in boards} == {
        ("recruitee", "acme"), ("personio", "deepco"),
    }


def test_wayback_page_count_falls_back_rather_than_crashing() -> None:
    """The CDX ``showNumPages`` answer is a bare integer, not JSON."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.params.get("showNumPages"):
            return httpx.Response(200, text="1\n")
        return httpx.Response(200, text="https://jobs.lever.co/acme/job-1\nhttps://jobs.lever.co/...")

    with _client(handler) as client:
        boards = imp.collect_wayback(
            imp.Fetcher(client=client, delay=0.0), [imp.VENDORS["lever"]], "2026-09-09"
        )
    assert [b.slug for b in boards] == ["acme"]
    assert boards[0].source == "wayback"


def test_a_source_that_never_answers_is_an_error_not_an_empty_result() -> None:
    """Three 500s in a row is a failure.  It used to be an empty list."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(500)

    with _client(handler) as client:
        fetcher = imp.Fetcher(client=client, delay=0.0)
        with pytest.raises(imp.ImporterError):
            fetcher.get("https://hn.algolia.com/api/v1/items/1", tries=2)
        assert fetcher.errors == 1


# ---------------------------------------------------------------------------
# The CLI: a failed run must not look like a successful one
# ---------------------------------------------------------------------------


def test_a_failed_import_refuses_to_write_the_registry(tmp_path, monkeypatch) -> None:
    """Exit 2, and the existing registry is left exactly as it was.

    Without this, an operator whose network blocked index.commoncrawl.org
    would commit a registry with 400 boards instead of 3,750 and see a green
    run - the same "recorded a failure as a success" defect the gathering plan
    found in the collection worker.
    """
    out = tmp_path / "board_registry.json"
    before = imp.registry_document(
        [imp.Board(vendor="recruitee", slug="acme", first_seen="2026-01-01",
                   source="commoncrawl")],
        {"generated_at": "2026-01-01"},
    )
    imp.write_registry(out, before)
    original = out.read_text(encoding="utf-8")

    def explode(*args, **kwargs):
        raise imp.ImporterError("index.commoncrawl.org: HTTP 503")

    monkeypatch.setattr(imp, "collect_common_crawl", explode)
    code = imp.main(["--source", "commoncrawl", "--vendor", "recruitee", "--out", str(out)])

    assert code == 2
    assert out.read_text(encoding="utf-8") == original

    # ...and the operator can override it deliberately, which then says so in
    # the file itself rather than in a lost console line.
    code = imp.main(["--source", "commoncrawl", "--vendor", "recruitee", "--out", str(out),
                     "--allow-partial"])
    assert code == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["provenance"]["partial"] is True
    assert written["provenance"]["failures"]
    assert len(written["boards"]) == 1                 # merged, not truncated


def test_from_file_import_reproduces_the_seed_shape(tmp_path) -> None:
    """The committed registry was built this way, from recorded evidence."""
    slugs = tmp_path / "slugs.json"
    slugs.write_text(json.dumps(["acme", "sitemap.xml", "beta"]), encoding="utf-8")
    out = tmp_path / "registry.json"

    code = imp.main(["--from-file", f"recruitee={slugs}=commoncrawl", "--out", str(out),
                     "--note", "test"])

    assert code == 0
    document = json.loads(out.read_text(encoding="utf-8"))
    assert [b["slug"] for b in document["boards"]] == ["acme", "beta"]
    assert document["provenance"]["composition"] == ["test"]
    assert document["counts"]["by_vendor"] == {"recruitee": 2}


def test_a_disabled_vendor_cannot_be_imported(tmp_path) -> None:
    """IR-101: SmartRecruiters is disabled, so no route may fill it with rows."""
    with pytest.raises(SystemExit):
        imp.main(["--vendor", "smartrecruiters", "--source", "commoncrawl",
                  "--out", str(tmp_path / "r.json")])


# ---------------------------------------------------------------------------
# Migration 092: liveness state in the database
# ---------------------------------------------------------------------------

_ENV_KEYS = ("DREAMJOB_DATA_DIR", "DREAMJOB_DB_PATH", "DREAMJOB_MASTER_KEY",
             "DREAMJOB_SESSION_SECRET", "DREAMJOB_ENV", "DEEPSEEK_API_KEY")


@pytest.fixture
def isolated_db(tmp_path: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_SESSION_SECRET"] = secrets.token_urlsafe(32)
    os.environ["DREAMJOB_ENV"] = "development"
    os.environ["DEEPSEEK_API_KEY"] = ""
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


def test_a_board_is_identified_by_vendor_and_slug(isolated_db: None) -> None:
    """Two rows for one board are two fetches and two companies (DR-101)."""
    from dreamjob.db.connection import insert_row, utcnow

    row = {"vendor": "recruitee", "slug": "acme", "source": "commoncrawl",
           "first_seen": "2026-09-09", "updated_at": utcnow()}
    insert_row("board_registry", dict(row))
    with pytest.raises(sqlite3.IntegrityError):
        insert_row("board_registry", dict(row))

    # The same slug on another vendor is a different company, and allowed.
    insert_row("board_registry", {**row, "vendor": "personio"})


def test_liveness_is_a_state_the_importer_must_not_touch(isolated_db: None) -> None:
    """Re-importing must not reset ``last_verified``.

    This is the invisible failure: the import prints "3,750 updated" either
    way, and the cost lands on the nightly job, which re-verifies 14,600
    boards it verified yesterday - 8 hours at the compliant rate.
    """
    from dreamjob.db.connection import query_one, update_row

    board = imp.Board(vendor="recruitee", slug="acme", first_seen="2026-01-01",
                      source="commoncrawl")
    assert imp.load_into_db([board]) == {"inserted": 1, "updated": 0}

    row_id = imp.board_row_id("recruitee", "acme")
    update_row("board_registry", row_id, {
        "liveness": "live", "last_verified": "2026-09-08", "last_status": 200, "job_count": 12,
    })

    later = imp.Board(vendor="recruitee", slug="acme", first_seen="2026-09-09",
                      source="commoncrawl+hackernews")
    assert imp.load_into_db([later]) == {"inserted": 0, "updated": 1}

    stored = query_one("SELECT * FROM board_registry WHERE id = ?", (row_id,))
    assert stored["last_verified"] == "2026-09-08"
    assert stored["liveness"] == "live"
    assert stored["job_count"] == 12
    assert stored["first_seen"] == "2026-01-01"          # the oldest sighting wins
    assert stored["source"] == "commoncrawl+hackernews"  # the new evidence lands


def test_the_seed_registry_loads_into_the_table(isolated_db: None, registry: dict) -> None:
    """The shipped file and the table hold the same rows, keyed the same way."""
    from dreamjob.db.connection import query_all

    boards = [imp.Board.from_json(row) for row in registry["boards"][:500]]
    stats = imp.load_into_db(boards)

    assert stats["inserted"] == len(boards)
    rows = query_all("SELECT vendor, slug, liveness, last_verified FROM board_registry")
    assert len(rows) == len(boards)
    assert {r["liveness"] for r in rows} == {"unverified"}
    assert {r["last_verified"] for r in rows} == {None}
