import sys
from pathlib import Path

from wot_sem.affordances.action_affordance import ActionAffordance
from wot_sem.affordances.event_affordance import EventAffordance
from wot_sem.affordances.form import Form
from wot_sem.affordances.property_affordance import PropertyAffordance

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sem_profile import Profile
from urllib.request import Request, urlopen

from pyshacl import validate
from rdflib import URIRef, Graph, Namespace, BNode, Literal, RDFS, RDF

from signifier import Signifier
from utils import (
    generate_artifact_url_from_id,
    generate_id,
    generate_signifier_url_from_id,
    get_schema_from_tool_input,
)
from wot_sem.affordances.thing_description import ThingDescription


HMAS = Namespace("https://purl.org/hmas/")

HCTL = Namespace("https://www.w3.org/2019/wot/hypermedia#")
HTTP = Namespace("http://www.w3.org/2011/http#")
TD = Namespace("https://www.w3.org/2019/wot/td#")

SIGNIFIER_SHACL_TURTLE = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix ex: <http://example.com/#> .
@prefix hmas: <https://purl.org/hmas/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix hctl: <https://www.w3.org/2019/wot/hypermedia#> .
@prefix http: <http://www.w3.org/2011/http#> .
@prefix td: <https://www.w3.org/2019/wot/td#> .

ex:SignifierShape a sh:NodeShape ;
    sh:targetClass hmas:Signifier ;
    sh:property [
        sh:path rdfs:label ;
        sh:datatype xsd:string ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
    ] ;
    sh:property [ sh:path hmas:recommendsAbility ] ;
    sh:property [
        sh:path hmas:recommendsContext ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:property [
            sh:path rdfs:comment ;
            sh:minCount 1 ;
            sh:maxCount 1 ;
        ] ;
    ] ;
    sh:property [
        sh:path hmas:signifies ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:node ex:AffordanceShape ;
    ] .

ex:AffordanceShape a sh:NodeShape ;
    sh:targetClass td:InteractionAffordance ;
    sh:property [
        sh:path td:hasForm ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:node ex:FormShape
    ] ;
    sh:property [
        sh:path td:hasInputSchema ;
        sh:minCount 0 ;
        sh:maxCount 1 ;
    ] .

ex:FormShape a sh:NodeShape ;
    sh:property [
        sh:path http:methodName ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
    ] ;
    sh:property [
        sh:path hctl:hasTarget ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
    ] ;
    sh:property [
        sh:path hctl:forContentType ;
        sh:minCount 0 ;
        sh:maxCount 1 ;
    ] ;
    sh:property [
        sh:path http:headers ;
        sh:minCount 0 ;
        sh:maxCount 1 ;
    ] .
