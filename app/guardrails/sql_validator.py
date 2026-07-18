"""SQL validation and anti-hallucination guardrails.

Multi-pass validation pipeline that checks LLM-generated SQL before execution:
1. Basic syntax validation (parseable SQL)
2. Table whitelist enforcement (only allowed tables)
3. Column validation (only columns that exist in the schema)
4. Redshift/PostgreSQL syntax detection (wrong dialect)
5. Injection prevention (semicolons, UNION injection, DDL)
6. Broad query detection (missing WHERE on large table)
7. Safety limits (auto-add LIMIT if missing)
"""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Set, Tuple

import sqlparse

from app.guardrails.schema_registry import (
    VALID_COLUMN_NAMES,
    ALLOWED_TABLE_ALIASES,
    is_valid_column,
    is_valid_table,
)

logger = logging.getLogger(__name__)


class Severity(Enum):
    """Severity of a validation issue."""
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class ValidationIssue:
    """A single validation issue found in SQL."""
    severity: Severity
    code: str
    message: str
    suggestion: Optional[str] = None


@dataclass
class ValidationResult:
    """Result of SQL validation."""
    is_valid: bool
    sql: str
    issues: List[ValidationIssue] = field(default_factory=list)
    was_modified: bool = False

    @property
    def errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    @property
    def error_summary(self) -> str:
        """Summarize errors for LLM repair prompt."""
        if not self.errors:
            return ""
        return "; ".join(e.message for e in self.errors)


# Redshift/PostgreSQL syntax patterns to detect
_REDSHIFT_PATTERNS = [
    (re.compile(r"DATEDIFF\s*\(\s*['\"]\w+['\"]", re.IGNORECASE),
     "REDSHIFT_DATEDIFF",
     "Redshift DATEDIFF syntax detected. Use Databricks: DATEDIFF(endDate, startDate)"),
    (re.compile(r"DATEDIFF\s*\(\s*(?:DAY|MONTH|YEAR|HOUR|MINUTE|SECOND)\s*,", re.IGNORECASE),
     "REDSHIFT_DATEDIFF_KEYWORD",
     "Redshift DATEDIFF(unit, ...) syntax. Use Databricks: DATEDIFF(endDate, startDate)"),
    (re.compile(r"\bGETDATE\s*\(\s*\)", re.IGNORECASE),
     "REDSHIFT_GETDATE",
     "GETDATE() is Redshift/SQL Server. Use CURRENT_DATE() or CURRENT_TIMESTAMP()"),
    (re.compile(r"\bCONVERT\s*\(\s*\w+\s*,", re.IGNORECASE),
     "REDSHIFT_CONVERT",
     "CONVERT(type, expr) is Redshift. Use CAST(expr AS type)"),
    (re.compile(r"DATEADD\s*\(\s*['\"]\w+['\"]", re.IGNORECASE),
     "REDSHIFT_DATEADD",
     "Redshift DATEADD syntax. Use Databricks: DATE_ADD(date, n) or INTERVAL"),
    (re.compile(r"\bSELECT\s+TOP\s+\d+", re.IGNORECASE),
     "SQL_SERVER_TOP",
     "TOP N is SQL Server/Redshift. Use LIMIT N at end of query"),
    (re.compile(r"\bNVL\s*\(", re.IGNORECASE),
     "REDSHIFT_NVL",
     "NVL() is Redshift/Oracle. Use COALESCE() in Databricks SQL"),
    (re.compile(r"\bISNULL\s*\(\s*\w+\s*,", re.IGNORECASE),
     "SQL_SERVER_ISNULL",
     "ISNULL(expr, replacement) is SQL Server. Use COALESCE() or IFNULL()"),
    (re.compile(r"::\s*(?:text|varchar|int|integer|float|date|timestamp|numeric)", re.IGNORECASE),
     "POSTGRES_CAST",
     "PostgreSQL :: casting. Use CAST(expr AS type)"),
]

# Injection patterns
_INJECTION_PATTERNS = [
    (re.compile(r";\s*(?:SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|EXEC|EXECUTE)", re.IGNORECASE),
     "INJECTION_MULTI_STATEMENT",
     "Multiple SQL statements detected (possible injection)"),
    (re.compile(r"UNION\s+(?:ALL\s+)?SELECT\s+(?:NULL|1|2|3|')", re.IGNORECASE),
     "INJECTION_UNION",
     "Suspicious UNION SELECT pattern (possible injection)"),
    (re.compile(r"\b(?:information_schema|pg_catalog|sys\.|system\.)\b", re.IGNORECASE),
     "INJECTION_SYSTEM_TABLE",
     "System table access not allowed"),
]

