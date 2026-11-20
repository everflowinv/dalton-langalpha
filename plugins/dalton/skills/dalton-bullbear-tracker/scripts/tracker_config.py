"""Config-driven ticker discovery and company rules resolution. Stdlib only."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
CARD_STYLE_KEYS: tuple[str, ...] = (
    "title_prefix",
    "subtitle",
    "tag",
    "header_template",
    "tag_color",
)
_SUPPORTED_CACHE: dict[str, tuple[tuple[Any, ...], tuple[str, ...]]] = {}


class RulesPathError(ValueError):
    """Raised when a workspace rules path escapes the workspace or uses an unsafe symlink."""

    def __init__(self, status: str, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def _inside(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _inspect_workspace_rules(workspace: Path | str | None) -> tuple[Path | None, str | None]:
    """Return ``(rules_path, error_status)`` for ``workspace/research/tracker/rules.md``."""
    if workspace is None:
        return None, None
    ws = Path(workspace)
    if ws.is_symlink() or not ws.is_dir():
        return None, None
    ws_resolved = ws.resolve()
    current = ws
    for part in ("research", "tracker"):
        current = current / part
        if current.is_symlink():
            if not _inside(ws_resolved, current):
                return None, "symlink_escape"
            current = current.resolve()
        if not current.exists():
            return None, None
        if not current.is_dir():
            return None, None
    candidate = current / "rules.md"
    if candidate.is_symlink():
        if not _inside(ws_resolved, candidate):
            return None, "symlink_escape"
        resolved = candidate.resolve()
        if not resolved.exists():
            return None, None
        if not resolved.is_file():
            return None, "symlink_escape"
        return resolved, None
    if not candidate.exists():
        return None, None
    if not candidate.is_file() or not _inside(ws_resolved, candidate):
        return None, "symlink_escape"
    return candidate, None


def resolve_rules_path(
    ticker: str,
    workspace: Path | str | None = None,
    skill_dir: Path | None = None,
    *,
    strict: bool = False,
) -> tuple[Path | None, str | None]:
    """Resolve ``(rules_path, source)``: workspace rules.md first, then references/<ticker>-rules.md."""
    chosen = str(ticker or "").strip().lower()
    if not chosen:
        return None, None
    ws_rules, err = _inspect_workspace_rules(workspace)
    if err is not None:
        if strict:
            raise RulesPathError(err, "工作区规则文件路径存在符号链接越界")
    elif ws_rules is not None:
        return ws_rules, "workspace"
    base = Path(skill_dir) if skill_dir is not None else SKILL_DIR
    ref_rules = base / "references" / f"{chosen}-rules.md"
    if ref_rules.is_symlink() or not ref_rules.is_file():
        return None, None
    if not _inside(base, ref_rules):
        return None, None
    return ref_rules, "skill"


def _stat_sig(path: Path) -> tuple[Any, ...] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (path.name, st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def _dir_signature(base: Path) -> tuple[Any, ...]:
    config_dir = base / "config"
    refs_dir = base / "references"

    cfg_entries: list[tuple[Any, ...]] = []
    if config_dir.is_dir():
        for p in sorted(config_dir.glob("*.json")):
            if p.is_symlink() or not p.is_file():
                continue
            entry = _stat_sig(p)
            if entry is not None:
                cfg_entries.append(entry)
        try:
            cfg_st = config_dir.stat()
            cfg_dir_sig: Any = (cfg_st.st_dev, cfg_st.st_ino, cfg_st.st_mtime_ns, cfg_st.st_ctime_ns)
        except OSError:
            cfg_dir_sig = 0
    else:
        cfg_dir_sig = -1

    ref_entries: list[tuple[Any, ...]] = []
    if refs_dir.is_dir():
        for p in sorted(refs_dir.glob("*-rules.md")):
            if p.is_symlink() or not p.is_file():
                continue
            entry = _stat_sig(p)
            if entry is not None:
                ref_entries.append(entry)
        try:
            ref_st = refs_dir.stat()
            ref_dir_sig: Any = (ref_st.st_dev, ref_st.st_ino, ref_st.st_mtime_ns, ref_st.st_ctime_ns)
        except OSError:
            ref_dir_sig = 0
    else:
        ref_dir_sig = -1

    return (cfg_dir_sig, tuple(cfg_entries), ref_dir_sig, tuple(ref_entries))


def _has_valid_card(card: Any) -> bool:
    if not isinstance(card, dict):
        return False
    return all(isinstance(card.get(key), str) and bool(card.get(key)) for key in CARD_STYLE_KEYS)


def _has_valid_sources_shape(data: dict[str, Any]) -> bool:
    if "sources" not in data:
        return True
    return isinstance(data.get("sources"), dict)


def load_validated_config(
    ticker: str,
    skill_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Validate config/<T>.json (exact ticker, card styles, dict sources) and skill rules once."""
    if not isinstance(ticker, str):
        return None
    chosen = ticker.strip().upper()
    if not chosen:
        return None
    base = Path(skill_dir) if skill_dir is not None else SKILL_DIR
    path = base / "config" / f"{chosen}.json"
    if path.is_symlink() or not path.is_file() or not _inside(base, path):
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # ticker 必须与文件名完全一致（不 strip，带空格即判不受支持）
    if data.get("ticker") != path.stem:
        return None
    if not _has_valid_card(data.get("card")):
        return None
    if not _has_valid_sources_shape(data):
        return None
    ref_path, _source = resolve_rules_path(chosen, workspace=None, skill_dir=base)
    if ref_path is None:
        return None
    return data


