#!/usr/bin/env python3
"""Everflow production local consistent backup-set orchestrator (§3.1 step 4).

Lives under ``scripts/dalton/ops/prod/`` (Everflow-only). Delegates the core
"snapshot first, list versions second" backup protocol and full-consumer
reconciliation to shared ``src.dalton.consistent_backup`` and ``src.dalton.storage_refs``.

Directory layout under ``backups_root`` (default ``/Volumes/EveSSD/Dalton/backups``):
- ``pg/``: daily/weekly/deploy ``dalton-pg-<ts>.dump`` files
- ``frozen/``: ``manual-*`` tagged dumps & sets (never auto-pruned)
- ``minio-mirror/objects/<aa>/<sha256>``: deduplicated content-addressed object pool
- ``asr-jobs/asr-jobs-<backup_set_id>.tar.gz``: terminal-only ASR job archives
- ``sets/backup-set-<id>.json``, ``sets/minio-manifest-<id>.json``, ``sets/latest-verified-set.json``
- ``.tmp/``: same-volume staging directory so promotion is atomic ``os.replace``
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.ops import backup_lib
from src.dalton.consistent_backup import (
    ArchivedObjectRecord,
    AsrArchiveSummary,
    BucketArchiveSpec,
    MirrorPoolSink,
    run_consistent_backup,
)
from src.dalton.storage_config import resolve as resolve_storage_config

DEFAULT_EXTERNAL_MOUNT_ROOT = Path("/Volumes/EveSSD")
DEFAULT_BACKUPS_ROOT = Path("/Volumes/EveSSD/Dalton/backups")

RETAIN_DAILY_DEFAULT = 14
RETAIN_WEEKLY_DEFAULT = 8
RETAIN_DEPLOY_DEFAULT = 5


@dataclasses.dataclass(frozen=True)
class PublishedBackupSet:
    """Published Everflow local backup set metadata."""

    backup_set_id: str
    tag: str
    created_at: str
    snapshot_id: str
    dump_path: Path
    dump_size_bytes: int
    dump_sha256: str
    manifest_path: Path
    set_path: Path
    asr_archive_path: Path | None
    asr_archive_size_bytes: int | None
    asr_archive_sha256: str | None
    object_count: int
    reconcile_ok: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "backup_set_id": self.backup_set_id,
            "tag": self.tag,
            "created_at": self.created_at,
            "snapshot_id": self.snapshot_id,
            "dump_path": str(self.dump_path),
            "dump_size_bytes": self.dump_size_bytes,
            "dump_sha256": self.dump_sha256,
            "manifest_path": str(self.manifest_path),
            "set_path": str(self.set_path),
            "asr_archive_path": str(self.asr_archive_path) if self.asr_archive_path else None,
            "asr_archive_size_bytes": self.asr_archive_size_bytes,
            "asr_archive_sha256": self.asr_archive_sha256,
            "object_count": self.object_count,
            "reconcile_ok": self.reconcile_ok,
        }


def _rewrite_container_minio_endpoint(endpoint_url: str, env: Mapping[str, str]) -> str:
    """Map container-internal ``http://minio:9000`` to host loopback ``http://127.0.0.1:9000``."""
    raw = (endpoint_url or "").strip().rstrip("/")
    if not raw:
        return ""
    host_override = (env.get("DALTON_HOST_STORAGE_ENDPOINT_URL") or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if (parsed.hostname or "").lower() in ("minio", "minio-s3", "host.docker.internal"):
        if host_override:
            return host_override
        port = parsed.port or 9000
        return urlunsplit((parsed.scheme or "http", f"127.0.0.1:{port}", parsed.path, "", ""))
    return raw


def _build_s3_client(
    *,
    endpoint_url: str,
    access_key: str,
    secret_key: str,
    region: str = "us-east-1",
    addressing_style: str = "path",
) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": addressing_style},
            connect_timeout=10,
            read_timeout=120,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def resolve_everflow_bucket_specs(
    env: Mapping[str, str] | None = None,
    *,
    client_factory: Callable[..., Any] = _build_s3_client,
) -> list[BucketArchiveSpec]:
    """Resolve active Everflow production buckets (main + aux/ASR) for host backup scripts."""
    em = os.environ if env is None else env
    specs_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}

    cfg = resolve_storage_config(env=em)
    if cfg.is_storage_enabled and cfg.bucket_name:
        ep = _rewrite_container_minio_endpoint(cfg.endpoint_url or "http://127.0.0.1:9000", em)
        ak = (
            em.get("STORAGE_ACCESS_KEY_ID")
            or em.get("S3_ACCESS_KEY_ID")
            or em.get("MINIO_ROOT_USER")
            or ""
        ).strip()
        sk = (
            em.get("STORAGE_SECRET_ACCESS_KEY")
            or em.get("S3_SECRET_ACCESS_KEY")
            or em.get("MINIO_ROOT_PASSWORD")
            or ""
        ).strip()
        region = (em.get("STORAGE_REGION") or em.get("S3_REGION") or "us-east-1").strip()
        style = (em.get("STORAGE_ADDRESSING_STYLE") or "path").strip().lower()
        prefix = (em.get("STORAGE_KEY_PREFIX") or "").strip().strip("/")
        if ep and ak and sk:
            key = (ep, cfg.bucket_name, prefix)
            specs_by_key[key] = {
                "endpoint": ep,
                "bucket": cfg.bucket_name,
                "prefix": prefix,
                "roles": ["main"],
                "ak": ak,
                "sk": sk,
                "region": region,
                "style": style,
            }

    asr_bucket = (em.get("DALTON_ASR_STORAGE_BUCKET_NAME") or "").strip()
    if asr_bucket:
        asr_ep = _rewrite_container_minio_endpoint(
            em.get("DALTON_ASR_STORAGE_ENDPOINT_URL") or "http://127.0.0.1:9000", em
        )
        asr_ak = (
            em.get("DALTON_ASR_STORAGE_ACCESS_KEY_ID") or em.get("MINIO_ROOT_USER") or ""
        ).strip()
        asr_sk = (
            em.get("DALTON_ASR_STORAGE_SECRET_ACCESS_KEY") or em.get("MINIO_ROOT_PASSWORD") or ""
        ).strip()
        asr_region = (em.get("DALTON_ASR_STORAGE_REGION") or "us-east-1").strip()
        asr_style = (em.get("DALTON_ASR_STORAGE_ADDRESSING_STYLE") or "path").strip().lower()
        asr_prefix = (em.get("DALTON_ASR_STORAGE_KEY_PREFIX") or "").strip().strip("/")
        if asr_ep and asr_ak and asr_sk:
            key = (asr_ep, asr_bucket, asr_prefix)
            if key in specs_by_key:
                specs_by_key[key]["roles"].append("asr")
            else:
                specs_by_key[key] = {
                    "endpoint": asr_ep,
                    "bucket": asr_bucket,
                    "prefix": asr_prefix,
                    "roles": ["asr"],
                    "ak": asr_ak,
                    "sk": asr_sk,
                    "region": asr_region,
                    "style": asr_style,
                }

    chat_bucket = (em.get("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME") or "").strip()
    if chat_bucket:
        chat_ep = _rewrite_container_minio_endpoint(
            em.get("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL") or "http://127.0.0.1:9000", em
        )
        chat_ak = (
            em.get("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID") or em.get("MINIO_ROOT_USER") or ""
        ).strip()
        chat_sk = (
            em.get("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY")
            or em.get("MINIO_ROOT_PASSWORD")
            or ""
        ).strip()
        chat_region = (em.get("DALTON_CHAT_UPLOAD_STORAGE_REGION") or "us-east-1").strip()
        chat_style = (
            em.get("DALTON_CHAT_UPLOAD_STORAGE_ADDRESSING_STYLE") or "path"
        ).strip().lower()
        chat_prefix = (em.get("DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX") or "").strip().strip("/")
        if chat_ep and chat_ak and chat_sk:
            key = (chat_ep, chat_bucket, chat_prefix)
            if key in specs_by_key:
                specs_by_key[key]["roles"].append("chat_upload")
            else:
                specs_by_key[key] = {
                    "endpoint": chat_ep,
                    "bucket": chat_bucket,
                    "prefix": chat_prefix,
                    "roles": ["chat_upload"],
                    "ak": chat_ak,
                    "sk": chat_sk,
                    "region": chat_region,
                    "style": chat_style,
                }
    elif asr_bucket:
        # Everflow fallback: chat uploads & channel snapshots share the ASR bucket
        for item in specs_by_key.values():
            if "asr" in item["roles"] and "chat_upload" not in item["roles"]:
                item["roles"].append("chat_upload")

    out: list[BucketArchiveSpec] = []
    for item in specs_by_key.values():
        client = client_factory(
            endpoint_url=item["endpoint"],
            access_key=item["ak"],
            secret_key=item["sk"],
            region=item["region"],
            addressing_style=item["style"],
        )
        out.append(
            BucketArchiveSpec(
                bucket=item["bucket"],
                roles=tuple(dict.fromkeys(item["roles"])),
                key_prefix=item["prefix"],
                s3_client=client,
            )
        )
    return out


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def publish_local_backup_set(
    *,
    backups_root: Path,
    backup_set_id: str,
    tag: str,
    snapshot_id: str,
    staged_dump_path: Path,
    manifest_objects: Sequence[ArchivedObjectRecord],
    asr_summary: AsrArchiveSummary | None,
    reconcile_ok: bool,
    created_at: str | None = None,
) -> PublishedBackupSet:
    """Atomically promote a verified staged backup set on the same external volume (§3.1 step 4)."""
    if not reconcile_ok:
        raise RuntimeError("Refusing to publish backup set that failed reconciliation")

    is_manual = tag.startswith("manual-")
    dump_dir = backups_root / ("frozen" if is_manual else "pg")
    asr_dir = backups_root / ("frozen" if is_manual else "asr-jobs")
    sets_dir = backups_root / "sets"
    for d in (dump_dir, asr_dir, sets_dir):
        d.mkdir(parents=True, exist_ok=True)

    dump_name = (
        f"dalton-pg-{tag}-{backup_set_id}.dump"
        if is_manual
        else f"dalton-pg-{backup_set_id}.dump"
    )
    final_dump = dump_dir / dump_name
    dump_sha = _sha256_file(staged_dump_path)
    dump_size = staged_dump_path.stat().st_size
    os.replace(staged_dump_path, final_dump)

    final_asr: Path | None = None
    asr_size: int | None = None
    asr_sha: str | None = None
    if asr_summary is not None and asr_summary.archive_path.is_file():
        asr_name = (
            f"asr-jobs-{tag}-{backup_set_id}.tar.gz"
            if is_manual
            else f"asr-jobs-{backup_set_id}.tar.gz"
        )
        final_asr = asr_dir / asr_name
        os.replace(asr_summary.archive_path, final_asr)
        asr_size = asr_summary.size_bytes
        asr_sha = asr_summary.sha256

    ts_iso = created_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    manifest_path = sets_dir / f"minio-manifest-{backup_set_id}.json"
    _atomic_write_json(
        manifest_path,
        {
            "version": 2,
            "backup_set_id": backup_set_id,
            "tag": tag,
            "created_at": ts_iso,
            "snapshot_id": snapshot_id,
            "objects": [r.to_dict() for r in manifest_objects],
        },
    )

    set_path = sets_dir / f"backup-set-{backup_set_id}.json"
    published = PublishedBackupSet(
        backup_set_id=backup_set_id,
        tag=tag,
        created_at=ts_iso,
        snapshot_id=snapshot_id,
        dump_path=final_dump,
        dump_size_bytes=dump_size,
        dump_sha256=dump_sha,
        manifest_path=manifest_path,
        set_path=set_path,
        asr_archive_path=final_asr,
        asr_archive_size_bytes=asr_size,
        asr_archive_sha256=asr_sha,
        object_count=len(manifest_objects),
        reconcile_ok=True,
    )
    _atomic_write_json(set_path, published.to_dict())
    _atomic_write_json(sets_dir / "latest-verified-set.json", published.to_dict())
    return published


