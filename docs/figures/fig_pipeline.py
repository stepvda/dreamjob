"""Figures about how a campaign runs.

Four figures:

* ``fdd-05-planning``   - campaign planning, and the knowledge-base reuse branch
* ``ta-07-dataflow``    - the end-to-end campaign data flow
* ``ta-05-adapter``     - the four-step adapter contract, and the 24 adapters
* ``fdd-06-financial``  - filings to two scores, with the FR-245 fallback

Every number drawn here comes from :mod:`facts`.  The one deliberately
unnumbered element is the five-year sparkline pair in ``fdd-06-financial``: it
shows the *shape* the analysis reads, carries no scale, and says so on its face.

Each figure is measured before it is written.  ``Canvas.check`` walks every
text artist that was placed inside a box, converts its rendered extent back
into data coordinates and reports anything that has escaped - so a diagram
cannot ship with a label spilling over its own border.
"""

from __future__ import annotations

import facts
import style as st

TITLE_PT = 9.0
BODY_PT = 8.5
SPACING = 1.42
PAD = 1.5           # x units of breathing room inside a box


# ---------------------------------------------------------------------------
# A canvas that knows its own aspect, and checks its own text
# ---------------------------------------------------------------------------


class Canvas:
    """A 0-100 coordinate space over a figure of known size in inches."""

    def __init__(self, height: float, width: float = st.WIDTH):
        self.fig, self.ax = st.figure(width, height)
        self.fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
        self.h = height
        self.w = width
        self._watch: list[tuple[object, tuple[float, float, float, float]]] = []

    # -- unit conversion ---------------------------------------------------
    def yu(self, points: float) -> float:
        return points / 72.0 / self.h * 100.0

    # -- text --------------------------------------------------------------
    def text(self, x, y, s, *, rect=None, size=BODY_PT, colour=st.INK_2,
             weight="normal", ha="center", va="center", style="normal",
             family=None, zorder=5):
        kw = {"family": family} if family else {}
        artist = self.ax.text(x, y, s, ha=ha, va=va, fontsize=size,
                              color=colour, weight=weight, style=style,
                              zorder=zorder, linespacing=1.42, **kw)
        self._watch.append((artist, rect or (0.0, 0.0, 100.0, 100.0)))
        return artist

    # -- primitives --------------------------------------------------------
    def box(self, x, y, w, h, *, colour=st.ACCENT, fill=None, lw=1.1,
            radius=1.6, top_rule=False):
        st.box(self.ax, x, y, w, h, "", colour=colour, fill=fill, lw=lw,
               radius=radius, top_rule=top_rule)
        return (x + PAD, y + 0.5, x + w - PAD, y + h - 0.5)

    def stack(self, cx, cy, title, lines=(), *, rect=None,
              title_size=TITLE_PT, size=BODY_PT, title_colour=st.INK,
              line_colour=st.INK_2, weight="600"):
        """A centred title with quieter detail lines beneath it."""
        th = self.yu(title_size * SPACING)
        lh = self.yu(size * SPACING)
        total = (th if title else 0.0) + lh * len(lines)
        y = cy + total / 2.0
        if title:
            self.text(cx, y - th / 2, title, rect=rect, size=title_size,
                      colour=title_colour, weight=weight)
            y -= th
        for line in lines:
            self.text(cx, y - lh / 2, line, rect=rect, size=size,
                      colour=line_colour)
            y -= lh

    def card(self, x, y, w, h, title, lines=(), *, colour=st.ACCENT,
             fill=None, top_rule=True, line_colour=st.INK_2,
             title_colour=st.INK, title_size=TITLE_PT, size=BODY_PT, lw=1.1):
        rect = self.box(x, y, w, h, colour=colour, fill=fill, lw=lw,
                        top_rule=top_rule)
        self.stack(x + w / 2, y + h / 2, title, lines, rect=rect,
                   title_size=title_size, size=size,
                   title_colour=title_colour, line_colour=line_colour)

    def gate(self, x, y, w, h, lines):
        """A point where the job seeker decides (NFR-305): ink, not a hue."""
        rect = self.box(x, y, w, h, colour=st.INK, fill=st.INK, lw=1.0,
                        radius=1.1)
        self.stack(x + w / 2, y + h / 2, None, lines, rect=rect,
                   line_colour="#ffffff")

    def path(self, points, *, colour=st.INK_3, lw=1.0, dashed=False,
             head=True):
        """A right-angled connector through a list of points."""
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        self.ax.plot(xs[:-1] if head else xs, ys[:-1] if head else ys,
                     color=colour, lw=lw, zorder=1,
                     linestyle="--" if dashed else "-",
                     solid_capstyle="butt")
        if head:
            st.arrow(self.ax, xs[-2], ys[-2], xs[-1], ys[-1], colour=colour,
                     lw=lw, dashed=dashed)

    # -- measurement -------------------------------------------------------
    def check(self, name: str) -> None:
        self.fig.canvas.draw()
        renderer = self.fig.canvas.get_renderer()
        inv = self.ax.transData.inverted()
        problems = []
        for artist, rect in self._watch:
            bb = artist.get_window_extent(renderer=renderer)
            (x0, y0), (x1, y1) = inv.transform([[bb.x0, bb.y0],
                                                [bb.x1, bb.y1]])
            rx0, ry0, rx1, ry1 = rect
            if x0 < rx0 - 0.3 or x1 > rx1 + 0.3 or y0 < ry0 - 0.3 or y1 > ry1 + 0.3:
                problems.append(
                    f"    {artist.get_text()[:44]!r}  "
                    f"x {x0:5.1f}..{x1:5.1f}  y {y0:5.1f}..{y1:5.1f}  "
                    f"outside {tuple(round(v, 1) for v in rect)}"
                )
        if problems:
            print(f"  {name}: {len(problems)} label(s) overflow")
            print("\n".join(problems))

    def save(self, name: str):
        self.check(name)
        return st.save(self.fig, name)


