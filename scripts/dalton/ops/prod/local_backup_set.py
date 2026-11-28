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
from contextlib import contextmanager
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.ops import backup_lib
from src.dalton.consistent_backup import (
    ArchivedObjectRecord,
    AsrArchiveSummary,
    BucketArchiveSpec,
    MirrorPoolSink,
    MissingReferencedObject,
    PgDumpProcess,
    build_backup_s3_client,
    resolve_runtime_bucket_specs,
    run_consistent_backup,
    start_pg_dump_process,
)
from src.dalton.storage_refs import ExpiredByRuleObject

logger = logging.getLogger(__name__)

DEFAULT_EXTERNAL_MOUNT_ROOT = Path("/Volumes/EveSSD")
DEFAULT_BACKUPS_ROOT = Path("/Volumes/EveSSD/Dalton/backups")

RETAIN_DAILY_DEFAULT = 14
RETAIN_WEEKLY_DEFAULT = 8
RETAIN_DEPLOY_DEFAULT = 5

POOL_LOCK_FILENAME = ".backup-pool.lock"
_HELD_POOL_LOCKS: dict[Path, tuple[int, Any]] = {}
_HELD_POOL_LOCKS_MU = threading.Lock()


class PoolPruneAbortedError(RuntimeError):
    """Raised when pruning aborts because retained set or manifest metadata is unreadable."""


