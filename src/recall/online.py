"""recall as an online MCP server: the same tools over Streamable HTTP, behind a sign-in.
The same server also answers the recall app's web API (api.py), behind the same sign-in.

`recall mcp-online` listens on a local port; a tunnel publishes it at RECALL_ONLINE_URL.
recall is only the OAuth resource server here. It issues no tokens and holds no client
secrets: an authorization server (Descope) signs the user in, registers the clients and
issues access tokens, and every request must carry one. For each token this module checks

- the signature, against the authorization server's published keys,
- the issuer, the audience (this server's public URL) and the expiry,
- that the signed-in user is on the allow-list (RECALL_ONLINE_SUBJECTS), so a token the
  authorization server issued to anyone else is refused,
- and the permissions (scopes), once for the connection and again per tool.

Agents that cannot sign in through a browser (a Hermes in the cloud) use an agent key
instead: a long random string the agent made itself, of which this server knows only the
SHA-256 (RECALL_AGENT_KEYS="cloud:<hex>"). An agent key may search and save, but never open the
vault or forget anything; those stay with the signed-in owner. The same check guards POST
/serve, the per-turn memory lookup a Hermes memory provider makes (`recall serve` over HTTPS).

Tools that would let a caller reach this machine's files or local addresses are left out
(see TOOLS in mcp_server.py). The vault tools exist only here: the allow-listed owner can list,
open and add encrypted vault entries, with the key given to this server as RECALL_VAULT_SECRET.
"""

from __future__ import annotations

import functools
import hashlib
import json
import hmac
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import anyio
import httpx
import jwt
from starlette.requests import Request
from starlette.responses import JSONResponse
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from . import api, db, mcp_server, serve

log = logging.getLogger("recall.online")

SCOPES = [mcp_server.READ_SCOPE, mcp_server.WRITE_SCOPE, mcp_server.FORGET_SCOPE]
ALGORITHMS = ["RS256"]
# Descope's clock can run a second or two ahead of ours. Without slack, a token checked the moment
# it is issued (the app's sign-in does exactly that) fails as "not yet valid (iat)".
CLOCK_SKEW_S = 30
USER_AGENT = "recall-online"
# What a memory provider may ask over HTTPS: everything `recall serve` answers except `store`
# (events for a stream-less instance) and `open_items` (the homepage's, not an agent's).
ONLINE_OPS = frozenset({"ping", "context", "profile", "status", "turn"})
WRITE_OPS = frozenset({"turn"})
AGENT_PREFIX = "rk_"        # an agent key; anything else is a sign-in token from the authorization server


