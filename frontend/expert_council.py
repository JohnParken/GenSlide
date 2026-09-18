"""WorkBuddy-style Expert Advisory Council (专家团) for GenSlide AI Writing Studio.

Maps underlying skills to vivid, professional expert personas with distinct
specializations, avatars, tags, and guidance styles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExpertProfile:
    id: str
    name: str
    title: str
    avatar: str
    tags: list[str]
    intro: str
    target_kind: str | None = None  # "document" | "presentation" | "writing" | None (universal)
    accent_color: str = "#0071e3"  # Apple blue default


# Core expert personas pre-configured for built-in skills
CORE_EXPERTS: dict[str, ExpertProfile] = {
    "official-document-skill": ExpertProfile(
        id="official-document-skill",
        name="机关公文资深参赞",
        title="机关公文与综合政务资深顾问",
        avatar="🏛️",
        tags=["红头规范", "请示批复", "人民日报笔法", "严格去AI味"],
        intro="专精党政机关法定公文、通报纪要、工作方案与调研汇报。行文克制内敛、依据确凿，严格遵循公文排版与文种规范。",
        target_kind="document",
        accent_color="#b3261e",  # Official crimson
    ),
    "business-report": ExpertProfile(
        id="business-report",
        name="商业咨询战略总监",
        title="商业分析与管理咨询顾问",
        avatar="💼",
        tags=["麦肯锡金字塔", "商业闭环", "战略可行性", "数据与洞察"],
        intro="精通商业计划书、行业深度研报、战略规划及高管决策材料。擅长以金字塔结构组织论点，论证严密、层层递进。",
        target_kind="writing",
        accent_color="#0071e3",  # Apple executive blue
    ),
    "presentation": ExpertProfile(
        id="presentation",
        name="演示胶片视觉架构师",
        title="高管演示与胶片逻辑总监",
        avatar="📊",
        tags=["金字塔逻辑", "精炼卡片布局", "视觉层级", "逐字演讲备注"],
        intro="专注高层汇报与发布会级 PPT 架构设计。提炼精炼要点，结合多栏卡片网格布局，并自动撰写专业详实的演讲者逐字稿备注。",
        target_kind="presentation",
        accent_color="#5856d6",  # Indigo purple
    ),
    "document": ExpertProfile(
        id="document",
        name="方案文书架构师",
        title="标准文书与技术方案顾问",
        avatar="📑",
        tags=["Word/WPS标准", "白皮书", "系统论述", "交付规范"],
        intro="擅长撰写长篇技术白皮书、实施方案、操作手册与规范性综合文件。正文结构严整、论述详实，输出可直接编辑的 Word 文档。",
        target_kind="document",
        accent_color="#34c759",  # Fresh emerald
    ),
    "writing": ExpertProfile(
        id="writing",
        name="深度专栏资深主笔",
        title="深度内容与专栏评论主笔",
        avatar="✍️",
        tags=["思想深刻", "专栏评论", "纯正文创作", "文风洗练"],
        intro="专注思想性长文、深度专栏、行业观察与理论评论。文字凝练有力、立意高远，注重独到见解与文字表现力。",
        target_kind="writing",
        accent_color="#ff9500",  # Energetic amber
    ),
}

DEFAULT_EXPERT = ExpertProfile(
    id="default",
    name="全能创作助理",
    title="自适应智能创作导师",
    avatar="✨",
    tags=["全能自适应", "智能感知", "双模协同"],
    intro="根据您选择的交付形态（文档/PPT/正文）自动调用最适宜的专业技能与知识库，随时待命。",
    target_kind=None,
    accent_color="#0071e3",
)


def get_expert(skill_id: str | None) -> ExpertProfile:
    """Retrieve expert profile by skill ID or return default."""
    if not skill_id or skill_id.strip() in {"default", ""}:
        return DEFAULT_EXPERT
    clean_id = skill_id.strip()
    if clean_id in CORE_EXPERTS:
        return CORE_EXPERTS[clean_id]

    # Dynamic fallback for newly discovered custom skills
    return ExpertProfile(
        id=clean_id,
        name=f"领域特邀专家 · {clean_id}",
        title="特邀领域智囊",
        avatar="🧠",
        tags=["动态扩展", "领域专属", "即插即用"],
        intro=f"由技能库动态加载的专项能力专家（{clean_id}）。",
        target_kind=None,
        accent_color="#af52de",
    )


def list_all_experts(discovered_skills: list[dict[str, Any]] | None = None) -> list[ExpertProfile]:
    """List all available expert personas, combining core personas and dynamically discovered skills."""
    experts: list[ExpertProfile] = [DEFAULT_EXPERT]
    seen_ids = set()

    # Add core configured experts
    for exp in CORE_EXPERTS.values():
        experts.append(exp)
        seen_ids.add(exp.id)

    # Incorporate any dynamically discovered skills not already in core
    if discovered_skills:
        for s in discovered_skills:
            sid = s.get("skill_id")
            if sid and sid not in seen_ids:
                desc = s.get("description") or f"专项领域专家 ({sid})"
                tkind = s.get("target_kind")
                experts.append(
                    ExpertProfile(
                        id=sid,
                        name=f"特邀智囊 · {sid}",
                        title="专项领域专家",
                        avatar="💡",
                        tags=["动态技能", tkind or "通用"],
                        intro=desc[:150] + ("..." if len(desc) > 150 else ""),
                        target_kind=tkind,
                        accent_color="#0071e3",
                    )
                )
                seen_ids.add(sid)

    return experts
