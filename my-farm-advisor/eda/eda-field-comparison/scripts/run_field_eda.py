#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Command-line entry point for the field-level EDA subskill.

The reusable logic lives in ``src/field_eda.py``; this file is only argument
parsing and a human-readable summary.

Examples
--------
Run against every grower in the runtime::

    python scripts/run_field_eda.py

Run against two growers into a scratch directory::

    python scripts/run_field_eda.py \
        --grower il-demo-grower --grower ia-demo-grower \
        --out /tmp/opencode/eda-scratch
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_SRC = _SCRIPTS.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from field_eda import build_all, resolve_data_root  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Field-level EDA: boundaries, CDL/cropland and weather "
        "at within-field, within-grower and cross-grower scales."
    )
    parser.add_argument(
        "--data-root",
        default=None,
        help="Runtime data root (default: $DATA_PIPELINE_DATA_ROOT or ~/my-farm-advisor-runtime)",
    )
    parser.add_argument(
        "--grower",
        action="append",
        dest="growers",
        metavar="SLUG",
        help="Grower slug to include; repeat for several. Default: all growers found.",
    )
    parser.add_argument("--start-year", type=int, default=2021)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument(
        "--out",
        default=None,
        help="Output directory (default: <data-root>/data-pipeline/eda/assignment-2)",
    )
    parser.add_argument(
        "--no-bridge",
        action="store_true",
        help="Skip the optional weather x NDVI bridge analysis",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = resolve_data_root(args.data_root)
    summary = build_all(
        data_root=root,
        growers=args.growers,
        start_year=args.start_year,
        end_year=args.end_year,
        out_dir=args.out,
        include_bridge=not args.no_bridge,
        verbose=not args.quiet,
    )

    print()
    print(f"output: {summary['output_dir']}")
    print(f"fields: {summary['counts']['fields']}  "
          f"field-years: {summary['counts']['field_years']}")
    figures = [a for a in summary["artifacts"] if a["kind"] != "table"]
    tables = [a for a in summary["artifacts"] if a["kind"] == "table"]
    print(f"figures: {len(figures)}  tables: {len(tables)}")
    if summary["warnings"]:
        print(f"warnings: {len(summary['warnings'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
