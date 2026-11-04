"""Offline tests: run the scraper against a local mock API (no internet needed).

    python -m unittest -v test_aquaculture_scraper.py
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


def fao_zip_bytes() -> bytes:
    """A small FAO-style release: data files plus code lists."""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Aquaculture_Quantity.csv",
                   "COUNTRY.UN_CODE,SPECIES.ALPHA_3_CODE,AREA.CODE,ENVIRONMENT.ALPHA_2_CODE,MEASURE,PERIOD,VALUE,STATUS\n"
                   "578,SAL,27,MA,Q_tlw,2023,1500000,A\n"
                   "784,GIT,51,MA,Q_tlw,2023,1200,E\n"
                   "784,GIT,51,MA,Q_tlw,2023,1200,E\n")  # duplicate row
        z.writestr("Aquaculture_Value.csv",
                   "COUNTRY.UN_CODE,SPECIES.ALPHA_3_CODE,AREA.CODE,ENVIRONMENT.ALPHA_2_CODE,MEASURE,PERIOD,VALUE,STATUS\n"
                   "578,SAL,27,MA,V_USD_1000,2023,9800000,A\n")
        z.writestr("CL_FI_COUNTRY_GROUPS.csv", "UN_Code,ISO2_Code,ISO3_Code,Name_En\n578,NO,NOR,Norway\n784,AE,ARE,United Arab Emirates\n")
        z.writestr("CL_FI_SPECIES_GROUPS.csv",
                   "3A_Code,Scientific_Name,Name_En,ISSCAAP_Group_En\nSAL,Salmo salar,Atlantic salmon,\"Salmons, trouts, smelts\"\n"
                   "GIT,Sparidae,Porgies,Miscellaneous coastal fishes\n")
        z.writestr("CL_FI_WATERAREA_GROUPS.csv", "Code,Name_En\n27,\"Atlantic, Northeast\"\n51,\"Indian Ocean, Western\"\n")
        z.writestr("CL_FI_ENVIRONMENT.csv", "Alpha_2_Code,Name_En\nMA,Marine\n")
    return buf.getvalue()


def eurostat_cube(geo):
    return {"version": "2.0", "class": "dataset", "id": ["species", "unit", "geo", "time"], "size": [2, 1, 1, 2],
            "dimension": {
                "species": {"category": {"index": {"SAL": 0, "MSM": 1}, "label": {"SAL": "Atlantic salmon", "MSM": "Mussels"}}},
                "unit": {"category": {"index": {"TLW": 0}, "label": {"TLW": "Tonnes live weight"}}},
                "geo": {"category": {"index": {geo: 0}, "label": {geo: f"Country {geo}"}}},
                "time": {"category": {"index": {"2022": 0, "2023": 1}}}},
            "value": {"0": 100.0, "1": 110.0, "3": 7.5}}


def openalex_work(i):
    return {"id": f"https://openalex.org/W{i}", "doi": f"https://doi.org/10.1/{i}", "display_name": f"Paper {i}",
            "publication_year": 2025, "publication_date": "2025-10-01", "type": "article",
            "primary_location": {"source": {"display_name": "Aquaculture"}, "landing_page_url": f"https://j.example/{i}"},
            "authorships": [{"author": {"display_name": "A. Berg"}, "institutions": [{"display_name": "NTNU"}],
                             "countries": ["NO"]},
                            {"author": {"display_name": "S. Ali"}, "institutions": [{"display_name": "UAEU"}],
                             "countries": ["AE"]}],
            "topics": [{"display_name": "Fish farming"}], "cited_by_count": i,
            "open_access": {"is_oa": True, "oa_url": None}}


WFS_CAPABILITIES = b"""<?xml version="1.0"?>
<WFS_Capabilities xmlns="http://www.opengis.net/wfs" version="1.0.0"><FeatureTypeList>
  <FeatureType><Name>emodnet:finfish</Name><Title>Finfish aquaculture</Title></FeatureType>
  <FeatureType><Name>emodnet:shellfish</Name><Title>Shellfish production</Title></FeatureType>
  <FeatureType><Name>emodnet:windfarms</Name><Title>Wind farms</Title></FeatureType>
