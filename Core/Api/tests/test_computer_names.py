import os
import unittest
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Network
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.computer_names import (
    ComputerNameFormatError,
    build_name_suggestion,
    evaluate_name_format,
    match_name_format,
    update_name_formats,
    validate_name_formats,
)
from app.config import Settings
from app.database import initialize_database
from app.deployments import Base, ComputerNameFormat, Deployment


def make_settings(**overrides) -> Settings:
    values = {
        "database_url": "sqlite:///:memory:",
        "allowed_client_networks": (IPv4Network("192.0.2.0/24"),),
        "ldap_server": "dc01.example.test",
        "ldap_base_dn": "DC=example,DC=test",
        "ldap_use_ssl": False,
        "ldap_connect_timeout": 5,
        "odj_domain": "example.test",
        "odj_machine_ou": "OU=Clients,DC=example,DC=test",
        "odj_blob_dir": "ODJ",
        "odj_djoin_path": "C:/Windows/System32/djoin.exe",
        "odj_provision_timeout": 60,
    }
    values.update(overrides)
    return Settings(**values)


def deployment(name: str, started_at: datetime) -> Deployment:
    return Deployment(
        computer_name=name,
        mac_address="AA:BB:CC:DD:EE:FF",
        ip_address="192.0.2.10",
        image_name="win11.wim",
        domain_join=False,
        status="completed",
        started_at=started_at,
        completed_at=started_at,
    )


class ComputerNameFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def test_local_suggestion_uses_maximum_suffix_not_latest_deployment(self) -> None:
        now = datetime.now(timezone.utc)
        self.session.add_all([
            deployment("ktc100", now - timedelta(days=1)),
            deployment("ktc042", now),
        ])
        name_format = ComputerNameFormat(
            prefix="ktc",
            number_width=3,
            start_number=1,
            domain_linked=False,
            position=0,
        )
        self.session.add(name_format)
        self.session.commit()

        result = evaluate_name_format(self.session, make_settings(), name_format)

        self.assertEqual(result.last_name, "ktc100")
        self.assertEqual(result.suggested_name, "ktc101")
        self.assertEqual(result.source, "irondeploy")

    def test_domain_suggestion_uses_directory_and_keeps_history_as_context(self) -> None:
        self.session.add(deployment("pc00999", datetime.now(timezone.utc)))
        name_format = ComputerNameFormat(
            prefix="pc",
            number_width=5,
            start_number=1,
            domain_linked=True,
            position=0,
        )
        self.session.add(name_format)
        self.session.commit()

        with patch(
            "app.ldap_names._run_adsi_search",
            return_value=[{"cn": ["pc00032"], "sAMAccountName": ["pc00032$"]}],
        ):
            result = evaluate_name_format(self.session, make_settings(), name_format)

        self.assertEqual(result.last_name, "pc00032")
        self.assertEqual(result.suggested_name, "pc00033")
        self.assertEqual(result.history_last_name, "pc00999")
        self.assertTrue(result.directory_available)
        self.assertTrue(result.domain_join_available)

    def test_unavailable_domain_never_falls_back_to_history_suggestion(self) -> None:
        self.session.add(deployment("pc00032", datetime.now(timezone.utc)))
        name_format = ComputerNameFormat(
            prefix="pc",
            number_width=5,
            start_number=1,
            domain_linked=True,
            position=0,
        )
        self.session.add(name_format)
        self.session.commit()

        result = evaluate_name_format(
            self.session,
            make_settings(ldap_server=None, ldap_base_dn=None),
            name_format,
        )

        self.assertFalse(result.directory_available)
        self.assertFalse(result.domain_join_available)
        self.assertIsNone(result.suggested_name)
        self.assertEqual(result.history_last_name, "pc00032")

    def test_overlapping_formats_are_rejected(self) -> None:
        with self.assertRaisesRegex(ComputerNameFormatError, "overlap"):
            validate_name_formats([
                {
                    "prefix": "pc",
                    "numberWidth": 3,
                    "startNumber": 1,
                    "domainLinked": True,
                },
                {
                    "prefix": "pc1",
                    "numberWidth": 2,
                    "startNumber": 1,
                    "domainLinked": False,
                },
            ])

    def test_update_and_match_use_only_new_database_rules(self) -> None:
        rows = update_name_formats(self.session, [
            {
                "prefix": "pc",
                "numberWidth": 5,
                "startNumber": 1,
                "domainLinked": True,
            },
            {
                "prefix": "ktc",
                "numberWidth": 3,
                "startNumber": 100,
                "domainLinked": False,
            },
        ])
        self.session.commit()

        self.assertTrue(match_name_format(rows, "PC00001").domain_linked)
        self.assertFalse(match_name_format(rows, "ktc100").domain_linked)
        self.assertIsNone(match_name_format(rows, "other01"))
        suggestion = build_name_suggestion(
            self.session,
            make_settings(ldap_server=None, ldap_base_dn=None),
            [],
        )
        self.assertEqual([item.pattern for item in suggestion.formats], [
            "pc#####",
            "ktc###",
        ])


class ComputerNameFormatMigrationTests(unittest.TestCase):
    def test_legacy_environment_is_imported_once_into_the_new_table(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        try:
            with patch.dict(os.environ, {
                "IRONAPI_NAME_PREFIX": "old",
                "IRONAPI_NAME_WIDTH": "4",
                "IRONAPI_NAME_START": "12",
                "IRONAPI_LDAP_SERVER": "dc01.example.test",
                "IRONAPI_LDAP_BASE_DN": "DC=example,DC=test",
            }):
                initialize_database(engine)

            with Session(engine) as session:
                rows = list(session.scalars(select(ComputerNameFormat)))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].prefix, "old")
            self.assertEqual(rows[0].number_width, 4)
            self.assertEqual(rows[0].start_number, 12)
            self.assertTrue(rows[0].domain_linked)
        finally:
            engine.dispose()

    def test_invalid_legacy_range_is_replaced_with_the_default_format(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        try:
            with patch.dict(os.environ, {
                "IRONAPI_NAME_PREFIX": "pc",
                "IRONAPI_NAME_WIDTH": "2",
                "IRONAPI_NAME_START": "100",
            }, clear=False):
                initialize_database(engine)

            with Session(engine) as session:
                row = session.scalar(select(ComputerNameFormat))
            self.assertIsNotNone(row)
            self.assertEqual(row.prefix, "pc")
            self.assertEqual(row.number_width, 5)
            self.assertEqual(row.start_number, 1)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
