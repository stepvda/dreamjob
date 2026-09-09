"""The Dream Job mark: DJ, where the spectrum *is* the stroke.

The idea
--------
Dream Job is a journey with five phases, and the word for a journey drawn by
hand is a *stroke*.  So the mark is a monogram - D and J, for the two words -
built as a single monoline of constant weight, and the five phases are five
measures of that line.  The colour is not laid over the letters; it is what the
line is made of, and it changes with how far the pen has travelled:

    Profile violet -> Plan blue -> Discover teal -> Apply amber -> Follow up rose

The pen touches down at the top-left of the D.  Both of that letter's strokes -
the stem and the bowl - run from that corner to the foot, so the D is violet at
the head and teal at the foot whichever way you trace it, and there is no seam
where they meet.  The D spends the first half of the spectrum; the J picks the
line up and spends the second.  They change hands at teal, which is Discover -
the phase where a dream stops being a dream and starts being a job.  Cool while
you are preparing, warm while you are acting.

That mapping is the whole point.  A gradient poured over a shape is a template.
Here the colour answers "how far along are you?", which is the question the
application exists to answer, and you can see it working: the bowl of the D
leaves the top-left violet, passes blue at its widest, and comes back to the
stem teal.  No straight-line gradient can do that, because the bowl's two ends
are the same height and different distances.

The letterforms
---------------
Constructed, not typed.  The D is a stem plus a bowl whose curve is a true
semicircle of radius half the cap height, joined by a short flat at the head and
the foot - the geometric D of Futura and its descendants.  The J is a stem and a
circular hook that descends below the baseline and turns back under the D's
bowl, which is what locks two letters into one mark instead of leaving them
neighbours.  Every terminal and every corner is rounded by exactly half a stroke
width, because that is the radius the pen already has; the mark has one radius
throughout and no other.

Stroke weight is 17% of the cap height: heavy enough to hold five colours at 32
pixels, light enough to leave the D's counter open at the same size.  The two
places the mark could close up on itself - the side gap between the D's shoulder
and the J's stem, and the vertical gap between the D's foot and the hook's tip -
are measured by ``clearances`` rather than eyeballed, and reported in pixels at
32 px, where they have to survive.

Both letters are the set of points within half a stroke width of a centreline
made of straight segments and circular arcs, so the shape is a signed distance
field and its edges are antialiased from the true distance at any size - no
jaggies at 1024, no mush at 32.  Colour is interpolated in Oklab and held still at each
anchor, so the five hues stay nameable and hand over without banding.

Run ``python docs/figures/logo_wordmark.py`` to write the four files.
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
# The interface hues are tuned to carry 4.5:1 against white at 14 px.  A mark is
# not small text, so it can afford the chroma the interface has to spend on
# legibility.  Same five hues, same order, pushed brighter; ``main`` prints both
# columns so the drift stays a decision rather than an accident.

LOGO_HUE = {
    1: "#7a53f0",   # Profile    violet   (interface #6d54d8)
    2: "#2a7bee",   # Plan       blue     (interface #2d6bc8)
    3: "#00b3a2",   # Discover   teal     (interface #0b7b73)
    4: "#ee9412",   # Apply      amber    (interface #9f6011)
    5: "#ec3f70",   # Follow up  rose     (interface #bc3d66)
}
PHASE_ORDER = [1, 2, 3, 4, 5]

# --- Geometry, in cap-height units ------------------------------------------
#
# One unit is the cap height measured on the centrelines; the drawn letter is a
# stroke width taller and wider than that.  Everything below is a ratio, so the
# 32 px file is the same drawing as the 1024 px one rather than a shrunk copy.

HW = 0.085                          # half the stroke: the line is 17% of the cap
D_FLAT = 0.20                       # the flat at the head and foot of the bowl
D_R = 0.50                          # the bowl is a true semicircle
J_X = 0.985                         # the J's stem, kerned to the D's shoulder
J_HOOK_R = 0.235                    # the hook's radius
J_HOOK_Y = -0.05                    # the hook's centre: the J descends
J_HOOK_END = np.deg2rad(-146.0)     # where the hook stops turning

# How the spectrum is spent.  The D's two strokes share a range because they
# share a start and an end: whichever you trace, you are the same distance in.
T_D = (0.00, 0.50)
T_J = (0.50, 1.00)


# --- Distance primitives ----------------------------------------------------


def _seg(a, b):
    """Distance to a straight segment, and how far along it the foot lies."""
    ax, ay = a
    vx, vy = b[0] - ax, b[1] - ay
    inv = 1.0 / (vx * vx + vy * vy)

    def f(px, py):
        s = np.clip(((px - ax) * vx + (py - ay) * vy) * inv, 0.0, 1.0)
        return np.hypot(px - (ax + s * vx), py - (ay + s * vy)), s

    f.length = float(np.hypot(vx, vy))
    f.sample = lambda n: (
        ax + np.linspace(0, 1, n) * vx,
        ay + np.linspace(0, 1, n) * vy,
    )
    return f


def _arc(c, r, a0, a1):
    """Distance to a circular arc, and how far along it the foot lies.

    Off the sweep the nearest point is one of the two ends, which is what gives
    an open terminal its round cap for free.
    """
    cx, cy = c
    delta = a1 - a0
    span = abs(delta)
    sgn = 1.0 if delta >= 0 else -1.0
    e0 = (cx + r * np.cos(a0), cy + r * np.sin(a0))
    e1 = (cx + r * np.cos(a1), cy + r * np.sin(a1))

    def f(px, py):
        dx, dy = px - cx, py - cy
        k = ((np.arctan2(dy, dx) - a0) * sgn) % (2.0 * np.pi)
        on = k <= span
        d0 = np.hypot(px - e0[0], py - e0[1])
        d1 = np.hypot(px - e1[0], py - e1[1])
        d = np.where(on, np.abs(np.hypot(dx, dy) - r), np.minimum(d0, d1))
        s = np.where(on, np.clip(k / span, 0.0, 1.0), np.where(d0 <= d1, 0.0, 1.0))
        return d, s

    f.length = float(r * span)
    f.sample = lambda n: (
        cx + r * np.cos(np.linspace(a0, a1, n)),
        cy + r * np.sin(np.linspace(a0, a1, n)),
    )
    return f


def _ramp(prims, t0, t1):
    """Give each primitive the slice of [t0, t1] its own arc length earns."""
    total = sum(p.length for p in prims)
    out, run = [], 0.0
    for p in prims:
        a = t0 + (t1 - t0) * run / total
        run += p.length
        out.append((p, a, t0 + (t1 - t0) * run / total))
    return out


def d_stem():
    return [_seg((0.0, 1.0), (0.0, 0.0))]


def d_bowl():
    return [
        _seg((0.0, 1.0), (D_FLAT, 1.0)),
        _arc((D_FLAT, 0.5), D_R, np.pi / 2, -np.pi / 2),
        _seg((D_FLAT, 0.0), (0.0, 0.0)),
    ]


def jay():
    return [
        _seg((J_X, 1.0), (J_X, J_HOOK_Y)),
        _arc((J_X - J_HOOK_R, J_HOOK_Y), J_HOOK_R, 0.0, J_HOOK_END),
    ]


def strokes():
    """The mark's centrelines, each carrying its slice of the spectrum."""
    return _ramp(d_stem(), *T_D) + _ramp(d_bowl(), *T_D) + _ramp(jay(), *T_J)


