r"""Recall: find the things a prompt names, walk their links, return the nearest facts as text.

    py -I recall.py "why did the campfire crash?" [--cwd DIR] [--hops 2] [--json]
    py -I recall.py --stdin --json          (the prompt hook: reads {"prompt", "cwd"} from stdin)

No model anywhere in this file (CLAUDE.md rule 1): plain SQLite, so it is deterministic and fast. With no database
yet it answers "nothing" without creating one.

Steps:
1. Seed: every 1-4 word run of the prompt, normalised, looked up in entity names and aliases (indexed). The current
   project's PROJECT node (by folder name) joins the seeds.
2. Walk: breadth first, `hops` steps, retired facts skipped. A node with more than HUB links that is not a seed is
   not walked through, so one busy node cannot pull in half the graph.
3. Rank: nearest first, then facts from this project or global before other projects' facts.
4. Trim: at most MAX_FACTS facts and MAX_CHARS characters (about 400 tokens).
"""
import argparse
import json
import os
import re
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

MAX_NGRAM = 4
HUB = 25
MAX_FACTS = 14
MAX_CHARS = 1800
STOP = {"the", "and", "for", "with", "this", "that", "what", "why", "how", "when", "where", "who", "was", "are",
        "you", "can", "did", "does", "not", "but", "from", "into", "then", "there", "it's", "its", "our", "your"}
HEADER = ("graphcraft-memory: facts from earlier sessions and installed mods that are linked to this prompt. "
          "Use them when relevant; they can be out of date, and what you read in files now wins.")


def ngrams(text: str) -> list:
    words = re.findall(r"[\w][\w.+'-]*", text.lower())
    words = [w.rstrip(".'-") for w in words]
    out = []
    for n in range(1, MAX_NGRAM + 1):
        for i in range(len(words) - n + 1):
            run = words[i:i + n]
            if n == 1 and (len(run[0]) < 3 or run[0] in STOP):
                continue
            norm = store.normalise(" ".join(run))
            if norm:
                out.append(norm)
    return list(dict.fromkeys(out))


def _ids_for(db, norms: list) -> set:
    ids = set()
    for i in range(0, len(norms), 400):
        chunk = norms[i:i + 400]
        marks = ",".join("?" * len(chunk))
        ids |= {r[0] for r in db.execute(f"SELECT id FROM entities WHERE norm IN ({marks})", chunk)}  # noqa: S608
        ids |= {r[0] for r in db.execute(f"SELECT entity_id FROM aliases WHERE norm IN ({marks})", chunk)}  # noqa: S608
    return ids


def project_seed(db, cwd: str) -> set:
    if not cwd:
        return set()
    name = store.normalise(Path(cwd).name)
    rows = db.execute("SELECT id FROM entities WHERE type='PROJECT' AND norm=?"
                      " UNION SELECT a.entity_id FROM aliases a JOIN entities e ON e.id=a.entity_id"
                      " WHERE e.type='PROJECT' AND a.norm=?", (name, name))
    return {r[0] for r in rows}


def walk(db, seeds: set, hops: int) -> dict:
    """{entity_id: depth} reached from the seeds."""
    depth = {s: 0 for s in seeds}
    frontier = set(seeds)
    for d in range(1, hops + 1):
        expand = [n for n in frontier if n in seeds or _degree(db, n) <= HUB]
        if not expand:
            break
        marks = ",".join("?" * len(expand))
        rows = db.execute(
            f"SELECT source_id, target_id FROM relations WHERE retired IS NULL"  # noqa: S608
            f" AND (source_id IN ({marks}) OR target_id IN ({marks}))", expand + expand)
        nxt = set()
        for s, t in rows:
            for n in (s, t):
                if n not in depth:
                    depth[n] = d
                    nxt.add(n)
        frontier = nxt
    return depth


def _degree(db, eid: str) -> int:
    return db.execute("SELECT COUNT(*) FROM relations WHERE retired IS NULL AND (source_id=? OR target_id=?)",
                      (eid, eid)).fetchone()[0]


