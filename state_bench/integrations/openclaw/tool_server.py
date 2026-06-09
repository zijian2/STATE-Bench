"""FastAPI server wrapping STATE-Bench task environment tool handlers.

Loads a task environment and exposes tool execution endpoints. Supports all
three STATE-Bench domains: travel, customer_support, shopping_assistant.
"""

import json
import traceback
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from state_bench.domain import get_domain_config
from state_bench.environment import BaseEnvironment
from state_bench.schemas import TaskDefinition

app = FastAPI()

# Global environment instance (loaded per task)
_env: BaseEnvironment | None = None
_domain_name: str | None = None


class ToolRequest(BaseModel):
    tool_name: str
    arguments: dict


class ToolResponse(BaseModel):
    content: list[dict]
    details: dict


class LoadTaskRequest(BaseModel):
    task_file: str
    # Optional explicit domain. If omitted, server will attempt to infer
    # the domain from the task's task_env_path (e.g. "state_bench/domains/shopping_assistant/...").
    domain: str | None = None


def _infer_domain(task: TaskDefinition) -> str:
    """Infer domain name from a task's env path or default to travel."""
    env_path = task.task_env_path or ""
    for candidate in ("travel", "customer_support", "shopping_assistant"):
        if f"/domains/{candidate}/" in env_path or env_path.endswith(f"/{candidate}"):
            return candidate
    return "travel"


@app.post("/load_task")
async def load_task(request: LoadTaskRequest) -> dict[str, str]:
    """Load a STATE-Bench task and initialize the matching domain environment."""
    global _env, _domain_name

    try:
        task = TaskDefinition.load(Path(request.task_file))

        domain_name = request.domain or _infer_domain(task)
        domain = get_domain_config(domain_name)

        from state_bench.env_loader import load_task_environment

        env_data, _env_path = load_task_environment(domain, task)
        _env = domain.environment_class(env_data, now=task.now)
        _domain_name = domain_name

        return {
            "status": "ok",
            "task_id": task.task_id,
            "user_id": task.user_id,
            "domain": domain_name,
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to load task: {e}") from e


@app.post("/execute_tool")
async def execute_tool(request: ToolRequest) -> ToolResponse:
    """Execute a STATE-Bench tool on the active environment and return results."""
    global _env

    if _env is None:
        raise HTTPException(status_code=400, detail="No task loaded. Call /load_task first.")

    tool_name = request.tool_name
    handlers = _env.tool_handlers

    if tool_name not in handlers:
        raise HTTPException(
            status_code=404,
            detail=f"Tool '{tool_name}' not found in domain '{_domain_name}'",
        )

    try:
        handler = handlers[tool_name]
        result = handler(request.arguments)

        # Convert STATE-Bench result format to OpenClaw plugin format
        if isinstance(result, dict) and "error" in result:
            return ToolResponse(
                content=[{"type": "text", "text": result["error"]}],
                details={"error": True},
            )
        if isinstance(result, dict):
            return ToolResponse(
                content=[{"type": "text", "text": json.dumps(result, indent=2)}],
                details={},
            )
        return ToolResponse(
            content=[{"type": "text", "text": str(result)}],
            details={},
        )
    except Exception as e:  # noqa: BLE001
        return ToolResponse(
            content=[
                {
                    "type": "text",
                    "text": f"Tool execution error: {e}\n{traceback.format_exc()}",
                }
            ],
            details={"error": True, "exception": str(e)},
        )


@app.get("/health")
async def health() -> dict[str, Any]:
    """Health check endpoint."""
    return {
        "status": "ok",
        "environment_loaded": _env is not None,
        "domain": _domain_name,
    }


@app.get("/snapshot")
async def snapshot() -> dict[str, Any]:
    """Return a full snapshot of the environment state for state-diff computation."""
    global _env
    if _env is None:
        raise HTTPException(status_code=400, detail="No task loaded.")
    return {"snapshot": _env.get_full_snapshot(), "domain": _domain_name}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)
