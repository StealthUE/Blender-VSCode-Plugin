"""Contact sheets, side-by-side comparisons and difference heatmaps, drawn with numpy.

Blender loads and saves the images. Labels use a built-in 5x7 bitmap font, so this needs no GPU
and no extra packages. No relative imports: render_job.py loads this file directly.
"""
from __future__ import annotations

import os

import bpy
import numpy as np

_FONT = {
    "A": ".###.|#...#|#...#|#####|#...#|#...#|#...#", "B": "####.|#...#|#...#|####.|#...#|#...#|####.",
    "C": ".###.|#...#|#....|#....|#....|#...#|.###.", "D": "####.|#...#|#...#|#...#|#...#|#...#|####.",
    "E": "#####|#....|#....|####.|#....|#....|#####", "F": "#####|#....|#....|####.|#....|#....|#....",
    "G": ".###.|#...#|#....|#.###|#...#|#...#|.####", "H": "#...#|#...#|#...#|#####|#...#|#...#|#...#",
    "I": ".###.|..#..|..#..|..#..|..#..|..#..|.###.", "J": "..###|...#.|...#.|...#.|...#.|#..#.|.##..",
    "K": "#...#|#..#.|#.#..|##...|#.#..|#..#.|#...#", "L": "#....|#....|#....|#....|#....|#....|#####",
    "M": "#...#|##.##|#.#.#|#.#.#|#...#|#...#|#...#", "N": "#...#|#...#|##..#|#.#.#|#..##|#...#|#...#",
    "O": ".###.|#...#|#...#|#...#|#...#|#...#|.###.", "P": "####.|#...#|#...#|####.|#....|#....|#....",
    "Q": ".###.|#...#|#...#|#...#|#.#.#|#..#.|.##.#", "R": "####.|#...#|#...#|####.|#.#..|#..#.|#...#",
    "S": ".####|#....|#....|.###.|....#|....#|####.", "T": "#####|..#..|..#..|..#..|..#..|..#..|..#..",
    "U": "#...#|#...#|#...#|#...#|#...#|#...#|.###.", "V": "#...#|#...#|#...#|#...#|#...#|.#.#.|..#..",
    "W": "#...#|#...#|#...#|#.#.#|#.#.#|#.#.#|.#.#.", "X": "#...#|#...#|.#.#.|..#..|.#.#.|#...#|#...#",
    "Y": "#...#|#...#|.#.#.|..#..|..#..|..#..|..#..", "Z": "#####|....#|...#.|..#..|.#...|#....|#####",
    "0": ".###.|#...#|#..##|#.#.#|##..#|#...#|.###.", "1": "..#..|.##..|..#..|..#..|..#..|..#..|.###.",
    "2": ".###.|#...#|....#|...#.|..#..|.#...|#####", "3": "#####|...#.|..#..|...#.|....#|#...#|.###.",
    "4": "...#.|..##.|.#.#.|#..#.|#####|...#.|...#.", "5": "#####|#....|####.|....#|....#|#...#|.###.",
    "6": "..##.|.#...|#....|####.|#...#|#...#|.###.", "7": "#####|....#|...#.|..#..|.#...|.#...|.#...",
    "8": ".###.|#...#|#...#|.###.|#...#|#...#|.###.", "9": ".###.|#...#|#...#|.####|....#|...#.|.##..",
    " ": ".....|.....|.....|.....|.....|.....|.....", "-": ".....|.....|.....|#####|.....|.....|.....",
    "_": ".....|.....|.....|.....|.....|.....|#####", ".": ".....|.....|.....|.....|.....|.##..|.##..",
    ":": ".....|.##..|.##..|.....|.##..|.##..|.....", "/": "....#|....#|...#.|..#..|.#...|#....|#....",
    "(": "...#.|..#..|.#...|.#...|.#...|..#..|...#.", ")": ".#...|..#..|...#.|...#.|...#.|..#..|.#...",
    "#": ".#.#.|.#.#.|#####|.#.#.|#####|.#.#.|.#.#.", "%": "##..#|##..#|...#.|..#..|.#...|#..##|#..##",
    "+": ".....|..#..|..#..|#####|..#..|..#..|.....", ",": ".....|.....|.....|.....|.##..|..#..|.#...",
    "'": "..#..|..#..|.#...|.....|.....|.....|.....", "=": ".....|.....|#####|.....|#####|.....|.....",
    "x": ".....|.....|#...#|.#.#.|..#..|.#.#.|#...#", "?": ".###.|#...#|....#|...#.|..#..|.....|..#..",
    "[": ".###.|.#...|.#...|.#...|.#...|.#...|.###.", "]": ".###.|...#.|...#.|...#.|...#.|...#.|.###.",
    ">": ".#...|..#..|...#.|....#|...#.|..#..|.#...", "<": "...#.|..#..|.#...|#....|.#...|..#..|...#.",
}
_GLYPHS = {ch: np.array([[c == "#" for c in row] for row in rows.split("|")], dtype=bool) for ch, rows in _FONT.items()}


