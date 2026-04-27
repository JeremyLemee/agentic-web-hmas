import asyncio
import sys
from pathlib import Path

from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from pyshacl import validate
from rdflib import Graph, Literal, Namespace, RDF, RDFS

# Add project root to path for imports
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from a2a_sem import a2a_interact


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


def _formalizer_agent_card() -> AgentCard:
    formalizer_skill = AgentSkill(
        id="formalize_goal",
        name="Formalize goal",
        description="Converts a natural language goal into move(d) or rotate(a).",
        tags=["formalizer", "goal"],
        examples=[
            "move forward 2 meters",
            "rotate 90 degrees",
            "turn left 45 degrees",
        ],
    )

    return AgentCard(
        name="Formalizer Agent",
        description="Formalizes goal descriptions into move/rotate commands.",
        url="http://localhost:9997/",
        version="1.0.0",
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[formalizer_skill],
        supportsAuthenticatedExtendedCard=False,
    )


def test_a2a_formalizer_profile_creates_a_shacl_conformant_signifier(monkeypatch):
    async def _fake_fetch_agent_card_from_host(_base_url: str) -> AgentCard:
        return _formalizer_agent_card()

    monkeypatch.setattr(
        a2a_interact,
        "fetch_agent_card_from_host",
        _fake_fetch_agent_card_from_host,
    )

    profile = asyncio.run(
        a2a_interact.create_profile_for_a2a_agent("http://localhost:9997", "formalizer")
    )

    shacl_graph = Graph()
    shacl_graph.parse(data=SHACL_SHAPE_TURTLE, format="turtle")

    conforms, _, report_text = validate(
        profile.graph, shacl_graph=shacl_graph, allow_infos=False, allow_warnings=False
    )
    assert conforms, f"A2A formalizer signifier does not conform to the SHACL shape:\n{report_text}"

    signifiers = list(profile.graph.subjects(RDF.type, HMAS["Signifier"]))
    assert len(signifiers) == 1
    signifier = signifiers[0]

    assert _single_object(profile.graph, signifier, RDFS.label) == Literal("formalizer_Formalize goal")

    context_node = _single_object(profile.graph, signifier, HMAS["recommendsContext"])
    assert _single_object(profile.graph, context_node, RDFS.comment) == Literal(
        "Converts a natural language goal into move(d) or rotate(a)."
    )

    affordance = _single_object(profile.graph, signifier, HMAS["signifies"])
    form = _single_object(profile.graph, affordance, TD["hasForm"])
    assert _single_object(profile.graph, form, HCTL["hasTarget"]).toPython() == "http://localhost:9997"
    assert _single_object(profile.graph, form, HCTL["forContentType"]) == Literal("application/json")
    assert _single_object(profile.graph, form, HTTP["methodName"]) == Literal("POST")

