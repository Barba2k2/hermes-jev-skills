"""Pick the cheapest model that is good enough for this turn.

Jev answers three things in one request: how hard the turn is, what kind of work
it is, and whether a mistake would be costly. Code, not Jev, turns that into a
model: it walks the pool for that tier and specialty and takes the first model
that fits the turn's hard requirements (images, context size). Every unsure or
failed path keeps the model you were already on. Routing never blocks a turn.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import catalog as catalog_mod
from . import client, ladder, privacy

TIERS = ("simple", "medium", "hard")
SPECIALTIES = ("general", "coding", "writing", "research", "vision")
POLICY_VERSION = "route-2"

DIFFICULTY = [
    "Trivial or mechanical: a lookup, reformat, rename, short factual reply, or a single obvious step",
    "Routine: ordinary multi-step work with a clear path and low ambiguity",
    "Substantial: needs planning, several interacting parts, debugging, or careful judgment",
    "Expert: subtle, ambiguous, or high-stakes; architecture, security, concurrency, data migration, legal or money",
]
KIND = {
    "coding": "Writing, changing, debugging or reviewing software, scripts, configs or shell commands",
    "writing": "Drafting or editing prose, marketing, messages, documents or creative text",
    "research": "Finding, comparing or synthesizing information, analysis, or current events",
    "general": "Conversation, planning, operations, or anything that is none of the others",
}

# Short prompts can still be dangerous. These never route to the cheapest tier.
_HARD_RISK = re.compile(
    r"(?i)\b(prod(uction)?|deploy|migrat\w+|rollback|drop\s+table|delete|rm\s+-rf|force[- ]push|secur\w+|"
    r"vulnerab\w+|auth(entication|orization)?|encrypt\w*|concurren\w+|race condition|deadlock|payment|refund|"
    r"invoice|stripe|billing|legal|contract|lawsuit|medical|diagnos\w+|customer data|pii|dns|certificate)\b"
)

DEFAULT_CONFIG: Dict[str, Any] = {
    "mode": "redacted-text",          # or "features": no prompt text ever leaves the machine
    "min_confidence": 0.6,
    "simple_needs_confidence": 0.85,
    "hard_needs_probability": 0.6,    # P(substantial or expert) needed before paying for the hard tier
    "simple_needs_probability": 0.7,  # P(trivial) needed before dropping to the cheapest tier
    "ask_chars": 2500,                # how much of a long turn Jev reads: the opening and, mostly, the end
    # A scheduled or queued turn is a standing contract wrapped around one real instruction. Measured on this
    # fleet, that instruction is ~1% of the envelope. Name the sections that hold it and the ones that are
    # previous output, and the job gets judged on what it actually asks for.
    "ask_sections": ["Prompt", "Task", "Request", "Instruction", "Objective", "Goal"],
    "drop_sections": ["Your previous run's output", "Script Output", "Output from job",
                      "Previous output", "Prior run", "Last run"],
    # Recurring jobs repeat their instruction verbatim, so the same decision is reused instead of re-bought.
    "cache_repeat_asks": True,
    "cache_size": 512,
    "sticky_context_tokens": 32000,   # above this, do not downgrade: the cache rebuild costs more than it saves
    # Hard work can be handed to a frontier seat instead of an OpenRouter model. The plugin can
    # swap a model but not a provider connection, so this is a DELEGATION signal for the agent,
    # never a silent switch — see jevkit/ladder.py.
    "escalation": {"enabled": False, "rungs": []},
    "exclude": ["*:free", "cloudflare-ai-gateway:*"],
    "private_profiles": [],
    "tiers": {},                      # {"simple": {"general": ["provider:model", ...], "coding": [...]}, ...}
}


def config_paths() -> List[Path]:
    """Least specific first. The shared file is the default for every Hermes profile; a profile's own file overrides it."""
    override = os.environ.get("JEV_ROUTING_CONFIG")
    if override:
        return [Path(override)]
    paths = [Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "jev" / "routing.json"]
    if catalog_mod.hermes_root().is_dir():
        paths.append(catalog_mod.hermes_root() / "jev" / "routing.json")
        if catalog_mod.hermes_home() != catalog_mod.hermes_root():
            paths.append(catalog_mod.hermes_home() / "jev" / "routing.json")
    return paths


def config_path() -> Path:
    """Where `jev models suggest --write` saves: the most specific location that applies."""
    return config_paths()[-1]


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    for candidate in ([path] if path else config_paths()):
        try:
            layer = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        tiers = {**config.get("tiers", {}), **(layer.get("tiers") or {})}   # a profile may override one tier only
        config.update(layer)
        config["tiers"] = tiers
    return config


