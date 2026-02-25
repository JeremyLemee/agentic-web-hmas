#!/usr/bin/env python3
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from subprocess import Popen
from typing import Callable

from rdflib import Graph, Namespace, RDF, RDFS

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
RESULTS_PATH = ROOT / "results.txt"
PARTIAL_RESULTS_PATH = ROOT / "partial_results.txt"

HMAS = Namespace("https://purl.org/hmas/")


@dataclass
class LlmConfig:
    provider: str
    model: str


@dataclass
class GoalCase:
    goal: str
    relevant_tools: list[str]


SERVER_COMMANDS = [
    ("cherrybot_simu", 5, ["uv", "run", "wot_sem/cherrybot_simulation.py"]),
    ("example_mcp", 2, ["uv", "run", "mcp_sem/example_mcp.py"]),
    ("formalizer_coala", 2, ["uv", "run", "a2a_sem/formalizer/formalizer_coala.py"]),
    ("goal_mcp", 2, ["uv", "run", "mcp_sem/goal_mcp.py"]),
    ("cherrybot_proxy", 10, ["uv", "run", "wot_sem/cherrybot_proxy.py"]),
    ("app", 2, ["uv", "run", "app.py"]),
    ("sem_mcp", 0, ["uv", "run", "mcp_sem/sem_mcp.py"]),
]

ROBOT_GOAL = "The agent wants to control a robot from a formal goal."

BASE_GOAL_CASES = [
    GoalCase(
        goal="The agent wants to read the user goal.",
        relevant_tools=["goal_mcp_CurrentGoal"],
    ),
GoalCase(
        goal="The agent wants to provide feedback to the agent",
        relevant_tools=["goal_mcp_provide_feedback"],
    ),
    GoalCase(
        goal="The agent wants to convert the natural language goal into a formal description.",
        relevant_tools=["formalizer_Formalize goal"],
    ),
    GoalCase(
        goal="The agents wants to read the goal or provide feedback on the goal",
        relevant_tools=["goal_mcp_CurrentGoal", "goal_mcp_provide_feedback"]
    )

]

LLMS_TO_EVALUATE = [
    LlmConfig(provider="ollama", model="gemma3:270m"),
    LlmConfig(provider="ollama", model="gemma3:1b"),
    LlmConfig(provider="ollama", model="ministral-3:latest"),
LlmConfig(provider="ollama", model="ministral-3:14b"),
    LlmConfig(provider="openai", model="gpt-4.1-mini"),
    LlmConfig(provider="openai", model="gpt-5-mini-2025-08-07")
]

CHERRYBOT_UTCP_INSTANCE = "cherrybot_utcp"
CHERRYBOT_UTCP_URL = "http://localhost:8086/utcp"
CHERRYBOT_TD_INSTANCE = "cherrybot_td"
CHERRYBOT_TD_URL = "http://localhost:8086/td"

ARTIFACT_REGISTRATION_URL = "http://localhost:5000/artifacts/registration"

PROFILE_ID = "evaluation"
PROFILE_URL = f"http://localhost:5000/profile/{PROFILE_ID}"
SIGNIFIERS_URL = "http://localhost:5000/signifiers"
PROFILE_CONTEXT_URL = f"http://localhost:5000/profile/{PROFILE_ID}/nl_context"

RUNS_PER_GOAL = 1
RUN_WOT_ROBOT_CONTROL_TEST = False


class ServerManager:
    def __init__(self) -> None:
        self.processes: list[Popen] = []
        self.started = False

    def start(self) -> None:
        if self.started:
            return
        for name, delay, cmd in SERVER_COMMANDS:
            print(f"Starting: {name}")
            proc = Popen(cmd, cwd=str(ROOT), start_new_session=True)
            self.processes.append(proc)
            print(f"  -> PID {proc.pid}")
            if delay:
                print(f"Sleeping {delay}s...")
                time.sleep(delay)
        self.started = True

    def stop(self) -> None:
        if not self.processes:
            return
        print("\nStopping all servers...")
        for proc in self.processes:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
        for proc in self.processes:
            try:
                proc.wait(timeout=5)
            except Exception:
                continue


def _http_request(
    url: str, method: str = "GET", data: bytes | None = None, headers: dict | None = None
):
    req = urllib.request.Request(url, data=data, method=method)
    if headers:
        for key, value in headers.items():
            req.add_header(key, value)
    # Intentionally no timeout: wait until the endpoint responds.
    return urllib.request.urlopen(req)


def update_profile_context(context: str) -> None:
    payload = json.dumps({"context": context}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    with _http_request(PROFILE_CONTEXT_URL, method="PUT", data=payload, headers=headers) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"Failed to update profile context: HTTP {resp.status}")


