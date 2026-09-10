"""
Unified tool-calling interface for all LLM providers.

Abstracts the provider-specific tool-calling API formats so every phase
can run the same agent loop regardless of provider (GPT, Grok, Gemini, Claude).

Usage:
    from diffai.eraf4xrd.tool_calling import ToolCaller

    caller = ToolCaller(provider, model)
    caller.set_system(system_prompt)
    caller.add_user_message(text, images=[...])  # images optional

    for step in range(max_steps):
        tool_calls, raw_text = caller.call(tools)
        if not tool_calls:
            break
        for tc in tool_calls:
            if tc["name"] == "finalize":
                # done
                break
            result = execute_tool(tc["name"], tc["arguments"])
            caller.add_tool_result(tc["call_id"], tc["name"], result)

    caller.get_usage()  # {"input_tokens": ..., "output_tokens": ...}
"""

import json
import os
import random
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

# Max seconds to wait for any single LLM call before giving up (env-overridable).
# Keeps a hung/slow API call from blocking a run forever.
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "600"))


def _http_post(
    url: str,
    payload: Dict,
    headers: Dict[str, str],
    timeout: float = LLM_TIMEOUT,
) -> Dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {body[:500]}") from e


def _clean_schema_for_gemini(schema):
    """Recursively strip JSON Schema fields that Gemini doesn't support."""
    if not isinstance(schema, dict):
        return schema
    cleaned = {}
    skip_keys = {
        "additionalProperties",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
    }
    for k, v in schema.items():
        if k in skip_keys:
            continue
        if k == "type" and isinstance(v, list):
            # Gemini doesn't support union types like ["integer", "null"]
            # Pick the first non-null type
            non_null = [t for t in v if t != "null"]
            cleaned[k] = non_null[0] if non_null else "string"
            continue
        if isinstance(v, dict):
            cleaned[k] = _clean_schema_for_gemini(v)
        elif isinstance(v, list):
            cleaned[k] = [
                (
                    _clean_schema_for_gemini(item)
                    if isinstance(item, dict)
                    else item
                )
                for item in v
            ]
        else:
            cleaned[k] = v
    return cleaned


def _openai_tools_to_chat_completions(tools: List[Dict]) -> List[Dict]:
    """Convert flat OpenAI Responses-API tool schemas to Chat Completions format.

    Responses API:  {"type": "function", "name": ..., "parameters": ...}
    Chat Completions: {"type": "function", "function": {"name": ..., "parameters": ...}}
    """
    chat_tools = []
    for t in tools:
        if t.get("type") != "function":
            continue
        chat_tools.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get(
                        "parameters", {"type": "object", "properties": {}}
                    ),
                },
            }
        )
    return chat_tools


def _openai_tools_to_gemini(tools: List[Dict]) -> List[Dict]:
    """Convert OpenAI function tool schemas to Gemini functionDeclarations."""
    declarations = []
    for t in tools:
        if t.get("type") != "function":
            continue
        params = t.get("parameters", {"type": "object", "properties": {}})
        decl = {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": _clean_schema_for_gemini(params),
        }
        declarations.append(decl)
    return declarations


def _openai_tools_to_claude(tools: List[Dict]) -> List[Dict]:
    """Convert OpenAI function tool schemas to Anthropic tool format."""
    claude_tools = []
    for t in tools:
        if t.get("type") != "function":
            continue
        claude_tools.append(
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            }
        )
    return claude_tools


