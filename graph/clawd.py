r"""Clawd for the Memory pane: Clawd thinking, with a memory bubble that builds a little knowledge graph.

    py -I clawd.py svg       {"lit": {"svg", "width", "height"}, "idle": {...}, "source": "clawd-pet 0.29.5" | "own"}

- lit (facts were recalled for the last prompt): Clawd ponders while, in the bubble, nodes appear one by one and links
  grow between them with a bright tip, as recall walks the graph; then it glows twice, dims and starts over.
- idle: Clawd looks around; the bubble rests dim and dotted, its nodes twinkle in turn, and a speck rises into it.

The bubble (and the stand-in Clawd) is pixel art: a small grid, a few frames, swapped every 160 ms in a plain loop, the
way zonecraft's crab scenes are drawn.

Clawd himself comes from the clawd-pet mod's frames when that mod is installed (Anthropic's artwork, which that mod's
NOTICE keeps outside its license). They are read from the user's own install at run time and never copied into this
repo; only PNG files are read, no code of the other mod runs (CLAUDE.md rule 4). Without it, a small Clawd drawn here
stands in. Frames swap with SMIL (no script), so the pane's sandboxed Svg plays them.
"""
import base64
import json
import os
import struct
import sys
from pathlib import Path

ANIMATIONS = {"lit": "Thinking", "idle": "LookingAround"}  # the pet's own: pondering; looking around
FPS = 12
SCALE = 0.5  # CSS pixels per frame pixel
MAX_SVG = 120_000  # the pane's Svg takes at most 131072 characters

# Laid out in the pet's frame space (192 x 128): Clawd stands in CLAWD, the bubble floats up and to the right of his
# head (BUBBLE_AT), and the drawing shows VIEW. The pet's own speech bubble in Thinking (above y 60) is clipped off.
CLAWD = (32, 60, 128, 128)
VIEW = (32, 0, 186, 128)
BUBBLE_AT = (104, 0)

BODY, SHADE, EYE = "#D97757", "#BF694D", "#000000"


def frames_root() -> Path | None:
    """The newest installed clawd-pet's frames/ folder, from the plugin cache, else its marketplace copy."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "plugins"
    found = sorted(base.glob("cache/*/clawd-pet/*/frames"), key=lambda p: p.stat().st_mtime, reverse=True)
    found += list(base.glob("marketplaces/*/clawd-pet/frames"))
    return next((p for p in found if p.is_dir()), None)


def frames(folder: Path) -> list:
    """The animation's PNGs in order; the mirrored copies ("0m.png") are left out."""
    files = sorted((p for p in folder.glob("*.png") if p.stem.isdigit()), key=lambda p: int(p.stem))
    return [p.read_bytes() for p in files]


def pet_layer(pngs: list) -> str:
    """Every frame an <image>, shown in turn at the pet's frame rate; discrete, so frames swap without blending."""
    full_w, full_h = struct.unpack(">II", pngs[0][16:24])
    n, dur = len(pngs), len(pngs) / FPS
    parts = []
    for i, d in enumerate(pngs):
        if n == 1:
            anim = ""
        else:
            if i == 0:
                values, times = "visible;hidden", f"0;{1 / n:.5f}"
            elif i == n - 1:
                values, times = "hidden;visible", f"0;{i / n:.5f}"
            else:
                values, times = "hidden;visible;hidden", f"0;{i / n:.5f};{(i + 1) / n:.5f}"
            anim = (f'<animate attributeName="visibility" values="{values}" keyTimes="{times}" calcMode="discrete"'
                    f' dur="{dur:.3f}s" repeatCount="indefinite"/>')
        parts.append(f'<image width="{full_w}" height="{full_h}" href="data:image/png;base64,'
                     f'{base64.b64encode(d).decode()}" visibility="{"visible" if i == 0 else "hidden"}">{anim}</image>')
    x0, y0, x1, y1 = CLAWD
    return (f'<clipPath id="clawd"><rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}"/></clipPath>'
            f'<g clip-path="url(#clawd)" style="image-rendering:pixelated">{"".join(parts)}</g>')


# ---- Pixel frames, looped (the zonecraft way: a small grid, a few steps, swapped every STEP_MS) ----

STEP_MS = 160
CELL = 4  # frame pixels per bubble pixel: twice as fine as Clawd's own, so a graph fits in the bubble
BW, BH = 20, 16  # the bubble's grid

