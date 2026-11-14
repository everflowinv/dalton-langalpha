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
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable

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


class MountCheckError(RuntimeError):
    """Raised when the external backup drive is missing, unmounted, or mismatched."""


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


def matches_exclude(rel_path: str, patterns: Iterable[str]) -> bool:
    """Return True if ``rel_path`` (POSIX relative path) matches any exclude rule."""
    rel = rel_path.replace("\\", "/").strip("/")
    if not rel:
        return False
    parts = rel.split("/")
    basename = parts[-1]
    for raw_pat in patterns:
        pat = raw_pat.replace("\\", "/").strip("/")
        if not pat:
            continue
        if "/" in pat:
            if rel == pat or rel.startswith(pat + "/") or fnmatch.fnmatch(rel, pat):
                return True
        else:
            if fnmatch.fnmatch(basename, pat) or any(
                fnmatch.fnmatch(seg, pat) for seg in parts
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


def verify_external_mount(
    mount_root: Path,
    target_path: Path,
    *,
    is_mount_fn: Callable[[Path], bool] = os.path.ismount,
) -> None:
    """Ensure ``mount_root`` is a mounted volume and ``target_path`` lives inside it.

    Refuses to run if ``mount_root`` is missing or merely an unmounted folder on ``/``,
    preventing weekly backups from spilling onto the internal disk.
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
    for raw_target in args.target_path:
        try:
            verify_external_mount(mount_root, Path(raw_target))
        except MountCheckError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    return 0


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
    p_mount.add_argument("--target-path", nargs="+", required=True)
    p_mount.set_defaults(func=_cmd_verify_mount)

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

    return ap


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
