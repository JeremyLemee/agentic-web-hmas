import argparse
import asyncio
from langchain_core.language_models import BaseChatModel

from a2a_sem.a2a_interact import create_profile_for_a2a_agent
from sem_profile import Profile
from typing import Any, Callable

from flask import Flask, Response, abort, request
from pathlib import Path
import json
import uuid

from rdflib import Graph, URIRef, BNode, RDF, RDFS, Literal

from llm import load_llm
from signifier import Signifier, bind_common_prefixes, HMAS
from mcp_sem.mcp_interact import create_profile_from_mcp_server
from utcp_sem.utcp_interact import create_profile_from_utcp_manual
from wot_sem.wot_interact import create_profile_from_td

# Keep a global environment KG for incoming signifiers and profile generation.
envKG = Graph()

# Mapping from profile identifier to its stored RDF graph.
profiles: dict[str, Graph] = {}

artifacts: dict[str, Graph] = {}
artifact_instances: dict[str, str] = {}

# Supported media types and the format strings rdflib expects.
SUPPORTED_SERIALIZATIONS = {
    "text/turtle": "turtle",
    "application/ld+json": "json-ld",
}

DEFAULT_MIMETYPE = "text/turtle"
DEFAULT_FORMAT = "turtle"
DEFAULT_APP_CONFIG = "config_app.json"


def _extract_llm_text(llm_response: Any) -> str:
    content = getattr(llm_response, "content", llm_response)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    parts.append(str(item))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


def _strip_leading_think_block(text: str) -> str:
    stripped = text.lstrip()
    if not stripped.lower().startswith("<think>"):
        return text
    end_idx = stripped.lower().find("</think>")
    if end_idx == -1:
        return stripped
    return stripped[end_idx + len("</think>") :].lstrip()


def register_profile(profile_id: str, context_comment: str) -> URIRef:
    """
    Create and store a profile graph identified by profile_id with a natural-language context comment.
    Mirrors the structure used by the initial profile.
    """
    global envKG, profiles
    profile_uri = URIRef(f"http://localhost:5000/profile/{profile_id}")
    g = Graph()
    context = BNode()
    g.add((profile_uri, HMAS["hasContext"], context))
    g.add((context, RDFS["comment"], Literal(context_comment)))
    profiles[profile_id] = g
    envKG += g
    return profile_uri


def register_mcp_server(instance_name: str, mcp_url: str) -> str | None:
    global envKG, artifacts, artifact_instances
    try:
        mcp_profile = create_profile_from_mcp_server(mcp_url, instance_name)
    except Exception as exc:
        print(f"Skipping MCP server registration for {instance_name} ({mcp_url}): {exc}")
        return None
    bind_common_prefixes(mcp_profile.graph)
    artifact_uri = str(mcp_profile.uri)
    artifacts[artifact_uri] = mcp_profile.graph
    artifact_instances[instance_name] = artifact_uri
    envKG += mcp_profile.graph
    return artifact_uri


def register_a2a_agent(instance_name: str, a2a_url: str) -> str | None:
    global envKG, artifacts, artifact_instances
    try:
        a2a_profile = asyncio.run(create_profile_for_a2a_agent(a2a_url, instance_name))
    except Exception as exc:
        print(f"Skipping A2A agent registration for {instance_name} ({a2a_url}): {exc}")
        return None
    bind_common_prefixes(a2a_profile.graph)
    artifact_uri = str(a2a_profile.uri)
    artifacts[artifact_uri] = a2a_profile.graph
    artifact_instances[instance_name] = artifact_uri
    envKG += a2a_profile.graph
    return artifact_uri


def register_utcp_manual(instance_name: str, utcp_manual_url: str) -> str | None:
    global envKG, artifacts, artifact_instances
    try:
        utcp_profile = create_profile_from_utcp_manual(utcp_manual_url, instance_name)
    except Exception as exc:
        print(f"Skipping UTCP manual registration for {instance_name} ({utcp_manual_url}): {exc}")
        return None
    bind_common_prefixes(utcp_profile.graph)
    artifact_uri = str(utcp_profile.uri)
    artifacts[artifact_uri] = utcp_profile.graph
    artifact_instances[instance_name] = artifact_uri
    envKG += utcp_profile.graph
    return artifact_uri


