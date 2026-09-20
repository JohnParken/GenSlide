"""Offline session persistence and client state regressions."""
from unittest.mock import patch

from frontend.chat_session import load_session, save_session
from frontend.service_chat_client import ChatClient, ChatState


def test_save_load_replaces_attachment_and_followups_before_rerun():
    state = {
        "loaded_session_id": "a",
        "current_session_id": "a",
        "sessions_store": {"a": {"chat_state": ChatState(), "messages": [], "downloads": [],
                                   "current_attachment": None, "active_follow_ups": [], "uploader_key": 0}},
        "chat_state": ChatState(), "messages": [{"role": "user", "text": "old"}], "downloads": [],
        "current_attachment": {"filename": "brief.md", "text": "source"},
        "active_follow_ups": [{"question": "q"}], "uploader_key": 3,
    }
    save_session(state)
    state["messages"] = []
    state["current_attachment"] = None
    state["active_follow_ups"] = []
    state["current_session_id"] = "a"
    loaded = load_session(state)
    assert loaded["messages"] == [{"role": "user", "text": "old"}]
    assert loaded["current_attachment"]["filename"] == "brief.md"
    assert loaded["active_follow_ups"] == [{"question": "q"}]
    assert loaded["uploader_key"] == 3


def test_switching_sessions_keeps_state_isolated():
    state = {
        "loaded_session_id": "a", "current_session_id": "a",
        "sessions_store": {
            "a": {"chat_state": ChatState(), "messages": [{"text": "A"}], "downloads": [], "current_attachment": {"filename": "a.txt"}, "active_follow_ups": [], "uploader_key": 0},
            "b": {"chat_state": ChatState(), "messages": [{"text": "B"}], "downloads": [], "current_attachment": None, "active_follow_ups": [], "uploader_key": 0},
        },
    }
    load_session(state)
    state["messages"].append({"text": "A2"})
    save_session(state)
    state["current_session_id"] = "b"
    load_session(state)
    assert [m["text"] for m in state["messages"]] == ["B"]
    assert state["current_attachment"] is None
    state["current_session_id"] = "a"
    load_session(state)
    assert [m["text"] for m in state["messages"]] == ["A", "A2"]


def test_client_explicit_empty_requirements_clears_but_omitted_content_is_preserved():
    client = ChatClient("http://bff", "http://service", "token")
    state = ChatState(requirements={"topic": "old"}, content={"title": "existing"})
    with patch.object(client, "_request", side_effect=[
        {}, {"result": {"requirements": {}}},
        {}, {"result": {}},
    ]):
        client.execute("clarify", state=state)
        assert state.requirements == {}
        client.execute("clarify", state=state)
    assert state.content == {"title": "existing"}
