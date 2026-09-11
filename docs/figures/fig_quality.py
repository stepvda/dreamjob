"""Figures about scoring, honesty and evidence.

Five figures for the Functional Design Document:

* ``fdd-07-scoring``      seven sub-scores into one number, the dream-job meter
                          kept beside it, and the human override that wins.
* ``fdd-10-sample-size``  the same 50% at six sample sizes, with its Wilson
                          interval, and the threshold below which the product
                          refuses to advise.
* ``fdd-09-learning``     the response-and-learning loop, closing through a
                          person and leaving the previous directive version
                          intact.
* ``fdd-09b-segments``    the worked example: data engineering against data
                          science, with the overlap left visible.
* ``fdd-11-coverage``     requirement coverage by MoSCoW priority, with the two
                          uncited requirements named rather than rounded away.

Every number comes from ``facts.py``; every colour comes from ``style.py``.
"""

from __future__ import annotations

import math

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch

import facts
import style
from style import (
    ACCENT,
    INK,
    INK_2,
    INK_3,
    LINE_SOFT,
    PHASE,
    PHASE_SOFT,
    SURFACE,
    SURFACE_2,
)

DASH = (0, (2.6, 2.0))


# --- small local helpers ----------------------------------------------------


def pct(value: float) -> str:
    """A percentage rounded the way a reader expects: 62.5 becomes 63."""
    return f"{math.floor(value + 0.5):.0f}%"


def full_bleed(fig):
    """Let a diagram use the whole canvas, so a tight box keeps 6.5 inches."""
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)


def card(ax, x, y, w, h, title, body=None, *, colour, fill=None,
         top_rule=True, dashed=False, title_size=8.8, body_size=7.4,
         title_band=None, lw=1.1):
    """A titled box: bold title in a band at the top, quiet body beneath."""
    patch = style.box(ax, x, y, w, h, "", colour=colour, fill=fill,
                      top_rule=top_rule, lw=lw)
    if dashed:
        patch.set_linestyle(DASH)
    cx = x + w / 2
    if body is None:
        ax.text(cx, y + h / 2, title, ha="center", va="center",
                fontsize=title_size, color=INK, weight="600", zorder=4,
                linespacing=1.4)
        return patch
    band = title_band if title_band is not None else min(7.0, h * 0.38)
    ax.text(cx, y + h - band / 2, title, ha="center", va="center",
            fontsize=title_size, color=INK, weight="600", zorder=4,
            linespacing=1.35)
    ax.text(cx, y + (h - band) / 2, body, ha="center", va="center",
            fontsize=body_size, color=INK_2, zorder=4, linespacing=1.5)
    return patch


def thin_arrow(ax, x1, y1, x2, y2, *, colour=INK_3, lw=0.8, rad=0.0,
               scale=8, dashed=False):
    ax.add_patch(
        FancyArrowPatch(
            (x1, y1), (x2, y2),
            arrowstyle="-|>", mutation_scale=scale, linewidth=lw, color=colour,
            connectionstyle=f"arc3,rad={rad}",
            linestyle=DASH if dashed else "-", zorder=1,
        )
    )


def no_entry(ax, x, y, *, r=1.5, colour=INK_3):
    """The crossed circle that marks a connection which deliberately is not made."""
    ax.plot([x], [y], marker="o", markersize=8.5, markerfacecolor="none",
            markeredgecolor=colour, markeredgewidth=1.1, zorder=4)
    ax.plot([x - r * 0.62, x + r * 0.62], [y - r * 1.05, y + r * 1.05],
            color=colour, linewidth=1.1, zorder=4, solid_capstyle="round")


# --- fdd-07 -----------------------------------------------------------------


