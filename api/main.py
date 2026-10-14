"""HTTP API over the Reporting Agent's export layer.

Thin FastAPI shell over existing exports.py functions, plus the UI at "/".
"""


from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from datetime import date
import httpx

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# Sibling agent dirs use bare imports, so they must be on sys.path first --
# same convention orchestrator/nodes.py already uses.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPORTING_AGENT = os.path.join(_HERE, "..", "reporting_agent")
_GOVERNANCE = os.path.join(_HERE, "..", "governance")
# risk_agent / aml_agent / ingestion_agent are needed by the escalate endpoint.
_RISK_AGENT = os.path.join(_HERE, "..", "risk_agent")
_AML_AGENT = os.path.join(_HERE, "..", "aml_agent")
_INGESTION_AGENT = os.path.join(_HERE, "..", "ingestion_agent")
for _p in (_REPORTING_AGENT, _GOVERNANCE, _RISK_AGENT, _AML_AGENT, _INGESTION_AGENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import exports  # noqa: E402
import contact_store  # noqa: E402
import templates  # noqa: E402
import top_risk  # noqa: E402
import case_actions  # noqa: E402
from risk_agent import RiskAgent  # noqa: E402
from aml_agent import AMLAgent  # noqa: E402
from governance import GovernanceMiddleware  # noqa: E402
from llm_client import (  # noqa: E402
    OllamaClient, GeminiClient, VertexGeminiClient, MockLlamaClient,
)


def _resolve_bucket_root() -> str:
    """Bucket root resolution, mirroring orchestrator/nodes.py::_build_bucket_backend()'s
env-var-driven BUCKET_BACKEND=local|gcs swap pattern used everywhere else in
"""

    backend = os.environ.get("BUCKET_BACKEND", "local").lower()
    if backend == "gcs":
        local_mirror = os.environ.get("BUCKET_LOCAL_MIRROR")
        if local_mirror:
            return local_mirror
        bucket_name = os.environ.get("GCS_BUCKET_NAME")
        if bucket_name:
            return f"gs://{bucket_name}"
        raise RuntimeError("GCS_BUCKET_NAME is required when BUCKET_BACKEND=gcs")
    return os.environ.get("BUCKET_ROOT", os.path.join(_HERE, "..", "bucket_output"))


BUCKET_ROOT = _resolve_bucket_root()

# The three valid personas, sourced from contact_store as the single source
# of truth. Any /api/{persona}/... route validates against this.
VALID_PERSONAS = set(contact_store.PERSONA_OWNERS)

_ORCHESTRATOR_DIR = os.path.join(_HERE, "..", "orchestrator")


def _ensure_todays_snapshot() -> None:
    """Kick off a pipeline run for today's date for every persona that doesn't
    already have one, the moment this service starts (covers both `uvicorn`
    started locally and a Cloud Run Service revision starting up).
    """
    if os.environ.get("SKIP_STARTUP_PIPELINE_RUN", "").lower() in ("1", "true", "yes"):
        return

    def _run() -> None:
        try:
            subprocess.run(
                [sys.executable, "run_orchestrator.py"],
                cwd=_ORCHESTRATOR_DIR,
                env={**os.environ, "RUN_ALL_PERSONAS": "1"},
                check=False,
            )
        except Exception as e:  # pragma: no cover -- best-effort, never crash the API
            print(f"[api] startup pipeline run failed: {e}", file=sys.stderr)

    threading.Thread(target=_run, daemon=True).start()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _ensure_todays_snapshot()
    yield


app = FastAPI(
    title="KapitalWerkNxt Read API",
    description="Read-only HTTP surface over reporting_agent/exports.py for the Standalone UI.",
    version="1.0.0",
    lifespan=_lifespan,
)

# Permissive CORS so a UI opened as a local file:// document (Origin: null)
# can still fetch() from here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # must be False when allow_origins=["*"] echoes "*"
    allow_methods=["GET", "POST", "OPTIONS"],  # POST added for acknowledge/escalate
    allow_headers=["*"],
)


