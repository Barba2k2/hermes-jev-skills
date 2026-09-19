"""`jev`: one command for every Jev skill. JSON in on stdin, JSON out on stdout.

Nothing here ever prints an API key. Every subcommand that talks to Jev exits 0
with a usable fail-open answer when Jev is down, so an agent never gets stuck.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__, catalog, choose, client, compact, key_setup, keystore, ladder, rerank, replay, route, skillpick, spend, supervise, triage


def _stdin_json() -> Any:
    raw = sys.stdin.read(2_000_000)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise SystemExit(f"stdin is not valid JSON: {error}") from None


def _out(value: Any) -> int:
    sys.stdout.write(json.dumps(value, indent=2, default=str) + "\n")
    return 0


def _skill_roots(extra: List[str]) -> List[Path]:
    home = Path.home()
    hermes = catalog.hermes_home()
    roots = [Path(p).expanduser() for p in extra] or [
        hermes / "skills", home / ".claude" / "skills", home / ".codex" / "skills", home / ".agents" / "skills",
        Path.cwd() / ".claude" / "skills", Path.cwd() / "skills"]
    return roots


def cmd_setup_key(args: argparse.Namespace) -> int:
    hermes_home = Path(args.hermes_home).expanduser() if args.hermes_home else None
    common = {"verify": not args.no_verify, "hermes": not args.no_hermes, "hermes_home": hermes_home}
    if args.tty:
        result = key_setup.run_tty(**common)
    else:
        result = key_setup.run_browser(host=args.host, port=args.port, timeout=args.timeout,
                                       open_browser=not args.no_open, **common)
    _out(result)
    return 0 if result.get("status") == "stored" else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    report: Dict[str, Any] = {"version": __version__, "key": keystore.describe()}
    if report["key"]["present"] and not args.offline:
        try:
            reply = client.ask("The build finished and all tests passed.",
                               {"ok": client.noul("The text reports a successful outcome")}, timeout=10)
            report["jev"] = {"reachable": True, "latency_ms": reply["latency_ms"]}
        except client.JevError as error:
            report["jev"] = {"reachable": False, "error": error.code}
    config = route.load_config()
    report["routing"] = {"config": str(route.config_path()), "mode": config["mode"],
                         "tiers_configured": sorted(config.get("tiers") or {})}
    report["hermes_home"] = str(catalog.hermes_home()) if catalog.hermes_home().is_dir() else None
    _out(report)
    return 0 if report["key"]["present"] else 1


def cmd_models(args: argparse.Namespace) -> int:
    data = catalog.load_models_dev(refresh=args.refresh)
    if args.action == "providers":
        return _out({"available": catalog.available_providers(data)})
    rows = catalog.models(data)
    config = route.load_config()
    if args.action == "list":
        if args.provider:
            rows = [r for r in rows if r["provider"] == args.provider]
        if args.search:
            rows = [r for r in rows if args.search.lower() in (r["model"] + r["name"]).lower()]
        return _out({"count": len(rows), "models": rows})
    tiers = route.suggest_tiers(rows, config["exclude"])
    if args.write:
        config["tiers"] = tiers
        path = route.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        return _out({"written": str(path), "tiers": tiers})
    return _out({"suggested_tiers": tiers, "note": "add --write to save, then edit the pools to taste"})


def cmd_route(args: argparse.Namespace) -> int:
    request = _stdin_json() if args.prompt is None else {"prompt": args.prompt}
    return _out(route.decide(
        request["prompt"], current=request.get("current") or args.current,
        context_tokens=int(request.get("context_tokens") or args.context_tokens),
        has_images=bool(request.get("has_images") or args.has_images), profile=request.get("profile") or args.profile,
        pinned=bool(request.get("pinned")), timeout=args.timeout))


def cmd_rerank(args: argparse.Namespace) -> int:
    request = _stdin_json()
    return _out(rerank.rerank(request["query"], request["candidates"], top_k=int(request.get("top_k", 8))))


def cmd_compact(args: argparse.Namespace) -> int:
    request = _stdin_json()
    messages = request["messages"] if isinstance(request, dict) else request
    selection = compact.select(messages, keep_last=args.keep_last)
    if args.digest:
        selection["digest"] = compact.digest(messages, selection)
    return _out(selection)


def cmd_pick_skill(args: argparse.Namespace) -> int:
    turn = args.turn if args.turn is not None else _stdin_json()["turn"]
    return _out(skillpick.pick(turn, skillpick.discover(_skill_roots(args.root)), top_k=args.top_k))


def cmd_choose(args: argparse.Namespace) -> int:
    try:
        return _out(choose.choose(_stdin_json(), mock=args.mock))
    except ValueError as error:
        raise SystemExit(f"invalid request: {error}") from None


def _rungs(config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    settings = (config or route.load_config()).get("escalation") or {}
    return list(settings.get("rungs") or [])


def cmd_triage(args: argparse.Namespace) -> int:
    """Classify incoming messages: act now, today, queue, or ignore."""
    if args.file:
        raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
        messages = raw if isinstance(raw, list) else raw.get("messages") or raw.get("items") or []
    else:
        raw = _stdin_json()
        messages = raw if isinstance(raw, list) else [raw]
    if not messages:
        raise SystemExit("no messages to classify")
    rows = triage.classify_many(messages, workers=args.workers,
                                known_domains=args.customer_domain or [])
    if args.summary:
        return _out(triage.summarize(rows))
    return _out({"summary": triage.summarize(rows), "messages": rows})


def cmd_spend(args: argparse.Namespace) -> int:
    """What the window cost, and what it would have cost on every alternative."""
    rows: List[spend.Usage] = []
    for path in args.usage or []:
        rows += spend.from_json(path)
    for db in args.hermes_db or []:
        rows += spend.from_hermes_sessions(db, since=time.time() - args.days * 86400,
                                           label=Path(db).parent.name)
    if not rows:
        raise SystemExit("no usage: pass --usage <export.json> and/or --hermes-db <state.db>")
    seats = [spend.Seat(**s) for s in json.loads(Path(args.seats).read_text(encoding="utf-8"))] if args.seats else []
    data = spend.report(rows, candidates=args.compare or [], seats=seats, days=args.days)
    if args.json:
        return _out(data)
    sys.stdout.write(spend.render(data) + "\n")
    return 0


def cmd_ladder(args: argparse.Namespace) -> int:
    """Which frontier seat takes hard work, and which ones are currently full."""
    rungs = _rungs()
    if args.action == "status":
        return _out(ladder.status(rungs))
    if args.action == "choose":
        if not rungs:
            raise SystemExit("no escalation.rungs configured in routing.json")
        return _out(ladder.choose(rungs, skip_probe=args.no_probe))
    if args.action == "refuse":
        if not args.rung:
            raise SystemExit("refuse needs --rung")
        return _out(ladder.refuse(args.rung, args.reason or "refused", cooldown=args.cooldown))
    ladder.clear(args.rung)
    return _out({"cleared": args.rung or "all"})


def cmd_supervise(args: argparse.Namespace) -> int:
    """Assess one delegated run: is it progressing, stuck, waiting on you, or done."""
    request = _stdin_json() if not args.tail_file else {
        "goal": args.goal or "", "tail": Path(args.tail_file).read_text(encoding="utf-8", errors="replace")}
    goal = request.get("goal") or args.goal or ""
    if not goal.strip():
        raise SystemExit("supervise needs a goal (--goal, or a `goal` field on stdin)")
    snap = supervise.assess(
        goal, str(request.get("tail") or ""), elapsed_s=float(request.get("elapsed_s") or 0),
        quiet_s=float(request.get("quiet_s") or 0), new_output=bool(request.get("new_output", True)),
        looping=bool(request.get("looping")), exited=request.get("exited"), timeout=args.timeout)
    return _out(snap.as_dict())


def cmd_replay(args: argparse.Namespace) -> int:
    """Replay real turns through the policy and price the result against the baseline."""
    turns = replay.turns_from_jsonl(args.turns, current=args.current, limit=args.limit)
    if not turns:
        raise SystemExit(f"no usable turns in {args.turns} (expect JSONL with a `prompt` field)")
    out = replay.replay(turns, only_provider=args.only_provider, workers=args.workers)
    if args.verbose:
        return _out(out)
    return _out(out["summary"])


def cmd_dashboard(args: argparse.Namespace) -> int:
    """The model-routing page: per-profile models, an all-profiles target, the Jev switch and live decisions."""
    import os
    import subprocess

    server = Path(__file__).resolve().parents[1] / "router-dashboard" / "server.py"
    if not server.is_file():
        raise SystemExit("the dashboard ships with the repo checkout; run `jev` from there")
    # The dashboard needs PyYAML. Hermes' own interpreter always has it.
    venv = catalog.hermes_root() / "hermes-agent" / "venv" / "bin" / "python"
    python = str(venv) if venv.is_file() else sys.executable
    command = [python, str(server), "--host", args.host, "--port", str(args.port), "--hermes-home", str(catalog.hermes_root())]
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        import secrets
        token = os.environ.get("DASHBOARD_TOKEN") or secrets.token_urlsafe(24)
        os.environ["DASHBOARD_TOKEN"] = token
        print(f"open once with the token: http://{args.host}:{args.port}/?token={token}", file=sys.stderr)
    return subprocess.call(command)


def cmd_ask(args: argparse.Namespace) -> int:
    request = _stdin_json()
    try:
        return _out(client.ask(request["state"], request["questions"], timeout=args.timeout))
    except client.JevError as error:
        _out({"error": error.code})
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jev", description="Hermes Jev Skills")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup-key", help="open a private page for the person to paste their TypeSafe key")
    p.add_argument("--tty", action="store_true", help="hidden terminal prompt instead of a browser page")
    p.add_argument("--host", default="127.0.0.1", help="bind address; keep loopback unless you are on a private network")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--no-open", action="store_true", help="do not try to open a browser; just print the link")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--no-hermes", action="store_true", help="do not write the key into Hermes lane .env files")
    p.add_argument("--hermes-home")
    p.set_defaults(func=cmd_setup_key)

    p = sub.add_parser("doctor", help="is the key present and does Jev answer")
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("models", help="every model this machine can call")
    p.add_argument("action", choices=["list", "providers", "suggest"])
    p.add_argument("--provider")
    p.add_argument("--search")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--write", action="store_true")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("route", help="pick the model for one fresh user turn")
    p.add_argument("--prompt")
    p.add_argument("--current")
    p.add_argument("--profile")
    p.add_argument("--context-tokens", type=int, default=0)
    p.add_argument("--has-images", action="store_true")
    p.add_argument("--timeout", type=float, default=2.5)
    p.set_defaults(func=cmd_route)

    p = sub.add_parser("rerank", help="filter retrieved memory passages")
    p.set_defaults(func=cmd_rerank)

    p = sub.add_parser("compact-select", help="mark each transcript turn keep / summarize / drop")
    p.add_argument("--keep-last", type=int, default=6)
    p.add_argument("--digest", action="store_true", help="also return the reduced transcript for the summarizer")
    p.set_defaults(func=cmd_compact)

    p = sub.add_parser("pick-skill", help="which skill, if any, this turn needs")
    p.add_argument("--turn")
    p.add_argument("--root", action="append", default=[])
    p.add_argument("--top-k", type=int, default=3)
    p.set_defaults(func=cmd_pick_skill)

    p = sub.add_parser("choose", help="pick the next GUI or browser action from a candidate table")
    p.add_argument("--mock", action="store_true")
    p.set_defaults(func=cmd_choose)

    p = sub.add_parser("triage", help="classify incoming messages: now / today / queue / ignore")
    p.add_argument("--file", help="JSON list of messages (subject, content, sender); else read stdin")
    p.add_argument("--customer-domain", action="append", help="a domain whose mail is a real customer (repeatable)")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--summary", action="store_true", help="counts only, no per-message rows")
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser("spend", help="weekly cost report: what ran, what it cost, what would have been cheaper")
    p.add_argument("--usage", action="append", help="JSON export of metered usage rows (repeatable)")
    p.add_argument("--hermes-db", action="append", help="Hermes state.db, to value subscription work (repeatable)")
    p.add_argument("--compare", action="append", help="model id to price the same tokens against (repeatable)")
    p.add_argument("--seats", help="JSON list of flat-fee seats: name, monthly_usd, market_equivalent")
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_spend)

    p = sub.add_parser("ladder", help="the frontier escalation ladder: who takes hard work, and who is full")
    p.add_argument("action", choices=["status", "choose", "refuse", "clear"])
    p.add_argument("--rung", help="rung name, for refuse/clear")
    p.add_argument("--reason", help="why it refused, e.g. the quota message")
    p.add_argument("--cooldown", type=float, default=ladder.DEFAULT_COOLDOWN, help="seconds to skip this rung")
    p.add_argument("--no-probe", action="store_true", help="trust the cooldowns; do not run availability probes")
    p.set_defaults(func=cmd_ladder)

    p = sub.add_parser("supervise", help="assess a delegated run: progressing, stuck, waiting on you, or done")
    p.add_argument("--goal", help="what the delegated run was asked to do")
    p.add_argument("--tail-file", help="file holding the run's recent output; otherwise read JSON on stdin")
    p.add_argument("--timeout", type=float, default=5.0)
    p.set_defaults(func=cmd_supervise)

    p = sub.add_parser("replay", help="replay logged turns through the policy and price it against the baseline")
    p.add_argument("turns", help="JSONL file, one object per turn with at least a `prompt` field")
    p.add_argument("--current", help="the model these turns ran on, e.g. openrouter:deepseek/deepseek-v4.1-flash")
    p.add_argument("--only-provider", help="restrict picks to one provider, as the Hermes plugin does")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--verbose", action="store_true", help="include every per-turn decision, not just the summary")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("dashboard", help="open the model-routing page (models per profile, Jev switch, live decisions)")
    p.add_argument("--host", default="127.0.0.1", help="keep loopback unless you are on a private network; other hosts require a token")
    p.add_argument("--port", type=int, default=8791)
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("ask", help="raw Jev call: {state, questions}")
    p.add_argument("--timeout", type=float, default=5)
    p.set_defaults(func=cmd_ask)
    return parser


def main(argv: Any = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
