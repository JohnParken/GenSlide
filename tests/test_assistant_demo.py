from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from frontend.service_chat_client import ChatClient


def test_demo_submits_natural_language_and_retains_no_file_bytes():
    def turn(self, message, *, state, **kwargs):
        assert message == "直接写一段介绍"
        assert kwargs["action_id"]
        state.session_version = 1
        state.content = {"title": "介绍", "sections": [{"title": "正文", "body": "示例正文"}]}
        return {"effect": "deliverable", "reply": "完成", "session_version": 1,
                "files": [{"file_id": "f", "filename": "介绍.docx", "content_type": "application/test"}]}

    with patch.object(ChatClient, "turn", turn):
        app = AppTest.from_file("frontend/assistant_demo.py").run()
        assert not app.exception
        app.chat_input[0].set_value("直接写一段介绍").run()
        assert not app.exception
        assert app.session_state.assistant_pending is None
        assert app.session_state.assistant_files["f"]["filename"] == "介绍.docx"
        assert "data" not in app.session_state.assistant_files["f"]
