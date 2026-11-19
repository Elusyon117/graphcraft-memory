"""The user's tuning (settings.json next to the graph): cleaned on save, and obeyed by recall and by learning."""
import os
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "graph"))
import capture  # noqa: E402
import recall  # noqa: E402
import store  # noqa: E402

A, B = r"C:\mods\Campfires", r"C:\mods\Bedroll"


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["GRAPHCRAFT_DB"] = str(Path(self.tmp.name) / "graph.db")
        self.db = store.connect()
        vocab = store.load_vocabulary()
        here = store.add_origin(self.db, "session", "a", A)
        store.upsert(self.db, {
            "nodes": [{"name": "Campfires", "type": "PROJECT"}, {"name": "User", "type": "PERSON"},
                      {"name": "Short replies", "type": "PREFERENCE"}, {"name": "Zone Kit", "type": "TOOL"}]
                     + [{"name": f"Choice {i}", "type": "DECISION"} for i in range(20)],
            "edges": [{"source": "User", "predicate": "prefers", "target": "Short replies"},
                      {"source": "User", "predicate": "works_on", "target": "Campfires"},
                      {"source": "Campfires", "predicate": "uses", "target": "Zone Kit"}]
                     + [{"source": "Campfires", "predicate": "decided", "target": f"Choice {i}"} for i in range(20)]},
            here, vocab)
        there = store.add_origin(self.db, "session", "b", B)
        store.upsert(self.db, {"nodes": [{"name": "Bedroll", "type": "PROJECT"}, {"name": "Zone Kit", "type": "TOOL"}],
                               "edges": [{"source": "Bedroll", "predicate": "uses", "target": "Zone Kit"}]}, there, vocab)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        del os.environ["GRAPHCRAFT_DB"]
        self.tmp.cleanup()

    def set(self, **kw):
        store.save_settings(kw)
        time.sleep(0.01)  # a new mtime, so the cached settings are read again

    def facts(self, prompt, cwd=A):
        return [(t["source"], t["predicate"], t["target"]) for t in recall.recall(prompt, cwd, db=self.db)["triples"]]

    def test_bad_values_fall_back_and_numbers_are_clamped(self):
        out = store.clean_settings({"recallFacts": 500, "recallHops": "far", "otherProjects": "no",
                                    "skipTypes": "person, crash family;;"})
        self.assertEqual(out, {"recallFacts": 40, "recallHops": 2, "otherProjects": True,
                               "skipTypes": ["CRASH_FAMILY", "PERSON"], "mapLayout": "structured"})
        self.assertEqual(store.clean_settings({"mapLayout": "free"})["mapLayout"], "free")
        self.assertEqual(store.settings(), store.SETTINGS)  # no file yet

    def test_recall_obeys_how_many_facts(self):
        self.assertEqual(len(self.facts("Campfires")), 14)
        self.set(recallFacts=5)
        self.assertEqual(len(self.facts("Campfires")), 5)

    def test_switched_off_types_are_not_recalled(self):
        self.assertIn(("User", "prefers", "Short replies"), self.facts("User"))
        self.set(skipTypes=["PREFERENCE"])
        self.assertNotIn(("User", "prefers", "Short replies"), self.facts("User"))

    def test_other_projects_can_be_left_out(self):
        self.assertIn(("Bedroll", "uses", "Zone Kit"), self.facts("Zone Kit"))
        self.set(otherProjects=False)
        got = self.facts("Zone Kit")
        self.assertNotIn(("Bedroll", "uses", "Zone Kit"), got)
        self.assertIn(("Campfires", "uses", "Zone Kit"), got)

    def test_switched_off_types_are_not_learned_or_offered(self):
        self.set(skipTypes=["PREFERENCE"])
        vocab = capture.learnable_vocabulary()
        o = store.add_origin(self.db, "session", "c", A)
        rep = store.upsert(self.db, {
            "nodes": [{"name": "Terse", "type": "PREFERENCE"}, {"name": "Wwise", "type": "TOOL"}],
            "edges": [{"source": "User", "predicate": "prefers", "target": "Terse"},
                      {"source": "User", "predicate": "prefers", "target": "Short replies"},
                      {"source": "Campfires", "predicate": "uses", "target": "Wwise"}]}, o, vocab)
        self.assertEqual(rep["edges"], 1)  # only the tool
        self.assertTrue(any("switched off" in d for d in rep["dropped"]))
        self.db.commit()
        q = capture.queue_dir()
        q.mkdir(parents=True, exist_ok=True)
        job = q / "j.json"
        job.write_text('{"version": 1, "sessionId": "c", "project": "x", "messages": ['
                       + ",".join('{"role": "user", "text": "hello %d"}' % i for i in range(3)) + "]}", encoding="utf-8")
        system = capture.prepare(str(job))["system"]
        self.assertNotIn("- PREFERENCE:", system)
        self.assertIn("- TOOL:", system)


class SentOnceTest(SettingsTest):
    """Within one session, a fact already given is not sent again; the pane still sees it, marked."""

    def test_a_second_message_gets_only_what_is_new(self):
        first = recall.recall("Campfires", A, db=self.db, session="s1.0")
        self.assertIn("(s:", first["context"])  # short source tags
        self.assertNotIn("session:", first["context"])
        again = recall.recall("Campfires Zone Kit", A, db=self.db, session="s1.0")
        self.assertTrue(any(t["again"] for t in again["triples"]))
        self.assertIn("given earlier in this session", again["context"])
        self.assertLess(len(again["context"]), len(first["context"]))
        fresh = recall.recall("Campfires", A, db=self.db, session="s1.1")  # after a compaction: all again
        self.assertEqual(fresh["context"], first["context"])
        alone = recall.recall("Campfires", A, db=self.db)  # memory_search and tests: no session, no memory of it
        self.assertFalse(any(t["again"] for t in alone["triples"]))

    def test_nothing_new_is_one_short_line(self):
        recall.recall("Campfires", A, db=self.db, session="s2.0")
        out = recall.recall("Campfires", A, db=self.db, session="s2.0")
        self.assertTrue(out["context"].startswith("graphcraft-memory: the "))
        self.assertLess(len(out["context"]), 140)


if __name__ == "__main__":
    unittest.main()
