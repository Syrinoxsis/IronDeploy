import unittest
from ipaddress import IPv4Network
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.ldap_names import (
    DirectoryLookupError,
    _adsi_connection,
    _escape_filter_value,
    computer_exists,
    suggest_computer_name,
)


def make_settings(**overrides) -> Settings:
    values = {
        "database_url": "sqlite:///test.db",
        "deployment_secret_sha256": "0" * 64,
        "allowed_client_networks": (IPv4Network("192.0.2.0/24"),),
        "ldap_server": "dc01.example.test",
        "ldap_base_dn": "DC=example,DC=test",
        "ldap_use_ssl": False,
        "ldap_connect_timeout": 5,
        "odj_domain": "example.test",
        "odj_machine_ou": (
            "OU=Workstations,OU=Clients,DC=example,DC=test"
        ),
        "odj_blob_dir": "ODJ",
        "odj_djoin_path": "C:/Windows/System32/djoin.exe",
        "odj_provision_timeout": 60,
    }
    values.update(overrides)
    return Settings(**values)


class ComputerExistsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings()

    def test_exact_sam_account_name_is_found(self) -> None:
        with patch(
            "app.ldap_names._run_adsi_search",
            return_value=[{"distinguishedName": ["CN=pc00042"]}],
        ) as search:
            exists = computer_exists(self.settings, "pc00042")

        self.assertTrue(exists)
        search.assert_called_once_with(
            self.settings,
            (
                "(&(objectCategory=computer)"
                "(sAMAccountName=pc00042$))"
            ),
            ("distinguishedName",),
            size_limit=1,
        )

    def test_missing_account_returns_false(self) -> None:
        with patch(
            "app.ldap_names._run_adsi_search",
            return_value=[],
        ):
            exists = computer_exists(self.settings, "pc00042")

        self.assertFalse(exists)

    def test_disabled_ldap_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            DirectoryLookupError,
            "required for ODJ account detection",
        ):
            computer_exists(
                make_settings(ldap_server=None, ldap_base_dn=None),
                "pc00042",
            )

    def test_search_failure_is_wrapped(self) -> None:
        with patch(
            "app.ldap_names._run_adsi_search",
            side_effect=RuntimeError("unavailable"),
        ):
            with self.assertRaisesRegex(
                DirectoryLookupError,
                "LDAP computer lookup failed",
            ):
                computer_exists(self.settings, "pc00042")

    def test_name_suggestion_uses_adsi_rows(self) -> None:
        with patch(
            "app.ldap_names._run_adsi_search",
            return_value=[
                {"cn": ["pc00008"], "sAMAccountName": ["pc00008$"]},
                {"cn": ["unrelated"], "sAMAccountName": []},
            ],
        ) as search:
            suggestion = suggest_computer_name(self.settings, "pc", 5, 1)

        self.assertEqual(suggestion.last_domain_name, "pc00008")
        self.assertEqual(suggestion.suggested_name, "pc00009")
        search.assert_called_once_with(
            self.settings,
            (
                "(&(objectCategory=computer)"
                "(|(cn=pc*)(sAMAccountName=pc*)))"
            ),
            ("cn", "sAMAccountName"),
        )

    def test_filter_values_are_escaped(self) -> None:
        self.assertEqual(
            _escape_filter_value("pc(*)\\"),
            r"pc\28\2a\29\5c",
        )

    def test_adsi_connection_uses_current_windows_identity(self) -> None:
        connection = MagicMock()
        flag_property = MagicMock()
        connection.Properties.return_value = flag_property

        with (
            patch(
                "app.ldap_names.win32com.client.Dispatch",
                return_value=connection,
            ),
            patch("app.ldap_names.pythoncom.CoInitialize") as initialize,
            patch("app.ldap_names.pythoncom.CoUninitialize") as uninitialize,
        ):
            with _adsi_connection(self.settings) as opened:
                self.assertIs(opened, connection)

        initialize.assert_called_once_with()
        connection.Properties.assert_called_once_with("ADSI Flag")
        self.assertEqual(flag_property.Value, 1)
        connection.Open.assert_called_once_with("Active Directory Provider")
        connection.Close.assert_called_once_with()
        uninitialize.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
