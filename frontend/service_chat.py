"""Streamlit chat UI for the service API (run: streamlit run frontend/service_chat.py)."""
from __future__ import annotations
import streamlit as st
try:
    from .service_chat_client import ChatClient, ChatClientError, ChatState
except ImportError:  # streamlit run frontend/service_chat.py
    from service_chat_client import ChatClient, ChatClientError, ChatState

st.set_page_config(page_title="GenSlide Chat", page_icon="📄", layout="wide")
st.title("GenSlide · Chat")
st.caption("本页仅用于本地联调：每轮都会经过 mock BFF 授权，再调用选定的引擎。")

with st.sidebar:
    bff = st.text_input("BFF URL", "http://localhost:8010")
    token = st.text_input("Token", type="password")
    engine = st.selectbox("Engine", ["agentscope", "langgraph"])
    default_urls = {"langgraph": "http://localhost:8001", "agentscope": "http://localhost:8002"}
    service = st.text_input(f"{engine} URL", default_urls[engine])
    target = st.selectbox("目标", ["document", "presentation", "writing"], help="document 会生成可下载的 DOCX。")
    skill_id = st.text_input("Skill ID（可选）", help="留空时使用目标类型的默认 skill。")
    reset = st.button("新建会话", use_container_width=True)

context = (engine, target, skill_id.strip())
if reset or "chat_context" not in st.session_state or st.session_state.chat_context != context:
    st.session_state.chat_context = context
    st.session_state.chat_state = ChatState(engine=engine)
    st.session_state.messages = []
    st.session_state.downloads = []
if "messages" not in st.session_state: st.session_state.messages = []
if "downloads" not in st.session_state: st.session_state.downloads = []
with st.sidebar:
    st.caption(f"会话 {st.session_state.chat_state.session[:8]} · 版本 {st.session_state.chat_state.session_version}")
for item in st.session_state.messages: st.chat_message(item["role"]).write(item["text"])


def assistant_text(state: ChatState, operation: str) -> str:
    if state.answer:
        return state.answer
    guidance = state.guidance
    parts = [guidance.get("summary", "").strip()]
    parts.extend(q.get("text", "").strip() for q in guidance.get("questions", []))
    parts.extend(
        f"建议：{p.get('value', '')}" + (f"（{p['reason']}）" if p.get("reason") else "")
        for p in guidance.get("proposals", [])
    )
    parts = [part for part in parts if part]
    if parts:
        return "\n\n".join(parts)
    labels = {
        "create_outline": "大纲已生成，你可以继续修订或确认。",
        "revise_outline": "大纲已根据你的意见修订。",
        "confirm_outline": "大纲已确认，现在可以生成文档。",
        "generate": "文档已生成。",
    }
    return labels.get(operation, "操作已完成。")

client = ChatClient(bff, service, token, engine=engine)
uploaded = st.file_uploader("附件", type=["txt", "md", "pdf", "docx"])
prompt = st.chat_input("描述你的需求")
operations = ["clarify", "create_outline", "revise_outline", "explain_outline", "confirm_outline", "generate"]
operation_labels = {
    "clarify": "继续澄清",
    "create_outline": "生成大纲",
    "revise_outline": "修订大纲",
    "explain_outline": "解释大纲",
    "confirm_outline": "确认大纲",
    "generate": "生成内容",
}
cols = st.columns(len(operations))
state = st.session_state.chat_state
confirmed = state.guidance.get("stage") == "confirmed"
enabled = {"clarify": True, "create_outline": True, "revise_outline": bool(state.draft), "explain_outline": bool(state.draft),
           "confirm_outline": bool(state.draft), "generate": bool(state.draft) and confirmed}
chosen = next((op for op, col in zip(operations, cols) if col.button(operation_labels[op], disabled=not enabled[op])), None)
if prompt or chosen:
    message = prompt or ""
    if chosen in {"confirm_outline", "generate"}: message = ""
    operation = chosen or ("revise_outline" if state.draft else "clarify")
    files = []
    try:
        if uploaded:
            files.append(client.upload(uploaded.name, uploaded.getvalue(), uploaded.type, session_id=st.session_state.chat_state.session)["file_id"])
        result = client.execute(operation, target_kind=target, message=message, state=st.session_state.chat_state, current_file_ids=files,
                                draft_id=(st.session_state.chat_state.draft or {}).get("draft_id"),
                                expected_outline_version=(st.session_state.chat_state.draft or {}).get("outline_version"),
                                skill_id=skill_id.strip() or None)
        st.session_state.messages.append({"role": "user", "text": message or operation})
        st.session_state.messages.append({"role": "assistant", "text": assistant_text(state, operation)})
        for artifact in state.artifacts:
            if artifact.get("file_id"):
                st.session_state.downloads.append({"name": artifact.get("filename", artifact["file_id"]), "data": client.artifact(artifact["file_id"])})
        st.rerun()
    except ChatClientError as exc:
        st.error(f"{exc.code or 'ERROR'}: {exc}")
if st.session_state.chat_state.guidance: st.subheader("Guidance"); st.json(st.session_state.chat_state.guidance)
if st.session_state.chat_state.draft: st.subheader("大纲"); st.json(st.session_state.chat_state.draft)
if st.session_state.chat_state.content: st.subheader("正文"); st.json(st.session_state.chat_state.content)
for download in st.session_state.get("downloads", []): st.download_button("下载 " + download["name"], download["data"], file_name=download["name"])
