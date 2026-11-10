"""Deterministic data-task report delivery plans, T27 snapshot capture, and safe artifact reads.

Constructs validated, secret-redacted delivery plans from persisted run artifacts under
``.dalton-runs/<run_id>/`` without loading OpenClaw configuration or invoking external tools.
"""

from __future__ import annotations

from collections.abc import Mapping
import copy
from dataclasses import dataclass, field
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any
from uuid import UUID

from src.dalton.data_jobs.runner_wrapper import SecretRedactor

MAX_NOTIFICATION_ARTIFACT_BYTES = 20 * 1024 * 1024  # 20 MiB hard ceiling

T27_JOB_KEY = "asia-leverage-crossview-daily"
T26_JOB_KEY = "us-topbottom-dm-v23"

T27_EXPECTED_CHARTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("cn", "cn_latest.png", ("中国", "a股")),
    ("kr", "kr_latest.png", ("韩国", "kospi")),
    ("jp", "jp_latest.png", ("日本", "日经")),
    ("tw", "tw_latest.png", ("台湾", "加权")),
)

CONTAINER_WORKSPACE_PREFIX = "/Users/everflow/.openclaw/workspace"

ARTIFACT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_FILENAME_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
MARKDOWN_LOCAL_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\(\s*(?!https?://)([^)\s]+)\s*\)",
    re.IGNORECASE,
)

ALLOWED_IMAGE_EXTENSIONS: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

CARD_MARKDOWN_MAX_CHARS = 2000
IMG_LINE_RE = re.compile(r"^IMG:(\S+)\s*$")

NOTATION_SECTION_RE = re.compile(
    r"^(?:📝\s*)?(?:来源|信源|数据源|口径说明|口径限制|信源与数据口径|说明|备注|免责声明)$"
)
COLLAPSIBLE_SUFFIX_RE = re.compile(r"[\(（](?:折叠|点击展开)[\)）]$")


class DataTaskNotificationError(ValueError):
    """Base error for deterministic data-task report preparation or artifact validation."""

    def __init__(self, message: str, *, code: str = "invalid_notification_payload") -> None:
        super().__init__(message)
        self.code = code


class DataTaskNotificationPrepError(DataTaskNotificationError):
    """Raised when task output cannot be finalized into a valid report snapshot."""

    def __init__(self, message: str, *, code: str = "report_preparation_failed") -> None:
        super().__init__(message, code=code)


class DataTaskArtifactError(DataTaskNotificationError):
    """Raised when a report artifact fails containment, symlink, type, size, or digest checks."""

    def __init__(self, message: str, *, code: str = "artifact_invalid") -> None:
        super().__init__(message, code=code)


class DataTaskArtifactNotFoundError(DataTaskArtifactError):
    """Raised when an artifact identifier is absent from the run's delivery plan."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="artifact_not_found")


@dataclass(frozen=True)
class PlanArtifactDescriptor:
    """Wire-safe immutable artifact metadata included in an authorized delivery plan."""

    artifact_id: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    placeholder: str | None = None

    def to_wire_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "artifact_id": self.artifact_id,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }
        if self.placeholder is not None:
            out["placeholder"] = self.placeholder
        return out


@dataclass(frozen=True)
class InternalArtifactRef:
    """Backend-internal reference binding an opaque ``artifact_id`` to a run-relative file."""

    artifact_id: str
    rel_path: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ValidatedArtifactBytes:
    """Bounded, digest-verified bytes served by the internal artifact endpoint."""

    artifact_id: str
    content: bytes
    mime_type: str
    filename: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class DataTaskDeliveryItem:
    """Single ordered message/card item in a data-task delivery plan."""

    item_index: int
    kind: str  # "feishu_card" | "text_and_file" | "text"
    text: str = ""
    card: dict[str, Any] | None = None
    images: tuple[PlanArtifactDescriptor, ...] = ()
    artifact: PlanArtifactDescriptor | None = None

    def to_wire_dict(self, *, delivery_key: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "item_index": self.item_index,
            "delivery_key": delivery_key,
            "kind": self.kind,
        }
        if self.kind == "feishu_card":
            payload["card"] = copy.deepcopy(self.card or {})
            payload["images"] = [img.to_wire_dict() for img in self.images]
            if self.text:
                payload["text"] = self.text
        elif self.kind == "text_and_file":
            payload["text"] = self.text
            if self.artifact is None:
                raise DataTaskArtifactError("text_and_file item requires an artifact")
            payload["artifact"] = self.artifact.to_wire_dict()
        else:
            payload["text"] = self.text
        return payload


@dataclass(frozen=True)
class ResolvedDataTaskPlan:
    """Validated delivery plan and internal artifact table for one completed run."""

    run_id: str
    job_key: str
    items: tuple[DataTaskDeliveryItem, ...]
    artifacts_by_id: dict[str, InternalArtifactRef] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure Card Schema 2.0 rendering (extracted from feishu_send_card.py, no I/O)
# ---------------------------------------------------------------------------


def auto_infer_tag(title: str) -> tuple[str, str] | None:
    """Infer ``(tag_text, tag_color)`` from keywords in ``title`` matching ``feishu_send_card.py``."""
    t = title.lower()
    if "晨报" in title:
        return ("市场晨报", "blue")
    if "晚报" in title:
        return ("市场晚报", "purple")
    if "etf" in t:
        return ("ETF Tracking", "indigo")
    if "token" in t or "用量" in title:
        return ("用量与成本", "blue")
    if "ai" in t and ("投资" in title or "信号" in title or "日报" in title):
        return ("AI 投研", "purple")
    if "周报" in title or "weekly" in t or "tracker" in t:
        return ("周度 Tracking", "indigo")
    if "日报" in title or "daily" in t:
        return ("每日追踪", "blue")
    if "风险" in title or "risk" in t or "预警" in title or "alert" in t:
        return ("风险信号", "orange")
    if "监控" in title or "monitor" in t:
        return ("实时监控", "wathet")
    if "扫描" in title or "scan" in t:
        return ("趋势扫描", "turquoise")
    if "总结" in title or "summary" in t:
        return ("每日总结", "blue")
    return None


def extract_markdown_h1_title(markdown: str, default_title: str) -> str:
    """Extract the first top-level ``# ...`` heading from ``markdown`` or return ``default_title``."""
    for raw_line in markdown.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            extracted = stripped[2:].strip()
            if extracted:
                return extracted
    return default_title


def split_markdown_blocks(text: str, max_chars: int = CARD_MARKDOWN_MAX_CHARS) -> list[str]:
    """Split Markdown without dropping content, preferring paragraph boundaries."""
    remaining = text.strip()
    blocks: list[str] = []

    while remaining:
        if len(remaining) <= max_chars:
            blocks.append(remaining)
            break

        split_at = remaining.rfind("\n\n", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = remaining.rfind("\n", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = max_chars

        block = remaining[:split_at].rstrip()
        if block:
            blocks.append(block)
        remaining = remaining[split_at:].lstrip("\n")

    return blocks


def content_to_elements(text: str) -> list[dict[str, Any]]:
    """Convert Markdown text with ``IMG:<placeholder>`` lines into Card 2.0 elements."""
    elements: list[dict[str, Any]] = []
    buf: list[str] = []

    def flush_text() -> None:
        chunk = "\n".join(buf).strip()
        buf.clear()
        if chunk:
            for block in split_markdown_blocks(chunk):
                elements.append({"tag": "markdown", "content": block})

    for line in text.split("\n"):
        m = IMG_LINE_RE.match(line.strip())
        if m:
            flush_text()
            elements.append({"tag": "__img__", "path": m.group(1)})
        else:
            buf.append(line)
    flush_text()
    return elements


def markdown_to_card_elements(
    markdown: str,
    title: str,
    subtitle: str = "",
    header_template: str = "indigo",
    tag: str = "",
    tag_color: str = "",
) -> dict[str, Any]:
    """Deterministically render Markdown into a Feishu Card Schema 2.0 JSON object."""
    sections: list[dict[str, Any]] = []
    current_section: dict[str, Any] = {"title": "", "content": []}

    def flush_current_section() -> None:
        nonlocal current_section
        content = current_section["content"]
        while content and not content[0].strip():
            content.pop(0)
        while content and not content[-1].strip():
            content.pop()
        section_title = str(current_section["title"] or "").strip()
        is_duplicate_card_title = bool(
            section_title and not content and title.strip().startswith(section_title)
        )
        if content or (section_title and not is_duplicate_card_title):
            sections.append(current_section)
        current_section = {"title": "", "content": []}

    lines = markdown.strip().split("\n")
    i = 0
    extracted_subtitle = ""

    while i < len(lines):
        line = lines[i]
        if line.startswith("# ") and not line.startswith("## "):
            if not subtitle and i + 1 < len(lines) and lines[i + 1].startswith("> "):
                extracted_subtitle = lines[i + 1][2:].strip()
                i += 2
                continue
            i += 1
            continue

        heading = re.match(r"^#{2,4}\s+(.+?)\s*$", line)
        if heading:
            flush_current_section()
            current_section = {"title": heading.group(1), "content": []}
            i += 1
            continue

        current_section["content"].append(line)
        i += 1

    flush_current_section()

    effective_subtitle = subtitle or extracted_subtitle
    header: dict[str, Any] = {
        "title": {"tag": "plain_text", "content": title},
        "template": header_template or "indigo",
    }
    if effective_subtitle:
        header["subtitle"] = {"tag": "plain_text", "content": effective_subtitle}

    if tag:
        header["text_tag_list"] = [
            {
                "tag": "text_tag",
                "text": {"tag": "plain_text", "content": tag},
                "color": tag_color or "blue",
            }
        ]
    else:
        inferred = auto_infer_tag(title)
        if inferred:
            header["text_tag_list"] = [
                {
                    "tag": "text_tag",
                    "text": {"tag": "plain_text", "content": inferred[0]},
                    "color": tag_color or inferred[1],
                }
            ]

    elements: list[dict[str, Any]] = []
    titled_sections = [s for s in sections if s["title"]]

    if not titled_sections:
        elements.extend(content_to_elements(markdown.strip()))
    else:
        for section in sections:
            sec_title = str(section["title"]).strip()
            if not sec_title:
                continue

            section_text = "\n".join(section["content"]).strip()

            if NOTATION_SECTION_RE.match(sec_title):
                if elements and elements[-1].get("tag") != "hr":
                    elements.append({"tag": "hr"})
                notation_lines = [
                    f"<font color='grey'>{ln}</font>" if ln.strip() else ""
                    for ln in f"**{sec_title}**\n{section_text}".splitlines()
                ]
                elements.append(
                    {
                        "tag": "markdown",
                        "text_size": "notation",
                        "content": "\n".join(notation_lines),
                    }
                )
                continue

            is_collapsible = bool(COLLAPSIBLE_SUFFIX_RE.search(sec_title)) or sec_title.startswith(
                ("例行披露", "长尾")
            )
            if is_collapsible:
                if elements and elements[-1].get("tag") != "hr":
                    elements.append({"tag": "hr"})
                clean_title = COLLAPSIBLE_SUFFIX_RE.sub("", sec_title).strip()
                inner_elements = content_to_elements(section_text)
                elements.append(
                    {
                        "tag": "collapsible_panel",
                        "expanded": False,
                        "header": {
                            "title": {
                                "tag": "markdown",
                                "content": f"**{clean_title}**",
                            },
                            "vertical_align": "center",
                            "padding": "4px 8px 4px 8px",
                            "icon": {
                                "tag": "standard_icon",
                                "token": "down-small-ccm_outlined",
                                "size": "12px 12px",
                            },
                            "icon_position": "right",
                            "icon_expanded_angle": -180,
                        },
                        "border": {"color": "grey", "corner_radius": "6px"},
                        "vertical_spacing": "6px",
                        "padding": "8px 8px 8px 8px",
                        "elements": inner_elements,
                    }
                )
                continue

            section_content = f"**{sec_title}**\n\n{section_text}"
            elements.extend(content_to_elements(section_content))
            elements.append({"tag": "hr"})

        while elements and elements[-1].get("tag") == "hr":
            elements.pop()

    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
            "update_multi": True,
        },
        "header": header,
        "body": {
            "direction": "vertical",
            "vertical_spacing": "8px",
            "elements": elements,
        },
    }


# ---------------------------------------------------------------------------
# Symlink-proof file access & image validation
# ---------------------------------------------------------------------------


def normalize_run_uuid(run_id: str) -> str:
    """Validate and canonicalize ``run_id`` as a lowercase UUID string."""
    cleaned = str(run_id or "").strip()
    if not cleaned:
        raise DataTaskArtifactError("run_id is required", code="invalid_run_id")
    try:
        return str(UUID(cleaned))
    except ValueError as exc:
        raise DataTaskArtifactError(f"Invalid run_id UUID: {cleaned!r}", code="invalid_run_id") from exc


def sanitize_artifact_filename(raw_name: str, *, fallback: str = "artifact.png") -> str:
    """Produce a clean basename for Content-Disposition and platform uploads."""
    base = PurePosixPath(str(raw_name or "").replace("\\", "/")).name.strip()
    if not base or base in (".", ".."):
        return fallback
    cleaned = SAFE_FILENAME_CHARS_RE.sub("_", base).strip("._")
    if not cleaned:
        return fallback
    suffix = Path(base).suffix.lower()
    if suffix and not cleaned.lower().endswith(suffix):
        cleaned = f"{cleaned}{suffix}"
    return cleaned[:120]


def _normalize_strict_relative_parts(raw_rel_path: str) -> tuple[str, ...]:
    cleaned = str(raw_rel_path or "").strip()
    if not cleaned:
        raise DataTaskArtifactError("Artifact path is empty")
    if "\x00" in cleaned:
        raise DataTaskArtifactError("Artifact path contains null byte")
    if "://" in cleaned or cleaned.startswith(("http:", "https:", "file:", "data:")):
        raise DataTaskArtifactError("URLs are not allowed in artifact paths", code="url_rejected")
    if "\\" in cleaned or cleaned.startswith("/") or cleaned.startswith("~"):
        raise DataTaskArtifactError("Absolute or non-POSIX artifact path is forbidden")
    if len(cleaned) >= 2 and cleaned[1] == ":":
        raise DataTaskArtifactError("Drive-prefixed artifact path is forbidden")

    posix = PurePosixPath(cleaned)
    parts = posix.parts
    if not parts:
        raise DataTaskArtifactError("Artifact path has no components")
    for part in parts:
        if not part or part in (".", ".."):
            raise DataTaskArtifactError("Path traversal segment is forbidden", code="path_traversal")
    return tuple(parts)


def _detect_validated_image_mime(data: bytes, filename: str, *, max_bytes: int = MAX_NOTIFICATION_ARTIFACT_BYTES) -> str:
    if not data:
        raise DataTaskArtifactError("Artifact file is empty", code="artifact_empty")
    if len(data) > max_bytes:
        raise DataTaskArtifactError(
            f"Artifact exceeds maximum size ({len(data)} > {max_bytes} bytes)",
            code="artifact_too_large",
        )

    ext = Path(filename).suffix.lower()
    expected_mime = ALLOWED_IMAGE_EXTENSIONS.get(ext)
    if expected_mime is None:
        raise DataTaskArtifactError(
            f"Unsupported artifact file extension: {ext or '<none>'}",
            code="unsupported_media_type",
        )

    actual_mime: str | None = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        actual_mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        actual_mime = "image/jpeg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        actual_mime = "image/gif"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        actual_mime = "image/webp"

    if actual_mime is None or actual_mime != expected_mime:
        raise DataTaskArtifactError(
            f"Artifact bytes do not match declared image format ({expected_mime})",
            code="unsupported_media_type",
        )
    return actual_mime


def _safe_read_contained_regular_file(
    base_dir: Path,
    relative_path: str,
    *,
    max_bytes: int = MAX_NOTIFICATION_ARTIFACT_BYTES,
    reject_base_symlink: bool = True,
) -> bytes:
    """Open and read ``relative_path`` under ``base_dir`` without following symlinks on any segment.

    Opens the leaf with ``O_NONBLOCK`` so FIFOs or special nodes cannot block ``open(2)``
    before ``fstat`` rejects non-regular descriptors.
    """
    parts = _normalize_strict_relative_parts(relative_path)
    base_str = os.fspath(base_dir)
    try:
        base_lstat = os.lstat(base_str)
    except FileNotFoundError as exc:
        raise DataTaskArtifactError("Artifact base directory does not exist", code="artifact_missing") from exc
    except OSError as exc:
        raise DataTaskArtifactError(f"Unable to stat artifact base directory: {exc.errno}") from exc

    if reject_base_symlink and stat.S_ISLNK(base_lstat.st_mode):
        raise DataTaskArtifactError("Artifact base directory cannot be a symlink", code="symlink_rejected")

    cloexec_flag = getattr(os, "O_CLOEXEC", 0)
    nonblock_flag = getattr(os, "O_NONBLOCK", 0)
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | cloexec_flag
    if reject_base_symlink:
        dir_flags |= getattr(os, "O_NOFOLLOW", 0)

    nofollow_dir_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | cloexec_flag
    )
    nofollow_file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | nonblock_flag
        | cloexec_flag
    )

    current_fd: int | None = None
    try:
        try:
            current_fd = os.open(base_str, dir_flags)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise DataTaskArtifactError(
                    "Symlink or non-directory rejected on base directory",
                    code="symlink_rejected",
                ) from exc
            raise DataTaskArtifactError("Artifact base directory cannot be opened", code="artifact_missing") from exc

        for dir_part in parts[:-1]:
            try:
                next_fd = os.open(dir_part, nofollow_dir_flags, dir_fd=current_fd)
            except FileNotFoundError as exc:
                raise DataTaskArtifactError("Artifact directory does not exist", code="artifact_missing") from exc
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise DataTaskArtifactError(
                        "Symlink traversal in artifact directory is forbidden",
                        code="symlink_rejected",
                    ) from exc
                raise DataTaskArtifactError("Failed to traverse artifact directory") from exc
            prev_fd = current_fd
            current_fd = next_fd
            os.close(prev_fd)

        leaf_name = parts[-1]
        try:
            file_fd = os.open(leaf_name, nofollow_file_flags, dir_fd=current_fd)
        except FileNotFoundError as exc:
            raise DataTaskArtifactError(f"Artifact file not found: {leaf_name}", code="artifact_missing") from exc
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EISDIR):
                raise DataTaskArtifactError(
                    "Symlink or directory target is forbidden for artifact file",
                    code="symlink_rejected",
                ) from exc
            if exc.errno in (
                errno.ENXIO,
                getattr(errno, "EOPNOTSUPP", errno.ENXIO),
                getattr(errno, "ENOTSUP", errno.ENXIO),
            ):
                raise DataTaskArtifactError(
                    "Artifact target is not a regular file",
                    code="unsupported_media_type",
                ) from exc
            raise DataTaskArtifactError("Failed to open artifact file") from exc

        try:
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise DataTaskArtifactError(
                    "Artifact target is not a regular file",
                    code="unsupported_media_type",
                )
            if file_stat.st_size <= 0:
                raise DataTaskArtifactError("Artifact file is empty", code="artifact_empty")
            if file_stat.st_size > max_bytes:
                raise DataTaskArtifactError(
                    f"Artifact exceeds maximum size ({file_stat.st_size} > {max_bytes} bytes)",
                    code="artifact_too_large",
                )

            if nonblock_flag and hasattr(os, "set_blocking"):
                os.set_blocking(file_fd, True)

            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(file_fd, min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise DataTaskArtifactError(
                        f"Artifact grew beyond maximum size ({max_bytes} bytes)",
                        code="artifact_too_large",
                    )
            data = b"".join(chunks)
            if len(data) != file_stat.st_size:
                raise DataTaskArtifactError(
                    "Artifact size changed while reading",
                    code="artifact_changed",
                )
            return data
        finally:
            os.close(file_fd)
    finally:
        if current_fd is not None:
            os.close(current_fd)


def _atomic_write_snapshot_file(run_artifacts_dir: Path, rel_dest: str, content: bytes) -> None:
    parts = _normalize_strict_relative_parts(rel_dest)
    try:
        if run_artifacts_dir.is_symlink():
            raise DataTaskNotificationPrepError(
                "Run artifacts directory cannot be a symlink",
                code="symlink_rejected",
            )
    except OSError as exc:
        raise DataTaskNotificationPrepError("Unable to inspect run artifacts directory") from exc

    run_artifacts_dir.mkdir(parents=True, exist_ok=True)
    cur = run_artifacts_dir
    for part in parts[:-1]:
        cur = cur / part
        if cur.exists() and cur.is_symlink():
            raise DataTaskNotificationPrepError(
                "Run artifacts subdirectory cannot be a symlink",
                code="symlink_rejected",
            )
        cur.mkdir(parents=True, exist_ok=True)

    dest_file = cur / parts[-1]
    if dest_file.exists() and dest_file.is_symlink():
        raise DataTaskNotificationPrepError("Snapshot destination cannot be a symlink", code="symlink_rejected")
    tmp_file = cur / f".{parts[-1]}.tmp.{os.getpid()}"
    if tmp_file.exists() and tmp_file.is_symlink():
        raise DataTaskNotificationPrepError("Temporary snapshot file cannot be a symlink", code="symlink_rejected")
    tmp_file.write_bytes(content)
    os.replace(tmp_file, dest_file)


# ---------------------------------------------------------------------------
# T27 (asia-leverage-crossview-daily) deterministic Card 2.0 & chart capture
# ---------------------------------------------------------------------------


def _resolve_t27_workspace_rel_chart_path(raw_marker_path: str, workspace_dir: Path) -> str:
    cleaned = str(raw_marker_path or "").strip()
    if not cleaned:
        raise DataTaskNotificationPrepError("Empty IMG: marker path in T27 report")
    if "://" in cleaned or cleaned.startswith(("http:", "https:", "file:", "data:")):
        raise DataTaskNotificationPrepError(
            "URL markers are not allowed in T27 report",
            code="url_rejected",
        )

    ws_posix = workspace_dir.as_posix().rstrip("/")
    candidates_to_strip = [CONTAINER_WORKSPACE_PREFIX]
    if ws_posix and ws_posix not in candidates_to_strip:
        candidates_to_strip.append(ws_posix)

    rel_candidate = cleaned
    for prefix in candidates_to_strip:
        prefix_slash = f"{prefix}/"
        if cleaned.startswith(prefix_slash):
            rel_candidate = cleaned[len(prefix_slash) :]
            break

    if rel_candidate.startswith("/"):
        raise DataTaskNotificationPrepError(
            f"T27 chart path escapes workspace root: {cleaned!r}",
            code="path_traversal",
        )

    parts = _normalize_strict_relative_parts(rel_candidate)
    return "/".join(parts)


def prepare_t27_run_outbox(
    markdown_text: str,
    *,
    workspace_dir: Path,
    run_artifacts_dir: Path,
    redactor: SecretRedactor | None = None,
) -> dict[str, Any]:
    """Render ``asia-leverage-crossview-daily`` Markdown into Card 2.0 and snapshot all 4 charts."""
    effective_md = redactor.redact_text(markdown_text) if redactor is not None else str(markdown_text or "")
    if not effective_md.strip():
        raise DataTaskNotificationPrepError("T27 Markdown output is empty")

    if MARKDOWN_LOCAL_IMAGE_RE.search(effective_md):
        raise DataTaskNotificationPrepError(
            "Local Markdown image links are forbidden in T27 output",
            code="local_markdown_image_forbidden",
        )

    # Parse sections in order and verify CN -> KR -> JP -> TW each carry their chart marker.
    sections: list[tuple[str | None, list[str]]] = []
    current_heading: str | None = None
    current_lines: list[str] = []
    for raw_line in effective_md.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("## "):
            if current_heading is not None or current_lines:
                sections.append((current_heading, current_lines))
            current_heading = stripped[3:].strip()
            current_lines = []
        else:
            current_lines.append(raw_line)
    if current_heading is not None or current_lines:
        sections.append((current_heading, current_lines))

    all_img_markers: list[tuple[int, str]] = []
    for sec_idx, (_heading, sec_lines) in enumerate(sections):
        for ln in sec_lines:
            s_ln = ln.strip()
            if s_ln.startswith("IMG:"):
                all_img_markers.append((sec_idx, s_ln[4:].strip()))

    if len(all_img_markers) != len(T27_EXPECTED_CHARTS):
        raise DataTaskNotificationPrepError(
            f"T27 requires exactly {len(T27_EXPECTED_CHARTS)} IMG: chart markers, found {len(all_img_markers)}"
        )

    prev_sec_idx = -1
    snapshot_entries: list[dict[str, Any]] = []
    marker_replacements: dict[int, tuple[str, str]] = {}

    for expected_idx, (market_code, expected_filename, heading_keywords) in enumerate(T27_EXPECTED_CHARTS):
        sec_idx, raw_marker_path = all_img_markers[expected_idx]
        if sec_idx <= prev_sec_idx:
            raise DataTaskNotificationPrepError(
                f"T27 chart marker {expected_filename} is not placed in its own ordered market section"
            )
        sec_heading = (sections[sec_idx][0] or "").lower()
        if not any(kw in sec_heading for kw in heading_keywords):
            raise DataTaskNotificationPrepError(
                f"T27 section {sections[sec_idx][0]!r} does not match expected {market_code.upper()} market order"
            )
        prev_sec_idx = sec_idx

        rel_chart_path = _resolve_t27_workspace_rel_chart_path(raw_marker_path, workspace_dir)
        actual_basename = PurePosixPath(rel_chart_path).name
        if actual_basename != expected_filename:
            raise DataTaskNotificationPrepError(
                f"T27 expected chart {expected_filename} for {market_code.upper()}, got {actual_basename!r}"
            )

        try:
            chart_bytes = _safe_read_contained_regular_file(
                workspace_dir,
                rel_chart_path,
                reject_base_symlink=False,
            )
        except DataTaskArtifactError as exc:
            raise DataTaskNotificationPrepError(
                f"T27 required chart {expected_filename} failed validation: {exc}",
                code=exc.code,
            ) from exc

        mime_type = _detect_validated_image_mime(chart_bytes, expected_filename)
        digest = hashlib.sha256(chart_bytes).hexdigest()
        run_rel_dest = f"images/t27_{expected_idx}_{expected_filename}"
        _atomic_write_snapshot_file(run_artifacts_dir, run_rel_dest, chart_bytes)

        artifact_id = f"card0-img{expected_idx}"
        marker_replacements[expected_idx] = (raw_marker_path, run_rel_dest)
        snapshot_entries.append(
            {
                "artifact_id": artifact_id,
                "placeholder": run_rel_dest,
                "path": run_rel_dest,
                "filename": expected_filename,
                "mime_type": mime_type,
                "size_bytes": len(chart_bytes),
                "sha256": digest,
            }
        )

    # Rewrite IMG: lines in exact occurrence order so local host paths never enter card JSON.
    rewritten_lines: list[str] = []
    marker_cursor = 0
    for raw_line in effective_md.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("IMG:"):
            _, run_rel_dest = marker_replacements[marker_cursor]
            marker_cursor += 1
            rewritten_lines.append(f"IMG:{run_rel_dest}")
        else:
            rewritten_lines.append(raw_line)
    rewritten_md = "\n".join(rewritten_lines)

    title = extract_markdown_h1_title(effective_md, "亚洲四地杠杆/拥挤度监控")
    card = markdown_to_card_elements(markdown=rewritten_md, title=title)

    img_nodes = _collect_card_img_placeholders(card)
    if len(img_nodes) != len(T27_EXPECTED_CHARTS):
        raise DataTaskNotificationPrepError(
            f"Rendered T27 Card 2.0 expected {len(T27_EXPECTED_CHARTS)} image placeholders, got {len(img_nodes)}"
        )
    for idx, node in enumerate(img_nodes):
        expected_placeholder = snapshot_entries[idx]["placeholder"]
        if node.get("path") != expected_placeholder:
            raise DataTaskNotificationPrepError("Rendered T27 Card 2.0 placeholder order mismatch")

    if _card_contains_raw_img_marker(card):
        raise DataTaskNotificationPrepError("Unresolved raw IMG: marker remained in rendered T27 card")

    outbox: dict[str, Any] = {
        "summary": title,
        "feishu_cards": [
            {
                "card": card,
                "images": snapshot_entries,
            }
        ],
    }
    if redactor is not None:
        redacted_val = redactor.redact_value(outbox)
        if isinstance(redacted_val, dict):
            return redacted_val
    return outbox


def is_t27_outbox_prepared(outbox: Mapping[str, Any] | None) -> bool:
    """Return ``True`` when ``outbox`` already carries T27's finalized ``feishu_cards`` snapshot."""
    if not isinstance(outbox, Mapping):
        return False
    cards = outbox.get("feishu_cards")
    return isinstance(cards, list) and len(cards) > 0 and isinstance(cards[0], Mapping)


# ---------------------------------------------------------------------------
# Delivery plan construction & bounded artifact reading
# ---------------------------------------------------------------------------


def _collect_card_img_placeholders(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("tag") == "__img__":
            found.append(node)
        else:
            for val in node.values():
                found.extend(_collect_card_img_placeholders(val))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_card_img_placeholders(item))
    return found


def _card_contains_raw_img_marker(node: Any) -> bool:
    if isinstance(node, dict):
        if node.get("tag") == "markdown":
            content = str(node.get("content") or "")
            for line in content.splitlines():
                if line.strip().startswith("IMG:"):
                    return True
            if MARKDOWN_LOCAL_IMAGE_RE.search(content):
                return True
        for val in node.values():
            if _card_contains_raw_img_marker(val):
                return True
    elif isinstance(node, list):
        for item in node:
            if _card_contains_raw_img_marker(item):
                return True
    return False


def _upgrade_card_envelope_if_needed(raw_card: Mapping[str, Any]) -> dict[str, Any]:
    card = copy.deepcopy(dict(raw_card))
    if card.get("schema") != "2.0" and "elements" in card and "body" not in card:
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": card.get("header", {}),
            "body": {
                "direction": "vertical",
                "vertical_spacing": "8px",
                "elements": card.get("elements", []),
            },
        }
    return card


