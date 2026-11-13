#!/usr/bin/env python3
# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
"""Generate a lightweight interactive grower-level web map.

Reads the canonical runtime tree (``growers/<grower>/farms/<farm>/``) and emits
one single-file HTML map per grower: actual pipeline field boundaries drawn over
Esri World Imagery tiles, with a field list, click-to-inspect metadata, and
zoom controls.

Popups are enriched from the pipeline's own derived artifacts -- SSURGO soil
summary, DEM terrain summary, daily field weather, CDL crop history, and NDVI
card summaries -- so the map explains each field without opening another
report. Only local CSV/JSON text is read; no raster is opened.

No rasters, imagery, or data bundles are embedded -- only rounded GeoJSON
coordinates and a small metadata payload -- so the output stays small.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import statistics
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_LOCAL_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(_LOCAL_LIB))

from paths import (  # noqa: E402
    farm_boundary_path,
    farm_cdl_rotation_path,
    farm_weather_path,
    grower_dir,
)
from runtime_paths import resolve_runtime_paths  # noqa: E402

_RUNTIME_PATHS = resolve_runtime_paths()
_REPO = _RUNTIME_PATHS.runtime_base

_PALETTE = (
    "#f1c40f",
    "#2ecc71",
    "#3498db",
    "#e74c3c",
    "#9b59b6",
    "#1abc9c",
    "#e67e22",
    "#34495e",
)

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<style>
  :root { --ink:#1b2430; --muted:#5b6b7c; --line:#d8e0e8; --accent:#2e7d32; }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body { font: 14px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--ink); }
  #app { display: flex; height: 100%; }
  #panel { width: 300px; min-width: 300px; display: flex; flex-direction: column;
           border-right: 1px solid var(--line); background: #fff; }
  #panel header { padding: 14px 16px 10px; border-bottom: 1px solid var(--line); }
  #panel h1 { font-size: 15px; margin: 0 0 4px; letter-spacing: .01em; }
  #panel .sub { color: var(--muted); font-size: 12px; }
  #fields { overflow-y: auto; flex: 1; padding: 8px; }
  .farm-head { font-size: 11px; text-transform: uppercase; letter-spacing: .08em;
               color: var(--muted); padding: 10px 8px 4px; }
  .field-row { display: flex; align-items: center; gap: 8px; width: 100%;
               padding: 8px; border: 1px solid transparent; border-radius: 6px;
               background: none; text-align: left; cursor: pointer; font: inherit; color: inherit; }
  .field-row:hover, .field-row:focus { background: #f2f6fa; border-color: var(--line); outline: none; }
  .swatch { width: 10px; height: 10px; border-radius: 2px; flex: none; }
  .field-row .name { font-weight: 600; }
  .field-row .meta { color: var(--muted); font-size: 12px; }
  #map { flex: 1; height: 100%; background: #e9eef3; }
  footer { padding: 8px 16px 12px; border-top: 1px solid var(--line);
           color: var(--muted); font-size: 11px; }
  .leaflet-popup-content { margin: 10px 12px; max-height: 70vh; overflow: auto; }
  .pop { min-width: 236px; max-width: 300px; }
  .pop h3 { margin: 0 0 2px; font-size: 14px; }
  .pop .badge { display: inline-block; font-size: 10px; font-weight: 600; letter-spacing: .02em;
                padding: 1px 6px; border-radius: 10px; margin-bottom: 5px; }
  .pop .badge-corn { background: #fdf3d0; color: #6b5200; }
  .pop .badge-soy { background: #e2f3e0; color: #1f5a1a; }
  .pop .badge-other { background: #eceff3; color: #3c4a58; }
  .pop table { border-collapse: collapse; font-size: 12px; }
  .pop td { padding: 2px 8px 2px 0; vertical-align: top; }
  .pop td:first-child { color: var(--muted); white-space: nowrap; }
  .pop .sect-h { font-size: 10px; text-transform: uppercase; letter-spacing: .07em;
                 color: var(--muted); margin: 9px 0 3px; }
  .pop .strip { display: flex; flex-wrap: wrap; gap: 3px; }
  .pop .chip { font-size: 10px; padding: 1px 5px; border-radius: 3px; white-space: nowrap; }
  .pop .chip-corn { background: #fdf3d0; color: #6b5200; }
  .pop .chip-soy { background: #e2f3e0; color: #1f5a1a; }
  .pop .chip-other { background: #eceff3; color: #3c4a58; }
  .pop .dim { color: var(--muted); }
  .pop details { border-top: 1px solid var(--line); padding-top: 5px; margin-top: 7px; }
  .pop summary { cursor: pointer; font-size: 12px; font-weight: 600; outline: none; }
  .pop summary .dim { font-weight: 400; }
  .pop .note { font-size: 10px; color: #8a6d00; background: #fdf8e3;
               border-radius: 3px; padding: 2px 5px; margin-top: 4px; }
  @media (max-width: 720px) {
    #app { flex-direction: column; }
    #panel { width: 100%; min-width: 0; max-height: 45%; border-right: 0; border-bottom: 1px solid var(--line); }
  }
</style>
</head>
<body>
<div id="app">
  <aside id="panel">
    <header>
      <h1>__TITLE__</h1>
      <div class="sub">__SUBTITLE__</div>
    </header>
    <nav id="fields" aria-label="Fields"></nav>
    <footer>Boundaries: pipeline output &middot; Imagery &copy; Esri</footer>
  </aside>
  <div id="map" role="application" aria-label="Grower field map"></div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const DATA = __DATA__;
const PALETTE = __PALETTE__;

const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = (v, d) => (v === null || v === undefined || isNaN(Number(v)))
  ? "&mdash;" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });

const map = L.map("map", { zoomControl: true, scrollWheelZoom: true });
const tiles = {
  "Satellite (Esri World Imagery)": L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 19, attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics" }),
  "Streets (OpenStreetMap)": L.tileLayer(
    "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    { maxZoom: 19, attribution: "&copy; OpenStreetMap contributors" })
};
tiles["Satellite (Esri World Imagery)"].addTo(map);
L.control.layers(tiles, null, { position: "topright", collapsed: true }).addTo(map);
L.control.scale({ imperial: true, metric: true }).addTo(map);

const features = DATA.features || [];
const farmOrder = [];
features.forEach((f) => {
  if (!farmOrder.includes(f.properties.farm_slug)) farmOrder.push(f.properties.farm_slug);
});

const layers = {};

const cropTone = (crop) => {
  const key = String(crop || "").toLowerCase();
  if (key.startsWith("corn")) return "corn";
  if (key.startsWith("soy")) return "soy";
  return "other";
};
const latestCrop = (p) => {
  const years = (p.details && p.details.crop_history && p.details.crop_history.years) || {};
  const keys = Object.keys(years).sort();
  return keys.length ? years[keys[keys.length - 1]] : (p.crop || "");
};
const filled = (pairs) => pairs.filter(([, value]) =>
  value !== null && value !== undefined && String(value).length > 0);
const kvTable = (pairs) => {
  const rows = filled(pairs);
  if (!rows.length) return "";
  return `<table>${rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("")}</table>`;
};
const section = (title, summary, body) =>
  `<details><summary>${title}${summary ? ` <span class="dim">&middot; ${summary}</span>` : ""}</summary>${body}</details>`;

const popupHtml = (p) => {
  const d = p.details || {};
  const ch = d.crop_history || {};
  const ndvi = d.ndvi || {};
  const soil = d.soil || {};
  const terr = d.terrain || {};
  const wx = d.weather || {};
  const years = ch.years || {};
  const yearKeys = Object.keys(years).sort();
  const current = latestCrop(p);

  const strip = yearKeys.map((y) =>
    `<span class="chip chip-${cropTone(years[y])}">${esc(y)} ${esc(years[y])}</span>`).join("");
  const nextLine = ch.next_crop
    ? `${esc(ch.next_crop)}${ch.confidence ? ` <span class="dim">(${esc(ch.confidence)} confidence)</span>` : ""}`
    : "";
  const rotationBody = strip
    ? `<div class="strip">${strip}</div>` +
      (nextLine ? `<table><tr><td>Next</td><td>${nextLine}</td></tr></table>` : "")
    : `<div class="dim">Crop history unavailable</div>`;

  const ndviRows = [
    ndvi.corn ? ["Corn NDVI", `${fmt(ndvi.corn.mean, 2)} <span class="dim">(${ndvi.corn.years} yr${ndvi.corn.years === 1 ? "" : "s"})</span>`] : ["Corn NDVI", ""],
    ndvi.soybean ? ["Soy NDVI", `${fmt(ndvi.soybean.mean, 2)} <span class="dim">(${ndvi.soybean.years} yr${ndvi.soybean.years === 1 ? "" : "s"})</span>`] : ["Soy NDVI", ""],
  ];
  const ndviTable = kvTable(ndviRows);

  const soilPairs = [
    ["Series", esc(soil.dominant_soil || "")],
    ["Drainage", esc(soil.drainage_class || "")],
    ["Organic matter", soil.avg_om_pct != null ? `${fmt(soil.avg_om_pct, 2)} %` : ""],
    ["pH", soil.avg_ph != null ? fmt(soil.avg_ph, 2) : ""],
    ["CEC", soil.avg_cec != null ? `${fmt(soil.avg_cec, 0)} meq/100g` : ""],
    ["Clay", soil.avg_clay_pct != null ? `${fmt(soil.avg_clay_pct, 0)} %` : ""],
    ["Water capacity", soil.total_aws_inches != null ? `${fmt(soil.total_aws_inches, 1)} in` : ""],
    ["Erosion risk", esc(soil.erosion_risk || "")],
    ["pH constraint", esc(soil.ph_constraint || "")],
  ];
  const soilTable = kvTable(soilPairs);

  const elev = (terr.elev_min_ft != null && terr.elev_max_ft != null)
    ? `${fmt(terr.elev_min_ft, 0)}&ndash;${fmt(terr.elev_max_ft, 0)} ft` : "";
  const elevM = (terr.elev_min_m != null && terr.elev_max_m != null)
    ? `${fmt(terr.elev_min_m, 0)}&ndash;${fmt(terr.elev_max_m, 0)} m` : "";
  const terrainTable = kvTable([
    ["Elevation", elev ? `${elev} <span class="dim">(${elevM})</span>` : ""],
    ["Slope median", terr.slope_p50_pct != null ? `${fmt(terr.slope_p50_pct, 1)} %` : ""],
    ["Slope p95", terr.slope_p95_pct != null ? `${fmt(terr.slope_p95_pct, 1)} %` : ""],
    ["Ponding proxy", terr.ponding_acres != null ? `${fmt(terr.ponding_acres, 0)} ac` : ""],
    ["Erosion proxy", terr.erosion_acres != null ? `${fmt(terr.erosion_acres, 0)} ac` : ""],
  ]);
  const terrainNote = terr.conditioning_status
    ? `<div class="note">Hydrology proxies from an unconditioned DEM</div>` : "";

  const weatherTable = kvTable([
    ["GDD (base 10&deg;C)", wx.gdd_avg != null
      ? `${fmt(wx.gdd_avg, 0)} <span class="dim">(${fmt(wx.gdd_min, 0)}&ndash;${fmt(wx.gdd_max, 0)}, ${esc(wx.gdd_years || "")})</span>` : ""],
    ["Precip", wx.precip_in_avg != null ? `${fmt(wx.precip_in_avg, 1)} in/yr` : ""],
    ["Frost-free", wx.frost_spring
      ? `${esc(wx.frost_spring)} &rarr; ${esc(wx.frost_fall)}${wx.frost_free_days != null ? ` <span class="dim">(${fmt(wx.frost_free_days, 0)} d)</span>` : ""}` : ""],
  ]);

  return `
  <div class="pop">
    <h3>${esc(p.field_name || p.field_slug)}</h3>
    ${current ? `<span class="badge badge-${cropTone(current)}">${esc(current)}</span>` : ""}
    ${kvTable([
      ["Grower", esc(p.grower_name || p.grower_slug)],
      ["Farm", esc(p.farm_name || p.farm_slug)],
      ["Field", esc(p.field_slug)],
      ["County", [p.county, p.state].filter(Boolean).map(esc).join(", ")],
      ["Area", p.acres != null ? `${fmt(p.acres, 1)} ac <span class="dim">(${fmt(p.ha, 1)} ha)</span>` : ""],
      ["Field ID", p.field_id && p.field_id !== p.field_slug ? esc(p.field_id) : ""],
    ])}
    <div class="sect-h">Crop rotation</div>
    ${rotationBody}
    ${ndviTable ? `<div class="sect-h">NDVI averages</div>${ndviTable}` : ""}
    ${soilTable ? section("Soil", esc(soil.dominant_soil || ""), soilTable) : ""}
    ${terrainTable ? section("Terrain", elev, terrainTable + terrainNote) : ""}
    ${weatherTable ? section("Weather", wx.gdd_avg != null ? `${fmt(wx.gdd_avg, 0)} GDD` : "", weatherTable) : ""}
  </div>`;
};

const geoLayer = L.geoJSON(DATA, {
  style: (f) => {
    const idx = Math.max(0, farmOrder.indexOf(f.properties.farm_slug));
    return { color: PALETTE[idx % PALETTE.length], weight: 2, opacity: 0.95,
             fillColor: PALETTE[idx % PALETTE.length], fillOpacity: 0.22 };
  },
  onEachFeature: (f, layer) => {
    layers[f.properties.key] = layer;
    layer.bindPopup(popupHtml(f.properties));
    layer.on("click", () => layer.openPopup());
    layer.on("mouseover", () => layer.setStyle({ weight: 4, fillOpacity: 0.38 }));
    layer.on("mouseout", () => layer.setStyle({ weight: 2, fillOpacity: 0.22 }));
  }
}).addTo(map);

const list = document.getElementById("fields");
if (!features.length) {
  list.innerHTML = '<p class="sub" style="padding:8px">No field boundaries found.</p>';
} else {
  farmOrder.forEach((farmSlug) => {
    const rows = features.filter((f) => f.properties.farm_slug === farmSlug);
    const idx = farmOrder.indexOf(farmSlug);
    const head = document.createElement("div");
    head.className = "farm-head";
    head.textContent = rows[0].properties.farm_name || farmSlug;
    list.appendChild(head);
    rows.forEach((f) => {
      const p = f.properties;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "field-row";
      btn.innerHTML =
        `<span class="swatch" style="background:${PALETTE[idx % PALETTE.length]}"></span>` +
        `<span><span class="name">${esc(p.field_name || p.field_slug)}</span><br>` +
        `<span class="meta">${[latestCrop(p), p.acres != null ? fmt(p.acres, 0) + " ac" : ""]
          .filter(Boolean).map(esc).join(" &middot; ")}</span></span>`;
      btn.addEventListener("click", () => {
        const layer = layers[p.key];
        if (!layer) return;
        map.flyToBounds(layer.getBounds(), { padding: [30, 30], maxZoom: 17 });
        layer.openPopup();
      });
      list.appendChild(btn);
    });
  });
}

const all = geoLayer.getBounds();
if (all.isValid()) map.fitBounds(all, { padding: [20, 20] });
else map.setView([41.5, -93.0], 5);

// Keep the map sized correctly after the flex layout settles.
setTimeout(() => map.invalidateSize(), 0);
</script>
</body>
</html>
"""


