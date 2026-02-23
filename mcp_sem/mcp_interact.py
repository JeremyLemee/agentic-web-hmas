import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sem_profile import Profile
from urllib.request import Request, urlopen

from rdflib import URIRef, Graph, Namespace, BNode, Literal, RDFS

from signifier import Signifier
from utils import (
    generate_artifact_url_from_id,
    generate_id,
    generate_signifier_url_from_id,
    create_rdf_list,
    get_schema_from_tool_input,
)

HMAS = Namespace("https://purl.org/hmas/")

HCTL = Namespace("https://www.w3.org/2019/wot/hypermedia#")

HTTP = Namespace("http://www.w3.org/2011/http#")

RDF = Namespace("http://www.w3.org/1999/02/22-rdf-syntax-ns#")

JS = Namespace("https://www.w3.org/2019/wot/json-schema#")
TD = Namespace("https://www.w3.org/2019/wot/td#")


def _add_schema_comment(
    graph: Graph, property_name: str, comment: str, fallback_node: BNode | None = None
) -> None:
    if not comment:
        return
    target_nodes = list(graph.subjects(JS["propertyName"], Literal(property_name)))
    if not target_nodes and fallback_node is not None:
        target_nodes = [fallback_node]
    for node in target_nodes:
        graph.add((node, RDFS["comment"], Literal(comment)))


def _iter_json_messages(stream_text: str):
    """
    Parse either plain JSON or SSE-style `data:` lines into JSON objects.
    """
    decoder = json.JSONDecoder()
    buffer_text = ""

    for raw_line in stream_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()

        try:
            # Try to parse the line as standalone JSON.
            yield json.loads(line)
            continue
        except json.JSONDecodeError:
            buffer_text += line

    # Fallback: try to decode concatenated JSON.
    idx = 0
    while idx < len(buffer_text):
        try:
            obj, end_idx = decoder.raw_decode(buffer_text, idx)
        except json.JSONDecodeError:
            break
        yield obj
        idx = end_idx
        while idx < len(buffer_text) and buffer_text[idx].isspace():
            idx += 1


def _post_jsonrpc(mcp_url: str, message: dict, session_id: str | None = None):
    """
    Send a single JSON-RPC message via Streamable HTTP and return
    (response_body_text, response_headers).
    """
    body = json.dumps(message).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        # MCP Streamable HTTP: must accept both JSON and SSE.
        "Accept": "application/json, text/event-stream",
        # Use the modern streamable HTTP spec version
        "MCP-Protocol-Version": "2025-03-26",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    req = Request(mcp_url, data=body, method="POST", headers=headers)

    with urlopen(req) as resp:
        text = resp.read().decode("utf-8")
        # Grab session id if server assigns one
        resp_session = resp.headers.get("Mcp-Session-Id")
        return text, resp_session


def get_mcp_session_id(mcp_url: str) -> str | None:
    """
    Initialize a session with the MCP and return the assigned MCP session id.
    """
    init_id = f"init-{uuid.uuid4()}"
    init_msg = {
        "jsonrpc": "2.0",
        "id": init_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "example-script", "version": "0.0.1"},
        },
    }
    _, session_id = _post_jsonrpc(mcp_url, init_msg)
    return session_id


def list_mcp_tools(mcp_url: str):
    """
    Talk to a Streamable HTTP MCP server and return its advertised tools.
    """

    session_id = get_mcp_session_id(mcp_url)

    # You could parse init_body to verify the server's response if you want.

    # 2) tools/list (same session if provided)
    tools_id = f"tools-{uuid.uuid4()}"
    tools_msg = {
        "jsonrpc": "2.0",
        "id": tools_id,
        "method": "tools/list",
        "params": {},
    }

    tools_body, _ = _post_jsonrpc(mcp_url, tools_msg, session_id=session_id)
    print("tools body: ", tools_body)

    tools = []
    for message in _iter_json_messages(tools_body):
        if not isinstance(message, dict):
            continue
        if message.get("id") == tools_id and "result" in message:
            result = message.get("result") or {}
            tools = result.get("tools", [])
            break

    return tools


def list_mcp_resources(mcp_url: str):
    """
    Talk to a Streamable HTTP MCP server and return its advertised resources.
    """
    session_id = get_mcp_session_id(mcp_url)

    resources_id = f"resources-{uuid.uuid4()}"
    resources_msg = {
        "jsonrpc": "2.0",
        "id": resources_id,
        "method": "resources/list",
        "params": {},
    }

    resources_body, _ = _post_jsonrpc(mcp_url, resources_msg, session_id=session_id)
    print("resources body: ", resources_body)

    resources = []
    for message in _iter_json_messages(resources_body):
        if not isinstance(message, dict):
            continue
        if message.get("id") == resources_id and "result" in message:
            result = message.get("result") or {}
            resources = result.get("resources", [])
            break

    return resources


def _build_signifier_label(instance_name: str, entry_name: str | None, fallback: str) -> str:
    suffix = entry_name or fallback
    return f"{instance_name}_{suffix}"


