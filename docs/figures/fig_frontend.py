"""Figures about the interface and its measured properties.

Five figures, all drawn from ``facts.py`` and painted from ``style.py``:

* ``ta-08-bundle``      route-level code splitting, before and after
* ``ta-09-contrast``    every phase hue against WCAG AA, in both themes
* ``ta-10-codebase``    where the lines live
* ``fdd-08-generation`` the four generated artefacts, and which of them leave
* ``fdd-04-identity``   six signals into a classification gate (RK-02)

Run with ``python3 fig_frontend.py``; the PNGs land in ``out/``.

Note on sizing: ``style.save`` writes with a tight bounding box, which would
crop each diagram to whatever happens to be drawn.  ``_bleed`` lays a
surface-coloured rectangle over the full 100x100 canvas first, so every diagram
comes out at the declared 6.5 inches and the set stays dimensionally consistent.
"""

from __future__ import annotations

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyBboxPatch

import facts
import style as S

# The rose tint from the palette, used here as the ground for a hard stop.  It
# is the only pale red in style.py; nothing new is introduced.
ALARM_SOFT = S.PHASE_SOFT[5]


def _canvas(height: float):
    """A full-bleed 100x100 drawing canvas at the house width.

    The axes is stretched to the whole figure and a surface-coloured rectangle
    laid over it, so ``savefig``'s tight bounding box has the full 6.5 inches to
    crop to and every diagram in the set comes out the same width.
    """
    fig, ax = S.figure(height=height)
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    ax.add_patch(
        mpatches.Rectangle((0, 0), 100, 100, facecolor=S.SURFACE,
                           edgecolor="none", zorder=-10)
    )
    return fig, ax


# ---------------------------------------------------------------------------
# ta-08-bundle
# ---------------------------------------------------------------------------

def bundle() -> None:
    B = facts.BUNDLE
    fig, ax = _canvas(3.3)

    x0, span = 2.0, 84.0
    scale = span / B["before_kb"]
    shell_x = x0 + B["after_initial_kb"] * scale

    def bar(x, y, kb, h, *, fill, edge, lw=1.0):
        w = kb * scale
        ax.add_patch(
            mpatches.Rectangle((x, y), w, h, facecolor=fill, edgecolor=edge,
                               linewidth=lw, zorder=3)
        )
        return x + w

    # --- Before -----------------------------------------------------------
    S.label(ax, x0, 96.0, "Before  —  one bundle, everything up front",
            ha="left", size=8.6, colour=S.INK, weight="600")

    end = bar(x0, 82, B["before_kb"], 9, fill=S.PHASE_SOFT[0], edge=S.PHASE[0])
    S.label(ax, end + 1.4, 86.5, f"{B['before_kb']:.0f} KB", ha="left",
            size=8.5, colour=S.INK, weight="600")
    ax.plot([shell_x, shell_x], [82, 91], color=S.PHASE[0], linewidth=1.0,
            zorder=4)
    S.label(ax, (shell_x + end) / 2, 86.5,
            f"{B['before_kb'] - B['after_initial_kb']:.0f} KB that no longer "
            "loads first", size=7.6, colour=S.INK_2)

    end = bar(x0, 73, B["before_gzip_kb"], 6, fill=S.PHASE[0], edge=S.PHASE[0])
    S.label(ax, end + 1.4, 76.0, f"{B['before_gzip_kb']:.0f} KB over the wire (gzip)",
            ha="left", size=8.0, colour=S.INK_2)

    # --- After ------------------------------------------------------------
    S.label(ax, x0, 64.0, "After  —  a shared shell, routes on demand",
            ha="left", size=8.6, colour=S.INK, weight="600")

    for y_a, y_b in ((81.8, 68.5), (61.0, 55.4)):
        ax.plot([shell_x, shell_x], [y_a, y_b], color=S.LINE,
                linestyle=(0, (2, 2.4)), linewidth=1.0, zorder=1)

    bar(x0, 46, B["after_initial_kb"], 9, fill=S.SURFACE_2, edge=S.ACCENT, lw=1.3)
    S.label(ax, shell_x + 1.4, 50.5, f"{B['after_initial_kb']:.0f} KB shell",
            ha="left", size=8.5, colour=S.INK, weight="600")

    gz_end = bar(x0, 37, B["after_initial_gzip_kb"], 6,
                 fill=S.ACCENT, edge=S.ACCENT)
    saved = 1 - B["after_initial_gzip_kb"] / B["before_gzip_kb"]
    S.label(ax, gz_end + 1.4, 40.0,
            f"{B['after_initial_gzip_kb']:.0f} KB over the wire (gzip)"
            f"  —  {saved:.0%} less before first paint",
            ha="left", size=8.0, colour=S.INK_2)

    # the 22 lazy chunks, drawn as a count and never as sizes
    S.label(ax, 43.0, 50.5, "+", ha="center", size=13, colour=S.INK_3)
    cx, cw, gap = 46.5, 1.5, 0.45
    for i in range(B["lazy_chunks"]):
        ax.add_patch(
            mpatches.Rectangle((cx + i * (cw + gap), 47.4), cw, 5.6,
                               facecolor=S.ACCENT, edgecolor="none", zorder=3)
        )
    S.label(ax, cx, 59.5,
            f"{B['lazy_chunks']} lazy chunks, {B['smallest_page_kb']:.0f}–"
            f"{B['largest_page_kb']:.0f} KB each, fetched on navigation",
            ha="left", size=7.8, colour=S.INK_2)

    # --- the point --------------------------------------------------------
    ax.add_patch(
        FancyBboxPatch((x0, 8), 90.0 - x0, 17.0,
                       boxstyle="round,pad=0,rounding_size=1.6",
                       facecolor=S.SURFACE_2, edgecolor=S.LINE, linewidth=1.0,
                       zorder=1)
    )
    ax.text(46, 16.5,
            "The administration area and the PDF previews are no longer in the "
            "first payload.\nNothing is downloaded to reach the profile page "
            "but the profile page  (NFR-101).",
            ha="center", va="center", fontsize=8.2, color=S.INK_2,
            linespacing=1.6, zorder=4)

    S.save(fig, "ta-08-bundle")


