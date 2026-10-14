"""Reporting Agent -- weekly reminder emails.

The bank (KapitalWerkNxt) has exactly THREE business customers -- one per
"""


from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "inflow_agent"))

#from reporting_agent.test_exports import bucket
from storage import GCSBucket, LocalBucket
import templates  # noqa: E402  (local import, path set up above -- keeps this runnable as a script)
from contact_store import (  # noqa: E402
    PERSONA_OWNERS,        # persona -> owner fallback for the greeting
    _mask_email,           # mask a recipient_override for the (readable) send log
    resolve_send_email,    # decrypt the real address ONLY at dispatch
)
from email_client import (  # noqa: E402
    ConsoleEmailNotifier,
    EmailNotifier,
    InlineAttachment,
    ResendEmailNotifier,
    SentEmail,
)

MIN_TRANSACTIONS_FOR_BASELINE = 3  # same guard used by every other agent in this pipeline

LOGO_PATH = os.path.join(os.path.dirname(__file__), "assets", "kapitalwerknxt-logo.png")


def _load_json(path: str):
    with open(path) as f:
        return json.load(f)


def _load_logo_attachment() -> InlineAttachment:
    with open(LOGO_PATH, "rb") as f:
        return InlineAttachment(
            content_id=templates.LOGO_CID,
            filename="kapitalwerknxt-logo.png",
            mime_type="image/png",
            data=f.read(),
        )


def _cadence_days(dates_sorted: list[str]) -> int:
    """Median gap between successful payments, in days. Mirrors
inflow_agent.py's InflowAgent.flag_missing_payments() -- duplicated
"""

    if len(dates_sorted) < 2:
        return 30
    gaps = [
        (datetime.strptime(dates_sorted[i + 1], "%Y-%m-%d") - datetime.strptime(dates_sorted[i], "%Y-%m-%d")).days
        for i in range(len(dates_sorted) - 1)
    ]
    return round(statistics.median(gaps)) if gaps else 30


def _name_map(transactions: list[dict]) -> dict[str, str]:
    # counterparty_name is optional -- the membership_club schema masks/omits
    # it (a hashed member ref), so fall back to the customer_reference_id.
    return {
        t["customer_reference_id"]: (t.get("counterparty_name") or t["customer_reference_id"])
        for t in transactions
    }


def _successful_dates_by_customer(transactions: list[dict]) -> dict[str, list[str]]:
    by_cust: dict[str, list[str]] = {}
    for t in transactions:
        if t["status"] == "Success":
            by_cust.setdefault(t["customer_reference_id"], []).append(t["transaction_date"])
    for dates in by_cust.values():
        dates.sort()
    return by_cust


def _resolve_persona_recipient(
    customer_id: str,
    contact_directory: dict | None,
    recipient_override: str | None,
) -> tuple[str | None, str | None, bool]:
    """Resolve, WITHOUT producing any plaintext address, the (masked_address,
owner_name, has_recipient) for this persona's single reminder.
"""

    record = contact_directory or {}
    owner_name = record.get("owner_name") or PERSONA_OWNERS.get(customer_id)
    if recipient_override:
        return _mask_email(recipient_override), owner_name, True
    if record.get("email_encrypted"):
        return record.get("email_masked") or _mask_email(record.get("email_encrypted")), owner_name, True
    return None, owner_name, False


def _write_send_log(bucket, customer_id: str, run_date: str, kind: str, record: dict) -> str:
    """Persist what was actually sent THIS RUN for THIS persona -- at most one
email, so `record` is a single dict (not a list keyed by counterparty).
"""
    
    blob_path = f"{customer_id}/{run_date}/reminders_sent_{kind}.json"
    payload = {
        "customer_id": customer_id,
        "run_date": run_date,
        "kind": kind,
        "email_count": 1 if record.get("status") == "sent" else 0,
        "record": record,
    }
    return bucket.write_json(blob_path, payload)


def _dispatch(notifier: EmailNotifier, data: dict, attachments: list[InlineAttachment]) -> SentEmail:
    html = templates.render_html(data)
    text = templates.render_text(data)
    return notifier.send(
        to=data["to_address"],
        subject=data["subject"],
        body=text,
        html=html,
        from_address=data["from_address"],
        inline_attachments=attachments,
    )


# Matches templates.MAX_BREAKDOWN_ROWS so the send log mirrors the real email.
LOG_BREAKDOWN_ROWS = templates.MAX_BREAKDOWN_ROWS


