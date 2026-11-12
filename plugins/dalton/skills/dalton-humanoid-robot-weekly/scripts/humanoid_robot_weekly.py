#!/usr/bin/env python3
"""Humanoid weekly helper: plan, validate, card render, and sent-state.

Research stays with the agent. This script only builds the source plan,
checks the card markdown, renders Card Schema 2.0, and records a sent archive
after the agent has already confirmed delivery.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

VERSION = "1.0.0"
TZ = ZoneInfo("Asia/Hong_Kong")
STATE_REL = "state/humanoid-robot-weekly/state.json"
HEADING_GROUPS = (
    ("## 本周结论",),
    ("## 🧠 模型与控制", "## 🧠 大脑 / 模型能力"),
    ("## 🏭 规模化商业部署",),
    ("## ⚙️ 经济性与可靠性",),
    ("## 📰 产品、融资与产业事件",),
    ("## 🎙️ Guidepoint 新增访谈",),
    ("## 📡 社区与开发者信号",),
    ("## 👀 下周观察",),
    ("## 📝 来源与口径",),
)
X_HANDLES = (
    "physical_int",
    "SkildAI",
    "Figure_robot",
    "AgilityRobotics",
    "Apptronik",
    "SanctuaryAI",
    "1x_tech",
    "BostonDynamics",
    "GoogleDeepMind",
    "DrJimFan",
    "svlevine",
    "hausman_k",
    "LerrelPinto",
    "yukez",
    "pathak2206",
)
GUIDEPOINT_QUERIES = (
    {
        "query": "Commercial deployment of humanoid robots in factories and warehouses; customer scale, utilization, reliability and repeat orders",
        "keywords": ["humanoid", "factory deployment", "warehouse", "utilization", "repeat orders"],
    },
    {
        "query": "Humanoid robot VLA generalization, dexterous manipulation, whole-body control, teleoperation and failure recovery",
        "keywords": ["humanoid", "VLA", "dexterous manipulation", "whole-body control", "teleoperation"],
    },
    {
        "query": "Humanoid robot cost, RaaS economics, maintenance, actuators, reducers, roller screws and production ramp",
        "keywords": ["humanoid", "RaaS", "actuators", "reducers", "roller screws", "maintenance"],
    },
)
WEB_QUERIES = {
    "model": [
        "vision-language-action robot foundation model benchmark real robot",
        "whole-body control dexterous manipulation long-horizon failure recovery robot",
        "cross-embodiment robot model code weights dataset release",
    ],
    "deployment_en": [
        "humanoid robot deployed factory customer units production line",
        "humanoid robots operating at plant paid deployment throughput reliability",
    ],
    "deployment_zh": [
        "人形机器人 工厂 已部署 台 正式生产 客户",
        "人形机器人 产线 批量应用 运行 工序 复购",
    ],
    "deployment_ja": [
        "ヒューマノイド ロボット 工場 導入 台 稼働 生産ライン",
    ],
    "deployment_ko": [
        "휴머노이드 로봇 공장 도입 대 가동 생산라인 고객",
    ],
    "deployment_de": [
        "humanoider Roboter Werk Einsatz Stück Produktionslinie Kunde",
    ],
}


def _card_io() -> Any:
    path = Path(__file__).resolve().parent / "card_io.py"
    name = "card_io_dalton_humanoid_robot_weekly"
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


def skill_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def require_workspace(raw: str | None) -> Path:
    if not raw:
        raise RuntimeError("missing --workspace-dir")
    path = Path(raw)
    if path.is_symlink():
        raise RuntimeError("workspace is a symlink")
    if not path.is_dir():
        raise RuntimeError("workspace not found")
    return path.resolve()


def window(as_of: str | None = None, days: int = 7) -> dict[str, Any]:
    if as_of:
        parsed = datetime.fromisoformat(as_of)
        end = parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)
    else:
        end = datetime.now(TZ)
    start = end - timedelta(days=days)
    return {
        "timezone": "Asia/Hong_Kong",
        "start_date": start.date().isoformat(),
        "end_date": end.date().isoformat(),
        "start_iso": start.isoformat(),
        "end_iso": end.isoformat(),
        "days": days,
    }


def build_plan(as_of: str | None, days: int) -> dict[str, Any]:
    span = window(as_of, days)
    start = span["start_date"]
    end = span["end_date"]
    web_calls = [
        {"query": query, "start_date": start, "end_date": end, "topic": "news"}
        for queries in WEB_QUERIES.values()
        for query in queries
    ]
    guidepoint_calls = [
        {
            "route": "guidepoint",
            "operation": "search_library",
            "query": item["query"],
            "start": start,
            "end": end,
            "limit": 30,
            "params": {"keywords": item["keywords"], "recency_bias": 50},
        }
        for item in GUIDEPOINT_QUERIES
    ]
    timeline_calls = [
        {
            "route": "x-timeline",
            "operation": "tweets",
            "handle": handle,
            "since": start,
            "until": end,
            "count": 20,
        }
        for handle in X_HANDLES
    ]
    return {
        "ok": True,
        "version": VERSION,
        "window": span,
        "deployment_gate": {
            "single_site_active_units_min": 250,
            "single_customer_active_units_min": 500,
            "single_customer_binding_order_min": 1000,
            "orders_are_not_deployments": True,
        },
        "model_evidence_main_card": ["A", "B"],
        "web_queries": WEB_QUERIES,
        "guidepoint_queries": [item["query"] for item in GUIDEPOINT_QUERIES],
        "x_handles": list(X_HANDLES),
        "subreddits": [
            "robotics",
            "HumanoidRobotics",
            "ROS",
            "ReinforcementLearning",
            "controltheory",
        ],
        "reddit_covered": False,
        "missing_params": [
            "enable_image_understanding",
            "enable_video_understanding",
            "allowed_x_handles",
        ],
        "tool_calls": {
            "web_search": web_calls,
            "guidepoint": guidepoint_calls,
            "x_timeline": timeline_calls,
            "x_search": {
                "query": "humanoid robot deployment OR foundation model OR factory",
                "from_date": start,
                "to_date": end,
                "handles": list(X_HANDLES),
            },
        },
        "message": "weekly source plan ready",
    }


def self_test(workspace: Path) -> dict[str, Any]:
    root = skill_dir()
    checks: dict[str, bool] = {
        "python_3_12_plus": sys.version_info >= (3, 12),
        "template": (root / "templates" / "weekly-card.md").is_file(),
        "source_universe": (root / "references" / "source-universe.yaml").is_file(),
    }
    writable = False
    try:
        probe = _card_io().workspace_file(workspace, "state/humanoid-robot-weekly/.write-probe")
        _card_io().write_bytes_exclusive(probe, b"1")
        probe.unlink()
        writable = True
    except (OSError, ValueError):
        writable = False
    checks["state_dir_writable"] = writable
    try:
        _card_io().find_renderer()
        checks["card_renderer"] = True
    except (OSError, FileNotFoundError):
        checks["card_renderer"] = False
    missing = [name for name, ok in checks.items() if not ok]
    if missing:
        return {
            "ok": False,
            "error": "required checks failed: " + ", ".join(missing),
            "hint": "Restore the template, source list, writable state directory, and shared card renderer.",
            "checks": checks,
        }
    return {"ok": True, "message": "self-test passed", "checks": checks}


def _resolve_report(workspace: Path, raw: str) -> Path:
    candidate = Path(raw)
    roots = (workspace, skill_dir())
    if not candidate.is_absolute():
        for root in roots:
            nested = root / candidate
            if nested.is_file():
                candidate = nested
                break
        else:
            candidate = workspace / candidate
    if candidate.is_symlink() or not candidate.is_file():
        raise RuntimeError("report file not found")
    resolved = candidate.resolve()
    if not any(resolved == root.resolve() or root.resolve() in resolved.parents for root in roots):
        raise RuntimeError("report file is outside the workspace and the skill directory")
    return resolved


def _heading_body(text: str, heading: str) -> str:
    """Return the body under ``heading`` until the next heading of the same or higher level."""
    lines = text.splitlines()
    level = len(heading.split(" ", 1)[0])
    start = None
    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = index + 1
            break
    if start is None:
        return ""
    body: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            marks = stripped.split(" ", 1)[0]
            if set(marks) == {"#"} and len(marks) <= level:
                break
        body.append(line)
    return "\n".join(body)


def _label_error(body: str, topic: str, label: str, message: str) -> str | None:
    """Require ``label`` on a section line that states ``topic``, unless that line is the empty week."""
    for line in body.splitlines():
        stripped = line.strip()
        # Only the template's empty-week bullet ("- 本周无…") is exempt; a line that merely
        # mentions 本周无 next to a real item ("公司本周无新融资，仅发布演示") is still checked.
        if not stripped or stripped.startswith("#") or stripped.lstrip("-*• ").startswith("本周无"):
            continue
        if topic in stripped and label not in stripped:
            return message
    return None


def validate_text(text: str) -> list[str]:
    errors: list[str] = []
    for group in HEADING_GROUPS:
        if not any(heading in text for heading in group):
            errors.append("missing heading: " + group[0])
    if "|" in text and "\n|---" in text:
        errors.append("markdown tables are not allowed in the card")
    if "### 规模化订单观察" in text:
        order_body = _heading_body(text, "### 规模化订单观察")
    else:
        order_body = _heading_body(text, "## 🏭 规模化商业部署")
    order_error = _label_error(order_body, "订单", "订单，尚非部署", "orders must be labeled 订单，尚非部署")
    if order_error:
        errors.append(order_error)
    demo_error = _label_error(
        _heading_body(text, "## 📰 产品、融资与产业事件"),
        "演示",
        "演示级证据",
        "demo claims must be labeled 演示级证据",
    )
    if demo_error:
        errors.append(demo_error)
    return errors


def validate_file(workspace: Path, raw: str) -> dict[str, Any]:
    try:
        path = _resolve_report(workspace, raw)
    except RuntimeError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "hint": "Pass --file with the weekly card markdown.",
        }
    errors = validate_text(path.read_text(encoding="utf-8"))
    if errors:
        return {
            "ok": False,
            "error": "; ".join(errors),
            "hint": "Use templates/weekly-card.md and preserve deployment and evidence labels.",
        }
    return {"ok": True, "message": "report validation passed", "file": path.name}


def mark_sent(workspace: Path, args: argparse.Namespace) -> dict[str, Any]:
    archive = _card_io().workspace_file(workspace, args.archive)
    if not archive.is_file():
        raise RuntimeError("archive not found")
    errors = validate_text(archive.read_text(encoding="utf-8"))
    if errors:
        return {
            "ok": False,
            "error": "; ".join(errors),
            "hint": "Fix the archive before recording it as sent.",
        }
    if args.validation != "passed":
        raise RuntimeError("validation must be passed")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    state_path = _card_io().workspace_file(workspace, STATE_REL)
    state: dict[str, Any] = {"reports": {}}
    if state_path.is_file():
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise TypeError("state file is not an object")
        state = loaded
        reports = state.get("reports")
        state["reports"] = reports if isinstance(reports, dict) else {}
    current = state["reports"].get(args.report_date)
    record = {
        "report_date": args.report_date,
        "status": "sent",
        "archive": args.archive,
        "sha256": digest,
        "validation": "passed",
        "window_start": args.window_start,
        "window_end": args.window_end,
    }
    if args.x_search_calls is not None:
        record["x_search_calls"] = args.x_search_calls
    if isinstance(current, dict):
        if current.get("sha256") == digest and current.get("archive") == args.archive:
            return {"ok": True, "reused": True, "path": STATE_REL, "sha256": digest}
        return {
            "ok": False,
            "error": "state_conflict",
            "hint": "this report date already has a different archive hash; left the state unchanged",
        }
    state["reports"][args.report_date] = record
    text = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _card_io().write_bytes_exclusive(state_path, text.encode("utf-8"))
    return {"ok": True, "reused": False, "path": STATE_REL, "sha256": digest}


def _title_from(path: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# ") and not line.startswith("## "):
            return line[2:].strip()
    return ""


def cmd_self_test(args: argparse.Namespace) -> dict[str, Any]:
    return self_test(require_workspace(args.workspace_dir))


def cmd_plan(args: argparse.Namespace) -> dict[str, Any]:
    return build_plan(args.as_of, args.days)


def cmd_window(args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, "window": window(args.as_of, args.days)}


def cmd_validate(args: argparse.Namespace) -> dict[str, Any]:
    return validate_file(require_workspace(args.workspace_dir), args.file)


def cmd_render(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    if args.validate:
        checked = validate_file(workspace, args.file)
        if not checked.get("ok"):
            return checked
    source = _resolve_report(workspace, args.file)
    relative = args.file
    if Path(relative).is_absolute() or not (workspace / relative).is_file():
        copied_rel = f"temp/humanoid-robot-weekly/{source.name}"
        copied = _card_io().workspace_file(workspace, copied_rel)
        if not copied.exists():
            _card_io().write_bytes_exclusive(copied, source.read_bytes())
        relative = copied_rel
    return _card_io().render_markdown(
        workspace=workspace,
        markdown_rel=relative,
        output_rel=args.output,
        title=_title_from(source, args.title),
    )


def cmd_mark_sent(args: argparse.Namespace) -> dict[str, Any]:
    return mark_sent(require_workspace(args.workspace_dir), args)


def _build_parser() -> argparse.ArgumentParser:
    # Separate action objects. parents= reuses one object, and set_defaults
    # would replace its SUPPRESS default; the subparser would then write None
    # over a --workspace-dir already parsed before the subcommand.
    parser = argparse.ArgumentParser(description="Humanoid robot weekly helper")
    parser.add_argument("--workspace-dir", default=None)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--workspace-dir", default=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test", parents=[shared]).set_defaults(func=cmd_self_test)
    plan = sub.add_parser("plan", parents=[shared])
    plan.add_argument("--as-of")
    plan.add_argument("--days", type=int, default=7)
    plan.set_defaults(func=cmd_plan)
    span = sub.add_parser("window", parents=[shared])
    span.add_argument("--as-of")
    span.add_argument("--days", type=int, default=7)
    span.set_defaults(func=cmd_window)
    checked = sub.add_parser("validate", parents=[shared])
    checked.add_argument("--file", required=True)
    checked.set_defaults(func=cmd_validate)
    render = sub.add_parser("render-card", parents=[shared])
    render.add_argument("--file", required=True)
    render.add_argument("--output", required=True)
    render.add_argument("--title")
    render.add_argument("--validate", action="store_true")
    render.set_defaults(func=cmd_render)
    sent = sub.add_parser("mark-sent", parents=[shared])
    sent.add_argument("--report-date", required=True)
    sent.add_argument("--archive", required=True)
    sent.add_argument("--window-start", required=True)
    sent.add_argument("--window-end", required=True)
    sent.add_argument("--validation", default="passed")
    sent.add_argument("--x-search-calls", type=int)
    sent.set_defaults(func=cmd_mark_sent)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except Exception as exc:  # noqa: BLE001 - the CLI reports any failure as one JSON object
        result = {"ok": False, "error": str(exc), "hint": "Check the command arguments and workspace path."}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
