"""Model routing for generation, and the response cache (FR-363, NFR-104).

Two findings from E2E_1500 section 8.9 are pinned here: the generation tasks are
prose writing and belong on the chat model, and a repeat extraction of the same
page must not be paid for twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dreamjob.db import connection as conn_mod
from dreamjob.db.migrator import migrate
from dreamjob.llm import client as llm


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "llm_cache.db"
    migrate(path)
    real = conn_mod.get_connection
    monkeypatch.setattr(conn_mod, "get_connection", lambda db_path=None: real(path))
    llm.invalidate_admin_config()
    yield path
    llm.invalidate_admin_config()


def test_generation_tasks_route_to_the_chat_model(db):
    """FR-363: the reasoning model returned nothing for over half of them."""
    client = llm.LLMClient()
    for task in ("generate.cv", "generate.email", "generate.motivation", "generate.briefing"):
        _, _, model, _ = client.route(task)
        assert model == client.settings.llm_model_cheap, task


def test_reasoning_tasks_still_route_to_the_strong_model(db):
    client = llm.LLMClient()
    for task in ("profile.composite", "score.opportunity", "analysis.financial"):
        _, _, model, _ = client.route(task)
        assert model == client.settings.llm_model_strong, task


def test_a_repeat_extraction_is_served_from_cache_without_a_second_call(db, monkeypatch):
    client = llm.LLMClient()
    monkeypatch.setattr(
        client, "route", lambda task, prefer_strong=None: ("http://x/v1", "k", "m", "deepseek")
    )
    logged: list = []
    monkeypatch.setattr(client, "_log_call", lambda *a, **k: logged.append(k.get("status")))

    network_calls: list[int] = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {"message": {"content": '{"title": "Data Engineer"}'}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def post(self, *args, **kwargs):
            network_calls.append(1)
            return FakeResponse()

    monkeypatch.setattr(llm.httpx, "Client", FakeClient)

    first = client.complete("extract.vacancy", "sys", "user", json_mode=True)
    second = client.complete("extract.vacancy", "sys", "user", json_mode=True)

    assert first.text == second.text
    assert len(network_calls) == 1, "the second identical extraction must not hit the network"
    assert "cached" in logged


def test_a_generation_answer_is_never_cached(db, monkeypatch):
    """A cached CV would be a stale CV; generation is asked for once."""
    assert "generate.cv" not in llm.CACHEABLE_TASKS
    assert "extract.vacancy" in llm.CACHEABLE_TASKS
