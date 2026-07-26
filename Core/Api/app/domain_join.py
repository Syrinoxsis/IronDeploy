import locale
import subprocess
from pathlib import Path
from threading import Lock

from app.config import Settings


class DomainJoinError(RuntimeError):
    pass


_provision_lock = Lock()


def domain_join_blob_path(settings: Settings, computer_name: str) -> Path:
    return settings.odj_blob_dir / f"{computer_name}.txt"


def provision_domain_join_blob(
    settings: Settings,
    computer_name: str,
    reuse_existing_account: bool = False,
) -> Path:
    blob_path = domain_join_blob_path(settings, computer_name)

    with _provision_lock:
        if _is_nonempty_file(blob_path):
            return blob_path

        if not settings.odj_djoin_path.is_file():
            raise DomainJoinError(
                f"djoin.exe not found: {settings.odj_djoin_path}"
            )

        settings.odj_blob_dir.mkdir(parents=True, exist_ok=True)
        if blob_path.exists():
            blob_path.unlink()

        command = [
            str(settings.odj_djoin_path),
            "/provision",
            "/domain",
            settings.odj_domain,
            "/machine",
            computer_name,
        ]
        if reuse_existing_account:
            command.append("/reuse")
        else:
            command.extend([
                "/machineou",
                settings.odj_machine_ou,
            ])
        command.extend([
            "/savefile",
            str(blob_path),
        ])

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                encoding=locale.getpreferredencoding(False),
                errors="replace",
                timeout=settings.odj_provision_timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            blob_path.unlink(missing_ok=True)
            raise DomainJoinError(
                f"Failed to run djoin.exe: {exc}"
            ) from exc

        if completed.returncode != 0:
            blob_path.unlink(missing_ok=True)
            detail = (completed.stderr or completed.stdout).strip()
            message = f"djoin.exe exited with code {completed.returncode}"
            if detail:
                message = f"{message}: {detail}"
            raise DomainJoinError(message)

        if not _is_nonempty_file(blob_path):
            blob_path.unlink(missing_ok=True)
            raise DomainJoinError(
                "djoin.exe completed without creating a non-empty ODJ blob"
            )

        return blob_path


def get_domain_join_blob(
    settings: Settings,
    computer_name: str,
) -> Path:
    blob_path = domain_join_blob_path(settings, computer_name)
    if not _is_nonempty_file(blob_path):
        raise DomainJoinError("ODJ blob is not ready")
    return blob_path


def delete_domain_join_blob(
    settings: Settings,
    computer_name: str,
) -> bool:
    blob_path = domain_join_blob_path(settings, computer_name)

    with _provision_lock:
        if not blob_path.exists():
            return False
        try:
            blob_path.unlink()
        except OSError as exc:
            raise DomainJoinError(
                f"Failed to delete the acknowledged ODJ blob: {exc}"
            ) from exc
        return True


def _is_nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False