@dataclass(frozen=True)
class OnlineSettings:
    url: str                     # the public address of the MCP endpoint, e.g. https://name.example/mcp
    issuer: str                  # the authorization server's issuer URL
    subjects: frozenset[str]     # user ids (the token's `sub`) allowed to use this server
    host: str = "127.0.0.1"
    port: int = 8791
    jwks_url: str = ""           # where the signing keys are; discovered from the issuer when empty
    # Host names a proxy in front may send instead of the public one (RECALL_ONLINE_HOSTS),
    # e.g. "container" behind a Cloudflare Worker. Requests for any other host are refused.
    extra_hosts: tuple[str, ...] = ()
    # Agents allowed in with a key instead of a sign-in: (name, SHA-256 of the key in hex).
    agent_keys: tuple[tuple[str, str], ...] = ()

    @property
    def path(self) -> str:
        return urlsplit(self.url).path or "/"

    @property
    def public_host(self) -> str:
        return urlsplit(self.url).netloc

    @property
    def public_origin(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"


def load_settings(env: Mapping[str, str] = os.environ) -> OnlineSettings:
    """Read the RECALL_ONLINE_* settings; exits with what is missing."""
    url = env.get("RECALL_ONLINE_URL", "").strip()
    issuer = env.get("RECALL_ONLINE_ISSUER", "").strip().rstrip("/")
    subjects = frozenset(s.strip() for s in env.get("RECALL_ONLINE_SUBJECTS", "").split(",") if s.strip())
    missing = [name for name, value in (
        ("RECALL_ONLINE_URL", url), ("RECALL_ONLINE_ISSUER", issuer), ("RECALL_ONLINE_SUBJECTS", subjects),
    ) if not value]
    if missing:
        raise SystemExit(f"recall mcp-online needs {', '.join(missing)} (see the README, 'Online')")
    parts = urlsplit(url)
    local = parts.hostname in ("127.0.0.1", "localhost")
    if parts.scheme != "https" and not local:
        raise SystemExit("RECALL_ONLINE_URL must be an https address")
    if not parts.netloc or parts.query or parts.fragment:
        raise SystemExit("RECALL_ONLINE_URL must be a plain address such as https://name.example/mcp")
    return OnlineSettings(
        url=url, issuer=issuer, subjects=subjects,
        host=env.get("RECALL_ONLINE_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=int(env.get("RECALL_ONLINE_PORT", "8791")),
        jwks_url=env.get("RECALL_ONLINE_JWKS_URL", "").strip(),
        extra_hosts=tuple(h.strip() for h in env.get("RECALL_ONLINE_HOSTS", "").split(",") if h.strip()),
        agent_keys=agent_keys(env.get("RECALL_AGENT_KEYS", "")),
    )


def agent_keys(text: str) -> tuple[tuple[str, str], ...]:
    """Parse "name:sha256hex,name2:sha256hex"; exits on an entry that is not a SHA-256."""
    found = []
    for entry in filter(None, (e.strip() for e in text.split(","))):
        name, _, digest = entry.partition(":")
        name, digest = name.strip(), digest.strip().lower()
        if not name or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise SystemExit("RECALL_AGENT_KEYS entries look like name:<64 hex characters>, the SHA-256 of the key")
        found.append((name, digest))
    return tuple(found)


def agent_for(token: str, settings: OnlineSettings) -> AccessToken | None:
    """The access an agent key gives, or None when the key is not one this server knows."""
    if not token.startswith(AGENT_PREFIX) or len(token) < 32:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    for name, known in settings.agent_keys:
        if hmac.compare_digest(digest, known):
            # Every scope, so the connection opens; the tools an agent may not use refuse it themselves.
            return AccessToken(token=token, client_id=f"agent:{name}", scopes=list(SCOPES),
                               resource=settings.url, subject=f"agent:{name}", claims={"agent": name})
    log.info("refused an agent key no entry in RECALL_AGENT_KEYS matches")
    return None


def is_agent(access: AccessToken | None) -> bool:
    return bool(access and (access.claims or {}).get("agent"))


def scopes_of(claims: Mapping) -> list[str]:
    """The permissions a token carries, whichever of the usual claims holds them."""
    found: list[str] = []
    for name in ("scope", "scp", "scopes"):
        value = claims.get(name)
        parts = value.split() if isinstance(value, str) else value if isinstance(value, list) else []
        found += [p for p in parts if isinstance(p, str) and p not in found]
    return found


def access_for(claims: Mapping, token: str, settings: OnlineSettings) -> AccessToken | None:
    """Turn verified claims into an access token, or None when the user is not allowed.
    The signature, issuer, audience and expiry are already checked by then."""
    subject = claims.get("sub")
    if not isinstance(subject, str) or subject not in settings.subjects:
        # The id is logged so the owner can add their own after a first sign-in; it is not a secret.
        log.warning("refused a valid token: user %r is not on the allow-list (RECALL_ONLINE_SUBJECTS)", subject)
        return None
    client = claims.get("azp") or claims.get("client_id") or ""
    return AccessToken(
        token=token, client_id=str(client), scopes=scopes_of(claims), expires_at=claims.get("exp"),
        resource=settings.url, subject=subject, claims={"iss": claims.get("iss")},
    )


def discover_jwks_url(issuer: str) -> str:
    """The signing-key address the authorization server publishes for itself."""
    resp = httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=15, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    url = resp.json().get("jwks_uri")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise RuntimeError("the authorization server publishes no usable jwks_uri")
    return url


class JwtVerifier:
    """Checks an access token against the authorization server's keys and this server's rules.
    `key_for` returns the public key for a token; by default it looks the key up (and caches it)
    at the issuer's published address."""

    def __init__(self, settings: OnlineSettings, key_for: Callable[[str], object] | None = None):
        self.settings = settings
        self._key_for = key_for
        self._jwks: jwt.PyJWKClient | None = None

    def _signing_key(self, token: str):
        if self._key_for is not None:
            return self._key_for(token)
        if self._jwks is None:
            url = self.settings.jwks_url or discover_jwks_url(self.settings.issuer)
            # Named client: Descope answers 403 to Python's default "Python-urllib" user agent.
            self._jwks = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600, headers={"User-Agent": USER_AGENT})
        return self._jwks.get_signing_key_from_jwt(token).key

    def check(self, token: str) -> AccessToken | None:
        if token.startswith(AGENT_PREFIX):
            return agent_for(token, self.settings)
        try:
            claims = jwt.decode(
                token, self._signing_key(token), algorithms=ALGORITHMS,
                audience=self.settings.url, issuer=self.settings.issuer, leeway=CLOCK_SKEW_S,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWKClientError as exc:  # the keys could not be fetched: our problem, not the token's
            log.warning("could not fetch the signing keys: %s", exc)
            return None
        except jwt.PyJWTError as exc:
            log.info("refused a token: %s", type(exc).__name__)
            return None
        except Exception as exc:  # the key lookup failed (network, bad metadata): refuse, do not crash
            log.warning("could not check a token: %s", exc)
            return None
        return access_for(claims, token, self.settings)

    async def verify_token(self, token: str) -> AccessToken | None:
        return await anyio.to_thread.run_sync(self.check, token)


def owner_only(fn: Callable[..., str], scope: str) -> bool:
    """Tools an agent key never gets: forgetting, and everything in the vault."""
    return scope == mcp_server.FORGET_SCOPE or fn.__name__.startswith("vault_")


def guarded(fn: Callable[..., str], scope: str) -> Callable[..., str]:
    """The same tool, refused unless the caller's sign-in carries `scope` (and, for the
    owner-only tools, is the owner rather than an agent key)."""
    people_only = owner_only(fn, scope)

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        access = get_access_token()
        if access is None or scope not in access.scopes:
            raise ToolError(f"this sign-in does not include the {scope} permission")
        if people_only and is_agent(access):
            raise ToolError("this tool is only for the owner's own sign-in, not an agent key")
        return fn(*args, **kwargs)
    return inner


def bearer(request: Request) -> str:
    kind, _, value = request.headers.get("authorization", "").partition(" ")
    return value.strip() if kind.lower() == "bearer" else ""


def build(settings: OnlineSettings, verifier: object | None = None) -> MCPServer:
    """The online server: every tool that has an online permission, each behind it,
    and the web API (api.py) behind the same token check."""
    verifier = verifier or JwtVerifier(settings)
    server = MCPServer(
        "recall", instructions=mcp_server.ONLINE_INSTRUCTIONS,
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=settings.issuer, resource_server_url=settings.url,
            required_scopes=SCOPES, validate_token_resource=True,
        ),
    )
    for fn, scope, options in mcp_server.TOOLS:
        if scope is not None:
            server.tool(**options)(guarded(fn, scope))

    @server.custom_route("/serve", methods=["POST"])
    async def serve_route(request: Request) -> JSONResponse:
        """`recall serve` over HTTPS, for a memory provider that cannot reach the desktop's socket.
        Takes one request object as the socket does; only the ops in ONLINE_OPS (no store)."""
        access = await verifier.verify_token(bearer(request)) if bearer(request) else None
        if access is None or mcp_server.READ_SCOPE not in access.scopes:
            return JSONResponse({"ok": False, "error": "sign-in or agent key required"}, status_code=401)
        line = await request.body()
        try:
            op = json.loads(line).get("op")
        except (ValueError, AttributeError):
            op = None
        if op not in ONLINE_OPS:
            return JSONResponse({"ok": False, "error": f"op must be one of {', '.join(sorted(ONLINE_OPS))}"},
                                status_code=400)
        if op in WRITE_OPS and mcp_server.WRITE_SCOPE not in access.scopes:
            return JSONResponse({"ok": False, "error": "this sign-in cannot save"}, status_code=403)
        try:
            settings_ = mcp_server._settings()
        except Exception as exc:  # recall unconfigured: the caller goes on without memory
            return JSONResponse({"ok": False, "error": type(exc).__name__}, status_code=503)
        return JSONResponse(await anyio.to_thread.run_sync(serve.answer, settings_, line))

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        """Open to anyone, so it says only whether the server is up and can reach its database."""
        ok = await anyio.to_thread.run_sync(database_reachable)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 503)

    api.register(server, verifier)
    return server


def database_reachable() -> bool:
    try:
        db.client(mcp_server._settings()).admin.command("ping")
        return True
    except BaseException as exc:  # includes the SystemExit a missing setting raises
        log.warning("health check: database not reachable (%s)", type(exc).__name__)
        return False


def main() -> None:
    settings = load_settings()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log.info("serving %s on %s:%s for %d allowed user(s)", settings.url, settings.host, settings.port, len(settings.subjects))
    build(settings).run(
        "streamable-http", host=settings.host, port=settings.port, streamable_http_path=settings.path,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[settings.public_host, *settings.extra_hosts], allowed_origins=[settings.public_origin],
        ),
    )
