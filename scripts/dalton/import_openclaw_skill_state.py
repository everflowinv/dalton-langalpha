#!/usr/bin/env python3
"""Copy an OpenClaw skill state tree into a Dalton workspace.

Offline and repeatable. The default is a dry run; ``--apply`` writes.
Existing destination files are left unchanged, including when the bytes differ.
The import manifest is the only file this script overwrites. A second apply
rewrites that manifest with the same bytes when the tree is unchanged.

Recipient fields are removed from JSON. Absolute home paths and recipient
markers inside strings are rewritten. No credentials are copied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "openclaw-skill-state-import/1"
MANIFEST_NAME = "import-manifest.json"
RECIPIENT_KEYS = frozenset(
    {"chat_id", "open_id", "receive_id", "union_id", "email"}
)
CONDITIONAL_KEYS = frozenset({"to", "target", "recipient"})
_RECIPIENT_RES = (
    re.compile(r"feishu:chat:oc_[A-Za-z0-9_-]{3,128}"),
    re.compile(r"feishu:user:ou_[A-Za-z0-9_-]{3,128}"),
    re.compile(r"discord:user:\d+"),
    re.compile(r"oc_[A-Za-z0-9_-]{3,128}"),
)
_RECIPIENT_VALUE_RE = re.compile(
    r"(?:feishu:chat:oc_[A-Za-z0-9_-]{3,128}"
    r"|feishu:user:ou_[A-Za-z0-9_-]{3,128}"
    r"|discord:user:\d+"
    r"|oc_[A-Za-z0-9_-]{3,128})"
)
_SENSITIVE_RE = re.compile(r"(token|secret|credential)", re.IGNORECASE)
_RELATIVE_ANCHORS = ("research-outputs/", "state/", "temp/")


def import_openclaw_skill_state(
    source_dir: Path | str,
    workspace_dir: Path | str,
    dest: str,
    *,
    apply: bool = False,
    rename: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Plan, and optionally copy, ``source_dir`` into ``workspace / dest``.

    Returns the manifest and write counts. File bodies are not included.
    ``apply`` false leaves the workspace unchanged.
    """
    source = Path(source_dir)
    workspace = Path(workspace_dir)
    mapping = dict(rename or {})
    result: dict[str, Any] = {
        "ok": False,
        "apply": apply,
        "conflicts": [],
        "counts": {
            "copied": 0,
            "kept_existing": 0,
            "stored": 0,
            "skipped": 0,
        },
        "manifest": None,
        "written": [],
    }
    dest_rel = _relative_posix(dest)
    if dest_rel is None:
        _conflict(result, "dest", "path_escape")
        return result
    for old, new in mapping.items():
        if _relative_posix(old) is None or _relative_posix(new) is None:
            _conflict(result, "rename", "path_escape")
            return result
    if not _real_dir(source):
        reason = "symlink_escape" if source.is_symlink() else "missing_source"
        _conflict(result, "source", reason)
        return result
    if not _real_dir(workspace):
        reason = "symlink_escape" if workspace.is_symlink() else "missing_workspace"
        _conflict(result, "workspace", reason)
        return result
    if _overlaps(source, workspace):
        _conflict(result, "workspace", "overlapping_roots")
        return result

    planned = _plan_files(source, dest_rel, mapping, result)
    if result["conflicts"]:
        return result
    manifest = _manifest(dest_rel, planned, workspace, apply=False)
    result["manifest"] = manifest
    result["counts"] = _counts(manifest, planned)
    if not apply:
        result["ok"] = True
        return result

    written: list[str] = []
    for item in planned:
        payload = item["write_bytes"]
        if payload is None:
            continue
        rel = item["dest"]
        dest_path = workspace.joinpath(*Path(rel).parts)
        if dest_path.exists() or dest_path.is_symlink():
            continue
        try:
            _atomic_write(workspace, rel, payload)
        except (OSError, ValueError):
            _conflict(result, rel, "write_failed")
            result["written"] = written
            result["manifest"] = _manifest(dest_rel, planned, workspace, apply=True)
            return result
        written.append(rel)
        item["wrote_now"] = True
    manifest_rel = str(Path(dest_rel) / MANIFEST_NAME)
    try:
        final = _manifest(dest_rel, planned, workspace, apply=True)
        _atomic_write(
            workspace,
            manifest_rel,
            _dump(final).encode("utf-8"),
        )
    except (OSError, ValueError):
        _conflict(result, manifest_rel, "write_failed")
        result["written"] = written
        return result
    written.append(manifest_rel)
    result["written"] = written
    result["manifest"] = final
    result["counts"] = _counts(final, planned)
    result["ok"] = True
    return result


