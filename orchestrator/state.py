"""Shared state that flows through the orchestrator graph. Every node reads
what it needs from this and writes its own output back into it -- this is
what LangGraph passes between nodes, so no agent needs to know how to call
"""


from __future__ import annotations

from typing import TypedDict, Optional


class PipelineState(TypedDict, total=False):
    # inputs, set once at the start of a run
    customer_id: str
    source_type: str
    csv_path: str
    run_date: str
    tenant_name: str            # display name for reminder emails, e.g. "MedSupply Direct"
    reminder_recipient_override: str  # optional -- see reporting_agent/weekly_reminders.py

    # written by ingestion_node
    ingestion_summary: dict
    transactions: list
    historical_stats: dict

    # written by inflow_node
    inflow_flags: dict          # missing_payments / high_value_transactions / deviations / explicit_failures

    # written by risk_node
    risk_score: str             # low / medium / high
    risk_reasons: list
    risk_signals: dict          # deterministic counts the tier was derived from
    # Fail-closed PII guard: when True, AML and Marketing both skip their LLM
    # call, and records_to_remask lists which IDs ingestion must re-mask.
    pii_blocked: bool
    pii_findings: list
    records_to_remask: list

    # written by aml_node  (the ONLY node that calls an LLM)
    aml_cases: list
    governance_summary: dict   # calls_made, tokens_used, cache_size, stopped_early, stop_reason

    # written by reporting_node
    report_text: str
    reminder_results: list      # full per-counterparty send results, see weekly_reminders.py
    top_risk_report: str        # path to the structured top-5 risk artifact (reporting_agent/top_risk.py)

    # written by marketing_node (LLM-based -- postal-code heatmap)
    marketing_summary: dict

    # written by reminder_node
    reminders_queued: list
