"""Installed-Windows local driver reconciliation contracts and helpers."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.driver_index.service import get_indexer
from app.driver_models import DriverInventory, DriverTarget
from app.driver_providers.local import LocalDriverProvider


class DriverReconciliationRequest(BaseModel):
    pass_number: int = Field(ge=1, le=3)
    inventory: DriverInventory
    os_version: str = Field(min_length=1, max_length=128)
    product_type: int = Field(default=1, ge=1, le=3)


class DriverReconciliationFinalRequest(BaseModel):
    inventory: DriverInventory
    os_version: str = Field(min_length=1, max_length=128)
    product_type: int = Field(default=1, ge=1, le=3)
    reboot_required: bool = False
    warnings: list[str] = Field(default_factory=list, max_length=100)


class DriverArchiveCompletionRequest(BaseModel):
    pass_number: int = Field(ge=1, le=3)
    installed: bool = False


def resolve_installed_inventory(payload: DriverReconciliationRequest | DriverReconciliationFinalRequest):
    indexer = get_indexer()
    target = DriverTarget(
        architecture=payload.inventory.architecture,
        version=payload.os_version,
        product_type=payload.product_type,
    )
    return LocalDriverProvider(indexer.db, indexer.root).resolve(
        payload.inventory.devices,
        target,
    )


def delivered_package_ids(driver_resolution: dict[str, Any] | None, before_pass: int) -> set[str]:
    resolution = driver_resolution or {}
    delivered = set(resolution.get("appliedPackageIds", []))
    reconciliation = resolution.get("reconciliation") or {}
    for item in reconciliation.get("passes", []):
        if int(item.get("passNumber", 0)) < before_pass and item.get("installed") is True:
            delivered.update(str(value) for value in item.get("newPackageIds", []))
    return delivered


def archive_package(candidates) -> dict[str, Any] | None:
    files = {
        item["path"].casefold(): item
        for candidate in candidates
        for item in candidate.files
    }
    if not files:
        return None
    selected = list(files.values())
    return {
        "relativePath": "AUTO_POST",
        "sourceFiles": selected,
        "size": sum(int(item["size"]) for item in selected),
        "fileCount": len(selected),
        "infCount": sum(item["path"].lower().endswith(".inf") for item in selected),
    }


def candidate_summary(candidate) -> dict[str, Any]:
    return {
        "package_id": candidate.package_id,
        "source": candidate.source,
        "import_path": candidate.import_path,
        "inf_paths": candidate.inf_paths,
        "metadata": candidate.metadata,
    }


def pass_summary(payload: DriverReconciliationRequest, result, candidates) -> dict[str, Any]:
    return {
        "passNumber": payload.pass_number,
        "devicesDetected": result.devices_detected,
        "matchedDevices": result.matched_devices,
        "unresolvedDevices": [item.model_dump() for item in result.unresolved_devices],
        "matches": [item.model_dump() for item in result.matches],
        "candidatePackages": [candidate_summary(item) for item in result.candidate_packages],
        "newPackageIds": [item.package_id for item in candidates],
        "warnings": result.warnings,
    }


def build_device_report(inventory: DriverInventory, result) -> list[dict[str, Any]]:
    packages = {item.package_id: item for item in result.candidate_packages}
    matches: dict[str, list[Any]] = {}
    for match in result.matches:
        matches.setdefault(match.instance_id, []).append(match)
    report = []
    for device in inventory.devices:
        device_matches = matches.get(device.instance_id, [])
        candidates = [packages[item.package_id] for item in device_matches if item.package_id in packages]
        infs = sorted({path for item in candidates for path in item.inf_paths}, key=str.casefold)
        report.append({
            "instanceId": device.instance_id,
            "deviceName": device.device_name,
            "deviceClass": device.device_class,
            "problemCode": device.problem_code,
            # Windows normally publishes third-party INFs as oemNN.inf, so
            # InfName alone cannot prove which source supplied the installed
            # package. Report the honest local-index match separately.
            "status": "found_local" if device_matches else "not_found",
            "matchedIds": sorted({item.matched_id for item in device_matches}),
            "packageIds": sorted({item.package_id for item in device_matches}),
            "infPaths": infs,
            "installedInf": device.driver_inf_name,
            "installedProvider": device.driver_provider,
            "installedVersion": device.driver_version,
        })
    return report


def save_reconciliation(resolution: dict[str, Any] | None, reconciliation: dict[str, Any]) -> dict[str, Any]:
    updated = dict(resolution or {})
    updated["reconciliation"] = reconciliation
    return updated