def _plan_files(
    source: Path,
    dest_rel: str,
    rename: dict[str, str],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    planned: list[dict[str, Any]] = []
    dests: dict[str, str] = {}
    for path in _iter_files(source):
        rel = path.relative_to(source).as_posix()
        action, payload = _classify(path, rel)
        target = rename.get(rel, rel)
        target_rel = str(Path(dest_rel) / target)
        if _relative_posix(target_rel) is None or not target_rel.startswith(dest_rel + "/"):
            _conflict(result, rel, "path_escape")
            continue
        if target_rel in dests:
            _conflict(result, target_rel, "duplicate_dest")
            continue
        dests[target_rel] = rel
        planned.append(
            {
                "source": rel,
                "dest": target_rel,
                "action_hint": action,
                "write_bytes": payload,
                "wrote_now": False,
            }
        )
    planned.sort(key=lambda item: item["source"])
    return planned


def _classify(path: Path, rel: str) -> tuple[str, bytes | None]:
    name = path.name
    if name == MANIFEST_NAME or rel == MANIFEST_NAME:
        return "skipped_manifest", None
    if name.startswith(".") or any(part.startswith(".") for part in Path(rel).parts):
        return "skipped_dotfile", None
    if "__pycache__" in Path(rel).parts or name.endswith(".pyc"):
        return "skipped_sensitive", None
    if name == ".env" or name.endswith(".pem") or _SENSITIVE_RE.search(name):
        return "skipped_sensitive", None
    if path.is_symlink():
        return "skipped_symlink", None
    try:
        raw = path.read_bytes()
    except OSError:
        return "skipped_binary", None
    transformed = _transform(raw, path.suffix.lower())
    if transformed is None:
        return "skipped_binary", None
    return "ready", transformed


def _transform(raw: bytes, suffix: str) -> bytes | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return None
    if suffix == ".json":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return (_redact_text(text).rstrip("\n") + "\n").encode("utf-8")
        return (_dump(_clean(parsed))).encode("utf-8")
    if suffix == ".jsonl":
        lines: list[str] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                lines.append(_redact_text(line))
            else:
                lines.append(json.dumps(_clean(parsed), ensure_ascii=False, sort_keys=True))
        body = "\n".join(lines)
        return (body + "\n").encode("utf-8") if body else b""
    return (_redact_text(text)).encode("utf-8")


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or key in RECIPIENT_KEYS:
                continue
            if key in CONDITIONAL_KEYS and isinstance(item, str) and _RECIPIENT_VALUE_RE.search(item):
                continue
            cleaned[key] = _clean(item)
        return cleaned
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


_LOCAL_PATH_RE = re.compile(r"(?:file://|/(?:Users|home|private)/)\S*")


def _redact_text(value: str) -> str:
    text = value
    for pattern in _RECIPIENT_RES:
        text = pattern.sub("[recipient]", text)

    def _replace_path(match: re.Match[str]) -> str:
        found = match.group(0)
        for anchor in _RELATIVE_ANCHORS:
            index = found.find(anchor)
            if index >= 0:
                return found[index:]
        return "[local-path]"

    text = _LOCAL_PATH_RE.sub(_replace_path, text)
    return text.replace(".openclaw", "[local-path]")


def _manifest(
    dest_rel: str,
    planned: list[dict[str, Any]],
    workspace: Path,
    *,
    apply: bool,
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for item in planned:
        entry: dict[str, Any] = {
            "source": item["source"],
            "dest": item["dest"],
        }
        payload = item["write_bytes"]
        if payload is None:
            entry["action"] = item["action_hint"]
            files.append(entry)
            continue
        entry["source_sha256"] = hashlib.sha256(payload).hexdigest()
        dest_path = workspace / item["dest"]
        if dest_path.is_symlink():
            entry["action"] = "skipped_symlink"
        elif dest_path.is_file():
            existing = dest_path.read_bytes()
            entry["sha256"] = hashlib.sha256(existing).hexdigest()
            entry["action"] = "stored" if existing == payload else "kept_existing"
        elif apply and item["wrote_now"]:
            entry["sha256"] = entry["source_sha256"]
            entry["action"] = "stored"
        else:
            # Dry run, or apply before the write: describe the bytes that would land.
            entry["sha256"] = entry["source_sha256"]
            entry["action"] = "stored" if not dest_path.exists() else "kept_existing"
        files.append(entry)
    files.sort(key=lambda row: (row["source"], row["dest"]))
    return {"schema": MANIFEST_SCHEMA, "dest": dest_rel, "files": files}


def _counts(manifest: dict[str, Any], planned: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"copied": 0, "kept_existing": 0, "stored": 0, "skipped": 0}
    wrote = {item["dest"] for item in planned if item["wrote_now"]}
    for row in manifest["files"]:
        action = row["action"]
        if action == "stored":
            counts["stored"] += 1
            if row["dest"] in wrote:
                counts["copied"] += 1
        elif action == "kept_existing":
            counts["kept_existing"] += 1
        else:
            counts["skipped"] += 1
    return counts


def _iter_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name != "__pycache__" and not name.startswith("."))
        current = Path(dirpath)
        for name in sorted(filenames):
            found.append(current / name)
    return found


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _atomic_write(workspace: Path, relative: str, payload: bytes) -> None:
    rel = _relative_posix(relative)
    if rel is None:
        raise ValueError("path escape")
    path = workspace.joinpath(*Path(rel).parts)
    if path.is_symlink():
        raise ValueError("symlink escape")
    parent = path.parent.resolve()
    root = workspace.resolve()
    if parent != root and root not in parent.parents:
        raise ValueError("path escape")
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes_exclusive(path, payload)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("incomplete write")
        view = view[written:]


def _write_bytes_exclusive(path: Path, payload: bytes) -> None:
    """Replace ``path`` via mkstemp so a pre-existing temp symlink is not followed."""
    if path.is_symlink():
        raise ValueError("symlink escape")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        try:
            _write_all(fd, payload)
        finally:
            os.close(fd)
        os.replace(name, path)
    except OSError:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _relative_posix(value: str) -> str | None:
    raw = Path(value)
    if raw.is_absolute() or not raw.parts or any(part in ("", ".", "..") for part in raw.parts):
        return None
    return raw.as_posix()


def _real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _overlaps(left: Path, right: Path) -> bool:
    left_root = left.resolve()
    right_root = right.resolve()
    return left_root == right_root or left_root in right_root.parents or right_root in left_root.parents


def _conflict(result: dict[str, Any], path: str, reason: str) -> None:
    result["conflicts"].append({"path": path, "reason": reason})
    result["ok"] = False


def _parse_rename(values: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise ValueError("rename must be OLD=NEW")
        old, new = item.split("=", 1)
        if not old or not new:
            raise ValueError("rename must be OLD=NEW")
        mapping[Path(old).as_posix()] = Path(new).as_posix()
    return mapping


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import OpenClaw skill state without overwriting")
    parser.add_argument("--source", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--dest", required=True, help="Workspace-relative destination directory")
    parser.add_argument("--rename", action="append", default=[], help="OLD=NEW relative path")
    parser.add_argument("--apply", action="store_true", help="Write files. Default is a dry run.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        rename = _parse_rename(args.rename)
    except ValueError as exc:
        payload = {"ok": False, "error": str(exc), "conflicts": [{"path": "rename", "reason": "path_escape"}]}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    result = import_openclaw_skill_state(
        args.source,
        args.workspace,
        args.dest,
        apply=args.apply,
        rename=rename,
    )
    public = {
        "ok": result["ok"],
        "apply": result["apply"],
        "conflicts": result["conflicts"],
        "counts": result["counts"],
        "manifest": result["manifest"],
        "written": result["written"],
    }
    print(json.dumps(public, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
