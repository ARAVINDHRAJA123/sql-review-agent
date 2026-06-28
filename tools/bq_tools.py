"""
BigQuery tools for the SQL Review Agent.

Three core capabilities:
  dry_run()           — estimate bytes scanned + cost before execution
  get_table_metadata()— schema, partition key, clustering, row count, size
  get_dataset_tables()— list all tables in a dataset

All queries are read-only. No data is modified or executed.
"""
from __future__ import annotations

import os
import re
from typing import Any

from google.cloud import bigquery

PROJECT  = os.environ.get("GCP_PROJECT", "")
LOCATION = os.environ.get("BQ_LOCATION", "US")

# BigQuery on-demand pricing: $6.25 per TB scanned
PRICE_PER_TB = 6.25

# ── Safety wall ────────────────────────────────────────────────────────────
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|create|alter|truncate|grant|revoke|call)\b",
    re.IGNORECASE,
)
_STARTS_OK = re.compile(r"^\s*(with|select)\b", re.IGNORECASE | re.DOTALL)


def is_read_only(sql: str) -> bool:
    """Return True only if the SQL is a safe read-only SELECT/WITH statement."""
    stripped = sql.strip().rstrip(";")
    return bool(_STARTS_OK.match(stripped)) and not _FORBIDDEN.search(stripped)


def _client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT, location=LOCATION)


# ── Core tools ─────────────────────────────────────────────────────────────

def dry_run(sql: str) -> dict[str, Any]:
    """
    Dry-run a SQL query and return cost estimate without executing it.

    Returns:
        bytes_processed: estimated bytes scanned
        bytes_processed_gb: same in GB
        estimated_cost_usd: estimated cost at on-demand pricing ($6.25/TB)
        is_cached: whether the result would be served from cache (free)
        statement_type: SELECT / WITH / etc.
    """
    if not is_read_only(sql):
        return {"error": "Rejected: only SELECT/WITH queries are allowed."}
    if not PROJECT:
        return {"error": "GCP_PROJECT is not set."}

    try:
        client = _client()
        job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        job = client.query(sql, job_config=job_config)

        bytes_processed = job.total_bytes_processed or 0
        gb = bytes_processed / (1024 ** 3)
        tb = bytes_processed / (1024 ** 4)
        cost = round(tb * PRICE_PER_TB, 6)

        return {
            "bytes_processed":    bytes_processed,
            "bytes_processed_gb": round(gb, 4),
            "estimated_cost_usd": cost,
            "is_cached":          job.cache_hit or False,
            "statement_type":     job.statement_type or "SELECT",
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def get_table_metadata(project: str, dataset: str, table: str) -> dict[str, Any]:
    """
    Return full metadata for a BigQuery table including schema, partitioning,
    clustering, row count, and size.

    Returns:
        table_id, schema (columns + types), partition_field, partition_type,
        clustering_fields, num_rows, size_gb, description
    """
    if not PROJECT:
        return {"error": "GCP_PROJECT is not set."}

    try:
        client = _client()
        tbl = client.get_table(f"{project}.{dataset}.{table}")

        columns = [
            {
                "name":        f.name,
                "type":        f.field_type,
                "mode":        f.mode,
                "description": f.description or "",
            }
            for f in tbl.schema
        ]

        # Partition info
        partition_field = None
        partition_type  = None
        if tbl.time_partitioning:
            partition_field = tbl.time_partitioning.field or "_PARTITIONTIME"
            partition_type  = tbl.time_partitioning.type_
        elif tbl.range_partitioning:
            partition_field = tbl.range_partitioning.field
            partition_type  = "RANGE"

        return {
            "table_id":          f"{project}.{dataset}.{table}",
            "schema":            columns,
            "column_count":      len(columns),
            "partition_field":   partition_field,
            "partition_type":    partition_type,
            "clustering_fields": tbl.clustering_fields or [],
            "num_rows":          tbl.num_rows,
            "size_gb":           round((tbl.num_bytes or 0) / (1024 ** 3), 4),
            "description":       tbl.description or "",
            "table_type":        tbl.table_type,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def get_dataset_tables(project: str, dataset: str) -> dict[str, Any]:
    """
    List all tables in a BigQuery dataset with basic metadata.
    Useful for schema discovery before writing a query.
    """
    if not PROJECT:
        return {"error": "GCP_PROJECT is not set."}

    try:
        client = _client()
        tables = list(client.list_tables(f"{project}.{dataset}"))
        return {
            "dataset":     f"{project}.{dataset}",
            "table_count": len(tables),
            "tables": [
                {"table_id": t.table_id, "table_type": t.table_type}
                for t in tables
            ],
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
