#!/usr/bin/env python3
"""Close every live conversation for the night and leave tomorrow a capsule.

An agent that is never given a fresh start accumulates. One of the conversations this
was written for had 2,255 messages and seventeen days of history, and every single turn
re-sent all of it. Nobody chose that; it is just what happens when a session never ends.

So this runs once a night. For each conversation that is actually alive, it writes a
handoff capsule and marks the session closed. The next morning's first message opens a
new session and receives the capsule, so the agent knows what it was doing without
carrying the transcript that proves it.

Deliberately conservative:
  * Only sessions with recent activity and enough substance to be worth summarising.
  * A capsule must be written BEFORE a session is closed. If the capsule fails, the
    session is left open — losing the thread is worse than a large context.
  * --dry-run shows exactly what would happen and touches nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

MIN_MESSAGES = 6            # below this there is nothing worth carrying forward
ACTIVE_WITHIN_S = 3 * 86400  # a conversation nobody has touched in days needs no capsule
MAX_PER_PROFILE = 40        # a runaway night must not mean hundreds of model calls


def log(message: str) -> None:
    sys.stderr.write(f"[nightly-handoff] {message}\n")
    sys.stderr.flush()


def profiles(home: Path, names: Optional[List[str]] = None) -> List[Path]:
    root = home / "profiles"
    if not root.is_dir():
        return []
    found = [p for p in sorted(root.iterdir()) if (p / "state.db").is_file()]
    if names:
        wanted = set(names)
        found = [p for p in found if p.name in wanted]
    return found


def live_sessions(db: Path, *, now: float, min_messages: int, within_s: float, limit: int) -> List[Dict[str, Any]]:
    """Open sessions with recent activity and real content, newest first."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=20)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as error:
        log(f"  cannot read {db}: {error}")
        return []
    columns = {r[1] for r in conn.execute("pragma table_info(sessions)")}
    if not {"id", "ended_at"} <= columns:
        return []
    activity = "last_activity_at" if "last_activity_at" in columns else "started_at"
    optional = [c for c in ("session_key", "chat_id", "thread_id", "source", "message_count", "title") if c in columns]
    select = ", ".join(["id", activity + " as activity_at"] + optional)
    rows: List[Dict[str, Any]] = []
    try:
        for row in conn.execute(
                f"select {select} from sessions where ended_at is null and {activity} is not null "
                f"and {activity} > ? order by {activity} desc limit ?", (now - within_s, limit * 4)):
            entry = dict(row)
            count = entry.get("message_count")
            if count is None:
                count = conn.execute("select count(*) from messages where session_id=?", (entry["id"],)).fetchone()[0]
            entry["messages"] = int(count or 0)
            if entry["messages"] >= min_messages:
                rows.append(entry)
            if len(rows) >= limit:
                break
    except sqlite3.Error as error:
        log(f"  query failed on {db}: {error}")
    finally:
        conn.close()
    return rows


