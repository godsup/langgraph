"""Shared data models.

`MigrationPlan` is the only channel between agents: each agent reads the plan,
adds its own section and hands it back to the orchestrator. It is persisted as
`runs/<run-id>/migration-plan.json` and, as JSON, in LangGraph state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict

SCHEMA_VERSION = 1


def utcnow() -> datetime:
    return datetime.now(UTC)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


class SourceCloud(StrEnum):
    AWS = "aws"
    AZURE = "azure"


class ResourceKind(StrEnum):
    """Cloud-neutral resource categories used for mapping."""

    COMPUTE_INSTANCE = "compute_instance"
    BLOCK_VOLUME = "block_volume"
    NETWORK = "network"
    SUBNET = "subnet"
    SECURITY_GROUP = "security_group"
    POSTGRES = "postgres"
    MYSQL = "mysql"
    MARIADB = "mariadb"
    OBJECT_BUCKET = "object_bucket"
    LOAD_BALANCER = "load_balancer"
    KUBERNETES_CLUSTER = "kubernetes_cluster"
    OTHER = "other"


class SourceResource(Model):
    """One discovered resource, normalised across source clouds."""

    id: str
    name: str
    kind: ResourceKind
    source_type: str  # e.g. "aws:ec2:instance"
    region: str
    tags: dict[str, str] = Field(default_factory=dict)
    properties: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class MappingStatus(StrEnum):
    MAPPED = "mapped"  # clean 1:1 mapping
    PARTIAL = "partial"  # mapping exists but needs human review
    GAP = "gap"  # no clean STACKIT equivalent


class DecidedBy(StrEnum):
    RULE = "rule"  # deterministic rule from config/mapping.yaml
    LLM = "llm"  # LLM chose between allowed candidates
    FALLBACK = "fallback"  # LLM unavailable/invalid; rule default used


class TargetMapping(Model):
    resource_id: str
    status: MappingStatus
    target_service: str | None = None  # e.g. "STACKIT PostgreSQL Flex"
    terraform_type: str | None = None  # e.g. "stackit_postgresflex_instance"
    target_spec: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    decided_by: DecidedBy = DecidedBy.RULE
    needs_review: bool = False
    role: str = ""  # LLM workload classification, e.g. "web frontend"
    criticality: str | None = None  # LLM: high | medium | low


class GapSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Gap(Model):
    resource_id: str
    reason: str
    severity: GapSeverity = GapSeverity.MEDIUM
    suggestion: str = ""
    source: Literal["rule", "llm"] = "rule"  # llm = flagged by the LLM review


class CostLine(Model):
    resource_id: str
    description: str
    source_monthly: float | None = None
    target_monthly: float | None = None
    note: str = ""


class CostEstimate(Model):
    currency: str = "EUR"
    lines: list[CostLine] = Field(default_factory=list)
    source_total: float = 0.0
    target_total: float = 0.0
    missing_prices: list[str] = Field(default_factory=list)
    disclaimer: str = ""


class DiscoveryResult(Model):
    completed_at: datetime | None = None
    account_id: str | None = None
    summary: str = ""  # LLM-written executive summary
    report_path: str | None = None
    catalog_source: str | None = None  # where STACKIT offerings came from
    notes: list[str] = Field(default_factory=list)  # assumptions and TODOs


# --------------------------------------------------------------------------
# Approvals
# --------------------------------------------------------------------------


class Gate(StrEnum):
    DEPLOYMENT = "deployment"
    CUTOVER = "cutover"


class ApprovalDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalRecord(Model):
    gate: Gate
    decision: ApprovalDecision = ApprovalDecision.PENDING
    approver: str | None = None
    comment: str = ""
    artifact_sha256: str | None = None  # hash of what was approved
    requested_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None


# --------------------------------------------------------------------------
# Deployment
# --------------------------------------------------------------------------


class DeployMode(StrEnum):
    APPLY = "apply"
    PR = "pr"


class DeploymentStatus(StrEnum):
    NOT_STARTED = "not_started"
    RENDERED = "rendered"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"
    APPLIED = "applied"
    PR_OPENED = "pr_opened"
    FAILED = "failed"


class PlannedChange(Model):
    address: str
    resource_type: str
    action: str  # create, update, delete, replace, no-op


class DeploymentResult(Model):
    status: DeploymentStatus = DeploymentStatus.NOT_STARTED
    mode: DeployMode = DeployMode.APPLY
    terraform_dir: str | None = None
    rendered_files: list[str] = Field(default_factory=list)
    plan_file: str | None = None
    plan_sha256: str | None = None
    changes: list[PlannedChange] = Field(default_factory=list)
    summary: str = ""  # LLM plain-language summary of the plan
    risks: list[str] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)  # non-sensitive tf outputs
    pr_url: str | None = None
    finished_at: datetime | None = None
    error: str | None = None


# --------------------------------------------------------------------------
# Data migration
# --------------------------------------------------------------------------


class TransferKind(StrEnum):
    OBJECT_STORAGE = "object_storage"
    POSTGRES = "postgres"


class TransferStatus(StrEnum):
    PENDING = "pending"
    DRY_RUN = "dry_run"
    COPIED = "copied"
    FAILED = "failed"
    SKIPPED = "skipped"


class TransferTask(Model):
    id: str
    kind: TransferKind
    source_resource_id: str
    source: str  # bucket name or database name, never a credential
    target: str
    status: TransferStatus = TransferStatus.PENDING
    method: str = ""  # e.g. "rclone copy", "pg_dump/pg_restore"
    stats: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class VerificationCheck(Model):
    task_id: str
    name: str  # e.g. "object_count", "sample_checksums", "row_counts"
    passed: bool
    source_value: Any = None
    target_value: Any = None
    details: str = ""


class CutoverStatus(StrEnum):
    NOT_STARTED = "not_started"
    VERIFIED = "verified"
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"
    CUT_OVER = "cut_over"
    FAILED = "failed"


class DataMigrationResult(Model):
    status: CutoverStatus = CutoverStatus.NOT_STARTED
    tasks: list[TransferTask] = Field(default_factory=list)
    checks: list[VerificationCheck] = Field(default_factory=list)
    summary: str = ""
    report_path: str | None = None
    report_sha256: str | None = None
    runbook_path: str | None = None
    finished_at: datetime | None = None


# --------------------------------------------------------------------------
# Monitoring
# --------------------------------------------------------------------------


class Anomaly(Model):
    rule: str
    severity: str
    query: str
    value: float | None
    threshold: float | None
    labels: dict[str, str] = Field(default_factory=dict)
    detected_at: datetime = Field(default_factory=utcnow)
    triage: str = ""  # LLM summary with proposed fix (never auto-applied)


class MonitoringResult(Model):
    polls: int = 0
    last_poll_at: datetime | None = None
    anomalies: list[Anomaly] = Field(default_factory=list)
    report_path: str | None = None


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


class Stage(StrEnum):
    DISCOVERY = "discovery"
    DEPLOYMENT = "deployment"
    DATA_MIGRATION = "data_migration"
    MONITORING = "monitoring"


class MigrationPlan(Model):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    source_cloud: SourceCloud
    source_region: str
    target_region: str = "eu01"
    mock: bool = False

    resources: list[SourceResource] = Field(default_factory=list)
    mappings: list[TargetMapping] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    cost: CostEstimate = Field(default_factory=CostEstimate)
    discovery: DiscoveryResult = Field(default_factory=DiscoveryResult)
    deployment: DeploymentResult = Field(default_factory=DeploymentResult)
    data_migration: DataMigrationResult = Field(default_factory=DataMigrationResult)
    monitoring: MonitoringResult = Field(default_factory=MonitoringResult)
    approvals: list[ApprovalRecord] = Field(default_factory=list)

    # Why the last invocation stopped early (dry run, rejection, failure).
    halt_reason: str | None = None

    def resource(self, resource_id: str) -> SourceResource:
        for res in self.resources:
            if res.id == resource_id:
                return res
        raise KeyError(resource_id)

    def mapping_for(self, resource_id: str) -> TargetMapping | None:
        return next((m for m in self.mappings if m.resource_id == resource_id), None)

    def latest_approval(self, gate: Gate) -> ApprovalRecord | None:
        records = [a for a in self.approvals if a.gate == gate]
        return records[-1] if records else None

    def touch(self) -> None:
        self.updated_at = utcnow()


# --------------------------------------------------------------------------
# LangGraph state
# --------------------------------------------------------------------------

Goal = Literal["discover", "plan", "deploy", "data", "monitor", "all"]


class RunOptions(Model):
    """Per-invocation options. Not part of the plan; chosen on the CLI."""

    execute: bool = False  # mutations need this flag *and* an approval
    deploy_mode: DeployMode | None = None
    live_llm: bool = False


VISITED_RESET = "__reset__"


def merge_visited(old: list[str] | None, new: list[str] | None) -> list[str]:
    """Reducer: stages append themselves; `[VISITED_RESET]` starts a new invocation."""
    if new is None:
        return list(old or [])
    if new and new[0] == VISITED_RESET:
        return list(new[1:])
    merged = list(old or [])
    merged.extend(stage for stage in new if stage not in merged)
    return merged


class MigrationState(TypedDict, total=False):
    plan: dict[str, Any]  # MigrationPlan as JSON (keeps checkpoints portable)
    goal: Goal
    options: dict[str, Any]  # RunOptions as JSON
    visited: Annotated[list[str], merge_visited]  # stages run in this invocation


def load_plan(state: MigrationState) -> MigrationPlan:
    return MigrationPlan.model_validate(state["plan"])


def load_options(state: MigrationState) -> RunOptions:
    return RunOptions.model_validate(state.get("options") or {})


def dump_plan(plan: MigrationPlan) -> dict[str, Any]:
    plan.touch()
    return plan.model_dump(mode="json")
