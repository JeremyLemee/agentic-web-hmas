import inspect
import json
import socket
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os
import re
import time
from typing import Annotated, Any, Dict, List, Tuple
from urllib.error import URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from rdflib import BNode, Graph, Literal, RDF, RDFS, URIRef

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from signifier import HMAS, HCTL, HTTP, JS, TD

DEFAULT_SEM_BASE_URL = "http://localhost:5000"
SEM_BASE_URL = os.getenv("SEM_BASE_URL", DEFAULT_SEM_BASE_URL).rstrip("/")
SEM_HTTP_TIMEOUT_SECONDS = float(os.getenv("SEM_HTTP_TIMEOUT_SECONDS", "30"))
SEM_HTTP_RETRY_ATTEMPTS = max(int(os.getenv("SEM_HTTP_RETRY_ATTEMPTS", "2")), 1)
SEM_HTTP_RETRY_BACKOFF_SECONDS = float(os.getenv("SEM_HTTP_RETRY_BACKOFF_SECONDS", "1.5"))

mcp = FastMCP(name="SEM MCP", host="0.0.0.0", port=8200)
see_all_signifiers = True

# Cache already-registered signifiers to avoid re-adding duplicate tools.
_registered_signifiers: dict[str, str] = {}


def _slugify(value: str) -> str:
    """Convert an arbitrary string to a safe tool identifier suffix."""
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_")
    return slug or "signifier"


def _python_type_from_json_schema(schema: Dict[str, Any]) -> Any:
    """Best-effort mapping from JSON Schema primitive types to Python types."""
    json_type = schema.get("type")
    if isinstance(json_type, list):
        json_type = next((t for t in json_type if t != "null"), json_type[0] if json_type else None)

    return {
        "string": str,
        "number": float,
        "integer": int,
        "boolean": bool,
        "array": list,
        "object": dict,
        "null": type(None),
    }.get(json_type, Any)


def _annotation_from_json_schema(schema: Dict[str, Any]) -> Any:
    """
    Build a Python annotation for a tool parameter, preserving JSON schema descriptions
    so MCP exposes them in the generated tool parameter docs.
    """
    base_type = _python_type_from_json_schema(schema)
    description = schema.get("description") if isinstance(schema, dict) else None
    if isinstance(description, str):
        description = description.strip()
    if description:
        return Annotated[base_type, Field(description=description)]
    return base_type


def _collect_input_fields(
    schema: Dict[str, Any], path: Tuple[str, ...] = (), required: bool = True
):
    """
    Return a list of (path_parts, leaf_schema, required) for all non-const leaves
    we expect callers to provide.
    """
    if not isinstance(schema, dict):
        return []
    if "const" in schema:
        return []

    json_type = schema.get("type")
    if json_type == "object":
        props = schema.get("properties", {}) or {}
        reqs = set(schema.get("required", []))
        leaves = []
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
            # Flatten single-item arrays by exposing the first element.
            return _collect_input_fields(items_schema, path + ("0",), required)
        # Complex arrays are treated as a single leaf parameter.

    return [(path, schema, required)]


def _param_name(path: Tuple[str, ...]) -> str:
    """Convert a path tuple into a safe parameter name."""
    return "__".join(path) if path else "arguments"


def _assign_nested(root: Any, path: Tuple[str, ...], value: Any) -> Any:
    """Insert value into root following the given path, creating dicts/lists as needed."""
    if not path:
        return value
    head, *rest = path
    is_index = head.isdigit()
    if is_index:
        idx = int(head)
        arr = root if isinstance(root, list) else []
        while len(arr) <= idx:
            arr.append({})
        arr[idx] = _assign_nested(arr[idx], tuple(rest), value)
        return arr

    obj = root if isinstance(root, dict) else {}
    obj[head] = _assign_nested(obj.get(head, {}), tuple(rest), value)
    return obj


def _build_payload_from_arguments(field_specs, arguments: Dict[str, Any]) -> Any:
    """Rebuild a nested payload value from flattened tool arguments."""
    payload: Any = {}
    for path, leaf_schema, _ in field_specs:
        name = _param_name(path)
        if name not in arguments:
            continue
        value = arguments[name]
        value = _coerce_value_for_schema(value, leaf_schema, name)
        payload = _assign_nested(payload, path, value)
    return payload


