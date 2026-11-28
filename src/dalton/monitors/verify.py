"""Three-stage monitor publish verification, candidate HMAC probing, and sibling JSON discovery.

Stage 1 (``db.stage_monitor_candidate_version_tx``) stages ``artifact_versions`` /
``artifact_version_files`` without moving any visitor-facing pointer.
Stage 2 (``verify_candidate_monitor_version``) completes ALL verification BEFORE the
promotion transaction:
- Reads actual entry bytes and computes SHA-256 against ``snap.entry_sha256``;
- Parses the entry (and referenced stylesheets) to verify every required local asset
  (JS, CSS, page assets — including references outside ``snap.files``) exists and
  matches SHA-256 in ``artifact_version_files`` / blob storage;
- Verifies every dependency file in ``snap.files[1:]``;
- For ``visibility == 'public'``, probes the public detail endpoint, public entry file
  endpoint, and required referenced JS/CSS endpoints via the real ASGI app using a
  short-lived HMAC ``X-Dalton-Candidate-Probe`` header signed by
  ``server_keys['monitor_grant']``.
Stage 3 (``db.promote_verified_monitor_version_tx`` / ``promote_verified_monitor_batch_tx``)
atomically promotes visitor pointers under three fences (``claim_epoch``,
``share_links.revision`` / ``audience`` / ``acl_revision``, and in-tx ``superseded``)
with zero fallible post-commit steps.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace
import hashlib
import logging
import posixpath
import re
import shlex
import sys
from typing import Any
from urllib.parse import quote, unquote

import httpx

from src.dalton.monitors.models import MonitorVisibility
from src.server.database import artifacts as artifacts_db
from src.server.database.artifacts.versions import StoredVersionFile, VersionFile
from src.server.database.blob_keys import blob_key
from src.server.database.workspace_file_blobs import blob_size, fetch_blob
from src.server.preview_proxy.artifacts import _charset_is_utf8, _content_type, _is_html
from src.server.preview_proxy.page_bridge import bridge_for, with_bridge
from src.server.services.artifacts.manifest import (
    MAX_BYTES,
    MAX_FILES,
    ManifestEntry,
    _names_origin_root,
)
from src.server.services.artifacts.reader import Reader
from src.server.services.artifacts.references import (
    REASON_LINK,
    REASON_PAGE,
    REASON_STYLE,
    file_kind,
    file_references,
    url_path,
)
from src.server.services.served_paths import may_be_artifact
from src.server.services.signed_grants import COUNT, GrantCodec
from src.utils.storage import does_object_exist, is_storage_enabled

logger = logging.getLogger(__name__)

CANDIDATE_PROBE_HEADER = "X-Dalton-Candidate-Probe"
CANDIDATE_PROBE_TTL_SECONDS = 30
SMALL_VERIFY_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB
SMALL_JSON_VERIFY_MAX_BYTES = SMALL_VERIFY_MAX_BYTES
REASON_SIBLING_JSON = "sibling_json"
MAX_DEPENDENCY_VERIFY_DEPTH = 8

_REQUIRED_LOAD_REASONS = frozenset({REASON_PAGE, REASON_STYLE})
_JS_MODULE_SUFFIXES = (".js", ".mjs", ".cjs")
_PROBE_ASSET_SUFFIXES = (".js", ".mjs", ".cjs", ".css")
_BYTE_VERIFY_SUFFIXES = (".json", ".js", ".mjs", ".cjs", ".css")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_FORBIDDEN_ENCODED_PATH_RE = re.compile(r"%2[ef]|%5c", re.IGNORECASE)
_JS_COMMENTS_OR_STRINGS_RE = re.compile(
    r"""(?P<str>"(?:\\[\s\S]|[^"\\\r\n])*"|'(?:\\[\s\S]|[^'\\\r\n])*'|`(?:\\[\s\S]|[^`\\])*`)|//[^\r\n]*|/\*[\s\S]*?\*/"""
)
_JS_STATIC_FROM_RE = re.compile(
    r"""(?:^|[;{}\r\n])\s*(?:import|export)(?:\s+|(?=[{*]))(?:[^;'"`]{1,1024}?)\bfrom\s*(['"])([^'"\r\n]{1,2048})\1""",
    re.MULTILINE,
)
_JS_SIDE_EFFECT_IMPORT_RE = re.compile(
    r"""(?:^|[;{}\r\n])\s*import\s*(['"])([^'"\r\n]{1,2048})\1""",
    re.MULTILINE,
)
_ARTIFACT_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


class CandidateProbeError(Exception):
    """Raised when an internal candidate probe token is invalid or expired."""


class MonitorVerificationError(RuntimeError):
    """Raised when a staged candidate monitor version fails pre-promotion verification."""


_CANDIDATE_PROBE_CODEC = GrantCodec(
    key_name="monitor_grant",
    version="cp1",
    claim_patterns=(_ARTIFACT_ID_RE, COUNT),
    error=CandidateProbeError,
    label="monitor candidate probe",
)


@dataclass(frozen=True)
class VerifiedCandidateProbe:
    artifact_id: str
    candidate_seq: int
    expires_at: int


@dataclass(frozen=True)
class VerifiedCandidateResult:
    artifact_id: str
    candidate_seq: int
    entry_path: str
    entry_sha256: str
    verified_dependencies: int
    public_probed: bool
    probed_assets: tuple[str, ...] = ()


