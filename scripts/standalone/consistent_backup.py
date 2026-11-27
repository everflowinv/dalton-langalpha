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
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.dalton import consistent_backup, storage_refs
from src.dalton.consistent_backup import (
    BucketArchiveSpec,
    ConsistentBackupError,
    PgDumpProcess,
    TarGzObjectSink,
    build_backup_s3_client,
    resolve_runtime_bucket_specs,
    run_consistent_backup,
    start_pg_dump_process,
)

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
) -> str:
    """Resolve one unified PostgreSQL DSN for BOTH control session locks and ``pg_dump``.

    Priority:
    1. Explicit ``--pg-dsn``
    2. ``DATABASE_URL`` environment variable
    3. ``DB_HOST`` / ``DB_PORT`` / ``DB_NAME`` / ``DB_USER`` / ``DB_PASSWORD`` / ``DB_SSLMODE``
    """
    explicit = (pg_dsn or "").strip()
    if explicit:
        return explicit
    em = os.environ if env is None else env
    env_url = (em.get("DATABASE_URL") or "").strip()
    if env_url:
        return env_url
    db_host = (em.get("DB_HOST") or "localhost").strip()
    db_port = (em.get("DB_PORT") or "5432").strip()
    db_name = (em.get("DB_NAME") or "postgres").strip()
    db_user = (em.get("DB_USER") or "postgres").strip()
    db_password = em.get("DB_PASSWORD") or "postgres"
    db_sslmode = (em.get("DB_SSLMODE") or "prefer").strip() or "prefer"
    return (
        f"postgresql://{quote_plus(db_user)}:{quote_plus(db_password)}"
        f"@{db_host}:{db_port}/{db_name}?sslmode={quote_plus(db_sslmode)}"
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
    client_factory: Callable[..., Any] = build_backup_s3_client,
) -> list[BucketArchiveSpec]:
    """Resolve active Standalone Core buckets via shared runtime rules (supporting ``AWS_*``)."""
    return resolve_runtime_bucket_specs(
        env=env,
        apply_core_defaults=True,
        rewrite_container_minio=False,
        client_factory=client_factory,
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
    cmd = ["pg_dump", dsn, "-Fc", "--no-owner", "--no-acl", f"--snapshot={snapshot_id}"]
    return start_pg_dump_process(cmd, out_path, cmd_label="pg_dump")


def _cmd_backup(
    args: argparse.Namespace,
    *,
    conn_opener: Callable[[str], Any] = open_standalone_session_connection,
    pg_dump_starter: Callable[..., Any] = _start_pg_dump,
    bucket_resolver: Callable[..., list[BucketArchiveSpec]] = resolve_standalone_bucket_specs,
) -> int:
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dump_path = out_dir / "postgres.dump"
    minio_tar = out_dir / "minio-objects.tar.gz"
    asr_dir = Path(args.asr_jobs_dir) if args.asr_jobs_dir else None
    asr_tar = (out_dir / "asr-jobs.tar.gz") if (asr_dir and asr_dir.is_dir()) else None
    dsn = resolve_standalone_pg_dsn(args.pg_dsn, os.environ)
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
) -> int:
    """Coordinate consistent snapshot backup with host-driven ``docker compose exec postgres pg_dump``.

    Protocol over stderr (control lines prefixed with ``@@DALTON_CTL@@ ``) and stdin:
    1. Container acquires GC advisory locks, exports REPEATABLE READ snapshot, enumerates DB refs,
       and writes ``@@DALTON_CTL@@ SNAPSHOT_READY <snapshot_id>`` to stderr.
    2. Host starts ``pg_dump --snapshot=<snapshot_id>`` in postgres container, waits for dump output,
       and writes ``SNAPSHOT_IMPORTED`` to stdin -> container immediately COMMITs export txn.
    3. Container lists versions, selects objects, and waits for ``PG_DUMP_DONE`` on stdin.
    4. Container releases GC locks (before versioned mirroring), builds ``minio-objects.tar.gz``,
       runs full 17-consumer reconciliation, streams ``minio-objects.tar.gz`` to stdout (or ``--out-tar``),
       and emits ``@@DALTON_CTL@@ BACKUP_SUMMARY <json>`` to stderr.
    """
    dsn = resolve_standalone_pg_dsn(args.pg_dsn, os.environ)
    specs = bucket_resolver(os.environ)
    stage_dir = Path(tempfile.mkdtemp(prefix="dalton-core-cb-"))
    tar_path = Path(args.out_tar) if (args.out_tar and args.out_tar != "-") else (stage_dir / "minio-objects.tar.gz")
    asr_dir = Path(args.asr_jobs_dir) if args.asr_jobs_dir else None
    asr_tar = (stage_dir / "asr-jobs.tar.gz") if (asr_dir and asr_dir.is_dir()) else None

    def _emit_ctl(msg: str) -> None:
        sys.stderr.write(f"{CTL_PREFIX}{msg}\n")
        sys.stderr.flush()

    async def _read_ctl_token(expected: str) -> None:
        line = await asyncio.to_thread(sys.stdin.readline)
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
                    pg_dump_fn=lambda snap_id, _out_p: _emit_ctl(f"SNAPSHOT_READY {snap_id}"),
                    wait_pg_dump_imported_fn=lambda _h: _read_ctl_token("SNAPSHOT_IMPORTED"),
                    wait_pg_dump_finished_fn=lambda _h: _read_ctl_token("PG_DUMP_DONE"),
                    bucket_specs=specs,
                    object_sink=sink,
                    asr_jobs_dir=asr_dir if asr_tar is not None else None,
                    asr_archive_path=asr_tar,
                    lock_phase_timeout_s=args.lock_timeout_s,
                    pg_dump_timeout_s=args.pg_dump_timeout_s,
                    mirror_timeout_s=args.mirror_timeout_s,
                    snapshot_import_timeout_s=args.snapshot_import_timeout_s,
                )
            finally:
                await conn.close()

        res = asyncio.run(_execute())
        if not args.out_tar or args.out_tar == "-":
            with tar_path.open("rb") as fh:
                shutil.copyfileobj(fh, sys.stdout.buffer, length=1024 * 1024)
            sys.stdout.buffer.flush()

        summary_payload = {
            "snapshot_id": res.snapshot_id,
            "object_count": len(res.manifest_objects),
            "warnings": list(res.warnings),
            "reconcile_ok": res.reconcile_report.ok,
            "checked_refs": res.reconcile_report.checked_refs,
        }
        _emit_ctl("BACKUP_SUMMARY " + json.dumps(summary_payload, ensure_ascii=False))
        return 0
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


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
    p_cb.add_argument("--asr-jobs-dir", default="")
    p_cb.add_argument("--lock-timeout-s", type=float, default=None)
    p_cb.add_argument("--pg-dump-timeout-s", type=float, default=None)
    p_cb.add_argument("--mirror-timeout-s", type=float, default=None)
    p_cb.add_argument("--snapshot-import-timeout-s", type=float, default=None)
    p_cb.set_defaults(func=_cmd_compose_backup)

    p_rec = sub.add_parser("reconcile-restored-inflight-states")
    p_rec.add_argument("--skip-object-check", action="store_true")
    p_rec.add_argument("--asr-jobs-dir", default="")
    p_rec.add_argument("--restored-before", default="")
    p_rec.set_defaults(
        func=lambda a: consistent_backup.main(
            ["reconcile-restored-inflight-states"]
            + (["--skip-object-check"] if a.skip_object_check else [])
            + (["--asr-jobs-dir", a.asr_jobs_dir] if a.asr_jobs_dir else [])
            + (["--restored-before", a.restored_before] if a.restored_before else [])
        )
    )

    p_ref = sub.add_parser("reconcile")
    p_ref.add_argument("extra", nargs=argparse.REMAINDER)
    p_ref.set_defaults(func=lambda a: storage_refs.main(["reconcile", *a.extra]))
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
