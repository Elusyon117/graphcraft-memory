r"""Worked files: learn what the code files a session worked in hold (docs/DESIGN.md § Worked files).

    py -I files.py learn          read every session's list in <data>/worked/, store each file's outline, drop the lists

The mod writes <data>/worked/<session>.json as a session reads and edits files:
    {"sessionId", "files": {"<absolute path>": "read" | "edit"}}
No model: a file's outline is its opening comment and the names it exports, read by code. Only repos where the
session edited something are learned (reading alone is only looking), and then the files it read there too, since
those are what it built on. Each file is a snapshot under origin kind "file", like a project doc: when the file
changes, facts it no longer gives are retired.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402
import docs  # noqa: E402
import store  # noqa: E402

MAX_FILES = 30          # per session, edited files first
MAX_BYTES = 300_000     # bigger files are data or generated
MAX_DESC = 240
MAX_SUMMARY = 140
MAX_EXPORTS = 10
MAX_UP = 12             # folders walked up looking for the repo root
CODE = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".rs", ".go", ".java", ".kt", ".cs", ".c", ".cc", ".cpp",
        ".h", ".hpp", ".lua", ".rb", ".php", ".swift", ".sh", ".ps1", ".css", ".scss", ".vue", ".svelte"}
JS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte"}
JS_EXPORT = re.compile(r"^export\s+(?:default\s+)?(?:declare\s+)?(?:async\s+)?(?:const|let|var|function\*?|class|type|interface|enum)\s+([A-Za-z_$][\w$]*)", re.M)
JS_EXPORT_LIST = re.compile(r"^export\s*\{([^}]*)\}", re.M)
JS_IMPORT = re.compile(r"""(?:from|import)\s*\(?\s*['"](\.{1,2}/[^'"]+)['"]""")
PY_DEF = re.compile(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z]\w*)", re.M)
PY_IMPORT = re.compile(r"^(?:from\s+(\w+)\s+import|import\s+(\w+))", re.M)
JS_TRY = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", "/index.ts", "/index.tsx", "/index.js")


def worked_dir() -> Path:
    return store.db_path().parent / "worked"


def _state_file() -> Path:
    return store.db_path().parent / "files.json"


def repo_root(path: Path) -> Path | None:
    """The nearest folder above `path` holding .git; never the home folder or a drive root."""
    for d in list(path.parents)[:MAX_UP]:
        if (d / ".git").exists():
            return d if docs._is_project(d) else None
    return None


def _opening_comment(text: str, ext: str) -> str:
    """The comment or docstring a file opens with, as one line of prose."""
    lines = text.lstrip("﻿").splitlines()
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].startswith("#!") or "coding" in lines[i][:30]):
        i += 1
    out = []
    if ext == ".py" and i < len(lines) and re.match(r'^[rubRUB]*("""|\'\'\')', lines[i].strip()):
        quote = re.match(r'^[rubRUB]*("""|\'\'\')', lines[i].strip()).group(1)
        first = lines[i].strip().split(quote, 1)[1]
        if quote in first:
            return first.split(quote)[0].strip()
        out.append(first)
        for line in lines[i + 1:]:
            if quote in line or not line.strip():  # the first paragraph is the summary
                out.append(line.split(quote)[0])
                break
            out.append(line)
    elif i < len(lines) and lines[i].strip().startswith("/*"):
        for line in lines[i:]:
            out.append(re.sub(r"^\s*(/\*+|\*/|\*)\s?", "", line).split("*/")[0])
            if "*/" in line:
                break
    else:
        for line in lines[i:]:
            m = re.match(r"^\s*(//+|#+|--)\s?(.*)$", line)
            if not m:
                break
            out.append(m.group(2))
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def exports(text: str, ext: str) -> list:
    if ext in JS:
        names = JS_EXPORT.findall(text)
        for group in JS_EXPORT_LIST.findall(text):
            names += [p.split(" as ")[-1].strip() for p in group.split(",") if p.strip() and not p.strip().startswith("type ")]
    elif ext == ".py":
        names = [n for n in PY_DEF.findall(text) if not n.startswith("_") and n != "main"]
    else:
        return []
    return list(dict.fromkeys(n for n in names if n and n != "default"))


def imports(path: Path, text: str) -> list:
    """Files in the same repo this one imports by a relative path."""
    out = []
    if path.suffix in JS:
        for spec in JS_IMPORT.findall(text):
            base = (path.parent / spec).resolve()
            for tail in JS_TRY:
                cand = Path(str(base) + tail)
                if cand.is_file():
                    out.append(cand)
                    break
    elif path.suffix == ".py":
        for a, b in PY_IMPORT.findall(text):
            cand = path.parent / f"{a or b}.py"
            if cand.is_file():
                out.append(cand.resolve())
    return out


