"""Provisioning workflow — turn a confirmed plan tier into a Rocketlane project and a Slack channel.

    START → create_rocketlane_project → communication_agent (subgraph: Slack channel) → END

Runs on thread ``provision:<onboarding_id>``. It starts here no matter how the tier was
confirmed (the call, or the AE's email reply), with a tiny input. A failure raises at the
failing node and keeps its checkpoint, so ``POST /onboardings/{id}/retry`` re-runs from that
node; the ``rocketlane_projects`` / ``slack_channels`` rows make every step safe to repeat.
"""
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from src.agents.communication.graph import build_communication_graph
from src.agents.provisioning.nodes import assign_team_node, create_rocketlane_project_node
from src.agents.provisioning.state import ProvisioningState
from src.agents.workflow_graph import WorkflowGraph
from src.config.settings import get_settings


def build_provisioning_graph(checkpointer=None) -> CompiledStateGraph:
    graph = StateGraph(ProvisioningState)
    graph.add_node("communication_agent", build_communication_graph())

    if get_settings().rocketlane_enabled:
        graph.add_node("create_rocketlane_project", create_rocketlane_project_node)
        graph.add_edge(START, "create_rocketlane_project")
        graph.add_edge("create_rocketlane_project", "communication_agent")
    else:
        # ROCKETLANE_ENABLED=false: the Rocketlane step is switched off; go straight to Slack.
        graph.add_node("assign_team", assign_team_node)
        graph.add_edge(START, "assign_team")
        graph.add_edge("assign_team", "communication_agent")
    graph.add_edge("communication_agent", END)

    return graph.compile(checkpointer=checkpointer)


provisioning_graph = WorkflowGraph(build_provisioning_graph)