# ---------------------------------------------------------------------------
# ta-09-contrast
# ---------------------------------------------------------------------------

def contrast() -> None:
    key = {name: k for k, name in S.PHASE_NAME.items()}
    names = list(facts.CONTRAST_LIGHT)
    aa = facts.WCAG_AA_SMALL

    fig, axes = plt.subplots(1, 2, figsize=(S.WIDTH, 3.45), sharey=True)
    fig.subplots_adjust(left=0.135, right=0.995, top=0.795, bottom=0.125,
                        wspace=0.30)

    y = np.arange(len(names))
    h = 0.36

    for ax, (title, table) in zip(
        axes, [("Light theme", facts.CONTRAST_LIGHT),
               ("Dark theme", facts.CONTRAST_DARK)], strict=False
    ):
        ax.add_patch(
            mpatches.Rectangle((0, -1.3), aa, len(names) + 1.0,
                               facecolor=S.LINE_SOFT, edgecolor="none", zorder=0)
        )
        ax.axvline(aa, color=S.DANGER, linestyle="--", linewidth=1.1, zorder=2)

        for i, name in enumerate(names):
            hue = S.PHASE[key[name]]
            tint = S.PHASE_SOFT[key[name]]
            on_surface, on_tint = table[name]

            ax.barh(i - h / 2 - 0.02, on_surface, height=h, color=hue,
                    edgecolor=hue, linewidth=0.8, zorder=3)
            ax.barh(i + h / 2 + 0.02, on_tint, height=h, color=tint,
                    edgecolor=hue, linewidth=1.1, zorder=3)

            for val, yy in ((on_surface, i - h / 2 - 0.02),
                            (on_tint, i + h / 2 + 0.02)):
                ax.text(val + 0.18, yy, f"{val:.2f}", va="center", ha="left",
                        fontsize=7.8, color=S.INK_2, zorder=4)

        ax.set_yticks(y)
        ax.set_yticklabels(names, fontsize=8.5, color=S.INK)
        ax.set_xlim(0, 9.9)
        ax.set_ylim(len(names) - 0.45, -1.35)
        ax.set_xticks([0, 2, 4, 6, 8])
        ax.set_xlabel("contrast ratio  (:1)", fontsize=8.2)
        ax.set_title(title, fontsize=9.2, color=S.INK, pad=6, loc="left")
        S.bare_axes(ax)
        ax.tick_params(length=2.5)
        ax.spines["left"].set_visible(False)
        ax.tick_params(axis="y", length=0)
        ax.text(aa + 0.2, -0.95, "WCAG AA  4.5:1", fontsize=7.4,
                color=S.DANGER, ha="left", va="center", zorder=4)

    lo_l = min(min(v) for v in facts.CONTRAST_LIGHT.values())
    lo_d = min(min(v) for v in facts.CONTRAST_DARK.values())
    fig.text(0.005, 0.975,
             "Every phase hue clears AA for small text — on the surface it sits "
             "on, and on its own tint",
             fontsize=8.8, color=S.INK, weight="600", ha="left", va="top")
    fig.text(0.005, 0.905,
             f"filled bar = hue on the surface       outlined bar = hue on its "
             f"own tint       lowest of the 24 measurements: {lo_l:.2f} light, "
             f"{lo_d:.2f} dark",
             fontsize=7.6, color=S.INK_3, ha="left", va="top")

    S.save(fig, "ta-09-contrast")


