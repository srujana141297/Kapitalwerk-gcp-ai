"""Unit tests for reminder email content + rendering. Run with:
pytest test_templates.py -v
"""


import templates


def test_upcoming_payment_email_data_shape():
    data = templates.build_upcoming_payment_email(
        hospital_name="Charite - Universitaetsmedizin Berlin",
        tenant_name="MedSupply Direct",
        amount=19392.55,
        cadence_days=7,
        last_transaction_date="2026-06-26",
        trace="trc-rep-cust-hosp-1001-upcoming",
        from_address="reports@kapitalwerknxt.bank",
        to_address="finance@example.com",
    )
    assert data["subject"] == "Your upcoming payment to MedSupply Direct"
    assert data["box_amount"] == "€19,392.55"
    assert data["badge_text"] == "UPCOMING PAYMENT"
    # next expected = last_transaction_date + cadence_days
    assert any(r["label"] == "Expected date" and r["value"] == "Jul 3, 2026" for r in data["detail_rows"])
    assert data["to_address"] == "finance@example.com"


def test_missed_payment_email_data_shape():
    data = templates.build_missed_payment_email(
        hospital_name="Charite - Universitaetsmedizin Berlin",
        tenant_name="MedSupply Direct",
        amount=19392.55,
        cadence_days=7,
        expected_by="2026-07-03",
        grace_period_ends="2026-07-09",
        trace="trc-rep-cust-hosp-1001-missed",
        from_address="reports@kapitalwerknxt.bank",
        to_address="finance@example.com",
    )
    assert "hasn't been received" in data["subject"]
    assert data["badge_text"] == "PAYMENT REMINDER"
    assert data["box_amount"] == "€19,392.55"
    assert any(r["label"] == "Grace period ended" and r["value"] == "Jul 9, 2026" for r in data["detail_rows"])


def test_render_html_substitutes_every_placeholder():
    data = templates.build_missed_payment_email(
        hospital_name="Test Hospital",
        tenant_name="MedSupply Direct",
        amount=1000.0,
        cadence_days=7,
        expected_by="2026-07-03",
        grace_period_ends="2026-07-09",
        trace="trc-test",
        from_address="reports@kapitalwerknxt.bank",
        to_address="finance@example.com",
    )
    html = templates.render_html(data)
    assert "${" not in html, "unsubstituted placeholder left in rendered HTML"
    assert "Test Hospital" in html
    assert "€1,000.00" in html
    assert "trc-test" in html
    assert f'cid:{templates.LOGO_CID}' in html


def test_render_text_has_no_html_and_includes_amount():
    data = templates.build_upcoming_payment_email(
        hospital_name="Test Hospital",
        tenant_name="MedSupply Direct",
        amount=500.5,
        cadence_days=14,
        last_transaction_date="2026-07-01",
        trace="trc-test-2",
        from_address="reports@kapitalwerknxt.bank",
        to_address="finance@example.com",
    )
    text = templates.render_text(data)
    assert "${" not in text
    assert "<div" not in text
    assert "€500.50" in text
    assert "trc-test-2" in text


def test_weekly_upcoming_summary_aggregates_and_greets_owner():
    data = templates.build_weekly_upcoming_summary_email(
        owner_name="Claire",
        tenant_name="MedSupply Direct",
        contributors=[
            {"name": "Charite Berlin", "amount": 9000.0},
            {"name": "Uniklinik Koln", "amount": 1000.0},
        ],
        trace="trc-rep-glucometer-vendor-weekly-upcoming-2026-07-16",
        from_address="reports@kapitalwerknxt.bank",
        to_address="anu.shree@neversaydie.co.in",
    )
    # box shows the aggregate total, not any single counterparty amount
    assert data["box_amount"] == "€10,000.00"
    assert data["greeting"] == "Dear Customer,"
    assert data["badge_text"] == "WEEKLY SUMMARY"
    # breakdown sorted descending by amount
    assert data["detail_rows"][0]["label"] == "Charite Berlin"


def test_weekly_missed_summary_sorts_by_overdue_and_totals():
    data = templates.build_weekly_missed_summary_email(
        owner_name="Mark",
        tenant_name="Gym Chain",
        missed=[
            {"name": "Gym Berlin", "amount": 30.0, "days_overdue": 2},
            {"name": "Gym Munich", "amount": 50.0, "days_overdue": 9},
        ],
        trace="trc-test",
        from_address="reports@kapitalwerknxt.bank",
        to_address="anu.shree@neversaydie.co.in",
    )
    assert data["greeting"] == "Dear Customer,"
    assert data["box_amount"] == "€80.00"
    # most overdue first
    assert data["detail_rows"][0]["label"] == "Gym Munich"
    assert "9d overdue" in data["detail_rows"][0]["value"]


def test_weekly_summary_breakdown_is_capped():
    contributors = [{"name": f"Payer {i:03d}", "amount": float(i + 1)} for i in range(25)]
    data = templates.build_weekly_upcoming_summary_email(
        owner_name="Arya", tenant_name="Boho House Club", contributors=contributors,
        trace="trc-test", from_address="a@b.c", to_address="d@e.f",
    )
    rows = data["detail_rows"]
    # MAX_BREAKDOWN_ROWS individual rows + one collapse row
    assert len(rows) == templates.MAX_BREAKDOWN_ROWS + 1
    assert rows[-1]["label"] == f"+{25 - templates.MAX_BREAKDOWN_ROWS} more counterparties"
    # aggregate total still reflects all 25
    assert data["box_amount"] == templates.fmt_eur(sum(i + 1 for i in range(25)))


def test_weekly_summary_renders_with_no_unsubstituted_placeholders():
    data = templates.build_weekly_missed_summary_email(
        owner_name="Claire", tenant_name="MedSupply Direct",
        missed=[{"name": "Charite", "amount": 9000.0, "days_overdue": 4}],
        trace="trc-render", from_address="a@b.c", to_address="d@e.f",
    )
    html = templates.render_html(data)
    text = templates.render_text(data)
    assert "${" not in html and "${" not in text
    assert "Dear Customer," in text
    assert "€9,000.00" in html


def test_same_template_different_data_produces_different_output():
    """The core 'Go template' property: one template, many data payloads.
"""
    base = dict(
        tenant_name="MedSupply Direct",
        amount=100.0,
        cadence_days=7,
        last_transaction_date="2026-07-01",
        from_address="reports@kapitalwerknxt.bank",
        to_address="a@example.com",
    )
    a = templates.render_html(templates.build_upcoming_payment_email(hospital_name="Hospital A", trace="trc-a", **base))
    b = templates.render_html(templates.build_upcoming_payment_email(hospital_name="Hospital B", trace="trc-b", **{**base, "to_address": "b@example.com"}))
    assert "Hospital A" in a and "Hospital A" not in b
    assert "Hospital B" in b and "Hospital B" not in a
    # everything else (layout, colors, labels) is identical
    assert a.split("Hospital A")[0] == b.split("Hospital B")[0]
