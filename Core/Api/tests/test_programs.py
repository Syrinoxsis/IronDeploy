import asyncio
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.programs import (
    MAX_ARGUMENTS_LENGTH,
    MAX_PROGRAM_SIZE_BYTES,
    ProgramError,
    _safe_program_name,
    _safe_relative_path,
    add_program_file,
    cancel_program_upload,
    delete_program,
    delete_program_file,
    finalize_program_upload,
    list_programs,
    rename_program,
    save_program_upload_file,
    save_uploaded_program,
    set_program_arguments,
    set_program_enabled,
    set_program_entrypoint,
    start_program_upload,
    validate_program_arguments,
)

STATIC_ROOT = Path(__file__).resolve().parents[1] / "app" / "static"
CORE_ROOT = Path(__file__).resolve().parents[2]


async def _chunks(*parts: bytes):
    for part in parts:
        yield part


async def _broken_chunks():
    yield b"partial"
    raise RuntimeError("upload interrupted")


class ProgramNameTests(unittest.TestCase):
    def test_package_names_and_relative_paths_are_validated(self) -> None:
        self.assertEqual(_safe_program_name("7-Zip"), "7-Zip")
        self.assertEqual(_safe_program_name("Office 365"), "Office 365")
        self.assertEqual(_safe_relative_path("data/config.xml"), "data\\config.xml")
        self.assertEqual(
            _safe_relative_path("bin/setup.EXE", entrypoint=True),
            "bin\\setup.EXE",
        )

        for name in (
            "", "../setup", "..\\setup", " office ", "bad<name", "CON",
            ".irondeploy-program-uploads",
        ):
            with self.subTest(name=name), self.assertRaises(ProgramError):
                _safe_program_name(name)

        for path in ("", "..\\setup.exe", "C:\\setup.exe", "dir\\", "a\\.\\b"):
            with self.subTest(path=path), self.assertRaises(ProgramError):
                _safe_relative_path(path)

        with self.assertRaisesRegex(ProgramError, r"\.exe or \.msi"):
            _safe_relative_path("setup.cmd", entrypoint=True)


