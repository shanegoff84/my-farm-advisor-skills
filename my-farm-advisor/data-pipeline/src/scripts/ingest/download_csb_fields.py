"""Ingest real farm-field polygons from USDA CSB via FIBOA's hosted GeoParquet.

Reads the FIBOA "us-usda-cropland" collection (16.19M field polygons, derived
from USDA NASS Crop Sequence Boundaries) hosted on Source Cooperative and
extracts the largest fields inside a lon/lat window WITHOUT downloading the
whole file: row-group statistics on the flattened bbox columns prune the scan
(648 row groups), then only candidate groups are read.

The parquet stores geometry + bbox in the file's projected CRS (NAD83 Albers
Conic Equal Area, read live from the GeoParquet key/value metadata). The
window is transformed degrees -> file CRS with pyproj before pruning, which
is required: comparing degrees against meters silently matches nothing.

Why pyarrow and not DuckDB: this file dictionary-encodes the bbox struct,
which triggers a DuckDB filter-pushdown internal error (Vector::Reference
type mismatch). Row-group stats via pyarrow avoid the bug and are explicit.

OSM license note: FIBOA collection CC-BY-4.0; underlying CSB is USDA public
data. Provenance (URL, row groups touched, accessed_at) is journalled.

Usage:
  export DATA_PIPELINE_DATA_ROOT=... AG_GROWER_SLUG=... AG_FARM_SLUG=...
  python download_csb_fields.py --bbox S W N E --min-area-ha 40 \
      --field-prefix ia-r --max-fields 3
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from paths import DATA_ROOT  # pyright: ignore[reportMissingImports]

import fsspec  # pyright: ignore[reportMissingImports]
import geopandas as gpd  # pyright: ignore[reportMissingImports]
import pandas as pd  # pyright: ignore[reportMissingImports]
import pyarrow.parquet as pq  # pyright: ignore[reportMissingImports]
from pyproj import CRS, Transformer
from shapely import wkb  # pyright: ignore[reportMissingImports]

URL = "https://data.source.coop/fiboa/us-usda-cropland/us_usda_cropland.parquet"
# Physical leaf columns for the bbox struct (verified against live stats 2026-11-03)
LEAF = {"bbox.xmin": 7, "bbox.ymin": 8, "bbox.xmax": 9, "bbox.ymax": 10}

_STATE_FIPS: dict[str, str] | None = None
_COUNTY_FIPS: dict[tuple[str, str], str] | None = None
_WARNED_STATES: set[str] = set()


def _state_fips_lookup() -> dict[str, str]:
    """Map USPS state code -> two-digit FIPS from the shared geoadmin states."""
    global _STATE_FIPS
    if _STATE_FIPS is not None:
        return _STATE_FIPS
    mapping: dict[str, str] = {}
    try:
        from paths import shared_geoadmin_states_dir  # noqa: PLC0415

        path = shared_geoadmin_states_dir() / "states_usa.geojson"
        payload = json.loads(path.read_text(encoding="utf-8"))
        for feature in payload.get("features", []):
            props = feature.get("properties") or {}
            code = str(props.get("state_code") or "").strip().upper()
            fips = str(props.get("state_fips") or "").strip().zfill(2)
            if code and fips:
                mapping[code] = fips
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  warning: could not read geoadmin states for state_fips ({exc})")
    _STATE_FIPS = mapping
    return mapping


def _county_fips_lookup() -> dict[tuple[str, str], str]:
    """Map (state_fips, county name) -> three-digit county FIPS."""
    global _COUNTY_FIPS
    if _COUNTY_FIPS is not None:
        return _COUNTY_FIPS
    mapping: dict[tuple[str, str], str] = {}
    try:
        from paths import shared_geoadmin_counties_dir  # noqa: PLC0415

        path = shared_geoadmin_counties_dir() / "counties_usa.geojson"
        payload = json.loads(path.read_text(encoding="utf-8"))
        for feature in payload.get("features", []):
            props = feature.get("properties") or {}
            state_fips = str(props.get("state_fips") or "").strip().zfill(2)
            county_fips = str(props.get("county_fips") or "").strip().zfill(3)
            name = str(props.get("county_name") or "").strip().lower()
            if state_fips and county_fips and name:
                mapping[(state_fips, name)] = county_fips
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  warning: could not read geoadmin counties for county_fips ({exc})")
    _COUNTY_FIPS = mapping
    return mapping


def resolve_fips(state: str, county: str) -> tuple[str, str]:
    """Resolve ``state_fips``/``county_fips`` for a CSB field.

    The pipeline's CDL step routes on the boundary ``state_fips`` property, so
    omitting it silently sends a farm to the wrong state raster. Derive the FIPS
    codes from the canonical geoadmin lookups instead of leaving them blank.
    """
    state_code = (state or "").strip().upper()
    state_fips = _state_fips_lookup().get(state_code, "")
    if not state_fips and state_code and state_code not in _WARNED_STATES:
        _WARNED_STATES.add(state_code)
        print(f"  warning: no state FIPS found for state={state!r}; boundaries will omit state_fips")
    county_fips = ""
    if state_fips and county:
        county_fips = _county_fips_lookup().get((state_fips, county.strip().lower()), "")
        if not county_fips and county.strip() not in _WARNED_STATES:
            _WARNED_STATES.add(county.strip())
            print(f"  warning: no county FIPS found for {county!r} in state {state_code}")
    return state_fips, county_fips


def file_crs(pf: pq.ParquetFile) -> CRS:
    kv = {
        (k.decode() if isinstance(k, bytes) else k): (
            v.decode() if isinstance(v, bytes) else v
        )
        for k, v in pf.metadata.metadata.items()
    }
    geo = json.loads(kv["geo"])
    return CRS.from_json_dict(geo["columns"]["geometry"]["crs"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Real US field boundaries from USDA CSB (FIBOA geoparquet).")
    ap.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("S", "W", "N", "E"))
    ap.add_argument("--min-area-ha", type=float, default=40.0)
    ap.add_argument("--max-fields", type=int, default=3)
    ap.add_argument("--field-prefix", required=True)
    ap.add_argument("--country", default="US")
    ap.add_argument("--state", default="")
    ap.add_argument("--year", default="")
    args = ap.parse_args(argv)

    grower = os.environ.get("AG_GROWER_SLUG") or sys.exit("AG_GROWER_SLUG required")
    farm = os.environ.get("AG_FARM_SLUG") or sys.exit("AG_FARM_SLUG required")
    farm_dir = DATA_ROOT / "growers" / grower / "farms" / farm
    if not farm_dir.exists():
        sys.exit(f"farm dir missing: {farm_dir} (create farm.json first)")

    s, w, n, e = args.bbox
    fo = fsspec.open(URL, block_size=2**26).open()
    pf = pq.ParquetFile(fo)
    crs = file_crs(pf)
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x0, y0 = tr.transform(w, s)
    x1, y1 = tr.transform(e, n)
    xmin, xmax = sorted((x0, x1))
    ymin, ymax = sorted((y0, y1))

    sel = []
    for i in range(pf.metadata.num_row_groups):
        st = pf.metadata.row_group(i)
        stats = [st.column(LEAF[k]) for k in LEAF]
        if any(not (c.statistics and c.statistics.has_min_max) for c in stats):
            sel.append(i)
            continue
        x_min_lo, _x_min_hi = stats[0].statistics.min, stats[0].statistics.max
        y_min_lo, _y_min_hi = stats[1].statistics.min, stats[1].statistics.max
        _x_max_lo, x_max_hi = stats[2].statistics.min, stats[2].statistics.max
        _y_max_lo, y_max_hi = stats[3].statistics.min, stats[3].statistics.max
        if x_max_hi > xmin and x_min_lo < xmax and y_max_hi > ymin and y_min_lo < ymax:
            sel.append(i)

    rows = []
    for i in sel:
        d = pf.read_row_group(
            i, columns=["id", "crop:name", "administrative_area_level_2", "geometry"]
        ).to_pydict()
        for fid, crop, county, geom in zip(
            d["id"], d["crop:name"], d["administrative_area_level_2"], d["geometry"]
        ):
            if geom is None:
                continue
            poly = wkb.loads(geom)
            bb = poly.bounds
            if bb[2] > xmin and bb[0] < xmax and bb[3] > ymin and bb[1] < ymax:
                ha = poly.area / 10000.0  # projected CRS is equal-area
                if ha >= args.min_area_ha:
                    rows.append((fid, crop, county, ha, poly))
    rows.sort(key=lambda r: (-r[3], str(r[0])))
    rows = rows[: args.max_fields]
    if not rows:
        print("no fields above threshold in this window")
        return 1

    inv_rows = []
    merged = None
    farm_bounds = farm_dir / "boundary" / "field_boundaries.geojson"
    for i, (fid, crop, county, ha, poly) in enumerate(rows):
        slug = f"{args.field_prefix}{i + 1}"
        state_fips, county_fips = resolve_fips(args.state, county)
        fdir = farm_dir / "fields" / slug
        (fdir / "boundary").mkdir(parents=True, exist_ok=True)
        gdf = gpd.GeoDataFrame(
            [
                {
                    "field_id": slug,
                    "country": args.country,
                    "state": args.state,
                    "state_fips": state_fips,
                    "county_fips": county_fips,
                    "year": args.year,
                    "area_ha_est": round(ha, 1),
                    "crop": crop,
                    "area_acres": round(ha * 2.47105, 1),
                    "source": "usda_csb_fiboa",
                    "csb_id": str(fid),
                    "county": county,
                    "geometry": poly,
                }
            ],
            crs=crs,
        ).to_crs("EPSG:4326")
        gdf.to_file(fdir / "boundary" / "field_boundary.geojson", driver="GeoJSON")
        merged = gdf if merged is None else pd.concat([merged, gdf], ignore_index=True)
        inv_rows.append({"field_id": slug, "field_slug": slug})
        print(f"field {slug}  {ha:>8.1f} ha  crop={crop}  county={county}  csb={fid}")

    if farm_bounds.exists():
        old = gpd.read_file(farm_bounds)
        keep = old[~old["field_id"].isin([r["field_id"] for r in inv_rows])]
        merged = pd.concat([keep, merged], ignore_index=True)
    farm_bounds.parent.mkdir(exist_ok=True)
    assert merged is not None
    merged.to_file(farm_bounds, driver="GeoJSON")

    inv = farm_dir / "manifests" / "field-inventory.csv"
    inv.parent.mkdir(exist_ok=True)
    frame = pd.DataFrame(inv_rows)
    if inv.exists():
        frame = pd.concat([pd.read_csv(inv), frame]).drop_duplicates(subset="field_id", keep="last")
    frame.to_csv(inv, index=False)

    prov = {
        "source": "FIBOA us-usda-cropland (USDA NASS Crop Sequence Boundaries)",
        "url": URL,
        "license": "CC-BY-4.0 (FIBOA); USDA public (CSB)",
        "file_crs": crs.name,
        "bbox": args.bbox,
        "row_groups_read": len(sel),
        "total_row_groups": pf.metadata.num_row_groups,
        "min_area_ha": args.min_area_ha,
        "country": args.country,
        "state": args.state,
        "accessed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "fields": [r["field_id"] for r in inv_rows],
    }
    mdir = farm_dir / "manifests"
    mdir.mkdir(exist_ok=True)
    (mdir / "csb_fields_provenance.json").write_text(json.dumps(prov, indent=2, default=str))
    jdir = DATA_ROOT / "shared" / "osm" / "manifests"
    jdir.mkdir(parents=True, exist_ok=True)
    journal = jdir / "sha256_journal.json"
    log = json.loads(journal.read_text()) if journal.exists() else {}
    log[f"{grower}/{farm}/{args.field_prefix}"] = prov
    journal.write_text(json.dumps(log, indent=2, default=str))
    print(f"\n✓ {len(rows)} real CSB fields -> {inv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
