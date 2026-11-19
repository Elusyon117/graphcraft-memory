r"""Recall: find the things a prompt names, walk their links, return the nearest facts as text.

    py -I recall.py "why did the campfire crash?" [--cwd DIR] [--hops 2] [--json]
    py -I recall.py --stdin --json          (the prompt hook: reads {"prompt", "cwd"} from stdin)

No model anywhere in this file (CLAUDE.md rule 1): plain SQLite, so it is deterministic and fast. With no database
yet it answers "nothing" without creating one.

Steps:
1. Seed: every 1-4 word run of the prompt, normalised, looked up in entity names and aliases (indexed). Then the
   word index (terms.py): nodes whose name or description share the prompt's rarer words, stems and synonyms, so a
   vague prompt finds them too; they start one step further out, so exact names rank first. The current project's
   PROJECT node (by folder name) joins the seeds.
2. Walk: breadth first, `hops` steps, retired facts skipped. A node with more than HUB links that is not a seed is
   not walked through, so one busy node cannot pull in half the graph.
   From any node, only its PER_NODE most important links are followed, so a busy node stays fast and its
   weightiest facts are the ones reached.
3. Rank by one score: importance (the kind of link, decisions, problems and preferences, a fact several sources
   said), less NEAR_COST per link away from what the prompt named, less OTHER_PROJECT for another project's facts;
   newest first among equals. A fact linking two things the prompt named gets BOTH_NAMED on top, and a fact
   on a short path between two of them (how they connect) gets PATH per pair it bridges. So an old decision and its reason are not crowded out by many newer, lighter notes
   about the same thing, while a weighty fact one link out still beats a trivial one next to the seed.
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
import terms  # noqa: E402

MAX_NGRAM = 4
HUB = 25
MAX_FACTS = 14
MAX_CHARS = 1800
PER_NODE = 40
NEAR_COST = 3  # a fact about what the prompt named beats context one link out, unless that context is weighty and it is trivial
OTHER_PROJECT = 0.5
# A fact linking two things the prompt named is what it asks about: it comes before any weightier neighbour.
BOTH_NAMED = 5
# A fact on a short path between two things the prompt named (A -> X -> B) is how they connect: no NEAR_COST, and
# PATH for each such pair it lies on, at most PATH_PAIRS pairs. Paths up to PATH_LEN links; hubs and PASS_OVER nodes are not walked
# through, or the project and the user would connect everything. At most PATH_ENDS named things are paired.
PATH = 2
PATH_PAIRS = 2
PATH_LEN = 3
PATH_ENDS = 8
# How much a kind of link says. Reasons, choices, problems and preferences matter most; "part of" and
# "references" are the notes a big graph has thousands of. Unlisted predicates (a feeder's own) weigh 1.
WEIGHT = {"because": 3, "fixed_by": 3, "decided": 3, "blocked_by": 3, "avoids": 3, "prefers": 3, "crashed_with": 3,
          "failed_because": 3, "conflicts_with": 2, "wins_over": 2, "overrides": 2, "depends_on": 2, "advice_in": 2,
          "tried": 2, "patches": 1.5, "uses": 1.5, "works_on": 1, "about": 1, "part_of": 0.5, "references": 0.5}
WEIGHTY_TYPES = {"DECISION", "PROBLEM", "PREFERENCE", "CRASH_FAMILY"}
# Not walked through unless the prompt names them: the user is linked to every project, so passing through "User"
# would bring one project's decisions into another's prompt.
PASS_OVER = {"PERSON"}


def weight(predicate: str, *types: str) -> float:
    return WEIGHT.get(predicate, 1) + (1 if WEIGHTY_TYPES & set(types) else 0)
STOP = {"the", "and", "for", "with", "this", "that", "what", "why", "how", "when", "where", "who", "was", "are",
        "you", "can", "did", "does", "not", "but", "from", "into", "then", "there", "it's", "its", "our", "your"}
HEADER = "graphcraft-memory (linked to this message; may be out of date, files win):"


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


def walk(db, seeds: set, hops: int, later: dict | None = None) -> dict:
    """{entity_id: depth} reached from the seeds; `later` seeds start at the depth given ({id: depth}), so word
    matches and the project rank after the names the prompt said."""
    later = {s: d for s, d in (later or {}).items() if s not in seeds}
    depth = {s: 0 for s in seeds} | later
    frontier = set(seeds)
    for d in range(1, hops + 1):
        frontier |= {s for s in later if depth[s] == d - 1}
        ask = [n for n in frontier if n not in seeds and n not in later]
        info = _hub_info(db, ask)
        expand = [n for n in frontier if n in seeds or n in later or (info.get(n, (0, ""))[0] <= HUB and info.get(n, (0, ""))[1] not in PASS_OVER)]
        if not expand:
            if any(dd >= d for dd in later.values()):
                frontier = set()
                continue  # word matches still to join, one step further out
            break
        marks = ",".join("?" * len(expand))
        rows = db.execute(
            f"SELECT r.source_id, r.target_id, r.predicate, r.created, es.type, et.type FROM relations r"  # noqa: S608
            f" JOIN entities es ON es.id=r.source_id JOIN entities et ON et.id=r.target_id WHERE r.retired IS NULL"
            f" AND (r.source_id IN ({marks}) OR r.target_id IN ({marks}))", expand + expand)
        out, best = set(expand), {}
        for s, t, p, c, st, tt in rows:
            for a, b in ((s, t), (t, s)):
                if a in out and b not in depth:
                    best.setdefault(a, []).append((weight(p, st, tt), c, b))
        nxt = set()
        for links in best.values():  # each node's weightiest links, newest first among equals
            links.sort(reverse=True)
            for _, _, b in links[:PER_NODE]:
                if b not in depth:
                    depth[b] = d
                    nxt.add(b)
        frontier = nxt
    return depth


def short_origin(origin: str) -> str:
    """'session:9df5caa2-ee6c-…' -> 's:9df5caa2': a full id costs ~25 tokens a line, and its first characters are
    enough for /graph-memory-undo and the map. Feeders and files keep their names."""
    kind, _, ref = origin.partition(":")
    return f"s:{ref[:8]}" if kind == "session" else origin


SENT_KEEP_S = 3 * 86400


def _sent_path(session: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session)[:120]
    return store.db_path().parent / "sent" / f"{safe}.txt"


def _load_sent(session: str) -> set:
    try:
        return set(_sent_path(session).read_text(encoding="utf-8").split())
    except OSError:
        return set()


def _save_sent(session: str, keys: set) -> None:
    path = _sent_path(session)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(sorted(keys)), encoding="utf-8")
        now = time.time()
        for old in path.parent.glob("*.txt"):  # sessions long gone
            if now - old.stat().st_mtime > SENT_KEEP_S:
                old.unlink(missing_ok=True)
    except OSError:
        pass  # memory still works; it just may repeat itself


def _hub_info(db, ids: list) -> dict:
    """{id: (live links, type)} in two queries for the whole frontier, instead of two per node."""
    out = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        marks = ",".join("?" * len(chunk))
        types = dict(db.execute(f"SELECT id, type FROM entities WHERE id IN ({marks})", chunk))  # noqa: S608
        deg = dict(db.execute(
            f"SELECT n, COUNT(*) FROM (SELECT source_id AS n FROM relations WHERE retired IS NULL AND source_id IN ({marks})"  # noqa: S608
            f" UNION ALL SELECT target_id FROM relations WHERE retired IS NULL AND target_id IN ({marks})) GROUP BY n",
            chunk + chunk))
        out.update({n: (deg.get(n, 0), types.get(n, "")) for n in chunk})
    return out


def _type(db, eid: str) -> str:
    row = db.execute("SELECT type FROM entities WHERE id=?", (eid,)).fetchone()
    return row[0] if row else ""


def _degree(db, eid: str) -> int:
    return db.execute("SELECT COUNT(*) FROM relations WHERE retired IS NULL AND (source_id=? OR target_id=?)",
                      (eid, eid)).fetchone()[0]


def bridges(db, rows: list, ends: list) -> dict:
    """{(source_id, predicate, target_id): how many pairs of `ends` the fact lies on a shortest path between}, over
    the facts already reached (`rows`: source_id at [3], target_id at [4]). A fact linking two ends directly is left
    out: BOTH_NAMED already lifts it."""
    if len(ends) < 2:
        return {}
    ends_set = set(ends)
    adj = {}
    for r in rows:
        if r[3] != r[4]:
            adj.setdefault(r[3], set()).add(r[4])
            adj.setdefault(r[4], set()).add(r[3])
    info = _hub_info(db, [n for n in adj if n not in ends_set])
    through = {n for n in adj if n in ends_set or (info[n][0] <= HUB and info[n][1] not in PASS_OVER)}

    def dist_from(start):  # BFS, PATH_LEN deep, only passing through `through` nodes
        dist, frontier = {start: 0}, [start]
        for d in range(1, PATH_LEN + 1):
            nxt = []
            for n in frontier:
                if n != start and n not in through:
                    continue
                for m in adj.get(n, ()):
                    if m not in dist:
                        dist[m] = d
                        nxt.append(m)
            frontier = nxt
        return dist

    dist = {e: dist_from(e) for e in ends if e in adj}
    pairs = [(a, b) for i, a in enumerate(dist) for b in list(dist)[i + 1:] if b in dist[a] and dist[a][b] >= 2]
    out = {}
    for r in rows:
        u, v = r[3], r[4]
        if u in ends_set and v in ends_set:
            continue
        n = 0
        for a, b in pairs:
            da, db_, length = dist[a], dist[b], dist[a][b]
            if any(x in da and y in db_ and da[x] + 1 + db_[y] == length and
                   (x == a or x in through) and (y == b or y in through) for x, y in ((u, v), (v, u))):
                n += 1
        if n:
            out[(r[3], r[1], r[4])] = min(n, PATH_PAIRS)
    return out


def recall(prompt: str, cwd: str = "", hops: int | None = None, db=None, max_facts: int | None = None,
           max_chars: int | None = None, seeds: set | None = None, with_project: bool = True, session: str = "") -> dict:
    """The prompt hook's recall; memory_search passes bigger limits and, when it found seeds its own way, those.
    What is not given comes from the user's settings (store.settings): how many facts, how far, which types,
    whether other projects' facts may come."""
    t0 = time.perf_counter()
    cfg = store.settings()
    hops = hops or cfg["recallHops"]
    max_facts = max_facts or cfg["recallFacts"]
    max_chars = max_chars or max(MAX_CHARS, 130 * max_facts)
    skip, others = set(cfg["skipTypes"]), cfg["otherProjects"]
    empty = {"facts": 0, "ms": 0.0, "summary": "", "context": "", "triples": [], "matched": [], "more": 0}
    if db is None:
        db = store.connect(create=False)
        if db is None:
            return empty
        with closing(db):
            return recall(prompt, cwd, hops, db, max_facts, max_chars, seeds, with_project, session)

    fuzzy = set()
    if seeds is None:
        seeds = _ids_for(db, ngrams(prompt))
        fuzzy = set(terms.fuzzy_seeds(db, prompt, exclude=seeds))
    seeds = set(seeds)
    if not seeds and not fuzzy:  # nothing named: the project alone would only add noise
        empty["ms"] = (time.perf_counter() - t0) * 1000
        return empty
    named = sorted(seeds)
    later = {s: 1 for s in fuzzy}
    if with_project:  # context, not the subject: one step out when a name was said, two when only words matched
        for p in project_seed(db, cwd):
            later.setdefault(p, 1 if seeds else 2)
    depth = walk(db, seeds, hops, later)
    ids = list(depth)

    marks = ",".join("?" * len(ids))
    rows = db.execute(
        f"SELECT es.name, r.predicate, et.name, r.source_id, r.target_id, o.kind, o.ref, o.project,"  # noqa: S608
        f" r.created, es.type, et.type"
        f" FROM relations r JOIN entities es ON es.id=r.source_id JOIN entities et ON et.id=r.target_id"
        f" JOIN origins o ON o.id=r.origin_id"
        f" WHERE r.retired IS NULL AND r.source_id IN ({marks}) AND r.target_id IN ({marks})", ids + ids).fetchall()

    here = os.path.normcase(os.path.abspath(cwd)) if cwd else ""
    prompted = seeds | fuzzy
    said = {}  # how many sources said each fact: one said in several sessions weighs more
    for row in rows:
        said[(row[3], row[1], row[4])] = said.get((row[3], row[1], row[4]), 0) + 1
    # Only things named outright: a word match is a guess, and a path to a guess must not outrank the names.
    bridged = bridges(db, rows, sorted(seeds)[:PATH_ENDS])

    def rank(row):
        near = min(depth.get(row[3], 99), depth.get(row[4], 99))
        proj = os.path.normcase(os.path.abspath(row[7])) if row[7] else ""
        other_project = 1 if proj and proj != here else 0
        importance = weight(row[1], row[9], row[10]) + 0.5 * (said[(row[3], row[1], row[4])] - 1)
        both = BOTH_NAMED if row[3] in prompted and row[4] in prompted else 0
        bridges_ = bridged.get((row[3], row[1], row[4]), 0)
        # between two named things is about them: no cost for being a link or two out
        cost = 0 if bridges_ else NEAR_COST * near
        score = importance + both + PATH * bridges_ - cost - OTHER_PROJECT * other_project
        return (-score, near, -row[8], row[0], row[1], row[2])

    triples, seen, used = [], set(), set()
    def allowed(row):  # the user's settings: switched-off types, and other projects' facts when they said so
        if row[9] in skip or row[10] in skip:
            return False
        return others or not (row[7] and here and os.path.normcase(os.path.abspath(row[7])) != here)

    for row in sorted(filter(allowed, rows), key=rank):
        key = (row[3], row[1], row[4])
        if key in seen:
            continue
        seen.add(key)
        triples.append({"source": row[0], "predicate": row[1], "target": row[2],
                        "origin": f"{row[5]}:{row[6]}", "near": rank(row)[1],
                        "source_id": row[3], "target_id": row[4]})
        used |= {row[3], row[4]}

    # What this session was already given still sits in the conversation: send only what is new (the pane still
    # lists everything recalled, marked "again"). Kept per session in a small file, so warm and cold runs agree.
    sent = _load_sent(session) if session else set()
    lines, size, new_keys, again = [], len(HEADER), [], 0
    for t in triples[:max_facts]:
        k = "f:" + "|".join((t["source_id"], t["predicate"], t["target_id"]))
        t["again"] = k in sent
        if t["again"]:
            again += 1
            continue
        line = f"{t['source']} --[{t['predicate']}]--> {t['target']}   ({short_origin(t['origin'])})"
        if size + len(line) > max_chars:
            break
        lines.append(line)
        new_keys.append(k)
        size += len(line) + 1
    kept = [t for t in triples[:max_facts] if t["again"] or "f:" + "|".join((t["source_id"], t["predicate"], t["target_id"])) in new_keys]

    names = {n for t in kept if not t["again"] for n in (t["source"], t["target"])}
    notes = []
    for eid, name, desc in db.execute(
            f"SELECT id, name, description FROM entities WHERE description != '' AND id IN ({marks})", ids):  # noqa: S608
        if name in names and "n:" + eid not in sent and size + len(desc) < max_chars:
            notes.append(f"  {name}: {desc}")
            new_keys.append("n:" + eid)
            size += len(name) + len(desc) + 4

    ms = (time.perf_counter() - t0) * 1000
    if not kept:
        empty["ms"] = ms
        return empty
    if session and new_keys:
        _save_sent(session, sent | set(new_keys))
    tail = f"\n({again} more linked facts were given earlier in this session and still apply.)" if again else ""
    if lines:
        context = HEADER + "\n" + "\n".join(lines) + ("\nwhere:\n" + "\n".join(notes) if notes else "") + tail
    else:
        context = f"graphcraft-memory: the {again} facts linked to this message were given earlier in this session and still apply."

    def names(ids_):
        marks_ = ",".join("?" * len(ids_))
        return [r[0] for r in db.execute(f"SELECT name FROM entities WHERE id IN ({marks_})", sorted(ids_))]  # noqa: S608
    matched = names(named) + ["~" + n for n in names(fuzzy - set(named))]  # ~: found by its words, not its name
    return {"facts": len(kept), "new": len(lines), "ms": ms, "summary": f"memory: {len(kept)} facts" + (f" ({len(lines)} new)" if again else "") + f", {ms:.0f} ms",
            "context": context, "triples": kept, "matched": matched, "more": len(triples) - len(kept)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prompt", nargs="?", default="")
    ap.add_argument("--cwd", default="")
    ap.add_argument("--hops", type=int, default=None)
    ap.add_argument("--stdin", action="store_true", help='read {"prompt", "cwd"} as JSON from stdin')
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--session", default="", help="leave out what this session (and compaction round) was already given")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    prompt, cwd = args.prompt, args.cwd
    if args.stdin:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
        prompt, cwd = data.get("prompt", ""), data.get("cwd", "")
    out = recall(prompt, cwd, args.hops, session=args.session)  # hops None: the settings' depth
    out["db"] = str(store.db_path())  # so the hook's debug line says which graph was read
    out["prompt_chars"] = len(prompt)  # and how much prompt text actually arrived
    print(json.dumps(out) if args.json else (out["context"] or "(no memory matches)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
