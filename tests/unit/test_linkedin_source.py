"""The browser strategies are planned at all (FR-165, FR-201, CR-401, CR-403).

LinkedIn and Glassdoor are *browser strategies*, not adapters: they are planned
as plan items like any other source but executed by the browser job rather than
the collection spine, which is why ``campaigns.BROWSER_STRATEGY_KEYS`` exists
and why they are deliberately absent from the adapter registry.

The bug these tests close is in the planner, not the registry.
``generate_plan`` would only attach the LinkedIn network crawl when it found a
**source catalogue row** for it - and nothing writes that row, precisely
because there is no adapter to write it.  So the crawl was never planned, every
campaign held zero LinkedIn targets, ``build_allowlist`` was always empty, and
the run planner's "Start run" could never leave its disabled state no matter
what the user did.

``test_browser_automation.py`` did not catch it because its ``_linkedin_plan``
helper *fabricates* that catalogue row by hand - the exact row production never
had.  These tests plan a campaign for real instead.
"""

from __future__ import annotations

import pytest
from dreamjob.adapters.base import all_adapters
from dreamjob.browser import glassdoor as gd
from dreamjob.browser import linkedin as li
from dreamjob.browser import session as session_mod
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import campaigns as repo
from dreamjob.pipeline import planning
from dreamjob.security import auth_service as auth


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()


DIRECTIVES = {
    # ``roles`` is one of planning._KEYWORD_KEYS; ``target_titles`` is not, and
    # a campaign with no keywords plans no searches.
    "job_content": {"roles": ["Head of Data", "Data Engineering Manager"]},
    "location": {"countries": ["Belgium"], "cities": ["Gent"]},
}


def _seeker() -> str:
    return insert_row(
        "job_seeker",
        {"email": "li@example.com", "display_name": "Seeker",
         "created_at": utcnow(), "updated_at": utcnow()},
    )


def _campaign(seeker_id: str) -> str:
    directive_id = insert_row(
        "directive_set",
        {"job_seeker_id": seeker_id, "name": "Data leadership",
         "job_content": DIRECTIVES["job_content"], "location": DIRECTIVES["location"],
         "created_at": utcnow()},
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1,
         "sections": {"experience": [{"company": "Acme NV", "title": "Data Lead"}],
                      "education": [{"school": "Ghent University"}]},
         "created_at": utcnow()},
    )
    return repo.create_campaign(
        seeker_id,
        {"name": "LinkedIn chain", "directive_set_id": directive_id,
         "profile_version_id": profile_id,
         "caps": {"max_pages": 20, "max_pages_per_source": 3}},
    )


# ---------------------------------------------------------------------------
# FR-165: planned on the seeker's consent, and on nothing else
# ---------------------------------------------------------------------------


def test_the_premise_linkedin_is_a_browser_strategy_not_an_adapter():
    """Guard the design this fix depends on, so a later 'fix' cannot undo it."""
    assert li.ADAPTER_KEY not in all_adapters()
    assert repo.BROWSER_STRATEGY_KEYS == {li.ADAPTER_KEY, gd.ADAPTER_KEY}


def test_consent_alone_is_enough_to_plan_the_network_crawl(db):
    """The regression test for a permanently greyed-out "Start run".

    No adapter and no catalogue row exist for ``linkedin_network`` - by design.
    Planning must not wait for one.
    """
    seeker = _seeker()
    auth.record_consent(seeker, "linkedin_automation", True, "test")
    campaign_id = _campaign(seeker)

    planning.generate_plan(campaign_id, seeker, use_llm=False)

    assert not any(
        c["adapter_key"] == li.ADAPTER_KEY for c in repo.list_catalogue(enabled_only=False)
    ), "the premise: nothing writes a catalogue row for a browser strategy"
    items = li.linkedin_plan_items(campaign_id)
    assert items, "the crawl must be planned on consent alone"
    allowlist = li.build_allowlist(campaign_id)
    assert len(allowlist) > 0, "an empty allowlist is what disabled the button"
    prepared = li.prepare_run(campaign_id, seeker)
    assert prepared["target_count"] == len(allowlist)
    assert prepared["estimate"]["seconds"] > 0


def test_without_consent_nothing_linkedin_is_planned(db):
    """CR-403: no consent recorded, no crawl - the gate that should be the only one."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)

    planning.generate_plan(campaign_id, seeker, use_llm=False)

    assert li.linkedin_plan_items(campaign_id) == []


def test_planned_targets_are_shapes_the_browser_slice_accepts(db):
    """The seam: whatever planning writes, targets_from_plan_item must read."""
    seeker = _seeker()
    auth.record_consent(seeker, "linkedin_automation", True, "test")
    campaign_id = _campaign(seeker)
    planning.generate_plan(campaign_id, seeker, use_llm=False)

    for item in li.linkedin_plan_items(campaign_id):
        targets = li.targets_from_plan_item(item)
        assert targets, f"plan item {item['id']} yields no targets"
        assert all(t.url.startswith("https://www.linkedin.com/") for t in targets)


# ---------------------------------------------------------------------------
# Glassdoor answers on a domain per country (FR-205)
# ---------------------------------------------------------------------------


def test_glassdoor_is_recognised_on_every_country_domain():
    """glassdoor.be is Glassdoor.

    The enumerated host set carried ``www.glassdoor.be`` but not the bare
    domain, and no ``.de``/``.ie``/``.es`` at all - so those tabs went
    undetected on the connection card *and* were refused as targets, since the
    same set feeds ``make_target``.
    """
    profile = session_mod.SITES["glassdoor"]
    for host in ("www.glassdoor.be", "glassdoor.be", "nl.glassdoor.be", "www.glassdoor.de",
                 "glassdoor.nl", "www.glassdoor.co.uk", "glassdoor.com", "www.glassdoor.com"):
        assert profile.allows_host(host), host


def test_a_lookalike_host_is_not_glassdoor():
    """The suffix test is anchored, so it cannot be walked out of."""
    profile = session_mod.SITES["glassdoor"]
    for host in ("glassdoor.com.example.net", "notglassdoor.be", "evil-glassdoor.be",
                 "glassdoor.evil.net", ""):
        assert not profile.allows_host(host), host


def test_a_country_domain_url_is_accepted_as_a_target():
    target = gd.make_target("https://glassdoor.be/Overview/Working-at-Acme-EI_IE1.htm")
    assert target.url.startswith("https://glassdoor.be/")


def test_site_for_url_finds_glassdoor_on_a_country_domain():
    assert session_mod.site_for_url("https://www.glassdoor.de/Overview/x.htm").key == "glassdoor"


def test_linkedin_host_matching_stays_tight():
    """Broadening Glassdoor must not broaden LinkedIn: its hosts are enumerated."""
    profile = session_mod.SITES["linkedin"]
    assert profile.allows_host("www.linkedin.com")
    assert profile.allows_host("linkedin.com")
    assert not profile.allows_host("linkedin.cn")
    assert not profile.allows_host("evil.linkedin.com.example.net")
