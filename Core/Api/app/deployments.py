import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Float,
    Identity,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    and_,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.config import get_settings


logger = logging.getLogger("uvicorn.error")

DEPLOYMENT_BEGIN = "begin"
DEPLOYMENT_COMPLETED = "completed"
DEPLOYMENT_FAILED = "failed"
STAGE_RUNNING = "running"
STAGE_COMPLETED = "completed"
STAGE_FAILED = "failed"
STAGE_SKIPPED = "skipped"

WinPEStageCode = Literal[
    "disk_partitioning",
    "image_download",
    "image_apply",
    "driver_injection",
    "deployment_state",
    "unattend_generation",
    "unattend_apply",
    "domain_join",
    "postinstall_copy",
    "boot_files",
    "windows_setup",
]
DeploymentStageStatus = Literal["running", "completed", "failed", "skipped"]
DeploymentStageEvent = Literal["start", "complete", "fail", "skip"]


class Base(DeclarativeBase):
    pass


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('begin', 'completed', 'failed')",
            name="ck_deployments_status",
        ),
        CheckConstraint(
            "(status = 'begin' AND completed_at IS NULL) OR "
            "(status IN ('completed', 'failed') AND completed_at IS NOT NULL)",
            name="ck_deployments_completion",
        ),
        Index("ix_deployments_status", "status"),
        Index("ix_deployments_computer_name", "computer_name"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        Identity(),
        primary_key=True,
    )
    computer_name: Mapped[str] = mapped_column(String(63), nullable=False)
    serial_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    manufacturer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    system_sku: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mac_address: Mapped[str] = mapped_column(String(17), nullable=False)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False)
    image_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_apply_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_disk_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_disk_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_disk_size_bytes: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    domain_join: Mapped[bool] = mapped_column(Boolean, nullable=False)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=DEPLOYMENT_BEGIN,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class DeploymentStage(Base):
    __tablename__ = "deployment_stages"
    __table_args__ = (
        CheckConstraint(
            "phase IN ('winpe')",
            name="ck_deployment_stages_phase",
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'skipped')",
            name="ck_deployment_stages_status",
        ),
        CheckConstraint(
            "(status = 'running' AND completed_at IS NULL) OR "
            "(status IN ('completed', 'failed', 'skipped') "
            "AND completed_at IS NOT NULL)",
            name="ck_deployment_stages_completion",
        ),
        UniqueConstraint(
            "deployment_id",
            "stage",
            name="uq_deployment_stages_deployment_stage",
        ),
        Index("ix_deployment_stages_deployment_id", "deployment_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        Identity(),
        primary_key=True,
    )
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="winpe",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=STAGE_RUNNING,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class DeploymentNetworkSummary(Base):
    __tablename__ = "deployment_network_summaries"
    __table_args__ = (
        CheckConstraint(
            "icmp_status IN ('available', 'unavailable', 'not_measured')",
            name="ck_deployment_network_summaries_icmp_status",
        ),
    )

    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ping_target: Mapped[str] = mapped_column(String(255), nullable=False)
    smb_adapter_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smb_adapter_description: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    smb_adapter_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smb_local_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    smb_link_speed_bps: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    api_adapter_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    api_adapter_description: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    api_adapter_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    api_local_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    api_link_speed_bps: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    adapters_differ: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    icmp_status: Mapped[str] = mapped_column(String(16), nullable=False)
    ping_sent: Mapped[int] = mapped_column(Integer, nullable=False)
    ping_received: Mapped[int] = mapped_column(Integer, nullable=False)
    ping_lost: Mapped[int] = mapped_column(Integer, nullable=False)
    loss_percentage: Mapped[float | None] = mapped_column(Float, nullable=True)
    rtt_min_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    rtt_avg_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    rtt_max_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_spikes: Mapped[int] = mapped_column(Integer, nullable=False)
    bytes_received: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    average_inbound_mbps: Mapped[float | None] = mapped_column(Float, nullable=True)
    link_utilization_percent: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    api_request_count: Mapped[int] = mapped_column(Integer, nullable=False)
    api_error_count: Mapped[int] = mapped_column(Integer, nullable=False)
    api_min_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    api_avg_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    api_max_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    smb_connect_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    smb_connect_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    smb_connect_duration_ms: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    smb_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnostic_errors: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )


