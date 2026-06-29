"""
SQL Review Agent — agentic review loop.

The agent:
  1. Runs static analysis immediately (no LLM needed)
  2. Uses an LLM tool-use loop to:
     - Fetch table schema & partition keys for each table in the query
     - Dry-run the SQL to get the cost estimate
     - Suggest a rewrite that fixes the flagged issues
     - Write the final structured report

Works on Claude (ANTHROPIC_API_KEY) or Gemini (GEMINI_API_KEY).

Trigger modes:
  CLI  → python agent.py --sql "SELECT * FROM ..."
  File → python agent.py --file query.sql
  MCP  → review_sql tool (any MCP-compatible client)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.bq_tools import dry_run, get_table_metadata, is_read_only, PROJECT
from tools.sql_tools import analyze, extract_tables, format_report

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_MODEL = "claude-opus-4-8"
GEMINI_MODEL    = "gemini-2.5-flash-lite"

BQ_DATASET = os.environ.get("BQ_DATASET", "analytics")


def _get_provider() -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    raise RuntimeError(
        "No LLM key found. Set ANTHROPIC_API_KEY (Claude) or GEMINI_API_KEY (free Gemini)."
    )


# ── Output type ───────────────────────────────────────────────────────────────

@dataclass
class ReviewReport:
    original_sql:       str
    severity:           str                    # none | low | medium | high | critical
    issues:             list[dict]             # from static analysis
    bytes_scanned_gb:   float
    estimated_cost_usd: float
    table_schemas:      dict[str, Any]         # table_id → metadata
    rewritten_sql:      str
    explanation:        str
    plain_report:       str = field(default="")

    def __post_init__(self):
        if not self.plain_report:
            self.plain_report = self._format()

    def _format(self) -> str:
        lines = ["── SQL Review Report ──\n"]
        lines.append(f"Severity:       {self.severity.upper()}")
        lines.append(f"Cost estimate:  {self.bytes_scanned_gb:.3f} GB  ≈  ${self.estimated_cost_usd:.6f} USD")
        if self.issues:
            lines.append(f"\nIssues ({len(self.issues)}):")
            order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            for iss in sorted(self.issues, key=lambda i: order.get(i.get("severity", "low"), 4)):
                lines.append(f"  [{iss['severity'].upper():8}] {iss['code']}")
                lines.append(f"             {iss['message']}")
                if iss.get("suggestion"):
                    lines.append(f"             Fix: {iss['suggestion']}")
        else:
            lines.append("\nNo issues found — SQL looks good!")
        if self.rewritten_sql and self.rewritten_sql.strip() != self.original_sql.strip():
            lines.append("\nRewritten SQL:")
            lines.append(self.rewritten_sql)
        if self.explanation:
            lines.append(f"\nExplanation: {self.explanation}")
        return "\n".join(lines)


# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_table_schema",
        "description": (
            "Get the BigQuery table schema, partition key, clustering fields, and row count. "
            "Call this for each table referenced in the SQL to understand if it is partitioned."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "dataset": {"type": "string"},
                "table":   {"type": "string"},
            },
            "required": ["project", "dataset", "table"],
        },
    },
    {
        "name": "dry_run_sql",
        "description": "Estimate the bytes scanned and cost for a SQL query without executing it.",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "The SQL to dry-run"}},
            "required": ["sql"],
        },
    },
    {
        "name": "write_report",
        "description": (
            "Write the final review report once you have collected table schemas and the cost estimate. "
            "Provide the rewritten SQL and a brief explanation of every change made."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rewritten_sql": {
                    "type": "string",
                    "description": "Improved SQL that fixes the flagged issues. Return original if no fix needed.",
                },
                "explanation": {
                    "type": "string",
                    "description": "1-3 sentences explaining what was changed and why.",
                },
            },
            "required": ["rewritten_sql", "explanation"],
        },
    },
]


# ── System prompt ─────────────────────────────────────────────────────────────

def _system(sql: str, static_issues: list, tables: list[str]) -> str:
    issues_str = json.dumps(static_issues, indent=2) if static_issues else "None found."
    return f"""You are a BigQuery SQL performance reviewer.

