"""Resolver/index/transport tests use disposable repositories, never host DISM."""
import asyncio
import json
import tempfile
import tarfile
import subprocess
import shutil
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from app.driver_models import DriverDevice, DriverInventory, DriverTarget
from app.driver_index.database import DriverIndexDB
from app.driver_index.service import DriverIndexer
from app.driver_index.inf_parser import parse_import
from app.driver_providers.local import LocalDriverProvider
from app.driver_providers.wsus import WsusDriverProvider
from app.driver_resolver import DriverResolver
from app.driver_manifest import resolve_manifest
from app.driver_archive_selection import selected_snapshot, write_selected_tar
from app.deployments import DeploymentManifestRequest
from app.drivers import begin_driver_package_upload, save_uploaded_driver_file, finalize_driver_package_upload, DriverUploadLimits


class DynamicDriverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'Drivers'
        self.root.mkdir()
        self.db = DriverIndexDB(Path(temporary.name) / 'drivers_index.sqlite')
        self.db.initialize()
        self.indexer = DriverIndexer(self.root, self.db)
        self.addCleanup(self.indexer.shutdown)
        self.provider = LocalDriverProvider(self.db, self.root)
        self.target = DriverTarget(architecture='amd64', version='10.0.26100', product_type=1)

    def add(self, package='Dell\\Model', name='base', ids='PCI\\VEN_1234&DEV_5678',
            decoration='NTamd64.10.0', extra='', catalog=None, publish=True):
        path = self.root.joinpath(*package.split('\\'))
        path.mkdir(parents=True, exist_ok=True)
        cat = catalog or name + '.cat'
        (path / cat).write_bytes(b'catalog')
        (path / (name + '.sys')).write_bytes(b'payload')
        (path / (name + '.inf')).write_text(
            '[Version]\nSignature="$Windows NT$"\nProvider=%Maker%\nClass=Net\n'
            f'CatalogFile={cat}\nDriverVer=01/02/2026,1.2.3.4\n'
            f'[Manufacturer]\n%Maker%=Models,{decoration}\n[Models.{decoration}]\n'
            f'%Device%=Install,{ids}\n[SourceDisksFiles]\n{name}.sys=1\n'
            '[Strings]\nMaker="Vendor"\nDevice="Device"\n' + extra, encoding='utf-16')
        if publish:
            self.indexer.submit(package).result(10)
        return path

    def resolve(self, ids=None, compatible=None, devices=None):
        return self.provider.resolve(devices or [DriverDevice(instance_id='dev1',
            hardware_ids=ids or ['PCI\\VEN_1234&DEV_5678'], compatible_ids=compatible or [])], self.target)

    def test_exact_hardware_id(self):
        self.add()
        result = self.resolve()
        self.assertEqual(result.matched_devices, 1)
        self.assertEqual(result.matches[0].specificity, 0)

    def test_multiple_ordered_hardware_ids(self):
        self.add()
        result = self.resolve(['PCI\\UNKNOWN', 'PCI\\VEN_1234&DEV_5678'])
        self.assertEqual(result.matches[0].specificity, 1)

    def test_subsys_precedes_generic_candidates(self):
        self.add(name='generic')
        self.add(name='exact', ids='PCI\\VEN_1234&DEV_5678&SUBSYS_0001')
        result = self.resolve(['PCI\\VEN_1234&DEV_5678&SUBSYS_0001', 'PCI\\VEN_1234&DEV_5678'])
        self.assertEqual([m.specificity for m in result.matches], [0, 1])
        self.assertEqual(len(result.candidate_packages), 2)

    def test_compatible_id_fallback(self):
        self.add(ids='PCI\\OTHER,PCI\\GENERIC')
        result = self.resolve(['PCI\\UNKNOWN'], ['PCI\\GENERIC'])
        self.assertEqual(result.matches[0].id_kind, 'compatible')

    def test_searches_across_vendor_folders(self):
        self.add()
        self.add(package='HP\\Other')
        self.assertEqual(len(self.resolve().candidate_packages), 2)

    def test_wrong_architecture(self):
        self.add(decoration='NTarm64.10.0')
        self.assertEqual(self.resolve().matched_devices, 0)

    def test_wrong_os_version(self):
        self.add(decoration='NTamd64.11.0')
        self.assertEqual(self.resolve().matched_devices, 0)

    def test_wrong_os_build(self):
        self.add(decoration='NTamd64.10.0...28000')
        self.assertEqual(self.resolve().matched_devices, 0)

    def test_wrong_product_type(self):
        self.add(decoration='NTamd64.10.0.3')
        self.assertEqual(self.resolve().matched_devices, 0)

    def test_unresolved_device(self):
        self.assertEqual(self.resolve().unresolved_devices[0].instance_id, 'dev1')

    def test_multiple_devices_deduplicate_bundle(self):
        self.add()
        result = self.resolve(devices=[DriverDevice(instance_id=str(i), hardware_ids=['PCI\\VEN_1234&DEV_5678']) for i in range(3)])
        self.assertEqual(result.matched_devices, 3)
        self.assertEqual(len(result.candidate_packages), 1)

    def test_shared_catalog_groups_multiple_infs(self):
        self.add(name='base', catalog='shared.cat')
        self.add(name='extension', catalog='shared.cat', ids='PCI\\OTHER')
        self.assertEqual(len(self.resolve().candidate_packages[0].inf_paths), 2)

    def test_component_dependency_is_kept(self):
        self.add(extra='[ComponentInstall]\nComponentIDs=VID_TEST\n')
        self.add(name='component', ids='SWC\\VID_TEST')
        self.assertEqual(len(self.resolve().candidate_packages[0].inf_paths), 2)

    def test_component_dependency_from_another_import(self):
        self.add(extra='[ComponentInstall]\nComponentIDs=VID_TEST\n')
        self.add(package='HP\\Components', name='component', ids='SWC\\VID_TEST')
        result = self.resolve()
        self.assertEqual(len(result.candidate_packages), 2)
        self.assertIn('component_dependency', [m.id_kind for m in result.matches])

    def test_component_dependency_cycle_terminates(self):
        self.add(extra='[ComponentInstall]\nComponentIDs=VID_TEST\n', ids='PCI\\VEN_1234&DEV_5678,SWC\\BACK')
        self.add(package='HP\\Components', name='component', ids='SWC\\VID_TEST',
                 extra='[ComponentInstall]\nComponentIDs=BACK\n')
        self.assertEqual(len(self.resolve().candidate_packages), 2)

    def test_unrelated_inf_in_same_directory_is_not_copied(self):
        self.add()
        self.add(name='unrelated', ids='PCI\\UNRELATED')
        result = self.resolve()
        self.assertEqual(result.candidate_packages[0].inf_paths, ['base.inf'])
        self.assertFalse(any('unrelated' in f['path'] for f in result.candidate_packages[0].files))

    def test_parallel_read_requests(self):
        self.add()
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: self.resolve().matched_devices, range(60)))
        self.assertEqual(results, [1]*60)

    def test_new_import_not_visible_until_commit(self):
        path = self.add(publish=False)
        bundles, errors = parse_import(path, 'Dell\\Model')
        self.db.start('Dell\\Model')
        self.assertEqual(self.resolve().matched_devices, 0)
        self.db.publish('Dell\\Model', bundles, errors)
        self.assertEqual(self.resolve().matched_devices, 1)

    def test_existing_generation_visible_during_reindex(self):
        self.add()
        self.db.start('Dell\\Model')
        self.assertEqual(self.resolve().matched_devices, 1)

    def test_failed_new_index_not_ready(self):
        path = self.root / 'Bad' / 'Model'
        path.mkdir(parents=True)
        (path / 'bad.inf').write_text('malformed')
        self.indexer.submit('Bad\\Model').result(10)
        self.assertEqual(self.db.imports()[0]['status'], 'error')
        self.assertFalse(self.resolve().candidate_packages)

    def test_failed_rebuild_preserves_last_generation(self):
        path = self.add()
        (path / 'base.inf').write_text('malformed')
        self.indexer.submit('Dell\\Model').result(10)
        self.assertEqual(self.db.imports()[0]['status'], 'error')
        self.assertTrue(self.db.lookup(['PCI\\VEN_1234&DEV_5678']))
        self.assertEqual(self.resolve().matched_devices, 0)
        with self.assertRaises(ValueError):
            selected_snapshot(self.root, json.loads(self.db.lookup(['PCI\\VEN_1234&DEV_5678'])[0]['payload'])['files'])

    def test_one_malformed_inf_does_not_hide_other_valid_packages(self):
        path = self.add()
        (path / 'broken.inf').write_text('bad')
        self.indexer.submit('Dell\\Model').result(10)
        self.assertEqual(self.resolve().matched_devices, 1)
        self.assertIn('broken.inf', self.db.imports()[0]['error'])

    def test_23_index_jobs_do_not_break_readers(self):
        self.add()
        for i in range(23):
            self.add(package=f'HP\\Model{i}', publish=False)
        with ThreadPoolExecutor(max_workers=10) as readers:
            futures = [readers.submit(lambda: [self.resolve().matched_devices for _ in range(10)]) for _ in range(10)]
            jobs = [self.indexer.submit(f'HP\\Model{i}') for i in range(23)]
            for job in jobs:
                job.result(20)
            self.assertTrue(all(n == 1 for f in futures for n in f.result()))
        self.assertEqual(len(self.resolve().candidate_packages), 24)

    def test_initial_scan_and_explicit_rebuild(self):
        self.add(publish=False)
        for job in self.indexer.scan():
            job.result(10)
        self.assertEqual(self.resolve().matched_devices, 1)
        self.assertEqual(self.indexer.scan(), [])
        for job in self.indexer.scan(rebuild=True):
            job.result(10)
        self.assertEqual(self.resolve().matched_devices, 1)

    def test_deleted_index_rebuilt_from_source(self):
        path = self.add()
        self.db.path.unlink()
        for job in self.indexer.scan():
            job.result(10)
        self.assertTrue((path / 'base.inf').is_file())
        self.assertEqual(self.resolve().matched_devices, 1)

    def test_auto_local_does_not_call_wsus(self):
        self.add()
        external = Mock()
        result = DriverResolver(self.provider, external).resolve(DriverInventory(devices=[DriverDevice(
            instance_id='d', hardware_ids=['PCI\\VEN_1234&DEV_5678'])]), self.target, 'AUTO_LOCAL')
        external.resolve.assert_not_called()
        self.assertEqual(result.matched_devices, 1)

    def test_auto_wsus_receives_only_unresolved(self):
        self.add()
        external = Mock(wraps=WsusDriverProvider())
        result = DriverResolver(self.provider, external).resolve(DriverInventory(devices=[
            DriverDevice(instance_id='known', hardware_ids=['PCI\\VEN_1234&DEV_5678']),
            DriverDevice(instance_id='unknown', hardware_ids=['PCI\\UNKNOWN'])]), self.target, 'AUTO_LOCAL_WSUS')
        self.assertEqual([d.instance_id for d in external.resolve.call_args.args[0]], ['unknown'])
        self.assertEqual(result.provider_status['wsus'], 'not_implemented')
        self.assertEqual(len(result.candidate_packages), 1)

    def test_provider_error_is_nonfatal(self):
        provider = Mock()
        provider.resolve.side_effect = OSError('busy')
        result = DriverResolver(provider, WsusDriverProvider()).resolve(
            DriverInventory(devices=[DriverDevice(instance_id='d')]), self.target, 'AUTO_LOCAL_WSUS')
        self.assertEqual(result.provider_status['local'], 'unavailable')
        self.assertEqual(len(result.unresolved_devices), 1)

    def test_manual_and_none_bypass_resolver(self):
        with patch('app.driver_manifest.get_indexer') as factory:
            for mode, package, expected in [(None, None, 'NO_DRIVERS'), (None, 'Dell\\Model', 'MANUAL_FOLDER'),
                    ('MANUAL_FOLDER', 'Dell\\Model', 'MANUAL_FOLDER'), ('NO_DRIVERS', None, 'NO_DRIVERS')]:
                payload = DeploymentManifestRequest(image_name='os.wim', driver_mode=mode, driver_package=package)
                self.assertEqual(resolve_manifest(payload, {}, 1), (expected, None, None))
            factory.assert_not_called()

    def test_auto_manifest_does_not_scan_or_parse(self):
        self.add()
        with patch('app.driver_manifest.get_indexer', return_value=self.indexer), patch.object(self.indexer, 'scan') as scan:
            result = resolve_manifest(DeploymentManifestRequest(image_name='os.wim', driver_mode='AUTO_LOCAL',
                hardware_inventory=DriverInventory(devices=[DriverDevice(instance_id='d', hardware_ids=['PCI\\VEN_1234&DEV_5678'])])),
                {'defaultIndex': 1, 'indexes': [{'index': 1, 'architecture': 'amd64', 'version': '10.0.26100'}]}, 1)
            self.assertEqual(result[2]['infCount'], 1)
            scan.assert_not_called()

    def test_disabled_import_excluded(self):
        self.add()
        (self.root / '.irondeploy-drivers.json').write_text(json.dumps({'packages': {'Dell\\Model': {'enabled': False}}}))
        self.assertEqual(self.resolve().matched_devices, 0)

    def test_selected_tar_contains_only_manifest_files(self):
        self.add()
        self.add(name='unrelated', ids='PCI\\OTHER')
        files = self.resolve().candidate_packages[0].files
        output = self.root.parent / 'test.tar'
        write_selected_tar(self.root, files, output, threading.Event(), 10)
        with tarfile.open(output) as archive:
            self.assertEqual(set(archive.getnames()), {f['path'] for f in files})
            self.assertEqual(archive.extractfile('Dell/Model/base.sys').read(), b'payload')

    def test_tar_is_readable_by_bundled_winpe_7zip(self):
        from app.driver_archives import SEVEN_ZIP_EXE
        if not SEVEN_ZIP_EXE.is_file():
            self.skipTest('Bundled Windows 7-Zip unavailable')
        self.add(package='HP\\Модель')
        files = self.resolve().candidate_packages[0].files
        output = self.root.parent / 'drivers.tar'
        extracted = self.root.parent / 'extracted'
        write_selected_tar(self.root, files, output, threading.Event(), 10)
        process = subprocess.run([str(SEVEN_ZIP_EXE), 'x', str(output), '-o' + str(extracted), '-y'],
                                 capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual({p.relative_to(extracted).as_posix() for p in extracted.rglob('*') if p.is_file()},
                         {f['path'] for f in files})

    def test_inventory_helper_preserves_arrays_with_mocked_cim(self):
        if not shutil.which('powershell.exe'):
            self.skipTest('Windows PowerShell unavailable')
        helper = Path(__file__).resolve().parents[2] / 'WinPE/Runtime/IronDeploy.DriverInventory.ps1'
        script = r'''
function Get-CimInstance {
    param($ClassName, $ErrorAction)
    if ($ClassName -eq 'Win32_ComputerSystem') {
        return [pscustomobject]@{ Manufacturer='OEM'; Model='Model'; SystemFamily='Family'; SystemSKUNumber='SKU' }
    }
    return [pscustomobject]@{ PNPDeviceID='dev'; HardwareID=@('PCI\EXACT','PCI\GENERIC');
        CompatibleID=@('PCI\COMPAT'); PNPClass='Net'; Status='OK'; ConfigManagerErrorCode=0 }
}
. 'HELPER'
Get-IronDriverInventory | ConvertTo-Json -Depth 12 -Compress
'''.replace('HELPER', str(helper).replace("'", "''"))
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        inventory = json.loads(result.stdout)
        self.assertEqual(inventory['devices'][0]['hardware_ids'], ['PCI\\EXACT', 'PCI\\GENERIC'])
        self.assertEqual(inventory['devices'][0]['compatible_ids'], ['PCI\\COMPAT'])
        self.assertEqual(inventory['sku'], 'SKU')

    def test_source_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            selected_snapshot(self.root, [{'path': '../secret', 'size': 0, 'mtime_ns': 0}])

    def test_upload_finalization_schedules_indexing(self):
        vendor = self.root / 'Vendor'
        vendor.mkdir()
        limits = DriverUploadLimits(min_free_space_gib=1)
        upload = begin_driver_package_upload('Vendor', 'Model', self.root, limits)

        async def chunks():
            yield b'[Version]\nSignature="$Windows NT$"\n'

        asyncio.run(save_uploaded_driver_file(upload['uploadId'], 'driver.inf', chunks(), self.root, limits))
        with patch('app.drivers._notify_driver_index') as notify:
            finalize_driver_package_upload(upload['uploadId'], self.root)
            notify.assert_called_once_with(self.root, 'Vendor\\Model')

    def test_archive_worker_status_and_cleanup_for_auto(self):
        from app.driver_archives import prepare_driver_archive, get_driver_archive_status, cleanup_driver_archive
        from types import SimpleNamespace
        self.add()
        files = self.resolve().candidate_packages[0].files
        package = {'relativePath': 'AUTO\\17', 'sourceFiles': files, 'size': sum(f['size'] for f in files),
                   'fileCount': len(files), 'infCount': 1}
        with patch('app.driver_archives.DRIVERS_DIR', self.root), patch('app.driver_archives.ARCHIVE_ROOT', self.root / '.irondeploy-archives'):
            try:
                prepare_driver_archive(17, package, SimpleNamespace(driver_archive_max_gib=25, deployment_timeout_minutes=90))
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    status = get_driver_archive_status(17)
                    if status['status'] != 'preparing':
                        break
                    time.sleep(.02)
                self.assertEqual(status['status'], 'ready', status)
                self.assertEqual(status['sourceFileCount'], len(files))
                with tarfile.open(self.root / '.irondeploy-archives/17/drivers.tar') as archive:
                    self.assertEqual(set(archive.getnames()), {f['path'] for f in files})
            finally:
                cleanup_driver_archive(17)
            self.assertFalse((self.root / '.irondeploy-archives/17').exists())

    def test_comma_in_disk_label_and_decorated_source_directory(self):
        path = self.add(publish=False)
        (path / 'payload').mkdir()
        (path / 'base.sys').rename(path / 'payload/base.sys')
        inf = path / 'base.inf'
        text = inf.read_text(encoding='utf-16')
        text += '\n[SourceDisksNames.amd64]\n1=%Disk%,,,.\\payload\n[Strings]\nDisk="Vendor, Inc. disk"\n'
        inf.write_text(text, encoding='utf-16')
        self.indexer.submit('Dell\\Model').result(10)
        self.assertEqual(self.resolve().matched_devices, 1)
        self.assertTrue(any(f['path'].endswith('payload/base.sys') for f in self.resolve().candidate_packages[0].files))

    def test_damaged_bundle_does_not_remove_other_candidates(self):
        damaged = self.add()
        self.add(package='HP\\Healthy')
        (damaged / 'base.sys').unlink()
        result = self.resolve()
        self.assertEqual(len(result.candidate_packages), 1)
        self.assertEqual(result.candidate_packages[0].import_path, 'HP\\Healthy')
        self.assertTrue(result.warnings)


if __name__ == '__main__':
    unittest.main()
