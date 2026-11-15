"""Backend tools `podcast_weekly_read` and `podcast_weekly_write` (internal/admin only)."""

from __future__ import annotations

import base64
import json
import logging
import posixpath
import re
import shlex
from pathlib import Path
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.access_tiers import is_internal_or_admin
from src.dalton.feishu import is_uncertain_bridge_error
from src.dalton.podcast_weekly_store import (
    DEFAULT_COLLECT_ARTIFACTS,
    PodcastStoreSecurityError,
    PublishInProgressError,
    RecordUnreadableError,
    abandon_pending_report,
    abandon_uncertain_doc,
    clear_in_flight_write,
    doc_record_path,
    episode_publish_lock,
    get_status,
    load_stage_artifacts,
    mark_in_flight_write,
    mark_reported,
    prepare_doc,
    read_artifact,
    read_json_dict,
    record_chunk,
    record_failure,
    resolve_collect_targets,
    resolve_host_openclaw_data_root,
    resolve_in_flight_write,
    save_doc,
    save_pending_report,
    validate_video_id,
    write_artifact,
)
from src.dalton.tools.conversation_tools import _extract_caller_identity
from src.dalton.tools.feishu_tools import (
    append_feishu_doc_on_server,
    create_feishu_doc_on_server,
)

logger = logging.getLogger(__name__)


async def _authorize_internal(tool_name: str, config: RunnableConfig | None) -> str | None:
    user_id, workspace_id, thread_id, _run_id = _extract_caller_identity(config)
    if not user_id:
        logger.warning(
            "[dalton.podcast_weekly] rejected unauthenticated %s (workspace_id=%s thread_id=%s)",
            tool_name,
            workspace_id or "-",
            thread_id or "-",
        )
        return json.dumps(
            {
                "ok": False,
                "error": "unauthenticated",
                "message": f"{tool_name} requires an authenticated internal or admin user.",
            },
            ensure_ascii=False,
        )
    if not await is_internal_or_admin(user_id):
        logger.warning(
            "[dalton.podcast_weekly] rejected external %s (user_id=%s)",
            tool_name,
            user_id,
        )
        return json.dumps(
            {
                "ok": False,
                "error": "not_allowed",
                "message": f"{tool_name} is restricted to internal and admin members.",
            },
            ensure_ascii=False,
        )
    return None


def _resolve_sandbox_backend(
    filesystem_backend: Any,
    config: RunnableConfig | None,
) -> Any:
    """Resolve the current thread's sandbox backend from closure or RunnableConfig."""
    if filesystem_backend is not None:
        return filesystem_backend
    if isinstance(config, dict):
        configurable = config.get("configurable")
        if isinstance(configurable, dict):
            return configurable.get("filesystem_backend")
    return None


def _sandbox_rel_dir(video_id: str) -> str:
    return f".work/podcast-weekly/{video_id}"


def _resolve_sandbox_workspace_paths(
    backend: Any,
    rel_dir: str,
) -> tuple[str | None, str | None, list[str] | None, str | None]:
    """Resolve `(ws_root, abs_ep_dir, rel_parts, error)` anchored at the workspace dir rather than computer root."""
    ws_attr = getattr(backend, "workspace_dir", None)
    rd_attr = getattr(backend, "root_dir", None)
    ws_root: str | None = None
    if isinstance(ws_attr, str) and ws_attr.strip().startswith("/"):
        ws_root = posixpath.normpath(ws_attr.strip()).rstrip("/") or "/"
    elif isinstance(rd_attr, str) and rd_attr.strip().startswith("/"):
        ws_root = posixpath.normpath(rd_attr.strip()).rstrip("/") or "/"

    abs_ep_dir: str | None = None
    norm_fn = getattr(backend, "normalize_path", None)
    if callable(norm_fn):
        norm_val = norm_fn(rel_dir)
        if isinstance(norm_val, str) and norm_val.strip().startswith("/"):
            abs_ep_dir = posixpath.normpath(norm_val.strip()).rstrip("/")
            if ws_root is None and abs_ep_dir.endswith(f"/{rel_dir}"):
                ws_root = abs_ep_dir[: -len(f"/{rel_dir}")] or "/"

    if not ws_root:
        return None, None, None, "无法解析沙箱工作区的绝对路径（缺少 workspace_dir 或 root_dir）"

    if abs_ep_dir is None:
        abs_ep_dir = posixpath.normpath(f"{ws_root.rstrip('/')}/{rel_dir}")

    try:
        common = posixpath.commonpath([ws_root, abs_ep_dir])
    except ValueError:
        return None, None, None, "沙箱工作区路径与期次目录路径不兼容"

    if common != ws_root or abs_ep_dir == ws_root:
        return None, None, None, f"沙箱目标目录越出工作区边界: {abs_ep_dir}"

    rel_tail = posixpath.relpath(abs_ep_dir, ws_root)
    rel_parts = [p for p in rel_tail.split("/") if p]
    if not rel_parts or any(p in {".", ".."} for p in rel_parts):
        return None, None, None, f"沙箱相对目录无效: {rel_tail}"
    return ws_root, abs_ep_dir, rel_parts, None