class ProgramArgumentTests(unittest.TestCase):
    def test_raw_installer_arguments_are_accepted_without_rewriting(self) -> None:
        for arguments in (
            "/qn /norestart ALLUSERS=1",
            "/S ALLUSERS=1",
            "--quiet --server https://example.test",
            'INSTALLDIR="C:\\Program Files\\App"',
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(validate_program_arguments(arguments), arguments)
        self.assertEqual(validate_program_arguments("  /qn   /norestart  "), "/qn   /norestart")
        self.assertEqual(validate_program_arguments("x" * MAX_ARGUMENTS_LENGTH), "x" * MAX_ARGUMENTS_LENGTH)
        self.assertEqual(validate_program_arguments(""), "")

    def test_invalid_arguments_are_rejected(self) -> None:
        for arguments in (None, 123, ["/S"], {"value": "/S"}):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(
                ProgramError, r"^arguments must be a string\.$"
            ):
                validate_program_arguments(arguments)
        for arguments in ("/S\0X", "/S\rX", "/S\nX", "x" * (MAX_ARGUMENTS_LENGTH + 1)):
            with self.subTest(arguments=repr(arguments)), self.assertRaises(ProgramError):
                validate_program_arguments(arguments)


class ProgramManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.programs_dir = self.root / "Programs"
        self.programs_dir.mkdir()
        self.metadata_path = self.programs_dir / ".irondeploy-programs.json"

    def upload_package(self, name="Company Agent") -> dict:
        started = start_program_upload(
            name,
            "bin\\setup.exe",
            [
                {"path": "bin\\setup.exe", "size": 9},
                {"path": "config\\agent.json", "size": 2},
            ],
            arguments="/S",
            programs_dir=self.programs_dir,
        )
        asyncio.run(
            save_program_upload_file(
                started["uploadId"],
                "bin/setup.exe",
                _chunks(b"installer"),
                self.programs_dir,
            )
        )
        asyncio.run(
            save_program_upload_file(
                started["uploadId"],
                "config/agent.json",
                _chunks(b"{}"),
                self.programs_dir,
            )
        )
        return finalize_program_upload(
            started["uploadId"], self.programs_dir, self.metadata_path
        )

    def test_multi_file_upload_publishes_directory_and_hashes_every_file(self) -> None:
        result = self.upload_package()

        self.assertTrue(result["uploaded"])
        self.assertEqual(result["name"], "Company Agent")
        self.assertEqual(result["entrypoint"], "bin\\setup.exe")
        self.assertEqual(result["fileCount"], 2)
        self.assertEqual(result["size"], 11)
        self.assertEqual(
            (self.programs_dir / "Company Agent" / "config" / "agent.json").read_bytes(),
            b"{}",
        )
        self.assertEqual(
            result["files"][0]["sha256"], hashlib.sha256(b"installer").hexdigest()
        )
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["version"], 4)
        self.assertEqual(metadata["programs"]["Company Agent"]["arguments"], "/S")

    def test_incomplete_upload_cannot_be_published_and_can_be_cancelled(self) -> None:
        started = start_program_upload(
            "Office",
            "setup.exe",
            [{"path": "setup.exe", "size": 2}, {"path": "config.xml", "size": 3}],
            programs_dir=self.programs_dir,
        )
        asyncio.run(
            save_program_upload_file(
                started["uploadId"], "setup.exe", _chunks(b"MZ"), self.programs_dir
            )
        )
        with self.assertRaisesRegex(ProgramError, "incomplete"):
            finalize_program_upload(started["uploadId"], self.programs_dir, self.metadata_path)
        self.assertTrue(cancel_program_upload(started["uploadId"], self.programs_dir)["deleted"])
        self.assertFalse((self.programs_dir / "Office").exists())

    def test_upload_rejects_traversal_duplicates_and_invalid_sizes(self) -> None:
        with self.assertRaises(ProgramError):
            start_program_upload(
                "Bad", "setup.exe", [{"path": "..\\setup.exe", "size": 1}],
                programs_dir=self.programs_dir,
            )
        with self.assertRaisesRegex(ProgramError, "Duplicate"):
            start_program_upload(
                "Bad", "setup.exe",
                [{"path": "setup.exe", "size": 1}, {"path": "SETUP.EXE", "size": 1}],
                programs_dir=self.programs_dir,
            )
        with patch("app.programs.MAX_PROGRAM_SIZE_BYTES", 5), self.assertRaisesRegex(
            ProgramError, "limited to 5 GiB"
        ):
            start_program_upload(
                "Large", "setup.msi", [{"path": "setup.msi", "size": 6}],
                programs_dir=self.programs_dir,
            )

    def test_single_installer_compatibility_upload_creates_package_folder(self) -> None:
        result = asyncio.run(
            save_uploaded_program(
                "7zip.exe", _chunks(b"MZ", b"payload"), arguments="/S",
                programs_dir=self.programs_dir, metadata_path=self.metadata_path,
            )
        )
        self.assertEqual(result["name"], "7zip")
        self.assertEqual(result["entrypoint"], "7zip.exe")
        self.assertEqual((self.programs_dir / "7zip" / "7zip.exe").read_bytes(), b"MZpayload")

    def test_failed_single_upload_cleans_staging_directory(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "upload interrupted"):
            asyncio.run(
                save_uploaded_program(
                    "broken.exe", _broken_chunks(), programs_dir=self.programs_dir,
                    metadata_path=self.metadata_path,
                )
            )
        uploads = self.programs_dir / ".irondeploy-program-uploads"
        self.assertEqual(list(uploads.iterdir()) if uploads.exists() else [], [])

    def test_legacy_flat_installer_is_migrated_to_package(self) -> None:
        (self.programs_dir / "manual.msi").write_bytes(b"msi")
        self.metadata_path.write_text(
            json.dumps({"version": 3, "programs": {"manual.msi": {"arguments": "/qn"}}}),
            encoding="utf-8",
        )

        program = list_programs(self.programs_dir, self.metadata_path)["programs"][0]

        self.assertEqual(program["name"], "manual")
        self.assertEqual(program["entrypoint"], "manual.msi")
        self.assertEqual(program["arguments"], "/qn")
        self.assertTrue((self.programs_dir / "manual" / "manual.msi").is_file())
        self.assertFalse((self.programs_dir / "manual.msi").exists())

    def test_listing_refreshes_package_hash_when_supporting_file_changes(self) -> None:
        self.upload_package()
        first = list_programs(self.programs_dir, self.metadata_path)["programs"][0]
        (self.programs_dir / "Company Agent" / "config" / "agent.json").write_bytes(b'{"x":1}')
        second = list_programs(self.programs_dir, self.metadata_path)["programs"][0]
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(second["size"], 16)

    def test_package_settings_rename_and_delete(self) -> None:
        self.upload_package()
        updated = set_program_arguments(
            "Company Agent", " /qn  /norestart ", self.programs_dir, self.metadata_path
        )
        self.assertEqual(updated["arguments"], "/qn  /norestart")
        self.assertFalse(
            set_program_enabled(
                "Company Agent", False, self.programs_dir, self.metadata_path
            )["enabled"]
        )
        renamed = rename_program(
            "Company Agent", "Corporate Agent", self.programs_dir, self.metadata_path
        )
        self.assertEqual(renamed["name"], "Corporate Agent")
        self.assertTrue((self.programs_dir / "Corporate Agent" / "bin" / "setup.exe").is_file())
        self.assertTrue(delete_program("Corporate Agent", self.programs_dir, self.metadata_path)["deleted"])
        self.assertFalse((self.programs_dir / "Corporate Agent").exists())

    def test_add_file_change_entrypoint_and_remove_file(self) -> None:
        self.upload_package()
        asyncio.run(
            add_program_file(
                "Company Agent", "tools\\repair.exe", _chunks(b"repair"),
                self.programs_dir, self.metadata_path,
            )
        )
        changed = set_program_entrypoint(
            "Company Agent", "tools/repair.exe", self.programs_dir, self.metadata_path
        )
        self.assertEqual(changed["entrypoint"], "tools\\repair.exe")
        deleted = delete_program_file(
            "Company Agent", "bin\\setup.exe", self.programs_dir, self.metadata_path
        )
        self.assertTrue(deleted["deleted"])
        delete_program_file(
            "Company Agent", "tools\\repair.exe", self.programs_dir, self.metadata_path
        )
        program = list_programs(self.programs_dir, self.metadata_path)["programs"][0]
        self.assertFalse(program["ready"])
        self.assertEqual(program["entrypoint"], "")
        self.assertIn("No .exe or .msi", program["warning"])
        asyncio.run(add_program_file(
            "Company Agent", "new.msi", _chunks(b"installer"),
            self.programs_dir, self.metadata_path,
        ))
        program = list_programs(self.programs_dir, self.metadata_path)["programs"][0]
        self.assertTrue(program["ready"])
        self.assertEqual(program["entrypoint"], "new.msi")

    def test_empty_folder_does_not_block_valid_packages(self):
        self.upload_package()
        (self.programs_dir / "Empty").mkdir()
        packages = list_programs(self.programs_dir, self.metadata_path)["programs"]
        self.assertEqual(len(packages), 2)
        self.assertTrue(packages[0]["ready"])
        self.assertFalse(packages[1]["ready"])
        self.assertTrue(delete_program("Empty", self.programs_dir, self.metadata_path)["deleted"])

    def test_duplicate_upload_is_rejected_without_overwriting_first(self):
        self.upload_package()

        async def exercise():
            entered, release = asyncio.Event(), asyncio.Event()

            async def first_chunks():
                entered.set()
                await release.wait()
                yield b"first"

            task = asyncio.create_task(add_program_file(
                "Company Agent", "shared.dat", first_chunks(),
                self.programs_dir, self.metadata_path,
            ))
            await entered.wait()
            try:
                with self.assertRaisesRegex(ProgramError, "Rename your file"):
                    await add_program_file(
                        "Company Agent", "SHARED.DAT", _chunks(b"second"),
                        self.programs_dir, self.metadata_path,
                    )
            finally:
                release.set()
                await task
            with self.assertRaisesRegex(ProgramError, "Rename your file"):
                await add_program_file(
                    "Company Agent", "shared.dat", _chunks(b"third"),
                    self.programs_dir, self.metadata_path,
                )

        asyncio.run(exercise())
        self.assertEqual((self.programs_dir / "Company Agent/shared.dat").read_bytes(), b"first")

    def test_failed_add_releases_filename_for_retry(self):
        self.upload_package()
        with self.assertRaises(RuntimeError):
            asyncio.run(add_program_file(
                "Company Agent", "retry.dat", _broken_chunks(),
                self.programs_dir, self.metadata_path,
            ))
        asyncio.run(add_program_file(
            "Company Agent", "retry.dat", _chunks(b"retry"),
            self.programs_dir, self.metadata_path,
        ))

    def test_rename_rolls_back_directory_when_metadata_write_fails(self) -> None:
        self.upload_package("Before")
        with patch(
            "app.programs._write_metadata",
            side_effect=ProgramError("Failed to save program metadata."),
        ), self.assertRaisesRegex(ProgramError, "Failed to save program metadata"):
            rename_program("Before", "After", self.programs_dir, self.metadata_path)
        self.assertTrue((self.programs_dir / "Before").is_dir())
        self.assertFalse((self.programs_dir / "After").exists())

    def test_entrypoint_and_package_limits_are_enforced(self) -> None:
        self.assertEqual(MAX_PROGRAM_SIZE_BYTES, 5 * 1024**3)
        with self.assertRaisesRegex(ProgramError, "entrypoint"):
            start_program_upload(
                "Missing", "setup.exe", [{"path": "readme.txt", "size": 1}],
                programs_dir=self.programs_dir,
            )


