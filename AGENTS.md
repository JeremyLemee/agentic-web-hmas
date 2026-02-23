# Repository Guidelines

## Project Structure & Module Organization
This repository is a Python 3.12 project for SEM experiments across multiple protocols.

- Root scripts: `app.py`, `evaluation.py`, `signifier.py`, `utils.py`, `config_loader.py`
- Protocol modules:
  - `a2a_sem/` for A2A agents and schemas
  - `mcp_sem/` for MCP servers/clients and interaction scripts
  - `utcp_sem/` for UTCP client examples
  - `wot_sem/` for WoT affordances, simulation, and proxy
  - `llm_agent/` for executor and CoALA tooling
- Runtime/config files: `config.json`, `API_KEY.txt`, `results.txt`
- Dependency lock and metadata: `pyproject.toml`, `uv.lock`

## Build, Test, and Development Commands
Use `uv` for environment and execution.

- `uv sync` installs project and dev dependencies from `pyproject.toml`/`uv.lock`.
- `uv run app.py` runs the main Flask app locally.
- `bash run.sh` starts the full multi-service stack in the expected order.
- `uv run evaluation.py` runs evaluation workflows.
- `uv run ruff check .` runs lint checks.
- `uv run pyright` runs static type checks.
- `uv run pytest` runs the full test suite.
- `uv run pytest tests/test_artifacts_list.py -q` runs the artifact list integration test.

## Coding Style & Naming Conventions
- Follow PEP 8 with 4-space indentation.
- Keep line length at 100 (configured in Ruff).
- Use `snake_case` for functions/files, `PascalCase` for classes, and descriptive module names (for example `goal_mcp.py`, `cherrybot_proxy.py`).
- Prefer small, focused modules grouped by protocol folder.

## Testing Guidelines
Automated tests are available under `tests/` and should be run alongside lint/type checks.

- Run `uv run ruff check .`, `uv run pyright`, and `uv run pytest` before opening a PR.
- Validate changed behavior with the closest script (for example `uv run mcp_sem/mcp_interact.py`).
- Prefer targeted test runs while iterating (for example `uv run pytest tests/test_artifacts_list.py -q`), then run the full suite.
- Place tests under `tests/` and name files `test_<feature>.py`.

## Commit & Pull Request Guidelines
Recent history uses short imperative messages (for example `Update evaluation`, `Add evaluation`).

- Commit format: `<Verb> <scope>` with a focused change per commit.
- PRs should include:
  - Clear summary of behavior changes
  - Linked issue/task (if available)
  - Reproduction/verification commands run
  - Logs or screenshots for UI/API behavior when relevant

## Security & Configuration Tips
- Do not commit secrets; keep API keys out of Git (`API_KEY.txt` is local-only).
- Review `config.json` values before running shared demos.
