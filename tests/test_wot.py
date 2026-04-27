import sys
from pathlib import Path

import pytest
from rdflib import Graph, Namespace
from pyshacl import validate

# Add project root to path for imports
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wot_sem.affordances.thing_description import ThingDescription
from wot_sem.wot_interact import (
    create_signifier_from_action,
    create_signifier_from_property,
    create_signifier_from_event,
)


# Common Thing Description with all 3 affordance types
COMMON_TD_TURTLE = """
@prefix td: <https://www.w3.org/2019/wot/td#> .
@prefix hctl: <https://www.w3.org/2019/wot/hypermedia#> .
@prefix http: <http://www.w3.org/2011/http#> .
@prefix hmas: <https://purl.org/hmas/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix ex: <http://example.com/td/> .

ex:TestDevice a td:Thing ;
    td:title "Test Device" ;
    td:description "A device with property, action, and event affordances" ;
    td:hasPropertyAffordance ex:temperatureProperty ;
    td:hasActionAffordance ex:setTemperatureAction ;
    td:hasEventAffordance ex:temperatureChangedEvent .

ex:temperatureProperty a td:PropertyAffordance ;
    td:name "temperature" ;
    td:title "Temperature Reading" ;
    td:description "Current temperature in Celsius" ;
    td:hasForm ex:temperaturePropertyForm .

ex:temperaturePropertyForm a hctl:Form ;
    hctl:hasTarget <http://localhost:8080/properties/temperature> ;
    hctl:hasOperationType "readproperty" ;
    hctl:methodName "GET" ;
    hctl:forContentType "application/json" .

ex:setTemperatureAction a td:ActionAffordance ;
    td:name "setTemperature" ;
    td:title "Set Temperature" ;
    td:description "Set the device temperature" ;
    td:hasForm ex:setTemperatureActionForm ;
    td:hasInputSchema ex:setTemperatureSchema .

ex:setTemperatureActionForm a hctl:Form ;
    hctl:hasTarget <http://localhost:8080/actions/setTemperature> ;
    hctl:hasOperationType "invokeaction" ;
    hctl:methodName "POST" ;
    hctl:forContentType "application/json" .

ex:setTemperatureSchema a <https://www.w3.org/2019/wot/json-schema#> ;
    <https://www.w3.org/2019/wot/json-schema#type> "object" .

ex:temperatureChangedEvent a td:EventAffordance ;
    td:name "temperatureChanged" ;
    td:title "Temperature Changed" ;
    td:description "Emitted when temperature changes" ;
    td:hasForm ex:temperatureChangedEventForm .

ex:temperatureChangedEventForm a hctl:Form ;
    hctl:hasTarget <http://localhost:8080/events/temperatureChanged> ;
    hctl:hasOperationType "subscribeevent" ;
    hctl:methodName "GET" ;
    hctl:forContentType "application/json" .
"""

# SHACL Shape for validating signifiers
SHACL_SHAPE_TURTLE = """
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
    sh:property [
        sh:path hmas:recommendsAbility ;
        sh:minCount 0 ;
    ] ;
    sh:property [
        sh:path hmas:recommendsContext ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:node ex:ContextShape ;
    ] ;
    sh:property [
        sh:path hmas:signifies ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:node ex:AffordanceShape ;
    ] .

ex:ContextShape a sh:NodeShape ;
    sh:property [
        sh:path rdfs:comment ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
    ] .

ex:AffordanceShape a sh:NodeShape ;
    sh:targetClass td:InteractionAffordance ;
    sh:property [
        sh:path td:hasForm ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:node ex:FormShape ;
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
        sh:minCount 1 ;
        sh:maxCount 1 ;
    ] .
"""


