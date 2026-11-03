#!/usr/bin/env python3
"""Offline behavioral regressions for the agenda and the single-track memo."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from research_structure import (
    SECTIONS,
    QUESTIONS,
    is_legacy_report,
    make_agenda,
    validate_agenda,
    validate_legacy_report_structure,
    validate_report_structure,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
DASH_SAMPLE = REPO_ROOT / ".agy" / "DASH_memo_sample.md"


def specimen(merged=False):
    parts = ["# 结构验收用虚拟公司"]
    body = list(SECTIONS)
    if merged:
        body.remove("行业与竞争")
    for s in body:
        parts.append(f"## {s}")
        if s == "摘要":
            parts.append("当前观察，等待续费兑现。 [引用1]")
        elif s == "公司与商业模式" and merged:
            parts.append("### 行业与竞争")
            parts.append("行业结构在此并入。 [引用2]")
        else:
            parts.append("本章提供论证。")
    parts += ["## 附录", "计算明细。", "## 引用", "[引用1]: f:F1 （底表｜锚点）", "[引用2]: f:F2 （底表｜锚点）"]
    return "\n\n".join(parts) + "\n"


LEGACY_CHAPTERS = (
    "公司与业务", "业务质量", "行业与竞争", "核心投资逻辑", "风险与反方证据",
    "风险收益", "管理层与股权", "市场预期", "估值", "后续跟踪",
)
CN_NUM = ("零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十")


def legacy_specimen():
    """一份结构完整的旧版双轨稿（第零节 12 问 + 十章），供旧规则校验。"""
    parts = ["# 旧结构验收用虚拟公司", "## 基本信息", "锚点。", "## 核心判断", "当前观察。"]
    parts.append("## 零、12 个核心问题与投资判断")
    for i in range(1, 13):
        parts += [
            f"### Q{i}. 第 {i} 问？",
            f"本题判断成立，证据见对应章节。 [引用{i}]",
            f"来源：一手 —— [引用{i}]",
            f"详见：第{min(i, 10)}章",
        ]
    for i, title in enumerate(LEGACY_CHAPTERS, 1):
        parts += [f"## {CN_NUM[i]}、{title}", "本章提供论证。 [引用1]"]
    parts += ["## 附录", "明细。", "## 引用"] + [f"[引用{i}]: f:k{i} （底表｜锚点）" for i in range(1, 13)]
    return "\n\n".join(parts) + "\n"


class ReportCases(unittest.TestCase):
    def setUp(self):
        self.text = specimen()

    def test_valid_single_track_report(self):
        self.assertEqual(validate_report_structure(self.text), [])

    def test_merged_industry_section_via_subheading(self):
        self.assertEqual(validate_report_structure(specimen(merged=True)), [])

    def test_missing_section_fails(self):
        changed = self.text.replace("## 风险\n\n本章提供论证。\n\n", "")
        self.assertTrue(any("风险" in x and "唯一" in x for x in validate_report_structure(changed)))

    def test_dropped_merge_marker_fails(self):
        merged = specimen(merged=True).replace("### 行业与竞争\n\n行业结构在此并入。 [引用2]\n\n", "")
        self.assertTrue(
            any("行业与竞争" in x and "三级标题" in x for x in validate_report_structure(merged))
        )

    def test_unknown_h2_section_rejected(self):
        changed = self.text.replace("## 附录", "## 结语\n\n一段。\n\n## 附录")
        self.assertTrue(any("结语" in x for x in validate_report_structure(changed)))

    def test_reordered_sections_rejected(self):
        head_risk = "## 风险\n\n本章提供论证。"
        head_mgmt = "## 管理层\n\n本章提供论证。"
        changed = self.text.replace(head_risk + "\n\n" + head_mgmt, head_mgmt + "\n\n" + head_risk)
        self.assertTrue(any("顺序" in x for x in validate_report_structure(changed)))

    def test_duplicate_industry_section_fails(self):
        changed = self.text.replace(
            "## 行业与竞争", "## 行业与竞争\n\n独立成节。\n\n## 行业与竞争", 1
        )
        self.assertTrue(any("行业与竞争" in x and "唯一" in x for x in validate_report_structure(changed)))

    def test_missing_tail_sections_fail(self):
        changed = self.text.split("## 附录")[0]
        errors = validate_report_structure(changed)
        self.assertTrue(any("附录" in x and "唯一" in x for x in errors))
        self.assertTrue(any("引用" in x and "唯一" in x for x in errors))

    def test_fake_fenced_sections_not_counted(self):
        fence = chr(96) * 3
        changed = self.text.replace(
            "## 摘要\n\n当前观察，等待续费兑现。 [引用1]",
            fence + "\n## 摘要\n\n当前观察，等待续费兑现。 [引用1]\n" + fence,
        )
        self.assertTrue(any("摘要" in x and "唯一" in x for x in validate_report_structure(changed)))


class LegacyReportCases(unittest.TestCase):
    """在途旧结构运行按旧规则校验（第 2 轮 #3）：不报新结构错误。"""

    def setUp(self):
        self.text = legacy_specimen()

    def test_legacy_detected_and_valid(self):
        self.assertTrue(is_legacy_report(self.text))
        self.assertFalse(is_legacy_report(specimen()))
        self.assertEqual(validate_legacy_report_structure(self.text), [])
        # 派发入口对新旧稿各走各的规则：旧稿不产生新结构报错。
        self.assertEqual(validate_report_structure(self.text), [])

    def test_legacy_missing_question_fails_old_rules(self):
        changed = self.text.replace("### Q4. 第 4 问？", "### X4. 第 4 问？")
        self.assertTrue(any("Q1–Q12" in x for x in validate_report_structure(changed)))

    def test_legacy_part_validation_scopes_chapters(self):
        errors = validate_legacy_report_structure(self.text, max_chapter=2)
        self.assertEqual(errors, [])

    def test_numbered_new_headings_are_not_legacy(self):
        """新稿给二级标题加中文序号（## 一、摘要）不判成旧稿（第 3 轮 #5）：
        按新规则校验，报的是可修的节名错误，不是一整套旧结构错误。"""
        numbered = ["# 序号版新稿"]
        for i, s in enumerate(SECTIONS, 1):
            numbered += [f"## {CN_NUM[i]}、{s}", "本章提供论证。"]
        numbered += ["## 附录", "明细。", "## 引用", "[引用1]: f:k （底表）"]
        text = "\n\n".join(numbered) + "\n"
        self.assertFalse(is_legacy_report(text))
        errors = validate_report_structure(text)
        self.assertTrue(errors, "numbered headings must fail the new-structure check")
        self.assertTrue(any("一、摘要" in x for x in errors))
        self.assertFalse(any("Q1–Q12" in x or "第零节" in x for x in errors))

    def test_single_numbered_heading_is_not_legacy(self):
        """只有一个「N、」式标题（如 ## 一、摘要）同样不触发旧稿判定。"""
        text = specimen().replace("## 摘要", "## 一、摘要")
        self.assertFalse(is_legacy_report(text))

    def test_three_old_chapter_names_still_detect_legacy(self):
        """没有第零节、但有多个旧章名的残稿仍按旧稿识别（≥3 个旧章名）。"""
        partial = (
            "# 旧残稿\n\n## 一、公司与业务\n\n内容。\n\n"
            "## 二、业务质量\n\n内容。\n\n## 四、核心投资逻辑\n\n内容。\n"
        )
        self.assertTrue(is_legacy_report(partial))


