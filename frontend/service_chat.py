"""GenSlide AI Writing Studio - Apple-inspired Workbench with Expert Advisory Council.

Features:
1. Creative Exploration Mode (自由探索模式) with 3-5 follow-up questions & clickable option chips;
2. Professional Workflow Mode (专业模式) with model-driven intelligent state transitions;
3. WorkBuddy-style Expert Advisory Council (专家团) with vivid personas and specialized guidance;
4. Apple-grade frosted glass aesthetics and live artifact inspector.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from zipfile import BadZipFile, ZipFile
import streamlit as st

try:
    from .service_chat_client import ChatClient, ChatClientError, ChatState
    from .autonomous_agent import AutonomousAgent, detect_target_kind, route_professional_intent
    from .expert_council import get_expert, list_all_experts, ExpertProfile
except ImportError:  # streamlit run frontend/service_chat.py
    from service_chat_client import ChatClient, ChatClientError, ChatState
    from autonomous_agent import AutonomousAgent, detect_target_kind, route_professional_intent
    from expert_council import get_expert, list_all_experts, ExpertProfile

st.set_page_config(
    page_title="GenSlide · AI 写作工作台",
    page_icon="✨",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# 附件预检校验辅助函数
# ---------------------------------------------------------------------------
def validate_attachment(filename: str, content: bytes) -> tuple[bool, str]:
    """Pre-flight check for attachment validity before submitting to BFF/backend."""
    if not content or len(content) == 0:
        return False, "所选文件内容为空（0 字节），无法提取有效文本。"
    if len(content) > 20 * 1024 * 1024:
        return False, "文件大小超过 20MB 上限。"

    suffix = Path(filename).suffix.lower()
    if suffix not in {".txt", ".md", ".pdf", ".docx"}:
        return False, f"不支持的文件后缀 `{suffix}`，仅支持 .txt, .md, .pdf, .docx。"

    if suffix in {".txt", ".md"}:
        try:
            content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                content.decode("gb18030")
            except UnicodeDecodeError:
                return False, "文本文件编码不支持，请确保为 UTF-8 或 GBK/GB18030 编码。"
    elif suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(content))
            if len(reader.pages) > 200:
                return False, f"PDF 页数 ({len(reader.pages)}) 超过 200 页限制。"
            has_text = any((page.extract_text() or "").strip() for page in reader.pages)
            if not has_text:
                return False, (
                    "该 PDF 文件未检测到文本图层（疑似纯图片或扫描件）。\n"
                    "后端需要可直接提取的文字（当前未集成 OCR 图文识别）。请上传包含可复制文字的 PDF，或使用 TXT/Word 文档。"
                )
        except Exception as exc:
            return False, f"PDF 解析失败或损坏: {exc}"
    elif suffix == ".docx":
        try:
            with ZipFile(io.BytesIO(content)) as archive:
                if any(entry.flag_bits & 0x1 for entry in archive.infolist()):
                    return False, "不支持受密码加密保护的 DOCX 文件。"
        except BadZipFile:
            return False, "无效的 DOCX 文件（若是旧版 .doc 格式，请在 Word 中另存为 .docx 后再上传）。"
        try:
            from docx import Document

            doc = Document(io.BytesIO(content))
            has_text = any(p.text.strip() for p in doc.paragraphs) or any(
                cell.text.strip() for table in doc.tables for row in table.rows for cell in row.cells
            )
            if not has_text:
                return False, "DOCX 文档内未找到任何有效正文段落或表格文字。"
        except Exception as exc:
            return False, f"DOCX 解析失败: {exc}"

    return True, ""


# ---------------------------------------------------------------------------
# 侧边栏：工作台设置与 WorkBuddy 专家团
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### ⚙️ 工作台设置")

    # 视觉主题选择器：默认启用 Apple Studio Light（浅色纸质质感）
    ui_theme = st.radio(
        "🎨 视觉主题 (Theme)",
        ["☀️ 经典浅色 (Apple Studio Light)", "🌙 暗黑深邃 (Dark Studio)"],
        index=0,
        help="☀️ 经典浅色：明亮清爽纸质质感，右侧看板纯白卡片（默认）；🌙 暗黑深邃：暗黑模式。",
    )
    is_dark = ui_theme.startswith("🌙")

    interaction_mode = st.radio(
        "工作台模式 (Mode)",
        ["🌟 自由探索模式 (Creative Exploration)", "📐 专业模式 (Professional Workflow)"],
        index=0,
        help=(
            "🌟 自由探索模式：大模型完全自主规划并调用专家技能，支持一句话直出、智能追问与选项快速点击；\n"
            "📐 专业模式：严格刚性状态机控制（需求澄清->制定大纲->显式确认->生成正文），支持自然语言意图智能流转。"
        ),
    )
    is_autonomous = interaction_mode.startswith("🌟")

    st.divider()

    # 1. 服务网关配置
    with st.expander("🔧 服务网关与通信设置", expanded=False):
        bff = st.text_input("BFF URL", "http://localhost:8010")
        default_token = os.getenv("GENSLIDE_SERVICE_TOKEN", "local-development-token-at-least-32-characters")
        token = st.text_input("Token", value=default_token, type="password")
        engine = st.selectbox("Engine", ["agentscope", "langgraph"], index=0)
        default_urls = {"langgraph": "http://localhost:8001", "agentscope": "http://localhost:8002"}
        service = st.text_input(f"{engine} URL", default_urls[engine])
        client = ChatClient(bff, service, token, engine=engine)

    client = ChatClient(bff, service, token, engine=engine)

    # 2. 默认目标交付形态
    target = st.selectbox(
        "默认交付形态 (Target)",
        ["document", "presentation", "writing"],
        index=0,
        help="document: 生成可编辑 Word/WPS 文档（默认标准交付形态）；presentation: 仅在明确要制作PPT时生成 PPTX；writing: 生成纯正文。",
    )

    st.divider()

    # 3. WorkBuddy 专家团顾问席位 (Expert Advisory Council)
    st.markdown("### 👥 专家顾问团 (Expert Council)")

    # 动态发现技能
    discovered_skills = []
    try:
        discovered_skills = client.list_skills()
    except Exception:
        discovered_skills = []

    if not discovered_skills:
        try:
            if "autonomous_agent" not in st.session_state:
                st.session_state.autonomous_agent = AutonomousAgent()
            discovered_skills = st.session_state.autonomous_agent.list_skills()
        except Exception:
            discovered_skills = []

    all_experts = list_all_experts(discovered_skills)
    expert_options = [f"{exp.avatar} {exp.name}" for exp in all_experts]
    expert_map = {opt: exp for opt, exp in zip(expert_options, all_experts)}

    col_exp_title, col_exp_btn = st.columns([3, 1])
    with col_exp_title:
        st.caption("请派当席坐镇专家顾问")
    with col_exp_btn:
        if st.button("🔄", help="热重载技能与专家团名单 (零停机即时生效)"):
            try:
                client.reload_skills()
            except Exception:
                pass
            if "autonomous_agent" in st.session_state:
                try:
                    st.session_state.autonomous_agent.reload_skills()
                except Exception:
                    pass
            st.toast("专家团成员与技能库已成功动态刷新！")
            st.rerun()

    selected_expert_label = st.selectbox(
        "专家顾问",
        expert_options,
        index=0,
        label_visibility="collapsed",
        help="请派各领域资深专家为您提供专属辅导与文风约束",
    )
    current_expert = expert_map[selected_expert_label]
    skill_id = current_expert.id if current_expert.id != "default" else ""


# ---------------------------------------------------------------------------
# Apple 视觉主题色彩动态计算 (Theme Palette)
# ---------------------------------------------------------------------------
if is_dark:
    app_bg = "#000000"
    text_main = "#f5f5f7"
    text_sub = "#86868b"
    text_caption = "#a1a1a6"
    sidebar_bg = "#161618"
    sidebar_border = "rgba(255, 255, 255, 0.1)"
    card_bg = "rgba(28, 28, 30, 0.65)"
    card_border = "rgba(255, 255, 255, 0.08)"
    card_shadow = "0 8px 30px rgba(0, 0, 0, 0.4)"
    header_bg = "rgba(28, 28, 30, 0.75)"
    header_border = "rgba(255, 255, 255, 0.1)"
    header_title_color = "#ffffff"
    tag_bg = "rgba(255, 255, 255, 0.08)"
    tag_border = "rgba(255, 255, 255, 0.1)"
    tag_text = "#d1d1d6"
    chat_bubble_bg = "rgba(28, 28, 30, 0.6)"
    chat_bubble_border = "rgba(255, 255, 255, 0.08)"
    btn_bg = "rgba(255, 255, 255, 0.06)"
    btn_border = "rgba(255, 255, 255, 0.12)"
    btn_text = "#ffffff"
    banner_bg = "rgba(28, 28, 30, 0.6)"
    banner_border = "rgba(255, 255, 255, 0.08)"
else:
    app_bg = "#fbfbfd"
    text_main = "#1d1d1f"
    text_sub = "#86868b"
    text_caption = "#424245"
    sidebar_bg = "#f5f5f7"
    sidebar_border = "rgba(0, 0, 0, 0.08)"
    card_bg = "#ffffff"
    card_border = "rgba(0, 0, 0, 0.08)"
    card_shadow = "0 2px 10px rgba(0, 0, 0, 0.04), 0 1px 3px rgba(0, 0, 0, 0.02)"
    header_bg = "rgba(255, 255, 255, 0.85)"
    header_border = "rgba(0, 0, 0, 0.08)"
    header_title_color = "#1d1d1f"
    tag_bg = "#f5f5f7"
    tag_border = "rgba(0, 0, 0, 0.06)"
    tag_text = "#1d1d1f"
    chat_bubble_bg = "#ffffff"
    chat_bubble_border = "rgba(0, 0, 0, 0.08)"
    btn_bg = "#ffffff"
    btn_border = "rgba(0, 0, 0, 0.12)"
    btn_text = "#1d1d1f"
    banner_bg = "rgba(0, 113, 227, 0.04)"
    banner_border = "rgba(0, 113, 227, 0.15)"


# ---------------------------------------------------------------------------
# Apple Design System CSS 注入
# ---------------------------------------------------------------------------
APPLE_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

/* Global Apple typography and base styling */
html, body, [data-testid="stAppViewContainer"], .main {{
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Inter", "Helvetica Neue", sans-serif !important;
    color: {text_main} !important;
    background-color: {app_bg} !important;
}}

[data-testid="stSidebar"] {{
    background-color: {sidebar_bg} !important;
    border-right: 1px solid {sidebar_border} !important;
}}

/* Header bar */
.apple-header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 20px;
    background: {header_bg};
    backdrop-filter: blur(25px);
    -webkit-backdrop-filter: blur(25px);
    border: 1px solid {header_border};
    border-radius: 18px;
    margin-bottom: 20px;
    box-shadow: {card_shadow};
}}

.apple-title-wrap {{
    display: flex;
    align-items: center;
    gap: 12px;
}}

.apple-logo-badge {{
    font-size: 24px;
    background: rgba(0, 113, 227, 0.08);
    border-radius: 12px;
    width: 44px;
    height: 44px;
    display: flex;
    align-items: center;
    justify-content: center;
    border: 1px solid rgba(0, 113, 227, 0.18);
}}

.apple-main-title {{
    font-size: 20px;
    font-weight: 700;
    letter-spacing: -0.5px;
    color: {header_title_color};
    margin: 0;
}}

.apple-sub-title {{
    font-size: 12px;
    color: {text_sub};
    margin-top: 2px;
}}

.status-dot {{
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
    background-color: #34c759;
    box-shadow: 0 0 8px #34c759;
    margin-right: 6px;
}}

.apple-pill {{
    display: inline-flex;
    align-items: center;
    padding: 4px 12px;
    border-radius: 9999px;
    font-size: 12px;
    font-weight: 500;
    background: rgba(0, 113, 227, 0.08);
    color: #0071e3;
    border: 1px solid rgba(0, 113, 227, 0.2);
}}

/* Streamlit button refinement */
div.stButton > button {{
    border-radius: 12px !important;
    font-weight: 500 !important;
    transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1) !important;
    border: 1px solid {btn_border} !important;
    background: {btn_bg} !important;
    color: {btn_text} !important;
}}

div.stButton > button:hover {{
    border-color: #0071e3 !important;
    color: #0071e3 !important;
    box-shadow: 0 4px 16px rgba(0, 113, 227, 0.15) !important;
    transform: translateY(-1px) !important;
}}

/* Primary buttons with Apple Blue gradient */
div.stButton > button[kind="primary"] {{
    background: linear-gradient(135deg, #0071e3 0%, #0077ed 100%) !important;
    border: none !important;
    color: #ffffff !important;
}}

div.stButton > button[kind="primary"]:hover {{
    background: linear-gradient(135deg, #0077ed 0%, #2997ff 100%) !important;
}}

/* Chat message bubbles */
[data-testid="stChatMessage"] {{
    background: {chat_bubble_bg} !important;
    border: 1px solid {chat_bubble_border} !important;
    border-radius: 16px !important;
    padding: 14px 18px !important;
    margin-bottom: 12px !important;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.02) !important;
}}

/* Tabs styling */
[data-testid="stTabs"] button {{
    border-radius: 10px !important;
    font-weight: 600 !important;
    color: {text_sub} !important;
}}

[data-testid="stTabs"] button[aria-selected="true"] {{
    color: #0071e3 !important;
    border-bottom-color: #0071e3 !important;
}}
</style>
"""
st.markdown(APPLE_CSS, unsafe_allow_html=True)


