r"""A made-up graph for the README's pictures: three invented projects and a dozen sessions over two weeks.

Nothing here comes from anyone's real memory (CLAUDE.md rule 2); the names, paths and sessions are invented.

    py -I docs/images/demo_graph.py DB      write the demo graph to DB (a scratch path; it is replaced)
"""
import hashlib
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "graph"))
import store  # noqa: E402

TIDE, LANTERN, ORBIT = r"C:\dev\tidepool", r"C:\dev\lantern-mod", r"C:\dev\orbit-cli"
DAY = 86400

# (days ago, project, session title, nodes, edges)  nodes: (name, TYPE, description)
SESSIONS = [
    (13, TIDE, "Set up Tidepool's stack",
     [("Tidepool", "PROJECT", "Web app for sharing tide-pool photos."), ("User", "PERSON", "The person using Claude Code."),
      ("Vite", "TOOL", "Front-end build tool."), ("Postgres", "TOOL", "The database."),
      ("Server-side rendering", "DECISION", "Pages render on the server for fast first load."),
      ("Slow first paint", "PROBLEM", "The client-only build took 4 s to show anything on phones.")],
     [("User", "works_on", "Tidepool"), ("Tidepool", "uses", "Vite"), ("Tidepool", "uses", "Postgres"),
      ("Tidepool", "decided", "Server-side rendering"), ("Server-side rendering", "because", "Slow first paint"),
      ("Slow first paint", "fixed_by", "Server-side rendering")]),
    (12, TIDE, "Photo uploads time out",
     [("Upload timeouts", "PROBLEM", "Large photos hit the 30 s request limit."),
      ("Resumable uploads", "DECISION", "Uploads go in 5 MB chunks that can resume."),
      ("upload.ts", "FILE", "The upload handler."), ("Short answers", "PREFERENCE", "Answer first, no recap.")],
     [("Tidepool", "decided", "Resumable uploads"), ("Resumable uploads", "because", "Upload timeouts"),
      ("Upload timeouts", "about", "upload.ts"), ("Upload timeouts", "fixed_by", "Resumable uploads"),
      ("User", "prefers", "Short answers")]),
    (11, LANTERN, "Lantern light flickers in caves",
     [("Lantern Mod", "PROJECT", "Game mod: a lantern that reacts to darkness."), ("Godot", "TOOL", "The game engine."),
      ("Light flicker", "PROBLEM", "The lantern flickers when two light zones overlap."),
      ("One light owner", "DECISION", "Only the nearest zone may set the lantern's brightness."),
      ("lantern.gd", "FILE", "The lantern's script.")],
     [("User", "works_on", "Lantern Mod"), ("Lantern Mod", "uses", "Godot"),
      ("Light flicker", "about", "lantern.gd"), ("Lantern Mod", "decided", "One light owner"),
      ("One light owner", "because", "Light flicker"), ("Light flicker", "fixed_by", "One light owner")]),
    (10, LANTERN, "Save files across versions",
     [("Versioned saves", "DECISION", "Each save file carries a format version and is migrated on load."),
      ("Broken old saves", "PROBLEM", "Saves from 1.2 crashed 1.3 on load."), ("save_game.gd", "FILE", "Save and load.")],
     [("Lantern Mod", "decided", "Versioned saves"), ("Versioned saves", "because", "Broken old saves"),
      ("Broken old saves", "about", "save_game.gd"), ("Broken old saves", "fixed_by", "Versioned saves")]),
    (9, ORBIT, "Start Orbit CLI",
     [("Orbit CLI", "PROJECT", "Command line for scheduling satellite passes."), ("Rust", "TOOL", "The language."),
      ("clap", "TOOL", "Argument parsing."), ("Plain text output", "DECISION", "Output is plain columns; --json for scripts."),
      ("Pipes into other tools", "CONCEPT", "People pipe the output into grep and awk.")],
     [("User", "works_on", "Orbit CLI"), ("Orbit CLI", "uses", "Rust"), ("Orbit CLI", "uses", "clap"),
      ("Orbit CLI", "decided", "Plain text output"), ("Plain text output", "because", "Pipes into other tools")]),
    (8, TIDE, "Pick the image service",
     [("Image resizing", "CONCEPT", "Thumbnails at three sizes."), ("Sharp", "TOOL", "Image processing library."),
      ("Resize on upload", "DECISION", "Thumbnails are made once, at upload, not per request."),
      ("CPU spikes", "PROBLEM", "Resizing per request pinned the CPU at peak hours.")],
     [("Tidepool", "uses", "Sharp"), ("Resize on upload", "about", "Image resizing"), ("Tidepool", "decided", "Resize on upload"),
      ("Resize on upload", "because", "CPU spikes"), ("CPU spikes", "fixed_by", "Resize on upload")]),
    (7, ORBIT, "Time zones in pass times",
     [("UTC everywhere", "DECISION", "Times are stored and printed in UTC unless --local is given."),
      ("Wrong pass times", "PROBLEM", "Passes showed an hour off after the clock change."),
      ("schedule.rs", "FILE", "Pass scheduling.")],
     [("Orbit CLI", "decided", "UTC everywhere"), ("UTC everywhere", "because", "Wrong pass times"),
      ("Wrong pass times", "about", "schedule.rs"), ("Wrong pass times", "fixed_by", "UTC everywhere")]),
    (6, LANTERN, "Settings menu for the lantern",
     [("Mod settings menu", "DECISION", "Brightness and battery drain are player settings."),
      ("Players want dimmer light", "CONCEPT", "Feedback: the lantern is too bright in co-op."),
      ("No hard-coded numbers", "PREFERENCE", "Tunable values go in settings, not in code.")],
     [("Lantern Mod", "decided", "Mod settings menu"), ("Mod settings menu", "because", "Players want dimmer light"),
      ("User", "prefers", "No hard-coded numbers")]),
    (4, TIDE, "Search is slow on big albums",
     [("Slow album search", "PROBLEM", "Search over 10k photos takes 2 s."),
      ("Full-text index", "DECISION", "A Postgres full-text index on captions and tags."), ("search.ts", "FILE", "Search endpoint.")],
     [("Slow album search", "about", "search.ts"), ("Tidepool", "decided", "Full-text index"),
      ("Full-text index", "because", "Slow album search"), ("Full-text index", "depends_on", "Postgres")]),
    (3, ORBIT, "Release builds for three platforms",
     [("GitHub Actions", "TOOL", "CI."), ("Cross-compile in CI", "DECISION", "Windows, macOS and Linux builds come from CI."),
      ("Unsigned macOS build", "PROBLEM", "macOS blocks the binary until it is signed.")],
     [("Orbit CLI", "uses", "GitHub Actions"), ("Orbit CLI", "decided", "Cross-compile in CI"),
      ("Orbit CLI", "blocked_by", "Unsigned macOS build")]),
    (1, LANTERN, "Battery drain balance",
     [("Battery drain", "CONCEPT", "How fast the lantern's battery empties."),
      ("Drain by brightness", "DECISION", "Drain scales with brightness, so dimmer light lasts longer.")],
     [("Lantern Mod", "decided", "Drain by brightness"), ("Drain by brightness", "about", "Battery drain"),
      ("Drain by brightness", "because", "Players want dimmer light")]),
    (0, TIDE, "Search ranking for captions",
     [("Rank by recency", "DECISION", "Newer photos rank higher when relevance ties.")],
     [("Tidepool", "decided", "Rank by recency"), ("Rank by recency", "about", "Slow album search")]),
]
# Changed minds, so the timeline shows a retired fact: Tidepool first decided client-side rendering.
RETIRED = (13, TIDE, ("Client-side rendering", "DECISION", "Everything rendered in the browser."), ("Tidepool", "decided", "Client-side rendering"))


