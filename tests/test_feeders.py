"""Feeders: a fake mod in a temp folder, its adapter a tiny script that passes its file's batch through."""
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "graph"))

import feeders  # noqa: E402
import store  # noqa: E402

ADAPTER = """import json, sys
sid, file = sys.argv[1], sys.argv[2]
data = json.load(open(file, encoding="utf-8"))
if data.get("fail"):
    sys.exit("broken on purpose")
print(json.dumps({"nodes": [{"name": m, "type": "MOD"} for m in data["mods"]] + [{"name": data["asset"], "type": "ASSET"}],
                  "edges": [{"source": m, "predicate": "overrides", "target": data["asset"]} for m in data["mods"]]}))
"""
VOCAB = {"types": {"MOD": "A mod.", "ASSET": "A game asset."}, "predicates": {"overrides": "MOD overrides ASSET"}}


class FeederTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["GRAPHCRAFT_DB"] = str(root / "data" / "graph.db")
        self.plugin = root / "mods" / "fakemod"
        (self.plugin / "graphcraft").mkdir(parents=True)
        self.write_manifest({"name": "fakemod", "vocabulary": "graphcraft/vocabulary.json",
                             "adapter": "graphcraft/adapter.py",
                             "sources": [{"id": "conflicts", "file": "conflicts.json", "title": "Mod conflicts"}]})
        (self.plugin / "graphcraft" / "vocabulary.json").write_text(json.dumps(VOCAB), encoding="utf-8")
        (self.plugin / "graphcraft" / "adapter.py").write_text(ADAPTER, encoding="utf-8")
        os.environ["GRAPHCRAFT_PLUGINS"] = str(self.plugin)
        self.feed = root / "data" / "feeds" / "fakemod" / "conflicts.json"
        self.feed.parent.mkdir(parents=True)

    def tearDown(self):
        del os.environ["GRAPHCRAFT_DB"], os.environ["GRAPHCRAFT_PLUGINS"]
        self.tmp.cleanup()

    def write_manifest(self, m):
        (self.plugin / "graphcraft" / "feeder.json").write_text(json.dumps(m), encoding="utf-8")

    def drop(self, data):
        self.feed.write_text(json.dumps(data), encoding="utf-8")
        t = time.time() + len(self.feed.read_text())  # a different mtime each write, however fast
        os.utime(self.feed, (t, t))

    def live(self):
        with closing(store.connect()) as db:
            return {(s, t) for s, t in db.execute(
                "SELECT es.name, et.name FROM relations r JOIN entities es ON es.id=r.source_id"
                " JOIN entities et ON et.id=r.target_id WHERE r.retired IS NULL")}

    def test_no_feeders_does_nothing(self):
        os.environ["GRAPHCRAFT_PLUGINS"] = ""
        out = feeders.run()
        self.assertEqual((out["feeders"], out["facts"], out["problems"]), ([], 0, []))
        self.assertFalse(Path(os.environ["GRAPHCRAFT_DB"]).exists())

    def test_no_file_yet_loads_nothing(self):
        out = feeders.run()
        self.assertEqual((out["feeders"], out["loaded"]), (["fakemod"], []))

    def test_file_is_loaded_under_its_feeder_origin_with_a_title(self):
        self.drop({"mods": ["Campfires", "Bedroll Mod"], "asset": "AnimBP_Player"})
        out = feeders.run()
        self.assertEqual(out["facts"], 2)
        self.assertEqual(self.live(), {("Campfires", "AnimBP_Player"), ("Bedroll Mod", "AnimBP_Player")})
        with closing(store.connect()) as db:
            oid, ref = db.execute("SELECT id, ref FROM origins WHERE kind='feeder'").fetchone()
            self.assertEqual((ref, store.title_of(db, oid)[0]), ("fakemod:conflicts", "Mod conflicts"))
        self.assertTrue((Path(os.environ["GRAPHCRAFT_DB"]).parent / "vocab" / "feeder-fakemod.json").exists())

    def test_unchanged_file_is_not_read_again(self):
        self.drop({"mods": ["Campfires", "Bedroll Mod"], "asset": "AnimBP_Player"})
        feeders.run()
        self.assertEqual(feeders.run()["loaded"], [])

    def test_a_fact_gone_from_the_snapshot_is_retired_and_comes_back(self):
        self.drop({"mods": ["Campfires", "Bedroll Mod"], "asset": "AnimBP_Player"})
        feeders.run()
        self.drop({"mods": ["Campfires"], "asset": "AnimBP_Player"})
        self.assertEqual(feeders.run()["retired"], 1)
        self.assertEqual(self.live(), {("Campfires", "AnimBP_Player")})
        self.drop({"mods": ["Campfires", "Bedroll Mod"], "asset": "AnimBP_Player"})
        feeders.run()
        self.assertEqual(len(self.live()), 2)

    def test_broken_adapter_is_a_problem_not_a_crash(self):
        self.drop({"fail": True})
        out = feeders.run()
        self.assertEqual(out["loaded"], [])
        self.assertIn("broken on purpose", out["problems"][0])

    def test_paths_outside_the_plugin_are_refused(self):
        self.write_manifest({"name": "fakemod", "adapter": "../../evil.py", "sources": [{"id": "x", "file": "x.json"}]})
        out = feeders.run()
        self.assertEqual(out["feeders"], [])
        self.assertIn("adapter", out["problems"][0])
        self.write_manifest({"name": "fakemod", "adapter": "graphcraft/adapter.py",
                             "sources": [{"id": "x", "file": "../../x.json"}]})
        self.assertIn("no usable sources", feeders.run()["problems"][0])

    def test_uninstalled_feeder_loses_its_vocabulary_file(self):
        feeders.run()
        vocab = Path(os.environ["GRAPHCRAFT_DB"]).parent / "vocab" / "feeder-fakemod.json"
        self.assertTrue(vocab.exists())
        os.environ["GRAPHCRAFT_PLUGINS"] = ""
        feeders.run()
        self.assertFalse(vocab.exists())

    def test_out_of_vocabulary_facts_are_dropped(self):
        (self.plugin / "graphcraft" / "vocabulary.json").write_text(json.dumps({"types": {"MOD": "A mod."}}), encoding="utf-8")
        self.drop({"mods": ["Campfires"], "asset": "AnimBP_Player"})
        out = feeders.run()
        self.assertEqual(out["facts"], 0)
        self.assertTrue(out["loaded"][0]["dropped"])


if __name__ == "__main__":
    unittest.main()
