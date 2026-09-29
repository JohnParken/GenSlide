"""Reasoning and output sanitization for LLM responses."""
from __future__ import annotations

import re
from dataclasses import dataclass


_THINK_PAIR_PATTERN = re.compile(r"<think>(.*?)</think>", flags=re.DOTALL | re.IGNORECASE)
_THINK_OPEN_PATTERN = re.compile(r"<think>(.*)", flags=re.DOTALL | re.IGNORECASE)
_MARKDOWN_BLOCK_PATTERN = re.compile(r"```(?:[a-zA-Z0-9_-]+)?\s*\n?(.*?)\n?```", flags=re.DOTALL)


@dataclass(frozen=True, slots=True)
class SanitizedOutput:
    """Sanitized LLM output with separated thought chain and clean payload."""
    raw: str
    clean_text: str
    thought: str = ""
    has_thought: bool = False


def extract_thought(raw_text: str) -> tuple[str, str]:
    """Extract <think> tags from text, returning (clean_text, thought_text)."""
    if not raw_text or "<think>" not in raw_text.lower():
        return raw_text, ""

    thoughts: list[str] = []

    def _replace_pair(match: re.Match) -> str:
        thoughts.append(match.group(1).strip())
        return ""

    # 1. Replace closed pairs
    cleaned = _THINK_PAIR_PATTERN.sub(_replace_pair, raw_text)

    # 2. Check for an unclosed <think> tag (e.g. streaming or token limit truncation)
    unclosed_match = _THINK_OPEN_PATTERN.search(cleaned)
    if unclosed_match:
        thoughts.append(unclosed_match.group(1).strip())
        cleaned = cleaned[:unclosed_match.start()]

    thought_summary = "\n\n".join(t for t in thoughts if t)
    return cleaned.strip(), thought_summary


def strip_markdown_fences(text: str) -> str:
    """Extract inner content if wrapped in markdown code blocks, otherwise return original trimmed text."""
    if not text:
        return ""
    text = text.strip()
    match = _MARKDOWN_BLOCK_PATTERN.search(text)
    if match:
        # Check if the code block represents the core content
        candidate = match.group(1).strip()
        if candidate:
            return candidate
    return text


def sanitize_model_output(raw_text: str, strip_fences: bool = True) -> SanitizedOutput:
    """Sanitize LLM output by separating thinking chain and stripping code fences.

    Args:
        raw_text: The raw string response from the model.
        strip_fences: Whether to strip markdown ```...``` fences.

    Returns:
        SanitizedOutput instance containing clean_text and thought.
    """
    if not raw_text:
        return SanitizedOutput(raw="", clean_text="", thought="", has_thought=False)

    clean_text, thought = extract_thought(raw_text)
    has_thought = bool(thought.strip())

    if strip_fences:
        clean_text = strip_markdown_fences(clean_text)

    return SanitizedOutput(
        raw=raw_text,
        clean_text=clean_text.strip(),
        thought=thought.strip(),
        has_thought=has_thought,
    )