def fig_scoring():
    """Seven sub-scores, one score, one separate meter, one human override."""
    fig, ax = style.figure(height=3.9)
    full_bleed(fig)
    teal, violet = PHASE[3], PHASE[1]

    # Dream-job fit is drawn first so its second output can leave the panel
    # without crossing the lines that feed the weighted sum.
    order = ["Dream-job fit", "Profile fit", "Directive fit", "Company",
             "Compensation", "Plausibility", "Reachability"]
    by_name = {name: (kind, note) for name, kind, note in facts.SUB_SCORES}
    rows = [(name, *by_name[name]) for name in order]

    style.label(ax, 1, 95, "Seven weighted sub-scores", size=8.6,
                colour=INK_2, ha="left", weight="600")

    top, hh, step = 88.0, 8.0, 9.6
    mids = []
    for i, (name, kind, _note) in enumerate(rows):
        yy = top - i * step - hh
        llm = kind == "LLM"
        p = style.box(ax, 1, yy, 26, hh, "", colour=teal,
                      fill=SURFACE if llm else PHASE_SOFT[3])
        if llm:
            p.set_linestyle(DASH)
        ax.text(3.4, yy + hh / 2, name, ha="left", va="center", fontsize=8.5,
                color=INK, zorder=4)
        ax.text(25.0, yy + hh / 2, "LLM" if llm else "computed", ha="right",
                va="center", fontsize=7.4, color=INK_3, zorder=4)
        mids.append(yy + hh / 2)

    # the weighted sum
    card(ax, 40, 46, 26, 16, "Weighted overall score",
         "one number, reproducible;\nweights re-tuned by feedback\n(FR-285)",
         colour=teal, fill=PHASE_SOFT[3], title_band=6.4)
    for i, ymid in enumerate(mids):
        thin_arrow(ax, 27.4, ymid, 39.4, 60.0 - i * 2.0,
                   colour="#b3afc2", lw=0.75, rad=0.03)

    # the meter, fed from the same dream-job comparison, summed into nothing
    card(ax, 40, 76, 26, 16, "Dream-job fit meter",
         "criteria met · partially met ·\nviolated  (FR-383)",
         colour=violet, fill=PHASE_SOFT[1], title_band=6.4)
    thin_arrow(ax, 27.4, mids[0], 39.4, 84.0, colour=violet, lw=1.2, scale=9)

    ax.plot([47, 47], [76, 73.0], linestyle=DASH, color="#c4c0d2", lw=1.0)
    ax.plot([47, 47], [66.0, 62], linestyle=DASH, color="#c4c0d2", lw=1.0)
    no_entry(ax, 47, 69.5)
    ax.text(50.4, 69.5, "not summed into\nthe overall score", ha="left",
            va="center", fontsize=7.4, color=INK_3, linespacing=1.5)

    # the ranked list, and the person who re-orders it
    card(ax, 72, 68, 27, 12, "Computed rank",
         "the order the system proposes", colour=teal, title_band=5.6)
    card(ax, 72, 43, 27, 16, "The job seeker re-orders",
         "pins, drags, marks not interested\nwith a reason  (FR-284)",
         colour=ACCENT, lw=1.8, title_band=6.4)
    card(ax, 72, 17, 27, 16, "Final order",
         "the manual order wins and\nsurvives recalculation",
         colour=ACCENT, fill=SURFACE_2, lw=1.8, title_band=6.4)

    style.arrow(ax, 66, 54, 71.4, 72, connection="arc3,rad=0.22")
    style.arrow(ax, 85.5, 68, 85.5, 59.6)
    style.arrow(ax, 85.5, 43, 85.5, 33.6)

    thin_arrow(ax, 66, 86, 70.6, 86, colour=violet, lw=1.0, dashed=True)
    ax.text(71.6, 86, "shown beside each\nopportunity, on its own",
            ha="left", va="center", fontsize=7.4, color=INK_2, linespacing=1.5)

    # legend
    style.box(ax, 1, 12.4, 3.4, 3.4, "", colour=teal, fill=PHASE_SOFT[3],
              radius=0.8)
    style.label(ax, 5.8, 14.1, "computed deterministically", size=7.4,
                colour=INK_2, ha="left")
    p = style.box(ax, 1, 7.0, 3.4, 3.4, "", colour=teal, fill=SURFACE,
                  radius=0.8)
    p.set_linestyle(DASH)
    style.label(ax, 5.8, 8.7, "written by the language model", size=7.4,
                colour=INK_2, ha="left")

    style.caption_band(
        ax, 1.5,
        "A role can fit a CV perfectly and still not be what the person wants,\n"
        "so the meter is reported beside the score and never inside it.",
    )
    return style.save(fig, "fdd-07-scoring")


# --- fdd-10 -----------------------------------------------------------------


