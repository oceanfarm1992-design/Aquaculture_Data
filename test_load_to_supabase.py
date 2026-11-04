"""Offline tests for load_to_supabase.py against a mock Supabase REST API.

    python -m unittest -v test_load_to_supabase.py
"""
import json
import os
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import load_to_supabase as lts

MIGRATIONS = sorted(Path(__file__).parent.glob("supabase/migrations/*.sql"))


def migration_tables() -> dict[str, tuple[list[str], list[str]]]:
    """Apply every migration in order (on paper) and return table -> (primary key, columns)."""
    tables: dict[str, tuple[list[str], list[str]]] = {}
    for path in MIGRATIONS:
        sql = re.sub(r"--[^\n]*", "", path.read_text())
        for name, body in re.findall(r"create table if not exists public\.(\w+)\s*\((.*?)\n\);", sql, re.S):
            columns, pk = [], []
            for line in body.splitlines():
                line = line.strip().rstrip(",").strip()
                if not line:
                    continue
                m = re.match(r"primary key \(([^)]*)\)", line)
                if m:
                    pk = [c.strip() for c in m.group(1).split(",")]
                    continue
                col = line.split()[0]
                columns.append(col)
                if "primary key" in line:
                    pk = [col]
            tables[name] = (pk, columns)
        for name, body in re.findall(r"alter table public\.(\w+)\s+(add column.*?);", sql, re.S):
            tables[name][1].extend(re.findall(r"add column if not exists (\w+)", body))
        for name in re.findall(r"drop table if exists public\.(\w+)", sql):
            tables.pop(name, None)
    return tables


