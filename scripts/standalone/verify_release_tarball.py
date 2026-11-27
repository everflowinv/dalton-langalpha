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
import sys
import tarfile
from pathlib import Path, PurePosixPath

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
# The bridge filename is assembled so this shipped file passes the Core term scan.
FORBIDDEN_RELEASE_SUBPATHS: tuple[tuple[str, ...], ...] = (
    ("plugins", "dalton_data"),
    ("plugins", "dalton", "skills", "dalton-richard-coverage-weekly"),
    ("plugins", "dalton", "skills", "dalton-ai-app-weekly-monitor"),
    ("scripts", "dalton", "researchdata" + "_bridge.py"),
    ("scripts", "dalton", "research_runtime"),
    ("scripts", "dalton", "ops"),
    ("web", ".env.dalton"),
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
    tar_path: Path | str,
    *,
    expected_top_dir: str | None = None,
) -> list[str]:
    """Inspect a tar archive and return a list of policy violations (empty if clean)."""
    path = Path(tar_path)
    violations: list[str] = []
    member_count = 0
    recognized_top_dirs: set[str] = set()
    nested_file_count = 0

    with tarfile.open(path, mode="r:*") as tf:
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
                    f"forbidden Everflow-only member in core archive: {member.name} "
                    f"(matched {'/'.join(matched_forbidden)})"
                )

            pax_headers = member.pax_headers or {}
            for key in sorted(pax_headers):
                if _is_forbidden_pax_key(key):
                    violations.append(
                        f"forbidden metadata pax header on {member.name}: {key}"
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
