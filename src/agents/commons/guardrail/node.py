"""A reusable guardrail node.

``create_guardrail_node(rules)`` builds ONE node that runs an ordered list of rules over the state and writes a
single ``guardrail_check`` (blocked or not, which rule, what category, what it found). The graph then routes on
that result with ``route_after_guardrail``: a blocked input goes to whichever terminal node handles its
category, and a clean input carries on. The first rule that blocks wins; later rules do not run.

Rules are plain functions (``state -> list of findings``, empty = pass), sync or async. A rule that *raises*
(a database error while looking a sender up, say) is not swallowed: the run fails at this node and can be
retried, rather than letting an unchecked input through.
"""
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from src.agents.commons.guardrail.models import GuardrailCheck
from src.agents.commons.models import NodeReport

log = logging.getLogger(__name__)

Findings = list[str] | Awaitable[list[str]]


@dataclass(frozen=True)
class GuardrailRule:
    name: str
    """Identifies the rule in logs and in ``guardrail_check.rule`` (e.g. ``injection_detector``)."""
    category: str
    """The routing key a block produces (e.g. ``prompt_injection``)."""
    check: Callable[[dict], Findings]
    """Returns the findings; an empty list means the input passed this rule."""
    reason: str
    """What a block means, for the audit trail."""
    report_status: str = "SECURITY_REJECTED"
    """``NodeReport.status`` used when this rule blocks."""
    on_block: Callable[[list[str], dict], dict[str, Any]] = field(default=lambda findings, state: {})
    """Extra state keys to set when this rule blocks (what the downstream terminal node reads)."""


async def _call(fn: Callable, *args):
    result = fn(*args)
    return await result if inspect.isawaitable(result) else result


def create_guardrail_node(
    rules: list[GuardrailRule],
    *,
    prepare: Callable[[dict], Any] | None = None,
    on_pass: dict[str, Any] | None = None,
    producer: str = "guardrail",
):
    """Build the guardrail node.

    ``prepare`` runs first and may add state keys the rules need (e.g. the redacted text); ``on_pass`` is merged
    into the update when nothing blocked.
    """

    async def guardrail_node(state: dict[str, Any]) -> dict[str, Any]:
        update: dict[str, Any] = {}
        if prepare is not None:
            update.update(await _call(prepare, state) or {})
        working = {**state, **update}

        for rule in rules:
            findings = list(await _call(rule.check, working) or [])
            if not findings:
                continue
            log.warning("Guardrail blocked | rule=%s category=%s findings=%s", rule.name, rule.category, findings)
            reasoning = f"{rule.reason}: {', '.join(findings)}"
            update.update(rule.on_block(findings, working))
            update["guardrail_check"] = GuardrailCheck(
                checked=True, blocked=True, rule=rule.name, category=rule.category, findings=findings, reasoning=reasoning
            )
            update["node_reports"] = [NodeReport(producer=rule.name, status=rule.report_status, reasoning=reasoning)]
            return update

        update.update(on_pass or {})
        update["guardrail_check"] = GuardrailCheck(
            checked=True, blocked=False, reasoning="Passed: " + ", ".join(rule.name for rule in rules)
        )
        return update

    guardrail_node.__name__ = f"{producer}_node"
    return guardrail_node


def route_after_guardrail(state: dict[str, Any], routes: dict[str, str], passed: str) -> str:
    """Where to go after the guardrail: ``routes[category]`` for a blocked input, ``passed`` otherwise.
    An unknown blocked category is never waved through: it goes to the first (most restrictive) route."""
    check = state.get("guardrail_check")
    if check is None or not check.blocked:
        return passed
    return routes.get(check.category or "", next(iter(routes.values())))