class MockSupabase(BaseHTTPRequestHandler):
    received: dict = {}       # table -> every row POSTed
    store: dict = {}          # table -> {key: row}, like the real table
    requests: list = []       # POSTs
    reads: list = []          # GETs
    no_hash_tables: set = set()   # tables created before the row_hash migration

    def log_message(self, *a):
        pass

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        MockSupabase.requests.append({"path": u.path, "query": q, "headers": dict(self.headers), "n": len(body)})
        if self.headers.get("apikey") != "sb_secret_test":
            return self._send(401, {"message": "Invalid API key"})
        table = u.path.rsplit("/", 1)[-1]
        if table == "missing_table":
            return self._send(404, {"code": "PGRST205", "message": "Could not find the table"})
        MockSupabase.received.setdefault(table, []).extend(body)
        key_cols = lts.TABLES[table][0]
        for row in body:
            MockSupabase.store.setdefault(table, {})[tuple(str(row[c]) for c in key_cols)] = row
        self._send(201, None)

    def do_GET(self):  # PostgREST select with order/limit/offset
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        table = u.path.rsplit("/", 1)[-1]
        MockSupabase.reads.append({"table": table, "offset": int(q["offset"]), "limit": int(q["limit"])})
        if self.headers.get("apikey") != "sb_secret_test":
            return self._send(401, {"message": "Invalid API key"})
        columns = q["select"].split(",")
        if table in MockSupabase.no_hash_tables and "row_hash" in columns:
            return self._send(400, {"code": "42703", "message": f"column {table}.row_hash does not exist"})
        rows = [MockSupabase.store.get(table, {})[k] for k in sorted(MockSupabase.store.get(table, {}))]
        page = rows[int(q["offset"]): int(q["offset"]) + int(q["limit"])]
        self._send(200, [{c: r.get(c) for c in columns} for r in page])

    def _send(self, status, body):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class LoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockSupabase)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        MockSupabase.received, MockSupabase.requests, MockSupabase.reads = {}, [], []
        MockSupabase.store, MockSupabase.no_hash_tables = {}, set()
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"SUPABASE_URL": self.url + "/",
                                                "SUPABASE_SERVICE_ROLE_KEY": "sb_secret_test"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write(self, table, rows):
        (self.data / f"{table}.json").write_text(json.dumps(rows), encoding="utf-8")

    def test_loader_matches_migration(self):
        not_scraped = lts.DATABASE_COLUMNS | {lts.HASH_COLUMN}
        expected = {t: (pk, [c for c in cols if c not in not_scraped])
                    for t, (pk, cols) in migration_tables().items()}
        self.assertEqual(expected, lts.TABLES)

    def test_every_table_records_first_seen_and_row_hash(self):
        for table, (_, cols) in migration_tables().items():
            self.assertIn("first_seen_at", cols, table)
            self.assertIn("row_hash", cols, table)

    def companies(self, run, name_of_2="Fjord Laks AS"):
        return [{"org_number": str(n), "company_name": name, "employees": 10.0 * n,
                 "retrieved_at": f"2026-11-0{run}T03:00:00Z",        # changes every run
                 "source_url": f"https://api.example/?page={run}"}    # can change every run
                for n, name in [(1, "Havbruk AS"), (2, name_of_2), (3, "Skjell AS")]]

    def test_second_identical_load_writes_nothing(self):
        self.write("companies_norway", self.companies(1))
        self.assertEqual(lts.main(["--data-dir", str(self.data), "--only", "companies_norway"]), 0)
        self.assertEqual(len(MockSupabase.received["companies_norway"]), 3)
        self.assertTrue(all(len(r["row_hash"]) == 32 for r in MockSupabase.received["companies_norway"]))

        self.write("companies_norway", self.companies(2))   # same data, new download time and page
        with self.assertLogs("load_to_supabase", "INFO") as logs:
            self.assertEqual(lts.main(["--data-dir", str(self.data), "--only", "companies_norway"]), 0)
        self.assertEqual(len(MockSupabase.received["companies_norway"]), 3)   # nothing more written
        self.assertTrue(any("0 new, 0 changed, 3 unchanged" in m for m in logs.output))

    def test_only_changed_rows_are_rewritten(self):
        self.write("companies_norway", self.companies(1))
        lts.main(["--data-dir", str(self.data), "--only", "companies_norway"])
        self.write("companies_norway", self.companies(2, name_of_2="Fjord Laks Holding AS")
                   + [{"org_number": "4", "company_name": "Ny Fisk AS"}])
        lts.main(["--data-dir", str(self.data), "--only", "companies_norway"])
        resent = MockSupabase.received["companies_norway"][3:]
        self.assertEqual(sorted(r["company_name"] for r in resent), ["Fjord Laks Holding AS", "Ny Fisk AS"])

    def test_existing_hashes_are_read_page_by_page(self):
        self.write("companies_norway", self.companies(1))
        lts.main(["--data-dir", str(self.data), "--only", "companies_norway"])
        MockSupabase.reads.clear()
        with mock.patch.object(lts, "READ_PAGE", 2):
            lts.main(["--data-dir", str(self.data), "--only", "companies_norway"])
        self.assertEqual([r["offset"] for r in MockSupabase.reads], [0, 2, 3])
        self.assertEqual(len(MockSupabase.received["companies_norway"]), 3)   # still nothing re-sent

    def test_table_without_row_hash_column_still_loads(self):
        MockSupabase.no_hash_tables = {"companies_norway"}
        self.write("companies_norway", self.companies(1))
        self.assertEqual(lts.main(["--data-dir", str(self.data), "--only", "companies_norway"]), 0)
        sent = MockSupabase.received["companies_norway"]
        self.assertEqual(len(sent), 3)
        self.assertNotIn("row_hash", sent[0])     # the old table has no such column

    def test_jsonb_columns_are_sent_as_objects(self):
        rows = lts.prepare_rows("facilities_europe", [{
            "layer": "emodnet:finfish", "feature_id": "finfish.1",
            "properties": '{"name": "Site 1"}', "geometry": '{"type": "Point", "coordinates": [5, 60]}'}])
        self.assertEqual(rows[0]["properties"], {"name": "Site 1"})
        self.assertEqual(rows[0]["geometry"]["type"], "Point")
        self.assertNotIn("first_seen_at", rows[0])

    def test_every_run_all_table_has_a_database_table(self):
        import run_all
        self.assertEqual(set(run_all.ALL_JOBS), set(lts.TABLES))

    def test_prepare_rows(self):
        rows = lts.prepare_rows("companies_norway", [
            {"org_number": "1", "company_name": "A", "employees": 10.0, "extra_col": "x"},
            {"org_number": "1", "company_name": "A2", "employees": float("nan")},   # same key: last wins
            {"org_number": None, "company_name": "no key"},
            {"org_number": "2", "company_name": "B", "bankrupt": False},
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["company_name"], "A2")
        self.assertIsNone(rows[0]["employees"])
        self.assertNotIn("extra_col", rows[0])
        self.assertEqual(set(rows[1]), set(lts.TABLES["companies_norway"][1]))
        self.assertEqual(lts.prepare_rows("companies_norway", [{"org_number": "3", "employees": 12.0}])[0]["employees"], 12)
        self.assertIsInstance(lts.prepare_rows("companies_norway", [{"org_number": "3", "employees": 12.0}])[0]["employees"], int)

    def test_upsert_batches_and_headers(self):
        self.write("production_by_country", [
            {"country_iso3": "NOR" if i % 2 else "ARE", "year": str(2000 + i // 2), "indicator_id": "ER.FSH.AQUA.MT",
             "value": float(i)} for i in range(5)])
        code = lts.main(["--data-dir", str(self.data), "--only", "production_by_country", "--batch-size", "2"])
        self.assertEqual(code, 0)
        self.assertEqual([r["n"] for r in MockSupabase.requests], [2, 2, 1])
        first = MockSupabase.requests[0]
        self.assertEqual(first["path"], "/rest/v1/production_by_country")
        self.assertEqual(first["query"]["on_conflict"], ["country_iso3,year,indicator_id"])
        self.assertIn("resolution=merge-duplicates", first["headers"]["Prefer"])
        self.assertNotIn("Authorization", first["headers"])   # sb_secret_ keys go in apikey only
        self.assertEqual(len(MockSupabase.received["production_by_country"]), 5)

    def test_legacy_jwt_key_also_sent_as_bearer(self):
        self.assertEqual(lts.auth_headers("eyJabc")["Authorization"], "Bearer eyJabc")

    def test_missing_files_are_skipped_and_failures_reported(self):
        self.write("prices_norway_salmon_export", [{"period": "2025U01", "product_code": "01",
                                                    "measure_code": "Vekt", "value": 1.0}])
        self.write("facilities_norway", [{"facility_name": "no site number"}])   # no usable key
        code = lts.main(["--data-dir", str(self.data)])
        self.assertEqual(code, 1)   # facilities had rows but none with a key
        self.assertEqual(len(MockSupabase.received["prices_norway_salmon_export"]), 1)
        self.assertNotIn("facilities_norway", MockSupabase.received)

    def test_wrong_key_fails(self):
        self.write("companies_norway", [{"org_number": "1"}])
        with mock.patch.dict(os.environ, {"SUPABASE_SERVICE_ROLE_KEY": "wrong"}):
            self.assertEqual(lts.main(["--data-dir", str(self.data)]), 1)

    def test_not_configured(self):
        with mock.patch.dict(os.environ, {"SUPABASE_URL": "", "SUPABASE_SERVICE_ROLE_KEY": ""}):
            self.assertEqual(lts.main(["--data-dir", str(self.data)]), 2)


if __name__ == "__main__":
    unittest.main()
