"""Shared drawing style for the Dream Job design documents.

Every figure in the FDD and the TA is generated from code in this directory, so
the documents can be regenerated when the system changes rather than drifting
away from it.

The palette is the application's own: the five journey phases each own a point
on the spectrum, and a figure about the Apply phase is amber in the document for
the same reason it is amber on screen.  Nothing here invents a colour.

Conventions the figures follow, so twenty of them read as one set:

* 6.5 inches wide - the printable width of A4 with 25 mm margins - and never
  wider, so Word never has to rescale and blur a diagram.
* One idea per figure.  A diagram that needs a paragraph of explanation to be
  read is the wrong diagram.
* Text at 8.5 pt or larger at final size.  A figure that has to be zoomed is
  not carrying its weight.
* No chartjunk: no 3-D, no gradients on data, no gridlines competing with the
  data, axes only where a reader needs to estimate a value.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.patches as mpatches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

FIGURES = Path(__file__).resolve().parent
OUT = FIGURES / "out"
OUT.mkdir(exist_ok=True)

# --- The application palette ------------------------------------------------

PHASE = {
    1: "#6d54d8",   # Profile    violet
    2: "#2d6bc8",   # Plan       blue
    3: "#0b7b73",   # Discover   teal
    4: "#9f6011",   # Apply      amber
    5: "#bc3d66",   # Follow up  rose
    0: "#5f6672",   # System     slate
}

PHASE_SOFT = {
    1: "#f0ecfd",
    2: "#e9f1fd",
    3: "#e2f4f2",
    4: "#fcf1de",
    5: "#fdeaf0",
    0: "#f1f2f4",
}

PHASE_NAME = {
    1: "Profile",
    2: "Plan",
    3: "Discover",
    4: "Apply",
    5: "Follow up",
    0: "System",
}

# Speculative openings sit outside the five phases on purpose (FR-263).
SPECULATIVE = "#8b46c4"
SPECULATIVE_SOFT = "#f6ecfb"

INK = "#1a1a22"
INK_2 = "#4a4a58"
INK_3 = "#74748a"
LINE = "#d8d6e2"
LINE_SOFT = "#eceaf3"
SURFACE = "#ffffff"
SURFACE_2 = "#faf9fd"

OK = "#1f8a5a"
WARN = "#a8700d"
DANGER = "#c4344f"
ACCENT = "#4f57cf"

SPECTRUM = [PHASE[1], PHASE[2], PHASE[3], PHASE[4], PHASE[5]]

# --- Matplotlib defaults ----------------------------------------------------

plt.rcParams.update(
    {
        "font.family": "sans-serif",
        "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 9,
        "axes.titlesize": 10.5,
        "axes.titleweight": "600",
        "axes.labelsize": 9,
        "axes.edgecolor": LINE,
        "axes.labelcolor": INK_2,
        "axes.facecolor": SURFACE,
        "figure.facecolor": SURFACE,
        "text.color": INK,
        "xtick.color": INK_3,
        "ytick.color": INK_3,
        "xtick.labelsize": 8.5,
        "ytick.labelsize": 8.5,
        "legend.fontsize": 8.5,
        "legend.frameon": False,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.dpi": 200,
        "savefig.dpi": 200,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.06,
    }
)

WIDTH = 6.5           # inches; the printable width of A4 at 25 mm margins
WIDTH_NARROW = 4.6    # for a figure that sits beside text


def figure(width: float = WIDTH, height: float = 3.0):
    """A figure of standard width with no axes - the base for a diagram."""
    fig, ax = plt.subplots(figsize=(width, height))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")
    return fig, ax


def save(fig, name: str) -> Path:
    """Write a figure to ``out/<name>.png`` and return the path."""
    path = OUT / f"{name}.png"
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path


# --- Diagram primitives -----------------------------------------------------


def box(
    ax,
    x: float,
    y: float,
    w: float,
    h: float,
    label: str,
    *,
    colour: str = ACCENT,
    fill: str | None = None,
    text_colour: str | None = None,
    fontsize: float = 8.5,
    weight: str = "normal",
    radius: float = 1.6,
    lw: float = 1.1,
    align: str = "center",
    top_rule: bool = False,
):
    """A rounded box with centred text.

    ``top_rule`` draws a thicker coloured edge along the top only, which is how
    the application marks a card as belonging to a phase.
    """
    patch = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle=f"round,pad=0,rounding_size={radius}",
        linewidth=lw,
        edgecolor=colour,
        facecolor=fill if fill is not None else SURFACE,
        zorder=2,
    )
    ax.add_patch(patch)

    if top_rule:
        ax.plot(
            [x + radius * 0.4, x + w - radius * 0.4],
            [y + h, y + h],
            color=colour,
            linewidth=2.6,
            solid_capstyle="round",
            zorder=3,
        )

    tx = x + w / 2 if align == "center" else x + 2.2
    ha = "center" if align == "center" else "left"
    ax.text(
        tx,
        y + h / 2,
        label,
        ha=ha,
        va="center",
        fontsize=fontsize,
        color=text_colour or INK,
        weight=weight,
        zorder=4,
        linespacing=1.45,
    )
    return patch


def arrow(
    ax,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    colour: str = INK_3,
    lw: float = 1.1,
    style: str = "-|>",
    connection: str = "arc3,rad=0",
    dashed: bool = False,
):
    ax.add_patch(
        FancyArrowPatch(
            (x1, y1),
            (x2, y2),
            arrowstyle=style,
            mutation_scale=11,
            linewidth=lw,
            color=colour,
            connectionstyle=connection,
            linestyle="--" if dashed else "-",
            zorder=1,
        )
    )


def label(ax, x: float, y: float, text: str, *, size: float = 7.6,
          colour: str = INK_3, ha: str = "center", weight: str = "normal",
          style: str = "normal"):
    ax.text(x, y, text, ha=ha, va="center", fontsize=size, color=colour,
            weight=weight, style=style, linespacing=1.4)


def caption_band(ax, y: float, text: str):
    """A quiet strip of explanatory text under a diagram."""
    ax.text(50, y, text, ha="center", va="center", fontsize=7.4,
            color=INK_3, style="italic")


def spectrum_bar(ax, x: float, y: float, w: float, h: float = 1.1):
    """The five-phase spectrum as a continuous bar - the document's motif."""
    seg = w / len(SPECTRUM)
    for i, colour in enumerate(SPECTRUM):
        ax.add_patch(
            mpatches.Rectangle((x + i * seg, y), seg, h, facecolor=colour,
                               edgecolor="none", zorder=3)
        )


