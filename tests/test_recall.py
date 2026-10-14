"""Focused standalone recall tests; run with `python3 -m unittest test_recall.py`."""

import unittest
import sys
import json
import shlex
import subprocess
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recall


def record(ident, kind="decision", claim="authored claim"):
    return {"id": ident, "kind": kind, "topic": "topic", "title": ident,
            "claim": claim, "actor": "author", "authority": "reported_human",
            "state": "accepted", "recorded_at": "2026-01-01",
            "sources": [{"path": "packet.txt", "start_line": 1, "end_line": 1,
                         "sha256": "digest", "excerpt": "source text"}]}


def link(source, relation, target):
    return {"from": source, "relation": relation, "to": target,
            "note": "authored reason", "sources": []}


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.store = {"schema": "decision-store.v1", "records": [
            record("old"), record("new"), record("premise", "judgment"),
            record("code", "implementation")], "edges": [
                link("new", "supersedes", "old"),
                link("old", "depends_on_premise", "premise"),
                link("code", "implements", "old")]}

    def test_hidden_superseder_and_premise_at_depth_zero(self):
        result = recall.recall(self.store, seeds=["old"], profile="review", depth=0)
        self.assertEqual({"new", "premise"}, {r["card"]["id"] for r in result["records"] if r["reason"] == "warning_endpoint"})
        self.assertEqual({"supersedes", "depends_on_premise"}, {e["relation"] for e in result["warnings"]})
        self.assertGreater(result["counts"]["depth_limited"], 0)
        self.assertFalse(result["complete"])

    def test_budget_omission_is_counted_and_followable(self):
        result = recall.recall(self.store, seeds=["old"], budget=700)
        self.assertLessEqual(len(recall.encoded(result)), 700)
        self.assertGreater(result["counts"]["budget_omitted"], 0)
        self.assertIn("--seed old", result["follow_up"])
        self.assertFalse(result["complete"])

    def test_unknown_seed_and_sources_default(self):
        self.assertEqual(["unknown"], recall.recall(self.store, seeds=["unknown"])["unknown_seeds"])
        plain = recall.recall(self.store, seeds=["old"], depth=0)
        cited = recall.recall(self.store, seeds=["old"], depth=0, sources=True)
        self.assertNotIn("source text", recall.encoded(plain).decode())
        self.assertIn("source text", recall.encoded(cited).decode())

    def test_query_discovery_does_not_depend_on_profile(self):
        roots = [recall.recall(self.store, query="topic", profile=profile, depth=0)["roots"]
                 for profile in recall.PROFILES]
        self.assertTrue(all(root == roots[0] for root in roots))

    def test_many_seeds_are_not_cut_by_query_limit(self):
        result = recall.recall(self.store, seeds=["old", "new"], limit=1, depth=0)
        self.assertEqual(["old", "new"], result["roots"])
        self.assertIn("--seed new", result["follow_up"])

    def test_dense_warnings_page_with_stable_cursor(self):
        store = {"schema": "decision-store.v1", "records": [record("old")] +
                 [record(f"new{i:03}") for i in range(40)],
                 "edges": [link(f"new{i:03}", "supersedes", "old") for i in range(40)]}
        first = recall.recall(store, seeds=["old"], depth=0, budget=1000)
        self.assertLessEqual(len(recall.encoded(first)), 1000)
        self.assertTrue(any(r["card"]["id"] == "old" for r in first["records"]))
        cursor = first["warning_page"]["next_offset"]
        self.assertIsNotNone(cursor)
        self.assertEqual(cursor, len(first["warnings"]))
        follow_offset = 0 if first["counts"]["warning_details_omitted"] else cursor
        self.assertIn(f"--warning-offset {follow_offset}", first["follow_up"])
        second = recall.recall(store, seeds=["old"], depth=0, budget=1000, warning_offset=cursor)
        self.assertNotEqual(first["warnings"][0]["from"], second["warnings"][0]["from"])
        self.assertFalse(second["complete"])

    def test_warning_followup_keeps_profile_depth_and_cursor_population(self):
        store = {"schema": "decision-store.v1", "records": [record("old"), record("code", "implementation"),
                 record("aaa")] + [record(f"new{i:03}") for i in range(30)],
                 "edges": [link(f"new{i:03}", "supersedes", "old") for i in range(30)] +
                 [link("code", "implements", "old"), link("aaa", "supersedes", "code")]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.json"
            path.write_text(json.dumps(store), encoding="utf-8")
            first = recall.recall(path, seeds=["old"], profile="review", depth=0, budget=1000)
            cursor = first["warning_page"]["next_offset"]
            self.assertIsNotNone(cursor)
            command = shlex.split(first["follow_up"])
            self.assertEqual("review", command[command.index("--profile") + 1])
            self.assertEqual("0", command[command.index("--depth") + 1])
            follow_offset = 0 if first["counts"]["warning_details_omitted"] else cursor
            self.assertEqual(str(follow_offset), command[command.index("--warning-offset") + 1])
            result = subprocess.run([sys.executable, str(Path(recall.__file__)), *command[2:]],
                                    capture_output=True, text=True, check=True)
            second = json.loads(result.stdout)
            expected = recall.recall(path, seeds=["old"], profile="review", depth=0,
                                     budget=int(command[command.index("--budget") + 1]), warning_offset=follow_offset)
            self.assertEqual(expected["warnings"], second["warnings"])
            self.assertEqual("new%03d" % follow_offset, second["warnings"][0]["from"])


if __name__ == "__main__":
    unittest.main()
