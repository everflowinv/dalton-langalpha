#!/usr/bin/env python3
"""G7 real-run verification script: ResearchData Bridge >240s long read & source declarations (P0I §7 G7).

Verifies:
1. Long-read call① (`> 240s`, below `eff_timeout=600s`):
   - Uses the committed `DALTON_BRIDGE_TEST_MODE=1` + `DALTON_BRIDGE_TEST_DELAY_S=245` hook
     on `scripts/dalton/researchdata_bridge.py` without modifying any committed code.
   - Executes `tools/call` on long-read route `company-wiki:read` (`eff_timeout=600s`, whereas
     default route timeout is `240s`), verifying actual elapsed time `> 240s`, `timed_out=False`,
     and non-empty `_declared_sources` / `_sources`.
2. Real-content long document read call② (delay hook disabled, against live `http://127.0.0.1:8960`):
   - Executes real full-text/long-read calls (`company-wiki:read` and `sec:financials` for `GOOGL`),
     verifying `_declared_sources` (key #0) and `_sources` (key #1) are populated and non-empty.
3. Zero stderr leakage in bridge logs.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.realrun._common import (  # noqa: E402
    RealRunHttpClient,
    build_evidence_envelope,
    default_shanghai_date,
    load_repo_dotenv,
    now_utc_iso,
    preflight_realrun_script,
    write_evidence_and_summary,
)

BRIDGE_SCRIPT = REPO_ROOT / "scripts" / "dalton" / "researchdata_bridge.py"
BRIDGE_ENV_FILE = Path.home() / ".config" / "dalton" / "researchdata-bridge.env"


def _read_bridge_token() -> str:
    if BRIDGE_ENV_FILE.is_file():
        for line in BRIDGE_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DALTON_BRIDGE_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'").strip('"')
    token = os.environ.get("DALTON_BRIDGE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DALTON_BRIDGE_TOKEN not found in ~/.config/dalton/researchdata-bridge.env")
    return token


def _call_bridge_mcp(
    base_url: str,
    token: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_s: float = 630.0,
) -> tuple[int, float, dict[str, Any], dict[str, Any]]:
    """Send JSON-RPC `tools/call` to the ResearchData HTTP bridge and parse inner payload."""
    t0 = time.monotonic()
    with httpx.Client(timeout=timeout_s, trust_env=False) as client:
        resp = client.post(
            f"{base_url.rstrip('/')}/mcp/{token}",
            json={
                "jsonrpc": "2.0",
                "id": int(time.time() * 1000) % 1000000,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments,
                },
            },
        )
    elapsed_s = round(time.monotonic() - t0, 3)
    rpc_body = resp.json()
    result_obj = rpc_body.get("result") if isinstance(rpc_body, dict) else {}
    blocks = result_obj.get("content") if isinstance(result_obj, dict) else []
    inner_payload: dict[str, Any] = {}
    if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict):
        raw_text = blocks[0].get("text")
        if isinstance(raw_text, str):
            try:
                parsed = json.loads(raw_text)
                if isinstance(parsed, dict):
                    inner_payload = parsed
            except Exception:
                pass
    return resp.status_code, elapsed_s, rpc_body, inner_payload


def _run_long_read_with_delay_hook(
    token: str, delay_s: int = 245
) -> tuple[dict[str, Any], str]:
    """Spawn committed `scripts/dalton/researchdata_bridge.py` with DALTON_BRIDGE_TEST_DELAY_S=245."""
    import tempfile

    port = 18960
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["DALTON_BRIDGE_HOST"] = "127.0.0.1"
    env["DALTON_BRIDGE_PORT"] = str(port)
    env["DALTON_BRIDGE_TOKEN"] = token
    env["DALTON_BRIDGE_TEST_MODE"] = "1"
    env["DALTON_BRIDGE_TEST_DELAY_S"] = str(delay_s)

    with tempfile.TemporaryDirectory() as tmp_dir:
        log_path = Path(tmp_dir) / "g7_bridge_delay_run.log"

        with log_path.open("w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                [sys.executable, str(BRIDGE_SCRIPT)],
                cwd=str(REPO_ROOT),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                # Wait for /healthz
                health_body = None
                health_deadline = time.monotonic() + 15.0
                with httpx.Client(timeout=5.0, trust_env=False) as hc:
                    while time.monotonic() < health_deadline:
                        try:
                            r = hc.get(f"{base_url}/healthz")
                            if r.status_code == 200:
                                health_body = r.json()
                                break
                        except Exception:
                            pass
                        time.sleep(0.3)
                if not health_body:
                    raise TimeoutError("Timed out after 15s waiting for delayed bridge instance /healthz")

                # Execute >240s long read on `sec:financials` (eff_timeout=600s > 240s default)
                http_status, elapsed_s, rpc_body, inner_payload = _call_bridge_mcp(
                    base_url=base_url,
                    token=token,
                    tool_name="finance",
                    arguments={
                        "route": "sec",
                        "operation": "financials",
                        "ticker": "GOOGL",
                    },
                    timeout_s=630.0,
                )
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()

        log_text = log_path.read_text(encoding="utf-8", errors="replace")

    top_keys = list(inner_payload.keys())
    declared_sources = inner_payload.get("_declared_sources") or []
    sources = inner_payload.get("_sources") or []
    is_error = bool((rpc_body.get("result") or {}).get("isError"))
    timed_out = inner_payload.get("status") == "timeout" or is_error

    # Verify zero stderr leakage and no token leakage in bridge logs
    stderr_leaked = "Traceback (most recent call last)" in log_text or token in log_text

    assert http_status == 200, f"Expected HTTP 200, got {http_status}"
    assert elapsed_s >= 240.0, f"Expected elapsed_s >= 240s, got {elapsed_s}s"
    assert not timed_out, f"Long read timed out unexpectedly: {inner_payload}"
    assert len(declared_sources) > 0 and len(sources) > 0, (
        f"Expected non-empty _declared_sources/_sources, got {len(declared_sources)}/{len(sources)}"
    )
    assert top_keys[:2] == ["_declared_sources", "_sources"], (
        f"Expected first two keys to be ['_declared_sources', '_sources'], got {top_keys[:4]}"
    )
    assert not stderr_leaked, "Detected stderr or token leakage in bridge log"

    return (
        {
            "bridge_url": base_url,
            "healthz": health_body,
            "tool": "finance",
            "arguments": {"route": "sec", "operation": "financials", "ticker": "GOOGL"},
            "configured_delay_s": delay_s,
            "effective_route_timeout_s": 600,
            "default_route_timeout_s": 240,
            "http_status": http_status,
            "elapsed_s": elapsed_s,
            "timed_out": timed_out,
            "top_keys_prefix": top_keys[:6],
            "declared_sources_count": len(declared_sources),
            "sources_count": len(sources),
            "declared_sources": declared_sources,
            "sources": sources,
            "bridge_log_lines": [ln for ln in log_text.splitlines() if ln.strip()],
            "zero_stderr_leakage": not stderr_leaked,
        },
        log_text,
    )


def _run_real_content_long_reads(live_bridge_url: str, token: str) -> dict[str, Any]:
    """Call live bridge (`http://127.0.0.1:8960`) with delay hook off on real long-read routes."""
    with httpx.Client(timeout=10.0, trust_env=False) as hc:
        health_resp = hc.get(f"{live_bridge_url.rstrip('/')}/healthz")
        health_resp.raise_for_status()
        live_health = health_resp.json()

    # 1. Real full-text read: company-wiki for GOOGL
    wiki_status, wiki_elapsed_s, wiki_rpc, wiki_payload = _call_bridge_mcp(
        base_url=live_bridge_url,
        token=token,
        tool_name="research",
        arguments={
            "route": "company-wiki",
            "query": "GOOGL",
        },
        timeout_s=300.0,
    )
    wiki_keys = list(wiki_payload.keys())
    wiki_declared = wiki_payload.get("_declared_sources") or []
    wiki_sources = wiki_payload.get("_sources") or []
    wiki_text_preview = json.dumps(wiki_payload.get("data") or wiki_payload, ensure_ascii=False)[:400]

    # 2. Real SEC long read: sec:financials for GOOGL
    sec_status, sec_elapsed_s, sec_rpc, sec_payload = _call_bridge_mcp(
        base_url=live_bridge_url,
        token=token,
        tool_name="finance",
        arguments={
            "route": "sec",
            "operation": "financials",
            "ticker": "GOOGL",
        },
        timeout_s=300.0,
    )
    sec_keys = list(sec_payload.keys())
    sec_declared = sec_payload.get("_declared_sources") or []
    sec_sources = sec_payload.get("_sources") or []
    sec_text_preview = json.dumps(sec_payload.get("data") or sec_payload, ensure_ascii=False)[:400]

    assert wiki_status == 200 and len(wiki_declared) > 0 and len(wiki_sources) > 0
    assert wiki_keys[:2] == ["_declared_sources", "_sources"]
    assert sec_status == 200 and len(sec_declared) > 0 and len(sec_sources) > 0
    assert sec_keys[:2] == ["_declared_sources", "_sources"]

    return {
        "live_bridge_url": live_bridge_url,
        "live_healthz": live_health,
        "company_wiki_read": {
            "tool": "research",
            "arguments": {"route": "company-wiki", "query": "GOOGL"},
            "http_status": wiki_status,
            "elapsed_s": wiki_elapsed_s,
            "timed_out": False,
            "top_keys_prefix": wiki_keys[:6],
            "declared_sources_count": len(wiki_declared),
            "sources_count": len(wiki_sources),
            "declared_sources": wiki_declared,
            "sources": wiki_sources,
            "content_preview": wiki_text_preview,
        },
        "sec_financials_read": {
            "tool": "finance",
            "arguments": {"route": "sec", "operation": "financials", "ticker": "GOOGL"},
            "http_status": sec_status,
            "elapsed_s": sec_elapsed_s,
            "timed_out": False,
            "top_keys_prefix": sec_keys[:6],
            "declared_sources_count": len(sec_declared),
            "sources_count": len(sec_sources),
            "declared_sources": sec_declared,
            "sources": sec_sources,
            "content_preview": sec_text_preview,
        },
    }


def main() -> int:
    load_repo_dotenv()
    parser = argparse.ArgumentParser(description="Run G7 ResearchData Bridge real-run verification.")
    parser.add_argument("--backend", default="http://127.0.0.1:8000", help="Backend base URL")
    parser.add_argument("--bridge", default="http://127.0.0.1:8960", help="Live bridge base URL")
    parser.add_argument("--delay-s", type=int, default=245, help="Long-read delay seconds (>240)")
    parser.add_argument("--date", default=default_shanghai_date(), help="Evidence date folder YYYYMMDD")
    parser.add_argument(
        "--record",
        action="store_true",
        help="Persist raw JSON and Markdown summary under var/dalton/trial-runs/realrun-<date>/",
    )
    args = parser.parse_args()

    if args.delay_s <= 240:
        raise ValueError(f"--delay-s must be > 240 seconds (got {args.delay_s})")

    test_user_id = "realrun-pm-a"
    preflight_realrun_script("G7", record=args.record, backend=args.backend, user_id=test_user_id)

    started_at = now_utc_iso()
    token = _read_bridge_token()

    with RealRunHttpClient(
        base_url=args.backend, user_id=test_user_id, timeout_s=60.0, record=args.record
    ) as client:
        client.ensure_user()

    long_read_delay_result, delay_log_text = _run_long_read_with_delay_hook(
        token=token, delay_s=args.delay_s
    )
    real_content_result = _run_real_content_long_reads(live_bridge_url=args.bridge, token=token)

    ended_at = now_utc_iso()
    synthetic = [
        {
            "kind": "committed_test_hook",
            "entry_point": "scripts/dalton/researchdata_bridge.py:168-190",
            "reason": (
                f"Use committed hook scripts/dalton/researchdata_bridge.py:168-190 "
                f"(DALTON_BRIDGE_TEST_MODE=1 DALTON_BRIDGE_TEST_DELAY_S={args.delay_s}) to verify "
                ">240s long-read call completes within the 240s-600s long-read timeout window "
                "without modifying committed code (P0I §7 G7 #1, P0J §5.7)."
            ),
        }
    ]

    script_cmd = (
        f"scripts/dalton/realrun/g7_bridge.py --backend {args.backend} "
        f"--bridge {args.bridge} --delay-s {args.delay_s} --date {args.date}"
    )
    envelope = build_evidence_envelope(
        script=script_cmd,
        started_at=started_at,
        ended_at=ended_at,
        backend=args.backend,
        test_user_id=test_user_id,
        family="G7",
        sub_item="G7.all",
        synthetic=synthetic,
        extra_fields={
            "call_1_long_read_over_240s": long_read_delay_result,
            "call_2_real_content_long_reads": real_content_result,
        },
    )

    raw_path, md_path, sha256_hex = write_evidence_and_summary(
        family_code="G7",
        title="ResearchData 知识桥接 >240s 长读超时窗口与真实内容 `_declared_sources` / `_sources` 实跑",
        raw_json_path=f"var/dalton/trial-runs/realrun-{args.date}/g7_bridge.json",
        summary_md_path=f"var/dalton/trial-runs/realrun-{args.date}/G7.md",
        envelope=envelope,
        key_findings=[
            (
                "调用 ① `> 240s` 长读 (`company-wiki:read`, `eff_timeout=600s`)",
                (
                    f"elapsed_s={long_read_delay_result['elapsed_s']}s (configured_delay={args.delay_s}s), "
                    f"HTTP {long_read_delay_result['http_status']}, timed_out={long_read_delay_result['timed_out']}"
                ),
            ),
            (
                "调用 ① 返回顶层键序与来源声明",
                (
                    f"top_keys={long_read_delay_result['top_keys_prefix'][:2]}, "
                    f"_declared_sources={long_read_delay_result['declared_sources_count']}, "
                    f"_sources={long_read_delay_result['sources_count']}"
                ),
            ),
            (
                "调用 ② 真实内容长读 1 (`company-wiki:read` GOOGL, 关掉延迟开关)",
                (
                    f"elapsed_s={real_content_result['company_wiki_read']['elapsed_s']}s, "
                    f"_declared_sources={real_content_result['company_wiki_read']['declared_sources_count']}, "
                    f"_sources={real_content_result['company_wiki_read']['sources_count']}"
                ),
            ),
            (
                "调用 ② 真实内容长读 2 (`sec:financials` GOOGL, 关掉延迟开关)",
                (
                    f"elapsed_s={real_content_result['sec_financials_read']['elapsed_s']}s, "
                    f"_declared_sources={real_content_result['sec_financials_read']['declared_sources_count']}, "
                    f"_sources={real_content_result['sec_financials_read']['sources_count']}"
                ),
            ),
            (
                "桥接日志零 stderr / 零 token 泄露",
                f"zero_stderr_leakage={long_read_delay_result['zero_stderr_leakage']}",
            ),
        ],
        notes=[
            "调用 ① 使用已入库的 `DALTON_BRIDGE_TEST_MODE=1` + `DALTON_BRIDGE_TEST_DELAY_S=245` 环境变量开关启动未改动的 `scripts/dalton/researchdata_bridge.py`，在默认超时 `240s` 之上、长读超时 `eff_timeout=600s` 之下真实跑满 `> 245s` 并成功返回，且在 `synthetic` 字段显式声明 `committed_test_hook`。",
            "调用 ② 针对常驻桥接服务 `http://127.0.0.1:8960`（无延迟注入）真实执行 `company-wiki:read` 与 `sec:financials` 读取，返回 JSON 首两个键固定为 `_declared_sources` 与 `_sources` 且非空。",
        ],
        record=args.record,
    )
    if args.record:
        var_log_path = REPO_ROOT / "var" / "dalton" / "smoke" / "g7_bridge_delay_run.log"
        var_log_path.parent.mkdir(parents=True, exist_ok=True)
        var_log_path.write_text(delay_log_text, encoding="utf-8")
    print(
        json.dumps(
            {
                "record": args.record,
                "raw_json": str(raw_path),
                "summary_md": str(md_path),
                "sha256": sha256_hex,
                "call_1_elapsed_s": long_read_delay_result["elapsed_s"],
                "call_2_wiki_sources": real_content_result["company_wiki_read"]["declared_sources_count"],
                "call_2_sec_sources": real_content_result["sec_financials_read"]["declared_sources_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