def _is_schema_less_json_field(schema: Dict[str, Any]) -> bool:
    """
    A field with no explicit JSON schema typing should accept any JSON value.
    """
    if not isinstance(schema, dict):
        return True
    if "const" in schema:
        return False
    typed_keys = {
        "type",
        "properties",
        "items",
        "enum",
        "oneOf",
        "anyOf",
        "allOf",
        "$ref",
    }
    return not any(key in schema for key in typed_keys)


def _coerce_any_json_value(value: Any, param_name: str) -> Any:
    """
    Accept any JSON value. If provided as a string, parse it as JSON and fail clearly on invalid JSON.
    """
    if isinstance(value, str):
        raw = value.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON for parameter '{param_name}': {exc}") from exc
    if isinstance(value, (dict, list, int, float, bool)) or value is None:
        return value
    raise ValueError(
        f"Invalid JSON for parameter '{param_name}': unsupported type {type(value).__name__}"
    )


def _coerce_value_for_schema(value: Any, schema: Dict[str, Any], param_name: str) -> Any:
    """
    Coerce values to match JSON schema field types when callers provide JSON as strings.
    """
    if _is_schema_less_json_field(schema):
        return _coerce_any_json_value(value, param_name)

    json_type = schema.get("type") if isinstance(schema, dict) else None
    if isinstance(json_type, list):
        json_type = next((t for t in json_type if t != "null"), json_type[0] if json_type else None)

    if json_type == "array":
        if isinstance(value, str):
            raw = value.strip()
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON array for parameter '{param_name}': {exc}"
                ) from exc
        if not isinstance(value, list):
            raise ValueError(
                f"Invalid value for parameter '{param_name}': expected a JSON array"
            )
        return value

    if json_type == "object":
        if isinstance(value, str):
            raw = value.strip()
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON object for parameter '{param_name}': {exc}"
                ) from exc
        if not isinstance(value, dict):
            raise ValueError(
                f"Invalid value for parameter '{param_name}': expected a JSON object"
            )
        return value

    return value


def _extract_const_structure(schema: Dict[str, Any]) -> Any:
    """Return a nested structure containing only const/default values from a JSON schema."""
    if not isinstance(schema, dict):
        return None
    if "const" in schema:
        return schema["const"]

    json_type = schema.get("type")
    if json_type == "object":
        props = schema.get("properties", {})
        collected: Dict[str, Any] = {}
        for name, prop_schema in props.items():
            value = _extract_const_structure(prop_schema if isinstance(prop_schema, dict) else {})
            if value is not None:
                collected[name] = value
        return collected or None
    return None


def _deep_merge(base: Any, override: Any) -> Any:
    """Recursively merge dict-like structures, letting override values win."""
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for k, v in override.items():
            merged[k] = _deep_merge(merged.get(k), v)
        return merged
    return override if override is not None else base


def _merge_const_defaults(schema: Dict[str, Any], data: Any) -> Any:
    """
    Merge const/default values from schema into the provided data so callers
    don't need to supply them explicitly.
    """
    defaults = _extract_const_structure(schema) if isinstance(schema, dict) else None
    return _deep_merge(defaults or {}, data or {})


