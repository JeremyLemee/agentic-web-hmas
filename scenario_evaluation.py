#!/usr/bin/env python3
"""Run scenario evaluations for UTCP and WoT TD and collect agent run logs."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import signal
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LAST_RUN_FILE = ROOT / "last_run_executor_agent.txt"
DEFAULT_RESULTS_FILE = ROOT / "scenario_results.md"

STACKS = [
    ("UTCP", ROOT / "run.sh"),
    ("WoT TD", ROOT / "wot_run.sh"),
]


@dataclass
class RunResult:
    interface: str
    task: str
    run_index: int
    timestamp_utc: str
    agent_exit_code: int | None
    agent_timed_out: bool
    stack_start_ok: bool
    goal_set_ok: bool
    log_content: str
    error: str | None


def _http_open(url: str, method: str = "GET", data: bytes | None = None, timeout: float = 5.0):
    req = urllib.request.Request(url=url, method=method, data=data)
    return urllib.request.urlopen(req, timeout=timeout)


def wait_for_stack(timeout_s: int = 90) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with _http_open("http://localhost:5000/signifiers/list", timeout=2.0) as resp:
                if resp.status < 500:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def set_goal(goal: str, attempts: int = 30, delay_s: float = 1.0) -> bool:
    payload = urllib.parse.urlencode({"goal": goal}).encode("utf-8")
    for _ in range(attempts):
        try:
            with _http_open(
                "http://localhost:5001/goal",
                method="POST",
                data=payload,
                timeout=2.0,
            ) as resp:
                if resp.status < 400:
                    return True
        except Exception:
            pass
        time.sleep(delay_s)
    return False


def start_stack(script_path: Path) -> subprocess.Popen:
    return subprocess.Popen(
        ["bash", str(script_path)],
        cwd=str(ROOT),
        start_new_session=True,
    )


def stop_stack(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=3)


def run_agent(timeout_s: int) -> tuple[int | None, bool, str | None]:
    cmd = ["uv", "run", "llm_agent/executor_agent.py"]
    proc = subprocess.Popen(cmd, cwd=str(ROOT))
    try:
        code = proc.wait(timeout=timeout_s)
        return code, False, None
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        return None, True, f"Agent timed out after {timeout_s}s."


def read_last_run_file() -> str:
    if not LAST_RUN_FILE.exists():
        return "(missing last_run_executor_agent.txt)"
    return LAST_RUN_FILE.read_text(encoding="utf-8").rstrip() or "(empty file)"


def evaluate(tasks: list[str], runs: int, agent_timeout_s: int) -> list[RunResult]:
    results: list[RunResult] = []

    for interface_name, stack_script in STACKS:
        stack_proc = start_stack(stack_script)
        stack_ok = wait_for_stack(timeout_s=120)

        for task in tasks:
            for run_idx in range(1, runs + 1):
                timestamp = dt.datetime.now(dt.UTC).isoformat()

                if LAST_RUN_FILE.exists():
                    LAST_RUN_FILE.unlink()

                if not stack_ok:
                    results.append(
                        RunResult(
                            interface=interface_name,
                            task=task,
                            run_index=run_idx,
                            timestamp_utc=timestamp,
                            agent_exit_code=None,
                            agent_timed_out=False,
                            stack_start_ok=False,
                            goal_set_ok=False,
                            log_content="(run skipped: stack did not become ready)",
                            error=f"Stack failed to become ready: {stack_script}",
                        )
                    )
                    continue

                goal_ok = set_goal(task)
                if not goal_ok:
                    results.append(
                        RunResult(
                            interface=interface_name,
                            task=task,
                            run_index=run_idx,
                            timestamp_utc=timestamp,
                            agent_exit_code=None,
                            agent_timed_out=False,
                            stack_start_ok=True,
                            goal_set_ok=False,
                            log_content="(run skipped: failed to update goal)",
                            error=f"Goal MCP update failed for task: {task}",
                        )
                    )
                    continue

                agent_code, timed_out, run_error = run_agent(timeout_s=agent_timeout_s)
                run_log = read_last_run_file()
                results.append(
                    RunResult(
                        interface=interface_name,
                        task=task,
                        run_index=run_idx,
                        timestamp_utc=timestamp,
                        agent_exit_code=agent_code,
                        agent_timed_out=timed_out,
                        stack_start_ok=True,
                        goal_set_ok=True,
                        log_content=run_log,
                        error=run_error,
                    )
                )

        stop_stack(stack_proc)
        time.sleep(1)

    return results


def write_markdown_results(output_path: Path, tasks: list[str], runs: int, results: list[RunResult]) -> None:
    now = dt.datetime.now(dt.UTC).isoformat()
    lines: list[str] = []
    lines.append("# Scenario Evaluation Results")
    lines.append("")
    lines.append(f"- Generated (UTC): `{now}`")
    lines.append(f"- Runs per task/interface: `{runs}`")
    lines.append(f"- Tasks: `{tasks}`")
    lines.append("- Agent: `llm_agent/executor_agent.py`")
    lines.append("")

    for interface_name, _stack_script in STACKS:
        lines.append(f"## Interface: {interface_name}")
        lines.append("")
        for task in tasks:
            lines.append(f"### Task: `{task}`")
            lines.append("")
            task_runs = [
                r
                for r in results
                if r.interface == interface_name and r.task == task
            ]
            if not task_runs:
                lines.append("_No runs recorded._")
                lines.append("")
                continue

            for r in task_runs:
                lines.append(f"#### Run {r.run_index}")
                lines.append("")
                lines.append(f"- Timestamp (UTC): `{r.timestamp_utc}`")
                lines.append(f"- Stack ready: `{r.stack_start_ok}`")
                lines.append(f"- Goal updated: `{r.goal_set_ok}`")
                lines.append(f"- Agent exit code: `{r.agent_exit_code}`")
                lines.append(f"- Agent timed out: `{r.agent_timed_out}`")
                if r.error:
                    lines.append(f"- Error: `{r.error}`")
                lines.append("")
                lines.append("```text")
                lines.append(r.log_content)
                lines.append("```")
                lines.append("")

    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run scenario evaluation for UTCP (run.sh) and WoT TD (wot_run.sh) "
            "and aggregate executor logs into scenario_results.md."
        )
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Number of runs per task for each interface (default: 1).",
    )
    parser.add_argument(
        "--tasks",
        nargs="+",
        default=["move(10)", "rotate(12)"],
        help='Task list (default: "move(10)" "rotate(12)").',
    )
    parser.add_argument(
        "--agent-timeout",
        type=int,
        default=240,
        help="Timeout in seconds for llm_agent/executor_agent.py (default: 240).",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_RESULTS_FILE),
        help=f"Output markdown file (default: {DEFAULT_RESULTS_FILE.name}).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.runs < 1:
        raise SystemExit("--runs must be >= 1")

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = ROOT / output_path

    results = evaluate(tasks=args.tasks, runs=args.runs, agent_timeout_s=args.agent_timeout)
    write_markdown_results(output_path, tasks=args.tasks, runs=args.runs, results=results)
    print(f"Wrote scenario results to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
