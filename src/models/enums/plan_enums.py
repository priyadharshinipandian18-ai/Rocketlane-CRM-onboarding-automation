from enum import Enum


class PlanTierCode(str, Enum):
    """The two plans. Only ever set from a confirmed source (voice call or a
    human) — the LLM never chooses a tier."""
    ENTERPRISE = "enterprise"
    GROWTH = "growth"


class CsmModel(str, Enum):
    DEDICATED = "dedicated"
    POOLED = "pooled"


class CsRole(str, Enum):
    CSM = "csm"
    CS_MANAGER = "cs_manager"


class PlanTierSource(str, Enum):
    VOICE_CALL = "voice_call"
    MANUAL_OVERRIDE = "manual_override"
