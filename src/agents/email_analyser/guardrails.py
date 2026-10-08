"""Deterministic guardrails for the email analyser: PII redaction, prompt-
injection detection, and the closing-phrase checks the fact validator uses.

All pure functions — no I/O, no LLM — so they are cheap to unit-test.
"""
import re
from email.utils import parseaddr

# ── PII redaction ──────────────────────────────────────────────────────────
# Email addresses are deliberately NOT redacted: the customer contact's and
# the AE's addresses are exactly what the downstream deal parser extracts.

_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_PHONE = re.compile(
    r"(?<![\w$])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?|\d{2,4}[\s.-])\d{3,4}[\s.-]\d{3,4}(?![\w])"
)
_ADDRESS = re.compile(
    r"\b\d{1,6}\s+(?:[A-Z][\w.'-]*\s+){1,4}"
    r"(?i:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|way|place|pl)\b\.?"
    r"(?:,?\s+(?i:suite|ste|apt|unit)\.?\s*#?\w+)?"
)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _redact_cards(text: str) -> tuple[str, int]:
    count = 0

    def repl(match: re.Match) -> str:
        nonlocal count
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            count += 1
            return "[REDACTED_CARD]"
        return match.group()

    return _CARD.sub(repl, text), count


def redact_pii(text: str) -> tuple[str, dict[str, int]]:
    """Replace card numbers, SSNs, IBANs, phone numbers and street addresses
    with ``[REDACTED_*]`` placeholders. Returns the cleaned text and a count
    per category (only categories that matched).
    """
    counts: dict[str, int] = {}

    text, n = _redact_cards(text)
    if n:
        counts["CARD"] = n

    for label, pattern in (("SSN", _SSN), ("IBAN", _IBAN), ("PHONE", _PHONE), ("ADDRESS", _ADDRESS)):
        text, n = pattern.subn(f"[REDACTED_{label}]", text)
        if n:
            counts[label] = n

    return text, counts


# ── Prompt-injection detection ─────────────────────────────────────────────

_INJECTION_RULES: dict[str, re.Pattern] = {
    name: re.compile(pattern, re.IGNORECASE)
    for name, pattern in {
        "ignore_previous_instructions": r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|any|your|the)\b[^.\n]{0,30}\b(instructions?|rules?|prompts?|guidelines?|directions?)\b",
        "role_reassignment": r"\byou\s+are\s+(now|no\s+longer)\b|\bact\s+as\s+(a|an|the)\b|\bpretend\s+(to\s+be|you\s+are)\b",
        "system_prompt_probe": r"\b(reveal|show|print|repeat|leak)\b[^.\n]{0,30}\b(system\s+prompt|your\s+(instructions|prompt|rules))\b",
        "fake_system_markup": r"</?\s*(system|assistant|instructions?)\s*>|\[/?INST\]|<\|im_(start|end)\|>|^\s*system\s*:",
        "new_instructions": r"\bnew\s+(instructions?|rules?|task)\s*:",
        "forced_verdict": r"\b(mark|classify|label|treat|set|report)\b[^.\n]{0,40}\b(as|to)\b[^.\n]{0,20}\b(closed|won|signed|a\s+closed\s+deal|confirmed)\b[^.\n]{0,20}\b(regardless|no\s+matter|always|now)\b|\b(always|must)\s+(return|output|answer|respond)\b[^.\n]{0,30}\b(true|closed|confirmed)\b",
        "jailbreak_terms": r"\b(jailbreak|DAN\s+mode|developer\s+mode)\b",
    }.items()
}


def detect_injection(text: str) -> list[str]:
    """Names of every injection rule that matches ``text`` (empty if clean)."""
    return [name for name, pattern in _INJECTION_RULES.items() if pattern.search(text)]


# ── Phishing detection ─────────────────────────────────────────────────────
# Deterministic rules, tuned for an inbox that receives *internal sales* mail. A "hard" finding is
# enough on its own; "soft" findings (common in marketing and in phishing alike) only count in pairs.

_PHISHING_HARD: dict[str, re.Pattern] = {
    name: re.compile(pattern, re.IGNORECASE)
    for name, pattern in {
        "credential_request": r"\b(?:send|share|provide|enter|type|confirm|verify|give|reply\s+with)\b[^.\n]{0,40}\b(?:password|passcode|passphrase|one[- ]time\s+(?:code|password)|otp|2fa\s+code|security\s+code|login\s+credentials?)\b",
        "payment_redirect": r"\b(?:new|changed?|updated?)\s+(?:bank(?:ing)?|wire|payment|remittance)\s+(?:details|instructions|account)\b|\bchange\s+(?:the\s+)?(?:bank|payment)\s+(?:details|account)\b",
        "untraceable_payment": r"\bgift\s+cards?\b|\bwire\s+transfer\b|\b(?:bitcoin|crypto(?:currency)?|usdt)\s+(?:payment|wallet|transfer)\b",
        "ip_address_link": r"https?://\d{1,3}(?:\.\d{1,3}){3}\b",
        "link_with_credentials": r"https?://[^/\s]*@[^/\s]+",
        "punycode_link": r"https?://[^\s/]*xn--",
    }.items()
}