def field(px, py, prims):
    """Nearest-stroke distance and journey position, for a grid of points."""
    best_d = best_t = None
    for p, a, b in prims:
        d, s = p(px, py)
        t = a + s * (b - a)
        if best_d is None:
            best_d, best_t = d, t
        else:
            take = d < best_d
            best_d = np.where(take, d, best_d)
            best_t = np.where(take, t, best_t)
    return best_d, best_t


def bounds(prims, samples=4000):
    """The ink box: sample every centreline, then grow by half a stroke."""
    xs, ys = [], []
    for p, _a, _b in prims:
        x, y = p.sample(samples)
        xs.append(x)
        ys.append(y)
    x = np.concatenate(xs)
    y = np.concatenate(ys)
    return x.min() - HW, y.min() - HW, x.max() + HW, y.max() + HW


def clearances(samples=6000):
    """The two gaps that decide whether the mark survives being small.

    Returns (side, hook) in cap-height units: the ink-to-ink distance between
    the D's shoulder and the J's stem, and between the D and the J's hook.
    """
    side = J_X - HW - (D_FLAT + D_R + HW)

    d_prims = d_stem() + d_bowl()
    xs, ys = [], []
    for p in jay():
        x, y = p.sample(samples)
        xs.append(x)
        ys.append(y)
    px, py = np.concatenate(xs), np.concatenate(ys)
    d = np.min([p(px, py)[0] for p in d_prims], axis=0)
    return side, float(d.min() - 2 * HW)


