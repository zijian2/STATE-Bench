"""Convert OpenClaw trajectory JSONL to STATE-Bench canonical format.

Reads .trajectory.jsonl event stream and produces STATE-Bench Trajectory schema.
"""

from pathlib import Path
import json
from typing import Any


def parse_openclaw_trajectory(trajectory_path: Path) -> dict[str, Any]:
    """Parse OpenClaw trajectory JSONL into STATE-Bench format.

    OpenClaw stores complete conversation in `model.completed` events under
    `messagesSnapshot`. We use the LAST model.completed event which contains
    the full final conversation state.

    Args:
        trajectory_path: Path to .trajectory.jsonl file.

    Returns:
        Dict with conversation, token_usage.
    """
    last_snapshot: list[dict[str, Any]] | None = None
    total_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "total_tokens": 0,
    }

    with open(trajectory_path) as f:
        for line in f:
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") == "model.completed":
                data = event.get("data", {})
                snapshot = data.get("messagesSnapshot")
                if snapshot:
                    last_snapshot = snapshot
                # Accumulate usage from each model call
                usage = data.get("usage", {})
                if usage:
                    total_usage["input_tokens"] += usage.get("input", 0)
                    total_usage["output_tokens"] += usage.get("output", 0)
                    total_usage["cached_input_tokens"] += usage.get("cacheRead", 0)
                    total_usage["total_tokens"] += usage.get("total", 0)

    if last_snapshot is None:
        return {"conversation": [], "token_usage": total_usage}

    # Convert OpenClaw message format to STATE-Bench canonical format.
    # OpenClaw schema (NOT Anthropic wire format):
    #   - assistant message content blocks: {type: "text"|"toolCall"|"thinking", ...}
    #     - toolCall: {id, name, arguments}
    #     - thinking: {thinking, thinkingSignature}  -> skipped from canonical convo
    #   - toolResult is its OWN role (not nested in user msg):
    #     {role: "toolResult", toolCallId, toolName, content: [{type:"text",text}], isError}
    conversation: list[dict[str, Any]] = []
    for msg in last_snapshot:
        role = msg.get("role", "")

        if role == "toolResult":
            content_blocks = msg.get("content", [])
            if isinstance(content_blocks, list):
                result_text = "".join(
                    b.get("text", "") for b in content_blocks
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                result_text = str(content_blocks)
            conversation.append({
                "role": "tool",
                "content": result_text,
                "tool_use_id": msg.get("toolCallId", ""),
                "tool_name": msg.get("toolName", ""),
                "is_error": msg.get("isError", False),
            })
            continue

        content_blocks = msg.get("content", [])
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        if isinstance(content_blocks, str):
            text_parts.append(content_blocks)
        elif isinstance(content_blocks, list):
            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text_parts.append(block.get("text", ""))
                elif block_type == "toolCall":
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "arguments": block.get("arguments", {}),
                    })
                # "thinking" blocks are intentionally dropped from canonical conversation.

        text = "\n".join(p for p in text_parts if p)

        if role == "user":
            if text:
                conversation.append({"role": "user", "content": text})
        elif role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": text}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            conversation.append(entry)

    return {"conversation": conversation, "token_usage": total_usage}


def compute_efficiency_metrics(conversation: list[dict[str, Any]]) -> dict[str, int]:
    """Compute efficiency metrics from conversation.

    Returns:
        Dict with turns, tool_calls, tool_errors, redundant_calls.
    """
    turns = sum(1 for msg in conversation if msg.get("role") == "assistant")

    tool_calls = 0
    tool_errors = sum(1 for msg in conversation if msg.get("role") == "tool" and msg.get("is_error"))
    seen_calls: set[tuple[str, str]] = set()
    redundant_calls = 0

    for msg in conversation:
        if msg.get("role") == "assistant" and "tool_calls" in msg:
            for tc in msg["tool_calls"]:
                tool_calls += 1
                call_sig = (tc.get("name", ""), json.dumps(tc.get("arguments", {}), sort_keys=True))
                if call_sig in seen_calls:
                    redundant_calls += 1
                seen_calls.add(call_sig)

    return {
        "turns": turns,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "redundant_calls": redundant_calls,
    }
