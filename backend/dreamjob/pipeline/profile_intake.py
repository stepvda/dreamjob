"""Profile intake: merge, conflicts, versioning and disclosure (FR-103..109).

This module is the write path for the profile.  It takes the retained source
documents (FR-102 LinkedIn export, FR-103 CV) plus manual edits, folds them
into one set of sections, and appends a ``profile_version`` (FR-105).

The two documents disagree, and they are meant to: a CV is written for an
audience while a LinkedIn profile accumulates.  In the product owner's own
pair, the same sixteen-year employer appears as "NGA Human Resources" and as
"Alight Solutions (formerly NGA Human Resources)", and the strategy role runs
to 2023 in one and to 2020 in the other.  Silently preferring one source would
put an unverified date into a generated CV, so every disagreement over an
employer, a title or a date becomes a ``profile_conflict`` row for the job
seeker to settle (FR-103); the merged sections carry the LinkedIn reading
until they do.

Sources are kept on disk under ``data/uploads/<seeker>/sources`` and re-parsed
on every merge (DR-102): when an extractor improves, the profile can be
rebuilt from the originals without asking for another upload.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from dreamjob.config import get_settings
from dreamjob.db.repositories import profiles as repo
from dreamjob.pipeline.cv_parser import parse_cv, save_photo
from dreamjob.pipeline.linkedin_pdf import (
    ParsedDocument,
    empty_profile,
    llm_rescue,
    parse_linkedin_pdf,
    year_of,
)
from dreamjob.pipeline.skills import build_skills

log = logging.getLogger(__name__)

SOURCE_LINKEDIN = "linkedin_pdf"
SOURCE_CV = "cv"
LOW_CONFIDENCE = 0.6


class SourceUnreadable(ValueError):
    """An uploaded document could not be opened at all (FR-102, FR-103).

    Raised only for a fresh upload: the file is discarded and the profile the
    job seeker already has is left untouched, rather than being replaced by an
    empty version parsed from an unreadable file.
    """

_LEGAL_SUFFIXES = re.compile(
    r"\b(nv|sa|bv|bvba|sprl|gmbh|ag|ltd|limited|llc|inc|plc|s\.?a\.?s|srl|"
    r"oy|ab|as|aps|kft|spa|pte|pty|corp|corporation|company|group|holding|"
    r"solutions|consulting|services|international)\b",
    re.IGNORECASE,
)
_FORMERLY = re.compile(r"\((?:formerly|now|ex|previously|part of)\s+([^)]+)\)", re.IGNORECASE)
_PARENTHETICAL = re.compile(r"\(([^()]*)\)")


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


@dataclass
class MergeResult:
    sections: dict[str, Any]
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    field_confidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    photo: bytes | None = None


def company_variants(name: str | None) -> set[str]:
    """Every name a single employer may be written under.

    "Alight Solutions (formerly NGA Human Resources)" is one employer with two
    names; matching on either is what pairs the CV entry with the LinkedIn one
    so the *dates* can be compared rather than treated as separate jobs.
    """
    if not name:
        return set()
    variants = {name}
    for match in _FORMERLY.finditer(name):
        variants.add(match.group(1))
    # "Hogeschool Gent (HOGENT)" is the same school as "Hogeschool Gent";
    # a parenthetical is only itself a name when it reads as an acronym, so
    # "(Belgium)" does not make two unrelated employers look alike.
    variants.add(_PARENTHETICAL.sub(" ", name))
    for inner in _PARENTHETICAL.findall(name):
        token = inner.strip()
        if len(token) >= 3 and token.isupper():
            variants.add(token)
    out = set()
    for variant in variants:
        cleaned = re.sub(r"[^a-z0-9 &/]+", " ", variant.lower())
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            continue
        out.add(cleaned)
        for part in re.split(r"\s*/\s*", cleaned):
            part = part.strip()
            if part:
                out.add(part)
                stripped = re.sub(r"\s+", " ", _LEGAL_SUFFIXES.sub(" ", part)).strip()
                if len(stripped) >= 3:
                    out.add(stripped)
    return {v for v in out if len(v) >= 2}


def _title_tokens(title: str | None) -> set[str]:
    if not title:
        return set()
    text = re.sub(r"[^a-z0-9 ]+", " ", title.lower())
    stop = {"and", "of", "the", "for", "at", "in", "a", "senior", "global", "enterprise"}
    return {t for t in text.split() if t and t not in stop}


def _similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _dates_overlap(x: dict, y: dict) -> float:
    xs, xe = year_of(x.get("start")), year_of(x.get("end"))
    ys, ye = year_of(y.get("start")), year_of(y.get("end"))
    if xs is None or ys is None:
        return 0.0
    this_year = datetime.now(UTC).year
    xe, ye = xe or this_year, ye or this_year
    overlap = min(xe, ye) - max(xs, ys)
    span = max(xe - xs, ye - ys, 1)
    return max(0.0, overlap / span)


def _pair_score(li: dict, cv: dict) -> float:
    shared = company_variants(li.get("company")) & company_variants(cv.get("company"))
    employer = 1.0 if shared else 0.0
    title = _similarity(_title_tokens(li.get("title")), _title_tokens(cv.get("title")))
    dates = _dates_overlap(li, cv)
    return employer * 0.5 + title * 0.35 + dates * 0.15


def _match_experience(
    linkedin: list[dict], cv: list[dict]
) -> tuple[list[tuple[int, int]], set[int], set[int]]:
    """Greedy best-first pairing of roles across the two sources."""
    candidates = [
        (_pair_score(li, c), i, j)
        for i, li in enumerate(linkedin)
        for j, c in enumerate(cv)
    ]
    candidates.sort(reverse=True)
    pairs: list[tuple[int, int]] = []
    used_li: set[int] = set()
    used_cv: set[int] = set()
    for score, i, j in candidates:
        if score < 0.5 or i in used_li or j in used_cv:
            continue
        pairs.append((i, j))
        used_li.add(i)
        used_cv.add(j)
    return (
        pairs,
        {i for i in range(len(linkedin)) if i not in used_li},
        {j for j in range(len(cv)) if j not in used_cv},
    )


def _same_date(a: str | None, b: str | None) -> bool:
    """Year-only dates from a CV must not fight month-level LinkedIn dates."""
    if a == b:
        return True
    if not a or not b:
        return False
    if len(str(a)) == 4 or len(str(b)) == 4:
        return year_of(a) == year_of(b)
    return False


def _conflict(path: str, li: Any, cv: Any) -> dict[str, Any]:
    return {
        "field_path": path,
        "value_linkedin": None if li is None else str(li),
        "value_cv": None if cv is None else str(cv),
    }


_CONTACT_COMPARED = ("name", "email", "phone", "location")


def merge_documents(
    linkedin: ParsedDocument | None,
    cv: ParsedDocument | None,
    *,
    base: dict[str, Any] | None = None,
) -> MergeResult:
    """Fold the LinkedIn export and the CV into one profile (FR-103).

    LinkedIn supplies the skeleton because FR-102 makes its section structure
    the schema; the CV fills gaps and contributes its own sections.  Nothing is
    overwritten silently - a disagreement produces a conflict row instead.
    """
    sections = base or empty_profile()
    conflicts: list[dict[str, Any]] = []
    confidence: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    primary = linkedin or cv
    if primary is None:
        return MergeResult(sections=sections, warnings=["No source document to merge."])
    for doc in (cv, linkedin):
        if doc is not None:
            confidence.update(doc.field_confidence)
            warnings.extend(doc.warnings)

    li_sections = linkedin.sections if linkedin else empty_profile()
    cv_sections = cv.sections if cv else empty_profile()

    # -- contact ------------------------------------------------------------
    contact = dict(li_sections["contact"])
    for key, value in cv_sections["contact"].items():
        if key == "websites":
            known = {w["url"].lower() for w in contact["websites"]}
            contact["websites"].extend(w for w in value if w["url"].lower() not in known)
            continue
        if contact.get(key) in (None, "", []):
            contact[key] = value
        elif (
            key in _CONTACT_COMPARED
            and value
            and linkedin is not None
            and cv is not None
            and str(value).strip().lower() != str(contact[key]).strip().lower()
        ):
            conflicts.append(_conflict(f"contact.{key}", contact[key], value))
    sections["contact"] = contact

    # -- summary and free text ---------------------------------------------
    sections["summary"] = li_sections["summary"] or cv_sections["summary"]
    if linkedin and cv and li_sections["summary"] and cv_sections["summary"]:
        sections.setdefault("other", {})
        sections["other"]["cv_summary"] = cv_sections["summary"]
    other = {**(li_sections.get("other") or {}), **(cv_sections.get("other") or {})}
    other.update(sections.get("other") or {})
    sections["other"] = other

    # -- experience ---------------------------------------------------------
    li_exp = li_sections["experience"]
    cv_exp = cv_sections["experience"]
    pairs, li_only, cv_only = _match_experience(li_exp, cv_exp)
    merged_exp: list[dict[str, Any]] = []
    role_conflicts: list[dict[str, Any]] = []
    for i, entry in enumerate(li_exp):
        merged = dict(entry)
        merged["sources"] = [SOURCE_LINKEDIN]
        merged["_slot"] = len(merged_exp)
        match = next((j for a, j in pairs if a == i), None)
        if match is not None:
            merged = _merge_role(merged, cv_exp[match], merged["_slot"], role_conflicts)
        merged_exp.append(merged)
    for j in sorted(cv_only):
        entry = dict(cv_exp[j])
        entry["sources"] = [SOURCE_CV]
        entry["_slot"] = len(merged_exp)
        merged_exp.append(entry)
    # Roles are presented newest first, so the conflict paths - which the API
    # and the disclosure flags both address by index - are rewritten to match.
    merged_exp.sort(key=_sort_key, reverse=True)
    slots = {entry.pop("_slot"): index for index, entry in enumerate(merged_exp)}
    for conflict in role_conflicts:
        prefix, slot, leaf = conflict["field_path"].split(".", 2)
        conflict["field_path"] = f"{prefix}.{slots[int(slot)]}.{leaf}"
    conflicts.extend(role_conflicts)
    sections["experience"] = merged_exp
    if li_only and cv is not None:
        warnings.append(
            f"{len(li_only)} LinkedIn role(s) have no counterpart in the CV."
        )

    # -- education ----------------------------------------------------------
    sections["education"] = _merge_education(
        li_sections["education"], cv_sections["education"], conflicts
    )

    # NFR-402: the parsers scored fields against their own entry order, which
    # the merge has just changed.  Re-score by merged position, and let a fact
    # both documents carry outrank one only a single document states.
    confidence = {
        k: v for k, v in confidence.items() if not k.startswith(("experience.", "education."))
    }
    _score_entries(confidence, "experience", sections["experience"], ("company", "title", "start"))
    _score_entries(confidence, "education", sections["education"], ("school", "degree"))

    # -- list sections ------------------------------------------------------
    for key in ("top_skills", "interests"):
        sections[key] = _merge_str_list(li_sections[key], cv_sections[key])
    sections["languages"] = _merge_by_key(
        li_sections["languages"], cv_sections["languages"], "language"
    )
    for key in ("certifications", "publications", "projects", "honors",
                "volunteering", "recommendations", "courses"):
        sections[key] = _merge_by_key(li_sections[key], cv_sections[key], "title")

    return MergeResult(
        sections=sections,
        conflicts=conflicts,
        field_confidence=confidence,
        warnings=warnings,
        photo=(cv.photo if cv and cv.photo else (linkedin.photo if linkedin else None)),
    )


def _score_entries(
    confidence: dict[str, dict[str, Any]],
    key: str,
    entries: list[dict],
    fields: tuple[str, ...],
) -> None:
    for index, entry in enumerate(entries):
        sources = entry.get("sources") or []
        corroborated = len(sources) > 1
        label = "+".join(sources) or "unknown"
        for field_name in fields:
            score = 0.3 if not entry.get(field_name) else (0.97 if corroborated else 0.88)
            confidence[f"{key}.{index}.{field_name}"] = {
                "confidence": score,
                "source": label,
            }


def _sort_key(entry: dict) -> tuple[int, int]:
    start = year_of(entry.get("start")) or 0
    end = year_of(entry.get("end")) or 9999
    return (end, start)


def _merge_role(
    merged: dict[str, Any], cv_entry: dict[str, Any], index: int, conflicts: list[dict]
) -> dict[str, Any]:
    """Combine one paired role, recording every disagreement (FR-103)."""
    merged["sources"] = [SOURCE_LINKEDIN, SOURCE_CV]
    path = f"experience.{index}"

    if cv_entry.get("company") and merged.get("company") != cv_entry["company"]:
        conflicts.append(_conflict(f"{path}.company", merged.get("company"), cv_entry["company"]))
    if cv_entry.get("title") and merged.get("title") != cv_entry["title"]:
        if _similarity(_title_tokens(merged.get("title")), _title_tokens(cv_entry["title"])) < 0.99:
            conflicts.append(_conflict(f"{path}.title", merged.get("title"), cv_entry["title"]))
    for bound in ("start", "end"):
        # Only a real disagreement is a conflict; a date one source simply
        # does not carry is a gap, and gaps are filled in silently below.
        if (
            merged.get(bound)
            and cv_entry.get(bound)
            and not _same_date(merged.get(bound), cv_entry.get(bound))
        ):
            conflicts.append(_conflict(f"{path}.{bound}", merged.get(bound), cv_entry.get(bound)))

    for key in ("location", "start", "end", "duration_raw"):
        if merged.get(key) in (None, "") and cv_entry.get(key):
            merged[key] = cv_entry[key]
    if cv_entry.get("description"):
        if merged.get("description"):
            merged["description_cv"] = cv_entry["description"]
        else:
            merged["description"] = cv_entry["description"]
    return merged


def _merge_education(
    li_items: list[dict], cv_items: list[dict], conflicts: list[dict]
) -> list[dict]:
    out = [dict(item) for item in li_items]
    for item in out:
        item["sources"] = [SOURCE_LINKEDIN]
    for cv_item in cv_items:
        target = next(
            (
                item
                for item in out
                if company_variants(item.get("school")) & company_variants(cv_item.get("school"))
            ),
            None,
        )
        if target is None:
            out.append({**cv_item, "sources": [SOURCE_CV]})
            continue
        index = out.index(target)
        target["sources"] = [SOURCE_LINKEDIN, SOURCE_CV]
        for key in ("degree", "start_year", "end_year"):
            li_value, cv_value = target.get(key), cv_item.get(key)
            if li_value and cv_value and str(li_value) != str(cv_value):
                conflicts.append(_conflict(f"education.{index}.{key}", li_value, cv_value))
            elif not li_value and cv_value:
                target[key] = cv_value
        for key in ("field", "description", "location"):
            if not target.get(key) and cv_item.get(key):
                target[key] = cv_item[key]
    return out


def _merge_str_list(a: list[Any], b: list[Any]) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    for item in [*a, *b]:
        key = str(item).strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _merge_by_key(a: list[dict], b: list[dict], key: str) -> list[dict]:
    out: list[dict] = [dict(item) for item in a]
    index = {str(item.get(key, "")).strip().lower(): item for item in out}
    for item in b:
        ident = str(item.get(key, "")).strip().lower()
        existing = index.get(ident)
        if existing is None:
            out.append(dict(item))
            index[ident] = out[-1]
            continue
        for field_name, value in item.items():
            if existing.get(field_name) in (None, "", []) and value:
                existing[field_name] = value
    return out


# ---------------------------------------------------------------------------
# Conflict resolution (FR-103)
# ---------------------------------------------------------------------------


def apply_resolutions(sections: dict[str, Any], conflicts: list[dict]) -> dict[str, Any]:
    """Write settled conflicts back into the sections."""
    for conflict in conflicts:
        resolution = conflict.get("resolution")
        if resolution in (None, "", "unresolved"):
            continue
        if resolution == "blank":
            # The job seeker chose to leave the field out entirely - drop it
            # rather than substituting either of the conflicting values.
            clear_path(sections, conflict["field_path"])
            continue
        if resolution == "linkedin":
            value = conflict.get("value_linkedin")
        elif resolution == "cv":
            value = conflict.get("value_cv")
        else:
            value = conflict.get("resolved_value")
        set_path(sections, conflict["field_path"], value)
    return sections


def clear_path(sections: dict[str, Any], path: str) -> bool:
    """Remove the value at ``path``.

    ``set_path`` writes a value; ``clear_path`` removes it.  A scalar leaf in a
    dict is deleted; a list element is set to ``None`` (not popped, because
    popping would shift the indices that the profile's other conflicts and
    paths reference).  "Leave blank" then means the fact is absent, never that
    a slot in the middle of the array disappeared.
    """
    node: Any = sections
    parts = path.split(".")
    for part in parts[:-1]:
        if isinstance(node, list):
            if not part.isdigit() or int(part) >= len(node):
                return False
            node = node[int(part)]
        elif isinstance(node, dict):
            if part not in node:
                return False
            node = node[part]
        else:
            return False
    leaf = parts[-1]

    if isinstance(node, list) and leaf.isdigit():
        idx = int(leaf)
        if idx >= len(node):
            return False
        node[idx] = None
        return True
    if isinstance(node, dict):
        if leaf not in node:
            return False
        if node.get(leaf) in (None, "", [], {}):
            del node[leaf]
        else:
            node[leaf] = None
        return True
    return False


def set_path(sections: dict[str, Any], path: str, value: Any) -> bool:
    node: Any = sections
    parts = path.split(".")
    for part in parts[:-1]:
        if isinstance(node, list):
            if not part.isdigit() or int(part) >= len(node):
                return False
            node = node[int(part)]
        elif isinstance(node, dict):
            if part not in node:
                return False
            node = node[part]
        else:
            return False
    leaf = parts[-1]
    if isinstance(node, list) and leaf.isdigit() and int(leaf) < len(node):
        node[int(leaf)] = value
        return True
    if isinstance(node, dict):
        node[leaf] = value
        return True
    return False


def get_path(sections: dict[str, Any], path: str, default: Any = None) -> Any:
    node: Any = sections
    for part in path.split("."):
        if isinstance(node, list):
            if not part.isdigit() or int(part) >= len(node):
                return default
            node = node[int(part)]
        elif isinstance(node, dict):
            if part not in node:
                return default
            node = node[part]
        else:
            return default
    return node


# ---------------------------------------------------------------------------
# FR-104: manual editing with rich text in descriptions
# ---------------------------------------------------------------------------

_ALLOWED_TAGS = {
    "p", "br", "strong", "b", "em", "i", "u", "ul", "ol", "li",
    "h3", "h4", "a", "code", "blockquote",
}
_ALLOWED_ATTRS = {"a": {"href", "title"}}
# Elements whose *text* is code rather than prose: dropping the tag alone would
# leave the script body behind as visible text.
_VOID_CONTENT_TAGS = {"script", "style", "template", "title"}
_RICH_TEXT_PATHS = re.compile(r"(^|\.)(description|description_cv|summary|detail|notes)$")


class _Sanitiser(HTMLParser):
    """Allowlist HTML cleaner for rich-text descriptions (FR-104, NFR-206).

    Rich text arrives from a browser editor, so it is treated as untrusted:
    anything outside the allowlist is dropped rather than escaped-and-kept, and
    ``javascript:`` URLs never survive.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._open: list[str] = []
        self._muted = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID_CONTENT_TAGS:
            self._muted += 1
            return
        if tag not in _ALLOWED_TAGS:
            return
        allowed = _ALLOWED_ATTRS.get(tag, set())
        rendered = []
        for name, value in attrs:
            if name not in allowed or value is None:
                continue
            if name == "href" and not re.match(r"^(https?:|mailto:|/|#)", value.strip(), re.I):
                continue
            rendered.append(f' {name}="{html.escape(value, quote=True)}"')
        self.parts.append(f"<{tag}{''.join(rendered)}>")
        if tag != "br":
            self._open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_CONTENT_TAGS:
            self._muted = max(0, self._muted - 1)
            return
        if tag in _ALLOWED_TAGS and tag in self._open:
            self.parts.append(f"</{tag}>")
            self._open.remove(tag)

    def handle_data(self, data: str) -> None:
        if self._muted:
            return
        self.parts.append(html.escape(data, quote=False))

    def result(self) -> str:
        return "".join(self.parts) + "".join(f"</{t}>" for t in reversed(self._open))


