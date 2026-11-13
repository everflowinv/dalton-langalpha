"""Merged conflict lines and stop conditions stay in both guidance modes.

Lean is a subset of detailed, so a rule that both modes must follow is written
outside the detailed fence. These check that the merged sentences survived.
"""

import pytest

from ptc_agent.agent.middleware.background_subagent.subagent import (
    TASK_TOOL_DESCRIPTION,
)
from ptc_agent.agent.prompts import guidance_template_vars, init_loader

_BASE = dict(current_time="2026-10-03 11:00 ET", subagent_summary="")

_STATUS = (
    "更新用于正在运行的任务；恢复仅用于工具明确标为可恢复的任务。"
    "用户取消的任务不自动恢复或重新派发。"
    "因超时、额度或执行中断而停止的任务，按返回的原因和恢复说明处理。"
)

_BOTH_MODES = (
    "普通聊天使用紧邻主张的来源链接",
    "同一份交付物采用一种引用格式",
    "子代理内部交付还须保留可检查的原始来源位置",
    "Memory（长期记忆）保存用户明确表达的长期偏好",
    "写入公司知识层",
    _STATUS,
    "不构成长期偏好",
    "简短查询直接回答",
    "需要保存交付物时创建任务目录",
    "报告工具实际返回的价格时间和交易时段",
)


@pytest.mark.parametrize("guidance", ["lean", "detailed"])
@pytest.mark.parametrize("role", ["analyst", "chief_of_staff"])
def test_merged_rules_render_in_both_modes(guidance, role):
    prompt = init_loader().get_system_prompt(
        **_BASE,
        role=role,
        **guidance_template_vars(guidance),
    )
    for phrase in _BOTH_MODES:
        assert phrase in prompt, phrase
    assert "accepts an unusual choice without pushback" not in prompt
    assert "Every task gets a directory" not in prompt
    assert "Add regulatory analysis" not in prompt


def test_task_tool_uses_the_same_status_sentence():
    assert _STATUS in TASK_TOOL_DESCRIPTION
    assert "completed or stopped task" not in TASK_TOOL_DESCRIPTION
    assert "iterate on a completed task" not in TASK_TOOL_DESCRIPTION
