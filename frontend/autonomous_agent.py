"""Autonomous Agent for GenSlide.

Operates in free-form conversation mode where the LLM autonomously decides
when to chat, when to draft outlines, and when to generate deliverables
using its internal skill set, without strict state machine enforcement.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# Ensure services modules are importable
_REPO_ROOT = Path(__file__).resolve().parents[1]
_AGENTSCOPE_SRC = _REPO_ROOT / "services" / "genslide-agentscope" / "src"
if str(_AGENTSCOPE_SRC) not in os.sys.path:
    os.sys.path.insert(0, str(_AGENTSCOPE_SRC))

from genslide_agentscope.content_io import render_content
from genslide_agentscope.skills import SkillRegistry
from genslide_agentscope.tl_provider import TLProvider


@dataclass
class AutonomousResult:
    thought: str = ""
    skill: str = "reply"  # "reply" | "outline" | "generate"
    target_kind: str = "document"  # "document" | "presentation" | "writing"
    reply_text: str = ""
    follow_up_questions: list[dict[str, Any]] = field(default_factory=list)
    outline: dict[str, Any] | None = None
    content: dict[str, Any] | None = None
    rendered_file: dict[str, Any] | None = None  # {"id": str, "name": str, "data": bytes}


PPT_KEYWORDS = ["ppt", "pptx", "幻灯片", "演示文稿", "演示幻灯片", "幻灯", "胶片", "slide", "slides", "presentation"]
DOC_KEYWORDS = [
    "文档", "word", "docx", "报告", "公文", "方案", "文章", "总结",
    "材料", "资料", "简报", "白皮书", "正文", "说明书", "手册", "doc", "document"
]
PPT_NEGATION_REGEX = re.compile(
    r"(?:不|不要|别|无需|不用|不需要|非|免|勿|不是|并非|不要做成|不生成)\s{0,3}(?:做成|生成|要|是)?\s{0,3}(?:ppt|pptx|幻灯片|演示文稿|胶片|slide)",
    re.IGNORECASE,
)


def detect_target_kind(text: str, default: str = "document") -> str:
    """Detect delivery target kind based on user intent.

    Core rule: Default to 'document' (Word/WPS .docx). ONLY produce 'presentation'
    when the user explicitly and affirmatively requests PPT / slides / presentation.
    """
    raw = text.strip().lower()
    if not raw:
        return default or "document"

    # Check if PPT is explicitly negated (e.g. "不要PPT", "不是PPT", "生成文档不要做成PPT")
    has_ppt_negation = bool(PPT_NEGATION_REGEX.search(raw))

    # Check if PPT keywords exist affirmatively
    has_ppt_kw = any(kw in raw for kw in PPT_KEYWORDS)
    is_explicit_ppt = has_ppt_kw and not has_ppt_negation

    # Check if document keywords exist
    has_doc_kw = any(kw in raw for kw in DOC_KEYWORDS)

    # Check if writing / plain text keywords exist
    is_writing = any(kw in raw for kw in ["纯正文", "纯文本", "只输出文本", "无需文件", "markdown"])
    if is_writing:
        return "writing"

    if is_explicit_ppt and not has_doc_kw:
        return "presentation"

    if is_explicit_ppt and has_doc_kw:
        # Both appear: e.g. "把文档做成PPT" -> presentation; "生成文档不是PPT" -> document
        if any(
            re.search(p, raw)
            for p in [
                r"(把|将|由).{0,6}(文档|材料).{0,6}(做成|转成|生成|制成).{0,4}(ppt|幻灯片|演示文稿)",
                r"(做成|生成|转成).{0,4}(ppt|幻灯片|演示文稿)",
            ]
        ):
            return "presentation"
        return "document"

    if has_doc_kw or has_ppt_negation:
        return "document"

    # Default fallback: only allow presentation if explicit default is requested and text doesn't contradict
    return default or "document"


AUTONOMOUS_SYSTEM_PROMPT = """你是一个专业的智能创作与文档/幻灯片生成 Agent。
你与用户进行自然语言对话，并完全自主决定何时进行自由交流、何时制定/调整大纲、何时直接生成交付物。

【核心交付形态规则（至关重要）】：
1. 默认交付物形态必须是【可编辑的 Word/WPS 文档】(target_kind: "document")！
2. 【严禁擅自生成 PPT】：只有当用户在指令中【明确要求生成 PPT、幻灯片、演示文稿或胶片】时，才可生成 presentation！
3. 若用户说“帮我生成文档”、“写一份报告”、“整理材料”、“撰写方案”或未明确提及 PPT，一律生成 document（Word 文档）！
4. 在输出的 JSON 中必须包含 "target_kind": "document" | "presentation" 字段，明确当前交付形态。