async def _run_sandbox_guard(
    backend: Any,
    *,
    mode: str,
    ws_root: str,
    rel_parts: list[str],
    files: list[str],
) -> dict[str, Any]:
    """Run path-only symlink/realpath verification (and stale file cleanup on stage) inside the sandbox."""
    exec_fn = getattr(backend, "aexecute", None)
    if not callable(exec_fn):
        return {
            "ok": False,
            "error": "sandbox_execute_unavailable",
            "message": "沙箱不支持命令执行（aexecute），无法校验路径安全",
        }

    req_b64 = base64.b64encode(
        json.dumps(
            {
                "mode": mode,
                "ws_root": ws_root,
                "rel_parts": rel_parts,
                "files": files,
            },
            ensure_ascii=False,
        ).encode("utf-8")
    ).decode("ascii")

    script = (
        "import base64, json, os, shutil, stat, sys\n"
        f"req = json.loads(base64.b64decode('{req_b64}').decode('utf-8'))\n"
        "ws = req['ws_root']\n"
        "parts = req['rel_parts']\n"
        "files = req['files']\n"
        "mode = req['mode']\n"
        "def fail(code, msg):\n"
        "    print(json.dumps({'ok': False, 'error': code, 'message': msg}))\n"
        "    sys.exit(0)\n"
        "if not os.path.lexists(ws):\n"
        "    fail('workspace_missing', f'沙箱工作区不存在: {ws}')\n"
        "ws_st = os.lstat(ws)\n"
        "if stat.S_ISLNK(ws_st.st_mode):\n"
        "    fail('symlink_rejected', f'沙箱工作区根目录为符号链接: {ws}')\n"
        "if not stat.S_ISDIR(ws_st.st_mode):\n"
        "    fail('invalid_workspace', f'沙箱工作区路径不是目录: {ws}')\n"
        "resolved_root = os.path.realpath(ws)\n"
        "cur = ws\n"
        "for part in parts:\n"
        "    cur = os.path.join(cur, part)\n"
        "    if os.path.lexists(cur):\n"
        "        st = os.lstat(cur)\n"
        "        if stat.S_ISLNK(st.st_mode):\n"
        "            fail('symlink_rejected', f'沙箱路径包含符号链接: {cur}')\n"
        "        if not stat.S_ISDIR(st.st_mode):\n"
        "            fail('not_a_directory', f'沙箱路径不是目录: {cur}')\n"
        "    elif mode == 'prepare_stage':\n"
        "        os.mkdir(cur, 0o755)\n"
        "    else:\n"
        "        rejected = {f: {'error': 'sandbox_dir_missing', 'message': f'沙箱期次目录不存在: {cur}'} for f in files}\n"
        "        print(json.dumps({'ok': True, 'dir_missing': True, 'verified': [], 'rejected': rejected}))\n"
        "        sys.exit(0)\n"
        "resolved_dir = os.path.realpath(cur)\n"
        "expected_dir = os.path.join(resolved_root, *parts)\n"
        "if resolved_dir != expected_dir or not resolved_dir.startswith(resolved_root + os.sep):\n"
        "    fail('path_escape', f'沙箱期次目录越出工作区: {resolved_dir}')\n"
        "if mode == 'prepare_stage':\n"
        "    keep = set(files)\n"
        "    cleaned = []\n"
        "    for entry in os.scandir(cur):\n"
        "        if entry.name in keep:\n"
        "            if entry.is_symlink():\n"
        "                fail('symlink_rejected', f'目标产物路径为符号链接: {entry.path}')\n"
        "            if not entry.is_file(follow_symlinks=False):\n"
        "                fail('not_regular_file', f'目标产物路径不是普通文件: {entry.path}')\n"
        "        else:\n"
        "            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):\n"
        "                os.unlink(entry.path)\n"
        "            else:\n"
        "                shutil.rmtree(entry.path)\n"
        "            cleaned.append(entry.name)\n"
        "    print(json.dumps({'ok': True, 'resolved_root': resolved_root, 'resolved_dir': resolved_dir, 'cleaned_files': sorted(cleaned)}))\n"
        "else:\n"
        "    verified = []\n"
        "    rejected = {}\n"
        "    for fname in files:\n"
        "        fpath = os.path.join(cur, fname)\n"
        "        if os.path.lexists(fpath):\n"
        "            fst = os.lstat(fpath)\n"
        "            if stat.S_ISLNK(fst.st_mode):\n"
        "                rejected[fname] = {'error': 'symlink_rejected', 'message': f'拒绝读取符号链接文件: {fname}'}\n"
        "            elif not stat.S_ISREG(fst.st_mode):\n"
        "                rejected[fname] = {'error': 'not_regular_file', 'message': f'沙箱目标不是普通文件: {fname}'}\n"
        "            elif os.path.realpath(fpath) != os.path.join(resolved_dir, fname):\n"
        "                rejected[fname] = {'error': 'path_escape', 'message': f'沙箱目标路径越界: {fname}'}\n"
        "            else:\n"
        "                verified.append(fname)\n"
        "        else:\n"
        "            verified.append(fname)\n"
        "    print(json.dumps({'ok': True, 'dir_missing': False, 'resolved_root': resolved_root, 'resolved_dir': resolved_dir, 'verified': verified, 'rejected': rejected}))\n"
    )

    res = await exec_fn(f"python3 -c {shlex.quote(script)}")
    exit_code = getattr(res, "exit_code", None)
    output = str(getattr(res, "output", "") or "").strip()
    if exit_code != 0 or not output:
        return {
            "ok": False,
            "error": "sandbox_guard_failed",
            "message": f"沙箱路径安全检查失败 (退出码={exit_code}): {output[:300]}",
        }
    try:
        parsed = json.loads(output.splitlines()[-1])
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": "sandbox_guard_invalid_output",
            "message": f"沙箱路径安全检查输出无法解析: {exc}",
        }
    if not isinstance(parsed, dict):
        return {
            "ok": False,
            "error": "sandbox_guard_invalid_output",
            "message": "沙箱路径安全检查输出不是 JSON 对象",
        }
    return parsed


def _extract_upload_error(res: Any) -> str | None:
    if res is False:
        return "上传文件到沙箱失败（返回 False）"
    if isinstance(res, list):
        for item in res:
            err = getattr(item, "error", None)
            if err:
                path_label = getattr(item, "path", None) or "文件"
                return f"上传文件到沙箱失败（{path_label}）: {err}"
    return None


async def _upload_sandbox_files(
    backend: Any,
    abs_ep_dir: str,
    uploads: list[tuple[str, bytes]],
) -> str | None:
    """Write `(rel_path, content_bytes)` files into the sandbox via backend APIs."""
    upload_fn = getattr(backend, "aupload_files", None)
    write_text_fn = getattr(backend, "awrite_text", None)

    if callable(upload_fn):
        res = await upload_fn(uploads)
        err = _extract_upload_error(res)
        if err:
            # Daytona refuses upload_files when the parent directory does not yet exist;
            # create it at the resolved absolute workspace directory rather than a relative path.
            exec_fn = getattr(backend, "aexecute", None)
            if callable(exec_fn):
                mkdir_res = await exec_fn(f"mkdir -p {shlex.quote(abs_ep_dir)}")
                if isinstance(getattr(mkdir_res, "exit_code", None), int) and mkdir_res.exit_code == 0:
                    retry_res = await upload_fn(uploads)
                    err = _extract_upload_error(retry_res)
        return err

    if callable(write_text_fn):
        for rel_path, content_bytes in uploads:
            wrote = await write_text_fn(rel_path, content_bytes.decode("utf-8"))
            if wrote is False:
                return f"写入沙箱文件失败: {rel_path}"
        return None

    return "当前沙箱不支持文件上传或文本写入"


