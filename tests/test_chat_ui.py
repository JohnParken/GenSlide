"""Streamlit AppTest coverage for the offline chat workbench."""
from types import SimpleNamespace
from unittest.mock import patch
import os
import sys

from streamlit.testing.v1 import AppTest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "frontend"))


def _fake_result(message):
    return SimpleNamespace(
        reply_text=f"已处理：{message}", follow_up_questions=[], outline=None, content=None,
        rendered_file=None, memory={}, target_kind="document", skill="reply",
    )


def test_startup_chat_new_session_and_switch_preserves_messages(tmp_path):
    import frontend.autonomous_agent as agent_module
    import frontend.service_chat_client as client_module

    agent_step = lambda self, message, **kwargs: _fake_result(message)
    with patch.object(client_module.ChatClient, "list_skills", return_value=[
        {"skill_id": "document", "name": "document", "description": "offline", "target_kind": "document", "version": "1", "hash": "x"}
    ]), patch.object(agent_module.AutonomousAgent, "step", agent_step):
        app = AppTest.from_file("frontend/service_chat.py").run()
        assert not app.exception
        new_session = next(button for button in app.button if button.label == "➕ 新建会话")
        assert new_session.value is False

        app.chat_input[0].set_value("第一条消息").run()
        assert not app.exception
        assert any(item["text"] == "第一条消息" for item in app.session_state.messages)

        sid = app.session_state.current_session_id
        attachment = {"filename": "brief.md", "text": "source evidence", "char_count": 14, "size_str": "14 B"}
        app.session_state.sessions_store[sid]["current_attachment"] = attachment
        app.session_state.current_attachment = attachment
        expert = next(select for select in app.selectbox if select.label == "坐镇顾问 (Expert)")
        expert.set_value(expert.options[1]).run()
        assert not app.exception
        assert any(item["text"] == "第一条消息" for item in app.session_state.messages)
        assert app.session_state.current_attachment["filename"] == "brief.md"

        new_session.click().run()
        assert not app.exception
        assert len(app.session_state.sessions_store) == 2
        assert app.session_state.messages == []

        # Select the first session button by its rendered label and verify its history returns.
        first = next(button for button in app.button if "新创作对话" in button.label)
        first.click().run()
        assert not app.exception
        assert any(item["text"] == "第一条消息" for item in app.session_state.messages)


def test_empty_option_followups_submit_without_crashing():
    import frontend.autonomous_agent as agent_module
    import frontend.service_chat_client as client_module

    def result_with_followups(self, message, **kwargs):
        return SimpleNamespace(
            reply_text="请补充信息", follow_up_questions=[
                {"question": "主题？", "field": "topic", "options": []},
                {"question": "受众？", "field": "audience", "options": []},
            ], outline=None, content=None, rendered_file=None, memory={},
            target_kind="document", skill="reply",
        )

    with patch.object(client_module.ChatClient, "list_skills", return_value=[]), \
         patch.object(agent_module.AutonomousAgent, "step", result_with_followups):
        app = AppTest.from_file("frontend/service_chat.py").run()
        app.chat_input[0].set_value("开始规划").run()
        assert not app.exception
        submit = next(button for button in app.button if button.label == "提交补充")
        submit.click().run()
        assert not app.exception
