import os
import subprocess
import tempfile
import time
import unittest
from ipaddress import IPv4Network
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.domain_join import (
    DomainJoinError,
    delete_domain_join_blob,
    get_domain_join_blob,
    provision_domain_join_blob,
    purge_stale_domain_join_blobs,
)


def age_file(path: Path, minutes: float) -> None:
    old = time.time() - minutes * 60
    os.utime(path, (old, old))


class DomainJoinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.djoin_path = self.root / "djoin.exe"
        self.djoin_path.touch()
        self.settings = Settings(
            database_url="sqlite:///test.db",
            name_prefix="pc",
            name_width=5,
            name_start=1,
            allowed_client_networks=(IPv4Network("192.0.2.0/24"),),
            ldap_server=None,
            ldap_base_dn=None,
            ldap_credential_target="IronDeploy-LDAP",
            ldap_use_ssl=False,
            ldap_connect_timeout=5,
            odj_domain="example.test",
            odj_machine_ou=(
                "OU=Workstations,OU=Clients,DC=example,DC=test"
            ),
            odj_blob_dir=self.root / "ODJ",
            odj_djoin_path=self.djoin_path,
            odj_provision_timeout=60,
            odj_blob_max_age_minutes=120,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_provision_builds_expected_command_and_is_idempotent(self) -> None:
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess:
            commands.append(command)
            Path(command[-1]).write_bytes(b"odj-blob")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("app.domain_join.subprocess.run", side_effect=fake_run):
            first_path = provision_domain_join_blob(
                self.settings,
                "pc00042",
            )
            second_path = provision_domain_join_blob(
                self.settings,
                "pc00042",
            )

        expected_path = self.settings.odj_blob_dir / "pc00042.txt"
        self.assertEqual(first_path, expected_path)
        self.assertEqual(second_path, expected_path)
        self.assertEqual(expected_path.read_bytes(), b"odj-blob")
        self.assertEqual(len(commands), 1)
        self.assertEqual(
            commands[0],
            [
                str(self.djoin_path),
                "/provision",
                "/domain",
                "example.test",
                "/machine",
                "pc00042",
                "/machineou",
                "OU=Workstations,OU=Clients,DC=example,DC=test",
                "/savefile",
                str(expected_path),
            ],
        )

    def test_failed_provision_removes_partial_blob(self) -> None:
        def fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess:
            Path(command[-1]).write_bytes(b"partial")
            return subprocess.CompletedProcess(command, 1, "", "failed")

        with patch("app.domain_join.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(
                DomainJoinError,
                "djoin.exe exited with code 1",
            ):
                provision_domain_join_blob(self.settings, "pc00042")

        self.assertFalse(
            (self.settings.odj_blob_dir / "pc00042.txt").exists()
        )

    def test_reuse_omits_machine_ou(self) -> None:
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess:
            commands.append(command)
            Path(command[-1]).write_bytes(b"odj-blob")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("app.domain_join.subprocess.run", side_effect=fake_run):
            provision_domain_join_blob(
                self.settings,
                "pc00042",
                reuse_existing_account=True,
            )

        self.assertEqual(len(commands), 1)
        self.assertIn("/reuse", commands[0])
        self.assertNotIn("/machineou", commands[0])
        self.assertEqual(commands[0][-2:], [
            "/savefile",
            str(self.settings.odj_blob_dir / "pc00042.txt"),
        ])

    def test_timed_out_provision_removes_partial_blob(self) -> None:
        def fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess:
            Path(command[-1]).write_bytes(b"partial")
            raise subprocess.TimeoutExpired(command, 60)

        with patch("app.domain_join.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(
                DomainJoinError,
                "Failed to run djoin.exe",
            ):
                provision_domain_join_blob(self.settings, "pc00042")

        self.assertFalse(
            (self.settings.odj_blob_dir / "pc00042.txt").exists()
        )

    def test_get_and_acknowledge_blob(self) -> None:
        self.settings.odj_blob_dir.mkdir(parents=True)
        blob_path = self.settings.odj_blob_dir / "pc00042.txt"
        blob_path.write_bytes(b"odj-blob")

        self.assertEqual(
            get_domain_join_blob(self.settings, "pc00042"),
            blob_path,
        )
        self.assertTrue(
            delete_domain_join_blob(self.settings, "pc00042")
        )
        self.assertFalse(blob_path.exists())
        self.assertFalse(
            delete_domain_join_blob(self.settings, "pc00042")
        )

    def test_missing_djoin_is_reported(self) -> None:
        self.djoin_path.unlink()

        with self.assertRaisesRegex(DomainJoinError, "djoin.exe not found"):
            provision_domain_join_blob(self.settings, "pc00042")

    def test_expired_blob_is_reprovisioned_instead_of_reused(self) -> None:
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess:
            commands.append(command)
            Path(command[-1]).write_bytes(b"fresh-blob")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.settings.odj_blob_dir.mkdir(parents=True)
        blob_path = self.settings.odj_blob_dir / "pc00042.txt"
        blob_path.write_bytes(b"expired-blob")
        age_file(blob_path, self.settings.odj_blob_max_age_minutes + 1)

        with patch("app.domain_join.subprocess.run", side_effect=fake_run):
            provision_domain_join_blob(self.settings, "pc00042")

        self.assertEqual(len(commands), 1)
        self.assertEqual(blob_path.read_bytes(), b"fresh-blob")

    def test_purge_removes_only_expired_blobs(self) -> None:
        self.settings.odj_blob_dir.mkdir(parents=True)
        fresh = self.settings.odj_blob_dir / "pc00001.txt"
        expired = self.settings.odj_blob_dir / "pc00002.txt"
        unrelated = self.settings.odj_blob_dir / "notes.md"
        for path in (fresh, expired, unrelated):
            path.write_bytes(b"odj-blob")
        age_file(fresh, self.settings.odj_blob_max_age_minutes - 1)
        age_file(expired, self.settings.odj_blob_max_age_minutes + 1)
        age_file(unrelated, self.settings.odj_blob_max_age_minutes + 1)

        self.assertEqual(purge_stale_domain_join_blobs(self.settings), 1)
        self.assertTrue(fresh.exists())
        self.assertFalse(expired.exists())
        self.assertTrue(unrelated.exists())

    def test_purge_tolerates_a_missing_blob_directory(self) -> None:
        self.assertFalse(self.settings.odj_blob_dir.exists())
        self.assertEqual(purge_stale_domain_join_blobs(self.settings), 0)

    def test_throttled_purge_runs_once_per_interval(self) -> None:
        self.settings.odj_blob_dir.mkdir(parents=True)

        def make_expired(name: str) -> Path:
            path = self.settings.odj_blob_dir / name
            path.write_bytes(b"odj-blob")
            age_file(path, self.settings.odj_blob_max_age_minutes + 1)
            return path

        first = make_expired("pc00001.txt")
        with patch("app.domain_join._last_purge_at", None):
            self.assertEqual(
                purge_stale_domain_join_blobs(self.settings, throttle=True),
                1,
            )
            second = make_expired("pc00002.txt")
            self.assertEqual(
                purge_stale_domain_join_blobs(self.settings, throttle=True),
                0,
            )

        self.assertFalse(first.exists())
        self.assertTrue(second.exists())


if __name__ == "__main__":
    unittest.main()