def create_signifier_from_tool(mcp_url: str, t: dict, instance_name: str):
    s: Signifier = Signifier(URIRef(generate_signifier_url_from_id(generate_id())), Graph())
    tool_name = t.get("name")
    s.graph.add(
        (s.uri, RDFS["label"], Literal(_build_signifier_label(instance_name, tool_name, "tool")))
    )
    s.add_nl_context(t["description"])
    behavior_id = s.create_behavior()
    form = BNode()
    s.graph.add((behavior_id, TD["hasForm"], form))
    s.graph.add((form, HCTL["hasTarget"], URIRef(mcp_url)))
    s.graph.add((form, HCTL["forContentType"], Literal("application/json")))
    s.graph.add((form, HTTP["methodName"], Literal("POST")))
    h1 = BNode()
    s.graph.add((h1, HTTP["fieldName"], Literal("Accept")))
    s.graph.add((h1, HTTP["fieldValue"], Literal("application/json, text/event-stream")))
    h2 = BNode()
    s.graph.add((h2, HTTP["fieldName"], Literal("MCP-Protocol-Version")))
    s.graph.add((h2, HTTP["fieldValue"], Literal("2025-06-18")))
    h3 = BNode()
    s.graph.add((h3, HTTP["fieldName"], Literal("Mcp-Session-Id")))
    s.graph.add((h3, HTTP["fieldValue"], Literal(get_mcp_session_id(mcp_url))))
    headers = create_rdf_list(s.graph, [h1, h2, h3])
    s.graph.add((form, HTTP["headers"], headers))
    tool_name = t["name"]
    tool_input = t["inputSchema"]
    # Build full JSON-RPC payload schema expected by the MCP server.
    payload_schema = {
        "type": "object",
        "properties": {
            "jsonrpc": {"type": "string", "const": "2.0"},
            "id": {"type": "integer"},
            "method": {"type": "string", "const": "tools/call"},
            "params": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "const": tool_name},
                    "arguments": tool_input,
                },
                "required": ["name", "arguments"],
            },
        },
        "required": ["jsonrpc", "id", "method", "params"],
    }
    json_schema = get_schema_from_tool_input(s.graph, payload_schema)
    tool_description = (t.get("description") or "").strip()
    if tool_description:
        _add_schema_comment(s.graph, "arguments", tool_description, fallback_node=json_schema)
    s.graph.add((behavior_id, TD["hasInputSchema"], json_schema))
    return s


def create_signifier_from_resource(mcp_url: str, r: dict, instance_name: str):
    s: Signifier = Signifier(URIRef(generate_signifier_url_from_id(generate_id())), Graph())
    resource_name = r.get("name") or r.get("uri")
    s.graph.add(
        (
            s.uri,
            RDFS["label"],
            Literal(_build_signifier_label(instance_name, resource_name, "resource")),
        )
    )
    s.add_nl_context(r["description"])
    behavior_id = s.create_behavior()
    form = BNode()
    s.graph.add((behavior_id, TD["hasForm"], form))
    s.graph.add((form, HCTL["hasTarget"], URIRef(mcp_url)))
    s.graph.add((form, HCTL["forContentType"], Literal("application/json")))
    s.graph.add((form, HTTP["methodName"], Literal("POST")))
    h1 = BNode()
    s.graph.add((h1, HTTP["fieldName"], Literal("Accept")))
    s.graph.add((h1, HTTP["fieldValue"], Literal("application/json, text/event-stream")))
    h2 = BNode()
    s.graph.add((h2, HTTP["fieldName"], Literal("MCP-Protocol-Version")))
    s.graph.add((h2, HTTP["fieldValue"], Literal("2025-06-18")))
    h3 = BNode()
    s.graph.add((h3, HTTP["fieldName"], Literal("Mcp-Session-Id")))
    s.graph.add((h3, HTTP["fieldValue"], Literal(get_mcp_session_id(mcp_url))))
    headers = create_rdf_list(s.graph, [h1, h2, h3])
    s.graph.add((form, HTTP["headers"], headers))
    resource_uri = r["uri"]
    payload_schema = {
        "type": "object",
        "properties": {
            "jsonrpc": {"type": "string", "const": "2.0"},
            "id": {"type": "integer", "const": 1},
            "method": {"type": "string", "const": "resources/read"},
            "params": {
                "type": "object",
                "properties": {"uri": {"type": "string", "const": resource_uri}},
                "required": ["uri"],
            },
        },
        "required": ["jsonrpc", "id", "method", "params"],
    }
    json_schema = get_schema_from_tool_input(s.graph, payload_schema)
    s.graph.add((behavior_id, TD["hasInputSchema"], json_schema))
    return s


def create_profile_from_mcp_server(mcp_url: str, instance_name: str):
    profile_uri = generate_artifact_url_from_id(instance_name)
    profile = Profile(URIRef(profile_uri), Graph())
    profile.graph.add((profile.uri, RDF["type"], HMAS["ResourceProfile"]))
    profile.graph.add((profile.uri, RDFS["label"], Literal(instance_name)))
    tools = list_mcp_tools(mcp_url)
    for t in tools:
        print("MCP tool: ", t)
        profile.exposes_signifier(create_signifier_from_tool(mcp_url, t, instance_name))
    resources = list_mcp_resources(mcp_url)
    for r in resources:
        print("MCP resource: ", r)
        s = create_signifier_from_resource(mcp_url, r, instance_name)
        profile.exposes_signifier(s)
    return profile