# ---------------------------------------------------------------------------
# ta-10-codebase
# ---------------------------------------------------------------------------

def codebase() -> None:
    pkgs = sorted(facts.PACKAGE_LINES.items(), key=lambda kv: -kv[1])
    C = facts.CODEBASE

    fig = plt.figure(figsize=(S.WIDTH, 3.9))
    gs = fig.add_gridspec(1, 2, width_ratios=[2.25, 1.0], wspace=0.10,
                          left=0.155, right=0.985, top=0.855, bottom=0.055)
    ax = fig.add_subplot(gs[0, 0])
    ax2 = fig.add_subplot(gs[0, 1])

    labels = [p for p, _ in pkgs]
    values = [v for _, v in pkgs]
    colours = [S.ACCENT if p == "pipeline" else S.PHASE[0] for p in labels]

    y = np.arange(len(labels))
    bars = ax.barh(y, values, height=0.62, color=colours, edgecolor="none",
                   zorder=3)
    for bar, v in zip(bars, values, strict=False):
        ax.text(bar.get_width() + 330, bar.get_y() + bar.get_height() / 2,
                f"{v:,}", va="center", ha="left", fontsize=8.0, color=S.INK_2)

    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=8.5, color=S.INK)
    ax.invert_yaxis()
    ax.set_xlim(0, max(values) * 1.20)
    ax.set_xticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.set_title("Backend packages, lines of Python", fontsize=9.2,
                 color=S.INK, loc="left", pad=9)

    share = values[0] / C["backend_lines"]
    ax.text(values[0] * 1.19, len(labels) - 0.9,
            f"the pipeline alone is {share:.0%} of the backend",
            ha="right", va="center", fontsize=7.8, color=S.ACCENT)
    ax.text(values[0] * 1.19, len(labels) - 0.05,
            f"{len(labels)} packages · {C['api_routes']} API routes · "
            f"{C['db_tables']} tables",
            ha="right", va="center", fontsize=7.6, color=S.INK_3)

    # --- the three bodies of code ----------------------------------------
    # Drawn as one stacked column rather than as bars: the left panel is a
    # different scale, and a reader must not compare a bar there with a bar
    # here.  A column of one whole cannot be misread that way, and it shows the
    # test share as an area instead of asking for a subtraction.
    groups = [
        ("Backend", C["backend_lines"], C["backend_files"], S.PHASE[0]),
        ("Frontend", C["frontend_lines"], C["frontend_files"], S.INK_3),
        ("Tests", C["test_lines"], C["test_files"], S.OK),
    ]
    total = sum(g[1] for g in groups)

    ax2.set_xlim(0, 1)
    ax2.set_ylim(-total * 0.26, total * 1.04)
    bottom = 0.0
    for name, lines, files, colour in reversed(groups):
        ax2.add_patch(
            mpatches.Rectangle((0.05, bottom), 0.30, lines, facecolor=colour,
                               edgecolor=S.SURFACE, linewidth=0.8, zorder=3)
        )
        mid = bottom + lines / 2
        ax2.text(0.41, mid + total * 0.022, name, ha="left", va="center",
                 fontsize=8.5, color=S.INK, weight="600")
        ax2.text(0.41, mid - total * 0.020, f"{lines:,} lines",
                 ha="left", va="center", fontsize=7.6, color=S.INK_2)
        ax2.text(0.41, mid - total * 0.060, f"{files} files",
                 ha="left", va="center", fontsize=7.2, color=S.INK_3)
        bottom += lines

    ax2.set_xticks([])
    ax2.set_yticks([])
    for sp in ax2.spines.values():
        sp.set_visible(False)
    ax2.set_title("Where the lines live", fontsize=9.2, color=S.INK,
                  loc="left", pad=9)
    ax2.text(0.05, total * 1.005, f"{total:,} lines in all", ha="left",
             va="bottom", fontsize=7.6, color=S.INK_3)
    ax2.text(0.05, -total * 0.135,
             f"tests are {C['test_lines'] / total:.0%} of everything written\n"
             f"{C['tests_passing']} tests passing",
             ha="left", va="center", fontsize=7.8, color=S.OK, linespacing=1.6)

    S.save(fig, "ta-10-codebase")