def select_sets_to_prune(
    set_records: Sequence[Mapping[str, Any]],
    *,
    retain_daily: int = RETAIN_DAILY_DEFAULT,
    retain_weekly: int = RETAIN_WEEKLY_DEFAULT,
    retain_deploy: int = RETAIN_DEPLOY_DEFAULT,
) -> list[str]:
    """Select expired ``backup_set_id``s under tiered retention (daily 14, weekly 8, deploy 5; manual-* kept)."""
    by_tier: dict[str, list[tuple[str, str]]] = {"daily": [], "weekly": [], "deploy": []}
    for rec in set_records:
        sid = str(rec.get("backup_set_id") or "").strip()
        tag = str(rec.get("tag") or "daily").strip()
        created = str(rec.get("created_at") or sid)
        if not sid or tag.startswith("manual-"):
            continue
        if tag in by_tier:
            by_tier[tag].append((created, sid))
        else:
            by_tier["daily"].append((created, sid))

    limits = {
        "daily": max(0, int(retain_daily)),
        "weekly": max(0, int(retain_weekly)),
        "deploy": max(0, int(retain_deploy)),
    }
    to_prune: list[str] = []
    for tier, items in by_tier.items():
        items.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
        for _created, sid in items[limits[tier] :]:
            to_prune.append(sid)
    return sorted(set(to_prune))


