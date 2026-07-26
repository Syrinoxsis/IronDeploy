from collections.abc import Generator
from pathlib import Path

from sqlalchemy import String, Text, create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.auth import AuthPermission, AuthSession, AuthUser
from app.deployments import Base, Deployment, update_computer_inventory
from app.winpe_auth import WinPEAuthPolicy, WinPEPinAttempt


database_url = make_url(get_settings().database_url)

if database_url.get_backend_name() == "sqlite":
    if database_url.database and database_url.database != ":memory:":
        Path(database_url.database).expanduser().resolve().parent.mkdir(
            parents=True,
            exist_ok=True,
        )
    connect_args = {"check_same_thread": False}
else:
    connect_args = {}

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


def _deployment_constraints_support_failed(target_engine: Engine) -> bool:
    constraints = {
        constraint["name"]: constraint.get("sqltext") or ""
        for constraint in inspect(target_engine).get_check_constraints(
            Deployment.__tablename__
        )
    }
    return all(
        "'failed'" in constraints.get(name, "").lower()
        for name in (
            "ck_deployments_status",
            "ck_deployments_completion",
        )
    )


def _deployments_image_name_is_nullable(target_engine: Engine) -> bool:
    columns = inspect(target_engine).get_columns(Deployment.__tablename__)
    for column in columns:
        if column["name"] == "image_name":
            return bool(column.get("nullable", True))
    return True


def _upgrade_sqlite_deployment_constraints(target_engine: Engine) -> None:
    connection = target_engine.raw_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN")
        cursor.execute(
            """
            CREATE TABLE deployments_timeout_upgrade (
                id INTEGER NOT NULL PRIMARY KEY,
                computer_name VARCHAR(63) NOT NULL,
                mac_address VARCHAR(17) NOT NULL,
                ip_address VARCHAR(45) NOT NULL,
                image_name VARCHAR(255),
                domain_join BOOLEAN NOT NULL,
                status VARCHAR(16) NOT NULL,
                started_at DATETIME NOT NULL,
                completed_at DATETIME,
                serial_number VARCHAR(128),
                model VARCHAR(128),
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
        cursor.execute(
            """
            INSERT INTO deployments_timeout_upgrade (
                id,
                computer_name,
                mac_address,
                ip_address,
                image_name,
                domain_join,
                status,
                started_at,
                completed_at,
                serial_number,
                model,
                last_error_message
            )
            SELECT
                id,
                computer_name,
                mac_address,
                ip_address,
                image_name,
                domain_join,
                status,
                started_at,
                completed_at,
                serial_number,
                model,
                last_error_message
            FROM deployments
            """
        )
        cursor.execute("DROP TABLE deployments")
        cursor.execute(
            "ALTER TABLE deployments_timeout_upgrade RENAME TO deployments"
        )
        cursor.execute(
            "CREATE INDEX ix_deployments_status ON deployments (status)"
        )
        cursor.execute(
            "CREATE INDEX ix_deployments_computer_name "
            "ON deployments (computer_name)"
        )
        violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                "SQLite foreign key check failed after deployment schema upgrade"
            )
        cursor.execute("COMMIT")
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        connection.close()


def _upgrade_deployment_constraints(target_engine: Engine) -> None:
    if (
        _deployment_constraints_support_failed(target_engine) and
        _deployments_image_name_is_nullable(target_engine)
    ):
        return

    if target_engine.dialect.name == "sqlite":
        _upgrade_sqlite_deployment_constraints(target_engine)
        return

    with target_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE deployments "
                "DROP CONSTRAINT ck_deployments_status"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE deployments "
                "DROP CONSTRAINT ck_deployments_completion"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE deployments "
                "ADD CONSTRAINT ck_deployments_status "
                "CHECK (status IN ('begin', 'completed', 'failed'))"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE deployments "
                "ADD CONSTRAINT ck_deployments_completion CHECK ("
                "(status = 'begin' AND completed_at IS NULL) OR "
                "(status IN ('completed', 'failed') "
                "AND completed_at IS NOT NULL))"
            )
        )
        connection.execute(
            text("ALTER TABLE deployments ALTER COLUMN image_name DROP NOT NULL")
        )


def initialize_database(target_engine: Engine = engine) -> None:
    Base.metadata.create_all(bind=target_engine)

    table_name = Deployment.__tablename__
    columns = {
        column["name"]
        for column in inspect(target_engine).get_columns(table_name)
    }
    if "serial_number" not in columns:
        serial_type = String(128).compile(dialect=target_engine.dialect)
        with target_engine.begin() as connection:
            connection.execute(
                text(
                    f"ALTER TABLE {table_name} "
                    f"ADD COLUMN serial_number {serial_type}"
                )
            )
        columns.add("serial_number")
    if "last_error_message" not in columns:
        error_type = Text().compile(dialect=target_engine.dialect)
        with target_engine.begin() as connection:
            connection.execute(
                text(
                    f"ALTER TABLE {table_name} "
                    f"ADD COLUMN last_error_message {error_type}"
                )
            )
        columns.add("last_error_message")
    if "model" not in columns:
        model_type = String(128).compile(dialect=target_engine.dialect)
        with target_engine.begin() as connection:
            connection.execute(
                text(f"ALTER TABLE {table_name} ADD COLUMN model {model_type}")
            )
        columns.add("model")

    computer_table = "computers"
    computer_columns = {
        column["name"]
        for column in inspect(target_engine).get_columns(computer_table)
    }
    if "last_model" not in computer_columns:
        model_type = String(128).compile(dialect=target_engine.dialect)
        with target_engine.begin() as connection:
            connection.execute(
                text(
                    f"ALTER TABLE {computer_table} "
                    f"ADD COLUMN last_model {model_type}"
                )
            )

    stage_table = "deployment_stages"
    stage_columns = {
        column["name"]
        for column in inspect(target_engine).get_columns(stage_table)
    }
    if "error_message" not in stage_columns:
        error_type = Text().compile(dialect=target_engine.dialect)
        with target_engine.begin() as connection:
            connection.execute(
                text(
                    f"ALTER TABLE {stage_table} "
                    f"ADD COLUMN error_message {error_type}"
                )
            )

    program_table = "deployment_programs"
    program_columns = {
        column["name"]
        for column in inspect(target_engine).get_columns(program_table)
    }
    if "reason" not in program_columns:
        reason_type = String(32).compile(dialect=target_engine.dialect)
        with target_engine.begin() as connection:
            connection.execute(
                text(
                    f"ALTER TABLE {program_table} "
                    f"ADD COLUMN reason {reason_type}"
                )
            )

    _upgrade_deployment_constraints(target_engine)
    _backfill_computer_inventory(target_engine)


def _backfill_computer_inventory(target_engine: Engine) -> None:
    with Session(target_engine) as session:
        deployments = session.scalars(
            text(
                "SELECT id FROM deployments "
                "ORDER BY started_at ASC, id ASC"
            )
        ).all()
        if not deployments:
            return

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


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
