import asyncio
import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("IRONAPI_DATABASE_URL", "sqlite:///:memory:")
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

from fastapi import HTTPException

from app.main import update_program_arguments


class JsonRequest:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {
            "host": "192.0.2.10:8000",
            "x-requested-with": "IronDeploy",
        }

    async def json(self):
        return self.payload


class ProgramArgumentsApiTests(unittest.TestCase):
    def test_non_string_arguments_are_rejected(self) -> None:
        for arguments in (None, 123, ["/S"], {"value": "/S"}):
            with self.subTest(arguments=arguments):
                with (
                    patch("app.main.set_program_arguments") as set_arguments,
                    self.assertRaises(HTTPException) as raised,
                ):
                    asyncio.run(
                        update_program_arguments(
                            "tool.exe",
                            JsonRequest({"arguments": arguments}),
                        )
                    )

                self.assertEqual(raised.exception.status_code, 400)
                self.assertEqual(
                    raised.exception.detail,
                    "arguments must be a string.",
                )
                set_arguments.assert_not_called()

    @patch("app.main.set_program_arguments")
    def test_string_arguments_are_passed_through(self, set_arguments) -> None:
        set_arguments.return_value = {
            "name": "tool.exe",
            "arguments": "/S",
        }

        response = asyncio.run(
            update_program_arguments(
                "tool.exe",
                JsonRequest({"arguments": "/S"}),
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.body),
            {"name": "tool.exe", "arguments": "/S"},
        )
        set_arguments.assert_called_once_with("tool.exe", "/S")


if __name__ == "__main__":
    unittest.main()
