"""Unit tests for contact_store.py. Run with: pytest test_contact_store.py -v

The contact directory is PERSONA-LEVEL: exactly one record per business
"""


import json
import os

from contact_store import (
    DEMO_CONTACT_EMAIL,
    PERSONA_OWNERS,
    build_contact_directory_file,
    load_contact_directory,
    resolve_send_email,
    _mask_email,
)
from contact_crypto import decrypt_email
import weekly_reminders
from email_client import InMemoryEmailNotifier


def test_build_writes_exactly_one_record_per_persona(tmp_path):
    out = str(tmp_path / "contacts.json")
    build_contact_directory_file(out_path=out)
    with open(out) as f:
        data = json.load(f)

    # exactly three records -- one per real business customer, not per payer
    assert len(data["records"]) == 3
    by_id = {r["customer_id"]: r for r in data["records"]}
    assert set(by_id) == {"glucometer-vendor", "gym-chain", "boho-house"}
    assert by_id["glucometer-vendor"]["owner_name"] == "Claire"
    assert by_id["gym-chain"]["owner_name"] == "Mark"
    assert by_id["boho-house"]["owner_name"] == "Arya"

    # NO plaintext email is ever written to disk -- neither an `email` field
    # on any record, nor the raw address anywhere in the file's raw text.
    assert all("email" not in r for r in data["records"])
    with open(out) as f:
        raw = f.read()
    assert DEMO_CONTACT_EMAIL not in raw

    # every record carries an encrypted address that round-trips back to the
    # single safe demo inbox (via the REAL crypto functions, not a mock)
    assert all("email_encrypted" in r for r in data["records"])
    assert all(decrypt_email(r["email_encrypted"]) == DEMO_CONTACT_EMAIL for r in data["records"])
    # and carries a production-shaped masked display + tenant_name
    rec = by_id["glucometer-vendor"]
    assert rec["tenant_name"] == "MedSupply Direct"
    assert rec["email_masked"] == _mask_email(DEMO_CONTACT_EMAIL)
    assert rec["email_masked"] != DEMO_CONTACT_EMAIL and "@" in rec["email_masked"]
    assert rec["source"] == "demo_contact_store" and "updated_at" in rec


def test_build_uses_persona_owners_as_source_of_truth(tmp_path):
    """The directory does not scan bucket_output for counterparties -- it is
driven by PERSONA_OWNERS. A custom subset produces exactly that subset.
"""

    out = str(tmp_path / "contacts.json")
    build_contact_directory_file(out_path=out, personas={"gym-chain": "Mark"})
    directory = load_contact_directory(path=out)
    assert set(directory) == {"gym-chain"}
    # and the default really is the three canonical personas
    assert set(PERSONA_OWNERS) == {"glucometer-vendor", "gym-chain", "boho-house"}


def test_load_with_customer_id_returns_single_persona_record(tmp_path):
    out = str(tmp_path / "contacts.json")
    build_contact_directory_file(out_path=out)
    rec = load_contact_directory(path=out, customer_id="glucometer-vendor")
    assert rec["owner_name"] == "Claire"
    # no plaintext email loaded; only the encrypted form, which round-trips
    assert "email" not in rec
    assert decrypt_email(rec["email_encrypted"]) == DEMO_CONTACT_EMAIL
    assert rec["customer_id"] == "glucometer-vendor"


def test_load_without_customer_id_returns_all_personas(tmp_path):
    out = str(tmp_path / "contacts.json")
    build_contact_directory_file(out_path=out)
    directory = load_contact_directory(path=out)
    assert set(directory) == {"glucometer-vendor", "gym-chain", "boho-house"}
    assert directory["boho-house"]["owner_name"] == "Arya"


def test_load_unknown_persona_returns_empty(tmp_path):
    out = str(tmp_path / "contacts.json")
    build_contact_directory_file(out_path=out)
    assert load_contact_directory(path=out, customer_id="does-not-exist") == {}


def test_load_missing_store_returns_empty_dict(tmp_path):
    assert load_contact_directory(path=str(tmp_path / "nope.json")) == {}


