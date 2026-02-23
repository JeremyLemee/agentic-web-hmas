#!/usr/bin/env bash
set -euo pipefail

# mcp-http.sh — tiny MCP Streamable HTTP client (curl + jq)
#
# Requirements: bash 4+, curl, jq
#
# Usage:
#   ./mcp-http.sh https://example.com/mcp
#
# Optional auth/custom headers:
#   export MCP_HEADERS=$'Authorization: Bearer YOURTOKEN\nX-Trace-Id: 123'
#
# Notes:
# - Handles JSON responses and basic SSE ("data: {json}") responses for POSTs.
# - SSE parsing is intentionally simple (works for the common "one JSON per data line" case).

MCP_URL="${1:-}"
if [[ -z "${MCP_URL}" ]]; then
  echo "Usage: $0 <mcp_endpoint_url>" >&2
  exit 1
fi

command -v curl >/dev/null || { echo "Missing dependency: curl" >&2; exit 1; }
command -v jq   >/dev/null || { echo "Missing dependency: jq" >&2; exit 1; }

REQ_ID=1
SESSION_ID=""
LISTEN_PID=""

# Read extra headers from MCP_HEADERS (one header per line)
declare -a EXTRA_CURL_HEADERS=()
if [[ -n "${MCP_HEADERS:-}" ]]; then
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    EXTRA_CURL_HEADERS+=(-H "$line")
  done < <(printf '%s\n' "$MCP_HEADERS")
fi

next_id() {
  local id="$REQ_ID"
  REQ_ID=$((REQ_ID + 1))
  printf '%s' "$id"
}

# Extract a header value (case-insensitive) from a curl -D header file.
# Usage: header_get <file> <Header-Name>
header_get() {
  local file="$1"
  local name="$2"
  # Normalize CRLF, then grep case-insensitively
  tr -d '\r' <"$file" | awk -v IGNORECASE=1 -v h="$name" '
    BEGIN { found=0 }
    $0 ~ "^" h ":" {
      sub("^[^:]*:[[:space:]]*", "", $0)
      print $0
      found=1
      exit
    }
    END { if (!found) exit 1 }
  '
}

content_type_from_headers() {
  local file="$1"
  (header_get "$file" "Content-Type" 2>/dev/null || true) | awk '{print tolower($1)}' | tr -d ';'
}

http_status_from_headers() {
  local file="$1"
  # First HTTP status line: HTTP/1.1 200 OK
  tr -d '\r' <"$file" | awk 'NR==1 {print $2; exit}'
}

curl_post() {
  local payload="$1"
  local hdr_file body_file
  hdr_file="$(mktemp)"
  body_file="$(mktemp)"

  local -a sess_header=()
  if [[ -n "${SESSION_ID}" ]]; then
    sess_header=(-H "Mcp-Session-Id: ${SESSION_ID}")
  fi

  curl -sS -D "$hdr_file" -o "$body_file" \
    "${EXTRA_CURL_HEADERS[@]}" \
    "${sess_header[@]}" \
    -H "Accept: application/json, text/event-stream" \
    -H "Content-Type: application/json" \
    --data "$payload" \
    "$MCP_URL"

  printf '%s\t%s\n' "$hdr_file" "$body_file"
}

curl_delete_session() {
  [[ -z "${SESSION_ID}" ]] && return 0
  # Spec says clients SHOULD send DELETE with Mcp-Session-Id when done; server may 405. :contentReference[oaicite:3]{index=3}
  curl -sS -o /dev/null -w "%{http_code}" \
    "${EXTRA_CURL_HEADERS[@]}" \
    -H "Mcp-Session-Id: ${SESSION_ID}" \
    -X DELETE \
    "$MCP_URL" || true
}