def legend_dots(ax, x: float, y: float, entries: list[tuple[str, str]],
                *, gap: float = 15.0, size: float = 7.4):
    """A compact inline legend: coloured dot, then label."""
    for i, (colour, text) in enumerate(entries):
        cx = x + i * gap
        ax.plot([cx], [y], marker="o", markersize=5, color=colour, zorder=4)
        ax.text(cx + 1.6, y, text, ha="left", va="center", fontsize=size, color=INK_2)


def bare_axes(ax, *, x=True, y=True):
    """Strip an axis back to the data."""
    if not x:
        ax.set_xticks([])
    if not y:
        ax.set_yticks([])
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color(LINE)
    ax.spines["bottom"].set_color(LINE)


def value_labels(ax, bars, values, *, fmt="{:.0f}", size=8.0, colour=INK_2,
                 offset=0.6, horizontal=False):
    """Print the number on each bar, so no one has to read it off an axis."""
    for bar, value in zip(bars, values, strict=False):
        if horizontal:
            ax.text(bar.get_width() + offset, bar.get_y() + bar.get_height() / 2,
                    fmt.format(value), va="center", ha="left", fontsize=size, color=colour)
        else:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + offset,
                    fmt.format(value), ha="center", va="bottom", fontsize=size, color=colour)
