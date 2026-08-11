from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config_store import (
    IronDeployPaths,
    load_config,
    save_config,
    validate_certificate,
)
from .security import (
    SESSION_COOKIE,
    LocalOnlyMiddleware,
    clear_session_cookie,
    get_security_state,
    make_session_redirect,
    require_session,
)
from .smb_tools import (
    SmbToolError,
    configure_local_share,
    get_local_smb_info,
    test_local_share_access,
)
from .odj_tools import OdjToolError, get_local_odj_info, secure_odj_acl

SETUPWEB_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = SETUPWEB_ROOT / "static"
TEMPLATE_ROOT = SETUPWEB_ROOT / "templates"
PATHS = IronDeployPaths.from_setupweb(SETUPWEB_ROOT)
SECURITY = get_security_state()

app = FastAPI(title="IronDeploy SetupWeb", docs_url=None, redoc_url=None)
app.add_middleware(LocalOnlyMiddleware, state=SECURITY)
app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")


def session_dependency(request: Request) -> None:
    require_session(request, SECURITY)


SessionRequired = Annotated[None, Depends(session_dependency)]


@app.get("/")
def index(request: Request) -> Response:
    token = request.query_params.get("token")
    if token:
        session_id = SECURITY.exchange_bootstrap_token(token)
        if session_id is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "The setup link is invalid, expired, or already used.",
            )
        return make_session_redirect(session_id, SECURITY.session_ttl_seconds)

    require_session(request, SECURITY)
    return FileResponse(TEMPLATE_ROOT / "index.html")


@app.get("/api/session")
def get_session(request: Request) -> dict[str, Any]:
    session = require_session(request, SECURITY)
    return {
        "root": str(PATHS.root),
        "csrfToken": session.csrf_token,
        "expiresInSeconds": max(
            0,
            int(SECURITY.session_ttl_seconds - (session.last_seen_at - session.created_at)),
        ),
    }


@app.get("/api/config")
def get_config(_: SessionRequired) -> dict[str, Any]:
    config = load_config(PATHS)
    config["localSmb"] = get_local_smb_info(PATHS)
    config["localOdj"] = get_local_odj_info(PATHS)
    return config


@app.post("/api/config")
async def post_config(request: Request, _: SessionRequired) -> JSONResponse:
    try:
        payload = await request.json()
        result = save_config(PATHS, payload)
        result["config"]["localSmb"] = get_local_smb_info(PATHS)
        result["config"]["localOdj"] = get_local_odj_info(PATHS)
        return JSONResponse(result)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@app.post("/api/validate")
def validate(_: SessionRequired) -> dict[str, Any]:
    if not PATHS.validation_script.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Validation script not found.")

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PATHS.validation_script),
        ],
        cwd=str(PATHS.root),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return {
        "exitCode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


@app.post("/api/certificate/validate")
async def validate_uploaded_certificate(
    request: Request, _: SessionRequired
) -> JSONResponse:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid payload.")
        result = validate_certificate(
            PATHS,
            str(payload.get("certificateBase64", "")),
            str(payload.get("certificateType", "")),
        )
        return JSONResponse(result)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


def smb_error_response(exc: SmbToolError) -> JSONResponse:
    return JSONResponse(
        {"detail": str(exc), "code": exc.code},
        status_code=exc.status_code,
    )


def odj_error_response(exc: OdjToolError) -> JSONResponse:
    return JSONResponse(
        {"detail": str(exc), "code": exc.code},
        status_code=exc.status_code,
    )


@app.post("/api/odj/secure-folder")
async def secure_odj_folder(
    request: Request, _: SessionRequired
) -> JSONResponse:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise OdjToolError("invalid_request", "Invalid ODJ ACL request.")
        return JSONResponse(
            secure_odj_acl(PATHS, str(payload.get("account", "")))
        )
    except OdjToolError as exc:
        return odj_error_response(exc)


@app.post("/api/smb/configure-local-share")
async def configure_smb_share(
    request: Request, _: SessionRequired
) -> JSONResponse:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise SmbToolError("invalid_request", "Invalid SMB request.")
        result = configure_local_share(
            PATHS,
            str(payload.get("serverAddress", "")),
            str(payload.get("shareName", "")),
            str(payload.get("account", "")),
        )
        return JSONResponse(result)
    except SmbToolError as exc:
        return smb_error_response(exc)


@app.post("/api/smb/test-access")
async def test_smb_access(
    request: Request, _: SessionRequired
) -> JSONResponse:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise SmbToolError("invalid_request", "Invalid SMB request.")
        result = test_local_share_access(
            PATHS,
            str(payload.get("serverAddress", "")),
            str(payload.get("shareName", "")),
            str(payload.get("account", "")),
            str(payload.get("password", "")),
        )
        return JSONResponse(result)
    except SmbToolError as exc:
        return smb_error_response(exc)


@app.post("/api/finish")
def finish(request: Request, response: Response, _: SessionRequired) -> dict[str, Any]:
    SECURITY.destroy_session(request.cookies.get(SESSION_COOKIE))
    clear_session_cookie(response)

    should_stop = os.environ.get("SETUPWEB_DISABLE_AUTO_EXIT") != "1"
    if should_stop:
        threading.Timer(0.75, lambda: os._exit(0)).start()

    return {"stopping": should_stop}
