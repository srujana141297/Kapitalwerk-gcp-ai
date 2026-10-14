"""Risk Agent -- now LLM-based, the second (and last) agent that calls a
model. Combines two inputs:

"""


from __future__ import annotations

import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
for candidate in [str(ROOT_DIR), str(ROOT_DIR / "governance")]:
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from governance import BudgetExceeded

DEFAULT_GUARDRAILS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guardrails.txt")

# Fail-closed PII guard: if raw-looking PII is present in what would be sent
# to the LLM, block the call, log it, and mark records for re-masking.

# Generic backstops, deliberately conservative so structured pipeline data
# (reference IDs, decimal amounts, ISO dates) doesn't false-positive.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# International dialing form: a '+' country code followed by mostly digits.
_INTL_PHONE_RE = re.compile(r"\+\d[\d\s()./-]{6,}\d")
# A pure number/amount or bare ISO date -- structured data, never PII.
_PURE_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?%?$")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")
# A field that, by naming convention, should carry a masked pseudonym.
_SHOULD_BE_MASKED_FIELDS = {"counterparty_name"}
# Two or more capitalized words in a row -- a human-name shape.
_NAME_RE = re.compile(r"\b[A-ZÄÖÜ][a-zäöüß]+\s+[A-ZÄÖÜ][a-zäöüß]+\b")

# EU/German banking PII taxonomy: IBAN (ISO 13616) and BIC/SWIFT (ISO 9362)
# identifiers that could leak into a free-text field and must never reach the LLM.
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_BIC_RE = re.compile(r"\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b")


def _looks_like_phone(value: str) -> bool:
    """True only for values that genuinely resemble a phone number, not for the
structured numeric/date/reference data that legitimately flows through the
"""

    v = value.strip()
    if _INTL_PHONE_RE.search(v):
        return True
    if _PURE_NUMBER_RE.match(v) or _ISO_DATE_RE.match(v):
        return False
    digit_count = sum(c.isdigit() for c in v)
    return digit_count >= 10 and bool(re.search(r"[()\s./-]", v))


def _iter_scannable(inflow_flags: dict):
    """Yield (customer_reference_id, field, value) for every string value in
every flag, so the scan sees exactly the material that would flow into the
"""

    for bucket in ("deviations", "high_value_transactions", "missing_payments", "explicit_failures"):
        for flag in inflow_flags.get(bucket, []):
            if not isinstance(flag, dict):
                continue
            cust_id = flag.get("customer_reference_id")
            for field_name, value in flag.items():
                if isinstance(value, str):
                    yield cust_id, field_name, value


def _name_masking_sources() -> set[str]:
    """The set of ingestion source_types whose schema is supposed to MASK
counterparty_name (i.e. it carries a real *person* name -- e.g.
"""

    try:
        from schema_config import SOURCE_SCHEMAS
    except Exception:
        return set()
    return {
        source
        for source, cfg in SOURCE_SCHEMAS.items()
        if "counterparty_name" in (cfg.get("mask_fields") or [])
    }


def _iter_scannable_transactions(transactions: list):
    """Yield (customer_reference_id, field, value) for every string value in every
raw transaction record -- INCLUDING nested `extra` dict values (City /
"""

    name_masking_sources = _name_masking_sources()
    for t in transactions or []:
        if not isinstance(t, dict):
            continue
        cust_id = t.get("customer_reference_id")
        source_type = t.get("source_type")
        for field_name, value in t.items():
            if field_name == "extra":
                continue  # nested dict, walked separately below
            if not isinstance(value, str):
                continue
            if field_name == "counterparty_name" and source_type not in name_masking_sources:
                continue  # legitimately-unmasked business entity name -- skip
            yield cust_id, field_name, value
        extra = t.get("extra")
        if isinstance(extra, dict):
            for field_name, value in extra.items():
                if isinstance(value, str):
                    yield cust_id, field_name, value


