"""Unit tests for weekly_reminders.py. Run with: pytest test_weekly_reminders.py -v

The bank has three business customers, so a run sends AT MOST ONE aggregate
"""


import json
import os

import pytest
import weekly_reminders
from contact_crypto import encrypt_email
from email_client import InMemoryEmailNotifier


def _write_bucket(root, customer_id, run_date, *, transactions, stats, flags=None):
    prefix = os.path.join(root, customer_id, run_date)
    os.makedirs(prefix, exist_ok=True)
    with open(os.path.join(prefix, "normalized_transactions.json"), "w") as f:
        json.dump(transactions, f)
    with open(os.path.join(prefix, "historical_stats.json"), "w") as f:
        json.dump(stats, f)
    if flags is not None:
        with open(os.path.join(prefix, "inflow_flags.json"), "w") as f:
            json.dump(flags, f)
    return prefix


def txn(cust_id, name, dt, amount, status="Success"):
    return {
        "counterparty_name": name,
        "customer_reference_id": cust_id,
        "reference_id": f"E2E-{dt}",
        "transaction_date": dt,
        "amount": amount,
        "status": status,
    }


def _stat(count, avg, last="2026-05-26"):
    return {
        "transaction_count": count,
        "rolling_3m_avg": avg if count >= 3 else None,
        "rolling_6m_avg": avg if count >= 3 else None,
        "rolling_12m_avg": avg if count >= 3 else None,
        "overall_avg": avg,
        "last_transaction_date": last,
    }


@pytest.fixture
def bucket(tmp_path):
    """Two eligible counterparties (CUST-1 @1000, CUST-3 @500) + one cold-start
(CUST-2, 1 txn) that must be excluded. Both eligible ones are also flagged
"""

    root = str(tmp_path / "bucket_output")
    transactions = [
        txn("CUST-1", "Test Hospital One", "2026-05-05", 1000.0),
        txn("CUST-1", "Test Hospital One", "2026-05-12", 1000.0),
        txn("CUST-1", "Test Hospital One", "2026-05-19", 1000.0),
        txn("CUST-1", "Test Hospital One", "2026-05-26", 1000.0),
        txn("CUST-3", "Test Hospital Three", "2026-05-05", 500.0),
        txn("CUST-3", "Test Hospital Three", "2026-05-12", 500.0),
        txn("CUST-3", "Test Hospital Three", "2026-05-19", 500.0),
        txn("CUST-2", "Test Hospital Two (cold start)", "2026-06-01", 500.0),  # only 1 txn
    ]
    stats = {
        "CUST-1": _stat(4, 1000.0),
        "CUST-3": _stat(3, 500.0),
        "CUST-2": _stat(1, 500.0),  # below MIN_TRANSACTIONS_FOR_BASELINE -- excluded
    }
    flags = {
        "customer_id": "test-tenant",
        "run_date": "2026-07-15",
        "as_of": "2026-07-15",
        "missing_payments": [
            {"customer_reference_id": "CUST-1", "type": "missing_payment",
             "last_transaction_date": "2026-05-26", "typical_cadence_days": 7,
             "expected_by": "2026-06-02", "grace_period_ends": "2026-06-08", "days_overdue": 3},
            {"customer_reference_id": "CUST-3", "type": "missing_payment",
             "last_transaction_date": "2026-05-19", "typical_cadence_days": 7,
             "expected_by": "2026-05-26", "grace_period_ends": "2026-06-01", "days_overdue": 10},
        ],
        "high_value_transactions": [],
        "deviations": [],
        "explicit_failures": [],
    }
    _write_bucket(root, "test-tenant", "2026-07-15", transactions=transactions, stats=stats, flags=flags)
    return root


def _run_monday(bucket, notifier, **kw):
    return weekly_reminders.run_monday(
        bucket_root=bucket, customer_id="test-tenant", run_date="2026-07-15",
        tenant_name="Test Tenant Inc", from_address="reports@kapitalwerknxt.bank",
        notifier=notifier, **kw,
    )


def _run_friday(bucket, notifier, **kw):
    return weekly_reminders.run_friday(
        bucket_root=bucket, customer_id="test-tenant", run_date="2026-07-15",
        tenant_name="Test Tenant Inc", from_address="reports@kapitalwerknxt.bank",
        notifier=notifier, **kw,
    )


def test_monday_sends_exactly_one_aggregate_email(bucket):
    notifier = InMemoryEmailNotifier()
    results = _run_monday(bucket, notifier, recipient_override="demo-inbox@example.com")

    # ONE record for the whole persona, not one per counterparty
    assert len(results) == 1
    rec = results[0]
    assert rec["status"] == "sent"
    assert rec["customer_id"] == "test-tenant"
    # aggregate total == sum of the two eligible counterparties (cold-start excluded)
    assert rec["total_amount"] == 1500.0
    assert rec["counterparty_count"] == 2

    assert len(notifier.sent) == 1
    sent = notifier.sent[0]
    assert sent.to == "demo-inbox@example.com"
    assert "Test Tenant Inc" in sent.subject
    # breakdown table lists every contributing counterparty
    assert "Test Hospital One" in sent.html
    assert "Test Hospital Three" in sent.html


def test_monday_aggregate_total_equals_sum_of_contributors(bucket):
    notifier = InMemoryEmailNotifier()
    results = _run_monday(bucket, notifier, recipient_override="demo-inbox@example.com")
    breakdown = results[0]["breakdown_top"]
    assert results[0]["total_amount"] == sum(r["amount"] for r in breakdown) == 1500.0


