from collections.abc import Callable, Generator
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.deployments import Deployment, update_computer_inventory
from app.sqlite_migration_0001 import SQLITE_MIGRATION_0001


MIGRATION_TABLE = "schema_migrations"


class DatabaseMigrationError(RuntimeError):
    """Raised when database migration cannot complete safely."""

    def __init__(self, message: str, backup_path: Path | None = None) -> None:
        super().__init__(message)
        self.backup_path = backup_path


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    upgrade: Callable[[Connection], None]


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(
    dbapi_connection: object,
    _connection_record: object,
) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


database_url = make_url(get_settings().database_url)
if database_url.get_backend_name() != "sqlite":
    raise DatabaseMigrationError(
        "IronAPI currently supports SQLite databases only; "
        f"configured dialect: {database_url.get_backend_name()}."
    )
if database_url.database and database_url.database != ":memory:":
    Path(database_url.database).expanduser().resolve().parent.mkdir(
        parents=True,
        exist_ok=True,
    )
connect_args = {"check_same_thread": False, "timeout": 30}

engine = create_engine(
    database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)


def _deployment_constraints_support_failed(bind: Connection) -> bool:
    table_sql = (
        bind.exec_driver_sql(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = ?",
            (Deployment.__tablename__,),
        ).scalar_one_or_none()
        or ""
    )
    normalized = table_sql.lower()
    return (
        "ck_deployments_status" in normalized
        and "ck_deployments_completion" in normalized
        and normalized.count("'failed'") >= 2
    )


def _deployments_image_name_is_nullable(bind: Connection) -> bool:
    columns = inspect(bind).get_columns(Deployment.__tablename__)
    for column in columns:
        if column["name"] == "image_name":
            return bool(column.get("nullable", True))
    return True


