#!/usr/bin/env python3
"""VIP news alert helper: window, filter, card render, and seen state.

The script does not fetch pages and does not write Chinese summaries. It
filters saved tool payloads to the strict 24-hour Hong Kong window, drops
duplicate URLs, titles, and status ids, and renders the card markdown.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Hong_Kong")
SEEN_REL = "state/vip-news-alert/seen.json"
SEEN_DAYS = 7
TIMELINE_COUNT = 50
MIN_DURATION_SECONDS = 120
PROFILE = "vip-news-alert"
LABEL_INTERVIEW = "**🎥 采访/演讲:**"
LABEL_ARTICLE = "**📰 新闻报道:**"
LABEL_HEADLINE = "**📰 动态:**"
SECTION_TRANSCRIPT = "完整采访记录"
FETCH_PROMPT = (
    "页面由 YouTube 抽取器生成。字幕在 markdown 的 Transcript 章节，"
    "每行以 [MM:SS] 开头。若该章节有正文，原样返回字幕，保留时间戳。"
    "若正文写明 No transcript available，或没有 Transcript 章节，只返回 NO_TRANSCRIPT。"
)
X_QUERY = (
    "查找过去24小时内由 Donald Trump、Scott Bessent、Kevin Warsh、Demis Hassabis、"
    "Elon Musk、Sam Altman、Dario Amodei 本人账号或其机构官方账号发布的实质动态，"
    "以及包含他们完整直接引语或原始视频的 X 原帖。"
    "必须返回作者、准确原帖 URL、发布时间和一句话内容，不要返回第三方评论。"
)
WIRE_ACCOUNTS = (
    "DeItaone",
    "CNN",
    "WSJ",
    "Bloomberg",
    "SkyNews",
    "FT",
    "FoxNews",
    "TrumpDailyPosts",
)
VIPS: tuple[dict[str, Any], ...] = (
    {
        "key": "Trump",
        "name": "Donald Trump",
        "search_names": ("Donald Trump",),
        "yt_queries": (
            "Trump interview today",
            "Trump press conference today",
            "Trump speech today",
        ),
        "keywords": ("TRUMP", "PRESIDENT", "WHITE HOUSE", "OVAL OFFICE"),
    },
    {
        "key": "Bessent",
        "name": "Scott Bessent",
        "search_names": ("Scott Bessent",),
        "yt_queries": (
            "Scott Bessent interview",
            "Treasury Secretary Bessent",
            "Bessent press conference",
            "Bessent remarks today",
        ),
        "keywords": ("BESSENT", "TREASURY SECRETARY"),
    },
    {
        "key": "Warsh",
        "name": "Kevin Warsh",
        "search_names": ("Kevin Warsh",),
        "yt_queries": (
            "Kevin Warsh interview",
            "Kevin Warsh Fed",
            "Kevin Warsh speech",
            "Kevin Warsh remarks",
        ),
        "keywords": ("WARSH",),
    },
    {
        "key": "Demis",
        "name": "Demis Hassabis",
        "search_names": ("Demis Hassabis",),
        "yt_queries": (
            "Demis Hassabis interview",
            "Hassabis DeepMind",
            "Hassabis keynote",
            "Hassabis talk",
        ),
        "keywords": ("HASSABIS", "DEEPMIND CEO"),
    },
    {
        "key": "Elon",
        "name": "Elon Musk",
        "search_names": ("Elon Musk",),
        "yt_queries": (
            "Elon Musk interview today",
            "Elon Musk speech",
            "Elon Musk press conference",
            "Elon Musk remarks today",
        ),
        "keywords": ("MUSK", "ELON", "TESLA CEO", "SPACEX"),
    },
    {
        "key": "Sam",
        "name": "Sam Altman",
        "search_names": ("Sam Altman",),
        "yt_queries": (
            "Sam Altman interview",
            "Sam Altman OpenAI",
            "Sam Altman keynote",
            "Sam Altman talk today",
        ),
        "keywords": ("ALTMAN", "OPENAI CEO"),
    },
    {
        "key": "Dario",
        "name": "Dario Amodei",
        "search_names": ("Dario Amodei",),
        "yt_queries": (
            "Dario Amodei interview",
            "Amodei Anthropic",
            "Dario Amodei talk",
            "Dario Amodei keynote",
        ),
        "keywords": ("AMODEI", "ANTHROPIC CEO"),
    },
)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_STATUS_RE = re.compile(r"/status/(\d+)")
_TWITTER_TIME_RE = re.compile(
    r"^[A-Za-z]{3}\s+([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+([+-]\d{4})\s+(\d{4})$"
)
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _card_io() -> Any:
    path = Path(__file__).resolve().parent / "card_io.py"
    name = "card_io_dalton_vip_news_alert"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("card helper is missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def require_workspace(raw: str | None) -> Path:
    if not raw:
        raise RuntimeError("missing --workspace-dir")
    path = Path(raw)
    if path.is_symlink():
        raise RuntimeError("workspace is a symlink")
    if not path.is_dir():
        raise RuntimeError("workspace not found")
    return path.resolve()


def _as_of(value: str | None) -> datetime:
    if not value:
        return datetime.now(TZ)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def compute_window(as_of: str | None = None) -> dict[str, str]:
    end = _as_of(as_of)
    start = end - timedelta(hours=24)
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    summary = end.date().isoformat()
    return {
        "timezone": "Asia/Hong_Kong",
        "start_iso": start.isoformat(),
        "end_iso": end.isoformat(),
        "start_date": start.date().isoformat(),
        "end_date": end.date().isoformat(),
        "summary_date": summary,
        "search_start_date": start_utc.date().isoformat(),
        "search_end_date": (end_utc + timedelta(days=1)).date().isoformat(),
        "x_from_date": start_utc.date().isoformat(),
        "x_to_date": end_utc.date().isoformat(),
        "report_run_id": f"{PROFILE}-{summary}",
    }


def parse_time(value: Any) -> datetime | None:
    """Parse a tool timestamp. Naive clock times and date-only values are UTC."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"\d{8}", text):
        try:
            return datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return _twitter_time(text)


