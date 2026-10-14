"""Repo-root pytest config.

Every agent package's tests import their sibling modules bare (e.g.
``from notifications import ...`` inside reporting_agent/) -- the same
convention orchestrator/nodes.py and api/main.py rely on. That works when
pytest is invoked from inside the agent directory, but running ``pytest``
from the repo root (as in the Docker image at /app) fails with e.g.
``ModuleNotFoundError: No module named 'notifications'``.

Putting every agent directory on sys.path here makes the whole suite
runnable from the repo root with a single ``pytest``.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))

_AGENT_DIRS = (
    "reporting_agent",
    "governance",
    "ingestion_agent",
    "inflow_agent",
    "risk_agent",
    "aml_agent",
    "marketing_agent",
    "orchestrator",
    "api",
)

for _name in _AGENT_DIRS:
    _path = os.path.join(_ROOT, _name)
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.insert(0, _path)
