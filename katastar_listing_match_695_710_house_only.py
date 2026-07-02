"""
python -m venv venv
source venv/bin/activate

python -m pip install --upgrade pip
python -m pip install requests pandas geopandas shapely pyproj lxml pyogrio
"""

#!/usr/bin/env python3

from __future__ import annotations

import math
import re
import tempfile
import unicodedata
import zipfile
from collections import deque
from pathlib import Path
from urllib.parse import urljoin

import geopandas as gpd
import pandas as pd
import pyogrio
import requests
from lxml import etree


# ============================================================
# PARAMETRI — MIJENJAJ OVDJE
# ============================================================

ATOM_FEED_URL = "https://oss.uredjenazemlja.hr/oss/public/atom/atom_feed.xml"

# k.o. Sesvetski Kraljevec
TARGET_KO_NAME = "SESVETSKI KRALJEVEC"
TARGET_MBKO = "325392"

AREA_MIN_M2 = 695
AREA_MAX_M2 = 710

# "auto"     = koristi službeni atribut površine ako ga nađe, inače računa iz geometrije
# "official" = koristi samo atribut površine iz GML-a
# "geometry" = računa površinu iz geometrije
AREA_SOURCE = "auto"

HOUSE_ONLY = True

# Minimum overlap between parcel geometry and building/object geometry.
# This prevents false positives from tiny boundary touches.
MIN_BUILDING_INTERSECTION_AREA_M2 = 20

HOUSE_INCLUDE_KEYWORDS = [
    "KUCA",
    "KUĆA",
    "STAMB",
    "STAMBENA",
    "ZGRADA",
    "ZGRADE",
    "BUILDING",
    "HOUSE",
]

HOUSE_EXCLUDE_KEYWORDS = [
    "GARAZ",
    "GARAŽ",
    "GARAGE",
    "SUPA",
    "ŠUPA",
    "SHED",
    "SPREM",
    "SPREMIŠTE",
    "POMOC",
    "POMOĆ",
]

LISTING_MODE = True

LISTING_PARCEL_AREA_M2 = 705
LISTING_PARCEL_AREA_TOLERANCE_M2 = 10

# Anti-scam candidate narrowing notes:
# - This script identifies likely parcels based on public DKP geometry and attributes only.
# - It cannot prove ownership, sole ownership, bank-loanability, or seller authorization.
# - Final candidates must be verified manually in OSS and zemljišna knjiga.
#
# The ad claims:
# parcel = 705 m²
# house = 364 m²
# auxiliary building = 82 m²
# total building advertised = 446 m²
# yard = 441 m²
#
# 705 - 441 = approx. 264 m² built/covered footprint.
# This is more useful for cadastral geometry than advertised gross floor area.
LISTING_ADVERTISED_TOTAL_BUILDING_M2 = 446
LISTING_ADVERTISED_HOUSE_M2 = 364
LISTING_ADVERTISED_AUX_BUILDING_M2 = 82
LISTING_ADVERTISED_YARD_M2 = 441
LISTING_EXPECTED_BUILT_FOOTPRINT_M2 = 264
LISTING_BUILT_FOOTPRINT_TOLERANCE_M2 = 80
LISTING_EXPECTED_HOUSE_FOOTPRINT_M2 = 182
LISTING_HOUSE_FOOTPRINT_TOLERANCE_M2 = 45
LISTING_AUX_BUILDING_FOOTPRINT_TOLERANCE_M2 = 30
LISTING_YARD_TOLERANCE_M2 = 60

LISTING_EXPECTED_MIN_BUILDING_COUNT = 2
LISTING_MIN_SCORE_TO_EXPORT = 180

# Semicolon CSV is easier to open correctly in Croatian/European spreadsheet
# locales, and encoded Google Maps coordinates avoid a literal comma in URLs.
CSV_SEPARATOR = ";"

OUTPUT_DIR = Path("katastar_output")
OUTPUT_CSV = OUTPUT_DIR / "listing_candidates_sesvetski_kraljevec_695_710_house_only.csv"
OUTPUT_GPKG = OUTPUT_DIR / "listing_candidates_sesvetski_kraljevec_695_710_house_only.gpkg"

HTTP_TIMEOUT_SEC = 180
MAX_ATOM_PAGES_TO_SCAN = 350

# Limit output bloat in description column.
MAX_DESCRIPTION_ITEMS_PER_PARCEL = 30


# ============================================================
# TEXT / HTTP HELPERS
# ============================================================

def normalize_text(value: object) -> str:
    value = "" if value is None else str(value)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.upper()
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def clean_cell_value(value: object) -> str:
    if value is None:
        return ""

    try:
        if isinstance(value, float) and math.isnan(value):
            return ""
    except Exception:
        pass

    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    text = str(value).strip()

    if text.lower() in {"nan", "none", "null", "<na>"}:
        return ""

    return text


def target_matches(text: object) -> bool:
    blob = normalize_text(text)

    if TARGET_MBKO and TARGET_MBKO in blob:
        return True

    if TARGET_KO_NAME and normalize_text(TARGET_KO_NAME) in blob:
        return True

    return False


def download_bytes(url: str) -> bytes:
    headers = {
        "User-Agent": "katastar-area-description-filter/1.0",
    }

    response = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT_SEC)
    response.raise_for_status()
    return response.content


def parse_xml(xml_bytes: bytes) -> etree._Element:
    parser = etree.XMLParser(recover=True, huge_tree=True)
    return etree.fromstring(xml_bytes, parser=parser)


def collect_hrefs(root: etree._Element, base_url: str) -> list[str]:
    hrefs = []

    for href in root.xpath(".//@href"):
        href = str(href).strip()
        if href:
            hrefs.append(urljoin(base_url, href))

    text = etree.tostring(root, encoding="unicode", method="xml", with_tail=False)

    for url in re.findall(r"https?://[^\s\"'<>]+", text, flags=re.IGNORECASE):
        hrefs.append(url)

    seen = set()
    result = []

    for href in hrefs:
        if href not in seen:
            seen.add(href)
            result.append(href)

    return result


def is_zip_url(url: str) -> bool:
    return ".zip" in url.lower()


def is_xml_or_atom_url(url: str) -> bool:
    lower = url.lower()
    return (
        lower.endswith(".xml")
        or ".xml?" in lower
        or "atom" in lower
        or "feed" in lower
    )


