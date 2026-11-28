#!/usr/bin/env python3
"""Pure logic and CLI helpers for Dalton offsite config & weekly data backups.

Stdlib-only so host launchd scripts work with either the repo venv or system Python 3.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fnmatch
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

# Secret filename suffixes/patterns (B2).
_SECRET_EXACT_NAMES = frozenset({".env", ".netrc"})
_SECRET_SUFFIXES = (".env", ".key", ".pem", ".p12", ".pfx", ".jks", ".keystore")
_SECRET_NAME_KEYWORDS = (
    "token",
    "secret",
    "password",
    "passwd",
    "passphrase",
    "credential",
    "api_key",
    "apikey",
    "access_key",
    "accesskey",
    "private_key",
    "cookie",
    "session",
    "bearer",
)

# Secret content patterns (used as fallback guard on allowlisted plaintext files).
_SECRET_KEY_FRAG = (
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL"
    r"|API_KEY|ACCESS_KEY|COOKIE|SESSION|BEARER)"
)
_RE_ENV_SECRET = re.compile(
    rf"(?im)^[ \t]*(?:export[ \t]+)?([A-Za-z0-9_.-]*{_SECRET_KEY_FRAG}[A-Za-z0-9_.-]*)[ \t]*=[ \t]*(.+?)[ \t]*$"
)
_RE_YAML_SECRET = re.compile(
    rf"(?im)^[ \t]*-?[ \t]*[\"']?([A-Za-z0-9_.-]*{_SECRET_KEY_FRAG}[A-Za-z0-9_.-]*)[\"']?[ \t]*:[ \t]*(?:\r?\n[ \t]+)?([^\r\n]+)"
)
_RE_YAML_INLINE_SECRET = re.compile(
    rf"(?i)(?:\{{|,)[ \t]*[\"']?([A-Za-z0-9_.-]*{_SECRET_KEY_FRAG}[A-Za-z0-9_.-]*)[\"']?[ \t]*:\s*(\"[^\"]*\"|'[^']*'|[^,\}}#\s]+)"
)
_RE_JSON_SECRET = re.compile(
    rf"(?i)\"([^\"]*{_SECRET_KEY_FRAG}[^\"]*)\"\s*:\s*\"([^\"]+)\""
)
_RE_JSON_NUM_SECRET = re.compile(
    rf"(?i)\"([^\"]*{_SECRET_KEY_FRAG}[^\"]*)\"\s*:\s*(-?\d+(?:\.\d+)?)\b"
)
_RE_PLIST_SECRET = re.compile(
    rf"(?is)<key>([^<]*{_SECRET_KEY_FRAG}[^<]*)</key>\s*<string>([^<]+)</string>"
)
_RE_NETRC_INLINE_SECRET = re.compile(
    r"(?im)\b(?:machine|default|login)\b.*?\b(password|passwd|passphrase|credential|secret|token)[ \t]+([^\s#]+)"
)
_RE_NETRC_LINE_SECRET = re.compile(
    rf"(?im)^[ \t]*([A-Za-z0-9_.-]*{_SECRET_KEY_FRAG}[A-Za-z0-9_.-]*)[ \t]+([^\s=#:\"'()\[\]{{}},;<>|&]+)[ \t]*$"
)

# Default-encrypt policy: ONLY files matching these source dir + basename globs may stay plaintext
# (and still pass through fallback secret scanning). Everything else is encrypted into .enc.
PLAINTEXT_ALLOWLIST_GLOBS: dict[str, tuple[str, ...]] = {
    "backup_scripts": ("*.sh", "*.py"),
    "launch_agents": ("com.dalton.*.plist",),
}

# The backup repo's own files at its root (not synced from the host). They stay
# plaintext so an existing repo keeps committing, and still pass the secret scan.
REPO_ROOT_PLAINTEXT = frozenset({"README.md", ".gitignore", ".gitattributes"})

_EMPTY_VALUE_LITERALS = frozenset({"", '""', "''", "null", "none", "~"})
_RE_PG_DUMP = re.compile(r"^dalton-pg-(\d{8}-\d{6})\.dump$")
_RE_WEEKLY_DATE_DIR = re.compile(r"^(\d{4}-\d{2}-\d{2})/?$")

MANIFEST_FILENAME = ".backup-enc-manifest.json"
STREAM_CAP_EXIT_CODE = 42
PROXY_ENV_VARS = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
)

DEFAULT_CONFIG_VAR_EXCLUDES = (
    "openclaw-data/temp",
    "openclaw-data/research-outputs",
    "pr_baseline",
    "tmp",
    ".dalton-runs",
    "backups",
    "*.dump",
    "*.sqlite",
    "*.sqlite-wal",
    "*.sqlite-shm",
    "venv",
    ".venv",
    "__pycache__",
    "*.pyc",
    "node_modules",
    ".dalton-freeze",
    "reextract",
    "*.db",
    "*.duckdb",
    ".DS_Store",
    ".dalton-outbox-*",
    "openclaw-data/skills/*/data",
    "openclaw-data/skills/*/output",
    "openclaw-data/skills/*/temp",
    "openclaw-data/skills/*/state",
    "openclaw-data/skills/*/events",
    "asr_jobs",
    "smoke",
    "trial-runs",
)

DEFAULT_DATA_VAR_EXCLUDES = (
    "venv",
    ".venv",
    "__pycache__",
    "*.pyc",
    "node_modules",
    ".DS_Store",
    "pr_baseline",
    "tmp",
    "openclaw-data/temp",
    "openclaw-data/skills/*/temp",
    "run-secrets",
    ".dalton-outbox-*",
)

SQLITE_FILE_HEADER = b"SQLite format 3\x00"


class MountCheckError(RuntimeError):
    """Raised when the external backup drive is missing, unmounted, or mismatched."""


class SqliteSnapshotError(RuntimeError):
    """Raised when online SQLite backup fails for one or more database files."""


@dataclasses.dataclass(frozen=True)
class SyncSourceSpec:
    """One logical source directory/pattern set to stage into the backup git repo."""

    name: str
    src_dir: Path
    dest_subdir: str
    excludes: tuple[str, ...] = ()
    include_globs: tuple[str, ...] = ()
    top_level_only: bool = False


@dataclasses.dataclass(frozen=True)
class StageSummary:
    """Counts and diagnostics produced by staging into the config backup repo."""

    plaintext_count: int
    encrypted_reused: int
    encrypted_new: int
    skipped_large: tuple[str, ...]
    skipped_excluded: int
    skipped_forbidden: tuple[str, ...]


def is_forbidden_key_file(path: Path, key_file: Path | None = None) -> bool:
    """Return True when ``path`` is the backup passphrase file that must never enter git."""
    name = path.name
    if name == "backup.key" or name.startswith("backup.key."):
        return True
    if key_file is not None:
        try:
            if path.resolve() == key_file.resolve():
                return True
        except OSError:
            if path.name == key_file.name:
                return True
    return False


def is_plaintext_allowlisted(dest_subdir: str, rel_path: str) -> bool:
    """Return True only when ``dest_subdir/rel_path`` matches the explicit plaintext allowlist."""
    globs = PLAINTEXT_ALLOWLIST_GLOBS.get(dest_subdir.strip("/"))
    if not globs:
        return False
    basename = Path(rel_path.replace("\\", "/")).name
    return any(fnmatch.fnmatch(basename, pat) for pat in globs)


def is_plaintext_allowlisted_repo_rel(repo_rel_path: str) -> bool:
    """Check whether a relative path inside the config backup git repo is plaintext-allowlisted."""
    posix_rel = repo_rel_path.replace("\\", "/").removeprefix("./")
    if "/" not in posix_rel:
        return posix_rel in REPO_ROOT_PLAINTEXT
    dest_subdir, inner_rel = posix_rel.split("/", 1)
    return is_plaintext_allowlisted(dest_subdir, inner_rel)


def _strip_quotes_and_comment(raw: str) -> tuple[str, bool]:
    val = raw.strip()
    if val.startswith('"'):
        end = val.find('"', 1)
        if end != -1:
            return val[1:end].strip(), True
    elif val.startswith("'"):
        end = val.find("'", 1)
        if end != -1:
            return val[1:end].strip(), True
    if " #" in val:
        val = val.split(" #", 1)[0].strip()
    if "\t#" in val:
        val = val.split("\t#", 1)[0].strip()
    return val, False


def detect_secret_reason(rel_path: str, data: bytes) -> str | None:
    """Return a non-empty reason string if filename or content looks like a secret.

    Returns ``None`` if the file is clean according to heuristic secret rules.
    """
    posix_rel = rel_path.replace("\\", "/")
    basename = Path(posix_rel).name
    lower_name = basename.lower()

    if lower_name in _SECRET_EXACT_NAMES or lower_name.startswith(".env."):
        return f"filename:{basename}"
    if any(lower_name.endswith(sfx) for sfx in _SECRET_SUFFIXES):
        return f"filename_suffix:{basename}"
    if any(kw in lower_name for kw in _SECRET_NAME_KEYWORDS):
        return f"filename_keyword:{basename}"

    if b"-----BEGIN" in data:
        return "content:pem_begin"

    if b"\x00" in data:
        return "content:binary_nul"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "content:non_utf8"

    for pat, label in (
        (_RE_ENV_SECRET, "content:env_secret"),
        (_RE_YAML_SECRET, "content:yaml_secret"),
        (_RE_YAML_INLINE_SECRET, "content:yaml_inline_secret"),
        (_RE_JSON_SECRET, "content:json_secret"),
        (_RE_JSON_NUM_SECRET, "content:json_numeric_secret"),
        (_RE_PLIST_SECRET, "content:plist_secret"),
        (_RE_NETRC_INLINE_SECRET, "content:netrc_secret"),
        (_RE_NETRC_LINE_SECRET, "content:netrc_line_secret"),
    ):
        for match in pat.finditer(text):
            line_or_val = match.group(2)
            full_line = match.group(0).lstrip()
            if full_line.startswith("#"):
                continue
            cleaned, was_quoted = _strip_quotes_and_comment(line_or_val)
            if cleaned.lower() in _EMPTY_VALUE_LITERALS:
                continue
            if not was_quoted and cleaned.startswith("#"):
                continue
            return f"{label}:{match.group(1)}"

    return None


def scan_plaintext_tree(
    repo_dir: Path,
    *,
    ignore_rel_paths: Iterable[str] = (),
) -> list[tuple[str, str]]:
    """Pre-commit guard for ``repo_dir``.

    Under the default-encrypt policy:
    1. ``.enc`` files are exempted ONLY when registered in ``.backup-enc-manifest.json``
       with a matching on-disk ciphertext SHA-256.
    2. ``backup.key*`` is always forbidden.
    3. Non-allowlisted plaintext files are rejected unconditionally.
    4. Allowlisted plaintext files still pass through ``detect_secret_reason``.
    """
    ignored = {p.replace("\\", "/").removeprefix("./") for p in ignore_rel_paths}
    ignored.add(MANIFEST_FILENAME)
    manifest = load_enc_manifest(repo_dir / MANIFEST_FILENAME)
    findings: list[tuple[str, str]] = []
    if not repo_dir.exists():
        return findings

    for root, dirs, files in os.walk(repo_dir, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        root_path = Path(root)
        for fname in sorted(files):
            fpath = root_path / fname
            if fpath.is_symlink():
                continue
            rel = fpath.relative_to(repo_dir).as_posix()
            if rel in ignored:
                continue
            if rel.endswith(".enc"):
                entry = manifest.get(rel)
                expected_cipher = (
                    entry.get("ciphertext_sha256") if isinstance(entry, dict) else None
                )
                if not isinstance(expected_cipher, str) or not expected_cipher:
                    findings.append((rel, "unverified_enc_not_in_manifest"))
                    continue
                try:
                    actual_cipher = sha256_file(fpath)
                except OSError as exc:
                    findings.append((rel, f"unreadable_enc:{type(exc).__name__}"))
                    continue
                if actual_cipher != expected_cipher:
                    findings.append((rel, "unverified_enc_hash_mismatch"))
                continue
            if is_forbidden_key_file(fpath):
                findings.append((rel, "forbidden_backup_key"))
                continue
            if not is_plaintext_allowlisted_repo_rel(rel):
                findings.append((rel, "non_allowlisted_plaintext"))
                continue
            try:
                data = fpath.read_bytes()
            except OSError as exc:
                findings.append((rel, f"unreadable:{type(exc).__name__}"))
                continue
            reason = detect_secret_reason(rel, data)
            if reason is not None:
                findings.append((rel, reason))
    return findings


def parse_pattern_list(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    """Parse a comma/newline/whitespace-delimited pattern setting into a tuple."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        tokens = [tok.strip() for tok in re.split(r"[\n,]+", raw) if tok.strip()]
        return tuple(tokens)
    return tuple(str(tok).strip() for tok in raw if str(tok).strip())


