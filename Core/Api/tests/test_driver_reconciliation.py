import unittest
from pathlib import Path

from app.driver_models import (
    DriverCandidate,
    DriverDevice,
    DriverInventory,
    DriverMatch,
    DriverResolution,
)
from app.driver_reconciliation import (
    archive_package,
    build_device_report,
    delivered_package_ids,
)


class DriverReconciliationTests(unittest.TestCase):
    def candidate(self, package_id="package-1"):
        return DriverCandidate(
            package_id=package_id,
            import_path="Hp\\Model",
            inf_paths=["net.inf"],
            files=[
                {"path": "Hp/Model/net.inf", "size": 10, "mtime_ns": 1},
                {"path": "Hp/Model/net.sys", "size": 20, "mtime_ns": 1},
            ],
            metadata=[{"provider": "Vendor", "version": "1.2.3"}],
        )

    def test_archive_package_deduplicates_shared_files(self):
        first = self.candidate("one")
        second = self.candidate("two")
        package = archive_package([first, second])
        self.assertEqual(package["fileCount"], 2)
        self.assertEqual(package["infCount"], 1)
        self.assertEqual(package["size"], 30)

    def test_delivered_packages_include_initial_and_earlier_passes(self):
        resolution = {
            "candidate_packages": [{"package_id": "initial"}],
            "reconciliation": {"passes": [
                {"passNumber": 1, "newPackageIds": ["pass-one"]},
                {"passNumber": 2, "newPackageIds": ["current"]},
            ]},
        }
        self.assertEqual(
            delivered_package_ids(resolution, 2),
            {"initial", "pass-one"},
        )

    def test_device_report_keeps_local_match_separate_from_installed_inf(self):
        device = DriverDevice(
            instance_id="PCI\\DEVICE",
            hardware_ids=["PCI\\VEN_1234"],
            device_name="Network adapter",
            device_class="Net",
            driver_inf_name="oem42.inf",
            driver_provider="Vendor",
            driver_version="1.2.3",
        )
        candidate = self.candidate()
        resolution = DriverResolution(
            devices_detected=1,
            matched_devices=1,
            candidate_packages=[candidate],
            matches=[DriverMatch(
                instance_id=device.instance_id,
                matched_id="PCI\\VEN_1234",
                specificity=0,
                id_kind="hardware",
                package_id=candidate.package_id,
            )],
        )
        report = build_device_report(
            DriverInventory(devices=[device]),
            resolution,
        )
        self.assertEqual(report[0]["status"], "found_local")
        self.assertEqual(report[0]["installedInf"], "oem42.inf")
        self.assertEqual(report[0]["infPaths"], ["net.inf"])

    def test_postinstall_runs_reconciliation_before_software_and_reboots_last(self):
        root = Path(__file__).resolve().parents[2]
        postinstall = (root / "ServerTemplates/PostInstall/postinstall.ps1").read_text(
            encoding="utf-8"
        )
        setup_complete = (root / "ServerTemplates/PostInstall/SetupComplete.cmd").read_text(
            encoding="utf-8"
        )
        reconcile = postinstall.index("$DriverReconciliation = Invoke-IronDriverReconciliation")
        software = postinstall.index("$ProgramResults = @(Install-IronDeployPrograms)")
        completed = postinstall.index("$Completed = Complete-Deployment")
        self.assertLess(reconcile, software)
        self.assertLess(software, completed)
        self.assertIn("for ($Pass = 1; $Pass -le 3; $Pass++)", postinstall)
        self.assertIn("pnputil.exe /add-driver", postinstall)
        self.assertIn(
            '$DriversRoot = Join-Path "$($DriveName):\\" "Drivers"',
            postinstall,
        )
        self.assertIn("-Force -ErrorAction Stop", postinstall)
        self.assertIn("expected $ExpectedSize, got $ActualSize", postinstall)
        self.assertNotIn("shutdown.exe", postinstall)
        self.assertGreater(
            setup_complete.index("shutdown.exe /r"),
            setup_complete.index("IronDeploy SetupComplete finished"),
        )


if __name__ == "__main__":
    unittest.main()
