"""Framework-free writing and requirement primitives shared by runtime execution."""
import json
import re
from pydantic import Field, ValidationError
from .domain import Content, Outline, Section, ServiceError, StrictModel

FIELDS = {"topic", "audience", "language", "length", "style", "purpose", "constraints"}
_OUTPUT_KIND = {"text": "writing", "document": "document", "presentation": "presentation"}

_ORDINAL_TOKENS = {
    "1": ["第一", "第1", "前一个", "首个", "A"],
    "2": ["第二", "第2", "后一个", "B"],
    "3": ["第三", "第3", "C"],
    "4": ["第四", "第4", "D"],
}
_CHINESE_NUMS = {"1": "一", "2": "二", "3": "三", "4": "四"}
PREFIX_BOUND = r"(?:^|[\s,，。；;、选按用要考虑第])"
SUFFIX_BOUND = r"(?:$|[\s,，。；;、项个种吧啦]|[^\w])"

MAX_CATALOG_PAYLOAD_BYTES = 20000
MAX_SKILL_DESC_CHARS = 120


def _is_ambiguous_comparison_question(msg: str) -> bool:
    """Detect whether user input is an ambiguous comparative inquiry rather than an affirmative directive."""
    if not msg:
        return False
    text = msg.strip()

    is_writing_topic_request = bool(
        re.search(r"^(?:请|麻烦|帮我)?\s*(?:写|作|撰写|生成|输出|制作)\s*(?:一篇|一个|份)?.*(?:对比|比较)", text)
    )
    if is_writing_topic_request:
        if re.search(r"(?:，|,)?\s*(?:可以|行|好|能|麻烦)?\s*(?:吗|呢|么)\s*[？?]?\s*$", text):
            if not re.search(r"(?:还是|或者|哪个|哪种|怎么选|如何选)", text):
                return False

    if re.search(r"(?:还是|就|按|请|决定)\s*(?:用|选|按|采用|以|决定)", text):
        if not re.search(r"(?:哪个|哪种|哪一个|怎么选|如何选|区别|对比一下)", text):
            return False

    if re.search(r"(?:吧|即可|就行|好了)$", text):
        if not re.search(r"(?:哪个|哪种|哪一个|怎么选|如何选|区别|对比一下)", text):
            return False

    inquiry_phrases = (
        "哪个好", "哪一个好", "哪个适合", "哪个更适合", "哪种更好", "怎么选", "如何选", "如何选择", "怎么选择",
        "有什么区别", "有什么不同", "区别在哪", "对比一下", "哪个更", "选哪一个", "选哪个",
        "哪种更", "哪个更优"
    )
    if any(p in text for p in inquiry_phrases):
        return True

    if re.search(r"(?:还是|或者).*(?:更好|更适合|更优|哪个好|比较好|(?:[一二三四1-4A-Za-z]|个|项|种)?\s*好(?![了好的]))", text):
        return True

    if re.search(r"(?:写|作|撰写|分析|生成|输出|制作|关于).*(?:对比|比较)", text):
        if not re.search(r"(?:还是|或者|哪个|选哪|如何选|怎么选)", text):
            return False
    if re.search(r"(?:对比|比较).*(?:分析|研究|报告|文章|总结)", text):
        if not re.search(r"(?:还是|或者|哪个|选哪|如何选|怎么选)", text):
            return False

    is_question = any(q in text for q in ("？", "?", "吗", "呢", "如何", "怎样"))
    has_alternative = any(a in text for a in ("还是", "或者"))
    if is_question and has_alternative:
        return True

    return False


