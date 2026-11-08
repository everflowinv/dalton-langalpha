"""Per-slot artifact freeze and atomic ``done.json`` writer for OC-1 data tasks (§6.1 item 1)."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
from typing import Any, Mapping, Sequence
import uuid

from src.dalton.data_jobs.evaluators import extract_t5_pipeline_steps
from src.dalton.data_jobs.manifest import load_data_task_manifest

logger = logging.getLogger(__name__)

FREEZE_DIR_NAME = ".dalton-freeze"
DONE_FILENAME = "done.json"

_ISO_DATE_RE = re.compile(r"^(\d{4})[-/](\d{2})[-/](\d{2})(?:[T\s].*)?$")
_COMPACT_DATE_RE = re.compile(r"^(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$")
_VOLATILE_JSON_KEYS = frozenset(
    {
        "completed_at",
        "created_at",
        "generated_at",
        "run_id",
        "started_at",
        "timestamp",
        "updated_at",
    }
)


def format_freeze_slot_utc(value: datetime | str) -> str:
    """Format ``scheduled_slot_utc`` / ``completed_at`` as a canonical UTC ISO-8601 string."""
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        dt_utc = dt.astimezone(timezone.utc)
        if dt_utc.microsecond:
            return dt_utc.isoformat().replace("+00:00", "Z")
        return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    raw = str(value).strip()
    if not raw:
        raise ValueError("scheduled_slot_utc cannot be empty")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed_utc = parsed.astimezone(timezone.utc)
        if parsed_utc.microsecond:
            return parsed_utc.isoformat().replace("+00:00", "Z")
        return parsed_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return raw


def compute_file_sha256(path: Path) -> str:
    """Return the hex SHA-256 digest of ``path``."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_date_token(token: str) -> str | None:
    text = token.strip()
    if not text:
        return None
    m_iso = _ISO_DATE_RE.match(text)
    if m_iso:
        y, m, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"
    m_compact = _COMPACT_DATE_RE.match(text)
    if m_compact:
        return f"{m_compact.group(1)}-{m_compact.group(2)}-{m_compact.group(3)}"
    return None


def summarize_csv_file(path: Path) -> tuple[int, str | None]:
    """Return ``(row_count, latest_date)`` for a CSV file.

    ``row_count`` is the number of non-empty data rows after the header row.
    ``latest_date`` is the maximum normalized ``YYYY-MM-DD`` date across date columns.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return 0, None

    reader = csv.reader(lines)
    try:
        header = next(reader)
    except StopIteration:
        return 0, None

    date_col_indices: list[int] = []
    for idx, col_name in enumerate(header):
        lowered = col_name.strip().lower()
        if (
            lowered in {"date", "trade_date", "as_of_date", "dt", "day", "timestamp"}
            or "date" in lowered
        ):
            date_col_indices.append(idx)
    if not date_col_indices and header:
        date_col_indices = [0]

    row_count = 0
    latest_date: str | None = None
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        row_count += 1
        for col_idx in date_col_indices:
            if col_idx < len(row):
                norm = _normalize_date_token(row[col_idx])
                if norm is not None and (latest_date is None or norm > latest_date):
                    latest_date = norm

    return row_count, latest_date


def _strip_volatile_fields(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {
            str(k): _strip_volatile_fields(v)
            for k, v in sorted(obj.items(), key=lambda item: str(item[0]))
            if str(k) not in _VOLATILE_JSON_KEYS
        }
    if isinstance(obj, list):
        return [_strip_volatile_fields(item) for item in obj]
    return obj


def extract_core_metrics(
    *,
    job_key: str,
    slot_dir: Path,
    matched_rel_paths: Sequence[str],
    csv_summary: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Extract deterministic core metrics summary from frozen artifacts in ``slot_dir``."""
    metrics: dict[str, Any] = {}

    for rel in matched_rel_paths:
        file_path = slot_dir / rel
        if not file_path.is_file():
            continue
        if rel == "state/cn-leverage/pipeline_status.json":
            try:
                raw = json.loads(file_path.read_text(encoding="utf-8"))
                if isinstance(raw, Mapping):
                    metrics["pipeline_steps"] = extract_t5_pipeline_steps(raw)
                    if "trade_date" in raw:
                        metrics["trade_date"] = raw.get("trade_date")
            except Exception:
                pass
        elif rel == "temp/megacap-relative-signal-monitor/latest.json":
            try:
                raw = json.loads(file_path.read_text(encoding="utf-8"))
                if isinstance(raw, Mapping):
                    metrics["date"] = raw.get("date")
                    rows = raw.get("rows")
                    if isinstance(rows, list):
                        metrics["row_count"] = len(rows)
                        signals: dict[str, Any] = {}
                        for idx, row_item in enumerate(rows):
                            if isinstance(row_item, Mapping):
                                key = str(
                                    row_item.get("ticker")
                                    or row_item.get("symbol")
                                    or idx
                                )
                                signals[key] = _strip_volatile_fields(row_item)
                        metrics["signals"] = signals
            except Exception:
                pass
        elif rel.lower().endswith(".json"):
            try:
                raw = json.loads(file_path.read_text(encoding="utf-8"))
                if isinstance(raw, Mapping):
                    metrics[rel] = _strip_volatile_fields(raw)
            except Exception:
                pass

    if "state/cn-leverage/quant_unwind_signals.csv" in csv_summary:
        unwind_meta = csv_summary["state/cn-leverage/quant_unwind_signals.csv"]
        metrics["quant_unwind_row_count"] = unwind_meta.get("row_count")
        metrics["quant_unwind_latest_date"] = unwind_meta.get("latest_date")

    return metrics