def _round_coordinates(value: Any, decimals: int) -> Any:
    if isinstance(value, (int, float)):
        return round(float(value), decimals)
    if isinstance(value, (list, tuple)):
        return [_round_coordinates(item, decimals) for item in value]
    return value


def _field_display_name(farm_dir: Path, field_slug: str) -> str:
    meta = farm_dir / "fields" / field_slug / "field.json"
    if meta.exists():
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return field_slug
        name = str(payload.get("display_name") or "").strip()
        if name and name != field_slug:
            return name
    return field_slug


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


_ACRE_M2 = 4046.8564224
_M_TO_FT = 1 / 0.3048
_MM_PER_IN = 25.4
_GDD_BASE_C = 10.0
_SOIL_NUMERIC = {
    "avg_om_pct": 2,
    "avg_ph": 2,
    "total_aws_inches": 1,
    "avg_cec": 0,
    "avg_clay_pct": 0,
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    """Read a CSV into rows, returning an empty list when it is absent or empty."""
    if not path.exists():
        return []
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except (OSError, csv.Error, UnicodeDecodeError):
        return []


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _month_day(day_of_year: int) -> str:
    moment = datetime(2021, 1, 1) + timedelta(days=max(0, day_of_year - 1))
    return f"{moment:%b} {moment.day}"


def _field_paths(farm_path: Path, field_slug: str) -> tuple[Path, Path]:
    field_path = farm_path / "fields" / field_slug
    return field_path, field_path / "derived"


def _load_soil(farm_path: Path, field_slug: str) -> dict[str, Any]:
    """Dominant soil, chemistry, and drainage from the SSURGO field summary."""
    rows = _read_csv(farm_path / "fields" / field_slug / "soil" / "ssurgo_summary.csv")
    if not rows:
        return {}
    row = rows[0]
    soil: dict[str, Any] = {}
    for key, digits in _SOIL_NUMERIC.items():
        value = _as_float(row.get(key))
        if value is not None:
            soil[key] = round(value, digits)
    for key in ("dominant_soil", "drainage_class", "erosion_risk"):
        text = str(row.get(key) or "").strip()
        if text:
            soil[key] = text
    constraint = str(row.get("ph_constraint") or "").strip()
    if constraint and constraint.lower() != "none":
        soil["ph_constraint"] = constraint
    return soil


def _load_terrain(farm_path: Path, field_slug: str) -> dict[str, Any]:
    """Elevation, slope, and hydrology proxies from the DEM terrain summary."""
    _, derived = _field_paths(farm_path, field_slug)
    payload = _load_json(derived / "tables" / "dem_terrain_summary.json")
    if not payload:
        return {}
    terrain: dict[str, Any] = {}
    elevation = payload.get("elevation_range_m") or {}
    minimum = _as_float(elevation.get("min"))
    maximum = _as_float(elevation.get("max"))
    if minimum is not None and maximum is not None:
        terrain["elev_min_ft"] = round(minimum * _M_TO_FT)
        terrain["elev_max_ft"] = round(maximum * _M_TO_FT)
        terrain["elev_min_m"] = round(minimum)
        terrain["elev_max_m"] = round(maximum)
    slope = payload.get("slope_percentiles") or {}
    median = _as_float(slope.get("p50"))
    p95 = _as_float(slope.get("p95"))
    if median is not None:
        terrain["slope_p50_pct"] = round(median, 1)
    if p95 is not None:
        terrain["slope_p95_pct"] = round(p95, 1)
    for source_key, target_key in (
        ("ponding_risk_area", "ponding_acres"),
        ("erosion_proxy_area", "erosion_acres"),
    ):
        area = _as_float((payload.get(source_key) or {}).get("area_square_map_units"))
        if area:
            terrain[target_key] = round(area / _ACRE_M2, 1)
    status = str(payload.get("conditioning_status") or "").strip()
    if status and status not in {"ok", "completed", "applied"}:
        terrain["conditioning_status"] = status
    return terrain


def _load_weather(
    grower_slug: str, farm_slug: str, farm_path: Path, field_slug: str
) -> dict[str, Any]:
    """Season GDD, precipitation, and frost window from the daily field record.

    GDD uses the mean method with a 10 C base, matching
    ``generate_weather_charts.py`` so the number agrees with the farm report.
    """
    rows = _read_csv(farm_path / "fields" / field_slug / "weather" / "daily_weather.csv")
    if not rows:
        rows = [
            row
            for row in _read_csv(farm_weather_path(grower_slug, farm_slug))
            if str(row.get("field_id") or "") == field_slug
        ]
    if not rows:
        return {}

    gdd_by_year: dict[int, float] = {}
    precip_by_year: dict[int, float] = {}
    days_by_year: dict[int, int] = {}
    last_spring_frost: dict[int, int] = {}
    first_fall_frost: dict[int, int] = {}
    for row in rows:
        stamp = str(row.get("date") or "")
        if len(stamp) < 10:
            continue
        try:
            year = int(stamp[:4])
            month = int(stamp[5:7])
            day = int(stamp[8:10])
        except ValueError:
            continue
        days_by_year[year] = days_by_year.get(year, 0) + 1
        tmax = _as_float(row.get("T2M_MAX"))
        tmin = _as_float(row.get("T2M_MIN"))
        if tmax is not None and tmin is not None:
            gdd_by_year[year] = gdd_by_year.get(year, 0.0) + max(
                0.0, (tmax + tmin) / 2.0 - _GDD_BASE_C
            )
            if tmin <= 0.0:
                doy = datetime(year, month, day).timetuple().tm_yday
                if month <= 6:
                    last_spring_frost[year] = max(last_spring_frost.get(year, 0), doy)
                elif month >= 9:
                    first_fall_frost[year] = min(first_fall_frost.get(year, 999), doy)
        precip = _as_float(row.get("PRECTOTCORR"))
        if precip is not None:
            precip_by_year[year] = precip_by_year.get(year, 0.0) + precip

    if not gdd_by_year:
        return {}
    all_years = sorted(gdd_by_year)
    # A partial trailing year would understate GDD and drag the average down.
    complete = [year for year in all_years if days_by_year.get(year, 0) >= 300]
    years = complete or all_years
    weather: dict[str, Any] = {
        "gdd_avg": round(statistics.mean(gdd_by_year[year] for year in years)),
        "gdd_min": round(min(gdd_by_year[year] for year in years)),
        "gdd_max": round(max(gdd_by_year[year] for year in years)),
        "gdd_years": f"{years[0]}-{years[-1]}",
    }
    precip_years = [year for year in years if year in precip_by_year]
    if precip_years:
        weather["precip_in_avg"] = round(
            statistics.mean(precip_by_year[year] for year in precip_years) / _MM_PER_IN, 1
        )
    frost_years = sorted(set(last_spring_frost) & set(first_fall_frost) & set(years))
    if frost_years:
        spring_doy = round(
            statistics.median(last_spring_frost[year] for year in frost_years)
        )
        fall_doy = round(statistics.median(first_fall_frost[year] for year in frost_years))
        weather["frost_spring"] = _month_day(spring_doy)
        weather["frost_fall"] = _month_day(fall_doy)
        weather["frost_free_days"] = fall_doy - spring_doy
    return weather


def _rotation_rows(grower_slug: str, farm_slug: str) -> dict[str, dict[str, str]]:
    rows = _read_csv(farm_cdl_rotation_path(grower_slug, farm_slug))
    return {
        str(row.get("field_id") or ""): row
        for row in rows
        if str(row.get("field_id") or "").strip()
    }


def _load_crop_history(
    farm_path: Path, field_slug: str, rotation: dict[str, str]
) -> dict[str, Any]:
    """Per-year crops from the NDVI join plus the CDL rotation outlook."""
    _, derived = _field_paths(farm_path, field_slug)
    years: dict[str, str] = {}
    for row in _read_csv(derived / "tables" / "ndvi_year_crop_join.csv"):
        crop = str(row.get("crop_name") or "").strip()
        year = str(row.get("year") or "").strip()
        if crop and crop.lower() != "unknown" and year.isdigit():
            years[year] = crop

    history: dict[str, Any] = {}
    if years:
        history["years"] = dict(sorted(years.items()))
    if rotation:
        next_crop = str(rotation.get("predicted_next_crop") or "").strip()
        confidence = str(rotation.get("rotation_confidence") or "").strip()
        if next_crop:
            history["next_crop"] = next_crop
        if confidence:
            history["confidence"] = confidence
    return history


def _load_ndvi_means(farm_path: Path, field_slug: str) -> dict[str, Any]:
    """Crop-conditioned mean NDVI already written by the NDVI card step."""
    _, derived = _field_paths(farm_path, field_slug)
    payload = _load_json(derived / "summaries" / "ndvi_card_summary.json")
    cards = payload.get("cards") if isinstance(payload.get("cards"), dict) else {}
    means: dict[str, Any] = {}
    for key in ("corn", "soybean"):
        card = cards.get(key) or {}
        mean = _as_float(card.get("mean_ndvi"))
        if card.get("status") == "available" and mean is not None:
            means[key] = {"mean": round(mean, 2), "years": len(card.get("years") or [])}
    return means


def _collect_details(
    grower_slug: str,
    farm_slug: str,
    farm_path: Path,
    field_slug: str,
    rotation: dict[str, str],
) -> dict[str, Any]:
    details: dict[str, Any] = {}
    crop_history = _load_crop_history(farm_path, field_slug, rotation)
    if crop_history:
        details["crop_history"] = crop_history
    ndvi = _load_ndvi_means(farm_path, field_slug)
    if ndvi:
        details["ndvi"] = ndvi
    soil = _load_soil(farm_path, field_slug)
    if soil:
        details["soil"] = soil
    terrain = _load_terrain(farm_path, field_slug)
    if terrain:
        details["terrain"] = terrain
    weather = _load_weather(grower_slug, farm_slug, farm_path, field_slug)
    if weather:
        details["weather"] = weather
    return details



def _normalize_properties(
    raw: dict[str, Any],
    *,
    grower_slug: str,
    grower_name: str,
    farm_slug: str,
    farm_name: str,
    farm_state: str,
    field_name: str,
    key: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    field_slug = str(raw.get("field_id") or raw.get("field_slug") or key)
    crop = str(raw.get("crop") or raw.get("crop_name") or "").strip()
    if crop.lower() == "farmland":
        crop = "Farmland (OSM)"
    county = str(raw.get("county") or raw.get("county_name") or "").strip()
    state = str(raw.get("state") or farm_state or "").strip()

    acres: float | None = None
    raw_acres = raw.get("area_acres")
    if isinstance(raw_acres, (int, float)):
        acres = float(raw_acres)
    ha: float | None = None
    raw_ha = raw.get("area_ha_est")
    if isinstance(raw_ha, (int, float)):
        ha = float(raw_ha)
    if ha is None and acres is not None:
        ha = acres / 2.47105

    props: dict[str, Any] = {
        "key": key,
        "grower_slug": grower_slug,
        "grower_name": grower_name,
        "farm_slug": farm_slug,
        "farm_name": farm_name,
        "field_slug": field_slug,
        "field_name": field_name,
        "field_id": str(raw.get("field_id") or ""),
    }
    if crop:
        props["crop"] = crop
    if county:
        props["county"] = county
    if state:
        props["state"] = state
    if acres is not None:
        props["acres"] = round(acres, 1)
    if ha is not None:
        props["ha"] = round(ha, 1)
    if details:
        props["details"] = details
    return props


def build_grower_payload(
    grower_slug: str,
    decimals: int,
    *,
    lean: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    root = grower_dir(grower_slug)
    grower_meta = _load_json(root / "grower.json")
    grower_name = str(grower_meta.get("display_name") or grower_slug)

    features: list[dict[str, Any]] = []
    sources: list[str] = []
    for farm_path in sorted((root / "farms").glob("*")):
        if not farm_path.is_dir():
            continue
        farm_slug = farm_path.name
        boundary = farm_boundary_path(grower_slug, farm_slug)
        if not boundary.exists():
            print(f"  skip {grower_slug}/{farm_slug}: no boundary file")
            continue
        farm_meta = _load_json(farm_path / "farm.json")
        farm_name = str(farm_meta.get("display_name") or farm_slug)
        farm_state = str(farm_meta.get("state") or "")
        rotation = {} if lean else _rotation_rows(grower_slug, farm_slug)

        payload = _load_json(boundary)
        sources.append(str(boundary.relative_to(_REPO)))
        for index, feature in enumerate(payload.get("features", [])):
            geometry = feature.get("geometry")
            raw_props = feature.get("properties")
            if not isinstance(geometry, dict) or not isinstance(raw_props, dict):
                continue
            field_slug = str(raw_props.get("field_id") or f"field-{index + 1}")
            key = f"{farm_slug}/{field_slug}"
            details = (
                {}
                if lean
                else _collect_details(
                    grower_slug,
                    farm_slug,
                    farm_path,
                    field_slug,
                    rotation.get(field_slug, {}),
                )
            )
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": geometry.get("type"),
                        "coordinates": _round_coordinates(geometry.get("coordinates"), decimals),
                    },
                    "properties": _normalize_properties(
                        raw_props,
                        grower_slug=grower_slug,
                        grower_name=grower_name,
                        farm_slug=farm_slug,
                        farm_name=farm_name,
                        farm_state=farm_state,
                        field_name=_field_display_name(farm_path, field_slug),
                        key=key,
                        details=details,
                    ),
                }
            )

    features.sort(key=lambda item: str(item["properties"]["key"]))
    return {"type": "FeatureCollection", "features": features}, sources


