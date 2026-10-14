"""Reminder email content + HTML rendering for the Reporting Agent.

Structured the way a Go html/template-based mailer would be: ONE shared
"""


from __future__ import annotations

from datetime import datetime, timedelta
from string import Template

# One HTML skeleton for every reminder email. LOGO_CID is the Content-ID the
# caller must attach the logo under -- cid: survives mail clients that strip data: URIs.
LOGO_CID = "kapitalwerknxt-logo"

EMAIL_HTML_TEMPLATE = Template("""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${subject}</title>
</head>
<body style="margin:0;padding:0;background:#EDEAE1;font-family:'Public Sans',Arial,sans-serif">
  <div style="padding:24px;background:#EDEAE1">
    <div style="max-width:560px;margin:0 auto;background:#FFFFFF;border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06)">

      <div style="background:${accent_color};padding:22px 28px;display:flex;align-items:center;justify-content:space-between">
        <div style="display:flex;align-items:center;gap:8px">
          <img src="cid:${logo_cid}" alt="KapitalWerkNxt" width="22" height="20" style="display:block">
          <div style="display:flex;flex-direction:column;gap:0px">
            <span style="font-size:16px;font-weight:800;color:#fff;letter-spacing:-0.01em">KapitalWerkNxt</span>
            <span style="font-size:10px;color:rgba(255,255,255,.72)">Predict | Remind | Grow</span>
          </div>
        </div>
        <span style="font-size:10.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#fff;background:rgba(255,255,255,.18);padding:5px 10px;border-radius:20px">${badge_text}</span>
      </div>

      <div style="padding:30px 28px 8px">
        <div style="display:flex;gap:14px;align-items:flex-start;margin-bottom:20px">
          <div style="width:38px;height:38px;border-radius:50%;background:${accent_soft};color:${accent_color};display:flex;align-items:center;justify-content:center;font-size:17px;font-weight:800;flex-shrink:0">${icon_glyph}</div>
          <div>
            <div style="font-size:15px;font-weight:700;color:#23241F;margin-bottom:2px">${headline}</div>
            <div style="font-size:12px;color:#9B9A8F">${subheadline}</div>
          </div>
        </div>

        <div style="font-size:14px;color:#23241F;line-height:1.6;margin-bottom:12px">${greeting}</div>
        <div style="font-size:13.5px;color:#3D3C35;line-height:1.65;margin-bottom:20px">${body_text}</div>

        <div style="${box_style}">
          <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;opacity:.75;margin-bottom:6px">${box_label}</div>
          <div style="font-size:26px;font-weight:800">${box_amount}</div>
          <div style="font-size:12px;margin-top:4px;opacity:.85">${box_sub}</div>
        </div>

        ${detail_rows_html}

        <div style="font-size:12.5px;color:#3D3C35;line-height:1.6;margin-top:22px">${signoff}</div>
      </div>

      <div style="padding:16px 28px;border-top:1px solid #F0EEE8;background:#FAFAF7">
        <div style="font-size:10.5px;color:#B5B1A2;line-height:1.5">This is an automated message from KapitalWerkNxt's Reporting Agent &middot; trace ${trace}</div>
        <div style="font-size:10.5px;color:#C7C3B5;margin-top:4px">KapitalWerkNxt Financial Intelligence &middot; sent on behalf of ${tenant_name} &middot; this mailbox is not monitored</div>
      </div>
    </div>
  </div>
</body>
</html>""")

# one row's markup, repeated per detail row -- the html/template equivalent
# of a {{range .DetailRows}} block
DETAIL_ROW_TEMPLATE = Template(
    '<div style="display:flex;justify-content:space-between;padding:11px 16px;'
    'font-size:12.5px;border-top:1px solid #F5F3ED">'
    '<span style="color:#9B9A8F">${label}</span>'
    '<span style="color:#23241F;font-weight:600;font-family:\'IBM Plex Mono\',monospace">${value}</span>'
    "</div>"
)
DETAIL_ROWS_WRAPPER_TEMPLATE = Template(
    '<div style="margin-top:18px;border:1px solid #EFEBE1;border-radius:10px;overflow:hidden">${rows}</div>'
)

EMAIL_TEXT_TEMPLATE = Template(
    "${greeting}\n\n"
    "${body_text}\n\n"
    "${box_label}: ${box_amount} (${box_sub})\n\n"
    "${detail_rows_text}\n"
    "${signoff}\n\n"
    "-- KapitalWerkNxt · trace ${trace}"
)


def fmt_eur(amount: float) -> str:
    return f"€{amount:,.2f}"


def fmt_date_nice(iso_date: str) -> str:
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%b %-d, %Y")