# One character per pixel; "." is empty. Colors with an opacity, so the bubble reads on light and dark panes.
COLORS = {
    "o": ("#9A9893", 1), "f": ("#9A9893", 0.14),            # cloud outline, cloud fill
    "n": ("#D97757", 1), "h": ("#F2C9A8", 1),               # node, node highlight
    "l": ("#E3B79C", 1), "p": ("#FFF3C4", 1),               # link, pulse and glow
    "d": ("#8A8883", 0.75), "e": ("#8A8883", 0.45),         # dim node, dim link
    "g": ("#C9C6BF", 1),                                    # twinkle
    "x": (BODY, 1), "s": (SHADE, 1), "k": (EYE, 1),         # the stand-in Clawd
}

NODES = [(4, 6), (8, 2), (14, 3), (11, 7)]  # top-left of each 2 x 2 node, in bubble pixels
LINKS = [(0, 1), (1, 2), (2, 3)]
PUFFS = [((3, 12), 2), ((1, 14), 1)]  # little circles leading down to Clawd's head: (top-left, size)


def _grid(w, h):
    return [["."] * w for _ in range(h)]


def _line(a, b):
    """Bresenham from a to b, both ends included."""
    (x0, y0), (x1, y1) = a, b
    dx, dy, sx, sy = abs(x1 - x0), -abs(y1 - y0), 1 if x1 > x0 else -1, 1 if y1 > y0 else -1
    err, out = dx + dy, []
    while True:
        out.append((x0, y0))
        if (x0, y0) == (x1, y1):
            return out
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _center(i):
    x, y = NODES[i]
    return x + 1, y + 1


