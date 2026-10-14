"""Unit tests for top_risk.py. Run with: pytest test_top_risk.py -v
Fixture data only -- no dependency on the real bucket_output.
"""


import json
import os

from top_risk import build_top_risk_report, write_top_risk_report, TOP_N


FLAGS = {
    "high_value_transactions": [
        {"customer_reference_id": "CUST-HOSP-1001", "reference_id": "E2E-DE-60204",
         "transaction_date": "2026-07-23", "amount": 32500.0,
         "baseline_3m_avg": 9500.0, "multiplier": 3.42},
        {"customer_reference_id": "CUST-HOSP-1006", "reference_id": "E2E-DE-60201",
         "transaction_date": "2026-07-22", "amount": 9800.0,
         "baseline_3m_avg": 5000.0, "multiplier": 1.96},
    ],
    "deviations": [
        {"customer_reference_id": "CUST-HOSP-1003", "rolling_3m_avg": 19392.55,
         "rolling_12m_avg": 15571.82, "drift_pct": 23.6, "direction": "increase"},
    ],
}

TXNS = [
    {"customer_reference_id": "CUST-HOSP-1001", "counterparty_name": "Charite Berlin"},
    {"customer_reference_id": "CUST-HOSP-1006", "counterparty_name": "Asklepios Hamburg"},
]


def test_report_is_sorted_by_risk_desc_and_capped():
    report = build_top_risk_report(FLAGS, TXNS)
    scores = [e["risk_score"] for e in report]
    assert scores == sorted(scores, reverse=True)
    assert len(report) <= TOP_N
    # highest-multiplier transaction ranks first
    assert report[0]["customer_reference_id"] == "CUST-HOSP-1001"


def test_entry_has_all_required_fields_and_resolves_counterparty_name():
    report = build_top_risk_report(FLAGS, TXNS)
    top = report[0]
    for field in ("case_id", "customer_reference_id", "counterparty", "amount",
                  "risk_score", "risk_tier", "reason", "suggested_action"):
        assert field in top
    assert top["counterparty"] == "Charite Berlin"
    assert top["risk_tier"] == "high"  # 3.42x >= 2.5


def test_deviation_is_included_and_named_without_a_transaction_name():
    report = build_top_risk_report(FLAGS, TXNS)
    dev = next(e for e in report if e["customer_reference_id"] == "CUST-HOSP-1003")
    # no counterparty name available for this membership-style id -> falls back
    assert dev["counterparty"] == "CUST-HOSP-1003"
    assert "drifted" in dev["reason"]


def test_write_persists_artifact(tmp_path):
    root = str(tmp_path / "bucket_output")
    prefix = os.path.join(root, "glucometer-vendor", "2026-07-23")
    os.makedirs(prefix)
    with open(os.path.join(prefix, "normalized_transactions.json"), "w") as f:
        json.dump(TXNS, f)

    path = write_top_risk_report(root, "glucometer-vendor", "2026-07-23", FLAGS,
                                 run_risk_tier="high")
    assert path.endswith("top_risk_report.json")
    with open(path) as f:
        payload = json.load(f)
    assert payload["run_risk_tier"] == "high"
    assert payload["count"] == len(payload["top_transactions"]) <= TOP_N
    assert payload["top_transactions"][0]["risk_score"] >= payload["top_transactions"][-1]["risk_score"]


def test_empty_flags_produce_empty_report():
    assert build_top_risk_report({"high_value_transactions": [], "deviations": []}, []) == []


# A counterparty tripping both a deviation and a high-value flag -- each must
# get its own distinct case_id (real data collided under the old scheme).
COLLISION_FLAGS = {
    "high_value_transactions": [
        {"customer_reference_id": "MEM-001001", "reference_id": "SEPA-DD-MND001001",
         "transaction_date": "2026-07-23", "amount": 149.99,
         "baseline_3m_avg": 51.24, "multiplier": 2.93},
    ],
    "deviations": [
        {"customer_reference_id": "MEM-001001", "rolling_3m_avg": 51.24,
         "rolling_12m_avg": 36.43, "drift_pct": 40.7, "direction": "increase"},
    ],
}


def test_case_ids_are_unique_when_one_counterparty_has_both_flag_types():
    report = build_top_risk_report(COLLISION_FLAGS, [])
    assert len(report) == 2
    case_ids = [e["case_id"] for e in report]
    # the whole point of the fix: two different flags, two different ids
    assert len(set(case_ids)) == 2, f"case_ids collided: {case_ids}"
    # ids are still readable/deterministic and carry the counterparty suffix
    for cid in case_ids:
        assert cid.startswith("AML-1001-")
    # and each entry declares which inflow_flags bucket it came from
    flag_types = {e["flag_type"] for e in report}
    assert flag_types == {"high_value_transaction", "deviation"}


def test_case_id_is_deterministic_across_rebuilds():
    a = build_top_risk_report(COLLISION_FLAGS, [])
    b = build_top_risk_report(COLLISION_FLAGS, [])
    assert [e["case_id"] for e in a] == [e["case_id"] for e in b]