async def mint_candidate_probe_token(
    artifact_id: str,
    candidate_seq: int,
    *,
    ttl: int = CANDIDATE_PROBE_TTL_SECONDS,
    now: int | None = None,
) -> str:
    """Mint a short-lived HMAC token bound to ``(artifact_id, candidate_seq)``."""
    token, _exp = await _CANDIDATE_PROBE_CODEC.mint(
        (str(artifact_id), str(int(candidate_seq))),
        ttl=ttl,
        now=now,
    )
    return token


async def verify_candidate_probe_token(
    token: str | None,
    *,
    expected_artifact_id: str | None = None,
    expected_seq: int | None = None,
    now: int | None = None,
) -> VerifiedCandidateProbe:
    """Verify an ``X-Dalton-Candidate-Probe`` header token."""
    (artifact_id, seq_str), expires_at = await _CANDIDATE_PROBE_CODEC.verify(
        token, now=now
    )
    seq = int(seq_str)
    if expected_artifact_id is not None and artifact_id != str(expected_artifact_id):
        raise CandidateProbeError("candidate probe artifact_id mismatch")
    if expected_seq is not None and seq != int(expected_seq):
        raise CandidateProbeError("candidate probe candidate_seq mismatch")
    return VerifiedCandidateProbe(
        artifact_id=artifact_id,
        candidate_seq=seq,
        expires_at=expires_at,
    )


async def head_blob(user_id: str, sha256: str) -> bool:
    """Verify an external blob exists and is readable in object storage (or registry when storage is off)."""
    if is_storage_enabled():
        key = blob_key(user_id, sha256)
        return bool(await asyncio.to_thread(does_object_exist, key))
    size = await blob_size(user_id, sha256)
    return size is not None


def _is_sibling_json_path(path: str, allowed_dirs: frozenset[str]) -> bool:
    clean = str(path or "").strip().lstrip("/")
    if not clean or not clean.lower().endswith(".json"):
        return False
    parent = posixpath.dirname(clean)
    return parent in allowed_dirs and may_be_artifact(clean)