def _upgrade_sqlite_deployment_constraints(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE deployments_timeout_upgrade (
            id INTEGER NOT NULL PRIMARY KEY,
            computer_name VARCHAR(63) NOT NULL,
            mac_address VARCHAR(17) NOT NULL,
            ip_address VARCHAR(45) NOT NULL,
            image_name VARCHAR(255),
            image_apply_mode VARCHAR(16),
            driver_apply_mode VARCHAR(16),
            target_disk_number INTEGER,
            target_disk_model VARCHAR(255),
            target_disk_size_bytes BIGINT,
            domain_join BOOLEAN NOT NULL,
            status VARCHAR(16) NOT NULL,
            started_at DATETIME NOT NULL,
            completed_at DATETIME,
            serial_number VARCHAR(128),
            model VARCHAR(128),
            manufacturer VARCHAR(128),
            system_sku VARCHAR(128),
            last_error_message TEXT,
            CONSTRAINT ck_deployments_status
                CHECK (status IN ('begin', 'completed', 'failed')),
            CONSTRAINT ck_deployments_completion CHECK (
                (status = 'begin' AND completed_at IS NULL) OR
                (status IN ('completed', 'failed') AND completed_at IS NOT NULL)
            )
        )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO deployments_timeout_upgrade (
            id,
            computer_name,
            mac_address,
            ip_address,
            image_name,
            image_apply_mode,
            driver_apply_mode,
            target_disk_number,
            target_disk_model,
            target_disk_size_bytes,
            domain_join,
            status,
            started_at,
            completed_at,
            serial_number,
            model,
            manufacturer,
            system_sku,
            last_error_message
        )
        SELECT
            id,
            computer_name,
            mac_address,
            ip_address,
            image_name,
            image_apply_mode,
            driver_apply_mode,
            target_disk_number,
            target_disk_model,
            target_disk_size_bytes,
            domain_join,
            status,
            started_at,
            completed_at,
            serial_number,
            model,
            manufacturer,
            system_sku,
            last_error_message
        FROM deployments
        """
    )
    connection.exec_driver_sql("DROP TABLE deployments")
    connection.exec_driver_sql(
        "ALTER TABLE deployments_timeout_upgrade RENAME TO deployments"
    )
    connection.exec_driver_sql(
        "CREATE INDEX ix_deployments_status ON deployments (status)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX ix_deployments_computer_name "
        "ON deployments (computer_name)"
    )


def _upgrade_deployment_constraints(connection: Connection) -> None:
    if (
        _deployment_constraints_support_failed(connection)
        and _deployments_image_name_is_nullable(connection)
    ):
        return

    _upgrade_sqlite_deployment_constraints(connection)


def _backfill_computer_inventory(connection: Connection) -> None:
    with Session(connection) as session:
        deployments = session.scalars(
            text(
                "SELECT id FROM deployments "
                "ORDER BY started_at ASC, id ASC"
            )
        ).all()
        for deployment_id in deployments:
            deployment = session.get(Deployment, deployment_id)
            if deployment is None:
                continue
            update_computer_inventory(
                session,
                deployment,
                observed_at=deployment.started_at,
            )
        session.commit()


def _migration_create_schema(connection: Connection) -> None:
    for statement in SQLITE_MIGRATION_0001:
        connection.exec_driver_sql(statement)


def _migration_add_legacy_columns(connection: Connection) -> None:
    additions = {
        "deployments": (
            ("serial_number", "VARCHAR(128)"),
            ("last_error_message", "TEXT"),
            ("model", "VARCHAR(128)"),
            ("manufacturer", "VARCHAR(128)"),
            ("system_sku", "VARCHAR(128)"),
            # Migration 3 backfills inventory through the current Deployment
            # ORM model. Add future nullable deployment columns before that
            # backfill when upgrading a database with no migration history.
            ("target_disk_number", "INTEGER"),
            ("target_disk_model", "VARCHAR(255)"),
            ("target_disk_size_bytes", "BIGINT"),
        ),
        "computers": (("last_model", "VARCHAR(128)"),),
        "deployment_stages": (("error_message", "TEXT"),),
        "deployment_programs": (("reason", "VARCHAR(32)"),),
    }

    for table_name, columns_to_add in additions.items():
        existing_columns = {
            column["name"]
            for column in inspect(connection).get_columns(table_name)
        }
        for column_name, column_type in columns_to_add:
            if column_name in existing_columns:
                continue
            connection.execute(
                text(
                    f"ALTER TABLE {table_name} "
                    f"ADD COLUMN {column_name} {column_type}"
                )
            )
            existing_columns.add(column_name)


def _migration_upgrade_constraints_and_inventory(
    connection: Connection,
) -> None:
    # A database can legitimately have migrations 1-2 from an older release
    # while migration 3 is still pending. Ensure the current ORM columns exist
    # before its table rebuild and ORM-backed inventory backfill.
    _migration_add_target_disk_snapshot(connection)
    _upgrade_deployment_constraints(connection)
    _backfill_computer_inventory(connection)


def _migration_add_target_disk_snapshot(connection: Connection) -> None:
    existing_columns = {
        column["name"]
        for column in inspect(connection).get_columns(Deployment.__tablename__)
    }
    additions = (
        ("target_disk_number", "INTEGER"),
        ("target_disk_model", "VARCHAR(255)"),
        ("target_disk_size_bytes", "BIGINT"),
        # The migration-three inventory backfill uses the current Deployment
        # ORM model, so this nullable column must exist before that backfill.
        ("image_apply_mode", "VARCHAR(16)"),
        ("driver_apply_mode", "VARCHAR(16)"),
    )
    for column_name, column_type in additions:
        if column_name in existing_columns:
            continue
        connection.execute(
            text(
                f"ALTER TABLE {Deployment.__tablename__} "
                f"ADD COLUMN {column_name} {column_type}"
            )
        )
        existing_columns.add(column_name)


def _migration_add_image_apply_strategy(connection: Connection) -> None:
    deployment_columns = {
        column["name"]
        for column in inspect(connection).get_columns(Deployment.__tablename__)
    }
    if "image_apply_mode" not in deployment_columns:
        connection.execute(
            text(
                "ALTER TABLE deployments "
                "ADD COLUMN image_apply_mode VARCHAR(16)"
            )
        )

    table_sql = (
        connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = ?",
            ("deployment_network_stages",),
        ).scalar_one_or_none()
        or ""
    )
    if "'image_download'" in table_sql.lower():
        return

    connection.exec_driver_sql(
        """
        CREATE TABLE deployment_network_stages_image_strategy_upgrade (
            id INTEGER NOT NULL PRIMARY KEY,
            deployment_id INTEGER NOT NULL,
            stage VARCHAR(64) NOT NULL,
            started_at DATETIME NOT NULL,
            completed_at DATETIME NOT NULL,
            duration_seconds FLOAT NOT NULL,
            icmp_status VARCHAR(16) NOT NULL,
            ping_sent INTEGER NOT NULL,
            ping_received INTEGER NOT NULL,
            ping_lost INTEGER NOT NULL,
            loss_percentage FLOAT,
            rtt_min_ms FLOAT,
            rtt_avg_ms FLOAT,
            rtt_max_ms FLOAT,
            latency_spikes INTEGER NOT NULL,
            bytes_received BIGINT,
            average_inbound_mbps FLOAT,
            link_utilization_percent FLOAT,
            CONSTRAINT ck_deployment_network_stages_stage CHECK (
                stage IN (
                    'image_download', 'image_apply',
                    'driver_injection', 'postinstall_copy'
                )
            ),
            CONSTRAINT ck_deployment_network_stages_icmp_status CHECK (
                icmp_status IN ('available', 'unavailable', 'not_measured')
            ),
            CONSTRAINT uq_deployment_network_stages_deployment_stage
                UNIQUE (deployment_id, stage),
            FOREIGN KEY(deployment_id) REFERENCES deployments (id)
                ON DELETE CASCADE
        )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO deployment_network_stages_image_strategy_upgrade (
            id, deployment_id, stage, started_at, completed_at,
            duration_seconds, icmp_status, ping_sent, ping_received,
            ping_lost, loss_percentage, rtt_min_ms, rtt_avg_ms,
            rtt_max_ms, latency_spikes, bytes_received,
            average_inbound_mbps, link_utilization_percent
        )
        SELECT
            id, deployment_id, stage, started_at, completed_at,
            duration_seconds, icmp_status, ping_sent, ping_received,
            ping_lost, loss_percentage, rtt_min_ms, rtt_avg_ms,
            rtt_max_ms, latency_spikes, bytes_received,
            average_inbound_mbps, link_utilization_percent
        FROM deployment_network_stages
        """
    )
    connection.exec_driver_sql("DROP TABLE deployment_network_stages")
    connection.exec_driver_sql(
        "ALTER TABLE deployment_network_stages_image_strategy_upgrade "
        "RENAME TO deployment_network_stages"
    )
    connection.exec_driver_sql(
        "CREATE INDEX ix_deployment_network_stages_deployment_id "
        "ON deployment_network_stages (deployment_id)"
    )


def _migration_allow_early_network_adapter_snapshot(
    connection: Connection,
) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE deployment_network_summaries_adapter_upgrade (
            deployment_id INTEGER NOT NULL PRIMARY KEY,
            started_at DATETIME,
            completed_at DATETIME,
            ping_target VARCHAR(255),
            smb_adapter_name VARCHAR(255),
            smb_adapter_description VARCHAR(512),
            smb_adapter_id VARCHAR(255),
            smb_local_ip VARCHAR(45),
            smb_link_speed_bps BIGINT,
            api_adapter_name VARCHAR(255),
            api_adapter_description VARCHAR(512),
            api_adapter_id VARCHAR(255),
            api_local_ip VARCHAR(45),
            api_link_speed_bps BIGINT,
            adapters_differ BOOLEAN NOT NULL,
            duration_seconds FLOAT,
            icmp_status VARCHAR(16),
            ping_sent INTEGER,
            ping_received INTEGER,
            ping_lost INTEGER,
            loss_percentage FLOAT,
            rtt_min_ms FLOAT,
            rtt_avg_ms FLOAT,
            rtt_max_ms FLOAT,
            latency_spikes INTEGER,
            bytes_received BIGINT,
            average_inbound_mbps FLOAT,
            link_utilization_percent FLOAT,
            api_request_count INTEGER,
            api_error_count INTEGER,
            api_min_ms FLOAT,
            api_avg_ms FLOAT,
            api_max_ms FLOAT,
            smb_connect_success BOOLEAN,
            smb_connect_attempts INTEGER,
            smb_connect_duration_ms FLOAT,
            smb_error_message TEXT,
            diagnostic_errors JSON,
            CONSTRAINT ck_deployment_network_summaries_icmp_status
                CHECK (
                    icmp_status IS NULL OR
                    icmp_status IN ('available', 'unavailable', 'not_measured')
                ),
            FOREIGN KEY(deployment_id) REFERENCES deployments (id)
                ON DELETE CASCADE
        )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO deployment_network_summaries_adapter_upgrade (
            deployment_id, started_at, completed_at, ping_target,
            smb_adapter_name, smb_adapter_description, smb_adapter_id,
            smb_local_ip, smb_link_speed_bps, api_adapter_name,
            api_adapter_description, api_adapter_id, api_local_ip,
            api_link_speed_bps, adapters_differ, duration_seconds,
            icmp_status, ping_sent, ping_received, ping_lost,
            loss_percentage, rtt_min_ms, rtt_avg_ms, rtt_max_ms,
            latency_spikes, bytes_received, average_inbound_mbps,
            link_utilization_percent, api_request_count, api_error_count,
            api_min_ms, api_avg_ms, api_max_ms, smb_connect_success,
            smb_connect_attempts, smb_connect_duration_ms,
            smb_error_message, diagnostic_errors
        )
        SELECT
            deployment_id, started_at, completed_at, ping_target,
            smb_adapter_name, smb_adapter_description, smb_adapter_id,
            smb_local_ip, smb_link_speed_bps, api_adapter_name,
            api_adapter_description, api_adapter_id, api_local_ip,
            api_link_speed_bps, adapters_differ, duration_seconds,
            icmp_status, ping_sent, ping_received, ping_lost,
            loss_percentage, rtt_min_ms, rtt_avg_ms, rtt_max_ms,
            latency_spikes, bytes_received, average_inbound_mbps,
            link_utilization_percent, api_request_count, api_error_count,
            api_min_ms, api_avg_ms, api_max_ms, smb_connect_success,
            smb_connect_attempts, smb_connect_duration_ms,
            smb_error_message, diagnostic_errors
        FROM deployment_network_summaries
        """
    )
    connection.exec_driver_sql("DROP TABLE deployment_network_summaries")
    connection.exec_driver_sql(
        "ALTER TABLE deployment_network_summaries_adapter_upgrade "
        "RENAME TO deployment_network_summaries"
    )


