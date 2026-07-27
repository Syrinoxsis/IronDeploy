import os

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
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app.auth import create_deployment_token, create_user, set_user_permissions
from app.database import initialize_database
from app.deployments import (
    Base,
    Computer,
    Deployment,
    DeploymentBeginRequest,
    update_computer_inventory,
)
from app.main import computer_list, deploy_begin, deployment_list


IRONDEPLOY_ROOT = Path(__file__).resolve().parents[2]
ENGINE_PATH = IRONDEPLOY_ROOT / "WinPE" / "Runtime" / "IronDeploy.Engine.ps1"
GUI_PATH = IRONDEPLOY_ROOT / "WinPE" / "Runtime" / "IronDeploy.Gui.ps1"
STATIC_ROOT = IRONDEPLOY_ROOT / "Api" / "app" / "static"


class HardwareModelRequestTests(unittest.TestCase):
    @staticmethod
    def request(**overrides) -> DeploymentBeginRequest:
        payload = {
            "computer_name": "pc00042",
            "serial_number": "PF4ABC12",
            "mac_address": "AA:BB:CC:DD:EE:FF",
            "domain_join": False,
        }
        payload.update(overrides)
        return DeploymentBeginRequest(**payload)

    def test_model_is_optional_for_older_winpe_images(self) -> None:
        self.assertIsNone(self.request().model)
        self.assertIsNone(self.request().manufacturer)
        self.assertIsNone(self.request().system_sku)

    def test_model_whitespace_is_collapsed(self) -> None:
        self.assertEqual(
            self.request(model="  HP EliteDesk\t800  G6 \n").model,
            "HP EliteDesk 800 G6",
        )

    def test_blank_and_control_characters_become_none(self) -> None:
        for value in ("", "   ", "\t\n", "ThinkPad\x00T14"):
            with self.subTest(value=value):
                self.assertIsNone(self.request(model=value).model)

    def test_model_longer_than_the_column_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.request(model="X" * 129)

    def test_optional_hardware_identity_is_normalized(self) -> None:
        request = self.request(
            manufacturer="  HP\tInc. ",
            system_sku="  1D2E3EA#ACB \n",
        )
        self.assertEqual(request.manufacturer, "HP Inc.")
        self.assertEqual(request.system_sku, "1D2E3EA#ACB")

    def test_invalid_optional_hardware_identity_becomes_none(self) -> None:
        for field in ("manufacturer", "system_sku"):
            with self.subTest(field=field):
                self.assertIsNone(getattr(self.request(**{field: " \t\n"}), field))
                self.assertIsNone(
                    getattr(self.request(**{field: "value\x00"}), field)
                )
                with self.assertRaises(ValidationError):
                    self.request(**{field: "X" * 129})


class HardwareModelStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def deploy_request(self, session: Session) -> SimpleNamespace:
        user = create_user(session, "winpe-operator", "OperatorPassword123")
        set_user_permissions(session, user, {"deploy"})
        _, token = create_deployment_token(session, user)
        return SimpleNamespace(
            client=SimpleNamespace(host="192.0.2.42"),
            state=SimpleNamespace(deployment_token_id=token.id),
        )

    @staticmethod
    def begin_payload(**overrides) -> DeploymentBeginRequest:
        payload = {
            "computer_name": "pc00042",
            "serial_number": "PF4ABC12",
            "mac_address": "AA:BB:CC:DD:EE:FF",
            "model": "ThinkPad T14 Gen 2",
            "manufacturer": "LENOVO",
            "system_sku": "20XW00A6US",
            "domain_join": False,
        }
        payload.update(overrides)
        return DeploymentBeginRequest(**payload)

    def test_begin_stores_the_model_and_both_lists_expose_it(self) -> None:
        with Session(self.engine) as session:
            request = self.deploy_request(session)
            response = deploy_begin(self.begin_payload(), request, session)

            stored = session.get(Deployment, response.deployment_id)
            self.assertEqual(stored.model, "ThinkPad T14 Gen 2")
            self.assertEqual(stored.manufacturer, "LENOVO")
            self.assertEqual(stored.system_sku, "20XW00A6US")

            deployments = deployment_list(limit=500, session=session)
            self.assertEqual(
                deployments.items[0].model,
                "ThinkPad T14 Gen 2",
            )
            self.assertEqual(deployments.items[0].manufacturer, "LENOVO")
            self.assertEqual(deployments.items[0].system_sku, "20XW00A6US")

            computers = computer_list(limit=500, session=session)
            self.assertEqual(
                computers.items[0].last_model,
                "ThinkPad T14 Gen 2",
            )

    def test_deployment_without_a_model_keeps_the_known_one(self) -> None:
        with Session(self.engine) as session:
            first = Deployment(
                computer_name="pc00042",
                serial_number="PF4ABC12",
                model="ThinkPad T14 Gen 2",
                mac_address="AA:BB:CC:DD:EE:FF",
                ip_address="192.0.2.42",
                image_name="win11.wim",
                domain_join=False,
                status="begin",
            )
            session.add(first)
            session.flush()
            update_computer_inventory(session, first)
            session.commit()

            # A WinPE image built before model reporting registers the same host.
            second = Deployment(
                computer_name="pc00042",
                serial_number="PF4ABC12",
                model=None,
                mac_address="AA:BB:CC:DD:EE:FF",
                ip_address="192.0.2.42",
                image_name="win11.wim",
                domain_join=False,
                status="begin",
            )
            session.add(second)
            session.flush()
            computer = update_computer_inventory(session, second)
            session.commit()

            self.assertEqual(computer.last_model, "ThinkPad T14 Gen 2")
            self.assertEqual(
                session.scalar(
                    text("SELECT COUNT(*) FROM computers")
                ),
                1,
            )


