# OpenClaw STATE-Bench Evaluation

OpenClaw-based evaluation harness for STATE-Bench tasks.

## Architecture

```
openclaw-eval/
├── plugins/state-bench-travel/    # OpenClaw plugin exposing STATE-Bench travel tools
│   ├── openclaw.plugin.json       # Plugin manifest
│   ├── package.json               # NPM package config
│   └── index.js                   # Plugin implementation (17 tools)
├── tool_server.py                 # FastAPI server wrapping STATE-Bench environment
├── simulator.py                   # Anthropic-powered user simulator
├── runner.py                      # Main orchestration loop
├── trajectory_parser.py           # OpenClaw → STATE-Bench trajectory converter
├── scorer.py                      # STATE-Bench scoring with Anthropic judges
├── adapt_for_metrics.py           # Convert runner output to compute_metrics format
└── merge_retry.py                 # Merge retry-run results into main summary
```

## Key Design

**OpenClaw as orchestrator**: Unlike STATE-Bench's built-in agent loop, OpenClaw handles the entire agent runtime (session management, tool calling, memory, conversation flow). STATE-Bench provides only tasks, environment, and scoring.

**Memory ablation**: Run identical tasks with OpenClaw memory ON vs OFF to measure memory's effect on agent performance.

**Plugin tools**: All 17 STATE-Bench travel domain tools exposed as OpenClaw plugin tools. Plugin calls tool_server.py which wraps the STATE-Bench environment.

**Anthropic-native**: User simulator and judges use Anthropic API directly instead of STATE-Bench's Azure/OpenAI clients.

## Components

### 1. Travel Tools Plugin

Registers 17 STATE-Bench travel tools as OpenClaw plugin tools:
- Flight tools: search_flights, get_booking, create_booking, update_booking, cancel_booking, get_flight_status
- User tools: get_user_details, get_user_reservations
- Policy tools: get_policies
- Hotel tools: search_hotels, book_hotel, get_hotel_reservation, cancel_hotel_reservation
- Car rental tools: search_car_rentals, book_car_rental, get_car_rental, cancel_car_rental

Each tool forwards requests to tool_server.py.

### 2. Tool Server

FastAPI server that:
- Loads a STATE-Bench task environment via `/load_task`
- Executes tool calls via `/execute_tool`
- Returns full environment snapshot via `/snapshot`
- Maintains environment state for the duration of a task run

### 3. User Simulator

Anthropic-powered simulator following STATE-Bench's UserSimulatorConfig:
- Receives system prompt assembled from task personality + rules + context
- Generates user responses based on conversation history
- Uses Claude Sonnet 4 by default

### 4. Trajectory Parser

Converts OpenClaw `.trajectory.jsonl` event stream to STATE-Bench canonical format:
- Groups events into turns with role, content, tool_calls
- Extracts tool call parameters and results
- Computes efficiency metrics (turns, tool_calls, redundancy)

### 5. Scorer

Wraps STATE-Bench judges with Anthropic client:
- TaskRequirementsJudge: evaluates task completion
- UXQualityJudge: evaluates user experience (control, friction, awareness, communication, alignment)
- Uses Claude Opus 4-7 with extended thinking for judge reasoning

### 6. Runner

Main orchestration loop that:
- Loads STATE-Bench tasks (optionally filters via `--tasks`)
- Starts conversation loop with task context injected as `[Task: {id} | User: {user_id}]`
- Spawns OpenClaw agent subprocess with/without memory
- Drives user simulator turns until task completion or timeout
- Parses OpenClaw trajectory and scores immediately (incremental mode)
- Writes per-task results to `outputs/<run>/<task_id>/trajectory.json`
- Maintains `summary.json` with all task results (updated after each task)

**Key features:**
- `--score`: Score each trajectory immediately after task completion (incremental, crash-safe)
- `--score-only`: Score existing trajectories without re-running tasks (supports `--rescore`)
- `--tasks`: Run only specified tasks (comma-separated IDs); omit to run all tasks in domain
- `--memory`: Enable OpenClaw memory search and ingestion

### 7. Metrics Adapter (`adapt_for_metrics.py`)

Converts runner output to STATE-Bench `compute_metrics` format:
- Reads `summary.json` (contains scores) and per-task `trajectory.json` files
- Evaluates `state_requirements` locally from saved `state_diff`
- Merges score fields into trajectory top-level (flattens nested structure)
- Writes adapted trajectories to `outputs/<run>/run1/<task_id>.json`

