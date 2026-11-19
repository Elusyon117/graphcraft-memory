r"""Feeders: other mods' facts, read from files they leave for graphcraft-memory (docs/DESIGN.md § Feeders).

    py -I feeders.py run      read every installed feeder's changed files into the graph
    py -I feeders.py list     the feeders found and what each last loaded

A mod is a feeder when its plugin folder has graphcraft/feeder.json:
    {"name": "zonecraft", "vocabulary": "graphcraft/vocabulary.json", "adapter": "graphcraft/adapter.py",
     "sources": [{"id": "conflicts", "file": "conflicts.json", "title": "Mod conflicts"}, ...]}

The mod writes each source file into <data>/feeds/<name>/ (only when that folder's parent, graphcraft-memory's data
folder, exists), whenever it has something new. Here, a file that changed since the last run goes through the mod's
adapter, run as its own process and never imported (CLAUDE.md rule 4): `py -I adapter.py <id> <file>` prints one
{nodes, edges, aliases} batch. It is checked against core plus the mod's vocabulary and loaded under the origin
feeder "<name>:<id>". A source is a snapshot: facts it gave before and no longer gives are retired, not deleted.

Installed plugins come from <config>/plugins/installed_plugins.json, or GRAPHCRAFT_PLUGINS (folders joined by
os.pathsep) in tests.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

ADAPTER_TIMEOUT_S = 60
MAX_BATCH_BYTES = 4 * 1024 * 1024
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def data_dir() -> Path:
    return store.db_path().parent


def feeds_dir() -> Path:
    return data_dir() / "feeds"


def plugin_dirs() -> list:
    """Every installed plugin's folder, newest install of each first."""
    if os.environ.get("GRAPHCRAFT_PLUGINS") is not None:
        return [Path(p) for p in os.environ["GRAPHCRAFT_PLUGINS"].split(os.pathsep) if p]
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    try:
        reg = json.loads((Path(base) / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for installs in (reg.get("plugins") or {}).values():
        for i in sorted((x for x in installs if isinstance(x, dict)), key=lambda x: x.get("lastUpdated", ""), reverse=True):
            if i.get("installPath"):
                out.append(Path(i["installPath"]))
                break
    return out


def _inside(root: Path, rel: str) -> Path | None:
    """root/rel when it stays inside root; None for an absolute path or one that climbs out."""
    if not isinstance(rel, str) or not rel or os.path.isabs(rel):
        return None
    p = (root / rel).resolve()
    return p if p == root.resolve() or root.resolve() in p.parents else None


def manifest(plugin: Path) -> tuple:
    """(feeder dict, None) or (None, why it is not usable). No graphcraft/feeder.json: (None, None)."""
    f = plugin / "graphcraft" / "feeder.json"
    if not f.is_file():
        return None, None
    try:
        m = json.loads(f.read_text(encoding="utf-8-sig"))
    except ValueError as e:
        return None, f"{f}: not JSON ({e})"
    name = m.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        return None, f"{f}: name must be lowercase letters, digits, - or _"
    adapter = _inside(plugin, m.get("adapter"))
    if not adapter or not adapter.is_file():
        return None, f"{name}: adapter {m.get('adapter')!r} not found in the plugin folder"
    vocab = _inside(plugin, m.get("vocabulary")) if m.get("vocabulary") else None
    if m.get("vocabulary") and (not vocab or not vocab.is_file()):
        return None, f"{name}: vocabulary {m.get('vocabulary')!r} not found in the plugin folder"
    sources = []
    for s in m.get("sources") or []:
        sid, file = (s or {}).get("id"), (s or {}).get("file")
        if isinstance(sid, str) and NAME.match(sid) and isinstance(file, str) and NAME.match(file.replace(".", "_")):
            sources.append({"id": sid, "file": file, "title": str(s.get("title") or f"{name} {sid}")[:store.TITLE_MAX]})
    if not sources:
        return None, f"{name}: no usable sources"
    return {"name": name, "plugin": str(plugin), "adapter": adapter, "vocabulary": vocab, "sources": sources}, None


def discover() -> tuple:
    """(feeders, problems). The first plugin to claim a name keeps it."""
    feeders, problems, seen = [], [], set()
    for d in plugin_dirs():
        m, why = manifest(d)
        if why:
            problems.append(why)
        if m and m["name"] not in seen:
            seen.add(m["name"])
            feeders.append(m)
    return feeders, problems


def sync_vocabularies(feeders: list) -> None:
    """<data>/vocab/feeder-<name>.json for each installed feeder, so capture and memory_remember can use its types
    too; a feeder no longer installed loses its file (its facts stay, until undone)."""
    vdir = data_dir() / "vocab"
    vdir.mkdir(parents=True, exist_ok=True)
    keep = set()
    for f in feeders:
        if f["vocabulary"]:
            dest = vdir / f"feeder-{f['name']}.json"
            text = Path(f["vocabulary"]).read_text(encoding="utf-8-sig")
            json.loads(text)  # a broken file is reported by the caller, not copied
            if not dest.exists() or dest.read_text(encoding="utf-8") != text:
                dest.write_text(text, encoding="utf-8")
            keep.add(dest.name)
    for old in vdir.glob("feeder-*.json"):
        if old.name not in keep:
            old.unlink()


def _state() -> dict:
    try:
        return json.loads((feeds_dir() / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    feeds_dir().mkdir(parents=True, exist_ok=True)
    (feeds_dir() / "state.json").write_text(json.dumps(st, indent=1), encoding="utf-8")


def adapt(feeder: dict, source: dict, file: Path) -> dict:
    """The adapter's batch for one file; raises ValueError with the reason when there is none."""
    r = subprocess.run([sys.executable, "-I", str(feeder["adapter"]), source["id"], str(file)],
                       capture_output=True, timeout=ADAPTER_TIMEOUT_S, cwd=feeder["plugin"])
    if r.returncode != 0:
        raise ValueError(f"adapter exited {r.returncode}: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    if len(r.stdout) > MAX_BATCH_BYTES:
        raise ValueError("adapter printed more than 4 MB")
    try:
        batch = json.loads(r.stdout.decode("utf-8"))
    except ValueError as e:
        raise ValueError(f"adapter printed no JSON ({e})") from None
    if not isinstance(batch, dict) or not all(isinstance(batch.get(k, []), list) for k in ("nodes", "edges", "aliases")):
        raise ValueError("adapter printed something other than {nodes, edges, aliases}")
    return {k: [x for x in batch.get(k, []) if isinstance(x, dict)] for k in ("nodes", "edges", "aliases")}


def load(db, feeder: dict, source: dict, batch: dict, vocab: dict) -> dict:
    """Upsert one source's snapshot, then retire what it gave before and gives no more."""
    oid = store.add_origin(db, "feeder", f"{feeder['name']}:{source['id']}")
    report = store.upsert(db, {**batch, "scope": "global"}, oid, vocab)
    now_live = set(report["stored"])
    stale = [r for r in db.execute("SELECT source_id, predicate, target_id FROM relations"
                                   " WHERE origin_id=? AND retired IS NULL", (oid,)) if tuple(r) not in now_live]
    for s, p, t in stale:
        store.retire_ids(db, s, p, t)
    db.execute("UPDATE origins SET at=? WHERE id=?", (time.time(), oid))  # the timeline shows when it last changed
    db.commit()
    store.set_title(db, oid, source["title"], "model")
    return {"facts": report["edges"], "retired": len(stale), "dropped": report["dropped"]}


def run() -> dict:
    feeders, problems = discover()
    try:
        sync_vocabularies(feeders)
    except (OSError, ValueError) as e:
        problems.append(f"vocabulary: {e}")
    vocab = store.load_vocabulary(sorted((data_dir() / "vocab").glob("*.json")))
    st, out = _state(), []
    db = None
    try:
        for f in feeders:
            for s in f["sources"]:
                file = feeds_dir() / f["name"] / s["file"]
                key = f"{f['name']}:{s['id']}"
                if not file.is_file():
                    continue
                stamp = [file.stat().st_size, file.stat().st_mtime_ns, f["plugin"]]  # a mod update reruns its adapter
                if st.get(key, {}).get("stamp") == stamp:
                    continue
                try:
                    batch = adapt(f, s, file)
                except (ValueError, OSError, subprocess.TimeoutExpired) as e:
                    problems.append(f"{key}: {e}")
                    st[key] = {"stamp": stamp, "at": time.time(), "error": str(e)[:300]}
                    continue
                db = db or store.connect()
                rep = load(db, f, s, batch, vocab)
                st[key] = {"stamp": stamp, "at": time.time(), "facts": rep["facts"], "retired": rep["retired"],
                           "dropped": len(rep["dropped"])}
                out.append({"source": key, **rep, "dropped": rep["dropped"][:20]})
    finally:
        if db is not None:
            db.close()
    _save_state(st)
    if out or problems:
        with open(data_dir() / "capture.jsonl", "a", encoding="utf-8") as log:
            for o in out:
                log.write(json.dumps({"at": time.time(), "event": "fed", **o}) + "\n")
    return {"feeders": [f["name"] for f in feeders], "loaded": out, "problems": problems,
            "facts": sum(o["facts"] for o in out), "retired": sum(o["retired"] for o in out)}


def listing() -> dict:
    feeders, problems = discover()
    st = _state()
    return {"feeders": [{"name": f["name"], "plugin": f["plugin"],
                         "sources": [{**s, "last": st.get(f"{f['name']}:{s['id']}")} for s in f["sources"]]}
                        for f in feeders], "problems": problems}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "list"])
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        out = run() if args.cmd == "run" else listing()
    except Exception as e:  # noqa: BLE001 - the hook reads {"error"} rather than a traceback
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
