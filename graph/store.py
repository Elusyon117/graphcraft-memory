r"""The graph store: one SQLite file per machine, upserts only.

    py -I store.py init                                  create the database if missing
    py -I store.py load FILE.json --kind file --ref R [--project DIR] [--vocab EXTRA.json ...]
                                                         upsert a {nodes, edges, aliases} batch
    py -I store.py forget-origin ID                      remove everything one origin added
    py -I store.py rename SESSION TITLE                  name a session (an id, its first characters, or "last")
    py -I store.py stats                                 counts
    py -I store.py merge                                 join nodes saved twice under different types
(Moving the graph to another PC: sync.py.)

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import terms  # noqa: E402

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
  created REAL NOT NULL, retired REAL,  -- created: when the fact was last said
  UNIQUE(source_id, predicate, target_id, origin_id));
CREATE TABLE IF NOT EXISTS aliases (
  entity_id TEXT NOT NULL, alias TEXT NOT NULL, norm TEXT NOT NULL, UNIQUE(entity_id, norm));
CREATE TABLE IF NOT EXISTS captured (
  session_id TEXT NOT NULL, hash TEXT NOT NULL, at REAL NOT NULL, UNIQUE(session_id, hash));
CREATE TABLE IF NOT EXISTS terms (
  term TEXT NOT NULL, entity_id TEXT NOT NULL, field INTEGER NOT NULL, UNIQUE(term, entity_id, field));
CREATE INDEX IF NOT EXISTS entities_norm ON entities(norm);
CREATE INDEX IF NOT EXISTS terms_term ON terms(term);
CREATE INDEX IF NOT EXISTS terms_entity ON terms(entity_id);
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


def signature(predicate: str, text: str, types) -> tuple:
    """(source types, target types) read from a predicate's description, e.g. "PROJECT or PERSON uses TOOL".

    The vocabulary types named before the predicate word are the allowed sources, those after it the allowed targets;
    a side that names none ("something", "another thing") takes any type. So a description doubles as the rule.
    """
    words = re.findall(r"[A-Za-z_]+", text)
    if predicate not in words:
        return frozenset(), frozenset()
    i = words.index(predicate)
    return (frozenset(w for w in words[:i] if w in types), frozenset(w for w in words[i + 1:] if w in types))


def connect(path: Path | None = None, create: bool = True) -> sqlite3.Connection | None:
    path = path or db_path()
    if not path.exists():
        if not create:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    _migrate(db)
    return db


def _migrate(db: sqlite3.Connection) -> None:
    """Columns added after a graph was first made; each one additive, with a default, so old graphs just gain it."""
    have = {r[1] for r in db.execute("PRAGMA table_info(origins)")}
    if "title" not in have:  # what the source was about: the extraction model's title, or the user's own name
        db.execute("ALTER TABLE origins ADD COLUMN title TEXT NOT NULL DEFAULT ''")
    if "titled_by" not in have:  # '' (none yet), 'model' or 'user'; a user's name is never replaced
        db.execute("ALTER TABLE origins ADD COLUMN titled_by TEXT NOT NULL DEFAULT ''")
    # the word index for vague prompts (terms.py), built once for a graph made before it existed
    if not db.execute("SELECT 1 FROM terms LIMIT 1").fetchone() and db.execute("SELECT 1 FROM entities LIMIT 1").fetchone():
        terms.rebuild(db)
    db.commit()


TITLE_MAX = 80


def set_title(db: sqlite3.Connection, origin_id: int, title: str, by: str) -> bool:
    """Name a source. The model names it once (the first capture of a session sees how it began); the user can rename
    it any time, and a later capture never replaces the user's name. Returns whether the name changed."""
    title = " ".join((title or "").split())[:TITLE_MAX]
    if not title:
        return False
    cur = db.execute("UPDATE origins SET title=?, titled_by=? WHERE id=? AND (? = 'user' OR titled_by = '')",
                     (title, by, origin_id, by))
    db.commit()
    return cur.rowcount > 0


