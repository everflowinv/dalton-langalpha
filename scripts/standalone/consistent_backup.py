#!/usr/bin/env python3
"""Standalone Core CLI wrapper for the shared consistent backup & post-restore protocol (§3.1, §6.1).

Thin entrypoint under ``scripts/standalone/``: all protocol logic lives once in
``src.dalton.consistent_backup`` and ``src.dalton.storage_refs`` (no duplicated
version selection, reconciliation, or inflight-state SQL).

Outputs the Standalone Core archive layout:
- ``postgres.dump``
- ``minio-objects.tar.gz`` (containing ``objects/<bucket>/<key>`` and ``_minio_manifest.json``)
- ``asr-jobs.tar.gz`` (terminal snapshot ASR jobs + ``asr_usage.jsonl``, when enabled)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import shutil
import sys
import tarfile
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.dalton.pg_config import PgConfigConflictError, resolve_pg_config, resolve_pg_dsn

if TYPE_CHECKING:
    from src.dalton.consistent_backup import BucketArchiveSpec, PgDumpProcess

RESTORE_MARKER_KEY = ".dalton-restore.json"
RESTORE_TAG_KEY = "dalton-restore"
CTL_PREFIX = "@@DALTON_CTL@@ "


def _norm_ep(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        has_scheme = "://" in raw
        parts = urlsplit(raw if has_scheme else f"//{raw}")
        if parts.hostname is not None:
            host = parts.hostname.lower()
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            port = parts.port
            netloc = f"{host}:{port}" if port is not None else host
            path = parts.path.rstrip("/") if not (parts.query or parts.fragment) else parts.path
            if has_scheme:
                return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, parts.fragment))
            return urlunsplit(("", netloc, path, parts.query, parts.fragment)).lstrip("/")
    except Exception:
        pass
    return raw


def is_restore_created_bucket(s3_client: Any, bucket_name: str) -> bool:
    """Identify restore-created buckets via bucket tag or .dalton-restore.json marker (never name suffix)."""
    from botocore.exceptions import ClientError

    try:
        tag_resp = s3_client.get_bucket_tagging(Bucket=bucket_name)
        for tag in tag_resp.get("TagSet") or []:
            if tag.get("Key") == RESTORE_TAG_KEY and str(tag.get("Value", "")).lower() == "true":
                return True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if code not in (
            "NoSuchTagSet",
            "NoSuchTagSetError",
            "404",
            "NotFound",
            "NotImplemented",
            "MethodNotAllowed",
        ) and status not in (404, 405, 501):
            raise

    try:
        s3_client.head_object(Bucket=bucket_name, Key=RESTORE_MARKER_KEY)
        return True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if code not in ("404", "NoSuchKey", "NotFound") and status != 404:
            raise
    return False


def resolve_standalone_pg_dsn(
    pg_dsn: str = "",
    env: Mapping[str, str] | None = None,
    *,
    default_host: str = "localhost",
    check_conflict: bool = False,
) -> str:
    """Resolve one unified PostgreSQL DSN shared with backend, backup, and restore.

    Delegates to :func:`src.dalton.pg_config.resolve_pg_dsn` (stdlib-only) with
    ``allow_backup_override=True`` so ``DB_*`` > ``DATABASE_URL`` priority matches
    ``src/server/database/pool.py`` and ``scripts/standalone/restore.sh`` identically.
    """
    return resolve_pg_dsn(
        pg_dsn,
        env=env,
        default_host=default_host,
        allow_backup_override=True,
        check_conflict=check_conflict,
    )


def resolve_standalone_pg_config(
    pg_dsn: str = "",
    env: Mapping[str, str] | None = None,
    *,
    default_host: str = "localhost",
    check_conflict: bool = False,
) -> dict[str, str]:
    """Resolve unified PostgreSQL connection fields from :mod:`src.dalton.pg_config`."""
    return resolve_pg_config(
        pg_dsn,
        env=env,
        default_host=default_host,
        allow_backup_override=True,
        check_conflict=check_conflict,
    )


async def open_standalone_session_connection(dsn: str) -> Any:
    """Open an autocommit control connection directly against ``dsn`` (no prepare cache)."""
    from psycopg import AsyncConnection

    conn = await AsyncConnection.connect(
        dsn,
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=60,
        keepalives_interval=10,
        keepalives_count=5,
    )
    conn.prepare_threshold = None
    await conn.set_autocommit(True)
    return conn


def resolve_standalone_bucket_specs(
    env: Mapping[str, str] | None = None,
    *,
    client_factory: Callable[..., Any] | None = None,
) -> list[BucketArchiveSpec]:
    """Resolve active Standalone Core buckets matching the running backend's effective config.

    Uses ``apply_core_defaults=False`` (matching ``src/utils/storage/__init__.py``) so
    backup never invents or probes ``dalton-storage`` when the running backend has no
    main storage bucket configured.
    """
    from src.dalton.consistent_backup import (
        build_backup_s3_client,
        resolve_runtime_bucket_specs,
    )

    factory = build_backup_s3_client if client_factory is None else client_factory
    return resolve_runtime_bucket_specs(
        env=env,
        apply_core_defaults=False,
        rewrite_container_minio=False,
        client_factory=factory,
    )


def _standalone_manifest_extra(
    specs: list[BucketArchiveSpec],
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build Standalone Core ``_minio_manifest.json`` target metadata compatible with ``restore.sh``."""
    em = os.environ if env is None else env
    buckets = sorted({s.bucket for s in specs})
    primary_bucket = ""
    primary_key_prefix = ""
    targets: dict[str, dict[str, str]] = {}
    var_buckets: dict[str, str] = {}

    for spec in specs:
        for role in spec.roles:
            if role == "chat_upload":
                primary_bucket = spec.bucket
                primary_key_prefix = spec.key_prefix
                ep = _norm_ep(
                    em.get("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL")
                    or em.get("DALTON_ASR_STORAGE_ENDPOINT_URL")
                    or "http://minio:9000"
                )
                targets["chat_upload"] = {
                    "endpoint": ep,
                    "bucket": spec.bucket,
                    "key_prefix": spec.key_prefix,
                }
            elif role == "asr":
                ep = _norm_ep(em.get("DALTON_ASR_STORAGE_ENDPOINT_URL") or "http://minio:9000")
                targets["asr"] = {
                    "endpoint": ep,
                    "bucket": spec.bucket,
                    "key_prefix": spec.key_prefix,
                }
            elif role == "main":
                ep = _norm_ep(
                    em.get("STORAGE_ENDPOINT_URL")
                    or em.get("S3_ENDPOINT_URL")
                    or ""
                )
                targets["general"] = {
                    "endpoint": ep,
                    "bucket": spec.bucket,
                    "key_prefix": spec.key_prefix,
                }
    if not primary_bucket and buckets:
        primary_bucket = buckets[0]
        primary_key_prefix = next((s.key_prefix for s in specs if s.bucket == primary_bucket), "")

    for var_name in (
        "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME",
        "DALTON_ASR_STORAGE_BUCKET_NAME",
        "STORAGE_BUCKET_NAME",
        "S3_BUCKET_NAME",
        "OSS_BUCKET_NAME",
        "R2_BUCKET_NAME",
    ):
        val = (em.get(var_name) or "").strip()
        if val and val in buckets:
            var_buckets[var_name] = val
    if "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME" not in var_buckets and primary_bucket:
        var_buckets["DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME"] = primary_bucket

    return {
        "buckets": buckets,
        "primary_bucket": primary_bucket or None,
        "primary_key_prefix": primary_key_prefix,
        "targets": targets,
        "var_buckets": var_buckets,
    }