# --- Colour space -----------------------------------------------------------


def _hex_rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)])


def _to_linear(c):
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _to_srgb(c):
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


_M1 = np.array([
    [0.4122214708, 0.5363325363, 0.0514459929],
    [0.2119034982, 0.6806995451, 0.1073969566],
    [0.0883024619, 0.2817188376, 0.6299787005],
])
_M2 = np.array([
    [0.2104542553, 0.7936177850, -0.0040720468],
    [1.9779984951, -2.4285922050, 0.4505937099],
    [0.0259040371, 0.7827717662, -0.8086757660],
])
_M1I = np.linalg.inv(_M1)
_M2I = np.linalg.inv(_M2)


def _oklab(hex_colour):
    return _M2 @ np.cbrt(_M1 @ _to_linear(_hex_rgb(hex_colour)))


def _oklab_to_rgb(lab):
    return _to_srgb(((lab @ _M2I.T) ** 3) @ _M1I.T)


def _plateau(u):
    """Ease across an interval, but hold still at both ends.

    A plain interpolation spends the whole interval in transit, and five phases
    become a smear.  Compressing the ease into the middle 60% leaves each phase
    a stretch of its own hue, so the mark shows five colours you could name.
    """
    v = np.clip((np.clip(u, 0.0, 1.0) - 0.20) / 0.60, 0.0, 1.0)
    return v * v * v * (v * (v * 6.0 - 15.0) + 10.0)


_ANCHORS = np.linspace(0.0, 1.0, 5)
_LAB = np.stack([_oklab(LOGO_HUE[k]) for k in PHASE_ORDER])


def spectrum(t):
    """Journey position -> colour.

    Five anchors, eased between by ``_plateau`` so each phase holds a stretch of
    its own hue and hands over cleanly.  The mark shows five colours rather than
    a smear, because the product has five phases and not a smear.
    """
    t = np.clip(t, 0.0, 1.0)
    i = np.clip(np.searchsorted(_ANCHORS, t, side="right") - 1, 0, 3)
    w = _plateau((t - _ANCHORS[i]) / 0.25)[..., None]
    return _oklab_to_rgb(_LAB[i] * (1.0 - w) + _LAB[i + 1] * w)


# --- Rasterising ------------------------------------------------------------

PAD = 0.055           # margin as a fraction of the square, on the tight side


