"""Offline tests: run the scraper against a local mock API (no internet needed).

    python -m unittest -v test_aquaculture_scraper.py
"""
import json
import os
import re
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pandas as pd

import aquaculture_scraper as aq
import run_all

FARMS = [
    {"id": i, "name": f"Farm {i}", "location": {"country": "NO", "lat": 60 + i / 10, "lon": 5.0},
     "species": [{"name": "Salmo salar"}, {"name": "Salmo salar"}], "operator": {"company_name": f"Co {i % 3}"},
     "production": {"tonnes": 100 * i}, "year": 2024}
    for i in range(1, 8)
]

JSONSTAT = {  # 2 products x 2 measures x 2 weeks, in JSON-stat 2.0 form
    "version": "2.0", "class": "dataset",
    "id": ["VareGrupper2", "ContentsCode", "Tid"], "size": [2, 2, 2],
    "dimension": {
        "VareGrupper2": {"category": {"index": {"01": 0, "02": 1},
                                      "label": {"01": "Fresh salmon", "02": "Frozen salmon"}}},
        "ContentsCode": {"category": {"index": {"Vekt": 0, "Kilopris": 1},
                                      "label": {"Vekt": "Weight (tonnes)", "Kilopris": "Price (NOK/kg)"}}},
        "Tid": {"category": {"index": ["2025U01", "2025U02"]}},
    },
    "value": [20000, 21000, 85.5, 87.1, 1200, 1100, 70.2, 71.0],
}


