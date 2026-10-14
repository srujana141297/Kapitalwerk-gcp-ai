"""Structured top-risk artifact for the Reporting Agent.

`report_text` (see orchestrator/nodes.py::reporting_node) is a human sentence.
"""


from __future__ import annotations

import hashlib
import json
import os

#from reporting_agent.test_exports import bucket

TOP_N = 5


def _case_id(cust: str, flag_type: str, discriminator) -> str:
    """Deterministic, collision-free case id for a single flag.

"""

    stem = str(cust)[-4:]
    digest = hashlib.sha256(f"{cust}:{flag_type}:{discriminator}".encode()).hexdigest()[:4]
    return f"AML-{stem}-{digest}"


def _name_map(transactions: list[dict]) -> dict[str, str]:
    """customer_reference_id -> display counterparty name, when the source
carried one. Membership sources don't, so we fall back to the reference.
"""

    out: dict[str, str] = {}
    for t in transactions:
        name = t.get("counterparty_name")
        if name:
            out.setdefault(t["customer_reference_id"], name)
    return out


def _high_value_entry(flag: dict, name_map: dict[str, str]) -> dict:
    multiplier = float(flag.get("multiplier", 0) or 0)
    tier = "high" if multiplier >= 2.5 else "medium"
    cust = flag["customer_reference_id"]
    return {
        "case_id": _case_id(cust, "high_value_transaction", flag.get("reference_id")),
        "flag_type": "high_value_transaction",
        "transaction_reference": flag.get("reference_id"),
        "customer_reference_id": cust,
        "counterparty": name_map.get(cust, cust),
        "amount": flag.get("amount"),
        "risk_score": round(multiplier, 2),
        "risk_tier": tier,
        "reason": (f"Transaction was {multiplier}x this counterparty's normal 3-month "
                   f"average (EUR {flag.get('baseline_3m_avg')})."),
        "suggested_action": ("Escalate to AML review; confirm source of funds before release."
                             if tier == "high" else
                             "Analyst review of the transaction against counterparty history."),
    }


def _deviation_entry(flag: dict, name_map: dict[str, str]) -> dict:
    drift = float(flag.get("drift_pct", 0) or 0)
    tier = "high" if abs(drift) >= 40 else "medium"
    cust = flag["customer_reference_id"]
    churn_note = (" Decrease may indicate reduced activity / churn rather than an AML concern."
                  if flag.get("direction") == "decrease" else "")
    return {
        "case_id": _case_id(cust, "deviation", flag.get("drift_pct")),
        "flag_type": "deviation",
        "transaction_reference": None,
        "customer_reference_id": cust,
        "counterparty": name_map.get(cust, cust),
        "amount": flag.get("rolling_3m_avg"),
        "risk_score": round(abs(drift) / 10, 2),
        "risk_tier": tier,
        "reason": (f"Rolling 3-month average drifted {drift}% ({flag.get('direction')}) "
                   f"vs the 12-month baseline.{churn_note}"),
        "suggested_action": "Monitor counterparty trend; review for churn vs. AML concern.",
    }


def build_top_risk_report(
    inflow_flags: dict,
    transactions: list[dict] | None = None,
    run_risk_tier: str | None = None,
    top_n: int = TOP_N,
) -> list[dict]:
    """Rank every high-value / deviation flag into the structured shape above,
sorted by risk_score descending and capped to top_n.
"""

    name_map = _name_map(transactions or [])
    entries = [_high_value_entry(f, name_map) for f in inflow_flags.get("high_value_transactions", [])]
    entries += [_deviation_entry(f, name_map) for f in inflow_flags.get("deviations", [])]
    entries.sort(key=lambda e: e["risk_score"], reverse=True)
    return entries[:top_n]


def write_top_risk_report(
    bucket,
    customer_id: str,
    run_date: str,
    inflow_flags: dict,
    run_risk_tier: str | None = None,
    top_n: int = TOP_N,
) -> str:
    """Load the run's transactions (for counterparty names) from the bucket,
build the ranked list, and persist it next to the other per-run artifacts.
"""

    prefix = f"{customer_id}/{run_date}"
    try:
        transactions = bucket.read_json(
        f"{prefix}/normalized_transactions.json"
    )
    except Exception:
        transactions = []

    top = build_top_risk_report(inflow_flags, transactions, run_risk_tier, top_n)
    payload = {
        "customer_id": customer_id,
        "run_date": run_date,
        "run_risk_tier": run_risk_tier,
        "count": len(top),
        "top_transactions": top,
    }
    blob_path = f"{customer_id}/{run_date}/top_risk_report.json"
    out_path = bucket.write_json(
        blob_path,
        payload
    )
    return out_path