# Serve the Standalone UI at "/" so the API and UI share one Cloud Run
# Service. The file self-bundles React/ReactDOM/Babel from a CDN at runtime.
_STANDALONE_UI_PATH = os.path.join(_HERE, "..", "standalone-ui.html")


@app.get("/", include_in_schema=False)
def serve_standalone_ui() -> FileResponse:
    """Serve the bundled Standalone UI at the service root."""
    return FileResponse(_STANDALONE_UI_PATH, media_type="text/html")


@app.post("/api/run")
async def api_run(payload: dict):
    """Invoke the long-running agent pipeline via the agent Cloud Run service.

    The client should POST the same JSON payload the agent expects, e.g.
    `{ "customer_id": "glucometer-vendor", "source_type": "hospital_vendor", "csv_path": "gs://..." }`.
    """
    try:
        result = await _invoke_agent(payload)
        return result
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e.response.text))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# On-demand LLM path for the escalate endpoint: this service needs its own
# governance-wrapped client, separate from the batch pipeline's.
def _build_llm_client():
    """Env-driven provider selection, identical to orchestrator/nodes.py.
"""
    provider = os.environ.get("LLM_PROVIDER", "mock").lower()
    if provider == "mock":
        return MockLlamaClient()
    if provider == "vertex":
        return VertexGeminiClient(model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"))
    if provider == "gemini":
        return GeminiClient(model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"))
    return OllamaClient(
        model=os.environ.get("OLLAMA_MODEL", "llama3"),
        host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
    )


def _build_governance_stores():
    """Cache/audit backend selection, identical to orchestrator/nodes.py.
"""
    backend = os.environ.get("GOVERNANCE_STORE", "memory").lower()
    if backend == "firestore":
        try:
            from cache_store import FirestoreCacheStore   # noqa: E402
            from audit_store import FirestoreAuditStore   # noqa: E402
            return FirestoreCacheStore(), FirestoreAuditStore()
        except Exception as e:
            # Fail open to the local audit log rather than crashing the API:
            # governance must keep recording/displaying LLM calls even if the
            # Firestore database is missing/misconfigured.
            print(f"[governance] GOVERNANCE_STORE=firestore but Firestore is "
                  f"unavailable -- falling back to in-memory cache + local "
                  f"JSONL audit log. Cause: {e}", file=sys.stderr)
    return None, None


async def _invoke_agent(payload: dict) -> dict:
    """Forward the given payload to the agent service's /run endpoint.

    Expects `AGENT_URL` env var to contain the full agent service URL
    (for example: https://kapitalwerk-agent-xyz.a.run.app).
    """
    agent_url = os.environ.get("AGENT_URL")
    if not agent_url:
        raise RuntimeError("AGENT_URL environment variable is not set")

    url = agent_url.rstrip("/") + "/run"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload)
    resp.raise_for_status()
    return resp.json()


_ESCALATION_AUDIT_LOG = os.path.join(_HERE, "..", "bucket_output", "_audit", "aml_audit.jsonl")
_GUARDRAILS_PATH = os.path.join(_HERE, "..", "risk_agent", "guardrails.txt")

_cache_store, _audit_store = _build_governance_stores()
_ESCALATION_GOVERNANCE = GovernanceMiddleware(
    _build_llm_client(),
    audit_log_path=_ESCALATION_AUDIT_LOG,
    max_calls_per_run={"aml_agent": 5000, "risk_agent": 5000, "marketing_agent": 5000},
    cache_store=_cache_store,
    audit_store=_audit_store,
)


def _require_persona(persona: str) -> None:
    """Validate a path persona against the three known customers; 404 otherwise.
"""
    if persona not in VALID_PERSONAS:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unknown persona '{persona}'. Valid personas are: "
                f"{', '.join(sorted(VALID_PERSONAS))}."
            ),
        )