class ProgramPageTests(unittest.TestCase):
    def test_programs_page_exposes_package_upload_and_file_management(self) -> None:
        page = (STATIC_ROOT / "programs.html").read_text(encoding="utf-8")
        script = (STATIC_ROOT / "programs.js").read_text(encoding="utf-8")
        styles = (STATIC_ROOT / "programs.css").read_text(encoding="utf-8")

        self.assertIn('id="installer-input"', page)
        self.assertIn('id="folder-input"', page)
        self.assertIn("The installer always runs from the package root", page)
        self.assertIn("/api/programs/uploads", script)
        self.assertIn("+ Add files", script)
        self.assertIn("width: min(100% - 32px, 1440px)", styles)


class ProgramRuntimeIntegrationTests(unittest.TestCase):
    def test_winpe_stages_complete_packages_and_writes_file_manifest(self) -> None:
        engine = (
            CORE_ROOT / "WinPE" / "Runtime" / "IronDeploy.Engine.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("-Recurse", engine)
        self.assertIn("$SelectedProgram.files", engine)
        self.assertIn("Program package file SHA-256 mismatch", engine)
        self.assertIn("entrypoint = [string]$SelectedProgram.entrypoint", engine)
        self.assertIn("ConvertTo-Json -InputObject @($ProgramManifest) -Depth 6", engine)

    def test_postinstall_verifies_package_and_runs_from_package_root(self) -> None:
        postinstall = (
            CORE_ROOT / "ServerTemplates" / "PostInstall" / "postinstall.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("$ProgramEntrypoint = [string]$Program.entrypoint", postinstall)
        self.assertIn("$ActualFiles.Count -ne $DeclaredFiles.Count", postinstall)
        self.assertIn("Get-FileHash -LiteralPath $DeclaredFilePath", postinstall)
        self.assertIn("-WorkingDirectory $PackageRoot", postinstall)


if __name__ == "__main__":
    unittest.main()
