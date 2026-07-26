import asyncio
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.programs import (
    MAX_PROGRAM_SIZE_BYTES,
    ProgramError,
    _safe_program_name,
    delete_program,
    list_programs,
    rename_program,
    save_uploaded_program,
    set_program_arguments,
    validate_program_arguments,
)


async def _chunks(*parts: bytes):
    for part in parts:
        yield part


async def _broken_chunks():
    yield b"partial"
    raise RuntimeError("upload interrupted")


class ProgramNameTests(unittest.TestCase):
    def test_exe_and_msi_names_are_accepted(self) -> None:
        self.assertEqual(_safe_program_name("7zip.exe"), "7zip.exe")
        self.assertEqual(_safe_program_name("office.MSI"), "office.MSI")
        self.assertEqual(_safe_program_name("COM10.exe"), "COM10.exe")
        self.assertEqual(_safe_program_name("LPT10.msi"), "LPT10.msi")

    def test_other_suffixes_and_traversal_are_rejected(self) -> None:
        for name in (
            "",
            "setup.bat",
            "setup.exe.txt",
            "../setup.exe",
            "..\\setup.msi",
            "dir/setup.exe",
            " office.MSI ",
            "bad<name.exe",
            "badname.exe.",
            "badname.exe ",
            "CON.exe",
            "prn.msi",
            "COM1.msi",
            "COM1 .msi",
            "com9.exe",
            "LPT1.exe",
            "lpt9.msi",
        ):
            with self.assertRaises(ProgramError):
                _safe_program_name(name)


class ProgramArgumentTests(unittest.TestCase):
    def test_switch_style_arguments_are_accepted(self) -> None:
        self.assertEqual(validate_program_arguments("/S"), "/S")
        self.assertEqual(
            validate_program_arguments("  /qn   /norestart "), "/qn /norestart"
        )
        self.assertEqual(validate_program_arguments("-silent -log:a.txt"),
                         "-silent -log:a.txt")
        self.assertEqual(validate_program_arguments(""), "")

    def test_non_switch_or_shell_arguments_are_rejected(self) -> None:
        for arguments in (
            "quiet",
            "PROPERTY=1",
            '/S "extra"',
            "/a&b",
            "/a|b",
            "/a;b",
            "/" + "x" * 600,
        ):
            with self.assertRaises(ProgramError):
                validate_program_arguments(arguments)


class ProgramManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.programs_dir = self.root / "Programs"
        self.programs_dir.mkdir()
        self.metadata_path = self.programs_dir / ".irondeploy-programs.json"

    def test_upload_saves_file_and_arguments(self) -> None:
        result = asyncio.run(
            save_uploaded_program(
                "7zip.exe",
                _chunks(b"MZ", b"payload"),
                arguments="/S",
                programs_dir=self.programs_dir,
                metadata_path=self.metadata_path,
            )
        )

        self.assertTrue(result["uploaded"])
        self.assertEqual(result["arguments"], "/S")
        self.assertEqual(
            result["sha256"], hashlib.sha256(b"MZpayload").hexdigest()
        )
        self.assertEqual((self.programs_dir / "7zip.exe").read_bytes(), b"MZpayload")
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["programs"]["7zip.exe"]["arguments"], "/S")
        self.assertEqual(metadata["programs"]["7zip.exe"]["sha256"], result["sha256"])

    def test_upload_rejects_files_larger_than_five_gib(self) -> None:
        self.assertEqual(MAX_PROGRAM_SIZE_BYTES, 5 * 1024**3)

        with patch("app.programs.MAX_PROGRAM_SIZE_BYTES", 5):
            with self.assertRaisesRegex(ProgramError, "limited to 5 GiB"):
                asyncio.run(
                    save_uploaded_program(
                        "large.msi",
                        _chunks(b"1234", b"56"),
                        programs_dir=self.programs_dir,
                        metadata_path=self.metadata_path,
                    )
                )

        self.assertFalse((self.programs_dir / "large.msi").exists())
        self.assertEqual(list(self.programs_dir.glob("*.upload-*.tmp")), [])

    def test_failed_upload_removes_temporary_file(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "upload interrupted"):
            asyncio.run(
                save_uploaded_program(
                    "interrupted.exe",
                    _broken_chunks(),
                    programs_dir=self.programs_dir,
                    metadata_path=self.metadata_path,
                )
            )

        self.assertFalse((self.programs_dir / "interrupted.exe").exists())
        self.assertEqual(list(self.programs_dir.glob("*.upload-*.tmp")), [])

    def test_upload_rolls_back_file_when_metadata_write_fails(self) -> None:
        with patch(
            "app.programs._write_metadata",
            side_effect=ProgramError("Failed to save program metadata."),
        ):
            with self.assertRaisesRegex(ProgramError, "Failed to save program metadata"):
                asyncio.run(
                    save_uploaded_program(
                        "rollback.exe",
                        _chunks(b"MZpayload"),
                        programs_dir=self.programs_dir,
                        metadata_path=self.metadata_path,
                    )
                )

        self.assertFalse((self.programs_dir / "rollback.exe").exists())
        self.assertEqual(list(self.programs_dir.glob("*.upload-*.tmp")), [])

    def test_upload_rejects_duplicates_and_empty_files(self) -> None:
        (self.programs_dir / "7zip.exe").write_bytes(b"MZ")

        with self.assertRaisesRegex(ProgramError, "already exists"):
            asyncio.run(
                save_uploaded_program(
                    "7zip.exe",
                    _chunks(b"MZ"),
                    programs_dir=self.programs_dir,
                    metadata_path=self.metadata_path,
                )
            )
        with self.assertRaisesRegex(ProgramError, "empty"):
            asyncio.run(
                save_uploaded_program(
                    "other.exe",
                    _chunks(),
                    programs_dir=self.programs_dir,
                    metadata_path=self.metadata_path,
                )
            )
        self.assertEqual(
            [item.name for item in self.programs_dir.iterdir()], ["7zip.exe"]
        )

    def test_listing_discovers_manual_files_and_drops_stale_records(self) -> None:
        (self.programs_dir / "manual.msi").write_bytes(b"msi")
        self.metadata_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "programs": {"removed.exe": {"arguments": "/S"}},
                }
            ),
            encoding="utf-8",
        )

        result = list_programs(self.programs_dir, self.metadata_path)

        self.assertEqual(
            [(item["name"], item["type"], item["arguments"]) for item in result["programs"]],
            [("manual.msi", "MSI", "")],
        )
        self.assertEqual(
            result["programs"][0]["sha256"], hashlib.sha256(b"msi").hexdigest()
        )
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self.assertNotIn("removed.exe", metadata["programs"])
        self.assertIn("manual.msi", metadata["programs"])

    def test_listing_refreshes_sha256_when_program_changes(self) -> None:
        program_path = self.programs_dir / "agent.exe"
        program_path.write_bytes(b"first")
        first = list_programs(self.programs_dir, self.metadata_path)["programs"][0]

        program_path.write_bytes(b"second payload")
        second = list_programs(self.programs_dir, self.metadata_path)["programs"][0]

        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(
            second["sha256"], hashlib.sha256(b"second payload").hexdigest()
        )

    def test_arguments_are_validated_and_persisted(self) -> None:
        (self.programs_dir / "tool.exe").write_bytes(b"MZ")

        program = set_program_arguments(
            "tool.exe", " /qn  /norestart ", self.programs_dir, self.metadata_path
        )

        self.assertEqual(program["arguments"], "/qn /norestart")
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["programs"]["tool.exe"]["arguments"], "/qn /norestart")

        with self.assertRaises(ProgramError):
            set_program_arguments(
                "tool.exe", "not-a-switch spaced", self.programs_dir, self.metadata_path
            )
        with self.assertRaisesRegex(ProgramError, "not found"):
            set_program_arguments(
                "missing.exe", "/S", self.programs_dir, self.metadata_path
            )

    def test_delete_removes_file_and_metadata(self) -> None:
        asyncio.run(
            save_uploaded_program(
                "tool.exe",
                _chunks(b"MZ"),
                arguments="/S",
                programs_dir=self.programs_dir,
                metadata_path=self.metadata_path,
            )
        )

        result = delete_program("tool.exe", self.programs_dir, self.metadata_path)

        self.assertTrue(result["deleted"])
        self.assertFalse((self.programs_dir / "tool.exe").exists())
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["programs"], {})

        with self.assertRaisesRegex(ProgramError, "not found"):
            delete_program("tool.exe", self.programs_dir, self.metadata_path)

    def test_rename_preserves_program_and_arguments(self) -> None:
        asyncio.run(
            save_uploaded_program(
                "tool.exe",
                _chunks(b"MZpayload"),
                arguments="/S",
                programs_dir=self.programs_dir,
                metadata_path=self.metadata_path,
            )
        )

        result = rename_program(
            "tool.exe", "Company Tool.exe", self.programs_dir, self.metadata_path
        )

        self.assertEqual(result["name"], "Company Tool.exe")
        self.assertEqual(result["arguments"], "/S")
        self.assertFalse((self.programs_dir / "tool.exe").exists())
        self.assertEqual(
            (self.programs_dir / "Company Tool.exe").read_bytes(), b"MZpayload"
        )
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self.assertNotIn("tool.exe", metadata["programs"])
        self.assertEqual(metadata["programs"]["Company Tool.exe"]["arguments"], "/S")

    def test_rename_rejects_conflicts_and_extension_changes(self) -> None:
        (self.programs_dir / "first.exe").write_bytes(b"first")
        (self.programs_dir / "second.exe").write_bytes(b"second")

        with self.assertRaisesRegex(ProgramError, "already exists"):
            rename_program(
                "first.exe", "second.exe", self.programs_dir, self.metadata_path
            )
        with self.assertRaisesRegex(ProgramError, "extension cannot be changed"):
            rename_program(
                "first.exe", "first.msi", self.programs_dir, self.metadata_path
            )

    def test_rename_rolls_back_file_when_metadata_write_fails(self) -> None:
        (self.programs_dir / "before.msi").write_bytes(b"msi")

        with patch(
            "app.programs._write_metadata",
            side_effect=ProgramError("Failed to save program metadata."),
        ):
            with self.assertRaisesRegex(ProgramError, "Failed to save program metadata"):
                rename_program(
                    "before.msi", "after.msi", self.programs_dir, self.metadata_path
                )

        self.assertTrue((self.programs_dir / "before.msi").is_file())
        self.assertFalse((self.programs_dir / "after.msi").exists())


if __name__ == "__main__":
    unittest.main()
