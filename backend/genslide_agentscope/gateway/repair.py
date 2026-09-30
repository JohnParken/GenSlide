"""Fault-tolerant and self-healing JSON parser.

Preserves legitimate JSON strings verbatim on the fast path, only applying
outer wrapper stripping and heuristic/library repairs when parsing fails.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .sanitizer import sanitize_model_output

logger = logging.getLogger(__name__)

try:
    import json_repair
except ImportError:  # pragma: no cover
    json_repair = None


_TRAILING_COMMA_PATTERN = re.compile(r",\s*([\]\}])")
_PYTHON_LITERAL_REPLACEMENTS = [
    (re.compile(r"\bTrue\b"), "true"),
    (re.compile(r"\bFalse\b"), "false"),
    (re.compile(r"\bNone\b"), "null"),
]


def _extract_outermost_json(text: str) -> str:
    """Find and return the outermost { ... } or [ ... ] block if present."""
    text = text.strip()
    first_obj = text.find("{")
    last_obj = text.rfind("}")
    first_arr = text.find("[")
    last_arr = text.rfind("]")

    # Determine whether object or array starts first
    if first_obj != -1 and (first_arr == -1 or first_obj < first_arr):
        if last_obj > first_obj:
            return text[first_obj : last_obj + 1]
    elif first_arr != -1:
        if last_arr > first_arr:
            return text[first_arr : last_arr + 1]

    return text


def _balance_brackets(text: str) -> str:
    """Balance open braces and brackets by appending missing terminators."""
    stack: list[str] = []
    in_string = False
    escape = False

    for char in text:
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue

        if char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if stack and stack[-1] == char:
                stack.pop()

    if stack:
        if in_string:
            text += '"'
        while stack:
            text += stack.pop()
    return text


def _heuristic_repair(text: str) -> str:
    """Apply deterministic heuristic transformations for common LLM mistakes."""
    repaired = text.strip()

    # 1. Replace Python literals
    for pattern, replacement in _PYTHON_LITERAL_REPLACEMENTS:
        repaired = pattern.sub(replacement, repaired)

    # 2. Remove trailing commas in objects and arrays
    repaired = _TRAILING_COMMA_PATTERN.sub(r"\1", repaired)

    # 3. Balance unclosed brackets
    repaired = _balance_brackets(repaired)

    return repaired


def repair_json(raw_text: str) -> str:
    """Heuristically repair a malformed JSON string into valid JSON."""
    if not raw_text or not raw_text.strip():
        raise ValueError("Cannot repair empty JSON string")

    trimmed = raw_text.strip()

    # 1. Fast path 1: already valid pristine JSON
    try:
        json.loads(trimmed)
        return trimmed
    except Exception:
        pass

    # 2. Fast path 2: JSON wrapped only in outer markdown code fences (```...```)
    # Strip fences immediately without touching internal <think> or tags
    from .sanitizer import strip_markdown_fences
    fenced = strip_markdown_fences(trimmed)
    if fenced != trimmed:
        try:
            json.loads(fenced)
            return fenced
        except Exception:
            pass

    # 3. Outer sanitization: Strip outer thinking tags and outermost code fences
    sanitized = sanitize_model_output(trimmed)
    clean = sanitized.clean_text.strip()
    try:
        json.loads(clean)
        return clean
    except Exception:
        pass

    # 3. Extract outermost JSON slice
    outermost = _extract_outermost_json(clean)
    try:
        json.loads(outermost)
        return outermost
    except Exception:
        pass

    # 4. Apply heuristic corrections
    healed = _heuristic_repair(outermost)
    try:
        json.loads(healed)
        return healed
    except Exception:
        pass

    # 5. Deep repair via json_repair library
    if json_repair is not None:
        try:
            repaired_lib = json_repair.repair_json(clean)
            json.loads(repaired_lib)
            return repaired_lib
        except Exception:
            try:
                repaired_lib = json_repair.repair_json(healed)
                json.loads(repaired_lib)
                return repaired_lib
            except Exception:
                pass

    return healed


def loads_repaired(raw_text: str) -> Any:
    """Parse JSON with multi-level self-healing recovery.

    Preserves valid JSON strings verbatim. Only strips outer envelopes
    or applies syntactic healing when standard parsing fails.

    Args:
        raw_text: The input text potentially containing JSON, markdown, or syntax flaws.

    Returns:
        Parsed Python object (dict, list, etc.).

    Raises:
        ValueError: If all repair strategies fail to construct valid JSON.
    """
    if not raw_text or not raw_text.strip():
        raise ValueError("Cannot parse empty JSON input")

    trimmed = raw_text.strip()

    # 1. Fast path 1: Direct strict parse on unmodified string (zero pollution)
    try:
        return json.loads(trimmed)
    except Exception:
        pass

    # 2. Fast path 2: Direct parse after stripping outermost markdown ```...``` fences ONLY
    # This guarantees that legitimate <think> tags occurring inside wrapped JSON strings are 100% preserved!
    from .sanitizer import strip_markdown_fences
    fenced = strip_markdown_fences(trimmed)
    if fenced != trimmed:
        try:
            return json.loads(fenced)
        except Exception:
            pass

    # 3. Outer sanitization: Strip outer thinking tags and outermost code fences
    sanitized = sanitize_model_output(trimmed)
    clean = sanitized.clean_text.strip()
    try:
        return json.loads(clean)
    except Exception:
        pass

    # 3. Extract outermost envelope slice
    envelope = _extract_outermost_json(clean)
    try:
        return json.loads(envelope)
    except Exception:
        pass

    # 4. Heuristic repair on envelope
    repaired = _heuristic_repair(envelope)
    try:
        return json.loads(repaired)
    except Exception:
        pass

    # 5. Deep repair via json_repair
    if json_repair is not None:
        try:
            return json_repair.loads(clean)
        except Exception:
            try:
                return json_repair.loads(repaired)
            except Exception:
                pass

    # Final attempt: repair_json string then parse
    try:
        final_str = repair_json(raw_text)
        return json.loads(final_str)
    except Exception as exc:
        raise ValueError(
            f"Failed to parse and repair JSON after all strategies: {exc}\nOriginal snippet: {raw_text[:200]}"
        ) from exc
