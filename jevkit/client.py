"""One small, strict client for TypeSafe Jev (POST /v1/systemone).

Jev answers typed questions about a state: ``choice`` (one of a closed set),
``score`` (a position on an ordered rubric) and ``noul`` (probability of yes).
It never writes text. Every helper here validates the reply against the question
that was asked, so a malformed or surprising answer becomes a ``JevError`` and
the caller takes its fail-open path rather than acting on junk.
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

from . import keystore

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY_ENDPOINT = "https://openrouter.ai/api/v1/key"
DEFAULT_MODEL = "jev-latest"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-4o-mini"
MAX_RESPONSE_BYTES = 1_000_000
MAX_STATE_CHARS = 60_000
USER_AGENT = "hermes-jev-skills/0.1"

State = Union[str, Mapping[str, Any], Sequence[Any]]
Transport = Callable[[bytes, Dict[str, str], float], bytes]


class JevError(RuntimeError):
    """Anything that means "do not trust or use this Jev result"."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# ── question builders ────────────────────────────────────────────────────────

def choice(instructions: str, criteria: Mapping[str, str]) -> Dict[str, Any]:
    if len(criteria) < 2:
        raise ValueError("a choice needs at least two options")
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def score(instructions: str, levels: Sequence[str]) -> Dict[str, Any]:
    if len(levels) < 2:
        raise ValueError("a score needs at least two levels")
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


def noul(instructions: str) -> Dict[str, Any]:
    return {"type": "noul", "instructions": instructions}


