import os

os.environ.setdefault("IRONAPI_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("IRONAPI_NAME_PREFIX", "pc")
os.environ.setdefault("IRONAPI_NAME_WIDTH", "5")
os.environ.setdefault("IRONAPI_NAME_START", "1")
os.environ.setdefault("IRONAPI_ALLOWED_CLIENT_NETWORKS", "192.0.2.0/24")
os.environ.setdefault("IRONAPI_LDAP_SERVER", "dc01.example.test")
os.environ.setdefault("IRONAPI_LDAP_BASE_DN", "DC=example,DC=test")
os.environ.setdefault("IRONAPI_LDAP_CREDENTIAL_TARGET", "IronDeploy-LDAP")
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

import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import Session

from app.auth import create_deployment_token, create_user, set_user_permissions
from app.deployments import (
    Base,
    DeploymentBeginRequest,
    DeploymentNetworkDiagnosticsRequest,
    DeploymentNetworkStage,
    DeploymentNetworkSummary,
)
from app.main import (
    deploy_begin,
    deployment_detail,
    save_deployment_network_diagnostics,
)


IRONDEPLOY_ROOT = Path(__file__).resolve().parents[2]
ENGINE_PATH = IRONDEPLOY_ROOT / "WinPE" / "Runtime" / "IronDeploy.Engine.ps1"


def aggregate_payload(
    *,
    started_at: datetime,
    completed_at: datetime,
    sent: int = 3,
    received: int = 2,
) -> dict:
    available = received > 0
    return {
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": (completed_at - started_at).total_seconds(),
        "icmp_status": "available" if available else "unavailable",
        "ping_sent": sent,
        "ping_received": received,
        "ping_lost": sent - received,
        "loss_percentage": ((sent - received) * 100 / sent) if available else None,
        "rtt_min_ms": 10 if available else None,
        "rtt_avg_ms": 30 if available else None,
        "rtt_max_ms": 60 if available else None,
        "latency_spikes": 1 if available else 0,
        "bytes_received": 12 * 1024 * 1024,
        "average_inbound_mbps": 4,
        "link_utilization_percent": 3.2,
    }


class NetworkDiagnosticsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    @staticmethod
    def payload() -> DeploymentNetworkDiagnosticsRequest:
        started_at = datetime.now(timezone.utc) - timedelta(seconds=3)
        completed_at = datetime.now(timezone.utc)
        return DeploymentNetworkDiagnosticsRequest(
            ping_target="fileserver.example.test",
            smb_adapter={
                "name": "Ethernet",
                "description": "Intel Ethernet",
                "adapter_id": "smb-adapter",
                "local_ip": "192.0.2.42",
                "link_speed_bps": 1_000_000_000,
            },
            api_adapter={
                "name": "Management",
                "description": "USB Ethernet",
                "adapter_id": "api-adapter",
                "local_ip": "198.51.100.42",
                "link_speed_bps": 100_000_000,
            },
            adapters_differ=True,
            overall=aggregate_payload(
                started_at=started_at,
                completed_at=completed_at,
                sent=3,
                received=0,
            ),
            stages=[
                {
                    "stage": "image_apply",
                    **aggregate_payload(
                        started_at=started_at,
                        completed_at=completed_at,
                    ),
                }
            ],
            api={
                "request_count": 7,
                "error_count": 1,
                "min_ms": 8,
                "avg_ms": 24,
                "max_ms": 95,
            },
            smb={
                "success": True,
                "attempts": 1,
                "duration_ms": 122,
                "error_message": None,
            },
            diagnostic_errors=["Link speed was unavailable once"],
        )

    @staticmethod
    def deployment_request(session: Session) -> SimpleNamespace:
        user = create_user(session, "winpe-network", "OperatorPassword123")
        set_user_permissions(session, user, {"deploy"})
        _, token = create_deployment_token(session, user)
        return SimpleNamespace(
            client=SimpleNamespace(host="192.0.2.42"),
            state=SimpleNamespace(deployment_token_id=token.id),
        )

    def test_network_report_is_idempotent_and_exposed_by_detail(self) -> None:
        with Session(self.engine) as session:
            request = self.deployment_request(session)
            deployment = deploy_begin(
                DeploymentBeginRequest(
                    computer_name="pc00042",
                    serial_number="PF4ABC12",
                    model="ThinkPad T14",
                    mac_address="AA:BB:CC:DD:EE:FF",
                    domain_join=False,
                ),
                request,
                session,
            )
            first = save_deployment_network_diagnostics(
                deployment.deployment_id,
                self.payload(),
                request,
                session,
            )
            second_payload = self.payload().model_copy(deep=True)
            second_payload.api.request_count = 8
            second = save_deployment_network_diagnostics(
                deployment.deployment_id,
                second_payload,
                request,
                session,
            )

            self.assertEqual(first.overall.icmp_status, "unavailable")
            self.assertIsNone(first.overall.loss_percentage)
            self.assertTrue(second.adapters_differ)
            self.assertEqual(second.api.request_count, 8)
            self.assertEqual(
                session.scalar(select(func.count(DeploymentNetworkSummary.deployment_id))),
                1,
            )
            self.assertEqual(
                session.scalar(select(func.count(DeploymentNetworkStage.id))),
                1,
            )

            detail = deployment_detail(deployment.deployment_id, session)
            self.assertIsNotNone(detail.network_diagnostics)
            self.assertEqual(
                detail.network_diagnostics.stages[0].stage,
                "image_apply",
            )
            self.assertEqual(
                detail.network_diagnostics.smb_adapter.local_ip,
                "192.0.2.42",
            )

    def test_ping_counts_must_be_consistent(self) -> None:
        payload = self.payload().model_dump()
        payload["overall"]["ping_lost"] = 99
        with self.assertRaises(ValidationError):
            DeploymentNetworkDiagnosticsRequest(**payload)

    def test_schema_stores_only_aggregates_not_raw_ping_samples(self) -> None:
        tables = set(inspect(self.engine).get_table_names())
        self.assertIn("deployment_network_summaries", tables)
        self.assertIn("deployment_network_stages", tables)
        self.assertNotIn("deployment_ping_samples", tables)


class NetworkDiagnosticsPowerShellTests(unittest.TestCase):
    @staticmethod
    def run_powershell(assertions: str) -> subprocess.CompletedProcess[str]:
        escaped_engine = str(ENGINE_PATH).replace("'", "''")
        script = f"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    '{escaped_engine}',
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
foreach ($name in @(
    'Start-IronApiTimingCollection',
    'Add-IronApiRequestMeasurement',
    'Get-IronApiAggregate',
    'Get-IronNetworkPingStatistics',
    'New-IronNetworkAggregate',
    'Start-IronPingMonitor',
    'Stop-IronPingMonitor'
)) {{
    $functionAst = $ast.FindAll(
        {{
            param($node)
            $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
        }},
        $true
    ) | Select-Object -First 1
    if ($null -eq $functionAst) {{ throw "Missing function $name" }}
    Invoke-Expression $functionAst.Extent.Text
}}
{assertions}
"""
        return subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def assert_powershell(self, assertions: str) -> None:
        result = self.run_powershell(assertions)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_loss_rtt_spikes_and_inbound_rate_calculation(self) -> None:
        self.assert_powershell(
            """
$start = [DateTime]::UtcNow.AddSeconds(-4)
$samples = @(
    [pscustomobject]@{
        TimestampUtc = $start.AddMilliseconds(100)
        Success = $true
        LatencyMs = 10
    },
    [pscustomobject]@{
        TimestampUtc = $start.AddMilliseconds(1100)
        Success = $false
        LatencyMs = $null
    },
    [pscustomobject]@{
        TimestampUtc = $start.AddMilliseconds(2100)
        Success = $true
        LatencyMs = 70
    },
    [pscustomobject]@{
        TimestampUtc = $start.AddMilliseconds(2500)
        Success = $true
        LatencyMs = 50
    }
)
$result = New-IronNetworkAggregate `
    -Samples $samples `
    -StartedAt $start `
    -CompletedAt $start.AddSeconds(3) `
    -BytesBefore 1000 `
    -BytesAfter 3146728 `
    -LinkSpeedBps 1000000000
if ($result.ping_sent -ne 4 -or $result.ping_lost -ne 1) {
    throw 'Incorrect ping counts'
}
if ([Math]::Abs($result.loss_percentage - 25) -gt 0.001) {
    throw 'Incorrect loss percentage'
}
if (
    $result.rtt_min_ms -ne 10 -or
    [Math]::Abs($result.rtt_avg_ms - 43.333) -gt 0.001 -or
    $result.rtt_max_ms -ne 70 -or
    $result.latency_spikes -ne 1
) {
    throw 'Incorrect RTT statistics'
}
if ([Math]::Abs($result.average_inbound_mbps - 1) -gt 0.001) {
    throw 'Incorrect average inbound rate'
}
if ([Math]::Abs($result.link_utilization_percent - 0.839) -gt 0.001) {
    throw 'Incorrect link utilization'
}
"""
        )

    def test_no_replies_means_icmp_unavailable(self) -> None:
        self.assert_powershell(
            """
$start = [DateTime]::UtcNow.AddSeconds(-2)
$samples = @(
    [pscustomobject]@{
        TimestampUtc = $start.AddMilliseconds(100)
        Success = $false
        LatencyMs = $null
    }
)
$result = Get-IronNetworkPingStatistics `
    -Samples $samples `
    -StartedAt $start `
    -CompletedAt $start.AddSeconds(1)
if ($result.icmp_status -ne 'unavailable') {
    throw 'ICMP should be unavailable'
}
if ($null -ne $result.loss_percentage) {
    throw 'Unavailable ICMP must not be displayed as 100 percent loss'
}
"""
        )

    def test_ping_runspace_stops_and_is_disposed(self) -> None:
        self.assert_powershell(
            """
$samples = New-Object 'System.Collections.Concurrent.ConcurrentQueue[object]'
$errors = New-Object 'System.Collections.Concurrent.ConcurrentQueue[string]'
$monitor = Start-IronPingMonitor `
    -TargetHost '127.0.0.1' `
    -SampleQueue $samples `
    -ErrorQueue $errors `
    -TimeoutMs 800 `
    -IntervalMs 100
Start-Sleep -Milliseconds 350
Stop-IronPingMonitor -Monitor $monitor -ErrorQueue $errors
if (-not $monitor.Stopped) { throw 'Monitor was not marked stopped' }
if ($null -ne $monitor.Runspace -or $null -ne $monitor.PowerShell) {
    throw 'Monitor resources were not released'
}
if ($samples.Count -lt 1) { throw 'Monitor recorded no ping samples' }
"""
        )

    def test_existing_api_request_timings_are_aggregated(self) -> None:
        self.assert_powershell(
            """
Start-IronApiTimingCollection
Add-IronApiRequestMeasurement `
    -Uri 'https://api.test/one' `
    -Method Get `
    -DurationMs 10 `
    -Success $true
Add-IronApiRequestMeasurement `
    -Uri 'https://api.test/two' `
    -Method Post `
    -DurationMs 30 `
    -Success $true
Add-IronApiRequestMeasurement `
    -Uri 'https://api.test/error' `
    -Method Get `
    -DurationMs 100 `
    -Success $false `
    -ErrorMessage 'timeout'
$result = Get-IronApiAggregate
if ($result.request_count -ne 3 -or $result.error_count -ne 1) {
    throw 'Incorrect API request counts'
}
if (
    $result.min_ms -ne 10 -or
    $result.avg_ms -ne 20 -or
    $result.max_ms -ne 30
) {
    throw 'Incorrect successful API request timing'
}
"""
        )

    def test_monitor_uses_no_external_processes(self) -> None:
        engine = ENGINE_PATH.read_text(encoding="utf-8-sig")
        start = engine.index("function Start-IronPingMonitor")
        end = engine.index("function Stop-IronPingMonitor", start)
        monitor = engine[start:end]
        for forbidden in ("ping.exe", "Start-Job", "cmd.exe", "powershell.exe"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, monitor)
        self.assertIn("[powershell]::Create()", monitor)
        self.assertIn("[runspacefactory]::CreateRunspace()", monitor)

    def test_smb_adapter_is_selected_from_the_real_route(self) -> None:
        engine = ENGINE_PATH.read_text(encoding="utf-8-sig")
        start = engine.index("function Get-IronNetworkRouteAdapter")
        end = engine.index("function Get-IronAdapterBytesReceived", start)
        adapter_selection = engine[start:end]
        self.assertIn("$Socket.Connect(", adapter_selection)
        self.assertIn("$Socket.LocalEndPoint.Address", adapter_selection)
        self.assertIn("UnicastAddresses", adapter_selection)
        self.assertNotIn("OperationalStatus", adapter_selection)

    def test_monitor_lifecycle_is_wired_to_deployment_lifecycle(self) -> None:
        engine = ENGINE_PATH.read_text(encoding="utf-8-sig")

        pipeline_start = engine.index("function Invoke-IronDeployment")
        pipeline = engine[pipeline_start:]
        credentials = pipeline.index("Get-IronDeployShareCredentials")
        monitor_start = pipeline.index("Start-IronNetworkDiagnostics")
        smb_connect = pipeline.index("Connect-IronDeployShare")
        final_report = pipeline.rindex("Complete-IronNetworkDiagnostics -Send")
        postinstall = pipeline.rindex("/postinstall")
        self.assertLess(credentials, monitor_start)
        self.assertLess(monitor_start, smb_connect)
        self.assertLess(postinstall, final_report)

        error_start = engine.index("function Send-DeploymentError")
        error_end = engine.index("function Fail", error_start)
        error_handler = engine[error_start:error_end]
        self.assertLess(
            error_handler.index("Complete-IronNetworkDiagnostics -Send"),
            error_handler.index("/error"),
        )

    def test_stage_windows_and_real_smb_connection_are_instrumented(self) -> None:
        engine = ENGINE_PATH.read_text(encoding="utf-8-sig")

        start_stage = engine[
            engine.index("function Start-DeploymentStage"):
            engine.index("function Complete-DeploymentStage")
        ]
        complete_stage = engine[
            engine.index("function Complete-DeploymentStage"):
            engine.index("function Skip-DeploymentStage")
        ]
        smb = engine[
            engine.index("function Connect-IronDeployShare"):
            engine.index("function Get-IronDeployCatalog")
        ]

        self.assertIn("Start-IronNetworkStageMeasurement", start_stage)
        self.assertIn("Complete-IronNetworkStageMeasurement", complete_stage)
        self.assertIn("[Diagnostics.Stopwatch]::StartNew()", smb)
        self.assertIn("Set-IronNetworkSmbResult", smb)
        self.assertIn("& net.exe use", smb)
        self.assertNotIn(":445", smb)
        self.assertNotIn("Test-NetConnection", smb)


if __name__ == "__main__":
    unittest.main()