def resolve_manifest_freeze_paths(job_key: str) -> tuple[str, list[str]]:
    """Return ``(canonical_job_key, freeze_paths)`` from ``manifest.yaml`` for ``job_key``."""
    try:
        manifest = load_data_task_manifest()
        entry = manifest.by_job_key().get(job_key) or manifest.by_task_code().get(job_key)
        if entry is not None:
            return entry.job_key, list(entry.freeze_paths)
    except Exception:
        pass
    return job_key, []


def _is_strictly_within(candidate_real: Path, root_real: Path) -> bool:
    """Return True iff ``candidate_real`` is strictly inside ``root_real``."""
    return candidate_real != root_real and candidate_real.is_relative_to(root_real)


def _has_symlink_below(base: Path, target: Path) -> bool:
    """Return True if ``base`` itself or any path component from ``base`` to ``target`` is a symlink."""
    if base.is_symlink():
        return True
    try:
        rel_parts = target.relative_to(base).parts
    except ValueError:
        return True
    cur = base
    for part in rel_parts:
        if part in ("", "."):
            continue
        if part == "..":
            return True
        cur = cur / part
        if cur.is_symlink():
            return True
    return False


def _expand_freeze_paths(
    root_path: Path,
    root_real: Path,
    patterns: Sequence[str],
) -> tuple[list[str], list[str]]:
    """Expand and validate ``patterns`` under ``root_path``.

    Returns ``(matched_rel_paths, missing_or_invalid_patterns)``.
    Rejects absolute paths, ``..`` components, symlinks, and any path resolving
    outside ``root_real``.
    """
    matched: list[str] = []
    missing_or_invalid: list[str] = []
    seen: set[str] = set()
    for raw_pat in patterns:
        pat = str(raw_pat).strip()
        if not pat:
            logger.warning("Skipping empty freeze_path entry in %s", root_path)
            missing_or_invalid.append(str(raw_pat))
            continue

        pat_obj = Path(pat)
        if (
            pat.startswith(("/", "\\"))
            or pat_obj.is_absolute()
            or ".." in pat_obj.parts
        ):
            logger.warning(
                "Rejected unsafe freeze_path %r (absolute or contains '..') under %s",
                raw_pat,
                root_path,
            )
            missing_or_invalid.append(pat)
            continue

        norm_pat = pat_obj.as_posix()
        if norm_pat in ("", "."):
            logger.warning("Rejected degenerate freeze_path %r under %s", raw_pat, root_path)
            missing_or_invalid.append(pat)
            continue

        if any(ch in norm_pat for ch in ("*", "?", "[")):
            glob_hits = 0
            for candidate in sorted(root_path.glob(norm_pat)):
                if _has_symlink_below(root_path, candidate) or candidate.is_symlink():
                    logger.warning(
                        "Skipping symlink freeze candidate %s for pattern %r",
                        candidate,
                        norm_pat,
                    )
                    continue
                try:
                    cand_real = candidate.resolve()
                except OSError:
                    logger.warning("Failed to resolve freeze candidate %s", candidate)
                    continue
                if not _is_strictly_within(cand_real, root_real):
                    logger.warning(
                        "Skipping freeze candidate %s resolved outside data_root %s",
                        candidate,
                        root_real,
                    )
                    continue
                if not candidate.is_file():
                    continue
                rel = cand_real.relative_to(root_real).as_posix()
                glob_hits += 1
                if rel not in seen:
                    seen.add(rel)
                    matched.append(rel)
            if glob_hits == 0:
                missing_or_invalid.append(norm_pat)
        else:
            candidate = root_path / norm_pat
            if _has_symlink_below(root_path, candidate) or candidate.is_symlink():
                logger.warning(
                    "Skipping symlink freeze_path %r (%s) under %s",
                    norm_pat,
                    candidate,
                    root_path,
                )
                missing_or_invalid.append(norm_pat)
                continue
            try:
                cand_real = candidate.resolve()
            except OSError:
                logger.warning("Failed to resolve freeze_path %r (%s)", norm_pat, candidate)
                missing_or_invalid.append(norm_pat)
                continue
            if not _is_strictly_within(cand_real, root_real):
                logger.warning(
                    "Skipping freeze_path %r resolved outside data_root %s (%s)",
                    norm_pat,
                    root_real,
                    cand_real,
                )
                missing_or_invalid.append(norm_pat)
                continue
            if not candidate.is_file():
                missing_or_invalid.append(norm_pat)
                continue
            rel = cand_real.relative_to(root_real).as_posix()
            if rel not in seen:
                seen.add(rel)
                matched.append(rel)
    return matched, missing_or_invalid


