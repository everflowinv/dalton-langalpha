#!/usr/bin/env python3
"""podcast-weekly-digest CLI (Dalton host data-task overlay).

YouTube 播客 → 英文逐字稿（Seed-ASR en-US）→ Gemini 看原视频判说话人 → 按句合并
→ 分块中文翻译 + 校对（默认走 Dalton 内部订阅模型接口，失败的块退回 Gemini API）→ 金额量级审计。
每一步落盘，可断点续跑；飞书建档与周报投递由 dalton-podcast-weekly agentTurn 技能接管。
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "2.0.0"
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG = SCRIPT_DIR.parent / "references" / "channels.json"
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")


def resolve_workspace(script_path: Path | str | None = None) -> Path:
    """Resolve Dalton data root via env vars or skill directory ancestry (never ~/.openclaw)."""
    for env_name in ("DALTON_DATA_ROOT", "OPENCLAW_WORKSPACE"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
    here = Path(script_path or __file__).resolve()
    if len(here.parents) >= 4 and here.parents[2].name == "skills":
        return here.parents[3]
    return Path.cwd().resolve()


def workspace_path(ws: Path, *parts: str) -> Path:
    """Resolve a relative path strictly inside ``ws``, rejecting traversal and symlinks."""
    root = ws.resolve()
    if not parts:
        raise SkillError("路径不能为空")
    current = root
    for raw_part in parts:
        sub = Path(str(raw_part))
        if sub.is_absolute() or not sub.parts or any(p in ("", ".", "..") for p in sub.parts):
            raise SkillError(f"非法路径片段（不得逃逸数据目录）: {raw_part}")
        for segment in sub.parts:
            current = current / segment
            if current.is_symlink():
                raise SkillError(f"拒绝符号链接路径: {current}")
    resolved = current.resolve()
    if resolved != root and root not in resolved.parents:
        raise SkillError(f"路径越界（不在数据目录 {root} 内）: {resolved}")
    return current


def validate_video_id(vid: str) -> str:
    cleaned = (vid or "").strip()
    if not _VIDEO_ID_RE.match(cleaned):
        raise SkillError(f"非法 video_id: {vid!r}", "video_id 只能包含字母、数字、_ 或 -")
    return cleaned


def _audit_script_path(ws: Path) -> Path:
    local = SCRIPT_DIR / "audit_numeric_units.py"
    if local.is_file():
        return local
    return ws / "skills" / "transcript-polish" / "scripts" / "audit_numeric_units.py"


def configure_workspace(root: Path | None = None) -> Path:
    """Rebind module workspace paths (used at startup and in isolated unit tests)."""
    global WS, WORK, OUT, LEDGER, AUDIT
    ws = root.resolve() if root is not None else resolve_workspace(__file__)
    WS = ws
    WORK = workspace_path(ws, "temp", "podcast-weekly")
    OUT = workspace_path(ws, "research-outputs", "podcast-weekly")
    LEDGER = workspace_path(ws, "state", "podcast-weekly", "episodes.json")
    AUDIT = _audit_script_path(ws)
    return ws


WS = resolve_workspace(__file__)
WORK = workspace_path(WS, "temp", "podcast-weekly")
OUT = workspace_path(WS, "research-outputs", "podcast-weekly")
LEDGER = workspace_path(WS, "state", "podcast-weekly", "episodes.json")
AUDIT = _audit_script_path(WS)

API_SUBMIT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
API_QUERY = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
RESOURCE_ID = "volc.seedasr.auc"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_FALLBACK_TEXT_MODEL = "gemini-4-argon"
DALTON_PARALLEL = 3
WINDOW_SEC = 720          # 12 分钟一段：实测一次视频判人约 68 秒、6.5 万 token
ASR_POLL_MAX = 3600
PARA_WORDS = 200
_LOCK = threading.Lock()
_DALTON_SLOTS = threading.BoundedSemaphore(DALTON_PARALLEL)


class SkillError(Exception):
    def __init__(self, msg: str, hint: str = ""):
        super().__init__(msg)
        self.hint = hint


# ---------- 基础 ----------

def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def emit(payload: dict, as_json: bool):
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def read_json(path: Path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1))
    tmp.replace(path)


def _asr_env() -> dict[str, str]:
    required = ("SEED_ASR_APP_ID", "SEED_ASR_ACCESS_KEY", "VOLC_TOS_AK", "VOLC_TOS_SK", "VOLC_TOS_BUCKET")
    env = {k: os.environ.get(k, "").strip() for k in required}
    env["VOLC_TOS_ENDPOINT"] = os.environ.get("VOLC_TOS_ENDPOINT", "").strip() or "tos-cn-beijing.volces.com"
    env["VOLC_TOS_REGION"] = os.environ.get("VOLC_TOS_REGION", "").strip() or "cn-beijing"
    missing = [k for k in required if not env.get(k)]
    if missing:
        raise SkillError(f"缺少环境变量 {missing}", "在 Dalton 数据任务 secret_names 或运行环境中配置 Seed-ASR / TOS 凭据")
    return env


def gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    raise SkillError("GEMINI_API_KEY 未找到", "在运行环境中设置 GEMINI_API_KEY")


def channels() -> dict:
    return read_json(CONFIG)


def ledger() -> dict:
    return read_json(LEDGER, {}) or {}


def ledger_update(video_id: str, **fields):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    lock_file = LEDGER.with_suffix(".lock")
    if lock_file.is_symlink():
        raise SkillError(f"拒绝软链接锁文件: {lock_file}")
    with _LOCK, open(lock_file, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # 手动补跑可能和周任务同时写台账
        try:
            data = ledger()
            entry = data.setdefault(video_id, {})
            entry.update(fields)
            entry["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            write_json(LEDGER, data)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def mmss(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def parse_ts(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    parts = [float(p) for p in str(value).strip().split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


# ---------- 列节目 ----------

def list_channel(handle: str, limit: int) -> list[dict]:
    cmd = ["yt-dlp", "--flat-playlist", "--playlist-end", str(limit),
           "--extractor-args", "youtubetab:approximate_date",
           "--print", "%(id)s\t%(upload_date)s\t%(duration)s\t%(title)s",
           f"https://www.youtube.com/@{handle}/videos"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if res.returncode != 0 and not res.stdout.strip():
        raise SkillError(f"yt-dlp 列 @{handle} 失败: {res.stderr.strip()[-300:]}",
                         "先 `yt-dlp -U` 或 `brew upgrade yt-dlp`，YouTube 改版后旧版常失效")
    rows = []
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        vid, date, dur, title = parts
        rows.append({"video_id": vid, "upload_date": date if date != "NA" else None,
                     "duration": int(float(dur)) if dur not in ("NA", "") else None, "title": title})
    return rows


def discover(since_days: int, only_channel: str | None = None) -> list[dict]:
    cfg = channels()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y%m%d")
    seen = ledger()
    episodes = []
    for ch in cfg["channels"]:
        if only_channel and ch["handle"] != only_channel:
            continue
        for row in list_channel(ch["handle"], ch.get("scan_limit", 25)):
            if not row["upload_date"] or row["upload_date"] < cutoff:
                continue
            if (row["duration"] or 0) < cfg.get("min_duration_sec", 300):
                continue  # 预告片、Shorts、基金宣传片
            row.update(channel=ch["name"], handle=ch["handle"], hosts=ch.get("hosts", []),
                       published=bool(seen.get(row["video_id"], {}).get("doc_url")))
            episodes.append(row)
    episodes.sort(key=lambda r: (r["upload_date"], r["video_id"]))
    return episodes


# ---------- 取音频 ----------

def stage_fetch(vid: str, wd: Path) -> dict:
    meta_path, audio = wd / "meta.json", wd / "audio.mp3"
    if meta_path.is_file() and (audio.is_file() or (wd / "asr_raw.json").is_file()):
        return read_json(meta_path)
    wd.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={vid}"
    res = subprocess.run(["yt-dlp", "-f", "bestaudio[ext=m4a]/bestaudio", "--no-playlist",
                          "--print-json", "--no-simulate", "-o", str(wd / "src.%(ext)s"), url],
                         capture_output=True, text=True, timeout=1800)
    if res.returncode != 0:
        raise SkillError(f"{vid} 音频下载失败: {res.stderr.strip()[-300:]}", "重跑一次；持续失败先升级 yt-dlp")
    info = json.loads(res.stdout.strip().splitlines()[-1])
    meta = {"video_id": vid, "title": info.get("title"), "channel": info.get("channel"),
            "upload_date": info.get("upload_date"), "duration": info.get("duration"),
            "description": (info.get("description") or "")[:3000], "url": url}
    src = next(wd.glob("src.*"))
    # TOS 上行慢（17MB 实测 185 秒），先压成 16kHz 单声道 24kbps
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "16000",
                    "-b:a", "24k", str(audio)], check=True, timeout=1800)
    src.unlink()
    write_json(meta_path, meta)
    return meta


# ---------- 异常清洗 & Seed-ASR ----------

_SENSITIVE_ENV_TOKENS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "ACCESS", "AUTH", "CREDENTIAL", "AK", "SK")
_URL_QUERY_RE = re.compile(r"(https?://[^\s\"'<>?]+)\?[^\s\"'<>]+")


def _safe_stage_error(operation: str, exc: BaseException, extra_secrets: tuple[str, ...] = ()) -> str:
    """Format an exception safely for persistence (`stage_error`), never leaking cmdlines or secrets."""
    if isinstance(exc, subprocess.TimeoutExpired):
        detail = f"timed out after {exc.timeout}s"
    elif isinstance(exc, subprocess.CalledProcessError):
        detail = f"exit status {exc.returncode}"
    else:
        detail = f"{exc}"

    detail = _URL_QUERY_RE.sub(r"\1?[REDACTED]", detail)
    secret_vals: set[str] = {str(s) for s in extra_secrets if s}
    for k, v in os.environ.items():
        if v and any(tok in k.upper() for tok in _SENSITIVE_ENV_TOKENS):
            secret_vals.add(v)
    for sec in sorted(secret_vals, key=len, reverse=True):
        if len(sec) >= 4 and sec in detail:
            detail = detail.replace(sec, "***")
    detail = " ".join(detail.split())[:220]
    msg = f"{operation}: {type(exc).__name__}"
    if detail:
        msg = f"{msg}: {detail}"
    return msg[:300]


def _asr_headers(env: dict, task_id: str) -> dict[str, str]:
    return {
        "X-Api-App-Key": env["SEED_ASR_APP_ID"],
        "X-Api-Access-Key": env["SEED_ASR_ACCESS_KEY"],
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-Request-Id": task_id,
        "X-Api-Sequence": "-1",
        "Content-Type": "application/json",
    }


def _asr_call(url: str, env: dict, task_id: str, body: dict) -> tuple[str, str, str]:
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers=_asr_headers(env, task_id),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            code = (resp.headers.get("X-Api-Status-Code") or "").strip()
            msg = (resp.headers.get("X-Api-Message") or "").strip()
            text = resp.read().decode("utf-8", errors="replace")
            return code, msg, text
    except urllib.error.HTTPError as exc:
        code = ((exc.headers.get("X-Api-Status-Code") if exc.headers else None) or str(exc.code)).strip()
        msg = ((exc.headers.get("X-Api-Message") if exc.headers else None) or str(exc.reason or "")).strip()
        try:
            body_bytes = exc.read()
            text = body_bytes.decode("utf-8", errors="replace") if isinstance(body_bytes, (bytes, bytearray)) else ""
        except Exception:
            text = ""
        return code, msg, text


def tos_client(env: dict):
    import tos
    return tos.TosClientV2(env["VOLC_TOS_AK"], env["VOLC_TOS_SK"],
                           env.get("VOLC_TOS_ENDPOINT") or "tos-cn-beijing.volces.com",
                           env.get("VOLC_TOS_REGION") or "cn-beijing")


def stage_asr_submit(vid: str, wd: Path, meta: dict) -> dict:
    task_path = wd / "asr_task.json"
    if task_path.is_file() or (wd / "asr_raw.json").is_file():
        return read_json(task_path, {})
    from tos.enum import HttpMethodType
    env = _asr_env()
    client = tos_client(env)
    audio = wd / "audio.mp3"
    key = f"asr-temp/podcast-{vid}-{uuid.uuid4().hex[:8]}.mp3"
    log(f"{vid} 上传 TOS {audio.stat().st_size / 1e6:.1f}MB")
    if audio.stat().st_size > 20 * 1024 * 1024:
        client.upload_file(env["VOLC_TOS_BUCKET"], key, str(audio), part_size=10 * 1024 * 1024,
                           task_num=3, enable_checkpoint=True)
    else:
        client.put_object_from_file(env["VOLC_TOS_BUCKET"], key, str(audio))
    url = client.pre_signed_url(http_method=HttpMethodType.Http_Method_Get, bucket=env["VOLC_TOS_BUCKET"],
                                key=key, expires=14400).signed_url
    hot = [{"word": w} for w in (meta.get("hotwords") or [])][:100]
    body = {"user": {"uid": "dalton-podcast-weekly"},
            # language 必须显式给：缺省时整段英文会被吐成中文，还带出 10 倍的数字错误
            "audio": {"format": "mp3", "url": url, "language": "en-US"},
            "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True,
                        "enable_ddc": True, "enable_speaker_info": True, "ssd_version": "300",
                        "show_utterances": True}}
    if hot:
        body["request"]["corpus"] = {"context": json.dumps({"hotwords": hot})}
    task_id = str(uuid.uuid4())
    code, msg, _ = _asr_call(API_SUBMIT, env, task_id, body)
    if code != "20000000":
        raise SkillError(f"{vid} ASR 提交失败 status={code} {msg}", "核对 SEED_ASR_* 凭据与额度")
    task = {"task_id": task_id, "tos_key": key, "submitted_at": time.time()}
    write_json(task_path, task)
    return task


def stage_asr_fetch(vid: str, wd: Path, wait: bool = True) -> bool:
    raw_path = wd / "asr_raw.json"
    if raw_path.is_file():
        return True
    task = read_json(wd / "asr_task.json")
    if not task:
        raise SkillError(f"{vid} 没有 ASR 任务记录", "先跑 asr-submit")
    env = _asr_env()
    start = time.time()
    while True:
        code, msg, body = _asr_call(API_QUERY, env, task["task_id"], {})
        if code == "20000000":
            data = json.loads(body)
            for utt in data.get("result", {}).get("utterances", []):
                utt.pop("words", None)  # 逐词时间戳占 9 成体积，合并只用句级
            write_json(raw_path, data)
            try:
                tos_client(env).delete_object(env["VOLC_TOS_BUCKET"], task["tos_key"])
            except Exception as exc:  # noqa: BLE001
                log(f"{vid} TOS 清理失败（不影响结果）: {exc}")
            (wd / "audio.mp3").unlink(missing_ok=True)
            return True
        if code not in ("20000001", "20000002"):
            raise SkillError(f"{vid} ASR 查询失败 status={code} {msg}",
                             "删掉该期目录下的 asr_task.json 后重跑，会重新上传提交")
        if not wait:
            return False
        if time.time() - start > ASR_POLL_MAX:
            raise SkillError(f"{vid} ASR 超过 {ASR_POLL_MAX}s 未完成", "服务端任务仍在，稍后重跑 pipeline 会用同一个 task_id 续取")
        log(f"{vid} ASR 处理中 {int(time.time() - start)}s")
        time.sleep(20)


def utterances(wd: Path) -> list[dict]:
    raw = read_json(wd / "asr_raw.json")
    out = []
    for utt in raw["result"]["utterances"]:
        text = (utt.get("text") or "").strip()
        if not text:
            continue
        add = utt.get("additions") or {}
        if isinstance(add, str):
            try:
                add = json.loads(add)
            except ValueError:
                add = {}
        out.append({"i": len(out), "start": utt["start_time"] / 1000, "end": utt["end_time"] / 1000,
                    "asr_speaker": add.get("speaker"), "text": text})
    return out


# ---------- Gemini ----------

def gemini(model: str, parts: list, schema: dict | None = None, timeout: int = 600, retries: int = 3,
           thinking: str | None = None) -> tuple:
    cfg = {"temperature": 0.1, "responseMimeType": "application/json"}
    if thinking:
        cfg["thinkingConfig"] = {"thinkingLevel": thinking}
    if schema:
        cfg["responseSchema"] = schema
    payload = json.dumps({"contents": [{"role": "user", "parts": parts}], "generationConfig": cfg}).encode()
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(GEMINI_URL.format(model=model), data=payload,
                                     headers={"Content-Type": "application/json", "x-goog-api-key": gemini_key()})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            cand = data["candidates"][0]
            text = "".join(p.get("text", "") for p in cand["content"]["parts"] if not p.get("thought"))
            return json.loads(text), data.get("usageMetadata", {})
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:400]
            last = SkillError(f"Gemini {err.code}: {detail}",
                              "429/503 稍后重跑即可续上；400 多半是视频不可访问（私享、会员专享或地区限制）")
            if err.code in (400, 401, 403, 404):
                break
            log(f"Gemini HTTP {err.code}，第 {attempt + 1} 次，重试")
            retry_after = err.headers.get("Retry-After")
            time.sleep(int(retry_after) if retry_after and retry_after.isdigit() else 30 * (attempt + 1))
        except (KeyError, ValueError, TimeoutError, OSError) as exc:
            last = SkillError(f"Gemini 返回无法解析: {exc}", "重跑；持续失败把 WINDOW_SEC 调小")
            log(f"Gemini 第 {attempt + 1} 次失败，重试：{str(exc)[:120]}")
            time.sleep(15 * (attempt + 1))
    raise last


def _dalton_backend_config() -> tuple[str, str]:
    base_url = os.environ.get("DALTON_BACKEND_URL", "").strip().rstrip("/")
    token = os.environ.get("INTERNAL_SERVICE_TOKEN", "").strip()
    missing = [k for k, v in (("DALTON_BACKEND_URL", base_url), ("INTERNAL_SERVICE_TOKEN", token)) if not v]
    if missing:
        raise SkillError(f"缺少 Dalton 后端配置 {missing}", "检查 DALTON_BACKEND_URL 与 INTERNAL_SERVICE_TOKEN 环境变量")
    return base_url, token


def dalton_llm_complete(
    model: str,
    system_prompt: str,
    user_prompt: str,
    schema: dict,
    purpose: str = "podcast_weekly_translate",
    timeout_s: float = 180.0,
    retries: int = 2,
) -> tuple[dict, dict]:
    """Call Dalton's internal subscription LLM endpoint with automatic block retry/fallback errors."""
    base_url, token = _dalton_backend_config()
    url = f"{base_url}/api/v1/dalton/internal/llm/complete"
    payload = json.dumps(
        {
            "model": model,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response_schema": schema,
            "purpose": purpose,
            "timeout_s": timeout_s,
        },
        ensure_ascii=False,
    ).encode()
    last: SkillError | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Service-Token": token,
            },
        )
        try:
            with _DALTON_SLOTS, urllib.request.urlopen(req, timeout=timeout_s + 15) as resp:
                data = json.loads(resp.read())
            if not isinstance(data, dict) or not data.get("ok"):
                status = data.get("status", "failed") if isinstance(data, dict) else "invalid_response"
                detail = data.get("error", "") if isinstance(data, dict) else str(data)
                last = SkillError(
                    f"Dalton 订阅接口失败 status={status}: {detail[:240]}",
                    "该块改走 Gemini API",
                )
                if status in ("budget_exhausted", "indeterminate", "conflict"):
                    break
                time.sleep(5)
                continue
            text = data.get("text")
            if not isinstance(text, str) or not text.strip():
                raise SkillError("Dalton 订阅接口返回空 text", "该块改走 Gemini API")
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise SkillError("Dalton 订阅接口返回非 JSON object", "该块改走 Gemini API")
            return parsed, {"status": data.get("status"), "model": data.get("model")}
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:300]
            last = SkillError(f"Dalton 订阅接口 HTTP {err.code}: {detail}", "该块改走 Gemini API")
            if err.code in (400, 401, 403, 413, 422, 429):
                break
            log(f"Dalton 订阅接口 HTTP {err.code}，第 {attempt + 1} 次，重试")
            time.sleep(10)
        except urllib.error.URLError as exc:
            last = SkillError(f"Dalton 订阅接口连不上: {exc.reason}", "该块改走 Gemini API")
            break
        except (KeyError, IndexError, ValueError, TimeoutError, OSError) as exc:
            last = SkillError(f"Dalton 订阅接口返回无法解析或超时: {exc}", "该块改走 Gemini API")
            log(f"Dalton 订阅接口第 {attempt + 1} 次失败，重试：{str(exc)[:120]}")
            time.sleep(5)
    raise last or SkillError("Dalton 订阅接口失败", "该块改走 Gemini API")