def prune_local_backup_sets_and_pool(
    backups_root: Path,
    *,
    retain_daily: int = RETAIN_DAILY_DEFAULT,
    retain_weekly: int = RETAIN_WEEKLY_DEFAULT,
    retain_deploy: int = RETAIN_DEPLOY_DEFAULT,
) -> dict[str, int]:
    """Prune expired local backup sets and unreferenced ``minio-mirror/objects/`` files (§3.1 step 4)."""
    sets_dir = backups_root / "sets"
    if not sets_dir.is_dir():
        return {"pruned_sets": 0, "pruned_pool_objects": 0, "surviving_sets": 0}

    records: dict[str, dict[str, Any]] = {}
    for path in sorted(sets_dir.glob("backup-set-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and data.get("backup_set_id"):
            records[str(data["backup_set_id"])] = data

    # Never prune the currently published latest-verified-set.json
    latest_id = ""
    latest_path = sets_dir / "latest-verified-set.json"
    if latest_path.is_file():
        try:
            latest_data = json.loads(latest_path.read_text(encoding="utf-8"))
            latest_id = str(latest_data.get("backup_set_id") or "")
        except Exception:
            pass

    prune_ids = [
        sid
        for sid in select_sets_to_prune(
            list(records.values()),
            retain_daily=retain_daily,
            retain_weekly=retain_weekly,
            retain_deploy=retain_deploy,
        )
        if sid != latest_id
    ]
    prune_id_set = set(prune_ids)
    surviving = {sid: rec for sid, rec in records.items() if sid not in prune_id_set}

    # Collect referenced files & archived_sha256 union across surviving sets
    keep_files: set[Path] = set()
    keep_shas: set[str] = set()
    for rec in surviving.values():
        for key in ("dump_path", "asr_archive_path", "manifest_path", "set_path"):
            raw_p = rec.get(key)
            if raw_p:
                keep_files.add(Path(str(raw_p)).resolve(strict=False))
        mpath = Path(str(rec.get("manifest_path") or ""))
        if mpath.is_file():
            try:
                mdata = json.loads(mpath.read_text(encoding="utf-8"))
                for obj in mdata.get("objects") or []:
                    sha = str(obj.get("archived_sha256") or "").strip().lower()
                    if sha:
                        keep_shas.add(sha)
            except Exception:
                pass

    pruned_sets = 0
    for sid in prune_ids:
        rec = records[sid]
        for key in ("dump_path", "asr_archive_path", "manifest_path", "set_path"):
            raw_p = rec.get(key)
            if not raw_p:
                continue
            p = Path(str(raw_p))
            if p.resolve(strict=False) in keep_files:
                continue
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        pruned_sets += 1

    # Prune unreferenced mirror pool objects
    pruned_pool = 0
    pool_objects_dir = backups_root / "minio-mirror" / "objects"
    if pool_objects_dir.is_dir():
        for sub in sorted(pool_objects_dir.iterdir()):
            if not sub.is_dir() or sub.is_symlink():
                continue
            for obj_file in sorted(sub.iterdir()):
                if not obj_file.is_file() or obj_file.is_symlink():
                    continue
                if obj_file.name.strip().lower() not in keep_shas:
                    try:
                        obj_file.unlink()
                        pruned_pool += 1
                    except OSError:
                        pass

    return {
        "pruned_sets": pruned_sets,
        "pruned_pool_objects": pruned_pool,
        "surviving_sets": len(surviving),
    }


def _docker_pg_dump_fn(
    snapshot_id: str,
    dump_out_path: Path,
    *,
    repo_root: Path,
    compose_files: Sequence[str],
) -> None:
    cmd = ["docker", "compose"]
    for cf in compose_files:
        cmd.extend(["-f", cf])
    cmd.extend(
        [
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            f'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --snapshot="{snapshot_id}"',
        ]
    )
    dump_out_path.parent.mkdir(parents=True, exist_ok=True)
    with dump_out_path.open("wb") as out_fh:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            stdout=out_fh,
            stderr=subprocess.PIPE,
            check=False,
        )
    if proc.returncode != 0 or dump_out_path.stat().st_size == 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"pg_dump --snapshot failed (rc={proc.returncode}): {err}")


