"""Profile intake, versions, skills, evidence and personas (FR-101..109, FR-441/442).

Everything here runs offline.  The two source documents are the product
owner's real ones, because the parsers exist to read exactly those shapes: a
two-column LinkedIn export whose contact rail interleaves with the body, and a
single-table DOCX CV with the photograph in ``word/media``.  The LLM fallback
is exercised with a stub client, never a live call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dreamjob.config import REPO_ROOT, get_settings
from dreamjob.db.connection import insert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import profiles as repo
from dreamjob.pipeline import profile_intake as intake
from dreamjob.pipeline.cv_parser import parse_cv
from dreamjob.pipeline.linkedin_pdf import parse_date_range, parse_linkedin_pdf
from dreamjob.pipeline.skills import build_skills, normalise_labels, normalise_skill

LINKEDIN_PDF = REPO_ROOT / "docs" / "Profile.pdf"
CV_DOCX = REPO_ROOT / "docs" / "CV - Stephane van der Aa.docx"

needs_linkedin = pytest.mark.skipif(not LINKEDIN_PDF.is_file(), reason="sample export missing")
needs_cv = pytest.mark.skipif(not CV_DOCX.is_file(), reason="sample CV missing")


@pytest.fixture
def db(tmp_path, monkeypatch) -> str:
    """An isolated migrated database with one job seeker in it."""
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "dreamjob.db"))
    get_settings.cache_clear()
    migrate()
    seeker_id = insert_row(
        "job_seeker",
        {
            "email": "profile@example.test",
            "display_name": "Test Seeker",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    yield seeker_id
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# FR-102: the LinkedIn export defines the schema
# ---------------------------------------------------------------------------


@needs_linkedin
def test_linkedin_export_columns_are_separated() -> None:
    doc = parse_linkedin_pdf(LINKEDIN_PDF)
    contact = doc.sections["contact"]

    # The rail and the body interleave in the raw text stream; only a
    # position-aware read keeps the name out of the address.
    assert contact["name"] == "Stephane van der Aa"
    assert contact["headline"] == "AI Data Science at BeCode"
    assert contact["location"] == "Brussels, Brussels Region, Belgium"
    assert contact["email"] == "stepvda@mac.com"
    assert contact["phone"] == "+32493701601"
    assert contact["address"] == "Van Volxemlaan 208 bus 31"
    assert "linkedin.com/in/stepvda" in contact["linkedin_url"]
    assert {w["label"] for w in contact["websites"]} == {"LinkedIn", "Personal"}
    assert doc.unsegmented == {}


@needs_linkedin
def test_linkedin_export_sections() -> None:
    doc = parse_linkedin_pdf(LINKEDIN_PDF)
    assert doc.sections["top_skills"] == [
        "Artificial Intelligence (AI)", "GitHub", "Data Analysis"
    ]
    assert {lang["language"] for lang in doc.sections["languages"]} == {
        "English", "German", "Dutch", "French"
    }
    assert doc.sections["summary"].startswith("Motivation, Vision, Faith to Believe.")

    roles = [(e["company"], e["title"], e["start"], e["end"]) for e in doc.sections["experience"]]
    assert ("BeCode", "AI Data Science", "2026-05", None) in roles
    assert (
        "NGA Human Resources", "Enterprise Strategy and Portfolio Director", "2014-11", "2023-10"
    ) in roles
    assert ("Fujitsu", "Senior Consultant", "1999-07", "2004-12") in roles
    # Six roles share one company header; each keeps the employer.
    assert sum(1 for c, *_ in roles if c == "NGA Human Resources") == 5

    education = doc.sections["education"][0]
    assert education["school"] == "Hogeschool Gent"
    assert (education["start_year"], education["end_year"]) == (1996, 1999)
    assert education["degree"].startswith("Graduaat")


def test_date_ranges() -> None:
    assert parse_date_range("November 2014 - October 2023 (9 years)").start == "2014-11"
    assert parse_date_range("November 2014 - October 2023 (9 years)").end == "2023-10"
    present = parse_date_range("May 2026 - Present (5 months)")
    assert present.end is None and present.current is True
    assert parse_date_range("2016 – 2020").start == "2016"
    assert parse_date_range("no dates here at all") is None


# ---------------------------------------------------------------------------
# FR-103: the CV and its photograph
# ---------------------------------------------------------------------------


@needs_cv
def test_cv_docx_is_read_with_its_photo(db: str) -> None:
    doc = parse_cv(CV_DOCX)
    assert doc.sections["contact"]["name"] == "Stephane van der Aa"
    assert doc.sections["contact"]["email"] == "stephane@stepvda.com"
    assert doc.sections["contact"]["location"] == "Brussels, Belgium"

    titles = [e["title"] for e in doc.sections["experience"]]
    assert "Strategy Director" in titles
    assert "Senior Consultant" in titles
    strategy = next(e for e in doc.sections["experience"] if e["title"] == "Strategy Director")
    assert strategy["company"].startswith("Alight Solutions")
    assert (strategy["start"], strategy["end"]) == ("2016", "2020")
    assert strategy["description"]

    assert [p["title"] for p in doc.sections["publications"]][:1] == [
        "Neuro-Rights: The Battle for the Last Private Place"
    ]
    assert {lang["language"] for lang in doc.sections["languages"]} == {
        "Dutch", "French", "English", "German"
    }

    # The apply phase reads the portrait back from profile_version.photo_path.
    assert doc.photo is not None and doc.photo[:2] == b"\xff\xd8"
    from dreamjob.pipeline.cv_parser import save_photo

    saved = Path(save_photo(doc.photo, db, get_settings().uploads_dir))
    assert saved.name == "photo.jpg"
    assert saved.read_bytes() == doc.photo


def test_unsupported_cv_format_is_rejected(tmp_path) -> None:
    bad = tmp_path / "cv.doc"
    bad.write_bytes(b"legacy")
    with pytest.raises(ValueError, match="not supported"):
        parse_cv(bad)


# ---------------------------------------------------------------------------
# FR-103: the merge surfaces the real disagreements
# ---------------------------------------------------------------------------


@needs_linkedin
@needs_cv
def test_merge_flags_the_conflicts_between_the_two_documents(db: str) -> None:
    intake.ingest_document(db, intake.SOURCE_LINKEDIN, "Profile.pdf", LINKEDIN_PDF.read_bytes())
    result = intake.ingest_document(db, intake.SOURCE_CV, "CV.docx", CV_DOCX.read_bytes())

    by_path = {c["field_path"]: c for c in result["conflicts"]}
    sections = result["version"]["sections"]

    # The strategy role: LinkedIn says it ran to October 2023, the CV to 2020.
    strategy = next(
        i
        for i, e in enumerate(sections["experience"])
        if e["title"] == "Enterprise Strategy and Portfolio Director"
    )
    assert by_path[f"experience.{strategy}.end"]["value_linkedin"] == "2023-10"
    assert by_path[f"experience.{strategy}.end"]["value_cv"] == "2020"
    assert by_path[f"experience.{strategy}.start"]["value_cv"] == "2016"
    assert by_path[f"experience.{strategy}.title"]["value_cv"] == "Strategy Director"

    # The same employer under two names is one employer, and one conflict.
    employer = by_path[f"experience.{strategy}.company"]
    assert employer["value_linkedin"] == "NGA Human Resources"
    assert employer["value_cv"].startswith("Alight Solutions")

    assert by_path["contact.email"]["value_cv"] == "stephane@stepvda.com"
    assert all(c["resolution"] == "unresolved" for c in result["conflicts"])

    # "Hogeschool Gent" and "Hogeschool Gent (HOGENT)" are one school, so the
    # degrees are compared rather than listed as two separate studies.
    assert len(sections["education"]) == 1
    assert by_path["education.0.degree"]["value_cv"] == "Computer Science"

    # A date only one source carries is a gap, not a conflict: it is filled in.
    assert "experience.4.start" not in by_path or by_path["experience.4.start"]["value_cv"]

    # The photo travelled from the CV onto the version the apply phase reads.
    assert Path(result["version"]["photo_path"]).is_file()
    assert result["version"]["source_note"] == "cv"


@needs_linkedin
@needs_cv
def test_resolving_a_conflict_writes_a_new_version(db: str) -> None:
    intake.ingest_document(db, intake.SOURCE_LINKEDIN, "Profile.pdf", LINKEDIN_PDF.read_bytes())
    result = intake.ingest_document(db, intake.SOURCE_CV, "CV.docx", CV_DOCX.read_bytes())
    strategy = next(
        i
        for i, e in enumerate(result["version"]["sections"]["experience"])
        if e["title"] == "Enterprise Strategy and Portfolio Director"
    )
    conflict = next(
        c for c in result["conflicts"] if c["field_path"] == f"experience.{strategy}.end"
    )
    repo.resolve_conflict(db, conflict["id"], "cv", None)

    version = intake.apply_conflict_resolutions(db)
    assert version["version"] == 3
    assert version["sections"]["experience"][strategy]["end"] == "2020"
    # FR-105: the earlier reading is still on record.
    assert len(repo.list_versions(db)) == 3
    assert repo.get_version(db, result["version"]["id"])["sections"]["experience"][strategy][
        "end"
    ] == "2023-10"
    # An unresolved conflict survives the rewrite so it can still be settled.
    assert repo.list_conflicts(db, profile_version_id=version["id"], unresolved_only=True)


@needs_linkedin
def test_reingesting_the_same_document_replaces_rather_than_duplicates(db: str) -> None:
    first = intake.ingest_document(
        db, intake.SOURCE_LINKEDIN, "Profile.pdf", LINKEDIN_PDF.read_bytes()
    )
    second = intake.ingest_document(
        db, intake.SOURCE_LINKEDIN, "Profile.pdf", LINKEDIN_PDF.read_bytes()
    )
    assert second["version"]["version"] == first["version"]["version"] + 1
    assert len(second["version"]["sections"]["_meta"]["sources"]) == 1
    assert len(second["version"]["sections"]["experience"]) == len(
        first["version"]["sections"]["experience"]
    )


@needs_linkedin
def test_retained_sources_allow_re_extraction(db: str) -> None:
    """DR-102: the originals stay on disk so extractors can be re-run."""
    intake.ingest_document(db, intake.SOURCE_LINKEDIN, "Profile.pdf", LINKEDIN_PDF.read_bytes())
    sources = intake.retained_sources(db)
    assert len(sources) == 1
    assert Path(sources[0]["path"]).is_file()
    assert sources[0]["sha256"]
    rebuilt = intake.rebuild_profile(db)
    assert rebuilt["version"]["version"] == 2
    assert rebuilt["version"]["sections"]["contact"]["name"] == "Stephane van der Aa"


def test_company_variants_link_a_renamed_employer() -> None:
    linkedin = intake.company_variants("NGA Human Resources")
    cv = intake.company_variants("Alight Solutions (formerly NGA Human Resources)")
    assert linkedin & cv
    assert not intake.company_variants("Fujitsu") & intake.company_variants("Accenture")
    # An acronym in brackets is a second name for the same organisation; a
    # country in brackets is not.
    assert intake.company_variants("Hogeschool Gent (HOGENT)") & intake.company_variants("HOGENT")
    assert not intake.company_variants("Acme (Belgium)") & intake.company_variants("Foo (Belgium)")


# ---------------------------------------------------------------------------
# FR-104, FR-105, FR-109
# ---------------------------------------------------------------------------


def test_manual_edit_versions_and_sanitises_rich_text(db: str) -> None:
    sections = {
        "contact": {"name": "Test Seeker", "websites": []},
        "summary": "<p>Designs <strong>systems</strong>.</p><script>steal()</script>",
        "experience": [
            {
                "company": "ACME",
                "title": "Engineer",
                "start": "2020-01",
                "end": None,
                "description": '<ul><li>Shipped</li></ul><a href="javascript:x()">click</a>',
            }
        ],
    }
    version = intake.save_manual_profile(db, sections, dream_job_statement="Something honest.")
    assert version["version"] == 1
    assert version["source_note"] == "manual"
    assert "<script>" not in version["sections"]["summary"]
    assert "<strong>systems</strong>" in version["sections"]["summary"]
    description = version["sections"]["experience"][0]["description"]
    assert "<ul><li>Shipped</li></ul>" in description
    assert "javascript:" not in description

    second = intake.save_manual_profile(db, sections)
    assert second["version"] == 2
    # FR-109: the statement rides along with the profile version.
    assert second["dream_job_statement"] == "Something honest."


def test_dream_job_statement_has_no_length_limit(db: str) -> None:
    intake.save_manual_profile(db, {"contact": {"name": "T"}, "experience": []})
    statement = "I want work that matters. " * 500
    version = intake.save_dream_job_statement(db, statement)
    assert version["dream_job_statement"] == statement
    assert version["version"] == 2
    assert repo.latest_version(db)["dream_job_statement"] == statement


def test_paths_address_nested_profile_fields() -> None:
    sections = {"experience": [{"title": "A"}, {"title": "B"}], "contact": {"email": "x@y.z"}}
    assert intake.get_path(sections, "experience.1.title") == "B"
    assert intake.set_path(sections, "experience.1.title", "C") is True
    assert sections["experience"][1]["title"] == "C"
    assert intake.set_path(sections, "experience.9.title", "D") is False
    assert intake.get_path(sections, "nothing.here", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# FR-106: do not disclose
# ---------------------------------------------------------------------------


def test_do_not_disclose_paths_are_per_seeker(db: str) -> None:
    other = insert_row(
        "job_seeker",
        {
            "email": "other@example.test",
            "display_name": "Other",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    repo.set_disclosure_flag(db, "contact.photo", reason="Not on Belgian CVs")
    repo.set_disclosure_flag(db, "contact.address")
    repo.set_disclosure_flag(other, "contact.email")

    assert repo.do_not_disclose_paths(db) == {"contact.photo", "contact.address"}
    assert repo.do_not_disclose_paths(other) == {"contact.email"}

    # Setting the same path twice updates rather than duplicates.
    repo.set_disclosure_flag(db, "contact.address", do_not_disclose=False)
    assert repo.do_not_disclose_paths(db) == {"contact.photo"}
    assert repo.delete_disclosure_flag(db, "contact.address") == 1


def test_redaction_uses_the_disclosure_paths(db: str) -> None:
    """The FR-106 export is what the LLM layer redacts with (CR-410)."""
    from dreamjob.llm.client import redact

    repo.set_disclosure_flag(db, "contact.address")
    payload = {"contact": {"name": "T", "address": "Van Volxemlaan 208"}}
    cleaned = redact(payload, repo.do_not_disclose_paths(db))
    assert cleaned == {"contact": {"name": "T"}}


# ---------------------------------------------------------------------------
# FR-107: normalised skills with proficiency and recency
# ---------------------------------------------------------------------------


def test_bundled_taxonomy_matches_labels_and_synonyms() -> None:
    assert normalise_skill("Python").normalised_label == "Python"
    assert normalise_skill("py").normalised_label == "Python"
    assert normalise_skill("Artificial Intelligence (AI)").taxonomy_code == "ai.ai"
    assert normalise_skill("object oriented analysis").normalised_label == "Object-oriented design"
    phrase = normalise_skill("hands-on machine learning at scale")
    assert phrase.normalised_label == "Machine learning"
    assert phrase.match_kind == "token"
    assert normalise_skill("x") is None


def test_unmatched_labels_are_kept_but_flagged_low_confidence() -> None:
    matches = normalise_labels(["Python", "Zorkblat frobnication"], llm=None)
    by_label = {m.normalised_label: m for m in matches}
    assert by_label["Python"].confidence == 1.0
    assert by_label["Zorkblat frobnication"].match_kind == "verbatim"
    assert by_label["Zorkblat frobnication"].confidence < 0.5


def test_llm_fallback_is_used_only_for_leftovers() -> None:
    class StubLLM:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def complete_json(self, task, system, user, *, untrusted=None, **kw):
            labels = json.loads(untrusted["labels"])
            self.calls.append(labels)
            return {"mappings": [{"input": labels[0], "taxonomy_label": "Kubernetes"}]}

    llm = StubLLM()
    matches = normalise_labels(["Python", "container orchestration at scale"], llm=llm)
    assert llm.calls == [["container orchestration at scale"]]
    assert {m.normalised_label for m in matches} == {"Python", "Kubernetes"}
    assert next(m for m in matches if m.normalised_label == "Kubernetes").match_kind == "llm"


@needs_linkedin
def test_skills_get_duration_and_recency_from_the_timeline() -> None:
    from datetime import UTC, datetime

    doc = parse_linkedin_pdf(LINKEDIN_PDF)
    now = datetime(2026, 9, 8, tzinfo=UTC)
    skills = {m.normalised_label: m for m in build_skills(doc.sections, now=now)}

    ecm = skills["Enterprise content management"]
    assert ecm.years_experience and ecm.years_experience > 4
    assert ecm.last_used_year == 2011
    assert 1 <= ecm.proficiency <= 5

    strategy = skills["Business strategy"]
    assert strategy.last_used_year == 2023
    assert strategy.years_experience and strategy.years_experience > 8
    assert strategy.proficiency >= 4
    # Recency counts: a skill last used fifteen years ago scores below a
    # skill of the same duration that is still in use.
    assert strategy.proficiency > ecm.proficiency


@needs_linkedin
def test_skill_rows_are_written_for_the_version(db: str) -> None:
    result = intake.ingest_document(
        db, intake.SOURCE_LINKEDIN, "Profile.pdf", LINKEDIN_PDF.read_bytes()
    )
    rows = repo.list_skills(db)
    assert rows and len(rows) == len(result["skills"])
    assert all(r["profile_version_id"] == result["version"]["id"] for r in rows)
    assert all(r["taxonomy"] in {"esco", "verbatim"} for r in rows)

    updated = repo.update_skill(db, rows[0]["id"], {"proficiency": 5})
    assert updated["proficiency"] == 5
    assert repo.update_skill(db, "nope", {"proficiency": 1}) is None


# ---------------------------------------------------------------------------
# NFR-402: confidence and provenance per field
# ---------------------------------------------------------------------------


@needs_linkedin
@needs_cv
def test_every_extracted_field_carries_confidence_and_source(db: str) -> None:
    intake.ingest_document(db, intake.SOURCE_LINKEDIN, "Profile.pdf", LINKEDIN_PDF.read_bytes())
    result = intake.ingest_document(db, intake.SOURCE_CV, "CV.docx", CV_DOCX.read_bytes())
    confidence = result["version"]["sections"]["_meta"]["field_confidence"]

    assert confidence["contact.name"]["source"] == "linkedin_pdf"
    assert confidence["contact.name"]["confidence"] >= 0.9
    assert all(0.0 <= v["confidence"] <= 1.0 for v in confidence.values())

    # A role both documents describe is scored above one only LinkedIn has,
    # and the provenance names the documents that agreed.
    sections = result["version"]["sections"]
    both = next(
        i for i, e in enumerate(sections["experience"]) if len(e["sources"]) == 2
    )
    only_one = next(
        i for i, e in enumerate(sections["experience"]) if len(e["sources"]) == 1
    )
    assert confidence[f"experience.{both}.company"]["source"] == "linkedin_pdf+cv"
    assert (
        confidence[f"experience.{both}.company"]["confidence"]
        > confidence[f"experience.{only_one}.company"]["confidence"]
    )
    assert intake.low_confidence_fields(sections, threshold=1.0)


# ---------------------------------------------------------------------------
# FR-441 evidence, FR-442 personas
# ---------------------------------------------------------------------------


def test_evidence_crud_and_skill_linking(db: str) -> None:
    item = repo.create_evidence(
        db,
        {
            "kind": "repository",
            "title": "TI One Voice",
            "url": "https://one.witysk.org",
            "linked_skills": ["Python", "FastAPI"],
            "linked_achievements": ["Built a documentation platform"],
        },
    )
    assert item["verification_status"] == "unverified"
    assert item["linked_skills"] == ["Python", "FastAPI"]
    assert repo.list_evidence(db, skill="python") == [item]
    assert repo.list_evidence(db, skill="rust") == []

    updated = repo.update_evidence(db, item["id"], {"verification_status": "verified"})
    assert updated["verification_status"] == "verified"
    assert repo.get_evidence("someone-else", item["id"]) is None
    assert repo.delete_evidence(db, item["id"]) == 1
    assert repo.get_evidence(db, item["id"]) is None


@needs_linkedin
def test_evidence_is_attached_to_the_skills_it_supports(db: str) -> None:
    repo.create_evidence(
        db, {"kind": "publication", "title": "Coding with AI", "linked_skills": ["Python"]}
    )
    intake.ingest_document(db, intake.SOURCE_LINKEDIN, "Profile.pdf", LINKEDIN_PDF.read_bytes())
    python = next(r for r in repo.list_skills(db) if r["normalised_label"] == "Python")
    assert len(python["evidence_refs"]) == 1


def test_personas_keep_exactly_one_default(db: str) -> None:
    architect = repo.create_persona(
        db,
        {
            "name": "Architect",
            "emphasis": "systems and design",
            "dream_job_statement": "Design systems that outlive me.",
            "directive_defaults": {"seniority": ["principal"]},
        },
    )
    # FR-442: the first persona becomes the default without being asked.
    assert architect["is_default"] == 1
    assert architect["directive_defaults"] == {"seniority": ["principal"]}

    leader = repo.create_persona(db, {"name": "Product leader"})
    assert leader["is_default"] == 0

    repo.set_default_persona(db, leader["id"])
    assert repo.default_persona(db)["id"] == leader["id"]
    assert repo.get_persona(db, architect["id"])["is_default"] == 0

    # Deleting the default hands the flag on rather than leaving none.
    repo.delete_persona(db, leader["id"])
    assert repo.default_persona(db)["id"] == architect["id"]
    assert repo.get_persona("someone-else", architect["id"]) is None


def test_persona_scopes_a_profile_version(db: str) -> None:
    persona = repo.create_persona(db, {"name": "Researcher"})
    version = intake.save_manual_profile(
        db, {"contact": {"name": "T"}, "experience": []}, persona_id=persona["id"]
    )
    assert version["persona_id"] == persona["id"]
    assert repo.latest_version(db, persona_id=persona["id"])["id"] == version["id"]
    assert repo.latest_version(db, persona_id="other-persona") is None


# ---------------------------------------------------------------------------
# The HTTP surface (FR-101: every route is scoped to the caller)
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db: str):
    from dreamjob.api.deps import SESSION_COOKIE
    from dreamjob.main import create_app
    from dreamjob.security.crypto import hash_token
    from fastapi.testclient import TestClient

    token = "test-session-token"
    insert_row(
        "session",
        {
            "job_seeker_id": db,
            "token_hash": hash_token(token),
            "expires_at": "2999-01-01T00:00:00+00:00",
            "created_at": utcnow(),
        },
    )
    with TestClient(create_app()) as http:
        http.cookies.set(SESSION_COOKIE, token)
        yield http


def test_api_requires_a_session(db: str) -> None:
    from dreamjob.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as http:
        assert http.get("/api/profile/").status_code == 401
        assert http.get("/api/profile/personas").status_code == 401


@needs_linkedin
@needs_cv
def test_upload_merge_and_resolve_over_http(client) -> None:
    assert client.get("/api/profile/").status_code == 404

    response = client.post(
        "/api/profile/uploads/linkedin",
        files={"file": ("Profile.pdf", LINKEDIN_PDF.read_bytes(), "application/pdf")},
    )
    assert response.status_code == 201
    assert response.json()["profile"]["sections"]["contact"]["name"] == "Stephane van der Aa"

    response = client.post(
        "/api/profile/uploads/cv",
        files={
            "file": (
                "CV.docx",
                CV_DOCX.read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["profile"]["unresolved_conflicts"] == len(body["conflicts"])
    assert body["profile"]["version"] == 2
    # Retained originals are described, but their on-disk paths are not exposed.
    assert body["profile"]["sources"] and "path" not in body["profile"]["sources"][0]

    conflict = next(c for c in body["conflicts"] if c["field_path"].endswith(".end"))
    assert client.post(
        f"/api/profile/conflicts/{conflict['id']}/resolve", json={"resolution": "cv"}
    ).status_code == 200
    applied = client.post("/api/profile/conflicts/apply").json()
    assert applied["version"] == 3

    assert client.get("/api/profile/photo").status_code == 200
    assert len(client.get("/api/profile/versions").json()) == 3
    assert client.get("/api/profile/skills").json()
    assert client.get("/api/profile/schema").json()["sections"][0] == "contact"


def test_upload_rejects_the_wrong_file_type(client) -> None:
    response = client.post(
        "/api/profile/uploads/linkedin",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 415
    assert client.post("/api/profile/rebuild").status_code == 409


def test_dream_job_and_disclosure_over_http(client) -> None:
    client.put(
        "/api/profile/",
        json={"sections": {"contact": {"name": "T"}, "experience": []}},
    )
    statement = "Work I would do anyway. " * 200
    assert client.put("/api/profile/dream-job", json={"statement": statement}).status_code == 200
    assert client.get("/api/profile/dream-job").json()["statement"] == statement

    client.put(
        "/api/profile/disclosure-flags",
        json={"field_path": "contact.photo", "reason": "Not on Belgian CVs"},
    )
    assert client.get("/api/profile/").json()["do_not_disclose"] == ["contact.photo"]
    assert client.request(
        "DELETE", "/api/profile/disclosure-flags", params={"field_path": "contact.photo"}
    ).status_code == 200
    assert client.get("/api/profile/disclosure-flags").json() == []


def test_persona_and_evidence_over_http(client) -> None:
    persona = client.post(
        "/api/profile/personas",
        json={"name": "Architect", "dream_job_statement": "Systems that outlive me."},
    ).json()
    assert persona["is_default"] == 1
    second = client.post("/api/profile/personas", json={"name": "Researcher"}).json()
    assert client.post(f"/api/profile/personas/{second['id']}/default").json()["is_default"] == 1
    assert client.get(f"/api/profile/personas/{persona['id']}").json()["is_default"] == 0
    assert client.delete(f"/api/profile/personas/{second['id']}").status_code == 200
    assert client.get("/api/profile/personas/missing").status_code == 404

    evidence = client.post(
        "/api/profile/evidence",
        json={"kind": "repository", "title": "TI One Voice", "linked_skills": ["Python"]},
    ).json()
    assert client.post(
        "/api/profile/evidence", json={"kind": "gossip", "title": "x"}
    ).status_code == 422
    attached = client.post(
        f"/api/profile/evidence/{evidence['id']}/file",
        files={"file": ("cert.pdf", b"%PDF-1.4 stub", "application/pdf")},
    ).json()
    assert Path(attached["file_path"]).is_file()
    assert client.get("/api/profile/evidence", params={"skill": "Python"}).json()
    assert client.delete(f"/api/profile/evidence/{evidence['id']}").status_code == 200


def test_pdf_cv_is_parsed_too(tmp_path) -> None:
    """FR-103 accepts PDF as well as DOCX; the same section builder reads both."""
    reportlab = pytest.importorskip("reportlab")
    assert reportlab
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    path = tmp_path / "cv.pdf"
    pdf = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for size, text in [
        (18, "Alex Mercier"),
        (10, "alex@example.test  |  Brussels, Belgium"),
        (14, "EXPERIENCE"),
        (10, "Data Engineer  ·  Acme NV  ·  Brussels    2019 - 2024"),
        (10, "- Built ETL pipelines in Python and Airflow."),
        (14, "EDUCATION"),
        (10, "Computer Science  ·  KU Leuven  ·  Leuven    2015 - 2019"),
        (14, "SKILLS"),
        (10, "Python, SQL, Apache Airflow"),
    ]:
        pdf.setFont("Helvetica", size)
        pdf.drawString(60, y, text)
        y -= 26
    pdf.save()

    doc = parse_cv(path)
    titles = [e["title"] for e in doc.sections["experience"]]
    assert "Data Engineer" in titles
    role = doc.sections["experience"][0]
    assert role["company"] == "Acme NV"
    assert (role["start"], role["end"]) == ("2019", "2024")
    assert "ETL" in (role["description"] or "")
    assert doc.sections["education"][0]["school"] == "KU Leuven"
    assert "Python" in doc.sections["top_skills"]


# ---------------------------------------------------------------------------
# Regressions found during verification
# ---------------------------------------------------------------------------


def test_script_body_is_dropped_with_its_tag() -> None:
    """FR-104: removing <script> must take the code with it, not leave it as text."""
    assert intake.sanitise_rich_text("<script>steal()</script>") == ""
    assert intake.sanitise_rich_text("<style>p{color:red}</style>Hello") == "Hello"
    assert "onerror" not in intake.sanitise_rich_text("<img src=x onerror=alert(1)>")


@needs_linkedin
def test_an_unreadable_upload_leaves_the_profile_alone(client) -> None:
    """A damaged file must not replace a profile that parses (FR-102, FR-103)."""
    client.post(
        "/api/profile/uploads/linkedin",
        files={"file": ("Profile.pdf", LINKEDIN_PDF.read_bytes(), "application/pdf")},
    )
    good = client.get("/api/profile/").json()

    response = client.post(
        "/api/profile/uploads/cv",
        files={"file": ("broken.docx", b"PK\x03\x04 not a docx", "application/octet-stream")},
    )
    assert response.status_code == 422

    after = client.get("/api/profile/").json()
    assert after["version"] == good["version"]
    assert after["sections"]["contact"]["name"] == "Stephane van der Aa"
    # The unreadable file is not kept, so a later rebuild cannot trip over it.
    assert [s["kind"] for s in intake.retained_sources(after["job_seeker_id"])] == ["linkedin_pdf"]
    rebuilt = client.post("/api/profile/rebuild")
    assert rebuilt.status_code == 200
    assert rebuilt.json()["profile"]["sections"]["contact"]["name"] == "Stephane van der Aa"


@needs_linkedin
@needs_cv
def test_conflicts_and_skills_survive_a_later_save(client) -> None:
    """Both hang off a version id, so a derived version must inherit them."""
    client.post(
        "/api/profile/uploads/linkedin",
        files={"file": ("Profile.pdf", LINKEDIN_PDF.read_bytes(), "application/pdf")},
    )
    merged = client.post(
        "/api/profile/uploads/cv",
        files={"file": ("CV.docx", CV_DOCX.read_bytes(), "application/octet-stream")},
    ).json()
    open_conflicts = len(merged["conflicts"])
    skills = len(client.get("/api/profile/skills").json())
    assert open_conflicts and skills

    # FR-109: writing the statement changes nothing else about the profile.
    client.put("/api/profile/dream-job", json={"statement": "Work that compounds."})
    assert len(client.get("/api/profile/conflicts").json()) == open_conflicts
    assert len(client.get("/api/profile/skills").json()) == skills

    # FR-104: editing one field must not hide the rest of the merge queue.
    client.put("/api/profile/", json={"sections": merged["profile"]["sections"]})
    assert client.get("/api/profile/").json()["unresolved_conflicts"] == open_conflicts

    # FR-105: a restore brings the queue back with the sections it belongs to.
    first = client.get("/api/profile/versions").json()[-1]["id"]
    restored = client.post(f"/api/profile/versions/{first}/restore").json()
    assert restored["version"] == 5
    assert client.get("/api/profile/skills").json()


@needs_linkedin
def test_retained_originals_are_visible_to_erasure(client, db: str) -> None:
    """DR-102 keeps the upload; FR-108 has to be able to find it again."""
    from dreamjob.db.repositories import seekers

    client.post(
        "/api/profile/uploads/linkedin",
        files={"file": ("Profile.pdf", LINKEDIN_PDF.read_bytes(), "application/pdf")},
    )
    retained = intake.retained_sources(db)
    assert [s["kind"] for s in retained] == ["linkedin_pdf"]
    assert set(retained[0]) >= {"path", "sha256", "byte_size"}
    assert retained[0]["path"] in seekers.private_file_paths(db)

    # A newer export of the same kind replaces the old file rather than
    # leaving an orphan nothing points at.
    superseded = Path(retained[0]["path"])
    client.post(
        "/api/profile/uploads/linkedin",
        files={"file": ("Profile.pdf", LINKEDIN_PDF.read_bytes() + b"\n%new", "application/pdf")},
    )
    assert not superseded.exists()
    assert len(intake.retained_sources(db)) == 1