class TestWotSignifierTransformation:
    """Test transformation of WoT affordances to HMAS signifiers with SHACL validation."""

    @pytest.fixture
    def td_graph(self):
        """Parse the common Thing Description."""
        g = Graph()
        g.parse(data=COMMON_TD_TURTLE, format="turtle")
        return g

    @pytest.fixture
    def thing_description(self, td_graph):
        """Create a ThingDescription from the parsed graph."""
        return ThingDescription(td_graph)

    @pytest.fixture
    def shacl_graph(self):
        """Parse the SHACL shape."""
        g = Graph()
        g.parse(data=SHACL_SHAPE_TURTLE, format="turtle")
        return g

    def _get_affordance_by_name(self, td, affordance_type, name):
        """Helper to extract an affordance from the TD by name."""
        if affordance_type == "action":
            affordances = td.actions
        elif affordance_type == "property":
            affordances = td.properties
        elif affordance_type == "event":
            affordances = td.events
        else:
            raise ValueError(f"Unknown affordance type: {affordance_type}")

        for aff in affordances:
            if aff.name == name:
                return aff

        return None

    def test_property_affordance_transformation(self, thing_description, shacl_graph):
        """Test that property affordance is correctly transformed to a signifier."""
        # Get property affordance
        prop = self._get_affordance_by_name(thing_description, "property", "temperature")
        assert prop is not None

        # Create signifier from property
        signifier = create_signifier_from_property(prop, "testDevice")

        # Merge signifier graph with SHACL graph
        conforms, report_g, report_text = validate(
            signifier.graph, shacl_graph=shacl_graph, allow_infos=False, allow_warnings=False
        )

        assert conforms, f"Signifier does not conform to SHACL shape:\n{report_text}"

        # Additional assertions on signifier structure
        RDFS = Namespace("http://www.w3.org/2000/01/rdf-schema#")

        # Check label exists
        labels = list(signifier.graph.objects(signifier.uri, RDFS.label))
        assert len(labels) == 1
        assert "temperature" in str(labels[0]).lower()

        # Check context exists
        contexts = signifier.nl_context()
        assert len(contexts) > 0

    def test_action_affordance_transformation(self, thing_description, shacl_graph):
        """Test that action affordance is correctly transformed to a signifier."""
        # Get action affordance
        action = self._get_affordance_by_name(thing_description, "action", "setTemperature")
        assert action is not None

        # Create signifier from action
        signifier = create_signifier_from_action(action, "testDevice")

        # Merge signifier graph with SHACL graph
        conforms, report_g, report_text = validate(
            signifier.graph, shacl_graph=shacl_graph, allow_infos=False, allow_warnings=False
        )

        assert conforms, f"Signifier does not conform to SHACL shape:\n{report_text}"

        # Additional assertions on signifier structure
        RDFS = Namespace("http://www.w3.org/2000/01/rdf-schema#")

        # Check label exists
        labels = list(signifier.graph.objects(signifier.uri, RDFS.label))
        assert len(labels) == 1
        assert "settemperature" in str(labels[0]).lower()

        # Check context exists
        contexts = signifier.nl_context()
        assert len(contexts) > 0

    def test_event_affordance_transformation(self, thing_description, shacl_graph):
        """Test that event affordance is correctly transformed to a signifier."""
        # Get event affordance
        event = self._get_affordance_by_name(thing_description, "event", "temperatureChanged")
        assert event is not None

        # Create signifier from event
        signifier = create_signifier_from_event(event, "testDevice")

        # Merge signifier graph with SHACL graph
        conforms, report_g, report_text = validate(
            signifier.graph, shacl_graph=shacl_graph, allow_infos=False, allow_warnings=False
        )

        assert conforms, f"Signifier does not conform to SHACL shape:\n{report_text}"

        # Additional assertions on signifier structure
        RDFS = Namespace("http://www.w3.org/2000/01/rdf-schema#")

        # Check label exists
        labels = list(signifier.graph.objects(signifier.uri, RDFS.label))
        assert len(labels) == 1
        assert "temperaturechanged" in str(labels[0]).lower()

        # Check context exists
        contexts = signifier.nl_context()
        assert len(contexts) > 0
