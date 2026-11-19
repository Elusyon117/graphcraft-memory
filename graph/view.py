r"""The graph map: export the graph into one self-contained HTML page and open it in the browser.

    py -I view.py build [--open] [--project NAME] [--out FILE]   write the page (default <data>/view/graph.html)
    py -I view.py json                                           the exported data, for tests and other tools

The page (graph/view.html) has everything inline: no CDN, no network, so the memory never leaves the machine.
Retired facts are left out. Prints one JSON object.
"""
import argparse
import json
import os
import sys
import time
import webbrowser
from contextlib import closing
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent / "view.html"
PLACEHOLDER = "/*__GRAPH_DATA__*/null"


def export(db) -> dict:
    nodes = [{"id": i, "name": n, "type": t, "description": d, "scope": s, "origin": f"{k}:{r}" if k else ""}
             for i, n, t, d, s, k, r in db.execute(
                 "SELECT e.id, e.name, e.type, e.description, e.scope, o.kind, o.ref FROM entities e"
                 " LEFT JOIN origins o ON o.id = e.origin_id")]
    edges = [{"s": s, "t": t, "p": p, "kind": k, "ref": r, "project": pr, "at": round(at)}
             for s, t, p, k, r, pr, at in db.execute(
                 "SELECT r.source_id, r.target_id, r.predicate, o.kind, o.ref, o.project, r.created"
                 " FROM relations r JOIN origins o ON o.id = r.origin_id WHERE r.retired IS NULL")]
    aliases = {}
    for eid, alias in db.execute("SELECT entity_id, alias FROM aliases"):
        aliases.setdefault(eid, []).append(alias)
    for n in nodes:
        n["aliases"] = aliases.get(n["id"], [])
    projects = sorted({e["project"] for e in edges if e["project"]})
    # For the timeline: every source in the order it first wrote, and the facts retired since (kept apart from
    # "edges", so the map never draws them).
    sources = [{"kind": k, "ref": r, "project": p, "at": round(at), "facts": n, "retired": m}
               for k, r, p, at, n, m in db.execute(
                   "SELECT o.kind, o.ref, o.project, o.at,"
                   " (SELECT COUNT(*) FROM relations r WHERE r.origin_id = o.id AND r.retired IS NULL),"
                   " (SELECT COUNT(*) FROM relations r WHERE r.origin_id = o.id AND r.retired IS NOT NULL)"
                   " FROM origins o ORDER BY o.at")]
    retired = [{"s": s, "t": t, "p": p, "kind": k, "ref": r, "project": pr, "at": round(at), "retiredAt": round(rt)}
               for s, t, p, k, r, pr, at, rt in db.execute(
                   "SELECT r.source_id, r.target_id, r.predicate, o.kind, o.ref, o.project, r.created, r.retired"
                   " FROM relations r JOIN origins o ON o.id = r.origin_id WHERE r.retired IS NOT NULL")]
    return {"generated": round(time.time()), "db": str(store.db_path()), "nodes": nodes, "edges": edges,
            "projects": projects, "sources": sources, "retired": retired}


def render(data: dict) -> str:
    # "</" inside the JSON would end the <script> element early; "<\/" is the same string to JSON.
    payload = json.dumps(data).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace(PLACEHOLDER, payload, 1)


def build(out: str = "", project: str = "", open_it: bool = False) -> dict:
    db = store.connect(create=False)
    if db is None:
        data = {"generated": round(time.time()), "db": str(store.db_path()), "nodes": [], "edges": [], "projects": [],
                "sources": [], "retired": []}
    else:
        with closing(db):
            data = export(db)
    data["startProject"] = project
    path = Path(out) if out else store.db_path().parent / "view" / "graph.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(data), encoding="utf-8")
    opened = webbrowser.open(path.resolve().as_uri()) if open_it else False
    return {"path": str(path), "url": path.resolve().as_uri(), "nodes": len(data["nodes"]),
            "edges": len(data["edges"]), "opened": bool(opened)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--open", action="store_true")
    b.add_argument("--project", default="")
    b.add_argument("--out", default="")
    sub.add_parser("json")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        if args.cmd == "build":
            out = build(args.out, args.project, args.open)
        else:
            db = store.connect(create=False)
            out = export(db) if db else {"nodes": [], "edges": [], "projects": []}
    except Exception as e:  # one JSON answer either way, so the mod can log it
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
