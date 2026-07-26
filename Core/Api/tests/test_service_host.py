import io
from pathlib import Path
import sys
import unittest
from unittest import mock

import irondeploy_service


class ServiceHostTests(unittest.TestCase):
    def test_registration_uses_virtual_environment_python_host(self) -> None:
        registration_input = io.StringIO("local_system\nLocalSystem\n\n")

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


if __name__ == "__main__":
    unittest.main()
