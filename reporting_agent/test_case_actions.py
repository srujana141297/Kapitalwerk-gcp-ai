"""Unit tests for case_actions.py -- the write-capable case-review action store.
Run with: pytest test_case_actions.py -v. All writes go to a tmp_path file, so
the real bucket_output/_case_actions store is never touched.
"""


import pytest

import case_actions


@pytest.fixture
def store(tmp_path):
    return str(tmp_path / "case_actions.json")


def test_load_missing_action_is_none(store):
    assert case_actions.load_case_action("gym-chain", "AML-9999-abcd", path=store) is None


def test_save_and_load_acknowledged(store):
    rec = case_actions.save_case_action("gym-chain", "AML-1002-a8eb", "acknowledged", path=store)
    assert rec["status"] == "acknowledged"
    assert rec["escalation"] is None
    assert rec["reviewed_at"]
    loaded = case_actions.load_case_action("gym-chain", "AML-1002-a8eb", path=store)
    assert loaded == rec


def test_save_escalated_persists_escalation_payload(store):
    esc = {"ran": True, "risk_tier": "high", "risk_reasoning": "3.61x baseline",
           "aml_case_narrative": "SAR narrative text.", "aml_trace_id": "trc-xyz"}
    case_actions.save_case_action("boho-house", "AML-0002-d44f", "escalated", escalation=esc, path=store)
    loaded = case_actions.load_case_action("boho-house", "AML-0002-d44f", path=store)
    assert loaded["status"] == "escalated"
    assert loaded["escalation"]["aml_case_narrative"] == "SAR narrative text."
    assert loaded["escalation"]["aml_trace_id"] == "trc-xyz"


def test_re_escalation_overwrites_prior_record(store):
    case_actions.save_case_action("boho-house", "AML-0002-d44f", "escalated",
                                  escalation={"aml_case_narrative": "first"}, path=store)
    case_actions.save_case_action("boho-house", "AML-0002-d44f", "escalated",
                                  escalation={"aml_case_narrative": "second"}, path=store)
    loaded = case_actions.load_case_action("boho-house", "AML-0002-d44f", path=store)
    assert loaded["escalation"]["aml_case_narrative"] == "second"
    # still exactly one record for that (persona, case_id)
    assert len(case_actions.all_case_actions("boho-house", path=store)) == 1


def test_same_case_id_different_personas_do_not_collide(store):
    # both personas can legitimately share an id like AML-0002-...
    case_actions.save_case_action("boho-house", "AML-0002-d44f", "acknowledged", path=store)
    case_actions.save_case_action("gym-chain", "AML-0002-d44f", "escalated",
                                  escalation={"aml_case_narrative": "n"}, path=store)
    assert case_actions.load_case_action("boho-house", "AML-0002-d44f", path=store)["status"] == "acknowledged"
    assert case_actions.load_case_action("gym-chain", "AML-0002-d44f", path=store)["status"] == "escalated"


def test_all_case_actions_filters_by_persona(store):
    case_actions.save_case_action("gym-chain", "AML-1", "acknowledged", path=store)
    case_actions.save_case_action("gym-chain", "AML-2", "acknowledged", path=store)
    case_actions.save_case_action("boho-house", "AML-3", "acknowledged", path=store)
    assert len(case_actions.all_case_actions(path=store)) == 3
    assert len(case_actions.all_case_actions("gym-chain", path=store)) == 2


def test_invalid_status_rejected(store):
    with pytest.raises(ValueError):
        case_actions.save_case_action("gym-chain", "AML-1", "bogus", path=store)
