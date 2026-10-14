r"""The orchestrator itself: wires the five agents into one LangGraph graph.

ingestion -> inflow -> risk --------\
"""


from langgraph.graph import StateGraph, END

from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from state import PipelineState
from nodes import (
    ingestion_node, inflow_node, risk_node, aml_node,
    reporting_node, marketing_node, reminder_node, should_run_aml,
    _track_node, get_agent_run_log, reset_agent_run_log,  # telemetry for /run API
)

# Re-export so callers only need to import from graph.py
__all__ = ["build_graph", "get_agent_run_log", "reset_agent_run_log"]


def build_graph():
    graph = StateGraph(PipelineState)

    graph.add_node("ingestion_node", lambda s: _track_node("ingestion_node", ingestion_node, s))
    graph.add_node("inflow_node", lambda s: _track_node("inflow_node", inflow_node, s))
    graph.add_node("risk_node", lambda s: _track_node("risk_node", risk_node, s))
    graph.add_node("aml_node", lambda s: _track_node("aml_node", aml_node, s))
    graph.add_node("reporting_node", lambda s: _track_node("reporting_node", reporting_node, s))
    graph.add_node("marketing_node", lambda s: _track_node("marketing_node", marketing_node, s))
    graph.add_node("reminder_node", lambda s: _track_node("reminder_node", reminder_node, s))


    graph.set_entry_point("ingestion_node")
    graph.add_edge("ingestion_node", "inflow_node")
    graph.add_edge("inflow_node", "risk_node")

    # conditional edge: only spend LLM tokens if Inflow found something
    # worth escalating
    graph.add_conditional_edges(
        "risk_node",
        should_run_aml,
        {"aml_node": "aml_node", "reporting_node": "reporting_node"},
    )

    graph.add_edge("aml_node", "reporting_node")
    graph.add_edge("reporting_node", "marketing_node")
    graph.add_edge("marketing_node", "reminder_node")
    graph.add_edge("reminder_node", END)

    return graph.compile()
