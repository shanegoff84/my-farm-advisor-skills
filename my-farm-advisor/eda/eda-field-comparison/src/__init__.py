"""Field-level EDA subskill for My Farm Advisor.

Importable surface for the reusable workflow::

    from field_eda import build_all
"""

from .field_eda import (  # noqa: F401
    FarmContext,
    build_all,
    classify_rotation,
    load_contexts,
    resolve_data_root,
    season_stats,
)

__all__ = [
    "FarmContext",
    "build_all",
    "classify_rotation",
    "load_contexts",
    "resolve_data_root",
    "season_stats",
]