# ── pools ────────────────────────────────────────────────────────────────────

def _ref(row: Dict[str, Any]) -> str:
    return f"{row['provider']}:{row['model']}"


def _excluded(ref: str, patterns: List[str]) -> bool:
    return any(fnmatch.fnmatch(ref, pattern) or fnmatch.fnmatch(ref.split(":", 1)[1], pattern) for pattern in patterns)


# A model that names its own specialty is telling you something the price band cannot.
# Only used to ORDER a pool, never to exclude: a model with no hint still appears, just
# after the ones that advertise the skill.
_SPECIALTY_HINTS = {
    "coding": ("code", "coder", "codestral", "devstral", "starcoder", "qwen2.5-coder"),
    "writing": ("writer", "creative", "prose"),
    "research": ("search", "sonar", "research", "deep-research", "grok"),
}


def suggest_tiers(rows: List[Dict[str, Any]], exclude: List[str], per_pool: int = 6) -> Dict[str, Dict[str, List[str]]]:
    """A starting point from price bands. Pin your own picks in routing.json.

    Generates a pool for every specialty, not only general and vision. An earlier version
    emitted just those two, which quietly killed the whole specialization axis: `route`
    still asked Jev "what kind of work is this?" on every turn, `_pick` still looked for a
    `coding` pool, found none, and fell through to `general`. The question cost money on
    every turn and could not change any answer. A generator that cannot produce a pool is
    the same as deleting the feature, so it produces all of them.
    """
    bands = {"simple": (0.0, 0.6), "medium": (0.6, 3.2), "hard": (3.2, 1e9)}
    usable = [r for r in rows if not _excluded(_ref(r), exclude) and not r["model"].startswith("~") and r["price"] > 0]
    out: Dict[str, Dict[str, List[str]]] = {}
    for tier, (low, high) in bands.items():
        pool = sorted((r for r in usable if low <= r["price"] < high), key=lambda r: r["released"], reverse=True)
        tier_pools: Dict[str, List[str]] = {
            "general": [_ref(r) for r in pool[:per_pool]],
            "vision": [_ref(r) for r in pool if r["vision"]][:per_pool],
        }
        for specialty, hints in _SPECIALTY_HINTS.items():
            named = [r for r in pool if any(h in r["model"].lower() for h in hints)]
            rest = [r for r in pool if r not in named]
            tier_pools[specialty] = [_ref(r) for r in (named + rest)[:per_pool]]
        out[tier] = tier_pools
    return out


def dead_axis(config: Dict[str, Any]) -> List[str]:
    """Tiers where Jev is asked for a specialty that cannot change the answer.

    `route` pays for a Choice over SPECIALTIES on every turn. If a tier only has `general`
    and `vision` pools then every specialty answer resolves to the same model, and that
    request is pure cost. Worth saying out loud rather than leaving someone to notice that
    a routing decision never varies.
    """
    specialist = [s for s in SPECIALTIES if s not in ("general", "vision")]
    blind: List[str] = []
    for tier, pools in (config.get("tiers") or {}).items():
        if isinstance(pools, dict) and not any(pools.get(s) for s in specialist):
            blind.append(tier)
    return blind


def _pick(config: Dict[str, Any], rows: Dict[str, Dict[str, Any]], tier: str, specialty: str,
          need_vision: bool, context_tokens: int, only_provider: Optional[str] = None) -> Optional[str]:
    tiers = config.get("tiers") or {}
    order = [tier] + [t for t in TIERS[TIERS.index(tier):] if t != tier]  # never fall DOWN a tier
    for candidate_tier in order:
        pools = tiers.get(candidate_tier) or {}
        for name in (("vision",) if need_vision else ()) + (specialty, "general"):
            for ref in pools.get(name) or []:
                if _excluded(ref, config.get("exclude") or []):
                    continue
                if only_provider and ref.split(":", 1)[0] != only_provider:
                    continue          # a plugin can swap the model, not the provider it is already connected to
                row = rows.get(ref)
                if row is None:          # pinned by the user but unknown to the catalog: trust the pin
                    if not need_vision:
                        return ref
                    continue
                if need_vision and not row["vision"]:
                    continue
                if row["context"] and context_tokens * 1.25 > row["context"]:
                    continue
                return ref
    return None


# ── envelopes ────────────────────────────────────────────────────────────────

_H2 = re.compile(r"(?m)^[ \t]*#{2,3}[ \t]+(\S[^\n]*?)[ \t]*$")


