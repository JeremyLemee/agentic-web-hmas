from rdflib import Graph, Namespace, URIRef

from signifier import Signifier, bind_common_prefixes

HMAS = Namespace("https://purl.org/hmas/")
RDFS = Namespace("http://www.w3.org/2000/01/rdf-schema#")


class Profile:
    def __init__(self, uri: URIRef, graph: Graph):
        self._uri: URIRef = uri
        self._graph: Graph = graph

    @property
    def uri(self) -> URIRef:
        return self._uri

    @property
    def graph(self) -> Graph:
        return self._graph

    def exposes_signifier(self, signifier: Signifier) -> None:
        self._graph.add((self._uri, HMAS["exposesSignifier"], signifier.uri))
        self._graph += signifier.graph

    def nl_context(self) -> list[str]:
        contexts: list[str] = []
        for ctx in self._graph.objects(self._uri, HMAS["hasContext"]):
            for comment in self._graph.objects(ctx, RDFS["comment"]):
                contexts.append(str(comment))
        return contexts

    def __str__(self) -> str:
        bind_common_prefixes(self._graph)
        return self._graph.serialize(format="turtle")