def _is_option_selected(user_msg: str, key: str, val: str, all_options: dict[str, str]) -> bool:
    """Verify that the user's natural language input affirmatively and unambiguously selected this option."""
    if not user_msg or not val:
        return False
    msg = user_msg.strip()

    pos_idx_str = None
    try:
        keys_list = list(all_options.keys())
        if key in keys_list:
            pos_idx_str = str(keys_list.index(key) + 1)
    except Exception:
        pass

    key_str = str(key)
    c_num = _CHINESE_NUMS.get(key_str, key_str)
    all_tokens = [key_str, c_num, val]
    if key_str in _ORDINAL_TOKENS:
        all_tokens.extend(_ORDINAL_TOKENS[key_str])

    if pos_idx_str:
        all_tokens.append(pos_idx_str)
        all_tokens.append(_CHINESE_NUMS.get(pos_idx_str, pos_idx_str))
        if pos_idx_str in _ORDINAL_TOKENS:
            all_tokens.extend(_ORDINAL_TOKENS[pos_idx_str])

    negation_patterns = []
    non_letter_tokens = []
    for t in set(all_tokens):
        if not t:
            continue
        if t.isalpha() and len(t) == 1:
            negation_patterns.append(
                rf"(?:不|别|不要|无需|排除|免去|取消|并非)\s*(?:想|要|选|用|采纳|考虑|以)?\s*(?:第)?\s*{re.escape(t)}{SUFFIX_BOUND}"
            )
        else:
            non_letter_tokens.append(re.escape(t))

    if non_letter_tokens:
        sub_pat = "|".join(non_letter_tokens)
        negation_patterns.append(
            rf"(?:不|别|不要|无需|排除|免去|取消|并非)\s*(?:想|要|选|用|采纳|考虑|以)?\s*(?:第)?(?:{sub_pat})"
        )

    for pat in negation_patterns:
        if re.search(pat, msg, flags=re.IGNORECASE):
            return False

    if _is_ambiguous_comparison_question(msg):
        return False

    if val in msg:
        return True

    if key_str.isalpha() and len(key_str) == 1:
        if re.search(rf"(?i){PREFIX_BOUND}\s*{re.escape(key_str)}\s*{SUFFIX_BOUND}", msg):
            return True

    ordinals = _ORDINAL_TOKENS.get(key_str, [])
    for ord_token in ordinals:
        if ord_token.isalpha() and len(ord_token) == 1:
            if re.search(rf"(?i){PREFIX_BOUND}\s*{re.escape(ord_token)}\s*{SUFFIX_BOUND}", msg):
                return True
        else:
            if ord_token in msg:
                return True

    if pos_idx_str:
        pos_ordinals = _ORDINAL_TOKENS.get(pos_idx_str, [])
        for ord_token in pos_ordinals:
            if ord_token.isalpha() and len(ord_token) == 1:
                if re.search(rf"(?i){PREFIX_BOUND}\s*{re.escape(ord_token)}\s*{SUFFIX_BOUND}", msg):
                    return True
            elif ord_token in msg:
                return True

    return False


def _is_requirement_affirmed(user_msg: str, val: str, pending_options: dict[str, str]) -> bool:
    """Verify that a requirement update value is affirmatively confirmed by the user."""
    if not user_msg or not val:
        return False
    msg = user_msg.strip()

    matched_opt_key = None
    for opt_key, opt_val in pending_options.items():
        if opt_val == val:
            matched_opt_key = opt_key
            break

    if matched_opt_key is not None:
        return _is_option_selected(msg, matched_opt_key, val, pending_options)

    if val not in msg:
        return False

    clauses = [c.strip() for c in re.split(r"[,，;；\n]", msg) if c.strip()]
    val_clauses = [c for c in clauses if val in c]
    target_scope = " ".join(val_clauses) if val_clauses else msg

    if _is_ambiguous_comparison_question(target_scope):
        return False

    negation_regex = re.compile(
        rf"(?:不|别|不要|无需|排除|免去|取消|并非)\s*(?:想|要|选|用|采纳|考虑|以)?\s*{re.escape(val)}",
        flags=re.IGNORECASE
    )
    if negation_regex.search(target_scope):
        return False

    return True


def prune_catalog_for_decision(
    catalog: list[dict],
    requested_skill_id: str | None,
    current_skill_id: str | None,
    requested_output: str | None,
) -> list[dict]:
    """Prune and compact the skill catalog so it fits safely within model input budgets."""
    if not catalog:
        return []

    top_priority = []
    output_matched = []
    others = []

    req_kind = _OUTPUT_KIND.get(requested_output) if requested_output and requested_output != "auto" else None

    for item in catalog:
        s_id = item.get("skill_id")
        if s_id and (s_id == requested_skill_id or s_id == current_skill_id):
            top_priority.append(item)
        elif req_kind and req_kind in item.get("supported_outputs", []):
            output_matched.append(item)
        else:
            others.append(item)

    output_matched.sort(key=lambda s: (-s.get("priority", 0), s.get("skill_id", "")))
    others.sort(key=lambda s: (-s.get("priority", 0), s.get("skill_id", "")))

    ordered_candidates = top_priority + output_matched + others

    pruned = []
    current_bytes = 0
    for s in ordered_candidates:
        desc = s.get("description", "")
        if len(desc) > MAX_SKILL_DESC_CHARS:
            desc = desc[:MAX_SKILL_DESC_CHARS] + "..."
        entry = {
            "skill_id": s["skill_id"],
            "name": s.get("name", s["skill_id"]),
            "description": desc,
            "supported_outputs": s.get("supported_outputs", []),
            "default_output": s.get("default_output", ""),
            "priority": s.get("priority", 0),
        }
        entry_bytes = len(json.dumps(entry, ensure_ascii=False).encode())
        if current_bytes + entry_bytes > MAX_CATALOG_PAYLOAD_BYTES and pruned:
            break
        pruned.append(entry)
        current_bytes += entry_bytes

    return pruned