def fetch_selected_signifier_labels() -> set[str]:
    params = urllib.parse.urlencode({"profile": PROFILE_URL})
    url = f"{SIGNIFIERS_URL}?{params}"
    headers = {"Accept": "text/turtle"}
    with _http_request(url, headers=headers) as resp:
        data = resp.read()
    graph = Graph()
    graph.parse(data=data, format="turtle")
    labels: set[str] = set()
    for signifier in graph.subjects(RDF.type, HMAS["Signifier"]):
        label = None
        for lbl in graph.objects(signifier, RDFS["label"]):
            label = str(lbl)
            break
        labels.add(label or str(signifier))
    return labels


def compute_precision_recall(
    selected: set[str], relevant: set[str]
) -> tuple[float, float, int, int, int]:
    tp = len(selected & relevant)
    fp = len(selected - relevant)
    fn = len(relevant - selected)

    if selected:
        precision = tp / len(selected)
    else:
        precision = 1.0 if not relevant else 0.0

    if relevant:
        recall = tp / len(relevant)
    else:
        recall = 1.0 if not selected else 0.0

    return precision, recall, tp, fp, fn


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def write_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")


def wait_for_app_ready() -> None:
    last_error = None
    while True:
        try:
            with _http_request("http://localhost:5000/signifiers/list") as resp:
                if resp.status < 400:
                    return
        except Exception as exc:
            last_error = exc
        time.sleep(1)
    raise RuntimeError(f"App did not become ready: {last_error}")


def _find_existing_result(
    all_results: list[dict], llm_label: str, interface_name: str
) -> dict | None:
    for entry in all_results:
        if entry.get("llm") == llm_label and entry.get("interface") == interface_name:
            return entry
    return None


def _find_or_create_goal_result(results: dict, goal_case: GoalCase) -> dict:
    for goal_result in results.get("goals", []):
        if goal_result.get("goal") == goal_case.goal:
            goal_result["relevant_tools"] = list(goal_case.relevant_tools)
            goal_result.setdefault("runs", [])
            return goal_result

    goal_result = {
        "goal": goal_case.goal,
        "relevant_tools": list(goal_case.relevant_tools),
        "runs": [],
    }
    results.setdefault("goals", []).append(goal_result)
    return goal_result


def evaluate_llm(
    llm: LlmConfig,
    cherrybot_tool: str,
    interface_name: str,
    all_results: list[dict],
    on_run_persist: Callable[[], None] | None = None,
) -> dict:
    config = load_config()
    config.setdefault("sem", {})
    config["sem"]["provider"] = llm.provider
    config["sem"]["model"] = llm.model
    write_config(config)

    llm_label = f"{llm.provider}:{llm.model}"
    results = _find_existing_result(all_results, llm_label, interface_name)
    if results is None:
        results = {
            "llm": llm_label,
            "interface": interface_name,
            "goals": [],
        }
        all_results.append(results)

    goal_cases = list(BASE_GOAL_CASES)
    goal_cases.append(
        GoalCase(
            goal=ROBOT_GOAL,
            relevant_tools=[cherrybot_tool],
        )
    )

    for goal_case in goal_cases:
        goal_result = _find_or_create_goal_result(results, goal_case)
        relevant_set = set(goal_case.relevant_tools)
        goal_result["runs"] = list(goal_result.get("runs", []))[:RUNS_PER_GOAL]

        completed_runs = len(goal_result["runs"])
        for _ in range(completed_runs, RUNS_PER_GOAL):
            update_profile_context(goal_case.goal)
            selected = fetch_selected_signifier_labels()
            precision, recall, tp, fp, fn = compute_precision_recall(selected, relevant_set)
            goal_result["runs"].append(
                {
                    "selected": sorted(selected),
                    "precision": precision,
                    "recall": recall,
                    "tp": tp,
                    "fp": fp,
                    "fn": fn,
                }
            )
            if on_run_persist is not None:
                on_run_persist()

    return results


def summarize_results(results: dict) -> None:
    print("\nEvaluation results:")
    print(f"LLM: {results['llm']}")
    if results.get("interface"):
        print(f"Interface: {results['interface']}")
    for goal in results["goals"]:
        precisions = [r["precision"] for r in goal["runs"]]
        recalls = [r["recall"] for r in goal["runs"]]
        avg_precision = sum(precisions) / len(precisions)
        avg_recall = sum(recalls) / len(recalls)
        print("-")
        print(f"Goal: {goal['goal']}")
        print(f"Relevant tools: {goal['relevant_tools']}")
        print(f"Avg precision: {avg_precision:.3f}")
        print(f"Avg recall: {avg_recall:.3f}")


