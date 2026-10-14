"""Tests for the read-only API, run against the REAL bucket_output snapshots
already committed in this repo (glucometer-vendor / gym-chain / boho-house all
have real data). Uses FastAPI's TestClient -- no server process needed.
"""


import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import app  # noqa: E402
import case_actions  # noqa: E402  (on path via main's sys.path setup)

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_case_action_store(tmp_path, monkeypatch):
    """Redirect the case-action store to a throwaway file for EVERY test, so the
acknowledge/escalate write tests never mutate the committed
"""

    store = str(tmp_path / "case_actions.json")
    monkeypatch.setattr(case_actions, "DEFAULT_CASE_ACTIONS_PATH", store)
    return store


def _first_case_id(persona: str) -> str:
    """A real case_id from the persona's latest snapshot.
"""
    r = client.get(f"/api/{persona}/cases")
    assert r.status_code == 200
    cases = r.json()
    assert cases, f"no cases for {persona}"
    return cases[0]["case_id"]

# The plaintext demo email that must NEVER leak through any read endpoint --
# mirrors the PII-masking discipline in reporting_agent/contact_store.py.
PLAINTEXT_EMAIL = "anu.shree@neversaydie.co.in"


def test_personas_returns_exactly_three_with_owner_names():
    r = client.get("/api/personas")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 3
    by_id = {p["customer_id"]: p for p in data}
    assert set(by_id) == {"glucometer-vendor", "gym-chain", "boho-house"}
    assert by_id["glucometer-vendor"]["owner_name"] == "Claire"
    assert by_id["gym-chain"]["owner_name"] == "Mark"
    assert by_id["boho-house"]["owner_name"] == "Arya"
    # tenant names come through too
    assert by_id["glucometer-vendor"]["tenant_name"] == "MedSupply Direct"


def test_counterparties_shape_and_cap():
    r = client.get("/api/glucometer-vendor/counterparties")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    assert len(rows) <= 10  # capped at 10
    if rows:
        row = rows[0]
        assert "customer_reference_id" in row
        assert "total_value" in row
        assert "transaction_count" in row
        # sorted descending by value
        values = [x["total_value"] for x in rows]
        assert values == sorted(values, reverse=True)


def test_counterparties_with_explicit_run_date():
    r = client.get("/api/boho-house/counterparties", params={"run_date": "2026-07-16"})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_cases_awaiting_review():
    r = client.get("/api/glucometer-vendor/cases")
    assert r.status_code == 200
    cases = r.json()
    assert isinstance(cases, list)
    assert len(cases) <= 10
    for c in cases:
        assert c.get("status") == "awaiting_review"


def test_notifications_default_today():
    r = client.get("/api/glucometer-vendor/notifications")
    assert r.status_code == 200
    notes = r.json()
    assert isinstance(notes, list)
    assert len(notes) <= 10


def test_marketing_heatmap():
    r = client.get("/api/boho-house/marketing-heatmap", params={"run_date": "2026-07-16"})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_dashboard_bundles_everything():
    r = client.get("/api/glucometer-vendor/dashboard")
    assert r.status_code == 200
    data = r.json()
    for key in (
        "persona",
        "run_date",
        "top_counterparties",
        "cases_awaiting_review",
        "recent_notifications",
        "marketing_heatmap",
        "governance_audit_summary",
    ):
        assert key in data
    assert data["persona"] == "glucometer-vendor"


def test_governance_audit_summary_not_persona_scoped():
    r = client.get("/api/governance/audit-summary")
    assert r.status_code == 200
    data = r.json()
    assert "total_events" in data
    assert "by_agent" in data
    assert "by_stop_reason" in data
    assert "pii_blocks" in data


def test_unknown_persona_is_404():
    r = client.get("/api/unknown-persona/counterparties")
    assert r.status_code == 404
    assert "Unknown persona" in r.json()["detail"]


def test_reminder_preview_is_masked_never_plaintext():
    r = client.get("/api/glucometer-vendor/reminder-preview")
    assert r.status_code == 200
    data = r.json()
    assert data["owner_name"] == "Claire"
    assert data["tenant_name"] == "MedSupply Direct"
    # masked address present...
    assert data["to_address_masked"] == "a*******e@neversaydie.co.in"
    # ...and the raw plaintext email must NOT appear ANYWHERE in the response
    assert PLAINTEXT_EMAIL not in r.text
    # a real, sensible subject line
    assert "MedSupply Direct" in data["subject_example"]


def test_reminder_preview_unknown_persona_404():
    r = client.get("/api/nope/reminder-preview")
    assert r.status_code == 404


@pytest.mark.parametrize("persona", ["glucometer-vendor", "gym-chain", "boho-house"])
def test_escalate_runs_risk_and_aml_and_persists(persona):
    case_id = _first_case_id(persona)
    r = client.post(f"/api/{persona}/cases/{case_id}/escalate")
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["status"] == "escalated"
    esc = rec["escalation"]
    assert esc["ran"] is True
    # a real, non-empty, non-placeholder AML narrative from the mock LLM client
    narrative = esc["aml_case_narrative"]
    assert isinstance(narrative, str) and len(narrative) > 40
    assert "compliance review" in narrative.lower()
    assert esc["risk_tier"] in ("low", "medium", "high")
    assert esc["aml_trace_id"]

    # a follow-up GET now reflects the escalation (status + narrative persisted)
    r2 = client.get(f"/api/{persona}/cases")
    assert r2.status_code == 200
    match = next(c for c in r2.json() if c["case_id"] == case_id)
    assert match["status"] == "escalated"
    assert match["escalation"]["aml_case_narrative"] == narrative


def test_acknowledge_persists_status_without_llm():
    persona = "gym-chain"
    case_id = _first_case_id(persona)
    r = client.post(f"/api/{persona}/cases/{case_id}/acknowledge")
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["status"] == "acknowledged"
    assert rec["escalation"] is None
    # reflected on the next read
    r2 = client.get(f"/api/{persona}/cases")
    match = next(c for c in r2.json() if c["case_id"] == case_id)
    assert match["status"] == "acknowledged"


def test_escalate_unknown_case_id_is_404():
    r = client.post("/api/gym-chain/cases/AML-does-not-exist/escalate")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_acknowledge_unknown_case_id_is_404():
    r = client.post("/api/gym-chain/cases/AML-nope-9999/acknowledge")
    assert r.status_code == 404


def test_escalate_unknown_persona_is_404():
    r = client.post("/api/not-a-persona/cases/AML-1/escalate")
    assert r.status_code == 404
    assert "Unknown persona" in r.json()["detail"]


def test_cors_allows_null_origin():
    # file:// documents send `Origin: null`; allow_origins=["*"] must echo "*".
    r = client.get("/api/personas", headers={"Origin": "null"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "*"
