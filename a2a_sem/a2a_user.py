#!/usr/bin/env python3
"""
a2a_user.py — a tiny CLI "A2A user" client.

Features:
  1) Read an agent card (/.well-known/agent-card.json)
  2) Send a message (at minimum: one text part)

Transports:
  - auto (default): pick from AgentCard.supportedInterfaces (preferred first)
  - jsonrpc: JSON-RPC 2.0 SendMessage
  - httpjson: HTTP+JSON/REST POST /message:send

Examples:
  python a2a_user.py https://agent.example.com card
  python a2a_user.py https://agent.example.com send "Hello there"
  python a2a_user.py https://agent.example.com send "Hi" --protocol jsonrpc
  python a2a_user.py https://agent.example.com send "Hi" --token "$BEARER_TOKEN"
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# ---------- HTTP helpers ----------


def _http_json(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body_obj: Optional[Dict[str, Any]] = None,
    timeout_s: int = 30,
) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
    headers = dict(headers or {})
    data: Optional[bytes] = None
    if body_obj is not None:
        data = json.dumps(body_obj).encode("utf-8")
        headers.setdefault("Content-Type", "application/a2a+json")
        headers.setdefault("Accept", "application/a2a+json")

    req = Request(url=url, data=data, method=method, headers=headers)

    try:
        with urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            if not raw.strip():
                return resp.status, {}, resp_headers
            return resp.status, json.loads(raw), resp_headers

    except HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {"_raw": raw}
        resp_headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        return e.code, payload, resp_headers

    except URLError as e:
        raise RuntimeError(f"Network error calling {url}: {e}") from e


def _origin_from_any_url(endpoint: str) -> str:
    """
    A2A Agent Cards are discovered at:
      https://{server_domain}/.well-known/agent-card.json
    So if the user passes https://host/some/path, we still fetch from https://host/.
    """
    p = urlparse(endpoint)
    if not p.scheme or not p.netloc:
        raise ValueError(f"Endpoint must be an absolute URL (got: {endpoint!r})")
    return f"{p.scheme}://{p.netloc}"


# ---------- A2A interface selection ----------


@dataclass(frozen=True)
class A2AInterface:
    url: str
    protocol_binding: str  # "JSONRPC" | "HTTP+JSON" | "GRPC" | ...
    tenant: Optional[str] = None


def _pick_interface(agent_card: Dict[str, Any], preferred: str = "auto") -> Optional[A2AInterface]:
    """
    preferred:
      - auto: pick first supported interface
      - jsonrpc: pick first protocolBinding == JSONRPC
      - httpjson: pick first protocolBinding == HTTP+JSON
    """
    interfaces = agent_card.get("supportedInterfaces") or []
    parsed: list[A2AInterface] = []
    for it in interfaces:
        if not isinstance(it, dict):
            continue
        url = it.get("url")
        pb = it.get("protocolBinding")
        tenant = it.get("tenant")
        if isinstance(url, str) and isinstance(pb, str):
            parsed.append(
                A2AInterface(
                    url=url, protocol_binding=pb, tenant=tenant if isinstance(tenant, str) else None
                )
            )

    if not parsed:
        return None

    if preferred == "auto":
        return parsed[0]

    want = {"jsonrpc": "JSONRPC", "httpjson": "HTTP+JSON"}.get(preferred)
    for it in parsed:
        if it.protocol_binding.upper() == want:
            return it
    return None


# ---------- A2A operations ----------


def fetch_agent_card(endpoint: str, token: Optional[str] = None) -> Dict[str, Any]:
    origin = _origin_from_any_url(endpoint)
    card_url = urljoin(origin + "/", ".well-known/agent-card.json")
    headers: Dict[str, str] = {"Accept": "application/a2a+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, payload, _ = _http_json(card_url, method="GET", headers=headers)
    if status >= 400:
        raise RuntimeError(f"Failed to fetch Agent Card ({status}) from {card_url}: {payload}")
    return payload


def send_text_message(
    endpoint: str,
    text: str,
    token: Optional[str] = None,
    protocol: str = "auto",
    a2a_version: Optional[str] = None,
) -> Dict[str, Any]:
    card = fetch_agent_card(endpoint, token=token)

    iface = _pick_interface(card, preferred=protocol)
    # If no interfaces are advertised, fall back to the user-provided endpoint with JSON-RPC.
    if iface is None:
        iface = A2AInterface(url=endpoint, protocol_binding="JSONRPC", tenant=None)

    # Pick a version header (recommended by spec). Prefer caller override, else first advertised protocolVersion.
    version = a2a_version
    if version is None:
        pv = card.get("protocolVersions")
        if isinstance(pv, list) and pv and isinstance(pv[0], str):
            version = pv[0]

    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if version:
        headers["A2A-Version"] = version

    msg = {
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "parts": [{"text": text}],
    }

    # Tenant (if present) goes into the SendMessageRequest object.
    params: Dict[str, Any] = {"message": msg}
    if iface.tenant:
        params["tenant"] = iface.tenant

    pb = iface.protocol_binding.upper()

    if pb == "HTTP+JSON":
        # REST binding: POST {base}/message:send
        url = urljoin(iface.url.rstrip("/") + "/", "message:send")
        status, payload, _ = _http_json(url, method="POST", headers=headers, body_obj=params)
        if status >= 400:
            raise RuntimeError(f"HTTP+JSON SendMessage failed ({status}) at {url}: {payload}")
        return payload

    if pb == "JSONRPC":
        # JSON-RPC binding: POST to iface.url (some servers mount at /rpc; the Agent Card should tell you).
        url = iface.url
        req_obj = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": params,
        }
        # JSON-RPC uses application/json
        headers2 = dict(headers)
        headers2.setdefault("Content-Type", "application/json")
        headers2.setdefault("Accept", "application/json")
        status, payload, _ = _http_json(url, method="POST", headers=headers2, body_obj=req_obj)
        if status >= 400:
            raise RuntimeError(f"JSON-RPC SendMessage failed ({status}) at {url}: {payload}")
        # JSON-RPC success shape: {"jsonrpc":"2.0","id":...,"result":{...}}
        if "error" in payload:
            raise RuntimeError(f"JSON-RPC error from {url}: {payload['error']}")
        return payload.get("result", payload)

    raise RuntimeError(
        f"Unsupported protocolBinding {iface.protocol_binding!r}. "
        f"Try --protocol jsonrpc or --protocol httpjson if the agent supports it."
    )


# ---------- CLI ----------


def _pretty(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="a2a_user.py",
        description="A tiny A2A 'user' client: fetch Agent Card and send messages.",
    )
    parser.add_argument(
        "endpoint",
        help="A2A server endpoint URL (any URL on the server; Agent Card is fetched from /.well-known/agent-card.json).",
    )
    parser.add_argument(
        "--token",
        help="Optional Bearer token for Authorization header.",
        default=None,
    )
    parser.add_argument(
        "--protocol",
        choices=["auto", "jsonrpc", "httpjson"],
        default="auto",
        help="Which protocol binding to use for send: auto (from Agent Card), jsonrpc, or httpjson.",
    )
    parser.add_argument(
        "--a2a-version",
        default=None,
        help="Optional A2A-Version header value. If omitted, uses the first protocolVersions entry from the Agent Card (if present).",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_card = sub.add_parser("card", help="Fetch and print the Agent Card.")
    p_card.add_argument("--raw", action="store_true", help="Print raw JSON (still JSON formatted).")

    p_send = sub.add_parser("send", help="Send a single-part text message.")
    p_send.add_argument("text", help="Text to send as one message part.")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "card":
            card = fetch_agent_card(args.endpoint, token=args.token)
            print(_pretty(card) if not args.raw else json.dumps(card))
            return 0

        if args.cmd == "send":
            resp = send_text_message(
                args.endpoint,
                args.text,
                token=args.token,
                protocol=args.protocol,
                a2a_version=args.a2a_version,
            )
            print(_pretty(resp))
            return 0

        parser.error("No command provided.")
        return 2

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
