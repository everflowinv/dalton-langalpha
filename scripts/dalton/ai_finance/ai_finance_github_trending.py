#!/usr/bin/env python3
"""Discover broadly, measure official star velocity, and keep tracking breakouts."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

try:
    from .ai_finance_github_momentum import (
        REPORT_LIMIT,
        complete_through,
        evaluate_history,
        github_json,
        history_days,
        report_rank,
        resolve_explicit_path,
        significance,
    )
except ImportError:
    from ai_finance_github_momentum import (
        REPORT_LIMIT,
        complete_through,
        evaluate_history,
        github_json,
        history_days,
        report_rank,
        resolve_explicit_path,
        significance,
    )


TREND_URL = "https://trendshift.io"


def dalton_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_state_file() -> Path:
    return dalton_root() / "state" / "ai_finance" / "github_trending_history.json"


def default_seen_file() -> Path:
    return dalton_root() / "state" / "ai_finance" / "github_seen.json"
AI_TAGS = {
    "3d-generation",
    "ai-agent",
    "ai-coding",
    "ai-infrastructure",
    "ai-skills",
    "ai-workflow",
    "generative-ai",
    "local-llm",
    "machine-learning",
    "mcp",
    "nlp",
    "workflow-automation",
}
AI_TEXT = re.compile(
    r"\b(?:ai|llms?|agents?|agentic|artificial intelligence|generative ai|machine learning|"
    r"language model|claude|codex|deepseek|gemini|copilot|model context protocol|tokeni[sz](?:er|ation))\b|智能体|大模型|人工智能",
    re.IGNORECASE,
)

# Tokens too generic to identify a shared upstream model/SDK/API.
UPSTREAM_STOPWORDS = {
    "agent", "agents", "agentic", "ai", "api", "app", "assistant", "audio", "audit",
    "auto", "awesome", "bot", "browser", "chat", "cli", "client", "cloud", "code",
    "coder", "coding", "computer", "container", "context", "core", "data", "database",
    "deep", "demo", "desktop", "dev", "docs", "engine", "fast", "framework", "free",
    "gen", "go", "gpt", "gui", "hub", "image", "inference", "js", "kit", "lab", "labs",
    "lite", "llm", "llms", "local", "mcp", "memory", "mini", "ml", "mobile", "model",
    "models", "next", "node", "open", "openai", "os", "platform", "plugin", "project",
    "prompt", "py", "python", "rag", "research", "review", "run", "runtime", "sandbox",
    "screen", "sdk", "search", "security", "server", "service", "skill", "skills",
    "smart", "stack", "starter", "studio", "swarm", "system", "template", "terminal",
    "test", "text", "tool", "toolkit", "tools", "ts", "ui", "use", "v1", "v2", "vector",
    "video",
    "vision", "voice", "web", "workflow",
}
UPSTREAM_TOKEN_RE = re.compile(r"[a-z][a-z0-9]{2,}")
UPSTREAM_WINDOW_HOURS = 48.0
UPSTREAM_MIN_REPOS = 2


def fetch_text(url: str, timeout: int = 30) -> str:
    req = Request(
        url,
        headers={
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "User-Agent": "Eve-AI-Finance-Tracker/1.0",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_next_chunks(html: str) -> str:
    pattern = re.compile(
        r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)</script>',
        re.DOTALL,
    )
    chunks: list[str] = []
    for match in pattern.finditer(html):
        try:
            chunks.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    return "".join(chunks)


def parse_initial_data(html: str) -> list[dict[str, Any]]:
    payload = extract_next_chunks(html)
    marker = '"initialData":'
    idx = payload.find(marker)
    if idx < 0:
        raise ValueError("Trendshift Next.js payload does not contain initialData")
    value, _ = json.JSONDecoder().raw_decode(payload[idx + len(marker) :])
    if not isinstance(value, list):
        raise ValueError("Trendshift initialData is not a list")
    return [row for row in value if isinstance(row, dict)]


def is_ai_related(row: dict[str, Any]) -> bool:
    tags = {
        str(tag.get("slug") or tag.get("name") or "").lower()
        for tag in row.get("tags", [])
        if isinstance(tag, dict)
    }
    if tags & AI_TAGS:
        return True
    text = " ".join(
        str(row.get(key) or "")
        for key in ("full_name", "repository_description")
    )
    return bool(AI_TEXT.search(text))


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in UPSTREAM_TOKEN_RE.findall(str(text or "").lower())
        if token not in UPSTREAM_STOPWORDS
    }


def name_tokens(item: dict[str, Any]) -> set[str]:
    """Tokens taken from owner/repo only; an upstream primitive usually shows up here."""
    return _tokens(str(item.get("full_name") or "").replace("/", " "))


def mention_tokens(item: dict[str, Any]) -> set[str]:
    """Tokens from name, description, topics and homepage host."""
    parts = [
        str(item.get("full_name") or "").replace("/", " "),
        str(item.get("description") or ""),
        " ".join(str(tag) for tag in item.get("trendshift_tags") or []),
        " ".join(str(tag) for tag in (item.get("github") or {}).get("topics") or []),
        str((item.get("github") or {}).get("homepage") or ""),
    ]
    return _tokens(" ".join(parts))


def recent_repo_tokens(
    state: dict[str, Any], window_hours: float, now: datetime | None = None
) -> dict[str, dict[str, Any]]:
    """Repos seen in earlier snapshots inside the window, with their stored tokens."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    seen: dict[str, dict[str, Any]] = {}
    for snapshot in state.get("snapshots") or []:
        if not isinstance(snapshot, dict):
            continue
        captured_at = snapshot.get("captured_at")
        try:
            age = (current - datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))).total_seconds() / 3600
        except (TypeError, ValueError):
            continue
        if age > window_hours or age < 0:
            continue
        for full_name, meta in (snapshot.get("repos") or {}).items():
            if not isinstance(meta, dict):
                continue
            entry = seen.setdefault(
                full_name,
                {"name_tokens": set(), "mention_tokens": set(), "first_seen": captured_at},
            )
            # Snapshots written before token storage existed still yield name tokens.
            fallback = _tokens(str(full_name).replace("/", " "))
            entry["name_tokens"] |= set(meta.get("name_tokens") or fallback)
            entry["mention_tokens"] |= set(meta.get("mention_tokens") or fallback)
    return seen