def _migration_add_default_deployment_profile(connection: Connection) -> None:
    if "deployment_profiles" not in inspect(connection).get_table_names():
        connection.exec_driver_sql(
            """
            CREATE TABLE deployment_profiles (
                id INTEGER NOT NULL PRIMARY KEY,
                name VARCHAR(64) NOT NULL UNIQUE,
                description TEXT,
                is_default BOOLEAN NOT NULL,
                local_admin_name VARCHAR(20) NOT NULL,
                enable_builtin_administrator BOOLEAN NOT NULL,
                enable_setup_local_admin BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT ck_deployment_profiles_positive_id CHECK (id > 0)
            )
            """
        )
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_deployment_profiles_default "
        "ON deployment_profiles (is_default) WHERE is_default = 1"
    )
    existing_default = connection.execute(
        text("SELECT id FROM deployment_profiles WHERE is_default = 1 LIMIT 1")
    ).first()
    if existing_default is None:
        connection.exec_driver_sql(
            """
            INSERT INTO deployment_profiles (
                id,
                name,
                description,
                is_default,
                local_admin_name,
                enable_builtin_administrator,
                enable_setup_local_admin,
                created_at,
                updated_at
            ) SELECT
                COALESCE(MAX(id), 0) + 1,
                'Default',
                'Default deployment settings',
                1,
                'localadmin',
                1,
                1,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            FROM deployment_profiles
            """
        )


