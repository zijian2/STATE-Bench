"""FastAPI server wrapping STATE-Bench task environment tool handlers.

Loads a task environment and exposes tool execution endpoints.
"""

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from state_bench.domains.travel.environment import TravelEnvironment
from state_bench.domains.travel.schemas import EnvironmentData
from state_bench.schemas import TaskDefinition


app = FastAPI()

# Global environment instance (loaded per task)
_env: TravelEnvironment | None = None


class ToolRequest(BaseModel):
    tool_name: str
    arguments: dict


class ToolResponse(BaseModel):
    content: list[dict]
    details: dict


class LoadTaskRequest(BaseModel):
    task_file: str


@app.post("/load_task")
async def load_task(request: LoadTaskRequest) -> dict[str, str]:
    """Load a STATE-Bench task and initialize the environment."""
    global _env

    try:
        task = TaskDefinition.load(Path(request.task_file))

        # Load environment data from task's environment file
        from state_bench.env_loader import load_task_environment
        from state_bench.domain import get_domain_config

        domain = get_domain_config("travel")
        env_data, _env_path = load_task_environment(domain, task)

        # Create environment instance
        _env = TravelEnvironment(env_data, task.now)

        return {"status": "ok", "task_id": task.task_id, "user_id": task.user_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load task: {str(e)}")


@app.post("/execute_tool")
async def execute_tool(request: ToolRequest) -> ToolResponse:
    """Execute a STATE-Bench tool and return results."""
    global _env

    if _env is None:
        raise HTTPException(status_code=400, detail="No task loaded. Call /load_task first.")

    tool_name = request.tool_name
    handlers = _env.tool_handlers

    if tool_name not in handlers:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")

    try:
        handler = handlers[tool_name]
        result = handler(request.arguments)

        # Convert STATE-Bench result format to OpenClaw plugin format
        if isinstance(result, dict) and "error" in result:
            # Error result
            return ToolResponse(
                content=[{"type": "text", "text": result["error"]}],
                details={"error": True}
            )
        elif isinstance(result, dict) and "results" in result:
            # Search results (multiple items)
            import json
            return ToolResponse(
                content=[{"type": "text", "text": json.dumps(result, indent=2)}],
                details={}
            )
        elif isinstance(result, dict):
            # Single item result
            import json
            return ToolResponse(
                content=[{"type": "text", "text": json.dumps(result, indent=2)}],
                details={}
            )
        else:
            # Unexpected format
            return ToolResponse(
                content=[{"type": "text", "text": str(result)}],
                details={}
            )
    except Exception as e:
        import traceback
        return ToolResponse(
            content=[{"type": "text", "text": f"Tool execution error: {str(e)}\n{traceback.format_exc()}"}],
            details={"error": True, "exception": str(e)}
        )


@app.get("/health")
async def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok", "environment_loaded": str(_env is not None)}


@app.get("/snapshot")
async def snapshot() -> dict:
    """Return a full snapshot of the environment state for state-diff computation."""
    global _env
    if _env is None:
        raise HTTPException(status_code=400, detail="No task loaded.")
    return {"snapshot": _env.get_full_snapshot()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
