"""Demo runner for the Monday/Friday weekly cycle, over the current period
(2026-07-01 to 2026-07-23) against the 2-year historical baseline
(which now ends 2026-06-26).
"""


import json

from weekly_cycle import load_baseline, expected_this_week, run_all_weeks
from narrative import monday_narrative, friday_narrative

BASELINE = "../bucket_output/glucometer-vendor/2026-07-12/historical_stats.json"
CURRENT_PERIOD = "../ingestion_data/hospital_vendor_current_period_jul1_23.csv"

if __name__ == "__main__":
    baseline = load_baseline(BASELINE)
    expected = expected_this_week(baseline)
    print(f"MONDAY -- expected {len(expected)} counterparties to pay this week (baseline amount each):")
    for cust_id, amt in sorted(expected.items()):
        print(f"  {cust_id}: EUR {amt:,.2f}")
    print(f"\nMONDAY report text:\n{monday_narrative(expected)}")

    print("\n" + "=" * 70)
    print("FRIDAY reports, week by week:")
    print("=" * 70)
    results = run_all_weeks(BASELINE, CURRENT_PERIOD)
    for r in results:
        print(f"\n--- {r['week']} ---")
        print(json.dumps(r, indent=2))
        print(f"\nFRIDAY report text:\n{friday_narrative(r)}")
