from rdflib import Namespace, Graph, URIRef

from signifier import Signifier, bind_common_prefixes

HMAS = Namespace("https://purl.org/hmas/")
RDFS = Namespace("http://www.w3.org/2000/01/rdf-schema#")
JS = Namespace("https://www.w3.org/2019/wot/json-schema#")
HTTP = Namespace("http://www.w3.org/2011/http#")
HCTL = Namespace("https://www.w3.org/2019/wot/hypermedia#")


class Profile:
    def __init__(self, uri: URIRef, graph: Graph):
        self._uri = uri
        self._graph = graph

    @property
    def uri(self):
        return self._uri

    @property
    def graph(self):
        return self._graph

    def exposes_signifier(self, signifier: Signifier):
        self._graph.add((self._uri, HMAS["exposesSignifier"], signifier.uri))
        self._graph += signifier.graph

    def nl_context(self):
        """
        Return all natural-language context comments attached via recommendsContext.
        """
        contexts = []
        for ctx in self._graph.objects(self._uri, HMAS["hasContext"]):
            for comment in self._graph.objects(ctx, RDFS["comment"]):
                contexts.append(str(comment))
        return contexts

    def __str__(self):
        bind_common_prefixes(self._graph)
        return self._graph.serialize(format="turtle")
