"""The Dream Job mark: the spectrum as a guiding star.

The idea
--------
A dream-job statement is a bearing, not a wish.  Profile, Plan, Discover, Apply
and Follow up are all steering by it, which is why the mark is a star and not a
badge: a fixed point you navigate by.

The spectrum is not painted onto that star - it *draws* it.  The mark is one
unbroken stroke that travels round a five-pointed star, and its colour travels
with it, cool to warm, clockwise from the top point:

    Profile violet -> Plan blue -> Discover teal -> Apply amber -> Follow up rose

Each phase draws exactly one point of the star.  The hand-over from one hue to
the next happens down in the valleys, where the stroke turns - so a point of the
star is one phase's own colour, and the turn between two points is where the
work is handed on.  The stroke closes where it began: Follow up runs back into
Profile, because the journey does.

That is what the colour is for.  Take a phase out and the star loses a point.

The geometry
------------
Nothing here is freehand.  Ten vertices - five at radius ``1``, five at
``COUNTER`` - give the star polygon ``P``.  Every edge of ``P`` is then pushed
inward by exactly ``STROKE`` and the pushed edges are intersected in pairs,
which is a true mitre: the counter ``Q`` it produces has sharp points too, and
the ribbon between ``P`` and ``Q`` is the same weight the whole way round.  A
distance-field offset would have rounded the points into a rating star; mitring
keeps them the sharp points of a star on a chart.  Every edge of this star is
the same distance from the centre, so the counter comes back a smaller copy of
the star itself rather than some other shape - ``main`` checks that, because if
the mitre ever ate the mark that is where it would show.

The mark is drawn from the signed distance to those two polygons, which gives an
exact analytic anti-alias on every edge at every size, and it is hung on the
bounding box of the star rather than on the origin - a five-pointed star has one
point up and two down, so centring it on its own centre leaves it floating high.

Colour is interpolated in OKLab.  A straight line between two sRGB values dives
towards grey in the middle, which would turn the teal-to-amber hand-over into
olive sludge; OKLab keeps the light in it, and the hand-over is lifted a little
besides, so the turns read as light rather than as mud.

Run ``python3 docs/figures/logo_star.py`` to write the four files.
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
# These are the same five hues as ``style.PHASE``, pushed brighter and cleaner;
# the identity has to survive the trip, so ``main`` prints both side by side.

LOGO_HUE = {
    1: "#7350ea",   # Profile    violet   (interface #6d54d8)
    2: "#2a78e8",   # Plan       blue     (interface #2d6bc8)
    3: "#06a595",   # Discover   teal     (interface #0b7b73)
    4: "#ef900f",   # Apply      amber    (interface #9f6011)
    5: "#eb3d6c",   # Follow up  rose     (interface #bc3d66)
}

PHASE_ORDER = [1, 2, 3, 4, 5]

HAND_OVER = 0.14    # share of a phase's 72 degrees spent turning into the next
TURN_LIFT = 0.030   # how much light the hand-over keeps, in OKLab L

# --- Geometry ---------------------------------------------------------------

POINTS = 5
COUNTER = 0.42       # radius of the star's inner vertices; 1.0 is a point
STROKE = 0.185       # weight of the ribbon, mitred, in the same units
MARK_FILL = 0.94     # fraction of the canvas the mark spans

FIRST_POINT_AT = 90.0   # Profile takes the top point
CLOCKWISE = True        # and the journey runs clockwise from there

STEP = -360.0 / POINTS if CLOCKWISE else 360.0 / POINTS


def star_polygon(counter: float = COUNTER) -> list[np.ndarray]:
    """The ten vertices of the star, point then valley, in journey order."""
    vertices = []
    for k in range(POINTS):
        tip = np.deg2rad(FIRST_POINT_AT + k * STEP)
        valley = np.deg2rad(FIRST_POINT_AT + (k + 0.5) * STEP)
        vertices.append(np.array([np.cos(tip), np.sin(tip)]))
        vertices.append(counter * np.array([np.cos(valley), np.sin(valley)]))
    return vertices


def mitred_inset(vertices: list[np.ndarray], weight: float) -> list[np.ndarray]:
    """Every edge pushed inward by ``weight``, then intersected in pairs.

    This is what a pen does, and what a distance field does not: the corners
    stay corners.  The origin is inside a star polygon, which is what decides
    which side of each edge is 'inward'.
    """
    count = len(vertices)
    lines = []
    for j in range(count):
        start, end = vertices[j], vertices[(j + 1) % count]
        direction = end - start
        normal = np.array([-direction[1], direction[0]]) / np.hypot(*direction)
        if normal @ (-(start + end) / 2.0) < 0.0:
            normal = -normal
        lines.append((normal, normal @ start + weight))

    inset = []
    for j in range(count):
        (n1, c1), (n2, c2) = lines[(j - 1) % count], lines[j]
        inset.append(np.linalg.solve(np.array([n1, n2]), np.array([c1, c2])))
    return inset


def _polygon_field(x: np.ndarray, y: np.ndarray, vertices: list[np.ndarray]) -> np.ndarray:
    """Signed distance to a simple polygon, positive inside."""
    count = len(vertices)
    distance = np.full(x.shape, np.inf, dtype=np.float32)
    inside = np.zeros(x.shape, dtype=bool)
    for j in range(count):
        start, end = vertices[j], vertices[(j + 1) % count]
        edge = end - start
        travel = np.clip(
            ((x - start[0]) * edge[0] + (y - start[1]) * edge[1]) / (edge @ edge), 0.0, 1.0
        )
        distance = np.minimum(
            distance, np.hypot(x - start[0] - travel * edge[0], y - start[1] - travel * edge[1])
        )
        crosses = (start[1] > y) != (end[1] > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            at = (end[0] - start[0]) * (y - start[1]) / (end[1] - start[1]) + start[0]
        inside ^= crosses & (x < at)
    return np.where(inside, distance, -distance)


def ribbon_field(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """The stroke itself: inside the star, outside its mitred counter."""
    outline = star_polygon()
    return np.minimum(
        _polygon_field(x, y, outline), -_polygon_field(x, y, mitred_inset(outline, STROKE))
    )


def _bounds() -> tuple[float, float]:
    """Optical centre and half-extent of the mark, from the star's own corners."""
    outline = star_polygon()
    xs = np.array([v[0] for v in outline])
    ys = np.array([v[1] for v in outline])
    centre_y = float((ys.max() + ys.min()) / 2.0)
    extent = max(float(np.abs(xs).max()), float((ys.max() - ys.min()) / 2.0))
    return centre_y, extent


