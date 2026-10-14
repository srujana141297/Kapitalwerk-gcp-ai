"""Marketing Agent -- the third agent that calls an LLM (after Risk and AML),
through the exact same GovernanceMiddleware boundary, so it is budgeted,
cached and audited identically and the mock provider the test suite uses works
"""


from __future__ import annotations

import json
import os
import re
from collections import defaultdict

from governance import BudgetExceeded

# City-centre coordinates for the map bubbles; unlisted cities get lat/lon=None.
CITY_COORDS = {
    "Berlin": (52.5200, 13.4050), "Hamburg": (53.5511, 9.9937),
    "Munich": (48.1351, 11.5820), "Cologne": (50.9375, 6.9603),
    "Frankfurt": (50.1109, 8.6821), "Stuttgart": (48.7758, 9.1829),
    "Duesseldorf": (51.2277, 6.7735), "Leipzig": (51.3397, 12.3731),
    "Dortmund": (51.5136, 7.4653), "Essen": (51.4556, 7.0116),
    "Bremen": (53.0793, 8.8017), "Dresden": (51.0504, 13.7373),
    "Nuernberg": (49.4521, 11.0767), "Bonn": (50.7374, 7.0982),
    "Offenbach": (50.0955, 8.7761), "Ludwigsburg": (48.8974, 9.1916),
    "Bochum": (51.4818, 7.2162), "Augsburg": (48.3705, 10.8978),
    "Muenster": (51.9607, 7.6261), "Mannheim": (49.4875, 8.4660),
    "Chemnitz": (50.8278, 12.9214), "Tuebingen": (48.5216, 9.0576),
    "Heidelberg": (49.3988, 8.6724),
}

VALID_LABELS = {"opportunity", "watch", "stable"}


def aggregate_by_postal(transactions: list[dict]) -> list[dict]:
    """Roll successful transactions up by postal code (deterministic, no LLM).
Returns one dict per postal code, sorted by total_value descending.
"""

    buckets: dict[str, dict] = defaultdict(lambda: {"transaction_count": 0, "total_value": 0.0, "city": None})
    for t in transactions:
        if t.get("status") not in (None, "Success"):
            continue
        extra = t.get("extra") or {}
        postal = extra.get("Postal_Code")
        city = extra.get("City")
        if not postal:
            continue  # no geography on this record -- can't place it on a map
        b = buckets[postal]
        b["transaction_count"] += 1
        b["total_value"] += float(t.get("amount") or 0)
        if city and not b["city"]:
            b["city"] = city

    out = []
    for postal, b in buckets.items():
        lat, lon = CITY_COORDS.get(b["city"], (None, None))
        out.append({
            "postal_code": postal,
            "city": b["city"],
            "lat": lat,
            "lon": lon,
            "transaction_count": b["transaction_count"],
            "total_value": round(b["total_value"], 2),
        })
    out.sort(key=lambda e: e["total_value"], reverse=True)
    return out


def build_prompt(aggregates: list[dict]) -> str:
    lines = "\n".join(
        f"  - {a['postal_code']} ({a['city'] or 'unknown city'}): "
        f"{a['transaction_count']} transactions, EUR {a['total_value']:,.2f} total value"
        for a in aggregates
    ) or "  (no geolocated transactions this run)"
    return (
        "You are a marketing analyst for a bank's business-customer portfolio. "
        "Below is this customer's transaction activity aggregated by German postal code. "
        "For EACH postal code, assign one label from {opportunity, watch, stable} and write "
        "a single short narrative sentence a relationship manager could read off a map.\n\n"
        "Guidance: 'opportunity' = high or growing value worth deepening the relationship; "
        "'watch' = low or thin activity worth monitoring; 'stable' = steady, no action needed.\n\n"
        f"POSTAL-CODE ACTIVITY\n{lines}\n\n"
        "Respond with one line per postal code in EXACTLY this format:\n"
        "POSTAL <postal_code>: <label> | <narrative sentence>"
    )