def explain_unsupported_ticker(ticker: str, skill_dir: Path | None = None) -> str:
    """Explain specifically which skill config, card field, or references rule file is missing."""
    chosen = str(ticker or "").strip().upper()
    if not chosen:
        return "不支持空 ticker（缺少 config/<TICKER>.json 与 references/<ticker>-rules.md）"
    base = Path(skill_dir) if skill_dir is not None else SKILL_DIR
    cfg_path = base / "config" / f"{chosen}.json"
    ref_rel = f"references/{chosen.lower()}-rules.md"
    ref_path = base / ref_rel

    reasons: list[str] = []
    if cfg_path.is_symlink() or not cfg_path.is_file() or not _inside(base, cfg_path):
        reasons.append(f"缺少 config/{chosen}.json")
    else:
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            reasons.append(f"config/{chosen}.json 无法解析")
        else:
            if not isinstance(data, dict):
                reasons.append(f"config/{chosen}.json 不是 JSON 对象")
            else:
                if data.get("ticker") != chosen:
                    reasons.append(f"config/{chosen}.json 的 ticker 字段不匹配")
                if not _has_valid_sources_shape(data):
                    reasons.append(f"config/{chosen}.json 的 sources 必须是对象")
                card = data.get("card")
                if not isinstance(card, dict):
                    reasons.append(f"config/{chosen}.json 缺少 card 配置")
                else:
                    missing_keys = [
                        k for k in CARD_STYLE_KEYS if not isinstance(card.get(k), str) or not card.get(k)
                    ]
                    if missing_keys:
                        reasons.append(
                            f"config/{chosen}.json 缺少 card 样式字段（{', '.join(missing_keys)}）"
                        )

    if ref_path.is_symlink() or not ref_path.is_file() or not _inside(base, ref_path):
        reasons.append(f"缺少 {ref_rel}")

    if not reasons:
        return f"不支持的 ticker {chosen!r}"
    return f"不支持的 ticker {chosen!r}：{'，'.join(reasons)}"


def supported_tickers(skill_dir: Path | None = None) -> tuple[str, ...]:
    """Return sorted tickers with valid config/<T>.json (ticker, dict sources & full card) and references/<t>-rules.md."""
    base = Path(skill_dir) if skill_dir is not None else SKILL_DIR
    config_dir = base / "config"
    if not config_dir.is_dir():
        return ()

    cache_key = os.fspath(base.resolve())
    sig = _dir_signature(base)
    cached = _SUPPORTED_CACHE.get(cache_key)
    if cached is not None and cached[0] == sig:
        return cached[1]

    found: list[str] = []
    for path in sorted(config_dir.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        stem = path.stem.strip().upper()
        if not stem or path.stem != stem:
            continue
        if load_validated_config(stem, skill_dir=base) is None:
            continue
        found.append(stem)

    result = tuple(found)
    _SUPPORTED_CACHE[cache_key] = (sig, result)
    return result


def bullbear_report_profiles(skill_dir: Path | None = None) -> tuple[str, ...]:
    """Return `<ticker>-tracker` profile names for all supported bull/bear tickers."""
    return tuple(
        f"{ticker.lower()}-tracker"
        for ticker in supported_tickers(skill_dir=skill_dir)
    )
