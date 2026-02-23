import threading
import time
import uuid
from math import atan, cos, degrees, radians, sin, sqrt

from flask import Flask, Response, jsonify, make_response, request

# ----------------------------
# Simple in-memory robot simulation
# ----------------------------
app = Flask(__name__)

_STATE_LOCK = threading.Lock()

_DEFAULT_COORDINATE = {"x": 0.0, "y": 0.0, "z": 0.0}
_DEFAULT_ROTATION = {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}

_STATE = {
    "operator": {"name": None, "email": None, "token": None},
    "tcp": {"coordinate": dict(_DEFAULT_COORDINATE), "rotation": dict(_DEFAULT_ROTATION)},
    "tcp_target": {"coordinate": dict(_DEFAULT_COORDINATE), "rotation": dict(_DEFAULT_ROTATION)},
    "gripper": 0,
    "initialized": False,
    "last_updated": time.time(),
}

_OPERATION_PATTERN = r"^(move|rotate)\([-+]?\d+(?:\.\d+)?\)$"


def _now() -> float:
    return time.time()


def _new_token() -> str:
    return uuid.uuid4().hex


def _require_auth() -> bool:
    token = request.headers.get("Authentication", "")
    with _STATE_LOCK:
        return bool(token) and token == _STATE["operator"]["token"]


def _error(status: int, message: str):
    return make_response(jsonify({"error": message}), status)


def _move_compute(x: float, y: float, d: float) -> tuple[float, float]:
    length = sqrt(x**2 + y**2)
    if length == 0:
        return x, y
    a = 1 + d / length
    return a * x, a * y


def _rotate_compute(x: float, y: float, angle: float) -> tuple[float, float]:
    r = sqrt(x**2 + y**2)
    if x == 0:
        theta = radians(90 if y >= 0 else -90)
    else:
        theta = atan(y / x)
    new_theta = theta + angle
    return r * cos(new_theta), r * sin(new_theta)


def _compute_yaw_diff(x_new: float, y_new: float) -> float:
    if x_new == 0:
        theta = 90 if y_new >= 0 else -90
    else:
        theta = degrees(atan(y_new / x_new))
        if x_new < 0:
            theta += 180
    return theta


def _compute_new_yaw(current_yaw: float, yaw_diff: float) -> float:
    new_yaw = current_yaw + yaw_diff
    if new_yaw > 180:
        new_yaw = -180 + (new_yaw - 180)
    elif new_yaw < -180:
        new_yaw += 360
    return new_yaw


def _parse_operation(raw: str) -> tuple[str, float] | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("move") or raw.startswith("rotate"):
        try:
            op_name, rest = raw.split("(", 1)
            op = op_name.strip().lower()
            value = float(rest.rstrip(")"))
            return op, value
        except Exception:
            return None
    return None


@app.get("/operator")
def operator_get():
    if not _require_auth():
        return _error(401, "Missing or invalid Authentication token")
    with _STATE_LOCK:
        return jsonify(_STATE["operator"])


@app.post("/operator")
def operator_post():
    payload = request.get_json(silent=True) or {}
    name = payload.get("name")
    email = payload.get("email")
    if not name or not email:
        return _error(400, "name and email are required")
    with _STATE_LOCK:
        token = _new_token()
        _STATE["operator"] = {"name": name, "email": email, "token": token}
        _STATE["last_updated"] = _now()
    resp = jsonify({"name": name, "email": email, "token": token})
    resp.headers["Location"] = f"/operator/{token}"
    return resp


@app.delete("/operator/<token>")
def operator_delete(token: str):
    if not _require_auth():
        return _error(401, "Missing or invalid Authentication token")
    with _STATE_LOCK:
        if token != _STATE["operator"]["token"]:
            return _error(404, "Operator token not found")
        _STATE["operator"] = {"name": None, "email": None, "token": None}
        _STATE["last_updated"] = _now()
    return jsonify({"ok": True})


@app.get("/tcp")
def tcp_get():
    if not _require_auth():
        return _error(401, "Missing or invalid Authentication token")
    with _STATE_LOCK:
        return jsonify(_STATE["tcp"])


@app.get("/tcp/target")
def tcp_target_get():
    if not _require_auth():
        return _error(401, "Missing or invalid Authentication token")
    with _STATE_LOCK:
        return jsonify(_STATE["tcp_target"])


