#!/usr/bin/env python3
"""Refuse to ship secrets or machine-specific paths. Run before every push."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "api key": re.compile(r"\b(apikey_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35})\b"),
    "home path": re.compile(r"/Users/[a-z][a-z0-9_-]+/|/home/[a-z][a-z0-9_-]+/"),
    "private address": re.compile(r"\b(100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+|192\.168\.\d+\.\d+|[a-z0-9-]+\.ts\.net)\b"),
}
ALLOW = {("tests/test_jevkit.py", "api key")}  # the fake key built at runtime never matches; listed for clarity

_git = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"],
                      cwd=ROOT, capture_output=True, text=True)
files = _git.stdout.split()
# A guard that cannot tell "clean" from "did not run" is worse than no guard: outside a
# git checkout this printed "clean: 0 files" and exited 0, giving a green light to a
# check that scanned nothing. The repo is distributed as a zip, so that case is real.
if _git.returncode != 0 or not files:
    sys.exit("check_release: nothing was scanned (not a git checkout, or git failed). "
             "This is not a pass.")
problems = []
for name in files:
    path = ROOT / name
    if not path.is_file() or path.suffix in {".png", ".jpg", ".gif", ".mp4"}:
        continue
    text = path.read_text(encoding="utf-8", errors="replace")
    for label, pattern in PATTERNS.items():
        if (name, label) in ALLOW or name == "scripts/check_release.py":
            continue
        for match in pattern.finditer(text):
            problems.append(f"{name}: {label}: {match.group(0)[:24]}…")
print("\n".join(problems) if problems else f"clean: {len(files)} files")
sys.exit(1 if problems else 0)
