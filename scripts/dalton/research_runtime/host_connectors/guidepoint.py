"""Commercial Guidepoint connector with persistent ownership lock & shared deadline (§6.4b)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from src.dalton.capabilities import CapabilityId
from src.dalton.research_host.auth import CURRENT_CALLER_HEADERS

_RUNTIME_DIR = str(Path(__file__).resolve().parent.parent)
if _RUNTIME_DIR not in sys.path:
    sys.path.insert(0, _RUNTIME_DIR)

from guidepoint_lock import (  # noqa: E402
    GuidepointOwnerLock,
    GuidepointStandbyError,
    GuidepointTokenLockBusyError,
    OWNER_RESEARCH_HOST,
    read_target_owner,
    resolve_guidepoint_lock_file,
    resolve_guidepoint_tokens_file,
    resolve_ownership_file,
)

logger = logging.getLogger("dalton-research-host.guidepoint")

GUIDEPOINT_UPSTREAM_URL = "https://clapi.guidepoint.io/mcp-server/v1/mcp"
GUIDEPOINT_TOKEN_URL = "https://auth0.guidepoint.com/oauth/token"
REFRESH_SKEW_SECONDS = 60.0
DEFAULT_UPSTREAM_TIMEOUT_S = 60.0

__all__ = [
    "GuidepointConnector",
    "GuidepointOwnerLock",
    "GuidepointStandbyError",
    "GuidepointTokenLockBusyError",
    "GuidepointTokenManager",
    "OWNER_RESEARCH_HOST",
    "resolve_guidepoint_lock_file",
    "resolve_guidepoint_tokens_file",
    "resolve_ownership_file",
]


class GuidepointTokenManager:
    """Exclusive owner of `guidepoint_mcp_tokens.json` with startup ownership awareness."""

    def __init__(
        self,
        token_file: Path | str,
        *,
        client_id: str = "",
        client_secret: str = "",
        token_url: str = GUIDEPOINT_TOKEN_URL,
        ownership_file: Path | str | None = None,
        service_owner_id: str = OWNER_RESEARCH_HOST,
        http_client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ) -> None:
        self.token_file = Path(token_file).expanduser().resolve()
        self.ownership_file = (
            resolve_ownership_file(ownership_file=ownership_file)
            if ownership_file is not None
            else None
        )
        self.service_owner_id = str(service_owner_id or OWNER_RESEARCH_HOST).strip()
        self._owner_lock = GuidepointOwnerLock(self.token_file, service_name="dalton-research-host")
        self.lock_file = self._owner_lock.lock_file
        self.client_id = str(client_id or "").strip()
        self.client_secret = str(client_secret or "").strip()
        self.token_url = token_url
        self._http_client_factory = http_client_factory or httpx.AsyncClient
        self._async_lock = asyncio.Lock()
        self._tokens: dict[str, Any] = {}
        self._standby: bool = True

    @property
    def lock_held(self) -> bool:
        return self._owner_lock.held

    @property
    def standby(self) -> bool:
        return self._standby

    def current_target_owner(self) -> str:
        return read_target_owner(ownership_file=self.ownership_file)

    def initialize_from_ownership(self, *, timeout_s: float = 2.0) -> bool:
        """Read `handoff-journal.json` FIRST; acquire lock & load tokens only when target_owner matches.

        When `target_owner` differs, enters standby (`_standby = True`, `lock_held = False`)
        without touching `.guidepoint.owner.lock` or reading `guidepoint_mcp_tokens.json`.
        """
        target_owner = self.current_target_owner()
        if target_owner != self.service_owner_id:
            self._standby = True
            logger.info(
                "GuidepointTokenManager starting in standby (target_owner=%s != %s)",
                target_owner,
                self.service_owner_id,
            )
            return False
        self.acquire_owner_lock(timeout_s=timeout_s)
        if self.token_file.is_file():
            self._load_tokens_unlocked()
        self._standby = False
        return True

    def acquire_owner_lock(self, *, timeout_s: float = 2.0) -> None:
        """Acquire exclusive flock before reading, refreshing, or writing `guidepoint_mcp_tokens.json`."""
        self._owner_lock.acquire(timeout_s=timeout_s)
        self._standby = False

    def acquire_lock_for_handoff(self, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        """Admin handoff hook for `POST /admin/guidepoint/acquire-lock` (§6.4b (3) step 4)."""
        target_owner = self.current_target_owner()
        if target_owner != self.service_owner_id:
            return 409, {
                "ok": False,
                "status": "ownership_mismatch",
                "error_code": "target_owner_mismatch",
                "error": (
                    f"Cannot acquire Guidepoint lock while target_owner={target_owner!r} "
                    f"(expected {self.service_owner_id!r})."
                ),
                "target_owner": target_owner,
                "owner_lock_held": self.lock_held,
                "standby": self._standby,
            }
        raw_timeout = (body or {}).get("timeout_s", 5.0)
        try:
            timeout_s = max(0.1, min(30.0, float(raw_timeout)))
        except (TypeError, ValueError):
            timeout_s = 5.0
        previously_held = self.lock_held
        if not previously_held:
            self._owner_lock.acquire(timeout_s=timeout_s)
        try:
            self._tokens = {}
            self._load_tokens_unlocked()
        except Exception:
            if not previously_held:
                self._owner_lock.release()
                self._standby = True
            raise
        self._standby = False
        return 200, {
            "ok": True,
            "status": "lock_acquired",
            "target_owner": target_owner,
            "owner_lock_held": self.lock_held,
            "standby": self._standby,
            "token_file_exists": self.token_file.is_file(),
        }

    def release_owner_lock(self) -> None:
        self._standby = True
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

    async def ensure_fresh_token(
        self,
        *,
        now: float | None = None,
        timeout_s: float | None = None,
        deadline_mono: float | None = None,
    ) -> str:
        """Return a valid access token; immediately fail closed with `GuidepointStandbyError` if standby."""
        if self._standby or not self.lock_held:
            raise GuidepointStandbyError("lock_not_acquired")
        eff_deadline: float | None
        if deadline_mono is not None:
            eff_deadline = float(deadline_mono)
        elif timeout_s is not None:
            eff_deadline = time.monotonic() + max(0.01, float(timeout_s))
        else:
            eff_deadline = None

        if eff_deadline is not None:
            rem_lock = eff_deadline - time.monotonic()
            if rem_lock <= 0.0:
                raise asyncio.TimeoutError("Guidepoint request deadline exceeded before token lock")
            await asyncio.wait_for(self._async_lock.acquire(), timeout=rem_lock)
        else:
            await self._async_lock.acquire()

        try:
            if self._standby or not self.lock_held:
                raise GuidepointStandbyError("lock_not_acquired")
            if eff_deadline is not None and (eff_deadline - time.monotonic()) <= 0.0:
                raise asyncio.TimeoutError("Guidepoint request deadline exceeded waiting for token lock")
            if not self._tokens:
                self._load_tokens_unlocked()
            now_ts = time.time() if now is None else float(now)
            obtained_at = float(self._tokens.get("obtained_at", 0) or 0)
            expires_in = float(self._tokens.get("expires_in", 600) or 600)
            if now_ts <= obtained_at + expires_in - REFRESH_SKEW_SECONDS:
                return str(self._tokens["access_token"])

            if not self.client_id or not self.client_secret:
                raise RuntimeError("GUIDEPOINT_CLIENT_ID and GUIDEPOINT_CLIENT_SECRET are required to refresh tokens")

            if eff_deadline is not None:
                rem_refresh = eff_deadline - time.monotonic()
                if rem_refresh <= 0.0:
                    raise asyncio.TimeoutError("Guidepoint request deadline exceeded before token refresh")
                refresh_timeout = max(0.01, min(30.0, rem_refresh))
            else:
                refresh_timeout = 30.0
            async with self._http_client_factory(timeout=refresh_timeout, trust_env=False) as client:
                resp = await asyncio.wait_for(
                    client.post(
                        self.token_url,
                        data={
                            "grant_type": "refresh_token",
                            "client_id": self.client_id,
                            "client_secret": self.client_secret,
                            "refresh_token": str(self._tokens["refresh_token"]),
                        },
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    ),
                    timeout=refresh_timeout,
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
        finally:
            self._async_lock.release()

    def health(self) -> dict[str, Any]:
        return {
            "owner_lock_held": self.lock_held,
            "standby": self._standby,
            "target_owner": self.current_target_owner(),
            "token_file_exists": self.token_file.is_file(),
        }


def _resolve_request_timeout_s(arguments: dict[str, Any]) -> float:
    """Resolve shared request timeout from `X-Dalton-Request-Timeout-Ms` header and `arguments.timeout`."""
    candidates: list[float] = []
    hdrs = CURRENT_CALLER_HEADERS.get() or {}
    raw_hdr_ms = str(hdrs.get("x-dalton-request-timeout-ms") or "").strip()
    if raw_hdr_ms:
        try:
            hdr_s = float(raw_hdr_ms) / 1000.0
            if hdr_s > 0:
                candidates.append(hdr_s)
        except ValueError:
            pass
    raw_arg = arguments.get("timeout")
    if isinstance(raw_arg, (int, float)) and not isinstance(raw_arg, bool) and float(raw_arg) > 0:
        candidates.append(float(raw_arg))
    if not candidates:
        return DEFAULT_UPSTREAM_TIMEOUT_S
    return max(0.1, min(candidates))


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
        self._session_id: str | None = None

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
                        "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
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
        timeout_s: float = DEFAULT_UPSTREAM_TIMEOUT_S,
    ) -> dict[str, Any]:
        deadline_mono = time.monotonic() + max(0.1, float(timeout_s))
        access_token = await self.token_manager.ensure_fresh_token(
            deadline_mono=deadline_mono
        )
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
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if caller_key and caller_key != "anonymous":
            headers["X-Dalton-Caller-Key"] = caller_key
        rem_s = deadline_mono - time.monotonic()
        if rem_s <= 0.0:
            raise asyncio.TimeoutError("Guidepoint deadline exceeded after token refresh")
        logger.debug("guidepoint.search_library caller=%s size=%s timeout=%.2fs", caller_key, gp_args.get("size"), rem_s)
        async with self._http_client_factory(timeout=max(0.01, rem_s), trust_env=False) as client:
            resp = await asyncio.wait_for(
                client.post(self.upstream_url, json=rpc_body, headers=headers),
                timeout=max(0.01, rem_s),
            )
            if resp.headers.get("mcp-session-id"):
                self._session_id = resp.headers["mcp-session-id"].strip()
            # Streamable HTTP fallback: if upstream rejects missing/stale session (400/404), run initialize once
            if resp.status_code in (400, 404) and (deadline_mono - time.monotonic()) > 0.2:
                init_rem = deadline_mono - time.monotonic()
                if init_rem <= 0.0:
                    raise asyncio.TimeoutError("Guidepoint deadline exceeded before session initialize")
                init_headers = {k: v for k, v in headers.items() if k.lower() != "mcp-session-id"}
                init_resp = await asyncio.wait_for(
                    client.post(
                        self.upstream_url,
                        json={
                            "jsonrpc": "2.0",
                            "id": 0,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2025-03-26",
                                "capabilities": {},
                                "clientInfo": {"name": "dalton-research-host", "version": "1.0"},
                            },
                        },
                        headers=init_headers,
                    ),
                    timeout=max(0.01, init_rem),
                )
                if init_resp.status_code == 200 and init_resp.headers.get("mcp-session-id"):
                    self._session_id = init_resp.headers["mcp-session-id"].strip()
                    headers["Mcp-Session-Id"] = self._session_id
                    call_rem = deadline_mono - time.monotonic()
                    if call_rem <= 0.0:
                        raise asyncio.TimeoutError("Guidepoint deadline exceeded after session initialize")
                    resp = await asyncio.wait_for(
                        client.post(self.upstream_url, json=rpc_body, headers=headers),
                        timeout=max(0.01, call_rem),
                    )
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
        eff_timeout_s = _resolve_request_timeout_s(arguments)
        try:
            gp_args = _normalize_guidepoint_args(arguments)
            return await self.search_raw(gp_args, caller_key=caller_key, timeout_s=eff_timeout_s)
        except GuidepointStandbyError:
            return {
                "ok": False,
                "status": "standby",
                "error_code": "lock_not_acquired",
                "http_status": 503,
                "error": "Guidepoint connector is in standby (lock_not_acquired).",
            }
        except (asyncio.TimeoutError, httpx.TimeoutException):
            return {
                "ok": False,
                "status": "timeout",
                "error_code": "upstream_timeout",
                "error": f"Guidepoint upstream timed out after {eff_timeout_s:.1f}s.",
            }
        except Exception as exc:
            return {"ok": False, "status": "error", "error": str(exc)}