@contextmanager
def backup_pool_lock(backups_root: Path) -> Iterator[Path]:
    """Coordinate mirror writes, manifest publication, and pool pruning via one exclusive file lock."""
    backups_root.mkdir(parents=True, exist_ok=True)
    lock_path = (backups_root / POOL_LOCK_FILENAME).resolve(strict=False)
    acquired_fh = None
    with _HELD_POOL_LOCKS_MU:
        existing = _HELD_POOL_LOCKS.get(lock_path)
        if existing is not None:
            depth, fh = existing
            _HELD_POOL_LOCKS[lock_path] = (depth + 1, fh)
        else:
            fh = lock_path.open("a+", encoding="utf-8")
            acquired_fh = fh
            _HELD_POOL_LOCKS[lock_path] = (1, fh)
    if acquired_fh is not None:
        try:
            fcntl.flock(acquired_fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            with _HELD_POOL_LOCKS_MU:
                _HELD_POOL_LOCKS.pop(lock_path, None)
            acquired_fh.close()
            raise
    try:
        yield lock_path
    finally:
        release_fh = None
        with _HELD_POOL_LOCKS_MU:
            cur = _HELD_POOL_LOCKS.get(lock_path)
            if cur is not None:
                depth, fh = cur
                if depth <= 1:
                    _HELD_POOL_LOCKS.pop(lock_path, None)
                    release_fh = fh
                else:
                    _HELD_POOL_LOCKS[lock_path] = (depth - 1, fh)
        if release_fh is not None:
            try:
                fcntl.flock(release_fh.fileno(), fcntl.LOCK_UN)
            finally:
                release_fh.close()


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
    manifest_sha256: str = ""
    missing_referenced_objects: tuple[MissingReferencedObject, ...] = ()
    expired_by_rule_objects: tuple[ExpiredByRuleObject, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
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
            "missing_referenced_objects": [m.to_dict() for m in self.missing_referenced_objects],
            "missing_referenced_count": len(self.missing_referenced_objects),
            "expired_by_rule_objects": [e.to_dict() for e in self.expired_by_rule_objects],
            "expired_by_rule_count": len(self.expired_by_rule_objects),
        }
        if self.manifest_sha256:
            out["manifest_sha256"] = self.manifest_sha256
        return out


def resolve_everflow_bucket_specs(
    env: Mapping[str, str] | None = None,
    *,
    client_factory: Callable[..., Any] = build_backup_s3_client,
) -> list[BucketArchiveSpec]:
    """Resolve active Everflow production buckets (main + aux/ASR) via shared runtime rules."""
    return resolve_runtime_bucket_specs(
        env=env,
        apply_core_defaults=False,
        rewrite_container_minio=True,
        client_factory=client_factory,
    )


def _sha256_and_size_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            h.update(chunk)
    return h.hexdigest(), total


def _sha256_file(path: Path) -> str:
    return _sha256_and_size_file(path)[0]


def verify_mirror_pool_objects(
    backups_root: Path,
    manifest_objects: Sequence[ArchivedObjectRecord],
) -> None:
    """Verify actual archived bytes (existence, size, SHA-256) on disk for every manifest entry."""
    pool_dir = backups_root / "minio-mirror" / "objects"
    verified_cache: dict[str, int] = {}
    for rec in manifest_objects:
        sha = str(rec.archived_sha256 or "").strip().lower()
        if len(sha) != 64:
            raise RuntimeError(
                f"Invalid archived_sha256 {rec.archived_sha256!r} for {rec.bucket}/{rec.key}"
            )
        if sha in verified_cache:
            if verified_cache[sha] != int(rec.size):
                raise RuntimeError(
                    f"Mirror pool size mismatch for {rec.bucket}/{rec.key} (sha={sha}): "
                    f"on-disk={verified_cache[sha]}, expected={rec.size}"
                )
            continue
        obj_path = pool_dir / sha[:2] / sha
        if obj_path.is_symlink() or not obj_path.is_file():
            raise RuntimeError(
                f"Mirror pool object missing for {rec.bucket}/{rec.key} (archived_sha256={sha}): {obj_path}"
            )
        actual_sha, actual_size = _sha256_and_size_file(obj_path)
        if actual_sha != sha:
            raise RuntimeError(
                f"Mirror pool object corrupted for {rec.bucket}/{rec.key}: "
                f"actual_sha256={actual_sha}, expected={sha}"
            )
        if actual_size != int(rec.size):
            raise RuntimeError(
                f"Mirror pool object size mismatch for {rec.bucket}/{rec.key}: "
                f"actual_size={actual_size}, expected={rec.size}"
            )
        verified_cache[sha] = actual_size


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _normalize_missing_referenced_objects(
    items: Sequence[MissingReferencedObject | Mapping[str, Any]] | None,
) -> tuple[MissingReferencedObject, ...]:
    if not items:
        return ()
    out: list[MissingReferencedObject] = []
    for item in items:
        if isinstance(item, MissingReferencedObject):
            out.append(item)
        elif isinstance(item, Mapping):
            out.append(MissingReferencedObject.from_dict(item))
    return tuple(out)


def _normalize_expired_by_rule_objects(
    items: Sequence[ExpiredByRuleObject | Mapping[str, Any]] | None,
) -> tuple[ExpiredByRuleObject, ...]:
    if not items:
        return ()
    out: list[ExpiredByRuleObject] = []
    for item in items:
        if isinstance(item, ExpiredByRuleObject):
            out.append(item)
        elif isinstance(item, Mapping):
            out.append(ExpiredByRuleObject.from_dict(item))
    return tuple(out)


def emit_missing_referenced_alert(
    code: str,
    message: str,
    *,
    env: Mapping[str, str] | None = None,
    append_alert_fn: Callable[..., tuple[Path, str | None]] = backup_lib.append_alert,
) -> Path:
    """Emit one ops alert line via ``backup_lib.append_alert`` for tolerated missing objects."""
    em = os.environ if env is None else env
    backup_alerts_file = (em.get("DALTON_BACKUP_ALERTS_FILE") or "").strip() or None
    if not backup_alerts_file:
        offsite_env_raw = (
            em.get("DALTON_OFFSITE_BACKUP_ENV_FILE")
            or str(Path.home() / ".config" / "dalton" / "offsite-backup.env")
        ).strip()
        if offsite_env_raw:
            from_file = backup_lib._read_env_var_from_file(
                Path(os.path.expanduser(offsite_env_raw)),
                "DALTON_BACKUP_ALERTS_FILE",
            )
            if from_file:
                backup_alerts_file = from_file
    ops_alerts_file = (em.get("DALTON_OPS_ALERTS_FILE") or "").strip() or None
    gw_env_raw = (
        em.get("DALTON_CHANNEL_GATEWAY_ENV_FILE")
        or str(Path.home() / ".config" / "dalton" / "channel-gateway.env")
    ).strip()
    gw_env_path = Path(os.path.expanduser(gw_env_raw)) if gw_env_raw else None
    target, warn_msg = append_alert_fn(
        level="WARN",
        source="local_backup_set",
        code=code,
        message=message,
        backup_alerts_file=backup_alerts_file,
        ops_alerts_file=ops_alerts_file,
        channel_gateway_env_file=gw_env_path,
    )
    if warn_msg:
        logger.warning("%s", warn_msg)
    return target


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
    missing_referenced_objects: Sequence[MissingReferencedObject | Mapping[str, Any]] = (),
    expired_by_rule_objects: Sequence[ExpiredByRuleObject | Mapping[str, Any]] = (),
    created_at: str | None = None,
) -> PublishedBackupSet:
    """Atomically promote a verified staged backup set on the same external volume (§3.1 step 4).

    Holds :func:`backup_pool_lock` and verifies actual on-disk bytes for every object in
    ``manifest_objects`` before promoting any artifact or updating ``latest-verified-set.json``.
    """
    if not reconcile_ok:
        raise RuntimeError("Refusing to publish backup set that failed reconciliation")

    seen_manifest_pairs: set[tuple[str, str]] = set()
    for rec in manifest_objects:
        pair = (str(rec.bucket), str(rec.key).lstrip("/"))
        if pair in seen_manifest_pairs:
            raise RuntimeError(
                f"Refusing to publish backup set with duplicate manifest object: {pair[0]}/{pair[1]}"
            )
        seen_manifest_pairs.add(pair)

    with backup_pool_lock(backups_root):
        verify_mirror_pool_objects(backups_root, manifest_objects)

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
        dump_sha, dump_size = _sha256_and_size_file(staged_dump_path)
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
        norm_missing = _normalize_missing_referenced_objects(missing_referenced_objects)
        norm_expired = _normalize_expired_by_rule_objects(expired_by_rule_objects)
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
                "missing_referenced_objects": [m.to_dict() for m in norm_missing],
                "missing_referenced_count": len(norm_missing),
                "expired_by_rule_objects": [e.to_dict() for e in norm_expired],
                "expired_by_rule_count": len(norm_expired),
            },
        )

        manifest_sha = _sha256_file(manifest_path)

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
            manifest_sha256=manifest_sha,
            missing_referenced_objects=norm_missing,
            expired_by_rule_objects=norm_expired,
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
    """Prune expired local backup sets and unreferenced ``minio-mirror/objects/`` files (§3.1 step 4).

    Coordinates via :func:`backup_pool_lock` and aborts without deleting anything if ANY
    backup-set file or retained manifest cannot be read/parsed cleanly.
    """
    sets_dir = backups_root / "sets"
    if not sets_dir.is_dir():
        return {"pruned_sets": 0, "pruned_pool_objects": 0, "surviving_sets": 0}

    with backup_pool_lock(backups_root):
        records: dict[str, dict[str, Any]] = {}
        for path in sorted(sets_dir.glob("backup-set-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise PoolPruneAbortedError(
                    f"Aborting prune: failed reading backup set metadata {path}: {exc}"
                ) from exc
            if not isinstance(data, dict) or not data.get("backup_set_id"):
                raise PoolPruneAbortedError(
                    f"Aborting prune: invalid backup set metadata in {path}"
                )
            records[str(data["backup_set_id"])] = data

        # Never prune the currently published latest-verified-set.json
        latest_id = ""
        latest_path = sets_dir / "latest-verified-set.json"
        if latest_path.is_file():
            try:
                latest_data = json.loads(latest_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise PoolPruneAbortedError(
                    f"Aborting prune: failed reading {latest_path}: {exc}"
                ) from exc
            if not isinstance(latest_data, dict) or not latest_data.get("backup_set_id"):
                raise PoolPruneAbortedError(
                    f"Aborting prune: invalid latest-verified-set metadata in {latest_path}"
                )
            latest_id = str(latest_data["backup_set_id"])

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

        # Collect referenced files & archived_sha256 union across ALL surviving sets;
        # abort immediately without deleting anything if ANY retained manifest cannot be read.
        keep_files: set[Path] = set()
        keep_shas: set[str] = set()
        for sid, rec in surviving.items():
            for key in ("dump_path", "asr_archive_path", "manifest_path", "set_path"):
                raw_p = rec.get(key)
                if raw_p:
                    keep_files.add(Path(str(raw_p)).resolve(strict=False))
            raw_mpath = str(rec.get("manifest_path") or "").strip()
            if not raw_mpath:
                raise PoolPruneAbortedError(
                    f"Aborting prune: surviving backup set {sid} has no manifest_path"
                )
            mpath = Path(raw_mpath)
            if not mpath.is_file():
                raise PoolPruneAbortedError(
                    f"Aborting prune: surviving backup set {sid} manifest missing: {mpath}"
                )
            try:
                mdata = json.loads(mpath.read_text(encoding="utf-8"))
            except Exception as exc:
                raise PoolPruneAbortedError(
                    f"Aborting prune: failed parsing surviving manifest {mpath}: {exc}"
                ) from exc
            if not isinstance(mdata, dict) or not isinstance(mdata.get("objects"), list):
                raise PoolPruneAbortedError(
                    f"Aborting prune: surviving manifest {mpath} missing valid 'objects' list"
                )
            for obj in mdata["objects"]:
                if not isinstance(obj, Mapping):
                    raise PoolPruneAbortedError(
                        f"Aborting prune: invalid object entry in surviving manifest {mpath}"
                    )
                sha = str(obj.get("archived_sha256") or "").strip().lower()
                if not sha:
                    raise PoolPruneAbortedError(
                        f"Aborting prune: object entry missing archived_sha256 in {mpath}"
                    )
                keep_shas.add(sha)

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
) -> PgDumpProcess:
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
    return start_pg_dump_process(
        cmd,
        dump_out_path,
        cwd=repo_root,
        cmd_label="docker compose pg_dump",
    )


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

        # Hold the shared backup pool lock across mirror writes, publication, and pruning
        # so concurrent daily/manual runs never prune freshly mirrored objects before publish.
        with backup_pool_lock(backups_root):
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
                        lock_phase_timeout_s=args.lock_timeout_s,
                        pg_dump_timeout_s=args.pg_dump_timeout_s,
                        mirror_timeout_s=args.mirror_timeout_s,
                        snapshot_import_timeout_s=args.snapshot_import_timeout_s,
                        alert_fn=emit_missing_referenced_alert,
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
                missing_referenced_objects=result.missing_referenced_objects,
                expired_by_rule_objects=result.expired_by_rule_objects,
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
    p_run.add_argument("--lock-timeout-s", type=float, default=None)
    p_run.add_argument("--pg-dump-timeout-s", type=float, default=None)
    p_run.add_argument("--mirror-timeout-s", type=float, default=None)
    p_run.add_argument("--snapshot-import-timeout-s", type=float, default=None)
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
    if not logging.getLogger().handlers:
        logging.basicConfig(
            stream=sys.stderr,
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        )
    args = build_arg_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
