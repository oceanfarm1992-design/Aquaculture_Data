"""Offline tests: run the scraper against a local mock API (no internet needed).

    python -m unittest -v test_aquaculture_scraper.py
"""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

import aquaculture_scraper as aq

FARMS = [
    {"id": i, "name": f"Farm {i}", "location": {"country": "NO", "lat": 60 + i / 10, "lon": 5.0},
     "species": [{"name": "Salmo salar"}, {"name": "Salmo salar"}], "operator": {"company_name": f"Co {i % 3}"},
     "production": {"tonnes": 100 * i}, "year": 2024}
    for i in range(1, 8)
]


class MockAPI(BaseHTTPRequestHandler):
    hits: dict = {}

    def log_message(self, *a):  # keep test output clean
        pass

    def _send(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        MockAPI.hits[u.path] = MockAPI.hits.get(u.path, 0) + 1
        MockAPI.last_ua = self.headers.get("User-Agent")

        if u.path == "/robots.txt":
            return self._send(200, b"User-agent: *\nDisallow: /private\n", "text/plain")

        if u.path == "/farms":  # page-number API; page 2 fails once with 503
            page, size = int(q["page"]), int(q["per_page"])
            if page == 2 and MockAPI.hits[u.path] == 2:
                return self._send(503, {"error": "busy"})
            chunk = FARMS[(page - 1) * size: page * size]
            if page == 2:
                chunk = chunk + ["garbage-record", {"id": 99}]  # bad record + sparse record
            return self._send(200, {"data": chunk if page <= 3 else []})

        if u.path == "/badjson":
            return self._send(200, b"<html>maintenance</html>", "text/html")

        if u.path == "/obis":  # cursor API
            after = int(q.get("after", 0))
            size = int(q["size"])
            rows = [{"id": i, "scientificName": "Sparus aurata", "decimalLatitude": 37.0,
                     "decimalLongitude": 999 if i == 3 else 23.0, "date_year": 2020}
                    for i in range(after + 1, min(after + size, 5) + 1)]
            return self._send(200, {"total": 5, "results": rows})

        if u.path == "/arcgis/FeatureServer/0/query":
            off, n = int(q["resultOffset"]), int(q["resultRecordCount"])
            feats = [{"attributes": {"OBJECTID": i, "SITE": f"Site {i}"},
                      "geometry": {"x": 10.0 + i, "y": 63.0}} for i in range(off, min(off + n, 5))]
            return self._send(200, {"features": feats, "exceededTransferLimit": off + n < 5})

        if u.path == "/arcgis-error/query":
            return self._send(200, {"error": {"code": 400, "message": "Invalid query"}})

        if u.path == "/ignores-paging":  # returns the same rows whatever the params
            return self._send(200, FARMS[:3])

        self._send(404, {"error": "not found"})


class ScraperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockAPI)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.session = aq.create_session("TestBot/1.0 (contact: test@example.org)")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        MockAPI.hits = {}

    def run_scrape(self, cfg, max_pages=20):
        return aq.scrape(cfg, self.session, max_pages=max_pages, delay=0)

    def test_get_path(self):
        rec = {"a": {"b": [{"c": 1}, {"c": 2}]}, "x": None}
        self.assertEqual(aq.get_path(rec, "a.b[1].c"), 2)
        self.assertEqual(aq.get_path(rec, "a.b[*].c"), [1, 2])
        self.assertIsNone(aq.get_path(rec, "a.b[5].c"))
        self.assertIsNone(aq.get_path(rec, "x.y.z"))
        self.assertIsNone(aq.get_path("not a dict", "a"))

    def test_page_pagination_retry_and_bad_records(self):
        fmap = {"farm_id": ["id"], "farm_name": ["name"], "country": ["location.country"],
                "latitude": ["location.lat"], "species": ["species[*].name"],
                "company": ["operator.company_name"], "production_tonnes": ["production.tonnes"]}
        cfg = aq.SourceConfig(name="t", url=self.base + "/farms", pagination="page",
                              records_path="data", page_size=3, field_map=fmap, id_field="id")
        res = self.run_scrape(cfg)
        self.assertEqual(res.pages_failed, 0)          # the 503 was retried transparently
        self.assertEqual(res.records_skipped, 1)       # "garbage-record"
        self.assertEqual(len(res.records), 8)          # 7 farms + sparse id 99
        self.assertEqual(res.records[0]["species"], "Salmo salar")  # list joined + de-duplicated
        sparse = next(r for r in res.records if r["farm_id"] == 99)
        self.assertIsNone(sparse["country"])           # missing nested key -> None, no crash

    def test_bad_json_is_reported_not_raised(self):
        cfg = aq.SourceConfig(name="t", url=self.base + "/badjson", pagination="page",
                              records_path="data")
        res = self.run_scrape(cfg)
        self.assertEqual(res.records, [])
        self.assertEqual(res.pages_failed, 3)          # stops after 3 consecutive failures

    def test_cursor_pagination_and_coordinate_cleaning(self):
        cfg = aq.SourceConfig(name="t", url=self.base + "/obis", pagination="cursor",
                              records_path="results", page_size=2, field_map=aq.OBIS_FIELDS,
                              id_field="id", total_path="total", size_param="size")
        res = self.run_scrape(cfg)
        self.assertEqual([r["record_id"] for r in res.records], [1, 2, 3, 4, 5])
        df = aq.build_dataframe(res.records)
        self.assertTrue(pd.isna(df.loc[df.record_id == 3, "longitude"]).all())  # 999 rejected

    def test_arcgis(self):
        args = aq.parse_args(["--source", "arcgis", "--url", self.base + "/arcgis/FeatureServer/0",
                              "--page-size", "2"])
        res = self.run_scrape(aq.build_source(args))
        self.assertEqual(len(res.records), 5)
        self.assertEqual(res.records[0], {"OBJECTID": 0, "SITE": "Site 0", "longitude": 10.0, "latitude": 63.0})

    def test_arcgis_error_in_200_body(self):
        cfg = aq.SourceConfig(name="t", url=self.base + "/arcgis-error/query", pagination="arcgis",
                              records_path="features")
        res = self.run_scrape(cfg)
        self.assertEqual(res.pages_failed, 3)

    def test_loop_guard_when_api_ignores_pagination(self):
        cfg = aq.SourceConfig(name="t", url=self.base + "/ignores-paging", pagination="range",
                              records_path=None, page_size=3, id_field="id")
        res = self.run_scrape(cfg)
        self.assertEqual(len(res.records), 3)
        self.assertEqual(MockAPI.hits["/ignores-paging"], 2)

    def test_robots(self):
        ua = "TestBot/1.0"
        self.assertTrue(aq.robots_allows(self.session, self.base + "/farms", ua))
        self.assertFalse(aq.robots_allows(self.session, self.base + "/private/x", ua))

    def test_end_to_end_cli_writes_csv_and_json(self):
        with tempfile.TemporaryDirectory() as out:
            code = aq.main(["--source", "custom", "--url", self.base + "/farms", "--page-size", "3",
                            "--raw", "--delay", "1", "--contact", "test@example.org", "--out-dir", out])
            self.assertEqual(code, 0)
            self.assertIn("contact: test@example.org", MockAPI.last_ua)
            csv_df = pd.read_csv(Path(out) / "aquaculture_data.csv", encoding="utf-8")
            data = json.loads((Path(out) / "aquaculture_data.json").read_text(encoding="utf-8"))
            self.assertEqual(len(csv_df), 8)
            self.assertEqual(len(data), 8)
            self.assertIn("location.lat", csv_df.columns)  # json_normalize flattening
            self.assertIn("operator.company_name", data[0])


if __name__ == "__main__":
    unittest.main()