def _cmd_run(args: argparse.Namespace) -> int:
    mount_root = Path(args.mount_root)
    backups_root = Path(args.backups_root)
    if not args.skip_mount_check:
        backup_lib.verify_backup_mount_layout(mount_root, backups_root)

    for sub in backup_lib.STANDARD_BACKUP_SUBDIRS:
        (backups_root / sub).mkdir(parents=True, exist_ok=True)

    backup_set_id = args.set_id or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    stage_dir = Path(tempfile.mkdtemp(prefix=f"set-{backup_set_id}-", dir=str(backups_root / ".tmp")))
    try:
        from src.server.database.pool import open_session_connection

        bucket_specs = resolve_everflow_bucket_specs(os.environ)
        sink = MirrorPoolSink(backups_root / "minio-mirror")
        asr_jobs_dir = Path(args.asr_jobs_dir) if args.asr_jobs_dir else None
        asr_stage = (
            stage_dir / f"asr-jobs-{backup_set_id}.tar.gz"
            if asr_jobs_dir is not None and asr_jobs_dir.is_dir()
            else None
        )
        dump_stage = stage_dir / f"dalton-pg-{backup_set_id}.dump"
        compose_files = [c for c in (args.compose_files or "").split(":") if c]

        async def _execute():
            conn = await open_session_connection()
            try:
                return await run_consistent_backup(
                    conn=conn,
                    pg_dump_path=dump_stage,
                    pg_dump_fn=lambda snap_id, out_p: _docker_pg_dump_fn(
                        snap_id, out_p, repo_root=REPO_ROOT, compose_files=compose_files
                    ),
                    bucket_specs=bucket_specs,
                    object_sink=sink,
                    asr_jobs_dir=asr_jobs_dir if asr_stage is not None else None,
                    asr_archive_path=asr_stage,
                )
            finally:
                await conn.close()

        result = asyncio.run(_execute())
        published = publish_local_backup_set(
            backups_root=backups_root,
            backup_set_id=backup_set_id,
            tag=args.tag,
            snapshot_id=result.snapshot_id,
            staged_dump_path=dump_stage,
            manifest_objects=result.manifest_objects,
            asr_summary=result.asr_archive,
            reconcile_ok=result.reconcile_report.ok,
        )
        prune_stats = prune_local_backup_sets_and_pool(
            backups_root,
            retain_daily=int(args.retain_daily),
            retain_weekly=int(args.retain_weekly),
            retain_deploy=int(args.retain_deploy),
        )
        out = published.to_dict()
        out["prune"] = prune_stats
        print(json.dumps(out, ensure_ascii=False))
        return 0
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