def unwrap(text: str, config: Optional[Dict[str, Any]] = None) -> str:
    """Return the instruction inside a scheduled/queued envelope, or the text unchanged.

    A cron or worker turn is mostly a standing contract that is identical every run: the
    anti-stall protocol, the status format, the previous run's output. Judging that says
    nothing about this run. If a section names the actual ask, use only that section.
    """
    config = config or DEFAULT_CONFIG
    wanted = [w.lower() for w in (config.get("ask_sections") or [])]
    unwanted = [w.lower() for w in (config.get("drop_sections") or [])]
    if not wanted or "#" not in text:
        return text
    heads = list(_H2.finditer(text))
    if not heads:
        return text
    for index, match in enumerate(heads):
        title = match.group(1).strip().lower().rstrip(":")
        if not any(title == w or title.startswith(w + " ") for w in wanted):
            continue
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        body = text[match.end():end].strip()
        if len(body) >= 24:                      # a heading with nothing under it is not the ask
            return body
    # No ask section, but previous output can still be dropped so the rest is judged on its merits.
    if unwanted:
        keep, cut = [], 0
        for index, match in enumerate(heads):
            title = match.group(1).strip().lower().rstrip(":")
            if any(title.startswith(w) for w in unwanted):
                end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
                keep.append(text[cut:match.start()])
                cut = end
        if keep:
            keep.append(text[cut:])
            trimmed = "".join(keep).strip()
            if len(trimmed) >= 24:
                return trimmed
    return text


# ── decision ─────────────────────────────────────────────────────────────────

def _features(prompt: str, context_tokens: int) -> Dict[str, Any]:
    words = len(prompt.split())
    return {
        "length": "short" if words < 25 else "medium" if words < 150 else "long",
        "questions": min(prompt.count("?"), 5), "has_code": bool(re.search(r"```|\bdef |\bclass |;\s*$|\{\s*$", prompt, re.M)),
        "numbered_steps": len(re.findall(r"(?m)^\s*(\d+[.)]|[-*])\s", prompt)),
        "risk_words": bool(_HARD_RISK.search(prompt)),
        "context": "small" if context_tokens < 8000 else "medium" if context_tokens < 64000 else "large",
    }


# A repeating job asks the same thing every run; its answer is worth exactly one Jev call.
_DECISIONS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()


def _cache_key(ask: str, profile: Optional[str], only_provider: Optional[str], has_images: bool, pinned: bool) -> str:
    material = "\x00".join([ask, str(profile), str(only_provider), str(has_images), str(pinned), POLICY_VERSION])
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()