【你拥有的技能 (Skills)】：
1. "reply" (自由探讨/需求启发追问)：
   - 当用户提出初步构想、探讨创作思路、或提供的信息还不够完整时使用；
   - 此时除输出 thought 和 reply_text 外，必须主动深入剖析，输出 3 到 5 项结构化追问 (follow_up_questions)，每项追问必须包含 3 到 4 个具体可选项 (options)！
2. "outline" (结构大纲制定与修订)：
   - 当用户要求列出大纲、梳理目录、或针对已有大纲提出增删改查时使用；
   - 必须在 outline 字段中提供大纲标题与章节列表。
3. "generate" (正文编写与交付物直出)：
   - 当用户明确要求“直接生成”、“写一份完整的文档/报告/PPT”、“出稿”；或大纲已明确且进入正文产出阶段时使用；
   - 对于 document (Word 文档，默认形态)：每个 section 编写详实完整的段落正文，结构清晰、论述充分；
   - 对于 presentation (演示幻灯片，仅限用户明确要求PPT时)：每个 section 对应一张幻灯片，body 为 3-5 条精炼要点，notes 为演讲者详细逐字稿备注；
   - 必须同时提供完整的 outline 与 content。

【自由探索追问与选项引导规则 (至关重要)】：
当处于自由探索/交流探讨阶段 (skill 为 "reply") 时，严禁仅给出一两句简短应答！你必须主动深度挖掘用户创作意图，提出 3 到 5 项关键维度的结构化追问 (follow_up_questions)，并且每个追问必须给出 3 到 4 个具体可行、切中要害的备选选项 (options)，以便用户一键点击选择，极大降低思考与输入成本！
追问维度通常涵盖：1. 核心受众与阅读场景；2. 报告/文书的重点侧重方向；3. 篇幅规模与详略深度；4. 行业基调或语言风格。

【输出格式约束】：
你必须且只能输出严格的 JSON 字符串，不能有任何 Markdown 前导语或后置说明。结构如下：
{
  "thought": "你的内心思考：分析用户当前意图，并解释为何选择该技能与交付形态",
  "skill": "reply" | "outline" | "generate",
  "target_kind": "document" | "presentation",
  "reply_text": "在对话框呈现给用户的友好回复文本（包含对操作的说明与总结）",
  "follow_up_questions": [
    {
      "question": "1. 核心目标受众与阅读场景是什么？",
      "options": ["集团高层决策者 (战略汇报)", "跨部门业务协同 (方案推进)", "技术落地团队 (详细指引)", "外部客户/监管机构 (合规展示)"]
    },
    {
      "question": "2. 内容的核心侧重点偏向于？",
      "options": ["痛点剖析与解决举措", "商业闭环与收益预测", "技术架构与实施细节", "阶段性成果与复盘总结"]
    },
    {
      "question": "3. 期望的篇幅与详略程度？",
      "options": ["精炼速览版 (3-4节/重点突出)", "标准详实版 (5-7节/全面严谨)", "深度落地版 (8节以上/详尽论述)"]
    }
  ],
  "outline": {
    "title": "文档或幻灯片主标题",
    "nodes": [
      {"node_id": "sec-1", "title": "第一节标题"},
      {"node_id": "sec-2", "title": "第二节标题"}
    ]
  },
  "content": {
    "title": "文档或幻灯片主标题",
    "summary": "全文核心摘要（100-200字）",
    "sections": [
      {
        "title": "第一节标题",
        "body": "正文段落（document）或幻灯片核心要点（presentation）",
        "notes": "补充阐释或演讲者备注"
      }
    ]
  }
}
注：当 skill 为 "reply" 时，follow_up_questions 必须包含 3 到 5 项，outline 和 content 可为 null；当 skill 为 "outline" 时，outline 必填；当 skill 为 "generate" 时，outline 和 content 均必填。

