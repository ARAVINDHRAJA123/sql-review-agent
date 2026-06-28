"""
SQL parsing and static analysis helpers.

These run purely in Python — no BigQuery connection needed.
They catch obvious issues before the query even reaches BigQuery.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ── Data types ─────────────────────────────────────────────────────────────

@dataclass
class SqlIssue:
    """A single issue found in a SQL query."""
    code:        str            # e.g. "NO_PARTITION_FILTER"
    severity:    str            # critical | high | medium | low
    message:     str            # human-readable description
    suggestion:  str = ""       # how to fix it


@dataclass
class StaticAnalysisResult:
    issues:  list[SqlIssue] = field(default_factory=list)
    tables:  list[str]      = field(default_factory=list)  # fully-qualified table refs
    columns: list[str]      = field(default_factory=list)  # selected columns (empty = SELECT *)

    @property
    def has_select_star(self) -> bool:
        return not self.columns or "*" in self.columns

    @property
    def severity(self) -> str:
        """Highest severity across all issues."""
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        if not self.issues:
            return "none"
        return min(self.issues, key=lambda i: order.get(i.severity, 4)).severity


# ── Helpers ────────────────────────────────────────────────────────────────

_TABLE_REF = re.compile(
    r"(?:FROM|JOIN)\s+`?([a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+)`?",
    re.IGNORECASE,
)
_SELECT_COLS = re.compile(
    r"^\s*SELECT\s+(.*?)\s+FROM\b",
    re.IGNORECASE | re.DOTALL,
)
_CROSS_JOIN = re.compile(r"\bCROSS\s+JOIN\b", re.IGNORECASE)
_COMMA_JOIN = re.compile(
    r"FROM\s+`?[\w.\-]+`?\s*,\s*`?[\w.\-]+`?",
    re.IGNORECASE,
)
_PARTITION_FILTER = re.compile(
    r"WHERE\s+.*?(?:_PARTITIONDATE|_PARTITIONTIME|DATE|TIMESTAMP)",
    re.IGNORECASE | re.DOTALL,
)
_WHERE_CLAUSE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_LIMIT = re.compile(r"\bLIMIT\s+\d+\b", re.IGNORECASE)
_WILDCARD = re.compile(r"SELECT\s+\*", re.IGNORECASE)
_SUBQUERY = re.compile(r"SELECT.*?SELECT", re.IGNORECASE | re.DOTALL)


def extract_tables(sql: str) -> list[str]:
    """Extract fully-qualified table references from a SQL query."""
    return list(dict.fromkeys(_TABLE_REF.findall(sql)))


def extract_columns(sql: str) -> list[str]:
    """Extract selected columns. Returns ['*'] for SELECT *."""
    m = _SELECT_COLS.search(sql)
    if not m:
        return []
    raw = m.group(1).strip()
    if raw == "*":
        return ["*"]
    # Split on commas not inside parens
    depth, cols, cur = 0, [], []
    for ch in raw:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            cols.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        cols.append("".join(cur).strip())
    return [c.split()[-1].strip("`") for c in cols if c.strip()]


def normalize(sql: str) -> str:
    """Collapse whitespace and strip trailing semicolon."""
    return re.sub(r"\s+", " ", sql.strip().rstrip("; ").rstrip()).strip()


# ── Static analysis ─────────────────────────────────────────────────────────

def analyze(sql: str, partitioned_tables: list[str] | None = None) -> StaticAnalysisResult:
    """
    Run static analysis on a SQL query.

    Args:
        sql: the BigQuery SQL to analyze
        partitioned_tables: list of table IDs known to be partitioned
                            (used to flag missing partition filter)
    Returns:
        StaticAnalysisResult with issues, tables, columns found
    """
    result = StaticAnalysisResult(
        tables  = extract_tables(sql),
        columns = extract_columns(sql),
    )
    partitioned_tables = partitioned_tables or []

    # ── SELECT * ──
    if _WILDCARD.search(sql):
        result.issues.append(SqlIssue(
            code       = "SELECT_STAR",
            severity   = "medium",
            message    = "SELECT * reads all columns — specify only the columns you need.",
            suggestion = "Replace SELECT * with explicit column names to reduce bytes scanned.",
        ))

    # ── No WHERE clause ──
    if not _WHERE_CLAUSE.search(sql):
        result.issues.append(SqlIssue(
            code       = "NO_WHERE_CLAUSE",
            severity   = "high",
            message    = "No WHERE clause — query will scan the entire table.",
            suggestion = "Add a WHERE clause to filter rows, especially on the partition column.",
        ))

    # ── Missing partition filter ──
    elif partitioned_tables:
        has_part_filter = bool(_PARTITION_FILTER.search(sql))
        if not has_part_filter:
            result.issues.append(SqlIssue(
                code       = "NO_PARTITION_FILTER",
                severity   = "high",
                message    = "Partitioned table queried without a partition filter — full table scan.",
                suggestion = (
                    "Add a filter on the partition column "
                    "(e.g. WHERE _PARTITIONDATE = '2026-01-01' or WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY))."
                ),
            ))

    # ── CROSS JOIN ──
    if _CROSS_JOIN.search(sql):
        result.issues.append(SqlIssue(
            code       = "CROSS_JOIN",
            severity   = "critical",
            message    = "CROSS JOIN detected — produces a cartesian product (rows × rows).",
            suggestion = "Replace with an INNER JOIN or LEFT JOIN with an ON condition.",
        ))

    # ── Implicit cartesian (FROM a, b) ──
    if _COMMA_JOIN.search(sql):
        result.issues.append(SqlIssue(
            code       = "IMPLICIT_CROSS_JOIN",
            severity   = "critical",
            message    = "Comma-separated tables in FROM clause — implicit cartesian join.",
            suggestion = "Use explicit JOIN syntax with an ON condition.",
        ))

    # ── No LIMIT on large result ──
    if not _LIMIT.search(sql) and not _WHERE_CLAUSE.search(sql):
        result.issues.append(SqlIssue(
            code       = "NO_LIMIT",
            severity   = "low",
            message    = "No LIMIT clause — may return a very large result set.",
            suggestion = "Add LIMIT N during development; remove for production aggregations.",
        ))

    return result


def format_report(result: StaticAnalysisResult, dry_run_result: dict | None = None) -> str:
    """Format a plain-text review report from analysis + optional dry_run result."""
    lines = ["── SQL Review Report ──"]

    if dry_run_result and "error" not in dry_run_result:
        gb   = dry_run_result.get("bytes_processed_gb", 0)
        cost = dry_run_result.get("estimated_cost_usd", 0)
        lines.append(f"Cost estimate:  {gb:.2f} GB scanned  ≈  ${cost:.4f} USD")

    if result.tables:
        lines.append(f"Tables:         {', '.join(result.tables)}")

    if not result.issues:
        lines.append("Issues:         none — looks good!")
    else:
        lines.append(f"Issues ({len(result.issues)}):")
        for iss in sorted(result.issues, key=lambda i: {"critical":0,"high":1,"medium":2,"low":3}.get(i.severity,4)):
            lines.append(f"  [{iss.severity.upper():8}] {iss.code}")
            lines.append(f"             {iss.message}")
            if iss.suggestion:
                lines.append(f"             Fix: {iss.suggestion}")

    lines.append(f"Severity:       {result.severity.upper()}")
    return "\n".join(lines)
