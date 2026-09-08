"""Classic CV template: single column, reverse-chronological (FR-322).

The conservative option, and the default: a centred name block with the
photograph beside it, a rule under every section heading, and one column of
experience.  It is the shape an ATS parses most reliably and the shape a
Belgian or Dutch hiring manager expects, which is why it is the fallback the
registry resolves to.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, Spacer, Table, TableStyle

from dreamjob.documents.pdf_builder import (
    HorizontalRule,
    PdfBuilder,
    escape,
)
from dreamjob.documents.templates import CvDocument
from dreamjob.documents.templates._docx import (
    borderless,
    bullet,
    cell_margins,
    new_document,
    para,
    rule,
)

TEMPLATE_KEY = "classic"
DISPLAY_NAME = "Classic"
DESCRIPTION = (
    "Single column, reverse-chronological, photograph beside the name block. "
    "The most ATS-tolerant of the templates."
)

PHOTO_WIDTH_MM = 28.0


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def render_docx(cv: CvDocument, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    document = new_document(margin_cm=1.8)
    accent = cv.accent

    _docx_header(document, cv, accent)

    if cv.summary:
        _docx_heading(document, cv.section_label("summary"), accent)
        para(document, cv.summary, size=9.5, align="justify")

    if cv.experience:
        _docx_heading(document, cv.section_label("experience"), accent)
        for job in cv.experience:
            heading = para(document, space_before=4, space_after=0)
            run = heading.add_run(job.title or job.company)
            run.bold = True
            run.font.size = _pt(10.5)
            if job.title and job.company:
                tail = heading.add_run(f"  |  {job.company}")
                tail.font.size = _pt(10.5)
            meta = " · ".join(p for p in (job.period(cv.language), job.location) if p)
            if meta:
                para(document, meta, size=8.5, italic=True, colour="#5b6b7c", space_after=2)
            for line in job.bullets:
                bullet(document, line, size=9.5)

    if cv.education:
        _docx_heading(document, cv.section_label("education"), accent)
        for study in cv.education:
            heading = para(document, space_before=3, space_after=0)
            run = heading.add_run(study.school)
            run.bold = True
            run.font.size = _pt(10)
            line = " · ".join(p for p in (study.line(), study.period(cv.language)) if p)
            if line:
                para(document, line, size=8.5, italic=True, colour="#5b6b7c", space_after=2)
            if study.detail:
                para(document, study.detail, size=9)

    if cv.skills:
        _docx_heading(document, cv.section_label("skills"), accent)
        para(document, " · ".join(cv.skills), size=9.5)

    if cv.languages:
        _docx_heading(document, cv.section_label("languages"), accent)
        para(document, " · ".join(entry.line() for entry in cv.languages), size=9.5)

    for key, entries in cv.entry_sections():
        _docx_heading(document, cv.section_label(key), accent)
        for entry in entries:
            text = entry.title
            if entry.date:
                text = f"{text} ({entry.date})"
            bullet(document, text, size=9.5)
            if entry.detail:
                para(document, entry.detail, size=8.5, colour="#5b6b7c")

    _docx_footer(document, cv)
    document.save(str(target))
    return target


def _pt(value: float):  # noqa: ANN202 - thin alias, keeps the layout code readable
    from docx.shared import Pt  # noqa: PLC0415

    return Pt(value)


def _docx_heading(document, text: str, accent: str) -> None:
    paragraph = para(
        document, text.upper(), size=10, bold=True, colour=accent,
        space_before=10, space_after=2,
    )
    rule(paragraph, accent)


def _docx_header(document, cv: CvDocument, accent: str) -> None:
    contact_lines = cv.contact.lines()
    if cv.has_photo():
        table = document.add_table(rows=1, cols=2)
        borderless(table)
        left, right = table.rows[0].cells
        cell_margins(left, left=0.0)
        cell_margins(right, right=0.0)
        left.width = _cm(12.5)
        right.width = _cm(3.5)
        _docx_identity(left, cv, accent, contact_lines)
        photo_paragraph = right.paragraphs[0]
        photo_paragraph.alignment = 2  # right
        photo_paragraph.add_run().add_picture(str(cv.photo_path), width=_cm(3.0))
    else:
        _docx_identity(document, cv, accent, contact_lines)
    rule(para(document, space_after=6), accent, size=12)


def _docx_identity(container, cv: CvDocument, accent: str, contact_lines: list[str]) -> None:
    para(container, cv.contact.name, size=20, bold=True, colour=accent, space_after=1)
    if cv.contact.headline:
        para(container, cv.contact.headline, size=11, colour="#5b6b7c", space_after=3)
    if contact_lines:
        para(container, "  ·  ".join(contact_lines), size=8.5, colour="#5b6b7c")


def _cm(value: float):  # noqa: ANN202
    from docx.shared import Cm  # noqa: PLC0415

    return Cm(value)


def _docx_footer(document, cv: CvDocument) -> None:
    """FR-331: the generation date and data versions travel with the file."""
    paragraph = document.sections[0].footer.paragraphs[0]
    paragraph.text = cv.meta.footer_line()
    for run in paragraph.runs:
        run.font.size = _pt(7)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def render_pdf(cv: CvDocument, path: str | Path) -> Path:
    accent = colors.HexColor(cv.accent)
    builder = PdfBuilder(cv.meta, accent=accent, margins_mm=16)
    styles = builder.styles

    identity: list = [
        Paragraph(escape(cv.contact.name), styles["title"]),
    ]
    if cv.contact.headline:
        identity.append(Paragraph(escape(cv.contact.headline), styles["subtitle"]))
    contact_lines = cv.contact.lines()
    if contact_lines:
        identity.append(
            Paragraph("  ·  ".join(escape(line) for line in contact_lines), styles["note"])
        )

    if cv.has_photo():
        photo = Image(
            str(cv.photo_path), width=PHOTO_WIDTH_MM * mm, height=PHOTO_WIDTH_MM * mm,
            kind="proportional",
        )
        header = Table(
            [[identity, photo]],
            colWidths=[builder.content_width - PHOTO_WIDTH_MM * mm - 6 * mm,
                       PHOTO_WIDTH_MM * mm + 6 * mm],
            hAlign="LEFT",
        )
        header.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (0, 0), "TOP"),
                    ("VALIGN", (1, 0), (1, 0), "TOP"),
                    ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        builder.story.append(header)
    else:
        builder.story.extend(identity)

    builder.story.append(HorizontalRule(colour=accent, thickness=1.2))
    builder.spacer(3)

    if cv.summary:
        builder.h1(cv.section_label("summary"))
        builder.para(cv.summary)

    if cv.experience:
        builder.h1(cv.section_label("experience"))
        for job in cv.experience:
            builder.keep_together(lambda sub, job=job: _pdf_job(sub, cv, job))

    if cv.education:
        builder.h1(cv.section_label("education"))
        for study in cv.education:
            line = " · ".join(p for p in (study.line(), study.period(cv.language)) if p)
            builder.h2(study.school)
            if line:
                builder.para(line, style="note")
            if study.detail:
                builder.para(study.detail)

    if cv.skills:
        builder.h1(cv.section_label("skills"))
        builder.para(" · ".join(cv.skills))

    if cv.languages:
        builder.h1(cv.section_label("languages"))
        builder.para(" · ".join(entry.line() for entry in cv.languages))

    for key, entries in cv.entry_sections():
        # A heading stranded at the foot of a page reads as a missing section.
        builder.keep_together(
            lambda sub, key=key, entries=entries: _pdf_entries(sub, cv, key, entries)
        )

    return builder.build(path)


def _pdf_entries(sub: PdfBuilder, cv: CvDocument, key: str, entries) -> None:
    sub.h1(cv.section_label(key))
    sub.bullets([f"{e.title} ({e.date})" if e.date else e.title for e in entries])


def _pdf_job(sub: PdfBuilder, cv: CvDocument, job) -> None:
    title = " | ".join(p for p in (job.title, job.company) if p)
    sub.story.append(Paragraph(f"<b>{escape(title)}</b>", sub.styles["h2"]))
    meta = " · ".join(p for p in (job.period(cv.language), job.location) if p)
    if meta:
        sub.story.append(Paragraph(escape(meta), sub.styles["note"]))
    sub.bullets(job.bullets)
    sub.story.append(Spacer(1, 2 * mm))
