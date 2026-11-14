"""Shared host/workspace store, path guards, and resumable state for `podcast-weekly`."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.dalton.settings import get_dalton_backend_var_dir, get_dalton_host_var_dir

HK_TZ = timezone(timedelta(hours=8))
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CHANNELS_PATH = (
    REPO_ROOT
    / "plugins"
    / "dalton"
    / "skills"
    / "dalton-podcast-weekly"
    / "references"
    / "channels.json"
)
EMPTY_WEEK_MESSAGE = "播客周报｜本周 All-In 和 a16z 没有新节目"
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
REPORT_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{3,80}$")

# Strictly allow reads/writes ONLY inside these two relative directories under data_root.
ALLOWED_PODCAST_REL_DIRS: tuple[Path, ...] = (
    Path("research-outputs/podcast-weekly"),
    Path("state/podcast-weekly"),
)

ARTIFACT_EXTENSION_MAP: dict[str, str] = {
    "zh": ".zh.md",
    "en": ".en.md",
    "notes": ".notes.json",
    "merged": ".merged.md",
    "summary": ".summary.md",
}
WRITABLE_ARTIFACTS: frozenset[str] = frozenset({"zh", "en", "notes", "summary"})


class RecordUnreadableError(RuntimeError):
    """Raised when an existing state record cannot be read or decoded."""


class PodcastStoreSecurityError(ValueError):
    """Raised when a path, filename, or video_id escapes the podcast-weekly boundary."""


def resolve_host_openclaw_data_root() -> Path:
    """Resolve `var/dalton/openclaw-data` in backend container or host dev checkout."""
    env_backend = os.getenv("DALTON_BACKEND_VAR_DIR", "").strip()
    if env_backend:
        return (Path(env_backend).expanduser() / "openclaw-data").resolve()
    backend_var = Path(get_dalton_backend_var_dir())
    if backend_var.exists():
        return (backend_var / "openclaw-data").resolve()
    return (Path(get_dalton_host_var_dir()).expanduser() / "openclaw-data").resolve()


def validate_video_id(raw: str) -> str:
    """Validate strict YouTube/episode `video_id` token (`^[A-Za-z0-9_-]{3,64}$`)."""
    cleaned = str(raw or "").strip()
    if not VIDEO_ID_RE.fullmatch(cleaned):
        raise PodcastStoreSecurityError(f"非法 video_id: {raw!r}")
    return cleaned


def validate_report_run_id(raw: str) -> str:
    """Validate `report_run_id` token used in automation reports and `reported.json`."""
    cleaned = str(raw or "").strip()
    if not REPORT_RUN_ID_RE.fullmatch(cleaned) or ".." in cleaned:
        raise PodcastStoreSecurityError(f"非法 report_run_id: {raw!r}")
    return cleaned


def _assert_no_relative_symlinks(root: Path, rel_parts: tuple[str, ...]) -> None:
    """Reject any symlink anywhere between `root` and `root / rel_parts` BEFORE calling resolve()."""
    if root.is_symlink():
        raise PodcastStoreSecurityError(f"拒绝软链接根目录: {root}")
    cur = root
    for part in rel_parts:
        cur = cur / part
        if cur.is_symlink():
            raise PodcastStoreSecurityError(f"拒绝软链接路径: {cur}")


def _fixed_allowed_dir(root: Path, rel_dir: Path) -> Path:
    """Return fixed allowed directory path ONLY after verifying intermediate dirs are not symlinks."""
    _assert_no_relative_symlinks(root, rel_dir.parts)
    # Anchor root.resolve() once only AFTER verifying rel_dir components are non-symlinks,
    # keeping allowed directories fixed rather than following a symlinked podcast dir.
    return root.resolve() / rel_dir


def safe_podcast_path(
    root: Path,
    rel_path: str | Path,
) -> Path:
    """Resolve `rel_path` strictly inside fixed `research-outputs/podcast-weekly` or `state/podcast-weekly`.

    Checks every intermediate segment from `root` for symlinks BEFORE any `.resolve()`,
    and keeps allowed boundaries anchored at fixed relative directories.
    """
    if isinstance(rel_path, Path):
        rel = rel_path
    else:
        raw = str(rel_path or "").strip()
        if not raw:
            raise PodcastStoreSecurityError("路径不能为空")
        rel = Path(raw)

    if rel.is_absolute() or not rel.parts or any(part in ("", ".", "..") for part in rel.parts):
        raise PodcastStoreSecurityError(f"只允许安全相对路径: {rel}")

    if len(rel.parts) < 2 or Path(*rel.parts[:2]) not in ALLOWED_PODCAST_REL_DIRS:
        raise PodcastStoreSecurityError(f"路径越界（仅允许 podcast-weekly 产出/状态目录）: {rel}")

    # Step 1: check every segment BEFORE resolve() so a symlinked podcast dir fails immediately.
    _assert_no_relative_symlinks(root, rel.parts)

    # Step 2: compute fixed non-symlinked boundary and verify resolved descendant stays within it.
    allowed_dir_rel = Path(*rel.parts[:2])
    fixed_allowed = _fixed_allowed_dir(root, allowed_dir_rel)
    fixed_root = root.resolve()
    target = (fixed_root / rel).resolve()
    if target != fixed_allowed and fixed_allowed not in target.parents:
        raise PodcastStoreSecurityError(f"路径越界（仅允许 podcast-weekly 产出/状态目录）: {rel}")
    return target


def _safe_child_in_dir(allowed_dir: Path, filename: str) -> Path:
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        raise PodcastStoreSecurityError(f"非法文件名: {filename!r}")
    if allowed_dir.is_symlink():
        raise PodcastStoreSecurityError(f"拒绝软链接目录: {allowed_dir}")
    raw_target = allowed_dir / filename
    if raw_target.is_symlink():
        raise PodcastStoreSecurityError(f"拒绝软链接文件: {raw_target}")
    resolved_dir = allowed_dir.resolve()
    candidate = (resolved_dir / filename).resolve()
    if candidate.parent != resolved_dir:
        raise PodcastStoreSecurityError(f"文件名越界: {filename!r}")
    return candidate


def _research_dir(root: Path) -> Path:
    return safe_podcast_path(root, ALLOWED_PODCAST_REL_DIRS[0])


def _state_dir(root: Path) -> Path:
    return safe_podcast_path(root, ALLOWED_PODCAST_REL_DIRS[1])


@contextlib.contextmanager
def podcast_state_lock(root: Path) -> Iterator[None]:
    """Acquire exclusive `fcntl.flock` on `state/podcast-weekly/episodes.lock`."""
    st = _state_dir(root)
    st.mkdir(parents=True, exist_ok=True)
    lock_file = _safe_child_in_dir(st, "episodes.lock")
    with open(lock_file, "a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def ledger_path(root: Path) -> Path:
    return _safe_child_in_dir(_state_dir(root), "episodes.json")


def _doc_dir(root: Path, vid: str) -> Path:
    safe_vid = validate_video_id(vid)
    st = _state_dir(root)
    docs_root = st / "docs"
    if docs_root.is_symlink():
        raise PodcastStoreSecurityError(f"拒绝软链接目录: {docs_root}")
    ep_dir = docs_root / safe_vid
    if ep_dir.is_symlink():
        raise PodcastStoreSecurityError(f"拒绝软链接目录: {ep_dir}")
    return ep_dir


def _doc_chunks_dir(root: Path, vid: str) -> Path:
    ep_dir = _doc_dir(root, vid)
    if ep_dir.exists() and not ep_dir.is_dir():
        raise PodcastStoreSecurityError(f"非目录状态路径: {ep_dir}")
    cdir = ep_dir / "chunks"
    if cdir.is_symlink():
        raise PodcastStoreSecurityError(f"拒绝软链接目录: {cdir}")
    return cdir


def doc_record_path(root: Path, vid: str) -> Path:
    ep_dir = _doc_dir(root, vid)
    if ep_dir.exists() and not ep_dir.is_dir():
        raise PodcastStoreSecurityError(f"非目录状态路径: {ep_dir}")
    return _safe_child_in_dir(ep_dir, "doc.json")


def doc_failed_path(root: Path, vid: str) -> Path:
    ep_dir = _doc_dir(root, vid)
    if ep_dir.exists() and not ep_dir.is_dir():
        raise PodcastStoreSecurityError(f"非目录状态路径: {ep_dir}")
    return _safe_child_in_dir(ep_dir, "failed.json")


def reported_path(root: Path) -> Path:
    return _safe_child_in_dir(_state_dir(root), "reported.json")


def pending_report_path(root: Path) -> Path:
    return _safe_child_in_dir(_state_dir(root), "pending_report.json")


def _read_text_nofollow(path: Path) -> str:
    """Read UTF-8 file using `O_NOFOLLOW` so symlinks cannot be followed even mid-flight."""
    if path.is_symlink():
        raise PodcastStoreSecurityError(f"拒绝读取软链接文件: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        return fh.read()


def read_json_dict(path: Path, *, required: bool = False) -> dict[str, Any]:
    """Read a JSON object from `path`; raises `FileNotFoundError` when `required` & absent."""
    if path.is_symlink():
        raise RecordUnreadableError(f"状态文件为软链接: {path}")
    if not path.exists():
        if required:
            raise FileNotFoundError(f"台账或状态文件不存在: {path}")
        return {}
    if not path.is_file():
        raise RecordUnreadableError(f"状态路径不是常规文件: {path}")
    try:
        data = json.loads(_read_text_nofollow(path))
    except Exception as exc:
        raise RecordUnreadableError(f"无法读取状态文件 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RecordUnreadableError(f"状态文件格式非法（非 JSON 对象）: {path}")
    return data


def atomic_write_text(path: Path, text: str) -> None:
    if path.is_symlink():
        raise PodcastStoreSecurityError(f"拒绝写入软链接: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def default_report_run_id(date_str: str | None = None) -> str:
    day = (date_str or "").strip() or datetime.now(HK_TZ).strftime("%Y-%m-%d")
    return validate_report_run_id(f"podcast-weekly-{day}")


def channels_folder_token(channels_path: Path = DEFAULT_CHANNELS_PATH) -> str:
    cfg = json.loads(channels_path.read_text(encoding="utf-8"))
    return str(cfg.get("feishu_folder_token") or "").strip()


def format_upload_date(raw_date: str) -> str:
    digits = re.sub(r"[^0-9]", "", str(raw_date or ""))
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return str(raw_date or "")


def _build_fixes_lines(notes: dict[str, Any]) -> list[str]:
    fixes: list[str] = []
    for item in notes.get("corrections") or []:
        fixes.append(f"- {item}")
    for item in notes.get("speaker_flags") or []:
        fixes.append(f"- {item}")
    for item in notes.get("unclear") or []:
        fixes.append(f"- 听不清，未补写：{item}")
    for warn in notes.get("amount_warnings") or []:
        fixes.append(
            f"- 金额待核（第 {warn.get('line')} 行 {warn.get('raw')}）：{warn.get('hint')}"
        )
    if not fixes:
        fixes.append("本期没有需要说明的校正。")
    return fixes


def split_markdown_for_feishu(markdown: str, max_chars: int) -> list[str]:
    """Split oversized bilingual Markdown along section/paragraph boundaries."""
    if len(markdown) <= max_chars:
        return [markdown]
    marker = "\n---\n\n## 英文校订稿"
    if marker in markdown:
        head, tail = markdown.split(marker, 1)
        sections = [head.strip(), ("## 英文校订稿" + tail).strip()]
    else:
        sections = [markdown]

    chunks: list[str] = []
    for sec in sections:
        if len(sec) <= max_chars:
            chunks.append(sec)
            continue
        cur: list[str] = []
        cur_len = 0
        for para in sec.split("\n\n"):
            add_len = len(para) + (2 if cur else 0)
            if cur and cur_len + add_len > max_chars:
                chunks.append("\n\n".join(cur))
                cur = [para]
                cur_len = len(para)
            else:
                cur.append(para)
                cur_len += add_len
        if cur:
            chunks.append("\n\n".join(cur))
    return chunks


def _artifact_ext(artifact: str) -> str:
    key = str(artifact or "").strip().lower()
    if key not in ARTIFACT_EXTENSION_MAP:
        allowed = ", ".join(sorted(ARTIFACT_EXTENSION_MAP))
        raise PodcastStoreSecurityError(f"不支持的产物类型 {artifact!r}（可选: {allowed}）")
    return ARTIFACT_EXTENSION_MAP[key]


def resolve_artifact_path(
    root: Path,
    vid: str,
    artifact: str,
    *,
    row: dict[str, Any] | None = None,
    for_write: bool = False,
) -> Path:
    """Resolve an episode's `.zh.md`/`.en.md`/`.notes.json`/`.merged.md`/`.summary.md` strictly inside `research-outputs/podcast-weekly`."""
    safe_vid = validate_video_id(vid)
    ext = _artifact_ext(artifact)
    out_dir = _research_dir(root)
    if for_write and artifact.strip().lower() not in WRITABLE_ARTIFACTS:
        raise PodcastStoreSecurityError(f"产物 {artifact!r} 只读，不允许写入")

    # 1. Check upload_date from ledger row if available.
    upload_date = re.sub(r"[^0-9]", "", str((row or {}).get("upload_date") or ""))
    if upload_date and len(upload_date) == 8:
        candidate = _safe_child_in_dir(out_dir, f"{upload_date}-{safe_vid}{ext}")
        if for_write or candidate.is_file():
            return candidate

    # 2. Check ledger `notes_path` stem ONLY if it stays strictly inside `research-outputs/podcast-weekly`.
    raw_notes = str((row or {}).get("notes_path") or "").strip()
    marker = "research-outputs/podcast-weekly/"
    if raw_notes and marker in raw_notes:
        rel_name = raw_notes.split(marker, 1)[1].strip()
        if rel_name.endswith(".notes.json") and f"-{safe_vid}.notes.json" in rel_name:
            stem = rel_name[: -len(".notes.json")]
            if re.fullmatch(r"[A-Za-z0-9_-]+", stem):
                candidate = _safe_child_in_dir(out_dir, f"{stem}{ext}")
                if for_write or candidate.is_file():
                    return candidate

    # 3. Scan existing matching files in `research-outputs/podcast-weekly/`.
    if out_dir.is_dir():
        matches = sorted(
            p
            for p in out_dir.iterdir()
            if p.name.endswith(f"-{safe_vid}{ext}") and not p.is_symlink() and p.is_file()
        )
        if matches:
            return _safe_child_in_dir(out_dir, matches[-1].name)
        if ext != ".notes.json":
            notes_matches = sorted(
                p
                for p in out_dir.iterdir()
                if p.name.endswith(f"-{safe_vid}.notes.json") and not p.is_symlink() and p.is_file()
            )
            if notes_matches:
                stem = notes_matches[-1].name[: -len(".notes.json")]
                return _safe_child_in_dir(out_dir, f"{stem}{ext}")

    if for_write:
        day = upload_date if len(upload_date) == 8 else datetime.now(HK_TZ).strftime("%Y%m%d")
        return _safe_child_in_dir(out_dir, f"{day}-{safe_vid}{ext}")

    raise FileNotFoundError(f"未找到 {safe_vid} 的产物 {ext}")


