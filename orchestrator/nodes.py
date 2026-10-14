"""Graph nodes. Each node is a plain function: (state) -> partial state update.
LangGraph merges the returned dict into the shared PipelineState.

"""


import json
import os
import sys
from pathlib import Path

# the real agents live as siblings to this orchestrator folder -- reuse
# their code directly instead of duplicating logic here
_ORCHESTRATOR_DIR = Path(__file__).resolve().parent
for sibling in (
    "../ingestion_agent",
    "../inflow_agent",
    "../risk_agent",
    "../aml_agent",
    "../marketing_agent",
    "../governance",
    "../reporting_agent",
):
    path = (_ORCHESTRATOR_DIR / sibling).resolve()
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

_BUCKET_OUTPUT_DIR = str((_ORCHESTRATOR_DIR / ".." / "bucket_output").resolve())
_RISK_AGENT_GUARDRAILS = str((_ORCHESTRATOR_DIR / ".." / "risk_agent" / "guardrails.txt").resolve())

from ingestion_agent import IngestionAgent            # noqa: E402
from storage import LocalBucket, GCSBucket             # noqa: E402
from inflow_agent import InflowAgent                  # noqa: E402
from risk_agent import RiskAgent                      # noqa: E402
from aml_agent import AMLAgent                        # noqa: E402
from governance import GovernanceMiddleware           # noqa: E402
from multi_client_provider import get_llm_client  # noqa: E402  (MCP factory)


class _MirrorBucket:
    """Writes every blob to BOTH a LocalBucket and a GCSBucket.

"""


    def __init__(self, local: "LocalBucket", gcs: "GCSBucket"):
        self.local = local
        self.gcs = gcs

    def write_json(self, blob_path: str, data) -> str:
        self.local.write_json(blob_path, data)
        return self.gcs.write_json(blob_path, data)

    def read_json(self, blob_path: str):
        try:
            print(f"[_MirrorBucket] reading {blob_path} from GCS first...")
            return self.gcs.read_json(blob_path)
        except Exception:
            print(f"[_MirrorBucket] reading {blob_path} from local storage...")
            return self.local.read_json(blob_path)


def _build_bucket_backend(bucket_root_dir: str):
    """Bucket backend selection, mirroring _build_llm_client()'s and
_build_governance_stores()'s env-var-driven factory pattern so the same
"""

    backend = os.environ.get("BUCKET_BACKEND", "local").lower()
    if backend == "gcs":
        return _MirrorBucket(
            LocalBucket(root_dir=bucket_root_dir),
            GCSBucket(bucket_name=os.environ["GCS_BUCKET_NAME"]),
        )
    return LocalBucket(root_dir=bucket_root_dir)


def _build_governance_stores():
    """Cache/audit backend selection, mirroring _build_llm_client()'s
provider-selection pattern:
"""

    from cache_store import LocalFileCacheStore
    
    backend = os.environ.get("GOVERNANCE_STORE", "memory").lower()
    if backend == "firestore":
        try:
            from cache_store import FirestoreCacheStore   # noqa: E402
            from audit_store import FirestoreAuditStore   # noqa: E402
            return FirestoreCacheStore(), FirestoreAuditStore()
        except Exception as e:
            # Fail open to the local audit log rather than crashing the whole
            # pipeline: governance must keep recording LLM calls even if the
            # Firestore database is missing/misconfigured.
            print(f"[governance] GOVERNANCE_STORE=firestore but Firestore is "
                  f"unavailable -- falling back to local file cache + local "
                  f"JSONL audit log. Cause: {e}", file=sys.stderr)
            return LocalFileCacheStore(os.path.join(_BUCKET_OUTPUT_DIR, "_audit", "llm_cache.json")), None
    return LocalFileCacheStore(os.path.join(_BUCKET_OUTPUT_DIR, "_audit", "llm_cache.json")), None


