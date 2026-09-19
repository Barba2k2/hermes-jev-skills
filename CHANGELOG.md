# Changelog

## 0.9.1 (2026-09-19)

An audit of the day's six commits, and the follow-up fixes none of them logged.

- **`jev escalate` does not exist.** `jev-frontier-work` told agents to run it at three
  separate places; the command was renamed to `jev ladder` and the skill was never
  updated. In a repo whose premise is "an agent reads a SKILL.md and acts", an agent that
  loaded that skill errored three times with no way to discover the real name. Fixed, and
  a test now asserts every `jev <subcommand>` in every skill is a real subcommand — proven
  by reintroducing the bug and watching it fail.
- **A privacy fix that traded one silent failure for another.** 0.9.0 stopped the phone
  rule eating UPS tracking numbers by widening its lookbehind to exclude letters. That
  stopped `x8505550134`, `ext8505550134` and `Phone8505550134` being redacted at all —
  data loss swapped for a leak. The right fix protects the specific thing instead of
  blunting the general rule: tracking numbers are held aside, the original phone rule runs
  untouched, and they are restored before truncation. Both directions are tested now.
- **The release guard passed without scanning anything.** Outside a git checkout
  `check_release.py` printed `clean: 0 files` and exited 0. The repo is distributed as a
  zip, so that case is real. A guard that cannot tell "clean" from "did not run" is worse
  than no guard; it now exits non-zero and says so.
- **Version drift.** Code said 0.8.0 while the changelog announced 0.9.0, so
  `build_release.sh` would have shipped 0.9.0 code in an 0.8.0 zip.
- **[`docs/turning-a-jev-feature-on.md`](docs/turning-a-jev-feature-on.md)** — the
  operational rules behind all of the above: shadow first, benchmark your benchmark, a
  config key that can default to "off" will, absence of errors proves nothing, prove it
  where it runs, latency is the cost that lands on every turn, and ship the implementation
  rather than only the loop.

## 0.9.0 (2026-09-19)

Triage went into a live pipeline, and a continuity rule turned out to forbid what handoff was doing.

- **Confidential handoffs.** Some deployments are bound by a rule that continuity may carry *only* task, sources checked, missing evidence, owner and next action — never customer detail. The default capsule breaks that rule by design: it is told to keep identifiers verbatim. `handoff_prompt(..., confidential=True)` replaces that instruction with a breadcrumb contract, and `compact.redact_capsule` is a mechanical second pass over the result. Both, because neither is enough: the regex is reliable but cannot know a surname is a customer, and the prompt can see but can be disobeyed.
- **Confidentiality cannot switch itself off quietly.** A host may expose no plugin-config API at all, and asking one that does not returns the default — which here means writing customer data against a rule forbidding it. A `CONFIDENTIAL` marker file in the handoff directory is the authority: one `ls` to verify, impossible to swallow in an exception handler. (This is the third silent-default failure in this project. The pattern is the lesson.)
- **Under confidentiality, a failed writer writes nothing.** The normal fallback stores the raw transcript, because losing the thread is worse than a fat capsule. Under a confidentiality contract that fallback is the single worst outcome, so the capsule says "ask the person what they were working on" instead. An older jevkit that cannot honour the mode refuses rather than silently downgrading.
- **[`docs/wiring-triage-into-a-live-pipeline.md`](docs/wiring-triage-into-a-live-pipeline.md)** and [`scripts/triage_adapter.py`](scripts/triage_adapter.py) — the four rules that made it safe to edit something already carrying real traffic: fail open or don't ship; shadow before it steers; respect the emit contract you found; bound the work and prove it *in the scheduler*, not in your shell.
- **A count cap is not a time cap.** The first wiring capped triage at 40 messages with a 6s timeout — 240s worst case, inside a router the wrapper kills at 180s. A kill mid-loop loses every message already marked seen, because dedupe is written before routing. `Budget` bounds messages *and* wall-clock, and reports what it skipped rather than truncating silently.

