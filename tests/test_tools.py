"""
Unit tests for sql_tools and bq_tools (no BigQuery connection needed).
"""
from __future__ import annotations

import pytest
from tools.sql_tools import (
    SqlIssue, analyze, extract_columns, extract_tables, format_report, normalize
)
from tools.bq_tools import is_read_only


# ── is_read_only ────────────────────────────────────────────────────────────

def test_read_only_select():
    assert is_read_only("SELECT * FROM `p.d.t`")

def test_read_only_with():
    assert is_read_only("WITH cte AS (SELECT 1) SELECT * FROM cte")

def test_read_only_rejects_delete():
    assert not is_read_only("DELETE FROM `p.d.t` WHERE 1=1")

def test_read_only_rejects_insert():
    assert not is_read_only("INSERT INTO t SELECT * FROM s")

def test_read_only_rejects_drop():
    assert not is_read_only("DROP TABLE `p.d.t`")

def test_read_only_rejects_merge():
    assert not is_read_only("MERGE `p.d.t` USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET x=1")


# ── extract_tables ───────────────────────────────────────────────────────────

def test_extract_tables_single():
    sql = "SELECT id FROM `my-project.analytics.fct_transactions`"
    assert extract_tables(sql) == ["my-project.analytics.fct_transactions"]

def test_extract_tables_join():
    sql = """
        SELECT a.id, b.name
        FROM `proj.ds.orders` a
        JOIN `proj.ds.customers` b ON a.customer_id = b.id
    """
    tables = extract_tables(sql)
    assert "proj.ds.orders" in tables
    assert "proj.ds.customers" in tables

def test_extract_tables_empty():
    assert extract_tables("SELECT 1 + 1") == []


# ── extract_columns ──────────────────────────────────────────────────────────

def test_extract_columns_star():
    assert "*" in extract_columns("SELECT * FROM `p.d.t`")

def test_extract_columns_named():
    cols = extract_columns("SELECT id, name, amount FROM `p.d.t`")
    assert "id" in cols
    assert "name" in cols
    assert "amount" in cols


# ── normalize ────────────────────────────────────────────────────────────────

def test_normalize_collapses_whitespace():
    assert normalize("SELECT  *\n  FROM   t ;") == "SELECT * FROM t"


# ── analyze — SELECT * ───────────────────────────────────────────────────────

def test_analyze_select_star_flagged():
    result = analyze("SELECT * FROM `p.d.t` WHERE id = 1")
    codes = [i.code for i in result.issues]
    assert "SELECT_STAR" in codes

def test_analyze_explicit_columns_no_star_flag():
    result = analyze("SELECT id, amount FROM `p.d.t` WHERE id = 1")
    codes = [i.code for i in result.issues]
    assert "SELECT_STAR" not in codes


# ── analyze — no WHERE ───────────────────────────────────────────────────────

def test_analyze_no_where_flagged():
    result = analyze("SELECT id FROM `p.d.t`")
    codes = [i.code for i in result.issues]
    assert "NO_WHERE_CLAUSE" in codes

def test_analyze_with_where_not_flagged():
    result = analyze("SELECT id FROM `p.d.t` WHERE date = '2026-01-01'")
    codes = [i.code for i in result.issues]
    assert "NO_WHERE_CLAUSE" not in codes


# ── analyze — partition filter ───────────────────────────────────────────────

def test_analyze_missing_partition_filter():
    result = analyze(
        "SELECT id FROM `p.d.t` WHERE id = 1",
        partitioned_tables=["p.d.t"]
    )
    codes = [i.code for i in result.issues]
    assert "NO_PARTITION_FILTER" in codes

def test_analyze_partition_filter_present():
    result = analyze(
        "SELECT id FROM `p.d.t` WHERE _PARTITIONDATE = '2026-01-01'",
        partitioned_tables=["p.d.t"]
    )
    codes = [i.code for i in result.issues]
    assert "NO_PARTITION_FILTER" not in codes


# ── analyze — CROSS JOIN ─────────────────────────────────────────────────────

def test_analyze_cross_join_flagged():
    result = analyze("SELECT * FROM `p.d.a` CROSS JOIN `p.d.b`")
    codes = [i.code for i in result.issues]
    assert "CROSS_JOIN" in codes

def test_analyze_implicit_cross_join_flagged():
    result = analyze("SELECT * FROM `p.d.a`, `p.d.b`")
    codes = [i.code for i in result.issues]
    assert "IMPLICIT_CROSS_JOIN" in codes


# ── severity rollup ──────────────────────────────────────────────────────────

def test_severity_critical_dominates():
    result = analyze("SELECT * FROM `p.d.a` CROSS JOIN `p.d.b`")
    assert result.severity == "critical"

def test_severity_none_on_clean_query():
    result = analyze(
        "SELECT id, amount FROM `p.d.t` WHERE _PARTITIONDATE = '2026-01-01'",
        partitioned_tables=["p.d.t"]
    )
    assert result.severity == "none"


# ── format_report ────────────────────────────────────────────────────────────

def test_format_report_contains_severity():
    result = analyze("SELECT * FROM `p.d.t` CROSS JOIN `p.d.b`")
    report = format_report(result)
    assert "CRITICAL" in report

def test_format_report_with_cost():
    result = analyze("SELECT id FROM `p.d.t` WHERE date = '2026-01-01'")
    dry = {"bytes_processed_gb": 1.5, "estimated_cost_usd": 0.009375}
    report = format_report(result, dry_run_result=dry)
    assert "1.50 GB" in report
    assert "0.009" in report
