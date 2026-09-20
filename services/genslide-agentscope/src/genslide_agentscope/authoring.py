"""Framework-free writing primitives shared by guided and conversational authoring."""
import re
from pydantic import Field, ValidationError
from .domain import Content, Outline, Section, ServiceError, StrictModel

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