class ToolCaller:
    """
    Unified tool-calling interface for GPT, Grok, Gemini, and Claude.

    Manages conversation history internally and exposes a simple
    call() → tool_calls interface.
    """

    def __init__(
        self,
        provider: str,
        model: str,
        max_retries: int = 3,
        base_sleep: float = 1.0,
        temperature: float = None,
    ):
        self.provider = provider.strip().lower()
        self.model = model
        self.max_retries = max_retries
        self.base_sleep = base_sleep
        # Temperature: explicit param > env var > 0.0
        if temperature is not None:
            self.temperature = temperature
        else:
            self.temperature = float(os.environ.get("TEMPERATURE", "0.0"))

        # Conversation state
        self._system = ""
        self._messages = []  # provider-agnostic message list
        self._openai_messages = (
            []
        )  # for GPT/Grok: raw SDK format (Responses API)
        self._chat_messages = (
            []
        )  # for Together/OpenAI-compat: Chat Completions format
        self._gemini_contents = []  # for Gemini: contents array
        self._claude_messages = []  # for Claude: messages array

        # Usage tracking
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._prev_logged_input = 0
        self._prev_logged_output = 0
        self._last_resp = None  # for OpenAI SDK tracking

        # Client for GPT/Grok/Together
        self._client = None
        if self.provider in ("gpt", "grok"):
            from openai import OpenAI

            if self.provider == "gpt":
                api_key = os.environ.get("OPENAI_API_KEY", "")
                if not api_key:
                    raise RuntimeError("Missing OPENAI_API_KEY")
                self._client = OpenAI(api_key=api_key, timeout=LLM_TIMEOUT)
            else:
                api_key = os.environ.get("XAI_API_KEY", "")
                if not api_key:
                    raise RuntimeError("Missing XAI_API_KEY")
                self._client = OpenAI(
                    api_key=api_key,
                    base_url="https://api.x.ai/v1",
                    timeout=LLM_TIMEOUT,
                )
        elif self.provider == "together":
            from openai import OpenAI

            api_key = os.environ.get("TOGETHER_API_KEY", "")
            if not api_key:
                raise RuntimeError("Missing TOGETHER_API_KEY")
            self._client = OpenAI(
                api_key=api_key,
                base_url="https://api.together.xyz/v1",
                timeout=LLM_TIMEOUT,
            )

    def set_system(self, text: str):
        """Set the system prompt."""
        self._system = text
        if self.provider in ("gpt", "grok"):
            # System goes as first message for Responses API
            self._openai_messages = [{"role": "system", "content": text}]
        elif self.provider == "together":
            # Chat Completions API: system is a regular message
            self._chat_messages = [{"role": "system", "content": text}]
        elif self.provider == "gemini":
            # Gemini uses systemInstruction
            pass  # handled in call()
        elif self.provider == "claude":
            # Claude uses system parameter
            pass  # handled in call()

    def add_user_message(self, text: str, images: Optional[List[Dict]] = None):
        """
        Add a user message, optionally with images.
        images: list of {"data_base64": ..., "media_type": "image/png"} dicts
        """
        if self.provider in ("gpt", "grok"):
            content = [{"type": "input_text", "text": text}]
            for img in images or []:
                content.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{img['media_type']};base64,{img['data_base64']}",
                    }
                )
            self._openai_messages.append({"role": "user", "content": content})

        elif self.provider == "together":
            # Chat Completions format
            if images:
                content = [{"type": "text", "text": text}]
                for img in images:
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{img['media_type']};base64,{img['data_base64']}"
                            },
                        }
                    )
                self._chat_messages.append(
                    {"role": "user", "content": content}
                )
            else:
                self._chat_messages.append({"role": "user", "content": text})

        elif self.provider == "gemini":
            parts = [{"text": text}]
            for img in images or []:
                parts.append(
                    {
                        "inlineData": {
                            "mimeType": img["media_type"],
                            "data": img["data_base64"],
                        }
                    }
                )
            self._gemini_contents.append({"role": "user", "parts": parts})

        elif self.provider == "claude":
            content = [{"type": "text", "text": text}]
            for img in images or []:
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img["media_type"],
                            "data": img["data_base64"],
                        },
                    }
                )
            self._claude_messages.append({"role": "user", "content": content})

    def add_tool_result(self, call_id: str, tool_name: str, result: str):
        """Add a tool result to the conversation history."""
        if self.provider in ("gpt", "grok"):
            self._openai_messages.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result,
                }
            )
        elif self.provider == "together":
            self._chat_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result,
                }
            )
        elif self.provider == "gemini":
            self._gemini_contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": tool_name,
                                "response": {"result": result},
                            }
                        }
                    ],
                }
            )
        elif self.provider == "claude":
            self._claude_messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": result,
                        }
                    ],
                }
            )

    def add_tool_result_with_image(
        self,
        call_id: str,
        tool_name: str,
        result: str,
        image_b64: str,
        media_type: str = "image/png",
    ):
        """Add a tool result accompanied by an image (for vision tools)."""
        if self.provider in ("gpt", "grok"):
            self._openai_messages.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result,
                }
            )
            self._openai_messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": f"data:{media_type};base64,{image_b64}",
                        },
                        {
                            "type": "input_text",
                            "text": f"[Image returned by {tool_name}]",
                        },
                    ],
                }
            )
        elif self.provider == "together":
            self._chat_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result,
                }
            )
            self._chat_messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media_type};base64,{image_b64}"
                            },
                        },
                        {
                            "type": "text",
                            "text": f"[Image returned by {tool_name}]",
                        },
                    ],
                }
            )
        elif self.provider == "gemini":
            self._gemini_contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": tool_name,
                                "response": {"result": result},
                            }
                        },
                        {
                            "inlineData": {
                                "mimeType": media_type,
                                "data": image_b64,
                            }
                        },
                    ],
                }
            )
        elif self.provider == "claude":
            self._claude_messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": result,
                        },
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                    ],
                }
            )

    def call(self, tools: List[Dict]) -> Tuple[List[Dict], str]:
        """
        Make one LLM call with tools.

        Returns:
            tool_calls: list of {"name": str, "arguments": dict, "call_id": str}
            raw_text: any non-tool text the model returned (usually empty)
        """
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                if self.provider in ("gpt", "grok"):
                    return self._call_openai(tools)
                elif self.provider == "together":
                    return self._call_chat_completions(tools)
                elif self.provider == "gemini":
                    return self._call_gemini(tools)
                elif self.provider == "claude":
                    return self._call_claude(tools)
                else:
                    raise RuntimeError(
                        f"Unsupported provider: {self.provider}"
                    )
            except Exception as e:
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(
                        self.base_sleep * (2**attempt) + random.random() * 0.25
                    )
        raise last_err

    def get_usage(self) -> Dict:
        """Return accumulated token usage."""
        return {
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
        }

    def get_last_call_usage(self) -> Dict:
        """Tokens used since the previous call to this method.

        get_usage() is cumulative; logging it once per agent step double-counts
        every earlier step. Call this once per caller.call() to log only that
        step's delta.
        """
        d_in = self._total_input_tokens - self._prev_logged_input
        d_out = self._total_output_tokens - self._prev_logged_output
        self._prev_logged_input = self._total_input_tokens
        self._prev_logged_output = self._total_output_tokens
        return {"input_tokens": d_in, "output_tokens": d_out}

    def get_last_resp(self) -> Any:
        """Return the last raw response (for OpenAI SDK tracker compatibility)."""
        return self._last_resp

    # ======================================================================
    # GPT / Grok (OpenAI Responses API)
    # ======================================================================
    def _call_openai(self, tools: List[Dict]) -> Tuple[List[Dict], str]:
        """Call GPT/Grok via the OpenAI Responses API."""
        resp = self._client.responses.create(
            model=self.model,
            input=self._openai_messages,
            tools=tools,
            temperature=self.temperature,
        )
        self._last_resp = resp

        # Track usage
        usage = getattr(resp, "usage", None)
        if usage:
            self._total_input_tokens += getattr(usage, "input_tokens", 0) or 0
            self._total_output_tokens += (
                getattr(usage, "output_tokens", 0) or 0
            )

        output_items = getattr(resp, "output", None) or []

        # Append all output items to history
        for item in output_items:
            try:
                # by_alias=True emits the wire names the API expects. Some
                # Responses output fields (e.g. reasoning items' `async`) are
                # Python-reserved words the SDK stores as `async_`; a plain
                # model_dump() would echo `async_` back and the API 400s
                # ("Unknown parameter: input[..].async_. Did you mean 'async'?").
                dumped = (
                    item
                    if isinstance(item, dict)
                    else item.model_dump(by_alias=True, exclude_none=True)
                )
                if isinstance(dumped, dict):
                    dumped.pop("status", None)
                self._openai_messages.append(dumped)
            except Exception:
                try:
                    self._openai_messages.append(dict(item))
                except Exception:
                    self._openai_messages.append(
                        {"role": "assistant", "content": str(item)}
                    )

        # Extract tool calls
        tool_calls = []
        raw_text = ""
        for item in output_items:
            itype = getattr(item, "type", None) or (
                item.get("type") if isinstance(item, dict) else None
            )
            if itype == "function_call":
                name = getattr(item, "name", None) or item.get("name")
                raw_args = (
                    getattr(item, "arguments", None)
                    or item.get("arguments", "")
                    or "{}"
                )
                call_id = (
                    getattr(item, "call_id", None)
                    or item.get("call_id")
                    or getattr(item, "id", None)
                    or str(uuid.uuid4())
                )
                args = (
                    json.loads(raw_args)
                    if isinstance(raw_args, str)
                    else (raw_args or {})
                )
                tool_calls.append(
                    {"name": name, "arguments": args, "call_id": call_id}
                )
            elif itype == "message":
                content = getattr(item, "content", None) or item.get(
                    "content", []
                )
                if isinstance(content, list):
                    for c in content:
                        t = getattr(c, "text", None) or (
                            c.get("text") if isinstance(c, dict) else None
                        )
                        if t:
                            raw_text += t

        return tool_calls, raw_text

    # ======================================================================
    # Together / OpenAI-compatible (Chat Completions API)
    # ======================================================================
    def _call_chat_completions(
        self, tools: List[Dict]
    ) -> Tuple[List[Dict], str]:
        """Call an OpenAI-compatible Chat Completions endpoint (Together, Groq, etc.)."""
        chat_tools = _openai_tools_to_chat_completions(tools)

        kwargs = dict(
            model=self.model,
            messages=self._chat_messages,
            temperature=self.temperature,
        )
        if chat_tools:
            kwargs["tools"] = chat_tools
            # "auto" (not "required"): open-source models sometimes reply in plain text,
            # but the agentic loops handle that with a nudge (retry asking for tool use)
            # so the model can gather evidence FIRST, then finalize. Forcing "required"
            # makes weak models jump straight to finalize with no evidence -> bad decisions.
            kwargs["tool_choice"] = "auto"

        resp = self._client.chat.completions.create(**kwargs)
        self._last_resp = resp

        # Track usage
        usage = getattr(resp, "usage", None)
        if usage:
            self._total_input_tokens += getattr(usage, "prompt_tokens", 0) or 0
            self._total_output_tokens += (
                getattr(usage, "completion_tokens", 0) or 0
            )

        msg = resp.choices[0].message

        # Append assistant message to history (including any tool_calls)
        assistant_msg = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        self._chat_messages.append(assistant_msg)

        # Extract tool calls
        tool_calls = []
        raw_text = msg.content or ""
        if msg.tool_calls:
            for tc in msg.tool_calls:
                raw_args = tc.function.arguments or "{}"
                args = (
                    json.loads(raw_args)
                    if isinstance(raw_args, str)
                    else (raw_args or {})
                )
                tool_calls.append(
                    {
                        "name": tc.function.name,
                        "arguments": args,
                        "call_id": tc.id,
                    }
                )

        return tool_calls, raw_text

    # ======================================================================
    # Gemini (REST API with function calling)
    # ======================================================================
    def _call_gemini(self, tools: List[Dict]) -> Tuple[List[Dict], str]:
        """Call Gemini via the generateContent REST endpoint."""
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("Missing GEMINI_API_KEY")

        # The key goes in a header, not the ?key= query parameter Google also
        # accepts: a URL-borne credential is copied into proxy logs, server
        # access logs and any HTTP error text (a requests HTTPError prints the
        # full URL), none of which we can scrub.
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )

        payload = {
            "contents": self._gemini_contents,
            "tools": [
                {"functionDeclarations": _openai_tools_to_gemini(tools)}
            ],
            "generationConfig": {"temperature": self.temperature},
        }
        if self._system:
            payload["systemInstruction"] = {"parts": [{"text": self._system}]}

        resp = _http_post(url, payload, headers={"x-goog-api-key": api_key})
        self._last_resp = resp

        # Track usage
        meta = resp.get("usageMetadata", {})
        self._total_input_tokens += meta.get("promptTokenCount", 0) or 0
        self._total_output_tokens += meta.get("candidatesTokenCount", 0) or 0

        # Parse response
        tool_calls = []
        raw_text = ""
        model_parts = []

        for cand in resp.get("candidates", []):
            content = cand.get("content", {})
            for part in content.get("parts", []):
                model_parts.append(part)
                if "functionCall" in part:
                    fc = part["functionCall"]
                    call_id = str(uuid.uuid4())
                    tool_calls.append(
                        {
                            "name": fc.get("name", ""),
                            "arguments": fc.get("args", {}),
                            "call_id": call_id,
                        }
                    )
                elif "text" in part:
                    raw_text += part["text"]

        # Append model response to history
        if model_parts:
            self._gemini_contents.append(
                {"role": "model", "parts": model_parts}
            )

        return tool_calls, raw_text

    # ======================================================================
    # Claude (Anthropic Messages API with tool use)
    # ======================================================================
    def _call_claude(self, tools: List[Dict]) -> Tuple[List[Dict], str]:
        """Call Claude via the Anthropic Messages REST API."""
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("Missing ANTHROPIC_API_KEY")

        url = "https://api.anthropic.com/v1/messages"
        payload = {
            "model": self.model,
            "max_tokens": 4096,
            "temperature": self.temperature,
            "tools": _openai_tools_to_claude(tools),
            "messages": self._claude_messages,
        }
        if self._system:
            payload["system"] = self._system

        resp = _http_post(
            url,
            payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        self._last_resp = resp

        # Track usage
        usage = resp.get("usage", {})
        self._total_input_tokens += usage.get("input_tokens", 0) or 0
        self._total_output_tokens += usage.get("output_tokens", 0) or 0

        # Parse response
        tool_calls = []
        raw_text = ""
        assistant_content = []

        for block in resp.get("content", []):
            assistant_content.append(block)
            if block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "name": block.get("name", ""),
                        "arguments": block.get("input", {}),
                        "call_id": block.get("id", str(uuid.uuid4())),
                    }
                )
            elif block.get("type") == "text":
                raw_text += block.get("text", "")

        # Append assistant response to history
        if assistant_content:
            self._claude_messages.append(
                {"role": "assistant", "content": assistant_content}
            )

        return tool_calls, raw_text