def _grid(size: int):
    """World coordinates for every pixel, plus the size of one pixel."""
    centre_y, extent = _bounds()
    half = extent / MARK_FILL
    x, y = np.meshgrid(
        np.linspace(-half, half, size, dtype=np.float32),
        np.linspace(centre_y + half, centre_y - half, size, dtype=np.float32),
    )
    return x, y, 2.0 * half / size


# --- OKLab ------------------------------------------------------------------

_LMS = np.array(
    [
        [0.4122214708, 0.5363325363, 0.0514459929],
        [0.2119034982, 0.6806995451, 0.1073969566],
        [0.0883024619, 0.2817188376, 0.6299787005],
    ]
)
_LAB = np.array(
    [
        [0.2104542553, 0.7936177850, -0.0040720468],
        [1.9779984951, -2.4285922050, 0.4505937099],
        [0.0259040371, 0.7827717662, -0.8086757660],
    ]
)
_LMS_INV = np.linalg.inv(_LMS)
_LAB_INV = np.linalg.inv(_LAB)


def _hex_to_linear(value: str) -> np.ndarray:
    value = value.lstrip("#")
    srgb = np.array([int(value[i : i + 2], 16) / 255.0 for i in (0, 2, 4)])
    return np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)


def _linear_to_oklab(linear: np.ndarray) -> np.ndarray:
    return np.cbrt(linear @ _LMS.T) @ _LAB.T


def _oklab_to_linear(lab: np.ndarray) -> np.ndarray:
    return (lab @ _LAB_INV.T) ** 3 @ _LMS_INV.T


def _linear_to_srgb(linear: np.ndarray) -> np.ndarray:
    linear = np.clip(linear, 0.0, 1.0)
    return np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055)


