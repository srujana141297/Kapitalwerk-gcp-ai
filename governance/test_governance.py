import json
import os

import pytest

from governance import GovernanceMiddleware
from llm_client import MockLlamaClient


@pytest.fixture
def gov(tmp_path):
    client = MockLlamaClient(simulated_latency_ms=1)
    return GovernanceMiddleware(client, audit_log_path=str(tmp_path / "audit.jsonl"),
                                 max_calls_per_run=3, max_tokens_per_run=10_000)


def test_first_call_hits_the_model(gov):
    text, record = gov.complete("aml_agent", "explain this case")
    assert record.stop_reason == "completed"
    assert record.model == "mock-llama3"
    assert len(text) > 0


def test_identical_prompt_is_served_from_cache(gov):
    text1, record1 = gov.complete("aml_agent", "explain this exact case")
    text2, record2 = gov.complete("aml_agent", "explain this exact case")
    assert record1.stop_reason == "completed"
    assert record2.stop_reason == "cache_hit"
    assert record2.model == "cache"
    assert record2.input_tokens == 0 and record2.output_tokens == 0
    assert text1 == text2


def test_whitespace_differences_still_hit_cache(gov):
    gov.complete("aml_agent", "explain   this case")
    _, record = gov.complete("aml_agent", "explain this case")
    assert record.stop_reason == "cache_hit"


def test_call_budget_is_enforced(gov):
    gov.complete("aml_agent", "case 1")
    gov.complete("aml_agent", "case 2")
    gov.complete("aml_agent", "case 3")
    with pytest.raises(RuntimeError, match="call budget"):
        gov.complete("aml_agent", "case 4")


def test_audit_log_is_append_only_and_has_one_line_per_call(gov, tmp_path):
    gov.complete("aml_agent", "case A")
    gov.complete("aml_agent", "case A")  # cache hit, still logged
    log_path = tmp_path / "audit.jsonl"
    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["stop_reason"] == "completed"
    assert second["stop_reason"] == "cache_hit"


def test_run_summary_reflects_actual_usage(gov):
    gov.complete("aml_agent", "case 1")
    gov.complete("aml_agent", "case 1")  # cache hit -- shouldn't add tokens/calls
    summary = gov.run_summary()
    assert summary["calls_made"] == 1
    assert summary["cache_size"] == 1
    assert summary["tokens_used"] > 0


def test_two_agents_have_independent_budgets(tmp_path):
    client = MockLlamaClient(simulated_latency_ms=1)
    gov = GovernanceMiddleware(
        client, audit_log_path=str(tmp_path / "audit.jsonl"),
        max_calls_per_run={"aml_agent": 2, "risk_agent": 1},
    )

    gov.complete("aml_agent", "aml case 1")
    gov.complete("aml_agent", "aml case 2")
    with pytest.raises(RuntimeError, match="call budget"):
        gov.complete("aml_agent", "aml case 3")  # aml_agent's own budget is now exhausted

    # risk_agent has its own separate budget -- exhausting aml_agent's
    # doesn't touch it
    gov.complete("risk_agent", "risk case 1")
    with pytest.raises(RuntimeError, match="call budget"):
        gov.complete("risk_agent", "risk case 2")  # risk_agent's own (smaller) budget, hit independently


def test_per_agent_summary_is_isolated(tmp_path):
    client = MockLlamaClient(simulated_latency_ms=1)
    gov = GovernanceMiddleware(client, audit_log_path=str(tmp_path / "audit.jsonl"))

    gov.complete("aml_agent", "aml case 1")
    gov.complete("aml_agent", "aml case 2")
    gov.complete("risk_agent", "risk case 1")

    aml_summary = gov.run_summary("aml_agent")
    risk_summary = gov.run_summary("risk_agent")
    assert aml_summary["calls_made"] == 2
    assert risk_summary["calls_made"] == 1

    aggregate = gov.run_summary()
    assert aggregate["calls_made"] == 3
    assert set(aggregate["by_agent"].keys()) == {"aml_agent", "risk_agent"}
    assert aggregate["by_agent"]["aml_agent"]["calls_made"] == 2
    assert aggregate["by_agent"]["risk_agent"]["calls_made"] == 1


def test_cache_is_shared_across_agents(tmp_path):
    client = MockLlamaClient(simulated_latency_ms=1)
    gov = GovernanceMiddleware(client, audit_log_path=str(tmp_path / "audit.jsonl"))

    gov.complete("aml_agent", "identical prompt text")
    _, record = gov.complete("risk_agent", "identical prompt text")
    assert record.stop_reason == "cache_hit"  # risk_agent benefits from aml_agent's earlier call
