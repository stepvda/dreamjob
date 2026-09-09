"""Figures about the shape of the product: the journey, intake, conflicts, jobs.

Four figures:

* ``fdd-01-journey``   the five-phase journey, the signature figure of both
  documents: each phase a column in its own hue, its three stages beneath it,
  and the spectrum running cool to warm with the stakes.
* ``fdd-02-intake``    profile intake - two documents in, a versioned profile
  out, with the photo branching off the CV towards the apply phase.
* ``fdd-03-conflicts`` the eighteen real conflicts, by what disagreed.
* ``ta-06-jobrunner``  the job state machine and where the checkpoint is taken.

Every number comes from ``facts.py``; every colour from ``style.py``.

    python3 fig_journey.py
"""

from __future__ import annotations

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt

import facts
import style
from style import INK, INK_2, INK_3, LINE, PHASE, PHASE_SOFT


def _full_bleed(fig):
    """Let the drawing use the whole canvas, so a tight box keeps 6.5 inches."""
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)


def _wrap(text: str, limit: int = 13) -> str:
    """Break a stage name onto two lines only when it needs it."""
    if len(text) <= limit or " " not in text:
        return text
    head, _, tail = text.rpartition(" ")
    return f"{head}\n{tail}"


# --- fdd-01 -----------------------------------------------------------------


def journey() -> None:
    """The five phases, their stages, and what the spectrum means."""
    fig, ax = style.figure(height=3.05)
    _full_bleed(fig)

    hues = {1: "violet", 2: "blue", 3: "teal", 4: "amber", 5: "rose"}

    x0, gap = 0.4, 4.0
    col_w = (99.6 - x0 - 4 * gap) / 5

    head_y, head_h = 82.0, 12.0
    card_h, card_gap = 13.0, 3.0
    card_top = head_y - card_gap

    for index, (phase, name, stages) in enumerate(facts.PHASES):
        x = x0 + index * (col_w + gap)
        colour = PHASE[phase]

        style.label(ax, x + col_w / 2, 97.6, f"Phase {phase}", size=7.2, colour=INK_3)

        style.box(ax, x, head_y, col_w, head_h, "", colour=colour, fill=colour,
                  radius=1.8, lw=0)
        ax.text(x + col_w / 2, head_y + head_h * 0.62, name.upper(), ha="center",
                va="center", fontsize=9.0, weight="bold", color=style.SURFACE, zorder=4)
        ax.text(x + col_w / 2, head_y + head_h * 0.26, hues[phase], ha="center",
                va="center", fontsize=7.2, color=style.SURFACE, alpha=0.88, zorder=4)

        for row, stage in enumerate(stages):
            y = card_top - (row + 1) * card_h - row * card_gap
            style.box(ax, x, y, col_w, card_h, _wrap(stage), colour=colour,
                      fill=PHASE_SOFT[phase], fontsize=8.5, text_colour=INK,
                      radius=1.6, lw=0.9)

        if index < 4:
            style.arrow(ax, x + col_w + 0.5, head_y + head_h / 2,
                        x + col_w + gap - 0.5, head_y + head_h / 2,
                        colour=INK_3, lw=1.3)

    bottom = card_top - 3 * card_h - 2 * card_gap

    bar_y = bottom - 8.5
    style.spectrum_bar(ax, x0, bar_y, 99.6 - x0, h=1.7)
    style.label(ax, x0, bar_y - 4.6, "cool: working out what you want",
                size=7.4, colour=INK_2, ha="left")
    style.label(ax, 99.6, bar_y - 4.6, "warm: where messages leave the building",
                size=7.4, colour=INK_2, ha="right")

    style.caption_band(
        ax, bar_y - 11.0,
        "A phase keeps its hue on every screen it owns. The spectrum runs cool to warm because the stakes do:\n"
        "the first phases only decide what you want, and nothing reaches a stranger until the warm end.",
    )

    ax.set_ylim(10.5, 100)
    style.save(fig, "fdd-01-journey")


# --- fdd-02 -----------------------------------------------------------------