## 0.8.0 (2026-09-19)

- **`jev triage`** — classify a message the moment it lands: act **now**, **today**, **queue**, or **ignore**. One Jev request per message (~400 ms, $0.00006) answers urgency, kind, whether a person must decide, and whether the sender is blocked. Cheap enough to run on every message, which is the point — triage that only runs when someone remembers to look is not triage.
- Code makes the routing call, not the model: Jev supplies calibrated readings and the thresholds are ours, in one readable function. Urgency is read from the probability mass at the top of the rubric, never the averaged score.
- Fails toward attention: Jev down routes to `today`, an unsure answer never lands in `ignore`, and a message that looks like it carries a credential is **never sent** and goes straight to a person.
- Tuned on 20 real support emails. Two rules earned their place there: a known customer reporting a problem they cannot work around is escalated even when they phrase it calmly ("can't do anything with these" scored mid-rubric and sat in `today`), but only when the message also reaches "this week" urgency — escalating low-urgency grumbles trains everyone to ignore the `now` pile.

## 0.7.1 (2026-09-19)

Two bugs found by testing a live deployment, both of which fail silently — the feature simply appears not to work.

- **The lane key did not survive the hop it exists for.** The hook that writes a capsule receives `chat_id`; the hook that injects it receives `platform` and `sender_id` and *not* `chat_id`. Every conversation therefore resolved to the same key on the reading side, and no capsule would ever have matched. `lane_from_session()` now resolves the conversation from the session store, which both sides can reach, and the nightly script calls the plugin's own `lane_key` instead of carrying a second copy of the rule.
- **Truncated lane keys could collide.** Real Teams conversation ids are 131 characters and share a structural prefix, so a 120-character truncation made uniqueness a matter of luck — and a collision hands one customer's capsule to another. Long keys now keep a readable prefix plus a hash of the full key. Containment inside the handoff directory is asserted against hostile ids rather than inferred.

## 0.7.0 (2026-09-19)

- **`hermes/scripts/nightly-handoff.py`** — close every live conversation once a night and leave tomorrow a capsule. Written for a deployment where one conversation had reached 2,255 messages and seventeen days, re-sent in full on every turn. Conservative by construction: only sessions with recent activity and real content, capped per profile, `--dry-run` touches nothing, and **a session is only closed after its capsule is safely written** — losing the thread is worse than a large context.
- Resolves the Hermes CLI from the installation root even when `HERMES_HOME` points at a profile, which is how per-profile state is addressed. `HERMES_CLI` overrides.

## 0.6.1 (2026-09-19)

- **Writer response shape** — `call_llm` returns a `ChatCompletion` object on some hosts, not a dict. `(response or {}).get("choices")` raised `AttributeError`, which `build()` caught, so every capsule silently took the transcript fallback and looked like a bad summary rather than a broken one. `handoff.extract_text()` now handles a string, an OpenAI-shaped dict, an SDK object, and content-part lists. Caught on a live deployment, not in review.

## 0.6.0 (2026-09-19)

- **`hermes-handoff` plugin** — say `handoff` (or `/handoff`) and the session closes deliberately: Jev marks which turns must survive word for word, the host's existing auxiliary model writes a five-section capsule from that digest, and the next session's first turn receives it as context, once. An agent that never starts fresh drags every past turn into every future one; one that starts fresh with nothing repeats settled work. This is the third option.
- Degrades rather than fails at every step: no Jev key means every turn is background and the capsule is still written; a writer that refuses, times out or answers something else falls back to the filtered transcript, which reads worse but loses nothing; a writer that raises never takes the session down.
- `jevkit.compact` gained `handoff_prompt()` and `looks_like_capsule()`. Jev still cannot write — it only decides what is worth writing about.

## 0.5.0 (2026-09-19)