def register_wot_td(instance_name: str, td_url: str) -> str | None:
    global envKG, artifacts, artifact_instances
    try:
        td_profile = create_profile_from_td(td_url, instance_name)
    except Exception as exc:
        print(f"Skipping TD registration for {instance_name} ({td_url}): {exc}")
        return None
    bind_common_prefixes(td_profile.graph)
    artifact_uri = str(td_profile.uri)
    artifacts[artifact_uri] = td_profile.graph
    artifact_instances[instance_name] = artifact_uri
    envKG += td_profile.graph
    return artifact_uri


ArtifactProfileRegistrar = Callable[[str, str], str | None]

ARTIFACT_PROFILE_REGISTRARS: dict[str, ArtifactProfileRegistrar] = {
    "mcp": register_mcp_server,
    "a2a": register_a2a_agent,
    "utcp": register_utcp_manual,
    "wot": register_wot_td,
}


def register_artifact_profile(
    instance_name: str, instance_url: str, instance_type: str
) -> str | None:
    normalized_name = instance_name.strip()
    if normalized_name in artifact_instances:
        raise RuntimeError(f"Artifact instance '{normalized_name}' is already registered")

    normalized_type = instance_type.strip().lower()
    registrar = ARTIFACT_PROFILE_REGISTRARS.get(normalized_type)
    if registrar is None:
        supported_types = ", ".join(sorted(ARTIFACT_PROFILE_REGISTRARS.keys()))
        raise RuntimeError(
            f"Unsupported artifact type '{instance_type}'. Expected one of: {supported_types}."
        )

    return registrar(normalized_name, instance_url.strip())


def load_sem_config() -> dict[str, Any]:
    """Return the parsed JSON configuration from config.json."""
    config_path = Path(__file__).resolve().parent / "config.json"
    try:
        return json.loads(config_path.read_text())
    except FileNotFoundError:
        abort(500, "Missing config.json")
    except json.JSONDecodeError as exc:
        abort(500, f"Unable to parse config.json: {exc}")
    raise RuntimeError("unreachable")


def get_signifiers(env_graph: Graph):
    combined_graph = Graph()
    combined_graph += env_graph
    for artifact_graph in artifacts.values():
        combined_graph += artifact_graph

    signifiers = []
    signifier_nodes = set(combined_graph.subjects(RDF.type, HMAS["Signifier"]))

    for signifier_uri in signifier_nodes:
        subgraph = Graph()
        queue = [signifier_uri]
        visited = set()

        while queue:
            node = queue.pop()
            if node in visited:
                continue
            visited.add(node)

            for s, p, o in combined_graph.triples((node, None, None)):
                subgraph.add((s, p, o))
                if isinstance(o, BNode):
                    queue.append(o)

        if isinstance(signifier_uri, URIRef):
            signifiers.append(Signifier(signifier_uri, subgraph))

    return signifiers


def signifier_filter(signifier: Signifier, profile: Profile | Graph, llm: BaseChatModel):
    signifier_context = signifier.nl_context()

    if isinstance(profile, Profile):
        profile_context = profile.nl_context()
    else:
        profile_context = []
        for ctx in profile.objects(None, HMAS["hasContext"]):
            for comment in profile.objects(ctx, RDFS["comment"]):
                profile_context.append(str(comment))

    if not signifier_context or not profile_context:
        return False

    prompt = (
        "Determine whether a signifier defines an affordance that could be used by an agent to achieve its stated goal, according to its context.\n"
        "To do so, use the information from the profile context and the signifier context.\n"
        "The answer is True if the information provided by the signifier, according to its context, is directly relevant to the agent, according to its profile context. If the signifier context is not relevant to the agent, the answer is False.\n"
        "Avoid both false negatives and false positives, but false negatives are worse."
        "Respond with exactly 'True' or 'False'.\n"
        f"Profile context: {profile_context}\n"
        f"Signifier context: {signifier_context}"
    )

    llm_response = llm.invoke(prompt)
    content = _strip_leading_think_block(_extract_llm_text(llm_response))
    return not (str(content).strip().lower() == "false")



def add_to_graph(graph: Graph, signifier: Signifier):
    graph += signifier.graph
    return graph


