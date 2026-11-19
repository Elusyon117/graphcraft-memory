"""Project docs: front-matter facts load with no model; key docs are listed for the model and stored as snapshots."""
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "graph"))

import docs  # noqa: E402
import store  # noqa: E402

FRONT = """---
title: Campfires notes
graphcraft:
  nodes:
    - Campfires: PROJECT
    - Linked anim layer: DECISION | Chosen over the AnimBP swap, which crashed in build 3.
    - Swap crash: PROBLEM
  links:
    - Campfires decided Linked anim layer
    - Linked anim layer because Swap crash
    - this line has no predicate at all
  aliases:
    - Linked anim layer: linked layer
---
# Notes
Body text the front-matter reader never looks at.
"""


class DocsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["GRAPHCRAFT_DB"] = str(root / "data" / "graph.db")
        self.proj = root / "Campfires"
        (self.proj / "docs").mkdir(parents=True)
        (self.proj / "node_modules" / "x").mkdir(parents=True)

    def tearDown(self):
        os.environ.pop("GRAPHCRAFT_DB", None)
        self.tmp.cleanup()

    def live(self):
        with closing(store.connect()) as db:
            return {(s, p, t) for s, p, t in db.execute(
                "SELECT es.name, r.predicate, et.name FROM relations r JOIN entities es ON es.id=r.source_id"
                " JOIN entities et ON et.id=r.target_id WHERE r.retired IS NULL")}

    def test_front_matter_loads_without_a_model(self):
        (self.proj / "notes.md").write_text(FRONT, encoding="utf-8")
        (self.proj / "node_modules" / "x" / "skip.md").write_text(FRONT.replace("Campfires", "Other"), encoding="utf-8")
        out = docs.scan(str(self.proj))
        self.assertEqual(out["facts"], 2)
        self.assertIn(("Campfires", "decided", "Linked anim layer"), self.live())
        self.assertIn(("Linked anim layer", "because", "Swap crash"), self.live())
        self.assertTrue(any("no predicate" in p for p in out["problems"]))
        self.assertFalse(any("Other" in s for s, _, _ in self.live()))  # node_modules is not read
        with closing(store.connect()) as db:
            self.assertEqual(store._find_by_name(db, "linked layer"), store._find_by_name(db, "Linked anim layer"))
            desc = db.execute("SELECT description FROM entities WHERE name='Linked anim layer'").fetchone()[0]
        self.assertIn("build 3", desc)
        self.assertEqual(docs.scan(str(self.proj))["facts"], 0)  # unchanged: not loaded again

    def test_a_changed_or_deleted_doc_retires_only_its_own_facts(self):
        f = self.proj / "notes.md"
        f.write_text(FRONT, encoding="utf-8")
        docs.scan(str(self.proj))
        with closing(store.connect()) as db:  # a session said the same decision
            o = store.add_origin(db, "session", "s1")
            store.upsert(db, {"edges": [{"source": "Campfires", "predicate": "decided", "target": "Linked anim layer"}]},
                         o, store.load_vocabulary())
            db.commit()
        f.write_text(FRONT.replace("    - Linked anim layer because Swap crash\n", ""), encoding="utf-8")
        out = docs.scan(str(self.proj))
        self.assertEqual(out["retired"], 1)
        self.assertNotIn(("Linked anim layer", "because", "Swap crash"), self.live())
        f.unlink()
        docs.scan(str(self.proj))
        self.assertIn(("Campfires", "decided", "Linked anim layer"), self.live())  # the session's record stays

    def test_key_docs_go_to_the_model_once_per_change(self):
        (self.proj / "CLAUDE.md").write_text("# Campfires\nUse a linked anim layer.", encoding="utf-8")
        (self.proj / "docs" / "DESIGN.md").write_text("# Design\nThe swap crashed.", encoding="utf-8")
        (self.proj / "README.md").write_text("# Readme", encoding="utf-8")
        jobs = docs.scan(str(self.proj))["jobs"]
        self.assertEqual(sorted(Path(j).name for j in jobs), ["CLAUDE.md", "DESIGN.md"])

        prep = docs.prepare(jobs[0], str(self.proj))
        self.assertIn("project document", prep["system"])
        self.assertNotIn("{types}", prep["system"])
        self.assertIn('<document path="Campfires/', prep["prompt"])

        claude = next(j for j in jobs if j.endswith("CLAUDE.md"))
        reply = Path(self.tmp.name) / "reply.txt"
        reply.write_text(json.dumps({"title": "x", "nodes": [{"name": "Linked anim layer", "type": "DECISION"},
                                                             {"name": "Campfires", "type": "PROJECT"}],
                                     "edges": [{"source": "Campfires", "predicate": "decided", "target": "Linked anim layer"}],
                                     "retire": [{"source": "a", "predicate": "uses", "target": "b"}]}), encoding="utf-8")
        out = docs.apply(claude, str(reply), str(self.proj))
        self.assertEqual(out["facts"], 1)
        self.assertIn(("Campfires", "decided", "Linked anim layer"), self.live())
        again = docs.scan(str(self.proj))["jobs"]
        self.assertEqual([Path(j).name for j in again], ["DESIGN.md"])  # CLAUDE.md unchanged since it was read

    def test_home_folder_is_never_walked(self):
        self.assertEqual(docs.scan(os.path.expanduser("~"))["jobs"], [])

    def test_doc_prompt_has_no_conversation_left(self):
        p = docs._doc_prompt()
        self.assertNotIn("conversation", p.lower())
        self.assertNotIn('list it under "retire"', p)


if __name__ == "__main__":
    unittest.main()