# ---------------------------------------------------------------------------
# fdd-05-planning
# ---------------------------------------------------------------------------


def planning() -> None:
    c = Canvas(4.4)
    blue, violet, slate = st.PHASE[2], st.PHASE[1], st.PHASE[0]

    # --- what the plan is built from --------------------------------------
    c.card(2, 87, 29, 13, "Composite profile", ("FR-121",),
           colour=violet, fill=st.PHASE_SOFT[1], line_colour=st.INK_3)
    c.card(34.5, 87, 29, 13, "Dream job model", ("FR-128",),
           colour=violet, fill=st.PHASE_SOFT[1], line_colour=st.INK_3)
    c.card(67, 87, 29, 13, "Directives", ("structured, not prose",),
           colour=blue, fill=st.PHASE_SOFT[2], line_colour=st.INK_3)

    for cx in (16.5, 49, 81.5):
        c.path([(cx, 87), (cx, 84)], head=False)
    c.ax.plot([16.5, 81.5], [84, 84], color=st.INK_3, lw=1.0, zorder=1)
    for cx in (24, 74):
        c.path([(cx, 84), (cx, 80)])

    # --- the two planning steps -------------------------------------------
    c.card(2, 64, 45, 16, "Source selection",
           ("the source catalogue's coverage decides",
            "which sources can serve this search",
            "(FR-161, FR-164)"),
           colour=blue)
    c.card(51, 64, 45, 16, "A native query per source",
           ("an LLM writes each source's own query",
            "form, grounded in the profile, the dream",
            "job model and the directives (FR-162)"),
           colour=blue)
    c.path([(47, 72), (51, 72)])

    # --- the knowledge-base branch ----------------------------------------
    c.path([(16, 64), (16, 57)], colour=slate)
    c.text(56, 60.5,
           "before scheduling anything, the plan asks what is already known "
           "(FR-342)",
           size=BODY_PT, colour=slate)

    c.card(2, 37, 28, 20, "Knowledge base",
           ("shared, no job seeker", "on any row (FR-341)"),
           colour=slate, fill=st.PHASE_SOFT[0])
    c.card(35, 48, 30, 9, "Fresh enough — reused",
           ("not collected again",), colour=st.OK, line_colour=st.INK_3)
    c.card(35, 37, 30, 9, "Stale or missing",
           ("scheduled for collection",), colour=blue, line_colour=st.INK_3)

    c.path([(30, 52.5), (35, 52.5)])
    c.path([(30, 41.5), (35, 41.5)])
    c.stack(33, 32.5, None,
            ("freshness per record type (FR-343)",
             f"vacancies {facts.STALENESS_DAYS['vacancy']} d  ·  "
             f"companies {facts.STALENESS_DAYS['company']} d  ·  "
             f"filings {facts.STALENESS_DAYS['financial_year']} d"),
            rect=(0, 28, 66, 37), line_colour=st.INK_3)

    # both outcomes are reported as a saving in the plan itself
    c.path([(65, 52.5), (70, 52.5), (70, 47)], head=False, colour=slate)
    c.path([(65, 41.5), (70, 41.5), (70, 47)], head=False, colour=slate)
    c.path([(70, 47), (70, 27)], colour=slate)
    c.text(82, 44, "reported before launch", size=BODY_PT, colour=slate)

    c.path([(93, 64), (93, 27)])

    # --- the plan, and the only way past it --------------------------------
    rect = c.box(2, 2, 94, 25, colour=blue, top_rule=True)
    c.stack(49, 21, "The plan, before anything runs (FR-163)",
            ("sources · queries · expected volume · estimated duration · "
             "estimated cost",
             "and the knowledge-base saving: records reused, time and cost "
             "avoided (FR-342)"),
            rect=rect)
    c.gate(21, 4.5, 56, 9, ("You exclude what you do not want, then launch",))

    c.save("fdd-05-planning")