def _signature_from_json_schema(schema: Dict[str, Any]) -> inspect.Signature:
    """
    Construct an inspect.Signature from a JSON schema describing an object.
    This lets FastMCP derive proper tool parameters even if we cannot pass a schema kwarg.
    """
    fields = _collect_input_fields(schema)
    if fields:
        # Python function signatures require non-default params before default params.
        # Keep relative order within each group for stability.
        fields = sorted(fields, key=lambda item: (not item[2]))
        parameters: List[inspect.Parameter] = []
        for path, prop_schema, required in fields:
            name = _param_name(path)
            annotation = _annotation_from_json_schema(
                prop_schema if isinstance(prop_schema, dict) else {}
            )
            default = inspect._empty if required else None
            parameters.append(
                inspect.Parameter(
                    name,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=default,
                    annotation=annotation,
                )
            )
        return inspect.Signature(parameters=parameters)

    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = set(schema.get("required", [])) if isinstance(schema, dict) else set()
    parameters: List[inspect.Parameter] = []
    prop_items = list(props.items())
    # As above, ensure required fields come first to avoid invalid signatures.
    prop_items.sort(key=lambda item: (item[0] not in required))

    for name, prop_schema in prop_items:
        if isinstance(prop_schema, dict) and "const" in prop_schema:
            continue
        annotation = _annotation_from_json_schema(
            prop_schema if isinstance(prop_schema, dict) else {}
        )
        default = inspect._empty if name in required else None
        parameters.append(
            inspect.Parameter(
                name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=annotation,
            )
        )

    # If no explicit properties are present, accept arbitrary keyword arguments.
    if not parameters:
        parameters.append(inspect.Parameter("arguments", inspect.Parameter.VAR_KEYWORD))

    return inspect.Signature(parameters=parameters)


def _parse_rdf_list(graph: Graph, head: URIRef | BNode | None) -> List[Any]:
    items: List[Any] = []
    while head and head != RDF.nil:
        first = graph.value(head, RDF.first)
        if first is not None:
            items.append(first)
        head = graph.value(head, RDF.rest)
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
    """
    Read hctl:hasTarget from a form.
    Preferred representation is URIRef; Literal is tolerated for backward compatibility.
    """
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
    """Reconstruct a JSON schema dictionary from the RDF form produced in utils.get_schema_from_tool_input."""
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
        if t in reverse_type_map:
            schema["type"] = reverse_type_map[t]

    for predicate, obj in graph.predicate_objects(node):
        if predicate == RDF.type:
            continue
        # Properties are stored as separate nodes with js:propertyName (sometimes inside RDF lists).
        if predicate == JS["properties"]:
            list_head = (
                obj if isinstance(obj, (URIRef, BNode)) and graph.value(obj, RDF.first) else None
            )
            prop_nodes = _parse_rdf_list(graph, list_head) if list_head else [obj]
            for prop_node in prop_nodes:
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
            # Some keys may have multiple values (e.g., items in arrays); normalize to list when needed.
            if key in schema and isinstance(schema[key], list):
                schema[key].append(value)
            elif key in schema and schema[key] != value:
                schema[key] = [schema[key], value]
            else:
                schema[key] = value
        else:
            literal_value: Any = obj.toPython() if hasattr(obj, "toPython") else obj
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
        if isinstance(payload, bytes):
            body = payload
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = str(payload).encode("utf-8")
    else:
        body = json.dumps(payload).encode("utf-8")

    req = Request(target, data=body, headers=request_headers, method=method)
    try:
        with urlopen(req, timeout=SEM_HTTP_TIMEOUT_SECONDS) as resp:
            resp_body = resp.read().decode("utf-8")
            return {
                "status": resp.status,
                "headers": dict(resp.headers),
                "body": resp_body,
            }
    except Exception as exc:
        return {"error": str(exc)}