async def _download_sandbox_files(
    backend: Any,
    paths: list[str],
) -> tuple[dict[str, bytes], dict[str, str]]:
    """Read raw file bytes from the sandbox via `adownload_files` with single-file fallbacks."""
    downloaded: dict[str, bytes] = {}
    errors: dict[str, str] = {}
    if not paths:
        return downloaded, errors

    download_files_fn = getattr(backend, "adownload_files", None)
    if callable(download_files_fn):
        try:
            raw_list = await download_files_fn(paths)
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            return {}, {p: msg for p in paths}

        if isinstance(raw_list, list):
            for idx, item in enumerate(raw_list):
                item_path = getattr(item, "path", None) or (paths[idx] if idx < len(paths) else None)
                if not item_path:
                    continue
                err = getattr(item, "error", None)
                content = getattr(item, "content", None)
                if err:
                    errors[str(item_path)] = str(err)
                elif isinstance(content, (bytes, bytearray)):
                    downloaded[str(item_path)] = bytes(content)
                elif isinstance(content, str):
                    downloaded[str(item_path)] = content.encode("utf-8")
                else:
                    errors[str(item_path)] = "沙箱文件为空或不存在"
            for p in paths:
                if p not in downloaded and p not in errors:
                    errors[p] = "沙箱未返回该文件"
            return downloaded, errors

    download_bytes_fn = getattr(backend, "adownload_file_bytes", None)
    read_text_fn = getattr(backend, "aread_text", None)
    for p in paths:
        err_msg: str | None = None
        if callable(download_bytes_fn):
            try:
                raw_bytes = await download_bytes_fn(p)
                if isinstance(raw_bytes, (bytes, bytearray)):
                    downloaded[p] = bytes(raw_bytes)
                    continue
            except Exception as exc:
                err_msg = f"{type(exc).__name__}: {exc}"
        if callable(read_text_fn):
            try:
                txt = await read_text_fn(p)
                if isinstance(txt, str):
                    downloaded[p] = txt.encode("utf-8")
                    continue
            except Exception as exc:
                err_msg = f"{type(exc).__name__}: {exc}"
        errors[p] = err_msg or "沙箱文件缺失或不可读"

    return downloaded, errors


def _extract_wiki_node_token(doc_url: str) -> str:
    raw = str(doc_url or "").strip()
    if not raw:
        return ""
    match = re.search(r"/wiki/([^/?#]+)", raw)
    return match.group(1).strip() if match else ""


def _derive_feishu_doc_url(res: dict[str, Any]) -> str:
    """Resolve Feishu doc URL from explicit `doc_url`/`url` or fall back to `node_token`/`document_id`."""
    explicit = str(res.get("doc_url") or res.get("url") or "").strip()
    if explicit:
        return explicit
    node_token = str(res.get("node_token") or "").strip()
    if node_token:
        return f"https://feishu.cn/wiki/{node_token}"
    doc_id = str(res.get("document_id") or "").strip()
    if doc_id:
        return f"https://feishu.cn/docx/{doc_id}"
    return ""


def _is_uncertain_write_error(res: dict[str, Any]) -> bool:
    if res.get("uncertain"):
        return True
    err_code = str(res.get("error") or "").strip()
    outcome = str(res.get("outcome") or "").strip()
    return is_uncertain_bridge_error(err_code, outcome=outcome)


def _format_image_bind_warning(res: dict[str, Any], *, part: int) -> str:
    images = res.get("images") if isinstance(res.get("images"), dict) else {}
    failed_items = images.get("failed") if isinstance(images.get("failed"), list) else []
    if not failed_items and isinstance(res.get("failed_images"), list):
        failed_items = list(res.get("failed_images") or [])
    refs: list[str] = []
    for item in failed_items:
        if isinstance(item, dict):
            label = str(
                item.get("ref")
                or item.get("path")
                or item.get("file")
                or item.get("block_id")
                or ""
            ).strip()
            reason = str(item.get("reason") or "").strip()
            if label and reason:
                refs.append(f"{label}({reason})")
            elif label:
                refs.append(label)
        elif str(item or "").strip():
            refs.append(str(item).strip())
    if refs:
        return f"第 {part} 段正文已写入飞书，但图片绑定失败（无需重发该段），请人工补图: {', '.join(refs)}"
    detail = str(res.get("message") or "").strip()
    if detail:
        return f"第 {part} 段正文已写入飞书，但图片绑定失败（无需重发该段），请人工补图（{detail}）"
    return f"第 {part} 段正文已写入飞书，但图片绑定失败（无需重发该段），请人工补图"


def _merge_warnings(*warnings: Any) -> Any:
    items = [w for w in warnings if w not in (None, "", [], {})]
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    rendered: list[str] = []
    for item in items:
        if isinstance(item, (dict, list)):
            rendered.append(json.dumps(item, ensure_ascii=False))
        else:
            rendered.append(str(item))
    return "；".join(rendered)


async def _append_chunks_sequentially(
    *,
    root: Path,
    video_id: str,
    doc_id: str,
    node_token: str = "",
    doc_url: str,
    total_parts: int,
    chunks: list[dict[str, Any]],
    filesystem_backend: Any,
    expected_doc_id: str | None = None,
    expected_doc_url: str | None = None,
) -> tuple[bool, list[int], dict[str, Any] | None, list[Any]]:
    appended_parts: list[int] = []
    warnings: list[Any] = []
    current_doc_id = doc_id
    exp_id: str | None = expected_doc_id if expected_doc_id is not None else doc_id
    exp_url: str | None = expected_doc_url
    target_token = current_doc_id or node_token or doc_url
    for chunk in chunks:
        part_num = int(chunk["part"])
        mark_res = mark_in_flight_write(
            root,
            video_id,
            operation="append",
            part=part_num,
            document_id=current_doc_id,
            doc_url=doc_url,
            expected_document_id=exp_id,
            expected_doc_url=exp_url,
        )
        if not mark_res.get("ok"):
            return False, appended_parts, mark_res, warnings

        append_res = await append_feishu_doc_on_server(
            token=target_token,
            markdown=str(chunk["content"]),
            filesystem_backend=filesystem_backend,
        )
        is_img_fail = str(append_res.get("error") or "").strip().lower() == "image_bind_failed"
        if not append_res.get("ok") and not is_img_fail:
            if _is_uncertain_write_error(append_res):
                return (
                    False,
                    appended_parts,
                    {**append_res, "uncertain": True, "failed_part": part_num},
                    warnings,
                )
            clear_res = clear_in_flight_write(
                root,
                video_id,
                expected_document_id=exp_id,
                expected_doc_url=exp_url,
            )
            if not clear_res.get("ok"):
                return False, appended_parts, clear_res, warnings
            return False, appended_parts, append_res, warnings

        resolved_doc_id = str(append_res.get("document_id") or "").strip()
        next_doc_id = current_doc_id or resolved_doc_id
        rec_res = record_chunk(
            root,
            video_id,
            part=part_num,
            total_parts=total_parts,
            document_id=next_doc_id,
            doc_url=doc_url,
            expected_document_id=exp_id,
            expected_doc_url=exp_url,
        )
        if not rec_res.get("ok"):
            return False, appended_parts, rec_res, warnings
        current_doc_id = str(rec_res.get("document_id") or next_doc_id).strip()
        target_token = current_doc_id or node_token or doc_url
        exp_id = current_doc_id
        exp_url = None if exp_id else exp_url
        if is_img_fail:
            warnings.append(_format_image_bind_warning(append_res, part=part_num))
        elif append_res.get("warning"):
            warnings.append(append_res.get("warning"))
        appended_parts.append(part_num)
    return True, appended_parts, None, warnings