# ── transport ────────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, ANN001
        # A redirect would carry the bearer token to another origin.
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _http_transport(body: bytes, headers: Dict[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(ENDPOINT, data=body, headers=headers, method="POST")
    return _open(request, timeout)


def _open(request: urllib.request.Request, timeout: float) -> bytes:
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        code = {401: "auth_failed", 403: "auth_failed", 402: "credits_exhausted",
                429: "rate_limited", 529: "overloaded"}.get(error.code, f"http_{error.code}")
        raise JevError(code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise JevError("network") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise JevError("response_too_large")
    return raw


def _openrouter_transport(body: bytes, headers: Dict[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(OPENROUTER_ENDPOINT, data=body, headers=headers, method="POST")
    raw = _open(request, timeout)
    try:
        payload = json.loads(raw)
        content = payload["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        normalized = json.loads(content)
    except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise JevError("malformed", "OpenRouter reply has no JSON answer") from None
    return json.dumps(normalized, separators=(",", ":")).encode("utf-8")


def _openrouter_key_valid(api_key: str, timeout: float) -> bool:
    request = urllib.request.Request(
        OPENROUTER_KEY_ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json", "User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        payload = json.loads(_open(request, timeout))
    except (JevError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and isinstance(payload.get("data"), dict)


_RETRYABLE = {"rate_limited", "overloaded", "network", "http_500", "http_502", "http_503", "http_504"}


# ── validation ───────────────────────────────────────────────────────────────

def _unit(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JevError("malformed", f"{name} is not numeric")
    number = float(value)
    if not math.isfinite(number) or not -1e-6 <= number <= 1 + 1e-6:
        raise JevError("malformed", f"{name} is outside 0..1")
    return min(1.0, max(0.0, number))


def _check_answer(name: str, question: Mapping[str, Any], answer: Any) -> Dict[str, Any]:
    if not isinstance(answer, dict) or answer.get("type") != question["type"]:
        raise JevError("malformed", f"answer {name} has the wrong type")
    kind = question["type"]
    if kind == "noul":
        return {"type": "noul", "noul": _unit(answer.get("noul"), f"{name}.noul")}
    if kind == "choice":
        options = set(question["criteria"])
        picked = answer.get("choice")
        if picked not in options:
            raise JevError("malformed", f"answer {name} chose an option that was not offered")
        raw = answer.get("probabilities")
        if not isinstance(raw, dict) or not set(raw) <= options:
            raise JevError("malformed", f"answer {name} has bad probabilities")
        probabilities = {key: _unit(value, f"{name}.p[{key}]") for key, value in raw.items()}
        return {"type": "choice", "choice": picked, "probabilities": probabilities,
                "confidence": _unit(answer.get("confidence"), f"{name}.confidence")}
    levels = len(question["criteria"])
    value = answer.get("score")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise JevError("malformed", f"answer {name} has no numeric score")
    if not -0.5 <= float(value) <= levels - 0.5:
        raise JevError("malformed", f"answer {name} scored off the rubric")
    # The per-level spread says far more than the averaged score: an unsure answer averages to
    # the middle of the rubric, which looks like a real "medium-hard" unless you read the spread.
    raw = answer.get("probabilities")
    spread = {}
    if isinstance(raw, dict):
        for key, probability in raw.items():
            if str(key).isdigit() and int(key) < levels:
                spread[int(key)] = _unit(probability, f"{name}.p[{key}]")
    return {"type": "score", "score": float(value), "probabilities": spread,
            "confidence": _unit(answer.get("confidence", 1.0), f"{name}.confidence")}


# ── public call ──────────────────────────────────────────────────────────────

def ask(
    state: State,
    questions: Mapping[str, Mapping[str, Any]],
    *,
    timeout: float = 4.0,
    retries: int = 1,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    provider: Optional[str] = None,
    transport: Optional[Transport] = None,
) -> Dict[str, Any]:
    """Ask Jev every question against one state, in a single request.

    Returns ``{"answers": {...validated...}, "usage": {...}, "latency_ms": int}``.
    Raises ``JevError`` for anything the caller should not act on. ``timeout`` is a
    total wall-clock budget across retries, not a per-attempt one.
    """
    if not questions:
        raise ValueError("no questions")
    key = api_key or keystore.resolve(provider)
    if not key:
        raise JevError("no_key", "run `jev setup-key` or configure OPENROUTER_API_KEY")
    selected_provider = provider or ("openrouter" if key.startswith("sk-or-") else ("typesafe" if api_key else keystore.resolve_provider()))
    if selected_provider not in ("typesafe", "openrouter"):
        raise JevError("unknown_provider")
    encoded_state = state if isinstance(state, str) else json.dumps(state, separators=(",", ":"), default=str)
    if len(encoded_state) > MAX_STATE_CHARS:
        raise JevError("state_too_large")
    if selected_provider == "openrouter":
        prompt = (
            "Return ONLY valid JSON with keys answers and usage. Preserve the answer schema exactly.\n"
            f"STATE:\n{json.dumps(state, default=str)}\nQUESTIONS:\n{json.dumps(questions, default=str)}"
        )
        body = json.dumps({
            "model": model or os.environ.get("OPENROUTER_JEV_MODEL") or DEFAULT_OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": "You are a typed decision model. Follow the supplied question schemas exactly."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
        }, separators=(",", ":")).encode("utf-8")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                   "Accept": "application/json", "User-Agent": USER_AGENT,
                   "HTTP-Referer": "https://github.com/kerpopule/hermes-jev-skills",
                   "X-Title": "Hermes Jev Skills"}
        send = transport or _openrouter_transport
    else:
        body = json.dumps(
            {"state": state, "model": model or os.environ.get("TYPESAFE_MODEL") or DEFAULT_MODEL,
             "questions": {name: dict(q) for name, q in questions.items()}},
            separators=(",", ":"), default=str,
        ).encode("utf-8")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                   "Accept": "application/json", "User-Agent": USER_AGENT}
        send = transport or _http_transport

    started = time.monotonic()
    attempt = 0
    while True:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0.05:
            raise JevError("timeout")
        try:
            raw = send(body, headers, remaining)
            break
        except JevError as error:
            attempt += 1
            if error.code not in _RETRYABLE or attempt > retries:
                raise
            time.sleep(min(0.25 * attempt, max(0.0, timeout - (time.monotonic() - started) - 0.1)))

    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise JevError("malformed", "reply is not JSON") from None
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        raise JevError("malformed", "reply has no answers")
    checked = {name: _check_answer(name, question, answers.get(name)) for name, question in questions.items()}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return {"answers": checked, "usage": usage, "latency_ms": int((time.monotonic() - started) * 1000)}


def verify_key(api_key: str, provider: Optional[str] = None, timeout: float = 10.0) -> bool:
    """Validate credentials without spending a model request."""
    try:
        if provider == "openrouter":
            return _openrouter_key_valid(api_key, timeout)
        ask("The build finished and all tests passed.",
            {"ok": noul("The text reports a successful outcome")}, api_key=api_key, provider=provider, timeout=timeout)
        return True
    except JevError:
        return False


def batches(items: Sequence[Any], size: int) -> List[Sequence[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]
