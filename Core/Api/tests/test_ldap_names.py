import unittest
from ipaddress import IPv4Network
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.ldap_names import DirectoryLookupError, computer_exists


def make_settings(**overrides) -> Settings:
    values = {
        "database_url": "sqlite:///test.db",
        "deployment_secret_sha256": "0" * 64,
        "name_prefix": "pc",
        "name_width": 5,
        "name_start": 1,
        "allowed_client_networks": (IPv4Network("192.0.2.0/24"),),
        "ldap_server": "dc01.example.test",
        "ldap_base_dn": "DC=example,DC=test",
        "ldap_credential_target": "IronDeploy-LDAP",
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
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.search.return_value = True
        connection.entries = [object()]

        with patch(
            "app.ldap_names._create_connection",
            return_value=connection,
        ):
            exists = computer_exists(self.settings, "pc00042")

        self.assertTrue(exists)
        connection.search.assert_called_once_with(
            search_base="DC=example,DC=test",
            search_filter=(
                "(&(objectCategory=computer)"
                "(sAMAccountName=pc00042$))"
            ),
            attributes=["distinguishedName"],
            size_limit=1,
        )

    def test_missing_account_returns_false(self) -> None:
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.search.return_value = True
        connection.entries = []

        with patch(
            "app.ldap_names._create_connection",
            return_value=connection,
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
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.search.return_value = False
        connection.result = {"description": "unavailable"}

        with patch(
            "app.ldap_names._create_connection",
            return_value=connection,
        ):
            with self.assertRaisesRegex(
                DirectoryLookupError,
                "LDAP computer lookup failed",
            ):
                computer_exists(self.settings, "pc00042")


if __name__ == "__main__":
    unittest.main()
