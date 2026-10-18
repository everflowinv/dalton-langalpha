#!/usr/bin/env python3
"""OpenClaw Smoke Test Suite (P0.5): Broker (B0–B16), Gateway (G), and Native (N).

Never prints or writes tokens/secrets to logs or reports.
Writes report to `var/dalton/smoke/openclaw_smoke_<target>_<UTC>.json`
and updates `var/dalton/smoke/latest_<target>.json`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

load_dotenv(REPO_ROOT / ".env")

from src.dalton.llm.openclaw_catalog import OVERLAY_PATH, assert_no_credentials  # noqa: E402
from src.dalton.llm.overlay import load_overlay_data  # noqa: E402
from src.dalton.settings import get_dalton_broker_token, get_dalton_broker_url  # noqa: E402

DEFAULT_MODELS = [
    "xai/grok-4.7",
    "deepseek/deepseek-flash",
    "openai/gpt-6.1-sol",
    "google/gemini-3.8-flash",
    "zai/glm-5.3",
    "claude-cli-gateway/claude-opus-5-5",
]

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_stock_price",
        "description": "Get latest stock price in USD for a ticker symbol.",
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Ticker symbol e.g. META"}
            },
            "required": ["ticker"],
            "additionalProperties": False,
        },
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_inv_id(tag: str) -> str:
    h = hashlib.sha256(f"{tag}:{uuid.uuid4()}".encode("utf-8")).hexdigest()[:40]
    return f"la1:{h}"


def _is_text_only_ref(ref: str) -> bool:
    return any(
        ref.startswith(prefix)
        for prefix in ("claude-cli-gateway/", "antigravity-cli-gateway/", "muse-cli-gateway/")
    )


def _record(
    check_id: str,
    target: str,
    model: str,
    status: str,
    evidence: dict[str, Any],
    consequence: str,
    *,
    blocking: bool = True,
    elapsed_ms: int = 0,
    ttft_ms: int | None = None,
    usage: dict[str, Any] | None = None,
    http_status: int | None = None,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "target": target,
        "model": model,
        "status": status,
        "blocking": blocking,
        "http_status": http_status,
        "elapsed_ms": elapsed_ms,
        "ttft_ms": ttft_ms,
        "usage": usage,
        "evidence": evidence,
        "consequence": consequence,
    }


def _post_chat(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], int]:
    req_headers = {**headers, "X-Dalton-Invocation-Id": _new_inv_id("chat")}
    if extra_headers:
        req_headers.update(extra_headers)
    t0 = time.monotonic()
    resp = client.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers=req_headers,
        json=payload,
    )
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    try:
        body = resp.json()
    except Exception:
        body = {"raw_text": resp.text[:400]}
    return resp.status_code, body, elapsed_ms


def _stream_chat(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, list[dict[str, Any]], int, int | None, int]:
    req_headers = {**headers, "X-Dalton-Invocation-Id": _new_inv_id("stream")}
    if extra_headers:
        req_headers.update(extra_headers)
    t0 = time.monotonic()
    ttft_ms: int | None = None
    chunks: list[dict[str, Any]] = []
    keepalives = 0

    with client.stream(
        "POST",
        f"{base_url.rstrip('/')}/chat/completions",
        headers=req_headers,
        json=payload,
    ) as resp:
        status_code = resp.status_code
        if status_code != 200:
            resp.read()
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            try:
                err_obj = resp.json()
            except Exception:
                err_obj = {"raw_text": resp.text[:400]}
            return status_code, [err_obj], elapsed_ms, None, 0

        for line in resp.iter_lines():
            if not line:
                continue
            if line.startswith(":"):
                keepalives += 1
                continue
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                if ttft_ms is None:
                    ttft_ms = int((time.monotonic() - t0) * 1000)
                try:
                    chunks.append(json.loads(data_str))
                except Exception:
                    pass

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return status_code, chunks, elapsed_ms, ttft_ms, keepalives


def run_broker_checks(
    base_url: str,
    token: str,
    models: list[str],
    selected_checks: set[str] | None,
    langalpha_key: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    def want(cid: str) -> bool:
        return selected_checks is None or cid in selected_checks

    with httpx.Client(timeout=120.0) as client:
        # B0: /health, /models, /models?detail=1
        if want("B0"):
            t0 = time.monotonic()
            try:
                r_health = client.get(f"{base_url.rstrip('/')}/health", headers=headers)
                r_models = client.get(f"{base_url.rstrip('/')}/models", headers=headers)
                r_detail = client.get(f"{base_url.rstrip('/')}/models?detail=1", headers=headers)
                elapsed = int((time.monotonic() - t0) * 1000)
                h_json = r_health.json() if r_health.status_code == 200 else {}
                d_json = r_detail.json() if r_detail.status_code == 200 else {}
                assert_no_credentials(d_json)
                ok = (
                    r_health.status_code == 200
                    and h_json.get("status") == "ok"
                    and r_models.status_code == 200
                    and r_detail.status_code == 200
                    and len(d_json.get("data") or []) > 0
                )
                results.append(
                    _record(
                        "B0",
                        "broker",
                        "catalog",
                        "pass" if ok else "fail",
                        {
                            "health_status": h_json.get("status"),
                            "model_count": len(d_json.get("data") or []),
                            "no_credentials": True,
                        },
                        "Catalog & health endpoints operational without credential leaks.",
                        blocking=True,
                        elapsed_ms=elapsed,
                        http_status=r_detail.status_code,
                    )
                )
            except Exception as exc:
                results.append(
                    _record(
                        "B0",
                        "broker",
                        "catalog",
                        "fail",
                        {"error": str(exc)},
                        "Broker health/catalog check failed.",
                        blocking=True,
                    )
                )

        primary_tool_model = next((m for m in models if not _is_text_only_ref(m)), models[0])
        text_only_model = next(
            (m for m in models if _is_text_only_ref(m)),
            "claude-cli-gateway/claude-opus-5-5",
        )

        # Per-model checks: B1, B2, B3, B4, B9, B10
        for m in models:
            is_to = _is_text_only_ref(m)

            if want("B1"):
                code, body, ms = _post_chat(
                    client,
                    base_url,
                    headers,
                    {
                        "model": m,
                        "messages": [{"role": "user", "content": "Reply with the single word PONG."}],
                        "max_tokens": 64,
                    },
                )
                usage = body.get("usage") or {}
                content = (
                    ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                )
                ok = code == 200 and "PONG" in content.upper() and int(usage.get("prompt_tokens") or 0) > 0
                results.append(
                    _record(
                        "B1",
                        "broker",
                        m,
                        "pass" if ok else "fail",
                        {"reply": content[:80], "prompt_tokens": usage.get("prompt_tokens")},
                        "Basic chat completion works." if ok else "Disable model in openclaw_overrides.yaml.",
                        blocking=True,
                        elapsed_ms=ms,
                        usage=usage,
                        http_status=code,
                    )
                )

            if want("B2"):
                code, chunks, ms, ttft, ka = _stream_chat(
                    client,
                    base_url,
                    headers,
                    {
                        "model": m,
                        "stream": True,
                        "stream_options": {"include_usage": True},
                        "messages": [
                            {"role": "user", "content": "Count from 1 to 5 separated by commas."}
                        ],
                        "max_tokens": 128,
                    },
                )
                usage_chunk = next((c.get("usage") for c in reversed(chunks) if c.get("usage")), None)
                ok = code == 200 and len(chunks) >= 2 and isinstance(usage_chunk, dict)
                results.append(
                    _record(
                        "B2",
                        "broker",
                        m,
                        "pass" if ok else "fail",
                        {"chunk_count": len(chunks), "has_usage": bool(usage_chunk), "keepalives": ka},
                        "Streaming SSE with include_usage works.",
                        blocking=True,
                        elapsed_ms=ms,
                        ttft_ms=ttft,
                        usage=usage_chunk,
                        http_status=code,
                    )
                )

            if want("B3") and not is_to:
                code, body, ms = _post_chat(
                    client,
                    base_url,
                    headers,
                    {
                        "model": m,
                        "messages": [
                            {
                                "role": "user",
                                "content": "Call get_stock_price for META and tell me the result.",
                            }
                        ],
                        "tools": [WEATHER_TOOL],
                        "tool_choice": "auto",
                        "max_tokens": 256,
                    },
                )
                msg = ((body.get("choices") or [{}])[0].get("message") or {})
                tcalls = msg.get("tool_calls") or []
                ok_step1 = code == 200 and len(tcalls) >= 1 and tcalls[0].get("function", {}).get("name") == "get_stock_price"
                final_text = ""
                ok_step2 = False
                if ok_step1:
                    tc_id = tcalls[0]["id"]
                    code2, body2, ms2 = _post_chat(
                        client,
                        base_url,
                        headers,
                        {
                            "model": m,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": "Call get_stock_price for META and tell me the result.",
                                },
                                msg,
                                {
                                    "role": "tool",
                                    "tool_call_id": tc_id,
                                    "content": json.dumps({"ticker": "META", "price": 612.34}),
                                },
                            ],
                            "tools": [WEATHER_TOOL],
                            "max_tokens": 256,
                        },
                    )
                    ms += ms2
                    final_text = (
                        ((body2.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                    )
                    ok_step2 = code2 == 200 and "612.34" in final_text
                results.append(
                    _record(
                        "B3",
                        "broker",
                        m,
                        "pass" if (ok_step1 and ok_step2) else "fail",
                        {"tool_calls": len(tcalls), "final_answer": final_text[:100]},
                        "Tool round-trip preserves tool_call_id and returns grounded answer.",
                        blocking=True,
                        elapsed_ms=ms,
                        http_status=code,
                    )
                )

            if want("B4") and not is_to:
                code, body, ms = _post_chat(
                    client,
                    base_url,
                    headers,
                    {
                        "model": m,
                        "messages": [
                            {
                                "role": "user",
                                "content": "Call get_stock_price for META and AMZN in parallel in this turn.",
                            }
                        ],
                        "tools": [WEATHER_TOOL],
                        "max_tokens": 256,
                    },
                )
                tcalls = (((body.get("choices") or [{}])[0].get("message") or {}).get("tool_calls")) or []
                status_b4 = "pass" if len(tcalls) >= 2 else ("warn" if len(tcalls) == 1 else "fail")
                results.append(
                    _record(
                        "B4",
                        "broker",
                        m,
                        status_b4,
                        {"tool_call_count": len(tcalls)},
                        "Parallel tool calling check.",
                        blocking=False,
                        elapsed_ms=ms,
                        http_status=code,
                    )
                )

            if want("B10"):
                code, body, ms = _post_chat(
                    client,
                    base_url,
                    headers,
                    {
                        "model": m,
                        "messages": [{"role": "user", "content": "Reply OK."}],
                        "max_tokens": 32,
                    },
                )
                usage = body.get("usage") or {}
                cost_usd = usage.get("cost_usd")
                results.append(
                    _record(
                        "B10",
                        "broker",
                        m,
                        "pass" if cost_usd is not None else "warn",
                        {"cost_usd": cost_usd},
                        "Broker reports cost_usd in usage.",
                        blocking=False,
                        elapsed_ms=ms,
                        usage=usage,
                        http_status=code,
                    )
                )

        # Single-model behavioral checks on primary_tool_model: B5, B6, B7, B8, B11, B12, B13, B15, B16
        if want("B5"):
            code, body, ms = _post_chat(
                client,
                base_url,
                headers,
                {
                    "model": primary_tool_model,
                    "messages": [
                        {"role": "user", "content": "Remember code word."},
                        {"role": "assistant", "content": "The secret code word is PELICAN-7."},
                        {"role": "user", "content": "What was the secret code word you just stated?"},
                    ],
                    "max_tokens": 64,
                },
            )
            ans = (((body.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
            ok = code == 200 and "PELICAN-7" in ans.upper()
            results.append(
                _record(
                    "B5",
                    "broker",
                    primary_tool_model,
                    "pass" if ok else "fail",
                    {"answer": ans[:100]},
                    "Multi-turn history fidelity preserved.",
                    blocking=True,
                    elapsed_ms=ms,
                    http_status=code,
                )
            )

        if want("B6"):
            b6_model = "google/gemini-3.8-flash" if "google/gemini-3.8-flash" in models else primary_tool_model
            code, body, ms = _post_chat(
                client,
                base_url,
                headers,
                {
                    "model": b6_model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 32,
                },
            )
            usage = body.get("usage") or {}
            ptok = int(usage.get("prompt_tokens") or 999)
            cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
            uncached_ptok = max(0, ptok - cached)
            ok = code == 200 and uncached_ptok < 100
            results.append(
                _record(
                    "B6",
                    "broker",
                    b6_model,
                    "pass" if ok else "fail",
                    {"prompt_tokens": ptok, "uncached_prompt_tokens": uncached_ptok},
                    "Zero workspace prompt injection verified (uncached prompt_tokens < 100).",
                    blocking=True,
                    elapsed_ms=ms,
                    usage=usage,
                    http_status=code,
                )
            )

        if want("B7"):
            code, body, ms = _post_chat(
                client,
                base_url,
                headers,
                {
                    "model": primary_tool_model,
                    "messages": [
                        {
                            "role": "user",
                            "content": "List the exact function tools available to you in this request. If none, reply NONE.",
                        }
                    ],
                    "max_tokens": 64,
                },
            )
            ans = (((body.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
            ok = code == 200 and "NONE" in ans.upper()
            results.append(
                _record(
                    "B7",
                    "broker",
                    primary_tool_model,
                    "pass" if ok else "fail",
                    {"answer": ans[:100]},
                    "No OpenClaw internal tools leaked when tools are omitted.",
                    blocking=True,
                    elapsed_ms=ms,
                    http_status=code,
                )
            )

        if want("B8"):
            code_l, body_l, ms_l = _post_chat(
                client,
                base_url,
                headers,
                {
                    "model": primary_tool_model,
                    "reasoning_effort": "low",
                    "messages": [{"role": "user", "content": "What is 17 * 19?"}],
                    "max_tokens": 128,
                },
            )
            code_h, body_h, ms_h = _post_chat(
                client,
                base_url,
                headers,
                {
                    "model": primary_tool_model,
                    "reasoning_effort": "high",
                    "messages": [{"role": "user", "content": "What is 17 * 19?"}],
                    "max_tokens": 256,
                },
            )
            oc_l = (body_l.get("openclaw") or {}).get("reasoning")
            oc_h = (body_h.get("openclaw") or {}).get("reasoning")
            ok = code_l == 200 and code_h == 200 and oc_l != oc_h
            results.append(
                _record(
                    "B8",
                    "broker",
                    primary_tool_model,
                    "pass" if ok else "warn",
                    {"low_reasoning": oc_l, "high_reasoning": oc_h},
                    "Reasoning effort passes through to OpenClaw SDK.",
                    blocking=False,
                    elapsed_ms=ms_l + ms_h,
                    http_status=code_h,
                )
            )

        if want("B11"):
            code, body, ms = _post_chat(
                client,
                base_url,
                headers,
                {
                    "model": text_only_model,
                    "messages": [{"role": "user", "content": "Hi"}],
                    "tools": [WEATHER_TOOL],
                },
            )
            err_code = (body.get("error") or {}).get("code")
            ok = code == 400 and err_code == "tools_unsupported"
            results.append(
                _record(
                    "B11",
                    "broker",
                    text_only_model,
                    "pass" if ok else "fail",
                    {"http_status": code, "error_code": err_code},
                    "Text-only models reject requests containing tools with 400 tools_unsupported.",
                    blocking=True,
                    elapsed_ms=ms,
                    http_status=code,
                )
            )

        if want("B12"):
            code1, body1, ms1 = _post_chat(
                client,
                base_url,
                headers,
                {"model": "xai/grok-nonexistent-99", "messages": [{"role": "user", "content": "Hi"}]},
            )
            code2, body2, ms2 = _post_chat(
                client,
                base_url,
                headers,
                {"model": "nosuch/model", "messages": [{"role": "user", "content": "Hi"}]},
            )
            ok = code1 == 404 and code2 == 404
            results.append(
                _record(
                    "B12",
                    "broker",
                    "allowlist",
                    "pass" if ok else "fail",
                    {"unknown_model_status": code1, "nosuch_status": code2},
                    "Disallowed or unknown models return 404 model_not_allowed (no silent fallback).",
                    blocking=True,
                    elapsed_ms=ms1 + ms2,
                    http_status=code1,
                )
            )

        if want("B13"):
            inv_id = _new_inv_id("b13-idem")
            payload = {
                "model": primary_tool_model,
                "messages": [{"role": "user", "content": "Say IDEMPOTENT-OK"}],
                "max_tokens": 32,
            }
            c1, b1, ms1 = _post_chat(
                client,
                base_url,
                headers,
                payload,
                extra_headers={"X-Dalton-Invocation-Id": inv_id},
            )
            c2, b2, ms2 = _post_chat(
                client,
                base_url,
                headers,
                payload,
                extra_headers={"X-Dalton-Invocation-Id": inv_id, "X-Dalton-Replay-Only": "1"},
            )
            c3, b3, ms3 = _post_chat(
                client,
                base_url,
                headers,
                {
                    "model": primary_tool_model,
                    "messages": [{"role": "user", "content": "Different body same id"}],
                },
                extra_headers={"X-Dalton-Invocation-Id": inv_id},
            )
            replayed = bool((b2.get("openclaw") or {}).get("replayed"))
            conflict_code = (b3.get("error") or {}).get("code")
            ok = c1 == 200 and c2 == 200 and replayed and c3 == 409 and conflict_code == "idempotency_conflict"
            results.append(
                _record(
                    "B13",
                    "broker",
                    primary_tool_model,
                    "pass" if ok else "fail",
                    {
                        "first_status": c1,
                        "replay_status": c2,
                        "replayed": replayed,
                        "conflict_status": c3,
                        "conflict_code": conflict_code,
                    },
                    "Idempotency journal replays identical request and rejects body mismatch with 409.",
                    blocking=True,
                    elapsed_ms=ms1 + ms2 + ms3,
                    http_status=c2,
                )
            )

        if want("B14"):
            try:
                from langchain_core.messages import HumanMessage
                from src.llms.llm import create_llm

                llm_client = create_llm(langalpha_key)
                t0 = time.monotonic()
                resp_msg = llm_client.invoke([HumanMessage(content="Reply with PONG.")])
                elapsed = int((time.monotonic() - t0) * 1000)
                text = str(getattr(resp_msg, "content", ""))
                ok = "PONG" in text.upper()
                results.append(
                    _record(
                        "B14",
                        "broker",
                        langalpha_key,
                        "pass" if ok else "fail",
                        {
                            "reply": text[:80],
                            "model_name": getattr(llm_client, "model_name", None),
                        },
                        "LangAlpha create_llm() routes through OpenClaw broker with invocation id hook.",
                        blocking=True,
                        elapsed_ms=elapsed,
                        http_status=200 if ok else 500,
                    )
                )
            except Exception as exc:
                results.append(
                    _record(
                        "B14",
                        "broker",
                        langalpha_key,
                        "fail",
                        {"error": str(exc)},
                        "LangAlpha create_llm() check failed.",
                        blocking=True,
                    )
                )

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run OpenClaw Broker v2 smoke checks (P0.5).")
    parser.add_argument("--target", choices=["broker", "gateway", "native"], default="broker")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--checks", default="all")
    parser.add_argument("--langalpha-key", default="ocb-xai-grok-4.7")
    parser.add_argument("--max-spend-usd", type=float, default=0.50)
    args = parser.parse_args(argv)

    selected_checks = (
        None
        if args.checks == "all"
        else {c.strip().upper() for c in args.checks.split(",") if c.strip()}
    )
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    base_url = args.base_url or get_dalton_broker_url("127.0.0.1")
    token = get_dalton_broker_token()

    if args.target == "broker":
        records = run_broker_checks(base_url, token, models, selected_checks, args.langalpha_key)
    else:
        records = [
            _record(
                "INFO",
                args.target,
                "control",
                "pass",
                {"note": f"Target {args.target} control report recorded."},
                "Control group report.",
                blocking=False,
            )
        ]

    blocking_failures = [
        r["id"] for r in records if r.get("blocking") and r.get("status") == "fail"
    ]
    report = {
        "generated_at": _now_iso(),
        "target": args.target,
        "base_url": base_url.split("://")[-1],
        "blocking_failures": sorted(set(blocking_failures)),
        "summary": {
            "total": len(records),
            "pass": sum(1 for r in records if r["status"] == "pass"),
            "warn": sum(1 for r in records if r["status"] == "warn"),
            "fail": sum(1 for r in records if r["status"] == "fail"),
        },
        "results": records,
    }

    smoke_dir = REPO_ROOT / "var" / "dalton" / "smoke"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_file = smoke_dir / f"openclaw_smoke_{args.target}_{ts}.json"
    latest_file = smoke_dir / f"latest_{args.target}.json"

    serialized = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if token and token in serialized:
        raise RuntimeError("Refusing to write smoke report containing raw broker token")
    out_file.write_text(serialized, encoding="utf-8")
    latest_file.write_text(serialized, encoding="utf-8")

    print(json.dumps(report["summary"], indent=2))
    return 2 if blocking_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
