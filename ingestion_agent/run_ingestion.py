"""Demo runner: proves the same IngestionAgent handles both use cases
(hospital vendor B2B data and the gym/Boho House membership data)
just by pointing it at a different source_type -- no code changes.
"""


import json
import os
import sys
from pathlib import Path

from ingestion_agent import IngestionAgent
from ingestion_agent.storage import GCSBucket


def _load_config():
    root = Path(__file__).resolve().parent.parent
    with open(root / "config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    config = _load_config()
    bucket_name = config.get("gcs_bucket")
    if not bucket_name:
        raise ValueError("gcs_bucket must be set in config.json")
    bucket = GCSBucket(bucket_name)
    agent = IngestionAgent(bucket)

    jobs = [
        # Both hospital files -- 2yr history AND the current-period file -- so
        # ALL the input data for this customer lands in one snapshot.
        (["../ingestion_data/hospital_vendor_2yr_history.csv",
          "../ingestion_data/hospital_vendor_current_period_jul1_23.csv"],
         "hospital_vendor", "glucometer-vendor"),
        ("../ingestion_data/gym_membership_2yr_history.csv", "membership_club", "gym-chain"),
        ("../ingestion_data/boho_house_2yr_history.csv", "membership_club", "boho-house"),
    ]

    for csv_path, source_type, customer_id in jobs:
        print(f"\n--- ingesting {customer_id} ({source_type}) ---")
        summary = agent.run(csv_path, source_type, customer_id)
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    sys.exit(main())
