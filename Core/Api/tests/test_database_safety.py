import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
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
    _create_sqlite_backup,
    initialize_database,
)
from app.sqlite_migration_0001 import SQLITE_MIGRATION_0001


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

    def create_legacy_database_with_child(self) -> None:
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
                    CREATE TABLE deployment_stages (
                        id INTEGER NOT NULL PRIMARY KEY,
                        deployment_id INTEGER NOT NULL,
                        stage VARCHAR(64) NOT NULL,
                        phase VARCHAR(16) NOT NULL,
                        status VARCHAR(16) NOT NULL,
                        started_at DATETIME NOT NULL,
                        completed_at DATETIME,
                        CONSTRAINT ck_deployment_stages_phase
                            CHECK (phase IN ('winpe')),
                        CONSTRAINT ck_deployment_stages_status
                            CHECK (status IN (
                                'running', 'completed', 'failed', 'skipped'
                            )),
                        CONSTRAINT ck_deployment_stages_completion CHECK (
                            (status = 'running' AND completed_at IS NULL) OR
                            (status IN ('completed', 'failed', 'skipped')
                             AND completed_at IS NOT NULL)
                        ),
                        CONSTRAINT uq_deployment_stages_deployment_stage
                            UNIQUE (deployment_id, stage),
                        FOREIGN KEY(deployment_id) REFERENCES deployments(id)
                            ON DELETE CASCADE
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
            connection.execute(
                text(
                    """
                    INSERT INTO deployment_stages (
                        id, deployment_id, stage, phase, status, started_at
                    ) VALUES (
                        11, 7, 'image_apply', 'winpe', 'running',
                        '2026-07-03 08:05:00'
                    )
                    """
                )
            )

    def test_new_database_is_created_at_latest_version(self) -> None:
        self.assertFalse(self.database_path.exists())

        initialize_database(self.engine)

        tables = set(inspect(self.engine).get_table_names())
        self.assertIn("deployments", tables)
        self.assertIn(MIGRATION_TABLE, tables)
        deployment_columns = {
            column["name"]
            for column in inspect(self.engine).get_columns("deployments")
        }
        self.assertIn("image_apply_mode", deployment_columns)
        with self.engine.connect() as connection:
            network_stage_sql = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'deployment_network_stages'"
                )
            ).scalar_one()
        self.assertIn("'image_download'", network_stage_sql)
        self.assertEqual(
            self.applied_versions(),
            [migration.version for migration in MIGRATIONS],
        )
        self.assertEqual(self.backups(), [])

    def test_version_two_database_adds_disk_columns_before_migration_three(
        self,
    ) -> None:
        with self.engine.begin() as connection:
            for statement in SQLITE_MIGRATION_0001:
                connection.exec_driver_sql(statement)
            connection.execute(
                text(
                    f"CREATE TABLE {MIGRATION_TABLE} ("
                    "version INTEGER NOT NULL PRIMARY KEY, "
                    "applied_at VARCHAR(32) NOT NULL)"
                )
            )
            connection.execute(
                text(
                    f"INSERT INTO {MIGRATION_TABLE} (version, applied_at) "
                    "VALUES (1, 'now'), (2, 'now')"
                )
            )

        initialize_database(self.engine)

        columns = {
            column["name"]
            for column in inspect(self.engine).get_columns("deployments")
        }
        self.assertIn("target_disk_number", columns)
        self.assertIn("target_disk_model", columns)
        self.assertIn("target_disk_size_bytes", columns)
        self.assertIn("image_apply_mode", columns)
        self.assertEqual(
            self.applied_versions(),
            [migration.version for migration in MIGRATIONS],
        )

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
        self.assertIn("target_disk_number", columns)
        self.assertIn("target_disk_model", columns)
        self.assertIn("target_disk_size_bytes", columns)
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

    def test_parallel_initialization_applies_each_migration_once(self) -> None:
        engines = [
            create_engine(
                f"sqlite:///{self.database_path.as_posix()}",
                connect_args={"check_same_thread": False, "timeout": 30},
            )
            for _ in range(2)
        ]
        self.addCleanup(lambda: [item.dispose() for item in engines])

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(initialize_database, engines))

        self.assertEqual(results, [None, None])
        self.assertEqual(
            self.applied_versions(),
            [migration.version for migration in MIGRATIONS],
        )
        with self.engine.connect() as connection:
            counts = connection.execute(
                text(
                    f"SELECT version, COUNT(*) FROM {MIGRATION_TABLE} "
                    "GROUP BY version ORDER BY version"
                )
            ).all()
        self.assertEqual(
            counts,
            [(migration.version, 1) for migration in MIGRATIONS],
        )

    def test_legacy_child_rows_constraints_indexes_and_on_delete_survive(
        self,
    ) -> None:
        self.create_legacy_database_with_child()

        initialize_database(self.engine)

        inspector = inspect(self.engine)
        deployment_indexes = {
            index["name"] for index in inspector.get_indexes("deployments")
        }
        stage_indexes = {
            index["name"]
            for index in inspector.get_indexes("deployment_stages")
        }
        with self.engine.connect() as connection:
            stage_foreign_keys = connection.exec_driver_sql(
                "PRAGMA foreign_key_list(deployment_stages)"
            ).fetchall()
            deployment_sql = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'deployments'"
                )
            ).scalar_one().lower()
            deployment = connection.execute(
                text(
                    "SELECT computer_name, image_name, serial_number "
                    "FROM deployments WHERE id = 7"
                )
            ).one()
            child = connection.execute(
                text(
                    "SELECT deployment_id, stage FROM deployment_stages "
                    "WHERE id = 11"
                )
            ).one()

        self.assertEqual(deployment, ("pc00007", "win11.wim", None))
        self.assertEqual(child, (7, "image_apply"))
        self.assertIn("ix_deployments_status", deployment_indexes)
        self.assertIn("ix_deployments_computer_name", deployment_indexes)
        self.assertIn("ix_deployment_stages_deployment_id", stage_indexes)
        self.assertIn("constraint ck_deployments_status", deployment_sql)
        self.assertIn("'failed'", deployment_sql)
        self.assertTrue(any(row[6] == "CASCADE" for row in stage_foreign_keys))

        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM deployments WHERE id = 7"))
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM deployment_stages "
                        "WHERE id = 11"
                    )
                ).scalar_one(),
                0,
            )

    def test_partial_deployments_rebuild_is_rolled_back(self) -> None:
        self.create_legacy_database_with_child()

        def fail_after_drop(connection) -> None:
            connection.exec_driver_sql(
                "CREATE TABLE deployments_timeout_upgrade "
                "AS SELECT * FROM deployments"
            )
            connection.exec_driver_sql("DROP TABLE deployments")
            raise RuntimeError("injected failure after deployments drop")

        with (
            patch(
                "app.database._upgrade_sqlite_deployment_constraints",
                side_effect=fail_after_drop,
            ),
            self.assertRaises(DatabaseMigrationError),
        ):
            initialize_database(self.engine)

        self.assertIn("deployments", inspect(self.engine).get_table_names())
        self.assertNotIn(
            "deployments_timeout_upgrade",
            inspect(self.engine).get_table_names(),
        )
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    text("SELECT computer_name FROM deployments WHERE id = 7")
                ).scalar_one(),
                "pc00007",
            )
            self.assertEqual(
                connection.execute(
                    text(
                        "SELECT deployment_id FROM deployment_stages "
                        "WHERE id = 11"
                    )
                ).scalar_one(),
                7,
            )

        self.engine.dispose()
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one(),
                1,
            )

    def test_invalid_applied_migration_histories_are_rejected(self) -> None:
        histories = {
            "gap": [1, 3],
            "duplicate": [1, 1],
            "future": [1, 2, 3, 4, 5, 6],
        }
        for label, versions in histories.items():
            with self.subTest(label=label):
                database_path = (
                    Path(self.temporary_directory.name) / f"{label}.db"
                )
                engine = create_engine(
                    f"sqlite:///{database_path.as_posix()}"
                )
                try:
                    with engine.begin() as connection:
                        connection.execute(
                            text(
                                f"CREATE TABLE {MIGRATION_TABLE} ("
                                "version INTEGER NOT NULL, "
                                "applied_at VARCHAR(32) NOT NULL)"
                            )
                        )
                        for version in versions:
                            connection.execute(
                                text(
                                    f"INSERT INTO {MIGRATION_TABLE} "
                                    "(version, applied_at) "
                                    "VALUES (:version, 'now')"
                                ),
                                {"version": version},
                            )
                    with self.assertRaises(DatabaseMigrationError):
                        initialize_database(engine)
                finally:
                    engine.dispose()

    def test_gap_and_duplicate_migration_definitions_are_rejected(self) -> None:
        invalid_definitions = (
            (
                Migration(1, "one", lambda connection: None),
                Migration(3, "three", lambda connection: None),
            ),
            (
                Migration(1, "one", lambda connection: None),
                Migration(1, "duplicate", lambda connection: None),
            ),
        )
        for migrations in invalid_definitions:
            with (
                self.subTest(versions=[item.version for item in migrations]),
                patch("app.database.MIGRATIONS", migrations),
                self.assertRaises(DatabaseMigrationError),
            ):
                initialize_database(self.engine)

    def test_backup_failure_stops_before_migration(self) -> None:
        self.create_legacy_database_with_child()
        with (
            patch(
                "app.database._create_sqlite_backup",
                side_effect=DatabaseMigrationError("injected backup failure"),
            ),
            self.assertRaises(DatabaseMigrationError),
        ):
            initialize_database(self.engine)

        self.assertNotIn(MIGRATION_TABLE, inspect(self.engine).get_table_names())
        columns = {
            column["name"]
            for column in inspect(self.engine).get_columns("deployments")
        }
        self.assertNotIn("serial_number", columns)

    def test_failed_source_quick_check_stops_before_backup(self) -> None:
        self.create_legacy_database_with_child()
        with (
            patch(
                "app.database._check_sqlite_file",
                side_effect=DatabaseMigrationError(
                    "injected source quick_check failure"
                ),
            ),
            patch("app.database._create_sqlite_backup") as create_backup,
            self.assertRaises(DatabaseMigrationError),
        ):
            initialize_database(self.engine)

        create_backup.assert_not_called()
        self.assertNotIn(MIGRATION_TABLE, inspect(self.engine).get_table_names())

    def test_invalid_backup_is_removed_and_migration_does_not_start(
        self,
    ) -> None:
        self.create_legacy_database_with_child()
        with (
            patch(
                "app.database._check_sqlite_file",
                side_effect=DatabaseMigrationError(
                    "injected backup integrity failure"
                ),
            ),
            self.assertRaises(DatabaseMigrationError),
        ):
            _create_sqlite_backup(self.database_path)

        self.assertEqual(self.backups(), [])
        self.assertNotIn(MIGRATION_TABLE, inspect(self.engine).get_table_names())

    def test_foreign_keys_are_on_after_success_on_new_connection(self) -> None:
        initialize_database(self.engine)
        self.engine.dispose()

        with self.engine.connect() as connection:
            self.assertEqual(
                connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one(),
                1,
            )

    def test_non_sqlite_engine_is_rejected(self) -> None:
        with (
            patch.object(self.engine.dialect, "name", "postgresql"),
            self.assertRaisesRegex(DatabaseMigrationError, "SQLite.*only"),
        ):
            initialize_database(self.engine)


if __name__ == "__main__":
    unittest.main()
