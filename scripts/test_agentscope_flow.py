#!/usr/bin/env python3
"""
AgentScope 本地全流程（澄清 -> 建纲 -> 确认 -> 生成）冒烟测试脚本。
直接调用运行中的 mock_bff (8010) 与 genslide-agentscope (8002)。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from frontend.service_chat_client import ChatClient, ChatState

def main():
    bff_url = "http://127.0.0.1:8010"
    service_url = "http://127.0.0.1:8002"
    token = "local-development-token-at-least-32-characters"

    print("==========================================================")
    print("  开始 AgentScope 端到端交互链路冒烟测试")
    print("==========================================================")

    client = ChatClient(bff_url, service_url, token, engine="agentscope")
    state = ChatState(engine="agentscope")

    # 1. 澄清阶段 (clarify)
    print("\n[第 1 轮] 提交需求并执行 clarify...")
    client.execute("clarify", target_kind="writing", message="写一份2026年微服务与云原生架构演进报告，面向架构师", state=state)
    print(f"✔ 状态阶段: {state.guidance.get('stage')}")
    print(f"✔ 引导总结: {state.guidance.get('summary')}")
    questions = state.guidance.get("questions", [])
    if questions:
        print(f"✔ Agent 提出的澄清问题: {questions[0].get('text')}")

    # 2. 建纲阶段 (create_outline)
    print("\n[第 2 轮] 请求生成大纲 (create_outline)...")
    client.execute("create_outline", target_kind="writing", message="按照标准技术白皮书风格生成大纲", state=state)
    draft = state.draft or {}
    print(f"✔ 大纲标题: {draft.get('title')}")
    print(f"✔ Draft ID: {draft.get('draft_id')}, Outline Version: {draft.get('outline_version')}")

    # 3. 确认大纲 (confirm_outline)
    print("\n[第 3 轮] 确认大纲 (confirm_outline)...")
    client.execute("confirm_outline", target_kind="writing", state=state,
                   draft_id=draft.get("draft_id"),
                   expected_outline_version=draft.get("outline_version"))
    print(f"✔ 状态阶段已更新为: {state.guidance.get('stage')}")

    # 4. 生成正文 (generate)
    print("\n[第 4 轮] 生成完整正文 (generate)...")
    client.execute("generate", target_kind="writing", state=state,
                   draft_id=draft.get("draft_id"),
                   expected_outline_version=draft.get("outline_version"))
    content = state.content or {}
    title = content.get("title") or draft.get("title")
    print(f"✔ 文档正文生成完成！标题: {title}")
    if content.get("sections"):
        print(f"✔ 生成段落数: {len(content['sections'])}")

    print("\n==========================================================")
    print("  🎉 全流程测试通过！全链路交互正常！")
    print("==========================================================")

if __name__ == "__main__":
    main()
