"""Intake workflow — classify one inbound email; for a closed deal, create the onboarding.

    START → email_analyser (subgraph: reply detection, sender gate, guardrails, LLM verdict)
              ├─ kind = deal_closed ──► extract_deal ──► END   (creates the onboarding, queues tier_confirmation)
              └─ anything else ───────────────────────► END   (no_deal / malicious / escalation_reply)

Runs on thread ``intake:<inbound_email_id>`` and finishes in one go — it never pauses. An
``escalation_reply`` ends here classified; the orchestrator then resumes the paused tier workflow.
"""
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from src.agents.email_analyser.graph import build_email_analyser_graph
from src.agents.intake.nodes import extract_deal_node, route_after_classification
from src.agents.intake.state import IntakeState
from src.agents.workflow_graph import WorkflowGraph


def build_intake_graph(checkpointer=None) -> CompiledStateGraph:
    graph = StateGraph(IntakeState)
    graph.add_node("email_analyser", build_email_analyser_graph())
    graph.add_node("extract_deal", extract_deal_node)

    graph.add_edge(START, "email_analyser")
    graph.add_conditional_edges("email_analyser", route_after_classification, {"extract_deal": "extract_deal", "__end__": END})
    graph.add_edge("extract_deal", END)

    return graph.compile(checkpointer=checkpointer)


intake_graph = WorkflowGraph(build_intake_graph)