async def _handle_stage(
    root: Path,
    backend: Any,
    video_id: str | None,
) -> dict[str, Any]:
    if not video_id:
        return {"ok": False, "error": "missing_video_id", "message": "stage 需要 video_id"}
    if backend is None:
        return {
            "ok": False,
            "error": "sandbox_unavailable",
            "message": "当前线程沙箱不可用，无法将宿主机产物放入沙箱",
        }

    loaded = load_stage_artifacts(root, video_id)
    if not loaded.get("ok"):
        return loaded

    safe_vid = loaded["video_id"]
    rel_dir = _sandbox_rel_dir(safe_vid)
    ws_root, abs_ep_dir, rel_parts, path_err = _resolve_sandbox_workspace_paths(backend, rel_dir)
    if path_err or ws_root is None or abs_ep_dir is None or rel_parts is None:
        return {
            "ok": False,
            "error": "sandbox_path_invalid",
            "video_id": safe_vid,
            "sandbox_dir": rel_dir,
            "message": path_err or "沙箱路径无效",
        }

    keep_filenames = [item["filename"] for item in loaded["items"]]
    guard = await _run_sandbox_guard(
        backend,
        mode="prepare_stage",
        ws_root=ws_root,
        rel_parts=rel_parts,
        files=keep_filenames,
    )
    if not guard.get("ok"):
        return {
            "ok": False,
            "error": str(guard.get("error") or "sandbox_guard_failed"),
            "video_id": safe_vid,
            "sandbox_dir": rel_dir,
            "message": str(guard.get("message") or "沙箱路径安全检查失败"),
        }

    uploads = [(f"{rel_dir}/{item['filename']}", item["content_bytes"]) for item in loaded["items"]]
    upload_err = await _upload_sandbox_files(backend, abs_ep_dir, uploads)
    if upload_err:
        return {
            "ok": False,
            "error": "sandbox_upload_failed",
            "video_id": safe_vid,
            "sandbox_dir": rel_dir,
            "message": upload_err,
        }

    files: dict[str, dict[str, Any]] = {}
    for item in loaded["items"]:
        files[item["artifact"]] = {
            "path": f"{rel_dir}/{item['filename']}",
            "filename": item["filename"],
            "bytes": item["bytes"],
            "sha256": item["sha256"],
        }
    return {
        "ok": True,
        "video_id": safe_vid,
        "sandbox_dir": rel_dir,
        "cleaned_files": list(guard.get("cleaned_files") or []),
        "files": files,
    }


async def _handle_collect(
    root: Path,
    backend: Any,
    video_id: str | None,
    artifacts: list[str] | None,
) -> dict[str, Any]:
    if not video_id:
        return {"ok": False, "error": "missing_video_id", "message": "collect 需要 video_id"}
    if backend is None:
        return {
            "ok": False,
            "error": "sandbox_unavailable",
            "message": "当前线程沙箱不可用，无法从沙箱读取产物",
        }

    safe_vid = validate_video_id(video_id)
    raw_list = list(DEFAULT_COLLECT_ARTIFACTS) if not artifacts else list(dict.fromkeys(artifacts))
    valid_targets, invalid_errors = resolve_collect_targets(root, safe_vid, raw_list)

    collected: dict[str, dict[str, Any]] = {}
    failed: dict[str, dict[str, str]] = dict(invalid_errors)
    rel_dir = _sandbox_rel_dir(safe_vid)

    if valid_targets:
        ws_root, _abs_ep_dir, rel_parts, path_err = _resolve_sandbox_workspace_paths(backend, rel_dir)
        if path_err or ws_root is None or rel_parts is None:
            for art, fname in valid_targets.items():
                failed[art] = {
                    "error": "sandbox_path_invalid",
                    "path": f"{rel_dir}/{fname}",
                    "message": path_err or "沙箱路径无效",
                }
        else:
            guard = await _run_sandbox_guard(
                backend,
                mode="verify_collect",
                ws_root=ws_root,
                rel_parts=rel_parts,
                files=list(valid_targets.values()),
            )
            if not guard.get("ok"):
                err_code = str(guard.get("error") or "sandbox_guard_failed")
                err_msg = str(guard.get("message") or "沙箱路径安全检查失败")
                for art, fname in valid_targets.items():
                    failed[art] = {
                        "error": err_code,
                        "path": f"{rel_dir}/{fname}",
                        "message": err_msg,
                    }
            else:
                guard_rejected = guard.get("rejected") or {}
                safe_targets: dict[str, str] = {}
                for art, fname in valid_targets.items():
                    rel_path = f"{rel_dir}/{fname}"
                    if fname in guard_rejected:
                        rej = guard_rejected[fname]
                        failed[art] = {
                            "error": str(rej.get("error") or "sandbox_security_rejected"),
                            "path": rel_path,
                            "message": str(rej.get("message") or "沙箱路径被安全策略拒绝"),
                        }
                    else:
                        safe_targets[art] = rel_path

                if safe_targets:
                    downloaded, dl_errors = await _download_sandbox_files(backend, list(safe_targets.values()))
                    for art, rel_path in safe_targets.items():
                        if rel_path in dl_errors:
                            failed[art] = {
                                "error": "sandbox_read_failed",
                                "path": rel_path,
                                "message": dl_errors[rel_path],
                            }
                            continue
                        try:
                            text = downloaded[rel_path].decode("utf-8")
                            res = write_artifact(
                                root,
                                safe_vid,
                                art,
                                text,
                                target_filename=valid_targets[art],
                            )
                        except UnicodeDecodeError as exc:
                            failed[art] = {
                                "error": "invalid_utf8",
                                "path": rel_path,
                                "message": f"UTF-8 解码失败: {exc}",
                            }
                            continue
                        except (
                            PodcastStoreSecurityError,
                            ValueError,
                            json.JSONDecodeError,
                            OSError,
                        ) as exc:
                            failed[art] = {
                                "error": type(exc).__name__,
                                "path": rel_path,
                                "message": str(exc),
                            }
                            continue

                        if not res.get("ok"):
                            failed[art] = {
                                "error": str(res.get("error") or "write_failed"),
                                "path": rel_path,
                                "message": str(res.get("message") or "写回宿主机失败"),
                            }
                            continue
                        collected[art] = {
                            "artifact": art,
                            "sandbox_path": rel_path,
                            "filename": valid_targets[art],
                            "bytes": res["bytes"],
                            "sha256": res["sha256"],
                        }

    all_ok = len(failed) == 0 and len(collected) > 0
    out: dict[str, Any] = {
        "ok": all_ok,
        "video_id": safe_vid,
        "sandbox_dir": rel_dir,
        "collected": collected,
        "failed": failed,
        "partial": bool(collected and failed),
    }
    if failed:
        reasons = "; ".join(f"{k}: {v['message']}" for k, v in failed.items())
        out["message"] = f"产物收集失败 ({len(failed)}/{len(collected) + len(failed)}): {reasons}"
    return out


