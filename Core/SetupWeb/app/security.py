from __future__ import annotations

import hmac
import os
import secrets
import time
from dataclasses import dataclass
from typing import Callable

from fastapi import HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

SESSION_COOKIE = "irondeploy_setup_session"
CSRF_HEADER = "x-csrf-token"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
LOCAL_CLIENTS = {"127.0.0.1", "::1"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass
class Session:
    csrf_token: str
    created_at: float
    last_seen_at: float


class SecurityState:
    def __init__(
        self,
        bootstrap_token: str,
        port: int,
        token_ttl_seconds: int = 120,
        session_ttl_seconds: int = 480,
    ) -> None:
        if not bootstrap_token:
            raise RuntimeError("SETUPWEB_BOOTSTRAP_TOKEN is required.")
        if port < 1 or port > 65535:
            raise RuntimeError("SETUPWEB_PORT must be a TCP port.")

        self.bootstrap_token = bootstrap_token
        self.port = port
        self.token_expires_at = time.time() + token_ttl_seconds
        self.session_ttl_seconds = session_ttl_seconds
        self.bootstrap_consumed = False
        self.sessions: dict[str, Session] = {}

    def expected_hosts(self) -> set[str]:
        return {f"{host}:{self.port}" for host in LOCAL_HOSTS}

    def expected_origins(self) -> set[str]:
        return {f"http://{host}:{self.port}" for host in LOCAL_HOSTS}

    def exchange_bootstrap_token(self, token: str) -> str | None:
        if self.bootstrap_consumed or time.time() > self.token_expires_at:
            return None
        if not hmac.compare_digest(token, self.bootstrap_token):
            return None
        self.bootstrap_consumed = True
        return self.create_session()

    def create_session(self) -> str:
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        self.sessions[session_id] = Session(
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            last_seen_at=now,
        )
        return session_id

    def get_session(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        session = self.sessions.get(session_id)
        if session is None:
            return None
        now = time.time()
        if (
            now - session.created_at > self.session_ttl_seconds
            or now - session.last_seen_at > self.session_ttl_seconds
        ):
            self.sessions.pop(session_id, None)
            return None
        session.last_seen_at = now
        return session

    def destroy_session(self, session_id: str | None) -> None:
        if session_id:
            self.sessions.pop(session_id, None)


def get_security_state() -> SecurityState:
    token = os.environ.get("SETUPWEB_BOOTSTRAP_TOKEN", "")
    port = int(os.environ.get("SETUPWEB_PORT", "0"))
    token_ttl = int(os.environ.get("SETUPWEB_TOKEN_TTL_SECONDS", "120"))
    session_ttl = int(os.environ.get("SETUPWEB_SESSION_TTL_SECONDS", "480"))
    return SecurityState(token, port, token_ttl, session_ttl)


class LocalOnlyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, state: SecurityState) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self.state = state

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        client_host = request.client.host if request.client else ""
        if client_host not in LOCAL_CLIENTS:
            return PlainTextResponse("Localhost only.", status_code=status.HTTP_403_FORBIDDEN)

        host = request.headers.get("host", "")
        if host not in self.state.expected_hosts():
            return PlainTextResponse(
                "Invalid Host header.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if request.method in UNSAFE_METHODS:
            origin = request.headers.get("origin")
            if origin not in self.state.expected_origins():
                return PlainTextResponse(
                    "Invalid Origin header.",
                    status_code=status.HTTP_403_FORBIDDEN,
                )

            session = self.state.get_session(request.cookies.get(SESSION_COOKIE))
            csrf_token = request.headers.get(CSRF_HEADER)
            if session is None or not csrf_token:
                return PlainTextResponse(
                    "Session required.",
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )
            if not hmac.compare_digest(csrf_token, session.csrf_token):
                return PlainTextResponse(
                    "Invalid CSRF token.",
                    status_code=status.HTTP_403_FORBIDDEN,
                )

        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response


def require_session(request: Request, state: SecurityState) -> Session:
    session = state.get_session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session required.")
    return session


def make_session_redirect(session_id: str, max_age: int) -> RedirectResponse:
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=max_age,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )
    return response


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
