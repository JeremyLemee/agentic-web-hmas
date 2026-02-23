import json
import sys
from pathlib import Path
from typing import Annotated

from pydantic import Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os
import threading
from flask import Flask, Response, redirect, request

from mcp.server.fastmcp import FastMCP


# ----------------------------
# Shared goal state (thread-safe)
# ----------------------------
_goal_lock = threading.Lock()
_goal_value = ""
_feedback_value = ""
_goal_achieved = False


def _get_goal() -> str:
    with _goal_lock:
        return _goal_value


def _set_goal(value: str) -> None:
    global _goal_value
    with _goal_lock:
        _goal_value = value


def _get_feedback() -> tuple[bool, str]:
    with _goal_lock:
        return _goal_achieved, _feedback_value


def _set_feedback(achieved: bool, feedback: str) -> None:
    global _goal_achieved, _feedback_value, _goal_value
    with _goal_lock:
        _goal_achieved = achieved
        _feedback_value = feedback
        if achieved:
            _goal_value = ""


# ----------------------------
# Browser GUI (same behavior)
# ----------------------------
gui_app = Flask(__name__)


@gui_app.get("/")
def goal_form() -> Response:
    goal = _get_goal()
    achieved, feedback = _get_feedback()
    feedback_json = json.dumps(feedback or "")
    html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Goal Setter</title>
    <script>
      window.addEventListener("DOMContentLoaded", () => {{
        const achieved = {str(achieved).lower()};
        const feedback = {feedback_json};
        if (achieved || feedback) {{
          const statusLine = `Goal achieved: ${{achieved ? "yes" : "no"}}`;
          const feedbackLine = `Feedback: ${{feedback || "(none)"}}`;
          alert(`${{statusLine}}\\n${{feedbackLine}}`);
        }}
      }});
    </script>
  </head>
  <body>
    <h1>Set Current Goal</h1>
    <p><strong>Current goal:</strong> {goal or "(empty)"}</p>
    <form action="/goal" method="post">
      <label for="goal">Goal</label>
      <input id="goal" name="goal" type="text" value="{goal}" />
      <button type="submit">Update</button>
    </form>
  </body>
</html>
"""
    return Response(html, mimetype="text/html")


@gui_app.post("/goal")
def update_goal():
    goal = request.form.get("goal", "").strip()
    _set_goal(goal)
    return redirect("/")


@gui_app.get("/goal")
def show_goal():
    return Response(_get_goal(), mimetype="text/plain")


def _run_gui(host: str, port: int) -> None:
    gui_app.run(host=host, port=port, debug=False, use_reloader=False)


# ----------------------------
# MCP server (resource instead of agent)
# ----------------------------
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "9996"))

mcp = FastMCP(name="Goal MCP", host=MCP_HOST, port=MCP_PORT)


@mcp.resource(
    uri="goal://current",
    name="CurrentGoal",
    description="Returns the current goal defined by the user.",
    mime_type="text/plain",
)
def current_goal() -> str:
    goal = _get_goal()
    return goal or "Goal is empty."


@mcp.tool()
def provide_feedback(achieved: Annotated[bool, Field(description="Whether the goal has been achieved")], feedback: Annotated[str, Field(description="The feedback to provide")]) -> dict:
    """Provide feedback for the current goal."""
    _set_feedback(bool(achieved), (feedback or "").strip())
    return {
        "ok": True,
        "achieved": _goal_achieved,
        "feedback": _feedback_value,
    }


if __name__ == "__main__":
    # Run the GUI on :5001, MCP on MCP_PORT, in the same process.
    gui_thread = threading.Thread(target=_run_gui, args=("0.0.0.0", 5002), daemon=True)
    gui_thread.start()

    # Streamable HTTP MCP server (no uvicorn needed)
    mcp.run(transport="streamable-http")
