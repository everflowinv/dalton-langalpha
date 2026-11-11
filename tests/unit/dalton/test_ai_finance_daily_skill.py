"""Registration and live call shape for dalton-ai-finance-daily.

Checks the registry entry, the installed references, and the parameter lists
of the prepare, read, and signals tools. It does not call the host.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.dalton.tools.ai_finance_inputs_tools import (  # noqa: E402
    create_ai_finance_inputs_tools,
)
from src.dalton.tools.ai_finance_signals_tools import (  # noqa: E402
    create_ai_finance_signals_tools,
)

SKILL_DIR = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-ai-finance-daily"
SKILL = SKILL_DIR / "SKILL.md"
QUERIES = (
    "AI startup funding round announced today Series A Series B Series C Series D CURRENT_YEAR",
    "new AI model release LLM",
    "AI finance fintech deal acquisition",
    "artificial intelligence enterprise partnership",
    "AI regulation policy government",
    "AI chip semiconductor infrastructure",
    "AI product launch consumer enterprise deployment",
    "AI productivity enterprise workforce layoff efficiency impact",
    "AI 大模型 融资 发布 CURRENT_YEAR",
    "人工智能 投资 金融科技",
    "AI 芯片 算力 数据中心",
    "人工智能 监管 政策",
    "AI 产品发布 消费级 企业级",
    "人工智能 效率 就业 裁员 企业应用",
)
HEADINGS = (
    "## 📊 AI + 金融投资日报",
    "### 今日要闻",
    "### GitHub AI 项目动量",
    "### Earnings Calls：AI 的直接业务影响",
    "#### 跨日共性与新趋势",
    "### 来源说明",
)


def test_daily_skill_is_registered_from_frontmatter() -> None:
    from ptc_agent.agent.middleware.skills.dalton_registry import (
        DALTON_SKILL_DEFINITIONS,
        _DALTON_SKILL_SPECS,
    )

    assert ("dalton-ai-finance-daily", None, ()) in _DALTON_SKILL_SPECS
    skill = DALTON_SKILL_DEFINITIONS["dalton-ai-finance-daily"]
    assert skill.command is None
    assert skill.requires == ()
    assert skill.hidden is False
    assert skill.skill_md_path == "skills/dalton-ai-finance-daily/SKILL.md"
    text = SKILL.read_text(encoding="utf-8")
    match = re.search(r'^description:\s*"(.*)"\s*$', text, re.M)
    assert match is not None
    assert match.group(1) == skill.description
    assert len(skill.description) <= 120
    assert "触发词" not in skill.description


def test_references_cover_the_original_contract() -> None:
    assert not (SKILL_DIR / "references" / "workflow.md").exists()
    names = ("news.md", "github.md", "earnings.md", "signals.md", "report.md")
    texts = {name: (SKILL_DIR / "references" / name).read_text(encoding="utf-8") for name in names}
    news = texts["news.md"]
    assert len(QUERIES) == 14
    assert len(set(QUERIES)) == 14
    for query in QUERIES:
        assert news.count(f"`{query}`") == 1
    assert "标准化英文核心事件的 SHA256" in news
    for guessed in ("after_date", "start_published_date", "time_range", "start_date"):
        assert guessed not in news
    assert "相关结果列表为空" in news
    report = texts["report.md"]
    for heading in HEADINGS:
        assert heading in report
    signals = texts["signals.md"]
    assert "evidence_status" in signals
    assert "upstream_primitive_adoption" in signals
    assert "workflow.md" not in signals
    skill = SKILL.read_text(encoding="utf-8")
    assert "workflow.md" not in skill
    assert skill.index("news_seen") < skill.index("跑完十四条查询")
    assert "embedding_errors" in skill
    assert "CRON_RECOVERED_OK" in skill
    assert "delivery_confirmed" in skill and "seen_status" in skill
    order = skill.index('ai_finance_signals(\n    "save"')
    assert order < skill.index('ai_finance_signals("compare"')
    assert skill.index('ai_finance_signals("compare"') < skill.index('ai_finance_signals("read"')
    assert skill.index('ai_finance_signals("read"') < skill.index('ai_finance_signals("ingest"')
    assert skill.index('ai_finance_signals("ingest"') < skill.index('ai_finance_signals("stats"')


def test_documented_calls_match_live_tool_signatures() -> None:
    prepare, read = create_ai_finance_inputs_tools()
    signals = create_ai_finance_signals_tools()[0]
    prepare_signature = inspect.signature(prepare.coroutine)
    read_signature = inspect.signature(read.coroutine)
    signals_signature = inspect.signature(signals.coroutine)
    assert list(prepare_signature.parameters) == ["config"]
    assert prepare_signature.parameters["config"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(read_signature.parameters) == [
        "batch_id",
        "resource",
        "doc_id",
        "offset",
        "max_chars",
        "config",
    ]
    assert read_signature.parameters["config"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(signals_signature.parameters) == [
        "action",
        "batch_id",
        "github_decisions",
        "earnings_dispositions",
        "signal_records",
        "offset",
        "max_chars",
        "config",
    ]
    assert signals_signature.parameters["config"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signals_signature.parameters["github_decisions"].default is None
    assert signals_signature.parameters["max_chars"].default == 16000
    skill = SKILL.read_text(encoding="utf-8")
    assert "ai_finance_prepare()" in skill
    assert 'ai_finance_read(batch_id, "candidate", doc_id=DOC_ID, offset=0)' in skill
    assert 'ai_finance_report("status", batch_id)' in skill
    assert 'ai_finance_report("finalize", batch_id)' in skill
    assert "ai_finance_report(\"submit\", batch_id, markdown=正文, news_events=" in skill
    assert 'ai_finance_report("submit", batch_id)' in skill