【专业技能挂载指引】：
当用户指令或上下文中挂载了【专业创作技能已挂载生效】（如党政公文、商业报告等专用技能）时，必须严格贯彻该技能的文体、结构、语言纪律、格式范式与专业标准！
"""



def _extract_json(text: str) -> dict:
    """Robustly extract and parse JSON from model output."""
    raw = text.strip()
    if raw.startswith("```json") and raw.endswith("```"):
        raw = raw[7:-3].strip()
    elif raw.startswith("```") and raw.endswith("```"):
        raw = raw[3:-3].strip()
    try:
        return json.loads(raw)
    except Exception:
        # Fallback regex search for outer JSON object
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise ValueError(f"无法从模型输出中解析有效 JSON: {raw[:200]}")


class AutonomousAgent:
    """Autonomous agent that uses skills to manage its own state and deliverables."""

    def __init__(self):
        self.provider = os.getenv("MODEL_PROVIDER", "tl").strip().lower()
        self.base_url = os.getenv("MODEL_BASE_URL", "http://127.0.0.1:8089").strip()
        self.api_key = os.getenv("MODEL_API_KEY", "local-proxy-key").strip()
        self.model_name = os.getenv("MODEL_NAME", "deepseek-v4-flash").strip()
        self.skills = SkillRegistry()

    def list_skills(self) -> list[dict[str, Any]]:
        return self.skills.list_skills()

    def reload_skills(self) -> list[dict[str, Any]]:
        return self.skills.reload()

    async def _call_llm(self, system: str, user_prompt: str) -> str:
        if self.provider == "tl":
            provider = TLProvider(self.base_url, self.api_key, self.model_name)
            try:
                return await provider.complete(system, user_prompt)
            finally:
                await provider.aclose()
        else:
            # OpenAI-compatible completion
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            payload = {
                "model": self.model_name,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.4,
            }
            async with httpx.AsyncClient(timeout=180.0) as client:
                res = await client.post(f"{self.base_url.rstrip('/')}/chat/completions", headers=headers, json=payload)
                res.raise_for_status()
                data = res.json()
                return data["choices"][0]["message"]["content"]

    async def astep(
        self,
        user_message: str,
        *,
        target_kind: str = "document",
        skill_id: str | None = None,
        history: list[dict] | None = None,
        current_outline: dict | None = None,
        current_content: dict | None = None,
        attachment_text: str = "",
    ) -> AutonomousResult:
        """Process one conversational turn autonomously."""
        # Detect target from user message; only allow presentation if explicitly requested
        detected_target = detect_target_kind(user_message, default=target_kind or "document")
        effective_target = detected_target

        prompt_parts = [
            f"【确定生成形态目标 (Target Kind)】: {effective_target} "
            + (
                "(演示幻灯片 PPT，仅在用户明确指定要求PPT时生成)"
                if effective_target == "presentation"
                else "(可编辑 Word/WPS 文档，默认标准交付形态)"
            ),
        ]

        # Check if a specialized skill is requested
        active_skill = None
        if skill_id:
            try:
                active_skill = self.skills.get(effective_target, skill_id)
            except Exception:
                active_skill = self.skills.skills.get(skill_id)

        if active_skill:
            instruction_text = active_skill.get("instructions", {}).get("generate", "")
            prompt_parts.append(
                f"【专业创作技能已挂载生效 (Active Skill: {active_skill['skill_id']})】:\n"
                f"技能说明: {active_skill.get('description', '')}\n"
                f"专业规范要求与编写指引:\n{instruction_text}"
            )

        if current_outline:
            prompt_parts.append(f"【当前已有大纲】:\n{json.dumps(current_outline, ensure_ascii=False, indent=2)}")

        if current_content:
            sections_summary = [f"- {s.get('title')}" for s in current_content.get("sections", [])]
            prompt_parts.append(
                f"【当前已有正文章节】:\n标题: {current_content.get('title')}\n" + "\n".join(sections_summary)
            )

        if attachment_text:
            prompt_parts.append(f"【挂载的参考材料全文/摘要】:\n{attachment_text[:12000]}")

        if history:
            recent_turns = history[-6:]
            history_text = "\n".join(
                f"[{item.get('role', 'user')}]: {item.get('text', '')}" for item in recent_turns
            )
            prompt_parts.append(f"【最近对话历史】:\n{history_text}")

        prompt_parts.append(f"【用户最新输入指令】:\n{user_message}")
        user_prompt = "\n\n".join(prompt_parts)

        raw_output = await self._call_llm(AUTONOMOUS_SYSTEM_PROMPT, user_prompt)
        parsed = _extract_json(raw_output)

        thought = parsed.get("thought", "")
        skill = parsed.get("skill", "reply")
        reply_text = parsed.get("reply_text", "")
        outline = parsed.get("outline")
        content = parsed.get("content")

        # Determine target_kind: default is document, PPT ONLY if explicitly requested
        model_target = parsed.get("target_kind", effective_target)
        if effective_target == "presentation":
            final_target = "presentation"
        elif model_target == "presentation":
            # Model tried to output presentation, but user did NOT explicitly request PPT
            final_target = "document"
        else:
            final_target = effective_target or "document"

        # Fallback: if skill is generate but content is present, ensure outline is aligned
        if skill == "generate" and content and not outline:
            outline = {
                "title": content.get("title", "未命名"),
                "nodes": [
                    {"node_id": f"sec-{idx}", "title": s.get("title", f"第{idx}节")}
                    for idx, s in enumerate(content.get("sections", []), 1)
                ],
            }

        # Render deliverable if content is generated and target is docx or pptx
        rendered_file = None
        if content and final_target in {"document", "presentation"}:
            try:
                with tempfile.TemporaryDirectory() as tmp_dir:
                    rendered_path = render_content(final_target, content, Path(tmp_dir))
                    ext = "docx" if final_target == "document" else "pptx"
                    clean_title = re.sub(r'[\\/*?:"<>|]', "_", content.get("title", "deliverable"))
                    filename = f"{clean_title}.{ext}"
                    rendered_file = {
                        "id": uuid.uuid4().hex,
                        "name": filename,
                        "data": rendered_path.read_bytes(),
                    }
            except Exception as exc:
                reply_text += f"\n\n*(注: 文件格式化渲染暂未完成: {exc})*"

        follow_up_raw = parsed.get("follow_up_questions")
        follow_up_questions = []
        if isinstance(follow_up_raw, list):
            for item in follow_up_raw:
                if isinstance(item, dict) and "question" in item:
                    opts = [str(o) for o in item.get("options", []) if str(o).strip()]
                    follow_up_questions.append({
                        "question": str(item["question"]),
                        "options": opts,
                    })

        return AutonomousResult(
            thought=thought,
            skill=skill,
            target_kind=final_target,
            reply_text=reply_text,
            follow_up_questions=follow_up_questions,
            outline=outline,
            content=content,
            rendered_file=rendered_file,
        )

    def step(
        self,
        user_message: str,
        *,
        target_kind: str = "document",
        skill_id: str | None = None,
        history: list[dict] | None = None,
        current_outline: dict | None = None,
        current_content: dict | None = None,
        attachment_text: str = "",
    ) -> AutonomousResult:
        """Synchronous wrapper for Streamlit."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import nest_asyncio

            nest_asyncio.apply()
            return loop.run_until_complete(
                self.astep(
                    user_message,
                    target_kind=target_kind,
                    skill_id=skill_id,
                    history=history,
                    current_outline=current_outline,
                    current_content=current_content,
                    attachment_text=attachment_text,
                )
            )
        else:
            return asyncio.run(
                self.astep(
                    user_message,
                    target_kind=target_kind,
                    skill_id=skill_id,
                    history=history,
                    current_outline=current_outline,
                    current_content=current_content,
                    attachment_text=attachment_text,
                )
            )