def _start_pg_dump(snapshot_id: str, out_path: Path, *, dsn: str) -> PgDumpProcess:
    from src.dalton.consistent_backup import start_pg_dump_process

    cmd = ["pg_dump", dsn, "-Fc", "--no-owner", "--no-acl", f"--snapshot={snapshot_id}"]
    return start_pg_dump_process(cmd, out_path, cmd_label="pg_dump")


def _emit_standalone_missing_alert(code: str, message: str) -> None:
    """Append a single-line WARN alert when ``DALTON_BACKUP_ALERTS_FILE`` is configured."""
    alerts_raw = (os.getenv("DALTON_BACKUP_ALERTS_FILE") or "").strip()
    if not alerts_raw:
        return
    target = Path(os.path.expanduser(alerts_raw))
    if target.suffix.lower() == ".json":
        return
    ts = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    clean_msg = " ".join(message.splitlines()).strip()
    line = f"{ts} [WARN] [standalone_consistent_backup] [{code}] {clean_msg}\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def _cmd_backup(
    args: argparse.Namespace,
    *,
    conn_opener: Callable[[str], Any] = open_standalone_session_connection,
    pg_dump_starter: Callable[..., Any] = _start_pg_dump,
    bucket_resolver: Callable[..., list[BucketArchiveSpec]] = resolve_standalone_bucket_specs,
    alert_fn: Callable[[str, str], Any] | None = _emit_standalone_missing_alert,
) -> int:
    from src.dalton.consistent_backup import TarGzObjectSink, run_consistent_backup

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dump_path = out_dir / "postgres.dump"
    minio_tar = out_dir / "minio-objects.tar.gz"
    asr_dir = Path(args.asr_jobs_dir) if args.asr_jobs_dir else None
    asr_tar = (out_dir / "asr-jobs.tar.gz") if (asr_dir and asr_dir.is_dir()) else None
    dsn = resolve_standalone_pg_dsn(args.pg_dsn, os.environ, check_conflict=True)
    if not dsn:
        print("DATABASE_URL, --pg-dsn, or DB_* configuration is required", file=sys.stderr)
        return 2

    specs = bucket_resolver(os.environ)
    sink = TarGzObjectSink(
        minio_tar,
        tmp_dir=out_dir,
        manifest_extra=_standalone_manifest_extra(specs, os.environ),
    )

    async def _execute():
        conn = await conn_opener(dsn)
        try:
            return await run_consistent_backup(
                conn=conn,
                pg_dump_path=dump_path,
                pg_dump_fn=lambda snap_id, out_p: pg_dump_starter(snap_id, out_p, dsn=dsn),
                bucket_specs=specs,
                object_sink=sink,
                asr_jobs_dir=asr_dir if asr_tar is not None else None,
                asr_archive_path=asr_tar,
                lock_phase_timeout_s=args.lock_timeout_s,
                pg_dump_timeout_s=args.pg_dump_timeout_s,
                mirror_timeout_s=args.mirror_timeout_s,
                snapshot_import_timeout_s=args.snapshot_import_timeout_s,
                alert_fn=alert_fn,
            )
        finally:
            await conn.close()

    res = asyncio.run(_execute())
    print(
        json.dumps(
            {
                "snapshot_id": res.snapshot_id,
                "pg_dump_path": str(res.pg_dump_path),
                "minio_archive": str(minio_tar),
                "asr_archive": str(res.asr_archive.archive_path) if res.asr_archive else None,
                "object_count": len(res.manifest_objects),
                "missing_referenced_objects": [m.to_dict() for m in res.missing_referenced_objects],
                "missing_referenced_count": len(res.missing_referenced_objects),
                "warnings": list(res.warnings),
                "reconcile": res.reconcile_report.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _cmd_compose_backup(
    args: argparse.Namespace,
    *,
    conn_opener: Callable[[str], Any] = open_standalone_session_connection,
    bucket_resolver: Callable[..., list[BucketArchiveSpec]] = resolve_standalone_bucket_specs,
    terminate_pg_dump_hook: Callable[[str], None] | None = None,
    alert_fn: Callable[[str, str], Any] | None = _emit_standalone_missing_alert,
) -> int:
    """Coordinate consistent snapshot backup with host-driven ``docker compose exec postgres pg_dump``.

    Protocol over stderr (control lines prefixed with ``@@DALTON_CTL@@ ``) and stdin:
    1. Container acquires GC advisory locks, exports REPEATABLE READ snapshot, enumerates DB refs,
       and writes ``@@DALTON_CTL@@ SNAPSHOT_READY <snapshot_id>`` to stderr.
    2. Host starts ``pg_dump --snapshot=<snapshot_id>`` in postgres container, waits for dump output,
       and writes ``SNAPSHOT_IMPORTED`` to stdin -> container immediately COMMITs export txn.
    3. Container lists versions, selects objects, and waits for ``PG_DUMP_DONE`` on stdin.
       If the lock-phase watchdog expires (or any error occurs) while ``pg_dump`` is still active,
       the watchdog emits ``@@DALTON_CTL@@ TERMINATE_PG_DUMP <snapshot_id>`` so the host supervisor
       immediately kills the in-container ``pg_dump`` process.
    4. Container releases GC locks (before versioned mirroring), builds ``minio-objects.tar.gz``,
       runs full 17-consumer reconciliation, streams ``minio-objects.tar.gz`` to stdout (or ``--out-tar``),
       and emits ``@@DALTON_CTL@@ BACKUP_SUMMARY <json>`` to stderr.
    """
    from src.dalton.consistent_backup import (
        ConsistentBackupError,
        TarGzObjectSink,
        _run_in_daemon_thread,
        run_consistent_backup,
    )

    dsn = resolve_standalone_pg_dsn(args.pg_dsn, os.environ, check_conflict=True)
    specs = bucket_resolver(os.environ)
    stage_dir = Path(tempfile.mkdtemp(prefix="dalton-core-cb-"))
    stream_bundle = bool(getattr(args, "stream_bundle", False))
    tar_path = (
        Path(args.out_tar)
        if (args.out_tar and args.out_tar != "-" and not stream_bundle)
        else (stage_dir / "minio-objects.tar.gz")
    )
    asr_dir = Path(args.asr_jobs_dir) if args.asr_jobs_dir else None
    out_asr_tar_arg = str(getattr(args, "out_asr_tar", "") or "").strip()
    asr_tar = (
        Path(out_asr_tar_arg)
        if out_asr_tar_arg
        else ((stage_dir / "asr-jobs.tar.gz") if (asr_dir and asr_dir.is_dir()) else None)
    )

    def _emit_ctl(msg: str) -> None:
        sys.stderr.write(f"{CTL_PREFIX}{msg}\n")
        sys.stderr.flush()

    def _start_host_pg_dump(snap_id: str, _out_p: Path) -> str:
        _emit_ctl(f"SNAPSHOT_READY {snap_id}")
        return snap_id

    terminated_sent = False

    def _terminate_host_pg_dump(handle: Any) -> None:
        nonlocal terminated_sent
        if terminated_sent:
            return
        terminated_sent = True
        snap_id = str(handle or "")
        _emit_ctl(f"TERMINATE_PG_DUMP {snap_id}".strip())
        if terminate_pg_dump_hook is not None:
            terminate_pg_dump_hook(snap_id)

    async def _read_ctl_token(expected: str) -> None:
        line = await _run_in_daemon_thread(sys.stdin.readline)
        tok = (line or "").strip()
        if tok != expected:
            raise ConsistentBackupError(
                f"Host pg_dump handshake aborted while waiting for {expected!r} (got {tok!r})"
            )

    sink = TarGzObjectSink(
        tar_path,
        tmp_dir=stage_dir,
        manifest_extra=_standalone_manifest_extra(specs, os.environ),
    )

    try:
        async def _execute():
            conn = await conn_opener(dsn)
            try:
                return await run_consistent_backup(
                    conn=conn,
                    pg_dump_path=stage_dir / "host-managed-postgres.dump",
                    pg_dump_fn=_start_host_pg_dump,
                    wait_pg_dump_imported_fn=lambda _h: _read_ctl_token("SNAPSHOT_IMPORTED"),
                    wait_pg_dump_finished_fn=lambda _h: _read_ctl_token("PG_DUMP_DONE"),
                    terminate_pg_dump_fn=_terminate_host_pg_dump,
                    bucket_specs=specs,
                    object_sink=sink,
                    asr_jobs_dir=asr_dir if asr_tar is not None else None,
                    asr_archive_path=asr_tar,
                    lock_phase_timeout_s=args.lock_timeout_s,
                    pg_dump_timeout_s=args.pg_dump_timeout_s,
                    mirror_timeout_s=args.mirror_timeout_s,
                    snapshot_import_timeout_s=args.snapshot_import_timeout_s,
                    alert_fn=alert_fn,
                )
            finally:
                await conn.close()

        res = asyncio.run(_execute())
        if stream_bundle:
            with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as bundle_tf:
                bundle_tf.add(tar_path, arcname="minio-objects.tar.gz", recursive=False)
                if res.asr_archive is not None and res.asr_archive.archive_path.is_file():
                    bundle_tf.add(
                        res.asr_archive.archive_path,
                        arcname="asr-jobs.tar.gz",
                        recursive=False,
                    )
            sys.stdout.buffer.flush()
        elif not args.out_tar or args.out_tar == "-":
            with tar_path.open("rb") as fh:
                shutil.copyfileobj(fh, sys.stdout.buffer, length=1024 * 1024)
            sys.stdout.buffer.flush()

        summary_payload = {
            "snapshot_id": res.snapshot_id,
            "object_count": len(res.manifest_objects),
            "missing_referenced_objects": [m.to_dict() for m in res.missing_referenced_objects],
            "missing_referenced_count": len(res.missing_referenced_objects),
            "asr_job_count": len(res.asr_archive.included_job_ids) if res.asr_archive else 0,
            "asr_archive_included": bool(
                res.asr_archive is not None and res.asr_archive.archive_path.is_file()
            ),
            "warnings": list(res.warnings),
            "reconcile_ok": res.reconcile_report.ok,
            "checked_refs": res.reconcile_report.checked_refs,
        }
        _emit_ctl("BACKUP_SUMMARY " + json.dumps(summary_payload, ensure_ascii=False))
        return 0
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


def _cmd_resolve_pg_config(args: argparse.Namespace) -> int:
    from scripts.standalone import storage_config as sc

    merged_env: dict[str, str] = {}
    if args.env_file:
        merged_env.update(sc.parse_env_file(Path(args.env_file)))
    if args.compose_config_json:
        svc_env = sc.parse_compose_service_env(
            Path(args.compose_config_json),
            service_name=args.service or "backend",
        )
        merged_env.update(svc_env)
    use_env: Mapping[str, str] = (
        merged_env if (args.env_file or args.compose_config_json) else os.environ
    )
    try:
        cfg = resolve_standalone_pg_config(
            args.pg_dsn,
            use_env,
            default_host=getattr(args, "default_host", "localhost") or "localhost",
            check_conflict=bool(getattr(args, "check_conflict", False)),
        )
    except PgConfigConflictError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(cfg, ensure_ascii=False))
    return 0


def _cmd_reconcile_restored(args: argparse.Namespace) -> int:
    from src.dalton import consistent_backup

    return int(
        consistent_backup.main(
            ["reconcile-restored-inflight-states"]
            + (["--skip-object-check"] if args.skip_object_check else [])
            + (["--asr-jobs-dir", args.asr_jobs_dir] if args.asr_jobs_dir else [])
            + (["--restored-before", args.restored_before] if args.restored_before else [])
        )
    )


def _cmd_reconcile_refs(args: argparse.Namespace) -> int:
    from src.dalton import storage_refs

    return int(storage_refs.main(["reconcile", *args.extra]))


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Standalone Core consistent backup & post-restore reconciliation CLI"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_bak = sub.add_parser("backup")
    p_bak.add_argument("--output-dir", required=True)
    p_bak.add_argument("--pg-dsn", default="")
    p_bak.add_argument("--asr-jobs-dir", default="")
    p_bak.add_argument("--lock-timeout-s", type=float, default=None)
    p_bak.add_argument("--pg-dump-timeout-s", type=float, default=None)
    p_bak.add_argument("--mirror-timeout-s", type=float, default=None)
    p_bak.add_argument("--snapshot-import-timeout-s", type=float, default=None)
    p_bak.set_defaults(func=_cmd_backup)

    p_cb = sub.add_parser("compose-backup")
    p_cb.add_argument("--pg-dsn", default="")
    p_cb.add_argument("--out-tar", default="-")
    p_cb.add_argument("--out-asr-tar", default="")
    p_cb.add_argument(
        "--stream-bundle",
        action="store_true",
        help="Stream an uncompressed tar bundle (minio-objects.tar.gz + optional asr-jobs.tar.gz) to stdout.",
    )
    p_cb.add_argument("--asr-jobs-dir", default="")
    p_cb.add_argument("--lock-timeout-s", type=float, default=None)
    p_cb.add_argument("--pg-dump-timeout-s", type=float, default=None)
    p_cb.add_argument("--mirror-timeout-s", type=float, default=None)
    p_cb.add_argument("--snapshot-import-timeout-s", type=float, default=None)
    p_cb.set_defaults(func=_cmd_compose_backup)

    p_pg = sub.add_parser("resolve-pg-config")
    p_pg.add_argument("--env-file", default="")
    p_pg.add_argument("--compose-config-json", default="")
    p_pg.add_argument("--service", default="backend")
    p_pg.add_argument("--pg-dsn", default="")
    p_pg.add_argument("--default-host", default="localhost")
    p_pg.add_argument("--check-conflict", action="store_true")
    p_pg.set_defaults(func=_cmd_resolve_pg_config)

    p_rec = sub.add_parser("reconcile-restored-inflight-states")
    p_rec.add_argument("--skip-object-check", action="store_true")
    p_rec.add_argument("--asr-jobs-dir", default="")
    p_rec.add_argument("--restored-before", default="")
    p_rec.set_defaults(func=_cmd_reconcile_restored)

    p_ref = sub.add_parser("reconcile")
    p_ref.add_argument("extra", nargs=argparse.REMAINDER)
    p_ref.set_defaults(func=_cmd_reconcile_refs)
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
