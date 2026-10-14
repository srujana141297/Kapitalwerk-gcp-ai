"""AML Agent -- this agent calls an LLM.

Takes the Inflow Agent's flagged cases (deviations, high-value transactions)
"""


from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
for candidate in [str(ROOT_DIR), str(ROOT_DIR / "governance")]:
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from governance import BudgetExceeded


def _risk_context_clause(risk_context: dict | None) -> str:
    """Render the Risk Agent's run-level assessment as a sentence the AML prompt
can hang each incident off. The AML Agent used to see only Inflow's raw
"""

    if not risk_context:
        return ""
    tier = risk_context.get("risk_tier") or risk_context.get("risk_score")
    reasons = risk_context.get("risk_reasons") or []
    reasoning = "; ".join(r for r in reasons if r) if reasons else risk_context.get("reasoning", "")
    parts = []
    if tier:
        parts.append(f"The Risk Agent assessed this run as {str(tier).upper()} risk.")
    if reasoning:
        parts.append(f"Its stated reasoning: \"{reasoning}\".")
    if not parts:
        return ""
    return (" " + " ".join(parts) +
            " Reference the Risk Agent's assessment explicitly in your note.")


def _prompt_for_deviation(flag: dict, risk_context: dict | None = None) -> str:
    cust = flag.get("customer_reference_id", "Unknown")
    r3 = flag.get("rolling_3m_avg", 0.0)
    r12 = flag.get("rolling_12m_avg", 0.0)
    drift = flag.get("drift_pct", 0.0)
    direction = flag.get("direction", "increase")
    return (
        f"Draft a brief compliance note explaining why the following counterparty "
        f"was flagged. Counterparty reference: {cust}. "
        f"Recent 3-month average inflow: EUR {r3:,.2f}. "
        f"12-month baseline average: EUR {r12:,.2f}. "
        f"Drift: {drift}% ({direction})."
        f"{_risk_context_clause(risk_context)} "
        f"Write 1-2 sentences suitable for a compliance analyst's case file."
    )


def _prompt_for_high_value(flag: dict, risk_context: dict | None = None) -> str:
    cust = flag.get("customer_reference_id", "Unknown")
    ref = flag.get("reference_id", "TX-REF")
    date_str = flag.get("transaction_date", "recent")
    amount = flag.get("amount", 0.0)
    mult = flag.get("multiplier", 2.0)
    base = flag.get("baseline_3m_avg", 0.0)
    return (
        f"Draft a brief compliance note explaining why the following transaction "
        f"was flagged. Counterparty reference: {cust}. "
        f"Transaction reference: {ref} on {date_str}. "
        f"Amount: EUR {amount:,.2f}, which is {mult}x this "
        f"counterparty's normal 3-month average (EUR {base:,.2f})."
        f"{_risk_context_clause(risk_context)} "
        f"Write 1-2 sentences suitable for a compliance analyst's case file."
    )



class AMLAgent:
    def __init__(self, governance):
        self.governance = governance

    def process_flags(self, inflow_flags: dict, risk_context: dict | None = None,
                      verbose: bool = True) -> list:
        """risk_context (optional): the Risk Agent's output for this run --
{risk_tier / risk_score, risk_reasons, risk_signals}. When supplied,
"""

        cases = []

        # unify both flag types into one queue so the stop condition applies
        # consistently regardless of which kind of case we're partway through
        queue = [("deviation", f) for f in inflow_flags.get("deviations", [])]
        queue += [("high_value_transaction", f) for f in inflow_flags.get("high_value_transactions", [])]
        total = len(queue)

        for i, (flag_type, flag) in enumerate(queue, start=1):
            if flag_type == "deviation":
                prompt = _prompt_for_deviation(flag, risk_context)
                case_id = f"AML-{flag['customer_reference_id'][-4:]}"
            else:
                prompt = _prompt_for_high_value(flag, risk_context)
                case_id = f"AML-{flag['reference_id'][-4:]}"

            try:
                narrative, record = self.governance.complete("aml_agent", prompt)
            except BudgetExceeded as e:
                # Budget hit: stop the queue rather than crashing the run.
                # Cases already drafted are still returned.
                if verbose:
                    print(f"  [aml_agent] STOPPED at case {i}/{total} -- {e.stop_reason}. "
                          f"{total - i + 1} case(s) not processed this run.")
                break

            if verbose:
                print(f"  [aml_agent] case {i}/{total} -- {flag['customer_reference_id']} "
                      f"({record.stop_reason}, {record.latency_ms}ms)")

            cases.append({
                "case_id": case_id,
                "customer_reference_id": flag["customer_reference_id"],
                "flag_type": flag_type,
                "narrative": narrative,
                "status": "under_review",
                "trace_id": record.trace_id,
                "stop_reason": record.stop_reason,
            })

        return cases