def _doc_is_complete(doc_rec: dict[str, Any]) -> bool:
    if not doc_rec or not str(doc_rec.get("doc_url") or "").strip():
        return False
    status = str(doc_rec.get("body_status") or "").strip().lower()
    if status == "incomplete":
        return False
    if status == "complete":
        return True
    # Legacy records with doc_url and no body_status were written only on full completion.
    return True


def _resolve_hk_date(date_str: str | None = None) -> str:
    clean_date = (date_str or "").strip() or datetime.now(HK_TZ).strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", clean_date):
        raise ValueError(f"非法日期格式: {clean_date!r}")
    return clean_date


def _compute_report_run_id(
    today_date: str,
    success_ids: list[str],
    failed_ids: list[str],
    *,
    abandoned_ids: set[str] | frozenset[str] | None = None,
) -> str:
    if not success_ids and not failed_ids:
        base = f"podcast-weekly-{today_date}-empty"
    else:
        tokens = sorted([*(f"s:{v}" for v in set(success_ids)), *(f"f:{v}" for v in set(failed_ids))])
        digest = hashlib.sha256("\n".join(tokens).encode("utf-8")).hexdigest()[:10]
        base = f"podcast-weekly-{today_date}-{digest}"
    abandoned_set = set(abandoned_ids or ())
    candidate = validate_report_run_id(base)
    rev = 2
    while candidate in abandoned_set:
        candidate = validate_report_run_id(f"{base}-r{rev}")
        rev += 1
    return candidate


