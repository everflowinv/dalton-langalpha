"""Descriptor-anchored, non-blocking, symlink-safe file I/O for data-task artifacts and metadata.

All path traversal, directory creation, bounded file reads, and atomic file publications operate
through verified directory descriptors (`dir_fd`) with `O_NOFOLLOW`, ensuring caller- or
task-controlled symlinks, FIFOs, device nodes, and mid-flight directory replacements cannot
redirect reads or writes outside the run-artifact boundary or block the backend/gateway process.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
from typing import Any

# Run metadata JSON limit (`outbox.json`, `frozen_manifest.json`): 1 MiB.
# Existing structured Card 2.0 / ordered-item / manifest payloads are < 64 KiB (charts live in
# separate image files), so 1 MiB provides >15x headroom while strictly bounding memory before
# UTF-8 decoding and JSON parsing.
MAX_RUN_METADATA_BYTES = 1 * 1024 * 1024

_SYMLINK_ERRNOS: tuple[int, ...] = tuple(
    dict.fromkeys(
        (
            errno.ELOOP,
            errno.ENOTDIR,
            getattr(errno, "EMLINK", errno.ELOOP),
        )
    )
)
_NONREGULAR_OPEN_ERRNOS: tuple[int, ...] = tuple(
    dict.fromkeys(
        (
            errno.ENXIO,
            getattr(errno, "EOPNOTSUPP", errno.ENXIO),
            getattr(errno, "ENOTSUP", errno.ENXIO),
        )
    )
)
_DIR_FSYNC_IGNORED_ERRNOS: tuple[int, ...] = tuple(
    dict.fromkeys(
        (
            errno.EBADF,
            errno.EINVAL,
            errno.EROFS,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        )
    )
)


class SafeFsError(OSError):
    """Typed filesystem validation or containment failure carrying a safe classification code."""

    def __init__(self, message: str, *, code: str = "artifact_invalid") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _VerifiedDirStep:
    parent_fd: int
    name: str
    fd: int
    dev: int
    ino: int


class _VerifiedDirChain:
    """Owns an anchor directory descriptor and each verified child directory descriptor."""

    def __init__(
        self,
        *,
        anchor_path: str,
        reject_anchor_symlink: bool,
        anchor_fd: int,
        anchor_dev: int,
        anchor_ino: int,
        steps: list[_VerifiedDirStep],
    ) -> None:
        self.anchor_path = anchor_path
        self.reject_anchor_symlink = reject_anchor_symlink
        self.anchor_fd = anchor_fd
        self.anchor_dev = anchor_dev
        self.anchor_ino = anchor_ino
        self.steps = steps
        self._closed = False

    @property
    def leaf_fd(self) -> int:
        if self.steps:
            return self.steps[-1].fd
        return self.anchor_fd

    def verify_intact(self) -> None:
        """Confirm neither the anchor nor any traversed subdirectory was replaced or symlinked."""
        try:
            anchor_st = (
                os.lstat(self.anchor_path)
                if self.reject_anchor_symlink
                else os.stat(self.anchor_path)
            )
        except OSError as exc:
            raise SafeFsError(
                "Anchor directory disappeared or became inaccessible during operation",
                code="directory_changed",
            ) from exc

        if self.reject_anchor_symlink and stat.S_ISLNK(anchor_st.st_mode):
            raise SafeFsError(
                "Anchor directory cannot be a symlink",
                code="symlink_rejected",
            )
        if not stat.S_ISDIR(anchor_st.st_mode):
            raise SafeFsError(
                "Anchor path is no longer a directory",
                code="directory_changed",
            )
        if (anchor_st.st_dev, anchor_st.st_ino) != (self.anchor_dev, self.anchor_ino):
            raise SafeFsError(
                "Anchor directory inode changed during operation",
                code="directory_changed",
            )

        for step in self.steps:
            try:
                cur_st = os.stat(step.name, dir_fd=step.parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise SafeFsError(
                    f"Directory component {step.name!r} disappeared during operation",
                    code="directory_changed",
                ) from exc
            if stat.S_ISLNK(cur_st.st_mode):
                raise SafeFsError(
                    f"Directory component {step.name!r} was replaced with a symlink",
                    code="symlink_rejected",
                )
            if not stat.S_ISDIR(cur_st.st_mode):
                raise SafeFsError(
                    f"Directory component {step.name!r} is no longer a directory",
                    code="directory_changed",
                )
            if (cur_st.st_dev, cur_st.st_ino) != (step.dev, step.ino):
                raise SafeFsError(
                    f"Directory component {step.name!r} was replaced during operation",
                    code="directory_changed",
                )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for step in reversed(self.steps):
            try:
                os.close(step.fd)
            except OSError:
                pass
        try:
            os.close(self.anchor_fd)
        except OSError:
            pass


def normalize_strict_relative_parts(raw_path: str | Sequence[str]) -> tuple[str, ...]:
    """Validate ``raw_path`` as a non-empty relative POSIX path with no ``.``, ``..``, or URL scheme."""
    if isinstance(raw_path, (list, tuple)):
        if not raw_path or any(not isinstance(seg, str) or not seg for seg in raw_path):
            raise SafeFsError("Artifact path cannot be empty", code="path_traversal")
        for seg in raw_path:
            if seg in (".", "..") or "/" in seg or "\\" in seg or "\x00" in seg or "://" in seg:
                raise SafeFsError(
                    f"Path traversal or malformed segment rejected: {seg!r}",
                    code="path_traversal",
                )
        return tuple(raw_path)

    cleaned = str(raw_path or "").strip()
    if not cleaned:
        raise SafeFsError("Artifact path cannot be empty", code="path_traversal")
    if "://" in cleaned or cleaned.startswith(("http:", "https:", "file:", "data:")):
        raise SafeFsError(
            "URL references are forbidden for artifacts",
            code="url_rejected",
        )
    if (
        cleaned.startswith(("/", "\\", "~"))
        or "\\" in cleaned
        or "\x00" in cleaned
        or (len(cleaned) >= 2 and cleaned[1] == ":")
    ):
        raise SafeFsError(
            f"Absolute or malformed artifact path rejected: {cleaned!r}",
            code="path_traversal",
        )
    rel = PurePosixPath(cleaned)
    if rel.is_absolute():
        raise SafeFsError(
            f"Absolute artifact path rejected: {cleaned!r}",
            code="path_traversal",
        )
    raw_segments = cleaned.split("/")
    if any(seg in ("", ".", "..") for seg in raw_segments):
        raise SafeFsError(
            f"Path traversal or empty segment rejected: {cleaned!r}",
            code="path_traversal",
        )
    parts = tuple(rel.parts)
    if not parts or any(p in ("", ".", "..") or "/" in p or "\\" in p for p in parts):
        raise SafeFsError(
            f"Path traversal segment rejected: {cleaned!r}",
            code="path_traversal",
        )
    return parts


def _resolve_anchor_and_dir_parts(
    base_dir: Path,
    rel_dir_parts: Sequence[str],
    *,
    anchor_dir: Path | None,
    reject_base_symlink: bool,
    create_missing: bool,
) -> tuple[Path, tuple[str, ...], bool]:
    """Determine ``(anchor_path, child_dir_parts, reject_anchor_symlink)``.

    Preserves configured storage-root semantics on macOS (`/var` symlink or configured
    `openclaw-data` root) while enforcing `O_NOFOLLOW` on `.dalton-runs`, `<run_id>`,
    `run_artifacts_dir`, and every subdirectory beneath the anchor.
    """
    if anchor_dir is not None:
        try:
            rel_from_anchor = base_dir.relative_to(anchor_dir)
        except ValueError as exc:
            raise SafeFsError(
                "Target directory escapes configured storage anchor",
                code="path_traversal",
            ) from exc
        extra_parts: list[str] = []
        for seg in rel_from_anchor.parts:
            if seg in ("", "."):
                continue
            if seg == ".." or "/" in seg or "\\" in seg or "\x00" in seg:
                raise SafeFsError(
                    "Invalid relative segment under storage anchor",
                    code="path_traversal",
                )
            extra_parts.append(seg)
        return anchor_dir, (*extra_parts, *rel_dir_parts), False

    if not reject_base_symlink:
        return base_dir, tuple(rel_dir_parts), False

    # Automatically anchor at `<data_root>` when `base_dir` is `<data_root>/.dalton-runs/<run_id>`
    # so both `.dalton-runs` and `<run_id>` are traversed via `dir_fd` + `O_NOFOLLOW` while
    # preserving `<data_root>` storage-root semantics on macOS and Linux.
    if (
        base_dir.name not in ("", ".", "..")
        and base_dir.parent.name == ".dalton-runs"
        and base_dir.parent.parent != base_dir.parent
    ):
        storage_root = base_dir.parent.parent
        return storage_root, (".dalton-runs", base_dir.name, *rel_dir_parts), False

    if not create_missing:
        return base_dir, tuple(rel_dir_parts), True

    # Walk upward only across genuinely missing (`ENOENT`) segments; any symlink encountered
    # via `os.lstat` (including a dangling symlink) is rejected immediately without `exists()`.
    missing_reversed: list[str] = []
    cur = base_dir
    while True:
        cur_str = os.fspath(cur)
        try:
            st = os.lstat(cur_str)
        except FileNotFoundError:
            parent = cur.parent
            if parent == cur or not cur.name or cur.name in (".", ".."):
                raise SafeFsError(
                    "Artifact base directory has no existing ancestor",
                    code="artifact_missing",
                ) from None
            missing_reversed.append(cur.name)
            cur = parent
            continue
        except OSError as exc:
            if exc.errno in _SYMLINK_ERRNOS:
                raise SafeFsError(
                    "Symlink rejected on artifact base directory",
                    code="symlink_rejected",
                ) from exc
            raise SafeFsError(
                f"Unable to inspect artifact base directory: {exc.errno}",
                code="artifact_missing",
            ) from exc

        if stat.S_ISLNK(st.st_mode):
            raise SafeFsError(
                "Artifact base directory cannot be a symlink",
                code="symlink_rejected",
            )
        if not stat.S_ISDIR(st.st_mode):
            raise SafeFsError(
                "Artifact base path is not a directory",
                code="symlink_rejected",
            )
        break

    missing_reversed.reverse()
    return cur, (*missing_reversed, *rel_dir_parts), True


def _open_verified_dir_chain(
    base_dir: Path,
    rel_dir_parts: Sequence[str],
    *,
    anchor_dir: Path | None = None,
    reject_base_symlink: bool = True,
    create_missing: bool = False,
    allow_missing: bool = False,
) -> _VerifiedDirChain | None:
    anchor_path, dir_parts, reject_anchor_symlink = _resolve_anchor_and_dir_parts(
        base_dir,
        rel_dir_parts,
        anchor_dir=anchor_dir,
        reject_base_symlink=reject_base_symlink,
        create_missing=create_missing,
    )
    anchor_str = os.fspath(anchor_path)
    cloexec_flag = getattr(os, "O_CLOEXEC", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)

    try:
        anchor_stat_pre = (
            os.lstat(anchor_str) if reject_anchor_symlink else os.stat(anchor_str)
        )
    except FileNotFoundError as exc:
        if create_missing and not reject_anchor_symlink:
            # Configured storage root directory (`data_root`) does not exist yet; create it,
            # then re-stat without allowing a leaf symlink if `reject_anchor_symlink` was requested.
            anchor_path.mkdir(parents=True, exist_ok=True)
            anchor_stat_pre = os.stat(anchor_str)
        elif allow_missing:
            return None
        else:
            raise SafeFsError(
                "Artifact base directory does not exist",
                code="artifact_missing",
            ) from exc
    except OSError as exc:
        if exc.errno in _SYMLINK_ERRNOS:
            raise SafeFsError(
                "Symlink rejected on artifact base directory",
                code="symlink_rejected",
            ) from exc
        raise SafeFsError(
            f"Unable to stat artifact base directory: {exc.errno}",
            code="artifact_missing",
        ) from exc

    if reject_anchor_symlink and stat.S_ISLNK(anchor_stat_pre.st_mode):
        raise SafeFsError(
            "Artifact base directory cannot be a symlink",
            code="symlink_rejected",
        )
    if not stat.S_ISDIR(anchor_stat_pre.st_mode):
        raise SafeFsError(
            "Artifact base path is not a directory",
            code="symlink_rejected",
        )

    anchor_open_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | cloexec_flag
    if reject_anchor_symlink:
        anchor_open_flags |= nofollow_flag

    try:
        anchor_fd = os.open(anchor_str, anchor_open_flags)
    except FileNotFoundError as exc:
        if allow_missing:
            return None
        raise SafeFsError(
            "Artifact base directory does not exist",
            code="artifact_missing",
        ) from exc
    except OSError as exc:
        if exc.errno in _SYMLINK_ERRNOS:
            raise SafeFsError(
                "Symlink or non-directory rejected on base directory",
                code="symlink_rejected",
            ) from exc
        raise SafeFsError(
            "Artifact base directory cannot be opened",
            code="artifact_missing",
        ) from exc

    steps: list[_VerifiedDirStep] = []
    try:
        anchor_fstat = os.fstat(anchor_fd)
        if not stat.S_ISDIR(anchor_fstat.st_mode):
            raise SafeFsError(
                "Opened anchor descriptor is not a directory",
                code="symlink_rejected",
            )
        if (anchor_fstat.st_dev, anchor_fstat.st_ino) != (
            anchor_stat_pre.st_dev,
            anchor_stat_pre.st_ino,
        ):
            raise SafeFsError(
                "Anchor directory changed while opening",
                code="directory_changed",
            )

        nofollow_dir_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow_flag | cloexec_flag
        )
        parent_fd = anchor_fd

        for part in dir_parts:
            if create_missing:
                try:
                    os.mkdir(part, 0o755, dir_fd=parent_fd)
                except FileExistsError:
                    # Existing entry (directory, regular file, or symlink/dangling symlink):
                    # never inspect with `exists()`; open below with `O_DIRECTORY | O_NOFOLLOW`
                    # so any symlink or non-directory is atomically rejected.
                    pass
                except OSError as exc:
                    if exc.errno in _SYMLINK_ERRNOS:
                        raise SafeFsError(
                            f"Symlink rejected while creating directory {part!r}",
                            code="symlink_rejected",
                        ) from exc
                    raise SafeFsError(
                        f"Failed to create artifact directory {part!r}",
                        code="write_failed",
                    ) from exc

            try:
                next_fd = os.open(part, nofollow_dir_flags, dir_fd=parent_fd)
            except FileNotFoundError as exc:
                # Confirm via `lstat` on `parent_fd` that `part` is genuinely absent rather than a symlink.
                try:
                    l_st = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    l_st = None
                if l_st is not None and stat.S_ISLNK(l_st.st_mode):
                    raise SafeFsError(
                        f"Symlink traversal in directory {part!r} is forbidden",
                        code="symlink_rejected",
                    ) from exc
                if allow_missing and not create_missing:
                    for s in reversed(steps):
                        try:
                            os.close(s.fd)
                        except OSError:
                            pass
                    try:
                        os.close(anchor_fd)
                    except OSError:
                        pass
                    return None
                raise SafeFsError(
                    f"Artifact directory does not exist: {part!r}",
                    code="artifact_missing",
                ) from exc
            except OSError as exc:
                if exc.errno in _SYMLINK_ERRNOS or exc.errno == errno.EEXIST:
                    raise SafeFsError(
                        f"Symlink or non-directory traversal in {part!r} is forbidden",
                        code="symlink_rejected",
                    ) from exc
                raise SafeFsError(
                    f"Failed to traverse artifact directory {part!r}",
                    code="artifact_invalid",
                ) from exc

            next_stat = os.fstat(next_fd)
            if not stat.S_ISDIR(next_stat.st_mode):
                os.close(next_fd)
                raise SafeFsError(
                    f"Traversed descriptor for {part!r} is not a directory",
                    code="symlink_rejected",
                )
            steps.append(
                _VerifiedDirStep(
                    parent_fd=parent_fd,
                    name=part,
                    fd=next_fd,
                    dev=next_stat.st_dev,
                    ino=next_stat.st_ino,
                )
            )
            parent_fd = next_fd

        chain = _VerifiedDirChain(
            anchor_path=anchor_str,
            reject_anchor_symlink=reject_anchor_symlink,
            anchor_fd=anchor_fd,
            anchor_dev=anchor_fstat.st_dev,
            anchor_ino=anchor_fstat.st_ino,
            steps=steps,
        )
        chain.verify_intact()
        return chain
    except BaseException:
        for s in reversed(steps):
            try:
                os.close(s.fd)
            except OSError:
                pass
        try:
            os.close(anchor_fd)
        except OSError:
            pass
        raise


def safe_read_contained_regular_bytes(
    base_dir: Path,
    relative_path: str,
    *,
    max_bytes: int,
    anchor_dir: Path | None = None,
    reject_base_symlink: bool = True,
    allow_missing: bool = False,
    too_large_code: str = "artifact_too_large",
    empty_code: str = "artifact_empty",
    nonregular_code: str = "unsupported_media_type",
) -> bytes | None:
    """Read a bounded regular file under ``base_dir`` without following symlinks or blocking on FIFOs."""
    parts = normalize_strict_relative_parts(relative_path)
    chain = _open_verified_dir_chain(
        base_dir,
        parts[:-1],
        anchor_dir=anchor_dir,
        reject_base_symlink=reject_base_symlink,
        create_missing=False,
        allow_missing=allow_missing,
    )
    if chain is None:
        return None

    cloexec_flag = getattr(os, "O_CLOEXEC", 0)
    nonblock_flag = getattr(os, "O_NONBLOCK", 0)
    nofollow_file_flags = (
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | nonblock_flag | cloexec_flag
    )
    leaf_name = parts[-1]

    try:
        try:
            file_fd = os.open(leaf_name, nofollow_file_flags, dir_fd=chain.leaf_fd)
        except FileNotFoundError as exc:
            try:
                l_st = os.stat(leaf_name, dir_fd=chain.leaf_fd, follow_symlinks=False)
            except FileNotFoundError:
                l_st = None
            if l_st is not None and stat.S_ISLNK(l_st.st_mode):
                raise SafeFsError(
                    "Symlink target is forbidden for artifact file",
                    code="symlink_rejected",
                ) from exc
            if allow_missing:
                return None
            raise SafeFsError(
                f"Artifact file not found: {leaf_name}",
                code="artifact_missing",
            ) from exc
        except OSError as exc:
            if exc.errno in (*_SYMLINK_ERRNOS, errno.EISDIR):
                # Distinguish directory leaf vs symlink leaf only when needed;
                # non-regular directory targets on metadata/artifacts are always rejected.
                raise SafeFsError(
                    "Symlink or directory target is forbidden for artifact file",
                    code="symlink_rejected",
                ) from exc
            if exc.errno in _NONREGULAR_OPEN_ERRNOS:
                raise SafeFsError(
                    "Artifact target is not a regular file",
                    code=nonregular_code,
                ) from exc
            raise SafeFsError(
                "Failed to open artifact file",
                code="artifact_invalid",
            ) from exc

        try:
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise SafeFsError(
                    "Artifact target is not a regular file",
                    code=nonregular_code,
                )
            if file_stat.st_size <= 0:
                raise SafeFsError("Artifact file is empty", code=empty_code)
            if file_stat.st_size > max_bytes:
                raise SafeFsError(
                    f"File exceeds maximum size ({file_stat.st_size} > {max_bytes} bytes)",
                    code=too_large_code,
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
                    raise SafeFsError(
                        f"File grew beyond maximum size ({max_bytes} bytes)",
                        code=too_large_code,
                    )
            data = b"".join(chunks)
            if len(data) != file_stat.st_size:
                raise SafeFsError(
                    "Artifact size changed while reading",
                    code="artifact_changed",
                )
            chain.verify_intact()
            return data
        finally:
            os.close(file_fd)
    finally:
        chain.close()


def safe_read_json_object(
    base_dir: Path,
    relative_path: str,
    *,
    anchor_dir: Path | None = None,
    reject_base_symlink: bool = True,
    allow_missing: bool = True,
    max_bytes: int = MAX_RUN_METADATA_BYTES,
) -> dict[str, Any] | None:
    """Safely read and parse a bounded JSON object metadata file without image MIME checks."""
    raw_bytes = safe_read_contained_regular_bytes(
        base_dir,
        relative_path,
        max_bytes=max_bytes,
        anchor_dir=anchor_dir,
        reject_base_symlink=reject_base_symlink,
        allow_missing=allow_missing,
        too_large_code="metadata_too_large",
        empty_code="invalid_metadata",
        nonregular_code="non_regular_file",
    )
    if raw_bytes is None:
        return None
    try:
        decoded = raw_bytes.decode("utf-8")
        parsed = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafeFsError(
            "Run metadata file is not valid UTF-8 JSON",
            code="invalid_metadata",
        ) from exc
    if not isinstance(parsed, dict):
        raise SafeFsError(
            "Run metadata JSON root must be an object",
            code="invalid_metadata",
        )
    return parsed


def safe_atomic_write_bytes(
    base_dir: Path,
    relative_path: str | Sequence[str],
    content: bytes,
    *,
    anchor_dir: Path | None = None,
    reject_base_symlink: bool = True,
    mode: int = 0o600,
    _token_supplier: Callable[[], str] | None = None,
    _on_dir_verified: Callable[[], None] | None = None,
    _on_before_publish: Callable[..., None] | None = None,
    _write_chunk_fn: Callable[[int, memoryview], int] | None = None,
) -> Path:
    """Create and atomically publish ``relative_path`` under ``base_dir`` via verified ``dir_fd``s."""
    parts = normalize_strict_relative_parts(relative_path)
    leaf_name = parts[-1]
    token_fn = _token_supplier if _token_supplier is not None else (lambda: secrets.token_hex(12))
    write_fn = _write_chunk_fn if _write_chunk_fn is not None else os.write

    chain = _open_verified_dir_chain(
        base_dir,
        parts[:-1],
        anchor_dir=anchor_dir,
        reject_base_symlink=reject_base_symlink,
        create_missing=True,
        allow_missing=False,
    )
    assert chain is not None

    dest_dir_fd = chain.leaf_fd
    tmp_fd: int | None = None
    created_tmp_name: str | None = None
    tmp_identity: tuple[int, int] | None = None

    try:
        if _on_dir_verified is not None:
            _on_dir_verified()

        chain.verify_intact()

        # Refuse existing or dangling destination symlinks and non-regular entries BEFORE writing.
        try:
            pre_dest_st = os.stat(leaf_name, dir_fd=dest_dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            pre_dest_st = None
        if pre_dest_st is not None:
            if stat.S_ISLNK(pre_dest_st.st_mode):
                raise SafeFsError(
                    "Destination path cannot be a symlink",
                    code="symlink_rejected",
                )
            if not stat.S_ISREG(pre_dest_st.st_mode):
                raise SafeFsError(
                    "Destination path is not a regular file",
                    code="symlink_rejected",
                )

        tmp_open_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        pid = os.getpid()
        max_attempts = 8
        for _attempt in range(max_attempts):
            token = str(token_fn()).strip()
            if not token or "/" in token or "\\" in token or "\x00" in token or ".." in token:
                raise SafeFsError("Invalid temporary filename token", code="write_failed")
            candidate_tmp = f".tmp.{leaf_name}.{pid}.{token}"
            try:
                opened_fd = os.open(
                    candidate_tmp,
                    tmp_open_flags,
                    mode,
                    dir_fd=dest_dir_fd,
                )
            except FileExistsError:
                # Existing file or symlink (including a dangling symlink): never open,
                # truncate, follow, or remove it; try a fresh unpredictable name.
                continue
            except OSError as exc:
                if exc.errno in _SYMLINK_ERRNOS:
                    continue
                raise SafeFsError(
                    "Failed to create exclusive temporary file",
                    code="write_failed",
                ) from exc
            tmp_fd = opened_fd
            created_tmp_name = candidate_tmp
            break

        if tmp_fd is None or created_tmp_name is None:
            raise SafeFsError(
                "Exhausted exclusive temporary filename attempts due to collisions",
                code="temp_create_collision",
            )

        tmp_stat = os.fstat(tmp_fd)
        if not stat.S_ISREG(tmp_stat.st_mode) or tmp_stat.st_nlink != 1:
            raise SafeFsError(
                "Opened temporary descriptor is not an unlinked-safe regular file",
                code="symlink_rejected",
            )
        tmp_identity = (tmp_stat.st_dev, tmp_stat.st_ino)

        view = memoryview(content)
        total_len = len(view)
        written = 0
        try:
            while written < total_len:
                n = write_fn(tmp_fd, view[written:])
                if n <= 0:
                    raise SafeFsError(
                        "Short or zero-byte write to temporary file",
                        code="write_failed",
                    )
                written += n
            os.fsync(tmp_fd)
        except SafeFsError:
            raise
        except OSError as exc:
            raise SafeFsError(
                f"Failed writing temporary file: {exc.errno}",
                code="write_failed",
            ) from exc

        if _on_before_publish is not None:
            try:
                _on_before_publish(created_tmp_name)
            except TypeError:
                _on_before_publish()

        chain.verify_intact()

        fd_st_before = os.fstat(tmp_fd)
        if (
            not stat.S_ISREG(fd_st_before.st_mode)
            or (fd_st_before.st_dev, fd_st_before.st_ino) != tmp_identity
        ):
            raise SafeFsError(
                "Temporary descriptor identity changed before publish",
                code="symlink_rejected",
            )

        try:
            tmp_entry_st = os.stat(
                created_tmp_name,
                dir_fd=dest_dir_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise SafeFsError(
                "Temporary file disappeared before publish",
                code="write_failed",
            ) from exc
        if stat.S_ISLNK(tmp_entry_st.st_mode):
            raise SafeFsError(
                "Temporary file was replaced with a symlink before publish",
                code="symlink_rejected",
            )
        if (
            not stat.S_ISREG(tmp_entry_st.st_mode)
            or (tmp_entry_st.st_dev, tmp_entry_st.st_ino) != tmp_identity
            or tmp_entry_st.st_nlink != 1
        ):
            raise SafeFsError(
                "Temporary file was replaced before publish",
                code="symlink_rejected",
            )

        # Re-check destination immediately before descriptor-relative atomic replace.
        try:
            dest_st = os.stat(leaf_name, dir_fd=dest_dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            dest_st = None
        if dest_st is not None:
            if stat.S_ISLNK(dest_st.st_mode):
                raise SafeFsError(
                    "Destination path cannot be a symlink",
                    code="symlink_rejected",
                )
            if not stat.S_ISREG(dest_st.st_mode):
                raise SafeFsError(
                    "Destination path is not a regular file",
                    code="symlink_rejected",
                )

        try:
            os.replace(
                created_tmp_name,
                leaf_name,
                src_dir_fd=dest_dir_fd,
                dst_dir_fd=dest_dir_fd,
            )
        except OSError as exc:
            if exc.errno in (*_SYMLINK_ERRNOS, errno.EISDIR):
                raise SafeFsError(
                    "Destination directory or symlink conflict prevented atomic publish",
                    code="symlink_rejected",
                ) from exc
            raise SafeFsError(
                f"Atomic rename failed: {exc.errno}",
                code="write_failed",
            ) from exc

        # Ownership moved to `leaf_name`; do not unlink `created_tmp_name` in `finally`.
        created_tmp_name = None

        fd_st_after = os.fstat(tmp_fd)
        os.close(tmp_fd)
        tmp_fd = None

        final_st = os.stat(leaf_name, dir_fd=dest_dir_fd, follow_symlinks=False)
        if (
            stat.S_ISLNK(final_st.st_mode)
            or not stat.S_ISREG(final_st.st_mode)
            or not stat.S_ISREG(fd_st_after.st_mode)
            or (fd_st_after.st_dev, fd_st_after.st_ino) != tmp_identity
            or (final_st.st_dev, final_st.st_ino) != tmp_identity
        ):
            raise SafeFsError(
                "Published artifact is not the verified regular temporary file",
                code="symlink_rejected",
            )

        chain.verify_intact()

        try:
            os.fsync(dest_dir_fd)
        except OSError as exc:
            if exc.errno not in _DIR_FSYNC_IGNORED_ERRNOS:
                raise SafeFsError(
                    f"Directory fsync failed: {exc.errno}",
                    code="write_failed",
                ) from exc

        return base_dir.joinpath(*parts)
    finally:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if created_tmp_name is not None and tmp_identity is not None:
            try:
                cleanup_st = os.stat(
                    created_tmp_name,
                    dir_fd=dest_dir_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISLNK(cleanup_st.st_mode)
                    and stat.S_ISREG(cleanup_st.st_mode)
                    and (cleanup_st.st_dev, cleanup_st.st_ino) == tmp_identity
                ):
                    os.unlink(created_tmp_name, dir_fd=dest_dir_fd)
            except OSError:
                pass
        chain.close()


def safe_unlink_contained_regular_file(
    base_dir: Path,
    relative_path: str,
    *,
    anchor_dir: Path | None = None,
    reject_base_symlink: bool = True,
) -> bool:
    """Unlink ``relative_path`` via ``dir_fd`` iff all parent dirs are real dirs and leaf is regular."""
    try:
        parts = normalize_strict_relative_parts(relative_path)
        chain = _open_verified_dir_chain(
            base_dir,
            parts[:-1],
            anchor_dir=anchor_dir,
            reject_base_symlink=reject_base_symlink,
            create_missing=False,
            allow_missing=True,
        )
    except SafeFsError:
        return False
    if chain is None:
        return False

    leaf_name = parts[-1]
    try:
        try:
            st = os.stat(leaf_name, dir_fd=chain.leaf_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return False
        os.unlink(leaf_name, dir_fd=chain.leaf_fd)
        return True
    except OSError:
        return False
    finally:
        chain.close()
