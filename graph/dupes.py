r"""One thing saved twice under different names ("warm recall" and "Warm recall process"): find likely pairs, join
them, take a join back. store.merge_duplicates already joins nodes that share a name; this is for names that differ.

    py -I dupes.py suggest [--limit N]          likely pairs, best first, each with the way round it would merge
    py -I dupes.py merge FROM INTO              join FROM into INTO (names or ids); FROM's name becomes an alias
    py -I dupes.py undo [ID|last]               take a merge back
    py -I dupes.py distinct A B                 "not the same": never suggest this pair again

Merging moves every fact (live and retired) from one node to the other with its source unchanged (rule 5), so
undo, the timeline and /graph-memory-undo keep working. It refuses when a fact would no longer fit the vocabulary
with the kept node's type. Each merge keeps a snapshot, so undo puts back exactly what was there.
No model is used: suggestions come from the word stems recall already indexes (graph/terms.py).
"""
import argparse
import itertools
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import terms  # noqa: E402

THRESHOLD = 0.4
SAME_KIND_THRESHOLD = 0.6  # two tools or two projects need more in common: their names are usually exact
COMMON_WORD = 40          # a word on more nodes than this does not make two of them candidates
NEVER = {"FILE"}          # file names are exact; two different ones are two files
# Two things linked like this are two things, not one said twice.
DISTINCT_LINKS = {"because", "fixed_by", "decided", "blocked_by", "conflicts_with", "wins_over", "overrides", "patches",
                  "depends_on", "works_on", "uses", "prefers", "avoids", "crashed_with", "failed_because", "tried"}
# Which of two to keep: the more specific kind.
SPECIFIC = {"CONCEPT": 0}

SCHEMA = """
CREATE TABLE IF NOT EXISTS merges (
  id INTEGER PRIMARY KEY, at REAL NOT NULL, from_name TEXT NOT NULL, into_id TEXT NOT NULL, snapshot TEXT NOT NULL,
  undone REAL);
CREATE TABLE IF NOT EXISTS distinct_pairs (a TEXT NOT NULL, b TEXT NOT NULL, at REAL NOT NULL, UNIQUE(a, b));
"""


def connect():
    db = store.connect()
    db.executescript(SCHEMA)
    return db


def _resolve(db, ref: str) -> str | None:
    if db.execute("SELECT 1 FROM entities WHERE id=?", (ref,)).fetchone():
        return ref
    return store._find_by_name(db, ref)  # noqa: SLF001 - the store's own name lookup


def _fits(db, drop: str, keep: str, keep_type: str, vocab: dict) -> str:
    """'' when every live fact on `drop` still fits the vocabulary with `keep`'s type, else why not."""
    for s, p, t in db.execute("SELECT source_id, predicate, target_id FROM relations WHERE retired IS NULL"
                              " AND (source_id=? OR target_id=?)", (drop, drop)).fetchall():
        if {s, t} == {drop, keep}:
            continue  # a link between the two disappears with the merge
        if p not in vocab["predicates"]:
            return f"{p} is not in the vocabulary"
        srcs, tgts = store.signature(p, vocab["predicates"][p], vocab["types"])
        st = keep_type if s == drop else db.execute("SELECT type FROM entities WHERE id=?", (s,)).fetchone()[0]
        tt = keep_type if t == drop else db.execute("SELECT type FROM entities WHERE id=?", (t,)).fetchone()[0]
        if (srcs and st not in srcs) or (tgts and tt not in tgts):
            return f"'{p}' cannot link a {st.lower()} to a {tt.lower()}"
    return ""


def _vocab():
    import capture  # the vocabulary in force (core plus feeders'), read where capture reads it
    return capture.active_vocabulary()


