r"""Capture: queue file -> prepare -> (fake model reply) -> apply -> recall. Run: py -I tests/test_capture.py"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "graph"))
import capture  # noqa: E402
import recall  # noqa: E402
import store  # noqa: E402

PROJECT = r"C:\mods\Campfires"


def conversation(n_user=3, extra=None):
    msgs = []
    for i in range(n_user):
        msgs.append({"role": "user", "text": f"user message {i} about the Campfires sit animation"})
        msgs.append({"role": "assistant", "text": f"reply {i}"})
    return msgs + (extra or [])


REPLY = """Here is the graph:
```json
{"nodes": [
  {"name": "Campfires", "type": "PROJECT", "description": "Stalker 2 mod: sit at campfires."},
  {"name": "Zone Kit", "type": "TOOL"},
  {"name": "Reinit swap on sit", "type": "DECISION", "description": "Tried in build 3; crashed."},
  {"name": "Short answers", "type": "PREFERENCE", "scope": "global"},
  {"name": "User", "type": "PERSON"},
  {"name": "Bogus", "type": "SPACESHIP"}
 ],
 "edges": [
  {"source": "Campfires", "predicate": "uses", "target": "Zone Kit"},
  {"source": "Campfires", "predicate": "decided", "target": "Reinit swap on sit"},
  {"source": "User", "predicate": "prefers", "target": "Short answers"},
  {"source": "Campfires", "predicate": "teleports", "target": "Zone Kit"}
 ],
 "aliases": [{"entity": "Zone Kit", "alias": "ZoneKit"}]}
```"""


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["GRAPHCRAFT_DB"] = str(Path(self.tmp.name) / "graph.db")
        capture.queue_dir().mkdir(parents=True)

    def tearDown(self):
        del os.environ["GRAPHCRAFT_DB"]
        self.tmp.cleanup()

    def job(self, messages, sid="s1", name=None):
        path = capture.queue_dir() / (name or f"{sid}-{time.time_ns()}.json")
        path.write_text(json.dumps({"version": 1, "sessionId": sid, "project": PROJECT, "trigger": "end",
                                    "at": 0, "messages": messages}), encoding="utf-8")
        return str(path)

    def reply(self, text=REPLY):
        path = Path(self.tmp.name) / f"reply-{time.time_ns()}.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def run_job(self, messages, sid="s1", text=REPLY):
        file = capture.claim(self.job(messages, sid))["claimed"]
        prep = capture.prepare(file)
        self.assertNotIn("skip", prep, prep)
        return capture.apply(file, self.reply(text))

    # --- prepare ---

    def test_short_conversation_is_skipped(self):
        prep = capture.prepare(self.job(conversation(2)))
        self.assertIn("skip", prep)

    def test_prompt_carries_vocabulary_project_and_cleaned_text(self):
        secret = "my key is sk-abcdefghijklmnopqrstuvwx and password: hunter2"
        msgs = conversation(3, [{"role": "user", "text": f"<system-reminder>hidden note</system-reminder>{secret}"}])
        prep = capture.prepare(self.job(msgs))
        self.assertIn("PREFERENCE", prep["system"])
        self.assertIn("blocked_by", prep["system"])
        self.assertIn('named "Campfires"', prep["system"])
        self.assertNotIn("sk-abcdefghijklmnop", prep["prompt"])
        self.assertNotIn("hunter2", prep["prompt"])
        self.assertNotIn("hidden note", prep["prompt"])
        self.assertIn("[redacted]", prep["prompt"])

    def test_known_names_are_offered_for_reuse(self):
        self.run_job(conversation(3))
        prep = capture.prepare(self.job(conversation(3, [{"role": "user", "text": "new: Zone Kit froze"}]), sid="s2"))
        self.assertIn("Zone Kit (TOOL)", prep["system"])

    def test_long_conversation_keeps_its_end(self):
        msgs = conversation(3) + [{"role": "user", "text": "x" * 2000} for _ in range(30)]
        msgs.append({"role": "user", "text": "THE LAST WORD"})
        prep = capture.prepare(self.job(msgs))
        self.assertIn("THE LAST WORD", prep["prompt"])
        self.assertLessEqual(len(prep["prompt"]), capture.MAX_DIGEST_CHARS + 100)

    # --- apply ---

    def test_apply_stores_valid_facts_and_drops_the_rest(self):
        out = self.run_job(conversation(3))
        self.assertEqual(out["facts"], 3)
        self.assertEqual(len(out["dropped"]), 2)  # SPACESHIP node, "teleports" edge
        with closing(store.connect()) as db:
            origin = db.execute("SELECT kind, ref, project FROM origins").fetchone()
            scopes = dict(db.execute("SELECT name, scope FROM entities"))
        self.assertEqual(origin, ("session", "s1", PROJECT))
        self.assertEqual(scopes["Short answers"], "global")
        self.assertEqual(scopes["Zone Kit"], PROJECT)

    def test_type_word_is_stripped_from_names_and_links_follow(self):
        text = json.dumps({
            "nodes": [{"name": "Campfires", "type": "PROJECT"}, {"name": "Linked layer decision", "type": "DECISION"},
                      {"name": "Swap crash problem", "type": "PROBLEM"}, {"name": "Problem", "type": "PROBLEM"}],
            "edges": [{"source": "Campfires", "predicate": "decided", "target": "Linked layer decision"},
                      {"source": "Linked layer decision", "predicate": "because", "target": "Swap crash problem"}],
            "aliases": [{"entity": "Swap crash problem", "alias": "the crash"}]})
        out = self.run_job(conversation(3), text=text)
        self.assertEqual((out["facts"], out["dropped"]), (2, []))
        with closing(store.connect()) as db:
            names = {r[0] for r in db.execute("SELECT name FROM entities")}
        self.assertEqual(names, {"Campfires", "Linked layer", "Swap crash", "Problem"})  # a bare type word stays

    def test_prompt_states_link_rules_and_what_to_leave_out(self):
        system = capture.prepare(self.job(conversation(3)))["system"]
        self.assertIn("Never add the type to the", system)
        self.assertIn("a plan is not a dependency", system)
        self.assertIn("DECISION because PROBLEM, CONCEPT or PREFERENCE", system)

    def test_first_capture_names_the_session_and_a_users_name_stays(self):
        titled = lambda t: REPLY.replace('{"nodes": [', '{"title": "%s", "nodes": [' % t, 1)
        self.assertEqual(self.run_job(conversation(3), text=titled("Campfire sit animation"))["title"],
                         "Campfire sit animation")
        with closing(store.connect()) as db:
            store.set_title(db, store.find_session(db, "s1")[0], "My own name", "user")
        out = self.run_job(conversation(6), text=titled("Something else"))  # a later capture of the same session
        self.assertEqual(out["title"], "My own name")
        self.assertEqual(capture.recent(5)["captures"][0]["title"], "My own name")

    def test_apply_cleans_up_queue_and_reply(self):
        file = capture.claim(self.job(conversation(3)))["claimed"]
        reply = self.reply()
        capture.apply(file, reply)
        self.assertFalse(Path(file).exists())
        self.assertFalse(Path(reply).exists())
        self.assertEqual(capture.recent(5)["captures"][0]["event"], "applied")

    def test_bad_reply_is_an_error_not_a_crash(self):
        file = capture.claim(self.job(conversation(3)))["claimed"]
        with self.assertRaises(ValueError):
            capture.apply(file, self.reply("Sorry, I can't help with that."))

    def test_same_messages_are_not_extracted_twice(self):
        msgs = conversation(3)
        self.run_job(msgs)  # captured at compaction
        later = msgs + [{"role": "user", "text": "one more thing"}]  # session end sees everything again
        prep = capture.prepare(self.job(later))
        self.assertIn("skip", prep)
        self.assertIn("1 new user message", prep["skip"])

    def test_retire_from_a_later_session(self):
        self.run_job(conversation(3))
        retire = json.dumps({"nodes": [], "edges": [],
                             "retire": [{"source": "Campfires", "predicate": "uses", "target": "Zone Kit"}]})
        out = self.run_job(conversation(3), sid="s2", text=retire)
        self.assertEqual(out["retired"], 1)
        facts = {(t["source"], t["predicate"], t["target"]) for t in recall.recall("Campfires")["triples"]}
        self.assertNotIn(("Campfires", "uses", "Zone Kit"), facts)

    def test_undo_last_removes_that_session(self):
        self.run_job(conversation(3))
        out = capture.undo("last")
        self.assertEqual(out["undone"], "s1")
        with closing(store.connect()) as db:
            self.assertEqual(store.stats(db)["relations"], 0)
        self.assertEqual(capture.undo("last")["undone"], None)

    def test_captured_facts_are_recalled(self):
        self.run_job(conversation(3))
        out = recall.recall("what does Campfires use?", cwd=PROJECT)
        self.assertIn(("Campfires", "uses", "Zone Kit"),
                      {(t["source"], t["predicate"], t["target"]) for t in out["triples"]})

    # --- queue handling ---

    def test_claim_is_exclusive(self):
        file = self.job(conversation(3))
        self.assertIsNotNone(capture.claim(file)["claimed"])
        self.assertIsNone(capture.claim(file)["claimed"])

    def test_stale_claim_is_requeued(self):
        claimed = capture.claim(self.job(conversation(3)))["claimed"]
        old = time.time() - capture.STALE_S - 60
        os.utime(claimed, (old, old))
        self.assertEqual(len(capture.pending()["files"]), 1)

    def test_three_failures_move_to_failed(self):
        file = self.job(conversation(3), name="s1-1.json")
        for i in range(capture.MAX_TRIES):
            claimed = capture.claim(file)["claimed"]
            out = capture.fail(claimed, "model: api-error")
        self.assertTrue(out["gaveUp"])
        self.assertEqual(capture.pending()["files"], [])
        self.assertTrue((capture.data_dir() / "failed" / "s1-1.json").exists())

    # --- drafts (the running copy the mod rewrites after every reply) ---

    def draft(self, sid, ended=False, age_s=0):
        capture.drafts_dir().mkdir(parents=True, exist_ok=True)
        path = capture.drafts_dir() / f"{sid}.json"
        path.write_text(json.dumps({"version": 1, "sessionId": sid, "project": PROJECT, "trigger": "turn",
                                    "at": 0, "messages": conversation(3)}), encoding="utf-8")
        if age_s:
            old = time.time() - age_s
            os.utime(path, (old, old))
        if ended:
            path.with_suffix(".ended").write_text("1", encoding="utf-8")
        return path

    def test_ended_draft_is_queued_at_once(self):
        path = self.draft("d1", ended=True)
        files = capture.pending()["files"]
        self.assertEqual(len(files), 1)
        self.assertIn("d1-", files[0])
        self.assertFalse(path.exists())
        self.assertFalse(path.with_suffix(".ended").exists())

    def test_open_session_draft_waits(self):
        self.draft("d2")
        self.assertEqual(capture.pending()["files"], [])

    def test_idle_draft_without_marker_is_queued(self):
        self.draft("d3", age_s=capture.IDLE_DRAFT_S + 60)
        self.assertEqual(len(capture.pending()["files"]), 1)

    def test_orphan_marker_is_cleaned(self):
        capture.drafts_dir().mkdir(parents=True, exist_ok=True)
        marker = capture.drafts_dir() / "d4.ended"
        marker.write_text("1", encoding="utf-8")
        capture.pending()
        self.assertFalse(marker.exists())

    def test_draft_flows_into_facts(self):
        self.draft("d5", ended=True)
        file = capture.claim(capture.pending()["files"][0])["claimed"]
        self.assertIn("system", capture.prepare(file))
        self.assertEqual(capture.apply(file, self.reply())["sessionId"], "d5")

    def test_cli_round_trip(self):
        file = self.job(conversation(3))
        env = {**os.environ}

        def cli(*args):
            r = subprocess.run([sys.executable, "-I", str(ROOT / "graph" / "capture.py"), *args],
                               capture_output=True, env=env, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            return json.loads(r.stdout)

        self.assertEqual(cli("pending")["files"], [file])
        claimed = cli("claim", file)["claimed"]
        self.assertIn("system", cli("prepare", claimed))
        self.assertEqual(cli("apply", claimed, self.reply())["facts"], 3)
        self.assertIn("error", cli("apply", claimed, self.reply()))  # file is gone: an error, still JSON


if __name__ == "__main__":
    unittest.main()
