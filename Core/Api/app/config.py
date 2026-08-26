from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
from os import getenv
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field


API_ROOT = Path(__file__).resolve().parents[1]
IRONDEPLOY_ROOT = API_ROOT.parent
load_dotenv(API_ROOT / ".env")


def _expand_irondeploy_root(value: str) -> str:
    return value.replace(
        "{IRONDEPLOY_ROOT}",
        IRONDEPLOY_ROOT.as_posix(),
    )


def _get_required_env(name: str, allow_empty: bool = False) -> str:
    value = getenv(name)
    if value is None:
        raise RuntimeError(f"{name} is required in Api\\.env.")
    value = value.strip()
    if not allow_empty and value == "":
        raise RuntimeError(f"{name} cannot be empty in Api\\.env.")
    return value


def _get_optional_env(name: str) -> str | None:
    value = _get_required_env(name, allow_empty=True)
    return value or None


def _get_bool(name: str) -> bool:
    return _get_required_env(name).lower() in {"1", "true", "yes", "y", "on"}


def _get_int(name: str) -> int:
    value = _get_required_env(name)
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer in Api\\.env.") from exc


def _get_int_or_default(name: str, default: int) -> int:
    value = getenv(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer in Api\\.env.") from exc


def _get_allowed_client_networks() -> tuple[IPv4Network | IPv6Network, ...]:
    value = _get_required_env(
        "IRONAPI_ALLOWED_CLIENT_NETWORKS",
        allow_empty=True,
    )
    return tuple(
        ip_network(item.strip(), strict=False)
        for item in value.split(",")
        if item.strip()
    )


class Settings(BaseModel):
    database_url: str = Field(repr=False)
    smb_share_path: str = Field(default="", repr=False)
    smb_user: str = Field(default="", repr=False)
    smb_password: str = Field(default="", repr=False)

    allowed_client_networks: tuple[IPv4Network | IPv6Network, ...]
    deployment_authorization_timeout_minutes: int = Field(default=10, ge=5, le=30)
    deployment_timeout_minutes: int = Field(default=90, ge=30, le=240)
    driver_max_files: int = Field(default=25000, ge=1, le=1_000_000)
    driver_max_depth: int = Field(default=16, ge=1, le=100)
    driver_max_full_path: int = Field(default=240, ge=64, le=32767)
    driver_upload_ttl_hours: int = Field(default=24, ge=1, le=8760)
    driver_max_active_uploads: int = Field(default=3, ge=1, le=100)
    driver_min_free_space_gib: int = Field(default=25, ge=1, le=10240)

    ldap_server: str | None
    ldap_base_dn: str | None
    ldap_use_ssl: bool
    ldap_connect_timeout: int = Field(ge=1, le=60)

    odj_domain: str | None
    odj_machine_ou: str | None
    odj_blob_dir: Path
    odj_djoin_path: Path
    odj_provision_timeout: int = Field(ge=1, le=300)
    odj_blob_max_age_minutes: int = Field(default=5, ge=5, le=1440)

    @property
    def ldap_enabled(self) -> bool:
        return bool(self.ldap_server and self.ldap_base_dn)

    @property
    def odj_enabled(self) -> bool:
        """Offline Domain Join needs a target domain and OU to provision into.

        Deployments without Active Directory leave both empty, which keeps the
        service identity free of any domain requirement.
        """
        return bool(self.odj_domain and self.odj_machine_ou)


@lru_cache
def get_settings() -> Settings:
    settings = Settings(
        database_url=_expand_irondeploy_root(
            _get_required_env("IRONAPI_DATABASE_URL")
        ),
        smb_share_path=getenv("IRONAPI_SMB_SHARE_PATH", "").strip(),
        smb_user=getenv("IRONAPI_SMB_USER", "").strip(),
        smb_password=getenv("IRONAPI_SMB_PASSWORD", "").strip(),
        allowed_client_networks=_get_allowed_client_networks(),
        deployment_authorization_timeout_minutes=_get_int_or_default(
            "IRONAPI_DEPLOYMENT_AUTHORIZATION_TIMEOUT_MINUTES", 10
        ),
        deployment_timeout_minutes=_get_int_or_default(
            "IRONAPI_DEPLOYMENT_TIMEOUT_MINUTES", 90
        ),
        driver_max_files=_get_int_or_default("IRONAPI_DRIVER_MAX_FILES", 25000),
        driver_max_depth=_get_int_or_default("IRONAPI_DRIVER_MAX_DEPTH", 16),
        driver_max_full_path=_get_int_or_default(
            "IRONAPI_DRIVER_MAX_FULL_PATH", 240
        ),
        driver_upload_ttl_hours=_get_int_or_default(
            "IRONAPI_DRIVER_UPLOAD_TTL_HOURS", 24
        ),
        driver_max_active_uploads=_get_int_or_default(
            "IRONAPI_DRIVER_MAX_ACTIVE_UPLOADS", 3
        ),
        driver_min_free_space_gib=_get_int_or_default(
            "IRONAPI_DRIVER_MIN_FREE_SPACE_GIB", 25
        ),
        ldap_server=_get_optional_env("IRONAPI_LDAP_SERVER"),
        ldap_base_dn=_get_optional_env("IRONAPI_LDAP_BASE_DN"),
        ldap_use_ssl=_get_bool("IRONAPI_LDAP_USE_SSL"),
        ldap_connect_timeout=_get_int("IRONAPI_LDAP_CONNECT_TIMEOUT"),
        odj_domain=_get_optional_env("IRONAPI_ODJ_DOMAIN"),
        odj_machine_ou=_get_optional_env("IRONAPI_ODJ_MACHINE_OU"),
        odj_blob_dir=Path(
            _expand_irondeploy_root(_get_required_env("IRONAPI_ODJ_BLOB_DIR"))
        ),
        odj_djoin_path=Path(_get_required_env("IRONAPI_ODJ_DJOIN_PATH")),
        odj_provision_timeout=_get_int("IRONAPI_ODJ_PROVISION_TIMEOUT"),
        odj_blob_max_age_minutes=_get_int_or_default(
            "IRONAPI_ODJ_BLOB_MAX_AGE_MINUTES", 5
        ),
    )
    return settings
