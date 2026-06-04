"""STATE-Bench scoring adapter with Anthropic judge client.

Wraps state_bench.scoring judges and shims them to use Anthropic API.
The judges call `client.complete_json(prompt, system_prompt, max_tokens, reasoning_effort)`
and expect a parsed `dict`.
"""

import json
import re
from pathlib import Path
from typing import Any

from state_bench.scoring import TaskRequirementsJudge, UXQualityJudge
from state_bench.schemas import StateDiff, TaskDefinition
from state_bench.domain import get_domain_config
from anthropic import Anthropic


_THINKING_BUDGET = {"low": 1000, "medium": 5000, "high": 10000}


class AnthropicJudgeClient:
    """LLM client adapter for Anthropic API matching STATE-Bench judge interface.

    Exposes `complete_json(prompt, system_prompt, max_tokens, reasoning_effort) -> dict`.
    """

    def __init__(self, api_key: str, model: str = "claude-opus-4-7"):
        self.client = Anthropic(api_key=api_key)
        self.model = model

    def complete_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        max_tokens: int = 8192,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_prompt:
            params["system"] = system_prompt
        if reasoning_effort:
            params["thinking"] = {
                "type": "enabled",
                "budget_tokens": _THINKING_BUDGET.get(reasoning_effort, 5000),
            }

        response = self.client.messages.create(**params)
        text = "".join(block.text for block in response.content if block.type == "text").strip()

        # Robust JSON extraction: strip ```json fences if present, then take outermost {...}.
        cleaned = text
        fence = re.search(r"```(?:json)?\s*(.+?)\s*```", cleaned, re.DOTALL)
        if fence:
            cleaned = fence.group(1).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if not m:
                raise json.JSONDecodeError(f"No JSON object in judge output: {text[:200]!r}", text, 0)
            return json.loads(m.group(0))


def _flatten_tool_calls(conversation: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract a flat list of tool calls from the canonical conversation.

    Pairs each assistant tool_call with its matching tool_result by id where available.
    """
    results_by_id: dict[str, dict[str, Any]] = {}
    for msg in conversation:
        if msg.get("role") == "tool":
            tid = msg.get("tool_use_id", "")
            results_by_id[tid] = {
                "content": msg.get("content", ""),
                "is_error": msg.get("is_error", False),
            }

    flat: list[dict[str, Any]] = []
    for msg in conversation:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls", []) or []:
            tid = tc.get("id", "")
            res = results_by_id.get(tid)
            entry: dict[str, Any] = {
                "name": tc.get("name", ""),
                "arguments": tc.get("arguments", {}),
            }
            if res is not None:
                entry["result"] = {"error": res["content"]} if res["is_error"] else {"output": res["content"]}
            flat.append(entry)
    return flat


def score_trajectory(
    trajectory_path: Path,
    task_file: Path,
    judge_client: AnthropicJudgeClient,
    domain_name: str,
    state_diff: StateDiff | None = None,
    protocol: Any = None,
) -> dict:
    """Score a trajectory using STATE-Bench judges with Anthropic client."""
    task = TaskDefinition.load(task_file)
    with open(trajectory_path) as f:
        traj_data = json.load(f)

    domain = get_domain_config(domain_name)
    conversation = traj_data.get("conversation", [])
    tool_calls = _flatten_tool_calls(conversation)

    if state_diff is None:
        # Fall back to whatever was persisted on the trajectory, else empty.
        sd_dict = traj_data.get("state_diff") or {}
        state_diff = StateDiff(
            created=sd_dict.get("created", {}) or {},
            modified=sd_dict.get("modified", {}) or {},
            deleted=sd_dict.get("deleted", {}) or {},
        )

    reasoning_effort = "medium"
    if protocol is not None and hasattr(protocol, "judge_reasoning_effort"):
        reasoning_effort = protocol.judge_reasoning_effort

    task_requirements_judge = TaskRequirementsJudge(
        client=judge_client,
        prompts_dir=domain.prompts_dir,
        system_prompt=domain.judge_system_prompt,
        reasoning_effort=reasoning_effort,
    )
    ux_judge = UXQualityJudge(
        client=judge_client,
        prompts_dir=domain.prompts_dir,
        system_prompt=domain.judge_system_prompt,
        reasoning_effort=reasoning_effort,
    )

    try:
        task_req_result = task_requirements_judge.evaluate(
            task=task,
            conversation=conversation,
            tool_calls=tool_calls,
            state_diff=state_diff,
        )
        ux_result = ux_judge.evaluate(
            task=task,
            conversation=conversation,
            tool_calls=tool_calls,
        )
        return {
            "status": "OK",
            "task_requirements": task_req_result.to_dict() if task_req_result else None,
            "ux_quality": ux_result.to_dict() if ux_result else None,
            "ux_score": ux_result.ux_score if ux_result else None,
        }
    except Exception as e:
        import traceback
        return {
            "status": "ERR",
            "error": str(e),
            "traceback": traceback.format_exc(),
        }