_PHISHING_SOFT: dict[str, re.Pattern] = {
    name: re.compile(pattern, re.IGNORECASE)
    for name, pattern in {
        "account_verification_lure": r"\b(?:verify|confirm|validate|re-?activate|unlock|restore)\b[^.\n]{0,30}\b(?:your|the)\s+(?:account|mailbox|email\s+account|identity)\b",
        "account_threat": r"\b(?:account|mailbox|access)\b[^.\n]{0,40}\b(?:suspended|locked|disabled|terminated|deactivated|will\s+be\s+(?:closed|deleted))\b",
        "pressure": r"\b(?:immediate\s+action|act\s+now|urgent\s+action\s+required|final\s+notice|within\s+24\s+hours)\b",
        "shortened_link": r"https?://(?:www\.)?(?:bit\.ly|tinyurl\.com|goo\.gl|ow\.ly|is\.gd|cutt\.ly|rb\.gy|t\.ly|shorturl\.at)/",
        "click_here": r"\b(?:click|tap)\s+(?:here|the\s+link\s+below|below)\b",
    }.items()
}

_DISPLAY_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def detect_phishing(text: str, sender: str = "") -> list[str]:
    """Names of the phishing signals that, by policy, make this email suspicious (empty if it is fine).

    * any one *hard* signal (asking for credentials, new bank details, gift cards / wire / crypto, a link to a
      bare IP, a link with embedded credentials, punycode) is enough;
    * *soft* signals (account-verification lures, account threats, pressure, shortened links, "click here")
      count only when two or more appear together;
    * a sender whose display name shows a different address than the real one (``"ceo@acme.com" <x@evil.test>``)
      is a hard signal.
    """
    findings = [name for name, pattern in _PHISHING_HARD.items() if pattern.search(text)]

    # ``parseaddr`` mangles an unquoted display name that contains an "@", so split ``Name <address>`` by hand first.
    named = re.match(r'\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$', sender or "")
    display, address = (named.group(1), named.group(2).strip()) if named else parseaddr(sender or "")
    shown = _DISPLAY_ADDRESS.search(display or "")
    if shown and address and shown.group(0).lower() != address.lower():
        findings.append("display_name_spoof")

    soft = [name for name, pattern in _PHISHING_SOFT.items() if pattern.search(text)]
    if len(soft) >= 2:
        findings += soft
    return findings


# ── Closing-phrase checks (used by the fact-check validator) ───────────────

_DEFINITIVE_CLOSE = re.compile(
    r"\bclosed[\s-]*won\b"
    r"|\bdeal\s+(?:is\s+|has\s+been\s+|was\s+)?(?:closed|signed|done|won)\b"
    r"|\b(?:we|i)\s+(?:just\s+|have\s+|finally\s+)?(?:closed|won|signed)\b"
    r"|\bsigned\s+(?:the\s+|a\s+)?(?:contract|agreement|order\s+form|msa|sow|paperwork)\b"
    r"|\b(?:contract|agreement|order\s+form|msa|sow|paperwork)\s+(?:has\s+been\s+|is\s+|was\s+|got\s+)?(?:signed|executed|countersigned)\b"
    r"|\bcountersigned\b|\bfully\s+executed\b"
    r"|\b(?:po|purchase\s+order)\s+(?:has\s+been\s+)?(?:received|issued)\b"
    r"|\bbooked\b",
    re.IGNORECASE,
)

_SIGNED_ATTACHMENT = re.compile(
    r"\b(?:signed|executed|countersigned)\b[^.\n]{0,40}\b(?:attached|attachment|enclosed)\b"
    r"|\b(?:attached|attachment|enclosed)\b[^.\n]{0,40}\b(?:signed|executed|countersigned)\b",
    re.IGNORECASE,
)

_HEDGING = re.compile(
    r"\b(?:expected|expecting|hoping|hope|aiming|trying|should|likely|projected)\s+(?:to\s+)?(?:close|sign|land|win)\b"
    r"|\b(?:pending|awaiting|waiting\s+(?:for|on))\s+(?:the\s+|their\s+|final\s+)?(?:signatures?|approval|legal|procurement|countersignature)\b"
    r"|\b(?:to\s+be|yet\s+to\s+be|not\s+yet|hasn'?t\s+been)\s+(?:signed|executed|closed)\b"
    r"|\b(?:in|still\s+in)\s+negotiations?\b|\bforecast\b|\bif\s+all\s+goes\s+well\b"
    r"|\bwill\s+(?:be\s+)?(?:sign|close|finali[sz]e)\b"
    r"|\bclos(?:e|es|ing)\s+(?:next|this)\s+(?:week|month|quarter)\b",
    re.IGNORECASE,
)


def has_definitive_close(text: str) -> bool:
    return bool(_DEFINITIVE_CLOSE.search(text))


def has_signed_attachment_mention(text: str) -> bool:
    return bool(_SIGNED_ATTACHMENT.search(text))


def find_hedging(text: str) -> list[str]:
    """The tentative / negotiating phrases found in ``text``."""
    return [m.group(0) for m in _HEDGING.finditer(text)]


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_is_grounded(quote: str, text: str) -> bool:
    """True if ``quote`` appears verbatim (ignoring case/whitespace) in ``text``."""
    needle = _normalise(quote)
    return bool(needle) and needle in _normalise(text)