# Data builders -- the only functions that vary per email type. Each returns
# a flat dict of placeholder values; nothing here touches markup.
def build_upcoming_payment_email(
    *,
    hospital_name: str,
    tenant_name: str,
    amount: float,
    cadence_days: int,
    last_transaction_date: str,
    trace: str,
    from_address: str,
    to_address: str,
) -> dict:
    next_expected = (
        datetime.strptime(last_transaction_date, "%Y-%m-%d").date() + timedelta(days=cadence_days)
    ).isoformat()
    return {
        "from_address": from_address,
        "to_address": to_address,
        "tenant_name": tenant_name,
        "subject": f"Your upcoming payment to {tenant_name}",
        "accent_color": "#1F4B3F",
        "accent_soft": "#E8F0EA",
        "badge_text": "UPCOMING PAYMENT",
        "icon_glyph": "→",
        "headline": "Your next payment is coming up",
        "subheadline": f"Based on your usual {cadence_days}-day cadence",
        "greeting": f"Hi {hospital_name} finance team,",
        "body_text": (
            f"This is a heads-up that your next regular payment to {tenant_name} is expected "
            f"around {fmt_date_nice(next_expected)}, based on your usual {cadence_days}-day "
            f"cadence. No action is needed if you're already on track."
        ),
        "box_label": "Expected amount",
        "box_amount": fmt_eur(amount),
        "box_sub": "Based on your trailing 3-month average",
        "box_style": "background:#132821;color:#fff;border-radius:12px;padding:18px 20px;margin-top:6px",
        "detail_rows": [
            {"label": "Counterparty", "value": hospital_name},
            {"label": "Expected date", "value": fmt_date_nice(next_expected)},
            {"label": "Typical cadence", "value": f"Every {cadence_days} days"},
            {"label": "Reference", "value": trace},
        ],
        "signoff": "We'll follow up only if this payment hasn't arrived by the end of the week.",
        "trace": trace,
    }


def build_missed_payment_email(
    *,
    hospital_name: str,
    tenant_name: str,
    amount: float,
    cadence_days: int,
    expected_by: str,
    grace_period_ends: str,
    trace: str,
    from_address: str,
    to_address: str,
) -> dict:
    return {
        "from_address": from_address,
        "to_address": to_address,
        "tenant_name": tenant_name,
        "subject": f"Reminder: your expected payment to {tenant_name} hasn't been received",
        "accent_color": "#B3402A",
        "accent_soft": "#FBEAE5",
        "badge_text": "PAYMENT REMINDER",
        "icon_glyph": "!",
        "headline": "A payment we expected hasn't arrived",
        "subheadline": f"{cadence_days}-day cadence · grace period closed {fmt_date_nice(grace_period_ends)}",
        "greeting": f"Hi {hospital_name} finance team,",
        "body_text": (
            f"Our records show your regular payment to {tenant_name} — expected around "
            f"{fmt_date_nice(expected_by)} based on your usual {cadence_days}-day cadence — "
            f"had not been received by the end of this week. The grace period for this cycle "
            f"ended {fmt_date_nice(grace_period_ends)}."
        ),
        "box_label": "Expected amount",
        "box_amount": fmt_eur(amount),
        "box_sub": "Based on your trailing 3-month average",
        "box_style": "background:#2B1512;color:#fff;border-radius:12px;padding:18px 20px;margin-top:6px",
        "detail_rows": [
            {"label": "Counterparty", "value": hospital_name},
            {"label": "Expected by", "value": fmt_date_nice(expected_by)},
            {"label": "Grace period ended", "value": fmt_date_nice(grace_period_ends)},
            {"label": "Reference", "value": trace},
        ],
        "signoff": (
            "If this payment has already been sent, please disregard this notice — it can "
            "take a day or two to reconcile. Otherwise, please reach out to your relationship "
            "manager to confirm timing."
        ),
        "trace": trace,
    }


# Aggregate builders -- ONE email per business customer summarising their
# whole book, reusing the same shared template (weekly_reminders sends these).

# Rows to show before collapsing the rest into a single "+N more" row.
MAX_BREAKDOWN_ROWS = 10


def _capped_breakdown_rows(items: list[dict], value_fn) -> list[dict]:
    """Turn a (already-sorted) list of contributor dicts into detail_rows,
showing at most MAX_BREAKDOWN_ROWS individual rows and collapsing any
"""

    rows = [
        {"label": item["name"], "value": value_fn(item)}
        for item in items[:MAX_BREAKDOWN_ROWS]
    ]
    remainder = len(items) - MAX_BREAKDOWN_ROWS
    if remainder > 0:
        rows.append({"label": f"+{remainder} more counterparties", "value": "…"})
    return rows


