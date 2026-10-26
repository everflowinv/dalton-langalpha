"""Request and response models for the Computer management API.

A computer is one isolated execution environment addressed on its own, so the
lifecycle fields a workspace used to carry (status, resource tier, always-on)
are read and written here. ``provider_ref`` is the vendor's id for the machine
and is only ever handed to the owner.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.server.services.persistence.sync_result import UnsavedReason


class ComputerStatus(StrEnum):
    """Computer lifecycle states: the workspace set minus ``unbound``.

    StrEnum, not ``(str, Enum)``: a member of the latter renders as
    ``ComputerStatus.RUNNING`` in an f-string while comparing equal to
    ``"running"``, so every log line and message built from one had to route
    around it. Here a member is its value everywhere a string is wanted.
    """

    CREATING = "creating"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"
    DELETED = "deleted"


# A machine is claimable for start only from a settled down state. ``creating``
# is one: 046 copies a workspace's status onto the machine it backfills, so a
# create that bound a sandbox and then crashed leaves a startable machine
# sitting at ``creating``.
CLAIMABLE_FOR_START = (ComputerStatus.STOPPED, ComputerStatus.CREATING)


ComputerKind = Literal["daytona", "docker", "e2b"]


class ComputerCreate(BaseModel):
    """Request model for creating a computer."""

    name: Optional[str] = Field(
        None,
        min_length=1,
        max_length=255,
        description="User-facing computer name; a default is used when absent",
    )
    resource_tier: Literal["standard", "performance", "max"] = Field(
        "standard",
        description="Spec preset the computer is created at",
    )
    kind: Optional[ComputerKind] = Field(
        None,
        description=(
            "Sandbox provider the computer runs on, for good; the deployment "
            "default when absent. Must be one GET /computers/providers lists."
        ),
    )


class ComputerProviderOption(BaseModel):
    """One sandbox provider a new computer may be created on."""

    kind: ComputerKind = Field(description="Sandbox provider")
    is_default: bool = Field(
        description="Whether a computer created without a kind runs here"
    )
    allowed: Optional[bool] = Field(
        description=(
            "Whether the caller may create a computer here; null when it is "
            "not gated or the answer is unknown, and the create is then allowed"
        )
    )


class ComputerProvidersResponse(BaseModel):
    """The sandbox providers a new computer may be created on."""

    providers: List[ComputerProviderOption] = Field(
        default_factory=list, description="The default provider first"
    )


class ComputerSpecRequest(BaseModel):
    """Request model for changing a computer's spec tier."""

    tier: Literal["standard", "performance", "max"] = Field(
        description="Target spec preset",
    )


class ComputerAlwaysOnRequest(BaseModel):
    """Request model for toggling a computer's always-on flag."""

    enabled: bool = Field(description="Whether to keep the computer always-on")


