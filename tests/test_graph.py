r"""Store and recall on a small fixture graph. Run: py -I tests/test_graph.py (or python -m unittest)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "graph"))
import recall  # noqa: E402
import store  # noqa: E402

FIXTURE = json.loads((HERE / "fixture.json").read_text(encoding="utf-8"))
VOCAB = store.load_vocabulary([HERE / "fixture_vocab.json"])


class GraphTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "graph.db"
        self.db = store.connect(self.path)
        self.origin = store.add_origin(self.db, "file", "fixture.json")
        self.report = store.upsert(self.db, FIXTURE, self.origin, VOCAB)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def ask(self, prompt, **kw):
        return recall.recall(prompt, db=self.db, **kw)

    def facts(self, out):
        return {(t["source"], t["predicate"], t["target"]) for t in out["triples"]}

    # --- store ---

    def test_fixture_loads_cleanly(self):
        self.assertEqual(self.report["dropped"], [])
        self.assertEqual(store.stats(self.db)["relations"], 5)

    def test_same_name_from_another_origin_is_one_node(self):
        other = store.add_origin(self.db, "session", "s-42", project="C:/mods/Campfires")
        store.upsert(self.db, {"nodes": [{"name": "animbp player", "type": "ASSET"},
                                         {"name": "Campfires", "type": "MOD"}],
                               "edges": [{"source": "Campfires", "predicate": "overrides",
                                          "target": "animbp player"}]}, other, VOCAB)
        n = self.db.execute("SELECT COUNT(*) FROM entities WHERE type='ASSET'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_out_of_vocabulary_is_dropped_not_stored(self):
        rep = store.upsert(self.db, {"nodes": [{"name": "X", "type": "SPACESHIP"}],
                                     "edges": [{"source": "Campfires", "predicate": "loves",
                                                "target": "AnimBP_Player"}]}, self.origin, VOCAB)
        self.assertEqual(len(rep["dropped"]), 2)
        self.assertIsNone(self.db.execute("SELECT 1 FROM entities WHERE name='X'").fetchone())
        self.assertIsNone(self.db.execute("SELECT 1 FROM relations WHERE predicate='loves'").fetchone())

    def test_core_only_vocabulary_drops_feeder_types(self):
        db = store.connect(Path(self.tmp.name) / "core.db")
        rep = store.upsert(db, FIXTURE, store.add_origin(db, "file", "f"), store.load_vocabulary())
        self.assertEqual(store.stats(db)["relations"], 0)
        self.assertTrue(rep["dropped"])
        db.close()

    def test_extra_vocabulary_cannot_redefine_core(self):
        self.assertEqual(VOCAB["types"]["PROJECT"], "A folder or repository being worked on.")

    def test_forget_origin_removes_only_its_facts(self):
        other = store.add_origin(self.db, "session", "s-1")
        store.upsert(self.db, {"nodes": [{"name": "Zone Kit", "type": "TOOL"}, {"name": "Campfires", "type": "MOD"}],
                               "edges": [{"source": "Campfires", "predicate": "uses", "target": "Zone Kit"}]},
                     other, VOCAB)
        store.forget_origin(self.db, other)
        self.assertEqual(store.stats(self.db)["relations"], 5)
        self.assertIsNone(self.db.execute("SELECT 1 FROM entities WHERE name='Zone Kit'").fetchone())
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM entities WHERE name='Campfires'").fetchone())

    # --- recall ---

    def test_two_hops_reach_the_other_mod_and_the_advice(self):
        out = self.ask("why does Campfires crash when I sit down?")
        f = self.facts(out)
        self.assertIn(("Bedroll Mod", "overrides", "AnimBP_Player"), f)
        self.assertIn(("Post-process reinit", "advice_in", "animation.md / Crash families"), f)
        self.assertIn("SetOverridePostProcessAnimBP", out["context"])  # descriptions ride along

    def test_one_hop_stops_short(self):
        f = self.facts(self.ask("why does Campfires crash?", hops=1))
        self.assertIn(("Campfires", "overrides", "AnimBP_Player"), f)
        self.assertNotIn(("Bedroll Mod", "overrides", "AnimBP_Player"), f)

    def test_alias_seeds(self):
        f = self.facts(self.ask("who else touches the player animbp?", hops=1))
        self.assertIn(("Bedroll Mod", "overrides", "AnimBP_Player"), f)

    def test_unrelated_prompt_recalls_nothing(self):
        out = self.ask("what's a good name for a cat?")
        self.assertEqual(out["facts"], 0)
        self.assertEqual(out["context"], "")

    def test_retired_fact_is_skipped(self):
        self.assertEqual(store.retire(self.db, "Bedroll Mod", "overrides", "AnimBP_Player"), 1)
        f = self.facts(self.ask("Campfires"))
        self.assertNotIn(("Bedroll Mod", "overrides", "AnimBP_Player"), f)

    def test_hub_is_not_walked_through(self):
        hub = {"nodes": [{"name": "Hub Tool", "type": "TOOL"}, {"name": "Far Away", "type": "CONCEPT"}],
               "edges": [{"source": "Campfires", "predicate": "uses", "target": "Hub Tool"},
                         {"source": "Far Away", "predicate": "uses", "target": "Hub Tool"}]}
        for i in range(recall.HUB + 5):
            hub["nodes"].append({"name": f"Project {i}", "type": "PROJECT"})
            hub["edges"].append({"source": f"Project {i}", "predicate": "uses", "target": "Hub Tool"})
        store.upsert(self.db, hub, self.origin, VOCAB)
        names = {t["source"] for t in self.ask("Campfires", hops=2)["triples"]}
        self.assertNotIn("Far Away", names)

    def test_other_projects_rank_after_this_one(self):
        a = store.add_origin(self.db, "session", "a", project="C:/work/elsewhere")
        b = store.add_origin(self.db, "session", "b", project="C:/mods/Campfires")
        store.upsert(self.db, {"nodes": [{"name": "Campfires", "type": "MOD"}, {"name": "Elsewhere Note", "type": "CONCEPT"}],
                               "edges": [{"source": "Campfires", "predicate": "about", "target": "Elsewhere Note"}]},
                     a, VOCAB)
        store.upsert(self.db, {"nodes": [{"name": "Campfires", "type": "MOD"}, {"name": "Here Note", "type": "CONCEPT"}],
                               "edges": [{"source": "Campfires", "predicate": "about", "target": "Here Note"}]},
                     b, VOCAB)
        targets = [t["target"] for t in self.ask("Campfires", cwd="C:/mods/Campfires", hops=1)["triples"]
                   if t["predicate"] == "about"]
        self.assertLess(targets.index("Here Note"), targets.index("Elsewhere Note"))

    def test_output_stays_within_budget(self):
        big = {"nodes": [{"name": "Campfires", "type": "MOD"}], "edges": []}
        for i in range(60):
            big["nodes"].append({"name": f"Idea number {i}", "type": "CONCEPT", "description": "x" * 80})
            big["edges"].append({"source": "Campfires", "predicate": "about", "target": f"Idea number {i}"})
        store.upsert(self.db, big, self.origin, VOCAB)
        out = self.ask("Campfires")
        self.assertLessEqual(out["facts"], recall.MAX_FACTS)
        self.assertLessEqual(len(out["context"]), recall.MAX_CHARS + 200)

    def test_no_database_answers_nothing_and_creates_nothing(self):
        missing = Path(self.tmp.name) / "none" / "graph.db"
        os.environ["GRAPHCRAFT_DB"] = str(missing)
        try:
            self.assertEqual(recall.recall("Campfires")["facts"], 0)
        finally:
            del os.environ["GRAPHCRAFT_DB"]
        self.assertFalse(missing.exists())

    def test_recall_is_fast_on_a_large_graph(self):
        big = {"nodes": [], "edges": []}
        for i in range(5000):
            big["nodes"].append({"name": f"Thing {i}", "type": "CONCEPT"})
            big["edges"].append({"source": f"Thing {i}", "predicate": "references", "target": f"Thing {(i * 7) % 5000}"})
        big["edges"].append({"source": "Campfires", "predicate": "references", "target": "Thing 1"})
        store.upsert(self.db, big, self.origin, VOCAB)
        self.ask("Campfires")  # warm
        t0 = time.perf_counter()
        out = self.ask("the game crashed again in Campfires after build 4, check the player animbp and Thing 1")
        ms = (time.perf_counter() - t0) * 1000
        self.assertGreater(out["facts"], 0)
        self.assertLess(ms, 50, f"recall took {ms:.1f} ms")

    # --- the hook's command line ---

    def test_cli_stdin_json(self):
        env = {**os.environ, "GRAPHCRAFT_DB": str(self.path)}
        self.db.commit()
        r = subprocess.run([sys.executable, "-I", str(ROOT / "graph" / "recall.py"), "--stdin", "--json"],
                           input=json.dumps({"prompt": "Campfires crashed", "cwd": ""}).encode(),
                           capture_output=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        out = json.loads(r.stdout)
        self.assertGreater(out["facts"], 0)
        self.assertTrue(out["summary"].startswith("memory: "))


if __name__ == "__main__":
    unittest.main()
