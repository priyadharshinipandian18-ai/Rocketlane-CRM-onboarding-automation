"""Communication agent ("Agent 2"): a ReAct agent that opens the shared customer Slack channel.

    START ─ no SLACK_BOT_TOKEN ──▶ skip_communication ──▶ END
      │
      └─▶ prepare ──▶ agent ──┬─ tool calls ──▶ tools ──▶ agent …   (a bounded loop)
           (facts brief)      └─ done / LLM down / step limit ──▶ finalize ──▶ END
                                                                    (completes anything not attempted,
                                                                     records the true outcome)

The LLM decides *which* Slack tool to use next and **writes the welcome message**; the tools do the work
(validated, idempotent, retried on temporary failures, see ``tools.py`` / ``operations.py``); ``finalize``
guarantees the setup is completed and that the outcome reflects the database, not the model's claims.

Runs after the Rocketlane step (or the team assignment) in the provisioning graph. A Slack problem never
fails the onboarding: it sets ``needs_human_review`` with the reason.

The agent's conversation lives only inside this subgraph (input and output schemas are
``CommunicationState``), so the provisioning checkpoint does not grow with it.
"""
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from src.agents.communication.nodes import (
    agent_node,
    finalize_node,
    prepare_node,
    route_after_agent,
    route_after_prepare,
    route_enabled,
    skip_communication_node,
    tool_error_message,
)
from src.agents.communication.state import CommunicationAgentState, CommunicationState
from src.agents.communication.tools import TOOLS


def build_communication_graph() -> CompiledStateGraph:
    graph = StateGraph(CommunicationAgentState, input_schema=CommunicationState, output_schema=CommunicationState)
    graph.add_node("skip_communication", skip_communication_node)
    graph.add_node("prepare", prepare_node)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(TOOLS, handle_tool_errors=tool_error_message))
    graph.add_node("finalize", finalize_node)

    graph.add_conditional_edges(START, route_enabled, ["prepare", "skip_communication"])
    graph.add_conditional_edges("prepare", route_after_prepare, ["agent", "finalize"])
    graph.add_conditional_edges("agent", route_after_agent, ["tools", "finalize"])
    graph.add_edge("tools", "agent")
    graph.add_edge("skip_communication", END)
    graph.add_edge("finalize", END)

    return graph.compile()
