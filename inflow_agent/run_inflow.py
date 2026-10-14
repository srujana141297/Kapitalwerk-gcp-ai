"""Demo runner: points the Inflow Agent at the Ingestion Agent's real output
(the shared ../bucket_output folder) and writes inflow_flags.json
alongside it -- same bucket, same customer/run_date partition.
"""


import json
import os
import sys
from pathlib import Path

from inflow_agent import InflowAgent
from inflow_agent.storage import GCSBucket


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
    agent = InflowAgent(bucket)

    jobs = [
        ("glucometer-vendor", "2026-07-12"),
        ("gym-chain", "2026-07-12"),
        ("boho-house", "2026-07-12"),
    ]

    for customer_id, run_date in jobs:
        print(f"\n--- inflow analysis: {customer_id} ---")
        summary = agent.run(bucket, customer_id, run_date)
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    sys.exit(main())
