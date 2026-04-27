import argparse
import os
import time
import hashlib
import re
import threading
import json
from html import escape
from math import atan, cos, degrees, radians, sin, sqrt
from urllib.parse import urljoin

import requests
from flask import Flask, request, Response, stream_with_context, make_response
from werkzeug.middleware.proxy_fix import ProxyFix
from rdflib import Graph
from mcp.server.fastmcp import FastMCP

# ----------------------------
# Configuration (env vars)
# ----------------------------
SIMULATION_ROBOT_BASE_URL = "http://localhost:8099"
REAL_ROBOT_BASE_URL = "https://api.interactions.ics.unisg.ch/cherrybot2"
ROBOT_BASE_URL = SIMULATION_ROBOT_BASE_URL
# Operator registration info
ROBOT_OPERATOR_USERNAME = "cherrybot-proxy"
OPERATOR_TTL_SECONDS = float(os.environ.get("OPERATOR_TTL_SECONDS", str(14 * 60)))

# Requests timeouts
HTTP_TIMEOUT = 15
OPERATION_SPEED = 50

# ----------------------------
# Flask app
# ----------------------------
app = Flask(__name__)

# If deploying behind a reverse proxy / ingress, this makes Flask respect X-Forwarded-*.
# (If you don't need it, remove it.)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