- **`jev spend`** — the weekly cost report. What ran, what it cost, and what the same tokens would have cost on every alternative. Two things it exists to fix: a flat-fee seat looks free at the margin and so vanishes from cost reports (it is valued here at what its work would have cost metered, and told to earn its keep or be cancelled), and a per-token price is not a per-task price. The effective $/M column also exposes prompt caching, which the headline price hides.
- Counterfactuals are honest about their limit: they price the tokens that were actually produced, so a model that reasons more or less would not have produced the same ones. Stated in the output, not just the docs.

## 0.4.0 (2026-09-19)

Frontier work: pick the seat, then watch the run.

- **`jevkit/ladder.py` + `jev ladder`** — an escalation ladder for hard work across paid frontier seats. A refusal is written to shared state, so one lane hitting a quota teaches all the others instead of forty agents rediscovering the same 429. A rung is skipped, never silently downgraded: when everything is full the decision says `forced` out loud rather than quietly serving hard work from a cheap model. Only the `hard` tier reaches it.
- **`jevkit/supervise.py` + `jev supervise`** — Jev watches delegated frontier runs. Code decides what is free to decide (has output arrived, is it repeating, has the process exited); Jev judges only what code cannot (is this meaningful progress, is it waiting on an answer, has it given up, is it finished); the expensive supervisor is woken only when one of those crosses a threshold. A Jev failure means keep waiting, never abort.
- **Scheduled turns are now routed, not skipped.** A cron turn is a ~37,000-character standing contract wrapped around a `## Prompt` of ~660 characters — the instruction is 1% of the envelope, which is why judging the envelope escalated everything. `unwrap()` pulls out the ask. Routing cron turns *without* unwrapping costs +115%; with it, +6%, and genuinely demanding jobs still reach the hard tier. Recurring jobs repeat their instruction verbatim, so decisions cache: 247 cron runs held 5 distinct asks.
- **Privacy gate fix**: `AWS_SECRET_ACCESS_KEY`, `DB_PASSWORD`, `GITHUB_TOKEN` and other env-var-style secrets were not caught, because the pattern only matched `secret_key` — the revealing word sits in the middle of the name. Found by a supervisor test; it affected every module.

## 0.3.0 (2026-09-19)

- **`jev replay`**: offline evaluation. Replays logged turns through a policy and prices it against the baseline those turns actually ran on, so "is this router worth it" is arithmetic instead of an opinion. Only Jev is called; a few hundred turns costs cents.
- Costs the **whole tool loop**, not one call. A median agent turn here is 8 API calls and ~192k input / 5.6k output tokens — **97% input**. Ranking models by a blended price misranks them for agent work; rank by that real mix.
- `jevkit.replay.compare` A/Bs several configs over the same turns. See [docs/measuring-a-router.md](docs/measuring-a-router.md).

## 0.2.5 (2026-09-19)

Routing policy `route-2`. In shadow mode on a real 41-profile fleet, 89% of judged turns were sent to the hard tier. None of the three causes was the turns being hard:

- **An unsure Score averages to the middle of the rubric**, which sat on the hard cutoff. The router now reads the per-level probabilities Jev returns: hard needs P(substantial or expert) of 0.6, simple needs P(trivial) of 0.7. An unsure answer never buys the hard tier: a harmless unsure turn keeps its model, a risky one gets medium.
- **Risk words set a floor of medium and no more.** They used to escalate to hard.
- **Jev judges the ask, not the boilerplate.** A long turn is read as its opening plus, mostly, its end (`ask_chars`, 2500), and the risk-word check runs on that same slice.
- **Template turns are not routed**: `skip_prefixes` (`[kanban]`, `[SESSION HANDOFF`, …) and `skip_session_prefixes` (`cron`). They wrap work Jev cannot see, so they keep the model their profile or job was configured with.

Replayed on 400 real turns: 316 template turns untouched, 48 unsure turns kept, 36 judged as 8 simple / 10 medium / 18 hard. The hard tier went from 89% of turns to 4.5%.