def _load_reported_unlocked(
    root: Path,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    rpath = reported_path(root)
    raw = read_json_dict(rpath)
    out: list[dict[str, dict[str, Any]]] = []
    for key in ("success", "failed", "empty_weeks", "abandoned"):
        sub = raw.get(key)
        if sub is None:
            out.append({})
        elif isinstance(sub, dict):
            out.append({str(k): dict(v) for k, v in sub.items() if isinstance(v, dict)})
        else:
            raise RecordUnreadableError(f"reported.json 字段 {key!r} 类型无效")
    return out[0], out[1], out[2], out[3]


def _load_pending_report_unlocked(root: Path) -> dict[str, Any] | None:
    ppath = pending_report_path(root)
    if ppath.is_symlink():
        raise RecordUnreadableError(f"状态文件为软链接: {ppath}")
    if not ppath.exists():
        return None
    raw = read_json_dict(ppath, required=True)
    run_id = validate_report_run_id(str(raw.get("report_run_id") or ""))
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise RecordUnreadableError(f"pending_report.json 缺少有效 text: {ppath}")
    raw_success = raw.get("success_video_ids") or []
    raw_failed = raw.get("failed_video_ids") or []
    if not isinstance(raw_success, list) or not isinstance(raw_failed, list):
        raise RecordUnreadableError(f"pending_report.json id 列表类型无效: {ppath}")
    clean_success = list(dict.fromkeys(validate_video_id(str(v)) for v in raw_success))
    clean_failed = list(dict.fromkeys(validate_video_id(str(v)) for v in raw_failed))
    raw_empty_date = raw.get("empty_date")
    clean_empty_date = (
        _resolve_hk_date(str(raw_empty_date))
        if raw_empty_date is not None and str(raw_empty_date).strip()
        else None
    )
    return {
        "report_run_id": run_id,
        "text": text,
        "success_video_ids": clean_success,
        "failed_video_ids": clean_failed,
        "empty_date": clean_empty_date,
        "created_at": str(raw.get("created_at") or ""),
    }


def _classify_episode_bucket(
    row: dict[str, Any],
    doc_rec: dict[str, Any],
    failed_doc: dict[str, Any] | None = None,
) -> str:
    doc_url = str(doc_rec.get("doc_url") or row.get("doc_url") or "").strip()
    is_doc_done = _doc_is_complete(doc_rec) or (
        bool(row.get("published_at")) and bool(doc_url) and not doc_rec
    )
    if is_doc_done and doc_url:
        return "published"
    if doc_rec and (doc_url or str(doc_rec.get("document_id") or "").strip()) and not is_doc_done:
        return "incomplete_docs"
    if int((failed_doc or {}).get("attempts") or 0) >= 2:
        return "stage_incomplete"
    if str(row.get("stage") or "") == "ready":
        return "ready"
    return "stage_incomplete"


def _hk_date_from_timestamp(raw_ts: str) -> str | None:
    val = str(raw_ts or "").strip()
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(val)
    except ValueError:
        return val[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", val[:10]) else None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=HK_TZ)
    else:
        dt = dt.astimezone(HK_TZ)
    return dt.strftime("%Y-%m-%d")


def _has_report_on_date(
    date_str: str,
    rep_success: dict[str, Any],
    rep_failed: dict[str, Any],
    rep_empty: dict[str, Any],
) -> bool:
    if date_str in rep_empty:
        return True
    for entry in rep_empty.values():
        if not isinstance(entry, dict):
            continue
        if _hk_date_from_timestamp(str(entry.get("reported_at") or "")) == date_str:
            return True
    for group in (rep_success, rep_failed):
        for entry in group.values():
            if not isinstance(entry, dict):
                continue
            run_id = str(entry.get("report_run_id") or "")
            if _extract_date_from_run_id(run_id) == date_str:
                return True
            if _hk_date_from_timestamp(str(entry.get("reported_at") or "")) == date_str:
                return True
    return False


def _compute_status_unlocked(
    root: Path,
    *,
    today_date: str,
    pending_only: bool = False,
) -> dict[str, Any]:
    """Compute status purely from `episodes.json`, doc states, and `reported.json` without writing files."""
    fallback_run_id = default_report_run_id(today_date)
    lpath = ledger_path(root)
    if not lpath.is_symlink() and not lpath.exists():
        return {
            "ok": False,
            "error": "ledger_missing",
            "reason": "ledger_missing",
            "message": "台账文件 state/podcast-weekly/episodes.json 不存在；先执行主机流水线任务并核查状态。",
            "report_run_id": fallback_run_id,
        }

    try:
        ledger = read_json_dict(lpath, required=True)
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "ledger_missing",
            "reason": "ledger_missing",
            "message": "台账文件 state/podcast-weekly/episodes.json 不存在；先执行主机流水线任务并核查状态。",
            "report_run_id": fallback_run_id,
        }
    except (RecordUnreadableError, ValueError, PodcastStoreSecurityError) as exc:
        return {
            "ok": False,
            "error": "ledger_unreadable",
            "reason": "ledger_unreadable",
            "detail": str(exc),
            "report_run_id": fallback_run_id,
        }

    try:
        rep_success, rep_failed, rep_empty, rep_abandoned = _load_reported_unlocked(root)
    except (RecordUnreadableError, ValueError, PodcastStoreSecurityError) as exc:
        return {
            "ok": False,
            "error": "reported_record_unreadable",
            "reason": "reported_record_unreadable",
            "detail": str(exc),
            "report_run_id": fallback_run_id,
        }

    try:
        pending_report = _load_pending_report_unlocked(root)
    except (RecordUnreadableError, ValueError, PodcastStoreSecurityError) as exc:
        return {
            "ok": False,
            "error": "pending_report_unreadable",
            "reason": "pending_report_unreadable",
            "detail": str(exc),
            "report_run_id": fallback_run_id,
        }

    ready: list[dict[str, Any]] = []
    incomplete_docs: list[dict[str, Any]] = []
    stage_incomplete: list[dict[str, Any]] = []
    published: list[dict[str, Any]] = []

    for raw_vid, row in ledger.items():
        if not isinstance(row, dict):
            continue
        vid = validate_video_id(raw_vid)
        dpath = doc_record_path(root, vid)
        fpath = doc_failed_path(root, vid)
        try:
            doc_rec = read_json_dict(dpath)
            failed_doc = read_json_dict(fpath)
        except (RecordUnreadableError, ValueError) as exc:
            return {
                "ok": False,
                "error": "doc_record_unreadable",
                "reason": "doc_record_unreadable",
                "video_id": vid,
                "detail": str(exc),
                "report_run_id": fallback_run_id,
            }

        bucket = _classify_episode_bucket(row, doc_rec, failed_doc)
        is_doc_done = bucket == "published"
        doc_url = str(doc_rec.get("doc_url") or row.get("doc_url") or "").strip()
        doc_failed_attempts = int(failed_doc.get("attempts") or 0)
        stage_error = str(row.get("stage_error") or "")
        if not stage_error and doc_failed_attempts >= 2:
            stage_error = str(failed_doc.get("last_error") or "doc_create_failed_twice")
        item: dict[str, Any] = {
            "video_id": vid,
            "title": row.get("title") or "",
            "channel": row.get("channel") or "",
            "upload_date": row.get("upload_date") or "",
            "stage": row.get("stage") or "",
            "stage_error": stage_error,
            "doc_failed_attempts": doc_failed_attempts,
            "notes_path": row.get("notes_path") or "",
            "doc_url": doc_url,
            "document_id": str(doc_rec.get("document_id") or ""),
            "body_status": str(doc_rec.get("body_status") or ("complete" if is_doc_done else "")),
            "completed_parts": list(doc_rec.get("completed_parts") or []),
            "total_parts": doc_rec.get("total_parts"),
            "published_at": row.get("published_at") or doc_rec.get("published_at") or "",
        }
        if bucket == "published":
            published.append(item)
        elif bucket == "incomplete_docs":
            incomplete_docs.append(item)
        elif bucket == "ready":
            ready.append(item)
        else:
            stage_incomplete.append(item)

    to_publish = [*incomplete_docs, *ready]
    report_success = [
        {**it, "recovered": it["video_id"] in rep_failed}
        for it in published
        if it["video_id"] not in rep_success
    ]
    report_failed = [
        dict(it)
        for it in stage_incomplete
        if it["video_id"] not in rep_failed and it["video_id"] not in rep_success
    ]
    already_reported_today = _has_report_on_date(today_date, rep_success, rep_failed, rep_empty)
    if pending_report is not None:
        next_action = "resubmit_pending"
        report_run_id = pending_report["report_run_id"]
    else:
        report_run_id = _compute_report_run_id(
            today_date,
            [it["video_id"] for it in report_success],
            [it["video_id"] for it in report_failed],
            abandoned_ids=set(rep_abandoned.keys()),
        )
        if to_publish:
            next_action = "publish"
        elif report_success or report_failed:
            next_action = "submit"
        elif not already_reported_today:
            next_action = "empty_week"
        else:
            next_action = "nothing_to_report"

    result: dict[str, Any] = {
        "ok": True,
        "date": today_date,
        "next_action": next_action,
        "report_run_id": report_run_id,
        "pending_report": pending_report,
        "to_publish": to_publish,
        "report_success": report_success,
        "report_failed": report_failed,
        "ready": ready,
        "incomplete_docs": incomplete_docs,
        "stage_incomplete": stage_incomplete,
        "published": [] if pending_only else published,
        "empty_week": next_action == "empty_week",
    }
    if next_action == "empty_week":
        result["empty_message"] = EMPTY_WEEK_MESSAGE
    return result


def get_status(
    root: Path,
    *,
    date_str: str | None = None,
    pending_only: bool = False,
) -> dict[str, Any]:
    """Read `episodes.json`, `<vid>/doc.json`, and `reported.json` cleanly under state lock."""
    today_date = _resolve_hk_date(date_str)
    fallback_run_id = default_report_run_id(today_date)
    lpath = ledger_path(root)
    if not lpath.is_symlink() and not lpath.exists():
        return {
            "ok": False,
            "error": "ledger_missing",
            "reason": "ledger_missing",
            "message": "台账文件 state/podcast-weekly/episodes.json 不存在；先执行主机流水线任务并核查状态。",
            "report_run_id": fallback_run_id,
        }
    try:
        with podcast_state_lock(root):
            return _compute_status_unlocked(root, today_date=today_date, pending_only=pending_only)
    except (PodcastStoreSecurityError, RecordUnreadableError) as exc:
        return {
            "ok": False,
            "error": "ledger_unreadable",
            "reason": "ledger_unreadable",
            "detail": str(exc),
            "report_run_id": fallback_run_id,
        }


def read_artifact(
    root: Path,
    vid: str,
    artifact: str,
    *,
    offset: int = 0,
    max_chars: int = 16000,
) -> dict[str, Any]:
    """Read a podcast episode artifact or `.doc.json` state with character pagination."""
    safe_vid = validate_video_id(vid)
    key = str(artifact or "").strip().lower()
    if max_chars < 1:
        max_chars = 16000
    max_chars = min(max_chars, 32000)
    if offset < 0:
        offset = 0

    if key == "doc_state":
        dpath = doc_record_path(root, safe_vid)
        rec = read_json_dict(dpath)
        fpath = doc_failed_path(root, safe_vid)
        frec = read_json_dict(fpath) if fpath.exists() else {}
        return {
            "ok": True,
            "video_id": safe_vid,
            "artifact": "doc_state",
            "exists": bool(rec),
            "doc_state": rec,
            "failed_state": frec,
        }

    ledger: dict[str, Any] = {}
    lpath = ledger_path(root)
    if lpath.exists():
        ledger = read_json_dict(lpath)
    row = ledger.get(safe_vid) if isinstance(ledger.get(safe_vid), dict) else {}
    target = resolve_artifact_path(root, safe_vid, key, row=row, for_write=False)
    full_text = _read_text_nofollow(target)
    digest = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
    total_chars = len(full_text)
    chunk = full_text[offset : offset + max_chars]
    next_offset = offset + len(chunk)
    return {
        "ok": True,
        "video_id": safe_vid,
        "artifact": key,
        "filename": target.name,
        "total_chars": total_chars,
        "offset": offset,
        "returned_chars": len(chunk),
        "next_offset": next_offset if next_offset < total_chars else None,
        "has_more": next_offset < total_chars,
        "content_sha256": digest,
        "content": chunk,
    }


def write_artifact(
    root: Path,
    vid: str,
    artifact: str,
    content: str,
) -> dict[str, Any]:
    """Write proofread `.zh.md`, `.en.md`, `.notes.json`, or `.summary.md` back to `research-outputs/podcast-weekly`."""
    safe_vid = validate_video_id(vid)
    key = str(artifact or "").strip().lower()
    if key not in WRITABLE_ARTIFACTS:
        allowed = ", ".join(sorted(WRITABLE_ARTIFACTS))
        return {
            "ok": False,
            "error": "artifact_not_writable",
            "message": f"仅允许写入 {allowed}，收到 {artifact!r}",
        }
    if not isinstance(content, str) or not content.strip():
        return {"ok": False, "error": "empty_content", "message": "写入内容不能为空"}

    lpath = ledger_path(root)
    ledger = read_json_dict(lpath) if lpath.exists() else {}
    row = ledger.get(safe_vid) if isinstance(ledger.get(safe_vid), dict) else {}
    target = resolve_artifact_path(root, safe_vid, key, row=row, for_write=True)

    if key == "notes":
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return {"ok": False, "error": "invalid_notes_json", "message": "notes 必须是 JSON 对象"}
        if parsed.get("video_id") and parsed.get("video_id") != safe_vid:
            return {
                "ok": False,
                "error": "video_id_mismatch",
                "message": "notes.json 的 video_id 与目标不一致",
            }
        atomic_write_json(target, parsed)
    else:
        text = content if content.endswith("\n") else content + "\n"
        atomic_write_text(target, text)

    return {
        "ok": True,
        "video_id": safe_vid,
        "artifact": key,
        "filename": target.name,
        "bytes": target.stat().st_size,
    }


def _chunk_manifest_path(root: Path, vid: str) -> Path:
    return _safe_child_in_dir(_doc_chunks_dir(root, vid), "manifest.json")


def _persisted_total_parts_unlocked(root: Path, vid: str) -> int:
    mpath = _chunk_manifest_path(root, vid)
    if not mpath.exists():
        return 0
    manifest = read_json_dict(mpath)
    return max(0, int(manifest.get("total_parts") or len(manifest.get("parts") or []) or 0))


def _persist_chunks_unlocked(
    root: Path,
    vid: str,
    *,
    full_title: str,
    zh_title: str,
    title_source: str,
    chunks: list[dict[str, Any]],
) -> None:
    cdir = _doc_chunks_dir(root, vid)
    cdir.mkdir(parents=True, exist_ok=True)
    parts_meta: list[dict[str, Any]] = []
    expected_files: set[str] = {"manifest.json"}
    for item in chunks:
        idx = int(item["part"])
        body = str(item["content"])
        digest = str(item.get("sha256") or hashlib.sha256(body.encode("utf-8")).hexdigest())
        item["sha256"] = digest
        fname = f"part-{idx:03d}.md"
        expected_files.add(fname)
        atomic_write_text(_safe_child_in_dir(cdir, fname), body)
        parts_meta.append(
            {
                "part": idx,
                "total": len(chunks),
                "chars": len(body),
                "sha256": digest,
                "file": fname,
            }
        )
    # Remove stale chunk files when `redo=True` shrinks the part count.
    for old_entry in cdir.iterdir():
        if old_entry.is_symlink():
            raise PodcastStoreSecurityError(f"拒绝软链接分段文件: {old_entry}")
        if old_entry.name.startswith("part-") and old_entry.name.endswith(".md") and old_entry.name not in expected_files:
            old_entry.unlink()
    manifest = {
        "video_id": vid,
        "title": full_title,
        "zh_title": zh_title,
        "title_source": title_source,
        "total_parts": len(chunks),
        "sections": ["内容提要", "中文全文", "校正说明", "英文校订稿"],
        "created_at": datetime.now(HK_TZ).isoformat(),
        "parts": parts_meta,
    }
    atomic_write_json(_safe_child_in_dir(cdir, "manifest.json"), manifest)


def _load_persisted_chunks_unlocked(root: Path, vid: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cdir = _doc_chunks_dir(root, vid)
    mpath = _safe_child_in_dir(cdir, "manifest.json")
    manifest = read_json_dict(mpath, required=True)
    raw_parts = manifest.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise RecordUnreadableError(f"分段清单为空或格式非法: {mpath}")
    total_parts = int(manifest.get("total_parts") or len(raw_parts))
    if total_parts != len(raw_parts):
        raise RecordUnreadableError(f"分段清单数量不一致: {mpath}")
    chunks: list[dict[str, Any]] = []
    for idx, entry in enumerate(sorted(raw_parts, key=lambda x: int(x.get("part") or 0)), start=1):
        if not isinstance(entry, dict) or int(entry.get("part") or 0) != idx:
            raise RecordUnreadableError(f"分段序号不连续: {mpath}")
        fname = str(entry.get("file") or f"part-{idx:03d}.md")
        expected_sha = str(entry.get("sha256") or "")
        part_path = _safe_child_in_dir(cdir, fname)
        if not part_path.exists():
            raise RecordUnreadableError(f"缺少持久化分段文件: {part_path}")
        body = _read_text_nofollow(part_path)
        actual_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if not expected_sha or actual_sha != expected_sha:
            raise RecordUnreadableError(f"持久化分段校验和不一致: {part_path}")
        chunks.append(
            {
                "part": idx,
                "total": total_parts,
                "chars": len(body),
                "sha256": actual_sha,
                "content": body,
            }
        )
    return manifest, chunks


def prepare_doc(
    root: Path,
    vid: str,
    *,
    max_chars: int = 15000,
    date_str: str | None = None,
    channels_path: Path = DEFAULT_CHANNELS_PATH,
    summary_file: str | None = None,
    redo: bool = False,
) -> dict[str, Any]:
    """Assemble all 4 Feishu doc sections, persist chunks on first prepare, and resume strictly from persisted chunks."""
    safe_vid = validate_video_id(vid)
    lpath = ledger_path(root)
    if not lpath.is_symlink() and not lpath.exists():
        return {
            "ok": False,
            "create": False,
            "resume_append": False,
            "video_id": safe_vid,
            "reason": "ledger_missing",
            "error": "ledger_missing",
            "message": "台账文件 state/podcast-weekly/episodes.json 不存在；先执行主机流水线任务并核查状态。",
        }

    with podcast_state_lock(root):
        try:
            ledger = read_json_dict(lpath, required=True)
        except FileNotFoundError:
            return {
                "ok": False,
                "create": False,
                "resume_append": False,
                "video_id": safe_vid,
                "reason": "ledger_missing",
                "error": "ledger_missing",
                "message": "台账文件 state/podcast-weekly/episodes.json 不存在；先执行主机流水线任务并核查状态。",
            }
        except (RecordUnreadableError, ValueError, PodcastStoreSecurityError) as exc:
            return {
                "ok": False,
                "create": False,
                "resume_append": False,
                "video_id": safe_vid,
                "reason": "doc_record_unreadable",
                "error": str(exc),
            }

        if safe_vid not in ledger or not isinstance(ledger[safe_vid], dict):
            return {
                "ok": False,
                "create": False,
                "resume_append": False,
                "video_id": safe_vid,
                "reason": "episode_not_in_ledger",
                "error": f"{safe_vid} is missing from state/podcast-weekly/episodes.json",
            }
        row = ledger[safe_vid]

        dpath = doc_record_path(root, safe_vid)
        try:
            existing_doc = read_json_dict(dpath)
        except (RecordUnreadableError, ValueError) as exc:
            return {
                "ok": False,
                "create": False,
                "resume_append": False,
                "error": "doc_record_unreadable",
                "reason": "doc_record_unreadable",
                "video_id": safe_vid,
                "detail": str(exc),
            }

        if not redo and existing_doc and _doc_is_complete(existing_doc):
            return {
                "ok": True,
                "already_exists": True,
                "create": False,
                "resume_append": False,
                "reason": "already_exists",
                "video_id": safe_vid,
                "document_id": existing_doc.get("document_id", ""),
                "doc_url": existing_doc["doc_url"],
                "zh_title": existing_doc.get("zh_title", ""),
            }

        if not redo and not existing_doc and row.get("published_at") and row.get("doc_url"):
            return {
                "ok": True,
                "already_exists": True,
                "create": False,
                "resume_append": False,
                "reason": "already_exists",
                "video_id": safe_vid,
                "document_id": str(row.get("doc_id") or ""),
                "doc_url": str(row["doc_url"]),
                "zh_title": str(row.get("zh_title") or ""),
            }

        fpath = doc_failed_path(root, safe_vid)
        try:
            failed_doc = read_json_dict(fpath)
        except (RecordUnreadableError, ValueError) as exc:
            return {
                "ok": False,
                "create": False,
                "resume_append": False,
                "error": "doc_failed_record_unreadable",
                "reason": "doc_failed_record_unreadable",
                "video_id": safe_vid,
                "detail": str(exc),
            }
        if not existing_doc and int(failed_doc.get("attempts") or 0) >= 2:
            return {
                "ok": False,
                "create": False,
                "resume_append": False,
                "reason": "doc_create_failed_twice",
                "video_id": safe_vid,
                "attempts": failed_doc.get("attempts"),
                "last_error": failed_doc.get("last_error", ""),
            }

        mpath = _chunk_manifest_path(root, safe_vid)
        if not redo and mpath.exists():
            try:
                manifest, chunks = _load_persisted_chunks_unlocked(root, safe_vid)
            except (RecordUnreadableError, ValueError, PodcastStoreSecurityError) as exc:
                return {
                    "ok": False,
                    "create": False,
                    "resume_append": False,
                    "error": "chunks_record_unreadable",
                    "reason": "chunks_record_unreadable",
                    "video_id": safe_vid,
                    "detail": str(exc),
                }
            full_title = str(manifest.get("title") or "")
            zh_title = str(manifest.get("zh_title") or "")
            title_source = str(manifest.get("title_source") or "persisted.manifest")
            markdown = "\n\n".join(c["content"] for c in chunks)
        else:
            notes_path = resolve_artifact_path(root, safe_vid, "notes", row=row, for_write=False)
            notes = read_json_dict(notes_path)
            zh_path = resolve_artifact_path(root, safe_vid, "zh", row=row, for_write=False)
            en_path = resolve_artifact_path(root, safe_vid, "en", row=row, for_write=False)
            if summary_file:
                summary_path = safe_podcast_path(root, summary_file)
            else:
                summary_path = resolve_artifact_path(root, safe_vid, "summary", row=row, for_write=False)

            zh_text = _read_text_nofollow(zh_path).strip()
            en_text = _read_text_nofollow(en_path).strip()
            summary_raw = _read_text_nofollow(summary_path)
            first_line, _, rest_body = summary_raw.partition("\n")
            if first_line.startswith("# ") and first_line[2:].strip():
                zh_title = first_line[2:].strip()
                summary_body = rest_body.strip()
                title_source = "summary.header"
            elif str(notes.get("zh_title") or "").strip():
                zh_title = str(notes["zh_title"]).strip()
                summary_body = summary_raw.strip()
                title_source = "notes.zh_title"
            else:
                raise ValueError(f"{summary_path} 第一行必须是 '# 中文标题'，或 notes.json 提供 zh_title")
            if not summary_body:
                raise ValueError(f"内容提要为空: {summary_path}")

            fixes_md = "\n".join(_build_fixes_lines(notes))
            channel = str(notes.get("channel") or row.get("channel") or "").strip()
            raw_date = str(notes.get("upload_date") or row.get("upload_date") or "")
            upload_date = format_upload_date(raw_date)
            full_title = f"【{channel}】{zh_title}（{upload_date}）" if channel else f"{zh_title}（{upload_date}）"
            source_url = str(notes.get("url") or "")
            duration_min = notes.get("duration_min") or 0

            markdown = "\n".join(
                [
                    f"原标题：{notes.get('title') or row.get('title') or ''}",
                    "",
                    "## 内容提要",
                    "",
                    summary_body,
                    "",
                    (
                        f"原视频：{source_url}　时长约 {duration_min} 分钟。"
                        "逐字稿来自语音识别，说话人由模型看原视频判定，中文为机器翻译加校对，引用前请回看视频对应时间。"
                    ),
                    "",
                    "---",
                    "",
                    "## 中文全文",
                    "",
                    zh_text,
                    "",
                    "---",
                    "",
                    "## 校正说明",
                    "",
                    fixes_md,
                    "",
                    "---",
                    "",
                    "## 英文校订稿",
                    "",
                    en_text,
                    "",
                ]
            )
            raw_chunks = split_markdown_for_feishu(markdown, max(1000, int(max_chars)))
            chunks = [
                {
                    "part": idx,
                    "total": len(raw_chunks),
                    "chars": len(body),
                    "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    "content": body,
                }
                for idx, body in enumerate(raw_chunks, start=1)
            ]
            _persist_chunks_unlocked(
                root,
                safe_vid,
                full_title=full_title,
                zh_title=zh_title,
                title_source=title_source,
                chunks=chunks,
            )

        if not redo and existing_doc and (existing_doc.get("document_id") or existing_doc.get("doc_url")):
            completed = {
                int(p)
                for p in (existing_doc.get("completed_parts") or [])
                if isinstance(p, int) or (isinstance(p, str) and p.isdigit())
            }
            remaining = [c for c in chunks if c["part"] not in completed]
            return {
                "ok": True,
                "already_exists": False,
                "create": False,
                "resume_append": True,
                "reason": "resume_incomplete_doc",
                "video_id": safe_vid,
                "document_id": str(existing_doc.get("document_id") or ""),
                "doc_url": str(existing_doc.get("doc_url") or ""),
                "body_status": "incomplete",
                "folder_token": channels_folder_token(channels_path),
                "title": full_title,
                "zh_title": zh_title,
                "title_source": title_source,
                "markdown": markdown,
                "sections": ["内容提要", "中文全文", "校正说明", "英文校订稿"],
                "total_parts": len(chunks),
                "completed_parts": sorted(completed),
                "chunks": chunks,
                "remaining_chunks": remaining,
            }

        return {
            "ok": True,
            "already_exists": False,
            "create": True,
            "resume_append": False,
            "video_id": safe_vid,
            "folder_token": channels_folder_token(channels_path),
            "title": full_title,
            "zh_title": zh_title,
            "title_source": title_source,
            "markdown": markdown,
            "sections": ["内容提要", "中文全文", "校正说明", "英文校订稿"],
            "total_parts": len(chunks),
            "chunks": chunks,
            "remaining_chunks": chunks,
        }


def _promote_published_in_ledger_unlocked(
    root: Path,
    vid: str,
    doc_url: str,
    published_at: str,
) -> None:
    lpath = ledger_path(root)
    ledger = read_json_dict(lpath) if lpath.exists() else {}
    if vid in ledger and isinstance(ledger[vid], dict):
        ledger[vid]["doc_url"] = doc_url
        ledger[vid]["published_at"] = published_at
        atomic_write_json(lpath, ledger)


def _save_doc_unlocked(
    root: Path,
    vid: str,
    *,
    doc_url: str,
    document_id: str = "",
    zh_title: str = "",
    total_parts: int = 1,
    completed_parts: list[int] | None = None,
    body_complete: bool | None = None,
) -> dict[str, Any]:
    safe_vid = validate_video_id(vid)
    clean_url = str(doc_url or "").strip()
    clean_doc_id = str(document_id or "").strip()
    if not clean_url and not clean_doc_id:
        raise ValueError("必须提供有效的 doc_url 或 document_id")

    dpath = doc_record_path(root, safe_vid)
    existing = read_json_dict(dpath) if dpath.exists() else {}

    persisted_total = _persisted_total_parts_unlocked(root, safe_vid)
    existing_total = int(existing.get("total_parts") or 0)
    req_total = int(total_parts or existing_total or 1)
    # When persisted chunks exist, total_parts is strictly authoritative from manifest.json;
    # discard any out-of-range parts from stale records or caller args.
    n_total = persisted_total if persisted_total > 0 else max(1, req_total, existing_total)

    prev_parts = {
        int(p)
        for p in (existing.get("completed_parts") or [])
        if str(p).isdigit() and 1 <= int(p) <= n_total
    }
    req_parts = (
        {
            int(p)
            for p in completed_parts
            if str(p).isdigit() and 1 <= int(p) <= n_total
        }
        if completed_parts is not None
        else set()
    )
    new_parts = prev_parts | req_parts

    all_parts_present = all(p in new_parts for p in range(1, n_total + 1))
    if body_complete is None:
        is_complete = n_total <= 1 or all_parts_present
    else:
        is_complete = bool(body_complete) and (
            n_total <= 1 or all_parts_present or (completed_parts is None and persisted_total <= 1)
        )

    now = datetime.now(HK_TZ).isoformat()
    body_status = "complete" if is_complete else "incomplete"
    sorted_parts = sorted(new_parts)
    if is_complete and not sorted_parts:
        sorted_parts = list(range(1, n_total + 1))

    missing = [p for p in range(1, n_total + 1) if p not in set(sorted_parts)]
    rec: dict[str, Any] = {
        "video_id": safe_vid,
        "document_id": clean_doc_id or str(existing.get("document_id") or ""),
        "doc_url": clean_url or str(existing.get("doc_url") or ""),
        "zh_title": zh_title or str(existing.get("zh_title") or ""),
        "body_status": body_status,
        "total_parts": n_total,
        "completed_parts": sorted_parts,
        "next_part": missing[0] if missing else None,
        "updated_at": now,
    }
    if is_complete:
        rec["published_at"] = str(existing.get("published_at") or now)

    atomic_write_json(dpath, rec)

    if is_complete and rec["doc_url"]:
        _promote_published_in_ledger_unlocked(root, safe_vid, rec["doc_url"], rec["published_at"])

    return {"ok": True, **rec}


def save_doc(
    root: Path,
    vid: str,
    *,
    doc_url: str,
    document_id: str = "",
    zh_title: str = "",
    total_parts: int = 1,
    completed_parts: list[int] | None = None,
    body_complete: bool | None = None,
) -> dict[str, Any]:
    """Persist Feishu `document_id` + `doc_url` under unified state lock."""
    with podcast_state_lock(root):
        return _save_doc_unlocked(
            root,
            vid,
            doc_url=doc_url,
            document_id=document_id,
            zh_title=zh_title,
            total_parts=total_parts,
            completed_parts=completed_parts,
            body_complete=body_complete,
        )


def record_chunk(
    root: Path,
    vid: str,
    *,
    part: int,
    total_parts: int,
    document_id: str = "",
    doc_url: str = "",
) -> dict[str, Any]:
    """Record one appended Feishu chunk under unified state lock and promote `complete` only when all locked parts finish."""
    safe_vid = validate_video_id(vid)
    with podcast_state_lock(root):
        dpath = doc_record_path(root, safe_vid)
        existing = read_json_dict(dpath)
        if not existing and not doc_url and not document_id:
            raise FileNotFoundError(f"缺少 {safe_vid}.doc.json，需先 save_doc 登记 document_id/doc_url")
        persisted_total = _persisted_total_parts_unlocked(root, safe_vid)
        existing_total = int(existing.get("total_parts") or 0)
        n_total = persisted_total if persisted_total > 0 else max(1, int(total_parts or 0), existing_total)
        part_num = int(part)
        if part_num < 1 or part_num > n_total:
            raise ValueError(f"part 超出范围: {part_num} (1..{n_total})")
        merged_parts = sorted(
            {
                *(
                    int(p)
                    for p in (existing.get("completed_parts") or [])
                    if str(p).isdigit() and 1 <= int(p) <= n_total
                ),
                part_num,
            }
        )
        all_done = all(p in set(merged_parts) for p in range(1, n_total + 1))
        return _save_doc_unlocked(
            root,
            safe_vid,
            doc_url=doc_url or str(existing.get("doc_url") or ""),
            document_id=document_id or str(existing.get("document_id") or ""),
            zh_title=str(existing.get("zh_title") or ""),
            total_parts=n_total,
            completed_parts=merged_parts,
            body_complete=all_done,
        )


def record_failure(root: Path, vid: str, error: str) -> dict[str, Any]:
    safe_vid = validate_video_id(vid)
    with podcast_state_lock(root):
        fpath = doc_failed_path(root, safe_vid)
        prev = read_json_dict(fpath) if fpath.exists() else {}
        attempts = int(prev.get("attempts") or 0) + 1
        stopped = attempts >= 2
        rec = {
            "video_id": safe_vid,
            "attempts": attempts,
            "last_error": str(error or "")[:500],
            "stopped": stopped,
            "updated_at": datetime.now(HK_TZ).isoformat(),
        }
        atomic_write_json(fpath, rec)
        return {
            "ok": True,
            "reason": "doc_create_failed_twice" if stopped else "doc_create_failed",
            **rec,
        }


def _extract_date_from_run_id(report_run_id: str) -> str | None:
    matched = re.match(r"^podcast-weekly-(\d{4}-\d{2}-\d{2})(?:-|$)", report_run_id)
    return matched.group(1) if matched else None


def save_pending_report(
    root: Path,
    report_run_id: str,
    text: str,
    success_ids: list[str] | None = None,
    failed_ids: list[str] | None = None,
    *,
    empty_date: str | None = None,
    date_str: str | None = None,
) -> dict[str, Any]:
    """Persist pending summary payload into `state/podcast-weekly/pending_report.json` under state lock."""
    safe_run = validate_report_run_id(report_run_id)
    if not isinstance(text, str) or not text.strip():
        return {
            "ok": False,
            "error": "empty_text",
            "reason": "empty_text",
            "message": "待发送汇总正文不能为空。",
        }
    clean_empty_date = (
        _resolve_hk_date(empty_date) if empty_date is not None and str(empty_date).strip() else None
    )
    clean_req_success = list(dict.fromkeys(validate_video_id(str(v)) for v in (success_ids or [])))
    clean_req_failed = list(dict.fromkeys(validate_video_id(str(v)) for v in (failed_ids or [])))
    calc_date = (
        _resolve_hk_date(date_str)
        if date_str is not None and str(date_str).strip()
        else (clean_empty_date or _extract_date_from_run_id(safe_run) or _resolve_hk_date(None))
    )

    with podcast_state_lock(root):
        try:
            existing_pending = _load_pending_report_unlocked(root)
        except (RecordUnreadableError, ValueError, PodcastStoreSecurityError) as exc:
            return {
                "ok": False,
                "error": "pending_report_unreadable",
                "reason": "pending_report_unreadable",
                "detail": str(exc),
            }
        if existing_pending is not None:
            return {
                "ok": False,
                "error": "pending_report_exists",
                "reason": "pending_report_exists",
                "message": "已存在待确认报告 state/podcast-weekly/pending_report.json，需先完成重提与登记。",
                "pending_report": existing_pending,
            }

        status = _compute_status_unlocked(root, today_date=calc_date, pending_only=False)
        if not status.get("ok"):
            return status

        expected_run_id = str(status.get("report_run_id") or "")
        if safe_run != expected_run_id:
            return {
                "ok": False,
                "error": "report_run_id_mismatch",
                "reason": "report_run_id_mismatch",
                "message": f"report_run_id 与当前 status 计算不一致: expected={expected_run_id}, got={safe_run}",
                "expected_report_run_id": expected_run_id,
            }

        allowed_success = {it["video_id"] for it in (status.get("report_success") or [])}
        allowed_failed = {it["video_id"] for it in (status.get("report_failed") or [])}
        next_action = status.get("next_action")

        if next_action == "empty_week":
            if clean_req_success or clean_req_failed or clean_empty_date != status.get("date"):
                return {
                    "ok": False,
                    "error": "invalid_empty_week_params",
                    "reason": "invalid_empty_week_params",
                    "message": "空周待确认报告要求 success_ids/failed_ids 为空且 empty_date 等于本次 status 日期。",
                }
        elif allowed_success or allowed_failed:
            bad_success = [v for v in clean_req_success if v not in allowed_success]
            bad_failed = [v for v in clean_req_failed if v not in allowed_failed]
            if clean_empty_date is not None or bad_success or bad_failed or (not clean_req_success and not clean_req_failed):
                return {
                    "ok": False,
                    "error": "invalid_video_ids",
                    "reason": "invalid_video_ids",
                    "message": "success_video_ids/failed_video_ids 必须是本次 report_success/report_failed 的有效子集。",
                    "invalid_success_ids": bad_success,
                    "invalid_failed_ids": bad_failed,
                }
        else:
            return {
                "ok": False,
                "error": "nothing_to_report",
                "reason": "nothing_to_report",
                "message": "当前无可提交的汇总或空周内容。",
            }

        pending_payload: dict[str, Any] = {
            "report_run_id": safe_run,
            "text": text,
            "success_video_ids": clean_req_success,
            "failed_video_ids": clean_req_failed,
            "empty_date": clean_empty_date,
            "created_at": datetime.now(HK_TZ).isoformat(),
        }
        atomic_write_json(pending_report_path(root), pending_payload)
        return {
            "ok": True,
            "pending_report": pending_payload,
        }


def abandon_pending_report(
    root: Path,
    report_run_id: str,
    *,
    date_str: str | None = None,
) -> dict[str, Any]:
    """Move an expired prior-day `pending_report.json` to `reported.json.abandoned` without marking episodes reported."""
    safe_run = validate_report_run_id(report_run_id)
    today_date = _resolve_hk_date(date_str)

    with podcast_state_lock(root):
        try:
            pending = _load_pending_report_unlocked(root)
        except (RecordUnreadableError, ValueError, PodcastStoreSecurityError) as exc:
            return {
                "ok": False,
                "error": "pending_report_unreadable",
                "reason": "pending_report_unreadable",
                "detail": str(exc),
            }
        if pending is None:
            return {
                "ok": False,
                "error": "pending_report_missing",
                "reason": "pending_report_missing",
                "message": "当前没有待确认报告可放弃。",
            }
        if pending["report_run_id"] != safe_run:
            return {
                "ok": False,
                "error": "report_run_id_mismatch",
                "reason": "report_run_id_mismatch",
                "message": f"待确认报告编号不一致: expected={pending['report_run_id']}, got={safe_run}",
                "expected_report_run_id": pending["report_run_id"],
            }
        created_hk_date = _hk_date_from_timestamp(str(pending.get("created_at") or ""))
        if not created_hk_date:
            return {
                "ok": False,
                "error": "invalid_created_at",
                "reason": "invalid_created_at",
                "message": "pending_report.json 缺少有效 created_at 时间戳。",
            }
        if created_hk_date >= today_date:
            return {
                "ok": False,
                "error": "pending_report_not_expired",
                "reason": "pending_report_not_expired",
                "message": f"待确认报告创建于当天（{created_hk_date}），当日保留原待确认状态。",
                "created_date": created_hk_date,
                "today_date": today_date,
            }

        rep_success, rep_failed, rep_empty, rep_abandoned = _load_reported_unlocked(root)
        now = datetime.now(HK_TZ).isoformat()
        abandoned_entry = {
            "abandoned_at": now,
            "created_at": str(pending.get("created_at") or ""),
            "success_video_ids": list(pending.get("success_video_ids") or []),
            "failed_video_ids": list(pending.get("failed_video_ids") or []),
            "empty_date": pending.get("empty_date"),
        }
        rep_abandoned[safe_run] = abandoned_entry
        reported_payload = {
            "success": rep_success,
            "failed": rep_failed,
            "empty_weeks": rep_empty,
            "abandoned": rep_abandoned,
        }
        atomic_write_json(reported_path(root), reported_payload)

        ppath = pending_report_path(root)
        if ppath.exists() and not ppath.is_symlink():
            ppath.unlink()

        return {
            "ok": True,
            "report_run_id": safe_run,
            "abandoned": abandoned_entry,
            "deleted_pending_report": True,
            "reported": reported_payload,
        }


def mark_reported(
    root: Path,
    report_run_id: str,
    success_ids: list[str] | None = None,
    failed_ids: list[str] | None = None,
    *,
    empty_date: str | None = None,
) -> dict[str, Any]:
    """Persist reported episode IDs or empty week into `state/podcast-weekly/reported.json` under state lock."""
    safe_run = validate_report_run_id(report_run_id)
    clean_empty_date = _resolve_hk_date(empty_date) if empty_date is not None else None
    calc_date = clean_empty_date or _extract_date_from_run_id(safe_run) or _resolve_hk_date(None)
    clean_req_success = list(dict.fromkeys(validate_video_id(v) for v in (success_ids or [])))
    clean_req_failed = list(dict.fromkeys(validate_video_id(v) for v in (failed_ids or [])))

    with podcast_state_lock(root):
        status = _compute_status_unlocked(root, today_date=calc_date, pending_only=False)
        if not status.get("ok"):
            return status

        pending = status.get("pending_report")
        pending_matches = isinstance(pending, dict) and pending.get("report_run_id") == safe_run

        allowed_success = {it["video_id"] for it in (status.get("report_success") or [])}
        allowed_failed = {it["video_id"] for it in (status.get("report_failed") or [])}
        if pending_matches and isinstance(pending, dict):
            allowed_success |= {str(v) for v in (pending.get("success_video_ids") or [])}
            allowed_failed |= {str(v) for v in (pending.get("failed_video_ids") or [])}

        rep_success, rep_failed, rep_empty, rep_abandoned = _load_reported_unlocked(root)
        now = datetime.now(HK_TZ).isoformat()

        recorded_success: list[str] = []
        recorded_failed: list[str] = []
        ignored: list[str] = []

        for vid in clean_req_success:
            if vid in allowed_success:
                if vid not in rep_success:
                    rep_success[vid] = {"reported_at": now, "report_run_id": safe_run}
                    recorded_success.append(vid)
            else:
                ignored.append(vid)

        for vid in clean_req_failed:
            if vid in allowed_failed:
                if vid not in rep_failed:
                    rep_failed[vid] = {"reported_at": now, "report_run_id": safe_run}
                    recorded_failed.append(vid)
            else:
                ignored.append(vid)

        recorded_empty_date: str | None = None
        if clean_empty_date is not None and clean_empty_date not in rep_empty:
            rep_empty[clean_empty_date] = {"reported_at": now, "report_run_id": safe_run}
            recorded_empty_date = clean_empty_date

        reported_payload = {
            "success": rep_success,
            "failed": rep_failed,
            "empty_weeks": rep_empty,
            "abandoned": rep_abandoned,
        }
        atomic_write_json(reported_path(root), reported_payload)

        deleted_pending_report = False
        if pending_matches:
            ppath = pending_report_path(root)
            if ppath.exists() and not ppath.is_symlink():
                ppath.unlink()
            deleted_pending_report = True

        return {
            "ok": True,
            "report_run_id": safe_run,
            "recorded_success": recorded_success,
            "recorded_failed": recorded_failed,
            "empty_date": recorded_empty_date or clean_empty_date,
            "ignored": list(dict.fromkeys(ignored)),
            "deleted_pending_report": deleted_pending_report,
            "reported": reported_payload,
        }
