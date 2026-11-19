r"""Clawd for the Memory pane: Clawd thinking, with a memory bubble that builds a little knowledge graph.

    py -I clawd.py svg       {"lit": {"svg", "width", "height"}, "idle": {...}, "source": "clawd-pet 0.29.5" | "own"}

- lit (facts were recalled for the last prompt): Clawd ponders while, in the bubble, nodes pop in one by one, links
  draw between them and a pulse walks along the links, as recall walks the graph; then it all glows, fades, repeats.
- idle: Clawd looks around; the bubble rests dim, its nodes twinkle, and specks rise from his head into it.

Clawd himself comes from the clawd-pet mod's frames when that mod is installed (Anthropic's artwork, which that mod's
NOTICE keeps outside its license). They are read from the user's own install at run time and never copied into this
repo; only PNG files are read, no code of the other mod runs (CLAUDE.md rule 4). Without it, a small Clawd drawn here
stands in. Everything animates with SMIL (no script), so the pane's sandboxed Svg plays it.
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
INK, NODE, LINK, PULSE = "#9A9893", "#D97757", "#E3B79C", "#FFF3C4"
EASE = 'calcMode="spline" keySplines="0.45 0 0.55 1;0.45 0 0.55 1"'
LOOP = 3.6  # seconds, the bubble's lit cycle


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


# The stand-in Clawd: 12 x 8 cells of 8 frame pixels, where the pet's Clawd stands. x body, s shaded, a arm, l leg.
OWN = ["..xxxxxxxx..", "..xxxxxxxx..", "aaxxxxxxxxaa", "aaxxxxxxxxaa",
       "..xxxxxxxx..", "..ssssssss..", "..l.l..l.l..", "..l.l..l.l.."]


def own_layer(lit: bool) -> str:
    cell, x0, y0 = 8, CLAWD[0], 64
    rects = [f'<rect x="{x0 + x * cell}" y="{y0 + y * cell}" width="{cell}" height="{cell}"'
             f' fill="{SHADE if c in "sl" else BODY}"/>'
             for y, row in enumerate(OWN) for x, c in enumerate(row) if c != "."]
    blink = ('<animate attributeName="height" values="8;8;1;8;8" keyTimes="0;0.9;0.92;0.95;1" dur="5s"'
             ' repeatCount="indefinite"/>')
    if lit:  # eyes on the bubble
        look = '<animateTransform attributeName="transform" type="translate" values="4 -2" dur="1s" fill="freeze"/>'
    else:  # glance right, back, left, back
        look = ('<animateTransform attributeName="transform" type="translate" values="0 0;0 0;6 0;6 0;0 0;0 0;-6 0;-6 0;0 0"'
                ' keyTimes="0;0.15;0.2;0.38;0.43;0.58;0.63;0.8;0.85" calcMode="discrete" dur="5s" repeatCount="indefinite"/>')
    eyes = "".join(f'<rect x="{x0 + c * cell}" y="{y0 + cell}" width="{cell}" height="{cell}" fill="{EYE}">{blink}</rect>'
                   for c in (3, 8))
    breathe = (f'<animateTransform attributeName="transform" type="translate" values="0 0;0 -3;0 0" keyTimes="0;0.5;1"'
               f' dur="2.4s" repeatCount="indefinite" {EASE}/>')
    return f'<g shape-rendering="crispEdges">{breathe}{"".join(rects)}<g>{look}{eyes}</g></g>'


# The bubble, in its own 82 x 64 space: a cloud, two puffs leading down to Clawd's head, and a four-node graph.
NODES = [(26, 32), (40, 14), (62, 20), (54, 40)]
LINKS = [(0, 1), (1, 2), (2, 3)]


def _t(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".") or "0"


def bubble(lit: bool) -> str:
    cloud = (f'<ellipse cx="44" cy="27" rx="35" ry="23" fill="{INK}" fill-opacity="0.12" stroke="{INK}"'
             f' stroke-width="1.6"/><circle cx="11" cy="53" r="4" fill="{INK}" fill-opacity="0.12" stroke="{INK}"'
             f' stroke-width="1.4"/><circle cx="3.5" cy="60" r="2.4" fill="{INK}" fill-opacity="0.12" stroke="{INK}"'
             f' stroke-width="1.2"/>')
    if not lit:
        return cloud + _idle_graph()
    return cloud + _lit_graph()


def _idle_graph() -> str:
    out = []
    for a, b in LINKS:
        (x1, y1), (x2, y2) = NODES[a], NODES[b]
        out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{INK}" stroke-width="1.2"'
                   f' stroke-dasharray="2 2.5" opacity="0.45"/>')
    for i, (x, y) in enumerate(NODES):
        out.append(f'<circle cx="{x}" cy="{y}" r="3" fill="{INK}" opacity="0.4">'
                   f'<animate attributeName="opacity" values="0.3;0.85;0.3" keyTimes="0;0.5;1" dur="3.2s"'
                   f' begin="{i * 0.8:.1f}s" repeatCount="indefinite" {EASE}/></circle>')
    # Specks rise from Clawd's head into the bubble, one after another: memories forming.
    for i in range(3):
        out.append(f'<circle r="1.6" fill="{NODE}" opacity="0">'
                   f'<animateMotion path="M2 64 Q8 50 20 40" dur="3s" begin="{i:.0f}s" repeatCount="indefinite"/>'
                   f'<animate attributeName="opacity" values="0;0.9;0" keyTimes="0;0.3;1" dur="3s" begin="{i:.0f}s"'
                   f' repeatCount="indefinite"/></circle>')
    return "".join(out)


def _lit_graph() -> str:
    # One loop: node 0 pops, then each link draws and its far node pops, a pulse riding along; at 0.78 everything
    # glows, at 0.9 it fades, and the loop starts over.
    pops = [0.04, 0.27, 0.47, 0.67]
    draws = [(0.1, 0.26), (0.3, 0.46), (0.5, 0.66)]
    glow, fade = 0.78, 0.9
    common = f'dur="{LOOP}s" repeatCount="indefinite"'
    out = []
    for (a, b), (s, e) in zip(LINKS, draws):
        (x1, y1), (x2, y2) = NODES[a], NODES[b]
        out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{LINK}" stroke-width="1.6"'
                   f' stroke-linecap="round" pathLength="1" stroke-dasharray="1" stroke-dashoffset="1">'
                   f'<animate attributeName="stroke-dashoffset" values="1;1;0;0" keyTimes="0;{_t(s)};{_t(e)};1"'
                   f' {common}/></line>')
    for (x, y), p in zip(NODES, pops):
        r = (f'<animate attributeName="r" values="0;0;4.2;3.2;3.2;3.9;3.2" keyTimes="0;{_t(p)};{_t(p + 0.05)};'
             f'{_t(p + 0.09)};{_t(glow)};{_t(glow + 0.05)};1" {common}/>')
        out.append(f'<circle cx="{x}" cy="{y}" r="6" fill="{NODE}" opacity="0">'  # the glow halo
                   f'<animate attributeName="opacity" values="0;0;0.35;0;0" keyTimes="0;{_t(glow)};{_t(glow + 0.05)};'
                   f'{_t(fade)};1" {common}/></circle>'
                   f'<circle cx="{x}" cy="{y}" r="0" fill="{NODE}">{r}</circle>'
                   f'<circle cx="{x - 0.9}" cy="{y - 0.9}" r="0.9" fill="{PULSE}" opacity="0">'
                   f'<animate attributeName="opacity" values="0;0;0.9;0.9" keyTimes="0;{_t(p + 0.06)};'
                   f'{_t(p + 0.09)};1" {common}/></circle>')
    path = "M" + " L".join(f"{x} {y}" for x, y in NODES)
    out.append(f'<circle r="2" fill="{PULSE}" opacity="0">'
               f'<animateMotion path="{path}" keyPoints="0;0;0.333;0.333;0.667;0.667;1;1"'
               f' keyTimes="0;{_t(draws[0][0])};{_t(draws[0][1])};{_t(draws[1][0])};{_t(draws[1][1])};{_t(draws[2][0])};'
               f'{_t(draws[2][1])};1" calcMode="linear" {common}/>'
               f'<animate attributeName="opacity" values="0;0;1;1;0;0" keyTimes="0;{_t(draws[0][0])};'
               f'{_t(draws[0][0] + 0.02)};{_t(draws[2][1])};{_t(draws[2][1] + 0.03)};1" {common}/></circle>')
    # The whole graph fades out at the end of the loop, then builds again.
    return (f'<g><animate attributeName="opacity" values="1;1;0;0" keyTimes="0;{_t(fade)};{_t(fade + 0.07)};1"'
            f' {common}/>{"".join(out)}</g>')


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
