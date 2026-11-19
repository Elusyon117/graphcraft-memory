r"""memory_search and memory_remember. Run: py -I tests/test_tools.py"""
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "graph"))
import capture  # noqa: E402
import store  # noqa: E402
import tools  # noqa: E402

PROJECT = r"C:\mods\Campfires"
FACTS = {  # made-up example facts in the core vocabulary
    "nodes": [{"name": "Campfires", "type": "PROJECT"},
              {"name": "Post-process AnimBP swap crash", "type": "PROBLEM", "description": "Crashed in build 3."},
              {"name": "Linked anim layer", "type": "DECISION"},
              {"name": "Zone Kit", "type": "TOOL", "description": "The editor; slow to cook."}],
    "edges": [{"source": "Linked anim layer", "predicate": "because", "target": "Post-process AnimBP swap crash"},
              {"source": "Post-process AnimBP swap crash", "predicate": "fixed_by", "target": "Linked anim layer"},
              {"source": "Campfires", "predicate": "uses", "target": "Zone Kit"}],
}


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["GRAPHCRAFT_DB"] = str(Path(self.tmp.name) / "graph.db")

    def tearDown(self):
        del os.environ["GRAPHCRAFT_DB"]
        self.tmp.cleanup()

    def seed(self):
        with closing(store.connect()) as db:
            store.upsert(db, FACTS, store.add_origin(db, "session", "s-0", PROJECT), store.load_vocabulary())

    def remember(self, batch, session="s-1"):
        f = Path(self.tmp.name) / "batch.json"
        f.write_text(json.dumps(batch), encoding="utf-8")
        out = tools.remember(str(f), session, PROJECT)
        self.assertFalse(f.exists())  # the batch file is consumed
        return out

    # --- search ---

    def test_empty_memory_says_so(self):
        self.assertEqual(tools.search("anything")["facts"], 0)

    def test_search_by_name(self):
        self.seed()
        out = tools.search("why did the Linked anim layer get picked?", PROJECT)
        self.assertEqual(out["facts"], 2)
        self.assertIn("Linked anim layer --[because]--> Post-process AnimBP swap crash", out["text"])
        self.assertNotIn("Zone Kit", out["text"])  # the project is not pulled in by a targeted search

    def test_search_by_words_when_no_name_matches(self):
        self.seed()
        out = tools.search("post-process swap")
        self.assertEqual(out["facts"], 2)
        self.assertIn("fixed_by", out["text"])

    def test_search_falls_back_to_descriptions(self):
        self.seed()
        self.assertIn("Campfires --[uses]--> Zone Kit", tools.search("slow cook")["text"])

    # --- remember ---

    def test_remember_stores_under_the_session_and_undo_removes_it(self):
        out = self.remember({"nodes": [{"name": "Campfires", "type": "PROJECT"},
                                       {"name": "Short answers", "type": "PREFERENCE", "scope": "global"},
                                       {"name": "User", "type": "PERSON"}],
                             "edges": [{"source": "User", "predicate": "prefers", "target": "Short answers"},
                                       {"source": "User", "predicate": "works_on", "target": "Campfires"}]})
        self.assertEqual((out["facts"], out["dropped"]), (2, []))
        with closing(store.connect()) as db:
            self.assertEqual(db.execute("SELECT kind, ref, project FROM origins").fetchone(), ("session", "s-1", PROJECT))
            scopes = dict(db.execute("SELECT name, scope FROM entities"))
        self.assertEqual(scopes["Short answers"], "global")
        self.assertEqual(scopes["Campfires"], PROJECT)
        self.assertEqual(capture.recent(1)["captures"][0]["event"], "remembered")
        self.assertEqual(capture.undo("s-1")["relations"], 2)

    def test_remember_drops_and_reports_wrong_links(self):
        out = self.remember({"nodes": [{"name": "Zone Kit", "type": "TOOL"}, {"name": "Cook time", "type": "PROBLEM"},
                                       {"name": "Bigger cache decision", "type": "DECISION"}],
                             "edges": [{"source": "Zone Kit", "predicate": "about", "target": "Cook time"},
                                       {"source": "Bigger cache decision", "predicate": "because",
                                        "target": "Cook time"}]})
        self.assertEqual(out["facts"], 1)
        self.assertEqual(len(out["dropped"]), 1)
        with closing(store.connect()) as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM entities WHERE name='Bigger cache'").fetchone())

    def test_remember_can_retire_an_old_fact(self):
        self.seed()
        out = self.remember({"nodes": [], "edges": [],
                             "retire": [{"source": "Campfires", "predicate": "uses", "target": "Zone Kit"}]})
        self.assertEqual(out["retired"], 1)
        self.assertNotIn("Zone Kit", tools.search("Campfires", PROJECT)["text"])

    def test_remember_refuses_a_flood(self):
        edges = [{"source": "User", "predicate": "prefers", "target": f"P{i}"} for i in range(16)]
        self.assertIn("at most 15", self.remember({"nodes": [], "edges": edges})["error"])

    def test_vocab_lists_core(self):
        v = tools.vocab()
        self.assertIn("DECISION", v["types"])
        self.assertIn("because", v["predicates"])


if __name__ == "__main__":
    unittest.main()
