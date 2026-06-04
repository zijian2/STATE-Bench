"""Main orchestration loop for OpenClaw STATE-Bench evaluation.

Drives a STATE-Bench task through OpenClaw agent with a user simulator,
collects the trajectory, and scores it. Logs to logs/, intermediate state to outputs/.
"""

import argparse
import json
import logging
import os
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from state_bench.schemas import TaskDefinition
from state_bench.domains.travel.simulator import build_simulator_prompt
from state_bench.env_loader import load_task_environment
from state_bench.domain import get_domain_config
from state_bench.paths import domain_tasks_dir

from .simulator import UserSimulator
from .trajectory_parser import parse_openclaw_trajectory, compute_efficiency_metrics


TOOL_SERVER_URL = "http://127.0.0.1:8765"
MAX_AGENT_TURNS = 15
AGENT_TIMEOUT_SECONDS = 180
SIMULATOR_TIMEOUT_SECONDS = 120

logger = logging.getLogger("openclaw_eval")


def setup_logging(log_dir: Path, task_id: str) -> Path:
    """Set up file + console logging for a task run."""
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"{task_id}_{timestamp}.log"

    # Reset handlers (in case of multiple task runs)
    for h in list(logger.handlers):
        logger.removeHandler(h)

    logger.setLevel(logging.DEBUG)

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    ))

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False

    return log_file


def load_task_into_server(task_file: Path) -> dict:
    response = requests.post(
        f"{TOOL_SERVER_URL}/load_task",
        json={"task_file": str(task_file)},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def get_server_snapshot() -> dict:
    response = requests.get(f"{TOOL_SERVER_URL}/snapshot", timeout=30)
    response.raise_for_status()
    return response.json().get("snapshot", {})


def call_openclaw_agent(
    session_key: str,
    message: str,
    agent_id: str = "main",
) -> tuple[dict, float]:
    """Call OpenClaw agent. Returns (response_dict, wall_seconds)."""
    cmd = [
        "openclaw", "agent",
        "--local",
        "--agent", agent_id,
        "--session-key", session_key,
        "--json",
        "-m", message,
    ]
    logger.debug("openclaw cmd: %s", " ".join(cmd[:-1]) + f" '<{len(message)} chars>'")
    t0 = time.monotonic()
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=AGENT_TIMEOUT_SECONDS,
    )
    elapsed = time.monotonic() - t0
    if result.returncode != 0:
        logger.error("openclaw agent stderr: %s", result.stderr[:500])
        raise RuntimeError(f"openclaw agent failed (exit {result.returncode}): {result.stderr[:300]}")
    return json.loads(result.stdout), elapsed


def extract_reply_text(agent_response: dict) -> str:
    payloads = agent_response.get("payloads", [])
    return "\n".join(p.get("text", "") for p in payloads if p.get("text"))


def extract_agent_meta(agent_response: dict) -> dict:
    return agent_response.get("meta", {}).get("agentMeta", {})


def check_termination(user_response: str, domain) -> bool:
    """Defer to the domain's termination check; fallback to canonical [TASK_DONE]."""
    if domain.check_termination is not None:
        return domain.check_termination(user_response)
    return "[TASK_DONE]" in user_response


