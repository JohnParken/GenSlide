"""Persist session aliases before Streamlit reruns or switches conversations."""
from __future__ import annotations

SESSION_FIELDS = ("chat_state", "messages", "downloads", "current_attachment", "active_follow_ups", "uploader_key")


def save_session(session_state) -> None:
    session_id = session_state.get("loaded_session_id")
    session = session_state.get("sessions_store", {}).get(session_id)
    if session is not None:
        for key in SESSION_FIELDS:
            if key in session_state:
                session[key] = session_state[key]


def load_session(session_state) -> dict:
    session_id = session_state["current_session_id"]
    session = session_state["sessions_store"][session_id]
    for key in SESSION_FIELDS:
        session_state[key] = session[key]
    session_state["loaded_session_id"] = session_id
    return session