def _load_existing_results(path: Path) -> dict | None:
    if not path.exists():
        return None
    text = path.read_text()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        # Support files where a human-readable table is appended after JSON.
        decoder = json.JSONDecoder()
        try:
            parsed, _end = decoder.raw_decode(text.lstrip())
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None


def _format_cell_stats(goal_entry: dict) -> str:
    runs = goal_entry.get("runs", [])
    if not runs:
        return "-"
    n = len(runs)
    avg_precision = sum(float(run.get("precision", 0.0)) for run in runs) / n
    avg_recall = sum(float(run.get("recall", 0.0)) for run in runs) / n
    return f"P:{_format_ratio(avg_precision)}, R:{_format_ratio(avg_recall)}"


def _format_ratio(value: float) -> str:
    rounded = round(value, 2)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.2f}"


def _canonical_goal_name(goal_name: str) -> str:
    normalized = " ".join(goal_name.strip().lower().split())
    if normalized == "the agent wants to read the user goal.":
        return "Goal Signifier"
    if normalized in {
        "the agent wants to provide feedback to the user.",
        "the agent wants to provide feedback to the agent",
    }:
        return "Goal Feedback Signifier"
    if normalized == "the agent wants to convert the natural language goal into a formal description.":
        return "Formalizer"
    if normalized == "the agents wants to read the goal or provide feedback on the goal":
        return "Goal + Feedback"
    if "control a robot from a formal goal" in normalized:
        return ROBOT_GOAL
    return goal_name.strip()


def _goal_row_label(goal_name: str, interface_name: str) -> str:
    canonical_goal = _canonical_goal_name(goal_name)
    if canonical_goal == ROBOT_GOAL:
        if interface_name.strip().lower() in {"td", "wot"}:
            return "Robot Control (WoT)"
        return "Robot Control"
    return canonical_goal


def _configured_llm_labels() -> list[str]:
    return [f"{llm.provider}:{llm.model}" for llm in LLMS_TO_EVALUATE]


def _expected_goal_rows(include_wot_robot_control: bool) -> list[str]:
    rows: list[str] = []
    for goal_case in BASE_GOAL_CASES:
        row_label = _goal_row_label(goal_case.goal, "utcp")
        if row_label not in rows:
            rows.append(row_label)
    rows.append("Robot Control")
    if include_wot_robot_control:
        rows.append("Robot Control (WoT)")
    return rows


def _display_model_label(llm_label: str) -> str:
    model = llm_label
    if ":" in llm_label:
        model = llm_label.split(":", 1)[1]
    if model.endswith(":latest"):
        model = model[: -len(":latest")]
    if model.startswith("gpt-5-mini"):
        return "gpt-5-mini"
    return model


def _is_wot_entry(result_entry: dict) -> bool:
    return str(result_entry.get("interface", "")).strip().lower() in {"td", "wot"}


def _filtered_results_entries(
    results_entries: list[dict], include_wot_robot_control: bool
) -> list[dict]:
    if include_wot_robot_control:
        return list(results_entries)
    return [entry for entry in results_entries if not _is_wot_entry(entry)]


def _latex_escape(value: str) -> str:
    replacements = {
        "&": r"\&",
        "%": r"\%",
        "_": r"\_",
        "#": r"\#",
    }
    out = value
    for old, new in replacements.items():
        out = out.replace(old, new)
    return out


