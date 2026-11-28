#!/usr/bin/env python3
"""Post-packaging gate for Standalone Core release tarballs.

Rejects any release archive containing:
- macOS AppleDouble files (`._*` at any directory depth)
- Finder metadata files (`.DS_Store` at any directory depth)
- Extended attribute or macOS/bsdtar metadata PAX headers (`LIBARCHIVE.xattr.*`,
  `SCHILY.xattr.*`, `SCHILY.fflags`, `LIBARCHIVE.fflags`, `LIBARCHIVE.creationtime`,
  `LIBARCHIVE.mac_metadata`), either per-member or global
- Missing, non-directory, empty, or mismatched single top-level directory when
  `--expected-top-dir` is given
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import importlib.util
import re
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

FORBIDDEN_PAX_PREFIXES: tuple[str, ...] = (
    "LIBARCHIVE.xattr.",
    "SCHILY.xattr.",
)

FORBIDDEN_PAX_KEYS: frozenset[str] = frozenset(
    {
        "SCHILY.fflags",
        "LIBARCHIVE.fflags",
        "LIBARCHIVE.creationtime",
        "LIBARCHIVE.mac_metadata",
    }
)

# Everflow-only subpaths that must never ship inside a Standalone Core archive.
FORBIDDEN_RELEASE_SUBPATHS: tuple[tuple[str, ...], ...] = (
    ("plugins", "dalton_data"),
    ("plugins", "dalton", "skills", "dalton-richard-coverage-weekly"),
    ("plugins", "dalton", "skills", "dalton-ai-app-weekly-monitor"),
    ("src", "dalton", "data_jobs", "everflow_defaults.yaml"),
    ("web", ".env.dalton"),
)

# `scripts/dalton/` is default-deny: only these paths ship in Core (they must
# match the release allowlist in package_release.sh). Everything else under it
# (the research runtime, ops and launchd scripts, data_assets/skills, Everflow-only
# bridges) is rejected without having to enumerate every private file.
CORE_SCRIPTS_DALTON_PREFIX: tuple[str, ...] = ("scripts", "dalton")
CORE_SCRIPTS_DALTON_ALLOWED: frozenset[str] = frozenset(
    {
        "Dockerfile.dalton",
        "Dockerfile.postgres",
        "docker_entrypoint_dalton.sh",
        "configure_minio.sh",
        "log_redaction.py",
        "seed_openclaw_data.sh",
        "sql/personal_integrations_restore_reset.sql",
        "data_assets/scripts",
        "data_assets/everflow_only.txt",
    }
)
CORE_SCRIPTS_DALTON_ALLOWED_DIRS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("data_assets", "scripts"),
    }
)
_CORE_SCRIPTS_DALTON_ALLOWED_PARTS: frozenset[tuple[str, ...]] = frozenset(
    tuple(entry.split("/")) for entry in CORE_SCRIPTS_DALTON_ALLOWED
)
_CORE_SCRIPTS_DALTON_PARENT_DIRS: frozenset[tuple[str, ...]] = frozenset(
    parts[:i]
    for parts in _CORE_SCRIPTS_DALTON_ALLOWED_PARTS
    for i in range(1, len(parts))
)

# Post-package content gate matching out-of-package checker (zero proprietary leaks).
FORBIDDEN_CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("everflow_company_domain", re.compile(r"everflowinv", re.IGNORECASE)),
    ("personal_home_path", re.compile(r"/Users/richardlu", re.IGNORECASE)),
    ("everflow_home_path", re.compile(r"/Users/everflow", re.IGNORECASE)),
    (
        "windows_eve_path",
        re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[/\\]Eve(?![A-Za-z0-9_])", re.IGNORECASE),
    ),
    ("feishu_chat_id", re.compile(r"oc_[0-9a-f]{20,}", re.IGNORECASE)),
    ("feishu_user_id", re.compile(r"ou_[0-9a-f]{20,}", re.IGNORECASE)),
    ("discord_target_id", re.compile(r"(?:user|channel):[0-9]{15,}", re.IGNORECASE)),
)

_BINARY_SUFFIXES: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".bmp",
        ".pdf",
        ".zip",
        ".gz",
        ".tgz",
        ".tar",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".parquet",
        ".pyc",
        ".pyo",
        ".so",
        ".dylib",
        ".wasm",
    }
)


def _matches_forbidden_subpath(rel_parts: tuple[str, ...]) -> tuple[str, ...] | None:
    for forbidden in FORBIDDEN_RELEASE_SUBPATHS:
        if len(rel_parts) >= len(forbidden) and rel_parts[: len(forbidden)] == forbidden:
            return forbidden
    prefix_len = len(CORE_SCRIPTS_DALTON_PREFIX)
    if len(rel_parts) > prefix_len and rel_parts[:prefix_len] == CORE_SCRIPTS_DALTON_PREFIX:
        sub = rel_parts[prefix_len:]
        if sub in _CORE_SCRIPTS_DALTON_ALLOWED_PARTS or sub in _CORE_SCRIPTS_DALTON_PARENT_DIRS:
            return None
        for allowed_dir in CORE_SCRIPTS_DALTON_ALLOWED_DIRS:
            if len(sub) > len(allowed_dir) and sub[: len(allowed_dir)] == allowed_dir:
                return None
        if len(sub) >= 2 and sub[:1] in _CORE_SCRIPTS_DALTON_PARENT_DIRS:
            return rel_parts[: prefix_len + 2]
        return rel_parts[: prefix_len + 1]
    return None


def _is_forbidden_pax_key(key: str) -> bool:
    return key in FORBIDDEN_PAX_KEYS or any(
        key.startswith(prefix) for prefix in FORBIDDEN_PAX_PREFIXES
    )


def _normalized_parts(member_name: str) -> tuple[str, ...]:
    """Split a tar member name strictly by POSIX `/` separators."""
    parts = PurePosixPath(member_name).parts
    return tuple(p for p in parts if p not in ("", ".", "/"))


def _load_ci_test_exempt_checker() -> Callable[[str], bool]:
    """Load the single-source CI test-path exemption matcher from check_core_release_package.py."""
    checker_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "dalton"
        / "ops"
        / "check_core_release_package.py"
    )
    if not checker_path.is_file():
        raise RuntimeError(
            f"--include-ci requires out-of-package scanner at {checker_path}"
        )
    mod_name = "_dalton_check_core_release_package"
    mod = sys.modules.get(mod_name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(mod_name, checker_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Failed to load out-of-package scanner at {checker_path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    return mod.is_ci_test_exempt_path


def inspect_tarball(
    tar_path: Path | str | None = None,
    *,
    fileobj: BinaryIO | None = None,
    expected_top_dir: str | None = None,
    include_ci: bool = False,
) -> list[str]:
    """Inspect a tar archive (path or in-memory fileobj) and return policy violations."""
    if tar_path is None and fileobj is None:
        raise ValueError("inspect_tarball requires tar_path or fileobj")
    is_ci_test_exempt = _load_ci_test_exempt_checker() if include_ci else None
    violations: list[str] = []
    member_count = 0
    recognized_top_dirs: set[str] = set()
    nested_file_count = 0

    open_kwargs = {"fileobj": fileobj, "mode": "r:*"} if fileobj is not None else {"name": Path(tar_path), "mode": "r:*"}
    with tarfile.open(**open_kwargs) as tf:
        global_pax = getattr(tf, "pax_headers", None) or {}
        for key in sorted(global_pax):
            if _is_forbidden_pax_key(key):
                violations.append(f"global pax header forbidden metadata: {key}")

        for member in tf.getmembers():
            member_count += 1
            raw_name = member.name
            parts = _normalized_parts(raw_name)

            for part in parts:
                if part.startswith("._"):
                    violations.append(f"forbidden AppleDouble member: {member.name}")
                    break
                if part == ".DS_Store":
                    violations.append(f"forbidden .DS_Store member: {member.name}")
                    break

            rel_parts = (
                parts[1:]
                if expected_top_dir is not None and parts and parts[0] == expected_top_dir
                else parts
            )
            matched_forbidden = _matches_forbidden_subpath(rel_parts)
            if (
                matched_forbidden is None
                and not member.isdir()
                and len(rel_parts) > len(CORE_SCRIPTS_DALTON_PREFIX)
                and rel_parts[: len(CORE_SCRIPTS_DALTON_PREFIX)] == CORE_SCRIPTS_DALTON_PREFIX
                and rel_parts[len(CORE_SCRIPTS_DALTON_PREFIX) :] in _CORE_SCRIPTS_DALTON_PARENT_DIRS
            ):
                matched_forbidden = rel_parts
            if matched_forbidden is not None:
                violations.append(
                    f"forbidden Everflow-only member in core archive: {member.name} "
                    f"(matched {'/'.join(matched_forbidden)})"
                )
            elif parts and parts[-1].endswith(".dalton.yaml"):
                violations.append(
                    f"forbidden Everflow overlay member in core archive: {member.name}"
                )

            pax_headers = member.pax_headers or {}
            for key in sorted(pax_headers):
                if _is_forbidden_pax_key(key):
                    violations.append(
                        f"forbidden metadata pax header on {member.name}: {key}"
                    )

            rel_posix = "/".join(rel_parts)
            exempt_ci_content = bool(
                member.isfile() and is_ci_test_exempt is not None and is_ci_test_exempt(rel_posix)
            )

            for rule_name, pattern in FORBIDDEN_CONTENT_PATTERNS:
                if pattern.search(member.name):
                    violations.append(
                        f"forbidden proprietary member path ({rule_name}): {member.name}"
                    )

            if (member.issym() or member.islnk()) and member.linkname:
                link_target = member.linkname
                for rule_name, pattern in FORBIDDEN_CONTENT_PATTERNS:
                    if pattern.search(link_target):
                        violations.append(
                            f"forbidden proprietary link target ({rule_name}): "
                            f"{member.name} -> {link_target}"
                        )
                if link_target.endswith(".dalton.yaml") or link_target.endswith(
                    "everflow_defaults.yaml"
                ):
                    violations.append(
                        f"forbidden Everflow overlay link target: {member.name} -> {link_target}"
                    )

            if member.isfile() and not exempt_ci_content:
                suffix = PurePosixPath(member.name).suffix.lower()
                if suffix not in _BINARY_SUFFIXES:
                    extracted = tf.extractfile(member)
                    if extracted is not None:
                        raw_bytes = extracted.read()
                        if b"\x00" not in raw_bytes[:8192]:
                            text = raw_bytes.decode("utf-8", errors="replace")
                            for line_no, line in enumerate(text.splitlines(), 1):
                                for rule_name, pattern in FORBIDDEN_CONTENT_PATTERNS:
                                    if pattern.search(line):
                                        violations.append(
                                            f"forbidden proprietary content ({rule_name}): "
                                            f"{member.name}:{line_no}"
                                        )

            if expected_top_dir is not None:
                if raw_name.startswith("/") or ".." in parts:
                    violations.append(
                        f"archive member {member.name!r} uses unsafe path outside {expected_top_dir!r}"
                    )
                    continue
                if not parts:
                    violations.append(
                        f"archive member {member.name!r} sits outside top-level directory {expected_top_dir!r}"
                    )
                    continue

                # 1. Every member must live strictly under expected_top_dir (POSIX `/` rules).
                if parts[0] != expected_top_dir:
                    violations.append(
                        f"archive member {member.name!r} has top-level entry {parts[0]!r}, expected {expected_top_dir!r}"
                    )
                    continue

                # 2. Independently validate an explicit top-level entry (if present, it must be a directory)
                #    and confirm at least one actual file exists inside expected_top_dir.
                if len(parts) == 1:
                    if member.isdir():
                        recognized_top_dirs.add(parts[0])
                    else:
                        violations.append(
                            f"top-level entry {member.name!r} must be a directory (got non-directory member)"
                        )
                else:
                    recognized_top_dirs.add(parts[0])
                    if member.isfile():
                        nested_file_count += 1

    if member_count == 0:
        violations.append("archive is empty")
    elif expected_top_dir is not None:
        if expected_top_dir not in recognized_top_dirs:
            violations.append(
                f"expected top-level directory {expected_top_dir!r} is missing"
            )
        elif nested_file_count == 0:
            violations.append(
                f"top-level directory {expected_top_dir!r} contains no release files"
            )

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a Standalone Core release tarball is free of macOS metadata and xattrs."
    )
    parser.add_argument("tarball", help="Path to the release .tar.gz archive")
    parser.add_argument(
        "--expected-top-dir",
        default=None,
        help="Require every archive member to live under this single top-level directory",
    )
    parser.add_argument(
        "--include-ci",
        action="store_true",
        help="Exempt CI test-only paths from forbidden content pattern checks (CI test tarballs only)",
    )
    args = parser.parse_args(argv)

    violations = inspect_tarball(
        args.tarball,
        expected_top_dir=args.expected_top_dir,
        include_ci=args.include_ci,
    )
    if violations:
        print(
            f"ERROR: Release tarball gate failed for {args.tarball} ({len(violations)} violation(s)):",
            file=sys.stderr,
        )
        for item in violations[:40]:
            print(f"  - {item}", file=sys.stderr)
        if len(violations) > 40:
            print(f"  ... ({len(violations) - 40} more violation(s))", file=sys.stderr)
        return 1

    print(f"[verify_release_tarball] OK: {args.tarball} passed metadata/xattr gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