def build_weekly_upcoming_summary_email(
    *,
    owner_name: str,
    tenant_name: str,
    contributors: list[dict],
    trace: str,
    from_address: str,
    to_address: str,
) -> dict:
    """Monday, one per business customer. `contributors` is a list of
{"name": str, "amount": float} -- one entry per counterparty with enough
"""

    contributors = sorted(contributors, key=lambda c: c["amount"], reverse=True)
    total = sum(c["amount"] for c in contributors)
    count = len(contributors)
    return {
        "from_address": from_address,
        "to_address": to_address,
        "tenant_name": tenant_name,
        "subject": f"Your expected inflow this week — {tenant_name}",
        "accent_color": "#1F4B3F",
        "accent_soft": "#E8F0EA",
        "badge_text": "WEEKLY SUMMARY",
        "icon_glyph": "∑",
        "headline": "Your expected inflow this week",
        "subheadline": f"Across {count} counterpart{'y' if count == 1 else 'ies'} on their usual cadence",
        # Formal generic salutation, matching real EU bank correspondence.
        "greeting": "Dear Customer,",
        "body_text": (
            f"Here's what we expect to land in {tenant_name}'s account this week, summed across "
            f"the {count} counterpart{'y' if count == 1 else 'ies'} with enough payment history to "
            f"forecast. Each figure is that counterparty's trailing 3-month average. No action is "
            f"needed -- we'll follow up on Friday about anything that hasn't arrived."
        ),
        "box_label": "Total expected this week",
        "box_amount": fmt_eur(total),
        "box_sub": f"Summed across {count} counterpart{'y' if count == 1 else 'ies'}' 3-month averages",
        "box_style": "background:#132821;color:#fff;border-radius:12px;padding:18px 20px;margin-top:6px",
        "detail_rows": _capped_breakdown_rows(contributors, lambda c: fmt_eur(c["amount"])),
        "signoff": "We'll follow up only about counterparties whose payment hasn't arrived by the end of the week.",
        "trace": trace,
    }


def build_weekly_missed_summary_email(
    *,
    owner_name: str,
    tenant_name: str,
    missed: list[dict],
    trace: str,
    from_address: str,
    to_address: str,
) -> dict:
    """Friday, one per business customer. `missed` is a list of
{"name": str, "amount": float, "days_overdue": int} -- one entry per
"""

    missed = sorted(missed, key=lambda m: m["days_overdue"], reverse=True)
    total = sum(m["amount"] for m in missed)
    count = len(missed)
    return {
        "from_address": from_address,
        "to_address": to_address,
        "tenant_name": tenant_name,
        "subject": f"Reminder: {count} expected payment{'' if count == 1 else 's'} to {tenant_name} not yet received",
        "accent_color": "#B3402A",
        "accent_soft": "#FBEAE5",
        "badge_text": "PAYMENT REMINDER",
        "icon_glyph": "!",
        "headline": "Payments we expected this week haven't arrived",
        "subheadline": f"{count} counterpart{'y' if count == 1 else 'ies'} · summed expected total below",
        # Formal generic salutation, matching real EU bank correspondence.
        "greeting": "Dear Customer,",
        "body_text": (
            f"Our records show {count} regular payment{'' if count == 1 else 's'} to {tenant_name} that "
            f"we expected this week had not been received by the end of it. The counterparties and how "
            f"overdue each one is are listed below. If any have since been reconciled, please disregard "
            f"those -- it can take a day or two."
        ),
        "box_label": "Total expected but not received",
        "box_amount": fmt_eur(total),
        "box_sub": f"Across {count} counterpart{'y' if count == 1 else 'ies'}",
        "box_style": "background:#2B1512;color:#fff;border-radius:12px;padding:18px 20px;margin-top:6px",
        "detail_rows": _capped_breakdown_rows(
            missed, lambda m: f"{fmt_eur(m['amount'])} · {m['days_overdue']}d overdue"
        ),
        "signoff": (
            "Reach out to your relationship manager to confirm timing on any of these, or to have us "
            "chase them on your behalf."
        ),
        "trace": trace,
    }


# Rendering: execute the shared template against a data dict, once per customer.
def render_html(data: dict) -> str:
    rows_html = "".join(DETAIL_ROW_TEMPLATE.substitute(row) for row in data.get("detail_rows", []))
    wrapped_rows = DETAIL_ROWS_WRAPPER_TEMPLATE.substitute(rows=rows_html) if rows_html else ""
    return EMAIL_HTML_TEMPLATE.substitute(
        {**data, "logo_cid": LOGO_CID, "detail_rows_html": wrapped_rows}
    )


def render_text(data: dict) -> str:
    rows_text = "\n".join(f"{r['label']}: {r['value']}" for r in data.get("detail_rows", []))
    return EMAIL_TEXT_TEMPLATE.substitute({**data, "detail_rows_text": rows_text})