def _abandon_on_uncertainty(
    root: Path,
    video_id: str,
    *,
    doc_id: str,
    doc_url: str,
    reason: str,
    failed_part: int | None = None,
    total_parts: int = 1,
    appended_parts: list[int] | None = None,
    zh_title: str = "",
    share_warning: Any = None,
    expected_document_id: str | None = None,
    expected_doc_url: str | None = None,
) -> dict[str, Any]:
    abandoned = abandon_uncertain_doc(
        root,
        video_id,
        document_id=doc_id,
        doc_url=doc_url,
        reason=reason,
        expected_document_id=expected_document_id,
        expected_doc_url=expected_doc_url,
    )
    if abandoned.get("code") == "doc_identity_mismatch":
        return abandoned
    stopped = bool(abandoned["stopped"])
    code = str(abandoned["code"])
    if stopped:
        msg = "该期飞书文档已连续 2 次写入结果不确定并作废，已停止自动重建，请人工核对作废文档链接"
    elif failed_part == 1:
        msg = "建档首段写入结果不确定，已将该文档标为作废并清空进度，下次运行将自动重建文档"
    else:
        part_label = f"第 {failed_part} 段" if failed_part else "正文分块"
        msg = f"{part_label}写入结果不确定，已将该文档标为作废并清空进度，下次运行将自动重建文档"
    out: dict[str, Any] = {
        "ok": False,
        "video_id": video_id,
        "already_exists": False,
        "code": code,
        "error": code,
        "stopped": stopped,
        "message": msg,
        "document_id": "",
        "doc_url": "",
        "abandoned_document": abandoned["abandoned_document"],
        "abandoned_documents": abandoned["abandoned_documents"],
        "total_parts": total_parts,
        "appended_parts": list(appended_parts or []),
    }
    if zh_title:
        out["zh_title"] = zh_title
    if share_warning:
        out["warning"] = share_warning
    return out


async def _handle_publish_doc(
    root: Path,
    backend: Any,
    user_id: str,
    video_id: str | None,
    date: str | None,
) -> dict[str, Any]:
    if not video_id:
        return {"ok": False, "error": "missing_video_id", "message": "publish_doc 需要 video_id"}

    safe_vid = validate_video_id(video_id)
    try:
        with episode_publish_lock(root, safe_vid):
            return await _handle_publish_doc_locked(
                root,
                backend,
                user_id,
                safe_vid,
                date,
            )
    except PublishInProgressError:
        return {
            "ok": False,
            "video_id": safe_vid,
            "already_exists": False,
            "code": "publish_in_progress",
            "error": "publish_in_progress",
            "message": f"期次 {safe_vid} 当前正在写入飞书文档（publish_in_progress），请稍后再试",
        }