# One governance-wrapped client per process, shared by both LLM-calling
# agents, each with its own independent budget.
# The MCP factory (multi_client_provider.get_llm_client) resolves the provider
# from LLM_PROVIDER env var, falling back to ENV=dev → ollama or production → vertex.
_cache_store, _audit_store = _build_governance_stores()
_governance = GovernanceMiddleware(
    get_llm_client(),
    audit_log_path=os.path.join(_BUCKET_OUTPUT_DIR, "_audit", "aml_audit.jsonl"),
    cache_store=_cache_store,
    audit_store=_audit_store,
)

# Track per-agent run telemetry for the enriched /run API response.
import time as _time  # noqa: E402 (module-level import after conditional path adds)
_agent_run_log: list[dict] = []


def _track_node(name: str, fn, state: dict) -> dict:
    """Wrap a node function with telemetry capture for the /run API response."""
    _agent_run_log.append({"agent": name, "status": "running", "started_at": _time.time()})
    idx = len(_agent_run_log) - 1
    t0 = _time.time()
    try:
        result = fn(state)
        gov_snap = _governance.run_summary(name.replace("_node", "_agent"))
        _agent_run_log[idx].update({
            "status": "succeeded",
            "latency_ms": int((_time.time() - t0) * 1000),
            "llm_calls": gov_snap.get("calls_made", 0),
            "tokens_used": gov_snap.get("tokens_used", 0),
            "cache_hits": gov_snap.get("cache_hits", 0),
        })
        return result
    except Exception as exc:
        _agent_run_log[idx].update({
            "status": "failed",
            "latency_ms": int((_time.time() - t0) * 1000),
            "error": str(exc),
        })
        raise


def get_agent_run_log() -> list[dict]:
    """Return the telemetry log accumulated during the most recent pipeline run."""
    return list(_agent_run_log)


def reset_agent_run_log() -> None:
    """Clear telemetry between pipeline runs (called by app.py before each /run)."""
    _agent_run_log.clear()


def ingestion_node(state: dict) -> dict:

    print("[ingestion_node] starting...")
    bucket = _build_bucket_backend(_BUCKET_OUTPUT_DIR)
    agent = IngestionAgent(bucket)
    summary = agent.run(state["csv_path"], state["source_type"], state["customer_id"], state.get("run_date"))

    # Re-read what was just written so downstream nodes get it via state.
    transactions, stats = InflowAgent(bucket).load(bucket, state["customer_id"], summary["run_date"])

    print(f"[ingestion_node] done -- {summary['accepted_rows']} rows accepted")
    return {
        "ingestion_summary": summary,
        "transactions": transactions,
        "historical_stats": stats,
        "run_date": summary["run_date"],
    }


def inflow_node(state: dict) -> dict:
    print("[inflow_node] starting...")
    from datetime import date
    bucket = _build_bucket_backend(_BUCKET_OUTPUT_DIR)
    agent = InflowAgent(bucket)
    summary = agent.run(bucket, state["customer_id"], state["run_date"], as_of=date.today())

    import json
    output_path = summary["output"]
    if output_path.startswith("gs://"):
        from google.cloud import storage
        bucket_name, blob_name = output_path[len("gs://"):].split("/", 1)
        client = storage.Client()
        blob = client.bucket(bucket_name).blob(blob_name)
        flags = json.loads(blob.download_as_text())
    else:
        with open(output_path) as f:
            flags = json.load(f)

    n_flags = len(flags["missing_payments"]) + len(flags["deviations"]) + len(flags["high_value_transactions"])
    print(f"[inflow_node] done -- {n_flags} total flags "
          f"({len(flags['deviations'])} deviations, {len(flags['high_value_transactions'])} high-value, "
          f"{len(flags['missing_payments'])} missing)")
    return {"inflow_flags": flags}


