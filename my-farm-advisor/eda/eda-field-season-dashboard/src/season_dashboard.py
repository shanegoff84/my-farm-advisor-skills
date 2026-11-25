"""Field-season storyline dashboard for one field-year.

Reads the canonical runtime tree (Sentinel-2 NDVI, NASA POWER daily weather and
CDL crop tables), aligns everything on the growing-season calendar, detects a
small number of meaningful events and renders a four-panel mini-dashboard.

The module is intentionally standalone: ``build_all`` is the reusable entry
point, while every loader and figure helper can be imported on its own.
"""

from __future__ import annotations

import json
import math
import os
import re
import textwrap
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from matplotlib.lines import Line2D
from matplotlib.ticker import MultipleLocator
from rasterio.crs import CRS
from rasterio.features import rasterize
from rasterio.warp import Resampling, reproject, transform_geom

# ---------------------------------------------------------------------------
# Interpretation constants
# ---------------------------------------------------------------------------

SEASON_START_MONTH = 4
SEASON_START_DAY = 1
SEASON_END_MONTH = 10
SEASON_END_DAY = 31

GDD_BASE_C = 10.0          # 50 F, mean method, matching the pipeline and corn guide
HOT_DAY_C = 32.0           # heat-stress watch threshold
EXTREME_HEAT_C = 35.0      # 95 F, pollination-stress threshold
HEAVY_RAIN_MM = 25.0
DRY_DAY_MM = 1.0
DRY_SPELL_MIN_DAYS = 14
MIN_VALID_PIXEL_FRACTION = 0.30
MIN_VALID_SCL_CLASSES = (4, 5, 6)  # vegetation, non-vegetation, water

NDVI_COLOR = "#2e7d32"
PRECIP_COLOR = "#1f77b4"
PRECIP_CUM_COLOR = "#d97706"
TMAX_COLOR = "#c0392b"
TMIN_COLOR = "#2471a3"
TAVG_COLOR = "#e67e22"
GDD_COLOR = "#6a1b9a"
NORMAL_COLOR = "#7f8c8d"
EVENT_COLOR = "#455a64"

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ---------------------------------------------------------------------------
# Paths and discovery
# ---------------------------------------------------------------------------

def resolve_data_root(data_root: str | os.PathLike[str] | None = None) -> Path:
    """Return the ``DATA_PIPELINE_DATA_ROOT`` directory.

    Accepts either the runtime root (the directory containing ``data-pipeline``)
    or the ``data-pipeline`` directory itself.
    """

    raw: str | os.PathLike[str] | None = data_root
    if raw is None:
        raw = os.environ.get("DATA_PIPELINE_DATA_ROOT")
    candidates: list[Path] = []
    if raw:
        candidates.append(Path(raw).expanduser())
    candidates.append(Path.home() / "my-farm-advisor-runtime")

    for candidate in candidates:
        path = candidate.resolve()
        if path.name == "data-pipeline" and (path / "growers").exists():
            return path.parent
        if (path / "data-pipeline" / "growers").exists():
            return path
        if (path / "growers").exists():
            return path

    tried = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError(
        "Could not find the data-pipeline runtime tree. Set "
        f"DATA_PIPELINE_DATA_ROOT (tried: {tried})."
    )


def pipeline_root(data_root: Path) -> Path:
    nested = data_root / "data-pipeline"
    return nested if nested.exists() else data_root


