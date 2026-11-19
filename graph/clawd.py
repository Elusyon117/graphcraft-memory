r"""Clawd for the Memory pane, from the clawd-pet mod's frames when that mod is installed.

    py -I clawd.py svg       {"lit": {"svg", "width", "height"}, "idle": {...}, "source": "clawd-pet 0.29.5"} or {"source": ""}

The pet mod ships Clawd's animation frames (Anthropic's artwork, which that mod's NOTICE keeps outside its license).
They are read here from the user's own install and never copied into this repo: with no pet mod, the answer has no
SVG and the pane draws its own small Clawd (hooks/clawd.ts). Only PNG files are read; no code of the other mod runs
(CLAUDE.md rule 4).

Each animation becomes one SVG: every frame an <image> (a data: PNG), shown in turn by SMIL at the pet's frame rate,
cropped to the pixels any frame uses so Clawd fills the space.
"""
import base64
import json
import os
import struct
import sys
import zlib
from pathlib import Path

ANIMATIONS = {"lit": "Lightbulb", "idle": "LookingAround"}  # facts recalled: an idea; nothing: looking around
FPS = 12
SCALE = 0.5  # CSS pixels per frame pixel: both animations share one box, so Clawd keeps his size and spot
MAX_SVG = 120_000  # the pane's Svg takes at most 131072 characters


def frames_root() -> Path | None:
    """The newest installed clawd-pet's frames/ folder, from the plugin cache, else its marketplace copy."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "plugins"
    found = sorted(base.glob("cache/*/clawd-pet/*/frames"), key=lambda p: p.stat().st_mtime, reverse=True)
    found += list(base.glob("marketplaces/*/clawd-pet/frames"))
    return next((p for p in found if p.is_dir()), None)


def png_alpha_box(data: bytes) -> tuple | None:
    """(left, top, right, bottom) of the non-transparent pixels of an 8-bit RGBA, non-interlaced PNG; None otherwise."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w, h, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", data[16:29])
    if (depth, color, interlace) != (8, 6, 0):
        return None
    idat, i = b"", 8
    while i < len(data):
        n = struct.unpack(">I", data[i:i + 4])[0]
        kind = data[i + 4:i + 8]
        if kind == b"IDAT":
            idat += data[i + 8:i + 8 + n]
        i += 12 + n
    raw, stride, prev = zlib.decompress(idat), w * 4, bytearray(w * 4)
    box = [w, h, -1, -1]
    for y in range(h):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            a = line[x - 4] if x >= 4 else 0
            b = prev[x]
            c = prev[x - 4] if x >= 4 else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        for x in range(w):
            if line[x * 4 + 3]:
                box = [min(box[0], x), min(box[1], y), max(box[2], x + 1), max(box[3], y + 1)]
        prev = line
    return tuple(box) if box[2] > 0 else None


def frames(folder: Path) -> list:
    files = sorted((p for p in folder.glob("*.png") if p.stem.isdigit()), key=lambda p: int(p.stem))
    return [p.read_bytes() for p in files]


def union_box(pngs: list) -> tuple | None:
    boxes = [b for b in (png_alpha_box(d) for d in pngs) if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def animation(pngs: list, box: tuple) -> dict | None:
    left, top, right, bottom = box
    full_w, full_h = struct.unpack(">II", pngs[0][16:24])
    n, dur = len(pngs), len(pngs) / FPS
    parts = []
    for i, d in enumerate(pngs):
        # Frame i is visible from i/n to (i+1)/n of the loop; discrete, so frames swap without blending.
        if i == 0:
            values, times = "visible;hidden", f"0;{1 / n:.5f}"
        elif i == n - 1:
            values, times = "hidden;visible", f"0;{i / n:.5f}"
        else:
            values, times = "hidden;visible;hidden", f"0;{i / n:.5f};{(i + 1) / n:.5f}"
        parts.append(
            f'<image width="{full_w}" height="{full_h}" href="data:image/png;base64,{base64.b64encode(d).decode()}"'
            f' visibility="{"visible" if i == 0 else "hidden"}">'
            f'<animate attributeName="visibility" values="{values}" keyTimes="{times}" calcMode="discrete"'
            f' dur="{dur:.3f}s" repeatCount="indefinite"/></image>')
    w, h = right - left, bottom - top
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{left} {top} {w} {h}" style="image-rendering:pixelated">'
           + "".join(parts) + "</svg>")
    if len(svg) > MAX_SVG:
        return None
    return {"svg": svg, "width": round(w * SCALE), "height": round(h * SCALE)}


def cache_file() -> Path:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return base / "graphcraft-memory" / "view" / "clawd.json"


def build() -> dict:
    """Decoding the frames takes seconds in plain Python, so the result is kept until the pet mod's frames change."""
    root = frames_root()
    if root is None:
        return {"source": ""}
    folders = [root / name for name in ANIMATIONS.values()]
    key = f"{root}|{SCALE}|" + "|".join(f"{p.name}:{p.stat().st_mtime_ns}" for f in folders if f.is_dir()
                                       for p in sorted(f.glob('*.png')))
    cache = cache_file()
    try:
        held = json.loads(cache.read_text(encoding="utf-8"))
        if held.get("key") == key:
            return held["out"]
    except (OSError, ValueError, KeyError):
        pass
    sets = {k: frames(root / name) for k, name in ANIMATIONS.items()}
    if not all(sets.values()):
        return {"source": ""}
    box = union_box([d for pngs in sets.values() for d in pngs])
    out = {k: animation(pngs, box) for k, pngs in sets.items()} if box else {}
    out = {**out, "source": f"clawd-pet {root.parent.name}"} if out and all(out.values()) else {"source": ""}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"key": key, "out": out}), encoding="utf-8")
    return out


def main(argv=None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        out = build() if args[:1] == ["svg"] else {"error": "usage: clawd.py svg"}
    except Exception as e:  # one JSON answer either way; the pane falls back to its own Clawd
        out = {"error": f"{type(e).__name__}: {e}", "source": ""}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
