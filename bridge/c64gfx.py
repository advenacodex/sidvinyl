"""C64 graphics helpers: palette, multicolour-bitmap cell quantiser, rotating
label frames (album cover), static vinyl disc, and a preview renderer.

Multicolour bitmap cell = 4x8 pixels (each pixel is 2 hires pixels wide).
Pixel value 00 = $D021 (black here), 01 = screen RAM high nibble,
10 = screen RAM low nibble, 11 = colour RAM nibble.
"""
import hashlib
from itertools import combinations

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

# Pepto PAL palette
PALETTE = np.array([
    (0, 0, 0), (255, 255, 255), (104, 55, 43), (112, 164, 178),
    (111, 61, 134), (88, 141, 67), (53, 40, 121), (184, 199, 111),
    (111, 79, 37), (67, 57, 0), (154, 103, 89), (68, 68, 68),
    (108, 108, 108), (154, 210, 132), (108, 94, 181), (149, 149, 149)], dtype=float)

LABEL_CELLS = 8                      # label is 8x8 cells = 64x64 hires pixels
COVER_FRAMES = 32
FRAME_BYTES = 640                    # 512 bitmap + 64 screen + 64 colour
DISC_CELLS = 16

_M = np.array([[0.4124564, 0.3575761, 0.1804375],
               [0.2126729, 0.7151522, 0.0721750],
               [0.0193339, 0.1191920, 0.9503041]])
_WHITE = np.array([0.95047, 1.0, 1.08883])


def srgb_to_lab(rgb):
    c = np.asarray(rgb, dtype=float) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = (lin @ _M.T) / _WHITE
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], -1)


PAL_LAB = srgb_to_lab(PALETTE)
BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16.0 - 0.5
_COMBO_CACHE = {}


def _combos(n):
    if n not in _COMBO_CACHE:
        _COMBO_CACHE[n] = np.array(list(combinations(range(n), 3)))
    return _COMBO_CACHE[n]


def encode_mc(rgb, mask=None, allowed=None, dither_amp=22.0, bg=0):
    """Quantise an RGB array (H, W, 3) of multicolour pixels (W = 4*cols,
    H = 8*rows) into bitmap/screen/colour bytes.

    Returns (bitmap[rows,cols,8], screen[rows,cols], color[rows,cols]).
    """
    rgb = np.asarray(rgb, dtype=float)
    H, W, _ = rgb.shape
    rows, cols = H // 8, W // 4
    pool = [c for c in (list(allowed) if allowed is not None else range(16)) if c != bg]
    while len(pool) < 3:
        pool.append(pool[-1])
    combos = _combos(len(pool))
    pal = PAL_LAB[pool]
    bg_lab = PAL_LAB[bg]
    lab = srgb_to_lab(rgb)
    yy, xx = np.mgrid[0:H, 0:W]
    lab_d = srgb_to_lab(np.clip(rgb + BAYER4[yy % 4, xx % 4][..., None] * dither_amp, 0, 255))
    if mask is None:
        mask = np.ones((H, W), dtype=bool)

    bitmap = np.zeros((rows, cols, 8), dtype=np.uint8)
    screen = np.zeros((rows, cols), dtype=np.uint8)
    color = np.zeros((rows, cols), dtype=np.uint8)
    weights = np.array([64, 16, 4, 1], dtype=np.uint8)
    for cy in range(rows):
        for cx in range(cols):
            sl = (slice(cy * 8, cy * 8 + 8), slice(cx * 4, cx * 4 + 4))
            m = mask[sl].reshape(32)
            if m.any():
                px = lab[sl].reshape(32, 3)
                D = ((px[:, None, :] - pal[None]) ** 2).sum(-1)
                d0 = ((px - bg_lab) ** 2).sum(-1)
                cand = np.minimum(D[:, combos].min(axis=2), d0[:, None])
                best = combos[(cand * m[:, None]).sum(0).argmin()]
            else:
                best = combos[0]
            chosen = [pool[i] for i in best]
            pxd = lab_d[sl].reshape(32, 3)
            cl = np.vstack([bg_lab, PAL_LAB[chosen]])
            idx = ((pxd[:, None, :] - cl[None]) ** 2).sum(-1).argmin(1)
            idx[~m] = 0
            idx = idx.reshape(8, 4).astype(np.uint8)
            bitmap[cy, cx] = (idx * weights).sum(1)
            screen[cy, cx] = (chosen[0] << 4) | chosen[1]
            color[cy, cx] = chosen[2]
    return bitmap, screen, color