TURN_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "start_u": {"type": "INTEGER"}, "speaker": {"type": "STRING"}, "evidence": {"type": "STRING"}},
    "required": ["start_u", "speaker"]}}


def stage_diarize(vid: str, wd: Path, meta: dict, model: str) -> list[dict]:
    """逐窗口让 Gemini 看原视频，给每个换人点标出从第几句 ASR 开始、是谁。"""
    utts = utterances(wd)
    turns_path = wd / "turns.json"
    state = read_json(turns_path, {"windows": {}, "roster": list(meta.get("hosts") or [])})
    duration = utts[-1]["end"] if utts else 0
    n_windows = int(duration // WINDOW_SEC) + 1
    def run_window(w: int):
        lo, hi = w * WINDOW_SEC, min((w + 1) * WINDOW_SEC, duration + 1)
        seg = [u for u in utts if lo <= u["start"] < hi]
        if not seg:
            return w, [], None
        with _LOCK:
            roster = list(state["roster"])
        listing = "\n".join(f"{u['i']}|{mmss(u['start'])}|{u['text'][:110]}" for u in seg)
        prompt = (
            f"Video: \"{meta['title']}\" ({meta.get('channel')}).\n"
            f"Description (may name guests):\n{meta.get('description', '')[:1200]}\n\n"
            f"Speakers identified so far: {', '.join(roster) or 'none yet'}.\n\n"
            f"Below are ASR sentences for {mmss(lo)}–{mmss(hi)} as index|video time|text. "
            "Watch and listen to this part of the video and decide who speaks each sentence. "
            "Identify people by who is on screen while talking, on-screen name captions, voice, and "
            "being addressed by name. Use full real names; reuse the exact spelling from the list above. "
            "If a person cannot be named, use 'Unknown 1', 'Unknown 2' consistently. "
            "Played clips or ads: use 'Clip' / 'Ad'.\n"
            "Return one item per speaker change: start_u = index of the first sentence of that turn, "
            "speaker, evidence (≤8 words on how you identified them). The first item must start at "
            f"index {seg[0]['i']}. Do not skip short interjections.\n\n{listing}")
        parts = [{"fileData": {"fileUri": meta["url"]},
                  "videoMetadata": {"startOffset": f"{int(lo)}s", "endOffset": f"{int(hi)}s"}},
                 {"text": prompt}]
        log(f"{vid} 视频判人 窗口 {w + 1}/{n_windows}")
        result, usage = gemini(model, parts, TURN_SCHEMA)
        valid = {u["i"] for u in seg}
        turns = sorted((t for t in result if t.get("start_u") in valid), key=lambda t: t["start_u"])
        if not turns:
            raise SkillError(f"{vid} 窗口 {w} 没有返回有效换人点", "重跑 pipeline；该窗口未写盘，会自动重试")
        if turns[0]["start_u"] != seg[0]["i"]:
            turns.insert(0, {"start_u": seg[0]["i"], "speaker": turns[0]["speaker"], "evidence": "窗口起点补齐"})
        return w, turns, usage.get("totalTokenCount")

    def save(w, turns, tokens):
        with _LOCK:
            for t in turns:
                if t["speaker"] not in state["roster"] and t["speaker"] not in ("Clip", "Ad"):
                    state["roster"].append(t["speaker"])
            state["windows"][str(w)] = turns
            if tokens:
                state.setdefault("usage", []).append(tokens)
            write_json(turns_path, state)

    todo = [w for w in range(n_windows) if str(w) not in state["windows"]]
    # 第一个窗口先单独跑：嘉宾通常在开场出现，名单定下来后其余窗口并行，名字拼写才一致
    if todo and todo[0] == 0:
        save(*run_window(todo.pop(0)))
    with cf.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run_window, w) for w in todo]
        errors = []
        for f in cf.as_completed(futures):
            if f.exception():
                errors.append(f.exception())
            else:
                save(*f.result())
    if errors:
        raise errors[0]
    return state