def risk_node(state: dict) -> dict:
    """LLM-based, via the governance-wrapped client -- applies guardrails.txt
(editable policy, no code deploy needed) to the deterministic counts
"""

    print("[risk_node] starting...")
    agent = RiskAgent(_governance, guardrails_path=_RISK_AGENT_GUARDRAILS)
    total_counterparties = len(state.get("historical_stats") or {})
    # Raw transactions too, so the PII scan covers what AML/Marketing will see.
    result = agent.assess(state["inflow_flags"], total_counterparties=total_counterparties,
                          transactions=state.get("transactions"))

    print(f"[risk_node] done -- tier={result['risk_tier']} "
          f"(llm_available={result['llm_available']}, parse_failed={result['parse_failed']})")
    if result.get("pii_detected"):
        print(f"[risk_node] PII BLOCK -- AML and Marketing will be skipped this run; "
              f"{len(result.get('records_to_remask', []))} record(s) marked for re-masking.")

    return {
        "risk_score": result["risk_tier"],
        "risk_reasons": [result["reasoning"]],
        "risk_signals": result["signals"],
        "pii_blocked": result.get("aml_blocked", False),
        "pii_findings": result.get("pii_findings", []),
        "records_to_remask": result.get("records_to_remask", []),
    }


def should_run_aml(state: dict) -> str:
    """Conditional edge: only invoke the AML agent (and therefore only spend
any LLM tokens at all) if Inflow actually surfaced something worth
"""

    # Fail-closed: skip AML entirely if Risk already detected PII.
    if state.get("pii_blocked"):
        return "reporting_node"

    flags = state["inflow_flags"]
    if flags["deviations"] or flags["high_value_transactions"]:
        return "aml_node"
    return "reporting_node"


def aml_node(state: dict) -> dict:
    """Delegates to the standalone, tested AMLAgent, which routes every call
through the governance middleware (token metering, budget/stop, cache,
"""

    flags = state["inflow_flags"]
    total = len(flags["deviations"]) + len(flags["high_value_transactions"])
    print(f"[aml_node] starting -- {total} case(s) to process via Llama 3, "
          f"this can take a while on local CPU inference...")

    # Hand AML the Risk Agent's assessment so each narrative can cite it.
    risk_context = {
        "risk_tier": state.get("risk_score"),
        "risk_reasons": state.get("risk_reasons", []),
        "risk_signals": state.get("risk_signals", {}),
    }
    agent = AMLAgent(_governance)
    cases = agent.process_flags(flags, risk_context=risk_context)

    aml_summary = _governance.run_summary("aml_agent")
    print(f"[aml_node] done -- {len(cases)} case(s) processed "
          f"({aml_summary['calls_made']} LLM calls, {aml_summary['tokens_used']} tokens, "
          f"{aml_summary['cache_hits']} cache hits)")
    if aml_summary["stopped_early"]:
        print(f"[aml_node] NOTE: stopped early -- {aml_summary['stop_reason']}. "
              f"{total - len(cases)} case(s) were not processed this run.")

    return {"aml_cases": cases, "governance_summary": _governance.run_summary()}


def _build_reminder_notifier():
    """Same env-driven backend-selection pattern as _build_llm_client() above.
Defaults to ConsoleEmailNotifier so running the full demo pipeline
"""

    from email_client import ConsoleEmailNotifier, ResendEmailNotifier

    if os.environ.get("REMINDER_NOTIFIER", "console") == "resend":
        return ResendEmailNotifier()
    return ConsoleEmailNotifier()


