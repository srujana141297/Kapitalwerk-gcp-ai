"""Unit tests for exports.py -- the read/export seam a UI/API will call.
Run with: pytest test_exports.py -v. Fixture-driven, no dependency on the
real bucket_output.
"""


import json
import os
from datetime import date

import pytest

import case_actions
import exports


@pytest.fixture(autouse=True)
def _isolated_case_action_store(tmp_path, monkeypatch):
    """Point the case-action store at a throwaway file so these tests never
read or write the real bucket_output/_case_actions store.
"""

    store = str(tmp_path / "case_actions.json")
    monkeypatch.setattr(case_actions, "DEFAULT_CASE_ACTIONS_PATH", store)
    return store


def _write(prefix, name, obj):
    os.makedirs(prefix, exist_ok=True)
    with open(os.path.join(prefix, name), "w") as f:
        json.dump(obj, f)


@pytest.fixture
def bucket(tmp_path):
    root = str(tmp_path / "bucket_output")
    prefix = os.path.join(root, "glucometer-vendor", "2026-07-23")
    txns = [
        {"customer_reference_id": "CUST-A", "counterparty_name": "Charite", "amount": 10000.0,
         "status": "Success", "transaction_date": "2026-06-20"},
        {"customer_reference_id": "CUST-A", "counterparty_name": "Charite", "amount": 5000.0,
         "status": "Success", "transaction_date": "2026-07-20"},
        {"customer_reference_id": "CUST-B", "counterparty_name": "Asklepios", "amount": 2000.0,
         "status": "Success", "transaction_date": "2026-07-21"},
        {"customer_reference_id": "CUST-B", "counterparty_name": "Asklepios", "amount": 9999.0,
         "status": "Failed", "transaction_date": "2026-07-22"},
    ]
    _write(prefix, "normalized_transactions.json", txns)
    _write(prefix, "historical_stats.json", {
        "CUST-A": {"transaction_count": 6, "rolling_3m_avg": 7500.0, "overall_avg": 7500.0,
                   "last_transaction_date": "2026-07-20"}})
    _write(prefix, "inflow_flags.json", {"missing_payments": [], "high_value_transactions": [
        {"customer_reference_id": "CUST-A", "reference_id": "E2E-1", "transaction_date": "2026-07-23",
         "amount": 32000.0, "baseline_3m_avg": 7500.0, "multiplier": 4.27}],
        "deviations": [], "explicit_failures": []})
    _write(prefix, "top_risk_report.json", {"customer_id": "glucometer-vendor",
        "run_date": "2026-07-23", "run_risk_tier": "high", "count": 1,
        "top_transactions": [{"case_id": "AML-E2E1", "customer_reference_id": "CUST-A",
            "counterparty": "Charite", "amount": 32000.0, "risk_score": 4.27,
            "risk_tier": "high", "reason": "4.27x normal", "suggested_action": "Escalate."}]})
    _write(prefix, "marketing_heatmap.json", {"heatmap": [
        {"postal_code": "10115", "city": "Berlin", "transaction_count": 3, "total_value": 15000.0,
         "label": "opportunity", "narrative": "Strong Berlin activity."}]})

    audit = str(tmp_path / "audit.jsonl")
    with open(audit, "w") as f:
        for rec in [{"agent": "risk_agent", "stop_reason": "completed"},
                    {"agent": "aml_agent", "stop_reason": "completed"},
                    {"agent": "aml_agent", "stop_reason": "cache_hit"},
                    {"agent": "risk_agent", "stop_reason": "pii_detected_blocked"}]:
            f.write(json.dumps(rec) + "\n")
    return root, audit


def test_top_counterparties_ranks_by_value_and_excludes_failed(bucket):
    root, _ = bucket
    rows = exports.top_counterparties(root, "glucometer-vendor")
    assert rows[0]["customer_reference_id"] == "CUST-A"
    assert rows[0]["total_value"] == 15000.0
    b = next(r for r in rows if r["customer_reference_id"] == "CUST-B")
    assert b["total_value"] == 2000.0  # the Failed 9999 row excluded


def test_cases_awaiting_review_reads_persisted_report(bucket):
    root, _ = bucket
    cases = exports.cases_awaiting_review(root, "glucometer-vendor")
    assert cases[0]["case_id"] == "AML-E2E1"
    assert cases[0]["status"] == "awaiting_review"


def test_cases_awaiting_review_rebuilds_when_no_report(bucket, tmp_path):
    root, _ = bucket
    # remove the persisted report; function must rebuild from inflow_flags
    os.remove(os.path.join(root, "glucometer-vendor", "2026-07-23", "top_risk_report.json"))
    cases = exports.cases_awaiting_review(root, "glucometer-vendor")
    assert len(cases) == 1
    assert cases[0]["customer_reference_id"] == "CUST-A"


def test_cases_awaiting_review_merges_escalated_status_and_narrative(bucket):
    root, _ = bucket
    # escalate the one persisted case and stash a real AML narrative on it
    case_actions.save_case_action(
        "glucometer-vendor", "AML-E2E1", "escalated",
        escalation={"ran": True, "risk_tier": "high",
                    "risk_reasoning": "Signals warrant review.",
                    "aml_case_narrative": "SAR: transaction 4.27x baseline, source of funds unverified.",
                    "aml_trace_id": "trc-abc123"},
    )
    cases = exports.cases_awaiting_review(root, "glucometer-vendor")
    c = cases[0]
    assert c["status"] == "escalated"
    assert c["escalation"]["aml_case_narrative"].startswith("SAR:")
    assert c["reviewed_at"]


def test_cases_awaiting_review_merges_acknowledged_status(bucket):
    root, _ = bucket
    case_actions.save_case_action("glucometer-vendor", "AML-E2E1", "acknowledged")
    cases = exports.cases_awaiting_review(root, "glucometer-vendor")
    assert cases[0]["status"] == "acknowledged"
    # acknowledge is simple: no escalation payload
    assert cases[0].get("escalation") is None


def test_marketing_heatmap_passthrough(bucket):
    root, _ = bucket
    hm = exports.marketing_heatmap(root, "glucometer-vendor")
    assert hm[0]["postal_code"] == "10115"


def test_governance_audit_summary_counts_and_pii_blocks(bucket):
    _, audit = bucket
    summ = exports.governance_audit_summary(audit)
    assert summ["total_events"] == 4
    assert summ["by_agent"]["aml_agent"] == 2
    assert summ["pii_blocks"] == 1
    assert summ["llm_calls_completed"] == 2
    assert summ["cache_hits"] == 1


def test_dashboard_bundles_everything_and_is_json_serializable(bucket):
    root, audit = bucket
    dash = exports.dashboard(root, "glucometer-vendor", today="2026-07-23", audit_path=audit)
    assert dash["run_date"] == "2026-07-23"
    for key in ("top_counterparties", "cases_awaiting_review", "recent_notifications",
                "marketing_heatmap", "governance_audit_summary"):
        assert key in dash
    json.dumps(dash)  # must be JSON-serializable


def test_missing_persona_returns_empty(bucket):
    root, _ = bucket
    assert exports.top_counterparties(root, "does-not-exist") == []
    assert exports.marketing_heatmap(root, "does-not-exist") == []
    assert exports.cases_awaiting_review(root, "does-not-exist") == []
