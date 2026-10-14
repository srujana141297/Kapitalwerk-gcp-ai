# Governance Middleware

Sits between every LLM-calling agent (AML, Risk) and the model itself. Each
agent has its own call/token budget, sharing one cache and one audit log.

## What it does

- **Cost table per provider** -- `COST_PER_1K_TOKENS` in `governance.py`.
- **Cache** -- exact-match on a normalized prompt hash.
- **Per-agent budget + stop conditions** -- caps calls/tokens per run per
  agent; over-budget calls are refused and logged with a `stop_reason`.
- **Immutable audit log** -- every call (cache hit, completed, or refused)
  is appended as one record, tagged by agent.

## Persistence: cache + audit log are pluggable

Interface: `cache_store.py`'s `CacheStore`, `audit_store.py`'s `AuditStore`.

| Backend | `GOVERNANCE_STORE=` | Notes |
|---|---|---|
| `InMemoryCacheStore` + `LocalFileAuditStore` | `memory` (default) | Not persistent across restarts or instances. |
| `FirestoreCacheStore` + `FirestoreAuditStore` | `firestore` | Persistent, shared across instances. Requires `google-cloud-firestore`. |

Configured via `FIRESTORE_PROJECT`, `FIRESTORE_CACHE_COLLECTION`,
`FIRESTORE_AUDIT_COLLECTION`, `FIRESTORE_DATABASE`.

## Swapping LLM providers

`llm_client.py`'s `LLMClient` interface has three implementations:

| Client | Use |
|---|---|
| `MockLlamaClient` | Tests / no reachable model |
| `OllamaClient` | Local Llama 3 |
| `GeminiClient` | Production (`GEMINI_API_KEY`) |

Selected via `LLM_PROVIDER` (`mock` / `ollama` / `gemini`) in
`orchestrator/nodes.py`'s `_build_llm_client()`.

## Run the tests

```
cd governance
python3 -m pytest test_governance.py -v
```