def save_turn_state(turn_dir: Path, turn_idx: int, payload: dict) -> None:
    """Persist intermediate per-turn state to outputs/<task>/turns/."""
    turn_dir.mkdir(parents=True, exist_ok=True)
    fp = turn_dir / f"turn_{turn_idx:02d}.json"
    with open(fp, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def run_task(
    task_file: Path,
    output_dir: Path,
    log_dir: Path,
    memory_enabled: bool,
    anthropic_api_key: str,
    agent_id: str = "main",
    domain_name: str = "travel",
) -> dict:
    """Run a single STATE-Bench task end-to-end through OpenClaw."""
    task = TaskDefinition.load(task_file)

    # Per-task output and log
    task_output_dir = output_dir / task.task_id
    task_output_dir.mkdir(parents=True, exist_ok=True)
    turns_dir = task_output_dir / "turns"

    log_file = setup_logging(log_dir, task.task_id)

    logger.info("=" * 60)
    logger.info("Task: %s", task.task_id)
    logger.info("Memory: %s", "ON" if memory_enabled else "OFF")
    logger.info("Log file: %s", log_file)
    logger.info("Output dir: %s", task_output_dir)
    logger.info("=" * 60)

    run_t0 = time.monotonic()
    timings: dict[str, Any] = {
        "task_id": task.task_id,
        "started_at": datetime.now().isoformat(),
        "load_task_seconds": 0.0,
        "turns": [],
        "total_seconds": 0.0,
    }

    # 1. Load task into tool server
    t = time.monotonic()
    server_status = load_task_into_server(task_file)
    timings["load_task_seconds"] = round(time.monotonic() - t, 3)
    logger.info("Tool server loaded task: %s (%.2fs)", server_status, timings["load_task_seconds"])

    # 1b. Snapshot environment BEFORE the agent runs (for state-diff scoring).
    snapshot_before = get_server_snapshot()

    # 2. Build user simulator
    domain = get_domain_config(domain_name)
    env_data, _ = load_task_environment(domain, task)
    sim_prompt = build_simulator_prompt(task, env_data, task.user_id)
    simulator = UserSimulator(
        api_key=anthropic_api_key,
        system_prompt=sim_prompt,
        model="claude-opus-4-7",
    )
    logger.debug("Simulator system prompt: %d chars", len(sim_prompt))

    # 3. Unique session for this task
    session_key = f"agent:{agent_id}:state-bench-{task.task_id}-{uuid.uuid4().hex[:8]}"
    logger.info("Session key: %s", session_key)

    # 4. Conversation loop
    conversation_log: list[dict[str, Any]] = [{"role": "user", "content": task.opening_message}]
    logger.info("[user opening] %s", task.opening_message[:200])

    user_message = task.opening_message
    final_session_file: str | None = None
    terminated = False
    last_error: str | None = None
    completed_turns = 0

    for turn in range(MAX_AGENT_TURNS):
        turn_idx = turn + 1
        logger.info("--- Turn %d ---", turn_idx)
        turn_t0 = time.monotonic()
        turn_record: dict[str, Any] = {"turn": turn_idx, "user_message": user_message}

        # ---- Agent step ----
        try:
            agent_response, agent_seconds = call_openclaw_agent(session_key, user_message, agent_id)
        except subprocess.TimeoutExpired:
            last_error = f"agent timeout after {AGENT_TIMEOUT_SECONDS}s"
            logger.error(last_error)
            break
        except Exception as e:
            last_error = f"agent call failed: {e}"
            logger.exception("agent call failed")
            break

        agent_text = extract_reply_text(agent_response)
        agent_meta = extract_agent_meta(agent_response)
        final_session_file = agent_meta.get("sessionFile")
        usage = agent_meta.get("usage", {})

        turn_record["agent_seconds"] = round(agent_seconds, 3)
        turn_record["agent_reply"] = agent_text
        turn_record["agent_usage"] = usage
        turn_record["session_file"] = final_session_file
        logger.info("[agent %.1fs, in=%s out=%s cacheR=%s] %s",
                    agent_seconds, usage.get("input"), usage.get("output"),
                    usage.get("cacheRead"), agent_text[:200] if agent_text else "<empty>")

        conversation_log.append({"role": "assistant", "content": agent_text})
        completed_turns = turn_idx

        if turn_idx >= MAX_AGENT_TURNS:
            logger.info("max turns reached")
            turn_record["turn_seconds"] = round(time.monotonic() - turn_t0, 3)
            timings["turns"].append(turn_record)
            save_turn_state(turns_dir, turn_idx, turn_record)
            break

        # ---- Simulator step ----
        sim_t0 = time.monotonic()
        try:
            user_response = simulator.respond(conversation_log)
        except Exception as e:
            last_error = f"simulator failed: {e}"
            logger.exception("simulator failed")
            turn_record["sim_seconds"] = round(time.monotonic() - sim_t0, 3)
            turn_record["sim_error"] = str(e)
            turn_record["turn_seconds"] = round(time.monotonic() - turn_t0, 3)
            timings["turns"].append(turn_record)
            save_turn_state(turns_dir, turn_idx, turn_record)
            break

        sim_seconds = time.monotonic() - sim_t0
        turn_record["sim_seconds"] = round(sim_seconds, 3)
        turn_record["user_reply"] = user_response
        logger.info("[user %.1fs] %s", sim_seconds, user_response[:200])

        conversation_log.append({"role": "user", "content": user_response})

        if check_termination(user_response, domain):
            logger.info("user terminated conversation")
            terminated = True
            turn_record["terminated"] = True
            turn_record["turn_seconds"] = round(time.monotonic() - turn_t0, 3)
            timings["turns"].append(turn_record)
            save_turn_state(turns_dir, turn_idx, turn_record)
            break

        turn_record["turn_seconds"] = round(time.monotonic() - turn_t0, 3)
        timings["turns"].append(turn_record)
        save_turn_state(turns_dir, turn_idx, turn_record)

        user_message = user_response

    timings["total_seconds"] = round(time.monotonic() - run_t0, 3)
    timings["completed_turns"] = completed_turns
    timings["terminated_by_user"] = terminated
    if last_error:
        timings["error"] = last_error

    # Persist timings
    with open(task_output_dir / "timings.json", "w") as f:
        json.dump(timings, f, indent=2)
    logger.info("Total wall time: %.1fs across %d turns", timings["total_seconds"], completed_turns)

    if last_error:
        return {
            "status": "ERR",
            "task_id": task.task_id,
            "error": last_error,
            "timings": timings,
            "log_file": str(log_file),
        }

    # 5. Parse trajectory
    if not final_session_file:
        return {"status": "ERR", "task_id": task.task_id, "error": "no session file"}

    trajectory_path = Path(final_session_file.replace(".jsonl", ".trajectory.jsonl"))
    logger.info("Trajectory: %s", trajectory_path)
    if not trajectory_path.exists():
        return {"status": "ERR", "task_id": task.task_id, "error": f"trajectory missing: {trajectory_path}"}

    parse_t0 = time.monotonic()
    parsed = parse_openclaw_trajectory(trajectory_path)
    canonical_conversation = parsed["conversation"]
    token_usage = parsed["token_usage"]
    metrics = compute_efficiency_metrics(canonical_conversation)
    timings["parse_seconds"] = round(time.monotonic() - parse_t0, 3)

    # Snapshot environment AFTER and compute state diff for scoring.
    snapshot_after = get_server_snapshot()
    from state_bench.schemas import StateDiff
    state_diff = StateDiff.compute(snapshot_before, snapshot_after)

    logger.info("Metrics: %s", metrics)
    logger.info("Tokens: %s", token_usage)

    # 6. Save STATE-Bench-format trajectory
    output_file = task_output_dir / "trajectory.json"
    output_data = {
        "task_id": task.task_id,
        "user_id": task.user_id,
        "task_summary": task.task_summary,
        "conversation": canonical_conversation,
        "turns": metrics["turns"],
        "tool_calls": metrics["tool_calls"],
        "tool_errors": metrics["tool_errors"],
        "redundant_calls": metrics["redundant_calls"],
        "token_usage": token_usage,
        "state_diff": state_diff.to_dict(),
        "metadata": {
            "memory_enabled": memory_enabled,
            "agent_id": agent_id,
            "session_key": session_key,
            "openclaw_trajectory_path": str(trajectory_path),
            "terminated_by_user": terminated,
            "timings": timings,
        },
    }
    with open(output_file, "w") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)

    logger.info("Saved: %s", output_file)

    # Update timings file with final summary
    with open(task_output_dir / "timings.json", "w") as f:
        json.dump(timings, f, indent=2)

    return {
        "status": "OK",
        "task_id": task.task_id,
        "trajectory_file": str(output_file),
        "metrics": metrics,
        "token_usage": token_usage,
        "terminated_by_user": terminated,
        "timings": timings,
        "log_file": str(log_file),
    }