def fig_sample_size():
    """The same 50%, six times, with what is actually known about it."""
    fig, ax = plt.subplots(figsize=(style.WIDTH, 3.5))
    fig.subplots_adjust(left=0.09, right=0.98, top=0.90, bottom=0.28)
    rose = PHASE[5]

    data = facts.SAMPLE_SIZE_DEMO
    ax.set_xscale("log")
    ax.set_xlim(1.5, 132)
    ax.set_ylim(-4, 110)

    threshold = facts.MIN_SEGMENT_TO_ADVISE   # below this, no advice is given
    ax.axvspan(1.5, threshold, facecolor=PHASE_SOFT[0], edgecolor="none",
               zorder=0)
    ax.axvline(threshold, color=INK_3, linestyle=DASH, linewidth=1.0, zorder=1)
    ax.text(threshold * 1.08, 100,
            f"advice threshold — {threshold} resolved applications",
            ha="left", va="center", fontsize=7.8, color=INK_2)
    ax.text((1.5 * threshold) ** 0.5, 3.0, "no advice below this",
            ha="center", va="center", fontsize=7.4, color=INK_3, style="italic")

    ax.axhline(50, color=INK_3, linewidth=0.8, linestyle=(0, (3, 3)), zorder=1)
    ax.text(128, 50, "50%", ha="right", va="bottom", fontsize=7.6, color=INK_3)

    widths = []
    for n, k in data:
        lo, hi = facts.wilson(k, n)
        lo, hi = lo * 100, hi * 100
        widths.append(hi - lo)
        ax.plot([n, n], [lo, hi], color=rose, linewidth=3.4, zorder=3,
                solid_capstyle="round")
        ax.plot([n], [50], marker="o", markersize=6.2, color=rose,
                markeredgecolor=SURFACE, markeredgewidth=1.4, zorder=4)
        ax.text(n, hi + 3.0, f"{pct(lo)[:-1]}–{pct(hi)}", ha="center", va="bottom",
                fontsize=7.6, color=INK_2)

    ax.set_xticks([n for n, _ in data])
    ax.set_xticklabels([f"{k} of {n}" for n, k in data])
    ax.minorticks_off()
    ax.set_yticks([0, 50, 100])
    ax.set_yticklabels(["0%", "50%", "100%"])
    style.bare_axes(ax)
    ax.set_xlabel("applications resolved, and how many replied",
                  fontsize=8.5, labelpad=6)
    ax.set_ylabel("reply rate", fontsize=8.5)
    ax.tick_params(length=0)

    ax.text(
        0.5, -0.30,
        f"Every point is the same observed rate: 50%. What changes is the 95% "
        f"interval around it:\n{widths[0]:.0f} percentage points wide at n = 2, "
        f"{widths[-1]:.0f} at n = 80.",
        transform=ax.transAxes, ha="center", va="top", fontsize=7.6,
        color=INK_3, style="italic",
    )
    return style.save(fig, "fdd-10-sample-size")


# --- fdd-09 -----------------------------------------------------------------


def fig_learning():
    """The loop from a sent application to a new directive version."""
    fig, ax = style.figure(height=3.6)
    full_bleed(fig)
    amber, rose, blue = PHASE[4], PHASE[5], PHASE[2]

    w, gap = 21.0, 5.0
    xs = [0.5 + i * (w + gap) for i in range(4)]
    top_y, bot_y, h = 73.0, 27.0, 23.0

    #  colour, fill, title, title band, body
    top_row = [
        (amber, PHASE_SOFT[4], "Application sent", 7.0,
         "dispatched in a\ncampaign (phase 4)"),
        (rose, PHASE_SOFT[5], "Response recorded", 7.0,
         "detected in the mailbox,\nor entered by hand:\nphone, LinkedIn, ATS"),
        (rose, PHASE_SOFT[5], "Classified", 7.0,
         "six reply types; the\nperson's stated outcome\noverrides the model"),
        (rose, PHASE_SOFT[5], "Outcome", 7.0,
         "resolved or still open;\na rejection counts\nas much as good news"),
    ]
    bottom_row = [   # drawn right to left, so the loop turns back
        (rose, PHASE_SOFT[5], "Segmented", 7.0,
         "9 dimensions a directive\ncan change; each rate\nwith its n and interval"),
        (rose, PHASE_SOFT[5], "Advice proposed", 7.0,
         "the numbers first, the\nmodel second — never\nasked to find a pattern"),
        (ACCENT, SURFACE, "The job seeker\naccepts", 11.5,
         "or does not; nothing is\napplied automatically"),
        (blue, PHASE_SOFT[2], "New directive-set\nversion", 11.5,
         "v2 — v1 is untouched\nand can be returned to"),
    ]

    for x, (colour, fill, title, band, body) in zip(xs, top_row):
        card(ax, x, top_y, w, h, title, body, colour=colour, fill=fill,
             title_band=band, title_size=8.6, body_size=7.3)
    for i, (colour, fill, title, band, body) in enumerate(bottom_row):
        card(ax, xs[3 - i], bot_y, w, h, title, body, colour=colour, fill=fill,
             title_band=band, title_size=8.6, body_size=7.3,
             lw=1.9 if colour == ACCENT else 1.1)

    ymid_top, ymid_bot = top_y + h / 2, bot_y + h / 2
    for i in range(3):
        style.arrow(ax, xs[i] + w, ymid_top, xs[i + 1] - 0.8, ymid_top)
        style.arrow(ax, xs[3 - i], ymid_bot, xs[2 - i] + w + 0.8, ymid_bot)
    style.arrow(ax, xs[3] + w / 2, top_y, xs[3] + w / 2, bot_y + h + 0.8)
    style.arrow(ax, xs[0] + w / 2, bot_y + h, xs[0] + w / 2, top_y - 0.8,
                colour=blue)
    ax.text(xs[0] + w / 2 + 2.2, 61.0, "next campaign\nruns under v2",
            ha="left", va="center", fontsize=7.4, color=INK_2, linespacing=1.5)

    ax.text(50, 65.0, "The loop closes only through a person.", ha="center",
            va="center", fontsize=9.6, color=INK, weight="600")
    ax.text(50, 57.0,
            "Advice is proposed, never applied — and the directive set it\n"
            "replaces is kept, so the previous version can be returned to.",
            ha="center", va="center", fontsize=7.8, color=INK_2,
            linespacing=1.6)

    style.legend_dots(
        ax, 1.5, 19.0,
        [(amber, "Apply"), (rose, "Follow up"), (blue, "Plan"),
         (ACCENT, "the job seeker decides")],
        gap=17.0,
    )
    style.caption_band(
        ax, 9.0,
        "Four channels reach the same record: a mailbox reply the system detects,\n"
        "and a phone call, a LinkedIn message or an ATS portal reply entered by hand.",
    )
    return style.save(fig, "fdd-09-learning")


