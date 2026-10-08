"""Email-analyser subgraph — classifies one inbound email.

One **guardrail** node first, then the classification:

    START → fetch_email → guardrail ──── route_after_guardrail ────────────────────────────────┐
                          (PII redaction → sender gate → injection check → phishing check)       │
                                                                                                  │
          blocked: not an AE ───────────▶ archive_skip        (kind=no_deal,   rule)              │
          blocked: injection / phishing ▶ log_and_reject      (kind=malicious, rule)              │
          passed ─────────────────────────────────────────────────────────────────────────────────┘
                                                      ▼
                                           detect_escalation_reply
                              ┌── reply to our escalation ───┴── anything else ──┐
                              ▼                                                  ▼
                     handle_escalation_reply                           analyze_deal_status (LLM)
                     kind=escalation_reply                                       ▼
                              │                                        fact_check_validator
                              │                              ┌── confirmed ──────┴── false_alarm /
                              │                              ▼                        failed_validation
                              │                    trigger_sales_system                     ▼
                              │                    kind=deal_closed                   archive_skip
                              ▼                              ▼                         kind=no_deal
                             END                            END                             ▼
                                                                                           END

The guardrail is built by ``create_email_guardrail_node`` (see ``email_guardrail.py``) on the reusable factory in
``agents/commons/guardrail``: it writes ``guardrail_check`` (blocked? which rule? which category?) and the graph
routes on that. Reply detection stays deterministic (Gmail thread / subject token) — an LLM never decides that an
email resumes a paused run — but it only ever sees mail that passed the guardrail, so a spoofed or malicious
"reply" cannot reach the tier workflow. Compiled without its own checkpointer: as a node of the intake workflow it
inherits that graph's.
"""
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from src.agents.email_analyser.email_guardrail import create_email_guardrail_node, route_after_email_guardrail
from src.agents.email_analyser.nodes import (
    analyze_deal_status_node,
    archive_skip_node,
    detect_escalation_reply_node,
    fact_check_validator_node,
    fetch_email_node,
    handle_escalation_reply_node,
    log_and_reject_node,
    route_after_reply_detection,
    route_deal_validation,
    trigger_sales_system_node,
)
from src.agents.email_analyser.state import EmailAnalyserState


def build_email_analyser_graph() -> CompiledStateGraph:
    graph = StateGraph(EmailAnalyserState)
    graph.add_node("fetch_email", fetch_email_node)
    graph.add_node("guardrail", create_email_guardrail_node())
    graph.add_node("log_and_reject", log_and_reject_node)
    graph.add_node("archive_skip", archive_skip_node)
    graph.add_node("detect_escalation_reply", detect_escalation_reply_node)
    graph.add_node("handle_escalation_reply", handle_escalation_reply_node)
    graph.add_node("analyze_deal_status", analyze_deal_status_node)
    graph.add_node("fact_check_validator", fact_check_validator_node)
    graph.add_node("trigger_sales_system", trigger_sales_system_node)

    graph.add_edge(START, "fetch_email")
    graph.add_edge("fetch_email", "guardrail")
    graph.add_conditional_edges(
        "guardrail", route_after_email_guardrail, ["archive_skip", "log_and_reject", "detect_escalation_reply"]
    )
    graph.add_conditional_edges(
        "detect_escalation_reply", route_after_reply_detection, ["handle_escalation_reply", "analyze_deal_status"]
    )
    graph.add_edge("analyze_deal_status", "fact_check_validator")
    graph.add_conditional_edges(
        "fact_check_validator", route_deal_validation, ["trigger_sales_system", "archive_skip"]
    )
    graph.add_edge("handle_escalation_reply", END)
    graph.add_edge("trigger_sales_system", END)
    graph.add_edge("archive_skip", END)
    graph.add_edge("log_and_reject", END)

    return graph.compile()