def freeze_task_run_artifacts(
    *,
    data_root: Path | str,
    job_key: str,
    scheduled_slot_utc: datetime | str,
    completed_at: datetime | str | None = None,
    exit_code: int | None = 0,
    freeze_paths: Sequence[str] | None = None,
) -> Path | None:
    """Copy verification artifacts for ``(job_key, scheduled_slot_utc)`` and atomically write ``done.json`` (§6.1 item 1).

    Returns the path to ``done.json``, or ``None`` when ``freeze_paths`` is not explicitly
    provided and the task's manifest entry has an empty ``freeze_paths`` list, or when
    any path validation / artifact completeness check fails.
    """
    resolved_job_key, manifest_paths = resolve_manifest_freeze_paths(job_key)
    if freeze_paths is None:
        if not manifest_paths:
            return None
        patterns: list[str] = manifest_paths
    else:
        patterns = list(freeze_paths)

    root_path = Path(data_root)
    if root_path.is_symlink():
        logger.warning(
            "Aborting freeze for job_key=%s: data_root %s is a symlink",
            resolved_job_key,
            root_path,
        )
        return None
    root_real = root_path.resolve()

    slot_str = format_freeze_slot_utc(scheduled_slot_utc)
    completed_str = format_freeze_slot_utc(
        completed_at if completed_at is not None else datetime.now(timezone.utc)
    )
    if (
        Path(resolved_job_key).parts != (resolved_job_key,)
        or resolved_job_key in (".", "..")
        or Path(slot_str).parts != (slot_str,)
        or slot_str in (".", "..")
    ):
        logger.warning(
            "Aborting freeze: invalid job_key=%r or slot=%r",
            resolved_job_key,
            slot_str,
        )
        return None

    freeze_base = root_path / FREEZE_DIR_NAME
    job_dir = freeze_base / resolved_job_key
    slot_dir = job_dir / slot_str

    for ancestor in (freeze_base, job_dir, slot_dir):
        if ancestor.is_symlink():
            logger.warning(
                "Aborting freeze for job_key=%s slot=%s: %s is a symlink",
                resolved_job_key,
                slot_str,
                ancestor,
            )
            return None
        if ancestor.exists() and not _is_strictly_within(ancestor.resolve(), root_real):
            logger.warning(
                "Aborting freeze for job_key=%s slot=%s: %s resolves outside data_root %s",
                resolved_job_key,
                slot_str,
                ancestor,
                root_real,
            )
            return None

    matched_rel_paths, missing_or_invalid = _expand_freeze_paths(
        root_path, root_real, patterns
    )
    if patterns and (missing_or_invalid or not matched_rel_paths):
        logger.warning(
            "Skipping done.json for job_key=%s slot=%s: missing or invalid freeze_paths=%s",
            resolved_job_key,
            slot_str,
            missing_or_invalid,
        )
        return None

    slot_dir.mkdir(parents=True, exist_ok=True)
    slot_real = slot_dir.resolve()
    if _has_symlink_below(root_path, slot_dir) or not _is_strictly_within(
        slot_real, root_real
    ):
        logger.warning(
            "Aborting freeze for job_key=%s slot=%s: slot_dir %s is a symlink or outside data_root %s",
            resolved_job_key,
            slot_str,
            slot_dir,
            root_real,
        )
        return None

    files_info: dict[str, dict[str, Any]] = {}
    file_sha256: dict[str, str] = {}
    csv_summary: dict[str, dict[str, Any]] = {}

    for rel in matched_rel_paths:
        src_file = root_path / rel
        if _has_symlink_below(root_path, src_file) or src_file.is_symlink():
            logger.warning("Aborting freeze: source %s is a symlink", src_file)
            return None
        src_real = src_file.resolve()
        if not _is_strictly_within(src_real, root_real):
            logger.warning(
                "Aborting freeze: source %s resolves outside data_root %s",
                src_file,
                root_real,
            )
            return None

        dst_file = slot_dir / rel
        if _has_symlink_below(slot_dir, dst_file) or dst_file.is_symlink():
            logger.warning("Aborting freeze: destination %s is a symlink", dst_file)
            return None
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        if (
            _has_symlink_below(slot_dir, dst_file)
            or dst_file.is_symlink()
            or not _is_strictly_within(dst_file.resolve(), slot_real)
        ):
            logger.warning(
                "Aborting freeze: destination %s resolves outside slot_dir %s",
                dst_file,
                slot_real,
            )
            return None
        shutil.copy2(src_file, dst_file, follow_symlinks=False)

        sha256_hex = compute_file_sha256(dst_file)
        size_bytes = dst_file.stat().st_size
        file_sha256[rel] = sha256_hex

        entry_meta: dict[str, Any] = {
            "sha256": sha256_hex,
            "size_bytes": size_bytes,
        }
        if rel.lower().endswith(".csv"):
            row_count, latest_date = summarize_csv_file(dst_file)
            entry_meta["row_count"] = row_count
            entry_meta["latest_date"] = latest_date
            csv_summary[rel] = {
                "row_count": row_count,
                "latest_date": latest_date,
            }
        files_info[rel] = entry_meta

    core_metrics = extract_core_metrics(
        job_key=resolved_job_key,
        slot_dir=slot_dir,
        matched_rel_paths=matched_rel_paths,
        csv_summary=csv_summary,
    )

    done_payload: dict[str, Any] = {
        "job_key": resolved_job_key,
        "scheduled_slot_utc": slot_str,
        "completed_at": completed_str,
        "exit_code": exit_code,
        "files": files_info,
        "file_sha256": file_sha256,
        "csv_summary": csv_summary,
        "core_metrics": core_metrics,
    }

    done_path = slot_dir / DONE_FILENAME
    tmp_path = slot_dir / f".{DONE_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    serialized = (
        json.dumps(done_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    try:
        tmp_path.write_text(serialized, encoding="utf-8")
        os.replace(tmp_path, done_path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise

    return done_path