def stage_merge(vid: str, wd: Path, meta: dict) -> dict:
    utts = utterances(wd)
    state = read_json(wd / "turns.json")
    speaker_at = {}
    for turns in state["windows"].values():
        for t in turns:
            speaker_at[t["start_u"]] = t["speaker"]
    blocks, current = [], None
    for u in utts:
        if u["i"] in speaker_at:
            current = speaker_at[u["i"]]
        # 长独白按约 200 词切自然段：整段 3000 词塞进一个 JSON 字段，翻译会漏句
        if blocks and blocks[-1]["speaker"] == current and len(blocks[-1]["text"].split()) < PARA_WORDS:
            blocks[-1]["text"] += " " + u["text"]
            blocks[-1]["asr_speakers"].add(u["asr_speaker"])
        else:
            blocks.append({"speaker": current, "start": u["start"], "text": u["text"],
                           "asr_speakers": {u["asr_speaker"]}})
    for b in blocks:
        b["asr_speakers"] = sorted(x for x in b["asr_speakers"] if x is not None)
    words = {}
    for b in blocks:
        words[b["speaker"]] = words.get(b["speaker"], 0) + len(b["text"].split())
    merged = {"video_id": vid, "blocks": blocks, "words_by_speaker": words}
    write_json(wd / "merged.json", merged)
    lines = [f"# {meta['title']}", "", f"{meta['url']}", ""]
    for b in blocks:
        lines += [f"**{b['speaker']}** [{mmss(b['start'])}]", b["text"], ""]
    (wd / "merged.md").write_text("\n".join(lines))
    return merged


