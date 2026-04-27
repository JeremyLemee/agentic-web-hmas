from rdflib import Namespace, Graph, URIRef, Literal, BNode, RDF

HMAS = Namespace("https://purl.org/hmas/")
RDFS = Namespace("http://www.w3.org/2000/01/rdf-schema#")
JS = Namespace("https://www.w3.org/2019/wot/json-schema#")
HTTP = Namespace("http://www.w3.org/2011/http#")
HCTL = Namespace("https://www.w3.org/2019/wot/hypermedia#")
TD = Namespace("https://www.w3.org/2019/wot/td#")

COMMON_PREFIXES = {
    "js": JS,
    "http": HTTP,
    "hmas": HMAS,
    "hctl": HCTL,
    "td": TD,
}


def bind_common_prefixes(graph: Graph):
    for prefix, ns in COMMON_PREFIXES.items():
        graph.bind(prefix, ns)


class Signifier:
    def __init__(self, uri: URIRef, graph: Graph):
        self._uri = uri
        self._graph = graph
        self._graph.add((self._uri, RDF.type, HMAS["Signifier"]))

    @property
    def uri(self):
        return self._uri

    @property
    def graph(self):
        return self._graph

    def add_nl_context(self, context: str):
        c = BNode()
        self._graph.add((self._uri, HMAS["recommendsContext"], c))
        self._graph.add((c, RDFS["comment"], Literal(context)))

    def add_recommended_ability(self, ability: str):
        ability_node = BNode()
        self._graph.add((self._uri, HMAS["recommendsAbility"], ability_node))
        self._graph.add((ability_node, RDFS["comment"], Literal(ability)))

    def nl_context(self):
        """
        Return all natural-language context comments attached via recommendsContext.
        """
        contexts = []
        for ctx in self._graph.objects(self._uri, HMAS["recommendsContext"]):
            for comment in self._graph.objects(ctx, RDFS["comment"]):
                contexts.append(str(comment))
        return contexts

    def create_behavior(self):
        behavior_id = BNode()
        self._graph.add((self._uri, HMAS["signifies"], behavior_id))
        return behavior_id

    def add_behavior(self, behavior: str):
        """Attach a behavior node with an rdfs:comment description."""
        behavior_node = self.create_behavior()
        self._graph.add((behavior_node, RDFS["comment"], Literal(behavior)))
        return behavior_node

    def __str__(self):
        bind_common_prefixes(self._graph)
        return self._graph.serialize(format="turtle")

    @staticmethod
    def get_signifiers(kg: Graph):
        return []
