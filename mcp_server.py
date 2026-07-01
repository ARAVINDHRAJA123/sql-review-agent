"""
MCP server for SQL Review Agent.

Tools exposed:
  review_sql   — full agentic review (static analysis + LLM rewrite)
  quick_check  — static analysis only, no LLM (instant, free)

Compatible with: Claude Code, OpenClaw, Cursor, Zed, any MCP client.

Run:  python mcp_server.py
Add to Claude Code:
  claude mcp add -s user sql-review -- /path/to/venv/bin/python /path/to/mcp_server.py
"""
from __future__ import annotations

import os
from mcp.server.fastmcp import FastMCP

from agent import review, ReviewReport
from tools.sql_tools import analyze, extract_tables

mcp = FastMCP("sql-review-agent")


@mcp.tool()
def review_sql(sql: str) -> str:
    """
    Full agentic review of a BigQuery SQL query.

    Runs static analysis, fetches table schemas from BigQuery,
    dry-runs for cost estimate, and suggests a rewrite.
    Returns a plain-text review report.

    Args:
        sql: The BigQuery SELECT query to review.
    """
    try:
        r: ReviewReport = review(sql.strip(), verbose=False)
        return r.plain_report
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Agent error: {e}"


@mcp.tool()
def quick_check(sql: str) -> str:
    """
    Instant static analysis of a BigQuery SQL query — no LLM, no BQ connection needed.

    Flags: SELECT *, missing partition filters, cartesian joins, CROSS JOINs.
    Does NOT dry-run or fetch schemas. Use review_sql for the full review.

    Args:
        sql: The BigQuery SELECT query to check.
    """
    try:
        result = analyze(sql.strip())
        tables = extract_tables(sql.strip())
        lines = [f"Severity: {result.severity.upper()}"]
        lines.append(f"Tables:   {', '.join(tables) if tables else 'none detected'}")
        if result.issues:
            lines.append(f"\nIssues ({len(result.issues)}):")
            for iss in result.issues:
                lines.append(f"  [{iss.severity.upper():8}] {iss.code} — {iss.message}")
                if iss.suggestion:
                    lines.append(f"             Fix: {iss.suggestion}")
        else:
            lines.append("\nNo static issues found.")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run()