def filter_sibling_json_paths(
    paths: Sequence[str],
    entry_path: str,
    *,
    existing_paths: set[str] | frozenset[str] = frozenset(),
    excluded_paths: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """Return sorted ``.json`` paths in ``entry_path``'s directory or its direct ``data/`` subdirectory.

    Paths listed in ``existing_paths`` or ``excluded_paths`` (publisher-excluded files) are omitted.
    """
    entry_dir = posixpath.dirname(str(entry_path or "").strip().lstrip("/"))
    data_dir = f"{entry_dir}/data" if entry_dir else "data"
    allowed_dirs = frozenset({entry_dir, data_dir})
    seen = {str(p or "").strip().lstrip("/") for p in existing_paths}
    excluded = {str(p or "").strip().lstrip("/") for p in excluded_paths}
    matched: list[str] = []
    for raw in sorted(paths):
        clean = str(raw or "").strip().lstrip("/")
        if clean in seen or clean in excluded:
            continue
        if _is_sibling_json_path(clean, allowed_dirs):
            seen.add(clean)
            matched.append(clean)
    return matched


async def _list_sandbox_sibling_json(
    sandbox_reader: Any,
    entry_dir: str,
    data_dir: str,
) -> list[str]:
    """Enumerate direct ``*.json`` files in ``entry_dir`` and ``data_dir`` on a live sandbox reader.

    Handles root-level entries (``entry_dir == ""``) via ``contained_listing_path`` so root
    ``index.html`` discovers sibling JSON files at the workspace root as well as ``data/*.json``.
    """
    from src.server.services.workspace_file_containment import (
        contained_listing_path,
        is_within,
    )
    from src.server.services.workspace_files import to_client_path

    sandbox = getattr(sandbox_reader, "_sandbox", None)
    work_dir = getattr(sandbox_reader, "_work_dir", None)
    if sandbox is None or not work_dir:
        return []
    base_dir = str(work_dir).rstrip("/") or "/"
    dirs_to_scan: list[str] = []
    seen_dirs: set[str] = set()
    for rel_dir in (entry_dir, data_dir):
        listed = contained_listing_path(rel_dir, base_dir)
        if listed is None:
            continue
        abs_dir = base_dir if listed == "" else f"{base_dir}/{listed}"
        if abs_dir in seen_dirs:
            continue
        if is_within(base_dir, abs_dir) and sandbox.validate_path(abs_dir):
            seen_dirs.add(abs_dir)
            dirs_to_scan.append(abs_dir)
    if not dirs_to_scan:
        return []
    quoted = " ".join(shlex.quote(d) for d in dirs_to_scan)
    cmd = (
        f"for d in {quoted}; do "
        'if [ -d "$d" ]; then '
        'for f in "$d"/*.json; do '
        '[ -f "$f" ] && printf "%s\\n" "$f"; '
        "done; fi; done"
    )
    try:
        res = await sandbox.runtime.exec(cmd, timeout=10)
    except Exception:
        logger.debug("Sibling JSON sandbox scan failed for %s", entry_dir, exc_info=True)
        return []
    if getattr(res, "exit_code", None) != 0:
        return []
    out: list[str] = []
    for line in str(getattr(res, "stdout", "") or "").splitlines():
        raw_abs = line.strip()
        if not raw_abs:
            continue
        rel = to_client_path(sandbox, raw_abs, work_dir)
        if rel:
            out.append(rel)
    return out


async def discover_sibling_json_entries(
    reader: Reader,
    entry_path: str,
    *,
    existing_paths: set[str],
    excluded_paths: set[str] | frozenset[str] = frozenset(),
    excluded_out: list[ManifestEntry] | None = None,
    max_files: int = MAX_FILES,
    max_file_bytes: int = MAX_BYTES,
) -> list[ManifestEntry]:
    """Discover same-directory and direct ``data/`` ``.json`` files for a monitor snapshot.

    Never returns files in ``excluded_paths``. When ``excluded_out`` is provided, existing
    excluded sibling JSONs are appended there so ``left_out_files`` stays preserved across cuts.
    """
    files_src = reader.files
    entry_dir = posixpath.dirname(str(entry_path or "").strip().lstrip("/"))
    data_dir = f"{entry_dir}/data" if entry_dir else "data"

    raw_candidates: list[str] = []
    if hasattr(files_src, "list_primary_artifact_files"):
        raw_candidates = list(files_src.list_primary_artifact_files())
    elif hasattr(files_src, "list_artifact_files"):
        raw_candidates = list(files_src.list_artifact_files())
    elif hasattr(files_src, "list_files"):
        listed = files_src.list_files()
        if asyncio.iscoroutine(listed):
            listed = await listed
        raw_candidates = list(listed or ())
    elif hasattr(files_src, "_workspace_id"):
        from src.server.services.persistence.file import FilePersistenceService

        sizes = getattr(files_src, "_sizes", None)
        if isinstance(sizes, dict):
            raw_candidates = list(sizes.keys())
        else:
            tree = await FilePersistenceService.get_file_tree(str(files_src._workspace_id))
            raw_candidates = [str(item.get("path") or "") for item in tree]
    elif hasattr(files_src, "_sandbox") and hasattr(files_src, "_work_dir"):
        raw_candidates = await _list_sandbox_sibling_json(files_src, entry_dir, data_dir)

    excluded_norm = {str(p or "").strip().lstrip("/") for p in excluded_paths if str(p or "").strip()}
    candidates = filter_sibling_json_paths(
        raw_candidates,
        entry_path,
        existing_paths=existing_paths,
        excluded_paths=excluded_norm,
    )

    if excluded_out is not None and excluded_norm:
        all_siblings = filter_sibling_json_paths(
            raw_candidates, entry_path, existing_paths=existing_paths
        )
        excluded_matches = [p for p in all_siblings if p in excluded_norm]
        if excluded_matches:
            try:
                ex_stats = await files_src.stat(excluded_matches)
            except Exception:
                ex_stats = {}
            seen_ex = {e.path for e in excluded_out}
            for req in excluded_matches:
                hit = ex_stats.get(req)
                canonical, size = hit if hit is not None else (req, None)
                if canonical not in seen_ex:
                    seen_ex.add(canonical)
                    excluded_out.append(
                        ManifestEntry(canonical, int(size) if size is not None else None, REASON_SIBLING_JSON, optional=True)
                    )

    if not candidates:
        return []

    remaining_slots = max(0, max_files - len(existing_paths))
    if remaining_slots <= 0:
        return []
    candidates = candidates[:remaining_slots]

    try:
        stat_hits = await files_src.stat(candidates)
    except Exception:
        logger.debug("Sibling JSON stat failed for %s", entry_path, exc_info=True)
        return []

    discovered: list[ManifestEntry] = []
    seen = {str(p or "").strip().lstrip("/") for p in existing_paths}
    for req in candidates:
        hit = stat_hits.get(req)
        if hit is None:
            continue
        canonical, size = hit
        if (
            canonical in seen
            or canonical in excluded_norm
            or req in excluded_norm
            or size > max_file_bytes
            or not may_be_artifact(canonical)
        ):
            if excluded_out is not None and (canonical in excluded_norm or req in excluded_norm):
                if canonical not in {e.path for e in excluded_out}:
                    excluded_out.append(
                        ManifestEntry(canonical, int(size), REASON_SIBLING_JSON, optional=True)
                    )
            continue
        seen.add(canonical)
        discovered.append(ManifestEntry(canonical, int(size), REASON_SIBLING_JSON, optional=True))
    return discovered


def monitor_page_matches_reviewed(snapshot: Any, reviewed_page: str) -> bool:
    """Return True if ``reviewed_page`` matches ``snapshot.page_sha256`` or its walked-only subset.

    When web ``PublishDialog`` submits ``page_sha256`` computed by upstream ``snapshot.status()``
    (which does not include auto-discovered sibling JSONs), comparing the walked-only digest
    allows non-excluded sibling JSONs to join the monitor snapshot while still rejecting any
    drift in the walked page/script/style files.
    """
    if snapshot.page_sha256 == reviewed_page:
        return True
    from src.server.services.artifacts.snapshot import _drives_loads

    digest = hashlib.sha256()
    for entry, sealed in zip(snapshot.entries, snapshot.files):
        if entry.reason == REASON_SIBLING_JSON:
            continue
        if not _drives_loads(entry.path) or (
            entry.optional and entry.reason == REASON_LINK
        ):
            continue
        digest.update(f"{entry.path}\0{sealed.sha256}\n".encode())
    return digest.hexdigest() == reviewed_page


class _MonitorWalkedSource:
    """Wrapper around ``snapshot._Walked`` that merges non-excluded same-directory and ``data/*.json`` files."""

    def __init__(self, base: Any) -> None:
        self._base = base
        self.caps = base.caps

    async def manifest(self, reader: Reader, artifact: Any) -> Any:
        found = await self._base.manifest(reader, artifact)
        entries = list(found.entries)
        left_out = list(found.left_out)
        seen = {e.path for e in entries}
        excluded = set(getattr(artifact, "left_out_files", ()) or ()) | {
            e.path for e in left_out
        }
        excluded_siblings: list[ManifestEntry] = []
        extras = await discover_sibling_json_entries(
            reader,
            artifact.path,
            existing_paths=seen,
            excluded_paths=excluded,
            excluded_out=excluded_siblings,
        )
        if not extras and not excluded_siblings:
            return found
        entries.extend(extras)
        existing_left_out_paths = {e.path for e in left_out}
        for ex in excluded_siblings:
            if ex.path not in existing_left_out_paths:
                left_out.append(ex)
                existing_left_out_paths.add(ex.path)
        byte_total = sum({e.path: (e.size or 0) for e in entries}.values())
        return replace(found, entries=entries, left_out=left_out, byte_total=byte_total)


def monitor_walked_source(base: Any) -> _MonitorWalkedSource:
    """Wrap a workspace snapshot walk source with sibling ``.json`` discovery for monitors."""
    return _MonitorWalkedSource(base)


def _strip_js_comments(source: str) -> str:
    """Remove ``//`` and ``/* ... */`` comments while preserving JS string and template literals."""
    return _JS_COMMENTS_OR_STRINGS_RE.sub(
        lambda m: m.group("str") if m.group("str") is not None else " ",
        source,
    )


def _extract_js_static_imports(raw_bytes: bytes) -> list[str]:
    """Extract relative static ES module ``import`` / ``export ... from`` specifiers."""
    text = raw_bytes.decode("utf-8", errors="replace").lstrip("\ufeff")
    stripped = _strip_js_comments(text)
    specifiers: list[str] = []
    seen: set[str] = set()
    for pattern in (_JS_STATIC_FROM_RE, _JS_SIDE_EFFECT_IMPORT_RE):
        for match in pattern.finditer(stripped):
            spec = match.group(2).strip()
            if not spec:
                continue
            # Only static relative module specifiers (./ or ../); ignore bare package names & URLs.
            if not (spec.startswith(("./", "../")) or spec in (".", "..")):
                continue
            if spec not in seen:
                seen.add(spec)
                specifiers.append(spec)
    return specifiers


def _resolve_visitor_reference(
    raw_ref: str,
    referrer_visitor_path: str,
) -> tuple[str | None, bool]:
    """Resolve ``raw_ref`` as the visitor browser resolves it under ``/v/{seq}/``.

    Returns ``(resolved_visitor_path, escaped_version_root)``.
    - External URLs, protocol-relative URLs, fragments, and origin-root refs return ``(None, False)``.
    - Relative references that climb outside ``/v/{seq}/`` return ``(None, True)`` so verification
      rejects them rather than silently passing broken links that 404 for visitors.
    """
    raw = (raw_ref or "").strip()
    if not raw or raw.startswith("#"):
        return None, False
    if _SCHEME_RE.match(raw) and not raw.lower().startswith("file:"):
        return None, False
    if _names_origin_root(raw):
        return None, False
    cleaned = url_path(raw.split("#", 1)[0].split("?", 1)[0])
    if cleaned.startswith("//"):
        return None, False
    # Reject percent-encoded path separators/dots (%2F, %5C, %2E) directly without decoding/folding,
    # since browsers preserve them and the file route rejects decoded '..' or invalid segments with 404.
    if _FORBIDDEN_ENCODED_PATH_RE.search(cleaned):
        return None, True
    decoded = unquote(cleaned)
    if not decoded or "\x00" in decoded:
        return None, False
    base_dir = posixpath.dirname(referrer_visitor_path)
    joined = posixpath.normpath(posixpath.join(base_dir, decoded))
    if joined in (".", "..") or joined.startswith(("/", "../")):
        return None, True
    if not may_be_artifact(joined):
        return None, False
    return joined, False


def _extract_visitor_required_references(
    stored_path: str,
    visitor_path: str,
    raw_bytes: bytes,
) -> tuple[list[str], list[str]]:
    """Extract required local references resolved against ``visitor_path`` under ``/v/{seq}/``.

    Supports HTML (``REASON_PAGE``/``REASON_STYLE``), CSS (``@import`` / ``url()``), and JS
    modules (static relative ``import`` / ``export ... from``). Returns ``(visitor_refs, escaped_raw_refs)``.
    """
    kind = file_kind(stored_path)
    raw_refs: list[str] = []
    if kind in ("html", "css"):
        for raw_ref, reason in dict.fromkeys(file_references(kind, raw_bytes, follow_links=False)):
            if reason in _REQUIRED_LOAD_REASONS:
                raw_refs.append(raw_ref)
    elif stored_path.lower().endswith(_JS_MODULE_SUFFIXES):
        raw_refs = _extract_js_static_imports(raw_bytes)
    else:
        return [], []

    resolved_refs: list[str] = []
    escaped_refs: list[str] = []
    seen: set[str] = set()
    for raw_ref in raw_refs:
        resolved, escaped = _resolve_visitor_reference(raw_ref, visitor_path)
        if escaped:
            escaped_refs.append(raw_ref)
            continue
        if resolved is None or resolved == visitor_path:
            continue
        if resolved not in seen:
            seen.add(resolved)
            resolved_refs.append(resolved)
    return resolved_refs, escaped_refs


def _extract_required_local_references(path: str, raw_bytes: bytes) -> list[str]:
    """Extract required local relative references (JS, CSS, page assets) loaded by ``path``.

    Uses upstream ``file_references`` + ``resolve_ref`` filtered strictly to required load
    reasons (``REASON_PAGE``, ``REASON_STYLE``), ignoring external URLs, origin-root paths,
    and optional hyperlinks/script guesses.
    """
    resolved, _escaped = _extract_visitor_required_references(path, path, raw_bytes)
    return resolved


def _build_internal_monitor_asgi_app() -> Any:
    """Return the loaded production FastAPI app when available, else a minimal app with monitors router."""
    setup_mod = sys.modules.get("src.server.app.setup")
    live_app = getattr(setup_mod, "app", None) if setup_mod is not None else None
    if live_app is not None:
        return live_app

    from fastapi import FastAPI

    from src.dalton.monitors.router import router as monitors_router

    probe_app = FastAPI()
    probe_app.include_router(monitors_router)
    return probe_app


async def _default_asgi_get(
    path: str, *, headers: dict[str, str] | None = None
) -> httpx.Response:
    app = _build_internal_monitor_asgi_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        timeout=15.0,
    ) as client:
        return await client.get(path, headers=headers)


