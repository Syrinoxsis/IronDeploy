"""Shared inventory and provider contracts; no storage or Windows dependencies."""
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field

DriverMode = Literal["AUTO_LOCAL", "AUTO_LOCAL_WSUS", "MANUAL_FOLDER", "NO_DRIVERS"]
HardwareID = Annotated[str, Field(min_length=1, max_length=1024)]


class DriverDevice(BaseModel):
    instance_id: str = Field(min_length=1, max_length=1024)
    hardware_ids: list[HardwareID] = Field(default_factory=list, max_length=256)
    compatible_ids: list[HardwareID] = Field(default_factory=list, max_length=256)
    device_class: str | None = None
    status: str | None = None
    problem_code: int | None = None


class DriverInventory(BaseModel):
    manufacturer: str | None = None
    model: str | None = None
    family: str | None = None
    sku: str | None = None
    architecture: str = "amd64"
    devices: list[DriverDevice] = Field(default_factory=list, max_length=4096)
    warnings: list[str] = Field(default_factory=list)


class DriverTarget(BaseModel):
    architecture: str
    version: str | None = None
    product_type: int | None = None


class DriverCandidate(BaseModel):
    package_id: str
    source: str = "local"
    import_path: str
    inf_paths: list[str]
    files: list[dict]
    metadata: list[dict] = Field(default_factory=list)


class DriverMatch(BaseModel):
    instance_id: str
    matched_id: str
    specificity: int
    id_kind: str
    package_id: str
    source: str = "local"


class DriverResolution(BaseModel):
    devices_detected: int = 0
    matched_devices: int = 0
    unresolved_devices: list[DriverDevice] = Field(default_factory=list)
    candidate_packages: list[DriverCandidate] = Field(default_factory=list)
    matches: list[DriverMatch] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    provider_status: dict[str, str] = Field(default_factory=dict)


class DriverProvider(Protocol):
    def resolve(self, devices: list[DriverDevice], target: DriverTarget) -> DriverResolution: ...


def normalize_arch(value: str) -> str:
    return {"9": "amd64", "x64": "amd64", "x86_64": "amd64", "0": "x86",
            "12": "arm64"}.get(value.lower(), value.lower())
