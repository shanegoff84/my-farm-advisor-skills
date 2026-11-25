#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Command-line entry point for the field-season storyline dashboard.

Examples
--------
One featured field-year::

    python scripts/run_season_dashboard.py --field osm-1360316095 --year 2023

Every available Sentinel year for that field::

    python scripts/run_season_dashboard.py --field osm-1360316095 --year all

Scratch run into a temporary directory::

    python scripts/run_season_dashboard.py --field osm-1360316095 --year 2023 \
        --out /tmp/opencode/season-dashboard
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_SRC = _SCRIPTS.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from season_dashboard import build_all, resolve_data_root  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build one aligned field-season mini-dashboard from "
        "Sentinel-2 NDVI, NASA POWER daily weather and CDL crop tables."
    )
    parser.add_argument(
        "--data-root",
        default=None,
        help="Runtime data root (default: $DATA_PIPELINE_DATA_ROOT or "
        "~/my-farm-advisor-runtime)",
    )
    parser.add_argument("--field", required=True,
                        help="Field id to analyse, e.g. osm-1360316095")
    parser.add_argument("--grower", default=None,
                        help="Optional grower slug when the field id is ambiguous")
    parser.add_argument("--farm", default=None,
                        help="Optional farm slug when the field id is ambiguous")
    parser.add_argument("--year", default="2023",
                        help="Growing-season year, or 'all' for every Sentinel year")
    parser.add_argument(
        "--out",
        default=None,
        help="Output directory (default: "
        "<data-root>/data-pipeline/eda/assignment-3)",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = resolve_data_root(args.data_root)
    summary = build_all(
        data_root=root,
        field_id=args.field,
        year=args.year,
        grower=args.grower,
        farm=args.farm,
        out_dir=args.out,
        verbose=not args.quiet,
    )
    print()
    print(f"output: {summary['output_dir']}")
    print(f"manifest: {summary['manifest']}")
    print(f"field: {summary['field_id']}  years: {', '.join(map(str, summary['years']))}")
    for run in summary["runs"]:
        print(
            f"  {run['year']} {run['crop']['crop_name']}: "
            f"{run['ndvi']['usable_in_season']} NDVI scenes, "
            f"{run['weather']['gdd_total_c_day']:.0f} GDD, "
            f"{run['weather']['precip_total_mm']:.0f} mm"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
