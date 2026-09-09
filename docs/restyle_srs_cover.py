#!/usr/bin/env python3
"""Give Dream_Job_Requirements.docx the shared Dream Job cover page.

The SRS predates the two generated documents and has no builder of its own --
the .docx is the source of truth -- so this script edits the cover in place
rather than rendering the whole file. Everything from the top of the body up
to and including the first page break is replaced with the same cover the
Functional Design and the Technical Architecture use: the logo lockup, the
document's own name, the five-hue phase stripe, the product tagline and the
metadata block. Nothing after that first page break is touched.

Re-running is safe. The replaced range is found by the page break, not by a
line count, so the script finds the cover it wrote last time and rewrites it.
The .docx is tracked in git, so that is the way back to the previous cover.

    python3 docs/restyle_srs_cover.py
"""

import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Inches, Pt, RGBColor

HERE = Path(__file__).resolve().parent
TARGET = HERE / "Dream_Job_Requirements.docx"
LOCKUP = HERE / "dreamjob-logo-lockup.png"

# --------------------------------------------------------------------------
# Palette and copy -- the same values build_fdd.py and build_ta.py use, so the
# three covers are one design rather than three near-misses.
# --------------------------------------------------------------------------

PHASES = ["6D54D8",   # 1 profile
          "2D6BC8",   # 2 plan
          "0B7B73",   # 3 discover
          "9F6011",   # 4 apply
          "BC3D66"]   # 5 follow up

INK = RGBColor(0x1F, 0x24, 0x30)        # title
INK_SOFT = RGBColor(0x4A, 0x51, 0x60)   # tagline
GREY = RGBColor(0x6B, 0x72, 0x80)       # metadata labels

BODY_FONT = "Calibri"

PRODUCT = "Dream Job"
TAGLINE = "AI-assisted job discovery and application platform"
DOC_TITLE = "Software Requirements Specification"

META = [
    ("Version", "0.3"),
    ("Date", "8 September 2026"),
    ("Author", "Stephane van der Aa"),
    ("Status", "Draft for review"),
]

LOCKUP_W_IN = 3.1
STRIPE_W_IN = 3.4
STRIPE_H_PT = 6

# The other two documents are set with 1" side margins; this one is narrower
# and reflowing 26 pages to change that is not worth it. The cover alone is
# indented by the difference so all three start on the same vertical line.
COVER_LEFT_IN = 1.0


# --------------------------------------------------------------------------
# OOXML helpers
# --------------------------------------------------------------------------

def _el(tag, **attrs):
    e = OxmlElement(tag)
    for k, v in attrs.items():
        e.set(qn("w:" + k), v)
    return e


def shade(cell, fill):
    cell._tc.get_or_add_tcPr().append(
        _el("w:shd", val="clear", color="auto", fill=fill))


def set_cell_borders(cell, colour=None):
    """Paint all four edges in `colour`, or remove them when colour is None."""
    tcPr = cell._tc.get_or_add_tcPr()
    for existing in tcPr.findall(qn("w:tcBorders")):
        tcPr.remove(existing)
    borders = OxmlElement("w:tcBorders")
    for edge in ("top", "left", "bottom", "right"):
        if colour is None:
            borders.append(_el("w:" + edge, val="nil"))
        else:
            borders.append(_el("w:" + edge, val="single", sz="2", space="0",
                               color=colour))
    tcPr.append(borders)


def set_table_borders(table, colour=None):
    tblPr = table._tbl.tblPr
    for existing in tblPr.findall(qn("w:tblBorders")):
        tblPr.remove(existing)
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        if colour is None:
            borders.append(_el("w:" + edge, val="nil"))
        else:
            borders.append(_el("w:" + edge, val="single", sz="2", space="0",
                               color=colour))
    tblPr.append(borders)


def set_cell_margins(table, top=60, start=110, bottom=60, end=110):
    """Cell padding in twentieths of a point."""
    tblPr = table._tbl.tblPr
    for existing in tblPr.findall(qn("w:tblCellMar")):
        tblPr.remove(existing)
    mar = OxmlElement("w:tblCellMar")
    for tag, val in (("top", top), ("start", start),
                     ("bottom", bottom), ("end", end)):
        mar.append(_el("w:" + tag, w=str(val), type="dxa"))
    tblPr.append(mar)


def fixed_layout(table):
    # Setting autofit writes the w:tblLayout element itself.
    table.autofit = False


def set_table_indent(table, inches):
    tblPr = table._tbl.tblPr
    for existing in tblPr.findall(qn("w:tblInd")):
        tblPr.remove(existing)
    tblPr.append(_el("w:tblInd", w=str(int(Inches(inches).twips)),
                     type="dxa"))


def set_col_widths(table, widths_in):
    """Width must be set on the grid and on every cell for Word to obey it."""
    grid = table._tbl.find(qn("w:tblGrid"))
    if grid is not None:
        table._tbl.remove(grid)
    grid = OxmlElement("w:tblGrid")
    for w in widths_in:
        grid.append(_el("w:gridCol", w=str(int(Inches(w).twips))))
    table._tbl.insert(1, grid)
    for row in table.rows:
        for cell, w in zip(row.cells, widths_in):
            cell.width = Inches(w)