# 渲染侧边栏坐镇专家卡片 (Seated Expert Profile Card)
with st.sidebar:
    tags_html = " ".join(
        f'<span style="background: rgba(0, 113, 227, 0.08); color: #0071e3; border: 1px solid rgba(0, 113, 227, 0.18); border-radius: 9999px; padding: 2px 8px; font-size: 11px; font-weight: 500;">#{tag}</span>'
        for tag in current_expert.tags
    )
    st.markdown(
        f"""
        <div style="background: {card_bg}; border: 1px solid {card_border}; border-radius: 14px; padding: 14px 16px; margin: 10px 0 16px 0; box-shadow: {card_shadow};">
            <div style="display: flex; align-items: center; gap: 12px;">
                <div style="font-size: 26px; background: rgba(0, 113, 227, 0.08); border-radius: 10px; width: 44px; height: 44px; display: flex; align-items: center; justify-content: center; border: 1px solid rgba(0, 113, 227, 0.15);">
                    {current_expert.avatar}
                </div>
                <div>
                    <div style="font-size: 15px; font-weight: 600; color: {text_main};">{current_expert.name}</div>
                    <div style="font-size: 12px; color: {text_sub};">{current_expert.title}</div>
                </div>
            </div>
            <div style="margin-top: 10px; font-size: 12px; color: {text_caption}; line-height: 1.5;">
                {current_expert.intro}
            </div>
            <div style="margin-top: 8px; display: flex; flex-wrap: wrap; gap: 4px;">
                {tags_html}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.divider()
    reset = st.button("✨ 重置并新建会话", use_container_width=True)

# ---------------------------------------------------------------------------
# 会话状态管理
# ---------------------------------------------------------------------------
context = (engine, target, skill_id.strip(), interaction_mode)
if reset or "chat_context" not in st.session_state or st.session_state.chat_context != context:
    st.session_state.chat_context = context
    st.session_state.chat_state = ChatState(engine=engine)
    st.session_state.messages = []
    st.session_state.downloads = []
    st.session_state.current_attachment = None
    st.session_state.active_follow_ups = []
    st.session_state.uploader_key = st.session_state.get("uploader_key", 0) + 1

if "autonomous_agent" not in st.session_state:
    st.session_state.autonomous_agent = AutonomousAgent()
if "messages" not in st.session_state:
    st.session_state.messages = []
if "downloads" not in st.session_state:
    st.session_state.downloads = []
if "current_attachment" not in st.session_state:
    st.session_state.current_attachment = None
if "active_follow_ups" not in st.session_state:
    st.session_state.active_follow_ups = []
if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0

state: ChatState = st.session_state.chat_state

with st.sidebar:
    st.caption(f"🆔 会话: `{state.session[:8]}` | 状态版本: `v{state.session_version}`")
    with st.expander("🛠️ 工作台底层状态 (State JSON)", expanded=False):
        st.caption("Guidance:")
        st.json(state.guidance or {})
        st.caption("Draft:")
        st.json(state.draft or {})
        st.caption("Content:")
        st.json(state.content or {})


# ---------------------------------------------------------------------------
# 阶段解析函数
# ---------------------------------------------------------------------------
def compute_stage(state: ChatState) -> tuple[int, str]:
    if state.content:
        return 4, "已生成交付物 (Completed)"
    if state.guidance.get("stage") == "confirmed" or (state.draft and state.draft.get("confirmed_hash")):
        return 3, "大纲已锁定确认 (Confirmed)"
    if state.draft:
        return 2, f"大纲制定中 v{state.draft.get('outline_version', 1)} (Outline)"
    return 1, "需求澄清中 (Clarify)"


current_stage_idx, current_stage_name = compute_stage(state)


# ---------------------------------------------------------------------------
# 格式化助手回复
# ---------------------------------------------------------------------------
def format_assistant_message(state: ChatState, operation: str) -> str:
    lines = []

    # 1. 结构化大纲生成或修订
    if operation in {"create_outline", "revise_outline"} and state.draft:
        draft = state.draft
        lines.append(f"📑 **大纲已成功生成：【{draft.get('title', '未命名大纲')}】** (版本: `v{draft.get('outline_version', 1)}`)")
        lines.append("\n**章节结构清单：**")
        nodes = draft.get("nodes", [])
        for idx, node in enumerate(nodes, 1):
            lines.append(f"{idx}. 📌 **{node.get('title')}** `(id: {node.get('node_id')})`")
        lines.append(
            "\n👉 **智能流转建议**：\n"
            "- 若大纲结构满意，直接在下方输入「确认大纲」或「就按这个大纲出正文」，AI 将自动为您锁定并生成；\n"
            "- 若需调整章节，直接输入修改意见（例如：*增加一节案例分析* 或 *第三节改成落地实践*）。"
        )
        return "\n".join(lines)

    # 2. 确认大纲
    if operation == "confirm_outline":
        conf_hash = (state.draft or {}).get("confirmed_hash") or ""
        short_hash = f" `({conf_hash[:8]})`" if conf_hash else ""
        ver = (state.draft or {}).get("outline_version", 1)
        return (
            f"🔒 **大纲已正式锁定确认！**{short_hash}\n\n"
            f"大纲结构现已刚性锁定（版本 `v{ver}`）。\n"
            f"直接输入「开始生成」或点击下方按钮，即可启动交付物渲染！"
        )

    # 3. 交付生成
    if operation == "generate" and state.content:
        content = state.content
        sections = content.get("sections", [])
        lines.append("🎉 **完整内容交付成功！**")
        lines.append(f"- **主标题**：【{content.get('title', '无标题')}】")
        lines.append(f"- **章节总数**：共 {len(sections)} 个完整章节")
        if content.get("summary"):
            lines.append(f"- **内容摘要**：{content.get('summary')}")
        lines.append("\n👉 您可以在右侧面板查看 **「📖 交付正文」**，或前往 **「💾 文件下载」** 获取导出的文件。")
        return "\n\n".join(lines)

    # 4. 大纲解释 (explain_outline)
    if operation == "explain_outline":
        ans = state.answer.strip() if state.answer else "暂无针对该大纲的解释说明。"
        return f"💬 **大纲解析说明**：\n\n{ans}"

    # 5. 澄清与讨论阶段 (clarify)
    if state.answer and state.answer.strip():
        lines.append(state.answer.strip())

    guidance = state.guidance or {}

    # 阶段指引
    summary = guidance.get("summary", "").strip()
    if summary and summary not in (state.answer or ""):
        lines.append(f"**📋 阶段指引**\n\n{summary}")

    # 待明确问题
    questions = guidance.get("questions", [])
    if questions:
        q_lines = ["\n**❓ 待明确问题：**"]
        for idx, q in enumerate(questions, 1):
            q_lines.append(f"{idx}. {q.get('text', '')}")
            opts = q.get("options", [])
            if opts:
                opts_str = " | ".join(f"`{opt}`" for opt in opts)
                q_lines.append(f"   *可选方案*: {opts_str}")
        lines.append("\n".join(q_lines))

    # 建议提案
    proposals = guidance.get("proposals", [])
    if proposals:
        p_lines = ["\n**💡 助手建议：**"]
        for idx, p in enumerate(proposals, 1):
            reason = f" *（{p['reason']}）*" if p.get("reason") else ""
            p_lines.append(f"{idx}. **{p.get('field', '建议')}**: `{p.get('value', '')}`{reason}")
        lines.append("\n".join(p_lines))

    if not lines:
        return "✅ 操作已成功完成，请继续输入下一步指令。"

    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Apple 风格顶栏与工作台状态标识
# ---------------------------------------------------------------------------
mode_badge = "🌟 自由探索模式" if is_autonomous else "📐 专业工作流模式"
mode_badge_style = (
    "background: rgba(255, 255, 255, 0.08); color: #e5e5ea; border: 1px solid rgba(255, 255, 255, 0.1);"
    if is_dark
    else "background: #f2f2f7; color: #1d1d1f; border: 1px solid rgba(0, 0, 0, 0.08);"
)
st.markdown(
    f"""
    <div class="apple-header">
        <div class="apple-title-wrap">
            <div class="apple-logo-badge">✨</div>
            <div>
                <h1 class="apple-main-title">GenSlide · AI 写作工作台</h1>
                <div class="apple-sub-title">Next-Gen Intelligent Document & Presentation Studio</div>
            </div>
        </div>
        <div style="display: flex; align-items: center; gap: 10px;">
            <span class="apple-pill">
                <span class="status-dot"></span> 坐镇专家：{current_expert.avatar} {current_expert.name}
            </span>
            <span style="display: inline-flex; align-items: center; padding: 4px 12px; border-radius: 9999px; font-size: 12px; font-weight: 500; {mode_badge_style}">
                {mode_badge}
            </span>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

if not is_autonomous:
    # 顶部阶段 Stepper 状态条
    col_s1, col_s2, col_s3, col_s4 = st.columns(4)
    with col_s1:
        if current_stage_idx == 1:
            st.info("🔹 **1. 需求澄清 (Clarify)**")
        elif current_stage_idx > 1:
            st.success("✔ **1. 需求澄清**")
        else:
            st.caption("1. 需求澄清")
    with col_s2:
        if current_stage_idx == 2:
            st.info("🔹 **2. 制定大纲 (Outline)**")
        elif current_stage_idx > 2:
            st.success("✔ **2. 制定大纲**")
        else:
            st.caption("2. 制定大纲")
    with col_s3:
        if current_stage_idx == 3:
            st.info("🔹 **3. 锁定确认 (Confirm)**")
        elif current_stage_idx > 3:
            st.success("✔ **3. 锁定确认**")
        else:
            st.caption("3. 锁定确认")
    with col_s4:
        if current_stage_idx == 4:
            st.success("🎉 **4. 交付完成 (Generate)**")
        else:
            st.caption("4. 内容生成")
else:
    with st.container():
        st.markdown(
            f"""
            <div style="background: {banner_bg}; border: 1px solid {banner_border}; border-radius: 14px; padding: 12px 18px; margin-bottom: 12px; display: flex; align-items: center; justify-content: space-between; box-shadow: {card_shadow};">
                <div style="font-size: 13px; color: {text_main};">
                    🌟 <b>自由探索模式</b> · 由 <b>{current_expert.name}</b> 协同自主规划。支持发散探讨与结构化追问，可随时一键直出交付物。
                </div>
                <div style="font-size: 12px; color: {text_sub};">
                    大纲状态: {'✅ 已就绪' if state.draft else '⏳ 规划中'} ｜ 正文状态: {'🎉 已产出' if state.content else '⏳ 待撰写'}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

st.divider()

chat_col, panel_col = st.columns([3, 2], gap="large")


# ---------------------------------------------------------------------------
# 自由探索模式执行回调
# ---------------------------------------------------------------------------
def run_autonomous_turn(user_msg: str):
    user_display = user_msg.strip()
    turn_target = detect_target_kind(user_display, default=target)
    with chat_col:
        with st.chat_message("user", avatar="👤"):
            st.markdown(user_display)
        with st.chat_message("assistant", avatar=current_expert.avatar):
            with st.spinner(f"{current_expert.avatar} {current_expert.name} 正在深度研判并调用专家能力..."):
                att_snippet = ""
                if st.session_state.get("current_attachment"):
                    att = st.session_state.current_attachment
                    att_snippet = att.get("text_snippet", "")

                try:
                    res = st.session_state.autonomous_agent.step(
                        user_display,
                        target_kind=turn_target,
                        skill_id=skill_id.strip() or None,
                        history=st.session_state.messages,
                        current_outline=st.session_state.chat_state.draft,
                        current_content=st.session_state.chat_state.content,
                        attachment_text=att_snippet,
                    )

                    # 状态同步到界面看板
                    if res.outline:
                        res.outline["target_kind"] = res.target_kind
                        st.session_state.chat_state.draft = res.outline
                    if res.content:
                        res.content["target_kind"] = res.target_kind
                        st.session_state.chat_state.content = res.content
                    if res.rendered_file:
                        existing = next(
                            (d for d in st.session_state.downloads if d["name"] == res.rendered_file["name"]), None
                        )
                        if existing:
                            existing["data"] = res.rendered_file["data"]
                        else:
                            st.session_state.downloads.append(res.rendered_file)

                    # 同步激活的选项式追问 (Option Chips)
                    st.session_state.active_follow_ups = res.follow_up_questions or []

                    # 格式化展示内容
                    asst_parts = []
                    if res.thought:
                        asst_parts.append(f"💭 *{current_expert.name} 决策思考*: {res.thought}")
                    asst_parts.append(res.reply_text)
                    if res.rendered_file:
                        kind_label = (
                            "Word 文档 (.docx)"
                            if res.target_kind == "document"
                            else ("演示文稿 (.pptx)" if res.target_kind == "presentation" else "文本文件")
                        )
                        asst_parts.append(
                            f"📦 **已完成文件渲染**：`{res.rendered_file['name']}`（{kind_label}，前往右侧看板「💾 文件下载」查收）"
                        )

                    asst_msg = "\n\n".join(asst_parts)
                    st.session_state.messages.append({"role": "user", "text": user_display})
                    st.session_state.messages.append(
                        {
                            "role": "assistant",
                            "text": asst_msg,
                            "avatar": current_expert.avatar,
                            "expert": current_expert.name,
                        }
                    )
                    st.rerun()
                except Exception as exc:
                    err_msg = f"❌ 专家处理失败: {exc}"
                    st.error(err_msg)
                    st.session_state.messages.append({"role": "user", "text": user_display})
                    st.session_state.messages.append({"role": "assistant", "text": err_msg, "avatar": "⚠️"})
                    st.rerun()


# ---------------------------------------------------------------------------
# 专业模式动作执行回调
# ---------------------------------------------------------------------------
def run_action(
    operation: str,
    message: str = "",
    extra_payload: dict | None = None,
    override_target: str | None = None,
):
    extra = extra_payload or {}
    op_labels = {
        "clarify": "需求澄清",
        "create_outline": "生成大纲",
        "revise_outline": "修订大纲",
        "explain_outline": "解释大纲",
        "confirm_outline": "确认大纲",
        "generate": "生成内容",
    }
    user_display = message if message else f"👉 执行操作：【{op_labels.get(operation, operation)}】"

    # 获取当前挂载的原材料文件 ID（若有）
    current_file_ids = [st.session_state.current_attachment["file_id"]] if st.session_state.get("current_attachment") else []

    # 确定执行目标形态
    existing_target = (st.session_state.chat_state.draft or {}).get("target_kind")
    effective_target = (
        existing_target
        if (existing_target and operation != "create_outline")
        else (override_target or existing_target or target)
    )

    # 1. 在界面渲染用户发出的消息
    with chat_col:
        with st.chat_message("user", avatar="👤"):
            st.markdown(user_display)
        with st.chat_message("assistant", avatar=current_expert.avatar):
            with st.spinner(f"{current_expert.avatar} {current_expert.name} 正在推进【{op_labels.get(operation, operation)}】..."):
                try:
                    result = client.execute(
                        operation,
                        target_kind=effective_target,
                        message=message,
                        state=st.session_state.chat_state,
                        current_file_ids=current_file_ids,
                        draft_id=(st.session_state.chat_state.draft or {}).get("draft_id"),
                        expected_outline_version=(st.session_state.chat_state.draft or {}).get("outline_version"),
                        skill_id=skill_id.strip() or None,
                        **extra,
                    )
                    # 确保 draft 记录有效的 target_kind
                    if st.session_state.chat_state.draft and "target_kind" not in st.session_state.chat_state.draft:
                        st.session_state.chat_state.draft["target_kind"] = effective_target

                    # 同步附件下载缓存
                    for artifact in st.session_state.chat_state.artifacts:
                        fid = artifact.get("file_id")
                        fname = artifact.get("filename", fid)
                        if fid and not any(d["id"] == fid for d in st.session_state.downloads):
                            data = client.artifact(fid)
                            st.session_state.downloads.append({"id": fid, "name": fname, "data": data})

                    asst_text = format_assistant_message(st.session_state.chat_state, operation)
                    st.session_state.messages.append({"role": "user", "text": user_display})
                    st.session_state.messages.append(
                        {
                            "role": "assistant",
                            "text": asst_text,
                            "avatar": current_expert.avatar,
                            "expert": current_expert.name,
                        }
                    )
                    st.rerun()
                except ChatClientError as exc:
                    st.session_state.messages.append({"role": "user", "text": user_display})
                    att_name = (st.session_state.get("current_attachment") or {}).get("filename", "所选附件")

                    if exc.code == "ATTACHMENT_INVALID":
                        err_text = (
                            f"❌ **参考材料解析失败 (ATTACHMENT_INVALID)**\n\n"
                            f"后端无法解析所挂载的附件 `{att_name}`，主要原因包括：\n"
                            "1. **PDF 为图片/扫描件**：PDF 内部无文字图层（系统当前未开启 OCR 识别）；\n"
                            "2. **Word 文档无正文**：文档内无段落或表格文字；\n"
                            "3. **文件为空或字数超限**：文件为 0 字节，或提取文字超过上限。\n\n"
                            "👉 请移除该附件后直接输入纯文本继续。"
                        )
                        st.error(f"❌ 附件解析失败 ({att_name})")
                    elif exc.code == "MATERIAL_OUTLINE_CONFLICT":
                        outline_title = (st.session_state.chat_state.draft or {}).get("title", "当前大纲")
                        err_text = (
                            f"⚠️ **参考材料与大纲主题冲突**\n\n"
                            f"挂载的材料 `{att_name}` 与当前确认的大纲主题（「{outline_title}」）严重不符。"
                        )
                        st.error(f"⚠️ 材料与大纲冲突")
                    elif exc.code == "TOPIC_REQUIRED":
                        err_text = "⚠️ **尚未明确写作主题**：请在输入框中简述主题后再生成大纲。"
                        st.error("⚠️ 需要明确写作主题")
                    elif exc.code == "OUTLINE_NOT_CONFIRMED":
                        err_text = "⚠️ **大纲尚未确认锁定**：请先发送「确认大纲」锁定结构后，再生成内容。"
                        st.error("⚠️ 大纲尚未确认锁定")
                    elif exc.code == "OUTLINE_REQUIRED":
                        err_text = "⚠️ **大纲尚未生成**：请先执行生成大纲操作。"
                        st.error("⚠️ 大纲尚未生成")
                    else:
                        err_text = f"❌ 调用失败 ({exc.code or 'ERROR'}): {exc}"
                        st.error(f"❌ 请求失败: {exc}")

                    st.session_state.messages.append({"role": "assistant", "text": err_text, "avatar": "⚠️"})
                    st.rerun()


# ---------------------------------------------------------------------------
# 左侧：交互式对话区
# ---------------------------------------------------------------------------
with chat_col:
    st.subheader("💬 对话工作区")
    chat_container = st.container()

    with chat_container:
        if not st.session_state.messages:
            welcome_intro = (
                f"👋 您好！我是 **{current_expert.name}**（{current_expert.title}）。\n\n"
                f"{current_expert.intro}\n\n"
                f"💡 **快速开启灵感**：请在下方输入您的写作需求或主题（例如：*帮我写一份2026年微服务架构演进方案* 或 *做一份季度工作总结汇报PPT*），我将为您全力以赴！"
            )
            with st.chat_message("assistant", avatar=current_expert.avatar):
                st.markdown(welcome_intro)

        for item in st.session_state.messages:
            msg_avatar = item.get("avatar") or ("👤" if item["role"] == "user" else current_expert.avatar)
            with st.chat_message(item["role"], avatar=msg_avatar):
                st.markdown(item["text"])

        # 渲染自由探索模式下的 3-5 项结构化追问与交互式选项胶囊 (Option Chips)
        follow_ups = st.session_state.get("active_follow_ups", [])
        if is_autonomous and follow_ups:
            chips_title_color = "#0071e3" if not is_dark else "#2997ff"
            st.markdown(
                f"""
                <div style="background: rgba(0, 113, 227, 0.06); border: 1px solid rgba(0, 113, 227, 0.2); border-radius: 14px; padding: 12px 18px; margin: 16px 0 10px 0;">
                    <div style="font-size: 13px; font-weight: 600; color: {chips_title_color}; margin-bottom: 2px;">
                        💡 专家建议与快速选项 · 点击一键带入交互
                    </div>
                    <div style="font-size: 11px; color: {text_sub};">可直接点击备选标签快速回复，无需手动敲字：</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            for q_idx, q_item in enumerate(follow_ups, 1):
                q_text = q_item.get("question", "")
                options = q_item.get("options", [])
                st.markdown(f"**{q_text}**")
                cols = st.columns(min(len(options), 4))
                for c_i, opt in enumerate(options):
                    btn_key = f"chip_{q_idx}_{c_i}_{abs(hash(opt)) % 100000}"
                    if cols[c_i % len(cols)].button(f"👉 {opt}", key=btn_key, use_container_width=True):
                        st.session_state.active_follow_ups = []
                        run_autonomous_turn(f"针对【{q_text}】，我的选择是：{opt}")

    # 快捷操作建议区（专业模式下渲染）
    guidance = state.guidance or {}
    questions = guidance.get("questions", [])
    proposals = guidance.get("proposals", [])

    if not is_autonomous and (questions or proposals):
        with st.expander("💡 快捷采纳建议", expanded=True):
            for q in questions:
                opts = q.get("options", [])
                if opts:
                    st.caption(f"针对问题「{q.get('text')}」，可直接选择推荐方案：")
                    opt_cols = st.columns(min(len(opts), 3))
                    for i, opt in enumerate(opts):
                        if opt_cols[i % len(opt_cols)].button(
                            opt, key=f"btn_opt_{q['question_id']}_{i}", use_container_width=True
                        ):
                            run_action("clarify", opt, {"answers": {q["question_id"]: opt}})

            for p in proposals:
                p_col1, p_col2 = st.columns([3, 1])
                p_col1.markdown(f"**建议设定** `{p.get('field')}` = `{p.get('value')}` *({p.get('reason')})*")
                if p_col2.button("✅ 采纳此建议", key=f"btn_prop_{p['proposal_id']}", use_container_width=True):
                    run_action(
                        "clarify",
                        f"采纳建议：{p.get('field')}为{p.get('value')}",
                        {"accepted_proposal_ids": [p["proposal_id"]]},
                    )

    st.write("")

    # -----------------------------------------------------------------------
    # 附件管理与上传区域
    # -----------------------------------------------------------------------
    if st.session_state.get("current_attachment"):
        att = st.session_state.current_attachment
        att_c1, att_c2 = st.columns([4, 1])
        att_c1.info(f"📎 **已挂载参考材料**: `{att['filename']}` ({att['size_str']})")
        if att_c2.button("🗑️ 移除附件", key="btn_remove_att", use_container_width=True, help="移除当前材料并恢复纯文本交互"):
            st.session_state.current_attachment = None
            st.session_state.uploader_key += 1
            st.rerun()

        # 智能快捷操作提示
        stem_name = Path(att["filename"]).stem
        target_name = "演示幻灯片 PPT" if target == "presentation" else ("Word 文档" if target == "document" else "纯正文")
        if is_autonomous:
            if st.button(
                f"⚡ 快捷指令：依据《{stem_name}》一键直接生成【{target_name}】",
                key="btn_quick_auto_gen_from_att",
                type="primary",
                use_container_width=True,
            ):
                run_autonomous_turn(f"请依据参考材料《{stem_name}》的核心内容，直接生成一份完整的【{target_name}】，并输出可编辑文件供下载。")
        else:
            if not state.draft:
                if st.button(
                    f"📑 快捷指令：以《{stem_name}》为核心主题制定大纲",
                    key="btn_quick_outline_from_att",
                    type="primary",
                    use_container_width=True,
                ):
                    run_action("create_outline", f"请以《{stem_name}》为主题，提炼核心结构制定大纲")

    with st.expander(
        "📎 上传参考原材料（TXT / MD / PDF / DOCX）",
        expanded=not bool(st.session_state.get("current_attachment")),
    ):
        st.caption("💡 提示：支持 .txt、.md、.docx、.pdf。PDF 需含可选中文字，单文件上限 20MB。")
        uploaded = st.file_uploader(
            "选择文件",
            type=["txt", "md", "pdf", "docx"],
            key=f"uploader_{st.session_state.uploader_key}",
            label_visibility="collapsed",
        )
        if uploaded is not None:
            file_bytes = uploaded.getvalue()
            file_hash = f"{uploaded.name}_{len(file_bytes)}"
            if (
                not st.session_state.get("current_attachment")
                or st.session_state.current_attachment.get("hash") != file_hash
            ):
                ok, reason = validate_attachment(uploaded.name, file_bytes)
                if not ok:
                    st.error(f"⚠️ 文件预检未通过: {reason}")
                else:
                    with st.spinner(f"正在上传材料「{uploaded.name}」到 BFF 网关..."):
                        try:
                            uploaded_res = client.upload(
                                uploaded.name, file_bytes, uploaded.type, session_id=state.session
                            )
                            size_kb = max(round(len(file_bytes) / 1024, 1), 0.1)

                            text_snippet = ""
                            try:
                                if uploaded.name.endswith((".txt", ".md")):
                                    text_snippet = file_bytes.decode("utf-8-sig", errors="ignore")[:20000]
                                elif uploaded.name.endswith(".pdf"):
                                    from pypdf import PdfReader

                                    text_snippet = "\n".join(
                                        p.extract_text() or "" for p in PdfReader(io.BytesIO(file_bytes)).pages
                                    )[:20000]
                                elif uploaded.name.endswith(".docx"):
                                    from docx import Document

                                    text_snippet = "\n".join(
                                        p.text for p in Document(io.BytesIO(file_bytes)).paragraphs
                                    )[:20000]
                            except Exception:
                                pass

                            st.session_state.current_attachment = {
                                "file_id": uploaded_res["file_id"],
                                "filename": uploaded.name,
                                "size_str": f"{size_kb} KB",
                                "hash": file_hash,
                                "text_snippet": text_snippet,
                            }
                            st.success(f"✅ 成功挂载材料: {uploaded.name} ({size_kb} KB)")
                            st.rerun()
                        except Exception as upload_err:
                            st.error(f"❌ 附件上传到网关失败: {upload_err}")

    # 专业模式操作快捷指令栏
    if not is_autonomous:
        st.caption("手动快捷动作（亦可直接在下方对话框用自然语言表达，模型将自动判断并流转）：")
        operations = ["clarify", "create_outline", "revise_outline", "explain_outline", "confirm_outline", "generate"]
        operation_labels = {
            "clarify": "💬 补充澄清",
            "create_outline": "📑 生成大纲",
            "revise_outline": "✍️ 修订大纲",
            "explain_outline": "❓ 解释大纲",
            "confirm_outline": "🔒 确认大纲",
            "generate": "🚀 生成内容",
        }

        confirmed = guidance.get("stage") == "confirmed" or bool(state.draft and state.draft.get("confirmed_hash"))
        enabled = {
            "clarify": True,
            "create_outline": True,
            "revise_outline": bool(state.draft),
            "explain_outline": bool(state.draft),
            "confirm_outline": bool(state.draft) and not confirmed,
            "generate": bool(state.draft) and confirmed,
        }

        btn_cols = st.columns(6)
        chosen_op = None
        for op, col in zip(operations, btn_cols):
            is_primary = (
                (op == "create_outline" and current_stage_idx == 1)
                or (op == "confirm_outline" and current_stage_idx == 2)
                or (op == "generate" and current_stage_idx == 3)
            )
            if col.button(
                operation_labels[op],
                key=f"op_{op}",
                disabled=not enabled[op],
                type="primary" if is_primary else "secondary",
                use_container_width=True,
            ):
                chosen_op = op
    else:
        chosen_op = None
        st.caption(
            f"💬 **自由探索提示**：直接向【{current_expert.name}】提出任何创作想法或任务需求。专家将深度解析并提供针对性选项建议。"
        )

    # 聊天输入框
    prompt = st.chat_input("输入你的需求、修改意见、自由探讨，或直接下达创作指令...")

    if is_autonomous:
        if prompt:
            st.session_state.active_follow_ups = []
            run_autonomous_turn(prompt)
    else:
        if prompt or chosen_op:
            op_to_run = chosen_op
            msg_to_send = (prompt or "").strip()
            override_target = None

            # 针对用户在对话框直接输入的自然语言，使用大模型意图路由器智能决策状态流转！
            if not op_to_run and prompt:
                with st.spinner("🤖 AI 正在智能研判意图并驱动状态流转..."):
                    route_res = route_professional_intent(
                        prompt,
                        stage=(state.guidance or {}).get("stage", "clarify"),
                        has_draft=bool(state.draft),
                        is_confirmed=(state.guidance or {}).get("stage") == "confirmed"
                        or bool(state.draft and state.draft.get("confirmed_hash")),
                        current_title=(state.draft or {}).get("title", ""),
                        agent=st.session_state.autonomous_agent,
                    )
                    op_to_run = route_res.get("operation", "clarify")
                    msg_to_send = route_res.get("clean_message", prompt)
                    override_target = route_res.get("override_target")

                    op_name = {
                        "clarify": "需求澄清",
                        "create_outline": "生成大纲",
                        "revise_outline": "修订大纲",
                        "explain_outline": "解释大纲",
                        "confirm_outline": "确认大纲",
                        "generate": "生成内容",
                    }.get(op_to_run, op_to_run)

                    st.toast(f"🤖 AI 语义研判：自动流转至【{op_name}】")

            # 针对 create_outline 检查是否缺少主题与意图形态推断
            if op_to_run == "create_outline":
                if msg_to_send:
                    override_target = detect_target_kind(msg_to_send, default=target)
                current_topic = getattr(state, "requirements", {}).get("topic", "")
                if not current_topic and not msg_to_send:
                    att = st.session_state.get("current_attachment")
                    if att:
                        stem_name = Path(att["filename"]).stem
                        msg_to_send = f"请以参考材料《{stem_name}》为核心主题制定大纲"
                        override_target = detect_target_kind(msg_to_send, default=target)
                    else:
                        st.warning("⚠️ 制定大纲需要明确主题。请在对话框中输入您想要的主题，再继续生成大纲。")
                        st.stop()

            # 确认与生成阶段不需要额外用户指令
            if op_to_run in {"confirm_outline", "generate"}:
                msg_to_send = ""

            run_action(op_to_run, msg_to_send, override_target=override_target)


# ---------------------------------------------------------------------------
# 右侧：实时产物工作台看板 (Apple Studio Inspector)
# ---------------------------------------------------------------------------
with panel_col:
    st.subheader("📑 创作看板")

    tab_outline, tab_content, tab_download, tab_council = st.tabs(
        ["📑 结构大纲", "📖 交付正文", "💾 文件下载", "👥 专家智囊团"]
    )

    with tab_outline:
        if state.draft:
            draft = state.draft
            is_locked = bool(draft.get("confirmed_hash"))
            st.markdown(f"### 📑 {draft.get('title', '未命名大纲')}")
            badge_confirmed = "🔒 **已锁定确认**" if is_locked else "⏳ **草稿待确认**"
            st.caption(
                f"版本: `v{draft.get('outline_version', 1)}` | 目标形态: `{draft.get('target_kind', target)}` | 状态: {badge_confirmed}"
            )

            nodes = draft.get("nodes", [])
            if not nodes:
                st.info("大纲中暂无章节节点")
            for idx, node in enumerate(nodes, 1):
                nid = node.get("node_id", f"node-{idx}")
                ntitle = node.get("title", f"第 {idx} 节")
                st.markdown(
                    f"""
                    <div style="background: {card_bg}; border: 1px solid {card_border}; border-radius: 12px; padding: 12px 16px; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; box-shadow: {card_shadow};">
                        <span style="font-weight: 600; font-size: 14px; color: {text_main};">{idx}. 📌 {ntitle}</span>
                        <code style="font-size: 11px; background: {tag_bg}; border: 1px solid {tag_border}; padding: 3px 8px; border-radius: 6px; color: {text_main}; font-weight: 500;">{nid}</code>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        else:
            st.info("💡 尚未生成大纲。请在左侧对话区提出写作构想或上传参考材料。")

    with tab_content:
        if state.content:
            content = state.content
            st.markdown(f"## 📖 {content.get('title', '交付物正文')}")
            if content.get("summary"):
                st.info(f"**核心摘要**：{content.get('summary')}")

            sections = content.get("sections", [])
            for idx, sec in enumerate(sections, 1):
                st.markdown(f"### {idx}. {sec.get('title', '')}")
                body = sec.get("body", "")
                if body:
                    st.markdown(body)
                notes = sec.get("notes", "")
                if notes:
                    with st.expander(f"📝 查看第 {idx} 节阐述与演讲备注", expanded=False):
                        st.caption(notes)
                st.divider()
        else:
            st.info("💡 正文内容尚未生成。当大纲确认后，AI 将自动出稿并排版。")

    with tab_download:
        st.markdown("### 💾 交付成果文件导出")
        if not st.session_state.downloads:
            st.info("暂无生成好的文件可供下载。")
        else:
            for item in st.session_state.downloads:
                fname = item["name"]
                fdata = item["data"]
                size_kb = max(round(len(fdata) / 1024, 1), 0.1)
                is_docx = fname.endswith(".docx")
                is_pptx = fname.endswith(".pptx")
                icon = "📄" if is_docx else ("📊" if is_pptx else "📝")
                badge_type = "Word 文档 (.docx)" if is_docx else ("演示幻灯片 (.pptx)" if is_pptx else "文本文件")

                st.markdown(
                    f"""
                    <div style="background: {card_bg}; border: 1px solid {card_border}; border-radius: 14px; padding: 14px 18px; margin-bottom: 12px; display: flex; align-items: center; justify-content: space-between; box-shadow: {card_shadow};">
                        <div>
                            <div style="font-size: 15px; font-weight: 600; color: {text_main};">{icon} {fname}</div>
                            <div style="font-size: 12px; color: {text_sub}; margin-top: 3px;">格式: {badge_type} ｜ 大小: {size_kb} KB</div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                mime = (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    if is_docx
                    else (
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                        if is_pptx
                        else "application/octet-stream"
                    )
                )
                st.download_button(
                    label=f"⬇️ 立即下载 {fname}",
                    data=fdata,
                    file_name=fname,
                    mime=mime,
                    key=f"dl_btn_{item['id']}",
                    use_container_width=True,
                    type="primary",
                )

    with tab_council:
        st.markdown("### 👥 专家顾问团队成员名录")
        st.caption("GenSlide AI 写作工作台联合业界专家顾问，提供各细分领域的专业能力支撑：")

        for exp in all_experts:
            is_current = exp.id == current_expert.id
            border_style = (
                "border: 1.5px solid #0071e3; box-shadow: 0 4px 18px rgba(0, 113, 227, 0.15);"
                if is_current
                else f"border: 1px solid {card_border};"
            )
            current_tag = '<span style="background: #0071e3; color: white; padding: 2px 8px; border-radius: 9999px; font-size: 11px; font-weight: 600;">🟢 当前坐镇</span>' if is_current else ""
            t_html = " ".join(
                f'<span style="background: {tag_bg}; color: {text_main}; border: 1px solid {tag_border}; border-radius: 4px; padding: 2px 8px; font-size: 10px; font-weight: 500;">{t}</span>'
                for t in exp.tags
            )

            st.markdown(
                f"""
                <div style="background: {card_bg}; {border_style} border-radius: 14px; padding: 14px 16px; margin-bottom: 12px; box-shadow: {card_shadow};">
                    <div style="display: flex; align-items: center; justify-content: space-between;">
                        <div style="display: flex; align-items: center; gap: 10px;">
                            <span style="font-size: 24px;">{exp.avatar}</span>
                            <div>
                                <strong style="font-size: 14px; color: {text_main};">{exp.name}</strong>
                                <div style="font-size: 11px; color: {text_sub};">{exp.title}</div>
                            </div>
                        </div>
                        <div>{current_tag}</div>
                    </div>
                    <div style="font-size: 12px; color: {text_caption}; margin-top: 8px; line-height: 1.5;">
                        {exp.intro}
                    </div>
                    <div style="margin-top: 8px; display: flex; flex-wrap: wrap; gap: 4px;">
                        {t_html}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
