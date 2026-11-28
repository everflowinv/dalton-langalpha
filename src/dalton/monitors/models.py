"""Models, constants, and validation helpers for Dalton Monitors."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MonitorCategory = Literal[
    "overview",
    "us",
    "cn",
    "hk",
    "asia",
    "stocks",
    "portfolio",
    "other",
]
MonitorVisibility = Literal["own", "members", "company", "public"]
MonitorSourceKind = Literal["workspace", "data_job"]

MONITOR_CATEGORIES: tuple[MonitorCategory, ...] = (
    "overview",
    "us",
    "cn",
    "hk",
    "asia",
    "stocks",
    "portfolio",
    "other",
)
MONITOR_VISIBILITIES: tuple[MonitorVisibility, ...] = (
    "own",
    "members",
    "company",
    "public",
)
MAX_MONITOR_VERSIONS = 100

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,118}[a-z0-9])?$")
_DATA_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# Route segments under /api/v1/dalton/monitors/* and /monitors/* that cannot be used as slugs.
RESERVED_MONITOR_SLUGS: frozenset[str] = frozenset(
    {
        "admin",
        "api",
        "by-artifact",
        "company-members",
        "data",
        "data-jobs",
        "edit",
        "export",
        "g",
        "health",
        "index",
        "internal",
        "list",
        "new",
        "private",
        "public",
        "search",
        "settings",
        "shared-documents",
        "status",
        "v",
    }
)

_HTML_ENTRY_SUFFIXES: tuple[str, ...] = (".html", ".htm")
_MARKDOWN_ENTRY_SUFFIXES: tuple[str, ...] = (".md", ".markdown")
EntryFileType = Literal["html", "markdown", "other"]


def is_html_entry_path(path: str | None) -> bool:
    """Return True when *path* points to an HTML web page (``.html`` or ``.htm``)."""
    clean = str(path or "").strip().lower()
    return bool(clean) and clean.endswith(_HTML_ENTRY_SUFFIXES)


def is_markdown_entry_path(path: str | None) -> bool:
    """Return True when *path* points to a Markdown document (``.md`` or ``.markdown``)."""
    clean = str(path or "").strip().lower()
    return bool(clean) and clean.endswith(_MARKDOWN_ENTRY_SUFFIXES)


def entry_file_type_of(path: str | None) -> EntryFileType:
    """Return a safe, path-free file type tag for viewer rendering."""
    if is_html_entry_path(path):
        return "html"
    if is_markdown_entry_path(path):
        return "markdown"
    return "other"


def is_monitor_panel_entry(monitor: MonitorRecord) -> bool:
    """Return True when *monitor* belongs on the Monitors panel (data job or HTML page)."""
    if monitor.source_kind == "data_job":
        return True
    return is_html_entry_path(monitor.entry_path)


def is_valid_slug(slug: str) -> bool:
    clean = str(slug or "").strip().lower()
    return bool(_SLUG_RE.fullmatch(clean)) and clean not in RESERVED_MONITOR_SLUGS


def is_valid_data_key(key: str) -> bool:
    return bool(_DATA_KEY_RE.fullmatch(key))


def slugify_candidate(raw: str) -> str:
    """Turn a title or filename into a valid, non-reserved URL slug candidate."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")
    if not cleaned:
        return "monitor"
    candidate = cleaned[:100].strip("-") or "monitor"
    if candidate in RESERVED_MONITOR_SLUGS:
        candidate = f"{candidate[:91]}-monitor"
    return candidate


@dataclass(frozen=True)
class MonitorRecord:
    monitor_id: str
    slug: str
    artifact_id: str
    owner_user_id: str
    title: str
    category: MonitorCategory
    sort_order: int
    visibility: MonitorVisibility
    published_seq: int
    keep_updated: bool
    source_kind: MonitorSourceKind
    source_ref: str | None
    acl_revision: int
    created_at: datetime
    updated_at: datetime
    last_publish_run_id: str | None = None
    last_publish_error: str | None = None
    last_publish_failed_at: datetime | None = None
    last_verified_seq: int | None = None
    last_verified_at: datetime | None = None
    # Joined fields from share_links / users / dalton_monitor_data
    artifact_code: str | None = None
    workspace_id: str | None = None
    entry_path: str | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    data_as_of: datetime | None = None
    member_user_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def effective_seq(self) -> int:
        # Prefer the higher of last_verified_seq and published_seq so rolling upgrades
        # (where an old worker advances published_seq after d061 backfill) never regress visitors.
        return max(int(self.last_verified_seq or 0), int(self.published_seq or 0))


