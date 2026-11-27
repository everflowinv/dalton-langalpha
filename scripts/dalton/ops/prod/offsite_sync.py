#!/usr/bin/env python3
"""Everflow production offsite weekly baseline + daily delta helper (§3.1 step 4).

Lives under ``scripts/dalton/ops/prod/`` (Everflow-only). Reuses
``src.dalton.consistent_backup`` for version selection and ``scripts.dalton.ops.backup_lib``
for weekly date-directory pruning.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tarfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.ops import backup_lib
from src.dalton.consistent_backup import (
    ArchivedObjectRecord,
    BucketArchiveSpec,
    TarGzObjectSink,
    detect_bucket_versioning,
    list_all_current_objects,
    list_all_object_versions,
    select_bucket_object_versions,
)


def build_weekly_minio_baseline(
    *,
    bucket_specs: Sequence[BucketArchiveSpec],
    baseline_id: str,
    out_tar_path: Path,
    manifest_out_path: Path | None = None,
    tmp_dir: Path | None = None,
) -> tuple[tuple[ArchivedObjectRecord, ...], dict[str, Any]]:
    """Archive current normal object versions across active buckets into a weekly baseline tar.gz."""
    sink = TarGzObjectSink(out_tar_path, tmp_dir=tmp_dir)
    try:
        for spec in bucket_specs:
            ver_enabled = (
                spec.versioning_enabled
                if spec.versioning_enabled is not None
                else detect_bucket_versioning(spec.s3_client, spec.bucket)
            )
            if ver_enabled:
                vers, dms = list_all_object_versions(
                    spec.s3_client, spec.bucket, prefix=spec.key_prefix
                )
            else:
                vers = list_all_current_objects(
                    spec.s3_client, spec.bucket, prefix=spec.key_prefix
                )
                dms = []
            sel = select_bucket_object_versions(
                bucket=spec.bucket,
                versions=vers,
                delete_markers=dms,
                snapshot_referenced_keys=(),
                versioning_enabled=ver_enabled,
            )
            for item in sel.selected:
                sink.archive_object(spec.s3_client, item)
        records = sink.finalize()
    except Exception:
        sink.abort()
        raise

    manifest_payload: dict[str, Any] = {
        "version": 2,
        "baseline_id": baseline_id,
        "object_count": len(records),
        "objects": [r.to_dict() for r in records],
    }
    if manifest_out_path is not None:
        manifest_out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = manifest_out_path.with_name(f".{manifest_out_path.name}.tmp.{os.getpid()}")
        tmp.write_text(
            json.dumps(manifest_payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, manifest_out_path)
    return records, manifest_payload


def compute_daily_minio_delta(
    daily_manifest_objects: Sequence[Mapping[str, Any]],
    baseline_manifest_objects: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return deduplicated objects whose ``archived_sha256`` is absent from the weekly baseline."""
    baseline_shas = {
        str(obj.get("archived_sha256") or obj.get("sha256") or "").strip().lower()
        for obj in baseline_manifest_objects
        if isinstance(obj, Mapping)
    }
    baseline_shas.discard("")

    seen_delta_shas: set[str] = set()
    delta: list[dict[str, Any]] = []
    for obj in daily_manifest_objects:
        if not isinstance(obj, Mapping):
            continue
        sha = str(obj.get("archived_sha256") or obj.get("sha256") or "").strip().lower()
        if not sha or sha in baseline_shas or sha in seen_delta_shas:
            continue
        seen_delta_shas.add(sha)
        delta.append(dict(obj))
    delta.sort(key=lambda item: (str(item.get("archived_sha256") or ""), str(item.get("key") or "")))
    return delta


