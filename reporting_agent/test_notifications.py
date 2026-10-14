"""Unit tests for notifications.py. Run with: pytest test_notifications.py -v

The real bucket_output may not span a full 4 weeks, so these build synthetic
"""


import json
import os
from datetime import date

import pytest

from notifications import (
    last_4_weeks_notifications,
    recent_weekly_snapshots,
    TOP_N,
)


def _snapshot(root, persona, run_date, *, stats, transactions, missing):
    prefix = os.path.join(root, persona, run_date)
    os.makedirs(prefix, exist_ok=True)
    with open(os.path.join(prefix, "historical_stats.json"), "w") as f:
        json.dump(stats, f)
    with open(os.path.join(prefix, "normalized_transactions.json"), "w") as f:
        json.dump(transactions, f)
    with open(os.path.join(prefix, "inflow_flags.json"), "w") as f:
        json.dump({"missing_payments": missing, "high_value_transactions": [],
                   "deviations": [], "explicit_failures": []}, f)


def _txns(cust, dates, amount):
    return [{"customer_reference_id": cust, "counterparty_name": cust,
             "transaction_date": d, "amount": amount, "status": "Success"} for d in dates]


@pytest.fixture
def bucket(tmp_path):
    root = str(tmp_path / "bucket_output")
    # 4 weekly snapshots relative to today=2026-07-23
    for run_date in ("2026-07-02", "2026-07-09", "2026-07-16", "2026-07-23"):
        stats = {
            "CUST-A": {"transaction_count": 6, "rolling_3m_avg": 5000.0,
                       "overall_avg": 5000.0, "last_transaction_date": "2026-06-01"},
            "CUST-B": {"transaction_count": 6, "rolling_3m_avg": 800.0,
                       "overall_avg": 800.0, "last_transaction_date": run_date},
            "CUST-COLD": {"transaction_count": 1, "rolling_3m_avg": None,
                          "overall_avg": 100.0, "last_transaction_date": run_date},
        }
        txns = (_txns("CUST-A", ["2026-04-01", "2026-05-01", "2026-06-01"], 5000.0)
                + _txns("CUST-B", ["2026-05-01", "2026-06-01", run_date], 800.0))
        # CUST-A is chronically overdue -> a missed_payment each week
        missing = [{"customer_reference_id": "CUST-A", "days_overdue": 20,
                    "expected_by": "2026-06-15", "typical_cadence_days": 30,
                    "grace_period_ends": "2026-06-21", "last_transaction_date": "2026-06-01"}]
        _snapshot(root, "gym-chain", run_date, stats=stats, transactions=txns, missing=missing)
    return root


def test_picks_up_to_four_recent_snapshots(bucket):
    snaps = recent_weekly_snapshots(bucket, "gym-chain", date(2026, 7, 23), weeks=4)
    assert snaps == ["2026-07-23", "2026-07-16", "2026-07-09", "2026-07-02"]


def test_older_snapshots_beyond_window_are_excluded(bucket, tmp_path):
    # a stale snapshot well outside the 4-week window must not be picked
    _snapshot(bucket, "gym-chain", "2026-01-01",
              stats={"CUST-A": {"transaction_count": 6, "rolling_3m_avg": 1.0,
                                "overall_avg": 1.0, "last_transaction_date": "2026-01-01"}},
              transactions=[], missing=[])
    snaps = recent_weekly_snapshots(bucket, "gym-chain", date(2026, 7, 23), weeks=4)
    assert "2026-01-01" not in snaps


def test_missed_outranks_upcoming_and_dedupes_per_counterparty(bucket):
    notes = last_4_weeks_notifications(bucket, "gym-chain", date(2026, 7, 23))
    # CUST-A appears as a missed payment in all 4 snapshots but is deduped to one entry
    a_missed = [n for n in notes if n["customer_reference_id"] == "CUST-A"
                and n["type"] == "missed_payment"]
    assert len(a_missed) == 1
    # missed payment ranks above any upcoming payment
    assert notes[0]["type"] == "missed_payment"
    types_after_first = {n["type"] for n in notes}
    assert "upcoming_payment" in types_after_first  # CUST-B produces an upcoming


def test_cold_start_counterparty_produces_no_upcoming(bucket):
    notes = last_4_weeks_notifications(bucket, "gym-chain", date(2026, 7, 23))
    assert all(n["customer_reference_id"] != "CUST-COLD" for n in notes)


def test_capped_at_top_n(bucket):
    notes = last_4_weeks_notifications(bucket, "gym-chain", date(2026, 7, 23))
    assert len(notes) <= TOP_N


def test_no_snapshots_returns_empty(tmp_path):
    assert last_4_weeks_notifications(str(tmp_path), "gym-chain", date(2026, 7, 23)) == []


def test_many_missed_payments_do_not_crowd_out_all_upcoming(tmp_path):
    # Regression: a naive sort-and-cap silently dropped every upcoming entry
    # once missed count exceeded top_n. The merged top_n must mix both types.
    root = str(tmp_path / "bucket_output")
    run_date = "2026-07-23"
    stats = {"CUST-UP": {"transaction_count": 6, "rolling_3m_avg": 500.0,
                          "overall_avg": 500.0, "last_transaction_date": run_date}}
    txns = _txns("CUST-UP", ["2026-05-23", "2026-06-23", run_date], 500.0)
    # 20 chronically-missed counterparties -- more than TOP_N on their own
    missing = [
        {"customer_reference_id": f"CUST-MISS-{i}", "days_overdue": 20 + i,
         "expected_by": "2026-06-15", "typical_cadence_days": 30,
         "grace_period_ends": "2026-06-21", "last_transaction_date": "2026-06-01"}
        for i in range(20)
    ]
    for i in range(20):
        stats[f"CUST-MISS-{i}"] = {"transaction_count": 6, "rolling_3m_avg": 100.0,
                                     "overall_avg": 100.0, "last_transaction_date": "2026-06-01"}
    _snapshot(root, "gym-chain", run_date, stats=stats, transactions=txns, missing=missing)

    notes = last_4_weeks_notifications(root, "gym-chain", date(2026, 7, 23))
    assert len(notes) == TOP_N
    types = [n["type"] for n in notes]
    assert "upcoming_payment" in types, "upcoming payments must not be entirely crowded out by missed ones"
    assert types.count("missed_payment") >= types.count("upcoming_payment"), \
        "missed still gets first claim on the ceiling slot when both types are abundant"
