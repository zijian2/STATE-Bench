"""Convert OpenClaw runner output to STATE-Bench compute_metrics format.

Reads:
  outputs/<run-name>/summary.json       (per-task scores)
  outputs/<run-name>/<task_id>/trajectory.json   (raw trajectories)

Writes:
  outputs/<run-name>/run1/<task_id>.json   (merged + flattened)

After conversion, run:
  python -m state_bench.scripts.compute_metrics \
      --domain travel --results-dir outputs/<run-name> \
      --num-runs 1 --output-dir outputs/<run-name> --ignore-missing-runs

Usage:
  python -m state_bench.integrations.openclaw.adapt_for_metrics \
      --output-dir outputs/travel-full-vector [--domain travel] [--run-index 1]
"""

import argparse
import json
from pathlib import Path

from state_bench.scoring import evaluate_state_requirements
from state_bench.schemas import StateDiff, TaskDefinition
from state_bench.integrations.openclaw.runner import domain_tasks_dir


def merge_traj_and_score(
    trajectory: dict,
    score: dict | None,
    metrics: dict | None,
    token_usage: dict | None,
    task: TaskDefinition,
) -> dict:
    """Produce a single trajectory dict that compute_metrics.load_run() will accept.

    compute_metrics expects flat top-level fields:
      task_completion_pass, ux_score, state_requirements_met, task_requirements_met,
      ux_user_control / ux_friction / ux_situational_awareness /
      ux_communication_quality / ux_intent_alignment, ux_reasoning,
      state_requirements_reasoning, task_requirements_reasoning,
      turns, tool_calls, token_usage / efficiency, agent_model, agent_pricing.
    """
    out = dict(trajectory)  # keep conversation/state_diff/etc untouched

    # ---- evaluate state_requirements locally from saved state_diff ----
    sd_dict = trajectory.get("state_diff") or {}
    state_diff = StateDiff(
        created=sd_dict.get("created", {}) or {},
        modified=sd_dict.get("modified", {}) or {},
        deleted=sd_dict.get("deleted", {}) or {},
    )
    state_score = evaluate_state_requirements(task, state_diff)
    state_pass = None if state_score is None else int(state_score.score)
    out["state_requirements_met"] = state_pass
    out["state_requirements_reasoning"] = (
        state_score.reasoning if state_score is not None else ""
    )

    # ---- task_requirements ----
    task_req = (score or {}).get("task_requirements") or {}
    task_req_score = task_req.get("score")
    task_pass = None if task_req_score is None else int(task_req_score)
    out["task_requirements_met"] = task_pass
    out["task_requirements_reasoning"] = task_req.get("reasoning", "")
    out["task_requirements_details"] = task_req.get("details", [])

    # ---- task_completion_pass = state AND task ----
    if state_pass is None or task_pass is None:
        out["task_completion_pass"] = None
    else:
        out["task_completion_pass"] = int(state_pass == 1 and task_pass == 1)

    # ---- UX dimensions ----
    ux = (score or {}).get("ux_quality") or {}
    out["ux_score"] = ux.get("ux_score") or (score or {}).get("ux_score")
    out["ux_reasoning"] = ux.get("ux_reasoning", "")
    for dim in (
        "ux_user_control",
        "ux_friction",
        "ux_situational_awareness",
        "ux_communication_quality",
        "ux_intent_alignment",
    ):
        if dim in ux:
            out[dim] = ux[dim]

    # ---- efficiency / metrics flat fields ----
    metrics = metrics or {}
    eff = {
        "turns": metrics.get("turns", 0),
        "tool_calls": metrics.get("tool_calls", 0),
        "tool_errors": metrics.get("tool_errors", 0),
        "redundant_calls": metrics.get("redundant_calls", 0),
    }
    out.setdefault("efficiency", eff)
    out["turns"] = metrics.get("turns") or trajectory.get("turns") or 0
    out["tool_calls"] = (
        metrics.get("tool_calls")
        if isinstance(metrics.get("tool_calls"), int)
        else (len(trajectory.get("tool_calls") or []) if isinstance(trajectory.get("tool_calls"), list) else 0)
    )

    # ---- token usage (dict already on trajectory; keep as object) ----
    if token_usage:
        out["token_usage"] = token_usage

    return out


def main():
    parser = argparse.ArgumentParser(description="Adapt OpenClaw runner output for compute_metrics")
    parser.add_argument("--output-dir", type=str, required=True, help="Runner output dir (contains summary.json and per-task subdirs)")
    parser.add_argument("--domain", type=str, default="travel")
    parser.add_argument("--run-index", type=int, default=1, help="Write to run<N>/ subdir (default: 1)")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing run dir contents")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    summary_file = output_dir / "summary.json"
    if not summary_file.exists():
        parser.error(f"summary.json not found in {output_dir}")

    with open(summary_file) as f:
        summary = json.load(f)

    tasks_dir = domain_tasks_dir(args.domain)
    run_dir = output_dir / f"run{args.run_index}"
    run_dir.mkdir(exist_ok=True)

    written = 0
    skipped = 0
    errors = []

    for result in summary.get("results", []):
        if result.get("status") != "OK":
            skipped += 1
            continue
        task_id = result["task_id"]
        traj_path = output_dir / task_id / "trajectory.json"
        if not traj_path.exists():
            errors.append(f"{task_id}: trajectory.json missing")
            continue

        try:
            with open(traj_path) as f:
                trajectory = json.load(f)
            task_file = tasks_dir / f"{task_id}.json"
            task = TaskDefinition.load(task_file)
            merged = merge_traj_and_score(
                trajectory=trajectory,
                score=result.get("score"),
                metrics=result.get("metrics"),
                token_usage=result.get("token_usage"),
                task=task,
            )
            out_path = run_dir / f"{task_id}.json"
            if out_path.exists() and not args.overwrite:
                pass  # always overwrite (idempotent merge)
            with open(out_path, "w") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False)
            written += 1
        except Exception as e:
            errors.append(f"{task_id}: {e}")

    print(f"[adapt] wrote {written} files to {run_dir}")
    if skipped:
        print(f"[adapt] skipped {skipped} (status != OK)")
    if errors:
        print(f"[adapt] {len(errors)} errors:")
        for e in errors[:10]:
            print(f"  - {e}")
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more")

    print()
    print("Next: compute aggregate metrics with:")
    print(f"  python -m state_bench.scripts.compute_metrics \\")
    print(f"      --domain {args.domain} --results-dir {output_dir} \\")
    print(f"      --num-runs 1 --output-dir {output_dir} --ignore-missing-runs")


if __name__ == "__main__":
    main()