class ComputerRenameRequest(BaseModel):
    """Request model for renaming a computer."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(
        min_length=1, max_length=255, description="New user-facing computer name"
    )


DiskLevel = Literal["healthy", "notice", "warning", "critical"]


class ComputerDisk(BaseModel):
    """The last disk reading taken on a computer, shared by every workspace on it."""

    used_bytes: int = Field(description="Bytes in use on the computer's disk")
    total_bytes: int = Field(description="Size of the computer's disk in bytes")
    free_bytes: int = Field(description="Bytes still free on the computer's disk")
    measured_at: datetime = Field(description="When the reading was taken")
    level: DiskLevel = Field(
        description="How close the disk is to full, from free space alone"
    )


SpecChangeState = Literal["in_progress", "succeeded", "failed"]

# Why an accepted change failed, read back from the row.
SpecChangeErrorCode = Literal[
    "turn_active",
    "backup_incomplete",
    "busy",
    "interrupted",
    "disk_too_small",
    "not_allowed",
    "unknown",
]

# Why POST /computers/{id}/spec refused to accept a change (409).
SpecRefusalCode = Literal[
    "turn_active", "spec_in_progress", "busy", "backup_incomplete"
]


class UnsavedFileOut(BaseModel):
    """A file a strict backup could not save, so its only copy is on the machine."""

    path: str
    reason: UnsavedReason
    size: Optional[int] = None


class SpecChangeError(BaseModel):
    """Why a spec change failed, in a shape a client can map to copy."""

    code: SpecChangeErrorCode
    message: str = Field(description="A sentence the user can be shown as is")
    files: List[UnsavedFileOut] = Field(
        default_factory=list,
        description="For backup_incomplete: the files the backup could not save",
    )


class SpecChangeRefusal(BaseModel):
    """The 409 detail of a spec change refused before it was accepted.

    Same shape as :class:`SpecChangeError`, so a client renders a refusal and
    a failed outcome with one piece of code.
    """

    code: SpecRefusalCode
    message: str
    files: List[UnsavedFileOut] = Field(default_factory=list)


class ComputerSpecChange(BaseModel):
    """The last spec change requested on a computer and how it went.

    The change runs after the request that asked for it has returned, so this
    is how a client learns the outcome. Stored as this shape in
    ``computers.spec_change``.
    """

    target_tier: str = Field(description="Tier the change moves to")
    from_tier: str = Field(description="Tier the computer was at when it began")
    state: SpecChangeState
    error: Optional[SpecChangeError] = None
    started_at: datetime
    heartbeat_at: Optional[datetime] = Field(
        None,
        description=(
            "When the worker running this change last reported in. Null on "
            "changes recorded before it existed."
        ),
    )
    finished_at: Optional[datetime] = None
    claim_id: Optional[str] = Field(
        None,
        description=(
            "Identifies this change; a new request gets a new one. Null on "
            "changes recorded before it existed."
        ),
    )
    took_over: bool = Field(
        False,
        description=(
            "This change took over one whose worker stopped reporting, so the "
            "machine's real size is unknown and it is rebuilt even at the "
            "tier the row reads."
        ),
    )


class ComputerResponse(BaseModel):
    """Response model for computer details."""

    computer_id: str = Field(description="Unique computer identifier")
    user_id: str = Field(description="Owner user ID")
    kind: str = Field(description="Execution backend: daytona, docker, e2b")
    name: str = Field(description="User-facing computer name")
    status: str = Field(
        description=(
            "Computer status: creating, starting, running, stopping, stopped, "
            "error, deleted"
        )
    )
    resource_tier: str = Field(
        "standard",
        description="Spec preset: standard, performance, max",
    )
    is_always_on: bool = Field(
        False,
        description="Whether auto-stop is disabled (always-on computer)",
    )
    is_primary: bool = Field(
        False,
        description="Whether this is the user's default computer",
    )
    root_dir: str = Field(description="Filesystem root of the computer's home")
    workspace_count: int = Field(
        0,
        description="Live projects on this computer, which its stop takes down together",
    )
    layout_version: Optional[int] = Field(
        None,
        description=(
            "Filesystem layout the machine last reported. Null until one has "
            "been observed, which is not the same as version zero."
        ),
    )
    provider_ref: Optional[str] = Field(
        None,
        description=(
            "Vendor identifier for the running machine. Present only for the "
            "owner; null when the computer has never been provisioned."
        ),
    )
    created_at: datetime = Field(description="Creation timestamp")
    updated_at: datetime = Field(description="Last update timestamp")
    last_activity_at: Optional[datetime] = Field(
        None,
        description="Last agent activity timestamp",
    )
    stopped_at: Optional[datetime] = Field(
        None,
        description="When the computer was stopped (if status=stopped)",
    )
    config: Optional[Dict[str, Any]] = Field(
        None,
        description="Configuration settings",
    )
    disk: Optional[ComputerDisk] = Field(
        None,
        description=(
            "Last disk reading. Null when never measured, or when the disk has "
            "no size of its own (a local computer without a storage quota)."
        ),
    )
    spec_change: Optional[ComputerSpecChange] = Field(
        None,
        description="The last spec change and its outcome; null when none was requested",
    )

    model_config = ConfigDict(from_attributes=True)


class WorkspaceStorage(BaseModel):
    """One workspace folder's share of its computer's disk."""

    workspace_id: str
    name: str
    dir_name: Optional[str] = None
    bytes: int


class ComputerStorageResponse(BaseModel):
    """A computer's disk with each workspace folder's size."""

    disk: Optional[ComputerDisk] = None
    workspaces: List[WorkspaceStorage] = Field(
        default_factory=list,
        description="Workspace folders by size, largest first",
    )
    other_bytes: int = Field(
        0,
        description="Used bytes outside any workspace folder (packages, caches)",
    )
    live: bool = Field(
        False,
        description=(
            "Whether this was measured on the running machine (now, or within "
            "the last minute) rather than read from the stored reading"
        ),
    )


class ComputerListResponse(BaseModel):
    """Response model for a user's computer list."""

    computers: List[ComputerResponse] = Field(
        default_factory=list,
        description="The user's computers, primary first",
    )
    total: int = Field(0, description="Total number of computers")


class ComputerActionResponse(BaseModel):
    """Response model for computer actions (start, stop, archive)."""

    computer_id: str = Field(description="Computer identifier")
    status: str = Field(description="New computer status")
    message: str = Field(description="Action result message")


class ComputerSessionResponse(BaseModel):
    """What a computer is, plus what the answering worker holds for it.

    ``status`` and ``provider_ref`` are the cross-worker truth from Postgres.
    ``ready`` and ``session_provider_ref`` describe only the worker that served
    this request, because a runtime session is process-local execution context;
    a client must never read them as liveness.
    """

    computer_id: str = Field(description="Computer identifier")
    status: str = Field(description="Computer status, from Postgres")
    provider_ref: Optional[str] = Field(
        None, description="Vendor identifier for the machine, from Postgres"
    )
    ready: bool = Field(
        False,
        description="This worker holds a session with a runtime attached",
    )
    session_provider_ref: Optional[str] = Field(
        None,
        description="What this worker's session is bound to, if it holds one",
    )