def _twitter_time(text: str) -> datetime | None:
    """Parse an X `createdAt` value without relying on the process locale."""
    match = _TWITTER_TIME_RE.match(text)
    if match is None:
        return None
    month = _MONTHS.get(match.group(1).casefold())
    if month is None:
        return None
    sign = 1 if match.group(6)[0] == "+" else -1
    tz = timezone(
        sign * timedelta(hours=int(match.group(6)[1:3]), minutes=int(match.group(6)[3:5]))
    )
    try:
        return datetime(
            int(match.group(7)),
            month,
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
            int(match.group(5)),
            tzinfo=tz,
        )
    except ValueError:
        return None


def in_window(moment: datetime | None, start: datetime, end: datetime) -> bool:
    return moment is not None and start <= moment <= end


def _outside_window(norm: dict[str, Any], start: datetime, end: datetime) -> bool:
    moment = norm.get("moment")
    if not isinstance(moment, datetime):
        return False
    return not in_window(moment, start, end)


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def title_hash(title: str) -> str:
    cleaned = re.sub(r"[^\w\s]", "", title.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return ""
    return hashlib.sha256(cleaned.encode()).hexdigest()[:16]


def _youtube_id(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host in ("youtube.com", "m.youtube.com"):
        if parsed.path == "/watch":
            found = parse_qs(parsed.query).get("v") or []
            return found[0] if found else ""
        for prefix in ("/shorts/", "/live/", "/embed/"):
            if parsed.path.startswith(prefix):
                return parsed.path[len(prefix) :].split("/")[0].split("?")[0]
    elif host == "youtu.be":
        return parsed.path.lstrip("/").split("/")[0].split("?")[0]
    return ""


def _is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return host in {"youtube.com", "m.youtube.com", "youtu.be"}


def canonical_url(url: str) -> str:
    text = url.strip()
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host in ("x.com", "twitter.com"):
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 3 and parts[1] == "status" and parts[2].isdigit():
            return f"https://x.com/{parts[0]}/status/{parts[2]}"
    video_id = _youtube_id(text)
    if video_id and host in ("youtube.com", "m.youtube.com", "youtu.be"):
        return f"https://www.youtube.com/watch?v={video_id}"
    return text


def web_query(name: str) -> str:
    return f'"{name}" (interview OR speech OR says OR remarks)'


def youtube_query(text: str) -> str:
    return f"{text} site:youtube.com"


def display_source(source: str) -> str:
    bare = source.strip().lstrip("*").lstrip("@").strip()
    if bare == "DeItaone":
        return "*DeItaone"
    return bare


def transcript_rel(summary_date: str) -> str:
    return f"research-outputs/vip-news-alert/{summary_date}-transcripts.md"


def resolve_person(token: str) -> str:
    folded = token.strip().casefold()
    if not folded:
        raise RuntimeError("unknown person")
    for vip in VIPS:
        names = [vip["key"], *vip["search_names"]]
        folded_names = [str(name).casefold() for name in names]
        if folded in folded_names:
            return str(vip["key"])
        if len(folded) >= 3 and any(folded in name for name in folded_names):
            return str(vip["key"])
    raise RuntimeError(f"unknown person: {token}")


def _selected_vips(person: str | None) -> tuple[dict[str, Any], ...]:
    if not person:
        return VIPS
    key = resolve_person(person)
    return tuple(vip for vip in VIPS if vip["key"] == key)


def _vip_by_key(key: str) -> dict[str, Any]:
    for vip in VIPS:
        if vip["key"] == key:
            return vip
    raise RuntimeError(f"unknown person: {key}")


def match_person(text: str, vips: tuple[dict[str, Any], ...]) -> str | None:
    upper = text.upper()
    for vip in vips:
        if any(keyword in upper for keyword in vip["keywords"]):
            return str(vip["key"])
    return None


def _items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("items", "tweets", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "tweets", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _bucket(payload: Any) -> tuple[bool, list[Any], bool]:
    if isinstance(payload, list):
        return True, payload, False
    if not isinstance(payload, dict):
        return False, [], False
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    has_more = bool(
        payload.get("has_more")
        or payload.get("hasMore")
        or data.get("has_more")
        or data.get("hasMore")
    )
    if "ok" in payload and not payload.get("ok"):
        return False, [], False
    return True, _items(payload), has_more


def _index_accounts(raw: Any, names: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    found: dict[str, Any] = {}
    for key, value in raw.items():
        folded = str(key).strip().lstrip("@").casefold()
        for name in names:
            if folded == name.casefold() and name not in found:
                found[name] = value
    return found


def _text(item: dict[str, Any]) -> str:
    for key in ("text", "full_text", "summary_excerpt", "content", "snippet", "title"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _status_id(item: dict[str, Any], url: str) -> str:
    for key in ("status_id", "id", "id_str", "rest_id", "tweet_id"):
        value = item.get(key)
        if value is not None and str(value).strip().isdigit():
            return str(value).strip()
    match = _STATUS_RE.search(url)
    return match.group(1) if match else ""


def _handle(item: dict[str, Any], default: str) -> str:
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    raw = (
        item.get("author_handle")
        or item.get("handle")
        or item.get("author")
        or user.get("screenName")
        or user.get("screen_name")
        or default
    )
    return str(raw).strip().lstrip("@")


def _duration(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _clean_transcript(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or text == "NO_TRANSCRIPT" or "No transcript available" in text:
        return ""
    return text


class _Deduper:
    def __init__(self, active: set[str]) -> None:
        self.active = active
        self.ids: set[str] = set()
        self.urls: set[str] = set()
        self.titles: set[str] = set()

    def keep(self, *, url: str, title: str, status_id: str) -> bool:
        if status_id and status_id in self.ids:
            return False
        if url and (url in self.urls or url_hash(url) in self.active):
            return False
        hashed = title_hash(title) if title else ""
        if hashed and (hashed in self.titles or hashed in self.active):
            return False
        if status_id:
            self.ids.add(status_id)
        if url:
            self.urls.add(url)
        if hashed:
            self.titles.add(hashed)
        return True


def _entry_time(value: Any) -> datetime | None:
    if isinstance(value, dict):
        return parse_time(value.get("ts"))
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def active_seen_keys(seen: dict[str, Any], as_of: datetime) -> set[str]:
    cutoff = as_of - timedelta(days=SEEN_DAYS)
    return {str(key) for key, value in seen.items() if _fresh(value, cutoff)}


def _fresh(value: Any, cutoff: datetime) -> bool:
    moment = _entry_time(value)
    return moment is not None and moment > cutoff


def prune_seen(seen: dict[str, Any], as_of: datetime) -> dict[str, Any]:
    cutoff = as_of - timedelta(days=SEEN_DAYS)
    return {str(key): value for key, value in seen.items() if _fresh(value, cutoff)}


def plan_requests(as_of: str | None = None) -> dict[str, Any]:
    window = compute_window(as_of)
    wires = [
        {
            "handle": handle,
            "since": window["start_date"],
            "until": window["end_date"],
            "count": TIMELINE_COUNT,
        }
        for handle in WIRE_ACCOUNTS
    ]
    web_search = [
        {
            "person": vip["key"],
            "query": web_query(vip["search_names"][0]),
            "start_date": window["search_start_date"],
            "end_date": window["search_end_date"],
        }
        for vip in VIPS
    ]
    youtube = [
        {
            "person": vip["key"],
            "query": youtube_query(query),
            "start_date": window["search_start_date"],
            "end_date": window["search_end_date"],
        }
        for vip in VIPS
        for query in vip["yt_queries"]
    ]
    return {
        "ok": True,
        "window": window,
        "wires": wires,
        "web_search": web_search,
        "youtube": youtube,
        "x_search": {
            "query": X_QUERY,
            "from_date": window["x_from_date"],
            "to_date": window["x_to_date"],
            "enable_image_understanding": True,
            "enable_video_understanding": True,
        },
        "fetch_prompt": FETCH_PROMPT,
    }


def _moment_of(item: dict[str, Any]) -> datetime | None:
    for key in (
        "created_at",
        "createdAt",
        "date",
        "published_at",
        "published_date",
        "publishedDate",
        "upload_date",
        "published",
    ):
        moment = parse_time(item.get(key))
        if moment is not None:
            return moment
    return None


def _public_wire(item: dict[str, Any]) -> dict[str, str]:
    return {
        "status_id": item["status_id"],
        "url": item["url"],
        "title": item["title"],
        "text": item["text"],
        "created_at": item["created_at"],
        "source": item["source"],
        "handle": item["handle"],
    }


def _normalize_wire(item: Any, account: str) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    handle = _handle(item, account) or account
    status_id = _status_id(item, str(item.get("url") or ""))
    url = canonical_url(str(item.get("url") or ""))
    if not url and handle and status_id:
        url = f"https://x.com/{handle}/status/{status_id}"
    if url:
        url = canonical_url(url)
        if not status_id:
            status_id = _status_id(item, url)
    moment = _moment_of(item)
    text = _text(item)[:500]
    if moment is None:
        return None
    return {
        "status_id": status_id,
        "url": url,
        "title": text,
        "text": text,
        "created_at": moment.isoformat(),
        "moment": moment,
        "source": display_source(handle or account),
        "handle": handle or account,
    }


def _normalize_web(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    url = canonical_url(str(item.get("url") or item.get("link") or ""))
    moment = _moment_of(item)
    title = str(item.get("title") or "").strip()
    snippet = str(item.get("content") or item.get("snippet") or "").strip()
    hashed = title or snippet
    if not url.startswith(("https://", "http://")) or not hashed:
        return None
    row: dict[str, Any] = {
        "url": url,
        "title": hashed,
        "snippet": snippet,
    }
    if moment is None:
        row["time_unverified"] = True
    else:
        row["created_at"] = moment.isoformat()
        row["moment"] = moment
    return row


def _normalize_video(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    url = canonical_url(str(item.get("url") or item.get("webpage_url") or ""))
    video_id = str(item.get("video_id") or item.get("id") or "").strip()
    if not url and video_id:
        url = f"https://www.youtube.com/watch?v={video_id}"
        url = canonical_url(url)
    if not video_id:
        video_id = _youtube_id(url)
    moment = _moment_of(item)
    title = str(item.get("title") or "").strip()
    duration = _duration(item.get("duration"))
    if duration is not None and 0 < duration < MIN_DURATION_SECONDS:
        return None
    if not url.startswith(("https://", "http://")):
        return None
    row: dict[str, Any] = {
        "url": url,
        "title": title,
        "channel": str(item.get("channel") or "").strip(),
        "video_id": video_id,
    }
    if moment is None:
        row["time_unverified"] = True
    else:
        row["created_at"] = moment.isoformat()
        row["moment"] = moment
    if duration is not None:
        row["duration"] = duration
    transcript = _clean_transcript(item.get("raw_transcript") or item.get("transcript"))
    if transcript:
        row["raw_transcript"] = transcript
    return row


def _normalize_x(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    url = canonical_url(str(item.get("url") or ""))
    status_id = _status_id(item, url)
    author = str(item.get("author") or item.get("author_handle") or "").strip()
    moment = _moment_of(item)
    text = str(item.get("summary_excerpt") or item.get("text") or "").strip()
    tool_title = str(item.get("title") or "").strip()
    title = text or tool_title
    if not author or moment is None or not status_id:
        return None
    if not url.startswith("https://x.com/") or "/status/" not in url:
        return None
    return {
        "status_id": status_id,
        "url": url,
        "author": author,
        "title": title,
        "text": text or title,
        "created_at": moment.isoformat(),
        "moment": moment,
    }


def _public_video(item: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "url": item["url"],
        "title": item["title"],
        "channel": item.get("channel") or "",
        "video_id": item.get("video_id") or "",
    }
    if item.get("time_unverified"):
        row["time_unverified"] = True
    else:
        row["created_at"] = item["created_at"]
    if "duration" in item:
        row["duration"] = item["duration"]
    if item.get("raw_transcript"):
        row["raw_transcript"] = item["raw_transcript"]
    return row


def _as_video(item: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "url": item["url"],
        "title": item["title"],
        "channel": "",
        "video_id": _youtube_id(item["url"]),
    }
    if item.get("time_unverified"):
        row["time_unverified"] = True
    else:
        row["created_at"] = item["created_at"]
        row["moment"] = item.get("moment")
    return row


def _public_web(item: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "url": item["url"],
        "title": item["title"],
        "snippet": item.get("snippet") or "",
    }
    if item.get("time_unverified"):
        row["time_unverified"] = True
    else:
        row["created_at"] = item["created_at"]
    return row


def _public_x(item: dict[str, Any]) -> dict[str, str]:
    return {
        "status_id": item["status_id"],
        "url": item["url"],
        "author": item["author"],
        "title": item["title"],
        "text": item["text"],
        "created_at": item["created_at"],
    }


def filter_raw(
    raw: dict[str, Any],
    window: dict[str, str],
    seen: dict[str, Any],
    *,
    person: str | None = None,
) -> dict[str, Any]:
    start = datetime.fromisoformat(window["start_iso"])
    end = datetime.fromisoformat(window["end_iso"])
    selected = _selected_vips(person)
    selected_keys = {str(vip["key"]) for vip in selected}
    dedupe = _Deduper(active_seen_keys(seen, end))
    buckets: dict[str, dict[str, list[Any]]] = {
        str(vip["key"]): {"wire": [], "videos": [], "web": [], "x": []} for vip in selected
    }
    notes: list[str] = []
    wire_ok = 0
    wires = _index_accounts(raw.get("wires") or raw.get("accounts"), WIRE_ACCOUNTS)
    for account in WIRE_ACCOUNTS:
        ok, items, has_more = _bucket(wires.get(account))
        if account not in wires:
            ok = False
        if not ok:
            notes.append(f"{account} 时间线没有返回")
            continue
        wire_ok += 1
        if has_more:
            notes.append(f"{account} 时间线已返回，窗口内可能还有未取回的帖子")
        for item in items:
            norm = _normalize_wire(item, account)
            if norm is None or not in_window(norm["moment"], start, end):
                continue
            matched = match_person(norm["text"], selected)
            if matched is None:
                continue
            if not dedupe.keep(url=norm["url"], title=norm["title"], status_id=norm["status_id"]):
                continue
            buckets[matched]["wire"].append(_public_wire(norm))

    video_ok = 0
    video_in = _index_accounts(raw.get("videos"), tuple(selected_keys))
    for vip in selected:
        key = str(vip["key"])
        ok, items, _has_more = _bucket(video_in.get(key))
        if key not in video_in:
            ok = False
        if not ok:
            notes.append(f"{vip['name']} 的视频检索没有返回")
            continue
        video_ok += 1
        for item in items:
            norm = _normalize_video(item)
            if norm is None or _outside_window(norm, start, end):
                continue
            if not dedupe.keep(url=norm["url"], title=norm["title"], status_id=""):
                continue
            buckets[key]["videos"].append(_public_video(norm))

    web_ok = 0
    web_in = _index_accounts(raw.get("web"), tuple(selected_keys))
    web_rows: list[tuple[str, dict[str, Any], Any]] = []
    for vip in selected:
        key = str(vip["key"])
        ok, items, _has_more = _bucket(web_in.get(key))
        if key not in web_in:
            ok = False
        if not ok:
            notes.append(f"{vip['name']} 的网页检索没有返回")
            continue
        web_ok += 1
        for item in items:
            norm = _normalize_web(item)
            if norm is None or _outside_window(norm, start, end):
                continue
            web_rows.append((key, norm, item))
    # YouTube links found by web search join the video group before plain
    # pages are deduped, so a same-title article never drops the video.
    for key, norm, item in [row for row in web_rows if _is_youtube(row[1]["url"])]:
        duration = _duration(item.get("duration")) if isinstance(item, dict) else None
        if duration is not None and 0 < duration < MIN_DURATION_SECONDS:
            continue
        if not dedupe.keep(url=norm["url"], title=norm["title"], status_id=""):
            continue
        video = _as_video(norm)
        if duration is not None:
            video["duration"] = duration
        buckets[key]["videos"].append(_public_video(video))
    for key, norm, _item in [row for row in web_rows if not _is_youtube(row[1]["url"])]:
        if not dedupe.keep(url=norm["url"], title=norm["title"], status_id=""):
            continue
        buckets[key]["web"].append(_public_web(norm))

    search = raw.get("x_search")
    search_ok, search_items, _search_more = _bucket(search)
    if not isinstance(search, (dict, list)):
        search_ok = False
    unmatched: list[dict[str, str]] = []
    if not search_ok:
        notes.append("补充检索没有返回")
    else:
        for item in search_items:
            norm = _normalize_x(item)
            if norm is None or not in_window(norm["moment"], start, end):
                continue
            if not dedupe.keep(url=norm["url"], title=norm["title"], status_id=norm["status_id"]):
                continue
            matched = match_person(f"{norm['text']} {norm['title']}", selected)
            public = _public_x(norm)
            if matched is None:
                unmatched.append(public)
            else:
                buckets[matched]["x"].append(public)

    people = []
    counts = {"wire": 0, "videos": 0, "web": 0, "x_posts": 0, "unmatched_x": len(unmatched)}
    for vip in selected:
        key = str(vip["key"])
        bucket = buckets[key]
        counts["wire"] += len(bucket["wire"])
        counts["videos"] += len(bucket["videos"])
        counts["web"] += len(bucket["web"])
        counts["x_posts"] += len(bucket["x"])
        people.append(
            {
                "key": key,
                "name": vip["name"],
                "wire_headlines": bucket["wire"],
                "videos": bucket["videos"],
                "web_articles": bucket["web"],
                "x_posts": bucket["x"],
            }
        )
    # True only when all 8 timelines, all 7 web groups, all 7 video groups,
    # and x_search failed. A --person subset stays false.
    sources_all_failed = (
        wire_ok == 0
        and web_ok == 0
        and video_ok == 0
        and not search_ok
        and len(selected) == len(VIPS)
    )
    return {
        "window": window,
        "report_run_id": window["report_run_id"],
        "people": people,
        "unmatched_x": unmatched,
        "notes": notes,
        "counts": counts,
        "sources_all_failed": sources_all_failed,
    }


def _summary_date(events: dict[str, Any]) -> str:
    value = events.get("summary_date")
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise RuntimeError("summary date is invalid")
    return value


def _require_http(value: Any) -> str:
    if not isinstance(value, str):
        raise RuntimeError("url must be http")
    url = canonical_url(value)
    if not url.startswith(("https://", "http://")):
        raise RuntimeError("url must be http")
    return url


def _require_text(item: dict[str, Any], key: str, label: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} is empty")
    return value.strip()


def _points(item: dict[str, Any]) -> list[str] | None:
    if "points" not in item or item.get("points") is None:
        return None
    value = item.get("points")
    if not isinstance(value, list) or len(value) != 3:
        raise RuntimeError("transcript points must be 3")
    points: list[str] = []
    for point in value:
        if not isinstance(point, str) or not point.strip():
            raise RuntimeError("transcript points must be 3")
        points.append(point.strip())
    return points


def _source_notes(events: dict[str, Any]) -> list[str]:
    raw = events.get("source_notes") or []
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RuntimeError("source note is invalid")
    notes: list[str] = []
    for note in raw:
        if not isinstance(note, str) or not note.strip() or "\n" in note:
            raise RuntimeError("source note is invalid")
        notes.append(note.strip())
    return notes


def _people(events: dict[str, Any]) -> list[dict[str, Any]]:
    raw = events.get("people")
    if not isinstance(raw, list):
        raise RuntimeError("people is missing")
    ordered: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeError("unknown person")
        key = item.get("key")
        if not isinstance(key, str) or key not in {str(vip["key"]) for vip in VIPS}:
            raise RuntimeError("unknown person")
        if key in ordered:
            raise RuntimeError("duplicate person")
        ordered[key] = item
    return [ordered[str(vip["key"])] for vip in VIPS if str(vip["key"]) in ordered]


def _column(item: dict[str, Any], kind: str) -> dict[str, Any]:
    summary = _require_text(item, "summary", "summary")
    title = _require_text(item, "title", "title")
    row: dict[str, Any] = {"summary": summary, "title": title}
    if kind == "headline":
        source = _require_text(item, "source", "headline source")
        row["source"] = display_source(source)
        if item.get("url"):
            row["url"] = _require_http(item.get("url"))
        status_id = item.get("status_id")
        if status_id is not None and str(status_id).strip():
            row["status_id"] = str(status_id).strip()
        return row
    row["url"] = _require_http(item.get("url"))
    if kind == "interview":
        points = _points(item)
        if points is not None:
            row["points"] = points
    return row


def _rows(person: dict[str, Any], key: str, kind: str) -> list[dict[str, Any]]:
    raw = person.get(key) or []
    if not isinstance(raw, list):
        raise RuntimeError(f"{key} is invalid")
    return [_column(item, kind) for item in raw if isinstance(item, dict)]


def _ensure_transcript(workspace: Path, summary_date: str) -> None:
    relative = transcript_rel(summary_date)
    path = _card_io().workspace_file(workspace, relative)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("transcript file missing")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise RuntimeError("transcript file missing")


def build_markdown(events: dict[str, Any], workspace: Path, *, as_of: str | None = None) -> dict[str, Any]:
    summary_date = _summary_date(events)
    if as_of:
        expected = compute_window(as_of)["summary_date"]
        if summary_date != expected:
            raise RuntimeError("summary date does not match the window")
    notes = _source_notes(events)
    blocks: list[tuple[str, list[str]]] = []
    pushed: list[tuple[str, str]] = []
    transcript_blocks: list[list[str]] = []
    for person in _people(events):
        vip = _vip_by_key(str(person["key"]))
        interviews = _rows(person, "interviews", "interview")
        articles = _rows(person, "articles", "article")
        headlines = _rows(person, "headlines", "headline")
        count = len(interviews) + len(articles) + len(headlines)
        if count == 0:
            continue
        body: list[str] = []
        if interviews:
            body.append(LABEL_INTERVIEW)
            for item in interviews:
                body.append(f"- {item['summary']}")
                body.append(f"  {item['url']}")
                pushed.append((item["url"], item["title"]))
                if item.get("points"):
                    block = [f"**{vip['name']}：{item['title']}**"]
                    block.extend(f"- {point}" for point in item["points"])
                    block.append(f"  {item['url']}")
                    transcript_blocks.append(block)
            body.append("")
        if articles:
            body.append(LABEL_ARTICLE)
            for item in articles:
                body.append(f"- {item['summary']}")
                body.append(f"  {item['url']}")
                pushed.append((item["url"], item["title"]))
            body.append("")
        if headlines:
            body.append(LABEL_HEADLINE)
            for item in headlines:
                body.append(f"- {item['summary']} [source: {item['source']}]")
                pushed.append((item.get("url") or "", item["title"]))
            body.append("")
        heading = f"◎ {vip['name']}（{count}条动态）"
        blocks.append((heading, body))
    if not blocks:
        return {
            "empty": True,
            "title": "",
            "markdown": "",
            "report_run_id": f"{PROFILE}-{summary_date}",
            "pushed": [],
        }
    if transcript_blocks:
        _ensure_transcript(workspace, summary_date)
    title = f"📡 VIP 动态速递 — {summary_date}"
    lines = [f"# {title}", ""]
    for heading, body in blocks:
        lines.append(f"## {heading}")
        lines.append("")
        lines.extend(body)
    if transcript_blocks:
        lines.append(f"## {SECTION_TRANSCRIPT}")
        lines.append("")
        for block in transcript_blocks:
            lines.extend(block)
            lines.append("")
        lines.append(f"完整记录已存工作区 {transcript_rel(summary_date)}")
        lines.append("")
    if notes:
        lines.append("## 来源")
        lines.append("")
        lines.extend(f"- {note}" for note in notes)
        lines.append("")
    markdown = "\n".join(lines).rstrip() + "\n"
    return {
        "empty": False,
        "title": title,
        "markdown": markdown,
        "report_run_id": f"{PROFILE}-{summary_date}",
        "pushed": pushed,
        "summary_date": summary_date,
    }


def manifest_rel(summary_date: str) -> str:
    return f"temp/vip-news-alert/{summary_date}/manifest.json"


def card_content_sha256(card: dict[str, Any]) -> str:
    """Match the cards branch of automation_report_content_sha256.

    The canonical object is profile, content_kind, the one-card list, and a
    null failure text. The text field is omitted. Key order does not change
    the digest.
    """
    canonical = {
        "profile": PROFILE,
        "content_kind": "cards",
        "cards": [card],
        "failure_text": None,
    }
    raw = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _seen_entries(pushed: list[tuple[str, str]]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for url, title in pushed:
        candidates: list[tuple[str, str]] = []
        if url:
            candidates.append((url_hash(url), "url"))
        hashed = title_hash(title) if title else ""
        if hashed:
            candidates.append((hashed, "title"))
        for key, kind in candidates:
            if key in seen:
                continue
            seen.add(key)
            entries.append({"key": key, "kind": kind, "url": url, "title": title})
    return entries


def _write_manifest(workspace: Path, built: dict[str, Any], card_rel: str) -> None:
    card = _read_json(workspace, card_rel)
    if not isinstance(card, dict):
        raise RuntimeError("card digest mismatch")
    payload = {
        "report_run_id": built["report_run_id"],
        "card": card_rel,
        "content_sha256": card_content_sha256(card),
        "seen_keys": _seen_entries(built["pushed"]),
    }
    _write_text(
        workspace,
        manifest_rel(str(built["summary_date"])),
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _run_date(report_run_id: Any) -> str | None:
    prefix = f"{PROFILE}-"
    if not isinstance(report_run_id, str) or not report_run_id.startswith(prefix):
        return None
    summary_date = report_run_id[len(prefix) :]
    if not _DATE_RE.fullmatch(summary_date):
        return None
    return summary_date


def _load_manifest(workspace: Path, summary_date: str) -> dict[str, Any]:
    path = _card_io().workspace_file(workspace, manifest_rel(summary_date))
    if path.is_symlink():
        raise RuntimeError("refusing to read a missing file or a symlink")
    if not path.is_file():
        raise RuntimeError("nothing was sent")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise RuntimeError("manifest is invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("manifest is invalid")
    return payload


def _manifest_entries(manifest: dict[str, Any]) -> list[dict[str, str]]:
    raw = manifest.get("seen_keys")
    if not isinstance(raw, list):
        raise RuntimeError("manifest is invalid")
    entries: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeError("manifest is invalid")
        key = item.get("key")
        kind = item.get("kind")
        url = item.get("url") if item.get("url") is not None else ""
        title = item.get("title") if item.get("title") is not None else ""
        if not isinstance(key, str) or not key or kind not in {"url", "title"}:
            raise RuntimeError("manifest is invalid")
        if not isinstance(url, str) or not isinstance(title, str):
            raise RuntimeError("manifest is invalid")
        entries.append({"key": key, "kind": kind, "url": url, "title": title})
    return entries


def _verified_digest(workspace: Path, manifest: dict[str, Any]) -> str:
    recorded = manifest.get("content_sha256")
    card_rel = manifest.get("card")
    if not isinstance(recorded, str) or not recorded or not isinstance(card_rel, str) or not card_rel:
        raise RuntimeError("manifest is invalid")
    card = _read_json(workspace, card_rel)
    if not isinstance(card, dict):
        raise RuntimeError("card digest mismatch")
    digest = card_content_sha256(card)
    if digest != recorded:
        raise RuntimeError("card digest mismatch")
    return digest


def _confirmed(confirm: Any, report_run_id: str) -> bool:
    if not isinstance(confirm, dict):
        return False
    return (
        confirm.get("ok") is True
        and confirm.get("delivery_confirmed") is True
        and confirm.get("status") == "sent"
        and confirm.get("content_kind") == "cards"
        and confirm.get("report_run_id") == report_run_id
    )


def load_seen(workspace: Path) -> dict[str, Any]:
    path = _card_io().workspace_file(workspace, SEEN_REL)
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def save_seen(workspace: Path, seen: dict[str, Any]) -> None:
    path = _card_io().workspace_file(workspace, SEEN_REL)
    encoded = json.dumps(seen, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    _card_io().write_bytes_exclusive(path, encoded)


def commit_seen(
    workspace: Path,
    confirm: Any,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    report_run_id = confirm.get("report_run_id") if isinstance(confirm, dict) else None
    summary_date = _run_date(report_run_id)
    if summary_date is None or not _confirmed(confirm, str(report_run_id)):
        raise RuntimeError("delivery not confirmed")
    if as_of and compute_window(as_of)["summary_date"] != summary_date:
        raise RuntimeError("summary date does not match the window")
    manifest = _load_manifest(workspace, summary_date)
    if manifest.get("report_run_id") != report_run_id:
        raise RuntimeError("delivery not confirmed")
    entries = _manifest_entries(manifest)
    digest = _verified_digest(workspace, manifest)
    if isinstance(confirm, dict) and "content_sha256" in confirm and confirm.get("content_sha256") != digest:
        raise RuntimeError("card digest mismatch")
    if not entries:
        raise RuntimeError("nothing was sent")
    moment = _as_of(as_of)
    seen = prune_seen(load_seen(workspace), moment)
    stamp = moment.isoformat()
    before = set(seen)
    for entry in entries:
        seen[entry["key"]] = {
            "url": entry["url"],
            "title": entry["title"],
            "ts": stamp,
            "kind": entry["kind"],
        }
    added = len(set(seen) - before)
    save_seen(workspace, seen)
    return {"ok": True, "path": SEEN_REL, "added": added, "kept": len(seen)}


def _read_json(workspace: Path, relative: str) -> Any:
    path = _card_io().workspace_file(workspace, relative)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("refusing to read a missing file or a symlink")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_text(workspace: Path, relative: str, text: str) -> None:
    path = _card_io().workspace_file(workspace, relative)
    _card_io().write_bytes_exclusive(path, text.encode("utf-8"))


def _write_bytes(workspace: Path, relative: str, payload: bytes) -> None:
    path = _card_io().workspace_file(workspace, relative)
    _card_io().write_bytes_exclusive(path, payload)


def cmd_window(args: argparse.Namespace) -> dict[str, Any]:
    require_workspace(args.workspace_dir)
    return {"ok": True, **compute_window(args.as_of)}


def cmd_plan(args: argparse.Namespace) -> dict[str, Any]:
    require_workspace(args.workspace_dir)
    return plan_requests(args.as_of)


def cmd_list_vips(args: argparse.Namespace) -> dict[str, Any]:
    require_workspace(args.workspace_dir)
    vips = [
        {
            "key": vip["key"],
            "name": vip["name"],
            "keywords": list(vip["keywords"]),
            "yt_queries": list(vip["yt_queries"]),
        }
        for vip in VIPS
    ]
    return {"ok": True, "vips": vips}


def cmd_self_test(args: argparse.Namespace) -> dict[str, Any]:
    require_workspace(args.workspace_dir)
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, error: str = "") -> None:
        row: dict[str, Any] = {"name": name, "ok": ok}
        if error:
            row["error"] = error
        checks.append(row)

    add("vips", len(VIPS) == 7 and len({vip["key"] for vip in VIPS}) == 7)
    add("wires", len(WIRE_ACCOUNTS) == 8 and "DeItaone" in WIRE_ACCOUNTS)
    add("seen_path", SEEN_REL == "state/vip-news-alert/seen.json")
    try:
        _card_io().find_renderer()
        add("renderer", True)
    except (FileNotFoundError, OSError) as exc:
        add("renderer", False, str(exc))
    return {"ok": all(row["ok"] for row in checks), "checks": checks}


def cmd_filter(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    raw = _read_json(workspace, args.input)
    if not isinstance(raw, dict):
        raise RuntimeError("filter input must be a JSON object")
    window = compute_window(args.as_of)
    filtered = filter_raw(raw, window, load_seen(workspace), person=args.person)
    _write_text(
        workspace,
        args.output,
        json.dumps(filtered, ensure_ascii=False, indent=2) + "\n",
    )
    return {
        "ok": True,
        "path": args.output,
        "report_run_id": filtered["report_run_id"],
        "counts": filtered["counts"],
        "sources_all_failed": filtered["sources_all_failed"],
        "notes": filtered["notes"],
    }


def cmd_render(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    events = _read_json(workspace, args.events)
    if not isinstance(events, dict):
        raise RuntimeError("events must be a JSON object")
    built = build_markdown(events, workspace, as_of=args.as_of)
    if built["empty"]:
        return {
            "ok": True,
            "empty": True,
            "report_run_id": built["report_run_id"],
        }
    md_path = _card_io().workspace_file(workspace, args.markdown)
    prior = md_path.read_bytes() if md_path.is_file() and not md_path.is_symlink() else None
    _write_text(workspace, args.markdown, built["markdown"])
    try:
        result = _card_io().render_markdown(
            workspace=workspace,
            markdown_rel=args.markdown,
            output_rel=args.output,
            title=built["title"],
            subtitle=f"{built['summary_date']} · 严格 24 小时",
        )
    except Exception:  # noqa: BLE001 - restore the previous markdown when card rendering fails
        if prior is not None:
            _write_bytes(workspace, args.markdown, prior)
        elif md_path.is_file() and not md_path.is_symlink():
            md_path.unlink()
        raise
    if not result.get("ok"):
        if prior is not None:
            _write_bytes(workspace, args.markdown, prior)
        elif md_path.is_file() and not md_path.is_symlink():
            md_path.unlink()
        result.setdefault("message", result.get("hint") or result.get("error") or "render failed")
        return result
    _write_manifest(workspace, built, args.output)
    result["empty"] = False
    result["title"] = built["title"]
    result["markdown"] = args.markdown
    result["report_run_id"] = built["report_run_id"]
    result["manifest"] = manifest_rel(str(built["summary_date"]))
    return result


def cmd_commit_seen(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    confirm = _read_json(workspace, args.confirm)
    return commit_seen(workspace, confirm, as_of=args.as_of)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VIP news alert helper")
    parser.add_argument("--workspace-dir", default=None)
    parser.add_argument("--json", action="store_true")
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--workspace-dir", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    window = sub.add_parser("window", parents=[shared])
    window.add_argument("--as-of")
    window.set_defaults(func=cmd_window)

    plan = sub.add_parser("plan", parents=[shared])
    plan.add_argument("--as-of")
    plan.set_defaults(func=cmd_plan)

    listed = sub.add_parser("list-vips", parents=[shared])
    listed.set_defaults(func=cmd_list_vips)

    probe = sub.add_parser("self-test", parents=[shared])
    probe.set_defaults(func=cmd_self_test)

    filtered = sub.add_parser("filter", parents=[shared])
    filtered.add_argument("--input", required=True)
    filtered.add_argument("--output", required=True)
    filtered.add_argument("--as-of")
    filtered.add_argument("--person")
    filtered.set_defaults(func=cmd_filter)

    render = sub.add_parser("render", parents=[shared])
    render.add_argument("--events", required=True)
    render.add_argument("--markdown", required=True)
    render.add_argument("--output", required=True)
    render.add_argument("--as-of")
    render.set_defaults(func=cmd_render)

    commit = sub.add_parser("commit-seen", parents=[shared])
    commit.add_argument("--confirm", required=True)
    commit.add_argument("--as-of")
    commit.set_defaults(func=cmd_commit_seen)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except Exception as exc:  # noqa: BLE001 - the CLI reports any failure as one JSON object
        result = {"ok": False, "error": str(exc), "message": str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