def suggest(db, limit: int = 5, vocab: dict | None = None) -> list:
    vocab = vocab or _vocab()
    live = {r[0] for r in db.execute("SELECT source_id FROM relations WHERE retired IS NULL"
                                     " UNION SELECT target_id FROM relations WHERE retired IS NULL")}
    links, count = {}, {}
    for s, p, t in db.execute("SELECT source_id, predicate, target_id FROM relations WHERE retired IS NULL"):
        links.setdefault(frozenset((s, t)), set()).add(p)
        count[s] = count.get(s, 0) + 1
        count[t] = count.get(t, 0) + 1
    dismissed = {frozenset(r) for r in db.execute("SELECT a, b FROM distinct_pairs")}
    vec, df = {}, {}
    for i, n, t, d in db.execute("SELECT id, name, type, description FROM entities"):
        if i not in live or t in NEVER:
            continue
        nt = terms.terms_of(n)
        vec[i] = (n, t, nt, terms.terms_of(d) - nt)
        for w in vec[i][2] | vec[i][3]:
            df[w] = df.get(w, 0) + 1
    total = len(vec) or 1

    def weights(v):  # name words count double; rarer words count more
        w = {x: 2 * math.log(1 + total / df[x]) for x in v[2]}
        w.update({x: math.log(1 + total / df[x]) for x in v[3] if x not in w})
        return w

    w = {i: weights(v) for i, v in vec.items()}
    norm = {i: math.sqrt(sum(x * x for x in ws.values())) or 1 for i, ws in w.items()}
    # Only nodes that share a word can match: pairs from each word's nodes, skipping words on very many nodes
    # (they say little), so a big graph is not compared pair by pair.
    by_word = {}
    for i, v in vec.items():
        for x in v[2] | v[3]:
            by_word.setdefault(x, []).append(i)
    candidates = set()
    for ids in by_word.values():
        if len(ids) <= COMMON_WORD:
            candidates.update(itertools.combinations(sorted(ids), 2))
    out = []
    for a, b in sorted(candidates):
        ta, tb = vec[a][1], vec[b][1]
        if ta != tb and "CONCEPT" not in (ta, tb):
            continue  # a decision and the problem it fixes are two things
        if frozenset((a, b)) in dismissed or links.get(frozenset((a, b)), set()) & DISTINCT_LINKS:
            continue
        shared = w[a].keys() & w[b].keys()
        if not shared:
            continue
        score = sum(w[a][x] * w[b][x] for x in shared) / (norm[a] * norm[b])
        if score < (SAME_KIND_THRESHOLD if ta == tb and ta != "CONCEPT" else THRESHOLD):
            continue
        # keep the more specific kind, then the one with more facts; fall back to the other way if it does not fit
        order = sorted((a, b), key=lambda x: (SPECIFIC.get(vec[x][1], 1), count.get(x, 0), len(vec[x][0])), reverse=True)
        for keep, drop in (order, order[::-1]):
            if not _fits(db, drop, keep, vec[keep][1], vocab):
                out.append({"score": round(score, 2), "from": _node(vec, drop, count), "into": _node(vec, keep, count)})
                break
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def _node(vec, eid, count) -> dict:
    return {"id": eid, "name": vec[eid][0], "type": vec[eid][1], "facts": count.get(eid, 0)}


def merge(db, drop: str, keep: str, vocab: dict | None = None) -> dict:
    vocab = vocab or _vocab()
    if drop == keep:
        return {"error": "that is the same node"}
    rows = {r[0]: r for r in db.execute("SELECT id, name, norm, type, description, scope, origin_id, created, updated"
                                        " FROM entities WHERE id IN (?, ?)", (drop, keep))}
    if drop not in rows or keep not in rows:
        return {"error": "no such node"}
    why = _fits(db, drop, keep, rows[keep][3], vocab)
    if why:
        return {"error": f"cannot merge {rows[drop][1]} into {rows[keep][1]}: {why}"}
    rels = db.execute("SELECT source_id, predicate, target_id, origin_id, created, retired FROM relations"
                      " WHERE source_id=? OR target_id=?", (drop, drop)).fetchall()
    aliases = db.execute("SELECT alias, norm FROM aliases WHERE entity_id=?", (drop,)).fetchall()
    inserted, added_aliases, moved = [], [], 0
    for s, p, t, o, c, r in rels:
        db.execute("DELETE FROM relations WHERE source_id=? AND predicate=? AND target_id=? AND origin_id=?", (s, p, t, o))
        ns, nt = (keep if s == drop else s), (keep if t == drop else t)
        if ns == nt:
            continue  # a link between the two: one thing is not linked to itself
        cur = db.execute("INSERT OR IGNORE INTO relations(source_id, predicate, target_id, origin_id, created, retired)"
                         " VALUES (?,?,?,?,?,?)", (ns, p, nt, o, c, r))
        if cur.rowcount:
            inserted.append([ns, p, nt, o])
            moved += r is None
    for alias, n in list(aliases) + [(rows[drop][1], rows[drop][2])]:  # its name too: later mentions find `keep`
        if n != rows[keep][2] and db.execute("INSERT OR IGNORE INTO aliases(entity_id, alias, norm) VALUES (?,?,?)",
                                             (keep, alias, n)).rowcount:
            added_aliases.append(n)
    if not rows[keep][4] and rows[drop][4]:
        db.execute("UPDATE entities SET description=? WHERE id=?", (rows[drop][4], keep))
    db.execute("DELETE FROM aliases WHERE entity_id=?", (drop,))
    db.execute("DELETE FROM terms WHERE entity_id=?", (drop,))
    db.execute("DELETE FROM entities WHERE id=?", (drop,))
    name, desc = db.execute("SELECT name, description FROM entities WHERE id=?", (keep,)).fetchone()
    terms.index_entity(db, keep, name, desc)
    snapshot = {"entity": list(rows[drop]), "keep_description": rows[keep][4], "relations": [list(x) for x in rels],
                "aliases": [list(x) for x in aliases], "inserted": inserted, "added_aliases": added_aliases}
    mid = db.execute("INSERT INTO merges(at, from_name, into_id, snapshot) VALUES (?,?,?,?)",
                     (time.time(), rows[drop][1], keep, json.dumps(snapshot))).lastrowid
    db.commit()
    return {"merged": rows[drop][1], "into": name, "facts": moved, "merge": mid}


