# Grower Web Map

A grower-level interactive map that renders the **actual downloaded field
boundaries** from the data pipeline over a satellite basemap. One HTML file per
grower, small enough to email or open from disk.

## What it produces

```
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/web-map/
├── <grower>-grower-map.html   # the map (single file)
└── manifest.json              # field ids, farms, byte size, boundary sources, enrichment counts
```

The HTML embeds only rounded polygon coordinates and a small metadata payload.
Basemap tiles and the Leaflet library load from the internet; nothing else is
external.

## Map behaviour

| Requirement | How it is met |
|---|---|
| Real pipeline boundaries | Reads `farms/<farm>/boundary/field_boundaries.geojson` verbatim |
| Satellite-style basemap | Esri World Imagery XYZ tiles; OpenStreetMap streets available as a toggle |
| All fields for the grower | Every farm and feature for the grower is drawn, coloured per farm |
| Zoom in/out | Standard Leaflet zoom control, scroll wheel, scale bar |
| Click for metadata | Popup with location, area, crop rotation, NDVI averages, and collapsible soil/terrain/weather sections |
| Field list control | Sidebar groups fields by farm; a row flies to that field and opens its popup |
| Small output | Coordinates rounded to 6 decimals (~11 cm); no rasters or imagery embedded |

## Popup contents

Each popup is built from artifacts the pipeline already wrote for that field.
The top block is always visible; the three heavy sections are `<details>`
elements so the popup stays readable until a reader expands one.

| Block | Source |
|---|---|
| Location, county/state, area | Boundary properties (`area_acres`, `area_ha_est`, `county`, `state`) |
| Crop rotation strip, next-crop outlook | `derived/tables/ndvi_year_crop_join.csv` and `<farm>_crop_rotation.csv` |
| Corn / soybean mean NDVI with year counts | `derived/summaries/ndvi_card_summary.json` |
| **Soil** series, drainage, OM, pH, CEC, clay, water capacity, erosion risk, pH constraint | `soil/ssurgo_summary.csv` |
| **Terrain** elevation (ft and m), median/p95 slope, ponding and erosion proxy acres | `derived/tables/dem_terrain_summary.json` |
| **Weather** annual GDD (base 10 °C, mean method), precipitation in/yr, frost-free window | `weather/daily_weather.csv` |

Missing inputs degrade to `Crop history unavailable` or a omitted section; the
generator never fails because a derived table is absent.

Weather GDD uses the same calculation as `generate_weather_charts.py` (mean
method, 10 °C base), so the popup agrees with the farm report chart. Frost-free
dates are the median last spring frost and first fall frost across the years on
record. Terrain hydrology numbers carry a caveat note when the DEM was not
conditioned.

## Quick start

From the **runtime** source copy (installed by `scripts/install.sh`):

```bash
export DATA_PIPELINE_DATA_ROOT=~/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/reporting/generate_grower_web_map.py --all-growers
```

Target a single grower, or override the output path:

```bash
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/reporting/generate_grower_web_map.py --grower-slug il-demo-grower

"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/reporting/generate_grower_web_map.py --grower-slug il-demo-grower \
  --output /tmp/il-map.html --decimals 5
```

Emit boundaries plus basic metadata only, with no derived-data enrichment:

```bash
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/reporting/generate_grower_web_map.py --grower-slug il-demo-grower --lean
```

Open the result in any browser:

```bash
xdg-open "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/il-demo-grower/web-map/il-demo-grower-grower-map.html"
```

## How it works

1. Enumerates `growers/*/farms/*` (or the requested growers), sorted for
   deterministic output.
2. Reads each farm's `farm.json` for the display name and state, and the farm
   boundary GeoJSON for the polygons.
3. Resolves each field's display name from `fields/<field>/field.json`.
4. Normalises provider-specific properties — CSB boundaries carry `crop` and
   `county`; OSM boundaries carry `crop_name`, `county_name`, `state_fips` — into
   one flat metadata shape.
5. Collects popup details from the field's own derived artifacts (soil summary,
   terrain summary, daily weather, crop history, NDVI cards). A
   missing artifact simply drops that section.
6. Rounds coordinates, sorts features by `farm/field` key, and writes the HTML
   plus a provenance manifest with an `enrichment` block showing how many fields
   resolved each section.

The generator is stdlib-only: `csv` and `json` for the local artifacts, no
geopandas, rasterio, or folium. It never opens a raster, so the map build is
seconds regardless of imagery volume.

Typical size: ~36 KB for a 4-field grower with full enrichment, ~18 KB without
large boundary files, and ~9 KB with `--lean`.

## Failure modes

| Symptom | Cause |
|---|---|
| Grey basemap, polygons still visible | No internet access for Esri tiles or the Leaflet CDN |
| `skip <grower>: no field features` | Grower has no farms or no boundary GeoJSON yet; run the pipeline first |
| Empty sidebar | Boundary features have no `field_id` property |
| `Crop history unavailable` in a popup | The farm has no CDL/NDVI crop join yet, or `state_fips` is missing so CDL resolved no crops |
| Missing soil/terrain/weather section | That pipeline step has not run for the field; `manifest.json` `enrichment.sections` shows the counts |

## Limitations

- One map per grower; farms are colour-coded and grouped in the sidebar rather
  than split into separate pages.
- No editing, drawing, or tile caching. The map is a read-only deliverable.
- Popup enrichment reads artifacts, not live feeds: values are only as fresh as
  the last pipeline run for that field.
- Weather is a single point record per field, not a spatial average.
- Basemap availability is the provider's; the tile URL is the same keyless Esri
  endpoint used by `../references/basemap.md`.
