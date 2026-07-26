import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.drivers import (
    DriverError,
    begin_driver_package_upload,
    cancel_driver_package_upload,
    create_vendor,
    delete_driver_package,
    delete_vendor,
    finalize_driver_package_upload,
    list_driver_packages,
    rename_driver_package,
    rename_vendor,
    save_uploaded_driver_file,
)


async def _chunks(*parts: bytes):
    for part in parts:
        yield part


class DriverManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.drivers_dir = self.root / "Drivers"
        self.drivers_dir.mkdir()

    def test_vendor_create_rename_and_delete(self) -> None:
        create_vendor("Lenovo", self.drivers_dir)
        (self.drivers_dir / "Lenovo" / "ThinkPad T14").mkdir()

        renamed = rename_vendor("lenovo", "LENOVO", self.drivers_dir)

        self.assertEqual(renamed["name"], "LENOVO")
        self.assertTrue(
            (self.drivers_dir / "LENOVO" / "ThinkPad T14").is_dir()
        )
        deleted = delete_vendor("lenovo", self.drivers_dir)
        self.assertTrue(deleted["deleted"])
        self.assertFalse((self.drivers_dir / "LENOVO").exists())

    def test_vendor_names_reject_traversal_reserved_and_duplicates(self) -> None:
        create_vendor("Dell", self.drivers_dir)
        for name in ("../HP", "bad/name", "NUL", "COM1.tools", " Dell "):
            with self.subTest(name=name), self.assertRaises(DriverError):
                create_vendor(name, self.drivers_dir)
        with self.assertRaisesRegex(DriverError, "already exists"):
            create_vendor("dell", self.drivers_dir)

    def test_package_upload_preserves_nested_structure_and_counts_inf(self) -> None:
        create_vendor("HP", self.drivers_dir)
        upload = begin_driver_package_upload(
            "hp", "EliteBook 840 G10", self.drivers_dir
        )

        asyncio.run(
            save_uploaded_driver_file(
                upload["uploadId"],
                "Audio/Realtek/audio.inf",
                _chunks(b"inf", b"-payload"),
                self.drivers_dir,
            )
        )
        asyncio.run(
            save_uploaded_driver_file(
                upload["uploadId"],
                "Audio/Realtek/audio.cat",
                _chunks(b"catalog"),
                self.drivers_dir,
            )
        )
        asyncio.run(
            save_uploaded_driver_file(
                upload["uploadId"],
                "Chipset/chipset.INF",
                _chunks(b"chipset"),
                self.drivers_dir,
            )
        )
        result = finalize_driver_package_upload(
            upload["uploadId"], self.drivers_dir
        )

        package = result["package"]
        self.assertEqual(package["vendor"], "HP")
        self.assertEqual(package["model"], "EliteBook 840 G10")
        self.assertEqual(package["infCount"], 2)
        self.assertEqual(package["fileCount"], 3)
        self.assertEqual(package["size"], 25)
        self.assertEqual(package["relativePath"], "HP\\EliteBook 840 G10")
        self.assertEqual(
            (
                self.drivers_dir
                / "HP"
                / "EliteBook 840 G10"
                / "Audio"
                / "Realtek"
                / "audio.inf"
            ).read_bytes(),
            b"inf-payload",
        )

    def test_finalize_requires_an_inf_and_does_not_publish_partial_package(self) -> None:
        create_vendor("Dell", self.drivers_dir)
        upload = begin_driver_package_upload(
            "Dell", "Latitude 7450", self.drivers_dir
        )
        asyncio.run(
            save_uploaded_driver_file(
                upload["uploadId"],
                "readme.txt",
                _chunks(b"notes"),
                self.drivers_dir,
            )
        )

        with self.assertRaisesRegex(DriverError, "no INF"):
            finalize_driver_package_upload(upload["uploadId"], self.drivers_dir)

        self.assertFalse(
            (self.drivers_dir / "Dell" / "Latitude 7450").exists()
        )
        cancel_driver_package_upload(upload["uploadId"], self.drivers_dir)

    def test_upload_rejects_traversal_duplicate_paths_and_size_limit(self) -> None:
        create_vendor("Dell", self.drivers_dir)
        upload = begin_driver_package_upload(
            "Dell", "OptiPlex", self.drivers_dir
        )
        for path in (
            "../evil.inf",
            "/absolute.inf",
            "folder\\evil.inf",
            "folder/../../evil.inf",
            "folder/./evil.inf",
            "folder//evil.inf",
            "folder/NUL.inf",
        ):
            with self.subTest(path=path), self.assertRaises(DriverError):
                asyncio.run(
                    save_uploaded_driver_file(
                        upload["uploadId"],
                        path,
                        _chunks(b"content"),
                        self.drivers_dir,
                    )
                )

    def test_file_upload_rolls_back_when_upload_state_cannot_be_saved(self) -> None:
        create_vendor("HP", self.drivers_dir)
        upload = begin_driver_package_upload(
            "HP", "EliteDesk", self.drivers_dir
        )

        with patch(
            "app.drivers._write_upload",
            side_effect=DriverError("Failed to save driver upload state."),
        ):
            with self.assertRaisesRegex(DriverError, "upload state"):
                asyncio.run(
                    save_uploaded_driver_file(
                        upload["uploadId"],
                        "Network/net.inf",
                        _chunks(b"driver"),
                        self.drivers_dir,
                    )
                )

        staging_files = (
            self.drivers_dir
            / ".irondeploy-uploads"
            / upload["uploadId"]
            / "files"
        )
        self.assertEqual(
            [path for path in staging_files.rglob("*") if path.is_file()],
            [],
        )

        asyncio.run(
            save_uploaded_driver_file(
                upload["uploadId"],
                "Network/driver.inf",
                _chunks(b"first"),
                self.drivers_dir,
            )
        )
        with self.assertRaisesRegex(DriverError, "twice"):
            asyncio.run(
                save_uploaded_driver_file(
                    upload["uploadId"],
                    "network/DRIVER.INF",
                    _chunks(b"second"),
                    self.drivers_dir,
                )
            )

        with patch("app.drivers.MAX_DRIVER_FILE_SIZE_BYTES", 3):
            with self.assertRaisesRegex(DriverError, "5 GiB"):
                asyncio.run(
                    save_uploaded_driver_file(
                        upload["uploadId"],
                        "large.bin",
                        _chunks(b"1234"),
                        self.drivers_dir,
                    )
                )

    def test_package_rename_delete_and_listing(self) -> None:
        create_vendor("Lenovo", self.drivers_dir)
        package = self.drivers_dir / "Lenovo" / "T14"
        (package / "nested").mkdir(parents=True)
        (package / "nested" / "net.inf").write_bytes(b"123")
        (package / "firmware.bin").write_bytes(b"12")

        listing = list_driver_packages(self.drivers_dir)
        self.assertEqual(listing["vendors"][0]["packageCount"], 1)
        self.assertEqual(listing["packages"][0]["size"], 5)
        self.assertEqual(listing["packages"][0]["infCount"], 1)

        renamed = rename_driver_package(
            "lenovo", "t14", "ThinkPad T14", self.drivers_dir
        )
        self.assertEqual(renamed["model"], "ThinkPad T14")
        self.assertTrue(
            (
                self.drivers_dir
                / "Lenovo"
                / "ThinkPad T14"
                / "nested"
                / "net.inf"
            ).is_file()
        )
        deleted = delete_driver_package(
            "Lenovo", "thinkpad t14", self.drivers_dir
        )
        self.assertTrue(deleted["deleted"])
        self.assertEqual(list_driver_packages(self.drivers_dir)["packages"], [])

    def test_upload_rejects_missing_vendor_and_existing_package(self) -> None:
        with self.assertRaisesRegex(DriverError, "Vendor not found"):
            begin_driver_package_upload("HP", "Model", self.drivers_dir)

        create_vendor("HP", self.drivers_dir)
        (self.drivers_dir / "HP" / "Model").mkdir()
        with self.assertRaisesRegex(DriverError, "already exists"):
            begin_driver_package_upload("hp", "model", self.drivers_dir)


if __name__ == "__main__":
    unittest.main()
