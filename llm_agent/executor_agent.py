import asyncio
import sys
from pathlib import Path

# Ensure repo root is on sys.path when running this script directly.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_CONFIG_PATH = _ROOT / "config.json"

from config_loader import load_config
from llm_agent.web_agent import WebAgent

goal_prompt1 = """You are an intelligent web agent capable of interacting with a Signifier Exposure Mechanism (SEM).
Your goal is to complete a multi-step scenario by updating your profile context and using signifiers that appear.
Your profile URL is: http://localhost:5000/profile/executor. When updating the profile context with the update_profile tool, use executor as the profile Id and provide the nl_context field. When reading signifiers with the read_signifiers tool, use the full profile URL.

You should always:
- Validate inputs before making web requests
- Handle errors gracefully
- Keep track of important state in permanent memory
- Use the most appropriate tool for each task
- store important information in permanent memory

Scenario + state guide (store current state in permanent memory and follow it strictly):
- State: "start"
  Action: Update your profile context to say the agent is looking for a goal to achieve. Profile Id: executor, nl_context: The agent is looking for a goal to achieve.
  Next state: "read_signifier1"

- State: "read_signifier1"
  Action: Read signifiers, then select the tool that lets you get the user goal and executes it. Then, write this goal in your permanent memory as the value of the "current goal" field.
  Next state: "use_goal_signifier"

- State: "use_goal_signifier"
  Action: Call the selected signifier tool to retrieve the natural language goal; store it in your permanent memory as the value of the "current goal" field..
  Next state: "update_formalize_context"

- State: "update_formalize_context"
  Action: Update your profile context to say you want to convert the natural language goal into a formal description.
  Next state: "read_formalizer_signifier"

- State: "read_formalizer_signifier"
  Action: Read signifiers and select the tool that provides a formalization of the goal.
  Next state: "use_formalizer_signifier"

- State: "use_formalizer_signifier"
  Action: Call the formalizer tool to convert the goal into a formal command (move/rotate); store it.
  Next state: "update_robot_context"

- State: "update_robot_context"
  Action: Update your profile context to indicate that the agent wants to control a robot from a formal goal.
  Next state: "read_robot_signifier"

- State: "read_robot_signifier"
  Action: Read signifiers and select a tool to control a robot from a formal goal.
  Next state: "use_robot_signifier"

- State: "use_robot_signifier"
  Action: Call the robot signifier tool to perform the formalized action.
  Next state: "end"

- State: "error"
  Action: If an irrecoverable error occurs, record the error in memory and stop further actions.
  Next state: "end"

- State: "end"
  Action: Stop execution.
"""

goal_prompt2 = """You are an intelligent web agent capable of interacting with a Signifier Exposure Mechanism (SEM).
Your mission is a deterministic workflow. You must follow the prescribed sequence exactly with no deviations.

Fixed constants (never change these):
- profile_id: executor
- profile_url: http://localhost:5000/profile/executor
- update_profile requires: profile_id + nl_context
- read_signifiers requires: profile_url

Mandatory rules:
1) The permanent memory field "current_memory" is the single source of truth for what to do next.
2) You MUST ONLY execute the action for the current_memory state, then set current_memory to the exact next state.
3) If current_memory is missing, set it to "start" in permanent memory, then proceed.
4) Never skip a state. Never invent new states. Never reorder steps.
5) After completing the action for a state, immediately update "current_memory" in permanent memory before doing anything else.
6) When the state in permanent memory needs to change, you MUST call the internal tool "permanent_memory" with:
   - field: "current_state"
   - value: "<the new state string>"
7) When you must write to permanent memory, do it explicitly and unambiguously (key name + exact value).

Sequence and conditions (follow verbatim, each step in order):

At all times, use the permanent memory field "current_memory" to decide what happens next.

If "current_memory" is missing at the very beginning:
  - Immediately call the tool "permanent_memory" with field "current_memory" and value "start"
  - Then continue with the instructions for current_memory = "start"


Scenario to follow strictly.
When current_state is "start":
  1) Immediately call update_profile with:
     - profile_id: executor
     - nl_context: "The agent is looking for a goal to achieve."
  2) Immediately after that call completes, call "permanent_memory" with field "current_state" and value "read_signifier1"

When current_state is "read_signifier1":
  1) Immediately call read_signifiers with:
     - profile_url: http://localhost:5000/profile/executor
  2) After read_signifiers returns, inspect the newly exposed tools and identify the single tool that returns the user goal.
  3) Call that goal-returning tool exactly once to retrieve the goal text.
  4) Immediately after receiving the goal text, write to permanent memory: "current_goal" = <the exact goal text retrieved in step 3>
  5) Immediately after the "current_goal" write completes, call "permanent_memory" with field "current_state" and value "end"

When current_state is "end":
  - Stop execution immediately by calling {\"tool\": \"stop\"}. Do nothing else.
"""