def title_of(db: sqlite3.Connection, origin_id: int) -> tuple:
    """(title, is_auto). With no title stored, a name made from the decisions and problems the source introduced
    (else any node it introduced), so older sessions read as something too."""
    row = db.execute("SELECT title FROM origins WHERE id=?", (origin_id,)).fetchone()
    if row and row[0]:
        return row[0], False
    names = [r[0] for r in db.execute(
        "SELECT e.name FROM entities e WHERE e.origin_id=? AND EXISTS (SELECT 1 FROM relations r WHERE r.retired IS NULL"
        " AND (r.source_id=e.id OR r.target_id=e.id)) ORDER BY CASE e.type WHEN 'DECISION' THEN 0 WHEN 'PROBLEM' THEN 1"
        " WHEN 'PROJECT' THEN 9 WHEN 'PERSON' THEN 9 ELSE 2 END, e.created LIMIT 2", (origin_id,))]
    return (" · ".join(names), True) if names else ("", True)


def find_session(db: sqlite3.Connection, which: str) -> tuple | None:
    """(origin id, session id) for "last", a whole session id, or its first characters (unambiguous)."""
    if which in ("", "last"):
        return db.execute("SELECT id, ref FROM origins WHERE kind='session' ORDER BY at DESC, id DESC LIMIT 1").fetchone()
    rows = db.execute("SELECT id, ref FROM origins WHERE kind='session' AND ref LIKE ?",
                      (which.replace("%", "").replace("_", "") + "%",)).fetchall()  # ids have no % or _
    exact = [r for r in rows if r[1] == which]
    return exact[0] if exact else rows[0] if len(rows) == 1 else None


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
    report = {"nodes": 0, "edges": 0, "aliases": 0, "dropped": [], "stored": []}  # stored: (s, p, t) ids
    local = {}  # normalised name -> id, for this batch

    held = {}  # normalised name -> the node as given, while an existing node of another type stands in for it

    def put(n: dict) -> str:
        name, type_ = n["name"].strip(), n["type"]
        eid = entity_id(type_, name)
        desc = (n.get("description") or "").strip()
        db.execute(
            "INSERT INTO entities(id, name, norm, type, description, scope, origin_id, created, updated)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,"
            "  description=CASE WHEN excluded.description != '' THEN excluded.description ELSE description END",
            (eid, name, normalise(name), type_, desc, n.get("scope") or default_scope, origin_id, now, now))
        stored_name, stored_desc = db.execute("SELECT name, description FROM entities WHERE id=?", (eid,)).fetchone()
        terms.index_entity(db, eid, stored_name, stored_desc)
        return eid

    for n in batch.get("nodes", []):
        name, type_ = (n.get("name") or "").strip(), n.get("type")
        if not name or type_ not in vocab["types"]:
            report["dropped"].append(f"node {name!r}: type {type_!r} not in vocabulary")
            continue
        # The same name already in memory as another type is the same thing, named again with a different guess at
        # its type: reuse it, so its facts are not split over two nodes. Only if a fact here needs this type is a
        # node of this type made after all (see `fits` below).
        other = [r[0] for r in db.execute("SELECT id FROM entities WHERE norm=? AND type!=?", (normalise(name), type_))]
        if len(other) == 1 and not db.execute("SELECT 1 FROM entities WHERE id=?", (entity_id(type_, name),)).fetchone():
            local[normalise(name)] = other[0]
            held[normalise(name)] = n
        else:
            local[normalise(name)] = put(n)
        report["nodes"] += 1

    def resolve(name: str) -> str | None:
        return local.get(normalise(name or "")) or _find_by_name(db, name or "")

    def type_of(eid: str) -> str:
        row = db.execute("SELECT type FROM entities WHERE id=?", (eid,)).fetchone()
        return row[0] if row else ""

    for e in batch.get("edges", []):
        p = e.get("predicate")
        if p not in vocab["predicates"]:
            report["dropped"].append(f"edge {e.get('source')!r} {p!r} {e.get('target')!r}: predicate not in vocabulary")
            continue
        s, t = resolve(e.get("source")), resolve(e.get("target"))
        if not s or not t:
            report["dropped"].append(f"edge {e.get('source')!r} {p!r} {e.get('target')!r}: endpoint not found")
            continue
        if s == t:
            report["dropped"].append(f"edge {e.get('source')!r} {p!r} itself: a node linked to itself says nothing")
            continue
        sources, targets = signature(p, vocab["predicates"][p], vocab["types"])

        def fits(a, b):
            return not ((sources and type_of(a) not in sources) or (targets and type_of(b) not in targets))

        def fits_with(side, type_):  # would the fact fit with this side's node of type_ instead?
            st_, tt_ = (type_, type_of(t)) if side == "source" else (type_of(s), type_)
            return not ((sources and st_ not in sources) or (targets and tt_ not in targets))

        for side in ("source", "target"):  # a stand-in node whose type this fact rules out: make the node as given
            k = normalise(e.get(side) or "")
            if not fits(s, t) and k in held and fits_with(side, held[k]["type"]):
                made = put(held.pop(k))
                local[k] = made
                s, t = (made, t) if side == "source" else (s, made)
        st, tt = type_of(s), type_of(t)
        if not fits(s, t):
            report["dropped"].append(f"edge {e.get('source')!r} ({st}) {p!r} {e.get('target')!r} ({tt}):"
                                     f" {p} links {vocab['predicates'][p]!r}")
            continue
        db.execute("INSERT OR IGNORE INTO relations(source_id, predicate, target_id, origin_id, created)"
                   " VALUES (?,?,?,?,?)", (s, p, t, origin_id, now))
        # a fact said again is live again, and `created` becomes when it was last said: an import from another PC
        # can then tell it was said after it was retired there (sync.py: the latest event wins)
        db.execute("UPDATE relations SET retired=NULL, created=? WHERE source_id=? AND predicate=? AND target_id=?"
                   " AND origin_id=?", (now, s, p, t, origin_id))
        report["edges"] += 1
        report["stored"].append((s, p, t))

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