def outline(path: Path, text: str) -> str:
    """Opening comment, then what it exports, cut to MAX_DESC characters."""
    # The first sentence only, so the exports (what a session reuses) keep their room.
    summary = re.split(r"(?<=[.!?])\s", _opening_comment(text, path.suffix), maxsplit=1)[0]
    if len(summary) > MAX_SUMMARY:
        summary = summary[:MAX_SUMMARY - 1].rsplit(" ", 1)[0] + "…"
    names = exports(text, path.suffix)
    parts = [summary.rstrip(".") + "." if summary else ""]
    if names:
        more = ", …" if len(names) > MAX_EXPORTS else ""
        parts.append(f"Exports {', '.join(names[:MAX_EXPORTS])}{more}.")
    desc = capture.clean(" ".join(p for p in parts if p))
    return desc if len(desc) <= MAX_DESC else desc[:MAX_DESC - 1].rsplit(" ", 1)[0] + "…"


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def chosen(files: dict) -> dict:
    """{repo root: [paths]} worth learning: code files in repos where the session edited something, edits first."""
    by_repo: dict = {}
    for raw, how in files.items():
        p = Path(raw)
        if p.suffix.lower() not in CODE or not p.is_file():
            continue
        try:
            p = p.resolve()
            if p.stat().st_size > MAX_BYTES:
                continue
        except OSError:
            continue
        root = repo_root(p)
        if root is not None:
            by_repo.setdefault(root, []).append((how != "edit", str(p)))
    out, left = {}, MAX_FILES
    for root, items in by_repo.items():
        if not any(not read for read, _ in items) or left <= 0:
            continue
        keep = [Path(p) for _, p in sorted(items)][:left]
        left -= len(keep)
        out[root] = keep
    return out


def learn_session(db, files: dict, st: dict, vocab: dict) -> dict:
    report = {"files": 0, "facts": 0, "retired": 0}
    for root, paths in chosen(files).items():
        project, rels = str(root), {p: _rel(p, root) for p in paths}
        for p in paths:
            try:
                text = p.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            ref, h = docs._ref(p), hashlib.sha1(text.encode("utf-8")).hexdigest()
            if st.get(ref, {}).get("hash") == h:
                continue
            name = rels[p]
            # Imported files named as nodes too (no description, so a stored one stays): the edge needs both ends.
            deps = list(dict.fromkeys(rels[d] for d in imports(p, text) if d in rels and d != p))
            batch = {
                "nodes": [{"name": name, "type": "FILE", "description": outline(p, text)}, {"name": root.name, "type": "PROJECT"}]
                + [{"name": d, "type": "FILE"} for d in deps],
                "edges": [{"source": name, "predicate": "part_of", "target": root.name}]
                + [{"source": name, "predicate": "depends_on", "target": d} for d in deps],
                "aliases": [{"entity": name, "alias": p.name}] if p.name != name else [],
            }
            rep = docs.load_snapshot(db, ref, project, f"{name} (code)", batch, vocab)
            st[ref] = {"hash": h, "at": time.time(), "project": project}
            report["files"] += 1
            report["facts"] += rep["facts"]
            report["retired"] += rep["retired"]
    return report


def learn() -> dict:
    lists = sorted(worked_dir().glob("*.json")) if worked_dir().exists() else []
    out = {"sessions": 0, "files": 0, "facts": 0, "retired": 0}
    if not lists:
        return out
    try:
        st = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    vocab = capture.learnable_vocabulary()
    with closing(store.connect()) as db:
        for f in lists:
            try:
                job = json.loads(f.read_text(encoding="utf-8-sig"))
                files = job.get("files") if isinstance(job, dict) else None
                if isinstance(files, dict):
                    rep = learn_session(db, files, st, vocab)
                    out["sessions"] += 1
                    for k in ("files", "facts", "retired"):
                        out[k] += rep[k]
            except (OSError, ValueError) as e:
                out.setdefault("problems", []).append(f"{f.name}: {e}")
            f.unlink(missing_ok=True)
    _state_file().parent.mkdir(parents=True, exist_ok=True)
    _state_file().write_text(json.dumps(st), encoding="utf-8")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("learn")
    ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        out = learn()
    except Exception as e:  # one JSON answer either way, so the mod can log it
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