def upstream_clusters(
    items: list[dict[str, Any]],
    state: dict[str, Any],
    window_hours: float = UPSTREAM_WINDOW_HOURS,
    min_repos: int = UPSTREAM_MIN_REPOS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Group trending repos that share one non-generic upstream token.

    A token qualifies only when it appears in at least one repository *name* and is
    mentioned by `min_repos` distinct repos inside the window (today's candidates plus
    earlier snapshots). This is what surfaces "several trending repos wrap the same new
    model/SDK", which single-repo dedupe would otherwise hide.
    """
    universe: dict[str, dict[str, Any]] = {}
    for full_name, entry in recent_repo_tokens(state, window_hours, now).items():
        universe[full_name] = {
            "name_tokens": set(entry["name_tokens"]),
            "mention_tokens": set(entry["mention_tokens"]),
            "today": False,
        }
    for item in items:
        full_name = item.get("full_name") or ""
        universe[full_name] = {
            "name_tokens": name_tokens(item),
            "mention_tokens": mention_tokens(item),
            "today": True,
        }

    by_token: dict[str, dict[str, set[str]]] = {}
    for full_name, entry in universe.items():
        for token in entry["mention_tokens"]:
            bucket = by_token.setdefault(token, {"mention": set(), "name": set()})
            bucket["mention"].add(full_name)
            if token in entry["name_tokens"]:
                bucket["name"].add(full_name)

    clusters: list[dict[str, Any]] = []
    for token, bucket in by_token.items():
        if not bucket["name"] or len(bucket["mention"]) < min_repos:
            continue
        # One owner publishing several repos is not a shared upstream.
        if len({name.split("/")[0].lower() for name in bucket["mention"]}) < min_repos:
            continue
        repos = sorted(bucket["mention"])
        today_repos = sorted(name for name in repos if universe[name]["today"])
        if not today_repos:
            continue
        clusters.append(
            {
                "upstream_token": token,
                "repos": repos,
                "repos_today": today_repos,
                "repo_count": len(repos),
                "window_hours": window_hours,
                "in_repo_name": sorted(bucket["name"]),
            }
        )
    clusters.sort(key=lambda cluster: (-cluster["repo_count"], cluster["upstream_token"]))
    return clusters


def github_repo(full_name: str, timeout: int = 20) -> dict[str, Any]:
    data = github_json(f"repos/{full_name}")
    return {
        "full_name": data.get("full_name", full_name),
        "description": data.get("description") or "",
        "stars": data.get("stargazers_count"),
        "forks": data.get("forks_count"),
        "open_issues": data.get("open_issues_count"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "pushed_at": data.get("pushed_at"),
        "homepage": data.get("homepage"),
        "license": (data.get("license") or {}).get("spdx_id"),
        "topics": data.get("topics") or [],
        "archived": bool(data.get("archived")),
    }


def velocity_bucket(gain: int, gain_pct: float) -> str:
    if gain >= 1000 or (gain >= 100 and gain_pct >= 50):
        return "explosive"
    if gain >= 300 or (gain >= 100 and gain_pct >= 20):
        return "very_fast"
    if gain >= 100 or (gain >= 50 and gain_pct >= 10):
        return "fast"
    return "notable"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"snapshots": []}
    # Fail visibly rather than overwrite an unreadable active watchlist.
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"State must be a JSON object: {path}")
    return data


def previous_snapshot(state: dict[str, Any]) -> dict[str, Any] | None:
    snapshots = state.get("snapshots") or []
    if not isinstance(snapshots, list) or not snapshots:
        return None
    latest = snapshots[-1]
    return latest if isinstance(latest, dict) else None


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        name = handle.name
    os.replace(name, path)


def update_state(
    path: Path, state: dict[str, Any], items: list[dict[str, Any]], now: datetime | None = None
) -> None:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    captured_at = current.isoformat().replace("+00:00", "Z")
    repos: dict[str, Any] = {}
    for item in items:
        verified_stars = (item.get("github") or {}).get("stars")
        repos[item["full_name"]] = {
            "stars": verified_stars if isinstance(verified_stars, int) else item.get("stars"),
            "trendshift_stars_gained": item.get("stars_gained_daily"),
            "name_tokens": sorted(name_tokens(item)),
            "mention_tokens": sorted(mention_tokens(item)),
        }
    snapshots = state.get("snapshots") or []
    if not isinstance(snapshots, list):
        snapshots = []
    snapshots.append({"captured_at": captured_at, "repos": repos})
    state["snapshots"] = snapshots[-35:]
    tracked = state.setdefault("tracked_repos", {})
    for item in items:
        name = item["full_name"]
        prior = tracked.get(name, {})
        entry = {**prior, "last_checked": captured_at,
                 "description": item.get("description") or prior.get("description", ""),
                 "first_seen": prior.get("first_seen", captured_at),
                 "previously_reported": item.get("previously_reported", False)}
        if item.get("history_verified"):
            curve = item["momentum"]
            entry.update({k: curve[k] for k in ("phase", "momentum_active", "needs_reacceleration", "complete_through", "daily_velocity_3d", "gain_7d")})
            if entry["phase"] != prior.get("phase"):
                entry["phase_since"] = captured_at
            if entry["momentum_active"]:
                entry["last_active_at"] = captured_at
        # A transport failure is not evidence that an existing hotspot cooled.
        tracked[name] = entry
    cutoff = (current - timedelta(days=90)).isoformat()
    state["tracked_repos"] = {name: entry for name, entry in tracked.items()
                              if entry.get("momentum_active") or entry.get("last_checked", "") >= cutoff}
    state["version"] = 2
    atomic_json(path, state)


def discover() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """No keyword gate: even a blank description must reach official verification."""
    pool: dict[str, dict[str, Any]] = {}
    coverage: dict[str, Any] = {"sources": {}, "errors": {}}
    urls = {"daily": TREND_URL, "weekly": TREND_URL + "/weekly", "monthly": TREND_URL + "/monthly"}
    with ThreadPoolExecutor(max_workers=3) as executor:
        jobs = {executor.submit(lambda url: parse_initial_data(fetch_text(url)), url): period for period, url in urls.items()}
        for future in as_completed(jobs):
            period = jobs[future]
            try:
                rows = future.result()
                if not rows:
                    raise ValueError("empty discovery page")
                coverage["sources"][period] = len(rows)
                for row in rows:
                    name = str(row.get("full_name") or "")
                    if not re.fullmatch(r"[\w.-]+/[\w.-]+", name):
                        continue
                    item = pool.setdefault(name, {"full_name": name, "discovery": {}})
                    item["discovery"][period] = {"rank": row.get("rank"), "stars_gained": row.get("repository_stars_gained"), "date": row.get("date") or row.get("week_start")}
                    if not item.get("repository_description") or period == "daily":
                        item.update({k: v for k, v in row.items() if k != "discovery"})
            except Exception as exc:
                coverage["errors"][period] = f"{type(exc).__name__}: {exc}"
    return pool, coverage


def watch_pool(pool: dict[str, dict[str, Any]], state: dict[str, Any], seen: dict[str, Any], now: datetime) -> None:
    cutoff = (now - timedelta(days=14)).isoformat()
    for name, entry in (state.get("tracked_repos") or {}).items():
        if entry.get("momentum_active") or entry.get("last_active_at", "") >= cutoff:
            pool.setdefault(name, {"full_name": name, "repository_description": entry.get("description", ""), "discovery": {}})
            pool[name]["watchlist_source"] = True
    for name, entry in (seen.get("repos") or {}).items():
        if isinstance(entry, dict) and float(entry.get("last_ts") or 0) >= now.timestamp() - 14*86400:
            pool.setdefault(name, {"full_name": name, "discovery": {}})["legacy_seen_source"] = True
    # The first upgraded run recovers recently discovered names even if the old
    # prompt suppressed them. Later runs use explicit per-repository state.
    if not state.get("tracked_repos"):
        for snapshot in (state.get("snapshots") or [])[-7:]:
            for name in (snapshot.get("repos") or {}):
                pool.setdefault(name, {"full_name": name, "discovery": {}})["legacy_snapshot_source"] = True


def verify_momentum(row: dict[str, Any], state: dict[str, Any], seen: dict[str, Any], now: datetime) -> dict[str, Any]:
    name = row["full_name"]
    previous = dict((state.get("tracked_repos") or {}).get(name) or {})
    reported = bool(previous.get("previously_reported") or name in (seen.get("repos") or {}))
    previous["previously_reported"] = reported
    daily = (row.get("discovery") or {}).get("daily", {})
    item = {
        "full_name": name, "github_url": f"https://github.com/{name}",
        "description": row.get("repository_description") or previous.get("description", ""),
        "discovery": row.get("discovery", {}), "stars_gained_daily": daily.get("stars_gained"),
        "trendshift_tags": [tag.get("display_name") or tag.get("name") for tag in row.get("tags", []) if isinstance(tag, dict)],
        "ai_hint": is_ai_related(row), "ai_review_required": not reported,
        "previously_reported": reported, "github_verified": False,
        "history_verified": False, "report_eligible": False,
    }
    try:
        item["github"] = github_repo(name)
        item["github_verified"] = True
        item["stars"] = item["github"]["stars"]
        item["description"] = item["github"]["description"] or item["description"]
        history = github_json(f"repos/{name}/stargazers/history?per_page=6")
        through = complete_through(now)
        days = history_days(history, through, item["github"]["created_at"])
        curve = evaluate_history(days, through, previous)
        if item["github"]["archived"]:
            curve.update(phase="archived", momentum_active=False)
        item["momentum"] = curve
        item["significance"] = significance(curve, item["github"]["stars"], reported)
        item["history_verified"] = True
        item["report_eligible"] = curve["momentum_active"] and item["significance"]["meaningful"]
        item["report_kind"] = "continuing" if reported else "first_coverage"
        item["history_url"] = f"https://api.github.com/repos/{name}/stargazers/history"
    except Exception as exc:
        item["validation_error"] = f"{type(exc).__name__}: {exc}"
    return item


def select_candidates(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    ranked = sorted((item for item in items if item["report_eligible"]), key=report_rank, reverse=True)
    # The output limit budgets new reviews only. It cannot hide an already
    # published hotspot that remains active, even after it leaves today's lists.
    continuing = [item for item in ranked if item["previously_reported"]]
    new = [item for item in ranked if not item["previously_reported"]][:max(limit, 0)]
    return sorted(continuing + new, key=report_rank, reverse=True)


def self_test() -> None:
    fixture = (
        '<script>self.__next_f.push([1,"29:[\\"$\\",\\"x\\",null,'
        '{\\"initialData\\":[{\\"full_name\\":\\"demo/agent\\",'
        '\\"repository_description\\":\\"AI agent\\",'
        '\\"repository_stars\\":120,\\"repository_stars_gained\\":20,'
        '\\"tags\\":[{\\"slug\\":\\"ai-agent\\"}]}]}"])</script>'
    )
    rows = parse_initial_data(fixture)
    assert len(rows) == 1 and rows[0]["full_name"] == "demo/agent"
    assert is_ai_related(rows[0])
    assert velocity_bucket(1000, 1.0) == "explosive"

    # Same-day cluster: two repos wrapping one upstream model (2026-09-18 shape).
    same_day = [
        {"full_name": "browser-use/jev-ultrafast", "description": "Ultrafast browser agent"},
        {"full_name": "jarrodwatts/jev-trader", "description": "Onchain trading agent"},
    ]
    clusters = upstream_clusters(same_day, {"snapshots": []})
    assert [c["upstream_token"] for c in clusters] == ["jev"], clusters
    assert clusters[0]["repo_count"] == 2

    # Generic tokens must not cluster.
    generic = [
        {"full_name": "a/coding-agent", "description": "an AI agent"},
        {"full_name": "b/agent-tools", "description": "AI agent toolkit"},
    ]
    assert upstream_clusters(generic, {"snapshots": []}) == []

    # One owner shipping several repos is not a shared upstream.
    same_owner = [
        {"full_name": "Tencent/BrowserSkill", "description": "browser control"},
        {"full_name": "Tencent/WeKnora", "description": "retrieval"},
    ]
    assert upstream_clusters(same_owner, {"snapshots": []}) == []

    # Cross-day cluster: yesterday's snapshot plus one new repo today.
    yesterday = datetime.now(timezone.utc).timestamp() - 20 * 3600
    prior_state = {
        "snapshots": [
            {
                "captured_at": datetime.fromtimestamp(yesterday, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "repos": {
                    "browser-use/jev-ultrafast": {
                        "name_tokens": ["jev", "ultrafast", "browser"],
                        "mention_tokens": ["jev", "ultrafast", "browser"],
                    }
                },
            }
        ]
    }
    today_only = [{"full_name": "awlevin/typesafe-computer-use", "description": "classify actions with Jev"}]
    cross = upstream_clusters(today_only, prior_state)
    assert [c["upstream_token"] for c in cross] == ["jev"], cross
    assert cross[0]["repos_today"] == ["awlevin/typesafe-computer-use"]

    # Stale snapshots fall out of the window.
    stale_state = {
        "snapshots": [
            {
                "captured_at": datetime.fromtimestamp(
                    datetime.now(timezone.utc).timestamp() - 96 * 3600, timezone.utc
                )
                .isoformat()
                .replace("+00:00", "Z"),
                "repos": {"browser-use/jev-ultrafast": {"name_tokens": ["jev"], "mention_tokens": ["jev"]}},
            }
        ]
    }
    assert upstream_clusters(today_only, stale_state) == []

    print(json.dumps({"ok": True, "tests": 9}))


def collect_trending(
    *,
    limit: int = 20,
    verify_limit: int = 100,
    state_file: Path,
    seen_file: Path,
    output: Path | None = None,
    upstream_window_hours: float = UPSTREAM_WINDOW_HOURS,
    min_cluster_repos: int = UPSTREAM_MIN_REPOS,
    update_state_file: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one trending collection. Reads seen and may update the watch pool; never writes seen."""
    state_file = resolve_explicit_path(state_file)
    seen_file = resolve_explicit_path(seen_file)
    if output is not None:
        output = resolve_explicit_path(output)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    pool, coverage = discover()
    state = load_state(state_file)
    seen = load_state(seen_file)
    watch_pool(pool, state, seen, current)
    if not pool:
        raise RuntimeError(f"No discovery or watchlist inputs: {coverage['errors']}")
    tracked = state.get("tracked_repos") or {}
    priority = sorted(pool.values(), key=lambda row: (
        not bool(tracked.get(row["full_name"], {}).get("momentum_active")),
        not bool(row.get("discovery")),
        min((int(value.get("rank") or 999) for value in row.get("discovery", {}).values()), default=999),
        row["full_name"],
    ))
    active_count = sum(bool(tracked.get(row["full_name"], {}).get("momentum_active")) for row in priority)
    queue = priority[:max(verify_limit, active_count, 0)]
    if not queue:
        raise ValueError("--verify-limit must permit at least one repository")
    with ThreadPoolExecutor(max_workers=6) as executor:
        items = list(executor.map(lambda row: verify_momentum(row, state, seen, current), queue))
    if not any(item["history_verified"] for item in items):
        raise RuntimeError("All official GitHub history checks failed; no momentum claims may be made")
    candidates = select_candidates(items, limit)
    clusters = upstream_clusters(candidates, state, upstream_window_hours, min_cluster_repos, current)
    if update_state_file:
        update_state(state_file, state, items, current)
    coverage.update({
        "pool_repositories": len(pool), "checked_repositories": len(items),
        "history_verified": sum(item["history_verified"] for item in items),
        "eligible_repositories": sum(item["report_eligible"] for item in items),
        "deferred_verification": [row["full_name"] for row in priority[len(queue):]],
        "deferred_new_review": [item["full_name"] for item in items if item["report_eligible"] and item not in candidates],
    })
    payload = {
            "generated_at": current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "version": 3,
            "discovery_source": "Trendshift daily + weekly + monthly + persistent watchlist",
            "canonical_validation_source": "https://api.github.com/repos/{owner}/{repo}/stargazers/history",
            "methodology": (
                "Broad discovery without AI keyword exclusions. Official completed-date counts only, "
                "with a conservative 12h completion buffer. Enter at >=1000/day or >=5000/7d with >=300/day "
                "3-day velocity. Keep reporting published hotspots while velocity is sustained or rising; "
                "cool only after two consecutive completed dates with recent-3d/prior-3d <0.80. "
                "Reentry requires qualifying growth and >=1.10 acceleration. Metadata/history failures "
                "do not erase active watch state. AI relevance must be reviewed from the repository, "
                "not inferred from ai_hint. Trendshift period gains are discovery hints only."
                " Publication also requires absolute AND size-adjusted gains: new coverage "
                ">=1000/day with >=1% of current total, or >=5000/7d with >=5%; continuing "
                ">=300/day with >=0.5%, or >=2000/7d with >=3%. Rank both groups together by "
                "breakout_score after AI review; publish at most 8 total, keep other repos monitored."
            ),
            "complete_through": complete_through(current).isoformat(),
            "coverage": coverage,
            "candidates": candidates,
            "report_limit": REPORT_LIMIT,
            "ranking_policy": "breakout_score desc, daily_velocity_3d desc, gain_7d desc; AI relevance review before final top 8",
            "continuing_hotspots": [item["full_name"] for item in candidates if item["previously_reported"]],
            "below_significance": [item["full_name"] for item in items if item.get("momentum", {}).get("momentum_active") and not item["report_eligible"]],
            "cooled": [{"full_name": item["full_name"], "previously_reported": item["previously_reported"], "momentum": item["momentum"]}
                       for item in items if item.get("momentum", {}).get("phase") == "cooling"],
            "data_gaps": [{"full_name": item["full_name"], "error": item.get("validation_error")}
                          for item in items if not item["history_verified"]],
            "upstream_clusters": clusters,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20, help="maximum NEW candidates; continuing hotspots are never truncated")
    parser.add_argument("--verify-limit", type=int, default=100, help="repository verification budget; active tracked repos are always checked")
    parser.add_argument("--state-file", type=Path, default=None)
    parser.add_argument("--seen-file", type=Path, default=None)
    parser.add_argument(
        "--upstream-window-hours",
        type=float,
        default=UPSTREAM_WINDOW_HOURS,
        help="lookback window for shared-upstream clustering",
    )
    parser.add_argument(
        "--min-cluster-repos",
        type=int,
        default=UPSTREAM_MIN_REPOS,
        help="minimum distinct repos sharing a token before a cluster is emitted",
    )
    parser.add_argument("--output", type=Path, help="write JSON to this path instead of stdout")
    parser.add_argument("--no-state-update", action="store_true")
    parser.add_argument("--now", help="fixed timezone-aware ISO timestamp; default is the current UTC time")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    try:
        fixed_now = datetime.fromisoformat(args.now) if args.now else None
        payload = collect_trending(
            limit=args.limit,
            verify_limit=args.verify_limit,
            state_file=args.state_file or default_state_file(),
            seen_file=args.seen_file or default_seen_file(),
            output=args.output,
            upstream_window_hours=args.upstream_window_hours,
            min_cluster_repos=args.min_cluster_repos,
            update_state_file=not args.no_state_update,
            now=fixed_now,
        )
        if args.output is None:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 0
    except Exception as exc:  # operational CLI: return actionable JSON error
        error = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
        sys.stderr.write(json.dumps(error, ensure_ascii=False) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
