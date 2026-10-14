"""Demo runner: executes the full graph against the real hospital vendor data
already sitting in ../ingestion_data and ../bucket_output.
"""


import json
import os
import sys
from datetime import date
from pathlib import Path

# Auto-loads a .env file next to this script (repo root)
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from graph import build_graph

# nodes.py hardcodes "../bucket_output" (relative to CWD, not to nodes.py's own
# location) for every agent's bucket writes -- it does not read BUCKET_ROOT.
# This has to resolve to that exact same directory (i.e. also CWD-relative,
# run from the orchestrator/ folder) or the "already ran today" check below
# would look in the wrong place and never find what the agents just wrote.
BUCKET_ROOT = os.environ.get("BUCKET_ROOT", "../bucket_output")

_REPORTING_AGENT_DIR = str((Path(__file__).resolve().parent / ".." / "reporting_agent").resolve())
if _REPORTING_AGENT_DIR not in sys.path:
    sys.path.insert(0, _REPORTING_AGENT_DIR)

from exports import is_complete_snapshot  # noqa: E402

# The three demo personas, mirroring ingestion_agent/run_ingestion.py's job
# list (single source of truth for csv_path/source_type/customer_id) plus
# the tenant display name each one reports under.
PERSONA_JOBS = [
    {
        "customer_id": "glucometer-vendor",
        "source_type": "hospital_vendor",
        # Both input files for this customer -- the 2yr history AND the
        # current-period file -- merged into one snapshot by IngestionAgent.
        "csv_path": [
            "../ingestion_data/hospital_vendor_2yr_history.csv",
            "../ingestion_data/hospital_vendor_current_period_jul1_23.csv",
        ],
        "tenant_name": "MedSupply Direct",
    },
    {
        "customer_id": "gym-chain",
        "source_type": "membership_club",
        "csv_path": "../ingestion_data/gym_membership_2yr_history.csv",
        "tenant_name": "FitCore Gyms",
    },
    {
        "customer_id": "boho-house",
        "source_type": "membership_club",
        "csv_path": "../ingestion_data/boho_house_2yr_history.csv",
        "tenant_name": "Boho House",
    },
]


def already_ran(customer_id: str, run_date: str) -> bool:
    """True only if run_date's snapshot is COMPLETE, not merely present --
otherwise a crashed mid-run folder blocks retries forever.
"""

    return is_complete_snapshot(BUCKET_ROOT, customer_id, run_date)


def run_one(customer_id: str, source_type: str, csv_path: str, tenant_name: str,
            run_date: str | None = None, force: bool = False,
            reminder_recipient_override: str | None = None, verbose: bool = True) -> dict | None:
    """Runs the full graph for one persona. Returns the final state, or None
    if skipped because today's snapshot already exists (and force=False).
    """

    run_date = run_date or date.today().isoformat()
    if not force and already_ran(customer_id, run_date):
        if verbose:
            print(f"--- {customer_id}: already ran for {run_date}, skipping (set FORCE_RUN=1 to override) ---")
        return None

    app = build_graph()
    initial_state = {
        "customer_id": customer_id,
        "source_type": source_type,
        "csv_path": csv_path,
        "tenant_name": tenant_name,
        "run_date": run_date,
    }
    if reminder_recipient_override:
        initial_state["reminder_recipient_override"] = reminder_recipient_override

    if verbose:
        print(f"--- {customer_id}: running for {run_date} ---")
    final_state = app.invoke(initial_state)
    if verbose:
        print(json.dumps({
            "customer_id": customer_id,
            "run_date": final_state["run_date"],
            "risk_score": final_state["risk_score"],
            "risk_reasons": final_state["risk_reasons"],
            "aml_cases": final_state.get("aml_cases", []),
            "report_text": final_state["report_text"],
            "reminders_queued": final_state["reminders_queued"],
        }, indent=2))
    return final_state


def run_all_personas(run_date: str | None = None, force: bool = False,
                      reminder_recipient_override: str | None = None, verbose: bool = True) -> dict:
    """Runs every persona in PERSONA_JOBS for run_date (default: today),
    skipping any persona that's already been run today unless force=True.
    Returns {customer_id: final_state_or_None}.
    """

    run_date = run_date or date.today().isoformat()
    results = {}
    for job in PERSONA_JOBS:
        results[job["customer_id"]] = run_one(
            run_date=run_date, force=force,
            reminder_recipient_override=reminder_recipient_override, verbose=verbose, **job,
        )
    return results


if __name__ == "__main__":
    _force = os.environ.get("FORCE_RUN", "").lower() in ("1", "true", "yes")
    _reminder_override = os.environ.get("REMINDER_RECIPIENT_OVERRIDE")
    _run_date = os.environ.get("RUN_DATE")  # None -> defaults to today, per run_one/run_all_personas

    if os.environ.get("RUN_ALL_PERSONAS", "").lower() in ("1", "true", "yes"):
        run_all_personas(run_date=_run_date, force=_force, reminder_recipient_override=_reminder_override)
    else:
        run_one(
            customer_id=os.environ.get("CUSTOMER_ID", "glucometer-vendor"),
            source_type=os.environ.get("SOURCE_TYPE", "hospital_vendor"),
            csv_path=os.environ.get(
                "CSV_PATH", "../ingestion_data/hospital_vendor_2yr_history.csv"
            ),
            tenant_name=os.environ.get("TENANT_NAME", "MedSupply Direct"),
            run_date=_run_date,
            force=_force,
            reminder_recipient_override=_reminder_override,
        )