# Parse a POST response that might be JSON or SSE.
# Args: headers_file body_file expected_id
# Prints: matching JSON-RPC response object (single line JSON) to stdout, or empty if none.
parse_post_response() {
  local hdr_file="$1"
  local body_file="$2"
  local expected_id="$3"

  local status ctype
  status="$(http_status_from_headers "$hdr_file")"
  ctype="$(content_type_from_headers "$hdr_file")"

  # Basic HTTP error handling
  if [[ -n "$status" && "$status" =~ ^[45] ]]; then
    echo "HTTP error $status" >&2
    echo "Headers:" >&2
    tr -d '\r' <"$hdr_file" >&2
    echo "Body:" >&2
    cat "$body_file" >&2
    return 2
  fi

  # If it's a normal JSON response, just return it.
  if [[ "$ctype" == "application/json" || "$ctype" == "application/json;charset=utf-8" ]]; then
    # Might be a JSON-RPC response, or a batch (array)
    jq -c --argjson id "$expected_id" '
      if type=="array" then
        (map(select(.id == $id)) | .[0]) // empty
      else
        (select(.id == $id)) // empty
      end
    ' <"$body_file"
    return 0
  fi

  # If it's SSE, extract data lines and find the JSON-RPC response with matching id.
  if [[ "$ctype" == "text/event-stream" ]]; then
    # Collect each "data:" line as a standalone JSON snippet (common case).
    # Some servers may emit batches; we handle both object and array per line.
    awk '
      BEGIN{OFS=""}
      $0 ~ /^data:[[:space:]]*/ {
        sub(/^data:[[:space:]]*/, "", $0)
        print $0
      }
    ' <"$body_file" | while IFS= read -r json; do
      [[ -z "$json" ]] && continue
      # Try to parse and match id
      echo "$json" | jq -c --argjson id "$expected_id" '
        if type=="array" then
          (map(select(.id == $id)) | .[0]) // empty
        else
          (select(.id == $id)) // empty
        end
      ' 2>/dev/null || true
    done | awk 'NF{print; exit}'
    return 0
  fi

  # No/unknown body types (e.g., 202 Accepted, empty body)
  return 0
}

rpc_request() {
  local method="$1"
  local params_json="${2:-null}"

  local id payload hdr_body hdr_file body_file resp

  id="$(next_id)"

  # Build JSON-RPC request. params are required for some methods; optional for others per schema. :contentReference[oaicite:4]{index=4}
  payload="$(jq -nc --arg m "$method" --argjson p "$params_json" --argjson id "$id" '
    {jsonrpc:"2.0", id:$id, method:$m} + (if $p == null then {} else {params:$p} end)
  ')"

  hdr_body="$(curl_post "$payload")"
  hdr_file="${hdr_body%%$'\t'*}"
  body_file="${hdr_body##*$'\t'}"

  # Capture session header on initialize if present. :contentReference[oaicite:5]{index=5}
  if [[ "$method" == "initialize" ]]; then
    local sid=""
    sid="$(header_get "$hdr_file" "Mcp-Session-Id" 2>/dev/null || true)"
    if [[ -n "$sid" ]]; then
      SESSION_ID="$sid"
    fi
  fi

  resp="$(parse_post_response "$hdr_file" "$body_file" "$id" || true)"

  rm -f "$hdr_file" "$body_file"

  if [[ -n "$resp" ]]; then
    echo "$resp"
  fi
}

rpc_notify() {
  local method="$1"
  local params_json="${2:-null}"

  local payload hdr_body hdr_file body_file

  payload="$(jq -nc --arg m "$method" --argjson p "$params_json" '
    {jsonrpc:"2.0", method:$m} + (if $p == null then {} else {params:$p} end)
  ')"

  hdr_body="$(curl_post "$payload")"
  hdr_file="${hdr_body%%$'\t'*}"
  body_file="${hdr_body##*$'\t'}"
  rm -f "$hdr_file" "$body_file"
}

pretty_tools() {
  jq -r '
    .result.tools[]? |
    "- \(.name)\n    \(.description // "(no description)")"
  '
}

pretty_resources() {
  jq -r '
    .result.resources[]? |
    "- \(.uri)\n    name: \(.name)\n    mime: \(.mimeType // "(unknown)")\n    desc: \(.description // "(no description)")"
  '
}