def sanitise_rich_text(value: str) -> str:
    parser = _Sanitiser()
    parser.feed(value)
    parser.close()
    return parser.result()


def sanitise_sections(sections: Any, path: str = "") -> Any:
    """Clean every rich-text field in a submitted profile (FR-104)."""
    if isinstance(sections, dict):
        return {k: sanitise_sections(v, f"{path}.{k}".strip(".")) for k, v in sections.items()}
    if isinstance(sections, list):
        return [sanitise_sections(v, path) for v in sections]
    if isinstance(sections, str):
        leaf = path.split(".")[-1] if path else ""
        if _RICH_TEXT_PATHS.search(path) or leaf in {"description", "summary", "detail"}:
            return sanitise_rich_text(sections)
        return re.sub(r"<[^>]+>", "", sections)
    return sections


# ---------------------------------------------------------------------------
# NFR-402: per-field confidence carried with the profile
# ---------------------------------------------------------------------------


def low_confidence_fields(
    sections: dict[str, Any], threshold: float = LOW_CONFIDENCE
) -> list[dict[str, Any]]:
    meta = (sections or {}).get("_meta") or {}
    out = []
    for path, info in (meta.get("field_confidence") or {}).items():
        if float(info.get("confidence", 1.0)) < threshold:
            out.append({"field_path": path, **info})
    return sorted(out, key=lambda r: r["field_path"])