"""

_SIGNIFIER_SHACL_GRAPH: Graph | None = None


def get_td_from_url(td_url):
    """
    Fetch a Thing Description from the given URL and parse it into a ThingDescription
    instance. We let rdflib handle content negotiation/format detection where
    possible, and fall back to basic JSON-LD parsing if needed.
    """
    g = Graph()
    try:
        # rdflib can fetch and attempt to infer the format from the URL/headers.
        g.parse(td_url)
    except Exception:
        # Fallback: manually fetch and try JSON-LD (common for TDs).
        with urlopen(Request(td_url)) as resp:
            data = resp.read()
        try:
            g.parse(data=data, format="json-ld", publicID=td_url)
        except Exception:
            # Last resort: try Turtle.
            g.parse(data=data, format="turtle", publicID=td_url)

    return ThingDescription(g)


def _build_signifier_label(instance_name: str, affordance_name: str | None, fallback: str) -> str:
    suffix = affordance_name or fallback
    return f"{instance_name}_{suffix}"


def _get_signifier_shacl_graph() -> Graph:
    global _SIGNIFIER_SHACL_GRAPH
    if _SIGNIFIER_SHACL_GRAPH is None:
        _SIGNIFIER_SHACL_GRAPH = Graph()
        _SIGNIFIER_SHACL_GRAPH.parse(data=SIGNIFIER_SHACL_TURTLE, format="turtle")
    return _SIGNIFIER_SHACL_GRAPH


def _signifier_conforms_to_shape(signifier: Signifier) -> bool:
    conforms, _, report_text = validate(
        signifier.graph,
        shacl_graph=_get_signifier_shacl_graph(),
        allow_infos=False,
        allow_warnings=False,
    )
    if not conforms:
        print(f"Skipping invalid signifier {signifier.uri}: {report_text}")
    return conforms


def _create_signifier_from_affordance(
    affordance: ActionAffordance | PropertyAffordance | EventAffordance,
    instance_name: str,
    fallback_label: str,
    default_operation: str,
):
    s: Signifier = Signifier(URIRef(generate_signifier_url_from_id(generate_id())), Graph())
    affordance_name = affordance.name or affordance.title
    s.graph.add(
        (
            s.uri,
            RDFS["label"],
            Literal(_build_signifier_label(instance_name, affordance_name, fallback_label)),
        )
    )
    nl_context = affordance.description or affordance.title or affordance.name
    if nl_context:
        s.add_nl_context(nl_context)

    behavior_id = s.create_behavior()
    s.graph.add((behavior_id, RDF.type, TD["InteractionAffordance"]))

    form_def: Form | None = affordance.forms[0] if affordance.forms else None
    if form_def is None:
        return s

    form = BNode()
    s.graph.add((behavior_id, TD["hasForm"], form))
    s.graph.add((form, HCTL["hasTarget"], URIRef(form_def.target)))

    if form_def.content_type:
        s.graph.add((form, HCTL["forContentType"], Literal(form_def.content_type)))

    # Prefer explicitly declared operation types; default to the affordance's primary operation.
    op_types = form_def.operation_types or {default_operation}
    for op in op_types:
        s.graph.add((form, HCTL["hasOperationType"], Literal(op)))

    method_name = form_def.get_method_name(next(iter(op_types), None))
    if method_name:
        s.graph.add((form, HTTP["methodName"], Literal(method_name)))

    if form_def.subprotocol:
        s.graph.add((form, HCTL["hasSubProtocol"], Literal(form_def.subprotocol)))
    if affordance.json_schema:
        print("has JSON Schema")
        json_schema = get_schema_from_tool_input(s.graph, affordance.json_schema)
        s.graph.add((behavior_id, TD["hasInputSchema"], json_schema))
    else:
        print("has no JSON Schema")

    return s


def create_signifier_from_action(a: ActionAffordance, instance_name: str):
    print("create for signifier from action for instance: ", instance_name, " and action: ", a.name)
    return _create_signifier_from_affordance(
        a, instance_name, "action", "invokeaction"
    )


def create_signifier_from_property(p: PropertyAffordance, instance_name: str):
    print("create for signifier from property for instance: ", instance_name, " and property: ", p.name)
    return _create_signifier_from_affordance(
        p, instance_name, "property", "readproperty"
    )


def create_signifier_from_event(e: EventAffordance, instance_name: str):
    print("create for signifier from event for instance: ", instance_name, " and event: ", e.name)
    return _create_signifier_from_affordance(
        e, instance_name, "event", "subscribeevent"
    )


def create_profile_from_td(td_url: str, instance_name: str):
    profile_uri = generate_artifact_url_from_id(instance_name)
    profile = Profile(URIRef(profile_uri), Graph())
    profile.graph.add((profile.uri, RDFS["label"], Literal(instance_name)))
    td = get_td_from_url(td_url)
    for a in td.actions:
        signifier = create_signifier_from_action(a, instance_name)
        if _signifier_conforms_to_shape(signifier):
            profile.exposes_signifier(signifier)
    for p in td.properties:
        signifier = create_signifier_from_property(p, instance_name)
        if _signifier_conforms_to_shape(signifier):
            profile.exposes_signifier(signifier)
    for e in td.events:
        signifier = create_signifier_from_event(e, instance_name)
        if _signifier_conforms_to_shape(signifier):
            profile.exposes_signifier(signifier)
    return profile
