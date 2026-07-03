"""Shared base for Google hosted-MCP clients with a REST fallback.

Google's hosted MCP servers (e.g. ``calendarmcp.googleapis.com``,
``gmailmcp.googleapis.com``) are currently access-gated: even with a valid,
fully-scoped OAuth token they answer tool *executions* with ``PERMISSION_DENIED``
for projects that aren't allowlisted (``tools/list`` still works). This base
handles the OAuth flow + MCP call, and transparently falls back to the plain
Google REST API using the same OAuth token when the MCP denies access or the
``mcp`` extra isn't installed. Concrete services subclass ``GoogleMcpClient`` and
provide REST handlers; MCP stays primary and resumes automatically once the
project is granted access.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import stat
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode, urlparse

import aiohttp
from aiohttp import web

try:
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider, TokenStorage
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
except ImportError:  # pragma: no cover - optional dependency guard
    ClientSession = None
    OAuthClientProvider = None
    TokenStorage = object
    streamablehttp_client = None
    OAuthClientInformationFull = None
    OAuthClientMetadata = None
    OAuthToken = None

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


@dataclass(frozen=True)
class GoogleMcpConfig:
    """Per-service configuration, all overridable via ``{PREFIX}_*`` env vars."""

    prefix: str          # e.g. "CALENDAR_MCP" / "GMAIL_MCP"
    label: str           # human label, e.g. "Calendar" / "Gmail"
    default_mcp_url: str  # hosted MCP endpoint
    api_base: str        # REST API base, e.g. https://www.googleapis.com/calendar/v3
    default_scopes: str
    token_filename: str  # under ~/.wactorz/
    login_tool: str      # a tool whose execution requires auth (used to trigger OAuth)
    client_name: str = "Wactorz"
    default_redirect: str = "http://localhost:8765/oauth/callback"

    def _env(self, suffix: str, default: str = "") -> str:
        return os.getenv(f"{self.prefix}_{suffix}", default)

    @property
    def key(self) -> str:
        return self.prefix.lower()  # "calendar_mcp" / "gmail_mcp"

    def mcp_url(self) -> str:
        return self._env("URL", self.default_mcp_url).rstrip("/")

    def redirect_uri(self) -> str:
        return self._env("REDIRECT_URI", self.default_redirect)

    def scopes(self) -> str:
        return self._env("SCOPES", self.default_scopes)

    def client_id(self) -> str:
        return self._env("CLIENT_ID")

    def client_secret(self) -> str:
        return self._env("CLIENT_SECRET")

    def token(self) -> str:
        return self._env("TOKEN")

    def authorization(self) -> str:
        return self._env("AUTHORIZATION")

    def token_file(self) -> str:
        return self._env("TOKEN_FILE") or str(Path.home() / ".wactorz" / self.token_filename)

    def headers(self) -> dict[str, str]:
        auth = self.authorization()
        if not auth and self.token():
            auth = f"Bearer {self.token()}"
        return {"Authorization": auth} if auth else {}

    def config_status(self) -> dict[str, Any]:
        client_id = self.client_id()
        return {
            f"{self.key}_url": self.mcp_url() or None,
            f"{self.key}_auth": bool(client_id or self.token() or self.authorization()),
            f"{self.key}_oauth_client": bool(client_id),
            f"{self.key}_redirect_uri": self.redirect_uri() if client_id else None,
            f"{self.key}_token_file": self.token_file() if client_id else None,
        }


class GoogleRestError(Exception):
    """A Google REST API call returned an error status."""


def format_mcp_content(result: Any) -> str:
    if getattr(result, "structuredContent", None) is not None:
        return json.dumps(result.structuredContent, indent=2)
    content = getattr(result, "content", None)
    if not content:
        return str(result)
    parts = []
    for item in content:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
        else:
            try:
                parts.append(item.model_dump_json(indent=2))
            except Exception:
                parts.append(str(item))
    return "\n".join(parts)


def rest_error_message(data: Any) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
    return str(data)


def _looks_permission_denied(text: str) -> bool:
    low = text.lower()
    return "does not have permission" in low or "permission_denied" in low


def _looks_mcp_unavailable(text: str) -> bool:
    low = text.lower()
    return "mcp error" in low or "package is required" in low


class _GoogleTokenStorage(TokenStorage):
    def __init__(self, config: GoogleMcpConfig):
        self.config = config
        self.path = Path(config.token_file()).expanduser()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:
            logger.warning("Could not read %s MCP token file %s", self.config.label, self.path)
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        new = tokens.model_dump(mode="json", exclude_none=True)
        # Google omits refresh_token from *refresh* responses; don't let that wipe
        # the one from the initial consent, or all future refreshes break.
        existing = data.get("tokens", {})
        if not new.get("refresh_token") and existing.get("refresh_token"):
            new["refresh_token"] = existing["refresh_token"]
        data["tokens"] = new
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        client_id = self.config.client_id()
        client_secret = self.config.client_secret()
        if client_id and client_secret:
            return OAuthClientInformationFull(
                client_id=client_id,
                client_secret=client_secret,
                token_endpoint_auth_method="client_secret_post",
                redirect_uris=[self.config.redirect_uri()],
            )
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)


def _make_redirect_handler(config: GoogleMcpConfig):
    async def _open_oauth_browser(url: str) -> None:
        print(
            f"\n{config.label} MCP OAuth required. Open this URL if the browser does not open:\n{url}\n",
            file=sys.stderr,
        )
        try:
            webbrowser.open(url)
        except Exception:
            pass

    return _open_oauth_browser


def _make_callback_handler(config: GoogleMcpConfig):
    async def _wait_for_oauth_callback() -> tuple[str, str | None]:
        parsed = urlparse(config.redirect_uri())
        host = parsed.hostname or "localhost"
        port = parsed.port or 8765
        path = parsed.path or "/oauth/callback"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[str, str | None]] = loop.create_future()

        async def callback(request: web.Request) -> web.Response:
            if request.path != path:
                return web.Response(status=404, text="Not found")
            code = request.query.get("code", "")
            state = request.query.get("state")
            if not future.done():
                future.set_result((code, state))
            return web.Response(
                text=f"{config.label} MCP authentication complete. You can close this window."
            )

        app = web.Application()
        app.router.add_get(path, callback)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        try:
            return await future
        finally:
            await runner.cleanup()

    return _wait_for_oauth_callback


def _make_noninteractive_handler(config: GoogleMcpConfig):
    async def _handler(*_args, **_kwargs):
        raise RuntimeError(
            f"{config.label} MCP needs interactive authorization; run login() once first"
        )

    return _handler


def build_auth(config: GoogleMcpConfig, interactive: bool = False):
    """Build an OAuth provider.

    With ``interactive=False`` (the default, used for normal tool calls) the
    provider still refreshes a stored token silently, but if it would need a
    *new* browser consent it raises instead of blocking — so a server-side agent
    fails fast to the REST fallback rather than hanging on an OAuth window.
    ``interactive=True`` (used by :meth:`GoogleMcpClient.login`) runs the full
    browser flow to mint a token.
    """
    client_id = config.client_id()
    client_secret = config.client_secret()
    if not (client_id and client_secret):
        return None
    metadata = OAuthClientMetadata(
        redirect_uris=[config.redirect_uri()],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=config.scopes(),
        client_name=config.client_name,
    )
    if interactive:
        redirect_handler = _make_redirect_handler(config)
        callback_handler = _make_callback_handler(config)
    else:
        redirect_handler = _make_noninteractive_handler(config)
        callback_handler = _make_noninteractive_handler(config)
    return OAuthClientProvider(
        server_url=config.mcp_url(),
        client_metadata=metadata,
        storage=_GoogleTokenStorage(config),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


def mcp_timeout() -> float:
    try:
        return float(os.getenv("GOOGLE_MCP_TIMEOUT", "20"))
    except ValueError:
        return 20.0


RestHandler = Callable[[dict], Awaitable[str]]


class GoogleMcpClient:
    """Base client: hosted MCP first, plain REST fallback on permission-denied."""

    def __init__(self, config: GoogleMcpConfig):
        self.config = config

    # ── Subclass hook ──────────────────────────────────────────────────────
    def _rest_handlers(self) -> dict[str, RestHandler]:
        """Map MCP tool name → async REST handler. Subclasses override."""
        return {}

    # ── Public API ─────────────────────────────────────────────────────────
    async def call_tool(self, tool_name: str, arguments: dict | None = None) -> str:
        arguments = arguments or {}
        try:
            result = await asyncio.wait_for(
                self._call_mcp(tool_name, arguments), timeout=mcp_timeout()
            )
        except asyncio.TimeoutError:
            result = f"{self.config.label} MCP error: timed out"
        if _looks_permission_denied(result) or _looks_mcp_unavailable(result):
            fallback = await self._call_rest(tool_name, arguments)
            if fallback is not None:
                logger.info(
                    "%s MCP unavailable; served '%s' via REST fallback",
                    self.config.label,
                    tool_name,
                )
                return fallback
        return result

    async def _call_mcp(self, tool_name: str, arguments: dict, interactive: bool = False) -> str:
        if ClientSession is None or streamablehttp_client is None:
            return "The 'mcp' package is required. Install with: pip install wactorz[mcp]"
        try:
            auth = build_auth(self.config, interactive=interactive)
            async with streamablehttp_client(
                self.config.mcp_url(),
                headers={} if auth else self.config.headers(),
                auth=auth,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    return format_mcp_content(result)
        except Exception as exc:
            return f"{self.config.label} MCP error: {exc}"

    async def list_tools(self, interactive: bool = False) -> str:
        if ClientSession is None or streamablehttp_client is None:
            return "The 'mcp' package is required. Install with: pip install wactorz[mcp]"
        try:
            auth = build_auth(self.config, interactive=interactive)
            async with streamablehttp_client(
                self.config.mcp_url(),
                headers={} if auth else self.config.headers(),
                auth=auth,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    return "\n".join(
                        f"{tool.name}: {tool.description or ''}".rstrip()
                        for tool in tools.tools
                    )
        except Exception as exc:
            return f"{self.config.label} MCP error: {exc}"

    def _store_token_response(self, td: dict) -> None:
        path = Path(self.config.token_file()).expanduser()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        tokens = data.get("tokens", {}) or {}
        if td.get("access_token"):
            tokens["access_token"] = td["access_token"]
        if td.get("refresh_token"):
            tokens["refresh_token"] = td["refresh_token"]
        if td.get("scope"):
            tokens["scope"] = td["scope"]
        tokens["token_type"] = td.get("token_type", "Bearer")
        data["tokens"] = tokens
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            logger.debug("Could not store %s token", self.config.label)

    async def authorize_direct(self, scopes: str | None = None) -> str:
        """Mint a token via a direct Google OAuth flow (bypassing the MCP server).

        Lets us request exactly the scopes we want for the REST fallback — e.g. a
        Gmail token *without* ``gmail.metadata`` (which otherwise blocks free-text
        ``q`` search) — and guarantees a refresh token (``access_type=offline`` +
        ``prompt=consent``).
        """
        client_id = self.config.client_id()
        client_secret = self.config.client_secret()
        if not (client_id and client_secret):
            return f"{self.config.label}: no OAuth client configured"
        scopes = scopes or self.config.scopes()
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        redirect = self.config.redirect_uri()
        auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "scope": scopes,
            "state": secrets.token_urlsafe(16),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        })
        wait_task = asyncio.create_task(_make_callback_handler(self.config)())
        await asyncio.sleep(0.4)  # let the callback server bind before opening the browser
        await _make_redirect_handler(self.config)(auth_url)
        code, _state = await wait_task
        if not code:
            return f"{self.config.label}: authorization was cancelled"
        async with aiohttp.ClientSession() as sess:
            async with sess.post(GOOGLE_TOKEN_URL, data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect,
                "code_verifier": verifier,
            }) as resp:
                token_data = await resp.json()
                if resp.status != 200:
                    return f"{self.config.label} token exchange failed: {rest_error_message(token_data)}"
        self._store_token_response(token_data)
        return f"{self.config.label}: authorized (direct REST)."

    async def login(self) -> str:
        """Run the interactive browser OAuth flow once to mint/refresh a token.

        Calls a real tool (execution requires auth, unlike ``tools/list`` which
        some Google MCP servers serve unauthenticated), so the OAuth flow fires
        and the token is stored. The tool's own result may still be
        ``PERMISSION_DENIED`` if the project is gated — that's fine, the token is
        what we're after (the REST fallback uses it).
        """
        result = await self._call_mcp(self.config.login_tool, {}, interactive=True)
        if self._access_token():
            if _looks_permission_denied(result):
                return f"{self.config.label}: authorized. (MCP is gated; REST fallback will be used.)"
            return f"{self.config.label}: authorized."
        return f"{self.config.label} login did not complete: {result}"

    # ── REST fallback plumbing ─────────────────────────────────────────────
    async def _call_rest(self, tool_name: str, arguments: dict) -> str | None:
        handler = self._rest_handlers().get(tool_name)
        if handler is None:
            return None
        try:
            return await handler(arguments)
        except GoogleRestError as exc:
            return f"{self.config.label} error: {exc}"
        except Exception as exc:  # transport/auth failure — let the MCP error stand
            logger.warning("%s REST fallback failed: %s", self.config.label, exc)
            return None

    def _stored_tokens(self) -> dict:
        path = Path(self.config.token_file()).expanduser()
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("tokens", {}) or {}
        except Exception:
            return {}

    def _save_access_token(self, access_token: str) -> None:
        path = Path(self.config.token_file()).expanduser()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data.setdefault("tokens", {})["access_token"] = access_token
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            logger.debug("Could not persist refreshed %s token", self.config.label)

    def _access_token(self) -> str | None:
        return self.config.token() or self._stored_tokens().get("access_token")

    async def _refresh_access_token(self) -> str | None:
        tokens = self._stored_tokens()
        refresh = tokens.get("refresh_token")
        client_id = self.config.client_id()
        client_secret = self.config.client_secret()
        if not (refresh and client_id and client_secret):
            return None
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        async with aiohttp.ClientSession() as sess:
            async with sess.post(GOOGLE_TOKEN_URL, data=payload) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        access_token = data.get("access_token")
        if access_token:
            self._save_access_token(access_token)
        return access_token

    async def _http(self, method: str, url: str, token: str, params, json_body):
        headers = {"Authorization": f"Bearer {token}"}
        async with aiohttp.ClientSession() as sess:
            async with sess.request(
                method, url, headers=headers, params=params, json=json_body
            ) as resp:
                if resp.status == 204:
                    return resp.status, {}
                try:
                    data = await resp.json()
                except Exception:
                    data = {"raw": await resp.text()}
                return resp.status, data

    async def _rest_request(self, method: str, path: str, params=None, json_body=None):
        token = self._access_token()
        if not token:
            raise GoogleRestError(f"Not authenticated for Google {self.config.label}")
        url = path if path.startswith("http") else f"{self.config.api_base}{path}"
        status, data = await self._http(method, url, token, params, json_body)
        if status == 401:
            refreshed = await self._refresh_access_token()
            if refreshed:
                status, data = await self._http(method, url, refreshed, params, json_body)
        return status, data
