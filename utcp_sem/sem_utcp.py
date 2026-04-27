#!/usr/bin/env python3
"""sem_utcp.py — HTTP server exposing SEM profiles and signifiers via a UTCP manual.

Mirrors the capabilities of mcp_sem/sem_mcp.py but over plain HTTP:
  - GET  /utcp               → UTCP manual (tool catalogue)
  - POST /tools/<tool_name>  → call any tool with a JSON payload of its parameters

Native tools (always present):
  register_profile, update_profile, read_signifiers, all_signifiers

Signifier tools are registered dynamically when read_signifiers / all_signifiers
is called and appear immediately in /utcp and as callable endpoints.

Environment variables:
  SEM_BASE_URL                  SEM Flask app base URL (default: http://localhost:5000)
  SEM_HTTP_TIMEOUT_SECONDS      HTTP timeout for SEM calls (default: 30)
  SEM_HTTP_RETRY_ATTEMPTS       Retry attempts for signifier fetching (default: 2)
  SEM_HTTP_RETRY_BACKOFF_SECONDS Backoff between retries (default: 1.5)
  SEM_UTCP_HOST                 Bind host for this server (default: 0.0.0.0)
  SEM_UTCP_PORT                 Bind port for this server (default: 8300)
  SEM_UTCP_PUBLIC_HOST          Host written into tool URLs in the manual (default: 127.0.0.1)
"""

import json
import os
import re
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple
from urllib.error import URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify, request as flask_request
from rdflib import BNode, Graph, Literal, RDF, RDFS, URIRef

from signifier import HMAS, HCTL, HTTP, JS, TD

# ─── Configuration ─────────────────────────────────────────────────────────────

DEFAULT_SEM_BASE_URL = "http://localhost:5000"
SEM_BASE_URL = os.getenv("SEM_BASE_URL", DEFAULT_SEM_BASE_URL).rstrip("/")
SEM_HTTP_TIMEOUT_SECONDS = float(os.getenv("SEM_HTTP_TIMEOUT_SECONDS", "30"))
SEM_HTTP_RETRY_ATTEMPTS = max(int(os.getenv("SEM_HTTP_RETRY_ATTEMPTS", "2")), 1)
SEM_HTTP_RETRY_BACKOFF_SECONDS = float(os.getenv("SEM_HTTP_RETRY_BACKOFF_SECONDS", "1.5"))

HOST = os.getenv("SEM_UTCP_HOST", "0.0.0.0")
PORT = int(os.getenv("SEM_UTCP_PORT", "8300"))
PUBLIC_HOST = os.getenv("SEM_UTCP_PUBLIC_HOST", "127.0.0.1")

UTCP_VERSION = "1.0.0"
MANUAL_VERSION = "1.0.0"

# ─── Tool registry ─────────────────────────────────────────────────────────────

# tool_name → {"description": str, "inputs": dict, "handler": Callable, "native": bool}
_tool_registry: Dict[str, Dict[str, Any]] = {}
_registered_signifiers: Dict[str, str] = {}  # signifier URI → tool name
_lock = threading.Lock()

app = Flask(__name__)


def _tool_url(name: str) -> str:
    return f"http://{PUBLIC_HOST}:{PORT}/tools/{name}"


def _register_tool(
    name: str,
    description: str,
    inputs: Dict[str, Any],
    handler: Callable[[Dict[str, Any]], Dict[str, Any]],
    native: bool = False,
) -> None:
    _tool_registry[name] = {
        "description": description,
        "inputs": inputs,
        "handler": handler,
        "native": native,
    }


def _reset_signifier_tools() -> None:
    """Remove all dynamically registered signifier tools; must be called under _lock."""
    for name in [n for n, e in _tool_registry.items() if not e["native"]]:
        del _tool_registry[name]
    _registered_signifiers.clear()


# ─── Shared utilities (adapted from mcp_sem/sem_mcp.py) ───────────────────────


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_")
    return slug or "signifier"


