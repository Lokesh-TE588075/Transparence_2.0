"""Deterministic table summarizer (Phase Q3).

Post-processes raw Genie query results to produce:

  computed_metrics   dict  — key aggregate statistics for the response banner
                             e.g. {"total_count": 4821, "mode_top": "Air",
                                   "top_destinations": [{"destination": "DE", "count": 523}, ...]}

  summary_text       str   — human-readable paragraph built from computed_metrics;
                             prepended to the Genie text response when the Genie
                             message is weak (short / templated / empty) and the
                             row count exceeds GENIE_RAW_TABLE_SUMMARY_THRESHOLD.

  computed_chart_data list  — Recharts-ready data array derived from the most
                             chart-able column pair in the result.  Used by
                             GenieChart.jsx when has_visualization=False.

Design principles:
  - Pure-Python, no I/O, no LLM calls.  Entirely deterministic.
  - Works on the table_data dict (headers + rows) already returned by the mapper.
  - Column detection is heuristic but conservative: false negatives (no summary)
    are far safer than false positives (wrong summary).
  - All functions return safe defaults when inputs are empty or malformed.

Triggered when:
  GENIE_ENABLE_TABLE_SUMMARY=true  AND  row_count > GENIE_RAW_TABLE_SUMMARY_THRESHOLD

Config flags (see config.py):
  GENIE_ENABLE_TABLE_SUMMARY           bool   default True
  GENIE_RAW_TABLE_SUMMARY_THRESHOLD    int    default 100   (rows)
  GENIE_SUMMARY_TOP_N                  int    default 5
  GENIE_ENABLE_COMPUTED_CHART          bool   default True
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple


# =============================================================================
# Column-name heuristics
# =============================================================================

_DESTINATION_COLS_RE = re.compile(
    r"(dest(?:ination)?|consignee_country|to_country|arrival_country"
    r"|ship_to_country|receiver_country|dlv_country)",
    re.IGNORECASE,
)
_ORIGIN_COLS_RE = re.compile(
    r"(origin|shipper_country|from_country|departure_country"
    r"|ship_from_country|sender_country|pick_country)",
    re.IGNORECASE,
)
_MODE_COLS_RE = re.compile(
    r"((?:transport|shipping|freight|ship|mode|delivery)[-_]?mode"
    r"|transport_type|mode_of_transport|mode_description|modality"
    # P1 fix: cover TRANSPORTATION_MODE_DESC (has _desc suffix)
    r"|transportation[-_]mode(?:[-_]desc)?|transport[-_]mode[-_]desc)",
    re.IGNORECASE,
)
_STATUS_COLS_RE = re.compile(
    r"(status|shipment_status|delivery_status|current_status|state)",
    re.IGNORECASE,
)
_COUNT_COLS_RE  = re.compile(
    r"^(count|cnt|total|shipment_count|num_shipments|quantity|volume|n)$",
    re.IGNORECASE,
)
# P1 fix: business unit column detection
_BU_COLS_RE = re.compile(
    r"(business[-_]?unit(?:[-_]?id)?|bu[-_]?id|bu[-_]?code|business_unit_id)",
    re.IGNORECASE,
)
# P1 fix: revenue / currency amount column detection
_REVENUE_COLS_RE = re.compile(
    r"(sales[-_](?:functional[-_])?currency[-_]amount"
    r"|revenue|sales[-_]amount|functional[-_]currency[-_]amount"
    r"|(?:total|sum)[-_](?:revenue|sales|amount))",
    re.IGNORECASE,
)
_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")


# =============================================================================
# Internal helpers
# =============================================================================

def _col_index(headers: List[str], pattern: re.Pattern) -> Optional[int]:
    """Return index of first header matching pattern, or None."""
    for i, h in enumerate(headers):
        if pattern.search(h):
            return i
    return None


def _numeric_value(v: Any) -> Optional[float]:
    """Parse a cell value to float, returning None if not numeric."""
    if v is None or v == "":
        return None
    s = str(v).strip()
    if _NUMERIC_RE.match(s):
        return float(s)
    return None


def _count_column(
    rows: List[List], count_idx: Optional[int], fallback_total: int
) -> int:
    """Sum the count column if present, else return fallback_total."""
    if count_idx is None:
        return fallback_total
    total = 0
    for row in rows:
        v = _numeric_value(row[count_idx] if count_idx < len(row) else None)
        if v is not None:
            total += int(v)
    return total if total > 0 else fallback_total


def _top_values(
    rows: List[List],
    col_idx: int,
    count_idx: Optional[int],
    top_n: int,
) -> List[Dict[str, Any]]:
    """Return top-N values for a categorical column.

    When count_idx is present, sums the count column per group.
    Otherwise, counts occurrences.
    """
    agg: Dict[str, int] = {}
    for row in rows:
        if col_idx >= len(row):
            continue
        label = str(row[col_idx] if row[col_idx] is not None else "(unknown)").strip()
        if not label or label.lower() in ("", "none", "null"):
            continue
        if count_idx is not None and count_idx < len(row):
            v = _numeric_value(row[count_idx])
            weight = int(v) if v is not None else 1
        else:
            weight = 1
        agg[label] = agg.get(label, 0) + weight

    sorted_items = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)
    return [{"label": k, "count": v} for k, v in sorted_items[:top_n]]


# =============================================================================
# Public API
# =============================================================================

class TableSummaryResult:
    """Result container returned by summarize_table()."""

    __slots__ = (
        "computed_metrics",
        "summary_text",
        "computed_chart_data",
        "chart_x_key",
        "chart_y_key",
    )

    def __init__(
        self,
        computed_metrics:    Optional[Dict[str, Any]] = None,
        summary_text:        str = "",
        computed_chart_data: Optional[List[Dict]] = None,
        chart_x_key:         Optional[str] = None,
        chart_y_key:         Optional[str] = None,
    ):
        self.computed_metrics    = computed_metrics or {}
        self.summary_text        = summary_text
        self.computed_chart_data = computed_chart_data or []
        self.chart_x_key         = chart_x_key
        self.chart_y_key         = chart_y_key


def summarize_table(
    headers:       List[str],
    rows:          List[List],
    row_count:     int,
    top_n:         int = 5,
    enable_chart:  bool = True,
) -> TableSummaryResult:
    """Produce computed_metrics, summary_text, and computed_chart_data from
    a Genie query result.

    Args:
        headers:      Column names from GenieQueryResult.
        rows:         Row data (List[List]).  Normalised by the caller.
        row_count:    Total row count (may exceed len(rows) if capped).
        top_n:        Number of top entries to include in breakdowns.
        enable_chart: When False, computed_chart_data is always empty.

    Returns:
        TableSummaryResult with all three fields populated (or empty on failure).
    """
    if not headers or not rows:
        return TableSummaryResult()

    # --- Locate key columns ---
    dest_idx    = _col_index(headers, _DESTINATION_COLS_RE)
    origin_idx  = _col_index(headers, _ORIGIN_COLS_RE)
    mode_idx    = _col_index(headers, _MODE_COLS_RE)
    status_idx  = _col_index(headers, _STATUS_COLS_RE)
    count_idx   = _col_index(headers, _COUNT_COLS_RE)
    # P1 fix: business unit and revenue columns
    bu_idx      = _col_index(headers, _BU_COLS_RE)
    revenue_idx = _col_index(headers, _REVENUE_COLS_RE)

    # --- Compute total ---
    total = _count_column(rows, count_idx, row_count)

    metrics: Dict[str, Any] = {"total_count": total}

    # --- Top destinations or origins ---
    geo_idx   = dest_idx if dest_idx is not None else origin_idx
    geo_label = "destination" if dest_idx is not None else "origin"
    top_geo: List[Dict] = []
    if geo_idx is not None:
        top_geo = _top_values(rows, geo_idx, count_idx, top_n)
        metrics[f"top_{geo_label}s"] = top_geo

    # --- Mode breakdown ---
    top_modes: List[Dict] = []
    if mode_idx is not None:
        top_modes = _top_values(rows, mode_idx, count_idx, top_n)
        metrics["mode_breakdown"] = top_modes
        if top_modes:
            metrics["mode_top"] = top_modes[0]["label"]

    # --- Status breakdown ---
    top_statuses: List[Dict] = []
    if status_idx is not None:
        top_statuses = _top_values(rows, status_idx, count_idx, top_n)
        metrics["status_breakdown"] = top_statuses

    # --- P1: Business unit breakdown ---
    top_bus: List[Dict] = []
    if bu_idx is not None:
        top_bus = _top_values(rows, bu_idx, count_idx, top_n)
        metrics["bu_breakdown"] = top_bus

    # --- P1: Revenue metrics ---
    total_revenue: Optional[float] = None
    avg_revenue: Optional[float] = None
    if revenue_idx is not None:
        revenue_values = [
            _numeric_value(row[revenue_idx])
            for row in rows
            if revenue_idx < len(row)
        ]
        revenue_values = [v for v in revenue_values if v is not None]
        if revenue_values:
            total_revenue = sum(revenue_values)
            avg_revenue = total_revenue / len(revenue_values)
            metrics["total_revenue"] = round(total_revenue, 2)
            metrics["avg_revenue"] = round(avg_revenue, 2)

    # --- Summary text ---
    summary_text = _build_summary_text(
        total=total,
        geo_label=geo_label,
        top_geo=top_geo,
        top_modes=top_modes,
        top_statuses=top_statuses,
        top_bus=top_bus,
        total_revenue=total_revenue,
    )

    # --- Computed chart data ---
    computed_chart_data: List[Dict] = []
    chart_x_key: Optional[str] = None
    chart_y_key: Optional[str] = None

    if enable_chart:
        computed_chart_data, chart_x_key, chart_y_key = _build_chart_data(
            headers, rows, count_idx, dest_idx, origin_idx, mode_idx,
            status_idx, top_n, bu_idx=bu_idx,
        )

    return TableSummaryResult(
        computed_metrics=metrics,
        summary_text=summary_text,
        computed_chart_data=computed_chart_data,
        chart_x_key=chart_x_key,
        chart_y_key=chart_y_key,
    )


# =============================================================================
# Internal: summary text builder
# =============================================================================

def _build_summary_text(
    total:         int,
    geo_label:     str,
    top_geo:       List[Dict],
    top_modes:     List[Dict],
    top_statuses:  List[Dict],
    top_bus:       Optional[List[Dict]] = None,    # P1
    total_revenue: Optional[float] = None,          # P1
) -> str:
    """Build a concise one-paragraph summary from the extracted metrics.

    P1: extended with business unit breakdown and revenue totals.
    """
    parts: List[str] = []

    if total > 0:
        parts.append(f"**{total:,} shipments** found.")

    if top_geo:
        geo_str = ", ".join(
            f"{g['label']} ({g['count']:,})"
            for g in top_geo[:3]
        )
        parts.append(f"Top {geo_label}s: {geo_str}.")

    if top_modes:
        mode_str = ", ".join(
            f"{m['label']} ({m['count']:,})"
            for m in top_modes[:3]
        )
        parts.append(f"Transport modes: {mode_str}.")

    if top_statuses:
        status_str = ", ".join(
            f"{s['label']} ({s['count']:,})"
            for s in top_statuses[:3]
        )
        parts.append(f"Status breakdown: {status_str}.")

    # P1: business unit breakdown
    if top_bus:
        bu_str = ", ".join(
            f"{b['label']} ({b['count']:,})"
            for b in top_bus[:3]
        )
        parts.append(f"Business units: {bu_str}.")

    # P1: revenue total
    if total_revenue is not None:
        parts.append(f"Total revenue: {total_revenue:,.2f}.")

    return "  ".join(parts) if parts else ""


# =============================================================================
# Internal: chart data builder
# =============================================================================

def _build_chart_data(
    headers:    List[str],
    rows:       List[List],
    count_idx:  Optional[int],
    dest_idx:   Optional[int],
    origin_idx: Optional[int],
    mode_idx:   Optional[int],
    status_idx: Optional[int],
    top_n:      int,
    bu_idx:     Optional[int] = None,  # P1
) -> Tuple[List[Dict], Optional[str], Optional[str]]:
    """Build a Recharts-ready data array from the most chart-able columns.

    Priority order for the x-axis dimension:
      1. mode (low cardinality, always bar-chartable)
      2. status (low cardinality)
      3. destination / origin (top-N by count)

    Returns (chart_data, x_key, y_key) or ([], None, None) on failure.
    """
    dim_idx: Optional[int] = None
    dim_key: Optional[str] = None

    for idx, name in [
        (mode_idx,   headers[mode_idx]   if mode_idx   is not None else None),
        (status_idx, headers[status_idx] if status_idx is not None else None),
        (bu_idx,     headers[bu_idx]     if bu_idx     is not None else None),  # P1
        (dest_idx,   headers[dest_idx]   if dest_idx   is not None else None),
        (origin_idx, headers[origin_idx] if origin_idx is not None else None),
    ]:
        if idx is not None:
            dim_idx = idx
            dim_key = name
            break

    if dim_idx is None:
        return [], None, None

    y_key = headers[count_idx] if count_idx is not None else "count"

    top = _top_values(rows, dim_idx, count_idx, top_n)
    if not top:
        return [], None, None

    chart_data = [{dim_key: item["label"], y_key: item["count"]} for item in top]
    return chart_data, dim_key, y_key