def render_html(title: str, subtitle: str, collection: dict[str, Any]) -> str:
    data = json.dumps(collection, sort_keys=True, separators=(",", ":"))
    data = data.replace("</", "<\\/")
    return (
        _TEMPLATE.replace("__TITLE__", html.escape(title))
        .replace("__SUBTITLE__", html.escape(subtitle))
        .replace("__PALETTE__", json.dumps(list(_PALETTE)))
        .replace("__DATA__", data)
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a lightweight interactive grower web map from pipeline boundaries."
    )
    parser.add_argument(
        "--grower-slug",
        action="append",
        default=[],
        help="Grower slug to map (repeatable). Defaults to every grower when --all-growers is set.",
    )
    parser.add_argument(
        "--all-growers",
        action="store_true",
        help="Generate a map for every grower under the runtime tree.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Explicit output HTML path (only valid with a single grower).",
    )
    parser.add_argument(
        "--decimals",
        type=int,
        default=6,
        help="Coordinate decimal places in the embedded GeoJSON (default: 6).",
    )
    parser.add_argument(
        "--lean",
        action="store_true",
        help="Skip derived-data enrichment (soil, terrain, weather, crop history, NDVI).",
    )
    return parser.parse_args(argv)


_ENRICHMENT_SECTIONS = ("crop_history", "ndvi", "soil", "terrain", "weather")


