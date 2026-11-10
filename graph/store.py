r"""The graph store: one SQLite file per machine, upserts only.

    py -I store.py init                                  create the database if missing
    py -I store.py load FILE.json --kind file --ref R [--project DIR] [--vocab EXTRA.json ...]
                                                         upsert a {nodes, edges, aliases} batch
    py -I store.py forget-origin ID                      remove everything one origin added
    py -I store.py stats                                 counts

The database is ~/.claude/graphcraft-memory/graph.db (CLAUDE_CONFIG_DIR if set), or GRAPHCRAFT_DB when set (tests).

A batch is the shape extraction and feeders produce:
    {"scope": "global" | "<project dir>",                (optional, default global)
     "nodes":   [{"name", "type", "description"?, "scope"?}],
     "edges":   [{"source", "predicate", "target"}],     (names, resolved in this batch first, then the graph)
     "aliases": [{"entity", "alias"}]}
Types and predicates outside the vocabulary are dropped and reported, never stored (CLAUDE.md rule 3).
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE_VOCAB = ROOT / "vocabulary" / "core.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS origins (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL, project TEXT NOT NULL DEFAULT '',
  at REAL NOT NULL, UNIQUE(kind, ref));
CREATE TABLE IF NOT EXISTS entities (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, norm TEXT NOT NULL, type TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'global',
  origin_id INTEGER, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS relations (
  source_id TEXT NOT NULL, predicate TEXT NOT NULL, target_id TEXT NOT NULL, origin_id INTEGER NOT NULL,
  created REAL NOT NULL, retired REAL,
  UNIQUE(source_id, predicate, target_id, origin_id));
CREATE TABLE IF NOT EXISTS aliases (
  entity_id TEXT NOT NULL, alias TEXT NOT NULL, norm TEXT NOT NULL, UNIQUE(entity_id, norm));
CREATE INDEX IF NOT EXISTS entities_norm ON entities(norm);
CREATE INDEX IF NOT EXISTS aliases_norm ON aliases(norm);
CREATE INDEX IF NOT EXISTS relations_source ON relations(source_id);
CREATE INDEX IF NOT EXISTS relations_target ON relations(target_id);
CREATE INDEX IF NOT EXISTS relations_origin ON relations(origin_id);
"""


def db_path() -> Path:
    if os.environ.get("GRAPHCRAFT_DB"):
        return Path(os.environ["GRAPHCRAFT_DB"])
    # Not %LOCALAPPDATA%: Windows redirects a packaged app's AppData writes (the Claude desktop app is one), so the
    # desktop app and a terminal Claude Code would each see a different graph. ~/.claude is shared by every install.
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return Path(base) / "graphcraft-memory" / "graph.db"