class DashSampleRegression(unittest.TestCase):
    def test_dash_sample_validates_under_legacy_rules(self):
        """真实旧稿（DASH 完整覆盖样本）：旧规则下结构零错误（第 2 轮 #3 的验收）。"""
        if not DASH_SAMPLE.exists():
            self.skipTest("样本不在本检出（.agy/DASH_memo_sample.md）")
        text = DASH_SAMPLE.read_text(encoding="utf-8")
        self.assertTrue(is_legacy_report(text))
        errors = validate_report_structure(text)
        self.assertEqual(errors, [])


class AgendaCases(unittest.TestCase):
    def test_generated_scaffold_requires_research(self):
        self.assertTrue(validate_agenda(make_agenda("虚拟公司", "TEST")))

    def test_completed_agenda_has_all_questions(self):
        agenda = make_agenda("虚拟公司", "TEST").replace("待填写", "测试材料中的续费与投入证据")
        self.assertEqual(validate_agenda(agenda), [])

    def test_scaffold_sections_are_real_memo_sections(self):
        for _, _, sections in QUESTIONS:
            for s in sections:
                self.assertIn(s, SECTIONS)

    def test_legacy_chapter_number_targets_still_accepted(self):
        """在途旧运行的 questions.md 写的是「第N章」：继续有效（旧规则收尾）。"""
        agenda = make_agenda("虚拟公司", "TEST").replace("待填写", "已读测试证据")
        agenda = agenda.replace("论证章节：公司与商业模式", "论证章节：第1章、第2章", 1)
        self.assertEqual(validate_agenda(agenda), [])

    def test_unknown_section_target_rejected(self):
        agenda = make_agenda("虚拟公司", "TEST").replace("待填写", "已读测试证据")
        agenda = agenda.replace("论证章节：公司与商业模式", "论证章节：结论、附录、感想", 1)
        self.assertTrue(any("Q1" in x and "节名" in x for x in validate_agenda(agenda)))

    def test_missing_company_specific_question(self):
        agenda = make_agenda("虚拟公司", "TEST").replace("待填写", "已读测试证据")
        agenda = agenda.replace("公司问题：已读测试证据", "公司问题：", 1)
        self.assertTrue(validate_agenda(agenda))


if __name__ == "__main__":
    unittest.main(verbosity=2)