def phase_at(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Which phase a pixel belongs to, and how far it has turned into the next.

    Bearings are measured from the middle of Profile's point, so the seams fall
    in the valleys - where the stroke turns - and never across a point.
    """
    bearing = np.degrees(np.arctan2(y, x))
    swing = (FIRST_POINT_AT - bearing) if CLOCKWISE else (bearing - FIRST_POINT_AT)
    turned = ((swing + 36.0) % 360.0) / 72.0
    index = np.floor(turned).astype(np.int64) % POINTS
    frac = turned - np.floor(turned)
    edge = np.clip((frac - 0.5) / max(HAND_OVER, 1e-6) + 0.5, 0.0, 1.0)
    return index, edge * edge * (3.0 - 2.0 * edge)


def render_mark(size: int, ink: str | None = None) -> Image.Image:
    """The mark on a transparent ground.  ``ink`` forces a single-colour version."""
    x, y, pixel = _grid(size)
    alpha = np.clip(ribbon_field(x, y) / pixel + 0.5, 0.0, 1.0)

    if ink is None:
        palette = _linear_to_oklab(np.array([_hex_to_linear(LOGO_HUE[p]) for p in PHASE_ORDER]))
        index, turn = phase_at(x, y)
        blend = turn[..., None]
        lab = palette[index] * (1.0 - blend) + palette[(index + 1) % POINTS] * blend
        lab[..., 0] += TURN_LIFT * (4.0 * turn * (1.0 - turn))
        rgb = _linear_to_srgb(_oklab_to_linear(lab))
    else:
        rgb = np.broadcast_to(_linear_to_srgb(_hex_to_linear(ink)), (size, size, 3))

    rgba = np.dstack([np.clip(rgb, 0.0, 1.0), alpha])
    return Image.fromarray((rgba * 255.0 + 0.5).astype(np.uint8), "RGBA")


# --- Downsampling -----------------------------------------------------------


def resample(image: Image.Image, size: int) -> Image.Image:
    """Shrink the way a well-behaved renderer does: premultiplied, in linear light.

    Averaging straight sRGB values across an alpha edge darkens the edge and
    drags the transparent ground into the colour.  The 32 px file is meant to be
    evidence, so it is made the honest way.
    """
    source = np.asarray(image, dtype=np.float32) / 255.0
    linear = np.where(
        source[..., :3] <= 0.04045,
        source[..., :3] / 12.92,
        ((source[..., :3] + 0.055) / 1.055) ** 2.4,
    )
    alpha = source[..., 3:4]
    small = _box_resize(np.dstack([linear * alpha, alpha]), size)

    out_alpha = small[..., 3:4]
    out_linear = np.maximum(small[..., :3] / np.maximum(out_alpha, 1e-6), 0.0)
    rgba = np.dstack([_linear_to_srgb(out_linear), np.clip(out_alpha, 0.0, 1.0)])
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
WORD_SPACE = 0.80    # the gap between the two words, as a fraction of a normal space
TRACKING = 0.004     # a little air between letters, in ems

# A weight lighter than Medium goes spindly the moment the lockup is reduced, so
# the pairing is Medium against Demi Bold: the contrast still reads at 260 px.
FONT_CANDIDATES = [
    ("/System/Library/Fonts/Avenir Next.ttc", 5, 2),      # Medium, Demi Bold
    ("/System/Library/Fonts/HelveticaNeue.ttc", 0, 1),    # Regular, Bold
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
            if glyph == " ":
                x += font.getlength(" ") * WORD_SPACE
                continue
            if draw is not None:
                draw.text((x, baseline), glyph, font=font, fill=colour, anchor="ls")
            x += font.getlength(glyph) + tracking
    return x - tracking


def render_lockup(mark_size: int = 620, ink: str = INK) -> Image.Image:
    """Mark and wordmark, on a transparent ground.

    The wordmark is set in dark ink, so the file that ships is the light-ground
    lockup.  For a dark ground, call this with ``ink="#ffffff"``; the mark itself
    needs no reversed version - it is a line of light, and a light reads on
    either ground.
    """
    mark = render_mark(mark_size)

    font_size = int(mark_size * 0.47)
    light, heavy = _fonts(font_size)
    tracking = font_size * TRACKING
    runs = [(WORD_LIGHT, light, ink), (WORD_HEAVY, heavy, ink)]

    text_width = _draw_runs(None, 0.0, 0.0, runs, tracking)
    _, cap_top, _, cap_bottom = heavy.getbbox("H")
    cap_height = cap_bottom - cap_top

    pad = int(mark_size * 0.08)
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
    "logo-star.png",
    "logo-star-lockup.png",
    "logo-star-32.png",
    "logo-star-mono.png",
)


def main() -> None:
    master = render_mark(1024)
    master.save(OUT / "logo-star.png")
    render_lockup().save(OUT / "logo-star-lockup.png")
    resample(master, 32).save(OUT / "logo-star-32.png")
    render_mark(1024, ink=INK).save(OUT / "logo-star-mono.png")

    # The counter has to stay a star, or the mitre has eaten the mark.
    counter = mitred_inset(star_polygon(), STROKE)
    radii = np.hypot([v[0] for v in counter], [v[1] for v in counter])
    points, valleys = radii[0::2].mean(), radii[1::2].mean()
    print(
        f"Counter: points at {points:.3f}, valleys at {valleys:.3f}, so the same star at "
        f"{valleys / points:.3f} against {COUNTER:.3f} - the stroke is {STROKE:.3f} all round"
    )

    _, _, pixel_at_32 = _grid(32)
    print(f"Stroke at 32 px: {STROKE / pixel_at_32:.2f} px  (the mark's thinnest part)")

    x, y, pixel = _grid(1024)
    alpha = np.clip(ribbon_field(x, y) / pixel + 0.5, 0.0, 1.0)
    index, _ = phase_at(x, y)
    total = alpha.sum()
    print("Each phase draws one point of the star, and they share it evenly:")
    for j, phase in enumerate(PHASE_ORDER):
        share = alpha[index == j].sum() / total * 100.0
        print(
            f"  point {j} at {(FIRST_POINT_AT + j * STEP) % 360:5.0f} deg"
            f"  {PHASE_NAME[phase]:<10}"
            f"  interface {PHASE[phase]} -> logo {LOGO_HUE[phase]}"
            f"  {share:5.2f}%"
        )
    for name in FILES:
        with Image.open(OUT / name) as image:
            band = np.asarray(image)[..., 3]
            print(
                f"  {name}: {image.size[0]}x{image.size[1]} {image.mode}, "
                f"{(band == 0).mean() * 100:.1f}% fully transparent"
            )


if __name__ == "__main__":
    main()