def _collect_input_fields(
    schema: Dict[str, Any], path: Tuple[str, ...] = (), required: bool = True
) -> List[Any]:
    if not isinstance(schema, dict):
        return []
    if "const" in schema:
        return []

    json_type = schema.get("type")
    if json_type == "object":
        props = schema.get("properties", {}) or {}
        reqs = set(schema.get("required", []))
        leaves: List[Any] = []
        for name, prop_schema in props.items():
            leaves.extend(
                _collect_input_fields(
                    prop_schema if isinstance(prop_schema, dict) else {},
                    path + (name,),
                    name in reqs,
                )
            )
        return leaves

    if json_type == "array":
        items_schema = schema.get("items")
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, str) and min_items.isdigit():
            min_items = int(min_items)
        if isinstance(max_items, str) and max_items.isdigit():
            max_items = int(max_items)
        if isinstance(items_schema, dict) and min_items == max_items == 1:
            return _collect_input_fields(items_schema, path + ("0",), required)

    return [(path, schema, required)]


def _param_name(path: Tuple[str, ...]) -> str:
    return "__".join(path) if path else "arguments"


def _assign_nested(root: Any, path: Tuple[str, ...], value: Any) -> Any:
    if not path:
        return value
    head, *rest = path
    if head.isdigit():
        idx = int(head)
        arr = root if isinstance(root, list) else []
        while len(arr) <= idx:
            arr.append({})
        arr[idx] = _assign_nested(arr[idx], tuple(rest), value)
        return arr
    obj = root if isinstance(root, dict) else {}
    obj[head] = _assign_nested(obj.get(head, {}), tuple(rest), value)
    return obj


def _build_payload_from_arguments(field_specs: List[Any], arguments: Dict[str, Any]) -> Any:
    payload: Any = {}
    for path, leaf_schema, _ in field_specs:
        name = _param_name(path)
        if name not in arguments:
            continue
        value = _coerce_value_for_schema(arguments[name], leaf_schema, name)
        payload = _assign_nested(payload, path, value)
    return payload


def _is_schema_less_json_field(schema: Dict[str, Any]) -> bool:
    if not isinstance(schema, dict):
        return True
    if "const" in schema:
        return False
    return not any(
        k in schema for k in {"type", "properties", "items", "enum", "oneOf", "anyOf", "allOf", "$ref"}
    )


def _coerce_any_json_value(value: Any, param_name: str) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value.strip())
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON for parameter '{param_name}': {exc}") from exc
    if isinstance(value, (dict, list, int, float, bool)) or value is None:
        return value
    raise ValueError(
        f"Invalid JSON for parameter '{param_name}': unsupported type {type(value).__name__}"
    )


def _coerce_value_for_schema(value: Any, schema: Dict[str, Any], param_name: str) -> Any:
    if _is_schema_less_json_field(schema):
        return _coerce_any_json_value(value, param_name)

    json_type = schema.get("type") if isinstance(schema, dict) else None
    if isinstance(json_type, list):
        json_type = next((t for t in json_type if t != "null"), json_type[0] if json_type else None)

    if json_type == "array":
        if isinstance(value, str):
            try:
                value = json.loads(value.strip())
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON array for parameter '{param_name}': {exc}") from exc
        if not isinstance(value, list):
            raise ValueError(f"Invalid value for parameter '{param_name}': expected a JSON array")
        return value

    if json_type == "object":
        if isinstance(value, str):
            try:
                value = json.loads(value.strip())
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON object for parameter '{param_name}': {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"Invalid value for parameter '{param_name}': expected a JSON object")
        return value

    return value


def _extract_const_structure(schema: Dict[str, Any]) -> Any:
    if not isinstance(schema, dict):
        return None
    if "const" in schema:
        return schema["const"]
    if schema.get("type") == "object":
        collected: Dict[str, Any] = {}
        for name, prop_schema in (schema.get("properties") or {}).items():
            v = _extract_const_structure(prop_schema if isinstance(prop_schema, dict) else {})
            if v is not None:
                collected[name] = v
        return collected or None
    return None


def _deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for k, v in override.items():
            merged[k] = _deep_merge(merged.get(k), v)
        return merged
    return override if override is not None else base