def _recommended_ability_types_match_profile(signifier: Signifier, profile: Graph) -> bool:
    """
    Return True when ability-type gating passes for this signifier/profile pair.

    Rules:
    - If the signifier has no hmas:recommendsAbility, skip this gate and return True.
    - Otherwise, every recommended ability must have at least one rdf:type.
    - Every profile ability (hmas:hasAbility) must have at least one rdf:type.
    - The union of recommended ability rdf:types must be a subset of the union of profile
      ability rdf:types.
    """
    recommended_abilities = list(signifier.graph.objects(signifier.uri, HMAS["recommendsAbility"]))
    if not recommended_abilities:
        return True

    recommended_types: set[Any] = set()
    for ability in recommended_abilities:
        ability_types = set(signifier.graph.objects(ability, RDF.type))
        if not ability_types:
            return False
        recommended_types.update(ability_types)

    profile_abilities = list(profile.objects(None, HMAS["hasAbility"]))
    if not profile_abilities:
        return False

    profile_types: set[Any] = set()
    for ability in profile_abilities:
        ability_types = set(profile.objects(ability, RDF.type))
        if not ability_types:
            return False
        profile_types.update(ability_types)

    return recommended_types.issubset(profile_types)


def selection(profile: Graph, env_graph: Graph) -> Graph:
    """Select and return signifiers matching the profile using semantic filtering."""
    config = load_sem_config()
    provider = config["sem"]["provider"]
    name = config["sem"]["model"]
    llm = load_llm(provider, name, temperature=0)
    signifiers = get_signifiers(env_graph)
    g = Graph()
    for s in signifiers:
        if not _recommended_ability_types_match_profile(s, profile):
            continue
        if signifier_filter(s, profile, llm):
            g = add_to_graph(g, s)

    return g


def _preferred_serialization() -> tuple[str, str]:
    """Pick the best serialization for the current request."""
    best_match = request.accept_mimetypes.best_match(SUPPORTED_SERIALIZATIONS.keys())
    if best_match:
        return best_match, SUPPORTED_SERIALIZATIONS[best_match]
    return DEFAULT_MIMETYPE, DEFAULT_FORMAT


def _parse_body(body: bytes, content_type: str | None, public_id: str | None = None) -> Graph:
    """Parse RDF from the request body given the Content-Type."""
    if not body:
        abort(400, "Request body must not be empty")
    media_type = content_type.split(";")[0].strip() if content_type else ""
    rdf_format = SUPPORTED_SERIALIZATIONS.get(media_type)

    if rdf_format is None:
        abort(415, f"Unsupported content type '{media_type}'")

    graph = Graph()
    try:
        graph.parse(data=body, format=rdf_format, publicID=public_id)
    except Exception as exc:
        abort(400, f"Failed to parse RDF: {exc}")
    return graph


def _parse_nl_context(body: bytes, content_type: str | None) -> str:
    """Extract a natural-language context string from JSON or plain-text bodies."""
    if not body:
        abort(400, "Request body must not be empty")

    media_type = content_type.split(";")[0].strip().lower() if content_type else ""
    if media_type == "application/json":
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            abort(400, f"Invalid JSON: {exc}")

        context = payload.get("context")
        if not isinstance(context, str) or not context.strip():
            abort(400, "JSON body must include non-empty 'context' string field")
        return context.strip()

    try:
        context = body.decode("utf-8").strip()
    except UnicodeDecodeError:
        abort(400, "Context must be UTF-8 encoded text")

    if not context:
        abort(400, "Context must not be empty")
    return context


def _parse_signifier_payload(body: bytes, content_type: str | None) -> tuple[str, str]:
    """Parse a JSON payload that provides signifier context and behavior."""
    if not body:
        abort(400, "Request body must not be empty")
    media_type = content_type.split(";")[0].strip().lower() if content_type else ""
    if media_type != "application/json":
        abort(415, "Content-Type must be application/json")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        abort(400, f"Invalid JSON: {exc}")

    context = payload.get("context")
    behavior = payload.get("behavior")
    if not isinstance(context, str) or not context.strip():
        abort(400, "JSON body must include non-empty 'context' string field")
    if not isinstance(behavior, str) or not behavior.strip():
        abort(400, "JSON body must include non-empty 'behavior' string field")

    return context.strip(), behavior.strip()


def _parse_artifact_control_payload(body: bytes, content_type: str | None) -> dict[str, Any]:
    if not body:
        abort(400, "Request body must not be empty")
    media_type = content_type.split(";")[0].strip().lower() if content_type else ""
    if media_type != "application/json":
        abort(415, "Content-Type must be application/json")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        abort(400, f"Invalid JSON: {exc}")
    if not isinstance(payload, dict):
        abort(400, "JSON body must be an object")
    return payload