def _normalize_rel_path(raw: str) -> str:
    cleaned = raw.replace("\\", "/").strip("/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def matches_exclude(rel_path: str, patterns: Iterable[str]) -> bool:
    """Return True if ``rel_path`` (POSIX relative path) matches any exclude rule.

    Patterns containing ``/`` are anchored to the root directory (matching the path
    itself or any descendant); patterns without ``/`` match any path component.
    """
    rel = _normalize_rel_path(rel_path)
    if not rel:
        return False
    parts = rel.split("/")
    basename = parts[-1]
    for raw_pat in patterns:
        pat = _normalize_rel_path(raw_pat)
        if not pat:
            continue
        if "/" in pat:
            if (
                rel == pat
                or rel.startswith(pat + "/")
                or fnmatch.fnmatchcase(rel, pat)
                or fnmatch.fnmatchcase(rel, pat + "/*")
            ):
                return True
        else:
            if fnmatch.fnmatchcase(basename, pat) or any(
                fnmatch.fnmatchcase(seg, pat) for seg in parts
            ):
                return True
    return False


def matches_include(rel_path: str, include_globs: Iterable[str]) -> bool:
    """Return True if ``include_globs`` is empty or ``rel_path`` matches one."""
    globs = tuple(include_globs)
    if not globs:
        return True
    rel = rel_path.replace("\\", "/").strip("/")
    basename = Path(rel).name
    for pat in globs:
        clean = pat.replace("\\", "/").strip("/")
        if not clean:
            continue
        if "/" in clean:
            if fnmatch.fnmatch(rel, clean):
                return True
        else:
            if fnmatch.fnmatch(basename, clean) or fnmatch.fnmatch(rel, clean):
                return True
    return False


def build_tar_exclude_args(
    excludes: str | Iterable[str] | None = None,
) -> list[str]:
    """Build ``--exclude <pattern>`` flags for streaming ``tar`` from an exclude list.

    Patterns containing ``/`` are emitted with a ``^./`` prefix so bsdtar anchors
    them to the archive root ``.`` instead of matching nested subdirectories.
    """
    raw = DEFAULT_DATA_VAR_EXCLUDES if excludes is None else excludes
    args: list[str] = []
    for raw_pat in parse_pattern_list(raw):
        clean = _normalize_rel_path(raw_pat)
        if not clean:
            continue
        tar_pat = f"^./{clean}" if "/" in clean else clean
        args.extend(["--exclude", tar_pat])
    return args


def _escape_posix_bre(text: str) -> str:
    return re.sub(r"([\\.^$[*])", r"\\\1", text)


def _escape_tar_glob(text: str) -> str:
    # bsdtar pathmatch treats these as wildcards, and a trailing ``$`` as an end anchor.
    return re.sub(r"([\\\[\]*?$^])", r"\\\1", text)


def is_sqlite_file(path: Path) -> bool:
    """Return True if ``path`` starts with the 16-byte SQLite 3 file header."""
    try:
        with path.open("rb") as fh:
            return fh.read(len(SQLITE_FILE_HEADER)) == SQLITE_FILE_HEADER
    except OSError:
        return False


def snapshot_sqlite(
    src_dir: Path,
    dest_dir: Path,
    excludes: str | Iterable[str] | None = None,
) -> list[str]:
    """Create consistent online backups of SQLite files under ``src_dir`` into ``dest_dir``.

    Detection checks the 16-byte ``SQLite format 3\\0`` header regardless of extension.
    Excluded paths and symlinks are skipped. Returns sorted relative POSIX paths.
    """
    if not src_dir.is_dir():
        raise SqliteSnapshotError(f"Source directory does not exist: {src_dir}")

    patterns = parse_pattern_list(
        DEFAULT_DATA_VAR_EXCLUDES if excludes is None else excludes
    )
    snapshotted: list[str] = []

    for root, dirs, files in os.walk(src_dir, followlinks=False):
        root_path = Path(root)
        rel_dir = root_path.relative_to(src_dir).as_posix()
        kept_dirs: list[str] = []
        for d in sorted(dirs):
            sub_rel = d if rel_dir == "." else f"{rel_dir}/{d}"
            if matches_exclude(sub_rel, patterns):
                continue
            kept_dirs.append(d)
        dirs[:] = kept_dirs

        for fname in sorted(files):
            fpath = root_path / fname
            if fpath.is_symlink() or not fpath.is_file():
                continue
            rel_path = fname if rel_dir == "." else f"{rel_dir}/{fname}"
            if matches_exclude(rel_path, patterns):
                continue
            if not is_sqlite_file(fpath):
                continue

            dst_file = dest_dir / rel_path
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            dst_file.unlink(missing_ok=True)
            src_uri = f"{fpath.resolve().as_uri()}?mode=ro"
            src_conn: sqlite3.Connection | None = None
            dst_conn: sqlite3.Connection | None = None
            try:
                src_conn = sqlite3.connect(src_uri, uri=True, timeout=30.0)
                dst_conn = sqlite3.connect(str(dst_file))
                src_conn.backup(dst_conn)
            except (sqlite3.Error, OSError) as exc:
                if dst_conn is not None:
                    dst_conn.close()
                    dst_conn = None
                dst_file.unlink(missing_ok=True)
                raise SqliteSnapshotError(f"{rel_path}: {exc}") from exc
            finally:
                if dst_conn is not None:
                    dst_conn.close()
                if src_conn is not None:
                    src_conn.close()
            try:
                shutil.copystat(fpath, dst_file)
            except OSError:
                pass
            snapshotted.append(rel_path)

    return sorted(snapshotted)


def build_var_dalton_tar_cmd(
    src_dir: Path,
    excludes: str | Iterable[str] | None = None,
    *,
    sqlite_paths: Iterable[str] = (),
    staging_dir: Path | None = None,
) -> list[str]:
    """Build the streaming ``tar czf -`` command for ``var/dalton`` with excludes.

    When ``sqlite_paths`` is non-empty, live database files and their ``-wal``/``-shm``/
    ``-journal`` sidecars are excluded from ``src_dir`` and the consistent copies under
    ``staging_dir`` are added at identical ``./<rel_path>`` archive paths via ``-s``.
    """
    seen_sqlite: set[str] = set()
    clean_sqlite: list[str] = []
    for raw_rel in sqlite_paths:
        rel = str(raw_rel).strip("/")
        while rel.startswith("./"):
            rel = rel[2:].lstrip("/")
        if rel and rel not in seen_sqlite:
            seen_sqlite.add(rel)
            clean_sqlite.append(rel)
    exclude_args = build_tar_exclude_args(excludes)
    if not clean_sqlite:
        return [
            "tar",
            "czf",
            "-",
            *exclude_args,
            "-C",
            str(src_dir),
            ".",
        ]

    if staging_dir is None:
        raise ValueError("staging_dir is required when sqlite_paths is non-empty")
    staging_path = Path(staging_dir)
    if not staging_path.name:
        raise ValueError(f"staging_dir must have a non-empty basename: {staging_dir}")
    staging_parent = str(staging_path.parent) or "."
    staging_name = staging_path.name

    sqlite_exclude_args: list[str] = []
    for rel in clean_sqlite:
        escaped_rel = _escape_tar_glob(rel)
        for suffix in ("", "-wal", "-shm", "-journal"):
            sqlite_exclude_args.extend(["--exclude", f"^./{escaped_rel}{suffix}"])

    return [
        "tar",
        "czf",
        "-",
        *exclude_args,
        *sqlite_exclude_args,
        "-s",
        f"/^{_escape_posix_bre(staging_name)}/./HS",
        "-C",
        str(src_dir),
        ".",
        "-C",
        staging_parent,
        *[f"{staging_name}/{rel}" for rel in clean_sqlite],
    ]


def _parse_pipe_status(pipe_status: str | Iterable[int]) -> tuple[int, ...]:
    if isinstance(pipe_status, str):
        tokens = [tok for tok in pipe_status.strip().split() if tok]
        if not tokens:
            raise ValueError("pipe_status must not be empty")
        return tuple(int(tok) for tok in tokens)
    values = tuple(int(code) for code in pipe_status)
    if not values:
        raise ValueError("pipe_status must not be empty")
    return values


def should_retry_tar_failure(
    attempt: int,
    pipe_status: str | Iterable[int],
    *,
    max_retries: int = 1,
) -> bool:
    """Return True only when ``tar`` (stage 0) failed while all downstream stages exited 0."""
    if attempt > max_retries:
        return False
    codes = _parse_pipe_status(pipe_status)
    if len(codes) != 4:
        # tar | openssl | stream-hash-meta | rclone: anything else is a caller bug, never retry.
        return False
    tar_rc, *downstream_rcs = codes
    return tar_rc != 0 and all(rc == 0 for rc in downstream_rcs)


def summarize_tar_stderr(stderr_text: str, *, max_chars: int = 240) -> str:
    """Collapse ``tar`` stderr into a single sanitized log summary line."""
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", stderr_text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return "tar exited non-zero with empty stderr"
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3].rstrip() + "..."