def _scan_value(cust_id, field_name, value) -> dict | None:
    """Apply the PII shape checks to a single (cust_id, field, value) triple.
Returns a finding dict or None. Shared by the inflow-flags scan and the
"""

    if _EMAIL_RE.search(value):
        return _finding(cust_id, field_name, value, "email")
    # Checked before the phone heuristic -- an IBAN's digit run would
    # otherwise false-positive as a phone number.
    if _IBAN_RE.search(value):
        return _finding(cust_id, field_name, value, "iban")
    if _BIC_RE.search(value):
        return _finding(cust_id, field_name, value, "bic_swift")
    if _looks_like_phone(value):
        return _finding(cust_id, field_name, value, "phone")
    # a field expected to be masked but which isn't, and looks like a name
    if field_name in _SHOULD_BE_MASKED_FIELDS and not value.startswith("MASKED-") \
            and _NAME_RE.search(value):
        return _finding(cust_id, field_name, value, "unmasked_name")
    return None


def scan_for_pii(inflow_flags: dict, transactions: list | None = None) -> list[dict]:
    """Return one finding per suspected raw-PII value. Each finding names the
customer_reference_id it belongs to (so callers can mark exactly which
"""

    findings = []
    for cust_id, field_name, value in _iter_scannable(inflow_flags):
        finding = _scan_value(cust_id, field_name, value)
        if finding is not None:
            findings.append(finding)
    if transactions:
        for cust_id, field_name, value in _iter_scannable_transactions(transactions):
            finding = _scan_value(cust_id, field_name, value)
            if finding is not None:
                findings.append(finding)
    return findings


def _finding(cust_id, field_name, value, pattern) -> dict:
    excerpt = (value[:6] + "...") if len(value) > 9 else "***"
    return {
        "customer_reference_id": cust_id,
        "field": field_name,
        "pattern": pattern,
        "excerpt": excerpt,
    }


def load_guardrails(path: str = DEFAULT_GUARDRAILS_PATH) -> str:
    with open(path) as f:
        return f.read()


def compute_signals(inflow_flags: dict, total_counterparties: int) -> dict:
    """The deterministic part -- plain counts and percentages, no LLM.
"""
    missing = inflow_flags.get("missing_payments", [])
    deviations = inflow_flags.get("deviations", [])
    high_value = inflow_flags.get("high_value_transactions", [])

    missed_pct = round(len(missing) / total_counterparties * 100, 1) if total_counterparties else 0.0

    hv_counts = Counter(f["customer_reference_id"] for f in high_value)
    repeat_high_value = [cust for cust, n in hv_counts.items() if n > 1]

    return {
        "total_counterparties": total_counterparties,
        "missed_payment_count": len(missing),
        "missed_payment_pct": missed_pct,
        "deviation_count": len(deviations),
        "deviations": deviations,
        "high_value_count": len(high_value),
        "high_value_transactions": high_value,
        "repeat_high_value_counterparties": repeat_high_value,
    }


def build_prompt(signals: dict, guardrails_text: str) -> str:
    deviation_lines = "\n".join(
        f"  - {d['customer_reference_id']}: {d['drift_pct']}% {d['direction']} vs 12-month baseline"
        for d in signals["deviations"]
    ) or "  (none)"

    hv_lines = "\n".join(
        f"  - {h['customer_reference_id']}: {h['reference_id']} was {h['multiplier']}x normal amount"
        for h in signals["high_value_transactions"]
    ) or "  (none)"

    return (
        f"{guardrails_text}\n\n"
        f"CURRENT RUN SIGNALS\n"
        f"Total active counterparties: {signals['total_counterparties']}\n"
        f"Missed payments: {signals['missed_payment_count']} "
        f"({signals['missed_payment_pct']}% of active counterparties)\n"
        f"Trend deviations ({signals['deviation_count']}):\n{deviation_lines}\n"
        f"High-value transactions ({signals['high_value_count']}):\n{hv_lines}\n"
        f"Counterparties with repeat high-value flags: "
        f"{', '.join(signals['repeat_high_value_counterparties']) or 'none'}\n\n"
        f"Apply the guardrails policy above to these signals and respond in "
        f"the exact output format specified."
    )