# ---------------------------------------------------------------------------
# fdd-08-generation
# ---------------------------------------------------------------------------

def generation() -> None:
    fig, ax = _canvas(4.1)
    amber, amber_soft = S.PHASE[4], S.PHASE_SOFT[4]

    ROWS = [
        ("Tailored CV",
         ["only true profile facts, with the photo",
          "DOCX + PDF, in the language of the opening"], [], 15.0, True),
        ("Introduction email",
         ["addressed to the validated hiring contact"],
         ["if speculative (FR-263): a spontaneous",
          "application that never asserts a vacancy"], 19.0, True),
        ("Company & job briefing",
         ["interview prep: company profile, financials,",
          "signals, competitors, likely questions"], [], 15.0, False),
        ("Motivation & fit",
         ["why this job, why you fit, objections with",
          "answers, rehearsable talking points"], [], 15.0, False),
    ]

    ax_x, ax_w = 17.0, 35.0
    trunk = 14.2
    tops, t = [], 91.0
    for *_x, h, _l in ROWS:
        tops.append(t)
        t -= h + 4.33
    centres = [tp - h / 2 for (*_x, h, _l), tp in zip(ROWS, tops, strict=False)]

    S.label(ax, ax_x, 95.5, "Four artefacts per selected opportunity",
            ha="left", size=8.6, colour=S.INK, weight="600")
    S.label(ax, 68.0, 95.5, "Where each one goes", ha="left", size=8.6,
            colour=S.INK, weight="600")

    # source
    S.box(ax, 1.0, 42.0, 12.0, 21.0, "Selected\nopportunity", colour=amber,
          fill=amber_soft, fontsize=8.5, weight="600", top_rule=True)

    ax.plot([trunk, trunk], [centres[0], centres[-1]], color=amber,
            linewidth=1.0, zorder=1)
    ax.plot([13.0, trunk], [52.5, 52.5], color=amber, linewidth=1.0, zorder=1)

    for (name, subs, spec, h, _leaves), tp, cy in zip(ROWS, tops, centres,
                                                      strict=False):
        y = tp - h
        S.box(ax, ax_x, y, ax_w, h, "", colour=amber, fill=S.SURFACE)
        ax.text(ax_x + 2.2, tp - 4.0, name, ha="left", va="center",
                fontsize=8.5, color=S.INK, weight="600", zorder=4)
        ly = tp - 8.0
        for sub in subs:
            ax.text(ax_x + 2.2, ly, sub, ha="left", va="center", fontsize=7.0,
                    color=S.INK_3, zorder=4)
            ly -= 3.4
        if spec:
            ly -= 0.6
            ax.plot([ax_x + 1.2, ax_x + 1.2], [ly + 2.0, ly - 3.4 - 1.0],
                    color=S.SPECULATIVE, linewidth=2.0, solid_capstyle="round",
                    zorder=4)
            for sub in spec:
                ax.text(ax_x + 2.8, ly, sub, ha="left", va="center",
                        fontsize=6.8, color=S.SPECULATIVE, zorder=4)
                ly -= 3.4
        S.arrow(ax, 14.2, cy, ax_x - 0.7, cy, colour=amber, lw=1.0)

    # the factual-consistency gate
    gx, gw = 56.5, 6.5
    ax.add_patch(
        FancyBboxPatch((gx, 14.0), gw, 78.0,
                       boxstyle="round,pad=0,rounding_size=1.6",
                       facecolor=amber_soft, edgecolor=amber, linewidth=1.2,
                       zorder=2)
    )
    ax.text(gx + gw / 2, 53.0, "Factual-consistency check  (FR-322)",
            rotation=90, ha="center", va="center", fontsize=8.2, color=S.INK,
            weight="600", zorder=4)
    for cy in centres:
        S.arrow(ax, ax_x + ax_w + 0.4, cy, gx - 0.6, cy, colour=S.INK_3, lw=0.9)

    S.arrow(ax, gx + gw / 2, 13.8, gx + gw / 2, 11.9, colour=S.DANGER, lw=1.2)
    ax.add_patch(
        FancyBboxPatch((26.0, 0.8), 40.0, 10.6,
                       boxstyle="round,pad=0,rounding_size=1.6",
                       facecolor=S.SURFACE, edgecolor=S.DANGER, linewidth=1.1,
                       zorder=2)
    )
    ax.text(46.0, 8.4, "A failed check visibly blocks approval", ha="center",
            va="center", fontsize=8.0, color=S.DANGER, weight="600", zorder=4)
    ax.text(46.0, 4.0,
            "date, employer and title checks are deterministic;\n"
            "an LLM judge contributes one signal, not the verdict",
            ha="center", va="center", fontsize=7.2, color=S.INK_2,
            linespacing=1.5, zorder=4)

    # the egress boundary
    ex = 65.5
    ax.plot([ex, ex], [13.0, 92.0], color=S.LINE, linestyle=(0, (2, 2.4)),
            linewidth=1.1, zorder=1)

    # sent
    for cy, dest in ((centres[0], "Attached to the email"),
                     (centres[1], "Sent to the hiring contact")):
        S.arrow(ax, gx + gw + 0.4, cy, 67.4, cy, colour=S.OK, lw=1.3)
        S.box(ax, 68.0, cy - 5.5, 31.5, 11.0, dest, colour=S.OK,
              fill=S.SURFACE, fontsize=8.0, text_colour=S.INK)

    # never sent
    ax.add_patch(
        FancyBboxPatch((68.0, 13.0), 31.5, 37.0,
                       boxstyle="round,pad=0,rounding_size=1.6",
                       facecolor=ALARM_SOFT, edgecolor=S.DANGER, linewidth=1.3,
                       zorder=1)
    )
    for cy in (centres[2], centres[3]):
        ax.plot([gx + gw + 0.4, ex], [cy, cy], color=S.DANGER, linewidth=1.6,
                zorder=3)
        ax.plot([ex, ex], [cy - 3.6, cy + 3.6], color=S.DANGER, linewidth=3.4,
                solid_capstyle="butt", zorder=4)

    ax.text(83.75, 43.0, "NEVER SENT", ha="center", va="center", fontsize=11.5,
            color=S.DANGER, weight="bold", zorder=5)
    ax.text(83.75, 30.0,
            "the briefing and the motivation document\n"
            "stay with the job seeker: they are\n"
            "preparation, not correspondence",
            ha="center", va="center", fontsize=7.4, color=S.INK_2,
            linespacing=1.6, zorder=5)
    ax.text(83.75, 18.0, "FR-329, FR-330", ha="center", va="center",
            fontsize=7.0, color=S.DANGER, zorder=5)

    S.save(fig, "fdd-08-generation")


