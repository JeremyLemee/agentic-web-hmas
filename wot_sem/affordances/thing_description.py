from rdflib import Graph, RDF, URIRef
from rdflib.namespace import DCTERMS
from rdflib.term import Literal


from wot_sem.affordances.action_affordance import ActionAffordanceBuilder
from wot_sem.affordances.property_affordance import PropertyAffordanceBuilder
from wot_sem.affordances.event_affordance import EventAffordanceBuilder
from wot_sem.affordances.form import FormBuilder
from wot_sem.affordances.json_schema import JSONSchema


class ThingDescription:
    TD = "https://www.w3.org/2019/wot/td#"
    HCTL = "https://www.w3.org/2019/wot/hypermedia#"
    HTV = "http://www.w3.org/2011/http#"
    JSON_SCHEMA = "https://www.w3.org/2019/wot/json-schema#"

    def __init__(self, g):
        self._id = None
        self._base_uri = None
        self._g: Graph = g
        self._properties = set()
        self._actions = set()
        self._events = set()
        self.parse_td()

    @property
    def id(self):
        return self._id

    @property
    def base_uri(self):
        return self._base_uri

    @property
    def properties(self):
        return self._properties

    @property
    def actions(self):
        return self._actions

    @property
    def events(self):
        return self._events

    def transform_graph(self):
        print("transform the graph")

    def parse_td(self):
        self.transform_graph()
        self.read_id()
        self.read_base_uri()
        if self.id is not None:
            self.read_actions()
            self.read_properties()
            self.read_events()

    def read_id(self):
        identifier = None
        for s, p, o in self._g:
            if p == RDF.type and o == URIRef("https://www.w3.org/2019/wot/td#Thing"):
                identifier = s
        self._id = identifier

    def read_base_uri(self):
        for s, p, o in self._g:
            if s == self.id and p in {URIRef(f"{self.TD}baseURI"), URIRef(f"{self.TD}base")}:
                self._base_uri = o

    def read_actions(self):
        for s, p, o in self._g:
            if s == self.id and p == URIRef(f"{self.TD}hasActionAffordance"):
                a = self.get_action_from_id(o)
                if a is not None:
                    self.actions.add(a)

    def get_action_from_id(self, action_id):
        name = self._get_first_literal(action_id, {URIRef(f"{self.TD}name")}) or self._local_name(
            action_id
        )
        title = self._get_first_literal(action_id, {DCTERMS.title, URIRef(f"{self.TD}title")})
        description = self._get_first_literal(
            action_id, {DCTERMS.description, URIRef(f"{self.TD}description")}
        )
        safe = self._get_boolean(action_id, {URIRef(f"{self.TD}isSafe"), URIRef(f"{self.TD}safe")})
        idempotent = self._get_boolean(
            action_id, {URIRef(f"{self.TD}isIdempotent"), URIRef(f"{self.TD}idempotent")}
        )
        affordance_schema = self._get_affordance_schema(
            action_id,
            schema_predicates={
                URIRef(f"{self.TD}hasInputSchema"),
                URIRef(f"{self.TD}inputSchema"),
            },
        )

        form_list = self.get_forms_from_affordance_id(action_id, default_operation="invokeaction")
        action_builder = ActionAffordanceBuilder(
            name or str(action_id), form_list, title=title, description=description
        )
        action_builder.set_safe(safe)
        action_builder.set_idempotent(idempotent)
        if affordance_schema:
            action_builder.set_json_schema(affordance_schema)
        action = action_builder.build()
        return action

    def read_properties(self):
        for s, p, o in self._g:
            if s == self.id and p == URIRef(f"{self.TD}hasPropertyAffordance"):
                prop = self.get_property_from_id(o)
                if prop is not None:
                    self.properties.add(prop)

    def get_property_from_id(self, prop_id):
        name = self._get_first_literal(prop_id, {URIRef(f"{self.TD}name")}) or self._local_name(
            prop_id
        )
        title = self._get_first_literal(prop_id, {DCTERMS.title, URIRef(f"{self.TD}title")})
        description = self._get_first_literal(
            prop_id, {DCTERMS.description, URIRef(f"{self.TD}description")}
        )
        read_only = self._get_boolean(prop_id, {URIRef(f"{self.TD}isReadOnly")})
        write_only = self._get_boolean(prop_id, {URIRef(f"{self.TD}isWriteOnly")})
        observable = self._get_boolean(prop_id, {URIRef(f"{self.TD}isObservable")})
        affordance_schema = self._get_affordance_schema(prop_id)

        forms = self.get_forms_from_affordance_id(prop_id, default_operation="readproperty")
        builder = PropertyAffordanceBuilder(
            name or str(prop_id), forms, title=title, description=description
        )
        builder.set_read_only(read_only)
        builder.set_write_only(write_only)
        builder.set_observable(observable)
        if affordance_schema:
            builder.set_json_schema(affordance_schema)
        return builder.build()

    def read_events(self):
        for s, p, o in self._g:
            if s == self.id and p == URIRef(f"{self.TD}hasEventAffordance"):
                evt = self.get_event_from_id(o)
                if evt is not None:
                    self.events.add(evt)

    def get_event_from_id(self, event_id):
        name = self._get_first_literal(event_id, {URIRef(f"{self.TD}name")}) or self._local_name(
            event_id
        )
        title = self._get_first_literal(event_id, {DCTERMS.title, URIRef(f"{self.TD}title")})
        description = self._get_first_literal(
            event_id, {DCTERMS.description, URIRef(f"{self.TD}description")}
        )
        affordance_schema = self._get_affordance_schema(event_id)
        forms = self.get_forms_from_affordance_id(event_id, default_operation="subscribeevent")
        builder = EventAffordanceBuilder(
            name or str(event_id), forms, title=title, description=description
        )
        if affordance_schema:
            builder.set_json_schema(affordance_schema)
        return builder.build()

    def get_forms_from_affordance_id(self, affordance_id, default_operation=None):
        forms = []
        for s, p, o in self._g:
            if s == affordance_id and p == URIRef(f"{self.TD}hasForm"):
                form = self._build_form_from_node(o, default_operation)
                if form is not None:
                    forms.append(form)
        return forms

    def _build_form_from_node(self, form_node, default_operation=None):
        target = None
        method_name = None
        content_type = None
        operation_types = set()
        subprotocol = None
        json_schema_node = None

        for s, p, o in self._g.triples((form_node, None, None)):
            if p == URIRef(f"{self.HCTL}hasTarget"):
                target = o
            elif p in {URIRef(f"{self.HTV}methodName"), URIRef(f"{self.HCTL}hasMethodName")}:
                method_name = str(o)
            elif p == URIRef(f"{self.HCTL}forContentType"):
                content_type = str(o)
            elif p == URIRef(f"{self.HCTL}hasOperationType"):
                operation_types.add(
                    self._local_name(o).lower() if self._local_name(o) else str(o).lower()
                )
            elif p == URIRef(f"{self.HCTL}hasSubProtocol"):
                subprotocol = str(o)
            elif p in {
                URIRef(f"{self.HCTL}hasPayloadSchema"),
                URIRef(f"{self.TD}hasPayloadSchema"),
            }:
                json_schema_node = o

        if json_schema_node is None:
            for _, __, o in self._g.triples((form_node, None, None)):
                if (o, RDF.type, None) in self._g:
                    for _, ___, t in self._g.triples((o, RDF.type, None)):
                        if str(t).startswith(self.JSON_SCHEMA):
                            json_schema_node = o
                            break
                if json_schema_node:
                    break

        if target is None:
            return None

        builder = FormBuilder(str(target))
        if method_name:
            builder.set_method(method_name)
        if content_type:
            builder.set_content_type(content_type)
        if operation_types:
            builder.set_operation_types(operation_types)
        elif default_operation:
            builder.add_operation_type(default_operation)
        if subprotocol:
            builder.set_subprotocol(subprotocol)
        if json_schema_node:
            schema = JSONSchema.from_graph(self._g, json_schema_node)
            builder.set_json_schema(schema)

        return builder.build()

    def _get_first_literal(self, subject, predicates):
        for predicate in predicates:
            for _, __, o in self._g.triples((subject, predicate, None)):
                if isinstance(o, Literal):
                    return str(o)
        return None

    def _get_boolean(self, subject, predicates):
        value = self._get_first_literal(subject, predicates)
        if value is None:
            return False
        if isinstance(value, str):
            return value.lower() in {"true", "1"}
        return bool(value)

    def _local_name(self, ref):
        if isinstance(ref, URIRef):
            text = str(ref)
            if "#" in text:
                return text.split("#")[-1]
            return text.rsplit("/", 1)[-1]
        return str(ref)

    def _get_affordance_schema(self, affordance_id, schema_predicates=None):
        schema_node = None
        if schema_predicates:
            for pred in schema_predicates:
                for _, __, o in self._g.triples((affordance_id, pred, None)):
                    schema_node = o
                    break
                if schema_node:
                    break

        if schema_node is None:
            properties_pred = URIRef(f"{self.JSON_SCHEMA}properties")
            if not any(self._g.triples((affordance_id, properties_pred, None))):
                return None
            schema_node = affordance_id

        schema = JSONSchema.json_schema_from_rdf(self._g, schema_node)
        if schema and "properties" in schema and "type" not in schema:
            schema["type"] = "object"
        return schema or None

    def get_turtle(self):
        return self._g.serialize(format="turtle")

    def get_json_ld(self):
        return self._g.serialize(format="json-ld")