def _migration_add_driver_apply_strategy(connection: Connection) -> None:
    deployment_columns = {
        column["name"]
        for column in inspect(connection).get_columns(Deployment.__tablename__)
    }
    if "driver_apply_mode" not in deployment_columns:
        connection.execute(
            text(
                "ALTER TABLE deployments "
                "ADD COLUMN driver_apply_mode VARCHAR(16)"
            )
        )

    table_sql = (
        connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = ?",
            ("deployment_network_stages",),
        ).scalar_one_or_none()
        or ""
    )
    if "'driver_download'" in table_sql.lower():
        return

    connection.exec_driver_sql(
        """
        CREATE TABLE deployment_network_stages_driver_strategy_upgrade (
            id INTEGER NOT NULL PRIMARY KEY,
            deployment_id INTEGER NOT NULL,
            stage VARCHAR(64) NOT NULL,
            started_at DATETIME NOT NULL,
            completed_at DATETIME NOT NULL,
            duration_seconds FLOAT NOT NULL,
            icmp_status VARCHAR(16) NOT NULL,
            ping_sent INTEGER NOT NULL,
            ping_received INTEGER NOT NULL,
            ping_lost INTEGER NOT NULL,
            loss_percentage FLOAT,
            rtt_min_ms FLOAT,
            rtt_avg_ms FLOAT,
            rtt_max_ms FLOAT,
            latency_spikes INTEGER NOT NULL,
            bytes_received BIGINT,
            average_inbound_mbps FLOAT,
            link_utilization_percent FLOAT,
            CONSTRAINT ck_deployment_network_stages_stage CHECK (
                stage IN (
                    'image_download', 'image_apply', 'driver_download',
                    'driver_injection', 'postinstall_copy'
                )
            ),
            CONSTRAINT ck_deployment_network_stages_icmp_status CHECK (
                icmp_status IN ('available', 'unavailable', 'not_measured')
            ),
            CONSTRAINT uq_deployment_network_stages_deployment_stage
                UNIQUE (deployment_id, stage),
            FOREIGN KEY(deployment_id) REFERENCES deployments (id)
                ON DELETE CASCADE
        )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO deployment_network_stages_driver_strategy_upgrade (
            id, deployment_id, stage, started_at, completed_at,
            duration_seconds, icmp_status, ping_sent, ping_received,
            ping_lost, loss_percentage, rtt_min_ms, rtt_avg_ms,
            rtt_max_ms, latency_spikes, bytes_received,
            average_inbound_mbps, link_utilization_percent
        )
        SELECT
            id, deployment_id, stage, started_at, completed_at,
            duration_seconds, icmp_status, ping_sent, ping_received,
            ping_lost, loss_percentage, rtt_min_ms, rtt_avg_ms,
            rtt_max_ms, latency_spikes, bytes_received,
            average_inbound_mbps, link_utilization_percent
        FROM deployment_network_stages
        """
    )
    connection.exec_driver_sql("DROP TABLE deployment_network_stages")
    connection.exec_driver_sql(
        "ALTER TABLE deployment_network_stages_driver_strategy_upgrade "
        "RENAME TO deployment_network_stages"
    )
    connection.exec_driver_sql(
        "CREATE INDEX ix_deployment_network_stages_deployment_id "
        "ON deployment_network_stages (deployment_id)"
    )