def _remember(key: Optional[str], decision: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Cache only decisions that will still be right next time: never a transient failure."""
    if key and decision.get("tier"):
        _DECISIONS[key] = decision
        _DECISIONS.move_to_end(key)
        while len(_DECISIONS) > int(config.get("cache_size", 512)):
            _DECISIONS.popitem(last=False)
    return decision


def _keep(current: Optional[str], reason: str, **extra: Any) -> Dict[str, Any]:
    out = {"routed": False, "model": current, "reason": reason, "policy": POLICY_VERSION,
           "notice": f"[Jev] kept {current or 'current model'} · {reason}"}
    out.update(extra)
    return out


def decide(
    prompt: str, *, current: Optional[str] = None, context_tokens: int = 0, has_images: bool = False,
    profile: Optional[str] = None, pinned: bool = False, config: Optional[Dict[str, Any]] = None,
    rows: Optional[List[Dict[str, Any]]] = None, transport: Optional[client.Transport] = None,
    timeout: float = 2.5, only_provider: Optional[str] = None, session_id: str = "",
) -> Dict[str, Any]:
    """Route one fresh user turn. Call it once per turn, never inside a tool loop."""
    config = config or load_config()
    if pinned:
        return _keep(current, "you pinned this model")
    if not (config.get("tiers") or {}):
        return _keep(current, "no tiers configured; run `jev models suggest --write`")
    if not prompt.strip():
        return _keep(current, "empty turn")

    # Judge the ask, not the contract around it. A scheduled or queued turn is a standing brief wrapped
    # around one real instruction; unwrap to that first. Whatever is left, a long turn keeps its opening
    # and — far more often where the actual request lives — its end.
    limit = int(config.get("ask_chars", 2500))
    inner = unwrap(prompt, config)
    unwrapped = inner is not prompt and inner != prompt
    ask = inner if len(inner) <= limit else inner[:limit // 4] + "\n[…]\n" + inner[-(limit - limit // 4):]

    # A recurring job repeats its instruction verbatim, so buy the decision once and reuse it.
    cache_key = None
    if config.get("cache_repeat_asks", True):
        cache_key = _cache_key(ask, profile, only_provider, has_images, bool(pinned))
        cached = _DECISIONS.get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}
    risky = bool(_HARD_RISK.search(privacy.normalize(ask)))
    private = profile in (config.get("private_profiles") or []) or privacy.is_sensitive(ask)
    mode = "features" if private else config.get("mode", "redacted-text")
    state: Any = (
        {"turn_features": _features(ask, context_tokens)} if mode == "features"
        else {"user_turn": privacy.redact(ask, limit + 50), "context": _features(ask, context_tokens)["context"]}
    )
    questions = {
        "difficulty": client.score("How demanding is it to complete this turn well?", DIFFICULTY),
        "kind": client.choice("What kind of work is this turn mainly?", KIND),
        "costly_mistake": client.noul("A wrong or sloppy answer here would be costly or hard to undo"),
    }
    try:
        reply = client.ask(state, questions, timeout=timeout, transport=transport)
    except client.JevError as error:
        return _keep(current, f"Jev unavailable ({error.code})", private=private)

    answers = reply["answers"]
    difficulty, confidence = answers["difficulty"]["score"], answers["difficulty"]["confidence"]
    stakes = answers["costly_mistake"]["noul"]
    spread = answers["difficulty"].get("probabilities") or {}
    if spread:
        p_simple, p_hard = spread.get(0, 0.0), spread.get(2, 0.0) + spread.get(3, 0.0)
    else:                      # no spread returned: fall back to the averaged score, conservatively
        p_simple, p_hard = float(difficulty < 0.5), float(difficulty >= 2.25)

    # An unsure answer is not evidence of a hard turn. Its averaged score lands mid-rubric by arithmetic,
    # so it must never buy the expensive tier: a harmless unsure turn stays put, a risky one gets medium.
    unsure = confidence < config["min_confidence"]
    if unsure and not (risky or stakes > 0.6):
        return _keep(current, f"low confidence {confidence:.2f}", private=private)

    if unsure:
        tier = "medium"
    elif p_hard >= config["hard_needs_probability"]:
        tier = "hard"
    elif p_simple >= config["simple_needs_probability"] and confidence >= config["simple_needs_confidence"]:
        tier = "simple"
    else:
        tier = "medium"
    if tier == "simple" and (risky or stakes > 0.4 or mode == "features"):
        tier = "medium"        # risk words and costly mistakes set a floor of medium; they do not buy hard
    if not unsure and stakes > 0.85 and p_hard >= 0.35:
        tier = "hard"          # a costly mistake tips a turn that is already leaning hard
    kind = answers["kind"]
    specialty = kind["choice"] if kind["confidence"] >= 0.5 else "general"

    catalog_rows = rows if rows is not None else catalog_mod.models()
    by_ref = {_ref(row): row for row in catalog_rows}
    picked = _pick(config, by_ref, tier, specialty, has_images, context_tokens, only_provider)
    if not picked:
        return _keep(current, f"no {tier} model fits this turn", private=private)

    if current and picked != current and context_tokens > config["sticky_context_tokens"]:
        current_price = (by_ref.get(current) or {}).get("price")
        picked_price = (by_ref.get(picked) or {}).get("price")
        if current_price is not None and picked_price is not None and picked_price < current_price:
            return _keep(current, "large context; switching down would cost more than it saves", private=private)

    provider, model = picked.split(":", 1)
    escalation = None
    settings = config.get("escalation") or {}
    if tier == "hard" and settings.get("enabled") and settings.get("rungs"):
        # A frontier seat is worth its cost only on work that earned the hard tier. Everything
        # below hard stays on OpenRouter, which is the whole point of paying for frontier seats.
        escalation = ladder.choose(settings["rungs"])
        escalation["stakes"] = round(stakes, 3)

    return _remember(cache_key, {
        "routed": picked != current, "model": picked, "provider": provider, "model_id": model, "tier": tier,
        **({"escalate": escalation} if escalation else {}),
        "specialty": specialty, "confidence": round(confidence, 3), "difficulty": round(difficulty, 2),
        "costly_mistake": round(stakes, 3), "private": private, "mode": mode, "latency_ms": reply["latency_ms"],
        "policy": POLICY_VERSION, "reason": f"{tier} {specialty}", "unwrapped": unwrapped,
        "notice": (f"[Jev] {tier} · {specialty} → {model} · confidence {confidence:.2f}"
                   + (f" · escalate to {escalation['rung']}" + (" (forced)" if escalation.get("forced") else "")
                      if escalation and escalation.get("rung") else "")),
    }, config)
