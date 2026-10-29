"""Direct LangChain tool `prepare_transcription` for audio transcription job preparation (Backlog A1b).

Reads the caller identity from trusted `RunnableConfig`, refuses external members,
verifies workspace read access and audio file integrity in the sandbox without
loading the audio into backend memory, creates the transcription job row in
Postgres, and writes a time-limited, job-bound ticket to a `0600` (owner-only)
file inside the sandbox. The tool output returns only `job_id` and `ticket_file`
— the ticket secret never appears in tool output or command-line arguments.
"""

from __future__ import annotations

import base64
import json
import logging
import posixpath
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from ptc_agent.core.sandbox.jobs import Failed, Running
from src.dalton.access_tiers import get_user_tier
from src.dalton.auth import resolve_caller_home_workspace_id
from src.dalton.services.transcriptions import (
    ALLOWED_TICKET_OPERATIONS,
    DEFAULT_TICKET_TTL_S,
    EXTERNAL_TRANSCRIPTION_FORBIDDEN_MESSAGE,
    MAX_TRANSCRIPTION_AUDIO_BYTES,
    prepare_transcription_job,
    verify_creator_workspace_access,
)
from src.dalton.tools.knowledge_tools import _extract_run_identity
from src.dalton.tools.wiki_tools import (
    _FORBIDDEN_PATH_CHARS_RE,
    _METADATA_JOB_WAIT_S,
    _validate_and_normalize_workspace_file_path,
)

logger = logging.getLogger(__name__)

TRANSCRIPTION_ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp3",
        ".wav",
        ".m4a",
        ".ogg",
        ".flac",
        ".aac",
        ".opus",
        ".wma",
        ".amr",
        ".mp4",
        ".webm",
        ".mpeg",
        ".mpga",
    }
)

_SAFE_THREAD_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_-]+")
_SAFE_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


async def _run_sandbox_json_job(
    backend: Any,
    code: str,
    *,
    wait_s: float = _METADATA_JOB_WAIT_S,
) -> dict[str, Any]:
    """Run a one-shot Python job in `backend` and parse its final JSON stdout line."""
    start_code_job_fn = getattr(backend, "start_code_job", None)
    if not callable(start_code_job_fn):
        raise RuntimeError("Filesystem backend does not support code jobs")

    outcome = await start_code_job_fn(code, wait_s=wait_s)
    if isinstance(outcome, Running):
        try:
            await backend.stop_job(outcome.handle.ref, discard=True)
        except Exception:
            logger.warning(
                "Could not stop transcription sandbox job %s",
                outcome.handle.ref.command_id,
                exc_info=True,
            )
        raise RuntimeError("Sandbox job outlived its wait")
    if isinstance(outcome, Failed):
        raise RuntimeError(f"Sandbox job failed: {outcome.message}")
    if outcome.exit_code != 0:
        raise RuntimeError(f"Sandbox job failed (exit={outcome.exit_code})")

    stdout_raw = outcome.output
    if not isinstance(stdout_raw, str) or not stdout_raw.strip():
        raise RuntimeError("Sandbox code job returned empty stdout")
    last_line = stdout_raw.strip().splitlines()[-1]
    parsed = json.loads(last_line)
    if not isinstance(parsed, dict):
        raise RuntimeError("Sandbox code job returned non-dict payload")
    return parsed


