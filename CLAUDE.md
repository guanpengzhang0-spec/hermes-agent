# Hermes Agent

## Identity
Hermes Agent — an open-source AI agent framework by Nous Research. Runs in CLI, TUI, or gateway mode across 20+ messaging platforms.

## Key Files
- `run_agent.py` — AIAgent class, core conversation loop (~12k LOC)
- `cli.py` — HermesCLI class, interactive CLI orchestrator (~11k LOC)
- `model_tools.py` — Tool orchestration, tool discovery
- `toolsets.py` — Toolset definitions
- `hermes_state.py` — SessionDB, SQLite session store with FTS5
- `agent/` — Provider adapters, memory, caching, compression
- `tools/` — Tool implementations (auto-discovered)
- `gateway/` — Messaging gateway, platform adapters (20+)
- `acp_adapter/` — ACP server for IDE integration
- `cron/` — Job scheduler
- `tests/` — ~15k tests across ~700 files

## Key Commands
- `scripts/run_tests.sh` — Run test suite
- Source venv: `. .venv/bin/activate`
- CLI entry: `hermes` (or `python cli.py`)
- Gateway: `hermes gateway run`

## Architecture
- File dependency chain: `tools/registry.py` (no deps) → `tools/*.py` → `model_tools.py` → `run_agent.py`/`cli.py`
- Agent loop: synchronous, budget-tracked, interrupt-aware
- Config: `~/.hermes/config.yaml` (settings), `~/.hermes/.env` (keys)
- Logs: `~/.hermes/logs/` — agent.log, errors.log, gateway.log

## Coding Standards
- Type hints on all public functions
- OpenAI-style message format: `{"role": "system/user/assistant/tool", ...}`
