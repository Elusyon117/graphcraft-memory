r"""Clawd from the clawd-pet mod's frames. Run: py -I tests/test_clawd.py

The frames here are made-up 2-pixel PNGs written by the test, not the pet mod's artwork.
"""
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "graph"))
import clawd  # noqa: E402


def png(w: int, h: int, opaque: set) -> bytes:
    """An 8-bit RGBA PNG with the given (x, y) pixels opaque, every row stored with filter 0."""
    rows = b"".join(b"\0" + b"".join(b"\xd9\x77\x57\xff" if (x, y) in opaque else b"\0\0\0\0" for x in range(w))
                    for y in range(h))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


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

    def install_pet(self):
        frames = Path(self.tmp.name, "plugins", "cache", "market", "clawd-pet", "1.0.0", "frames")
        for name, spots in (("Lightbulb", [{(1, 1)}, {(5, 2)}]), ("LookingAround", [{(2, 6)}])):
            (frames / name).mkdir(parents=True)
            for i, opaque in enumerate(spots):
                (frames / name / f"{i}.png").write_bytes(png(8, 8, opaque))
            (frames / name / "0m.png").write_bytes(png(8, 8, {(7, 7)}))  # a mirrored frame: left out

    def test_alpha_box(self):
        self.assertEqual(clawd.png_alpha_box(png(6, 4, {(1, 1), (3, 2)})), (1, 1, 4, 3))
        self.assertIsNone(clawd.png_alpha_box(png(4, 4, set())))
        self.assertIsNone(clawd.png_alpha_box(b"not a png"))

    def test_no_pet_mod_means_no_svg(self):
        self.assertEqual(clawd.build(), {"source": ""})

    def test_both_animations_share_one_box_and_loop(self):
        self.install_pet()
        out = clawd.build()
        self.assertEqual(out["source"], "clawd-pet 1.0.0")
        for key, frames in (("lit", 2), ("idle", 1)):
            svg = out[key]["svg"]
            self.assertIn('viewBox="1 1 5 6"', svg)  # (1,1)..(6,7): every frame of both animations
            self.assertEqual(svg.count("<image "), frames)
            self.assertIn('calcMode="discrete"', svg)
            self.assertNotIn("<script", svg)
        self.assertEqual((out["lit"]["width"], out["lit"]["height"]), (out["idle"]["width"], out["idle"]["height"]))

    def test_result_is_cached_until_frames_change(self):
        self.install_pet()
        first = clawd.build()
        self.assertTrue(clawd.cache_file().exists())
        self.assertEqual(clawd.build(), first)
        f = Path(self.tmp.name, "plugins", "cache", "market", "clawd-pet", "1.0.0", "frames", "LookingAround", "1.png")
        f.write_bytes(png(8, 8, {(0, 0)}))
        self.assertIn('viewBox="0 0', clawd.build()["idle"]["svg"])


if __name__ == "__main__":
    unittest.main()