# ---------------------------------------------------------------------------
# ta-07-dataflow
# ---------------------------------------------------------------------------


def dataflow() -> None:
    c = Canvas(4.4)
    ax = c.ax

    columns = [
        (2, "Plan", [
            ("Directives", ()),
            ("Campaign plan", ()),
            ("GATE", ("You approve", "the plan")),
            ("Collection", (f"{sum(facts.ADAPTERS.values())} adapters",)),
        ]),
        (3, "Discover", [
            ("Company profiles", ()),
            ("Financial analysis", ()),
            ("Opportunities", ("+ speculative ones",)),
            ("Ranking", (f"{len(facts.SUB_SCORES)} sub-scores",)),
        ]),
        (4, "Apply", [
            ("Hiring contacts", ()),
            ("Documents", ("generated, checked",)),
            ("GATE", ("You approve", "the application")),
            ("Dispatch", ()),
        ]),
        (5, "Follow up", [
            ("Replies", ("detected or entered",)),
            ("Pipeline board", ()),
            ("What works", (f"{facts.SEGMENT_DIMENSIONS} segments, with n",)),
            ("Redirection advice", ()),
        ]),
    ]

    X = [1.8, 26.8, 51.8, 76.8]
    CW = 21.5
    ROWS = [75.5, 62.0, 48.5, 35.0]      # row bottoms
    RH = 10.5

    for (phase, name, rows), x in zip(columns, X, strict=True):
        colour = st.PHASE[phase]
        c.text(x + CW / 2, 92.0, name, size=9.6, colour=colour, weight="600")
        ax.plot([x + CW / 2 - 4, x + CW / 2 + 4], [89.2, 89.2], color=colour,
                lw=2.4, solid_capstyle="round", zorder=3)

        for i, (title, lines) in enumerate(rows):
            y = ROWS[i]
            if title == "GATE":
                c.gate(x + 0.6, y + 0.5, CW - 1.2, RH - 1.0, lines)
            else:
                c.card(x + 0.6, y, CW - 1.2, RH, title, lines, colour=colour,
                       fill=st.SURFACE, top_rule=False, line_colour=st.INK_3,
                       title_size=BODY_PT)
            if i < len(rows) - 1:
                c.path([(x + CW / 2, y), (x + CW / 2, ROWS[i + 1] + RH)],
                       lw=0.9)

    for x in X[:-1]:
        st.arrow(ax, x + CW, 67.3, x + CW + 3.5, 67.3, colour=st.INK_2, lw=1.3)

    # --- the store the middle of the flow reads and writes -----------------
    rect = c.box(1.8, 12, 96.5, 15, colour=st.PHASE[0],
                 fill=st.PHASE_SOFT[0], top_rule=True)
    c.stack(50, 19.5, "Shared knowledge base",
            ("companies · vacancies · financial years · hiring signals · "
             "competitors · contacts",
             "no row carries a job seeker (FR-341); everything else here is "
             "private to one"),
            rect=rect, line_colour=st.INK_3)

    for x in X[:-1]:
        st.arrow(ax, x + CW / 2, 35.0, x + CW / 2, 27.0, colour=st.PHASE[0],
                 style="<|-|>", lw=1.0)
    c.text(87.5, 31, "replies stay private", size=BODY_PT, colour=st.INK_3)

    # --- what the follow-up phase sends back -------------------------------
    loop = st.INK_3
    c.path([(98.3, 40.2), (99.3, 40.2), (99.3, 99), (0.7, 99),
            (0.7, 80.7), (1.8, 80.7)], colour=loop)
    c.text(50, 95.6,
           "redirection advice becomes the next campaign's directives "
           "(FR-285)", size=BODY_PT, colour=loop)

    c.box(1.8, 3.2, 4.0, 3.8, colour=st.INK, fill=st.INK, radius=0.8)
    c.text(7.6, 5.1,
           "the two points where the job seeker decides — every score is "
           "advisory (NFR-305)",
           size=BODY_PT, colour=st.INK_2, ha="left")

    c.save("ta-07-dataflow")


