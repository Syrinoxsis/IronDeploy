import locale
import logging
import subprocess
import time
from pathlib import Path
from threading import Lock

from app.config import Settings


class DomainJoinError(RuntimeError):
    pass


logger = logging.getLogger("uvicorn.error")

_provision_lock = Lock()

# The purge scans a directory, so throttle it: it is driven by ordinary request
# traffic rather than by a scheduler.
_PURGE_INTERVAL_SECONDS = 300.0
_last_purge_at: float | None = None


def domain_join_blob_path(settings: Settings, computer_name: str) -> Path:
    return settings.odj_blob_dir / f"{computer_name}.txt"


def provision_domain_join_blob(
    settings: Settings,
    computer_name: str,
    reuse_existing_account: bool = False,
) -> Path:
    blob_path = domain_join_blob_path(settings, computer_name)
    max_age_seconds = settings.odj_blob_max_age_minutes * 60

    with _provision_lock:
        # Reuse the blob of an interrupted attempt so a retry does not churn the
        # machine account, but only while it is fresh: djoin resets the account
        # password on every provision, so an old blob may no longer be valid.
        if _is_nonempty_file(blob_path) and _age_seconds(blob_path) <= max_age_seconds:
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


def purge_stale_domain_join_blobs(
    settings: Settings,
    throttle: bool = False,
) -> int:
    """Delete ODJ blobs left behind by deployments that never acknowledged.

    A blob is only needed between provisioning and the WinPE download that
    follows it within seconds, so anything older than the configured maximum age
    is an orphan holding a computer-account secret. Deleting one that belongs to
    a still-running deployment is harmless: acknowledge answers already_deleted.
    """
    global _last_purge_at

    max_age_seconds = settings.odj_blob_max_age_minutes * 60
    deleted = 0

    with _provision_lock:
        now = time.monotonic()
        if (
            throttle
            and _last_purge_at is not None
            and now - _last_purge_at < _PURGE_INTERVAL_SECONDS
        ):
            return 0
        _last_purge_at = now

        try:
            candidates = list(settings.odj_blob_dir.glob("*.txt"))
        except OSError as exc:
            logger.warning("ODJ purge could not read the blob directory: %s", exc)
            return 0

        for blob_path in candidates:
            try:
                if not blob_path.is_file():
                    continue
                if _age_seconds(blob_path) <= max_age_seconds:
                    continue
                blob_path.unlink()
            except OSError as exc:
                logger.warning(
                    "ODJ purge could not delete %s: %s",
                    blob_path.name,
                    exc,
                )
                continue
            deleted += 1
            logger.warning(
                "Purged an orphaned ODJ blob older than %d minutes: %s",
                settings.odj_blob_max_age_minutes,
                blob_path.name,
            )

    return deleted


def _is_nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _age_seconds(path: Path) -> float:
    # An unreadable timestamp must never look fresh: treat it as expired.
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return float("inf")
