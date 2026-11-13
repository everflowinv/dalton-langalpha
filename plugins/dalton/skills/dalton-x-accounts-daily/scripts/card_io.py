"""Load the shared Card Schema 2.0 builder and write one card file.

The builder is ``markdown_to_card_elements`` from the bull/bear tracker skill.
This module does not reimplement rendering. It only checks the workspace path,
rejects recipient markers and local paths, and refuses to overwrite a different card.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

_FORBIDDEN_KEYS = frozenset(
    {
        "chat" + "_id",
        "open_id",
        "receive_id",
        "union_id",
        "user_id",
        "email",
    }
)
_NEEDLES = (
    "o" + "c_",
    "disc" + "ord:user",
    "/Users" + "/",
    "file:" + "//",
)


def find_renderer() -> Path:
    """Return the shared ``render_card.py``. Raises ``FileNotFoundError`` if it is absent."""
    here = Path(__file__).resolve()
    skill_rel = Path("dalton-bullbear-tracker") / "scripts" / "render_card.py"
    candidates = [here.parents[2] / skill_rel]
    for parent in here.parents:
        candidates.append(parent / "plugins" / "dalton" / "skills" / skill_rel)
        candidates.append(parent / "skills" / skill_rel)
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise FileNotFoundError("shared card renderer is not available")


def load_renderer_module() -> Any:
    path = find_renderer()
    name = "dalton_markdown_card_" + Path(__file__).resolve().parents[1].name.replace("-", "_")
    cached = sys.modules.get(name)
    if cached is not None and getattr(cached, "__file__", None) == str(path):
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError("shared card renderer is not available")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def workspace_file(workspace: Path, relative: str) -> Path:
    """Resolve a workspace-relative file and reject links and path escape."""
    raw = Path(relative)
    if raw.is_absolute() or not raw.parts or any(part in ("", ".", "..") for part in raw.parts):
        raise ValueError("path must stay inside the workspace")
    path = workspace.joinpath(*raw.parts)
    if path.is_symlink():
        raise ValueError("refusing symlink")
    parent = path.parent.resolve()
    root = workspace.resolve()
    if parent != root and root not in parent.parents:
        raise ValueError("path must stay inside the workspace")
    return path


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("incomplete write")
        view = view[written:]


def write_bytes_exclusive(path: Path, payload: bytes) -> None:
    """Replace ``path`` via a new temp file so an existing symlink is not followed."""
    if path.is_symlink():
        raise ValueError("refusing symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        try:
            _write_all(fd, payload)
        finally:
            os.close(fd)
        os.replace(name, path)
    except OSError:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def write_bytes_new(path: Path, payload: bytes) -> bool:
    """Create ``path`` atomically, never replacing an existing file.

    Returns False when the file already exists (another run created it first).
    """
    if path.is_symlink():
        raise ValueError("refusing symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        try:
            _write_all(fd, payload)
        finally:
            os.close(fd)
        try:
            os.link(name, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def _scan(value: Any) -> None:
    if isinstance(value, str):
        for needle in _NEEDLES:
            if needle in value:
                raise ValueError("card failed content check")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("card failed content check")
            if key.casefold() in _FORBIDDEN_KEYS:
                raise ValueError("card failed content check")
            _scan(item)
        return
    if isinstance(value, list):
        for item in value:
            _scan(item)
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    raise TypeError("card failed content check")


def render_markdown(
    *,
    workspace: Path,
    markdown_rel: str,
    output_rel: str,
    title: str,
    subtitle: str = "",
) -> dict[str, Any]:
    """Write one Card Schema 2.0 file. Identical bytes are reused; different bytes are kept."""
    if not title.strip():
        raise ValueError("title is empty")
    source = workspace_file(workspace, markdown_rel)
    output = workspace_file(workspace, output_rel)
    if source == output:
        raise ValueError("card output must differ from the markdown file")
    if not source.is_file():
        raise ValueError("markdown file not found")
    markdown = source.read_text(encoding="utf-8")
    module = load_renderer_module()
    if hasattr(module, "card_markdown"):
        markdown = module.card_markdown(markdown)
    builder = module.markdown_to_card_elements
    kwargs: dict[str, Any] = {"markdown": markdown, "title": title}
    parameters = inspect.signature(builder).parameters
    if "subtitle" in parameters:
        kwargs["subtitle"] = subtitle
    if "header_template" in parameters:
        kwargs["header_template"] = "indigo"
    card = builder(**kwargs)
    if not isinstance(card, dict):
        raise TypeError("card schema is not 2.0")
    if card.get("schema") != "2.0":
        raise ValueError("card schema is not 2.0")
    elements = (card.get("body") or {}).get("elements") if isinstance(card.get("body"), dict) else None
    if not isinstance(elements, list):
        raise TypeError("card body is empty")
    if not elements:
        raise ValueError("card body is empty")
    _scan(card)
    if hasattr(module, "dump_card"):
        text = module.dump_card(card)
    else:
        text = json.dumps(card, ensure_ascii=False, indent=2) + "\n"
    encoded = text.encode("utf-8")
    if not output.exists() and write_bytes_new(output, encoded):
        return {"ok": True, "reused": False, "path": output_rel, "card": card}
    # The card already exists, or another run created it between the check and the write.
    if output.is_symlink():
        raise ValueError("refusing symlink")
    if output.read_bytes() != encoded:
        return {
            "ok": False,
            "error": "card_conflict",
            "hint": (
                "已有卡片与本次内容不同，文件保持原样。"
                "禁止删除或覆盖已有卡片文件（不要 unlink，也不要换路径重写）。"
                "先读已保存的正文（summary.md）、卡片（card.json）和 "
                "automation_report(action=\"status\", report_run_id=RUN_ID) 的发送状态；"
                "若 status=\"sent\" 且 delivery_confirmed=true，最终回复写 NO_REPLY；"
                "若尚未确认，保持 start_iso 与 end_iso，用已保存的 card.json 和同一 "
                "report_run_id（x-accounts-daily-<summary_date>）提交。"
            ),
            "path": output_rel,
        }
    return {"ok": True, "reused": True, "path": output_rel, "card": card}
