"""Plain-English report text for the Inflow Agent -- template-based, NOT an LLM
call. Every number in these sentences comes straight out of the structured
output (expected_this_week() / friday_report()); this module only formats
"""


from __future__ import annotations


def monday_narrative(expected: dict) -> str:
    total = sum(expected.values())
    return (
        f"This week, we expect payments from {len(expected)} counterparties, "
        f"totalling approximately EUR {total:,.2f} based on their historical payment pattern."
    )


def friday_narrative(report: dict) -> str:
    lines = []

    deviation = report["inflow_deviation_pct"]
    total_actual = report["total_actual"]
    total_expected = report["total_expected"]

    if deviation is None:
        lines.append("No expected-inflow baseline was available to compare against this week.")
    elif deviation < 0:
        lines.append(
            f"Your account received approximately {abs(deviation):.1f}% lower inflows than "
            f"expected this week (EUR {total_actual:,.2f} received vs. EUR {total_expected:,.2f} expected)."
        )
    elif deviation > 0:
        lines.append(
            f"Your account received approximately {deviation:.1f}% higher inflows than "
            f"expected this week (EUR {total_actual:,.2f} received vs. EUR {total_expected:,.2f} expected)."
        )
    else:
        lines.append(
            f"Inflows this week matched the expected amount (EUR {total_actual:,.2f})."
        )

    misses = report["missed_payments"]
    if misses:
        names = ", ".join(m["hospital_name"] for m in misses)
        missed_total = sum(m["expected_amount"] for m in misses)
        lines.append(
            f"{report['missed_count']} expected payment(s) did not arrive this week, "
            f"totalling EUR {missed_total:,.2f}: {names}. You may wish to follow up on these."
        )
    else:
        lines.append("All expected counterparties paid this week.")

    lines.append(
        f"Cumulative inflow for the period to date: EUR {report['cumulative_amount_period_to_date']:,.2f}."
    )

    return " ".join(lines)