## 0.2.4 (2026-09-19)

- The routing middleware no longer double-prefixes an already-prefixed model id (`openrouter:openrouter:…`), and the "you pinned this model" check now compares bare model ids on both sides. A prefixed model id used to look pinned-or-not by accident; the check is now format-independent.
- New `tests/test_plugin_middleware.py`: routes/prefixed/pinned/off/stale-turn cases against the real middleware with the Jev call stubbed (no network, no real log).

## 0.2.3 (2026-09-19)

- **`jev-browser-use` path B actually runs now.** The runner required a CDP browser to already exist (`BU_CDP_WS` or a Chrome with remote debugging on) and simply failed on a machine without one. It now launches its own headless Chrome on a throwaway profile when no endpoint is given, closes it on exit, SIGINT and SIGTERM, and reports `browser: owned|attached`. `--no-launch-chrome`, `--chrome-path` and `BH_CHROME_PATH` control it. The person's everyday browser is never attached to.
- Fixed the launch order: the browser is started *after* the vendored-venv re-exec. Starting it before meant the exec replaced the process and orphaned the browser and its throwaway profile.
- New `tests/test_browser_runner.py`: chrome discovery, launch flags, CDP polling, the cleanup contract, the launch-order regression and the allowlist.

## 0.2.2 (2026-09-19)

- **Memory filter safety fix.** A passage the privacy gate refuses to send is never injection-checked, but it was still returned in `selected_ids` with no warning — so text shaped like an injection could reach the agent as though it had been judged. Withheld passages are now screened locally (no network) for instruction shapes; matches are dropped into `dropped_injection_ids` and listed in the new `local_screen_ids`. Harmless withheld passages are still kept, so no memory is silently lost.
- `jev_memory_filter`'s tool description and `jev-memory` now say that `unjudged_ids` is *unchecked*, not verified.

## 0.2.1 (2026-09-19)

- `jev-computer-use` and `jev-browser-use` now carry a **Managed fleets** section: the driver command, credential source, vendor checkout and machine map belong to the fleet, not this repo, and the fleet note they point at is authoritative for them.
- `jev-computer-use` documents the withdrawn preview schema `hermes.cua_jev_choice_request_v1` (`capture_id`, pixel `bounds`, per-region `confidence`, model `jev-1.13.0`) as incompatible with `jev.action_choice_request_v1`. Scripts must be updated, not renamed.
- `jev-browser-use` states that a fleet may make Jev Ultrafast the required default, with the own-browser-tool path reserved for Ultrafast's documented gaps, and that Jev is never bypassed in either path.

## 0.2.0 (2026-09-18)

- The model routing dashboard ships in the repo (`router-dashboard/`, `jev dashboard`): per-profile models, an All-profiles target with confirmation, an Off / Shadow / On switch for Jev routing, and a live view of decisions.
- `scripts/build_release.sh` builds the shareable zip from the committed tree.

## 0.1.1 (2026-09-18)

- Plugin manifest: `config_schema` in the flat shape Hermes expects (it logged a warning and skipped the old one).
- Key page: no reverse-DNS lookup on bind (stalled for seconds on some Macs).
- Shared `routing.json` / `state.json` in the Hermes root are the default for every profile; `/jev <switch> <value> all`.

## 0.1.0 (2026-09-18)

First release.

- `jevkit`: strict Jev client, key store, private key-entry page, privacy gate, model catalog, router, memory filter, compaction selector, two-stage skill picker, bounded action chooser, `jev` command.
- Hermes plugin `hermes-jev`: per-turn model routing through `llm_request` middleware, per-turn skill suggestion through `pre_llm_call`, three tools, `/jev` with per-profile and all-profile switches, decision log.
- Seven agent-agnostic skills.
- Installer for Hermes, Claude Code and Codex, with `--check` and `--uninstall`.
