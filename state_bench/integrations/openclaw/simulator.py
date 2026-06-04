"""Anthropic-powered user simulator for STATE-Bench tasks.

Wraps Anthropic API to simulate user responses following task-specific rules.
"""

import json
from typing import Any

from anthropic import Anthropic


class UserSimulator:
    """User simulator using Anthropic API."""

    def __init__(self, api_key: str, system_prompt: str, model: str = "claude-opus-4-7"):
        self.client = Anthropic(api_key=api_key)
        self.system_prompt = system_prompt
        self.model = model

    def respond(self, conversation: list[dict[str, Any]]) -> str:
        """Generate user response given conversation history.

        Args:
            conversation: Full conversation history including tool call summaries.
                Each message has 'role', 'content', and optionally 'tool_calls'.

        Returns:
            Simulated user response.
        """
        # Build conversation as readable text block for the simulator
        lines: list[str] = []
        for msg in conversation:
            role = msg["role"].upper()
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls")

            if role == "ASSISTANT" and tool_calls:
                tc_summary = "\n".join(
                    f"[Called {tc['name']}({json.dumps(tc.get('arguments', {}), ensure_ascii=False)[:200]})]"
                    for tc in tool_calls
                )
                content = f"{tc_summary}\n{content}" if content else tc_summary

            if content:
                lines.append(f"{role}: {content}")

        conversation_text = "\n\n".join(lines)

        instruction = (
            f"CONVERSATION SO FAR:\n{conversation_text}\n\n"
            "Respond as the customer based on the conversation and your rules above.\n"
            "YOUR RESPONSE:"
        )

        messages = [
            {"role": "user", "content": instruction},
        ]

        response = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=self.system_prompt,
            messages=messages,
        )

        # Extract text from response
        text_content = ""
        for block in response.content:
            if block.type == "text":
                text_content += block.text

        return text_content.strip()
