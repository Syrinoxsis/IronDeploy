import io
from pathlib import Path
import sys
import unittest
from unittest import mock

import irondeploy_service


class ServiceHostTests(unittest.TestCase):
    def test_registration_uses_virtual_environment_python_host(self) -> None:
        registration_input = io.StringIO(
            "account\nEXAMPLE\\svc_irondeploy\nservice-password\n"
        )

        with (
            mock.patch.object(
                irondeploy_service.sys,
                "stdin",
                registration_input,
            ),
            mock.patch.object(
                irondeploy_service.win32serviceutil,
                "InstallService",
            ) as install_service,
        ):
            irondeploy_service._register_service("install-from-stdin")

        _, kwargs = install_service.call_args
        self.assertEqual(kwargs["exeName"], sys.executable)
        self.assertEqual(
            kwargs["exeArgs"],
            f'"{Path(irondeploy_service.__file__).resolve()}"',
        )

    def test_no_arguments_runs_native_service_dispatcher(self) -> None:
        with (
            mock.patch.object(irondeploy_service.sys, "argv", ["service.py"]),
            mock.patch.object(
                irondeploy_service.servicemanager,
                "Initialize",
            ) as initialize,
            mock.patch.object(
                irondeploy_service.servicemanager,
                "PrepareToHostSingle",
            ) as prepare,
            mock.patch.object(
                irondeploy_service.servicemanager,
                "StartServiceCtrlDispatcher",
            ) as start_dispatcher,
        ):
            irondeploy_service.main()

        initialize.assert_called_once_with()
        prepare.assert_called_once_with(irondeploy_service.IronAPIService)
        start_dispatcher.assert_called_once_with()


class RegistrationSecretTests(unittest.TestCase):
    def _read(self, payload: str) -> tuple[str, str]:
        with mock.patch.object(
            irondeploy_service.sys,
            "stdin",
            io.StringIO(payload),
        ):
            return irondeploy_service._read_registration_secret()

    def test_domain_and_local_accounts_are_accepted(self) -> None:
        for payload, expected in (
            ("account\nEXAMPLE\\svc_irondeploy\nsecret\n",
             ("EXAMPLE\\svc_irondeploy", "secret")),
            ("account\nSERVER01\\svc_irondeploy\nsecret\n",
             ("SERVER01\\svc_irondeploy", "secret")),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self._read(payload), expected)

    def test_built_in_and_unqualified_identities_are_rejected(self) -> None:
        # LocalSystem is no longer a supported service identity, and an
        # unqualified name would let Windows resolve it against another scope.
        for payload in (
            "local_system\nLocalSystem\n\n",
            "account\nLocalSystem\nsecret\n",
            "account\nsvc_irondeploy\nsecret\n",
            "account\nEXAMPLE\\svc_irondeploy\n\n",
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self._read(payload)


if __name__ == "__main__":
    unittest.main()
