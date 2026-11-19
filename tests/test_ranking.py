"""Recall on a big graph: an old decision must not be crowded out by thousands of newer, lighter facts."""
import random
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "graph"))

import recall  # noqa: E402
import store  # noqa: E402

VOCAB = store.load_vocabulary()


class RankingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = store.connect(Path(cls.tmp.name) / "graph.db")
        rnd = random.Random(7)
        # months ago: why Campfires uses a linked anim layer, and what the user prefers
        old = store.add_origin(cls.db, "session", "old")
        store.upsert(cls.db, {
            "nodes": [{"name": "Campfires", "type": "PROJECT"}, {"name": "Linked anim layer", "type": "DECISION"},
                      {"name": "Swap crash", "type": "PROBLEM"}, {"name": "User", "type": "PERSON"},
                      {"name": "Short replies", "type": "PREFERENCE"}, {"name": "AnimBP_Player", "type": "FILE"}],
            "edges": [{"source": "Campfires", "predicate": "decided", "target": "Linked anim layer"},
                      {"source": "Linked anim layer", "predicate": "because", "target": "Swap crash"},
                      {"source": "Swap crash", "predicate": "about", "target": "AnimBP_Player"},
                      {"source": "User", "predicate": "prefers", "target": "Short replies"},
                      {"source": "User", "predicate": "works_on", "target": "Campfires"}]}, old, VOCAB)
        cls.db.execute("UPDATE relations SET created=created-90*86400 WHERE origin_id=?", (old,))
        # since then: 200 sessions of light facts around the same project and file, named to sort first
        for i in range(200):
            o = store.add_origin(cls.db, "session", f"s{i}")
            nodes = [{"name": "Campfires", "type": "PROJECT"}, {"name": "AnimBP_Player", "type": "FILE"}]
            edges = []
            for j in range(15):
                n = f"Aa note {i}-{j}"
                nodes.append({"name": n, "type": rnd.choice(["CONCEPT", "FILE", "TOOL"])})
                edges.append({"source": n, "predicate": rnd.choice(["part_of", "references"]),
                              "target": rnd.choice(["Campfires", "AnimBP_Player"])})
            store.upsert(cls.db, {"nodes": nodes, "edges": edges}, o, VOCAB)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.tmp.cleanup()

    def ask(self, prompt):
        t0 = time.perf_counter()
        out = recall.recall(prompt, db=self.db)
        return out, (time.perf_counter() - t0) * 1000

    def facts(self, out):
        return {(t["source"], t["predicate"], t["target"]) for t in out["triples"]}

    def test_the_old_decision_and_its_reason_come_first(self):
        out, _ = self.ask("Campfires keeps crashing around AnimBP_Player")
        self.assertIn(("Campfires", "decided", "Linked anim layer"), self.facts(out))
        self.assertIn(("Linked anim layer", "because", "Swap crash"), self.facts(out))
        self.assertIn(("Swap crash", "about", "AnimBP_Player"), self.facts(out))

    def test_a_preference_is_not_buried(self):
        out, _ = self.ask("User working on Campfires again")
        self.assertIn(("User", "prefers", "Short replies"), self.facts(out))

    def test_still_fast(self):
        self.ask("Campfires")
        _, ms = self.ask("Campfires keeps crashing around AnimBP_Player")
        self.assertLess(ms, 80, f"recall took {ms:.1f} ms on {3000} facts")


if __name__ == "__main__":
    unittest.main()
