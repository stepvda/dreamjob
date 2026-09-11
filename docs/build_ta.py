#!/usr/bin/env python3
"""
Build the Dream Job Technical Architecture Word deliverable.

    cd /Users/nstephane/Dev/AI_Data_Science_training/dreamjob/docs
    python3 build_ta.py

Content comes from Technical_Architecture.md and is not rewritten here. The
script parses that markdown into blocks, then applies a placement table that
(a) drops the generated figures at named anchors, (b) replaces the two ASCII
diagrams with their rendered figures, and (c) turns the two measured code
blocks into real tables that keep their chart alongside them.

Requires only python-docx and Pillow.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from PIL import Image

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor, Emu

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "Technical_Architecture.md"
FIGDIR = HERE / "figures" / "out"
OUTPUT = HERE / "Dream_Job_Technical_Architecture.docx"
DOCS = HERE
LOCKUP = DOCS / "dreamjob-logo-lockup.png"   # the chosen mark, not an exploration

AUTHOR = "Stephane van der Aa"

# --------------------------------------------------------------------------
# Palette — sampled from the generated figures so document and figures agree
# --------------------------------------------------------------------------

VIOLET = RGBColor(0x5A, 0x44, 0xC4)   # primary accent (Profile hue, deepened)
VIOLET_DEEP = RGBColor(0x3D, 0x2F, 0x8F)
BLUE = RGBColor(0x2D, 0x6B, 0xC8)
INK = RGBColor(0x1B, 0x1F, 0x2A)
SLATE = RGBColor(0x3B, 0x42, 0x52)
MUTED = RGBColor(0x5B, 0x64, 0x72)
FAINT = RGBColor(0x8A, 0x93, 0xA3)

SH_HEADER = "4B3AA8"      # table header fill
SH_CODE = "F4F5F8"        # code block fill
SH_CALLOUT = "F5F2FD"     # callout fill
SH_KEYCOL = "F0EDFB"      # key column fill in the control table
RULE_LIGHT = "DCE0E7"
RULE_ACCENT = "5A44C4"

# The five phase hues, in journey order, drawn as the stripe under the cover
# title. Same values and order as the Functional Design and the SRS, so the
# three documents present one cover.
PHASES = ["6D54D8",   # 1 profile
          "2D6BC8",   # 2 plan
          "0B7B73",   # 3 discover
          "9F6011",   # 4 apply
          "BC3D66"]   # 5 follow up

COVER_LABEL = RGBColor(0x6B, 0x72, 0x80)   # cover metadata labels

PRODUCT = "Dream Job"
TAGLINE = "AI-assisted job discovery and application platform"

BODY_FONT = "Calibri"
HEAD_FONT = "Calibri Light"
MONO_FONT = "Consolas"

TEXT_WIDTH = 6.30         # inches, A4 with 1" side margins
FIG_WIDTH = 6.30

REQ_RE = re.compile(r"\b(?:FR|NFR|CR|IR|DR)-\d{3}\b")


# --------------------------------------------------------------------------
# Low-level OOXML helpers
# --------------------------------------------------------------------------

def _el(tag, **attrs):
    e = OxmlElement(tag)
    for k, v in attrs.items():
        e.set(qn("w:" + k), v)
    return e


def shade(element, fill):
    """Apply a solid fill to a paragraph (pPr) or table cell (tcPr)."""
    pr = element.get_or_add_pPr() if element.tag.endswith("}p") else element
    pr.append(_el("w:shd", val="clear", color="auto", fill=fill))


def shade_cell(cell, fill):
    tcPr = cell._tc.get_or_add_tcPr()
    for old in tcPr.findall(qn("w:shd")):
        tcPr.remove(old)
    tcPr.append(_el("w:shd", val="clear", color="auto", fill=fill))


def cell_valign(cell, val="center"):
    cell._tc.get_or_add_tcPr().append(_el("w:vAlign", val=val))


def para_borders(p, **sides):
    """sides: top/bottom/left/right -> (size_eighths_pt, color) or None."""
    pPr = p._p.get_or_add_pPr()
    for old in pPr.findall(qn("w:pBdr")):
        pPr.remove(old)
    bdr = OxmlElement("w:pBdr")
    for side in ("top", "left", "bottom", "right"):
        spec = sides.get(side)
        if not spec:
            continue
        size, color = spec
        bdr.append(_el("w:" + side, val="single", sz=str(size),
                       space="6", color=color))
    pPr.append(bdr)


def cell_borders(cell, **sides):
    tcPr = cell._tc.get_or_add_tcPr()
    for old in tcPr.findall(qn("w:tcBorders")):
        tcPr.remove(old)
    b = OxmlElement("w:tcBorders")
    for side in ("top", "left", "bottom", "right"):
        spec = sides.get(side)
        if spec is None:
            continue
        if spec == "none":
            b.append(_el("w:" + side, val="nil"))
        else:
            size, color = spec
            b.append(_el("w:" + side, val="single", sz=str(size),
                         space="0", color=color))
    tcPr.append(b)


def table_cell_margins(table, top=60, bottom=60, left=110, right=110):
    tblPr = table._tbl.tblPr
    mar = OxmlElement("w:tblCellMar")
    for side, val in (("top", top), ("left", left),
                      ("bottom", bottom), ("right", right)):
        mar.append(_el("w:" + side, w=str(val), type="dxa"))
    tblPr.append(mar)


def fixed_layout(table):
    table.autofit = False
    table._tbl.tblPr.append(_el("w:tblLayout", type="fixed"))


def set_widths(table, widths):
    fixed_layout(table)
    for row in table.rows:
        for i, cell in enumerate(row.cells):
            if i < len(widths):
                cell.width = Inches(widths[i])
    for i, col in enumerate(table.columns):
        if i < len(widths):
            col.width = Inches(widths[i])


def keep_table_together(table, max_rows=10):
    """Short tables move to the next page whole rather than splitting."""
    rows = table.rows
    if len(rows) > max_rows:
        return
    for i, row in enumerate(rows):
        row._tr.get_or_add_trPr().append(_el("w:cantSplit", val="true"))
        if i < len(rows) - 1:
            for cell in row.cells:
                for para in cell.paragraphs:
                    para.paragraph_format.keep_with_next = True


def repeat_header(row):
    row._tr.get_or_add_trPr().append(_el("w:tblHeader", val="true"))


def keep_together(p, with_next=True):
    pPr = p._p.get_or_add_pPr()
    pPr.append(_el("w:keepLines", val="true"))
    if with_next:
        pPr.append(_el("w:keepNext", val="true"))


def add_field(paragraph, instr, placeholder=""):
    """A real Word field (PAGE, NUMPAGES, ...) as begin/instr/end runs."""
    r = paragraph.add_run()
    r._r.append(_el("w:fldChar", fldCharType="begin"))
    it = OxmlElement("w:instrText")
    it.set(qn("xml:space"), "preserve")
    it.text = instr
    r._r.append(it)
    r._r.append(_el("w:fldChar", fldCharType="separate"))
    t = OxmlElement("w:t")
    t.text = placeholder
    r._r.append(t)
    r._r.append(_el("w:fldChar", fldCharType="end"))
    return r


def add_toc_field(paragraph, instr='TOC \\o "1-3" \\h \\z \\u'):
    """A w:fldSimple TOC that Word populates on open / F9."""
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), instr)
    r = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    rPr.append(_el("w:i", val="true"))
    rPr.append(_el("w:color", val="5B6472"))
    r.append(rPr)
    t = OxmlElement("w:t")
    t.text = "The table of contents is empty until the field is updated."
    r.append(t)
    fld.append(r)
    paragraph._p.append(fld)


def update_fields_on_open(doc):
    doc.settings.element.append(_el("w:updateFields", val="true"))


def restart_page_numbering(section, start=1):
    section._sectPr.append(_el("w:pgNumType", start=str(start)))


# --------------------------------------------------------------------------
# Styles
# --------------------------------------------------------------------------

def build_styles(doc):
    st = doc.styles

    normal = st["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = INK
    normal.paragraph_format.space_after = Pt(8)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.line_spacing = 1.14
    rpr = normal.element.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.append(rf)
    for a in ("ascii", "hAnsi", "cs", "eastAsia"):
        rf.set(qn("w:" + a), BODY_FONT)

    def head(name, size, color, space_before, space_after, font=HEAD_FONT,
             bold=True, caps=False):
        s = st[name]
        s.font.name = font
        s.font.size = Pt(size)
        s.font.bold = bold
        s.font.color.rgb = color
        s.font.all_caps = caps
        pf = s.paragraph_format
        pf.space_before = Pt(space_before)
        pf.space_after = Pt(space_after)
        pf.keep_with_next = True
        pf.line_spacing = 1.02
        rpr = s.element.get_or_add_rPr()
        rfx = rpr.find(qn("w:rFonts"))
        if rfx is None:
            rfx = OxmlElement("w:rFonts")
            rpr.append(rfx)
        for a in ("ascii", "hAnsi", "cs"):
            rfx.set(qn("w:" + a), font)
        return s

    h1 = head("Heading 1", 19, VIOLET, 24, 8)
    # a hairline under every H1
    pPr = h1.element.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    bdr.append(_el("w:bottom", val="single", sz="6", space="6",
                   color="CFC6F0"))
    pPr.append(bdr)

    head("Heading 2", 13.5, INK, 18, 6)
    head("Heading 3", 11, SLATE, 14, 4, font=BODY_FONT)

    cap = st["Caption"]
    cap.font.name = BODY_FONT
    cap.font.size = Pt(8.5)
    cap.font.italic = False
    cap.font.bold = False
    cap.font.color.rgb = MUTED
    cap.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap.paragraph_format.space_before = Pt(6)
    cap.paragraph_format.space_after = Pt(18)
    cap.paragraph_format.keep_with_next = False
    cap.paragraph_format.line_spacing = 1.1

    title = st["Title"]
    title.font.name = HEAD_FONT
    title.font.size = Pt(38)
    title.font.bold = False
    title.font.color.rgb = INK

    st["TOC Heading"].font.name = HEAD_FONT
    st["TOC Heading"].font.color.rgb = VIOLET

    for nm in ("List Bullet", "List Paragraph"):
        s = st[nm]
        s.font.name = BODY_FONT
        s.font.size = Pt(10.5)
        s.font.color.rgb = INK


# --------------------------------------------------------------------------
# Inline markdown -> runs
# --------------------------------------------------------------------------

TOKEN_RE = re.compile(
    r"(`[^`]+`)"
    r"|(\*\*[^*]+\*\*)"
    r"|(\[[^\]]+\]\([^)]+\))"
    r"|(\*[^*]+\*)"
)


def style_run(run, *, bold=False, italic=False, mono=False, size=None,
              color=None):
    f = run.font
    f.name = MONO_FONT if mono else BODY_FONT
    if mono:
        rpr = run._r.get_or_add_rPr()
        rf = OxmlElement("w:rFonts")
        for a in ("ascii", "hAnsi", "cs"):
            rf.set(qn("w:" + a), MONO_FONT)
        rpr.append(rf)
    f.bold = bold
    f.italic = italic
    if size is not None:
        f.size = Pt(size)
    if color is not None:
        f.color.rgb = color
    return run


def add_plain(p, text, *, bold=False, italic=False, size=None, color=None):
    """Plain text, but requirement identifiers picked out in the accent hue."""
    pos = 0
    for m in REQ_RE.finditer(text):
        if m.start() > pos:
            style_run(p.add_run(text[pos:m.start()]), bold=bold, italic=italic,
                      size=size, color=color)
        style_run(p.add_run(m.group(0)), bold=bold, italic=italic, size=size,
                  color=VIOLET)
        pos = m.end()
    if pos < len(text):
        style_run(p.add_run(text[pos:]), bold=bold, italic=italic, size=size,
                  color=color)


def add_inline(p, text, *, size=None, base_bold=False, base_italic=False,
               color=None):
    """Render one line of inline markdown into paragraph p."""
    pos = 0
    for m in TOKEN_RE.finditer(text):
        if m.start() > pos:
            add_plain(p, text[pos:m.start()], bold=base_bold,
                      italic=base_italic, size=size, color=color)
        code, strong, link, em = m.groups()
        if code:
            style_run(p.add_run(code[1:-1]), mono=True,
                      size=(size or 10.5) - 1, color=VIOLET_DEEP,
                      bold=base_bold)
        elif strong:
            add_inline(p, strong[2:-2], size=size, base_bold=True,
                       base_italic=base_italic, color=color)
        elif link:
            label = link[1:link.index("]")]
            add_inline(p, label, size=size, base_bold=base_bold,
                       base_italic=base_italic, color=color)
        elif em:
            add_inline(p, em[1:-1], size=size, base_bold=base_bold,
                       base_italic=True, color=color)
        pos = m.end()
    if pos < len(text):
        add_plain(p, text[pos:], bold=base_bold, italic=base_italic, size=size,
                  color=color)


def strip_md(text):
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    return text.strip()


# --------------------------------------------------------------------------
# Markdown block parser
# --------------------------------------------------------------------------

def parse_blocks(md: str):
    lines = md.split("\n")
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
            lang = stripped[3:].strip()
            i += 1
            body = []
            while i < n and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            blocks.append({"kind": "code", "lang": lang,
                           "text": "\n".join(body).rstrip()})
            continue

        if re.fullmatch(r"-{3,}", stripped):
            blocks.append({"kind": "hr"})
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            blocks.append({"kind": "heading", "level": len(m.group(1)),
                           "text": m.group(2).strip()})
            i += 1
            continue

        if stripped.startswith("|"):
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            cells = []
            for r in rows:
                if re.fullmatch(r"\|[\s:|-]+\|", r):
                    continue
                parts = [c.strip() for c in r.strip("|").split("|")]
                cells.append(parts)
            blocks.append({"kind": "table", "rows": cells})
            continue

        if stripped.startswith(">"):
            body = []
            while i < n and lines[i].strip().startswith(">"):
                body.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append({"kind": "quote", "text": " ".join(body).strip()})
            continue

        if re.match(r"^[-*]\s+", stripped):
            items = []
            while i < n:
                cur = lines[i]
                if re.match(r"^[-*]\s+", cur.strip()) and not cur.startswith("  "):
                    items.append(re.sub(r"^[-*]\s+", "", cur.strip()))
                    i += 1
                elif cur.startswith("  ") and cur.strip() and items:
                    items[-1] += " " + cur.strip()
                    i += 1
                else:
                    break
            blocks.append({"kind": "list", "items": items})
            continue

        # A paragraph runs until a blank line or the start of another block.
        # Only a fence (```) breaks it -- a leading inline `code` span does not.
        body = []
        while i < n:
            cur = lines[i].strip()
            if not cur or cur[0] in "|>#" or cur.startswith("```") \
                    or re.match(r"^[-*]\s+", cur) \
                    or re.fullmatch(r"-{3,}", cur):
                break
            body.append(cur)
            i += 1
        if body:
            blocks.append({"kind": "para", "text": " ".join(body)})
        else:
            i += 1
    return blocks


# --------------------------------------------------------------------------
# Figure placement
# --------------------------------------------------------------------------
# Each entry: anchor substring -> list of figures dropped after that block.
# "replace" figures stand in for the block itself (the two ASCII diagrams).

FIG_AFTER = {
    "**Scale.** 219 Python modules": [
        ("ta-10-codebase.png",
         "Where the 110,773 backend lines sit. The pipeline package is 38% of "
         "the backend on its own; the tests are a further 51,165 lines.",
         {"width": 5.75}),
    ],
    "The architecture rests on funnelling five concerns": [
        ("ta-02-chokepoints.png",
         "The whole application narrows to seven modules. Under each is the "
         "requirement that module makes true for every caller, which is why "
         "these are properties rather than conventions."),
    ],
    "Full-text search over companies and vacancies": [
        ("ta-03-schema.png",
         "How the 87 tables divide. Private tables carry a job_seeker_id and "
         "shared ones carry none, so erasing a job seeker leaves the market "
         "data standing; contact is the one table that sits across the line."),
    ],
    "**Raw capture (FR-183, DR-102):**": [
        ("ta-11-egress.png",
         "The path every outbound request takes through egress/client.py, "
         "including the two places it stops early: a cache hit that never "
         "leaves the machine, and a robots.txt disallow that raises."),
    ],
    "Prompts are versioned `.md` templates": [
        ("ta-04-llm.png",
         "The eight steps between a call site and a model reply. Redaction "
         "comes first and the audit row last, so no call can skip either."),
    ],
    "Extraction prefers JSON-LD `JobPosting`": [
        ("ta-05-adapter.png",
         "The four-method contract every source implements, and how the 29 "
         "adapters divide by type."),
    ],
    "Deliberately not a broker": [
        ("ta-06-jobrunner.png",
         "Job states and the transitions between them. A restart returns "
         "running and paused jobs to pending rather than losing them."),
    ],
    "Down from 858 KB unsplit": [
        ("ta-08-bundle.png",
         "The same numbers as a picture: what used to arrive in one payload "
         "against the shell plus 24 chunks fetched on navigation."),
    ],
    "Body text stays near-neutral": [
        ("ta-09-contrast.png",
         "Every measurement against the 4.5:1 line, in both themes. Filled "
         "bars are the hue on the surface, outlined bars the hue on its own "
         "tint; the system hue is the sixth not listed in the table."),
    ],
}

# ASCII blocks replaced outright by their rendered figure.
FIG_REPLACE = [
    ("┌──────", "ta-01-architecture.png",
     "The system in one picture: the SPA talks only to the API, the pipeline "
     "reaches the outside world only through the shared service modules below "
     "it, and SQL is written in exactly one layer.",
     {"width": 5.55}),
    ("directives + composite profile + dream job model", "ta-07-dataflow.png",
     "One campaign end to end. The job seeker decides at two points, marked "
     "dark; the shared knowledge base is written and read at every collection "
     "stage, and the follow-up phase feeds the next campaign's directives."),
]

# Column widths, keyed by the first header cell of the table.
COL_WIDTHS = {
    "Layer": [1.05, 1.75, 3.50],
    "Chokepoint": [1.70, 1.85, 2.75],
    "Concern": [1.85, 4.45],
    "Requirement": [2.30, 4.00],
    "To add": [1.75, 2.65, 1.90],
}


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------

class Builder:
    def __init__(self, doc):
        self.doc = doc
        self.fig_no = 0
        self.used_figures = []

    # -- blank / spacer ----------------------------------------------------
    def spacer(self, pts):
        p = self.doc.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.space_before = Pt(0)
        r = p.add_run()
        r.font.size = Pt(pts)
        return p

    # -- paragraphs --------------------------------------------------------
    def para(self, text, **kw):
        p = self.doc.add_paragraph()
        add_inline(p, text, **kw)
        return p

    def heading(self, text, level):
        h = self.doc.add_heading(level=level)
        # numbers stay as written in the source ("3.1 Persistence — db/")
        add_inline_heading(h, text, level)
        return h

    # -- figures -----------------------------------------------------------
    def figure(self, filename, caption, break_before=False, width=FIG_WIDTH):
        path = FIGDIR / filename
        if not path.exists():
            raise SystemExit(f"missing figure: {path}")
        self.fig_no += 1

        with Image.open(path) as im:
            w_px, h_px = im.size
        height = width * h_px / w_px
        # never let a figure plus its caption overflow the text column
        max_h = 8.4
        if height > max_h:
            width = width * max_h / height
            height = max_h

        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(10)
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.keep_with_next = True
        if break_before:
            p.paragraph_format.page_break_before = True
        run = p.add_run()
        run.add_picture(str(path), width=Inches(width))

        cap = self.doc.add_paragraph(style="Caption")
        style_run(cap.add_run(f"Figure {self.fig_no}. "), bold=True, size=8.5,
                  color=INK)
        add_inline(cap, caption, size=8.5, color=MUTED)

        self.used_figures.append(filename)
        return p

    # -- code --------------------------------------------------------------
    def code(self, text):
        lines = text.split("\n")
        for idx, line in enumerate(lines):
            p = self.doc.add_paragraph()
            pf = p.paragraph_format
            pf.space_before = Pt(9 if idx == 0 else 0)
            pf.space_after = Pt(9 if idx == len(lines) - 1 else 0)
            pf.left_indent = Inches(0.16 + 0.22)
            pf.first_line_indent = Inches(-0.22)   # wrapped lines hang
            pf.right_indent = Inches(0.04)
            pf.line_spacing = 1.0
            pf.keep_with_next = idx < len(lines) - 1
            shade(p._p, SH_CODE)
            para_borders(p, left=(18, RULE_ACCENT))
            style_run(p.add_run(line if line.strip() else " "), mono=True,
                      size=8.5, color=SLATE)
        return p

    # -- callout -----------------------------------------------------------
    def callout(self, text):
        t = self.doc.add_table(rows=1, cols=1)
        t.style = "Table Grid"
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        set_widths(t, [TEXT_WIDTH])
        table_cell_margins(t, top=140, bottom=140, left=180, right=180)
        cell = t.cell(0, 0)
        shade_cell(cell, SH_CALLOUT)
        cell_borders(cell, left=(24, RULE_ACCENT), top="none", bottom="none",
                     right="none")
        cell.paragraphs[0].text = ""
        p = cell.paragraphs[0]
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.line_spacing = 1.16
        add_inline(p, text, size=10.5, color=SLATE)
        self.spacer(6)
        return t

    # -- lists -------------------------------------------------------------
    def bullets(self, items):
        for item in items:
            p = self.doc.add_paragraph(style="List Bullet")
            pf = p.paragraph_format
            pf.space_after = Pt(5)
            pf.left_indent = Inches(0.28)
            pf.line_spacing = 1.14
            add_inline(p, item)

    # -- tables ------------------------------------------------------------
    def table(self, rows, widths=None, header=True, key_value=False,
              size=9.5):
        if not rows:
            return None
        ncols = max(len(r) for r in rows)
        rows = [r + [""] * (ncols - len(r)) for r in rows]
        t = self.doc.add_table(rows=len(rows), cols=ncols)
        t.style = "Table Grid"
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        widths = widths or [TEXT_WIDTH / ncols] * ncols
        scale = TEXT_WIDTH / sum(widths)
        widths = [w * scale for w in widths]
        set_widths(t, widths)
        table_cell_margins(t)

        for ri, row in enumerate(rows):
            is_head = header and ri == 0
            if is_head:
                repeat_header(t.rows[ri])
            for ci, text in enumerate(row):
                cell = t.cell(ri, ci)
                cell.text = ""
                p = cell.paragraphs[0]
                p.paragraph_format.space_before = Pt(1)
                p.paragraph_format.space_after = Pt(1)
                p.paragraph_format.line_spacing = 1.08
                if is_head:
                    shade_cell(cell, SH_HEADER)
                    add_inline(p, text, size=size,
                               color=RGBColor(0xFF, 0xFF, 0xFF),
                               base_bold=True)
                    cell_borders(cell, top=(8, SH_HEADER),
                                 bottom=(8, SH_HEADER), left="none",
                                 right="none")
                else:
                    if key_value and ci == 0:
                        shade_cell(cell, SH_KEYCOL)
                        add_inline(p, text, size=size, base_bold=True,
                                   color=VIOLET_DEEP)
                    else:
                        add_inline(p, text, size=size)
                    cell_borders(cell, top="none", left="none", right="none",
                                 bottom=(4, RULE_LIGHT))
                cell_valign(cell, "center" if (is_head or key_value)
                            else "top")
        keep_table_together(t)
        self.spacer(8)
        return t


def add_inline_heading(h, text, level):
    """Heading text with the leading number set in the accent hue."""
    m = re.match(r"^((?:\d+\.)+\d*|\d+\.)\s+(.*)$", text)
    if m and level == 1:
        style_run(h.add_run(m.group(1) + "  "), bold=True, size=19,
                  color=RGBColor(0xA9, 0x9C, 0xE4))
        h.add_run(strip_md(m.group(2)))
    elif m:
        style_run(h.add_run(m.group(1) + "  "), bold=True,
                  color=RGBColor(0x9A, 0x8F, 0xD0))
        h.add_run(strip_md(m.group(2)))
    else:
        h.add_run(strip_md(text))
    for r in h.runs:
        r.font.name = HEAD_FONT if level <= 2 else BODY_FONT


# --------------------------------------------------------------------------
# Derived tables from the two measured code blocks
# --------------------------------------------------------------------------

def parse_bundle_block(text):
    rows = [["Chunk", "Raw", "Over the wire", "What it contains"]]
    pat = re.compile(r"^(initial|per page)\s+(\S+ KB)\s+\((.+?)\)\s+(.+)$")
    for line in text.split("\n"):
        m = pat.match(line.strip())
        if not m:
            return None
        label, raw, gz, what = m.groups()
        rows.append([label, raw, gz.replace(" gzip", ""), what])
    return rows if len(rows) > 1 else None


def parse_contrast_block(text):
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines or "LIGHT" not in lines[0]:
        return None
    rows = [["Phase", "Light · on white", "Light · on tint",
             "Dark · on surface", "Dark · on tint"]]
    pat = re.compile(
        r"^(.+?)\s{2,}([\d.]+)\s+([\d.]+)\s+.+?\s{2,}([\d.]+)\s+([\d.]+)$")
    for line in lines[1:]:
        m = pat.match(line.strip())
        if not m:
            return None
        rows.append([m.group(1).strip(), m.group(2), m.group(3),
                     m.group(4), m.group(5)])
    return rows if len(rows) > 1 else None


# --------------------------------------------------------------------------
# Header / footer
# --------------------------------------------------------------------------

def build_header(section, title, version):
    hdr = section.header
    hdr.is_linked_to_previous = False
    p = hdr.paragraphs[0]
    p.text = ""
    pf = p.paragraph_format
    pf.space_after = Pt(6)
    pf.tab_stops.add_tab_stop(Inches(TEXT_WIDTH), WD_TAB_ALIGNMENT.RIGHT)
    style_run(p.add_run(title), size=8.5, color=MUTED)
    p.add_run("\t")
    style_run(p.add_run(version), size=8.5, color=FAINT)
    para_borders(p, bottom=(4, RULE_LIGHT))


def build_footer(section, left_text):
    ftr = section.footer
    ftr.is_linked_to_previous = False
    p = ftr.paragraphs[0]
    p.text = ""
    pf = p.paragraph_format
    pf.space_before = Pt(6)
    pf.tab_stops.add_tab_stop(Inches(TEXT_WIDTH), WD_TAB_ALIGNMENT.RIGHT)
    para_borders(p, top=(4, RULE_LIGHT))
    style_run(p.add_run(left_text), size=8.5, color=FAINT)
    p.add_run("\t")
    style_run(p.add_run("Page "), size=8.5, color=MUTED)
    style_run(add_field(p, "PAGE", "1"), size=8.5, color=MUTED)
    style_run(p.add_run(" of "), size=8.5, color=MUTED)
    style_run(add_field(p, "NUMPAGES", "1"), size=8.5, color=MUTED)


def blank_header_footer(section):
    for part in (section.header, section.footer):
        part.is_linked_to_previous = False
        part.paragraphs[0].text = ""


# --------------------------------------------------------------------------
# Title page
# --------------------------------------------------------------------------

def colour_rule(doc, width_in=3.4, height_pt=6):
    """The five phase hues butted together into one flat stripe.

    A one-row table rather than a paragraph border, because each segment
    needs its own fill. Borders are painted in each segment's own colour so
    no hairline shows between them.
    """
    t = doc.add_table(rows=1, cols=len(PHASES))
    t.style = "Table Grid"
    fixed_layout(t)
    table_cell_margins(t, top=0, bottom=0, left=0, right=0)
    for cell, colour in zip(t.rows[0].cells, PHASES):
        shade_cell(cell, colour)
        cell_borders(cell, top=(2, colour), left=(2, colour),
                     right=(2, colour), bottom=(2, colour))
        cp = cell.paragraphs[0]
        cp.paragraph_format.space_after = Pt(0)
        cp.paragraph_format.line_spacing = Pt(height_pt)
        cp.add_run().font.size = Pt(2)
    set_widths(t, [width_in / len(PHASES)] * len(PHASES))
    return t


def build_title_page(b, meta, doc_title):
    """Lockup, document name, phase stripe, then the control fields.

    Shared with the Functional Design and the SRS: same lockup width, same
    stripe, same metadata block. The lockup already reads "Dream Job", so the
    title below it is the document's own name rather than a second wordmark,
    and the product name returns only in the tagline.
    """
    doc = b.doc
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(72)

    if LOCKUP.exists():
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(58)
        p.add_run().add_picture(str(LOCKUP), width=Inches(3.1))

    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(10)
    p.paragraph_format.line_spacing = 1.0
    r = style_run(p.add_run("Technical Architecture"), size=34, color=INK,
                  bold=True)
    r.font.name = BODY_FONT

    colour_rule(doc, width_in=3.4, height_pt=6)

    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(16)
    p.paragraph_format.space_after = Pt(200)
    style_run(p.add_run("%s · %s" % (PRODUCT, TAGLINE)), size=12, color=SLATE)

    status = strip_md(meta.get("Status", ""))
    fields = [
        ("Version", strip_md(meta.get("Version", ""))),
        ("Date", strip_md(meta.get("Date", ""))),
        ("Author", AUTHOR),
        ("Status", status),
    ]
    fields = [(k, v) for k, v in fields if v]

    t = doc.add_table(rows=len(fields), cols=2)
    fixed_layout(t)
    table_cell_margins(t, top=30, bottom=30, left=0, right=90)
    for ri, (k, v) in enumerate(fields):
        for ci, text in enumerate((k, v)):
            cell = t.cell(ri, ci)
            cell.text = ""
            cp = cell.paragraphs[0]
            cp.paragraph_format.space_after = Pt(2)
            if ci == 0:
                r = style_run(cp.add_run(text.upper()), size=8,
                              color=COVER_LABEL, bold=True)
                r.font.name = BODY_FONT
            else:
                add_inline(cp, text, size=10, color=INK)
            cell_borders(cell, top="none", bottom="none", left="none",
                         right="none")
    set_widths(t, [1.15, 5.12])


# --------------------------------------------------------------------------
# Front matter
# --------------------------------------------------------------------------

def build_front_matter(b, control_rows, doc_title):
    doc = b.doc
    b.heading("Document control", 1)
    b.table(control_rows, widths=[1.35, 4.95], header=False, key_value=True,
            size=10)

    b.heading("Contents", 1)
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(4)
    add_toc_field(p)
    note = doc.add_paragraph()
    note.paragraph_format.space_before = Pt(4)
    style_run(note.add_run(
        "This table of contents is a live field. If it shows the line above "
        "rather than a list of sections, click in it and press F9 "
        "(or right-click and choose Update field) to populate it."),
        size=9, italic=True, color=MUTED)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    if not SOURCE.exists():
        raise SystemExit(f"source not found: {SOURCE}")
    blocks = parse_blocks(SOURCE.read_text(encoding="utf-8"))

    # Title + document-control table come out of the front of the markdown.
    doc_title = "Dream Job — Technical Architecture"
    control_rows = []
    body_start = 0
    for idx, blk in enumerate(blocks):
        if blk["kind"] == "heading" and blk["level"] == 1:
            doc_title = strip_md(blk["text"])
            continue
        if blk["kind"] == "table" and not control_rows:
            control_rows = [r for r in blk["rows"] if any(c for c in r)]
            body_start = idx + 1
            break
    meta = {r[0]: r[1] for r in control_rows if len(r) > 1}
    control_rows = list(control_rows)
    control_rows.insert(2, ["Author", AUTHOR])
    control_rows = [[c for c in r] for r in control_rows]

    doc = Document()
    build_styles(doc)
    update_fields_on_open(doc)

    sec = doc.sections[0]
    sec.page_width = Inches(8.27)
    sec.page_height = Inches(11.69)
    for attr, val in (("left_margin", 1.0), ("right_margin", 0.97),
                      ("top_margin", 0.95), ("bottom_margin", 0.95),
                      ("header_distance", 0.5), ("footer_distance", 0.5)):
        setattr(sec, attr, Inches(val))
    blank_header_footer(sec)

    b = Builder(doc)
    build_title_page(b, meta, doc_title)

    # --- new section: everything else, with header, footer and numbering ---
    sec2 = doc.add_section(WD_SECTION.NEW_PAGE)
    sec2.page_width = Inches(8.27)
    sec2.page_height = Inches(11.69)
    for attr, val in (("left_margin", 1.0), ("right_margin", 0.97),
                      ("top_margin", 0.95), ("bottom_margin", 0.95),
                      ("header_distance", 0.5), ("footer_distance", 0.5)):
        setattr(sec2, attr, Inches(val))
    build_header(sec2, doc_title, "Version " + meta.get("Version", ""))
    build_footer(sec2, f"{AUTHOR} · {meta.get('Date', '')}")

    build_front_matter(b, control_rows, doc_title)

    # --- body -------------------------------------------------------------
    first_section_seen = False
    pending_breaks = set()

    for blk in blocks[body_start:]:
        kind = blk["kind"]

        if kind == "hr":
            continue

        if kind == "heading":
            level = 1 if blk["level"] == 2 else 2 if blk["level"] == 3 else 3
            h = b.heading(blk["text"], level)
            if level == 1:
                if not first_section_seen:
                    h.paragraph_format.page_break_before = True
                    first_section_seen = True
                elif blk["text"].startswith("3."):
                    # the chokepoints argument gets a page of its own
                    h.paragraph_format.page_break_before = True
            continue

        if kind == "para":
            text = blk["text"]
            if text.startswith("*Companion documents"):
                p = b.doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.space_before = Pt(26)
                para_borders(p, top=(4, RULE_LIGHT))
                add_inline(p, text, size=9, color=MUTED)
                continue
            b.para(text)
            for anchor, figs in FIG_AFTER.items():
                if anchor in text:
                    for fig in figs:
                        name, cap = fig[0], fig[1]
                        opts = fig[2] if len(fig) > 2 else {}
                        b.figure(name, cap, **opts)
                    pending_breaks.add(anchor)
            continue

        if kind == "quote":
            b.callout(blk["text"])
            continue

        if kind == "list":
            b.bullets(blk["items"])
            joined = " ".join(blk["items"])
            for anchor, figs in FIG_AFTER.items():
                if anchor in joined and anchor not in pending_breaks:
                    for fig in figs:
                        name, cap = fig[0], fig[1]
                        opts = fig[2] if len(fig) > 2 else {}
                        b.figure(name, cap, **opts)
                    pending_breaks.add(anchor)
            continue

        if kind == "table":
            rows = blk["rows"]
            widths = COL_WIDTHS.get(strip_md(rows[0][0]) if rows else "")
            b.table(rows, widths=widths)
            continue

        if kind == "code":
            text = blk["text"]

            replaced = False
            for entry in FIG_REPLACE:
                marker, fig, cap = entry[0], entry[1], entry[2]
                opts = entry[3] if len(entry) > 3 else {}
                if marker in text:
                    b.figure(fig, cap, **opts)
                    replaced = True
                    break
            if replaced:
                continue

            bundle = parse_bundle_block(text)
            if bundle:
                b.table(bundle, widths=[1.05, 1.0, 1.25, 3.0])
                continue

            contrast = parse_contrast_block(text)
            if contrast:
                b.table(contrast, widths=[1.5, 1.25, 1.15, 1.3, 1.1])
                continue

            b.code(text)
            continue

    doc.save(OUTPUT)
    return b


def verify(builder):
    """Re-open the saved file and confirm what actually landed in it."""
    import zipfile
    from docx import Document as D

    d = D(str(OUTPUT))
    paras = len(d.paragraphs)
    tables = len(d.tables)
    shapes = len(d.inline_shapes)
    headings = sum(1 for p in d.paragraphs if p.style.name.startswith("Heading"))
    captions = sum(1 for p in d.paragraphs if p.style.name == "Caption")

    # Figures live in FIGDIR; the lockup is the chosen mark in docs/, so the
    # check resolves each by its own path rather than assuming one folder.
    referenced = [FIGDIR / n for n in builder.used_figures] + [LOCKUP]
    with zipfile.ZipFile(OUTPUT) as z:
        names = z.namelist()
        media = [n for n in names if n.startswith("word/media/")]
        embedded_bytes = {z.read(n) for n in media}
        document_xml = z.read("word/document.xml").decode("utf-8")
        footer_xml = "".join(
            z.read(n).decode("utf-8") for n in names
            if re.match(r"word/footer\d*\.xml", n))
        header_xml = "".join(
            z.read(n).decode("utf-8") for n in names
            if re.match(r"word/header\d*\.xml", n))

    missing = [p.name for p in referenced if p.read_bytes() not in embedded_bytes]

    checks = {
        "TOC field present": 'TOC \\o &quot;1-3&quot;' in document_xml
                             or 'TOC \\o "1-3"' in document_xml,
        "PAGE field in footer": "PAGE" in footer_xml,
        "NUMPAGES field in footer": "NUMPAGES" in footer_xml,
        "title in header": "Technical Architecture" in header_xml,
        "every figure embedded": not missing,
        "one caption per figure": captions == len(builder.used_figures),
        "heading styles used": headings >= 15,
    }

    print(f"output      : {OUTPUT}")
    print(f"size        : {OUTPUT.stat().st_size / 1024:.1f} KB")
    print(f"paragraphs  : {paras}")
    print(f"tables      : {tables}")
    print(f"inline shapes: {shapes}   (media parts: {len(media)})")
    print(f"headings    : {headings}")
    print(f"captions    : {captions}")
    print(f"figures     : {len(builder.used_figures)} referenced, "
          f"{len(referenced) - len(missing)}/{len(referenced)} embedded "
          f"(incl. logo)")
    for name in builder.used_figures:
        print(f"   embedded  {name}")
    if missing:
        print(f"   MISSING   {missing}")
    for label, ok in checks.items():
        print(f"[{'ok' if ok else 'FAIL'}] {label}")
    return all(checks.values())


if __name__ == "__main__":
    builder = main()
    ok = verify(builder)
    sys.exit(0 if ok else 1)