def _cloud(f):
    cx, cy, rx, ry = 10, 5.5, 9.2, 5.6

    def inside(x, y):
        return ((x + 0.5 - cx) / rx) ** 2 + ((y + 0.5 - cy) / ry) ** 2 <= 1
    for y in range(BH):
        for x in range(BW):
            if inside(x, y):
                edge = not all(inside(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
                f[y][x] = "o" if edge else "f"
    for (x, y), size in PUFFS:
        for yy in range(y, y + size):
            for xx in range(x, x + size):
                f[yy][xx] = "o"


def _node(f, i, body="n", shine="h"):
    x, y = NODES[i]
    f[y][x], f[y][x + 1], f[y + 1][x], f[y + 1][x + 1] = shine, body, body, body


def _link(f, k, part=1.0, color="l", dotted=False, tip=None):
    a, b = LINKS[k]
    pts = _line(_center(a), _center(b))
    shown = pts[:max(1, round(len(pts) * part))]
    for j, (x, y) in enumerate(shown):
        if not dotted or j % 2 == 0:
            f[y][x] = color
    if tip:
        x, y = shown[-1]
        f[y][x] = tip


def bubble_frames(lit: bool) -> list:
    """The bubble's loop. lit: the graph builds node by node, a pulse at each link's growing tip; it glows twice,
    dims and starts over. idle: a dim, dotted graph whose nodes twinkle in turn while a speck rises into it."""
    out = []
    if lit:
        steps = [((), {}), ((0,), {})]
        for k in range(3):
            done = {j: 1.0 for j in range(k)}
            seen = tuple(range(k + 1))
            steps += [(seen, {**done, k: 1 / 3}), (seen, {**done, k: 2 / 3}), (seen + (k + 1,), {**done, k: 1.0})]
        whole = ((0, 1, 2, 3), {0: 1.0, 1: 1.0, 2: 1.0})
        steps += [whole + ("glow",), whole, whole + ("glow",), whole, whole + ("dim",), whole + ("dim",)]
        for step in steps:
            nodes, links, *mode = step
            f = _grid(BW, BH)
            _cloud(f)
            for k, part in links.items():
                growing = part < 1
                color = "e" if mode == ["dim"] else "p" if mode == ["glow"] else "l"
                _link(f, k, part, color, tip="p" if growing else None)
            for i in nodes:
                if mode == ["dim"]:
                    _node(f, i, "d", "d")
                elif mode == ["glow"]:
                    _node(f, i, "p", "p")
                else:
                    _node(f, i)
            out.append(f)
        return out
    rise = [(1, 13), (3, 11), (5, 10), None, None, None, None, None]
    for t in range(8):
        f = _grid(BW, BH)
        _cloud(f)
        for k in range(len(LINKS)):
            _link(f, k, color="e", dotted=True)
        for i in range(len(NODES)):
            _node(f, i, "g", "g") if i == t // 2 else _node(f, i, "d", "d")
        if rise[t]:
            x, y = rise[t]
            f[y][x] = "n"
        out.append(f)
    return out


# The stand-in Clawd: 12 x 8 pixels of 8 frame pixels each, where the pet's Clawd stands.
OWN = ["..xxxxxxxx..", "..xxxxxxxx..", "xxxxxxxxxxxx", "xxxxxxxxxxxx",
       "..xxxxxxxx..", "..ssssssss..", "..s.s..s.s..", "..s.s..s.s.."]


def own_frames(lit: bool) -> list:
    """lit: eyes on the bubble, one blink. idle: looks ahead, right, ahead, left, blinks."""
    looks = [1, 1, 1, 1, 1, 1, 1, None] if lit else [0, 0, 0, 1, 1, 0, -1, None]
    out = []
    for look in looks:
        f = [list(row) for row in OWN]
        if look is not None:
            for x in (3, 8):
                f[1][x + look] = "k"
        out.append(f)
    return out


def _runs(f, keep, ox, oy, cell):
    """Horizontal runs of one color as rects; keep(x, y) picks the pixels drawn."""
    out = []
    for y, row in enumerate(f):
        x = 0
        while x < len(row):
            c = row[x]
            if c == "." or not keep(x, y):
                x += 1
                continue
            end = x + 1
            while end < len(row) and row[end] == c and keep(end, y):
                end += 1
            color, alpha = COLORS[c]
            out.append(f'<rect x="{ox + x * cell}" y="{oy + y * cell}" width="{(end - x) * cell}" height="{cell}"'
                       f' fill="{color}"{"" if alpha == 1 else f" fill-opacity={chr(34)}{alpha}{chr(34)}"}/>')
            x = end
    return "".join(out)


def loop_layer(frames_: list, ox: int, oy: int, cell: int) -> str:
    """The pixels every frame shares drawn once; each frame's own pixels in a group shown in its turn."""
    n = len(frames_)
    dur = f"{n * STEP_MS / 1000:.2f}s"

    def same(x, y):
        return all(f[y][x] == frames_[0][y][x] for f in frames_)
    body = _runs(frames_[0], same, ox, oy, cell)
    for k, f in enumerate(frames_):
        values = ";".join("visible" if i == k else "hidden" for i in range(n))
        body += (f'<g visibility="{"visible" if k == 0 else "hidden"}"><animate attributeName="visibility"'
                 f' values="{values}" dur="{dur}" calcMode="discrete" repeatCount="indefinite"/>'
                 f'{_runs(f, lambda x, y: not same(x, y), ox, oy, cell)}</g>')
    return f'<g shape-rendering="crispEdges">{body}</g>'


def own_layer(lit: bool) -> str:
    return loop_layer(own_frames(lit), CLAWD[0], 64, 8)


def bubble(lit: bool) -> str:
    return loop_layer(bubble_frames(lit), 0, 0, CELL)


def compose(clawd_layer: str, lit: bool) -> dict:
    x0, y0, x1, y1 = VIEW
    bx, by = BUBBLE_AT
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0} {y0} {x1 - x0} {y1 - y0}">'
           f'{clawd_layer}<g transform="translate({bx} {by})">{bubble(lit)}</g></svg>')
    return {"svg": svg, "width": round((x1 - x0) * SCALE), "height": round((y1 - y0) * SCALE)}


def build() -> dict:
    root = frames_root()
    sets = {k: frames(root / name) for k, name in ANIMATIONS.items()} if root else {}
    if sets and all(sets.values()):
        out = {k: compose(pet_layer(pngs), k == "lit") for k, pngs in sets.items()}
        if all(len(v["svg"]) <= MAX_SVG for v in out.values()):
            return {**out, "source": f"clawd-pet {root.parent.name}"}
    return {"lit": compose(own_layer(True), True), "idle": compose(own_layer(False), False), "source": "own"}


def main(argv=None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        out = build() if args[:1] == ["svg"] else {"error": "usage: clawd.py svg"}
    except Exception as e:  # one JSON answer either way; the pane then shows no Clawd
        out = {"error": f"{type(e).__name__}: {e}", "source": ""}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
