"""Guardrails around the welcome message the LLM writes. Pure functions, no I/O.

The message goes to a *customer* in a shared channel, so the model's draft is checked before it is
posted: it cannot ping the whole channel, invent Slack mentions, or link anywhere except the
onboarding project. The model refers to teammates with ``{{csm}}`` / ``{{manager}}`` / ``{{ae}}``;
the tool swaps in the real Slack mention (or the person's name when they have no Slack ID), so the
model never sees or guesses a Slack ID.
"""
import re

ROLES = ("csm", "manager", "ae")
MIN_LENGTH = 40
MAX_LENGTH = 2500

_PLACEHOLDER = re.compile(r"\{\{\s*(csm|manager|ae)\s*\}\}", re.IGNORECASE)
_CHANNEL_PING = re.compile(r"<!(?:channel|here|everyone)\b|@(?:channel|here|everyone)\b", re.IGNORECASE)
_RAW_MENTION = re.compile(r"<@[A-Z0-9]+>|<#[A-Z0-9]+")
_URL = re.compile(r"https?://[^\s>|)\]]+", re.IGNORECASE)


def check_welcome(text: str, project_url: str | None) -> list[str]:
    """What is wrong with the draft (empty = fine). The messages are written for the model to act on."""
    text = (text or "").strip()
    problems: list[str] = []
    if len(text) < MIN_LENGTH:
        problems.append(f"too short: write a complete welcome message (at least {MIN_LENGTH} characters)")
    if len(text) > MAX_LENGTH:
        problems.append(f"too long: keep it under {MAX_LENGTH} characters")
    if _CHANNEL_PING.search(text):
        problems.append("it pings the whole channel (@channel / @here / @everyone): remove that")
    if _RAW_MENTION.search(text):
        problems.append("it contains a raw Slack mention: refer to teammates only as {{csm}}, {{manager}} or {{ae}}")
    for url in _URL.findall(text):
        if not project_url or not url.rstrip(".,").startswith(project_url.rstrip("/")):
            problems.append(f"it links to {url}, which is not the onboarding project: remove the link")
            break
    return problems


def render_welcome(text: str, mentions: dict[str, str], names: dict[str, str | None]) -> str:
    """Replace ``{{role}}`` with ``<@SLACKID>`` when known, otherwise with the person's name."""

    def swap(match: re.Match) -> str:
        role = match.group(1).lower()
        if mentions.get(role):
            return f"<@{mentions[role]}>"
        return names.get(role) or {"csm": "your CSM", "manager": "our CS manager", "ae": "your account executive"}[role]

    return _PLACEHOLDER.sub(swap, text.strip())