class HardwareModelSchemaUpgradeTests(unittest.TestCase):
    def test_model_columns_are_added_to_an_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "existing.db"
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")

            # Build the current schema, then drop the new columns to emulate a
            # database created before hardware-model reporting.
            Base.metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE deployments DROP COLUMN model"))
                connection.execute(
                    text("ALTER TABLE deployments DROP COLUMN manufacturer")
                )
                connection.execute(
                    text("ALTER TABLE deployments DROP COLUMN system_sku")
                )
                connection.execute(
                    text("ALTER TABLE computers DROP COLUMN last_model")
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO deployments (
                            id, computer_name, serial_number, mac_address,
                            ip_address, image_name, domain_join, status,
                            started_at
                        ) VALUES (
                            7, 'pc00007', 'PF4ABC12', 'AA:BB:CC:DD:EE:FF',
                            '192.0.2.7', 'win11.wim', 0, 'begin',
                            '2026-07-03 08:00:00'
                        )
                        """
                    )
                )

            initialize_database(engine)
            initialize_database(engine)

            deployment_columns = {
                column["name"]
                for column in inspect(engine).get_columns("deployments")
            }
            computer_columns = {
                column["name"]
                for column in inspect(engine).get_columns("computers")
            }
            self.assertIn("model", deployment_columns)
            self.assertIn("manufacturer", deployment_columns)
            self.assertIn("system_sku", deployment_columns)
            self.assertIn("last_model", computer_columns)
            self.assertNotIn("manufacturer", computer_columns)
            self.assertNotIn("system_sku", computer_columns)

            with Session(engine) as session:
                self.assertIsNone(session.get(Deployment, 7).model)
                # The backfill created inventory from the migrated deployment.
                computer = session.scalars(select(Computer)).first()
                self.assertIsNotNone(computer)
                self.assertIsNone(computer.last_model)

            engine.dispose()

    def test_sqlite_table_rebuild_preserves_the_model(self) -> None:
        # The legacy-constraint upgrade recreates ``deployments`` from an
        # explicit column list, so the model must survive that path.
        rebuild = (Path(IRONDEPLOY_ROOT) / "Api" / "app" / "database.py").read_text(
            encoding="utf-8"
        )
        start = rebuild.index("CREATE TABLE deployments_timeout_upgrade")
        end = rebuild.index("DROP TABLE deployments")
        rebuild_sql = rebuild[start:end]
        self.assertEqual(rebuild_sql.count("model"), 3)
        self.assertEqual(rebuild_sql.count("manufacturer"), 3)
        self.assertEqual(rebuild_sql.count("system_sku"), 3)


class HardwareModelSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = ENGINE_PATH.read_text(encoding="utf-8-sig")
        cls.gui = GUI_PATH.read_text(encoding="utf-8-sig")
        cls.dashboard = (STATIC_ROOT / "dashboard.js").read_text(encoding="utf-8")
        cls.styles = (STATIC_ROOT / "dashboard.css").read_text(encoding="utf-8")

    def test_engine_reads_and_reports_the_model(self) -> None:
        self.assertIn("function Get-SystemModel", self.engine)
        self.assertIn("Win32_ComputerSystem", self.engine)
        self.assertIn("Model = $Model", self.engine)
        self.assertIn("model = if ([string]::IsNullOrWhiteSpace", self.engine)

    def test_engine_reads_and_reports_manufacturer_and_system_sku(self) -> None:
        self.assertIn("function Get-SystemManufacturerAndSku", self.engine)
        self.assertIn("ComputerSystem.Manufacturer", self.engine)
        self.assertIn("ComputerSystem.SystemSKUNumber", self.engine)
        self.assertIn("manufacturer = if (", self.engine)
        self.assertIn("system_sku = if (", self.engine)

    def test_engine_never_fails_the_deployment_on_a_missing_model(self) -> None:
        start = self.engine.index("function Get-SystemModel")
        end = self.engine.index("function Get-SystemSerialNumber", start)
        self.assertNotIn("Fail ", self.engine[start:end])

    def test_gui_shows_the_model_next_to_the_driver_selection(self) -> None:
        self.assertIn('x:Name="ConfirmModelLabel"', self.gui)
        self.assertIn('x:Name="ConfirmModelText"', self.gui)
        self.assertIn('ConfirmModelLabel = "MODEL"', self.gui)
        self.assertIn('ConfirmModelLabel = "МОДЕЛЬ"', self.gui)
        # The label must be re-translated when the operator switches language.
        applied = self.gui[self.gui.index("$script:IronGuiApplyLanguage = {"):]
        self.assertIn('"ConfirmModelLabel"', applied[: applied.index("}")])

    def test_gui_model_card_column_count_matches_its_definitions(self) -> None:
        card = self.gui[
            self.gui.index('<Grid x:Name="DriversPage"') : self.gui.index(
                'x:Name="DriversLabel"'
            )
        ]
        self.assertEqual(card.count("<ColumnDefinition"), 4)
        self.assertEqual(card.count('Grid.Column="3"'), 1)

    def test_dashboard_headers_and_cells_stay_aligned(self) -> None:
        for view, field in (
            ("renderDeploymentRows", "deployment.model"),
            ("renderComputerRows", "computer.last_model"),
        ):
            with self.subTest(view=view):
                start = self.dashboard.index(f"function {view}(")
                end = self.dashboard.index("renderEmptyMessage(", start)
                body = self.dashboard[start:end]
                headers = body[body.index("createHeader([") : body.index("]);")]
                cells = body[body.index("row.append(") :]
                self.assertIn('"Model"', headers)
                self.assertIn(field, cells)
                # The Model header and its cell must share the same position.
                header_names = [
                    line.strip().strip(',').strip('"')
                    for line in headers.splitlines()[1:]
                    if line.strip()
                ]
                cell_lines = [
                    line.strip()
                    for line in cells.splitlines()[1:]
                    if line.strip().startswith("create")
                ]
                self.assertEqual(len(header_names), len(cell_lines))
                self.assertIn(
                    field,
                    cell_lines[header_names.index("Model")],
                )

    def test_dashboard_search_covers_the_model(self) -> None:
        self.assertIn("deployment.model,", self.dashboard)
        self.assertIn("computer.last_model,", self.dashboard)

    def test_computer_dashboard_does_not_show_deployment_hardware_fields(self) -> None:
        start = self.dashboard.index("function renderComputerRows(")
        end = self.dashboard.index("renderEmptyMessage(", start)
        computer_rows = self.dashboard[start:end]
        self.assertNotIn("manufacturer", computer_rows)
        self.assertNotIn("system_sku", computer_rows)

    def test_column_widths_cover_every_column(self) -> None:
        for table, count in (("deployment-table", 14), ("computer-table", 12)):
            with self.subTest(table=table):
                self.assertIn(f".{table} :is(th, td):nth-child({count})", self.styles)
                self.assertNotIn(
                    f".{table} :is(th, td):nth-child({count + 1})",
                    self.styles,
                )


if __name__ == "__main__":
    unittest.main()
