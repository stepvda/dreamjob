"""The Dream Job mark: the spectrum as an aperture.

The idea
--------
A job market arrives wide, bright and undifferentiated.  Dream Job's whole
purpose is to narrow it - through Profile, Plan, Discover, Apply and Follow up -
until it converges on the one right role.  That is an aperture: five blades,
turning together, closing a wide circle down to a single clear opening.

So the mark is a real five-blade iris, and the five blades are the five phases.
The colour is not decoration laid over a shape; each blade *is* a phase, in the
application's own order, cool to warm, running clockwise from the top:

    Profile violet -> Plan blue -> Discover teal -> Apply amber -> Follow up rose

Each blade is lit from within: its hue is deepest out at the rim, where the
market is wide, and brightest where it reaches the opening.  The light gathers
at the point of convergence.  The opening itself is left empty - a real
aperture, not a drawn dot - which is what lets the mark sit on any ground, work
in one ink, and stay legible at 32 pixels.

The geometry
------------
Nothing here is freehand.  Let ``n_k`` be five unit normals 72 degrees apart.
Each blade's inner edge is an arc of a circle of radius ``BLADE_ARC`` centred at
``-(BLADE_ARC - APERTURE) * n_k``, so every arc passes at distance ``APERTURE``
from the centre and the five of them cut a curved pentagon out of the middle.
Blade ``k`` is then

    inside the rim   AND   outside its own arc   AND   inside its neighbour's

which is exactly how physical iris blades stack: each one tucks under the next,
all five turning the same way.  By five-fold symmetry the blades are congruent
and tile the disc, so every phase owns precisely one fifth of the mark - which
``main`` checks rather than assumes.

``BLADE_ARC`` is deliberately large.  A small radius curls the blades into
petals and the mark becomes a flower; a large one keeps them nearly straight and
the mark stays an instrument.

Shapes are rasterised from their signed distance fields rather than filled as
polygons, which gives an exact analytic anti-alias on every curve at every size.

Run ``python docs/figures/logo_aperture.py`` to write the four files.
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

LIGHTEN_AT_OPENING = 0.22   # how far the hue lifts as it reaches the focus
DEEPEN_AT_RIM = 0.07        # how far it settles out at the wide edge

# --- Geometry ---------------------------------------------------------------

RIM = 1.0            # radius of the mark
APERTURE = 0.27      # distance from the centre to each blade's inner edge
BLADE_ARC = 1.25     # radius of the arc a blade edge rides on; larger = straighter
BLADE_GAP = 0.026    # the hairline of light between one blade and the next
MARK_FILL = 0.94     # fraction of the canvas the mark spans
TURN = 12.0          # whole-mark rotation, so one blade crowns it at twelve o'clock

FIRST_BLADE_AT = 90.0    # Profile takes the top
CLOCKWISE = True         # and the journey runs clockwise from there


def _hex(value: str) -> np.ndarray:
    value = value.lstrip("#")
    return np.array([int(value[i : i + 2], 16) / 255.0 for i in (0, 2, 4)], dtype=np.float32)


def _mix(colour: np.ndarray, target: float, amount: float) -> np.ndarray:
    return colour * (1.0 - amount) + target * amount


def _grid(size: int):
    """World coordinates for every pixel, plus the size of one pixel."""
    half = RIM / MARK_FILL
    x, y = np.meshgrid(
        np.linspace(-half, half, size, dtype=np.float32),
        np.linspace(half, -half, size, dtype=np.float32),
    )
    return x, y, np.hypot(x, y), 2.0 * half / size


def _blade_arc_centres() -> list[tuple[float, float]]:
    offset = BLADE_ARC - APERTURE
    centres = []
    for k in range(5):
        beta = np.deg2rad(TURN + k * 72.0)
        centres.append((-offset * float(np.cos(beta)), -offset * float(np.sin(beta))))
    return centres


def _blades(size: int) -> list[np.ndarray]:
    """Anti-aliased coverage for each of the five blades.

    Coverage is the signed distance to the blade's boundary expressed in pixels
    and clipped to 0..1: one pixel of feather, no jaggies, at any size.
    """
    x, y, radius, pixel = _grid(size)
    centres = _blade_arc_centres()
    to_arc = [np.hypot(x - cx, y - cy) for cx, cy in centres]

    coverage = []
    for k in range(5):
        signed = np.minimum(RIM - radius, to_arc[k] - BLADE_ARC)
        signed = np.minimum(signed, (BLADE_ARC - BLADE_GAP) - to_arc[(k + 1) % 5])
        coverage.append(np.clip(signed / pixel + 0.5, 0.0, 1.0))
    return coverage


def blade_phases(size: int = 512) -> list[int]:
    """Which phase belongs on which blade, so the journey reads from the top.

    The blades are built by construction, not by aim, so rather than working out
    which index landed where, measure each blade's centroid and hand the phases
    out around the circle in the direction the journey runs.
    """
    x, y, _, _ = _grid(size)
    bearings = []
    for k, alpha in enumerate(_blades(size)):
        weight = alpha.sum()
        cx = float((x * alpha).sum() / weight)
        cy = float((y * alpha).sum() / weight)
        bearings.append((k, float(np.degrees(np.arctan2(cy, cx))) % 360.0))

    step = -72.0 if CLOCKWISE else 72.0
    mapping: dict[int, int] = {}
    for position, phase in enumerate(PHASE_ORDER):
        wanted = (FIRST_BLADE_AT + position * step) % 360.0
        blade, _ = min(bearings, key=lambda b: abs((b[1] - wanted + 180.0) % 360.0 - 180.0))
        mapping[blade] = phase
    return [mapping[k] for k in range(5)]


def render_mark(size: int, ink: str | None = None) -> Image.Image:
    """The mark on a transparent ground.  ``ink`` forces a single-colour version."""
    coverage = _blades(size)
    phases = blade_phases()
    _, _, radius, _ = _grid(size)

    # Travel outward from the opening: 0 at the focus, 1 at the rim.
    travel = (np.clip((radius - APERTURE) / (RIM - APERTURE), 0.0, 1.0) ** 0.85)[..., None]

    accumulated = np.zeros((size, size, 3), dtype=np.float32)
    alpha = np.zeros((size, size), dtype=np.float32)

    for blade, phase in zip(coverage, phases, strict=True):
        if ink is None:
            base = _hex(LOGO_HUE[phase])
            at_focus = _mix(base, 1.0, LIGHTEN_AT_OPENING)[None, None, :]
            at_rim = _mix(base, 0.0, DEEPEN_AT_RIM)[None, None, :]
            paint = at_focus * (1.0 - travel) + at_rim * travel
        else:
            paint = _hex(ink)[None, None, :]
        accumulated += paint * blade[..., None]
        alpha += blade

    alpha = np.clip(alpha, 0.0, 1.0)
    straight = accumulated / np.maximum(alpha, 1e-6)[..., None]
    rgba = np.dstack([np.clip(straight, 0.0, 1.0), alpha])
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
    out_srgb = np.where(
        out_linear <= 0.0031308,
        out_linear * 12.92,
        1.055 * out_linear ** (1 / 2.4) - 0.055,
    )
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
    needs no reversed version, which is the point of leaving the aperture open.
    """
    mark = render_mark(mark_size)

    font_size = int(mark_size * 0.55)
    light, heavy = _fonts(font_size)
    tracking = font_size * TRACKING
    runs = [(WORD_LIGHT, light, ink), (WORD_HEAVY, heavy, ink)]

    text_width = _draw_runs(None, 0.0, 0.0, runs, tracking)
    _, cap_top, _, cap_bottom = heavy.getbbox("H")
    cap_height = cap_bottom - cap_top

    pad = int(mark_size * 0.09)
    gap = int(mark_size * 0.18)
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
    "logo-aperture.png",
    "logo-aperture-lockup.png",
    "logo-aperture-32.png",
    "logo-aperture-mono.png",
)


def main() -> None:
    master = render_mark(1024)
    master.save(OUT / "logo-aperture.png")
    render_lockup().save(OUT / "logo-aperture-lockup.png")
    resample(master, 32).save(OUT / "logo-aperture-32.png")
    render_mark(1024, ink=INK).save(OUT / "logo-aperture-mono.png")

    areas = [float(blade.sum()) for blade in _blades(1024)]
    total = sum(areas)
    print("Each blade is one phase, and each phase gets an equal share of the mark:")
    for blade, phase in enumerate(blade_phases()):
        print(
            f"  blade {blade}  {PHASE_NAME[phase]:<10}"
            f"  interface {PHASE[phase]} -> logo {LOGO_HUE[phase]}"
            f"  {areas[blade] / total * 100:5.2f}%"
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
