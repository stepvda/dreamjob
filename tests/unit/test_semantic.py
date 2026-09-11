"""The semantic index: stored vectors, cosine nearest neighbour (FR-261, FR-282).

The index is an enhancement that must be inert without an embeddings model and
must never turn a search failure into an error.  A stub embedder keeps the test
offline and deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dreamjob.config import get_settings
from dreamjob.db import connection as conn_mod
from dreamjob.db.connection import insert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.llm import client as llm_mod
from dreamjob.pipeline import semantic


class _Stub:
    """Deterministic two-axis embedder: 'data' vs 'sales'."""

    class settings:
        embeddings_model = "stub-model"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "data" in t.lower() else [0.0, 1.0] for t in texts]


class _NoModel:
    class settings:
        embeddings_model = ""

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - never called
        raise AssertionError("embed must not be called without a model")


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "semantic.db"
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(path))
    get_settings.cache_clear()
    migrate(path)
    real = conn_mod.get_connection
    monkeypatch.setattr(conn_mod, "get_connection", lambda db_path=None: real(path))
    yield path
    get_settings.cache_clear()


def _vacancy(title: str, description: str) -> str:
    return insert_row(
        "vacancy",
        {
            "title": title,
            "description": description,
            "source_adapter": "ats.greenhouse",
            "collected_at": utcnow(),
        },
    )


def test_cosine_is_bounded_and_symmetric() -> None:
    assert semantic.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert semantic.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert semantic.cosine([], [1.0]) == 0.0
    assert semantic.cosine([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0


def test_backfill_skips_unchanged_text(db: Path) -> None:
    _vacancy("Data Engineer", "Build data pipelines")
    _vacancy("Account Executive", "Sell the product")

    first = semantic.backfill("vacancy", llm=_Stub())
    assert first["indexed"] == 2
    assert semantic.backfill("vacancy", llm=_Stub())["indexed"] == 0


def test_search_returns_the_nearest_neighbour(db: Path) -> None:
    data_id = _vacancy("Data Engineer", "Build data pipelines")
    _vacancy("Account Executive", "Sell the product")
    semantic.backfill("vacancy", llm=_Stub())

    hits = semantic.search("senior data engineer", llm=_Stub(), limit=2)

    assert hits
    assert hits[0]["id"] == data_id
    assert hits[0]["similarity"] == pytest.approx(1.0)


def test_search_is_inert_without_a_model(db: Path) -> None:
    _vacancy("Data Engineer", "Build data pipelines")
    assert semantic.backfill("vacancy", llm=_NoModel())["indexed"] == 0
    assert semantic.search("anything", llm=_NoModel()) == []


def test_a_search_failure_is_not_an_error(db: Path, monkeypatch) -> None:
    _vacancy("Data Engineer", "Build data pipelines")
    semantic.backfill("vacancy", llm=_Stub())

    class _Broken(_Stub):
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise llm_mod.LLMError("endpoint down")

    assert semantic.search("data", llm=_Broken()) == []