ROUTER_SYSTEM_PROMPT = """你是一个智能写作工作台的状态机意图路由分析器。
你的任务是根据当前所处的写作工作台阶段以及用户输入的自然语言，研判用户真正想要执行的状态机动作 (operation)。

【当前工作台可用动作】：
1. "clarify": 用户在补充背景信息、探讨需求、说明约束，尚未明确要求生成大纲；
2. "create_outline": 用户要求生成、制定、起草大纲/目录/方案框架；
3. "revise_outline": 已有大纲，用户提出修改、补充、删除、调整特定章节或标题等修改意见；
4. "explain_outline": 用户询问大纲编排理由、设计逻辑、为什么要这样组织；
5. "confirm_outline": 用户认可、同意、批准当前大纲（例如“大纲没问题”、“可以开始写了”、“同意这个大纲”、“就按这个结构”、“确认”等）；
6. "generate": 大纲已明确或已确认，用户指令开始生成正文内容、直接出稿、写报告/做PPT。

【特殊复合意图原则】：
- 若当前已有大纲但尚未确认锁定（is_confirmed 为 false），而用户说“大纲可以，直接生成吧”或“按这个出正文”，由于系统必须先确认大纲才能生成正文，此时优先判定为 "confirm_outline"！
- 若大纲已经确认锁定（is_confirmed 为 true），而用户说“直接出稿”、“开始写”，此时判定为 "generate"！
- 若大纲已确认锁定，但用户又说“第二章改一下”，此时判定为 "revise_outline"。

【输出格式】：
严格返回纯 JSON 字符串：
{
  "operation": "clarify" | "create_outline" | "revise_outline" | "explain_outline" | "confirm_outline" | "generate",
  "reasoning": "决策理由简述",
  "clean_message": "清洗后传递给后端的有效指令文本（若是 confirm_outline 或 generate 则置为空字符串 \"\"）",
  "override_target": "document" | "presentation" | "writing" | null
}
"""