# ============================================================
# ATOM ZIP DISCOVERY
# ============================================================

def find_ko_zip_url(start_url: str) -> str:
    """
    Finds the DKP ZIP for TARGET_MBKO / TARGET_KO_NAME from the ATOM feed.

    Defensive because the ATOM structure can contain nested XML links.
    """
    queue = deque([(start_url, 0)])
    visited = set()
    scanned = 0

    debug_dir = OUTPUT_DIR / "_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    while queue and scanned < MAX_ATOM_PAGES_TO_SCAN:
        url, depth = queue.popleft()

        if url in visited:
            continue

        visited.add(url)
        scanned += 1

        print(f"[ATOM] Scanning: {url}")

        try:
            xml_bytes = download_bytes(url)
        except Exception as exc:
            print(f"  Skipping, download failed: {exc}")
            continue

        try:
            root = parse_xml(xml_bytes)
        except Exception as exc:
            print(f"  Skipping, XML parse failed: {exc}")
            continue

        hrefs = collect_hrefs(root, url)
        entries = root.xpath("//*[local-name()='entry']")

        # 1) Best case: matching entry contains ZIP.
        for entry in entries:
            entry_text = " ".join(entry.itertext())
            entry_hrefs = collect_hrefs(entry, url)
            entry_blob = f"{entry_text} {' '.join(entry_hrefs)}"

            if not target_matches(entry_blob):
                continue

            for href in entry_hrefs:
                if is_zip_url(href):
                    return href

            for href in entry_hrefs:
                if is_xml_or_atom_url(href) and href not in visited:
                    queue.appendleft((href, depth + 1))

        # 2) Direct ZIP URL somewhere in this XML matching MBKO/name.
        for href in hrefs:
            if is_zip_url(href) and target_matches(href):
                return href

        # 3) If whole XML mentions target and has ZIP, use first ZIP.
        full_blob = etree.tostring(root, encoding="unicode", method="xml", with_tail=False)

        if target_matches(full_blob):
            zip_links = [href for href in hrefs if is_zip_url(href)]
            if zip_links:
                return zip_links[0]

        # 4) Follow XML links that contain the target.
        for href in hrefs:
            if is_xml_or_atom_url(href) and target_matches(href) and href not in visited:
                queue.appendleft((href, depth + 1))

        # 5) Fallback: from top-level feed, follow XML/Atom links.
        if depth == 0:
            for href in hrefs:
                if is_xml_or_atom_url(href) and href not in visited:
                    queue.append((href, depth + 1))

        debug_file = debug_dir / f"atom_scan_{scanned}.xml"

        try:
            debug_file.write_bytes(xml_bytes)
        except Exception:
            pass

    raise RuntimeError(
        "Nisam našao ZIP za zadanu katastarsku općinu.\n"
        f"TARGET_KO_NAME={TARGET_KO_NAME!r}, TARGET_MBKO={TARGET_MBKO!r}\n"
        f"Pogledaj debug XML-ove u: {debug_dir}"
    )


# ============================================================
# ZIP / GML READING
# ============================================================

def extract_zip(zip_bytes: bytes, target_dir: Path) -> list[Path]:
    zip_path = target_dir / "dkp.zip"
    zip_path.write_bytes(zip_bytes)

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            member_name = member.filename

            # Basic zip-slip protection.
            if member_name.startswith("/") or ".." in Path(member_name).parts:
                continue

            zf.extract(member, target_dir)

    return [
        path for path in target_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".gml", ".xml"}
    ]


def read_all_vector_layers(files: list[Path]) -> list[dict]:
    layers = []

    for file in files:
        try:
            layer_defs = pyogrio.list_layers(file)
            layer_names = [str(row[0]) for row in layer_defs]
        except Exception:
            layer_names = [None]

        for layer_name in layer_names:
            try:
                if layer_name:
                    gdf = gpd.read_file(file, layer=layer_name)
                else:
                    gdf = gpd.read_file(file)
            except Exception:
                continue

            if gdf.empty:
                continue

            if "geometry" not in gdf.columns:
                continue

            if gdf.geometry.isna().all():
                continue

            if gdf.crs is None:
                # Croatian cadastral DKP data is normally HTRS96 / Croatia TM.
                gdf = gdf.set_crs(epsg=3765)
            else:
                gdf = gdf.to_crs(epsg=3765)

            geom_types = sorted(set(gdf.geometry.geom_type.dropna().astype(str)))

            layers.append({
                "file": file,
                "layer_name": layer_name or "",
                "gdf": gdf,
                "geom_types": geom_types,
            })

    return layers


# ============================================================
# LAYER DETECTION
# ============================================================

def layer_blob(layer: dict) -> str:
    file = layer["file"]
    layer_name = layer["layer_name"]
    gdf = layer["gdf"]

    return normalize_text(
        f"{file.name} {layer_name} {' '.join(map(str, gdf.columns))} {' '.join(layer['geom_types'])}"
    )


def score_parcel_layer(layer: dict) -> int:
    gdf = layer["gdf"]
    blob = layer_blob(layer)

    score = 0

    if "POLYGON" in blob:
        score += 30

    strong_needles = [
        "CADASTRAL PARCEL",
        "KATASTARSKA CESTICA",
        "KATASTARSKE CESTICE",
        "CESTICA",
        "CESTICE",
        "PARCEL",
    ]

    for needle in strong_needles:
        if needle in blob:
            score += 50

    area_needles = [
        "POVRSINA",
        "POVRS",
        "AREA",
        "AREAVALUE",
    ]

    for needle in area_needles:
        if needle in blob:
            score += 15

    negative_needles = [
        "ZGRADA",
        "ZGRADE",
        "BUILDING",
        "NACIN UPORABE",
        "LAND USE",
        "GRANICA",
        "BOUNDARY",
    ]

    for needle in negative_needles:
        if needle in blob:
            score -= 40

    score += min(len(gdf), 20_000) // 100

    return score


