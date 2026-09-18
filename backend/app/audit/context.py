"""Actor attribution: authenticated identity + observed request context.

Three distinct things are captured and never conflated:

1. **Authenticated identity** – derived only from the verified JWT and the
   server-side ``auth_sessions`` row (user id, e-mail, role, session id).
2. **Observed request context** – what the server saw: client IP as resolved by
   the ASGI server's trusted-proxy handling, raw forwarding header, User-Agent,
   and *client-reported* device/app identifiers (never authoritative).
3. The operation itself (recorded by :mod:`app.audit.recorder`).

Nothing in the request body can influence identity fields.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.audit.sanitize import optional_text
from app.audit.taxonomy import ActorType

if TYPE_CHECKING:
    from app.db.models.audit import AuthSessionModel
    from app.db.models.user import UserModel

REQUEST_ID_HEADER = "X-Request-ID"
DEVICE_ID_HEADER = "X-Client-Device-Id"
APP_VERSION_HEADER = "X-Client-App-Version"
CLIENT_CORRELATION_HEADER = "X-Correlation-ID"

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_APP_VERSION_RE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
_HOSTNAME = socket.gethostname()
_PID = os.getpid()


# --------------------------------------------------------------------------- #
# Request id middleware
# --------------------------------------------------------------------------- #


class RequestIdMiddleware:
    """Assign a server-generated request id to every HTTP request.

    A client-supplied ``X-Request-ID`` is never used as the id (it is only kept
    as a non-authoritative client correlation hint). The id is echoed back in
    the response so operators can quote it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.lower().encode(), request_id.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_id)


# --------------------------------------------------------------------------- #
# User-Agent parsing (coarse, non-invasive)
# --------------------------------------------------------------------------- #

_BROWSERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Edge", re.compile(r"Edg(?:e|A|iOS)?/([\d.]+)")),
    ("Opera", re.compile(r"(?:OPR|Opera)/([\d.]+)")),
    ("Firefox", re.compile(r"(?:Firefox|FxiOS)/([\d.]+)")),
    ("Chrome", re.compile(r"(?:Chrome|CriOS)/([\d.]+)")),
    ("Safari", re.compile(r"Version/([\d.]+).*Safari/")),
)
_API_CLIENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("curl", re.compile(r"^curl/([\d.]+)")),
    ("python-httpx", re.compile(r"python-httpx/([\d.]+)")),
    ("python-requests", re.compile(r"python-requests/([\d.]+)")),
    ("Postman", re.compile(r"PostmanRuntime/([\d.]+)")),
    ("okhttp", re.compile(r"okhttp/([\d.]+)")),
    ("Wget", re.compile(r"Wget/([\d.]+)")),
)
_OS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("iOS", re.compile(r"(?:iPhone|iPad|iPod).*?OS ([\d_]+)")),
    ("Android", re.compile(r"Android ([\d.]+)")),
    ("Windows", re.compile(r"Windows NT ([\d.]+)")),
    ("macOS", re.compile(r"Mac OS X ([\d_.]+)")),
    ("ChromeOS", re.compile(r"CrOS [\w]+ ([\d.]+)")),
    ("Linux", re.compile(r"Linux()")),
)


def parse_user_agent(user_agent: str | None) -> dict[str, str | None]:
    """Coarse browser/OS/device-class classification from the User-Agent."""
    info: dict[str, str | None] = {
        "browser": None,
        "browser_version": None,
        "os": None,
        "os_version": None,
        "device_class": None,
    }
    if not user_agent:
        return info
    for name, pattern in _API_CLIENTS:
        match = pattern.search(user_agent)
        if match:
            info.update(browser=name, browser_version=match.group(1), device_class="api-client")
            return info
    for name, pattern in _BROWSERS:
        match = pattern.search(user_agent)
        if match:
            info.update(browser=name, browser_version=match.group(1))
            break
    for name, pattern in _OS:
        match = pattern.search(user_agent)
        if match:
            version = match.group(1).replace("_", ".") or None
            info.update(os=name, os_version=version)
            break
    if "iPad" in user_agent or "Tablet" in user_agent:
        info["device_class"] = "tablet"
    elif "Mobi" in user_agent or "iPhone" in user_agent or "Android" in user_agent:
        info["device_class"] = "mobile"
    elif info["browser"] is not None:
        info["device_class"] = "desktop"
    else:
        info["device_class"] = "unknown"
    return info


# --------------------------------------------------------------------------- #
# Observed request context
# --------------------------------------------------------------------------- #


def _normalize_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _ip_scope(ip: str | None) -> str | None:
    if ip is None:
        return None
    addr = ipaddress.ip_address(ip)
    if addr.is_loopback:
        return "loopback"
    if addr.is_private:
        return "private"
    return "public"


@dataclass(frozen=True)
class RequestContext:
    """Server-observed facts about the HTTP request."""

    request_id: str | None = None
    client_ip: str | None = None
    client_ip_scope: str | None = None
    peer_label: str | None = None
    forwarded_for: str | None = None
    user_agent: str | None = None
    client: dict[str, str | None] = field(default_factory=dict)
    device_id: str | None = None
    device_id_invalid: bool = False
    app_version: str | None = None
    client_correlation_id: str | None = None
    http_method: str | None = None
    http_path: str | None = None

    def context_dict(self) -> dict[str, Any]:
        return {
            "network": {
                "client_ip": self.client_ip,
                "client_ip_scope": self.client_ip_scope,
                "peer_label": self.peer_label,
                "forwarded_for_header": self.forwarded_for,
                "ip_source": "asgi-client (uvicorn trusted-proxy resolution)",
            },
            "client": {
                **self.client,
                "app_version": self.app_version,
                "device_id": self.device_id,
                "device_id_invalid": self.device_id_invalid or None,
                "client_correlation_id": self.client_correlation_id,
                "client_reported_fields_are_unverified": True,
            },
            "server": {"host": _HOSTNAME, "pid": _PID, "process": "trading-api"},
        }


