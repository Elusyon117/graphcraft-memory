r"""Moving memory between PCs: export the whole graph to one JSON file, import (merge) it on another PC.

    py -I sync.py export [FILE]     default ~/Downloads/graphcraft-memory-YYYY-MM-DD.json
    py -I sync.py import FILE       merge FILE into this PC's graph (a copy of the graph is kept first)

Import merges, it never replaces, so both PCs can keep working and swap files again later; importing the same file
twice changes nothing. Nodes are content-addressed (store.entity_id), so the same node from either PC is one node.
Sources are matched by (kind, ref), and every fact keeps its source (CLAUDE.md rule 5), so /graph-memory-undo still
takes a session back out after a move. Where both PCs know a fact, the latest event wins: a fact retired after it
was last added stays retired. A session's name: the user's own name wins over the model's; between two names of the
same kind, the local one stays. A node's description: the more recently updated one.

The file holds only what memory holds (no transcripts); it is written outside the repo (CLAUDE.md rule 2).
Prints one JSON object.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import terms  # noqa: E402

FORMAT = "graphcraft-memory-export"
VERSION = 1
KEEP_BACKUPS = 3


def default_file() -> Path:
    downloads = Path.home() / "Downloads"
    return (downloads if downloads.is_dir() else Path.home()) / f"graphcraft-memory-{time.strftime('%Y-%m-%d')}.json"


def export(file: str = "") -> dict:
    db = store.connect(create=False)
    if db is None:
        return {"error": "memory is empty on this PC: nothing to export"}
    with closing(db):
        okey = {oid: [k, r] for oid, k, r in db.execute("SELECT id, kind, ref FROM origins")}
        data = {
            "format": FORMAT, "version": VERSION, "exportedAt": time.time(),
            "origins": [dict(zip(("kind", "ref", "project", "at", "title", "titledBy"), r)) for r in db.execute(
                "SELECT kind, ref, project, at, title, titled_by FROM origins")],
            "entities": [{"id": i, "name": n, "type": t, "description": d, "scope": s, "origin": okey.get(o),
                          "created": c, "updated": u} for i, n, t, d, s, o, c, u in db.execute(
                "SELECT id, name, type, description, scope, origin_id, created, updated FROM entities")],
            "relations": [{"s": s, "p": p, "t": t, "origin": okey.get(o), "created": c, "retired": r}
                          for s, p, t, o, c, r in db.execute(
                "SELECT source_id, predicate, target_id, origin_id, created, retired FROM relations")],
            "aliases": [{"entity": e, "alias": a} for e, a in db.execute("SELECT entity_id, alias FROM aliases")],
            "captured": [{"session": s, "hash": h, "at": a} for s, h, a in db.execute(
                "SELECT session_id, hash, at FROM captured")],
        }
    path = Path(file).expanduser() if file else default_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)
    return {"exported": str(path), "nodes": len(data["entities"]), "facts": len(data["relations"]),
            "sources": len(data["origins"])}


def _backup() -> str:
    """A copy of this PC's graph before an import changes it; the newest KEEP_BACKUPS are kept."""
    src = store.db_path()
    if not src.exists():
        return ""
    dest = src.with_name(f"graph.before-import-{time.strftime('%Y%m%d-%H%M%S')}.db")
    with closing(store.connect(src)) as db:  # SQLite's own backup: consistent even mid-write
        with closing(sqlite3.connect(dest)) as out:
            db.backup(out)
    for old in sorted(src.parent.glob("graph.before-import-*.db"))[:-KEEP_BACKUPS]:
        old.unlink()
    return str(dest)


def _latest_state(local: tuple, incoming: tuple) -> float | None:
    """retired time (or None for live) from both sides' (created, retired): the latest event wins."""
    events = [(c, None) for c in (local[0], incoming[0])] + [(r, r) for r in (local[1], incoming[1]) if r]
    return max(events, key=lambda e: (e[0], e[1] is not None))[1]