ZH_SCHEMA = {"type": "OBJECT", "properties": {
    "blocks": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "n": {"type": "INTEGER"}, "speaker": {"type": "STRING"}, "en": {"type": "STRING"},
        "zh": {"type": "STRING"}}, "required": ["n", "speaker", "en", "zh"]}},
    "corrections": {"type": "ARRAY", "items": {"type": "STRING"}},
    "unclear": {"type": "ARRAY", "items": {"type": "STRING"}}}, "required": ["blocks"]}


def _gemini_model_for_fallback(model: str, fallback_model: str = GEMINI_FALLBACK_TEXT_MODEL) -> str:
    cleaned = (model or "").strip()
    if not cleaned or cleaned.endswith("-oauth") or "hydrogen" in cleaned:
        return fallback_model
    return cleaned


def stage_translate(vid: str, wd: Path, meta: dict, model: str, backend: str = "dalton",
                    chunk_words: int = 1200, fallback_model: str = GEMINI_FALLBACK_TEXT_MODEL) -> list[dict]:
    merged = read_json(wd / "merged.json")
    roster = read_json(wd / "turns.json")["roster"]
    rules = (CONFIG.parent / "translation-rules.md").read_text()
    chunks, cur, count = [], [], 0
    for n, b in enumerate(merged["blocks"]):
        cur.append((n, b))
        count += len(b["text"].split())
        if count >= chunk_words:
            chunks.append(cur)
            cur, count = [], 0
    if cur:
        chunks.append(cur)
    zdir = wd / "zh"

    def run_chunk(item):
        ci, chunk = item
        path = zdir / f"{ci:03d}.json"
        if path.is_file():
            return read_json(path)
        src = "\n\n".join(f"#{n} | {b['speaker']} | {mmss(b['start'])}\n{b['text']}" for n, b in chunk)
        user_body = (
            f"节目：{meta['title']}（{meta.get('channel')}）\n"
            f"视频判定的说话人名单：{', '.join(roster)}\n"
            f"节目简介（人名、公司名拼写以此为准）：\n{meta.get('description', '')[:1200]}\n\n"
            f"待处理段落（#编号 | 视频判定说话人 | 时间）：\n\n{src}"
        )
        prompt = f"{rules}\n\n{user_body}"
        want = {n for n, _ in chunk}

        def checked(result):
            got = {b["n"] for b in result.get("blocks", [])}
            if got != want:
                raise SkillError(f"{vid} 块 {ci} 段落编号对不上：缺 {sorted(want - got)} 多 {sorted(got - want)}",
                                 "重跑 pipeline；该块未写盘，会自动重试")
            return result

        log(f"{vid} 翻译校对 块 {ci + 1}/{len(chunks)}（{backend}）")
        used, actual_model, fallback_reason = backend, model, None
        started = time.time()
        result = None
        if backend == "dalton":
            try:
                result, usage = dalton_llm_complete(
                    model=model,
                    system_prompt=rules,
                    user_prompt=user_body,
                    schema=ZH_SCHEMA,
                    purpose="podcast_weekly_translate",
                    timeout_s=180.0,
                )
                result = checked(result)
            except SkillError as exc:
                fallback_reason = str(exc)[:200]
                log(f"{vid} 块 {ci + 1} Dalton 订阅接口失败，改走 Gemini API：{fallback_reason[:160]}")
                result, used = None, "gemini-api"
        if result is None:
            actual_model = _gemini_model_for_fallback(model, fallback_model)
            # 默认思考档一块要 4 分钟以上，长连接常被对端掐断
            result, usage = gemini(actual_model, [{"text": prompt}], ZH_SCHEMA, timeout=600, thinking="low")
            result = checked(result)
        result["backend"] = {
            "route": used,
            "model": actual_model,
            "fallback_from": model if used != backend else None,
            "reason": fallback_reason,
            "seconds": round(time.time() - started),
            "usage": usage,
        }
        by_n = {}
        for blk in result["blocks"]:  # 模型偶尔把一段拆成两个同号 block，拼回去
            if blk["n"] in by_n:
                by_n[blk["n"]]["en"] += " " + blk["en"]
                by_n[blk["n"]]["zh"] += blk["zh"]
            else:
                by_n[blk["n"]] = blk
        for n, blk in by_n.items():
            blk["start"] = merged["blocks"][n]["start"]
            blk["speaker"] = merged["blocks"][n]["speaker"]  # 说话人只认视频判定
        result["blocks"] = [by_n[n] for n in sorted(by_n)]
        write_json(path, result)
        return result

    # 块之间互不依赖，并行跑；单块失败时其余块照常落盘，重跑只补缺的
    with cf.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(run_chunk, item) for item in enumerate(chunks)]
        errors = [f.exception() for f in futures if f.exception()]
    if errors:
        raise errors[0]
    return [f.result() for f in futures]