def _cmd_prune(args: argparse.Namespace) -> int:
    stats = prune_local_backup_sets_and_pool(
        Path(args.backups_root),
        retain_daily=int(args.retain_daily),
        retain_weekly=int(args.retain_weekly),
        retain_deploy=int(args.retain_deploy),
    )
    print(json.dumps(stats, ensure_ascii=False))
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Everflow local consistent backup-set orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run")
    p_run.add_argument("--mount-root", default=str(DEFAULT_EXTERNAL_MOUNT_ROOT))
    p_run.add_argument("--backups-root", default=str(DEFAULT_BACKUPS_ROOT))
    p_run.add_argument("--tag", default="daily")
    p_run.add_argument("--set-id", default="")
    p_run.add_argument("--asr-jobs-dir", default="")
    p_run.add_argument("--compose-files", default="docker-compose.yml:docker-compose.dalton.yml")
    p_run.add_argument("--retain-daily", type=int, default=RETAIN_DAILY_DEFAULT)
    p_run.add_argument("--retain-weekly", type=int, default=RETAIN_WEEKLY_DEFAULT)
    p_run.add_argument("--retain-deploy", type=int, default=RETAIN_DEPLOY_DEFAULT)
    p_run.add_argument("--skip-mount-check", action="store_true")
    p_run.set_defaults(func=_cmd_run)

    p_prune = sub.add_parser("prune")
    p_prune.add_argument("--backups-root", default=str(DEFAULT_BACKUPS_ROOT))
    p_prune.add_argument("--retain-daily", type=int, default=RETAIN_DAILY_DEFAULT)
    p_prune.add_argument("--retain-weekly", type=int, default=RETAIN_WEEKLY_DEFAULT)
    p_prune.add_argument("--retain-deploy", type=int, default=RETAIN_DEPLOY_DEFAULT)
    p_prune.set_defaults(func=_cmd_prune)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