def _migration_add_post_powershell(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS post_powershell_scripts (
            id INTEGER NOT NULL PRIMARY KEY,
            name VARCHAR(255) NOT NULL UNIQUE,
            size_bytes BIGINT NOT NULL,
            modified_ns BIGINT NOT NULL,
            sha256 VARCHAR(64) NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS deployment_profile_scripts (
            profile_id INTEGER NOT NULL,
            script_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            selection_mode VARCHAR(16) NOT NULL,
            run_phase VARCHAR(24) NOT NULL,
            arguments VARCHAR(500) NOT NULL,
            timeout_seconds INTEGER NOT NULL,
            PRIMARY KEY (profile_id, script_id),
            CONSTRAINT uq_deployment_profile_scripts_position
                UNIQUE (profile_id, position),
            CONSTRAINT ck_deployment_profile_scripts_selection_mode
                CHECK (selection_mode IN ('automatic', 'operator')),
            CONSTRAINT ck_deployment_profile_scripts_run_phase
                CHECK (run_phase IN ('before_software', 'after_software')),
            CONSTRAINT ck_deployment_profile_scripts_timeout
                CHECK (timeout_seconds BETWEEN 1 AND 86400),
            FOREIGN KEY(profile_id) REFERENCES deployment_profiles (id)
                ON DELETE CASCADE,
            FOREIGN KEY(script_id) REFERENCES post_powershell_scripts (id)
                ON DELETE CASCADE
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS deployment_powershell_results (
            id INTEGER NOT NULL PRIMARY KEY,
            deployment_id BIGINT NOT NULL,
            script_id INTEGER,
            position INTEGER NOT NULL,
            name VARCHAR(255) NOT NULL,
            selection_mode VARCHAR(16) NOT NULL,
            run_phase VARCHAR(24) NOT NULL,
            arguments VARCHAR(500) NOT NULL,
            timeout_seconds INTEGER NOT NULL,
            size_bytes BIGINT NOT NULL,
            sha256 VARCHAR(64) NOT NULL,
            status VARCHAR(24) NOT NULL,
            exit_code INTEGER,
            duration_seconds INTEGER,
            output_bytes BIGINT NOT NULL,
            output_total_bytes BIGINT NOT NULL,
            output_truncated BOOLEAN NOT NULL,
            error_message TEXT,
            reported_at DATETIME,
            CONSTRAINT uq_deployment_powershell_results_position
                UNIQUE (deployment_id, position),
            CONSTRAINT ck_deployment_powershell_results_selection_mode
                CHECK (selection_mode IN ('automatic', 'operator')),
            CONSTRAINT ck_deployment_powershell_results_run_phase
                CHECK (run_phase IN ('before_software', 'after_software')),
            CONSTRAINT ck_deployment_powershell_results_status CHECK (
                status IN (
                    'pending', 'succeeded', 'failed', 'timed_out',
                    'hash_mismatch', 'download_failed'
                )
            ),
            FOREIGN KEY(deployment_id) REFERENCES deployments (id)
                ON DELETE CASCADE,
            FOREIGN KEY(script_id) REFERENCES post_powershell_scripts (id)
                ON DELETE SET NULL
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS "
        "ix_deployment_powershell_results_deployment_id "
        "ON deployment_powershell_results (deployment_id)"
    )


MIGRATIONS = (
    Migration(1, "create current schema", _migration_create_schema),
    Migration(2, "add legacy columns", _migration_add_legacy_columns),
    Migration(
        3,
        "upgrade deployment constraints and inventory",
        _migration_upgrade_constraints_and_inventory,
    ),
    Migration(4, "add target disk snapshot", _migration_add_target_disk_snapshot),
    Migration(
        5,
        "add image apply strategy",
        _migration_add_image_apply_strategy,
    ),
    Migration(
        6,
        "allow early network adapter snapshot",
        _migration_allow_early_network_adapter_snapshot,
    ),
    Migration(
        7,
        "add default deployment profile",
        _migration_add_default_deployment_profile,
    ),
    Migration(8, "add post-powershell scripts", _migration_add_post_powershell),
    Migration(
        9,
        "add driver apply strategy",
        _migration_add_driver_apply_strategy,
    ),
)


def _validate_migration_definitions() -> None:
    versions = [migration.version for migration in MIGRATIONS]
    expected = list(range(1, len(MIGRATIONS) + 1))
    if versions != expected:
        raise DatabaseMigrationError(
            "Database migrations must use unique consecutive versions "
            f"starting at 1; found {versions}."
        )


def _read_applied_versions(connection: Connection) -> list[int]:
    if not inspect(connection).has_table(MIGRATION_TABLE):
        return []
    return list(
        connection.scalars(
            text(
                f"SELECT version FROM {MIGRATION_TABLE} "
                "ORDER BY version"
            )
        )
    )


def _validate_applied_versions(applied_versions: list[int]) -> None:
    if not applied_versions:
        return
    expected = list(range(1, applied_versions[-1] + 1))
    latest = MIGRATIONS[-1].version
    if applied_versions != expected or applied_versions[-1] > latest:
        raise DatabaseMigrationError(
            "Database migration history is invalid or newer than this "
            f"IronAPI build: {applied_versions}."
        )


def _sqlite_database_path(target_engine: Engine) -> Path | None:
    database = target_engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database).expanduser().resolve()


def _validate_sqlite_engine(target_engine: Engine) -> None:
    if target_engine.dialect.name != "sqlite":
        raise DatabaseMigrationError(
            "IronAPI currently supports SQLite databases only; "
            f"configured dialect: {target_engine.dialect.name}."
        )


def _check_sqlite_file(database_path: Path, pragma: str) -> None:
    try:
        with closing(
            sqlite3.connect(
                f"{database_path.as_uri()}?mode=ro",
                uri=True,
                timeout=30,
            )
        ) as connection:
            results = [
                row[0]
                for row in connection.execute(f"PRAGMA {pragma}").fetchall()
            ]
    except Exception as exc:
        raise DatabaseMigrationError(
            f"Could not verify SQLite database {database_path} "
            f"with PRAGMA {pragma}: {exc}"
        ) from exc
    if results != ["ok"]:
        raise DatabaseMigrationError(
            f"SQLite PRAGMA {pragma} failed for {database_path}: {results}. "
            "No migrations were run."
        )


def _create_sqlite_backup(database_path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = database_path.with_name(
        f"{database_path.name}.{timestamp}.bak"
    )
    try:
        with (
            closing(sqlite3.connect(str(database_path))) as source,
            closing(sqlite3.connect(str(backup_path))) as destination,
        ):
            source.backup(destination)
            destination.commit()
        _check_sqlite_file(backup_path, "integrity_check")
    except Exception as exc:
        try:
            backup_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise DatabaseMigrationError(
            "Failed to create the SQLite migration backup. "
            f"Database: {database_path}. No migrations were run. "
            f"Error: {exc}"
        ) from exc
    return backup_path


def _migration_error(
    migration: Migration | None,
    backup_path: Path | None,
    error: Exception,
) -> DatabaseMigrationError:
    if migration is None:
        label = "database migration transaction"
    else:
        label = f"database migration {migration.version} ({migration.name})"
    backup = str(backup_path) if backup_path is not None else "not available"
    return DatabaseMigrationError(
        f"Failed {label}; startup stopped and the transaction was rolled back. "
        f"Backup: {backup}. Error: {error}",
        backup_path=backup_path,
    )


def _run_pending_migrations(
    target_engine: Engine,
    backup_path: Path | None,
) -> None:
    connection = target_engine.connect()
    active_migration: Migration | None = None

    def apply_pending_migrations(
        pending_migrations: tuple[Migration, ...],
    ) -> None:
        nonlocal active_migration
        for active_migration in pending_migrations:
            active_migration.upgrade(connection)
            connection.execute(
                text(
                    f"INSERT INTO {MIGRATION_TABLE} "
                    "(version, applied_at) "
                    "VALUES (:version, :applied_at)"
                ),
                {
                    "version": active_migration.version,
                    "applied_at": datetime.now(timezone.utc).isoformat(),
                },
            )

        violations = connection.exec_driver_sql(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if violations:
            raise RuntimeError(
                "SQLite foreign key check failed after migrations: "
                f"{violations}"
            )

    try:
        # This must happen outside the transaction; SQLite ignores changes to
        # foreign_keys while a transaction is active.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        connection.exec_driver_sql("PRAGMA busy_timeout=30000")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            connection.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} ("
                    "version INTEGER PRIMARY KEY, "
                    "applied_at VARCHAR(32) NOT NULL)"
                )
            )
            # The history must be read only after BEGIN IMMEDIATE acquired the
            # writer lock. Another startup may have migrated while we waited.
            applied_versions = _read_applied_versions(connection)
            _validate_applied_versions(applied_versions)
            applied = set(applied_versions)
            pending_migrations = tuple(
                migration
                for migration in MIGRATIONS
                if migration.version not in applied
            )
            apply_pending_migrations(pending_migrations)
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
    except Exception as exc:
        raise _migration_error(active_migration, backup_path, exc) from exc
    finally:
        if connection.in_transaction():
            connection.rollback()
        try:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
        finally:
            connection.close()


def initialize_database(target_engine: Engine = engine) -> None:
    _validate_sqlite_engine(target_engine)
    _validate_migration_definitions()
    sqlite_path = _sqlite_database_path(target_engine)
    sqlite_database_existed = (
        sqlite_path is not None and sqlite_path.is_file()
    )

    with target_engine.connect() as connection:
        applied_versions = _read_applied_versions(connection)
    _validate_applied_versions(applied_versions)
    applied = set(applied_versions)
    pending_migrations = tuple(
        migration
        for migration in MIGRATIONS
        if migration.version not in applied
    )
    if not pending_migrations:
        return

    backup_path = None
    if sqlite_database_existed and sqlite_path is not None:
        _check_sqlite_file(sqlite_path, "quick_check")
        backup_path = _create_sqlite_backup(sqlite_path)

    _run_pending_migrations(
        target_engine,
        backup_path,
    )


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
