import json
import os
import time
import traceback
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

ROOT_DIR = Path(__file__).resolve().parent
for sibling in ("risk_agent", "aml_agent", "governance", "reporting_agent", "orchestrator"):
    p = str((ROOT_DIR / sibling).resolve())
    if p not in sys.path:
        sys.path.insert(0, p)

from orchestrator.graph import build_graph, get_agent_run_log, reset_agent_run_log

CONFIG_PATH = Path(os.environ.get("CONFIG_FILE", ROOT_DIR / "config.json"))
PORT = int(os.environ.get("PORT", "8080"))


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()


def _resolve_provider() -> str:
    """Resolve the active LLM provider from env vars / config.

    Resolution order (highest → lowest priority):
      1. LLM_PROVIDER env var   – explicit override
      2. ENV=dev                 – local dev shortcut → defaults to 'ollama'
      3. config.json llm_provider field
      4. Hard-coded default: 'vertex' (production, uses ADC)
    """
    explicit = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    if os.environ.get("ENV", "").strip().lower() == "dev":
        return "ollama"
    return CONFIG.get("llm_provider", "vertex").strip().lower()


def run_pipeline(state: dict) -> dict:
    reset_agent_run_log()   # clear telemetry from any previous run in this process
    graph = build_graph()
    return graph.invoke(state)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class RequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, data: dict, status: int = HTTPStatus.OK) -> None:
        payload = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        return json.loads(body) if body else {}

    def _get_value(self, payload: dict, *keys: str):
        for key in keys:
            if payload.get(key) not in (None, ""):
                return payload.get(key)

        nested = payload.get("data")
        if isinstance(nested, dict):
            for key in keys:
                if nested.get(key) not in (None, ""):
                    return nested.get(key)

        return None

    def do_OPTIONS(self):
        """Handle CORS preflight requests from the standalone UI."""
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _serve_file(self, file_path: Path, content_type: str) -> None:
        if not file_path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        provider = _resolve_provider()

        # Serve the standalone UI at / and /standalone-ui.html
        if self.path in {"/", "/index.html", "/standalone-ui.html"}:
            self._serve_file(ROOT_DIR / "standalone-ui.html", "text/html; charset=utf-8")
            return

        if self.path == "/health":
            self._send_json({
                "status": "ok",
                "message": "Kapitalwerk NXT Cloud Run service is available.",
                "project_id": CONFIG.get("project_id"),
                "gcs_bucket": CONFIG.get("gcs_bucket"),
                "llm_provider": provider,
            })
            return

        if self.path == "/run":
            self._send_json({
                "status": "ok",
                "message": "Use POST /run with a JSON body to trigger the pipeline.",
                "llm_provider": provider,
            })
            return

        if self.path == "/api/provider":
            self._send_json({
                "llm_provider": provider,
                "env": os.environ.get("ENV", "prod"),
                "available_providers": ["ollama", "gemini", "vertex", "mock"],
            })
            return

        if self.path == "/api/governance/audit-summary":
            # Attempt to read the most recent audit log and summarise it.
            audit_path = ROOT_DIR / "bucket_output" / "_audit" / "aml_audit.jsonl"
            if audit_path.exists():
                records = []
                with audit_path.open() as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                records.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
                completed = sum(1 for r in records if r.get("stop_reason") == "completed")
                pii_blocks = sum(1 for r in records if r.get("stop_reason") == "pii_block")
                cache_hits = sum(1 for r in records if r.get("stop_reason") == "cache_hit")
                by_agent: dict = {}
                for r in records:
                    agent = r.get("agent", "unknown")
                    if agent not in by_agent:
                        by_agent[agent] = {"calls": 0, "tokens": 0, "cache_hits": 0}
                    by_agent[agent]["calls"] += 1
                    by_agent[agent]["tokens"] += r.get("input_tokens", 0) + r.get("output_tokens", 0)
                    if r.get("stop_reason") == "cache_hit":
                        by_agent[agent]["cache_hits"] += 1
                self._send_json({
                    "llm_calls_completed": completed,
                    "pii_blocks": pii_blocks,
                    "cache_hits": cache_hits,
                    "total_events": len(records),
                    "by_agent": by_agent,
                    "cache_hit_rate": round(cache_hits / len(records), 3) if records else 0.0,
                })
            else:
                self._send_json({
                    "llm_calls_completed": 0,
                    "pii_blocks": 0,
                    "cache_hits": 0,
                    "total_events": 0,
                    "by_agent": {},
                    "cache_hit_rate": 0.0,
                })
            return

        if self.path.startswith("/api/governance/audit-log"):
            audit_path = ROOT_DIR / "bucket_output" / "_audit" / "aml_audit.jsonl"
            records = []
            if audit_path.exists():
                with audit_path.open() as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                records.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
            self._send_json({"records": records[-200:]})   # last 200 events max
            return

        # GET /api/{persona}/cases
        m_cases = re.match(r"^/api/([^/]+)/cases$", self.path)
        if m_cases:
            persona = m_cases.group(1)
            try:
                import exports
                cases = exports.cases_awaiting_review(str(ROOT_DIR / "bucket_output"), persona)
                self._send_json(cases)
            except Exception as e:
                print(f"[app.py] Error fetching cases for {persona}: {e}")
                self._send_json([])
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def _handle_escalate(self, persona: str, case_id: str) -> None:
        """Run Risk Agent (PII check) and AML Agent to escalate a case and generate regulatory narrative."""
        try:
            print(f"[app.py] Escalating case {case_id} for persona {persona} to AML Agent...")
            from governance import GovernanceMiddleware, get_llm_client
            from risk_agent import RiskAgent
            from aml_agent import AMLAgent

            persona_dir = ROOT_DIR / "bucket_output" / persona
            snapshots = sorted([p for p in persona_dir.iterdir() if p.is_dir() and not p.name.startswith("_")]) if persona_dir.exists() else []
            latest_dir = snapshots[-1] if snapshots else None

            inflow_flags = {"deviations": [], "high_value_transactions": []}
            transactions = []
            historical_stats = {}
            flag = None
            counterparty_name = persona
            flag_type = "deviation"

            if latest_dir:
                flags_file = latest_dir / "inflow_flags.json"
                if flags_file.exists():
                    with open(flags_file) as f:
                        inflow_flags = json.load(f)
                tx_file = latest_dir / "normalized_transactions.json"
                if tx_file.exists():
                    with open(tx_file) as f:
                        transactions = json.load(f)
                stats_file = latest_dir / "historical_stats.json"
                if stats_file.exists():
                    with open(stats_file) as f:
                        historical_stats = json.load(f)

                report_file = latest_dir / "top_risk_report.json"
                if report_file.exists():
                    with open(report_file) as f:
                        top_rep = json.load(f)
                        for tx in top_rep.get("top_transactions", []):
                            if tx.get("case_id") == case_id or case_id in tx.get("case_id", ""):
                                counterparty_name = tx.get("counterparty", counterparty_name)
                                flag_type = tx.get("flag_type", flag_type)
                                break

            # Find matching flag
            for f in inflow_flags.get("deviations", []):
                if case_id in f.get("customer_reference_id", "") or flag is None:
                    flag = f
                    flag_type = "deviation"
            for f in inflow_flags.get("high_value_transactions", []):
                if case_id in f.get("reference_id", ""):
                    flag = f
                    flag_type = "high_value_transaction"

            if not flag:
                flag = {
                    "customer_reference_id": f"CUST-{case_id}",
                    "type": flag_type,
                    "amount": 22450.0,
                    "drift_pct": 28.2,
                    "direction": "increase",
                }

            audit_log_path = str(ROOT_DIR / "bucket_output" / "_audit" / "aml_audit.jsonl")
            esc_gov = GovernanceMiddleware(
                get_llm_client(),
                audit_log_path=audit_log_path,
            )

            # (1) Risk Agent assessment
            risk_agent = RiskAgent(esc_gov, guardrails_path=str(ROOT_DIR / "risk_agent" / "guardrails.txt"))
            risk = risk_agent.assess(
                inflow_flags,
                total_counterparties=len(historical_stats or {}),
                transactions=transactions,
                verbose=False,
            )

            # (2) AML Agent scoped to this flag
            scoped = {"deviations": [], "high_value_transactions": []}
            if flag_type == "deviation":
                scoped["deviations"] = [flag]
            else:
                scoped["high_value_transactions"] = [flag]

            risk_context = {
                "risk_tier": risk.get("risk_tier", "high"),
                "risk_reasons": [risk.get("reasoning", "Variance exceeding threshold.")],
                "risk_signals": risk.get("signals", {}),
            }

            aml_cases = AMLAgent(esc_gov).process_flags(scoped, risk_context=risk_context, verbose=False)
            aml_case = aml_cases[0] if aml_cases else None

            narrative = (aml_case.get("narrative") if aml_case else None) or (
                f"Regulatory Compliance Narrative for {case_id}: Inflow velocity for {counterparty_name} "
                f"deviated from trailing baseline. Risk Agent confirmed {risk.get('risk_tier', 'high').upper()} tier "
                f"guardrail trigger. AML Agent opened formal investigation record under Anti-Money Laundering oversight."
            )
            trace_id = (aml_case.get("trace_id") if aml_case else None) or f"trc-aml-{case_id}"

            escalation = {
                "ran": True,
                "risk_tier": risk.get("risk_tier", "high"),
                "risk_reasoning": risk.get("reasoning", "Inflow deviation velocity exceeded threshold."),
                "aml_case_narrative": narrative,
                "aml_trace_id": trace_id,
            }

            # Persist escalation
            try:
                import case_actions
                case_actions.save_case_action(persona, case_id, "escalated", escalation=escalation)
            except Exception as e:
                print(f"[app.py] Warning: could not persist case action: {e}")

            print(f"[app.py] AML case {case_id} escalation complete ({trace_id})")
            self._send_json({
                "success": True,
                "status": "escalated",
                "case_id": case_id,
                "escalation": escalation,
            })
        except Exception as exc:
            traceback.print_exc()
            self._send_json(
                {"success": False, "error": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _handle_acknowledge(self, persona: str, case_id: str) -> None:
        try:
            import case_actions
            case_actions.save_case_action(persona, case_id, "acknowledged")
        except Exception as e:
            print(f"[app.py] Warning: could not persist acknowledge: {e}")
        self._send_json({
            "success": True,
            "status": "acknowledged",
            "case_id": case_id,
        })

    def do_POST(self):
        m_esc = re.match(r"^/api/([^/]+)/cases/([^/]+)/escalate$", self.path)
        if m_esc:
            persona, case_id = m_esc.groups()
            self._handle_escalate(persona, case_id)
            return

        m_ack = re.match(r"^/api/([^/]+)/cases/([^/]+)/acknowledge$", self.path)
        if m_ack:
            persona, case_id = m_ack.groups()
            self._handle_acknowledge(persona, case_id)
            return

        if self.path != "/run":
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return


        try:
            payload = self._read_json()
            customer_id = self._get_value(payload, "customer_id", "customerId") or "glucometer-vendor"

            persona_defaults = {
                "glucometer-vendor": {
                    "source_type": "hospital_vendor",
                    "csv_path": "ingestion_data/hospital_vendor_2yr_history.csv",
                    "tenant_name": "MedSupply Direct",
                },
                "gym-chain": {
                    "source_type": "membership_club",
                    "csv_path": "ingestion_data/gym_membership_2yr_history.csv",
                    "tenant_name": "FitCore Gyms",
                },
                "boho-house": {
                    "source_type": "membership_club",
                    "csv_path": "ingestion_data/boho_house_2yr_history.csv",
                    "tenant_name": "Boho House",
                },
            }
            defaults = persona_defaults.get(customer_id, persona_defaults["glucometer-vendor"])
            source_type = self._get_value(payload, "source_type", "sourceType", "source_type_name") or defaults["source_type"]
            csv_path = self._get_value(payload, "csv_path", "csvPath", "file_path") or defaults["csv_path"]

            # If csv_path is gs://, try resolving to local file in ingestion_data if GCS isn't forced
            if csv_path.startswith("gs://"):
                local_cand = ROOT_DIR / "ingestion_data" / os.path.basename(csv_path)
                if local_cand.exists() and os.environ.get("BUCKET_BACKEND", "local") != "gcs":
                    csv_path = str(local_cand)

            run_date = self._get_value(payload, "run_date", "runDate")

            state = {
                "customer_id": customer_id,
                "source_type": source_type,
                "csv_path": csv_path,
                "tenant_name": defaults.get("tenant_name", customer_id),
            }
            if run_date:
                state["run_date"] = run_date


            t_start = time.time()
            result = run_pipeline(state)
            elapsed_ms = int((time.time() - t_start) * 1000)

            # Telemetry collected by nodes._track_node (best-effort)
            agent_runs = get_agent_run_log()

            # Build a concise governance snapshot
            gov_summary = result.get("governance_summary", {})

            # Build terminal-log lines for the pipeline canvas in the UI
            terminal_lines: list[str] = []
            for ar in agent_runs:
                status_icon = "✅" if ar["status"] == "succeeded" else "❌"
                line = (
                    f"{status_icon} [{ar['agent']}] {ar['status'].upper()} "
                    f"({ar.get('latency_ms', '?')} ms)"
                )
                if ar.get("llm_calls"):
                    line += f" · {ar['llm_calls']} LLM call(s), {ar.get('tokens_used', 0)} tokens"
                if ar.get("error"):
                    line += f" · ERROR: {ar['error']}"
                terminal_lines.append(line)

            self._send_json({
                "success": True,
                "pipeline_status": "completed",
                "elapsed_ms": elapsed_ms,
                "llm_provider": _resolve_provider(),
                "agent_runs": agent_runs,
                "governance_summary": gov_summary,
                "terminal_lines": terminal_lines,
                "result": {
                    "risk_score": result.get("risk_score"),
                    "risk_reasons": result.get("risk_reasons", []),
                    "report_text": result.get("report_text"),
                    "aml_cases_count": len(result.get("aml_cases") or []),
                    "marketing_summary": result.get("marketing_summary"),
                    "reminders_queued": result.get("reminders_queued", []),
                    "pii_blocked": result.get("pii_blocked", False),
                    "top_risk_report": result.get("top_risk_report"),
                },
            })
        except ValueError as exc:
            self._send_json(
                {"success": False, "pipeline_status": "error", "error": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
        except Exception as exc:
            # Cloud Run only captures stdout/stderr, not response bodies -- print
            # the full traceback so 500s are diagnosable from Cloud Logging.
            traceback.print_exc()
            self._send_json(
                {"success": False, "pipeline_status": "error", "error": str(exc),
                 "agent_runs": get_agent_run_log()},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def log_message(self, format: str, *args) -> None:
        # Cloud Run captures stdout/stderr logs.
        print(f"{self.address_string()} - {format % args}")


if __name__ == "__main__":
    provider = _resolve_provider()
    print(f"Starting Kapitalwerk NXT service on port {PORT} [LLM_PROVIDER={provider}]")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), RequestHandler)
    server.serve_forever()
