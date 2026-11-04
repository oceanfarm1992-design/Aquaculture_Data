"""Offline tests for species_sources.py against mock WoRMS, GBIF, NCBI and FishBase servers."""
import io
import json
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlparse

import pandas as pd

import aquaculture_scraper as aq
import species_sources as ss

SALMON = {"AphiaID": 127186, "scientificname": "Salmo salar", "authority": "Linnaeus, 1758",
          "status": "accepted", "rank": "Species", "valid_AphiaID": 127186, "valid_name": "Salmo salar",
          "kingdom": "Animalia", "phylum": "Chordata", "class": "Teleostei", "order": "Salmoniformes",
          "family": "Salmonidae", "genus": "Salmo", "isMarine": 1, "isBrackish": 1, "isFreshwater": 1,
          "isTerrestrial": 0, "isExtinct": None, "url": "https://www.marinespecies.org/aphia.php?p=taxdetails&id=127186",
          "citation": "Froese, R. and D. Pauly. Editors. (2024). FishBase."}
OLD_SHRIMP = {"AphiaID": 158367, "scientificname": "Litopenaeus vannamei", "status": "unaccepted",
              "rank": "Species", "valid_AphiaID": 377748, "valid_name": "Penaeus vannamei",
              "family": "Penaeidae", "genus": "Litopenaeus", "url": "https://www.marinespecies.org/aphia.php?p=taxdetails&id=158367"}