def _register_signifier_tool(graph: Graph, signifier_uri: URIRef) -> str | None:
    behavior = graph.value(signifier_uri, HMAS["signifies"])
    if behavior is None:
        return None

    form = graph.value(behavior, TD["hasForm"]) or graph.value(behavior, HMAS["hasForm"])
    if form is None:
        return None

    target_url = _extract_target_url(graph, form)
    if target_url is None:
        return None

    content_type = str(graph.value(form, HCTL["forContentType"]) or "application/json")
    is_text_content_type = content_type.lower().startswith("text/")
    headers = _headers_from_graph(graph, graph.value(form, HTTP["headers"]))
    http_method = str(
        graph.value(form, HTTP["methodName"]) or graph.value(form, HCTL["hasMethodName"]) or "POST"
    )

    # Prefer affordance-level schema on the node referenced by hmas:signifies.
    # Keep form-level and HMAS expects fallbacks for backward compatibility.
    schema_node = graph.value(behavior, TD["hasInputSchema"])
    if schema_node is None:
        schema_node = graph.value(form, TD["hasInputSchema"])
    if schema_node is None:
        schema_node = graph.value(behavior, HMAS["hasInputSchema"])
    if schema_node is None:
        schema_node = graph.value(form, HMAS["hasInputSchema"])
    if schema_node is None:
        expects = graph.value(behavior, HMAS["expects"]) or graph.value(form, HMAS["expects"])
        schema_node = graph.value(expects, HMAS["hasSchema"]) if expects else None
    if schema_node is None:
        return None

    payload_schema = _json_schema_from_rdf(graph, schema_node)
    payload_props = payload_schema.get("properties", {}) if isinstance(payload_schema, dict) else {}
    has_jsonrpc_envelope = any(key in payload_props for key in ("params", "jsonrpc", "method"))

    if has_jsonrpc_envelope:
        params_schema = payload_props.get("params", {"type": "object"})
        params_props = (
            params_schema.get("properties", {}) if isinstance(params_schema, dict) else {}
        )

        name_schema = params_props.get("name", {}) if isinstance(params_props, dict) else {}
        arguments_schema = params_props.get("arguments") if isinstance(params_props, dict) else None
        is_mcp_style = arguments_schema is not None

        effective_arguments_schema = (
            arguments_schema if is_mcp_style else params_schema or {"type": "object"}
        )
        selected_schema = effective_arguments_schema or {"type": "object"}
        underlying_tool_name = name_schema.get("const") if isinstance(name_schema, dict) else None

        jsonrpc_schema = payload_props.get("jsonrpc", {}) if isinstance(payload_props, dict) else {}
        method_schema = payload_props.get("method", {}) if isinstance(payload_props, dict) else {}
        jsonrpc_version = jsonrpc_schema.get("const") if isinstance(jsonrpc_schema, dict) else None
        rpc_method_name = method_schema.get("const") if isinstance(method_schema, dict) else None
    else:
        params_schema = payload_schema if isinstance(payload_schema, dict) else {"type": "object"}
        is_mcp_style = False
        selected_schema = params_schema or {"type": "object"}
        underlying_tool_name = None
        jsonrpc_version = None
        rpc_method_name = None

    description = _extract_signifier_description(graph, signifier_uri)
    label_literal = graph.value(signifier_uri, RDFS.label)
    label_value = label_literal if label_literal is not None else None
    label_text = label_value.strip() if isinstance(label_value, str) else ""
    tool_label = label_text or underlying_tool_name or signifier_uri.rsplit("/", 1)[-1]
    mcp_tool_name = _slugify(tool_label)
    if is_text_content_type:
        # For text/* forms, tools should expose a direct textual body parameter.
        selected_schema = {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "Text body to send as-is in the HTTP request.",
                }
            },
            "required": ["body"],
        }

    input_fields = _collect_input_fields(selected_schema)
    no_input_params = not input_fields

    def handler(**arguments):
        try:
            user_payload = (
                _build_payload_from_arguments(input_fields, arguments) if input_fields else arguments
            )
        except ValueError as exc:
            return {"error": str(exc)}
        if is_text_content_type:
            body_text = user_payload.get("body") if isinstance(user_payload, dict) else None
            if not isinstance(body_text, str):
                return {"error": "Invalid text body: parameter 'body' must be a string"}
            return _perform_http_request(
                target_url, headers, content_type, body_text, method=http_method
            )

        if has_jsonrpc_envelope:
            params_payload: Dict[str, Any]
            if is_mcp_style:
                params_payload = {
                    "name": underlying_tool_name or tool_label,
                    "arguments": user_payload,
                }
            else:
                params_payload = user_payload

            params_payload = _merge_const_defaults(
                params_schema if isinstance(params_schema, dict) else {}, params_payload
            )

            # For A2A message/send, auto-fill messageId when callers omit it.
            effective_rpc_method = rpc_method_name or ("tools/call" if is_mcp_style else "message/send")
            if effective_rpc_method == "message/send" and isinstance(params_payload, dict):
                message = params_payload.get("message")
                if isinstance(message, dict) and "messageId" not in message:
                    message["messageId"] = str(uuid.uuid4())

            payload = {
                "jsonrpc": jsonrpc_version or "2.0",
                "id": int(time.time() * 1000),
                "method": effective_rpc_method,
                "params": params_payload,
            }
        else:
            payload = _merge_const_defaults(
                params_schema if isinstance(params_schema, dict) else {}, user_payload
            )

        return _perform_http_request(target_url, headers, content_type, payload, method=http_method)

    # Register the dynamically generated tool with the MCP server.
    tool_kwargs: Dict[str, Any] = {
        "name": mcp_tool_name,
        "description": description or f"Invoke signifier {signifier_uri}",
    }

    if no_input_params:
        # All inputs are constant in the schema, so expose a zero-arg tool.
        handler.__signature__ = inspect.Signature(parameters=[])
        decorator = mcp.tool(**tool_kwargs)
    else:
        # Make sure the handler advertises the concrete parameters rather than **arguments.
        handler.__signature__ = _signature_from_json_schema(selected_schema)
        decorator = None
        for schema_param in ("schema", "input_schema", "arguments_schema", "parameters_schema"):
            try:
                decorator = mcp.tool(**tool_kwargs, **{schema_param: selected_schema})
                break
            except TypeError as exc:
                # Keep trying other keyword names if this one is not supported.
                if "unexpected keyword argument" in str(exc):
                    continue
                raise

        if decorator is None:
            # Fallback: supply a synthetic signature so FastMCP can infer parameters (avoids single **arguments param).
            handler.__signature__ = _signature_from_json_schema(selected_schema)
            decorator = mcp.tool(**tool_kwargs)

    decorator(handler)
    return mcp_tool_name


