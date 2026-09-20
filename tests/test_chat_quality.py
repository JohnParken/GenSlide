"""Offline regression tests for conversational context and autonomous writing."""
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "frontend"))
sys.path.insert(0, os.path.join(_ROOT, "services", "genslide-agentscope", "src"))

from autonomous_agent import AutonomousAgent
from chat_context import (prepare_history, prepare_materials, relevant_materials,
                          updated_requirements, RequirementUpdate)


class TestChatContextQuality(unittest.IsolatedAsyncioTestCase):
    async def test_history_compaction_keeps_early_user_constraint(self):
        history = [{"role": "user", "text": "早期用户约束：必须中文，面向董事会。" + "x" * 2500}]
        history += [{"role": "assistant", "text": "已记录。" + "y" * 2500} for _ in range(7)]
        complete = AsyncMock(return_value={"summary": "早期用户约束：必须中文，面向董事会。"})
        memory = {}
        result = await prepare_history(history, memory, complete)
        self.assertIn("必须中文", result)
        self.assertGreaterEqual(complete.await_count, 1)
        first_payload = json.loads(complete.await_args_list[0].args[1])
        self.assertIn("必须中文", first_payload["messages"][0]["text"])

    def test_requirements_need_verbatim_user_evidence_and_are_session_local(self):
        first = {"requirements": {"topic": "旧主题"}}
        updates = {"topic": RequirementUpdate(value="新主题", evidence="助手建议的新主题")}
        self.assertEqual(updated_requirements(first, updates, "我只想讨论别的事情"), {"topic": "旧主题"})
        second = {"requirements": {}}
        valid = {"topic": RequirementUpdate(value="新主题", evidence="主题是新主题")}
        self.assertEqual(updated_requirements(second, valid, "主题是新主题"), {"topic": "新主题"})
        self.assertEqual(first["requirements"], {"topic": "旧主题"})

    async def test_long_material_keeps_late_source_evidence(self):
        text = "A" * 9995 + " EARLY_SOURCE\n" + "B" * 9994 + " MIDDLE_SOURCE\n" + "C" * 9994 + " LATE_SOURCE"

        async def excerpt(prompt, payload):
            data = json.loads(payload)
            return {"excerpts": [data["text"][-30:]]}

        memory = {}
        material = await prepare_materials(text, memory, excerpt)
        selected = relevant_materials(material, "LATE_SOURCE")
        self.assertIn("LATE_SOURCE", selected)
        self.assertIn("材料段3", selected)


class TestAutonomousQuality(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_json_repairs_once(self):
        agent = AutonomousAgent()
        agent._call_llm = AsyncMock(side_effect=["not json", '{"ok": true}'])
        result = await agent._json_complete("system", "prompt")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(agent._call_llm.await_count, 2)

    async def test_revision_uses_requested_source_and_preserves_other_sections(self):
        decision = {"skill": "revise", "reply_text": "", "section_ids": ["sec-2"]}
        replacement = {"sections": [{"title": "第二章", "body": "修订后的第二章正文", "notes": ""}]}
        calls = AsyncMock(side_effect=[json.dumps(decision, ensure_ascii=False), json.dumps(replacement, ensure_ascii=False)])
        current = {"title": "主题", "sections": [
            {"title": "第一章", "body": "第一章原始正文", "notes": "保留"},
            {"title": "第二章", "body": "第二章原始正文", "notes": "旧备注"},
            {"title": "第三章", "body": "第三章原始正文", "notes": "保留"},
        ]}
        with patch.object(AutonomousAgent, "_call_llm", calls):
            result = await AutonomousAgent().astep("修改第二章", current_content=current)
        self.assertEqual(result.content["sections"][0], current["sections"][0])
        self.assertEqual(result.content["sections"][2], current["sections"][2])
        self.assertEqual(result.content["sections"][1]["body"], "修订后的第二章正文")
        self.assertIn("第二章原始正文", calls.await_args_list[1].args[1])
        self.assertNotIn("第一章原始正文", calls.await_args_list[1].args[1])

    async def test_revision_rejects_missing_or_unknown_scope(self):
        current = {"title": "主题", "sections": [{"title": "第一章", "body": "正文", "notes": ""}]}
        for section_ids in ([], ["missing"]):
            decision = {"skill": "revise", "section_ids": section_ids}
            with patch.object(AutonomousAgent, "_call_llm", new=AsyncMock(return_value=json.dumps(decision))):
                with self.assertRaises(ValueError):
                    await AutonomousAgent().astep("修改章节", current_content=current)

    async def test_greeting_does_not_add_followup_questions(self):
        response = {"skill": "reply", "reply_text": "你好！", "follow_up_questions": [
            {"question": "请问主题是什么？", "field": "topic", "options": ["A", "B"]}
        ]}
        with patch.object(AutonomousAgent, "_call_llm", new=AsyncMock(return_value=json.dumps(response, ensure_ascii=False))):
            result = await AutonomousAgent().astep("你好")
        self.assertEqual(result.follow_up_questions, [])

    async def test_generate_batches_titles_and_writing_renders_md(self):
        nodes = [{"node_id": f"sec-{i}", "title": f"第{i}章"} for i in range(1, 6)]
        decision = {"skill": "generate", "outline": {"title": "纯文本计划", "nodes": nodes}}
        batches = [
            {"sections": [{"title": f"第{i}章", "body": f"第{i}章正文", "notes": ""} for i in range(1, 5)]},
            {"sections": [{"title": "第5章", "body": "第五章正文", "notes": ""}]},
        ]
        calls = AsyncMock(side_effect=[json.dumps(decision, ensure_ascii=False), *[json.dumps(item, ensure_ascii=False) for item in batches]])
        with patch.object(AutonomousAgent, "_call_llm", calls):
            result = await AutonomousAgent().astep("请直接生成纯文本正文")
        self.assertEqual([s["title"] for s in result.content["sections"]], [f"第{i}章" for i in range(1, 6)])
        self.assertTrue(result.rendered_file["name"].endswith(".md"))
        self.assertIn("第1章", calls.await_args_list[1].args[1])
        self.assertIn("第5章", calls.await_args_list[2].args[1])
        self.assertEqual(result.active_skill_id, "writing")


if __name__ == "__main__":
    unittest.main()