def _validate_and_register_run_artifact(
    *,
    artifact_id: str,
    rel_path: str,
    run_artifacts_dir: Path,
    expected_size_bytes: int | None = None,
    expected_sha256: str | None = None,
    preferred_filename: str | None = None,
    placeholder: str | None = None,
) -> tuple[PlanArtifactDescriptor, InternalArtifactRef]:
    if not ARTIFACT_ID_RE.match(artifact_id):
        raise DataTaskArtifactError(f"Invalid artifact_id: {artifact_id!r}")

    norm_rel = "/".join(_normalize_strict_relative_parts(rel_path))
    data = _safe_read_contained_regular_file(run_artifacts_dir, norm_rel)
    filename = sanitize_artifact_filename(preferred_filename or PurePosixPath(norm_rel).name)
    mime_type = _detect_validated_image_mime(data, filename)
    actual_size = len(data)
    actual_sha256 = hashlib.sha256(data).hexdigest()

    if expected_size_bytes is not None:
        try:
            exp_size = int(expected_size_bytes)
        except (TypeError, ValueError) as exc:
            raise DataTaskArtifactError("Recorded artifact size_bytes is invalid") from exc
        if exp_size <= 0 or actual_size != exp_size:
            raise DataTaskArtifactError(
                f"Artifact size mismatch for {artifact_id}: expected {exp_size}, got {actual_size}",
                code="artifact_size_mismatch",
            )

    if expected_sha256 is not None:
        exp_sha = str(expected_sha256).strip().lower()
        if not SHA256_HEX_RE.match(exp_sha):
            raise DataTaskArtifactError("Recorded artifact sha256 is malformed")
        if actual_sha256 != exp_sha:
            raise DataTaskArtifactError(
                f"Artifact sha256 mismatch for {artifact_id}",
                code="artifact_sha256_mismatch",
            )

    descriptor = PlanArtifactDescriptor(
        artifact_id=artifact_id,
        filename=filename,
        mime_type=mime_type,
        size_bytes=actual_size,
        sha256=actual_sha256,
        placeholder=placeholder,
    )
    internal_ref = InternalArtifactRef(
        artifact_id=artifact_id,
        rel_path=norm_rel,
        filename=filename,
        mime_type=mime_type,
        size_bytes=actual_size,
        sha256=actual_sha256,
    )
    return descriptor, internal_ref


