"""Get a TypeSafe key from a person into the secret store without an agent seeing it.

An agent runs ``jev setup-key``. This opens a one-time page served from this
machine only; the person pastes the key there; the page hands it straight to the
secret store and the Hermes lane files. The agent's view of the whole exchange is
one JSON line saying whether it worked: never the key, not even a prefix.

The key must never be pasted into a chat. That is the point of this module.
"""
from __future__ import annotations

import getpass
import html
import json
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs

from . import client, keystore

KEYS_URLS = {
    "typesafe": "https://console.typesafe.ai/settings/keys",
    "openrouter": "https://openrouter.ai/keys",
}
MAX_BODY = 4096

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect Jev</title>
<meta name="referrer" content="no-referrer">
<style>
:root{color-scheme:light dark;--bg:#f6f5f1;--fg:#17181a;--mut:#5d6067;--card:#fff;--line:#d9d7d0;--acc:#1d5fd1;--on:#fff;--ok:#19794a;--bad:#b3261e}
@media(prefers-color-scheme:dark){:root{--bg:#121315;--fg:#ecebe7;--mut:#a2a5ac;--card:#1b1d20;--line:#2e3136;--acc:#7aa7ff;--on:#0b1220;--ok:#5fd39a;--bad:#ff8a80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;
display:grid;place-items:center;min-height:100vh;padding:16px}
main{width:100%;max-width:460px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px}
h1{font-size:1.3rem;margin:0 0 6px}p{margin:0 0 14px;color:var(--mut)}a{color:var(--acc)}
label{display:block;font-weight:600;margin:18px 0 6px}
input{width:100%;padding:12px;border-radius:9px;border:1px solid var(--line);background:var(--bg);color:var(--fg);font:inherit}
button{margin-top:16px;width:100%;padding:12px;border:0;border-radius:9px;background:var(--acc);color:var(--on);font:600 1rem system-ui;cursor:pointer}
.note{font-size:.86rem;margin-top:18px}.ok{color:var(--ok)}.bad{color:var(--bad)}
</style></head><body><main>__BODY__</main></body></html>"""

_FORM = """<h1>Connect Jev</h1>
<p>Paste your __PROVIDER__ API key. It goes from this page straight into this computer's secret store.
Your AI agent never sees it.</p>
__ERROR__
<form method="post" autocomplete="off">
<label for="k">__PROVIDER__ API key</label>
<input id="k" name="key" type="password" required autofocus autocomplete="off" spellcheck="false">
<button type="submit">Save key</button></form>
<p class="note">No key yet? Create one at <a href="__KEYS__" target="_blank" rel="noreferrer noopener">the provider key page</a>.
This page is served only by your own machine and closes after one use.</p>"""

_DONE = """<h1 class="ok">Jev is connected</h1>
<p>The key was saved__VERIFIED__. You can close this tab and go back to your agent.</p>"""


def _render(body: str) -> bytes:
    return _PAGE.replace("__BODY__", body).encode("utf-8")


def _finish(key: str, provider: str, verify: bool, hermes: bool, hermes_home: Optional[Path]) -> Dict[str, Any]:
    verified: Optional[bool] = client.verify_key(key, provider=provider) if verify else None
    if verified is False:
        return {"status": "rejected", "reason": f"{provider} did not accept that key"}
    if provider == "typesafe":
        result = keystore.store(key, hermes=hermes, hermes_home=hermes_home)
    else:
        result = keystore.store(key, provider=provider, hermes=hermes, hermes_home=hermes_home)
    result.update({"status": "stored", "verified": verified})
    return result


def run_browser(
    *, host: str = "127.0.0.1", port: int = 0, timeout: float = 600.0, open_browser: bool = True,
    provider: str = "typesafe",
    verify: bool = True, hermes: bool = True, hermes_home: Optional[Path] = None,
) -> Dict[str, Any]:
    if provider not in KEYS_URLS:
        raise ValueError(f"unknown provider: {provider}")
    token = secrets.token_urlsafe(24)
    outcome: Dict[str, Any] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        server_version = "jev-setup"
        sys_version = ""

        def log_message(self, *args: Any) -> None:  # never log request lines or bodies
            return

        def _send(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _allowed(self) -> bool:
            # The unguessable path stops other local pages posting here; the Host check
            # stops a DNS-rebinding site from reaching a loopback server by name.
            if self.path.split("?", 1)[0] != "/" + token:
                return False
            if host in ("127.0.0.1", "localhost", "::1"):
                name = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
                return name in ("127.0.0.1", "localhost", "::1")
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._allowed() or done.is_set():
                self._send(404, _render("<h1>Not found</h1>"))
                return
            self._send(200, _render(_FORM.replace("__ERROR__", "").replace("__KEYS__", KEYS_URLS[provider]).replace("__PROVIDER__", provider.title())))

        def do_POST(self) -> None:  # noqa: N802
            if not self._allowed() or done.is_set():
                self._send(404, _render("<h1>Not found</h1>"))
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                self._send(400, _render("<h1>Bad request</h1>"))
                return
            fields = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            key = (fields.get("key") or [""])[0].strip()
            try:
                result = _finish(key, provider, verify, hermes, hermes_home)
            except ValueError as error:
                result = {"status": "rejected", "reason": str(error)}
            if result["status"] != "stored":
                message = f'<p class="bad">{html.escape(str(result["reason"]))}. Try again.</p>'
                self._send(200, _render(_FORM.replace("__ERROR__", message).replace("__KEYS__", KEYS_URLS[provider]).replace("__PROVIDER__", provider.title())))
                return
            note = f" and checked with {provider.title()}" if result.get("verified") else ""
            self._send(200, _render(_DONE.replace("__VERIFIED__", note)))
            outcome.update(result)
            done.set()

    class Server(ThreadingHTTPServer):
        def server_bind(self) -> None:
            # HTTPServer.server_bind resolves the machine's FQDN, a reverse-DNS lookup that can
            # stall for many seconds on some Macs. The name is never used here, so skip it.
            import socketserver
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = str(self.server_address[0]), int(self.server_address[1])

    server = Server((host, port), Handler)
    server.daemon_threads = True
    shown_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{shown_host}:{server.server_address[1]}/{token}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    opened = False
    if open_browser:
        try:
            opened = bool(webbrowser.open(url))
        except Exception:  # noqa: BLE001
            opened = False
    # The URL holds no secret; it is safe for an agent to relay to its person.
    print(json.dumps({"status": "waiting", "url": url, "browser_opened": opened,
                      "say": "Open this page on the computer running your agent and paste your TypeSafe key there."}),
          file=sys.stderr, flush=True)
    finished = done.wait(timeout)
    time.sleep(0.3)  # let the success page flush before the listener goes away
    server.shutdown()
    server.server_close()
    return outcome if finished else {"status": "timed_out"}


def run_tty(*, provider: str = "typesafe", verify: bool = True, hermes: bool = True, hermes_home: Optional[Path] = None) -> Dict[str, Any]:
    if provider not in KEYS_URLS:
        return {"status": "rejected", "reason": f"unknown provider: {provider}"}
    if not sys.stdin.isatty():
        return {"status": "rejected", "reason": "no terminal; use the browser flow"}
    key = getpass.getpass(f"{provider.title()} API key (hidden): ").strip()
    try:
        return _finish(key, provider, verify, hermes, hermes_home)
    except ValueError as error:
        return {"status": "rejected", "reason": str(error)}