def choose_parcel_layer(layers: list[dict]) -> dict:
    if not layers:
        raise RuntimeError("Nisam uspio pročitati nijedan GML/XML layer iz ZIP-a.")

    ranked = sorted(layers, key=score_parcel_layer, reverse=True)

    print("\nKandidati za sloj katastarskih čestica:")
    for layer in ranked[:15]:
        file = layer["file"]
        name = layer["layer_name"]
        gdf = layer["gdf"]

        print(
            f"  score={score_parcel_layer(layer):4d} "
            f"rows={len(gdf):6d} "
            f"geom={','.join(layer['geom_types']) or '-':20s} "
            f"file={file.name} "
            f"layer={name}"
        )

    selected = ranked[0]

    print("\nKoristim kao parcel layer:")
    print(f"  file:  {selected['file'].name}")
    print(f"  layer: {selected['layer_name']}")
    print(f"  rows:  {len(selected['gdf'])}")

    return selected


# ============================================================
# AREA / ID HELPERS
# ============================================================

def numeric_series(series: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")

    cleaned = (
        series.astype(str)
        .str.replace("\u00a0", "", regex=False)
        .str.replace(",", ".", regex=False)
        .str.extract(r"(-?\d+(?:\.\d+)?)", expand=False)
    )

    return pd.to_numeric(cleaned, errors="coerce")


def find_area_column(gdf: gpd.GeoDataFrame) -> str | None:
    candidates = []

    for col in gdf.columns:
        if col == "geometry":
            continue

        norm = normalize_text(col)

        if any(x in norm for x in ["POVRSINA", "POVRS", "AREA", "AREAVALUE", "SURFACE"]):
            values = numeric_series(gdf[col])
            valid_count = values.notna().sum()

            if valid_count > 0:
                candidates.append((col, valid_count))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[0][0]


def find_parcel_id_column(gdf: gpd.GeoDataFrame) -> str | None:
    preferred = [
        "BROJ CESTICE",
        "BROJCESTICE",
        "CESTICA",
        "CEST",
        "PARCEL",
        "LABEL",
        "NATIONALCADASTRALREFERENCE",
        "LOCALID",
        "IDENTIFIER",
        "ID",
    ]

    for needle in preferred:
        for col in gdf.columns:
            if col == "geometry":
                continue

            if needle in normalize_text(col):
                return col

    return None


def enrich_and_filter_parcels(parcels: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, str | None]:
    parcels = parcels.copy()

    parcels["area_geometry_m2"] = parcels.geometry.area

    official_area_col = find_area_column(parcels)

    if official_area_col:
        print(f"\nNašao mogući atribut službene površine: {official_area_col}")
        parcels["area_official_m2"] = numeric_series(parcels[official_area_col])
    else:
        print("\nNisam našao atribut službene površine; koristim geometrijsku površinu.")
        parcels["area_official_m2"] = pd.NA

    if AREA_SOURCE == "official":
        if not official_area_col:
            raise RuntimeError("AREA_SOURCE='official', ali nema prepoznatog atributa površine.")
        parcels["area_used_m2"] = parcels["area_official_m2"]

    elif AREA_SOURCE == "geometry":
        parcels["area_used_m2"] = parcels["area_geometry_m2"]

    elif AREA_SOURCE == "auto":
        parcels["area_used_m2"] = parcels["area_official_m2"].fillna(parcels["area_geometry_m2"])

    else:
        raise ValueError("AREA_SOURCE mora biti: 'auto', 'official' ili 'geometry'.")

    result = parcels[
        (parcels["area_used_m2"] >= AREA_MIN_M2)
        & (parcels["area_used_m2"] <= AREA_MAX_M2)
    ].copy()

    result = result.sort_values("area_used_m2")

    parcel_id_col = find_parcel_id_column(result)

    if parcel_id_col:
        print(f"Našao mogući stupac broja/ID-a čestice: {parcel_id_col}")
    else:
        print("Nisam siguran koji je stupac broj čestice; eksportiram sve atribute.")

    return result, parcel_id_col



# ============================================================
# HOUSE / BUILDING FILTER
# ============================================================

def row_text(row: pd.Series, source: str = "") -> str:
    values = [source]

    for value in row.values:
        text = clean_cell_value(value)
        if text:
            values.append(text)

    return normalize_text(" ".join(values))


def normalized_keywords(keywords: list[str]) -> list[str]:
    return [normalize_text(keyword) for keyword in keywords if normalize_text(keyword)]


def normalized_word_in_blob(blob: str, word: str) -> bool:
    word = normalize_text(word)

    if not word:
        return False

    return re.search(rf"(^| ){re.escape(word)}( |$)", blob) is not None


def house_candidate_columns(gdf: gpd.GeoDataFrame) -> list[str]:
    """
    Finds columns useful for deciding whether a candidate feature is a house,
    building, object, garage, storage structure, etc.
    """
    needles = [
        "OPIS",
        "DESCRIPTION",
        "DESCRIPT",
        "NAZIV",
        "IME",
        "VRSTA",
        "TYPE",
        "NAMJENA",
        "USE",
        "UPORABA",
        "NACIN",
        "KULTURA",
        "LANDUSE",
        "ZGRADA",
        "ZGRADE",
        "BUILDING",
        "OBJECT",
        "OBJEKT",
        "KUCA",
        "KUĆA",
        "HOUSE",
        "STAMB",
        "GARAZ",
        "GARAŽ",
        "GARAGE",
        "SUPA",
        "ŠUPA",
        "SHED",
        "SPREM",
        "POMOC",
        "POMOĆ",
        "LABEL",
        "SIFRA",
        "ŠIFRA",
        "CODE",
    ]

    cols = []

    for col in gdf.columns:
        if col == "geometry":
            continue

        norm = normalize_text(col)

        if any(needle in norm for needle in needles):
            cols.append(col)

    if cols:
        return cols[:20]

    fallback_cols = []

    for col in gdf.columns:
        if col == "geometry":
            continue

        if gdf[col].dtype == "object":
            fallback_cols.append(col)

    return fallback_cols[:12]


def looks_like_building_or_house_layer(layer: dict) -> bool:
    blob = layer_blob(layer)

    building_substrings = [
        "ZGRADA",
        "ZGRADE",
        "BUILDING",
        "OBJEKT",
        "KUCA",
        "HOUSE",
        "STAMB",
    ]
    building_words = [
        "OBJECT",
        "OBJECTS",
    ]

    has_building_concept = (
        any(needle in blob for needle in building_substrings)
        or any(normalized_word_in_blob(blob, needle) for needle in building_words)
    )

    # Land-use-only layers are too broad for HOUSE_ONLY. Keep a layer only when
    # its metadata clearly carries building/object/house concepts.
    if not has_building_concept:
        return False

    # A meaningful building/object overlap is area-based, so point/line layers
    # cannot satisfy MIN_BUILDING_INTERSECTION_AREA_M2 anyway. Avoid using them
    # as building candidates unless the geometry advertises polygon content.
    return "POLYGON" in blob


def build_house_building_features(
    layers: list[dict],
    parcel_layer: dict,
) -> list[dict]:
    building_layers = []
    parcel_file = parcel_layer["file"]
    parcel_layer_name = parcel_layer["layer_name"]

    for layer in layers:
        if layer["file"] == parcel_file and layer["layer_name"] == parcel_layer_name:
            continue

        if not looks_like_building_or_house_layer(layer):
            continue

        gdf = layer["gdf"].copy()

        if gdf.empty:
            continue

        try:
            valid_geometry = gdf.geometry.notna() & ~gdf.geometry.is_empty
            gdf = gdf[valid_geometry].copy()
        except Exception:
            gdf = gdf[gdf.geometry.notna()].copy()

        if gdf.empty:
            continue

        gdf = gdf.to_crs(epsg=3765)
        cols = house_candidate_columns(gdf)
        source = f"{layer['file'].name}::{layer['layer_name']}"

        building_layers.append({
            "source": source,
            "source_text": layer_blob(layer),
            "gdf": gdf,
            "cols": cols,
            "geom_types": layer["geom_types"],
        })

    return building_layers


def format_house_candidate(
    source: str,
    row: pd.Series,
    cols: list[str],
    intersection_area_m2: float,
) -> str:
    parts = [
        f"source={source}",
        f"intersect_area_m2={round(intersection_area_m2, 2)}",
    ]

    for col in cols[:12]:
        value = clean_cell_value(row.get(col))

        if value:
            parts.append(f"{col}={value}")

    return " | ".join(parts)


def summarize_house_filter_reasons(items: list[dict]) -> str:
    include_count = sum(1 for item in items if item.get("include_hit"))
    generic_count = len(items) - include_count
    total_area = round(sum(float(item["area"]) for item in items), 2)

    parts = [
        "meaningful_building_object_overlap",
        f"accepted_count={len(items)}",
        f"accepted_area_m2={total_area}",
    ]

    if include_count:
        parts.append(f"include_keyword_match_count={include_count}")

    if generic_count:
        parts.append(f"generic_overlap_count={generic_count}")

    return "; ".join(parts)


def collapse_house_candidates(items: list[dict], max_items: int = 8) -> str:
    sorted_items = sorted(items, key=lambda item: float(item["area"]), reverse=True)
    candidates = [str(item["candidate"]) for item in sorted_items[:max_items]]

    if len(sorted_items) > max_items:
        candidates.append(f"... truncated {len(sorted_items) - max_items} more candidate(s)")

    return " || ".join(candidates)


def classify_listing_building_candidate(candidate_text: str) -> str:
    text = normalize_text(candidate_text)

    if any(keyword in text for keyword in ["KUCA", "STAMB"]):
        return "house"

    if any(keyword in text for keyword in ["POMOC", "POMOCNA", "POMOCNI"]):
        return "auxiliary"

    if any(keyword in text for keyword in ["GARAZ", "SUPA", "SPREM"]):
        return "garage_storage"

    if any(keyword in text for keyword in ["GOSPODARSKA", "LJETNA KUHINJA"]):
        return "other_secondary"

    return "other_building"


def sum_candidate_area(items: list[dict], candidate_type: str) -> float:
    return round(
        sum(float(item["area"]) for item in items if item.get("listing_type") == candidate_type),
        2,
    )


def filter_parcels_with_houses_or_buildings(
    parcels: gpd.GeoDataFrame,
    layers: list[dict],
    parcel_layer: dict,
) -> gpd.GeoDataFrame:
    """
    Optional HOUSE_ONLY filter.

    This means "parcel has a meaningful building/object overlap". It is not
    legally authoritative, and it may include non-residential buildings when the
    source data does not classify building type. Manual verification in OSS/QGIS
    is still required before any purchase, zoning, ownership, or legal decision.
    """
    if not HOUSE_ONLY:
        return parcels

    before_count = len(parcels)
    parcels = parcels.copy()
    parcels["__parcel_rowid"] = range(len(parcels))
    parcels["has_house_or_building"] = False
    parcels["house_filter_reason"] = ""
    parcels["house_building_intersection_area_m2"] = pd.NA
    parcels["house_building_count"] = 0
    parcels["house_building_candidate"] = ""
    parcels["listing_house_footprint_m2"] = pd.NA
    parcels["listing_auxiliary_footprint_m2"] = pd.NA
    parcels["listing_other_building_footprint_m2"] = pd.NA
    parcels["listing_auxiliary_building_count"] = 0
    parcels["listing_house_candidate_count"] = 0

    building_layers = build_house_building_features(layers, parcel_layer)

    print("\nHOUSE_ONLY filter:")
    print(f"  parcels before: {before_count}")

    for building_layer in building_layers:
        source = building_layer["source"]
        geom_types = ",".join(building_layer["geom_types"]) or "-"
        print(f"    rows={len(building_layer['gdf']):6d} geom={geom_types:20s} source={source}")

    if not building_layers:
        print("  parcels after: 0")
        print(f"  removed: {before_count}")
        print("  candidate building layers used: 0")
        return parcels.iloc[0:0].drop(columns=["__parcel_rowid"], errors="ignore")

    include_keywords_norm = normalized_keywords(HOUSE_INCLUDE_KEYWORDS)
    exclude_keywords_norm = normalized_keywords(HOUSE_EXCLUDE_KEYWORDS)
    parcel_small = parcels[["__parcel_rowid", "geometry"]].copy()
    parcel_geoms = {
        int(rowid): geom
        for rowid, geom in zip(parcels["__parcel_rowid"], parcels.geometry)
    }
    accepted_by_parcel: dict[int, list[dict]] = {}

    for building_layer in building_layers:
        source = building_layer["source"]
        source_text = building_layer["source_text"]
        gdf = building_layer["gdf"]
        cols = [col for col in building_layer["cols"] if col in gdf.columns]

        try:
            joined = gpd.sjoin(
                parcel_small,
                gdf[cols + ["geometry"]],
                how="inner",
                predicate="intersects",
            )
        except Exception as exc:
            print(f"  House/building join failed for {source}: {exc}")
            continue

        if joined.empty:
            continue

        joined = joined.reset_index(drop=True)

        for _, joined_row in joined.iterrows():
            rowid = int(joined_row["__parcel_rowid"])
            right_index = joined_row.get("index_right")

            try:
                parcel_geom = parcel_geoms[rowid]
                candidate_row = gdf.loc[right_index]
                candidate_geom = candidate_row.geometry

                if parcel_geom is None or candidate_geom is None:
                    continue

                intersection_area_m2 = parcel_geom.intersection(candidate_geom).area
            except Exception:
                continue

            if intersection_area_m2 < MIN_BUILDING_INTERSECTION_AREA_M2:
                continue

            text_row = candidate_row[cols] if cols else pd.Series(dtype="object")
            row_candidate_text = row_text(text_row)
            source_candidate_text = normalize_text(f"{source} {source_text}")

            row_include_hit = any(keyword in row_candidate_text for keyword in include_keywords_norm)
            row_exclude_hit = any(keyword in row_candidate_text for keyword in exclude_keywords_norm)
            source_include_hit = any(keyword in source_candidate_text for keyword in include_keywords_norm)
            source_exclude_hit = any(keyword in source_candidate_text for keyword in exclude_keywords_norm)
            include_hit = row_include_hit or source_include_hit

            # Reject clearly garage/shed/storage-only candidates, but do not drop
            # the parcel if another non-excluded building candidate also overlaps.
            # Row-level exclusion wins over broad layer names like "ZGRADE".
            if row_exclude_hit and not row_include_hit:
                continue

            if source_exclude_hit and not source_include_hit and not row_include_hit:
                continue

            reason = "include_keyword_match" if include_hit else "building_object_overlap"
            candidate = format_house_candidate(source, candidate_row, cols, intersection_area_m2)
            listing_type = classify_listing_building_candidate(candidate)
            accepted_by_parcel.setdefault(rowid, []).append({
                "reason": reason,
                "area": round(float(intersection_area_m2), 2),
                "candidate": candidate,
                "include_hit": include_hit,
                "listing_type": listing_type,
            })

    keep_rowids = set(accepted_by_parcel)
    result = parcels[parcels["__parcel_rowid"].isin(keep_rowids)].copy()

    if not result.empty:
        result["has_house_or_building"] = True
        result["house_filter_reason"] = result["__parcel_rowid"].map(
            lambda rowid: summarize_house_filter_reasons(accepted_by_parcel[int(rowid)])
        )
        result["house_building_count"] = result["__parcel_rowid"].map(
            lambda rowid: len(accepted_by_parcel[int(rowid)])
        )
        result["house_building_intersection_area_m2"] = result["__parcel_rowid"].map(
            lambda rowid: round(
                sum(item["area"] for item in accepted_by_parcel[int(rowid)]),
                2,
            )
        )
        result["house_building_candidate"] = result["__parcel_rowid"].map(
            lambda rowid: collapse_house_candidates(accepted_by_parcel[int(rowid)])
        )
        result["listing_house_footprint_m2"] = result["__parcel_rowid"].map(
            lambda rowid: sum_candidate_area(accepted_by_parcel[int(rowid)], "house")
        )
        result["listing_auxiliary_footprint_m2"] = result["__parcel_rowid"].map(
            lambda rowid: sum_candidate_area(accepted_by_parcel[int(rowid)], "auxiliary")
        )
        result["listing_other_building_footprint_m2"] = result["__parcel_rowid"].map(
            lambda rowid: round(
                sum(
                    float(item["area"])
                    for item in accepted_by_parcel[int(rowid)]
                    if item.get("listing_type") in {"other_building", "other_secondary"}
                ),
                2,
            )
        )
        result["listing_auxiliary_building_count"] = result["__parcel_rowid"].map(
            lambda rowid: sum(
                1
                for item in accepted_by_parcel[int(rowid)]
                if item.get("listing_type") == "auxiliary"
            )
        )
        result["listing_house_candidate_count"] = result["__parcel_rowid"].map(
            lambda rowid: sum(
                1
                for item in accepted_by_parcel[int(rowid)]
                if item.get("listing_type") == "house"
            )
        )

    after_count = len(result)

    print(f"  parcels after: {after_count}")
    print(f"  removed: {before_count - after_count}")
    print(f"  candidate building layers used: {len(building_layers)}")

    return result.drop(columns=["__parcel_rowid"], errors="ignore")


# ============================================================
# PROPERTY DESCRIPTION JOIN
# ============================================================

def description_columns(gdf: gpd.GeoDataFrame) -> list[str]:
    """
    Finds likely useful columns that describe what exists on / inside a parcel:
    building, house, garage, yard, field, garden, land-use, etc.

    Exact DKP/GML column names can vary, so this is intentionally broad.
    """
    needles = [
        "OPIS",
        "DESCRIPTION",
        "DESCRIPT",
        "NAZIV",
        "IME",
        "VRSTA",
        "TYPE",
        "NAMJENA",
        "USE",
        "UPORABA",
        "UPORABE",
        "NACIN",
        "NACIN UPORABE",
        "KULTURA",
        "LAND",
        "LANDUSE",
        "ZGRADA",
        "ZGRADE",
        "BUILDING",
        "GARAZA",
        "GARAZ",
        "KUCA",
        "KUĆA",
        "DVORISTE",
        "DVORIŠTE",
        "VRT",
        "NJIVA",
        "LIVADA",
        "PASNJAK",
        "PAŠNJAK",
        "VOCNJAK",
        "VOĆNJAK",
        "ORANICA",
        "SUMA",
        "ŠUMA",
        "CESTA",
        "PUT",
        "LABEL",
        "SIFRA",
        "ŠIFRA",
        "CODE",
    ]

    cols = []

    for col in gdf.columns:
        if col == "geometry":
            continue

        norm = normalize_text(col)

        if any(needle in norm for needle in needles):
            cols.append(col)

    return cols


def looks_like_description_layer(layer: dict) -> bool:
    blob = layer_blob(layer)

    needles = [
        "NACIN UPORABE",
        "NACIN",
        "UPORABE",
        "UPORABA",
        "LAND USE",
        "LANDUSE",
        "KULTURA",
        "ZGRADA",
        "ZGRADE",
        "BUILDING",
        "OBJECT",
        "OBJEKT",
        "OPIS",
        "DESCRIPTION",
        "NAMJENA",
    ]

    return any(needle in blob for needle in needles)


def format_description_item(
    source: str,
    row: pd.Series,
    cols: list[str],
    intersection_area_m2: float | None = None,
) -> str:
    parts = []

    if source:
        parts.append(f"source={source}")

    if intersection_area_m2 is not None and not math.isnan(intersection_area_m2):
        parts.append(f"intersect_area_m2={round(intersection_area_m2, 2)}")

    for col in cols:
        value = clean_cell_value(row.get(col))

        if value:
            parts.append(f"{col}={value}")

    return " | ".join(parts)


def build_description_features(
    layers: list[dict],
    parcel_layer: dict,
) -> list[dict]:
    """
    Returns non-parcel layers that likely describe buildings / land use / objects.
    """
    desc_layers = []

    parcel_file = parcel_layer["file"]
    parcel_layer_name = parcel_layer["layer_name"]

    print("\nTražim description / land-use / building layere:")

    for layer in layers:
        if layer["file"] == parcel_file and layer["layer_name"] == parcel_layer_name:
            continue

        gdf = layer["gdf"].copy()

        if gdf.empty:
            continue

        cols = description_columns(gdf)
        is_desc = looks_like_description_layer(layer)

        if not cols and not is_desc:
            continue

        # If layer looks relevant but columns are weird, keep limited text-ish columns.
        if not cols:
            fallback_cols = []

            for col in gdf.columns:
                if col == "geometry":
                    continue

                if gdf[col].dtype == "object":
                    fallback_cols.append(col)

            cols = fallback_cols[:10]

        if not cols:
            continue

        gdf = gdf.to_crs(epsg=3765)

        source = f"{layer['file'].name}::{layer['layer_name']}"

        print(f"  rows={len(gdf):6d} source={source}")
        print(f"    cols={cols}")

        desc_layers.append({
            "source": source,
            "gdf": gdf,
            "cols": cols,
            "geom_types": layer["geom_types"],
        })

    return desc_layers


def add_property_description(
    parcels: gpd.GeoDataFrame,
    layers: list[dict],
    parcel_layer: dict,
) -> gpd.GeoDataFrame:
    """
    Adds one CSV-friendly column:
      property_description

    It spatially intersects filtered parcels with likely DKP layers:
      - land-use / way of use
      - buildings
      - objects
      - description-like layers

    This is intentionally broad because GML schemas can vary.
    """
    parcels = parcels.copy()
    parcels["__parcel_rowid"] = range(len(parcels))
    parcels["property_description"] = ""

    desc_layers = build_description_features(layers, parcel_layer)

    if not desc_layers:
        print("\nNisam našao nijedan description / land-use / building layer.")
        return parcels.drop(columns=["__parcel_rowid"], errors="ignore")

    descriptions_by_parcel: dict[int, list[str]] = {
        int(rowid): []
        for rowid in parcels["__parcel_rowid"].tolist()
    }

    parcel_small = parcels[["__parcel_rowid", "geometry"]].copy()

    for desc_layer in desc_layers:
        source = desc_layer["source"]
        gdf = desc_layer["gdf"]
        cols = desc_layer["cols"]

        try:
            joined = gpd.sjoin(
                parcel_small,
                gdf[cols + ["geometry"]],
                how="inner",
                predicate="intersects",
            )
        except Exception as exc:
            print(f"  Join failed for {source}: {exc}")
            continue

        if joined.empty:
            continue

        # For polygon layers, calculate actual intersection area where possible.
        # This helps distinguish e.g. tiny overlap vs. substantial land-use area.
        right_lookup = gdf[cols + ["geometry"]].copy()
        right_lookup["__right_index"] = right_lookup.index

        joined = joined.reset_index(drop=True)

        for _, joined_row in joined.iterrows():
            rowid = int(joined_row["__parcel_rowid"])
            right_index = joined_row.get("index_right")

            intersection_area_m2 = None

            try:
                parcel_geom = parcel_small.loc[
                    parcel_small["__parcel_rowid"] == rowid,
                    "geometry",
                ].iloc[0]

                right_geom = gdf.loc[right_index, "geometry"]

                if parcel_geom is not None and right_geom is not None:
                    if right_geom.geom_type in {
                        "Polygon",
                        "MultiPolygon",
                        "GeometryCollection",
                    }:
                        intersection_area_m2 = parcel_geom.intersection(right_geom).area
            except Exception:
                intersection_area_m2 = None

            item = format_description_item(
                source=source,
                row=joined_row,
                cols=cols,
                intersection_area_m2=intersection_area_m2,
            )

            if item:
                descriptions_by_parcel[rowid].append(item)

    def collapse_items(items: list[str]) -> str:
        unique = []

        seen = set()

        for item in items:
            item = item.strip()

            if not item:
                continue

            if item in seen:
                continue

            seen.add(item)
            unique.append(item)

        if len(unique) > MAX_DESCRIPTION_ITEMS_PER_PARCEL:
            kept = unique[:MAX_DESCRIPTION_ITEMS_PER_PARCEL]
            kept.append(f"... truncated {len(unique) - MAX_DESCRIPTION_ITEMS_PER_PARCEL} more item(s)")
            unique = kept

        return " || ".join(unique)

    parcels["property_description"] = parcels["__parcel_rowid"].map(
        lambda rowid: collapse_items(descriptions_by_parcel.get(int(rowid), []))
    )

    described_count = parcels["property_description"].str.len().gt(0).sum()
    print(f"\nParcele s property_description: {described_count}/{len(parcels)}")

    return parcels.drop(columns=["__parcel_rowid"], errors="ignore")


# ============================================================
# LISTING MATCH SCORING
# ============================================================

def extract_intersection_area_from_description_item(item: str) -> float | None:
    match = re.search(r"intersect_area_m2=([0-9]+(?:\.[0-9]+)?)", item)

    if not match:
        return None

    try:
        return float(match.group(1))
    except Exception:
        return None


def add_listing_land_use_metrics(df: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    df = df.copy()
    yard_areas = []

    for _, row in df.iterrows():
        yard_area = 0.0
        description = clean_cell_value(row.get("property_description"))

        for item in description.split(" || "):
            item_norm = normalize_text(item)

            if "DVORISTE" not in item_norm:
                continue

            area = extract_intersection_area_from_description_item(item)

            if area is None or area <= 1:
                continue

            yard_area += area

        yard_areas.append(round(yard_area, 2) if yard_area else pd.NA)

    df["listing_yard_area_m2"] = yard_areas
    return df


def numeric_cell(row: pd.Series, column: str) -> float | None:
    value = pd.to_numeric(pd.Series([row.get(column)]), errors="coerce").iloc[0]

    if pd.isna(value):
        return None

    return float(value)


def add_listing_match_score(df: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Adds a candidate ranking heuristic for one real-estate listing.

    The score is not proof. It only ranks parcels that deserve manual OSS/ZK
    verification against the ad details.
    """
    df = df.copy()
    scores = []
    reasons_by_row = []
    useful_description_keywords = normalized_keywords([
        "ZGRADA",
        "KUCA",
        "KUĆA",
        "POMOCNA",
        "POMOĆNA",
        "DVORISTE",
        "DVORIŠTE",
        "GARAZA",
        "GARAŽA",
    ])

    for _, row in df.iterrows():
        score = 0
        reasons = []

        area_used = numeric_cell(row, "area_used_m2")

        if area_used is not None:
            area_delta = abs(area_used - LISTING_PARCEL_AREA_M2)

            if area_delta <= 1:
                score += 50
                reasons.append(f"parcel_area_within_1m2(delta={round(area_delta, 2)})")
            elif area_delta <= 5:
                score += 35
                reasons.append(f"parcel_area_within_5m2(delta={round(area_delta, 2)})")
            elif area_delta <= LISTING_PARCEL_AREA_TOLERANCE_M2:
                score += 20
                reasons.append(f"parcel_area_within_tolerance(delta={round(area_delta, 2)})")
            elif area_delta <= 15:
                score += 10
                reasons.append(f"parcel_area_within_15m2(delta={round(area_delta, 2)})")

        footprint = numeric_cell(row, "house_building_intersection_area_m2")

        if footprint is not None:
            footprint_delta = abs(footprint - LISTING_EXPECTED_BUILT_FOOTPRINT_M2)

            if footprint_delta <= 25:
                score += 30
                reasons.append(f"built_footprint_within_25m2(delta={round(footprint_delta, 2)})")
            elif footprint_delta <= 45:
                score += 25
                reasons.append(f"built_footprint_within_45m2(delta={round(footprint_delta, 2)})")
            elif footprint_delta <= LISTING_BUILT_FOOTPRINT_TOLERANCE_M2:
                score += 10
                reasons.append(f"built_footprint_within_tolerance(delta={round(footprint_delta, 2)})")

        house_footprint = numeric_cell(row, "listing_house_footprint_m2")

        if house_footprint is not None:
            house_delta = abs(house_footprint - LISTING_EXPECTED_HOUSE_FOOTPRINT_M2)

            if house_delta <= 20:
                score += 40
                reasons.append(f"house_footprint_matches_two_floor_ad(delta={round(house_delta, 2)})")
            elif house_delta <= LISTING_HOUSE_FOOTPRINT_TOLERANCE_M2:
                score += 25
                reasons.append(f"house_footprint_plausible(delta={round(house_delta, 2)})")

        aux_footprint = numeric_cell(row, "listing_auxiliary_footprint_m2")

        if aux_footprint is not None:
            aux_delta = abs(aux_footprint - LISTING_ADVERTISED_AUX_BUILDING_M2)

            if aux_delta <= 15:
                score += 35
                reasons.append(f"auxiliary_building_matches_ad(delta={round(aux_delta, 2)})")
            elif aux_delta <= LISTING_AUX_BUILDING_FOOTPRINT_TOLERANCE_M2:
                score += 20
                reasons.append(f"auxiliary_building_plausible(delta={round(aux_delta, 2)})")

        yard_area = numeric_cell(row, "listing_yard_area_m2")

        if yard_area is not None:
            yard_delta = abs(yard_area - LISTING_ADVERTISED_YARD_M2)

            if yard_delta <= 20:
                score += 45
                reasons.append(f"yard_area_matches_ad(delta={round(yard_delta, 2)})")
            elif yard_delta <= LISTING_YARD_TOLERANCE_M2:
                score += 30
                reasons.append(f"yard_area_plausible(delta={round(yard_delta, 2)})")

        building_count = numeric_cell(row, "house_building_count")

        if building_count is not None:
            building_count = int(building_count)

            if building_count == LISTING_EXPECTED_MIN_BUILDING_COUNT:
                score += 20
                reasons.append(f"building_count_exactly_{LISTING_EXPECTED_MIN_BUILDING_COUNT}")
            elif building_count >= LISTING_EXPECTED_MIN_BUILDING_COUNT:
                score += 10
                reasons.append(f"building_count_at_least_{LISTING_EXPECTED_MIN_BUILDING_COUNT}")
            elif building_count == 1:
                score += 5
                reasons.append("single_building_candidate")

        house_count = numeric_cell(row, "listing_house_candidate_count") or 0
        aux_count = numeric_cell(row, "listing_auxiliary_building_count") or 0

        if int(house_count) >= 1 and int(aux_count) >= 1:
            score += 30
            reasons.append("has_house_and_auxiliary_building")

        description_text = normalize_text(
            f"{clean_cell_value(row.get('property_description'))} "
            f"{clean_cell_value(row.get('house_building_candidate'))}"
        )

        if any(keyword in description_text for keyword in useful_description_keywords):
            score += 10
            reasons.append("useful_description_keyword")

        scores.append(score)
        reasons_by_row.append("; ".join(reasons))

    df["listing_match_score"] = scores
    df["listing_match_reasons"] = reasons_by_row

    before_threshold = len(df)

    if LISTING_MIN_SCORE_TO_EXPORT is not None:
        df = df[df["listing_match_score"] >= LISTING_MIN_SCORE_TO_EXPORT].copy()
        print(
            "\nLISTING_MODE precision filter:"
            f"\n  minimum score: {LISTING_MIN_SCORE_TO_EXPORT}"
            f"\n  rows before:   {before_threshold}"
            f"\n  rows after:    {len(df)}"
            f"\n  removed:       {before_threshold - len(df)}"
        )

    sort_cols = [
        col for col in ["listing_match_score", "area_used_m2"]
        if col in df.columns
    ]

    if sort_cols:
        ascending = [False if col == "listing_match_score" else True for col in sort_cols]
        df = df.sort_values(sort_cols, ascending=ascending)

    return df


# ============================================================
# GOOGLE MAPS / EXPORT
# ============================================================

def add_google_maps_columns(result: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    result = result.copy()

    # Representative point: guaranteed to be inside the parcel polygon.
    # Better than centroid for Google Maps pins.
    map_points_3765 = result.geometry.representative_point()
    map_points_4326 = gpd.GeoSeries(map_points_3765, crs=result.crs).to_crs(epsg=4326)

    result["map_point_lon"] = map_points_4326.x
    result["map_point_lat"] = map_points_4326.y

    result["google_maps_url"] = result.apply(
        lambda row: (
            "https://www.google.com/maps/search/?api=1&query="
            f"{row['map_point_lat']:.8f}%2C{row['map_point_lon']:.8f}"
        ),
        axis=1,
    )

    return result


def export_results(
    result: gpd.GeoDataFrame,
    parcel_id_col: str | None,
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    result = add_google_maps_columns(result)

    # CSV: deliberately excludes geometry_wkt and coordinate helper columns requested for removal.
    csv_preferred_cols = [
        parcel_id_col,
        "listing_match_score",
        "listing_match_reasons",
        "area_used_m2",
        "area_official_m2",
        "area_geometry_m2",
        "google_maps_url",
        "listing_yard_area_m2",
        "listing_house_footprint_m2",
        "listing_auxiliary_footprint_m2",
        "listing_other_building_footprint_m2",
        "listing_house_candidate_count",
        "listing_auxiliary_building_count",
        "has_house_or_building",
        "house_filter_reason",
        "house_building_count",
        "house_building_intersection_area_m2",
        "house_building_candidate",
        "property_description",
    ]

    csv_preferred_cols = [
        col for col in csv_preferred_cols
        if col and col in result.columns
    ]

    excluded_from_csv = {
        "geometry",
        "geometry_wkt",
        "centroid_y_3765",
        "map_point_lat",
        "map_point_lon",
        "centroid_lat",
        "centroid_lon",
        "centroid_x_3765",
    }

    csv_other_cols = [
        col for col in result.columns
        if col not in csv_preferred_cols and col not in excluded_from_csv
    ]

    csv_df = pd.DataFrame(result[csv_preferred_cols + csv_other_cols])
    csv_df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8", sep=CSV_SEPARATOR)

    # GPKG: keep real geometry for QGIS inspection.
    gpkg_df = result.copy()

    for col in gpkg_df.columns:
        if col != "geometry" and gpkg_df[col].dtype == "object":
            gpkg_df[col] = gpkg_df[col].astype(str)

    gpkg_df.to_file(OUTPUT_GPKG, layer="parcele", driver="GPKG")

    print("\nExport complete:")
    print(f"  CSV:  {OUTPUT_CSV}")
    print(f"  GPKG: {OUTPUT_GPKG}")

    preview_cols = [
        parcel_id_col,
        "listing_match_score",
        "area_used_m2",
        "listing_yard_area_m2",
        "listing_house_footprint_m2",
        "listing_auxiliary_footprint_m2",
        "has_house_or_building",
        "house_filter_reason",
        "house_building_count",
        "house_building_intersection_area_m2",
        "google_maps_url",
        "property_description",
    ]

    preview_cols = [
        col for col in preview_cols
        if col and col in csv_df.columns
    ]

    #print("\nPreview:")
    #print(csv_df[preview_cols].head(30).to_string(index=False))


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    print("Parameters:")
    print(f"  TARGET_KO_NAME: {TARGET_KO_NAME}")
    print(f"  TARGET_MBKO:    {TARGET_MBKO}")
    print(f"  AREA RANGE:     {AREA_MIN_M2} - {AREA_MAX_M2} m²")
    print(f"  AREA_SOURCE:    {AREA_SOURCE}")
    print(f"  HOUSE_ONLY:     {HOUSE_ONLY}")
    print(f"  LISTING_MODE:   {LISTING_MODE}")

    print("\nFinding DKP ZIP from ATOM feed...")
    zip_url = find_ko_zip_url(ATOM_FEED_URL)

    print("\nFound ZIP:")
    print(f"  {zip_url}")

    print("\nDownloading ZIP...")
    zip_bytes = download_bytes(zip_url)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        print("\nExtracting ZIP...")
        files = extract_zip(zip_bytes, tmp_dir)

        print(f"Extracted GML/XML files: {len(files)}")
        for file in files[:20]:
            print(f"  {file.name}")

        print("\nReading vector layers...")
        layers = read_all_vector_layers(files)

        print(f"Readable vector layers: {len(layers)}")

        if not layers:
            raise RuntimeError("Nema čitljivih vector layera u ZIP-u.")

        parcel_layer = choose_parcel_layer(layers)
        parcels = parcel_layer["gdf"].copy()

        filtered, parcel_id_col = enrich_and_filter_parcels(parcels)

        print(f"\nUkupno čestica u parcel layeru: {len(parcels)}")
        print(f"Čestice {AREA_MIN_M2}-{AREA_MAX_M2} m²: {len(filtered)}")

        if filtered.empty:
            print("\nNema rezultata u zadanom rasponu.")
            return

        if HOUSE_ONLY:
            filtered = filter_parcels_with_houses_or_buildings(
                parcels=filtered,
                layers=layers,
                parcel_layer=parcel_layer,
            )

            if filtered.empty:
                print("\nNo parcels remain after HOUSE_ONLY filter.")
                return

        filtered = add_property_description(
            parcels=filtered,
            layers=layers,
            parcel_layer=parcel_layer,
        )

        if LISTING_MODE:
            filtered = add_listing_land_use_metrics(filtered)
            filtered = add_listing_match_score(filtered)

            if filtered.empty:
                print("\nNo parcels remain after LISTING_MODE precision filter.")
                return

        export_results(filtered, parcel_id_col)


if __name__ == "__main__":
    main()