# ----------------------------
# MCP server
# ----------------------------
MCP_HOST = os.getenv("CHERRYBOT_MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("CHERRYBOT_MCP_PORT", "8090"))
mcp = FastMCP(name="Cherrybot MCP", host=MCP_HOST, port=MCP_PORT)

# ----------------------------
# Token cache
# ----------------------------
_token_cache = {"access_token": None, "expires_at": 0.0}
_OPERATION_PATTERN = re.compile(
    r"^\s*(move|rotate)\s*\(\s*([-+]?\d+(?:\.\d+)?)\s*\)\s*$", re.IGNORECASE
)


def _move_compute(x: float, y: float, d: float) -> tuple[float, float]:
    length = sqrt(x**2 + y**2)
    if length == 0:
        return x, y
    a = 1 + d / length
    x_new = a * x
    y_new = a * y
    return x_new, y_new


def _rotate_compute(x: float, y: float, angle: float) -> tuple[float, float]:
    cos_a = cos(angle)
    sin_a = sin(angle)
    x_new = x * cos_a - y * sin_a
    y_new = x * sin_a + y * cos_a
    return x_new, y_new


def _compute_yaw_diff(x_new: float, y_new: float) -> float:
    if x_new == 0:
        theta = 90 if y_new >= 0 else -90
    else:
        theta = degrees(atan(y_new / x_new))
        if x_new < 0:
            theta += 180
    return theta


def _compute_new_yaw(yaw: float) -> float:
    while yaw > 180:
        yaw -= 360
    while yaw <= -180:
        yaw += 360
    return yaw


def _move_target(
    current_x: float,
    current_y: float,
    current_z: float,
    current_yaw: float,
    d: float,
) -> dict:
    x_new, y_new = _move_compute(current_x, current_y, 10 * d)
    return {
        "x": x_new,
        "y": y_new,
        "z": current_z,
        "yaw": current_yaw,
    }


def _rotate_target(
    current_x: float,
    current_y: float,
    current_z: float,
    current_yaw: float,
    a: float,
) -> dict:
    angle = radians(a)
    x_new, y_new = _rotate_compute(current_x, current_y, angle)
    new_yaw = _compute_new_yaw(current_yaw + a)
    return {
        "x": x_new,
        "y": y_new,
        "z": current_z,
        "yaw": new_yaw,
    }


def _parse_operation(raw: str) -> tuple[str, float] | None:
    match = _OPERATION_PATTERN.match(raw or "")
    if not match:
        return None
    op = match.group(1).lower()
    value = float(match.group(2))
    return op, value


def _now() -> float:
    return time.time()


def _http_request(method: str, url: str, **kwargs) -> requests.Response:
    time.sleep(1)
    return requests.request(method=method, url=url, **kwargs)


def _extract_token_from_location(location: str) -> str | None:
    if not location:
        return None
    if "/" not in location:
        return location.strip() or None
    token = location.rstrip("/").split("/")[-1].strip()
    return token or None


def _token_from_operator_response(resp: requests.Response) -> str | None:
    token = _extract_token_from_location(resp.headers.get("Location", ""))
    if token:
        return token
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("token", "apiKey", "apikey", "access_token"):
            if payload.get(key):
                return str(payload[key])
    if isinstance(payload, str):
        return payload.strip() or None
    return None


def _get_robot_token() -> str:
    """
    Fetch and cache a robot API token using the /operator endpoint.
    """
    if _token_cache["access_token"] and (_token_cache["expires_at"] - _now() > 30):
        return _token_cache["access_token"]

    operator_url = _make_target_url("operator")
    headers = {"Accept": "application/json"}

    get_resp = _http_request("GET", operator_url, headers=headers, timeout=HTTP_TIMEOUT)
    if get_resp.ok:
        token = _token_from_operator_response(get_resp)
        if token:
            _token_cache["access_token"] = token
            _token_cache["expires_at"] = _now() + OPERATOR_TTL_SECONDS
            return token

    if not ROBOT_OPERATOR_USERNAME:
        raise RuntimeError("ROBOT_OPERATOR_USERNAME must be set to register as an operator")

    post_resp = _http_request(
        "POST",
        operator_url,
        json={"name": "cherrybot-proxy", "email": "cherrybot-proxy@example.com"},
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    post_resp.raise_for_status()

    token = _token_from_operator_response(post_resp)
    if not token:
        raise RuntimeError(
            "Operator registration succeeded but no token was returned in Location header"
        )

    _token_cache["access_token"] = token
    _token_cache["expires_at"] = _now() + OPERATOR_TTL_SECONDS
    return token


# ----------------------------
# TD handling
# ----------------------------
def _proxy_base_url() -> str:
    """
    Compute the public base URL of the proxy from the current request.
    """
    # request.host_url always ends with '/'
    return request.host_url.rstrip("/")


def _proxy_td_graph(proxy_base: str) -> Graph:
    """
    Build the proxy Thing Description graph directly in Turtle so we don't depend on the robot TD.
    """
    td_turtle = f"""@prefix td: <https://www.w3.org/2019/wot/td#> .
@prefix hctl: <https://www.w3.org/2019/wot/hypermedia#> .
@prefix hmas: <https://purl.org/hmas/> .
@prefix js: <https://www.w3.org/2019/wot/json-schema#> .
@prefix htv: <http://www.w3.org/2011/http#> .

<http://localhost:8086/cherrybot> a td:Thing, hmas:Artifact;
  td:title "cherryBot";
  td:hasBase <{proxy_base}/>;
  td:hasActionAffordance [ a td:ActionAffordance;
      td:name "operation";
      td:description "Perform a move(d) or rotate(a) operation. Request body must be plain text (MIME type text/plain, no JSON), of the form operation_name(param) e.g. move(1) or rotate(1)";
      td:hasForm [
          htv:methodName "POST";
          hctl:hasTarget <{proxy_base}/operation>;
          hctl:forContentType "text/plain";
          hctl:hasOperationType td:invokeAction
        ];
      td:hasInputSchema [ a js:StringSchema ]
    ];
  td:hasActionAffordance [ a td:ActionAffordance;
      td:name "initialize";
      td:description "Initialize the robot to a ready state.";
      td:hasForm [
          htv:methodName "PUT";
          hctl:hasTarget <{proxy_base}/initialize>;
          hctl:hasOperationType td:invokeAction
        ]
    ] .
"""
    g = Graph()
    g.parse(data=td_turtle, format="turtle")
    return g


def _proxy_utcp_manual(proxy_base: str) -> dict:
    return {
        "utcp_version": "1.0.0",
        "manual_version": "1.0.0",
        "tools": [
            {
                "name": "operation",
                "description": (
                    "Perform a move(d) or rotate(a) operation on the robot. Request body must be plain text (MIME type text/plain, no JSON), of the form operation_name(param) e.g. move(1) or rotate(1)"
                ),
                "inputs": {
                    "type": "string",
                    "description": "Operation command as plain text, e.g. move(10).",
                },
                "outputs": {},
                "tags": [],
                "tool_call_template": {
                    "call_template_type": "http",
                    "url": f"{proxy_base}/operation",
                    "http_method": "POST",
                    "content_type": "text/plain",
                },
            },
            {
                "name": "initialize",
                "description": "Initialize the robot to a default state.",
                "inputs": {"type": "object", "properties": {}, "required": []},
                "outputs": {},
                "tags": [],
                "tool_call_template": {
                    "call_template_type": "http",
                    "url": f"{proxy_base}/initialize",
                    "http_method": "PUT",
                },
            },
        ],
    }


@app.get("/td")
def td_endpoint():
    """
    Serve the Thing Description. Default to Turtle, but allow JSON-LD via Accept negotiation.
    """
    g = _proxy_td_graph(_proxy_base_url())

    accept = request.headers.get("Accept", "")
    wants_jsonld = ("application/ld+json" in accept) or ("application/td+json" in accept)

    if wants_jsonld:
        body = g.serialize(format="json-ld", indent=2)
        content_type = "application/ld+json; charset=utf-8"
    else:
        body = g.serialize(format="turtle")
        content_type = "text/turtle; charset=utf-8"

    # Simple ETag for caching
    etag = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304)

    resp = make_response(body, 200)
    resp.headers["Content-Type"] = content_type
    resp.headers["ETag"] = etag
    # Helps caches treat different Accept values separately
    resp.headers["Vary"] = "Accept"
    return resp


@app.get("/utcp")
def utcp_endpoint():
    """
    Serve a UTCP manual mirroring the TD affordances, using the proxy base URL.
    """
    return _proxy_utcp_manual(_proxy_base_url())


# ----------------------------
# Reverse proxy forwarding
# ----------------------------
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def _filtered_request_headers() -> dict:
    """
    Forward most headers, but strip hop-by-hop headers and Host.
    We also strip Authorization/Authentication because the proxy supplies its own.
    """
    out = {}
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in HOP_BY_HOP_HEADERS:
            continue
        if lk in ("host", "content-length", "authorization", "authentication"):
            continue
        out[k] = v

    # Add standard forwarding info
    out["X-Forwarded-For"] = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    out["X-Forwarded-Proto"] = request.headers.get("X-Forwarded-Proto", request.scheme)
    out["X-Forwarded-Host"] = request.headers.get("X-Forwarded-Host", request.host)

    return out


def _make_target_url(path: str) -> str:
    base = ROBOT_BASE_URL.rstrip("/") + "/"
    return urljoin(base, path)


def _robot_request(method: str, path: str, **kwargs) -> requests.Response:
    token = _get_robot_token()
    headers = kwargs.pop("headers", {})
    headers.setdefault("Accept", "application/json")
    headers["Authentication"] = token
    return _http_request(
        method=method,
        url=_make_target_url(path),
        headers=headers,
        timeout=HTTP_TIMEOUT,
        **kwargs,
    )


def _mcp_response_payload(resp: requests.Response):
    try:
        return resp.json()
    except ValueError:
        return resp.text


def _mcp_handle_response(resp: requests.Response) -> dict:
    payload = _mcp_response_payload(resp)
    if resp.ok:
        return payload if isinstance(payload, dict) else {"result": payload}
    return {
        "error": "Robot request failed.",
        "status": resp.status_code,
        "body": payload,
    }


@mcp.tool()
def tcp() -> dict:
    """Read the robot tool center point position and rotation."""
    resp = _robot_request("GET", "tcp")
    return _mcp_handle_response(resp)


@mcp.tool()
def tcpTarget() -> dict:
    """Read the currently configured target TCP pose."""
    resp = _robot_request("GET", "tcp/target")
    return _mcp_handle_response(resp)


@mcp.tool()
def gripper() -> dict:
    """Read the current gripper opening value."""
    resp = _robot_request("GET", "gripper")
    return _mcp_handle_response(resp)


@mcp.tool()
def initialize() -> dict:
    """Initialize the robot to a ready state."""
    resp = _robot_request("PUT", "initialize")
    return _mcp_handle_response(resp)


@mcp.tool()
def setTarget(speed: int, target: dict) -> dict:
    """Command the robot to move towards a target TCP pose at a given speed."""
    payload = {"speed": speed, "target": target}
    resp = _robot_request("PUT", "tcp/target", json=payload)
    return _mcp_handle_response(resp)


@mcp.tool()
def setGripper(value: int) -> dict:
    """Set the gripper opening to a target value."""
    resp = _robot_request("PUT", "gripper", json=value)
    return _mcp_handle_response(resp)


@mcp.tool()
def operation(command: str) -> dict:
    """Perform a move(d) or rotate(a) operation provided as text."""
    parsed = _parse_operation(command)
    if not parsed:
        return {
            "error": "Invalid operation format. Expected move(d) or rotate(a).",
            "received": command,
        }

    op, value = parsed
    tcp_resp = _robot_request("GET", "tcp")
    if not tcp_resp.ok:
        return _mcp_handle_response(tcp_resp)

    tcp_payload = tcp_resp.json()
    coordinate = tcp_payload.get("coordinate") or {}
    rotation = tcp_payload.get("rotation") or {}
    try:
        current_x = float(coordinate["x"])
        current_y = float(coordinate["y"])
        current_z = float(coordinate["z"])
        current_roll = float(rotation["roll"])
        current_pitch = float(rotation["pitch"])
        current_yaw = float(rotation["yaw"])
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "error": "TCP response missing required coordinate/rotation fields.",
            "details": str(exc),
            "payload": tcp_payload,
        }

    if op == "move":
        target = _move_target(current_x, current_y, current_z, current_yaw, value)
    else:
        target = _rotate_target(current_x, current_y, current_z, current_yaw, value)

    target_body = {
        "speed": OPERATION_SPEED,
        "target": {
            "coordinate": {"x": target["x"], "y": target["y"], "z": target["z"]},
            "rotation": {"roll": current_roll, "pitch": current_pitch, "yaw": target["yaw"]},
        },
    }

    move_resp = _robot_request("PUT", "tcp/target", json=target_body)
    if not move_resp.ok:
        return _mcp_handle_response(move_resp)

    return {
        "operation": op,
        "value": value,
        "speed": OPERATION_SPEED,
        "target": target_body["target"],
    }


