"""
bulk_sources.py — sources that are not paged JSON APIs, used by aquaculture_scraper.py.

  fetch_fao_zip     FAO FishStat global aquaculture production: a yearly zip of CSV files
                    (every country × species × area × environment × year since 1950).
  fetch_wfs_layers  OGC WFS map services (e.g. EMODnet Human Activities): finds every layer
                    whose name or title matches a pattern and downloads all its features.

Each fetcher returns (pairs, failed_requests), where pairs is a list of
(record, source_url) tuples, and raises FetchError when nothing can be fetched at all.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any

import pandas as pd
import requests

import aquaculture_scraper as aq

log = logging.getLogger("aquaculture_scraper")

BIG_TIMEOUT = (15, 300)  # bulk downloads can take a few minutes


# --------------------------------------------------------------------------------------
# FAO FishStat bulk zip
# --------------------------------------------------------------------------------------
FAO_ZIP_PATTERN = "https://www.fao.org/fishery/static/Data/Aquaculture_{year}.1.{patch}.zip"


def fao_zip_candidates(today: date | None = None) -> list[str]:
    """FAO publishes one release a year (e.g. Aquaculture_2025.1.0.zip). Newest first."""
    year = (today or date.today()).year
    return [FAO_ZIP_PATTERN.format(year=y, patch=p) for y in range(year, year - 4, -1) for p in (1, 0)]


def _find_member(names: list[str], *patterns: str) -> str | None:
    for name in names:
        base = name.rsplit("/", 1)[-1].lower()
        if base.endswith(".csv") and all(re.search(p, base) for p in patterns):
            return name
    return None


def _col(df: pd.DataFrame, *parts: str, required: bool = True) -> str | None:
    """Find the first column whose upper-case name contains every part."""
    for col in df.columns:
        if all(p in col.upper() for p in parts):
            return col
    if required:
        raise aq.FetchError(f"no column containing {parts} (columns: {list(df.columns)})")
    return None


def _read_csv(zf: zipfile.ZipFile, name: str) -> pd.DataFrame:
    with zf.open(name) as fh:
        raw = fh.read()
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            df = pd.read_csv(io.BytesIO(raw), dtype=str, encoding=encoding, keep_default_na=False)
            df.columns = [c.strip() for c in df.columns]
            return df
        except UnicodeDecodeError:
            continue
    raise aq.FetchError(f"could not decode {name}")


def _codelist(zf, names, pattern, code_parts, columns: dict[str, tuple[str, ...]]) -> dict[str, dict]:
    """Load a code list CSV as {code: {out_name: value}}. Missing lists or columns are tolerated."""
    member = _find_member(names, pattern)
    if not member:
        log.warning("FAO zip has no code list matching %r; names will be empty.", pattern)
        return {}
    df = _read_csv(zf, member)
    code_col = None
    for parts in code_parts:
        code_col = _col(df, *parts, required=False)
        if code_col:
            break
    if not code_col:
        log.warning("Code list %s has no code column %s; names will be empty.", member, code_parts)
        return {}
    picked = {out: _col(df, *parts, required=False) for out, parts in columns.items()}
    return {row[code_col].strip(): {out: (row[c].strip() or None) if c else None for out, c in picked.items()}
            for _, row in df.iterrows()}


def fetch_fao_zip(session: requests.Session, cfg: "aq.SourceConfig", delay: float):
    candidates = [cfg.url] if cfg.url.lower().endswith(".zip") else fao_zip_candidates()
    content = url = None
    for i, candidate in enumerate(candidates):
        if i:
            aq.polite_sleep(delay)
        log.info("Trying FAO release %s ...", candidate)
        try:
            resp = session.get(candidate, timeout=BIG_TIMEOUT, headers={"Accept": "application/zip,*/*"})
        except requests.RequestException as exc:
            log.info("  not available (%s)", exc)
            continue
        if resp.status_code == 200 and resp.content[:2] == b"PK":
            content, url = resp.content, candidate
            break
        log.info("  not available (HTTP %s)", resp.status_code)
    if content is None:
        raise aq.FetchError("no FAO aquaculture zip found at the usual addresses; download page: "
                            "https://www.fao.org/fishery/en/collection/aquaculture — pass the zip "
                            "link with --url")
    log.info("Downloaded %s (%.1f MB)", url, len(content) / 1e6)

    zf = zipfile.ZipFile(io.BytesIO(content))
    names = zf.namelist()
    data_files = [n for n in (_find_member(names, r"quantity"), _find_member(names, r"value"))
                  if n and "cl_" not in n.lower().rsplit("/", 1)[-1]]
    if not data_files:
        raise aq.FetchError(f"no Quantity/Value CSV in the FAO zip (files: {names})")

    countries = _codelist(zf, names, r"country", [("UN_CODE",), ("UN",), ("CODE",)],
                          {"iso3": ("ISO3",), "name": ("NAME_EN",)})
    species = _codelist(zf, names, r"species", [("3A_CODE",), ("ALPHA_3",), ("3ALPHA",), ("CODE",)],
                        {"name": ("NAME_EN",), "scientific": ("SCIENTIFIC",),
                         "group": ("ISSCAAP", "EN"), "major": ("MAJOR", "GROUP")})
    areas = _codelist(zf, names, r"area", [("CODE",)], {"name": ("NAME_EN",)})
    envs = _codelist(zf, names, r"environment|prod.*source", [("ALPHA_2",), ("CODE",)], {"name": ("NAME_EN",)})

    pairs: list[tuple[dict, str]] = []
    for member in data_files:
        df = _read_csv(zf, member)
        c_country = _col(df, "COUNTRY")
        c_species = _col(df, "SPECIES")
        c_area = _col(df, "AREA", required=False)
        c_env = _col(df, "ENVIRONMENT", required=False) or _col(df, "SOURCE", required=False)
        c_measure = _col(df, "MEASURE", required=False)
        c_year = _col(df, "PERIOD", required=False) or _col(df, "YEAR")
        c_value = _col(df, "VALUE")
        c_status = _col(df, "STATUS", required=False)
        default_measure = "quantity" if "quantity" in member.lower() else "value"
        log.info("Reading %s: %d rows", member, len(df))
        for _, r in df.iterrows():
            code_c, code_s = r[c_country].strip(), r[c_species].strip()
            code_a = r[c_area].strip() if c_area else ""
            code_e = r[c_env].strip() if c_env else ""
            measure = (r[c_measure].strip() if c_measure else "") or default_measure
            c, s = countries.get(code_c, {}), species.get(code_s, {})
            record = {
                "country_un_code": code_c, "country_iso3": c.get("iso3"), "country": c.get("name"),
                "species_code": code_s, "species": s.get("name"), "scientific_name": s.get("scientific"),
                "species_group": s.get("group") or s.get("major"),
                "area_code": code_a or None, "area": areas.get(code_a, {}).get("name"),
                "environment_code": code_e or None, "environment": envs.get(code_e, {}).get("name"),
                "measure": measure, "year": r[c_year].strip(), "value": r[c_value].strip() or None,
                "status": (r[c_status].strip() or None) if c_status else None,
                "cell_key": "|".join([code_c, code_s, code_a, code_e, measure, r[c_year].strip()]),
            }
            pairs.append((record, url))
    return pairs, 0


# --------------------------------------------------------------------------------------
# OGC WFS layers (GeoServer, e.g. EMODnet)
# --------------------------------------------------------------------------------------
def _coords(geometry: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []

    def walk(c):
        if isinstance(c, (list, tuple)) and len(c) >= 2 and all(isinstance(v, (int, float)) for v in c[:2]):
            out.append((float(c[0]), float(c[1])))
        elif isinstance(c, (list, tuple)):
            for item in c:
                walk(item)

    if isinstance(geometry, dict):
        if geometry.get("type") == "GeometryCollection":
            for g in geometry.get("geometries") or []:
                out.extend(_coords(g))
        else:
            walk(geometry.get("coordinates"))
    return out


def centre(geometry: Any) -> tuple[float | None, float | None]:
    """(lat, lon): the point itself, or the mean of a shape's vertices (a rough centre)."""
    pts = _coords(geometry)
    if not pts:
        return None, None
    lon = sum(p[0] for p in pts) / len(pts)
    lat = sum(p[1] for p in pts) / len(pts)
    return lat, lon