DEFAULT_LIMIT = 500
MAX_LIMIT = 10000


class SQLValidator:
    """Validates LLM-generated SQL against guardrails."""

    def validate(self, sql: str, auto_limit: bool = True) -> ValidationResult:
        """Run all validation passes on a SQL statement."""
        issues: List[ValidationIssue] = []
        modified_sql = sql.strip().rstrip(";")

        issues.extend(self._check_syntax(modified_sql))
        issues.extend(self._check_dialect(modified_sql))
        issues.extend(self._check_injection(modified_sql))
        issues.extend(self._check_columns(modified_sql))
        issues.extend(self._check_tables(modified_sql))
        issues.extend(self._check_broad_query(modified_sql))

        if auto_limit:
            modified_sql, limit_issues = self._enforce_limit(modified_sql)
            issues.extend(limit_issues)

        has_errors = any(i.severity == Severity.ERROR for i in issues)
        return ValidationResult(
            is_valid=not has_errors,
            sql=modified_sql,
            issues=issues,
            was_modified=(modified_sql != sql.strip().rstrip(";")),
        )

    def _check_syntax(self, sql: str) -> List[ValidationIssue]:
        """Pass 1: Check if SQL is parseable."""
        issues = []
        try:
            parsed = sqlparse.parse(sql)
            if not parsed or not parsed[0].tokens:
                issues.append(ValidationIssue(
                    severity=Severity.ERROR, code="EMPTY_SQL",
                    message="SQL is empty or unparseable"))
        except Exception as e:
            issues.append(ValidationIssue(
                severity=Severity.ERROR, code="PARSE_ERROR",
                message=f"SQL parse error: {str(e)[:100]}"))
        return issues

    def _check_dialect(self, sql: str) -> List[ValidationIssue]:
        """Pass 2: Detect Redshift/PostgreSQL/SQL Server syntax."""
        issues = []
        for pattern, code, message in _REDSHIFT_PATTERNS:
            if pattern.search(sql):
                issues.append(ValidationIssue(
                    severity=Severity.ERROR, code=code,
                    message=message,
                    suggestion="Rewrite using Databricks SQL syntax"))
        return issues

    def _check_injection(self, sql: str) -> List[ValidationIssue]:
        """Pass 3: Detect injection attempts."""
        issues = []
        for pattern, code, message in _INJECTION_PATTERNS:
            if pattern.search(sql):
                issues.append(ValidationIssue(
                    severity=Severity.ERROR, code=code, message=message))
        return issues

    def _check_columns(self, sql: str) -> List[ValidationIssue]:
        """Pass 4: Check that referenced columns exist in schema."""
        issues = []
        referenced = self._extract_column_references(sql)
        unknown = referenced - VALID_COLUMN_NAMES
        sql_keywords = {
            "count", "sum", "avg", "min", "max", "distinct", "as", "from",
            "where", "and", "or", "not", "in", "between", "like", "ilike",
            "is", "null", "true", "false", "case", "when", "then", "else",
            "end", "group", "by", "order", "asc", "desc", "limit", "offset",
            "having", "join", "on", "left", "right", "inner", "outer", "full",
            "cross", "union", "all", "exists", "any", "some", "with",
            "select", "current_date", "current_timestamp", "date", "cast",
            "coalesce", "ifnull", "greatest", "least", "datediff", "date_add",
            "date_sub", "date_trunc", "concat", "upper", "lower", "trim",
            "substring", "replace", "length", "round", "floor", "ceil",
            "abs", "extract", "year", "month", "day", "hour", "minute",
            "interval", "over", "partition", "row_number", "rank",
            "dense_rank", "lag", "lead", "first_value", "last_value",
            "months_between", "add_months", "to_date", "to_timestamp",
        }
        unknown = unknown - sql_keywords
        aliases = self._extract_aliases(sql)
        unknown = unknown - aliases
        # Filter table name components (catalog.schema.table parts)
        table_parts = {"onedata_fn_ion_dev", "ion_l0_raw", "lbn_with_scorecard"}
        unknown = {c for c in unknown if len(c) > 1 and not c.isdigit() and c not in table_parts}
        # Also filter CTE/subquery aliases
        cte_names = {m.lower() for m in re.findall(r"\bWITH\s+(\w+)\s+AS|,\s*(\w+)\s+AS", sql, re.IGNORECASE) for m in m if m}
        unknown = unknown - cte_names

        if unknown:
            issues.append(ValidationIssue(
                severity=Severity.WARNING, code="UNKNOWN_COLUMNS",
                message=f"Possible unknown column(s): {', '.join(sorted(unknown)[:5])}",
                suggestion="Verify these are valid column names or aliases"))
        return issues

    def _check_tables(self, sql: str) -> List[ValidationIssue]:
        """Pass 5: Check that referenced tables are whitelisted."""
        issues = []
        # Extract CTE names (WITH name AS ...)
        cte_names = {m.lower() for m in re.findall(r"\bWITH\s+(\w+)\s+AS|,\s*(\w+)\s+AS", sql, re.IGNORECASE) for m in m if m}
        aliases = self._extract_aliases(sql) | cte_names
        table_pattern = re.compile(r"(?:FROM|JOIN)\s+([`\w.]+)", re.IGNORECASE)
        matches = table_pattern.findall(sql)
        for table_ref in matches:
            clean = table_ref.strip("`").strip().lower()
            if clean in aliases:
                continue
            if not is_valid_table(clean):
                issues.append(ValidationIssue(
                    severity=Severity.ERROR, code="UNAUTHORIZED_TABLE",
                    message=f"Table \'{table_ref}\' is not in the allowed list",
                    suggestion="Only onedata_fn_ion_dev.ion_l0_raw.lbn_with_scorecard is allowed"))
        return issues

    def _check_broad_query(self, sql: str) -> List[ValidationIssue]:
        """Pass 6: Detect overly broad queries."""
        issues = []
        has_select_star = bool(re.search(r"SELECT\s+\*", sql, re.IGNORECASE))
        has_where = "WHERE" in sql.upper()
        has_limit = "LIMIT" in sql.upper()
        if has_select_star and not has_where and not has_limit:
            issues.append(ValidationIssue(
                severity=Severity.WARNING, code="BROAD_QUERY",
                message="SELECT * without WHERE clause may return too many rows",
                suggestion="Add a WHERE clause or LIMIT to restrict results"))
        return issues

    def _enforce_limit(self, sql: str) -> Tuple[str, List[ValidationIssue]]:
        """Pass 7: Auto-add LIMIT if missing."""
        issues = []
        if re.search(r"LIMIT\s+\d+\s*$", sql, re.IGNORECASE):
            limit_match = re.search(r"LIMIT\s+(\d+)\s*$", sql, re.IGNORECASE)
            if limit_match:
                limit_val = int(limit_match.group(1))
                if limit_val > MAX_LIMIT:
                    issues.append(ValidationIssue(
                        severity=Severity.WARNING, code="HIGH_LIMIT",
                        message=f"LIMIT {limit_val} exceeds maximum ({MAX_LIMIT})"))
                    sql = re.sub(r"LIMIT\s+\d+\s*$", f"LIMIT {MAX_LIMIT}", sql, flags=re.IGNORECASE)
            return sql, issues

        if self._is_aggregate_only(sql):
            return sql, issues

        sql = f"{sql}\nLIMIT {DEFAULT_LIMIT}"
        issues.append(ValidationIssue(
            severity=Severity.INFO, code="LIMIT_ADDED",
            message=f"Auto-added LIMIT {DEFAULT_LIMIT} for safety"))
        return sql, issues

    @staticmethod
    def _extract_column_references(sql: str) -> Set[str]:
        """Extract potential column name references from SQL."""
        # Remove string literals before extracting identifiers
        cleaned = re.sub(r"'[^']*'", "", sql)
        cleaned = re.sub(r'"[^"]*"', "", cleaned)
        tokens = re.findall(r"\b([a-z_][a-z0-9_]*)\b", cleaned.lower())
        return set(tokens)

    @staticmethod
    def _extract_aliases(sql: str) -> Set[str]:
        """Extract column/table aliases from SQL."""
        alias_pattern = re.compile(r"\bAS\s+[`]?([\w]+)[`]?", re.IGNORECASE)
        matches = alias_pattern.findall(sql)
        return {m.lower() for m in matches}

    @staticmethod
    def _is_aggregate_only(sql: str) -> bool:
        """Check if query returns only aggregate results."""
        sql_upper = sql.upper()
        has_agg = bool(re.search(r"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(", sql_upper))
        if has_agg:
            select_match = re.search(r"SELECT\s+(.*?)\s+FROM", sql_upper, re.DOTALL)
            if select_match:
                select_list = select_match.group(1)
                items = [i.strip() for i in select_list.split(",")]
                all_agg = all(
                    re.search(r"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(", item)
                    for item in items)
                if all_agg:
                    return True
        return False
