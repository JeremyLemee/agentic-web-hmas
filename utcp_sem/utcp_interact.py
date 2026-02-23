import sys
from pathlib import Path
from typing import Any

import requests
from requests import RequestException

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sem_profile import Profile

from rdflib import URIRef, Graph, Namespace, BNode, Literal, RDFS

from signifier import Signifier
from utils import (
    generate_artifact_url_from_id,
    generate_id,
    generate_signifier_url_from_id,
    get_schema_from_tool_input,
)

HMAS = Namespace("https://purl.org/hmas/")

HCTL = Namespace("https://www.w3.org/2019/wot/hypermedia#")

HTTP = Namespace("http://www.w3.org/2011/http#")

RDF = Namespace("http://www.w3.org/1999/02/22-rdf-syntax-ns#")

JS = Namespace("https://www.w3.org/2019/wot/json-schema#")
TD = Namespace("https://www.w3.org/2019/wot/td#")


def _add_schema_comments(graph: Graph, schema: dict[str, Any]) -> None:
    if not isinstance(schema, dict):
        return
    props = schema.get("properties")
    if not isinstance(props, dict):
        return
    for prop_name, prop_schema in props.items():
        if not isinstance(prop_schema, dict):
            continue
        description = prop_schema.get("description")
        if not description:
            continue
        for node in graph.subjects(JS["propertyName"], Literal(prop_name)):
            graph.add((node, RDFS["comment"], Literal(description)))


def list_utcp_tools(utcp_url: str, timeout: float = 10.0) -> list[dict[str, Any]]:
    try:
        r = requests.get(utcp_url, timeout=timeout)
        r.raise_for_status()
        manual = r.json()

        tools = manual.get("tools")
        return tools if isinstance(tools, list) else []
    except (RequestException, ValueError, TypeError):
        # RequestException: network/HTTP issues; ValueError: JSON parse; TypeError: manual not a dict
        return []


def _build_signifier_label(instance_name: str, tool_name: str | None) -> str:
    suffix = tool_name or "tool"
    return f"{instance_name}_{suffix}"


def create_signifier_from_utcp_tool(t: dict[str, Any], instance_name: str):
    s: Signifier = Signifier(URIRef(generate_signifier_url_from_id(generate_id())), Graph())
    tool_name = t.get("name") or t.get("id")
    s.graph.add((s.uri, RDFS["label"], Literal(_build_signifier_label(instance_name, tool_name))))
    s.add_nl_context(t["description"])
    behavior_id = s.create_behavior()
    form = BNode()
    s.graph.add((behavior_id, TD["hasForm"], form))
    tool_template = t.get("tool_call_template", {})
    target_url = tool_template.get("url")
    if target_url:
        s.graph.add((form, HCTL["hasTarget"], URIRef(target_url)))
    content_type = tool_template.get("content_type")
    if content_type:
        s.graph.add((form, HCTL["forContentType"], Literal(content_type)))
    http_method = tool_template.get("http_method")
    if http_method:
        s.graph.add((form, HTTP["methodName"], Literal(http_method)))
    tool_input_schema = t.get("inputs", {})
    json_schema = get_schema_from_tool_input(s.graph, tool_input_schema)
    _add_schema_comments(s.graph, tool_input_schema)
    s.graph.add((behavior_id, TD["hasInputSchema"], json_schema))
    return s


def create_profile_from_utcp_manual(utcp_manual_url: str, instance_name: str):
    profile_uri = generate_artifact_url_from_id(instance_name)
    profile = Profile(URIRef(profile_uri), Graph())
    profile.graph.add((profile.uri, RDFS["label"], Literal(instance_name)))
    tools = list_utcp_tools(utcp_manual_url)
    for t in tools:
        print(t)
        profile.exposes_signifier(create_signifier_from_utcp_tool(t, instance_name))
    return profile