def intake() -> None:
    """Two documents in, one versioned profile out - with the real counts."""
    fig, ax = style.figure(height=2.88)
    _full_bleed(fig)

    violet = PHASE[1]
    soft = PHASE_SOFT[1]
    real = facts.REAL_INTAKE

    src_x, src_w = 0.6, 17.0
    parse_x, parse_w = 23.6, 19.0
    merge_x, merge_w = 47.6, 12.0
    conf_x, conf_w = 64.6, 15.0
    prof_x, prof_w = 84.6, 15.0

    li_y, cv_y, doc_h = 74.0, 56.0, 13.0
    mid_y = (li_y + doc_h + cv_y) / 2          # 59.5, the centre of the pair
    band_h = 16.0
    band_y = mid_y - band_h / 2

    style.box(ax, src_x, li_y, src_w, doc_h, "LinkedIn\nexport (PDF)",
              colour=violet, fill=soft, lw=0.9)
    style.box(ax, src_x, cv_y, src_w, doc_h, "CV\n(DOCX or PDF)",
              colour=violet, fill=soft, lw=0.9)

    style.box(ax, parse_x, band_y, parse_w, band_h, "Deterministic\nparse",
              colour=violet, lw=1.1)
    style.box(ax, merge_x, band_y, merge_w, band_h, "Merge", colour=violet, lw=1.1)
    style.box(ax, conf_x, band_y, conf_w, band_h, "Conflicts\nsurfaced",
              colour=style.WARN, lw=1.1)
    style.box(ax, prof_x, band_y, prof_w, band_h, "Profile\nversion",
              colour=violet, fill=soft, lw=1.1, weight="bold")

    style.arrow(ax, src_x + src_w + 0.6, li_y + doc_h / 2,
                parse_x - 0.8, band_y + band_h * 0.72, colour=INK_3)
    style.arrow(ax, src_x + src_w + 0.6, cv_y + doc_h / 2,
                parse_x - 0.8, band_y + band_h * 0.30, colour=INK_3)
    for a, b in ((parse_x + parse_w, merge_x), (merge_x + merge_w, conf_x),
                 (conf_x + conf_w, prof_x)):
        style.arrow(ax, a + 0.6, mid_y, b - 0.8, mid_y, colour=INK_3)

    style.label(ax, conf_x + conf_w / 2, mid_y - band_h / 2 - 4.2,
                "the job seeker settles each one", size=7.2, colour=INK_3)

    # LLM only where the deterministic splitter gives up.
    style.label(ax, parse_x + parse_w / 2, band_y - 5.0,
                "LLM fallback only for passages\nthe splitter cannot segment",
                size=7.2, colour=INK_3, style="italic")

    # Measured counts, above the boxes they belong to.
    style.label(ax, parse_x + parse_w / 2, 91.5,
                f"{real['experience_entries']} experience entries\n"
                f"{real['top_skills']} top skills",
                size=7.4, colour=INK_2)
    style.label(ax, conf_x + conf_w / 2, 91.5,
                f"{real['conflicts']} conflicts\nto resolve", size=8.0,
                colour=style.WARN, weight="bold")
    style.label(ax, prof_x + prof_w / 2, 91.5,
                "every save\na new version", size=7.4, colour=INK_2)

    # The photo branches off the CV and is used later, in the apply phase.
    photo_y, photo_h = 29.0, 12.0
    style.arrow(ax, src_x + src_w / 2, cv_y - 0.6,
                src_x + src_w / 2, photo_y + photo_h + 0.8, colour=violet)
    style.box(ax, src_x, photo_y, src_w, photo_h, "Photo\nextracted",
              colour=violet, fill=soft, lw=0.9)
    style.box(ax, prof_x, photo_y, prof_w, photo_h, "Tailored CV\n(Apply)",
              colour=PHASE[4], lw=1.0)
    style.arrow(ax, src_x + src_w + 0.8, photo_y + photo_h / 2,
                prof_x - 0.8, photo_y + photo_h / 2, colour=PHASE[4], dashed=True)
    style.label(ax, (src_x + src_w + prof_x) / 2, photo_y + photo_h / 2 + 4.0,
                "carried through the profile to the document the apply phase generates",
                size=7.2, colour=PHASE[4])

    style.caption_band(
        ax, 9.5,
        "Counts measured on the product owner's own LinkedIn export and CV. "
        "Sources are retained and re-parsed on every merge,\nso the profile can be rebuilt "
        "from the originals when an extractor improves.",
    )

    ax.set_ylim(5.5, 100)
    style.save(fig, "fdd-02-intake")