@app.post("/operation")
def operation_endpoint():
    if request.mimetype != "text/plain":
        return {
            "error": "Invalid content type. Expected text/plain body with command move(d) or rotate(a).",
        }, 415

    raw = request.get_data(cache=False, as_text=True).strip()
    if not raw:
        return {
            "error": "Invalid operation format. Expected plain text body, e.g. move(10).",
            "received": raw,
        }, 400

    parsed = _parse_operation(raw)
    if not parsed:
        return {
            "error": "Invalid operation format. Expected plain text matching move(d) or rotate(a).",
            "received": raw,
        }, 400

    op, value = parsed

    tcp_resp = _robot_request("GET", "tcp")
    if not tcp_resp.ok:
        return Response(
            tcp_resp.content,
            status=tcp_resp.status_code,
            content_type=tcp_resp.headers.get("Content-Type", "application/json"),
        )

    tcp_payload = tcp_resp.json()
    coordinate = tcp_payload.get("coordinate") or {}
    rotation = tcp_payload.get("rotation") or {}
    try:
        current_x = float(coordinate["x"])
        current_y = float(coordinate["y"])
        current_z = float(coordinate["z"])
        current_roll = float(rotation["roll"])
        current_pitch = float(rotation["pitch"])
        current_yaw = float(rotation["yaw"])
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "error": "TCP response missing required coordinate/rotation fields.",
            "details": str(exc),
            "payload": tcp_payload,
        }, 502

    if op == "move":
        target = _move_target(current_x, current_y, current_z, current_yaw, value)
    else:
        target = _rotate_target(current_x, current_y, current_z, current_yaw, value)

    target_body = {
        "speed": OPERATION_SPEED,
        "target": {
            "coordinate": {"x": target["x"], "y": target["y"], "z": target["z"]},
            "rotation": {"roll": current_roll, "pitch": current_pitch, "yaw": target["yaw"]},
        },
    }

    move_resp = _robot_request("PUT", "tcp/target", json=target_body)
    if not move_resp.ok:
        return Response(
            move_resp.content,
            status=move_resp.status_code,
            content_type=move_resp.headers.get("Content-Type", "application/json"),
        )

    return {
        "operation": op,
        "value": value,
        "speed": OPERATION_SPEED,
        "target": target_body["target"],
    }


