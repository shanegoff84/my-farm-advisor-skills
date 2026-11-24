---
name: eda-field-comparison
description: Compare field boundaries, CDL/cropland history and weather across growers at three scales (within a field over time, across fields in a grower, across growers). Produces static PNG figures, CSV tables and a statistical test log from the canonical runtime tree.
version: 1.0.0
tags: [eda, comparison, fields, cdl, weather, statistics, matplotlib]
---

# Workflow: eda-field-comparison

## Description

A runnable EDA subskill for multi-grower field data. It reads the canonical
runtime tree, compares **field boundaries**, **CDL/cropland history** and
**weather**, and writes static artifacts:

- 7 statistical figures (2 boundaries, 3 CDL, 2 weather),
- 1 context map,
- 1 comparison/correlation analysis per category (A3 and C3 as tables, B3 as a
  table plus a transition-matrix figure),
- 1 optional cross-category bridge figure,
- a statistical test log and a manifest.

Nine figures and five tables in the default run.

It does **not** analyse soil, **not** read rasters, **not** build dashboards, and
**not** write the narrative report. The report is assembled by hand on top of
these outputs; the current one is [`REPORT.md`](REPORT.md).

## When to use

- Comparing several growers/farms on the same footing.
- Checking whether a dataset is comparable at all before modelling it
  (field-size regimes, weather resolution, rotation consistency).
- Producing the figure set for a written EDA report.

Do not use it for single-field inspection — the individual `eda-*` guides and
the farm reports already cover that.

## Prerequisites

Run from a checkout of the skill with the runtime already built:

```bash
export DATA_PIPELINE_DATA_ROOT=~/my-farm-advisor-runtime
```

Requires `pandas`, `numpy`, `scipy`, `matplotlib`, `geopandas` (all already in
the pipeline venv).

## Quick start

```bash
cd my-farm-advisor/eda/eda-field-comparison
"$DATA_PIPELINE_DATA_ROOT/data-pipeline/.venv/bin/python" \
  scripts/run_field_eda.py
```

Default output: `$DATA_PIPELINE_DATA_ROOT/data-pipeline/eda/assignment-2/`.

Useful flags:

```bash
# only some growers
python scripts/run_field_eda.py --grower il-demo-grower --grower ne-demo-grower

# different window
python scripts/run_field_eda.py --start-year 2021 --end-year 2024

# skip the optional weather x NDVI bridge
python scripts/run_field_eda.py --no-bridge

# scratch run
python scripts/run_field_eda.py --out /tmp/opencode/eda-scratch
```

## Inputs

All read-only, discovered per grower/farm — never hardcode a slug or a path.

| Input | Location |
|---|---|
| Field boundaries | `growers/<g>/farms/<f>/boundary/field_boundaries.geojson` |
| Field inventory | `growers/<g>/farms/<f>/manifests/field-inventory.csv` |
| CDL by year | `growers/<g>/farms/<f>/derived/tables/<farm>_<year>_cdl.csv` |
| Weather (daily) | `growers/<g>/farms/<f>/derived/tables/<farm>_weather_<a>_<b>.csv` |
| NDVI summary (bridge only) | `growers/<g>/farms/<f>/fields/<field>/derived/summaries/ndvi_card_summary.json` |

Field area, perimeter and shape are **recomputed from geometry** in EPSG:5070
rather than trusting `area_ha_est`, so the three source schemas are measured
identically. `A3_boundary_stats.csv` reports the deviation from `area_ha_est`
where that column exists.

## Three comparison scales

| Scale | Where it is measured |
|---|---|
| Within a field, over time | Each field's own 5-year weather trajectory (C2); its own CDL rotation sequence (B2, B3) |
| Across fields in one grower | Size and shape spread (A1, A2), rotation diversity (B2), per-field weather lines (C2) |
| Across growers | Field-size regime (A1), provenance-driven shape (A2), crop balance (B1), rotation discipline (B2, B3), climate bands (C1, C3) |

Note that boundaries are static: there is no within-field time scale for them,
and the guide says so rather than inventing one.

## Artifacts and the story each one tells