def build_data_task_delivery_plan(
    *,
    run_id: str,
    job_key: str,
    raw_outbox: Mapping[str, Any] | None,
    run_artifacts_dir: Path,
) -> ResolvedDataTaskPlan:
    """Validate persisted report payload and artifacts for ``run_id`` into an immutable plan."""
    norm_run_id = normalize_run_uuid(run_id)
    cleaned_job_key = str(job_key or "").strip()
    if not cleaned_job_key:
        raise DataTaskNotificationError("job_key is required")
    if not isinstance(raw_outbox, Mapping) or not raw_outbox:
        raise DataTaskNotificationError("Run has no persisted notification outbox payload", code="empty_payload")

    if cleaned_job_key == T27_JOB_KEY and not is_t27_outbox_prepared(raw_outbox):
        raise DataTaskNotificationPrepError(
            "T27 run outbox was not finalized into Card 2.0 with immutable chart snapshots"
        )

    artifacts_by_id: dict[str, InternalArtifactRef] = {}
    plan_items: list[DataTaskDeliveryItem] = []

    # Format 1: feishu_cards: [{card, images, chat_id?}] (T13, T52, T54, finalized T27)
    raw_feishu_cards = raw_outbox.get("feishu_cards")
    if raw_feishu_cards is not None:
        if not isinstance(raw_feishu_cards, list) or not raw_feishu_cards:
            raise DataTaskNotificationError("feishu_cards must be a non-empty list")

        for card_idx, entry in enumerate(raw_feishu_cards):
            if not isinstance(entry, Mapping) or not isinstance(entry.get("card"), Mapping):
                raise DataTaskNotificationError(f"feishu_cards[{card_idx}] is missing a valid card object")
            card_copy = _upgrade_card_envelope_if_needed(entry["card"])
            if _card_contains_raw_img_marker(card_copy):
                raise DataTaskNotificationError("Card contains raw IMG: or local image links")

            raw_images = entry.get("images") or []
            if not isinstance(raw_images, list):
                raise DataTaskNotificationError(f"feishu_cards[{card_idx}].images must be a list")

            image_map_by_placeholder: dict[str, Mapping[str, Any]] = {}
            for img_entry in raw_images:
                if not isinstance(img_entry, Mapping):
                    raise DataTaskNotificationError("Invalid image mapping entry in feishu_cards")
                ph = str(img_entry.get("placeholder") or "").strip()
                rel_p = str(img_entry.get("path") or "").strip()
                if not ph or not rel_p:
                    raise DataTaskArtifactError("Card image entry requires placeholder and run-relative path")
                if ph in image_map_by_placeholder and image_map_by_placeholder[ph].get("path") != rel_p:
                    raise DataTaskArtifactError(f"Conflicting artifact paths for placeholder {ph!r}")
                image_map_by_placeholder[ph] = img_entry

            placeholder_nodes = _collect_card_img_placeholders(card_copy)
            if not placeholder_nodes and image_map_by_placeholder:
                raise DataTaskArtifactError("Card declares staged images that do not match any __img__ element")

            item_descriptors: list[PlanArtifactDescriptor] = []
            placeholder_to_wire: dict[str, PlanArtifactDescriptor] = {}
            next_img_idx = 0

            for node in placeholder_nodes:
                orig_ph = str(node.get("path") or "").strip()
                if not orig_ph or orig_ph not in image_map_by_placeholder:
                    raise DataTaskArtifactError(
                        "Unresolved __img__ placeholder in Feishu card",
                        code="unresolved_placeholder",
                    )
                if orig_ph not in placeholder_to_wire:
                    img_meta = image_map_by_placeholder[orig_ph]
                    artifact_id = f"card{card_idx}-img{next_img_idx}"
                    next_img_idx += 1
                    wire_placeholder = f"artifact:{artifact_id}"
                    preferred_name = str(
                        img_meta.get("filename")
                        or node.get("alt")
                        or PurePosixPath(str(img_meta.get("path") or "")).name
                    )
                    desc, iref = _validate_and_register_run_artifact(
                        artifact_id=artifact_id,
                        rel_path=str(img_meta.get("path") or ""),
                        run_artifacts_dir=run_artifacts_dir,
                        expected_size_bytes=img_meta.get("size_bytes"),
                        expected_sha256=img_meta.get("sha256"),
                        preferred_filename=preferred_name,
                        placeholder=wire_placeholder,
                    )
                    artifacts_by_id[artifact_id] = iref
                    placeholder_to_wire[orig_ph] = desc
                    item_descriptors.append(desc)

                matched_desc = placeholder_to_wire[orig_ph]
                node["path"] = matched_desc.placeholder

            if len(placeholder_to_wire) != len(image_map_by_placeholder):
                raise DataTaskArtifactError(
                    "Staged card image entry was not referenced by any __img__ placeholder",
                    code="unresolved_placeholder",
                )

            plan_items.append(
                DataTaskDeliveryItem(
                    item_index=card_idx,
                    kind="feishu_card",
                    card=card_copy,
                    images=tuple(item_descriptors),
                )
            )

        return ResolvedDataTaskPlan(
            run_id=norm_run_id,
            job_key=cleaned_job_key,
            items=tuple(plan_items),
            artifacts_by_id=artifacts_by_id,
        )

    # Format 2: Ordered items: [{index, text, media, chart_bytes, chart_sha256, ...}] (T26)
    raw_items = raw_outbox.get("items")
    if raw_items is not None:
        if not isinstance(raw_items, list) or not raw_items:
            raise DataTaskNotificationError("items must be a non-empty list")

        indexed_entries: list[tuple[int, Mapping[str, Any]]] = []
        seen_indices: set[int] = set()
        for pos, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, Mapping):
                raise DataTaskNotificationError(f"items[{pos}] must be an object")
            raw_idx = raw_item.get("index", pos)
            try:
                idx_val = int(raw_idx)
            except (TypeError, ValueError) as exc:
                raise DataTaskNotificationError(f"items[{pos}].index must be an integer") from exc
            if idx_val < 0 or idx_val in seen_indices:
                raise DataTaskNotificationError(f"Invalid or duplicate item index: {idx_val}")
            seen_indices.add(idx_val)
            indexed_entries.append((idx_val, raw_item))

        indexed_entries.sort(key=lambda pair: pair[0])
        if [idx for idx, _ in indexed_entries] != list(range(len(indexed_entries))):
            raise DataTaskNotificationError("Ordered items must have contiguous 0-based indices")

        if cleaned_job_key == T26_JOB_KEY and len(indexed_entries) != 2:
            raise DataTaskNotificationError(
                f"T26 ({T26_JOB_KEY}) requires exactly 2 ordered text/chart items, got {len(indexed_entries)}"
            )

        for ordered_idx, (_, item_obj) in enumerate(indexed_entries):
            item_text = str(item_obj.get("text") or "").strip()
            raw_media = item_obj.get("media")
            has_media = isinstance(raw_media, str) and bool(raw_media.strip())

            if cleaned_job_key == T26_JOB_KEY:
                if not item_text:
                    raise DataTaskNotificationError(f"T26 item {ordered_idx} has empty text")
                if not has_media:
                    raise DataTaskArtifactError(f"T26 item {ordered_idx} is missing required chart media")
                if item_obj.get("chart_bytes") is None or item_obj.get("chart_sha256") is None:
                    raise DataTaskArtifactError(
                        f"T26 item {ordered_idx} is missing chart_bytes or chart_sha256 metadata"
                    )

            if MARKDOWN_LOCAL_IMAGE_RE.search(item_text):
                raise DataTaskNotificationError("Local Markdown image links are forbidden in item text")

            if has_media:
                artifact_id = f"item{ordered_idx}-media0"
                desc, iref = _validate_and_register_run_artifact(
                    artifact_id=artifact_id,
                    rel_path=str(raw_media).strip(),
                    run_artifacts_dir=run_artifacts_dir,
                    expected_size_bytes=item_obj.get("chart_bytes"),
                    expected_sha256=item_obj.get("chart_sha256"),
                )
                artifacts_by_id[artifact_id] = iref
                plan_items.append(
                    DataTaskDeliveryItem(
                        item_index=ordered_idx,
                        kind="text_and_file",
                        text=item_text,
                        artifact=desc,
                    )
                )
            else:
                if not item_text:
                    raise DataTaskNotificationError(f"Item {ordered_idx} has neither text nor media")
                plan_items.append(
                    DataTaskDeliveryItem(
                        item_index=ordered_idx,
                        kind="text",
                        text=item_text,
                    )
                )

        return ResolvedDataTaskPlan(
            run_id=norm_run_id,
            job_key=cleaned_job_key,
            items=tuple(plan_items),
            artifacts_by_id=artifacts_by_id,
        )

    # Format 3: Single card or plain/markdown text outbox
    raw_single_card = raw_outbox.get("card")
    text_val = ""
    for text_key in ("feishu_message", "message", "text", "summary"):
        candidate = raw_outbox.get(text_key)
        if isinstance(candidate, str) and candidate.strip():
            text_val = candidate.strip()
            break

    if isinstance(raw_single_card, Mapping):
        card_copy = _upgrade_card_envelope_if_needed(raw_single_card)
        if _collect_card_img_placeholders(card_copy):
            raise DataTaskArtifactError(
                "Single card contains unresolved __img__ placeholder",
                code="unresolved_placeholder",
            )
        if _card_contains_raw_img_marker(card_copy):
            raise DataTaskNotificationError("Single card contains raw IMG: or local image links")
        plan_items.append(
            DataTaskDeliveryItem(
                item_index=0,
                kind="feishu_card",
                text=text_val,
                card=card_copy,
                images=(),
            )
        )
        return ResolvedDataTaskPlan(
            run_id=norm_run_id,
            job_key=cleaned_job_key,
            items=tuple(plan_items),
            artifacts_by_id=artifacts_by_id,
        )

    if not text_val:
        raise DataTaskNotificationError("Outbox contains no deliverable card, items, or text", code="empty_payload")
    for line in text_val.splitlines():
        if line.strip().startswith("IMG:"):
            raise DataTaskNotificationError("Raw IMG: marker cannot be sent as plain text")
    if MARKDOWN_LOCAL_IMAGE_RE.search(text_val):
        raise DataTaskNotificationError("Local Markdown image links cannot be sent")

    plan_items.append(
        DataTaskDeliveryItem(
            item_index=0,
            kind="text",
            text=text_val,
        )
    )
    return ResolvedDataTaskPlan(
        run_id=norm_run_id,
        job_key=cleaned_job_key,
        items=tuple(plan_items),
        artifacts_by_id=artifacts_by_id,
    )


