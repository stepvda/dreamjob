"""The Dream Job mark: the spectrum as the route.

The idea
--------
A job search is not one leap.  It is a route: you get your bearings, you climb,
you traverse, you climb again, and the last stretch is still going up.  Dream
Job names those five stretches - Profile, Plan, Discover, Apply, Follow up - so
the mark is that route, walked from where you are to where you want to be.

The mark is one unbroken ribbon of constant width with five legs, and each leg
*is* a phase, in the application's own order, cool to warm along the walk:

    Profile violet -> Plan blue -> Discover teal -> Apply amber -> Follow up rose

The colour is not a gradient poured over a shape.  Every colour boundary falls
on a corner and nowhere else, so the five hues and the four turns are the same
five events: the route changes direction exactly when the phase changes.  Colour
is chosen by distance *along the path*, not by height or by x, which is why it
survives the corners cleanly and why it reads as a walk rather than as stripes.
The ribbon is also lit - deep underfoot, brightening as it climbs - so the
spectrum reads as light gathering at the destination.

The legs alternate ground and climb, which is the honest shape of the thing.
Profile is level: you are taking stock, not gaining height.  Plan is the first
rise.  Discover is a traverse - lots of movement, little altitude.  Apply is the
second and longer rise.  Follow up is the last leg, and it is the only one that
is neither flat nor vertical: it leaves the staircase tilted upward, because
following up is where the ground is still rising under you.

Direction without an arrow
--------------------------
A uniform staircase is symmetric under a half turn, so it can be read as a
descent just as easily as a climb.  This one cannot: the treads shorten
(0.300, 0.240, 0.205) while the risers grow (0.260, 0.310), the climb steepens
towards the top, and the light gathers there.  Nothing points; the shape simply
only makes sense one way round.

An earlier draft ended the route on a filled disc - a destination node.  It was
a bulb on the end of a long diagonal shaft and it read, unmistakably and
unfortunately, as something else entirely.  It is gone.

Why only two square turns
-------------------------
The first draft gave every phase its own tread *and* riser: ten turns.  At 32
pixels the notches were under two pixels and the whole thing silted up into a
diagonal smudge.  Two square turns and one tilt survive the same test with a
four-pixel notch, which ``main`` measures rather than asserts.  Clarity at 32 px
was the binding constraint on this design; everything else negotiated around it.

The geometry
------------
Nothing here is drawn freehand.  ``SPINE`` is six points; ``_centreline``
replaces each interior corner with a circular fillet of radius ``FILLET`` - arc
centre on the angle bisector at ``r / sin(theta/2)``, tangent points at
``r / tan(theta/2)`` along each leg - and returns a dense polyline with its
cumulative arc length.  The mark is then every point within ``STROKE / 2`` of
that centreline, which is a stroked path with round joins and round caps by
construction.  ``FILLET`` sits just above ``STROKE / 2``, so the outer corners
are round and the inner ones are nearly square: a stair, not a noodle.

The ribbon is rasterised from its signed distance field rather than filled as a
polygon, which gives an exact analytic anti-alias on every curve at every size.

Run ``python3 docs/figures/logo_path.py`` to write the four files.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))

from style import INK, PHASE, PHASE_NAME  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(parents=True, exist_ok=True)

# --- Colour -----------------------------------------------------------------
#
# A mark is not small text, so it can carry more chroma than the interface does.
# These are the same five hues as ``style.PHASE`` pushed brighter and cleaner,
# and they are identical to the ones the other Dream Job mark uses, because the
# two marks are one brand.  ``main`` prints them beside the interface values, so
# any drift stays visible rather than accumulating quietly.

LOGO_HUE = {
    1: "#7350ea",   # Profile    violet   (interface #6d54d8)
    2: "#2a78e8",   # Plan       blue     (interface #2d6bc8)
    3: "#06a595",   # Discover   teal     (interface #0b7b73)
    4: "#ef900f",   # Apply      amber    (interface #9f6011)
    5: "#eb3d6c",   # Follow up  rose     (interface #bc3d66)
}

PHASE_ORDER = [1, 2, 3, 4, 5]

BLEND = 0.016        # fraction of the walk each colour boundary is smeared over
LIFT_AT_END = 0.13   # how far the hue lifts towards white by the destination
DEEPEN_AT_START = 0.06

# --- Geometry ---------------------------------------------------------------
#
# The spine in its own units; ``_fit`` scales whatever comes out of it onto the
# canvas, so these numbers can be tuned for shape alone.  Five legs, one per
# phase: ground, climb, traverse, longer climb, and a final stretch that is
# still rising.  Treads shorten and risers grow, so the route accelerates.

SPINE = [
    (0.000, 0.000),   # Profile begins - the round cap, where you are
    (0.300, 0.000),   # Profile ends, Plan begins
    (0.300, 0.260),   # Plan ends, Discover begins
    (0.540, 0.260),   # Discover ends, Apply begins
    (0.540, 0.570),   # Apply ends, Follow up begins
    (0.745, 0.680),   # Follow up ends, still climbing
]

STROKE = 0.140       # ribbon width
FILLET = 0.078       # centreline corner radius; just over STROKE / 2
MARK_FILL = 0.93     # fraction of the canvas the mark spans
ARC_STEPS = 48       # segments per corner arc


def _hex(value: str) -> np.ndarray:
    value = value.lstrip("#")
    return np.array([int(value[i : i + 2], 16) / 255.0 for i in (0, 2, 4)], dtype=np.float32)


def _mix(colour: np.ndarray, target: float, amount) -> np.ndarray:
    return colour * (1.0 - amount) + target * amount


def _to_linear(srgb: np.ndarray) -> np.ndarray:
    """sRGB to linear light, where two colours may honestly be averaged."""
    return np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)


def _to_srgb(linear: np.ndarray) -> np.ndarray:
    linear = np.maximum(linear, 0.0)
    return np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055)


def _centreline() -> tuple[np.ndarray, np.ndarray, list[float]]:
    """The filleted spine as a dense polyline.

    Returns the points, their cumulative arc length, and the arc length at the
    middle of each corner - which is where one phase hands over to the next.
    """
    spine = [np.array(p, dtype=np.float64) for p in SPINE]
    points = [spine[0]]
    corner_index: list[int] = []

    for i in range(1, len(spine) - 1):
        vertex, before, after = spine[i], spine[i - 1], spine[i + 1]
        into = (before - vertex) / np.linalg.norm(before - vertex)
        out = (after - vertex) / np.linalg.norm(after - vertex)

        theta = float(np.arccos(np.clip(into @ out, -1.0, 1.0)))   # angle at the vertex
        tangent = FILLET / np.tan(theta / 2.0)                     # how far back to turn in
        tangent = min(
            tangent,
            0.5 * min(np.linalg.norm(vertex - before), np.linalg.norm(after - vertex)),
        )
        radius = tangent * np.tan(theta / 2.0)

        bisector = into + out
        bisector /= np.linalg.norm(bisector)
        centre = vertex + bisector * (radius / np.sin(theta / 2.0))

        start_point = vertex + into * tangent
        end_point = vertex + out * tangent
        start = float(np.arctan2(*(start_point - centre)[::-1]))
        end = float(np.arctan2(*(end_point - centre)[::-1]))
        if end - start > np.pi:
            end -= 2.0 * np.pi
        elif end - start < -np.pi:
            end += 2.0 * np.pi

        sweep = np.linspace(start, end, ARC_STEPS + 1)
        points.append(start_point)
        corner_index.append(len(points) + ARC_STEPS // 2 - 1)
        points.extend(centre + radius * np.column_stack([np.cos(sweep), np.sin(sweep)])[1:])

    points.append(spine[-1])
    pts = np.array(points, dtype=np.float32)
    step = np.hypot(*np.diff(pts, axis=0).T)
    arclen = np.concatenate([[0.0], np.cumsum(step)]).astype(np.float32)
    return pts, arclen, [float(arclen[i]) for i in corner_index]


def _fit(size: int):
    """World coordinates per pixel, scaled so the mark spans ``MARK_FILL``."""
    pts, arclen, corners = _centreline()
    lo = pts.min(axis=0) - STROKE / 2.0
    hi = pts.max(axis=0) + STROKE / 2.0
    extent = float(max(hi - lo)) / MARK_FILL
    centre = (lo + hi) / 2.0

    axis = np.linspace(-extent / 2.0, extent / 2.0, size, dtype=np.float32)
    return (
        centre[0] + axis[None, :],
        centre[1] - axis[:, None],          # image rows run downwards
        pts,
        arclen,
        corners,
        extent / size,
    )


def _fields(size: int):
    """Signed distance to the ribbon, and how far along the walk each pixel is.

    Walking the segments one at a time keeps this to a few full-size arrays
    however finely the corners are sampled.
    """
    x, y, pts, arclen, corners, pixel = _fit(size)

    best = np.full((size, size), np.inf, dtype=np.float32)
    walk = np.zeros((size, size), dtype=np.float32)

    for i in range(len(pts) - 1):
        start, end = pts[i], pts[i + 1]
        edge = end - start
        length2 = float(edge @ edge)
        if length2 == 0.0:
            continue
        along = np.clip(
            ((x - start[0]) * edge[0] + (y - start[1]) * edge[1]) / length2, 0.0, 1.0
        )
        distance = np.hypot(x - (start[0] + along * edge[0]), y - (start[1] + along * edge[1]))
        closer = distance < best
        best = np.where(closer, distance, best)
        walk = np.where(closer, arclen[i] + along * np.sqrt(length2), walk)

    total = float(arclen[-1])
    return best - STROKE / 2.0, walk / total, [c / total for c in corners], pixel


def _spectrum(walk: np.ndarray, corners: list[float], ink: str | None) -> np.ndarray:
    """Paint the walk: one hue per leg, handed over across each corner.

    A hand-over is two colours mixing, so it happens in linear light.  Blending
    sRGB numbers directly would drag every crossing towards mud - teal into
    amber goes qualmish olive - and the crossings are exactly where the eye
    looks.  The lift that follows is not a mix of two lights but a tint chosen
    by eye, so it stays in sRGB, where it was chosen.
    """
    if ink is not None:
        return np.broadcast_to(_hex(ink), (*walk.shape, 3)).copy()

    stops = [0.0]
    colours = [_to_linear(_hex(LOGO_HUE[PHASE_ORDER[0]]))]
    for corner, phase in zip(corners, PHASE_ORDER[1:], strict=True):
        stops += [corner - BLEND, corner + BLEND]
        colours += [colours[-1], _to_linear(_hex(LOGO_HUE[phase]))]
    stops.append(1.0)
    colours.append(colours[-1])

    painted = _to_srgb(
        np.dstack([np.interp(walk, stops, [c[channel] for c in colours]) for channel in range(3)])
    ).astype(np.float32)

    # The light gathers as the route climbs: deep underfoot, bright on arrival.
    lit = walk[..., None]
    painted = _mix(painted, 0.0, DEEPEN_AT_START * (1.0 - lit))
    return _mix(painted, 1.0, LIFT_AT_END * lit**1.15)


def render_mark(size: int, ink: str | None = None) -> Image.Image:
    """The mark on a transparent ground.  ``ink`` forces a single-colour version."""
    distance, walk, corners, pixel = _fields(size)
    alpha = np.clip(0.5 - distance / pixel, 0.0, 1.0)
    rgba = np.dstack([np.clip(_spectrum(walk, corners, ink), 0.0, 1.0), alpha])
    return Image.fromarray((rgba * 255.0 + 0.5).astype(np.uint8), "RGBA")


# --- Downsampling -----------------------------------------------------------


def resample(image: Image.Image, size: int) -> Image.Image:
    """Shrink the way a well-behaved renderer does: premultiplied, in linear light.

    Averaging straight sRGB values across an alpha edge darkens the edge and
    drags the transparent ground into the colour.  The 32 px file is meant to be
    evidence, so it is made the honest way.
    """
    source = np.asarray(image, dtype=np.float32) / 255.0
    alpha = source[..., 3:4]
    small = _box_resize(np.dstack([_to_linear(source[..., :3]) * alpha, alpha]), size)

    out_alpha = small[..., 3:4]
    out_srgb = _to_srgb(small[..., :3] / np.maximum(out_alpha, 1e-6))
    rgba = np.dstack([np.clip(out_srgb, 0.0, 1.0), np.clip(out_alpha, 0.0, 1.0)])
    return Image.fromarray((rgba * 255.0 + 0.5).astype(np.uint8), "RGBA")


def _box_resize(data: np.ndarray, size: int) -> np.ndarray:
    """Exact area average when the ratio divides, Lanczos when it does not."""
    height = data.shape[0]
    if height % size == 0:
        factor = height // size
        return data.reshape(size, factor, size, factor, data.shape[2]).mean(axis=(1, 3))
    return np.dstack(
        [
            np.asarray(
                Image.fromarray(data[..., c]).resize((size, size), Image.LANCZOS),
                dtype=np.float32,
            )
            for c in range(data.shape[2])
        ]
    )


# --- Wordmark ---------------------------------------------------------------

WORD_LIGHT = "Dream "
WORD_HEAVY = "Job"

FONT_CANDIDATES = [
    ("/System/Library/Fonts/Avenir Next.ttc", 7, 2),      # Regular, Demi Bold
    ("/System/Library/Fonts/HelveticaNeue.ttc", 0, 10),   # Regular, Medium
    ("/System/Library/Fonts/Supplemental/Arial.ttf", None, None),
]


def _fonts(size: int) -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    for path, light_index, heavy_index in FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        if light_index is None:
            face = ImageFont.truetype(path, size)
            return face, face
        return (
            ImageFont.truetype(path, size, index=light_index),
            ImageFont.truetype(path, size, index=heavy_index),
        )
    fallback = ImageFont.load_default()
    return fallback, fallback


def _draw_runs(draw: ImageDraw.ImageDraw | None, x: float, baseline: float, runs, tracking: float):
    """Set the wordmark letter by letter, so the tracking can be opened a little."""
    for text, font, colour in runs:
        for glyph in text:
            if draw is not None and glyph != " ":
                draw.text((x, baseline), glyph, font=font, fill=colour, anchor="ls")
            x += font.getlength(glyph) + tracking
    return x - tracking


def render_lockup(mark_size: int = 620) -> Image.Image:
    """Mark and wordmark, set so the ribbon's own height governs the type size.

    The mark is wider than it is tall, so measuring the type against the square
    canvas would leave the wordmark towering over it.  The ink is what the eye
    compares, so the type is sized against the ribbon's height instead.
    """
    mark = render_mark(mark_size)
    ink_height = int(np.count_nonzero(np.asarray(mark)[..., 3].any(axis=1)))

    font_size = int(ink_height * 0.66)
    light, heavy = _fonts(font_size)
    tracking = font_size * 0.004
    runs = [(WORD_LIGHT, light, INK), (WORD_HEAVY, heavy, INK)]

    text_width = _draw_runs(None, 0.0, 0.0, runs, tracking)
    _, cap_top, _, cap_bottom = heavy.getbbox("H")
    cap_height = cap_bottom - cap_top

    pad = int(mark_size * 0.05)
    gap = int(mark_size * 0.10)
    canvas = Image.new(
        "RGBA",
        (int(pad + mark_size + gap + text_width + pad), int(mark_size + 2 * pad)),
        (0, 0, 0, 0),
    )
    canvas.alpha_composite(mark, (pad, pad))
    _draw_runs(
        ImageDraw.Draw(canvas),
        pad + mark_size + gap,
        pad + mark_size / 2 + cap_height / 2,
        runs,
        tracking,
    )
    return canvas


# --- Build ------------------------------------------------------------------

FILES = (
    "logo-path.png",
    "logo-path-lockup.png",
    "logo-path-32.png",
    "logo-path-mono.png",
)

ICON = 32


def main() -> None:
    master = render_mark(1024)
    master.save(OUT / "logo-path.png")
    render_lockup().save(OUT / "logo-path-lockup.png")
    resample(master, ICON).save(OUT / "logo-path-32.png")
    render_mark(1024, ink=INK).save(OUT / "logo-path-mono.png")

    distance, walk, corners, pixel = _fields(1024)
    alpha = np.clip(0.5 - distance / pixel, 0.0, 1.0)
    edges = [0.0, *corners, 1.0]
    total = float(alpha.sum())
    print("Each leg of the route is one phase, and each phase owns its share of the ink:")
    for i, phase in enumerate(PHASE_ORDER):
        share = float(alpha[(walk >= edges[i]) & (walk < edges[i + 1])].sum()) / total
        print(
            f"  leg {i + 1}  {PHASE_NAME[phase]:<10}"
            f"  interface {PHASE[phase]} -> logo {LOGO_HUE[phase]}"
            f"  {share * 100:5.2f}%"
        )

    # The 32 px test, stated as a number: the clear gap between the underside of
    # one tread and the top of the one below it, in pixels, at icon size.
    per_pixel = _fit(ICON)[5]
    risers = [SPINE[2][1] - SPINE[1][1], SPINE[4][1] - SPINE[3][1]]
    notch = (min(risers) - STROKE) / per_pixel
    print(f"\nAt {ICON} px the shallowest notch between steps is {notch:.1f} px clear.")

    for name in FILES:
        with Image.open(OUT / name) as image:
            band = np.asarray(image)[..., 3]
            print(
                f"  {name}: {image.size[0]}x{image.size[1]} {image.mode}, "
                f"{(band == 0).mean() * 100:.1f}% fully transparent"
            )


if __name__ == "__main__":
    main()
