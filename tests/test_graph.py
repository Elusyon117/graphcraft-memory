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
import terms  # noqa: E402

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

    def test_signature_is_read_from_the_description(self):
        types = VOCAB["types"]
        self.assertEqual(store.signature("because", VOCAB["predicates"]["because"], types),
                         ({"DECISION"}, {"PROBLEM", "CONCEPT", "PREFERENCE"}))
        self.assertEqual(store.signature("part_of", VOCAB["predicates"]["part_of"], types), (set(), set()))
        self.assertEqual(store.signature("avoids", VOCAB["predicates"]["avoids"], types), ({"PERSON", "PROJECT"}, set()))

    def test_edge_between_wrong_types_is_dropped(self):
        rep = store.upsert(self.db, {
            "nodes": [{"name": "Zone Kit", "type": "TOOL"}, {"name": "Crash", "type": "PROBLEM"},
                      {"name": "Use layers", "type": "DECISION"}],
            "edges": [{"source": "Zone Kit", "predicate": "about", "target": "Crash"},            # TOOL can't be about
                      {"source": "Use layers", "predicate": "because", "target": "Zone Kit"},     # a TOOL is no reason
                      {"source": "Use layers", "predicate": "because", "target": "Crash"},        # fine
                      {"source": "Crash", "predicate": "fixed_by", "target": "Use layers"}]},     # fine
            self.origin, VOCAB)
        self.assertEqual(rep["edges"], 2)
        self.assertEqual(len(rep["dropped"]), 2)
        self.assertIn("(TOOL) 'about'", rep["dropped"][0])

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
                               "edges": [{"source": "Campfires", "predicate": "tried", "target": "Elsewhere Note"}]},
                     a, VOCAB)
        store.upsert(self.db, {"nodes": [{"name": "Campfires", "type": "MOD"}, {"name": "Here Note", "type": "CONCEPT"}],
                               "edges": [{"source": "Campfires", "predicate": "tried", "target": "Here Note"}]},
                     b, VOCAB)
        targets = [t["target"] for t in self.ask("Campfires", cwd="C:/mods/Campfires", hops=1)["triples"]
                   if t["predicate"] == "tried"]
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

    def test_source_tags_stay_short(self):
        self.assertEqual(recall.short_origin("session:9df5caa2-ee6c-4b1e-9f0a-123456789abc"), "s:9df5caa2")
        self.assertEqual(recall.short_origin("file:c:/users/me/repo/src/templatekit.ts"), "file:templatekit.ts")
        self.assertEqual(recall.short_origin("feeder:zonecraft:crashes"), "feeder:zonecraft:crashes")

    def test_long_lines_run_out_of_budget_without_failing(self):
        # Long names (file paths) fill the budget before the fact limit: the facts left over are simply not sent.
        big = {"nodes": [{"name": "Campfires", "type": "MOD"}], "edges": []}
        for i in range(30):
            name = f"src/features/a/very/deep/folder/with/a/long/path/number/{i}/and/a/longer/file/name/{'x' * 60}.ts"
            big["nodes"].append({"name": name, "type": "FILE"})
            big["edges"].append({"source": name, "predicate": "about", "target": "Campfires"})
        store.upsert(self.db, big, self.origin, VOCAB)
        out = self.ask("Campfires")
        self.assertGreater(out["facts"], 0)
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

    # --- duplicates: one thing saved under two types ---

    def types_named(self, name):
        return sorted(r[0] for r in self.db.execute("SELECT type FROM entities WHERE norm=?", (store.normalise(name),)))

    def test_same_name_as_another_type_reuses_the_node(self):
        store.upsert(self.db, {"nodes": [{"name": "AnimBP_Player", "type": "TOOL"},
                                         {"name": "Reinit swap on sit", "type": "DECISION"}],
                               "edges": [{"source": "Reinit swap on sit", "predicate": "about", "target": "AnimBP_Player"}]},
                     self.origin, VOCAB)
        self.assertEqual(self.types_named("AnimBP_Player"), ["ASSET"])  # no TOOL twin
        self.assertIn(("Reinit swap on sit", "about", "AnimBP_Player"), self.facts(self.ask("AnimBP_Player")))

    def test_a_fact_that_needs_the_other_type_gets_it(self):
        # "overrides" takes a MOD or NEXUS_MOD source: a Refund Policy FILE cannot be one, so a MOD node is made
        store.upsert(self.db, {"nodes": [{"name": "Refund Policy", "type": "MOD"}],
                               "edges": [{"source": "Refund Policy", "predicate": "overrides", "target": "AnimBP_Player"}]},
                     self.origin, VOCAB)
        self.assertEqual(self.types_named("Refund Policy"), ["FILE", "MOD"])

    def test_merge_joins_a_split_node_and_keeps_every_source(self):
        other = store.add_origin(self.db, "session", "s-dup")
        self.db.execute("INSERT INTO entities(id, name, norm, type, created, updated) VALUES (?,?,?,?,1,1)",
                        (store.entity_id("CONCEPT", "Reinit swap on sit"), "Reinit swap on sit",
                         store.normalise("Reinit swap on sit"), "CONCEPT"))
        self.db.execute("INSERT INTO relations(source_id, predicate, target_id, origin_id, created) VALUES (?,?,?,?,1)",
                        (store.entity_id("CONCEPT", "Reinit swap on sit"), "about",
                         store.entity_id("ASSET", "AnimBP_Player"), other))
        self.db.commit()
        out = store.merge_duplicates(self.db, VOCAB)
        self.assertEqual(out["merged"], ["Reinit swap on sit (DECISION)"])
        self.assertEqual(self.types_named("Reinit swap on sit"), ["DECISION"])
        origins = {r[0] for r in self.db.execute(
            "SELECT o.ref FROM relations r JOIN origins o ON o.id=r.origin_id WHERE r.source_id=? OR r.target_id=?",
            (store.entity_id("DECISION", "Reinit swap on sit"),) * 2)}
        self.assertEqual(origins, {"fixture.json", "s-dup"})

    def test_merge_leaves_a_pair_whose_facts_would_break(self):
        self.db.execute("INSERT INTO entities(id, name, norm, type, created, updated) VALUES (?,?,?,?,1,1)",
                        (store.entity_id("PREFERENCE", "Campfires"), "Campfires", "campfires", "PREFERENCE"))
        self.db.execute("INSERT INTO relations(source_id, predicate, target_id, origin_id, created) VALUES (?,?,?,?,1)",
                        (store.entity_id("PREFERENCE", "Campfires"), "advice_in",
                         store.entity_id("KNOWLEDGE_TOPIC", "animation.md / Crash families"), self.origin))
        self.db.commit()
        out = store.merge_duplicates(self.db, VOCAB)
        self.assertEqual(out["left"], ["Campfires"])
        self.assertEqual(self.types_named("Campfires"), ["MOD", "PREFERENCE"])

    # --- vague prompts (terms.py) ---

    def test_stems_split_words_and_synonyms(self):
        self.assertEqual(terms.terms_of("Sitting crashed, it broke"), {"sit", "crash"})
        self.assertEqual(terms.terms_of("AnimBP_Player"), {"anim", "player"})
        self.assertEqual(terms.terms_of("overriding"), terms.terms_of("overrides"))
        self.assertEqual(terms.terms_of("the thing that was there"), set())

    def test_vague_prompt_finds_facts_by_their_words(self):
        out = self.ask("the game broke when I was seated")
        self.assertIn(("Campfires", "tried", "Reinit swap on sit"), self.facts(out))
        self.assertIn("~Reinit swap on sit", out["matched"])

    def test_description_words_find_a_node(self):
        out = self.ask("which mod also replaces the player blueprint?")
        self.assertIn(("Bedroll Mod", "overrides", "AnimBP_Player"), self.facts(out))

    def test_exact_names_rank_before_word_matches(self):
        out = self.ask("Bedroll Mod and the sitting idea")
        self.assertEqual(out["triples"][0]["near"], 0)
        self.assertEqual(out["triples"][0]["source"], "Bedroll Mod")

    def test_one_common_word_is_not_enough(self):
        self.assertEqual(self.ask("tell me about the build")["facts"], 0)

    def test_word_index_follows_edits_and_forgetting(self):
        store.upsert(self.db, {"nodes": [{"name": "Bedroll Mod", "type": "NEXUS_MOD", "description": "Lets you sleep."}]},
                     self.origin, VOCAB)
        self.assertTrue(terms.fuzzy_seeds(self.db, "the sleeping mod"))
        store.forget_origin(self.db, self.origin)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_old_graph_gets_its_word_index(self):
        self.db.execute("DELETE FROM terms")
        self.db.commit()
        self.db.close()
        self.db = store.connect(self.path)
        self.assertGreater(self.db.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    # --- session names ---

    def session(self, ref):
        return store.add_origin(self.db, "session", ref)

    def test_model_names_once_and_the_users_name_always_stays(self):
        oid = self.session("aaaa1111-x")
        self.assertTrue(store.set_title(self.db, oid, "  Fix the   sit crash ", "model"))
        self.assertEqual(store.title_of(self.db, oid), ("Fix the sit crash", False))
        self.assertFalse(store.set_title(self.db, oid, "Something later", "model"))  # a later capture
        self.assertTrue(store.set_title(self.db, oid, "My name", "user"))
        self.assertFalse(store.set_title(self.db, oid, "Model again", "model"))
        self.assertTrue(store.set_title(self.db, oid, "My second name", "user"))
        self.assertEqual(store.title_of(self.db, oid)[0], "My second name")
        self.assertFalse(store.set_title(self.db, oid, "   ", "user"))  # blank never erases a name

    def test_untitled_source_is_named_from_what_it_introduced(self):
        title, auto = store.title_of(self.db, self.origin)
        self.assertTrue(auto)
        self.assertTrue(title)
        self.assertEqual(store.title_of(self.db, self.session("empty")), ("", True))

    def test_find_session_by_last_whole_id_or_unique_prefix(self):
        a = self.session("abc123-one")
        self.db.execute("UPDATE origins SET at = at + 10 WHERE id=?", (a,))
        b = self.session("abd456-two")
        self.assertEqual(store.find_session(self.db, "last"), (a, "abc123-one"))
        self.assertEqual(store.find_session(self.db, "abd"), (b, "abd456-two"))
        self.assertIsNone(store.find_session(self.db, "ab"))  # ambiguous
        self.assertIsNone(store.find_session(self.db, "zzz"))
        self.assertIsNone(store.find_session(self.db, "%"))  # not a wildcard
        self.assertEqual(store.find_session(self.db, "abc123-one"), (a, "abc123-one"))

    def test_old_graph_gains_the_title_columns(self):
        self.db.close()
        old = Path(self.tmp.name) / "old.db"
        import sqlite3
        raw = sqlite3.connect(old)
        raw.execute("CREATE TABLE origins (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL,"
                    " project TEXT NOT NULL DEFAULT '', at REAL NOT NULL, UNIQUE(kind, ref))")
        raw.execute("INSERT INTO origins(kind, ref, at) VALUES ('session', 'old-1', 1)")
        raw.commit(); raw.close()
        self.db = store.connect(old)
        self.assertEqual(store.title_of(self.db, 1), ("", True))
        self.assertTrue(store.set_title(self.db, 1, "Named later", "user"))

    def test_cli_rename_takes_a_name_that_starts_with_a_dash(self):
        self.session("cli-session-1")
        self.db.commit()
        env = {**os.environ, "GRAPHCRAFT_DB": str(self.path)}
        r = subprocess.run([sys.executable, "-I", str(ROOT / "graph" / "store.py"), "rename", "cli", "--", "-Sit fix", "v2"],
                           capture_output=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(json.loads(r.stdout), {"renamed": "cli-session-1", "title": "-Sit fix v2"})
        r = subprocess.run([sys.executable, "-I", str(ROOT / "graph" / "store.py"), "rename", "nope", "--", "x"],
                           capture_output=True, env=env, timeout=30)
        self.assertIsNone(json.loads(r.stdout)["renamed"])

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
