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
└── scorer.py                      # STATE-Bench scoring with Anthropic judges
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
- Uses Claude Sonnet 4 with extended thinking for judge reasoning

### 6. Runner (TODO)

Main orchestration loop that:
- Loads STATE-Bench tasks
- Starts tool_server with task environment
- Creates OpenClaw agent with/without memory
- Runs conversation loop with user simulator
- Parses and scores trajectories
- Aggregates results

## Installation

1. Install STATE-Bench plugin:
```bash
cd openclaw-eval/plugins/state-bench-travel
openclaw plugins install --link .
openclaw gateway restart
```

2. Enable plugin tools for your agent:
```bash
# Add all 17 tools to agent config
openclaw config set agents.list[0].tools.alsoAllow '["search_flights","get_user_details","get_user_reservations","get_booking","get_flight_status","get_policies","create_booking","update_booking","cancel_booking","search_hotels","book_hotel","get_hotel_reservation","cancel_hotel_reservation","search_car_rentals","book_car_rental","get_car_rental","cancel_car_rental"]'
```

3. Install Python dependencies:
```bash
pip install fastapi uvicorn anthropic
```

## Usage (Planned)

```bash
# Run with memory enabled
python runner.py \
  --domain travel \
  --tasks task_001,task_002 \
  --memory \
  --output-dir outputs/travel-memory-on

# Run with memory disabled
python runner.py \
  --domain travel \
  --tasks task_001,task_002 \
  --output-dir outputs/travel-memory-off

# Compare results
python compare_results.py \
  --baseline outputs/travel-memory-off \
  --treatment outputs/travel-memory-on
```

## Status

- [x] Plugin structure (manifest, package.json, index.js)
- [x] Tool server (FastAPI + STATE-Bench environment wrapper)
- [x] User simulator (Anthropic client)
- [x] Trajectory parser (OpenClaw → STATE-Bench format)
- [x] Scorer (Anthropic-powered judges)
- [ ] Runner orchestration loop
- [ ] OpenClaw agent integration
- [ ] End-to-end testing
- [ ] Results comparison utilities
