#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Field-level EDA for My Farm Advisor.

Compares three things — field boundaries, CDL/cropland history, and weather —
at three scales:

  1. within a field over time (where the data supports it),
  2. across fields inside one grower,
  3. across growers.

The module produces static artifacts only: PNG figures, CSV tables and a
statistical test log. It deliberately does NOT read rasters, does NOT touch
soil, and does NOT assemble a narrative report — the report is a one-time
artifact written by hand on top of these outputs.

Public entry point::

    from field_eda import build_all
    build_all(growers=["il-demo-grower", "ia-demo-grower", "ne-demo-grower"])
"""

from __future__ import annotations

import json
import math
import os
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.cm import ScalarMappable
from matplotlib.colors import Normalize
from matplotlib.lines import Line2D
from matplotlib.patches import Ellipse
from scipy import stats

try:  # geopandas is only needed for the boundary layer
    import geopandas as gpd
except ImportError:  # pragma: no cover - exercised only without geopandas
    gpd = None  # type: ignore[assignment]


FIG_DPI = 150
SEED = 42

#: Growing season used for all "season" aggregates (Apr 1 - Sep 30).
SEASON_MONTHS = (4, 5, 6, 7, 8, 9)

#: Canonical GDD convention, matching reporting/generate_weather_charts.py:
#: mean method, base 10 degC, floored at zero.
GDD_BASE_C = 10.0

#: Equal-area CRS used for area, perimeter and map geometry.
EQUAL_AREA_CRS = "EPSG:5070"

CROP_ORDER = [
    "Corn",
    "Soybeans",
    "Alfalfa",
    "Winter Wheat",
    "Grass/Pasture",
    "Forest",
    "Fallow/Idle",
]

CROP_COLORS = {
    "Corn": "#d9a441",
    "Soybeans": "#5f8f4e",
    "Alfalfa": "#7f6bb0",
    "Winter Wheat": "#c9b47a",
    "Grass/Pasture": "#8fae8b",
    "Forest": "#3f5f45",
    "Fallow/Idle": "#b8b0a0",
    "Other": "#9a9a9a",
}

PATTERN_ORDER = [
    "Corn\u2013Soy alternation",
    "Mixed rotation",
    "Continuous corn",
    "Continuous soy",
    "Other alternation",
]

GROWER_PALETTE = ["#2f6f4e", "#b3762b", "#8c3b3b", "#3f6d8c", "#6b5b95"]

MIN_PIXELS_PER_FIELD_YEAR = 200


# --------------------------------------------------------------------------
# runtime discovery
# --------------------------------------------------------------------------


def resolve_data_root(explicit: str | Path | None = None) -> Path:
    """Locate the data-pipeline runtime root.

    Order: explicit argument, ``DATA_PIPELINE_DATA_ROOT``, then the documented
    default of ``~/my-farm-advisor-runtime``.
    """
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("DATA_PIPELINE_DATA_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / "my-farm-advisor-runtime").resolve()


@dataclass(frozen=True)
class FarmContext:
    """One grower/farm pair plus its display metadata."""

    grower_slug: str
    farm_slug: str
    farm_dir: Path
    display_name: str
    state: str
    grower_name: str

    @property
    def label(self) -> str:
        state = f"{self.state} " if self.state else ""
        return f"{state}{self.display_name}".strip()

    @property
    def short(self) -> str:
        return self.state or self.grower_slug


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_contexts(
    data_root: Path, growers: Sequence[str] | None = None
) -> list[FarmContext]:
    """Discover each grower's farm(s) from the canonical runtime tree."""
    root = data_root / "data-pipeline" / "growers"
    if not root.is_dir():
        raise FileNotFoundError(f"no growers directory under {data_root}")

    if growers:
        grower_dirs = [root / g for g in growers]
    else:
        grower_dirs = sorted(p for p in root.iterdir() if (p / "farms").is_dir())

    contexts: list[FarmContext] = []
    for grower_dir in grower_dirs:
        if not grower_dir.is_dir():
            raise FileNotFoundError(f"grower not found: {grower_dir}")
        grower_meta = _read_json(grower_dir / "grower.json")
        grower_name = str(
            grower_meta.get("display_name") or grower_dir.name.replace("-", " ").title()
        )
        for farm_dir in sorted((grower_dir / "farms").iterdir()):
            if not farm_dir.is_dir():
                continue
            meta = _read_json(farm_dir / "farm.json")
            state = str(meta.get("state") or "").strip().upper()
            display = str(
                meta.get("display_name") or farm_dir.name.replace("-", " ").title()
            )
            contexts.append(
                FarmContext(
                    grower_slug=grower_dir.name,
                    farm_slug=farm_dir.name,
                    farm_dir=farm_dir,
                    display_name=display,
                    state=state,
                    grower_name=grower_name,
                )
            )
    if not contexts:
        raise FileNotFoundError("no farms discovered")
    return contexts


# --------------------------------------------------------------------------
# loaders
# --------------------------------------------------------------------------


def _first_match(directory: Path, patterns: Iterable[str]) -> Path:
    for pattern in patterns:
        hits = sorted(directory.glob(pattern))
        if hits:
            return hits[0]
    raise FileNotFoundError(f"none of {list(patterns)} under {directory}")


def load_boundaries(ctx: FarmContext) -> dict[str, Any]:
    """Return boundary metrics, normalising the two boundary schemas in use.

    CSB farms carry ``area_ha_est`` and a ``crop`` column; OSM farms carry
    ``crop_name``. Geometry-derived area is authoritative so all three farms
    are measured the same way.
    """
    if gpd is None:  # pragma: no cover
        raise RuntimeError("geopandas is required for the boundary category")

    path = ctx.farm_dir / "boundary" / "field_boundaries.geojson"
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")

    projected = gdf.to_crs(EQUAL_AREA_CRS)
    area_ha = projected.geometry.area / 10_000.0
    perimeter_m = projected.geometry.length
    shape_index = perimeter_m / (2.0 * np.sqrt(np.pi * area_ha * 10_000.0))
    vertices = gdf.geometry.apply(
        lambda geom: len(geom.exterior.coords) if geom is not None else 0
    )

    if "crop" in gdf.columns:
        crop_attr = pd.Series(gdf["crop"]).astype("string")
    elif "crop_name" in gdf.columns:
        crop_attr = pd.Series(gdf["crop_name"]).astype("string")
    else:
        crop_attr = pd.Series([pd.NA] * len(gdf), dtype="string")

    out = pd.DataFrame(
        {
            "field_id": gdf["field_id"].astype(str).to_numpy(),
            "grower_slug": ctx.grower_slug,
            "farm_slug": ctx.farm_slug,
            "grower": ctx.short,
            "farm_label": ctx.label,
            "crop_attr": crop_attr.fillna("").astype(str).to_numpy(),
            "area_ha": area_ha.to_numpy(),
            "perimeter_m": perimeter_m.to_numpy(),
            "shape_index": shape_index.to_numpy(),
            "vertex_count": vertices.to_numpy(),
            "area_ha_est": pd.to_numeric(
                gdf.get("area_ha_est", pd.Series(np.nan, index=gdf.index)),
                errors="coerce",
            ).to_numpy(),
        }
    )
    return {"table": out, "geodata": gdf, "projected": projected}


def load_cdl(ctx: FarmContext, start_year: int, end_year: int) -> pd.DataFrame:
    """Load per-year CDL composition tables and concatenate the window.

    Per-year files are used in preference to the aggregate
    ``*_full_composition.csv`` because the aggregate files in this runtime are
    inconsistent: one Illinois file is empty and one Iowa file stops at 2023.
    """
    tables = ctx.farm_dir / "derived" / "tables"
    frames: list[pd.DataFrame] = []
    for year in range(start_year, end_year + 1):
        hits = sorted(tables.glob(f"*_{year}_cdl.csv"))
        if hits:
            frames.append(pd.read_csv(hits[0]))

    if not frames:
        candidates = sorted(tables.glob("*full_composition.csv"))
        preferred = [p for p in candidates if f"{start_year}_{end_year}" in p.name]
        chosen = preferred or candidates
        if not chosen:
            raise FileNotFoundError(f"no CDL composition tables under {tables}")
        frames.append(pd.read_csv(chosen[0]))

    df = pd.concat(frames, ignore_index=True)
    df["field_id"] = df["field_id"].astype(str)
    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
    df = df[df["year"].between(start_year, end_year)].copy()
    df["pct"] = pd.to_numeric(df["pct"], errors="coerce").fillna(0.0)
    df["pixel_count"] = pd.to_numeric(
        df.get("pixel_count"), errors="coerce"
    ).fillna(0).astype("int64")
    df["crop_name"] = df["crop_name"].astype(str).str.strip()
    df["grower_slug"] = ctx.grower_slug
    df["farm_slug"] = ctx.farm_slug
    df["grower"] = ctx.short

    # A year table can legitimately repeat a crop code across sources; keep the
    # highest-percentage row per (field, year, crop) so shares never exceed 100.
    df = (
        df.sort_values(["field_id", "year", "crop_name", "pct"], ascending=[True, True, True, False])
        .drop_duplicates(subset=["field_id", "year", "crop_name"], keep="first")
        .reset_index(drop=True)
    )
    return df


def load_rotation(ctx: FarmContext) -> pd.DataFrame:
    tables = ctx.farm_dir / "derived" / "tables"
    try:
        path = _first_match(tables, ["*crop_rotation*.csv"])
    except FileNotFoundError:
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["field_id"] = df["field_id"].astype(str)
    df["grower_slug"] = ctx.grower_slug
    df["grower"] = ctx.short
    return df