# ---------------------------------------------------------------------------
# ta-05-adapter
# ---------------------------------------------------------------------------


def adapter() -> None:
    c = Canvas(3.6)
    blue = st.PHASE[2]

    c.text(0, 96.5, "One contract", size=9.6, colour=st.INK, weight="600",
           ha="left")
    c.text(64, 96.5, f"{sum(facts.ADAPTERS.values())} adapters, by type",
           size=9.6, colour=st.INK, weight="600", ha="left")

    steps = [
        ("plan", "(directives, composite, caps) -> PlanItem",
         ("the directives and the composite profile become",
          "this source's own query form, priced and paced")),
        ("fetch", "(item) -> RawRecord",
         ("every request through EgressClient: robots.txt,",
          "rate limit, cache, and raw capture (FR-183)")),
        ("parse", "(raw) -> fields",
         ("JSON-LD JobPosting first, then deterministic",
          "selectors, then LLM extraction — in that order")),
        ("normalise", "(parsed, raw) -> NormalisedRecord",
         ("knowledge-base columns, carrying a confidence",
          "and a provenance record (NFR-402)")),
    ]

    top, h, gap = 91.0, 18.0, 4.0
    for i, (name, signature, detail) in enumerate(steps):
        y = top - (i + 1) * h - i * gap
        rect = c.box(0, y, 60, h, colour=blue, lw=1.0)
        c.ax.plot([0.7, 0.7], [y + 1.5, y + h - 1.5], color=blue, lw=2.6,
                  solid_capstyle="round", zorder=3)
        lh = c.yu(BODY_PT * SPACING)
        head = y + h - lh * 0.9
        c.text(2.6, head, f"{i + 1}   {name}", rect=rect, size=9.2,
               colour=st.INK, weight="600", ha="left")
        c.text(58.0, head, signature, rect=rect, size=BODY_PT, colour=blue,
               ha="right", family="monospace")
        for j, line in enumerate(detail):
            c.text(2.6, head - lh * (1.15 + j), line, rect=rect,
                   size=BODY_PT, colour=st.INK_2, ha="left")
        if i < len(steps) - 1:
            c.path([(30, y), (30, y - gap)], lw=0.9)

    c.text(0, 3.0,
           "NFR-601: a new source is one subclass and one decorator.",
           size=BODY_PT, colour=st.INK_3, ha="left", style="italic")

    # --- the population the contract holds ---------------------------------
    items = sorted(facts.ADAPTERS.items(), key=lambda kv: -kv[1])
    total = sum(v for _, v in items)
    unit = 2.0                       # x units per adapter
    bar_x = 82.5
    y = 84.0
    for name, value in items:
        c.text(81.0, y + 2.5, name, size=BODY_PT, colour=st.INK_2, ha="right",
               rect=(64, 0, 81.3, 100))
        c.ax.add_patch(
            st.mpatches.Rectangle((bar_x, y), value * unit, 5.0,
                                  facecolor=blue, edgecolor="none", zorder=3)
        )
        c.text(bar_x + value * unit + 1.0, y + 2.5, str(value), size=BODY_PT,
               colour=st.INK, ha="left", weight="600")
        y -= 10.0

    c.text(64, 30,
           f"{facts.ADAPTERS_REQUIRING_ACK} of them ship disabled until\n"
           "an administrator acknowledges\ntheir terms (IR-101).",
           size=BODY_PT, colour=st.INK_3, ha="left", va="top",
           rect=(64, 0, 100, 100))

    c.save("ta-05-adapter")