def reporting_node(state: dict) -> dict:
    """Runs BOTH weekly reminder checks for real, not just Friday's. Per the
original product spec: "Beginning of the week -> accumulated amount
"""

    from weekly_reminders import run_friday, run_monday
    from contact_store import load_contact_directory
    from top_risk import write_top_risk_report

    notifier = _build_reminder_notifier()

    # This persona's contact record; recipient_override still wins if set.
    contact_directory = load_contact_directory(customer_id=state["customer_id"])
    bucket = _build_bucket_backend(_BUCKET_OUTPUT_DIR)
    common_kwargs = dict(
        bucket=bucket,
        customer_id=state["customer_id"],
        run_date=state["run_date"],
        tenant_name=state.get("tenant_name", state["customer_id"]),
        from_address=os.environ.get("REMINDER_FROM_ADDRESS", "onboarding@resend.dev"),
        notifier=notifier,
        contact_directory=contact_directory,
        recipient_override=state.get("reminder_recipient_override"),
    )

    upcoming_results = run_monday(**common_kwargs)
    missed_results = run_friday(**common_kwargs)
    # 0 or 1 record each; tag "kind" here so reminder_node can tell them apart.
    for r in upcoming_results:
        r["kind"] = "monday_upcoming"
    for r in missed_results:
        r["kind"] = "friday_missed"
    results = upcoming_results + missed_results

    upcoming_sent = sum(1 for r in upcoming_results if r["status"] == "sent")
    missed_sent = sum(1 for r in missed_results if r["status"] == "sent")
    skipped = sum(1 for r in results if r["status"] == "skipped_no_recipient")
    missed_counterparties = missed_results[0]["counterparty_count"] if missed_results else 0

    # additive structured artifact: the run's top-5 highest-risk transactions,
    # ranked, for a future UI/API to render (does not replace report_text).
    bucket = _build_bucket_backend(_BUCKET_OUTPUT_DIR)  
    top_risk_path = write_top_risk_report(
        bucket=bucket,
        customer_id=state["customer_id"],
        run_date=state["run_date"],
        inflow_flags=state["inflow_flags"],
        run_risk_tier=state.get("risk_score"),
    )

    text = (
        f"Risk tier: {state['risk_score']}. Reasons: {'; '.join(state['risk_reasons']) or 'none'}. "
        f"AML cases opened: {len(state.get('aml_cases', []))}. "
        f"Missing payments: {missed_counterparties} counterpart(y/ies) -- "
        f"{upcoming_sent} upcoming-payment email(s) sent, "
        f"{missed_sent} missed-payment email(s) sent, "
        f"{skipped} skipped (no recipient on file)."
    )
    return {"report_text": text, "reminder_results": results, "top_risk_report": top_risk_path}


def marketing_node(state: dict) -> dict:
    """LLM-based, via the same governance-wrapped client Risk and AML use. Builds
the per-postal-code marketing heatmap from this run's ingested transactions
"""

    print("[marketing_node] starting...")
    from marketing_agent import MarketingAgent

    # Fail-closed, same PII gate as Risk/AML. An in-node guard, not a graph
    # edge, since the block still needs its own audit entry + heatmap.
    pii_blocked = bool(state.get("pii_blocked"))
    if pii_blocked:
        print("[marketing_node] PII BLOCK -- Marketing LLM call skipped this run "
              "(fail-closed); heatmap annotated deterministically only.")
    bucket = _build_bucket_backend(_BUCKET_OUTPUT_DIR)
    agent = MarketingAgent(_governance)
    summary = agent.run(bucket, state["customer_id"], state["run_date"],
                        pii_blocked=pii_blocked)
    print(f"[marketing_node] done -- {summary['postal_code_count']} postal code(s) annotated "
          f"(llm_available={summary['llm_available']}, pii_blocked={summary['pii_blocked']})")
    return {"marketing_summary": summary}


def reminder_node(state: dict) -> dict:
    """reporting_node already dispatched the real email (Friday check) -- this
node just reshapes that result into the reminders_queued shape existing
"""

    results = state.get("reminder_results", [])
    queued = [
        {
            "customer_id": r["customer_id"],
            "channel": "email",
            "reason": "upcoming_payment_summary" if r.get("kind") == "monday_upcoming" else "missing_payment_summary",
            "counterparty_count": r.get("counterparty_count", 0),
            "status": r["status"],
        }
        for r in results
    ]
    return {"reminders_queued": queued}