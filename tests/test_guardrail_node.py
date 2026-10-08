"""The reusable guardrail node and its routing (no graph, DB or LLM)."""
import os

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")

import pytest

from src.agents.commons.guardrail import GuardrailCheck, GuardrailRule, create_guardrail_node, route_after_guardrail

ROUTES = {"injection": "reject", "scope": "archive"}


def rule(name, category, findings, calls=None, **kwargs):
    def check(state):
        if calls is not None:
            calls.append(name)
        return findings
    return GuardrailRule(name=name, category=category, check=check, reason=f"{name} blocked", **kwargs)


async def test_an_input_that_passes_every_rule_is_marked_checked_and_not_blocked():
    node = create_guardrail_node([rule("a", "scope", []), rule("b", "injection", [])], on_pass={"security_status": "safe"})
    out = await node({"text": "hi"})
    assert out["guardrail_check"] == GuardrailCheck(checked=True, blocked=False, reasoning="Passed: a, b")
    assert out["security_status"] == "safe" and "node_reports" not in out


async def test_the_first_rule_that_blocks_wins_and_later_rules_do_not_run():
    ran = []
    node = create_guardrail_node([
        rule("scope_rule", "scope", [], ran), rule("injection_rule", "injection", ["bad"], ran), rule("late_rule", "scope", ["x"], ran),
    ])
    out = await node({})
    check = out["guardrail_check"]
    assert (check.blocked, check.rule, check.category, check.findings) == (True, "injection_rule", "injection", ["bad"])
    assert ran == ["scope_rule", "injection_rule"]
    assert out["node_reports"][0].producer == "injection_rule" and out["node_reports"][0].status == "SECURITY_REJECTED"


async def test_a_block_can_set_extra_state_and_its_own_report_status():
    node = create_guardrail_node([
        rule("gate", "scope", ["not_an_ae"], report_status="NOT_A_DEAL", on_block=lambda f, s: {"skip_reason": f[0]}),
    ], on_pass={"security_status": "safe"})
    out = await node({})
    assert out["skip_reason"] == "not_an_ae" and out["node_reports"][0].status == "NOT_A_DEAL"
    assert "security_status" not in out                      # on_pass is only for a clean input


async def test_prepare_runs_first_and_its_output_reaches_the_rules():
    seen = {}

    def check(state):
        seen.update(state)
        return []

    node = create_guardrail_node(
        [GuardrailRule("r", "scope", check, "r blocked")], prepare=lambda state: {"safe_text": state["raw"].upper()}
    )
    out = await node({"raw": "hello"})
    assert seen["safe_text"] == "HELLO" and out["safe_text"] == "HELLO"


async def test_async_rules_are_supported_and_a_failing_rule_is_not_swallowed():
    async def lookup(state):
        return []

    async def broken(state):
        raise ConnectionError("database down")

    assert (await create_guardrail_node([GuardrailRule("ok", "scope", lookup, "x")])({}))["guardrail_check"].blocked is False
    with pytest.raises(ConnectionError):                     # fail closed: nothing unchecked slips through
        await create_guardrail_node([GuardrailRule("db", "scope", broken, "x")])({})


def test_routing_follows_the_blocked_category():
    blocked = lambda category: {"guardrail_check": GuardrailCheck(checked=True, blocked=True, category=category)}   # noqa: E731
    assert route_after_guardrail(blocked("injection"), ROUTES, passed="go") == "reject"
    assert route_after_guardrail(blocked("scope"), ROUTES, passed="go") == "archive"
    assert route_after_guardrail({"guardrail_check": GuardrailCheck(checked=True, blocked=False)}, ROUTES, passed="go") == "go"
    assert route_after_guardrail({}, ROUTES, passed="go") == "go"


def test_an_unmapped_blocked_category_is_never_waved_through():
    unknown = {"guardrail_check": GuardrailCheck(checked=True, blocked=True, category="something_new")}
    assert route_after_guardrail(unknown, ROUTES, passed="go") == "reject"      # the first (most restrictive) route
