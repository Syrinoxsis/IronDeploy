import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from ipaddress import ip_address
from pathlib import Path
from time import perf_counter
from typing import AsyncIterator
from urllib.parse import unquote

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.orm import Session

from app.config import IRONDEPLOY_ROOT, get_settings
from app.database import SessionLocal, get_session, initialize_database
from app.auth import (
    DEPLOYMENT_COMPLETION_RECEIPT_TTL,
    PERMISSIONS,
    SESSION_COOKIE,
    AuthError,
    AuthUser,
    DeploymentToken,
    authenticate_user,
    bind_deployment_token,
    bootstrap_superadmin,
    cookie_secure,
    create_deployment_token,
    create_session,
    create_user,
    current_user,
    deployment_completion_receipt_from_authorization,
    deployment_token_from_authorization,
    destroy_session,
    is_deployment_operator,
    revoke_deployment_token,
    serialize_user,
    set_deployment_token_phase,
    set_user_active,
    set_user_password,
    set_user_permissions,
    user_permissions,
)
from app.deployments import (
    DEPLOYMENT_BEGIN,
    DEPLOYMENT_COMPLETED,
    DEPLOYMENT_FAILED,
    STAGE_COMPLETED,
    STAGE_FAILED,
    STAGE_RUNNING,
    STAGE_SKIPPED,
    Computer,
    ComputerListResponse,
    Deployment,
    DeploymentBeginRequest,
    DeploymentCompleteRequest,
    DeploymentErrorRequest,
    DeploymentImageRequest,
    DeploymentListItem,
    DeploymentListResponse,
    DeploymentManifestRequest,
    DeploymentNetworkAdaptersReport,
    DeploymentNetworkDiagnosticsRequest,
    DeploymentNetworkDiagnosticsResponse,
    DeploymentNetworkStage,
    DeploymentNetworkSummary,
    DeploymentProgram,
    DeploymentPowerShellResult,
    DeploymentResponse,
    DeploymentStage,
    DeploymentStageEvent,
    DeploymentStageResponse,
    DomainJoinAcknowledgeResponse,
    DomainJoinProvisionResponse,
    NetworkStageReport,
    WinPEStageCode,
    as_utc,
    discard_domain_join_blob,
    expire_stale_deployments,
    find_known_computer_names,
    to_computer_list_item,
    to_deployment_list_item,
    to_network_diagnostics_response,
    to_network_stage_response,
    to_deployment_response,
    to_deployment_stage_response,
    update_computer_inventory,
)
from app.domain_join import (
    DomainJoinError,
    delete_domain_join_blob,
    get_domain_join_blob,
    provision_domain_join_blob,
)
from app.deployment_profiles import (
    DeploymentProfileError,
    load_default_profile,
    update_default_profile,
)
from app.drivers import (
    DriverError,
    DriverUploadLimits,
    begin_driver_package_upload,
    cancel_driver_package_upload,
    create_vendor,
    delete_abandoned_driver_upload,
    delete_all_abandoned_driver_uploads,
    delete_driver_package,
    delete_vendor,
    finalize_driver_package_upload,
    get_driver_upload_info,
    list_driver_packages,
    rename_driver_package,
    rename_vendor,
    save_uploaded_driver_file,
)
from app.image_config import (
    ImageConfigError,
    load_image_config,
    save_image_config,
)
from app.ldap_names import (
    DirectoryLookupError,
    NameSuggestion,
    computer_exists,
    suggest_computer_name,
)
from app.deployment_images import (
    cancel_esd_conversion,
    DeploymentImageError,
    get_conversion_state,
    list_deployment_images,
    rename_deployment_image,
    save_uploaded_image,
    set_default_image_index,
    start_esd_conversion,
)
from app.programs import (
    ProgramError,
    delete_program,
    list_programs,
    rename_program,
    save_uploaded_program,
    set_program_arguments,
)
from app.post_powershell import (
    MAX_OUTPUT_SIZE_BYTES,
    PostPowerShellError,
    delete_script,
    list_scripts,
    resolve_profile_scripts,
    result_log_path,
    save_uploaded_script,
    update_script_settings,
    verified_script_path,
)
from app.winpe_build import (
    WinPEBuildError,
    get_winpe_build_state,
    start_winpe_build,
)
from app.winpe_auth import (
    WINPE_AUTH_ACCOUNT,
    WINPE_SYSTEM_USERNAME,
    WinPEAuthError,
    authorize_with_pin,
    authorize_without_credentials,
    get_winpe_auth_policy,
    is_winpe_system_user,
    serialize_winpe_auth_policy,
    set_winpe_auth_policy,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    initialize_database()
    with SessionLocal() as session:
        bootstrap_superadmin(session)
    yield


app = FastAPI(title="IronAPI", version="0.0.1-alpha.1", lifespan=lifespan)
STATIC_DIR = Path(__file__).resolve().parent / "static"
SERVER_TEMPLATES_ROOT = IRONDEPLOY_ROOT / "ServerTemplates"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
access_logger = logging.getLogger("uvicorn.error")


def is_client_allowed(client_ip: str | None) -> bool:
    if client_ip is None:
        return False

    try:
        address = ip_address(client_ip)
    except ValueError:
        return False

    if address.is_loopback:
        return True

    allowed_networks = get_settings().allowed_client_networks
    if not allowed_networks:
        return True

    return any(
        address.version == network.version and address in network
        for network in allowed_networks
    )


@app.middleware("http")
async def authorize_deployment_client(request: Request, call_next):
    path = request.url.path
    public_auth_paths = {
        "/api/deploy/auth/login",
        "/api/deploy/auth/policy",
        "/api/deploy/auth/authorize",
    }
    if path.startswith("/api/deploy/") and path not in public_auth_paths:
        with SessionLocal() as session:
            authorization = request.headers.get("authorization")
            token = deployment_token_from_authorization(
                session,
                authorization,
            )
            if token is None and request.method == "POST":
                completion_match = re.fullmatch(
                    r"/api/deploy/([1-9][0-9]*)/complete",
                    path,
                )
                if completion_match is not None:
                    token = deployment_completion_receipt_from_authorization(
                        session,
                        authorization,
                        int(completion_match.group(1)),
                    )
                    if token is not None:
                        request.state.deployment_completion_replay = True
            if token is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Valid deployment Bearer token required."},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            request.state.deployment_token_id = token.id
    return await call_next(request)


def require_deployment_token(
    request: Request,
    session: Session,
    *allowed_phases: str,
) -> DeploymentToken:
    token_id = getattr(request.state, "deployment_token_id", None)
    token = session.get(DeploymentToken, token_id) if token_id else None
    if token is None or token.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Deployment token is invalid.")
    if allowed_phases and token.phase not in allowed_phases:
        raise HTTPException(
            status_code=409,
            detail=f"Deployment token is in the {token.phase} phase.",
        )
    return token


def require_owned_deployment(
    deployment_id: int,
    request: Request,
    session: Session,
    *allowed_phases: str,
) -> tuple[Deployment, DeploymentToken]:
    token = require_deployment_token(request, session, *allowed_phases)
    if token.deployment_id != deployment_id:
        raise HTTPException(
            status_code=403,
            detail="Deployment token belongs to another deployment.",
        )
    deployment = session.get(Deployment, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return deployment, token


@app.middleware("http")
async def restrict_client_ip(request: Request, call_next):
    client_ip = request.client.host if request.client else None
    client_port = request.client.port if request.client else None
    client_address = (
        f"{client_ip}:{client_port}" if client_ip and client_port else str(client_ip)
    )
    started_at = perf_counter()

    if not is_client_allowed(client_ip):
        response = JSONResponse(status_code=403, content={"detail": "Forbidden"})
    else:
        try:
            response = await call_next(request)
        except Exception:
            access_logger.exception(
                "client=%s method=%s endpoint=%s status=500 duration_ms=%.1f",
                client_address,
                request.method,
                request.url.path,
                (perf_counter() - started_at) * 1000,
            )
            raise

    access_logger.info(
        "client=%s method=%s endpoint=%s status=%d duration_ms=%.1f",
        client_address,
        request.method,
        request.url.path,
        response.status_code,
        (perf_counter() - started_at) * 1000,
    )
    return response


_PAGE_PERMISSIONS = {
    "/": "dashboard",
    "/images": "images",
    "/programs": "programs",
    "/post-powershell": "post_powershell",
    "/drivers": "drivers",
    "/info": "superadmin",
    "/image-config": "image_config",
    "/users": "superadmin",
    "/access-control": "superadmin",
    "/docs": "superadmin",
    "/redoc": "superadmin",
    "/openapi.json": "superadmin",
}


def _browser_permission(path: str) -> str | None:
    if path.startswith("/dashboard/"):
        return "dashboard"
    if path in _PAGE_PERMISSIONS:
        return _PAGE_PERMISSIONS[path]
    if (
        path in {"/api/deployments", "/api/computers"}
        or path.startswith("/api/deployments/")
    ):
        return "dashboard"
    if path.startswith("/api/deployment-images"):
        return "images"
    if path.startswith("/api/programs"):
        return "programs"
    if path.startswith("/api/post-powershell"):
        return "post_powershell"
    if path.startswith("/api/drivers"):
        return "drivers"
    if path.startswith("/api/info"):
        return "superadmin"
    if path.startswith("/api/image-config") or path.startswith("/api/winpe-build"):
        return "image_config"
    if path.startswith("/api/admin/"):
        return "superadmin"
    if path in {"/api/auth/me", "/api/auth/logout"}:
        return "authenticated"
    return None


@app.middleware("http")
async def authorize_browser_request(request: Request, call_next):
    client_ip = request.client.host if request.client else None
    if not is_client_allowed(client_ip):
        # Let the access-control middleware return and log the network-level 403
        # before attempting any account or session lookup.
        return await call_next(request)
    if request.url.path.startswith("/static/") and request.url.path.endswith(".html"):
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    required = _browser_permission(request.url.path)
    if required is None:
        return await call_next(request)

    with SessionLocal() as session:
        user = current_user(session, request)
        if user is None:
            if request.url.path.startswith("/api/"):
                return JSONResponse(status_code=401, content={"detail": "Login required."})
            return RedirectResponse(
                f"/login?next={request.url.path}", status_code=303
            )
        allowed = (
            required == "authenticated"
            or user.is_superadmin
            or required in user_permissions(session, user)
        )
        if not allowed:
            if request.url.path.startswith("/api/"):
                return JSONResponse(status_code=403, content={"detail": "Access denied."})
            return FileResponse(
                STATIC_DIR / "forbidden.html",
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
    return await call_next(request)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "form-action 'self'; base-uri 'none'"
    )
    if (
        request.url.path == "/login"
        or request.url.path.startswith("/api/auth/")
        or request.url.path.startswith("/api/deploy/")
    ):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "dashboard.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/dashboard/{deployment_id}", include_in_schema=False)
def deployment_dashboard_page(deployment_id: int) -> FileResponse:
    return FileResponse(
        STATIC_DIR / "deployment-detail.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/image-config", include_in_schema=False)
def image_config_page() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "image-config.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/login", include_in_schema=False)
def login_page() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "login.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/users", include_in_schema=False)
def users_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "users.html", headers={"Cache-Control": "no-cache"})