def _encode_url_path(path: str) -> str:
    return "/".join(quote(seg, safe="") for seg in str(path or "").lstrip("/").split("/") if seg)


async def _read_stored_bytes(stored: StoredVersionFile) -> bytes:
    if stored.content is not None:
        return stored.content
    return await fetch_blob(stored.user_id, stored.sha256)


async def _verify_stored_dependency(
    *,
    artifact_id: str,
    candidate_seq: int,
    dep_path: str,
    expected_sha256: str | None = None,
) -> tuple[StoredVersionFile, bytes | None]:
    """Verify one staged dependency file exists, matches SHA-256, and has readable bytes/blob."""
    dep_row = await artifacts_db.get_version_file(artifact_id, candidate_seq, dep_path)
    if dep_row is None:
        raise MonitorVerificationError(
            f"Candidate v{candidate_seq} dependency missing in DB: {dep_path}"
        )
    if expected_sha256 is not None and dep_row.sha256 != expected_sha256:
        raise MonitorVerificationError(
            f"Candidate v{candidate_seq} dependency SHA-256 mismatch for {dep_path}: "
            f"stored={dep_row.sha256} expected={expected_sha256}"
        )
    loaded_bytes: bytes | None = None
    if dep_row.content is not None:
        dep_actual = hashlib.sha256(dep_row.content).hexdigest()
        if dep_actual != dep_row.sha256:
            raise MonitorVerificationError(
                f"Candidate v{candidate_seq} inline dependency byte hash mismatch for {dep_path}"
            )
        loaded_bytes = dep_row.content
    elif dep_path.lower().endswith(_BYTE_VERIFY_SUFFIXES) and dep_row.byte_len <= SMALL_VERIFY_MAX_BYTES:
        try:
            dep_bytes = await fetch_blob(dep_row.user_id, dep_row.sha256)
        except Exception as exc:
            raise MonitorVerificationError(
                f"Candidate v{candidate_seq} dependency blob unreadable ({dep_path}): {exc}"
            ) from exc
        dep_actual = hashlib.sha256(dep_bytes).hexdigest()
        if dep_actual != dep_row.sha256:
            raise MonitorVerificationError(
                f"Candidate v{candidate_seq} dependency byte hash mismatch for {dep_path}"
            )
        loaded_bytes = dep_bytes
    else:
        readable = await head_blob(dep_row.user_id, dep_row.sha256)
        if not readable:
            raise MonitorVerificationError(
                f"Candidate v{candidate_seq} external dependency blob missing ({dep_path})"
            )
    return dep_row, loaded_bytes