def stage_assemble(vid: str, wd: Path, meta: dict) -> dict:
    validate_video_id(vid)
    chunks = [read_json(p) for p in sorted((wd / "zh").glob("*.json"))]
    blocks = [b for c in chunks for b in c["blocks"]]
    corrections = [x for c in chunks for x in c.get("corrections", [])]
    unclear = [x for c in chunks for x in c.get("unclear", [])]
    # 漏句护栏：校订稿词数明显少于逐字稿的段落，交给 agent 对照原文
    source = read_json(wd / "merged.json")["blocks"]
    kept = {}
    for b in blocks:
        kept[b["n"]] = kept.get(b["n"], 0) + len(b["en"].split())
    short = [{"time": mmss(source[n]["start"]), "speaker": source[n]["speaker"], "source_words": len(source[n]["text"].split()),
              "kept_words": k, "source_head": source[n]["text"][:80]}
             for n, k in kept.items() if len(source[n]["text"].split()) >= 40 and k < 0.7 * len(source[n]["text"].split())]

    def render(field: str) -> str:
        out, prev = [], None
        for b in blocks:
            if b["speaker"] == prev:
                out += [b[field], ""]
            else:
                out += [f"**{b['speaker']}** [{mmss(b['start'])}]", b[field], ""]
            prev = b["speaker"]
        return "\n".join(out)

    OUT.mkdir(parents=True, exist_ok=True)
    stem = f"{meta['upload_date']}-{vid}"
    en_path = workspace_path(OUT, f"{stem}.en.md")
    zh_path = workspace_path(OUT, f"{stem}.zh.md")
    merged_out_path = workspace_path(OUT, f"{stem}.merged.md")
    notes_path = workspace_path(OUT, f"{stem}.notes.json")
    en_path.write_text(render("en"))
    zh_path.write_text(render("zh"))
    if (wd / "merged.md").is_file():
        merged_out_path.write_text((wd / "merged.md").read_text())
    else:
        merged_lines = [f"# {meta['title']}", "", f"{meta['url']}", ""]
        for b in blocks:
            merged_lines += [f"**{b['speaker']}** [{mmss(b['start'])}]", b.get("en", ""), ""]
        merged_out_path.write_text("\n".join(merged_lines))
    audit = {"ok": False, "warnings": [], "error": "未运行"}
    if AUDIT.is_file():
        res = subprocess.run([sys.executable, str(AUDIT), str(en_path), str(zh_path), "--json"],
                             capture_output=True, text=True, timeout=300)
        try:
            audit = json.loads(res.stdout)
        except ValueError:
            audit["error"] = res.stderr.strip()[-300:]
    else:
        audit["error"] = f"缺少审计脚本: {AUDIT}"
    notes = {"video_id": vid, "title": meta["title"], "channel": meta.get("channel"), "url": meta["url"],
             "upload_date": meta["upload_date"], "duration_min": round((meta.get("duration") or 0) / 60),
             "zh_path": str(zh_path), "en_path": str(en_path), "merged_path": str(merged_out_path),
             "zh_chars": len(zh_path.read_text()), "en_words": len(en_path.read_text().split()),
             "speaker_flags": [c for c in corrections if "说话人" in c],
             "short_blocks": short, "corrections": [c for c in corrections if "说话人" not in c], "unclear": unclear,
             "amount_warnings": audit.get("warnings", []), "audit_error": audit.get("error")}
    routes = [c.get("backend", {}).get("route", "gemini-api") for c in chunks]
    notes["translation_routes"] = {r: routes.count(r) for r in sorted(set(routes))}
    fallbacks = [
        {
            "chunk": idx,
            "route": c.get("backend", {}).get("route"),
            "model": c.get("backend", {}).get("model"),
            "fallback_from": c.get("backend", {}).get("fallback_from"),
            "reason": c.get("backend", {}).get("reason"),
        }
        for idx, c in enumerate(chunks)
        if c.get("backend", {}).get("fallback_from") or c.get("backend", {}).get("reason")
    ]
    if fallbacks:
        notes["translation_route_fallbacks"] = fallbacks
    write_json(notes_path, notes)
    return notes


