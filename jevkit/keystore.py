"""Where the TypeSafe key lives, and how to read or store it without showing it.

Resolution order (first hit wins):
  1. ``TYPESAFE_API_KEY`` in the process environment
  2. the OS secret store (macOS Keychain, or ``secret-tool`` on Linux)
  3. ``~/.config/jev/credentials`` (mode 0600), the portable fallback

Nothing in this module prints, logs, or returns the key to a caller that did not
ask for it by name, and ``describe()`` only ever reports presence and length.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

PROVIDERS = {
    "typesafe": {"env": "TYPESAFE_API_KEY", "service": "Hermes TypeSafe API"},
    "openrouter": {"env": "OPENROUTER_API_KEY", "service": "Hermes OpenRouter API"},
}
ENV_VAR = PROVIDERS["typesafe"]["env"]
KEYCHAIN_SERVICE = PROVIDERS["typesafe"]["service"]
KEYCHAIN_ACCOUNT = ENV_VAR
_SECURITY = "/usr/bin/security"


def credentials_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "jev" / "credentials"


def looks_like_key(value: str) -> bool:
    """Cheap shape check so an obvious paste mistake is caught before storing."""
    value = value.strip()
    return 20 <= len(value) <= 512 and not any(c.isspace() for c in value) and value.isprintable()


# ── read ─────────────────────────────────────────────────────────────────────

def _from_keychain(provider: str = "typesafe") -> Optional[str]:
    spec = PROVIDERS[provider]
    service, account = spec["service"], spec["env"]
    if sys.platform == "darwin" and os.path.exists(_SECURITY):
        cmd = [_SECURITY, "find-generic-password", "-w", "-s", service, "-a", account]
    elif shutil.which("secret-tool"):
        cmd = ["secret-tool", "lookup", "service", service, "account", account]
    else:
        return None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=False)
    except Exception:  # noqa: BLE001 - a broken secret store must never crash a caller
        return None
    value = proc.stdout.strip()
    return value if proc.returncode == 0 and value else None


def _from_file(provider: str = "typesafe") -> Optional[str]:
    env_var = PROVIDERS[provider]["env"]
    path = credentials_file()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(env_var + "="):
                return line.split("=", 1)[1].strip() or None
    except OSError:
        return None
    return None


def provider_names() -> List[str]:
    return list(PROVIDERS)


def resolve(provider: Optional[str] = None) -> Optional[str]:
    """Resolve a provider key; TypeSafe wins when no provider is specified."""
    names = [provider] if provider else ["typesafe", "openrouter"]
    for name in names:
        if name not in PROVIDERS:
            raise ValueError(f"unknown provider: {name}")
        env_var = PROVIDERS[name]["env"]
        value = (os.environ.get(env_var) or "").strip() or _from_keychain(name) or _from_file(name)
        if value:
            return value
    return None


def resolve_provider(provider: Optional[str] = None) -> Optional[str]:
    names = [provider] if provider else ["typesafe", "openrouter"]
    for name in names:
        if resolve(name):
            return name
    return None


def source(provider: Optional[str] = None) -> str:
    name = provider or resolve_provider()
    if not name:
        return "absent"
    env_var = PROVIDERS[name]["env"]
    if (os.environ.get(env_var) or "").strip():
        return "environment"
    if _from_keychain(name):
        return "os-secret-store"
    if _from_file(name):
        return "credentials-file"
    return "absent"


def describe(provider: Optional[str] = None) -> Dict[str, object]:
    name = provider or resolve_provider()
    key = resolve(name) if name else None
    return {"present": bool(key), "provider": name, "source": source(name), "length": len(key) if key else 0}


# ── write ────────────────────────────────────────────────────────────────────

def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename, so a crash can never leave a half-written .env.
    temp = path.with_name(path.name + ".jev-tmp")
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temp, path)


def upsert_env_file(path: Path, value: str, provider: str = "typesafe") -> None:
    """Set the selected provider variable in a dotenv file."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    env_var = PROVIDERS[provider]["env"]
    entry = f"{env_var}={value}"
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith(env_var + "="):
            lines[index] = entry
            replaced = True
    if not replaced:
        lines.append(entry)
    _write_private(path, "\n".join(lines) + "\n")


def _store_keychain(value: str, provider: str = "typesafe") -> bool:
    service, account = PROVIDERS[provider]["service"], PROVIDERS[provider]["env"]
    if sys.platform == "darwin" and os.path.exists(_SECURITY):
        # `security` has no stdin mode for the secret, so it is briefly an argv entry
        # of a child we own. The alternative (no secret store at all) is worse.
        cmd = [_SECURITY, "add-generic-password", "-U", "-s", service, "-a", account, "-w", value]
        stdin = None
    elif shutil.which("secret-tool"):
        cmd = ["secret-tool", "store", "--label", service, "service", service, "account", account]
        stdin = value
    else:
        return False
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=15, check=False)
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == 0


def hermes_env_files(hermes_home: Optional[Path] = None) -> List[Path]:
    """Every dotenv a Hermes lane reads. A lane resolves ${VAR} from its OWN .env."""
    home = hermes_home or Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    if not home.is_dir():
        return []
    files = [home / ".env"]
    profiles = home / "profiles"
    if profiles.is_dir():
        files += sorted(p / ".env" for p in profiles.iterdir() if p.is_dir() and not p.name.startswith("."))
    return files


def store(value: str, hermes: bool = True, hermes_home: Optional[Path] = None, provider: str = "typesafe") -> Dict[str, object]:
    """Persist a provider key. Returns where it went, never the key itself."""
    value = value.strip()
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    if not looks_like_key(value):
        raise ValueError("that does not look like an API key")
    written: List[str] = []
    if _store_keychain(value, provider):
        written.append("os-secret-store")
    else:
        upsert_env_file(credentials_file(), value, provider)
        written.append(str(credentials_file()))
    lanes = 0
    if hermes:
        for env_file in hermes_env_files(hermes_home):
            upsert_env_file(env_file, value, provider)
            lanes += 1
    return {"stored_in": written, "hermes_env_files": lanes, "length": len(value)}