def _default_today(persona: str) -> str:
    """Sensible default for `today` on notification queries: the persona's most
recent snapshot date if it has one, else the real calendar today. Matches
"""

    return exports.latest_run_date(BUCKET_ROOT, persona) or date.today().isoformat()


# ---- Endpoints ----

@app.get("/api/personas")
def list_personas() -> list[dict]:
    """The three business customers, so the UI can build its persona switcher
without hardcoding names. {customer_id, owner_name, tenant_name}.
"""

    return [
        {
            "customer_id": persona,
            "owner_name": owner_name,
            "tenant_name": contact_store.PERSONA_TENANTS.get(persona, persona),
        }
        for persona, owner_name in sorted(contact_store.PERSONA_OWNERS.items())
    ]


@app.get("/api/{persona}/counterparties")
def get_counterparties(persona: str, run_date: str | None = None) -> list[dict]:
    """Top-10 counterparties by value for a snapshot (exports.top_counterparties).
"""
    _require_persona(persona)
    return exports.top_counterparties(BUCKET_ROOT, persona, run_date)


@app.get("/api/{persona}/cases")
def get_cases(persona: str, run_date: str | None = None) -> list[dict]:
    """Top-10 flagged cases awaiting review (exports.cases_awaiting_review).
"""
    _require_persona(persona)
    return exports.cases_awaiting_review(BUCKET_ROOT, persona, run_date)


# ---- Case acknowledge / escalate (write + on-demand-LLM endpoints) ----

def _is_gcs_path(path: str) -> bool:
    return isinstance(path, str) and path.startswith("gs://")


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"Malformed GCS URI: {uri!r}")
    without_scheme = uri[len("gs://"):]
    if "/" not in without_scheme:
        raise ValueError(f"Malformed GCS URI: {uri!r}")
    bucket, blob = without_scheme.split("/", 1)
    return bucket, blob


def _gcs_client():
    from google.cloud import storage
    return storage.Client()


def _read_gcs_text(path: str) -> str:
    bucket_name, blob_name = _parse_gcs_uri(path)
    client = _gcs_client()
    blob = client.bucket(bucket_name).blob(blob_name)
    return blob.download_as_text()


def _exists(path: str) -> bool:
    if _is_gcs_path(path):
        bucket_name, blob_name = _parse_gcs_uri(path)
        return _gcs_client().bucket(bucket_name).blob(blob_name).exists()
    return os.path.exists(path)


def _load_path_json(path: str, default=None):
    if not _exists(path):
        return default
    if _is_gcs_path(path):
        return json.loads(_read_gcs_text(path))
    with open(path) as f:
        return json.load(f)


def _latest_snapshot_dir(persona: str) -> str | None:
    run_date = exports.latest_run_date(BUCKET_ROOT, persona)
    if not run_date:
        return None
    return os.path.join(BUCKET_ROOT, persona, run_date)


def _base_cases(prefix: str) -> list[dict]:
    """The un-merged top-risk case entries for a snapshot (case_id, flag_type,
customer_reference_id, ...). Mirrors exports.cases_awaiting_review's source.

Uses the GCS-aware _exists/_load_path_json helpers (not bare os.path.exists/
open) since `prefix` is a gs:// URI in the deployed environment -- plain
os.path calls on a gs:// string are always False, which previously made this
always return [] against a real bucket and 404 every acknowledge/escalate.
"""

    report_path = os.path.join(prefix, "top_risk_report.json")
    report = _load_path_json(report_path)
    if report is not None:
        return report.get("top_transactions", [])
    flags_path = os.path.join(prefix, "inflow_flags.json")
    flags = _load_path_json(flags_path)
    if flags is None:
        return []
    norm_path = os.path.join(prefix, "normalized_transactions.json")
    transactions = _load_path_json(norm_path, default=[])
    return top_risk.build_top_risk_report(flags, transactions)