# ---------------------------------------------------------------------------
# Source retention (DR-102) and ingestion
# ---------------------------------------------------------------------------


def _sources_dir(job_seeker_id: str) -> Path:
    path = get_settings().uploads_dir / job_seeker_id / "sources"
    path.mkdir(parents=True, exist_ok=True)
    return path


def retain_source(job_seeker_id: str, kind: str, filename: str, data: bytes) -> dict[str, Any]:
    """Keep the uploaded original so extraction can be re-run later (DR-102).

    The file stays in the job seeker's private upload area; it is deliberately
    not registered in the shared ``raw_document`` table, which must carry no
    link to a job seeker (FR-344).  Registering it is a separate step, taken
    once the document is known to parse (see ``register_source``).
    """
    digest = hashlib.sha256(data).hexdigest()
    suffix = Path(filename).suffix.lower() or (".pdf" if kind == SOURCE_LINKEDIN else "")
    target = _sources_dir(job_seeker_id) / f"{kind}__{digest[:16]}{suffix}"
    target.write_bytes(data)
    return {
        "kind": kind,
        "filename": filename,
        "path": str(target),
        "sha256": digest,
        "byte_size": len(data),
        "retained_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def register_source(job_seeker_id: str, record: dict[str, Any]) -> None:
    """Record a retained original and drop the one it replaces (DR-102, FR-108).

    Erasure finds a job seeker's files through the private tables, so a source
    document that only ever appeared inside a JSON blob would outlive the
    account it belongs to.
    """
    sources = repo.list_sources(job_seeker_id)
    previous = next((s for s in sources if s["kind"] == record["kind"]), None)
    repo.record_source(job_seeker_id, record)
    if previous and previous["file_path"] != record["path"]:
        Path(previous["file_path"]).unlink(missing_ok=True)


def retained_sources(job_seeker_id: str) -> list[dict[str, Any]]:
    """The retained originals, newest registration per kind, missing files dropped."""
    records = [
        {
            "kind": row["kind"],
            "filename": row["filename"],
            "path": row["file_path"],
            "sha256": row["sha256"],
            "byte_size": row["byte_size"],
            "retained_at": row["retained_at"],
        }
        for row in repo.list_sources(job_seeker_id)
    ]
    if not records:
        # Profiles built before the sources were registered still name them.
        latest = repo.latest_version(job_seeker_id)
        meta = ((latest or {}).get("sections") or {}).get("_meta") or {}
        records = list(meta.get("sources") or [])
    return [s for s in records if Path(s.get("path", "")).is_file()]


def _read_source(record: dict[str, Any], llm: Any = None) -> ParsedDocument:
    """Read one retained document, letting a parse failure surface."""
    path = Path(record["path"])
    doc = parse_linkedin_pdf(path) if record["kind"] == SOURCE_LINKEDIN else parse_cv(path)
    if doc.unsegmented and llm is not None:
        doc = llm_rescue(doc, llm)
    return doc


def _parse_source(record: dict[str, Any], llm: Any = None) -> ParsedDocument:
    """Read one retained document for a rebuild, tolerating a bad file.

    A rebuild works from documents that were readable when they arrived, so a
    failure here means the file has since been damaged: the other source must
    still produce a profile (NFR-104), and the loss is recorded as a warning.
    """
    try:
        return _read_source(record, llm)
    except Exception as exc:  # noqa: BLE001 - one bad source must not lose the other
        log.exception("Could not parse retained source %s", record["path"])
        return ParsedDocument(
            source=record["kind"],
            sections=empty_profile(),
            warnings=[f"{Path(record['filename']).name} could not be parsed: {exc}"],
        )


def rebuild_profile(
    job_seeker_id: str,
    *,
    sources: list[dict[str, Any]] | None = None,
    llm: Any = None,
    persona_id: str | None = None,
    dream_job_statement: str | None = None,
    source_note: str | None = None,
    preparsed: dict[str, ParsedDocument] | None = None,
) -> dict[str, Any]:
    """Re-parse every retained source, merge, version and re-derive skills.

    Idempotent: running it twice over the same sources produces the same
    sections (and one more version, per FR-105).
    """
    records = sources if sources is not None else retained_sources(job_seeker_id)
    for record in records:
        # Idempotent: a rebuild is also where a profile saved before the
        # sources were registered gets its originals on the books (FR-108).
        register_source(job_seeker_id, record)
    parsed: dict[str, ParsedDocument] = {}
    for record in records:
        cached = (preparsed or {}).get(record["kind"])
        parsed[record["kind"]] = cached if cached is not None else _parse_source(record, llm)

    result = merge_documents(parsed.get(SOURCE_LINKEDIN), parsed.get(SOURCE_CV))

    previous = repo.latest_version(job_seeker_id)
    photo_path = (previous or {}).get("photo_path")
    if result.photo:
        photo_path = save_photo(result.photo, job_seeker_id, get_settings().uploads_dir)

    statement = dream_job_statement
    if statement is None:
        statement = (previous or {}).get("dream_job_statement")

    kinds = sorted(parsed)
    result.sections["_meta"] = {
        "sources": records,
        "field_confidence": result.field_confidence,
        "warnings": result.warnings,
        "merged_from": kinds,
        "rebuilt_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }

    default_note = "merged" if len(kinds) > 1 else (kinds[0] if kinds else "manual")
    version = repo.create_version(
        job_seeker_id,
        result.sections,
        source_note=source_note or default_note,
        dream_job_statement=statement,
        photo_path=photo_path,
        persona_id=persona_id or (previous or {}).get("persona_id"),
    )
    conflicts = repo.replace_conflicts(job_seeker_id, version["id"], result.conflicts)
    skills = refresh_skills(job_seeker_id, version, llm=llm)
    return {"version": version, "conflicts": conflicts, "skills": skills,
            "warnings": result.warnings}


def ingest_document(
    job_seeker_id: str,
    kind: str,
    filename: str,
    data: bytes,
    *,
    llm: Any = None,
    persona_id: str | None = None,
) -> dict[str, Any]:
    """Accept an uploaded LinkedIn export or CV and rebuild the profile."""
    if kind not in (SOURCE_LINKEDIN, SOURCE_CV):
        raise ValueError(f"Unknown source kind {kind!r}")
    record = retain_source(job_seeker_id, kind, filename, data)
    try:
        doc = _read_source(record, llm)
    except Exception as exc:  # noqa: BLE001 - an unreadable upload changes nothing
        Path(record["path"]).unlink(missing_ok=True)
        raise SourceUnreadable(f"{Path(filename).name} could not be read: {exc}") from exc
    register_source(job_seeker_id, record)
    records = [s for s in retained_sources(job_seeker_id) if s["kind"] != kind]
    records.append(record)
    return rebuild_profile(
        job_seeker_id,
        sources=records,
        llm=llm,
        persona_id=persona_id,
        source_note=kind,
        preparsed={kind: doc},
    )


def carry_conflicts(job_seeker_id: str, source_version_id: str, version_id: str) -> None:
    """Move the conflict queue onto a version derived from an earlier one (FR-103).

    Conflicts are addressed by version id, so a version created from another -
    a manual edit, a new dream-job statement, a restore - would otherwise hide
    every disagreement the merge found before the job seeker had settled it.
    Resolutions already made are preserved by ``replace_conflicts``.
    """
    if not source_version_id or source_version_id == version_id:
        return
    rows = repo.list_conflicts(job_seeker_id, profile_version_id=source_version_id)
    if not rows:
        return
    repo.replace_conflicts(
        job_seeker_id,
        version_id,
        [
            {
                "field_path": row["field_path"],
                "value_linkedin": row["value_linkedin"],
                "value_cv": row["value_cv"],
            }
            for row in rows
        ],
    )


def carry_skills(job_seeker_id: str, source_version_id: str, version_id: str) -> list[dict]:
    """Copy the skill rows onto a version whose sections did not change (FR-107).

    Skills hang off a version id; a save that only touches the dream-job
    statement must not leave the newest version without any.  Copying rather
    than re-deriving keeps whatever the job seeker corrected by hand.
    """
    if not source_version_id or source_version_id == version_id:
        return []
    rows = repo.list_skills(job_seeker_id, profile_version_id=source_version_id)
    if not rows:
        return []
    return repo.replace_skills(job_seeker_id, version_id, rows)


def refresh_skills(job_seeker_id: str, version: dict[str, Any], *, llm: Any = None) -> list[dict]:
    """Re-derive the FR-107 skill rows for one profile version."""
    matches = build_skills(version.get("sections") or {}, llm=llm)
    evidence = repo.list_evidence(job_seeker_id)
    payload = []
    for match in matches:
        refs = [
            item["id"]
            for item in evidence
            if any(
                str(label).strip().lower() == match.normalised_label.lower()
                for label in item["linked_skills"]
            )
        ]
        payload.append(
            {
                "raw_label": match.raw_label,
                "normalised_label": match.normalised_label,
                "taxonomy": match.taxonomy,
                "taxonomy_code": match.taxonomy_code,
                "proficiency": match.proficiency,
                "years_experience": match.years_experience,
                "last_used_year": match.last_used_year,
                "evidence_refs": refs,
            }
        )
    return repo.replace_skills(job_seeker_id, version["id"], payload)


# ---------------------------------------------------------------------------
# Manual save paths (FR-104, FR-105, FR-109)
# ---------------------------------------------------------------------------


def save_manual_profile(
    job_seeker_id: str,
    sections: dict[str, Any],
    *,
    dream_job_statement: str | None = None,
    persona_id: str | None = None,
    llm: Any = None,
) -> dict[str, Any]:
    """Save an edited profile as a new version (FR-104, FR-105)."""
    previous = repo.latest_version(job_seeker_id)
    cleaned = sanitise_sections(sections)
    meta = (previous or {}).get("sections", {}).get("_meta") if previous else None
    incoming_meta = sections.get("_meta") if isinstance(sections.get("_meta"), dict) else None
    cleaned["_meta"] = {
        **(meta or {}),
        **(incoming_meta or {}),
        "edited_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "edited_by_user": True,
    }
    statement = (
        dream_job_statement
        if dream_job_statement is not None
        else (previous or {}).get("dream_job_statement")
    )
    version = repo.create_version(
        job_seeker_id,
        cleaned,
        source_note="manual",
        dream_job_statement=statement,
        photo_path=(previous or {}).get("photo_path"),
        persona_id=persona_id or (previous or {}).get("persona_id"),
    )
    if previous:
        carry_conflicts(job_seeker_id, previous["id"], version["id"])
    refresh_skills(job_seeker_id, version, llm=llm)
    return version


def save_dream_job_statement(
    job_seeker_id: str, statement: str, *, persona_id: str | None = None
) -> dict[str, Any]:
    """FR-109: the statement is versioned with the profile and has no limit."""
    previous = repo.latest_version(job_seeker_id)
    sections = (previous or {}).get("sections") or empty_profile()
    version = repo.create_version(
        job_seeker_id,
        sections,
        source_note="dream_job_statement",
        dream_job_statement=statement,
        photo_path=(previous or {}).get("photo_path"),
        persona_id=persona_id or (previous or {}).get("persona_id"),
    )
    if previous:
        # The sections are unchanged, so the skills and the conflict queue
        # belong to this version too (FR-103, FR-107).
        carry_skills(job_seeker_id, previous["id"], version["id"])
        carry_conflicts(job_seeker_id, previous["id"], version["id"])
    return version


def apply_conflict_resolutions(job_seeker_id: str, *, llm: Any = None) -> dict[str, Any] | None:
    """Fold every settled conflict into a fresh version (FR-103, FR-105)."""
    previous = repo.latest_version(job_seeker_id)
    if previous is None:
        return None
    conflicts = repo.list_conflicts(job_seeker_id, profile_version_id=previous["id"])
    settled = [c for c in conflicts if c.get("resolution") not in (None, "", "unresolved")]
    if not settled:
        return previous
    sections = apply_resolutions(dict(previous["sections"]), settled)
    version = repo.create_version(
        job_seeker_id,
        sections,
        source_note="conflicts_resolved",
        dream_job_statement=previous.get("dream_job_statement"),
        photo_path=previous.get("photo_path"),
        persona_id=previous.get("persona_id"),
    )
    remaining = [
        {
            "field_path": c["field_path"],
            "value_linkedin": c["value_linkedin"],
            "value_cv": c["value_cv"],
        }
        for c in conflicts
    ]
    repo.replace_conflicts(job_seeker_id, version["id"], remaining)
    refresh_skills(job_seeker_id, version, llm=llm)
    return version