| Id | Artifact | Story |
|---|---|---|
| A1 | `A1_field_area_by_grower.png` | Field-size regimes differ ~5× in median (48 ha vs 208–245 ha); every per-field metric must be area-weighted before cross-grower comparison. |
| A2 | `A2_boundary_shape_vs_area.png` | Shape follows data provenance (OSM vs CSB), not size — within-grower area→shape correlation is ~0. |
| A3 | `A3_boundary_stats.csv` | The numeric form of A1/A2, plus the area weights B and C reuse. |
| M1 | `M1_fields_by_grower.png` | Where the farms are and how differently the land is subdivided. Context map, not one of the seven statistical figures. |
| B1 | `B1_crop_mix_by_grower_year.png` | Two-crop system everywhere; the corn:soy balance is what shifts between growers. |
| B2 | `B2_rotation_patterns.png` | Illinois rotates uniformly; Nebraska is a portfolio of regimes, so "the Nebraska rotation" is not one fact. |
| B3 | `B3_transition_matrix.png` | Day-to-day of the rotation: corn→corn carryover is 0.44 (IA), 0.18 (IL), 0.61 (NE), and soy→corn is 1.00 everywhere — the difference is corn repetition. |
| B3t | `B3_rotation_transitions.csv`, `B3_rotation_purity.csv` | The numbers behind B3: per-grower transition counts/row shares and per-field alternation rate + diversity. |
| C1 | `C1_climate_envelope_by_grower.png` | Temperature separates growers (81 % of variance); precipitation separates years, not places (18 %). |
| C2 | `C2_season_trends.png` | Rainfall is a year story (year range 7.6× the within-farm field gap); GDD is a year-and-place story (only 2.3×), partly because farms span 11–37 km. |
| C3 | `C3_weather_stats.csv` | Per-grower climate summary plus between-grower variance share. |
| X1 | `X1_weather_ndvi_bridge.png` | Optional. Pooling growers destroys the weather→canopy signal (Simpson's paradox). |
| — | `statistical_tests.csv` | Every test, p-value and effect size in one place. |
| — | `INDEX.md`, `run_manifest.json` | Artifact manifest, row counts and data warnings. |

Figures embed their headline and footnote, so they can be dropped into a report
without re-reading this guide.

`statistical_tests.csv` is descriptive first: `p_adj` is Holm-adjusted **within
each metric's pairwise set**, not across the whole log, and the rotation and
transition tests are kept as descriptive because fields and field-years are not
independent observations. Read a single row, not a "significant" flag.

## Output layout

```
<data-root>/data-pipeline/eda/assignment-2/
├── INDEX.md
├── run_manifest.json
├── figures/
│   ├── A1_field_area_by_grower.png
│   ├── A2_boundary_shape_vs_area.png
│   ├── M1_fields_by_grower.png
│   ├── B1_crop_mix_by_grower_year.png
│   ├── B2_rotation_patterns.png
│   ├── B3_transition_matrix.png
│   ├── C1_climate_envelope_by_grower.png
│   ├── C2_season_trends.png
│   └── X1_weather_ndvi_bridge.png
└── tables/
    ├── A3_boundary_stats.csv
    ├── B3_rotation_purity.csv
    ├── B3_rotation_transitions.csv
    ├── C3_weather_stats.csv
    ├── X1_weather_ndvi.csv
    └── statistical_tests.csv
```

## Data caveats this workflow already handles

1. **Aggregate CDL composition files are inconsistent.** One Illinois
   `*_2019_2023_full_composition.csv` is empty and one Iowa file stops at 2023.
   The loader therefore concatenates the per-year `<farm>_<year>_cdl.csv`
   tables and only falls back to an aggregate file if no per-year table exists.
2. **CDL year coverage differs.** Nebraska is 2021–2025 only, so every time
   comparison is trimmed to the requested window (default 2021–2025).
3. **NASA POWER's grid is coarser than a farm.** Ten fields often resolve to
   only 2–4 distinct five-year series. This is reported as a warning and is a
   finding in its own right (see C2), not something to hide.
4. **Boundary schemas differ.** CSB farms expose `area_ha_est`/`crop`; OSM farms
   expose `crop_name` and no area estimate. Geometry-derived metrics normalise
   this.
5. **Field ids are not stable across a re-bootstrap** for OSM farms. The
   workflow always joins on the inventory `field_id` and never hardcodes one.

## Reusing pieces

The module is importable without the CLI:

```python
from field_eda import build_all, load_contexts, resolve_data_root, season_stats

root = resolve_data_root()
ctxs = load_contexts(root, ["ne-demo-grower"])
summary = build_all(growers=["ne-demo-grower"], include_bridge=False)
```

Each figure is an independent function (`plot_field_area`,
`plot_shape_vs_area`, `plot_field_map`, `plot_crop_mix`,
`plot_rotation_patterns`, `plot_transition_matrix`, `plot_climate_envelope`,
`plot_season_trends`, `plot_weather_ndvi_bridge`), so dropping or replacing one
does not disturb the rest.

## Extending

- **A fourth grower** — add it to the runtime and pass `--grower`; nothing else
  changes.
- **A new crop bucket** — add it to `CROP_ORDER` and `CROP_COLORS` in
  `src/field_eda.py`.
- **A new weather metric** — add it to `season_stats()` and to the `specs` lists
  in the C figures; keep `statistical_tests.csv` in sync.

## Validation

```bash
# smoke run (geopandas + matplotlib + scipy present)
"$DATA_PIPELINE_DATA_ROOT/data-pipeline/.venv/bin/python" \
  scripts/run_field_eda.py --quiet --out /tmp/opencode/eda-smoke

# repo-wide checks
./scripts/validate.sh
```

Generated artifacts live under the runtime root and are not committed, so the
repo figure-quality gate is not triggered by a normal run.