async def verify_candidate_monitor_version(
    *,
    artifact_id: str,
    candidate_seq: int,
    entry_path: str,
    snap_files: Sequence[VersionFile],
    snap_entry_sha256: str,
    visibility: MonitorVisibility,
    slug: str,
    probe_public_route: bool = True,
    probe_get: Any = None,
    max_dep_depth: int = MAX_DEPENDENCY_VERIFY_DEPTH,
) -> VerifiedCandidateResult:
    """Complete pre-promotion verification before moving any visitor-facing monitor pointer.

    1. Reads actual entry bytes (inline or external blob), verifies SHA-256 against
       ``snap_entry_sha256``, verifies the raw entry is non-empty BEFORE bridge injection,
       and verifies ``with_bridge`` produces valid non-empty UTF-8 for UTF-8 HTML.
    2. Verifies every file in ``snap_files[1:]`` and recursively walks required local
       references (HTML -> JS/CSS/page assets, CSS -> ``@import``/``url()``, JS modules ->
       static relative ``import``/``export ... from``) up to ``max_dep_depth``, strictly
       within ``(artifact_id, candidate_seq)``. Relative references are resolved against the
       actual visitor URL path (``/v/{seq}/`` for public visitors; ``/v/{seq}/{entry_path}``
       for authenticated visitors), failing if any relative reference escapes ``/v/{seq}/``.
    3. For ``visibility == 'public'`` (when ``probe_public_route`` is True), runs complete
       pre-promotion readback through the public routes with a short-lived HMAC
       ``X-Dalton-Candidate-Probe`` header:
       - ``GET /api/v1/dalton/monitors/public/{slug}`` (HTTP 200, ``Cache-Control: no-cache``,
         ``monitor.last_verified_seq == candidate_seq``);
       - ``GET /api/v1/dalton/monitors/public/{slug}/v/{candidate_seq}/``
         (HTTP 200, ``Cache-Control: private, no-cache``, ``ETag`` prefix ``"{stored.sha256}``,
         non-empty body);
       - ``GET /api/v1/dalton/monitors/public/{slug}/v/{candidate_seq}/{visitor_asset_path}``
         for every required referenced JS/CSS asset (HTTP 200, matching ``ETag`` prefix).
    """
    if candidate_seq <= 0:
        raise MonitorVerificationError(f"Invalid candidate_seq={candidate_seq}")

    stored_entry = await artifacts_db.get_version_file(
        artifact_id, candidate_seq, entry_path
    )
    if stored_entry is None:
        raise MonitorVerificationError(
            f"Candidate v{candidate_seq} entry file missing in DB: {entry_path}"
        )

    try:
        raw_bytes = await _read_stored_bytes(stored_entry)
    except Exception as exc:
        raise MonitorVerificationError(
            f"Candidate v{candidate_seq} entry bytes unreadable ({entry_path}): {exc}"
        ) from exc

    actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if actual_sha256 != stored_entry.sha256 or stored_entry.sha256 != snap_entry_sha256:
        raise MonitorVerificationError(
            f"Candidate v{candidate_seq} entry SHA-256 mismatch for {entry_path}: "
            f"actual={actual_sha256} stored={stored_entry.sha256} expected={snap_entry_sha256}"
        )

    # Check non-empty raw entry BEFORE injecting the page bridge (which would otherwise pad empty HTML).
    if not raw_bytes or not raw_bytes.strip():
        raise MonitorVerificationError(
            f"Candidate v{candidate_seq} entry file is empty ({entry_path})"
        )

    content_type = _content_type(stored_entry.content_type)
    if _is_html(content_type) and _charset_is_utf8(content_type):
        try:
            bridged = await asyncio.to_thread(with_bridge, raw_bytes, bridge_for("view"))
            if not bridged:
                raise MonitorVerificationError(
                    f"Candidate v{candidate_seq} bridged HTML body is empty ({entry_path})"
                )
            bridged.decode("utf-8")
        except MonitorVerificationError:
            raise
        except Exception as exc:
            raise MonitorVerificationError(
                f"Candidate v{candidate_seq} bridged HTML validation failed ({entry_path}): {exc}"
            ) from exc

    # Verify every dependency file in snap_files[1:] AND recursively verify every required
    # local reference (HTML -> CSS/JS/assets, CSS -> @import/url, JS -> static relative import)
    # resolved identically to how visitors load /v/{seq}/.
    snap_by_path: dict[str, VersionFile] = {f.path: f for f in snap_files[1:]}
    verified_rows: dict[str, StoredVersionFile] = {}
    verified_bytes: dict[str, bytes] = {}
    verified_deps = 0

    for dep in snap_files[1:]:
        dep_row, dep_loaded = await _verify_stored_dependency(
            artifact_id=artifact_id,
            candidate_seq=candidate_seq,
            dep_path=dep.path,
            expected_sha256=dep.sha256,
        )
        verified_rows[dep.path] = dep_row
        if dep_loaded is not None:
            verified_bytes[dep.path] = dep_loaded
        verified_deps += 1

    # Public iframe loads /public/{slug}/v/{seq}/ (virtual base ""), while authenticated
    # iframe loads /g/{grant}/v/{seq}/{entry_path}.
    entry_visitor_path = "" if visibility == "public" else entry_path
    entry_dir = posixpath.dirname(entry_path)

    async def _resolve_and_verify_visitor_asset(
        ref_visitor_path: str,
        referrer_label: str,
    ) -> tuple[str, StoredVersionFile, bytes | None]:
        nonlocal verified_deps
        candidate_paths = [ref_visitor_path]
        if visibility == "public" and entry_dir:
            nested = posixpath.normpath(f"{entry_dir}/{ref_visitor_path}")
            if (
                nested != ref_visitor_path
                and nested.startswith(f"{entry_dir}/")
                and may_be_artifact(nested)
            ):
                candidate_paths.append(nested)

        # Match serve_public_monitor_file order: try exact visitor path first, then entry_dir fallback.
        for cand_path in candidate_paths:
            if cand_path in verified_rows:
                return cand_path, verified_rows[cand_path], verified_bytes.get(cand_path)
            existing_row = await artifacts_db.get_version_file(
                artifact_id, candidate_seq, cand_path
            )
            if existing_row is not None:
                expected_sha = (
                    snap_by_path[cand_path].sha256 if cand_path in snap_by_path else None
                )
                dep_row, dep_loaded = await _verify_stored_dependency(
                    artifact_id=artifact_id,
                    candidate_seq=candidate_seq,
                    dep_path=cand_path,
                    expected_sha256=expected_sha,
                )
                verified_rows[cand_path] = dep_row
                if dep_loaded is not None:
                    verified_bytes[cand_path] = dep_loaded
                verified_deps += 1
                return cand_path, dep_row, dep_loaded

        raise MonitorVerificationError(
            f"Candidate v{candidate_seq} missing or invalid required referenced asset "
            f"'{ref_visitor_path}' referenced by {referrer_label}"
        )

    entry_refs, entry_escaped = await asyncio.to_thread(
        _extract_visitor_required_references, entry_path, entry_visitor_path, raw_bytes
    )
    if entry_escaped:
        raise MonitorVerificationError(
            f"Candidate v{candidate_seq} required reference '{entry_escaped[0]}' in "
            f"'{entry_path}' resolves outside version root '/v/{candidate_seq}/'"
        )

    required_refs_queue: list[tuple[str, int, str]] = [
        (ref_vpath, 1, entry_path) for ref_vpath in entry_refs
    ]
    required_visitor_paths_seen: set[str] = set()
    probed_asset_candidates: list[tuple[str, str]] = []
    depth_limit = max(1, int(max_dep_depth))

    while required_refs_queue:
        ref_visitor_path, depth, referrer_label = required_refs_queue.pop(0)
        if ref_visitor_path in required_visitor_paths_seen or ref_visitor_path == entry_visitor_path:
            continue
        required_visitor_paths_seen.add(ref_visitor_path)

        stored_path, dep_row, dep_bytes = await _resolve_and_verify_visitor_asset(
            ref_visitor_path, referrer_label
        )

        if ref_visitor_path.lower().endswith(_PROBE_ASSET_SUFFIXES):
            probed_asset_candidates.append((ref_visitor_path, stored_path))

        is_css = file_kind(stored_path) == "css"
        is_js_mod = stored_path.lower().endswith(_JS_MODULE_SUFFIXES)
        if (is_css or is_js_mod) and depth < depth_limit:
            if dep_bytes is None:
                try:
                    dep_bytes = await _read_stored_bytes(dep_row)
                    verified_bytes[stored_path] = dep_bytes
                except Exception as exc:
                    raise MonitorVerificationError(
                        f"Candidate v{candidate_seq} referenced asset unreadable ({stored_path}): {exc}"
                    ) from exc
            sub_refs, sub_escaped = await asyncio.to_thread(
                _extract_visitor_required_references,
                stored_path,
                ref_visitor_path,
                dep_bytes,
            )
            if sub_escaped:
                raise MonitorVerificationError(
                    f"Candidate v{candidate_seq} required reference '{sub_escaped[0]}' in "
                    f"'{stored_path}' resolves outside version root '/v/{candidate_seq}/'"
                )
            for sub_vpath in sub_refs:
                if (
                    sub_vpath not in required_visitor_paths_seen
                    and sub_vpath != entry_visitor_path
                ):
                    required_refs_queue.append((sub_vpath, depth + 1, stored_path))

    public_probed = False
    probed_assets: list[str] = []
    if visibility == "public" and probe_public_route:
        token = await mint_candidate_probe_token(artifact_id, candidate_seq)
        getter = probe_get or _default_asgi_get
        slug_quoted = quote(slug, safe="")
        probe_headers = {CANDIDATE_PROBE_HEADER: token}

        # 1. Pre-promotion public detail readback for candidate_seq
        detail_url = f"/api/v1/dalton/monitors/public/{slug_quoted}"
        detail_resp = await getter(detail_url, headers=probe_headers)
        if detail_resp.status_code != 200:
            raise MonitorVerificationError(
                f"Candidate v{candidate_seq} public detail probe failed: "
                f"HTTP {detail_resp.status_code} ({detail_url})"
            )
        detail_cc = (detail_resp.headers.get("cache-control") or "").strip().lower()
        if "no-cache" not in detail_cc:
            raise MonitorVerificationError(
                f"Candidate v{candidate_seq} public detail probe Cache-Control mismatch: got {detail_cc!r}"
            )
        detail_body = detail_resp.json()
        mon = detail_body.get("monitor") if isinstance(detail_body, dict) else None
        verified_seq = mon.get("last_verified_seq") if isinstance(mon, dict) else None
        if not isinstance(verified_seq, int) or verified_seq != candidate_seq:
            raise MonitorVerificationError(
                f"Candidate v{candidate_seq} public detail probe last_verified_seq mismatch: "
                f"got {verified_seq!r}, expected {candidate_seq}"
            )

        # 2. Pre-promotion public entry readback using the exact visitor iframe URL /v/{seq}/
        probe_url = f"/api/v1/dalton/monitors/public/{slug_quoted}/v/{candidate_seq}/"
        resp = await getter(probe_url, headers=probe_headers)
        if resp.status_code != 200:
            raise MonitorVerificationError(
                f"Candidate v{candidate_seq} public probe failed: HTTP {resp.status_code} ({probe_url})"
            )
        cc = (resp.headers.get("cache-control") or "").strip().lower()
        if cc != "private, no-cache":
            raise MonitorVerificationError(
                f"Candidate v{candidate_seq} public probe Cache-Control mismatch: got {cc!r}"
            )
        etag = (resp.headers.get("etag") or "").strip()
        expected_prefix = f'"{stored_entry.sha256}'
        if not etag.startswith(expected_prefix):
            raise MonitorVerificationError(
                f"Candidate v{candidate_seq} public probe ETag mismatch: got {etag!r}, expected prefix {expected_prefix!r}"
            )
        if not resp.content:
            raise MonitorVerificationError(
                f"Candidate v{candidate_seq} public probe returned empty body ({probe_url})"
            )

        # 3. Pre-promotion public readback for required referenced JS/CSS assets using visitor URLs
        for visitor_asset_path, stored_asset_path in probed_asset_candidates:
            asset_row = verified_rows[stored_asset_path]
            encoded_asset = _encode_url_path(visitor_asset_path)
            asset_url = (
                f"/api/v1/dalton/monitors/public/{slug_quoted}"
                f"/v/{candidate_seq}/{encoded_asset}"
            )
            asset_resp = await getter(asset_url, headers=probe_headers)
            if asset_resp.status_code != 200:
                raise MonitorVerificationError(
                    f"Candidate v{candidate_seq} required asset public probe failed: "
                    f"HTTP {asset_resp.status_code} ({asset_url})"
                )
            asset_etag = (asset_resp.headers.get("etag") or "").strip()
            asset_prefix = f'"{asset_row.sha256}'
            if not asset_etag.startswith(asset_prefix):
                raise MonitorVerificationError(
                    f"Candidate v{candidate_seq} required asset ETag mismatch for {visitor_asset_path}: "
                    f"got {asset_etag!r}, expected prefix {asset_prefix!r}"
                )
            probed_assets.append(visitor_asset_path)

        public_probed = True

    return VerifiedCandidateResult(
        artifact_id=artifact_id,
        candidate_seq=candidate_seq,
        entry_path=entry_path,
        entry_sha256=stored_entry.sha256,
        verified_dependencies=verified_deps,
        public_probed=public_probed,
        probed_assets=tuple(probed_assets),
    )


