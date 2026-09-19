---
name: jev-computer-use
description: Use when operating a desktop GUI (windows, menus, native apps, OS dialogs) through a computer-use driver. You observe and build a table of safe actions; Jev picks the next one in about 0.4 seconds instead of a large model reasoning over every screenshot.
version: 0.1.0
license: MIT
metadata:
  hermes:
    tags: [jev, typesafe, computer-use, gui, cua]
    related_skills: [jev-browser-use]
---

# Computer use with Jev

You stay the planner and the hands. Jev is only the fast "which one next?" in the middle. It returns an id from a table **you** built, so it cannot invent coordinates, text, selectors or tool calls. The worst a wrong answer can do is pick another action you already judged safe.

Web pages belong to `jev-browser-use`. This skill is for desktop apps and OS surfaces, driven through whatever computer-use driver you have (CUA Driver over MCP, the platform's native computer-use tool, an accessibility bridge).

## The loop

1. **Observe** with your driver. Prefer accessibility/semantic state over pixels. Every ref, capture id and coordinate is good for this observation only.
2. **Build the candidate table locally.** Each row is an opaque id plus one complete, prevalidated action. Always include:
   - `reobserve`: look again, change nothing
   - `abstain`: stop and ask for help
3. **Privacy gate.** Nothing sensitive goes to Jev: no credentials, tokens, cookies, password-field contents, payment data, customer data, screenshots, files or unbounded page text. If the screen holds such content, abstain or handle it without Jev.
4. **Ask once:**

   ```bash
   jev choose < request.json          # Hermes: the jev_choose_action tool, argument `request`
   ```

   ```json
   {"schema": "jev.action_choice_request_v1",
    "goal": "Open Settings and select Appearance.",
    "observation_id": "capture-0042",
    "regions": [{"id": "r1", "role": "button", "label": "Appearance", "interactive": true}],
    "history": [{"selected_id": "open-settings", "outcome": "settings window opened"}],
    "candidates": [
      {"id": "select-appearance", "description": "Click the Appearance row in the Settings sidebar."},
      {"id": "reobserve", "description": "Take a fresh observation without changing anything."},
      {"id": "abstain", "description": "Do not act; ask the person for help."}]}
   ```

   Pass the JSON on stdin or from a temp file. Never interpolate it into a shell string.
5. **Run exactly the one action** behind `selected_id`. Confidence under 0.80, or any Jev failure, comes back as `reobserve`. Never derive an action from anything but the id.
6. **Observe again and verify the postcondition yourself.** A chosen id, a delivered click or a screenshot is not proof. Check application state before the next step. Stop after a bounded number of steps.

## Authority

Driving a GUI gives you no new permissions. Sending, publishing, paying, purchasing, deleting, changing credentials or security settings, and anything touching customer data still need the person's explicit yes, exactly as they would without a GUI. Use your driver's standard permission mode; never an approval-bypass flag. The person does all sign-ins, 2FA and payment prompts themselves.

If the driver, the key or the target is unavailable: stop and say what is missing. Do not improvise another way to control the screen.

`jev choose --mock` answers `reobserve` with no network call, for testing your loop.

## Bundled runner

The loop above is the contract. `scripts/jev_gui_agent.py` is a working implementation of it —
the desktop counterpart to `jev-browser-use`'s runner — so you do not have to rebuild the
observe/choose/act cycle by hand:

```bash
python3 <this skill>/scripts/jev_gui_agent.py \
  --pid 26955 --window-id 46041 \
  --goal 'Open the Library page in YouTube Music' \
  --expect 'Library' --max-steps 12 --json
```

It drives `cua-driver` over MCP, builds the candidate table from the accessibility tree, sends
`jev.action_choice_request_v1`, and performs only the action behind the returned id. Exit 0
verified, 4 unverified, 2 refused to start, 6 abstained. `--max-regions` defaults to 26 so the
table stays inside the 32-candidate contract once `reobserve` and `abstain` are added.

If you cannot run it, fall back to the loop above by hand — but do **not** fall back to
AppleScript UI scripting, `xdotool` or coordinate clicking. Stop and say what is missing.

## Managed fleets

This skill is the *loop*. Machine-specific runtime — which driver binary to start, how it is
registered as an MCP server, where the credential comes from, which machine map to resolve
paths against, and which older skills are retired — belongs to the fleet, not to this public
repo. On a managed fleet, read the fleet's `shared/rules/jev-computer-use-fleet.md` (Hermes:
`~/.hermes/shared/rules/`) before the first GUI action, and resolve `$HOME`-relative paths
against that fleet's machine map.

## Retired schema — do not reuse it

An earlier preview of this loop used `hermes.cua_jev_choice_request_v1`: `capture_id`, pixel
`bounds` and a per-region `confidence`, pinned to model `jev-1.13.0`. It is withdrawn and
incompatible with the request above. Regions here carry `id`, `role`, `label`, `interactive`
and no coordinates; the model is `jev-latest`. A script or skill that still sends the old
shape must be updated, not renamed. If something hands you the old schema, stop and report it.
