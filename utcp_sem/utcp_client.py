#!/usr/bin/env python3
"""
utcp_client.py — tiny UTCP manual client

Usage:
  python utcp_sem/utcp_client.py <manual_url>

Optional headers (repeatable):
  python utcp_sem/utcp_client.py <manual_url> -H "Authorization: Bearer TOKEN"
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utcp.data.utcp_manual import UtcpManual


HELP = """Commands:
  help
  info
  tools
  show <tool_name>
  call [-p] <tool_name> [jsonArgs]
  quit | exit

Examples:
  tools
  show multiply
  call multiply {"a":2,"b":5}
  call -p multiply
"""


def _parse_json_args(s: str) -> Dict[str, Any]:
    s = s.strip()
    if not s:
        return {}
    obj = json.loads(s)
    if not isinstance(obj, dict):
        raise ValueError('tool arguments must be a JSON object (e.g. {"a":1})')
    return obj


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


def _prompt_for_arguments(tool: dict) -> Dict[str, Any]:
    schema = tool.get("inputs") if isinstance(tool, dict) else None
    props, req_set = _schema_props(schema)

    if not props:
        print("(tool has no declared params; calling with {})")
        return {}

    args: Dict[str, Any] = {}
    print(f"Prompting for parameters of tool: {tool.get('name')}")
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


def _resolve_env_value(value: str) -> Optional[str]:
    if not isinstance(value, str):
        return None
    if value.startswith("${") and value.endswith("}"):
        env_name = value[2:-1]
        return os.getenv(env_name)
    return value


def _apply_auth(headers: Dict[str, str], url: str, auth: dict) -> str:
    auth_type = str(auth.get("auth_type", "")).lower()
    location = str(auth.get("location", "header")).lower()
    var_name = auth.get("var_name")
    raw_key = auth.get("api_key") or auth.get("token") or auth.get("value")
    value = _resolve_env_value(raw_key) if raw_key is not None else None
    if not value:
        return url

    if auth_type in {"bearer", "oauth2", "token"} and not var_name:
        var_name = "Authorization"
        value = f"Bearer {value}"

    if location == "header":
        if var_name:
            headers[var_name] = value
        return url

    if location == "query":
        if not var_name:
            return url
        parsed = urlparse(url)
        query = parsed.query
        params = dict([kv.split("=", 1) for kv in query.split("&") if kv] if query else [])
        params[var_name] = value
        new_query = urlencode(params)
        return urlunparse(parsed._replace(query=new_query))

    return url


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


def _manual_to_dict(manual: Any) -> dict:
    if isinstance(manual, dict):
        return manual
    if hasattr(manual, "model_dump"):
        return manual.model_dump()
    if hasattr(manual, "dict"):
        return manual.dict()
    if hasattr(manual, "to_dict"):
        return manual.to_dict()
    return {}


def _load_manual(url: str) -> Any:
    try:
        with urlopen(Request(url)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise SystemExit(f"Failed to fetch manual: {e}") from e

    # Best-effort parsing via utcp library
    try:
        if hasattr(UtcpManual, "model_validate"):
            return UtcpManual.model_validate(data)
        if hasattr(UtcpManual, "parse_obj"):
            return UtcpManual.parse_obj(data)
        from_dict = getattr(UtcpManual, "from_dict", None)
        if callable(from_dict):
            return from_dict(data)
        return UtcpManual(**data)
    except Exception:
        return data


def _find_tool(manual_dict: dict, tool_name: str) -> Optional[dict]:
    tools = manual_dict.get("tools")
    if not isinstance(tools, list):
        return None
    for t in tools:
        if t.get("name") == tool_name or t.get("id") == tool_name:
            return t
    return None


def _print_tool_list(manual_dict: dict) -> None:
    tools = manual_dict.get("tools")
    if not tools:
        print("(no tools)")
        return
    for t in tools:
        desc = (t.get("description") or "").strip()
        print(f"- {t.get('name')}")
        if desc:
            print(f"    {desc}")
        schema = t.get("inputs")
        props, req_set = _schema_props(schema)
        if not props:
            print("    params: (none)")
            continue
        print("    params:")
        for pname, pschema in props.items():
            if not isinstance(pschema, dict):
                pschema = {}
            print(_format_param_line(pname, pschema, pname in req_set))


def _print_tool_details(tool: dict) -> None:
    print(json.dumps(tool, indent=2))


def _call_tool(tool: dict, args: Dict[str, Any], headers: Dict[str, str], timeout: float) -> None:
    template = tool.get("tool_call_template") or {}
    if template.get("call_template_type") not in (None, "http"):
        print(f"Unsupported call_template_type: {template.get('call_template_type')}")
        return

    url = template.get("url")
    if not url:
        print("Tool has no URL in tool_call_template")
        return
    method = (template.get("http_method") or "POST").upper()
    content_type = template.get("content_type") or "application/json"

    req_headers = dict(template.get("headers") or {})
    req_headers.update(headers)

    auth = template.get("auth") or {}
    url = _apply_auth(req_headers, url, auth) if isinstance(auth, dict) else url

    body = None
    if method in {"GET", "HEAD"}:
        if args:
            parsed = urlparse(url)
            query = parsed.query
            params = dict([kv.split("=", 1) for kv in query.split("&") if kv] if query else [])
            params.update(
                {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in args.items()}
            )
            new_query = urlencode(params)
            url = urlunparse(parsed._replace(query=new_query))
    else:
        body = json.dumps(args).encode("utf-8") if args else None
        req_headers.setdefault("Content-Type", content_type)
    req_headers.setdefault("Accept", content_type)

    status, resp_headers, data = _http_request(method, url, req_headers, body, timeout)
    _print_response(status, resp_headers, data)


def repl(manual: Any, headers: Dict[str, str], timeout: float) -> None:
    manual_dict = _manual_to_dict(manual)
    print(HELP)
    while True:
        line = input("utcp> ").strip()
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
                print(f"utcp_version: {manual_dict.get('utcp_version')}")
                print(f"manual_version: {manual_dict.get('manual_version')}")
                tools = manual_dict.get("tools") or []
                print(f"tools: {len(tools)}")
                continue
            if cmd == "tools":
                _print_tool_list(manual_dict)
                continue
            if cmd == "show":
                if not rest:
                    print("Usage: show <tool_name>")
                    continue
                tool = _find_tool(manual_dict, rest[0])
                if not tool:
                    print(f"Tool not found: {rest[0]}")
                    continue
                _print_tool_details(tool)
                continue
            if cmd == "call":
                if not rest:
                    print("Usage: call [-p] <tool_name> [jsonArgs]")
                    continue
                prompt_mode = False
                if rest[0] == "-p":
                    prompt_mode = True
                    rest = rest[1:]
                if not rest:
                    print("Usage: call [-p] <tool_name> [jsonArgs]")
                    continue
                tool_name = rest[0]
                tool = _find_tool(manual_dict, tool_name)
                if not tool:
                    print(f"Tool not found: {tool_name}")
                    continue
                if prompt_mode:
                    args = _prompt_for_arguments(tool)
                else:
                    json_args = " ".join(rest[1:]) if len(rest) > 1 else ""
                    args = _parse_json_args(json_args)
                _call_tool(tool, args, headers, timeout)
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
    parser = argparse.ArgumentParser(description="UTCP manual client")
    parser.add_argument("url", help="UTCP manual URL (JSON)")
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

    manual = _load_manual(args.url)
    repl(manual, headers, args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
