---
name: eda-field-season-dashboard
description: Build one aligned field-season mini-dashboard from Sentinel-2 NDVI, NASA POWER daily weather and CDL crop-year tables, with generated annotations for the season's most important events.
version: 1.0.0
tags: [eda, dashboard, field-year, sentinel-2, ndvi, weather, gdd, cdl, matplotlib]
---

# Workflow: eda-field-season-dashboard

## What it does

This is a runnable, single-field workflow. For one field and one growing season
it:

1. finds the field boundary, daily weather and CDL crop-year rows in the
   canonical runtime tree;
2. masks each Sentinel-2 NDVI scene with its scene-classification layer (SCL);
3. aligns usable NDVI observations and every weather day on one
   1 April–31 October axis;
4. derives precipitation, temperature/extreme and cumulative-GDD metrics;
5. detects a small set of headline events and NDVI change points;
6. renders a four-panel PNG with generated captions and a coverage note.

It does **not** compare fields or growers, read soil, or make a yield forecast.
For multi-grower comparison use `../eda-field-comparison/`.

## Selected field-year

The assignment submission features:

| Item | Value |
|---|---|
| Field | `osm-1360316095` (Kossuth County, Iowa, `ia-demo-grower` / `iowa-demo-farm`) |
| Year | **2023** |
| CDL crop | **Corn**, 695 px, **96.53 %** of the field |
| Sentinel-2 scenes | 9 total, 7 in season, all 7 usable after SCL masking |
| Weather | 214/214 season days present |
| Seasonal precipitation | 527 mm |
| Seasonal GDD (base 10 °C) | 1,938 °C-day |

The workflow also generated 2021, 2022, 2024 and 2025 for the same field so the
same code path is exercised against a cloudy spring (2022), a long mid-season
gap (2024), and wetter seasons.

## Prerequisites

Run from a checkout of the skill with the runtime already built:

```bash
export DATA_PIPELINE_DATA_ROOT=~/my-farm-advisor-runtime
```

Requires `pandas`, `numpy`, `matplotlib` and `rasterio` (all already in the
pipeline virtualenv).

## Quick start

```bash
cd my-farm-advisor/eda/eda-field-season-dashboard
"$DATA_PIPELINE_DATA_ROOT/data-pipeline/.venv/bin/python" \
  scripts/run_season_dashboard.py \
  --field osm-1360316095 --year 2023
```

Default output: `$DATA_PIPELINE_DATA_ROOT/data-pipeline/eda/assignment-3/`.

Useful flags:

```bash
# every Sentinel year available for the field
python scripts/run_season_dashboard.py --field osm-1360316095 --year all

# disambiguate a field id that exists in more than one farm
python scripts/run_season_dashboard.py --field osm-1360316095 \
  --grower ia-demo-grower --farm iowa-demo-farm --year 2023

# scratch run
python scripts/run_season_dashboard.py --field osm-1360316095 --year 2023 \
  --out /tmp/opencode/season-dashboard
```

## Inputs

All read-only and discovered from `--field` + `--year`; no slug is hardcoded.

| Input | Runtime location |
|---|---|
| Field boundary | `growers/<g>/farms/<f>/boundary/field_boundaries.geojson` |
| CDL crop-year | `growers/<g>/farms/<f>/derived/tables/<farm>_<year>_cdl.csv` |
| Daily weather | `growers/<g>/farms/<f>/fields/<field>/weather/daily_weather.csv` |
| Sentinel NDVI | `growers/<g>/farms/<f>/fields/<field>/satellite/sentinel/<year>/sentinel_<yyyymmdd>/sentinel_<yyyymmdd>_ndvi.tif` |
| Sentinel SCL | same scene, `*_scl_epsg4326.tif` (falls back to native `*_scl.tif`) |
| 5-year GDD normal | the same field's daily weather for the available Sentinel years |

Weather columns consumed: `T2M`, `T2M_MAX`, `T2M_MIN`, `PRECTOTCORR`.
The crop-year selection takes the CDL row with the largest `pct` for the field
and records `pixel_count`, `pct` and the source table in the manifest.

## Method

### Shared calendar

The growing-season window is **1 April–31 October**. Every panel uses the same
dates, monthly ticks and x-limits. Scenes outside the window are retained in the
manifest but are not plotted.

### NDVI

For each Sentinel scene:

1. reproject the field polygon into the NDVI raster CRS;
2. rasterize the polygon to a field mask;
3. reproject the SCL layer (nearest) onto the NDVI grid when the grids differ;
4. keep SCL classes 4 (vegetation), 5 (non-vegetation) and 6 (water) that are
   finite and in `[-1, 1]`;