@app.get("/access-control", include_in_schema=False)
def access_control_page() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "access-control.html", headers={"Cache-Control": "no-cache"}
    )


@app.get("/images", include_in_schema=False)
def deployment_images_page() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "images.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/programs", include_in_schema=False)
def programs_page() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "programs.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/post-powershell", include_in_schema=False)
def post_powershell_page() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "post-powershell.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/drivers", include_in_schema=False)
def drivers_page() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "drivers.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/info", include_in_schema=False)
def info_page() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "info.html",
        headers={"Cache-Control": "no-cache"},
    )


_IMAGE_CONFIG_REQUEST_HEADER = "x-requested-with"
_IMAGE_CONFIG_REQUEST_VALUE = "IronDeploy"


def _header_hostname(value: str | None) -> str | None:
    """Extract the lower-cased host from a Host or Origin header value."""

    if not value:
        return None
    host = value.strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    if host.startswith("["):  # bracketed IPv6, e.g. [::1]:8000
        return host.split("]", 1)[0].lower() + "]"
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    return host.lower()


def require_image_config_write(request: Request) -> None:
    """Require same-origin writes from the image-settings page.

    Network access is governed by the same allowed-client middleware as the
    dashboard. A cross-site form cannot set the custom header, and a
    cross-origin fetch that tries triggers a CORS preflight this service does
    not answer, so forged writes from another site are rejected.
    """

    origin = request.headers.get("origin")
    if (
        origin is not None
        and _header_hostname(origin) != _header_hostname(request.headers.get("host"))
    ):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected.")
    if request.headers.get(_IMAGE_CONFIG_REQUEST_HEADER) != _IMAGE_CONFIG_REQUEST_VALUE:
        raise HTTPException(
            status_code=403,
            detail="Missing IronDeploy request header.",
        )


@app.get("/api/image-config")
def get_image_config(session: Session = Depends(get_session)) -> dict:
    config = load_image_config()
    config.update(load_default_profile(session))
    return config


@app.post("/api/image-config")
async def post_image_config(
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    require_image_config_write(request)
    try:
        payload = await request.json()
        profile = update_default_profile(session, payload)
        result = save_image_config(payload)
        session.commit()
        result["config"].update(profile)
    except (DeploymentProfileError, ImageConfigError) as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@app.get("/api/deployment-images")
def get_deployment_images() -> dict:
    try:
        return list_deployment_images()
    except DeploymentImageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/deployment-images/upload")
async def upload_deployment_image(request: Request) -> JSONResponse:
    require_image_config_write(request)
    encoded_name = request.headers.get("x-irondeploy-filename", "")
    try:
        result = await save_uploaded_image(unquote(encoded_name), request.stream())
    except DeploymentImageError as exc:
        status_code = 409 if "already exists" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return JSONResponse(result, status_code=201)


@app.get("/api/programs")
def get_programs() -> dict:
    try:
        return list_programs()
    except ProgramError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/programs/upload")
async def upload_program(request: Request) -> JSONResponse:
    require_image_config_write(request)
    encoded_name = request.headers.get("x-irondeploy-filename", "")
    encoded_arguments = request.headers.get("x-irondeploy-arguments", "")
    try:
        result = await save_uploaded_program(
            unquote(encoded_name),
            request.stream(),
            arguments=unquote(encoded_arguments),
        )
    except ProgramError as exc:
        status_code = 409 if "already exists" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return JSONResponse(result, status_code=201)


@app.post("/api/programs/{name}/arguments")
async def update_program_arguments(name: str, request: Request) -> JSONResponse:
    require_image_config_write(request)
    try:
        payload = await request.json()
        arguments = payload.get("arguments", "")
        if not isinstance(arguments, str):
            raise ProgramError("arguments must be a string.")
        result = set_program_arguments(name, arguments)
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="arguments must be a string.")
    except ProgramError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@app.post("/api/programs/{name}/rename")
async def rename_uploaded_program(name: str, request: Request) -> JSONResponse:
    require_image_config_write(request)
    try:
        payload = await request.json()
        result = rename_program(name, payload.get("newName", ""))
    except (AttributeError, TypeError):
        raise HTTPException(
            status_code=400,
            detail="newName must be an EXE or MSI file name.",
        )
    except ProgramError as exc:
        status_code = 409 if "already exists" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return JSONResponse(result)


@app.delete("/api/programs/{name}")
def remove_program(name: str, request: Request) -> JSONResponse:
    require_image_config_write(request)
    try:
        result = delete_program(name)
    except ProgramError as exc:
        status_code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return JSONResponse(result)


@app.get("/api/post-powershell")
def get_post_powershell(session: Session = Depends(get_session)) -> dict:
    try:
        result = list_scripts(session)
        session.commit()
        return result
    except PostPowerShellError as exc:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/post-powershell/upload")