def main():
    parser = argparse.ArgumentParser(description="Run STATE-Bench evaluation via OpenClaw")
    parser.add_argument("--domain", type=str, default="travel")
    parser.add_argument("--tasks", type=str, required=True, help="Comma-separated task IDs")
    parser.add_argument("--memory", action="store_true", help="Enable OpenClaw memory")
    parser.add_argument("--output-dir", type=str, required=True)
    parser.add_argument("--log-dir", type=str, default=None, help="Log directory (default: <output-dir>/../logs)")
    parser.add_argument("--agent-id", type=str, default="main")
    parser.add_argument("--score", action="store_true", help="Score trajectories after run")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        parser.error("ANTHROPIC_API_KEY environment variable required")

    try:
        health = requests.get(f"{TOOL_SERVER_URL}/health", timeout=5).json()
        print(f"[health] {health}")
    except Exception as e:
        parser.error(f"Tool server not reachable at {TOOL_SERVER_URL}: {e}")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path(args.log_dir).resolve() if args.log_dir else output_dir.parent.parent / "logs"

    tasks_dir = domain_tasks_dir(args.domain)
    task_ids = [t.strip() for t in args.tasks.split(",") if t.strip()]
    task_files = []
    for tid in task_ids:
        tf = tasks_dir / f"{tid}.json"
        if not tf.exists():
            parser.error(f"Task file not found: {tf}")
        task_files.append(tf)

    overall_t0 = time.monotonic()
    results = []
    for task_file in task_files:
        result = run_task(
            task_file=task_file,
            output_dir=output_dir,
            log_dir=log_dir,
            memory_enabled=args.memory,
            anthropic_api_key=api_key,
            agent_id=args.agent_id,
            domain_name=args.domain,
        )
        results.append(result)

    overall_seconds = time.monotonic() - overall_t0

    # Optional scoring
    if args.score:
        from .scorer import AnthropicJudgeClient, score_trajectory
        judge = AnthropicJudgeClient(api_key=api_key)
        for result in results:
            if result["status"] != "OK":
                continue
            traj_file = Path(result["trajectory_file"])
            task_file = tasks_dir / f"{result['task_id']}.json"
            t = time.monotonic()
            score_result = score_trajectory(traj_file, task_file, judge, args.domain)
            score_result["scoring_seconds"] = round(time.monotonic() - t, 3)
            result["score"] = score_result

    # Summary
    print(f"\n{'='*60}\nSummary\n{'='*60}")
    ok = sum(1 for r in results if r["status"] == "OK")
    print(f"OK: {ok}/{len(results)}  Wall: {overall_seconds:.1f}s")
    for r in results:
        line = f"  {r['task_id']}: {r['status']}"
        if r["status"] == "OK":
            m = r["metrics"]
            t = r["timings"]
            line += f" turns={m['turns']} tools={m['tool_calls']} errs={m['tool_errors']} ({t['total_seconds']:.1f}s)"
            if "score" in r and r["score"].get("status") == "OK":
                ux = r["score"].get("ux_score")
                if ux is not None:
                    line += f" ux={ux:.2f}"
        else:
            line += f" — {r.get('error', '?')}"
        print(line)

    summary_file = output_dir / "summary.json"
    with open(summary_file, "w") as f:
        json.dump({
            "memory_enabled": args.memory,
            "overall_seconds": round(overall_seconds, 3),
            "results": results,
        }, f, indent=2)
    print(f"\n[summary] {summary_file}")
    print(f"[logs] {log_dir}")


if __name__ == "__main__":
    main()
