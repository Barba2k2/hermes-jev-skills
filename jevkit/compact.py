"""Compaction and handoffs: Jev decides what survives, a text model only writes it up.

Jev cannot summarize. What it can do, in one fast request, is read a transcript
turn by turn and mark each turn as something to carry forward word for word, to
fold into the summary, or to drop. The summarizing model then gets a fraction of
the transcript with the decisions, open work and pointers already pulled out, so
the handoff is shorter, cheaper, and stops losing the one line that mattered.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import client, privacy

TURN_CHARS = 700
BATCH = 40
FATE = {
    "keep": "Carries a decision, a constraint, a user preference, an unfinished task, an exact value, path, id, "
            "command or error that later work depends on",
    "summarize": "Useful background whose gist matters but whose exact wording does not",
    "drop": "Chatter, acknowledgements, superseded attempts, repeated output, or detail nothing later depends on",
}


def _text(message: Mapping[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, list):
        content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content)


def select(
    messages: Sequence[Mapping[str, Any]], *, keep_last: int = 6, timeout: float = 8.0,
    transport: Optional[client.Transport] = None,
) -> Dict[str, Any]:
    """Return a fate for every message index. The last ``keep_last`` are always kept."""
    total = len(messages)
    fates: Dict[int, str] = {index: "keep" for index in range(max(0, total - keep_last), total)}
    judged = [index for index in range(total) if index not in fates]
    for index in judged:
        fates[index] = "summarize"            # the fail-open default: nothing is dropped unless Jev said so
        if messages[index].get("role") == "system":
            fates[index] = "keep"
    judged = [i for i in judged if fates[i] != "keep" and _text(messages[i]).strip()]

    calls, errors, latency = 0, [], 0
    for group in client.batches(judged, BATCH):
        sendable = [i for i in group if not privacy.is_sensitive(_text(messages[i]))]
        if not sendable:
            continue
        state = {"turns": {f"T{i}": f"{messages[i].get('role', 'user')}: {privacy.redact(_text(messages[i]), TURN_CHARS)}"
                           for i in sendable}}
        questions = {f"t{i}": client.choice(f"For continuing this work later, what should happen to turn T{i}?", FATE)
                     for i in sendable}
        try:
            reply = client.ask(state, questions, timeout=timeout, transport=transport)
        except client.JevError as error:
            errors.append(error.code)
            continue
        calls += 1
        latency += reply["latency_ms"]
        for i in sendable:
            answer = reply["answers"][f"t{i}"]
            # Dropping is the only irreversible fate, so it needs a confident answer.
            if answer["choice"] == "drop" and answer["confidence"] < 0.7:
                continue
            fates[i] = answer["choice"]

    counts = {fate: sum(1 for value in fates.values() if value == fate) for fate in FATE}
    return {"status": "ok" if calls or not judged else "fail_open", "fates": {str(k): v for k, v in sorted(fates.items())},
            "counts": counts, "jev_calls": calls, "errors": errors, "latency_ms": latency}


def digest(messages: Sequence[Mapping[str, Any]], selection: Mapping[str, Any], limit: int = 24000) -> str:
    """The reduced transcript to hand to the summarizing model."""
    fates = selection["fates"]
    lines: List[str] = []
    for index, message in enumerate(messages):
        fate = fates.get(str(index), "summarize")
        if fate == "drop":
            continue
        body = _text(message).strip()
        if not body:
            continue
        if fate == "summarize":
            body = body[:400] + (" […]" if len(body) > 400 else "")
        marker = "KEEP VERBATIM" if fate == "keep" else "background"
        lines.append(f"[{marker}] {message.get('role', 'user')}: {body}")
    text = "\n\n".join(lines)
    return text if len(text) <= limit else text[-limit:]


# The five headings a handoff needs. More than this and the next session reads an essay
# instead of getting to work; fewer and it starts by rediscovering what was already decided.
HANDOFF_SECTIONS = ("Working on", "State", "Decisions", "Pointers", "Next")

HANDOFF_PROMPT = """Write a handoff so a fresh session can pick this work up cold.

