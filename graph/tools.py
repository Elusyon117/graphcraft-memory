r"""The memory tools Claude can call: memory_search and memory_remember. No model here (CLAUDE.md rule 1).

    py -I tools.py vocab                                      the types and predicates in force, for the tool text
    py -I tools.py search QUERY [--cwd DIR] [--hops 2]        facts linked to QUERY, more than the prompt hook gives
    py -I tools.py remember FILE --session ID [--project DIR] add a {nodes, edges, retire} batch Claude wrote

search: the prompt hook's seeding first (names and aliases in QUERY); when that finds nothing, any node whose name or
description holds a word of QUERY, so "post-process swap" still finds "Post-process AnimBP swap crash".
remember: the same checks as capture (vocabulary, link types, type words in names) and the same origin as this
session's capture (session:<id>), so /graph-memory-undo and the pane's Undo session take it back out (rule 5).
Prints one JSON object.
"""
import argparse
import json
import os
import re
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402
import recall  # noqa: E402
import store  # noqa: E402

SEARCH_FACTS = 40
SEARCH_CHARS = 6000
MAX_REMEMBER_EDGES = 15


def vocab() -> dict:
    v = capture.active_vocabulary()
    return {"types": v["types"], "predicates": v["predicates"]}


def _word_seeds(db, query: str) -> set:
    """Nodes matching the most words of QUERY: in their names first, else in their descriptions."""
    words = [w for w in re.findall(r"[\w][\w.+'-]*", query.lower()) if len(w) >= 3 and w not in recall.STOP][:12]
    for column in ("name", "description"):
        hits = {}
        for w in words:
            for (eid,) in db.execute(f"SELECT id FROM entities WHERE lower({column}) LIKE ?", (f"%{w}%",)):  # noqa: S608
                hits[eid] = hits.get(eid, 0) + 1
        if hits:
            best = max(hits.values())
            return {eid for eid, n in hits.items() if n == best}
    return set()


def search(query: str, cwd: str = "", hops: int = 2) -> dict:
    db = store.connect(create=False)
    if db is None:
        return {"facts": 0, "text": "Memory is empty: nothing has been captured or remembered yet."}
    with closing(db):
        hops = max(1, min(3, hops))
        seeds = recall._ids_for(db, recall.ngrams(query)) or _word_seeds(db, query)
        out = recall.recall(query, cwd, hops, db, SEARCH_FACTS, SEARCH_CHARS, seeds=seeds, with_project=False)
    if not out["facts"]:
        return {"facts": 0, "text": f"Nothing in memory matches {query!r}."}
    text = out["context"].replace(recall.HEADER, f"Memory facts linked to {query!r} (within {hops} links):", 1)
    return {"facts": out["facts"], "text": text, "triples": out["triples"]}


def remember(file: str, session: str, project: str = "") -> dict:
    batch = json.loads(Path(file).read_text(encoding="utf-8-sig"))
    Path(file).unlink(missing_ok=True)
    if not isinstance(batch, dict):
        return {"error": "expected an object with nodes and edges"}
    edges = batch.get("edges") or []
    if len(edges) > MAX_REMEMBER_EDGES:
        return {"error": f"{len(edges)} edges: remember at most {MAX_REMEMBER_EDGES} at a time, the ones that matter"}
    batch = capture.strip_type_words(batch)
    batch["scope"] = project or "global"
    for n in batch.get("nodes", []):
        if n.get("scope") != "global":
            n.pop("scope", None)
    with closing(store.connect()) as db:
        origin = store.add_origin(db, "session", session, project)
        report = store.upsert(db, batch, origin, capture.active_vocabulary())
        retired = sum(store.retire(db, r.get("source", ""), r.get("predicate", ""), r.get("target", ""))
                      for r in batch.get("retire", []) if isinstance(r, dict))
    capture._log({"event": "remembered", "sessionId": session, "project": project, "facts": report["edges"],
                  "nodes": report["nodes"], "retired": retired, "dropped": report["dropped"]})
    return {"facts": report["edges"], "nodes": report["nodes"], "retired": retired, "dropped": report["dropped"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("vocab")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--cwd", default="")
    s.add_argument("--hops", type=int, default=2)
    r = sub.add_parser("remember")
    r.add_argument("file")
    r.add_argument("--session", required=True)
    r.add_argument("--project", default="")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        if args.cmd == "vocab":
            out = vocab()
        elif args.cmd == "search":
            out = search(args.query, args.cwd, args.hops)
        else:
            out = remember(args.file, args.session, args.project)
    except Exception as e:  # one JSON answer either way, so the mod can hand Claude the reason
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