# ---------- 编排（仅产出稿件与台账；发飞书与投递由 dalton-podcast-weekly 技能完成） ----------

def process_episode(ep: dict, args) -> dict:
    vid = validate_video_id(ep["video_id"])
    wd = workspace_path(WORK, vid)
    current_op = "fetch"
    try:
        current_op = "fetch"
        meta = stage_fetch(vid, wd)
        meta.setdefault("hosts", ep.get("hosts") or [])
        meta["hotwords"] = (ep.get("hosts") or []) + channels().get("hotwords", [])
        if not meta.get("hosts"):
            meta["hosts"] = ep.get("hosts") or []
        ledger_update(vid, title=meta["title"], channel=meta.get("channel"), upload_date=meta["upload_date"],
                      stage="fetched")
        current_op = "asr_submit"
        stage_asr_submit(vid, wd, meta)
        ledger_update(vid, stage="asr_submitted")
        current_op = "asr_fetch"
        stage_asr_fetch(vid, wd)
        ledger_update(vid, stage="asr_done")
        current_op = "diarize"
        stage_diarize(vid, wd, meta, args.video_model)
        current_op = "merge"
        stage_merge(vid, wd, meta)
        ledger_update(vid, stage="merged")
        current_op = "translate"
        stage_translate(vid, wd, meta, args.text_model, args.text_backend)
        current_op = "assemble"
        notes = stage_assemble(vid, wd, meta)
        ledger_update(vid, stage="ready", notes_path=str(OUT / f"{meta['upload_date']}-{vid}.notes.json"))
        log(f"{vid} 完成：{notes['en_words']} 词 → {notes['zh_chars']} 字")
        return {"video_id": vid, "ok": True, "title": meta["title"], "notes": notes}
    except Exception as exc:  # noqa: BLE001 - 单期失败不拖垮整批
        hint = getattr(exc, "hint", "") or "看该期目录下已有的中间文件，修好后重跑 pipeline 会从断点续上"
        safe_err = _safe_stage_error(current_op, exc)
        ledger_update(vid, stage_error=safe_err)
        log(f"{vid} 失败：{safe_err}")
        return {"video_id": vid, "ok": False, "title": ep.get("title"), "error": safe_err, "hint": hint}