def run_monday(
    *,
    bucket,
    customer_id: str,
    run_date: str,
    tenant_name: str,
    from_address: str,
    notifier: EmailNotifier,
    contact_directory: dict | None = None,
    recipient_override: str | None = None,
) -> list[dict]:
    """Send AT MOST ONE "expected inflow this week" email to this persona's
single customer. Returns a list of 0 or 1 record (never one-per-
"""

    prefix = f"{customer_id}/{run_date}"
    transactions = bucket.read_json(
        f"{prefix}/normalized_transactions.json"
    )
    stats = bucket.read_json(
        f"{prefix}/historical_stats.json"
    )

    name_map = _name_map(transactions)

    # Aggregate every counterparty with enough history into ONE book.
    contributors = []
    for cust_id, s in stats.items():
        if s["transaction_count"] < MIN_TRANSACTIONS_FOR_BASELINE:
            continue  # cold start -- same guard as InflowAgent
        amount = s.get("rolling_3m_avg") or s.get("overall_avg")
        if not amount:
            continue  # no baseline amount -> nothing to expect from them
        contributors.append({
            "customer_reference_id": cust_id,
            "name": name_map.get(cust_id) or cust_id,
            "amount": amount,
        })

    if not contributors:
        # Genuinely nothing to summarise -- send nothing, log the no-op.
        record = {"customer_id": customer_id, "status": "nothing_to_report",
                  "counterparty_count": 0, "total_amount": 0.0, "to_address_masked": None}
        _write_send_log(bucket, customer_id, run_date, "monday_upcoming", record)
        return []

    masked_to, owner_name, has_recipient = _resolve_persona_recipient(customer_id, contact_directory, recipient_override)
    greeting_owner = owner_name or tenant_name
    total_amount = round(sum(c["amount"] for c in contributors), 2)
    top = sorted(contributors, key=lambda c: c["amount"], reverse=True)[:LOG_BREAKDOWN_ROWS]
    trace = f"trc-rep-{customer_id.lower()}-weekly-upcoming-{run_date}"

    # Only the masked address is ever persisted; plaintext exists transiently
    # below, only for notifier.send().
    record = {
        "customer_id": customer_id,
        "owner_name": greeting_owner,
        "to_address_masked": masked_to,
        "counterparty_count": len(contributors),
        "total_amount": total_amount,
        "breakdown_top": [{"name": c["name"], "amount": c["amount"]} for c in top],
        "trace": trace,
    }

    if not has_recipient:
        record["status"] = "skipped_no_recipient"
        _write_send_log(bucket, customer_id, run_date, "monday_upcoming", record)
        return [record]

    # Resolve the plaintext recipient only now, at dispatch time; never persisted.
    to_address = recipient_override or resolve_send_email(contact_directory or {}, trace=trace)

    data = templates.build_weekly_upcoming_summary_email(
        owner_name=greeting_owner,
        tenant_name=tenant_name,
        contributors=[{"name": c["name"], "amount": c["amount"]} for c in contributors],
        trace=trace,
        from_address=from_address,
        to_address=to_address,
    )
    sent = _dispatch(notifier, data, [_load_logo_attachment()])
    record["status"] = "sent"
    record["provider_response"] = sent.provider_response
    _write_send_log(bucket, customer_id, run_date, "monday_upcoming", record)
    return [record]


