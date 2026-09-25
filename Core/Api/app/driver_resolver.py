"""Provider orchestration independent of import, WinPE and archive transport."""
import logging
import time
from app.driver_models import DriverResolution, DriverProvider, DriverInventory, DriverTarget

log = logging.getLogger(__name__)


class DriverResolver:
    def __init__(self, local: DriverProvider, external: DriverProvider):
        self.local, self.external = local, external

    def resolve(self, inventory: DriverInventory, target: DriverTarget, mode: str,
                deployment_id: int | None = None) -> DriverResolution:
        started = time.monotonic()
        try:
            result = self.local.resolve(inventory.devices, target)
        except Exception as exc:
            log.exception('Driver local resolution unavailable deployment=%s', deployment_id)
            result = DriverResolution(devices_detected=len(inventory.devices), unresolved_devices=inventory.devices,
                                      warnings=[f'Local driver index unavailable: {exc}'], provider_status={'local': 'unavailable'})
        if mode == 'AUTO_LOCAL_WSUS':
            try:
                extra = self.external.resolve(result.unresolved_devices, target)
                packages = {p.package_id: p for p in result.candidate_packages + extra.candidate_packages}
                result.candidate_packages = list(packages.values())
                result.matches.extend(extra.matches)
                result.unresolved_devices = extra.unresolved_devices
                result.matched_devices = len(inventory.devices) - len(result.unresolved_devices)
                result.warnings.extend(extra.warnings)
                result.provider_status.update(extra.provider_status)
            except Exception as exc:
                result.warnings.append(f'External provider unavailable: {exc}')
                result.provider_status['wsus'] = 'unavailable'
        result.warnings.extend(inventory.warnings)
        log.info('Driver resolution deployment=%s devices=%d hwids=%d matched=%d unresolved=%s packages=%d elapsed=%.3fs',
                 deployment_id, len(inventory.devices), sum(len(d.hardware_ids) for d in inventory.devices),
                 result.matched_devices, [d.instance_id for d in result.unresolved_devices],
                 len(result.candidate_packages), time.monotonic()-started)
        for match in result.matches:
            log.info('Driver match deployment=%s device=%s hwid=%s package=%s specificity=%s source=%s',
                     deployment_id, match.instance_id, match.matched_id, match.package_id, match.specificity, match.source)
        return result
