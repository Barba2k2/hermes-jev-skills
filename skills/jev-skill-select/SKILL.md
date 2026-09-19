---
name: jev-skill-select
description: Use when unsure which of many installed skills applies to a request, if any, or when asked to make skill loading cheaper or more accurate. Jev ranks the whole catalog and may say no skill is needed.
version: 0.1.0
license: MIT
metadata:
  hermes:
    tags: [jev, typesafe, skills, routing]
---

# Skill selection with Jev

Two requests, about 0.9 s for a few hundred skills. The first ranks every skill against the turn (side-by-side batches, each with a "none" option). The second reads the top five properly, judges each on its own, and may reject them all. Small talk and ordinary turns come back with no skill.

## On Hermes

`/jev skills on` makes the `hermes-jev` plugin do this once per fresh turn. When a skill clearly fits, a one-line suggestion is attached to the turn naming it; load it with `skill_view` unless it plainly does not apply. It reads the profile's skills folder and respects `skills.disabled`.

## Asking directly (any agent)

```bash
jev pick-skill --turn "<the request>"            # searches Hermes, Claude Code, Codex and ./skills folders
jev pick-skill --turn "..." --root ~/my/skills   # or name the folders
```

Reply: `needs_skill` (0–1) and up to three `{name, path, match}`. Load the first one whose `match` is 0.5 or more. An empty list means proceed without a skill; do not go hunting for one.

## Notes

- It reads only each skill's `name` and `description` from its front matter, so a skill with a vague description will not be found. Fix the description, not the threshold.
- The turn is redacted before sending; a turn that looks like it holds a secret is not sent, and you get an empty list.
- A suggestion is advice. If the loaded skill does not match the task once you read it, drop it and carry on.
