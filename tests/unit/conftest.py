"""No unit test may touch the installed database (plan item C5).

Two rows named ``broken_board`` and ``stub_board`` sat in the installed
``source_catalogue`` for months and were selected as real sources by every
campaign, and fourteen more had joined them by the time anyone looked.  They
were not seeded by a migration or by an operator: they are stub adapters that
unit modules register in the process-wide adapter registry (NFR-601), and one
test class called ``load_all()`` without pointing the database anywhere, so
``sync_catalogue`` wrote every stub in the registry - including the ones other
modules had registered - into ``data/dreamjob.db``.

Individually each module was innocent; the leak only appeared when the whole
suite ran in one process, which is why it survived so long.  Rather than
asking every future test to remember the fixture, the whole unit suite is
pointed at a scratch database here, before any module fixture runs.  Modules
that want isolation still create their own ``tmp_path`` database on top of
this; ``monkeypatch`` then restores the scratch path rather than the installed
one when they are done.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from dreamjob.config import get_settings
from dreamjob.db.migrator import migrate


@pytest.fixture(scope="session", autouse=True)
def scratch_database(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Point ``DREAMJOB_DB_PATH`` at a throw-away file for the whole session."""
    root = tmp_path_factory.mktemp("dreamjob-unit")
    patch = pytest.MonkeyPatch()
    patch.setenv("DREAMJOB_DATA_DIR", str(root / "data"))
    patch.setenv("DREAMJOB_DB_PATH", str(root / "data" / "unit.db"))
    get_settings.cache_clear()
    get_settings().ensure_dirs()
    migrate()
    yield
    patch.undo()
    get_settings.cache_clear()