cmd_help() {
  cat <<'EOF'
Commands:
  help                         Show this help
  session                      Show current session id (if any)
  tools [cursor]               List tools (tools/list)
  resources [cursor]           List resources (resources/list)
  read <uri>                   Read a resource (resources/read)
  call <tool> [jsonArgs]       Call a tool (tools/call). jsonArgs defaults to {}
  raw <jsonrpc>                Send a raw JSON-RPC object (advanced)
  listen                       Open a GET SSE stream (prints server notifications/requests)
  unlisten                     Stop SSE listener
  quit | exit                  Quit (sends HTTP DELETE to end session when possible)

Examples:
  tools
  resources
  read file:///README.md
  call echo {"text":"hi"}
EOF
}

start_listener() {
  if [[ -n "${LISTEN_PID}" ]] && kill -0 "${LISTEN_PID}" 2>/dev/null; then
    echo "Listener already running (pid ${LISTEN_PID})."
    return 0
  fi

  local -a sess_header=()
  if [[ -n "${SESSION_ID}" ]]; then
    sess_header=(-H "Mcp-Session-Id: ${SESSION_ID}")
  fi

  echo "Starting SSE listener (GET ${MCP_URL})..."
  # Per spec, GET may return SSE or 405 if unsupported. :contentReference[oaicite:6]{index=6}
  (
    curl -sS -N \
      "${EXTRA_CURL_HEADERS[@]}" \
      "${sess_header[@]}" \
      -H "Accept: text/event-stream" \
      "$MCP_URL" \
    | awk '
        $0 ~ /^data:[[:space:]]*/ {
          sub(/^data:[[:space:]]*/, "", $0)
          print "[SSE data] " $0
          fflush()
        }
        $0 ~ /^event:/ {
          print "[SSE " $0 "]"
          fflush()
        }
      '
  ) &
  LISTEN_PID="$!"
  echo "Listener pid: ${LISTEN_PID}"
}

stop_listener() {
  if [[ -n "${LISTEN_PID}" ]] && kill -0 "${LISTEN_PID}" 2>/dev/null; then
    kill "${LISTEN_PID}" 2>/dev/null || true
    wait "${LISTEN_PID}" 2>/dev/null || true
    echo "Listener stopped."
  fi
  LISTEN_PID=""
}

cleanup() {
  stop_listener || true
  local code
  code="$(curl_delete_session || true)"
  if [[ -n "${SESSION_ID}" ]]; then
    echo "Session cleanup: HTTP DELETE returned ${code} (405 is ok; some servers don't allow client termination)."
  fi
}
trap cleanup EXIT

# ---- 1) initialize + 2) notifications/initialized ----
INIT_PARAMS="$(jq -nc '
  {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "bash-mcp", version: "0.1.0" }
  }
')"

echo "Initializing MCP session at: ${MCP_URL}"
init_resp="$(rpc_request "initialize" "$INIT_PARAMS" || true)"
if [[ -z "$init_resp" ]]; then
  echo "Initialize failed (no response). Check URL/auth." >&2
  exit 2
fi

echo "$init_resp" | jq .
if [[ -n "${SESSION_ID}" ]]; then
  echo "Got Mcp-Session-Id: ${SESSION_ID}"
else
  echo "No Mcp-Session-Id header returned (server may be stateless or using other session scheme)."
fi

# Per lifecycle, client sends notifications/initialized after init completes. :contentReference[oaicite:7]{index=7}
rpc_notify "notifications/initialized" "null" || true

