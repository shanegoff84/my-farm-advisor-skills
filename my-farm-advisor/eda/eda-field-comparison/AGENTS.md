# Local Instructions

## Purpose

This folder owns the **runnable** field-level EDA workflow: comparing field
boundaries, CDL/cropland history and weather across growers, at within-field,
within-grower and cross-grower scales.

The sibling `eda-*` folders are documentation-only guides. This one ships code
because the comparison needs reproducible artifact generation.

## Safe edit scope

Edits should stay in this folder and its children unless the user explicitly
asks for a broader change. Do not modify `../INDEX.md` beyond the single routing
bullet, do not touch sibling `eda-*` guides, and do not change the root
`SKILL.md` from a subskill task.

## Read nearby docs first

Read `GUIDE.md` first. Then check the live schemas before changing a loader:

```bash
B="$DATA_PIPELINE_DATA_ROOT/data-pipeline/growers/il-demo-grower/farms/illinois-demo-farm"
head -2 "$B/derived/tables/illinois_demo_2025_cdl.csv"
head -2 "$B/derived/tables/illinois_demo_crop_rotation.csv"
head -2 "$B"/derived/tables/*weather*.csv
```

For routing context, read `../INDEX.md` and `../../SKILL.md`.

## Local validation

```bash
export DATA_PIPELINE_DATA_ROOT=~/my-farm-advisor-runtime
cd my-farm-advisor/eda/eda-field-comparison
"$DATA_PIPELINE_DATA_ROOT/data-pipeline/.venv/bin/python" \
  scripts/run_field_eda.py --quiet --out /tmp/opencode/eda-smoke
```

A clean run prints `figures: 9  tables: 5` and exits 0. Warnings about collapsed
weather series are expected on the demo data and must not be silenced — they are
a real property of the NASA POWER grid.

## Invariants

- **Never hardcode grower, farm or field slugs.** Discover them from the runtime
  tree; join on inventory `field_id`.
- **Never trust `area_ha_est` as the area source.** Recompute from geometry in
  EPSG:5070 and report the deviation in `A3_boundary_stats.csv`.
- **Keep the year window uniform.** Nebraska CDL starts in 2021, so a
  cross-grower time comparison must not silently use 2019–2020 for IL/IA.
- **No report generation here.** This folder emits figures, tables and a
  manifest. The narrative report is a separate, one-time artifact.
- **No soil, no yield, no raster reads.** If a request needs them, it belongs in
  another subskill.
- **A figure's headline must be checked against its own data.** Several claims in
  the first draft were contradicted by the plotted values and were rewritten;
  do not reintroduce a story the numbers do not support.
- **Percentages need denominators.** OSM parcels are 25–80 ha, so CDL pixel
  counts are small; always keep `pixel_count` next to `pct`.