def list_wfs_layers(session: requests.Session, url: str, pattern: str) -> list[tuple[str, str]]:
    resp = session.get(url, params={"service": "WFS", "version": "1.0.0", "request": "GetCapabilities"},
                       timeout=aq.DEFAULT_TIMEOUT, headers={"Accept": "application/xml,text/xml"})
    if resp.status_code >= 400:
        raise aq.FetchError(f"GetCapabilities failed with HTTP {resp.status_code}")
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise aq.FetchError(f"GetCapabilities was not valid XML: {exc}") from exc
    layers = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "FeatureType":
            continue
        fields = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in el}
        name, title = fields.get("Name", ""), fields.get("Title", "")
        if name and re.search(pattern, f"{name} {title}", re.IGNORECASE):
            layers.append((name, title))
    return layers


def fetch_wfs_layers(session: requests.Session, cfg: "aq.SourceConfig", delay: float):
    pattern = cfg.params.get("layer_pattern", r"aquacult")
    layers = list_wfs_layers(session, cfg.url, pattern)
    if not layers:
        raise aq.FetchError(f"no WFS layers match {pattern!r} at {cfg.url}")
    log.info("Found %d matching layers: %s", len(layers), ", ".join(n for n, _ in layers))

    pairs: list[tuple[dict, str]] = []
    failed = 0
    for name, title in layers:
        start = 0
        while True:
            aq.polite_sleep(delay)
            params = {"service": "WFS", "version": "1.0.0", "request": "GetFeature", "typeName": name,
                      "outputFormat": "application/json", "srsName": "EPSG:4326",
                      "maxFeatures": cfg.page_size, "startIndex": start}
            log.info("Fetching layer %s from feature %d...", name, start)
            try:
                body, page_url = aq.fetch_json(session, cfg.url, params, timeout=BIG_TIMEOUT)
            except aq.FetchError as exc:
                failed += 1
                log.error("Layer %s failed at feature %d: %s", name, start, exc)
                break
            features = body.get("features") if isinstance(body, dict) else None
            if not features:
                break
            for i, f in enumerate(features):
                if not isinstance(f, dict):
                    continue
                geometry = f.get("geometry")
                lat, lon = centre(geometry)
                pairs.append(({
                    "layer": name, "layer_title": title or None,
                    "feature_id": str(f.get("id") or f"{name}.{start + i}"),
                    "geometry_type": geometry.get("type") if isinstance(geometry, dict) else None,
                    "latitude": lat, "longitude": lon,
                    "properties": f.get("properties") or {}, "geometry": geometry,
                }, page_url))
            log.info("  %d features (running total %d)", len(features), len(pairs))
            if len(features) < cfg.page_size:
                break
            start += len(features)
    return pairs, failed