# --- fdd-03 -----------------------------------------------------------------


def conflicts() -> None:
    """What actually disagreed between one real CV and one real LinkedIn export."""
    # Reversed then stably sorted, so equal counts keep the order of facts.py.
    kinds = sorted(reversed(list(facts.CONFLICT_KINDS.items())), key=lambda kv: kv[1])
    names = [k for k, _ in kinds]
    values = [v for _, v in kinds]
    total = sum(values)

    fig, ax = plt.subplots(figsize=(style.WIDTH, 3.0))
    bars = ax.barh(names, values, height=0.62, color=PHASE[1], edgecolor="none")
    style.value_labels(ax, bars, values, horizontal=True, offset=0.14,
                       size=8.5, colour=INK_2)

    ax.set_xlim(0, max(values) * 1.14)
    ax.set_xticks([])
    ax.tick_params(axis="y", length=0, labelsize=8.5, colors=INK)
    for spine in ("top", "right", "bottom", "left"):
        ax.spines[spine].set_visible(False)

    fig.subplots_adjust(left=0.165, right=0.985, top=0.735, bottom=0.275)

    fig.text(0.012, 0.975, "Two documents about the same person, and where they disagree",
             ha="left", va="top", fontsize=10.5, weight="bold", color=INK)
    fig.text(0.012, 0.895,
             f"All {total} conflicts raised by one real LinkedIn export and one real CV. "
             "Each is a genuine ambiguity between two\naccounts of the same career, not a "
             "parser error, and each is settled by the job seeker before anything uses it.",
             ha="left", va="top", fontsize=7.8, color=INK_3, linespacing=1.5)

    dates_li, dates_cv = facts.CONFLICT_EXAMPLES["role_dates"]
    emp_li, emp_cv = facts.CONFLICT_EXAMPLES["employer"]
    fig.text(0.012, 0.205,
             f"The largest group is role dates: the strategy role runs {dates_li} in "
             f"the export and {dates_cv} in the CV.\n"
             "The four employer-name conflicts are one employer written two ways: "
             f"\u201c{emp_li}\u201d in the export,\n"
             f"\u201c{emp_cv}\u201d in the CV.",
             ha="left", va="top", fontsize=7.3, color=INK_3, style="italic",
             linespacing=1.55)

    style.save(fig, "fdd-03-conflicts")


# --- ta-06 ------------------------------------------------------------------


