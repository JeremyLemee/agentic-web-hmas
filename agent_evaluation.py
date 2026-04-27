#!/usr/bin/env python3
"""Run repeated executor-agent evaluations across SEM situations."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import signal
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DEFAULT_RUNS_PER_SITUATION = 15
DEFAULT_APP_CONFIGS = ["config_app.json", "config_app2.json"]
DEFAULT_SEM_LLM_SPECS = [{"provider": "openai", "model": "gpt-4.1-mini"}]
#DEFAULT_SEM_LLM_SPECS = [{"provider": "ollama", "model": "ministral-3:14b"}]
DEFAULT_GOALS = ["Move the robot by 10 centimeters"]
DEFAULT_RESULTS_PATH = ROOT / "agent_results.txt"
DEFAULT_LAST_RUN_PATH = ROOT / "last_run_executor_agent.txt"
DEFAULT_AGENT_CMD = "uv run llm_agent/executor_agent.py"
DEFAULT_APP_READY_TIMEOUT_S = 120

STACK_COMMANDS = [
    ("cherrybot_simu", 5, ["uv", "run", "wot_sem/cherrybot_simulation.py"]),
    ("formalizer_coala", 2, ["uv", "run", "a2a_sem/formalizer/formalizer_coala.py"]),
    ("goal_mcp", 2, ["uv", "run", "mcp_sem/goal_mcp.py"]),
    ("cherrybot_proxy", 10, ["uv", "run", "wot_sem/cherrybot_proxy.py"]),
    ("app", 2, ["uv", "run", "app.py"]),
    ("sem_mcp", 0, ["uv", "run", "mcp_sem/sem_mcp.py"]),
]

@dataclass
class SemLlmConfig:
    provider: str
    model: str

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass
class Situation:
    app_config: str
    sem_llm: SemLlmConfig
    goal: str

    @property
    def label(self) -> str:
        return f"app_config={self.app_config}, sem_llm={self.sem_llm.label}, goal={self.goal}"


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def write_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def parse_sem_llm(value: str | dict[str, str]) -> SemLlmConfig:
    if isinstance(value, dict):
        provider = value.get("provider", "").strip()
        model = value.get("model", "").strip()
        if not provider or not model:
            raise argparse.ArgumentTypeError(
                f"Invalid SEM LLM entry {value!r}. Expected object with non-empty "
                "'provider' and 'model' fields."
            )
        return SemLlmConfig(provider=provider, model=model)

    parts = value.split(":", 1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise argparse.ArgumentTypeError(
            f"Invalid --sem-llms entry '{value}'. Expected format provider:model."
        )
    return SemLlmConfig(provider=parts[0].strip(), model=parts[1].strip())


DEFAULT_SEM_LLMS = [parse_sem_llm(spec) for spec in DEFAULT_SEM_LLM_SPECS]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run multiple agent evaluations. A situation is defined by an app config, "
            "a SEM LLM (provider:model), and a goal, then executor logs are appended "
            "to agent_results.txt."
        )
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=DEFAULT_RUNS_PER_SITUATION,
        help=f"Number of runs per situation (default: {DEFAULT_RUNS_PER_SITUATION}).",
    )
    parser.add_argument(
        "--app-configs",
        nargs="+",
        default=DEFAULT_APP_CONFIGS,
        help=(
            "App runtime config files passed to app.py --app-config "
            f"(default: {' '.join(DEFAULT_APP_CONFIGS)})."
        ),
    )
    parser.add_argument(
        "--sem-llms",
        nargs="+",
        type=parse_sem_llm,
        default=DEFAULT_SEM_LLMS,
        help=(
            "SEM LLM list in provider:model form (cross-product with --app-configs). "
            f"Default: {json.dumps(DEFAULT_SEM_LLM_SPECS)}."
        ),
    )
    parser.add_argument(
        "--goals",
        nargs="+",
        default=DEFAULT_GOALS,
        help=(
            "Goal texts used for situations and registered to Goal MCP at run initialization "
            f"(default: {' | '.join(DEFAULT_GOALS)})."
        ),
    )
    parser.add_argument(
        "--agent-timeout",
        type=int,
        default=240,
        help="Timeout in seconds for one agent run (default: 240).",
    )
    parser.add_argument(
        "--app-ready-timeout",
        type=int,
        default=DEFAULT_APP_READY_TIMEOUT_S,
        help=f"Timeout in seconds waiting for app readiness (default: {DEFAULT_APP_READY_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--results-file",
        default=str(DEFAULT_RESULTS_PATH),
        help=f"Output results file (default: {DEFAULT_RESULTS_PATH.name}).",
    )
    parser.add_argument(
        "--last-run-file",
        default=str(DEFAULT_LAST_RUN_PATH),
        help=f"Executor log source file (default: {DEFAULT_LAST_RUN_PATH.name}).",
    )
    parser.add_argument(
        "--agent-cmd",
        default=DEFAULT_AGENT_CMD,
        help=f"Command to run one agent execution (default: '{DEFAULT_AGENT_CMD}').",
    )
    return parser.parse_args()


def resolve_path(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return ROOT / candidate


def wait_for_app_ready(timeout_s: int) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://localhost:5000/signifiers/list", timeout=2) as resp:
                if resp.status < 500:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def wait_for_goal_mcp_update(goal_text: str, attempts: int = 30, delay_s: float = 1.0) -> bool:
    payload = urllib.parse.urlencode({"goal": goal_text}).encode("utf-8")
    for _ in range(attempts):
        try:
            req = urllib.request.Request(
                "http://localhost:5002/goal",
                data=payload,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status < 400:
                    return True
        except Exception:
            pass
        time.sleep(delay_s)
    return False


def start_stack(app_config: str, goal_text: str) -> tuple[list[subprocess.Popen], str | None]:
    processes: list[subprocess.Popen] = []
    config_name = Path(app_config).name

    try:
        for name, delay_s, cmd in STACK_COMMANDS:
            run_cmd = list(cmd)
            if name == "app":
                run_cmd.extend(["--app-config", app_config])
            proc = subprocess.Popen(run_cmd, cwd=str(ROOT), start_new_session=True)
            processes.append(proc)
            if delay_s:
                time.sleep(delay_s)
            if name == "goal_mcp":
                if not wait_for_goal_mcp_update(goal_text):
                    return processes, (
                        f"Failed to update Goal MCP goal for '{config_name}' "
                        f"with goal '{goal_text}'."
                    )
        return processes, None
    except Exception as exc:
        return processes, f"Failed to start stack for '{config_name}': {exc}"


def stop_stack(processes: list[subprocess.Popen]) -> None:
    for proc in processes:
        if proc.poll() is not None:
            continue
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for proc in processes:
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            if proc.poll() is not None:
                continue
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                continue


def run_agent(command: str, timeout_s: int) -> tuple[int | None, bool, str | None]:
    cmd = shlex.split(command)
    proc = subprocess.Popen(cmd, cwd=str(ROOT))
    try:
        return proc.wait(timeout=timeout_s), False, None
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        return None, True, f"Agent timed out after {timeout_s}s."


def read_last_run_log(path: Path) -> str:
    if not path.exists():
        return "(missing last_run_executor_agent.txt)"
    content = path.read_text(encoding="utf-8").rstrip()
    return content or "(empty file)"


def append_run_result(
    results_path: Path,
    situation: Situation,
    run_index: int,
    runs: int,
    log_content: str,
    app_ready: bool,
    agent_exit_code: int | None,
    agent_timed_out: bool,
    error: str | None,
) -> None:
    lines: list[str] = [
        f"Situation: {situation.label}",
        f"Run: {run_index}/{runs}",
        f"Timestamp (UTC): {dt.datetime.now(dt.UTC).isoformat()}",
        f"App ready: {app_ready}",
        f"Agent exit code: {agent_exit_code}",
        f"Agent timed out: {agent_timed_out}",
    ]
    if error:
        lines.append(f"Error: {error}")
    lines.append("Agent log:")
    lines.append(log_content)
    lines.append("")
    lines.append("-" * 80)
    lines.append("")
    with results_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def build_situations(
    app_configs: list[str],
    sem_llms: list[SemLlmConfig],
    goals: list[str],
) -> list[Situation]:
    situations: list[Situation] = []
    for app_config in app_configs:
        for sem_llm in sem_llms:
            for goal in goals:
                situations.append(Situation(app_config=app_config, sem_llm=sem_llm, goal=goal))
    return situations


def main() -> int:
    args = parse_args()
    if args.runs < 1:
        raise SystemExit("--runs must be >= 1")
    if args.agent_timeout < 1:
        raise SystemExit("--agent-timeout must be >= 1")
    if args.app_ready_timeout < 1:
        raise SystemExit("--app-ready-timeout must be >= 1")
    if not args.goals:
        raise SystemExit("--goals must contain at least one goal")

    results_path = resolve_path(args.results_file)
    last_run_path = resolve_path(args.last_run_file)

    original_config = load_config()
    situations = build_situations(args.app_configs, args.sem_llms, args.goals)
    if not situations:
        raise SystemExit("No situations to evaluate.")

    header = [
        "# Agent Evaluation Results",
        f"Generated (UTC): {dt.datetime.now(dt.UTC).isoformat()}",
        f"Runs per situation: {args.runs}",
        f"Situations: {len(situations)}",
        "",
        "=" * 80,
        "",
    ]
    results_path.write_text("\n".join(header), encoding="utf-8")

    try:
        for situation in situations:
            config = load_config()
            config.setdefault("sem", {})
            config["sem"]["provider"] = situation.sem_llm.provider
            config["sem"]["model"] = situation.sem_llm.model
            write_config(config)

            for run_idx in range(1, args.runs + 1):
                if last_run_path.exists():
                    last_run_path.unlink()

                stack_processes: list[subprocess.Popen] = []
                app_ready = False
                agent_exit_code: int | None = None
                agent_timed_out = False
                error: str | None = None

                try:
                    stack_processes, stack_error = start_stack(situation.app_config, situation.goal)
                    if stack_error:
                        error = stack_error
                    else:
                        app_ready = wait_for_app_ready(args.app_ready_timeout)
                    if not stack_error and not app_ready:
                        error = (
                            "SEM app did not become ready at "
                            "http://localhost:5000/signifiers/list "
                            f"within {args.app_ready_timeout}s."
                        )
                    elif not stack_error:
                        agent_exit_code, agent_timed_out, agent_error = run_agent(
                            args.agent_cmd, args.agent_timeout
                        )
                        if agent_error:
                            error = agent_error
                finally:
                    stop_stack(stack_processes)
                    time.sleep(1)

                run_log = read_last_run_log(last_run_path)
                append_run_result(
                    results_path=results_path,
                    situation=situation,
                    run_index=run_idx,
                    runs=args.runs,
                    log_content=run_log,
                    app_ready=app_ready,
                    agent_exit_code=agent_exit_code,
                    agent_timed_out=agent_timed_out,
                    error=error,
                )
    finally:
        write_config(original_config)

    print(f"Wrote agent evaluation results to: {results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
