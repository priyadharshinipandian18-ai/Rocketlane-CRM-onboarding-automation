"""Context engineering for the Slack agent: what the model is shown, and, just as important, what it is not.

The prompt TEXT lives in ``src/prompts/communication/`` (``agent_system.md``, ``agent_brief.md``); this module
builds and trims what goes into the conversation.

* The **system prompt** is stable: role, the goal, the tools' order and recovery rules, and how to write
  the welcome message. No customer data is in it.
* The **brief** is the only per-onboarding input: a small block of facts (names, plan, dates, the channel
  names to use, the project link). It contains no email addresses, no Slack IDs and no secrets; the tools
  look those up themselves from the database, so the model cannot leak or invent them.
* Customer-supplied text (company and contact names came from an email) is fenced inside ``<facts>`` and
  declared to be data, not instructions.
* The conversation is **windowed** before every model call so a long tool loop cannot grow the context.
"""
import json

from langchain_core.messages import BaseMessage, ToolMessage

from src.agents.communication.channel_plan import PHASES, ChannelPlan
from src.models.dto.comms_context import OnboardingCommsContext
from src.prompts import load_prompt, render_prompt

WINDOW = 24
"""Messages kept (besides the system prompt and the brief) when the model is called."""


def system_prompt() -> str:
    return load_prompt("communication/agent_system")


def build_brief(ctx: OnboardingCommsContext, plan: ChannelPlan) -> str:
    """The per-onboarding facts. Names and dates only: no emails, no Slack IDs."""

    def day(d):
        return d.strftime("%B %d, %Y").replace(" 0", " ") if d else "to be confirmed"

    team = {
        role: name
        for role, name in (("csm", ctx.csm_name), ("manager", ctx.manager_name), ("ae", ctx.ae_name))
        if name
    }
    facts = {
        "customer_company": ctx.customer_name,
        "contact_name": ctx.customer_contact_name or "there",
        "plan": ctx.tier_name,
        "csm_model": getattr(ctx.csm_model, "value", ctx.csm_model),
        "onboarding_days": ctx.duration_days,
        "start_date": day(ctx.start_date),
        "target_go_live": day(ctx.target_go_live_date),
        "team_roles_available": team,
        "phases": list(PHASES),
        "project_url": ctx.project_url or None,
        "channel_name_options": plan.name_candidates,
        "suggested_topic": plan.topic,
    }
    return render_prompt("communication/agent_brief", facts=json.dumps(facts, indent=2, ensure_ascii=False))


def window(messages: list[BaseMessage], keep: int = WINDOW) -> list[BaseMessage]:
    """System prompt + brief + the last ``keep`` messages, never starting on an orphaned tool result."""
    if len(messages) <= keep + 2:
        return messages
    head, tail = messages[:2], messages[-keep:]
    while tail and isinstance(tail[0], ToolMessage):
        tail = tail[1:]
    return head + tail
