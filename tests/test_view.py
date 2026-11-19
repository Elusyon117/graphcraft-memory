r"""The graph map page. Run: py -I tests/test_view.py"""
import json
import os
import re
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "graph"))
import store  # noqa: E402
import view  # noqa: E402

FIXTURE = json.loads((HERE / "fixture.json").read_text(encoding="utf-8"))
VOCAB = store.load_vocabulary([HERE / "fixture_vocab.json"])


def embedded(html: str) -> dict:
    m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


class ViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["GRAPHCRAFT_DB"] = str(Path(self.tmp.name) / "graph.db")
        with closing(store.connect()) as db:
            store.upsert(db, FIXTURE, store.add_origin(db, "session", "s-1", r"C:\mods\Campfires"), VOCAB)

    def tearDown(self):
        del os.environ["GRAPHCRAFT_DB"]
        self.tmp.cleanup()

    def build(self, **kw):
        out = view.build(out=str(Path(self.tmp.name) / "map.html"), **kw)
        return out, Path(out["path"]).read_text(encoding="utf-8")

    def test_export_has_nodes_edges_projects_and_aliases(self):
        with closing(store.connect()) as db:
            data = view.export(db)
        self.assertEqual(len(data["nodes"]), 7)
        self.assertEqual(len(data["edges"]), 5)
        self.assertEqual(data["projects"], [r"C:\mods\Campfires"])
        anim = next(n for n in data["nodes"] if n["name"] == "AnimBP_Player")
        self.assertIn("player animbp", anim["aliases"])
        self.assertEqual({e["kind"] for e in data["edges"]}, {"session"})

    def test_retired_facts_are_left_out(self):
        with closing(store.connect()) as db:
            store.retire(db, "Bedroll Mod", "overrides", "AnimBP_Player")
            self.assertEqual(len(view.export(db)["edges"]), 4)

    def test_timeline_data_lists_sources_and_retired_facts(self):
        with closing(store.connect()) as db:
            store.upsert(db, {"nodes": [{"name": "Zone Kit", "type": "TOOL"}],
                              "edges": [{"source": "Campfires", "predicate": "tried", "target": "Zone Kit"}]},
                         store.add_origin(db, "feeder", "zonecraft:conflicts"), VOCAB)
            store.retire(db, "Bedroll Mod", "overrides", "AnimBP_Player")
            data = view.export(db)
        sources = {(s["kind"], s["ref"]): s for s in data["sources"]}
        self.assertEqual((sources[("session", "s-1")]["facts"], sources[("session", "s-1")]["retired"]), (4, 1))
        self.assertEqual(sources[("feeder", "zonecraft:conflicts")]["facts"], 1)
        self.assertTrue(all("title" in s and "titleIsAuto" in s for s in data["sources"]))
        self.assertEqual([s["at"] for s in data["sources"]], sorted(s["at"] for s in data["sources"]))
        self.assertEqual(len(data["retired"]), 1)
        self.assertEqual(data["retired"][0]["p"], "overrides")
        self.assertGreater(data["retired"][0]["retiredAt"], 0)
        self.assertNotIn("overrides", [e["p"] for e in data["edges"] if e["s"] == data["retired"][0]["s"]])
        kit = next(n for n in data["nodes"] if n["name"] == "Zone Kit")
        self.assertEqual(kit["origin"], "feeder:zonecraft:conflicts")

    def test_the_map_opens_in_the_layout_from_the_settings(self):
        self.assertEqual(embedded(self.build()[1])["layout"], "structured")
        store.save_settings({"mapLayout": "free"})
        time.sleep(0.01)
        _, html = self.build()
        self.assertEqual(embedded(html)["layout"], "free")
        for needle in ('id="mini"', 'id="zIn"', 'id="bands"', "function layoutStruct"):
            self.assertIn(needle, html)

    def test_page_has_the_timeline_tab(self):
        _, html = self.build()
        self.assertIn('id="tTime"', html)
        self.assertIn('id="timeline"', html)
        self.assertEqual(embedded(html)["sources"][0]["ref"], "s-1")

    def test_page_embeds_the_data(self):
        out, html = self.build(project="Campfires")
        data = embedded(html)
        self.assertEqual(out["nodes"], 7)
        self.assertEqual(len(data["edges"]), 5)
        self.assertEqual(data["startProject"], "Campfires")
        self.assertNotIn("/*__GRAPH_DATA__*/null", html)

    def test_page_loads_nothing_from_the_network(self):
        _, html = self.build()
        self.assertIsNone(re.search(r'(src|href)\s*=\s*["\']?https?://', html, re.I))
        self.assertNotIn("@import", html)

    def test_a_name_with_script_end_tag_cannot_break_out(self):
        with closing(store.connect()) as db:
            store.upsert(db, {"nodes": [{"name": "</script><script>alert(1)</script>", "type": "CONCEPT"}]},
                         store.add_origin(db, "file", "evil"), VOCAB)
        _, html = self.build()
        self.assertEqual(html.count("</script>"), 2)  # the data block and the app script, nothing more
        self.assertIn("</script><script>alert(1)</script>", [n["name"] for n in embedded(html)["nodes"]])

    def test_empty_or_missing_graph_still_builds(self):
        os.environ["GRAPHCRAFT_DB"] = str(Path(self.tmp.name) / "none" / "graph.db")
        out, html = self.build()
        self.assertEqual(out["nodes"], 0)
        self.assertEqual(embedded(html)["nodes"], [])
        self.assertFalse(Path(self.tmp.name, "none", "graph.db").exists())


if __name__ == "__main__":
    unittest.main()
