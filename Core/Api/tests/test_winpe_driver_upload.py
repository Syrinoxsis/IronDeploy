import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import winpe_driver_upload
from app.drivers import DriverUploadLimits
from app.winpe_driver_upload import (
    WinPEDriverUploadError,
    begin_winpe_driver_upload,
    cancel_winpe_driver_upload,
    delete_winpe_drivers,
    finalize_winpe_driver_upload,
    get_winpe_drivers,
    save_winpe_driver_file,
)


async def chunks(*values: bytes):
    for value in values:
        yield value


class WinPEDriverUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.drivers = root / "WinPEDrivers"
        self.uploads = root / ".winpe-driver-uploads"
        self.limits = DriverUploadLimits(
            max_files=10,
            max_depth=5,
            max_full_path=200,
            min_free_space_gib=0,
        )
        self.patches = (
            patch.object(winpe_driver_upload, "WINPE_DRIVERS_DIR", self.drivers),
            patch.object(winpe_driver_upload, "UPLOADS_DIR", self.uploads),
        )
        for item in self.patches:
            item.start()
        winpe_driver_upload._active_uploads.clear()

    def tearDown(self) -> None:
        winpe_driver_upload._active_uploads.clear()
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    async def test_upload_preserves_tree_and_replaces_previous_set(self) -> None:
        self.drivers.mkdir(parents=True)
        (self.drivers / "old.inf").write_bytes(b"old")
        upload_id = begin_winpe_driver_upload(self.limits)["uploadId"]

        await save_winpe_driver_file(
            upload_id,
            "Network/adapter.inf",
            chunks(b"inf"),
            self.limits,
        )
        await save_winpe_driver_file(
            upload_id,
            "Network/adapter.sys",
            chunks(b"sys"),
            self.limits,
        )
        result = finalize_winpe_driver_upload(upload_id)

        self.assertEqual(result["fileCount"], 2)
        self.assertEqual(result["infCount"], 1)
        self.assertFalse((self.drivers / "old.inf").exists())
        self.assertEqual(
            (self.drivers / "Network" / "adapter.sys").read_bytes(),
            b"sys",
        )

    async def test_rejects_unsafe_relative_paths(self) -> None:
        upload_id = begin_winpe_driver_upload(self.limits)["uploadId"]
        for path in ("../escape.inf", "/root.inf", "folder\\driver.inf"):
            with self.subTest(path=path):
                with self.assertRaises(WinPEDriverUploadError):
                    await save_winpe_driver_file(
                        upload_id,
                        path,
                        chunks(b"data"),
                        self.limits,
                    )
        cancel_winpe_driver_upload(upload_id)

    async def test_finalize_requires_an_inf(self) -> None:
        upload_id = begin_winpe_driver_upload(self.limits)["uploadId"]
        await save_winpe_driver_file(
            upload_id,
            "driver.sys",
            chunks(b"data"),
            self.limits,
        )
        with self.assertRaisesRegex(WinPEDriverUploadError, "no INF"):
            finalize_winpe_driver_upload(upload_id)
        cancel_winpe_driver_upload(upload_id)

    def test_delete_removes_only_published_winpe_drivers(self) -> None:
        self.drivers.mkdir(parents=True)
        (self.drivers / "driver.inf").write_bytes(b"data")
        self.uploads.mkdir(parents=True)

        delete_winpe_drivers()

        self.assertFalse(self.drivers.exists())
        self.assertTrue(self.uploads.exists())
        self.assertFalse(get_winpe_drivers()["available"])


if __name__ == "__main__":
    unittest.main()
