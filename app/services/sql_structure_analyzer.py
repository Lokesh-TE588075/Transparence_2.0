"""SQL structural analyzer for diagnostic tracing.

Extracts metadata about a SQL statement without storing the full query text.
Used exclusively by the diagnostic trace layer — no business logic depends on
this module.

Output is safe to persist: column names, function names, boolean flags.
No query parameter values, no row data, no authentication data.

All functions return safe defaults when given None or empty input.
All functions swallow exceptions to avoid disrupting the calling context.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

_SELECT_COLS_RE = re.compile(
    r"SELECT\s+(.*?)\s+FROM\b",
    re.IGNORECASE | re.DOTALL,
)
_AGG_FUNCS_RE = re.compile(
    r"\b(COUNT|SUM|AVG|MIN|MAX|MEDIAN|STDDEV|VARIANCE|PERCENTILE)\s*\(",
    re.IGNORECASE,
)
_GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)
_ORDER_BY_RE = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)
_LIMIT_RE    = re.compile(r"\bLIMIT\s+\d+", re.IGNORECASE)
_DATE_TRUNC_RE = re.compile(
    r"\b(DATE_TRUNC|DATE_FORMAT|TRUNC|YEAR|MONTH|QUARTER|WEEK|DAY)\s*\(",
    re.IGNORECASE,
)
_WHERE_RE = re.compile(r"\bWHERE\b(.*?)(?:\bGROUP\b|\bORDER\b|\bLIMIT\b|\bHAVING\b|$)",
                        re.IGNORECASE | re.DOTALL)
_IDENT_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b")
_SQL_KEYWORDS = frozenset({
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "IS", "NULL",
    "TRUE", "FALSE", "LIKE", "BETWEEN", "AS", "ON", "JOIN", "LEFT",
    "RIGHT", "INNER", "OUTER", "FULL", "CROSS", "UNION", "ALL",
    "DISTINCT", "HAVING", "GROUP", "BY", "ORDER", "LIMIT", "OFFSET",
    "WITH", "CASE", "WHEN", "THEN", "ELSE", "END", "EXISTS", "ANY",
    "SOME", "CURRENT_DATE", "CURRENT_TIMESTAMP", "CAST", "COALESCE",
    "NULLIF", "IIF", "IF", "DATE", "TIMESTAMP", "STRING", "INT",
    "BIGINT", "DOUBLE", "FLOAT", "BOOLEAN",
})


def _safe_col_names(raw_select: str) -> List[str]:
    """Extract column/expression names from a SELECT clause."""
    cols: List[str] = []
    for token in raw_select.split(","):
        token = token.strip()
        # Take last identifier (handles "expr AS alias")
        parts = re.split(r"\s+AS\s+", token, flags=re.IGNORECASE)
        name_part = parts[-1].strip().strip("`\"'")
        # Skip complex expressions; keep simple names
        if name_part and re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name_part):
            if name_part.upper() not in _SQL_KEYWORDS:
                cols.append(name_part)
    return cols[:20]  # cap at 20 columns


def _where_field_names(where_clause: str) -> List[str]:
    """Extract field names referenced in a WHERE clause."""
    fields: List[str] = []
    for m in _IDENT_RE.finditer(where_clause):
        name = m.group(1)
        if name.upper() not in _SQL_KEYWORDS and len(name) > 1:
            if name not in fields:
                fields.append(name)
    return fields[:20]


def analyze_sql_structure(sql: Optional[str]) -> Dict[str, Any]:
    """Extract structural metadata from a SQL statement.

    Args:
        sql: Raw SQL string.  May be None or empty.

    Returns:
        Dict with structural fields.  No raw values are included.
        Safe to write to a diagnostic table.
    """
    empty: Dict[str, Any] = {
        "sql_length": 0,
        "sql_hash": None,
        "selected_columns": [],
        "aggregate_functions": [],
        "has_group_by": False,
        "has_order_by": False,
        "has_limit": False,
        "has_date_truncation": False,
        "where_clause_fields": [],
        "is_select": False,
        "is_insert": False,
        "is_update": False,
    }
    if not sql or not sql.strip():
        return empty

    try:
        sql_upper = sql.strip()
        result: Dict[str, Any] = {
            "sql_length": len(sql),
            "sql_hash": hashlib.sha256(sql.encode("utf-8", errors="replace")).hexdigest()[:16],
            "selected_columns": [],
            "aggregate_functions": [],
            "has_group_by": bool(_GROUP_BY_RE.search(sql)),
            "has_order_by": bool(_ORDER_BY_RE.search(sql)),
            "has_limit": bool(_LIMIT_RE.search(sql)),
            "has_date_truncation": bool(_DATE_TRUNC_RE.search(sql)),
            "where_clause_fields": [],
            "is_select": sql_upper.upper().startswith("SELECT"),
            "is_insert": sql_upper.upper().startswith("INSERT"),
            "is_update": sql_upper.upper().startswith("UPDATE"),
        }

        # SELECT columns
        select_m = _SELECT_COLS_RE.search(sql)
        if select_m:
            result["selected_columns"] = _safe_col_names(select_m.group(1))

        # Aggregate functions
        result["aggregate_functions"] = list({
            m.group(1).upper() for m in _AGG_FUNCS_RE.finditer(sql)
        })

        # WHERE fields
        where_m = _WHERE_RE.search(sql)
        if where_m:
            result["where_clause_fields"] = _where_field_names(where_m.group(1))

        return result

    except Exception:
        return empty
