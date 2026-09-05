"""Reading OpenTelemetry span attributes, across two competing conventions.

OTel attributes are a flat list of typed key/value pairs, so anything nested has
to be flattened into indexed keys on the way in::

    llm.input_messages.0.message.role                                  = "user"
    llm.output_messages.0.message.tool_calls.0.tool_call.function.name = "refund_order"

Undoing that is most of the work here.

The other half is that there is no single convention for GenAI spans. Two are in
use and neither has won:

* **OpenInference** (Arize) -- purpose-built for agents. It models tool calls and
  their arguments directly, which is what Evalkeep's expectations are written
  against, so it is the convention this adapter targets.
* **OTel GenAI** (``gen_ai.*``) -- broader vendor support, and its messages are
  whole JSON documents rather than flattened fields. Supported as a fallback for
  the fields where the mapping is unambiguous.

Where a trace carries both, OpenInference wins: it is the more specific of the
two, and guessing between them would be worse than preferring the one that can
actually express a tool call.
"""

from __future__ import annotations

import json
from typing import Any

# -- OpenInference ---------------------------------------------------------

SPAN_KIND = "openinference.span.kind"
INPUT_VALUE = "input.value"
OUTPUT_VALUE = "output.value"
LLM_INPUT_MESSAGES = "llm.input_messages"
LLM_OUTPUT_MESSAGES = "llm.output_messages"
MESSAGE_ROLE = "message.role"
MESSAGE_CONTENT = "message.content"
MESSAGE_TOOL_CALLS = "message.tool_calls"
TOOL_CALL_NAME = "tool_call.function.name"
TOOL_CALL_ARGUMENTS = "tool_call.function.arguments"
TOOL_NAME = "tool.name"
TOOL_PARAMETERS = "tool.parameters"
LLM_MODEL_NAME = "llm.model_name"

#: The span kinds Evalkeep reads. Others are carried as context only.
KIND_TOOL = "TOOL"
KIND_LLM = "LLM"
KIND_AGENT = "AGENT"
KIND_CHAIN = "CHAIN"

# -- OTel GenAI ------------------------------------------------------------

GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages"
GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_ARGUMENTS = "gen_ai.tool.call.arguments"
GEN_AI_OPERATION = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_SYSTEM = "gen_ai.system"

SERVICE_NAME = "service.name"


def decode_attributes(raw: Any) -> dict[str, Any]:
    """Turn OTLP's ``[{key, value: {...}}]`` list into a flat dictionary."""
    decoded: dict[str, Any] = {}
    if not isinstance(raw, list):
        return decoded
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if isinstance(key, str):
            decoded[key] = decode_value(item.get("value"))
    return decoded


def decode_value(value: Any) -> Any:
    """One OTLP ``AnyValue``. Unknown shapes decode to ``None`` rather than raise."""
    if not isinstance(value, dict):
        return value
    for field in ("stringValue", "boolValue", "doubleValue"):
        if field in value:
            return value[field]
    if "intValue" in value:
        raw = value["intValue"]
        # Protobuf renders 64-bit integers as strings in JSON.
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw
    if "arrayValue" in value:
        values = (value["arrayValue"] or {}).get("values") or []
        return [decode_value(item) for item in values]
    if "kvlistValue" in value:
        return decode_attributes((value["kvlistValue"] or {}).get("values"))
    if "bytesValue" in value:
        return value["bytesValue"]
    return None


def indexed(attributes: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    """Collect ``prefix.<i>.rest`` keys back into a list of dictionaries.

    Ordering follows the index, not the order the attributes happened to arrive
    in, because a message list that reorders itself is a different conversation.
    """
    grouped: dict[int, dict[str, Any]] = {}
    marker = f"{prefix}."
    for key, value in attributes.items():
        if not key.startswith(marker):
            continue
        remainder = key[len(marker) :]
        index, separator, rest = remainder.partition(".")
        if not separator or not index.isdigit():
            continue
        grouped.setdefault(int(index), {})[rest] = value
    return [grouped[index] for index in sorted(grouped)]


def text(attributes: dict[str, Any], key: str) -> str | None:
    """A string attribute, or ``None`` when absent or empty."""
    value = attributes.get(key)
    if value is None:
        return None
    rendered = value if isinstance(value, str) else json.dumps(value, default=str)
    return rendered or None


def json_object(attributes: dict[str, Any], key: str) -> dict[str, Any]:
    """An attribute holding a JSON object, however it was encoded.

    Conventions disagree about whether structured values are JSON strings or
    real key/value lists, so both are accepted rather than one being declared
    correct.
    """
    value = attributes.get(key)
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def json_list(attributes: dict[str, Any], key: str) -> list[Any]:
    """An attribute holding a JSON array, however it was encoded."""
    value = attributes.get(key)
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def messages(attributes: dict[str, Any], *, output: bool = False) -> list[dict[str, str]]:
    """Conversation messages from either convention, as role/content pairs."""
    prefix = LLM_OUTPUT_MESSAGES if output else LLM_INPUT_MESSAGES
    found: list[dict[str, str]] = []
    for entry in indexed(attributes, prefix):
        role = entry.get(MESSAGE_ROLE)
        content = entry.get(MESSAGE_CONTENT)
        if isinstance(role, str) and isinstance(content, str) and content:
            found.append({"role": role, "content": content})
    if found:
        return found

    # OTel GenAI keeps whole messages as one JSON document.
    key = GEN_AI_OUTPUT_MESSAGES if output else GEN_AI_INPUT_MESSAGES
    for entry in json_list(attributes, key):
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        content = _flatten_content(entry.get("content") or entry.get("parts"))
        if isinstance(role, str) and content:
            found.append({"role": role, "content": content})
    return found


def _flatten_content(content: Any) -> str:
    """Message content, whether a string or a list of typed parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [part.get("text", "") if isinstance(part, dict) else str(part) for part in content]
        return "".join(part for part in parts if part)
    return ""


def tool_calls(attributes: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Tool calls recorded on an LLM span's output messages."""
    calls: list[tuple[str, dict[str, Any]]] = []
    for message in indexed(attributes, LLM_OUTPUT_MESSAGES):
        for call in indexed(message, MESSAGE_TOOL_CALLS):
            name = call.get(TOOL_CALL_NAME)
            if not isinstance(name, str) or not name:
                continue
            calls.append((name, json_object(call, TOOL_CALL_ARGUMENTS)))
    return calls
