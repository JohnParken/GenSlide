"""Repeatable, opt-in live chat evaluation using synthetic materials only.

Run with --dry-run to inspect cases without making model calls. Live execution
uses the same MODEL_* / TL proxy configuration as the workbench.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CASES = [
    {
        "name": "discussion_and_memory",
        "turns": [
            "你好",
            "我们讨论一份试点方案，受众是董事会，风格务实，禁止编造数据。先讨论，不生成文件。",
            "我比较担心落地成本，先帮我分析要考虑什么。",
            "再讨论一下实施风险。",
            "如果资源有限，应该怎样安排优先级？",
            "先不要出稿，解释一下验收标准应该怎么确定。",
            "我们讨论一下如何避免重复建设。",
            "你认为前面几个方向中，哪个最需要先验证？",
            "现在请回顾我最早说的受众和写作风格，仍然不要生成文件。",
        ],
        "no_artifact": True,
        "final_mentions": ["董事会", "务实"],
    },
    {
        "name": "scoped_revision",
        "turns": ["只改第二章，把原文改成更清楚的执行建议，其他章节一字不动。"],
        "content": {"title": "试点方案", "sections": [
            {"title": "第一章：目标", "body": "第一章保留标记：只讨论小范围试点。", "notes": ""},
            {"title": "第二章：实施", "body": "先明确负责人，再确定验收条件，最后开展试点。", "notes": ""},
            {"title": "第三章：约束", "body": "第三章保留标记：不新增未经批准的预算。", "notes": ""},
        ]},
        "preserve_indices": [0, 2],
        "changed_index": 1,
    },
    {
        "name": "late_material_and_length",
        "material": "项目背景：试点部署。\n" + "附录说明：以下信息只用于背景理解。\n" * 1400
                    + "\n材料末尾的关键事实：本次试点预算为73万元，负责人为林岚。\n",
        "turns": ["依据材料直接生成约1200字的纯文本试点报告。准确使用材料末尾的预算和负责人，不编造其他数据。"],
        "content_mentions": ["73", "林岚"],
        "length": 1200,
    },
]


async def evaluate(cases: list[dict]) -> list[dict]:
    from frontend.autonomous_agent import AutonomousAgent

    agent = AutonomousAgent()
    reports = []
    for case in cases:
        memory, history, outline = {}, [], None
        original = deepcopy(case.get("content"))
        content = deepcopy(original)
        started = time.monotonic()
        checks, turns = {}, []
        try:
            for index, message in enumerate(case["turns"], 1):
                print(f"{case['name']}: turn {index}/{len(case['turns'])}", flush=True)
                result = await agent.astep(
                    message, memory=memory, history=history, current_outline=outline,
                    current_content=content, attachment_text=case.get("material", ""),
                )
                memory = result.memory
                outline = result.outline or outline
                content = result.content or content
                history.extend([{"role": "user", "text": message}, {"role": "assistant", "text": result.reply_text}])
                turns.append({"message": message, "reply": result.reply_text, "action": result.skill,
                              "questions": len(result.follow_up_questions), "warnings": result.quality_warnings})
                checks[f"turn_{index}_question_limit"] = len(result.follow_up_questions) <= 2
                if case.get("no_artifact"):
                    checks[f"turn_{index}_no_artifact"] = result.rendered_file is None
            body = "\n".join(s["body"] for s in (content or {}).get("sections", []))
            for phrase in case.get("final_mentions", []):
                checks[f"remembers_{phrase}"] = phrase in turns[-1]["reply"]
            for phrase in case.get("content_mentions", []):
                checks[f"material_{phrase}"] = phrase in body
            for index in case.get("preserve_indices", []):
                checks[f"preserves_section_{index + 1}"] = bool(content) and content["sections"][index] == original["sections"][index]
            if "changed_index" in case:
                index = case["changed_index"]
                checks["requested_section_changed"] = bool(content) and content["sections"][index] != original["sections"][index]
            if "length" in case:
                chars = len("".join(body.split()))
                checks["length_within_15_percent"] = case["length"] * .85 <= chars <= case["length"] * 1.15
            reports.append({"case": case["name"], "passed": all(checks.values()), "checks": checks,
                            "seconds": round(time.monotonic() - started, 2), "turns": turns, "content": content})
        except Exception as exc:
            reports.append({"case": case["name"], "passed": False, "error": str(exc), "checks": checks, "turns": turns})
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="List cases; never call the model")
    parser.add_argument("--case", choices=[case["name"] for case in CASES])
    parser.add_argument("--output", type=Path, help="Write the report to this JSON file")
    args = parser.parse_args()
    cases = [case for case in CASES if not args.case or case["name"] == args.case]
    if args.dry_run:
        print(json.dumps([{ "name": c["name"], "turns": len(c["turns"]), "material_chars": len(c.get("material", ""))} for c in cases], ensure_ascii=False, indent=2))
        return
    results = asyncio.run(evaluate(cases))
    report = json.dumps(results, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"Report: {args.output}")
    else:
        print(report)
    if not all(result["passed"] for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