class SectionBatch(StrictModel):
    sections: list[Section] = Field(min_length=1, max_length=8)

CHARS_PER_SECTION = 900
GENERATE_BATCH_SECTIONS = 4
_LENGTH_NUMBER = re.compile(r"(\d+(?:\.\d+)?)\s*(万|w|k|千)?", re.IGNORECASE)
_EXPLICIT_LENGTH = re.compile(
    r"(?:至少|不少于|不超过|最多|最少)?\s*\d[\d.,]*\s*(?:万|千|k|w)?"
    r"(?:\s*[-–~至]\s*\d[\d.,]*\s*(?:万|千|k|w)?)?\s*字(?:符)?(?:以内|以上)?", re.IGNORECASE)

def explicit_length(message: str) -> str | None:
    """Return the user's own verbatim length wording when the message states one.

    The clarify model sometimes omits an explicit length from its requirements. Because the user
    wrote it literally, adopting that substring cannot violate the no-invented-requirements rule,
    and the outline scale depends on it.
    """
    if not isinstance(message, str):
        return None
    candidates = list(_EXPLICIT_LENGTH.finditer(message))
    for match in reversed(candidates):
        if re.search(r"(?:不要|取消|去掉)\s*$", message[:match.start()]):
            continue
        candidate = match.group(0).strip()
        if length_target({"length": candidate}) is not None:
            return candidate
    return None


def length_target(requirements: dict) -> int | None:
    """Best-effort conversion of a free-form length requirement into a character target.

    Understands plain counts, thousands markers (千/k/w) and 万, and averages ranges so
    that "5000-8000字" becomes a target inside the requested band. Page counts such as
    "3页" are ignored because they are far below any plausible character target.
    """
    text = requirements.get("length", "")
    if not isinstance(text, str) or not text.strip():
        return None
    values: list[int] = []
    pairs = _LENGTH_NUMBER.findall(text.replace(",", ""))
    # In "3-5千字", the trailing unit applies to both endpoints.
    trailing_unit = pairs[-1][1] if pairs else ""
    for raw, unit in pairs:
        value = float(raw)
        if not unit and value < 200 and len(pairs) == 2 and re.search(r"[-–~至]", text):
            unit = trailing_unit
        unit = (unit or "").lower()
        if unit in {"万", "w"}:
            value *= 10000
        elif unit in {"k", "千"}:
            value *= 1000
        if 200 <= value <= 100_000:
            values.append(int(value))
    if not values:
        return None
    return sum(values) // len(values)


def _band(target: int) -> str:
    return f"{round(target * 0.85)}-{round(target * 1.15)}"


def length_bounds(requirements: dict) -> tuple[int, int] | None:
    target = length_target(requirements)
    if target is None:
        return None
    wording = requirements.get("length", "")
    if any(word in wording for word in ("不超过", "最多", "以内")):
        return 0, target
    if any(word in wording for word in ("至少", "不少于", "最少", "以上")):
        return target, 100000
    return round(target * .85), round(target * 1.15)


def requirements_block(requirements: dict) -> str:
    """Render confirmed requirements as binding instructions rather than background JSON."""
    if not requirements:
        return ""
    listed = "\n".join(f"- {field}: {value}" for field, value in sorted(requirements.items()))
    return ("Confirmed requirements are binding, not background:\n" + listed + "\n"
            "- length is a hard budget; self-check the final total before returning.\n"
            "- audience sets register, terminology depth and how much background is assumed.\n"
            "- style drives sentence patterns, structure and formatting.\n"
            "- purpose shapes emphasis and the closing.\n"
            "- constraints must each be satisfied; if one cannot be met, say so in the affected\n"
            "  section instead of silently ignoring it.")


