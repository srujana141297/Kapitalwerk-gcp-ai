"""Minimal read/export layer over bucket_output.

This is the seam a future UI or HTTP API will sit on top of. It deliberately
"""


from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import date

import case_actions
import notifications
import top_risk

# governance's audit store lives one folder over, needed only for audit-summary.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "governance"))

DEFAULT_AUDIT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "bucket_output", "_audit", "aml_audit.jsonl"
)


def _is_gcs_path(path: str) -> bool:
    return isinstance(path, str) and path.startswith("gs://")


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"Malformed GCS URI: {uri!r}")
    without_scheme = uri[len("gs://"):]
    if "/" not in without_scheme:
        raise ValueError(f"Malformed GCS URI: {uri!r}")
    bucket, blob = without_scheme.split("/", 1)
    return bucket, blob


_gcs_client_singleton = None


def _gcs_client():
    """Reused across calls -- constructing storage.Client() repeatedly redoes
ADC credential discovery and was the dominant cost of dashboard() requests.
"""

    global _gcs_client_singleton
    if _gcs_client_singleton is None:
        from google.cloud import storage
        _gcs_client_singleton = storage.Client()
    return _gcs_client_singleton


def _read_gcs_text(path: str) -> str:
    bucket_name, blob_name = _parse_gcs_uri(path)
    client = _gcs_client()
    blob = client.bucket(bucket_name).blob(blob_name)
    return blob.download_as_text()


def _join(bucket_root: str, *parts: str) -> str:
    if _is_gcs_path(bucket_root):
        return "/".join([bucket_root.rstrip("/")] + [part.strip("/") for part in parts])
    return os.path.join(bucket_root, *parts)


def _exists(path: str) -> bool:
    if _is_gcs_path(path):
        bucket_name, blob_name = _parse_gcs_uri(path)
        client = _gcs_client()
        return client.bucket(bucket_name).blob(blob_name).exists()
    return os.path.exists(path)


def _is_dir(path: str) -> bool:
    if _is_gcs_path(path):
        bucket_name, prefix = _parse_gcs_uri(path)
        client = _gcs_client()
        bucket = client.bucket(bucket_name)
        prefix = prefix.rstrip("/") + "/"
        return any(True for _ in bucket.list_blobs(prefix=prefix, max_results=1))
    return os.path.isdir(path)


def _load(path):
    if _is_gcs_path(path):
        return json.loads(_read_gcs_text(path))
    with open(path) as f:
        return json.load(f)


def _join(bucket_root: str, *parts: str) -> str:
    if _is_gcs_path(bucket_root):
        return "/".join([bucket_root.rstrip("/")] + [part.strip("/") for part in parts])
    return os.path.join(bucket_root, *parts)


def is_complete_snapshot(bucket_root: str, persona: str, run_date: str) -> bool:
    """True once this date folder's pipeline run has actually finished writing.
"""

    if _exists(_join(bucket_root, persona, run_date, "marketing_heatmap.json")):
        return True
    if (_exists(_join(bucket_root, persona, run_date, "reminders_sent_monday_upcoming.json"))
            and not _exists(_join(bucket_root, persona, run_date, "inflow_flags.json"))):
        return True
    return False


_LATEST_RUN_DATE_CACHE: dict[tuple[str, str], tuple[float, str | None]] = {}
_LATEST_RUN_DATE_TTL_SECONDS = 30
# Every /{persona}/<endpoint> request independently resolves "today's run
# date" via this function. On GCS that means a list_blobs() call plus one or
# more blob.exists() probes -- real network round trips.