def _fetch_signifiers(profile_url: str) -> Tuple[Graph, List[URIRef]]:
    def _fetch_signifiers_by_url(url: str) -> Tuple[Graph, List[URIRef]]:
        req = Request(url, headers={"Accept": "application/ld+json"})
        with urlopen(req, timeout=SEM_HTTP_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")

        graph = Graph()
        graph.parse(data=body, format="json-ld", publicID=url)
        signifiers = list(graph.subjects(RDF.type, HMAS["Signifier"]))
        return graph, signifiers

    def _is_timeout_error(exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return True
        if isinstance(exc, URLError):
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                return True
        return "timed out" in str(exc).lower()

    query = urlencode({"profile": profile_url})
    filtered_url = f"{SEM_BASE_URL}/signifiers?{query}"

    last_error: Exception | None = None
    for attempt in range(1, SEM_HTTP_RETRY_ATTEMPTS + 1):
        try:
            return _fetch_signifiers_by_url(filtered_url)
        except Exception as exc:
            last_error = exc
            if not _is_timeout_error(exc) or attempt >= SEM_HTTP_RETRY_ATTEMPTS:
                break
            time.sleep(SEM_HTTP_RETRY_BACKOFF_SECONDS * attempt)

    if last_error is not None and _is_timeout_error(last_error):
        fallback_url = f"{SEM_BASE_URL}/signifiers"
        try:
            return _fetch_signifiers_by_url(fallback_url)
        except Exception as fallback_exc:
            raise RuntimeError(
                "Timed out reading filtered signifiers and fallback read failed "
                f"(filtered error: {last_error}; fallback error: {fallback_exc})"
            ) from fallback_exc

    if last_error is not None:
        raise last_error

    raise RuntimeError("Unexpected error while reading signifiers")


def _fetch_all_signifiers() -> Tuple[Graph, List[URIRef]]:
    list_url = f"{SEM_BASE_URL}/signifiers/list"
    req = Request(list_url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=SEM_HTTP_TIMEOUT_SECONDS) as resp:
        body = resp.read().decode("utf-8")
    payload = json.loads(body)
    signifier_urls = payload.get("signifiers", [])
    if not isinstance(signifier_urls, list):
        raise RuntimeError("Invalid /signifiers/list response: 'signifiers' must be a list")

    graph = Graph()
    signifiers: List[URIRef] = []
    for signifier_url in signifier_urls:
        if not isinstance(signifier_url, str) or not signifier_url.strip():
            continue
        signifier_url = signifier_url.strip()
        signifier_req = Request(signifier_url, headers={"Accept": "application/ld+json"})
        with urlopen(signifier_req, timeout=SEM_HTTP_TIMEOUT_SECONDS) as signifier_resp:
            signifier_body = signifier_resp.read().decode("utf-8")
        graph.parse(data=signifier_body, format="json-ld", publicID=signifier_url)
        signifiers.append(URIRef(signifier_url))

    return graph, signifiers


def _register_signifiers_from_graph(graph: Graph, signifiers: List[URIRef]) -> Dict[str, Any]:
    _reset_registered_tools()
    added_tools: List[str] = []
    for signifier_uri in signifiers:
        if str(signifier_uri) in _registered_signifiers:
            continue
        tool_name = _register_signifier_tool(graph, signifier_uri)
        if tool_name:
            _registered_signifiers[str(signifier_uri)] = tool_name
            added_tools.append(tool_name)

    return {
        "signifiers_seen": [str(s) for s in signifiers],
        "tools_added": added_tools,
    }


def _reset_registered_tools() -> None:
    keep_tools = {"register_profile", "read_signifiers", "update_profile", "all_signifiers"}
    for tool_name in list(_registered_signifiers.values()):
        if tool_name in keep_tools:
            continue
        try:
            mcp.remove_tool(tool_name)
        except Exception:
            pass
    _registered_signifiers.clear()

    tool_registry = getattr(mcp, "tools", None) or getattr(mcp, "_tools", None)
    if isinstance(tool_registry, dict):
        for tool_name in list(tool_registry.keys()):
            if tool_name in keep_tools:
                continue
            try:
                mcp.remove_tool(tool_name)
            except Exception:
                pass


@mcp.tool()
def register_profile(
    profile_id: Annotated[str, Field(description="Profile identifier to register (e.g., executor).")],
) -> Dict[str, Any]:
    """
    Register a profile with an empty natural-language context.
    """
    encoded_profile_id = quote(profile_id, safe="/")
    profile_uri = URIRef(f"{SEM_BASE_URL}/profile/{encoded_profile_id}")
    payload_graph = Graph()
    context_node = BNode()
    payload_graph.add((profile_uri, HMAS["hasContext"], context_node))
    payload_graph.add((context_node, RDFS["comment"], Literal("")))

    target = f"{SEM_BASE_URL}/profile/{encoded_profile_id}"
    return _perform_http_request(
        target,
        {},
        "text/turtle",
        payload_graph.serialize(format="turtle"),
        method="PUT",
    )


@mcp.tool()
def update_profile(
    profile_id: Annotated[str, Field(description="Profile identifier to update (e.g., executor).")],
    nl_context: Annotated[
        str, Field(description="Natural-language context to store for the profile.")
    ],
) -> Dict[str, Any]:
    """
    Update the natural-language context for the given profile.
    """
    encoded_profile_id = quote(profile_id, safe="/")
    target = f"{SEM_BASE_URL}/profile/{encoded_profile_id}/nl_context"
    payload = {"context": nl_context}
    return _perform_http_request(target, {}, "application/json", payload, method="PUT")


@mcp.tool()
def read_signifiers(
    profile_url: Annotated[str, Field(description="Full profile URL to query for signifiers.")],
) -> Dict[str, Any]:
    """
    Read signifiers relevant to the given profile URL and expose them as MCP tools.
    """
    try:
        graph, signifiers = _fetch_signifiers(profile_url)
    except Exception as exc:
        return {"error": f"Failed to read signifiers: {exc}"}

    return _register_signifiers_from_graph(graph, signifiers)


if see_all_signifiers:

    @mcp.tool()
    def all_signifiers() -> Dict[str, Any]:
        """
        Read all signifiers exposed by SEM and expose them as MCP tools.
        """
        try:
            graph, signifiers = _fetch_all_signifiers()
        except Exception as exc:
            return {"error": f"Failed to read all signifiers: {exc}"}
        return _register_signifiers_from_graph(graph, signifiers)


if __name__ == "__main__":
    # Streamable HTTP on one endpoint
    mcp.run(transport="streamable-http")
