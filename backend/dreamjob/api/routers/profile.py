"""Profile API (FR-101..109, FR-441, FR-442, NFR-402, DR-102).

Uploads, the merge conflict queue, the versioned profile itself, the FR-109
dream-job statement, do-not-disclose flags, the normalised skill list,
evidence items and personas.

Every handler takes the current job seeker from ``current_seeker`` and passes
that id into the repository, which is where tenant isolation is enforced
(FR-101, FR-344).  Uploaded documents are untrusted: they are parsed
deterministically, and only passages the parser cannot segment reach the model,
fenced as untrusted data (NFR-205).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from dreamjob.api.deps import CurrentSeeker, current_seeker
from dreamjob.config import get_settings
from dreamjob.db.repositories import profiles as repo
from dreamjob.llm.client import LLMClient
from dreamjob.pipeline import profile_intake as intake
from dreamjob.pipeline.skills import load_taxonomy, normalise_skill

log = logging.getLogger(__name__)
router = APIRouter()

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_LINKEDIN_TYPES = {".pdf"}
_CV_TYPES = {".pdf", ".docx"}


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ProfileIn(BaseModel):
    """FR-104: the structured form, matching the FR-102 schema."""

    sections: dict[str, Any]
    dream_job_statement: str | None = None
    persona_id: str | None = None


class DreamJobIn(BaseModel):
    """FR-109: free text, the job seeker's own language, no length limit."""

    statement: str


class ConflictResolutionIn(BaseModel):
    resolution: str = Field(pattern="^(linkedin|cv|manual|unresolved)$")
    resolved_value: str | None = None


class DisclosureFlagIn(BaseModel):
    field_path: str
    do_not_disclose: bool = True
    reason: str | None = None


class SkillIn(BaseModel):
    normalised_label: str | None = None
    proficiency: int | None = Field(default=None, ge=1, le=5)
    years_experience: float | None = Field(default=None, ge=0)
    last_used_year: int | None = Field(default=None, ge=1900, le=2100)
    evidence_refs: list[str] | None = None


class EvidenceIn(BaseModel):
    kind: str
    title: str
    url: str | None = None
    description: str | None = None
    linked_skills: list[str] = Field(default_factory=list)
    linked_achievements: list[str] = Field(default_factory=list)
    verification_status: str | None = None


class EvidenceUpdateIn(BaseModel):
    kind: str | None = None
    title: str | None = None
    url: str | None = None
    description: str | None = None
    linked_skills: list[str] | None = None
    linked_achievements: list[str] | None = None
    verification_status: str | None = None


class PersonaIn(BaseModel):
    """FR-442: emphasis, dream-job statement and directive defaults per persona."""

    name: str
    emphasis: str | None = None
    dream_job_statement: str | None = None
    directive_defaults: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False


class PersonaUpdateIn(BaseModel):
    name: str | None = None
    emphasis: str | None = None
    dream_job_statement: str | None = None
    directive_defaults: dict[str, Any] | None = None
    is_default: bool | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _llm_for(seeker: CurrentSeeker) -> LLMClient | None:
    """A client for the extraction fallback, or ``None`` to stay deterministic.

    CR-410: profile text only reaches the remote provider once the job seeker
    has consented; without that, or without a key, parsing degrades to what the
    deterministic reader found (NFR-104).
    """
    settings = get_settings()
    if not settings.deepseek_api_key and not settings.local_llm_base_url:
        return None
    if not repo.llm_transfer_consented(seeker.id) and not settings.local_llm_base_url:
        return None
    return LLMClient(job_seeker_id=seeker.id)


async def _read_upload(upload: UploadFile, allowed: set[str]) -> tuple[str, bytes]:
    filename = upload.filename or "upload"
    suffix = Path(filename).suffix.lower()
    if suffix not in allowed:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"Expected {' or '.join(sorted(allowed))}, got {suffix or 'no extension'}",
        )
    data = await upload.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The uploaded file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"The file exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit",
        )
    return filename, data


def _current_or_404(seeker_id: str) -> dict:
    version = repo.latest_version(seeker_id)
    if version is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No profile yet: upload a LinkedIn export or a CV, or save one manually.",
        )
    return version