# ---------------------------------------------------------------------------
# fdd-06-financial
# ---------------------------------------------------------------------------


def _sparkline(ax, x, y, w, h, series, colour):
    lo, hi = min(series), max(series)
    span = (hi - lo) or 1.0
    n = len(series)
    xs = [x + w * i / (n - 1) for i in range(n)]
    ys = [y + h * 0.1 + (v - lo) / span * h * 0.8 for v in series]
    ax.plot(xs, ys, color=colour, lw=1.5, solid_capstyle="round", zorder=4)
    ax.plot(xs, ys, marker="o", markersize=2.4, linestyle="none",
            color=colour, zorder=5)


def financial() -> None:
    c = Canvas(4.8)
    teal = st.PHASE[3]

    # --- what is read, and what is computed from it ------------------------
    c.card(3, 74, 24, 25, f"Filings, {facts.FINANCIAL_YEARS} years",
           ("NBB · KBO/BCE · KvK",
            "Companies House",
            "SEC EDGAR",
            "XBRL, CSV, PDF"),
           colour=teal, fill=st.PHASE_SOFT[3], line_colour=st.INK_2)
    c.card(32, 74, 28, 25, "Line items, per year",
           ("revenue · gross margin",
            "EBIT · EBITDA · equity",
            "net result · cash",
            "total debt · headcount",
            "staff costs · capex"),
           colour=teal)
    c.card(65, 74, 34, 25, "Ratios and trends",
           ("revenue and headcount CAGR",
            "margin trend · cost per FTE",
            "current and solvency ratios",
            "trajectory, with a confidence"),
           colour=teal)
    c.path([(27, 86.5), (32, 86.5)])
    c.path([(60, 86.5), (65, 86.5)])

    # --- what the five years look like ------------------------------------
    rect = c.box(3, 50, 37, 20, colour=st.LINE, fill=st.SURFACE_2, lw=1.0)
    c.text(21.5, 67, "the shape behind every ratio", size=BODY_PT,
           colour=st.INK_2, weight="600", rect=rect)
    _sparkline(c.ax, 19, 59, 18, 4.6, [1.00, 1.14, 1.05, 1.32, 1.51], teal)
    _sparkline(c.ax, 19, 53.6, 18, 4.6, [1.00, 1.10, 1.12, 1.26, 1.40],
               st.PHASE[0])
    c.text(17.5, 61.3, "revenue", size=BODY_PT, colour=teal, ha="right",
           rect=rect)
    c.text(17.5, 55.9, "headcount", size=BODY_PT, colour=st.PHASE[0],
           ha="right", rect=rect)
    c.text(21.5, 51.6, "illustrative — the figure carries no scale",
           size=BODY_PT, colour=st.INK_3, style="italic", rect=rect)

    # --- the two scores ----------------------------------------------------
    def score_card(y, h, title, weights):
        rect = c.box(44, y, 55, h, colour=teal, top_rule=True)
        c.text(71.5, y + h - c.yu(TITLE_PT * SPACING) * 0.7, title,
               size=9.2, colour=st.INK, weight="600", rect=rect)
        c.text(71.5, y + h - c.yu(TITLE_PT * SPACING) * 1.75,
               "0 – 100, weighted from", size=BODY_PT, colour=st.INK_3,
               rect=rect)
        lh = c.yu(BODY_PT * 1.55)
        rows = list(weights.items())
        half = (len(rows) + 1) // 2
        first = y + h - c.yu(TITLE_PT * SPACING) * 2.7
        for k, (name, weight) in enumerate(rows):
            col, row = divmod(k, half)
            lx = 46.5 + col * 27.0
            ry = first - row * lh
            c.text(lx, ry, name, size=BODY_PT, colour=st.INK_2, ha="left",
                   rect=rect)
            c.text(lx + 24.0, ry, f"{weight:.0%}", size=BODY_PT, colour=teal,
                   ha="right", weight="600", rect=rect)

    score_card(53, 17, "Ability to pay", facts.ABILITY_TO_PAY_WEIGHTS)
    score_card(28, 21, "Investment capacity", facts.INVESTMENT_CAPACITY_WEIGHTS)

    c.path([(82, 74), (82, 72), (42, 72), (42, 61.5), (44, 61.5)])
    c.path([(42, 61.5), (42, 38.5), (44, 38.5)])

    c.stack(21.5, 38,
            None,
            ("Both scores carry a rationale",
             "that cites the figures it used.",
             "A rationale naming no numbers",
             "is a defect (FR-244)."),
            rect=(3, 26, 40, 48), line_colour=st.INK_2)

    # --- FR-245: when there are no filings ---------------------------------
    amber = st.WARN
    c.card(3, 2, 28, 20, "Filings unavailable",
           ("a young or foreign firm,",
            "or Belgian abbreviated",
            "accounts, which omit",
            "turnover"),
           colour=amber, line_colour=st.INK_2)
    c.card(36, 2, 28, 20, "Secondary signals",
           ("funding rounds · press",
            "headcount growth"),
           colour=amber, line_colour=st.INK_2)
    c.card(69, 2, 30, 20, "Marked estimated",
           ("never shown as a figure;",
            "both scores capped at",
            f"{facts.ESTIMATED_SCORE_CEILING} out of 100 (FR-245)"),
           colour=amber, line_colour=st.INK_2)

    c.path([(15, 74), (15, 71.5), (1.2, 71.5), (1.2, 12), (3, 12)],
           colour=amber, dashed=True)
    c.path([(31, 12), (36, 12)], colour=amber, dashed=True)
    c.path([(64, 12), (69, 12)], colour=amber, dashed=True)
    c.path([(84, 22), (84, 28)], colour=amber, dashed=True)

    c.save("fdd-06-financial")


def main() -> None:
    for fn in (planning, dataflow, adapter, financial):
        fn()


if __name__ == "__main__":
    main()