def undo(db, which: str = "last") -> dict:
    row = (db.execute("SELECT id, into_id, snapshot, from_name FROM merges WHERE undone IS NULL ORDER BY id DESC LIMIT 1")
           .fetchone() if which == "last" else
           db.execute("SELECT id, into_id, snapshot, from_name FROM merges WHERE id=? AND undone IS NULL", (int(which),))
           .fetchone())
    if not row:
        return {"error": "no merge to undo"}
    mid, keep, snap, name = row[0], row[1], json.loads(row[2]), row[3]
    for s, p, t, o in snap["inserted"]:
        db.execute("DELETE FROM relations WHERE source_id=? AND predicate=? AND target_id=? AND origin_id=?", (s, p, t, o))
    db.execute("INSERT OR REPLACE INTO entities(id, name, norm, type, description, scope, origin_id, created, updated)"
               " VALUES (?,?,?,?,?,?,?,?,?)", snap["entity"])
    db.executemany("INSERT OR IGNORE INTO relations(source_id, predicate, target_id, origin_id, created, retired)"
                   " VALUES (?,?,?,?,?,?)", snap["relations"])
    db.executemany("INSERT OR IGNORE INTO aliases(entity_id, alias, norm) VALUES (?,?,?)",
                   [(snap["entity"][0], a, n) for a, n in snap["aliases"]])
    for n in snap["added_aliases"]:
        db.execute("DELETE FROM aliases WHERE entity_id=? AND norm=?", (keep, n))
    db.execute("UPDATE entities SET description=? WHERE id=?", (snap["keep_description"], keep))
    for eid in (snap["entity"][0], keep):
        r = db.execute("SELECT name, description FROM entities WHERE id=?", (eid,)).fetchone()
        if r:
            terms.index_entity(db, eid, r[0], r[1])
    db.execute("UPDATE merges SET undone=? WHERE id=?", (time.time(), mid))
    db.commit()
    return {"undone": mid, "restored": name}


def distinct(db, a: str, b: str) -> dict:
    a, b = sorted((a, b))
    db.execute("INSERT OR IGNORE INTO distinct_pairs(a, b, at) VALUES (?,?,?)", (a, b, time.time()))
    db.commit()
    return {"distinct": [a, b]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("suggest")
    s.add_argument("--limit", type=int, default=5)
    m = sub.add_parser("merge")
    m.add_argument("source")
    m.add_argument("into")
    u = sub.add_parser("undo")
    u.add_argument("which", nargs="?", default="last")
    d = sub.add_parser("distinct")
    d.add_argument("a")
    d.add_argument("b")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    db = connect()
    try:
        if args.cmd == "suggest":
            out = {"pairs": suggest(db, args.limit)}
        elif args.cmd == "merge":
            a, b = _resolve(db, args.source), _resolve(db, args.into)
            missing = [x for x, y in ((args.source, a), (args.into, b)) if not y]
            out = {"error": f"no single node named {missing[0]!r}"} if missing else merge(db, a, b)
        elif args.cmd == "undo":
            out = undo(db, args.which)
        else:
            out = distinct(db, args.a, args.b)
    except Exception as e:  # one JSON answer either way, so the mod can show it
        out = {"error": f"{type(e).__name__}: {e}"}
    finally:
        db.close()
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