def _present(version: dict, seeker_id: str) -> dict:
    """One profile version plus the metadata the profile screens need."""
    sections = version.get("sections") or {}
    return {
        **version,
        "low_confidence_fields": intake.low_confidence_fields(sections),
        "do_not_disclose": sorted(repo.do_not_disclose_paths(seeker_id)),
        "unresolved_conflicts": len(
            repo.list_conflicts(seeker_id, profile_version_id=version["id"], unresolved_only=True)
        ),
        "sources": [
            {k: v for k, v in source.items() if k != "path"}
            for source in (sections.get("_meta") or {}).get("sources") or []
        ],
    }


# ---------------------------------------------------------------------------
# Uploads (FR-102, FR-103, DR-102)
# ---------------------------------------------------------------------------


@router.post("/uploads/linkedin", status_code=status.HTTP_201_CREATED)
async def upload_linkedin(
    file: UploadFile = File(...),
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict:
    """Import a LinkedIn 'Save to PDF' export (FR-102)."""
    filename, data = await _read_upload(file, _LINKEDIN_TYPES)
    result = _ingest(seeker, intake.SOURCE_LINKEDIN, filename, data)
    return _ingest_response(result, seeker.id)


@router.post("/uploads/cv", status_code=status.HTTP_201_CREATED)
async def upload_cv(
    file: UploadFile = File(...),
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict:
    """Import a CV and merge it with the LinkedIn-derived profile (FR-103)."""
    filename, data = await _read_upload(file, _CV_TYPES)
    result = _ingest(seeker, intake.SOURCE_CV, filename, data)
    return _ingest_response(result, seeker.id)


@router.post("/rebuild")
def rebuild(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Re-extract the profile from the retained originals (DR-102)."""
    if not intake.retained_sources(seeker.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "No retained source documents to re-read")
    result = intake.rebuild_profile(seeker.id, llm=_llm_for(seeker))
    return _ingest_response(result, seeker.id)


def _ingest(seeker: CurrentSeeker, kind: str, filename: str, data: bytes) -> dict:
    """Parse and merge one upload, refusing a file that cannot be read at all.

    A damaged or mislabelled document must not replace a profile that already
    parses: the upload is rejected and nothing is retained (FR-102, FR-103).
    """
    try:
        return intake.ingest_document(seeker.id, kind, filename, data, llm=_llm_for(seeker))
    except intake.SourceUnreadable as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


def _ingest_response(result: dict, seeker_id: str) -> dict:
    return {
        "profile": _present(result["version"], seeker_id),
        "conflicts": result["conflicts"],
        "skills": result["skills"],
        "warnings": result["warnings"],
    }


# ---------------------------------------------------------------------------
# The profile itself (FR-104, FR-105)
# ---------------------------------------------------------------------------


@router.get("/")
def get_profile(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    return _present(_current_or_404(seeker.id), seeker.id)


@router.put("/")
def save_profile(body: ProfileIn, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Save an edited profile as a new version (FR-104, FR-105)."""
    version = intake.save_manual_profile(
        seeker.id,
        body.sections,
        dream_job_statement=body.dream_job_statement,
        persona_id=body.persona_id,
    )
    return _present(version, seeker.id)


@router.get("/schema")
def get_schema() -> dict:
    """The FR-102 section list, so the edit form is built from one source."""
    from dreamjob.pipeline.linkedin_pdf import PROFILE_SECTIONS, empty_profile  # noqa: PLC0415

    return {"sections": list(PROFILE_SECTIONS), "empty": empty_profile()}


@router.get("/versions")
def list_versions(seeker: CurrentSeeker = Depends(current_seeker)) -> list[dict]:
    return repo.list_versions(seeker.id)


@router.get("/versions/{version_id}")
def get_version(version_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    version = repo.get_version(seeker.id, version_id)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Profile version not found")
    return _present(version, seeker.id)


@router.post("/versions/{version_id}/restore")
def restore_version(version_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Bring an older version back as the newest one; history stays intact."""
    old = repo.get_version(seeker.id, version_id)
    if old is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Profile version not found")
    version = repo.create_version(
        seeker.id,
        old["sections"],
        source_note=f"restored_from_v{old['version']}",
        dream_job_statement=old.get("dream_job_statement"),
        photo_path=old.get("photo_path"),
        persona_id=old.get("persona_id"),
    )
    intake.carry_conflicts(seeker.id, old["id"], version["id"])
    intake.refresh_skills(seeker.id, version)
    return _present(version, seeker.id)


@router.get("/photo")
def get_photo(seeker: CurrentSeeker = Depends(current_seeker)) -> FileResponse:
    version = _current_or_404(seeker.id)
    path = Path(version.get("photo_path") or "")
    if not version.get("photo_path") or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No profile photo has been extracted")
    return FileResponse(path)


# ---------------------------------------------------------------------------
# Dream-job statement (FR-109)
# ---------------------------------------------------------------------------


@router.get("/dream-job")
def get_dream_job(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    version = repo.latest_version(seeker.id)
    return {
        "statement": (version or {}).get("dream_job_statement"),
        "profile_version": (version or {}).get("version"),
        "profile_version_id": (version or {}).get("id"),
    }


@router.put("/dream-job")
def put_dream_job(body: DreamJobIn, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    version = intake.save_dream_job_statement(seeker.id, body.statement)
    return {
        "statement": version["dream_job_statement"],
        "profile_version": version["version"],
        "profile_version_id": version["id"],
    }


# ---------------------------------------------------------------------------
# Conflicts (FR-103)
# ---------------------------------------------------------------------------


@router.get("/conflicts")
def list_conflicts(
    unresolved_only: bool = False, seeker: CurrentSeeker = Depends(current_seeker)
) -> list[dict]:
    version = repo.latest_version(seeker.id)
    if version is None:
        return []
    return repo.list_conflicts(
        seeker.id, profile_version_id=version["id"], unresolved_only=unresolved_only
    )


@router.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(
    conflict_id: str,
    body: ConflictResolutionIn,
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict:
    if body.resolution == "manual" and not body.resolved_value:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "A manual resolution needs the value to use",
        )
    conflict = repo.resolve_conflict(seeker.id, conflict_id, body.resolution, body.resolved_value)
    if conflict is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conflict not found")
    return conflict


@router.post("/conflicts/apply")
def apply_conflicts(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Write the settled conflicts into a new profile version (FR-103, FR-105)."""
    version = intake.apply_conflict_resolutions(seeker.id)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No profile to update")
    return _present(version, seeker.id)


# ---------------------------------------------------------------------------
# Do-not-disclose flags (FR-106)
# ---------------------------------------------------------------------------


@router.get("/disclosure-flags")
def list_disclosure_flags(seeker: CurrentSeeker = Depends(current_seeker)) -> list[dict]:
    return repo.list_disclosure_flags(seeker.id)


@router.put("/disclosure-flags")
def put_disclosure_flag(
    body: DisclosureFlagIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    """Mark a field as never to be used in generated CVs or emails (FR-106)."""
    return repo.set_disclosure_flag(
        seeker.id,
        body.field_path,
        do_not_disclose=body.do_not_disclose,
        reason=body.reason,
    )


@router.delete("/disclosure-flags")
def delete_disclosure_flag(
    field_path: str = Query(...), seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    removed = repo.delete_disclosure_flag(seeker.id, field_path)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such disclosure flag")
    return {"field_path": field_path, "deleted": removed}


# ---------------------------------------------------------------------------
# Skills (FR-107)
# ---------------------------------------------------------------------------


@router.get("/skills")
def list_skills(seeker: CurrentSeeker = Depends(current_seeker)) -> list[dict]:
    return repo.list_skills(seeker.id)


@router.get("/skills/taxonomy")
def get_taxonomy() -> dict:
    taxonomy, entries = load_taxonomy()
    return {
        "taxonomy": taxonomy,
        "skills": [
            {"code": e.code, "label": e.label, "group": e.group, "synonyms": list(e.synonyms)}
            for e in entries
        ],
    }


@router.get("/skills/normalise")
def normalise(label: str = Query(..., min_length=1)) -> dict:
    match = normalise_skill(label)
    if match is None:
        return {"raw_label": label, "normalised_label": None, "match_kind": "unmatched"}
    return {
        "raw_label": match.raw_label,
        "normalised_label": match.normalised_label,
        "taxonomy": match.taxonomy,
        "taxonomy_code": match.taxonomy_code,
        "match_kind": match.match_kind,
        "confidence": match.confidence,
    }


@router.post("/skills/refresh")
def refresh_skills(seeker: CurrentSeeker = Depends(current_seeker)) -> list[dict]:
    version = _current_or_404(seeker.id)
    return intake.refresh_skills(seeker.id, version, llm=_llm_for(seeker))


@router.put("/skills/{skill_id}")
def update_skill(
    skill_id: str, body: SkillIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    values = body.model_dump(exclude_none=True)
    skill = repo.update_skill(seeker.id, skill_id, values)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    return skill


@router.delete("/skills/{skill_id}")
def delete_skill(skill_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    if not repo.delete_skill(seeker.id, skill_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    return {"id": skill_id, "deleted": True}


# ---------------------------------------------------------------------------
# Evidence items (FR-441)
# ---------------------------------------------------------------------------


@router.get("/evidence")
def list_evidence(
    skill: str | None = None, seeker: CurrentSeeker = Depends(current_seeker)
) -> list[dict]:
    return repo.list_evidence(seeker.id, skill=skill)


@router.post("/evidence", status_code=status.HTTP_201_CREATED)
def create_evidence(body: EvidenceIn, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    if body.kind not in repo.EVIDENCE_KINDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"kind must be one of {', '.join(sorted(repo.EVIDENCE_KINDS))}",
        )
    return repo.create_evidence(seeker.id, body.model_dump(exclude_none=True))


@router.get("/evidence/{evidence_id}")
def get_evidence(evidence_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    item = repo.get_evidence(seeker.id, evidence_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidence item not found")
    return item


@router.put("/evidence/{evidence_id}")
def update_evidence(
    evidence_id: str, body: EvidenceUpdateIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    item = repo.update_evidence(seeker.id, evidence_id, body.model_dump(exclude_none=True))
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidence item not found")
    return item


@router.post("/evidence/{evidence_id}/file")
async def attach_evidence_file(
    evidence_id: str,
    file: UploadFile = File(...),
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict:
    """Attach a certificate, slide deck or reference letter to an evidence item."""
    if repo.get_evidence(seeker.id, evidence_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidence item not found")
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File too large")
    target_dir = get_settings().uploads_dir / seeker.id / "evidence"
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "").suffix[:10]
    target = target_dir / f"{evidence_id}{suffix}"
    target.write_bytes(data)
    return repo.update_evidence(seeker.id, evidence_id, {"file_path": str(target)})  # type: ignore[return-value]


@router.delete("/evidence/{evidence_id}")
def delete_evidence(evidence_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    if not repo.delete_evidence(seeker.id, evidence_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidence item not found")
    return {"id": evidence_id, "deleted": True}


# ---------------------------------------------------------------------------
# Personas (FR-442)
# ---------------------------------------------------------------------------


@router.get("/personas")
def list_personas(seeker: CurrentSeeker = Depends(current_seeker)) -> list[dict]:
    return repo.list_personas(seeker.id)


@router.post("/personas", status_code=status.HTTP_201_CREATED)
def create_persona(body: PersonaIn, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    return repo.create_persona(seeker.id, body.model_dump())


@router.get("/personas/{persona_id}")
def get_persona(persona_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    persona = repo.get_persona(seeker.id, persona_id)
    if persona is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Persona not found")
    return persona


@router.put("/personas/{persona_id}")
def update_persona(
    persona_id: str, body: PersonaUpdateIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    persona = repo.update_persona(seeker.id, persona_id, body.model_dump(exclude_none=True))
    if persona is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Persona not found")
    return persona


@router.post("/personas/{persona_id}/default")
def make_default_persona(
    persona_id: str, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    persona = repo.set_default_persona(seeker.id, persona_id)
    if persona is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Persona not found")
    return persona


@router.delete("/personas/{persona_id}")
def delete_persona(persona_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    if not repo.delete_persona(seeker.id, persona_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Persona not found")
    return {"id": persona_id, "deleted": True}