def outline_scale_hint(requirements: dict) -> str:
    """Tell the outline model how many sections the confirmed length implies."""
    target = length_target(requirements)
    if target is None:
        return ("Scale: plan a compact outline of 4-8 sections unless the user asked for a "
                "different scale.")
    sections = max(3, min(30, round(target / CHARS_PER_SECTION)))
    return (f"Scale: the confirmed length is about {target} characters, so plan roughly "
            f"{sections} sections (700-1100 characters each) and keep the finished body within "
            f"{_band(target)} characters.")


def generation_budget(requirements: dict, section_count: int) -> str:
    """Turn the confirmed length into a per-section budget for the generate stage."""
    target = length_target(requirements)
    if target is None or section_count <= 0:
        return ""
    lower, upper = length_bounds(requirements)
    return (f"Length is a hard budget: about {target} characters across {section_count} sections, "
            f"so write about {max(1, target // section_count)} characters per section and keep the "
            f"whole body within {lower}-{upper} characters.")



async def generate_sections(model, prompt: str, payload: dict, outline: Outline,
                             materials: str, progress, requirements: dict) -> Content:
    """Generate the confirmed sections, in bounded batches, retrying only the failed batch."""
    nodes = outline.nodes
    target = length_target(requirements)
    # Large length budgets use smaller batches instead of exceeding one response.
    per_section = max(1, (target or 0) // len(nodes))
    batch_size = min(GENERATE_BATCH_SECTIONS, max(1, 4500 // per_section)) if target else GENERATE_BATCH_SECTIONS
    batches = [nodes[start:start + batch_size] for start in range(0, len(nodes), batch_size)]
    if progress is not None:
        await progress("generating_sections", {"batches": len(batches), "sections": len(nodes)})
    sections: list[Section] = []
    for index, batch in enumerate(batches, 1):
        titles = [node.title for node in batch]
        single = len(batches) == 1
        if single:
            section_prompt = prompt
            request_payload = {**payload, "materials": materials, "schema": Content.model_json_schema()}
        else:
            recap = "\n".join(
                f"- {section.title}: {' '.join(section.body.split())[:120]}" for section in sections[-3:])
            target = length_target(requirements)
            share = ""
            if target:
                share = (f"This batch of {len(batch)} sections must total about "
                         f"{max(1, target * len(batch) // len(nodes))} characters. The finished body is "
                         f"{target} characters across {len(nodes)} sections, so do not write more "
                         f"than this batch's share.")
            section_prompt = "\n".join(part for part in (
                prompt,
                "Write ONLY these sections, in this exact order, with these exact titles:\n"
                + "\n".join(f"{position}. {title}" for position, title in enumerate(titles, 1)),
                share,
                f"Sections already written (do not repeat or rewrite them):\n{recap}" if recap else "",
            ) if part)
            request_payload = {**payload, "materials": materials, "section_titles": titles,
                               "schema": SectionBatch.model_json_schema()}
        produced: list[Section] = []
        for attempt in (1, 2):
            try:
                raw = await model.complete(section_prompt, request_payload)
                if raw == {"error": "MATERIAL_OUTLINE_CONFLICT"}:
                    raise ServiceError("MATERIAL_OUTLINE_CONFLICT")
                if single:
                    content = Content.model_validate(raw)
                    produced = content.sections
                    if content.title != outline.title:
                        raise ServiceError("GENERATED_STRUCTURE_MISMATCH", 502)
                else:
                    produced = SectionBatch.model_validate(raw).sections
                if [section.title for section in produced] != titles:
                    raise ServiceError("GENERATED_STRUCTURE_MISMATCH", 502)
                break
            except ServiceError as exc:
                # Contradicting materials will not improve on retry; a structure miss may.
                if exc.code == "MATERIAL_OUTLINE_CONFLICT" or attempt == 2:
                    raise
            except ValidationError as exc:
                if attempt == 2:
                    raise ServiceError("GENERATED_STRUCTURE_MISMATCH", 502) from exc
        sections.extend(produced)
        if progress is not None and not single:
            await progress("generated_sections", {"batch": index, "batches": len(batches)})
    return Content(title=outline.title, sections=sections)
