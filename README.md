# Hermes Jev Skills

Give your agent a fast, cheap second brain for the small decisions.

[Jev](https://docs.typesafe.ai) is TypeSafe's decision model. It does not write text. You hand it a state and typed questions (pick one, score this, yes or no) and it answers in about 0.4 seconds for a tiny fraction of a cent, with a calibrated confidence. This repo puts that to work on the decisions an agent makes all day, so your expensive model only does the thinking and writing.

| Skill | What Jev decides | Measured |
|---|---|---|
| **Model routing** | Which model is good enough for this turn, from every model you can call | ~0.4 s per turn |
| **Memory** | Which retrieved passages are worth reading, and which contain hidden instructions | one request for up to 60 passages |
| **Compaction and handoffs** | Which turns survive word for word, which get summarized, which are dropped | 71 turns in 0.95 s |
| **Skill selection** | Which installed skill this turn needs, or none | 377 skills in ~2.8 s; acknowledgements answered locally for free |
| **Triage** | How urgent a message is, what kind it is, and whether a person must see it | ~0.4 s per message, $0.00006 |
| **Computer use** | The next GUI action, from a table of actions you already judged safe | ~0.4 s per step |
| **Browser use** | The next page action, same contract | ~0.4 s per step |

Plus a **model routing dashboard** (`jev dashboard`): every profile's models on one page, an on/shadow/off switch for Jev routing, and a live view of where each turn is being sent. See [router-dashboard](router-dashboard/README.md).

Built for [Hermes](https://github.com/NousResearch/hermes-agent). The skills and the `jev` command also work in Claude Code, Codex and anything else that reads `SKILL.md` files.

## Install

**Point your agent at this repo** and say: *"Install Hermes Jev Skills from https://github.com/kerpopule/hermes-jev-skills"*. It will follow [AGENTS.md](AGENTS.md).

**Got it as a zip?** Unzip it anywhere, then run the second and third commands below from that folder.

Or by hand (Python 3.9+, no dependencies):

```bash
git clone https://github.com/kerpopule/hermes-jev-skills ~/hermes-jev-skills
```

```bash
python3 ~/hermes-jev-skills/install.py
```

```bash
jev setup-key
```

The installer finds Hermes, Claude Code and Codex on the machine and installs for each one it finds. `python3 install.py --check` shows what it would do without changing anything; `--uninstall` reverses it.

## Your API key never touches the agent

`jev setup-key` opens a one-time page served only by your own computer. You paste your [TypeSafe key](https://console.typesafe.ai/settings/keys) there. It goes straight into the OS secret store (macOS Keychain, or `secret-tool` on Linux, or a 0600 file as a last resort) and, on a Hermes machine, into each profile's `.env`. The agent that ran the command sees one line: stored, verified, yes or no. Never the key, not even a prefix.

The page lives on an unguessable one-time URL, refuses requests with a foreign `Host` header (DNS rebinding), sends no referrer, logs nothing, and shuts down after one use or ten minutes. On a headless box, run `jev setup-key --tty` yourself for a hidden prompt.

**Do not paste your key into a chat.** If you already did, make a new one.

## On Hermes

The `hermes-jev` plugin uses only public plugin seams (`pre_llm_call`, `llm_request` middleware, tools, a slash command), so `hermes update` does not break it and nothing in Hermes core is patched.

```
/jev                         status
/jev routing shadow          decide and log, do not switch (start here)
/jev routing on              switch models per turn
/jev skills on               suggest the right skill per turn
/jev notice on               show "[Jev] hard · coding → kimi-k3 · confidence 0.92" on routed replies
/jev routing on all          make it the default for every profile (a profile's own setting still wins)
```

Tools the agent gets: `jev_memory_filter`, `jev_compact_select`, `jev_choose_action`.

A plugin can swap the model, not the provider connection. On OpenRouter that still covers every vendor. If you run `/model` yourself, your choice wins.

### Every model you have

```bash
jev models list              # everything this machine can call: price, context, vision, reasoning
jev models providers         # which providers you hold a key or login for
jev models suggest --write   # first-draft pools from price bands; then edit to taste
```

The catalog is [models.dev](https://models.dev), filtered to providers whose API-key name is set in your environment or Hermes `.env`, or that Hermes holds a login for. Only key *names* are read. Pools live in `~/.hermes/jev/routing.json` (the default for every profile); `~/.hermes/profiles/<name>/jev/routing.json` overrides it for one profile. See [skills/jev-model-routing](skills/jev-model-routing/SKILL.md).

## What leaves your machine

Jev is a cloud API, so this is spelled out:

- **Routing**: the user's turn, redacted (emails, phones, tokens, long hex masked), capped at 3,000 characters. Never history, tool results, files or memory. Turns that look like they hold a secret, and any profile you list in `private_profiles`, send only coarse features: length, whether code is present, whether risk words appear.
- **Memory**: the query and up to 900 characters per passage, redacted. Your store's ids, paths and sources are replaced with `P0`, `P1`… and never sent. A passage that looks like a credential is not sent at all.
- **Compaction**: up to 700 characters per turn, redacted. Turns that look sensitive are skipped.
- **Skills**: the turn, redacted, plus skill names and descriptions.
- **Computer and browser use**: the goal, short element labels, and your action descriptions. Never screenshots, page text or field values. A goal or label that looks sensitive is refused before sending.

Logs hold decisions only (tier, model, confidence, latency). Never prompt text.

## Everything fails open

No key, timeout, rate limit, malformed reply, low confidence: routing keeps your current model, memory returns the original list, compaction drops nothing, skill selection suggests nothing, and computer use returns `reobserve`. A Jev outage costs you at most the time budget (2.5 s for routing) and never blocks a turn.

Safety rails that do not depend on Jev being right: risk words (production, delete, migration, security, payment, legal…) never route to the cheapest tier; a large context never switches to a cheaper model mid-session; a transcript turn is only dropped on a confident answer; Jev can only ever return an action id you put in the table.

## Layout

```
jevkit/          the library and the `jev` command (stdlib only)
skills/          eight SKILL.md skills, agent-agnostic
hermes/plugin/   the Hermes plugin
router-dashboard/  the model routing page (`jev dashboard`)
install.py       installer / uninstaller
tests/           offline tests, every Jev reply faked
docs/            integration notes and hard-won operational lessons
```

| Doc | Read it when |
|---|---|
| [turning-a-jev-feature-on.md](docs/turning-a-jev-feature-on.md) | **Before you enable anything.** Shadow mode, silent defaults, why a quiet log proves nothing, and bounding by the clock rather than the count. |
| [measuring-a-router.md](docs/measuring-a-router.md) | Replaying routing against your own traffic before you trust the savings. |
| [wiring-triage-into-a-live-pipeline.md](docs/wiring-triage-into-a-live-pipeline.md) | Adding classification to something already carrying real traffic. |
| [hermes-compaction.md](docs/hermes-compaction.md) | Compaction and handoff on Hermes specifically. |

```bash
python3 -m unittest discover -s tests
```

## License

MIT. Jev and TypeSafe are products of TypeSafe AI; this project is independent. Optional browser runner wraps [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) (MIT), which is not bundled.
