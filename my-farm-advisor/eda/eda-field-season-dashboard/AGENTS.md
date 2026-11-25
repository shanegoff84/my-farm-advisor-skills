# Local Instructions

## Purpose

This folder owns the **runnable field-season storyline dashboard**: one field,
one growing season, four aligned panels (NDVI, precipitation,
temperature/extremes, cumulative GDD) plus generated callouts. It turns the
canonical runtime tree into a single image an advisor can read top-to-bottom on
one shared calendar.

## Safe edit scope

Edits stay in this folder and its children unless the user explicitly asks for a
broader change. Do not modify `../INDEX.md` beyond the single routing bullet,
do not touch sibling `eda-*` guides, and do not change the root `SKILL.md` from
a subskill task.

## Read nearby docs first

Read `GUIDE.md` first. Then check the live inputs before changing a loader:

```bash
B="$DATA_PIPELINE_DATA_ROOT/data-pipeline/growers/ia-demo-grower/farms/iowa-demo-farm"
head -2 "$B/derived/tables/iowa_2023_cdl.csv"
head -2 "$B/fields/osm-1360316095/weather/daily_weather.csv"
ls "$B/fields/osm-1360316095/satellite/sentinel/2023"
```

The assignments use `osm-1360316095` in `iowa-demo-farm` as the featured field,
but every loader discovers its inputs from the requested field id and year.

## Local validation

```bash
export DATA_PIPELINE_DATA_ROOT=~/my-farm-advisor-runtime
cd my-farm-advisor/eda/eda-field-season-dashboard
"$DATA_PIPELINE_DATA_ROOT/data-pipeline/.venv/bin/python" \
  scripts/run_season_dashboard.py \
  --field osm-1360316095 --year 2023 --out /tmp/opencode/season-smoke
```

A clean run writes one PNG, three small sidecar CSVs and a manifest, and exits
0. The default five-year check is `--year all`.

## Invariants

- **Never hardcode a grower, farm or field slug.** Discover all three from the
  runtime tree and join on the inventory `field_id`.
- **The crop comes from CDL for the requested field-year.** Do not infer it from
  the previous year or from the satellite signal.
- **Keep one calendar.** Every panel uses 1 April–31 October of the selected
  year; do not give a panel its own x-window.
- **SCL is a mask, not a cosmetic.** Drop scenes below
  `MIN_VALID_PIXEL_FRACTION` (30 %) instead of plotting a contaminated mean.
  A dropped scene must remain visible in the coverage note.
- **Weather is gridded.** NASA POWER is a grid cell, not an on-farm gauge; keep
  that caveat in the figure and README.
- **GDD is the mean method, base 10 °C, no cap.** Do not silently switch to a
  capped crop model or Fahrenheit without changing the legend and GUIDE.
- **Captions are generated from the same arrays as the panels.** If a caption
  says a number, that number must come from the plotted weather/NDVI record.
- **A figure's headline must be checked against its own data.** Open the PNG
  after a rendering change; do not commit a dashboard with a clipped caption,
  empty panel, or marker collision.
- **No raw data in git.** Commit the rendered PNG(s) under
  `examples/**/output/` and the manifest; sidecar CSVs and rasters stay under the
  runtime root.
