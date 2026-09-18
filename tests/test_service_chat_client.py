import json
from unittest.mock import patch

from frontend.service_chat_client import ChatClient, ChatState


def test_execute_begins_then_calls_service_and_updates_state():
    responses = [{"request": {"action_id": "a", "authorization": "grant", "engine": "agentscope"}},
                 {"session_version": 1, "result": {"guidance": {"stage": "outline"}, "outline": {"draft_id": "d"}, "answer": "ok"}}]
    class Reply:
        headers = type("H", (), {"get_content_type": lambda self: "application/json"})()
        def read(self): return json.dumps(responses.pop(0)).encode()
        def __enter__(self): return self
        def __exit__(self, *args): pass
    with patch("frontend.service_chat_client.urlopen", side_effect=lambda *a, **k: Reply()) as opened:
        state = ChatState()
        result = ChatClient("http://bff", "http://service", "token").execute("create_outline", state=state)
    assert state.session_version == 1 and state.draft["draft_id"] == "d" and state.answer == "ok"
    assert opened.call_count == 2


def test_execute_sends_draft_fields_and_artifact_download():
    responses = [{"request": {"action_id": "a", "authorization": "grant"}}, {"session_version": 2, "result": {"outline": {"draft_id": "d", "outline_version": 3}}, "content": {"title": "Doc", "sections": []}, "files": [{"file_id": "f", "filename": "out.docx"}]}]
    class Reply:
        headers = type("H", (), {"get_content_type": lambda self: "application/json"})()
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def fake(req, **kwargs):
        if req.full_url.endswith("/artifacts/f"): return Reply(b"doc")
        return Reply(json.dumps(responses.pop(0)).encode())
    with patch("frontend.service_chat_client.urlopen", side_effect=fake) as opened:
        state = ChatState(draft={"draft_id": "d", "outline_version": 3})
        client = ChatClient("http://bff", "http://service", "token")
        client.execute("revise_outline", state=state, draft_id="d", expected_outline_version=3, skill_id="document")
        assert state.content["title"] == "Doc" and state.artifacts[0]["file_id"] == "f"
        assert client.artifact("f") == b"doc"
    execute_request = opened.call_args_list[1].args[0]
    body = json.loads(execute_request.data)
    assert body["draft_id"] == "d" and body["expected_outline_version"] == 3
    assert body["skill_id"] == "document"


def test_upload_scopes_file_to_session_and_sanitizes_name():
    class Reply:
        headers = type("H", (), {"get_content_type": lambda self: "application/json"})()
        def read(self): return b'{"file_id":"uploaded"}'
        def __enter__(self): return self
        def __exit__(self, *args): pass

    with patch("frontend.service_chat_client.urlopen", return_value=Reply()) as opened:
        result = ChatClient("http://bff", "http://service", "token").upload(
            '../bad"name.docx', b"document", session_id="session-1"
        )

    request = opened.call_args.args[0]
    assert result == {"file_id": "uploaded"}
    assert b'name="session_id"' in request.data and b"session-1" in request.data
    assert b'filename="bad_name.docx"' in request.data


def test_list_skills_and_reload_skills():
    class Reply:
        headers = type("H", (), {"get_content_type": lambda self: "application/json"})()
        def __init__(self, data): self.data = data
        def read(self): return json.dumps(self.data).encode()
        def __enter__(self): return self
        def __exit__(self, *args): pass

    skills_payload = {"skills": [{"skill_id": "s1", "name": "Skill 1"}]}
    with patch("frontend.service_chat_client.urlopen", return_value=Reply(skills_payload)):
        client = ChatClient("http://bff", "http://service", "token")
        skills = client.list_skills()
        assert len(skills) == 1
        assert skills[0]["skill_id"] == "s1"

        reloaded = client.reload_skills()
        assert len(reloaded) == 1
        assert reloaded[0]["skill_id"] == "s1"

