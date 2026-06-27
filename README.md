# SQL Review Agent

An agentic AI system that reviews BigQuery SQL queries before you run them — catching
performance issues, estimating cost, and suggesting rewrites.

> **Work in progress.** Building incrementally — check back for updates.

---

## What it does

Paste a BigQuery SQL query and the agent:

1. **Dry-runs it** — gets the estimated bytes scanned (= cost) without executing
2. **Reads your schema** — understands partition keys, clustering, column types
3. **Flags issues** — full table scans, missing partition filters, cartesian joins, `SELECT *`
4. **Suggests a rewrite** — returns improved SQL with explanation
5. **Reports severity** — low / medium / high / critical

```
Input SQL
    │
    ▼
dry_run()          get_schema()
→ bytes scanned    → partition keys, types, row count
    │
    ▼
Issue detection + rewrite suggestion
    │
    ▼
ReviewReport (cost · issues · rewritten SQL · severity)
```

---

## Three trigger modes

- **CLI** — `python agent.py --sql "SELECT * FROM ..."`
- **Web UI** — paste SQL in browser, see review results
- **MCP** — any MCP-compatible client (Claude Code, OpenClaw, Cursor, Zed)

---

## Stack

- **Claude / Gemini** — LLM provider (auto-detected, free Gemini supported)
- **BigQuery** — `dry_run` for cost estimation, schema introspection
- **Flask** — web UI
- **FastMCP** — MCP server
- **pytest** — test suite

---

## Setup

```bash
git clone https://github.com/ARAVINDHRAJA123/sql-review-agent.git
cd sql-review-agent
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

export GCP_PROJECT=your-project
export BQ_LOCATION=asia-south1
export GEMINI_API_KEY=your-key   # free: aistudio.google.com
# or: export ANTHROPIC_API_KEY=your-key

gcloud auth application-default login
```

---

## Project structure

```
sql-review-agent/
├── agent.py          ← agentic review loop (coming Day 3)
├── server.py         ← Flask web UI (coming Day 4)
├── mcp_server.py     ← FastMCP server (coming Day 5)
├── tools/
│   ├── bq_tools.py   ← dry_run, schema, metadata (coming Day 2)
│   └── sql_tools.py  ← SQL parsing helpers (coming Day 2)
├── tests/
├── docs/
└── requirements.txt
```
