import asyncio
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app.deployment_images as deployment_images
from app.deployment_images import (
    DeploymentImageError,
    _finish_conversion,
    _inspect_image,
    _parse_wim_info,
    list_deployment_images,
    rename_deployment_image,
    save_uploaded_image,
    set_default_image_index,
)

STATIC_ROOT = Path(__file__).resolve().parents[1] / "app" / "static"


DISM_OUTPUT = """
Deployment Image Servicing and Management tool

Details for image : install.wim

Index : 1
Name : Windows 11 Home
Description : Windows 11 Home
Size : 18,000,000,000 bytes

Index : 6
Name : Windows 11 Pro
Description : Windows 11 Pro for workstations
Architecture : x64

The operation completed successfully.
"""


class DeploymentImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.images_dir = self.root / "Images"
        self.images_dir.mkdir()
        self.metadata_path = self.images_dir / ".irondeploy-images.json"
        deployment_images._conversion_cancel.clear()

    def test_dism_output_is_parsed_into_named_indexes(self) -> None:
        indexes = _parse_wim_info(DISM_OUTPUT)

        self.assertEqual([item["index"] for item in indexes], [1, 6])
        self.assertEqual(indexes[0]["name"], "Windows 11 Home")
        self.assertEqual(indexes[1]["name"], "Windows 11 Pro")
        self.assertEqual(indexes[1]["architecture"], "x64")

    @patch("app.deployment_images.subprocess.run")
    def test_inspection_preserves_unicode_edition_names(self, run) -> None:
        payload = json.dumps(
            [
                {
                    "index": 1,
                    "name": "Windows 11 Домашняя",
                    "description": "Windows 11 Домашняя для одного языка",
                    "architecture": "x64",
                }
            ],
            ensure_ascii=False,
        ).encode("utf-8")
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout=payload,
            stderr=b"",
        )

        indexes = _inspect_image(self.images_dir / "install.wim")

        self.assertEqual(indexes[0]["name"], "Windows 11 Домашняя")
        self.assertIn("одного языка", indexes[0]["description"])
        self.assertEqual(
            run.call_args.kwargs["env"]["IRONDEPLOY_IMAGE_PATH"],
            str(self.images_dir / "install.wim"),
        )

    @patch("app.deployment_images._legacy_default_index", return_value=6)
    @patch("app.deployment_images._inspect_image")
    def test_refresh_discovers_manual_files_and_caches_indexes(
        self, inspect_image, _legacy_index
    ) -> None:
        inspect_image.return_value = _parse_wim_info(DISM_OUTPUT)
        (self.images_dir / "install.wim").write_bytes(b"wim")
        (self.images_dir / "install.esd").write_bytes(b"esd")

        result = list_deployment_images(self.images_dir, self.metadata_path)

        self.assertEqual([item["name"] for item in result["images"]], [
            "install.esd",
            "install.wim",
        ])
        self.assertEqual(result["images"][0]["defaultIndex"], 6)
        self.assertTrue(result["images"][0]["canConvert"])
        self.assertFalse(result["images"][1]["canConvert"])
        self.assertEqual(
            result["images"][1]["sha256"],
            hashlib.sha256(b"wim").hexdigest(),
        )
        self.assertEqual(inspect_image.call_count, 2)

        list_deployment_images(self.images_dir, self.metadata_path)
        self.assertEqual(inspect_image.call_count, 2)

    @patch("app.deployment_images._legacy_default_index", return_value=1)
    @patch("app.deployment_images._inspect_image")
    def test_default_index_is_validated_and_persisted(
        self, inspect_image, _legacy_index
    ) -> None:
        inspect_image.return_value = _parse_wim_info(DISM_OUTPUT)
        (self.images_dir / "install.wim").write_bytes(b"wim")

        selected = set_default_image_index(
            "install.wim", 6, self.images_dir, self.metadata_path
        )

        self.assertEqual(selected["defaultIndex"], 6)
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["images"]["install.wim"]["defaultIndex"], 6)
        with self.assertRaises(DeploymentImageError):
            set_default_image_index(
                "install.wim", 99, self.images_dir, self.metadata_path
            )

    @patch("app.deployment_images._inspect_image")
    def test_refresh_does_not_publish_a_file_that_is_still_changing(
        self, inspect_image
    ) -> None:
        image_path = self.images_dir / "install.wim"
        image_path.write_bytes(b"partial")

        def inspect_changing_image(path):
            path.write_bytes(b"partial-but-still-growing")
            return _parse_wim_info(DISM_OUTPUT)

        inspect_image.side_effect = inspect_changing_image
        result = list_deployment_images(self.images_dir, self.metadata_path)

        image = result["images"][0]
        self.assertEqual(image["indexes"], [])
        self.assertIn("still be in progress", image["inspectionError"])

    @patch("app.deployment_images._legacy_default_index", return_value=1)
    @patch("app.deployment_images._inspect_image")
    def test_wim_rename_preserves_metadata_and_default_index(
        self, inspect_image, _legacy_index
    ) -> None:
        inspect_image.return_value = _parse_wim_info(DISM_OUTPUT)
        (self.images_dir / "install.wim").write_bytes(b"wim")
        set_default_image_index(
            "install.wim", 6, self.images_dir, self.metadata_path
        )

        result = rename_deployment_image(
            "install.wim", "Windows 11 Pro.wim", self.images_dir, self.metadata_path
        )

        self.assertFalse((self.images_dir / "install.wim").exists())
        self.assertTrue((self.images_dir / "Windows 11 Pro.wim").is_file())
        self.assertEqual(result["defaultIndex"], 6)
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self.assertNotIn("install.wim", metadata["images"])
        self.assertEqual(
            metadata["images"]["Windows 11 Pro.wim"]["defaultIndex"], 6
        )

    def test_wim_rename_rejects_conflicts_and_non_wim_names(self) -> None:
        (self.images_dir / "first.wim").write_bytes(b"first")
        (self.images_dir / "second.wim").write_bytes(b"second")

        with self.assertRaises(DeploymentImageError):
            rename_deployment_image(
                "first.wim", "second.wim", self.images_dir, self.metadata_path
            )
        with self.assertRaises(DeploymentImageError):
            rename_deployment_image(
                "first.wim", "first.esd", self.images_dir, self.metadata_path
            )

    def test_upload_is_streamed_and_rejects_overwrite(self) -> None:
        async def chunks():
            yield b"first"
            yield b"second"

        result = asyncio.run(
            save_uploaded_image("install.esd", chunks(), self.images_dir)
        )

        self.assertEqual(result["size"], 11)
        self.assertTrue(result["canConvert"])
        self.assertEqual(
            (self.images_dir / "install.esd").read_bytes(), b"firstsecond"
        )
        with self.assertRaises(DeploymentImageError):
            asyncio.run(
                save_uploaded_image("install.esd", chunks(), self.images_dir)
            )

    def test_upload_rejects_paths_and_other_extensions(self) -> None:
        async def chunks():
            yield b"content"

        for name in (
            "../install.wim",
            "folder/install.esd",
            "setup.exe",
            " install.wim ",
            "bad<name.wim",
            "badname.wim.",
            "CON.wim",
            "com1.esd",
            "LPT1 .esd",
            "LPT9.wim",
        ):
            with self.subTest(name=name), self.assertRaises(DeploymentImageError):
                asyncio.run(save_uploaded_image(name, chunks(), self.images_dir))

    def test_com10_and_lpt10_image_names_are_allowed(self) -> None:
        async def chunks():
            yield b"content"

        for name in ("COM10.wim", "LPT10.esd"):
            with self.subTest(name=name):
                result = asyncio.run(
                    save_uploaded_image(name, chunks(), self.images_dir)
                )
                self.assertEqual(result["name"], name)

    @patch("app.deployment_images.subprocess.Popen")
    def test_conversion_exports_all_indexes_and_publishes_atomically(
        self, popen
    ) -> None:
        source = self.images_dir / "install.esd"
        destination = self.images_dir / "install.wim"
        working = self.root / "conversion.wim"
        log = self.root / "conversion.log"
        source.write_bytes(b"esd")

        def export(command, **_kwargs):
            destination_argument = next(
                item for item in command if item.startswith("/DestinationImageFile:")
            )
            output = Path(destination_argument.split(":", 1)[1])
            with output.open("ab") as handle:
                handle.write(b"index")
            return SimpleNamespace(wait=lambda timeout: 0)

        popen.side_effect = export
        _finish_conversion(source, destination, working, [1, 6], log)

        self.assertTrue(destination.is_file())
        self.assertFalse(working.exists())
        self.assertEqual(popen.call_count, 2)
        source_indexes = [
            next(item for item in call.args[0] if item.startswith("/SourceIndex:"))
            for call in popen.call_args_list
        ]
        self.assertEqual(source_indexes, ["/SourceIndex:1", "/SourceIndex:6"])

    @patch("app.deployment_images.subprocess.Popen")
    def test_cancelled_conversion_removes_temporary_wim(self, popen) -> None:
        source = self.images_dir / "install.esd"
        destination = self.images_dir / "install.wim"
        working = self.root / "conversion.wim"
        log = self.root / "conversion.log"
        source.write_bytes(b"esd")
        working.write_bytes(b"partial")
        deployment_images._conversion_cancel.set()

        _finish_conversion(source, destination, working, [1], log)

        popen.assert_not_called()
        self.assertFalse(working.exists())
        self.assertFalse(destination.exists())
        self.assertEqual(
            deployment_images.get_conversion_state()["status"], "cancelled"
        )


class DeploymentImagePageTests(unittest.TestCase):
    def test_images_page_uses_dark_page_scope_and_hides_idle_progress(self) -> None:
        html = (STATIC_ROOT / "images.html").read_text(encoding="utf-8")
        css = (STATIC_ROOT / "images.css").read_text(encoding="utf-8")

        self.assertIn('class="images-page" data-page="images"', html)
        self.assertIn(".progress-row[hidden]", css)
        self.assertIn("body.images-page", css)


if __name__ == "__main__":
    unittest.main()
