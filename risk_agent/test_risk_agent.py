import os
import sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "governance"))
# ingestion_agent on the path so _name_masking_sources() can read schema_config.
sys.path.insert(0, os.path.join(_HERE, "..", "ingestion_agent"))

import pytest

from risk_agent import (
    RiskAgent, compute_signals, build_prompt, _parse_response, load_guardrails,
    scan_for_pii,
)
from governance import GovernanceMiddleware
from llm_client import LLMClient, LLMResponse


class FakeRiskLLMClient(LLMClient):
    """Returns a properly formatted RISK_TIER/REASONING response, unlike the
shared MockLlamaClient (which is intentionally generic and doesn't know
"""

    def __init__(self, canned_text: str):
        self.canned_text = canned_text
        self.calls = 0

    def complete(self, prompt: str) -> LLMResponse:
        self.calls += 1
        return LLMResponse(text=self.canned_text, input_tokens=200, output_tokens=40,
                            model="fake", latency_ms=5)


GUARDRAILS_PATH = os.path.join(_HERE, "guardrails.txt")


def make_agent(tmp_path, canned_text):
    client = FakeRiskLLMClient(canned_text)
    gov = GovernanceMiddleware(client, audit_log_path=str(tmp_path / "audit.jsonl"))
    return RiskAgent(gov, guardrails_path=GUARDRAILS_PATH), gov


EMPTY_FLAGS = {"missing_payments": [], "deviations": [], "high_value_transactions": []}


def test_guardrails_file_loads():
    text = load_guardrails(GUARDRAILS_PATH)
    assert "HIGH risk" in text
    assert "RISK_TIER" in text


def test_compute_signals_percentage_math():
    flags = {
        "missing_payments": [{"customer_reference_id": "A"}, {"customer_reference_id": "B"}],
        "deviations": [],
        "high_value_transactions": [],
    }
    signals = compute_signals(flags, total_counterparties=10)
    assert signals["missed_payment_count"] == 2
    assert signals["missed_payment_pct"] == 20.0


def test_compute_signals_detects_repeat_high_value_counterparty():
    flags = {
        "missing_payments": [],
        "deviations": [],
        "high_value_transactions": [
            {"customer_reference_id": "CUST-1", "reference_id": "r1", "multiplier": 2.0},
            {"customer_reference_id": "CUST-1", "reference_id": "r2", "multiplier": 2.5},
            {"customer_reference_id": "CUST-2", "reference_id": "r3", "multiplier": 1.9},
        ],
    }
    signals = compute_signals(flags, total_counterparties=10)
    assert signals["repeat_high_value_counterparties"] == ["CUST-1"]


def test_prompt_includes_guardrails_and_signals():
    signals = compute_signals(EMPTY_FLAGS, total_counterparties=5)
    guardrails = load_guardrails(GUARDRAILS_PATH)
    prompt = build_prompt(signals, guardrails)
    assert "HIGH risk" in prompt          # guardrails text made it in
    assert "Total active counterparties: 5" in prompt  # computed signal made it in


def test_parse_well_formed_response():
    text = "RISK_TIER: high\nREASONING: CUST-HOSP-1001 shows a large deviation."
    tier, reasoning, failed = _parse_response(text)
    assert tier == "high"
    assert "CUST-HOSP-1001" in reasoning
    assert failed is False


def test_parse_falls_back_on_malformed_response():
    text = "I think this looks like a medium concern overall given the data."
    tier, reasoning, failed = _parse_response(text)
    assert tier == "medium"
    assert failed is True


def test_assess_returns_high_tier_from_well_formed_llm_response(tmp_path):
    agent, _ = make_agent(tmp_path, "RISK_TIER: high\nREASONING: Two deviations detected, driven by CUST-HOSP-1001.")
    result = agent.assess(EMPTY_FLAGS, total_counterparties=10)
    assert result["risk_tier"] == "high"
    assert result["parse_failed"] is False
    assert result["llm_available"] is True


def test_assess_flags_parse_failure_but_still_returns_a_tier(tmp_path):
    agent, _ = make_agent(tmp_path, "This account seems fine, low concern here.")
    result = agent.assess(EMPTY_FLAGS, total_counterparties=10)
    assert result["risk_tier"] == "low"
    assert result["parse_failed"] is True


def test_assess_degrades_gracefully_on_budget_exceeded(tmp_path):
    client = FakeRiskLLMClient("RISK_TIER: high\nREASONING: n/a")
    gov = GovernanceMiddleware(client, audit_log_path=str(tmp_path / "audit.jsonl"),
                                max_calls_per_run={"risk_agent": 0})
    agent = RiskAgent(gov, guardrails_path=GUARDRAILS_PATH)

    result = agent.assess(EMPTY_FLAGS, total_counterparties=10, verbose=False)
    assert result["llm_available"] is False
    assert result["risk_tier"] in ("low", "medium", "high")  # fallback still produces a usable tier
    assert "unavailable" in result["reasoning"]