def close_session(db: Path, session_id: str, *, now: float, reason: str) -> bool:
    """Mark the session ended so the next message opens a fresh one."""
    try:
        conn = sqlite3.connect(str(db), timeout=30)
        conn.execute("pragma busy_timeout=30000")
        columns = {r[1] for r in conn.execute("pragma table_info(sessions)")}
        if "end_reason" in columns:
            conn.execute("update sessions set ended_at=?, end_reason=? where id=? and ended_at is null",
                         (now, reason, session_id))
        else:
            conn.execute("update sessions set ended_at=? where id=? and ended_at is null", (now, session_id))
        conn.commit()
        changed = conn.total_changes > 0
        conn.close()
        return changed
    except sqlite3.Error as error:
        log(f"  could not close {session_id}: {error}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"))
    parser.add_argument("--profile", action="append", help="limit to these profiles (repeatable)")
    parser.add_argument("--plugin", help="path to the hermes-handoff plugin (default: <home>/plugins/hermes-handoff)")
    parser.add_argument("--min-messages", type=int, default=MIN_MESSAGES)
    parser.add_argument("--within-hours", type=float, default=ACTIVE_WITHIN_S / 3600)
    parser.add_argument("--limit", type=int, default=MAX_PER_PROFILE)
    parser.add_argument("--dry-run", action="store_true", help="report only; write and close nothing")
    parser.add_argument("--confidential", action="store_true",
                        help="capsules carry no customer detail (required where a continuity "
                             "rule forbids persisting it); a capsule that cannot meet the "
                             "contract is not written at all")
    args = parser.parse_args()

    home = Path(args.hermes_home).expanduser()
    plugin = Path(args.plugin) if args.plugin else home / "plugins" / "hermes-handoff"
    if not (plugin / "handoff.py").is_file():
        log(f"no handoff plugin at {plugin}")
        return 2
    sys.path.insert(0, str(plugin))
    os.environ.setdefault("HERMES_HOME", str(home))
    # The .env holds the Jev key; the CLI loads it per-process, a plain script does not.
    env_file = home / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip().isidentifier() and name.strip() not in os.environ:
                os.environ[name.strip()] = value.strip()

    import handoff as ho  # noqa: E402
    compact = None
    try:
        from jevkit import compact as _compact  # noqa: E402
        compact = _compact
    except Exception as error:  # noqa: BLE001
        log(f"jevkit unavailable ({error}); every turn will be treated as background")

    def writer(prompt: str) -> str:
        from agent.auxiliary_client import call_llm  # type: ignore
        return ho.extract_text(call_llm(task="compression", messages=[{"role": "user", "content": prompt}]))

    now = time.time()
    report: Dict[str, Any] = {"at": now, "dry_run": args.dry_run, "profiles": []}
    for profile in profiles(home, args.profile):
        db = profile / "state.db"
        sessions = live_sessions(db, now=now, min_messages=args.min_messages,
                                 within_s=args.within_hours * 3600, limit=args.limit)
        log(f"{profile.name}: {len(sessions)} live session(s) to hand off")
        done: List[Dict[str, Any]] = []
        for session in sessions:
            # Deliberately the plugin's own function: two implementations of this rule
            # would drift, and a capsule keyed differently from how it is read is a
            # silent no-op that looks like the feature simply not working.
            lane = ho.lane_key({**session, "session_id": session["id"]})
            entry = {"session": session["id"], "lane": lane, "messages": session["messages"]}
            if args.dry_run:
                entry["action"] = "would hand off and close"
                done.append(entry)
                log(f"  [dry-run] {session['id']}  lane={lane}  msgs={session['messages']}")
                continue
            os.environ["HERMES_HOME"] = str(profile)     # capsules live beside their own profile
            # Checked per profile, after HERMES_HOME moves: one profile may be bound by a
            # continuity rule its neighbour is not.
            confidential = args.confidential or ho.confidential_here()
            entry["confidential"] = confidential
            try:
                built = ho.build(session["id"], lane, write=writer,
                                 select=(compact.select if compact else None),
                                 digest=(compact.digest if compact else None),
                                 prompt_for=(compact.handoff_prompt if compact else None),
                                 valid=(compact.looks_like_capsule if compact else None),
                                 confidential=confidential,
                                 scrub=(compact.redact_capsule
                                        if (compact and confidential) else None))
            except Exception as error:  # noqa: BLE001
                built = {"status": "error", "error": str(error)[:200]}
            entry["handoff"] = built.get("status")
            # Only close a session whose thread has actually been preserved.
            if built.get("status") == "ok":
                entry["closed"] = close_session(db, session["id"], now=now, reason="nightly-handoff")
            else:
                entry["closed"] = False
                log(f"  kept open (capsule {built.get('status')}): {session['id']}")
            done.append(entry)
        report["profiles"].append({"profile": profile.name, "sessions": done})

    os.environ["HERMES_HOME"] = str(home)
    out = home / "logs" / "nightly-handoff.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except OSError:
        pass
    handed = sum(1 for p in report["profiles"] for s in p["sessions"] if s.get("closed"))
    log(f"done: {handed} session(s) handed off and closed")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