def cmd_list(args):
    eps = discover(args.since_days, args.channel)
    emit({"ok": True, "count": len(eps), "total_minutes": round(sum(e["duration"] or 0 for e in eps) / 60),
          "episodes": eps}, args.json)


def cmd_pipeline(args):
    if args.video:
        host_map = {c["handle"]: c for c in channels()["channels"]}
        eps = [{"video_id": v, "hosts": host_map.get(args.channel or "", {}).get("hosts", [])} for v in args.video]
    else:
        eps = [e for e in discover(args.since_days, args.channel) if not e["published"] or args.redo]
    if args.max_episodes:
        eps = eps[: args.max_episodes]
    log(f"待处理 {len(eps)} 期")
    results = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for res in pool.map(lambda e: process_episode(e, args), eps):
            results.append(res)
    failed = [r for r in results if not r["ok"]]
    emit({"ok": not failed, "processed": len(results), "failed": len(failed), "results": results,
          "hint": "失败的期次修好后重跑同一条命令即可续跑" if failed else None}, args.json)
    if failed:
        sys.exit(2)


def cmd_status(args):
    data = ledger()
    rows = [{"video_id": k, **v} for k, v in data.items()]
    if args.pending:
        rows = [r for r in rows if r.get("stage") == "ready" and not r.get("doc_url")]
    rows.sort(key=lambda r: r.get("upload_date") or "")
    emit({"ok": True, "count": len(rows), "episodes": rows}, args.json)


