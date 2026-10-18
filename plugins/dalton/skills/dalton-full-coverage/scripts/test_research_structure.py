#!/usr/bin/env python3
"""Offline behavioral regressions for front-loaded 12Q reports."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from research_structure import QUESTIONS, CHAPTERS, CN_NUM, make_agenda, validate_agenda, validate_report_structure


def specimen():
    parts = ["# 结构验收用虚拟公司", "## 核心判断", "当前观察，等待续费兑现。", "## 零、12 个核心问题与投资判断"]
    for i, (title, branches, chapters) in enumerate(QUESTIONS, 1):
        parts += [f"### Q{i}. {title}", f"本题判断取决于续费与获客投入的传导，证据和反方详见对应章节。 [C-{i}]",
                  f"来源：一手 —— [C-{i}]", "详见：" + "、".join(f"第{n}章" for n in chapters)]
    for i, title in enumerate(CHAPTERS, 1):
        parts += [f"## {CN_NUM[i]}、{title}", "本章提供论证。"]
    return "\n\n".join(parts) + "\n"


class ReportCases(unittest.TestCase):
    def setUp(self):
        self.text = specimen()

    def test_valid_frontloaded_report(self):
        self.assertEqual(validate_report_structure(self.text), [])

    def test_appendix_regression(self):
        a = self.text.index("## 零、")
        b = self.text.index("## 一、")
        moved = self.text[:a] + self.text[b:] + self.text[a:b]
        self.assertTrue(validate_report_structure(moved))

    def test_missing_and_duplicate_question(self):
        for changed in (self.text.replace("### Q4.", "### Q3."), self.text.replace("### Q4.", "### X4.")):
            with self.subTest(changed=changed[:20]):
                self.assertTrue(validate_report_structure(changed))

    def test_last_question_cannot_borrow_next_chapter_source(self):
        changed = self.text.replace("来源：一手 —— [C-12]", "")
        changed = changed.replace("## 一、公司与业务", "## 一、公司与业务\n来源：一手 —— [C-12]")
        self.assertTrue(any("Q12 缺来源" in x for x in validate_report_structure(changed)))

    def test_empty_source_cannot_read_next_line(self):
        changed = self.text.replace("来源：一手 —— [C-1]", "来源：")
        self.assertTrue(any("Q1 缺来源" in x for x in validate_report_structure(changed)))

    def test_missing_chapter_target(self):
        changed = self.text.replace("详见：第1章、第2章", "详见：第99章", 1)
        self.assertTrue(any("Q1 指向不存在" in x for x in validate_report_structure(changed)))

    def test_hollow_answer_with_source_and_pointer(self):
        start = self.text.index("### Q1.")
        end = self.text.index("### Q2.")
        changed = self.text[:start] + "### Q1. 业务是什么？\n来源：一手 —— [C-1]\n详见：第1章\n" + self.text[end:]
        self.assertTrue(any("Q1 缺实质回答" in x for x in validate_report_structure(changed)))

    def test_fake_fenced_questions_not_counted(self):
        start = self.text.index("## 零、")
        end = self.text.index("## 一、")
        fence = chr(96) * 3
        changed = self.text[:start] + fence + "\n" + self.text[start:end] + fence + "\n" + self.text[end:]
        self.assertTrue(validate_report_structure(changed))

    def test_disclosed_material_gap_allowed(self):
        changed = self.text.replace("来源：一手 —— [C-1]", "来源：缺口 —— 需要分部利润表，已查年度报告未取得")
        self.assertEqual(validate_report_structure(changed), [])

    def test_reordered_detail_chapters_rejected(self):
        changed = self.text.replace("## 一、公司与业务", "## 二、业务质量", 1)
        self.assertTrue(validate_report_structure(changed))


class AgendaCases(unittest.TestCase):
    def test_generated_scaffold_requires_research(self):
        self.assertTrue(validate_agenda(make_agenda("虚拟公司", "TEST")))

    def test_completed_agenda_has_all_questions(self):
        agenda = make_agenda("虚拟公司", "TEST").replace("待填写", "测试材料中的续费与投入证据")
        self.assertEqual(validate_agenda(agenda), [])

    def test_missing_company_specific_question(self):
        agenda = make_agenda("虚拟公司", "TEST").replace("待填写", "已读测试证据")
        agenda = agenda.replace("公司问题：已读测试证据", "公司问题：", 1)
        self.assertTrue(validate_agenda(agenda))


if __name__ == "__main__":
    unittest.main(verbosity=2)