async def aroute_professional_intent(
    user_message: str,
    *,
    stage: str = "clarify",
    has_draft: bool = False,
    is_confirmed: bool = False,
    current_title: str = "",
    agent: AutonomousAgent | None = None,
) -> dict[str, Any]:
    """Intelligently route natural language to professional workflow operations using LLM with fallback."""
    raw_text = user_message.strip()
    lower_text = raw_text.lower()

    # Fast heuristic check for target kind
    target_override = detect_target_kind(raw_text, default="")
    target_override = target_override if target_override else None

    # Try LLM routing
    active_agent = agent if agent is not None else AutonomousAgent()

    user_context = (
        f"【当前工作台状态】:\n"
        f"- 当前阶段: {stage}\n"
        f"- 是否已生成大纲草案: {has_draft}\n"
        f"- 当前大纲是否已确认锁定: {is_confirmed}\n"
        f"- 当前大纲标题: {current_title or '未命名'}\n\n"
        f"【用户最新输入指令】:\n{raw_text}"
    )

    try:
        raw_output = await active_agent._call_llm(ROUTER_SYSTEM_PROMPT, user_context)
        parsed = _extract_json(raw_output)
        op = parsed.get("operation", "clarify")
        valid_ops = {"clarify", "create_outline", "revise_outline", "explain_outline", "confirm_outline", "generate"}
        if op in valid_ops:
            clean_msg = parsed.get("clean_message", raw_text)
            if op in {"confirm_outline", "generate"}:
                clean_msg = ""
            return {
                "operation": op,
                "reasoning": parsed.get("reasoning", "大模型语义分析研判"),
                "clean_message": clean_msg,
                "override_target": parsed.get("override_target") or target_override,
            }
    except Exception:
        pass

    # Heuristic fallback if LLM routing fails
    if has_draft and not is_confirmed:
        if any(kw in lower_text for kw in ["确认大纲", "锁定大纲", "大纲没问题", "大纲可以", "同意大纲", "确认", "通过", "就按这个", "开始写吧"]):
            return {"operation": "confirm_outline", "reasoning": "语义判定为确认大纲", "clean_message": "", "override_target": target_override}
        elif any(kw in lower_text for kw in ["解释大纲", "大纲说明", "说明大纲", "为什么要这样设计", "为什么这么编排"]):
            return {"operation": "explain_outline", "reasoning": "语义判定为请求解释大纲", "clean_message": raw_text, "override_target": target_override}
        elif any(kw in lower_text for kw in ["改", "修改", "增加", "删除", "调整", "替换", "补充"]):
            return {"operation": "revise_outline", "reasoning": "语义判定为修改大纲", "clean_message": raw_text, "override_target": target_override}
        else:
            return {"operation": "revise_outline", "reasoning": "未确认大纲下的反馈", "clean_message": raw_text, "override_target": target_override}
    elif has_draft and is_confirmed:
        if any(kw in lower_text for kw in ["修改大纲", "重新制定大纲", "调整大纲", "重修大纲", "重写大纲", "改大纲"]):
            return {"operation": "revise_outline", "reasoning": "已确认后重新调整大纲", "clean_message": raw_text, "override_target": target_override}
        else:
            return {"operation": "generate", "reasoning": "已确认大纲后直接出正文", "clean_message": "", "override_target": target_override}
    else:
        if any(kw in lower_text for kw in ["生成大纲", "制定大纲", "出大纲", "写大纲", "大纲", "建立大纲", "生成目录", "开始生成大纲"]) or stage == "outline":
            return {"operation": "create_outline", "reasoning": "要求生成大纲", "clean_message": raw_text, "override_target": target_override}
        return {"operation": "clarify", "reasoning": "需求澄清探讨", "clean_message": raw_text, "override_target": target_override}


def route_professional_intent(
    user_message: str,
    *,
    stage: str = "clarify",
    has_draft: bool = False,
    is_confirmed: bool = False,
    current_title: str = "",
    agent: AutonomousAgent | None = None,
) -> dict[str, Any]:
    """Synchronous wrapper for route_professional_intent."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import nest_asyncio

        nest_asyncio.apply()
        return loop.run_until_complete(
            aroute_professional_intent(
                user_message,
                stage=stage,
                has_draft=has_draft,
                is_confirmed=is_confirmed,
                current_title=current_title,
                agent=agent,
            )
        )
    return asyncio.run(
        aroute_professional_intent(
            user_message,
            stage=stage,
            has_draft=has_draft,
            is_confirmed=is_confirmed,
            current_title=current_title,
            agent=agent,
        )
    )