async def verify_public_monitor_readback(
    *,
    slug: str,
    candidate_seq: int,
    entry_path: str,
    expected_entry_sha256: str,
    probe_get: Any = None,
) -> None:
    """Standalone anonymous public readback helper (no internal headers; used by diagnostics & tests)."""
    getter = probe_get or _default_asgi_get
    slug_quoted = quote(slug, safe="")

    # 1. Anonymous public detail endpoint: HTTP 200, Cache-Control: no-cache, last_verified_seq >= candidate_seq
    detail_url = f"/api/v1/dalton/monitors/public/{slug_quoted}"
    detail_resp = await getter(detail_url, headers=None)
    if detail_resp.status_code != 200:
        raise MonitorVerificationError(
            f"Public detail readback failed for {slug}: HTTP {detail_resp.status_code}"
        )
    detail_cc = (detail_resp.headers.get("cache-control") or "").strip().lower()
    if "no-cache" not in detail_cc:
        raise MonitorVerificationError(
            f"Public detail readback missing Cache-Control no-cache for {slug}: got {detail_cc!r}"
        )
    detail_body = detail_resp.json()
    mon = detail_body.get("monitor") if isinstance(detail_body, dict) else None
    verified_seq = mon.get("last_verified_seq") if isinstance(mon, dict) else None
    if not isinstance(verified_seq, int) or verified_seq < candidate_seq:
        raise MonitorVerificationError(
            f"Public detail readback last_verified_seq mismatch for {slug}: "
            f"got {verified_seq!r}, expected >= {candidate_seq}"
        )

    # 2. Anonymous public file endpoint: HTTP 200 and ETag starts with "{expected_entry_sha256}
    encoded_entry = _encode_url_path(entry_path)
    file_url = (
        f"/api/v1/dalton/monitors/public/{slug_quoted}/v/{verified_seq}/{encoded_entry}"
    )
    file_resp = await getter(file_url, headers=None)
    if file_resp.status_code != 200:
        raise MonitorVerificationError(
            f"Public file readback failed for {slug} v{verified_seq}: HTTP {file_resp.status_code}"
        )
    file_etag = (file_resp.headers.get("etag") or "").strip()
    if verified_seq == candidate_seq:
        expected_prefix = f'"{expected_entry_sha256}'
        if not file_etag.startswith(expected_prefix):
            raise MonitorVerificationError(
                f"Public file readback ETag mismatch for {slug} v{verified_seq}: "
                f"got {file_etag!r}, expected prefix {expected_prefix!r}"
            )
    elif not file_etag.startswith('"'):
        raise MonitorVerificationError(
            f"Public file readback missing quoted ETag for {slug} v{verified_seq}: got {file_etag!r}"
        )
