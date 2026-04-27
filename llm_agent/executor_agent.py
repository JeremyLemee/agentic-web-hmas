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

goal_prompt = """You are an intelligent web agent capable of interacting with a Signifier Exposure Mechanism (SEM).
Your goal is to complete a multi-step scenario by updating your profile context and using signifiers that appear.
Your profile URL is: http://localhost:5000/profile/executor. Your first action must be to call the register_profile tool with profile_id executor. When updating the profile context with the update_profile tool, use executor as the profile Id and provide the nl_context field. When reading signifiers with the read_signifiers tool, use the full profile URL.

You should always:
- Validate inputs before making web requests
- Handle errors gracefully
- Keep track of important state in permanent memory
- Use the most appropriate tool for each task
- store important information in permanent memory
- If any error occurs, switch to state "error"

Scenario + state guide (store current state in permanent memory and follow it strictly):
- State: "start"
  Action: Register your profile by calling register_profile with profile_id executor.
  Next state: "initialize_goal_context"

- State: "initialize_goal_context"
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