def recall(prompt: str, cwd: str = "", hops: int = 2, db=None) -> dict:
    t0 = time.perf_counter()
    empty = {"facts": 0, "ms": 0.0, "summary": "", "context": "", "triples": [], "matched": [], "more": 0}
    if db is None:
        db = store.connect(create=False)
        if db is None:
            return empty
        with closing(db):
            return recall(prompt, cwd, hops, db)

    seeds = _ids_for(db, ngrams(prompt))
    if not seeds:  # nothing named: the project alone would only add noise
        empty["ms"] = (time.perf_counter() - t0) * 1000
        return empty
    named = sorted(seeds)
    seeds |= project_seed(db, cwd)
    depth = walk(db, seeds, hops)
    ids = list(depth)

    marks = ",".join("?" * len(ids))
    rows = db.execute(
        f"SELECT es.name, r.predicate, et.name, r.source_id, r.target_id, o.kind, o.ref, o.project"  # noqa: S608
        f" FROM relations r JOIN entities es ON es.id=r.source_id JOIN entities et ON et.id=r.target_id"
        f" JOIN origins o ON o.id=r.origin_id"
        f" WHERE r.retired IS NULL AND r.source_id IN ({marks}) AND r.target_id IN ({marks})", ids + ids).fetchall()

    here = os.path.normcase(os.path.abspath(cwd)) if cwd else ""

    def rank(row):
        near = min(depth.get(row[3], 99), depth.get(row[4], 99))
        proj = os.path.normcase(os.path.abspath(row[7])) if row[7] else ""
        other_project = 1 if proj and proj != here else 0
        return (near, other_project, row[0], row[1], row[2])

    triples, seen, used = [], set(), set()
    for row in sorted(rows, key=rank):
        key = (row[3], row[1], row[4])
        if key in seen:
            continue
        seen.add(key)
        triples.append({"source": row[0], "predicate": row[1], "target": row[2],
                        "origin": f"{row[5]}:{row[6]}", "near": rank(row)[0],
                        "source_id": row[3], "target_id": row[4]})
        used |= {row[3], row[4]}

    lines, size = [], len(HEADER)
    for t in triples[:MAX_FACTS]:
        line = f"{t['source']} --[{t['predicate']}]--> {t['target']}   ({t['origin']})"
        if size + len(line) > MAX_CHARS:
            break
        lines.append(line)
        size += len(line) + 1
    kept = triples[:len(lines)]

    names = {n for t in kept for n in (t["source"], t["target"])}
    notes = []
    for name, desc in db.execute(
            f"SELECT name, description FROM entities WHERE description != '' AND id IN ({marks})", ids):  # noqa: S608
        if name in names and size + len(desc) < MAX_CHARS:
            notes.append(f"  {name}: {desc}")
            size += len(name) + len(desc) + 4

    ms = (time.perf_counter() - t0) * 1000
    if not lines:
        empty["ms"] = ms
        return empty
    context = HEADER + "\n\n" + "\n".join(lines) + ("\n\nwhere:\n" + "\n".join(notes) if notes else "")
    marks = ",".join("?" * len(named))
    matched = [r[0] for r in db.execute(f"SELECT name FROM entities WHERE id IN ({marks})", named)]  # noqa: S608
    return {"facts": len(lines), "ms": ms, "summary": f"memory: {len(lines)} facts, {ms:.0f} ms",
            "context": context, "triples": kept, "matched": matched, "more": len(triples) - len(kept)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prompt", nargs="?", default="")
    ap.add_argument("--cwd", default="")
    ap.add_argument("--hops", type=int, default=2)
    ap.add_argument("--stdin", action="store_true", help='read {"prompt", "cwd"} as JSON from stdin')
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    prompt, cwd = args.prompt, args.cwd
    if args.stdin:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
        prompt, cwd = data.get("prompt", ""), data.get("cwd", "")
    out = recall(prompt, cwd, args.hops)
    out["db"] = str(store.db_path())  # so the hook's debug line says which graph was read
    out["prompt_chars"] = len(prompt)  # and how much prompt text actually arrived
    print(json.dumps(out) if args.json else (out["context"] or "(no memory matches)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
