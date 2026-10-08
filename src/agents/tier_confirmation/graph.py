"""Tier-confirmation workflow — call the AE; if that fails, email the AE and wait.

    START → place_plan_tier_call ─┬─ placed ──────────► await_plan_tier_result (pause: call result)
                                  ├─ deferred ────────► wait_for_call_retry     (pause: timer)
                                  ├─ failed ──────────► retry / request_ae_reply
                                  └─ no phone ────────► request_ae_reply
    await_plan_tier_result ─┬─ confirmed ───────────► confirm_tier ──► END
                            ├─ not confirmed, tries left ──► schedule_call_retry ──► wait_for_call_retry ──► place_plan_tier_call
                            └─ tries exhausted ─────► request_ae_reply ──► await_ae_reply (pause: AE's email reply) ──► confirm_tier

Runs on thread ``tier:<onboarding_id>``. ``confirm_tier`` queues the provisioning workflow.
"""
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from src.agents.tier_confirmation.nodes import (
    await_ae_reply_node,
    await_plan_tier_result_node,
    confirm_tier_node,
    place_plan_tier_call_node,
    request_ae_reply_node,
    route_after_call,
    route_after_place,
    schedule_call_retry_node,
    wait_for_call_retry_node,
)
from src.agents.tier_confirmation.state import TierConfirmationState
from src.agents.workflow_graph import WorkflowGraph


def build_tier_confirmation_graph(checkpointer=None) -> CompiledStateGraph:
    graph = StateGraph(TierConfirmationState)
    graph.add_node("place_plan_tier_call", place_plan_tier_call_node)
    graph.add_node("await_plan_tier_result", await_plan_tier_result_node)
    graph.add_node("schedule_call_retry", schedule_call_retry_node)
    graph.add_node("wait_for_call_retry", wait_for_call_retry_node)
    graph.add_node("request_ae_reply", request_ae_reply_node)
    graph.add_node("await_ae_reply", await_ae_reply_node)
    graph.add_node("confirm_tier", confirm_tier_node)

    graph.add_edge(START, "place_plan_tier_call")
    graph.add_conditional_edges(
        "place_plan_tier_call", route_after_place,
        ["await_plan_tier_result", "wait_for_call_retry", "schedule_call_retry", "request_ae_reply", "confirm_tier"],
    )
    graph.add_conditional_edges(
        "await_plan_tier_result", route_after_call, ["confirm_tier", "schedule_call_retry", "request_ae_reply"]
    )
    graph.add_edge("schedule_call_retry", "wait_for_call_retry")
    graph.add_edge("wait_for_call_retry", "place_plan_tier_call")
    graph.add_edge("request_ae_reply", "await_ae_reply")
    graph.add_edge("await_ae_reply", "confirm_tier")
    graph.add_edge("confirm_tier", END)

    return graph.compile(checkpointer=checkpointer)


tier_graph = WorkflowGraph(build_tier_confirmation_graph)