def _lookup_case_entry(persona: str, case_id: str) -> dict:
    """Return the top-risk entry for a case_id in the persona's LATEST snapshot,
or raise 404. A stale/unknown case_id must not silently succeed.
"""

    prefix = _latest_snapshot_dir(persona)
    if not prefix:
        raise HTTPException(status_code=404, detail=f"No snapshot found for persona '{persona}'.")
    for entry in _base_cases(prefix):
        if entry.get("case_id") == case_id:
            return entry
    raise HTTPException(
        status_code=404,
        detail=(f"Case '{case_id}' not found in the latest snapshot for persona "
                f"'{persona}'. It may be stale -- re-fetch /api/{persona}/cases."),
    )


def _load_json(prefix: str, name: str, default):
    # GCS-aware: prefix is a gs:// URI in the deployed environment, where bare
    # os.path.exists/open always fail silently. See _base_cases for the same fix.
    path = os.path.join(prefix, name)
    return _load_path_json(path, default)


def _find_original_flag(inflow_flags: dict, entry: dict) -> dict | None:
    """Re-find the exact Inflow flag a case came from, using the entry's
flag_type + customer_reference_id (+ transaction_reference for high-value).
"""

    cust = entry.get("customer_reference_id")
    if entry.get("flag_type") == "deviation":
        # deviations are one per counterparty in a run
        for f in inflow_flags.get("deviations", []):
            if f.get("customer_reference_id") == cust:
                return f
        return None
    ref = entry.get("transaction_reference")
    for f in inflow_flags.get("high_value_transactions", []):
        if f.get("customer_reference_id") == cust and f.get("reference_id") == ref:
            return f
    return None


@app.post("/api/{persona}/cases/{case_id}/acknowledge")
def acknowledge_case(persona: str, case_id: str) -> dict:
    """Record a simple 'acknowledged' status for a case. No LLM call. 404 if the
case_id isn't in the persona's latest snapshot.
"""

    _require_persona(persona)
    _lookup_case_entry(persona, case_id)  # 404s on unknown case_id
    return case_actions.save_case_action(persona, case_id, "acknowledged")


@app.post("/api/{persona}/cases/{case_id}/escalate")
def escalate_case(persona: str, case_id: str) -> dict:
    """Escalate a flagged case for real: re-run the Risk Agent (through its
fail-closed PII scan) and, if clear, the AML Agent scoped to just this one
"""

    _require_persona(persona)
    entry = _lookup_case_entry(persona, case_id)  # 404s on unknown case_id

    prefix = _latest_snapshot_dir(persona)
    inflow_flags = _load_json(prefix, "inflow_flags.json",
                              {"deviations": [], "high_value_transactions": []})
    transactions = _load_json(prefix, "normalized_transactions.json", [])
    historical_stats = _load_json(prefix, "historical_stats.json", {})

    flag = _find_original_flag(inflow_flags, entry)
    if flag is None:
        raise HTTPException(
            status_code=404,
            detail=(f"Case '{case_id}' has no matching Inflow flag in the latest "
                    f"snapshot for persona '{persona}'."),
        )

    # (1) fresh Risk Agent assessment -- SAME fail-closed PII scan risk_node uses.
    risk_agent = RiskAgent(_ESCALATION_GOVERNANCE, guardrails_path=_GUARDRAILS_PATH)
    risk = risk_agent.assess(
        inflow_flags,
        total_counterparties=len(historical_stats or {}),
        transactions=transactions,
        verbose=False,
    )

    # (2) fail closed on PII -- do NOT proceed to AML, mirroring risk_node/should_run_aml.
    if risk.get("pii_detected"):
        escalation = {
            "ran": False,
            "risk_tier": risk["risk_tier"],
            "risk_reasoning": risk["reasoning"],
            "aml_case_narrative": None,
            "aml_trace_id": None,
        }
        return case_actions.save_case_action(persona, case_id, "escalated", escalation=escalation)

    # (3) AML Agent, scoped to JUST this one flag, in the bucket shape it expects,
    # with risk_context built exactly like aml_node does.
    scoped = {"deviations": [], "high_value_transactions": []}
    if entry.get("flag_type") == "deviation":
        scoped["deviations"] = [flag]
    else:
        scoped["high_value_transactions"] = [flag]

    risk_context = {
        "risk_tier": risk["risk_tier"],
        "risk_reasons": [risk["reasoning"]],
        "risk_signals": risk["signals"],
    }
    aml_cases = AMLAgent(_ESCALATION_GOVERNANCE).process_flags(
        scoped, risk_context=risk_context, verbose=False)
    aml_case = aml_cases[0] if aml_cases else None

    escalation = {
        "ran": True,
        "risk_tier": risk["risk_tier"],
        "risk_reasoning": risk["reasoning"],
        "aml_case_narrative": aml_case["narrative"] if aml_case else None,
        "aml_trace_id": aml_case["trace_id"] if aml_case else None,
    }
    return case_actions.save_case_action(persona, case_id, "escalated", escalation=escalation)