def load(path: str) -> np.ndarray:
    """RGBA float32, top row first."""
    img = bpy.data.images.load(os.path.abspath(path), check_existing=False)
    try:
        w, h = img.size
        if not w or not h:
            raise RuntimeError(f"could not read {path}")
        buf = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(buf)
    finally:
        bpy.data.images.remove(img)
    return np.flipud(buf.reshape(h, w, 4)).copy()


def save(path: str, pixels: np.ndarray) -> str:
    h, w = pixels.shape[:2]
    img = bpy.data.images.new("_vsblender_sheet", w, h, alpha=True)
    try:
        img.pixels.foreach_set(np.flipud(np.clip(pixels, 0.0, 1.0)).astype(np.float32).ravel())
        img.filepath_raw = os.path.abspath(path)
        img.file_format = "PNG"
        img.save()
    finally:
        bpy.data.images.remove(img)
    return path


def resize(pixels: np.ndarray, width: int, height: int) -> np.ndarray:
    """Bilinear resize; box-filters first when shrinking a lot, so thin lines survive."""
    h, w = pixels.shape[:2]
    width, height = max(1, int(width)), max(1, int(height))
    if (w, h) == (width, height):
        return pixels
    factor = int(min(w / width, h / height))
    if factor >= 2:
        hh, ww = (h // factor) * factor, (w // factor) * factor
        pixels = pixels[:hh, :ww].reshape(hh // factor, factor, ww // factor, factor, -1).mean(axis=(1, 3))
        h, w = pixels.shape[:2]
    ys = np.clip((np.arange(height) + 0.5) * h / height - 0.5, 0, h - 1)
    xs = np.clip((np.arange(width) + 0.5) * w / width - 0.5, 0, w - 1)
    y0, x0 = np.floor(ys).astype(int), np.floor(xs).astype(int)
    y1, x1 = np.minimum(y0 + 1, h - 1), np.minimum(x0 + 1, w - 1)
    fy, fx = (ys - y0)[:, None, None], (xs - x0)[None, :, None]
    top = pixels[y0][:, x0] * (1 - fx) + pixels[y0][:, x1] * fx
    bottom = pixels[y1][:, x0] * (1 - fx) + pixels[y1][:, x1] * fx
    return (top * (1 - fy) + bottom * fy).astype(np.float32)


def text_width(text: str, scale: int) -> int:
    return len(text) * 6 * scale


def draw_text(pixels: np.ndarray, text: str, x: int, y: int, scale: int = 2, color=(1.0, 1.0, 1.0, 1.0)) -> None:
    h, w = pixels.shape[:2]
    for i, ch in enumerate(text):
        glyph = _GLYPHS.get(ch) if ch in _GLYPHS else _GLYPHS.get(ch.upper(), _GLYPHS["?"])
        big = np.kron(glyph, np.ones((scale, scale), dtype=bool))
        gx = x + i * 6 * scale
        if gx >= w:
            break
        gh, gw = big.shape
        y2, x2 = min(h, y + gh), min(w, gx + gw)
        if y2 <= y or x2 <= gx:
            continue
        region = pixels[y:y2, gx:x2]
        region[big[: y2 - y, : x2 - gx]] = color


def fit_label(text: str, width: int, scale: int) -> str:
    room = max(1, (width - 8) // (6 * scale))
    return text if len(text) <= room else text[: max(1, room - 1)] + "."


def compose(images, labels=None, columns: int | None = None, cell: int = 512, pad: int = 8,
            background=(0.12, 0.12, 0.13, 1.0)) -> np.ndarray:
    """Grid of images, each scaled to fit a cell `cell` pixels high, with a label bar above it."""
    arrays = [load(item) if isinstance(item, str) else item for item in images]
    if not arrays:
        raise ValueError("nothing to compose")
    labels = list(labels or [""] * len(arrays))
    count = len(arrays)
    columns = columns or (count if count <= 4 else int(np.ceil(np.sqrt(count))))
    rows = int(np.ceil(count / columns))
    widths = []
    scaled = []
    for arr in arrays:
        h, w = arr.shape[:2]
        new_w = max(1, round(w * cell / h))
        scaled.append(resize(arr, new_w, cell))
        widths.append(new_w)
    col_w = max(widths)
    scale = 2 if cell < 700 else 3
    bar = 7 * scale + 10 if any(labels) else 0
    sheet_w = columns * col_w + (columns + 1) * pad
    sheet_h = rows * (cell + bar) + (rows + 1) * pad
    sheet = np.empty((sheet_h, sheet_w, 4), dtype=np.float32)
    sheet[:] = background
    for i, arr in enumerate(scaled):
        r, c = divmod(i, columns)
        x = pad + c * (col_w + pad)
        y = pad + r * (cell + bar + pad)
        if labels[i]:
            draw_text(sheet, fit_label(str(labels[i]), col_w, scale), x + 2, y + 4, scale)
        ox = x + (col_w - arr.shape[1]) // 2
        sheet[y + bar: y + bar + arr.shape[0], ox: ox + arr.shape[1]] = arr
    return sheet


def heatmap(a: np.ndarray, b: np.ndarray, threshold: float = 0.04):
    """Per-pixel difference of b against a, coloured black -> red -> yellow -> white.

    Returns (image, stats). b is resized to a when the sizes differ.
    """
    if a.shape[:2] != b.shape[:2]:
        b = resize(b, a.shape[1], a.shape[0])
    diff = np.abs(a[..., :3] - b[..., :3]).max(axis=2)
    stats = {"mean_difference": round(float(diff.mean()), 4),
             "changed_percent": round(float((diff > threshold).mean() * 100.0), 2),
             "max_difference": round(float(diff.max()), 4)}
    t = np.clip(diff * 4.0, 0.0, 1.0)
    out = np.empty(a.shape[:2] + (4,), dtype=np.float32)
    out[..., 0] = np.clip(t * 3.0, 0, 1)
    out[..., 1] = np.clip(t * 3.0 - 1.0, 0, 1)
    out[..., 2] = np.clip(t * 3.0 - 2.0, 0, 1)
    out[..., 3] = 1.0
    return out, stats


def compose_files(paths, out: str, labels=None, columns=None, cell: int = 512, diff: bool = False) -> dict:
    """Compose image files into one PNG. With diff=True and two images, adds a heatmap panel."""
    arrays = [load(p) for p in paths]
    labels = list(labels or [os.path.basename(p) for p in paths])
    stats = None
    if diff:
        if len(arrays) != 2:
            raise ValueError("a difference heatmap needs exactly two images")
        hm, stats = heatmap(arrays[0], arrays[1])
        arrays.append(hm)
        labels.append(f"difference: {stats['changed_percent']}% of pixels changed")
    sheet = compose(arrays, labels, columns=columns, cell=cell)
    save(out, sheet)
    result = {"file": out, "width": int(sheet.shape[1]), "height": int(sheet.shape[0]), "panels": len(arrays)}
    if stats:
        result["difference"] = stats
    return result