def run_friday(
    *,
    bucket,
    customer_id: str,
    run_date: str,
    tenant_name: str,
    from_address: str,
    notifier: EmailNotifier,
    contact_directory: dict | None = None,
    recipient_override: str | None = None,
) -> list[dict]:
    """Send AT MOST ONE "payments not yet received" email to this persona's
single customer. Returns a list of 0 or 1 record (never one-per-
"""

    prefix = f"{customer_id}/{run_date}"
    transactions = bucket.read_json(f"{prefix}/normalized_transactions.json")
    stats = bucket.read_json(f"{prefix}/historical_stats.json")
    flags = bucket.read_json(f"{prefix}/inflow_flags.json")

    name_map = _name_map(transactions)

    # Aggregate every flagged missing payment into ONE book.
    missed = []
    for miss in flags["missing_payments"]:
        cust_id = miss["customer_reference_id"]
        s = stats.get(cust_id, {})
        amount = s.get("rolling_3m_avg") or s.get("overall_avg") or 0.0
        missed.append({
            "customer_reference_id": cust_id,
            "name": name_map.get(cust_id) or cust_id,
            "amount": amount,
            "days_overdue": miss["days_overdue"],
        })

    if not missed:
        record = {"customer_id": customer_id, "status": "nothing_to_report",
                  "counterparty_count": 0, "total_amount": 0.0, "to_address_masked": None}
        _write_send_log(bucket, customer_id, run_date, "friday_missed", record)
        return []

    masked_to, owner_name, has_recipient = _resolve_persona_recipient(customer_id, contact_directory, recipient_override)
    greeting_owner = owner_name or tenant_name
    total_amount = round(sum(m["amount"] for m in missed), 2)
    top = sorted(missed, key=lambda m: m["days_overdue"], reverse=True)[:LOG_BREAKDOWN_ROWS]
    trace = f"trc-rep-{customer_id.lower()}-weekly-missed-{run_date}"

    # Only the masked address is ever persisted; plaintext exists transiently
    # below, only for notifier.send().
    record = {
        "customer_id": customer_id,
        "owner_name": greeting_owner,
        "to_address_masked": masked_to,
        "counterparty_count": len(missed),
        "total_amount": total_amount,
        "breakdown_top": [
            {"name": m["name"], "amount": m["amount"], "days_overdue": m["days_overdue"]} for m in top
        ],
        "trace": trace,
    }

    if not has_recipient:
        record["status"] = "skipped_no_recipient"
        _write_send_log(bucket, customer_id, run_date, "friday_missed", record)
        return [record]

    # Resolve the plaintext recipient only now, at dispatch time; never persisted.
    to_address = recipient_override or resolve_send_email(contact_directory or {}, trace=trace)

    data = templates.build_weekly_missed_summary_email(
        owner_name=greeting_owner,
        tenant_name=tenant_name,
        missed=[{"name": m["name"], "amount": m["amount"], "days_overdue": m["days_overdue"]} for m in missed],
        trace=trace,
        from_address=from_address,
        to_address=to_address,
    )
    sent = _dispatch(notifier, data, [_load_logo_attachment()])
    record["status"] = "sent"
    record["provider_response"] = sent.provider_response
    _write_send_log(bucket, customer_id, run_date, "friday_missed", record)
    return [record]


def _build_notifier(name: str) -> EmailNotifier:
    if name == "resend":
        return ResendEmailNotifier()
    return ConsoleEmailNotifier()


def main():
    parser = argparse.ArgumentParser(description="Send weekly reminder emails (Monday upcoming / Friday missed).")
    parser.add_argument("--mode", choices=["monday", "friday"], required=True)
    parser.add_argument("--customer", default="glucometer-vendor", help="customer_id under bucket_root")
    parser.add_argument("--tenant-name", default="MedSupply Direct")
    parser.add_argument("--run-date", default=date.today().isoformat())
    parser.add_argument("--bucket-root", default=os.path.join(os.path.dirname(__file__), "..", "bucket_output"))
    parser.add_argument("--from-address", default="onboarding@resend.dev")
    parser.add_argument("--notifier", choices=["console", "resend"], default="console")
    parser.add_argument("--recipient-override", default=None, help="send every reminder in this run to one address (demo/test)")
    args = parser.parse_args()

    from contact_store import load_contact_directory

    notifier = _build_notifier(args.notifier)
    # The persona's single contact record (one customer's email + owner name);
    # recipient_override still wins inside _resolve_persona_recipient.
    contact_directory = load_contact_directory(customer_id=args.customer)
    fn = run_monday if args.mode == "monday" else run_friday
    

    if os.environ.get("BUCKET_BACKEND", "local") == "gcs":
        bucket = GCSBucket(os.environ["GCS_BUCKET_NAME"])
    else:
        bucket = LocalBucket(args.bucket_root)
    results = fn(
        bucket=bucket,
        customer_id=args.customer,
        run_date=args.run_date,
        tenant_name=args.tenant_name,
        from_address=args.from_address,
        notifier=notifier,
        contact_directory=contact_directory,
        recipient_override=args.recipient_override,
    )
    sent = sum(1 for r in results if r["status"] == "sent")
    skipped = sum(1 for r in results if r["status"] == "skipped_no_recipient")
    # At most one email per persona per run now, so these are 0 or 1.
    print(f"[weekly_reminders] {args.mode} for {args.customer}: {sent} sent, "
          f"{skipped} skipped (no recipient on file)")


if __name__ == "__main__":
    main()