def test_mask_email_hides_the_local_part():
    assert _mask_email("anu.shree@neversaydie.co.in").endswith("@neversaydie.co.in")
    assert "anu.shree" not in _mask_email("anu.shree@neversaydie.co.in")


def test_resolve_send_email_returns_plaintext_and_audits_without_leaking(tmp_path):
    """resolve_send_email decrypts the real address for a send AND appends one
access-log line -- but that log records only the FACT of access (trace,
"""

    out = str(tmp_path / "contacts.json")
    build_contact_directory_file(out_path=out)
    rec = load_contact_directory(path=out, customer_id="glucometer-vendor")

    audit_path = str(tmp_path / "contact_access.jsonl")
    plaintext = resolve_send_email(rec, trace="trc-test-123", access_log_path=audit_path)
    # the real address is recovered for the actual send
    assert plaintext == DEMO_CONTACT_EMAIL

    # the access log recorded the FACT of access, keyed by trace + customer...
    with open(audit_path) as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert len(lines) == 1
    entry = lines[0]
    assert entry["trace"] == "trc-test-123"
    assert entry["customer_id"] == "glucometer-vendor"
    assert entry["owner_name"] == "Claire"
    assert entry["action"] == "email_resolved_for_send"
    assert "timestamp" in entry

    # ...but the plaintext email string appears NOWHERE in the log's raw text
    with open(audit_path) as f:
        raw = f.read()
    assert DEMO_CONTACT_EMAIL not in raw

    # a second resolve appends (append-only), never overwrites
    resolve_send_email(rec, trace="trc-test-456", access_log_path=audit_path)
    with open(audit_path) as f:
        assert sum(1 for l in f if l.strip()) == 2


def test_directory_feeds_weekly_reminders_single_recipient(tmp_path):
    """The whole point: the persona record the store produces is directly
usable as weekly_reminders' `contact_directory`, resolving the ONE
"""

    contacts = str(tmp_path / "contacts.json")
    build_contact_directory_file(out_path=contacts)
    record = load_contact_directory(path=contacts, customer_id="glucometer-vendor")

    # minimal bucket for a friday run with one missing payment for a hospital
    root = str(tmp_path / "bucket_output")
    prefix = os.path.join(root, "glucometer-vendor", "2026-07-22")
    os.makedirs(prefix, exist_ok=True)
    txns = [{"counterparty_name": "Charite Berlin", "customer_reference_id": "CUST-HOSP-1",
             "reference_id": "E2E-1", "transaction_date": "2026-05-01",
             "amount": 9000.0, "status": "Success"}]
    stats = {"CUST-HOSP-1": {"transaction_count": 4, "rolling_3m_avg": 9000.0,
                             "rolling_12m_avg": 9000.0, "overall_avg": 9000.0,
                             "last_transaction_date": "2026-05-01"}}
    flags = {"missing_payments": [{"customer_reference_id": "CUST-HOSP-1",
             "type": "missing_payment", "last_transaction_date": "2026-05-01",
             "typical_cadence_days": 7, "expected_by": "2026-05-08",
             "grace_period_ends": "2026-05-14", "days_overdue": 5}],
             "high_value_transactions": [], "deviations": [], "explicit_failures": []}
    for name, obj in [("normalized_transactions.json", txns),
                      ("historical_stats.json", stats), ("inflow_flags.json", flags)]:
        with open(os.path.join(prefix, name), "w") as f:
            json.dump(obj, f)

    notifier = InMemoryEmailNotifier()
    results = weekly_reminders.run_friday(
        bucket_root=root, customer_id="glucometer-vendor", run_date="2026-07-22",
        tenant_name="MedSupply Direct", from_address="reports@kapitalwerknxt.bank",
        notifier=notifier, contact_directory=record,
    )
    # exactly ONE email to the ONE customer, formal generic salutation
    assert len(results) == 1 and results[0]["status"] == "sent"
    assert len(notifier.sent) == 1
    assert notifier.sent[0].to == DEMO_CONTACT_EMAIL
    assert "Dear Customer," in notifier.sent[0].body