def _resolve_runtime_config_path(config_path: str) -> Path:
    candidate = Path(config_path)
    if candidate.is_absolute():
        return candidate
    return Path(__file__).resolve().parent / candidate


def _load_runtime_config(config_path: str) -> dict[str, Any]:
    resolved = _resolve_runtime_config_path(config_path)
    try:
        payload = json.loads(resolved.read_text())
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing runtime config file: {resolved}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Unable to parse runtime config file '{resolved}': {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Runtime config '{resolved}' must contain a JSON object")
    return payload


def initialize_runtime(config_path: str = DEFAULT_APP_CONFIG) -> None:
    """Initialize in-memory profiles/artifacts based on the given runtime config file."""
    global envKG, profiles, artifacts, artifact_instances
    payload = _load_runtime_config(config_path)
    profiles_data = payload.get("profiles", [])
    artifacts_data = payload.get("artifacts", [])

    if not isinstance(profiles_data, list):
        raise RuntimeError("Runtime config field 'profiles' must be a list")
    if not isinstance(artifacts_data, list):
        raise RuntimeError("Runtime config field 'artifacts' must be a list")

    envKG = Graph()
    profiles.clear()
    artifacts.clear()
    artifact_instances.clear()

    for entry in profiles_data:
        if not isinstance(entry, dict):
            raise RuntimeError("Each profile entry must be an object")
        profile_id = entry.get("id")
        context = entry.get("context", "")
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise RuntimeError("Each profile entry must include a non-empty string 'id'")
        if not isinstance(context, str):
            raise RuntimeError("Each profile entry must include a string 'context'")
        register_profile(profile_id.strip(), context)

    for entry in artifacts_data:
        if not isinstance(entry, dict):
            raise RuntimeError("Each artifact entry must be an object")
        kind = entry.get("kind")
        instance_name = entry.get("instance_name")
        url = entry.get("url")
        if not isinstance(kind, str) or not kind.strip():
            raise RuntimeError("Each artifact entry must include a non-empty string 'kind'")
        if not isinstance(instance_name, str) or not instance_name.strip():
            raise RuntimeError("Each artifact entry must include a non-empty string 'instance_name'")
        if not isinstance(url, str) or not url.strip():
            raise RuntimeError("Each artifact entry must include a non-empty string 'url'")
        register_artifact_profile(instance_name.strip(), url.strip(), kind)


def create_app(config_path: str = DEFAULT_APP_CONFIG) -> Flask:
    initialize_runtime(config_path)
    app = Flask(__name__)

    @app.get("/signifiers")
    def list_signifiers():
        profile_url = request.args.get("profile")
        if not profile_url:
            all_signifiers = get_signifiers(envKG)
            combined_graph = Graph()
            for signifier in all_signifiers:
                combined_graph += signifier.graph
            bind_common_prefixes(combined_graph)
            mimetype, rdf_format = _preferred_serialization()
            serialized = combined_graph.serialize(format=rdf_format)
            return Response(serialized, mimetype=mimetype)

        profile_graph = Graph()
        try:
            profile_graph.parse(profile_url)
        except Exception as exc:
            abort(400, f"Unable to parse profile at '{profile_url}': {exc}")
        result_graph = selection(profile_graph, envKG)
        mimetype, rdf_format = _preferred_serialization()
        bind_common_prefixes(result_graph)
        serialized = result_graph.serialize(format=rdf_format)
        return Response(serialized, mimetype=mimetype)

    @app.get("/signifiers/list")
    def list_signifiers_urls():
        all_signifiers = get_signifiers(envKG)
        signifier_urls = sorted(str(signifier.uri) for signifier in all_signifiers)
        return Response(
            json.dumps({"signifiers": signifier_urls}),
            mimetype="application/json",
        )

    @app.post("/signifiers")
    def post_signifier():
        global envKG
        rdf_graph = _parse_body(request.get_data(), request.content_type)
        envKG += rdf_graph
        return Response(status=204)

    @app.get("/profile/<path:profile_id>")
    def get_profile(profile_id: str):
        profile_graph = profiles.get(profile_id)
        if profile_graph is None:
            abort(404, "Profile not found")

        mimetype, rdf_format = _preferred_serialization()
        bind_common_prefixes(profile_graph)
        serialized = profile_graph.serialize(format=rdf_format)
        return Response(serialized, mimetype=mimetype)

    @app.put("/profile/<path:profile_id>/nl_context")
    def put_profile_nl_context(profile_id: str):
        """
        Update or create the natural-language context of a profile.
        Accepts JSON payloads with a 'context' field or plain text bodies.
        """
        global envKG
        context_comment = _parse_nl_context(request.get_data(), request.content_type)
        profile_uri = URIRef(f"http://localhost:5000/profile/{profile_id}")
        profile_graph = profiles.get(profile_id, Graph())

        def _clear_context(target_graph: Graph):
            for ctx in list(target_graph.objects(profile_uri, HMAS["hasContext"])):
                for comment in list(target_graph.objects(ctx, RDFS["comment"])):
                    target_graph.remove((ctx, RDFS["comment"], comment))
                target_graph.remove((profile_uri, HMAS["hasContext"], ctx))

        _clear_context(profile_graph)
        _clear_context(envKG)

        def _add_context(target_graph: Graph):
            ctx = BNode()
            target_graph.add((profile_uri, HMAS["hasContext"], ctx))
            target_graph.add((ctx, RDFS["comment"], Literal(context_comment)))

        _add_context(profile_graph)
        _add_context(envKG)

        profiles[profile_id] = profile_graph
        return Response(status=204)

    @app.put("/profile/<path:profile_id>")
    def put_profile(profile_id: str):
        global envKG
        rdf_graph = _parse_body(request.get_data(), request.content_type, public_id=profile_id)
        profiles[profile_id] = rdf_graph
        envKG += rdf_graph
        return Response(status=204)

    @app.get("/artifacts")
    def get_artifacts():
        return Response(
            json.dumps({"artifacts": list(artifacts.keys())}),
            mimetype="application/json",
        )

    @app.get("/artifacts/list")
    def list_artifact_profile_urls():
        base_url = request.host_url.rstrip("/")
        artifact_urls = sorted(f"{base_url}/artifacts/{name}" for name in artifact_instances.keys())
        return Response(
            json.dumps(artifact_urls),
            mimetype="application/json",
        )

    @app.get("/artifacts/<path:artifact_id>")
    def get_artifact(artifact_id: str):
        artifact_id = artifact_id.strip()
        artifact_uri = artifact_instances.get(artifact_id)
        if artifact_uri is None:
            artifact_uri = request.host_url.rstrip("/") + "/artifacts/" + artifact_id
        artifact_graph = artifacts.get(artifact_uri)
        if artifact_graph is None:
            abort(404, "Artifact not found")

        mimetype, rdf_format = _preferred_serialization()
        bind_common_prefixes(artifact_graph)
        serialized = artifact_graph.serialize(format=rdf_format)
        return Response(serialized, mimetype=mimetype)

    @app.post("/artifacts/registration")
    def control_artifact_registration():
        """
        Control artifact registration.
        JSON body:
          - action: register | sync | delete (default: register)
          - kind: artifact type key (required for register/sync)
          - instance_name: string (required for register/sync or delete by name)
          - url: string (required for register/sync)
          - artifact_uri: string (optional for sync/delete)
        """
        global envKG, artifacts, artifact_instances
        payload = _parse_artifact_control_payload(request.get_data(), request.content_type)

        action = payload.get("action", "register")
        if not isinstance(action, str):
            abort(400, "Field 'action' must be a string")
        action = action.strip().lower()
        if action not in {"register", "sync", "delete"}:
            abort(400, "Field 'action' must be one of: register, sync, delete")

        kind = payload.get("kind")
        instance_name = payload.get("instance_name")
        url = payload.get("url")
        artifact_uri = payload.get("artifact_uri")

        def _remove_artifact(uri: str):
            artifact_graph = artifacts.get(uri)
            if artifact_graph is None:
                abort(404, "Artifact not found")
            assert artifact_graph is not None
            for triple in list(artifact_graph.triples((None, None, None))):
                envKG.remove(triple)
            del artifacts[uri]
            for name, mapped_uri in list(artifact_instances.items()):
                if mapped_uri == uri:
                    del artifact_instances[name]

        if action in {"register", "sync"}:
            if not isinstance(kind, str) or not kind.strip():
                abort(400, "Field 'kind' must be a non-empty string")
            if not isinstance(instance_name, str) or not instance_name.strip():
                abort(400, "Field 'instance_name' must be a non-empty string")
            if not isinstance(url, str) or not url.strip():
                abort(400, "Field 'url' must be a non-empty string")
            assert isinstance(kind, str)
            assert isinstance(instance_name, str)
            assert isinstance(url, str)

            kind = kind.strip().lower()
            if kind not in ARTIFACT_PROFILE_REGISTRARS:
                supported_kinds = ", ".join(sorted(ARTIFACT_PROFILE_REGISTRARS.keys()))
                abort(400, f"Field 'kind' must be one of: {supported_kinds}")

            normalized_instance_name = instance_name.strip()
            normalized_url = url.strip()

            if action == "sync":
                target_uri = None
                if isinstance(artifact_uri, str) and artifact_uri.strip():
                    target_uri = artifact_uri.strip()
                elif normalized_instance_name in artifact_instances:
                    target_uri = artifact_instances[normalized_instance_name]
                if target_uri:
                    _remove_artifact(target_uri)

            try:
                new_uri = register_artifact_profile(normalized_instance_name, normalized_url, kind)
            except RuntimeError as exc:
                message = str(exc)
                if "already registered" in message:
                    abort(409, message)
                abort(400, message)

            if new_uri is None:
                abort(502, "Artifact registration failed")

            return Response(
                json.dumps({"artifact_uri": new_uri}),
                status=201 if action == "register" else 200,
                mimetype="application/json",
            )

        if action == "delete":
            target_uri = None
            if isinstance(artifact_uri, str) and artifact_uri.strip():
                target_uri = artifact_uri.strip()
            elif isinstance(instance_name, str) and instance_name.strip():
                target_uri = artifact_instances.get(instance_name.strip())

            if not target_uri:
                abort(400, "Provide 'artifact_uri' or 'instance_name' to delete an artifact")
            assert isinstance(target_uri, str)

            _remove_artifact(target_uri)
            return Response(status=204)

        abort(400, "Unsupported action")

    @app.get("/signifiers/<path:signifier_id>")
    def get_signifier(signifier_id: str):
        signifier_uri = URIRef(
            "http://localhost:5000/signifiers/" + signifier_id
        )  # TODO: update base if needed

        is_signifier = (signifier_uri, RDF.type, HMAS["Signifier"]) in envKG or any(
            envKG.triples((None, HMAS["exposesSignifier"], signifier_uri))
        )
        if not is_signifier:
            abort(404, "Signifier not found")

        # Extract the reachable subgraph starting from the signifier node.
        result = Graph()
        for s, p, o in envKG.triples((None, HMAS["exposesSignifier"], signifier_uri)):
            result.add((s, p, o))

        queue: list[URIRef | BNode] = [signifier_uri]
        seen = set()
        while queue:
            node = queue.pop()
            if node in seen:
                continue
            seen.add(node)
            for s, p, o in envKG.triples((node, None, None)):
                result.add((s, p, o))
                if isinstance(o, BNode) and o not in seen:
                    queue.append(o)

        bind_common_prefixes(result)
        mimetype, rdf_format = _preferred_serialization()
        serialized = result.serialize(format=rdf_format)
        return Response(serialized, mimetype=mimetype)

    @app.post("/signifiers/nl")
    def post_signifier_nl():
        """Create a signifier from natural-language context and behavior."""
        global envKG
        context_text, behavior_text = _parse_signifier_payload(
            request.get_data(), request.content_type
        )
        signifier_id = str(uuid.uuid4())
        signifier_uri = URIRef(f"http://localhost:5000/signifiers/{signifier_id}")
        signifier = Signifier(signifier_uri, Graph())
        signifier.add_nl_context(context_text)
        signifier.add_behavior(behavior_text)
        envKG += signifier.graph
        location_header = f"/signifiers/{signifier_id}"
        return Response(status=201, headers={"Location": location_header})

    return app


if __name__ != "__main__":
    sem = create_app()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run SEM app with runtime registration config.")
    parser.add_argument(
        "--app-config",
        default=DEFAULT_APP_CONFIG,
        help="Path to runtime app config JSON (default: config_app.json).",
    )
    args = parser.parse_args()
    sem = create_app(args.app_config)
    sem.run(threaded=True)