def latest_run_date(bucket_root: str, persona: str) -> str | None:
    """Most recent COMPLETE snapshot date folder (skips in-progress runs).

Cached for _LATEST_RUN_DATE_TTL_SECONDS per (bucket_root, persona) to avoid
repeating the underlying GCS list_blobs()/exists() round trips on every one
of the several concurrent per-tenant API calls the UI fires on each load.
"""

    cache_key = (bucket_root, persona)
    cached = _LATEST_RUN_DATE_CACHE.get(cache_key)
    now = time.monotonic()
    if cached is not None and (now - cached[0]) < _LATEST_RUN_DATE_TTL_SECONDS:
        return cached[1]

    persona_dir = os.path.join(bucket_root, persona)
    if _is_gcs_path(persona_dir):
        bucket_name, prefix = _parse_gcs_uri(persona_dir)
        client = _gcs_client()
        bucket = client.bucket(bucket_name)
        root_prefix = prefix.rstrip("/") + "/"
        # delimiter="/" makes GCS return only immediate subfolder names instead
        iterator = bucket.list_blobs(prefix=root_prefix, delimiter="/")
        list(iterator)  # must be consumed for .prefixes to populate
        dates = {
            p[len(root_prefix):].rstrip("/")
            for p in iterator.prefixes
            if not p[len(root_prefix):].rstrip("/").startswith("_")
        }
    else:
        if not os.path.isdir(persona_dir):
            _LATEST_RUN_DATE_CACHE[cache_key] = (now, None)
            return None
        dates = {d for d in os.listdir(persona_dir)
                 if os.path.isdir(os.path.join(persona_dir, d)) and not d.startswith("_")}

    result = None
    for d in sorted(dates, reverse=True):
        if is_complete_snapshot(bucket_root, persona, d):
            result = d
            break
    _LATEST_RUN_DATE_CACHE[cache_key] = (now, result)
    return result


def _resolve(bucket_root, persona, run_date):
    return run_date or latest_run_date(bucket_root, persona)


def top_counterparties(bucket_root: str, persona: str, run_date: str | None = None,
                       limit: int = 10) -> list[dict]:
    """Top counterparties by total successful transaction value in a snapshot.
"""
    run_date = _resolve(bucket_root, persona, run_date)
    if not run_date:
        return []
    path = _join(bucket_root, persona, run_date, "normalized_transactions.json")
    if _is_gcs_path(path):
        # We can only test existence by attempting to read or metadata lookup.
        try:
            _read_gcs_text(path)
        except Exception:
            return []
    else:
        if not os.path.exists(path):
            return []
    agg = defaultdict(lambda: {"transaction_count": 0, "total_value": 0.0, "counterparty_name": None})
    for t in _load(path):
        if t.get("status") not in (None, "Success"):
            continue
        cust = t.get("customer_reference_id")
        if not cust:
            continue
        a = agg[cust]
        a["transaction_count"] += 1
        a["total_value"] += float(t.get("amount") or 0)
        if not a["counterparty_name"] and t.get("counterparty_name"):
            a["counterparty_name"] = t["counterparty_name"]
    rows = [{"customer_reference_id": c, "counterparty_name": v["counterparty_name"] or c,
             "transaction_count": v["transaction_count"], "total_value": round(v["total_value"], 2)}
            for c, v in agg.items()]
    rows.sort(key=lambda r: r["total_value"], reverse=True)
    return rows[:limit]


def cases_awaiting_review(bucket_root: str, persona: str, run_date: str | None = None,
                          limit: int = 10) -> list[dict]:
    """Flagged cases awaiting analyst review, highest-risk first. Reads the
persisted top_risk_report.json when present; otherwise rebuilds it from the
"""

    run_date = _resolve(bucket_root, persona, run_date)
    if not run_date:
        return []
    prefix = _join(bucket_root, persona, run_date)
    report_path = _join(bucket_root, persona, run_date, "top_risk_report.json")
    if _exists(report_path):
        cases = _load(report_path).get("top_transactions", [])
    else:
        flags_path = _join(bucket_root, persona, run_date, "inflow_flags.json")
        norm_path = _join(bucket_root, persona, run_date, "normalized_transactions.json")
        if not _exists(flags_path):
            return []
        transactions = _load(norm_path) if _exists(norm_path) else []
        cases = top_risk.build_top_risk_report(_load(flags_path), transactions)

    merged = []
    for c in cases[:limit]:
        action = case_actions.load_case_action(persona, c.get("case_id"))
        if action and not case_actions.is_action_expired(action):
            merged.append({**c, "status": action["status"],
                           "reviewed_at": action.get("reviewed_at"),
                           "escalation": action.get("escalation")})
        else:
            merged.append({**c, "status": "awaiting_review"})
    return merged