def import_(file: str) -> dict:
    try:
        data = json.loads(Path(file).expanduser().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return {"error": f"could not read {file}: {e}"}
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return {"error": f"{file} is not a graphcraft-memory export"}
    if data.get("version", 0) > VERSION:
        return {"error": "that file comes from a newer graphcraft-memory: update this PC first"}
    backup = _backup()
    added = {"nodes": 0, "facts": 0, "retired": 0, "sources": 0}
    with closing(store.connect()) as db:
        oid = {}
        for o in data.get("origins", []):
            k, r = o["kind"], o["ref"]
            row = db.execute("SELECT id, at, title, titled_by FROM origins WHERE kind=? AND ref=?", (k, r)).fetchone()
            if row is None:
                cur = db.execute("INSERT INTO origins(kind, ref, project, at, title, titled_by) VALUES (?,?,?,?,?,?)",
                                 (k, r, o.get("project", ""), o["at"], o.get("title", ""), o.get("titledBy", "")))
                oid[(k, r)] = cur.lastrowid
                added["sources"] += 1
                continue
            oid[(k, r)] = row[0]
            db.execute("UPDATE origins SET at=MIN(at, ?) WHERE id=?", (o["at"], row[0]))
            mine, theirs = row[3], o.get("titledBy", "")
            if o.get("title") and (theirs == "user" and mine != "user" or not mine):
                db.execute("UPDATE origins SET title=?, titled_by=? WHERE id=?", (o["title"], theirs, row[0]))

        def origin_of(x):
            return oid.get(tuple(x["origin"])) if x.get("origin") else None

        for e in data.get("entities", []):
            if e.get("id") != store.entity_id(e["type"], e["name"]):
                continue  # not a node this format would make: skipped rather than trusted
            row = db.execute("SELECT updated FROM entities WHERE id=?", (e["id"],)).fetchone()
            if row is None:
                db.execute("INSERT INTO entities(id, name, norm, type, description, scope, origin_id, created, updated)"
                           " VALUES (?,?,?,?,?,?,?,?,?)", (e["id"], e["name"], store.normalise(e["name"]), e["type"],
                                                         e.get("description", ""), e.get("scope", "global"),
                                                         origin_of(e), e["created"], e["updated"]))
                added["nodes"] += 1
            elif e["updated"] > row[0] and e.get("description"):
                db.execute("UPDATE entities SET description=?, updated=? WHERE id=?",
                           (e["description"], e["updated"], e["id"]))
            else:
                continue
            terms.index_entity(db, e["id"], e["name"], db.execute(
                "SELECT description FROM entities WHERE id=?", (e["id"],)).fetchone()[0])

        known = {r[0] for r in db.execute("SELECT id FROM entities")}
        for r in data.get("relations", []):
            o = origin_of(r)
            if o is None or r["s"] not in known or r["t"] not in known:
                continue
            row = db.execute("SELECT created, retired FROM relations WHERE source_id=? AND predicate=? AND target_id=?"
                             " AND origin_id=?", (r["s"], r["p"], r["t"], o)).fetchone()
            if row is None:
                db.execute("INSERT INTO relations(source_id, predicate, target_id, origin_id, created, retired)"
                           " VALUES (?,?,?,?,?,?)", (r["s"], r["p"], r["t"], o, r["created"], r.get("retired")))
                added["facts"] += 1
                continue
            state = _latest_state(row, (r["created"], r.get("retired")))
            if state != row[1]:
                db.execute("UPDATE relations SET retired=? WHERE source_id=? AND predicate=? AND target_id=?"
                           " AND origin_id=?", (state, r["s"], r["p"], r["t"], o))
                added["retired"] += 1 if state else 0
        db.executemany("INSERT OR IGNORE INTO aliases(entity_id, alias, norm) VALUES (?,?,?)",
                       [(a["entity"], a["alias"], store.normalise(a["alias"])) for a in data.get("aliases", [])
                        if a.get("entity") in known and store.normalise(a.get("alias", ""))])
        db.executemany("INSERT OR IGNORE INTO captured(session_id, hash, at) VALUES (?,?,?)",
                       [(c["session"], c["hash"], c["at"]) for c in data.get("captured", [])])
        db.commit()
    return {"imported": str(file), "backup": backup, **added}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("file", nargs="?", default="")
    im = sub.add_parser("import")
    im.add_argument("file")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        out = export(args.file) if args.cmd == "export" else import_(args.file)
    except Exception as e:  # noqa: BLE001 - one JSON answer either way
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
