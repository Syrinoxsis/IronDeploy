"""Future WSUS boundary: unresolved PnP devices + target -> shared candidates.

No authentication, network calls, downloads or cache are implemented here.
"""
import logging
from app.driver_models import DriverResolution, DriverDevice, DriverTarget


class WsusDriverProvider:
    def resolve(self, devices: list[DriverDevice], target: DriverTarget) -> DriverResolution:
        logging.getLogger(__name__).info('WSUS stub received unresolved=%d target=%s', len(devices), target)
        return DriverResolution(devices_detected=len(devices), unresolved_devices=devices,
                                provider_status={'wsus': 'not_implemented'},
                                warnings=['WSUS provider is not implemented; continuing with local drivers.'])
