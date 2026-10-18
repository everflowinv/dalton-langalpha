#!/usr/bin/env python3
"""
P0.2 Spike Verification Client for dalton-langalpha-broker v2.
Verifies K1 (SSE streaming), K2 (tool_calls), K3 (tool result round-trip),
K4 (reasoning_effort + reasoning_content), K5 (usage + dalton_meta.cost_usd),
and K6 (/models catalog) against loopback http://127.0.0.1:18789/dalton-broker/v1.

Security: Never logs or prints bearer tokens or provider credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:18789/dalton-broker/v1"
DEFAULT_TOKEN_PATH = Path.home() / ".openclaw/state/plugins/dalton-langalpha-broker/broker.token"


def load_token(token_path: Path) -> str:
    env_tok = os.environ.get("DALTON_BROKER_TOKEN", "").strip()
    if env_tok:
        return env_tok
    if not token_path.exists():
        raise FileNotFoundError(f"Broker token file not found at {token_path}")
    return token_path.read_text(encoding="utf-8").strip()


def http_json(
    method: str,
    url: str,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 120.0,
) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"raw_error": raw[:500]}
        return exc.code, body


def http_sse_chunks(
    url: str,
    token: str,
    payload: dict[str, Any],
    timeout: float = 120.0,
) -> tuple[int, list[dict[str, Any]], bool]:
    headers = {
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    chunks: list[dict[str, Any]] = []
    saw_done = False

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = resp.status
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data_str = line[len("data:") :].strip()
            if data_str == "[DONE]":
                saw_done = True
                break
            chunks.append(json.loads(data_str))
    return status, chunks, saw_done


def run_spike(base_url: str, token: str, models: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": base_url,
        "checks": {},
    }

    # Check 0: /health (loopback, no token)
    status_h, body_h = http_json("GET", f"{base_url}/health")
    report["checks"]["health"] = {
        "status": status_h,
        "ok": status_h == 200 and bool(body_h.get("ok")),
        "plugin_id": body_h.get("plugin_id"),
        "version": body_h.get("version"),
        "configured_models_count": body_h.get("configured_models_count"),
    }

    # Check K6: /models?detail=1
    status_m, body_m = http_json("GET", f"{base_url}/models?detail=1", token=token)
    model_items = body_m.get("data", []) if isinstance(body_m, dict) else []
    auth_ready_models = [
        m.get("id")
        for m in model_items
        if isinstance(m, dict)
        and isinstance(m.get("dalton_capabilities"), dict)
        and m["dalton_capabilities"].get("auth_ready") is True
    ]
    report["checks"]["K6_models"] = {
        "status": status_m,
        "ok": status_m == 200 and len(model_items) > 0,
        "model_count": len(model_items),
        "auth_ready_count": len(auth_ready_models),
        "sample_models": [m.get("id") for m in model_items[:8]],
        "auth_ready_models": auth_ready_models[:12],
    }

    weather_tool = {
        "type": "function",
        "function": {
            "name": "get_equity_price",
            "description": "Return latest equity price in USD for a ticker symbol.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol, e.g. AAPL"}
                },
                "required": ["ticker"],
                "additionalProperties": False,
            },
        },
    }

    per_model_results: dict[str, Any] = {}
    for model_ref in models:
        m_res: dict[str, Any] = {}
        try:
            # K1 + K4 + K5: Streaming SSE chat completion with reasoning_effort="low"
            t0 = time.monotonic()
            st_sse, sse_chunks, saw_done = http_sse_chunks(
                f"{base_url}/chat/completions",
                token=token,
                payload={
                    "model": model_ref,
                    "stream": True,
                    "reasoning_effort": "low",
                    "max_tokens": 256,
                    "messages": [
                        {"role": "system", "content": "Reply concisely in one short sentence."},
                        {"role": "user", "content": "What is 19 * 23?"},
                    ],
                },
            )
            text_accum = "".join(
                c.get("choices", [{}])[0].get("delta", {}).get("content", "") or ""
                for c in sse_chunks
            )
            reasoning_accum = "".join(
                c.get("choices", [{}])[0].get("delta", {}).get("reasoning_content", "") or ""
                for c in sse_chunks
            )
            last_chunk = sse_chunks[-1] if sse_chunks else {}
            m_res["K1_K4_K5_stream"] = {
                "status": st_sse,
                "saw_done": saw_done,
                "chunk_count": len(sse_chunks),
                "text_preview": text_accum[:120],
                "has_437": "437" in text_accum,
                "has_reasoning_delta": len(reasoning_accum) > 0,
                "usage": last_chunk.get("usage"),
                "dalton_meta": last_chunk.get("dalton_meta"),
                "elapsed_ms": round((time.monotonic() - t0) * 1000),
                "ok": st_sse == 200 and saw_done and "437" in text_accum and bool(last_chunk.get("usage")),
            }

            # K2 + K3: Tool call turn 1 -> Tool result turn 2
            session_id = f"spike-sess-{int(time.time() * 1000)}"
            st_t1, body_t1 = http_json(
                "POST",
                f"{base_url}/chat/completions",
                token=token,
                payload={
                    "model": model_ref,
                    "stream": False,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You must call get_equity_price before answering price questions.",
                        },
                        {"role": "user", "content": "What is the current equity price of AAPL?"},
                    ],
                    "tools": [weather_tool],
                    "tool_choice": "auto",
                    "dalton": {"session_id": session_id},
                },
            )
            choice1 = (body_t1.get("choices") or [{}])[0]
            msg1 = choice1.get("message") or {}
            tcalls = msg1.get("tool_calls") or []
            m_res["K2_tool_call"] = {
                "status": st_t1,
                "finish_reason": choice1.get("finish_reason"),
                "tool_calls_count": len(tcalls),
                "first_tool_call": tcalls[0] if tcalls else None,
                "ok": st_t1 == 200 and len(tcalls) > 0 and tcalls[0].get("function", {}).get("name") == "get_equity_price",
            }

            if m_res["K2_tool_call"]["ok"]:
                tc_id = tcalls[0]["id"]
                st_t2, body_t2 = http_json(
                    "POST",
                    f"{base_url}/chat/completions",
                    token=token,
                    payload={
                        "model": model_ref,
                        "stream": False,
                        "messages": [
                            {
                                "role": "system",
                                "content": "You must call get_equity_price before answering price questions.",
                            },
                            {"role": "user", "content": "What is the current equity price of AAPL?"},
                            msg1,
                            {
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": json.dumps({"ticker": "AAPL", "price_usd": 234.56, "as_of": "2026-10-18"}),
                            },
                        ],
                        "tools": [weather_tool],
                        "dalton": {"session_id": session_id},
                    },
                )
                choice2 = (body_t2.get("choices") or [{}])[0]
                msg2_content = (choice2.get("message") or {}).get("content") or ""
                m_res["K3_tool_roundtrip"] = {
                    "status": st_t2,
                    "finish_reason": choice2.get("finish_reason"),
                    "content_preview": msg2_content[:160],
                    "mentions_price": "234.56" in msg2_content,
                    "dalton_meta": body_t2.get("dalton_meta"),
                    "ok": st_t2 == 200 and "234.56" in msg2_content,
                }
        except Exception as exc:
            m_res["exception"] = str(exc)

        per_model_results[model_ref] = m_res

    report["checks"]["models_tested"] = per_model_results
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify dalton-langalpha-broker v2 spike")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--token-path", type=Path, default=DEFAULT_TOKEN_PATH)
    parser.add_argument(
        "--models",
        nargs="+",
        default=["xai/grok-4.7", "google/gemini-3.8-flash", "openai/gpt-6-astra"],
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    token = load_token(args.token_path)
    report = run_spike(args.base_url.rstrip("/"), token, args.models)
    serialized = json.dumps(report, indent=2, ensure_ascii=False)
    print(serialized)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(serialized + "\n", encoding="utf-8")

    health_ok = report["checks"].get("health", {}).get("ok") is True
    models_ok = report["checks"].get("K6_models", {}).get("ok") is True
    tested = report["checks"].get("models_tested", {})
    all_models_ok = bool(tested) and all(
        m.get("K1_K4_K5_stream", {}).get("ok") is True
        and m.get("K2_tool_call", {}).get("ok") is True
        and m.get("K3_tool_roundtrip", {}).get("ok") is True
        for m in tested.values()
    )
    return 0 if (health_ok and models_ok and all_models_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
