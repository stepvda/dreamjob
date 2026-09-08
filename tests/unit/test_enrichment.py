"""Online enrichment, composite profile and dream job model
(FR-121..FR-128, NFR-205, NFR-602, CR-410, RK-02).

Everything here runs against a throwaway SQLite file with no network access and
no LLM key configured, so the LLM paths are exercised through an explicit stub
and the no-LLM paths through the real degradation code.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, utcnow
from dreamjob.db.repositories import enrichment as repo
from dreamjob.pipeline import composite as composite_pipeline
from dreamjob.pipeline import dreamjob_model as dream_pipeline
from dreamjob.pipeline import enrichment as enrichment_pipeline
from dreamjob.pipeline import identity_match as idm

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_SESSION_SECRET",
    "DREAMJOB_ENV",
    "DEEPSEEK_API_KEY",
    "DREAMJOB_LOCAL_LLM_BASE_URL",
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    """A throwaway database, and deliberately no LLM credentials."""
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_SESSION_SECRET"] = secrets.token_urlsafe(32)
    os.environ["DREAMJOB_ENV"] = "development"
    # No key: any accidental call to the provider fails loudly instead of
    # quietly reaching the network from a unit test.
    os.environ["DEEPSEEK_API_KEY"] = ""
    os.environ["DREAMJOB_LOCAL_LLM_BASE_URL"] = ""
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
# Fixtures
# ---------------------------------------------------------------------------

SECTIONS = {
    "name": "Stephane van der Aa",
    "summary": "Data and analytics leader.",
    "experience": [
        {
            "title": "Head of Data",
            "company": "Witysk",
            "location": "Brussels, Belgium",
            "start": "2019-01",
            "end": "2024-06",
        },
        {
            "title": "Analytics Manager",
            "company": "Northwind Logistics",
            "location": "Antwerp, Belgium",
            "start": "2014-03",
            "end": "2018-12",
        },
    ],
    "education": [{"school": "KU Leuven", "end_year": "2008"}],
    "skills": ["Python", "Data governance", "Team leadership"],
    "websites": ["https://stepvda.net", "https://github.com/stepvda"],
}

DREAM_STATEMENT = (
    "I want to lead a small data team in a company that actually ships. "
    "Hybrid, near Brussels. No agencies, no 100% remote."
)


def _seeker(display_name: str = "Stephane van der Aa") -> str:
    return insert_row(
        "job_seeker",
        {
            "email": f"{secrets.token_hex(4)}@example.test",
            "display_name": display_name,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )


def _profile(seeker_id: str, statement: str | None = DREAM_STATEMENT) -> str:
    return insert_row(
        "profile_version",
        {
            "job_seeker_id": seeker_id,
            "version": 1,
            "sections": SECTIONS,
            "dream_job_statement": statement,
            "source_note": "linkedin_pdf",
            "created_at": utcnow(),
        },
    )


def _consent(seeker_id: str, kind: str, granted: bool = True) -> None:
    insert_row(
        "consent_record",
        {
            "job_seeker_id": seeker_id,
            "kind": kind,
            "granted": 1 if granted else 0,
            "detail": "test",
            "granted_at": utcnow(),
        },
    )


class StubLLM:
    """Stands in for ``LLMClient``; records how it was called."""

    def __init__(self, payload: object):
        self.payload = payload
        self.calls: list[dict] = []

    def complete_json(self, task: str, *, system: str, user: str, **kw: object) -> object:
        self.calls.append({"task": task, "system": system, "user": user, **kw})
        return self.payload


def _png(fill: int) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("L", (64, 64), color=fill).save(buffer, format="PNG")
    return buffer.getvalue()


def _anchors() -> idm.ProfileAnchors:
    return idm.build_anchors(SECTIONS, display_name="Stephane van der Aa")


# ---------------------------------------------------------------------------
# FR-123 / RK-02: identity matching
# ---------------------------------------------------------------------------


def test_name_variants_cover_particles_and_handles() -> None:
    variants = idm.name_variants("Stephane van der Aa")
    assert "stephane van der aa" in variants
    assert "van der aa, stephane" in variants
    assert "s. van der aa" in variants
    # A compound surname is routinely abbreviated to its initials in a handle.
    assert "stepvda" in idm.handle_candidates("Stephane van der Aa")


def test_anchors_are_read_out_of_the_profile() -> None:
    anchors = _anchors()
    assert "Witysk" in anchors.employers
    assert any("Brussels" in loc for loc in anchors.locations)
    assert "stepvda.net" in anchors.declared_domains
    assert {2019, 2024, 2014} <= anchors.years


def test_homonym_is_never_confirmed() -> None:
    """RK-02: a name match with nothing corroborating it must not reach a CV."""
    verdict = idm.assess(
        url="https://example.org/people/stephane-van-der-aa",
        title="Stephane van der Aa",
        text=(
            "Stephane van der Aa joined Meridian Shipping in 2003, based in Lyon. "
            "He graduated in 1999 and became CEO of the group in 2011."
        ),
        anchors=_anchors(),
    )
    assert verdict.classification == idm.DOUBTFUL
    assert verdict.auto_merge is False
    conflict = verdict.signal("conflicting_evidence")
    assert conflict is not None and conflict.score == 1.0
    assert "homonym" in verdict.rule


def test_single_corroborating_signal_is_only_probable() -> None:
    verdict = idm.assess(
        url="https://conf.example.com/speakers/svda",
        title="Speaker: Stephane van der Aa",
        text="Stephane van der Aa, Witysk. Session on data platforms.",
        anchors=_anchors(),
    )
    assert verdict.classification == idm.PROBABLE
    assert verdict.auto_merge is False
    assert "RK-02" in verdict.rule


def test_multi_signal_finding_is_confirmed() -> None:
    verdict = idm.assess(
        url="https://press.example.com/interview-2021",
        title="Interview with Stephane van der Aa",
        text=(
            "Stephane van der Aa, Head of Data at Witysk in Brussels, previously at "
            "Northwind Logistics in Antwerp, joined in 2019 after 2014. "
            "More on stepvda.net."
        ),
        anchors=_anchors(),
    )
    assert verdict.classification == idm.CONFIRMED
    assert verdict.auto_merge is True
    names = {s["signal"] for s in verdict.to_signals_json()["signals"]}
    # FR-123: every signal is recorded separately, matched or not.
    assert names == {
        "name_match", "employer_overlap", "cross_link", "location_match",
        "timeline_consistency", "photo_similarity", "conflicting_evidence",
    }


def test_page_on_a_declared_domain_is_confirmed_outright() -> None:
    verdict = idm.assess(
        url="https://stepvda.net/about",
        title="About",
        text="Stephane van der Aa writes here.",
        anchors=_anchors(),
    )
    assert verdict.classification == idm.CONFIRMED
    assert "declared" in verdict.rule


def test_photo_similarity_uses_a_perceptual_hash() -> None:
    from PIL import Image

    def encode(image: Image.Image) -> bytes:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    portrait = encode(Image.radial_gradient("L"))
    other = encode(Image.linear_gradient("L").transpose(Image.Transpose.ROTATE_90))

    same = idm.perceptual_hash(portrait)
    again = idm.perceptual_hash(encode(Image.open(io.BytesIO(portrait)).resize((200, 200))))
    different = idm.perceptual_hash(other)
    assert same is not None and different is not None
    # A rescale is still the same photo; a different image is not.
    assert idm.photo_similarity(same, again) >= 0.9
    assert idm.photo_similarity(same, different) < 0.75
    assert idm.perceptual_hash(b"not an image") is None
    # A flat placeholder carries no information and must not match anything.
    assert idm.perceptual_hash(_png(200)) is None


def test_photo_signal_contributes_when_the_portrait_matches() -> None:
    from PIL import Image

    buffer = io.BytesIO()
    Image.radial_gradient("L").save(buffer, format="PNG")

    anchors = _anchors()
    anchors.photo_hash = idm.perceptual_hash(buffer.getvalue())
    verdict = idm.assess(
        url="https://blog.example.com/author/svda",
        title="Stephane van der Aa",
        text="Posts by Stephane van der Aa.",
        anchors=anchors,
        candidate_photo_hash=anchors.photo_hash,
    )
    photo = verdict.signal("photo_similarity")
    assert photo is not None and photo.score == 1.0


# ---------------------------------------------------------------------------
# FR-122: query material and search parsing
# ---------------------------------------------------------------------------


def test_queries_are_built_from_the_profile_anchors() -> None:
    queries = enrichment_pipeline.build_queries(_anchors(), limit=10)
    assert queries[0] == '"Stephane van der Aa"'
    assert any("Witysk" in q for q in queries)
    assert any("Brussels" in q for q in queries)
    assert all(q.startswith('"') for q in queries)


def test_candidate_urls_prefer_declared_links_then_handles() -> None:
    urls = enrichment_pipeline.candidate_urls(_anchors())
    assert urls[0] == "https://stepvda.net"
    assert "https://github.com/stepvda" in urls
    assert not any("linkedin.com" in u for u in urls)


DDG_HTML = """
<html><body><div id="links">
  <div class="result results_links web-result">
    <h2 class="result__title">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fstepvda.net%2F&amp;rut=x">
        stepvda.net
      </a>
    </h2>
    <a class="result__snippet">Personal site of Stephane van der Aa.</a>
  </div>
  <div class="result result--ad">
    <h2 class="result__title"><a class="result__a" href="https://ad.example.com">Ad</a></h2>
  </div>