async def _handle_publish_doc_locked(
    root: Path,
    backend: Any,
    user_id: str,
    safe_vid: str,
    date: str | None,
) -> dict[str, Any]:
    try:
        inflight_res = resolve_in_flight_write(root, safe_vid)
    except RecordUnreadableError as exc:
        return {
            "ok": False,
            "video_id": safe_vid,
            "already_exists": False,
            "reason": "doc_record_unreadable",
            "error": f"文档状态文件损坏或不可读: {exc}",
        }

    if inflight_res.get("had_in_flight"):
        abandoned = dict(inflight_res.get("abandoned") or {})
        if abandoned.get("code") == "doc_identity_mismatch":
            return abandoned
        if abandoned.get("stopped"):
            return {
                "ok": False,
                "video_id": safe_vid,
                "already_exists": False,
                "code": "doc_uncertain_twice",
                "error": "doc_uncertain_twice",
                "stopped": True,
                "message": "检测到上次正在写入的未完成记录，且该期已连续 2 次写入结果不确定并作废，已停止自动重建，请人工核对作废文档链接",
                "document_id": "",
                "doc_url": "",
                "abandoned_document": abandoned.get("abandoned_document"),
                "abandoned_documents": abandoned.get("abandoned_documents") or [],
            }

    prep = prepare_doc(root, safe_vid, max_chars=25000, date_str=date)

    if prep.get("ok") is False:
        out = dict(prep)
        out.setdefault("error", str(prep.get("reason") or "prepare_doc_failed"))
        out.setdefault("already_exists", False)
        if out.get("reason") == "doc_create_failed_twice":
            out.setdefault("stopped", True)
        return out

    if prep.get("already_exists"):
        total_parts = 1
        try:
            rec = read_json_dict(doc_record_path(root, safe_vid), required=False)
            total_parts = int(rec.get("total_parts") or 1)
        except (OSError, json.JSONDecodeError, RecordUnreadableError):
            total_parts = 1
        return {
            "ok": True,
            "video_id": safe_vid,
            "already_exists": True,
            "document_id": str(prep.get("document_id") or ""),
            "doc_url": str(prep.get("doc_url") or ""),
            "zh_title": str(prep.get("zh_title") or ""),
            "total_parts": total_parts,
            "appended_parts": [],
        }

    if prep.get("resume_append"):
        doc_id = str(prep.get("document_id") or "").strip()
        raw_doc_url = str(prep.get("doc_url") or "").strip()
        doc_url = _derive_feishu_doc_url({"doc_url": raw_doc_url, "document_id": doc_id})
        node_token = _extract_wiki_node_token(doc_url)
        total_parts = int(prep.get("total_parts") or 1)
        remaining = list(prep.get("remaining_chunks") or [])
        exp_doc_id = doc_id
        exp_doc_url = None if doc_id else raw_doc_url
        append_warnings: list[Any] = []

        if remaining:
            ok, appended_parts, err_res, append_warnings = await _append_chunks_sequentially(
                root=root,
                video_id=safe_vid,
                doc_id=doc_id,
                node_token=node_token,
                doc_url=doc_url,
                total_parts=total_parts,
                chunks=remaining,
                filesystem_backend=backend,
                expected_doc_id=exp_doc_id,
                expected_doc_url=exp_doc_url,
            )
            if not doc_id:
                rec = read_json_dict(doc_record_path(root, safe_vid), required=False)
                doc_id = str(rec.get("document_id") or "").strip()
            if not ok:
                err_dict = err_res or {}
                if err_dict.get("code") == "doc_identity_mismatch":
                    return err_dict
                merged_warn = _merge_warnings(*append_warnings)
                if _is_uncertain_write_error(err_dict):
                    return _abandon_on_uncertainty(
                        root,
                        safe_vid,
                        doc_id=doc_id,
                        doc_url=doc_url,
                        reason=str(
                            err_dict.get("detail")
                            or err_dict.get("message")
                            or err_dict.get("error")
                            or "append_outcome_unknown"
                        ),
                        failed_part=int(err_dict["failed_part"]) if err_dict.get("failed_part") else None,
                        total_parts=total_parts,
                        appended_parts=appended_parts,
                        share_warning=merged_warn,
                        expected_document_id=exp_doc_id,
                        expected_doc_url=exp_doc_url,
                    )
                out_resume_err: dict[str, Any] = {
                    "ok": False,
                    "video_id": safe_vid,
                    "already_exists": False,
                    "error": str(err_dict.get("error") or "append_failed"),
                    "message": str(err_dict.get("message") or "断点续传追加飞书正文失败"),
                    "document_id": doc_id,
                    "doc_url": doc_url,
                    "total_parts": total_parts,
                    "appended_parts": appended_parts,
                }
                if merged_warn:
                    out_resume_err["warning"] = merged_warn
                return out_resume_err
        else:
            appended_parts = []

        out_resume_ok: dict[str, Any] = {
            "ok": True,
            "video_id": safe_vid,
            "already_exists": False,
            "document_id": doc_id,
            "doc_url": doc_url,
            "total_parts": total_parts,
            "appended_parts": appended_parts,
        }
        merged_warn = _merge_warnings(*append_warnings)
        if merged_warn:
            out_resume_ok["warning"] = merged_warn
        return out_resume_ok

    chunks = list(prep.get("chunks") or [])
    if not chunks:
        return {
            "ok": False,
            "video_id": safe_vid,
            "already_exists": False,
            "error": "empty_chunks",
            "message": "未能从宿主机产物生成飞书正文分块",
        }

    total_parts = int(prep.get("total_parts") or len(chunks))
    zh_title = str(prep.get("zh_title") or "")
    first_part = int(chunks[0]["part"])
    mark_res = mark_in_flight_write(
        root,
        safe_vid,
        operation="create",
        part=first_part,
        document_id="",
        doc_url="",
        expected_document_id="",
        expected_doc_url="",
    )
    if not mark_res.get("ok"):
        return mark_res

    create_res = await create_feishu_doc_on_server(
        user_id=user_id,
        title=str(prep.get("title") or zh_title),
        markdown=str(chunks[0]["content"]),
        folder_token=str(prep.get("folder_token") or "") or None,
        filesystem_backend=backend,
    )
    doc_id = str(create_res.get("document_id") or "").strip()
    doc_url = _derive_feishu_doc_url(create_res)
    node_token = str(create_res.get("node_token") or "") or _extract_wiki_node_token(doc_url)
    is_create_img_fail = str(create_res.get("error") or "").strip().lower() == "image_bind_failed"
    create_warning = _merge_warnings(
        create_res.get("warning"),
        _format_image_bind_warning(create_res, part=first_part) if is_create_img_fail else None,
    )

    if (create_res.get("ok") is True or is_create_img_fail) and (doc_id or doc_url):
        saved = save_doc(
            root,
            safe_vid,
            doc_url=doc_url,
            document_id=doc_id,
            zh_title=zh_title,
            total_parts=total_parts,
            completed_parts=[first_part],
            body_complete=(total_parts <= 1),
            expected_document_id="",
            expected_doc_url="",
        )
        if not saved.get("ok"):
            return saved
        doc_id = str(saved.get("document_id") or doc_id).strip()
        doc_url = str(saved.get("doc_url") or doc_url).strip()
        tail_chunks = chunks[1:]
        if not tail_chunks:
            out_single: dict[str, Any] = {
                "ok": True,
                "video_id": safe_vid,
                "already_exists": False,
                "document_id": doc_id,
                "doc_url": doc_url,
                "zh_title": zh_title,
                "total_parts": total_parts,
                "appended_parts": [],
            }
            if create_warning:
                out_single["warning"] = create_warning
            return out_single
        exp_after_save_id = doc_id
        exp_after_save_url = None if doc_id else doc_url
        ok, appended_parts, err_res, append_warnings = await _append_chunks_sequentially(
            root=root,
            video_id=safe_vid,
            doc_id=doc_id,
            node_token=node_token,
            doc_url=doc_url,
            total_parts=total_parts,
            chunks=tail_chunks,
            filesystem_backend=backend,
            expected_doc_id=exp_after_save_id,
            expected_doc_url=exp_after_save_url,
        )
        if not doc_id:
            rec = read_json_dict(doc_record_path(root, safe_vid), required=False)
            doc_id = str(rec.get("document_id") or "").strip()
        combined_warning = _merge_warnings(create_warning, *append_warnings)
        if not ok:
            err_dict = err_res or {}
            if err_dict.get("code") == "doc_identity_mismatch":
                return err_dict
            if _is_uncertain_write_error(err_dict):
                return _abandon_on_uncertainty(
                    root,
                    safe_vid,
                    doc_id=doc_id,
                    doc_url=doc_url,
                    reason=str(
                        err_dict.get("detail")
                        or err_dict.get("message")
                        or err_dict.get("error")
                        or "append_outcome_unknown"
                    ),
                    failed_part=int(err_dict["failed_part"]) if err_dict.get("failed_part") else None,
                    total_parts=total_parts,
                    appended_parts=appended_parts,
                    zh_title=zh_title,
                    share_warning=combined_warning,
                    expected_document_id=exp_after_save_id,
                    expected_doc_url=exp_after_save_url,
                )
            out_tail_err: dict[str, Any] = {
                "ok": False,
                "video_id": safe_vid,
                "already_exists": False,
                "error": str(err_dict.get("error") or "append_failed"),
                "message": str(err_dict.get("message") or "建档后追加后续分块失败"),
                "document_id": doc_id,
                "doc_url": doc_url,
                "zh_title": zh_title,
                "total_parts": total_parts,
                "appended_parts": appended_parts,
            }
            if combined_warning:
                out_tail_err["warning"] = combined_warning
            return out_tail_err
        out_multi: dict[str, Any] = {
            "ok": True,
            "video_id": safe_vid,
            "already_exists": False,
            "document_id": doc_id,
            "doc_url": doc_url,
            "zh_title": zh_title,
            "total_parts": total_parts,
            "appended_parts": appended_parts,
        }
        if combined_warning:
            out_multi["warning"] = combined_warning
        return out_multi

    is_uncertain_create = bool(doc_id or doc_url or _is_uncertain_write_error(create_res))
    if is_uncertain_create:
        return _abandon_on_uncertainty(
            root,
            safe_vid,
            doc_id=doc_id,
            doc_url=doc_url,
            reason=str(
                create_res.get("detail")
                or create_res.get("message")
                or create_res.get("error")
                or "content_insert_failed"
            ),
            failed_part=first_part,
            total_parts=total_parts,
            appended_parts=[],
            zh_title=zh_title,
            share_warning=create_warning,
            expected_document_id="",
            expected_doc_url="",
        )

    clear_res = clear_in_flight_write(
        root,
        safe_vid,
        expected_document_id="",
        expected_doc_url="",
    )
    if not clear_res.get("ok"):
        return clear_res

    err_detail = str(create_res.get("message") or create_res.get("error") or "feishu_create_doc_failed")
    fail_res = record_failure(root, safe_vid, err_detail)
    stopped = bool(fail_res.get("stopped"))
    return {
        "ok": False,
        "video_id": safe_vid,
        "already_exists": False,
        "error": str(fail_res.get("reason") if stopped else (create_res.get("error") or "doc_create_failed")),
        "message": str(create_res.get("message") or err_detail),
        "attempts": fail_res.get("attempts"),
        "stopped": stopped,
        "reason": str(fail_res.get("reason") or ""),
        "document_id": "",
        "doc_url": "",
        "total_parts": total_parts,
        "appended_parts": [],
    }