# ---------------------------------------------------------------------------
# fdd-04-identity
# ---------------------------------------------------------------------------

def identity() -> None:
    fig, ax = _canvas(4.1)
    violet, violet_soft = S.PHASE[1], S.PHASE_SOFT[1]

    SIGNALS = [
        ("Name variants", "spelling, ordering, diacritics, initials"),
        ("Employer overlap", "organisations named against the profile"),
        ("Location", "cities and regions"),
        ("Timeline consistency", "page dates against the career span"),
        ("Cross-links", "links to a profile already confirmed"),
        ("Photo similarity", "perceptual hash against the profile photo"),
    ]

    S.label(ax, 1.0, 96.0, "Six independent signals", ha="left", size=8.6,
            colour=S.INK, weight="600")
    S.label(ax, 1.0, 91.5, "each scored and recorded separately", ha="left",
            size=7.2)
    S.label(ax, 50.0, 96.0, "Classification (FR-123)", ha="left", size=8.6,
            colour=S.INK, weight="600")
    S.label(ax, 72.0, 96.0, "What happens to the finding", ha="left", size=8.6,
            colour=S.INK, weight="600")

    bx, bw, bh, gap = 1.0, 35.0, 10.0, 2.1
    tops = [86.0 - i * (bh + gap) for i in range(len(SIGNALS))]
    centres = [tp - bh / 2 for tp in tops]
    for (name, sub), tp in zip(SIGNALS, tops, strict=False):
        y = tp - bh
        S.box(ax, bx, y, bw, bh, "", colour=violet, fill=S.SURFACE)
        ax.text(bx + 2.0, y + bh * 0.68, name, ha="left", va="center",
                fontsize=8.2, color=S.INK, weight="600", zorder=4)
        ax.text(bx + 2.0, y + bh * 0.26, sub, ha="left", va="center",
                fontsize=6.8, color=S.INK_3, zorder=4)

    # the six scores converge on one finding
    node_c = (centres[0] + centres[-1]) / 2
    for cy in centres:
        S.arrow(ax, bx + bw + 0.4, cy, 39.6, node_c, colour=S.INK_3, lw=0.8,
                style="-")
    S.box(ax, 40.0, node_c - 15.0, 5.5, 30.0, "", colour=violet,
          fill=violet_soft)
    ax.text(42.75, node_c, "one finding, six scores", rotation=90, ha="center",
            va="center", fontsize=7.8, color=S.INK, weight="600", zorder=4)

    # classification
    gx, gw = 50.0, 18.0
    CLASSES = [("Confirmed", S.OK, 70.0), ("Probable", S.WARN, 51.0),
               ("Doubtful", S.WARN, 32.0)]
    for name, colour, cy in CLASSES:
        S.box(ax, gx, cy - 6.0, gw, 12.0, name, colour=colour, fill=S.SURFACE,
              fontsize=8.5, weight="600", text_colour=S.INK)
        S.arrow(ax, 45.9, node_c, gx - 0.6, cy, colour=S.INK_3, lw=0.9,
                connection="arc3,rad=0.06")

    # outcomes
    ox, ow = 72.0, 27.0
    S.arrow(ax, gx + gw + 0.4, 70.0, ox - 0.6, 70.0, colour=S.OK, lw=1.3)
    S.box(ax, ox, 62.0, ow, 16.0,
          "Merged automatically\ninto the composite profile", colour=S.OK,
          fill=S.SURFACE, fontsize=7.9)
    S.label(ax, ox + ow / 2, 58.5, "FR-124 — the only automatic path", size=7.0)

    for cy in (51.0, 32.0):
        S.arrow(ax, gx + gw + 0.4, cy, ox - 0.6, 43.0, colour=S.WARN, lw=1.3,
                connection="arc3,rad=0.08")
    S.box(ax, ox, 35.0, ow, 16.0,
          "Shown with its evidence\nfor the job seeker to decide", colour=violet,
          fill=violet_soft, fontsize=7.9)

    S.arrow(ax, ox + ow * 0.25, 34.6, ox + ow * 0.25, 29.4, colour=S.OK, lw=1.1)
    S.arrow(ax, ox + ow * 0.75, 34.6, ox + ow * 0.75, 29.4, colour=S.DANGER,
            lw=1.1)
    S.box(ax, ox, 16.0, ow * 0.46, 13.0, "Confirm\nand merge", colour=S.OK,
          fill=S.SURFACE, fontsize=7.6)
    S.box(ax, ox + ow * 0.54, 16.0, ow * 0.46, 13.0, "Reject\npermanently",
          colour=S.DANGER, fill=ALARM_SOFT, fontsize=7.6)
    S.label(ax, ox + ow / 2, 12.5, "a rejected finding is never proposed again",
            size=7.0)

    # the reason the figure exists
    ax.add_patch(
        FancyBboxPatch((1.0, 0.5), 98.0, 9.5,
                       boxstyle="round,pad=0,rounding_size=1.6",
                       facecolor=S.SURFACE_2, edgecolor=S.LINE, linewidth=1.0,
                       zorder=1)
    )
    ax.text(50, 5.3,
            "RK-02  —  a namesake's career must never reach a CV.\n"
            "Nothing merges on a name alone, and nothing below "
            "\u2018confirmed\u2019 merges without the job seeker saying so.",
            ha="center", va="center", fontsize=7.8, color=S.INK_2,
            linespacing=1.6, zorder=4)

    S.save(fig, "fdd-04-identity")


def main() -> None:
    bundle()
    contrast()
    codebase()
    generation()
    identity()


if __name__ == "__main__":
    main()
