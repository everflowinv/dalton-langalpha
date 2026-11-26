"""Commercial Guidepoint connector with single-process exclusive token refresh lock (§2.2, §6.4)."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from src.dalton.capabilities import CapabilityId

logger = logging.getLogger("dalton-research-host.guidepoint")


class GuidepointTokenLockBusyError(RuntimeError):
    """Raised when `.guidepoint.owner.lock` is already held by another owner process."""


def resolve_guidepoint_tokens_file(
    *,
    data_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the canonical Guidepoint OAuth token JSON path."""
    env = os.environ if environ is None else environ
    explicit = str(env.get("GUIDEPOINT_TOKENS_FILE") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    if data_root is not None:
        base = Path(data_root).expanduser().resolve()
    else:
        raw_home = str(env.get("DALTON_RESEARCH_HOME") or "").strip()
        if raw_home:
            base = Path(raw_home).expanduser().resolve()
        else:
            base = (Path.home() / "Library" / "Application Support" / "Everflow" / "research").resolve()
    return base / "config" / "guidepoint_mcp_tokens.json"


def resolve_guidepoint_lock_file(token_file: Path | str) -> Path:
    """Return the shared `.guidepoint.owner.lock` path alongside `token_file`."""
    return Path(token_file).expanduser().resolve().parent / ".guidepoint.owner.lock"


class GuidepointOwnerLock:
    """Process-scoped exclusive `fcntl.flock` guard for `guidepoint_mcp_tokens.json`."""

    def __init__(self, token_file: Path | str, *, service_name: str = "guidepoint") -> None:
        self.token_file = Path(token_file).expanduser().resolve()
        self.lock_file = resolve_guidepoint_lock_file(self.token_file)
        self.service_name = service_name
        self._lock_fd: int | None = None

    @property
    def held(self) -> bool:
        return self._lock_fd is not None

    def acquire(self, *, timeout_s: float = 2.0) -> None:
        if self._lock_fd is not None:
            return
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_file), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        acquired = False
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise GuidepointTokenLockBusyError(
                            f"Guidepoint token owner lock busy: {self.lock_file}"
                        ) from None
                    time.sleep(0.05)
            meta = {
                "pid": os.getpid(),
                "service": self.service_name,
                "token_file": str(self.token_file),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            payload = (json.dumps(meta) + "\n").encode("utf-8")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
            os.fsync(fd)
            self._lock_fd = fd
        except BaseException:
            if acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def release(self) -> None:
        fd = self._lock_fd
        self._lock_fd = None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

GUIDEPOINT_UPSTREAM_URL = "https://clapi.guidepoint.io/mcp-server/v1/mcp"
GUIDEPOINT_TOKEN_URL = "https://auth0.guidepoint.com/oauth/token"
REFRESH_SKEW_SECONDS = 60.0

__all__ = [
    "GuidepointConnector",
    "GuidepointOwnerLock",
    "GuidepointTokenLockBusyError",
    "GuidepointTokenManager",
    "resolve_guidepoint_lock_file",
    "resolve_guidepoint_tokens_file",
]


class GuidepointTokenManager:
    """Exclusive owner of `guidepoint_mcp_tokens.json` with a single `asyncio.Lock` + flock."""

    def __init__(
        self,
        token_file: Path | str,
        *,
        client_id: str = "",
        client_secret: str = "",
        token_url: str = GUIDEPOINT_TOKEN_URL,
        http_client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ) -> None:
        self.token_file = Path(token_file).expanduser().resolve()
        self._owner_lock = GuidepointOwnerLock(self.token_file, service_name="dalton-research-host")
        self.lock_file = self._owner_lock.lock_file
        self.client_id = str(client_id or "").strip()
        self.client_secret = str(client_secret or "").strip()
        self.token_url = token_url
        self._http_client_factory = http_client_factory or httpx.AsyncClient
        self._async_lock = asyncio.Lock()
        self._tokens: dict[str, Any] = {}

    @property
    def lock_held(self) -> bool:
        return self._owner_lock.held

    def acquire_owner_lock(self, *, timeout_s: float = 2.0) -> None:
        """Acquire exclusive flock before reading, refreshing, or writing `guidepoint_mcp_tokens.json`."""
        self._owner_lock.acquire(timeout_s=timeout_s)

    def release_owner_lock(self) -> None:
        self._owner_lock.release()

    def _load_tokens_unlocked(self) -> dict[str, Any]:
        if not self.token_file.is_file():
            raise FileNotFoundError(f"Guidepoint token store missing: {self.token_file}")
        data = json.loads(self.token_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "access_token" not in data or "refresh_token" not in data:
            raise ValueError("Guidepoint token store is missing access_token or refresh_token")
        self._tokens = data
        return data

    def _save_tokens_atomic_unlocked(self, tokens: dict[str, Any]) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.token_file.parent),
            prefix=".guidepoint_mcp_tokens.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(tokens, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.token_file)
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    async def ensure_fresh_token(self, *, now: float | None = None) -> str:
        """Return a valid access token, refreshing serially under `self._async_lock` when near expiry."""
        async with self._async_lock:
            if not self.lock_held:
                self.acquire_owner_lock()
            if not self._tokens:
                self._load_tokens_unlocked()
            now_ts = time.time() if now is None else float(now)
            obtained_at = float(self._tokens.get("obtained_at", 0) or 0)
            expires_in = float(self._tokens.get("expires_in", 600) or 600)
            if now_ts <= obtained_at + expires_in - REFRESH_SKEW_SECONDS:
                return str(self._tokens["access_token"])

            if not self.client_id or not self.client_secret:
                raise RuntimeError("GUIDEPOINT_CLIENT_ID and GUIDEPOINT_CLIENT_SECRET are required to refresh tokens")

            async with self._http_client_factory(timeout=30.0, trust_env=False) as client:
                resp = await client.post(
                    self.token_url,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "refresh_token": str(self._tokens["refresh_token"]),
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            resp.raise_for_status()
            refreshed = resp.json()
            if not isinstance(refreshed, dict) or not refreshed.get("access_token"):
                raise RuntimeError("Guidepoint OAuth refresh returned invalid payload")
            refreshed.setdefault("refresh_token", self._tokens["refresh_token"])
            refreshed["obtained_at"] = now_ts
            self._tokens.update(refreshed)
            self._save_tokens_atomic_unlocked(self._tokens)
            return str(self._tokens["access_token"])

    def health(self) -> dict[str, Any]:
        return {
            "owner_lock_held": self.lock_held,
            "token_file_exists": self.token_file.is_file(),
        }


def _normalize_guidepoint_args(arguments: dict[str, Any]) -> dict[str, Any]:
    params = dict(arguments.get("params") or {})
    query_parts = [
        str(arguments.get("query") or params.get("query") or "").strip(),
        str(arguments.get("ticker") or "").strip(),
    ]
    merged_query = " ".join(p for p in dict.fromkeys(query_parts) if p)
    if not merged_query or len(merged_query) > 1000:
        raise ValueError("Guidepoint search_library requires a 1-1000 char query")
    out: dict[str, Any] = {"query": merged_query}

    raw_size = params.get("size") if "size" in params else arguments.get("size", arguments.get("limit", 3))
    size_val = max(1, min(30, int(raw_size if raw_size is not None else 3)))
    out["size"] = size_val

    start_date = arguments.get("start") or arguments.get("start_date") or params.get("start_date")
    end_date = arguments.get("end") or arguments.get("end_date") or params.get("end_date")
    if start_date:
        out["start_date"] = str(start_date).strip()
    if end_date:
        out["end_date"] = str(end_date).strip()

    keywords = params.get("keywords") if "keywords" in params else arguments.get("keywords")
    if isinstance(keywords, str) and keywords.strip():
        keywords = [keywords.strip()]
    if isinstance(keywords, list):
        cleaned_kw = [str(k).strip() for k in keywords if isinstance(k, str) and k.strip()]
        if len(cleaned_kw) > 10:
            raise ValueError("Guidepoint keywords allows at most 10 items")
        if cleaned_kw:
            out["keywords"] = cleaned_kw

    recency = params.get("recency_bias") if "recency_bias" in params else arguments.get("recency_bias")
    if recency is not None and str(recency).strip() != "":
        bias = int(recency)
        if not 1 <= bias <= 100:
            raise ValueError("Guidepoint recency_bias must be 1-100")
        out["recency_bias"] = bias

    return out


class GuidepointConnector:
    """MCP connector mounted at ``/mcp/guidepoint`` preserving `data.structuredContent.data`."""

    route_id = "guidepoint"
    server_name = "dalton_guidepoint"
    required_capabilities = (CapabilityId.GUIDEPOINT,)
    require_all_capabilities = True

    def __init__(
        self,
        token_manager: GuidepointTokenManager,
        *,
        upstream_url: str = GUIDEPOINT_UPSTREAM_URL,
        http_client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ) -> None:
        self.token_manager = token_manager
        self.upstream_url = upstream_url
        self._http_client_factory = http_client_factory or httpx.AsyncClient

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "search_library",
                "description": "Search Guidepoint expert interview transcripts (returns Q&A excerpts; max size 30).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Research query (1-1000 chars)"},
                        "ticker": {"type": "string", "description": "Optional ticker merged into query"},
                        "start": {"type": "string", "description": "Start UTC date YYYY-MM-DD"},
                        "end": {"type": "string", "description": "End UTC date YYYY-MM-DD"},
                        "limit": {"type": "integer", "default": 3, "minimum": 1, "maximum": 30},
                        "params": {
                            "type": "object",
                            "description": "Optional start_date, end_date, size (1-30), keywords (1-10), recency_bias (1-100)",
                        },
                    },
                    "required": ["query"],
                },
            }
        ]

    async def search_raw(
        self,
        gp_args: dict[str, Any],
        *,
        caller_key: str = "anonymous",
    ) -> dict[str, Any]:
        access_token = await self.token_manager.ensure_fresh_token()
        rpc_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_library", "arguments": gp_args},
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {access_token}",
        }
        if caller_key and caller_key != "anonymous":
            headers["X-Dalton-Caller-Key"] = caller_key
        logger.debug("guidepoint.search_library caller=%s size=%s", caller_key, gp_args.get("size"))
        async with self._http_client_factory(timeout=60.0, trust_env=False) as client:
            resp = await client.post(self.upstream_url, json=rpc_body, headers=headers)
        resp.raise_for_status()
        raw_text = resp.text
        if raw_text.lstrip().startswith("{"):
            msg = json.loads(raw_text)
        else:
            data_lines = [line[5:].strip() for line in raw_text.splitlines() if line.startswith("data:")]
            msg = json.loads(data_lines[-1]) if data_lines else {}
        if "error" in msg:
            return {"ok": False, "status": "error", "error": str(msg["error"])}
        result_obj = msg.get("result") or {}
        if result_obj.get("isError"):
            return {"ok": False, "status": "error", "error": str(result_obj.get("content") or "Guidepoint error")}

        sc = result_obj.get("structuredContent")
        if not isinstance(sc, dict) or not isinstance(sc.get("data"), list):
            for blk in result_obj.get("content") or []:
                if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                    try:
                        cand = json.loads(blk["text"])
                        if isinstance(cand, dict) and isinstance(cand.get("data"), list):
                            sc = cand
                            break
                    except ValueError:
                        continue
        rows = list(sc.get("data") or []) if isinstance(sc, dict) else []
        sources: list[dict[str, Any]] = []
        for idx, row in enumerate(rows[:50]):
            if not isinstance(row, dict):
                continue
            tid = str(row.get("transcript_id") or row.get("id") or f"gp-{idx}")
            title = str(row.get("transcript_name") or row.get("title") or tid)
            as_of = str(row.get("date") or "")[:10]
            sources.append(
                {
                    "document_id": f"guidepoint:{tid}"[:200],
                    "identifier": f"guidepoint:{tid}"[:200],
                    "route": "guidepoint",
                    "source_type": "research_doc",
                    "identity": "expert",
                    "detail": "expert",
                    "publisher": "Guidepoint",
                    "provider": "dalton_guidepoint:Guidepoint",
                    "title": title[:300],
                    "as_of": as_of,
                    "private": True,
                }
            )
        out: dict[str, Any] = {
            "ok": True,
            "status": "ok" if rows else "empty",
            "data": {"structuredContent": {"data": rows}},
            "results": rows,
            "count": len(rows),
            "cursor": None,
            "has_more": False,
            "total_upstream": len(rows),
            "returned": len(rows),
            "caller_key": caller_key,
        }
        if sources:
            out["_sources"] = sources
        return out

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        active_caps: frozenset[CapabilityId],
        caller_key: str = "anonymous",
    ) -> dict[str, Any]:
        if CapabilityId.GUIDEPOINT not in active_caps:
            return {
                "ok": False,
                "status": "capability_disabled",
                "error_code": "capability_disabled",
                "http_status": 403,
                "error": "Capability 'guidepoint' is disabled.",
            }
        if tool_name not in ("search_library", "search", "query"):
            return {"ok": False, "status": "error", "error": f"Unknown guidepoint tool: {tool_name}"}
        try:
            gp_args = _normalize_guidepoint_args(arguments)
            return await self.search_raw(gp_args, caller_key=caller_key)
        except Exception as exc:
            return {"ok": False, "status": "error", "error": str(exc)}
