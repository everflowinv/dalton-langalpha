#!/usr/bin/env python3
"""Local MSFT tracker review records and X-pool edits. Stdlib only."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MAX_NEW_AUTHORS = 3
THESIS_REL = "research/tracker/thesis.md"
POOL_REL = "research/tracker/x_pool.json"
AUTHORS_REL = "research/tracker/x_authors.json"
PROFILE_FIELDS = ("name", "bio", "followers", "rest_id")
SEND_STATUS = frozenset({"pending", "unknown", "failed", "sent", "not_required"})
CONTENT_KINDS = frozenset({"cards", "failure_text"})
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \| "
    r"(?P<run_id>\d{8}-\d{4}) \| material=(?P<material>[^|\s]+) \| "
    r"(?P<summary>.*)$"
)


class StateError(Exception):
    def __init__(self, status: str, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def _inside(workspace: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(workspace.resolve())
    except (OSError, ValueError):
        return False
    return True


def open_workspace(raw: str | Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    resolved = path.resolve()
    if ".openclaw" in path.parts or ".openclaw" in resolved.parts:
        raise StateError("openclaw_root", "拒绝读写旧目录")
    if not resolved.is_dir():
        raise StateError("workspace_not_directory", "工作区不是目录")
    return resolved


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None or ".." in run_id:
        raise StateError("bad_run_id", "run_id 只能是一段不含路径的字母、数字、点、下划线或连字符")
    return run_id


def ensure_relative(workspace: Path, raw: str) -> Path:
    relative = Path(raw)
    if not raw or relative.is_absolute() or ".." in relative.parts:
        raise StateError("path_escape", "路径必须留在工作区内")
    current = workspace
    for part in relative.parts:
        if part in ("", ".", ".."):
            raise StateError("path_escape", "路径必须留在工作区内")
        current = current / part
        if current.is_symlink() and not _inside(workspace, current):
            raise StateError("symlink_escape", "符号链接指向工作区外")
        if current.is_symlink():
            current = current.resolve()
            if not _inside(workspace, current):
                raise StateError("symlink_escape", "符号链接指向工作区外")
    if not _inside(workspace, current):
        raise StateError("path_escape", "路径必须留在工作区内")
    return current


def normalize_handle(handle: str) -> str:
    text = handle.strip().lstrip("@").strip()
    if not text or "/" in text or "\\" in text or ".." in text or any(char.isspace() for char in text):
        raise StateError("bad_handle", "handle 需要是不含路径的账号")
    return text.lower()


@contextmanager
def workspace_lock(workspace: Path) -> Iterator[None]:
    tracker = workspace / "research" / "tracker"
    tracker.mkdir(parents=True, exist_ok=True)
    lock_path = tracker / ".msft-collect.lock"
    if not _inside(workspace, lock_path):
        raise StateError("path_escape", "锁文件越出工作区")
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def atomic_write_text(workspace: Path, path: Path, text: str) -> None:
    if not _inside(workspace, path.parent):
        raise StateError("path_escape", "拒绝写入工作区外")
    if path.is_symlink():
        raise StateError("symlink_escape", "拒绝透过符号链接写入")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def _load_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if path.is_symlink():
        raise StateError("symlink_escape", f"拒绝透过符号链接读取：{path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError("state_unreadable", f"{path.name} 无法读取") from exc
    if not isinstance(data, dict):
        raise StateError("state_unreadable", f"{path.name} 必须是对象")
    return data


def _require_packet(workspace: Path, run_id: str) -> str:
    run_id = validate_run_id(run_id)
    packet = ensure_relative(workspace, f"research/tracker/{run_id}/packet.json")
    if packet.is_symlink() or not packet.is_file():
        raise StateError("packet_missing", "运行目录没有 packet.json")
    return run_id


def _moment(now: datetime | None) -> datetime:
    moment = now or datetime.now(ET)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ET)
    return moment.astimezone(ET).replace(microsecond=0)


def _one_line(value: str, status: str, message: str, limit: int = 500) -> str:
    text = value.strip()
    if not text or "\n" in text or len(text) > limit:
        raise StateError(status, message)
    return text


def _norm_list(items: Any) -> list[str]:
    if not isinstance(items, list):
        raise StateError("state_unreadable", "账号修改记录无法读取")
    found: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        try:
            norm = normalize_handle(item)
        except StateError:
            continue
        if norm not in found:
            found.append(norm)
    return found


def _usable(value: Any) -> bool:
    return value not in (None, "") and not isinstance(value, (dict, list, bool))


def _find_author(authors: dict[str, Any], norm: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for key, value in authors.items():
        if not isinstance(value, dict):
            continue
        names = [str(key)]
        handle = value.get("handle")
        if isinstance(handle, str):
            names.append(handle)
        norms: set[str] = set()
        for name in names:
            try:
                norms.add(normalize_handle(name))
            except StateError:
                continue
        if norm in norms:
            matches.append(value)
    if len(matches) == 1:
        return matches[0]
    return {}


def _pool_entry(pool: dict[str, Any], norm: str) -> tuple[str | None, dict[str, Any]]:
    matches: list[str] = []
    for key in pool:
        try:
            if normalize_handle(str(key)) == norm:
                matches.append(str(key))
        except StateError:
            continue
    if len(matches) > 1:
        raise StateError("x_handle_conflict", "账号池里有多个键对应同一作者")
    if not matches:
        return None, {}
    key = matches[0]
    entry = pool[key]
    if not isinstance(entry, dict):
        raise StateError("bad_pool", "账号记录必须是对象")
    return key, dict(entry)


def _apply_profile(entry: dict[str, Any], authors: dict[str, Any], norm: str) -> None:
    cache = _find_author(authors, norm)
    copied = False
    for field in PROFILE_FIELDS:
        if _usable(entry.get(field)):
            continue
        if _usable(cache.get(field)):
            entry[field] = cache[field]
            copied = True
    if not any(_usable(entry.get(field)) for field in PROFILE_FIELDS):
        entry["profile_status"] = "missing"
    elif copied:
        entry["profile_status"] = "cached"
    elif "profile_status" not in entry:
        entry["profile_status"] = "present"


def _merge_tickers(entry: dict[str, Any], incoming: list[str]) -> None:
    current = entry.get("tickers", [])
    if current is None:
        current = []
    if not isinstance(current, list) or any(not isinstance(item, str) for item in current):
        raise StateError("bad_pool", "tickers 必须是字符串列表")
    merged = list(current)
    seen = {item.upper() for item in merged}
    for item in incoming:
        if item.upper() not in seen:
            merged.append(item)
            seen.add(item.upper())
    if not any(item.upper() in {"MSFT", "ALL"} for item in merged):
        raise StateError("ticker_scope", "账号池只维护 MSFT 或 ALL")
    entry["tickers"] = merged


def _parse_tickers(raw: str) -> list[str]:
    items = [part.strip().upper() for part in raw.split(",") if part.strip()]
    if not items or any(re.fullmatch(r"[A-Z][A-Z0-9._-]{0,15}", item) is None for item in items):
        raise StateError("bad_tickers", "tickers 需要是股票代码")
    return items


def _load_changes(workspace: Path, run_id: str) -> dict[str, Any]:
    path = workspace / "research" / "tracker" / run_id / "x_pool_changes.json"
    raw = _load_object(path)
    if raw.get("run_id") not in (None, run_id) and raw:
        raise StateError("run_mismatch", "本轮账号记录属于另一个 run_id")
    changes = dict(raw)
    changes["run_id"] = run_id
    changes["added"] = _norm_list(raw.get("added", []))
    changes["removed"] = _norm_list(raw.get("removed", []))
    return changes


def _store_pool(workspace: Path, pool: dict[str, Any], changes: dict[str, Any], run_id: str) -> None:
    atomic_write_text(
        workspace,
        workspace / "research" / "tracker" / run_id / "x_pool_changes.json",
        _dump(changes),
    )
    atomic_write_text(workspace, workspace / POOL_REL, _dump(pool))


def add_author(
    workspace: Path | str,
    run_id: str,
    handle: str,
    tickers: str,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = open_workspace(workspace)
    run_id = _require_packet(root, run_id)
    norm = normalize_handle(handle)
    incoming = _parse_tickers(tickers)
    basis = _one_line(reason, "bad_reason", "研究依据需要一行非空文字")
    with workspace_lock(root):
        pool = _load_object(root / POOL_REL)
        authors = _load_object(root / AUTHORS_REL)
        changes = _load_changes(root, run_id)
        old_key, entry = _pool_entry(pool, norm)
        was_active = bool(entry) and entry.get("active", True) is not False
        added = list(changes["added"])
        counted = False
        if norm not in added and not was_active:
            if len(added) >= MAX_NEW_AUTHORS:
                raise StateError("author_cap", "本轮最多新增 3 位作者")
            added.append(norm)
            counted = True
        changes["added"] = added
        if not entry.get("added_at"):
            entry["added_at"] = _moment(now).date().isoformat()
        entry["active"] = True
        entry["reason"] = basis
        _merge_tickers(entry, incoming)
        _apply_profile(entry, authors, norm)
        updated = dict(pool)
        if old_key is not None and old_key != norm:
            updated.pop(old_key, None)
        updated[norm] = entry
        _store_pool(root, updated, changes, run_id)
    return {
        "ok": True,
        "handle": norm,
        "active": True,
        "counted": counted,
        "added_count": len(added),
        "profile_status": entry.get("profile_status"),
    }


def remove_author(
    workspace: Path | str,
    run_id: str,
    handle: str,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = open_workspace(workspace)
    run_id = _require_packet(root, run_id)
    norm = normalize_handle(handle)
    basis = _one_line(reason, "bad_reason", "移出原因需要一行非空文字")
    with workspace_lock(root):
        pool = _load_object(root / POOL_REL)
        changes = _load_changes(root, run_id)
        old_key, entry = _pool_entry(pool, norm)
        if old_key is None:
            raise StateError("author_missing", "账号池里没有这个作者")
        entry["active"] = False
        entry["removed_at"] = _moment(now).isoformat(timespec="seconds")
        entry["removed_reason"] = basis
        removed = list(changes["removed"])
        if norm not in removed:
            removed.append(norm)
        changes["removed"] = removed
        updated = dict(pool)
        if old_key != norm:
            updated.pop(old_key, None)
        updated[norm] = entry
        _store_pool(root, updated, changes, run_id)
    return {"ok": True, "handle": norm, "active": False, "removed_count": len(removed)}


def save_review(
    workspace: Path | str,
    run_id: str,
    *,
    material: str,
    summary: str,
    report_run_id: str,
    send_status: str,
    card_json: str | None = None,
    thesis_path: str = THESIS_REL,
    thesis_sha256: str | None = None,
    content_kind: str | None = None,
) -> dict[str, Any]:
    root = open_workspace(workspace)
    run_id = _require_packet(root, run_id)
    report_run_id = validate_run_id(report_run_id)
    if material not in {"yes", "no"}:
        raise StateError("bad_material", "material 只能是 yes 或 no")
    if send_status not in SEND_STATUS:
        raise StateError("bad_send_status", "send_status 不在允许的状态里")
    if content_kind is not None and content_kind not in CONTENT_KINDS:
        raise StateError("bad_content_kind", "content_kind 只能是 cards 或 failure_text")
    text = _one_line(summary, "bad_summary", "summary 需要一行非空文字")
    thesis = ensure_relative(root, thesis_path)
    if thesis.is_symlink() or not thesis.is_file():
        raise StateError("thesis_missing", "工作底稿不存在")
    digest = hashlib.sha256(thesis.read_bytes()).hexdigest()
    if thesis_sha256 is not None and thesis_sha256.lower() != digest:
        raise StateError("thesis_hash_mismatch", "底稿哈希与文件不一致")
    card_rel = None
    if card_json:
        ensure_relative(root, card_json)
        card_rel = card_json
    rel = f"research/tracker/{run_id}/review.json"
    target = root / rel
    with workspace_lock(root):
        if target.is_symlink():
            raise StateError("symlink_escape", "拒绝透过符号链接写入恢复记录")
        if target.exists():
            if not target.is_file():
                raise StateError("state_unreadable", "review.json 必须是文件")
            card_rel, content_kind, material = _merge_review_identity(
                _load_object(target),
                run_id=run_id,
                report_run_id=report_run_id,
                card_rel=card_rel,
                content_kind=content_kind,
                material=material,
                thesis_path=thesis_path,
            )
        record = {
            "run_id": run_id,
            "material": material,
            "summary": text,
            "thesis_path": thesis_path,
            "thesis_sha256": digest,
            "report_run_id": report_run_id,
            "card_json": card_rel,
            "send_status": send_status,
            "content_kind": content_kind,
        }
        atomic_write_text(root, target, _dump(record))
    return {"ok": True, "path": rel, "review": record}


def _merge_review_identity(
    existing: dict[str, Any],
    *,
    run_id: str,
    report_run_id: str,
    card_rel: str | None,
    content_kind: str | None,
    material: str,
    thesis_path: str,
) -> tuple[str | None, str | None, str]:
    """Keep the first run, report, and card identity. send_status may change."""
    if existing.get("run_id") != run_id or existing.get("report_run_id") != report_run_id:
        raise StateError("review_identity_conflict", "已有恢复记录的运行和报告标识保持不变")
    if existing.get("material") not in (None, material):
        raise StateError("review_identity_conflict", "已有恢复记录的 material 保持不变")
    prior_thesis = existing.get("thesis_path")
    if prior_thesis not in (None, thesis_path):
        raise StateError("review_identity_conflict", "已有恢复记录的底稿路径保持不变")
    prior_card = existing.get("card_json")
    if prior_card:
        if card_rel not in (None, prior_card):
            raise StateError("review_identity_conflict", "已有恢复记录的卡片路径保持不变")
        card_rel = prior_card
    prior_kind = existing.get("content_kind")
    if prior_kind:
        if content_kind not in (None, prior_kind):
            raise StateError("review_identity_conflict", "已有恢复记录的 content_kind 保持不变")
        content_kind = prior_kind
    return card_rel, content_kind, material


def _committed_ids(workspace: Path) -> set[str]:
    log = workspace / "research" / "tracker" / "updates.log"
    if not log.is_file() or log.is_symlink():
        return set()
    found: set[str] = set()
    for line in log.read_text(encoding="utf-8").splitlines():
        match = _LOG_RE.match(line)
        if match:
            found.add(match.group("run_id"))
    return found


def unfinished_runs(workspace: Path | str) -> list[dict[str, Any]]:
    root = open_workspace(workspace)
    tracker = root / "research" / "tracker"
    if not tracker.is_dir():
        return []
    committed = _committed_ids(root)
    found: list[dict[str, Any]] = []
    for review_path in tracker.glob("*/review.json"):
        if review_path.is_symlink():
            continue
        run_id = review_path.parent.name
        if run_id in committed:
            continue
        try:
            review = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(review, dict):
            found.append(review)
    found.sort(key=lambda item: str(item.get("run_id") or ""), reverse=True)
    return found


def _same_report(review: dict[str, Any], report: dict[str, Any] | None) -> bool:
    if not isinstance(report, dict):
        return False
    expected = review.get("report_run_id")
    actual = report.get("report_run_id")
    return isinstance(expected, str) and bool(expected) and actual == expected


def _delivery_sent(report: dict[str, Any] | None) -> bool:
    return (
        isinstance(report, dict)
        and report.get("ok") is True
        and report.get("delivery_confirmed") is True
        and report.get("status") == "sent"
    )


def next_step(
    review: dict[str, Any],
    report: dict[str, Any] | None,
    *,
    committed: bool,
    card_present: bool | None = None,
) -> dict[str, Any]:
    """Map one saved review and one parsed automation_report object to the next local action.

    Card or failure confirmation requires report.report_run_id to equal
    review.report_run_id and report.content_kind to be exactly that kind.
    not_automation_run is report.code. card_present False means the recorded
    card path is not a regular file yet.
    """
    decision: dict[str, Any] = {
        "action": "hold",
        "commit_material": None,
        "report_action": None,
        "seen_unchanged": True,
        "keep_card_bytes": True,
        "blocks_new_collect": True,
    }
    if committed:
        decision.update(action="done", seen_unchanged=False, blocks_new_collect=False)
        return decision
    material = review.get("material")
    if material == "no":
        decision.update(action="commit_material_no", commit_material="no", seen_unchanged=False)
        return decision
    if material != "yes":
        return decision
    if isinstance(report, dict) and report.get("code") == "not_automation_run":
        return decision
    if isinstance(report, dict) and not _same_report(review, report):
        return decision
    review_kind = review.get("content_kind")
    report_kind = report.get("content_kind") if isinstance(report, dict) else None
    # A saved failure_text is not a sent card, even when delivery_confirmed is true.
    if review_kind == "failure_text":
        if _delivery_sent(report) and report_kind == "failure_text":
            decision.update(action="failure_closed", blocks_new_collect=False)
            return decision
        if _delivery_sent(report) or (
            isinstance(report, dict)
            and report.get("found") is not False
            and report_kind not in (None, "failure_text")
        ):
            return decision
        decision.update(action="retry_failure_text", report_action="submit")
        return decision
    if _delivery_sent(report) and report_kind == "cards" and review_kind in (None, "cards"):
        decision.update(
            action="commit_material_yes",
            commit_material="yes",
            report_action="status",
            seen_unchanged=False,
        )
        return decision
    if _delivery_sent(report):
        return decision
    if review.get("card_json") and card_present is False:
        # A recorded path is not a card until the file exists. A local sent
        # flag still needs a status read and must not load the missing file.
        if review.get("send_status") == "sent" and not (
            isinstance(report, dict) and report.get("found") is False
        ):
            decision.update(action="status_only", report_action="status")
        else:
            decision.update(action="continue_research")
        return decision
    if isinstance(report, dict) and report.get("found") is False:
        if review.get("card_json"):
            decision.update(action="submit_cards", report_action="submit")
        else:
            decision.update(action="continue_research")
        return decision
    if isinstance(report, dict) and report.get("found") is not False and report_kind != "cards":
        return decision
    if review.get("send_status") == "sent":
        decision.update(action="status_only", report_action="status")
        return decision
    if review.get("card_json") and (
        report_kind == "cards"
        or review.get("send_status") in {"pending", "unknown", "failed"}
        or (isinstance(report, dict) and (report.get("ok") is True or report.get("created") is True))
    ):
        if isinstance(report, dict):
            decision.update(action="retry_same_cards", report_action="submit")
        else:
            decision.update(action="submit_cards", report_action="submit")
        return decision
    if review.get("card_json"):
        decision.update(action="submit_cards", report_action="submit")
        return decision
    decision.update(action="continue_research")
    return decision


def _emit(payload: dict[str, Any], code: int) -> int:
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MSFT 跟踪的本地研究状态")
    sub = parser.add_subparsers(dest="cmd", required=True)

    review = sub.add_parser("save-review")
    review.add_argument("--workspace-dir", required=True)
    review.add_argument("--run-id", required=True)
    review.add_argument("--material", required=True)
    review.add_argument("--summary", required=True)
    review.add_argument("--report-run-id", required=True)
    review.add_argument("--send-status", required=True)
    review.add_argument("--card-json")
    review.add_argument("--thesis-path", default=THESIS_REL)
    review.add_argument("--thesis-sha256")
    review.add_argument("--content-kind")

    opened = sub.add_parser("open-runs")
    opened.add_argument("--workspace-dir", required=True)

    pool = sub.add_parser("x-pool")
    pool_cmd = pool.add_subparsers(dest="pool_cmd", required=True)
    add = pool_cmd.add_parser("add")
    remove = pool_cmd.add_parser("remove")
    for command in (add, remove):
        command.add_argument("--workspace-dir", required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--handle", required=True)
        command.add_argument("--reason", required=True)
    add.add_argument("--tickers", required=True)

    args = parser.parse_args(argv)
    try:
        if args.cmd == "save-review":
            result = save_review(
                args.workspace_dir,
                args.run_id,
                material=args.material,
                summary=args.summary,
                report_run_id=args.report_run_id,
                send_status=args.send_status,
                card_json=args.card_json,
                thesis_path=args.thesis_path,
                thesis_sha256=args.thesis_sha256,
                content_kind=args.content_kind,
            )
        elif args.cmd == "open-runs":
            result = {"ok": True, "runs": unfinished_runs(args.workspace_dir)}
        elif args.pool_cmd == "add":
            result = add_author(
                args.workspace_dir,
                args.run_id,
                args.handle,
                args.tickers,
                args.reason,
            )
        else:
            result = remove_author(
                args.workspace_dir,
                args.run_id,
                args.handle,
                args.reason,
            )
    except StateError as exc:
        return _emit({"ok": False, "status": exc.status, "error": exc.message}, 2)
    return _emit(result, 0)


if __name__ == "__main__":
    sys.exit(main())
