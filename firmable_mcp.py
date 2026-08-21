"""firmable mcp — drive the Firmable MCP server from the shell.

Firmable ships an MCP server (agents.firmable.com/mcp) whose tool set is richer
than the three documented REST endpoints. Normally you would only reach it from
inside an MCP client (Claude, ChatGPT, Cursor). This module speaks the protocol
directly — Streamable HTTP + JSON-RPC — so the same tools are available from a
terminal, a cron job, or any script that can shell out.

    firmable mcp login              # one-time browser sign-in (OAuth 2.1 + PKCE)
    firmable mcp status             # who you are signed in as, token expiry
    firmable mcp tools              # list every tool with its input schema
    firmable mcp call <tool> --arg key=value --arg other=value
    firmable mcp call <tool> --args '{"query":"..."}'
    firmable mcp raw resources/list # any JSON-RPC method, for poking around
    firmable mcp logout

Auth is Clerk-backed OAuth 2.1: authorization code + PKCE against a public
client, with a refresh token so you sign in once. Tokens are cached in
~/.config/firmable/mcp.json (0600) and refreshed automatically. Firmable's
advertised dynamic client registration endpoint returns 500, so the CLI uses the
published client ID and the callback port its docs register (53612).

Nothing here shares state with the REST commands: the REST side uses
FIRMABLE_API_KEY, this uses your Firmable user login.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

MCP_URL = os.environ.get("FIRMABLE_MCP_URL", "https://agents.firmable.com/mcp")
CLIENT_ID = os.environ.get("FIRMABLE_MCP_CLIENT_ID", "rkI2RHxS55F7qKp8")
CALLBACK_PORT = int(os.environ.get("FIRMABLE_MCP_CALLBACK_PORT", "53612"))
CALLBACK_HOST = os.environ.get("FIRMABLE_MCP_CALLBACK_HOST", "localhost")
PROTOCOL_VERSION = "2025-06-18"
SCOPES = "email profile user:org:read public_metadata offline_access"

TOKEN_PATH = Path(
    os.environ.get("FIRMABLE_MCP_TOKEN_FILE", Path.home() / ".config" / "firmable" / "mcp.json")
)

# Fallbacks if discovery is unreachable; both are confirmed live values.
FALLBACK_AS = {
    "authorization_endpoint": "https://agents.firmable.com/oauth/authorize",
    "token_endpoint": "https://clerk.firmable.com/oauth/token",
    "revocation_endpoint": "https://clerk.firmable.com/oauth/token/revoke",
}
RESOURCE = "https://agents.firmable.com"


class McpError(Exception):
    pass


# ---------------------------------------------------------------------------
# token cache
# ---------------------------------------------------------------------------

def load_tokens() -> dict:
    if not TOKEN_PATH.exists():
        return {}
    try:
        return json.loads(TOKEN_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_tokens(tokens: dict) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(TOKEN_PATH.parent, 0o700)
    tmp = TOKEN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_PATH)


# ---------------------------------------------------------------------------
# discovery + oauth
# ---------------------------------------------------------------------------

def _get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "firmable-cli/0.1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def discover() -> dict:
    """Resolve the authorization server metadata, per the MCP auth spec."""
    base = MCP_URL.split("/mcp")[0].rstrip("/")
    path = urllib.parse.urlsplit(MCP_URL).path.strip("/")
    for url in (
        f"{base}/.well-known/oauth-authorization-server/{path}",
        f"{base}/.well-known/oauth-authorization-server",
    ):
        try:
            meta = _get_json(url)
            if meta.get("token_endpoint"):
                return meta
        except (urllib.error.URLError, json.JSONDecodeError, OSError):
            continue
    return dict(FALLBACK_AS)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _form_post(url: str, fields: dict, timeout: int = 30) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            # Clerk sits behind Cloudflare, which 403s urllib's default
            # `Python-urllib/3.x` User-Agent. Identify as a normal product.
            "User-Agent": "firmable-cli/0.1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise McpError(f"token endpoint returned HTTP {e.code}: {body[:400]}")


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    captured: dict = {}

    def do_GET(self):  # noqa: N802 — stdlib naming
        parsed = urllib.parse.urlsplit(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        _CallbackHandler.captured = params
        ok = "code" in params
        body = (
            "<html><body style='font-family:system-ui;padding:3rem'>"
            + ("<h2>Firmable CLI connected</h2><p>You can close this tab and return to the terminal.</p>"
               if ok else
               f"<h2>Authorization failed</h2><pre>{params.get('error_description') or params.get('error') or 'no code returned'}</pre>")
            + "</body></html>"
        ).encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # silence the default stderr logging
        return


def do_login(args) -> None:
    meta = discover()
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(16))
    redirect_uri = f"http://{CALLBACK_HOST}:{args.port}/callback"

    query = {
        "response_type": "code",
        "client_id": args.client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
    }
    auth_url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode(query)

    _CallbackHandler.captured = {}
    try:
        server = http.server.HTTPServer(("127.0.0.1", args.port), _CallbackHandler)
    except OSError as e:
        raise McpError(
            f"cannot listen on 127.0.0.1:{args.port} ({e}).\n"
            f"  Port {CALLBACK_PORT} is the one Firmable registers for this client ID — "
            "free it up, or pass --port if you registered another."
        )
    server.timeout = 1

    print("Opening your browser to authorize the Firmable CLI.")
    print(f"If it does not open, paste this URL:\n\n{auth_url}\n")
    if not args.no_browser:
        webbrowser.open(auth_url)

    deadline = time.monotonic() + args.timeout
    while not _CallbackHandler.captured and time.monotonic() < deadline:
        server.handle_request()
    server.server_close()

    params = _CallbackHandler.captured
    if not params:
        raise McpError(f"timed out after {args.timeout}s waiting for the browser callback")
    if params.get("error"):
        raise McpError(f"authorization failed: {params.get('error_description') or params['error']}")
    if params.get("state") != state:
        raise McpError("state mismatch on the callback — aborting")

    payload = _form_post(meta["token_endpoint"], {
        "grant_type": "authorization_code",
        "code": params["code"],
        "redirect_uri": redirect_uri,
        "client_id": args.client_id,
        "code_verifier": verifier,
        "resource": RESOURCE,
    })
    _store(payload, args.client_id, meta)
    print(f"Signed in. Tokens cached in {TOKEN_PATH} (0600).")
    if not payload.get("refresh_token"):
        print("Note: no refresh token returned — you'll need to re-run `login` when this expires.")


def _store(payload: dict, client_id: str, meta: dict) -> dict:
    existing = load_tokens()
    tokens = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token") or existing.get("refresh_token"),
        "token_type": payload.get("token_type", "Bearer"),
        "scope": payload.get("scope", SCOPES),
        "expires_at": int(time.time()) + int(payload.get("expires_in", 3600)),
        "client_id": client_id,
        "token_endpoint": meta.get("token_endpoint", FALLBACK_AS["token_endpoint"]),
        "mcp_url": MCP_URL,
    }
    save_tokens(tokens)
    return tokens


def refresh(tokens: dict) -> dict:
    if not tokens.get("refresh_token"):
        raise McpError("access token expired and no refresh token cached — run: firmable mcp login")
    payload = _form_post(tokens.get("token_endpoint") or FALLBACK_AS["token_endpoint"], {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": tokens.get("client_id", CLIENT_ID),
        "resource": RESOURCE,
    })
    return _store(payload, tokens.get("client_id", CLIENT_ID),
                  {"token_endpoint": tokens.get("token_endpoint")})


def access_token(allow_refresh: bool = True) -> str:
    tokens = load_tokens()
    if not tokens.get("access_token"):
        raise McpError("not signed in — run: firmable mcp login")
    if allow_refresh and tokens.get("expires_at", 0) - 60 < time.time():
        tokens = refresh(tokens)
    return tokens["access_token"]


# ---------------------------------------------------------------------------
# streamable http transport
# ---------------------------------------------------------------------------

class McpSession:
    """One JSON-RPC session over MCP Streamable HTTP.

    Handles the session header the server hands back on initialize, the SSE
    framing it may reply with, and a single silent token refresh on a 401.
    """

    def __init__(self, url: str = MCP_URL, timeout: int = 120, verbose: bool = False):
        self.url = url
        self.timeout = timeout
        self.verbose = verbose
        self.session_id: str | None = None
        self._id = 0
        self._token = access_token()
        self._initialized = False

    # -- plumbing ---------------------------------------------------------
    def _headers(self) -> dict:
        h = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "User-Agent": "firmable-cli",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    @staticmethod
    def _parse_sse(text: str):
        """Pull the last JSON payload out of an SSE stream."""
        found = None
        for line in text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    found = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
        return found

    def _post(self, payload: dict, retry_auth: bool = True):
        body = json.dumps(payload).encode()
        if self.verbose:
            sys.stderr.write(f"-> {payload.get('method')} {self.url}\n")
        req = urllib.request.Request(self.url, data=body, method="POST", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                raw = resp.read().decode(errors="replace")
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if resp.status == 202 or not raw.strip():
                    return None
                if "text/event-stream" in ctype:
                    return self._parse_sse(raw)
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code == 401 and retry_auth:
                self._token = refresh(load_tokens())["access_token"]
                return self._post(payload, retry_auth=False)
            if e.code == 404 and self.session_id:
                # Server dropped the session — start a fresh one and replay.
                self.session_id, self._initialized = None, False
                self.initialize()
                return self._post(payload, retry_auth=False)
            raise McpError(f"MCP HTTP {e.code}: {detail[:500]}")
        except (socket.timeout, TimeoutError) as e:
            raise McpError(f"MCP timeout (>{self.timeout}s): {e}")
        except urllib.error.URLError as e:
            raise McpError(f"MCP network error: {e}")

    def request(self, method: str, params: dict | None = None):
        self._id += 1
        resp = self._post({"jsonrpc": "2.0", "id": self._id, "method": method,
                           "params": params or {}})
        if resp is None:
            raise McpError(f"{method}: empty response from server")
        if isinstance(resp, dict) and resp.get("error"):
            err = resp["error"]
            raise McpError(f"{method}: {err.get('message')} (code {err.get('code')})")
        return (resp or {}).get("result")

    def notify(self, method: str, params: dict | None = None) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # -- lifecycle --------------------------------------------------------
    def initialize(self) -> dict:
        if self._initialized:
            return {}
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "firmable-cli", "version": "0.1.0"},
        })
        self._initialized = True
        try:
            self.notify("notifications/initialized")
        except McpError:
            pass  # some servers 405 the notification; harmless
        return result or {}

    def list_tools(self) -> list:
        self.initialize()
        tools, cursor = [], None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self.request("tools/list", params) or {}
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict):
        self.initialize()
        return self.request("tools/call", {"name": name, "arguments": arguments})


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def render_tool_result(result) -> str:
    """MCP tool results are content blocks; unwrap the useful part."""
    if not isinstance(result, dict):
        return json.dumps(result, indent=2, ensure_ascii=False)
    if result.get("structuredContent") is not None:
        return json.dumps(result["structuredContent"], indent=2, ensure_ascii=False)
    parts = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text") or ""
            try:  # most servers hand back JSON as a text block
                parts.append(json.dumps(json.loads(text), indent=2, ensure_ascii=False))
            except (json.JSONDecodeError, ValueError):
                parts.append(text)
        else:
            parts.append(json.dumps(block, indent=2, ensure_ascii=False))
    if not parts:
        return json.dumps(result, indent=2, ensure_ascii=False)
    out = "\n".join(parts)
    if result.get("isError"):
        out = "tool reported an error:\n" + out
    return out


def _schema_summary(tool: dict) -> list[str]:
    schema = tool.get("inputSchema") or {}
    required = set(schema.get("required") or [])
    lines = []
    for field, spec in (schema.get("properties") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        ftype = spec.get("type") or ("enum" if spec.get("enum") else "any")
        if spec.get("enum"):
            ftype = f"{ftype}: {', '.join(map(str, spec['enum'][:8]))}"
        mark = "*" if field in required else " "
        desc = (spec.get("description") or "").strip().splitlines()
        lines.append(f"    {mark}{field} [{ftype}] {desc[0] if desc else ''}".rstrip())
    return lines


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def do_status(_args) -> None:
    tokens = load_tokens()
    if not tokens.get("access_token"):
        print(f"not signed in (no {TOKEN_PATH})\n  run: firmable mcp login")
        return
    left = tokens.get("expires_at", 0) - int(time.time())
    print(f"signed in           : yes")
    print(f"mcp url             : {tokens.get('mcp_url', MCP_URL)}")
    print(f"client id           : {tokens.get('client_id')}")
    print(f"scope               : {tokens.get('scope')}")
    print(f"access token expires: {'in %d min' % (left // 60) if left > 0 else 'expired'}")
    print(f"refresh token       : {'cached' if tokens.get('refresh_token') else 'none — re-login when expired'}")
    print(f"token file          : {TOKEN_PATH}")


def do_logout(_args) -> None:
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()
        print(f"removed {TOKEN_PATH}")
    else:
        print("nothing to remove")


def do_tools(args) -> None:
    session = McpSession(verbose=args.verbose)
    tools = session.list_tools()
    if args.json:
        print(json.dumps(tools, indent=2, ensure_ascii=False))
        return
    for tool in tools:
        print(f"\n{tool.get('name')}")
        desc = (tool.get("description") or "").strip()
        for line in desc.splitlines()[:4]:
            print(f"  {line}")
        schema_lines = _schema_summary(tool)
        if schema_lines:
            print("  arguments (* = required):")
            print("\n".join(schema_lines))
    print(f"\n{len(tools)} tools. Call one with: firmable mcp call <name> --arg key=value")


def _coerce(value: str):
    """Let --arg carry numbers, booleans and JSON without extra ceremony."""
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    if low == "null":
        return None
    if value.strip()[:1] in "[{":
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def do_call(args) -> None:
    arguments: dict = json.loads(args.args) if args.args else {}
    for pair in args.arg or []:
        if "=" not in pair:
            sys.exit(f"--arg expects key=value, got: {pair}")
        key, _, value = pair.partition("=")
        arguments[key] = _coerce(value)

    session = McpSession(verbose=args.verbose)
    if args.dry_run:
        print(json.dumps({"_dry_run": True, "tool": args.tool, "arguments": arguments}, indent=2))
        return
    result = session.call_tool(args.tool, arguments)
    text = json.dumps(result, indent=2, ensure_ascii=False) if args.raw else render_tool_result(result)
    if args.out:
        Path(args.out).write_text(text)
        sys.stderr.write(f"wrote {len(text):,} bytes to {args.out}\n")
    else:
        print(text)


def do_raw(args) -> None:
    session = McpSession(verbose=args.verbose)
    params = json.loads(args.params) if args.params else {}
    result = session.request(args.method, params)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="firmable mcp", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="sub", required=True)

    p = sub.add_parser("login", help="Browser sign-in (OAuth 2.1 + PKCE)")
    p.add_argument("--client-id", default=CLIENT_ID, help=f"OAuth client id (default {CLIENT_ID})")
    p.add_argument("--port", type=int, default=CALLBACK_PORT,
                   help=f"Loopback callback port (default {CALLBACK_PORT} — the one Firmable registers)")
    p.add_argument("--timeout", type=int, default=300, help="Seconds to wait for the callback")
    p.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening it")
    p.set_defaults(func=do_login)

    p = sub.add_parser("status", help="Show sign-in state and token expiry")
    p.set_defaults(func=do_status)

    p = sub.add_parser("logout", help="Delete the cached tokens")
    p.set_defaults(func=do_logout)

    p = sub.add_parser("tools", help="List the MCP tools and their input schemas")
    p.add_argument("--json", action="store_true", help="Emit the raw tool definitions")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=do_tools)

    p = sub.add_parser("call", help="Call one MCP tool")
    p.add_argument("tool")
    p.add_argument("--arg", action="append", help="key=value (repeatable; JSON values allowed)")
    p.add_argument("--args", help="Full arguments object as JSON")
    p.add_argument("--out", help="Write the result to this file")
    p.add_argument("--raw", action="store_true", help="Print the full MCP result envelope")
    p.add_argument("--dry-run", action="store_true", help="Show the call without sending it")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=do_call)

    p = sub.add_parser("raw", help="Send any JSON-RPC method (resources/list, prompts/list, ...)")
    p.add_argument("method")
    p.add_argument("--params", help="Params object as JSON")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=do_raw)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except McpError as e:
        sys.exit(f"firmable mcp: {e}")
