from typing import Any, Iterable

from rdflib import BNode, Graph, Literal, RDF, URIRef, Namespace


class JSONSchema:
    """
    Minimal JSON Schema representation using the WoT JSON Schema ontology.
    Captures key/value pairs in a dictionary-like structure and can round-trip
    to and from an RDF graph.
    """

    NAMESPACE = "https://www.w3.org/2019/wot/json-schema#"

    def __init__(self, schema_dict=None):
        self._data = schema_dict or {}

    @property
    def data(self):
        return self._data

    def to_dict(self):
        return self._data

    @classmethod
    def from_graph(cls, graph: Graph, node):
        visited = set()

        def _local_name(uri):
            text = str(uri)
            if "#" in text:
                return text.split("#")[-1]
            return text.rsplit("/", 1)[-1]

        def _walk(current):
            if current in visited:
                return {}
            visited.add(current)
            mapping = {}
            for _, p, o in graph.triples((current, None, None)):
                if p == RDF.type and str(o).startswith(cls.NAMESPACE):
                    mapping.setdefault("typeIRI", []).append(str(o))
                    continue
                if not str(p).startswith(cls.NAMESPACE):
                    continue
                key = _local_name(p)
                value = None
                if isinstance(o, Literal):
                    value = o.toPython()
                elif isinstance(o, URIRef):
                    value = str(o)
                else:  # BNode or others
                    value = _walk(o)

                if key in mapping:
                    if not isinstance(mapping[key], list):
                        mapping[key] = [mapping[key]]
                    mapping[key].append(value)
                else:
                    mapping[key] = value
            return mapping

        return cls(_walk(node))

    def to_graph(self, graph: Graph, node=None):
        subject = node or BNode()

        def _add_value(subject_node, key, value):
            predicate = URIRef(f"{self.NAMESPACE}{key}")
            if isinstance(value, dict):
                b = BNode()
                graph.add((subject_node, predicate, b))
                _add_dict(b, value)
            elif isinstance(value, list):
                for v in value:
                    _add_value(subject_node, key, v)
            elif isinstance(value, URIRef):
                graph.add((subject_node, predicate, value))
            else:
                graph.add((subject_node, predicate, Literal(value)))

        def _add_dict(subject_node, data_dict):
            for k, v in data_dict.items():
                _add_value(subject_node, k, v)

        _add_dict(subject, self._data)
        return subject

    @classmethod
    def json_schema_from_rdf(cls, graph: Graph, node: URIRef | BNode) -> dict[str, Any]:
        """
        Build a JSON Schema dict from RDF triples using the WoT JSON Schema ontology.
        """
        js = Namespace(cls.NAMESPACE)
        reverse_type_map = {
            js.ObjectSchema: "object",
            js.ArraySchema: "array",
            js.StringSchema: "string",
            js.NumberSchema: "number",
            js.IntegerSchema: "integer",
            js.BooleanSchema: "boolean",
            js.NullSchema: "null",
        }

        def _parse_rdf_list(head: URIRef | BNode | None) -> Iterable:
            items = []
            current = head
            while current and current != RDF.nil:
                first = graph.value(current, RDF.first)
                if first is None:
                    break
                items.append(first)
                current = graph.value(current, RDF.rest)
            return items

        def _walk(current: URIRef | BNode) -> dict[str, Any]:
            schema: dict[str, Any] = {}
            for _, _, t in graph.triples((current, RDF.type, None)):
                if t in reverse_type_map:
                    schema["type"] = reverse_type_map[t]

            for predicate, obj in graph.predicate_objects(current):
                if predicate == RDF.type:
                    continue
                if predicate == js["properties"]:
                    list_head = obj if graph.value(obj, RDF.first) else None
                    prop_nodes = _parse_rdf_list(list_head) if list_head else [obj]
                    for prop_node in prop_nodes:
                        prop_schema = _walk(prop_node)
                        prop_name = graph.value(prop_node, js["propertyName"])
                        if prop_name:
                            schema.setdefault("properties", {})[str(prop_name)] = prop_schema
                    continue
                if predicate == js["propertyName"]:
                    continue

                if not str(predicate).startswith(cls.NAMESPACE):
                    continue

                key = str(predicate).split("#")[-1]
                if isinstance(obj, (URIRef, BNode)):
                    if predicate == js["required"] and graph.value(obj, RDF.first):
                        for item in _parse_rdf_list(obj):
                            schema.setdefault("required", []).append(str(item.toPython()))
                        continue
                    value = _walk(obj)
                    if key in schema and isinstance(schema[key], list):
                        schema[key].append(value)
                    elif key in schema and schema[key] != value:
                        schema[key] = [schema[key], value]
                    else:
                        schema[key] = value
                else:
                    literal_value = obj.toPython()
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

        return _walk(node)

    def __repr__(self):
        return f"JSONSchema({self._data})"
