"""State payload of the guardrail node (the same ``guardrail_check`` idea as rappit-ai-companion)."""
from src.models.base_model import BaseModel


class GuardrailCheck(BaseModel):
    """The guardrail's verdict on the current input, written to the graph state under ``guardrail_check``.

    ``blocked`` is ``None`` until the guardrail has run. When it blocked, ``rule`` names the rule that did it and
    ``category`` is the routing key the graph branches on (see ``route_after_guardrail``).
    """

    checked: bool = False
    blocked: bool | None = None
    rule: str | None = None
    category: str | None = None
    findings: list[str] = []
    reasoning: str | None = None
