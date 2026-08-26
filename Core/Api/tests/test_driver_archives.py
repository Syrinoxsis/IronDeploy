import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.driver_archives import (
    DriverArchiveError,
    cleanup_driver_archive,
    get_driver_archive_status,
    prepare_driver_archive,
)


class DriverArchiveTests(unittest.TestCase):
    @staticmethod
    def settings(maximum_gib: int = 25) -> SimpleNamespace:
        return SimpleNamespace(
            driver_archive_max_gib=maximum_gib,
            deployment_timeout_minutes=90,
        )

    @staticmethod
    def wait_until_finished(deployment_id: int) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = get_driver_archive_status(deployment_id)
            if status["status"] != "preparing":
                return status
            time.sleep(0.02)
        raise AssertionError("Driver archive task did not finish")

    def test_prepares_one_deployment_tar_and_cleanup_removes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            drivers = Path(temporary) / "Drivers"
            package = drivers / "Vendor" / "Model"
            package.mkdir(parents=True)
            (package / "driver.inf").write_bytes(b"inf")
            (package / "driver.sys").write_bytes(b"sys")
            archive_root = drivers / ".irondeploy-archives"

            def fake_7za(_deployment_id, _source, partial, _timeout, _job):
                partial.write_bytes(b"uncompressed-tar")

            driver_package = {
                "relativePath": "Vendor\\Model",
                "size": 6,
                "fileCount": 2,
                "infCount": 1,
            }
            with (
                patch("app.driver_archives.DRIVERS_DIR", drivers),
                patch("app.driver_archives.ARCHIVE_ROOT", archive_root),
                patch("app.driver_archives._run_7za", side_effect=fake_7za),
            ):
                started = prepare_driver_archive(
                    42, driver_package, self.settings()
                )
                self.assertEqual(started["status"], "preparing")
                ready = self.wait_until_finished(42)
                self.assertEqual(ready["status"], "ready")
                self.assertEqual(
                    ready["archiveRelativePath"],
                    ".irondeploy-archives\\42\\drivers.tar",
                )
                self.assertEqual(ready["archiveSize"], len(b"uncompressed-tar"))
                self.assertTrue((archive_root / "42" / "drivers.tar").is_file())

                cleanup_driver_archive(42)
                self.assertFalse((archive_root / "42").exists())

    def test_changed_package_is_rebuilt_once_with_updated_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            drivers = Path(temporary) / "Drivers"
            package = drivers / "Vendor" / "Model"
            package.mkdir(parents=True)
            source = package / "driver.inf"
            source.write_bytes(b"first")
            archive_root = drivers / ".irondeploy-archives"
            calls = 0

            def fake_7za(_deployment_id, _source, partial, _timeout, _job):
                nonlocal calls
                calls += 1
                partial.write_bytes(b"tar")
                if calls == 1:
                    source.write_bytes(b"second-version")

            with (
                patch("app.driver_archives.DRIVERS_DIR", drivers),
                patch("app.driver_archives.ARCHIVE_ROOT", archive_root),
                patch("app.driver_archives._run_7za", side_effect=fake_7za),
            ):
                prepare_driver_archive(
                    43,
                    {
                        "relativePath": "Vendor\\Model",
                        "size": 5,
                        "fileCount": 1,
                        "infCount": 1,
                    },
                    self.settings(),
                )
                ready = self.wait_until_finished(43)
                self.assertEqual(ready["status"], "ready")
                self.assertEqual(calls, 2)
                self.assertEqual(ready["sourceSize"], len(b"second-version"))
                cleanup_driver_archive(43)

    def test_package_larger_than_hard_limit_is_rejected_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            drivers = Path(temporary) / "Drivers"
            package = drivers / "Vendor" / "Model"
            package.mkdir(parents=True)
            (package / "driver.inf").write_bytes(b"inf")
            archive_root = drivers / ".irondeploy-archives"
            with (
                patch("app.driver_archives.DRIVERS_DIR", drivers),
                patch("app.driver_archives.ARCHIVE_ROOT", archive_root),
                patch(
                    "app.driver_archives._estimate_archive_bytes",
                    return_value=2 * 1024**3,
                ),
            ):
                with self.assertRaisesRegex(
                    DriverArchiveError, "larger than the configured"
                ):
                    prepare_driver_archive(
                        44,
                        {
                            "relativePath": "Vendor\\Model",
                            "size": 3,
                            "fileCount": 1,
                            "infCount": 1,
                        },
                        self.settings(maximum_gib=1),
                    )


if __name__ == "__main__":
    unittest.main()
