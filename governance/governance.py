"""Governance middleware -- every LLM call any agent makes goes through this,
never directly to the LLMClient. Now monitors two agents (AML Agent and
Risk Agent), each with its own independent budget/stop-condition tracking,
"""


from __future__ import annotations

import hashlib
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

try:
    from cache_store import CacheStore, InMemoryCacheStore  # type: ignore
    from audit_store import AuditStore, LocalFileAuditStore  # type: ignore
except ModuleNotFoundError:
    class CacheStore:  # type: ignore[override]
        pass

    class InMemoryCacheStore(CacheStore):  # type: ignore[override]
        pass

    class AuditStore:  # type: ignore[override]
        pass

    class LocalFileAuditStore(AuditStore):  # type: ignore[override]
        def __init__(self, *args, **kwargs):
            pass

try:
    from .multi_client_provider import get_llm_client
except ImportError:
    from multi_client_provider import get_llm_client


# EUR per 1,000 tokens, input/output blended for simplicity -- swap per
# provider as real pricing is confirmed
COST_PER_1K_TOKENS = {
    "mock-llama3": 0.0,       # local/free during dev
    "llama3": 0.0,            # local/free during dev
    "gemini-2.5-flash": 0.00015,  # production model, placeholder rate
}

DEFAULT_MAX_CALLS_PER_RUN = 50
DEFAULT_MAX_TOKENS_PER_RUN = 50_000


class BudgetExceeded(RuntimeError):
    """Raised when a specific agent hits its own call or token budget. A
specific type on purpose -- callers (AMLAgent, RiskAgent) should catch
"""

    def __init__(self, message: str, stop_reason: str, agent: str):
        super().__init__(message)
        self.stop_reason = stop_reason
        self.agent = agent


@dataclass
class CallRecord:
    trace_id: str
    agent: str
    prompt_hash: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_eur: float
    latency_ms: int
    stop_reason: str
    timestamp: float = field(default_factory=time.time)
    # Optional context for non-LLM events (e.g. a PII block); None otherwise.
    detail: dict | None = None


