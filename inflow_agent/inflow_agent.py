"""Inflow Agent.

Reads what the Ingestion Agent already produced (normalized_transactions.json
"""


from __future__ import annotations

import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# grace period after a counterparty's expected payment date before it's
# flagged as missing -- "day 1 to day 6" from the architecture description
GRACE_PERIOD_DAYS = 6

# Beyond this many days past the grace-period deadline, a counterparty reads
# as churned/dormant, not a collections concern -- stop flagging it forever.
MAX_OVERDUE_DAYS = 45

# a transaction more than this many times the counterparty's rolling 3m
# average is flagged as unusual-high-value
HIGH_VALUE_MULTIPLIER = 1.8

# recent (3m) vs long-term (12m) average drift beyond this % is flagged
# as a deviation worth Risk Agent's attention
DEVIATION_THRESHOLD_PCT = 20.0

MIN_TRANSACTIONS_FOR_BASELINE = 3  # cold-start guard -- don't flag brand-new counterparties


@dataclass
class InflowFlags:
    missing_payments: list = field(default_factory=list)
    high_value_transactions: list = field(default_factory=list)
    deviations: list = field(default_factory=list)
    explicit_failures: list = field(default_factory=list)


class InflowAgent:
    def __init__(self, bucket):
        self.bucket = bucket

    # ---- loading ingestion output -------------------------------------------

    def load(self, bucket, customer_id: str, run_date: str):
        prefix = f"{customer_id}/{run_date}"
        transactions = bucket.read_json(f"{prefix}/normalized_transactions.json")
        stats = bucket.read_json(f"{prefix}/historical_stats.json")
        return transactions, stats

    # ---- check 1: missing payments / inflow mismatch ------------------------

    def flag_missing_payments(self, transactions: list, stats: dict, as_of: date) -> list:
        flags = []
        by_customer = defaultdict(list)
        for t in transactions:
            if t["status"] == "Success":
                by_customer[t["customer_reference_id"]].append(t["transaction_date"])

        for cust_id, s in stats.items():
            if s["transaction_count"] < MIN_TRANSACTIONS_FOR_BASELINE:
                continue  # cold start -- no reliable cadence to compare against yet

            dates_sorted = sorted(by_customer[cust_id])
            gaps = [
                (datetime.strptime(dates_sorted[i + 1], "%Y-%m-%d")
                 - datetime.strptime(dates_sorted[i], "%Y-%m-%d")).days
                for i in range(len(dates_sorted) - 1)
            ]
            typical_cadence = round(statistics.median(gaps)) if gaps else 30

            last_txn = datetime.strptime(s["last_transaction_date"], "%Y-%m-%d").date()
            expected_by = last_txn + timedelta(days=typical_cadence)
            deadline = expected_by + timedelta(days=GRACE_PERIOD_DAYS)

            if as_of > deadline:
                days_overdue = (as_of - deadline).days
                if days_overdue > MAX_OVERDUE_DAYS:
                    # Past MAX_OVERDUE_DAYS -- churned/dormant, not a miss.
                    continue
                flags.append({
                    "customer_reference_id": cust_id,
                    "type": "missing_payment",
                    "last_transaction_date": s["last_transaction_date"],
                    "typical_cadence_days": typical_cadence,
                    "expected_by": expected_by.isoformat(),
                    "grace_period_ends": deadline.isoformat(),
                    "days_overdue": days_overdue,
                })
        return flags

    # ---- check 2: unusual high-value transactions ---------------------------

    def flag_high_value_transactions(self, transactions: list, stats: dict) -> list:
        flags = []
        for t in transactions:
            if t["status"] != "Success":
                continue
            s = stats.get(t["customer_reference_id"])
            if not s or s["transaction_count"] < MIN_TRANSACTIONS_FOR_BASELINE:
                continue
            baseline = s["rolling_3m_avg"] or s["overall_avg"]
            if baseline and t["amount"] > baseline * HIGH_VALUE_MULTIPLIER:
                flags.append({
                    "customer_reference_id": t["customer_reference_id"],
                    "type": "high_value_transaction",
                    "reference_id": t["reference_id"],
                    "transaction_date": t["transaction_date"],
                    "amount": t["amount"],
                    "baseline_3m_avg": baseline,
                    "multiplier": round(t["amount"] / baseline, 2),
                })
        return flags

    # ---- check 3: rolling-average deviation ---------------------------------

    def flag_deviations(self, stats: dict) -> list:
        flags = []
        for cust_id, s in stats.items():
            if s["transaction_count"] < MIN_TRANSACTIONS_FOR_BASELINE:
                continue
            recent, baseline = s["rolling_3m_avg"], s["rolling_12m_avg"]
            if not recent or not baseline:
                continue
            drift_pct = round((recent - baseline) / baseline * 100, 1)
            if abs(drift_pct) > DEVIATION_THRESHOLD_PCT:
                flags.append({
                    "customer_reference_id": cust_id,
                    "type": "deviation",
                    "rolling_3m_avg": recent,
                    "rolling_12m_avg": baseline,
                    "drift_pct": drift_pct,
                    "direction": "increase" if drift_pct > 0 else "decrease",
                })
        return flags

    # ---- check 4: explicit failures (sources that report Status directly) --

    def flag_explicit_failures(self, transactions: list) -> list:
        flags = []
        for t in transactions:
            if t["status"] == "Failed":
                flags.append({
                    "customer_reference_id": t["customer_reference_id"],
                    "type": "failed_transaction",
                    "reference_id": t["reference_id"],
                    "transaction_date": t["transaction_date"],
                    "amount": t["amount"],
                })
        return flags

    # ---- orchestration --------------------------------------------------------

    def run(self, bucket, customer_id: str, run_date: str, as_of: date | None = None) -> dict:
        as_of = as_of or date.today()
        transactions, stats = self.load(bucket, customer_id, run_date)

        flags = InflowFlags(
            missing_payments=self.flag_missing_payments(transactions, stats, as_of),
            high_value_transactions=self.flag_high_value_transactions(transactions, stats),
            deviations=self.flag_deviations(stats),
            explicit_failures=self.flag_explicit_failures(transactions),
        )

        output = {
            "customer_id": customer_id,
            "run_date": run_date,
            "as_of": as_of.isoformat(),
            "missing_payments": flags.missing_payments,
            "high_value_transactions": flags.high_value_transactions,
            "deviations": flags.deviations,
            "explicit_failures": flags.explicit_failures,
        }

        path = self.bucket.write_json(f"{customer_id}/{run_date}/inflow_flags.json", output)

        summary = {
            "customer_id": customer_id,
            "run_date": run_date,
            "missing_payments": len(flags.missing_payments),
            "high_value_transactions": len(flags.high_value_transactions),
            "deviations": len(flags.deviations),
            "explicit_failures": len(flags.explicit_failures),
            "output": path,
        }
        return summary