def load_weather(ctx: FarmContext, start_year: int, end_year: int) -> pd.DataFrame:
    tables = ctx.farm_dir / "derived" / "tables"
    path = _first_match(tables, ["*weather*.csv"])
    df = pd.read_csv(path, parse_dates=["date"])
    df["field_id"] = df["field_id"].astype(str)
    df = df[df["date"].dt.year.between(start_year, end_year)].copy()
    df["year"] = df["date"].dt.year
    df["doy"] = df["date"].dt.dayofyear
    df["month"] = df["date"].dt.month
    df["grower_slug"] = ctx.grower_slug
    df["farm_slug"] = ctx.farm_slug
    df["grower"] = ctx.short
    for col in ("T2M", "T2M_MAX", "T2M_MIN", "PRECTOTCORR"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # canonical GDD: mean method, base 10 degC
    df["gdd"] = (
        ((df["T2M_MAX"] + df["T2M_MIN"]) / 2.0 - GDD_BASE_C).clip(lower=0.0).fillna(0.0)
    )
    return df


def load_ndvi(ctx: FarmContext) -> pd.DataFrame:
    """Per-field, per-crop NDVI summaries (yearly composites only).

    No within-season NDVI series exists in tabular form, so this feeds the
    optional cross-category bridge only.
    """
    fields_root = ctx.farm_dir / "fields"
    rows: list[dict[str, Any]] = []
    if not fields_root.is_dir():
        return pd.DataFrame(rows)

    for field_dir in sorted(p for p in fields_root.iterdir() if p.is_dir()):
        summary = _read_json(field_dir / "derived" / "summaries" / "ndvi_card_summary.json")
        cards = summary.get("cards") or {}
        for crop_key, card_key in (
            ("Corn", "corn"),
            ("Soybeans", "soybean"),
            ("Corn", "corn_peak_95"),
            ("Soybeans", "soybean_peak_95"),
        ):
            card = cards.get(card_key) or {}
            if card.get("status") != "available":
                continue
            if not card_key.endswith("peak_95"):
                continue  # peak-95 is the comparable across-year statistic
            rows.append(
                {
                    "field_id": field_dir.name,
                    "grower_slug": ctx.grower_slug,
                    "grower": ctx.short,
                    "crop": crop_key,
                    "peak95_ndvi": card.get("mean_ndvi"),
                    "scene_count": card.get("scene_count"),
                    "years": ",".join(str(y) for y in card.get("years", [])),
                }
            )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------


def season_stats(weather: pd.DataFrame) -> pd.DataFrame:
    """Per field-year growing-season aggregates."""
    season = weather[weather["month"].isin(SEASON_MONTHS)]
    grouped = season.groupby(["grower", "grower_slug", "farm_slug", "field_id", "year"])

    rows: list[dict[str, Any]] = []
    for (grower, grower_slug, farm_slug, field_id, year), g in grouped:
        g = g.sort_values("date")
        frozen = g[g["T2M_MIN"] <= 0.0]
        spring = frozen[frozen["doy"] <= 182]
        autumn = frozen[frozen["doy"] > 182]
        rows.append(
            {
                "grower": grower,
                "grower_slug": grower_slug,
                "farm_slug": farm_slug,
                "field_id": field_id,
                "year": int(year),
                "tmean_c": float(g["T2M"].mean()),
                "tmax_mean_c": float(g["T2M_MAX"].mean()),
                "precip_mm": float(g["PRECTOTCORR"].sum()),
                "gdd_total": float(g["gdd"].sum()),
                "frost_free_days": int(len(g) - len(frozen)),
                "last_spring_frost_doy": int(spring["doy"].max()) if not spring.empty else np.nan,
                "first_fall_frost_doy": int(autumn["doy"].min()) if not autumn.empty else np.nan,
            }
        )
    return pd.DataFrame(rows)


def dominant_crop_by_year(cdl: pd.DataFrame) -> pd.DataFrame:
    """Pick each field-year's dominant crop by percentage."""
    if cdl.empty:
        return pd.DataFrame(columns=["field_id", "year", "dominant_crop", "dominant_pct"])
    ordered = cdl.sort_values(
        ["field_id", "year", "pct", "crop_name"], ascending=[True, True, False, True]
    )
    top = ordered.groupby(["grower", "grower_slug", "field_id", "year"], as_index=False).first()
    return top.rename(columns={"crop_name": "dominant_crop", "pct": "dominant_pct"})


def crop_area_shares(cdl: pd.DataFrame, metrics: pd.DataFrame) -> pd.DataFrame:
    """Area-weighted crop mix, so growers with different field sizes compare."""
    area = metrics.set_index("field_id")["area_ha"]
    df = cdl.copy()
    df["crop_area_ha"] = df["field_id"].map(area).fillna(0.0) * df["pct"] / 100.0
    df["crop_bucket"] = np.where(df["crop_name"].isin(CROP_ORDER), df["crop_name"], "Other")
    agg = (
        df.groupby(["grower", "year", "crop_bucket"], as_index=False)["crop_area_ha"]
        .sum()
    )
    totals = agg.groupby(["grower", "year"])["crop_area_ha"].transform("sum")
    agg["share"] = np.where(totals > 0, agg["crop_area_ha"] / totals, 0.0)
    return agg


def classify_rotation(sequence: Sequence[str]) -> str:
    seq = [str(s) for s in sequence if isinstance(s, str) and s]
    if not seq:
        return "Mixed rotation"
    unique = set(seq)
    alternates = len(seq) >= 2 and all(a != b for a, b in zip(seq, seq[1:]))
    if alternates and unique == {"Corn", "Soybeans"}:
        return "Corn\u2013Soy alternation"
    if alternates:
        return "Other alternation"
    if unique == {"Corn"}:
        return "Continuous corn"
    if unique == {"Soybeans"}:
        return "Continuous soy"
    return "Mixed rotation"


def rotation_profile(dominant: pd.DataFrame) -> pd.DataFrame:
    """Per-field rotation characterisation plus a per-grower transition matrix."""
    rows: list[dict[str, Any]] = []
    if dominant.empty:
        return pd.DataFrame(rows)

    for (grower, grower_slug, field_id), g in dominant.groupby(
        ["grower", "grower_slug", "field_id"]
    ):
        g = g.sort_values("year")
        seq = g["dominant_crop"].tolist()
        years = g["year"].astype(int).tolist()
        transitions = list(zip(seq, seq[1:]))
        alternation = (
            float(np.mean([a != b for a, b in transitions])) if transitions else np.nan
        )
        counts = pd.Series(seq).value_counts(normalize=True)
        shannon = max(0.0, float(-(counts * np.log(counts)).sum())) if len(counts) else np.nan
        rows.append(
            {
                "grower": grower,
                "grower_slug": grower_slug,
                "field_id": field_id,
                "n_years": len(seq),
                "year_start": min(years) if years else np.nan,
                "year_end": max(years) if years else np.nan,
                "sequence": " \u2192 ".join(seq),
                "pattern": classify_rotation(seq),
                "alternation_rate": alternation,
                "shannon_diversity": shannon,
                "n_transitions": len(transitions),
            }
        )
    return pd.DataFrame(rows)


def transition_matrix(dominant: pd.DataFrame) -> pd.DataFrame:
    """Year-over-year dominant-crop transitions, row-normalised per grower."""
    rows: list[dict[str, Any]] = []
    if dominant.empty:
        return pd.DataFrame(rows)

    for (grower, grower_slug, field_id), g in dominant.groupby(
        ["grower", "grower_slug", "field_id"]
    ):
        g = g.sort_values("year")
        seq = g["dominant_crop"].tolist()
        for (a, b), year_pair in zip(
            zip(seq, seq[1:]), zip(g["year"].tolist(), g["year"].tolist()[1:])
        ):
            rows.append(
                {
                    "grower": grower,
                    "grower_slug": grower_slug,
                    "field_id": field_id,
                    "from_crop": a,
                    "to_crop": b,
                    "from_year": int(year_pair[0]),
                    "to_year": int(year_pair[1]),
                }
            )
    trans = pd.DataFrame(rows)
    if trans.empty:
        return trans
    out = (
        trans.groupby(["grower", "from_crop", "to_crop"], as_index=False)
        .size()
        .rename(columns={"size": "count"})
    )
    out["row_share"] = out["count"] / out.groupby(["grower", "from_crop"])["count"].transform("sum")
    return out


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------


def holm(pvals: Sequence[float]) -> list[float]:
    """Holm-Bonferroni step-down adjustment."""
    m = len(pvals)
    if m == 0:
        return []
    order = np.argsort(pvals)
    adjusted = [1.0] * m
    running = 0.0
    for rank, idx in enumerate(order):
        value = (m - rank) * pvals[idx]
        running = max(running, value)
        adjusted[idx] = min(1.0, running)
    return adjusted


def kruskal_test(groups: dict[str, Sequence[float]], *, metric: str) -> dict[str, Any]:
    clean = {
        k: np.asarray([v for v in vals if np.isfinite(v)], dtype=float)
        for k, vals in groups.items()
    }
    clean = {k: v for k, v in clean.items() if len(v) >= 2}
    if len(clean) < 2:
        return {"metric": metric, "ok": False, "reason": "fewer than two usable groups"}
    labels = list(clean)
    samples = [clean[k] for k in labels]
    h, p = stats.kruskal(*samples)
    n = sum(len(s) for s in samples)
    k = len(samples)
    eps2 = (h - k + 1) / (n - k) if n > k else float("nan")
    result = {
        "metric": metric,
        "ok": True,
        "test": "Kruskal-Wallis",
        "groups": ", ".join(labels),
        "n": n,
        "statistic": float(h),
        "p_value": float(p),
        "effect_size": float(eps2),
        "effect_metric": "epsilon^2",
    }
    pairs = []
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            u, pu = stats.mannwhitneyu(clean[labels[i]], clean[labels[j]], alternative="two-sided")
            pairs.append(
                {
                    "metric": metric,
                    "test": "Mann-Whitney U",
                    "groups": f"{labels[i]} vs {labels[j]}",
                    "statistic": float(u),
                    "p_value": float(pu),
                    "n": int(len(clean[labels[i]]) + len(clean[labels[j]])),
                    "n_a": int(len(clean[labels[i]])),
                    "n_b": int(len(clean[labels[j]])),
                }
            )
    for pair, adj in zip(pairs, holm([pr["p_value"] for pr in pairs])):
        pair["p_adj"] = adj
    result["pairs"] = pairs
    return result


def spearman(x: Sequence[float], y: Sequence[float]) -> dict[str, float]:
    xv = np.asarray(x, dtype=float)
    yv = np.asarray(y, dtype=float)
    mask = np.isfinite(xv) & np.isfinite(yv)
    if mask.sum() < 4:
        return {"n": int(mask.sum()), "rho": float("nan"), "p": float("nan")}
    rho, p = stats.spearmanr(xv[mask], yv[mask])
    return {"n": int(mask.sum()), "rho": float(rho), "p": float(p)}


def chi_square(table: pd.DataFrame, *, metric: str) -> dict[str, Any]:
    if table.shape[0] < 2 or table.shape[1] < 2:
        return {"metric": metric, "ok": False, "reason": "degenerate table"}
    try:
        chi2, p, dof, _expected = stats.chi2_contingency(table.to_numpy())
    except ValueError as exc:
        return {"metric": metric, "ok": False, "reason": str(exc)}
    n = float(table.to_numpy().sum())
    k = min(table.shape) - 1
    cramers_v = math.sqrt(chi2 / (n * k)) if n > 0 and k > 0 else float("nan")
    return {
        "metric": metric,
        "ok": True,
        "test": "Chi-square",
        "groups": " x ".join(str(c) for c in table.index),
        "n": int(n),
        "statistic": float(chi2),
        "p_value": float(p),
        "dof": int(dof),
        "effect_size": float(cramers_v),
        "effect_metric": "Cramers V",
    }


def variance_decomposition(values: pd.DataFrame, value_col: str) -> dict[str, float]:
    """Share of total variance that sits between growers vs between fields."""
    grand = values[value_col].mean()
    n = len(values)
    between_ss = (
        values.groupby("grower")[value_col].apply(lambda s: len(s) * (s.mean() - grand) ** 2).sum()
    )
    total_ss = ((values[value_col] - grand) ** 2).sum()
    ratio = float(between_ss / total_ss) if total_ss > 0 else float("nan")
    return {"between_share": ratio, "within_share": 1.0 - ratio if np.isfinite(ratio) else float("nan"), "n": n}


def _fmt_p(p: float) -> str:
    if not np.isfinite(p):
        return "n/a"
    if p < 0.001:
        return "p<0.001"
    return f"p={p:.3f}"


def _round_sig(value: Any, digits: int = 4) -> Any:
    """Round a float to ``digits`` significant figures, keeping it a float.

    CSV tables otherwise carry 18-digit noise that makes the artifact look
    unreviewed.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, np.integer)):
        return int(value)
    if not isinstance(value, (float, np.floating)):
        return value
    if not np.isfinite(value):
        return ""
    if value == 0:
        return 0.0
    return float(f"{value:.{digits}g}")


# --------------------------------------------------------------------------
# figure scaffolding
# --------------------------------------------------------------------------


def _save(
    fig: plt.Figure,
    out_path: Path,
    title: str,
    subtitle: str | None = None,
    note: str | None = None,
) -> Path:
    fig.suptitle(title, fontsize=13.5, fontweight="bold", x=0.012, ha="left", y=0.985)
    if subtitle:
        fig.text(
            0.012,
            0.945,
            "\n".join(textwrap.wrap(subtitle, 130)),
            fontsize=9,
            color="#3d3d3d",
            ha="left",
            va="top",
        )
    if note:
        fig.text(
            0.012,
            0.008,
            "\n".join(textwrap.wrap(note, 170)),
            fontsize=7,
            color="#6a6a6a",
            ha="left",
            va="bottom",
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=FIG_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_path


def _new_fig(
    rows: int = 1,
    cols: int = 1,
    figsize: tuple[float, float] = (10.0, 6.0),
    *,
    top: float = 0.80,
    bottom: float = 0.13,
    left: float = 0.09,
    right: float = 0.97,
    hspace: float = 0.32,
    wspace: float = 0.25,
) -> tuple[plt.Figure, Any]:
    fig, axes = plt.subplots(rows, cols, figsize=figsize)
    fig.subplots_adjust(
        top=top, bottom=bottom, left=left, right=right, hspace=hspace, wspace=wspace
    )
    return fig, axes


def _grower_colors(labels: Sequence[str]) -> dict[str, str]:
    ordered = list(dict.fromkeys(labels))
    return {label: GROWER_PALETTE[i % len(GROWER_PALETTE)] for i, label in enumerate(ordered)}


def _short_id(field_id: str) -> str:
    digits = ""
    for ch in reversed(str(field_id)):
        if ch.isdigit():
            digits = ch + digits
        else:
            break
    return digits[-2:] if digits else str(field_id)


def _nice_length(span_m: float) -> float:
    target = span_m / 5.0
    if target <= 0:
        return 1000.0
    magnitude = 10 ** math.floor(math.log10(target))
    for mult in (1.0, 2.0, 5.0, 10.0):
        if mult * magnitude >= target:
            return mult * magnitude
    return 10.0 * magnitude


def _draw_scale_bar(ax: plt.Axes) -> None:
    """Scale bar in a reserved strip below the map, never over a field.

    Drawing in axes-fraction coordinates keeps the bar and its label off the
    data: a bar placed inside the data area collided with a field polygon.
    Panel width is reported in the panel title instead.
    """
    x0, x1 = ax.get_xlim()
    span = x1 - x0
    length = _nice_length(span)
    frac_len = length / span if span else 0.2
    x_start = 0.04
    y_bar = -0.050
    ax.plot(
        [x_start, x_start + frac_len],
        [y_bar, y_bar],
        transform=ax.transAxes,
        color="#222",
        lw=3,
        solid_capstyle="butt",
        clip_on=False,
        zorder=6,
    )
    text = f"{length / 1000:.0f} km" if length >= 1000 else f"{length:.0f} m"
    ax.text(
        x_start + frac_len / 2.0,
        y_bar - 0.014,
        text,
        transform=ax.transAxes,
        ha="center",
        va="top",
        fontsize=7.5,
        color="#222",
        clip_on=False,
    )


# --------------------------------------------------------------------------
# category A: field boundaries
# --------------------------------------------------------------------------


def plot_field_area(metrics: pd.DataFrame, out: Path) -> Path:
    labels = list(dict.fromkeys(metrics["grower"]))
    colors = _grower_colors(labels)
    fig, ax = _new_fig(1, 1, (9.5, 4.6), top=0.74, bottom=0.14)

    rng = np.random.default_rng(SEED)
    for idx, label in enumerate(labels):
        vals = metrics.loc[metrics["grower"] == label, "area_ha"].to_numpy()
        bp = ax.boxplot(
            vals,
            positions=[idx],
            widths=0.42,
            vert=False,
            patch_artist=True,
            showfliers=False,
            medianprops={"color": "#222", "lw": 1.6},
            whiskerprops={"color": "#666"},
            capprops={"color": "#666"},
        )
        for patch in bp["boxes"]:
            patch.set(facecolor=colors[label], alpha=0.28, edgecolor=colors[label], lw=1.2)
        jitter = rng.uniform(-0.11, 0.11, size=len(vals))
        ax.scatter(
            vals,
            np.full_like(vals, idx, dtype=float) + jitter,
            s=34,
            color=colors[label],
            alpha=0.9,
            edgecolor="white",
            linewidth=0.6,
            zorder=4,
        )
        median = float(np.median(vals))
        cv = float(np.std(vals, ddof=1) / np.mean(vals)) if len(vals) > 1 else float("nan")
        ax.annotate(
            f"median {median:.0f} ha   CV {cv:.2f}   n={len(vals)}",
            xy=(median, idx + 0.30),
            fontsize=8,
            color="#333",
            ha="left",
            va="bottom",
        )

    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels)
    ax.set_xlabel("Field area (ha, equal-area computed)")
    ax.grid(axis="x", color="#e2e2e2", lw=0.7)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    return _save(
        fig,
        out,
        "Field size regimes differ sharply between growers",
        "Illinois and Nebraska operate CSB-scale fields (184-387 ha). Iowa is a fine-grained "
        "OSM parcel mosaic (25-80 ha). Any per-field metric must therefore be area-weighted "
        "before it is compared across growers \u2014 raw pixel counts are not comparable.",
        "Source: boundary/field_boundaries.geojson, area recomputed from geometry in EPSG:5070.",
    )


def plot_shape_vs_area(metrics: pd.DataFrame, out: Path, tests: list[dict[str, Any]]) -> Path:
    labels = list(dict.fromkeys(metrics["grower"]))
    colors = _grower_colors(labels)
    fig, ax = _new_fig(1, 1, (9.5, 5.8), top=0.78, bottom=0.14)

    blocks: list[str] = []
    for label in labels:
        sub = metrics[metrics["grower"] == label]
        ax.scatter(
            sub["area_ha"],
            sub["shape_index"],
            s=42,
            color=colors[label],
            alpha=0.85,
            edgecolor="white",
            linewidth=0.7,
            label=label,
            zorder=3,
        )
        result = spearman(sub["area_ha"], sub["shape_index"])
        tests.append(
            {
                "category": "A boundaries",
                "analysis": "A2 shape vs area",
                "comparison": f"{label}: area ~ shape index",
                "test": "Spearman",
                "statistic": result["rho"],
                "p_value": result["p"],
                "p_adj": "",
                "effect_size": "",
                "effect_metric": "rho",
                "n": result["n"],
            }
        )
        blocks.append(
            f"{label}  median {sub['shape_index'].median():.2f}   "
            f"\u03c1={result['rho']:+.2f} ({_fmt_p(result['p'])})"
        )

    ax.set_xscale("log")
    ax.set_xticks([25, 30, 40, 50, 70, 100, 150, 200, 300, 400])
    ax.set_xticklabels(["25", "30", "40", "50", "70", "100", "150", "200", "300", "400"])
    ax.xaxis.set_minor_formatter(plt.NullFormatter())
    ax.axhline(1.0, color="#999", lw=1, ls="--")
    ax.text(
        25.5,
        1.008,
        "1.0 = perfect circle",
        fontsize=7.5,
        color="#777",
        va="bottom",
    )
    ax.text(
        0.02,
        0.975,
        "Shape index by grower\n" + "\n".join(blocks),
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=8.5,
        family="monospace",
        linespacing=1.5,
        bbox={"facecolor": "white", "edgecolor": "#dddddd", "boxstyle": "round,pad=0.5"},
    )
    ax.set_xlabel("Field area (ha, log scale)")
    ax.set_ylabel("Shape index  P / (2\u221a(\u03c0A))")
    ax.grid(color="#ececec", lw=0.7)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    return _save(
        fig,
        out,
        "Boundary irregularity follows data provenance, not field size",
        "Within every grower, area says nothing about shape (\u03c1 \u2248 0, all p > 0.6). What "
        "separates them is where the polygon came from: Iowa's mapper-drawn OSM farmland "
        "polygons are the most compact (median 1.26), while CSB's real management boundaries "
        "are more elongated and irregular (1.76 and 1.71). So shape differences here are a "
        "data-source artefact, not an agronomic one.",
        "Shape index = perimeter / (2\u221a(\u03c0\u00b7area)); 1.0 is a circle, values rise with "
        "boundary irregularity. Computed in EPSG:5070 over n=10 fields per grower.",
    )


def plot_field_map(
    boundaries: dict[Any, dict[str, Any]], out: Path, warnings: list[str]
) -> Path:
    contexts = list(boundaries)
    all_areas = pd.concat([boundaries[c]["table"] for c in contexts])["area_ha"]
    norm = Normalize(vmin=float(all_areas.min()), vmax=float(all_areas.max()))
    cmap = plt.get_cmap("viridis")

    ncols = len(contexts)
    fig, axes = _new_fig(
        1,
        ncols,
        (4.4 * ncols, 5.7),
        top=0.75,
        bottom=0.11,
        left=0.02,
        right=0.90,
        wspace=0.06,
    )
    axes_list = np.atleast_1d(axes)
    for ax, ctx in zip(axes_list, contexts):
        projected = boundaries[ctx]["projected"]
        table = boundaries[ctx]["table"]
        projected.plot(
            ax=ax,
            color=[cmap(norm(a)) for a in table["area_ha"]],
            edgecolor="#1f1f1f",
            linewidth=0.8,
        )
        minx, miny, maxx, maxy = projected.total_bounds
        pad = max(maxx - minx, maxy - miny) * 0.05
        ax.set_aspect("equal", adjustable="box")
        ax.autoscale(False)
        ax.set_xlim(minx - pad, maxx + pad)
        ax.set_ylim(miny - pad, maxy + pad)
        ax.set_axis_off()
        ax.set_title(
            f"{ctx.label}\n{len(table)} fields \u00b7 {table['area_ha'].sum():.0f} ha "
            f"\u00b7 panel {(maxx - minx) / 1000:.0f} km wide",
            fontsize=9.5,
            loc="left",
        )
        _draw_scale_bar(ax)

    sm = ScalarMappable(norm=norm, cmap=cmap)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=list(axes_list), fraction=0.03, pad=0.01)
    cbar.set_label("Field area (ha)", fontsize=8)
    cbar.ax.tick_params(labelsize=7)

    return _save(
        fig,
        out,
        "Three farms, three patterns of land subdivision",
        "Every panel carries its own scale bar and panel width \u2014 read those, not the panel "
        "sizes, because the panels are not at equal zoom. Illinois and Nebraska are blocky "
        "quarter-section fields across a county; Iowa is a dense mesh of much smaller parcels.",
        "Geometry in EPSG:5070. Colour is field area on one shared scale across all three "
        "panels, so the Iowa mosaic reads as uniformly small.",
    )


# --------------------------------------------------------------------------
# category B: CDL / cropland
# --------------------------------------------------------------------------


def plot_crop_mix(shares: pd.DataFrame, out: Path) -> Path:
    growers = list(dict.fromkeys(shares["grower"]))
    years = sorted(shares["year"].unique())
    present = [c for c in CROP_ORDER if c in set(shares["crop_bucket"])]
    if "Other" in set(shares["crop_bucket"]):
        present.append("Other")

    # Only label crops that are actually visible; the rest stay as slivers but
    # would clutter the legend (Alfalfa, Winter Wheat, Forest, Fallow < 0.5%).
    mean_share = shares.groupby("crop_bucket")["share"].mean()
    legend_crops = {c for c in present if float(mean_share.get(c, 0.0)) >= 0.005}

    fig, axes = _new_fig(
        len(growers),
        1,
        (9.8, 1.95 * len(growers) + 1.3),
        top=0.85,
        bottom=0.10,
        left=0.08,
        right=0.78,
        hspace=0.42,
    )
    axes_list = np.atleast_1d(axes)

    ratios: dict[str, tuple[float, float]] = {}
    for ax, grower in zip(axes_list, growers):
        sub = shares[shares["grower"] == grower]
        bottoms = np.zeros(len(years))
        for crop in present:
            vals = np.array(
                [
                    float(sub.loc[(sub["year"] == y) & (sub["crop_bucket"] == crop), "share"].sum())
                    * 100.0
                    for y in years
                ]
            )
            ax.bar(
                [str(y) for y in years],
                vals,
                bottom=bottoms,
                color=CROP_COLORS.get(crop, "#999"),
                edgecolor="white",
                linewidth=0.6,
                label=crop if (grower == growers[0] and crop in legend_crops) else None,
            )
            bottoms += vals
        corn = float(
            sub.loc[sub["crop_bucket"] == "Corn", "share"].sum() / max(sub["year"].nunique(), 1)
        ) * 100.0
        soy = float(
            sub.loc[sub["crop_bucket"] == "Soybeans", "share"].sum() / max(sub["year"].nunique(), 1)
        ) * 100.0
        ratios[grower] = (corn, soy)
        ax.set_ylim(0, 100)
        ax.set_yticks([0, 25, 50, 75, 100])
        ax.set_ylabel("share of area (%)", fontsize=8)
        ax.set_title(
            f"{grower}   corn {corn:.0f}%  :  soy {soy:.0f}%", fontsize=9.5, loc="left", pad=2
        )
        ax.tick_params(labelsize=8)
        ax.grid(axis="y", color="#ececec", lw=0.6)
        ax.set_axisbelow(True)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)

    handles, names = axes_list[0].get_legend_handles_labels()
    fig.legend(
        handles,
        names,
        loc="center left",
        bbox_to_anchor=(0.80, 0.50),
        frameon=False,
        fontsize=8,
        title="Crop",
        title_fontsize=8.5,
    )

    return _save(
        fig,
        out,
        "A two-crop system everywhere \u2014 the corn:soy balance is what moves",
        "Corn plus soybeans cover at least 98% of every farm in every year (IA 99.9%, IL 98.8%, "
        "NE 99.3% on average), so the non-crop residual is not a differentiator. The real "
        "difference is the ratio: Iowa runs close to 54:46, Illinois 59:40, Nebraska 66:33. Corn "
        "intensity rises westward alongside the hotter, drier climate in C1 \u2014 an association, "
        "not an established cause.",
        "Area-weighted (field geometry area \u00d7 CDL percentage), so farms of different field "
        "sizes compare on equal footing. Percentages in each subplot title are across-year means; "
        "legend shows crops above 0.5% of area.",
    )


def alternation_contingency(profile: pd.DataFrame) -> pd.DataFrame:
    """2 x k table of full alternation vs not, per grower."""
    flags = np.where(profile["alternation_rate"].fillna(0.0) >= 1.0, "full alternation", "other")
    return pd.crosstab(profile["grower"], flags)


def plot_rotation_patterns(profile: pd.DataFrame, out: Path) -> Path:
    growers = list(dict.fromkeys(profile["grower"]))
    colors = _grower_colors(growers)
    patterns = [p for p in PATTERN_ORDER if p in set(profile["pattern"])]
    patterns += [p for p in sorted(set(profile["pattern"])) if p not in patterns]

    fig, (ax_left, ax_right) = _new_fig(
        1, 2, (11.0, 5.4), top=0.78, bottom=0.16, left=0.09, right=0.97, wspace=0.30
    )

    y = np.arange(len(patterns))
    height = 0.8 / max(len(growers), 1)
    for i, grower in enumerate(growers):
        sub = profile[profile["grower"] == grower]
        counts = [int((sub["pattern"] == p).sum()) for p in patterns]
        ax_left.barh(
            y + (i - (len(growers) - 1) / 2) * height,
            counts,
            height=height,
            color=colors[grower],
            edgecolor="white",
            linewidth=0.6,
            label=grower,
        )
        for yy, count in zip(y + (i - (len(growers) - 1) / 2) * height, counts):
            if count:
                ax_left.text(count + 0.1, yy, str(count), va="center", fontsize=8, color="#333")

    ax_left.set_yticks(y)
    ax_left.set_yticklabels(patterns, fontsize=9)
    ax_left.invert_yaxis()
    ax_left.set_xlabel("Fields (n=10 per grower)")
    ax_left.set_title("Rotation pattern by field", fontsize=10, loc="left")
    ax_left.legend(frameon=False, fontsize=8, loc="lower right")
    ax_left.grid(axis="x", color="#ececec", lw=0.6)
    ax_left.set_axisbelow(True)
    for spine in ("top", "right"):
        ax_left.spines[spine].set_visible(False)

    rng = np.random.default_rng(SEED)
    for i, grower in enumerate(growers):
        sub = profile[profile["grower"] == grower]
        vals = sub["shannon_diversity"].to_numpy(dtype=float)
        # Strip plot, not a boxplot: with 10 fields and heavy ties a box hides
        # the fact that the values cluster at 0, 0.50 and 0.67.
        ax_right.scatter(
            np.full_like(vals, i, dtype=float) + rng.uniform(-0.09, 0.09, len(vals)),
            vals,
            s=46,
            color=colors[grower],
            alpha=0.9,
            edgecolor="white",
            linewidth=0.7,
            zorder=3,
        )
        median = float(np.median(vals))
        ax_right.plot([i - 0.24, i + 0.24], [median, median], color="#222", lw=1.8, zorder=4)
        ax_right.annotate(
            f"median {median:.2f}",
            xy=(i + 0.26, median),
            va="center",
            ha="left",
            fontsize=7.5,
            color="#333",
        )

    ax_right.axhline(0.0, color="#bbb", lw=0.9, ls=":")
    ax_right.axhline(float(np.log(2)), color="#bbb", lw=0.9, ls=":")
    ax_right.text(
        -0.44,
        0.008,
        "0 = one crop every year",
        fontsize=7.5,
        color="#777",
        ha="left",
        va="bottom",
    )
    ax_right.text(
        -0.44,
        float(np.log(2)) + 0.008,
        "0.67 = clean two-crop alternation",
        fontsize=7.5,
        color="#777",
        ha="left",
        va="bottom",
    )
    ax_right.set_xlim(-0.5, len(growers) - 0.35)
    ax_right.set_ylim(-0.03, 0.76)

    ax_right.set_xticks(range(len(growers)))
    ax_right.set_xticklabels(growers)
    ax_right.set_ylabel("Shannon diversity of crops")
    ax_right.set_title("Diversity per field (each dot is one field)", fontsize=10, loc="left")
    ax_right.grid(axis="y", color="#ececec", lw=0.6)
    ax_right.set_axisbelow(True)
    for spine in ("top", "right"):
        ax_right.spines[spine].set_visible(False)

    alt = chi_square(alternation_contingency(profile), metric="full alternation x grower")
    alt_p = float(alt["p_value"]) if alt.get("ok") else float("nan")
    diversity = kruskal_test(
        {g: s["shannon_diversity"].to_numpy() for g, s in profile.groupby("grower")},
        metric="rotation diversity",
    )
    diversity_p = float(diversity["p_value"]) if diversity.get("ok") else float("nan")

    return _save(
        fig,
        out,
        "Illinois is uniformly rotated; Nebraska is a portfolio of regimes",
        "Illinois runs a clean corn\u2013soy alternation on 9 of 10 fields, so for Illinois the "
        "rotation really is a grower-level fact. Iowa alternates on 6 (plus 2 continuous-corn and "
        "2 mixed); Nebraska alternates on 4 and runs 3 continuous-corn and 3 mixed fields, so "
        "'the Nebraska rotation' is not a single thing. The grower differences are descriptive "
        f"\u2014 just short of significance at this sample size (alternation \u03c7\u00b2 "
        f"{_fmt_p(alt_p)}; diversity Kruskal\u2013Wallis {_fmt_p(diversity_p)}; n = 10 fields per "
        "grower).",
        "Pattern derived from each field's 5-year sequence of dominant CDL crops (2021\u20132025). "
        "Diversity is Shannon entropy over those dominant crops; dots are jittered horizontally "
        "to separate ties, black bar is the grower median.",
    )


def plot_transition_matrix(transitions: pd.DataFrame, out: Path) -> Path:
    """Year-over-year crop transitions as small-multiple probability matrices."""
    growers = list(dict.fromkeys(transitions["grower"]))
    present = set(transitions["from_crop"]) | set(transitions["to_crop"])
    crops = [c for c in CROP_ORDER if c in present]
    crops += [c for c in sorted(present) if c not in crops]
    short = {
        "Corn": "Corn",
        "Soybeans": "Soy",
        "Alfalfa": "Alfalfa",
        "Winter Wheat": "Wheat",
        "Grass/Pasture": "Grass",
        "Forest": "Forest",
        "Fallow/Idle": "Fallow",
    }

    ncols = len(growers)
    fig, axes = _new_fig(
        1,
        ncols,
        (4.0 * ncols, 4.5),
        top=0.70,
        bottom=0.20,
        left=0.09,
        right=0.86,
        wspace=0.38,
    )
    axes_list = np.atleast_1d(axes)
    cmap = plt.get_cmap("Blues")

    for ax, grower in zip(axes_list, growers):
        sub = transitions[transitions["grower"] == grower]
        share = np.zeros((len(crops), len(crops)))
        count = np.zeros((len(crops), len(crops)), dtype=int)
        for _, row in sub.iterrows():
            i = crops.index(row["from_crop"])
            j = crops.index(row["to_crop"])
            share[i, j] = float(row["row_share"])
            count[i, j] = int(row["count"])

        ax.imshow(share, cmap=cmap, vmin=0.0, vmax=1.0, aspect="auto")

        for i in range(len(crops)):
            for j in range(len(crops)):
                value = share[i, j]
                label = f"{value:.2f}\n({count[i, j]})" if count[i, j] else "0.00\n(0)"
                ax.text(
                    j,
                    i,
                    label,
                    ha="center",
                    va="center",
                    fontsize=9.5,
                    color="white" if value > 0.55 else "#222",
                )

        ax.set_xticks(range(len(crops)))
        ax.set_xticklabels([f"\u2192 {short.get(c, c)}" for c in crops], fontsize=9)
        ax.set_yticks(range(len(crops)))
        ax.set_yticklabels([short.get(c, c) for c in crops], fontsize=9)
        ax.set_xticks(np.arange(-0.5, len(crops), 1), minor=True)
        ax.set_yticks(np.arange(-0.5, len(crops), 1), minor=True)
        ax.grid(which="minor", color="white", lw=1.4)
        ax.tick_params(which="minor", length=0)
        ax.set_title(
            f"{grower}   n={int(count.sum())} transitions", fontsize=10, loc="left"
        )
        ax.set_xlabel("crop in year t+1", fontsize=8.5)
        if ax is axes_list[0]:
            ax.set_ylabel("crop in year t", fontsize=8.5)
        for spine in ax.spines.values():
            spine.set_visible(False)

    sm = ScalarMappable(norm=Normalize(vmin=0.0, vmax=1.0), cmap=cmap)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=list(axes_list), fraction=0.025, pad=0.015)
    cbar.set_label("P(next crop | current crop)", fontsize=8)
    cbar.ax.tick_params(labelsize=7)

    trans_types = transitions.assign(
        transition=transitions["from_crop"] + "\u2192" + transitions["to_crop"]
    )
    contingency = (
        trans_types.pivot_table(
            index="transition", columns="grower", values="count", aggfunc="sum"
        )
        .fillna(0)
    )
    homogeneity = chi_square(contingency, metric="crop transition x grower")

    carry = (
        transitions[
            (transitions["from_crop"] == "Corn") & (transitions["to_crop"] == "Corn")
        ]
        .set_index("grower")["row_share"]
        .reindex(growers)
    )
    carry_text = ", ".join(f"{g} {v:.2f}" for g, v in carry.items() if np.isfinite(v))
    soy_corn = transitions[
        (transitions["from_crop"] == "Soybeans") & (transitions["to_crop"] == "Corn")
    ]["row_share"]
    if not soy_corn.empty and np.isclose(soy_corn.min(), 1.0) and np.isclose(soy_corn.max(), 1.0):
        soy_text = "soy\u2192corn is 1.00 in every grower"
    elif not soy_corn.empty:
        soy_text = f"soy\u2192corn ranges {soy_corn.min():.2f}\u2013{soy_corn.max():.2f}"
    else:
        soy_text = ""
    chi_text = (
        f"\u03c7\u00b2 {_fmt_p(float(homogeneity['p_value']))}"
        if homogeneity.get("ok")
        else "chi-square unavailable"
    )

    return _save(
        fig,
        out,
        "Corn follows corn far more often in Nebraska than in Illinois",
        "Each cell is the probability of next year's crop given this year's crop, for that grower; "
        f"counts in brackets. Corn\u2192corn carryover: {carry_text}. {soy_text}, so the rotation "
        "difference is entirely in how often corn repeats. Across the three matrices "
        f"{chi_text}, but transitions inside one field are not independent, so treat that as "
        "descriptive rather than a formal test.",
        "Derived from each field's 5-year sequence of dominant CDL crops (2021\u20132025); rows are "
        "normalised within each grower, so a row sums to 1.00. Empty cells mean the transition never "
        "occurred in the window.",
    )


# --------------------------------------------------------------------------
# category C: weather
# --------------------------------------------------------------------------
def _ellipse_for(points: np.ndarray, **kwargs: Any) -> Ellipse | None:
    if len(points) < 3:
        return None
    cov = np.cov(points, rowvar=False)
    if not np.all(np.isfinite(cov)):
        return None
    eigvals, eigvecs = np.linalg.eigh(cov)
    eigvals = np.clip(eigvals, 1e-9, None)
    order = eigvals.argsort()[::-1]
    eigvals, eigvecs = eigvals[order], eigvecs[:, order]
    angle = math.degrees(math.atan2(eigvecs[1, 0], eigvecs[0, 0]))
    width, height = 2.0 * np.sqrt(eigvals) * 1.5  # 1.5 SD ~ 86% of a normal cloud
    return Ellipse(
        xy=points.mean(axis=0),
        width=float(width),
        height=float(height),
        angle=float(angle),
        fill=False,
        **kwargs,
    )


def plot_climate_envelope(season: pd.DataFrame, out: Path) -> Path:
    growers = list(dict.fromkeys(season["grower"]))
    colors = _grower_colors(growers)
    fig, ax = _new_fig(1, 1, (9.8, 6.0), top=0.76, bottom=0.14, right=0.97)

    ranges: dict[str, tuple[float, float]] = {}
    for grower in growers:
        sub = season[season["grower"] == grower]
        ax.scatter(
            sub["tmean_c"],
            sub["precip_mm"],
            s=30,
            color=colors[grower],
            alpha=0.55,
            edgecolor="none",
            label=f"{grower}  (n={len(sub)})",
        )
        pts = sub[["tmean_c", "precip_mm"]].to_numpy(dtype=float)
        ellipse = _ellipse_for(
            pts, edgecolor=colors[grower], linewidth=1.5, linestyle="--", alpha=0.8, zorder=3
        )
        if ellipse is not None:
            ax.add_patch(ellipse)
        centroid = pts.mean(axis=0)
        ax.scatter(
            [centroid[0]],
            [centroid[1]],
            marker="X",
            s=190,
            color=colors[grower],
            edgecolor="white",
            linewidth=1.2,
            zorder=5,
        )
        ax.annotate(
            grower,
            xy=(centroid[0], centroid[1]),
            xytext=(9, -14),
            textcoords="offset points",
            fontsize=9.5,
            fontweight="bold",
            color=colors[grower],
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.75, "pad": 1.0},
        )
        ranges[grower] = (float(sub["tmean_c"].min()), float(sub["tmean_c"].max()))

    temp_share = variance_decomposition(season, "tmean_c")["between_share"]
    precip_share = variance_decomposition(season, "precip_mm")["between_share"]
    ax.text(
        0.985,
        0.975,
        f"variance explained by grower\n"
        f"temperature   {temp_share:.2f}\n"
        f"precipitation {precip_share:.2f}",
        transform=ax.transAxes,
        ha="right",
        va="top",
        fontsize=8.5,
        family="monospace",
        linespacing=1.6,
        bbox={"facecolor": "white", "edgecolor": "#dddddd", "boxstyle": "round,pad=0.5"},
    )

    ax.set_xlabel("Growing-season mean temperature (\u00b0C, Apr\u2013Sep)")
    ax.set_ylabel("Growing-season precipitation (mm, Apr\u2013Sep)")
    ax.grid(color="#ececec", lw=0.7)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, fontsize=8.5, loc="lower left")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    band_text = " / ".join(
        f"{g} {lo:.1f}\u2013{hi:.1f}" for g, (lo, hi) in ranges.items()
    )
    return _save(
        fig,
        out,
        "Temperature separates the growers; precipitation does not",
        "Each grower sits in its own temperature band ("
        f"{band_text} \u00b0C) and grower alone explains {temp_share:.0%} of temperature "
        "variance. Precipitation has no such structure \u2014 only "
        f"{precip_share:.0%} \u2014 because every grower spans roughly 340\u2013800 mm "
        "depending on the year. So precipitation separates years, not places, and is a poor "
        "cross-grower discriminator.",
        "One point per field-year (10 fields \u00d7 5 years per grower). Dashed ellipses are 1.5 SD; "
        "X marks the grower centroid. NASA POWER at each field centroid.",
    )


def plot_season_trends(
    season: pd.DataFrame, out: Path, span_text: str | None = None
) -> Path:
    """Within-field time comparison: each field's own 5-year trajectory.

    Two different scales are at work: rainfall is almost entirely a year
    effect, while heat carries a year term and a comparably large place term.
    """
    growers = list(dict.fromkeys(season["grower"]))
    colors = _grower_colors(growers)
    years = sorted(int(y) for y in season["year"].unique())

    fig, axes = _new_fig(
        1, 2, (11.4, 5.6), top=0.78, bottom=0.15, left=0.07, right=0.98, wspace=0.22
    )
    left, right = axes

    specs = [
        (left, "precip_mm", "Season precipitation (mm)", "mm", (0.03, 0.97, "top")),
        (right, "gdd_total", "Season GDD (\u00b0C-day)", "GDD", (0.03, 0.05, "bottom")),
    ]
    notes: list[str] = []
    summary: dict[str, tuple[float, float, float]] = {}

    for ax, column, ylabel, unit, (tx, ty, va) in specs:
        for grower in growers:
            sub = season[season["grower"] == grower]
            for _, field in sub.groupby("field_id"):
                field = field.sort_values("year")
                ax.plot(
                    field["year"],
                    field[column],
                    color=colors[grower],
                    alpha=0.30,
                    lw=1.1,
                    marker="o",
                    ms=2.6,
                    zorder=2,
                )
            mean_curve = sub.groupby("year")[column].mean().reindex(years)
            ax.plot(
                mean_curve.index,
                mean_curve.to_numpy(),
                color=colors[grower],
                lw=2.8,
                marker="o",
                ms=5,
                label=grower,
                zorder=4,
            )

        year_means = season.groupby("year")[column].mean()
        year_range = float(year_means.max() - year_means.min())
        per_farm_year = season.groupby(["grower", "year"])[column].agg(lambda s: s.max() - s.min())
        field_gap = float(per_farm_year.max())
        typical_gap = float(per_farm_year.median())
        notes.append(
            f"{unit}: year effect {year_range:.0f} vs field gap max {field_gap:.0f}"
        )
        summary[column] = (year_range, field_gap, typical_gap)

        ax.set_xticks(years)
        ax.set_xticklabels([str(y) for y in years])
        ax.set_xlabel("Year")
        ax.set_ylabel(ylabel)
        ax.grid(color="#ececec", lw=0.7)
        ax.set_axisbelow(True)
        ax.text(
            tx,
            ty,
            f"year-to-year range  {year_range:.0f}\n"
            f"max within-farm field gap  {field_gap:.0f}\n"
            f"median within-farm gap  {typical_gap:.0f}",
            transform=ax.transAxes,
            va=va,
            ha="left",
            fontsize=8,
            family="monospace",
            linespacing=1.5,
            bbox={"facecolor": "white", "edgecolor": "#dddddd", "boxstyle": "round,pad=0.45"},
        )
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)

    left.legend(frameon=False, fontsize=8.5, loc="lower right")

    distinct = []
    for grower in growers:
        sub = season[season["grower"] == grower]
        signatures = sub.sort_values("year").groupby("field_id")["precip_mm"].apply(
            lambda s: tuple(np.round(s.to_numpy(), 1))
        )
        distinct.append(f"{grower} {signatures.nunique()}/{len(signatures)}")
    distinct_text = ", ".join(distinct)

    precip_year = season.groupby("year")["precip_mm"].mean()
    p_year, p_gap, p_typ = summary["precip_mm"]
    g_year, g_gap, g_typ = summary["gdd_total"]
    p_ratio = p_year / p_typ if p_typ else float("nan")
    g_ratio = g_year / g_typ if g_typ else float("nan")
    span_clause = (
        f" Farms span {span_text}, so part of the within-farm spread is grid cell, not microclimate."
        if span_text
        else ""
    )
    return _save(
        fig,
        out,
        "Rainfall is a year story; heat is a year-and-place story",
        f"Rainfall swings {precip_year.min():.0f}\u2192{precip_year.max():.0f} mm between years "
        f"(a {p_year:.0f} mm year effect) \u2014 {p_ratio:.0f}\u00d7 the {p_typ:.0f} mm median gap "
        f"between fields of the same farm (max {p_gap:.0f}). Heat is different: its year range is "
        f"{g_year:.0f} \u00b0C-day, only {g_ratio:.1f}\u00d7 the {g_typ:.0f} \u00b0C-day median "
        f"within-farm field gap (max {g_gap:.0f}), so GDD carries both a year and a place term."
        f"{span_clause}",
        "Each thin line is one field's own five-year seasonal total \u2014 this is the within-field "
        "time comparison. Bold lines are grower means. Distinct five-year precipitation series per "
        f"farm: {distinct_text} \u2014 NASA POWER's grid is coarser than the farm.",
    )


# --------------------------------------------------------------------------
# optional bridge: weather x canopy
# --------------------------------------------------------------------------


def plot_weather_ndvi_bridge(bridge: pd.DataFrame, out: Path) -> Path:
    growers = list(dict.fromkeys(bridge["grower"]))
    if not growers:
        raise ValueError("bridge data is empty")

    colors = _grower_colors(growers)
    marker_for = {"Corn": "o", "Soybeans": "^"}
    crops = [c for c in ("Corn", "Soybeans") if c in set(bridge["crop"])]

    def _pooled(crop: str) -> dict[str, float]:
        s = bridge[bridge["crop"] == crop]
        return spearman(s["mean_gdd"], s["peak95_ndvi"])

    def _fit(ax: plt.Axes, s: pd.DataFrame, *, ls: str, color: str = "#444") -> None:
        x = s["mean_gdd"].to_numpy(dtype=float)
        y = s["peak95_ndvi"].to_numpy(dtype=float)
        mask = np.isfinite(x) & np.isfinite(y)
        if mask.sum() < 4:
            return
        slope, intercept = np.polyfit(x[mask], y[mask], 1)
        grid = np.linspace(x[mask].min(), x[mask].max(), 40)
        ax.plot(grid, intercept + slope * grid, color=color, lw=1.4, ls=ls, alpha=0.85, zorder=2)

    fig = plt.figure(figsize=(12.8, 8.4))
    grid = fig.add_gridspec(
        2,
        3,
        height_ratios=[1.05, 1.0],
        hspace=0.50,
        wspace=0.30,
        top=0.80,
        bottom=0.10,
        left=0.07,
        right=0.98,
    )
    ax_pool = fig.add_subplot(grid[0, :])

    pooled = {crop: _pooled(crop) for crop in crops}
    for grower in growers:
        for crop in crops:
            s = bridge[(bridge["grower"] == grower) & (bridge["crop"] == crop)]
            if s.empty:
                continue
            ax_pool.scatter(
                s["mean_gdd"],
                s["peak95_ndvi"],
                marker=marker_for[crop],
                s=52,
                color=colors[grower],
                alpha=0.85,
                edgecolor="white",
                linewidth=0.7,
                zorder=3,
            )
    for crop, ls in zip(crops, ("-", "--")):
        _fit(ax_pool, bridge[bridge["crop"] == crop], ls=ls)

    ax_pool.set_title(
        "All fields pooled \u2014 three disjoint climate clusters, no corn slope",
        fontsize=10.5,
        loc="left",
    )
    ax_pool.set_xlabel("Mean season GDD in crop years (\u00b0C-day)")
    ax_pool.set_ylabel("Peak-95 NDVI")
    ax_pool.grid(color="#ececec", lw=0.7)
    ax_pool.set_axisbelow(True)
    for spine in ("top", "right"):
        ax_pool.spines[spine].set_visible(False)

    # Two-part key: grower = colour, crop = marker.
    handles = [
        Line2D([], [], marker="s", ls="", color=colors[g], label=g) for g in growers
    ] + [
        Line2D([], [], marker=marker_for[c], ls="", color="#666", label=c) for c in crops
    ]
    ax_pool.legend(handles=handles, frameon=False, fontsize=8.5, loc="lower right", ncol=2)

    lines = []
    for crop in crops:
        res = pooled[crop]
        glyph = "\u25cf" if crop == "Corn" else "\u25b2"
        lines.append(f"{glyph} {crop[:3].lower()}  \u03c1={res['rho']:+.2f}  n={res['n']}  ({_fmt_p(res['p'])})")
    ax_pool.text(
        0.015,
        0.97,
        "pooled\n" + "\n".join(lines),
        transform=ax_pool.transAxes,
        va="top",
        ha="left",
        fontsize=8.5,
        family="monospace",
        linespacing=1.55,
        bbox={"facecolor": "white", "edgecolor": "#dddddd", "boxstyle": "round,pad=0.5"},
    )

    for col, grower in enumerate(growers):
        ax = fig.add_subplot(grid[1, col])
        sub = bridge[bridge["grower"] == grower]
        for crop in crops:
            s = sub[sub["crop"] == crop]
            if s.empty:
                continue
            ax.scatter(
                s["mean_gdd"],
                s["peak95_ndvi"],
                marker=marker_for[crop],
                s=52,
                color=colors[grower],
                alpha=0.85,
                edgecolor="white",
                linewidth=0.7,
                zorder=3,
            )
            _fit(ax, s, ls="-")
        lines = []
        for crop in crops:
            s = sub[sub["crop"] == crop]
            if s.empty:
                continue
            res = spearman(s["mean_gdd"], s["peak95_ndvi"])
            glyph = "\u25cf" if crop == "Corn" else "\u25b2"
            lines.append(f"{glyph} {crop[:3].lower()}  \u03c1={res['rho']:+.2f}  n={res['n']}")
        ax.set_title(f"{grower}  (zoomed x-axis)", fontsize=10, loc="left")
        ax.set_xlabel("Mean season GDD (\u00b0C-day)")
        ax.set_ylabel("Peak-95 NDVI" if col == 0 else "")
        ax.grid(color="#ececec", lw=0.7)
        ax.set_axisbelow(True)
        ax.text(
            0.03,
            0.97,
            "\n".join(lines),
            transform=ax.transAxes,
            va="top",
            ha="left",
            fontsize=8,
            family="monospace",
            linespacing=1.5,
            bbox={"facecolor": "white", "edgecolor": "#dddddd", "boxstyle": "round,pad=0.4"},
        )
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)

    per_grower = {
        crop: {
            g: spearman(s["mean_gdd"], s["peak95_ndvi"])
            for g, s in bridge[bridge["crop"] == crop].groupby("grower")
        }
        for crop in crops
    }
    corn_slopes = ", ".join(
        f"{g} {r['rho']:+.2f}" for g, r in per_grower.get("Corn", {}).items()
    )
    soy_slopes = ", ".join(
        f"{g} {r['rho']:+.2f}" for g, r in per_grower.get("Soybeans", {}).items()
    )
    corn_by_grower = per_grower.get("Corn", {})
    worst_grower, worst = min(
        corn_by_grower.items(), key=lambda kv: kv[1]["rho"], default=(None, {})
    )
    cluster_text = ", ".join(
        f"{g} \u2248 {bridge.loc[bridge['grower'] == g, 'mean_gdd'].mean():.0f}"
        for g in growers
    )
    return _save(
        fig,
        out,
        "Exploratory: pooling the growers destroys the weather-canopy signal",
        "Corn's pooled correlation across all 30 fields is "
        f"\u03c1={pooled['Corn']['rho']:+.2f} ({_fmt_p(pooled['Corn']['p'])}) \u2014 it looks "
        f"like 'no relationship' only because the {len(growers)} growers sit in disjoint heat "
        f"ranges ({cluster_text} \u00b0C-day). "
        f"Inside the growers the signs differ: corn {corn_slopes}, with {worst_grower} at "
        f"\u03c1={worst.get('rho', float('nan')):+.2f} ({_fmt_p(worst.get('p', float('nan')))}, "
        f"n={worst.get('n', 0)}). Soybeans pool positive "
        f"(\u03c1={pooled['Soybeans']['rho']:+.2f}) and stay positive within growers "
        f"({soy_slopes}). Treat both as hypotheses for the report, not results.",
        "One point per field and crop, using only years when that crop was dominant. Bottom panels "
        "zoom each grower's own heat range, which is why the pooled top panel is the one that shows "
        "the between-grower separation. NDVI is the yearly peak-95 composite mean from "
        "ndvi_card_summary.json; no within-season NDVI series is tabulated.",
    )


# --------------------------------------------------------------------------
# tables
# --------------------------------------------------------------------------


def boundary_stats_table(metrics: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for grower, g in metrics.groupby("grower"):
        area = g["area_ha"]
        est = g["area_ha_est"].dropna()
        dev = (
            float(((g.loc[est.index, "area_ha"] - est).abs() / est * 100).max())
            if len(est)
            else np.nan
        )
        rows.append(
            {
                "grower": grower,
                "fields": len(g),
                "total_ha": round(float(area.sum()), 1),
                "min_ha": round(float(area.min()), 1),
                "median_ha": round(float(area.median()), 1),
                "max_ha": round(float(area.max()), 1),
                "iqr_ha": round(float(area.quantile(0.75) - area.quantile(0.25)), 1),
                "cv_area": round(float(area.std(ddof=1) / area.mean()), 3),
                "mean_shape_index": round(float(g["shape_index"].mean()), 3),
                "mean_perimeter_m": round(float(g["perimeter_m"].mean()), 1),
                "mean_vertex_count": round(float(g["vertex_count"].mean()), 1),
                "perimeter_area_ratio": round(
                    float((g["perimeter_m"] / (g["area_ha"] * 10_000.0)).mean()), 5
                ),
                "area_est_max_dev_pct": round(dev, 3) if np.isfinite(dev) else "n/a",
            }
        )
    return pd.DataFrame(rows)


def weather_stats_table(season: pd.DataFrame) -> pd.DataFrame:
    metrics = {
        "tmean_c": "season mean temp (C)",
        "precip_mm": "season precipitation (mm)",
        "gdd_total": "season GDD (C-day)",
        "frost_free_days": "frost-free days in season",
    }
    rows = []
    for grower, g in season.groupby("grower"):
        for col, label in metrics.items():
            vals = g[col].astype(float)
            rows.append(
                {
                    "grower": grower,
                    "metric": label,
                    "n_field_years": len(vals),
                    "mean": round(float(vals.mean()), 2),
                    "sd": round(float(vals.std(ddof=1)), 2),
                    "min": round(float(vals.min()), 2),
                    "median": round(float(vals.median()), 2),
                    "max": round(float(vals.max()), 2),
                    "cv": round(float(vals.std(ddof=1) / vals.mean()), 3)
                    if vals.mean()
                    else np.nan,
                }
            )
    overall = []
    for col, label in metrics.items():
        decomp = variance_decomposition(season, col)
        overall.append(
            {
                "grower": "ALL",
                "metric": label,
                "n_field_years": decomp["n"],
                "mean": round(float(season[col].mean()), 2),
                "sd": round(float(season[col].std(ddof=1)), 2),
                "min": round(float(season[col].min()), 2),
                "median": round(float(season[col].median()), 2),
                "max": round(float(season[col].max()), 2),
                "cv": round(
                    float(season[col].std(ddof=1) / season[col].mean()), 3
                )
                if season[col].mean()
                else np.nan,
                "between_grower_variance_share": round(decomp["between_share"], 3),
            }
        )
    return pd.concat([pd.DataFrame(rows), pd.DataFrame(overall)], ignore_index=True)


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------


def _tests_frame(tests: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(tests)
    preferred = [
        "category",
        "analysis",
        "comparison",
        "test",
        "statistic",
        "p_value",
        "p_adj",
        "effect_size",
        "effect_metric",
        "n",
        "n_a",
        "n_b",
        "note",
    ]
    for col in preferred:
        if col not in frame.columns:
            frame[col] = ""
    for col in ("statistic", "p_value", "p_adj", "effect_size"):
        frame[col] = frame[col].apply(_round_sig)
    return frame[preferred]


def build_all(
    *,
    data_root: str | Path | None = None,
    growers: Sequence[str] | None = None,
    start_year: int = 2021,
    end_year: int = 2025,
    out_dir: str | Path | None = None,
    include_bridge: bool = True,
    verbose: bool = True,
) -> dict[str, Any]:
    """Run the whole field-level EDA and write static artifacts."""
    log = print if verbose else (lambda *a, **k: None)
    root = resolve_data_root(data_root)
    contexts = load_contexts(root, growers)
    target = (
        Path(out_dir).expanduser().resolve()
        if out_dir is not None
        else root / "data-pipeline" / "eda" / "assignment-2"
    )
    figures_dir = target / "figures"
    tables_dir = target / "tables"
    figures_dir.mkdir(parents=True, exist_ok=True)
    tables_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    tests: list[dict[str, Any]] = []
    artifacts: list[dict[str, str]] = []

    log(f"runtime   : {root}")
    log(f"output    : {target}")
    log(f"growers   : {', '.join(c.grower_slug for c in contexts)}")
    log(f"window    : {start_year}-{end_year}")

    boundaries: dict[FarmContext, dict[str, Any]] = {}
    cdl_frames: list[pd.DataFrame] = []
    weather_frames: list[pd.DataFrame] = []
    ndvi_frames: list[pd.DataFrame] = []

    for ctx in contexts:
        boundaries[ctx] = load_boundaries(ctx)
        cdl = load_cdl(ctx, start_year, end_year)
        weather = load_weather(ctx, start_year, end_year)
        ndvi = load_ndvi(ctx)

        present_years = sorted(int(y) for y in cdl["year"].dropna().unique())
        missing = [y for y in range(start_year, end_year + 1) if y not in present_years]
        if missing:
            warnings.append(
                f"{ctx.grower_slug}: CDL missing {missing}; comparisons use available years only"
            )

        coords = weather.groupby("field_id")[["lat", "lon"]].first().drop_duplicates()
        if len(coords) < 2:
            warnings.append(
                f"{ctx.grower_slug}: all fields share one weather grid point; "
                "within-grower weather variation is not measurable"
            )

        pixel_totals = (
            cdl.groupby(["field_id", "year"])["pixel_count"].sum().astype(float)
        )
        thin = pixel_totals[pixel_totals < MIN_PIXELS_PER_FIELD_YEAR]
        if not thin.empty:
            warnings.append(
                f"{ctx.grower_slug}: {len(thin)} field-years have <{MIN_PIXELS_PER_FIELD_YEAR} "
                "CDL pixels; composition percentages there are coarse"
            )

        if ndvi.empty:
            warnings.append(f"{ctx.grower_slug}: no NDVI summaries; bridge analysis skipped")

        cdl_frames.append(cdl)
        weather_frames.append(weather)
        ndvi_frames.append(ndvi)
        log(
            f"  {ctx.short:3s} {ctx.farm_slug:24s} fields={len(boundaries[ctx]['table']):2d} "
            f"cdl_rows={len(cdl):4d} weather_rows={len(weather):6d}"
        )

    metrics = pd.concat([boundaries[c]["table"] for c in contexts], ignore_index=True)
    cdl_all = pd.concat(cdl_frames, ignore_index=True)
    weather_all = pd.concat(weather_frames, ignore_index=True)
    ndvi_all = pd.concat(ndvi_frames, ignore_index=True) if ndvi_frames else pd.DataFrame()

    season = season_stats(weather_all)

    # Weather resolution check: a farm is usually smaller than one NASA POWER
    # cell, so "per-field" weather can collapse to a couple of distinct series.
    for grower, group in season.groupby("grower"):
        signatures = (
            group.sort_values("year")
            .groupby("field_id")["precip_mm"]
            .apply(lambda s: tuple(np.round(s.to_numpy(), 1)))
        )
        n_distinct = signatures.nunique()
        if n_distinct < len(signatures):
            warnings.append(
                f"{grower}: {len(signatures)} fields resolve to only {n_distinct} distinct "
                "5-year precipitation series \u2014 NASA POWER's grid is coarser than the farm, "
                "so within-farm weather differences are grid artefacts"
            )
    dominant = dominant_crop_by_year(cdl_all)
    shares = crop_area_shares(cdl_all, metrics)
    profile = rotation_profile(dominant)
    transitions = transition_matrix(dominant)

    # ---- category A -------------------------------------------------------
    log("[A] field boundaries")
    artifacts.append(
        {"id": "A1", "path": str(plot_field_area(metrics, figures_dir / "A1_field_area_by_grower.png")),
         "kind": "figure", "category": "boundaries", "scale": "across growers"}
    )
    artifacts.append(
        {"id": "A2", "path": str(plot_shape_vs_area(metrics, figures_dir / "A2_boundary_shape_vs_area.png", tests)),
         "kind": "figure", "category": "boundaries", "scale": "within grower / across growers"}
    )
    artifacts.append(
        {"id": "M1", "path": str(plot_field_map(boundaries, figures_dir / "M1_fields_by_grower.png", warnings)),
         "kind": "map", "category": "boundaries", "scale": "context"}
    )
    area_test = kruskal_test(
        {g: sub["area_ha"].to_numpy() for g, sub in metrics.groupby("grower")}, metric="field area (ha)"
    )
    shape_test = kruskal_test(
        {g: sub["shape_index"].to_numpy() for g, sub in metrics.groupby("grower")},
        metric="shape index",
    )
    for result in (area_test, shape_test):
        if result.get("ok"):
            tests.append(
                {
                    "category": "A boundaries",
                    "analysis": "A3 geometry comparison",
                    "comparison": f"{result['metric']} \u2014 {', '.join(result['groups'].split(', '))}",
                    "test": result["test"],
                    "statistic": result["statistic"],
                    "p_value": result["p_value"],
                    "p_adj": "",
                    "effect_size": result["effect_size"],
                    "effect_metric": result["effect_metric"],
                    "n": result["n"],
                }
            )
            for pair in result.get("pairs", []):
                tests.append(
                    {
                        "category": "A boundaries",
                        "analysis": "A3 geometry comparison",
                        "comparison": f"{result['metric']}: {pair['groups']}",
                        "test": pair["test"],
                        "statistic": pair["statistic"],
                        "p_value": pair["p_value"],
                        "p_adj": pair["p_adj"],
                        "effect_size": "",
                        "effect_metric": "",
                        "n": pair.get("n", result["n"]),
                        "n_a": pair.get("n_a", ""),
                        "n_b": pair.get("n_b", ""),
                    }
                )
    boundary_stats = boundary_stats_table(metrics)
    boundary_stats.to_csv(tables_dir / "A3_boundary_stats.csv", index=False)
    artifacts.append(
        {"id": "A3", "path": str(tables_dir / "A3_boundary_stats.csv"), "kind": "table",
         "category": "boundaries", "scale": "across growers"}
    )

    # ---- category B -------------------------------------------------------
    log("[B] CDL / cropland")
    artifacts.append(
        {"id": "B1", "path": str(plot_crop_mix(shares, figures_dir / "B1_crop_mix_by_grower_year.png")),
         "kind": "figure", "category": "CDL", "scale": "across growers"}
    )
    artifacts.append(
        {"id": "B2", "path": str(plot_rotation_patterns(profile, figures_dir / "B2_rotation_patterns.png")),
         "kind": "figure", "category": "CDL", "scale": "within field over time / within grower"}
    )

    dominant_counts = (
        dominant.assign(
            crop_bucket=np.where(
                dominant["dominant_crop"].isin(["Corn", "Soybeans"]),
                dominant["dominant_crop"],
                "Other",
            )
        )
        .pivot_table(index="crop_bucket", columns="grower", values="field_id", aggfunc="count")
        .fillna(0)
    )
    chi = chi_square(dominant_counts, metric="dominant crop x grower")
    if chi.get("ok"):
        tests.append(
            {
                "category": "B CDL",
                "analysis": "B1 crop mix",
                "comparison": "dominant crop distribution across growers",
                "test": chi["test"],
                "statistic": chi["statistic"],
                "p_value": chi["p_value"],
                "p_adj": "",
                "effect_size": chi["effect_size"],
                "effect_metric": chi["effect_metric"],
                "n": chi["n"],
            }
        )

    diversity_test = kruskal_test(
        {g: sub["shannon_diversity"].to_numpy() for g, sub in profile.groupby("grower")},
        metric="rotation diversity",
    )
    if diversity_test.get("ok"):
        tests.append(
            {
                "category": "B CDL",
                "analysis": "B2 rotation patterns",
                "comparison": f"rotation diversity \u2014 {', '.join(diversity_test['groups'].split(', '))}",
                "test": diversity_test["test"],
                "statistic": diversity_test["statistic"],
                "p_value": diversity_test["p_value"],
                "p_adj": "",
                "effect_size": diversity_test["effect_size"],
                "effect_metric": diversity_test["effect_metric"],
                "n": diversity_test["n"],
            }
        )
        for pair in diversity_test.get("pairs", []):
            tests.append(
                {
                    "category": "B CDL",
                    "analysis": "B2 rotation patterns",
                    "comparison": f"rotation diversity: {pair['groups']}",
                    "test": pair["test"],
                    "statistic": pair["statistic"],
                    "p_value": pair["p_value"],
                    "p_adj": pair["p_adj"],
                    "effect_size": "",
                    "effect_metric": "",
                    "n": pair.get("n", diversity_test["n"]),
                    "n_a": pair.get("n_a", ""),
                    "n_b": pair.get("n_b", ""),
                }
            )

    alternation = chi_square(alternation_contingency(profile), metric="full alternation x grower")
    if alternation.get("ok"):
        tests.append(
            {
                "category": "B CDL",
                "analysis": "B2 rotation patterns",
                "comparison": "full alternation (yes/no) across growers",
                "test": alternation["test"],
                "statistic": alternation["statistic"],
                "p_value": alternation["p_value"],
                "p_adj": "",
                "effect_size": alternation["effect_size"],
                "effect_metric": alternation["effect_metric"],
                "n": alternation["n"],
                "note": "descriptive; 10 fields per grower",
            }
        )

    transitions_out = transitions.copy()
    transitions_out["row_share"] = transitions_out["row_share"].round(3)
    profile_out = profile.copy()
    for col in ("alternation_rate", "shannon_diversity"):
        profile_out[col] = profile_out[col].round(3)
    transitions_out.to_csv(tables_dir / "B3_rotation_transitions.csv", index=False)
    profile_out.to_csv(tables_dir / "B3_rotation_purity.csv", index=False)
    artifacts.append(
        {"id": "B3", "path": str(plot_transition_matrix(transitions, figures_dir / "B3_transition_matrix.png")),
         "kind": "figure", "category": "CDL", "scale": "within field over time / across growers"}
    )
    artifacts.append(
        {"id": "B3t", "path": str(tables_dir / "B3_rotation_transitions.csv"), "kind": "table",
         "category": "CDL", "scale": "within field over time / across growers"}
    )

    # transition homogeneity across growers
    if not transitions.empty:
        trans_types = transitions.assign(
            transition=transitions["from_crop"] + "\u2192" + transitions["to_crop"]
        )
        if trans_types["transition"].nunique() > 1 and trans_types["grower"].nunique() > 1:
            contingency = (
                trans_types.pivot_table(
                    index="transition", columns="grower", values="count", aggfunc="sum"
                )
                .fillna(0)
            )
            homogeneity = chi_square(contingency, metric="crop transition x grower")
            if homogeneity.get("ok"):
                tests.append(
                    {
                        "category": "B CDL",
                        "analysis": "B3 rotation transitions",
                        "comparison": "transition matrix across growers",
                        "test": homogeneity["test"],
                        "statistic": homogeneity["statistic"],
                        "p_value": homogeneity["p_value"],
                        "p_adj": "",
                        "effect_size": homogeneity["effect_size"],
                        "effect_metric": homogeneity["effect_metric"],
                        "n": homogeneity["n"],
                    }
                )

    # ---- category C -------------------------------------------------------
    log("[C] weather")
    artifacts.append(
        {"id": "C1", "path": str(plot_climate_envelope(season, figures_dir / "C1_climate_envelope_by_grower.png")),
         "kind": "figure", "category": "weather", "scale": "within grower / across growers"}
    )
    spans_km = []
    for ctx in contexts:
        minx, _miny, maxx, _maxy = boundaries[ctx]["projected"].total_bounds
        spans_km.append((maxx - minx) / 1000.0)
    span_text = f"{min(spans_km):.0f}\u2013{max(spans_km):.0f} km" if spans_km else None
    artifacts.append(
        {"id": "C2", "path": str(plot_season_trends(season, figures_dir / "C2_season_trends.png", span_text)),
         "kind": "figure", "category": "weather", "scale": "within field over time"}
    )

    for col, label in (
        ("tmean_c", "season mean temp (C)"),
        ("precip_mm", "season precipitation (mm)"),
        ("gdd_total", "season GDD (C-day)"),
        ("frost_free_days", "frost-free days"),
    ):
        result = kruskal_test(
            {g: sub[col].to_numpy() for g, sub in season.groupby("grower")}, metric=label
        )
        if result.get("ok"):
            tests.append(
                {
                    "category": "C weather",
                    "analysis": "C3 climate comparison",
                    "comparison": f"{label} \u2014 {', '.join(result['groups'].split(', '))}",
                    "test": result["test"],
                    "statistic": result["statistic"],
                    "p_value": result["p_value"],
                    "p_adj": "",
                    "effect_size": result["effect_size"],
                    "effect_metric": result["effect_metric"],
                    "n": result["n"],
                }
            )

    weather_stats = weather_stats_table(season)
    weather_stats.to_csv(tables_dir / "C3_weather_stats.csv", index=False)
    artifacts.append(
        {"id": "C3", "path": str(tables_dir / "C3_weather_stats.csv"), "kind": "table",
         "category": "weather", "scale": "within grower / across growers"}
    )

    # ---- optional bridge --------------------------------------------------
    if include_bridge and not ndvi_all.empty and not dominant.empty:
        log("[X] weather x canopy bridge")
        bridge_rows = []
        for (grower, field_id), g in dominant.groupby(["grower", "field_id"]):
            field_season = season[
                (season["grower"] == grower) & (season["field_id"] == field_id)
            ]
            for crop in ("Corn", "Soybeans"):
                crop_years = g.loc[g["dominant_crop"] == crop, "year"].astype(int)
                if crop_years.empty:
                    continue
                ndvi_rows = ndvi_all[
                    (ndvi_all["field_id"] == field_id) & (ndvi_all["crop"] == crop)
                ]
                if ndvi_rows.empty:
                    continue
                sub = field_season[field_season["year"].isin(crop_years)]
                if sub.empty:
                    continue
                bridge_rows.append(
                    {
                        "grower": grower,
                        "field_id": field_id,
                        "crop": crop,
                        "n_years": len(sub),
                        "mean_gdd": float(sub["gdd_total"].mean()),
                        "mean_precip": float(sub["precip_mm"].mean()),
                        "mean_tmean": float(sub["tmean_c"].mean()),
                        "peak95_ndvi": float(ndvi_rows["peak95_ndvi"].iloc[0]),
                    }
                )
        bridge = pd.DataFrame(bridge_rows)
        if not bridge.empty:
            bridge_out = bridge.copy()
            for col, digits in (
                ("mean_gdd", 1),
                ("mean_precip", 1),
                ("mean_tmean", 2),
                ("peak95_ndvi", 4),
            ):
                bridge_out[col] = bridge_out[col].round(digits)
            bridge_out.to_csv(tables_dir / "X1_weather_ndvi.csv", index=False)
            artifacts.append(
                {"id": "X1", "path": str(plot_weather_ndvi_bridge(bridge, figures_dir / "X1_weather_ndvi_bridge.png")),
                 "kind": "figure", "category": "weather x CDL", "scale": "within field over time"}
            )
            artifacts.append(
                {"id": "X1t", "path": str(tables_dir / "X1_weather_ndvi.csv"), "kind": "table",
                 "category": "weather x CDL", "scale": "within field over time"}
            )
            for crop, sub in bridge.groupby("crop"):
                result = spearman(sub["mean_gdd"], sub["peak95_ndvi"])
                tests.append(
                    {
                        "category": "X bridge",
                        "analysis": "X1 weather x canopy",
                        "comparison": f"{crop}: GDD ~ peak-95 NDVI (pooled)",
                        "test": "Spearman",
                        "statistic": result["rho"],
                        "p_value": result["p"],
                        "p_adj": "",
                        "effect_size": "",
                        "effect_metric": "rho",
                        "n": result["n"],
                        "note": "exploratory; pooling across growers cancels within-grower signal",
                    }
                )
                for grower, s in sub.groupby("grower"):
                    inner = spearman(s["mean_gdd"], s["peak95_ndvi"])
                    tests.append(
                        {
                            "category": "X bridge",
                            "analysis": "X1 weather x canopy",
                            "comparison": f"{grower} {crop}: GDD ~ peak-95 NDVI",
                            "test": "Spearman",
                            "statistic": inner["rho"],
                            "p_value": inner["p"],
                            "p_adj": "",
                            "effect_size": "",
                            "effect_metric": "rho",
                            "n": inner["n"],
                            "note": "exploratory; small n",
                        }
                    )

    # ---- write test log + manifest ----------------------------------------
    tests_frame = _tests_frame(tests)
    tests_frame.to_csv(tables_dir / "statistical_tests.csv", index=False)
    artifacts.append(
        {"id": "T", "path": str(tables_dir / "statistical_tests.csv"), "kind": "table",
         "category": "all", "scale": "all"}
    )

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_root": str(root),
        "output_dir": str(target),
        "year_window": [start_year, end_year],
        "growers": [
            {
                "grower_slug": ctx.grower_slug,
                "farm_slug": ctx.farm_slug,
                "label": ctx.label,
                "fields": int((metrics["grower_slug"] == ctx.grower_slug).sum()),
            }
            for ctx in contexts
        ],
        "counts": {
            "fields": int(len(metrics)),
            "weather_rows": int(len(weather_all)),
            "cdl_rows": int(len(cdl_all)),
            "field_years": int(len(season)),
        },
        "artifacts": artifacts,
        "warnings": warnings,
        "tests": [
            {
                "category": t.get("category"),
                "comparison": t.get("comparison"),
                "test": t.get("test"),
                "p_value": t.get("p_value"),
                "effect_size": t.get("effect_size"),
                "n": t.get("n"),
            }
            for t in tests
        ],
    }
    (target / "run_manifest.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (target / "INDEX.md").write_text(_render_index(summary, artifacts), encoding="utf-8")

    log("")
    log(f"wrote {len([a for a in artifacts if a['kind'] != 'table'])} figures and "
        f"{len([a for a in artifacts if a['kind'] == 'table'])} tables")
    for warning in warnings:
        log(f"  warning: {warning}")
    return summary


def _render_index(summary: dict[str, Any], artifacts: list[dict[str, str]]) -> str:
    rows = [
        "| id | kind | category | scale | path |",
        "|---|---|---|---|---|",
    ]
    for artifact in artifacts:
        rel = Path(artifact["path"]).relative_to(Path(summary["output_dir"]))
        rows.append(
            f"| {artifact['id']} | {artifact['kind']} | {artifact['category']} | "
            f"{artifact['scale']} | `{rel}` |"
        )

    lines = [
        "# Field-level EDA \u2014 artifact manifest",
        "",
        f"Generated {summary['generated_at']} from `{summary['data_root']}`.",
        f"Window 2021\u20132025. {summary['counts']['fields']} fields across "
        f"{len(summary['growers'])} growers.",
        "",
        "This file is a manifest, not a report. The narrative report is assembled separately.",
        "",
        "## Artifacts",
        "",
        *rows,
        "",
        "## Statistical tests",
        "",
        "| category | comparison | test | p | effect | n |",
        "|---|---|---|---|---|---|",
    ]
    for test in summary["tests"]:
        p = test.get("p_value")
        p_text = f"{p:.3g}" if isinstance(p, (int, float)) and np.isfinite(p) else "n/a"
        effect = test.get("effect_size")
        effect_text = f"{effect:.3g}" if isinstance(effect, (int, float)) and np.isfinite(effect) else ""
        lines.append(
            f"| {test.get('category')} | {test.get('comparison')} | {test.get('test')} | "
            f"{p_text} | {effect_text} | {test.get('n')} |"
        )
    if summary["warnings"]:
        lines += ["", "## Data warnings", ""]
        lines += [f"- {w}" for w in summary["warnings"]]
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "FarmContext",
    "build_all",
    "load_contexts",
    "resolve_data_root",
    "season_stats",
    "classify_rotation",
]
