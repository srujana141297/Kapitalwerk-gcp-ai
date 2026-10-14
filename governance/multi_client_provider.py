"""Multi-Client Provider (MCP) -- factory that resolves the correct LLM client
from environment variables, so the same Docker image works without any code
change across four different environments:

  LLM_PROVIDER=ollama   → OllamaClient   (local dev, default when ENV=dev)
  LLM_PROVIDER=gemini   → GeminiClient   (direct Gemini API key, needs GEMINI_API_KEY)
  LLM_PROVIDER=vertex   → VertexGeminiClient (Vertex AI / ADC, production default)
  LLM_PROVIDER=mock     → MockLlamaClient  (tests / CI / offline demos)

Resolution order:
  1. LLM_PROVIDER env var (explicit override, highest priority)
  2. ENV env var:  ENV=dev  → defaults to "ollama"; anything else → falls back
     to config.json's llm_provider value, then "vertex".
  3. config.json  llm_provider field
  4. Hard-coded default: "vertex"

Usage
-----
    from governance.multi_client_provider import get_llm_client
    client = get_llm_client()   # respects env vars automatically

Or with an explicit config dict (e.g. when app.py already loaded config.json):
    client = get_llm_client(config={"llm_provider": "gemini"})
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm_client import LLMClient  # only for type-checkers, never executed at import time


def get_llm_client(config: dict | None = None) -> "LLMClient":
    """Return the appropriate LLMClient instance based on environment / config.

    Parameters
    ----------
    config:
        Optional dict (e.g. the loaded config.json) that may contain a
        ``llm_provider`` key.  Only consulted when ``LLM_PROVIDER`` is not set.

    Returns
    -------
    LLMClient
        A fully initialised (but lazily-connecting) client ready for use.

    Raises
    ------
    ValueError
        If the resolved provider name is not one of: ollama, gemini, vertex, mock.
    """
    # --- resolve provider -----------------------------------------------
    explicit_provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    env_mode = os.environ.get("ENV", "prod").strip().lower()

    if explicit_provider:
        provider = explicit_provider
    elif env_mode == "dev":
        # Local development: default to Ollama so no cloud credentials are needed.
        provider = "ollama"
    else:
        # Production path: honour config.json, fall back to Vertex (ADC, no API key).
        provider = (config or {}).get("llm_provider", "vertex").strip().lower()

    # --- import client classes lazily (avoids ImportError when a provider's
    #     SDK is not installed in the current environment) -----------------
    # All four classes live in the same module; we only import it here
    # (never at module import time) to keep governance/__init__.py clean.
    # Support two import contexts:
    # 1. `from governance.multi_client_provider import get_llm_client`  (package import, e.g. app.py)
    # 2. `from multi_client_provider import get_llm_client`             (flat path, e.g. orchestrator/nodes.py
    #    which adds governance/ to sys.path directly)
    try:
        from .llm_client import (  # noqa: PLC0415
            OllamaClient,
            GeminiClient,
            VertexGeminiClient,
            MockLlamaClient,
        )
    except ImportError:
        from llm_client import (  # noqa: PLC0415  # type: ignore[no-redef]
            OllamaClient,
            GeminiClient,
            VertexGeminiClient,
            MockLlamaClient,
        )

    if provider == "ollama":
        return OllamaClient(
            model=os.environ.get("OLLAMA_MODEL", "llama3"),
            host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        )

    if provider == "gemini":
        return GeminiClient(
            model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        )

    if provider == "vertex":
        return VertexGeminiClient(
            model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        )

    if provider == "mock":
        return MockLlamaClient()

    raise ValueError(
        f"Unsupported LLM_PROVIDER={provider!r}. "
        "Choose one of: ollama, gemini, vertex, mock."
    )