# --- fdd-09b ----------------------------------------------------------------


def fig_segments():
    """The worked example, with the overlap left where a reader can see it."""
    fig, ax = plt.subplots(figsize=(style.WIDTH, 3.0))
    fig.subplots_adjust(left=0.235, right=0.975, top=0.86, bottom=0.32)
    rose = PHASE[5]

    seg = facts.SEGMENT_EXAMPLE
    rows = [("Data engineering", 1), ("Data science", 0)]
    total_n = sum(v["n"] for v in seg.values())
    total_r = sum(v["replies"] for v in seg.values())
    baseline = 100 * total_r / total_n

    bounds = {}
    for name, _y in rows:
        d = seg[name]
        lo, hi = facts.wilson(d["replies"], d["n"])
        bounds[name] = (lo * 100, hi * 100, 100 * d["replies"] / d["n"])

    ov_lo = max(b[0] for b in bounds.values())
    ov_hi = min(b[1] for b in bounds.values())
    ax.axvspan(ov_lo, ov_hi, facecolor=LINE_SOFT, edgecolor="none", zorder=0)

    # a segment rather than a full rule, so it does not strike through the note
    ax.plot([baseline, baseline], [-0.22, 1.72], color=INK_3, linestyle=DASH,
            linewidth=1.0, zorder=1)
    ax.text(baseline + 1.8, 1.63,
            f"baseline: both segments together, {total_r} of {total_n} "
            f"({pct(baseline)})",
            ha="left", va="center", fontsize=7.6, color=INK_2)

    for name, y in rows:
        d = seg[name]
        lo, hi, p_obs = bounds[name]
        ax.plot([lo, hi], [y, y], color=rose, linewidth=4.2, zorder=3,
                solid_capstyle="round")
        ax.plot([p_obs], [y], marker="o", markersize=7.0, color=rose,
                markeredgecolor=SURFACE, markeredgewidth=1.6, zorder=4)
        ax.text(p_obs, y + 0.15, pct(p_obs), ha="center", va="bottom",
                fontsize=9.6, color=INK, weight="600")
        ax.text(lo, y - 0.17, pct(lo), ha="center", va="top", fontsize=7.4,
                color=INK_3)
        ax.text(hi, y - 0.17, pct(hi), ha="center", va="top", fontsize=7.4,
                color=INK_3)
        ax.text(-3.0, y + 0.10, name, ha="right", va="bottom", fontsize=9.0,
                color=INK, weight="600")
        ax.text(-3.0, y - 0.06, f"{d['replies']} of {d['n']} replied",
                ha="right", va="top", fontsize=8.5, color=INK_2)

    ax.text((ov_lo + ov_hi) / 2, -0.62,
            f"the two intervals overlap\nbetween {pct(ov_lo)} and {pct(ov_hi)}",
            ha="center", va="center", fontsize=7.4, color=INK_3,
            style="italic", linespacing=1.5)

    ax.set_xlim(0, 100)
    ax.set_ylim(-0.95, 1.85)
    ax.set_yticks([])
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xticklabels(["0%", "25%", "50%", "75%", "100%"])
    style.bare_axes(ax)
    ax.spines["left"].set_visible(False)
    ax.tick_params(length=0)
    ax.set_xlabel("reply rate, with its 95% Wilson interval", fontsize=8.5,
                  labelpad=6)

    ax.text(0.5, -0.36,
            "Five of eight against one of nine is a wide gap, and it is what "
            "the advice is built on —\nbut on 17 applications the intervals "
            "still overlap: a direction to test, not a settled fact.",
            transform=ax.transAxes, ha="center", va="top", fontsize=7.6,
            color=INK_3, style="italic", linespacing=1.6)
    return style.save(fig, "fdd-09b-segments")