def test_friday_sends_one_aggregate_missed_email_sorted_by_overdue(bucket):
    notifier = InMemoryEmailNotifier()
    results = _run_friday(bucket, notifier, recipient_override="demo-inbox@example.com")

    assert len(results) == 1
    rec = results[0]
    assert rec["status"] == "sent"
    assert rec["counterparty_count"] == 2
    assert rec["total_amount"] == 1500.0
    # sorted by days_overdue desc -> CUST-3 (10 days) first
    assert rec["breakdown_top"][0]["days_overdue"] == 10

    sent = notifier.sent[0]
    assert "not yet received" in sent.subject
    assert "PAYMENT REMINDER" in sent.html


def test_no_recipient_means_skip_not_guess(bucket):
    """With no contact_directory and no recipient_override, nothing is sent --
but there IS something to report, so one skipped record is surfaced.
"""

    notifier = InMemoryEmailNotifier()
    results = _run_monday(bucket, notifier)
    assert len(notifier.sent) == 0
    assert len(results) == 1
    assert results[0]["status"] == "skipped_no_recipient"


def test_contact_directory_record_resolves_recipient_and_owner(bucket, tmp_path):
    """A persona record carries the address ENCRYPTED (email_encrypted) plus a
masked display form -- never plaintext. run_monday must decrypt it only at
"""

    notifier = InMemoryEmailNotifier()
    real = "real-contact@example.com"
    record = {
        "customer_id": "test-tenant",
        "owner_name": "Claire",
        "email_masked": "r**********t@example.com",
        "email_encrypted": encrypt_email(real),
    }
    results = _run_monday(bucket, notifier, contact_directory=record)
    assert results[0]["status"] == "sent"
    # real (decrypted) address used for the actual send...
    assert notifier.sent[0].to == real
    # formal generic salutation (EU bank convention), not first-name-casual
    assert "Dear Customer," in notifier.sent[0].body

    # ...but only the MASKED form is persisted; plaintext appears nowhere in the log
    log_path = os.path.join(bucket, "test-tenant", "2026-07-15", "reminders_sent_monday_upcoming.json")
    assert results[0]["to_address_masked"] == "r**********t@example.com"
    with open(log_path) as f:
        raw = f.read()
    assert real not in raw
    assert "to_address_masked" in raw and '"to_address"' not in raw


def test_send_log_persists_masked_address_only_never_plaintext(bucket):
    """The reminders_sent_*.json artifact is broadly readable (a future UI /
export reads it), so it must carry to_address_masked and NOT the plaintext
"""

    notifier = InMemoryEmailNotifier()
    _run_friday(bucket, notifier, recipient_override="demo-inbox@example.com")
    log_path = os.path.join(bucket, "test-tenant", "2026-07-15", "reminders_sent_friday_missed.json")
    with open(log_path) as f:
        raw = f.read()
        f.seek(0)
        logged = json.load(f)
    assert logged["record"]["to_address_masked"] == "d********x@example.com"
    assert "demo-inbox@example.com" not in raw
    assert '"to_address"' not in raw


def test_zero_counterparties_sends_no_email_without_erroring(tmp_path):
    """A persona where every counterparty is cold-start: no email, no crash.
"""
    root = str(tmp_path / "bucket_output")
    _write_bucket(
        root, "test-tenant", "2026-07-15",
        transactions=[txn("CUST-9", "Rookie", "2026-06-01", 10.0)],
        stats={"CUST-9": _stat(1, 10.0)},  # below MIN
    )
    notifier = InMemoryEmailNotifier()
    results = _run_monday(root, notifier, recipient_override="demo-inbox@example.com")
    assert results == []
    assert len(notifier.sent) == 0
    # log still written, recording zero emails
    log_path = os.path.join(root, "test-tenant", "2026-07-15", "reminders_sent_monday_upcoming.json")
    with open(log_path) as f:
        logged = json.load(f)
    assert logged["email_count"] == 0


def test_breakdown_is_capped_with_plus_n_more(tmp_path):
    """A persona with more than MAX_BREAKDOWN_ROWS counterparties gets a capped
breakdown table (top N + a '+N more' row), but the aggregate total still
"""

    root = str(tmp_path / "bucket_output")
    n = 15  # > MAX_BREAKDOWN_ROWS (10)
    transactions = []
    stats = {}
    for i in range(n):
        cid = f"CUST-{i:03d}"
        amount = float(100 + i)  # distinct amounts so ordering is deterministic
        for w in range(3):
            transactions.append(txn(cid, f"Hospital {i:03d}", f"2026-05-0{w+1}", amount))
        stats[cid] = _stat(3, amount)
    _write_bucket(root, "test-tenant", "2026-07-15", transactions=transactions, stats=stats)

    notifier = InMemoryEmailNotifier()
    results = _run_monday(root, notifier, recipient_override="demo-inbox@example.com")

    rec = results[0]
    assert rec["counterparty_count"] == n
    assert rec["total_amount"] == sum(100 + i for i in range(n))
    # the email shows a capped table with a "+5 more" collapse row
    assert "+5 more counterparties" in notifier.sent[0].html
    # highest amount (114) appears; the smallest few are collapsed
    assert "Hospital 014" in notifier.sent[0].html


def test_send_log_is_one_record_per_persona(bucket):
    notifier = InMemoryEmailNotifier()
    _run_monday(bucket, notifier, recipient_override="demo-inbox@example.com")
    log_path = os.path.join(bucket, "test-tenant", "2026-07-15", "reminders_sent_monday_upcoming.json")
    assert os.path.exists(log_path)
    with open(log_path) as f:
        logged = json.load(f)
    assert logged["kind"] == "monday_upcoming"
    assert logged["email_count"] == 1
    # a single record dict, NOT a list keyed by counterparty
    assert isinstance(logged["record"], dict)
    assert logged["record"]["status"] == "sent"
    assert logged["record"]["counterparty_count"] == 2