def merge_duplicates(db: sqlite3.Connection, vocab: dict) -> dict:
    """Join nodes that share a name but not a type (one thing saved twice under different guesses at its type), so
    recall finds all its facts. The type kept is the one under which the most of their facts still fit the
    vocabulary; a pair where any fact would not fit is left alone and listed. Facts keep their sources (rule 5)."""
    merged, left = [], []
    for (norm,) in db.execute("SELECT norm FROM entities GROUP BY norm HAVING COUNT(DISTINCT type) > 1").fetchall():
        group = db.execute("SELECT id, name, type, description FROM entities WHERE norm=? ORDER BY created", (norm,)).fetchall()
        ids = [g[0] for g in group]
        marks = ",".join("?" * len(ids))
        rels = db.execute(f"SELECT source_id, predicate, target_id FROM relations"  # noqa: S608
                          f" WHERE retired IS NULL AND (source_id IN ({marks}) OR target_id IN ({marks}))",
                          ids + ids).fetchall()  # live facts decide; retired ones are history and just move along

        def fits_as(keep_type):
            ok = 0
            for s, p, t in rels:
                if p not in vocab["predicates"]:
                    return -1
                srcs, tgts = signature(p, vocab["predicates"][p], vocab["types"])
                st = keep_type if s in ids else db.execute("SELECT type FROM entities WHERE id=?", (s,)).fetchone()[0]
                tt = keep_type if t in ids else db.execute("SELECT type FROM entities WHERE id=?", (t,)).fetchone()[0]
                if (srcs and st not in srcs) or (tgts and tt not in tgts) or (s in ids and t in ids):
                    return -1
                ok += 1
            return ok

        best = max(group, key=lambda g: (fits_as(g[2]), -group.index(g)))
        if fits_as(best[2]) < 0:
            left.append(best[1])
            continue
        keep = best[0]
        for drop, _name, _type, desc in group:
            if drop == keep:
                continue
            for s, p, t, o, c, r in db.execute("SELECT source_id, predicate, target_id, origin_id, created, retired"
                                                " FROM relations WHERE source_id=? OR target_id=?", (drop, drop)).fetchall():
                ns, nt = (keep if s == drop else s), (keep if t == drop else t)
                db.execute("DELETE FROM relations WHERE source_id=? AND predicate=? AND target_id=? AND origin_id=?",
                           (s, p, t, o))
                db.execute("INSERT OR IGNORE INTO relations(source_id, predicate, target_id, origin_id, created, retired)"
                           " VALUES (?,?,?,?,?,?)", (ns, p, nt, o, c, r))
            db.execute("UPDATE OR IGNORE aliases SET entity_id=? WHERE entity_id=?", (keep, drop))
            db.execute("UPDATE entities SET description=? WHERE id=? AND description=''", (desc, keep))
            db.execute("DELETE FROM aliases WHERE entity_id=?", (drop,))
            db.execute("DELETE FROM terms WHERE entity_id=?", (drop,))
            db.execute("DELETE FROM entities WHERE id=?", (drop,))
        row = db.execute("SELECT name, description FROM entities WHERE id=?", (keep,)).fetchone()
        terms.index_entity(db, keep, row[0], row[1])
        merged.append(f"{best[1]} ({best[2]})")
    db.commit()
    return {"merged": merged, "left": left}


