"""The outbound boundary. Everything sent to Jev passes through here first.

Two tools: ``redact`` masks things that look like secrets or contact details, and
``is_sensitive`` says "do not send this at all". Callers that get a True from
``is_sensitive`` must skip Jev and take their fail-open path.
"""
from __future__ import annotations

import re
import unicodedata

_SECRET_WORDS = re.compile(
    r"(?i)(api[_ -]?key|access[_ -]?token|authorization\s*:|bearer\s+[a-z0-9._-]{8,}|password|passwd|"
    r"client[_ -]?secret|session[_ -]?cookie|credit[_ -]?card|card[_ -]?number|"
    r"\bcvv\b|\bssn\b|private[_ -]?key|BEGIN [A-Z ]*PRIVATE KEY)"
)
# An env-var name is how a secret usually appears in agent output: AWS_SECRET_ACCESS_KEY,
# STRIPE_SECRET, DB_PASSWORD, GITHUB_TOKEN. Matching only `secret_key` missed every one of
# them, because the revealing word sits in the middle of the name, not at its end.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b[A-Z][A-Z0-9]*(?:[_-][A-Z0-9]+)*[_-]"
    r"(?:SECRET|SECRET[_-]?\w*KEY|API[_-]?KEY|KEY|TOKEN|PASSWORD|PASSWD|CREDENTIALS?|AUTH)\b"
    r"\s*[:=]\s*\S*"
)
_SECRET_NAME = re.compile(r"(?i)\bsecret[_ -](?:access[_ -])?key\b|\bsecret[_ -]?key\b")
_TOKEN_SHAPES = re.compile(
    r"\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|"
    r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|apikey_[A-Za-z0-9_]{20,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})\b"
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# The lookbehind excludes letters as well as digits. Without that, the digit tail of a
# carrier tracking number reads as country-code + 3 + 3 + 4 and gets masked:
# "1Z999AA10123456784" became "1Z999AA[phone]". Those numbers are the operational spine
# of a shipping desk, and a redactor that silently eats them makes the text useless while
# looking like it worked. A real phone number never begins immediately after a letter.
_PHONE = re.compile(
    r"(?<![A-Za-z0-9])(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_LONG_HEX = re.compile(r"\b[a-fA-F0-9]{32,}\b")


def normalize(text: str) -> str:
    """Fold look-alike and invisible characters so a gate cannot be dodged with Unicode."""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(c for c in folded if unicodedata.category(c) not in {"Cf", "Cc"} or c in "\n\t")


def is_sensitive(text: str) -> bool:
    probe = normalize(text)
    return bool(_SECRET_WORDS.search(probe) or _SECRET_NAME.search(probe)
                or _SECRET_ASSIGNMENT.search(probe) or _TOKEN_SHAPES.search(probe))


def redact(text: str, limit: int = 4000) -> str:
    out = normalize(text)
    out = _TOKEN_SHAPES.sub("[secret]", out)
    # Keep the variable's NAME (it is often the useful signal) and mask only its value.
    out = _SECRET_ASSIGNMENT.sub(lambda m: re.split(r"[:=]", m.group(0), 1)[0].rstrip() + "=[secret]", out)
    out = _LONG_HEX.sub("[hex]", out)
    out = _EMAIL.sub("[email]", out)
    out = _PHONE.sub("[phone]", out)
    if len(out) > limit:
        half = limit // 2
        out = out[:half] + "\n[…]\n" + out[-half:]
    return out
