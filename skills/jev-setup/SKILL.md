---
name: jev-setup
description: Use when Jev is not working yet, a Jev tool reports no_key or auth_failed, or the person asks to connect or fix Jev. Gets the TypeSafe API key into the secret store without the agent ever seeing it.
version: 0.1.0
license: MIT
metadata:
  hermes:
    tags: [jev, typesafe, setup, credentials]
---

# Connect Jev (the key never passes through you)

Jev is a structured decision model, not a chat model. It receives a state (string, object, or array) plus typed questions and returns calibrated answers: a yes/no probability (`noul`), a choice from options defined by the caller (`choice`), or a position on an ordered rubric (`score`). The surrounding code owns the workflow and acts on those answers. Use Jev for routing, ranking, verification and other structured decisions—not for generating text or holding a conversation.

It can run through TypeSafe directly or through OpenRouter. The selected provider is determined from the configured key: `TYPESAFE_API_KEY` first, then `OPENROUTER_API_KEY`. You must never see, ask for, or handle either key.
## Rules

- Never ask the person to paste the key into the chat. If they paste one anyway, do not store it, do not repeat it, tell them that key should be replaced, and start the flow below.
- Never read the secret store, `.env` files or `~/.config/jev/credentials` to "check" the key. Use `jev doctor`, which reports only presence and length.
- Never put the key in a command line, a URL, a config file you write, or a log.

## Flow

1. Check the state: `jev doctor`. If `key.present` is true and `jev.reachable` is true, you are done.
2. Connect the key privately:

   ```bash
   jev setup-key                 # TypeSafe (default)
   jev setup-key --provider openrouter
   ```

   The command asks in a hidden terminal prompt. The key is never printed, placed in a URL, or sent through a browser. If they have no key yet, TypeSafe keys come from https://console.typesafe.ai/settings/keys and OpenRouter keys from https://openrouter.ai/keys.
3. Wait for `{"status": "stored", "verified": true, ...}`. `rejected` means the key was wrong: run it again.
4. Run `jev doctor` once more and report the result in a sentence.

## Where the key goes

The OS secret store (macOS Keychain service `Hermes TypeSafe API`, or `secret-tool` on Linux), falling back to `~/.config/jev/credentials` (mode 0600). On a Hermes machine it is also written as `TYPESAFE_API_KEY` into `~/.hermes/.env` and every `profiles/*/.env`, because each Hermes lane reads its own file. Running gateways pick it up on their next restart; do not restart one without being asked.