You have been given a SQL query to review. Static analysis has already identified these issues:

{issues_str}

Tables referenced: {', '.join(tables) if tables else 'none detected'}

Your job:
1. Call get_table_schema for each table to learn partition keys and clustering fields.
2. Call dry_run_sql to get the cost estimate.
3. Rewrite the SQL to fix the issues (add partition filters, remove SELECT *, add explicit columns,
   rewrite cartesian joins with proper ON conditions, etc.).
4. Call write_report with your rewritten SQL and a brief explanation.

Rules:
- If a table is partitioned, always add a WHERE filter on its partition column.
- Replace SELECT * with explicit columns based on the schema you fetch.
- Keep the query semantically equivalent — do not change its intent.
- If the SQL is already good (no issues), return the original SQL and say so.
- GCP project: {PROJECT or 'not set'}
- Default dataset: {BQ_DATASET}"""


# ── Tool dispatcher ───────────────────────────────────────────────────────────

def _dispatch(tool_name: str, args: dict) -> dict:
    if tool_name == "get_table_schema":
        return get_table_metadata(args["project"], args["dataset"], args["table"])
    if tool_name == "dry_run_sql":
        return dry_run(args["sql"])
    if tool_name == "write_report":
        return args  # handled by the caller
    return {"error": f"Unknown tool: {tool_name}"}


# ── Claude loop ───────────────────────────────────────────────────────────────

def _run_anthropic(sql: str, static_issues: list, tables: list[str],
                   verbose: bool, model: str | None) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": f"Review this SQL:\n\n{sql}"}]

    report_args: dict | None = None
    collected: dict = {}

    for _ in range(10):
        resp = client.messages.create(
            model=model or ANTHROPIC_MODEL,
            max_tokens=4096,
            system=_system(sql, static_issues, tables),
            tools=[{"name": t["name"], "description": t["description"],
                    "input_schema": t["input_schema"]} for t in TOOLS],
            messages=messages,
        )

        if resp.stop_reason == "end_turn":
            break

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                if verbose:
                    print(f"\n[tool] {block.name}({json.dumps(block.input, default=str)[:100]})", flush=True)
                if block.name == "write_report":
                    report_args = block.input
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Report recorded.",
                    })
                else:
                    out = _dispatch(block.name, block.input)
                    if block.name == "get_table_schema" and "error" not in out:
                        collected[block.input.get("table", "")] = out
                    if verbose:
                        print(f"   → {str(out)[:160]}", flush=True)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(out, default=str),
                        "is_error": "error" in out,
                    })
            messages.append({"role": "user", "content": results})
            if report_args:
                break

    return {"report_args": report_args or {}, "schemas": collected}


# ── Gemini loop ───────────────────────────────────────────────────────────────

def _run_gemini(sql: str, static_issues: list, tables: list[str],
                verbose: bool, model: str | None) -> dict:
    import time
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    client  = genai.Client(api_key=api_key)

    fn_decls = []
    for t in TOOLS:
        props = {}
        for pname, pdef in t["input_schema"].get("properties", {}).items():
            props[pname] = types.Schema(type="STRING", description=pdef.get("description", ""))
        fn_decls.append(types.FunctionDeclaration(
            name=t["name"], description=t["description"],
            parameters=types.Schema(
                type="OBJECT", properties=props,
                required=t["input_schema"].get("required", []),
            ),
        ))

    config = types.GenerateContentConfig(
        system_instruction=_system(sql, static_issues, tables),
        tools=[types.Tool(function_declarations=fn_decls)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    contents = [types.Content(role="user", parts=[
        types.Part(text=f"Review this SQL:\n\n{sql}")
    ])]

    report_args: dict | None = None
    collected: dict = {}

    def _gemini_call(c):
        for attempt in range(5):
            try:
                return client.models.generate_content(
                    model=model or GEMINI_MODEL, contents=c, config=config
                )
            except Exception as e:
                msg = str(e)
                if any(x in msg for x in ("429", "503", "UNAVAILABLE", "EXHAUSTED")) and attempt < 4:
                    wait = (attempt + 1) * 20
                    if verbose:
                        print(f"   [gemini] waiting {wait}s…", flush=True)
                    time.sleep(wait)
                else:
                    raise

    for _ in range(10):
        resp = _gemini_call(contents)
        calls = resp.function_calls
        if not calls:
            break

        contents.append(resp.candidates[0].content)
        for call in calls:
            args = dict(call.args or {})
            if verbose:
                print(f"\n[tool] {call.name}({json.dumps(args, default=str)[:100]})", flush=True)
            if call.name == "write_report":
                report_args = args
                contents.append(types.Content(role="user", parts=[
                    types.Part.from_function_response(
                        name=call.name, response={"result": "Report recorded."}
                    )
                ]))
            else:
                out = _dispatch(call.name, args)
                if call.name == "get_table_schema" and "error" not in out:
                    collected[args.get("table", "")] = out
                if verbose:
                    print(f"   → {str(out)[:160]}", flush=True)
                contents.append(types.Content(role="user", parts=[
                    types.Part.from_function_response(
                        name=call.name, response={"result": json.dumps(out, default=str)}
                    )
                ]))
        if report_args:
            break

    return {"report_args": report_args or {}, "schemas": collected}


# ── Public interface ──────────────────────────────────────────────────────────

def review(
    sql: str,
    verbose: bool = False,
    model: str | None = None,
) -> ReviewReport:
    """
    Review a BigQuery SQL query and return a ReviewReport.

    Steps:
      1. Static analysis (instant, no LLM)
      2. LLM loop: fetch schemas, dry-run, rewrite, report
    """
    if not is_read_only(sql):
        raise ValueError("Only SELECT/WITH queries can be reviewed.")

    # Step 1 — static analysis (fast, no LLM)
    tables       = extract_tables(sql)
    static       = analyze(sql)
    static_issues = [
        {"code": i.code, "severity": i.severity,
         "message": i.message, "suggestion": i.suggestion}
        for i in static.issues
    ]

    if verbose:
        print(f"[review] tables={tables} static_issues={len(static_issues)}", flush=True)

    # Step 2 — LLM loop
    provider = _get_provider()
    if provider == "anthropic":
        result = _run_anthropic(sql, static_issues, tables, verbose, model)
    else:
        result = _run_gemini(sql, static_issues, tables, verbose, model)

    report_args = result.get("report_args", {})
    schemas     = result.get("schemas", {})

    # Dry-run cost (may have been fetched by LLM; if not, fetch now)
    dr = dry_run(sql)
    gb   = dr.get("bytes_processed_gb", 0.0) if "error" not in dr else 0.0
    cost = dr.get("estimated_cost_usd", 0.0) if "error" not in dr else 0.0

    return ReviewReport(
        original_sql       = sql,
        severity           = static.severity,
        issues             = static_issues,
        bytes_scanned_gb   = gb,
        estimated_cost_usd = cost,
        table_schemas      = schemas,
        rewritten_sql      = report_args.get("rewritten_sql", sql),
        explanation        = report_args.get("explanation", ""),
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Review a BigQuery SQL query.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sql",  help="SQL string to review")
    group.add_argument("--file", help="Path to a .sql file")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--model", help="Override LLM model")
    args = parser.parse_args()

    sql = args.sql if args.sql else Path(args.file).read_text(encoding="utf-8")
    report = review(sql.strip(), verbose=args.verbose, model=args.model)
    print(report.plain_report)


if __name__ == "__main__":
    main()