@app.get("/api/{persona}/notifications")
def get_notifications(persona: str, today: str | None = None) -> list[dict]:
    """Last-4-weeks top-10 upcoming/missed notifications
(exports.recent_notifications). `today` defaults to the persona's latest
"""

    _require_persona(persona)
    today = today or _default_today(persona)
    return exports.recent_notifications(BUCKET_ROOT, persona, today)


@app.get("/api/{persona}/marketing-heatmap")
def get_marketing_heatmap(persona: str, run_date: str | None = None) -> list[dict]:
    """The marketing agent's per-postal-code heatmap (exports.marketing_heatmap).
"""
    _require_persona(persona)
    return exports.marketing_heatmap(BUCKET_ROOT, persona, run_date)


@app.get("/api/{persona}/dashboard")
def get_dashboard(persona: str) -> dict:
    """Everything a per-persona overview screen needs in one call
(exports.dashboard).
"""

    _require_persona(persona)
    return exports.dashboard(BUCKET_ROOT, persona)


@app.get("/api/governance/audit-summary")
def get_audit_summary() -> dict:
    """Rollup of the cross-persona governance audit trail
(exports.governance_audit_summary). NOT persona-scoped.
"""

    return exports.governance_audit_summary()


@app.get("/api/governance/audit-log")
def get_audit_log(limit: int = 50) -> list[dict]:
    """Individual audit events, newest first (exports.governance_audit_log).
"""

    return exports.governance_audit_log(limit=limit)


@app.get("/api/{persona}/reminder-preview")
def get_reminder_preview(persona: str) -> dict:
    """What an "email preview" modal should show for this persona's weekly reminder.

"""

    _require_persona(persona)
    record = contact_store.load_contact_directory(customer_id=persona)

    owner_name = record.get("owner_name") or contact_store.PERSONA_OWNERS.get(persona, persona)
    tenant_name = record.get("tenant_name") or contact_store.PERSONA_TENANTS.get(persona, persona)
    to_address_masked = record.get("email_masked", "")

    # Built from the real template so the preview matches what gets sent,
    # with placeholder (not real) aggregate numbers.
    subject_example = templates.build_weekly_upcoming_summary_email(
        owner_name=owner_name,
        tenant_name=tenant_name,
        contributors=[],
        trace="preview",
        from_address=os.environ.get("REMINDER_FROM_ADDRESS", "onboarding@resend.dev"),
        to_address=to_address_masked,
    )["subject"]

    return {
        "customer_id": persona,
        "owner_name": owner_name,
        "tenant_name": tenant_name,
        "to_address_masked": to_address_masked,
        "subject_example": subject_example,
    }
