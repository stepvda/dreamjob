"""Administrator LLM configuration is in force, not merely stored (FR-362).

The administration screens exist to change the provider, the per-task model and
the local-model routing without a restart.  That is only true if the routing
decision reads them, so these tests assert the decision itself rather than the
stored value.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dreamjob.db import connection as conn_mod
from dreamjob.db.migrator import migrate


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "routing.db"
    migrate(path)
    real = conn_mod.get_connection
    monkeypatch.setattr(conn_mod, "get_connection", lambda db_path=None: real(path))

    from dreamjob.llm.client import invalidate_admin_config

    invalidate_admin_config()
    yield path
    invalidate_admin_config()


def test_env_defaults_apply_when_nothing_is_overridden(db):
    from dreamjob.llm.client import LLMClient

    client = LLMClient()
    _, _, model, provider = client.route("extract.vacancy")
    assert model == client.settings.llm_model_cheap
    assert provider == client.settings.llm_provider


def test_a_per_task_model_override_wins(db):
    from dreamjob.db.repositories import admin as repo
    from dreamjob.llm.client import LLMClient, invalidate_admin_config

    repo.set_setting("llm.task_models", {"generate.cv": "some-other-model"})
    invalidate_admin_config()

    _, _, model, _ = LLMClient().route("generate.cv")
    assert model == "some-other-model"
    # A task without an override is unaffected.
    _, _, other, _ = LLMClient().route("extract.vacancy")
    assert other != "some-other-model"


def test_a_group_override_covers_every_task_in_it(db):
    from dreamjob.db.repositories import admin as repo
    from dreamjob.llm.client import LLMClient, invalidate_admin_config

    repo.set_setting("llm.task_models", {"extract": "cheap-extractor"})
    invalidate_admin_config()

    for task in ("extract.vacancy", "extract.company", "extract.table"):
        _, _, model, _ = LLMClient().route(task)
        assert model == "cheap-extractor"


def test_privacy_sensitive_tasks_route_to_a_local_model(db):
    """NFR-306: CV and profile work can be kept off a non-EU provider."""
    from dreamjob.db.repositories import admin as repo
    from dreamjob.llm.client import LLMClient, invalidate_admin_config

    repo.set_setting("llm.local_base_url", "http://127.0.0.1:11434/v1")
    repo.set_setting("llm.local_model", "llama-local")
    repo.set_setting("llm.local_tasks", ["profile", "cv"])
    invalidate_admin_config()

    base, _, model, provider = LLMClient().route("generate.cv")
    assert provider == "local"
    assert model == "llama-local"
    assert base.startswith("http://127.0.0.1")

    # A task outside the pinned groups still goes to the cloud provider.
    _, _, _, other = LLMClient().route("score.opportunity")
    assert other != "local"


def test_cost_rates_follow_the_administrator(db):
    from dreamjob.db.repositories import admin as repo
    from dreamjob.llm.client import LLMClient, invalidate_admin_config

    repo.set_setting("llm.cost_per_1m_input_eur", 2.0)
    repo.set_setting("llm.cost_per_1m_output_eur", 4.0)
    invalidate_admin_config()

    price = LLMClient()._price(1_000_000, 1_000_000)
    assert price == pytest.approx(6.0)


def test_saving_a_setting_takes_effect_without_waiting_for_the_cache(db):
    from dreamjob.db.repositories import admin as repo
    from dreamjob.llm.client import LLMClient, invalidate_admin_config

    LLMClient().route("extract.vacancy")          # warm the cache
    repo.set_setting("llm.task_models", {"extract.vacancy": "changed-model"})
    invalidate_admin_config()                     # what the admin router calls

    _, _, model, _ = LLMClient().route("extract.vacancy")
    assert model == "changed-model"


# --- FR-127 -----------------------------------------------------------------


def test_a_career_in_a_regulated_domain_is_not_special_category_data():
    """FR-127 protects the person, not the vocabulary.

    An earlier keyword sweep deleted "healthcare", "medical devices",
    "political science", "human rights" and "race condition" from ordinary
    professional histories. That protects nobody and breaks FR-121.
    """
    from dreamjob.pipeline.enrichment import strip_special_categories

    professional = [
        "Head of Engineering at a healthcare data company",
        "Built medical device software for a medtech scale-up",
        "MSc Political Science, KU Leuven",
        "Founded a human rights documentation platform",
        "Author of a book on neuro-rights and cognitive liberty",
        "Fixed a race condition in the scheduler",
        "Works on biometric authentication systems",
        "Public health data engineering",
    ]
    for statement in professional:
        cleaned, removed = strip_special_categories({"statement": statement})
        assert cleaned == {"statement": statement}, f"wrongly stripped: {statement}"
        assert removed == []


def test_special_category_assertions_about_a_person_are_removed():
    from dreamjob.pipeline.enrichment import strip_special_categories

    personal = [
        "He was diagnosed with a chronic illness in 2019",
        "Her religion is Catholic",
        "Member of the Socialist party since 2010",
        "Identifies as gay",
        "Trade-union membership: ACV",
    ]
    for statement in personal:
        cleaned, removed = strip_special_categories({"statement": statement})
        assert cleaned is None, f"should have been stripped: {statement}"
        assert removed


def test_a_field_named_for_a_special_category_goes_whatever_its_value():
    from dreamjob.pipeline.enrichment import strip_special_categories

    cleaned, removed = strip_special_categories(
        {"name": "Someone", "religion": "unspecified", "health": "n/a"}
    )
    assert cleaned == {"name": "Someone"}
    assert set(removed) == {"religion", "health"}


# --- FR-363 -----------------------------------------------------------------


def test_an_administrator_page_cap_bounds_a_campaign(db):
    """FR-363: caps set by the operator are a ceiling, not a suggestion."""
    from dreamjob.db.repositories import admin as repo
    from dreamjob.pipeline.collection import Caps, _pages_for, administrator_caps

    caps = Caps()
    item = {"adapter_key": "greenhouse", "estimated_pages": 50}

    # Without an override the campaign's own per-source ceiling applies.
    unbounded = _pages_for(item, caps, {"pagination": True})
    assert unbounded == caps.max_pages_per_source

    repo.set_setting("source.greenhouse.caps", {"max_pages": 3})
    assert administrator_caps("greenhouse") == {"max_pages": 3}
    assert _pages_for(item, caps, {"pagination": True}) == 3

    # A different adapter is untouched.
    other = {"adapter_key": "lever", "estimated_pages": 50}
    assert _pages_for(other, caps, {"pagination": True}) == caps.max_pages_per_source