Required because:
- STATE-Bench's `compute_metrics` expects trajectories with scoring fields at top level
- Runner writes scores nested in `summary.json` and doesn't embed them in trajectory files
- State requirements evaluation happens locally (scorer only evaluates task requirements + UX)

### 8. Retry Merger (`merge_retry.py`)

Merges retry-run results into main summary:
- Reads main `summary.json` and retry `summary.json`
- Replaces failed tasks with retry results (or appends new tasks)
- Copies retry trajectory subdirectories to main output
- Updates main `summary.json` in place (backs up to `summary.before-merge.json`)

## Installation

1. Install STATE-Bench plugin:
```bash
cd ./state_bench/integrations/openclaw/plugins/state-bench-travel
openclaw plugins install --link .
openclaw gateway restart

## same as shopping and customer-support
```

2. Enable plugin tools for your agent:
```bash
# Add all 17 tools to agent config
openclaw config set agents.list[0].tools.alsoAllow '["search_flights","get_user_details","get_user_reservations","get_booking","get_flight_status","get_policies","create_booking","update_booking","cancel_booking","search_hotels","book_hotel","get_hotel_reservation","cancel_hotel_reservation","search_car_rentals","book_car_rental","get_car_rental","cancel_car_rental","get_order","get_customer","search_products","get_product_details","get_warranty_status","process_return","process_refund","cancel_order","process_exchange","process_warranty_claim","get_variants","get_customer_account","get_cart","check_compatibility","get_promotions","validate_promo","add_to_cart","update_cart_item","remove_from_cart","apply_promo","remove_promo","redeem_loyalty_points","cancel_loyalty_redemption","get_shipping_options","set_shipping_option"]'
openclaw gateway restart
```

3. Install Python dependencies:
```bash
export ANTHROPIC_API_KEY=$(python -c "import json; cfg=json.load(open('/root/.openclaw/openclaw.json')); print(cfg['models']['providers']['claude']['apiKey'])")
export ANTHROPIC_BASE_URL="http://one.iflytek.com/api/llm/console/chat"
uv sync --index https://pypi.tuna.tsinghua.edu.cn/simple --extra openclaw
```

## Usage

### 1. Start Tool Server

The tool server must be running before starting the runner.

```bash
cd /root/.openclaw/workspace/STATE-Bench
# . .venv/bin/activate
nohup uv run  python -m state_bench.integrations.openclaw.tool_server > /tmp/tool_server.log 2>&1 &
curl http://127.0.0.1:8765/health  # Verify it's running
```

### 2. Run Tasks

**Run all tasks with memory + incremental scoring:**
```bash
# python -m state_bench.integrations.openclaw.runner \
#   --memory --score \
#   --agent-id state-bench \
#   --output-dir outputs/travel-full-vector

nohup uv run python -m state_bench.integrations.openclaw.runner \
  --memory \
  --score \
  --agent-id state-bench \
  --output-dir outputs/all-domain \
  > /tmp/state_bench_runner.log 2>&1 &
```

**Run specific tasks:**
```bash
python -m state_bench.integrations.openclaw.runner \
  --memory --score \
  --agent-id state-bench \
  --tasks 1-cancel_economy_domestic,5-cancel_airline_cancelled \
  --output-dir outputs/travel-subset
```

**Score existing trajectories only (no re-run):**
```bash
python -m state_bench.integrations.openclaw.runner \
  --score-only \
  --output-dir outputs/travel-full-vector
```

**Re-score all tasks (overwrite existing scores):**
```bash
python -m state_bench.integrations.openclaw.runner \
  --score-only --rescore \
  --output-dir outputs/travel-full-vector
```

### 3. Retry Failed Tasks

If some tasks failed (timeout, error), retry them and merge:

```bash
# 1. Find failed task IDs from summary.json
python -c "import json; s=json.load(open('outputs/travel-full-vector/summary.json')); \
  failed=[r['task_id'] for r in s['results'] if r.get('status')!='OK']; \
  print(','.join(failed))"

# 2. Run failed tasks to a separate output dir
python -m state_bench.integrations.openclaw.runner \
  --memory --score \
  --agent-id state-bench \
  --tasks <failed_ids_from_step_1> \
  --output-dir outputs/travel-full-vector-retry

# 3. Merge retry results into main output
python -m state_bench.integrations.openclaw.merge_retry \
  --main outputs/travel-full-vector \
  --retry outputs/travel-full-vector-retry
```

