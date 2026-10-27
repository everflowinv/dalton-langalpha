"""Direct LangChain tool `ingest_company_wiki` for two-step company-wiki ingestion via the host bridge."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import posixpath
import re
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid5

import httpx
from fastapi import HTTPException
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from ptc_agent.core.sandbox.jobs import Failed, Running
from src.dalton.access_tiers import get_user_tier
from src.dalton.auth import (
    resolve_caller_home_workspace_id,
    verify_workspace_access,
)
from src.dalton.database.wiki_ingests import (
    create_pending_wiki_ingest,
    record_wiki_ingest,
    resolve_user_email,
    update_wiki_ingest_status,
)
from src.dalton.settings import (
    get_dalton_bridge_admin_token,
    get_dalton_bridge_base_url,
)
from src.dalton.tools.knowledge_tools import _extract_run_identity
from src.utils.cache import get_cache_client

logger = logging.getLogger(__name__)

WIKI_INGEST_RATE_LIMIT_PER_HOUR: int = 30
WIKI_INGEST_RATE_WINDOW_S: int = 3600
WIKI_INGEST_HTTP_TIMEOUT_S: float = 190.0
WIKI_INGEST_MAX_FILE_BYTES: int = 20 * 1024 * 1024
WIKI_INGEST_ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {".md", ".txt", ".pdf", ".docx", ".png", ".jpg", ".webp"}
)
_WIKI_REQUEST_ID_NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
_FORBIDDEN_PATH_CHARS_RE = re.compile(r"""[;$()`|&<>'"!*?\[\]{}~#\x00-\x1f\x7f\\]""")
_ALLOWED_ARGV_BINARIES: frozenset[str] = frozenset({"stat", "realpath"})

EXTERNAL_WIKI_INGEST_FORBIDDEN_MESSAGE = (
    "外部成员不能写入公司 wiki（ingest_company_wiki 仅管理员与内部成员可用）；"
    "请如实告知用户这一身份限制，不要尝试绕过。"
)

TargetTypeLiteral = Literal["company", "sector"]


def _extract_user_email_from_config(config: RunnableConfig | None) -> str:
    if not isinstance(config, dict):
        return ""
    configurable = config.get("configurable")
    if isinstance(configurable, dict):
        for key in ("user_email", "email"):
            val = configurable.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def derive_wiki_ingest_request_id(
    *,
    user_id: str,
    workspace_id: str,
    thread_id: str | None,
    target_type: str,
    target: str,
    content_type: str,
    date: str,
    payload_sha256: str,
) -> str:
    """Compute a deterministic UUID5 `request_id` so retries of the same ingest are idempotent."""
    norm_target = (
        target.strip().upper()
        if target_type.strip().lower() == "company"
        else target.strip().lower()
    )
    seed = (
        f"{user_id.strip()}:{workspace_id.strip()}:{(thread_id or '').strip()}:"
        f"{target_type.strip().lower()}:{norm_target}:{content_type.strip()}:"
        f"{date.strip()}:{payload_sha256.strip()}"
    )
    return str(uuid5(_WIKI_REQUEST_ID_NAMESPACE, seed))


_WIKI_RATE_LIMIT_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 or redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
return current
"""


async def check_and_increment_wiki_ingest_rate_limit(
    user_id: str,
    *,
    limit: int = WIKI_INGEST_RATE_LIMIT_PER_HOUR,
    window_s: int = WIKI_INGEST_RATE_WINDOW_S,
) -> tuple[bool, int]:
    """Atomically increment per-user hourly write counter in Redis via a single Lua `EVAL`.

    Fails closed (`(False, -1)`) when Redis is unavailable, disabled, or errors out.
    """
    key = f"dalton:wiki_ingest:rate:{user_id}"
    try:
        cache = get_cache_client()
    except Exception as exc:
        logger.warning(
            "Redis cache client lookup failed for wiki rate limit (failing closed): %s",
            type(exc).__name__,
        )
        return False, -1

    if cache is None or not getattr(cache, "enabled", False):
        logger.warning("Redis cache is disabled/unavailable for wiki rate limit (failing closed)")
        return False, -1

    raw_redis = getattr(cache, "client", None)
    if raw_redis is None or not hasattr(raw_redis, "eval"):
        logger.warning("Redis client missing atomic eval for wiki rate limit (failing closed)")
        return False, -1

    try:
        count = await raw_redis.eval(_WIKI_RATE_LIMIT_LUA, 1, key, int(window_s))
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            logger.warning("Redis EVAL returned invalid count for wiki rate limit (failing closed)")
            return False, -1
        return count <= limit, count
    except Exception as exc:
        logger.warning("Redis atomic rate-limit check failed (failing closed): %s", type(exc).__name__)
        return False, -1


def _validate_and_normalize_workspace_file_path(
    file_path: str,
    *,
    workspace_dir: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Validate `file_path` and `workspace_dir` against shell metacharacters and traversal.

    Returns `(ws_root, workspace_rel_path, error_message)`.
    """
    if not isinstance(file_path, str) or not file_path.strip():
        return None, None, "file_path must be a non-empty string."
    cleaned = file_path.strip()
    if _FORBIDDEN_PATH_CHARS_RE.search(cleaned):
        return None, None, "file_path contains forbidden characters."

    ws_raw = str(workspace_dir or "").strip().rstrip("/")
    if not ws_raw or not ws_raw.startswith("/") or _FORBIDDEN_PATH_CHARS_RE.search(ws_raw):
        return None, None, "Workspace root directory is invalid or not configured."
    ws_root = posixpath.normpath(ws_raw)
    if not ws_root.startswith("/") or ws_root == "/":
        return None, None, "Workspace root directory is invalid."

    raw_parts = [p for p in cleaned.split("/") if p and p != "."]
    if ".." in raw_parts:
        return None, None, "Path traversal ('..') is not allowed in file_path."

    if cleaned.startswith("/"):
        norm_abs = posixpath.normpath(cleaned)
        if not norm_abs.startswith(ws_root + "/"):
            return None, None, "file_path escapes caller workspace directory."
        rel_path = norm_abs[len(ws_root) + 1 :]
    else:
        rel_path = posixpath.normpath(cleaned)
        while rel_path.startswith("./"):
            rel_path = rel_path[2:]

    if (
        not rel_path
        or rel_path == "."
        or rel_path.startswith("/")
        or rel_path.startswith("..")
        or "/../" in f"/{rel_path}/"
    ):
        return None, None, "file_path escapes caller workspace directory."

    rel_parts = [p for p in rel_path.split("/") if p]
    if not rel_parts or any(p == ".agents" or p.startswith(".") for p in rel_parts):
        return None, None, "file_path cannot reference hidden or mounted system paths (.agents)."

    ext = Path(rel_parts[-1]).suffix.lower()
    if ext not in WIKI_INGEST_ALLOWED_EXTENSIONS:
        return (
            None,
            None,
            f"Unsupported file extension '{ext}'. Allowed: {', '.join(sorted(WIKI_INGEST_ALLOWED_EXTENSIONS))}",
        )

    return ws_root, rel_path, None


#: How long a metadata-inspection code job may run before the gate fails
#: closed. The check itself (`stat`/`realpath`) is subsecond; the budget only
#: has to cover the sandbox python's startup and imports, so a cold container
#: gets seconds, not minutes.
_METADATA_JOB_WAIT_S: float = 20.0


async def _exec_sandbox_argv(
    backend: Any,
    argv: list[str],
    *,
    timeout: int = 10,
) -> tuple[int, str]:
    """Execute `argv` (`stat` or `realpath`) in the sandbox as an argument list without a shell.

    Never interpolates any argument into a shell command string: the argv rides
    base64-encoded into a Python one-shot the sandbox runs as a code job.
    Raises `RuntimeError` if execution fails, is unsupported, or outlives its
    wait — this is a synchronous fail-closed gate, so a check that cannot
    complete must never read as passed, and a job that outlives the wait is
    stopped rather than left dangling.
    """
    if not isinstance(argv, list) or not argv:
        raise RuntimeError("argv must be a non-empty list")
    if argv[0] not in _ALLOWED_ARGV_BINARIES:
        raise RuntimeError(f"Unsupported binary for metadata inspection: {argv[0]!r}")
    for arg in argv:
        if not isinstance(arg, str) or "\x00" in arg or _FORBIDDEN_PATH_CHARS_RE.search(arg):
            raise RuntimeError("Invalid argument in argv list")

    start_code_job_fn = getattr(backend, "start_code_job", None)
    if not callable(start_code_job_fn):
        raise RuntimeError("Filesystem backend does not support code jobs")

    argv_b64 = base64.b64encode(json.dumps(list(argv)).encode("utf-8")).decode("ascii")
    code = (
        "import base64, json, subprocess, sys\n"
        f"argv = json.loads(base64.b64decode('{argv_b64}').decode('utf-8'))\n"
        "proc = subprocess.run(argv, shell=False, check=False, capture_output=True, text=True)\n"
        "print(json.dumps({'exit_code': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr}))\n"
    )
    outcome = await start_code_job_fn(code, wait_s=_METADATA_JOB_WAIT_S)
    if isinstance(outcome, Running):
        try:
            await backend.stop_job(outcome.handle.ref, discard=True)
        except Exception:
            logger.warning(
                "Could not stop metadata inspection job %s",
                outcome.handle.ref.command_id,
                exc_info=True,
            )
        raise RuntimeError("Sandbox metadata inspection outlived its wait")
    if isinstance(outcome, Failed):
        raise RuntimeError(f"Sandbox metadata inspection failed: {outcome.message}")
    if outcome.exit_code != 0:
        # The one-shot python itself failed (startup, import): its own exit
        # code, never the payload's, since none was printed.
        raise RuntimeError(
            f"Sandbox metadata inspection job failed (exit={outcome.exit_code})"
        )

    stdout_raw = outcome.output
    if not isinstance(stdout_raw, str) or not stdout_raw.strip():
        raise RuntimeError("Sandbox code job returned empty stdout")
    last_line = stdout_raw.strip().splitlines()[-1]
    parsed = json.loads(last_line)
    if not isinstance(parsed, dict):
        raise RuntimeError("Sandbox code job returned non-dict payload")
    exit_code = parsed.get("exit_code")
    stdout_str = parsed.get("stdout")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) or not isinstance(stdout_str, str):
        raise RuntimeError("Sandbox code job returned malformed result")
    return exit_code, stdout_str


async def _read_workspace_file_safely(
    backend: Any,
    file_path: str,
) -> tuple[str | None, bytes | None, str | None]:
    """Validate `file_path` in the sandbox without a shell, reject symlinks at every level,
    confirm `realpath` stays inside the caller's workspace root, and read from the resolved realpath.

    Any check failure fails closed and rejects reading.
    Returns `(filename, file_bytes, error_message)`.
    """
    if backend is None:
        return None, None, "Filesystem backend is unavailable."

    ws_dir_attr = getattr(backend, "workspace_dir", None)
    if not isinstance(ws_dir_attr, str) or not ws_dir_attr.strip():
        root_dir_attr = getattr(backend, "root_dir", None)
        ws_dir = root_dir_attr.strip() if isinstance(root_dir_attr, str) else None
    else:
        ws_dir = ws_dir_attr.strip()

    ws_root, rel_path, path_err = _validate_and_normalize_workspace_file_path(
        file_path, workspace_dir=ws_dir
    )
    if path_err is not None or ws_root is None or rel_path is None:
        return None, None, path_err or "Invalid file_path."

    # Build every path level from workspace root down to target file
    chain_paths: list[str] = [ws_root]
    cur = ws_root
    for part in rel_path.split("/"):
        cur = f"{cur}/{part}"
        chain_paths.append(cur)
    target_abs = chain_paths[-1]

    # 1. Inspect lstat (`stat -c %F -- ...`, no `-L`) on every level via argv list (no shell)
    try:
        stat_code, stat_out = await _exec_sandbox_argv(
            backend,
            ["stat", "-c", "%F", "--", *chain_paths],
            timeout=10,
        )
    except Exception as exc:
        return None, None, f"Metadata stat check failed: {type(exc).__name__}: {exc}"

    if stat_code != 0 or not stat_out.strip():
        return None, None, f"Metadata stat check failed for '{rel_path}' (exit={stat_code})."

    stat_lines = [line.strip().lower() for line in stat_out.strip().splitlines() if line.strip()]
    if len(stat_lines) != len(chain_paths):
        return None, None, "Metadata stat check returned incomplete path component types."

    for ftype in stat_lines:
        if "symbolic link" in ftype or "symlink" in ftype:
            return None, None, "Symlinks are not allowed in file_path."

    for parent_ftype in stat_lines[:-1]:
        if parent_ftype != "directory":
            return None, None, "Intermediate path component is not a directory."

    leaf_ftype = stat_lines[-1]
    if leaf_ftype == "regular empty file":
        return None, None, f"File '{rel_path}' is empty."
    if leaf_ftype != "regular file":
        return None, None, f"Target '{rel_path}' is not a regular file ({leaf_ftype})."

    # 2. Resolve canonical realpath (`realpath -e -- ws_root target_abs`) via argv list (no shell)
    try:
        rp_code, rp_out = await _exec_sandbox_argv(
            backend,
            ["realpath", "-e", "--", ws_root, target_abs],
            timeout=10,
        )
    except Exception as exc:
        return None, None, f"Metadata realpath check failed: {type(exc).__name__}: {exc}"

    if rp_code != 0 or not rp_out.strip():
        return None, None, f"Metadata realpath check failed for '{rel_path}' (exit={rp_code})."

    rp_lines = [line.strip() for line in rp_out.strip().splitlines() if line.strip()]
    if len(rp_lines) != 2:
        return None, None, "Metadata realpath check returned unexpected output."

    resolved_root = posixpath.normpath(rp_lines[0]).rstrip("/")
    resolved_target = posixpath.normpath(rp_lines[1])
    if (
        not resolved_root.startswith("/")
        or resolved_root == ""
        or not resolved_target.startswith("/")
        or _FORBIDDEN_PATH_CHARS_RE.search(resolved_root)
        or _FORBIDDEN_PATH_CHARS_RE.search(resolved_target)
    ):
        return None, None, "Resolved realpath is invalid."

    try:
        common = posixpath.commonpath([resolved_root, resolved_target])
    except ValueError:
        return None, None, "file_path escapes caller workspace directory."

    expected_resolved_target = f"{resolved_root}/{rel_path}"
    if (
        common != resolved_root
        or not resolved_target.startswith(resolved_root + "/")
        or resolved_target != expected_resolved_target
    ):
        return None, None, "file_path escapes caller workspace directory."

    # 3. Read file bytes using the exact same resolved realpath (`resolved_target`)
    file_bytes: bytes | None = None
    if hasattr(backend, "adownload_files"):
        try:
            dl_list = await backend.adownload_files([resolved_target])
            if isinstance(dl_list, list) and dl_list:
                first = dl_list[0]
                err = getattr(first, "error", None) or (
                    first.get("error") if isinstance(first, dict) else None
                )
                if err:
                    if "symlink" in str(err).lower():
                        return None, None, "Symlinks are not allowed in file_path."
                    return None, None, f"Failed reading '{resolved_target}': {err}"
                content_val = getattr(first, "content", None)
                if content_val is None and isinstance(first, dict):
                    content_val = first.get("content")
                if isinstance(content_val, (bytes, bytearray)):
                    file_bytes = bytes(content_val)
                elif isinstance(content_val, str):
                    file_bytes = content_val.encode("utf-8")
        except Exception as exc:
            return None, None, f"Failed reading '{resolved_target}': {type(exc).__name__}"

    if file_bytes is None and hasattr(backend, "adownload_file_bytes"):
        try:
            raw = await backend.adownload_file_bytes(resolved_target)
            if isinstance(raw, (bytes, bytearray)):
                file_bytes = bytes(raw)
        except Exception as exc:
            return None, None, f"Failed reading '{resolved_target}': {type(exc).__name__}"

    if file_bytes is None:
        return None, None, f"File not found or unreadable: '{resolved_target}'."
    if len(file_bytes) == 0:
        return None, None, f"File '{rel_path}' is empty."
    if len(file_bytes) > WIKI_INGEST_MAX_FILE_BYTES:
        return None, None, f"File '{rel_path}' exceeds 20 MB limit ({len(file_bytes)} bytes)."

    return Path(resolved_target).name, file_bytes, None


async def call_bridge_wiki_ingest(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST `payload` to `{bridge_url}/wiki/ingest` authenticated with `DALTON_BRIDGE_ADMIN_TOKEN`."""
    bridge_url = get_dalton_bridge_base_url().rstrip("/")
    admin_token = get_dalton_bridge_admin_token()
    if not admin_token:
        return 500, {
            "ok": False,
            "status": "config_error",
            "error": "DALTON_BRIDGE_ADMIN_TOKEN is not configured on backend.",
        }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {admin_token}",
        "X-Dalton-Bridge-Token": admin_token,
    }
    async with httpx.AsyncClient(
        timeout=WIKI_INGEST_HTTP_TIMEOUT_S,
        trust_env=False,
    ) as client:
        resp = await client.post(
            f"{bridge_url}/wiki/ingest",
            json=payload,
            headers=headers,
        )
        try:
            data = resp.json()
            if not isinstance(data, dict):
                data = {"ok": False, "status": "invalid_response", "error": str(data)}
        except Exception:
            data = {
                "ok": False,
                "status": "http_error",
                "error": f"Bridge returned HTTP {resp.status_code}",
            }
        return resp.status_code, data


def build_ingest_company_wiki_tool(
    filesystem_backend: Any = None,
    *,
    allow_home_workspace: bool = False,
) -> BaseTool:
    """Build `ingest_company_wiki` bound to `filesystem_backend` for Analyst or Chief of Staff."""

    @tool("ingest_company_wiki")
    async def _ingest_company_wiki(
        target_type: TargetTypeLiteral,
        target: str,
        content_type: str,
        date: str,
        source: str | None = None,
        source_url: str | None = None,
        content: str | None = None,
        file_path: str | None = None,
        confirm: bool = False,
        *,
        config: RunnableConfig,
    ) -> str:
        """将公司或行业一手研究资料（会议纪要、专家访谈、买方交流、研究笔记等）入库到基金共享 company-wiki；卖方研报与 sales color 严禁入库（请走 sales notes）。

        仅管理员与内部成员可入库：外部成员（按名单固定）调用会被直接拒绝（confirm=False 预览同样拒绝），此时如实告知用户，不要尝试绕过。
        必须分两步调用：先传 confirm=False 获取预览（解析后的 slug、类型、文件名、大小、是否疑似卖方或重复）并向用户展示确认；用户明确同意后再传 confirm=True 正式写入。
        Args:
            target_type: 归属维度，'company' 或 'sector'。
            target: 公司代码/名称（如 'NVDA'）或行业标识（如 'us-semiconductor'）。
            content_type: 允许的类型（管理层会议纪要、专家访谈、季度研究笔记、Earnings Update、NDR纪要、Fireside Chat、买方交流、研究笔记、新闻更新、News Update、行业趋势、Industry Trends、Flomo笔记、微信文章、IPO Prospectus、投资备忘、Investment Memo）。
            date: 文档日期 YYYY-MM-DD。
            source: 资料来源名称（可选，如 'Expert Network'、'IR 1x1'；不得为券商/卖方渠道）。
            source_url: 原始链接 URL（可选，须为 http/https）。
            content: 直接入库的文本正文（与 file_path 二选一，<=200k 字符）。
            file_path: 当前工作区（或 Home）内的文件路径（与 content 二选一，支持 .md/.txt/.pdf/.docx/.png/.jpg/.webp，<=20MB）。
            confirm: False 仅校验并返回预览；True 执行正式写入并记录审计。
        Returns: JSON 字符串，含预览信息（confirm=False）或正式入库结果与 wiki:<doc_id>（confirm=True）。
        """
        user_id, workspace_id, thread_id, _ = _extract_run_identity(config)
        if not user_id:
            return "ERROR: user_id and workspace_id are required in RunnableConfig."

        # Company wiki is team-shared storage: refuse external members before
        # any bridge call (preview included), regardless of confirm.
        if await get_user_tier(user_id) == "external":
            return json.dumps(
                {
                    "ok": False,
                    "status": "forbidden",
                    "error_code": "external_member_forbidden",
                    "error": EXTERNAL_WIKI_INGEST_FORBIDDEN_MESSAGE,
                },
                ensure_ascii=False,
            )

        if allow_home_workspace and (
            not workspace_id or workspace_id.strip().lower() == "home"
        ):
            try:
                workspace_id = await resolve_caller_home_workspace_id(user_id)
            except Exception as exc:
                detail = getattr(exc, "detail", None) or str(exc)
                return f"ERROR: {detail}"

        if not workspace_id or workspace_id.strip().lower() == "home":
            return "ERROR: user_id and workspace_id are required in RunnableConfig."

        try:
            await verify_workspace_access(workspace_id, user_id)
        except HTTPException as exc:
            return f"ERROR: {exc.detail}"
        except Exception as exc:
            return f"ERROR: {exc}"

        has_content = content is not None and str(content).strip() != ""
        has_file = file_path is not None and str(file_path).strip() != ""
        if has_content == has_file:
            return "ERROR: Provide exactly one of content or file_path (not both, not neither)."

        file_payload: dict[str, Any] | None = None
        payload_sha256: str = ""
        if has_file and file_path is not None:
            fname, fbytes, ferr = await _read_workspace_file_safely(
                filesystem_backend, file_path
            )
            if ferr is not None or fname is None or fbytes is None:
                return f"ERROR: {ferr or 'Failed reading file_path.'}"
            payload_sha256 = hashlib.sha256(fbytes).hexdigest()
            file_payload = {
                "filename": fname,
                "content_b64": base64.b64encode(fbytes).decode("ascii"),
            }
        else:
            norm_body = re.sub(r"\s+", " ", str(content or "").replace("\r\n", "\n").replace("\r", "\n").strip())
            payload_sha256 = hashlib.sha256(norm_body.encode("utf-8")).hexdigest()

        if confirm:
            allowed, current_count = await check_and_increment_wiki_ingest_rate_limit(
                user_id
            )
            if not allowed:
                if current_count < 0:
                    return json.dumps(
                        {
                            "ok": False,
                            "status": "rate_limit_unavailable",
                            "error_code": "rate_limit_unavailable",
                            "error": "限流服务（Redis）不可用，出于安全策略拒绝正式写入（预览仍可使用）。",
                        },
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {
                        "ok": False,
                        "status": "rate_limited",
                        "error_code": "rate_limited",
                        "limit_per_hour": WIKI_INGEST_RATE_LIMIT_PER_HOUR,
                        "current_count": current_count,
                        "error": (
                            f"入库频率超限：每用户每小时最多写入 {WIKI_INGEST_RATE_LIMIT_PER_HOUR} 次 "
                            f"（当前第 {current_count} 次），请稍后再试。"
                        ),
                    },
                    ensure_ascii=False,
                )

        user_email = _extract_user_email_from_config(config)
        if not user_email:
            user_email = await resolve_user_email(user_id)

        request_id: str | None = None
        if confirm:
            request_id = derive_wiki_ingest_request_id(
                user_id=user_id,
                workspace_id=workspace_id,
                thread_id=thread_id,
                target_type=target_type,
                target=target,
                content_type=content_type,
                date=date,
                payload_sha256=payload_sha256,
            )
            await create_pending_wiki_ingest(
                request_id=request_id,
                user_id=user_id,
                workspace_id=workspace_id,
                thread_id=thread_id,
                target_type=target_type,
                target=target,
                content_type=content_type,
                doc_date=date,
                sha256=payload_sha256,
            )

        bridge_req: dict[str, Any] = {
            "target_type": target_type,
            "target": target,
            "content_type": content_type,
            "date": date,
            "source": source,
            "source_url": source_url,
            "content": content if file_payload is None else None,
            "file": file_payload,
            "ingested_by": {"user_id": user_id, "email": user_email},
            "ingested_via": {
                "workspace_id": workspace_id,
                "thread_id": thread_id or "",
            },
            "dry_run": not bool(confirm),
        }
        if request_id is not None:
            bridge_req["request_id"] = request_id

        try:
            _http_code, bridge_resp = await call_bridge_wiki_ingest(bridge_req)
        except Exception as exc:
            logger.warning("Bridge wiki ingest call failed: %s", type(exc).__name__)
            if confirm and request_id is not None:
                try:
                    await update_wiki_ingest_status(request_id, status="failed")
                except Exception:
                    pass
            return json.dumps(
                {
                    "ok": False,
                    "status": "bridge_unreachable",
                    "request_id": request_id,
                    "error": f"Failed calling researchdata bridge /wiki/ingest: {type(exc).__name__}",
                },
                ensure_ascii=False,
            )

        if not confirm:
            return json.dumps(bridge_resp, ensure_ascii=False)

        if bridge_resp.get("ok") is True and bridge_resp.get("status") in ("ingested", "done"):
            assert request_id is not None
            row = await update_wiki_ingest_status(
                request_id,
                status="done",
                doc_id=bridge_resp.get("doc_id"),
                filepath=bridge_resp.get("filepath"),
                sha256=str(bridge_resp.get("sha256") or payload_sha256),
                target_type=str(bridge_resp.get("target_type") or target_type),
                target=str(bridge_resp.get("target") or target),
                content_type=str(bridge_resp.get("content_type") or content_type),
                doc_date=str(bridge_resp.get("date") or date),
            )
            out = {
                **bridge_resp,
                "ingest_id": row.get("ingest_id") or request_id,
                "request_id": request_id,
                "db_status": row.get("status") or "done",
            }
            return json.dumps(out, ensure_ascii=False)

        is_in_progress = (
            bridge_resp.get("status") in ("in_progress", "pending", "processing")
            or bridge_resp.get("error_code") in ("request_in_progress", "in_progress")
            or bridge_resp.get("duplicate_reason") == "same_request_inflight"
        )
        if is_in_progress:
            return json.dumps(
                {
                    **bridge_resp,
                    "ok": False,
                    "status": "in_progress",
                    "db_status": "pending",
                    "request_id": request_id,
                    "error": str(
                        bridge_resp.get("error") or "同一请求正在处理，稍后重试"
                    ),
                },
                ensure_ascii=False,
            )

        if request_id is not None:
            try:
                await update_wiki_ingest_status(request_id, status="failed")
            except Exception:
                pass
        return json.dumps(bridge_resp, ensure_ascii=False)

    return _ingest_company_wiki


__all__ = [
    "WIKI_INGEST_ALLOWED_EXTENSIONS",
    "WIKI_INGEST_HTTP_TIMEOUT_S",
    "WIKI_INGEST_MAX_FILE_BYTES",
    "WIKI_INGEST_RATE_LIMIT_PER_HOUR",
    "WIKI_INGEST_RATE_WINDOW_S",
    "build_ingest_company_wiki_tool",
    "call_bridge_wiki_ingest",
    "check_and_increment_wiki_ingest_rate_limit",
    "derive_wiki_ingest_request_id",
    "record_wiki_ingest",
]
