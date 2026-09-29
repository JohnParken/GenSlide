"""Fault-tolerant and self-healing JSON parser.

Handles common LLM formatting flaws:
- Wrapping markdown code fences (```json ... ```)
- Unescaped newlines in strings
- Trailing commas in arrays and objects
- Single quotes instead of double quotes
- Missing closing braces or brackets due to truncation
- Python literal values (True, False, None)
- Conversational preambles or postscripts
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


_JSON_EXTRACT_PATTERN = re.compile(r"(\{.*\}|\[.*\])", flags=re.DOTALL)
_TRAILING_COMMA_PATTERN = re.compile(r",\s*([\]\}])")
_UNQUOTED_KEY_PATTERN = re.compile(r'(?<=[{,\s])([a-zA-Z_][a-zA-Z0-9_-]*)\s*:\s*')
_PYTHON_LITERAL_REPLACEMENTS = [
    (re.compile(r"\bTrue\b"), "true"),
    (re.compile(r"\bFalse\b"), "false"),
    (re.compile(r"\bNone\b"), "null"),
]


def _strip_envelope(text: str) -> str:
    """Find and return the outermost { ... } or [ ... ] block if present."""
    text = text.strip()
    match = _JSON_EXTRACT_PATTERN.search(text)
    if match:
        return match.group(1).strip()
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
        # If open string at truncation, close it first
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

    # Step 1: Sanitize model output (strip <think> and markdown fences)
    sanitized = sanitize_model_output(raw_text)
    candidate = sanitized.clean_text.strip()

    # Step 2: Try standard JSON parsing first
    try:
        json.loads(candidate)
        return candidate
    except Exception:
        pass

    # Step 3: Extract outermost JSON object/array
    stripped = _strip_envelope(candidate)
    try:
        json.loads(stripped)
        return stripped
    except Exception:
        pass

    # Step 4: Apply heuristic corrections
    healed = _heuristic_repair(stripped)
    try:
        json.loads(healed)
        return healed
    except Exception:
        pass

    # Step 5: Fallback to json_repair library if available
    if json_repair is not None:
        try:
            repaired_lib = json_repair.repair_json(candidate)
            # Verify that output from json_repair is parseable
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

    Args:
        raw_text: The input text potentially containing JSON, markdown, or syntax flaws.

    Returns:
        Parsed Python object (dict, list, etc.).

    Raises:
        ValueError: If all repair strategies fail to construct valid JSON.
    """
    if not raw_text or not raw_text.strip():
        raise ValueError("Cannot parse empty JSON input")

    # 1. Fast path: Direct parse on sanitized string
    sanitized = sanitize_model_output(raw_text)
    clean = sanitized.clean_text.strip()

    try:
        return json.loads(clean)
    except Exception:
        pass

    # 2. Extract envelope
    envelope = _strip_envelope(clean)
    try:
        return json.loads(envelope)
    except Exception:
        pass

    # 3. Heuristic repair
    repaired = _heuristic_repair(envelope)
    try:
        return json.loads(repaired)
    except Exception:
        pass

    # 4. Deep repair via json_repair
    if json_repair is not None:
        try:
            return json_repair.loads(clean)
        except Exception:
            try:
                return json_repair.loads(repaired)
            except Exception:
                pass

    # Final attempt: repair_json and json.loads
    try:
        final_str = repair_json(raw_text)
        return json.loads(final_str)
    except Exception as exc:
        raise ValueError(
            f"Failed to parse and repair JSON after all strategies: {exc}\nOriginal snippet: {raw_text[:200]}"
        ) from exc
