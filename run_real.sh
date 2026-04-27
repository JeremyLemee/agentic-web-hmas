#!/usr/bin/env bash
set -euo pipefail

PIDS=()

cleanup() {
  echo
  echo "Stopping all servers..."
  trap - SIGTERM
  kill -- -$$ 2>/dev/null || true
}
trap cleanup SIGINT SIGTERM EXIT

start_bg() {
  local name="$1"
  local sleep_s="$2"
  shift 2

  echo "Starting: $name"
  uv run "$@" &
  local pid=$!
  PIDS+=("$pid")
  echo "  -> PID $pid"
  echo "Sleeping ${sleep_s}s..."
  sleep "$sleep_s"
}

set_goal_mcp_goal() {
  local goal_text="$1"
  local attempts=30
  local delay_s=1

  echo "Updating Goal MCP goal..."
  sleep 1
  for ((i=1; i<=attempts; i++)); do
    if curl -fsS -X POST "http://localhost:5002/goal" \
      --data-urlencode "goal=${goal_text}" >/dev/null; then
      echo "  -> Goal MCP goal updated"
      return 0
    fi
    sleep "$delay_s"
  done

  echo "Failed to update Goal MCP goal after ${attempts} attempts" >&2
  return 1
}

start_bg "formalizer_coala"   2  a2a_sem/formalizer/formalizer_coala.py
start_bg "goal_mcp"           2  mcp_sem/goal_mcp.py
set_goal_mcp_goal "Rotate the robot by 12 degrees"
start_bg "cherrybot_proxy"   10  wot_sem/cherrybot_proxy.py --real
start_bg "app"                2  app.py
start_bg "sem_mcp"            0  mcp_sem/sem_mcp.py

echo
echo "All servers started. Waiting (Ctrl+C to stop)..."
wait