</FeatureTypeList></WFS_Capabilities>"""

WFS_FEATURES = {
    "emodnet:finfish": [
        {"id": f"finfish.{i}", "geometry": {"type": "Point", "coordinates": [5.0 + i, 60.0]},
         "properties": {"name": f"Site {i}", "country": "NO", "species": "Salmo salar"}} for i in range(3)],
    "emodnet:shellfish": [
        {"id": "shellfish.1", "geometry": {"type": "Polygon", "coordinates": [[[0, 40], [2, 40], [2, 42], [0, 42]]]},
         "properties": {"COUNTRY": "ES"}}],
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
        if u.path == "/ssb":
            query = json.loads(body)
            if query.get("response", {}).get("format") != "json-stat2":
                return self._send(400, {"error": {"code": 400, "message": "bad format"}})
            return self._send(200, JSONSTAT)
        self._send(404, {"error": "not found"})

    def _send_raw(self, status, data, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
                          "_links": {"self": {"href": f"https://data.brreg.no/enhetsregisteret/api/enheter/{900000000 + i}"}},
                          "forretningsadresse": {"adresse": ["Kaia 1", "Bygg B"], "poststed": "BERGEN"}}
                         for i in range(3)][page * size:(page + 1) * size]
            body = {"page": {"size": size, "totalElements": 3, "totalPages": 2, "number": page}}
            if companies:
                body["_embedded"] = {"enheter": companies}
            return self._send(200, body)

        m = re.fullmatch(r"/weekly/(\d{4})/(\d{1,2})", u.path)
        if m:  # one URL per ISO week
            year, week = int(m.group(1)), int(m.group(2))
            if (year, week) == (2025, 1):
                return self._send(200, {"year": year, "week": week})  # no reports this week
            return self._send(200, {"year": year, "week": week, "localities": [
                {"localityNo": 12345, "name": "Testvika", "avgAdultFemaleLice": 0.21}]})

        if u.path == "/fao/Aquaculture_2026.1.0.zip":
            return self._send_raw(200, fao_zip_bytes(), "application/zip")

        if u.path == "/eurostat":  # one country per request; LU has no data
            geo = q["geo"]
            if geo == "LU":
                return self._send(400, {"error": {"status": "400", "label": "No data"}})
            if geo == "ES":
                return self._send(200, {**eurostat_cube("ES"), "value": {}})
            return self._send(200, eurostat_cube(geo))

        if u.path == "/openalex":
            cursor = q.get("cursor")
            if cursor == "*":
                return self._send(200, {"meta": {"count": 3, "next_cursor": "c2"}, "results": [
                    openalex_work(1), openalex_work(2)]})
            if cursor == "c2":
                return self._send(200, {"meta": {"count": 3, "next_cursor": None}, "results": [openalex_work(3)]})
            return self._send(400, {"error": "bad cursor"})

        if u.path == "/zenodo":
            page = int(q["page"])
            hits = [{"id": i, "doi": f"10.5281/zenodo.{i}",
                     "metadata": {"title": f"Dataset {i}", "publication_date": "2025-06-01",
                                  "creators": [{"name": "Hansen, K."}], "keywords": ["aquaculture", "salmon"],
                                  "resource_type": {"title": "Dataset"}, "license": {"id": "cc-by-4.0"}},
                     "links": {"self_html": f"https://zenodo.org/records/{i}"}}
                    for i in range(1, 4)][(page - 1) * 2: page * 2]
            return self._send(200, {"hits": {"total": 3, "hits": hits}})

        if u.path == "/gdelt":
            if q.get("mode") != "artlist" or q.get("format") != "json":
                return self._send_raw(200, b"Invalid query", "text/plain")
            return self._send(200, {"articles": [
                {"url": "https://news.example/a", "title": "New salmon farm approved", "seendate": "20251101T080000Z",
                 "domain": "news.example", "language": "English", "sourcecountry": "Norway"},
                {"url": "https://news.example/b", "title": "Oyster prices rise", "seendate": "20251102T080000Z",
                 "domain": "news.example", "language": "English", "sourcecountry": "France"}]})

        if u.path == "/wfs":
            if q.get("request") == "GetCapabilities":
                return self._send_raw(200, WFS_CAPABILITIES, "application/xml")
            layer, start, n = q["typeName"], int(q["startIndex"]), int(q["maxFeatures"])
            feats = WFS_FEATURES.get(layer, [])[start:start + n]
            return self._send(200, {"type": "FeatureCollection", "features": feats})

        if u.path == "/comtrade-error":
            return self._send(200, {"data": [], "error": "invalid parameter"})

        if u.path == "/comtrade":  # keyless preview: one year per request
            if "," in q["period"] or q.get("reporterCode") != "578,784":
                return self._send(400, {"error": "bad query"})
            if q["period"] != "2023":
                return self._send(200, {"count": 0, "error": "", "data": []})
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
                                   "Tid": "2025U02", "Tid_label": "2025U02", "value": 21000,
                                   "cell_key": "VareGrupper2=01|ContentsCode=Vekt|Tid=2025U02"})
        self.assertEqual(rows[7]["value"], 71.0)
        # sparse cube: only listed cells come back, in order, at the right coordinates
        sparse = aq.jsonstat_to_records({**JSONSTAT, "value": {"7": 9.5, "0": 5}})
        self.assertEqual([(r["VareGrupper2"], r["ContentsCode"], r["Tid"], r["value"]) for r in sparse],
                         [("01", "Vekt", "2025U01", 5), ("02", "Kilopris", "2025U02", 9.5)])
        dense_with_gaps = aq.jsonstat_to_records({**JSONSTAT, "value": [1, None, None, None, None, None, None, 2]})
        self.assertEqual([r["value"] for r in dense_with_gaps], [1, 2])

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
        first = res.records[0]
        self.assertEqual({k: first[k] for k in ("OBJECTID", "SITE", "longitude", "latitude")},
                         {"OBJECTID": 0, "SITE": "Site 0", "longitude": 10.0, "latitude": 63.0})
        layer = self.base + "/arcgis/FeatureServer/0"
        self.assertEqual(first["record_url"], layer + "/0")           # one link per feature
        self.assertTrue(first["source_url"].startswith(layer + "/query?"))
        self.assertIn("resultOffset=0", first["source_url"])
        self.assertIn("resultOffset=2", res.records[2]["source_url"])  # page 2's request
        self.assertEqual(cfg.reference_url, layer)

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
        self.assertEqual(res.records[0]["record_url"], "https://data.brreg.no/enhetsregisteret/api/enheter/900000000")
        self.assertIn("page=1", res.records[2]["source_url"])

    def test_week_pagination(self):
        cfg = self.preset("--source", "custom", "--url", self.base + "/weekly/{year}/{week}",
                          "--pagination", "week", "--records-path", "localities",
                          "--weeks-from", "2024-W52", "--weeks-to", "2025-W02", "--id-field", "")
        cfg = aq.replace(cfg, missing_records_is_empty=True, id_field=None)
        res = self.run_scrape(cfg)
        self.assertEqual(res.pages_failed, 0)
        self.assertEqual(MockAPI.hits.get("/weekly/2025/1"), 1)   # empty week did not stop the run
        self.assertEqual([(r["year"], r["week"]) for r in res.records], [(2024, 52), (2025, 2)])

    def test_parse_years(self):
        self.assertEqual(aq.parse_years("2019:2021,2023"), ["2019", "2020", "2021", "2023"])

    def test_ssb_post_jsonstat(self):
        cfg = self.preset("--source", "ssb_salmon", "--url", self.base + "/ssb")
        res = self.run_scrape(cfg)
        self.assertEqual(len(res.records), 8)
        self.assertEqual(res.records[2], {"period": "2025U01", "product_code": "01", "product": "Fresh salmon",
                                          "measure_code": "Kilopris", "measure": "Price (NOK/kg)", "value": 85.5,
                                          "source_url": self.base + "/ssb"})
        self.assertEqual(cfg.reference_url, "https://www.ssb.no/en/statbank/table/03024")

    def test_comtrade_keyless_one_request_per_year_end_to_end(self):
        with tempfile.TemporaryDirectory() as out:
            code = aq.main(["--source", "comtrade", "--url", self.base + "/comtrade", "--delay", "1",
                            "--years", "2022:2023", "--out-dir", out, "--basename", "trade"])
            self.assertEqual(code, 0)
            df = pd.read_csv(Path(out) / "trade.csv")
        self.assertEqual(MockAPI.hits["/comtrade"], 2)       # 2022 (empty) and 2023
        self.assertNotIn("Authorization", MockAPI.last_headers)
        self.assertEqual(list(df["reporter_iso3"]), ["ARE", "NOR"])
        self.assertEqual(df.loc[1, "trade_value_usd"], 8.0e9)
        self.assertIn("UN Comtrade", df.loc[0, "source"])   # provenance columns, always last
        self.assertEqual(list(df.columns[-5:]), aq.PROVENANCE_COLUMNS)
        self.assertIn("period=2023", df.loc[0, "source_url"])
        self.assertIn("reporterCode=578%2C784", df.loc[0, "source_url"])
        self.assertEqual(df.loc[0, "reference_url"], "https://comtradeplus.un.org/")

    def test_comtrade_error_string(self):
        cfg = self.preset("--source", "comtrade", "--url", self.base + "/comtrade-error", "--years", "2023")
        self.assertEqual(self.run_scrape(cfg).pages_failed, 1)

    def test_fao_zip(self):
        cfg = self.preset("--source", "fao_aquaculture", "--url", self.base + "/fao/Aquaculture_2026.1.0.zip")
        res = self.run_scrape(cfg)
        self.assertEqual(res.pages_failed, 0)
        self.assertEqual(len(res.records), 3)            # duplicate row removed
        self.assertEqual(res.duplicates, 1)
        nor = res.records[0]
        self.assertEqual((nor["country"], nor["country_iso3"], nor["species"], nor["scientific_name"]),
                         ("Norway", "NOR", "Atlantic salmon", "Salmo salar"))
        self.assertEqual((nor["area"], nor["environment"], nor["year"], nor["value"]),
                         ("Atlantic, Northeast", "Marine", "2023", "1500000"))
        self.assertEqual(res.records[1]["species_group"], "Miscellaneous coastal fishes")
        self.assertEqual(res.records[2]["measure"], "V_USD_1000")
        self.assertTrue(nor["source_url"].endswith("Aquaculture_2026.1.0.zip"))

    def test_fao_candidate_urls(self):
        import bulk_sources
        urls = bulk_sources.fao_zip_candidates(aq.date(2026, 11, 4))
        self.assertEqual(urls[0], "https://www.fao.org/fishery/static/Data/Aquaculture_2026.1.1.zip")
        self.assertIn("https://www.fao.org/fishery/static/Data/Aquaculture_2025.1.0.zip", urls)

    def test_fao_missing_zip_reported(self):
        cfg = self.preset("--source", "fao_aquaculture", "--url", self.base + "/fao/missing.zip")
        res = self.run_scrape(cfg)
        self.assertEqual((res.records, res.pages_failed), ([], 1))

    def test_eurostat_one_request_per_country(self):
        cfg = self.preset("--source", "eurostat", "--url", self.base + "/eurostat", "--geo", "NO,LU,ES,IS")
        res = self.run_scrape(cfg)
        self.assertEqual(MockAPI.hits["/eurostat"], 4)
        self.assertEqual(res.pages_failed, 1)              # LU failed; the run carried on
        self.assertEqual(len(res.records), 6)              # 3 filled cells each for NO and IS
        first = res.records[0]
        self.assertEqual((first["geo"], first["species"], first["year"], first["value"]),
                         ("NO", "Atlantic salmon", "2022", 100.0))
        self.assertEqual(first["cell_key"], "species=SAL|unit=TLW|geo=NO|time=2022")
        self.assertIn("geo=IS", res.records[-1]["source_url"])

    def test_openalex_cursor_from_response(self):
        cfg = self.preset("--source", "openalex", "--url", self.base + "/openalex", "--contact", "me@example.org")
        self.assertEqual(cfg.params["mailto"], "me@example.org")
        res = self.run_scrape(cfg)
        self.assertEqual(res.pages_failed, 0)
        self.assertEqual([r["title"] for r in res.records], ["Paper 1", "Paper 2", "Paper 3"])
        r = res.records[0]
        self.assertEqual(r["authors"], "A. Berg; S. Ali")
        self.assertEqual(r["countries"], "NO; AE")
        self.assertEqual(r["record_url"], "https://doi.org/10.1/1")
        self.assertIn("cursor=c2", res.records[2]["source_url"])

    def test_zenodo(self):
        cfg = self.preset("--source", "zenodo", "--url", self.base + "/zenodo", "--page-size", "2")
        res = self.run_scrape(cfg)
        self.assertEqual(len(res.records), 3)
        self.assertEqual(res.records[0]["keywords"], "aquaculture; salmon")
        self.assertEqual(res.records[0]["record_url"], "https://zenodo.org/records/1")

    def test_gdelt(self):
        cfg = self.preset("--source", "gdelt", "--url", self.base + "/gdelt", "--timespan", "8d")
        self.assertEqual(cfg.params["timespan"], "8d")
        res = self.run_scrape(cfg)
        self.assertEqual([r["source_country"] for r in res.records], ["Norway", "France"])
        self.assertEqual(res.records[0]["record_url"], "https://news.example/a")

    def test_emodnet_wfs_layers(self):
        cfg = self.preset("--source", "emodnet", "--url", self.base + "/wfs", "--page-size", "2")
        res = self.run_scrape(cfg)
        self.assertEqual(res.pages_failed, 0)
        self.assertEqual({r["layer"] for r in res.records}, {"emodnet:finfish", "emodnet:shellfish"})  # no wind farms
        self.assertEqual(len(res.records), 4)               # 3 finfish over 2 pages + 1 shellfish
        site = res.records[0]
        self.assertEqual((site["name"], site["country"], site["latitude"], site["longitude"]),
                         ("Site 0", "NO", 60.0, 5.0))
        shell = res.records[-1]
        self.assertEqual((shell["country"], shell["geometry_type"], shell["latitude"], shell["longitude"]),
                         ("ES", "Polygon", 41.0, 1.0))
        self.assertEqual(json.loads(shell["geometry"])["type"], "Polygon")   # full shape kept

    def test_worldbank_many_indicators_need_source(self):
        cfg = self.preset("--source", "worldbank", "--indicator", aq.WORLDBANK_FISHERY_INDICATORS, "--countries", "all")
        self.assertEqual(cfg.params["source"], "2")
        self.assertIn("/country/all/indicator/ER.FSH.AQUA.MT;ER.FSH.CAPT.MT;ER.FSH.PROD.MT", cfg.url)

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
            self.assertEqual(data[0]["reference_url"], self.base + "/farms")  # custom: the API itself
            self.assertIn("page=1", data[0]["source_url"])

    def test_run_all_needs_no_keys(self):
        """Every table builds with an environment that has no keys or tokens at all."""
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(aq, "main", return_value=0) as fake_main:
            code = run_all.main([])
        self.assertEqual(code, 0)
        self.assertEqual(fake_main.call_count, len(run_all.JOBS))

    def test_every_run_all_job_parses(self):
        for table, (job_args, _) in run_all.JOBS.items():
            cfg = aq.build_source(aq.parse_args(job_args))
            self.assertEqual(cfg.table, table, f"{table} preset has a different table name")


if __name__ == "__main__":
    unittest.main()
