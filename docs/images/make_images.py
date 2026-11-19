r"""Remake the README's screenshots from the made-up demo graph (docs/images/demo_graph.py), never a real one.

    py -I docs/images/make_images.py [--browser PATH]

Builds the demo graph in a temporary folder, renders the map page from it, and screenshots each view with a
Chromium browser in headless mode (Edge on Windows by default, else Chrome or Chromium on PATH), in light and dark.
Writes docs/images/map-*.png and docs/images/recall.txt (what Claude is handed for one prompt).
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "graph"))
sys.path.insert(0, str(HERE))

# (file name, hash for graph/view.html, window size)
SHOTS = [
    ("map-structured", "focus=Tidepool", (1440, 900)),
    ("map-free", "layout=free&focus=Lantern Mod", (1440, 900)),
    ("map-timeline", "tab=time", (1200, 900)),
    ("map-find", "q=upload&focus=Resumable uploads", (1440, 900)),
]
PROMPT, CWD = "why did we pick resumable uploads for Tidepool?", r"C:\dev\tidepool"


def find_browser(given: str) -> str:
    candidates = [given] if given else [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        shutil.which("chromium") or "", shutil.which("google-chrome") or "", shutil.which("chrome") or ""]
    for c in candidates:
        if c and Path(c).exists():
            return c
    sys.exit("No Chromium browser found; pass --browser PATH")


def shoot(browser: str, page: Path, hash_: str, size: tuple, out: Path, dark: bool, profile: str) -> None:
    url = page.resolve().as_uri() + "#" + hash_.replace(" ", "%20")
    args = [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
            f"--user-data-dir={profile}", f"--window-size={size[0]},{size[1]}", "--virtual-time-budget=6000",
            f"--screenshot={out}", url]
    if dark:
        args.insert(2, "--force-dark-mode")
        args.insert(2, "--blink-settings=preferredColorScheme=0")
    else:
        args.insert(2, "--blink-settings=preferredColorScheme=1")
    subprocess.run(args, check=True, capture_output=True, timeout=120)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--browser", default="")
    args = ap.parse_args()
    browser = find_browser(args.browser)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "graph.db"
        os.environ["GRAPHCRAFT_DB"] = str(db)
        import demo_graph  # noqa: E402 - after GRAPHCRAFT_DB is set
        import recall  # noqa: E402
        import view  # noqa: E402
        demo_graph.build(db)
        page = Path(tmp) / "map.html"
        view.build(out=str(page))
        for name, hash_, size in SHOTS:
            for dark in (False, True):
                out = HERE / f"{name}{'-dark' if dark else ''}.png"
                shoot(browser, page, hash_, size, out, dark, str(Path(tmp) / "profile"))
                print(out.relative_to(ROOT))
        ctx = recall.recall(PROMPT, CWD)["context"]
        (HERE / "recall.txt").write_text(f"> {PROMPT}\n\n{ctx}\n", encoding="utf-8")
        print((HERE / "recall.txt").relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