def read_validated_run_artifact(
    *,
    run_id: str,
    artifact_id: str,
    plan: ResolvedDataTaskPlan,
    run_artifacts_dir: Path,
) -> ValidatedArtifactBytes:
    """Safely read bounded bytes for ``artifact_id`` from ``plan`` under ``.dalton-runs/<run_id>/``."""
    norm_run_id = normalize_run_uuid(run_id)
    if norm_run_id != plan.run_id:
        raise DataTaskArtifactError("Cross-run artifact access is forbidden", code="cross_run_denied")

    cleaned_id = str(artifact_id or "").strip()
    if not ARTIFACT_ID_RE.match(cleaned_id):
        raise DataTaskArtifactError("Invalid artifact_id format", code="artifact_invalid")

    ref = plan.artifacts_by_id.get(cleaned_id)
    if ref is None:
        raise DataTaskArtifactNotFoundError(f"Artifact {cleaned_id!r} is not part of run {norm_run_id}")

    data = _safe_read_contained_regular_file(run_artifacts_dir, ref.rel_path)
    mime_type = _detect_validated_image_mime(data, ref.filename)
    actual_size = len(data)
    actual_sha256 = hashlib.sha256(data).hexdigest()

    if mime_type != ref.mime_type:
        raise DataTaskArtifactError("Artifact MIME type changed since plan validation", code="artifact_changed")
    if actual_size != ref.size_bytes:
        raise DataTaskArtifactError(
            f"Artifact size changed since plan validation: expected {ref.size_bytes}, got {actual_size}",
            code="artifact_size_mismatch",
        )
    if actual_sha256 != ref.sha256:
        raise DataTaskArtifactError(
            "Artifact sha256 changed since plan validation",
            code="artifact_sha256_mismatch",
        )

    return ValidatedArtifactBytes(
        artifact_id=cleaned_id,
        content=data,
        mime_type=mime_type,
        filename=ref.filename,
        size_bytes=actual_size,
        sha256=actual_sha256,
    )
