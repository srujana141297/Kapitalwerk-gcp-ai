"""Weekly cycle logic for the Inflow Agent.

Splits the Inflow Agent's job into the two moments a business customer
"""


from __future__ import annotations

import csv
import json
from collections import defaultdict

MIN_TRANSACTIONS_FOR_BASELINE = 3


def load_baseline(historical_stats_path: str) -> dict:
    with open(historical_stats_path) as f:
        return json.load(f)


def load_current_period(csv_path: str) -> list:
    with open(csv_path, newline="") as f:
        return list(csv.DictReader(f))


def expected_this_week(baseline: dict) -> dict:
    """MONDAY. Every counterparty with enough history to trust is expected to
pay this week, at their rolling-3-month average amount. Cold-start
"""

    expected = {}
    for cust_id, s in baseline.items():
        if s["transaction_count"] < MIN_TRANSACTIONS_FOR_BASELINE:
            continue
        expected[cust_id] = s["rolling_3m_avg"] or s["overall_avg"]
    return expected


def friday_report(expected: dict, week_rows: list, cumulative_so_far: float, name_map: dict | None = None) -> dict:
    """FRIDAY. Compare this week's actual transactions against Monday's
expectation for the same week. `name_map` (customer_reference_id ->
"""

    actual_by_customer = defaultdict(float)
    for r in week_rows:
        actual_by_customer[r["Customer_Reference_ID"]] += float(r["Amount"])

    name_map = name_map or {}
    misses = []
    for cust_id, expected_amount in expected.items():
        if actual_by_customer.get(cust_id, 0.0) == 0.0:
            misses.append({
                "customer_reference_id": cust_id,
                "hospital_name": name_map.get(cust_id, "(unknown)"),
                "expected_amount": expected_amount,
            })

    total_expected = sum(expected.values())
    total_actual = sum(actual_by_customer.values())
    deviation_pct = round((total_actual - total_expected) / total_expected * 100, 1) if total_expected else None
    new_cumulative = cumulative_so_far + total_actual

    return {
        "total_expected": round(total_expected, 2),
        "total_actual": round(total_actual, 2),
        "inflow_deviation_pct": deviation_pct,
        "missed_payments": misses,
        "missed_count": len(misses),
        "cumulative_amount_period_to_date": round(new_cumulative, 2),
    }, new_cumulative


def run_all_weeks(baseline_path: str, current_period_csv: str) -> list:
    baseline = load_baseline(baseline_path)
    all_rows = load_current_period(current_period_csv)

    expected = expected_this_week(baseline)  # same Monday expectation every week -- baseline doesn't change mid-period
    name_map = {r["Customer_Reference_ID"]: r["Hospital_Name"] for r in all_rows}

    rows_by_week = defaultdict(list)
    for r in all_rows:
        rows_by_week[r["Week"]].append(r)

    cumulative = 0.0
    weekly_results = []
    for week_id in sorted(rows_by_week.keys()):
        report, cumulative = friday_report(expected, rows_by_week[week_id], cumulative, name_map)
        weekly_results.append({"week": week_id, **report})
    return weekly_results
