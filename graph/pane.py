r"""Data for the memory pane and the session brief. No model here, like recall (CLAUDE.md rule 1).

    py -I pane.py state [--cwd DIR]                  {"project", "brief", "learned", "problems"}
    py -I pane.py forget SOURCE_ID PREDICATE TARGET_ID   retire one fact (kept for history, never recalled again)
    py -I pane.py links SOURCE_ID TARGET_ID              the links the vocabulary allows between two nodes, both ways
    py -I pane.py relink SOURCE_ID PREDICATE TARGET_ID NEW --session ID [--project DIR]
                                                         correct a fact's link: NEW is a predicate, or ~predicate for
                                                         the other direction; the old fact is retired, the new one
                                                         saved under this session (so undo takes it back out)

- learned: the facts the newest captured session added that are still live, with its session id (for undo).
- problems: PROBLEM nodes within 2 links of this folder's PROJECT node; fixed when one has a live fixed_by link.
- brief: one line, "Last time in Campfires: <newest decision> · 1 open problem · 2 decisions"; empty when this
  folder has no PROJECT node or nothing linked to it.
Prints one JSON object; with no database yet it answers empty without creating one.
"""
import argparse
import json
import os
import sys
import time
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402
import recall  # noqa: E402
import store  # noqa: E402

MAX_LEARNED = 30
MAX_PROBLEMS = 8


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def learned(db) -> dict | None:
    row = db.execute(
        "SELECT o.id, o.ref, o.at FROM origins o WHERE o.kind='session' AND EXISTS"
        " (SELECT 1 FROM relations r WHERE r.origin_id=o.id AND r.retired IS NULL)"
        " ORDER BY o.at DESC, o.id DESC LIMIT 1").fetchone()
    if not row:
        return None
    facts = [{"source": s, "predicate": p, "target": t, "source_id": si, "target_id": ti}
             for s, p, t, si, ti in db.execute(
                 "SELECT es.name, r.predicate, et.name, r.source_id, r.target_id FROM relations r"
                 " JOIN entities es ON es.id=r.source_id JOIN entities et ON et.id=r.target_id"
                 " WHERE r.origin_id=? AND r.retired IS NULL ORDER BY r.created, es.name", (row[0],))]
    title, auto = store.title_of(db, row[0])
    return {"sessionId": row[1], "at": round(row[2]), "total": len(facts), "facts": facts[:MAX_LEARNED],
            "title": title, "titleIsAuto": auto}


def project_view(db, cwd: str) -> dict:
    seeds = recall.project_seed(db, cwd)
    if not seeds:
        return {"project": "", "brief": "", "problems": []}
    name = db.execute("SELECT name FROM entities WHERE id=?", (next(iter(sorted(seeds))),)).fetchone()[0]
    ids = list(recall.walk(db, seeds, 2))
    marks = ",".join("?" * len(ids))
    near = db.execute(f"SELECT id, name, type FROM entities WHERE id IN ({marks})"  # noqa: S608
                      f" AND type IN ('PROBLEM', 'DECISION')", ids).fetchall()

    def newest(eid: str) -> float:
        r = db.execute("SELECT MAX(created) FROM relations WHERE retired IS NULL AND (source_id=? OR target_id=?)",
                       (eid, eid)).fetchone()
        return r[0] or 0

    problems = []
    for eid, pname, _ in (n for n in near if n[2] == "PROBLEM"):
        fix = db.execute("SELECT e.name FROM relations r JOIN entities e ON e.id=r.target_id WHERE r.source_id=?"
                         " AND r.predicate='fixed_by' AND r.retired IS NULL", (eid,)).fetchone()
        problems.append({"name": pname, "fixed": fix is not None, "fixedBy": fix[0] if fix else "",
                         "at": newest(eid)})
    problems.sort(key=lambda p: (p["fixed"], -p["at"]))
    decisions = sorted((n for n in near if n[2] == "DECISION"), key=lambda n: -newest(n[0]))
    open_count = sum(1 for p in problems if not p["fixed"])

    parts = []
    if decisions:
        parts.append(f"latest decision: {decisions[0][1]}")
    if open_count:
        parts.append(_plural(open_count, "open problem"))
    if len(decisions) > 1:
        parts.append(_plural(len(decisions), "decision"))
    brief = f"Last time in {name}: " + " · ".join(parts) if parts else ""
    for p in problems:
        del p["at"]
    return {"project": name, "brief": brief, "problems": problems[:MAX_PROBLEMS]}


