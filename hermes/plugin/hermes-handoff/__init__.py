"""Hermes plugin: `handoff` — close a session deliberately and carry the thread forward.

Three seams, all public:

* ``pre_gateway_dispatch``  the bare word "handoff" builds the capsule and ends the turn
* ``/handoff``              the same thing, for anyone who prefers a command
* ``pre_llm_call``          the next session's first turn gets the capsule as context

The writer model is whatever the host already uses for auxiliary work, so this needs no
new credential. Jev is optional: without it every turn is treated as background and the
capsule is still written.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from . import handoff

_CTX: Any = None


def _setting(name: str, default: Any) -> Any:
    if _CTX is not None:
        try:
            value = _CTX.get_config(name, None)
            if value is not None:
                return value
        except Exception:  # noqa: BLE001
            pass
    return default


def _jevkit():
    """jevkit from beside us, or from the hermes-jev plugin; otherwise handoff runs without Jev."""
    # Vendored copy first: it makes this plugin self-contained, which matters when it is
    # deployed to a host that does not run the rest of the Jev toolkit.
    local = Path(__file__).resolve().parent
    if (local / "jevkit" / "compact.py").is_file():
        import sys
        if str(local) not in sys.path:
            sys.path.insert(0, str(local))
        try:
            from jevkit import compact  # type: ignore
            return compact
        except Exception:  # noqa: BLE001
            pass
    try:
        from hermes_jev.jevkit import compact  # type: ignore
        return compact
    except Exception:  # noqa: BLE001
        pass
    for base in (Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes"),):
        candidate = base / "plugins" / "hermes-jev"
        if (candidate / "jevkit" / "compact.py").is_file():
            import sys
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            try:
                from jevkit import compact  # type: ignore
                return compact
            except Exception:  # noqa: BLE001
                return None
    return None


def _writer() -> Any:
    """The host's own auxiliary model writes the capsule — no new credential, no new bill."""
    def write(prompt: str) -> str:
        from agent.auxiliary_client import call_llm  # type: ignore

        return handoff.extract_text(
            call_llm(task="compression", messages=[{"role": "user", "content": prompt}]))
    return write


def run_handoff(context: Dict[str, Any]) -> Dict[str, Any]:
    """Build and store the capsule for this conversation."""
    lane = handoff.lane_key(context)
    session_id = str(context.get("session_id") or "")
    if not session_id:
        return {"status": "no_session"}
    compact = _jevkit()
    # Some deployments are bound by a continuity rule that forbids carrying customer
    # detail forward. Where that is true this must be on, and the capsule becomes a
    # breadcrumb rather than a summary.
    confidential = (_setting("confidential", False) in (True, "true", "yes", "on", 1)
                    or handoff.confidential_here())
    return handoff.build(
        session_id, lane, write=_writer(),
        select=(compact.select if compact else None),
        digest=(compact.digest if compact else None),
        prompt_for=(compact.handoff_prompt if compact else None),
        valid=(compact.looks_like_capsule if compact else None),
        confidential=confidential,
        scrub=(compact.redact_capsule if (compact and confidential) else None))


# ── hooks ────────────────────────────────────────────────────────────────────

def _on_dispatch(**context: Any) -> Any:
    """The bare word `handoff`. A sentence mentioning one is left alone."""
    text = context.get("text") or context.get("message") or context.get("user_message")
    if not handoff.is_trigger(text):
        return None
    result = run_handoff(context)
    if result.get("status") == "ok":
        note = (f"Handoff saved ({result['messages']} turns read). Starting fresh — the next message "
                f"begins a new session and carries the summary forward.")
    elif result.get("status") == "too_short":
        note = "Nothing to hand off yet; this session has barely started."
    else:
        note = f"Could not write the handoff ({result.get('status')}). The session is unchanged."
    # `skip` ends the turn without spending a model call on it.
    return {"action": "skip", "message": note}


def _on_pre_llm_call(session_id: str = "", **context: Any) -> Any:
    """Give the new session its capsule, once."""
    if _setting("inject", True) in (False, "false", "off"):
        return None
    lane = handoff.lane_key({**context, "session_id": session_id})
    capsule = handoff.take_pending(lane)
    if not capsule:
        return None
    return {"context": handoff.injection(capsule)}


def _command(raw_args: str = "") -> str:
    context = {"session_id": os.environ.get("HERMES_SESSION_ID", ""),
               "platform": os.environ.get("HERMES_SESSION_PLATFORM", "")}
    result = run_handoff(context)
    if result.get("status") == "ok":
        return (f"Handoff written to {result['path']} from {result['messages']} turns. "
                "The next session starts fresh and carries it forward.")
    return f"No handoff written ({result.get('status')})."


_RULE = (
    "If the person says just \"handoff\", that is a request to close this session cleanly: the "
    "conversation so far is summarised into a short capsule and the next message starts a fresh "
    "session carrying it. When a session opens with a [Handoff from the previous session] block, "
    "that is history you already established — do not greet again or re-ask what it settles; "
    "continue from its Next section."
)


def register(ctx: Any) -> None:
    global _CTX
    _CTX = ctx
    ctx.register_hook("pre_gateway_dispatch", _on_dispatch)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_command("handoff", _command, description="Close this session and carry a summary forward")
    ctx.register_system_prompt_section("hermes-handoff", _RULE, max_chars=700)
