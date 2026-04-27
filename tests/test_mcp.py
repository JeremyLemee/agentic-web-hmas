import os
import signal
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest
from rdflib import BNode, Graph, Literal, Namespace, RDF, RDFS, URIRef


REPO_ROOT = Path(__file__).resolve().parents[1]
SEM_BASE_URL = "http://localhost:5000"
GOAL_ARTIFACT_URL = f"{SEM_BASE_URL}/artifacts/goal_mcp"
GOAL_MCP_ARTIFACT_URI = URIRef("http://localhost:5000/artifacts/goal_mcp")
GOAL_MCP_TARGET = URIRef("http://localhost:9996/mcp")

HMAS = Namespace("https://purl.org/hmas/")
HCTL = Namespace("https://www.w3.org/2019/wot/hypermedia#")
HTTP = Namespace("http://www.w3.org/2011/http#")
JS = Namespace("https://www.w3.org/2019/wot/json-schema#")
TD = Namespace("https://www.w3.org/2019/wot/td#")
EXPECTED_FEEDBACK_TEXT = (
    "Provide feedback for the current goal. If achieved is true, the current goal is cleared."
)


def _wait_for_goal_artifact(process: subprocess.Popen, timeout_seconds: int = 120) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("run.sh terminated before goal_mcp artifact became ready")
        try:
            with urlopen(GOAL_ARTIFACT_URL, timeout=5) as response:
                if response.status == 200:
                    return
        except (HTTPError, URLError):
            pass
        time.sleep(2)

    raise TimeoutError(f"goal_mcp artifact not ready after {timeout_seconds}s: {GOAL_ARTIFACT_URL}")


@pytest.fixture(scope="module")
def running_stack():
    process = subprocess.Popen(
        ["bash", "run.sh"],
        cwd=REPO_ROOT,
        preexec_fn=os.setsid,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_goal_artifact(process)
        yield
    finally:
        if process.poll() is None:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                process.wait(timeout=10)


def _single_object(graph: Graph, subject, predicate):
    values = list(graph.objects(subject, predicate))
    assert len(values) == 1
    return values[0]


def _required_values(graph: Graph, schema_node) -> set[str]:
    return {str(v) for v in graph.objects(schema_node, JS["required"])}


def _property_node(graph: Graph, schema_node, property_name: str):
    for candidate in graph.objects(schema_node, JS["properties"]):
        if (candidate, JS["propertyName"], Literal(property_name)) in graph:
            return candidate
    raise AssertionError(f"Missing property '{property_name}'")


def _rdf_list_length(graph: Graph, head) -> int:
    if head == RDF.nil:
        return 0

    count = 0
    node = head
    seen = set()
    while node != RDF.nil:
        assert node not in seen, "Detected cycle in RDF list"
        seen.add(node)
        assert isinstance(node, BNode)
        assert (node, RDF.first, None) in graph
        node = _single_object(graph, node, RDF.rest)
        count += 1
    return count


def _normalize_ws(value: str) -> str:
    return " ".join(value.split())


def test_goal_mcp_artifact_profile_matches_expected_shape(running_stack):
    request = Request(GOAL_ARTIFACT_URL, headers={"Accept": "text/turtle"})
    with urlopen(request, timeout=10) as response:
        assert response.status == 200
        payload = response.read().decode("utf-8")

    graph = Graph()
    graph.parse(data=payload, format="turtle")

    signifier = None
    for candidate in graph.subjects(RDFS["label"], Literal("goal_mcp_provide_feedback")):
        signifier = candidate
        break
    assert signifier is not None

    assert (GOAL_MCP_ARTIFACT_URI, HMAS["exposesSignifier"], signifier) in graph
    assert (GOAL_MCP_ARTIFACT_URI, RDF.type, HMAS["ResourceProfile"]) in graph
    assert (signifier, RDF.type, HMAS["Signifier"]) in graph

    context_node = _single_object(graph, signifier, HMAS["recommendsContext"])
    context_comment = _single_object(graph, context_node, RDFS["comment"])
    assert _normalize_ws(str(context_comment)) == EXPECTED_FEEDBACK_TEXT

    behavior_node = _single_object(graph, signifier, HMAS["signifies"])
    form_node = _single_object(graph, behavior_node, TD["hasForm"])

    assert _single_object(graph, form_node, HCTL["forContentType"]) == Literal("application/json")
    assert _single_object(graph, form_node, HCTL["hasTarget"]) == GOAL_MCP_TARGET
    assert _single_object(graph, form_node, HTTP["methodName"]) == Literal("POST")

    headers_list = _single_object(graph, form_node, HTTP["headers"])
    assert _rdf_list_length(graph, headers_list) == 3

    root_schema = _single_object(graph, behavior_node, TD["hasInputSchema"])

    assert (root_schema, RDF.type, JS["ObjectSchema"]) in graph
    assert _required_values(graph, root_schema) == {"id", "jsonrpc", "method", "params"}

    id_node = _property_node(graph, root_schema, "id")
    assert (id_node, RDF.type, JS["IntegerSchema"]) in graph

    method_node = _property_node(graph, root_schema, "method")
    assert (method_node, RDF.type, JS["StringSchema"]) in graph
    assert (method_node, JS["const"], Literal("tools/call")) in graph

    jsonrpc_node = _property_node(graph, root_schema, "jsonrpc")
    assert (jsonrpc_node, RDF.type, JS["StringSchema"]) in graph
    assert (jsonrpc_node, JS["const"], Literal("2.0")) in graph

    params_node = _property_node(graph, root_schema, "params")
    assert (params_node, RDF.type, JS["ObjectSchema"]) in graph
    assert _required_values(graph, params_node) == {"arguments", "name"}

    name_node = _property_node(graph, params_node, "name")
    assert (name_node, RDF.type, JS["StringSchema"]) in graph
    assert (name_node, JS["const"], Literal("provide_feedback")) in graph

    arguments_node = _property_node(graph, params_node, "arguments")
    assert (arguments_node, RDF.type, JS["ObjectSchema"]) in graph
    assert _required_values(graph, arguments_node) == {"achieved", "feedback"}

    argument_comments = {str(v).strip() for v in graph.objects(arguments_node, RDFS["comment"])}
    assert EXPECTED_FEEDBACK_TEXT in {_normalize_ws(comment) for comment in argument_comments}

    achieved_node = _property_node(graph, arguments_node, "achieved")
    assert (achieved_node, RDF.type, JS["BooleanSchema"]) in graph

    feedback_node = _property_node(graph, arguments_node, "feedback")
    assert (feedback_node, RDF.type, JS["StringSchema"]) in graph
