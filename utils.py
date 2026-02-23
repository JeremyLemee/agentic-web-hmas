import uuid
from typing import Iterable

from rdflib import BNode, Graph, RDF, Literal, Namespace, URIRef
import json


def generate_id():
    return str(uuid.uuid4())


def generate_artifact_url_from_id(id: str):
    return "http://localhost:5000/artifacts/" + id  # TODO: update


def generate_signifier_url_from_id(id: str):
    return "http://localhost:5000/signifiers/" + id  # TODO: update


def create_rdf_list(graph: Graph, elements: Iterable) -> BNode | URIRef:
    """
    Build an RDF list from `elements` and attach it to `graph`.
    Returns the head node (or RDF.nil when the iterable is empty).
    """
    elements_list = list(elements)
    if not elements_list:
        return RDF.nil

    head = BNode()
    current = head

    for element in elements_list[:-1]:
        graph.add((current, RDF.first, element))
        next_node = BNode()
        graph.add((current, RDF.rest, next_node))
        current = next_node

    last_element = elements_list[-1]
    graph.add((current, RDF.first, last_element))
    graph.add((current, RDF.rest, RDF.nil))

    return head


def get_schema_from_tool_input(graph: Graph, tool_schema: dict) -> BNode:
    """
    Convert a JSON schema dictionary into RDF triples using the WoT JSON Schema ontology.
    """
    JS = Namespace("https://www.w3.org/2019/wot/json-schema#")
    TYPE_MAP = {
        "object": JS.ObjectSchema,
        "array": JS.ArraySchema,
        "string": JS.StringSchema,
        "number": JS.NumberSchema,
        "integer": JS.IntegerSchema,
        "boolean": JS.BooleanSchema,
        "null": JS.NullSchema,
    }

    def _build(schema: dict) -> BNode:
        node = BNode()
        for key, value in schema.items():
            if key == "type":
                if isinstance(value, list):
                    for item in value:
                        mapped_item = TYPE_MAP.get(item)
                        if mapped_item:
                            graph.add((node, RDF.type, mapped_item))
                        else:
                            graph.add((node, JS[key], Literal(item)))
                else:
                    mapped = TYPE_MAP.get(value)
                    if mapped:
                        graph.add((node, RDF.type, mapped))
                    else:
                        graph.add((node, JS[key], Literal(value)))
                continue

            if key == "title":
                continue

            if key == "properties" and isinstance(value, dict):
                for prop_name, prop_schema in value.items():
                    prop_node = _build(prop_schema)
                    graph.add((prop_node, JS["propertyName"], Literal(prop_name)))
                    graph.add((node, JS["properties"], prop_node))
                continue

            if isinstance(value, dict):
                child = _build(value)
                graph.add((node, JS[key], child))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        graph.add((node, JS[key], _build(item)))
                    else:
                        graph.add((node, JS[key], Literal(item)))
            else:
                graph.add((node, JS[key], Literal(value)))

        return node

    return _build(tool_schema)


def get_schema_from_tool_input_safe(graph: Graph, tool_schema: dict) -> BNode:
    """
    Safer variant of get_schema_from_tool_input that tolerates list-valued literals
    (e.g., enum entries that themselves contain arrays) by serializing them to JSON
    strings before turning them into RDF literals.
    """
    JS = Namespace("https://www.w3.org/2019/wot/json-schema#")
    TYPE_MAP = {
        "object": JS.ObjectSchema,
        "array": JS.ArraySchema,
        "string": JS.StringSchema,
        "number": JS.NumberSchema,
        "integer": JS.IntegerSchema,
        "boolean": JS.BooleanSchema,
        "null": JS.NullSchema,
    }

    def _literal(value):
        if isinstance(value, (dict, list)):
            # Serialize complex values so rdflib gets a hashable literal.
            return Literal(json.dumps(value))
        return Literal(value)

    def _build(schema: dict) -> BNode:
        node = BNode()
        for key, value in schema.items():
            if key == "type":
                if isinstance(value, list):
                    for item in value:
                        mapped_item = TYPE_MAP.get(item)
                        if mapped_item:
                            graph.add((node, RDF.type, mapped_item))
                        else:
                            graph.add((node, JS[key], _literal(item)))
                else:
                    mapped = TYPE_MAP.get(value)
                    if mapped:
                        graph.add((node, RDF.type, mapped))
                    else:
                        graph.add((node, JS[key], _literal(value)))
                continue

            if key == "title":
                continue

            if key == "properties" and isinstance(value, dict):
                for prop_name, prop_schema in value.items():
                    prop_node = _build(prop_schema)
                    graph.add((prop_node, JS["propertyName"], Literal(prop_name)))
                    graph.add((node, JS["properties"], prop_node))
                continue

            if isinstance(value, dict):
                child = _build(value)
                graph.add((node, JS[key], child))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        graph.add((node, JS[key], _build(item)))
                    else:
                        graph.add((node, JS[key], _literal(item)))
            else:
                graph.add((node, JS[key], _literal(value)))

        return node

    return _build(tool_schema)