Use exactly these five headings, in this order, nothing before or after:
## Working on
## State
## Decisions
## Pointers
## Next

Rules:
- Under 400 words total.
- Every line marked [KEEP VERBATIM] carries a decision, a constraint, an exact value, a path,
  an id, a command or an error. Carry those through UNCHANGED. Do not paraphrase them.
- [background] lines only need their gist, at most a sentence or two of context.
- Pointers means exact paths, ids, URLs, ports, branch names, commands. No prose there.
- Next means what the following session should actually do first, concretely.
- Write nothing you cannot support from the transcript below. No guessing, no filler,
  no "the user seems to want". If something is unknown, say it is unknown.
- Plain sentences. No bullets inside a section unless listing pointers.
"""


CONFIDENTIAL_RULES = """
CONFIDENTIALITY — these override the rules above wherever they conflict:
- This handoff is a BREADCRUMB, not a summary. Record only: the task, which classes of
  source were checked, what evidence is still missing, who owns or must approve it, and
  the next safe action.
- Never carry a person's name, an email address, a phone number, a postal address, a
  document or file name, a message id, a link, an account or invoice number, or any
  payment, health or disability detail. Write "the customer", "the staff member", "the
  quote in the shared drive" instead.
- This REPLACES the instruction to keep identifiers verbatim. Where a [KEEP VERBATIM]
  line contains an identifier of that kind, carry the decision it expresses and drop the
  identifier. A constraint like "ship by Tuesday, not to the second site" survives; the
  site's name does not.
- Internal technical pointers — file paths on our own servers, branch names, commands,
  ports, error strings — are fine and should be kept.
- If following these rules would leave a section with nothing to say, write "nothing
  recorded" under it rather than reaching for detail you are not allowed to keep.
"""


def redact_capsule(text: str, limit: int = 6000) -> str:
    """Mechanical backstop over a written capsule: emails, phones, tokens, long ids.

    The prompt above is the real control, because only the writer knows that "Kiridena"
    is a customer. This catches the shapes a regex *can* be sure about, so a writer that
    ignores its instructions still cannot leave a phone number on disk. Both layers
    exist because neither is sufficient: one is reliable but blind, the other sees but
    can be disobeyed.
    """
    return _GUID.sub("[id]", privacy.redact(text, limit=limit))


_GUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


def handoff_prompt(digest_text: str, previous: str = "", *, confidential: bool = False) -> str:
    """The full prompt for whatever text model writes the capsule. Jev cannot write it."""
    prompt = HANDOFF_PROMPT + (CONFIDENTIAL_RULES if confidential else "")
    if previous.strip():
        prompt += ("\nA PREVIOUS handoff for this same work is below. Carry forward anything still "
                   "true, especially Pointers, and fold in what has happened since. Do not lose "
                   "identifiers.\n\n<previous_handoff>\n" + previous.strip()[-6000:] + "\n</previous_handoff>\n")
    return prompt + "\n\nTRANSCRIPT (already filtered; read the markers):\n\n" + digest_text + "\n"


def looks_like_capsule(text: str) -> bool:
    """A cheap check that the writer produced a handoff and not an apology or a refusal."""
    if not text or len(text.strip()) < 40:
        return False
    found = sum(1 for heading in HANDOFF_SECTIONS if f"## {heading}" in text)
    return found >= 3


def should_compact(used_tokens: int, window_tokens: int, *, soft: float = 0.6, hard: float = 0.85) -> Dict[str, Any]:
    """Pure arithmetic; no model needed to know the window is nearly full."""
    ratio = used_tokens / window_tokens if window_tokens else 0.0
    return {"ratio": round(ratio, 3), "compact": ratio >= soft, "urgent": ratio >= hard}
