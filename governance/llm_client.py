"""Pluggable LLM client -- same swap pattern as storage.py's LocalBucket/GCSBucket.
The AML Agent (and the governance middleware wrapping it) only ever talks to
the LLMClient interface below, never to a specific provider's SDK directly.
"""


from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    latency_ms: int


class LLMClient:
    def complete(self, prompt: str) -> LLMResponse:
        raise NotImplementedError


class OllamaClient(LLMClient):
    """Development client -- talks to a local Ollama server running Llama 3.
Requires Ollama installed and running on your machine:
"""


    def __init__(self, model: str = "llama3", host: str = "http://localhost:11434"):
        self.model = model
        self.host = host

    def complete(self, prompt: str) -> LLMResponse:
        import urllib.request
        import urllib.error

        start = time.time()
        payload = json.dumps({"model": self.model, "prompt": prompt, "stream": False}).encode()
        req = urllib.request.Request(
            f"{self.host}/api/generate", data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Failed to connect to local Ollama at {self.host} ({e}). "
                "Ensure Ollama is running ('ollama serve' / 'ollama run llama3') or set LLM_PROVIDER=mock."
            ) from e


        return LLMResponse(
            text=data["response"],
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
            model=self.model,
            latency_ms=int((time.time() - start) * 1000),
        )


class GeminiClient(LLMClient):
    """Production client -- Gemini Flash. Requires GEMINI_API_KEY and the
`google-generativeai` package. Stubbed with the real call shape so
"""


    def __init__(self, model: str = "gemini-2.5-flash", api_key_env: str = "GEMINI_API_KEY"):
        import os
        self.model = model
        self.api_key = os.environ.get(api_key_env)
        if not self.api_key:
            raise ValueError(f"{api_key_env} is required when LLM_PROVIDER=gemini")

    def complete(self, prompt: str) -> LLMResponse:
        import google.generativeai as genai  # noqa: F401 -- only imported when this client is actually used

        genai.configure(api_key=self.api_key)
        model = genai.GenerativeModel(self.model)
        start = time.time()
        result = model.generate_content(prompt)
        usage = getattr(result, "usage_metadata", None)
        return LLMResponse(
            text=result.text,
            input_tokens=getattr(usage, "prompt_token_count", 0) if usage else 0,
            output_tokens=getattr(usage, "candidates_token_count", 0) if usage else 0,
            model=self.model,
            latency_ms=int((time.time() - start) * 1000),
        )


class VertexGeminiClient(LLMClient):
    """Production client -- Gemini Flash via Vertex AI, authenticated with
Application Default Credentials (ADC) instead of an API key. This is the
"""


    def __init__(
        self,
        model: str = "gemini-2.5-flash",
        project_env: str = "GCP_PROJECT",
        location_env: str = "GCP_LOCATION",
    ):
        import os
        self.model = model
        self.project = os.environ.get(project_env)
        self.location = os.environ.get(location_env, "us-central1")
        if not self.project:
            raise ValueError(
                f"{project_env} env var is required when LLM_PROVIDER=vertex "
                "(cloudbuild.yaml sets it to $PROJECT_ID; export it for local runs)"
            )
        self._client = None  # lazy, reused across calls

    def complete(self, prompt: str) -> LLMResponse:
        from google import genai  # noqa: F401 -- only imported when this client is actually used
        from google.genai.errors import ClientError

        if self._client is None:
            self._client = genai.Client(
                vertexai=True, project=self.project, location=self.location,
            )
        client = self._client
        start = time.time()
        # Vertex enforces a separate requests-per-minute quota from the
        # tokens-per-minute one; a burst of back-to-back agent calls
        # (risk -> aml -> marketing) within one pipeline run can trip it
        # even when token usage is nowhere near its own limit. 
        max_attempts = 6
        base_delay_s = 3.0
        max_delay_s = 60.0
        last_error: ClientError | None = None
        for attempt in range(max_attempts):
            try:
                result = client.models.generate_content(model=self.model, contents=prompt)
                break
            except ClientError as e:
                if getattr(e, "code", None) != 429 or attempt == max_attempts - 1:
                    raise
                last_error = e
                delay = min(base_delay_s * (2 ** attempt), max_delay_s)
                print(f"[VertexGeminiClient] 429 RESOURCE_EXHAUSTED (attempt {attempt + 1}/{max_attempts}), "
                      f"backing off {delay:.1f}s before retry...")
                time.sleep(delay)
        else:  # pragma: no cover -- loop always breaks or raises above
            raise last_error
        usage = getattr(result, "usage_metadata", None)
        return LLMResponse(
            text=result.text,
            input_tokens=getattr(usage, "prompt_token_count", 0) if usage else 0,
            output_tokens=getattr(usage, "candidates_token_count", 0) if usage else 0,
            model=self.model,
            latency_ms=int((time.time() - start) * 1000),
        )


class MockLlamaClient(LLMClient):
    """Deterministic stand-in for local dev/testing without a live model --
used by the test suite and by this environment's demo run, since there's
"""


    def __init__(self, simulated_latency_ms: int = 350):
        self.simulated_latency_ms = simulated_latency_ms
        self.call_count = 0

    def complete(self, prompt: str) -> LLMResponse:
        self.call_count += 1
        time.sleep(self.simulated_latency_ms / 1000)
        # Deterministic, content-free synthetic completion for local dev/tests.
        ref = hashlib.sha1(prompt.encode()).hexdigest()[:6]
        text = (
            f"Reviewing the signals in this run (ref {ref}): the observed pattern is "
            f"a measurable deviation from the counterparty's established baseline, "
            f"consistent with either normal seasonal variance or an early trend shift. "
            f"This pattern warrants compliance review given the deviation from established baseline."
        )
        return LLMResponse(
            text=text,
            input_tokens=len(prompt) // 4,     # rough token estimate, same heuristic real tokenizers approximate with
            output_tokens=len(text) // 4,
            model="mock-llama3",
            latency_ms=self.simulated_latency_ms,
        )
