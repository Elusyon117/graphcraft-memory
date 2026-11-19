r"""Clawd and his memory bubble. Run: py -I tests/test_clawd.py

The "pet frames" here are made-up 1-pixel PNGs written by the test, not the clawd-pet mod's artwork.
"""
import os
import re
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "graph"))
import clawd  # noqa: E402


def png() -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 192, 128, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0" + b"\0" * 4 * 192)) + chunk(b"IEND", b""))


class ClawdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            del os.environ["CLAUDE_CONFIG_DIR"]
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self.old
        self.tmp.cleanup()

    def install_pet(self, counts=(("Thinking", 3), ("LookingAround", 2))):
        frames = Path(self.tmp.name, "plugins", "cache", "market", "clawd-pet", "1.0.0", "frames")
        for name, n in counts:
            (frames / name).mkdir(parents=True)
            for i in range(n):
                (frames / name / f"{i}.png").write_bytes(png())
            (frames / name / "0m.png").write_bytes(png())  # a mirrored frame: left out

    def check(self, out: dict):
        for key in ("lit", "idle"):
            svg = out[key]["svg"]
            self.assertTrue(svg.startswith("<svg") and svg.endswith("</svg>"))
            self.assertNotIn("<script", svg)
            self.assertNotRegex(svg, r"\son\w+=")
            self.assertLessEqual(len(svg), clawd.MAX_SVG)
            self.assertEqual((out[key]["width"], out[key]["height"]), (77, 64))

    def test_without_the_pet_mod_the_stand_in_clawd_is_drawn(self):
        out = clawd.build()
        self.assertEqual(out["source"], "own")
        self.check(out)
        self.assertNotIn("<image", out["lit"]["svg"])

    def test_with_the_pet_mod_its_frames_play_in_turn(self):
        self.install_pet()
        out = clawd.build()
        self.assertEqual(out["source"], "clawd-pet 1.0.0")
        self.check(out)
        self.assertEqual(out["lit"]["svg"].count("<image "), 3)  # Thinking, mirrored frame left out
        self.assertEqual(out["idle"]["svg"].count("<image "), 2)
        self.assertIn('calcMode="discrete"', out["lit"]["svg"])
        self.assertIn('clip-path="url(#clawd)"', out["lit"]["svg"])  # the pet's own bubble is cut off

    def test_a_missing_animation_falls_back(self):
        self.install_pet(counts=(("Thinking", 2),))
        self.assertEqual(clawd.build()["source"], "own")

    def test_lit_bubble_builds_the_graph_step_by_step(self):
        frames = clawd.bubble_frames(True)
        nodes = [sum(row.count("n") for row in f) // 3 for f in frames]  # a lit node is 3 "n" and 1 "h"
        built = nodes[:nodes.index(4) + 1]
        self.assertEqual(built[0], 0)
        self.assertEqual(built, sorted(built))  # nodes only appear while it builds
        self.assertTrue(any("p" in "".join(map("".join, f)) for f in frames[2:4]))  # the growing link's bright tip
        self.assertTrue(all("d" in "".join(map("".join, f)) for f in frames[-2:]))  # it dims before starting over

    def test_idle_bubble_twinkles_each_node_in_turn(self):
        frames = clawd.bubble_frames(False)
        lit_nodes = [next(i for i, (x, y) in enumerate(clawd.NODES) if f[y][x] == "g") for f in frames]
        self.assertEqual(sorted(set(lit_nodes)), list(range(len(clawd.NODES))))

    def test_frames_share_one_grid_and_loop_at_a_steady_step(self):
        for lit in (True, False):
            frames = clawd.bubble_frames(lit)
            self.assertEqual({(len(f), len(f[0])) for f in frames}, {(clawd.BH, clawd.BW)})
            svg = clawd.bubble(lit)
            self.assertIn(f'dur="{len(frames) * clawd.STEP_MS / 1000:.2f}s"', svg)
            self.assertEqual(svg.count("<g visibility="), len(frames))
            self.assertIn('shape-rendering="crispEdges"', svg)

    def test_every_animation_has_valid_key_times(self):
        # A browser ignores an animation whose keyTimes do not start at 0, go backwards, or (unless discrete) end
        # before 1: the step it was meant to show would silently never happen.
        for svg in (clawd.bubble(True), clawd.bubble(False), clawd.own_layer(True), clawd.own_layer(False)):
            for tag in re.findall(r"<animate[^>]*>", svg):
                m = re.search(r'keyTimes="([^"]+)"', tag)
                if not m:
                    continue
                times = [float(x) for x in m.group(1).split(";")]
                self.assertEqual(times[0], 0, tag)
                self.assertEqual(times, sorted(times), tag)
                if 'calcMode="discrete"' not in tag:
                    self.assertEqual(times[-1], 1, tag)


if __name__ == "__main__":
    unittest.main()