def jobrunner() -> None:
    """The job state machine, and the barrier where a job can be stopped."""
    fig, ax = style.figure(height=3.07)
    _full_bleed(fig)

    slate, accent = PHASE[0], style.ACCENT

    pend = (8.0, 50.0, 21.0, 14.0)
    run = (42.0, 50.0, 21.0, 14.0)
    done = (77.0, 50.0, 20.0, 14.0)
    paused = (42.0, 77.0, 20.0, 12.0)
    cancelled = (25.0, 20.0, 21.0, 12.0)
    failed = (56.0, 20.0, 21.0, 12.0)

    style.box(ax, *pend, "pending", colour=slate, fill=PHASE_SOFT[0], weight="bold")
    style.box(ax, *run, "running", colour=accent, fill=style.SURFACE_2, weight="bold")
    style.box(ax, *done, "done", colour=style.OK, fill=style.SURFACE, weight="bold")
    style.box(ax, *paused, "paused", colour=style.WARN, weight="bold")
    style.box(ax, *cancelled, "cancelled", colour=slate, weight="bold")
    style.box(ax, *failed, "failed", colour=style.DANGER, weight="bold")

    y = run[1] + run[3] / 2

    style.arrow(ax, 1.6, y, pend[0] - 0.8, y, colour=INK_3)
    style.label(ax, 4.6, y + 4.8, "create()", size=7.2, colour=INK_3)

    style.arrow(ax, pend[0] + pend[2] + 0.6, y, run[0] - 0.8, y, colour=INK_3, lw=1.2)
    style.label(ax, (pend[0] + pend[2] + run[0]) / 2, y + 4.8, "start()",
                size=7.4, colour=INK_2)

    style.arrow(ax, run[0] + run[2] + 0.6, y, done[0] - 0.8, y, colour=style.OK, lw=1.2)
    style.label(ax, (run[0] + run[2] + done[0]) / 2, y - 5.0, "worker returns",
                size=7.4, colour=INK_2)

    # pause / resume, between running and paused
    style.arrow(ax, 48.0, run[1] + run[3] + 0.6, 48.0, paused[1] - 0.8, colour=style.WARN)
    style.arrow(ax, 56.0, paused[1] - 0.8, 56.0, run[1] + run[3] + 0.6, colour=style.WARN)
    style.label(ax, 46.8, 70.5, "pause", size=7.4, colour=INK_2, ha="right")
    style.label(ax, 57.2, 70.5, "resume", size=7.4, colour=INK_2, ha="left")

    # the two ways out of running
    style.arrow(ax, 46.5, run[1] - 0.6, 40.5, cancelled[1] + cancelled[3] + 0.8,
                colour=slate)
    style.label(ax, 37.5, 40.5, "cancel", size=7.4, colour=INK_2, ha="right")
    style.arrow(ax, 58.5, run[1] - 0.6, 64.0, failed[1] + failed[3] + 0.8,
                colour=style.DANGER)
    style.label(ax, 67.5, 40.5, "unhandled\nexception", size=7.4, colour=INK_2, ha="left")

    # a job can also be cancelled before a worker ever picks it up
    style.arrow(ax, 19.0, pend[1] - 0.6, 29.5, cancelled[1] + cancelled[3] + 0.8,
                colour=slate, lw=0.9, dashed=True)
    style.label(ax, 17.5, 39.0, "cancel before\nit starts", size=7.0, colour=INK_3,
                ha="right")

    # Whatever a restart interrupted - running or paused - comes back as pending.
    style.arrow(ax, run[0] - 0.2, run[1] + run[3] - 2.0,
                pend[0] + pend[2] - 8.0, pend[1] + pend[3] + 0.6,
                colour=accent, dashed=True, connection="arc3,rad=0.45", lw=1.0)
    style.label(ax, 20.0, 84.0,
                "interrupted by restart:\nrunning or paused jobs\nreturn to pending (NFR-401)",
                size=7.4, colour=accent)

    # where the checkpoint is written
    note_x, note_y, note_w, note_h = 67.0, 71.0, 32.0, 20.0
    ax.add_patch(
        mpatches.FancyBboxPatch(
            (note_x, note_y), note_w, note_h,
            boxstyle="round,pad=0,rounding_size=1.6",
            linewidth=0.9, edgecolor=LINE, facecolor=style.SURFACE_2, zorder=2,
        )
    )
    ax.text(note_x + note_w / 2, note_y + note_h * 0.76, "checkpoint_barrier()",
            ha="center", va="center", fontsize=8.2, color=accent,
            family="monospace", zorder=4)
    ax.text(note_x + note_w / 2, note_y + note_h * 0.34,
            "after every unit of work: the worker\nyields, saves its resume position,\n"
            "and honours pause, cancel and skip",
            ha="center", va="center", fontsize=7.4, color=INK_2, zorder=4,
            linespacing=1.5)
    style.arrow(ax, note_x + 1.0, note_y - 0.6, run[0] + run[2] - 1.5,
                run[1] + run[3] + 0.4, colour=LINE, style="-", lw=1.0)

    style.caption_band(
        ax, 8.0,
        "In-process asyncio on a single node: no broker, one writer. "
        "A crash loses at most the page in flight.",
    )

    ax.set_ylim(4.0, 100)
    style.save(fig, "ta-06-jobrunner")


def main() -> None:
    journey()
    intake()
    conflicts()
    jobrunner()


if __name__ == "__main__":
    main()
