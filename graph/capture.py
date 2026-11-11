r"""Capture: turn saved conversations into facts.

The mod writes each conversation it saves (at session end and before a compaction) to the queue folder as JSON:
    {"version": 1, "sessionId", "project": "<cwd>", "trigger": "end" | "compact", "at": <ms>,
     "messages": [{"role": "user" | "assistant", "text"}]}
The model call itself happens in the mod ($.model.complete); this file does everything around it:

    py -I capture.py pending                         queued files, oldest first (stale claims requeued)
    py -I capture.py claim FILE                      move it to working/ so only one session extracts it
    py -I capture.py prepare FILE                   {"skip": why} or {"system", "prompt", "new"}: what to send
    py -I capture.py apply FILE REPLY_FILE           check the model's reply, store it, drop FILE from the queue
    py -I capture.py fail FILE REASON                count a failed try; the third moves FILE to failed/
    py -I capture.py discard FILE REASON             drop FILE (nothing worth extracting)
    py -I capture.py recent [N]                      the last N captures, newest first
    py -I capture.py undo [SESSION_ID|last]          remove every fact one session added

Every command prints one JSON object. Data lives beside the graph: <data>/pending, <data>/failed, <data>/capture.jsonl.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recall  # noqa: E402
import store  # noqa: E402

PROMPT_FILE = Path(__file__).resolve().parent / "extract_prompt.md"
MIN_USER_TURNS = 3       # fewer new user messages than this: not worth a model call
MAX_DIGEST_CHARS = 24000  # the newest part of the conversation is kept when it is longer
MAX_MESSAGE_CHARS = 2500  # one long paste or reply is cut to its start
MAX_TRIES = 3
KNOWN_NAMES = 60

SECRET = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|\b[0-9a-fA-F]{40,}\b"
    r"|(?i:(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+))")
TAGS = re.compile(r"<(system-reminder|command-[a-z-]+|local-command-[a-z-]+)>.*?</\1>", re.S)


def data_dir() -> Path:
    return store.db_path().parent


def queue_dir() -> Path:
    return data_dir() / "pending"


def _hash(m: dict) -> str:
    return hashlib.sha1(f"{m.get('role')}\n{m.get('text', '')}".encode("utf-8")).hexdigest()


def clean(text: str) -> str:
    text = TAGS.sub("", text or "")
    text = SECRET.sub("[redacted]", text)
    return text.strip()


def _log(entry: dict) -> None:
    data_dir().mkdir(parents=True, exist_ok=True)
    entry = {"at": time.time(), **entry}
    with open(data_dir() / "capture.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _read(file: str) -> dict:
    return json.loads(Path(file).read_text(encoding="utf-8-sig"))


def new_messages(db, job: dict) -> list:
    """The job's messages not already captured for this session, cleaned, empty ones dropped."""
    sid = job.get("sessionId", "")
    seen = {r[0] for r in db.execute("SELECT hash FROM captured WHERE session_id=?", (sid,))}
    out = []
    for m in job.get("messages", []):
        if m.get("role") not in ("user", "assistant"):
            continue
        h = _hash(m)
        text = clean(m.get("text", ""))
        if h in seen or not text:
            continue
        out.append({"role": m["role"], "text": text[:MAX_MESSAGE_CHARS], "hash": h})
    return out


def digest(messages: list) -> str:
    lines = [f"{'User' if m['role'] == 'user' else 'Claude'}: {m['text']}" for m in messages]
    kept, size = [], 0
    for line in reversed(lines):  # newest first, so a long session keeps its end
        if size + len(line) > MAX_DIGEST_CHARS:
            break
        kept.append(line)
        size += len(line) + 2
    return "\n\n".join(reversed(kept))


def active_vocabulary() -> dict:
    """Core, plus vocabulary files feeders have registered in <data>/vocab (milestone 3)."""
    return store.load_vocabulary(sorted((data_dir() / "vocab").glob("*.json")))


def known_names(db, text: str) -> list:
    ids = list(recall._ids_for(db, recall.ngrams(text)))[:KNOWN_NAMES]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return [f"{n} ({t})" for n, t in db.execute(
        f"SELECT name, type FROM entities WHERE id IN ({marks}) ORDER BY name", ids)]  # noqa: S608


