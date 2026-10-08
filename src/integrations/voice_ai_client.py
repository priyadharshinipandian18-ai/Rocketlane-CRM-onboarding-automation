"""Voice AI client — places the "confirm plan tier" phone call to the AE.

Built against Bolna (https://www.bolna.ai/docs), an Indian voice-AI platform
that calls Indian numbers natively (calls arrive from a +91 number):

  * ``POST {BOLNA_API_BASE}/call`` with ``agent_id``, ``recipient_phone_number``
    and ``user_data`` (fills ``{{variables}}`` in the agent's prompt).
    Returns an ``execution_id`` immediately; the call runs asynchronously.
  * The agent itself — prompt, voice, **extractions** and **webhook URL** — is
    configured once in the Bolna dashboard (see ``docs/bolna_agent_setup.md``).
  * Bolna POSTs the execution payload to the agent's webhook URL every time
    the call's status changes (same ``id`` each time). Only terminal statuses
    matter here; for those the webhook re-fetches the execution with
    ``GET {BOLNA_API_BASE}/executions/{id}`` so it never trusts a webhook body
    on its own (Bolna webhooks are not signed).
  * The tier comes from the agent's extractions (``plan_tier`` +
    ``readback_confirmed``), and is only accepted when the AE confirmed the
    read-back — never guessed.

The rest of the code base only uses ``BolnaVoiceClient`` and the pure helpers
below, so swapping providers stays a one-file change (plus the webhook).
"""
import json
import logging
import re
from typing import Any, Optional

import httpx

from src.config.settings import get_settings

log = logging.getLogger(__name__)

PROVIDER = "bolna"

# Bolna call statuses (https://www.bolna.ai/docs/post-call/list-phone-call-status)
TERMINAL_STATUSES = {"completed", "busy", "no-answer", "failed", "canceled", "stopped", "error", "balance-low"}
_STATUS_MAP = {
    "scheduled": "queued",
    "queued": "queued",
    "rescheduled": "queued",
    "initiated": "ringing",
    "ringing": "ringing",
    "in-progress": "in_progress",
    "call-disconnected": "in_progress",   # line dropped; post-call processing still running
    "completed": "completed",
    "busy": "busy",
    "no-answer": "no_answer",
    "failed": "failed",
    "canceled": "failed",
    "stopped": "failed",
    "error": "failed",
    "balance-low": "failed",
}


class BolnaVoiceClient:
    def __init__(self):
        settings = get_settings()
        self._base = settings.bolna_api_base.rstrip("/")
        self._api_key = settings.bolna_api_key
        self._agent_id = settings.bolna_agent_id
        self._from_number = settings.bolna_from_phone_number

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    async def send_call(
        self,
        phone_number: str,
        ae_name: str,
        customer_name: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        """Place the call; returns Bolna's ``execution_id`` immediately."""
        user_data = {
            "ae_name": ae_name or "there",
            "ae_first_name": (ae_name or "there").split()[0],
            "customer_name": customer_name or "your new customer",
        }
        user_data.update({k: str(v) for k, v in (metadata or {}).items()})

        body: dict[str, Any] = {
            "agent_id": self._agent_id,
            "recipient_phone_number": phone_number,
            "user_data": user_data,
        }
        if self._from_number:
            body["from_phone_number"] = self._from_number

        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.post(f"{self._base}/call", headers=self._headers(), json=body)
            if response.status_code >= 400:
                log.error("Bolna call creation failed | status=%s body=%s", response.status_code, response.text[:500])
            response.raise_for_status()
            data = response.json()
            log.info("Voice call placed | execution_id=%s phone=%s status=%s",
                     data.get("execution_id"), phone_number, data.get("status"))
            return data["execution_id"]

    async def get_execution(self, execution_id: str) -> dict[str, Any]:
        """Authoritative copy of a call's result, straight from Bolna's API."""
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.get(f"{self._base}/executions/{execution_id}", headers=self._headers())
            response.raise_for_status()
            return response.json()


# ── Pure helpers for execution payloads (unit-tested, no I/O) ────────────


def map_call_status(bolna_status: Optional[str]) -> Optional[str]:
    """Bolna status → ``app.voice_calls.status``."""
    return _STATUS_MAP.get((bolna_status or "").lower())


def is_terminal(bolna_status: Optional[str]) -> bool:
    return (bolna_status or "").lower() in TERMINAL_STATUSES


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")


def _find(data: Any, wanted: str) -> Any:
    """Depth-first search for a key (case/space-insensitive) anywhere in the
    extraction output. Bolna groups extractions under category names in the
    current format, and uses flat keys in the legacy format — this handles both.
    """
    if isinstance(data, dict):
        for k, v in data.items():
            if _norm_key(k) == wanted:
                return v
        for v in data.values():
            found = _find(v, wanted)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find(item, wanted)
            if found is not None:
                return found
    return None


def _value_and_confidence(raw: Any) -> tuple[Optional[str], Optional[float]]:
    """``{"objective": "growth", "confidence": 0.9}`` (current format) or a
    plain string (legacy format) → ``("growth", 0.9)``."""
    if raw is None:
        return None, None
    if isinstance(raw, dict):
        value = raw.get("objective", raw.get("subjective", raw.get("value", raw.get("answer"))))
        confidence = raw.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None
        return (str(value) if value is not None else None), confidence
    return str(raw), None


def _normalise_plan(value: Optional[str]) -> str:
    v = (value or "").strip().lower()
    has_ent, has_growth = "enterprise" in v, "growth" in v
    if has_ent and not has_growth:
        return "enterprise"
    if has_growth and not has_ent:
        return "growth"
    return "unknown"


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"yes", "y", "true", "confirmed", "1"}


