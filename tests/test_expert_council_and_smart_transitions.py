"""Tests for Expert Council and Smart Model-Driven Transitions."""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "frontend"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "services", "genslide-agentscope", "src"))

from expert_council import get_expert, list_all_experts, ExpertProfile, CORE_EXPERTS
from autonomous_agent import (
    AutonomousAgent,
    AutonomousResult,
    route_professional_intent,
)


class TestExpertCouncil(unittest.TestCase):
    def test_core_experts_exist(self):
        experts = list_all_experts()
        self.assertGreaterEqual(len(experts), 5)
        expert_ids = [e.id for e in experts]
        self.assertIn("official-document-skill", expert_ids)
        self.assertIn("business-report", expert_ids)
        self.assertIn("presentation", expert_ids)
        self.assertIn("document", expert_ids)
        self.assertIn("writing", expert_ids)

    def test_expert_details(self):
        doc_expert = get_expert("official-document-skill")
        self.assertEqual(doc_expert.name, "机关公文资深参赞")
        self.assertEqual(doc_expert.target_kind, "document")
        self.assertIn("红头规范", doc_expert.tags)
        self.assertTrue(doc_expert.avatar.startswith("🏛️"))

        ppt_expert = get_expert("presentation")
        self.assertEqual(ppt_expert.name, "演示胶片视觉架构师")
        self.assertEqual(ppt_expert.target_kind, "presentation")

        biz_expert = get_expert("business-report")
        self.assertEqual(biz_expert.name, "商业咨询战略总监")
        self.assertEqual(biz_expert.target_kind, "writing")

    def test_dynamic_fallback_for_custom_skill(self):
        unknown = get_expert("custom-deep-seek-skill")
        self.assertEqual(unknown.id, "custom-deep-seek-skill")
        self.assertIn("custom-deep-seek-skill", unknown.name)
        self.assertIsNone(unknown.target_kind)

    def test_list_all_experts_with_dynamic_catalog(self):
        catalog = [
            {
                "skill_id": "finance-audit",
                "name": "财务审计专家",
                "description": "专用于企业财报与合规审计",
                "target_kind": "document",
            }
        ]
        experts = list_all_experts(catalog)
        expert_ids = [e.id for e in experts]
        self.assertIn("finance-audit", expert_ids)
        fa_expert = next(e for e in experts if e.id == "finance-audit")
        self.assertEqual(fa_expert.target_kind, "document")


class TestSmartModelDrivenTransitions(unittest.TestCase):
    def test_route_professional_intent_heuristics(self):
        # Fallback heuristic tests when LLM fails or raises
        with patch.object(AutonomousAgent, "_call_llm", side_effect=RuntimeError("LLM unavailable")):
            # 1. clarify
            res_clarify = route_professional_intent("有什么注意规范吗？", stage="clarify", has_draft=False)
            self.assertEqual(res_clarify["operation"], "clarify")

            # 2. create outline
            res_create = route_professional_intent("帮我生成大纲框架", stage="clarify", has_draft=False)
            self.assertEqual(res_create["operation"], "create_outline")

            # 3. revise outline
            res_revise = route_professional_intent("把第二部分改成需求剖析，增加一段", stage="outline", has_draft=True, is_confirmed=False)
            self.assertEqual(res_revise["operation"], "revise_outline")

            # 4. explain outline
            res_explain = route_professional_intent("为什么要这样设计大纲编排？", stage="outline", has_draft=True, is_confirmed=False)
            self.assertEqual(res_explain["operation"], "explain_outline")

            # 5. confirm outline
            res_confirm = route_professional_intent("大纲没问题了，确认通过", stage="outline", has_draft=True, is_confirmed=False)
            self.assertEqual(res_confirm["operation"], "confirm_outline")

            # 6. generate (when outline is already confirmed)
            res_gen = route_professional_intent("立即开始写吧", stage="outline", has_draft=True, is_confirmed=True)
            self.assertEqual(res_gen["operation"], "generate")

    def test_route_professional_intent_llm_success(self):
        mock_reply = '{"operation": "confirm_outline", "reasoning": "用户认可当前大纲结构", "clean_message": "", "override_target": null}'
        with patch.object(AutonomousAgent, "_call_llm", return_value=mock_reply):
            res = route_professional_intent("大纲看起来相当扎实，赞成", stage="outline", has_draft=True, is_confirmed=False)
            self.assertEqual(res["operation"], "confirm_outline")
            self.assertIn("用户认可", res["reasoning"])


class TestFollowUpQuestionsInAutonomousAgent(unittest.TestCase):
    @patch.object(AutonomousAgent, "_call_llm")
    def test_reply_extracts_3_to_5_follow_up_questions(self, mock_call):
        mock_call.return_value = '''{
            "thought": "Clarifying requirements with 3-5 structured options",
            "skill": "reply",
            "reply_text": "好的，为了帮您写出最地道的战略报告，请先确定以下几个关键要点：",
            "follow_up_questions": [
                {
                    "question": "1. 报告的主要面向对象是？",
                    "options": ["集团高管 / 董事会汇报", "业务线执行层与技术骨干", "外部投资人与监管机构"]
                },
                {
                    "question": "2. 期望的篇幅与深度？",
                    "options": ["标准方案架构（约 3000-5000 字）", "速决型决策摘要（约 1500 字）", "详实完整白皮书（8000+ 字）"]
                },
                {
                    "question": "3. 重点突出的核心模块？",
                    "options": ["市场竞争格局与商业闭环", "技术架构蓝图与演进路线", "财务测算与落地风控策略"]
                }
            ]
        }'''
        agent = AutonomousAgent()
        res = agent.step("我想写一份企业数字化转型战略报告")
        self.assertEqual(res.skill, "reply")
        self.assertIsNotNone(res.follow_up_questions)
        self.assertEqual(len(res.follow_up_questions), 3)
        self.assertEqual(res.follow_up_questions[0]["question"], "1. 报告的主要面向对象是？")
        self.assertEqual(len(res.follow_up_questions[0]["options"]), 3)


if __name__ == "__main__":
    unittest.main()
