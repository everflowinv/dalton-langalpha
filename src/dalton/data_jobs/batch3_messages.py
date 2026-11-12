"""OC-1 batch-3 message construction and post-confirm writeback.

OpenClaw targets for tasks that do not go through ``cron_command_tasks.py`` live
here, next to the overlay constants ``DEV`` / ``TRACKING`` / ``SEMI``. Chat ids
for semi-rescue are read from ``config/semi_rescue_alert.json`` at prepare time.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from src.dalton.data_jobs.notification_payloads import DataTaskNotificationPrepError

logger = logging.getLogger(__name__)

# OpenClaw cron ``delivery.to`` for announce tasks. ai-chain and ETF keep using
# the overlay constants DEV and TRACKING. Batch 4 personal targets stay in the
# OpenClaw ``user:`` / ``ou_`` form here; ``canonical_route_target`` is what the
# notification route and the binding check consume.
FDE_WEEKLY_DISCORD_TARGET = "channel:1481256083589697566"
SEMI_BOTTOM_DISCORD_TARGET = "channel:1480405100672581875"
E_WATCH_DISCORD_DM = "user:1134818575211839498"
A_SHARE_FEISHU_DM = "user:ou_75b483fb76c31370c715e3a3df1e0a61"
E_WATCH_INTRADAY_JOB_KEY = "e-watch-intraday"
A_SHARE_VOLUME_JOB_KEY = "a-share-bottom-volume-watch"
_ANNOUNCE_STDOUT_TARGETS: dict[str, tuple[str, str]] = {
    E_WATCH_INTRADAY_JOB_KEY: ("discord", E_WATCH_DISCORD_DM),
    "T2": ("discord", E_WATCH_DISCORD_DM),
    A_SHARE_VOLUME_JOB_KEY: ("feishu", A_SHARE_FEISHU_DM),
    "T38": ("feishu", A_SHARE_FEISHU_DM),
}
FDE_WEEKLY_FAILURE_PREFIX = "⚠️ FDE 周报生成失败（"

SEMI_RESCUE_JOB_KEYS = frozenset({"semi-rescue-alert-am", "semi-rescue-alert-pm"})
_ALLOWED_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_HKT = ZoneInfo("Asia/Hong_Kong")

# Same stop-words and title signature as scripts/semi_rescue_alert.py. Writeback
# must keep a "main" placement when the script would, so the next run does not
# treat an already-shown story as new.
_NEWS_STOP = set(
    "the a an and or of to in on for with as at by from is are be its it this that after over into amid "
    "says said say new will could may can than more up down how why what who".split()
)


class _SuppressOutbox:
    """Sentinel: this run has nothing to notify."""


SUPPRESS_OUTBOX = _SuppressOutbox()


def fde_weekly_failure_text(date_iso: str, err_tail: str) -> str:
    """The stdout ``fde_hiring_weekly_report.sh`` prints on failure.

    ``echo`` adds one newline after the header. ``tail -15`` then prints the
    error file's own bytes, including its trailing newline when the file has one.
    """
    return f"{FDE_WEEKLY_FAILURE_PREFIX}{date_iso}），最后 15 行错误：\n{err_tail}"


def _news_toks(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(w) > 2 and w not in _NEWS_STOP}


def _near_dup(left: str, right: str, th: float = 0.6) -> bool:
    words_a, words_b = _news_toks(left), _news_toks(right)
    return bool(words_a and words_b) and len(words_a & words_b) / min(len(words_a), len(words_b)) >= th


def _news_same_event(left: str, right: str) -> bool:
    """Match ``semi_rescue_alert._news_same_event``: number or direction changes are a new story."""
    left_l, right_l = left.lower(), right.lower()

    def signature(text: str) -> tuple[Any, ...]:
        return (
            tuple(re.findall(r"\d+(?:\.\d+)?", text)),
            bool(re.search(r"\b(?:cut\w*|fall\w*|fell|lower\w*|drop\w*|cancel\w*|delay\w*)\b", text)),
            bool(re.search(r"\b(?:rais\w*|ris\w*|rose|boost\w*|increas\w*)\b", text)),
            bool(re.search(r"\b(?:end\w*|clear\w*|resolv\w*|no evidence|no reason)\b", text)),
        )

    return signature(left_l) == signature(right_l) and _near_dup(left_l, right_l, th=0.8)


def _md_us_day(value: Any) -> str:
    """``semi_rescue_alert.md``: month/day with no zero padding."""
    if isinstance(value, datetime):
        day = value.date()
    elif isinstance(value, date):
        day = value
    else:
        day = datetime.fromisoformat(str(value)[:10]).date()
    return f"{day.month}/{day.day}"


def _load_markdown_to_card_elements():
    script = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "dalton"
        / "openclaw_overlays"
        / "scripts"
        / "feishu_send_card.py"
    )
    spec = importlib.util.spec_from_file_location("dalton_overlay_feishu_send_card", script)
    if spec is None or spec.loader is None:
        raise DataTaskNotificationPrepError(f"飞书卡片脚本无法加载：{script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.markdown_to_card_elements


def _workspace_root(workspace: Path) -> Path:
    root = Path(workspace)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"workspace 不可用：{root}")
    resolved = root.resolve(strict=True)
    if resolved != root and root.is_symlink():
        raise ValueError(f"workspace 不能是软链接：{root}")
    return resolved


def _contained(workspace: Path, raw: str, *, must_exist: bool = False) -> Path:
    """Resolve ``raw`` inside ``workspace``. Reject ``..``, absolute escapes, and symlinks."""
    root = _workspace_root(workspace)
    text = str(raw or "").strip()
    if not text or "\x00" in text:
        raise ValueError("路径为空或含空字符")
    raw_path = Path(text)
    if ".." in raw_path.parts:
        raise ValueError(f"路径越界：{text}")
    if raw_path.is_absolute():
        try:
            rel_path = Path(os.path.abspath(raw_path)).relative_to(root)
        except ValueError as exc:
            raise ValueError(f"路径越出 workspace：{text}") from exc
    else:
        rel_path = raw_path
    parts = [part for part in rel_path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"路径越界：{text}")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"拒绝软链接：{current}")
    resolved = current.resolve(strict=False)
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError(f"路径越出 workspace：{text}")
    if must_exist and not current.is_file():
        raise ValueError(f"文件不存在：{current}")
    return current


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    if target.is_symlink():
        raise ValueError(f"拒绝写入软链接：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".tmp.{target.name}.{os.getpid()}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _read_json_file(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"JSON 文件不可读：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_route_target(channel: str, openclaw_target: str) -> str:
    """Map an OpenClaw ``channel`` + ``target`` to a Dalton notify route string.

    Discord ``user:`` / ``channel:`` and Feishu ``user:`` / ``chat:`` keep their
    prefix. A bare Feishu chat id ``oc_…`` becomes ``feishu:chat:oc_…``.
    """
    platform = str(channel or "").strip().lower()
    target = str(openclaw_target or "").strip()
    if platform == "discord" and (target.startswith("user:") or target.startswith("channel:")):
        return f"discord:{target}"
    if platform == "feishu" and (target.startswith("user:") or target.startswith("chat:")):
        return f"feishu:{target}"
    if platform == "feishu" and target.startswith("oc_"):
        return f"feishu:chat:{target}"
    raise ValueError(f"无法转换成通知路由目标：{platform} {target}")


def _canonical_announce_job_key(job_key: str) -> str:
    if job_key in {E_WATCH_INTRADAY_JOB_KEY, "T2"}:
        return E_WATCH_INTRADAY_JOB_KEY
    if job_key in {A_SHARE_VOLUME_JOB_KEY, "T38"}:
        return A_SHARE_VOLUME_JOB_KEY
    return job_key


def adjust_batch3_payload(
    *,
    job_key: str,
    payload: dict[str, Any] | None,
    stdout_text: str = "",
    workspace_dir: Path | None = None,
    run_artifacts_dir: Path | None = None,
) -> dict[str, Any] | _SuppressOutbox | None:
    """Return a replacement outbox, ``SUPPRESS_OUTBOX``, or None to keep ``payload``."""
    key = str(job_key or "").strip()
    if key in SEMI_RESCUE_JOB_KEYS:
        return _adjust_semi_rescue(
            key,
            payload,
            workspace_dir=workspace_dir,
            run_artifacts_dir=run_artifacts_dir,
        )
    if key == "semi-bottom-checklist-1030et":
        return _adjust_plain_discord(
            payload,
            stdout_text,
            job_key=key,
            target=SEMI_BOTTOM_DISCORD_TARGET,
            suppress_no_reply=True,
        )
    if key == "fde-hiring-weekly-report":
        return _adjust_fde_weekly(payload, stdout_text)
    if key in _ANNOUNCE_STDOUT_TARGETS:
        channel, target = _ANNOUNCE_STDOUT_TARGETS[key]
        return _adjust_announce_stdout(
            payload,
            stdout_text,
            job_key=key,
            channel=channel,
            target=target,
        )
    return None


def _adjust_plain_discord(
    payload: dict[str, Any] | None,
    stdout_text: str,
    *,
    job_key: str,
    target: str,
    suppress_no_reply: bool,
    channel: str = "discord",
) -> dict[str, Any] | _SuppressOutbox | None:
    text = stdout_text if stdout_text else str((payload or {}).get("text") or "")
    if not text.strip():
        return SUPPRESS_OUTBOX
    first = text.splitlines()[0].strip() if text.splitlines() else ""
    if suppress_no_reply and first == "NO_REPLY":
        return SUPPRESS_OUTBOX
    out = dict(payload or {})
    out["job_key"] = job_key
    out["format"] = "text"
    out["channel"] = channel
    out["target"] = target
    out["text"] = text
    out["feishu_message"] = text
    out["preserve_exact_text"] = True
    out.pop("notify_on_failure", None)
    return out


def _adjust_announce_stdout(
    payload: dict[str, Any] | None,
    stdout_text: str,
    *,
    job_key: str,
    channel: str,
    target: str,
) -> dict[str, Any] | _SuppressOutbox | None:
    """Send the same stdout OpenClaw announce would, including a failure sentence.

    Empty text and a first line of ``NO_REPLY`` are dropped. A kept message is
    marked ``notify_on_failure`` so a non-zero exit that still printed the
    sentence (a-share's eight-failure line) is not skipped.
    """
    adjusted = _adjust_plain_discord(
        payload,
        stdout_text,
        job_key=_canonical_announce_job_key(job_key),
        target=target,
        suppress_no_reply=True,
        channel=channel,
    )
    if adjusted is SUPPRESS_OUTBOX or not isinstance(adjusted, dict):
        return adjusted
    adjusted["route_target"] = canonical_route_target(channel, target)
    adjusted["notify_on_failure"] = True
    return adjusted


def _adjust_fde_weekly(
    payload: dict[str, Any] | None,
    stdout_text: str,
) -> dict[str, Any] | _SuppressOutbox | None:
    adjusted = _adjust_plain_discord(
        payload,
        stdout_text,
        job_key="fde-hiring-weekly-report",
        target=FDE_WEEKLY_DISCORD_TARGET,
        suppress_no_reply=False,
    )
    if adjusted is SUPPRESS_OUTBOX or not isinstance(adjusted, dict):
        return adjusted
    text = str(adjusted.get("text") or "")
    if text.startswith(FDE_WEEKLY_FAILURE_PREFIX):
        adjusted["notify_on_failure"] = True
    return adjusted


def _semi_rescue_subtitle(cfg: Mapping[str, Any], snap: Mapping[str, Any], layout: str) -> str:
    generated_hkt = str(snap.get("generated_hkt") or "")
    last_day = _md_us_day(snap.get("last_day"))
    if layout == "compact":
        return f"{generated_hkt} 香港｜美股截至 {last_day} 收盘"
    verdict = snap.get("verdict") if isinstance(snap.get("verdict"), Mapping) else {}
    generated_ts = snap.get("generated_ts")
    if isinstance(generated_ts, (int, float)) and not isinstance(generated_ts, bool):
        full_ts = datetime.fromtimestamp(float(generated_ts), _HKT).strftime("%Y-%m-%d %H:%M")
    else:
        full_ts = generated_hkt
    manual = cfg.get("manual_items") or []
    manual_n = len(manual) if isinstance(manual, list) else 0
    auto_n = int(verdict.get("g") or 0) + int(verdict.get("y") or 0) + int(verdict.get("r") or 0)
    return f"{full_ts} 香港时间｜美股截至 {last_day} 收盘｜自动指标 {auto_n} 项 + 人工 {manual_n} 项"


def _stage_card_images(
    card: dict[str, Any],
    *,
    workspace: Path,
    run_dir: Path,
) -> list[dict[str, Any]]:
    """Copy ``__img__`` files into the run directory. A missing chart fails the run."""
    placeholders: list[str] = []

    def walk(elements: list[Any]) -> None:
        for el in elements:
            if not isinstance(el, dict):
                continue
            if el.get("tag") == "__img__" and el.get("path"):
                placeholders.append(str(el["path"]))
            nested = el.get("elements")
            if el.get("tag") == "collapsible_panel" and isinstance(nested, list):
                walk(nested)

    body = card.get("body")
    if isinstance(body, dict) and isinstance(body.get("elements"), list):
        walk(body["elements"])

    images: list[dict[str, Any]] = []
    for img_idx, placeholder in enumerate(placeholders):
        src = _contained(workspace, placeholder, must_exist=True)
        if src.is_symlink() or not src.is_file() or src.stat().st_size <= 0:
            raise DataTaskNotificationPrepError(f"卡片图片不存在或为空：{placeholder}")
        raw_ext = src.suffix.lower()
        ext = raw_ext if raw_ext in _ALLOWED_IMAGE_EXTS else ".png"
        rel = f"images/card_0_img_{img_idx}{ext}"
        dest = run_dir / rel
        if dest.is_symlink():
            raise DataTaskNotificationPrepError(f"运行目录图片路径是软链接：{rel}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        if not data:
            raise DataTaskNotificationPrepError(f"卡片图片为空：{placeholder}")
        _atomic_write_bytes(dest, data)
        images.append(
            {
                "placeholder": placeholder,
                "path": rel,
                "filename": src.name,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return images


def _adjust_semi_rescue(
    job_key: str,
    payload: dict[str, Any] | None,
    *,
    workspace_dir: Path | None,
    run_artifacts_dir: Path | None,
) -> dict[str, Any] | _SuppressOutbox | None:
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("feishu_cards"), list):
        return None
    status = str(payload.get("status") or "")
    if status != "generated":
        return SUPPRESS_OUTBOX
    if workspace_dir is None or run_artifacts_dir is None:
        raise DataTaskNotificationPrepError("半导体救场卡片缺少 workspace，不能在发送前组装")
    workspace = _workspace_root(Path(workspace_dir))
    run_dir = Path(run_artifacts_dir)
    raw_dir = str(payload.get("dir") or "").strip()
    if not raw_dir:
        raise DataTaskNotificationPrepError("半导体救场输出缺少 dir")
    out_dir = _contained(workspace, raw_dir)
    if out_dir.is_symlink() or not out_dir.is_dir():
        raise DataTaskNotificationPrepError(f"半导体救场目录不可用：{raw_dir}")
    card_md_path = out_dir / "card.md"
    snap_path = out_dir / "snapshot.json"
    if card_md_path.is_symlink() or not card_md_path.is_file():
        raise DataTaskNotificationPrepError("半导体救场缺少 card.md")
    if snap_path.is_symlink() or not snap_path.is_file():
        raise DataTaskNotificationPrepError("半导体救场缺少 snapshot.json")
    cfg_path = _contained(workspace, "config/semi_rescue_alert.json", must_exist=True)
    cfg = _read_json_file(cfg_path)
    snap = _read_json_file(snap_path)
    if not isinstance(cfg, dict) or not isinstance(snap, dict):
        raise DataTaskNotificationPrepError("半导体救场配置或快照不是对象")
    for required in ("title", "chat_id"):
        if not str(cfg.get(required) or "").strip():
            raise DataTaskNotificationPrepError(f"半导体救场配置缺少 {required}")
    verdict = snap.get("verdict")
    if not isinstance(verdict, dict) or not verdict.get("text") or not verdict.get("emoji"):
        raise DataTaskNotificationPrepError("半导体救场快照缺少结论")
    slot = str(snap.get("slot") or payload.get("slot") or "")
    if slot not in ("am", "pm"):
        raise DataTaskNotificationPrepError(f"半导体救场 slot 无法识别：{slot}")
    layout = str(cfg.get("layout") or "compact")
    slot_cn = "早报" if slot == "am" else "晚报"
    title = f"{cfg['title']}{slot_cn}｜{verdict['emoji']} {verdict['text']}"
    subtitle = _semi_rescue_subtitle(cfg, snap, layout)
    markdown = card_md_path.read_text(encoding="utf-8")
    to_card = _load_markdown_to_card_elements()
    card = to_card(
        markdown,
        title,
        subtitle,
        header_template=str(verdict.get("template") or "indigo"),
        tag="半导体",
        tag_color="indigo",
    )
    images = _stage_card_images(card, workspace=workspace, run_dir=run_dir)
    try:
        rel_dir = out_dir.resolve(strict=True).relative_to(workspace).as_posix()
    except ValueError as exc:
        raise DataTaskNotificationPrepError("半导体救场目录越出 workspace") from exc
    return {
        "job_key": job_key,
        "channel": "feishu",
        "chat_id": str(cfg["chat_id"]),
        "feishu_cards": [
            {
                "card": card,
                "images": images,
                "chat_id": str(cfg["chat_id"]),
            }
        ],
        "delivery_writeback": {
            "kind": "semi_rescue_alert",
            "dir": rel_dir,
            "slot": slot,
        },
    }


def _openclaw_json_bytes(value: Any, *, default: Any = None) -> bytes:
    """Match OpenClaw ``write_json``: indent 2, ``ensure_ascii=False``, no trailing newline.

    ai-chain's writer does not pass ``default``. Semi's writer passes ``default=str``.
    """
    if default is None:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=default)
    return text.encode("utf-8")


def _writeback_snapshot_dir(run_artifacts_dir: Path | None) -> Path:
    if run_artifacts_dir is None:
        raise ValueError("写回缺少该次运行的快照目录")
    snap = Path(run_artifacts_dir) / "writeback"
    if snap.is_symlink() or not snap.is_dir():
        raise ValueError("写回快照目录不存在")
    return snap


def _snapshot_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def capture_delivery_writeback_snapshot(
    outbox: Mapping[str, Any],
    *,
    workspace: Path | str,
    run_dir: Path | str,
) -> dict[str, Any]:
    """Copy this run's writeback inputs under ``run_dir/writeback`` and mark pending.

    A later confirm reads only these files. A required ai-chain or semi source
    that is missing raises. A missing ETF mirror helper does not: the Feishu
    card still goes out and the mirror is recorded as skipped.
    """
    if not isinstance(outbox, Mapping):
        raise ValueError("写回 outbox 不是对象")
    payload = dict(outbox)
    raw_spec = payload.get("delivery_writeback")
    if not isinstance(raw_spec, Mapping):
        raise ValueError("写回缺少 delivery_writeback")
    spec = dict(raw_spec)
    kind = str(spec.get("kind") or "")
    root = _workspace_root(Path(workspace))
    snap_dir = Path(run_dir) / "writeback"
    if snap_dir.exists() and (snap_dir.is_symlink() or not snap_dir.is_dir()):
        raise ValueError("写回快照目录非法")
    snap_dir.mkdir(parents=True, exist_ok=True)
    if kind == "ai_chain_daily_push":
        _capture_ai_chain_snapshot(root, spec, snap_dir)
    elif kind == "semi_rescue_alert":
        _capture_semi_snapshot(root, spec, snap_dir)
    elif kind == "etf_discord_mirror":
        _capture_etf_mirror(payload, spec, root, snap_dir)
    else:
        raise ValueError(f"未知写回类型：{kind}")
    payload["delivery_writeback"] = spec
    payload["writeback_status"] = "pending"
    return payload


def _capture_ai_chain_snapshot(workspace: Path, spec: dict[str, Any], snap_dir: Path) -> None:
    raw = str(spec.get("path") or "skills/ai-chain/state/daily_push.json")
    path = _contained(workspace, raw, must_exist=True)
    if path.name != "daily_push.json":
        raise ValueError(f"ai-chain 写回路径必须是 daily_push.json：{raw}")
    data = path.read_bytes()
    _atomic_write_bytes(snap_dir / "daily_push.json", data)
    spec["snapshot_rel"] = "writeback/daily_push.json"
    spec["snapshot_sha256"] = _snapshot_sha256(data)


def _capture_semi_snapshot(workspace: Path, spec: dict[str, Any], snap_dir: Path) -> None:
    raw_dir = str(spec.get("dir") or "").strip()
    if not raw_dir:
        raise ValueError("semi-rescue 写回缺少 dir")
    out_dir = _contained(workspace, raw_dir)
    snap_path = out_dir / "snapshot.json"
    if snap_path.is_symlink() or not snap_path.is_file():
        raise ValueError("semi-rescue snapshot.json 不存在")
    snap_bytes = snap_path.read_bytes()
    snap = json.loads(snap_bytes.decode("utf-8"))
    if not isinstance(snap, dict):
        raise ValueError("semi-rescue snapshot.json 不是对象")
    news_path = out_dir / "news.json"
    if news_path.is_symlink():
        raise ValueError("拒绝读取软链接 news.json")
    if news_path.is_file():
        news_bytes = news_path.read_bytes()
    else:
        news_bytes = b"{}"
    line = _history_line(_semi_history_record(snap))
    _atomic_write_bytes(snap_dir / "snapshot.json", snap_bytes)
    _atomic_write_bytes(snap_dir / "history_line.txt", line.encode("utf-8"))
    _atomic_write_bytes(snap_dir / "news.json", news_bytes)
    spec["snapshot_rel"] = "writeback/snapshot.json"
    spec["snapshot_sha256"] = _snapshot_sha256(snap_bytes)


def _discord_target_from_mirror(raw: Mapping[str, Any]) -> str | None:
    channel = str(raw.get("channel") or "").strip().lower()
    target = str(raw.get("target") or "").strip()
    if channel != "discord" or not target:
        return None
    if target.startswith("discord:"):
        return target
    if target.startswith("user:") or target.startswith("channel:"):
        return f"discord:{target}"
    return None


def _load_eve_tracking_mirror(workspace: Path):
    script = workspace / "scripts" / "eve_tracking_mirror.py"
    if script.is_symlink() or not script.is_file():
        return None
    spec = importlib.util.spec_from_file_location("dalton_eve_tracking_mirror", script)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot_mirror_images(workspace: Path, snap_dir: Path, image_paths: Any) -> list[dict[str, Any]]:
    saved: list[dict[str, Any]] = []
    if not isinstance(image_paths, list):
        return saved
    for raw in image_paths:
        try:
            src = _contained(workspace, str(raw), must_exist=True)
        except ValueError:
            continue
        if src.is_symlink() or not src.is_file():
            continue
        data = src.read_bytes()
        if not data:
            continue
        filename = f"{len(saved)}-{src.name}"
        _atomic_write_bytes(snap_dir / "media" / filename, data)
        saved.append(
            {
                "filename": src.name,
                "rel": f"writeback/media/{filename}",
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return saved


def _capture_etf_mirror(
    payload: Mapping[str, Any],
    spec: dict[str, Any],
    workspace: Path,
    snap_dir: Path,
) -> None:
    """Snapshot Discord text. A missing helper or a declined mirror does not raise."""
    card: dict[str, Any] | None = None
    cards = payload.get("feishu_cards")
    if isinstance(cards, list) and cards and isinstance(cards[0], Mapping):
        raw_card = cards[0].get("card")
        if isinstance(raw_card, Mapping):
            card = dict(raw_card)
    module = _load_eve_tracking_mirror(workspace)
    skipped = True
    messages: list[str] = []
    targets: list[str] = []
    images: list[dict[str, Any]] = []
    if module is None:
        logger.warning("ETF Discord mirror helper is missing; Feishu card will still be sent")
    elif card is None:
        logger.warning("ETF Discord mirror has no card to copy; Feishu card will still be sent")
    else:
        try:
            config = module.load_config()
            chat_id = str(spec.get("chat_id") or "")
            receive_id_type = str(spec.get("receive_id_type") or "chat_id")
            if module.should_mirror(chat_id, receive_id_type, config):
                text, image_paths = module.card_to_text(card)
                messages = [str(item) for item in module.chunk(text) if str(item)]
                for raw_target in config.get("targets") or []:
                    if not isinstance(raw_target, Mapping):
                        continue
                    mapped = _discord_target_from_mirror(raw_target)
                    if mapped and mapped not in targets:
                        targets.append(mapped)
                images = _snapshot_mirror_images(workspace, snap_dir, image_paths)
                skipped = False
        except Exception as exc:
            logger.warning("ETF Discord mirror snapshot skipped: %s", exc)
            skipped = True
            messages = []
            targets = []
            images = []
    record = {"skipped": skipped, "messages": messages, "targets": targets, "images": images}
    _atomic_write_bytes(
        snap_dir / "etf_mirror.json",
        json.dumps(record, ensure_ascii=False).encode("utf-8"),
    )
    spec["mirror_skipped"] = skipped
    spec["targets"] = targets
    spec["snapshot_rel"] = "writeback/etf_mirror.json"


def apply_confirmed_delivery_writeback(
    outbox: Mapping[str, Any] | None,
    workspace: Path | str,
    delivery_status: str,
    *,
    run_artifacts_dir: Path | None = None,
) -> list[str]:
    """Apply this run's snapshotted writeback. Return ETF Discord targets still to send.

    No-op unless delivery is confirmed. A second call for the same run does not
    append another history line, refresh ``news_seen`` timestamps, or flip
    ``sent`` again. Callers must not invoke this when delivery failed.
    """
    if delivery_status != "dispatched":
        return []
    if not isinstance(outbox, Mapping):
        return []
    spec = outbox.get("delivery_writeback")
    if not isinstance(spec, Mapping):
        return []
    kind = str(spec.get("kind") or "")
    root = _workspace_root(Path(workspace))
    if kind == "ai_chain_daily_push":
        _write_ai_chain_sent(root, spec, run_artifacts_dir)
        return []
    if kind == "semi_rescue_alert":
        _write_semi_rescue_state(root, spec, run_artifacts_dir)
        return []
    if kind == "etf_discord_mirror":
        return _etf_mirror_targets(run_artifacts_dir)
    return []


def _write_ai_chain_sent(
    workspace: Path,
    spec: Mapping[str, Any],
    run_artifacts_dir: Path | None,
) -> None:
    raw = str(spec.get("path") or "skills/ai-chain/state/daily_push.json")
    path = _contained(workspace, raw, must_exist=True)
    if path.name != "daily_push.json":
        raise ValueError(f"ai-chain 写回路径必须是 daily_push.json：{raw}")
    snap_dir = _writeback_snapshot_dir(run_artifacts_dir)
    snap_file = snap_dir / "daily_push.json"
    if snap_file.is_symlink() or not snap_file.is_file():
        raise ValueError("ai-chain 写回快照不存在")
    snap_bytes = snap_file.read_bytes()
    snap_obj = json.loads(snap_bytes.decode("utf-8"))
    if not isinstance(snap_obj, dict):
        raise ValueError("ai-chain 写回快照不是对象")
    live_bytes = path.read_bytes()
    live_obj = json.loads(live_bytes.decode("utf-8"))
    if not isinstance(live_obj, dict):
        raise ValueError("daily_push.json 不是对象")
    sent_obj = {**snap_obj, "sent": True}
    if live_obj == sent_obj:
        return
    snap_hash = str(spec.get("snapshot_sha256") or _snapshot_sha256(snap_bytes))
    live_hash = _snapshot_sha256(live_bytes)
    if live_hash != snap_hash:
        logger.warning(
            "ai-chain daily_push.json hash %s does not match snapshot %s; skipping sent writeback",
            live_hash,
            snap_hash,
        )
        return
    _atomic_write_bytes(path, _openclaw_json_bytes(sent_obj))


def _semi_history_record(snap: Mapping[str, Any]) -> dict[str, Any]:
    indicators = snap.get("indicators")
    status_map: dict[str, Any] = {}
    if isinstance(indicators, list):
        for item in indicators:
            if isinstance(item, dict) and item.get("id") is not None:
                status_map[str(item["id"])] = item.get("status")
    return {
        "t": snap.get("generated_hkt"),
        "slot": snap.get("slot"),
        "verdict": snap.get("verdict"),
        "status": status_map,
    }


def _history_line(record: Mapping[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, default=str) + "\n"


def _etf_mirror_targets(run_artifacts_dir: Path | None) -> list[str]:
    snap_dir = _writeback_snapshot_dir(run_artifacts_dir)
    raw = _read_json_file(snap_dir / "etf_mirror.json")
    if not isinstance(raw, dict):
        raise ValueError("ETF Discord 快照不是对象")
    if raw.get("skipped") is True:
        return []
    targets = raw.get("targets")
    if not isinstance(targets, list):
        raise ValueError("ETF Discord 快照缺少目标")
    cleaned: list[str] = []
    for item in targets:
        text = str(item or "").strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _pinned_news_ts(snap_dir: Path) -> float:
    """Pin ``ts`` before mutating shared files so a retry of this run stays identical."""
    marker = snap_dir / "news_ts.txt"
    if marker.is_symlink():
        raise ValueError("拒绝读取软链接 news_ts.txt")
    if marker.is_file():
        return float(marker.read_text(encoding="utf-8").strip())
    now = time.time()
    _atomic_write_bytes(marker, str(now).encode("utf-8"))
    return now


def _write_semi_rescue_state(
    workspace: Path,
    spec: Mapping[str, Any],
    run_artifacts_dir: Path | None,
) -> None:
    snap_dir = _writeback_snapshot_dir(run_artifacts_dir)
    snap_path = snap_dir / "snapshot.json"
    line_path = snap_dir / "history_line.txt"
    news_path = snap_dir / "news.json"
    for required in (snap_path, line_path, news_path):
        if required.is_symlink() or not required.is_file():
            raise ValueError(f"semi-rescue 写回快照缺少 {required.name}")
    snap_bytes = snap_path.read_bytes()
    state_dir = _contained(workspace, "state/semi-rescue-alert")
    if state_dir.exists() and (state_dir.is_symlink() or not state_dir.is_dir()):
        raise ValueError("semi-rescue 状态目录非法")
    state_dir.mkdir(parents=True, exist_ok=True)
    if _semi_writeback_blocked_by_newer_run(state_dir, spec):
        return
    now = _pinned_news_ts(snap_dir)

    last_path = state_dir / "last.json"
    if last_path.is_symlink():
        raise ValueError("拒绝写入软链接 last.json")
    if not last_path.is_file() or last_path.read_bytes() != snap_bytes:
        _atomic_write_bytes(last_path, snap_bytes)

    news = json.loads(news_path.read_text(encoding="utf-8"))
    if isinstance(news, dict) and news:
        _update_news_seen(state_dir / "news_seen.json", news, now=now)

    line = line_path.read_text(encoding="utf-8")
    if not line.endswith("\n"):
        line += "\n"
    history_path = state_dir / "history.jsonl"
    if history_path.is_symlink():
        raise ValueError("拒绝写入软链接 history.jsonl")
    existing = history_path.read_text(encoding="utf-8") if history_path.is_file() else ""
    if line not in existing.splitlines(keepends=True):
        _atomic_write_bytes(history_path, (existing + line).encode("utf-8"))
    _record_semi_writeback_order(state_dir, spec)


def _started_at_value(value: Any) -> datetime | None:
    """Parse a data-task ``started_at`` into a timezone-aware datetime."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _semi_writeback_blocked_by_newer_run(state_dir: Path, spec: Mapping[str, Any]) -> bool:
    """True when a newer run already wrote the shared semi-rescue files.

    Order is ``dalton_data_task_runs.started_at``, stored on the writeback
    spec at capture and on ``writeback_order.json`` after a successful write.
    A retry of the same run (equal ``started_at``) is not blocked. Returning
    true skips the write so the caller can mark the attempt finished.
    """
    marker = state_dir / "writeback_order.json"
    if marker.is_symlink():
        raise ValueError("拒绝读取软链接 writeback_order.json")
    if not marker.is_file():
        return False
    raw = _read_json_file(marker)
    recorded_text = raw.get("started_at") if isinstance(raw, dict) else None
    recorded = _started_at_value(recorded_text)
    if recorded is None:
        raise ValueError("semi-rescue writeback_order.json 无法比较运行顺序")
    current = _started_at_value(spec.get("run_started_at"))
    if current is not None and current >= recorded:
        return False
    logger.warning(
        "semi-rescue writeback skipped; shared state was written by a newer run "
        "(this started_at=%s, recorded started_at=%s)",
        spec.get("run_started_at"),
        recorded_text,
    )
    return True


def _record_semi_writeback_order(state_dir: Path, spec: Mapping[str, Any]) -> None:
    started = spec.get("run_started_at")
    if not isinstance(started, str) or not started.strip():
        return
    if _started_at_value(started) is None:
        return
    body = json.dumps(
        {"started_at": started.strip(), "run_id": str(spec.get("run_id") or "")},
        ensure_ascii=False,
    ).encode("utf-8")
    _atomic_write_bytes(state_dir / "writeback_order.json", body)


def _match_published(topics: list[Any], topic_id: str, title: str, link: str) -> tuple[str, str]:
    """Recover ``published`` and ``source`` from topic items. Empty when the file has neither."""
    best: tuple[int, Mapping[str, Any]] | None = None
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        for item in topic.get("items") or []:
            if not isinstance(item, dict):
                continue
            score = 0
            if str(topic.get("id") or "") == topic_id:
                score += 4
            if link and str(item.get("link") or "") == link:
                score += 2
            if str(item.get("title") or "") == title:
                score += 1
            if score <= 0:
                continue
            if best is None or score > best[0]:
                best = (score, item)
    if best is None:
        return "", ""
    item = best[1]
    published = item.get("ts")
    if not isinstance(published, str):
        published = ""
    source = item.get("source")
    if not isinstance(source, str):
        source = ""
    return published, source


def _update_news_seen(path: Path, news: Mapping[str, Any], *, now: float) -> None:
    """Match ``news_seen_update``: every new successful send refreshes ``ts``.

    ``now`` is pinned in this run's snapshot directory, so a retry of the same
    confirm rewrites the same bytes and does not refresh ``ts`` again.
    """
    if path.is_symlink():
        raise ValueError("拒绝写入软链接 news_seen.json")
    seen: dict[str, Any] = {}
    if path.is_file():
        loaded = _read_json_file(path)
        if isinstance(loaded, dict):
            seen = dict(loaded)
    selection = news.get("selection") if isinstance(news.get("selection"), list) else []
    topics = news.get("topics") if isinstance(news.get("topics"), list) else []
    changed = False
    for raw in selection:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "")
        link = str(raw.get("link") or "")
        key = f"gn:{link or title}"
        placement = str(raw.get("placement") or "fold")
        previous = seen.get(key)
        if (
            isinstance(previous, dict)
            and previous.get("placement", "main") == "main"
            and _news_same_event(title, str(previous.get("t") or ""))
        ):
            placement = "main"
        published, source = _match_published(topics, str(raw.get("topic") or ""), title, link)
        record = {
            "t": title,
            "ts": now,
            "placement": placement,
            "topic": raw.get("topic"),
            "published": published,
            "source": source,
            "link": link,
            "importance": raw.get("importance", 0),
        }
        if previous != record:
            seen[key] = record
            changed = True
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        for mail in topic.get("mail") or []:
            if not isinstance(mail, dict) or not mail.get("key"):
                continue
            mail_key = str(mail["key"])
            record = {"t": str(mail.get("text") or "")[:80], "ts": now}
            if seen.get(mail_key) != record:
                seen[mail_key] = record
                changed = True
    pruned = {k: v for k, v in seen.items() if isinstance(v, dict) and now - float(v.get("ts") or 0) < 7 * 86400}
    if pruned.keys() != seen.keys():
        changed = True
        seen = pruned
    if not changed:
        return
    _atomic_write_bytes(path, _openclaw_json_bytes(seen, default=str))
