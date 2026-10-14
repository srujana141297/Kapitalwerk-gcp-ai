import sys
import json
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "governance"))

import pytest

from marketing_agent import (
    MarketingAgent, aggregate_by_postal, build_prompt, parse_annotations, CITY_COORDS,
)
from governance import GovernanceMiddleware
from llm_client import LLMClient, LLMResponse, MockLlamaClient


class FakeMarketingLLMClient(LLMClient):
    """Returns well-formed POSTAL annotation lines, unlike the generic
MockLlamaClient -- lets us test the parser against clean output.
"""

    def __init__(self, canned_text: str):
        self.canned_text = canned_text
        self.calls = 0

    def complete(self, prompt: str) -> LLMResponse:
        self.calls += 1
        return LLMResponse(text=self.canned_text, input_tokens=200, output_tokens=40,
                           model="fake", latency_ms=5)


def txn(postal, city, amount, status="Success"):
    return {"customer_reference_id": "X", "amount": amount, "status": status,
            "extra": {"City": city, "Postal_Code": postal}}


TXNS = [
    txn("10115", "Berlin", 1000.0),
    txn("10115", "Berlin", 3000.0),
    txn("10115", "Berlin", 2000.0),
    txn("80331", "Munich", 500.0),
    txn("80331", "Munich", 500.0),
    txn("99999", "Nowhere", 50.0),          # unknown city -> no coordinates
    txn("10115", "Berlin", 9999.0, status="Failed"),  # failed -> excluded
    {"customer_reference_id": "Y", "amount": 42.0, "status": "Success", "extra": {}},  # no geo -> skipped
]


def make_agent(tmp_path, client):
    gov = GovernanceMiddleware(client, audit_log_path=str(tmp_path / "audit.jsonl"))
    return MarketingAgent(gov), gov


# ---- deterministic aggregation ----------------------------------------------

def test_aggregate_sums_value_and_count_per_postal_and_excludes_failed():
    aggs = aggregate_by_postal(TXNS)
    by_pc = {a["postal_code"]: a for a in aggs}
    assert by_pc["10115"]["transaction_count"] == 3          # failed row excluded
    assert by_pc["10115"]["total_value"] == 6000.0
    assert by_pc["80331"]["total_value"] == 1000.0
    # records with no postal code are dropped entirely
    assert set(by_pc) == {"10115", "80331", "99999"}


def test_aggregate_assigns_real_coordinates_for_known_cities():
    aggs = {a["postal_code"]: a for a in aggregate_by_postal(TXNS)}
    assert aggs["10115"]["lat"], aggs["10115"]["lon"] == CITY_COORDS["Berlin"]
    assert aggs["99999"]["lat"] is None  # unknown city -> no coords


def test_aggregate_sorted_by_value_desc():
    aggs = aggregate_by_postal(TXNS)
    values = [a["total_value"] for a in aggs]
    assert values == sorted(values, reverse=True)


def test_prompt_lists_every_postal_code():
    prompt = build_prompt(aggregate_by_postal(TXNS))
    assert "10115" in prompt and "80331" in prompt
    assert "POSTAL <postal_code>" in prompt  # output-format instruction present


def test_parse_annotations_reads_well_formed_lines():
    text = ("POSTAL 10115: opportunity | Strong Berlin activity worth deepening.\n"
            "POSTAL 80331: stable | Steady Munich base, no action needed.\n"
            "garbage line that should be ignored")
    parsed = parse_annotations(text)
    assert parsed["10115"] == ("opportunity", "Strong Berlin activity worth deepening.")
    assert parsed["80331"][0] == "stable"
    assert "garbage" not in "".join(parsed.keys())


# ---- end-to-end via governance (mock + fake providers) ----------------------

def test_assess_uses_llm_labels_when_well_formed(tmp_path):
    canned = ("POSTAL 10115: opportunity | Strong Berlin activity.\n"
              "POSTAL 80331: watch | Thin Munich activity.\n"
              "POSTAL 99999: stable | Minor outlying activity.")
    agent, _ = make_agent(tmp_path, FakeMarketingLLMClient(canned))
    result = agent.assess(TXNS, verbose=False)
    assert result["llm_available"] is True
    assert result["parse_failed"] is False
    berlin = next(h for h in result["heatmap"] if h["postal_code"] == "10115")
    assert berlin["label"] == "opportunity"
    assert berlin["narrative"] == "Strong Berlin activity."