def build_request_context(request: Request | None) -> RequestContext:
    if request is None:
        return RequestContext()
    state_request_id = request.scope.get("state", {}).get("request_id")
    raw_host = request.client.host if request.client else None
    client_ip = _normalize_ip(raw_host)
    user_agent = optional_text(request.headers.get("user-agent"), 512)

    device_raw = request.headers.get(DEVICE_ID_HEADER)
    device_id = device_raw.strip() if device_raw and _DEVICE_ID_RE.match(device_raw.strip()) else None
    app_raw = request.headers.get(APP_VERSION_HEADER)
    app_version = app_raw.strip() if app_raw and _APP_VERSION_RE.match(app_raw.strip()) else None

    return RequestContext(
        request_id=state_request_id,
        client_ip=client_ip,
        client_ip_scope=_ip_scope(client_ip),
        peer_label=None if client_ip else optional_text(raw_host, 64),
        forwarded_for=optional_text(request.headers.get("x-forwarded-for"), 256),
        user_agent=user_agent,
        client=parse_user_agent(user_agent),
        device_id=device_id,
        device_id_invalid=bool(device_raw) and device_id is None,
        app_version=app_version,
        client_correlation_id=optional_text(request.headers.get(CLIENT_CORRELATION_HEADER), 64),
        http_method=request.method,
        http_path=optional_text(request.url.path, 512),
    )


# --------------------------------------------------------------------------- #
# Authenticated principal (set by app.api.deps.get_current_user)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SessionInfo:
    session_id: uuid.UUID
    created_at: datetime | None
    expires_at: datetime | None
    login_ip: str | None
    login_user_agent: str | None
    login_device_id: str | None
    login_audit_event_id: uuid.UUID | None

    @classmethod
    def from_row(cls, row: AuthSessionModel) -> SessionInfo:
        return cls(
            session_id=row.id,
            created_at=row.created_at,
            expires_at=row.expires_at,
            login_ip=str(row.login_ip) if row.login_ip is not None else None,
            login_user_agent=row.login_user_agent,
            login_device_id=row.login_device_id,
            login_audit_event_id=row.login_audit_event_id,
        )


@dataclass(frozen=True)
class Principal:
    """Server-verified identity attached to ``request.state.audit_principal``."""

    user_id: int
    email: str
    role: str
    auth_method: str
    token_issued_at: datetime | None = None
    session: SessionInfo | None = None


PRINCIPAL_STATE_KEY = "audit_principal"


def principal_from_request(request: Request | None) -> Principal | None:
    if request is None:
        return None
    value = getattr(request.state, PRINCIPAL_STATE_KEY, None)
    return value if isinstance(value, Principal) else None


# --------------------------------------------------------------------------- #
# Actor
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AuditActor:
    actor_type: ActorType
    user_id: int | None = None
    email: str | None = None
    role: str | None = None
    auth_method: str | None = None
    session: SessionInfo | None = None
    token_issued_at: datetime | None = None
    service_label: str | None = None

    @classmethod
    def anonymous(cls) -> AuditActor:
        return cls(actor_type=ActorType.ANONYMOUS)

    @classmethod
    def service(cls, label: str, auth_method: str) -> AuditActor:
        return cls(actor_type=ActorType.SERVICE, service_label=label, auth_method=auth_method)


def actor_for_user(request: Request | None, user: UserModel) -> AuditActor:
    """Build the actor for an authenticated user.

    Identity comes from the resolved ``UserModel`` (loaded server-side from the
    verified token). Session details come from the principal that
    ``get_current_user`` attached; when a dependency override supplied the user
    (tests) there is simply no session information.
    """
    principal = principal_from_request(request)
    if principal is not None and principal.user_id == user.id:
        return AuditActor(
            actor_type=ActorType.USER,
            user_id=principal.user_id,
            email=principal.email,
            role=principal.role,
            auth_method=principal.auth_method,
            session=principal.session,
            token_issued_at=principal.token_issued_at,
        )
    return AuditActor(
        actor_type=ActorType.USER,
        user_id=user.id,
        email=user.email,
        role=user.role,
        auth_method="unverified-context",
    )


def session_observations(actor: AuditActor, ctx: RequestContext) -> dict[str, Any] | None:
    """Factual comparisons between this request and the session's login context.

    These are observations for investigators, not a risk score: a mismatch can
    be entirely legitimate (e.g. network change).
    """
    session = actor.session
    if session is None:
        return None
    now = datetime.now(UTC)
    age = (now - session.created_at).total_seconds() if session.created_at else None

    def _compare(current: str | None, at_login: str | None) -> bool | None:
        if current is None or at_login is None:
            return None
        return current == at_login

    return {
        "session_age_seconds": round(age, 1) if age is not None else None,
        "ip_matches_login": _compare(ctx.client_ip, session.login_ip),
        "device_matches_login": _compare(ctx.device_id, session.login_device_id),
        "user_agent_matches_login": _compare(ctx.user_agent, session.login_user_agent),
        "login_ip": session.login_ip,
        "login_audit_event_id": str(session.login_audit_event_id)
        if session.login_audit_event_id
        else None,
    }
