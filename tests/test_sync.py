"""Moving memory between PCs: two graphs in temp folders stand for two PCs."""
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

import recall  # noqa: E402
import store  # noqa: E402
import sync  # noqa: E402

VOCAB = store.load_vocabulary()
LAPTOP = {"nodes": [{"name": "Campfires", "type": "PROJECT"}, {"name": "Linked layer", "type": "DECISION",
                                                                  "description": "Use a linked anim layer."},
                    {"name": "Swap crash", "type": "PROBLEM"}],
          "edges": [{"source": "Campfires", "predicate": "decided", "target": "Linked layer"},
                    {"source": "Swap crash", "predicate": "fixed_by", "target": "Linked layer"}],
          "aliases": [{"entity": "Swap crash", "alias": "the sit crash"}]}
GAMING = {"nodes": [{"name": "Campfires", "type": "PROJECT"}, {"name": "Bedroll clash", "type": "PROBLEM"}],
          "edges": [{"source": "Campfires", "predicate": "blocked_by", "target": "Bedroll clash"}]}


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pc("laptop")
        with closing(store.connect()) as db:
            oid = store.add_origin(db, "session", "laptop-1", "C:/mods/Campfires")
            store.upsert(db, LAPTOP, oid, VOCAB)
            store.set_title(db, oid, "Linked layer for the sit crash", "model")
        self.pc("gaming")
        with closing(store.connect()) as db:
            store.upsert(db, GAMING, store.add_origin(db, "session", "gaming-1", "D:/mods/Campfires"), VOCAB)
        self.file = str(self.root / "export.json")

    def tearDown(self):
        del os.environ["GRAPHCRAFT_DB"]
        self.tmp.cleanup()

    def pc(self, name):
        os.environ["GRAPHCRAFT_DB"] = str(self.root / name / "graph.db")

    def move(self, src, dst):
        self.pc(src)
        sync.export(self.file)
        self.pc(dst)
        return sync.import_(self.file)

    def live(self):
        with closing(store.connect()) as db:
            return {(s, p, t) for s, p, t in db.execute(
                "SELECT es.name, r.predicate, et.name FROM relations r JOIN entities es ON es.id=r.source_id"
                " JOIN entities et ON et.id=r.target_id WHERE r.retired IS NULL")}

    def test_import_merges_both_pcs_memory(self):
        out = self.move("laptop", "gaming")
        self.assertEqual((out["facts"], out["sources"]), (2, 1))
        self.assertEqual(self.live(), {("Campfires", "decided", "Linked layer"), ("Swap crash", "fixed_by", "Linked layer"),
                                       ("Campfires", "blocked_by", "Bedroll clash")})
        with closing(store.connect()) as db:  # one Campfires node, not two
            self.assertEqual(db.execute("SELECT COUNT(*) FROM entities WHERE name='Campfires'").fetchone()[0], 1)

    def test_imported_facts_are_recalled_by_alias_and_words(self):
        self.move("laptop", "gaming")
        self.assertTrue(recall.recall("the sit crash again")["facts"])
        self.assertTrue(recall.recall("did we choose the linked anim approach?")["facts"])

    def test_importing_twice_changes_nothing(self):
        self.move("laptop", "gaming")
        again = sync.import_(self.file)
        self.assertEqual((again["nodes"], again["facts"], again["sources"], again["retired"]), (0, 0, 0, 0))

    def test_sessions_keep_their_source_and_name_so_undo_still_works(self):
        self.move("laptop", "gaming")
        with closing(store.connect()) as db:
            found = store.find_session(db, "laptop-1")
            self.assertEqual(store.title_of(db, found[0])[0], "Linked layer for the sit crash")
            store.forget_origin(db, found[0])
        self.assertEqual(self.live(), {("Campfires", "blocked_by", "Bedroll clash")})

    def test_a_fact_retired_on_one_pc_is_retired_on_the_other(self):
        self.move("laptop", "gaming")
        self.pc("laptop")
        time.sleep(0.01)
        with closing(store.connect()) as db:
            store.retire(db, "Swap crash", "fixed_by", "Linked layer")
        out = self.move("laptop", "gaming")
        self.assertEqual(out["retired"], 1)
        self.assertNotIn(("Swap crash", "fixed_by", "Linked layer"), self.live())

    def test_a_fact_said_again_after_it_was_retired_stays_live(self):
        self.move("laptop", "gaming")  # gaming now has the laptop's facts too
        self.pc("laptop")
        with closing(store.connect()) as db:
            store.retire(db, "Swap crash", "fixed_by", "Linked layer")
        self.pc("gaming")
        time.sleep(0.01)
        with closing(store.connect()) as db:  # gaming said it again later, under the same session
            store.upsert(db, LAPTOP, store.find_session(db, "laptop-1")[0], VOCAB)
        self.move("laptop", "gaming")
        self.assertIn(("Swap crash", "fixed_by", "Linked layer"), self.live())

    def test_the_users_own_session_name_wins(self):
        self.move("laptop", "gaming")
        self.pc("laptop")
        with closing(store.connect()) as db:
            store.set_title(db, store.find_session(db, "laptop-1")[0], "My name for it", "user")
        self.move("laptop", "gaming")
        with closing(store.connect()) as db:
            self.assertEqual(store.title_of(db, store.find_session(db, "laptop-1")[0])[0], "My name for it")

    def test_a_copy_is_kept_before_import(self):
        out = self.move("laptop", "gaming")
        self.assertTrue(Path(out["backup"]).exists())
        with closing(store.connect(Path(out["backup"]))) as db:  # the copy is the graph as it was
            self.assertEqual(db.execute("SELECT COUNT(*) FROM relations").fetchone()[0], 1)

    def test_import_into_a_pc_with_no_memory_yet(self):
        self.pc("laptop")
        sync.export(self.file)
        self.pc("new")
        out = sync.import_(self.file)
        self.assertEqual((out["backup"], out["facts"]), ("", 2))

    def test_other_files_are_refused(self):
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"nodes": []}), encoding="utf-8")
        self.assertIn("not a graphcraft-memory export", sync.import_(str(bad))["error"])
        self.assertIn("could not read", sync.import_(str(self.root / "missing.json"))["error"])

    def test_export_with_no_memory_says_so(self):
        self.pc("empty")
        self.assertIn("error", sync.export(self.file))


if __name__ == "__main__":
    unittest.main()