### 4. Compute Aggregate Metrics

STATE-Bench's `compute_metrics` script provides aggregate analysis (task completion rate, mean UX, per-dimension breakdown, efficiency metrics). It requires a specific directory structure; use `adapt_for_metrics` to convert runner output:

```bash
# 1. Adapt runner output to compute_metrics format
python -m state_bench.integrations.openclaw.adapt_for_metrics \
  --output-dir outputs/full-run \
  --domain all \
  --run-index 1

# 2. Compute aggregate metrics
python -m state_bench.scripts.compute_metrics \
  --domain all \
  --results-dir outputs/full-run \
  --num-runs 1 \
  --output-dir outputs/full-run \
  --ignore-missing-runs
```

**Output files:**
- `outputs/travel-full-vector/metrics.json` — Standardized aggregate metrics
- `outputs/travel-full-vector/per_task_metrics/{task_id}.json` — Per-task detailed scores (144 files)
- `outputs/travel-full-vector/run1/{task_id}.json` — Adapted trajectories (input for compute_metrics)

**Key aggregate metrics:**
- `task_completion_pass@1` — Overall task completion rate (state + task requirements)
- `Mean UX score` — Average across all 5 UX dimensions
- `state_pass@1` / `task_requirements_pass@1` — Component pass rates
- Per-dimension UX scores (user control, friction, situational awareness, communication, intent alignment)
- Efficiency metrics (mean turns, tool calls, tokens, cost)

## Output Structure

```
outputs/<run-name>/
├── summary.json                   # All task results (updated incrementally)
├── summary.backup.json            # Backup before merge (if applicable)
├── summary.before-merge.json      # Backup before retry merge
├── <task_id>/
│   ├── trajectory.json            # STATE-Bench canonical trajectory
│   ├── timings.json               # Per-turn timing data
│   └── turns/                     # Per-turn conversation snapshots
├── run1/                          # Adapted for compute_metrics (created by adapt_for_metrics.py)
│   └── <task_id>.json             # Merged trajectory + scores (flat structure)
├── metrics.json                   # Aggregate metrics (created by compute_metrics)
└── per_task_metrics/              # Per-task detailed metrics (created by compute_metrics)
    └── <task_id>.json
```

## Implementation Status

- [x] Plugin structure (manifest, package.json, index.js)
- [x] Tool server (FastAPI + STATE-Bench environment wrapper)
- [x] User simulator (Anthropic client)
- [x] Trajectory parser (OpenClaw → STATE-Bench format)
- [x] Scorer (Anthropic-powered judges)
- [x] Runner orchestration loop
- [x] Task context injection (`[Task: {id} | User: {user_id}]`)
- [x] Incremental scoring (`--score` with crash-safe progress)
- [x] Score-only mode (`--score-only` + `--rescore`)
- [x] Metrics adapter (convert to compute_metrics format)
- [x] Retry merger (merge retry results into main summary)
- [x] Full pipeline tested (144/150 tasks scored in travel domain)
- [ ] Multi-run support (currently single-run only)
- [ ] Memory ablation comparison utilities

## Known Limitations

- **Agent timeout**: Runner uses 180s timeout per task; complex multi-step tasks may hit this limit
- **Single-run only**: Designed for 1-run analysis; STATE-Bench protocol requires 5 runs for official submissions
- **No cost tracking**: Trajectory doesn't record `agent_model` or `agent_pricing` metadata (compute_metrics will skip cost calculations)
- **State requirements evaluation**: Done locally by adapter (not by scorer); judges only evaluate task_requirements + UX

## Troubleshooting

**Tool server not reachable:**
```bash
curl http://127.0.0.1:8765/health
# If failed, restart: pkill -f tool_server.py && nohup python -m ... &
```

**Agent timeout on complex tasks:**
- Increase `AGENT_TIMEOUT_SECONDS` in `runner.py` (default: 180)
- Check logs in `logs/<task_id>_*.log` for stuck turns

**compute_metrics "incomplete split" error:**
- Use `--ignore-missing-runs` if running partial task set for local analysis
- STATE-Bench protocol validation requires all split tasks present

**Retry tasks still failing:**
- Check simulator timeout settings in `simulator.py`
- Verify API_KEY and BASE_URL are set correctly
- Review turn-level logs in task output directory
