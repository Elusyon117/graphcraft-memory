"""Worked files: a session's edited repos get each worked file's outline; unchanged files are not read twice."""
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "graph"))

import files  # noqa: E402
import store  # noqa: E402

KIT = """// Shared helpers for every template: timing, cards and titles.
import { imageLayer } from './model';
export const at = () => 1;
export function glass() {}
export type Folder = string;
export { kinetic as words, sweep };
"""
TRADE = """// Trading templates: five stills and five animations.
import { at } from './templateKit';
export const STILL_TRADE = [];
"""
PY = '''"""Capture: turn saved conversations into facts.

More detail the outline leaves out.
"""
def prepare(): pass
def _private(): pass
class Job: pass
'''


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["GRAPHCRAFT_DB"] = str(root / "data" / "graph.db")
        self.repo = root / "Glovebox"
        (self.repo / ".git").mkdir(parents=True)
        src = self.repo / "src"
        src.mkdir()
        self.kit, self.trade, self.py = src / "templateKit.ts", src / "tradeTemplates.ts", src / "capture.py"
        self.kit.write_text(KIT, encoding="utf-8")
        self.trade.write_text(TRADE, encoding="utf-8")
        self.py.write_text(PY, encoding="utf-8")
        (src / "notes.md").write_text("# not code", encoding="utf-8")
        other = root / "Lookonly"
        (other / ".git").mkdir(parents=True)
        self.looked = other / "a.ts"
        self.looked.write_text("// only read\nexport const x = 1;\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("GRAPHCRAFT_DB", None)

    def worked(self, files_: dict, sid="s1"):
        d = files.worked_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.json").write_text(json.dumps({"sessionId": sid, "files": files_}), encoding="utf-8")

    def facts(self):
        with closing(store.connect()) as db:
            return {(s, p, t) for s, p, t in db.execute(
                "SELECT a.name, r.predicate, b.name FROM relations r JOIN entities a ON a.id=r.source_id"
                " JOIN entities b ON b.id=r.target_id WHERE r.retired IS NULL")}

    def desc(self, name):
        with closing(store.connect()) as db:
            return db.execute("SELECT description FROM entities WHERE name=?", (name,)).fetchone()[0]

    def test_outlines(self):
        self.assertEqual(files.outline(self.kit, KIT), "Shared helpers for every template: timing, cards and titles. Exports at, glass, Folder, words, sweep.")
        self.assertEqual(files.outline(self.py, PY), "Capture: turn saved conversations into facts. Exports prepare, Job.")

    def test_learns_edited_repo_only(self):
        self.worked({str(self.trade): "edit", str(self.kit): "read", str(self.py): "read",
                     str(self.repo / "src" / "notes.md"): "read", str(self.looked): "read"})
        out = files.learn()
        self.assertEqual(out["files"], 3)
        got = self.facts()
        self.assertIn(("src/tradeTemplates.ts", "part_of", "Glovebox"), got)
        self.assertIn(("src/tradeTemplates.ts", "depends_on", "src/templateKit.ts"), got)
        self.assertFalse(any("a.ts" in s or "notes.md" in s for s, _, _ in got))  # a repo only read, and docs
        self.assertIn("Exports at, glass", self.desc("src/templateKit.ts"))
        self.assertFalse(files.worked_dir().joinpath("s1.json").exists())

        # Unchanged: nothing is read again. Changed: the snapshot moves, and a dropped import is retired.
        self.worked({str(self.trade): "edit", str(self.kit): "read"}, "s2")
        self.assertEqual(files.learn()["files"], 0)
        self.trade.write_text("// Trading templates, now standalone.\nexport const VIDEO_TRADE = [];\n", encoding="utf-8")
        self.worked({str(self.trade): "edit", str(self.kit): "read"}, "s3")
        out = files.learn()
        self.assertEqual((out["files"], out["retired"]), (1, 1))
        self.assertNotIn(("src/tradeTemplates.ts", "depends_on", "src/templateKit.ts"), self.facts())
        self.assertEqual(self.desc("src/tradeTemplates.ts"), "Trading templates, now standalone. Exports VIDEO_TRADE.")

    def test_no_lists_no_work(self):
        self.assertEqual(files.learn(), {"sessions": 0, "files": 0, "facts": 0, "retired": 0})


if __name__ == "__main__":
    unittest.main()
