#!/usr/bin/env python3
"""
wot_consumer.py — tiny WoT Thing consumer CLI

Usage:
  python wot_sem/wot_consumer.py <td_url_or_file>

Optional headers (repeatable):
  python wot_sem/wot_consumer.py <td_url_or_file> -H "Authorization: Bearer TOKEN"
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from rdflib import Graph

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wot_sem.affordances.thing_description import ThingDescription


HELP = """Commands:
  help
  info
  properties
  actions
  events
  forms <type> <name>
  read <property>
  write [-p] <property> [jsonValue]
  invoke [-p] <action> [jsonArgs]
  quit | exit

Examples:
  properties
  read temperature
  write -p threshold
  invoke blink {"times":3}
"""


def _parse_json_value(s: str) -> Any:
    s = s.strip()
    if not s:
        return None
    return json.loads(s)


def _schema_props(schema: Optional[dict]) -> Tuple[dict[str, Any], set[str]]:
    if not isinstance(schema, dict):
        return {}, set()
    properties_value = schema.get("properties")
    required_value = schema.get("required")
    props: dict[str, Any] = properties_value if isinstance(properties_value, dict) else {}
    req: list[Any] = required_value if isinstance(required_value, list) else []
    return props, {x for x in req if isinstance(x, str)}


def _schema_type(s: dict) -> str:
    t = s.get("type")
    if isinstance(t, str):
        return t
    if isinstance(t, list) and t:
        return "|".join(str(x) for x in t)
    if "enum" in s:
        return "enum"
    if "oneOf" in s:
        return "oneOf"
    if "anyOf" in s:
        return "anyOf"
    return "unknown"


def _format_param_line(name: str, s: dict, required: bool) -> str:
    t = _schema_type(s)
    desc = (s.get("description") or "").strip()
    default = s.get("default", None)
    enum = s.get("enum", None)

    bits = [name, f"({t}{', required' if required else ''})"]
    if default is not None:
        bits.append(f"default={default!r}")
    if isinstance(enum, list) and enum:
        preview = enum if len(enum) <= 8 else enum[:8] + ["..."]
        bits.append(f"enum={preview!r}")

    line = "  - " + " ".join(bits)
    if desc:
        line += f"\n      {desc}"
    return line


def _read_td(td_source: str) -> ThingDescription:
    g = Graph()
    source_path = Path(td_source)
    if source_path.exists():
        g.parse(source_path.as_posix())
        return ThingDescription(g)

    try:
        g.parse(td_source)
    except Exception:
        with urlopen(Request(td_source)) as resp:
            data = resp.read()
        try:
            g.parse(data=data, format="json-ld", publicID=td_source)
        except Exception:
            g.parse(data=data, format="turtle", publicID=td_source)
    return ThingDescription(g)


def _resolve_target(td: ThingDescription, target: str) -> str:
    parsed = urlparse(target)
    if parsed.scheme:
        return target

    base = None
    if td.base_uri is not None:
        base = str(td.base_uri)
    elif td.id is not None:
        base = str(td.id)
    if base:
        return urljoin(base, target)
    return target


def _select_form(affordance, op_type: Optional[str]) -> Optional[Any]:
    forms = list(affordance.forms or [])
    if not forms:
        return None

    op_type_l = op_type.lower() if op_type else None
    if op_type_l:
        candidates = [f for f in forms if op_type_l in (f.operation_types or set())]
        if candidates:
            forms = candidates

    # Prefer HTTP(S) targets if present.
    for f in forms:
        if f.protocol and f.protocol.lower() in {"http", "https"}:
            return f
    return forms[0]


def _default_method(op_type: str) -> str:
    mapping = {
        "readproperty": "GET",
        "writeproperty": "PUT",
        "invokeaction": "POST",
        "subscribeevent": "GET",
        "unsubscribeevent": "GET",
    }
    return mapping.get(op_type, "GET")


def _coerce_from_schema(user_text: str, schema: dict) -> Any:
    user_text = user_text.strip()
    t = _schema_type(schema)
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        try:
            v = json.loads(user_text)
        except Exception:
            v = user_text
        if v not in enum:
            raise ValueError(f"value must be one of {enum!r}")
        return v

    if t in ("object", "array"):
        return json.loads(user_text)
    if t == "boolean":
        lowered = user_text.lower()
        if lowered in ("true", "t", "yes", "y", "1"):
            return True
        if lowered in ("false", "f", "no", "n", "0"):
            return False
        raise ValueError("expected a boolean (true/false, yes/no, 1/0)")
    if t == "integer":
        return int(user_text)
    if t == "number":
        return float(user_text)

    try:
        return json.loads(user_text)
    except Exception:
        return user_text


def _prompt_for_schema(schema: Optional[dict], label: str) -> Any:
    if not isinstance(schema, dict) or not schema:
        raw = input(f"{label} (JSON, blank for none): ").strip()
        return None if raw == "" else json.loads(raw)

    props, req_set = _schema_props(schema)
    if not props:
        raw = input(f"{label} value (type={_schema_type(schema)}): ").strip()
        if raw == "":
            return None
        return _coerce_from_schema(raw, schema)

    args: Dict[str, Any] = {}
    print(f"Prompting for parameters: {label}")
    print("(Press Enter to skip optional params; JSON required for object/array.)")
    for pname, pschema in props.items():
        if not isinstance(pschema, dict):
            pschema = {}
        required = pname in req_set
        desc = (pschema.get("description") or "").strip()
        default = pschema.get("default", None)
        enum = pschema.get("enum", None)

        hint_bits = [f"type={_schema_type(pschema)}"]
        if required:
            hint_bits.append("required")
        if default is not None:
            hint_bits.append(f"default={default!r}")
        if isinstance(enum, list) and enum:
            preview = enum if len(enum) <= 8 else enum[:8] + ["..."]
            hint_bits.append(f"enum={preview!r}")

        print(f"\n{pname} ({', '.join(hint_bits)})")
        if desc:
            print(f"  {desc}")

        while True:
            raw = input(f"  value for {pname}: ").strip()
            if raw == "":
                if default is not None:
                    args[pname] = default
                    break
                if required:
                    print("  This parameter is required.")
                    continue
                break
            try:
                args[pname] = _coerce_from_schema(raw, pschema)
                break
            except json.JSONDecodeError:
                print('  Invalid JSON. For object/array, enter valid JSON (e.g. {"k":1} or [1,2]).')
            except Exception as e:
                print(f"  {e}")
    return args


def _http_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[bytes],
    timeout: float,
) -> Tuple[int, Dict[str, str], bytes]:
    req = Request(url, method=method, data=body)
    for k, v in headers.items():
        req.add_header(k, v)
    with urlopen(req, timeout=timeout) as resp:
        status = resp.getcode()
        resp_headers = {k: v for k, v in resp.headers.items()}
        data = resp.read()
    return status, resp_headers, data


def _print_response(status: int, headers: Dict[str, str], data: bytes) -> None:
    print(f"status: {status}")
    content_type = headers.get("Content-Type", "")
    if data:
        text = data.decode("utf-8", errors="replace")
        if "json" in content_type.lower():
            try:
                obj = json.loads(text)
                print(json.dumps(obj, indent=2))
                return
            except Exception:
                pass
        print(text)
    else:
        print("(empty response)")


def _print_affordances(title: str, items: Iterable[Any]) -> None:
    items = list(items)
    if not items:
        print(f"(no {title})")
        return
    for a in items:
        desc = (a.description or "").strip() if hasattr(a, "description") else ""
        print(f"- {a.name}")
        if desc:
            print(f"    {desc}")
        if a.forms:
            print(f"    forms: {len(a.forms)}")
        if getattr(a, "json_schema", None):
            print("    schema: yes")


def _print_forms(affordance) -> None:
    if not affordance.forms:
        print("(no forms)")
        return
    for i, f in enumerate(affordance.forms, start=1):
        ops = sorted(f.operation_types) if f.operation_types else []
        ops_txt = ", ".join(ops) if ops else "none"
        print(f"- form[{i}]")
        print(f"    target: {f.target}")
        if f.method_name:
            print(f"    method: {f.method_name}")
        if f.content_type:
            print(f"    content-type: {f.content_type}")
        print(f"    ops: {ops_txt}")
        if f.subprotocol:
            print(f"    subprotocol: {f.subprotocol}")
        if f.json_schema:
            print("    schema: yes")


def repl(td: ThingDescription, headers: Dict[str, str], timeout: float) -> None:
    print(HELP)
    props = {p.name: p for p in td.properties}
    actions = {a.name: a for a in td.actions}
    events = {e.name: e for e in td.events}

    while True:
        line = input("wot> ").strip()
        if not line:
            continue
        parts = shlex.split(line)
        cmd = parts[0].lower()
        rest = parts[1:]

        try:
            if cmd in ("quit", "exit"):
                return

            if cmd == "help":
                print(HELP)
                continue

            if cmd == "info":
                print(f"id: {td.id}")
                print(f"base: {td.base_uri}")
                print(f"properties: {len(td.properties)}")
                print(f"actions: {len(td.actions)}")
                print(f"events: {len(td.events)}")
                continue

            if cmd == "properties":
                _print_affordances("properties", td.properties)
                continue

            if cmd == "actions":
                _print_affordances("actions", td.actions)
                continue

            if cmd == "events":
                _print_affordances("events", td.events)
                continue

            if cmd == "forms":
                if len(rest) < 2:
                    print("Usage: forms <type> <name>  (type: property|action|event)")
                    continue
                kind = rest[0].lower()
                name = rest[1]
                if kind == "property":
                    aff = props.get(name)
                elif kind == "action":
                    aff = actions.get(name)
                elif kind == "event":
                    aff = events.get(name)
                else:
                    print("Unknown type. Use property|action|event.")
                    continue
                if not aff:
                    print(f"Not found: {name}")
                    continue
                _print_forms(aff)
                continue

            if cmd == "read":
                if not rest:
                    print("Usage: read <property>")
                    continue
                name = rest[0]
                prop = props.get(name)
                if not prop:
                    print(f"Property not found: {name}")
                    continue
                form = _select_form(prop, "readproperty")
                if not form:
                    print("No form available for readproperty")
                    continue
                target = _resolve_target(td, form.target)
                method = form.get_method_name("readproperty") or _default_method("readproperty")
                req_headers = {"Accept": form.content_type or "application/json", **headers}
                status, resp_headers, data = _http_request(
                    method, target, req_headers, None, timeout
                )
                _print_response(status, resp_headers, data)
                continue

            if cmd == "write":
                if not rest:
                    print("Usage: write [-p] <property> [jsonValue]")
                    continue
                prompt_mode = False
                if rest[0] == "-p":
                    prompt_mode = True
                    rest = rest[1:]
                if not rest:
                    print("Usage: write [-p] <property> [jsonValue]")
                    continue
                name = rest[0]
                prop = props.get(name)
                if not prop:
                    print(f"Property not found: {name}")
                    continue
                if prompt_mode:
                    payload = _prompt_for_schema(prop.json_schema, f"{name} value")
                else:
                    payload_text = " ".join(rest[1:])
                    payload = _parse_json_value(payload_text)
                form = _select_form(prop, "writeproperty")
                if not form:
                    print("No form available for writeproperty")
                    continue
                target = _resolve_target(td, form.target)
                method = form.get_method_name("writeproperty") or _default_method("writeproperty")
                body = None
                if payload is not None:
                    body = json.dumps(payload).encode("utf-8")
                req_headers = {
                    "Accept": form.content_type or "application/json",
                    "Content-Type": form.content_type or "application/json",
                    **headers,
                }
                status, resp_headers, data = _http_request(
                    method, target, req_headers, body, timeout
                )
                _print_response(status, resp_headers, data)
                continue

            if cmd == "invoke":
                if not rest:
                    print("Usage: invoke [-p] <action> [jsonArgs]")
                    continue
                prompt_mode = False
                if rest[0] == "-p":
                    prompt_mode = True
                    rest = rest[1:]
                if not rest:
                    print("Usage: invoke [-p] <action> [jsonArgs]")
                    continue
                name = rest[0]
                action = actions.get(name)
                if not action:
                    print(f"Action not found: {name}")
                    continue
                if prompt_mode:
                    payload = _prompt_for_schema(action.json_schema, f"{name} input")
                else:
                    payload_text = " ".join(rest[1:])
                    payload = _parse_json_value(payload_text)
                form = _select_form(action, "invokeaction")
                if not form:
                    print("No form available for invokeaction")
                    continue
                target = _resolve_target(td, form.target)
                method = form.get_method_name("invokeaction") or _default_method("invokeaction")
                body = None
                if payload is not None:
                    body = json.dumps(payload).encode("utf-8")
                req_headers = {
                    "Accept": form.content_type or "application/json",
                    "Content-Type": form.content_type or "application/json",
                    **headers,
                }
                status, resp_headers, data = _http_request(
                    method, target, req_headers, body, timeout
                )
                _print_response(status, resp_headers, data)
                continue

            print(f"Unknown command: {cmd} (type 'help')")

        except json.JSONDecodeError as e:
            print(f"Invalid JSON: {e}")
        except HTTPError as e:
            data = e.read().decode("utf-8", errors="replace") if e.fp else ""
            print(f"HTTPError: {e.code} {e.reason}")
            if data:
                print(data)
        except URLError as e:
            print(f"URLError: {e.reason}")
        except Exception as e:
            print(f"Error: {type(e).__name__}: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="WoT Thing consumer CLI")
    parser.add_argument("td", help="Thing Description URL or file path")
    parser.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        help='Extra HTTP header, repeatable (e.g. -H "Authorization: Bearer TOKEN")',
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout seconds")
    args = parser.parse_args()

    headers: Dict[str, str] = {}
    for h in args.header:
        if ":" not in h:
            raise SystemExit(f"Bad header {h!r}; expected 'Name: value'")
        k, v = h.split(":", 1)
        headers[k.strip()] = v.strip()

    td = _read_td(args.td)
    repl(td, headers, args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