def build_results_table(
    results_entries: list[dict], include_wot_robot_control: bool = False
) -> str:
    results_entries = _filtered_results_entries(results_entries, include_wot_robot_control)
    if not results_entries:
        return "% No results available."

    present_model_labels: list[str] = []
    for entry in results_entries:
        label = str(entry.get("llm", "unknown"))
        if label not in present_model_labels:
            present_model_labels.append(label)

    configured_model_labels = _configured_llm_labels()
    model_labels = list(configured_model_labels)
    model_labels.extend([label for label in present_model_labels if label not in model_labels])

    goals = _expected_goal_rows(include_wot_robot_control)
    present_goal_rows: list[str] = []
    for entry in results_entries:
        interface_name = str(entry.get("interface", ""))
        for goal in entry.get("goals", []):
            goal_name = str(goal.get("goal", "unknown"))
            row_label = _goal_row_label(goal_name, interface_name)
            if not include_wot_robot_control and row_label == "Robot Control (WoT)":
                continue
            if row_label not in present_goal_rows:
                present_goal_rows.append(row_label)
    goals.extend([row for row in present_goal_rows if row not in goals])

    stats_runs_by_model_goal: dict[tuple[str, str], list[dict]] = {}
    for entry in results_entries:
        model = str(entry.get("llm", "unknown"))
        interface_name = str(entry.get("interface", ""))
        for goal in entry.get("goals", []):
            goal_name = str(goal.get("goal", "unknown"))
            row_label = _goal_row_label(goal_name, interface_name)
            if not include_wot_robot_control and row_label == "Robot Control (WoT)":
                continue
            key = (model, row_label)
            stats_runs_by_model_goal.setdefault(key, [])
            stats_runs_by_model_goal[key].extend(goal.get("runs", []))

    col_spec = "l" + ("c" * len(model_labels))
    rows = [
        r"\begin{table}[htbp]",
        r"\centering",
        f"\\begin{{tabular}}{{{col_spec}}}",
        "  Task/Model & "
        + " & ".join(_latex_escape(_display_model_label(label)) for label in model_labels)
        + r"\\",
    ]
    for goal_name in goals:
        cells: list[str] = []
        for model in model_labels:
            runs = stats_runs_by_model_goal.get((model, goal_name), [])
            cells.append(_format_cell_stats({"runs": runs}) if runs else "-")
        rows.append(
            "  "
            + _latex_escape(goal_name)
            + " & "
            + " & ".join(_latex_escape(cell) for cell in cells)
            + r"\\"
        )
    rows.extend(
        [
            r"\end{tabular}",
            r"\caption{Results}",
            r"\label{table:results}",
            r"\end{table}",
        ]
    )
    return "\n".join(rows)


def write_results(path: Path, payload: dict, include_wot_robot_control: bool = False) -> None:
    results_entries = payload.get("results", [])
    filtered_entries = (
        _filtered_results_entries(results_entries, include_wot_robot_control)
        if isinstance(results_entries, list)
        else []
    )
    table = build_results_table(
        filtered_entries,
        include_wot_robot_control=include_wot_robot_control,
    )
    filtered_payload = {"results": filtered_entries}
    output = (
        json.dumps(filtered_payload, indent=2, sort_keys=True)
        + "\n\n# Task x Model Statistics (LaTeX)\n\n"
        + table
        + "\n"
    )
    path.write_text(output)


def _post_artifact_registration(payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    with _http_request(
        ARTIFACT_REGISTRATION_URL, method="POST", data=data, headers=headers
    ) as resp:
        if resp.status >= 400:
            body = resp.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Artifact registration failed: HTTP {resp.status} {body}")


def switch_to_td_interface() -> None:
    _post_artifact_registration(
        {
            "action": "delete",
            "instance_name": CHERRYBOT_UTCP_INSTANCE,
        }
    )
    _post_artifact_registration(
        {
            "action": "register",
            "kind": "wot",
            "instance_name": CHERRYBOT_TD_INSTANCE,
            "url": CHERRYBOT_TD_URL,
        }
    )


def main() -> int:
    server_manager = ServerManager()

    def _cleanup(*_args):
        server_manager.stop()
        sys.exit(1)

    signal.signal(signal.SIGINT, _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    original_config = load_config()
    partial_payload = _load_existing_results(PARTIAL_RESULTS_PATH)
    if isinstance(partial_payload, dict) and isinstance(partial_payload.get("results"), list):
        all_results = _filtered_results_entries(
            partial_payload["results"], RUN_WOT_ROBOT_CONTROL_TEST
        )
    else:
        all_results = []

    def persist_partial_results() -> None:
        write_results(
            PARTIAL_RESULTS_PATH,
            {"results": all_results},
            include_wot_robot_control=RUN_WOT_ROBOT_CONTROL_TEST,
        )

    try:
        server_manager.start()
        wait_for_app_ready()

        for llm in LLMS_TO_EVALUATE:
            results = evaluate_llm(
                llm,
                f"{CHERRYBOT_UTCP_INSTANCE}_operation",
                "utcp",
                all_results,
                persist_partial_results,
            )
            summarize_results(results)

        if RUN_WOT_ROBOT_CONTROL_TEST:
            switch_to_td_interface()
            time.sleep(2)
            for llm in LLMS_TO_EVALUATE:
                results = evaluate_llm(
                    llm,
                    f"{CHERRYBOT_TD_INSTANCE}_operation",
                    "wot",
                    all_results,
                    persist_partial_results,
                )
                summarize_results(results)
        write_results(
            RESULTS_PATH,
            {"results": all_results},
            include_wot_robot_control=RUN_WOT_ROBOT_CONTROL_TEST,
        )
        if PARTIAL_RESULTS_PATH.exists():
            PARTIAL_RESULTS_PATH.unlink()
    except Exception as exc:
        print(f"Evaluation failed: {exc}")
        return 1
    finally:
        write_config(original_config)
        server_manager.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
