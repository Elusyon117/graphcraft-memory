"""Duplicates under different names: suggested, merged with sources kept, undone exactly, or marked distinct."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "graph"))
import dupes  # noqa: E402
import recall  # noqa: E402
import store  # noqa: E402

VOCAB = store.load_vocabulary()


def dump(db) -> tuple:
    return (sorted(db.execute("SELECT id, name, type, description FROM entities")),
            sorted(db.execute("SELECT source_id, predicate, target_id, origin_id, retired IS NULL FROM relations")),
            sorted(db.execute("SELECT entity_id, norm FROM aliases")))


class DupesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["GRAPHCRAFT_DB"] = str(Path(self.tmp.name) / "graph.db")
        self.db = dupes.connect()
        a = store.add_origin(self.db, "session", "early", r"C:\mods\Memory")
        store.upsert(self.db, {
            "nodes": [{"name": "Memory", "type": "PROJECT"},
                      {"name": "warm recall", "type": "CONCEPT", "description": "Recall kept running in one process."},
                      {"name": "Python startup", "type": "PROBLEM", "description": "Starting Python each prompt is slow."}],
            "edges": [{"source": "warm recall", "predicate": "part_of", "target": "Memory"},
                      {"source": "Python startup", "predicate": "about", "target": "warm recall"}]}, a, VOCAB)
        b = store.add_origin(self.db, "session", "later", r"C:\mods\Memory")
        store.upsert(self.db, {
            "nodes": [{"name": "Warm recall process", "type": "DECISION",
                       "description": "One long-lived recall process per session, so recall stays fast."},
                      {"name": "Memory", "type": "PROJECT"}, {"name": "Python startup", "type": "PROBLEM"}],
            "edges": [{"source": "Memory", "predicate": "decided", "target": "Warm recall process"},
                      {"source": "Warm recall process", "predicate": "because", "target": "Python startup"}]}, b, VOCAB)
        self.db.commit()
        self.concept = store.entity_id("CONCEPT", "warm recall")
        self.decision = store.entity_id("DECISION", "Warm recall process")

    def tearDown(self):
        self.db.close()
        del os.environ["GRAPHCRAFT_DB"]
        self.tmp.cleanup()

    def test_the_pair_is_suggested_keeping_the_more_specific_kind(self):
        pairs = dupes.suggest(self.db, vocab=VOCAB)
        self.assertEqual((pairs[0]["from"]["name"], pairs[0]["into"]["name"]), ("warm recall", "Warm recall process"))

    def test_merge_moves_facts_with_their_sources_and_keeps_the_old_name(self):
        out = dupes.merge(self.db, self.concept, self.decision, VOCAB)
        self.assertEqual(out["into"], "Warm recall process")
        rows = self.db.execute("SELECT r.predicate, o.ref FROM relations r JOIN origins o ON o.id=r.origin_id"
                               " WHERE r.source_id=? OR r.target_id=?", (self.decision, self.decision)).fetchall()
        self.assertIn(("part_of", "early"), rows)  # the concept's fact, still from the session that said it
        self.assertIsNone(self.db.execute("SELECT 1 FROM entities WHERE id=?", (self.concept,)).fetchone())
        got = recall.recall("how does warm recall work", db=self.db)  # the old name now finds the kept node
        self.assertIn(("Memory", "decided", "Warm recall process"),
                      {(t["source"], t["predicate"], t["target"]) for t in got["triples"]})

    def test_undo_puts_back_exactly_what_was_there(self):
        before = dump(self.db)
        dupes.merge(self.db, self.concept, self.decision, VOCAB)
        self.assertNotEqual(dump(self.db), before)
        self.assertEqual(dupes.undo(self.db)["restored"], "warm recall")
        self.assertEqual(dump(self.db), before)
        self.assertIn("error", dupes.undo(self.db))  # nothing left to undo

    def test_a_merge_that_breaks_a_link_rule_is_refused(self):
        # "Memory decided X" needs X to be a decision: the decision cannot become a concept
        out = dupes.merge(self.db, self.decision, self.concept, VOCAB)
        self.assertIn("cannot merge", out["error"])
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM entities WHERE id=?", (self.decision,)).fetchone())

    def test_not_the_same_is_remembered(self):
        dupes.distinct(self.db, self.decision, self.concept)
        self.assertEqual(dupes.suggest(self.db, vocab=VOCAB), [])

    def test_two_linked_by_a_reason_are_never_suggested(self):
        names = {(p["from"]["name"], p["into"]["name"]) for p in dupes.suggest(self.db, limit=20, vocab=VOCAB)}
        self.assertNotIn(("Python startup", "Warm recall process"), names)


if __name__ == "__main__":
    unittest.main()