async def _inspect_workspace_audio_file_safely(
    backend: Any,
    file_path: str,
) -> tuple[str | None, str | None, str | None, int | None, str | None, str | None]:
    """Inspect an audio file in the sandbox without reading its bytes into backend memory.

    Checks path containment, rejects symlinks at every level via `lstat`, verifies
    `realpath`, checks size (`0 < size <= 2 GiB`), and streams SHA-256 in 1 MiB chunks.

    Returns `(ws_root, rel_path, filename, size_bytes, sha256_hex, error_message)`.
    """
    if backend is None:
        return None, None, None, None, None, "Filesystem backend is unavailable."

    ws_dir_attr = getattr(backend, "workspace_dir", None)
    if not isinstance(ws_dir_attr, str) or not ws_dir_attr.strip():
        root_dir_attr = getattr(backend, "root_dir", None)
        ws_dir = root_dir_attr.strip() if isinstance(root_dir_attr, str) else None
    else:
        ws_dir = ws_dir_attr.strip()

    ws_root, rel_path, path_err = _validate_and_normalize_workspace_file_path(
        file_path,
        workspace_dir=ws_dir,
        allowed_extensions=TRANSCRIPTION_ALLOWED_EXTENSIONS,
    )
    if path_err is not None or ws_root is None or rel_path is None:
        return None, None, None, None, None, path_err or "Invalid path."

    chain_paths: list[str] = [ws_root]
    cur = ws_root
    for part in rel_path.split("/"):
        cur = f"{cur}/{part}"
        chain_paths.append(cur)
    target_abs = chain_paths[-1]

    req_b64 = base64.b64encode(
        json.dumps(
            {
                "ws_root": ws_root,
                "target_abs": target_abs,
                "chain_paths": chain_paths,
                "max_bytes": MAX_TRANSCRIPTION_AUDIO_BYTES,
            }
        ).encode("utf-8")
    ).decode("ascii")

    code = (
        "import base64, hashlib, json, os, stat, sys\n"
        f"req = json.loads(base64.b64decode('{req_b64}').decode('utf-8'))\n"
        "chain = req['chain_paths']\n"
        "modes = []\n"
        "try:\n"
        "    for p in chain:\n"
        "        st = os.lstat(p)\n"
        "        modes.append((st.st_mode, st.st_size))\n"
        "except OSError as exc:\n"
        "    print(json.dumps({'ok': False, 'error': f'File not found or unreadable: {exc.strerror or type(exc).__name__}'}))\n"
        "    sys.exit(0)\n"
        "for mode, _sz in modes:\n"
        "    if stat.S_ISLNK(mode):\n"
        "        print(json.dumps({'ok': False, 'error': 'Symlinks are not allowed in path.'}))\n"
        "        sys.exit(0)\n"
        "for mode, _sz in modes[:-1]:\n"
        "    if not stat.S_ISDIR(mode):\n"
        "        print(json.dumps({'ok': False, 'error': 'Intermediate path component is not a directory.'}))\n"
        "        sys.exit(0)\n"
        "leaf_mode, leaf_size = modes[-1]\n"
        "if not stat.S_ISREG(leaf_mode):\n"
        "    print(json.dumps({'ok': False, 'error': 'Target path is not a regular file.'}))\n"
        "    sys.exit(0)\n"
        "if leaf_size <= 0:\n"
        "    print(json.dumps({'ok': False, 'error': 'Audio file is empty.'}))\n"
        "    sys.exit(0)\n"
        "if leaf_size > req['max_bytes']:\n"
        "    print(json.dumps({'ok': False, 'error': f'Audio file exceeds 2 GiB limit ({leaf_size} bytes).'}))\n"
        "    sys.exit(0)\n"
        "resolved_root = os.path.realpath(req['ws_root'])\n"
        "resolved_target = os.path.realpath(req['target_abs'])\n"
        "h = hashlib.sha256()\n"
        "read_bytes = 0\n"
        "with open(resolved_target, 'rb') as fh:\n"
        "    while True:\n"
        "        chunk = fh.read(1048576)\n"
        "        if not chunk:\n"
        "            break\n"
        "        read_bytes += len(chunk)\n"
        "        h.update(chunk)\n"
        "print(json.dumps({\n"
        "    'ok': True,\n"
        "    'resolved_root': resolved_root,\n"
        "    'resolved_target': resolved_target,\n"
        "    'size_bytes': read_bytes,\n"
        "    'sha256': h.hexdigest(),\n"
        "}))\n"
    )

    try:
        parsed = await _run_sandbox_json_job(backend, code)
    except Exception as exc:
        return (
            None,
            None,
            None,
            None,
            None,
            f"Sandbox audio inspection failed: {type(exc).__name__}: {exc}",
        )

    if not parsed.get("ok"):
        return (
            None,
            None,
            None,
            None,
            None,
            str(parsed.get("error") or "Sandbox audio inspection rejected file."),
        )

    resolved_root = posixpath.normpath(str(parsed.get("resolved_root") or "")).rstrip("/")
    resolved_target = posixpath.normpath(str(parsed.get("resolved_target") or ""))
    if (
        not resolved_root.startswith("/")
        or resolved_root == ""
        or not resolved_target.startswith("/")
        or _FORBIDDEN_PATH_CHARS_RE.search(resolved_root)
        or _FORBIDDEN_PATH_CHARS_RE.search(resolved_target)
    ):
        return None, None, None, None, None, "Resolved realpath is invalid."

    try:
        common = posixpath.commonpath([resolved_root, resolved_target])
    except ValueError:
        return None, None, None, None, None, "file_path escapes caller workspace directory."

    expected_resolved_target = f"{resolved_root}/{rel_path}"
    if (
        common != resolved_root
        or not resolved_target.startswith(resolved_root + "/")
        or resolved_target != expected_resolved_target
    ):
        return None, None, None, None, None, "file_path escapes caller workspace directory."

    size_bytes = parsed.get("size_bytes")
    sha256_hex = str(parsed.get("sha256") or "").strip().lower()
    if not isinstance(size_bytes, int) or size_bytes <= 0:
        return None, None, None, None, None, f"File '{rel_path}' is empty."
    if size_bytes > MAX_TRANSCRIPTION_AUDIO_BYTES:
        return (
            None,
            None,
            None,
            None,
            None,
            f"File '{rel_path}' exceeds 2 GiB limit ({size_bytes} bytes).",
        )
    if len(sha256_hex) != 64:
        return None, None, None, None, None, "Failed computing valid SHA-256 digest."

    return resolved_root, rel_path, Path(resolved_target).name, size_bytes, sha256_hex, None