# ---- fail-closed PII guard --------------------------------------------------

def test_scan_for_pii_passes_clean_masked_data():
    flags = {
        "deviations": [{"customer_reference_id": "CUST-HOSP-1001", "drift_pct": 23.6,
                        "direction": "increase", "reference_id": "E2E-DE-40170"}],
        "high_value_transactions": [], "missing_payments": [], "explicit_failures": [],
    }
    assert scan_for_pii(flags) == []


def test_scan_for_pii_detects_email_and_phone():
    flags = {
        "deviations": [{"customer_reference_id": "CUST-1", "note": "call +49 30 1234567 or a@b.com"}],
        "high_value_transactions": [], "missing_payments": [], "explicit_failures": [],
    }
    findings = scan_for_pii(flags)
    patterns = {f["pattern"] for f in findings}
    assert "email" in patterns or "phone" in patterns
    assert all(f["customer_reference_id"] == "CUST-1" for f in findings)


def test_scan_for_pii_detects_unmasked_name_where_masking_expected():
    flags = {
        "deviations": [{"customer_reference_id": "CUST-1", "counterparty_name": "Anna Keller"}],
        "high_value_transactions": [], "missing_payments": [], "explicit_failures": [],
    }
    findings = scan_for_pii(flags)
    assert any(f["pattern"] == "unmasked_name" for f in findings)
    # excerpt must not leak the full raw value into the finding
    assert all("Keller" not in f["excerpt"] for f in findings)


def test_scan_for_pii_detects_iban():
    """A real-shaped German IBAN leaking into a free-text field is caught.
"""
    flags = {
        "deviations": [{"customer_reference_id": "CUST-1",
                        "note": "please pay to DE89370400440532013000 thanks"}],
        "high_value_transactions": [], "missing_payments": [], "explicit_failures": [],
    }
    findings = scan_for_pii(flags)
    assert any(f["pattern"] == "iban" for f in findings)
    # excerpt-only discipline: the full IBAN never lands in the finding
    assert all("0532013000" not in f["excerpt"] for f in findings)


def test_scan_for_pii_detects_bic_swift():
    """Both 8-char and 11-char BIC/SWIFT shapes are caught.
"""
    for bic in ("DEUTDEFF", "DEUTDEFF500"):
        flags = {
            "deviations": [{"customer_reference_id": "CUST-2", "note": f"bank {bic}"}],
            "high_value_transactions": [], "missing_payments": [], "explicit_failures": [],
        }
        findings = scan_for_pii(flags)
        assert any(f["pattern"] == "bic_swift" for f in findings), bic


def test_scan_for_pii_no_false_positive_on_pipeline_reference_formats():
    """The pipeline's own reference-ID and masked-pseudonym formats must NOT
trip the new IBAN/BIC patterns (or any other) -- a false positive here
"""

    flags = {
        "deviations": [{"customer_reference_id": "CUST-HOSP-1001",
                        "reference_id": "E2E-DE-40170",
                        "counterparty_name": "MASKED-a1b2c3d4e5f6a7b8",
                        "direction": "increase"}],
        "high_value_transactions": [
            {"customer_reference_id": "CUST-HOSP-1001", "reference_id": "MND-DE-2201"},
        ],
        "missing_payments": [], "explicit_failures": [],
    }
    assert scan_for_pii(flags) == []


def test_assess_blocks_llm_and_marks_records_when_pii_present(tmp_path):
    """Clean-block case: PII present -> no LLM call, an audit entry written,
records flagged for re-masking, and AML explicitly blocked.
"""

    agent, gov = make_agent(tmp_path, "RISK_TIER: high\nREASONING: should never be used")
    flags = {
        "deviations": [{"customer_reference_id": "CUST-9", "counterparty_name": "Markus Vogel"}],
        "high_value_transactions": [], "missing_payments": [], "explicit_failures": [],
    }
    result = agent.assess(flags, total_counterparties=10, verbose=False)

    assert result["pii_detected"] is True
    assert result["aml_blocked"] is True
    assert result["llm_available"] is False
    assert result["stop_reason"] == "pii_detected_blocked"
    assert "CUST-9" in result["records_to_remask"]
    # the LLM was never called
    assert gov.run_summary("risk_agent")["calls_made"] == 0
    # ...but the block was written to the immutable audit trail
    audit = gov.audit_store.all()
    assert any(r["stop_reason"] == "pii_detected_blocked" for r in audit)


