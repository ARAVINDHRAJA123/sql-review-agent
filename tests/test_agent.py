"""Tests for agent.py — no BQ or LLM connection needed."""
from __future__ import annotations

import pytest
from agent import ReviewReport


def _make_report(**kwargs) -> ReviewReport:
    defaults = dict(
        original_sql       = "SELECT * FROM `p.d.t`",
        severity           = "high",
        issues             = [{"code": "SELECT_STAR", "severity": "medium",
                               "message": "SELECT * detected", "suggestion": "Use explicit columns"}],
        bytes_scanned_gb   = 2.5,
        estimated_cost_usd = 0.015625,
        table_schemas      = {},
        rewritten_sql      = "SELECT id, name FROM `p.d.t` WHERE date = '2026-01-01'",
        explanation        = "Added explicit columns and partition filter.",
    )
    defaults.update(kwargs)
    return ReviewReport(**defaults)


def test_report_contains_severity():
    r = _make_report()
    assert "HIGH" in r.plain_report


def test_report_contains_cost():
    r = _make_report()
    assert "2.500 GB" in r.plain_report


def test_report_contains_issue_code():
    r = _make_report()
    assert "SELECT_STAR" in r.plain_report


def test_report_shows_rewrite_when_different():
    r = _make_report()
    assert "SELECT id, name" in r.plain_report


def test_report_no_rewrite_section_when_same():
    r = _make_report(
        original_sql  = "SELECT id FROM `p.d.t` WHERE date = '2026-01-01'",
        rewritten_sql = "SELECT id FROM `p.d.t` WHERE date = '2026-01-01'",
        severity      = "none",
        issues        = [],
    )
    assert "No issues found" in r.plain_report
    assert "Rewritten SQL" not in r.plain_report


def test_report_explanation_included():
    r = _make_report()
    assert "Added explicit columns" in r.plain_report


def test_review_rejects_dml():
    from agent import review
    with pytest.raises(ValueError, match="Only SELECT"):
        review("DELETE FROM `p.d.t` WHERE 1=1")