def recent_notifications(bucket_root: str, persona: str, today: date | str,
                         limit: int = 10) -> list[dict]:
    """Last-4-weeks top-10 upcoming/missed notifications -- reuses notifications.py.
"""
    if isinstance(today, str):
        today = date.fromisoformat(today)
    return notifications.last_4_weeks_notifications(bucket_root, persona, today, top_n=limit)


_firestore_audit_store_singleton = None


def _build_audit_store(audit_path: str):
    """GOVERNANCE_STORE-aware, mirroring orchestrator/nodes.py's and
api/main.py's _build_governance_stores() -- firestore in the cloud (shared
across the agent/api containers' separate filesystems), local file for dev.
"""

    from audit_store import LocalFileAuditStore

    if os.environ.get("GOVERNANCE_STORE", "memory").lower() == "firestore":
        global _firestore_audit_store_singleton
        if _firestore_audit_store_singleton is None:
            try:
                from audit_store import FirestoreAuditStore
                _firestore_audit_store_singleton = FirestoreAuditStore()
            except Exception as e:
                # Keep the governance audit endpoints serving (from the local
                # JSONL log) instead of 500ing when Firestore is unavailable.
                import sys
                print(f"[exports] Firestore audit store unavailable -- serving "
                      f"the local JSONL audit log instead. Cause: {e}", file=sys.stderr)
                return LocalFileAuditStore(audit_path)
        return _firestore_audit_store_singleton
    return LocalFileAuditStore(audit_path)


def governance_audit_summary(audit_path: str | None = None) -> dict:
    """Rollup of the immutable governance audit trail: total events, a
breakdown by agent and by stop_reason, and an explicit count of fail-closed
"""

    records = _build_audit_store(audit_path or DEFAULT_AUDIT_PATH).all()

    by_agent: dict[str, int] = defaultdict(int)
    by_stop_reason: dict[str, int] = defaultdict(int)
    for r in records:
        by_agent[r.get("agent", "unknown")] += 1
        by_stop_reason[r.get("stop_reason", "unknown")] += 1
    return {
        "total_events": len(records),
        "by_agent": dict(by_agent),
        "by_stop_reason": dict(by_stop_reason),
        "pii_blocks": by_stop_reason.get("pii_detected_blocked", 0),
        "llm_calls_completed": by_stop_reason.get("completed", 0),
        "cache_hits": by_stop_reason.get("cache_hit", 0),
    }


def governance_audit_log(audit_path: str | None = None, limit: int = 50) -> list[dict]:
    """Individual audit events, newest first -- the per-row detail behind
governance_audit_summary()'s rollup, for the UI's audit-log table.
"""

    records = _build_audit_store(audit_path or DEFAULT_AUDIT_PATH).all()
    records.sort(key=lambda r: r.get("timestamp") or 0, reverse=True)
    return [
        {
            "trace_id": r.get("trace_id"),
            "agent": r.get("agent"),
            "model": r.get("model"),
            "input_tokens": r.get("input_tokens", 0),
            "output_tokens": r.get("output_tokens", 0),
            "latency_ms": r.get("latency_ms", 0),
            "stop_reason": r.get("stop_reason"),
        }
        for r in records[:limit]
    ]


def marketing_heatmap(bucket_root: str, persona: str, run_date: str | None = None) -> list[dict]:
    """The marketing agent's per-postal-code heatmap for a snapshot (reuses its
persisted marketing_heatmap.json).
"""

    run_date = _resolve(bucket_root, persona, run_date)
    if not run_date:
        return []
    path = _join(bucket_root, persona, run_date, "marketing_heatmap.json")
    if not _exists(path):
        return []
    return _load(path).get("heatmap", [])


def dashboard(bucket_root: str, persona: str, today: date | str | None = None,
              audit_path: str | None = None) -> dict:
    """Everything a per-persona overview screen needs, in one call.
"""
    run_date = latest_run_date(bucket_root, persona)
    today = today or (run_date or date.today().isoformat())
    return {
        "persona": persona,
        "run_date": run_date,
        "top_counterparties": top_counterparties(bucket_root, persona, run_date),
        "cases_awaiting_review": cases_awaiting_review(bucket_root, persona, run_date),
        "recent_notifications": recent_notifications(bucket_root, persona, today),
        "marketing_heatmap": marketing_heatmap(bucket_root, persona, run_date),
        "governance_audit_summary": governance_audit_summary(audit_path),
    }