def parquet(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


FB_SPECIES = pd.DataFrame([
    {"SpecCode": 236, "Genus": "Salmo", "Species": "salar", "FBname": "Atlantic salmon", "Author": "Linnaeus, 1758",
     "FamCode": 76, "Fresh": -1, "Brack": -1, "Saltwater": -1, "DemersPelag": "benthopelagic", "AnaCat": "anadromous",
     "DepthRangeShallow": 0.0, "DepthRangeDeep": 210.0, "Length": 150.0, "LTypeMaxM": "SL", "Weight": 46800.0,
     "LongevityWild": 13.0, "Vulnerability": 54.6, "Importance": "highly commercial", "UsedforAquaculture": "commercial",
     "Dangerous": "harmless"},
    {"SpecCode": 4535, "Genus": "Lates", "Species": "calcarifer", "FBname": "Barramundi", "Author": "(Bloch, 1790)",
     "FamCode": 300, "Fresh": -1, "Brack": -1, "Saltwater": -1, "DemersPelag": "demersal", "AnaCat": "catadromous",
     "DepthRangeShallow": 10.0, "DepthRangeDeep": 40.0, "Length": 200.0, "LTypeMaxM": "TL", "Weight": 60000.0,
     "LongevityWild": None, "Vulnerability": 50.0, "Importance": "commercial", "UsedforAquaculture": None,
     "Dangerous": "harmless"},
    {"SpecCode": 9999, "Genus": "Notfarmed", "Species": "fishus", "FBname": "Wild fish", "FamCode": 76,
     "UsedforAquaculture": "never/rarely"},
])
FB_FILES = {
    "species": FB_SPECIES,
    "families": pd.DataFrame([{"FamCode": 76, "Family": "Salmonidae", "Order": "Salmoniformes", "Class": "Teleostei"},
                              {"FamCode": 300, "Family": "Latidae", "Order": "Perciformes", "Class": "Teleostei"}]),
    "estimate": pd.DataFrame([{"SpecCode": 236, "Troph": 4.4, "TempPrefMin": 2.5, "TempPrefMean": 8.6,
                               "TempPrefMax": 14.0, "K": 0.21}]),
    "popgrowth": pd.DataFrame([{"AutoCtr": 1, "SpecCode": 236, "Loo": 120.0, "K": 0.2, "to": -0.5, "Locality": "Norway"},
                               {"AutoCtr": 2, "SpecCode": 9999, "Loo": 30.0, "K": 0.9, "to": 0.0, "Locality": "x"}]),
    "poplw": pd.DataFrame([{"SpecCode": 4535, "a": 0.0123, "b": 3.01, "Locality": "Australia"}]),  # no AutoCtr
}


class Mock(BaseHTTPRequestHandler):
    hits: dict = {}

    def log_message(self, *a):
        pass

    def send(self, status, body=None, ctype="application/json"):
        data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        path, q = unquote(u.path), {k: v[0] for k, v in parse_qs(u.query).items()}
        Mock.hits[path] = Mock.hits.get(path, 0) + 1

        # ---- WoRMS ----
        m = re.fullmatch(r"/worms/AphiaRecordsByName/(.+)", path)
        if m:
            return self.send(200, {"Salmo salar": [SALMON], "Litopenaeus vannamei": [OLD_SHRIMP]}[m.group(1)]) \
                if m.group(1) in ("Salmo salar", "Litopenaeus vannamei") else self.send(204)
        m = re.fullmatch(r"/worms/AphiaExternalIDByAphiaID/(\d+)", path)
        if m:
            ids = {("127186", "ncbi"): ["8030"], ("127186", "fishbase"): ["236"]}.get((m.group(1), q["type"]))
            return self.send(200, ids) if ids else self.send(204)
        m = re.fullmatch(r"/worms/AphiaVernacularsByAphiaID/(\d+)", path)
        if m:
            if m.group(1) == "127186":
                return self.send(200, [{"vernacular": "Atlantic salmon", "language_code": "eng", "language": "English"},
                                       {"vernacular": "laks", "language_code": "nor", "language": "Norwegian"},
                                       {"vernacular": "Atlantic Salmon ", "language_code": "eng", "language": "English"}])
            return self.send(200, [{"vernacular": "whiteleg shrimp", "language_code": "eng", "language": "English"}])
        m = re.fullmatch(r"/worms/AphiaSynonymsByAphiaID/(\d+)", path)
        if m:
            if m.group(1) != "127186":
                return self.send(204)
            offset = int(q["offset"])
            total = [{"AphiaID": 9000 + i, "scientificname": f"Salmo synonym{i}", "status": "unaccepted",
                      "unacceptreason": "synonym"} for i in range(52)]
            return self.send(200, total[offset - 1: offset - 1 + 50])
        m = re.fullmatch(r"/worms/AphiaDistributionsByAphiaID/(\d+)", path)
        if m:
            return self.send(200, [
                {"locality": "Norwegian Sea", "locationID": "http://marineregions.org/mrgid/2353",
                 "establishmentMeans": "Native", "recordStatus": "valid", "qualityStatus": "checked"},
                {"locality": "Chile", "locationID": "http://marineregions.org/mrgid/2179",
                 "establishmentMeans": "Alien", "invasiveness": "invasive", "recordStatus": "valid"}])
        m = re.fullmatch(r"/worms/AphiaAttributesByAphiaID/(\d+)", path)
        if m:
            return self.send(200, [{"measurementTypeID": 1, "measurementType": "IUCN Red List Category",
                                    "measurementValue": "Least Concern (LC)", "qualitystatus": "checked",
                                    "children": [{"measurementType": "IUCN Assessment Year", "measurementValue": "2023"}]}])

        # ---- GBIF ----
        if path == "/gbif/species/match":
            if q["name"] == "Salmo salar":
                return self.send(200, {"usageKey": 7595433, "scientificName": "Salmo salar Linnaeus, 1758",
                                       "canonicalName": "Salmo salar", "rank": "SPECIES", "status": "ACCEPTED",
                                       "confidence": 99, "matchType": "EXACT"})
            return self.send(200, {"matchType": "NONE", "confidence": 100})
        if path == "/gbif/species/7595433/iucnRedListCategory":
            return self.send(200, {"category": "LEAST_CONCERN", "code": "LC"})
        if path == "/gbif/occurrence/search":
            if q["limit"] == "0":
                return self.send(200, {"count": 54321, "facets": [{"field": "COUNTRY", "counts": [
                    {"name": "NO", "count": 30000}, {"name": "CA", "count": 9000}]}]})
            offset, limit = int(q["offset"]), int(q["limit"])
            occ = [{"key": 4000000000 + i, "species": "Salmo salar", "taxonKey": 7595433,
                    "decimalLatitude": 60.0 + i / 10, "decimalLongitude": 5.0, "eventDate": "2024-06-01",
                    "year": 2024, "countryCode": "NO", "basisOfRecord": "HUMAN_OBSERVATION",
                    "datasetName": "Artsobservasjoner"} for i in range(5)][offset:offset + limit]
            return self.send(200, {"offset": offset, "limit": limit, "endOfRecords": offset + limit >= 5,
                                   "results": occ})

        # ---- NCBI ----
        m = re.fullmatch(r"/ncbi/genome/taxon/(.+)/dataset_report", path)
        if m:
            if m.group(1) != "Salmo salar":
                return self.send(200, {"total_count": 0})
            if q.get("page_token") == "p2":
                return self.send(200, {"total_count": 3, "reports": [
                    {"accession": "GCA_000233375.4", "organism": {"tax_id": 8030, "organism_name": "Salmo salar"},
                     "assembly_info": {"assembly_name": "ICSASG_v2", "assembly_level": "Chromosome"},
                     "assembly_stats": {"total_sequence_length": "2966890203"}}]})
            return self.send(200, {"total_count": 3, "next_page_token": "p2", "reports": [
                {"accession": "GCF_905237065.1", "paired_accession": "GCA_905237065.2",
                 "organism": {"tax_id": 8030, "organism_name": "Salmo salar"},
                 "assembly_info": {"assembly_name": "Ssal_v3.1", "assembly_level": "Chromosome",
                                   "refseq_category": "reference genome", "release_date": "2021-04-22"},
                 "assembly_stats": {"total_sequence_length": "2751484724", "contig_n50": 46430,
                                    "total_number_of_chromosomes": 29, "gc_percent": 44.0},
                 "annotation_info": {"name": "GCF_905237065.1-RS_2024_03", "provider": "NCBI RefSeq"}},
                {"accession": "GCA_905237065.2", "paired_accession": "GCF_905237065.1",
                 "organism": {"tax_id": 8030, "organism_name": "Salmo salar"},
                 "assembly_info": {"assembly_name": "Ssal_v3.1", "assembly_level": "Chromosome"}}]})

        # ---- FishBase on Hugging Face ----
        if path == "/hf/tree":
            files = [{"type": "file", "path": f"data/fb/v24.07/parquet/{t}.parquet"} for t in FB_FILES]
            files += [{"type": "file", "path": "data/fb/v23.05/parquet/species.parquet"},
                      {"type": "file", "path": "data/fb/v24.07/README.md"}]
            return self.send(200, files)
        m = re.fullmatch(r"/hf/files/data/fb/v24\.07/parquet/(\w+)\.parquet", path)
        if m and m.group(1) in FB_FILES:
            return self.send(200, parquet(FB_FILES[m.group(1)]), "application/octet-stream")

        self.send(404, {"error": "not found"})


class SpeciesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
        base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.patches = [mock.patch.object(ss, "WORMS", base + "/worms"), mock.patch.object(ss, "GBIF", base + "/gbif"),
                       mock.patch.object(ss, "NCBI", base + "/ncbi"), mock.patch.object(ss, "HF_DATASET", base + "/hf/tree"),
                       mock.patch.object(ss, "HF_FILES", base + "/hf/files/")]
        for p in cls.patches:
            p.start()
        cls.session = aq.create_session("TestBot/1.0")

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        cls.server.shutdown()

    def setUp(self):
        ss.clear_caches()
        Mock.hits = {}
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "species.txt").write_text("# my farm\nSalmo salar   # salmon\nLitopenaeus vannamei\n\nNosuch species\n")
        fao = [{"scientific_name": n, "species_code": c, "species": cn, "measure": "Q_tlw", "year": y, "value": v}
               for n, c, cn, v in [("Salmo salar", "SAL", "Atlantic salmon", 2.0e6),
                                   ("Lates calcarifer", "BSS", "Barramundi", 1.0e5),
                                   ("Cyprinidae", "FCY", "Cyprinids nei", 9.9e6),      # a group, not a species
                                   ("Oreochromis niloticus", "TLN", "Nile tilapia", 5.0e6)]
               for y in (2022, 2023)]
        fao += [{"scientific_name": "Gadus morhua", "species_code": "COD", "species": "Cod", "measure": "Q_tlw",
                 "year": 2010, "value": 9.0e9}]                                             # too old to count
        (d / "fao_aquaculture_production.json").write_text(json.dumps(fao))
        self.dir = d

    def tearDown(self):
        self.tmp.cleanup()

    def scrape(self, source, *extra):
        args = aq.parse_args(["--source", source, "--species-file", str(self.dir / "species.txt"),
                              "--top-species", "2", "--out-dir", str(self.dir), *extra])
        cfg = aq.build_source(args)
        res = aq.scrape(cfg, self.session, max_pages=10, delay=0)
        return cfg, res

    # ---- which species ----
    def test_species_list_file_first_then_fao_top(self):
        cfg = aq.build_source(aq.parse_args(["--source", "worms_taxonomy", "--species-file", str(self.dir / "species.txt"),
                                             "--top-species", "2", "--out-dir", str(self.dir)]))
        names = [s["scientific_name"] for s in ss.species_list(self.session, cfg, 0)]
        # species.txt order, then FAO's top 2 proper species (no 'Cyprinidae', no 2010 cod); salmon not repeated
        self.assertEqual(names, ["Salmo salar", "Litopenaeus vannamei", "Nosuch species", "Oreochromis niloticus"])

    # ---- WoRMS ----
    def test_worms_taxonomy(self):
        cfg, res = self.scrape("worms_taxonomy")
        self.assertEqual(res.pages_failed, 0)
        by = {r["queried_name"]: r for r in res.records}
        self.assertEqual(set(by), {"Salmo salar", "Litopenaeus vannamei"})   # unknown name skipped, not failed
        salmon = by["Salmo salar"]
        self.assertEqual((salmon["aphia_id"], salmon["family"], salmon["order_name"], salmon["authority"]),
                         ("127186", "Salmonidae", "Salmoniformes", "Linnaeus, 1758"))
        self.assertEqual((salmon["ncbi_taxid"], salmon["fishbase_id"], salmon["itis_tsn"]), ("8030", "236", None))
        self.assertEqual(salmon["fao_code"], "SAL")
        self.assertEqual(salmon["record_url"], SALMON["url"])
        shrimp = by["Litopenaeus vannamei"]
        self.assertEqual((shrimp["status"], shrimp["valid_aphia_id"], shrimp["valid_name"]),
                         ("unaccepted", "377748", "Penaeus vannamei"))

    def test_worms_lookups_are_shared_between_tables(self):
        self.scrape("worms_taxonomy")
        self.scrape("worms_common_names")
        self.assertEqual(Mock.hits["/worms/AphiaRecordsByName/Salmo salar"], 1)

    def test_worms_common_names_deduplicated(self):
        _, res = self.scrape("worms_common_names")
        salmon = [(r["common_name"], r["language"]) for r in res.records if r["aphia_id"] == "127186"]
        self.assertEqual(salmon, [("Atlantic salmon", "English"), ("laks", "Norwegian")])
        shrimp = [r for r in res.records if r["aphia_id"] == "377748"]   # looked up under the accepted id
        self.assertEqual(shrimp[0]["scientific_name"], "Penaeus vannamei")

    def test_worms_synonyms_paged(self):
        _, res = self.scrape("worms_synonyms")
        self.assertEqual(len([r for r in res.records if r["aphia_id"] == "127186"]), 52)
        self.assertEqual(Mock.hits["/worms/AphiaSynonymsByAphiaID/127186"], 2)

    def test_worms_distribution_native_and_introduced(self):
        _, res = self.scrape("worms_distribution")
        chile = next(r for r in res.records if r["aphia_id"] == "127186" and r["locality"] == "Chile")
        self.assertEqual((chile["establishment_means"], chile["invasiveness"]), ("Alien", "invasive"))

    def test_worms_attributes(self):
        _, res = self.scrape("worms_attributes")
        iucn = res.records[0]
        self.assertEqual((iucn["measurement_type"], iucn["measurement_value"]),
                         ("IUCN Red List Category", "Least Concern (LC)"))
        self.assertEqual(json.loads(iucn["details"])[0]["measurementValue"], "2023")

    # ---- GBIF ----
    def test_gbif_summary(self):
        _, res = self.scrape("gbif_species")
        self.assertEqual(len(res.records), 1)            # only salmon matches
        r = res.records[0]
        self.assertEqual((r["gbif_key"], r["iucn_code"], r["occurrence_count"]), ("7595433", "LC", 54321))
        self.assertEqual(json.loads(r["countries"]), {"NO": 30000, "CA": 9000})
        self.assertEqual(r["record_url"], "https://www.gbif.org/species/7595433")

    def test_gbif_occurrences_paged_and_capped(self):
        _, res = self.scrape("gbif_occurrences", "--occurrences-per-species", "4")
        self.assertEqual(len(res.records), 4)
        self.assertEqual(res.records[0]["record_url"], "https://www.gbif.org/occurrence/4000000000")
        self.assertIn("hasCoordinate=true", res.records[0]["source_url"])

    # ---- NCBI ----
    def test_ncbi_genomes_keep_refseq_copy_only(self):
        _, res = self.scrape("ncbi_genomes")
        self.assertEqual([r["accession"] for r in res.records], ["GCF_905237065.1", "GCA_000233375.4"])
        ref = res.records[0]
        self.assertEqual((ref["chromosomes"], ref["refseq_category"], ref["annotation_provider"]),
                         (29, "reference genome", "NCBI RefSeq"))
        self.assertIn("page_token=p2", res.records[1]["source_url"])

    # ---- FishBase ----
    def test_fishbase_profile(self):
        _, res = self.scrape("fishbase_profile")
        self.assertEqual(res.pages_failed, 0)
        # salmon (species.txt + farmed in FishBase); barramundi isn't flagged or listed; 9999 never farmed
        self.assertEqual([r["scientific_name"] for r in res.records], ["Salmo salar"])
        r = res.records[0]
        self.assertEqual((r["common_name"], r["family"], r["order_name"], r["max_length_cm"], r["habitat"],
                          r["migration"], r["trophic_level"], r["temp_pref_max_c"], r["used_for_aquaculture"]),
                         ("Atlantic salmon", "Salmonidae", "Salmoniformes", 150.0, "benthopelagic",
                          "anadromous", 4.4, 14.0, "commercial"))
        self.assertEqual(json.loads(r["data"])["LongevityWild"], 13.0)   # every FishBase column kept
        self.assertEqual(r["record_url"], "https://www.fishbase.se/summary/236")
        self.assertIn("/v24.07/", r["source_url"])                     # newest release

    def test_fishbase_records_filtered_to_chosen_species(self):
        species_file = self.dir / "species.txt"
        species_file.write_text(species_file.read_text() + "Lates calcarifer\n")
        _, res = self.scrape("fishbase_records", "--fishbase-tables", "popgrowth,poplw,nosuchtable")
        self.assertEqual(res.pages_failed, 0)            # a missing table is skipped, not a failure
        rows = {(r["fb_table"], r["scientific_name"]): r for r in res.records}
        self.assertEqual(set(rows), {("popgrowth", "Salmo salar"), ("poplw", "Lates calcarifer")})
        self.assertEqual(rows[("popgrowth", "Salmo salar")]["row_key"], "1")      # FishBase's own id
        self.assertEqual(len(rows[("poplw", "Lates calcarifer")]["row_key"]), 32)  # fingerprint when none
        self.assertEqual(json.loads(rows[("popgrowth", "Salmo salar")]["data"])["Loo"], 120.0)


if __name__ == "__main__":
    unittest.main()