# --- fdd-11 -----------------------------------------------------------------


def fig_coverage():
    """Requirement coverage, with the remainder named rather than rounded off."""
    fig, ax = plt.subplots(figsize=(style.WIDTH, 3.0))
    fig.subplots_adjust(left=0.105, right=0.99, top=0.82, bottom=0.30)
    slate = PHASE[0]

    cov = facts.COVERAGE
    order = ["Must", "Should", "Could"]
    ys = [2, 1, 0]
    uncited_by_priority = {"M": "Must", "S": "Should", "C": "Could"}
    gap_label = {uncited_by_priority[p]: rid for rid, p, _t in cov["uncited"]}

    for name, y in zip(order, ys):
        cited, total = cov["by_priority"][name]
        ax.barh(y, cited, height=0.46, color=slate, edgecolor="none", zorder=3)
        if total > cited:
            ax.barh(y, total - cited, left=cited, height=0.46,
                    color=SURFACE, edgecolor=slate, linewidth=1.0,
                    linestyle=DASH, zorder=3)
            ax.plot([cited + (total - cited) / 2, cited + 1.5],
                    [y + 0.24, y + 0.44], color=INK_3, linewidth=0.8, zorder=2)
            ax.text(cited + 2.0, y + 0.46, f"{gap_label[name]} · not cited",
                    ha="left", va="center", fontsize=7.6, color=INK_2)
        ax.text(-2.0, y, name, ha="right", va="center", fontsize=9.0,
                color=INK, weight="600")
        ax.text(total + 2.5, y, f"{cited} of {total} cited", ha="left",
                va="center", fontsize=8.5, color=INK_2)

    ax.set_xlim(0, 132)
    ax.set_ylim(-0.5, 2.9)
    ax.set_yticks([])
    ax.set_xticks([0, 25, 50, 75, 100])
    style.bare_axes(ax)
    ax.spines["left"].set_visible(False)
    ax.tick_params(length=0)
    ax.set_xlabel("requirements", fontsize=8.5, labelpad=5)

    pct = 100 * cov["cited"] / cov["total"]
    ax.text(0, 2.72,
            f"{cov['cited']} of {cov['total']} requirements are cited in the "
            f"implementation — {pct:.1f}%, which is not all of them.",
            ha="left", va="center", fontsize=8.6, color=INK, weight="600")

    lines = []
    why = {
        "NFR-103": "measurable only against a real campaign",
        "NFR-304": "a document, delivered as DPIA.md",
    }
    rank = {"M": 0, "S": 1, "C": 2}
    for rid, prio, text in sorted(cov["uncited"], key=lambda r: rank[r[1]]):
        lines.append(f"{rid} ({uncited_by_priority[prio]})  ·  {text} — "
                     f"{why.get(rid, '')}")
    ax.text(0.0, -0.30, "The two not cited in code:\n" + "\n".join(lines),
            transform=ax.transAxes, ha="left", va="top", fontsize=7.6,
            color=INK_3, linespacing=1.7)
    return style.save(fig, "fdd-11-coverage")


def main():
    paths = [
        fig_scoring(),
        fig_sample_size(),
        fig_learning(),
        fig_segments(),
        fig_coverage(),
    ]
    for p in paths:
        print(p.name)


if __name__ == "__main__":
    main()