def create_podcast_weekly_tools(filesystem_backend: Any = None) -> list[BaseTool]:
    """Build `[podcast_weekly_read, podcast_weekly_write]` (`DALTON-HOOK(dalton-tools)`)."""

    @tool("podcast_weekly_read")
    async def podcast_weekly_read(
        action: str,
        video_id: Optional[str] = None,
        artifact: Optional[str] = None,
        date: Optional[str] = None,
        pending_only: bool = False,
        offset: int = 0,
        max_chars: int = 16000,
        *,
        config: RunnableConfig,
    ) -> str:
        """Inspect host `podcast-weekly` publish state or stage episode files into the sandbox; internal and admin only.

        Call `"status"` first to get `next_action` and episode queues; use `"stage"` to copy host files into `.work/podcast-weekly/<video_id>/`. Keep `"read_artifact"` and `"prepare_doc"` for manual troubleshooting only.

        Args:
            action: `"status"` (queues and `next_action`), `"stage"` (copy host `notes`, `zh`, `en`,
                and `merged` to `.work/podcast-weekly/<video_id>/`), or debug-only
                `"read_artifact"` / `"prepare_doc"`.
            video_id: Episode ID (`^[A-Za-z0-9_-]{3,64}$`), required for `"stage"`,
                `"read_artifact"`, and `"prepare_doc"`.
            artifact: For `"read_artifact"` only: `"zh"`, `"en"`, `"notes"`, `"merged"`,
                `"summary"`, or `"doc_state"`.
            date: Optional `YYYY-MM-DD` (HKT) date override for `"status"` or `"prepare_doc"`.
            pending_only: With `"status"`, omit already-reported completed episodes when true.
            offset: Starting character offset for `"read_artifact"` (default 0).
            max_chars: Character limit for `"read_artifact"` or chunk size for `"prepare_doc"`.

        Returns:
            JSON with `ok`, weekly queues and `next_action` (`"status"`), or sandbox file paths, byte sizes, and `sha256` without file text (`"stage"`).

        Stop on `ok=false`; never treat `ledger_missing`, `ledger_unreadable`, or missing files as an empty week.
        """
        denied = await _authorize_internal("podcast_weekly_read", config)
        if denied is not None:
            return denied

        act = str(action or "").strip().lower()
        root = resolve_host_openclaw_data_root()
        backend = _resolve_sandbox_backend(filesystem_backend, config)
        try:
            if act == "status":
                out = get_status(root, date_str=date, pending_only=bool(pending_only))
                return json.dumps(out, ensure_ascii=False)
            if act == "stage":
                out = await _handle_stage(root, backend, video_id)
                return json.dumps(out, ensure_ascii=False)
            if act == "read_artifact":
                if not video_id or not artifact:
                    return json.dumps(
                        {"ok": False, "error": "missing_params", "message": "read_artifact 需要 video_id 与 artifact"},
                        ensure_ascii=False,
                    )
                out = read_artifact(
                    root,
                    video_id,
                    artifact,
                    offset=int(offset or 0),
                    max_chars=int(max_chars or 16000),
                )
                return json.dumps(out, ensure_ascii=False)
            if act == "prepare_doc":
                if not video_id:
                    return json.dumps(
                        {"ok": False, "error": "missing_video_id", "message": "prepare_doc 需要 video_id"},
                        ensure_ascii=False,
                    )
                chunk_limit = 25000 if max_chars == 16000 else max(1000, int(max_chars or 25000))
                out = prepare_doc(root, video_id, max_chars=chunk_limit, date_str=date)
                out.pop("markdown", None)
                return json.dumps(out, ensure_ascii=False)
            return json.dumps(
                {
                    "ok": False,
                    "error": "invalid_action",
                    "message": f"未知 action={action!r}，可选 status / stage / read_artifact / prepare_doc",
                },
                ensure_ascii=False,
            )
        except (
            PodcastStoreSecurityError,
            RecordUnreadableError,
            FileNotFoundError,
            ValueError,
            OSError,
        ) as exc:
            return json.dumps(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
            )

    @tool("podcast_weekly_write")
    async def podcast_weekly_write(
        action: str,
        video_id: Optional[str] = None,
        artifact: Optional[str] = None,
        artifacts: Optional[list[str]] = None,
        content: Optional[str] = None,
        doc_url: Optional[str] = None,
        document_id: Optional[str] = None,
        zh_title: Optional[str] = None,
        part: Optional[int] = None,
        total_parts: Optional[int] = None,
        body_complete: Optional[bool] = None,
        error: Optional[str] = None,
        report_run_id: Optional[str] = None,
        text: Optional[str] = None,
        success_video_ids: Optional[list[str]] = None,
        failed_video_ids: Optional[list[str]] = None,
        empty_date: Optional[str] = None,
        date: Optional[str] = None,
        *,
        config: RunnableConfig,
    ) -> str:
        """Save proofread sandbox files to host storage, publish Feishu docs server-side, or record weekly report delivery; internal and admin only.

        Use `"collect"` after subagent proofreading and `"publish_doc"` to create or resume an episode's Feishu doc on the server. Keep `"write_artifact"`, `"save_doc"`, `"record_chunk"`, and `"record_failure"` for manual recovery only.

        Args:
            action: `"collect"` (validate and save `.work/podcast-weekly/<video_id>/` files to host),
                `"publish_doc"` (create or resume Feishu doc and record state on server),
                `"save_pending_report"` (save summary text before sending),
                `"abandon_pending_report"` (discard expired prior-day pending report),
                `"mark_reported"` (record confirmed delivery), or legacy `"write_artifact"` /
                `"save_doc"` / `"record_chunk"` / `"record_failure"`.
            video_id: Episode ID (`^[A-Za-z0-9_-]{3,64}$`), required for `"collect"`,
                `"publish_doc"`, and legacy episode actions.
            artifacts: Artifact keys for `"collect"`; defaults to `["zh", "en", "notes", "summary"]`.
            artifact: For `"write_artifact"`: `"zh"`, `"en"`, `"notes"`, or `"summary"`.
            content: Full UTF-8 text for `"write_artifact"`.
            doc_url: Feishu doc URL for `"save_doc"` or `"record_chunk"`.
            document_id: Feishu `document_id` for `"save_doc"` or `"record_chunk"`.
            zh_title: Chinese episode title for `"save_doc"`.
            part: Completed chunk number for `"save_doc"` or `"record_chunk"`.
            total_parts: Total chunk count for `"save_doc"` or `"record_chunk"`.
            body_complete: Whether all chunks are written, for `"save_doc"`.
            error: Failure message for `"record_failure"`.
            report_run_id: Report run ID for `"save_pending_report"`, `"abandon_pending_report"`,
                or `"mark_reported"`.
            text: Exact summary text saved by `"save_pending_report"` for deterministic resubmit.
            success_video_ids: Published `video_id` list for `"save_pending_report"` or
                `"mark_reported"`.
            failed_video_ids: Failed `video_id` list for `"save_pending_report"` or
                `"mark_reported"`.
            empty_date: `YYYY-MM-DD` (HKT) date recorded in `empty_weeks` for empty-week
                `"save_pending_report"` or `"mark_reported"`.
            date: Optional `YYYY-MM-DD` (HKT) date override for `"publish_doc"`,
                `"save_pending_report"`, or `"abandon_pending_report"`.

        Returns:
            JSON with `ok`, per-file `bytes`/`sha256` without file text (`"collect"`), Feishu `doc_url`/`document_id`/`warning` (`"publish_doc"`), or report state.

        For `"collect"`, invalid or missing files remain unwritten in `failed` while valid siblings save. For `"publish_doc"`, skip the episode in this run on `code="publish_in_progress"` or `code="doc_abandoned_uncertain"` (rebuilds next run), stop and list `abandoned_documents` links for manual check on `code="doc_uncertain_twice"`, stop the episode on `stopped=true`, never resend body chunks when `warning` reports image-bind or share failure, and pass `warning` to the user for manual fix. Call `"save_pending_report"` before `automation_report`, and `"mark_reported"` only after `delivery_confirmed=true`.
        """
        denied = await _authorize_internal("podcast_weekly_write", config)
        if denied is not None:
            return denied

        act = str(action or "").strip().lower()
        root = resolve_host_openclaw_data_root()
        backend = _resolve_sandbox_backend(filesystem_backend, config)
        user_id, _ws_id, _th_id, _run_id = _extract_caller_identity(config)
        try:
            if act == "collect":
                out = await _handle_collect(root, backend, video_id, artifacts)
                return json.dumps(out, ensure_ascii=False)
            if act == "publish_doc":
                out = await _handle_publish_doc(root, backend, user_id, video_id, date)
                return json.dumps(out, ensure_ascii=False)
            if act == "write_artifact":
                if not video_id or not artifact or content is None:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "missing_params",
                            "message": "write_artifact 需要 video_id, artifact, content",
                        },
                        ensure_ascii=False,
                    )
                out = write_artifact(root, video_id, artifact, content)
                return json.dumps(out, ensure_ascii=False)
            if act == "save_doc":
                if not video_id:
                    return json.dumps(
                        {"ok": False, "error": "missing_video_id", "message": "save_doc 需要 video_id"},
                        ensure_ascii=False,
                    )
                parts_list = [int(part)] if part is not None and int(part) > 0 else ([] if body_complete is False else None)
                out = save_doc(
                    root,
                    video_id,
                    doc_url=doc_url or "",
                    document_id=document_id or "",
                    zh_title=zh_title or "",
                    total_parts=int(total_parts or 1),
                    completed_parts=parts_list,
                    body_complete=body_complete,
                )
                return json.dumps(out, ensure_ascii=False)
            if act == "record_chunk":
                if not video_id or part is None:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "missing_params",
                            "message": "record_chunk 需要 video_id 与 part",
                        },
                        ensure_ascii=False,
                    )
                out = record_chunk(
                    root,
                    video_id,
                    part=int(part),
                    total_parts=int(total_parts or 1),
                    document_id=document_id or "",
                    doc_url=doc_url or "",
                )
                return json.dumps(out, ensure_ascii=False)
            if act == "record_failure":
                if not video_id:
                    return json.dumps(
                        {"ok": False, "error": "missing_video_id", "message": "record_failure 需要 video_id"},
                        ensure_ascii=False,
                    )
                out = record_failure(root, video_id, error or "")
                return json.dumps(out, ensure_ascii=False)
            if act == "save_pending_report":
                if not report_run_id:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "missing_report_run_id",
                            "message": "save_pending_report 需要 report_run_id",
                        },
                        ensure_ascii=False,
                    )
                report_text = text if text is not None else content
                if not isinstance(report_text, str) or not report_text.strip():
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "missing_text",
                            "message": "save_pending_report 需要 text",
                        },
                        ensure_ascii=False,
                    )
                out = save_pending_report(
                    root,
                    report_run_id,
                    report_text,
                    success_ids=success_video_ids,
                    failed_ids=failed_video_ids,
                    empty_date=empty_date,
                    date_str=date,
                )
                return json.dumps(out, ensure_ascii=False)
            if act == "abandon_pending_report":
                if not report_run_id:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "missing_report_run_id",
                            "message": "abandon_pending_report 需要 report_run_id",
                        },
                        ensure_ascii=False,
                    )
                out = abandon_pending_report(root, report_run_id, date_str=date)
                return json.dumps(out, ensure_ascii=False)
            if act == "mark_reported":
                if not report_run_id:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "missing_report_run_id",
                            "message": "mark_reported 需要 report_run_id",
                        },
                        ensure_ascii=False,
                    )
                out = mark_reported(
                    root,
                    report_run_id,
                    success_ids=success_video_ids,
                    failed_ids=failed_video_ids,
                    empty_date=empty_date,
                )
                return json.dumps(out, ensure_ascii=False)
            return json.dumps(
                {
                    "ok": False,
                    "error": "invalid_action",
                    "message": (
                        f"未知 action={action!r}，可选 collect / publish_doc / write_artifact / "
                        "save_doc / record_chunk / record_failure / save_pending_report / "
                        "abandon_pending_report / mark_reported"
                    ),
                },
                ensure_ascii=False,
            )
        except (
            PodcastStoreSecurityError,
            RecordUnreadableError,
            FileNotFoundError,
            ValueError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            return json.dumps(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
            )

    return [podcast_weekly_read, podcast_weekly_write]