# ---------------------------------------------------------------- label / cover
def label_source(img):
    """PIL image -> centred square 128x128, slightly boosted for the C64 palette."""
    img = img.convert("RGB")
    w, h = img.size
    s = min(w, h)
    img = img.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
    img = img.resize((128, 128), Image.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(1.25)
    img = ImageEnhance.Color(img).enhance(1.35)
    return img


def _label_mask():
    c = np.arange(4 * LABEL_CELLS)
    r = np.arange(8 * LABEL_CELLS)
    xh = 2 * c[None, :] + 1 - 32.0
    yh = r[:, None] + 0.5 - 32.0
    d2 = xh ** 2 + yh ** 2
    return (d2 <= 31.0 ** 2) & (d2 >= 3.5 ** 2)


_LABEL_MASK = _label_mask()


def _disc_under_label():
    """RGB of the static disc where the label sits (so label corners show grooves)."""
    rgb, _ = disc_rgb()
    c0 = 4 * ((DISC_CELLS - LABEL_CELLS) // 2)
    r0 = 8 * ((DISC_CELLS - LABEL_CELLS) // 2)
    return rgb[r0:r0 + 8 * LABEL_CELLS, c0:c0 + 4 * LABEL_CELLS]


_UNDER = None


def label_frame(src, angle_deg):
    """One rotated label frame (clockwise by angle_deg) as 640 bytes."""
    global _UNDER
    if _UNDER is None:
        _UNDER = _disc_under_label()
    rot = src.rotate(-angle_deg, resample=Image.BICUBIC, fillcolor=(0, 0, 0))
    small = np.asarray(rot.resize((4 * LABEL_CELLS, 8 * LABEL_CELLS), Image.BOX), dtype=float)
    small = np.where(_LABEL_MASK[..., None], small, _UNDER)
    bm, sc, co = encode_mc(small)
    return bm.tobytes() + sc.tobytes() + co.tobytes()


def label_frames(img, nframes=COVER_FRAMES):
    src = label_source(img)
    return b"".join(label_frame(src, 360.0 * k / nframes) for k in range(nframes))


def default_cover(seed="sidvinyl"):
    """Generated label used when an album has no cover art."""
    h = hashlib.md5(str(seed).encode()).digest()
    hue = h[0] / 255.0
    import colorsys
    def col(hh, s, v):
        r, g, b = colorsys.hsv_to_rgb(hh % 1.0, s, v)
        return (int(r * 255), int(g * 255), int(b * 255))
    img = Image.new("RGB", (256, 256), col(hue, 0.75, 0.75))
    d = ImageDraw.Draw(img)
    for i, rad in enumerate(range(124, 8, -22)):
        d.ellipse((128 - rad, 128 - rad, 128 + rad, 128 + rad),
                  fill=col(hue + 0.08 * (i % 3), 0.7, 0.45 + 0.4 * (i % 2)))
    d.rectangle((0, 112, 256, 144), fill=(250, 250, 250))
    try:
        font = ImageFont.load_default(size=26)
    except TypeError:
        font = ImageFont.load_default()
    d.text((128, 128), "SID VINYL", fill=(20, 20, 20), font=font, anchor="mm")
    return img


# ---------------------------------------------------------------- vinyl disc
def disc_rgb():
    """Float RGB image (128 rows x 64 mc columns) of the vinyl and its mask."""
    H, W = 8 * DISC_CELLS, 4 * DISC_CELLS
    c = np.arange(W)
    r = np.arange(H)
    xh = 2 * c[None, :] + 1 - 64.0
    yh = r[:, None] + 0.5 - 64.0
    rad = np.sqrt(xh ** 2 + yh ** 2)
    ang = np.arctan2(yh, xh)
    groove = (rad >= 33) & (rad <= 60.5)
    band = (np.floor(rad * 0.9).astype(int) % 4 == 0)
    g = np.where(groove, np.where(band, 0.20, 0.07), 0.0)
    g = np.where((rad > 60.5) & (rad <= 62.5), 0.28, g)

    def wedge(a0):
        d = np.angle(np.exp(1j * (ang - a0)))
        return np.maximum(0.0, np.cos(d)) ** 10

    radial = np.clip((rad - 34) / 26.0, 0, 1)
    sheen = (wedge(-0.9) + wedge(2.24)) * (0.35 + 0.65 * radial)
    g = np.clip(g + 0.48 * sheen * ((rad > 33) & (rad <= 62.5)), 0, 1)
    rgb = np.stack([g * 255, g * 255, g * 255 * 1.04], -1).clip(0, 255)
    return rgb, rad <= 62.5


def render_disc():
    """Static 16x16-cell vinyl record. Returns (bitmap, screen, color) arrays."""
    rgb, mask = disc_rgb()
    return encode_mc(rgb, mask=mask, allowed=[11, 12, 15, 1], dither_amp=30.0)


def tonearm():
    """Tonearm drawn with three x/y-expanded hires sprites (48x42 screen px each).

    Returns (sprite_bytes[192], positions[7]) where positions are
    x0,y0,x1,y1,x2,y2,msb (sprite coordinate space: x = 24 + screen_x, y = 50 + screen_y).
    """
    p0 = np.array([300.0, 58.0])          # pivot
    p1 = np.array([226.0, 134.0])         # needle on the grooves
    d = p1 - p0
    dd = float(d @ d)
    data = bytearray()
    pos = []
    for t in (0.2, 0.5, 0.8):
        c = p0 + t * d
        ox, oy = int(round(c[0] - 24)), int(round(c[1] - 21))
        pos.append((ox, oy))
        rows = []
        for j in range(21):
            bits = 0
            for i in range(24):
                px = np.array([ox + 2 * i + 1, oy + 2 * j + 1], dtype=float)
                u = max(0.0, min(1.0, float((px - p0) @ d) / dd))
                dist = float(np.linalg.norm(px - (p0 + u * d)))
                hub = float(np.linalg.norm(px - p0)) <= 9.0
                head = float(np.linalg.norm(px - p1)) <= 6.0
                if dist <= 2.4 or hub or head:
                    bits |= 1 << (23 - i)
            rows.append(bits.to_bytes(3, "big"))
        data += b"".join(rows) + b"\x00"
    msb = sum(1 << k for k, (x, _) in enumerate(pos) if x >= 256)
    flat = []
    for x, y in pos:
        flat += [x & 0xFF, y & 0xFF]
    return bytes(data), bytes(flat + [msb])


def disc_blob():
    """Payload of PKT_DISC: disc bitmap/screen/colour (2560 B) + 3 sprites (192 B) + 7 position bytes."""
    bm, sc, co = render_disc()
    spr, pos = tonearm()
    return bm.tobytes() + sc.tobytes() + co.tobytes() + spr + pos


# ---------------------------------------------------------------- preview
def mc_screen_image(bitmap, screen, color, bg=0, cols=40, rows=25):
    """Render multicolour-bitmap memory (8000/1000/1000 bytes) to a PIL image
    of 320x200 (each mc pixel doubled horizontally)."""
    img = np.zeros((rows * 8, cols * 8, 3), dtype=np.uint8)
    for cy in range(rows):
        for cx in range(cols):
            s = screen[cy * cols + cx]
            cmap = [bg, s >> 4, s & 15, color[cy * cols + cx] & 15]
            for y in range(8):
                b = bitmap[(cy * cols + cx) * 8 + y]
                for p in range(4):
                    v = (b >> (6 - 2 * p)) & 3
                    rgbv = PALETTE[cmap[v]]
                    img[cy * 8 + y, cx * 8 + p * 2: cx * 8 + p * 2 + 2] = rgbv
    return Image.fromarray(img)


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "disc.bin"
    open(out, "wb").write(disc_blob())
    print("wrote", out)