def build(path: Path) -> dict:
    for p in (path, Path(str(path) + "-journal")):
        p.unlink(missing_ok=True)
    os.environ["GRAPHCRAFT_DB"] = str(path)
    db = store.connect(path)
    vocab = store.load_vocabulary()
    now = time.time()
    first = {}
    days, project, node, edge = RETIRED
    o = store.add_origin(db, "session", "demo-0000", project)
    store.upsert(db, {"nodes": [{"name": node[0], "type": node[1], "description": node[2]},
                                {"name": "Tidepool", "type": "PROJECT"}],
                      "edges": [dict(zip(("source", "predicate", "target"), edge))]}, o, vocab)
    for i, (days, project, title, nodes, edges) in enumerate(SESSIONS):
        h = hashlib.md5(title.encode()).hexdigest()  # looks like a session id, means nothing
        ref = f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
        o = store.add_origin(db, "session", ref, project)
        rep = store.upsert(db, {"nodes": [{"name": n, "type": t, "description": d} for n, t, d in nodes],
                                "edges": [{"source": s, "predicate": p, "target": t} for s, p, t in edges]}, o, vocab)
        assert not rep["dropped"], rep["dropped"]  # the demo must be a valid graph
        store.set_title(db, o, title, "model")
        at = now - days * DAY - (12 - i) * 1800
        db.execute("UPDATE origins SET at=? WHERE id=?", (at, o))
        db.execute("UPDATE relations SET created=? WHERE origin_id=?", (at, o))
        first.setdefault(days, at)
    db.execute("UPDATE origins SET at=? WHERE ref='demo-0000'", (now - 13 * DAY - 7200,))
    db.execute("UPDATE relations SET created=? WHERE origin_id=(SELECT id FROM origins WHERE ref='demo-0000')",
               (now - 13 * DAY - 7200,))
    store.retire(db, *edge)
    db.commit()
    out = store.stats(db)
    db.close()
    return out


if __name__ == "__main__":
    print(build(Path(sys.argv[1])))
