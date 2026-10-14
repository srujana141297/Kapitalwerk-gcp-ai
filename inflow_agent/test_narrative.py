"""Tests for the template-based narrative module. No LLM involved -- these
assert exact substring content, which only works because the text is
generated deterministically from the input numbers.
"""


from narrative import monday_narrative, friday_narrative


def test_monday_narrative_includes_count_and_total():
    expected = {"CUST-1": 1000.0, "CUST-2": 2000.0}
    text = monday_narrative(expected)
    assert "2 counterparties" in text
    assert "3,000.00" in text


def test_friday_narrative_reports_shortfall():
    report = {
        "total_expected": 10000.0,
        "total_actual": 9000.0,
        "inflow_deviation_pct": -10.0,
        "missed_payments": [{"hospital_name": "Klinikum Test", "expected_amount": 1000.0}],
        "missed_count": 1,
        "cumulative_amount_period_to_date": 50000.0,
    }
    text = friday_narrative(report)
    assert "10.0% lower" in text
    assert "Klinikum Test" in text
    assert "1 expected payment(s)" in text
    assert "50,000.00" in text


def test_friday_narrative_reports_surplus():
    report = {
        "total_expected": 10000.0,
        "total_actual": 11000.0,
        "inflow_deviation_pct": 10.0,
        "missed_payments": [],
        "missed_count": 0,
        "cumulative_amount_period_to_date": 50000.0,
    }
    text = friday_narrative(report)
    assert "10.0% higher" in text
    assert "All expected counterparties paid this week." in text


def test_friday_narrative_handles_no_baseline():
    report = {
        "total_expected": 0.0,
        "total_actual": 0.0,
        "inflow_deviation_pct": None,
        "missed_payments": [],
        "missed_count": 0,
        "cumulative_amount_period_to_date": 0.0,
    }
    text = friday_narrative(report)
    assert "No expected-inflow baseline" in text