def _parse_response(text: str) -> tuple[str, str, bool]:
    """Returns (tier, reasoning, parse_failed).
"""
    tier_match = re.search(r"RISK_TIER:\s*(low|medium|high)", text, re.IGNORECASE)
    reasoning_match = re.search(r"REASONING:\s*(.+)", text, re.IGNORECASE | re.DOTALL)

    if tier_match:
        reasoning = reasoning_match.group(1).strip() if reasoning_match else text.strip()
        return tier_match.group(1).lower(), reasoning, False

    # fallback: model didn't follow the format -- search for a tier keyword
    # rather than fail outright, but flag it so callers know to trust this less
    for tier in ("high", "medium", "low"):
        if re.search(rf"\b{tier}\b", text, re.IGNORECASE):
            return tier, text.strip(), True

    return "medium", text.strip(), True  # safest neutral default when totally unparseable


class RiskAgent:
    def __init__(self, governance, guardrails_path: str = DEFAULT_GUARDRAILS_PATH):
        self.governance = governance
        self.guardrails_text = load_guardrails(guardrails_path)

    def assess(self, inflow_flags: dict, total_counterparties: int,
               transactions: list | None = None, verbose: bool = True) -> dict:
        signals = compute_signals(inflow_flags, total_counterparties)

        # Fail-closed PII guard, before any prompt is built -- scans both
        # Inflow's flags and the raw transactions.
        pii_findings = scan_for_pii(inflow_flags, transactions=transactions)
        if pii_findings:
            records_to_remask = sorted({f["customer_reference_id"] for f in pii_findings
                                        if f["customer_reference_id"]})
            self.governance.log_governance_event(
                "risk_agent", stop_reason="pii_detected_blocked",
                detail={"findings": pii_findings, "records_to_remask": records_to_remask},
            )
            if verbose:
                print(f"  [risk_agent] PII DETECTED pre-call -- LLM call BLOCKED (fail-closed). "
                      f"{len(pii_findings)} finding(s); {len(records_to_remask)} record(s) marked for re-masking.")
            # Conservative tier, no LLM reasoning, and a hard signal to the
            # orchestrator not to run AML on this data until it's re-masked.
            return {
                "risk_tier": "high",
                "reasoning": ("PII detected in inflow signals / raw transactions before the model "
                              "call; the call was blocked (fail-closed) and the affected records "
                              "were marked for re-masking. No LLM assessment was performed, and "
                              "neither AML nor Marketing may run their LLM on these records until "
                              "ingestion re-masks them."),
                "signals": signals,
                "trace_id": None,
                "stop_reason": "pii_detected_blocked",
                "parse_failed": False,
                "llm_available": False,
                "pii_detected": True,
                "pii_findings": pii_findings,
                "records_to_remask": records_to_remask,
                "aml_blocked": True,
            }

        prompt = build_prompt(signals, self.guardrails_text)

        try:
            response_text, record = self.governance.complete("risk_agent", prompt)
        except BudgetExceeded as e:
            if verbose:
                print(f"  [risk_agent] STOPPED -- {e.stop_reason}. Falling back to signals-only assessment.")
            # Graceful degradation: conservative tier from raw signals alone.
            fallback_tier = "high" if (signals["deviation_count"] >= 2 or signals["missed_payment_pct"] >= 20) else "medium"
            return {
                "risk_tier": fallback_tier,
                "reasoning": f"LLM assessment unavailable ({e.stop_reason}); tier estimated from raw signal counts only.",
                "signals": signals,
                "trace_id": None,
                "stop_reason": e.stop_reason,
                "parse_failed": False,
                "llm_available": False,
                "pii_detected": False,
                "pii_findings": [],
                "records_to_remask": [],
                "aml_blocked": False,
            }

        tier, reasoning, parse_failed = _parse_response(response_text)
        if verbose:
            note = ", PARSE FAILED -- used fallback keyword match" if parse_failed else ""
            print(f"  [risk_agent] tier={tier} ({record.stop_reason}, {record.latency_ms}ms{note})")

        return {
            "risk_tier": tier,
            "reasoning": reasoning,
            "signals": signals,
            "trace_id": record.trace_id,
            "stop_reason": record.stop_reason,
            "parse_failed": parse_failed,
            "llm_available": True,
            "pii_detected": False,
            "pii_findings": [],
            "records_to_remask": [],
            "aml_blocked": False,
        }
