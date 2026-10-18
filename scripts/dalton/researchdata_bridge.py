#!/usr/bin/env python3
"""Host-side Streamable HTTP MCP bridge for OpenClaw's `researchdata_mcp.py`.

Listens on `127.0.0.1:8960` (configurable via `DALTON_BRIDGE_HOST` / `DALTON_BRIDGE_PORT`).
Endpoints:
  - `GET /healthz` (unauthenticated health & quota check)
  - `POST /mcp/<token>` (Streamable HTTP JSON-RPC 2.0 endpoint)

Pure Python 3.11+ standard library only.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure sibling `source_identity.py` is importable regardless of cwd
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import source_identity  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] researchdata_bridge: %(message)s",
)
logger = logging.getLogger("researchdata_bridge")

MAX_REQUEST_BYTES = 256 * 1024  # 256 KiB

# Route-level (default_s, max_s) timeout budgets
ROUTE_TIMEOUTS: dict[str, tuple[int, int]] = {
    "catalog": (15, 30),
    "company-wiki": (60, 120),
    "sales-notes": (60, 120),
    "tmtb": (60, 120),
    "funda-ai": (60, 120),
    "all": (90, 180),
    "alphaengine": (60, 120),
    "alphaengine:get_document": (240, 600),
    "guidepoint": (90, 180),
    "transcript": (90, 180),
    "sec": (90, 180),
    "estimates": (60, 120),
    "fred": (60, 120),
    "treasury": (60, 120),
    "cn-hk": (60, 120),
}


def _load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE lines into os.environ if not already set."""
    if not path.is_file():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'").strip('"')
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception as exc:
        logger.warning("Failed reading env file %s: %s", path, type(exc).__name__)


class BridgeConfig:
    def __init__(self) -> None:
        env_path = Path(
            os.environ.get(
                "DALTON_BRIDGE_ENV_FILE",
                os.path.expanduser("~/.config/dalton/researchdata-bridge.env"),
            )
        )
        _load_env_file(env_path)

        self.host: str = os.environ.get("DALTON_BRIDGE_HOST", "127.0.0.1")
        self.port: int = int(os.environ.get("DALTON_BRIDGE_PORT", "8960"))
        self.token: str = os.environ.get("DALTON_BRIDGE_TOKEN", "").strip()
        self.ocw_root: Path = Path(
            os.environ.get(
                "DALTON_OCW_ROOT",
                os.path.expanduser("~/.openclaw/workspace"),
            )
        ).resolve()
        self.python_bin: str = os.environ.get(
            "DALTON_OCW_PYTHON", "/opt/homebrew/bin/python3"
        )
        if not os.path.exists(self.python_bin):
            self.python_bin = sys.executable

        self.mcp_script: Path = Path(
            os.environ.get(
                "DALTON_MCP_SCRIPT",
                str(self.ocw_root / "scripts" / "researchdata_mcp.py"),
            )
        ).resolve()
        self.data_sources_json: Path = (
            self.ocw_root / "config" / "data_sources.json"
        )
        self.max_inflight: int = max(
            1, int(os.environ.get("DALTON_BRIDGE_MAX_INFLIGHT", "2"))
        )
        self.caller_budget_s: int = max(
            35, int(os.environ.get("DALTON_BRIDGE_CALLER_BUDGET_S", "300"))
        )
        self.usage_file: Path = Path(
            os.environ.get(
                "DALTON_BRIDGE_USAGE_FILE",
                os.path.expanduser("~/.cache/dalton/bridge-usage.json"),
            )
        )


