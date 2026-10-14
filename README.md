# Kapitalwerk NXT

## What this project does

Kapitalwerk NXT is a multi-agent transaction monitoring pipeline for SME banking use cases.
It ingests customer transaction files, computes anomaly and risk signals, optionally runs AML narratives through an LLM with governance controls, generates reporting artifacts, and exposes everything through an API and bundled UI.

## Core components

- Ingestion agent: normalizes source CSV data and writes snapshot artifacts
- Inflow agent: computes flags such as missing payments, deviations, and high-value transactions
- Risk agent: derives risk tier and signals, plus PII fail-closed gating
- AML agent: creates case narratives for escalated flags
- Marketing agent: builds postal-code heatmap annotations
- Reporting agent: reminder outputs, top-risk report, exports used by API/UI
- Governance: LLM budget, caching, audit trail, provider abstraction
- Orchestrator: LangGraph workflow wiring all nodes
- API service: read and action endpoints plus standalone UI at /

## End-to-end flow

Execution order in orchestrator graph:

1. ingestion_node
2. inflow_node
3. risk_node
4. Conditional branch:
5. aml_node if escalation criteria met, else skip to reporting
6. reporting_node
7. marketing_node
8. reminder_node

Flow source: orchestrator/graph.py.

## Storage model

Per-run artifacts are stored under:

- customer_id/run_date/normalized_transactions.json
- customer_id/run_date/inflow_flags.json
- customer_id/run_date/top_risk_report.json
- customer_id/run_date/marketing_heatmap.json
- plus related reporting and audit outputs

Bucket backend is environment-driven:

- local: LocalBucket
- gcs: MirrorBucket (reads from GCS first, writes to both local and GCS)

## LLM and governance model

- LLM provider is selected by LLM_PROVIDER
- Supported: mock, vertex, gemini, ollama
- Governance store is selected by GOVERNANCE_STORE
- Supported: memory (default), firestore

Only selected nodes call LLMs directly, and all LLM calls pass through GovernanceMiddleware.

## Local setup

Prerequisites:

- Python 3.10+
- pip

Install dependencies:

pip install -r requirements.txt

Optional: create a .env file in repo root for local overrides.

## Run the full pipeline locally

Configure your LLM provider before running (choose one):

```bash
# Option 1: Fast local mock (no external credentials needed)
export LLM_PROVIDER=mock

# Option 2: Local Ollama (or set ENV=dev)
export LLM_PROVIDER=ollama
# export OLLAMA_HOST="http://localhost:11434"
# export OLLAMA_MODEL="llama3"

# Option 3: Google Gemini API
export LLM_PROVIDER=gemini
export GEMINI_API_KEY="your-gemini-api-key"

# Option 4: Google Cloud Vertex AI (requires GCP_PROJECT and ADC)
export LLM_PROVIDER=vertex
export GCP_PROJECT="your-gcp-project-id"
```

Run from orchestrator folder:

```bash
cd orchestrator
python3 run_orchestrator.py
```

Useful environment flags:

- `RUN_ALL_PERSONAS=1` to process all personas
- `FORCE_RUN=1` to force rerun even if snapshot exists
- `RUN_DATE=YYYY-MM-DD` to backfill or re-run a date
- `BUCKET_BACKEND=local` or `gcs`
- `ENV=dev` to default provider to Ollama

## Run the API and load the UI

Once pipeline artifacts are generated (or when testing with existing outputs), start the FastAPI service from the repo root:

```bash
uvicorn api.main:app --reload --port 8000
```

### Access the Web UI:
Open your browser and navigate to:
```
http://localhost:8000/
```
The standalone dashboard loads at `/`, providing interactive views for each persona (`glucometer-vendor`, `gym-chain`, `boho-house`), transaction flags, compliance case reviews, notifications, and postal heatmaps.

### API Endpoints:
- `GET /` — Standalone dashboard UI
- `GET /api/personas` — Supported business personas
- `GET /api/{persona}/dashboard` — Aggregated dashboard state
- `GET /api/{persona}/counterparties` — Top-10 counterparties by value
- `GET /api/{persona}/cases` — Flagged cases awaiting review
- `POST /api/{persona}/cases/{case_id}/acknowledge` — Acknowledge a case
- `POST /api/{persona}/cases/{case_id}/escalate` — Re-run Risk + AML review
- `GET /api/{persona}/marketing-heatmap` — Postal code customer heatmap
- `GET /api/governance/audit-summary` — Governance audit rollup

## Run the Cloud Run agent endpoint locally

The root app.py exposes:

- GET /health
- POST /run

Start service:

```bash
python3 app.py
```

Example run request:

```bash
curl -X POST "http://localhost:8080/run" \
  -H "Content-Type: application/json" \
  -d '{"customer_id":"glucometer-vendor","source_type":"hospital_vendor","csv_path":"../ingestion_data/hospital_vendor_2yr_history.csv"}'
```

## Cloud deployment

cloudbuild.yaml builds and deploys two Cloud Run services:

1. kapitalwerk-agent
2. kapitalwerk-api

Highlights:

- Agent is deployed first
- API gets AGENT_URL from deployed agent service
- BUCKET_BACKEND=gcs and GCS bucket name are injected via env vars
- Optional Firestore governance backend can be enabled with GOVERNANCE_STORE=firestore

Run deploy:

gcloud builds submit --config cloudbuild.yaml .

## Test commands

Run all module tests:

for d in ingestion_agent inflow_agent risk_agent aml_agent governance reporting_agent marketing_agent; do (cd "$d" && python3 -m pytest -q); done

## Key env vars

- BUCKET_BACKEND: local or gcs
- GCS_BUCKET_NAME: target GCS bucket when BUCKET_BACKEND=gcs
- LLM_PROVIDER: mock, vertex, gemini, ollama
- GEMINI_MODEL: model name for gemini or vertex
- AGENT_URL: API-to-agent forwarding endpoint
- GOVERNANCE_STORE: memory or firestore
- FIRESTORE_PROJECT: optional explicit project for firestore backend
- FIRESTORE_DATABASE: firestore database id, default is (default)
- REMINDER_NOTIFIER: console or resend
- RESEND_API_KEY: required when REMINDER_NOTIFIER=resend

## File map

- app.py: Cloud Run style run endpoint
- orchestrator/run_orchestrator.py: local and batch orchestrator runner
- orchestrator/graph.py: node wiring and conditional path
- orchestrator/nodes.py: node implementations
- api/main.py: API and UI service
- cloudbuild.yaml: build and deploy pipeline