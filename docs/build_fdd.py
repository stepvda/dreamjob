#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build the Dream Job Functional Design deliverable.

    cd docs && python3 build_fdd.py

Reads Functional_Design.md and renders it as Dream_Job_Functional_Design.docx.
The markdown is the single source of truth: no prose is written here. This
script only decides presentation — styles, tables, callouts, and where the
generated figures are placed.

Requires: python-docx 1.2, Pillow.
"""

import os
import re
import sys

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from PIL import Image

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "Functional_Design.md")
FIGDIR = os.path.join(HERE, "figures", "out")
DOCSDIR = HERE
OUTPUT = os.path.join(HERE, "Dream_Job_Functional_Design.docx")
LOCKUP = os.path.join(DOCSDIR, "dreamjob-logo-lockup.png")   # the chosen mark

# --------------------------------------------------------------------------
# Page geometry (A4, 1 inch margins -> 6.27in of text)
# --------------------------------------------------------------------------

PAGE_W, PAGE_H = Inches(8.27), Inches(11.69)
MARGIN = Inches(1.0)
TEXT_W_IN = 6.27
FIG_MAX_H_IN = 7.4

# Sections flow on. Short sections (1, 2, 3) would otherwise leave three thin
# pages in a row straight after the contents. Only the first body heading is
# forced onto a new page, to clear the table of contents.
BREAK_BEFORE_SECTION = False

# --------------------------------------------------------------------------
# Palette — sampled from the generated figures so document and figures agree
# --------------------------------------------------------------------------

VIOLET = "6D54D8"   # phase 1, profile
BLUE = "2D6BC8"     # phase 2, plan
TEAL = "0B7B73"     # phase 3, discover
AMBER = "9F6011"    # phase 4, apply
ROSE = "BC3D66"     # phase 5, follow up

INK = "1F2430"      # body text
INK_SOFT = "4A5160"  # secondary text
GREY = "6B7280"     # captions, header/footer
RULE = "D5D8DF"     # hairlines
HEAD_FILL = "EEF0F5"  # table header shading
ZEBRA = "F7F8FA"    # table banding

PHASES = [VIOLET, BLUE, TEAL, AMBER, ROSE]

BODY_FONT = "Calibri"
MONO_FONT = "Consolas"

# --------------------------------------------------------------------------
# Document metadata — taken from the markdown front-matter table
# --------------------------------------------------------------------------

TITLE = "Dream Job"
SUBTITLE = "Functional Design"
TAGLINE = "AI-assisted job discovery and application platform"
AUTHOR = "Stephane van der Aa"
VERSION = "1.0"
DATE = "8 September 2026"
STATUS = "Describes the implemented system"
SPECIFIES = "Dream Job SRS v0.3 (Dream_Job_Requirements.docx)"
COMPANION = "Technical Architecture · DPIA"
RUNNING_HEAD = "Dream Job — Functional Design"

# --------------------------------------------------------------------------
# Figures.
#
# Each entry is placed immediately after the first source block whose raw
# markdown contains `after`. Anchors are distinctive sentence fragments, so
# the placement survives ordinary edits to the surrounding prose.
#
# `fdd-11-coverage.png` is deliberately not used: it renders 155 of 157
# requirements (98.7%) and 9 of 9 Could, while section 12 of the markdown --
# the source of truth, and the Technical Architecture agreeing with it --
# states 154 of 157 (98%) and 8 of 9 Could. A figure may not contradict the
# text it illustrates. Regenerate the chart from the same count and add it
# back here.
# --------------------------------------------------------------------------

FIGURES = [
    dict(
        file="fdd-01-journey.png",
        replaces_code_fence=True,
        caption="The five phases and the fifteen stages inside them. A phase keeps "
                "its colour on every screen it owns, so a user always knows where "
                "in the pipeline they are standing.",
    ),
    dict(
        file="fdd-02-intake.png",
        after="extracted 300×300 photo",
        caption="What the two source documents become, measured on the product "
                "owner's own export and CV — and where the extracted photo "
                "travels to.",
    ),
    dict(
        file="fdd-03-conflicts.png",
        after="Both are genuine ambiguities",
        caption="How the eighteen conflicts break down. Role dates dominate, and "
                "the four employer-name conflicts are one employer written two "
                "ways.",
    ),
    dict(
        file="fdd-04-identity.png",
        after="a rejection is permanent",
        caption="Six independent signals decide a finding's class. Only "
                "*confirmed* merges on its own; everything else waits for the job "
                "seeker.",
    ),
    dict(
        file="fdd-05-planning.png",
        after="vacancies 7 days, company websites",
        caption="Everything the planner settles before a single request is made, "
                "including which records the knowledge base can already answer for.",
    ),
    dict(
        file="fdd-06-financial.png",
        after="inconsistencies flagged rather than silently accepted",
        caption="Five years of filings become two 0–100 scores, with the "
                "weights that produce them and the marked-estimated path taken "
                "when filings are missing.",
    ),
    dict(
        file="fdd-07-scoring.png",
        after="suggests directive refinements",
        caption="Which sub-scores are computed and which are written by the model, "
                "why the dream-job meter stays outside the total, and where the "
                "manual order wins.",
    ),
    dict(
        file="fdd-08-generation.png",
        after="mandatory summary of exactly what",
        caption="The four artefacts per opportunity, the check that gates "
                "approval, and the two documents that are never sent to anyone.",
    ),
    dict(
        file="fdd-09-learning.png",
        after="the analysis needs it",
        caption="The follow-up loop. Every path from a response back to a changed "
                "directive passes through the job seeker, and the previous "
                "directive version is kept.",
    ),
    dict(
        file="fdd-10-sample-size.png",
        after="entirely different evidence",
        feature=True,
        caption="The same observed reply rate — 50% — at six sample "
                "sizes. The percentage never moves; only the interval around it "
                "does. This is why the advice refuses to speak below six resolved "
                "applications.",
    ),
    dict(
        file="fdd-09b-segments.png",
        after="an invented segment is rejected",
        caption="The verified example. Five of eight against one of nine is a wide "
                "gap and drives the advice, but at seventeen applications the "
                "intervals still overlap.",
    ),
]

# --------------------------------------------------------------------------
# Callouts. Blockquotes in the markdown become shaded boxes, keyed on the
# bold lead-in so a reviewer can find every judgement call quickly.
# --------------------------------------------------------------------------

CALLOUTS = [
    # (prefix match on the lead-in, kind, accent, fill)
    ("CR-401", "banner", ROSE, "FCEEF3"),
    ("FR-263", "banner", AMBER, "FDF4E6"),
    ("Decision", "side", VIOLET, "F4F1FD"),
    ("Extension", "side", TEAL, "EAF5F3"),
    ("Open item", "side", AMBER, "FDF4E6"),
    ("Note", "side", GREY, "F4F5F7"),
]
DEFAULT_CALLOUT = ("side", BLUE, "EEF3FB")


# ==========================================================================
# Low-level OOXML helpers
# ==========================================================================

def _el(tag, **attrs):
    e = OxmlElement(tag)
    for k, v in attrs.items():
        e.set(qn("w:" + k), v)
    return e


def shade(cell, fill):
    cell._tc.get_or_add_tcPr().append(_el("w:shd", val="clear", color="auto", fill=fill))


def set_cell_borders(cell, **edges):
    """edges: top/left/bottom/right -> dict(sz=..., color=..., val=...)."""
    tcPr = cell._tc.get_or_add_tcPr()
    for existing in tcPr.findall(qn("w:tcBorders")):
        tcPr.remove(existing)
    borders = OxmlElement("w:tcBorders")
    for edge in ("top", "left", "bottom", "right"):
        spec = edges.get(edge)
        if spec is None:
            continue
        borders.append(_el(
            "w:" + edge,
            val=spec.get("val", "single"),
            sz=str(spec.get("sz", 6)),
            space="0",
            color=spec.get("color", RULE),
        ))
    tcPr.append(borders)


def set_cell_margins(table, top=60, start=110, bottom=60, end=110):
    """Cell padding in twentieths of a point."""
    tblPr = table._tbl.tblPr
    mar = OxmlElement("w:tblCellMar")
    for tag, val in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        mar.append(_el("w:" + tag, w=str(val), type="dxa"))
    tblPr.append(mar)


def set_vertical_center(cell):
    cell._tc.get_or_add_tcPr().append(_el("w:vAlign", val="center"))


def fixed_layout(table):
    table.autofit = False
    table._tbl.tblPr.append(_el("w:tblLayout", type="fixed"))


def repeat_header(row):
    row._tr.get_or_add_trPr().append(_el("w:tblHeader", val="true"))


def keep_row_together(row):
    row._tr.get_or_add_trPr().append(_el("w:cantSplit", val="true"))


def add_field(paragraph, instruction, placeholder="1"):
    """A real Word field, so Word computes the value."""
    r = paragraph.add_run()
    r._r.append(_el("w:fldChar", fldCharType="begin"))
    r = paragraph.add_run()
    it = OxmlElement("w:instrText")
    it.set(qn("xml:space"), "preserve")
    it.text = instruction
    r._r.append(it)
    r = paragraph.add_run()
    r._r.append(_el("w:fldChar", fldCharType="separate"))
    paragraph.add_run(placeholder)
    r = paragraph.add_run()
    r._r.append(_el("w:fldChar", fldCharType="end"))


def add_toc_field(paragraph, placeholder):
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), r'TOC \o "1-3" \h \z \u')
    run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    rPr.append(_el("w:i", val="true"))
    rPr.append(_el("w:color", val=GREY))
    run.append(rPr)
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = placeholder
    run.append(t)
    fld.append(run)
    paragraph._p.append(fld)


def ask_word_to_update_fields(doc):
    doc.settings.element.append(_el("w:updateFields", val="true"))


def paragraph_border(paragraph, edge="bottom", sz=6, color=RULE, space="4"):
    pPr = paragraph._p.get_or_add_pPr()
    bdr = pPr.find(qn("w:pBdr"))
    if bdr is None:
        bdr = OxmlElement("w:pBdr")
        pPr.append(bdr)
    bdr.append(_el("w:" + edge, val="single", sz=str(sz), space=space, color=color))


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


# ==========================================================================
# Styles
# ==========================================================================

def build_styles(doc):
    styles = doc.styles

    normal = styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = RGBColor.from_string(INK)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
    pf = normal.paragraph_format
    pf.space_after = Pt(7)
    pf.space_before = Pt(0)
    pf.line_spacing = 1.14
    pf.widow_control = True

    def heading(name, size, color, before, after, bold=True, caps=False):
        st = styles[name]
        st.font.name = BODY_FONT
        st.font.size = Pt(size)
        st.font.bold = bold
        st.font.color.rgb = RGBColor.from_string(color)
        st.font.all_caps = caps
        st.paragraph_format.space_before = Pt(before)
        st.paragraph_format.space_after = Pt(after)
        st.paragraph_format.keep_with_next = True
        st.paragraph_format.keep_together = True
        st.paragraph_format.line_spacing = 1.05
        return st

    h1 = heading("Heading 1", 18, VIOLET, 30, 10)
    h1.paragraph_format.page_break_before = BREAK_BEFORE_SECTION
    heading("Heading 2", 13, INK, 16, 5)
    heading("Heading 3", 11, INK_SOFT, 12, 4)

    def new_para_style(name, size=10.5, color=INK, italic=False, bold=False,
                       before=0, after=7, align=None, left=0.0, hanging=0.0,
                       spacing=1.14, font=BODY_FONT):
        st = styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        st.base_style = styles["Normal"]
        st.quick_style = True
        st.font.name = font
        st.font.size = Pt(size)
        st.font.italic = italic
        st.font.bold = bold
        st.font.color.rgb = RGBColor.from_string(color)
        p = st.paragraph_format
        p.space_before = Pt(before)
        p.space_after = Pt(after)
        p.line_spacing = spacing
        if align is not None:
            p.alignment = align
        if left:
            p.left_indent = Inches(left)
        if hanging:
            p.first_line_indent = Inches(-hanging)
        return st

    new_para_style("Figure Caption", size=9, color=GREY, before=5, after=16,
                   align=WD_ALIGN_PARAGRAPH.CENTER, spacing=1.10,
                   left=0.35)
    new_para_style("Figure Image", after=0, before=12,
                   align=WD_ALIGN_PARAGRAPH.CENTER)
    new_para_style("DJ Bullet", left=0.26, hanging=0.26, after=5)
    new_para_style("DJ Number", left=0.30, hanging=0.30, after=5)
    new_para_style("DJ Table Text", size=9.5, after=0, before=0, spacing=1.08)
    new_para_style("DJ Table Head", size=9.5, bold=True, after=0, before=0,
                   spacing=1.08, color=INK)
    new_para_style("DJ Callout", size=10, after=0, before=0, spacing=1.12)
    new_para_style("DJ Callout Banner", size=10, bold=True, color="FFFFFF",
                   after=0, before=0, spacing=1.10)
    new_para_style("DJ Section Title", size=18, bold=True, color=VIOLET,
                   before=0, after=10)
    new_para_style("DJ Colophon", size=9, italic=True, color=GREY, before=18,
                   align=WD_ALIGN_PARAGRAPH.CENTER)
    new_para_style("DJ Title", size=34, bold=True, color=INK, after=0,
                   spacing=1.0)
    new_para_style("DJ Subtitle", size=22, color=VIOLET, after=6, spacing=1.0)
    new_para_style("DJ Tagline", size=12, color=INK_SOFT, after=0)
    new_para_style("DJ Meta", size=10, color=INK_SOFT, after=2)
    new_para_style("DJ Hint", size=8.5, italic=True, color=GREY, before=4,
                   after=14)

    code = styles.add_style("Code Inline", WD_STYLE_TYPE.CHARACTER)
    code.font.name = MONO_FONT
    code.font.size = Pt(9.5)
    code.font.color.rgb = RGBColor.from_string("2C3E57")
    code.quick_style = True

    link = styles.add_style("Doc Link", WD_STYLE_TYPE.CHARACTER)
    link.font.name = BODY_FONT
    link.font.color.rgb = RGBColor.from_string(BLUE)
    link.quick_style = True


# ==========================================================================
# Inline markdown -> runs
# ==========================================================================

INLINE = re.compile(
    r"(\*\*.+?\*\*"                      # bold
    r"|(?<!\*)\*(?!\s)[^*]+?(?<!\s)\*(?!\*)"  # italic
    r"|`[^`]+`"                          # code
    r"|\[[^\]]+?\]\([^)]+?\))",          # link
    re.S,
)


def add_runs(paragraph, text, bold=False, italic=False, color=None, size=None):
    """Render inline markdown into runs on `paragraph`."""

    def emit(chunk, b=bold, i=italic, style=None):
        if not chunk:
            return
        run = paragraph.add_run(chunk)
        if style:
            run.style = style
        run.bold = b or None
        run.italic = i or None
        if color and style is None:
            run.font.color.rgb = RGBColor.from_string(color)
        if size:
            run.font.size = Pt(size)

    pos = 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            emit(text[pos:m.start()])
        tok = m.group(0)
        if tok.startswith("**"):
            add_runs(paragraph, tok[2:-2], bold=True, italic=italic,
                     color=color, size=size)
        elif tok.startswith("`"):
            emit(tok[1:-1], style="Code Inline")
        elif tok.startswith("["):
            label = tok[1:tok.index("](")]
            emit(label, style="Doc Link")
        else:
            add_runs(paragraph, tok[1:-1], bold=bold, italic=True,
                     color=color, size=size)
        pos = m.end()
    if pos < len(text):
        emit(text[pos:])


def plain(text):
    text = re.sub(r"\[([^\]]+?)\]\([^)]+?\)", r"\1", text)
    return text.replace("**", "").replace("`", "").replace("*", "")


# ==========================================================================
# Markdown block parser
# ==========================================================================

def parse_blocks(md):
    lines = md.split("\n")

    # Skip the front matter: everything before the first "## " heading is the
    # title, tagline and control table, which the title page renders instead.
    start = next(i for i, l in enumerate(lines) if l.startswith("## "))
    lines = lines[start:]

    blocks = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if stripped.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            blocks.append(("code", "\n".join(buf)))
            continue

        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            blocks.append(("h", level, stripped[level:].strip()))
            i += 1
            continue

        if stripped.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append(("quote", " ".join(x for x in buf if x)))
            continue

        if stripped.startswith("|"):
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                raw = lines[i].strip()
                cells = [c.strip() for c in raw.strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            blocks.append(("table", rows))
            continue

        if stripped in ("---", "***", "___"):
            blocks.append(("hr",))
            i += 1
            continue

        bullet = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        number = re.match(r"^(\s*)(\d+)\.\s+(.*)$", line)
        if bullet or number:
            kind = "ul" if bullet else "ol"
            items = []
            while i < n:
                b = re.match(r"^(\s*)[-*]\s+(.*)$", lines[i])
                o = re.match(r"^(\s*)(\d+)\.\s+(.*)$", lines[i])
                if kind == "ul" and b:
                    items.append(b.group(2))
                elif kind == "ol" and o:
                    items.append(o.group(3))
                elif lines[i].strip() and lines[i].startswith((" ", "\t")):
                    items[-1] += " " + lines[i].strip()   # continuation line
                else:
                    break
                i += 1
            blocks.append((kind, items))
            continue

        buf = []
        while i < n and lines[i].strip() and not re.match(
                r"^\s*(#|>|\||```|[-*]\s|\d+\.\s|---\s*$)", lines[i]):
            buf.append(lines[i].strip())
            i += 1
        blocks.append(("p", " ".join(buf)))

    return blocks


# ==========================================================================
# Renderers
# ==========================================================================

class Builder:
    def __init__(self, doc):
        self.doc = doc
        self.fig_no = 0
        self.pending = list(FIGURES)
        self.embedded = []
        self.first_body_heading = True
        self.bind_next = 0

    # -- figures --------------------------------------------------------

    def figure(self, spec):
        path = os.path.join(FIGDIR, spec["file"])
        if not os.path.exists(path):
            raise SystemExit("missing figure: %s" % path)

        with Image.open(path) as im:
            w_px, h_px = im.size
        width_in = TEXT_W_IN
        if h_px * width_in / w_px > FIG_MAX_H_IN:
            width_in = FIG_MAX_H_IN * w_px / h_px

        self.fig_no += 1
        feature = spec.get("feature", False)

        p = self.doc.add_paragraph(style="Figure Image")
        p.paragraph_format.keep_with_next = True
        p.paragraph_format.keep_together = True
        if feature:
            p.paragraph_format.space_before = Pt(20)
        p.add_run().add_picture(path, width=Inches(width_in))

        cap = self.doc.add_paragraph(style="Figure Caption")
        cap.paragraph_format.keep_together = True
        cap.paragraph_format.right_indent = Inches(0.35)
        if feature:
            cap.paragraph_format.space_after = Pt(20)
        run = cap.add_run("Figure %d. " % self.fig_no)
        run.bold = True
        run.font.color.rgb = RGBColor.from_string(VIOLET)
        add_runs(cap, spec["caption"], color=GREY)

        self.embedded.append((self.fig_no, spec["file"]))

    def release_figures(self, raw):
        """Emit any figure anchored to the block just rendered."""
        for spec in list(self.pending):
            anchor = spec.get("after")
            if anchor and anchor in raw:
                self.figure(spec)
                self.pending.remove(spec)

    # -- blocks ---------------------------------------------------------

    def heading(self, level, text):
        # markdown ## -> Heading 1, ### -> Heading 2
        style = "Heading %d" % max(1, min(3, level - 1))
        p = self.doc.add_paragraph(style=style)
        if style == "Heading 1" and self.first_body_heading:
            p.paragraph_format.page_break_before = True
            p.paragraph_format.space_before = Pt(0)
            self.first_body_heading = False
        add_runs(p, text)
        if style == "Heading 1":
            paragraph_border(p, "bottom", sz=8, color=VIOLET, space="6")
            # the heading style already keeps with the next block; one more
            # keeps a section opening from stranding at the foot of a page
            self.bind_next = 1
        else:
            self.bind_next = 0

    def hold(self, paragraph):
        """Keep this paragraph with the next, while a heading is still binding."""
        if self.bind_next > 0:
            paragraph.paragraph_format.keep_with_next = True
            self.bind_next -= 1

    def paragraph(self, text):
        p = self.doc.add_paragraph()
        add_runs(p, text)
        self.hold(p)

    def bullets(self, items):
        for item in items:
            p = self.doc.add_paragraph(style="DJ Bullet")
            r = p.add_run("•\t")
            r.font.color.rgb = RGBColor.from_string(VIOLET)
            r.bold = True
            add_runs(p, item)
            self.hold(p)
        self.doc.paragraphs[-1].paragraph_format.space_after = Pt(7)

    def numbers(self, items):
        for idx, item in enumerate(items, 1):
            p = self.doc.add_paragraph(style="DJ Number")
            r = p.add_run("%d.\t" % idx)
            r.bold = True
            r.font.color.rgb = RGBColor.from_string(VIOLET)
            add_runs(p, item)
            self.hold(p)
        self.doc.paragraphs[-1].paragraph_format.space_after = Pt(7)

    def code_block(self, text):
        p = self.doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(0.15)
        run = p.add_run(text)
        run.style = "Code Inline"
        run.font.size = Pt(8)

    # -- tables ---------------------------------------------------------

    def table(self, rows):
        header, body = rows[0], rows[1:]
        ncols = max(len(r) for r in rows)
        header = header + [""] * (ncols - len(header))

        widths = self.column_widths(rows, ncols)

        t = self.doc.add_table(rows=1, cols=ncols)
        t.style = "Table Grid"
        t.alignment = WD_TABLE_ALIGNMENT.LEFT
        fixed_layout(t)
        set_cell_margins(t)

        for cell, text in zip(t.rows[0].cells, header):
            cell.paragraphs[0].style = self.doc.styles["DJ Table Head"]
            add_runs(cell.paragraphs[0], text)
            shade(cell, HEAD_FILL)
            set_vertical_center(cell)
            set_cell_borders(
                cell,
                top={"sz": 4, "color": RULE},
                left={"sz": 4, "color": RULE},
                right={"sz": 4, "color": RULE},
                bottom={"sz": 12, "color": VIOLET},
            )
        repeat_header(t.rows[0])
        keep_row_together(t.rows[0])

        for n, raw_row in enumerate(body):
            row = t.add_row()
            keep_row_together(row)
            cells = raw_row + [""] * (ncols - len(raw_row))
            for cell, text in zip(row.cells, cells):
                cell.paragraphs[0].style = self.doc.styles["DJ Table Text"]
                add_runs(cell.paragraphs[0], text)
                set_vertical_center(cell)
                if n % 2 == 1:
                    shade(cell, ZEBRA)
                set_cell_borders(
                    cell,
                    top={"sz": 4, "color": RULE},
                    left={"sz": 4, "color": RULE},
                    right={"sz": 4, "color": RULE},
                    bottom={"sz": 4, "color": RULE},
                )

        set_col_widths(t, widths)
        self.doc.add_paragraph().paragraph_format.space_after = Pt(2)

    @staticmethod
    def column_widths(rows, ncols):
        """Proportional to content, damped, with a floor, summing to the text width."""
        weights = []
        for c in range(ncols):
            longest = 1
            for r in rows:
                if c < len(r):
                    longest = max(longest, len(plain(r[c])))
            weights.append(min(longest, 90) ** 0.62)

        total = sum(weights)
        widths = [TEXT_W_IN * w / total for w in weights]

        floor = 0.72 if ncols > 2 else 0.95
        for _ in range(6):
            deficit = sum(floor - w for w in widths if w < floor)
            if deficit <= 0.0001:
                break
            spare = [i for i, w in enumerate(widths) if w > floor]
            pool = sum(widths[i] - floor for i in spare)
            if pool <= 0:
                break
            for i in spare:
                widths[i] -= deficit * (widths[i] - floor) / pool
            widths = [max(w, floor) for w in widths]

        scale = TEXT_W_IN / sum(widths)
        return [w * scale for w in widths]

    # -- callouts -------------------------------------------------------

    def callout(self, text):
        lead, rest = self.split_lead(text)
        kind, accent, fill = DEFAULT_CALLOUT
        for prefix, k, a, f in CALLOUTS:
            if plain(lead).startswith(prefix) or plain(lead).lstrip("*").startswith(prefix):
                kind, accent, fill = k, a, f
                break

        if kind == "banner":
            self.banner_callout(lead, rest, accent, fill)
        else:
            self.side_callout(lead, rest, accent, fill)

    @staticmethod
    def lead_gap(rest):
        """No gap when the sentence continues straight into punctuation."""
        return "" if rest[:1] in ",.;:!?)" else "  "

    @staticmethod
    def split_lead(text):
        """A blockquote opens with a bold lead-in; it becomes the callout label."""
        m = re.match(r"^\*\*(.+?)\*\*(.*)$", text, re.S)
        if not m:
            return "", text
        return m.group(1).strip(), m.group(2).strip()

    def _shell(self, rows=1):
        t = self.doc.add_table(rows=rows, cols=1)
        t.style = "Table Grid"
        t.alignment = WD_TABLE_ALIGNMENT.LEFT
        fixed_layout(t)
        set_col_widths(t, [TEXT_W_IN])
        return t

    def side_callout(self, lead, rest, accent, fill):
        self.doc.add_paragraph().paragraph_format.space_after = Pt(2)
        t = self._shell(1)
        set_cell_margins(t, top=120, bottom=120, start=170, end=170)
        cell = t.rows[0].cells[0]
        keep_row_together(t.rows[0])
        shade(cell, fill)
        set_cell_borders(
            cell,
            left={"sz": 30, "color": accent},
            top={"sz": 4, "color": fill},
            bottom={"sz": 4, "color": fill},
            right={"sz": 4, "color": fill},
        )
        p = cell.paragraphs[0]
        p.style = self.doc.styles["DJ Callout"]
        if lead:
            run = p.add_run(lead + self.lead_gap(rest))
            run.bold = True
            run.font.color.rgb = RGBColor.from_string(accent)
        add_runs(p, rest)
        self.doc.add_paragraph().paragraph_format.space_after = Pt(4)

    def banner_callout(self, lead, rest, accent, fill):
        """For the hard rules: a solid colour bar carrying the lead sentence."""
        self.doc.add_paragraph().paragraph_format.space_after = Pt(2)
        t = self._shell(2)
        set_cell_margins(t, top=110, bottom=110, start=170, end=170)

        bar = t.rows[0].cells[0]
        keep_row_together(t.rows[0])
        shade(bar, accent)
        set_cell_borders(
            bar,
            top={"sz": 4, "color": accent},
            left={"sz": 4, "color": accent},
            right={"sz": 4, "color": accent},
            bottom={"sz": 4, "color": accent},
        )
        p = bar.paragraphs[0]
        p.style = self.doc.styles["DJ Callout Banner"]
        add_runs(p, lead, bold=True)
        for run in p.runs:
            run.font.color.rgb = RGBColor.from_string("FFFFFF")

        body = t.rows[1].cells[0]
        keep_row_together(t.rows[1])
        shade(body, fill)
        set_cell_borders(
            body,
            top={"sz": 4, "color": accent},
            left={"sz": 12, "color": accent},
            right={"sz": 12, "color": accent},
            bottom={"sz": 12, "color": accent},
        )
        p = body.paragraphs[0]
        p.style = self.doc.styles["DJ Callout"]
        add_runs(p, rest)
        self.doc.add_paragraph().paragraph_format.space_after = Pt(4)


# ==========================================================================
# Front matter
# ==========================================================================

def colour_rule(doc, width_in=TEXT_W_IN, height_pt=7):
    t = doc.add_table(rows=1, cols=len(PHASES))
    t.style = "Table Grid"
    fixed_layout(t)
    set_cell_margins(t, top=0, bottom=0, start=0, end=0)
    for cell, colour in zip(t.rows[0].cells, PHASES):
        shade(cell, colour)
        set_cell_borders(
            cell,
            top={"sz": 2, "color": colour},
            left={"sz": 2, "color": colour},
            right={"sz": 2, "color": colour},
            bottom={"sz": 2, "color": colour},
        )
        cell.paragraphs[0].paragraph_format.space_after = Pt(0)
        cell.paragraphs[0].paragraph_format.line_spacing = Pt(height_pt)
        cell.paragraphs[0].add_run().font.size = Pt(2)
    set_col_widths(t, [width_in / len(PHASES)] * len(PHASES))
    return t


def title_page(doc):
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(72)

    # The lockup carries the product name, so the title below it is the
    # document's own name rather than a second "Dream Job".
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(58)
    p.add_run().add_picture(LOCKUP, width=Inches(3.1))

    p = doc.add_paragraph(style="DJ Title")
    p.paragraph_format.space_after = Pt(10)
    p.add_run(SUBTITLE)

    colour_rule(doc, width_in=3.4, height_pt=6)

    p = doc.add_paragraph(style="DJ Tagline")
    p.paragraph_format.space_before = Pt(16)
    p.paragraph_format.space_after = Pt(200)
    p.add_run("%s · %s" % (TITLE, TAGLINE))

    meta = [
        ("Version", VERSION),
        ("Date", DATE),
        ("Author", AUTHOR),
        ("Status", STATUS),
    ]
    t = doc.add_table(rows=len(meta), cols=2)
    fixed_layout(t)
    set_cell_margins(t, top=30, bottom=30, start=0, end=90)
    for row, (k, v) in zip(t.rows, meta):
        kp = row.cells[0].paragraphs[0]
        kp.style = doc.styles["DJ Meta"]
        r = kp.add_run(k.upper())
        r.bold = True
        r.font.size = Pt(8)
        r.font.color.rgb = RGBColor.from_string(GREY)
        vp = row.cells[1].paragraphs[0]
        vp.style = doc.styles["DJ Meta"]
        r = vp.add_run(v)
        r.font.color.rgb = RGBColor.from_string(INK)
    set_col_widths(t, [1.15, 5.12])


def document_control(doc, builder):
    p = doc.add_paragraph(style="Heading 1")
    p.paragraph_format.page_break_before = False
    p.add_run("Document control")
    paragraph_border(p, "bottom", sz=8, color=VIOLET, space="6")

    rows = [
        ("Document", "Functional Design (FDD)"),
        ("Version", VERSION),
        ("Date", DATE),
        ("Author", AUTHOR),
        ("Status", STATUS),
        ("Specifies", SPECIFIES),
        ("Companion", COMPANION),
        ("Generated from", "docs/Functional_Design.md by docs/build_fdd.py"),
    ]
    t = doc.add_table(rows=len(rows), cols=2)
    t.style = "Table Grid"
    fixed_layout(t)
    set_cell_margins(t)
    for n, (k, v) in enumerate(rows):
        row = t.rows[n]
        keep_row_together(row)
        kc, vc = row.cells
        kp = kc.paragraphs[0]
        kp.style = doc.styles["DJ Table Head"]
        kp.add_run(k)
        shade(kc, HEAD_FILL)
        vp = vc.paragraphs[0]
        vp.style = doc.styles["DJ Table Text"]
        add_runs(vp, v)
        for cell in (kc, vc):
            set_vertical_center(cell)
            set_cell_borders(
                cell,
                top={"sz": 4, "color": RULE},
                left={"sz": 4, "color": RULE},
                right={"sz": 4, "color": RULE},
                bottom={"sz": 4, "color": RULE},
            )
    set_col_widths(t, [1.55, 4.72])

    doc.add_paragraph().paragraph_format.space_after = Pt(10)

    p = doc.add_paragraph(style="DJ Section Title")
    p.paragraph_format.space_before = Pt(18)
    p.add_run("Contents")
    paragraph_border(p, "bottom", sz=8, color=VIOLET, space="6")

    toc = doc.add_paragraph()
    toc.paragraph_format.space_after = Pt(2)
    add_toc_field(toc, "Table of contents — not yet built.")

    hint = doc.add_paragraph(style="DJ Hint")
    hint.add_run(
        "Click in the line above and press F9, or right-click and choose "
        "“Update field” → “Update entire table”, to populate it."
    )


def header_and_footer(section):
    section.header.is_linked_to_previous = False
    section.footer.is_linked_to_previous = False

    def tabs(paragraph):
        paragraph.paragraph_format.tab_stops.add_tab_stop(
            Inches(TEXT_W_IN), WD_TAB_ALIGNMENT.RIGHT)

    p = section.header.paragraphs[0]
    p.text = ""
    tabs(p)
    p.paragraph_format.space_after = Pt(4)
    r = p.add_run(RUNNING_HEAD)
    r.font.size = Pt(8.5)
    r.font.color.rgb = RGBColor.from_string(GREY)
    r.font.name = BODY_FONT
    r = p.add_run("\tVersion %s" % VERSION)
    r.font.size = Pt(8.5)
    r.font.color.rgb = RGBColor.from_string(GREY)
    r.font.name = BODY_FONT
    paragraph_border(p, "bottom", sz=4, color=RULE, space="3")

    p = section.footer.paragraphs[0]
    p.text = ""
    tabs(p)
    paragraph_border(p, "top", sz=4, color=RULE, space="3")
    p.paragraph_format.space_before = Pt(4)
    r = p.add_run(DATE)
    r.font.size = Pt(8.5)
    r.font.color.rgb = RGBColor.from_string(GREY)
    r.font.name = BODY_FONT
    r = p.add_run("\tPage ")
    r.font.size = Pt(8.5)
    r.font.color.rgb = RGBColor.from_string(GREY)
    r.font.name = BODY_FONT
    add_field(p, " PAGE ")
    r = p.add_run(" of ")
    add_field(p, " NUMPAGES ")
    for run in p.runs:
        run.font.size = Pt(8.5)
        run.font.name = BODY_FONT
        if run.font.color.rgb is None:
            run.font.color.rgb = RGBColor.from_string(GREY)


# ==========================================================================
# Build
# ==========================================================================

def build():
    with open(SOURCE, encoding="utf-8") as fh:
        md = fh.read()

    doc = Document()
    build_styles(doc)

    sec = doc.sections[0]
    sec.page_width, sec.page_height = PAGE_W, PAGE_H
    for attr in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(sec, attr, MARGIN)
    sec.header_distance = Inches(0.6)
    sec.footer_distance = Inches(0.6)

    title_page(doc)

    body = doc.add_section(WD_SECTION.NEW_PAGE)
    body.page_width, body.page_height = PAGE_W, PAGE_H
    for attr in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(body, attr, MARGIN)
    body.header_distance = Inches(0.6)
    body.footer_distance = Inches(0.6)
    header_and_footer(body)

    builder = Builder(doc)
    document_control(doc, builder)

    blocks = parse_blocks(md)
    for block in blocks:
        kind = block[0]
        raw = ""

        if kind == "h":
            builder.heading(block[1], block[2])
            raw = block[2]
        elif kind == "p":
            text = block[1]
            if text.startswith("*Companion documents") or text.startswith(
                    "*Companion documents:"):
                p = doc.add_paragraph(style="DJ Colophon")
                add_runs(p, text)
            else:
                builder.paragraph(text)
            raw = text
        elif kind == "ul":
            builder.bullets(block[1])
            raw = " ".join(block[1])
        elif kind == "ol":
            builder.numbers(block[1])
            raw = " ".join(block[1])
        elif kind == "table":
            builder.table(block[1])
            raw = " ".join(" ".join(r) for r in block[1])
        elif kind == "quote":
            builder.callout(block[1])
            raw = block[1]
        elif kind == "code":
            # The ASCII phase map is rendered as the journey figure instead.
            spec = next((f for f in builder.pending
                         if f.get("replaces_code_fence")), None)
            if spec and "FOLLOW UP" in block[1]:
                builder.figure(spec)
                builder.pending.remove(spec)
            else:
                builder.code_block(block[1])
            raw = ""
        elif kind == "hr":
            continue

        if raw:
            builder.release_figures(raw)

    if builder.pending:
        raise SystemExit("figures never placed: %s"
                         % ", ".join(f["file"] for f in builder.pending))

    doc.core_properties.title = "%s — %s" % (TITLE, SUBTITLE)
    doc.core_properties.subject = TAGLINE
    doc.core_properties.author = AUTHOR
    doc.core_properties.category = "Functional Design"
    doc.core_properties.comments = "Generated from Functional_Design.md by build_fdd.py"

    ask_word_to_update_fields(doc)
    doc.save(OUTPUT)
    return builder


def verify(builder):
    doc = Document(OUTPUT)

    paragraphs = len(doc.paragraphs)
    tables = len(doc.tables)
    shapes = len(doc.inline_shapes)

    headings = {}
    for p in doc.paragraphs:
        if p.style.name.startswith("Heading "):
            headings[p.style.name] = headings.get(p.style.name, 0) + 1

    body = doc.element.body
    toc = body.findall(".//" + qn("w:fldSimple"))
    has_toc = any("TOC" in (f.get(qn("w:instr")) or "") for f in toc)

    captions = [p.text for p in doc.paragraphs
                if p.style.name == "Figure Caption"]

    # every image part actually stored in the package
    image_parts = [p.partname for p in doc.part.package.parts
                   if str(p.partname).startswith("/word/media/")]

    print("verification")
    print("  paragraphs        %d" % paragraphs)
    print("  tables            %d" % tables)
    print("  inline shapes     %d" % shapes)
    print("  image parts       %d" % len(image_parts))
    print("  headings          %s" % ", ".join(
        "%s=%d" % (k, v) for k, v in sorted(headings.items())))
    print("  TOC field         %s" % ("present" if has_toc else "MISSING"))
    print("  captions          %d" % len(captions))
    for n, name in builder.embedded:
        print("    Figure %-2d %s" % (n, name))

    expected_images = len(builder.embedded) + 1  # figures + title-page lockup
    ok = (shapes == expected_images
          and len(image_parts) == expected_images
          and len(captions) == len(builder.embedded)
          and has_toc
          and headings.get("Heading 1", 0) > 0)

    size_kb = os.path.getsize(OUTPUT) / 1024.0
    print("  size              %.0f KB" % size_kb)
    print("  result            %s" % ("PASS" if ok else "FAIL"))

    return dict(ok=ok, paragraphs=paragraphs, tables=tables, shapes=shapes,
                headings=sum(headings.values()), captions=len(captions),
                size_kb=size_kb)


if __name__ == "__main__":
    b = build()
    result = verify(b)
    print("\nwrote %s" % OUTPUT)
    sys.exit(0 if result["ok"] else 1)