def retire(db: sqlite3.Connection, source: str, predicate: str, target: str) -> int:
    """Mark a fact no longer true (kept for history, skipped by recall). Returns rows retired."""
    s, t = _find_by_name(db, source), _find_by_name(db, target)
    if not s or not t:
        return 0
    cur = db.execute("UPDATE relations SET retired=? WHERE source_id=? AND predicate=? AND target_id=?"
                     " AND retired IS NULL", (time.time(), s, predicate, t))
    db.commit()
    return cur.rowcount


def retire_ids(db: sqlite3.Connection, source_id: str, predicate: str, target_id: str) -> int:
    """retire() by node ids, for the pane: a name shared by two types cannot pick the wrong node."""
    cur = db.execute("UPDATE relations SET retired=? WHERE source_id=? AND predicate=? AND target_id=?"
                     " AND retired IS NULL", (time.time(), source_id, predicate, target_id))
    db.commit()
    return cur.rowcount


def forget_origin(db: sqlite3.Connection, origin_id: int) -> dict:
    """Remove what one origin added: its relations, then nodes it created that nothing links any more."""
    rel = db.execute("DELETE FROM relations WHERE origin_id=?", (origin_id,)).rowcount
    ent = db.execute(
        "DELETE FROM entities WHERE origin_id=? AND id NOT IN"
        " (SELECT source_id FROM relations UNION SELECT target_id FROM relations)", (origin_id,)).rowcount
    db.execute("DELETE FROM aliases WHERE entity_id NOT IN (SELECT id FROM entities)")
    db.execute("DELETE FROM terms WHERE entity_id NOT IN (SELECT id FROM entities)")
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
    sub.add_parser("merge")
    rn = sub.add_parser("rename")
    rn.add_argument("session")
    rn.add_argument("title", nargs="+")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    db = connect()
    if args.cmd == "load":
        batch = json.loads(Path(args.file).read_text(encoding="utf-8"))
        oid = add_origin(db, args.kind, args.ref, args.project)
        out = upsert(db, batch, oid, load_vocabulary(args.vocab))
        out["origin"] = oid
    elif args.cmd == "forget-origin":
        out = forget_origin(db, args.id)
    elif args.cmd == "merge":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import capture  # the vocabulary in force (core plus feeders'), read where capture reads it
        out = merge_duplicates(db, capture.active_vocabulary())
    elif args.cmd == "rename":
        found = find_session(db, args.session)
        if not found:
            out = {"renamed": None, "reason": f"no single captured session matches {args.session!r}"}
        else:
            set_title(db, found[0], " ".join(args.title), "user")
            out = {"renamed": found[1], "title": title_of(db, found[0])[0]}
    else:
        out = stats(db)
        out["path"] = str(db_path())
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