def _alpha_and_t(size, ss, box, prims):
    """Coverage and journey position on an ``ss``-times supersampled grid."""
    x0, y0, x1, y1 = box
    n = size * ss
    scale = (1.0 - 2 * PAD) / max(x1 - x0, y1 - y0)
    ox = 0.5 - scale * (x0 + x1) / 2.0
    oy = 0.5 - scale * (y0 + y1) / 2.0

    p = (np.arange(n, dtype=np.float64) + 0.5) / n
    ux = (p - ox) / scale
    uy = (1.0 - p - oy) / scale              # y up
    unit = 1.0 / (n * scale)                 # one device pixel, in cap units

    alpha = np.empty((n, n), dtype=np.float32)
    tmap = np.empty((n, n), dtype=np.float32)
    band = max(1, 2_000_000 // n)
    for r0 in range(0, n, band):
        r1 = min(n, r0 + band)
        gx, gy = np.meshgrid(ux, uy[r0:r1])
        d, t = field(gx, gy, prims)
        alpha[r0:r1] = np.clip(0.5 - (d - HW) / unit, 0.0, 1.0)
        tmap[r0:r1] = t
    return alpha, tmap


def _downsample(arr, ss):
    if ss == 1:
        return arr
    n = arr.shape[0] // ss
    if arr.ndim == 2:
        return arr.reshape(n, ss, n, ss).mean(axis=(1, 3))
    return arr.reshape(n, ss, n, ss, arr.shape[2]).mean(axis=(1, 3))


def render(size, *, ss=3, mono=None, prims=None, box=None):
    """The mark as a square RGBA image on a transparent ground."""
    prims = prims if prims is not None else strokes()
    box = box if box is not None else bounds(prims)
    alpha, tmap = _alpha_and_t(size, ss, box, prims)

    if mono is None:
        rgb = spectrum(tmap.astype(np.float64)).astype(np.float32)
    else:
        rgb = np.broadcast_to(_hex_rgb(mono).astype(np.float32),
                              alpha.shape + (3,)).copy()

    # Average in premultiplied space, or the edges pick up colour from nowhere.
    pre = _downsample(rgb * alpha[..., None], ss)
    a = _downsample(alpha, ss)
    safe = np.maximum(a[..., None], 1e-6)
    out = np.where(a[..., None] > 1e-6, pre / safe, 0.0)

    return Image.fromarray(np.dstack([
        np.round(np.clip(out, 0, 1) * 255).astype(np.uint8),
        np.round(np.clip(a, 0, 1) * 255).astype(np.uint8),
    ]), "RGBA")


# --- The lockup -------------------------------------------------------------

FONTS = [
    ("/System/Library/Fonts/Avenir Next.ttc", 2),          # Demi Bold
    ("/System/Library/Fonts/Supplemental/Futura.ttc", 0),  # Medium
    ("/System/Library/Fonts/HelveticaNeue.ttc", 10),       # Medium
    ("/Library/Fonts/Arial.ttf", 0),
]
WORD = "Dream Job"
TRACKING = 0.005      # letterspacing, in word cap heights
WORD_GAP = 0.50       # space between mark and word, in word cap heights
MARK_SCALE = 1.45     # the mark's cap height, in word cap heights


def _font(px):
    for path, index in FONTS:
        try:
            return ImageFont.truetype(path, max(1, int(round(px))), index=index)
        except Exception:
            continue
    return ImageFont.load_default()


def _cap_height(font):
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    return -probe.textbbox((0, 0), "D", font=font, anchor="ls")[1]


def _font_for_cap(target):
    """Pick the size whose cap height matches the mark's, by measurement."""
    size = target * 1.4
    for _ in range(8):
        font = _font(size)
        have = _cap_height(font)
        if have <= 0 or abs(have - target) <= 0.5:
            return font
        size *= target / have
    return _font(size)


def _draw_tracked(draw, x, baseline, text, font, fill, tracking):
    for ch in text:
        draw.text((x, baseline), ch, font=font, fill=fill, anchor="ls")
        x += draw.textlength(ch, font=font) + tracking
    return x - tracking


def lockup(cap=280):
    """Mark, then the words, on one baseline.

    The two share a baseline because the mark is itself made of letters, and a
    letter that does not sit on the line looks dropped.  The mark is set larger
    than the word's cap height - a monogram at the same size as the type beside
    it reads as a stray initial rather than as a mark.

    The word is set in a single ink.  The mark already carries the spectrum, and
    a logo that needs its gradient twice trusts neither half of itself.
    """
    prims = strokes()
    box = bounds(prims)
    bh = box[3] - box[1]

    # Render the mark so its cap height - head of the D to foot of the D, which
    # is 1 + 2*HW units - comes out at MARK_SCALE times the word's cap height.
    ppu = MARK_SCALE * cap / (1.0 + 2 * HW)
    square = int(round(bh * ppu / (1.0 - 2 * PAD)))
    mark = render(square, ss=3, prims=prims, box=box)
    mark = mark.crop(mark.getbbox())
    mw, mh = mark.size
    mark_above = int(round((box[3] + HW) * ppu))    # ink top -> the D's foot
    mark_below = mh - mark_above                    # the J's descender

    font = _font_for_cap(cap)
    tracking = TRACKING * cap
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    tw = sum(probe.textlength(c, font=font) for c in WORD) + tracking * (len(WORD) - 1)
    word_below = probe.textbbox((0, 0), WORD, font=font, anchor="ls")[3]

    gap = int(round(WORD_GAP * cap))
    margin = int(round(0.14 * cap))
    above = max(mark_above, cap)
    below = max(mark_below, word_below)

    img = Image.new("RGBA",
                    (margin * 2 + mw + gap + int(round(tw)),
                     margin * 2 + above + below),
                    (0, 0, 0, 0))
    baseline = margin + above
    img.alpha_composite(mark, (margin, baseline - mark_above))
    _draw_tracked(ImageDraw.Draw(img), margin + mw + gap, baseline,
                  WORD, font, INK, tracking)
    return img


# --- Checks and output ------------------------------------------------------


def _report(path):
    im = Image.open(path)
    a = np.array(im)
    alpha = a[..., 3]
    edge = int(max(alpha[0].max(), alpha[-1].max(), alpha[:, 0].max(),
                   alpha[:, -1].max()))
    opaque = (alpha == 255).mean()
    print(f"  {path.name:28s} {im.size[0]:>5d}x{im.size[1]:<5d} {im.mode}"
          f"  max edge alpha={edge:<3d}  solid ink={opaque:5.1%}"
          f"  {path.stat().st_size:>7,d} bytes")


def main():
    prims = strokes()
    box = bounds(prims)
    w, h = box[2] - box[0], box[3] - box[1]
    side, hook = clearances()
    px32 = (32 * (1 - 2 * PAD)) / max(w, h)     # device pixels per cap unit at 32

    print("Dream Job - the DJ monogram\n")
    print("  phase        interface   mark")
    for k in PHASE_ORDER:
        print(f"  {PHASE_NAME[k]:<11s}  {PHASE[k]}     {LOGO_HUE[k]}")

    print(f"\n  ink box     {w:.3f} x {h:.3f} units   (aspect {w / h:.2f})")
    print(f"  stroke      {2 * HW:.3f} units = {2 * HW * px32:.1f} px at 32 px")
    print(f"  D counter   {D_R + D_FLAT - 2 * HW:.3f} x {1 - 2 * HW:.3f} units"
          f" = {(D_R + D_FLAT - 2 * HW) * px32:.1f} x {(1 - 2 * HW) * px32:.1f} px at 32 px")
    print(f"  side gap    {side:.3f} units = {side * px32:.1f} px at 32 px")
    print(f"  hook gap    {hook:.3f} units = {hook * px32:.1f} px at 32 px")

    # The docstring claims the bowl passes exactly Plan blue at its widest and
    # hands over to the J at Discover teal.  Check it rather than assert it.
    lens = [q.length for q in d_bowl()]
    t_wide = T_D[0] + (T_D[1] - T_D[0]) * (lens[0] + lens[1] / 2) / sum(lens)
    print(f"  bowl widest at t={t_wide:.3f} (Plan blue sits at 0.250);"
          f" D hands to J at t={T_D[1]:.3f} (Discover teal at 0.500)")
    if min(side, hook) * px32 < 1.5:
        print("  WARNING: a gap closes below 1.5 px at 32 px - open it up.")

    print("\n  written:")
    p = OUT / "logo-wordmark.png"
    render(1024, ss=3, prims=prims, box=box).save(p)
    _report(p)

    p = OUT / "logo-wordmark-lockup.png"
    lockup().save(p)
    _report(p)

    p = OUT / "logo-wordmark-32.png"
    render(32, ss=32, prims=prims, box=box).save(p)
    _report(p)

    p = OUT / "logo-wordmark-mono.png"
    render(1024, ss=3, prims=prims, box=box, mono=INK).save(p)
    _report(p)


if __name__ == "__main__":
    main()
