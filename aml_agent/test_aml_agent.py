import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "governance"))

from aml_agent import AMLAgent, _prompt_for_deviation, _prompt_for_high_value
from governance import GovernanceMiddleware
from llm_client import MockLlamaClient


def make_agent(tmp_path):
    client = MockLlamaClient(simulated_latency_ms=1)
    gov = GovernanceMiddleware(client, audit_log_path=str(tmp_path / "audit.jsonl"))
    return AMLAgent(gov), gov


def test_deviation_flag_produces_one_case(tmp_path):
    agent, _ = make_agent(tmp_path)
    flags = {
        "deviations": [{
            "customer_reference_id": "CUST-HOSP-1001", "rolling_3m_avg": 19392.55,
            "rolling_12m_avg": 15571.82, "drift_pct": 23.6, "direction": "increase",
        }],
        "high_value_transactions": [],
    }
    cases = agent.process_flags(flags)
    assert len(cases) == 1
    assert cases[0]["flag_type"] == "deviation"
    assert cases[0]["customer_reference_id"] == "CUST-HOSP-1001"
    assert cases[0]["status"] == "under_review"


def test_high_value_flag_produces_one_case(tmp_path):
    agent, _ = make_agent(tmp_path)
    flags = {
        "deviations": [],
        "high_value_transactions": [{
            "customer_reference_id": "CUST-HOSP-1003", "reference_id": "E2E-DE-40170",
            "transaction_date": "2024-11-28", "amount": 19000.0,
            "baseline_3m_avg": 8000.0, "multiplier": 2.43,
        }],
    }
    cases = agent.process_flags(flags)
    assert len(cases) == 1
    assert cases[0]["flag_type"] == "high_value_transaction"


def test_prompt_references_risk_agent_assessment_when_provided():
    """The AML prompt must explicitly carry the Risk Agent's tier + reasoning
so the narrative reflects the whole assessment chain.
"""

    dev = {"customer_reference_id": "CUST-HOSP-1001", "rolling_3m_avg": 19392.55,
           "rolling_12m_avg": 15571.82, "drift_pct": 23.6, "direction": "increase"}
    risk_context = {"risk_tier": "high",
                    "risk_reasons": ["CUST-HOSP-1001 drove the highest concern (23.6% increase)."]}
    prompt = _prompt_for_deviation(dev, risk_context)
    assert "HIGH risk" in prompt
    assert "highest concern" in prompt
    assert "Risk Agent" in prompt

    # and it degrades cleanly to the old standalone prompt with no context
    assert "Risk Agent" not in _prompt_for_deviation(dev)


def test_risk_context_flows_into_case_narrative(tmp_path):
    agent, _ = make_agent(tmp_path)
    flags = {
        "deviations": [{"customer_reference_id": "CUST-HOSP-1001", "rolling_3m_avg": 19392.55,
                        "rolling_12m_avg": 15571.82, "drift_pct": 23.6, "direction": "increase"}],
        "high_value_transactions": [],
    }
    risk_context = {"risk_tier": "high", "risk_reasons": ["Two deviations detected."]}
    cases = agent.process_flags(flags, risk_context=risk_context)
    # MockLlamaClient echoes the prompt tail, so the risk framing reaches the note
    assert len(cases) == 1
    assert "compliance review" in cases[0]["narrative"].lower()


def test_no_flags_produces_no_cases_and_no_llm_calls(tmp_path):
    agent, gov = make_agent(tmp_path)
    cases = agent.process_flags({"deviations": [], "high_value_transactions": []})
    assert cases == []
    assert gov.run_summary()["calls_made"] == 0


def test_identical_case_across_two_runs_is_cached(tmp_path):
    agent, gov = make_agent(tmp_path)
    flags = {
        "deviations": [{
            "customer_reference_id": "CUST-HOSP-1001", "rolling_3m_avg": 19392.55,
            "rolling_12m_avg": 15571.82, "drift_pct": 23.6, "direction": "increase",
        }],
        "high_value_transactions": [],
    }
    agent.process_flags(flags)
    cases_again = agent.process_flags(flags)  # same exact case again
    assert cases_again[0]["stop_reason"] == "cache_hit"
    assert gov.run_summary()["calls_made"] == 1  # second run didn't hit the model


def test_hitting_budget_stops_gracefully_instead_of_crashing(tmp_path):
    """This is the actual stop condition test: with a budget of 2 calls and 5
distinct cases queued, the agent must return the 2 it completed and
"""

    client = MockLlamaClient(simulated_latency_ms=1)
    gov = GovernanceMiddleware(client, audit_log_path=str(tmp_path / "audit.jsonl"), max_calls_per_run=2)
    agent = AMLAgent(gov)

    flags = {
        "deviations": [
            {"customer_reference_id": f"CUST-{i}", "rolling_3m_avg": 10000.0,
             "rolling_12m_avg": 8000.0, "drift_pct": 25.0, "direction": "increase"}
            for i in range(5)
        ],
        "high_value_transactions": [],
    }

    cases = agent.process_flags(flags, verbose=False)  # must not raise

    assert len(cases) == 2  # only the two the budget allowed
    summary = gov.run_summary("aml_agent")  # per-agent slice, now that governance tracks multiple agents
    assert summary["calls_made"] == 2
    assert summary["stopped_early"] is True
    assert summary["stop_reason"] == "budget_exceeded_calls"
