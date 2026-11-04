"""Tests for archive.py and fingerprint.py: unchanged tables are never stored twice."""
import json
import tempfile
import unittest
from pathlib import Path

import archive
from fingerprint import row_fingerprint, table_fingerprint


def rows(run, value=100.0, n=3):
    return [{"cell_key": f"578|SAL|{y}", "year": y, "value": value if y == 2020 else 1.0,
             "geometry": '{"type": "Point", "coordinates": [5, 60]}',
             "retrieved_at": f"2026-11-0{run}T03:00:00Z", "source_url": f"https://x/?page={run}"}
            for y in range(2020, 2020 + n)]


class FingerprintTests(unittest.TestCase):
    def test_download_details_do_not_change_the_fingerprint(self):
        self.assertEqual(table_fingerprint(rows(1)), table_fingerprint(rows(2)))

    def test_row_order_does_not_matter(self):
        self.assertEqual(table_fingerprint(rows(1)), table_fingerprint(list(reversed(rows(1)))))

    def test_data_changes_do(self):
        self.assertNotEqual(table_fingerprint(rows(1)), table_fingerprint(rows(1, value=101.0)))
        self.assertNotEqual(table_fingerprint(rows(1)), table_fingerprint(rows(1, n=4)))

    def test_number_and_json_forms_are_equivalent(self):
        a = {"value": 10.0, "geometry": '{"type": "Point"}'}
        b = {"value": 10, "geometry": {"type": "Point"}}
        self.assertEqual(row_fingerprint(a), row_fingerprint(b))


class ArchivePlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, **tables):
        for f in self.dir.glob("*.json"):
            f.unlink()
        for name, data in tables.items():
            (self.dir / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")

    def test_first_archive_stores_everything(self):
        self.write(fao=rows(1), news=[{"url": "a"}])
        manifest, changed = archive.plan(self.dir, {"tables": {}}, "data-1")
        self.assertEqual(changed, ["fao", "news"])
        self.assertEqual(manifest["tables"]["fao"]["release"], "data-1")
        self.assertEqual(manifest["tables"]["fao"]["rows"], 3)

    def test_unchanged_tables_point_to_the_older_release(self):
        self.write(fao=rows(1), news=[{"url": "a"}])
        first, _ = archive.plan(self.dir, {"tables": {}}, "data-1")
        self.write(fao=rows(2), news=[{"url": "a"}, {"url": "b"}])   # FAO same data, news grew
        second, changed = archive.plan(self.dir, first, "data-2")
        self.assertEqual(changed, ["news"])
        self.assertEqual(second["tables"]["fao"]["release"], "data-1")   # not stored again
        self.assertEqual(second["tables"]["news"]["release"], "data-2")
        self.assertEqual(second["tables"]["fao"]["last_checked"], "data-2")

    def test_nothing_changed_means_no_release(self):
        self.write(fao=rows(1))
        first, _ = archive.plan(self.dir, {"tables": {}}, "data-1")
        self.write(fao=rows(3))
        _, changed = archive.plan(self.dir, first, "data-2")
        self.assertEqual(changed, [])

    def test_tables_missing_from_a_run_keep_their_entry(self):
        self.write(fao=rows(1), news=[{"url": "a"}])
        first, _ = archive.plan(self.dir, {"tables": {}}, "data-1")
        self.write(news=[{"url": "a"}])          # e.g. a daily run, or FAO failed
        second, changed = archive.plan(self.dir, first, "data-2")
        self.assertEqual(changed, [])
        self.assertEqual(second["tables"]["fao"], first["tables"]["fao"])

    def test_cli_writes_manifest_changed_list_and_notes(self):
        self.write(fao=rows(1), news=[{"url": "a"}])
        outdir = tempfile.TemporaryDirectory()      # outside the data folder
        self.addCleanup(outdir.cleanup)
        out = Path(outdir.name)
        prev = out / "prev.json"
        archive.main(["--data-dir", str(self.dir), "--tag", "data-1",
                      "--out-manifest", str(prev), "--out-changed", str(out / "c1.txt"),
                      "--out-notes", str(out / "n1.md")])
        self.write(fao=rows(2), news=[{"url": "a"}, {"url": "b"}])
        archive.main(["--data-dir", str(self.dir), "--previous", str(prev), "--tag", "data-2",
                      "--out-manifest", str(out / "m2.json"), "--out-changed", str(out / "c2.txt"),
                      "--out-notes", str(out / "n2.md"), "--run-url", "https://run"])
        self.assertEqual((out / "c2.txt").read_text(), "news\n")
        notes = (out / "n2.md").read_text()
        self.assertIn("`news`: 2 rows", notes)
        self.assertIn("`fao`: in release `data-1`", notes)
        self.assertIn("https://run", notes)

    def test_missing_or_broken_previous_manifest_means_everything_is_new(self):
        self.assertEqual(archive.load_manifest(None), {"tables": {}})
        bad = self.dir / "bad.json"
        bad.write_text("not json")
        self.assertEqual(archive.load_manifest(str(bad)), {"tables": {}})


if __name__ == "__main__":
    unittest.main()