def state(cwd: str = "") -> dict:
    db = store.connect(create=False)
    if db is None:
        return {"project": "", "brief": "", "learned": None, "problems": []}
    with closing(db):
        return {**project_view(db, cwd), "learned": learned(db)}


def forget(source_id: str, predicate: str, target_id: str) -> dict:
    db = store.connect(create=False)
    if db is None:
        return {"retired": 0}
    with closing(db):
        return {"retired": store.retire_ids(db, source_id, predicate, target_id)}


def _node(db, eid: str) -> tuple:
    row = db.execute("SELECT name, type FROM entities WHERE id=?", (eid,)).fetchone()
    if not row:
        raise ValueError("that node is no longer in memory")
    return row


def links(source_id: str, target_id: str) -> dict:
    """[{value, label}]: each predicate the vocabulary allows source -> target, then those allowed the other way
    (value "~p"), for the pane's Fix picker."""
    db = store.connect(create=False)
    if db is None:
        return {"links": []}
    with closing(db):
        (sn, st), (tn, tt) = _node(db, source_id), _node(db, target_id)
        vocab = capture.active_vocabulary()
        out = []
        for flip, (a, at, b, bt) in ((False, (sn, st, tn, tt)), (True, (tn, tt, sn, st))):
            for p, rule in vocab["predicates"].items():
                srcs, tgts = store.signature(p, rule, vocab["types"])
                if (not srcs or at in srcs) and (not tgts or bt in tgts):
                    out.append({"value": ("~" if flip else "") + p, "label": f"{a} {p.replace('_', ' ')} {b}"})
        return {"links": out}


def relink(source_id: str, predicate: str, target_id: str, new: str, session: str, project: str = "") -> dict:
    allowed = {x["value"] for x in links(source_id, target_id)["links"]}
    if new not in allowed:
        return {"error": f"{new!r} is not a link the vocabulary allows between these two"}
    db = store.connect(create=False)
    with closing(db):
        retired = store.retire_ids(db, source_id, predicate, target_id)
        s, t = (target_id, source_id) if new.startswith("~") else (source_id, target_id)
        p = new.lstrip("~")
        oid = store.add_origin(db, "session", session, project)
        db.execute("INSERT OR IGNORE INTO relations(source_id, predicate, target_id, origin_id, created) VALUES (?,?,?,?,?)",
                   (s, p, t, oid, time.time()))
        db.execute("UPDATE relations SET retired=NULL, created=? WHERE source_id=? AND predicate=? AND target_id=?"
                   " AND origin_id=? AND retired IS NOT NULL", (time.time(), s, p, t, oid))
        db.commit()
        (sn, _), (tn, _) = _node(db, s), _node(db, t)
        capture._log({"event": "corrected", "sessionId": session, "project": project, "facts": 1, "retired": retired})
        return {"retired": retired, "fact": {"source": sn, "predicate": p, "target": tn, "source_id": s, "target_id": t}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("state")
    s.add_argument("--cwd", default="")
    f = sub.add_parser("forget")
    f.add_argument("source_id")
    f.add_argument("predicate")
    f.add_argument("target_id")
    lk = sub.add_parser("links")
    lk.add_argument("source_id")
    lk.add_argument("target_id")
    rl = sub.add_parser("relink")
    for a in ("source_id", "predicate", "target_id", "new"):
        rl.add_argument(a)
    rl.add_argument("--session", required=True)
    rl.add_argument("--project", default="")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        if args.cmd == "state":
            out = state(args.cwd)
        elif args.cmd == "forget":
            out = forget(args.source_id, args.predicate, args.target_id)
        elif args.cmd == "links":
            out = links(args.source_id, args.target_id)
        else:
            out = relink(args.source_id, args.predicate, args.target_id, args.new, args.session, args.project)
    except Exception as e:  # one JSON answer either way, so the mod can log it
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
