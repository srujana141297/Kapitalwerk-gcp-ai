# KapitalWerkNxt API

FastAPI service wrapping `reporting_agent/exports.py` and
`reporting_agent/contact_store.py` as REST endpoints, read by the Standalone
HTML UI. Reads the same local `bucket_output/` the batch pipeline writes.

Read-only except two `POST` endpoints (acknowledge / escalate). `escalate` is
the only endpoint that calls an LLM (re-runs Risk then AML, fail-closed on
PII). Both `POST` endpoints persist to `bucket_output/_case_actions/case_actions.json`.

## Run it locally

From the repo root:

```bash
pip install -r requirements.txt
uvicorn api.main:app --reload --port 8000
```

Run the tests:

```bash
cd api && python3 -m pytest -q
```

## Endpoints

Persona must be one of `glucometer-vendor`, `gym-chain`, `boho-house`.

| Endpoint | Description |
| --- | --- |
| `GET /api/personas` | The 3 business customers. |
| `GET /api/{persona}/counterparties?run_date=...` | Top-10 counterparties by value. |
| `GET /api/{persona}/cases?run_date=...` | Top-10 flagged cases awaiting review, with status. |
| `POST /api/{persona}/cases/{case_id}/acknowledge` | Mark a case acknowledged. No LLM call. |
| `POST /api/{persona}/cases/{case_id}/escalate` | Re-run Risk + AML for one case, persist the narrative. |
| `GET /api/{persona}/notifications?today=...` | Last-4-weeks top-10 upcoming/missed notifications. |
| `GET /api/{persona}/marketing-heatmap?run_date=...` | Per-postal-code marketing heatmap. |
| `GET /api/{persona}/dashboard` | All of the above bundled for one persona. |
| `GET /api/governance/audit-summary` | Cross-persona governance audit rollup. |
| `GET /api/{persona}/reminder-preview` | Email preview metadata (masked recipient). |
