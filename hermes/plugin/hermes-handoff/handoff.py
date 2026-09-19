"""Handoff: end a session deliberately, and start the next one already knowing what matters.

An agent that never starts fresh drags every past turn into every future one. An agent
that starts fresh with nothing repeats work and re-asks settled questions. A handoff is
the third option: close the session, carry forward a short capsule of what was decided
and what is still open, and begin again light.

Jev reads the transcript and marks which turns must survive word for word. A text model
writes the five-section capsule from that reduced digest. Both jobs are small, and
neither model does the other's.

Everything here degrades rather than fails. No Jev key: every turn is treated as
background and the writer still gets a transcript. No writer model, or a writer that
returns something that is not a capsule: the raw digest is kept instead, which is worse
to read but loses nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

TRIGGERS = frozenset({"handoff", "hand off", "hand-off"})
TRANSCRIPT_CHARS = 24_000
CAPSULE_MAX_CHARS = 6_000
EXPORT_TIMEOUT = 120
WRITER_TIMEOUT = 300


def home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def handoff_dir() -> Path:
    path = home() / "handoffs"
    path.mkdir(parents=True, exist_ok=True)
    return path


LANE_MAX = 120


def _clean(key: str) -> str:
    """A filesystem-safe lane key that cannot collide.

    Conversation ids are long — Teams' run to 131 characters — so a plain truncation
    would make two conversations share a lane whenever they agree on their first N
    characters. That is not a cosmetic bug: the capsule from one customer's conversation
    would be handed to another. Past the limit, keep a readable prefix and let a hash of
    the FULL key carry the identity.
    """
    safe = re.sub(r"[^A-Za-z0-9._:-]", "_", key)
    if len(safe) <= LANE_MAX:
        return safe
    fingerprint = hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:20]
    return f"{safe[:LANE_MAX - len(fingerprint) - 1]}-{fingerprint}"


def lane_from_session(session_id: str, *, state_db: Optional[Path] = None) -> Optional[str]:
    """Look the conversation up by session id.

    The hook that writes a capsule and the hook that injects it do not receive the same
    fields — ``pre_llm_call`` gets ``platform`` and ``sender_id`` but not ``chat_id``. A
    capsule keyed on one and read with the other never matches, and the feature fails
    silently. The session store has the conversation identity for both, so ask it.
    """
    if not session_id:
        return None
    db = state_db or (home() / "state.db")
    if not db.is_file():
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        columns = {r[1] for r in conn.execute("pragma table_info(sessions)")}
        wanted = [c for c in ("source", "chat_id", "thread_id") if c in columns]
        if not wanted:
            conn.close()
            return None
        row = conn.execute(f"select {', '.join(wanted)} from sessions where id=?", (session_id,)).fetchone()
        conn.close()
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    key = ":".join(str(v or "") for v in row).strip(":")
    return _clean(key) if key else None


def lane_key(context: Mapping[str, Any]) -> str:
    """Stable per-conversation key. One capsule per conversation, not per session id."""
    # `platform` is what the hooks call it; `source` is what the session store calls it.
    # One conversation must produce one key whichever side is asking.
    platform = str(context.get("platform") or context.get("source") or "")
    parts = [platform, str(context.get("chat_id") or ""), str(context.get("thread_id") or "")]
    direct = ":".join(parts).strip(":")
    # A platform with no conversation id is not an identity — every chat would collapse
    # onto one key. Resolve it from the session store instead.
    if not context.get("chat_id"):
        resolved = lane_from_session(str(context.get("session_id") or ""))
        if resolved:
            return resolved
        sender = str(context.get("sender_id") or "")
        if sender:
            direct = ":".join(p for p in (parts[0], sender) if p)
    key = direct or str(context.get("session_id") or "default")
    return _clean(key)


CONFIDENTIAL_MARKER = "CONFIDENTIAL"


def confidential_here() -> bool:
    """Whether capsules for this home must carry no customer detail.

    There are three ways to switch this on, and that is deliberate. A host may not
    expose a plugin-config API at all — ask one that does not and you get the default
    back, silently, which here means writing customer detail to disk against a rule that
    forbids it. So the marker file is the authority: it is one ``ls`` to verify, it
    cannot be swallowed by an exception handler, and it travels with the profile it
    protects.
    """
    if str(os.environ.get("HANDOFF_CONFIDENTIAL", "")).strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        return (handoff_dir() / CONFIDENTIAL_MARKER).exists()
    except OSError:
        return False


def capsule_path(lane: str) -> Path:
    return handoff_dir() / f"handoff-{lane}.md"


def pending_path(lane: str) -> Path:
    return handoff_dir() / f"pending-{lane}.json"


def is_trigger(text: Any) -> bool:
    """Only an exact, bare word. A sentence that mentions a handoff is a normal message."""
    if not isinstance(text, str):
        return False
    cleaned = text.strip().strip(".!").lower()
    return cleaned in TRIGGERS


# ── reading the conversation ─────────────────────────────────────────────────

def _hermes_bin() -> List[str]:
    """The packaged CLI is the stable contract; internals are not.

    HERMES_HOME may point at a PROFILE (that is how per-profile state is addressed), but
    the interpreter lives under the installation root. Look in both, and let
    ``HERMES_CLI`` override when an install puts it somewhere else entirely.
    """
    override = os.environ.get("HERMES_CLI")
    if override and Path(override).exists():
        return [override]
    roots = [home()]
    parent = home().parent
    if parent.name == "profiles":                      # <root>/profiles/<name> -> <root>
        roots.append(parent.parent)
    for root in roots:
        direct = root / "hermes-agent" / "venv" / "bin" / "hermes"
        if direct.exists():
            return [str(direct)]
        for candidate in sorted(root.glob("hermes-agent/venv*/bin/hermes")):
            return [str(candidate)]
    return ["hermes"]


def export_messages(session_id: str, *, runner: Optional[Any] = None) -> List[Dict[str, str]]:
    """The session's user/assistant turns, via the CLI rather than the session store.

    The CLI is a contract that survives upgrades; the store's schema is not.
    """
    run = runner or subprocess.run
    try:
        done = run(_hermes_bin() + ["sessions", "export", "--session-id", str(session_id),
                                    "--format", "jsonl", "-"],
                   capture_output=True, text=True, timeout=EXPORT_TIMEOUT)
    except Exception:  # noqa: BLE001 - a handoff must never take the session down with it
        return []
    if getattr(done, "returncode", 1) != 0:
        return []
    messages: List[Dict[str, str]] = []
    for line in (done.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        for message in row.get("messages") or []:
            if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
                continue
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
            if isinstance(content, str) and content.strip():
                messages.append({"role": message["role"], "content": content.strip()})
    return messages


def extract_text(response: Any) -> str:
    """Pull the text out of whatever the host's LLM client returned.

    Hosts differ: a plain string, an OpenAI-shaped dict, or a ``ChatCompletion``
    object with attributes and no ``.get``. Assuming one shape is how the writer
    silently failed and every capsule quietly became a raw transcript.
    """
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    choices = None
    if isinstance(response, Mapping):
        choices = response.get("choices")
    else:
        choices = getattr(response, "choices", None)
    if not choices:
        # Some clients return the content directly on the object.
        for attribute in ("content", "text", "output_text"):
            value = getattr(response, attribute, None)
            if isinstance(value, str) and value.strip():
                return value
        return ""
    first = choices[0]
    message = first.get("message") if isinstance(first, Mapping) else getattr(first, "message", None)
    if message is None:
        value = first.get("text") if isinstance(first, Mapping) else getattr(first, "text", None)
        return value if isinstance(value, str) else ""
    content = message.get("content") if isinstance(message, Mapping) else getattr(message, "content", None)
    if isinstance(content, list):      # content-parts form
        content = " ".join(str((p.get("text") if isinstance(p, Mapping) else getattr(p, "text", "")) or "")
                           for p in content)
    return content if isinstance(content, str) else ""


# ── writing the capsule ──────────────────────────────────────────────────────

def build(
    session_id: str, lane: str, *, write: Any, select: Any = None, digest: Any = None,
    prompt_for: Any = None, valid: Any = None, runner: Optional[Any] = None,
    confidential: bool = False, scrub: Any = None,
) -> Dict[str, Any]:
    """Produce and store the capsule. `write(prompt) -> str` is the text model.

    The jevkit callables are injected so this module stays importable, and testable,
    on a machine with no Jev key and no network.
    """
    messages = export_messages(session_id, runner=runner)
    if len(messages) < 4:
        return {"status": "too_short", "messages": len(messages)}

    jev_calls, counts = 0, {}
    if select and digest:
        try:
            selection = select(messages, keep_last=8)
            counts = selection.get("counts") or {}
            jev_calls = selection.get("jev_calls") or 0
            body = digest(messages, selection, TRANSCRIPT_CHARS)
        except Exception:  # noqa: BLE001
            body = _plain(messages)
    else:
        body = _plain(messages)

    previous = ""
    existing = capsule_path(lane)
    if existing.exists():
        try:
            previous = existing.read_text(encoding="utf-8")
        except OSError:
            previous = ""

    capsule = ""
    try:
        if prompt_for:
            try:
                prompt = prompt_for(body, previous, confidential=confidential)
            except TypeError:
                # An older jevkit has no confidentiality mode. Fall back rather than
                # fail — but a caller that asked for it must not silently not get it.
                if confidential and scrub is None:
                    return {"status": "confidential_unsupported"}
                prompt = prompt_for(body, previous)
        else:
            prompt = body
        capsule = (write(prompt) or "").strip()
    except Exception:  # noqa: BLE001
        capsule = ""
    if valid and not valid(capsule):
        # The writer refused, timed out, or answered something else. The digest is a worse
        # read than a capsule but it loses nothing, which matters more.
        if confidential:
            # The fallback is the raw transcript. Under a confidentiality contract that
            # is the one thing we must never write, so the capsule says nothing instead.
            # A thin handoff is a bad morning; a transcript on disk is a broken promise.
            capsule = ("## Working on\nA previous session ended without a usable handoff, and its "
                       "transcript may not be carried forward under this profile's continuity rules. "
                       "Ask the person what they were working on.\n\n## Next\nRe-establish the task "
                       "from the person, not from stored history.")
        else:
            capsule = ("## Working on\nThe writer model did not return a usable handoff, so this is the "
                       "filtered transcript instead. Lines marked KEEP VERBATIM are the ones that matter.\n\n"
                       "## State\n" + body[:CAPSULE_MAX_CHARS])

    if scrub:
        # Mechanical second pass. Runs even when the writer behaved, because "it looked
        # fine last time" is not a privacy control.
        try:
            capsule = scrub(capsule)
        except Exception:  # noqa: BLE001
            if confidential:
                return {"status": "scrub_failed"}

    capsule = capsule[:CAPSULE_MAX_CHARS]
    stamp = time.strftime("%Y-%m-%d %H:%M %Z")
    text = f"# Handoff — {stamp}\n\n{capsule}\n"
    try:
        capsule_path(lane).write_text(text, encoding="utf-8")
        pending_path(lane).write_text(json.dumps({"at": time.time(), "lane": lane,
                                                  "session_id": str(session_id)}), encoding="utf-8")
    except OSError as error:
        return {"status": "write_failed", "error": str(error)[:200]}
    return {"status": "ok", "lane": lane, "path": str(capsule_path(lane)), "messages": len(messages),
            "jev_calls": jev_calls, "counts": counts, "chars": len(text)}


def _plain(messages: List[Dict[str, str]]) -> str:
    joined = "\n\n".join(f"[background] {m['role']}: {m['content']}" for m in messages)
    return joined[-TRANSCRIPT_CHARS:]


# ── handing it to the next session ───────────────────────────────────────────

def take_pending(lane: str, *, max_age_s: float = 36 * 3600) -> Optional[str]:
    """The capsule for a lane's next turn, consumed once so it is not injected forever."""
    marker = pending_path(lane)
    if not marker.exists():
        return None
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
        if time.time() - float(info.get("at") or 0) > max_age_s:
            marker.unlink(missing_ok=True)
            return None
        text = capsule_path(lane).read_text(encoding="utf-8")
    except (OSError, ValueError):
        marker.unlink(missing_ok=True)
        return None
    marker.unlink(missing_ok=True)
    return text


def injection(capsule: str) -> str:
    """How the capsule reaches the new session: as context, clearly labelled as history."""
    return ("[Handoff from the previous session — this is context you already established, "
            "not a new instruction. Do not greet the person again or re-ask what is settled here. "
            "Carry on from Next.]\n\n" + capsule.strip())