def _merge_const_defaults(schema: Dict[str, Any], data: Any) -> Any:
    defaults = _extract_const_structure(schema) if isinstance(schema, dict) else None
    return _deep_merge(defaults or {}, data or {})


def _strip_const_fields(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return schema

    if "const" in schema:
        return None

    stripped = dict(schema)
    json_type = stripped.get("type")

    if json_type == "object":
        properties = stripped.get("properties")
        if isinstance(properties, dict):
            new_properties: Dict[str, Any] = {}
            for name, prop_schema in properties.items():
                cleaned = _strip_const_fields(prop_schema)
                if cleaned is not None:
                    new_properties[name] = cleaned
            stripped["properties"] = new_properties

            required = stripped.get("required")
            if isinstance(required, list):
                stripped["required"] = [name for name in required if name in new_properties]
        return stripped

    if json_type == "array" and isinstance(stripped.get("items"), dict):
        stripped["items"] = _strip_const_fields(stripped["items"]) or {}

    for key in ("oneOf", "anyOf", "allOf"):
        variants = stripped.get(key)
        if isinstance(variants, list):
            stripped[key] = [
                cleaned for variant in variants if (cleaned := _strip_const_fields(variant)) is not None
            ]

    return stripped


def _parse_rdf_list(graph: Graph, head: URIRef | BNode | None) -> List[Any]:
    items: List[Any] = []
    current: Any = head
    while current and current != RDF.nil:
        first = graph.value(current, RDF.first)
        if first is not None:
            items.append(first)
        current = graph.value(current, RDF.rest)
    return items


def _headers_from_graph(graph: Graph, headers_node: URIRef | BNode | None) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for header_node in _parse_rdf_list(graph, headers_node):
        name = graph.value(header_node, HTTP["fieldName"])
        value = graph.value(header_node, HTTP["fieldValue"])
        if name and value:
            headers[str(name)] = str(value)
    return headers


def _extract_target_url(graph: Graph, form: URIRef | BNode) -> str | None:
    target = graph.value(form, HCTL["hasTarget"])
    if target is None:
        return None
    target_url = str(target).strip()
    if not target_url:
        return None
    parsed = urlparse(target_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return target_url


def _json_schema_from_rdf(graph: Graph, node: URIRef | BNode) -> Dict[str, Any]:
    reverse_type_map = {
        JS.ObjectSchema: "object",
        JS.ArraySchema: "array",
        JS.StringSchema: "string",
        JS.NumberSchema: "number",
        JS.IntegerSchema: "integer",
        JS.BooleanSchema: "boolean",
        JS.NullSchema: "null",
    }

    schema: Dict[str, Any] = {}
    for _, _, t in graph.triples((node, RDF.type, None)):
        schema_type = reverse_type_map.get(t)  # type: ignore[call-overload]
        if schema_type is not None:
            schema["type"] = schema_type

    for predicate, obj in graph.predicate_objects(node):
        if predicate == RDF.type:
            continue
        if predicate == JS["properties"]:
            list_head = (
                obj if isinstance(obj, (URIRef, BNode)) and graph.value(obj, RDF.first) else None
            )
            prop_nodes = _parse_rdf_list(graph, list_head) if list_head else [obj]
            for prop_node in prop_nodes:
                if not isinstance(prop_node, (URIRef, BNode)):
                    continue
                prop_schema = _json_schema_from_rdf(graph, prop_node)
                prop_name = graph.value(prop_node, JS["propertyName"])
                if prop_name:
                    schema.setdefault("properties", {})[str(prop_name)] = prop_schema
            continue
        if predicate == JS["propertyName"]:
            continue
        if predicate == RDFS["comment"]:
            schema["description"] = str(obj)
            continue
        if predicate == JS["description"]:
            schema["description"] = str(obj)
            continue

        key = str(predicate).split("#")[-1] if str(predicate).startswith(str(JS)) else None
        if key is None:
            continue

        if isinstance(obj, (URIRef, BNode)):
            if predicate == JS["required"] and graph.value(obj, RDF.first):
                for item in _parse_rdf_list(graph, obj):
                    schema.setdefault("required", []).append(str(item.toPython()))
                continue
            value = _json_schema_from_rdf(graph, obj)
            if key in schema and isinstance(schema[key], list):
                schema[key].append(value)
            elif key in schema and schema[key] != value:
                schema[key] = [schema[key], value]
            else:
                schema[key] = value
        else:
            literal_value: Any = obj.toPython() if hasattr(obj, "toPython") else obj  # type: ignore[union-attr]
            if key == "required":
                schema.setdefault("required", []).append(str(literal_value))
            else:
                if key == "const" and literal_value in (None, "None"):
                    continue
                if key in schema and isinstance(schema[key], list):
                    schema[key].append(literal_value)
                elif key in schema and schema[key] != literal_value:
                    schema[key] = [schema[key], literal_value]
                else:
                    schema[key] = literal_value

    return schema


def _extract_signifier_description(graph: Graph, signifier_uri: URIRef) -> str:
    comments: List[str] = []
    for ctx in graph.objects(signifier_uri, HMAS["recommendsContext"]):
        for comment in graph.objects(ctx, RDFS["comment"]):
            comments.append(str(comment))
    return " ".join(comments).strip()


def _perform_http_request(
    target: str,
    headers: Dict[str, str],
    content_type: str,
    payload: Any,
    method: str = "POST",
) -> Dict[str, Any]:
    request_headers = {"Content-Type": content_type}
    request_headers.update(headers)
    if content_type.lower().startswith("text/"):
        body = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
    else:
        body = json.dumps(payload).encode("utf-8")

    req = Request(target, data=body, headers=request_headers, method=method)
    try:
        with urlopen(req, timeout=SEM_HTTP_TIMEOUT_SECONDS) as resp:
            return {
                "status": resp.status,
                "headers": dict(resp.headers),
                "body": resp.read().decode("utf-8"),
            }
    except Exception as exc:
        return {"error": str(exc)}


# ─── Signifier fetching ────────────────────────────────────────────────────────


def _fetch_signifiers(profile_url: str) -> Tuple[Graph, List[URIRef]]:
    def _fetch_by_url(url: str) -> Tuple[Graph, List[URIRef]]:
        req = Request(url, headers={"Accept": "application/ld+json"})
        with urlopen(req, timeout=SEM_HTTP_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
        graph = Graph()
        graph.parse(data=body, format="json-ld", publicID=url)
        return graph, [s for s in graph.subjects(RDF.type, HMAS["Signifier"]) if isinstance(s, URIRef)]

    def _is_timeout(exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return True
        if isinstance(exc, URLError) and isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return True
        return "timed out" in str(exc).lower()

    query = urlencode({"profile": profile_url})
    filtered_url = f"{SEM_BASE_URL}/signifiers?{query}"

    last_error: Exception | None = None
    for attempt in range(1, SEM_HTTP_RETRY_ATTEMPTS + 1):
        try:
            return _fetch_by_url(filtered_url)
        except Exception as exc:
            last_error = exc
            if not _is_timeout(exc) or attempt >= SEM_HTTP_RETRY_ATTEMPTS:
                break
            time.sleep(SEM_HTTP_RETRY_BACKOFF_SECONDS * attempt)

    if last_error is not None and _is_timeout(last_error):
        try:
            return _fetch_by_url(f"{SEM_BASE_URL}/signifiers")
        except Exception as fallback_exc:
            raise RuntimeError(
                f"Timed out reading filtered signifiers and fallback failed "
                f"(filtered: {last_error}; fallback: {fallback_exc})"
            ) from fallback_exc

    if last_error is not None:
        raise last_error
    raise RuntimeError("Unexpected error while reading signifiers")


def _fetch_all_signifiers() -> Tuple[Graph, List[URIRef]]:
    req = Request(f"{SEM_BASE_URL}/signifiers/list", headers={"Accept": "application/json"})
    with urlopen(req, timeout=SEM_HTTP_TIMEOUT_SECONDS) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    signifier_urls = payload.get("signifiers", [])
    if not isinstance(signifier_urls, list):
        raise RuntimeError("Invalid /signifiers/list response: 'signifiers' must be a list")

    graph = Graph()
    signifiers: List[URIRef] = []
    for url in signifier_urls:
        if not isinstance(url, str) or not url.strip():
            continue
        url = url.strip()
        req = Request(url, headers={"Accept": "application/ld+json"})
        with urlopen(req, timeout=SEM_HTTP_TIMEOUT_SECONDS) as resp:
            graph.parse(data=resp.read().decode("utf-8"), format="json-ld", publicID=url)
        signifiers.append(URIRef(url))

    return graph, signifiers


# ─── Signifier → HTTP tool registration ───────────────────────────────────────


def _register_signifier_as_http_tool(graph: Graph, signifier_uri: URIRef) -> str | None:
    """Extract a signifier from the RDF graph and register it as an HTTP-callable tool."""
    behavior = graph.value(signifier_uri, HMAS["signifies"])
    if behavior is None:
        return None

    form_node = graph.value(behavior, TD["hasForm"]) or graph.value(behavior, HMAS["hasForm"])
    if not isinstance(form_node, (URIRef, BNode)):
        return None
    form: URIRef | BNode = form_node

    target_url = _extract_target_url(graph, form)
    if target_url is None:
        return None

    content_type = str(graph.value(form, HCTL["forContentType"]) or "application/json")
    is_text = content_type.lower().startswith("text/")
    headers_node = graph.value(form, HTTP["headers"])
    headers = _headers_from_graph(
        graph, headers_node if isinstance(headers_node, (URIRef, BNode)) else None
    )
    http_method = str(
        graph.value(form, HTTP["methodName"]) or graph.value(form, HCTL["hasMethodName"]) or "POST"
    )

    # Locate input schema node (same fallback chain as sem_mcp.py).
    schema_node = (
        graph.value(behavior, TD["hasInputSchema"])
        or graph.value(form, TD["hasInputSchema"])
        or graph.value(behavior, HMAS["hasInputSchema"])
        or graph.value(form, HMAS["hasInputSchema"])
    )
    if schema_node is None:
        expects = graph.value(behavior, HMAS["expects"]) or graph.value(form, HMAS["expects"])
        schema_node = graph.value(expects, HMAS["hasSchema"]) if expects else None
    if not isinstance(schema_node, (URIRef, BNode)):
        return None

    payload_schema = _json_schema_from_rdf(graph, schema_node)
    payload_props = payload_schema.get("properties", {}) if isinstance(payload_schema, dict) else {}
    has_jsonrpc_envelope = any(k in payload_props for k in ("params", "jsonrpc", "method"))

    if has_jsonrpc_envelope:
        params_schema = payload_props.get("params", {"type": "object"})
        params_props = params_schema.get("properties", {}) if isinstance(params_schema, dict) else {}
        name_schema = params_props.get("name", {}) if isinstance(params_props, dict) else {}
        arguments_schema = params_props.get("arguments") if isinstance(params_props, dict) else None
        is_mcp_style = arguments_schema is not None
        selected_schema = arguments_schema if is_mcp_style else params_schema or {"type": "object"}
        underlying_tool_name = name_schema.get("const") if isinstance(name_schema, dict) else None
        jsonrpc_version = (
            payload_props.get("jsonrpc", {}).get("const")
            if isinstance(payload_props.get("jsonrpc"), dict)
            else None
        )
        rpc_method_name = (
            payload_props.get("method", {}).get("const")
            if isinstance(payload_props.get("method"), dict)
            else None
        )
    else:
        params_schema = payload_schema if isinstance(payload_schema, dict) else {"type": "object"}
        is_mcp_style = False
        selected_schema = params_schema or {"type": "object"}
        underlying_tool_name = None
        jsonrpc_version = None
        rpc_method_name = None

    description = _extract_signifier_description(graph, signifier_uri)
    label_literal = graph.value(signifier_uri, RDFS.label)
    label_text = str(label_literal).strip() if label_literal is not None else ""
    tool_label = label_text or underlying_tool_name or str(signifier_uri).rsplit("/", 1)[-1]
    tool_name = _slugify(tool_label)

    if is_text:
        selected_schema = {
            "type": "object",
            "properties": {"body": {"type": "string", "description": "Text body to send as-is."}},
            "required": ["body"],
        }

    selected_schema_dict: Dict[str, Any] = (
        selected_schema if isinstance(selected_schema, dict) else {"type": "object"}
    )
    effective_schema = _strip_const_fields(selected_schema_dict) or {"type": "object", "properties": {}}
    input_fields = _collect_input_fields(effective_schema)

    # Capture loop variables in the closure explicitly.
    _target_url = target_url
    _headers = headers
    _content_type = content_type
    _http_method = http_method
    _is_text = is_text
    _has_jsonrpc = has_jsonrpc_envelope
    _is_mcp_style = is_mcp_style
    _params_schema = params_schema
    _underlying_tool_name = underlying_tool_name
    _tool_label = tool_label
    _jsonrpc_version = jsonrpc_version
    _rpc_method_name = rpc_method_name

    def handler(arguments: Dict[str, Any]) -> Dict[str, Any]:
        try:
            user_payload = (
                _build_payload_from_arguments(input_fields, arguments) if input_fields else arguments
            )
        except ValueError as exc:
            return {"error": str(exc)}

        if _is_text:
            body_text = user_payload.get("body") if isinstance(user_payload, dict) else None
            if not isinstance(body_text, str):
                return {"error": "Invalid text body: parameter 'body' must be a string"}
            return _perform_http_request(
                _target_url, _headers, _content_type, body_text, method=_http_method
            )

        if _has_jsonrpc:
            if _is_mcp_style:
                params_payload: Dict[str, Any] = {
                    "name": _underlying_tool_name or _tool_label,
                    "arguments": user_payload,
                }
            else:
                params_payload = user_payload

            params_payload = _merge_const_defaults(_params_schema, params_payload)

            effective_method = _rpc_method_name or ("tools/call" if _is_mcp_style else "message/send")
            if effective_method == "message/send" and isinstance(params_payload, dict):
                message = params_payload.get("message")
                if isinstance(message, dict) and "messageId" not in message:
                    message["messageId"] = str(uuid.uuid4())

            payload = {
                "jsonrpc": _jsonrpc_version or "2.0",
                "id": int(time.time() * 1000),
                "method": effective_method,
                "params": params_payload,
            }
        else:
            payload = _merge_const_defaults(_params_schema, user_payload)

        return _perform_http_request(_target_url, _headers, _content_type, payload, method=_http_method)

    _register_tool(tool_name, description, effective_schema, handler, native=False)
    return tool_name


def _register_signifiers_from_graph(graph: Graph, signifiers: List[URIRef]) -> Dict[str, Any]:
    with _lock:
        _reset_signifier_tools()
        added_tools: List[str] = []
        for signifier_uri in signifiers:
            if str(signifier_uri) in _registered_signifiers:
                continue
            tool_name = _register_signifier_as_http_tool(graph, signifier_uri)
            if tool_name:
                _registered_signifiers[str(signifier_uri)] = tool_name
                added_tools.append(tool_name)

        available_tools = list(_tool_registry.keys())

    return {
        "signifiers_seen": [str(s) for s in signifiers],
        "tools_added": added_tools,
        "available_tools": available_tools,
    }


# ─── Native tool handlers ──────────────────────────────────────────────────────


def _handler_register_profile(arguments: Dict[str, Any]) -> Dict[str, Any]:
    profile_id = arguments.get("profile_id", "")
    encoded = quote(profile_id, safe="/")
    profile_uri = URIRef(f"{SEM_BASE_URL}/profile/{encoded}")
    g = Graph()
    ctx = BNode()
    g.add((profile_uri, HMAS["hasContext"], ctx))
    g.add((ctx, RDFS["comment"], Literal("")))
    return _perform_http_request(
        f"{SEM_BASE_URL}/profile/{encoded}", {}, "text/turtle", g.serialize(format="turtle"), method="PUT"
    )


def _handler_update_profile(arguments: Dict[str, Any]) -> Dict[str, Any]:
    profile_id = arguments.get("profile_id", "")
    nl_context = arguments.get("nl_context", "")
    encoded = quote(profile_id, safe="/")
    return _perform_http_request(
        f"{SEM_BASE_URL}/profile/{encoded}/nl_context",
        {},
        "application/json",
        {"context": nl_context},
        method="PUT",
    )


def _handler_read_signifiers(arguments: Dict[str, Any]) -> Dict[str, Any]:
    profile_url = arguments.get("profile_url", "")
    try:
        graph, signifiers = _fetch_signifiers(profile_url)
    except Exception as exc:
        return {"error": f"Failed to read signifiers: {exc}"}
    return _register_signifiers_from_graph(graph, signifiers)


def _handler_all_signifiers(_arguments: Dict[str, Any]) -> Dict[str, Any]:
    try:
        graph, signifiers = _fetch_all_signifiers()
    except Exception as exc:
        return {"error": f"Failed to read all signifiers: {exc}"}
    return _register_signifiers_from_graph(graph, signifiers)


# ─── Flask routes ──────────────────────────────────────────────────────────────


@app.route("/utcp", methods=["GET"])
def utcp_manual():
    """Return the UTCP manual listing all currently registered tools."""
    with _lock:
        tools = [
            {
                "name": name,
                "description": entry["description"],
                "inputs": entry["inputs"],
                "outputs": {},
                "tags": [],
                "tool_call_template": {
                    "call_template_type": "http",
                    "url": _tool_url(name),
                    "http_method": "POST",
                    "content_type": "application/json",
                },
            }
            for name, entry in _tool_registry.items()
        ]
    return jsonify({"utcp_version": UTCP_VERSION, "manual_version": MANUAL_VERSION, "tools": tools})


@app.route("/tools/<tool_name>", methods=["POST"])
def call_tool(tool_name: str):
    """Invoke a registered tool with a JSON payload of its parameters."""
    with _lock:
        entry = _tool_registry.get(tool_name)
    if entry is None:
        return jsonify({"error": f"Tool '{tool_name}' not found"}), 404

    arguments = flask_request.get_json(silent=True) or {}
    if not isinstance(arguments, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    try:
        result = entry["handler"](arguments)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify(result)


# ─── Bootstrap ─────────────────────────────────────────────────────────────────


def _bootstrap_native_tools() -> None:
    _register_tool(
        "register_profile",
        "Register a profile with an empty natural-language context.",
        {
            "type": "object",
            "properties": {
                "profile_id": {
                    "type": "string",
                    "description": "Profile identifier to register (e.g., executor).",
                }
            },
            "required": ["profile_id"],
        },
        _handler_register_profile,
        native=True,
    )
    _register_tool(
        "update_profile",
        "Update the natural-language context for the given profile.",
        {
            "type": "object",
            "properties": {
                "profile_id": {
                    "type": "string",
                    "description": "Profile identifier to update (e.g., executor).",
                },
                "nl_context": {
                    "type": "string",
                    "description": "Natural-language context to store for the profile.",
                },
            },
            "required": ["profile_id", "nl_context"],
        },
        _handler_update_profile,
        native=True,
    )
    _register_tool(
        "read_signifiers",
        "Read signifiers relevant to the given profile URL and expose them as HTTP tools.",
        {
            "type": "object",
            "properties": {
                "profile_url": {
                    "type": "string",
                    "description": "Full profile URL to query for signifiers.",
                }
            },
            "required": ["profile_url"],
        },
        _handler_read_signifiers,
        native=True,
    )
    _register_tool(
        "all_signifiers",
        "Read all signifiers exposed by SEM and expose them as HTTP tools.",
        {"type": "object", "properties": {}},
        _handler_all_signifiers,
        native=True,
    )


if __name__ == "__main__":
    _bootstrap_native_tools()
    app.run(host=HOST, port=PORT)
