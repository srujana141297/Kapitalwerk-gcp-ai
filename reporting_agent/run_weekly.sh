#!/usr/bin/env bash
# Single entry point for the two Cloud Scheduler triggers -- see DEPLOY.md.
#
#   MONDAY:  fresh ingestion (this week's baseline) + "upcoming payment" emails.
#            Doesn't need current-week transactions yet, so it doesn't run
#            the full risk/AML pipeline -- just enough to know who to expect
#            payment from and how much.
#   FRIDAY:  the full pipeline (ingestion -> inflow -> risk -> AML ->
#            reporting -> reminder). reporting_node already dispatches the
#            real "missed payment" emails as part of this run -- see
#            orchestrator/nodes.py.
#
# Usage:  ./run_weekly.sh monday   (or)   ./run_weekly.sh friday
#
# Required env vars (see DEPLOY.md for where these come from in Cloud Run):
#   RESEND_API_KEY, RESEND_FROM_EMAIL              -- real send credentials
#   CUSTOMER_ID       (default: glucometer-vendor)
#   TENANT_NAME       (default: MedSupply Direct)
#   CSV_PATH          (default: ../ingestion_data/hospital_vendor_2yr_history.csv)
#   SOURCE_TYPE       (default: hospital_vendor)
#   REMINDER_RECIPIENT_OVERRIDE  -- optional, see reporting_agent/weekly_reminders.py.
#                                   Leave unset once a real contact_directory exists.

set -euo pipefail

MODE="${1:?usage: run_weekly.sh monday|friday}"
CUSTOMER_ID="${CUSTOMER_ID:-glucometer-vendor}"
TENANT_NAME="${TENANT_NAME:-MedSupply Direct}"
CSV_PATH="${CSV_PATH:-../ingestion_data/hospital_vendor_2yr_history.csv}"
SOURCE_TYPE="${SOURCE_TYPE:-hospital_vendor}"
NOTIFIER="${REMINDER_NOTIFIER:-resend}"

cd "$(dirname "$0")"

case "$MODE" in
  monday)
    echo "[run_weekly] Monday -- refreshing ingestion output, then sending upcoming-payment emails"
    RUN_DATE=$(date +%F)
    # run_ingestion.py is a hardcoded 3-tenant demo script with no CLI args --
    # call IngestionAgent directly instead, same way orchestrator/nodes.py's
    # ingestion_node does, so this only touches the one tenant this job cares
    # about.
    python3 - "$CUSTOMER_ID" "$SOURCE_TYPE" "$CSV_PATH" "$RUN_DATE" <<'PYEOF'
import sys
sys.path.insert(0, "../ingestion_agent")
from ingestion_agent import IngestionAgent
from storage import LocalBucket

customer_id, source_type, csv_path, run_date = sys.argv[1:5]
agent = IngestionAgent(LocalBucket(root_dir="../bucket_output"))
summary = agent.run(csv_path, source_type, customer_id, run_date)
print(f"[run_weekly] ingestion done: {summary}")
PYEOF
    python3 weekly_reminders.py --mode monday --customer "$CUSTOMER_ID" --tenant-name "$TENANT_NAME" \
      --run-date "$RUN_DATE" --notifier "$NOTIFIER" ${REMINDER_RECIPIENT_OVERRIDE:+--recipient-override "$REMINDER_RECIPIENT_OVERRIDE"}
    ;;
  friday)
    echo "[run_weekly] Friday -- running full pipeline (ingestion through reminder dispatch)"
    export REMINDER_NOTIFIER="$NOTIFIER"
    (cd ../orchestrator && python3 run_orchestrator.py)
    ;;
  *)
    echo "unknown mode: $MODE (expected monday|friday)" >&2
    exit 1
    ;;
esac