def style_run(run, *, size, color, bold=False):
    run.font.name = BODY_FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    rPr = run._r.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.insert(0, rFonts)
    for attr in ("ascii", "hAnsi", "cs", "eastAsia"):
        rFonts.set(qn("w:" + attr), BODY_FONT)
    return run


# --------------------------------------------------------------------------
# The cover
# --------------------------------------------------------------------------

def build_cover(doc, indent=0.0):
    """Append the cover to the end of the body and return its elements.

    Built at the end because that is where python-docx can register the image
    part; the caller then moves the elements to the front of the body.
    """
    made = []

    def para():
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(indent)
        made.append(p._p)
        return p

    spacer = para()
    spacer.paragraph_format.space_after = Pt(72)

    # The lockup carries the product name, so the title below it is the
    # document's own name rather than a second "Dream Job".
    p = para()
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(58)
    p.add_run().add_picture(str(LOCKUP), width=Inches(LOCKUP_W_IN))

    p = para()
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(10)
    p.paragraph_format.line_spacing = 1.0
    style_run(p.add_run(DOC_TITLE), size=34, color=INK, bold=True)

    # The five phase hues butted together into one flat stripe. A one-row
    # table rather than a paragraph border, because each segment needs its own
    # fill; the borders are painted in each segment's own colour so no
    # hairline shows between them.
    t = doc.add_table(rows=1, cols=len(PHASES))
    made.append(t._tbl)
    fixed_layout(t)
    set_table_indent(t, indent)
    set_table_borders(t, colour=None)
    set_cell_margins(t, top=0, start=0, bottom=0, end=0)
    for cell, colour in zip(t.rows[0].cells, PHASES):
        shade(cell, colour)
        set_cell_borders(cell, colour)
        cp = cell.paragraphs[0]
        cp.paragraph_format.space_after = Pt(0)
        cp.paragraph_format.line_spacing = Pt(STRIPE_H_PT)
        cp.add_run().font.size = Pt(2)
    set_col_widths(t, [STRIPE_W_IN / len(PHASES)] * len(PHASES))

    p = para()
    p.paragraph_format.space_before = Pt(16)
    p.paragraph_format.space_after = Pt(200)
    style_run(p.add_run("%s · %s" % (PRODUCT, TAGLINE)), size=12,
              color=INK_SOFT)

    t = doc.add_table(rows=len(META), cols=2)
    made.append(t._tbl)
    fixed_layout(t)
    set_table_indent(t, indent)
    set_table_borders(t, colour=None)
    set_cell_margins(t, top=30, start=0, bottom=30, end=90)
    for row, (k, v) in zip(t.rows, META):
        kc, vc = row.cells
        kp = kc.paragraphs[0]
        kp.paragraph_format.space_after = Pt(2)
        style_run(kp.add_run(k.upper()), size=8, color=GREY, bold=True)
        vp = vc.paragraphs[0]
        vp.paragraph_format.space_after = Pt(2)
        style_run(vp.add_run(v), size=10, color=INK)
        for cell in (kc, vc):
            set_cell_borders(cell, None)
    set_col_widths(t, [1.15, 5.12])

    p = para()
    p.paragraph_format.space_after = Pt(0)
    p.add_run().add_break(WD_BREAK.PAGE)

    return made


def first_page_break_index(children):
    """Index of the paragraph holding the first page break, or None."""
    for i, child in enumerate(children):
        if child.tag != qn("w:p"):
            continue
        for br in child.iter(qn("w:br")):
            if br.get(qn("w:type")) == "page":
                return i
    return None


def suppress_first_page_header_footer(doc):
    """Give the section a blank first page, as the other two covers have.

    `w:titlePg` alone makes Word look for first-page header and footer parts;
    with none referenced it falls back to the default ones, so an empty header
    and footer are added explicitly.
    """
    section = doc.sections[0]
    section.different_first_page_header_footer = True
    for part in (section.first_page_header, section.first_page_footer):
        part.is_linked_to_previous = False
        for p in part.paragraphs:
            for r in list(p.runs):
                r._r.getparent().remove(r._r)


def main():
    if not TARGET.exists():
        raise SystemExit("not found: %s" % TARGET)
    if not LOCKUP.exists():
        raise SystemExit("logo not found: %s" % LOCKUP)

    doc = Document(str(TARGET))
    body = doc.element.body

    children = list(body.iterchildren())
    cut = first_page_break_index(children)
    if cut is None:
        raise SystemExit(
            "no page break found: cannot tell where the cover ends")
    old = children[:cut + 1]

    indent = max(0.0, COVER_LEFT_IN - doc.sections[0].left_margin.inches)
    new = build_cover(doc, indent=indent)

    # Move the freshly built cover to the front, keeping its order, then drop
    # the cover it replaces.
    anchor = None
    for el in new:
        body.remove(el)
        if anchor is None:
            body.insert(0, el)
        else:
            anchor.addnext(el)
        anchor = el
    for el in old:
        body.remove(el)

    suppress_first_page_header_footer(doc)

    doc.save(str(TARGET))

    print("cover replaced: %d element(s) out, %d in" % (len(old), len(new)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
