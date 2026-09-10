"""The SPA catch-all serves the SPA, and nothing else (CR-407, NFR-201, FR-361).

``@app.get("/{full_path:path}")`` is the last route registered, so it matches
every GET the routers did not claim - which used to include ``/api``.  Two
things followed from that and both are tested here.

An endpoint the server does not have answered ``200 text/html`` with the SPA
shell as its body.  ``api/client.js`` treats any 2xx as success and falls back
to the raw text when the body will not parse as JSON, so the caller is handed a
*string* rather than an error: a screen polling a route that is not deployed
reads it as a successful but empty answer and retries forever.  The activity
panel (FR-361) is the live case - it has a 404 branch that could never run.

And ``dist / full_path`` follows ``..`` out of ``frontend/dist``.  The
repository root one level above it holds ``.env``, which carries the provider
API keys, so the file the SPA route would serve for ``/../../.env`` is the one
file on the machine that must never leave it (NFR-201).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from dreamjob.config import REPO_ROOT, get_settings
from dreamjob.db.migrator import migrate
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings().ensure_dirs()
    migrate()
    from dreamjob.main import create_app

    with TestClient(create_app()) as http:
        yield http
    get_settings.cache_clear()


#: The route only exists when the SPA has been built.
built = pytest.mark.skipif(
    not (REPO_ROOT / "frontend" / "dist").exists(), reason="frontend/dist is not built"
)


@built
def test_unknown_api_route_is_a_json_404_not_the_spa(client: TestClient) -> None:
    """The shape a missing endpoint answers in is the shape the client parses."""
    res = client.get("/api/definitely/not/a/route")
    assert res.status_code == 404
    assert res.headers["content-type"].startswith("application/json")
    assert res.json() == {"detail": "Not Found"}
    assert "<!doctype html" not in res.text.lower()


@built
def test_the_activity_poll_never_answers_with_the_spa(client: TestClient) -> None:
    """FR-361: the activity panel's failure branches have to be reachable.

    A backend that has not been restarted since the endpoint was added has no
    such route, and used to answer the poll with the SPA shell and a 200 - so
    the panel read it as a successful empty feed and showed "nothing recorded
    for this run yet" for the length of the run while polling every four
    seconds.  With the route present the answer is 401 or 404; without it, 404.
    Never 200, and never HTML.
    """
    res = client.get("/api/campaigns/whatever/activity")
    assert res.status_code in (401, 404)
    assert res.headers["content-type"].startswith("application/json")
    assert "<!doctype html" not in res.text.lower()


@built
def test_the_spa_is_still_served_for_a_deep_link(client: TestClient) -> None:
    res = client.get("/campaigns/abc123")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")


@built
@pytest.mark.parametrize(
    "path",
    [
        "/../.env",
        "/../../.env",
        "/..%2f..%2f.env",
        "/%2e%2e/%2e%2e/.env",
    ],
)
def test_the_spa_route_cannot_walk_out_of_dist(client: TestClient, path: str) -> None:
    """NFR-201: no request may read a file outside the built frontend."""
    res = client.get(path)
    # Either the request never reaches the route, or the route answers the
    # index.  Never the file.
    assert "DREAMJOB" not in res.text
    assert "API_KEY" not in res.text
    if res.status_code == 200:
        assert res.headers["content-type"].startswith("text/html")
        assert "<div id=\"root\">" in res.text


@built
def test_a_real_static_file_is_still_served(client: TestClient) -> None:
    """The resolve() guard must not stop the SPA serving its own files."""
    name = next(
        (f.name for f in (REPO_ROOT / "frontend" / "dist").iterdir() if f.is_file()),
        None,
    )
    assert name is not None
    res = client.get(f"/{name}")
    assert res.status_code == 200


@built
def test_health_still_answers(client: TestClient) -> None:
    """A real /api route is not caught by the guard."""
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
