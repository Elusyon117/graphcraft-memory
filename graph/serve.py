r"""Recall kept warm: one Python process for the session, so a prompt does not pay for starting Python (about
230 ms on Windows) every time. No model here either (CLAUDE.md rule 1).

    py -I serve.py            prints {"port": N} on its first line, then answers until it is stopped

It listens on 127.0.0.1 only, on a free port, and answers only a request carrying the token the mod sets in
GRAPHCRAFT_TOKEN (so no other program on the PC can read memory through it):

    POST /recall   {"prompt", "cwd", "session"}  ->  recall.py --json's answer

The mod starts it at session start and falls back to running recall.py once per prompt whenever it is not answering.
It ends with the session (the mod's spawn loop), or on its own after IDLE_S without a request.
"""
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recall  # noqa: E402
import store  # noqa: E402

IDLE_S = 6 * 3600
MAX_BODY = 64 * 1024
last = [time.time()]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet: stderr is the mod's debug log
        pass

    def answer(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802 (http.server's name)
        token = os.environ.get("GRAPHCRAFT_TOKEN", "")
        if not token or not hmac.compare_digest(self.headers.get("X-Token", ""), token):
            return self.answer(403, {"error": "forbidden"})
        if self.path != "/recall":
            return self.answer(404, {"error": "not found"})
        size = int(self.headers.get("Content-Length") or 0)
        if size > MAX_BODY:
            return self.answer(413, {"error": "too large"})
        last[0] = time.time()
        try:
            data = json.loads(self.rfile.read(size).decode("utf-8") or "{}")
            prompt, cwd = str(data.get("prompt", "")), str(data.get("cwd", ""))
            out = recall.recall(prompt, cwd, session=str(data.get("session", "")))
            out["db"] = str(store.db_path())
            out["prompt_chars"] = len(prompt)
            self.answer(200, out)
        except Exception as e:  # noqa: BLE001 - the mod falls back to recall.py
            self.answer(500, {"error": f"{type(e).__name__}: {e}"})


def main() -> int:
    if not os.environ.get("GRAPHCRAFT_TOKEN"):
        print(json.dumps({"error": "GRAPHCRAFT_TOKEN is not set"}), flush=True)
        return 2
    server = HTTPServer(("127.0.0.1", 0), Handler)
    print(json.dumps({"port": server.server_address[1]}), flush=True)

    def idle():
        while time.time() - last[0] < IDLE_S:
            time.sleep(60)
        server.shutdown()

    threading.Thread(target=idle, daemon=True).start()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
