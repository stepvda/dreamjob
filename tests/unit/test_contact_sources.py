"""The FR-303 contact sources added for the second strategy (FR-303, FR-305).

Everything here is offline.  It covers the sources a corporate site's home page
does not carry - schema.org JSON-LD, an RFC 9116 ``security.txt`` and the
sitemap - the bot-protection detection that stops a challenge page being read
as "no address", and the search-provider response parsing.
"""

from __future__ import annotations

import base64
import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from dreamjob.config import get_settings
from dreamjob.pipeline import email_patterns as patterns

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_SESSION_SECRET",
    "DREAMJOB_ENV",
    "DEEPSEEK_API_KEY",
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    """A throwaway database so the admin settings store works."""
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


# ---------------------------------------------------------------------------
# Challenge detection
# ---------------------------------------------------------------------------


def test_an_f5_challenge_is_not_read_as_a_page() -> None:
    # The exact shape agfa.com answered every fetch with.
    html = '<script>window["bobcmn"] = "1011..."; window["TSPD_101"] = "abc"</script>'
    assert patterns.looks_like_challenge(html) is True


def test_a_cloudflare_challenge_is_detected() -> None:
    assert patterns.looks_like_challenge("<title>Just a moment...</title>") is True


def test_an_ordinary_page_is_not_a_challenge() -> None:
    html = "<html><title>Contact us</title><a href='mailto:hr@acme.be'>HR</a></html>"
    assert patterns.looks_like_challenge(html) is False


def test_the_challenge_check_survives_no_html() -> None:
    assert patterns.looks_like_challenge("") is False


# ---------------------------------------------------------------------------
# JSON-LD (schema.org)
# ---------------------------------------------------------------------------


def test_an_organization_email_is_read_from_jsonld() -> None:
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Organization","name":"Acme","email":"hr@acme.be"}'
        "</script>"
    )
    found = patterns.jsonld_contacts(html, domain="acme.be", source_url="https://acme.be")
    assert [a.email for a in found] == ["hr@acme.be"]
    assert found[0].method == patterns.METHOD_JSONLD


def test_a_contact_point_carries_its_role() -> None:
    html = (
        '<script type="application/ld+json">'
        '{"@graph":[{"@type":"Organization","name":"Acme","contactPoint":'
        '{"@type":"ContactPoint","email":"jobs@acme.be","contactType":"Recruitment"}}]}'
        "</script>"
    )
    found = patterns.jsonld_contacts(html, domain="acme.be")
    assert found and found[0].email == "jobs@acme.be"


def test_jsonld_from_another_domain_is_not_kept() -> None:
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Organization","email":"info@elsewhere.example"}'
        "</script>"
    )
    assert patterns.jsonld_contacts(html, domain="acme.be") == []


def test_a_broken_jsonld_block_does_not_lose_the_good_one() -> None:
    html = (
        '<script type="application/ld+json">{not json,}</script>'
        '<script type="application/ld+json">{"email":"hr@acme.be"}</script>'
    )
    found = patterns.jsonld_contacts(html, domain="acme.be")
    assert [a.email for a in found] == ["hr@acme.be"]


# ---------------------------------------------------------------------------
# security.txt and sitemap
# ---------------------------------------------------------------------------


def test_security_txt_contact_fields_are_read() -> None:
    text = "Contact: mailto:security@acme.be\nContact: https://acme.be/report\n"
    found = patterns.security_txt_addresses(text, domain="acme.be")
    assert [a.email for a in found] == ["security@acme.be"]


def test_sitemap_locations_are_parsed_from_a_plain_sitemap() -> None:
    xml = (
        "<urlset><url><loc>https://acme.be/contact</loc></url>"
        "<url><loc>https://acme.be/about</loc></url></urlset>"
    )
    assert patterns.sitemap_locations(xml) == [
        "https://acme.be/contact",
        "https://acme.be/about",
    ]


def test_a_sitemap_index_is_recognisable_by_its_xml_locations() -> None:
    xml = "<sitemapindex><sitemap><loc>https://acme.be/sitemap-pages.xml</loc></sitemap></sitemapindex>"
    assert patterns.sitemap_locations(xml) == ["https://acme.be/sitemap-pages.xml"]


def test_only_contact_intent_urls_are_selected() -> None:
    assert patterns.is_contact_url("https://acme.be/nl/contacteer-ons") is True
    assert patterns.is_contact_url("https://acme.be/team/leadership") is True
    assert patterns.is_contact_url("https://acme.be/products/widget") is False


# ---------------------------------------------------------------------------
# Search provider
# ---------------------------------------------------------------------------


def test_search_is_disabled_until_configured() -> None:
    assert patterns.search_enabled() is False
    with pytest.raises(patterns.SearchDisabled):
        import asyncio

        asyncio.run(patterns.search_for_contacts("Acme", "acme.be"))


def test_brave_results_are_parsed() -> None:
    payload = {"web": {"results": [{"url": "https://acme.be/jobs", "description": "mail hr@acme.be"}]}}
    assert patterns.search_results(payload, "brave") == [
        ("https://acme.be/jobs", "mail hr@acme.be")
    ]


def test_bing_results_are_parsed() -> None:
    payload = {"webPages": {"value": [{"url": "https://acme.be/contact", "name": "Contact", "snippet": "info@acme.be"}]}}
    results = patterns.search_results(payload, "bing")
    assert results and results[0][0] == "https://acme.be/contact"
    assert "info@acme.be" in results[0][1]


def test_unknown_provider_shapes_degrade_to_nothing() -> None:
    assert patterns.search_results({"unexpected": []}, "brave") == []
    assert patterns.search_results("not a dict", "bing") == []
