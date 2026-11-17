"""tests for ghostwalk (stdlib unittest, no third-party deps)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ghostwalk  # noqa: E402


def write_tmp(directory, name, payload, as_json=True):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        if as_json:
            json.dump(payload, handle)
        else:
            handle.write(payload)
    return path


class ParseTimestampTests(unittest.TestCase):
    def test_epoch_number(self):
        stamp = ghostwalk.parse_timestamp(1700000000)
        self.assertEqual(stamp.timestamp(), 1700000000)
        self.assertIsNotNone(stamp.tzinfo)

    def test_epoch_string(self):
        self.assertEqual(ghostwalk.parse_timestamp("1700000000").timestamp(),
                         1700000000)

    def test_iso_z(self):
        self.assertEqual(ghostwalk.parse_timestamp("2023-11-14T22:13:20Z")
                         .timestamp(), 1700000000)

    def test_iso_offset(self):
        stamp = ghostwalk.parse_timestamp("2023-11-14T22:13:20+00:00")
        self.assertEqual(stamp.timestamp(), 1700000000)

    def test_naive_iso_read_as_utc(self):
        stamp = ghostwalk.parse_timestamp("2023-11-14T22:13:20")
        self.assertEqual(stamp.timestamp(), 1700000000)

    def test_junk_is_none(self):
        self.assertIsNone(ghostwalk.parse_timestamp("not a time"))
        self.assertIsNone(ghostwalk.parse_timestamp(""))
        self.assertIsNone(ghostwalk.parse_timestamp(None))
        self.assertIsNone(ghostwalk.parse_timestamp(True))


class LoadRowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_json_bare_list(self):
        path = write_tmp(self.dir, "a.json",
                         [{"id": "r1", "author": "a", "timestamp": 1}])
        self.assertEqual(ghostwalk.load_rows(path),
                         [{"id": "r1", "author": "a", "timestamp": 1}])

    def test_json_wrapped_rows(self):
        path = write_tmp(self.dir, "b.json",
                         {"rows": [{"id": "r1", "author": "a", "timestamp": 1}]})
        self.assertEqual(len(ghostwalk.load_rows(path)), 1)

    def test_json_id_map(self):
        path = write_tmp(self.dir, "c.json",
                         {"r1": {"author": "a", "timestamp": 1},
                          "r2": {"author": "b", "timestamp": 2}})
        rows = ghostwalk.load_rows(path)
        self.assertEqual(sorted(r["id"] for r in rows), ["r1", "r2"])

    def test_json_bad_shape_raises(self):
        path = write_tmp(self.dir, "d.json", {"rows": "nope"})
        with self.assertRaises(ghostwalk.GhostwalkError):
            ghostwalk.load_rows(path)

    def test_single_bare_row_object(self):
        path = write_tmp(self.dir, "i.json",
                         '{"id": "r1", "author": "a", "timestamp": 1}',
                         as_json=False)
        rows = ghostwalk.load_rows(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "r1")

    def test_jsonl(self):
        path = write_tmp(self.dir, "g.jsonl",
                         '{"id": "r1", "author": "a", "timestamp": 1}\n'
                         '\n'
                         '{"id": "r2", "author": "b", "timestamp": 2}\n',
                         as_json=False)
        rows = ghostwalk.load_rows(path)
        self.assertEqual([r["id"] for r in rows], ["r1", "r2"])

    def test_broken_json_and_jsonl_raises(self):
        path = write_tmp(self.dir, "h.json", '{"id": "r1",,\n', as_json=False)
        with self.assertRaises(ghostwalk.GhostwalkError):
            ghostwalk.load_rows(path)

    def test_csv(self):
        path = write_tmp(self.dir, "e.csv",
                         "id,author,timestamp,status\n"
                         "r1,a,1700000000,active\n", as_json=False)
        rows = ghostwalk.load_rows(path)
        self.assertEqual(rows[0]["id"], "r1")
        self.assertEqual(rows[0]["status"], "active")

    def test_csv_missing_header(self):
        path = write_tmp(self.dir, "f.csv", "", as_json=False)
        with self.assertRaises(ghostwalk.GhostwalkError):
            ghostwalk.load_rows(path)


class WalkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def old_new(self, old_rows, new_rows):
        old = write_tmp(self.dir, "old.json", old_rows)
        new = write_tmp(self.dir, "new.json", new_rows)
        return ghostwalk.walk(ghostwalk.load_rows(old),
                              ghostwalk.load_rows(new), 15)

    def test_lost_rows_named(self):
        old = [{"id": "r1", "author": "a", "timestamp": 100, "status": "active"},
               {"id": "r2", "author": "b", "timestamp": 200, "status": "active"}]
        new = [{"id": "r2", "author": "b", "timestamp": 200, "status": "active"}]
        findings = self.old_new(old, new)
        self.assertEqual([r["id"] for r in findings["lost"]], ["r1"])
        self.assertEqual(findings["counts"]["lost"], 1)

    def test_ghost_pair_detected(self):
        old = [{"id": "r1", "author": "w", "timestamp": 1700000000,
                "status": "active"},
               {"id": "r2", "author": "w", "timestamp": 1700000010,
                "status": "active"},
               {"id": "r3", "author": "w", "timestamp": 1700000500,
                "status": "active"}]
        new = [row for row in old if row["id"] != "r2"]
        findings = self.old_new(old, new)
        self.assertEqual(len(findings["twin_pairs"]), 1)
        pair = findings["twin_pairs"][0]
        self.assertEqual(pair["kind"], "ghost")
        self.assertEqual(pair["author"], "w")
        self.assertEqual(pair["older_id"], "r1")
        self.assertEqual(pair["newer_id"], "r2")
        self.assertEqual(pair["delta_seconds"], 10)

    def test_far_apart_no_pair(self):
        base = 1700000000
        old = [{"id": "r1", "author": "w", "timestamp": base, "status": "x"},
               {"id": "r2", "author": "w", "timestamp": base + 16, "status": "x"}]
        findings = self.old_new(old, old)
        self.assertEqual(findings["twin_pairs"], [])

    def test_window_boundary_inclusive(self):
        base = 1700000000
        old = [{"id": "r1", "author": "w", "timestamp": base, "status": "x"},
               {"id": "r2", "author": "w", "timestamp": base + 15, "status": "x"}]
        findings = self.old_new(old, old)
        self.assertEqual(len(findings["twin_pairs"]), 1)

    def test_custom_window(self):
        old = [{"id": "r1", "author": "w", "timestamp": 1000, "status": "x"},
               {"id": "r2", "author": "w", "timestamp": 1060, "status": "x"}]
        old_p = write_tmp(self.dir, "o.json", old)
        findings = ghostwalk.walk(ghostwalk.load_rows(old_p),
                                  ghostwalk.load_rows(old_p), 60)
        self.assertEqual(len(findings["twin_pairs"]), 1)
        self.assertEqual(findings["window_seconds"], 60)

    def test_different_authors_never_pair(self):
        old = [{"id": "r1", "author": "w", "timestamp": 1000, "status": "x"},
               {"id": "r2", "author": "v", "timestamp": 1001, "status": "x"}]
        findings = self.old_new(old, old)
        self.assertEqual(findings["twin_pairs"], [])

    def test_twin_kind_both_present(self):
        old = [{"id": "r1", "author": "w", "timestamp": 1000, "status": "x"},
               {"id": "r2", "author": "w", "timestamp": 1005, "status": "x"}]
        findings = self.old_new(old, old)
        self.assertEqual(findings["twin_pairs"][0]["kind"], "twin")

    def test_gone_kind_both_lost(self):
        old = [{"id": "r1", "author": "w", "timestamp": 1000, "status": "x"},
               {"id": "r2", "author": "w", "timestamp": 1005, "status": "x"},
               {"id": "r9", "author": "z", "timestamp": 900, "status": "x"}]
        findings = self.old_new(old, [old[2]])
        kinds = {p["older_id"]: p["kind"] for p in findings["twin_pairs"]}
        self.assertEqual(kinds["r1"], "gone")
        self.assertEqual([r["id"] for r in findings["lost"]], ["r1", "r2"])

    def test_survivor_uses_new_row_data(self):
        old = [{"id": "r1", "author": "w", "timestamp": 1000, "status": "old"}]
        new = [{"id": "r1", "author": "w", "timestamp": 1000, "status": "new"}]
        findings = self.old_new(old, new)
        self.assertEqual(findings["lost"], [])

    def test_malformed_rows_skipped_and_counted(self):
        old = [{"id": "r1", "author": "", "timestamp": 1000, "status": "x"},
               {"id": "r2", "author": "w", "timestamp": "nope", "status": "x"},
               {"id": "r3", "author": "w", "timestamp": 1001, "status": "x"}]
        findings = self.old_new(old, old)
        self.assertEqual(findings["counts"]["rows_skipped_for_twins"], 2)
        self.assertEqual(findings["twin_pairs"], [])

    def test_duplicate_ids_kept_first(self):
        old = [{"id": "r1", "author": "w", "timestamp": 1000, "status": "first"},
               {"id": "r1", "author": "w", "timestamp": 1001, "status": "second"}]
        findings = self.old_new(old, [old[0]])
        self.assertEqual(findings["counts"]["duplicate_ids"], 1)
        self.assertEqual(findings["lost"], [])

    def test_clean_listing_is_clean(self):
        old = [{"id": "r1", "author": "a", "timestamp": 1, "status": "s"}]
        new = [{"id": "r1", "author": "a", "timestamp": 1, "status": "s"},
               {"id": "r2", "author": "b", "timestamp": 900000, "status": "s"}]
        findings = self.old_new(old, new)
        self.assertEqual(findings["lost"], [])
        self.assertEqual(findings["twin_pairs"], [])

    def test_mixed_iso_and_epoch_line_up(self):
        old = [{"id": "r1", "author": "w",
                "timestamp": "2023-11-14T22:13:20Z", "status": "x"},
               {"id": "r2", "author": "w", "timestamp": 1700000009,
                "status": "x"}]
        findings = self.old_new(old, old)
        self.assertEqual(len(findings["twin_pairs"]), 1)
        self.assertEqual(findings["twin_pairs"][0]["delta_seconds"], 9)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "ghostwalk.py")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        return subprocess.run(
            [sys.executable, self.script] + list(argv),
            capture_output=True, text=True)

    def test_clean_exit_zero(self):
        rows = [{"id": "r1", "author": "a", "timestamp": 1, "status": "s"}]
        old = write_tmp(self.dir, "old.json", rows)
        new = write_tmp(self.dir, "new.json", rows)
        result = self.run_cli(old, new)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verdict: clean", result.stdout)

    def test_ghost_found_exit_one(self):
        old = [{"id": "r1", "author": "w", "timestamp": 1700000000,
                "status": "active"},
               {"id": "r2", "author": "w", "timestamp": 1700000012,
                "status": "active"}]
        new = [row for row in old if row["id"] != "r2"]
        old_p = write_tmp(self.dir, "old.json", old)
        new_p = write_tmp(self.dir, "new.json", new)
        result = self.run_cli(old_p, new_p)
        self.assertEqual(result.returncode, 1)
        self.assertIn("[ghost]", result.stdout)
        self.assertIn("id=r2", result.stdout)
        self.assertIn("12s apart", result.stdout)

    def test_json_output_and_window_flag(self):
        old = [{"id": "r1", "author": "w", "timestamp": 1, "status": "x"},
               {"id": "r2", "author": "w", "timestamp": 30, "status": "x"}]
        old_p = write_tmp(self.dir, "old.json", old)
        new_p = write_tmp(self.dir, "new.json", old)
        result = self.run_cli(old_p, new_p, "--json")
        self.assertEqual(result.returncode, 0)  # 29s apart > default 15s: clean
        findings = json.loads(result.stdout)
        self.assertEqual(findings["counts"]["twin_pairs"], 0)
        result = self.run_cli(old_p, new_p, "--window", "30", "--json")
        self.assertEqual(result.returncode, 1)
        findings = json.loads(result.stdout)
        self.assertEqual(findings["counts"]["twin_pairs"], 1)
        self.assertEqual(findings["twin_pairs"][0]["delta_seconds"], 29)

    def test_missing_file_exit_two(self):
        result = self.run_cli("/nonexistent/old.json", "/nonexistent/new.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot read export", result.stderr)

    def test_malformed_exit_two(self):
        old = write_tmp(self.dir, "old.json", {"rows": "junk"})
        new = write_tmp(self.dir, "new.json", [])
        result = self.run_cli(old, new)
        self.assertEqual(result.returncode, 2)
        self.assertIn("ghostwalk:", result.stderr)

    def test_jsonl_inputs_work(self):
        old = write_tmp(self.dir, "old.jsonl",
                        '{"id": "r1", "author": "w", "timestamp": '
                        '1700000000, "status": "active"}\n'
                        '{"id": "r2", "author": "w", "timestamp": '
                        '1700000007, "status": "active"}\n', as_json=False)
        new = write_tmp(self.dir, "new.jsonl",
                        '{"id": "r1", "author": "w", "timestamp": '
                        '1700000000, "status": "active"}\n', as_json=False)
        result = self.run_cli(old, new)
        self.assertEqual(result.returncode, 1)
        self.assertIn("[ghost]", result.stdout)

    def test_csv_inputs_work(self):
        old = write_tmp(self.dir, "old.csv",
                        "id,author,timestamp,status\n"
                        "r1,w,1700000000,active\n"
                        "r2,w,1700000008,active\n", as_json=False)
        new = write_tmp(self.dir, "new.csv",
                        "id,author,timestamp,status\n"
                        "r1,w,1700000000,active\n", as_json=False)
        result = self.run_cli(old, new)
        self.assertEqual(result.returncode, 1)
        self.assertIn("[ghost]", result.stdout)

    def test_bare_invocation_needs_two_args(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)  # argparse usage error


if __name__ == "__main__":
    unittest.main()
