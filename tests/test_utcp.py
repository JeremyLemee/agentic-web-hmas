import sys
from pathlib import Path

from pyshacl import validate
from rdflib import Graph, Literal, Namespace, RDF, RDFS

# Add project root to path for imports
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utcp_sem import utcp_interact
from wot_sem.cherrybot_proxy import _proxy_utcp_manual


HMAS = Namespace("https://purl.org/hmas/")
HCTL = Namespace("https://www.w3.org/2019/wot/hypermedia#")
HTTP = Namespace("http://www.w3.org/2011/http#")
TD = Namespace("https://www.w3.org/2019/wot/td#")

# The prompt uses hctl:methodName, but the repository consistently models
# method names with http:methodName in the interaction code and existing tests.
SHACL_SHAPE_TURTLE = """
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
@prefix ex:   <http://example.com/#> .
@prefix hmas: <https://purl.org/hmas/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix hctl: <https://www.w3.org/2019/wot/hypermedia#> .
@prefix http: <http://www.w3.org/2011/http#> .
@prefix td:   <https://www.w3.org/2019/wot/td#> .

ex:SignifierShape a sh:NodeShape ;
  sh:targetClass hmas:Signifier ;
  sh:property [ sh:path rdfs:label ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path hmas:recommendsAbility ; sh:minCount 0 ] ;
  sh:property [
    sh:path hmas:recommendsContext ; sh:minCount 1 ; sh:maxCount 1 ;
    sh:node ex:ContextShape
  ] ;
  sh:property [
    sh:path hmas:signifies ; sh:minCount 1 ; sh:maxCount 1 ;
    sh:node ex:AffordanceShape
  ] .

ex:ContextShape a sh:NodeShape ;
  sh:property [ sh:path rdfs:comment ; sh:minCount 1 ; sh:maxCount 1 ] .

ex:AffordanceShape a sh:NodeShape ;
  sh:property [
    sh:path td:hasForm ;
    sh:minCount 1 ;
    sh:maxCount 1 ;
    sh:node ex:FormShape
  ] ;
  sh:property [ sh:path td:hasInputSchema ; sh:minCount 0 ; sh:maxCount 1 ] .

ex:FormShape a sh:NodeShape ;
  sh:property [ sh:path http:methodName ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path hctl:hasTarget ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path hctl:forContentType ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path http:headers ; sh:minCount 0 ; sh:maxCount 1 ] .
"""


def _single_object(graph: Graph, subject, predicate):
    values = list(graph.objects(subject, predicate))
    assert len(values) == 1
    return values[0]


def _signifier_by_label(graph: Graph, label: str):
    for candidate in graph.subjects(RDFS.label, Literal(label)):
        if (candidate, RDF.type, HMAS["Signifier"]) in graph:
            return candidate
    raise AssertionError(f"Missing signifier with label {label!r}")


def _signifier_closure(graph: Graph, signifier) -> Graph:
    closure = Graph()
    visited = set()
    queue = [signifier]

    while queue:
        subject = queue.pop()
        if subject in visited:
            continue
        visited.add(subject)
        for predicate, obj in graph.predicate_objects(subject):
            closure.add((subject, predicate, obj))
            if not isinstance(obj, Literal):
                queue.append(obj)

    return closure


def test_utcp_operation_signifier_from_cherrybot_manual_conforms_to_shape(monkeypatch):
    manual = _proxy_utcp_manual("http://localhost:8086")
    monkeypatch.setattr(utcp_interact, "list_utcp_tools", lambda _url: manual["tools"])

    profile = utcp_interact.create_profile_from_utcp_manual("http://localhost:8086/utcp", "cherrybot")

    operation_signifier = _signifier_by_label(profile.graph, "cherrybot_operation")
    operation_graph = _signifier_closure(profile.graph, operation_signifier)

    shacl_graph = Graph()
    shacl_graph.parse(data=SHACL_SHAPE_TURTLE, format="turtle")

    conforms, _, report_text = validate(
        operation_graph, shacl_graph=shacl_graph, allow_infos=False, allow_warnings=False
    )
    assert conforms, f"UTCP operation signifier does not conform to the SHACL shape:\n{report_text}"

    affordance = _single_object(operation_graph, operation_signifier, HMAS["signifies"])
    form = _single_object(operation_graph, affordance, TD["hasForm"])
    assert _single_object(operation_graph, form, HCTL["hasTarget"]).toPython() == (
        "http://localhost:8086/operation"
    )
    assert _single_object(operation_graph, form, HCTL["forContentType"]) == Literal("text/plain")
    assert _single_object(operation_graph, form, HTTP["methodName"]) == Literal("POST")


def test_utcp_initialize_signifier_documents_manual_shape_gap(monkeypatch):
    manual = _proxy_utcp_manual("http://localhost:8086")
    monkeypatch.setattr(utcp_interact, "list_utcp_tools", lambda _url: manual["tools"])

    profile = utcp_interact.create_profile_from_utcp_manual("http://localhost:8086/utcp", "cherrybot")

    initialize_signifier = _signifier_by_label(profile.graph, "cherrybot_initialize")
    initialize_graph = _signifier_closure(profile.graph, initialize_signifier)

    shacl_graph = Graph()
    shacl_graph.parse(data=SHACL_SHAPE_TURTLE, format="turtle")

    conforms, _, report_text = validate(
        initialize_graph, shacl_graph=shacl_graph, allow_infos=False, allow_warnings=False
    )
    assert not conforms
    assert "forContentType" in report_text