</div></body></html>
"""

DDG_BOT_CHECK = """
<html><body><div class="anomaly-modal__modal">
  <div class="anomaly-modal__title">Unfortunately, bots use DuckDuckGo too.</div>
</div></body></html>
"""


def test_duckduckgo_results_are_parsed_and_unwrapped() -> None:
    hits = enrichment_pipeline.parse_ddg_results(DDG_HTML, query='"Stephane van der Aa"')
    assert len(hits) == 1  # the ad is skipped
    assert hits[0].url == "https://stepvda.net/"
    assert "Personal site" in hits[0].snippet


def test_bot_check_is_a_degradation_not_an_empty_result() -> None:
    with pytest.raises(enrichment_pipeline.SearchUnavailable):
        enrichment_pipeline.parse_ddg_results(DDG_BOT_CHECK)


def test_page_text_and_portrait_extraction() -> None:
    html = (
        '<html><head><title>About</title>'
        '<meta property="og:image" content="/img/me.jpg"></head>'
        "<body><script>alert(1)</script><p>Hello  world</p></body></html>"
    )
    assert enrichment_pipeline.page_title(html) == "About"
    assert enrichment_pipeline.page_text(html) == "Hello world"
    assert (
        enrichment_pipeline.portrait_url(html, "https://stepvda.net/about")
        == "https://stepvda.net/img/me.jpg"
    )


def test_extract_finding_without_llm_falls_back_to_a_heuristic() -> None:
    facts = enrichment_pipeline.extract_finding(
        None, url="https://stepvda.net", title="Home", text="Some text", anchors=_anchors()
    )
    assert facts["extraction"] == "heuristic"
    assert facts["facts"] == []
    assert facts["excerpt"] == "Some text"


# ---------------------------------------------------------------------------
# FR-127: special-category data is never stored
# ---------------------------------------------------------------------------


def test_special_categories_are_stripped_before_storage() -> None:
    payload = {
        "facts": [
            {"statement": "Led the data team at Witysk", "category": "role"},
            {"statement": "Board member of a political party", "category": "other"},
        ],
        "health_notes": "irrelevant",
        "identity_clues": {"employers": ["Witysk"]},
    }
    cleaned, removed = enrichment_pipeline.strip_special_categories(payload)
    assert "health_notes" not in cleaned
    statements = [f.get("statement") for f in cleaned["facts"]]
    assert statements == ["Led the data team at Witysk"]
    assert len(removed) >= 2
    assert cleaned["identity_clues"] == {"employers": ["Witysk"]}
    # The censored fact is dropped whole; no hollow fragment is left behind.
    assert all(f.get("statement") for f in cleaned["facts"])


# ---------------------------------------------------------------------------
# FR-124: the review queue and permanent rejection
# ---------------------------------------------------------------------------


def test_rejected_finding_is_never_reproposed() -> None:
    seeker = _seeker()
    finding_id = repo.save_finding(
        seeker,
        url="https://example.org/other-person",
        title="Someone else",
        extracted_facts={"facts": []},
        identity_signals={"signals": []},
        identity_score=0.3,
        classification="doubtful",
    )
    assert finding_id is not None

    repo.set_finding_status(seeker, finding_id, "rejected", permanent=True)
    assert repo.rejected_urls(seeker) == {"https://example.org/other-person"}

    # A later run rediscovers the same URL and must not resurrect it.
    assert (
        repo.save_finding(
            seeker,
            url="https://example.org/other-person",
            title="Someone else",
            extracted_facts={"facts": [{"statement": "new"}]},
            identity_signals={"signals": []},
            identity_score=0.9,
            classification="confirmed",
        )
        is None
    )
    stored = repo.get_finding(seeker, finding_id)
    assert stored is not None
    assert stored["status"] == "rejected" and stored["identity_score"] == 0.3


def test_only_confirmed_findings_are_usable_without_a_decision() -> None:
    seeker = _seeker()
    for url, classification in (
        ("https://a.example/1", "confirmed"),
        ("https://b.example/2", "probable"),
        ("https://c.example/3", "doubtful"),
    ):
        repo.save_finding(
            seeker, url=url, title=url, extracted_facts={}, identity_signals={},
            identity_score=0.5, classification=classification,
        )
    assert {f["url"] for f in repo.usable_findings(seeker)} == {"https://a.example/1"}

    probable = repo.list_findings(seeker, classification="probable")[0]
    repo.set_finding_status(seeker, probable["id"], "accepted")
    assert {f["url"] for f in repo.usable_findings(seeker)} == {
        "https://a.example/1",
        "https://b.example/2",
    }


# ---------------------------------------------------------------------------
# FR-126: enrichment can be switched off entirely
# ---------------------------------------------------------------------------


def test_disabled_enrichment_makes_no_requests() -> None:
    seeker = _seeker()
    _profile(seeker)
    report = asyncio.run(enrichment_pipeline.run_enrichment(seeker))
    assert report.enabled is False
    assert report.pages_fetched == 0 and report.findings == []

    repo.set_enrichment_enabled(seeker, True)
    assert repo.enrichment_enabled(seeker) is True


# ---------------------------------------------------------------------------
# NFR-602: versioned prompt templates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["composite_profile", "dream_job_model", "enrichment_finding"]
)
def test_prompts_are_versioned_and_renderable(name: str) -> None:
    prompt = enrichment_pipeline.load_prompt(name)
    assert prompt.version and prompt.version[0].isdigit()
    assert prompt.task and prompt.meta.get("requirements")
    system, user = prompt.render(
        language="en", enrichment_state="enabled", person="X", employers="Y",
        locations="Z", url="https://example.test",
    )
    assert "{{" not in system and "{{" not in user
    assert system and user


# ---------------------------------------------------------------------------
# FR-121, FR-125, CR-410, NFR-205: the composite profile
# ---------------------------------------------------------------------------


COMPOSITE_PAYLOAD = {
    "narrative": {"id": "narrative:1", "text": "Data leader in Brussels."},
    "seniority": {"level": "senior_manager", "text": "Leads a data function."},
    "career_trajectory": [{"id": "career_trajectory:1", "text": "Analytics to Head of Data."}],
    "core_competencies": ["Data governance"],
    "public_footprint": [
        {"id": "public_footprint:1", "text": "Personal site", "url": "https://stepvda.net"},
        {"id": "public_footprint:2", "text": "Invented talk"},
    ],
    "evidence_refs": {
        "narrative:1": {"source_type": "linkedin_export", "source_ref": "summary"},
        "seniority:1": {"source_type": "cv", "source_ref": "experience[0]"},
        "career_trajectory:1": {"source_type": "profile", "source_ref": "experience"},
        "public_footprint:1": {"source_type": "web", "source_ref": "https://stepvda.net"},
        "public_footprint:2": {"source_type": "web", "source_ref": "https://never-fetched.test"},
    },
}


def _confirmed_finding(seeker: str) -> None:
    repo.save_finding(
        seeker,
        url="https://stepvda.net",
        title="stepvda.net",
        extracted_facts={"page_kind": "personal_site", "facts": []},
        identity_signals={"signals": []},
        identity_score=0.9,
        classification="confirmed",
    )


def test_composite_requires_llm_transfer_consent() -> None:
    """CR-410: nothing goes to the provider before the consent is recorded."""
    seeker = _seeker()
    _profile(seeker)
    with pytest.raises(composite_pipeline.ConsentRequired):
        composite_pipeline.build_composite(seeker, llm=StubLLM(COMPOSITE_PAYLOAD))


def test_composite_build_stubbed_llm() -> None:
    seeker = _seeker()
    _profile(seeker)
    _consent(seeker, "llm_transfer")
    _consent(seeker, "enrichment")
    _confirmed_finding(seeker)

    llm = StubLLM(COMPOSITE_PAYLOAD)
    stored = composite_pipeline.build_composite(seeker, llm=llm)

    assert stored["version"] == 1
    assert stored["narrative"] == "Data leader in Brussels."
    assert stored["seniority"]["level"] == "senior_manager"

    refs = stored["evidence_refs"]
    # FR-125: every statement is traceable.
    for statement_id in refs["_meta"]["statement_ids"]:
        assert statement_id in refs
    # A bare-string statement still gets an id and a ref.
    assert stored["core_competencies"][0]["id"] == "core_competencies:1"
    assert refs["core_competencies:1"]["source_type"] == "unsupported"
    # RK-02: a citation to a URL enrichment never confirmed is demoted.
    assert refs["public_footprint:2"]["source_type"] == "unsupported"
    assert refs["public_footprint:2"]["rejected_ref"] == "https://never-fetched.test"
    assert refs["public_footprint:1"]["source_type"] == "web"

    # NFR-205: profile and findings travel as untrusted blocks, not instructions.
    call = llm.calls[0]
    assert call["task"] == "profile.composite"
    assert set(call["untrusted"]) == {"profile", "web_findings"}
    assert "Witysk" not in call["user"] and "Witysk" not in call["system"]
    assert "Witysk" in call["untrusted"]["profile"]
    assert call["prompt_version"] == enrichment_pipeline.load_prompt("composite_profile").version


def test_composite_without_enrichment_ignores_findings() -> None:
    """FR-126: the composite is built from user data only."""
    seeker = _seeker()
    _profile(seeker)
    _consent(seeker, "llm_transfer")
    _confirmed_finding(seeker)

    llm = StubLLM(COMPOSITE_PAYLOAD)
    composite_pipeline.build_composite(seeker, include_enrichment=False, llm=llm)
    assert set(llm.calls[0]["untrusted"]) == {"profile"}


def test_structural_composite_when_no_llm_is_configured() -> None:
    seeker = _seeker()
    _profile(seeker)
    stored = composite_pipeline.build_composite(seeker)

    assert stored["_generation"]["mode"] == "structural"
    trajectory = stored["career_trajectory"]
    assert trajectory[0]["text"].startswith("Head of Data at Witysk")
    # CR-405: every line restates a profile field and cites it.
    assert stored["evidence_refs"]["career_trajectory:1"]["source_ref"] == "experience[0]"
    assert stored["evidence_refs"]["_meta"]["unsupported"] == []


def test_composite_edits_are_recorded_and_attributed() -> None:
    seeker = _seeker()
    _profile(seeker)
    stored = composite_pipeline.build_composite(seeker)

    edited = composite_pipeline.edit_composite(
        seeker,
        stored["id"],
        {"achievements": [{"text": "Cut reporting latency by half"}]},
    )
    assert edited is not None
    assert edited["edited_by_user"] == 1
    assert edited["achievements"][0]["text"] == "Cut reporting latency by half"
    ref = edited["evidence_refs"]["achievements:1"]
    assert ref["source_type"] == "user_input"

    rows = composite_pipeline.statements(edited)
    assert any(r["block"] == "achievements" for r in rows)
    assert all(r["source"] for r in rows)


# ---------------------------------------------------------------------------
# FR-128: the dream job model
# ---------------------------------------------------------------------------


DREAM_PAYLOAD = {
    "target_roles": [
        {
            "title": "Head of Data", "priority": 9, "source": "stated",
            "quote": "lead a small data team",
        },
        {"title": "", "priority": 1},
    ],
    "role_families": [{"family": "data & analytics leadership", "examples": ["Data Manager"]}],
    "responsibilities": [{"activity": "Lead a small team", "importance": "critical"}],
    "company_characteristics": [
        {"attribute": "geography", "value": "near Brussels", "importance": "must"},
        {"attribute": "nonsense", "value": "ships fast", "importance": "strong"},
    ],
    "culture_values": [{"cue": "ships fast", "polarity": "seek"}],
    "deal_breakers": [
        {"constraint": "agencies", "hard": True, "detectable_from": ["company type"]},
        {"constraint": "fully remote", "hard": False},
    ],
    "implicit_preferences": [{"preference": "hands-on leadership", "confidence": 3}],
    "summary": "A hands-on data leadership role near Brussels.",
}


def test_dream_job_model_stubbed_llm() -> None:
    seeker = _seeker()
    _profile(seeker)
    _consent(seeker, "llm_transfer")

    llm = StubLLM(DREAM_PAYLOAD)
    stored = dream_pipeline.build_dream_job_model(seeker, llm=llm)

    assert stored["version"] == 1
    assert stored["statement"] == DREAM_STATEMENT
    assert stored["confirmed_by_user"] == 0

    roles = stored["target_roles"]
    assert len(roles) == 1  # the empty title is dropped
    assert roles[0]["priority"] == 5  # clamped into 1..5
    assert stored["responsibilities"][0]["importance"] == "strong"  # unknown -> default
    assert stored["company_characteristics"][1]["attribute"] == "mission"  # unknown -> default
    assert stored["implicit_preferences"][0]["confidence"] == 1.0  # clamped into 0..1
    assert stored["role_families"][0]["example_titles"] == ["Data Manager"]

    # NFR-205: the free-text statement is data, never instruction text.
    call = llm.calls[0]
    assert call["task"] == "profile.dreamjob"
    assert call["untrusted"]["statement"] == DREAM_STATEMENT
    assert DREAM_STATEMENT not in call["user"]


def test_dream_job_model_confirmation_and_reads_for_other_slices() -> None:
    seeker = _seeker()
    _profile(seeker)
    _consent(seeker, "llm_transfer")
    stored = dream_pipeline.build_dream_job_model(seeker, llm=StubLLM(DREAM_PAYLOAD))

    assert dream_pipeline.latest(seeker, confirmed_only=True) is None
    dream_pipeline.confirm(seeker, stored["id"])
    confirmed = dream_pipeline.latest(seeker, confirmed_only=True)
    assert confirmed is not None and confirmed["confirmed_by_user"] == 1

    assert "Head of Data" in dream_pipeline.as_query_terms(confirmed)
    assert "data & analytics leadership" in dream_pipeline.as_query_terms(confirmed)
    assert [d["constraint"] for d in dream_pipeline.hard_deal_breakers(confirmed)] == ["agencies"]
    assert [m.get("value") for m in dream_pipeline.must_haves(confirmed)] == ["near Brussels"]


def test_dream_job_model_without_llm_keeps_the_statement_verbatim() -> None:
    seeker = _seeker()
    _profile(seeker)
    stored = dream_pipeline.build_dream_job_model(seeker)
    assert stored["_generation"]["mode"] == "unparsed"
    assert stored["statement"] == DREAM_STATEMENT
    assert stored["target_roles"] == []


def test_dream_job_model_needs_a_statement() -> None:
    seeker = _seeker()
    _profile(seeker, statement=None)
    with pytest.raises(dream_pipeline.StatementMissing):
        dream_pipeline.build_dream_job_model(seeker)


# ---------------------------------------------------------------------------
# RK-02 regression: a declared account does not make the whole platform "ours"
# ---------------------------------------------------------------------------


def test_declared_platform_account_does_not_confirm_a_stranger() -> None:
    """A live run confirmed github.com/<stranger> because github.com/<seeker>
    was declared.  Ownership is a URL prefix on a shared platform, not a domain.
    """
    anchors = _anchors()
    assert anchors.is_self_declared("https://github.com/stepvda") is True
    assert anchors.is_self_declared("https://github.com/stepvda/some-repo") is True
    assert anchors.is_self_declared("https://github.com/someone-else") is False
    # A domain of the job seeker's own carries over to its whole path space.
    assert anchors.is_self_declared("https://stepvda.net/writing/2024") is True

    verdict = idm.assess(
        url="https://github.com/someone-else",
        title="someone-else (Someone Else)",
        text="Repositories by someone else. 2016 2018 2020. joined GitHub in 2016.",
        anchors=anchors,
    )
    assert verdict.classification != idm.CONFIRMED
    assert verdict.auto_merge is False


def test_handles_are_taken_from_declared_links_first() -> None:
    anchors = _anchors()
    # github.com/stepvda and stepvda.net both point at the same real handle.
    assert anchors.handles[0] == "stepvda"
    urls = enrichment_pipeline.candidate_urls(anchors)
    assert "https://gitlab.com/stepvda" in urls
    # The platform's own name is never mistaken for a username.
    assert "github" not in anchors.handles


def test_a_wholly_special_category_extraction_collapses_to_nothing() -> None:
    """FR-127: the page may be recorded; its content must not be."""
    cleaned, removed = enrichment_pipeline.strip_special_categories(
        {"title": "Diagnosis and recovery", "facts": []}
    )
    assert cleaned is None
    assert removed


def test_anchor_name_falls_back_to_the_account_name_through_the_repository() -> None:
    """CR-408: profile_version carries no name, so the account name is read -
    from the repository, never with SQL issued by the pipeline.
    """
    seeker_id = _seeker("Stephane van der Aa")
    insert_row(
        "profile_version",
        {
            "job_seeker_id": seeker_id,
            "version": 1,
            "sections": {"experience": [{"title": "Head of Data", "company": "Witysk"}]},
            "source_note": "cv",
            "created_at": utcnow(),
        },
    )
    anchors = enrichment_pipeline.anchors_for_seeker(seeker_id)
    assert anchors.display_name == "Stephane van der Aa"
    assert "stephane van der aa" in anchors.variants
    assert anchors.handles
    assert repo.seeker_display_name(seeker_id) == "Stephane van der Aa"
    assert repo.seeker_display_name("no-such-seeker") == ""