class ResearchDataBridge:
    def __init__(self, cfg: BridgeConfig) -> None:
        self.cfg = cfg
        self.sem = asyncio.Semaphore(cfg.max_inflight)
        self.server_version: str = "unknown"
        self._cached_tools: list[dict[str, Any]] | None = None
        self._cached_version: str | None = None
        self._usage_lock = asyncio.Lock()

    def handoff_routes(self) -> set[str]:
        """Return route IDs whose `kind` is `agent_tool` or `skill` in `data_sources.json`."""
        try:
            raw = json.loads(
                self.cfg.data_sources_json.read_text(encoding="utf-8")
            )
            routes = raw.get("routes") if isinstance(raw, dict) else []
            if not isinstance(routes, list):
                return set()
            return {
                str(r["id"])
                for r in routes
                if isinstance(r, dict)
                and r.get("id")
                and r.get("kind") in ("agent_tool", "skill")
            }
        except Exception as exc:
            logger.warning("Failed to load data_sources.json: %s", exc)
            return {
                "web-news",
                "web-page",
                "filings",
                "last30days",
                "agent-reach",
                "x-timeline",
                "x-search",
                "xueqiu",
                "employee",
            }

    def compute_timeout(self, tool_name: str, arguments: dict[str, Any]) -> int:
        """Compute clamped timeout in seconds for a tool call."""
        ceiling = max(10, self.cfg.caller_budget_s - 25)
        if tool_name == "catalog":
            default_s, max_s = ROUTE_TIMEOUTS["catalog"]
        else:
            route = str(arguments.get("route") or "").strip()
            op = str(arguments.get("operation") or "").strip()
            key = f"{route}:{op}" if f"{route}:{op}" in ROUTE_TIMEOUTS else route
            default_s, max_s = ROUTE_TIMEOUTS.get(key, (60, 120))

        req_timeout = arguments.get("timeout")
        if isinstance(req_timeout, int) and req_timeout > 0:
            chosen = min(req_timeout, max_s)
        else:
            chosen = default_s
        return max(5, min(chosen, max_s, ceiling))

    async def _run_stdio_rpc(
        self, requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        """Spawn a single `researchdata_mcp.py` subprocess, send JSON-RPC lines, and read responses."""
        proc = await asyncio.create_subprocess_exec(
            self.cfg.python_bin,
            str(self.cfg.mcp_script),
            cwd=str(self.cfg.ocw_root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            payload_bytes = (
                "\n".join(json.dumps(r, ensure_ascii=False) for r in requests)
                + "\n"
            ).encode("utf-8")
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=payload_bytes),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            raise
        except Exception:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            raise

        responses: list[dict[str, Any]] = []
        for line in stdout_bytes.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    responses.append(parsed)
            except Exception:
                continue
        if not responses and stderr_bytes:
            err_tail = stderr_bytes.decode("utf-8", errors="replace").strip()[-400:]
            raise RuntimeError(f"researchdata_mcp exited without JSON response: {err_tail}")
        return responses

    async def ensure_initialized(self) -> dict[str, Any]:
        """Run `initialize` against `researchdata_mcp.py` and track `server_version`."""
        reqs = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "dalton-researchdata-bridge", "version": "0.1.0"},
                },
            }
        ]
        resps = await self._run_stdio_rpc(reqs, timeout_s=15.0)
        init_res = (resps[0].get("result") or {}) if resps else {}
        srv_info = init_res.get("serverInfo") if isinstance(init_res, dict) else {}
        ver = str((srv_info or {}).get("version") or "unknown")
        if self._cached_version and self._cached_version != ver:
            logger.info(
                "researchdata server_version changed (%s -> %s); invalidating tools/list cache",
                self._cached_version,
                ver,
            )
            self._cached_tools = None
        self.server_version = ver
        self._cached_version = ver
        return init_res

    async def get_filtered_tools(self) -> list[dict[str, Any]]:
        """Fetch `tools/list` from `researchdata_mcp.py` and strip handoff routes."""
        await self.ensure_initialized()
        if self._cached_tools is not None:
            return self._cached_tools

        reqs = [{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]
        resps = await self._run_stdio_rpc(reqs, timeout_s=15.0)
        raw_tools = (
            ((resps[0].get("result") or {}).get("tools") or []) if resps else []
        )
        handoff = self.handoff_routes()
        filtered: list[dict[str, Any]] = []
        for tool in raw_tools:
            if not isinstance(tool, dict):
                continue
            tool_copy = json.loads(json.dumps(tool))
            props = (
                tool_copy.get("inputSchema", {}).get("properties", {})
                if isinstance(tool_copy.get("inputSchema"), dict)
                else {}
            )
            route_prop = props.get("route")
            if isinstance(route_prop, dict) and isinstance(route_prop.get("enum"), list):
                route_prop["enum"] = [
                    r for r in route_prop["enum"] if r not in handoff
                ]
            filtered.append(tool_copy)

        self._cached_tools = filtered
        return filtered

    def _read_usage_today(self) -> dict[str, int]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            if self.cfg.usage_file.is_file():
                data = json.loads(self.cfg.usage_file.read_text(encoding="utf-8"))
                day_entry = data.get(today) if isinstance(data, dict) else None
                if isinstance(day_entry, dict):
                    return {
                        "calls": int(day_entry.get("calls", 0)),
                        "chars": int(day_entry.get("chars", 0)),
                    }
        except Exception:
            pass
        return {"calls": 0, "chars": 0}

    async def _record_alphaengine_get_doc(self, chars: int) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with self._usage_lock:
            try:
                self.cfg.usage_file.parent.mkdir(parents=True, exist_ok=True)
                data: dict[str, Any] = {}
                if self.cfg.usage_file.is_file():
                    try:
                        loaded = json.loads(
                            self.cfg.usage_file.read_text(encoding="utf-8")
                        )
                        if isinstance(loaded, dict):
                            data = loaded
                    except Exception:
                        data = {}
                day_entry = data.get(today)
                if not isinstance(day_entry, dict):
                    day_entry = {"calls": 0, "chars": 0}
                day_entry["calls"] = int(day_entry.get("calls", 0)) + 1
                day_entry["chars"] = int(day_entry.get("chars", 0)) + max(0, int(chars))
                data[today] = day_entry
                tmp_path = self.cfg.usage_file.with_suffix(".tmp")
                tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                tmp_path.replace(self.cfg.usage_file)
            except Exception as exc:
                logger.warning("Failed to record alphaengine usage: %s", exc)

    async def handle_tools_call(
        self, req_id: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        tool_name = str(params.get("name") or "").strip()
        raw_args = params.get("arguments")
        arguments: dict[str, Any] = (
            dict(raw_args) if isinstance(raw_args, dict) else {}
        )
        route = str(arguments.get("route") or "").strip()
        operation = str(arguments.get("operation") or "").strip()

        handoff = self.handoff_routes()
        if route and route in handoff:
            err_payload = {
                "_declared_sources": [],
                "_sources": [],
                "ok": False,
                "status": "handoff_filtered",
                "error": f"Route '{route}' is an OpenClaw handoff route and is disabled in LangAlpha.",
            }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(err_payload, ensure_ascii=False),
                        }
                    ],
                    "isError": True,
                },
            }

        eff_timeout = self.compute_timeout(tool_name, arguments)
        call_args = dict(arguments)
        if tool_name != "catalog":
            call_args["timeout"] = eff_timeout

        t0 = time.monotonic()
        async with self.sem:
            try:
                rpc_req = {
                    "jsonrpc": "2.0",
                    "id": req_id if req_id is not None else 1,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": call_args},
                }
                resps = await self._run_stdio_rpc(
                    [rpc_req], timeout_s=float(eff_timeout) + 5.0
                )
            except asyncio.TimeoutError:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                logger.warning(
                    "tools/call timeout tool=%s route=%s op=%s timeout_s=%d elapsed_ms=%d",
                    tool_name,
                    route,
                    operation,
                    eff_timeout,
                    elapsed_ms,
                )
                timeout_payload = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "timeout",
                    "error": f"researchdata_bridge timed out after {eff_timeout}s on {tool_name}/{route}",
                }
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    timeout_payload, ensure_ascii=False
                                ),
                            }
                        ],
                        "isError": True,
                    },
                }

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        resp = resps[0] if resps else {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": "Empty response from researchdata_mcp"}}
        resp["id"] = req_id

        result_obj = resp.get("result")
        status_str = "ok"
        declared_count = 0

        if isinstance(result_obj, dict) and isinstance(result_obj.get("content"), list):
            blocks = result_obj["content"]
            if blocks and isinstance(blocks[0], dict) and isinstance(blocks[0].get("text"), str):
                try:
                    payload = json.loads(blocks[0]["text"])
                except Exception:
                    payload = None

                if isinstance(payload, dict):
                    # Filter handoff routes from catalog() output too
                    if tool_name == "catalog" and isinstance(payload.get("routes"), list):
                        payload["routes"] = [
                            r
                            for r in payload["routes"]
                            if isinstance(r, dict) and r.get("id") not in handoff
                        ]
                        payload["route_count"] = len(payload["routes"])

                    declared = source_identity.declare(tool_name, arguments, payload)
                    declared_count = len(declared)
                    status_str = str(payload.get("status") or ("ok" if payload.get("ok") else "error"))

                    # Track AlphaEngine get_document quota usage
                    if route == "alphaengine" and operation == "get_document":
                        inner = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                        sc = inner.get("structuredContent") if isinstance(inner, dict) else None
                        chars = 0
                        if isinstance(sc, dict):
                            chars = int(sc.get("returned_chars") or len(str(sc.get("text") or "")))
                        await self._record_alphaengine_get_doc(chars)

                    # Place _declared_sources and _sources first so 64 KiB truncation preserves them
                    enriched = {
                        "_declared_sources": declared,
                        "_sources": declared,
                        **payload,
                    }
                    blocks[0]["text"] = json.dumps(enriched, ensure_ascii=False)
                    if isinstance(result_obj.get("structuredContent"), dict):
                        result_obj["structuredContent"] = {
                            "_declared_sources": declared,
                            "_sources": declared,
                            **result_obj["structuredContent"],
                        }

        logger.info(
            "tools/call tool=%s route=%s op=%s status=%s declared_sources=%d elapsed_ms=%d",
            tool_name,
            route or "-",
            operation or "-",
            status_str,
            declared_count,
            elapsed_ms,
        )
        return resp


async def _send_http(
    writer: asyncio.StreamWriter,
    status_code: int,
    status_text: str,
    body_Obj: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> None:
    body_bytes = (
        json.dumps(body_Obj, ensure_ascii=False).encode("utf-8")
        if body_Obj is not None
        else b""
    )
    headers = [
        f"HTTP/1.1 {status_code} {status_text}",
        "Content-Type: application/json; charset=utf-8",
        f"Content-Length: {len(body_bytes)}",
        "Connection: close",
    ]
    if extra_headers:
        for k, v in extra_headers.items():
            headers.append(f"{k}: {v}")
    header_bytes = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8")
    writer.write(header_bytes + body_bytes)
    await writer.drain()
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


async def _handle_client(
    bridge: ResearchDataBridge,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        req_line_bytes = await asyncio.wait_for(reader.readline(), timeout=15.0)
        if not req_line_bytes:
            writer.close()
            return
        req_line = req_line_bytes.decode("utf-8", errors="replace").strip()
        parts = req_line.split(" ")
        if len(parts) < 2:
            await _send_http(writer, 400, "Bad Request", {"error": "Bad Request"})
            return
        method, raw_path = parts[0].upper(), parts[1]
        path = raw_path.split("?", 1)[0]

        # Read headers
        headers: dict[str, str] = {}
        while True:
            hline = await asyncio.wait_for(reader.readline(), timeout=15.0)
            if not hline or hline in (b"\r\n", b"\n"):
                break
            decoded = hline.decode("utf-8", errors="replace").strip()
            if ":" in decoded:
                hk, hv = decoded.split(":", 1)
                headers[hk.strip().lower()] = hv.strip()

        content_length = int(headers.get("content-length") or "0")
        if content_length > MAX_REQUEST_BYTES:
            await _send_http(
                writer, 413, "Payload Too Large", {"error": "Request body exceeds 256 KiB"}
            )
            return

        body_bytes = b""
        if content_length > 0:
            body_bytes = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=15.0
            )

        # Route 1: GET /healthz
        if method == "GET" and path == "/healthz":
            if bridge.server_version == "unknown":
                try:
                    await bridge.ensure_initialized()
                except Exception as exc:
                    logger.warning("Healthz init probe failed: %s", exc)
            health_payload = {
                "ok": True,
                "status": "ok",
                "server_version": bridge.server_version,
                "max_inflight": bridge.cfg.max_inflight,
                "caller_budget_s": bridge.cfg.caller_budget_s,
                "alphaengine_get_document_today": bridge._read_usage_today(),
            }
            await _send_http(writer, 200, "OK", health_payload)
            return

        # Route 2: /mcp/<token>
        if not path.startswith("/mcp/"):
            await _send_http(writer, 404, "Not Found", {"error": "Not Found"})
            return

        supplied_token = path[len("/mcp/") :].strip("/")
        expected_token = bridge.cfg.token
        if not expected_token or not supplied_token or not hmac.compare_digest(
            supplied_token.encode("utf-8"), expected_token.encode("utf-8")
        ):
            # Constant-time rejection; NEVER log supplied_token or path
            await _send_http(writer, 404, "Not Found", {"error": "Not Found"})
            return

        if method in ("GET", "DELETE"):
            await _send_http(
                writer, 405, "Method Not Allowed", {"error": "Method Not Allowed"}
            )
            return

        if method != "POST":
            await _send_http(
                writer, 405, "Method Not Allowed", {"error": "Method Not Allowed"}
            )
            return

        try:
            rpc_msg = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            await _send_http(
                writer,
                400,
                "Bad Request",
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                },
            )
            return

        if not isinstance(rpc_msg, dict):
            await _send_http(
                writer,
                400,
                "Bad Request",
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Invalid Request"},
                },
            )
            return

        req_id = rpc_msg.get("id")
        rpc_method = str(rpc_msg.get("method") or "")
        rpc_params = rpc_msg.get("params") if isinstance(rpc_msg.get("params"), dict) else {}

        # JSON-RPC notifications (no id, e.g., notifications/initialized) -> 202 Accepted
        if "id" not in rpc_msg or req_id is None:
            await _send_http(writer, 202, "Accepted", None)
            return

        if rpc_method == "initialize":
            init_res = await bridge.ensure_initialized()
            offered_proto = str(rpc_params.get("protocolVersion") or "2024-11-05")
            adopted_proto = (
                offered_proto
                if offered_proto in ("2024-11-05", "2025-03-26", "2025-11-25")
                else str(init_res.get("protocolVersion") or "2024-11-05")
            )
            reply = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": adopted_proto,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "researchdata",
                        "version": bridge.server_version,
                    },
                },
            }
            await _send_http(writer, 200, "OK", reply)
            return

        if rpc_method == "ping":
            await _send_http(
                writer, 200, "OK", {"jsonrpc": "2.0", "id": req_id, "result": {}}
            )
            return

        if rpc_method == "tools/list":
            tools = await bridge.get_filtered_tools()
            await _send_http(
                writer,
                200,
                "OK",
                {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}},
            )
            return

        if rpc_method == "tools/call":
            reply = await bridge.handle_tools_call(req_id, rpc_params)
            await _send_http(writer, 200, "OK", reply)
            return

        # Unsupported method (e.g. server/discover probe -> -32601 so client falls back to legacy initialize)
        await _send_http(
            writer,
            200,
            "OK",
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {rpc_method}"},
            },
        )
    except Exception as exc:
        logger.error("Unhandled bridge error: %s", type(exc).__name__)
        try:
            await _send_http(
                writer,
                500,
                "Internal Server Error",
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": "Internal bridge error"},
                },
            )
        except Exception:
            pass


async def main() -> None:
    cfg = BridgeConfig()
    if not cfg.token:
        logger.error(
            "DALTON_BRIDGE_TOKEN is not set (checked env and ~/.config/dalton/researchdata-bridge.env)"
        )
        sys.exit(1)

    bridge = ResearchDataBridge(cfg)
    try:
        await bridge.ensure_initialized()
    except Exception as exc:
        logger.warning("Startup initialize probe warning: %s", exc)

    server = await asyncio.start_server(
        lambda r, w: _handle_client(bridge, r, w),
        host=cfg.host,
        port=cfg.port,
    )
    logger.info(
        "Listening on http://%s:%d (server_version=%s, max_inflight=%d, caller_budget_s=%d)",
        cfg.host,
        cfg.port,
        bridge.server_version,
        cfg.max_inflight,
        cfg.caller_budget_s,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
