"""Shared building blocks for the demo dashboards (sales, supply).

Both dashboards read every value through the data guard; this package holds
what they have in common: the template loader extension, cell formatting for
masked values, the "What you can see" panel and the ad-hoc SQL page.
"""

from apps.dashboard_common.core import (
    ColumnState,
    Example,
    ResultView,
    chart_spec,
    column_state,
    numeric,
    install_dashboard,
    register_query_page,
    run_view,
    visibility_panel,
)

__all__ = [
    "ColumnState",
    "Example",
    "ResultView",
    "chart_spec",
    "column_state",
    "numeric",
    "install_dashboard",
    "register_query_page",
    "run_view",
    "visibility_panel",
]
