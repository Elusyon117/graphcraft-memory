"""graph/serve.py, the warm recall process: started for real, asked over loopback."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "graph"))

import store  # noqa: E402

FIXTURE = json.loads((HERE / "fixture.json").read_text(encoding="utf-8"))
VOCAB = store.load_vocabulary([HERE / "fixture_vocab.json"])


class ServeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = Path(cls.tmp.name) / "graph.db"
        with closing(store.connect(db_path)) as db:
            store.upsert(db, FIXTURE, store.add_origin(db, "file", "fixture.json"), VOCAB)
        env = {**os.environ, "GRAPHCRAFT_DB": str(db_path), "GRAPHCRAFT_TOKEN": "secret-token"}
        cls.proc = subprocess.Popen([sys.executable, "-I", str(ROOT / "graph" / "serve.py")], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        cls.port = json.loads(cls.proc.stdout.readline())["port"]

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait(timeout=10)
        cls.proc.stdout.close()
        cls.proc.stderr.close()
        cls.tmp.cleanup()

    def post(self, body, token="secret-token", path="/recall"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
                                     headers={"X-Token": token, "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_answers_like_recall_py(self):
        status, out = self.post({"prompt": "why did Campfires crash?", "cwd": ""})
        self.assertEqual(status, 200)
        self.assertGreater(out["facts"], 0)
        self.assertIn("db", out)

    def test_refuses_without_the_token(self):
        self.assertEqual(self.post({"prompt": "Campfires"}, token="wrong")[0], 403)
        self.assertEqual(self.post({"prompt": "Campfires"}, token="")[0], 403)

    def test_only_recall(self):
        self.assertEqual(self.post({}, path="/other")[0], 404)

    def test_warm_is_fast(self):
        self.post({"prompt": "Campfires"})
        t0 = time.perf_counter()
        self.post({"prompt": "the game crashed again in Campfires"})
        ms = (time.perf_counter() - t0) * 1000
        self.assertLess(ms, 100, f"a warm recall took {ms:.0f} ms")

    def test_will_not_start_without_a_token(self):
        env = {k: v for k, v in os.environ.items() if k != "GRAPHCRAFT_TOKEN"}
        r = subprocess.run([sys.executable, "-I", str(ROOT / "graph" / "serve.py")], env=env, capture_output=True,
                           timeout=30)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