async def upload_post_powershell(
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    require_image_config_write(request)
    try:
        result = await save_uploaded_script(
            session,
            unquote(request.headers.get("x-irondeploy-filename", "")),
            request.stream(),
            arguments=unquote(request.headers.get("x-irondeploy-arguments", "")),
            selection_mode=request.headers.get(
                "x-irondeploy-selection-mode", "operator"
            ),
            run_phase=request.headers.get(
                "x-irondeploy-run-phase", "after_software"
            ),
            timeout_seconds=request.headers.get(
                "x-irondeploy-timeout-seconds", "600"
            ),
        )
        session.commit()
        return JSONResponse({"uploaded": True, "script": result}, status_code=201)
    except PostPowerShellError as exc:
        session.rollback()
        status_code = 409 if "already exists" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.post("/api/post-powershell/{script_id}/settings")
async def set_post_powershell_settings(
    script_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    require_image_config_write(request)
    try:
        payload = await request.json()
        result = update_script_settings(session, script_id, payload)
        session.commit()
        return {"saved": True, "script": result}
    except (AttributeError, TypeError, ValueError):
        session.rollback()
        raise HTTPException(status_code=400, detail="Invalid script settings.")
    except PostPowerShellError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/post-powershell/{script_id}")
def remove_post_powershell(
    script_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    require_image_config_write(request)
    try:
        result = delete_script(session, script_id)
        session.commit()
        return result
    except PostPowerShellError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/drivers")
def get_drivers() -> dict:
    try:
        return list_driver_packages()
    except DriverError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _driver_upload_limits() -> DriverUploadLimits:
    settings = get_settings()
    return DriverUploadLimits(
        max_files=settings.driver_max_files,
        max_depth=settings.driver_max_depth,
        max_full_path=settings.driver_max_full_path,
        upload_ttl_hours=settings.driver_upload_ttl_hours,
        max_active_uploads=settings.driver_max_active_uploads,
        min_free_space_gib=settings.driver_min_free_space_gib,
    )


@app.get("/api/info/driver-uploads")
def get_driver_uploads_info() -> dict:
    try:
        return get_driver_upload_info(_driver_upload_limits())
    except DriverError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/info/driver-uploads/abandoned")
def remove_all_abandoned_driver_uploads(request: Request) -> JSONResponse:
    require_image_config_write(request)
    try:
        result = delete_all_abandoned_driver_uploads(_driver_upload_limits())
    except DriverError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@app.delete("/api/info/driver-uploads/{upload_id}")
def remove_abandoned_driver_upload(
    upload_id: str,
    request: Request,
) -> JSONResponse:
    require_image_config_write(request)
    try:
        result = delete_abandoned_driver_upload(
            upload_id,
            _driver_upload_limits(),
        )
    except DriverError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return JSONResponse(result)


@app.post("/api/drivers/vendors")
async def add_driver_vendor(request: Request) -> JSONResponse:
    require_image_config_write(request)
    try:
        payload = await request.json()
        result = create_vendor(payload.get("name", ""))
    except (AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="name must be a string.")
    except DriverError as exc:
        status_code = 409 if "already exists" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return JSONResponse(result, status_code=201)


@app.post("/api/drivers/vendors/{name}/rename")
async def rename_driver_vendor(name: str, request: Request) -> JSONResponse:
    require_image_config_write(request)
    try:
        payload = await request.json()
        result = rename_vendor(name, payload.get("newName", ""))
    except (AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="newName must be a string.")
    except DriverError as exc:
        status_code = 409 if "already exists" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return JSONResponse(result)


@app.delete("/api/drivers/vendors/{name}")
def remove_driver_vendor(name: str, request: Request) -> JSONResponse:
    require_image_config_write(request)
    try:
        result = delete_vendor(name)
    except DriverError as exc:
        status_code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return JSONResponse(result)


@app.post("/api/drivers/package-uploads")
async def begin_driver_upload(request: Request) -> JSONResponse:
    require_image_config_write(request)
    try:
        payload = await request.json()
        result = begin_driver_package_upload(
            payload.get("vendor", ""),
            payload.get("model", ""),
            limits=_driver_upload_limits(),
        )
    except (AttributeError, TypeError):
        raise HTTPException(
            status_code=400, detail="vendor and model must be strings."
        )
    except DriverError as exc:
        status_code = 409 if "already exists" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return JSONResponse(result, status_code=201)


@app.put("/api/drivers/package-uploads/{upload_id}/files")
async def upload_driver_file(upload_id: str, request: Request) -> JSONResponse:
    require_image_config_write(request)
    encoded_path = request.headers.get("x-irondeploy-relative-path", "")
    try:
        result = await save_uploaded_driver_file(
            upload_id,
            unquote(encoded_path),
            request.stream(),
            limits=_driver_upload_limits(),
        )
    except DriverError as exc:
        status_code = 404 if "upload not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return JSONResponse(result, status_code=201)


@app.post("/api/drivers/package-uploads/{upload_id}/finalize")
def finalize_driver_upload(upload_id: str, request: Request) -> JSONResponse:
    require_image_config_write(request)
    try:
        result = finalize_driver_package_upload(upload_id)
    except DriverError as exc:
        status_code = 409 if "already exists" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return JSONResponse(result, status_code=201)


@app.delete("/api/drivers/package-uploads/{upload_id}")
def cancel_driver_upload(upload_id: str, request: Request) -> JSONResponse:
    require_image_config_write(request)
    try:
        result = cancel_driver_package_upload(upload_id)
    except DriverError as exc:
        status_code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return JSONResponse(result)


@app.post("/api/drivers/packages/{vendor}/{model}/rename")
async def rename_uploaded_driver_package(
    vendor: str,
    model: str,
    request: Request,
) -> JSONResponse:
    require_image_config_write(request)
    try:
        payload = await request.json()
        result = rename_driver_package(
            vendor,
            model,
            payload.get("newModel", ""),
        )
    except (AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="newModel must be a string.")
    except DriverError as exc:
        status_code = 409 if "already exists" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return JSONResponse(result)


@app.delete("/api/drivers/packages/{vendor}/{model}")
def remove_driver_package(
    vendor: str,
    model: str,
    request: Request,
) -> JSONResponse:
    require_image_config_write(request)
    try:
        result = delete_driver_package(vendor, model)
    except DriverError as exc:
        status_code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return JSONResponse(result)


@app.post("/api/deployment-images/{name}/default-index")
async def update_default_image_index(name: str, request: Request) -> JSONResponse:
    require_image_config_write(request)
    try:
        payload = await request.json()
        index = int(payload.get("defaultIndex", 0))
        result = set_default_image_index(name, index)
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="defaultIndex must be a positive integer.",
        )
    except DeploymentImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@app.post("/api/auth/login")
async def auth_login(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid login payload.")
    with SessionLocal() as session:
        try:
            user = authenticate_user(session, username, password)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        if is_deployment_operator(session, user):
            raise HTTPException(
                status_code=403,
                detail="This account can only be used from IronDeploy WinPE.",
            )
        token = create_session(session, user)
        result = serialize_user(session, user)
    response = JSONResponse({"user": result})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=7 * 24 * 60 * 60,
        httponly=True,
        secure=cookie_secure(),
        samesite="strict",
        path="/",
    )
    return response


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict:
    with SessionLocal() as session:
        user = current_user(session, request)
        if user is None:
            raise HTTPException(status_code=401, detail="Login required.")
        return {
            "user": serialize_user(session, user),
            "availablePermissions": PERMISSIONS,
        }


@app.post("/api/auth/logout")
def auth_logout(request: Request) -> JSONResponse:
    require_image_config_write(request)
    with SessionLocal() as session:
        destroy_session(session, request.cookies.get(SESSION_COOKIE))
    response = JSONResponse({"loggedOut": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


def _managed_user(session: Session, user_id: int) -> AuthUser:
    user = session.get(AuthUser, user_id)
    if user is None or is_winpe_system_user(user):
        raise HTTPException(status_code=404, detail="User not found.")
    return user


@app.get("/api/admin/users")
def admin_user_list(session: Session = Depends(get_session)) -> dict:
    users = session.scalars(
        select(AuthUser)
        .where(AuthUser.username_normalized != WINPE_SYSTEM_USERNAME)
        .order_by(AuthUser.is_superadmin.desc(), AuthUser.username)
    ).all()
    return {
        "users": [serialize_user(session, user) for user in users],
        "permissions": PERMISSIONS,
    }


@app.post("/api/admin/users")
async def admin_create_user(
    request: Request, session: Session = Depends(get_session)
) -> JSONResponse:
    require_image_config_write(request)
    try:
        payload = await request.json()
        user = create_user(
            session,
            str(payload.get("username", "")),
            str(payload.get("password", "")),
        )
    except (AttributeError, TypeError, AuthError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(serialize_user(session, user), status_code=201)


@app.post("/api/admin/users/{user_id}/password")
async def admin_reset_password(
    user_id: int, request: Request, session: Session = Depends(get_session)
) -> JSONResponse:
    require_image_config_write(request)
    user = _managed_user(session, user_id)
    try:
        payload = await request.json()
        set_user_password(session, user, str(payload.get("password", "")))
    except (AttributeError, TypeError, AuthError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"saved": True})


@app.post("/api/admin/users/{user_id}/active")
async def admin_set_user_active(
    user_id: int, request: Request, session: Session = Depends(get_session)
) -> JSONResponse:
    require_image_config_write(request)
    user = _managed_user(session, user_id)
    try:
        payload = await request.json()
        active = payload.get("active")
        if not isinstance(active, bool):
            raise AuthError("active must be true or false.")
        set_user_active(session, user, active)
    except (AttributeError, TypeError, AuthError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(serialize_user(session, user))


@app.put("/api/admin/users/{user_id}/permissions")
async def admin_set_permissions(
    user_id: int, request: Request, session: Session = Depends(get_session)
) -> JSONResponse:
    require_image_config_write(request)
    user = _managed_user(session, user_id)
    try:
        payload = await request.json()
        raw_permissions = payload.get("permissions")
        if not isinstance(raw_permissions, list) or not all(
            isinstance(item, str) for item in raw_permissions
        ):
            raise AuthError("permissions must be a list.")
        set_user_permissions(session, user, set(raw_permissions))
    except (AttributeError, TypeError, AuthError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(serialize_user(session, user))


@app.post("/api/deployment-images/{name}/rename")
async def rename_windows_image(name: str, request: Request) -> JSONResponse:
    require_image_config_write(request)
    try:
        payload = await request.json()
        result = rename_deployment_image(name, payload.get("newName", ""))
    except (AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="newName must be a WIM file name.")
    except DeploymentImageError as exc:
        status_code = 409 if "already exists" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return JSONResponse(result)


@app.get("/api/deployment-images/conversion")
def deployment_image_conversion_status() -> dict:
    return get_conversion_state()


@app.post("/api/deployment-images/conversion/cancel")
def cancel_deployment_image_conversion(request: Request) -> JSONResponse:
    require_image_config_write(request)
    try:
        result = cancel_esd_conversion()
    except DeploymentImageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(result, status_code=202)


@app.post("/api/deployment-images/{name}/convert")
def convert_deployment_image(name: str, request: Request) -> JSONResponse:
    require_image_config_write(request)
    try:
        result = start_esd_conversion(name)
    except DeploymentImageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(result, status_code=202)


@app.get("/api/winpe-build")
def winpe_build_status() -> dict:
    return get_winpe_build_state()


@app.post("/api/winpe-build/{target}")
def begin_winpe_build(target: str, request: Request) -> JSONResponse:
    require_image_config_write(request)
    try:
        result = start_winpe_build(target)
    except WinPEBuildError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(result, status_code=202)


@app.post("/api/deploy/auth/login")
async def deployment_login(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid login payload.")

    with SessionLocal() as session:
        try:
            if get_winpe_auth_policy(session).mode != WINPE_AUTH_ACCOUNT:
                raise AuthError(
                    "Username and password authorization is disabled for WinPE."
                )
            user = authenticate_user(session, username, password)
            token, record = create_deployment_token(session, user)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return JSONResponse(
            {
                "access_token": token,
                "token_type": "bearer",
                "authorization_id": record.id,
                "expires_at": record.expires_at.isoformat(),
            },
            headers={"Cache-Control": "no-store"},
        )


@app.get("/api/admin/winpe-auth")
def admin_get_winpe_auth(session: Session = Depends(get_session)) -> dict:
    return serialize_winpe_auth_policy(get_winpe_auth_policy(session))


@app.put("/api/admin/winpe-auth")
async def admin_set_winpe_auth(
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    require_image_config_write(request)
    try:
        payload = await request.json()
        result = set_winpe_auth_policy(
            session,
            str(payload.get("mode", "")),
            payload.get("pin"),
        )
    except (AttributeError, TypeError, WinPEAuthError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(serialize_winpe_auth_policy(result))


@app.get("/api/deploy/auth/policy")
def deployment_auth_policy(
    session: Session = Depends(get_session),
) -> JSONResponse:
    policy = serialize_winpe_auth_policy(get_winpe_auth_policy(session))
    return JSONResponse(
        {
            "mode": policy["mode"],
            "pin_configured": policy["pinConfigured"],
            "pin_min_length": policy["pinMinLength"],
            "pin_max_length": policy["pinMaxLength"],
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/deploy/auth/authorize")
async def deployment_authorize(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        requested_mode = str(payload.get("mode", "")).strip().lower()
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid authorization payload.")

    client_address = request.client.host if request.client else None
    with SessionLocal() as session:
        try:
            policy = get_winpe_auth_policy(session)
            if requested_mode != policy.mode:
                raise WinPEAuthError(
                    "WinPE authorization mode changed. Refresh and try again."
                )
            if policy.mode == "pin":
                token, record = authorize_with_pin(
                    session,
                    str(payload.get("pin", "")),
                    client_address,
                )
            elif policy.mode == "none":
                token, record = authorize_without_credentials(session)
            else:
                raise WinPEAuthError(
                    "Use username and password authorization for this mode."
                )
        except WinPEAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return JSONResponse(
            {
                "access_token": token,
                "token_type": "bearer",
                "authorization_id": record.id,
                "expires_at": record.expires_at.isoformat(),
            },
            headers={"Cache-Control": "no-store"},
        )


@app.get("/api/deploy/suggest-name", response_model=NameSuggestion)
def deploy_suggest_name(
    request: Request,
    serial_number: str | None = None,
    mac_address: str | None = None,
    session: Session = Depends(get_session),
) -> NameSuggestion:
    require_deployment_token(request, session, "authorized", "winpe")
    settings = get_settings()
    normalized_mac = None
    if mac_address:
        try:
            normalized_mac = DeploymentBeginRequest.validate_mac_address(mac_address)
        except ValueError:
            normalized_mac = mac_address.strip().upper()

    normalized_serial = DeploymentBeginRequest.validate_serial_number(serial_number)
    known_computer_names = find_known_computer_names(
        session,
        normalized_serial,
        normalized_mac,
    )
    try:
        suggestion = suggest_computer_name(settings)
    except DirectoryLookupError as exc:
        suggestion = NameSuggestion(
            last_domain_name=None,
            suggested_name="",
            max_existing_number=None,
            source="manual",
            ldap_enabled=settings.ldap_enabled,
            ldap_error=str(exc),
        )
    suggestion.known_computer_names = known_computer_names
    return suggestion


def _deployment_catalog(session: Session | None = None) -> dict:
    image_listing = list_deployment_images()
    program_listing = list_programs()
    post_powershell_listing = (
        list_scripts(session) if session is not None else {"scripts": []}
    )
    driver_listing = list_driver_packages()
    return {
        "images": [
            {
                "name": image["name"],
                "size": image["size"],
                "format": image["format"],
                "sha256": image.get("sha256"),
                "ready": image["ready"],
                "indexes": image["indexes"],
                "defaultIndex": image["defaultIndex"],
            }
            for image in image_listing["images"]
        ],
        "programs": [
            {
                "name": program["name"],
                "size": program["size"],
                "type": program["type"],
                "arguments": program["arguments"],
                "sha256": program["sha256"],
            }
            for program in program_listing["programs"]
        ],
        "postPowerShell": post_powershell_listing["scripts"],
        "drivers": [
            {
                "vendor": package["vendor"],
                "model": package["model"],
                "relativePath": package["relativePath"],
                "size": package["size"],
                "infCount": package["infCount"],
            }
            for package in driver_listing["packages"]
            if package["infCount"] > 0
        ],
    }


@app.get("/api/deploy/catalog")
def deploy_catalog(
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    require_deployment_token(request, session, "authorized", "winpe")
    try:
        catalog = _deployment_catalog(session)
        session.commit()
        return catalog
    except (
        DeploymentImageError,
        ProgramError,
        PostPowerShellError,
        DriverError,
    ) as exc:
        session.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/deploy/{deployment_id}/manifest")
def deploy_manifest(
    deployment_id: int,
    payload: DeploymentManifestRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    expire_stale_deployments(session)
    deployment, _ = require_owned_deployment(
        deployment_id, request, session, "winpe"
    )
    if deployment.status != DEPLOYMENT_BEGIN:
        raise HTTPException(status_code=409, detail="Deployment is not active")

    try:
        catalog = _deployment_catalog(session)
        image_config = load_image_config()
        deployment_profile = load_default_profile(session)
    except (
        DeploymentImageError,
        ProgramError,
        PostPowerShellError,
        DriverError,
        ImageConfigError,
        DeploymentProfileError,
    ) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    image = next(
        (
            item
            for item in catalog["images"]
            if item["name"].casefold() == payload.image_name.casefold()
        ),
        None,
    )
    if image is None:
        raise HTTPException(status_code=400, detail="Selected image is unavailable")
    if not image["ready"]:
        raise HTTPException(status_code=409, detail="Selected image is not ready")

    programs_by_name = {
        item["name"].casefold(): item for item in catalog["programs"]
    }
    selected_programs = []
    for name in payload.program_names:
        program = programs_by_name.get(name.casefold())
        if program is None:
            raise HTTPException(
                status_code=400,
                detail=f"Selected program is unavailable: {name}",
            )
        selected_programs.append(program)

    try:
        selected_post_powershell = resolve_profile_scripts(
            session, payload.post_powershell_names
        )
    except PostPowerShellError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    selected_driver = None
    if payload.driver_package is not None:
        selected_driver = next(
            (
                item
                for item in catalog.get("drivers", [])
                if item["relativePath"].casefold()
                == payload.driver_package.casefold()
            ),
            None,
        )
        if selected_driver is None:
            raise HTTPException(
                status_code=400,
                detail="Selected driver package is unavailable",
            )

    deployment.image_name = image["name"]
    image_apply_mode = image_config.get("imageApplyMode", "direct")
    if image_apply_mode not in {"direct", "staged"}:
        image_apply_mode = "direct"
    image_sha256 = str(image.get("sha256") or "").strip()
    if image_apply_mode == "staged" and re.fullmatch(
        r"[0-9a-fA-F]{64}", image_sha256
    ) is None:
        raise HTTPException(
            status_code=503,
            detail="Selected image SHA-256 is unavailable for staged deployment",
        )
    deployment.image_apply_mode = image_apply_mode
    session.execute(
        delete(DeploymentPowerShellResult).where(
            DeploymentPowerShellResult.deployment_id == deployment.id
        )
    )
    post_powershell_plan = []
    for position, script in enumerate(selected_post_powershell):
        session.add(
            DeploymentPowerShellResult(
                deployment_id=deployment.id,
                script_id=script["id"],
                position=position,
                name=script["name"],
                selection_mode=script["selectionMode"],
                run_phase=script["runPhase"],
                arguments=script["arguments"],
                timeout_seconds=script["timeoutSeconds"],
                size_bytes=script["size"],
                sha256=script["sha256"],
                status="pending",
                output_bytes=0,
                output_total_bytes=0,
                output_truncated=False,
            )
        )
        post_powershell_plan.append(
            {
                "position": position,
                "name": script["name"],
                "selectionMode": script["selectionMode"],
                "runPhase": script["runPhase"],
                "arguments": script["arguments"],
                "timeoutSeconds": script["timeoutSeconds"],
                "size": script["size"],
                "sha256": script["sha256"],
                "maxOutputBytes": MAX_OUTPUT_SIZE_BYTES,
                "downloadUrl": (
                    f"/api/deploy/{deployment.id}/post-powershell/"
                    f"{position}/script"
                ),
                "reportUrl": (
                    f"/api/deploy/{deployment.id}/post-powershell/"
                    f"{position}/report"
                ),
            }
        )
    update_computer_inventory(session, deployment)
    session.commit()
    return {
        "deploymentId": deployment.id,
        "imageApplyMode": image_apply_mode,
        "image": image,
        "programs": selected_programs,
        "postPowerShell": post_powershell_plan,
        "driverPackage": selected_driver,
        "postinstall": {
            "localAdminName": deployment_profile["localAdminName"],
            "enableBuiltInAdministrator": deployment_profile[
                "enableBuiltInAdministrator"
            ],
            "enableSetupLocalAdmin": deployment_profile["enableSetupLocalAdmin"],
        },
    }


@app.get("/api/deployments", response_model=DeploymentListResponse)
def deployment_list(
    limit: int = Query(default=500, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> DeploymentListResponse:
    expire_stale_deployments(session)
    deployments = session.scalars(
        select(Deployment).order_by(Deployment.id.desc()).limit(limit)
    ).all()
    deployment_ids = [deployment.id for deployment in deployments]
    stages_by_deployment: dict[int, list[DeploymentStage]] = {
        deployment_id: [] for deployment_id in deployment_ids
    }
    if deployment_ids:
        stages = session.scalars(
            select(DeploymentStage)
            .where(DeploymentStage.deployment_id.in_(deployment_ids))
            .order_by(DeploymentStage.id)
        ).all()
        for stage in stages:
            stages_by_deployment[stage.deployment_id].append(stage)
    programs_by_deployment: dict[int, list[DeploymentProgram]] = {
        deployment_id: [] for deployment_id in deployment_ids
    }
    if deployment_ids:
        programs = session.scalars(
            select(DeploymentProgram)
            .where(DeploymentProgram.deployment_id.in_(deployment_ids))
            .order_by(DeploymentProgram.deployment_id, DeploymentProgram.position)
        ).all()
        for program in programs:
            programs_by_deployment[program.deployment_id].append(program)
    powershell_by_deployment: dict[int, list[DeploymentPowerShellResult]] = {
        deployment_id: [] for deployment_id in deployment_ids
    }
    if deployment_ids:
        powershell_results = session.scalars(
            select(DeploymentPowerShellResult)
            .where(DeploymentPowerShellResult.deployment_id.in_(deployment_ids))
            .order_by(
                DeploymentPowerShellResult.deployment_id,
                DeploymentPowerShellResult.position,
            )
        ).all()
        for script_result in powershell_results:
            powershell_by_deployment[script_result.deployment_id].append(script_result)

    total, begin_count, completed_count, failed_count = session.execute(
        select(
            func.count(Deployment.id),
            func.coalesce(
                func.sum(case((Deployment.status == DEPLOYMENT_BEGIN, 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(
                    case((Deployment.status == DEPLOYMENT_COMPLETED, 1), else_=0)
                ),
                0,
            ),
            func.coalesce(
                func.sum(case((Deployment.status == DEPLOYMENT_FAILED, 1), else_=0)),
                0,
            ),
        )
    ).one()

    return DeploymentListResponse(
        total=total,
        begin=begin_count,
        completed=completed_count,
        failed=failed_count,
        items=[
            to_deployment_list_item(
                deployment=deployment,
                stages=stages_by_deployment[deployment.id],
                programs=programs_by_deployment[deployment.id],
                post_powershell=powershell_by_deployment[deployment.id],
            )
            for deployment in deployments
        ],
    )


@app.get(
    "/api/deployments/{deployment_id}",
    response_model=DeploymentListItem,
)
def deployment_detail(
    deployment_id: int,
    session: Session = Depends(get_session),
) -> DeploymentListItem:
    expire_stale_deployments(session)
    deployment = session.get(Deployment, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="Deployment not found")

    stages = session.scalars(
        select(DeploymentStage)
        .where(DeploymentStage.deployment_id == deployment_id)
        .order_by(DeploymentStage.id)
    ).all()
    programs = session.scalars(
        select(DeploymentProgram)
        .where(DeploymentProgram.deployment_id == deployment_id)
        .order_by(DeploymentProgram.position)
    ).all()
    powershell_results = session.scalars(
        select(DeploymentPowerShellResult)
        .where(DeploymentPowerShellResult.deployment_id == deployment_id)
        .order_by(DeploymentPowerShellResult.position)
    ).all()
    network_summary = session.get(DeploymentNetworkSummary, deployment_id)
    network_stages = session.scalars(
        select(DeploymentNetworkStage)
        .where(DeploymentNetworkStage.deployment_id == deployment_id)
        .order_by(DeploymentNetworkStage.id)
    ).all()
    return to_deployment_list_item(
        deployment=deployment,
        stages=stages,
        programs=programs,
        post_powershell=powershell_results,
        network_summary=network_summary,
        network_stages=network_stages,
    )


@app.get("/api/computers", response_model=ComputerListResponse)
def computer_list(
    limit: int = Query(default=500, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> ComputerListResponse:
    computers = session.scalars(
        select(Computer).order_by(Computer.last_seen_at.desc()).limit(limit)
    ).all()
    total = session.scalar(select(func.count(Computer.id))) or 0
    return ComputerListResponse(
        total=total,
        items=[to_computer_list_item(computer) for computer in computers],
    )


@app.post(
    "/api/deploy/begin",
    response_model=DeploymentResponse,
    status_code=status.HTTP_201_CREATED,
)
def deploy_begin(
    payload: DeploymentBeginRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> DeploymentResponse:
    if request.client is None:
        raise HTTPException(status_code=400, detail="Client IP is unavailable")
    token = require_deployment_token(request, session, "authorized")
    if token.deployment_id is not None:
        raise HTTPException(
            status_code=409,
            detail="This WinPE login has already been used for a deployment.",
        )

    # Reject an impossible domain join before WinPE erases the selected disk,
    # rather than once the deployment has already destroyed the target.
    if payload.domain_join and not get_settings().odj_enabled:
        raise HTTPException(
            status_code=409,
            detail=(
                "Domain join was requested, but Offline Domain Join is not "
                "configured on IronAPI."
            ),
        )

    deployment = Deployment(
        computer_name=payload.computer_name,
        serial_number=payload.serial_number,
        model=payload.model,
        manufacturer=payload.manufacturer,
        system_sku=payload.system_sku,
        mac_address=payload.mac_address,
        ip_address=request.client.host,
        image_name=payload.image_name,
        target_disk_number=payload.target_disk_number,
        target_disk_model=payload.target_disk_model,
        target_disk_size_bytes=payload.target_disk_size_bytes,
        domain_join=payload.domain_join,
        status=DEPLOYMENT_BEGIN,
    )
    session.add(deployment)
    session.flush()
    update_computer_inventory(session, deployment)
    try:
        bind_deployment_token(session, token, deployment.id)
    except AuthError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.refresh(deployment)
    return to_deployment_response(deployment)


@app.get("/api/deploy/{deployment_id}/smb-credentials")
def deployment_smb_credentials(
    deployment_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    get_domain_join_deployment(
        deployment_id,
        request,
        session,
        require_domain_join=False,
    )
    settings = get_settings()
    if not settings.smb_share_path or not settings.smb_user or not settings.smb_password:
        raise HTTPException(
            status_code=503,
            detail="SMB deployment credentials are not configured in IronAPI.",
        )
    return JSONResponse(
        {
            "share_path": settings.smb_share_path,
            "username": settings.smb_user,
            "password": settings.smb_password,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/deploy/{deployment_id}/unattend")
def deployment_unattend(
    deployment_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    deployment = get_domain_join_deployment(
        deployment_id,
        request,
        session,
        require_domain_join=False,
    )
    template_path = (
        SERVER_TEMPLATES_ROOT
        / "Unattend"
        / "unattend-win11-template.xml"
    )
    try:
        content = template_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail="Windows unattend template is unavailable.",
        ) from exc
    if "COMPUTER_NAME" not in content:
        raise HTTPException(
            status_code=503,
            detail="Windows unattend template has no COMPUTER_NAME placeholder.",
        )
    return Response(
        content.replace("COMPUTER_NAME", deployment.computer_name),
        media_type="application/xml",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/deploy/{deployment_id}/post-powershell/{position}/script")
def deployment_post_powershell_script(
    deployment_id: int,
    position: int,
    request: Request,
    session: Session = Depends(get_session),
) -> FileResponse:
    require_owned_deployment(deployment_id, request, session, "winpe")
    result = session.scalar(
        select(DeploymentPowerShellResult).where(
            DeploymentPowerShellResult.deployment_id == deployment_id,
            DeploymentPowerShellResult.position == position,
        )
    )
    if result is None:
        raise HTTPException(status_code=404, detail="PowerShell script not found.")
    try:
        path = verified_script_path(result.name, result.size_bytes, result.sha256)
    except PostPowerShellError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="text/plain",
        filename=result.name,
        headers={
            "Cache-Control": "no-store",
            "X-IronDeploy-SHA256": result.sha256,
        },
    )


def deployment_postinstall_file(
    deployment_id: int,
    request: Request,
    session: Session,
    filename: str,
) -> FileResponse:
    get_domain_join_deployment(
        deployment_id,
        request,
        session,
        require_domain_join=False,
    )
    path = SERVER_TEMPLATES_ROOT / "PostInstall" / filename
    if not path.is_file():
        raise HTTPException(
            status_code=503,
            detail=f"Post-install template is unavailable: {filename}",
        )
    return FileResponse(
        path,
        filename=filename,
        media_type="text/plain",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/deploy/{deployment_id}/postinstall/setup-complete")
def deployment_setup_complete(
    deployment_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> FileResponse:
    return deployment_postinstall_file(
        deployment_id,
        request,
        session,
        "SetupComplete.cmd",
    )


@app.get("/api/deploy/{deployment_id}/postinstall/script")
def deployment_postinstall_script(
    deployment_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> FileResponse:
    return deployment_postinstall_file(
        deployment_id,
        request,
        session,
        "postinstall.ps1",
    )


@app.post(
    "/api/deploy/{deployment_id}/image",
    response_model=DeploymentResponse,
)
def deploy_image_selected(
    deployment_id: int,
    payload: DeploymentImageRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> DeploymentResponse:
    deployment = get_domain_join_deployment(
        deployment_id,
        request,
        session,
        require_domain_join=False,
    )
    deployment.image_name = payload.image_name
    update_computer_inventory(session, deployment)
    session.commit()
    session.refresh(deployment)
    return to_deployment_response(deployment)


def _apply_network_aggregate(record, aggregate) -> None:
    for name in (
        "started_at",
        "completed_at",
        "duration_seconds",
        "icmp_status",
        "ping_sent",
        "ping_received",
        "ping_lost",
        "loss_percentage",
        "rtt_min_ms",
        "rtt_avg_ms",
        "rtt_max_ms",
        "latency_spikes",
        "bytes_received",
        "average_inbound_mbps",
        "link_utilization_percent",
    ):
        setattr(record, name, getattr(aggregate, name))


def _apply_network_adapter(summary, adapter, prefix: str) -> None:
    values = adapter.model_dump() if adapter is not None else {}
    for source, destination in (
        ("name", f"{prefix}_adapter_name"),
        ("description", f"{prefix}_adapter_description"),
        ("adapter_id", f"{prefix}_adapter_id"),
        ("local_ip", f"{prefix}_local_ip"),
        ("link_speed_bps", f"{prefix}_link_speed_bps"),
    ):
        setattr(summary, destination, values.get(source))


def _upsert_deployment_network_stage(
    session: Session,
    deployment_id: int,
    payload: NetworkStageReport,
) -> DeploymentNetworkStage:
    stage = session.scalar(
        select(DeploymentNetworkStage).where(
            DeploymentNetworkStage.deployment_id == deployment_id,
            DeploymentNetworkStage.stage == payload.stage,
        )
    )
    if stage is None:
        stage = DeploymentNetworkStage(
            deployment_id=deployment_id,
            stage=payload.stage,
        )
        session.add(stage)
    _apply_network_aggregate(stage, payload)
    return stage


@app.put(
    "/api/deploy/{deployment_id}/network-diagnostics/adapters",
    response_model=DeploymentNetworkAdaptersReport,
)
def save_deployment_network_adapters(
    deployment_id: int,
    payload: DeploymentNetworkAdaptersReport,
    request: Request,
    session: Session = Depends(get_session),
) -> DeploymentNetworkAdaptersReport:
    require_owned_deployment(
        deployment_id,
        request,
        session,
        "winpe",
    )
    summary = session.get(DeploymentNetworkSummary, deployment_id)
    if summary is None:
        summary = DeploymentNetworkSummary(deployment_id=deployment_id)
        session.add(summary)

    summary.adapters_differ = payload.adapters_differ
    _apply_network_adapter(summary, payload.smb_adapter, "smb")
    _apply_network_adapter(summary, payload.api_adapter, "api")
    session.commit()
    session.refresh(summary)
    return DeploymentNetworkAdaptersReport(
        smb_adapter=payload.smb_adapter,
        api_adapter=payload.api_adapter,
        adapters_differ=summary.adapters_differ,
    )


@app.put(
    "/api/deploy/{deployment_id}/network-diagnostics/stages/{stage_name}",
    response_model=NetworkStageReport,
)
def save_deployment_network_stage(
    deployment_id: int,
    stage_name: str,
    payload: NetworkStageReport,
    request: Request,
    session: Session = Depends(get_session),
) -> NetworkStageReport:
    require_owned_deployment(
        deployment_id,
        request,
        session,
        "winpe",
        "postinstall",
    )
    if stage_name != payload.stage:
        raise HTTPException(
            status_code=400,
            detail="Network diagnostic stage does not match the URL.",
        )
    stage = _upsert_deployment_network_stage(
        session,
        deployment_id,
        payload,
    )
    session.commit()
    session.refresh(stage)
    return to_network_stage_response(stage)


@app.put(
    "/api/deploy/{deployment_id}/network-diagnostics",
    response_model=DeploymentNetworkDiagnosticsResponse,
)
def save_deployment_network_diagnostics(
    deployment_id: int,
    payload: DeploymentNetworkDiagnosticsRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> DeploymentNetworkDiagnosticsResponse:
    require_owned_deployment(
        deployment_id,
        request,
        session,
        "winpe",
        "postinstall",
    )
    summary = session.get(DeploymentNetworkSummary, deployment_id)
    if summary is None:
        summary = DeploymentNetworkSummary(deployment_id=deployment_id)
        session.add(summary)

    summary.ping_target = payload.ping_target
    summary.adapters_differ = payload.adapters_differ
    _apply_network_adapter(summary, payload.smb_adapter, "smb")
    _apply_network_adapter(summary, payload.api_adapter, "api")
    _apply_network_aggregate(summary, payload.overall)
    summary.api_request_count = payload.api.request_count
    summary.api_error_count = payload.api.error_count
    summary.api_min_ms = payload.api.min_ms
    summary.api_avg_ms = payload.api.avg_ms
    summary.api_max_ms = payload.api.max_ms
    summary.smb_connect_success = payload.smb.success
    summary.smb_connect_attempts = payload.smb.attempts
    summary.smb_connect_duration_ms = payload.smb.duration_ms
    summary.smb_error_message = payload.smb.error_message
    summary.diagnostic_errors = list(payload.diagnostic_errors)

    for stage_payload in payload.stages:
        _upsert_deployment_network_stage(
            session,
            deployment_id,
            stage_payload,
        )

    session.commit()
    session.refresh(summary)
    network_stages = session.scalars(
        select(DeploymentNetworkStage)
        .where(DeploymentNetworkStage.deployment_id == deployment_id)
        .order_by(DeploymentNetworkStage.id)
    ).all()
    response = to_network_diagnostics_response(summary, network_stages)
    if response is None:
        raise HTTPException(
            status_code=500,
            detail="Network diagnostics could not be stored.",
        )
    return response


@app.post(
    "/api/deploy/{deployment_id}/error",
    response_model=DeploymentResponse,
)
def deploy_error(
    deployment_id: int,
    payload: DeploymentErrorRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> DeploymentResponse:
    deployment = get_domain_join_deployment(
        deployment_id,
        request,
        session,
        require_domain_join=False,
        allowed_phases=("winpe", "postinstall"),
    )
    failed_at = datetime.now(timezone.utc)
    deployment.status = DEPLOYMENT_FAILED
    deployment.completed_at = failed_at
    deployment.last_error_message = payload.message

    if payload.stage:
        stage_record = session.scalar(
            select(DeploymentStage).where(
                DeploymentStage.deployment_id == deployment_id,
                DeploymentStage.stage == payload.stage,
            )
        )
        if stage_record is None:
            stage_record = DeploymentStage(
                deployment_id=deployment_id,
                stage=payload.stage,
                phase="winpe",
                status=STAGE_FAILED,
                started_at=failed_at,
                completed_at=failed_at,
                error_message=payload.message,
            )
            session.add(stage_record)
        else:
            stage_record.status = STAGE_FAILED
            stage_record.completed_at = failed_at
            stage_record.error_message = payload.message

    token = require_deployment_token(request, session, "winpe", "postinstall")

    # A failed deployment will never acknowledge, so its blob would otherwise
    # sit in the pending directory forever. Deleting it is irreversible, so do
    # it only once the request is known to be accepted.
    if deployment.domain_join:
        discard_domain_join_blob(deployment.computer_name)

    session.commit()
    session.refresh(deployment)
    revoke_deployment_token(session, token)
    return to_deployment_response(deployment)


def get_domain_join_deployment(
    deployment_id: int,
    request: Request,
    session: Session,
    require_domain_join: bool = True,
    allowed_phases: tuple[str, ...] = ("winpe",),
) -> Deployment:
    expire_stale_deployments(session)
    deployment, _ = require_owned_deployment(
        deployment_id,
        request,
        session,
        *allowed_phases,
    )
    if require_domain_join and not deployment.domain_join:
        raise HTTPException(
            status_code=409,
            detail="Domain join is disabled for this deployment",
        )
    if request.client is None or request.client.host != deployment.ip_address:
        raise HTTPException(
            status_code=403,
            detail="Deployment belongs to another client",
        )
    if deployment.status != DEPLOYMENT_BEGIN:
        raise HTTPException(
            status_code=409,
            detail=f"Deployment is already {deployment.status}",
        )
    return deployment


@app.post(
    "/api/deploy/{deployment_id}/domain-join/provision",
    response_model=DomainJoinProvisionResponse,
)
def domain_join_provision(
    deployment_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> DomainJoinProvisionResponse:
    deployment = get_domain_join_deployment(deployment_id, request, session)
    settings = get_settings()
    try:
        reuse_existing_account = computer_exists(
            settings,
            deployment.computer_name,
        )
    except DirectoryLookupError as exc:
        access_logger.error(
            "ODJ account detection failed for deployment=%d machine=%s: %s",
            deployment.id,
            deployment.computer_name,
            exc,
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        provision_domain_join_blob(
            settings,
            deployment.computer_name,
            reuse_existing_account=reuse_existing_account,
        )
    except DomainJoinError as exc:
        access_logger.error(
            "ODJ provisioning failed for deployment=%d machine=%s: %s",
            deployment.id,
            deployment.computer_name,
            exc,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return DomainJoinProvisionResponse(
        computer_name=deployment.computer_name,
        status="ready",
        blob_url=f"/api/deploy/{deployment.id}/domain-join/blob",
    )


@app.get(
    "/api/deploy/{deployment_id}/domain-join/blob",
    response_class=FileResponse,
)
def domain_join_download(
    deployment_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> FileResponse:
    deployment = get_domain_join_deployment(deployment_id, request, session)
    try:
        blob_path = get_domain_join_blob(
            get_settings(),
            deployment.computer_name,
        )
    except DomainJoinError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return FileResponse(
        blob_path,
        filename=f"{deployment.computer_name}.txt",
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/api/deploy/{deployment_id}/domain-join/acknowledge",
    response_model=DomainJoinAcknowledgeResponse,
)
def domain_join_acknowledge(
    deployment_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> DomainJoinAcknowledgeResponse:
    deployment = get_domain_join_deployment(deployment_id, request, session)
    try:
        deleted = delete_domain_join_blob(
            get_settings(),
            deployment.computer_name,
        )
    except DomainJoinError as exc:
        access_logger.error(
            "ODJ blob deletion failed for deployment=%d machine=%s: %s",
            deployment.id,
            deployment.computer_name,
            exc,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return DomainJoinAcknowledgeResponse(
        computer_name=deployment.computer_name,
        status="deleted" if deleted else "already_deleted",
    )


@app.post(
    "/api/deploy/{deployment_id}/stages/{stage}/{event}",
    response_model=DeploymentStageResponse,
)
def deploy_stage_event(
    deployment_id: int,
    stage: WinPEStageCode,
    event: DeploymentStageEvent,
    request: Request,
    session: Session = Depends(get_session),
) -> DeploymentStageResponse:
    expire_stale_deployments(session)
    deployment, _ = require_owned_deployment(
        deployment_id, request, session, "winpe"
    )

    stage_record = session.scalar(
        select(DeploymentStage).where(
            DeploymentStage.deployment_id == deployment_id,
            DeploymentStage.stage == stage,
        )
    )
    if deployment.status != DEPLOYMENT_BEGIN:
        if stage_record is not None:
            return to_deployment_stage_response(stage_record)
        raise HTTPException(
            status_code=409,
            detail=f"Deployment is already {deployment.status}",
        )

    now = datetime.now(timezone.utc)

    if event == "start":
        if stage_record is None:
            session.execute(
                update(DeploymentStage)
                .where(
                    DeploymentStage.deployment_id == deployment_id,
                    DeploymentStage.status == STAGE_RUNNING,
                    DeploymentStage.stage != stage,
                )
                .values(
                    status=STAGE_COMPLETED,
                    completed_at=now,
                )
            )
            stage_record = DeploymentStage(
                deployment_id=deployment_id,
                stage=stage,
                phase="winpe",
                status=STAGE_RUNNING,
                started_at=now,
            )
            session.add(stage_record)
    elif stage_record is None:
        event_status = {
            "complete": STAGE_COMPLETED,
            "fail": STAGE_FAILED,
            "skip": STAGE_SKIPPED,
        }[event]
        stage_record = DeploymentStage(
            deployment_id=deployment_id,
            stage=stage,
            phase="winpe",
            status=event_status,
            started_at=now,
            completed_at=now,
        )
        session.add(stage_record)
    elif stage_record.status == STAGE_RUNNING:
        stage_record.status = {
            "complete": STAGE_COMPLETED,
            "fail": STAGE_FAILED,
            "skip": STAGE_SKIPPED,
        }[event]
        stage_record.completed_at = now

    session.commit()
    session.refresh(stage_record)
    return to_deployment_stage_response(stage_record)


@app.post("/api/deploy/{deployment_id}/postinstall")
def deploy_enter_postinstall(
    deployment_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, str]:
    deployment, token = require_owned_deployment(
        deployment_id, request, session, "winpe", "postinstall"
    )
    if deployment.status != DEPLOYMENT_BEGIN:
        raise HTTPException(status_code=409, detail="Deployment is not active")
    if token.phase == "winpe":
        set_deployment_token_phase(session, token, "postinstall")
    return {"status": "postinstall"}


@app.post("/api/deploy/{deployment_id}/post-powershell/{position}/report")
async def deploy_post_powershell_report(
    deployment_id: int,
    position: int,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    require_owned_deployment(deployment_id, request, session, "postinstall")
    result = session.scalar(
        select(DeploymentPowerShellResult).where(
            DeploymentPowerShellResult.deployment_id == deployment_id,
            DeploymentPowerShellResult.position == position,
        )
    )
    if result is None:
        raise HTTPException(status_code=404, detail="PowerShell result not found.")

    report_status = request.headers.get("x-irondeploy-status", "").strip().lower()
    if report_status not in {
        "succeeded",
        "failed",
        "timed_out",
        "hash_mismatch",
        "download_failed",
    }:
        raise HTTPException(status_code=400, detail="Invalid PowerShell status.")
    try:
        duration_seconds = int(
            request.headers.get("x-irondeploy-duration-seconds", "0")
        )
        total_bytes = int(request.headers.get("x-irondeploy-output-total-bytes", "0"))
        exit_code_text = request.headers.get("x-irondeploy-exit-code", "").strip()
        exit_code = int(exit_code_text) if exit_code_text else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid PowerShell metrics.") from exc
    if not 0 <= duration_seconds <= 86400 or total_bytes < 0:
        raise HTTPException(status_code=400, detail="Invalid PowerShell metrics.")
    output_truncated = request.headers.get(
        "x-irondeploy-output-truncated", "false"
    ).lower() in {"1", "true", "yes"}
    error_message = unquote(request.headers.get("x-irondeploy-error", ""))[:4000] or None

    destination = result_log_path(deployment_id, position)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.urandom(8).hex()}.tmp")
    output_bytes = 0
    try:
        with temporary.open("xb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                output_bytes += len(chunk)
                if output_bytes > MAX_OUTPUT_SIZE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="PowerShell output exceeds the 20 MiB report limit.",
                    )
                handle.write(chunk)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    result.status = report_status
    result.exit_code = exit_code
    result.duration_seconds = duration_seconds
    result.output_bytes = output_bytes
    result.output_total_bytes = max(total_bytes, output_bytes)
    result.output_truncated = output_truncated
    result.error_message = error_message
    result.reported_at = datetime.now(timezone.utc)
    session.commit()
    return {
        "accepted": True,
        "position": position,
        "status": report_status,
        "outputBytes": output_bytes,
    }


@app.get("/api/deployments/{deployment_id}/post-powershell/{position}/output")
def deployment_post_powershell_output(
    deployment_id: int,
    position: int,
    session: Session = Depends(get_session),
) -> FileResponse:
    result = session.scalar(
        select(DeploymentPowerShellResult).where(
            DeploymentPowerShellResult.deployment_id == deployment_id,
            DeploymentPowerShellResult.position == position,
        )
    )
    if result is None or result.reported_at is None:
        raise HTTPException(status_code=404, detail="PowerShell output not found.")
    path = result_log_path(deployment_id, position)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="PowerShell output file is missing.")
    return FileResponse(
        path,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/api/deploy/{deployment_id}/complete",
    response_model=DeploymentResponse,
)
def deploy_complete(
    deployment_id: int,
    request: Request,
    session: Session = Depends(get_session),
    report: DeploymentCompleteRequest | None = None,
) -> DeploymentResponse:
    completed_at = datetime.now(timezone.utc)

    if getattr(request.state, "deployment_completion_replay", False):
        token_id = getattr(request.state, "deployment_token_id", None)
        token = session.get(DeploymentToken, token_id) if token_id else None
        deployment = session.get(Deployment, deployment_id)
        if (
            token is None
            or token.deployment_id != deployment_id
            or token.phase != "completed"
            or token.revoked_at is None
            or token.expires_at is None
            or as_utc(token.expires_at) <= completed_at
            or deployment is None
            or deployment.status != DEPLOYMENT_COMPLETED
        ):
            raise HTTPException(
                status_code=401,
                detail="Deployment completion receipt is invalid.",
            )
        return to_deployment_response(deployment)

    expire_stale_deployments(session, completed_at)
    deployment, token = require_owned_deployment(
        deployment_id, request, session, "postinstall"
    )

    if report is not None:
        session.execute(
            delete(DeploymentProgram).where(
                DeploymentProgram.deployment_id == deployment_id
            )
        )
        for position, program in enumerate(report.programs):
            session.add(
                DeploymentProgram(
                    deployment_id=deployment_id,
                    position=position,
                    name=program.name,
                    status=program.status,
                    exit_code=program.exit_code,
                    duration_seconds=program.duration_seconds,
                    reason=program.reason,
                    error_message=program.error_message,
                )
            )
    session.execute(
        update(Deployment)
        .where(
            Deployment.id == deployment_id,
            Deployment.status == DEPLOYMENT_BEGIN,
        )
        .values(
            status=DEPLOYMENT_COMPLETED,
            completed_at=completed_at,
        )
    )
    session.execute(
        update(DeploymentStage)
        .where(
            DeploymentStage.deployment_id == deployment_id,
            DeploymentStage.status == STAGE_RUNNING,
        )
        .values(
            status=STAGE_COMPLETED,
            completed_at=completed_at,
        )
    )
    token.phase = "completed"
    token.last_seen_at = completed_at
    token.revoked_at = completed_at
    token.expires_at = completed_at + DEPLOYMENT_COMPLETION_RECEIPT_TTL

    # WinPE normally acknowledges right after applying the blob; this covers the
    # case where that call was lost but the deployment still reached Windows.
    if deployment.domain_join:
        discard_domain_join_blob(deployment.computer_name)

    session.commit()

    session.refresh(deployment)
    return to_deployment_response(deployment)
