"""python-docx primitives shared by the CV templates (FR-322).

python-docx exposes paragraphs and tables but not cell shading or paragraph
borders, which both templates need; those are written straight into the
document's XML here so the template modules stay about layout.
"""

from __future__ import annotations

from typing import Any

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Cm, Pt, RGBColor


def hex_to_rgb(value: str) -> RGBColor:
    raw = value.lstrip("#")
    return RGBColor(int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def new_document(
    *, margin_cm: float = 1.8, font: str = "Calibri", size_pt: float = 10.0
) -> Any:
    document = Document()
    section = document.sections[0]
    section.left_margin = Cm(margin_cm)
    section.right_margin = Cm(margin_cm)
    section.top_margin = Cm(margin_cm)
    section.bottom_margin = Cm(margin_cm)

    normal = document.styles["Normal"]
    normal.font.name = font
    normal.font.size = Pt(size_pt)
    # Latin and East-Asian font names are separate attributes in OOXML; without
    # the second one Word silently falls back for any non-ASCII glyph.
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), font)
    normal.paragraph_format.space_after = Pt(3)
    normal.paragraph_format.space_before = Pt(0)
    return document


def para(
    container: Any,
    text: str = "",
    *,
    size: float = 10.0,
    bold: bool = False,
    italic: bool = False,
    colour: str | None = None,
    align: str | None = None,
    space_before: float = 0.0,
    space_after: float = 3.0,
    font: str | None = None,
) -> Any:
    paragraph = container.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(space_before)
    paragraph.paragraph_format.space_after = Pt(space_after)
    if align == "center":
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    elif align == "right":
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    elif align == "justify":
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    if text:
        run = paragraph.add_run(text)
        run.bold = bold
        run.italic = italic
        run.font.size = Pt(size)
        if font:
            run.font.name = font
        if colour:
            run.font.color.rgb = hex_to_rgb(colour)
    return paragraph


def bullet(container: Any, text: str, *, size: float = 10.0, colour: str | None = None) -> Any:
    paragraph = container.add_paragraph(style="List Bullet")
    paragraph.paragraph_format.space_after = Pt(1)
    paragraph.paragraph_format.left_indent = Cm(0.6)
    run = paragraph.add_run(text)
    run.font.size = Pt(size)
    if colour:
        run.font.color.rgb = hex_to_rgb(colour)
    return paragraph


def rule(paragraph: Any, colour: str = "c9d4de", size: int = 6) -> None:
    """Draw a bottom border under a paragraph - the section rule."""
    borders = parse_xml(
        f'<w:pBdr {nsdecls("w")}>'
        f'<w:bottom w:val="single" w:sz="{size}" w:space="2" w:color="{colour.lstrip("#")}"/>'
        f"</w:pBdr>"
    )
    paragraph._p.get_or_add_pPr().append(borders)


def shade(cell: Any, colour: str) -> None:
    fill = colour.lstrip("#")
    cell._tc.get_or_add_tcPr().append(
        parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="{fill}"/>')
    )


def borderless(table: Any) -> None:
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    properties = table._tbl.tblPr
    properties.append(
        parse_xml(
            f'<w:tblBorders {nsdecls("w")}>'
            + "".join(
                f'<w:{edge} w:val="none" w:sz="0" w:space="0" w:color="auto"/>'
                for edge in ("top", "left", "bottom", "right", "insideH", "insideV")
            )
            + "</w:tblBorders>"
        )
    )


def cell_margins(cell: Any, *, left: float = 0.2, right: float = 0.2, top: float = 0.1,
                 bottom: float = 0.1) -> None:
    """Cell padding in centimetres, expressed in OOXML twentieths of a point."""
    def dxa(value: float) -> int:
        return int(value * 567)

    cell._tc.get_or_add_tcPr().append(
        parse_xml(
            f'<w:tcMar {nsdecls("w")}>'
            f'<w:top w:w="{dxa(top)}" w:type="dxa"/>'
            f'<w:start w:w="{dxa(left)}" w:type="dxa"/>'
            f'<w:bottom w:w="{dxa(bottom)}" w:type="dxa"/>'
            f'<w:end w:w="{dxa(right)}" w:type="dxa"/>'
            f"</w:tcMar>"
        )
    )