class DeploymentNetworkStage(Base):
    __tablename__ = "deployment_network_stages"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('image_download', 'image_apply', "
            "'driver_injection', 'postinstall_copy')",
            name="ck_deployment_network_stages_stage",
        ),
        CheckConstraint(
            "icmp_status IN ('available', 'unavailable', 'not_measured')",
            name="ck_deployment_network_stages_icmp_status",
        ),
        UniqueConstraint(
            "deployment_id",
            "stage",
            name="uq_deployment_network_stages_deployment_stage",
        ),
        Index(
            "ix_deployment_network_stages_deployment_id",
            "deployment_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        Identity(),
        primary_key=True,
    )
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    icmp_status: Mapped[str] = mapped_column(String(16), nullable=False)
    ping_sent: Mapped[int] = mapped_column(Integer, nullable=False)
    ping_received: Mapped[int] = mapped_column(Integer, nullable=False)
    ping_lost: Mapped[int] = mapped_column(Integer, nullable=False)
    loss_percentage: Mapped[float | None] = mapped_column(Float, nullable=True)
    rtt_min_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    rtt_avg_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    rtt_max_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_spikes: Mapped[int] = mapped_column(Integer, nullable=False)
    bytes_received: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    average_inbound_mbps: Mapped[float | None] = mapped_column(Float, nullable=True)
    link_utilization_percent: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )


class DeploymentProgram(Base):
    __tablename__ = "deployment_programs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('installed', 'failed', 'timed_out')",
            name="ck_deployment_programs_status",
        ),
        UniqueConstraint(
            "deployment_id",
            "position",
            name="uq_deployment_programs_deployment_position",
        ),
        Index("ix_deployment_programs_deployment_id", "deployment_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        Identity(),
        primary_key=True,
    )
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class Computer(Base):
    __tablename__ = "computers"
    __table_args__ = (
        Index("ix_computers_serial_number", "serial_number"),
        Index("ix_computers_mac_address", "mac_address"),
        Index("ix_computers_last_computer_name", "last_computer_name"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        Identity(),
        primary_key=True,
    )
    serial_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mac_address: Mapped[str | None] = mapped_column(String(17), nullable=True)
    last_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_computer_name: Mapped[str] = mapped_column(String(63), nullable=False)
    last_ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    last_image_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_domain_join: Mapped[bool] = mapped_column(Boolean, nullable=False)
    deployment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_deployment_id: Mapped[int | None] = mapped_column(
        ForeignKey("deployments.id", ondelete="SET NULL"),
        nullable=True,
    )


class DeploymentBeginRequest(BaseModel):
    computer_name: str = Field(min_length=1, max_length=63)
    serial_number: str | None = None
    # Optional so WinPE images built before hardware-model reporting keep working.
    model: str | None = Field(default=None, max_length=128)
    manufacturer: str | None = Field(default=None, max_length=128)
    system_sku: str | None = Field(default=None, max_length=128)
    mac_address: str
    image_name: str | None = Field(default=None, min_length=5, max_length=255)
    # Optional as a group so WinPE images built before disk selection remain
    # compatible with a newer IronAPI. Current WinPE always submits all three.
    target_disk_number: int | None = Field(default=None, ge=0, le=65535)
    target_disk_model: str | None = Field(default=None, max_length=255)
    target_disk_size_bytes: int | None = Field(default=None, gt=0)
    domain_join: bool

    @field_validator("computer_name")
    @classmethod
    def validate_computer_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", normalized):
            raise ValueError("computer_name is not a valid DNS host name")
        return normalized

    @field_validator("serial_number", mode="before")
    @classmethod
    def validate_serial_number(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > 128
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in normalized
            )
            or re.fullmatch(
                r"(?:To Be Filled By O\.?E\.?M\.?|Default string|"
                r"System Serial Number|Unknown|None|Not Applicable|"
                r"Not Specified|OEM|INVALID)",
                normalized,
                flags=re.IGNORECASE,
            )
        ):
            return None
        return normalized

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized or any(ord(character) < 32 for character in normalized):
            return None
        return normalized

    @field_validator("manufacturer", "system_sku")
    @classmethod
    def validate_optional_hardware_identity(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized or any(ord(character) < 32 for character in normalized):
            return None
        return normalized

    @field_validator("mac_address")
    @classmethod
    def validate_mac_address(cls, value: str) -> str:
        compact = re.sub(r"[:-]", "", value.strip())
        if not re.fullmatch(r"[0-9A-Fa-f]{12}", compact):
            raise ValueError("mac_address must contain 12 hexadecimal digits")
        return ":".join(
            compact[index : index + 2].upper()
            for index in range(0, len(compact), 2)
        )

    @field_validator("image_name")
    @classmethod
    def validate_image_name(cls, value: str) -> str:
        if value is None:
            return value
        normalized = value.strip()
        if (
            "/" in normalized
            or "\\" in normalized
            or not normalized.lower().endswith(".wim")
        ):
            raise ValueError("image_name must be a .wim file name without a path")
        return normalized

    @field_validator("target_disk_model")
    @classmethod
    def validate_target_disk_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("target_disk_model must contain printable characters")
        return normalized

    @model_validator(mode="after")
    def validate_target_disk_snapshot(self) -> "DeploymentBeginRequest":
        values = (
            self.target_disk_number,
            self.target_disk_model,
            self.target_disk_size_bytes,
        )
        if any(value is not None for value in values) and any(
            value is None for value in values
        ):
            raise ValueError(
                "target disk number, model, and size must be provided together"
            )
        return self


class DeploymentImageRequest(BaseModel):
    image_name: str = Field(min_length=5, max_length=255)

    @field_validator("image_name")
    @classmethod
    def validate_image_name(cls, value: str) -> str:
        return DeploymentBeginRequest.validate_image_name(value)


class DeploymentManifestRequest(BaseModel):
    image_name: str = Field(min_length=1, max_length=255)
    program_names: list[str] = Field(default_factory=list, max_length=500)
    driver_package: str | None = Field(default=None, min_length=3, max_length=511)

    @field_validator("program_names")
    @classmethod
    def validate_program_names(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 255 for value in values):
            raise ValueError("program_names contains an invalid name")
        if len({value.casefold() for value in values}) != len(values):
            raise ValueError("program_names contains duplicates")
        return values

    @field_validator("driver_package")
    @classmethod
    def validate_driver_package(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = value.split("\\")
        if (
            value != value.strip()
            or value.count("\\") != 1
            or any(
                not part
                or len(part) > 255
                or part in {".", ".."}
                or part[-1] in {".", " "}
                or any(
                    ord(character) < 32 or character in '<>:"/|?*'
                    for character in part
                )
                for part in parts
            )
        ):
            raise ValueError("driver_package must be a vendor\\model path")
        return value


class DeploymentErrorRequest(BaseModel):
    stage: WinPEStageCode | None = None
    message: str = Field(min_length=1, max_length=4000)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message cannot be empty")
        return normalized


class DeploymentResponse(BaseModel):
    deployment_id: int
    status: Literal["begin", "completed", "failed"]
    started_at: datetime
    completed_at: datetime | None


class DeploymentProgramReport(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    status: Literal["installed", "failed", "timed_out"]
    exit_code: int | None = None
    duration_seconds: int = Field(ge=0, le=24 * 60 * 60)
    reason: Literal["hash_mismatch"] | None = None
    error_message: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_reason(self) -> "DeploymentProgramReport":
        if self.reason is not None and self.status != "failed":
            raise ValueError("A program failure reason requires status failed")
        return self


class DeploymentCompleteRequest(BaseModel):
    programs: list[DeploymentProgramReport] = Field(default_factory=list, max_length=500)


class DeploymentProgramResponse(BaseModel):
    name: str
    status: Literal["installed", "failed", "timed_out"]
    exit_code: int | None
    duration_seconds: int
    reason: Literal["hash_mismatch"] | None
    error_message: str | None


class DeploymentStageResponse(BaseModel):
    stage: WinPEStageCode
    phase: Literal["winpe"]
    status: DeploymentStageStatus
    started_at: datetime
    completed_at: datetime | None
    error_message: str | None


class NetworkAdapterReport(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=512)
    adapter_id: str | None = Field(default=None, max_length=255)
    local_ip: str | None = Field(default=None, max_length=45)
    link_speed_bps: int | None = Field(default=None, ge=0)


class NetworkAggregateReport(BaseModel):
    started_at: datetime
    completed_at: datetime
    duration_seconds: float = Field(ge=0)
    icmp_status: Literal["available", "unavailable", "not_measured"]
    ping_sent: int = Field(ge=0)
    ping_received: int = Field(ge=0)
    ping_lost: int = Field(ge=0)
    loss_percentage: float | None = Field(default=None, ge=0, le=100)
    rtt_min_ms: float | None = Field(default=None, ge=0)
    rtt_avg_ms: float | None = Field(default=None, ge=0)
    rtt_max_ms: float | None = Field(default=None, ge=0)
    latency_spikes: int = Field(ge=0)
    bytes_received: int | None = Field(default=None, ge=0)
    average_inbound_mbps: float | None = Field(default=None, ge=0)
    link_utilization_percent: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_ping_counts(self) -> "NetworkAggregateReport":
        if self.ping_received + self.ping_lost != self.ping_sent:
            raise ValueError("ping_received + ping_lost must equal ping_sent")
        if self.icmp_status == "available" and self.ping_received == 0:
            raise ValueError("available ICMP requires at least one reply")
        if self.icmp_status != "available" and self.loss_percentage is not None:
            raise ValueError("loss_percentage is only valid when ICMP is available")
        return self


class NetworkStageReport(NetworkAggregateReport):
    stage: Literal[
        "image_download", "image_apply", "driver_injection", "postinstall_copy"
    ]


class NetworkApiReport(BaseModel):
    request_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    min_ms: float | None = Field(default=None, ge=0)
    avg_ms: float | None = Field(default=None, ge=0)
    max_ms: float | None = Field(default=None, ge=0)


class NetworkSmbReport(BaseModel):
    success: bool | None = None
    attempts: int = Field(ge=0)
    duration_ms: float | None = Field(default=None, ge=0)
    error_message: str | None = Field(default=None, max_length=4000)


class DeploymentNetworkDiagnosticsRequest(BaseModel):
    ping_target: str = Field(min_length=1, max_length=255)
    smb_adapter: NetworkAdapterReport | None = None
    api_adapter: NetworkAdapterReport | None = None
    adapters_differ: bool = False
    overall: NetworkAggregateReport
    stages: list[NetworkStageReport] = Field(default_factory=list, max_length=4)
    api: NetworkApiReport
    smb: NetworkSmbReport
    diagnostic_errors: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("diagnostic_errors")
    @classmethod
    def validate_diagnostic_errors(cls, values: list[str]) -> list[str]:
        return [value[:1000] for value in values if value.strip()]

    @model_validator(mode="after")
    def validate_unique_stages(self) -> "DeploymentNetworkDiagnosticsRequest":
        stages = [stage.stage for stage in self.stages]
        if len(stages) != len(set(stages)):
            raise ValueError("network diagnostic stages must be unique")
        return self


class DeploymentNetworkDiagnosticsResponse(BaseModel):
    """A complete final report or partial reports from completed stages."""

    ping_target: str | None = Field(default=None, max_length=255)
    smb_adapter: NetworkAdapterReport | None = None
    api_adapter: NetworkAdapterReport | None = None
    adapters_differ: bool | None = None
    overall: NetworkAggregateReport | None = None
    stages: list[NetworkStageReport] = Field(default_factory=list, max_length=4)
    api: NetworkApiReport | None = None
    smb: NetworkSmbReport | None = None
    diagnostic_errors: list[str] = Field(default_factory=list, max_length=100)


class DomainJoinProvisionResponse(BaseModel):
    computer_name: str
    status: Literal["ready"]
    blob_url: str


class DomainJoinAcknowledgeResponse(BaseModel):
    computer_name: str
    status: Literal["deleted", "already_deleted"]


class DeploymentListItem(BaseModel):
    deployment_id: int
    computer_name: str
    serial_number: str | None
    model: str | None
    manufacturer: str | None
    system_sku: str | None
    mac_address: str
    ip_address: str
    image_name: str | None
    imageApplyMode: Literal["direct", "staged"] | None
    target_disk_number: int | None
    target_disk_model: str | None
    target_disk_size_bytes: int | None
    domain_join: bool
    status: Literal["begin", "completed", "failed"]
    started_at: datetime
    completed_at: datetime | None
    last_error_message: str | None
    stages: list[DeploymentStageResponse]
    programs: list[DeploymentProgramResponse]
    network_diagnostics: DeploymentNetworkDiagnosticsResponse | None = None


class DeploymentListResponse(BaseModel):
    total: int
    begin: int
    completed: int
    failed: int
    items: list[DeploymentListItem]


class ComputerListItem(BaseModel):
    computer_id: int
    serial_number: str | None
    mac_address: str | None
    last_model: str | None
    last_computer_name: str
    last_ip_address: str | None
    last_image_name: str | None
    last_domain_join: bool
    deployment_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    last_deployment_id: int | None


class ComputerListResponse(BaseModel):
    total: int
    items: list[ComputerListItem]


class KnownComputerName(BaseModel):
    computer_name: str
    matched_by: Literal["serial_number", "mac_address"]
    last_deployed_at: datetime
    deployment_id: int


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def expire_stale_deployments(
    session: Session,
    now: datetime | None = None,
) -> int:
    current_time = as_utc(now) if now is not None else datetime.now(timezone.utc)
    deployment_timeout = timedelta(
        minutes=get_settings().deployment_timeout_minutes
    )
    timeout_minutes = int(deployment_timeout.total_seconds() // 60)
    timeout_message = f"Deployment timed out after {timeout_minutes} minutes."
    timeout_boundary = current_time - deployment_timeout
    stale_deployments = session.scalars(
        select(Deployment).where(
            Deployment.status == DEPLOYMENT_BEGIN,
            Deployment.started_at <= timeout_boundary,
        )
    ).all()

    for deployment in stale_deployments:
        # Imported lazily to avoid the auth/Base import cycle at module load.
        from app.auth import DeploymentToken

        if deployment.domain_join:
            discard_domain_join_blob(deployment.computer_name)

        failed_at = as_utc(deployment.started_at) + deployment_timeout
        deployment.status = DEPLOYMENT_FAILED
        deployment.completed_at = failed_at
        deployment.last_error_message = timeout_message
        session.execute(
            update(DeploymentStage)
            .where(
                DeploymentStage.deployment_id == deployment.id,
                DeploymentStage.status == STAGE_RUNNING,
            )
            .values(
                status=STAGE_FAILED,
                completed_at=failed_at,
                error_message=timeout_message,
            )
        )
        session.execute(
            update(DeploymentToken)
            .where(
                DeploymentToken.deployment_id == deployment.id,
                DeploymentToken.revoked_at.is_(None),
            )
            .values(revoked_at=failed_at)
        )

    if stale_deployments:
        session.commit()

    # Housekeeping hook: this runs on nearly every deployment request, which is
    # the only scheduler IronAPI has. It catches blobs whose deployment row was
    # already closed, or was never closed at all.
    purge_orphaned_domain_join_blobs()

    return len(stale_deployments)


def discard_domain_join_blob(computer_name: str) -> None:
    """Best-effort deletion of a computer-account secret we no longer need.

    Never raises: blob cleanup must not block a deployment from being closed.
    """
    from app.domain_join import DomainJoinError, delete_domain_join_blob

    try:
        delete_domain_join_blob(get_settings(), computer_name)
    except DomainJoinError as exc:
        logger.error(
            "Failed to delete the ODJ blob of %s: %s",
            computer_name,
            exc,
        )


def purge_orphaned_domain_join_blobs() -> None:
    from app.domain_join import purge_stale_domain_join_blobs

    try:
        purge_stale_domain_join_blobs(get_settings(), throttle=True)
    except Exception as exc:  # noqa: BLE001 - housekeeping must never break a request
        logger.error("ODJ blob purge failed: %s", exc)


def to_deployment_response(deployment: Deployment) -> DeploymentResponse:
    return DeploymentResponse(
        deployment_id=deployment.id,
        status=deployment.status,
        started_at=as_utc(deployment.started_at),
        completed_at=as_utc(deployment.completed_at),
    )


def to_deployment_stage_response(
    stage: DeploymentStage,
) -> DeploymentStageResponse:
    return DeploymentStageResponse(
        stage=stage.stage,
        phase=stage.phase,
        status=stage.status,
        started_at=as_utc(stage.started_at),
        completed_at=as_utc(stage.completed_at),
        error_message=stage.error_message,
    )


def _network_adapter_response(
    summary: DeploymentNetworkSummary,
    prefix: Literal["smb", "api"],
) -> NetworkAdapterReport | None:
    values = {
        "name": getattr(summary, f"{prefix}_adapter_name"),
        "description": getattr(summary, f"{prefix}_adapter_description"),
        "adapter_id": getattr(summary, f"{prefix}_adapter_id"),
        "local_ip": getattr(summary, f"{prefix}_local_ip"),
        "link_speed_bps": getattr(summary, f"{prefix}_link_speed_bps"),
    }
    if all(value is None for value in values.values()):
        return None
    return NetworkAdapterReport(**values)


def _network_aggregate_response(
    record: DeploymentNetworkSummary | DeploymentNetworkStage,
) -> NetworkAggregateReport:
    return NetworkAggregateReport(
        started_at=as_utc(record.started_at),
        completed_at=as_utc(record.completed_at),
        duration_seconds=record.duration_seconds,
        icmp_status=record.icmp_status,
        ping_sent=record.ping_sent,
        ping_received=record.ping_received,
        ping_lost=record.ping_lost,
        loss_percentage=record.loss_percentage,
        rtt_min_ms=record.rtt_min_ms,
        rtt_avg_ms=record.rtt_avg_ms,
        rtt_max_ms=record.rtt_max_ms,
        latency_spikes=record.latency_spikes,
        bytes_received=record.bytes_received,
        average_inbound_mbps=record.average_inbound_mbps,
        link_utilization_percent=record.link_utilization_percent,
    )


def to_network_stage_response(
    stage: DeploymentNetworkStage,
) -> NetworkStageReport:
    return NetworkStageReport(
        stage=stage.stage,
        **_network_aggregate_response(stage).model_dump(),
    )


def to_network_diagnostics_response(
    summary: DeploymentNetworkSummary | None,
    stages: list[DeploymentNetworkStage] | None = None,
) -> DeploymentNetworkDiagnosticsResponse | None:
    stage_reports = [
        to_network_stage_response(stage)
        for stage in (stages or [])
    ]
    if summary is None:
        if not stage_reports:
            return None
        return DeploymentNetworkDiagnosticsResponse(stages=stage_reports)
    return DeploymentNetworkDiagnosticsResponse(
        ping_target=summary.ping_target,
        smb_adapter=_network_adapter_response(summary, "smb"),
        api_adapter=_network_adapter_response(summary, "api"),
        adapters_differ=summary.adapters_differ,
        overall=_network_aggregate_response(summary),
        stages=stage_reports,
        api=NetworkApiReport(
            request_count=summary.api_request_count,
            error_count=summary.api_error_count,
            min_ms=summary.api_min_ms,
            avg_ms=summary.api_avg_ms,
            max_ms=summary.api_max_ms,
        ),
        smb=NetworkSmbReport(
            success=summary.smb_connect_success,
            attempts=summary.smb_connect_attempts,
            duration_ms=summary.smb_connect_duration_ms,
            error_message=summary.smb_error_message,
        ),
        diagnostic_errors=list(summary.diagnostic_errors or []),
    )


def to_deployment_list_item(
    deployment: Deployment,
    stages: list[DeploymentStage] | None = None,
    programs: list[DeploymentProgram] | None = None,
    network_summary: DeploymentNetworkSummary | None = None,
    network_stages: list[DeploymentNetworkStage] | None = None,
) -> DeploymentListItem:
    return DeploymentListItem(
        deployment_id=deployment.id,
        computer_name=deployment.computer_name,
        serial_number=deployment.serial_number,
        model=deployment.model,
        manufacturer=deployment.manufacturer,
        system_sku=deployment.system_sku,
        mac_address=deployment.mac_address,
        ip_address=deployment.ip_address,
        image_name=deployment.image_name,
        imageApplyMode=deployment.image_apply_mode,
        target_disk_number=deployment.target_disk_number,
        target_disk_model=deployment.target_disk_model,
        target_disk_size_bytes=deployment.target_disk_size_bytes,
        domain_join=deployment.domain_join,
        status=deployment.status,
        started_at=as_utc(deployment.started_at),
        completed_at=as_utc(deployment.completed_at),
        last_error_message=deployment.last_error_message,
        stages=[
            to_deployment_stage_response(stage)
            for stage in (stages or [])
        ],
        programs=[
            DeploymentProgramResponse(
                name=program.name,
                status=program.status,
                exit_code=program.exit_code,
                duration_seconds=program.duration_seconds,
                reason=program.reason,
                error_message=program.error_message,
            )
            for program in (programs or [])
        ],
        network_diagnostics=to_network_diagnostics_response(
            network_summary,
            network_stages,
        ),
    )


def to_computer_list_item(computer: Computer) -> ComputerListItem:
    return ComputerListItem(
        computer_id=computer.id,
        serial_number=computer.serial_number,
        mac_address=computer.mac_address,
        last_model=computer.last_model,
        last_computer_name=computer.last_computer_name,
        last_ip_address=computer.last_ip_address,
        last_image_name=computer.last_image_name,
        last_domain_join=computer.last_domain_join,
        deployment_count=computer.deployment_count,
        first_seen_at=as_utc(computer.first_seen_at),
        last_seen_at=as_utc(computer.last_seen_at),
        last_deployment_id=computer.last_deployment_id,
    )


def update_computer_inventory(
    session: Session,
    deployment: Deployment,
    observed_at: datetime | None = None,
) -> Computer:
    current_time = as_utc(observed_at) if observed_at else datetime.now(timezone.utc)
    computer = None
    if deployment.serial_number:
        computer = session.scalar(
            select(Computer).where(
                Computer.serial_number == deployment.serial_number,
            )
        )
    if computer is None and deployment.serial_number:
        computer = session.scalar(
            select(Computer).where(
                Computer.serial_number.is_(None),
                Computer.mac_address == deployment.mac_address,
            )
        )
    if computer is None and not deployment.serial_number:
        computer = session.scalar(
            select(Computer)
            .where(Computer.mac_address == deployment.mac_address)
            .order_by(
                Computer.serial_number.is_(None),
                Computer.last_seen_at.desc(),
            )
        )

    if computer is None:
        computer = Computer(
            serial_number=deployment.serial_number,
            mac_address=deployment.mac_address,
            last_model=deployment.model,
            last_computer_name=deployment.computer_name,
            last_ip_address=deployment.ip_address,
            last_image_name=deployment.image_name,
            last_domain_join=deployment.domain_join,
            deployment_count=0,
            first_seen_at=current_time,
            last_seen_at=current_time,
            last_deployment_id=deployment.id,
        )
        session.add(computer)

    # A transient missing serial must not erase a previously known stable one.
    computer.serial_number = deployment.serial_number or computer.serial_number
    computer.mac_address = deployment.mac_address
    # A WinPE image that does not report a model must not erase a known one.
    computer.last_model = deployment.model or computer.last_model
    computer.last_computer_name = deployment.computer_name
    computer.last_ip_address = deployment.ip_address
    computer.last_image_name = deployment.image_name
    computer.last_domain_join = deployment.domain_join
    computer.last_seen_at = current_time
    computer.last_deployment_id = deployment.id

    if computer.serial_number:
        count_filter = or_(
            Deployment.serial_number == computer.serial_number,
            and_(
                Deployment.serial_number.is_(None),
                Deployment.mac_address == computer.mac_address,
            ),
        )
    else:
        count_filter = Deployment.mac_address == computer.mac_address
    deployment_count = session.scalar(
        select(func.count(Deployment.id)).where(count_filter)
    )
    computer.deployment_count = int(deployment_count or 0)
    return computer


def find_known_computer_names(
    session: Session,
    serial_number: str | None,
    mac_address: str | None,
    limit: int = 10,
) -> list[KnownComputerName]:
    matches: dict[tuple[str, str], KnownComputerName] = {}
    if serial_number:
        rows = session.scalars(
            select(Deployment)
            .where(Deployment.serial_number == serial_number)
            .order_by(Deployment.started_at.desc())
            .limit(limit)
        ).all()
        for deployment in rows:
            key = ("serial_number", deployment.computer_name)
            matches.setdefault(
                key,
                KnownComputerName(
                    computer_name=deployment.computer_name,
                    matched_by="serial_number",
                    last_deployed_at=as_utc(deployment.started_at),
                    deployment_id=deployment.id,
                ),
            )

    if mac_address:
        rows = session.scalars(
            select(Deployment)
            .where(Deployment.mac_address == mac_address)
            .order_by(Deployment.started_at.desc())
            .limit(limit)
        ).all()
        for deployment in rows:
            key = ("mac_address", deployment.computer_name)
            if key in matches:
                continue
            matches[key] = KnownComputerName(
                computer_name=deployment.computer_name,
                matched_by="mac_address",
                last_deployed_at=as_utc(deployment.started_at),
                deployment_id=deployment.id,
            )

    return sorted(
        matches.values(),
        key=lambda item: item.last_deployed_at,
        reverse=True,
    )[:limit]
