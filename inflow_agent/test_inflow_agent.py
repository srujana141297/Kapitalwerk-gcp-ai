"""Unit tests for the Inflow Agent. Run with: pytest test_inflow_agent.py -v

No LLM involved -- same reasoning as the Ingestion Agent tests: this is
"""


from datetime import date

from inflow_agent import InflowAgent


def make_stats(count=6, avg3=1000.0, avg12=1000.0, last="2026-06-01"):
    return {
        "transaction_count": count,
        "rolling_3m_avg": avg3,
        "rolling_6m_avg": avg3,
        "rolling_12m_avg": avg12,
        "overall_avg": avg12,
        "last_transaction_date": last,
    }


def txn(cust_id, ref, dt, amount, status="Success"):
    return {
        "customer_reference_id": cust_id,
        "reference_id": ref,
        "transaction_date": dt,
        "amount": amount,
        "status": status,
    }


AGENT = InflowAgent(bucket=None)  # bucket only needed for .run(), not the individual checks


def test_cold_start_counterparty_is_not_flagged_missing():
    stats = {"CUST-1": make_stats(count=1)}  # below MIN_TRANSACTIONS_FOR_BASELINE
    flags = AGENT.flag_missing_payments([], stats, as_of=date(2026, 7, 12))
    assert flags == []


def test_overdue_payment_is_flagged_after_grace_period():
    # monthly cadence, last paid 2026-01-01, three prior monthly txns to
    # establish a ~30 day cadence, and it's now well past a month + grace
    transactions = [
        txn("CUST-1", "r1", "2025-11-01", 1000),
        txn("CUST-1", "r2", "2025-12-01", 1000),
        txn("CUST-1", "r3", "2026-01-01", 1000),
    ]
    stats = {"CUST-1": make_stats(count=3, last="2026-01-01")}
    flags = AGENT.flag_missing_payments(transactions, stats, as_of=date(2026, 3, 1))
    assert len(flags) == 1
    assert flags[0]["customer_reference_id"] == "CUST-1"
    assert flags[0]["days_overdue"] > 0


def test_long_dormant_counterparty_is_not_flagged_missing_forever():
    # Long-lapsed = churn, not "late" -- must stop being flagged past MAX_OVERDUE_DAYS.
    transactions = [
        txn("CUST-1", "r1", "2024-11-01", 1000),
        txn("CUST-1", "r2", "2024-12-01", 1000),
        txn("CUST-1", "r3", "2025-01-01", 1000),
    ]
    stats = {"CUST-1": make_stats(count=3, last="2025-01-01")}
    flags = AGENT.flag_missing_payments(transactions, stats, as_of=date(2026, 7, 16))
    assert flags == []


def test_missing_payment_capped_at_max_overdue_days():
    from inflow_agent import MAX_OVERDUE_DAYS
    transactions = [
        txn("CUST-1", "r1", "2025-11-01", 1000),
        txn("CUST-1", "r2", "2025-12-01", 1000),
        txn("CUST-1", "r3", "2026-01-01", 1000),
    ]
    stats = {"CUST-1": make_stats(count=3, last="2026-01-01")}
    # comfortably overdue but still inside the cap
    flags = AGENT.flag_missing_payments(transactions, stats, as_of=date(2026, 2, 20))
    assert len(flags) == 1
    assert flags[0]["days_overdue"] <= MAX_OVERDUE_DAYS


def test_payment_within_grace_period_is_not_flagged():
    transactions = [
        txn("CUST-1", "r1", "2025-11-01", 1000),
        txn("CUST-1", "r2", "2025-12-01", 1000),
        txn("CUST-1", "r3", "2026-01-01", 1000),
    ]
    stats = {"CUST-1": make_stats(count=3, last="2026-01-01")}
    # expected_by ~2026-01-31, grace period ends ~2026-02-06 -- check a day before that
    flags = AGENT.flag_missing_payments(transactions, stats, as_of=date(2026, 2, 5))
    assert flags == []


def test_high_value_transaction_flagged():
    transactions = [txn("CUST-1", "r1", "2026-06-01", 5000)]  # 5x normal
    stats = {"CUST-1": make_stats(count=6, avg3=1000.0)}
    flags = AGENT.flag_high_value_transactions(transactions, stats)
    assert len(flags) == 1
    assert flags[0]["multiplier"] == 5.0


def test_normal_transaction_not_flagged_high_value():
    transactions = [txn("CUST-1", "r1", "2026-06-01", 1100)]  # within normal range
    stats = {"CUST-1": make_stats(count=6, avg3=1000.0)}
    flags = AGENT.flag_high_value_transactions(transactions, stats)
    assert flags == []


def test_deviation_flagged_when_drift_exceeds_threshold():
    stats = {"CUST-1": make_stats(count=6, avg3=1500.0, avg12=1000.0)}  # +50% drift
    flags = AGENT.flag_deviations(stats)
    assert len(flags) == 1
    assert flags[0]["direction"] == "increase"


def test_deviation_not_flagged_within_threshold():
    stats = {"CUST-1": make_stats(count=6, avg3=1050.0, avg12=1000.0)}  # +5% drift
    flags = AGENT.flag_deviations(stats)
    assert flags == []


def test_explicit_failure_flagged():
    transactions = [txn("CUST-1", "r1", "2026-06-01", 50, status="Failed")]
    flags = AGENT.flag_explicit_failures(transactions)
    assert len(flags) == 1
    assert flags[0]["type"] == "failed_transaction"
