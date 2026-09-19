#!/usr/bin/env python3
"""Bounded GUI computer-use loop: cua-driver (MCP) + Jev.

The counterpart to jev_browser_agent.py for desktop surfaces. You own the
planner role; Jev only ever picks one id from a table you built. It never
writes coordinates, selectors or text.

  observe (cua-driver) -> element table -> jev choose -> one action -> observe

Usage:
  python3 jev_gui_agent.py --pid 26955 --window-id 46041 \
    --goal 'Open the Library page in YouTube Music' \
    --expect 'Library' --max-steps 12 --json

Exit codes: 0 verified, 4 unverified, 2 refused to start, 6 abstained.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- jevkit

def _repo_root() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "jevkit" / "choose.py").is_file():
            return parent
    return None


ROOT = _repo_root()
if ROOT is not None and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from jevkit.choose import choose as jev_choose
    from jevkit.privacy import is_sensitive
except Exception:  # noqa: BLE001
    jev_choose = None

    def is_sensitive(text: str) -> bool:  # type: ignore[misc]
        return False


def _find_driver() -> str:
    """Locate cua-driver without baking anyone's home directory into the repo.

    A hardcoded /Users/<someone>/ path is both wrong on every other machine and blocked
    by scripts/check_release.py, which is the gate that keeps fleet-specific runtime out
    of the public skill. Order: explicit override, PATH, then the usual install sites.
    """
    override = os.environ.get("CUA_DRIVER_BIN")
    if override:
        return override
    found = shutil.which("cua-driver")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "cua-driver",
                      Path("/Applications/CuaDriver.app/Contents/MacOS/cua-driver"),
                      Path("/usr/local/bin/cua-driver")):
        if candidate.exists():
            return str(candidate)
    return "cua-driver"      # let the failure name the missing binary


CUA = _find_driver()

INTERACTIVE_ROLES = {
    "AXButton", "AXLink", "AXTextField", "AXCheckBox", "AXRadioButton",
    "AXPopUpButton", "AXComboBox", "AXMenuItem", "AXSearchField",
    "AXTab", "AXSlider", "AXDisclosureTriangle", "AXSegmentedControl",
}

# ---------------------------------------------------------------- MCP client


class Driver:
    """Minimal MCP stdio client for cua-driver."""

    def __init__(self, binary: str = CUA) -> None:
        self.proc = subprocess.Popen(
            [binary, "mcp", "--no-daemon-relaunch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        self._id = 0
        self.call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "jev-gui-agent", "version": "0.1.0"},
        })
        self._notify("notifications/initialized")

    def _send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def call(self, method: str, params: dict, timeout: float = 90.0) -> dict:
        self._id += 1
        mid = self._id
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        assert self.proc.stdout is not None
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == mid:
                return msg
        return {"error": {"message": "timeout"}}

    def tool(self, name: str, args: dict, timeout: float = 90.0) -> dict:
        return self.call("tools/call", {"name": name, "arguments": args}, timeout)

    def stop(self) -> None:
        try:
            self.proc.terminate()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- observation


def observe(driver: Driver, pid: int, window_id: int, session: str) -> dict:
    args = {
        "pid": pid, "window_id": window_id,
        "include_screenshot": False, "max_elements": 6000,
    }
    if session:
        args["session"] = session
    res = driver.tool("get_window_state", args)
    return res.get("result", {}).get("structuredContent", {}) or {}


def with_session(args: dict, session: str) -> dict:
    if session:
        args["session"] = session
    return args


def window_bounds(state: dict) -> dict:
    return state.get("window_bounds") or {}


def content_bounds(state: dict) -> dict | None:
    """The web-content region of a browser window, so chrome buttons stay out of the table."""
    best = None
    for el in state.get("elements", []):
        if el.get("role") != "AXWebArea":
            continue
        frame = el.get("frame") or {}
        if float(frame.get("w", 0)) < 200 or float(frame.get("h", 0)) < 200:
            continue
        if best is None or float(frame.get("w", 0)) * float(frame.get("h", 0)) > \
                float(best.get("w", 0)) * float(best.get("h", 0)):
            best = frame
    return best


STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "into", "from", "then", "than",
    "click", "open", "page", "using", "left", "right", "sidebar", "button", "link",
    "so", "can", "will", "should", "make", "sure", "about", "must", "need",
}


def goal_tokens(goal: str) -> list[str]:
    out = []
    for word in goal.lower().replace("'s", "").split():
        word = word.strip(".,:;()\"'`[]!?")
        if len(word) > 3 and word not in STOPWORDS:
            out.append(word)
    return out


def relevance(label: str, tokens: list[str]) -> int:
    low = label.lower()
    return sum(1 for t in tokens if t in low)


def element_rows(state: dict, max_regions: int, tokens: list[str] | None = None) -> list[dict]:
    """Visible, labelled, interactive elements, ranked, deduplicated.

    When the window is a browser, only elements inside the web-content area are
    offered: the toolbar, omnibox and extension buttons are never the answer.
    Rows that share a meaningful word with the goal float to the top, because
    the 32-candidate contract cannot hold every control on a rich web app.
    """
    tokens = tokens or []
    bounds = window_bounds(state)
    bx, by = float(bounds.get("x", 0)), float(bounds.get("y", 0))
    bw, bh = float(bounds.get("width", 0)), float(bounds.get("height", 0))
    content = content_bounds(state)
    if content is not None:
        bx, by = float(content.get("x", 0)), float(content.get("y", 0))
        bw, bh = float(content.get("w", 0)), float(content.get("h", 0))
    rows: list[dict] = []
    seen: set[tuple[str, int, int]] = set()
    for el in state.get("elements", []):
        label = (el.get("label") or "").strip()
        if not label:
            continue
        role = el.get("role") or ""
        acts = el.get("actions") or []
        if role not in INTERACTIVE_ROLES and "AXPress" not in acts:
            continue
        frame = el.get("frame") or {}
        x, y = float(frame.get("x", 0)), float(frame.get("y", 0))
        w, h = float(frame.get("w", 0)), float(frame.get("h", 0))
        if w < 8 or h < 8:
            continue
        if bw and bh and not (bx <= x <= bx + bw and by <= y <= by + bh):
            continue
        key = (label[:60], int(x), int(y))
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "token": el.get("element_token"),
            "role": role,
            "label": label[:120],
            "x": x, "y": y, "w": w, "h": h,
            "local_x": x - bx, "local_y": y - by,
            "cx": x - bx + w / 2, "cy": y - by + h / 2,
            "index": el.get("element_index"),
        })
    rows.sort(key=lambda r: (-relevance(r["label"], tokens), r["y"], r["x"]))
    return rows[:max_regions]


def single_line(text: str, limit: int = 78) -> str:
    return " ".join(text.split())[:limit]


def build_table(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    regions: list[dict] = []
    candidates: list[dict] = []
    for i, r in enumerate(rows):
        rid = f"r{i}"
        regions.append({
            "id": rid, "role": r["role"].replace("AX", "").lower(),
            "label": single_line(r["label"]), "interactive": True,
        })
        verb = "Type into" if r["role"] in ("AXTextField", "AXSearchField") else "Click"
        candidates.append({
            "id": f"{'type' if verb == 'Type into' else 'click'}:{r['token']}",
            "description": f"{verb} [{i}] {r['role'].replace('AX','').lower()} "
                           f"\"{single_line(r['label'])}\".",
        })
    for extra, desc in (
        ("scroll-down", "Scroll the page down to reveal more elements."),
        ("scroll-up", "Scroll the page up."),
        ("wait", "Wait one second for the page to finish changing."),
        ("reobserve", "Take a fresh observation without changing anything."),
        ("done", "The goal is fully achieved and independently verified."),
        ("abstain", "Stop and ask the person for help."),
    ):
        candidates.append({"id": extra, "description": desc})
    return regions, candidates


# ---------------------------------------------------------------- text helper

def text_helper(goal: str, field_label: str, values: list[str]) -> str:
    key = os.environ.get("TEXT_MODEL_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
    model = os.environ.get("TEXT_MODEL", "google/gemini-2.5-flash")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    if not key or not values:
        return values[0] if values else ""
    prompt = (
        "You write one short value to type into a GUI field. Reply as JSON "
        '{"text": "..."} and nothing else.\n'
        f"Goal: {goal}\nField: {field_label}\n"
        f"Allowed values (choose the best one, exactly as written): {values}\n"
    )
    body = json.dumps({
        "model": model, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as fh:
            payload = json.load(fh)
        raw = payload["choices"][0]["message"]["content"].strip()
        raw = raw.strip("`").removeprefix("json").strip()
        return str(json.loads(raw).get("text", "")).strip() or values[0]
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError):
        return values[0]


# ---------------------------------------------------------------- execution


def execute(driver: Driver, pid: int, window_id: int, session: str,
            action: str, rows: list[dict], goal: str, values: list[str]) -> tuple[str, str]:
    """Run exactly one pre-validated action. Returns (op, detail)."""
    if action.startswith("click:"):
        token = action.split(":", 1)[1]
        for r in rows:
            if r["token"] == token:
                res = driver.tool("click", with_session({
                    "pid": pid, "window_id": window_id,
                    "element_token": token, "delivery_mode": "background",
                }, session))
                return "click", json.dumps(_brief(res))
        return "click", "target token no longer observed"
    if action.startswith("type:"):
        token = action.split(":", 1)[1]
        for r in rows:
            if r["token"] == token:
                text = text_helper(goal, r["label"], values)
                res = driver.tool("type_text", with_session({
                    "pid": pid, "window_id": window_id,
                    "element_token": token, "text": text,
                    "delivery_mode": "foreground",
                }, session), timeout=120)
                return "type", f"{text!r} -> {json.dumps(_brief(res))}"
        return "type", "target token no longer observed"
    if action in ("scroll-down", "scroll-up"):
        res = driver.tool("scroll", with_session({
            "pid": pid, "window_id": window_id,
            "direction": action.split("-", 1)[1], "amount": 6,
        }, session))
        return "scroll", json.dumps(_brief(res))
    if action == "wait":
        time.sleep(1.0)
        return "wait", "slept 1s"
    return action, "no-op"


def _brief(res: dict) -> dict:
    out = res.get("result", {}) if isinstance(res, dict) else {}
    sc = out.get("structuredContent") or {}
    return {
        "effect": sc.get("effect", out.get("status", "unknown")),
        "message": str(out.get("result") or res.get("error") or "")[:120],
    }


def verify(rows: list[dict], title: str, expect: str) -> bool:
    if not expect:
        return True
    needle = expect.lower()
    if needle in (title or "").lower():
        return True
    return any(needle in r["label"].lower() for r in rows)


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Bounded GUI computer-use loop: cua-driver + Jev")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--window-id", type=int, required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--max-steps", type=int, default=10)
    p.add_argument("--max-regions", type=int, default=26,
                   help="Element rows offered to Jev (26 keeps the table inside the "
                        "32-candidate contract once the standard actions are added).")
    p.add_argument("--expect", default="")
    p.add_argument("--values", default="", help="Pipe-separated pool for typed fields.")
    p.add_argument("--session", default="",
                   help="Optional cua-driver session label (omit when the transport has none).")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    values = [v.strip() for v in args.values.split("|") if v.strip()]
    tokens = goal_tokens(args.goal)

    if jev_choose is None:
        print("FAIL: jevkit not importable from this checkout.")
        return 2
    if is_sensitive(args.goal):
        print("FAIL: the goal looks sensitive; refusing to send it to Jev.")
        return 2

    for k in ("TYPESAFE_API_KEY",):
        if k in os.environ:
            continue
    if not os.environ.get("TYPESAFE_API_KEY"):
        tok = subprocess.run(
            ["security", "find-generic-password", "-s", "Hermes TypeSafe API",
             "-a", "TYPESAFE_API_KEY", "-w"],
            capture_output=True, text=True)
        if tok.returncode == 0 and tok.stdout.strip():
            os.environ["TYPESAFE_API_KEY"] = tok.stdout.strip()
    if not os.environ.get("TEXT_MODEL_API_KEY") and not os.environ.get("OPENROUTER_API_KEY"):
        tok = subprocess.run(
            ["security", "find-generic-password", "-s", "OPENROUTER_API_KEY",
             "-a", "vibex", "-w"],
            capture_output=True, text=True)
        if tok.returncode == 0 and tok.stdout.strip():
            os.environ["OPENROUTER_API_KEY"] = tok.stdout.strip()
    if not os.environ.get("TYPESAFE_API_KEY"):
        print("FAIL: no TypeSafe credential. Run `jev setup-key`.")
        return 2

    driver = Driver()
    log: list[dict] = []
    history: list[dict] = []
    title = ""
    rows: list[dict] = []
    steps = 0
    abstained = False
    t0 = time.time()
    try:
        if args.session:
            driver.tool("start_session", {"session": args.session})
        stalled = 0
        last_digest: str | None = None
        for step in range(1, args.max_steps + 1):
            state = observe(driver, args.pid, args.window_id, args.session)
            title = state.get("window_title", "")
            rows = element_rows(state, args.max_regions, tokens)
            if not rows:
                print(f"  step {step}: no interactive elements observed")
                break
            safe_rows = [r for r in rows if not is_sensitive(r["label"])]
            withheld = len(rows) - len(safe_rows)
            if withheld:
                print(f"  step {step}: withheld {withheld} label(s) that look sensitive")
            rows = safe_rows
            if not rows:
                print("  every observed label looks sensitive; stopping")
                abstained = True
                break
            digest = "|".join(f"{r['label'][:40]}@{int(r['x'])},{int(r['y'])}" for r in rows)
            stalled = stalled + 1 if digest == last_digest else 0
            last_digest = digest
            if args.expect and verify(rows, title, args.expect):
                steps = step - 1
                print(f"  verified before step {step}; stopping")
                break
            regions, candidates = build_table(rows)
            request = {
                "schema": "jev.action_choice_request_v1",
                "goal": args.goal,
                "observation_id": f"obs-{step}",
                "regions": regions,
                "history": history[-6:],
                "candidates": candidates,
            }
            try:
                reply = jev_choose(request)
            except ValueError as exc:
                print(f"  step {step}: request rejected by the Jev contract: {exc}")
                break
            action = reply.get("selected_id", "reobserve")
            confidence = reply.get("confidence")
            steps = step
            if action in ("done", "abstain"):
                print(f"  step {step:>2}  Jev -> {action} "
                      f"(conf {confidence}) {reply.get('reason','')}")
                abstained = action == "abstain"
                history.append({"selected_id": action, "outcome": "loop ended"})
                break
            if action == "reobserve" and stalled >= 2:
                print(f"  step {step:>2}  nothing on screen is changing after "
                      f"{stalled} reobservations; stopping instead of spinning")
                break
            t1 = time.time()
            op, detail = execute(driver, args.pid, args.window_id, args.session,
                                 action, rows, args.goal, values)
            print(f"  step {step:>2}  {round((time.time()-t1)*1000):>5} ms  op={op}  "
                  f"conf={confidence}  {detail[:110]}")
            log.append({"step": step, "action": action, "op": op,
                        "detail": detail, "confidence": confidence,
                        "decision_ms": int((time.time() - t1) * 1000)})
            history.append({"selected_id": action, "outcome": f"{op}: {detail[:80]}"})
            if op == "no-op":
                time.sleep(0.3)
            if not action.startswith(("click:", "type:")):
                continue
    finally:
        try:
            final = observe(driver, args.pid, args.window_id, args.session)
            title = final.get("window_title", title)
            rows = element_rows(final, args.max_regions, tokens)
        except Exception:  # noqa: BLE001
            pass
        driver.stop()

    verified = verify(rows, title, args.expect)
    result = {
        "schema": "hermes.computer_use_jev_run_v1",
        "goal": args.goal,
        "window_title": title,
        "steps": steps,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "expected": args.expect,
        "verified": verified,
        "abstained": abstained,
        "regions_last": len(rows),
        "actions": log,
    }
    print(f"  window_title: {title!r}")
    print(f"  independent check for {args.expect!r}: {'PASS' if verified else 'FAIL'}")
    print(f"  {steps} steps, {result['elapsed_ms']} ms total")
    if args.json:
        print(json.dumps(result))
    if abstained:
        return 6
    return 0 if verified else 4


if __name__ == "__main__":
    sys.exit(main())
