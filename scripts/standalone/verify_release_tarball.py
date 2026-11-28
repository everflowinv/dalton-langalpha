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

# Everflow/proprietary subpaths that must never ship inside a Standalone Core archive.
FORBIDDEN_RELEASE_SUBPATHS: tuple[tuple[str, ...], ...] = (
    ("plugins", "dalton_data"),
    ("plugins", "dalton", "skills", "dalton-richard-coverage-weekly"),
    ("plugins", "dalton", "skills", "dalton-ai-app-weekly-monitor"),
    ("scripts", "dalton", "researchdata_bridge.py"),
    ("scripts", "dalton", "research_runtime"),
    ("scripts", "dalton", "ops"),
    ("scripts", "dalton", "data_assets", "skills"),
    ("scripts", "dalton", "data_assets", "everflow_only.txt"),
    ("src", "dalton", "data_jobs", "everflow_defaults.yaml"),
    ("web", ".env.dalton"),
)

# Post-package content gate matching package_release.sh (zero proprietary leaks).
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
    return None


def _is_forbidden_pax_key(key: str) -> bool:
    return key in FORBIDDEN_PAX_KEYS or any(
        key.startswith(prefix) for prefix in FORBIDDEN_PAX_PREFIXES
    )


def _normalized_parts(member_name: str) -> tuple[str, ...]:
    """Split a tar member name strictly by POSIX `/` separators."""
    parts = PurePosixPath(member_name).parts
    return tuple(p for p in parts if p not in ("", ".", "/"))


def inspect_tarball(
    tar_path: Path | str | None = None,
    *,
    fileobj: BinaryIO | None = None,
    expected_top_dir: str | None = None,
) -> list[str]:
    """Inspect a tar archive (path or in-memory fileobj) and return policy violations."""
    if tar_path is None and fileobj is None:
        raise ValueError("inspect_tarball requires tar_path or fileobj")
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
            if matched_forbidden is not None:
                violations.append(
                    f"forbidden Everflow/proprietary member in core archive: {member.name} "
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

            if member.isfile():
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
    args = parser.parse_args(argv)

    violations = inspect_tarball(args.tarball, expected_top_dir=args.expected_top_dir)
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
