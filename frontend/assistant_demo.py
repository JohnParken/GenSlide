"""Ephemeral development UI for the unified server-side assistant."""
import os
from uuid import uuid4

import streamlit as st

try:
    from frontend.service_chat_client import ChatClient, ChatState
except ImportError:
    from service_chat_client import ChatClient, ChatState

st.set_page_config(page_title="GenSlide 创作助手", page_icon="✍️")
st.title("GenSlide 创作助手")
st.caption("开发演示：对话仅保留在当前页面会话中，持久创作空间尚未接入。")
client = ChatClient(
    os.getenv("GENSLIDE_DEMO_BFF_URL", "http://127.0.0.1:8010"),
    os.getenv("GENSLIDE_SERVICE_URL", "http://127.0.0.1:8002"),
    os.getenv("GENSLIDE_SERVICE_TOKEN", "local-development-token-at-least-32-characters"),
)
if "assistant_state" not in st.session_state:
    st.session_state.assistant_state = ChatState()
    st.session_state.assistant_messages = []
    st.session_state.assistant_files = {}
    st.session_state.assistant_pending = None

state = st.session_state.assistant_state
output = st.selectbox("交付形式", ["auto", "text", "document", "presentation"])
skill = st.text_input("指定 Skill（留空自动选择）")
attachment_id = st.text_input("本轮材料 file_id（由开发 BFF 上传获得）")
for role, text in st.session_state.assistant_messages:
    with st.chat_message(role):
        st.markdown(text)

pending = st.session_state.assistant_pending
if pending:
    st.info("上次请求尚未确认结果。重试将保留原 action 和本轮输入，也可以放弃并创建新输入。")
    col1, col2 = st.columns(2)
    with col1:
        retry = st.button("重试原请求")
    with col2:
        if st.button("放弃上次请求并重新输入"):
            st.session_state.assistant_pending = None
            st.rerun()
else:
    retry = False

message = st.chat_input("聊想法、修改原稿，或直接让我写作", disabled=bool(pending))
if message or retry:
    if message:
        pending = {"message": message, "action_id": uuid4().hex,
                   "requested_output": output, "requested_skill_id": skill.strip() or None,
                   "current_file_ids": [attachment_id.strip()] if attachment_id.strip() else []}
        st.session_state.assistant_pending = pending
    try:
        with st.spinner("正在创作…"):
            result = client.turn(state=state, **pending)
        payload = result.get("result", {})
        reply = result.get("reply") or payload.get("reply") or "本轮已完成。"
        st.session_state.assistant_messages.extend([("user", pending["message"]), ("assistant", reply)])
        for reference in result.get("files", []):
            st.session_state.assistant_files[reference["file_id"]] = reference
        st.session_state.assistant_pending = None
        st.rerun()
    except Exception as exc:
        st.error(f"请求未完成：{exc}")
        # 如果是确定性的终态失败（如 422 校验失败、ACTION_CLOSED、SKILL_NOT_FOUND 等），自动允许用户清除 pending
        err_msg = str(exc)
        if any(term in err_msg for term in ("422", "409", "404", "ACTION_CLOSED", "SKILL_NOT_FOUND", "OUTPUT_INTENT_MISMATCH")):
            st.warning("该操作已进入终态失败。您可修改参数后直接提交新消息。")
            st.session_state.assistant_pending = None

if state.content:
    st.subheader(state.content.get("title", "当前稿"))
    for section in state.content.get("sections", []):
        st.markdown(f"### {section['title']}")
        st.markdown(section["body"])
elif state.draft:
    st.json(state.draft)

for file_id, reference in st.session_state.assistant_files.items():
    if st.button(f"准备下载：{reference['filename']}", key=file_id):
        try:
            # Bytes exist only for this download render; never store them in session_state.
            st.download_button("下载", client.artifact(file_id), file_name=reference["filename"],
                               mime=reference["content_type"], key=f"download-{file_id}")
        except Exception as exc:
            st.error(f"下载失败：{exc}")
