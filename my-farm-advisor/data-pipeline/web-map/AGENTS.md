# Local Instructions

## Purpose

This folder owns the grower-level interactive web map subskill. It turns the
pipeline's downloaded field boundaries into one small, self-contained HTML map
per grower: satellite basemap, all farms and fields, zoom/pan, clickable field
metadata, and a field list that flies to each field.

The runnable implementation lives at
`../src/scripts/reporting/generate_grower_web_map.py` because the runtime
installer mirrors `src/` into `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src`.
Keeping the script there is what makes `scripts/install.sh` the only install
path the runtime needs.

## Safe edit scope

Edits should stay in this folder and its children unless the user explicitly
asks for a broader change. Do not change parent `SKILL.md`, sibling workflows,
or root policy from a subskill task unless explicitly requested.

## Read nearby docs first

Read `GUIDE.md` first. For the map data contract and basemap policy, read
`../references/basemap.md` and `../AGENTS.md`. Read `../README.md` for runtime
layout and installer behaviour.

## Runtime contract

- Inputs are read-only runtime artifacts under
  `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/`.
- Outputs are written to `growers/<grower>/web-map/` and must never land in the
  skill checkout.
- The generator is stdlib-only. Do not add geopandas, rasterio, or folium: the
  point is a small, fast, dependency-free map render.
- Enrichment reads local text artifacts only — `soil/ssurgo_summary.csv`,
  `derived/tables/dem_terrain_summary.json`, `weather/daily_weather.csv`,
  `derived/tables/ndvi_year_crop_join.csv`, `<farm>_crop_rotation.csv`, and
  `derived/summaries/ndvi_card_summary.json`. Never open a raster or image to
  build a popup; the map must stay a text-only render.
- No raster, imagery, or raw data may be embedded. Only rounded boundary
  coordinates and metadata strings belong in the HTML.

## Local validation

Run `./scripts/validate.sh` from the repository root after structural changes.
Then regenerate the maps from the runtime copy and confirm the fields on the map
match `boundary/field_boundaries.geojson`.

## Local-delta-only reminder

This nested AGENTS.md only records instructions that differ from the parent or
root files. Do not duplicate root-wide asset, vendor, or validation policy here
except this pointer to `../../AGENTS.md`.