def filter_var_dalton_paths(
    root_dir: Path,
    excludes: str | Iterable[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Walk ``root_dir`` and return sorted ``(included_files, excluded_files)`` relative paths."""
    patterns = parse_pattern_list(
        DEFAULT_DATA_VAR_EXCLUDES if excludes is None else excludes
    )
    included: list[str] = []
    excluded: list[str] = []
    if not root_dir.is_dir():
        return included, excluded

    for root, dirs, files in os.walk(root_dir, followlinks=False):
        dirs.sort()
        root_path = Path(root)
        rel_dir = root_path.relative_to(root_dir).as_posix()
        for fname in sorted(files):
            rel_path = fname if rel_dir == "." else f"{rel_dir}/{fname}"
            if matches_exclude(rel_path, patterns):
                excluded.append(rel_path)
            else:
                included.append(rel_path)
    return included, excluded


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def key_fingerprint(key_file: Path) -> str:
    """Return the first 16 hex chars of SHA-256 of the passphrase file (never the key itself)."""
    return hashlib.sha256(key_file.read_bytes()).hexdigest()[:16]


def load_enc_manifest_with_key(
    manifest_path: Path,
) -> tuple[str | None, dict[str, dict[str, Any]]]:
    """Load the repo's key fingerprint and ``.enc`` entry map from ``manifest_path``."""
    if not manifest_path.is_file():
        return None, {}
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, {}
    if not isinstance(raw, dict):
        return None, {}
    raw_fp = raw.get("key_fingerprint")
    key_fp = raw_fp if isinstance(raw_fp, str) and raw_fp else None
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        return key_fp, {}
    parsed = {
        str(k): dict(v)
        for k, v in entries.items()
        if isinstance(k, str) and isinstance(v, dict)
    }
    return key_fp, parsed


def load_enc_manifest(manifest_path: Path) -> dict[str, dict[str, Any]]:
    """Load the repo's plaintext-sha256 manifest for ``.enc`` files."""
    _, entries = load_enc_manifest_with_key(manifest_path)
    return entries


def should_reuse_encrypted(
    rel_enc_path: str,
    plaintext_sha256: str,
    prev_manifest: dict[str, dict[str, Any]],
    existing_enc_path: Path,
    *,
    prev_key_fingerprint: str | None = None,
    current_key_fingerprint: str | None = None,
) -> bool:
    """Return True if existing ``.enc`` matches passphrase fingerprint and plaintext/ciphertext SHA-256."""
    if current_key_fingerprint is not None:
        if not prev_key_fingerprint or prev_key_fingerprint != current_key_fingerprint:
            return False
    entry = prev_manifest.get(rel_enc_path)
    if not isinstance(entry, dict):
        return False
    if entry.get("plaintext_sha256") != plaintext_sha256:
        return False
    expected_cipher_sha = entry.get("ciphertext_sha256")
    if not isinstance(expected_cipher_sha, str) or not expected_cipher_sha:
        return False
    try:
        if (
            not existing_enc_path.is_file()
            or existing_enc_path.is_symlink()
            or existing_enc_path.stat().st_size <= 0
        ):
            return False
        return sha256_file(existing_enc_path) == expected_cipher_sha
    except OSError:
        return False


def openssl_encrypt_bytes(data: bytes, dst: Path, key_file: Path) -> None:
    """Encrypt in-memory ``data`` into ``dst`` via ``openssl enc -aes-256-cbc -pbkdf2 -salt``."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_dst = dst.with_name(f".{dst.name}.tmp.{os.getpid()}")
    try:
        proc = subprocess.run(
            [
                "openssl",
                "enc",
                "-aes-256-cbc",
                "-pbkdf2",
                "-salt",
                "-out",
                str(tmp_dst),
                "-pass",
                f"file:{key_file}",
            ],
            input=data,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            stderr_txt = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"openssl enc failed ({proc.returncode}): {stderr_txt}"
            )
        if not tmp_dst.is_file() or tmp_dst.stat().st_size == 0:
            raise RuntimeError("openssl enc produced empty output")
        os.chmod(tmp_dst, 0o600)
        os.replace(tmp_dst, dst)
    finally:
        if tmp_dst.exists():
            tmp_dst.unlink(missing_ok=True)


def openssl_encrypt_file(src: Path, dst: Path, key_file: Path) -> None:
    """Encrypt ``src`` into ``dst`` via ``openssl enc -aes-256-cbc -pbkdf2 -salt``."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_dst = dst.with_name(f".{dst.name}.tmp.{os.getpid()}")
    try:
        proc = subprocess.run(
            [
                "openssl",
                "enc",
                "-aes-256-cbc",
                "-pbkdf2",
                "-salt",
                "-in",
                str(src),
                "-out",
                str(tmp_dst),
                "-pass",
                f"file:{key_file}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"openssl enc failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        if not tmp_dst.is_file() or tmp_dst.stat().st_size == 0:
            raise RuntimeError("openssl enc produced empty output")
        os.chmod(tmp_dst, 0o600)
        os.replace(tmp_dst, dst)
    finally:
        if tmp_dst.exists():
            tmp_dst.unlink(missing_ok=True)


def stage_config_repo(
    *,
    sources: Iterable[SyncSourceSpec],
    stage_dir: Path,
    existing_repo_dir: Path,
    key_file: Path,
    max_file_bytes: int,
    encrypt_fn: Callable[[Path, Path, Path], None] = openssl_encrypt_file,
    log_fn: Callable[[str], None] | None = None,
) -> StageSummary:
    """Stage config sources into ``stage_dir`` with exclusions, size caps, and conditional encryption.

    No plaintext secret or ``backup.key`` is ever written into ``stage_dir``.
    """
    stage_dir.mkdir(parents=True, exist_ok=True)
    prev_key_fp, prev_manifest = load_enc_manifest_with_key(
        existing_repo_dir / MANIFEST_FILENAME
    )
    curr_key_fp = key_fingerprint(key_file)
    new_entries: dict[str, dict[str, Any]] = {}

    plaintext_count = 0
    encrypted_reused = 0
    encrypted_new = 0
    skipped_large: list[str] = []
    skipped_excluded = 0
    skipped_forbidden: list[str] = []

    def _emit(msg: str) -> None:
        if log_fn is not None:
            log_fn(msg)

    for spec in sources:
        dest_root = stage_dir / spec.dest_subdir
        dest_root.mkdir(parents=True, exist_ok=True)
        if not spec.src_dir.is_dir():
            _emit(f"SKIP_MISSING source={spec.name} path={spec.src_dir}")
            continue

        for root, dirs, files in os.walk(spec.src_dir, followlinks=False):
            root_path = Path(root)
            rel_dir = root_path.relative_to(spec.src_dir).as_posix()
            if spec.top_level_only:
                # For prod_overrides we may match relative patterns like scripts/load_env.sh
                # while still pruning noisy directories.
                pruned_dirs: list[str] = []
                for d in sorted(dirs):
                    if d in {".git", "node_modules", ".venv", "__pycache__", "var"}:
                        continue
                    sub_rel = d if rel_dir == "." else f"{rel_dir}/{d}"
                    if any("/" in g and g.startswith(sub_rel + "/") for g in spec.include_globs):
                        pruned_dirs.append(d)
                dirs[:] = pruned_dirs
            else:
                kept_dirs: list[str] = []
                for d in sorted(dirs):
                    sub_rel = d if rel_dir == "." else f"{rel_dir}/{d}"
                    if matches_exclude(sub_rel, spec.excludes):
                        skipped_excluded += 1
                        continue
                    kept_dirs.append(d)
                dirs[:] = kept_dirs

            for fname in sorted(files):
                src_file = root_path / fname
                if src_file.is_symlink() or not src_file.is_file():
                    continue
                rel_path = fname if rel_dir == "." else f"{rel_dir}/{fname}"

                if is_forbidden_key_file(src_file, key_file):
                    skipped_forbidden.append(f"{spec.name}:{rel_path}")
                    continue
                if spec.excludes and matches_exclude(rel_path, spec.excludes):
                    skipped_excluded += 1
                    continue
                if spec.include_globs and not matches_include(rel_path, spec.include_globs):
                    continue

                try:
                    size_bytes = src_file.stat().st_size
                except OSError:
                    continue
                if max_file_bytes > 0 and size_bytes > max_file_bytes:
                    size_mb = size_bytes / (1024 * 1024)
                    limit_mb = max_file_bytes / (1024 * 1024)
                    skipped_large.append(f"{spec.name}:{rel_path}")
                    _emit(
                        f"SKIP_LARGE {spec.name}/{rel_path} ({size_mb:.1f} MB > {limit_mb:.1f} MB)"
                    )
                    continue

                data = src_file.read_bytes()
                allowlisted = is_plaintext_allowlisted(spec.dest_subdir, rel_path)
                if allowlisted:
                    secret_reason = detect_secret_reason(rel_path, data)
                    should_encrypt = secret_reason is not None
                else:
                    should_encrypt = True
                if should_encrypt:
                    rel_enc = f"{spec.dest_subdir}/{rel_path}.enc"
                    dst_enc = stage_dir / rel_enc
                    dst_enc.parent.mkdir(parents=True, exist_ok=True)
                    digest = sha256_bytes(data)
                    existing_enc = existing_repo_dir / rel_enc
                    if should_reuse_encrypted(
                        rel_enc,
                        digest,
                        prev_manifest,
                        existing_enc,
                        prev_key_fingerprint=prev_key_fp,
                        current_key_fingerprint=curr_key_fp,
                    ):
                        shutil.copy2(existing_enc, dst_enc)
                        cipher_digest = prev_manifest[rel_enc]["ciphertext_sha256"]
                        encrypted_reused += 1
                    else:
                        if encrypt_fn is openssl_encrypt_file:
                            openssl_encrypt_bytes(data, dst_enc, key_file)
                        else:
                            with tempfile.TemporaryDirectory(prefix="dalton-enc-") as tmpd:
                                tmp_src = Path(tmpd) / src_file.name
                                tmp_src.write_bytes(data)
                                os.chmod(tmp_src, 0o600)
                                encrypt_fn(tmp_src, dst_enc, key_file)
                        cipher_digest = sha256_file(dst_enc)
                        encrypted_new += 1
                    new_entries[rel_enc] = {
                        "plaintext_sha256": digest,
                        "ciphertext_sha256": cipher_digest,
                        "size": len(data),
                    }
                else:
                    dst_plain = dest_root / rel_path
                    dst_plain.parent.mkdir(parents=True, exist_ok=True)
                    dst_plain.write_bytes(data)
                    shutil.copystat(src_file, dst_plain)
                    plaintext_count += 1

    manifest_payload = {
        "version": 1,
        "key_fingerprint": curr_key_fp,
        "entries": dict(sorted(new_entries.items())),
    }
    (stage_dir / MANIFEST_FILENAME).write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return StageSummary(
        plaintext_count=plaintext_count,
        encrypted_reused=encrypted_reused,
        encrypted_new=encrypted_new,
        skipped_large=tuple(skipped_large),
        skipped_excluded=skipped_excluded,
        skipped_forbidden=tuple(skipped_forbidden),
    )


STANDARD_BACKUP_SUBDIRS: tuple[str, ...] = (
    "pg",
    "frozen",
    "minio-mirror",
    "asr-jobs",
    "sets",
    ".tmp",
)

PREFLIGHT_EXT_SSD_MIN_GB = 150
PREFLIGHT_INTERNAL_ROOT_MIN_GB = 10
PREFLIGHT_COLIMA_DOCKER_MIN_GB = 55
_BYTES_PER_GB = 1024 * 1024 * 1024


@dataclasses.dataclass(frozen=True)
class PreflightDiskCheckItem:
    """One disk/mount preflight gate result (§2.1 step 0)."""

    name: str
    ok: bool
    detail: str
    free_bytes: int | None = None
    required_bytes: int | None = None


@dataclasses.dataclass(frozen=True)
class PreflightDiskReport:
    """Aggregate result of ``preflight-disk-space`` (§2.1 step 0)."""

    ok: bool
    colima_resolved_path: str
    checks: tuple[PreflightDiskCheckItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "colima_resolved_path": self.colima_resolved_path,
            "checks": [dataclasses.asdict(c) for c in self.checks],
        }


def _nearest_existing_path(path: Path) -> Path:
    """Return ``path`` if it exists, otherwise walk upward to the nearest existing ancestor."""
    cur = path
    while not cur.exists():
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return cur


def verify_external_mount(
    mount_root: Path,
    target_path: Path,
    *,
    is_mount_fn: Callable[[Path], bool] = os.path.ismount,
    stat_fn: Callable[[Path], Any] = os.stat,
) -> None:
    """Ensure ``mount_root`` is a mounted volume and ``target_path`` lives on the same filesystem device.

    Checks both canonical path containment and actual filesystem device ID (``st_dev``) of
    ``target_path`` (or its nearest existing ancestor), preventing nested mounts or internal
    symlinks from spilling onto the internal disk.
    """
    if not mount_root.exists() or not mount_root.is_dir():
        raise MountCheckError(f"External mount root does not exist: {mount_root}")
    if mount_root.is_symlink():
        raise MountCheckError(f"External mount root must not be a symlink: {mount_root}")
    if not is_mount_fn(mount_root):
        raise MountCheckError(
            f"External drive is not mounted at {mount_root}; refusing internal disk fallback"
        )
    resolved_mount = mount_root.resolve()
    resolved_target = target_path.resolve(strict=False)
    try:
        resolved_target.relative_to(resolved_mount)
    except ValueError as exc:
        raise MountCheckError(
            f"Backup target {resolved_target} is outside external mount {resolved_mount}"
        ) from exc

    existing_target = _nearest_existing_path(resolved_target)
    try:
        mount_dev = stat_fn(resolved_mount).st_dev
        target_dev = stat_fn(existing_target).st_dev
    except OSError as exc:
        raise MountCheckError(
            f"Cannot inspect device ID for {existing_target} against {resolved_mount}: {exc}"
        ) from exc
    if target_dev != mount_dev:
        raise MountCheckError(
            f"Target {resolved_target} (nearest existing {existing_target}, st_dev={target_dev}) "
            f"is on a different device than external mount {resolved_mount} (st_dev={mount_dev})"
        )


def verify_backup_mount_layout(
    mount_root: Path,
    backups_root: Path,
    *,
    subdirs: Sequence[str] = STANDARD_BACKUP_SUBDIRS,
    is_mount_fn: Callable[[Path], bool] = os.path.ismount,
    stat_fn: Callable[[Path], Any] = os.stat,
) -> list[Path]:
    """Verify ``backups_root`` and all standard backup subdirs reside on ``mount_root`` (§2.1 step 1)."""
    checked: list[Path] = []
    verify_external_mount(mount_root, backups_root, is_mount_fn=is_mount_fn, stat_fn=stat_fn)
    checked.append(backups_root)
    for sub in subdirs:
        target = backups_root / sub
        verify_external_mount(mount_root, target, is_mount_fn=is_mount_fn, stat_fn=stat_fn)
        checked.append(target)
    return checked


_COLIMA_DISK_NAMES = frozenset({"diffdisk", "basedisk", "datadisk", "disk"})
_COLIMA_DISK_SUFFIXES = (".img", ".qcow2", ".raw")


def _is_colima_disk_file_name(name: str) -> bool:
    low = name.lower()
    return low in _COLIMA_DISK_NAMES or low.endswith(_COLIMA_DISK_SUFFIXES)


def _discover_colima_disk_paths(
    colima_dir: Path,
    *,
    profile: str = "default",
) -> tuple[list[Path], list[Path]]:
    """Return ``(all_paths, disk_files)`` for the active Colima profile's Lima dir and virtual disks."""
    prof = (profile or "default").strip() or "default"
    instance_name = "colima" if prof == "default" else f"colima-{prof}"
    lima_root = colima_dir / "_lima"
    instance_dir = lima_root / instance_name
    candidates: list[Path] = []
    disk_files: list[Path] = []

    def _record_disk(p: Path) -> None:
        if p.exists() or p.is_symlink():
            candidates.append(p)
            # Treat existing regular files (or symlinks resolving to files, or dangling disk symlinks
            # which verify_external_mount will catch) as located virtual disk targets.
            if p.is_file() or (p.is_symlink() and not p.is_dir()):
                disk_files.append(p)

    if lima_root.exists() or lima_root.is_symlink():
        candidates.append(lima_root)
    if instance_dir.exists() or instance_dir.is_symlink():
        candidates.append(instance_dir)
        for name in sorted(_COLIMA_DISK_NAMES):
            _record_disk(instance_dir / name)
        try:
            for child in sorted(instance_dir.iterdir()):
                if _is_colima_disk_file_name(child.name) and child not in candidates:
                    _record_disk(child)
        except OSError:
            pass

    disks_root = lima_root / "_disks"
    if disks_root.is_dir():
        for disk_dir in sorted(disks_root.iterdir()):
            if disk_dir.is_dir() or disk_dir.is_symlink():
                candidates.append(disk_dir)
                for name in sorted(_COLIMA_DISK_NAMES):
                    _record_disk(disk_dir / name)
                try:
                    for child in sorted(disk_dir.iterdir()):
                        if _is_colima_disk_file_name(child.name) and child not in candidates:
                            _record_disk(child)
                except OSError:
                    pass

    profile_dir = colima_dir / prof
    if profile_dir.exists() or profile_dir.is_symlink():
        candidates.append(profile_dir)
        for child_name in ("disk", "diffdisk", "basedisk", "datadisk"):
            _record_disk(profile_dir / child_name)
        try:
            for child in sorted(profile_dir.iterdir()):
                if _is_colima_disk_file_name(child.name) and child not in candidates:
                    _record_disk(child)
        except OSError:
            pass

    def _dedup(items: list[Path]) -> list[Path]:
        seen: set[Path] = set()
        unique: list[Path] = []
        for p in items:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        return unique

    return _dedup(candidates), _dedup(disk_files)


class ColimaConfigResolutionError(RuntimeError):
    """Raised when the active Docker host/context cannot be mapped unambiguously to a Colima profile."""


def _parse_colima_socket_path(
    endpoint: str,
    *,
    expected_home: Path | None,
    realpath_fn: Callable[[str], str] = os.path.realpath,
) -> tuple[Path | None, str | None]:
    """Extract ``(colima_home, profile)`` from a ``unix://`` Docker endpoint pointing to Colima."""
    ep = (endpoint or "").strip()
    if not ep.startswith("unix://"):
        return None, None
    raw_sock = ep[len("unix://") :]
    if not raw_sock:
        return None, None

    expected_real = Path(realpath_fn(str(expected_home))) if expected_home is not None else None
    candidates = [Path(raw_sock)]
    try:
        real_sock = Path(realpath_fn(raw_sock))
        if real_sock not in candidates:
            candidates.append(real_sock)
    except OSError:
        pass

    for sock in candidates:
        if sock.name != "docker.sock":
            continue
        # Pattern 1: <colima_home>/_lima/colima[-<profile>]/sock/docker.sock
        if (
            sock.parent.name == "sock"
            and sock.parent.parent.parent.name == "_lima"
            and (
                sock.parent.parent.name == "colima"
                or sock.parent.parent.name.startswith("colima-")
            )
        ):
            lima_name = sock.parent.parent.name
            prof = "default" if lima_name == "colima" else lima_name[len("colima-") :]
            home = sock.parent.parent.parent.parent
            if prof:
                return home, prof

        # Pattern 2: <colima_home>/<profile>/docker.sock
        prof_dir = sock.parent
        home_dir = prof_dir.parent
        prof_name = prof_dir.name
        if not prof_name or home_dir == prof_dir:
            continue
        is_expected_home = False
        if expected_home is not None:
            try:
                home_real = Path(realpath_fn(str(home_dir)))
            except OSError:
                home_real = home_dir
            if home_dir == expected_home or (
                expected_real is not None and home_real == expected_real
            ):
                is_expected_home = True
        looks_like_colima_home = (
            is_expected_home
            or home_dir.name in (".colima", "colima")
            or "colima" in home_dir.name.lower()
            or (home_dir / "_lima").is_dir()
        )
        if looks_like_colima_home:
            return home_dir, prof_name

    return None, None


def _inspect_active_docker_endpoint(
    *,
    env: Mapping[str, str],
    run_fn: Callable[..., subprocess.CompletedProcess[str]],
    timeout_s: float = 10.0,
) -> tuple[str | None, str | None, str | None]:
    """Return ``(endpoint_url, context_name, error_reason)`` for the active Docker daemon.

    Matches Docker CLI precedence: ``DOCKER_CONTEXT`` > ``DOCKER_HOST`` > current Docker context.
    """
    ctx_env = (env.get("DOCKER_CONTEXT") or "").strip()
    docker_host = (env.get("DOCKER_HOST") or "").strip()
    if not ctx_env and docker_host:
        return docker_host, None, None

    inspect_cmd = ["docker", "context", "inspect"]
    if ctx_env:
        inspect_cmd.append(ctx_env)
    try:
        proc = run_fn(
            inspect_cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
            env=dict(env),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, ctx_env or None, f"failed running {' '.join(inspect_cmd)!r}: {exc}"

    if proc.returncode != 0 or not (proc.stdout or "").strip():
        err_msg = (proc.stderr or "").strip() or f"rc={proc.returncode}"
        return None, ctx_env or None, f"{' '.join(inspect_cmd)!r} failed ({err_msg})"

    try:
        parsed = json.loads(proc.stdout)
    except ValueError as exc:
        return None, ctx_env or None, f"invalid JSON from {' '.join(inspect_cmd)!r}: {exc}"

    if not isinstance(parsed, list) or not parsed or not isinstance(parsed[0], dict):
        return None, ctx_env or None, f"unexpected JSON shape from {' '.join(inspect_cmd)!r}"

    entry = parsed[0]
    ctx_name = str(entry.get("Name") or ctx_env or "").strip() or None
    endpoints = entry.get("Endpoints")
    docker_ep_obj = endpoints.get("docker") if isinstance(endpoints, dict) else None
    endpoint_url = (
        str(docker_ep_obj.get("Host") or "").strip()
        if isinstance(docker_ep_obj, dict)
        else ""
    )
    if not endpoint_url:
        return None, ctx_name, f"Docker context {ctx_name!r} has no Endpoints.docker.Host"
    return endpoint_url, ctx_name, None


def resolve_active_colima_config(
    *,
    colima_dir: Path | None = None,
    colima_profile: str | None = None,
    env: Mapping[str, str] | None = None,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    realpath_fn: Callable[[str], str] = os.path.realpath,
    timeout_s: float = 10.0,
) -> tuple[Path, str]:
    """Resolve the active Colima home directory and profile from the actual Docker context/host.

    Priority matches Docker CLI:
    - Resolves the active Docker daemon in order ``DOCKER_CONTEXT`` > ``DOCKER_HOST`` >
      active Docker context (``docker context inspect``) and maps its endpoint to the exact
      Colima home and profile.
    - Explicit ``colima_dir`` and ``colima_profile`` / ``COLIMA_PROFILE`` are validated against
      the active Docker endpoint; any mismatch fails closed with :class:`ColimaConfigResolutionError`.
    - Fails closed with :class:`ColimaConfigResolutionError` if the active Docker context/host
      cannot be determined or cannot be mapped unambiguously to a running Colima profile.
    """
    em = os.environ if env is None else env
    raw_home = (em.get("COLIMA_HOME") or "").strip()
    if colima_dir is not None:
        home_path = colima_dir
    elif raw_home:
        home_path = Path(raw_home).expanduser()
    else:
        home_path = Path.home() / ".colima"

    requested_profile = (
        str(colima_profile).strip()
        if colima_profile is not None and str(colima_profile).strip()
        else (em.get("COLIMA_PROFILE") or "").strip()
    )

    runner = run_fn if run_fn is not None else subprocess.run
    if env is None:
        sub_env = dict(os.environ)
    else:
        sub_env = {str(k): str(v) for k, v in em.items()}
        if "PATH" not in sub_env and "PATH" in os.environ:
            sub_env["PATH"] = os.environ["PATH"]
    sub_env["COLIMA_HOME"] = str(home_path)

    docker_endpoint, docker_context, docker_err = _inspect_active_docker_endpoint(
        env=sub_env,
        run_fn=runner,
        timeout_s=timeout_s,
    )
    if docker_err is not None or not docker_endpoint:
        raise ColimaConfigResolutionError(
            "Cannot determine active Docker context/endpoint to map Colima configuration: "
            f"{docker_err or 'empty endpoint'}"
        )

    sock_home, sock_profile = _parse_colima_socket_path(
        docker_endpoint,
        expected_home=home_path,
        realpath_fn=realpath_fn,
    )
    if sock_profile is None or sock_home is None:
        raise ColimaConfigResolutionError(
            f"Active Docker endpoint {docker_endpoint!r} "
            f"(context={docker_context or 'DOCKER_HOST'}) does not map to a Colima socket; "
            "refusing to guess Colima profile or virtual disk location"
        )

    if requested_profile and requested_profile != sock_profile:
        raise ColimaConfigResolutionError(
            f"Active Docker endpoint {docker_endpoint!r} "
            f"(context={docker_context or 'DOCKER_HOST'}) uses Colima profile {sock_profile!r} "
            f"under {sock_home}, which conflicts with specified Colima profile {requested_profile!r}"
        )

    # Use the actual Colima home indicated by the Docker context socket so preflight checks
    # the real virtual disk file (not an unrelated default ~/.colima). If caller explicitly
    # pinned a conflicting colima_dir, fail closed instead of silently checking the wrong home.
    resolved_home = sock_home
    if colima_dir is not None:
        try:
            pinned_real = Path(realpath_fn(str(colima_dir)))
            sock_home_real = Path(realpath_fn(str(sock_home)))
        except OSError:
            pinned_real = colima_dir
            sock_home_real = sock_home
        if pinned_real != sock_home_real:
            raise ColimaConfigResolutionError(
                f"Active Docker endpoint {docker_endpoint!r} uses Colima home {sock_home} "
                f"(profile={sock_profile!r}), which conflicts with specified colima_dir={colima_dir}"
            )
        resolved_home = colima_dir

    # Verify against `colima list --json` under the resolved Colima home when available:
    # if `colima list --json` reports running profiles and the Docker context's profile is NOT
    # running there, fail closed so we never check a stopped/mismatched profile.
    sub_env["COLIMA_HOME"] = str(resolved_home)
    running_profiles: list[str] = []
    colima_list_ok = False
    try:
        proc = runner(
            ["colima", "list", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
            env=sub_env,
        )
        if proc.returncode == 0 and proc.stdout:
            colima_list_ok = True
            for line in proc.stdout.splitlines():
                raw = line.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(entry, dict) and str(entry.get("status") or "").strip().lower() == "running":
                    name = str(entry.get("name") or "").strip()
                    if name:
                        running_profiles.append(name)
    except (OSError, subprocess.TimeoutExpired):
        pass

    if colima_list_ok and sock_profile not in running_profiles:
        raise ColimaConfigResolutionError(
            f"Active Docker context ({docker_context or 'DOCKER_HOST'}, endpoint={docker_endpoint!r}) "
            f"maps to Colima profile {sock_profile!r} under {resolved_home}, but that profile is not "
            f"running according to `colima list --json` (running={running_profiles})"
        )

    return resolved_home, sock_profile


def parse_df_available_bytes(df_stdout: str) -> int | None:
    """Parse available bytes from POSIX ``df -Pk <path>`` output (1024-byte blocks)."""
    lines = [line.strip() for line in df_stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    parts = lines[-1].split()
    if len(parts) < 4:
        return None
    # In POSIX df -Pk: Filesystem 1024-blocks Used Available Capacity Mounted-on
    # Take parts[-3] if Filesystem name contains spaces, else parts[3] when numeric.
    avail_tok = parts[3] if parts[3].isdigit() else (parts[-3] if len(parts) >= 5 and parts[-3].isdigit() else "")
    if not avail_tok.isdigit():
        return None
    return int(avail_tok) * 1024


def _probe_colima_docker_free_bytes(
    *,
    colima_home: Path | None = None,
    profile: str = "default",
    env: Mapping[str, str] | None = None,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout_s: float = 15.0,
) -> int | None:
    """Query free space inside the active Colima profile's ``/var/lib/docker`` with a bounded timeout."""
    prof = (profile or "default").strip() or "default"
    cmd = ["colima", "ssh", "--profile", prof, "--", "df", "-Pk", "/var/lib/docker"]
    sub_env = dict(os.environ if env is None else env)
    if colima_home is not None:
        sub_env["COLIMA_HOME"] = str(colima_home)
    try:
        proc = run_fn(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
            env=sub_env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return parse_df_available_bytes(proc.stdout)


def preflight_disk_space(
    *,
    external_mount_root: Path = Path("/Volumes/EveSSD"),
    internal_root: Path = Path("/"),
    colima_dir: Path | None = None,
    colima_profile: str | None = None,
    env: Mapping[str, str] | None = None,
    min_external_gb: int = PREFLIGHT_EXT_SSD_MIN_GB,
    min_internal_gb: int = PREFLIGHT_INTERNAL_ROOT_MIN_GB,
    min_colima_docker_gb: int = PREFLIGHT_COLIMA_DOCKER_MIN_GB,
    is_mount_fn: Callable[[Path], bool] = os.path.ismount,
    disk_usage_fn: Callable[[Path], Any] = shutil.disk_usage,
    realpath_fn: Callable[[str], str] = os.path.realpath,
    stat_fn: Callable[[Path], Any] = os.stat,
    colima_docker_free_bytes_fn: Callable[[], int | None] | None = None,
    colima_run_fn: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> PreflightDiskReport:
    """Run §2.1 Step 0 preflight gates before any write/migration starts.

    Gates:
    1. Active Colima home (respecting ``COLIMA_HOME``) AND the active Colima profile's
       actual virtual disk file(s) must exist and reside on the mounted external volume
       with matching device ID (``st_dev``). Fails closed if no virtual disk file is found.
    2. External volume free space >= ``min_external_gb`` (default 150 GB).
    3. Host internal root ``/`` free space >= ``min_internal_gb`` (default 10 GB).
    4. Active Colima profile's ``/var/lib/docker`` free space >= ``min_colima_docker_gb`` (default 55 GB).
    """
    checks: list[PreflightDiskCheckItem] = []
    colima_target: Path | None = None
    active_profile = ""
    resolved_colima_str = ""
    colima_resolution_err: str | None = None
    try:
        colima_target, active_profile = resolve_active_colima_config(
            colima_dir=colima_dir,
            colima_profile=colima_profile,
            env=env,
            run_fn=colima_run_fn,
            realpath_fn=realpath_fn,
        )
        resolved_colima = Path(realpath_fn(str(colima_target)))
        resolved_colima_str = str(resolved_colima)
    except ColimaConfigResolutionError as exc:
        colima_resolution_err = str(exc)
        resolved_colima = None

    # Gate 0: Colima root AND active profile virtual disk file(s) must exist on external mount's st_dev
    if colima_resolution_err is not None or colima_target is None or resolved_colima is None:
        checks.append(
            PreflightDiskCheckItem(
                name="colima_location",
                ok=False,
                detail=colima_resolution_err or "Failed resolving active Colima configuration",
            )
        )
    elif not colima_target.exists() and not colima_target.is_symlink():
        checks.append(
            PreflightDiskCheckItem(
                name="colima_location",
                ok=False,
                detail=(
                    f"Colima path {colima_target} (profile={active_profile}) does not exist; "
                    f"verify Colima is installed and located on external volume {external_mount_root}"
                ),
            )
        )
    else:
        try:
            verify_external_mount(
                external_mount_root,
                resolved_colima,
                is_mount_fn=is_mount_fn,
                stat_fn=stat_fn,
            )
            disk_paths, disk_files = _discover_colima_disk_paths(
                resolved_colima, profile=active_profile
            )
            verified_paths: list[str] = []
            for dp in disk_paths:
                resolved_dp = Path(realpath_fn(str(dp)))
                verify_external_mount(
                    external_mount_root,
                    resolved_dp,
                    is_mount_fn=is_mount_fn,
                    stat_fn=stat_fn,
                )
                verified_paths.append(str(resolved_dp))
            if not disk_files:
                checks.append(
                    PreflightDiskCheckItem(
                        name="colima_location",
                        ok=False,
                        detail=(
                            f"No Colima virtual disk file (diffdisk/basedisk/datadisk/*.img/*.qcow2/*.raw) "
                            f"found under {resolved_colima} for active profile {active_profile!r}; "
                            "cannot verify virtual disk placement on external volume"
                        ),
                    )
                )
            else:
                checks.append(
                    PreflightDiskCheckItem(
                        name="colima_location",
                        ok=True,
                        detail=(
                            f"{colima_target} -> {resolved_colima} (profile={active_profile}) is on "
                            f"external volume {external_mount_root}; verified {len(disk_files)} "
                            f"disk file(s) across {len(verified_paths)} path(s)"
                        ),
                    )
                )
        except MountCheckError as exc:
            checks.append(
                PreflightDiskCheckItem(
                    name="colima_location",
                    ok=False,
                    detail=(
                        f"Colima virtual disk ({colima_target} -> {resolved_colima}, profile={active_profile}) "
                        f"is not on external volume {external_mount_root} (likely on internal disk '/'): {exc}. "
                        "Move Colima home and its active disk files to the external SSD before proceeding."
                    ),
                )
            )

    # Gate 1: External SSD >= 150 GB
    req_ext = int(min_external_gb) * _BYTES_PER_GB
    try:
        verify_external_mount(
            external_mount_root,
            external_mount_root,
            is_mount_fn=is_mount_fn,
            stat_fn=stat_fn,
        )
        ext_free = int(disk_usage_fn(external_mount_root).free)
        ok_ext = ext_free >= req_ext
        checks.append(
            PreflightDiskCheckItem(
                name="external_ssd_free",
                ok=ok_ext,
                free_bytes=ext_free,
                required_bytes=req_ext,
                detail=(
                    f"{external_mount_root} free={ext_free / _BYTES_PER_GB:.1f} GB "
                    f"(required >= {min_external_gb} GB)"
                ),
            )
        )
    except (MountCheckError, OSError) as exc:
        checks.append(
            PreflightDiskCheckItem(
                name="external_ssd_free",
                ok=False,
                required_bytes=req_ext,
                detail=f"Cannot inspect external volume {external_mount_root}: {exc}",
            )
        )

    # Gate 2: Internal root '/' >= 10 GB
    req_int = int(min_internal_gb) * _BYTES_PER_GB
    try:
        int_free = int(disk_usage_fn(internal_root).free)
        ok_int = int_free >= req_int
        checks.append(
            PreflightDiskCheckItem(
                name="internal_root_free",
                ok=ok_int,
                free_bytes=int_free,
                required_bytes=req_int,
                detail=(
                    f"{internal_root} free={int_free / _BYTES_PER_GB:.1f} GB "
                    f"(required >= {min_internal_gb} GB)"
                ),
            )
        )
    except OSError as exc:
        checks.append(
            PreflightDiskCheckItem(
                name="internal_root_free",
                ok=False,
                required_bytes=req_int,
                detail=f"Cannot inspect internal root {internal_root}: {exc}",
            )
        )

    # Gate 3: Active Colima profile's /var/lib/docker >= 55 GB (using the SAME resolved Colima config)
    req_colima = int(min_colima_docker_gb) * _BYTES_PER_GB
    if colima_resolution_err is not None or colima_target is None:
        checks.append(
            PreflightDiskCheckItem(
                name="colima_docker_free",
                ok=False,
                required_bytes=req_colima,
                detail=f"Cannot probe Colima /var/lib/docker free space: {colima_resolution_err}",
            )
        )
    else:
        if colima_docker_free_bytes_fn is not None:
            colima_free = colima_docker_free_bytes_fn()
        else:
            colima_free = _probe_colima_docker_free_bytes(
                colima_home=colima_target,
                profile=active_profile,
                env=env,
                run_fn=colima_run_fn if colima_run_fn is not None else subprocess.run,
            )
        if colima_free is None:
            checks.append(
                PreflightDiskCheckItem(
                    name="colima_docker_free",
                    ok=False,
                    required_bytes=req_colima,
                    detail=(
                        "Failed probing Colima /var/lib/docker free space "
                        f"(required >= {min_colima_docker_gb} GB)"
                    ),
                )
            )
        else:
            ok_colima = int(colima_free) >= req_colima
            checks.append(
                PreflightDiskCheckItem(
                    name="colima_docker_free",
                    ok=ok_colima,
                    free_bytes=int(colima_free),
                    required_bytes=req_colima,
                    detail=(
                        f"Colima /var/lib/docker free={int(colima_free) / _BYTES_PER_GB:.1f} GB "
                        f"(required >= {min_colima_docker_gb} GB)"
                    ),
                )
            )

    return PreflightDiskReport(
        ok=all(c.ok for c in checks),
        colima_resolved_path=resolved_colima_str,
        checks=tuple(checks),
    )


def select_orphan_objects_to_reap(
    bucket_objects: Iterable[Mapping[str, Any]],
    registered_keys: Iterable[str],
    *,
    older_than_hours: float = 48.0,
    now: dt.datetime | None = None,
) -> list[str]:
    """Select unregistered objects older than ``older_than_hours`` eligible for orphan cleanup (§1.4)."""
    ref_now = now or dt.datetime.now(dt.timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=dt.timezone.utc)
    cutoff = ref_now - dt.timedelta(hours=float(older_than_hours))
    registered = {str(k).strip().lstrip("/") for k in registered_keys if str(k).strip()}
    orphans: list[str] = []
    for item in bucket_objects:
        key = str(item.get("Key") or item.get("key") or "").strip().lstrip("/")
        if not key or key in registered:
            continue
        lm = item.get("LastModified") or item.get("last_modified")
        if isinstance(lm, str):
            try:
                lm_dt = dt.datetime.fromisoformat(lm.strip().replace("Z", "+00:00"))
            except ValueError:
                continue
        elif isinstance(lm, dt.datetime):
            lm_dt = lm
        else:
            continue
        if lm_dt.tzinfo is None:
            lm_dt = lm_dt.replace(tzinfo=dt.timezone.utc)
        if lm_dt <= cutoff:
            orphans.append(key)
    return sorted(orphans)


def find_latest_pg_dump(pg_backup_dir: Path) -> Path | None:
    """Return the newest complete ``dalton-pg-YYYYMMDD-HHMMSS.dump`` file, or None."""
    if not pg_backup_dir.is_dir():
        return None
    candidates: list[tuple[dt.datetime, Path]] = []
    for entry in pg_backup_dir.iterdir():
        m = _RE_PG_DUMP.match(entry.name)
        if not m:
            continue
        if entry.is_symlink() or not entry.is_file():
            continue
        try:
            if entry.stat().st_size <= 0:
                continue
            stamp = dt.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
        except (OSError, ValueError):
            continue
        candidates.append((stamp, entry))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def stream_copy_with_limit(
    src_stream: Any,
    dst_path: Path,
    *,
    max_bytes: int,
    gzip_output: bool = False,
    chunk_size: int = 1024 * 1024,
) -> tuple[bool, int]:
    """Stream bytes from ``src_stream`` to ``dst_path``, aborting if uncompressed bytes exceed ``max_bytes``.

    Returns ``(ok, total_uncompressed_bytes)``. When ``ok`` is False, ``dst_path`` is removed.
    """
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        if gzip_output:
            with dst_path.open("wb") as raw_fh:
                with gzip.GzipFile(fileobj=raw_fh, mode="wb", mtime=0) as gz_fh:
                    while True:
                        chunk = src_stream.read(chunk_size)
                        if not chunk:
                            break
                        total += len(chunk)
                        if max_bytes > 0 and total > max_bytes:
                            raise OverflowError("stream exceeded max_bytes")
                        gz_fh.write(chunk)
        else:
            with dst_path.open("wb") as raw_fh:
                while True:
                    chunk = src_stream.read(chunk_size)
                    if not chunk:
                        break
                    total += len(chunk)
                    if max_bytes > 0 and total > max_bytes:
                        raise OverflowError("stream exceeded max_bytes")
                    raw_fh.write(chunk)
        return True, total
    except OverflowError:
        dst_path.unlink(missing_ok=True)
        return False, total
    except Exception:
        dst_path.unlink(missing_ok=True)
        raise


def build_rclone_cmd(*rclone_args: str) -> list[str]:
    """Build an ``rclone`` invocation with all proxy environment variables unset (B8b)."""
    cmd = ["env"]
    for var in PROXY_ENV_VARS:
        cmd.extend(["-u", var])
    cmd.append("rclone")
    cmd.extend(rclone_args)
    return cmd


def check_rclone_remote(
    remote_name: str,
    *,
    which_fn: Callable[[str], str | None] = shutil.which,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[bool, str]:
    """Check whether ``rclone`` is installed and ``remote_name`` is configured."""
    clean_remote = remote_name.strip().rstrip(":")
    if not clean_remote:
        return False, "DALTON_RCLONE_REMOTE is empty"
    if not which_fn("rclone"):
        return False, "rclone command not found"
    try:
        proc = run_fn(
            build_rclone_cmd("listremotes"),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return False, f"rclone listremotes failed: {exc}"
    if proc.returncode != 0:
        return False, f"rclone listremotes exited {proc.returncode}: {proc.stderr.strip()}"
    remotes = {
        line.strip().rstrip(":")
        for line in proc.stdout.splitlines()
        if line.strip()
    }
    if clean_remote not in remotes:
        return False, f"rclone remote '{clean_remote}' not in listremotes"
    return True, f"rclone remote '{clean_remote}' ready"


def expected_openssl_enc_size(plaintext_bytes: int) -> int:
    """Return exact byte length produced by ``openssl enc -aes-256-cbc -pbkdf2 -salt``."""
    if plaintext_bytes < 0:
        raise ValueError("plaintext_bytes must be non-negative")
    return 16 + ((plaintext_bytes // 16) + 1) * 16


def parse_rclone_about_free_bytes(stdout_json: str) -> int | None:
    """Extract integer ``free`` bytes from ``rclone about <remote>: --json`` output."""
    if not stdout_json.strip():
        return None
    try:
        data = json.loads(stdout_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    free = data.get("free")
    if isinstance(free, bool) or not isinstance(free, int) or free < 0:
        return None
    return free


def check_drive_free_space(
    free_bytes: int | None,
    file_size_bytes: int,
    *,
    multiplier: int = 2,
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` checking Drive free space is at least ``multiplier * file_size``."""
    if free_bytes is None:
        return True, "rclone about free space unavailable; proceeding"
    required = file_size_bytes * multiplier
    if free_bytes < required:
        return (
            False,
            f"Drive free space {free_bytes} bytes is below {multiplier}x backup size ({required} bytes)",
        )
    return True, f"Drive free space {free_bytes} bytes >= {required} bytes"


def stream_cap_stdout(
    src_stream: Any,
    dst_stream: Any,
    *,
    max_bytes: int,
    require_prefix: bytes = b"",
    chunk_size: int = 1024 * 1024,
) -> tuple[bool, int]:
    """Forward stdin stream to stdout, aborting if raw byte count exceeds ``max_bytes``."""
    total = 0
    prefix_buf = bytearray()
    prefix_checked = not require_prefix
    while True:
        chunk = src_stream.read(chunk_size)
        if not chunk:
            break
        if not prefix_checked:
            prefix_buf.extend(chunk)
            if len(prefix_buf) >= len(require_prefix):
                if not bytes(prefix_buf).startswith(require_prefix):
                    raise ValueError(
                        f"Stream missing expected header prefix {require_prefix!r}"
                    )
                prefix_checked = True
        total += len(chunk)
        if max_bytes > 0 and total > max_bytes:
            return False, total
        dst_stream.write(chunk)
    if not prefix_checked:
        raise ValueError(
            f"Stream ended before matching expected header prefix {require_prefix!r}"
        )
    dst_stream.flush()
    return True, total


def stream_hash_and_forward(
    src_stream: Any,
    dst_stream: Any,
    meta_out: Path,
    *,
    chunk_size: int = 1024 * 1024,
) -> tuple[int, str]:
    """Tee bytes from ``src_stream`` to ``dst_stream`` while recording byte size and MD5."""
    meta_out.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.md5()
    total = 0
    while True:
        chunk = src_stream.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        h.update(chunk)
        dst_stream.write(chunk)
    dst_stream.flush()
    md5_hex = h.hexdigest()
    tmp_meta = meta_out.with_name(f".{meta_out.name}.tmp.{os.getpid()}")
    tmp_meta.write_text(
        json.dumps({"size": total, "md5": md5_hex}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_meta, meta_out)
    return total, md5_hex


def verify_rclone_object_size(
    lsjson_stdout: str,
    filename: str,
    expected_size: int,
) -> tuple[bool, str]:
    """Verify ``rclone lsjson`` reports ``filename`` with exact ``expected_size``."""
    try:
        items = json.loads(lsjson_stdout)
    except json.JSONDecodeError as exc:
        return False, f"invalid rclone lsjson output: {exc}"
    if not isinstance(items, list):
        return False, "rclone lsjson output is not a list"
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("Name") == filename or item.get("Path") == filename:
            size = item.get("Size")
            if isinstance(size, int) and not isinstance(size, bool) and size == expected_size:
                return True, f"remote size verified: {size} bytes"
            return False, f"remote size mismatch for {filename}: got {size}, expected {expected_size}"
    return False, f"remote object not found in lsjson: {filename}"


def verify_rclone_hashsum(
    hashsum_stdout: str,
    filename: str,
    expected_md5: str,
) -> tuple[bool, str]:
    """Verify ``rclone hashsum md5`` output matches ``expected_md5`` for ``filename``."""
    want = expected_md5.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", want):
        return False, f"invalid expected md5: {expected_md5!r}"
    for raw_line in hashsum_stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        digest = parts[0].strip().lower()
        obj_name = parts[1].strip()
        if obj_name != filename and Path(obj_name).name != filename:
            continue
        if not re.fullmatch(r"[0-9a-f]{32}", digest):
            return False, f"unsupported or malformed remote md5 for {filename}: {parts[0]}"
        if digest != want:
            return False, f"remote md5 mismatch for {filename}: got {digest}, expected {want}"
        return True, f"remote md5 verified: {digest}"
    return False, f"remote md5 not found in hashsum output for {filename}"


def format_gateway_alert_payload(*, source: str, code: str, message: str) -> str:
    """Build JSON body for ``POST /internal/ops-alerts`` with sanitized <=300 char detail."""
    raw_detail = f"{source}: {code} - {message}"
    no_ctrl = re.sub(r"[\r\n\t]+", " ", raw_detail)
    no_ctrl = re.sub(r"[\x00-\x1f\x7f]", "", no_ctrl).strip()
    detail = no_ctrl[:300]
    return json.dumps({"code": "backup_failed", "detail": detail}, ensure_ascii=False)


def select_weekly_dirs_to_prune(
    dir_names: Iterable[str],
    retain_weeks: int,
) -> list[str]:
    """Select ``YYYY-MM-DD`` directory names older than the newest ``retain_weeks``.

    Non-matching names (e.g. ``.tmp``, ``manual``, ``README.md``) are never returned.
    """
    if retain_weeks < 0:
        raise ValueError("retain_weeks must be non-negative")
    valid: list[tuple[dt.date, str]] = []
    for raw_name in dir_names:
        stripped = raw_name.strip().rstrip("/")
        m = _RE_WEEKLY_DATE_DIR.match(stripped)
        if not m:
            continue
        try:
            parsed = dt.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        valid.append((parsed, stripped))

    # Deduplicate by name, sort newest first.
    seen: set[str] = set()
    unique_valid: list[tuple[dt.date, str]] = []
    for parsed, name in sorted(valid, key=lambda x: (x[0], x[1]), reverse=True):
        if name not in seen:
            seen.add(name)
            unique_valid.append((parsed, name))

    to_drop = unique_valid[retain_weeks:]
    return [name for _, name in sorted(to_drop, key=lambda x: (x[0], x[1]))]


def prune_local_weekly_dirs(weekly_root: Path, retain_weeks: int) -> list[Path]:
    """Delete expired ``YYYY-MM-DD`` subdirectories under ``weekly_root`` only."""
    if not weekly_root.is_dir():
        return []
    candidates = [
        entry.name
        for entry in weekly_root.iterdir()
        if entry.is_dir() and not entry.is_symlink()
    ]
    prune_names = select_weekly_dirs_to_prune(candidates, retain_weeks)
    deleted: list[Path] = []
    resolved_root = weekly_root.resolve()
    for name in prune_names:
        target = weekly_root / name
        if target.is_symlink() or not target.is_dir():
            continue
        if target.resolve().parent != resolved_root:
            continue
        shutil.rmtree(target)
        deleted.append(target)
    return deleted


def format_alert_line(
    *,
    level: str,
    source: str,
    code: str,
    message: str,
    timestamp: dt.datetime | None = None,
) -> str:
    """Format a single-line ops backup alert suitable for line-oriented logs."""
    ts = (timestamp or dt.datetime.now().astimezone()).isoformat(timespec="seconds")
    clean_level = level.strip().upper() or "ERROR"
    clean_source = re.sub(r"\s+", "_", source.strip()) or "dalton_backup"
    clean_code = re.sub(r"\s+", "_", code.strip().upper()) or "BACKUP_ERROR"
    clean_msg = " ".join(message.splitlines()).strip()
    return f"{ts} [{clean_level}] [{clean_source}] [{clean_code}] {clean_msg}"


def _read_env_var_from_file(env_file: Path, key: str) -> str | None:
    if not env_file.is_file() or env_file.is_symlink():
        return None
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        k, v = line.split("=", 1)
        if k.strip() != key:
            continue
        cleaned, _ = _strip_quotes_and_comment(v)
        return cleaned
    return None


def _looks_like_json_config_file(path: Path) -> bool:
    if path.suffix.lower() == ".json":
        return True
    if path.is_file():
        try:
            head = path.read_text(encoding="utf-8", errors="ignore").lstrip()[:1]
            if head in {"{", "["}:
                return True
        except OSError:
            pass
    return False


def resolve_alerts_target(
    *,
    backup_alerts_file: str | None = None,
    ops_alerts_file: str | None = None,
    channel_gateway_env_file: Path | None = None,
    default_alerts_file: Path | None = None,
) -> tuple[Path, str | None]:
    """Choose the writable line-oriented alert file without corrupting JSON configs.

    Note: ``DALTON_OPS_ALERTS_FILE`` in ``dalton-channel-gateway`` points to a strict
    JSON config (``ops-alerts.json``). If it points to a ``.json`` or JSON file, we
    refuse to append lines to it and fall back to ``default_alerts_file``.
    """
    fallback = default_alerts_file or (
        Path.home() / "Library" / "Logs" / "dalton-offsite-backup-alerts.log"
    )
    if backup_alerts_file and backup_alerts_file.strip():
        return Path(os.path.expanduser(backup_alerts_file.strip())), None

    candidate_raw = (ops_alerts_file or "").strip()
    if not candidate_raw and channel_gateway_env_file is not None:
        candidate_raw = _read_env_var_from_file(
            channel_gateway_env_file, "DALTON_OPS_ALERTS_FILE"
        ) or ""

    if candidate_raw:
        candidate = Path(os.path.expanduser(candidate_raw))
        if _looks_like_json_config_file(candidate):
            return (
                fallback,
                f"Refusing to append log line to JSON config DALTON_OPS_ALERTS_FILE={candidate}; using {fallback}",
            )
        return candidate, None

    return fallback, None


def append_alert(
    *,
    level: str,
    source: str,
    code: str,
    message: str,
    backup_alerts_file: str | None = None,
    ops_alerts_file: str | None = None,
    channel_gateway_env_file: Path | None = None,
    default_alerts_file: Path | None = None,
    timestamp: dt.datetime | None = None,
) -> tuple[Path, str | None]:
    """Append one formatted alert line to the resolved alerts log file."""
    target, warning = resolve_alerts_target(
        backup_alerts_file=backup_alerts_file,
        ops_alerts_file=ops_alerts_file,
        channel_gateway_env_file=channel_gateway_env_file,
        default_alerts_file=default_alerts_file,
    )
    line = format_alert_line(
        level=level,
        source=source,
        code=code,
        message=message,
        timestamp=timestamp,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        raise
    return target, warning


# ---------------------------------------------------------------------------
# CLI entrypoints invoked by dalton_config_git_backup.sh & dalton_data_backup.sh
# ---------------------------------------------------------------------------


def _cmd_stage_config(args: argparse.Namespace) -> int:
    sources = [
        SyncSourceSpec(
            name="var_dalton",
            src_dir=Path(args.prod_var_dir),
            dest_subdir="var_dalton",
            excludes=parse_pattern_list(args.var_excludes),
        ),
        SyncSourceSpec(
            name="dot_config_dalton",
            src_dir=Path(args.user_config_dir),
            dest_subdir="dot_config_dalton",
            excludes=parse_pattern_list(args.user_excludes),
        ),
        SyncSourceSpec(
            name="launch_agents",
            src_dir=Path(args.launchagents_dir),
            dest_subdir="launch_agents",
            include_globs=parse_pattern_list(args.launchagents_globs),
            top_level_only=True,
        ),
        SyncSourceSpec(
            name="backup_scripts",
            src_dir=Path(args.backup_scripts_dir),
            dest_subdir="backup_scripts",
            include_globs=parse_pattern_list(args.backup_scripts_globs),
            top_level_only=True,
        ),
        SyncSourceSpec(
            name="colima",
            src_dir=Path(args.colima_dir),
            dest_subdir="colima",
            excludes=parse_pattern_list(args.colima_excludes),
        ),
        SyncSourceSpec(
            name="prod_overrides",
            src_dir=Path(args.prod_checkout_dir),
            dest_subdir="prod_overrides",
            include_globs=parse_pattern_list(args.prod_override_patterns),
            top_level_only=True,
        ),
    ]
    summary = stage_config_repo(
        sources=sources,
        stage_dir=Path(args.stage_dir),
        existing_repo_dir=Path(args.repo_dir),
        key_file=Path(args.key_file),
        max_file_bytes=int(args.max_file_mb) * 1024 * 1024,
        log_fn=lambda msg: print(msg, file=sys.stderr),
    )
    print(json.dumps(dataclasses.asdict(summary), ensure_ascii=False))
    return 0


def _cmd_scan_plaintext(args: argparse.Namespace) -> int:
    findings = scan_plaintext_tree(Path(args.repo_dir))
    if findings:
        for rel, reason in findings:
            print(f"PLAINTEXT_SECRET {rel} ({reason})", file=sys.stderr)
        return 1
    return 0


def _cmd_verify_mount(args: argparse.Namespace) -> int:
    mount_root = Path(args.mount_root)
    targets = list(args.target_path or ())
    if not targets and not args.backups_root:
        print("verify-mount requires --target-path and/or --backups-root", file=sys.stderr)
        return 2
    if args.backups_root:
        try:
            verify_backup_mount_layout(mount_root, Path(args.backups_root))
        except MountCheckError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    for raw_target in targets:
        try:
            verify_external_mount(mount_root, Path(raw_target))
        except MountCheckError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    return 0


def _cmd_preflight_disk_space(args: argparse.Namespace) -> int:
    colima_fn = None
    if args.colima_df_file:
        df_text = Path(args.colima_df_file).read_text(encoding="utf-8")
        colima_fn = lambda: parse_df_available_bytes(df_text)  # noqa: E731
    report = preflight_disk_space(
        external_mount_root=Path(args.mount_root),
        internal_root=Path(args.internal_root),
        colima_dir=Path(args.colima_dir) if args.colima_dir else None,
        colima_profile=args.colima_profile or None,
        min_external_gb=int(args.min_external_gb),
        min_internal_gb=int(args.min_internal_gb),
        min_colima_docker_gb=int(args.min_colima_gb),
        colima_docker_free_bytes_fn=colima_fn,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    if not report.ok:
        for item in report.checks:
            if not item.ok:
                print(f"PREFLIGHT_FAIL [{item.name}]: {item.detail}", file=sys.stderr)
        return 1
    return 0


@dataclasses.dataclass(frozen=True)
class KnownMissingObject:
    """One referenced object registered as already missing in a backup-set or object manifest."""

    bucket: str
    key: str
    consumer: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "bucket": self.bucket,
            "key": self.key,
            "consumer": self.consumer,
        }


@dataclasses.dataclass(frozen=True)
class RestoredRefsVerifyReport:
    """Result of verifying restored objects against DB/snapshot references and registered known missing items."""

    ok: bool
    checked_count: int
    present_count: int
    known_missing: tuple[KnownMissingObject, ...] = ()
    unexpected_missing: tuple[KnownMissingObject, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_count": self.checked_count,
            "present_count": self.present_count,
            "known_missing_count": len(self.known_missing),
            "unexpected_missing_count": len(self.unexpected_missing),
            "known_missing": [item.to_dict() for item in self.known_missing],
            "unexpected_missing": [item.to_dict() for item in self.unexpected_missing],
        }


def load_known_missing_objects(
    manifest_or_set: Mapping[str, Any] | Path,
) -> tuple[KnownMissingObject, ...]:
    """Load registered ``missing_referenced_objects`` from a backup-set or object manifest."""
    if isinstance(manifest_or_set, Path):
        data = json.loads(manifest_or_set.read_text(encoding="utf-8"))
    else:
        data = manifest_or_set
    if not isinstance(data, Mapping):
        return ()
    raw_items = data.get("missing_referenced_objects")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        return ()
    out: list[KnownMissingObject] = []
    seen: set[tuple[str, str, str]] = set()
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        bucket = str(item.get("bucket") or "").strip()
        key = str(item.get("key") or "").strip().lstrip("/")
        consumer = str(item.get("consumer") or "").strip()
        if not bucket or not key:
            continue
        triple = (bucket, key, consumer)
        if triple in seen:
            continue
        seen.add(triple)
        out.append(KnownMissingObject(bucket=bucket, key=key, consumer=consumer))
    return tuple(out)


def verify_restored_references(
    referenced_objects: Sequence[Mapping[str, Any] | tuple[str, str] | tuple[str, str, str]],
    present_objects: Iterable[Mapping[str, Any] | tuple[str, str]],
    *,
    known_missing_objects: Sequence[Mapping[str, Any] | KnownMissingObject | tuple[str, str]] = (),
    manifest_or_set: Mapping[str, Any] | Path | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> RestoredRefsVerifyReport:
    """Verify restored bucket objects against referenced keys, treating manifest-registered missing items as known missing.

    - Registered items in ``known_missing_objects`` (or loaded from ``manifest_or_set``) are
      reported in ``known_missing`` and via ``log_fn``, and do NOT fail verification (``ok=True``).
    - Any unregistered missing object is reported in ``unexpected_missing`` and fails verification
      (``ok=False``).
    """
    present_pairs: set[tuple[str, str]] = set()
    for item in present_objects:
        if isinstance(item, Mapping):
            b = str(item.get("bucket") or "").strip()
            k = str(item.get("key") or "").strip().lstrip("/")
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            b = str(item[0] or "").strip()
            k = str(item[1] or "").strip().lstrip("/")
        else:
            continue
        if b and k:
            present_pairs.add((b, k))

    known_pairs: dict[tuple[str, str], str] = {}
    if manifest_or_set is not None:
        for km in load_known_missing_objects(manifest_or_set):
            known_pairs[(km.bucket, km.key)] = km.consumer
    for item in known_missing_objects:
        if isinstance(item, KnownMissingObject):
            b, k, c = item.bucket.strip(), item.key.strip().lstrip("/"), item.consumer.strip()
        elif isinstance(item, Mapping):
            b = str(item.get("bucket") or "").strip()
            k = str(item.get("key") or "").strip().lstrip("/")
            c = str(item.get("consumer") or "").strip()
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            b = str(item[0] or "").strip()
            k = str(item[1] or "").strip().lstrip("/")
            c = str(item[2] or "").strip() if len(item) >= 3 else ""
        else:
            continue
        if b and k:
            known_pairs.setdefault((b, k), c)

    present_count = 0
    checked_count = 0
    known_missing_hits: list[KnownMissingObject] = []
    unexpected_missing_hits: list[KnownMissingObject] = []

    for ref in referenced_objects:
        if isinstance(ref, Mapping):
            b = str(ref.get("bucket") or "").strip()
            k = str(ref.get("key") or "").strip().lstrip("/")
            c = str(ref.get("consumer") or "").strip()
        elif isinstance(ref, (tuple, list)) and len(ref) >= 2:
            b = str(ref[0] or "").strip()
            k = str(ref[1] or "").strip().lstrip("/")
            c = str(ref[2] or "").strip() if len(ref) >= 3 else ""
        else:
            continue
        if not b or not k:
            continue
        checked_count += 1
        if (b, k) in present_pairs:
            present_count += 1
        elif (b, k) in known_pairs:
            known_missing_hits.append(
                KnownMissingObject(bucket=b, key=k, consumer=c or known_pairs[(b, k)])
            )
        else:
            unexpected_missing_hits.append(KnownMissingObject(bucket=b, key=k, consumer=c))

    if known_missing_hits and log_fn is not None:
        sample = ", ".join(f"{x.bucket}/{x.key}" for x in known_missing_hits[:10])
        suffix = (
            f" (+{len(known_missing_hits) - 10} more)"
            if len(known_missing_hits) > 10
            else ""
        )
        log_fn(
            f"KNOWN_MISSING_OBJECTS count={len(known_missing_hits)} "
            f"(registered in backup manifest, not treated as restore failure): {sample}{suffix}"
        )

    return RestoredRefsVerifyReport(
        ok=len(unexpected_missing_hits) == 0,
        checked_count=checked_count,
        present_count=present_count,
        known_missing=tuple(known_missing_hits),
        unexpected_missing=tuple(unexpected_missing_hits),
    )


def _cmd_reconcile_restored_inflight_states(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from src.dalton import consistent_backup

    sub_argv = ["reconcile-restored-inflight-states"]
    if args.skip_object_check:
        sub_argv.append("--skip-object-check")
    if args.asr_jobs_dir:
        sub_argv.extend(["--asr-jobs-dir", args.asr_jobs_dir])
    if args.restored_before:
        sub_argv.extend(["--restored-before", args.restored_before])
    return consistent_backup.main(sub_argv)


def _cmd_verify_restored_refs(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from src.dalton import storage_refs

    manifest_path = str(args.manifest or "").strip()
    backup_set_path = str(args.backup_set or "").strip()
    if not manifest_path and backup_set_path:
        set_data = json.loads(Path(backup_set_path).read_text(encoding="utf-8"))
        if isinstance(set_data, dict):
            manifest_path = str(set_data.get("manifest_path") or "").strip()
    if not manifest_path:
        print("verify-restored-refs requires --manifest or --backup-set with manifest_path", file=sys.stderr)
        return 2

    sub_argv = ["reconcile", "--manifest", manifest_path]
    if backup_set_path:
        sub_argv.extend(["--backup-set", backup_set_path])
    if args.refs_json:
        sub_argv.extend(["--refs-json", args.refs_json])
    if args.asr_archive:
        sub_argv.extend(["--asr-archive", args.asr_archive])
    if args.asr_dir:
        sub_argv.extend(["--asr-dir", args.asr_dir])
    if args.source_asr_dir:
        sub_argv.extend(["--source-asr-dir", args.source_asr_dir])
    if args.main_bucket:
        sub_argv.extend(["--main-bucket", args.main_bucket])
    if args.main_prefix:
        sub_argv.extend(["--main-prefix", args.main_prefix])
    if args.aux_bucket:
        sub_argv.extend(["--aux-bucket", args.aux_bucket])
    if args.aux_prefix:
        sub_argv.extend(["--aux-prefix", args.aux_prefix])
    return int(storage_refs.main(sub_argv))


def _cmd_latest_pg_dump(args: argparse.Namespace) -> int:
    dump = find_latest_pg_dump(Path(args.pg_dir))
    if dump is None:
        print(f"No complete dalton-pg-*.dump found in {args.pg_dir}", file=sys.stderr)
        return 1
    print(str(dump))
    return 0


def _cmd_stream_cap(args: argparse.Namespace) -> int:
    ok, total = stream_copy_with_limit(
        sys.stdin.buffer,
        Path(args.out),
        max_bytes=int(args.max_bytes),
        gzip_output=bool(args.gzip),
    )
    if not ok:
        print(
            f"Stream exceeded limit ({total} > {args.max_bytes} bytes)",
            file=sys.stderr,
        )
        return STREAM_CAP_EXIT_CODE
    print(str(total))
    return 0


def _cmd_stream_cap_stdout(args: argparse.Namespace) -> int:
    req_prefix = args.require_prefix.encode("utf-8") if args.require_prefix else b""
    try:
        ok, total = stream_cap_stdout(
            sys.stdin.buffer,
            sys.stdout.buffer,
            max_bytes=int(args.max_bytes),
            require_prefix=req_prefix,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 1
    if not ok:
        print(
            f"Stream exceeded limit ({total} > {args.max_bytes} bytes)",
            file=sys.stderr,
        )
        return STREAM_CAP_EXIT_CODE
    return 0


def _cmd_stream_hash_meta(args: argparse.Namespace) -> int:
    try:
        stream_hash_and_forward(
            sys.stdin.buffer,
            sys.stdout.buffer,
            Path(args.meta_out),
        )
    except BrokenPipeError:
        return 1
    return 0


def _load_stream_meta(meta_file: Path) -> dict[str, Any]:
    data = json.loads(meta_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("meta file JSON is not an object")
    return data


def _cmd_check_rclone(args: argparse.Namespace) -> int:
    ok, msg = check_rclone_remote(args.remote)
    print(msg)
    return 0 if ok else 1


def _cmd_check_drive_space(args: argparse.Namespace) -> int:
    about_json = sys.stdin.read()
    free = parse_rclone_about_free_bytes(about_json)
    ok, msg = check_drive_free_space(free, int(args.file_size), multiplier=int(args.multiplier))
    print(msg)
    return 0 if ok else 1


def _cmd_expected_enc_size(args: argparse.Namespace) -> int:
    print(str(expected_openssl_enc_size(int(args.plain_size))))
    return 0


def _cmd_verify_remote_size(args: argparse.Namespace) -> int:
    lsjson_out = sys.stdin.read()
    if args.meta_file:
        expected_size = int(_load_stream_meta(Path(args.meta_file))["size"])
    elif args.expected_size is not None:
        expected_size = int(args.expected_size)
    else:
        print("verify-remote-size requires --expected-size or --meta-file", file=sys.stderr)
        return 2
    ok, msg = verify_rclone_object_size(lsjson_out, args.filename, expected_size)
    print(msg)
    return 0 if ok else 1


def _cmd_verify_remote_hash(args: argparse.Namespace) -> int:
    hashsum_out = sys.stdin.read()
    if args.meta_file:
        expected_md5 = str(_load_stream_meta(Path(args.meta_file))["md5"])
    elif args.expected_md5:
        expected_md5 = str(args.expected_md5)
    else:
        print("verify-remote-hash requires --expected-md5 or --meta-file", file=sys.stderr)
        return 2
    ok, msg = verify_rclone_hashsum(hashsum_out, args.filename, expected_md5)
    print(msg)
    return 0 if ok else 1


def _cmd_gateway_payload(args: argparse.Namespace) -> int:
    print(format_gateway_alert_payload(source=args.source, code=args.code, message=args.message))
    return 0


def _cmd_prune_local(args: argparse.Namespace) -> int:
    deleted = prune_local_weekly_dirs(Path(args.weekly_root), int(args.retain))
    for path in deleted:
        print(str(path))
    return 0


def _cmd_select_prune_names(args: argparse.Namespace) -> int:
    lines = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
    for name in select_weekly_dirs_to_prune(lines, int(args.retain)):
        print(name)
    return 0


def _cmd_emit_alert(args: argparse.Namespace) -> int:
    gw_env = Path(args.gateway_env_file) if args.gateway_env_file else None
    default_file = Path(args.default_file) if args.default_file else None
    target, warning = append_alert(
        level=args.level,
        source=args.source,
        code=args.code,
        message=args.message,
        backup_alerts_file=args.backup_alerts_file or None,
        ops_alerts_file=args.ops_alerts_file or None,
        channel_gateway_env_file=gw_env,
        default_alerts_file=default_file,
    )
    if warning:
        print(f"WARN {warning}", file=sys.stderr)
    print(str(target))
    return 0


def _cmd_snapshot_sqlite(args: argparse.Namespace) -> int:
    excludes: str | None = None if args.excludes is None else args.excludes
    try:
        paths = snapshot_sqlite(
            Path(args.src_dir),
            Path(args.dest_dir),
            excludes=excludes,
        )
    except SqliteSnapshotError as exc:
        print(f"SQLite snapshot failed: {exc}", file=sys.stderr)
        return 1
    if paths:
        sys.stdout.buffer.write(b"\0".join(p.encode("utf-8") for p in paths) + b"\0")
        sys.stdout.buffer.flush()
    return 0


def _cmd_var_dalton_tar_cmd(args: argparse.Namespace) -> int:
    excludes: str | None = None if args.excludes is None else args.excludes
    sqlite_paths: list[str] = list(args.sqlite_path or ())
    if args.sqlite_manifest:
        raw = Path(args.sqlite_manifest).read_bytes()
        for item in raw.split(b"\0"):
            if item:
                sqlite_paths.append(item.decode("utf-8"))
    staging_dir = Path(args.staging_dir) if args.staging_dir else None
    cmd = build_var_dalton_tar_cmd(
        Path(args.src_dir),
        excludes=excludes,
        sqlite_paths=sqlite_paths,
        staging_dir=staging_dir,
    )
    sys.stdout.buffer.write(b"\0".join(tok.encode("utf-8") for tok in cmd) + b"\0")
    sys.stdout.buffer.flush()
    return 0


def _cmd_should_retry_tar(args: argparse.Namespace) -> int:
    try:
        ok = should_retry_tar_failure(
            int(args.attempt),
            args.pipe_status,
            max_retries=int(args.max_retries),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0 if ok else 1


def _cmd_summarize_tar_stderr(args: argparse.Namespace) -> int:
    if args.err_file:
        text = Path(args.err_file).read_text(encoding="utf-8", errors="replace")
    else:
        text = sys.stdin.read()
    print(summarize_tar_stderr(text, max_chars=int(args.max_chars)))
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Dalton offsite backup helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_stage = sub.add_parser("stage-config")
    p_stage.add_argument("--stage-dir", required=True)
    p_stage.add_argument("--repo-dir", required=True)
    p_stage.add_argument("--key-file", required=True)
    p_stage.add_argument("--max-file-mb", type=int, default=20)
    p_stage.add_argument("--prod-var-dir", required=True)
    p_stage.add_argument("--var-excludes", default="")
    p_stage.add_argument("--user-config-dir", required=True)
    p_stage.add_argument("--user-excludes", default="")
    p_stage.add_argument("--launchagents-dir", required=True)
    p_stage.add_argument("--launchagents-globs", default="com.dalton.*.plist")
    p_stage.add_argument("--backup-scripts-dir", required=True)
    p_stage.add_argument("--backup-scripts-globs", default="*.sh,*.py")
    p_stage.add_argument("--colima-dir", required=True)
    p_stage.add_argument("--colima-excludes", default="")
    p_stage.add_argument("--prod-checkout-dir", required=True)
    p_stage.add_argument("--prod-override-patterns", default="")
    p_stage.set_defaults(func=_cmd_stage_config)

    p_scan = sub.add_parser("scan-plaintext")
    p_scan.add_argument("--repo-dir", required=True)
    p_scan.set_defaults(func=_cmd_scan_plaintext)

    p_mount = sub.add_parser("verify-mount")
    p_mount.add_argument("--mount-root", required=True)
    p_mount.add_argument("--target-path", nargs="+", default=[])
    p_mount.add_argument("--backups-root", default="")
    p_mount.set_defaults(func=_cmd_verify_mount)

    p_preflight = sub.add_parser("preflight-disk-space")
    p_preflight.add_argument("--mount-root", default="/Volumes/EveSSD")
    p_preflight.add_argument("--internal-root", default="/")
    p_preflight.add_argument("--colima-dir", default="")
    p_preflight.add_argument("--colima-profile", default="")
    p_preflight.add_argument("--min-external-gb", type=int, default=PREFLIGHT_EXT_SSD_MIN_GB)
    p_preflight.add_argument("--min-internal-gb", type=int, default=PREFLIGHT_INTERNAL_ROOT_MIN_GB)
    p_preflight.add_argument("--min-colima-gb", type=int, default=PREFLIGHT_COLIMA_DOCKER_MIN_GB)
    p_preflight.add_argument("--colima-df-file", default="")
    p_preflight.set_defaults(func=_cmd_preflight_disk_space)

    p_rri = sub.add_parser("reconcile-restored-inflight-states")
    p_rri.add_argument("--skip-object-check", action="store_true")
    p_rri.add_argument("--asr-jobs-dir", default="")
    p_rri.add_argument("--restored-before", default="")
    p_rri.set_defaults(func=_cmd_reconcile_restored_inflight_states)

    p_vrr = sub.add_parser("verify-restored-refs")
    p_vrr.add_argument("--manifest", default="")
    p_vrr.add_argument("--backup-set", default="")
    p_vrr.add_argument("--refs-json", default="")
    p_vrr.add_argument("--asr-archive", default="")
    p_vrr.add_argument("--asr-dir", default="")
    p_vrr.add_argument("--source-asr-dir", default="")
    p_vrr.add_argument("--main-bucket", default="")
    p_vrr.add_argument("--main-prefix", default="")
    p_vrr.add_argument("--aux-bucket", default="")
    p_vrr.add_argument("--aux-prefix", default="")
    p_vrr.set_defaults(func=_cmd_verify_restored_refs)

    p_pg = sub.add_parser("latest-pg-dump")
    p_pg.add_argument("--pg-dir", required=True)
    p_pg.set_defaults(func=_cmd_latest_pg_dump)

    p_cap = sub.add_parser("stream-cap")
    p_cap.add_argument("--out", required=True)
    p_cap.add_argument("--max-bytes", type=int, required=True)
    p_cap.add_argument("--gzip", action="store_true")
    p_cap.set_defaults(func=_cmd_stream_cap)

    p_cap_out = sub.add_parser("stream-cap-stdout")
    p_cap_out.add_argument("--max-bytes", type=int, required=True)
    p_cap_out.add_argument("--require-prefix", default="")
    p_cap_out.set_defaults(func=_cmd_stream_cap_stdout)

    p_shm = sub.add_parser("stream-hash-meta")
    p_shm.add_argument("--meta-out", required=True)
    p_shm.set_defaults(func=_cmd_stream_hash_meta)

    p_rc = sub.add_parser("check-rclone")
    p_rc.add_argument("--remote", required=True)
    p_rc.set_defaults(func=_cmd_check_rclone)

    p_ds = sub.add_parser("check-drive-space")
    p_ds.add_argument("--file-size", type=int, required=True)
    p_ds.add_argument("--multiplier", type=int, default=2)
    p_ds.set_defaults(func=_cmd_check_drive_space)

    p_es = sub.add_parser("expected-enc-size")
    p_es.add_argument("--plain-size", type=int, required=True)
    p_es.set_defaults(func=_cmd_expected_enc_size)

    p_vr = sub.add_parser("verify-remote-size")
    p_vr.add_argument("--filename", required=True)
    p_vr.add_argument("--expected-size", type=int, default=None)
    p_vr.add_argument("--meta-file", default="")
    p_vr.set_defaults(func=_cmd_verify_remote_size)

    p_vh = sub.add_parser("verify-remote-hash")
    p_vh.add_argument("--filename", required=True)
    p_vh.add_argument("--expected-md5", default="")
    p_vh.add_argument("--meta-file", default="")
    p_vh.set_defaults(func=_cmd_verify_remote_hash)

    p_gp = sub.add_parser("gateway-payload")
    p_gp.add_argument("--source", required=True)
    p_gp.add_argument("--code", required=True)
    p_gp.add_argument("--message", required=True)
    p_gp.set_defaults(func=_cmd_gateway_payload)

    p_pl = sub.add_parser("prune-local")
    p_pl.add_argument("--weekly-root", required=True)
    p_pl.add_argument("--retain", type=int, required=True)
    p_pl.set_defaults(func=_cmd_prune_local)

    p_sp = sub.add_parser("select-prune-names")
    p_sp.add_argument("--retain", type=int, required=True)
    p_sp.set_defaults(func=_cmd_select_prune_names)

    p_al = sub.add_parser("emit-alert")
    p_al.add_argument("--level", default="ERROR")
    p_al.add_argument("--source", required=True)
    p_al.add_argument("--code", required=True)
    p_al.add_argument("--message", required=True)
    p_al.add_argument("--backup-alerts-file", default="")
    p_al.add_argument("--ops-alerts-file", default="")
    p_al.add_argument("--gateway-env-file", default="")
    p_al.add_argument("--default-file", default="")
    p_al.set_defaults(func=_cmd_emit_alert)

    p_ss = sub.add_parser("snapshot-sqlite")
    p_ss.add_argument("--src-dir", required=True)
    p_ss.add_argument("--dest-dir", required=True)
    p_ss.add_argument("--excludes", default=None)
    p_ss.set_defaults(func=_cmd_snapshot_sqlite)

    p_vt = sub.add_parser("var-dalton-tar-cmd")
    p_vt.add_argument("--src-dir", required=True)
    p_vt.add_argument("--excludes", default=None)
    p_vt.add_argument("--sqlite-manifest", default="")
    p_vt.add_argument("--sqlite-path", action="append", default=[])
    p_vt.add_argument("--staging-dir", default="")
    p_vt.set_defaults(func=_cmd_var_dalton_tar_cmd)

    p_rt = sub.add_parser("should-retry-tar")
    p_rt.add_argument("--attempt", type=int, required=True)
    p_rt.add_argument("--pipe-status", required=True)
    p_rt.add_argument("--max-retries", type=int, default=1)
    p_rt.set_defaults(func=_cmd_should_retry_tar)

    p_st = sub.add_parser("summarize-tar-stderr")
    p_st.add_argument("--err-file", default="")
    p_st.add_argument("--max-chars", type=int, default=240)
    p_st.set_defaults(func=_cmd_summarize_tar_stderr)

    return ap


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
