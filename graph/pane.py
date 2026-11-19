r"""Data for the memory pane and the session brief. No model here, like recall (CLAUDE.md rule 1).

    py -I pane.py state [--cwd DIR]                  {"project", "brief", "learned", "problems"}
    py -I pane.py forget SOURCE_ID PREDICATE TARGET_ID   retire one fact (kept for history, never recalled again)

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
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("state")
    s.add_argument("--cwd", default="")
    f = sub.add_parser("forget")
    f.add_argument("source_id")
    f.add_argument("predicate")
    f.add_argument("target_id")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        out = state(args.cwd) if args.cmd == "state" else forget(args.source_id, args.predicate, args.target_id)
    except Exception as e:  # one JSON answer either way, so the mod can log it
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
