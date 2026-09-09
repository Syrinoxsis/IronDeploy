import json
import logging
import re
import time

from app.driver_models import DriverCandidate, DriverMatch, DriverResolution, normalize_arch
from app.drivers import _read_metadata
from app.driver_archive_selection import selected_snapshot

log = logging.getLogger(__name__)


def compatible(decoration, target):
    if decoration and not re.fullmatch(r'nt(?:amd64|x86|arm64|arm|ia64)?(?:\.\d*){0,5}', decoration):
        return False
    parts = decoration.split('.')
    arch = parts[0].removeprefix('nt')
    if arch and normalize_arch(arch) != normalize_arch(target.architecture):
        return False
    if target.version:
        version = tuple(int(p) for p in re.findall(r'\d+', target.version)[:3])
        minimum = tuple(int(p or '0') for p in parts[1:3])
        if minimum and version[:2] < minimum:
            return False
        if len(parts) > 5 and parts[5].isdigit() and len(version) > 2:
            if version[:2] == minimum and version[2] < int(parts[5]):
                return False
    if len(parts) > 3 and parts[3].isdigit() and target.product_type:
        if int(parts[3]) not in (0, target.product_type):
            return False
    return True


class LocalDriverProvider:
    def __init__(self, db, root):
        self.db, self.root = db, root

    def resolve(self, devices, target):
        started = time.monotonic()
        ids = {v.strip().upper() for d in devices for v in d.hardware_ids + d.compatible_ids}
        rows = self.db.lookup(ids)
        log.info('Driver batch lookup ids=%d rows=%d query=%.3fs', len(ids), len(rows), time.monotonic()-started)
        enabled = {k.casefold(): v.get('enabled', True) for k, v in _read_metadata(self.root)['packages'].items()}
        by_id, candidates = {}, {}
        excluded, warnings = set(), []
        pending_rows = rows
        queried_ids = set(ids)
        dependencies = {}
        # Component devices may not exist in WinPE yet. Resolve their declared
        # SWC IDs in batches across all imports, with cycle/depth protection.
        for depth in range(16):
            self._collect(pending_rows, target, enabled, by_id, candidates, excluded, warnings)
            component_ids = {value for candidate in candidates.values() for metadata in candidate.metadata
                             for value in metadata.get('component_ids', [])}
            missing = component_ids - queried_ids
            if not missing:
                break
            queried_ids.update(missing)
            pending_rows = self.db.lookup(missing)
        else:
            warnings.append('Component dependency depth limit reached')
        for candidate in candidates.values():
            declared = {value for metadata in candidate.metadata for value in metadata.get('component_ids', [])}
            dependencies[candidate.package_id] = {package for value in declared for package in by_id.get(value, [])}
            for value in sorted(declared - by_id.keys()):
                warnings.append(f'Unresolved component {value} required by {candidate.package_id}')
        result = DriverResolution(devices_detected=len(devices), provider_status={'local': 'ready'}, warnings=warnings)
        selected = set()
        for device in devices:
            matches = {}
            for kind, values, offset in [('hardware', device.hardware_ids, 0),
                                         ('compatible', device.compatible_ids, len(device.hardware_ids))]:
                for position, value in enumerate(values):
                    value = value.strip().upper()
                    for package in sorted(by_id.get(value, [])):
                        matches.setdefault(package, DriverMatch(instance_id=device.instance_id, matched_id=value,
                                           specificity=offset+position, id_kind=kind, package_id=package))
            queue = list(matches)
            while queue:
                parent = queue.pop()
                for package in sorted(dependencies.get(parent, [])):
                    if package not in matches:
                        matches[package] = DriverMatch(instance_id=device.instance_id,
                            matched_id=matches[parent].matched_id, specificity=matches[parent].specificity,
                            id_kind='component_dependency', package_id=package)
                        queue.append(package)
            if matches:
                result.matched_devices += 1
                result.matches.extend(sorted(matches.values(), key=lambda m: (m.specificity, m.package_id)))
                selected.update(matches)
            else:
                result.unresolved_devices.append(device)
        result.candidate_packages = [candidates[k] for k in sorted(selected)]
        return result

    def _collect(self, rows, target, enabled, by_id, candidates, excluded, warnings):
        for row in rows:
            if not compatible(row['decoration'], target):
                log.debug('Excluded INF=%s decoration=%s target=%s', row['inf_id'], row['decoration'], target)
                continue
            package_id = row['package_id']
            if package_id in excluded:
                continue
            if package_id not in candidates:
                data = json.loads(row['payload'])
                if not enabled.get(data['import_path'].casefold(), True):
                    excluded.add(package_id)
                    continue
                try:
                    selected_snapshot(self.root, data['files'])
                except (ValueError, OSError) as exc:
                    excluded.add(package_id)
                    warnings.append(f"Excluded changed/unavailable bundle {package_id}: {exc}")
                    continue
                candidates[package_id] = DriverCandidate(**data)
            candidate = candidates[package_id]
            by_id.setdefault(row['hardware_id'], set()).add(candidate.package_id)
