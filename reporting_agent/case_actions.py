"""Case-review action store for the Business Operations "cases awaiting review"
flow.

"""


from __future__ import annotations

import json
import os
from datetime import datetime, timezone

def _default_case_actions_path() -> str:
    """Local JSONL under bucket_output for dev; the GCS bucket on Cloud Run.
    """
    explicit = os.environ.get("CASE_ACTIONS_PATH")
    if explicit:
        return explicit
    if os.environ.get("BUCKET_BACKEND", "local").lower() == "gcs":
        bucket = os.environ.get("GCS_BUCKET_NAME")
        if bucket:
            return f"gs://{bucket}/_case_actions/case_actions.json"
    return os.path.join(
        os.path.dirname(__file__), "..", "bucket_output", "_case_actions", "case_actions.json"
    )


# Alongside the pipeline's other cross-run artifacts under bucket_output.
DEFAULT_CASE_ACTIONS_PATH = _default_case_actions_path()


def _is_gcs(path: str) -> bool:
    return path.startswith("gs://")


def _gcs_blob(path: str):
    from google.cloud import storage  # lazy -- local runs never need it
    bucket_name, blob_name = path[len("gs://"):].split("/", 1)
    return storage.Client().bucket(bucket_name).blob(blob_name)

# "under_review" is the implicit default; the store only persists the other two.
VALID_STATUSES = ("under_review", "acknowledged", "escalated")

# Demo reset: acknowledge/escalate actions auto-revert to "awaiting_review"
ACTION_RESET_HOURS = float(os.environ.get("CASE_ACTION_RESET_HOURS", "6"))


def is_action_expired(action: dict) -> bool:
    """True once a stored action's reviewed_at is older than ACTION_RESET_HOURS.
Callers should then treat the case as if no action was ever recorded.
"""

    if ACTION_RESET_HOURS <= 0:
        return False
    reviewed_at = action.get("reviewed_at")
    if not reviewed_at:
        return False
    try:
        reviewed = datetime.fromisoformat(reviewed_at)
    except ValueError:
        return False
    age_hours = (datetime.now(timezone.utc) - reviewed).total_seconds() / 3600
    return age_hours >= ACTION_RESET_HOURS


def _key(persona: str, case_id: str) -> str:
    """Stable dict key for a (persona, case_id) pair. A case_id is already
collision-free within a persona (see top_risk._case_id), but two different
"""

    return f"{persona}::{case_id}"


def _load_all(path: str) -> dict:
    """Read the whole store as a {key: record} dict, or {} if it doesn't exist
yet. Kept private -- callers use the three public functions below.
"""

    if _is_gcs(path):
        blob = _gcs_blob(path)
        if not blob.exists():
            return {}
        data = json.loads(blob.download_as_text())
        return data.get("records", {})

    if not os.path.exists(path):
        return {}
    with open(path) as f:
        data = json.load(f)
    return data.get("records", {})


def _write_all(path: str, records: dict) -> None:
    payload = json.dumps({"records": records}, indent=2, default=str)
    if _is_gcs(path):
        _gcs_blob(path).upload_from_string(payload, content_type="application/json")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(payload)


def load_case_action(persona: str, case_id: str, path: str | None = None) -> dict | None:
    """Return the stored action record for one case, or None if the case has never
been acted on (i.e. it is still implicitly "under_review"). Read-only.
"""

    path = path or DEFAULT_CASE_ACTIONS_PATH
    return _load_all(path).get(_key(persona, case_id))


def save_case_action(
    persona: str,
    case_id: str,
    status: str,
    escalation: dict | None = None,
    path: str | None = None,
) -> dict:
    """Upsert the action for one case and return the record just written.

"""

    if status not in VALID_STATUSES:
        raise ValueError(
            f"save_case_action: invalid status {status!r}; expected one of {VALID_STATUSES}."
        )
    path = path or DEFAULT_CASE_ACTIONS_PATH
    records = _load_all(path)
    record = {
        "persona": persona,
        "case_id": case_id,
        "status": status,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "escalation": escalation,
    }
    records[_key(persona, case_id)] = record
    _write_all(path, records)
    return record


def all_case_actions(persona: str | None = None, path: str | None = None) -> list[dict]:
    """Every stored action, or just one persona's when `persona` is given. Returns a
plain list of the record dicts (order is not significant). Read-only.
"""

    path = path or DEFAULT_CASE_ACTIONS_PATH
    records = _load_all(path).values()
    if persona is not None:
        return [r for r in records if r.get("persona") == persona]
    return list(records)