5. drop the scene when less than **30 %** of field pixels survive.

The plotted value is the mean of the surviving pixels. A gap longer than 30
days is shaded and labelled with its length, so a cloudy month is visible
rather than implied by a long straight segment.

### Weather metrics

| Metric | Definition |
|---|---|
| Daily GDD | `max(0, (Tmax + Tmin) / 2 − 10)` °C-day, mean method, no upper cap |
| Cumulative GDD | running season sum, starting at 0 on 1 April |
| Seasonal precipitation | sum of `PRECTOTCORR` over the season |
| Hot day | `Tmax ≥ 32 °C` |
| Extreme heat day | `Tmax ≥ 35 °C` (95 °F, pollination-stress watch) |
| Heavy-rain day | `PRECTOTCORR ≥ 25 mm` |
| Frost day | `Tmin ≤ 0 °C` |
| Dry spell | ≥14 consecutive days with `< 1 mm/day` |
| Heat spell | ≥3 consecutive days with `Tmax ≥ 32 °C` |

The cumulative-GDD panel compares the selected year with the same field's
2021–2025 mean and ±1 SD band, computed from daily weather, not from a separate
county product.

### Events and captions

Headline markers (shared vertical lines on all panels) use a maximum of three
events:

- the wettest May–July day when it is at least 80 % of the season maximum,
  otherwise the season's wettest day;
- the hottest day, when `Tmax ≥ 32 °C`;
- the first sub-zero minimum on/after 1 September.

NDVI annotations add the season peak and the largest drop between consecutive
usable scenes. Captions are assembled from the same computed values:

- green-up: largest NDVI rise plus rain in that interval;
- midseason: peak NDVI, hottest day, hot-day count, largest NDVI drop;
- finish: first freeze, seasonal GDD and rank, last usable NDVI.

The wording stays descriptive ("during", "coincided with") because the
dashboard does not attribute yield to any single driver.

## Outputs

Runtime:

```text
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/eda/assignment-3/
├── osm-1360316095_2023_corn_season_dashboard.png
├── osm-1360316095_2023_ndvi_scenes.csv
├── osm-1360316095_2023_weather_daily.csv
├── osm-1360316095_2023_events.csv
├── ... one set per year for --year all ...
└── osm-1360316095_season_dashboard_manifest.json
```

The PNG, the manifest and the three sidecar CSVs are written for every year.
Only PNGs and the manifest are committed to the repository, under
`examples/assignment-3/`.

## Data caveats this workflow handles

1. **Cloud gaps are real.** 2022 has a fully masked 14 May scene; 2023 has a
   55-day usable-scene gap (4 May–28 Jun); 2024 has a 65-day gap
   (23 May–27 Jul). The scene table and the coverage note keep those visible.
2. **SCL is coarser than NDVI.** The 20 m classification grid is resampled to
   the 10 m NDVI grid with nearest-neighbour, so a boundary pixel can inherit
   its neighbour's class. The 30 % threshold prevents a thin sliver from
   carrying a scene.
3. **NASA POWER is a grid cell.** It is not an on-farm rain gauge or a canopy
   sensor. Captions describe the grid-cell weather and the field-mean NDVI, not
   a cause-and-effect experiment.
4. **CDL is an annual land-cover class.** It identifies the crop-year; it does
   not provide planting date, hybrid, population or management.
5. **GDD is a bookkeeping metric.** The base-10 mean method is useful for
   comparing seasons and is the pipeline convention; it is not a crop model.
6. **Shoulder scenes are excluded.** March and November scenes exist for some
   years but fall outside the shared 1 April–31 October axis.

## Reusing pieces

```python
from season_dashboard import (
    build_all, find_field, load_cdl_crop, load_weather,
    load_ndvi_series, season_weather_metrics, detect_events,
)

summary = build_all(field_id="osm-1360316095", year=2023)
```

`render_dashboard` is a single function, so replacing the figure does not touch
the loaders. `build_year` returns the manifest record for one field-year;
`build_all` only loops years and writes the manifest.

## Validation

```bash
# one-year smoke
"$DATA_PIPELINE_DATA_ROOT/data-pipeline/.venv/bin/python" \
  scripts/run_season_dashboard.py --field osm-1360316095 --year 2023 \
  --out /tmp/opencode/season-smoke

# all five years
"$DATA_PIPELINE_DATA_ROOT/data-pipeline/.venv/bin/python" \
  scripts/run_season_dashboard.py --field osm-1360316095 --year all \
  --out /tmp/opencode/season-all

# repo-wide checks
./scripts/validate.sh
```

Open the PNG after any rendering change and check it against the five-point
figure gate in the repository `CONTRIBUTING.md`.
