"""
LangGraph version of the SQL review agent.

Replaces the raw for-loop in agent._run_anthropic() with an explicit
state machine: nodes for LLM calls and tool dispatch, a conditional
edge for routing, and automatic checkpointing.

Same inputs/outputs as agent.review() — drop-in replacement.

Usage:
  from graph_agent import review_with_graph
  report = review_with_graph(sql)
"""
from __future__ import annotations

import json
import os
from typing import TypedDict, Annotated, Any

from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

from tools.bq_tools import dry_run, get_table_metadata, PROJECT
from tools.sql_tools import analyze, extract_tables
from agent import ReviewReport, TOOLS, _system, BQ_DATASET, ANTHROPIC_MODEL

import anthropic


# ── State ─────────────────────────────────────────────────────────────────────

class ReviewState(TypedDict):
    messages:    Annotated[list, add_messages]
    sql:         str
    report_args: dict          # filled when LLM calls write_report
    schemas:     dict[str, Any]


# ── Nodes ─────────────────────────────────────────────────────────────────────

def call_llm(state: ReviewState) -> dict:
    client = anthropic.Anthropic()

    # Convert LangChain messages → Anthropic format
    anthropic_msgs = []
    for m in state["messages"]:
        if isinstance(m, HumanMessage):
            anthropic_msgs.append({"role": "user", "content": m.content})
        elif isinstance(m, AIMessage):
            if m.tool_calls:
                blocks = [{"type": "tool_use", "id": tc["id"],
                           "name": tc["name"], "input": tc["args"]}
                          for tc in m.tool_calls]
                anthropic_msgs.append({"role": "assistant", "content": blocks})
            else:
                anthropic_msgs.append({"role": "assistant", "content": m.content})
        elif isinstance(m, ToolMessage):
            anthropic_msgs.append({
                "role": "user",
                "content": [{"type": "tool_result",
                              "tool_use_id": m.tool_call_id,
                              "content": m.content}]
            })

    static_issues = state.get("_static_issues", [])
    tables        = state.get("_tables", [])

    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        system=_system(state["sql"], static_issues, tables),
        tools=[{"name": t["name"], "description": t["description"],
                "input_schema": t["input_schema"]} for t in TOOLS],
        messages=anthropic_msgs,
    )

    if resp.stop_reason == "tool_use":
        tool_calls = [
            {"id": b.id, "name": b.name, "args": b.input}
            for b in resp.content if b.type == "tool_use"
        ]
        return {"messages": [AIMessage(content="", tool_calls=tool_calls)]}
    else:
        text = next((b.text for b in resp.content if hasattr(b, "text")), "")
        return {"messages": [AIMessage(content=text)]}


def run_tools(state: ReviewState) -> dict:
    last       = state["messages"][-1]
    new_msgs   = []
    schemas    = dict(state.get("schemas", {}))
    report_args = dict(state.get("report_args", {}))

    for tc in last.tool_calls:
        name = tc["name"]
        args = tc["args"]

        if name == "write_report":
            report_args = args
            result = "Report recorded."
        elif name == "get_table_schema":
            out = get_table_metadata(args["project"], args["dataset"], args["table"])
            if "error" not in out:
                schemas[args.get("table", "")] = out
            result = json.dumps(out, default=str)
        elif name == "dry_run_sql":
            out = dry_run(args["sql"])
            result = json.dumps(out, default=str)
        else:
            result = json.dumps({"error": f"Unknown tool: {name}"})

        new_msgs.append(ToolMessage(content=result, tool_call_id=tc["id"]))

    return {"messages": new_msgs, "schemas": schemas, "report_args": report_args}


# ── Edge ──────────────────────────────────────────────────────────────────────

def route(state: ReviewState) -> str:
    last = state["messages"][-1]
    # Stop if write_report was just called
    if state.get("report_args"):
        return END
    # Continue if LLM requested tools
    if isinstance(last, AIMessage) and last.tool_calls:
        return "run_tools"
    return END


# ── Build graph ───────────────────────────────────────────────────────────────

def _build_graph():
    g = StateGraph(ReviewState)
    g.add_node("call_llm",  call_llm)
    g.add_node("run_tools", run_tools)
    g.set_entry_point("call_llm")
    g.add_conditional_edges("call_llm", route, {"run_tools": "run_tools", END: END})
    g.add_edge("run_tools", "call_llm")
    return g.compile()

_app = _build_graph()


# ── Public interface ──────────────────────────────────────────────────────────

def review_with_graph(sql: str, verbose: bool = False) -> ReviewReport:
    """Drop-in replacement for agent.review() using LangGraph."""
    from agent import is_read_only  # reuse the guard
    from tools.bq_tools import is_read_only as _ro
    if not _ro(sql):
        raise ValueError("Only SELECT/WITH queries can be reviewed.")

    tables        = extract_tables(sql)
    static        = analyze(sql)
    static_issues = [{"code": i.code, "severity": i.severity,
                      "message": i.message, "suggestion": i.suggestion}
                     for i in static.issues]

    if verbose:
        print(f"[graph] tables={tables}  static_issues={len(static_issues)}")

    result = _app.invoke({
        "messages":      [HumanMessage(content=f"Review this SQL:\n\n{sql}")],
        "sql":           sql,
        "report_args":   {},
        "schemas":       {},
        "_static_issues": static_issues,
        "_tables":        tables,
    })

    report_args = result.get("report_args", {})
    schemas     = result.get("schemas", {})

    dr   = dry_run(sql)
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


def save_graph_diagram(path: str = "docs/architecture_graph.png") -> None:
    """Save the LangGraph state machine diagram as a PNG."""
    png = _app.get_graph().draw_mermaid_png()
    with open(path, "wb") as f:
        f.write(png)
    print(f"Graph diagram saved → {path}")


if __name__ == "__main__":
    save_graph_diagram()
    print("Run  python graph_agent.py  to regenerate the diagram.")
