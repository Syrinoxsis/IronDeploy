import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("IRONAPI_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("IRONAPI_NAME_PREFIX", "pc")
os.environ.setdefault("IRONAPI_NAME_WIDTH", "5")
os.environ.setdefault("IRONAPI_NAME_START", "1")
os.environ.setdefault("IRONAPI_ALLOWED_CLIENT_NETWORKS", "192.0.2.0/24")
os.environ.setdefault("IRONAPI_LDAP_SERVER", "dc01.example.test")
os.environ.setdefault("IRONAPI_LDAP_BASE_DN", "DC=example,DC=test")
os.environ.setdefault("IRONAPI_LDAP_USE_SSL", "false")
os.environ.setdefault("IRONAPI_LDAP_CONNECT_TIMEOUT", "5")
os.environ.setdefault("IRONAPI_ODJ_DOMAIN", "example.test")
os.environ.setdefault(
    "IRONAPI_ODJ_MACHINE_OU",
    "OU=Workstations,OU=Clients,DC=example,DC=test",
)
os.environ.setdefault("IRONAPI_ODJ_BLOB_DIR", "{IRONDEPLOY_ROOT}\\ODJ\\pending")
os.environ.setdefault("IRONAPI_ODJ_DJOIN_PATH", "C:\\Windows\\System32\\djoin.exe")
os.environ.setdefault("IRONAPI_ODJ_PROVISION_TIMEOUT", "60")

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.database import (
    MIGRATIONS,
    MIGRATION_TABLE,
    DatabaseMigrationError,
    Migration,
    initialize_database,
)


class DatabaseSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = (
            Path(self.temporary_directory.name) / "irondeploy.db"
        )
        self.engine = create_engine(
            f"sqlite:///{self.database_path.as_posix()}"
        )
        self.addCleanup(self.engine.dispose)

    def applied_versions(self) -> list[int]:
        with self.engine.connect() as connection:
            return list(
                connection.scalars(
                    text(
                        f"SELECT version FROM {MIGRATION_TABLE} "
                        "ORDER BY version"
                    )
                )
            )

    def backups(self) -> list[Path]:
        return sorted(
            self.database_path.parent.glob(
                f"{self.database_path.name}.*.bak"
            )
        )

    def test_new_database_is_created_at_latest_version(self) -> None:
        self.assertFalse(self.database_path.exists())

        initialize_database(self.engine)

        tables = set(inspect(self.engine).get_table_names())
        self.assertIn("deployments", tables)
        self.assertIn(MIGRATION_TABLE, tables)
        self.assertEqual(
            self.applied_versions(),
            [migration.version for migration in MIGRATIONS],
        )
        self.assertEqual(self.backups(), [])

    def test_legacy_database_is_backed_up_and_upgraded(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE deployments (
                        id INTEGER NOT NULL PRIMARY KEY,
                        computer_name VARCHAR(63) NOT NULL,
                        mac_address VARCHAR(17) NOT NULL,
                        ip_address VARCHAR(45) NOT NULL,
                        image_name VARCHAR(255) NOT NULL,
                        domain_join BOOLEAN NOT NULL,
                        status VARCHAR(16) NOT NULL,
                        started_at DATETIME NOT NULL,
                        completed_at DATETIME,
                        CONSTRAINT ck_deployments_status
                            CHECK (status IN ('begin', 'completed')),
                        CONSTRAINT ck_deployments_completion CHECK (
                            (status = 'begin' AND completed_at IS NULL) OR
                            (status = 'completed' AND completed_at IS NOT NULL)
                        )
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO deployments (
                        id, computer_name, mac_address, ip_address,
                        image_name, domain_join, status, started_at
                    ) VALUES (
                        7, 'pc00007', 'AA:BB:CC:DD:EE:FF', '192.0.2.7',
                        'win11.wim', 0, 'begin', '2026-07-03 08:00:00'
                    )
                    """
                )
            )

        initialize_database(self.engine)

        backups = self.backups()
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            legacy_columns = {
                row[1]
                for row in backup.execute(
                    "PRAGMA table_info(deployments)"
                ).fetchall()
            }
            backup_tables = {
                row[0]
                for row in backup.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertNotIn("serial_number", legacy_columns)
        self.assertNotIn(MIGRATION_TABLE, backup_tables)

        columns = {
            column["name"]
            for column in inspect(self.engine).get_columns("deployments")
        }
        self.assertIn("serial_number", columns)
        self.assertIn("last_error_message", columns)
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT computer_name, serial_number "
                    "FROM deployments WHERE id = 7"
                )
            ).one()
        self.assertEqual(row, ("pc00007", None))

    def test_repeated_migration_run_is_a_no_op(self) -> None:
        initialize_database(self.engine)
        versions = self.applied_versions()

        initialize_database(self.engine)

        self.assertEqual(self.applied_versions(), versions)
        self.assertEqual(self.backups(), [])

    def test_failed_migration_rolls_back_and_preserves_backup(self) -> None:
        initialize_database(self.engine)

        def fail_after_schema_change(connection) -> None:
            connection.execute(
                text("CREATE TABLE migration_should_rollback (id INTEGER)")
            )
            raise RuntimeError("injected migration failure")

        failing_migration = Migration(
            len(MIGRATIONS) + 1,
            "injected failure",
            fail_after_schema_change,
        )
        with (
            patch(
                "app.database.MIGRATIONS",
                MIGRATIONS + (failing_migration,),
            ),
            self.assertRaises(DatabaseMigrationError) as raised,
        ):
            initialize_database(self.engine)

        backup_path = raised.exception.backup_path
        self.assertIsNotNone(backup_path)
        self.assertTrue(backup_path.is_file())
        self.assertIn(str(backup_path), str(raised.exception))
        self.assertIn("transaction was rolled back", str(raised.exception))
        self.assertNotIn(
            "migration_should_rollback",
            inspect(self.engine).get_table_names(),
        )
        self.assertEqual(
            self.applied_versions(),
            [migration.version for migration in MIGRATIONS],
        )
        with closing(sqlite3.connect(backup_path)) as backup:
            self.assertEqual(
                backup.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )

    def test_foreign_key_rejects_child_without_parent(self) -> None:
        initialize_database(self.engine)

        with self.engine.connect() as connection:
            self.assertEqual(
                connection.exec_driver_sql(
                    "PRAGMA foreign_keys"
                ).scalar_one(),
                1,
            )
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO deployment_stages (
                            deployment_id, stage, phase, status, started_at
                        ) VALUES (
                            999, 'image_apply', 'winpe', 'running',
                            '2026-07-03 08:05:00'
                        )
                        """
                    )
                )

    def test_delete_parent_cascades_to_child(self) -> None:
        initialize_database(self.engine)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO deployments (
                        id, computer_name, mac_address, ip_address,
                        image_name, domain_join, status, started_at
                    ) VALUES (
                        7, 'pc00007', 'AA:BB:CC:DD:EE:FF', '192.0.2.7',
                        'win11.wim', 0, 'begin', '2026-07-03 08:00:00'
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO deployment_stages (
                        deployment_id, stage, phase, status, started_at
                    ) VALUES (
                        7, 'image_apply', 'winpe', 'running',
                        '2026-07-03 08:05:00'
                    )
                    """
                )
            )
            connection.execute(
                text("DELETE FROM deployments WHERE id = 7")
            )

        with self.engine.connect() as connection:
            child_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM deployment_stages "
                    "WHERE deployment_id = 7"
                )
            ).scalar_one()
        self.assertEqual(child_count, 0)


if __name__ == "__main__":
    unittest.main()
