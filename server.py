"""
Flask web UI for SQL Review Agent.

GET  /          — review form
POST /review    — submit SQL, show result
POST /api/review — JSON API endpoint (returns ReviewReport as JSON)
"""
from __future__ import annotations

import json
import os

from flask import Flask, jsonify, render_template, request

from agent import ReviewReport, review

app = Flask(__name__)


def _report_to_dict(r: ReviewReport) -> dict:
    return {
        "severity":           r.severity,
        "bytes_scanned_gb":   round(r.bytes_scanned_gb, 4),
        "estimated_cost_usd": round(r.estimated_cost_usd, 8),
        "issues":             r.issues,
        "rewritten_sql":      r.rewritten_sql,
        "explanation":        r.explanation,
        "plain_report":       r.plain_report,
    }


@app.get("/")
def index():
    return render_template("index.html", result=None, sql="", error=None)


@app.post("/review")
def review_form():
    sql = request.form.get("sql", "").strip()
    if not sql:
        return render_template("index.html", result=None, sql="", error="SQL cannot be empty.")
    try:
        r = review(sql, verbose=False)
        return render_template("index.html", result=_report_to_dict(r), sql=sql, error=None)
    except ValueError as e:
        return render_template("index.html", result=None, sql=sql, error=str(e))
    except Exception as e:
        return render_template("index.html", result=None, sql=sql, error=f"Agent error: {e}")


@app.post("/api/review")
def api_review():
    body = request.get_json(force=True, silent=True) or {}
    sql  = (body.get("sql") or "").strip()
    if not sql:
        return jsonify({"error": "sql field is required"}), 400
    try:
        r = review(sql, verbose=False)
        return jsonify(_report_to_dict(r))
    except ValueError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=True, port=port)