def _fallback_annotation(agg: dict, all_aggs: list[dict]) -> tuple[str, str]:
    """Deterministic label/narrative when the LLM is unavailable or its output
for a given postal code can't be parsed -- so the heatmap is always fully
"""

    values = sorted((a["total_value"] for a in all_aggs), reverse=True)
    top_third = values[max(0, len(values) // 3 - 1)] if values else 0
    if agg["total_value"] >= top_third and agg["transaction_count"] >= 3:
        label = "opportunity"
    elif agg["transaction_count"] < 3:
        label = "watch"
    else:
        label = "stable"
    narrative = (f"{agg['transaction_count']} transactions worth EUR {agg['total_value']:,.2f} "
                 f"in {agg['city'] or agg['postal_code']}.")
    return label, narrative


def parse_annotations(text: str) -> dict[str, tuple[str, str]]:
    """Parse the LLM response into {postal_code: (label, narrative)}. Tolerant:
only lines matching the requested shape are picked up; anything else is
"""

    out: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        m = re.match(r"\s*POSTAL\s+(\S+)\s*:\s*(\w+)\s*\|\s*(.+)", line, re.IGNORECASE)
        if not m:
            continue
        postal, label, narrative = m.group(1), m.group(2).lower(), m.group(3).strip()
        if label not in VALID_LABELS:
            continue
        out[postal] = (label, narrative)
    return out


class MarketingAgent:
    def __init__(self, governance):
        self.governance = governance

    def assess(self, transactions: list[dict], verbose: bool = True,
               pii_blocked: bool = False) -> dict:
        aggregates = aggregate_by_postal(transactions)
        if not aggregates:
            return {"heatmap": [], "llm_available": False, "parse_failed": False,
                    "trace_id": None, "pii_blocked": pii_blocked}

        if pii_blocked:
            # Fail-closed: PII detected upstream -- skip the LLM, annotate
            # deterministically, and log the block to the audit trail.
            self.governance.log_governance_event(
                "marketing_agent", stop_reason="pii_detected_blocked",
                detail={
                    "postal_code_count": len(aggregates),
                    "note": ("marketing LLM call blocked (fail-closed) -- PII detected "
                             "upstream by risk_agent; deterministic labels used instead"),
                },
            )
            if verbose:
                print(f"  [marketing_agent] PII DETECTED upstream -- LLM call BLOCKED "
                      f"(fail-closed). Annotating {len(aggregates)} postal code(s) "
                      f"deterministically, no prompt sent.")
            heatmap = [
                {**agg, **dict(zip(("label", "narrative"), _fallback_annotation(agg, aggregates)))}
                for agg in aggregates
            ]
            return {"heatmap": heatmap, "llm_available": False, "parse_failed": False,
                    "trace_id": None, "pii_blocked": True}

        prompt = build_prompt(aggregates)
        annotations: dict[str, tuple[str, str]] = {}
        llm_available = True
        parse_failed = False
        trace_id = None

        try:
            response_text, record = self.governance.complete("marketing_agent", prompt)
            print("=" * 80)
            print("RAW LLM RESPONSE")
            print(response_text)
            print("=" * 80)
            trace_id = record.trace_id
            annotations = parse_annotations(response_text)
            print(f"[marketing_agent] annotations: {annotations}")
            parse_failed = len(annotations) == 0
            print(f"[marketing_agent] parse_failed: {parse_failed} ")
            if verbose:
                note = ", PARSE FAILED -- used deterministic labels" if parse_failed else ""
                print(f"  [marketing_agent] annotated {len(annotations)}/{len(aggregates)} "
                      f"postal codes ({record.stop_reason}{note})")
        except BudgetExceeded as e:
            llm_available = False
            if verbose:
                print(f"  [marketing_agent] STOPPED -- {e.stop_reason}. "
                      f"Falling back to deterministic labels.")

        heatmap = []
        for agg in aggregates:
            label, narrative = annotations.get(agg["postal_code"], (None, None))
            if label is None:
                label, narrative = _fallback_annotation(agg, aggregates)
            heatmap.append({**agg, "label": label, "narrative": narrative})

        return {"heatmap": heatmap, "llm_available": llm_available,
                "parse_failed": parse_failed, "trace_id": trace_id,
                "pii_blocked": False}

    def run(self, bucket, customer_id: str, run_date: str,
            verbose: bool = True, pii_blocked: bool = False) -> dict:
        """Read the run's normalized transactions from the bucket, build the
annotated heatmap, and persist it. Returns a small summary.
"""

        prefix = f"{customer_id}/{run_date}"
        try:
            transactions = bucket.read_json(
                f"{prefix}/normalized_transactions.json"
            )
        except Exception:
            transactions = []

        result = self.assess(transactions, verbose=verbose, pii_blocked=pii_blocked)
        blob_path = f"{customer_id}/{run_date}/marketing_heatmap.json"
        out_path = bucket.write_json(blob_path, {
            "customer_id": customer_id,
            "run_date": run_date,
            "postal_code_count": len(result["heatmap"]),
            "llm_available": result["llm_available"],
            "pii_blocked": result.get("pii_blocked", False),
            "heatmap": result["heatmap"],
        })

        return {
            "customer_id": customer_id,
            "run_date": run_date,
            "postal_code_count": len(result["heatmap"]),
            "output": out_path,
            "llm_available": result["llm_available"],
            "pii_blocked": result.get("pii_blocked", False),
        }