async def _write_ticket_file_in_sandbox(
    backend: Any,
    *,
    ws_root: str,
    thread_id: str | None,
    job_id: str,
    ticket_payload: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Write `ticket_payload` to a `0600` file in the sandbox without exposing the ticket on argv, script source, logs, or stdout.

    Writes the JSON credential via the sandbox backend's file-writing API
    (`backend.aupload_files` / `backend.awrite_text`) to a temporary file, then
    runs a small credential-free command that sets directory permissions (`0700`),
    sets file permissions (`0600`), and atomically replaces the target path.

    Returns `(relative_ticket_path, error_message)`.
    """
    clean_job_id = str(job_id or "").strip()
    if not _SAFE_JOB_ID_RE.fullmatch(clean_job_id):
        return None, "Invalid job_id for ticket file path."

    thread_clean = _SAFE_THREAD_SEGMENT_RE.sub("", str(thread_id or "").strip())[:8]
    if thread_clean:
        rel_dir = f".agents/threads/{thread_clean}/transcriptions"
    else:
        rel_dir = ".agents/transcriptions"

    rel_ticket_path = f"{rel_dir}/{clean_job_id}.ticket.json"
    rel_tmp_path = f"{rel_dir}/.{clean_job_id}.ticket.{uuid4().hex[:8]}.tmp"

    root_clean = ws_root.rstrip("/")
    abs_ticket_path = f"{root_clean}/{rel_ticket_path}"
    abs_tmp_path = f"{root_clean}/{rel_tmp_path}"

    ticket_text = json.dumps(ticket_payload, ensure_ascii=False, indent=2) + "\n"
    ticket_bytes = ticket_text.encode("utf-8")

    # The upload lands as 0644 (the Docker provider sets that mode), so the
    # directory is made 0700 *before* any ticket byte is written: another
    # account cannot reach the file in the window before its own chmod.
    # Only the path travels to this command.
    parent_b64 = base64.b64encode(
        json.dumps({"parent": f"{root_clean}/{rel_dir}"}).encode("utf-8")
    ).decode("ascii")
    prep_code = (
        "import base64, json, os, stat\n"
        f"parent = json.loads(base64.b64decode('{parent_b64}').decode('utf-8'))['parent']\n"
        "os.makedirs(parent, mode=0o700, exist_ok=True)\n"
        "st = os.lstat(parent)\n"
        "if not stat.S_ISDIR(st.st_mode):\n"
        "    raise RuntimeError('Ticket directory is not a directory')\n"
        "os.chmod(parent, 0o700)\n"
        "print(json.dumps({'ok': stat.S_IMODE(os.lstat(parent).st_mode) == 0o700}))\n"
    )
    try:
        prepared = await _run_sandbox_json_job(backend, prep_code)
    except Exception as exc:
        return (
            None,
            f"Failed preparing the transcription ticket directory: {type(exc).__name__}: {exc}",
        )
    if not prepared.get("ok"):
        return None, "Sandbox ticket directory permission check (0700) failed."

    upload_fn = getattr(backend, "aupload_files", None)
    write_text_fn = getattr(backend, "awrite_text", None)
    try:
        if callable(upload_fn):
            upload_res = await upload_fn([(rel_tmp_path, ticket_bytes)])
            if isinstance(upload_res, list) and upload_res:
                first_err = getattr(upload_res[0], "error", None)
                if first_err:
                    return (
                        None,
                        f"Failed uploading transcription ticket file in sandbox: {first_err}",
                    )
            elif upload_res is False:
                return None, "Failed uploading transcription ticket file in sandbox."
        elif callable(write_text_fn):
            wrote = await write_text_fn(rel_tmp_path, ticket_text)
            if wrote is False:
                return None, "Failed writing transcription ticket file in sandbox."
        else:
            return None, "Filesystem backend does not support file writes."
    except Exception as exc:
        return (
            None,
            f"Failed writing transcription ticket file in sandbox: {type(exc).__name__}: {exc}",
        )

    # Only paths (never the ticket or any secret) are passed to the chmod + atomic-replace command.
    paths_b64 = base64.b64encode(
        json.dumps(
            {
                "abs_tmp_path": abs_tmp_path,
                "abs_ticket_path": abs_ticket_path,
                "rel_ticket_path": rel_ticket_path,
            },
            ensure_ascii=False,
        ).encode("utf-8")
    ).decode("ascii")

    code = (
        "import base64, json, os, stat\n"
        f"env = json.loads(base64.b64decode('{paths_b64}').decode('utf-8'))\n"
        "tmp = env['abs_tmp_path']\n"
        "target = env['abs_ticket_path']\n"
        "parent = os.path.dirname(target)\n"
        "try:\n"
        "    os.makedirs(parent, mode=0o700, exist_ok=True)\n"
        "    os.chmod(parent, 0o700)\n"
        "    st_tmp = os.lstat(tmp)\n"
        "    if not stat.S_ISREG(st_tmp.st_mode):\n"
        "        raise RuntimeError('Temporary ticket file is not a regular file')\n"
        "    os.chmod(tmp, 0o600)\n"
        "    os.replace(tmp, target)\n"
        "    os.chmod(target, 0o600)\n"
        "    mode = stat.S_IMODE(os.lstat(target).st_mode)\n"
        "    dir_mode = stat.S_IMODE(os.lstat(parent).st_mode)\n"
        "    print(json.dumps({'ok': mode == 0o600 and dir_mode == 0o700, 'ticket_file': env['rel_ticket_path'], 'mode': mode, 'dir_mode': dir_mode}))\n"
        "finally:\n"
        "    try:\n"
        "        if os.path.lexists(tmp):\n"
        "            os.unlink(tmp)\n"
        "    except OSError:\n"
        "        pass\n"
    )

    try:
        parsed = await _run_sandbox_json_job(backend, code)
    except Exception as exc:
        return (
            None,
            f"Failed finalizing transcription ticket file in sandbox: {type(exc).__name__}: {exc}",
        )

    if not parsed.get("ok") or parsed.get("mode") != 0o600:
        return None, "Sandbox ticket file permission check (0600) failed."

    return str(parsed.get("ticket_file") or rel_ticket_path), None


def build_prepare_transcription_tool(
    filesystem_backend: Any = None,
    *,
    allow_home_workspace: bool = False,
) -> BaseTool:
    """Build `prepare_transcription` bound to `filesystem_backend`."""

    @tool("prepare_transcription")
    async def _prepare_transcription(
        path: str,
        language: str | None = None,
        hotwords: list[str] | str | None = None,
        *,
        config: RunnableConfig,
    ) -> str:
        """Prepare an audio transcription job for a workspace audio file and write a scoped task ticket to a `0600` sandbox file.

        Only admin and internal members may use transcription (external members are refused).
        Pass `language` explicitly (e.g. `'en-US'` for English audio, `'zh-CN'` for Chinese) so the ASR engine does not mis-transcribe numbers or units.
        Args:
            path: Workspace-relative or workspace-contained path to the audio file (`.mp3`, `.wav`, `.m4a`, `.ogg`, `.flac`, `.aac`, `.opus`, `.wma`, `.amr`, `.mp4`, `.webm`, <= 2 GiB).
            language: BCP-47 language code such as `'zh-CN'` or `'en-US'` (optional).
            hotwords: Domain terms, tickers, or executive names as a list of strings or comma-separated string (optional).
        Returns:
            JSON with `ok`, `job_id`, and `ticket_file` (the `0600` sandbox path holding the scoped credential).
        """
        user_id, workspace_id, thread_id, _ = _extract_run_identity(config)
        if not user_id:
            return "ERROR: user_id and workspace_id are required in RunnableConfig."

        if await get_user_tier(user_id) == "external":
            return json.dumps(
                {
                    "ok": False,
                    "status": "forbidden",
                    "error_code": "external_member_forbidden",
                    "error": EXTERNAL_TRANSCRIPTION_FORBIDDEN_MESSAGE,
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

        # Always re-verify workspace read permission against current live state
        # (never skip because `turn_admitted_workspace_id` is present), so access
        # revocation after turn start takes effect immediately.
        try:
            await verify_creator_workspace_access(workspace_id, user_id)
        except HTTPException as exc:
            return f"ERROR: {exc.detail}"
        except Exception as exc:
            return f"ERROR: {exc}"

        (
            ws_root,
            rel_path,
            filename,
            size_bytes,
            sha256_hex,
            inspect_err,
        ) = await _inspect_workspace_audio_file_safely(filesystem_backend, path)
        if (
            inspect_err is not None
            or ws_root is None
            or rel_path is None
            or filename is None
            or size_bytes is None
            or sha256_hex is None
        ):
            return f"ERROR: {inspect_err or 'Failed inspecting audio file.'}"

        try:
            job, ticket_token, exp_ts = await prepare_transcription_job(
                user_id=user_id,
                workspace_id=workspace_id,
                thread_id=thread_id,
                source_filename=filename,
                source_size_bytes=size_bytes,
                source_sha256=sha256_hex,
                language=language,
                hotwords=hotwords,
                workspace_access_verified=True,
                ticket_ttl_s=DEFAULT_TICKET_TTL_S,
            )
        except HTTPException as exc:
            return f"ERROR: {exc.detail}"
        except Exception as exc:
            logger.warning("prepare_transcription failed: %s", type(exc).__name__)
            return f"ERROR: {exc}"

        expires_at_iso = datetime.fromtimestamp(exp_ts, tz=UTC).isoformat()
        ticket_file_payload = {
            "job_id": str(job["job_id"]),
            "ticket": ticket_token,
            "workspace_id": str(job["workspace_id"]),
            "thread_id": str(job.get("thread_id") or ""),
            "source_path": rel_path,
            "source_object_key": str(job["source_object_key"]),
            "source_size_bytes": int(job["source_size_bytes"]),
            "source_sha256": str(job["source_sha256"]),
            "allowed_operations": list(ALLOWED_TICKET_OPERATIONS),
            "expires_at": expires_at_iso,
        }

        rel_ticket_path, write_err = await _write_ticket_file_in_sandbox(
            filesystem_backend,
            ws_root=ws_root,
            thread_id=thread_id,
            job_id=str(job["job_id"]),
            ticket_payload=ticket_file_payload,
        )
        if write_err is not None or rel_ticket_path is None:
            return f"ERROR: {write_err or 'Failed writing ticket file in sandbox.'}"

        return json.dumps(
            {
                "ok": True,
                "job_id": str(job["job_id"]),
                "ticket_file": rel_ticket_path,
                "status": str(job["status"]),
                "workspace_id": str(job["workspace_id"]),
                "source_path": rel_path,
                "source_size_bytes": int(job["source_size_bytes"]),
                "expires_at": expires_at_iso,
            },
            ensure_ascii=False,
        )

    return _prepare_transcription


__all__ = [
    "TRANSCRIPTION_ALLOWED_EXTENSIONS",
    "build_prepare_transcription_tool",
]