# ---- REPL ----
cmd_help
while true; do
  printf "mcp> "
  IFS= read -r line || break
  [[ -z "$line" ]] && continue

  # Shell-ish splitting, but keep the rest for raw/json args
  cmd="${line%% *}"
  rest="${line#"$cmd"}"
  rest="${rest# }"

  case "$cmd" in
    help)
      cmd_help
      ;;
    session)
      echo "MCP endpoint: ${MCP_URL}"
      echo "Mcp-Session-Id: ${SESSION_ID:-"(none)"}"
      ;;
    tools)
      cursor="${rest:-}"
      params="null"
      if [[ -n "$cursor" ]]; then
        params="$(jq -nc --arg c "$cursor" '{cursor:$c}')"
      fi
      resp="$(rpc_request "tools/list" "$params" || true)"
      if [[ -z "$resp" ]]; then
        echo "(no response)"
      else
        echo "$resp" | jq . >/dev/null 2>&1 || { echo "$resp"; continue; }
        echo "$resp" | jq .result >/dev/null 2>&1 || { echo "$resp" | jq .; continue; }
        echo "$resp" | jq . | pretty_tools
        # Show nextCursor if present
        nc="$(echo "$resp" | jq -r '.result.nextCursor // empty')"
        [[ -n "$nc" ]] && echo "nextCursor: $nc"
      fi
      ;;
    resources)
      cursor="${rest:-}"
      params="null"
      if [[ -n "$cursor" ]]; then
        params="$(jq -nc --arg c "$cursor" '{cursor:$c}')"
      fi
      resp="$(rpc_request "resources/list" "$params" || true)"
      if [[ -z "$resp" ]]; then
        echo "(no response)"
      else
        echo "$resp" | jq . | pretty_resources
        nc="$(echo "$resp" | jq -r '.result.nextCursor // empty')"
        [[ -n "$nc" ]] && echo "nextCursor: $nc"
      fi
      ;;
    read)
      uri="$rest"
      if [[ -z "$uri" ]]; then
        echo "Usage: read <uri>"
        continue
      fi
      params="$(jq -nc --arg u "$uri" '{uri:$u}')"
      resp="$(rpc_request "resources/read" "$params" || true)"
      if [[ -z "$resp" ]]; then
        echo "(no response)"
      else
        # Print nicely: show each content entry
        echo "$resp" | jq '
          .result.contents as $c
          | {contents: $c}
        '
      fi
      ;;
    call)
      tool="${rest%% *}"
      args="${rest#"$tool"}"
      args="${args# }"
      [[ -z "$tool" ]] && { echo "Usage: call <tool> [jsonArgs]"; continue; }
      if [[ -z "${args:-}" || "$args" == "$tool" ]]; then
        args='{}'
      fi
      # Validate args is JSON
      if ! echo "$args" | jq -e . >/dev/null 2>&1; then
        echo "jsonArgs must be valid JSON (example: call echo {\"text\":\"hi\"})"
        continue
      fi
      params="$(jq -nc --arg n "$tool" --argjson a "$args" '{name:$n, arguments:$a}')"
      resp="$(rpc_request "tools/call" "$params" || true)"
      if [[ -z "$resp" ]]; then
        echo "(no response)"
      else
        echo "$resp" | jq .
      fi
      ;;
    raw)
      if [[ -z "$rest" ]]; then
        echo "Usage: raw <jsonrpc>"
        continue
      fi
      if ! echo "$rest" | jq -e . >/dev/null 2>&1; then
        echo "raw payload must be valid JSON"
        continue
      fi
      # Send as-is (still as POST). Note: we don't auto-extract id here.
      hdr_body="$(curl_post "$rest")"
      hdr_file="${hdr_body%%$'\t'*}"
      body_file="${hdr_body##*$'\t'}"
      echo "HTTP $(http_status_from_headers "$hdr_file") $(content_type_from_headers "$hdr_file")"
      tr -d '\r' <"$hdr_file" | sed -n '1,20p'
      echo "--- body ---"
      cat "$body_file"
      rm -f "$hdr_file" "$body_file"
      ;;
    listen)
      start_listener
      ;;
    unlisten)
      stop_listener
      ;;
    quit|exit)
      break
      ;;
    *)
      echo "Unknown command: $cmd (type 'help')"
      ;;
  esac
done