def cmd_mark_published(args):
    if args.video not in ledger():
        raise SkillError(f"台账里没有 {args.video}", "先用 status 确认 video_id")
    ledger_update(args.video, doc_url=args.doc_url, published_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    emit({"ok": True, "video_id": args.video, "doc_url": args.doc_url}, args.json)


def cmd_self_test(args):
    checks = []

    def check(name, fn):
        try:
            detail = fn()
            checks.append({"name": name, "ok": True, "detail": detail})
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": name, "ok": False, "error": str(exc)[:300],
                           "hint": getattr(exc, "hint", "") or "按报错修复后重跑 self-test"})

    def need(binary):
        path = shutil.which(binary)
        if not path:
            raise SkillError(f"找不到 {binary}", f"brew install {binary}")
        return path

    def asr_env():
        env = _asr_env()
        return f"5 项环境变量齐全 ({env['VOLC_TOS_BUCKET']})"

    def path_guard():
        for bad in ("../escape", "/abs/escape"):
            try:
                workspace_path(WS, bad)
            except SkillError:
                continue
            raise SkillError(f"路径逃逸未被拦截: {bad}")
        return f"路径限定在 {WS}"

    def merge_fixture():
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            write_json(wd / "asr_raw.json", {"result": {"utterances": [
                {"start_time": 0, "end_time": 900, "text": "Welcome back.", "additions": {"speaker": "1"}},
                {"start_time": 1000, "end_time": 1900, "text": "Thanks.", "additions": {"speaker": "1"}},
                {"start_time": 2000, "end_time": 2900, "text": "Glad to be here.", "additions": {"speaker": "1"}}]}})
            write_json(wd / "turns.json", {"roster": ["A", "B"], "windows": {"0": [
                {"start_u": 0, "speaker": "A"}, {"start_u": 1, "speaker": "B"}]}})
            merged = stage_merge("fixture", wd, {"title": "t", "url": "u"})
            got = [(b["speaker"], b["text"]) for b in merged["blocks"]]
            assert got == [("A", "Welcome back."), ("B", "Thanks. Glad to be here.")], got
        return "同一 ASR 编号下的两个人能按视频判定拆开"

    check("yt-dlp", lambda: need("yt-dlp"))
    check("ffmpeg", lambda: need("ffmpeg"))
    check("channels.json", lambda: f"{len(channels()['channels'])} 个频道")
    check("translation-rules.md", lambda: f"{len((CONFIG.parent / 'translation-rules.md').read_text())} 字符")
    check("asr-credentials", asr_env)
    check("gemini-key", lambda: "已找到 (GEMINI_API_KEY)" if gemini_key() else "")
    check("dalton-backend-config", lambda: f"{_dalton_backend_config()[0]}")
    check("workspace-path-guard", path_guard)
    check("amount-audit-script", lambda: str(AUDIT) if AUDIT.is_file() else (_ for _ in ()).throw(
        SkillError(f"缺 {AUDIT}", "确认 audit_numeric_units.py 在技能目录或工作区中")))
    check("merge-logic", merge_fixture)
    if args.network:
        check("tos-bucket", lambda: tos_client(_asr_env()).head_bucket(
            _asr_env()["VOLC_TOS_BUCKET"]) and "可访问")
        check("youtube-list", lambda: f"{len(list_channel(channels()['channels'][0]['handle'], 3))} 条")
    ok = all(c["ok"] for c in checks)
    emit({"ok": ok, "version": VERSION, "workspace": str(WS), "checks": checks}, args.json)
    if not ok:
        sys.exit(1)


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="播客周报：分说话人逐字稿 + 中文翻译校对")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("self-test")
    p.add_argument("--network", action="store_true", help="加测 TOS 和 YouTube 连通")
    p.set_defaults(func=cmd_self_test)

    p = sub.add_parser("list", help="列窗口内的新节目")
    p.add_argument("--since-days", type=int, default=7)
    p.add_argument("--channel")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("pipeline", help="整条流水线，断点续跑")
    p.add_argument("--since-days", type=int, default=7)
    p.add_argument("--channel")
    p.add_argument("--video", action="append", help="只跑指定 video_id，可重复")
    p.add_argument("--max-episodes", type=int)
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--redo", action="store_true", help="已发布的也重跑")
    p.add_argument("--video-model", default="gemini-4-argon")
    p.add_argument("--text-model", default="gemini-4-hydrogen-high-oauth")
    p.add_argument("--text-backend", choices=["dalton", "gemini-api"], default="dalton",
                   help="翻译走 Dalton 内部订阅模型接口（默认，失败块退回 Gemini API）或直接走 Gemini API")
    p.set_defaults(func=cmd_pipeline)

    p = sub.add_parser("status", help="台账")
    p.add_argument("--pending", action="store_true", help="只看已出稿、未发飞书的")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("mark-published")
    p.add_argument("--video", required=True)
    p.add_argument("--doc-url", required=True)
    p.set_defaults(func=cmd_mark_published)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except SkillError as exc:
        emit({"ok": False, "error": str(exc), "hint": exc.hint or "检查参数与环境后重试"}, args.json)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        emit({"ok": False, "error": f"{type(exc).__name__}: {exc}", "hint": "带 --verbose 重跑，或看 stderr 日志定位是哪一步"}, args.json)
        sys.exit(1)


if __name__ == "__main__":
    main()