@dataclass
class FieldContext:
    data_root: Path
    pipeline: Path
    grower: str
    farm: str
    field_id: str
    field_dir: Path
    farm_dir: Path
    boundary_path: Path
    weather_path: Path
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def county(self) -> str:
        return str(self.properties.get("county_name") or "").strip()

    @property
    def state_fips(self) -> str:
        return str(self.properties.get("state_fips") or "").strip()

    @property
    def area_acres(self) -> float | None:
        value = self.properties.get("area_acres")
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def _discover(data_root: Path, grower: str | None = None,
              farm: str | None = None) -> list[FieldContext]:
    pipeline = pipeline_root(data_root)
    contexts: list[FieldContext] = []
    growers_dir = pipeline / "growers"
    if not growers_dir.exists():
        raise FileNotFoundError(f"No growers directory under {pipeline}")

    for grower_dir in sorted(p for p in growers_dir.iterdir() if p.is_dir()):
        if grower and grower_dir.name != grower:
            continue
        farms_dir = grower_dir / "farms"
        if not farms_dir.exists():
            continue
        for farm_dir in sorted(p for p in farms_dir.iterdir() if p.is_dir()):
            if farm and farm_dir.name != farm:
                continue
            boundary = farm_dir / "boundary" / "field_boundaries.geojson"
            fields_dir = farm_dir / "fields"
            if not boundary.exists() or not fields_dir.exists():
                continue
            try:
                collection = json.loads(boundary.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for feature in collection.get("features", []):
                props = feature.get("properties") or {}
                field_id = str(props.get("field_id") or "").strip()
                if not field_id:
                    continue
                field_dir = fields_dir / field_id
                if not field_dir.exists():
                    continue
                contexts.append(FieldContext(
                    data_root=data_root,
                    pipeline=pipeline,
                    grower=grower_dir.name,
                    farm=farm_dir.name,
                    field_id=field_id,
                    field_dir=field_dir,
                    farm_dir=farm_dir,
                    boundary_path=boundary,
                    weather_path=field_dir / "weather" / "daily_weather.csv",
                    properties=props,
                ))
    return contexts


def find_field(data_root: Path, field_id: str, grower: str | None = None,
               farm: str | None = None) -> FieldContext:
    matches = [c for c in _discover(data_root, grower, farm) if c.field_id == field_id]
    if not matches:
        scope = "/".join(x for x in (grower, farm) if x) or "any grower"
        raise LookupError(f"Field {field_id!r} not found in {scope}.")
    if len(matches) > 1:
        raise LookupError(
            f"Field {field_id!r} occurs in {len(matches)} farms; pass --grower/--farm."
        )
    return matches[0]


# ---------------------------------------------------------------------------
# CDL crop-year selection
# ---------------------------------------------------------------------------

def load_cdl_crop(ctx: FieldContext, year: int) -> dict[str, Any]:
    """Pick the dominant CDL crop for the field-year."""

    tables = sorted(ctx.farm_dir.glob(f"derived/tables/*_{year}_cdl.csv"))
    if not tables:
        tables = sorted(ctx.farm_dir.glob(f"derived/tables/*{year}*cdl*.csv"))
    frames: list[pd.DataFrame] = []
    source: Path | None = None
    for table in tables:
        try:
            frame = pd.read_csv(table, encoding="utf-8-sig")
        except (OSError, pd.errors.ParserError):
            continue
        if "field_id" not in frame.columns:
            continue
        frame = frame[frame["field_id"].astype(str) == ctx.field_id].copy()
        if frame.empty:
            continue
        frame["_table"] = str(table)
        frames.append(frame)
        source = table
    if not frames:
        raise LookupError(
            f"No CDL rows for field {ctx.field_id} in {year} under "
            f"{ctx.farm_dir / 'derived' / 'tables'}"
        )

    frame = pd.concat(frames, ignore_index=True)
    for column in ("pct", "pixel_count", "crop_code"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "pct" not in frame.columns or frame["pct"].isna().all():
        if "pixel_count" in frame.columns:
            total = frame["pixel_count"].sum()
            frame["pct"] = frame["pixel_count"] / total * 100 if total else np.nan
        else:
            frame["pct"] = np.nan

    frame = frame.sort_values(["pct", "pixel_count"], ascending=False,
                              na_position="last")
    row = frame.iloc[0]
    crop_name = str(row.get("crop_name") or row.get("crop") or "Unknown").strip()
    return {
        "crop_name": crop_name,
        "crop_code": _as_int(row.get("crop_code")),
        "pct": _as_float(row.get("pct")),
        "pixel_count": _as_int(row.get("pixel_count")),
        "crop_source": str(row.get("crop_source") or "cdl"),
        "table": str(source) if source else None,
        "alternatives": [
            {"crop_name": str(row.get("crop_name") or ""),
             "pct": _as_float(row.get("pct"))}
            for row in frame.head(3).to_dict("records")
        ] if "pct" in frame.columns else [],
    }


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------

WEATHER_COLUMNS = ("T2M", "T2M_MAX", "T2M_MIN", "PRECTOTCORR")


def season_bounds(year: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(year=year, month=SEASON_START_MONTH, day=SEASON_START_DAY)
    end = pd.Timestamp(year=year, month=SEASON_END_MONTH, day=SEASON_END_DAY)
    return start, end


def load_weather(ctx: FieldContext, year: int) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load, complete and annotate daily weather for the field-year."""

    if not ctx.weather_path.exists():
        raise FileNotFoundError(f"Missing daily weather: {ctx.weather_path}")
    frame = pd.read_csv(ctx.weather_path, parse_dates=["date"])
    if frame.empty:
        raise ValueError(f"Weather table is empty: {ctx.weather_path}")
    if "field_id" in frame.columns:
        subset = frame[frame["field_id"].astype(str) == ctx.field_id]
        if not subset.empty:
            frame = subset
    frame = frame.sort_values("date").drop_duplicates("date", keep="last")

    missing_columns = [c for c in WEATHER_COLUMNS if c not in frame.columns]
    if missing_columns:
        raise ValueError(
            f"Missing weather columns {missing_columns} in {ctx.weather_path}"
        )

    full_index = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="D")
    frame = frame.set_index("date").reindex(full_index)
    frame.index.name = "date"
    missing_before = int(frame["T2M_MAX"].isna().sum())

    for column in WEATHER_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame[list(WEATHER_COLUMNS)] = (
        frame[list(WEATHER_COLUMNS)]
        .interpolate(method="time", limit=7, limit_area="inside")
    )
    frame = frame.reset_index()
    frame["imputed"] = False
    frame.loc[frame["T2M_MAX"].isna(), "imputed"] = True

    # Smooth negative near-zero precipitation (NASA POWER can return tiny values).
    frame.loc[frame["PRECTOTCORR"] < 0, "PRECTOTCORR"] = 0.0
    frame["tmax"] = frame["T2M_MAX"]
    frame["tmin"] = frame["T2M_MIN"]
    frame["tavg"] = frame["T2M"]
    frame["precip"] = frame["PRECTOTCORR"]
    frame["doy"] = frame["date"].dt.dayofyear
    frame["gdd_daily"] = np.maximum(
        (frame["tmax"] + frame["tmin"]) / 2.0 - GDD_BASE_C, 0.0
    )
    frame["gdd_daily"] = frame["gdd_daily"].fillna(0.0)
    frame["gdd_cum"] = frame["gdd_daily"].cumsum()
    frame["precip_cum"] = frame["precip"].fillna(0.0).cumsum()
    frame["hot"] = frame["tmax"] >= HOT_DAY_C
    frame["extreme"] = frame["tmax"] >= EXTREME_HEAT_C
    frame["heavy_rain"] = frame["precip"] >= HEAVY_RAIN_MM
    frame["frost"] = frame["tmin"] <= 0.0
    frame["dry"] = frame["precip"].fillna(0.0) < DRY_DAY_MM
    frame["in_season"] = (
        (frame["date"] >= season_bounds(year)[0])
        & (frame["date"] <= season_bounds(year)[1])
    )

    expected = (season_bounds(year)[1] - season_bounds(year)[0]).days + 1
    present = int(frame.loc[frame["in_season"], "T2M_MAX"].notna().sum())
    coverage = {
        "year": year,
        "season_start": str(season_bounds(year)[0].date()),
        "season_end": str(season_bounds(year)[1].date()),
        "days_expected": expected,
        "days_present": present,
        "missing_days": max(expected - present, 0),
        "missing_days_before_interpolation": missing_before,
        "source": str(ctx.weather_path),
    }
    return frame, coverage


def _season_weather(weather: pd.DataFrame, year: int) -> pd.DataFrame:
    start, end = season_bounds(year)
    return weather[(weather["date"] >= start) & (weather["date"] <= end)].copy()


def _runs(mask: Sequence[bool], dates: Sequence[pd.Timestamp]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    start: int | None = None
    for index, value in enumerate(mask):
        if value and start is None:
            start = index
        elif not value and start is not None:
            runs.append({
                "start": dates[start],
                "end": dates[index - 1],
                "days": index - start,
            })
            start = None
    if start is not None:
        runs.append({
            "start": dates[start],
            "end": dates[len(dates) - 1],
            "days": len(dates) - start,
        })
    return runs


def season_weather_metrics(weather: pd.DataFrame, year: int) -> dict[str, Any]:
    season = _season_weather(weather, year)
    dates = list(season["date"])
    total_precip = float(season["precip"].fillna(0.0).sum())
    total_gdd = float(season["gdd_daily"].sum())

    heat_runs = [r for r in _runs(season["hot"].tolist(), dates) if r["days"] >= 3]
    dry_runs = [
        r for r in _runs(season["dry"].tolist(), dates)
        if r["days"] >= DRY_SPELL_MIN_DAYS
    ]
    heat_runs.sort(key=lambda r: (-r["days"], r["start"]))
    dry_runs.sort(key=lambda r: (-r["days"], r["start"]))

    hottest_index = season["tmax"].idxmax() if season["tmax"].notna().any() else None
    coldest_index = season["tmin"].idxmin() if season["tmin"].notna().any() else None
    wettest_index = season["precip"].idxmax() if season["precip"].notna().any() else None

    def row_date(index: int | None) -> pd.Timestamp | None:
        return None if index is None else pd.Timestamp(weather.loc[index, "date"])

    return {
        "season": season,
        "precip_total_mm": total_precip,
        "gdd_total": total_gdd,
        "hot_days": int(season["hot"].sum()),
        "extreme_days": int(season["extreme"].sum()),
        "heavy_rain_days": int(season["heavy_rain"].sum()),
        "frost_days": int(season["frost"].sum()),
        "hottest": {
            "date": row_date(hottest_index),
            "value": _as_float(season.loc[hottest_index, "tmax"])
            if hottest_index is not None else None,
        },
        "coldest": {
            "date": row_date(coldest_index),
            "value": _as_float(season.loc[coldest_index, "tmin"])
            if coldest_index is not None else None,
        },
        "wettest": {
            "date": row_date(wettest_index),
            "value": _as_float(season.loc[wettest_index, "precip"])
            if wettest_index is not None else None,
        },
        "heat_runs": heat_runs,
        "dry_runs": dry_runs,
        "monthly_precip": {
            MONTHS[month - 1]: float(value)
            for month, value in season.groupby(season["date"].dt.month)["precip"]
            .sum().items()
        },
    }


def gdd_normals(ctx: FieldContext, years: Iterable[int],
                year: int) -> pd.DataFrame | None:
    """Same-field cumulative-GDD mean and spread for the candidate years."""

    start, end = season_bounds(year)
    day_offsets = pd.RangeIndex((end - start).days + 1)
    series: list[pd.Series] = []
    used_years: list[int] = []
    for other_year in sorted(set(int(y) for y in years)):
        try:
            weather, _ = load_weather(ctx, other_year)
        except (FileNotFoundError, ValueError, LookupError):
            continue
        season = _season_weather(weather, other_year)
        if season.empty:
            continue
        values = season.set_index(
            (season["date"] - season["date"].min()).dt.days
        )["gdd_daily"].cumsum()
        values = values.reindex(day_offsets).ffill().fillna(0.0)
        series.append(values)
        used_years.append(other_year)
    if not series:
        return None
    matrix = pd.concat(series, axis=1)
    normal = pd.DataFrame({
        "day_offset": day_offsets,
        "mean": matrix.mean(axis=1).to_numpy(),
        "std": matrix.std(axis=1, ddof=0).fillna(0.0).to_numpy(),
        "years": matrix.notna().sum(axis=1).to_numpy(),
    })
    normal["date"] = start + pd.to_timedelta(normal["day_offset"], unit="D")
    normal.attrs["years"] = used_years
    normal.attrs["totals"] = [float(s.iloc[-1]) for s in series]
    return normal


# ---------------------------------------------------------------------------
# Sentinel NDVI
# ---------------------------------------------------------------------------

SCENE_RE = re.compile(r"sentinel_(\d{8})")


def _scene_date(scene_dir: Path) -> date | None:
    match = SCENE_RE.search(scene_dir.name)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            return None
    matches = re.findall(r"(\d{8})", scene_dir.name)
    for raw in matches:
        try:
            return datetime.strptime(raw, "%Y%m%d").date()
        except ValueError:
            continue
    return None


def _geometry_in_crs(ctx: FieldContext, target_crs: CRS) -> dict[str, Any]:
    collection = json.loads(ctx.boundary_path.read_text(encoding="utf-8"))
    feature = next(
        (f for f in collection.get("features", [])
         if str((f.get("properties") or {}).get("field_id")) == ctx.field_id),
        None,
    )
    if feature is None:
        raise LookupError(f"Field {ctx.field_id} not found in {ctx.boundary_path}")
    geometry = feature["geometry"]
    source_crs = collection.get("crs")
    if source_crs:
        raw = source_crs.get("properties", {}).get("name", "EPSG:4326")
        # GeoJSON names are often urn:ogc:def:crs:OGC:1.3:CRS84.
        if "CRS84" in raw:
            raw = "EPSG:4326"
        field_crs = CRS.from_user_input(raw)
    else:
        field_crs = CRS.from_epsg(4326)
    if field_crs != target_crs:
        geometry = transform_geom(field_crs, target_crs, geometry)
    return geometry


def _scl_on_ndvi_grid(scl_path: Path, reference: rasterio.DatasetReader) -> np.ndarray:
    with rasterio.open(scl_path) as source:
        same_grid = (
            source.crs == reference.crs
            and source.transform == reference.transform
            and source.shape == reference.shape
        )
        if same_grid:
            return source.read(1)
        destination = np.zeros(reference.shape, dtype="uint8")
        reproject(
            source=rasterio.band(source, 1),
            destination=destination,
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=source.nodata,
            dst_transform=reference.transform,
            dst_crs=reference.crs,
            dst_nodata=0,
            resampling=Resampling.nearest,
        )
        return destination


def load_ndvi_series(ctx: FieldContext, year: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sentinel_dir = ctx.field_dir / "satellite" / "sentinel" / str(year)
    start, end = season_bounds(year)
    observations: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    if not sentinel_dir.exists():
        return observations, {
            "scene_dirs": 0,
            "in_season_scenes": 0,
            "usable_in_season": 0,
            "skipped": 0,
            "largest_gap_days": None,
            "largest_gap": None,
            "note": f"No Sentinel directory at {sentinel_dir}",
        }

    for scene_dir in sorted(p for p in sentinel_dir.iterdir() if p.is_dir()):
        acquired = _scene_date(scene_dir)
        if acquired is None:
            skipped.append({"scene_dir": str(scene_dir), "reason": "unparsed_date"})
            continue
        in_season = bool(start.date() <= acquired <= end.date())
        record: dict[str, Any] = {
            "date": acquired,
            "in_season": in_season,
            "scene_dir": str(scene_dir),
        }
        ndvi_files = sorted(scene_dir.glob("*_ndvi.tif"))
        if not ndvi_files:
            record.update({"status": "missing_ndvi", "valid_fraction": 0.0})
            observations.append(record)
            continue
        scl_files = sorted(scene_dir.glob("*_scl_epsg4326.tif"))
        if not scl_files:
            scl_files = sorted(scene_dir.glob("*_scl.tif"))
        record["ndvi_path"] = str(ndvi_files[0])
        record["scl_path"] = str(scl_files[0]) if scl_files else None

        try:
            with rasterio.open(ndvi_files[0]) as source:
                values = source.read(1).astype("float32")
                geometry = _geometry_in_crs(ctx, source.crs)
                inside = rasterize(
                    [(geometry, 1)],
                    out_shape=source.shape,
                    transform=source.transform,
                    fill=0,
                    all_touched=False,
                    dtype="uint8",
                ).astype(bool)
                if scl_files:
                    scl = _scl_on_ndvi_grid(scl_files[0], source)
                    good = np.isin(scl, MIN_VALID_SCL_CLASSES)
                else:
                    good = np.ones(source.shape, dtype=bool)
        except Exception as exc:  # raster/decode failures are a data limitation
            record.update({"status": "read_error", "note": str(exc)[:180]})
            observations.append(record)
            continue

        finite = np.isfinite(values) & (values >= -1.0) & (values <= 1.0)
        valid = inside & good & finite
        inside_count = int(inside.sum())
        valid_count = int(valid.sum())
        fraction = valid_count / inside_count if inside_count else 0.0
        record["valid_fraction"] = fraction
        record["valid_pixels"] = valid_count
        record["field_pixels"] = inside_count

        if fraction < MIN_VALID_PIXEL_FRACTION:
            record.update({
                "status": "insufficient_valid_pixels",
                "note": (
                    f"{valid_count}/{inside_count} field pixels passed the SCL "
                    f"mask ({fraction:.0%} < {MIN_VALID_PIXEL_FRACTION:.0%})"
                ),
            })
            observations.append(record)
            continue

        sample = values[valid]
        record.update({
            "status": "ok",
            "ndvi_mean": float(sample.mean()),
            "ndvi_median": float(np.median(sample)),
            "ndvi_p10": float(np.percentile(sample, 10)),
            "ndvi_p90": float(np.percentile(sample, 90)),
        })
        observations.append(record)

    for record in observations:
        if record["status"] != "ok" and record["in_season"]:
            skipped.append({
                "date": str(record["date"]),
                "scene_dir": record["scene_dir"],
                "reason": record["status"],
                "note": record.get("note"),
            })

    usable = [r for r in observations if r["status"] == "ok" and r["in_season"]]
    usable.sort(key=lambda r: r["date"])
    gaps: list[dict[str, Any]] = []
    for left, right in zip(usable, usable[1:]):
        days = (right["date"] - left["date"]).days
        if days > 0:
            gaps.append({
                "start": str(left["date"]),
                "end": str(right["date"]),
                "days": days,
            })
    largest = max(gaps, key=lambda g: g["days"]) if gaps else None
    in_season_scenes = [r for r in observations if r["in_season"]]
    coverage = {
        "scene_dirs": len(observations),
        "in_season_scenes": len(in_season_scenes),
        "usable_in_season": len(usable),
        "skipped": len(skipped),
        "largest_gap_days": largest["days"] if largest else None,
        "largest_gap": largest,
        "gaps": gaps,
        "skipped_scenes": skipped,
        "note": (
            f"{len(usable)}/{len(in_season_scenes)} in-season scenes usable"
            if in_season_scenes else "No in-season Sentinel scenes"
        ),
    }
    return usable, coverage


# ---------------------------------------------------------------------------
# Events and captions
# ---------------------------------------------------------------------------

def _fmt_date(value: pd.Timestamp | date | None, with_year: bool = False) -> str:
    if value is None:
        return "—"
    stamp = pd.Timestamp(value)
    return stamp.strftime("%d %b %Y" if with_year else "%d %b")


def _fmt_month(value: str) -> str:
    return value


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _ordinal(number: int) -> str:
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def detect_events(ctx: FieldContext, year: int, crop: dict[str, Any],
                  weather: pd.DataFrame, metrics: dict[str, Any],
                  ndvi: list[dict[str, Any]],
                  normals: pd.DataFrame | None) -> dict[str, Any]:
    season = metrics["season"]
    events: list[dict[str, Any]] = []

    # Wet start: the wettest day from May through July, which is the part of the
    # season where rainfall most directly explains a green-up. Fall back to the
    # season maximum if the early window is dry.
    early = season[(season["date"].dt.month >= 5) & (season["date"].dt.month <= 7)]
    overall_index = season["precip"].idxmax() if season["precip"].notna().any() else None
    early_index = early["precip"].idxmax() if early["precip"].notna().any() else None
    wet_index = overall_index
    if early_index is not None and overall_index is not None:
        early_mm = float(weather.loc[early_index, "precip"])
        overall_mm = float(weather.loc[overall_index, "precip"])
        # Prefer a May–July rain event for the green-up story, but only when it
        # is close to the season's heaviest day; otherwise mark the real maximum.
        if early_mm >= max(HEAVY_RAIN_MM, 0.80 * overall_mm):
            wet_index = early_index
    if wet_index is not None:
        wet_mm = float(weather.loc[wet_index, "precip"])
        if wet_mm >= HEAVY_RAIN_MM:
            events.append({
                "key": "wet_start",
                "date": str(pd.Timestamp(weather.loc[wet_index, "date"]).date()),
                "label": "heavy rain",
                "short": "rain",
                "panel": "precipitation",
                "value": wet_mm,
                "unit": "mm",
                "detail": f"{wet_mm:.1f} mm on {_fmt_date(weather.loc[wet_index, 'date'])}",
            })

    hottest = metrics["hottest"]
    if hottest["date"] is not None and hottest["value"] is not None \
            and hottest["value"] >= HOT_DAY_C:
        events.append({
            "key": "heat_peak",
            "date": str(pd.Timestamp(hottest["date"]).date()),
            "label": "peak heat",
            "short": "heat",
            "panel": "temperature",
            "value": hottest["value"],
            "unit": "°C",
            "detail": (
                f"Tmax {hottest['value']:.1f} °C on "
                f"{_fmt_date(hottest['date'])}"
            ),
        })

    # Freeze: first sub-zero minimum on/after 1 September; otherwise the
    # coldest day of the season.
    fall = season[season["date"].dt.month >= 9]
    freeze_rows = fall[fall["tmin"] <= 0.0]
    if not freeze_rows.empty:
        row = freeze_rows.iloc[0]
        events.append({
            "key": "freeze",
            "date": str(pd.Timestamp(row["date"]).date()),
            "label": "freeze",
            "short": "freeze",
            "panel": "temperature",
            "value": float(row["tmin"]),
            "unit": "°C",
            "detail": f"Tmin {row['tmin']:.1f} °C on {_fmt_date(row['date'])}",
        })

    events.sort(key=lambda e: e["date"])
    # Keep the headline set readable: drop a rain marker within three days of
    # another headline event (the caption still reports it).
    headline: list[dict[str, Any]] = []
    for event in events:
        stamp = pd.Timestamp(event["date"])
        if any(abs((stamp - pd.Timestamp(other["date"])).days) <= 3
               for other in headline):
            continue
        headline.append(event)
        if len(headline) >= 3:
            break

    # NDVI change points.
    peak = max(ndvi, key=lambda r: r["ndvi_mean"]) if ndvi else None
    rise = None
    drop = None
    for left, right in zip(ndvi, ndvi[1:]):
        delta = right["ndvi_mean"] - left["ndvi_mean"]
        days = (right["date"] - left["date"]).days
        candidate = {
            "from": left["date"], "to": right["date"],
            "delta": delta, "days": days,
        }
        if rise is None or delta > rise["delta"]:
            rise = candidate
        if drop is None or delta < drop["delta"]:
            drop = candidate

    longest_dry = metrics["dry_runs"][0] if metrics["dry_runs"] else None
    longest_heat = metrics["heat_runs"][0] if metrics["heat_runs"] else None

    return {
        "headline": headline,
        "peak": peak,
        "rise": rise,
        "drop": drop,
        "longest_dry": longest_dry,
        "longest_heat": longest_heat,
    }


def build_captions(year: int, crop: dict[str, Any], metrics: dict[str, Any],
                   ndvi: list[dict[str, Any]], detected: dict[str, Any],
                   normals: pd.DataFrame | None,
                   ndvi_coverage: dict[str, Any]) -> list[str]:
    season = metrics["season"]
    captions: list[str] = []

    # 1. Green-up / canopy shape.
    rise = detected["rise"]
    peak = detected["peak"]
    if rise and rise["delta"] > 0.08:
        between = season[
            (season["date"] >= pd.Timestamp(rise["from"]))
            & (season["date"] <= pd.Timestamp(rise["to"]))
        ]
        rain = float(between["precip"].fillna(0.0).sum())
        captions.append(
            f"Green-up: NDVI rose "
            f"{ndvi_value_at(ndvi, rise['from']):.2f} → "
            f"{ndvi_value_at(ndvi, rise['to']):.2f} between "
            f"{_fmt_date(rise['from'])} and {_fmt_date(rise['to'])}"
            + (f", with {rain:.0f} mm of rain in the interval." if rain >= 5 else ".")
        )
    elif peak:
        captions.append(
            f"Canopy: NDVI peaked at {peak['ndvi_mean']:.2f} on "
            f"{_fmt_date(peak['date'])} across {len(ndvi)} usable scenes."
        )
    else:
        captions.append(
            f"Canopy: no usable in-season NDVI scenes for {year}; "
            "the weather panels are unaffected."
        )

    # 2. Peak / heat / decline.
    parts: list[str] = []
    if peak:
        parts.append(f"peak NDVI {peak['ndvi_mean']:.2f} on {_fmt_date(peak['date'])}")
    hottest = metrics["hottest"]
    if hottest["date"] is not None and hottest["value"] is not None:
        parts.append(
            f"Tmax reached {hottest['value']:.1f} °C on "
            f"{_fmt_date(hottest['date'])}"
        )
    parts.append(f"{metrics['hot_days']} day"
                 f"{'' if metrics['hot_days'] == 1 else 's'} ≥32 °C")
    if detected["drop"] and detected["drop"]["delta"] < -0.10:
        drop = detected["drop"]
        parts.append(
            f"NDVI then fell {abs(drop['delta']):.2f} to "
            f"{ndvi_value_at(ndvi, drop['to']):.2f} by {_fmt_date(drop['to'])}"
        )
    captions.append("Midseason: " + "; ".join(parts) + ".")

    # 3. Finish and seasonal totals.
    parts = []
    freeze = next((e for e in detected["headline"] if e["key"] == "freeze"), None)
    coldest = metrics["coldest"]
    if freeze:
        parts.append(f"first freeze Tmin {freeze['value']:.1f} °C on {_fmt_date(freeze['date'])}")
        # Only echo the season-low when it belongs to the same fall cold snap;
        # a spring frost earlier in the year would read as a contradiction.
        if (coldest["date"] is not None and coldest["value"] is not None
                and str(pd.Timestamp(coldest["date"]).date()) != freeze["date"]
                and pd.Timestamp(coldest["date"]).month >= 9
                and coldest["value"] <= freeze["value"] - 1.0):
            parts.append(
                f"coldest {coldest['value']:.1f} °C on {_fmt_date(coldest['date'])}"
            )
    elif coldest["date"] is not None:
        parts.append(
            f"coldest Tmin {coldest['value']:.1f} °C on "
            f"{_fmt_date(coldest['date'])}"
        )
    rank_clause = _gdd_rank_clause(metrics["gdd_total"], normals)
    parts.append(
        f"GDD {metrics['gdd_total']:,.0f} °C-day"
        + (f", {rank_clause}" if rank_clause else "")
    )
    if ndvi:
        last = ndvi[-1]
        parts.append(
            f"NDVI {last['ndvi_mean']:.2f} at the last scene ({_fmt_date(last['date'])})"
        )
    captions.append("Finish: " + "; ".join(parts) + ".")
    return captions


def ndvi_value_at(ndvi: list[dict[str, Any]], stamp: date) -> float:
    for record in ndvi:
        if record["date"] == stamp:
            return float(record["ndvi_mean"])
    return float("nan")


def _gdd_rank_clause(total: float, normals: pd.DataFrame | None) -> str:
    if normals is None:
        return ""
    totals = getattr(normals, "attrs", {}).get("totals") or []
    if not totals:
        return ""
    ordered = sorted(totals, reverse=True)
    rank = 1 + sum(1 for value in ordered if value > total + 1e-6)
    if rank > 1 and abs(ordered[0] - total) <= 1.0:
        rank = 1
    year_count = len(ordered)
    if year_count < 2:
        return ""
    if rank == 1:
        return f"highest of {year_count} years"
    return f"{_ordinal(rank)}-highest of {year_count} years"


def coverage_note(weather_coverage: dict[str, Any],
                  ndvi_coverage: dict[str, Any]) -> str:
    gap = ndvi_coverage.get("largest_gap")
    if gap:
        gap_text = f"largest gap {gap['days']} days ({gap['start']} → {gap['end']})"
    else:
        gap_text = "no gap between usable scenes"
    skipped = ndvi_coverage.get("skipped", 0)
    return textwrap.fill(
        f"Data coverage: {ndvi_coverage['usable_in_season']}/"
        f"{ndvi_coverage['in_season_scenes']} in-season Sentinel-2 scenes usable, "
        f"{gap_text}; {skipped} scene(s) dropped by the SCL/valid-pixel filter. "
        f"Weather {weather_coverage['days_present']}/"
        f"{weather_coverage['days_expected']} season days present. "
        "NDVI is the field-mean of SCL classes 4–6 on the 10 m grid; "
        "weather is the NASA POWER grid cell, not an on-farm gauge. "
        f"GDD uses the mean method, base {GDD_BASE_C:.0f} °C.",
        width=150,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _style_axis(ax: plt.Axes, show_x: bool = False) -> None:
    ax.grid(True, which="major", color="#d9d9d9", linewidth=0.6, alpha=0.9)
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#9e9e9e")
    ax.tick_params(axis="both", labelsize=8, colors="#424242")
    if show_x:
        ax.tick_params(axis="x", labelrotation=0)
    else:
        ax.tick_params(axis="x", labelbottom=False)


def _month_axis(ax: plt.Axes, start: pd.Timestamp, end: pd.Timestamp) -> None:
    ax.set_xlim(start, end)
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    ax.xaxis.set_minor_locator(mdates.DayLocator(interval=7))
    ax.tick_params(axis="x", which="minor", length=2, color="#bdbdbd")


def render_dashboard(ctx: FieldContext, year: int, crop: dict[str, Any],
                     weather: pd.DataFrame, metrics: dict[str, Any],
                     ndvi: list[dict[str, Any]],
                     ndvi_coverage: dict[str, Any],
                     normals: pd.DataFrame | None,
                     detected: dict[str, Any],
                     captions: list[str],
                     weather_coverage: dict[str, Any],
                     output_path: Path) -> Path:
    start, end = season_bounds(year)
    season = metrics["season"]
    dates = list(season["date"])
    fig, axes = plt.subplots(
        4, 1, figsize=(13.0, 13.5), dpi=150,
        gridspec_kw={"height_ratios": [1.22, 1.0, 1.0, 1.0]},
    )
    fig.patch.set_facecolor("white")
    fig.subplots_adjust(top=0.895, bottom=0.205, left=0.075, right=0.925,
                        hspace=0.18)

    crop_name = crop.get("crop_name") or "Unknown crop"
    county = ctx.county or "county n/a"
    area = ctx.area_acres
    area_text = f"{area:,.0f} ac" if area else "area n/a"
    pct = crop.get("pct")
    pct_text = f"{pct:.1f}% {crop_name.lower()}" if pct is not None else crop_name

    fig.suptitle(
        f"Field-season storyline — {ctx.field_id} · {year} · {crop_name}",
        x=0.075, y=0.972, ha="left", fontsize=16, fontweight="bold",
        color="#1b3a2b",
    )
    fig.text(
        0.075, 0.945,
        f"{county}, IA · {area_text} · CDL {pct_text} · "
        "Sentinel-2 NDVI + NASA POWER daily weather",
        ha="left", va="top", fontsize=9.5, color="#4a4a4a",
    )

    # Headline event guide (shared vertical markers).
    headline = detected["headline"]
    event_styles = {
        "wet_start": ("#1565c0", "heavy rain"),
        "heat_peak": ("#c62828", "peak heat"),
        "freeze": ("#283593", "first freeze"),
    }
    if headline:
        handles = [
            Line2D([0], [0],
                   color=event_styles.get(event["key"], (EVENT_COLOR, "event"))[0],
                   linewidth=1.4, linestyle=(0, (3, 2)),
                   label=event_styles.get(event["key"], (EVENT_COLOR, "event"))[1])
            for event in headline
        ]
        fig.legend(
            handles=handles, loc="upper right", bbox_to_anchor=(0.925, 0.940),
            ncol=len(handles), frameon=False, fontsize=7.8,
            handlelength=1.8, columnspacing=1.4,
        )

    # ------------------------------------------------------------------
    # 01 NDVI
    # ------------------------------------------------------------------
    ax = axes[0]
    _style_axis(ax)
    _month_axis(ax, start, end)
    ndvi_dates = [pd.Timestamp(r["date"]) for r in ndvi]
    ndvi_values = [r["ndvi_mean"] for r in ndvi]
    if ndvi_dates:
        ax.plot(ndvi_dates, ndvi_values, color=NDVI_COLOR, linewidth=1.9,
                marker="o", markersize=5.5, markerfacecolor="white",
                markeredgewidth=1.4, label="Field-mean NDVI (SCL-masked)",
                zorder=4)
        ax.fill_between(ndvi_dates, ndvi_values, 0.05, color=NDVI_COLOR,
                        alpha=0.08, zorder=1)
    else:
        ax.text(0.5, 0.52, "No usable in-season NDVI scenes",
                transform=ax.transAxes, ha="center", va="center",
                fontsize=11, color="#9e9e9e")
    ax.set_ylabel("Field-mean NDVI", fontsize=9.5)
    ax.set_ylim(0.0, 0.88)
    ax.yaxis.set_major_locator(MultipleLocator(0.2))

    # Cloud/coverage gaps longer than 30 days get a neutral band and a note.
    for gap in ndvi_coverage.get("gaps", []):
        if gap["days"] > 30:
            left = pd.Timestamp(gap["start"])
            right = pd.Timestamp(gap["end"])
            ax.axvspan(left, right, color="#bdbdbd", alpha=0.18, linewidth=0, zorder=0)
            ax.text(left + (right - left) / 2, 0.80,
                    f"no usable scene\n{gap['days']} d", ha="center", va="top",
                    fontsize=7.2, color="#616161", linespacing=1.1)

    peak = detected["peak"]
    if peak:
        peak_offset = (-12, 24)
        if any(abs((pd.Timestamp(peak["date"]) - pd.Timestamp(event["date"])).days) <= 14
               for event in headline):
            peak_offset = (10, 20)
        ax.annotate(
            f"peak {peak['ndvi_mean']:.2f}",
            xy=(pd.Timestamp(peak["date"]), peak["ndvi_mean"]),
            xytext=peak_offset, textcoords="offset points",
            fontsize=8.2, color=NDVI_COLOR, fontweight="bold",
            arrowprops=dict(arrowstyle="-", color=NDVI_COLOR, lw=0.9,
                            shrinkA=0, shrinkB=3),
        )
    drop = detected["drop"]
    if drop and drop["delta"] < -0.10:
        mid = pd.Timestamp(drop["to"])
        ax.annotate(
            f"−{abs(drop['delta']):.2f} in {drop['days']} d",
            xy=(mid, ndvi_value_at(ndvi, drop["to"])),
            xytext=(-8, -34), textcoords="offset points",
            fontsize=8.2, color="#8e0000", fontweight="bold",
            arrowprops=dict(arrowstyle="->", color="#8e0000", lw=1.0,
                            connectionstyle="arc3,rad=0.18"),
        )

    # ------------------------------------------------------------------
    # 02 Precipitation
    # ------------------------------------------------------------------
    ax = axes[1]
    _style_axis(ax)
    _month_axis(ax, start, end)
    bars = ax.bar(dates, season["precip"].fillna(0.0), width=0.95,
                  color=PRECIP_COLOR, edgecolor="none", alpha=0.78,
                  label="Daily precipitation", zorder=2)
    for bar, heavy in zip(bars, season["heavy_rain"].tolist()):
        if heavy:
            bar.set_color("#0d47a1")
            bar.set_edgecolor("#0d47a1")
            bar.set_linewidth(0.5)
    ax.set_ylabel("Daily precipitation (mm)", fontsize=9.5)
    daily_max = float(season["precip"].fillna(0.0).max()) if len(season) else 0.0
    ax.set_ylim(0, max(10.0, daily_max * 1.32))
    ax2 = ax.twinx()
    ax2.plot(dates, season["precip_cum"], color=PRECIP_CUM_COLOR,
             linewidth=1.8, label="Cumulative precipitation", zorder=5)
    ax2.set_ylabel("Cumulative (mm)", fontsize=9.5, color=PRECIP_CUM_COLOR)
    ax2.tick_params(axis="y", labelsize=8, colors=PRECIP_CUM_COLOR)
    ax2.tick_params(axis="x", labelbottom=False, top=False)
    ax2.spines["top"].set_visible(False)
    ax2.spines["right"].set_color("#d7b06a")
    ax2.grid(False)

    dry = detected["longest_dry"]
    if dry and dry["days"] >= DRY_SPELL_MIN_DAYS:
        ax.axvspan(dry["start"], dry["end"], color="#ffcc80", alpha=0.22,
                   linewidth=0, zorder=0)
        ax.text(dry["start"] + (dry["end"] - dry["start"]) / 2,
                ax.get_ylim()[1] * 0.52,
                f"{dry['days']}-day dry spell", ha="center", va="center",
                rotation=90, fontsize=6.8, color="#8d6e63")

    wettest = metrics["wettest"]
    if wettest["date"] is not None and wettest["value"] is not None:
        ax.annotate(
            f"{wettest['value']:.1f} mm",
            xy=(pd.Timestamp(wettest["date"]), wettest["value"]),
            xytext=(6, 12), textcoords="offset points", fontsize=8.0,
            color="#0d47a1", fontweight="bold",
        )

    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines1 + lines2, labels1 + labels2, loc="upper left",
              frameon=False, fontsize=7.8, ncol=2)

    # ------------------------------------------------------------------
    # 03 Temperature and extremes
    # ------------------------------------------------------------------
    ax = axes[2]
    _style_axis(ax)
    _month_axis(ax, start, end)
    ax.fill_between(dates, season["tmin"], season["tmax"],
                    color="#f6c27a", alpha=0.22, linewidth=0,
                    label="Tmin–Tmax range", zorder=1)
    ax.plot(dates, season["tmax"], color=TMAX_COLOR, linewidth=1.1,
            label="Tmax", zorder=3)
    ax.plot(dates, season["tmin"], color=TMIN_COLOR, linewidth=1.1,
            label="Tmin", zorder=3)
    ax.plot(dates, season["tavg"], color=TAVG_COLOR, linewidth=1.5,
            linestyle="--", label="Tavg", zorder=4)
    ax.axhline(0, color="#1a237e", linewidth=0.8, linestyle=":", alpha=0.8)
    ax.axhline(HOT_DAY_C, color="#e53935", linewidth=0.8, linestyle=":",
               alpha=0.8)
    ax.axhline(EXTREME_HEAT_C, color="#b71c1c", linewidth=0.8, linestyle=":",
               alpha=0.8)
    ax.text(end, HOT_DAY_C + 0.7, "32 °C", ha="right", va="bottom",
            fontsize=7.0, color="#c62828")
    ax.text(end, EXTREME_HEAT_C + 0.7, "35 °C", ha="right", va="bottom",
            fontsize=7.0, color="#8e0000")
    ax.text(start, 1.0, "0 °C", ha="left", va="bottom", fontsize=7.0,
            color="#1a237e")

    hot_rows = season[season["hot"]]
    if not hot_rows.empty:
        ax.scatter(hot_rows["date"], hot_rows["tmax"], s=8, color="#c62828",
                   alpha=0.7, zorder=5, label=f"Tmax ≥{HOT_DAY_C:.0f} °C")
    frost_rows = season[season["frost"]]
    if not frost_rows.empty:
        ax.scatter(frost_rows["date"], frost_rows["tmin"], s=12, color="#1a237e",
                   marker="D", zorder=5, label="Tmin ≤0 °C")

    y_low = min(-8.0, math.floor(float(season["tmin"].min()) - 2))
    y_high = max(42.0, math.ceil(float(season["tmax"].max()) + 3))
    ax.set_ylim(y_low, y_high)
    ax.set_ylabel("Temperature (°C)", fontsize=9.5)

    hottest = metrics["hottest"]
    if hottest["date"] is not None and hottest["value"] is not None:
        ax.annotate(
            f"{hottest['value']:.1f} °C",
            xy=(pd.Timestamp(hottest["date"]), hottest["value"]),
            xytext=(-10, -20), textcoords="offset points", fontsize=8.3,
            color="#8e0000", fontweight="bold",
            arrowprops=dict(arrowstyle="-", color="#8e0000", lw=0.9),
        )
    ax.legend(loc="upper left", frameon=False, fontsize=7.4, ncol=2)

    # ------------------------------------------------------------------
    # 04 Cumulative GDD
    # ------------------------------------------------------------------
    ax = axes[3]
    _style_axis(ax, show_x=True)
    _month_axis(ax, start, end)
    if normals is not None and not normals.empty:
        ax.fill_between(normals["date"], normals["mean"] - normals["std"],
                        normals["mean"] + normals["std"], color=NORMAL_COLOR,
                        alpha=0.16, linewidth=0,
                        label="2021–2025 mean ±1 SD")
        ax.plot(normals["date"], normals["mean"], color=NORMAL_COLOR,
                linewidth=1.3, linestyle="--", label="2021–2025 mean")
    ax.plot(dates, season["gdd_cum"], color=GDD_COLOR, linewidth=2.2,
            label=f"{year} cumulative GDD", zorder=4)
    ax.set_ylabel("Cumulative GDD (°C-day)", fontsize=9.5)
    max_gdd = float(season["gdd_cum"].max()) if len(season) else 0.0
    if normals is not None and not normals.empty:
        max_gdd = max(max_gdd, float((normals["mean"] + normals["std"]).max()))
    ax.set_ylim(0, max_gdd * 1.10)
    ax.annotate(
        f"{metrics['gdd_total']:,.0f} °C-day",
        xy=(dates[-1] if dates else end, metrics["gdd_total"]),
        xytext=(-8, 12), textcoords="offset points", ha="right",
        fontsize=8.6, color=GDD_COLOR, fontweight="bold",
    )
    ax.legend(loc="upper left", frameon=False, fontsize=7.6)

    # Shared headline-event markers on every panel.
    for ax_panel in axes:
        for event in headline:
            stamp = pd.Timestamp(event["date"])
            color = event_styles.get(event["key"], (EVENT_COLOR, "event"))[0]
            ax_panel.axvline(stamp, color=color, linewidth=0.9,
                             linestyle=(0, (3, 2)), alpha=0.65, zorder=1)
        ax_panel.xaxis.set_minor_locator(mdates.DayLocator(interval=7))

    # ------------------------------------------------------------------
    # Caption block
    # ------------------------------------------------------------------
    caption_ax = fig.add_axes([0.075, 0.010, 0.855, 0.175])
    caption_ax.set_axis_off()
    y = 0.93
    for line in captions:
        wrapped = textwrap.fill(line, width=168)
        caption_ax.text(0.0, y, "• " + wrapped, ha="left", va="top",
                        fontsize=8.5, color="#263238", linespacing=1.28)
        y -= 0.25
    caption_ax.text(0.0, 0.0, coverage_note(weather_coverage, ndvi_coverage),
                    ha="left", va="bottom", fontsize=7.0, color="#607d8b",
                    linespacing=1.2)
    fig.text(0.925, 0.012, f"Field {ctx.field_id} · {year}",
             ha="right", va="bottom", fontsize=6.8, color="#90a4ae")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150, facecolor="white",
                bbox_inches=None)
    plt.close(fig)
    return output_path


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def available_years(ctx: FieldContext) -> list[int]:
    sentinel_root = ctx.field_dir / "satellite" / "sentinel"
    if not sentinel_root.exists():
        return []
    years: set[int] = set()
    for child in sentinel_root.iterdir():
        if child.is_dir() and child.name.isdigit():
            years.add(int(child.name))
    return sorted(years)


def build_year(ctx: FieldContext, year: int, out_dir: Path,
               verbose: bool = True) -> dict[str, Any]:
    crop = load_cdl_crop(ctx, year)
    weather, weather_coverage = load_weather(ctx, year)
    metrics = season_weather_metrics(weather, year)
    ndvi, ndvi_coverage = load_ndvi_series(ctx, year)
    years = available_years(ctx) or [year]
    normals = gdd_normals(ctx, years, year)
    detected = detect_events(ctx, year, crop, weather, metrics, ndvi, normals)
    captions = build_captions(year, crop, metrics, ndvi, detected, normals,
                              ndvi_coverage)

    slug = re.sub(r"[^a-z0-9]+", "-", str(crop.get("crop_name") or "crop").lower())
    slug = slug.strip("-") or "crop"
    stem = f"{ctx.field_id}_{year}_{slug}_season_dashboard"
    png_path = out_dir / f"{stem}.png"

    render_dashboard(ctx, year, crop, weather, metrics, ndvi, ndvi_coverage,
                     normals, detected, captions, weather_coverage, png_path)

    # Small sidecar tables for review; not committed to the repository.
    ndvi_frame = pd.DataFrame([
        {
            "date": str(record["date"]),
            "in_season": record["in_season"],
            "status": record["status"],
            "valid_fraction": record.get("valid_fraction"),
            "ndvi_mean": record.get("ndvi_mean"),
            "ndvi_median": record.get("ndvi_median"),
            "ndvi_p10": record.get("ndvi_p10"),
            "ndvi_p90": record.get("ndvi_p90"),
            "note": record.get("note"),
            "scene_dir": record["scene_dir"],
        }
        for record in ndvi
    ])
    ndvi_frame.to_csv(out_dir / f"{ctx.field_id}_{year}_ndvi_scenes.csv", index=False)

    weather_columns = [
        "date", "tmax", "tmin", "tavg", "precip", "gdd_daily", "gdd_cum",
        "precip_cum", "hot", "extreme", "heavy_rain", "frost", "dry",
        "in_season",
    ]
    weather[weather_columns].to_csv(
        out_dir / f"{ctx.field_id}_{year}_weather_daily.csv", index=False
    )

    event_rows: list[dict[str, Any]] = []
    for event in detected["headline"]:
        event_rows.append({
            "kind": "headline", "key": event["key"], "date": event["date"],
            "end_date": "", "label": event["label"],
            "value": event["value"], "unit": event["unit"],
            "detail": event["detail"],
        })
    for key in ("rise", "drop"):
        change = detected[key]
        if change:
            event_rows.append({
                "kind": "ndvi_change", "key": key,
                "date": str(change["from"]), "end_date": str(change["to"]),
                "label": key, "value": change["delta"], "unit": "NDVI",
                "detail": f"{change['days']} days",
            })
    dry = detected["longest_dry"]
    if dry:
        event_rows.append({
            "kind": "dry_spell", "key": "longest_dry",
            "date": str(dry["start"].date()), "end_date": str(dry["end"].date()),
            "label": "dry spell", "value": dry["days"], "unit": "days",
            "detail": f"<{DRY_DAY_MM:.0f} mm/day",
        })
    pd.DataFrame(event_rows).to_csv(
        out_dir / f"{ctx.field_id}_{year}_events.csv", index=False
    )

    summary = {
        "field_id": ctx.field_id,
        "grower": ctx.grower,
        "farm": ctx.farm,
        "county": ctx.county,
        "state_fips": ctx.state_fips,
        "area_acres": ctx.area_acres,
        "year": year,
        "crop": crop,
        "weather": {
            "season_start": weather_coverage["season_start"],
            "season_end": weather_coverage["season_end"],
            "days_present": weather_coverage["days_present"],
            "days_expected": weather_coverage["days_expected"],
            "missing_days": weather_coverage["missing_days"],
            "precip_total_mm": round(metrics["precip_total_mm"], 1),
            "gdd_total_c_day": round(metrics["gdd_total"], 0),
            "hot_days": metrics["hot_days"],
            "extreme_heat_days": metrics["extreme_days"],
            "heavy_rain_days": metrics["heavy_rain_days"],
            "frost_days": metrics["frost_days"],
            "hottest": _serialize_frame_values(metrics["hottest"]),
            "coldest": _serialize_frame_values(metrics["coldest"]),
            "wettest": _serialize_frame_values(metrics["wettest"]),
            "monthly_precip_mm": {
                key: round(value, 1)
                for key, value in metrics["monthly_precip"].items()
            },
        },
        "ndvi": {
            "usable_in_season": ndvi_coverage["usable_in_season"],
            "in_season_scenes": ndvi_coverage["in_season_scenes"],
            "largest_gap_days": ndvi_coverage["largest_gap_days"],
            "largest_gap": ndvi_coverage["largest_gap"],
            "observations": [
                {
                    "date": str(record["date"]),
                    "ndvi_mean": round(float(record["ndvi_mean"]), 3),
                    "valid_fraction": round(float(record["valid_fraction"]), 3),
                }
                for record in ndvi
            ],
            "skipped": ndvi_coverage["skipped_scenes"],
        },
        "events": detected["headline"],
        "captions": captions,
        "output_png": str(png_path),
        "sidecars": {
            "ndvi_scenes": str(out_dir / f"{ctx.field_id}_{year}_ndvi_scenes.csv"),
            "weather_daily": str(out_dir / f"{ctx.field_id}_{year}_weather_daily.csv"),
            "events": str(out_dir / f"{ctx.field_id}_{year}_events.csv"),
        },
        "inputs": {
            "boundary": str(ctx.boundary_path),
            "weather": str(ctx.weather_path),
            "cdl_table": crop.get("table"),
            "sentinel_dir": str(ctx.field_dir / "satellite" / "sentinel" / str(year)),
            "gdd_normal_years": getattr(normals, "attrs", {}).get("years", []),
        },
    }
    if verbose:
        print(
            f"{ctx.field_id} {year} {crop.get('crop_name')}: "
            f"{len(ndvi)} NDVI scenes · {metrics['gdd_total']:.0f} GDD · "
            f"{metrics['precip_total_mm']:.0f} mm -> {png_path}"
        )
    return summary


def _serialize_frame_values(payload: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, (pd.Timestamp, datetime, date)):
            output[key] = str(pd.Timestamp(value).date())
        elif isinstance(value, (np.floating, np.integer)):
            output[key] = float(value)
        else:
            output[key] = value
    return output


def build_all(data_root: str | os.PathLike[str] | None = None,
              field_id: str | None = None,
              year: str | int = 2023,
              grower: str | None = None,
              farm: str | None = None,
              out_dir: str | os.PathLike[str] | None = None,
              verbose: bool = True) -> dict[str, Any]:
    root = resolve_data_root(data_root)
    if not field_id:
        raise ValueError("field_id is required")
    ctx = find_field(root, field_id, grower=grower, farm=farm)
    out_path = Path(out_dir).expanduser().resolve() if out_dir else (
        root / "data-pipeline" / "eda" / "assignment-3"
    )
    out_path.mkdir(parents=True, exist_ok=True)

    if str(year).lower() in {"all", "*", "every"}:
        years = available_years(ctx)
    else:
        years = [int(year)]
    if not years:
        raise LookupError(f"No Sentinel years found for {ctx.field_id}")

    runs = [build_year(ctx, int(item), out_path, verbose=verbose)
            for item in sorted(years)]
    manifest = {
        "skill": "eda-field-season-dashboard",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_root": str(root),
        "pipeline": str(pipeline_root(root)),
        "field": {
            "field_id": ctx.field_id,
            "grower": ctx.grower,
            "farm": ctx.farm,
            "county": ctx.county,
            "state_fips": ctx.state_fips,
            "area_acres": ctx.area_acres,
            "boundary": str(ctx.boundary_path),
            "weather": str(ctx.weather_path),
        },
        "years": [run["year"] for run in runs],
        "runs": runs,
    }
    manifest_path = out_path / f"{ctx.field_id}_season_dashboard_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if verbose:
        print(f"manifest: {manifest_path}")
    return {
        "output_dir": str(out_path),
        "manifest": str(manifest_path),
        "field_id": ctx.field_id,
        "years": [run["year"] for run in runs],
        "runs": runs,
    }