def parse_extraction(extracted_data: Any) -> dict[str, Any]:
    """Pull ``plan_tier`` / ``readback_confirmed`` out of Bolna's
    ``extracted_data`` (either format). Missing or ambiguous → ``unknown``.
    """
    if isinstance(extracted_data, str):
        try:
            extracted_data = json.loads(extracted_data)
        except json.JSONDecodeError:
            extracted_data = {}

    plan_raw, confidence = _value_and_confidence(_find(extracted_data or {}, "plan_tier"))
    readback_raw, _ = _value_and_confidence(_find(extracted_data or {}, "readback_confirmed"))
    return {
        "plan_tier": _normalise_plan(plan_raw),
        "readback_confirmed": _truthy(readback_raw),
        "confidence": confidence,
        "raw_plan_value": plan_raw,
    }


TRANSCRIPT_CONFIDENCE = 0.8
"""Confidence assigned to a tier read from the transcript: above the default BOLNA_MIN_CONFIDENCE (0.7),
below a deliberately strict setting (0.9), so an operator can turn this path off by raising the bar."""

_TURN = re.compile(r"^\s*(assistant|user)\s*:\s*(.*)$", re.IGNORECASE)
_TIER_WORD = re.compile(r"\b(enterprise|growth)\b", re.IGNORECASE)
_AFFIRM = re.compile(r"\b(yes|yeah|yep|yup|correct|right|exactly|confirmed?|sure|absolutely)\b", re.IGNORECASE)
_NEGATE = re.compile(r"\b(no|nope|not|wrong|incorrect|actually|wait|change|instead)\b|n't\b", re.IGNORECASE)


def _transcript_turns(transcript: Optional[str]) -> list[tuple[str, str]]:
    """``"assistant: …\\nuser: …"`` → ``[("assistant", "…"), ("user", "…")]`` (continuation lines are joined)."""
    turns: list[list[str]] = []
    for line in (transcript or "").splitlines():
        match = _TURN.match(line)
        if match:
            turns.append([match.group(1).lower(), match.group(2).strip()])
        elif turns and line.strip():
            turns[-1][1] += " " + line.strip()
    return [(who, text) for who, text in turns]


def parse_transcript(transcript: Optional[str]) -> dict[str, Any]:
    """Fallback for when Bolna returns no extractions: read the plan from the call transcript.

    As strict as the agent extraction it stands in for. A tier counts only when all three hold:

    1. the AE **stated** exactly one plan (no negation, not both words);
    2. the assistant **read that same plan back**, naming only it;
    3. the AE's **very next turn** is an affirmative with no negation.

    Anything else — a voicemail, a correction, "not sure", a mismatch between what the AE said and what was
    read back — is ``unknown``. A later, different statement by the AE cancels an earlier confirmation.
    """
    unknown = {"plan_tier": "unknown", "readback_confirmed": False, "confidence": None, "raw_plan_value": None}
    turns = _transcript_turns(transcript)
    stated: Optional[str] = None
    confirmed: Optional[str] = None
    for index, (who, text) in enumerate(turns):
        tiers = {t.lower() for t in _TIER_WORD.findall(text)}
        if who == "user":
            if tiers:
                new = next(iter(tiers)) if len(tiers) == 1 and not _NEGATE.search(text) else None
                if new != stated:
                    confirmed = None                       # the AE changed their answer
                stated = new
            continue
        if stated and tiers == {stated} and index + 1 < len(turns) and turns[index + 1][0] == "user":
            reply = turns[index + 1][1]
            if _AFFIRM.search(reply) and not _NEGATE.search(reply):
                confirmed = stated
    if confirmed is None:
        return unknown
    return {"plan_tier": confirmed, "readback_confirmed": True, "confidence": TRANSCRIPT_CONFIDENCE,
            "raw_plan_value": f"{confirmed} (read from the transcript)"}


def decide_outcome(
    bolna_status: Optional[str],
    plan_tier: Optional[str],
    readback_confirmed: bool,
    confidence: Optional[float],
    min_confidence: float,
    hangup_reason: Optional[str] = None,
) -> dict[str, Any]:
    """Turn a finished execution into the payload that resumes the graph.
    The tier is accepted only from a completed call where the AE confirmed
    the read-back (and, if Bolna reports one, confidence ≥ ``min_confidence``).
    """
    status = (bolna_status or "").lower()
    if (
        status == "completed"
        and plan_tier in ("enterprise", "growth")
        and readback_confirmed
        and (confidence is None or confidence >= min_confidence)
    ):
        return {"outcome": "confirmed", "plan_tier": plan_tier}

    if "voicemail" in (hangup_reason or "").lower():
        return {"outcome": "voicemail"}
    if status in ("busy", "no-answer"):
        return {"outcome": "no_answer"}
    if status in ("failed", "canceled", "stopped", "error", "balance-low"):
        return {"outcome": "failed"}
    return {"outcome": "unclear"}
