"""Adapt provider candidates to the existing staged driver archive contract."""
from app.driver_models import DriverInventory, DriverTarget
from app.driver_index.service import get_indexer
from app.driver_providers.local import LocalDriverProvider
from app.driver_providers.wsus import WsusDriverProvider
from app.driver_resolver import DriverResolver


def resolve_manifest(payload, image, deployment_id):
    mode = payload.driver_mode or ('MANUAL_FOLDER' if payload.driver_package else 'NO_DRIVERS')
    if mode not in ('AUTO_LOCAL', 'AUTO_LOCAL_WSUS'):
        return mode, None, None
    indexer = get_indexer()  # Does not initialize, scan, parse, or write the index.
    selected = next((i for i in image['indexes'] if i['index'] == image['defaultIndex']), {})
    inventory = payload.hardware_inventory or DriverInventory(warnings=['Hardware inventory unavailable'])
    target = DriverTarget(architecture=str(selected.get('architecture') or inventory.architecture),
                          version=selected.get('version'), product_type=selected.get('productType'))
    result = DriverResolver(LocalDriverProvider(indexer.db, indexer.root), WsusDriverProvider()).resolve(
        inventory, target, mode, deployment_id)
    if not target.version:
        result.warnings.append('Target WIM OS version unavailable; only known architecture restrictions applied.')
    files = {f['path'].casefold(): f for package in result.candidate_packages for f in package.files}
    if not files:
        return mode, result.model_dump(), None
    package = {'relativePath': f'AUTO\\{deployment_id}', 'sourceFiles': list(files.values()),
               'size': sum(f['size'] for f in files.values()), 'fileCount': len(files),
               'infCount': sum(f['path'].lower().endswith('.inf') for f in files.values())}
    return mode, result.model_dump(), package
