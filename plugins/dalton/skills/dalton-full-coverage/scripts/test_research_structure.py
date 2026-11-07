#!/usr/bin/env python3
"""Offline behavioral regressions for the agenda and the single-track memo."""
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from research_structure import (
    SECTIONS,
    _is_valid_self_rating,
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
        elif s == "业务质量":
            parts += [
                "### Good Business：高转换成本与负营运资本驱动稳态高回报",
                "ROIC 显著高于 WACC，详见「公司与商业模式」。 [引用1]",
                "### Good People：创始人长期导向但指引兑现尚待观察",
                "详见「管理层」。 [引用1]",
                "### Good Price：现价已计入乐观增速预期",
                "详见「财务预测与估值」。 [引用2]",
                "### 综合判断",
                "| 维度 | 评级 | 一句理由 |\n"
                "|---|---|---|\n"
                "| Good Business | 强 | 负营运资本与高转换成本 [引用1] |\n"
                "| Good People | 中 | 创始人长期导向但指引兑现待观察 [引用1] |\n"
                "| Good Price | 偏贵 | 现价已计入乐观增速 [引用2] |",
                "好生意但价格偏贵，对应方向中性、等待买点。",
            ]
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

    def test_business_quality_missing_subheadings_fails(self):
        missing_people = self.text.replace(
            "### Good People：创始人长期导向但指引兑现尚待观察\n\n详见「管理层」。 [引用1]\n\n",
            "",
        )
        self.assertTrue(
            any("Good People" in x and "三级标题" in x for x in validate_report_structure(missing_people))
        )
        for bad_heading in (
            "### Good Business：{一句判断}",
            "### Good Business：强 / 中 / 弱",
            "### Good Business：**强 / 中 / 弱**",
            "### Good Business：强、中、弱选一",
            "### Good Business：强",
        ):
            bad_doc = self.text.replace(
                "### Good Business：高转换成本与负营运资本驱动稳态高回报",
                bad_heading,
            )
            self.assertTrue(
                any("Good Business" in x and "具体判断" in x for x in validate_report_structure(bad_doc)),
                msg=f"should reject heading: {bad_heading}",
            )
        bad_price_heading = self.text.replace(
            "### Good Price：现价已计入乐观增速预期",
            "### Good Price：便宜、合理、偏贵选一",
        )
        self.assertTrue(
            any("Good Price" in x and "具体判断" in x for x in validate_report_structure(bad_price_heading))
        )
        missing_synth = self.text.replace("### 综合判断", "### 小结")
        self.assertTrue(
            any("综合判断" in x and "三级标题" in x for x in validate_report_structure(missing_synth))
        )

    def test_business_quality_duplicate_subheadings_rejected(self):
        dup_gb = self.text.replace(
            "### Good Business：高转换成本与负营运资本驱动稳态高回报",
            "### Good Business：高转换成本与负营运资本驱动稳态高回报\n\n一段。\n\n### Good Business：低壁垒导致回报偏低",
        )
        self.assertTrue(
            any("Good Business" in x and "重复" in x for x in validate_report_structure(dup_gb))
        )
        dup_synth = self.text.replace(
            "### 综合判断",
            "### 综合判断\n\n一段。\n\n### 综合判断",
        )
        self.assertTrue(
            any("综合判断" in x and "重复" in x for x in validate_report_structure(dup_synth))
        )

    def test_business_quality_duplicate_rating_table_or_row_rejected(self):
        second_table = (
            "| 维度 | 评级 | 一句理由 |\n"
            "|---|---|---|\n"
            "| Good Business | 弱 | 第二套冲突评级 |\n"
            "| Good People | 中 | 理由 |\n"
            "| Good Price | 便宜 | 第二套冲突评级 |"
        )
        two_tables = self.text.replace(
            "好生意但价格偏贵，对应方向中性、等待买点。",
            "好生意但价格偏贵，对应方向中性、等待买点。\n\n" + second_table,
        )
        errors_two_tables = validate_report_structure(two_tables)
        self.assertTrue(any("评级表重复" in x for x in errors_two_tables))
        self.assertTrue(any("Good Business" in x and "重复" in x for x in errors_two_tables))

        dup_row = self.text.replace(
            "| Good Business | 强 | 负营运资本与高转换成本 [引用1] |",
            "| Good Business | 强 | 负营运资本与高转换成本 [引用1] |\n"
            "| Good Business | 弱 | 重复的维度行 |",
        )
        self.assertTrue(
            any("Good Business" in x and "重复" in x for x in validate_report_structure(dup_row))
        )

    def test_business_quality_table_without_outer_pipes_accepted(self):
        no_outer_pipes = self.text.replace(
            "| 维度 | 评级 | 一句理由 |\n"
            "|---|---|---|\n"
            "| Good Business | 强 | 负营运资本与高转换成本 [引用1] |\n"
            "| Good People | 中 | 创始人长期导向但指引兑现待观察 [引用1] |\n"
            "| Good Price | 偏贵 | 现价已计入乐观增速 [引用2] |",
            "维度 | 评级 | 一句理由\n"
            "---|---|---\n"
            "Good Business | 强 | 负营运资本与高转换成本 [引用1]\n"
            "Good People | 中 | 创始人长期导向但指引兑现待观察 [引用1]\n"
            "Good Price | 偏贵 | 现价已计入乐观增速 [引用2]",
        )
        self.assertEqual(validate_report_structure(no_outer_pipes), [])
        bad_no_outer = no_outer_pipes.replace("Good Price | 偏贵 |", "Good Price | 不便宜 |")
        self.assertTrue(
            any("Good Price" in x for x in validate_report_structure(bad_no_outer))
        )

    def test_business_quality_prose_words_not_accepted_as_ratings(self):
        prose_only = self.text.replace(
            "| 维度 | 评级 | 一句理由 |\n"
            "|---|---|---|\n"
            "| Good Business | 强 | 负营运资本与高转换成本 [引用1] |\n"
            "| Good People | 中 | 创始人长期导向但指引兑现待观察 [引用1] |\n"
            "| Good Price | 偏贵 | 现价已计入乐观增速 [引用2] |",
            "Good Business：护城河很强；Good People：行业集中；Good Price：不便宜。",
        )
        errors_prose = validate_report_structure(prose_only)
        self.assertTrue(any("评级表" in x for x in errors_prose))

        sentence_in_cells = (
            self.text.replace("| Good Business | 强 |", "| Good Business | 护城河很强 |")
            .replace("| Good People | 中 |", "| Good People | 行业集中 |")
            .replace("| Good Price | 偏贵 |", "| Good Price | 不便宜 |")
        )
        errors_cells = validate_report_structure(sentence_in_cells)
        self.assertTrue(any("Good Business" in x for x in errors_cells))
        self.assertTrue(any("Good People" in x for x in errors_cells))
        self.assertTrue(any("Good Price" in x for x in errors_cells))

    def test_business_quality_missing_ratings_in_synthesis_fails(self):
        two_words = self.text.replace("| Good Business | 强 |", "| Good Business | 强或弱 |")
        self.assertTrue(
            any("Good Business" in x for x in validate_report_structure(two_words))
        )
        menu_cell = self.text.replace("| Good People | 中 |", "| Good People | 强 / 中 / 弱 |")
        self.assertTrue(
            any("Good People" in x for x in validate_report_structure(menu_cell))
        )
        placeholder_cell = self.text.replace(
            "| Good Price | 偏贵 |", "| Good Price | {便宜、合理、偏贵选一} |"
        )
        self.assertTrue(
            any("Good Price" in x for x in validate_report_structure(placeholder_cell))
        )
        missing_row = self.text.replace(
            "| Good Price | 偏贵 | 现价已计入乐观增速 [引用2] |\n", ""
        )
        self.assertTrue(
            any("Good Price" in x and "缺" in x for x in validate_report_structure(missing_row))
        )


class SynthesisTableCases(unittest.TestCase):
    """「综合判断」只放一张评级表：其他表格不参与计数，也不会让校验崩溃。"""

    def test_extra_table_with_missing_rating_row_reports_errors(self):
        doc = re.sub(r"^\| Good Business \|.*\n", "", specimen(), count=1, flags=re.M)
        doc = doc.replace(
            "### 综合判断\n",
            "### 综合判断\n\n| 项目 | 说明 |\n|---|---|\n| Good Business | 业务质量说明 |\n\n",
            1,
        )
        errors = validate_report_structure(doc)
        self.assertTrue(any("只放一张评级表" in e for e in errors))
        self.assertTrue(any("缺“Good Business”行" in e for e in errors))

    def test_extra_table_listing_a_dimension_in_another_column_is_rejected(self):
        doc = specimen().replace(
            "### 综合判断\n",
            "### 综合判断\n\n序号 | 维度 | 结论\n---|---|---\n1 | Good Business | 弱\n\n",
            1,
        )
        self.assertTrue(any("只放一张评级表" in e for e in validate_report_structure(doc)))


    def test_single_column_extra_table_is_rejected(self):
        doc = specimen().replace(
            "### 综合判断\n",
            "### 综合判断\n\n| 备注 |\n| --- |\n| Good Business：弱 |\n\n",
            1,
        )
        self.assertTrue(any("只放一张评级表" in e for e in validate_report_structure(doc)))


class SelfRatingParseCases(unittest.TestCase):
    def test_grade_followed_by_reason_is_accepted(self):
        for value in ("A B 支线未回", "A，B 支线未回", "A（B 支线未回）", "B（DCF 与可比估值二选一，缺少交叉验证）", "D"):
            self.assertTrue(_is_valid_self_rating(value), value)

    def test_multiple_grades_or_menus_are_rejected(self):
        for value in ("A B", "AB", "B-C", "A B C D", "A / B / C / D", "待填写（A / B / C / D）", "A、B、C、D 选一", "A、B、C、D（选一）", "A、B、C、D 四选一"):
            self.assertFalse(_is_valid_self_rating(value), value)


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

    def test_unnumbered_business_quality_heading_not_counted_as_legacy(self):
        """新版不带中文序号的「## 业务质量」「## 行业与竞争」不计入旧版标记。"""
        unnumbered_shared = (
            "# 混合稿\n\n## 一、公司与业务\n\n内容。\n\n"
            "## 业务质量\n\n内容。\n\n## 行业与竞争\n\n内容。\n\n"
            "## 四、核心投资逻辑\n\n内容。\n"
        )
        self.assertFalse(is_legacy_report(unnumbered_shared))


class DashSampleRegression(unittest.TestCase):
    def test_dash_sample_validates_under_legacy_rules(self):
        """真实旧稿（DASH 完整覆盖样本）：旧规则下结构零错误（第 2 轮 #3 的验收）。"""
        if not DASH_SAMPLE.exists():
            self.skipTest("样本不在本检出（.agy/DASH_memo_sample.md）")
        text = DASH_SAMPLE.read_text(encoding="utf-8")
        self.assertTrue(is_legacy_report(text))
        errors = validate_report_structure(text)
        self.assertEqual(errors, [])


def _filled_agenda(name="虚拟公司", ticker="TEST", fill="已读测试证据"):
    return (
        make_agenda(name, ticker)
        .replace("自评：待填写（A / B / C / D）", "自评：B（已有初步证据支撑）")
        .replace("待填写", fill)
    )


class AgendaCases(unittest.TestCase):
    def test_generated_scaffold_requires_research(self):
        scaffold = make_agenda("虚拟公司", "TEST")
        self.assertIn("自评：待填写（A / B / C / D）", scaffold)
        self.assertTrue(validate_agenda(scaffold))
        blind_replace = scaffold.replace("待填写", "测试材料中的续费与投入证据")
        self.assertTrue(any("Q1" in x and "自评" in x for x in validate_agenda(blind_replace)))

    def test_completed_agenda_has_all_questions(self):
        agenda = _filled_agenda(fill="测试材料中的续费与投入证据")
        self.assertEqual(validate_agenda(agenda), [])

    def test_scaffold_sections_are_real_memo_sections(self):
        for _, _, sections in QUESTIONS:
            for s in sections:
                self.assertIn(s, SECTIONS)

    def test_legacy_chapter_number_targets_still_accepted(self):
        """在途旧运行的 questions.md 写的是「第N章」：继续有效（旧规则收尾）。"""
        agenda = _filled_agenda()
        agenda = agenda.replace("论证章节：公司与商业模式", "论证章节：第1章、第2章", 1)
        self.assertEqual(validate_agenda(agenda), [])

    def test_unknown_section_target_rejected(self):
        agenda = _filled_agenda()
        agenda = agenda.replace("论证章节：公司与商业模式", "论证章节：结论、附录、感想", 1)
        self.assertTrue(any("Q1" in x and "节名" in x for x in validate_agenda(agenda)))

    def test_missing_company_specific_question(self):
        agenda = _filled_agenda()
        agenda = agenda.replace("公司问题：已读测试证据", "公司问题：", 1)
        self.assertTrue(validate_agenda(agenda))

    def test_self_rating_grades_accepted(self):
        agenda = _filled_agenda()
        agenda = agenda.replace("自评：B（", "自评：A（证据充分：", 1)
        agenda = agenda.replace("自评：B（", "自评：C（主要靠推演：", 1)
        agenda = agenda.replace("自评：B（", "自评：D（关键证据缺失：", 1)
        self.assertEqual(validate_agenda(agenda), [])
        for valid_val in (
            "B，DCF 对利率敏感，估值仍有次要缺口",
            "A，B 支线未回",
            "A（B 支线未回）",
        ):
            doc = re.sub(r"^自评：.*$", f"自评：{valid_val}", _filled_agenda(), count=1, flags=re.M)
            self.assertEqual(validate_agenda(doc), [], msg=f"should accept self rating: {valid_val}")

    def test_missing_self_rating_line_warns_without_error(self):
        agenda = re.sub(r"^自评：.*\n", "", _filled_agenda(), flags=re.M)
        warns = []
        self.assertEqual(validate_agenda(agenda, warnings=warns), [])
        self.assertEqual(len(warns), 12)
        self.assertTrue(any("Q1" in w and "自评" in w for w in warns))

    def test_missing_or_invalid_self_rating_rejected(self):
        agenda = _filled_agenda()
        for bad_val in (
            "",
            "待填写",
            "待填写（A / B / C / D）",
            "B-C",
            "BC",
            "A / B / C / D",
            "A B C D",
        ):
            doc = re.sub(r"^自评：.*$", f"自评：{bad_val}", agenda, count=1, flags=re.M)
            self.assertTrue(
                any("Q1" in x and "A / B / C / D" in x for x in validate_agenda(doc)),
                msg=f"should reject self rating: {bad_val!r}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