def _enrichment_summary(features: list[dict[str, Any]], enabled: bool) -> dict[str, Any]:
    counts = {section: 0 for section in _ENRICHMENT_SECTIONS}
    for feature in features:
        details = feature["properties"].get("details") or {}
        for section in _ENRICHMENT_SECTIONS:
            if details.get(section):
                counts[section] += 1
    return {"enabled": enabled, "fields": len(features), "sections": counts}


def _discover_growers() -> list[str]:
    growers_root = _REPO / "growers"
    if not growers_root.exists():
        return []
    return sorted(path.name for path in growers_root.iterdir() if path.is_dir())


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    growers = sorted(set(args.grower_slug))
    if args.all_growers or not growers:
        growers = _discover_growers()
    if not growers:
        print("No growers found in the runtime tree.")
        return 1
    if args.output and len(growers) > 1:
        print("--output is only valid with a single grower.")
        return 1

    failures = 0
    for grower_slug in growers:
        if args.output:
            output = Path(args.output).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
        else:
            output = grower_dir(grower_slug) / "web-map" / f"{grower_slug}-grower-map.html"
            output.parent.mkdir(parents=True, exist_ok=True)

        collection, sources = build_grower_payload(
            grower_slug,
            args.decimals,
            lean=args.lean,
        )
        count = len(collection["features"])
        if count == 0:
            print(f"skip {grower_slug}: no field features")
            failures += 1
            continue

        farm_names = sorted(
            {str(f["properties"]["farm_name"]) for f in collection["features"]}
        )
        title = f"{grower_slug} - grower map"
        subtitle = f"{count} fields across {len(farm_names)} farm(s)"
        document = render_html(title, subtitle, collection)
        output.write_text(document, encoding="utf-8")

        manifest = {
            "grower_slug": grower_slug,
            "output": str(output),
            "html_bytes": output.stat().st_size,
            "field_count": count,
            "farms": farm_names,
            "boundary_sources": sources,
            "coordinate_decimals": args.decimals,
            "enrichment": _enrichment_summary(collection["features"], not args.lean),
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "generator": "data-pipeline web-map subskill",
        }
        (output.parent / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

        print(f"✓ {grower_slug}: {count} fields -> {output} ({output.stat().st_size / 1024:.1f} KB)")
        if output.stat().st_size > 2_000_000:
            print("  warning: output exceeds 2 MB; consider a higher --decimals reduction")

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
