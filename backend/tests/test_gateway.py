"""Unit tests for the self-healing gateway layer (sanitizer, repair, capability cache)."""

import pytest
from pydantic import BaseModel

from genslide_agentscope.gateway import (
    SanitizedOutput,
    sanitize_model_output,
    repair_json,
    loads_repaired,
    ModelCapabilityCache,
    get_capability_cache,
)


class SampleModel(BaseModel):
    name: str
    count: int


def test_sanitize_clean_text_no_think():
    text = "Hello world, this is a clean response."
    out = sanitize_model_output(text)
    assert out.clean_text == text
    assert out.thought == ""
    assert not out.has_thought


def test_sanitize_closed_think_tags():
    text = "<think>Analyzing user request...\nStep 1: Check inputs.</think>Final result is 42."
    out = sanitize_model_output(text)
    assert out.clean_text == "Final result is 42."
    assert "Analyzing user request..." in out.thought
    assert out.has_thought


def test_sanitize_case_insensitive_think():
    text = "<THINK>Thinking deeply...</THINK>Output."
    out = sanitize_model_output(text)
    assert out.clean_text == "Output."
    assert out.thought == "Thinking deeply..."
    assert out.has_thought


def test_sanitize_unclosed_think_tag():
    text = "Intro.<think>Still thinking and generation truncated here..."
    out = sanitize_model_output(text)
    assert out.clean_text == "Intro."
    assert "Still thinking" in out.thought
    assert out.has_thought


def test_sanitize_strips_markdown_fences():
    text = "```json\n{\"status\": \"ok\", \"code\": 200}\n```"
    out = sanitize_model_output(text)
    assert out.clean_text == '{"status": "ok", "code": 200}'


def test_repair_clean_json():
    raw = '{"name": "Alice", "age": 30}'
    result = loads_repaired(raw)
    assert result == {"name": "Alice", "age": 30}


def test_repair_markdown_code_block():
    raw = """
Here is the JSON you requested:
```json
{
  "effect": "reply",
  "reply": "All clear!"
}
```
Hope that helps!
"""
    result = loads_repaired(raw)
    assert result["effect"] == "reply"
    assert result["reply"] == "All clear!"


def test_repair_with_think_tags():
    raw = """<think>
Need to return effect reply.
</think>
```json
{"effect": "reply", "reply": "Done"}
```"""
    result = loads_repaired(raw)
    assert result == {"effect": "reply", "reply": "Done"}


def test_repair_trailing_commas():
    raw = '{"items": [1, 2, 3, ], "config": {"debug": true, }, }'
    result = loads_repaired(raw)
    assert result == {"items": [1, 2, 3], "config": {"debug": True}}


def test_repair_python_literals():
    raw = '{"is_active": True, "is_deleted": False, "meta": None}'
    result = loads_repaired(raw)
    assert result == {"is_active": True, "is_deleted": False, "meta": None}


def test_repair_unclosed_brackets():
    raw = '{"title": "Mission", "tasks": [{"id": 1, "desc": "Start"'
    result = loads_repaired(raw)
    assert result["title"] == "Mission"
    assert result["tasks"][0]["id"] == 1


def test_repair_empty_raises_value_error():
    with pytest.raises(ValueError):
        loads_repaired("")

    with pytest.raises(ValueError):
        loads_repaired("    ")


def test_capability_cache_defaults():
    cache = ModelCapabilityCache()
    cap = cache.get("openai", "gpt-4o")
    assert cap.provider == "openai"
    assert cap.model == "gpt-4o"
    assert cap.supports_response_format is True
    assert cap.strict_json_schema is True
    assert cap.supports_tools is True


def test_capability_cache_auto_adaptation():
    cache = ModelCapabilityCache()
    # 1. Error indicating response_format failure
    cache.record_failure("custom_proxy", "my-model", "Error: response_format is not supported")
    cap = cache.get("custom_proxy", "my-model")
    assert cap.supports_response_format is False
    assert cap.strict_json_schema is True

    # 2. Error indicating strict schema rejection
    cache.record_failure("custom_proxy", "my-model", "Validation failed: additionalProperties: true rejected")
    cap = cache.get("custom_proxy", "my-model")
    assert cap.strict_json_schema is False

    # 3. Error indicating tools failure
    cache.record_failure("custom_proxy", "my-model", "Bad request: tools are not supported")
    cap = cache.get("custom_proxy", "my-model")
    assert cap.supports_tools is False


def test_singleton_capability_cache():
    c1 = get_capability_cache()
    c2 = get_capability_cache()
    assert c1 is c2


def test_json_verbatim_fidelity_for_code_blocks_and_tags():
    """Verify that inner markdown code blocks and think tags are preserved verbatim."""
    import json
    from genslide_agentscope.model import decode_output

    # 1. Inner code block
    v1 = {"effect": "reply", "reply": "示例：\n```python\nprint(1)\n```"}
    raw1 = json.dumps(v1, ensure_ascii=False)
    assert loads_repaired(raw1) == v1
    assert decode_output(raw1) == v1

    # 2. Inner think tags
    v2 = {"effect": "reply", "reply": "请原样保留 <think>示例文本</think> 标签"}
    raw2 = json.dumps(v2, ensure_ascii=False)
    assert loads_repaired(raw2) == v2
    assert decode_output(raw2) == v2

    # 3. Outer fences around legitimate inner fences
    wrapped = "```json\n" + raw1 + "\n```"
    assert loads_repaired(wrapped) == v1
    assert decode_output(wrapped) == v1