class MockAPI(BaseHTTPRequestHandler):
    hits: dict = {}
    last_headers: dict = {}

    def log_message(self, *a):  # keep test output clean
        pass

    def _send(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _count(self, path):
        MockAPI.hits[path] = MockAPI.hits.get(path, 0) + 1
        MockAPI.last_headers = dict(self.headers)
        MockAPI.last_ua = self.headers.get("User-Agent")

    def do_POST(self):
        u = urlparse(self.path)
        self._count(u.path)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        if u.path == "/token":
            form = parse_qs(body)
            if form.get("client_id") == ["cid"] and form.get("client_secret") == ["secret"]:
                return self._send(200, {"access_token": "tok123", "token_type": "Bearer"})
            return self._send(401, {"error": "invalid_client"})
        if u.path == "/ssb":
            query = json.loads(body)
            if query.get("response", {}).get("format") != "json-stat2":
                return self._send(400, {"error": {"code": 400, "message": "bad format"}})
            return self._send(200, JSONSTAT)
        self._send(404, {"error": "not found"})

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self._count(u.path)

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

        if u.path.startswith("/wb/"):  # World Bank: [meta, rows]
            page = int(q["page"])
            rows = [{"countryiso3code": c, "country": {"value": n}, "date": "2023",
                     "indicator": {"id": "ER.FSH.AQUA.MT", "value": "Aquaculture production (metric tons)"},
                     "value": v, "unit": ""}
                    for c, n, v in [("NOR", "Norway", 1650000), ("ARE", "United Arab Emirates", 3500),
                                    ("NOR", "Norway", 1600000)]][(page - 1) * 2: page * 2]
            return self._send(200, [{"page": page, "pages": 2, "per_page": 2, "total": 3}, rows])

        if u.path == "/wb-error":
            return self._send(200, [{"message": [{"id": "120", "key": "Invalid value"}]}])

        if u.path == "/brreg":  # pages start at 0; _embedded is missing past the end
            page, size = int(q["page"]), int(q["size"])
            companies = [{"organisasjonsnummer": str(900000000 + i), "navn": f"Laks AS {i}",
                          "naeringskode1": {"kode": "03.211", "beskrivelse": "Sea farming"},
                          "antallAnsatte": 10 * i,
                          "forretningsadresse": {"adresse": ["Kaia 1", "Bygg B"], "poststed": "BERGEN"}}
                         for i in range(3)][page * size:(page + 1) * size]
            body = {"page": {"size": size, "totalElements": 3, "totalPages": 2, "number": page}}
            if companies:
                body["_embedded"] = {"enheter": companies}
            return self._send(200, body)

        m = re.fullmatch(r"/bw/(\d{4})/(\d{1,2})", u.path)
        if m:  # BarentsWatch: needs the bearer token
            if self.headers.get("Authorization") != "Bearer tok123":
                return self._send(401, {"error": "unauthorized"})
            year, week = int(m.group(1)), int(m.group(2))
            if (year, week) == (2025, 1):
                return self._send(200, {"year": year, "week": week})  # no reports this week
            return self._send(200, {"year": year, "week": week, "localities": [
                {"localityNo": 12345, "name": "Testvika", "avgAdultFemaleLice": 0.21,
                 "hasPd": False, "hasIla": False, "isFallow": False, "lat": 69.1, "lon": 18.2}]})

        if u.path == "/comtrade":
            if self.headers.get("Ocp-Apim-Subscription-Key") != "k":
                return self._send(200, {"data": [], "error": "missing subscription key"})
            return self._send(200, {"count": 2, "error": "", "data": [
                {"refYear": 2023, "reporterCode": 784, "reporterISO": "ARE", "reporterDesc": "United Arab Emirates",
                 "flowCode": "M", "flowDesc": "Import", "partnerCode": 0, "partnerDesc": "World",
                 "cmdCode": "0302", "cmdDesc": "Fish, fresh or chilled", "netWgt": 5.0e7, "primaryValue": 3.1e8},
                {"refYear": 2023, "reporterCode": 578, "reporterISO": "NOR", "reporterDesc": "Norway",
                 "flowCode": "X", "flowDesc": "Export", "partnerCode": 0, "partnerDesc": "World",
                 "cmdCode": "0302", "cmdDesc": "Fish, fresh or chilled", "netWgt": 1.1e9, "primaryValue": 8.0e9}]})

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

    def run_scrape(self, cfg, max_pages=20, session=None):
        return aq.scrape(cfg, session or self.session, max_pages=max_pages, delay=0)

    def preset(self, *argv):
        return aq.build_source(aq.parse_args(list(argv)))

    # --- helpers -------------------------------------------------------------------
    def test_get_path(self):
        rec = {"a": {"b": [{"c": 1}, {"c": 2}]}, "x": None}
        self.assertEqual(aq.get_path(rec, "a.b[1].c"), 2)
        self.assertEqual(aq.get_path(rec, "a.b[*].c"), [1, 2])
        self.assertIsNone(aq.get_path(rec, "a.b[5].c"))
        self.assertIsNone(aq.get_path(rec, "x.y.z"))
        self.assertIsNone(aq.get_path("not a dict", "a"))
        self.assertEqual(aq.get_path([{"pages": 3}, []], "[0].pages"), 3)

    def test_jsonstat_to_records(self):
        rows = aq.jsonstat_to_records(JSONSTAT)
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[1], {"VareGrupper2": "01", "VareGrupper2_label": "Fresh salmon",
                                   "ContentsCode": "Vekt", "ContentsCode_label": "Weight (tonnes)",
                                   "Tid": "2025U02", "Tid_label": "2025U02", "value": 21000})
        self.assertEqual(rows[7]["value"], 71.0)
        sparse = {**JSONSTAT, "value": {"0": 5}}
        self.assertEqual([r["value"] for r in aq.jsonstat_to_records(sparse)][:2], [5, None])

    def test_iso_weeks(self):
        self.assertEqual(aq.parse_iso_week("2025-W07"), (2025, 7))
        self.assertEqual(aq.next_iso_week(2020, 53), (2021, 1))
        self.assertEqual(aq.next_iso_week(2024, 52), (2025, 1))
        with self.assertRaises(SystemExit):
            aq.parse_iso_week("2025-W60")

    # --- original behaviour ----------------------------------------------------------
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
        cfg = self.preset("--source", "obis", "--url", self.base + "/obis", "--page-size", "2")
        res = self.run_scrape(cfg)
        self.assertEqual([r["record_id"] for r in res.records], [1, 2, 3, 4, 5])
        df = aq.build_dataframe(res.records)
        self.assertTrue(pd.isna(df.loc[df.record_id == 3, "longitude"]).all())  # 999 rejected

    def test_arcgis(self):
        cfg = self.preset("--source", "arcgis", "--url", self.base + "/arcgis/FeatureServer/0",
                          "--page-size", "2")
        res = self.run_scrape(cfg)
        self.assertEqual(len(res.records), 5)
        self.assertEqual(res.records[0], {"OBJECTID": 0, "SITE": "Site 0", "longitude": 10.0, "latitude": 63.0})

    def test_arcgis_error_in_200_body(self):
        cfg = aq.SourceConfig(name="t", url=self.base + "/arcgis-error/query", pagination="arcgis",
                              records_path="features")
        self.assertEqual(self.run_scrape(cfg).pages_failed, 3)

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

    # --- new sources -----------------------------------------------------------------
    def test_worldbank_stops_at_reported_page_count(self):
        cfg = self.preset("--source", "worldbank", "--url", self.base + "/wb/NOR;ARE/indicator/X",
                          "--page-size", "2")
        res = self.run_scrape(cfg)
        self.assertEqual(MockAPI.hits["/wb/NOR;ARE/indicator/X"], 2)  # no 3rd request
        self.assertEqual({r["country_iso3"] for r in res.records}, {"NOR", "ARE"})
        self.assertEqual(res.records[1]["value"], 3500)

    def test_worldbank_error_message(self):
        cfg = self.preset("--source", "worldbank", "--url", self.base + "/wb-error")
        res = self.run_scrape(cfg)
        self.assertEqual(res.records, [])
        self.assertGreater(res.pages_failed, 0)

    def test_brreg_zero_based_pages(self):
        cfg = self.preset("--source", "brreg", "--url", self.base + "/brreg", "--page-size", "2")
        res = self.run_scrape(cfg)
        self.assertEqual(len(res.records), 3)
        self.assertEqual(MockAPI.hits["/brreg"], 2)
        self.assertEqual(res.records[0]["address"], "Kaia 1; Bygg B")
        self.assertEqual(res.records[0]["org_number"], "900000000")

    def test_barentswatch_oauth_and_weeks(self):
        cfg = self.preset("--source", "barentswatch", "--weeks-from", "2024-W52", "--weeks-to", "2025-W02")
        cfg = replace(cfg, url=self.base + "/bw/{year}/{week}",
                      oauth=replace(cfg.oauth, token_url=self.base + "/token"))
        session = aq.create_session("TestBot/1.0")
        with mock.patch.dict(os.environ, {"BW_CLIENT_ID": "cid", "BW_CLIENT_SECRET": "secret"}):
            session.headers["Authorization"] = "Bearer " + aq.get_oauth_token(session, cfg.oauth)
        res = self.run_scrape(cfg, session=session)
        self.assertEqual(res.pages_failed, 0)
        self.assertEqual([(r["year"], r["week"]) for r in res.records], [(2024, 52), (2025, 2)])
        self.assertEqual(res.records[0]["site_number"], 12345)
        self.assertEqual(res.records[0]["avg_adult_female_lice"], 0.21)

    def test_oauth_bad_credentials(self):
        oauth = aq.OAuthConfig(token_url=self.base + "/token", client_id_env="X_ID", client_secret_env="X_SECRET")
        with mock.patch.dict(os.environ, {"X_ID": "cid", "X_SECRET": "wrong"}):
            with self.assertRaises(aq.FetchError):
                aq.get_oauth_token(self.session, oauth)
        with self.assertRaises(aq.FetchError):  # env vars not set
            aq.get_oauth_token(self.session, replace(oauth, client_id_env="NOPE_ID"))

    def test_ssb_post_jsonstat(self):
        cfg = self.preset("--source", "ssb_salmon", "--url", self.base + "/ssb")
        res = self.run_scrape(cfg)
        self.assertEqual(len(res.records), 8)
        self.assertEqual(res.records[2], {"period": "2025U01", "product_code": "01", "product": "Fresh salmon",
                                          "measure_code": "Kilopris", "measure": "Price (NOK/kg)", "value": 85.5})

    def test_comtrade_with_api_key_end_to_end(self):
        with tempfile.TemporaryDirectory() as out, mock.patch.dict(os.environ, {"COMTRADE_API_KEY": "k"}):
            code = aq.main(["--source", "comtrade", "--url", self.base + "/comtrade", "--delay", "1",
                            "--out-dir", out, "--basename", "trade"])
            self.assertEqual(code, 0)
            df = pd.read_csv(Path(out) / "trade.csv")
        self.assertEqual(list(df["reporter_iso3"]), ["ARE", "NOR"])
        self.assertEqual(df.loc[1, "trade_value_usd"], 8.0e9)
        self.assertIn("UN Comtrade", df.loc[0, "source"])   # provenance columns
        self.assertIn("retrieved_at", df.columns)

    def test_comtrade_error_string(self):
        cfg = self.preset("--source", "comtrade", "--url", self.base + "/comtrade")
        res = self.run_scrape(cfg)  # no key header -> API reports an error in the body
        self.assertEqual(res.pages_failed, 1)

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

    def test_run_all_skips_jobs_without_credentials(self):
        env = {k: v for k, v in os.environ.items() if k not in ("BW_CLIENT_ID", "BW_CLIENT_SECRET")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(aq, "main") as fake_main:
            code = run_all.main(["--only", "health_norway_weekly"])
        self.assertEqual(code, 0)
        fake_main.assert_not_called()

    def test_every_run_all_job_parses(self):
        for table, (job_args, _, _) in run_all.JOBS.items():
            cfg = aq.build_source(aq.parse_args(job_args))
            self.assertEqual(cfg.table, table, f"{table} preset has a different table name")


if __name__ == "__main__":
    unittest.main()