@app.route("/gui", methods=["GET", "POST"])
def gui_endpoint():
    result = None
    error = None
    move_value = "1"
    rotate_value = "30"

    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()
        if action == "initialize":
            result = initialize()
        elif action in {"move", "rotate"}:
            raw_value = (request.form.get("value") or "").strip()
            if action == "move":
                move_value = raw_value or move_value
            else:
                rotate_value = raw_value or rotate_value

            try:
                value = float(raw_value)
            except ValueError:
                error = f"Invalid numeric value for {action}: {raw_value!r}"
            else:
                result = operation(f"{action}({value})")
        else:
            error = "Invalid action."

        if isinstance(result, dict) and result.get("error"):
            error = result.get("error")

    result_json = escape(json.dumps(result, indent=2, sort_keys=True)) if result else ""
    error_html = f"<p class=\"error\">{escape(error)}</p>" if error else ""

    html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Cherrybot Proxy Control</title>
    <style>
      body {{ font-family: "Georgia", "Times New Roman", serif; margin: 0; background: #f7f1e8; color: #1f1a12; }}
      header {{ padding: 20px 28px; background: #efe5d5; border-bottom: 1px solid #d8cbb3; }}
      h1 {{ margin: 0 0 8px; font-size: 26px; }}
      main {{ padding: 24px 28px; display: grid; gap: 16px; max-width: 760px; }}
      .card {{ background: #fffaf1; border: 1px solid #d8cbb3; border-radius: 12px; padding: 14px; box-shadow: 0 8px 18px rgba(0,0,0,0.08); }}
      form {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
      label {{ min-width: 60px; }}
      input {{ padding: 7px 8px; border-radius: 8px; border: 1px solid #bfae8f; }}
      button {{ padding: 8px 12px; border-radius: 8px; border: 1px solid #7f6a4b; background: #e3d3b8; cursor: pointer; }}
      button:hover {{ background: #d9c6a6; }}
      .error {{ color: #8b1e1e; font-weight: 600; }}
      pre {{ white-space: pre-wrap; word-break: break-word; margin: 0; }}
    </style>
  </head>
  <body>
    <header>
      <h1>Cherrybot Proxy Control</h1>
      <div>Run move(d), rotate(a), or reinitialize the robot.</div>
    </header>
    <main>
      <section class="card">
        <h2>Move</h2>
        <form method="post" action="/gui">
          <input type="hidden" name="action" value="move" />
          <label for="move-value">d</label>
          <input id="move-value" type="number" name="value" step="any" value="{escape(move_value)}" required />
          <button type="submit">Run move(d)</button>
        </form>
      </section>
      <section class="card">
        <h2>Rotate</h2>
        <form method="post" action="/gui">
          <input type="hidden" name="action" value="rotate" />
          <label for="rotate-value">a</label>
          <input id="rotate-value" type="number" name="value" step="any" value="{escape(rotate_value)}" required />
          <button type="submit">Run rotate(a)</button>
        </form>
      </section>
      <section class="card">
        <h2>Initialize</h2>
        <form method="post" action="/gui">
          <input type="hidden" name="action" value="initialize" />
          <button type="submit">Reinitialize Robot</button>
        </form>
      </section>
      <section class="card">
        <h2>Last Result</h2>
        {error_html}
        <pre>{result_json}</pre>
      </section>
    </main>
  </body>
</html>
"""
    return Response(html, mimetype="text/html")


@app.route(
    "/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
)
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
def proxy_all(path: str):
    # Don't proxy /td (handled above)
    if path == "td":
        return td_endpoint()
    if path == "utcp":
        return utcp_endpoint()
    if path == "gui":
        return gui_endpoint()

    target_url = _make_target_url(path)

    # Token injected here; client calling the proxy does NOT need any token.
    token = _get_robot_token()

    headers = _filtered_request_headers()
    headers["Authentication"] = token

    # Forward query string and body as-is
    params = request.args
    data = request.get_data(cache=False)  # raw body bytes
    method = request.method

    upstream = _http_request(
        method=method,
        url=target_url,
        params=params,
        data=data if data else None,
        headers=headers,
        stream=True,
        timeout=HTTP_TIMEOUT,
        allow_redirects=False,
    )

    # Build response back to client
    def generate():
        for chunk in upstream.iter_content(chunk_size=8192):
            if chunk:
                yield chunk

    resp_headers = []
    for k, v in upstream.headers.items():
        lk = k.lower()
        if lk in HOP_BY_HOP_HEADERS:
            continue
        # Requests may set a content-length; Flask can handle it, but we can keep it.
        resp_headers.append((k, v))

    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        headers=resp_headers,
    )


@app.get("/health")
def health():
    return {"status": "ok"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Expose a WoT/UTCP proxy for the Cherrybot.")
    parser.add_argument(
        "--real",
        action="store_true",
        help="Connect to the real Cherrybot API instead of the local simulation.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    ROBOT_BASE_URL = REAL_ROBOT_BASE_URL if args.real else SIMULATION_ROBOT_BASE_URL

    # Run the HTTP proxy and MCP server in the same process.
    proxy_thread = threading.Thread(
        target=app.run,
        kwargs={"host": "0.0.0.0", "port": 8086, "debug": True, "use_reloader": False},
        daemon=True,
    )
    proxy_thread.start()

    # Streamable HTTP MCP server (no uvicorn needed)
    mcp.run(transport="streamable-http")
