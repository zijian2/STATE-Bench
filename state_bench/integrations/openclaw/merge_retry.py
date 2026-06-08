"""Merge retry-run results into the main summary.json.

Reads:
  <main-output>/summary.json           (original results, including failed tasks)
  <retry-output>/summary.json          (retry results)
  <retry-output>/<task_id>/...         (retry trajectories — copied to main output)

Writes:
  <main-output>/summary.json           (updated; original backed up to summary.before-merge.json)
  <main-output>/<task_id>/...          (retry trajectory + timings + turns)

Usage:
  python -m state_bench.integrations.openclaw.merge_retry \
      --main outputs/travel-full-vector \
      --retry outputs/travel-full-vector-retry
"""

import argparse
import json
import shutil
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Merge retry summary into main summary")
    parser.add_argument("--main", type=str, required=True, help="Main output dir (will be updated in place)")
    parser.add_argument("--retry", type=str, required=True, help="Retry output dir to merge from")
    parser.add_argument("--prefer", choices=["retry", "ok"], default="ok",
                        help="ok = keep main result if it was OK; retry = always take retry. Default: ok")
    args = parser.parse_args()

    main_dir = Path(args.main).resolve()
    retry_dir = Path(args.retry).resolve()

    main_summary_file = main_dir / "summary.json"
    retry_summary_file = retry_dir / "summary.json"
    if not main_summary_file.exists():
        parser.error(f"Main summary not found: {main_summary_file}")
    if not retry_summary_file.exists():
        parser.error(f"Retry summary not found: {retry_summary_file}")

    with open(main_summary_file) as f:
        main_summary = json.load(f)
    with open(retry_summary_file) as f:
        retry_summary = json.load(f)

    main_results: list[dict] = main_summary.get("results", [])
    retry_results: list[dict] = retry_summary.get("results", [])

    # Index main results by task_id for in-place replacement
    main_by_id = {r["task_id"]: i for i, r in enumerate(main_results)}

    backup = main_dir / "summary.before-merge.json"
    shutil.copy(main_summary_file, backup)
    print(f"[merge] backed up main summary to {backup}")

    replaced = 0
    appended = 0
    skipped = 0

    for retry_r in retry_results:
        tid = retry_r["task_id"]
        # Decide whether to keep this retry
        if tid in main_by_id:
            existing = main_results[main_by_id[tid]]
            if args.prefer == "ok" and existing.get("status") == "OK" and retry_r.get("status") != "OK":
                skipped += 1
                continue
        # Copy per-task subdir from retry → main
        src_subdir = retry_dir / tid
        dst_subdir = main_dir / tid
        if src_subdir.exists():
            if dst_subdir.exists():
                shutil.rmtree(dst_subdir)
            shutil.copytree(src_subdir, dst_subdir)
            # Rewrite trajectory_file path in retry_r if it pointed to retry dir
            if "trajectory_file" in retry_r:
                retry_r["trajectory_file"] = str(dst_subdir / "trajectory.json")
            if "log_file" in retry_r:
                # log files live in logs/, just leave as-is (they're absolute)
                pass

        if tid in main_by_id:
            main_results[main_by_id[tid]] = retry_r
            replaced += 1
        else:
            main_results.append(retry_r)
            main_by_id[tid] = len(main_results) - 1
            appended += 1

    # Re-sort by task_id (so summary stays deterministic and matches the file scan order)
    main_results.sort(key=lambda r: r["task_id"])

    # Update aggregate fields
    main_summary["results"] = main_results
    # overall_seconds: keep the original (we're merging, not re-running everything)

    with open(main_summary_file, "w") as f:
        json.dump(main_summary, f, indent=2, ensure_ascii=False)

    ok = sum(1 for r in main_results if r.get("status") == "OK")
    print(f"[merge] replaced {replaced}  appended {appended}  skipped {skipped}")
    print(f"[merge] main summary now has {len(main_results)} tasks, {ok} OK")
    print(f"[merge] wrote {main_summary_file}")


if __name__ == "__main__":
    main()
