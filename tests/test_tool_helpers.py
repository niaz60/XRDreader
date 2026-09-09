"""Pure helpers: tool-schema conversion and human-readable error messages."""

from diffai.eraf4xrd.tool_calling import _openai_tools_to_chat_completions
from diffai.eraf4xrd.utils import humanize_llm_error

RESPONSES_TOOL = {
    "type": "function",
    "name": "get_abstract",
    "description": "Return the abstract.",
    "parameters": {"type": "object", "properties": {}},
}


def test_chat_completions_conversion_nests_under_function():
    out = _openai_tools_to_chat_completions([RESPONSES_TOOL])
    assert len(out) == 1
    assert out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "get_abstract"
    assert out[0]["function"]["description"] == "Return the abstract."
    assert out[0]["function"]["parameters"] == RESPONSES_TOOL["parameters"]


def test_chat_completions_skips_non_function_entries():
    assert _openai_tools_to_chat_completions([{"type": "other"}]) == []


def test_humanize_non_serverless():
    msg = humanize_llm_error(
        Exception("400 - Unable to access non-serverless model X")
    )
    assert "dedicated endpoint" in msg


def test_humanize_image_validation():
    msg = humanize_llm_error(
        Exception("Error code: 400 - Input validation error")
    )
    assert "vision" in msg.lower()


def test_humanize_auth():
    msg = humanize_llm_error(Exception("401 Unauthorized"))
    assert "key" in msg.lower()


def test_humanize_unknown_falls_back_to_original():
    msg = humanize_llm_error(Exception("something weird happened"))
    assert "something weird happened" in msg
