
from rdflib import URIRef, Graph, BNode, Namespace, Literal, RDFS
from typing import Iterable

from sem_profile import Profile

import httpx
from a2a.client import A2ACardResolver
from a2a.types import AgentCard
from a2a.types import AgentSkill

from signifier import Signifier
from utils import (
    generate_artifact_url_from_id,
    generate_signifier_url_from_id,
    generate_id,
    get_schema_from_tool_input_safe,
)

HMAS = Namespace("https://purl.org/hmas/")

HCTL = Namespace("https://www.w3.org/2019/wot/hypermedia#")

HTTP = Namespace("http://www.w3.org/2011/http#")

RDF = Namespace("http://www.w3.org/1999/02/22-rdf-syntax-ns#")

JS = Namespace("https://www.w3.org/2019/wot/json-schema#")
TD = Namespace("https://www.w3.org/2019/wot/td#")


def _add_schema_comment(graph: Graph, property_name: str, comment: str) -> None:
    if not comment:
        return
    for node in graph.subjects(JS["propertyName"], Literal(property_name)):
        graph.add((node, RDFS["comment"], Literal(comment)))


async def fetch_agent_card_from_host(base_url: str) -> AgentCard:
    async with httpx.AsyncClient(timeout=60.0) as http_client:
        resolver = A2ACardResolver(
            httpx_client=http_client,
            base_url=base_url,
        )
        # Explicitly point to /.well-known/agent-card.json
        card = await resolver.get_agent_card(relative_card_path="/.well-known/agent-card.json")
        return card


def _build_signifier_label(instance_name: str, skill_name: str | None) -> str:
    suffix = skill_name or "skill"
    return f"{instance_name}_{suffix}"


def _get_effective_input_modes(skill: AgentSkill, card: AgentCard) -> list[str]:
    # Skill-level input modes override card-level defaults when present.
    skill_input_modes = getattr(skill, "input_modes", None)
    if skill_input_modes is None:
        skill_input_modes = getattr(skill, "inputModes", None)
    if skill_input_modes is not None:
        return [m for m in skill_input_modes if isinstance(m, str)]

    default_input_modes = getattr(card, "default_input_modes", None)
    if default_input_modes is None:
        default_input_modes = getattr(card, "defaultInputModes", None)
    if default_input_modes is None:
        return []
    return [m for m in default_input_modes if isinstance(m, str)]


def _normalize_input_mode(mode: str) -> str | None:
    normalized = mode.strip().lower()
    aliases = {
        "text": "text/plain",
        "text/plain": "text/plain",
        "json": "application/json",
        "application/json": "application/json",
    }
    return aliases.get(normalized)


def _supported_input_modes(modes: Iterable[str]) -> list[str]:
    supported_modes: list[str] = []
    for mode in modes:
        normalized = _normalize_input_mode(mode)
        if normalized is None or normalized in supported_modes:
            continue
        supported_modes.append(normalized)
    return supported_modes


def _part_kinds_from_input_modes(input_modes: Iterable[str]) -> list[str]:
    mode_to_kind = {
        "text/plain": "text",
        "application/json": "data",
    }
    kinds: list[str] = []
    for mode in input_modes:
        kind = mode_to_kind.get(mode)
        if kind and kind not in kinds:
            kinds.append(kind)
    return kinds


def _build_parts_description(input_modes: Iterable[str]) -> str:
    kinds = _part_kinds_from_input_modes(input_modes)
    kinds_text = ", ".join(f'"{kind}"' for kind in kinds) if kinds else "none"
    return (
        "A JSON array. Each element is a JSON object whose first property is called "
        f'"kind". Allowed "kind" values from input modes: {kinds_text}. '
        'If "kind" is "text", the other field is "text" and its value is a string. '
        'If "kind" is "data", the other field is "data" and its value is a JSON object.'
    )


def _build_a2a_payload_schema(input_modes: Iterable[str]) -> dict:
    # Build full JSON-RPC payload schema expected by the A2A agent endpoint.
    parts_description = _build_parts_description(input_modes)

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "jsonrpc": {"type": "string", "const": "2.0"},
            "id": {"type": ["string", "number", "null"]},
            "method": {"type": "string", "const": "message/send"},
            "params": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "message": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "messageId": {"type": "string"},
                            "role": {"type": "string", "const": "user"},
                            "parts": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 1,
                                "description": parts_description,
                            },
                        },
                        "required": ["role", "parts"],
                    }
                },
                "required": ["message"],
            },
        },
        "required": ["jsonrpc", "id", "method", "params"],
    }


def create_signifier_from_skill(
    agent_url: str,
    skill: AgentSkill,
    instance_name: str,
    input_mode: str,
    effective_input_modes: Iterable[str],
):
    s: Signifier = Signifier(URIRef(generate_signifier_url_from_id(generate_id())), Graph())
    skill_name = getattr(skill, "name", None) or getattr(skill, "id", None)
    base_label = _build_signifier_label(instance_name, skill_name)
    s.graph.add((s.uri, RDFS["label"], Literal(base_label)))
    s.add_nl_context(skill.description)
    behavior_id = s.create_behavior()
    form = BNode()
    s.graph.add((behavior_id, TD["hasForm"], form))
    s.graph.add((form, HCTL["hasTarget"], URIRef(agent_url)))
    s.graph.add((form, HCTL["forContentType"], Literal("application/json")))
    s.graph.add((form, HTTP["methodName"], Literal("POST")))
    payload_schema = _build_a2a_payload_schema(effective_input_modes)
    json_schema = get_schema_from_tool_input_safe(s.graph, payload_schema)
    _add_schema_comment(s.graph, "messageId", "A string that identifies the message")
    if input_mode == "application/json":
        _add_schema_comment(
            s.graph,
            "data",
            "A JSON value to send to the A2A agent as a structured data part",
        )
    else:
        _add_schema_comment(s.graph, "text", "The message to send to the A2A agent")
    s.graph.add((behavior_id, TD["hasInputSchema"], json_schema))
    return s


async def create_profile_for_a2a_agent(base_url: str, instance_name: str):
    card = await fetch_agent_card_from_host(base_url)
    profile_uri = generate_artifact_url_from_id(instance_name)
    profile = Profile(URIRef(profile_uri), Graph())
    profile.graph.add((profile.uri, RDFS["label"], Literal(instance_name)))
    for skill in card.skills:
        input_modes = _supported_input_modes(_get_effective_input_modes(skill, card))
        for input_mode in input_modes:
            profile.exposes_signifier(
                create_signifier_from_skill(
                    base_url, skill, instance_name, input_mode, input_modes
                )
            )
    return profile
