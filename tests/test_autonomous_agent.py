"""Tests for AutonomousAgent in frontend/autonomous_agent.py."""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "frontend"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "backend"))

from autonomous_agent import AutonomousAgent, AutonomousResult, _extract_json, detect_target_kind


class TestAutonomousAgent(unittest.TestCase):
    def test_detect_target_kind_rules(self):
        # 1. 明确要求生成文档 / 报告 / 方案 -> document
        self.assertEqual(detect_target_kind("帮我生成文档"), "document")
        self.assertEqual(detect_target_kind("帮我生成一份微服务架构演进文档"), "document")
        self.assertEqual(detect_target_kind("写一份关于大模型技术的总结报告"), "document")
        self.assertEqual(detect_target_kind("撰写一篇云原生落地方案"), "document")

        # 2. 明确排除 PPT -> document
        self.assertEqual(detect_target_kind("生成文档，不要做成PPT"), "document")
        self.assertEqual(detect_target_kind("帮我整理成Word，不是PPT"), "document")
        self.assertEqual(detect_target_kind("不生成幻灯片，只要文档"), "document")

        # 3. 只有明确要求 PPT 时才生成 PPT
        self.assertEqual(detect_target_kind("帮我生成一份PPT"), "presentation")
        self.assertEqual(detect_target_kind("制作一份关于人工智能的演示文稿"), "presentation")
        self.assertEqual(detect_target_kind("把这份文档做成PPT"), "presentation")
        self.assertEqual(detect_target_kind("做一份6页胶片"), "presentation")
        self.assertEqual(detect_target_kind("生成8页slides"), "presentation")

        # 4. 未明确指明格式，默认必须是 document
        self.assertEqual(detect_target_kind("直接生成"), "document")
        self.assertEqual(detect_target_kind("请帮我整理这份材料"), "document")

        # 5. 纯文本
        self.assertEqual(detect_target_kind("只输出纯文本正文"), "writing")

    def test_extract_json_clean(self):
        sample = '{"thought": "ok", "skill": "reply", "reply_text": "hello"}'
        parsed = _extract_json(sample)
        self.assertEqual(parsed["skill"], "reply")
        self.assertEqual(parsed["reply_text"], "hello")

    def test_extract_json_markdown_block(self):
        sample = '```json\n{"thought": "planning", "skill": "outline", "outline": {"title": "Test", "nodes": []}}\n```'
        parsed = _extract_json(sample)
        self.assertEqual(parsed["skill"], "outline")
        self.assertEqual(parsed["outline"]["title"], "Test")

    def test_extract_json_embedded(self):
        sample = 'Here is the response:\n{"thought": "done", "skill": "reply", "reply_text": "Hi"}\nHope that helps!'
        parsed = _extract_json(sample)
        self.assertEqual(parsed["thought"], "done")

    @patch.object(AutonomousAgent, "_call_llm")
    def test_step_reply(self, mock_call):
        mock_call.return_value = '{"thought": "greeting", "skill": "reply", "reply_text": "你好！请问有什么可以帮你？"}'
        agent = AutonomousAgent()
        res = agent.step("你好")
        self.assertEqual(res.skill, "reply")
        self.assertEqual(res.reply_text, "你好！请问有什么可以帮你？")
        self.assertIsNone(res.outline)
        self.assertIsNone(res.content)

    @patch.object(AutonomousAgent, "_call_llm")
    def test_step_outline(self, mock_call):
        mock_call.return_value = '''{
            "thought": "user wants outline",
            "skill": "outline",
            "reply_text": "已为您制定大纲：",
            "outline": {
                "title": "微服务演进",
                "nodes": [
                    {"node_id": "sec-1", "title": "架构回顾"},
                    {"node_id": "sec-2", "title": "演进实践"}
                ]
            }
        }'''
        agent = AutonomousAgent()
        res = agent.step("帮我制定微服务演进大纲")
        self.assertEqual(res.skill, "outline")
        self.assertIsNotNone(res.outline)
        self.assertEqual(res.outline["title"], "微服务演进")
        self.assertEqual(len(res.outline["nodes"]), 2)

    @patch.object(AutonomousAgent, "_call_llm")
    def test_step_generate_document_when_user_asks_document(self, mock_call):
        """When user says '帮我生成文档', system must generate a .docx Word document, even if target_kind was presentation."""
        mock_call.return_value = '''{
            "thought": "user asks for document, generating docx",
            "skill": "generate",
            "target_kind": "document",
            "reply_text": "文档已生成完毕！",
            "outline": {
                "title": "系统架构设计文档",
                "nodes": [
                    {"node_id": "sec-1", "title": "第一章：项目背景"},
                    {"node_id": "sec-2", "title": "第二章：核心设计"}
                ]
            },
            "content": {
                "title": "系统架构设计文档",
                "summary": "架构设计核心摘要",
                "sections": [
                    {"title": "第一章：项目背景", "body": "这是项目背景详实的正文阐述段落，说明了系统建设的必要性与业务驱动力。", "notes": ""},
                    {"title": "第二章：核心设计", "body": "这是核心设计的技术选型与模块拆分说明。", "notes": ""}
                ]
            }
        }'''
        agent = AutonomousAgent()
        # Even if target_kind is accidentally passed as "presentation", saying "帮我生成文档" forces document
        res = agent.step("帮我生成文档", target_kind="presentation")
        self.assertEqual(res.skill, "generate")
        self.assertEqual(res.target_kind, "document")
        self.assertIsNotNone(res.rendered_file)
        self.assertTrue(res.rendered_file["name"].endswith(".docx"))
        self.assertGreater(len(res.rendered_file["data"]), 1000)

    @patch.object(AutonomousAgent, "_call_llm")
    def test_step_guard_against_llm_hallucinating_presentation(self, mock_call):
        """If user did not explicitly request PPT, but LLM outputs target_kind=presentation, system clamps to document."""
        mock_call.return_value = '''{
            "thought": "bad llm decision",
            "skill": "generate",
            "target_kind": "presentation",
            "reply_text": "已生成！",
            "outline": {"title": "架构总结", "nodes": [{"node_id": "sec-1", "title": "概述"}]},
            "content": {"title": "架构总结", "summary": "摘要", "sections": [{"title": "概述", "body": "正文内容", "notes": ""}]}
        }'''
        agent = AutonomousAgent()
        res = agent.step("帮我写一份总结报告")
        self.assertEqual(res.target_kind, "document")
        self.assertTrue(res.rendered_file["name"].endswith(".docx"))

    @patch.object(AutonomousAgent, "_call_llm")
    def test_step_generate_presentation_when_explicitly_requested(self, mock_call):
        """Only when user explicitly requests PPT/slides should presentation (.pptx) be generated."""
        mock_call.return_value = '''{
            "thought": "user wants direct ppt",
            "skill": "generate",
            "target_kind": "presentation",
            "reply_text": "PPT已生成完毕！",
            "outline": {
                "title": "云原生演进",
                "nodes": [
                    {"node_id": "sec-1", "title": "第一页：架构概览"},
                    {"node_id": "sec-2", "title": "第二页：核心优势"}
                ]
            },
            "content": {
                "title": "云原生演进",
                "summary": "云原生架构演进摘要",
                "sections": [
                    {"title": "第一页：架构概览", "body": "1. 容器化部署\\n2. 微服务治理", "notes": "演讲备注1"},
                    {"title": "第二页：核心优势", "body": "1. 弹性扩缩容\\n2. 高可用保障", "notes": "演讲备注2"}
                ]
            }
        }'''
        agent = AutonomousAgent()
        res = agent.step("直接帮我生成云原生演进PPT")
        self.assertEqual(res.skill, "generate")
        self.assertEqual(res.target_kind, "presentation")
        self.assertIsNotNone(res.outline)
        self.assertIsNotNone(res.content)
        self.assertIsNotNone(res.rendered_file)
        self.assertTrue(res.rendered_file["name"].endswith(".pptx"))
        self.assertGreater(len(res.rendered_file["data"]), 1000)


if __name__ == "__main__":
    unittest.main()
