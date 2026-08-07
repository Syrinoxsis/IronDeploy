"""Windows Service host and registration helper for IronAPI."""

from __future__ import annotations

import contextlib
import ipaddress
import os
from pathlib import Path
import sys
import threading
import traceback

from dotenv import load_dotenv
import servicemanager
import uvicorn
import win32service
import win32serviceutil


API_ROOT = Path(__file__).resolve().parent
IRONDEPLOY_ROOT = API_ROOT.parent
ENV_PATH = API_ROOT / ".env"
LOG_PATH = IRONDEPLOY_ROOT / "Logs" / "IronAPI-service.log"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

SERVICE_NAME = "IronAPI"
SERVICE_DISPLAY_NAME = "IronDeploy API"
SERVICE_DESCRIPTION = "IronDeploy deployment and management API"


def _dotenv_value(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


def _read_listener_settings() -> tuple[str, int, bool, str]:
    if not ENV_PATH.is_file():
        raise RuntimeError(f"IronAPI configuration is missing: {ENV_PATH}")

    load_dotenv(ENV_PATH, override=True)
    access_mode = _dotenv_value("IRONAPI_ACCESS_MODE", "http_direct")
    cookie_secure = _dotenv_value("IRONAPI_COOKIE_SECURE", "false").lower()

    if access_mode == "https_proxy":
        if cookie_secure != "true":
            raise RuntimeError(
                "https_proxy requires IRONAPI_COOKIE_SECURE=true. "
                "Save the SetupWeb configuration."
            )
        host = "127.0.0.1"
        port = 8000
    elif access_mode == "http_direct":
        if cookie_secure != "false":
            raise RuntimeError(
                "http_direct requires IRONAPI_COOKIE_SECURE=false."
            )
        host = _dotenv_value("IRONAPI_BIND_HOST", "0.0.0.0")
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise RuntimeError(
                "IRONAPI_BIND_HOST must be an IP address."
            ) from exc
        if address.is_loopback:
            raise RuntimeError(
                "http_direct requires a non-loopback bind address."
            )
        try:
            port = int(_dotenv_value("IRONAPI_PORT", "8000"))
        except ValueError as exc:
            raise RuntimeError("IRONAPI_PORT must be an integer.") from exc
        if not 1 <= port <= 65535:
            raise RuntimeError("IRONAPI_PORT must be between 1 and 65535.")
    else:
        raise RuntimeError(
            "IRONAPI_ACCESS_MODE must be http_direct or https_proxy."
        )

    access_log = _dotenv_value(
        "IRONAPI_ACCESS_LOG",
        "false",
    ).lower() in {"1", "true", "yes", "on"}
    return host, port, access_log, access_mode


class IronAPIService(win32serviceutil.ServiceFramework):
    """Run Uvicorn as the IronAPI Windows Service."""

    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY_NAME
    _svc_description_ = SERVICE_DESCRIPTION

    def __init__(self, args: list[str]) -> None:
        super().__init__(args)
        self._lock = threading.Lock()
        self._server: uvicorn.Server | None = None
        self._stop_requested = False

    def SvcStop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        with self._lock:
            self._stop_requested = True
            if self._server is not None:
                self._server.should_exit = True

    def SvcShutdown(self) -> None:
        self.SvcStop()

    def SvcDoRun(self) -> None:
        try:
            self._run_server()
        except Exception:
            details = traceback.format_exc()
            servicemanager.LogErrorMsg(
                f"{SERVICE_DISPLAY_NAME} failed:\n{details}"
            )
            raise

    def _run_server(self) -> None:
        host, port, access_log, access_mode = _read_listener_settings()
        for relative_path in ("Data", "ODJ/pending", "Logs"):
            (IRONDEPLOY_ROOT / relative_path).mkdir(
                parents=True,
                exist_ok=True,
            )

        os.chdir(API_ROOT)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8", buffering=1) as log_file:
            with contextlib.redirect_stdout(log_file):
                with contextlib.redirect_stderr(log_file):
                    print(
                        f"Starting {SERVICE_DISPLAY_NAME}: "
                        f"{access_mode} {host}:{port}",
                        flush=True,
                    )
                    config = uvicorn.Config(
                        "app.main:app",
                        host=host,
                        port=port,
                        access_log=access_log,
                    )
                    server = uvicorn.Server(config)
                    with self._lock:
                        self._server = server
                        if self._stop_requested:
                            server.should_exit = True

                    servicemanager.LogInfoMsg(
                        f"{SERVICE_DISPLAY_NAME} listening on {host}:{port}"
                    )
                    server.run()

                    with self._lock:
                        stopped_by_scm = self._stop_requested
                        self._server = None
                    if not stopped_by_scm:
                        raise RuntimeError(
                            "Uvicorn stopped without a service stop request."
                        )
                    print(f"Stopped {SERVICE_DISPLAY_NAME}.", flush=True)


def _service_class_string() -> str:
    module_path = Path(__file__).resolve().with_suffix("")
    return f"{module_path}.{IronAPIService.__name__}"


def _read_registration_secret() -> tuple[str, str]:
    mode = sys.stdin.readline().rstrip("\r\n")
    account = sys.stdin.readline().rstrip("\r\n")
    password = sys.stdin.readline().rstrip("\r\n")

    if mode != "account":
        raise ValueError("Unknown service account mode.")
    # Both a domain and a local identity are qualified, so a name without a
    # backslash would let the Service Control Manager resolve it elsewhere.
    # Built-in identities such as LocalSystem are rejected by the installer.
    if "\\" not in account or not password:
        raise ValueError(
            "A DOMAIN\\user or COMPUTER\\user account and a non-empty "
            "password are required."
        )
    return account, password


def _register_service(action: str) -> None:
    account, password = _read_registration_secret()
    common = {
        "pythonClassString": _service_class_string(),
        "serviceName": SERVICE_NAME,
        "startType": win32service.SERVICE_AUTO_START,
        "userName": account,
        "password": password,
        "description": SERVICE_DESCRIPTION,
        "delayedstart": False,
        # pythonservice.exe does not reliably load pywin32.pth from a virtual
        # environment and can fail before importing the service class with
        # "No module named 'servicemanager'".  Host the service with the
        # virtual environment's regular interpreter instead; normal Python
        # startup processes the .pth file before this script imports pywin32.
        "exeName": sys.executable,
        "exeArgs": f'"{Path(__file__).resolve()}"',
    }

    if action == "install-from-stdin":
        win32serviceutil.InstallService(
            displayName=SERVICE_DISPLAY_NAME,
            **common,
        )
        print(f"Installed Windows service {SERVICE_NAME}.")
        return
    if action == "update-from-stdin":
        win32serviceutil.ChangeServiceConfig(
            displayName=SERVICE_DISPLAY_NAME,
            **common,
        )
        print(f"Updated Windows service {SERVICE_NAME}.")
        return
    raise ValueError("Unsupported registration action.")


def main() -> None:
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(IronAPIService)
        servicemanager.StartServiceCtrlDispatcher()
        return

    if len(sys.argv) != 2:
        raise SystemExit(
            "This helper is managed by 5. Install-IronAPIService.ps1."
        )
    _register_service(sys.argv[1])


if __name__ == "__main__":
    main()