def normalise(name: str) -> str:
    """'AnimBP_Player', 'animbp player' and 'AnimBP-Player' all become 'animbp_player'."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def entity_id(type_: str, name: str) -> str:
    """Content-addressed: the same type and name give the same node, from any session or mod."""
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"{type_}:{normalise(name)}"))


def load_vocabulary(extra: list | None = None) -> dict:
    """Core plus each extra file. An extra may add types and predicates, never redefine core ones."""
    core = json.loads(CORE_VOCAB.read_text(encoding="utf-8"))
    types, predicates = dict(core["types"]), dict(core["predicates"])
    for path in extra or []:
        more = json.loads(Path(path).read_text(encoding="utf-8"))
        for k, v in more.get("types", {}).items():
            types.setdefault(k, v)
        for k, v in more.get("predicates", {}).items():
            predicates.setdefault(k, v)
    return {"types": types, "predicates": predicates}


def connect(path: Path | None = None, create: bool = True) -> sqlite3.Connection | None:
    path = path or db_path()
    if not path.exists():
        if not create:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def add_origin(db: sqlite3.Connection, kind: str, ref: str, project: str = "") -> int:
    """One row per (kind, ref): loading the same session or feeder run again reuses it."""
    db.execute("INSERT OR IGNORE INTO origins(kind, ref, project, at) VALUES (?,?,?,?)",
               (kind, ref, project, time.time()))
    return db.execute("SELECT id FROM origins WHERE kind=? AND ref=?", (kind, ref)).fetchone()[0]


def _find_by_name(db: sqlite3.Connection, name: str) -> str | None:
    """An existing node by name or alias; None when absent or ambiguous (two types share the name)."""
    norm = normalise(name)
    ids = {r[0] for r in db.execute("SELECT id FROM entities WHERE norm=?", (norm,))}
    ids |= {r[0] for r in db.execute("SELECT entity_id FROM aliases WHERE norm=?", (norm,))}
    return ids.pop() if len(ids) == 1 else None


def upsert(db: sqlite3.Connection, batch: dict, origin_id: int, vocab: dict) -> dict:
    """Add a batch. Returns {"nodes", "edges", "aliases": counts added or refreshed, "dropped": [reasons]}."""
    now = time.time()
    default_scope = batch.get("scope") or "global"
    report = {"nodes": 0, "edges": 0, "aliases": 0, "dropped": []}
    local = {}  # normalised name -> id, for this batch

    for n in batch.get("nodes", []):
        name, type_ = (n.get("name") or "").strip(), n.get("type")
        if not name or type_ not in vocab["types"]:
            report["dropped"].append(f"node {name!r}: type {type_!r} not in vocabulary")
            continue
        eid = entity_id(type_, name)
        desc = (n.get("description") or "").strip()
        db.execute(
            "INSERT INTO entities(id, name, norm, type, description, scope, origin_id, created, updated)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,"
            "  description=CASE WHEN excluded.description != '' THEN excluded.description ELSE description END",
            (eid, name, normalise(name), type_, desc, n.get("scope") or default_scope, origin_id, now, now))
        local[normalise(name)] = eid
        report["nodes"] += 1

    def resolve(name: str) -> str | None:
        return local.get(normalise(name or "")) or _find_by_name(db, name or "")

    for e in batch.get("edges", []):
        p = e.get("predicate")
        if p not in vocab["predicates"]:
            report["dropped"].append(f"edge {e.get('source')!r} {p!r} {e.get('target')!r}: predicate not in vocabulary")
            continue
        s, t = resolve(e.get("source")), resolve(e.get("target"))
        if not s or not t:
            report["dropped"].append(f"edge {e.get('source')!r} {p!r} {e.get('target')!r}: endpoint not found")
            continue
        db.execute("INSERT OR IGNORE INTO relations(source_id, predicate, target_id, origin_id, created)"
                   " VALUES (?,?,?,?,?)", (s, p, t, origin_id, now))
        db.execute("UPDATE relations SET retired=NULL WHERE source_id=? AND predicate=? AND target_id=?"
                   " AND origin_id=?", (s, p, t, origin_id))
        report["edges"] += 1

    for a in batch.get("aliases", []):
        eid = resolve(a.get("entity"))
        alias = (a.get("alias") or "").strip()
        if not eid or not normalise(alias):
            report["dropped"].append(f"alias {alias!r} for {a.get('entity')!r}: entity not found")
            continue
        db.execute("INSERT OR IGNORE INTO aliases(entity_id, alias, norm) VALUES (?,?,?)",
                   (eid, alias, normalise(alias)))
        report["aliases"] += 1

    db.commit()
    return report


def retire(db: sqlite3.Connection, source: str, predicate: str, target: str) -> int:
    """Mark a fact no longer true (kept for history, skipped by recall). Returns rows retired."""
    s, t = _find_by_name(db, source), _find_by_name(db, target)
    if not s or not t:
        return 0
    cur = db.execute("UPDATE relations SET retired=? WHERE source_id=? AND predicate=? AND target_id=?"
                     " AND retired IS NULL", (time.time(), s, predicate, t))
    db.commit()
    return cur.rowcount


def forget_origin(db: sqlite3.Connection, origin_id: int) -> dict:
    """Remove what one origin added: its relations, then nodes it created that nothing links any more."""
    rel = db.execute("DELETE FROM relations WHERE origin_id=?", (origin_id,)).rowcount
    ent = db.execute(
        "DELETE FROM entities WHERE origin_id=? AND id NOT IN"
        " (SELECT source_id FROM relations UNION SELECT target_id FROM relations)", (origin_id,)).rowcount
    db.execute("DELETE FROM aliases WHERE entity_id NOT IN (SELECT id FROM entities)")
    db.execute("DELETE FROM origins WHERE id=?", (origin_id,))
    db.commit()
    return {"relations": rel, "entities": ent}


def stats(db: sqlite3.Connection) -> dict:
    return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608 (fixed names)
            for t in ("entities", "relations", "aliases", "origins")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("stats")
    ld = sub.add_parser("load")
    ld.add_argument("file")
    ld.add_argument("--kind", default="file", choices=["session", "file", "feeder"])
    ld.add_argument("--ref", required=True)
    ld.add_argument("--project", default="")
    ld.add_argument("--vocab", action="append", default=[])
    fo = sub.add_parser("forget-origin")
    fo.add_argument("id", type=int)
    args = ap.parse_args(argv)

    db = connect()
    if args.cmd == "load":
        batch = json.loads(Path(args.file).read_text(encoding="utf-8"))
        oid = add_origin(db, args.kind, args.ref, args.project)
        out = upsert(db, batch, oid, load_vocabulary(args.vocab))
        out["origin"] = oid
    elif args.cmd == "forget-origin":
        out = forget_origin(db, args.id)
    else:
        out = stats(db)
        out["path"] = str(db_path())
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
