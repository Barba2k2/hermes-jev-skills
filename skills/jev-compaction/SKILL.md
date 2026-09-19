---
name: jev-compaction
description: Use when shrinking a conversation you already have — a handoff, a session summary, a compaction capsule. Jev marks each turn keep, summarize or drop; you write the summary from what is left.
version: 0.1.0
license: MIT
metadata:
  hermes:
    tags: [jev, typesafe, compaction, handoff, context]
---

# Compaction and handoffs with Jev

Jev cannot write a summary. What it does is read the transcript turn by turn and mark each one:

- **keep**: carries a decision, a constraint, a preference, unfinished work, or an exact value, path, id, command or error that later work depends on. Survives word for word.
- **summarize**: background whose gist matters.
- **drop**: chatter, superseded attempts, repeated output.

You then summarize a fraction of the transcript, with the lines that must not be paraphrased already flagged. Handoffs get shorter and stop losing the one line that mattered.

## Do this

1. Get the transcript as a list of `{role, content}` messages. On Hermes: `hermes sessions export --format jsonl -`.
2. Select:

   - Hermes: call `jev_compact_select` with `messages`.
   - Anywhere else:

     ```bash
     jev compact-select --digest < transcript.json      # {"messages":[...]} or a bare list
     ```

3. Write the handoff **from `digest`**, not from the raw transcript:
   - Every `[KEEP VERBATIM]` line goes in unchanged, grouped under Decisions, Open work, or Pointers (paths, ids, commands).
   - `[background]` lines become at most one short paragraph of context.
   - Add nothing that is not in the digest.
4. Keep the handoff under 400 words. If it will not fit, you are paraphrasing keep-lines; list them instead.

## Guarantees

- The last six messages are always kept; system messages are always kept.
- Nothing is dropped unless Jev was confident (0.7+). An unjudged turn is summarized, never dropped.
- Turns that look like they hold a secret are not sent; they default to summarize.
- Jev down: every turn comes back `summarize`, which is exactly what you would have done without it.

## When to compact at all

`should_compact` is arithmetic, not a model call: compact at 60% of the window, urgently at 85%. Do not ask a model whether the window is full.