@dataclass(frozen=True)
class MonitorDataEntry:
    monitor_id: str
    data_key: str
    payload: Any
    as_of: datetime | None
    sha256: str | None
    updated_at: datetime
    attempt_seq: int = 0
    is_stale: bool = False
    last_attempt_at: datetime | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class CompanyMember:
    user_id: str
    email: str | None
    name: str | None
    tier: Literal["admin", "internal"]


class PublishMonitorRequest(BaseModel):
    """Publish a workspace HTML file or folder as a monitor."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(..., min_length=1, max_length=64)
    path: str = Field(..., min_length=1, max_length=4096)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    slug: str | None = Field(default=None, min_length=1, max_length=120)
    category: MonitorCategory = "other"
    sort_order: int = Field(default=0, ge=-10000, le=10000)
    visibility: MonitorVisibility = "own"
    member_user_ids: list[str] = Field(default_factory=list, max_length=200)
    keep_updated: bool = False
    label: str | None = Field(default=None, max_length=200)
    files: list[str] | None = None
    loaded: list[str] = Field(default_factory=list)
    earlier: list[str] = Field(default_factory=list)
    page_sha256: str | None = None
    confirm_pause_token: str | None = Field(default=None, max_length=128)


class UpdateMonitorRequest(BaseModel):
    """Update monitor metadata, ACL visibility, or auto-update setting."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=300)
    category: MonitorCategory | None = None
    sort_order: int | None = Field(default=None, ge=-10000, le=10000)
    visibility: MonitorVisibility | None = None
    member_user_ids: list[str] | None = Field(default=None, max_length=200)
    keep_updated: bool | None = None


class RepublishMonitorRequest(BaseModel):
    """Optional reviewed file manifest when manually republishing a workspace monitor."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=200)
    files: list[str] | None = None
    loaded: list[str] = Field(default_factory=list)
    earlier: list[str] = Field(default_factory=list)
    page_sha256: str | None = None
    confirm_pause_token: str | None = Field(default=None, max_length=128)


class MonitorDataPutRequest(BaseModel):
    """Internal service write payload for ``dalton_monitor_data``."""

    model_config = ConfigDict(extra="forbid")

    data: Any = None
    as_of: datetime | None = None
    attempt_seq: int | None = Field(default=None, ge=0)
    is_stale: bool = False
    error: str | None = Field(default=None, max_length=2000)
    last_attempt_at: datetime | None = None


class DataJobMonitorPublishRequest(BaseModel):
    """Strict internal payload for data-job monitor republish (no arbitrary host paths)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1, max_length=128)


@dataclass(frozen=True)
class SharedDocumentRecord:
    doc_id: str
    artifact_id: str
    owner_user_id: str
    title: str
    visibility: MonitorVisibility
    published_seq: int
    keep_updated: bool
    acl_revision: int
    created_at: datetime
    updated_at: datetime
    artifact_code: str | None = None
    workspace_id: str | None = None
    entry_path: str | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    member_user_ids: tuple[str, ...] = field(default_factory=tuple)


class ShareDocumentRequest(BaseModel):
    """Share or update a non-HTML workspace file without creating a monitor entry."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(..., min_length=1, max_length=64)
    path: str = Field(..., min_length=1, max_length=4096)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    visibility: MonitorVisibility = "own"
    member_user_ids: list[str] = Field(default_factory=list, max_length=200)
    keep_updated: bool = False
    label: str | None = Field(default=None, max_length=200)
    files: list[str] | None = None
    loaded: list[str] = Field(default_factory=list)
    earlier: list[str] = Field(default_factory=list)
    page_sha256: str | None = None
    confirm_pause_token: str | None = Field(default=None, max_length=128)


class UpdateSharedDocumentRequest(BaseModel):
    """Update non-HTML shared document metadata, visibility ACL, or auto-update setting."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=300)
    visibility: MonitorVisibility | None = None
    member_user_ids: list[str] | None = Field(default=None, max_length=200)
    keep_updated: bool | None = None