def test_assess_clean_pass_does_not_block(tmp_path):
    agent, _ = make_agent(tmp_path, "RISK_TIER: medium\nREASONING: nominal.")
    result = agent.assess(EMPTY_FLAGS, total_counterparties=10, verbose=False)
    assert result["pii_detected"] is False
    assert result["aml_blocked"] is False
    assert result["llm_available"] is True


# ---- fail-closed PII guard over the RAW transactions (not just inflow flags) --

def _txn(**kw):
    base = {"customer_reference_id": "CUST-HOSP-1001", "reference_id": "E2E-DE-40042",
            "counterparty_name": "Charite - Universitaetsmedizin Berlin",
            "status": "Success", "source_type": "hospital_vendor",
            "extra": {"City": "Berlin", "Postal_Code": "10115", "Invoice_Number": "INV-2024-40042"}}
    base.update(kw)
    return base


def test_scan_transactions_clean_hospital_data_no_false_positive():
    """A real hospital-vendor record -- unmasked business-entity counterparty
name, a German city, a 5-digit postal code -- must NOT false-positive (a
"""

    findings = scan_for_pii(EMPTY_FLAGS, transactions=[_txn(), _txn(customer_reference_id="CUST-HOSP-1004")])
    assert findings == []


def test_scan_transactions_detects_injected_email():
    """PII injected into a raw transaction field (not an inflow flag) is caught.
"""
    txns = [_txn(reference_id="please remit to jane.doe@example.com")]
    findings = scan_for_pii(EMPTY_FLAGS, transactions=txns)
    assert any(f["pattern"] == "email" for f in findings)
    assert all("jane.doe" not in f["excerpt"] for f in findings)


def test_scan_transactions_detects_injected_iban_in_extra():
    """PII hiding in a nested `extra` dict value is still scanned.
"""
    txns = [_txn(extra={"City": "Berlin", "Postal_Code": "10115",
                        "Invoice_Number": "DE89370400440532013000"})]
    findings = scan_for_pii(EMPTY_FLAGS, transactions=txns)
    assert any(f["pattern"] == "iban" for f in findings)


def test_scan_transactions_unmasked_name_only_for_masking_sources():
    """counterparty_name name-shape check fires for a source that SHOULD mask it
(individual_tenant) but is legitimately skipped for hospital_vendor.
"""

    # hospital: unmasked business entity name -> NOT flagged
    assert scan_for_pii(EMPTY_FLAGS, transactions=[_txn(counterparty_name="Anna Keller")]) == []
    # individual_tenant: unmasked person name -> flagged
    leaky = _txn(source_type="individual_tenant", counterparty_name="Anna Keller")
    findings = scan_for_pii(EMPTY_FLAGS, transactions=[leaky])
    assert any(f["pattern"] == "unmasked_name" for f in findings)


def test_assess_blocks_when_pii_only_in_transactions(tmp_path):
    """The gate must trip on a transactions-only leak even when inflow flags are
clean -- and block the LLM, mark the record, and audit it.
"""

    agent, gov = make_agent(tmp_path, "RISK_TIER: high\nREASONING: never used")
    txns = [_txn(customer_reference_id="CUST-9", reference_id="mail me anna.b@corp.de")]
    result = agent.assess(EMPTY_FLAGS, total_counterparties=10, transactions=txns, verbose=False)
    assert result["pii_detected"] is True
    assert result["aml_blocked"] is True
    assert result["llm_available"] is False
    assert "CUST-9" in result["records_to_remask"]
    assert gov.run_summary("risk_agent")["calls_made"] == 0
    assert any(r["stop_reason"] == "pii_detected_blocked" for r in gov.audit_store.all())


def test_assess_clean_transactions_do_not_block(tmp_path):
    """Clean transactions passed in must not change the happy path -- LLM runs.
"""
    agent, _ = make_agent(tmp_path, "RISK_TIER: medium\nREASONING: nominal.")
    result = agent.assess(EMPTY_FLAGS, total_counterparties=10,
                          transactions=[_txn()], verbose=False)
    assert result["pii_detected"] is False
    assert result["llm_available"] is True


def test_risk_agent_budget_is_independent_of_aml_agent(tmp_path):
    """The whole point of per-agent budgets: risk_agent hitting its own limit
must not be affected by, or affect, aml_agent's separate budget.
"""

    client = FakeRiskLLMClient("RISK_TIER: medium\nREASONING: n/a")
    gov = GovernanceMiddleware(
        client, audit_log_path=str(tmp_path / "audit.jsonl"),
        max_calls_per_run={"aml_agent": 1, "risk_agent": 1},
    )
    # aml_agent uses its one call
    gov.complete("aml_agent", "some aml prompt")
    # risk_agent should still get its own independent first call
    agent = RiskAgent(gov, guardrails_path=GUARDRAILS_PATH)
    result = agent.assess(EMPTY_FLAGS, total_counterparties=10, verbose=False)
    assert result["llm_available"] is True