def project_name(path: str) -> str:
    return Path(path).name if path else "unknown project"


def prepare(file: str) -> dict:
    job = _read(file)
    with closing(store.connect()) as db:
        return _prepare(db, job)


def _prepare(db, job: dict) -> dict:
    msgs = new_messages(db, job)
    users = sum(1 for m in msgs if m["role"] == "user")
    if users < MIN_USER_TURNS:
        return {"skip": f"{users} new user message(s), fewer than {MIN_USER_TURNS}"}
    text = digest(msgs)
    vocab = active_vocabulary()
    known = known_names(db, text)
    system = (PROMPT_FILE.read_text(encoding="utf-8")
              .replace("{project}", project_name(job.get("project", "")))
              .replace("{types}", "\n".join(f"- {k}: {v}" for k, v in vocab["types"].items()))
              .replace("{predicates}", "\n".join(f"- {k}: {v}" for k, v in vocab["predicates"].items()))
              .replace("{known}", "\n".join(f"- {k}" for k in known) or "(none yet)"))
    return {"system": system, "prompt": f"<conversation>\n{text}\n</conversation>", "new": len(msgs)}


def parse_reply(text: str) -> dict:
    """The JSON object in the model's reply, code fences or chatter around it ignored."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    batch = json.loads(text[start:end + 1])
    if not isinstance(batch, dict):
        raise ValueError("the reply is not a JSON object")
    return batch


def apply(file: str, reply_file: str) -> dict:
    job = _read(file)
    project = job.get("project", "")
    try:
        batch = parse_reply(Path(reply_file).read_text(encoding="utf-8-sig"))
    finally:
        Path(reply_file).unlink(missing_ok=True)
    batch["scope"] = project or "global"
    for n in batch.get("nodes", []):
        if n.get("scope") != "global":
            n.pop("scope", None)

    with closing(store.connect()) as db:
        return _apply(db, job, batch, file)


def _apply(db, job: dict, batch: dict, file: str) -> dict:
    sid, project = job.get("sessionId", ""), job.get("project", "")
    msgs = new_messages(db, job)
    origin = store.add_origin(db, "session", sid, project)
    report = store.upsert(db, batch, origin, active_vocabulary())
    retired = sum(store.retire(db, r.get("source", ""), r.get("predicate", ""), r.get("target", ""))
                  for r in batch.get("retire", []) if isinstance(r, dict))
    now = time.time()
    db.executemany("INSERT OR IGNORE INTO captured(session_id, hash, at) VALUES (?,?,?)",
                   [(sid, m["hash"], now) for m in msgs])
    db.commit()
    Path(file).unlink(missing_ok=True)
    out = {"sessionId": sid, "project": project, "trigger": job.get("trigger"), "facts": report["edges"],
           "nodes": report["nodes"], "retired": retired, "dropped": report["dropped"]}
    _log({"event": "applied", **out})
    return out


def fail(file: str, reason: str) -> dict:
    job = _read(file)
    job["tries"] = job.get("tries", 0) + 1
    moved = job["tries"] >= MAX_TRIES
    if moved:
        dest = data_dir() / "failed"
        dest.mkdir(parents=True, exist_ok=True)
        Path(file).unlink(missing_ok=True)
        (dest / Path(file).name).write_text(json.dumps(job), encoding="utf-8")
    else:  # back in the queue for the next session start
        queue_dir().mkdir(parents=True, exist_ok=True)
        Path(file).unlink(missing_ok=True)
        (queue_dir() / Path(file).name).write_text(json.dumps(job), encoding="utf-8")
    _log({"event": "failed", "sessionId": job.get("sessionId"), "reason": reason, "tries": job["tries"],
          "gaveUp": moved})
    return {"tries": job["tries"], "gaveUp": moved}


def discard(file: str, reason: str) -> dict:
    job = _read(file)
    Path(file).unlink(missing_ok=True)
    _log({"event": "skipped", "sessionId": job.get("sessionId"), "reason": reason})
    return {"skipped": reason}


STALE_S = 15 * 60  # a claimed file older than this was left by a session that died mid-way: queue it again


def working_dir() -> Path:
    return data_dir() / "working"


IDLE_DRAFT_S = 30 * 60  # a draft unchanged this long belongs to a session that ended without saying so


def drafts_dir() -> Path:
    return data_dir() / "drafts"


def promote_drafts() -> list:
    """Queue each finished session's draft: one with an .ended marker, or one left unchanged for IDLE_DRAFT_S.

    A session still open that went idle gets queued too; that is harmless, because a later capture of the same
    session only extracts the messages it has not seen.
    """
    moved = []
    if not drafts_dir().exists():
        return moved
    for draft in drafts_dir().glob("*.json"):
        marker = draft.with_suffix(".ended")
        idle = time.time() - draft.stat().st_mtime
        if not marker.exists() and idle < IDLE_DRAFT_S:
            continue
        queue_dir().mkdir(parents=True, exist_ok=True)
        dest = queue_dir() / f"{draft.stem}-{int(time.time() * 1000)}.json"
        try:
            os.rename(draft, dest)
        except OSError:  # another session promoted it first
            continue
        marker.unlink(missing_ok=True)
        moved.append(str(dest))
    for marker in drafts_dir().glob("*.ended"):  # a marker whose draft never existed (a session with no replies)
        if not marker.with_suffix(".json").exists():
            marker.unlink(missing_ok=True)
    return moved


def pending() -> dict:
    promote_drafts()
    if working_dir().exists():
        for p in working_dir().glob("*.json"):
            if time.time() - p.stat().st_mtime > STALE_S:
                queue_dir().mkdir(parents=True, exist_ok=True)
                p.replace(queue_dir() / p.name)
    files = sorted(queue_dir().glob("*.json"), key=lambda p: p.stat().st_mtime) if queue_dir().exists() else []
    return {"files": [str(p) for p in files]}


def claim(file: str) -> dict:
    """Move FILE to working/ so a second open session does not extract it too. The rename is atomic."""
    working_dir().mkdir(parents=True, exist_ok=True)
    dest = working_dir() / Path(file).name
    try:
        os.rename(file, dest)
        os.utime(dest)
    except OSError:
        return {"claimed": None}
    return {"claimed": str(dest)}


def recent(n: int) -> dict:
    path = data_dir() / "capture.jsonl"
    if not path.exists():
        return {"captures": []}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {"captures": list(reversed(rows))[:n]}


def undo(which: str) -> dict:
    with closing(store.connect()) as db:
        return _undo(db, which)


def _undo(db, which: str) -> dict:
    if which in ("", "last"):
        row = db.execute("SELECT id, ref FROM origins WHERE kind='session' ORDER BY at DESC, id DESC LIMIT 1").fetchone()
    else:
        row = db.execute("SELECT id, ref FROM origins WHERE kind='session' AND ref=?", (which,)).fetchone()
    if not row:
        return {"undone": None, "reason": "no such captured session"}
    removed = store.forget_origin(db, row[0])
    db.execute("DELETE FROM captured WHERE session_id=?", (row[1],))
    db.commit()
    _log({"event": "undone", "sessionId": row[1], **removed})
    return {"undone": row[1], **removed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pending")
    c = sub.add_parser("claim")
    c.add_argument("file")
    p = sub.add_parser("prepare")
    p.add_argument("file")
    a = sub.add_parser("apply")
    a.add_argument("file")
    a.add_argument("reply")
    for name in ("fail", "discard"):
        s = sub.add_parser(name)
        s.add_argument("file")
        s.add_argument("reason", nargs="?", default="")
    r = sub.add_parser("recent")
    r.add_argument("n", nargs="?", type=int, default=5)
    u = sub.add_parser("undo")
    u.add_argument("which", nargs="?", default="last")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        if args.cmd == "pending":
            out = pending()
        elif args.cmd == "claim":
            out = claim(args.file)
        elif args.cmd == "prepare":
            out = prepare(args.file)
        elif args.cmd == "apply":
            out = apply(args.file, args.reply)
        elif args.cmd == "fail":
            out = fail(args.file, args.reason)
        elif args.cmd == "discard":
            out = discard(args.file, args.reason)
        elif args.cmd == "recent":
            out = recent(args.n)
        else:
            out = undo(args.which)
    except Exception as e:  # one JSON answer either way, so the mod can log it
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