def build_daily_minio_delta_tar(
    delta_entries: Sequence[Mapping[str, Any]],
    *,
    mirror_objects_dir: Path,
    out_tar_path: Path,
) -> tuple[int, str, int]:
    """Pack delta objects (by ``archived_sha256``) from the local mirror pool into ``minio-delta-*.tar.gz``."""
    out_tar_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_tar = out_tar_path.with_name(f".{out_tar_path.name}.tmp.{os.getpid()}")
    packed: list[dict[str, Any]] = []
    try:
        with tarfile.open(tmp_tar, "w:gz") as tf:
            for entry in delta_entries:
                sha = str(entry.get("archived_sha256") or "").strip().lower()
                if not sha:
                    continue
                src = mirror_objects_dir / sha[:2] / sha
                if not src.is_file():
                    raise FileNotFoundError(
                        f"Mirror pool object missing for archived_sha256={sha}: {src}"
                    )
                data = src.read_bytes()
                actual_sha = hashlib.sha256(data).hexdigest()
                if actual_sha != sha:
                    raise RuntimeError(
                        f"Mirror pool object corrupted for {sha}: actual={actual_sha}"
                    )
                arcname = f"objects/{sha[:2]}/{sha}"
                info = tarfile.TarInfo(name=arcname)
                info.size = len(data)
                info.mtime = 0
                info.mode = 0o600
                tf.addfile(info, io.BytesIO(data))
                packed.append(
                    {
                        "archived_sha256": sha,
                        "size": len(data),
                        "bucket": str(entry.get("bucket") or ""),
                        "key": str(entry.get("key") or ""),
                    }
                )
            meta_bytes = (
                json.dumps(
                    {"version": 1, "object_count": len(packed), "objects": packed},
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
            minfo = tarfile.TarInfo(name="_delta_manifest.json")
            minfo.size = len(meta_bytes)
            minfo.mtime = 0
            minfo.mode = 0o600
            tf.addfile(minfo, io.BytesIO(meta_bytes))

        h = hashlib.sha256()
        total = 0
        with tmp_tar.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                h.update(chunk)
        os.replace(tmp_tar, out_tar_path)
        return total, h.hexdigest(), len(packed)
    finally:
        tmp_tar.unlink(missing_ok=True)


def select_weekly_baselines_to_prune(
    weekly_dirs: Iterable[str],
    *,
    retain_weeks: int,
    referenced_baseline_ids: Iterable[str] = (),
) -> list[str]:
    """Select expired ``weekly/<YYYY-MM-DD>`` dirs while exempting any ``baseline_id`` referenced by retained daily sets."""
    referenced = {
        str(bid).strip().rstrip("/")
        for bid in referenced_baseline_ids
        if str(bid).strip()
    }
    candidates = backup_lib.select_weekly_dirs_to_prune(weekly_dirs, retain_weeks)
    return [name for name in candidates if name.strip().rstrip("/") not in referenced]


def build_daily_latest_json(
    *,
    run_id: str,
    date_stamp: str,
    backup_set_id: str,
    baseline_id: str,
    artifacts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the authoritative ``db/<YYYY-MM-DD>/LATEST.json`` manifest for offsite restore."""
    return {
        "version": 2,
        "run_id": run_id,
        "date": date_stamp,
        "backup_set_id": backup_set_id,
        "baseline_id": baseline_id,
        "artifacts": {k: dict(v) for k, v in artifacts.items()},
    }


def find_latest_weekly_baseline_manifest(sets_dir: Path) -> tuple[str, list[dict[str, Any]]]:
    """Locate the newest local ``weekly-baseline-<YYYY-MM-DD>.json`` manifest, if any."""
    if not sets_dir.is_dir():
        return "", []
    candidates = sorted(sets_dir.glob("weekly-baseline-*.json"), reverse=True)
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("objects"), list):
            bid = str(data.get("baseline_id") or path.stem.removeprefix("weekly-baseline-"))
            return bid, data["objects"]
    return "", []


def _cmd_build_delta(args: argparse.Namespace) -> int:
    daily_manifest = json.loads(Path(args.daily_manifest).read_text(encoding="utf-8"))
    daily_objects = daily_manifest.get("objects") or []
    baseline_id = args.baseline_id or ""
    baseline_objects: list[dict[str, Any]] = []
    if args.baseline_manifest and Path(args.baseline_manifest).is_file():
        bdata = json.loads(Path(args.baseline_manifest).read_text(encoding="utf-8"))
        baseline_id = baseline_id or str(bdata.get("baseline_id") or "")
        baseline_objects = list(bdata.get("objects") or [])
    elif args.sets_dir:
        baseline_id, baseline_objects = find_latest_weekly_baseline_manifest(Path(args.sets_dir))

    delta = compute_daily_minio_delta(daily_objects, baseline_objects)
    size, sha, count = build_daily_minio_delta_tar(
        delta,
        mirror_objects_dir=Path(args.mirror_objects_dir),
        out_tar_path=Path(args.out_tar),
    )
    print(
        json.dumps(
            {
                "baseline_id": baseline_id,
                "delta_object_count": count,
                "tar_size": size,
                "tar_sha256": sha,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_select_weekly_prune(args: argparse.Namespace) -> int:
    dirs = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
    referenced: list[str] = list(args.referenced_baseline or ())
    if args.referenced_file and Path(args.referenced_file).is_file():
        referenced.extend(
            line.strip()
            for line in Path(args.referenced_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    for name in select_weekly_baselines_to_prune(
        dirs,
        retain_weeks=int(args.retain),
        referenced_baseline_ids=referenced,
    ):
        print(name)
    return 0


def _cmd_weekly_minio_baseline(args: argparse.Namespace) -> int:
    from scripts.dalton.ops.prod.local_backup_set import resolve_everflow_bucket_specs

    specs = resolve_everflow_bucket_specs(os.environ)
    records, payload = build_weekly_minio_baseline(
        bucket_specs=specs,
        baseline_id=args.baseline_id,
        out_tar_path=Path(args.out_tar),
        manifest_out_path=Path(args.manifest_out) if args.manifest_out else None,
        tmp_dir=Path(args.tmp_dir) if args.tmp_dir else None,
    )
    print(
        json.dumps(
            {
                "baseline_id": payload["baseline_id"],
                "object_count": len(records),
                "out_tar": args.out_tar,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_build_latest_json(args: argparse.Namespace) -> int:
    artifacts = json.loads(Path(args.artifacts_json).read_text(encoding="utf-8"))
    payload = build_daily_latest_json(
        run_id=args.run_id,
        date_stamp=args.date_stamp,
        backup_set_id=args.backup_set_id,
        baseline_id=args.baseline_id,
        artifacts=artifacts,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


def _cmd_read_verified_set(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.set_file).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("reconcile_ok"):
        print("latest-verified-set.json is not reconciled", file=sys.stderr)
        return 1
    print(str(data["backup_set_id"]))
    print(str(data["dump_path"]))
    print(str(data["manifest_path"]))
    print(str(data.get("asr_archive_path") or ""))
    return 0


def _cmd_read_json_field(args: argparse.Namespace) -> int:
    raw = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    data = json.loads(raw)
    if not isinstance(data, dict):
        return 1
    for field in args.field:
        val = data.get(field)
        print("" if val is None else str(val))
    return 0


def _cmd_write_artifacts_json(args: argparse.Namespace) -> int:
    def _meta(path_str: str) -> dict[str, Any]:
        return json.loads(Path(path_str).read_text(encoding="utf-8"))

    arts: dict[str, Any] = {
        "dump": {"filename": args.dump_name, **_meta(args.dump_meta)},
        "minio_manifest": {"filename": args.manifest_name, **_meta(args.manifest_meta)},
        "minio_delta": {"filename": args.delta_name, **_meta(args.delta_meta)},
    }
    if args.asr_name and args.asr_meta and Path(args.asr_meta).is_file():
        arts["asr_jobs"] = {"filename": args.asr_name, **_meta(args.asr_meta)}
    Path(args.out).write_text(json.dumps(arts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Everflow offsite weekly baseline & daily delta helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_delta = sub.add_parser("build-delta")
    p_delta.add_argument("--daily-manifest", required=True)
    p_delta.add_argument("--baseline-manifest", default="")
    p_delta.add_argument("--sets-dir", default="")
    p_delta.add_argument("--baseline-id", default="")
    p_delta.add_argument("--mirror-objects-dir", required=True)
    p_delta.add_argument("--out-tar", required=True)
    p_delta.set_defaults(func=_cmd_build_delta)

    p_prune = sub.add_parser("select-weekly-prune")
    p_prune.add_argument("--retain", type=int, required=True)
    p_prune.add_argument("--referenced-baseline", action="append", default=[])
    p_prune.add_argument("--referenced-file", default="")
    p_prune.set_defaults(func=_cmd_select_weekly_prune)

    p_wk = sub.add_parser("weekly-minio-baseline")
    p_wk.add_argument("--baseline-id", required=True)
    p_wk.add_argument("--out-tar", required=True)
    p_wk.add_argument("--manifest-out", default="")
    p_wk.add_argument("--tmp-dir", default="")
    p_wk.set_defaults(func=_cmd_weekly_minio_baseline)

    p_lat = sub.add_parser("build-latest-json")
    p_lat.add_argument("--run-id", required=True)
    p_lat.add_argument("--date-stamp", required=True)
    p_lat.add_argument("--backup-set-id", required=True)
    p_lat.add_argument("--baseline-id", default="")
    p_lat.add_argument("--artifacts-json", required=True)
    p_lat.add_argument("--out", required=True)
    p_lat.set_defaults(func=_cmd_build_latest_json)

    p_rvs = sub.add_parser("read-verified-set")
    p_rvs.add_argument("--set-file", required=True)
    p_rvs.set_defaults(func=_cmd_read_verified_set)

    p_rjf = sub.add_parser("read-json-field")
    p_rjf.add_argument("--file", default="")
    p_rjf.add_argument("--field", action="append", required=True)
    p_rjf.set_defaults(func=_cmd_read_json_field)

    p_wa = sub.add_parser("write-artifacts-json")
    p_wa.add_argument("--dump-name", required=True)
    p_wa.add_argument("--dump-meta", required=True)
    p_wa.add_argument("--manifest-name", required=True)
    p_wa.add_argument("--manifest-meta", required=True)
    p_wa.add_argument("--delta-name", required=True)
    p_wa.add_argument("--delta-meta", required=True)
    p_wa.add_argument("--asr-name", default="")
    p_wa.add_argument("--asr-meta", default="")
    p_wa.add_argument("--out", required=True)
    p_wa.set_defaults(func=_cmd_write_artifacts_json)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