@app.put("/tcp/target")
def tcp_target_put():
    if not _require_auth():
        return _error(401, "Missing or invalid Authentication token")
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return _error(400, "Invalid payload")
    target = payload.get("target")
    if not isinstance(target, dict):
        return _error(400, "target is required")
    coordinate = target.get("coordinate") or {}
    rotation = target.get("rotation") or {}
    try:
        new_tcp = {
            "coordinate": {
                "x": float(coordinate["x"]),
                "y": float(coordinate["y"]),
                "z": float(coordinate["z"]),
            },
            "rotation": {
                "roll": float(rotation["roll"]),
                "pitch": float(rotation["pitch"]),
                "yaw": float(rotation["yaw"]),
            },
        }
    except (KeyError, TypeError, ValueError):
        return _error(
            400,
            "target.coordinate and target.rotation must contain numeric x/y/z and roll/pitch/yaw",
        )

    with _STATE_LOCK:
        _STATE["tcp_target"] = new_tcp
        _STATE["tcp"] = new_tcp
        _STATE["last_updated"] = _now()
    return jsonify({"ok": True, "tcp_target": new_tcp})


@app.get("/gripper")
def gripper_get():
    if not _require_auth():
        return _error(401, "Missing or invalid Authentication token")
    with _STATE_LOCK:
        return jsonify(_STATE["gripper"])


@app.put("/gripper")
def gripper_put():
    if not _require_auth():
        return _error(401, "Missing or invalid Authentication token")
    payload = request.get_json(silent=True)
    if payload is None:
        return _error(400, "Gripper value required")
    try:
        value = int(payload)
    except (TypeError, ValueError):
        return _error(400, "Gripper value must be an integer")
    if value < 0 or value > 800:
        return _error(400, "Gripper value must be between 0 and 800")
    with _STATE_LOCK:
        _STATE["gripper"] = value
        _STATE["last_updated"] = _now()
    return jsonify({"ok": True, "gripper": value})


@app.put("/initialize")
def initialize_put():
    if not _require_auth():
        return _error(401, "Missing or invalid Authentication token")
    with _STATE_LOCK:
        _STATE["initialized"] = True
        _STATE["last_updated"] = _now()
    return jsonify({"ok": True})


@app.post("/operation")
def operation_post():
    if not _require_auth():
        return _error(401, "Missing or invalid Authentication token")
    raw = None
    if request.is_json:
        payload = request.get_json(silent=True)
        if isinstance(payload, dict) and "body" in payload:
            raw = payload.get("body")
        else:
            raw = payload
    if raw is None:
        raw = request.get_data(as_text=True)
    if not isinstance(raw, str):
        return _error(400, "Operation must be a string")

    parsed = _parse_operation(raw)
    if not parsed:
        return _error(400, "Invalid operation format. Expected move(d) or rotate(a).")

    op, value = parsed
    with _STATE_LOCK:
        coordinate = _STATE["tcp"]["coordinate"]
        rotation = _STATE["tcp"]["rotation"]
        current_x = float(coordinate["x"])
        current_y = float(coordinate["y"])
        current_z = float(coordinate["z"])
        current_roll = float(rotation["roll"])
        current_pitch = float(rotation["pitch"])
        current_yaw = float(rotation["yaw"])

        if op == "move":
            x_new, y_new = _move_compute(current_x, current_y, 10 * value)
            target_yaw = current_yaw
        else:
            x_new, y_new = _rotate_compute(current_x, current_y, radians(value))
            yaw_diff = _compute_yaw_diff(x_new, y_new)
            target_yaw = _compute_new_yaw(current_yaw, yaw_diff)

        new_tcp = {
            "coordinate": {"x": x_new, "y": y_new, "z": current_z},
            "rotation": {"roll": current_roll, "pitch": current_pitch, "yaw": target_yaw},
        }
        _STATE["tcp_target"] = new_tcp
        _STATE["tcp"] = new_tcp
        _STATE["last_updated"] = _now()

    return jsonify(
        {
            "operation": op,
            "value": value,
            "target": new_tcp,
        }
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def gui():
    html = """<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Cherrybot Simulation</title>
    <style>
      body { font-family: "Georgia", "Times New Roman", serif; margin: 0; background: #f7f1e8; color: #1f1a12; }
      header { padding: 20px 28px; background: #efe5d5; border-bottom: 1px solid #d8cbb3; }
      h1 { margin: 0 0 8px; font-size: 26px; }
      main { padding: 24px 28px; display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; }
      .card { background: #fffaf1; border: 1px solid #d8cbb3; border-radius: 12px; padding: 14px; box-shadow: 0 8px 18px rgba(0,0,0,0.08); }
      pre { white-space: pre-wrap; word-break: break-word; }
    </style>
  </head>
  <body>
    <header>
      <h1>Cherrybot Simulation</h1>
      <div>Live state snapshot</div>
    </header>
    <main>
      <div class=\"card\"><pre id=\"state\"></pre></div>
    </main>
    <script>
      async function refresh() {
        const res = await fetch('/state');
        const data = await res.json();
        document.getElementById('state').textContent = JSON.stringify(data, null, 2);
      }
      refresh();
      setInterval(refresh, 1500);
    </script>
  </body>
</html>
"""
    return Response(html, mimetype="text/html")


@app.get("/state")
def state():
    with _STATE_LOCK:
        return jsonify(_STATE)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099, debug=True, use_reloader=False)