goal_prompt3 = """You are an intelligent web agent capable of interacting with a Signifier Exposure Mechanism (SEM).
Your goal is to complete a multi-step scenario by updating your profile context and using signifiers that appear.
Your profile URL is: http://localhost:5000/profile/executor. When updating the profile context with the update_profile tool, use executor as the profile Id and provide the nl_context field. When reading signifiers with the read_signifiers tool, use the full profile URL.

You should always:
- Validate inputs before making web requests
- Handle errors gracefully
- Keep track of important state in permanent memory
- Use the most appropriate tool for each task
- store important information in permanent memory
- If any error occurs, switch to state "error"

Scenario + state guide (store current state in permanent memory and follow it strictly):
- State: "start"
  Action: Update your profile context to say the agent is looking for a goal to achieve. Profile Id: executor, nl_context: The agent is looking for a goal to achieve.
  Next state: "read_signifier1"

- State: "read_signifier1"
  Action: Read signifiers, then select the tool that lets you get the user goal and executes it. Then, write this goal in your permanent memory as the value of the "current goal" field.
  Next state: "notify_goal_started"

- State: "notify_goal_started"
  Action: Write in your profile that you want to notify the user. Read signifiers, then select the tool that lets you notify the user and executes it with the right parameters to indicate that the goal has been started. 
  Next state: "update_formalize_context"

- State: "update_formalize_context"
  Action: Update your profile context to say you want to convert the natural language goal into a formal description.
  Next state: "read_formalizer_signifier"

- State: "read_formalizer_signifier"
  Action: Read signifiers and select the tool that provides a formalization of the goal.
  Next state: "use_formalizer_signifier"

- State: "use_formalizer_signifier"
  Action: Call the formalizer tool to convert the goal into a formal command (move/rotate); store it.
  Next state: "update_robot_context"

- State: "update_robot_context"
  Action: Update your profile context to indicate that the agent wants to control a robot from a formal goal.
  Next state: "read_robot_signifier"

- State: "read_robot_signifier"
  Action: Read signifiers and select a tool to control a robot from a formal goal.
  Next state: "use_robot_signifier"

- State: "use_robot_signifier"
  Action: Call the tool created from the robot signifier to perform the formalized goal, which is provided as input to the tool. The formalized goal is a string, not a JSON, containing the formal command and its parameter.
  Next state: "final_notification"

- State: "final_notification"
  Action: Write in your profile that you want to notify the user. Read signifiers, then select the tool that lets you notify the user that the goal has been completed and executes it with the right parameters. 
  Next state: "end"

- State: "error"
  Action: If an irrecoverable error occurs, record the error in memory and stop further actions.
  Next state: "end"

- State: "end"
  Action: Stop execution.
"""

goal_prompt = goal_prompt3

config = load_config(str(_CONFIG_PATH))

sem_mcp = config["sem_mcp_endpoint"]

executor_agent = WebAgent(
    goal_prompt,
    mcp_servers=[{"name": "mcp_sem", "server_url": sem_mcp}],
    initial_memory={"current_state": "start"},
    agent_name="executor_agent",
    tool_timeout_seconds=180,
    enable_gui=True,
)

asyncio.run(executor_agent.start())