class GovernanceMiddleware:
    def __init__(
        self,
        llm_client,
        audit_log_path: str | None = None,
        max_calls_per_run: int | dict[str, int] = DEFAULT_MAX_CALLS_PER_RUN,
        max_tokens_per_run: int | dict[str, int] = DEFAULT_MAX_TOKENS_PER_RUN,
        cache_store: CacheStore | None = None,
        audit_store: AuditStore | None = None,
    ):
        """max_calls_per_run / max_tokens_per_run accept either a single int
(applied to every agent) or a dict keyed by agent name for
"""

        self.llm_client = llm_client
        self.max_calls_per_run = max_calls_per_run
        self.max_tokens_per_run = max_tokens_per_run

        self.cache_store = cache_store or InMemoryCacheStore()
        if audit_store is not None:
            self.audit_store = audit_store
        elif audit_log_path is not None:
            self.audit_store = LocalFileAuditStore(audit_log_path)
        else:
            raise ValueError("GovernanceMiddleware needs either audit_log_path or audit_store")

        self._calls_by_agent: dict[str, int] = defaultdict(int)
        self._tokens_by_agent: dict[str, int] = defaultdict(int)
        self._cache_hits_by_agent: dict[str, int] = defaultdict(int)
        self._stop_reason_by_agent: dict[str, str] = {}

    def _limit_for(self, limits: int | dict[str, int], agent: str, default: int) -> int:
        if isinstance(limits, int):
            return limits
        return limits.get(agent, limits.get("default", default))

    def _hash_prompt(self, prompt: str) -> str:
        normalized = " ".join(prompt.split())  # collapse whitespace differences
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def _next_trace_id(self) -> str:
        # A simple incrementing counter isn't safe once more than one
        # instance can be constructing GovernanceMiddleware concurrently
        return f"trc-{uuid.uuid4().hex[:12]}"

    def _write_audit(self, record: CallRecord) -> None:
        self.audit_store.append(record)

    def complete(self, agent: str, prompt: str) -> tuple[str, CallRecord]:
        trace_id = self._next_trace_id()
        prompt_hash = self._hash_prompt(prompt)

        # cache check -- no call to the model at all on a hit
        cached = self.cache_store.get(prompt_hash)
        if cached is not None:
            self._cache_hits_by_agent[agent] += 1
            record = CallRecord(
                trace_id=trace_id, agent=agent, prompt_hash=prompt_hash,
                model="cache", input_tokens=0, output_tokens=0, cost_eur=0.0,
                latency_ms=0, stop_reason="cache_hit",
            )
            self._write_audit(record)
            return cached, record

        # budget check -- per agent, stop before spending anything further
        call_limit = self._limit_for(self.max_calls_per_run, agent, DEFAULT_MAX_CALLS_PER_RUN)
        if self._calls_by_agent[agent] >= call_limit:
            record = CallRecord(
                trace_id=trace_id, agent=agent, prompt_hash=prompt_hash,
                model="none", input_tokens=0, output_tokens=0, cost_eur=0.0,
                latency_ms=0, stop_reason="budget_exceeded_calls",
            )
            self._write_audit(record)
            self._stop_reason_by_agent[agent] = "budget_exceeded_calls"
            raise BudgetExceeded(
                f"{agent}: call budget ({call_limit}) exceeded this run",
                stop_reason="budget_exceeded_calls", agent=agent,
            )

        token_limit = self._limit_for(self.max_tokens_per_run, agent, DEFAULT_MAX_TOKENS_PER_RUN)
        if self._tokens_by_agent[agent] >= token_limit:
            record = CallRecord(
                trace_id=trace_id, agent=agent, prompt_hash=prompt_hash,
                model="none", input_tokens=0, output_tokens=0, cost_eur=0.0,
                latency_ms=0, stop_reason="budget_exceeded_tokens",
            )
            self._write_audit(record)
            self._stop_reason_by_agent[agent] = "budget_exceeded_tokens"
            raise BudgetExceeded(
                f"{agent}: token budget ({token_limit}) exceeded this run",
                stop_reason="budget_exceeded_tokens", agent=agent,
            )

        # actual call
        response = self.llm_client.complete(prompt)
        self._calls_by_agent[agent] += 1
        total_tokens = response.input_tokens + response.output_tokens
        self._tokens_by_agent[agent] += total_tokens

        rate = COST_PER_1K_TOKENS.get(response.model, COST_PER_1K_TOKENS["gemini-2.5-flash"])
        cost = round(total_tokens / 1000 * rate, 6)

        record = CallRecord(
            trace_id=trace_id, agent=agent, prompt_hash=prompt_hash,
            model=response.model, input_tokens=response.input_tokens,
            output_tokens=response.output_tokens, cost_eur=cost,
            latency_ms=response.latency_ms, stop_reason="completed",
        )
        self._write_audit(record)
        self.cache_store.set(prompt_hash, response.text, agent, response.model)
        return response.text, record

    def log_governance_event(self, agent: str, stop_reason: str, detail: dict | None = None) -> CallRecord:
        """Record a governance decision that did NOT result in an LLM call --
the audit-trail counterpart to a fail-closed control refusing to
"""

        record = CallRecord(
            trace_id=self._next_trace_id(), agent=agent, prompt_hash="",
            model="none", input_tokens=0, output_tokens=0, cost_eur=0.0,
            latency_ms=0, stop_reason=stop_reason, detail=detail,
        )
        self._write_audit(record)
        return record

    def run_summary(self, agent: str | None = None) -> dict:
        """With no argument: aggregate totals across every agent this
middleware has seen, plus a by_agent breakdown. With an agent
"""

        if agent is not None:
            return {
                "calls_made": self._calls_by_agent.get(agent, 0),
                "tokens_used": self._tokens_by_agent.get(agent, 0),
                "cache_hits": self._cache_hits_by_agent.get(agent, 0),
                "cache_size": self.cache_store.size(),
                "stopped_early": agent in self._stop_reason_by_agent,
                "stop_reason": self._stop_reason_by_agent.get(agent),
            }

        all_agents = set(self._calls_by_agent) | set(self._tokens_by_agent) | set(self._cache_hits_by_agent)
        return {
            "calls_made": sum(self._calls_by_agent.values()),
            "tokens_used": sum(self._tokens_by_agent.values()),
            "cache_size": self.cache_store.size(),
            "stopped_early": len(self._stop_reason_by_agent) > 0,
            "stop_reason": dict(self._stop_reason_by_agent) or None,
            "by_agent": {
                a: {
                    "calls_made": self._calls_by_agent.get(a, 0),
                    "tokens_used": self._tokens_by_agent.get(a, 0),
                    "cache_hits": self._cache_hits_by_agent.get(a, 0),
                    "stopped_early": a in self._stop_reason_by_agent,
                    "stop_reason": self._stop_reason_by_agent.get(a),
                }
                for a in all_agents
            },
        }
