r"""The memory pane's data and the session brief. Run: py -I tests/test_pane.py"""
import os
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "graph"))
import pane  # noqa: E402
import recall  # noqa: E402
import store  # noqa: E402

VOCAB = store.load_vocabulary()
FIRST = {  # made-up example facts in the core vocabulary
    "nodes": [{"name": "Campfires", "type": "PROJECT"}, {"name": "Swap crash", "type": "PROBLEM"},
              {"name": "Linked layer", "type": "DECISION"}, {"name": "Bedroll conflict", "type": "PROBLEM"}],
    "edges": [{"source": "Campfires", "predicate": "blocked_by", "target": "Swap crash"},
              {"source": "Swap crash", "predicate": "fixed_by", "target": "Linked layer"},
              {"source": "Campfires", "predicate": "decided", "target": "Linked layer"},
              {"source": "Campfires", "predicate": "blocked_by", "target": "Bedroll conflict"}],
}
SECOND = {
    "nodes": [{"name": "User", "type": "PERSON"}, {"name": "Short replies", "type": "PREFERENCE"},
              {"name": "Avoid AnimBP override", "type": "DECISION"}],
    "edges": [{"source": "User", "predicate": "prefers", "target": "Short replies"},
              {"source": "Campfires", "predicate": "decided", "target": "Avoid AnimBP override"}],
}
CWD = r"C:\mods\Campfires"


class PaneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["GRAPHCRAFT_DB"] = str(Path(self.tmp.name) / "graph.db")
        with closing(store.connect()) as db:
            store.upsert(db, FIRST, store.add_origin(db, "session", "s-1", CWD), VOCAB)
            time.sleep(0.01)  # the second session is newer by origin time and relation time
            store.upsert(db, SECOND, store.add_origin(db, "session", "s-2", CWD), VOCAB)

    def tearDown(self):
        del os.environ["GRAPHCRAFT_DB"]
        self.tmp.cleanup()

    def test_learned_is_the_newest_session(self):
        out = pane.state(CWD)["learned"]
        self.assertEqual(out["sessionId"], "s-2")
        self.assertEqual(out["total"], 2)
        self.assertEqual({(f["source"], f["target"]) for f in out["facts"]},
                         {("User", "Short replies"), ("Campfires", "Avoid AnimBP override")})
        self.assertTrue(out["titleIsAuto"])  # never named: a name made from what it introduced
        self.assertIn("Avoid AnimBP override", out["title"])
        with closing(store.connect()) as db:
            store.set_title(db, store.find_session(db, "s-2")[0], "Bedroll clash", "user")
        out = pane.state(CWD)["learned"]
        self.assertEqual((out["title"], out["titleIsAuto"]), ("Bedroll clash", False))

    # --- Fix ---

    def ids(self, a, at, b, bt):
        return store.entity_id(at, a), store.entity_id(bt, b)

    def test_links_follow_the_vocabulary_both_ways(self):
        crash, layer = self.ids("Swap crash", "PROBLEM", "Linked layer", "DECISION")
        values = {x["value"] for x in pane.links(crash, layer)["links"]}
        self.assertIn("fixed_by", values)  # PROBLEM fixed_by DECISION
        self.assertIn("~because", values)  # DECISION because PROBLEM: the other way round
        self.assertNotIn("prefers", values)

    def test_relink_retires_the_old_fact_and_saves_the_new_under_the_session(self):
        crash, layer = self.ids("Swap crash", "PROBLEM", "Linked layer", "DECISION")
        out = pane.relink(crash, "fixed_by", layer, "~because", "s-3", CWD)
        self.assertEqual((out["retired"], out["fact"]["source"], out["fact"]["predicate"]), (1, "Linked layer", "because"))
        with closing(store.connect()) as db:
            live = {r for r in db.execute("SELECT predicate, ref FROM relations r JOIN origins o ON o.id=r.origin_id"
                                          " WHERE source_id IN (?, ?) AND target_id IN (?, ?) AND retired IS NULL",
                                          (crash, layer, crash, layer))}
        self.assertEqual(live, {("because", "s-3")})

    def test_relink_refuses_a_link_the_vocabulary_does_not_allow(self):
        crash, layer = self.ids("Swap crash", "PROBLEM", "Linked layer", "DECISION")
        self.assertIn("error", pane.relink(crash, "fixed_by", layer, "prefers", "s-3", CWD))
        self.assertEqual(len(pane.state(CWD)["problems"]), 2)  # nothing retired

    def test_problems_open_first_and_fixed_by(self):
        problems = pane.state(CWD)["problems"]
        self.assertEqual([(p["name"], p["fixed"]) for p in problems],
                         [("Bedroll conflict", False), ("Swap crash", True)])
        self.assertEqual(problems[1]["fixedBy"], "Linked layer")

    def test_brief_names_latest_decision_and_counts(self):
        brief = pane.state(CWD)["brief"]
        self.assertEqual(brief, "Last time in Campfires: latest decision: Avoid AnimBP override · 1 open problem"
                                " · 2 decisions")

    def test_no_project_node_means_no_brief(self):
        out = pane.state(r"C:\somewhere\else")
        self.assertEqual((out["project"], out["brief"], out["problems"]), ("", "", []))
        self.assertIsNotNone(out["learned"])

    def test_forget_retires_by_ids_and_recall_skips_it(self):
        hit = recall.recall("what did Campfires decide?", CWD)
        fact = next(t for t in hit["triples"] if t["target"] == "Avoid AnimBP override")
        self.assertEqual(hit["matched"], ["Campfires"])
        self.assertEqual(pane.forget(fact["source_id"], "decided", fact["target_id"]), {"retired": 1})
        again = recall.recall("what did Campfires decide?", CWD)
        self.assertNotIn("Avoid AnimBP override", [t["target"] for t in again["triples"]])
        self.assertEqual(pane.state(CWD)["learned"]["total"], 1)

    def test_a_session_with_every_fact_retired_is_not_learned(self):
        with closing(store.connect()) as db:
            db.execute("UPDATE relations SET retired=1 WHERE origin_id=(SELECT id FROM origins WHERE ref='s-2')")
            db.commit()
        self.assertEqual(pane.state(CWD)["learned"]["sessionId"], "s-1")

    def test_missing_graph_answers_empty_without_creating_one(self):
        os.environ["GRAPHCRAFT_DB"] = str(Path(self.tmp.name) / "none" / "graph.db")
        self.assertEqual(pane.state(CWD), {"project": "", "brief": "", "learned": None, "problems": []})
        self.assertEqual(pane.forget("a", "uses", "b"), {"retired": 0})
        self.assertFalse(Path(self.tmp.name, "none").exists())


if __name__ == "__main__":
    unittest.main()