def test_assess_falls_back_to_deterministic_labels_on_generic_model(tmp_path):
    # MockLlamaClient doesn't emit POSTAL lines -> parser finds nothing ->
    # every postal code still gets a deterministic label + narrative.
    agent, _ = make_agent(tmp_path, MockLlamaClient(simulated_latency_ms=1))
    result = agent.assess(TXNS, verbose=False)
    assert result["parse_failed"] is True
    assert len(result["heatmap"]) == 3
    assert all(h["label"] in {"opportunity", "watch", "stable"} for h in result["heatmap"])
    assert all(h["narrative"] for h in result["heatmap"])


def test_assess_handles_no_geolocated_transactions(tmp_path):
    agent, gov = make_agent(tmp_path, MockLlamaClient(simulated_latency_ms=1))
    result = agent.assess([{"customer_reference_id": "Z", "amount": 5.0,
                            "status": "Success", "extra": {}}], verbose=False)
    assert result["heatmap"] == []
    assert gov.run_summary("marketing_agent")["calls_made"] == 0  # no prompt sent


# ---- fail-closed: PII detected upstream -> no LLM call for Marketing --------

def test_assess_pii_blocked_skips_llm_but_still_annotates(tmp_path):
    """When pii_blocked=True the Marketing LLM must NOT be called, yet every
postal code is still deterministically annotated and the block is written to
"""

    client = FakeMarketingLLMClient("POSTAL 10115: opportunity | should never be used")
    agent, gov = make_agent(tmp_path, client)
    result = agent.assess(TXNS, verbose=False, pii_blocked=True)

    assert result["pii_blocked"] is True
    assert result["llm_available"] is False
    # the LLM client was never invoked...
    assert client.calls == 0
    assert gov.run_summary("marketing_agent")["calls_made"] == 0
    # ...but the heatmap is still fully populated, deterministically
    assert len(result["heatmap"]) == 3
    assert all(h["label"] in {"opportunity", "watch", "stable"} for h in result["heatmap"])
    assert all(h["narrative"] for h in result["heatmap"])
    # ...and the block is on the immutable audit trail, attributed to marketing
    audit = gov.audit_store.all()
    assert any(r["agent"] == "marketing_agent" and r["stop_reason"] == "pii_detected_blocked"
               for r in audit)


def test_run_pii_blocked_writes_flag_into_heatmap_file(tmp_path):
    root = str(tmp_path / "bucket_output")
    prefix = os.path.join(root, "gym-chain", "2026-07-23")
    os.makedirs(prefix)
    with open(os.path.join(prefix, "normalized_transactions.json"), "w") as f:
        json.dump(TXNS, f)

    client = FakeMarketingLLMClient("POSTAL 10115: opportunity | unused")
    agent, _ = make_agent(tmp_path, client)
    summary = agent.run(root, "gym-chain", "2026-07-23", verbose=False, pii_blocked=True)
    assert summary["pii_blocked"] is True
    assert summary["llm_available"] is False
    assert client.calls == 0
    with open(summary["output"]) as f:
        payload = json.load(f)
    assert payload["pii_blocked"] is True
    assert payload["postal_code_count"] == 3


def test_run_writes_heatmap_file(tmp_path):
    root = str(tmp_path / "bucket_output")
    prefix = os.path.join(root, "gym-chain", "2026-07-23")
    os.makedirs(prefix)
    with open(os.path.join(prefix, "normalized_transactions.json"), "w") as f:
        json.dump(TXNS, f)

    agent, _ = make_agent(tmp_path, MockLlamaClient(simulated_latency_ms=1))
    summary = agent.run(root, "gym-chain", "2026-07-23", verbose=False)
    assert summary["postal_code_count"] == 3
    with open(summary["output"]) as f:
        payload = json.load(f)
    entry = payload["heatmap"][0]
    for field in ("postal_code", "city", "lat", "lon", "transaction_count",
                  "total_value", "label", "narrative"):
        assert field in entry
