"""LinkedIn "Save to PDF" export parser (FR-102, NFR-402, NFR-205).

FR-102 makes the LinkedIn export authoritative for the *shape* of a profile,
so the canonical section list and the ``ParsedDocument`` container both live
here and are imported by the CV parser and the merge step.

The export is a two-column PDF: a narrow left rail carrying Contact, Top
Skills, Languages and the certificate-like sections, and a wide body carrying
the name, headline, Summary, Experience and Education.  Read as a text stream
the two columns interleave line by line, which is why every extraction here
works from ``pdfplumber`` word boxes: ``x0`` separates the rail from the body,
and the font size separates a section header from a company from a role title
from a date line.  Those two signals are stable across exports in a way that
regular expressions over the flattened text are not.

Passages the deterministic splitter cannot segment - an unfamiliar section
header, or a body with no recognisable structure at all - are handed to
``llm_rescue`` and re-read by the model.  The call site decides whether that
fallback is available; parsing never requires it (NFR-104).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber

log = logging.getLogger(__name__)

# FR-102: the section structure of the export *is* the base profile schema.
PROFILE_SECTIONS: tuple[str, ...] = (
    "contact",
    "summary",
    "top_skills",
    "languages",
    "experience",
    "education",
    "certifications",
    "publications",
    "projects",
    "honors",
    "volunteering",
    "recommendations",
    "interests",
    "courses",
    "other",
)

LIST_SECTIONS = frozenset(
    {
        "top_skills",
        "languages",
        "experience",
        "education",
        "certifications",
        "publications",
        "projects",
        "honors",
        "volunteering",
        "recommendations",
        "interests",
        "courses",
    }
)

# Header text (lower-cased, punctuation stripped) -> canonical section key.
_HEADER_VOCABULARY: dict[str, str] = {
    "contact": "contact",
    "contact information": "contact",
    "summary": "summary",
    "about": "summary",
    "top skills": "top_skills",
    "skills": "top_skills",
    "skills expertise": "top_skills",
    "languages": "languages",
    "experience": "experience",
    "work experience": "experience",
    "education": "education",
    "licenses certifications": "certifications",
    "certifications": "certifications",
    "publications": "publications",
    "patents": "publications",
    "projects": "projects",
    "honors awards": "honors",
    "honors": "honors",
    "awards": "honors",
    "volunteering": "volunteering",
    "volunteer experience": "volunteering",
    "recommendations": "recommendations",
    "interests": "interests",
    "causes": "interests",
    "courses": "courses",
    "test scores": "courses",
    "organizations": "other",
}

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    # LinkedIn exports keep the profile language for month names.
    "januari": 1, "februari": 2, "maart": 3, "mei": 5, "juni": 6, "juli": 7,
    "augustus": 8, "oktober": 10, "janvier": 1, "fevrier": 2, "mars": 3,
    "avril": 4, "mai": 5, "juin": 6, "juillet": 7, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "decembre": 12,
}
_PRESENT = {"present", "current", "heden", "nu", "aujourd'hui", "actuel", "heute"}

_DASH = r"[-‐-―−]"
# Only real month names introduce a date, so that a location word in front of
# a year ("Brussels 2019 - 2024") is not read as "month year".
_MONTH_NAMES = "|".join(sorted((re.escape(m) for m in _MONTHS), key=len, reverse=True))
MONTH_YEAR_PATTERN = rf"(?:{_MONTH_NAMES})\.?\s+\d{{4}}|\d{{1,2}}/\d{{4}}|\d{{4}}"
_MONTH_YEAR = MONTH_YEAR_PATTERN
_RANGE_RE = re.compile(
    rf"(?P<start>{_MONTH_YEAR})\s*{_DASH}\s*(?P<end>{_MONTH_YEAR}|[A-Za-zÀ-ſ']+)",
    re.IGNORECASE,
)
_DURATION_ONLY_RE = re.compile(
    r"^\(?\s*(?:\d+\s*(?:years?|yrs?|jaar|ans?|jahre?)"
    r"(?:\s*\d+\s*(?:months?|mos?|maanden?|mois|monate?))?"
    r"|\d+\s*(?:months?|mos?|maanden?|mois|monate?))\s*\)?$",
    re.IGNORECASE,
)
_PARENTHETICAL_DURATION_RE = re.compile(
    r"\s*\([^()]*(?:year|month|yr|mo|jaar|maand|ans|mois)[^()]*\)\s*$", re.IGNORECASE
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"^\+?[\d][\d\s().–-]{6,}$")
_URL_RE = re.compile(r"^(?:https?://|www\.)\S+$|^[\w-]+(?:\.[\w-]+){1,}(?:/\S*)?$")
_LABEL_RE = re.compile(r"^\(([^)]{2,40})\)$")
_PAGE_FOOTER_RE = re.compile(r"^page\s+\d+\s+of\s+\d+$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


@dataclass
class ParsedDocument:
    """One source document, read into the FR-102 schema.

    ``field_confidence`` carries the NFR-402 per-field confidence and the
    provenance label; it is persisted alongside the sections so the profile
    screens can flag weakly-extracted fields.
    """

    source: str
    sections: dict[str, Any]
    field_confidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    photo: bytes | None = None
    raw_text: str = ""
    unsegmented: dict[str, str] = field(default_factory=dict)

    def confidence(self, path: str, value: float) -> None:
        self.field_confidence[path] = {"confidence": round(value, 2), "source": self.source}


def empty_profile() -> dict[str, Any]:
    """A profile skeleton with every FR-102 section present."""
    out: dict[str, Any] = {}
    for key in PROFILE_SECTIONS:
        if key == "contact":
            out[key] = {
                "name": None, "headline": None, "location": None, "address": None,
                "phone": None, "email": None, "linkedin_url": None, "websites": [],
            }
        elif key == "summary":
            out[key] = None
        elif key == "other":
            out[key] = {}
        else:
            out[key] = []
    return out


@dataclass
class DateRange:
    start: str | None = None
    end: str | None = None
    current: bool = False
    raw: str = ""          # the whole line, for display
    span: str = ""         # only the matched date text, for removal


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def parse_month_year(token: str) -> str | None:
    """``"November 2014"`` -> ``"2014-11"``; a bare year stays a bare year."""
    t = (token or "").strip().strip(",.").lower()
    if not t:
        return None
    if re.fullmatch(r"\d{4}", t):
        return t
    m = re.fullmatch(r"(\d{1,2})/(\d{4})", t)
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    m = re.fullmatch(r"([a-zÀ-ſ]{3,12})\.?\s+(\d{4})", t)
    if m:
        month = _MONTHS.get(m.group(1)) or _MONTHS.get(m.group(1)[:3])
        if month:
            return f"{m.group(2)}-{month:02d}"
        return m.group(2)
    return None


def parse_date_range(text: str) -> DateRange | None:
    """Read ``"May 2012 - October 2014 (2 years 6 months)"`` and friends."""
    if not text:
        return None
    body = _PARENTHETICAL_DURATION_RE.sub("", text.strip()).strip("()").strip()
    m = _RANGE_RE.search(body)
    if not m:
        year = re.fullmatch(r"\(?\s*(\d{4})\s*\)?", body)
        if year:
            return DateRange(
                start=year.group(1), end=year.group(1),
                raw=text.strip(), span=year.group(1),
            )
        return None
    start = parse_month_year(m.group("start"))
    end_token = m.group("end").strip().strip(".")
    if end_token.lower() in _PRESENT:
        return DateRange(
            start=start, end=None, current=True, raw=text.strip(), span=m.group(0)
        )
    end = parse_month_year(end_token)
    if start is None and end is None:
        return None
    return DateRange(start=start, end=end, raw=text.strip(), span=m.group(0))


def year_of(value: str | None) -> int | None:
    if not value:
        return None
    m = re.match(r"(\d{4})", str(value))
    return int(m.group(1)) if m else None


def months_between(start: str | None, end: str | None, now_year: int, now_month: int) -> float:
    """Length of a role in months, tolerating year-only and open-ended entries."""
    if not start:
        return 0.0
    sy, sm = year_of(start), 1
    if sy is None:
        return 0.0
    parts = str(start).split("-")
    if len(parts) > 1 and parts[1].isdigit():
        sm = int(parts[1])
    if end:
        ey = year_of(end) or now_year
        em = 12
        eparts = str(end).split("-")
        if len(eparts) > 1 and eparts[1].isdigit():
            em = int(eparts[1])
    else:
        ey, em = now_year, now_month
    return max(0.0, (ey - sy) * 12 + (em - sm) + 1)


# ---------------------------------------------------------------------------
# Layout reading
# ---------------------------------------------------------------------------


@dataclass
class Line:
    text: str
    size: float
    top: float
    x0: float
    page: int
    column: str  # "rail" | "body"


def _lines_from_page(page: Any, page_no: int, tolerance: float = 2.5) -> list[Line]:
    words = page.extract_words(extra_attrs=["size"], keep_blank_chars=False)
    if not words:
        return []
    split_x = _column_split_x(words, page.width)

    buckets: dict[tuple[str, int], list[dict]] = {}
    for w in words:
        column = "rail" if split_x is not None and w["x1"] <= split_x else "body"
        key = (column, int(round(w["top"] / tolerance)))
        buckets.setdefault(key, []).append(w)

    lines: list[Line] = []
    for (column, _), group in buckets.items():
        group.sort(key=lambda w: w["x0"])
        text = " ".join(w["text"] for w in group).strip()
        if not text or _PAGE_FOOTER_RE.match(text):
            continue
        lines.append(
            Line(
                text=text,
                size=round(max(float(w["size"]) for w in group), 2),
                top=min(float(w["top"]) for w in group),
                x0=min(float(w["x0"]) for w in group),
                page=page_no,
                column=column,
            )
        )
    lines.sort(key=lambda ln: (ln.column != "rail", ln.top))
    return lines


def _column_split_x(words: list[dict], page_width: float) -> float | None:
    """Left edge of the body column, or ``None`` for a single-column page.

    Every body line in a LinkedIn export starts at the same x, so the modal
    ``x0`` is the body's left edge as long as it sits clear of the page margin.
    """
    counts = Counter(round(float(w["x0"]), 1) for w in words)
    body_left, hits = counts.most_common(1)[0]
    if body_left < page_width * 0.2 or hits < 3:
        return None
    boundary = body_left - 8.0
    rail_words = sum(1 for w in words if w["x1"] <= boundary)
    if rail_words < max(4, 0.02 * len(words)):
        return None
    return boundary


def _read_lines(pdf_path: Path) -> tuple[list[Line], str]:
    lines: list[Line] = []
    raw_pages: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            lines.extend(_lines_from_page(page, i))
            raw_pages.append(page.extract_text() or "")
    lines.sort(key=lambda ln: (ln.page, ln.column != "rail", ln.top))
    return lines, "\n".join(raw_pages)


def _normalise_header(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip().replace("  ", " ")


def _section_of(text: str) -> str | None:
    return _HEADER_VOCABULARY.get(_normalise_header(text))


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------


@dataclass
class Block:
    key: str
    title: str
    column: str
    lines: list[Line] = field(default_factory=list)
    recognised: bool = True


def _split_sections(lines: list[Line]) -> tuple[list[Line], list[Block]]:
    """Split into a page-1 preamble (name/headline) and a list of section blocks."""
    body_sizes = Counter(ln.size for ln in lines if ln.column == "body")
    rail_sizes = Counter(ln.size for ln in lines if ln.column == "rail")
    body_detail = body_sizes.most_common(1)[0][0] if body_sizes else 10.0
    rail_detail = rail_sizes.most_common(1)[0][0] if rail_sizes else 10.0

    # Header size is calibrated per column from the headers we do recognise:
    # the name is set far larger than any header and must not open a section.
    header_floor: dict[str, float | None] = {"rail": None, "body": None}
    for column, detail in (("rail", rail_detail), ("body", body_detail)):
        known = [
            ln.size
            for ln in lines
            if ln.column == column and _section_of(ln.text) and ln.size > detail + 0.75
        ]
        header_floor[column] = min(known) if known else None

    preamble: list[Line] = []
    blocks: list[Block] = []
    # Each column is split on its own: the rail and the body interleave in
    # reading order, and a rail header must never swallow body lines.
    for column, detail in (("rail", rail_detail), ("body", body_detail)):
        _split_column(
            [ln for ln in lines if ln.column == column],
            detail, header_floor[column], preamble, blocks,
        )
    return preamble, blocks


def _split_column(
    lines: list[Line],
    detail: float,
    floor: float | None,
    preamble: list[Line],
    blocks: list[Block],
) -> None:
    current: Block | None = None
    for ln in lines:
        key = _section_of(ln.text)
        is_known = key is not None and ln.size > detail + 0.75
        # An unfamiliar short line set in a header-sized font opens a section
        # too; it is marked unrecognised so the LLM fallback can read it.
        is_unknown = (
            key is None
            and floor is not None
            and floor - 0.3 <= ln.size <= floor + 2.0
            and len(ln.text) <= 60
            and ln.size > detail + 0.75
        )
        if is_known or is_unknown:
            current = Block(
                key=key or "other",
                title=ln.text.strip(),
                column=ln.column,
                recognised=is_known,
            )
            blocks.append(current)
            continue
        if current is None:
            preamble.append(ln)
        else:
            current.lines.append(ln)


# ---------------------------------------------------------------------------
# Per-section readers
# ---------------------------------------------------------------------------


def _read_preamble(preamble: list[Line], doc: ParsedDocument) -> None:
    body = [ln for ln in preamble if ln.column == "body"]
    if not body:
        return
    contact = doc.sections["contact"]
    contact["name"] = body[0].text.strip()
    doc.confidence("contact.name", 0.97)
    rest = [ln.text.strip() for ln in body[1:] if ln.text.strip()]
    if len(rest) == 1:
        contact["headline"] = rest[0]
        doc.confidence("contact.headline", 0.9)
    elif rest:
        contact["headline"] = " ".join(rest[:-1])
        contact["location"] = rest[-1]
        doc.confidence("contact.headline", 0.9)
        doc.confidence("contact.location", 0.85)


def _read_contact(block: Block, doc: ParsedDocument) -> None:
    contact = doc.sections["contact"]
    address: list[str] = []
    for ln in block.lines:
        text = ln.text.strip()
        label = _LABEL_RE.match(text)
        if label and contact["websites"]:
            contact["websites"][-1]["label"] = label.group(1)
            if label.group(1).lower() == "linkedin":
                contact["linkedin_url"] = contact["websites"][-1]["url"]
                doc.confidence("contact.linkedin_url", 0.95)
            continue
        email = _EMAIL_RE.search(text)
        if email:
            contact["email"] = email.group(0)
            doc.confidence("contact.email", 0.96)
            continue
        phone_text = re.sub(r"\((?:mobile|home|work|gsm|tel)\)", "", text, flags=re.I).strip()
        if _PHONE_RE.match(phone_text):
            contact["phone"] = phone_text
            doc.confidence("contact.phone", 0.93)
            continue
        if _URL_RE.match(text):
            url = text.rstrip("/,")
            contact["websites"].append({"url": url, "label": None})
            if "linkedin.com/in/" in url.lower():
                contact["linkedin_url"] = url
                doc.confidence("contact.linkedin_url", 0.95)
            continue
        address.append(text)
    if address:
        contact["address"] = ", ".join(address)
        doc.confidence("contact.address", 0.8)


def _read_simple_list(block: Block, doc: ParsedDocument, key: str) -> None:
    for i, ln in enumerate(block.lines):
        text = ln.text.strip()
        if not text:
            continue
        doc.sections[key].append(text)
        doc.confidence(f"{key}.{i}", 0.9)


def _read_languages(block: Block, doc: ParsedDocument) -> None:
    for i, ln in enumerate(block.lines):
        text = ln.text.strip()
        if not text:
            continue
        m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", text)
        if m:
            entry = {"language": m.group(1).strip(), "proficiency": m.group(2).strip()}
        else:
            entry = {"language": text, "proficiency": None}
        doc.sections["languages"].append(entry)
        doc.confidence(f"languages.{i}.language", 0.93)


def _size_tiers(block: Block) -> tuple[float, float, float]:
    """``(entity, role, detail)`` font sizes for an Experience-shaped block."""
    sizes = sorted({ln.size for ln in block.lines}, reverse=True)
    if not sizes:
        return (0.0, 0.0, 0.0)
    if len(sizes) == 1:
        return (sizes[0] + 99, sizes[0] + 99, sizes[0])
    if len(sizes) == 2:
        return (sizes[0], sizes[0], sizes[1])
    return (sizes[0], sizes[1], sizes[2])


def _read_experience(block: Block, doc: ParsedDocument) -> None:
    entity_sz, role_sz, _ = _size_tiers(block)
    company: str | None = None
    company_duration: str | None = None
    entries: list[dict[str, Any]] = doc.sections["experience"]
    current: dict[str, Any] | None = None
    desc: list[str] = []

    def close() -> None:
        nonlocal current, desc
        if current is not None:
            current["description"] = "\n".join(desc).strip() or None
            entries.append(current)
        current, desc = None, []

    for ln in block.lines:
        text = ln.text.strip()
        if not text:
            continue
        if ln.size >= entity_sz - 0.05:
            close()
            company = text
            company_duration = None
            continue
        if ln.size >= role_sz - 0.05:
            close()
            current = {
                "company": company, "title": text, "start": None, "end": None,
                "current": False, "location": None, "description": None,
                "duration_raw": None, "company_duration": company_duration,
            }
            continue
        if current is None:
            # A duration on its own directly under a company name is the
            # total tenure across the roles that follow.
            if _DURATION_ONLY_RE.match(text):
                company_duration = text
            elif company:
                # A company with no separate title line: promote the company.
                current = {
                    "company": company, "title": None, "start": None, "end": None,
                    "current": False, "location": None, "description": None,
                    "duration_raw": None, "company_duration": company_duration,
                }
                desc.append(text)
            continue
        rng = parse_date_range(text) if not desc else None
        if rng and current["start"] is None:
            current["start"] = rng.start
            current["end"] = rng.end
            current["current"] = rng.current
            current["duration_raw"] = rng.raw
            continue
        if (
            current["start"] is not None
            and current["location"] is None
            and not desc
            and len(text) <= 60
            and not text.endswith((".", ":", ";", ","))
        ):
            current["location"] = text
            continue
        desc.append(text)
    close()

    for i, entry in enumerate(entries):
        doc.confidence(f"experience.{i}.company", 0.95 if entry["company"] else 0.3)
        doc.confidence(f"experience.{i}.title", 0.95 if entry["title"] else 0.3)
        doc.confidence(
            f"experience.{i}.dates", 0.92 if entry["start"] else 0.35
        )


def _read_education(block: Block, doc: ParsedDocument) -> None:
    entity_sz, _, _ = _size_tiers(block)
    entries: list[dict[str, Any]] = doc.sections["education"]
    current: dict[str, Any] | None = None
    detail_lines: list[str] = []

    def close() -> None:
        if current is None:
            return
        # LinkedIn wraps the degree line, so the detail is read as one string.
        detail = " ".join(detail_lines).strip()
        rng = parse_date_range(detail)
        if rng:
            current["start_year"] = year_of(rng.start)
            current["end_year"] = year_of(rng.end)
            detail = detail.replace(rng.span, "")
        detail = re.sub(r"\(\s*[-–—]?\s*\)", "", detail).strip(" ·•,-")
        if detail:
            parts = [p.strip() for p in detail.split(",", 1)]
            current["degree"] = parts[0] or None
            current["field"] = parts[1].strip(" ·•,-") if len(parts) > 1 else None

    for ln in block.lines:
        text = ln.text.strip()
        if not text:
            continue
        if ln.size >= entity_sz - 0.05:
            close()
            current = {
                "school": text, "degree": None, "field": None, "location": None,
                "start_year": None, "end_year": None, "description": None,
            }
            entries.append(current)
            detail_lines = []
            continue
        if current is not None:
            detail_lines.append(text)
    close()
    for i, _ in enumerate(entries):
        doc.confidence(f"education.{i}.school", 0.94)


def _read_entry_list(block: Block, doc: ParsedDocument, key: str) -> None:
    """Certifications / publications / projects / honors / volunteering."""
    entity_sz, _, _ = _size_tiers(block)
    entries: list[dict[str, Any]] = doc.sections[key]
    current: dict[str, Any] | None = None
    for ln in block.lines:
        text = ln.text.strip()
        if not text:
            continue
        if ln.size >= entity_sz - 0.05 or current is None:
            current = {"title": text, "detail": None, "start": None, "end": None}
            entries.append(current)
            continue
        rng = parse_date_range(text)
        if rng and current["start"] is None:
            current["start"], current["end"] = rng.start, rng.end
            continue
        current["detail"] = ((current["detail"] or "") + "\n" + text).strip()
    for i, _ in enumerate(entries):
        doc.confidence(f"{key}.{i}.title", 0.85)


_ENTRY_LIST_KEYS = ("certifications", "publications", "projects", "honors", "volunteering",
                    "recommendations", "courses")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def extract_photo(pdf_path: Path) -> bytes | None:
    """Return the embedded profile photo, when the export carries one.

    LinkedIn only embeds the picture for some accounts, so a ``None`` here is
    normal rather than an error; the CV parser usually supplies the photo.
    """
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - pypdf is a hard requirement
        return None
    try:
        reader = PdfReader(str(pdf_path))
        best: bytes | None = None
        for page in reader.pages[:2]:
            for image in page.images:
                data = image.data
                if data and len(data) > len(best or b""):
                    best = data
        return best
    except Exception:  # noqa: BLE001 - a malformed image must not fail the import
        log.debug("No usable image stream in %s", pdf_path, exc_info=True)
        return None


def parse_linkedin_pdf(pdf_path: str | Path) -> ParsedDocument:
    """Parse a LinkedIn export into the FR-102 profile schema."""
    path = Path(pdf_path)
    doc = ParsedDocument(source="linkedin_pdf", sections=empty_profile())
    lines, raw_text = _read_lines(path)
    doc.raw_text = raw_text
    if not lines:
        doc.warnings.append("The PDF contains no extractable text layer.")
        return doc

    preamble, blocks = _split_sections(lines)
    _read_preamble(preamble, doc)

    seen: set[str] = set()
    for block in blocks:
        if not block.recognised:
            doc.unsegmented[block.title] = "\n".join(ln.text for ln in block.lines)
            doc.sections["other"][block.title] = "\n".join(ln.text for ln in block.lines)
            continue
        key = block.key
        seen.add(key)
        if key == "contact":
            _read_contact(block, doc)
        elif key == "summary":
            text = "\n".join(ln.text for ln in block.lines).strip()
            doc.sections["summary"] = ((doc.sections["summary"] or "") + "\n" + text).strip()
            doc.confidence("summary", 0.95)
        elif key == "top_skills":
            _read_simple_list(block, doc, "top_skills")
        elif key == "languages":
            _read_languages(block, doc)
        elif key == "experience":
            _read_experience(block, doc)
        elif key == "education":
            _read_education(block, doc)
        elif key in _ENTRY_LIST_KEYS:
            _read_entry_list(block, doc, key)
        elif key == "interests":
            _read_simple_list(block, doc, "interests")
        else:
            doc.sections["other"][block.title] = "\n".join(ln.text for ln in block.lines)

    if "experience" not in seen:
        doc.warnings.append("No Experience section found; the export may be truncated.")
        doc.unsegmented.setdefault("whole document", raw_text)
    doc.photo = extract_photo(path)
    return doc


# ---------------------------------------------------------------------------
# LLM fallback (NFR-205: the export is untrusted input)
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = "profile_extract.md"
PROMPT_VERSION = "1"


def _prompt_text() -> str:
    prompt = Path(__file__).resolve().parents[1] / "llm" / "prompts" / PROMPT_TEMPLATE
    return prompt.read_text(encoding="utf-8")


def llm_rescue(doc: ParsedDocument, llm: Any) -> ParsedDocument:
    """Re-read passages the splitter could not segment.

    ``llm`` is an ``LLMClient``.  Failures are recorded as warnings and the
    deterministic result is kept: an unreadable extra section must never cost
    the user the sections that did parse (NFR-104).
    """
    if not doc.unsegmented or llm is None:
        return doc
    payload = "\n\n".join(f"## {title}\n{body}" for title, body in doc.unsegmented.items())
    try:
        data = llm.complete_json(
            "extract.profile",
            system=_prompt_text(),
            user="Read the profile passages below and return the sections they contain.",
            untrusted={"profile_export": payload},
            schema_hint=(
                '{"summary": str|null, "experience": [{"company": str, "title": str, '
                '"start": "YYYY-MM"|null, "end": "YYYY-MM"|null, "location": str|null, '
                '"description": str|null}], "education": [{"school": str, "degree": str|null, '
                '"start_year": int|null, "end_year": int|null}], '
                '"certifications": [{"title": str}], '
                '"publications": [{"title": str}], "projects": [{"title": str}], '
                '"honors": [{"title": str}], "volunteering": [{"title": str}], '
                '"top_skills": [str], "languages": [{"language": str, "proficiency": str|null}]}'
            ),
            prompt_template=PROMPT_TEMPLATE,
            prompt_version=PROMPT_VERSION,
            prefer_strong=False,
            max_tokens=2048,
        )
    except Exception as exc:  # noqa: BLE001 - degrade to the deterministic parse
        doc.warnings.append(f"LLM fallback unavailable: {exc}")
        return doc
    merge_llm_sections(doc, data if isinstance(data, dict) else {})
    doc.unsegmented.clear()
    return doc


def merge_llm_sections(doc: ParsedDocument, data: dict[str, Any]) -> None:
    """Fold an LLM reading into a parsed document without overwriting facts."""
    if data.get("summary") and not doc.sections.get("summary"):
        doc.sections["summary"] = str(data["summary"])
        doc.confidence("summary", 0.6)
    for key in PROFILE_SECTIONS:
        if key not in LIST_SECTIONS:
            continue
        incoming = data.get(key)
        if not isinstance(incoming, list):
            continue
        existing = doc.sections[key]
        known = {_entry_key(e) for e in existing}
        for item in incoming:
            entry = item if isinstance(item, (dict, str)) else None
            if entry is None or _entry_key(entry) in known:
                continue
            existing.append(entry)
            doc.confidence(f"{key}.{len(existing) - 1}", 0.6)


def _entry_key(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip().lower()
    if isinstance(entry, dict):
        for field_name in ("title", "language", "school", "company", "name"):
            if entry.get(field_name):
                return str(entry[field_name]).strip().lower()
    return repr(entry)
