"""Reasoning and output sanitization for LLM responses.

Carefully preserves legitimate markdown code blocks and internal content,
while cleanly separating outer <think> tags and outer conversational markdown fences.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SanitizedOutput:
    """Sanitized LLM output with separated thought chain and clean payload."""
    raw: str
    clean_text: str
    thought: str = ""
    has_thought: bool = False


def extract_thought(raw_text: str) -> tuple[str, str]:
    """Extract outer <think> tag if present before any JSON payload or in plain text responses.

    Preserves <think> tags that reside inside valid JSON payloads,
    and cleanly handles JSON braces '{' occurring inside thinking chains.
    """
    if not raw_text or "<think>" not in raw_text.lower():
        return raw_text, ""

    lower_text = raw_text.lower()
    think_start = lower_text.find("<think>")

    first_brace = raw_text.find("{")
    first_bracket = raw_text.find("[")
    json_candidates = [p for p in (first_brace, first_bracket) if p != -1]
    first_json = min(json_candidates) if json_candidates else -1

    # If a JSON structure clearly starts BEFORE the <think> tag,
    # the <think> tag is internal to the payload; preserve verbatim.
    if first_json != -1 and think_start > first_json:
        return raw_text, ""

    # think_start precedes any JSON payload. Identify outer <think> boundary.
    think_end = lower_text.find("</think>", think_start)
    if think_end != -1:
        thought = raw_text[think_start + 7 : think_end].strip()
        clean = (raw_text[:think_start] + raw_text[think_end + 8 :]).strip()
        return clean, thought

    # If unclosed <think> before JSON or in plain text
    thought = raw_text[think_start + 7 :].strip()
    clean = raw_text[:think_start].strip()
    return clean, thought


def strip_markdown_fences(text: str) -> str:
    """Strip outermost markdown code block fences (```...```) wrapping the entire response.

    Does NOT search for or alter inner code blocks inside text or JSON strings.
    """
    if not text:
        return ""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()

    return stripped


def sanitize_model_output(raw_text: str, strip_fences: bool = True) -> SanitizedOutput:
    """Sanitize LLM output by separating outer thinking chain and stripping outer code fences.

    Args:
        raw_text: The raw string response from the model.
        strip_fences: Whether to strip outermost markdown ```...``` fences.

    Returns:
        SanitizedOutput instance containing clean_text and thought.
    """
    if not raw_text:
        return SanitizedOutput(raw="", clean_text="", thought="", has_thought=False)

    text = raw_text
    if strip_fences:
        text = strip_markdown_fences(text)

    clean_text, thought = extract_thought(text)
    has_thought = bool(thought.strip())

    if strip_fences:
        clean_text = strip_markdown_fences(clean_text)

    return SanitizedOutput(
        raw=raw_text,
        clean_text=clean_text.strip(),
        thought=thought.strip(),
        has_thought=has_thought,
    )
