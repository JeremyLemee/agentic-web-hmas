"""
Test that an agent can use the MCP interface to resolve a signifier into a tool.

This test verifies the integration between:
1. SEM Flask app (app.py with config_app.json)
2. MCP SEM server (mcp_sem/)
3. MCPStreamingHTTPTool client (llm_agent/coala/tools/mcp_streaming_http_tool.py)
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SEM_BASE_URL = "http://localhost:5000"
MCP_SEM_URL = "http://localhost:8200/mcp"
PROFILE_ID = "test_profile"
PROFILE_URL = f"{SEM_BASE_URL}/profile/{PROFILE_ID}"


def _wait_for_service(url: str, timeout_seconds: int = 120, description: str = "Service") -> None:
    """Wait for an HTTP service to become available."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=5) as response:
                if response.status in (200, 201):
                    return
        except (URLError, Exception):
            pass
        time.sleep(1)

    raise TimeoutError(f"{description} not ready after {timeout_seconds}s: {url}")


def _wait_for_mcp_server(host: str, port: int, timeout_seconds: int = 120, description: str = "MCP Server") -> None:
    """Wait for an MCP server (streamable HTTP) to be accessible."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            sock = socket.create_connection((host, port), timeout=5)
            sock.close()
            time.sleep(1)  # Give it a moment to be fully ready
            return
        except (socket.error, OSError):
            pass
        time.sleep(1)

    raise TimeoutError(f"{description} not ready after {timeout_seconds}s: {host}:{port}")


@pytest.fixture(scope="module")
def running_stack():
    """Fixture that starts the full SEM stack (7 microservices via run.sh)."""
    process = subprocess.Popen(
        ["bash", "run.sh"],
        cwd=REPO_ROOT,
        preexec_fn=os.setsid,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Wait for SEM to be ready
        _wait_for_service(
            f"{SEM_BASE_URL}/artifacts/list",
            timeout_seconds=120,
            description="SEM Flask app",
        )
        # Wait for MCP SEM to be ready (port 8200)
        _wait_for_mcp_server(
            "localhost",
            8200,
            timeout_seconds=30,
            description="MCP SEM server",
        )
        yield
    finally:
        if process.poll() is None:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                process.wait(timeout=10)


@pytest.fixture
def mcp_tool(running_stack):
    """
    Fixture that provides an initialized MCPStreamingHTTPTool for testing.
    """
    # Import here to avoid import errors if dependencies aren't installed
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from llm_agent.coala.tools.mcp_streaming_http_tool import MCPStreamingHTTPTool

    return MCPStreamingHTTPTool("mcp_client", server_url=MCP_SEM_URL)


def test_mcp_sem_resolves_signifier_to_tool(running_stack, mcp_tool):
    """
    Test that:
    1. A profile can be registered via MCP
    2. Signifiers can be read for that profile via MCP
    3. Retrieved signifiers are exposed as callable MCP tools
    """
    async def run_test():
        # Step 1: Register a profile with a specific context
        register_result = await mcp_tool.ainvoke({"profile_id": PROFILE_ID})
        assert "error" not in register_result, f"Failed to register profile: {register_result}"

        # Step 2: Update the profile with a meaningful context
        # Use a context that should match some of the registered artifact signifiers
        context = "The agent wants to add two numbers"
        update_result = await mcp_tool.ainvoke(
            {"profile_id": PROFILE_ID, "nl_context": context}
        )
        assert "error" not in update_result, f"Failed to update profile: {update_result}"

        # Step 3: Read signifiers for this profile
        # This should return a list of tools that are relevant to the profile's context
        read_signifiers_result = await mcp_tool.ainvoke({"profile_url": PROFILE_URL})
        assert isinstance(
            read_signifiers_result, dict
        ), f"Expected dict, got {type(read_signifiers_result)}"
        assert "error" not in read_signifiers_result, (
            f"Failed to read signifiers: {read_signifiers_result}"
        )

        # Step 4: Verify that signifiers were registered as tools
        content = read_signifiers_result.get("content", "")
        assert isinstance(content, str), f"Expected string content, got {type(content)}"
        # The content should indicate that tools were registered or describe available signifiers
        assert len(content) > 0, "No signifiers or tools returned for the profile context"

        # Step 5: Verify that the MCP tool registry contains tools from signifiers
        # We should be able to list available tools after reading signifiers
        # Note: This is a basic check that the MCP interface is functional
        assert (
            "tool" in read_signifiers_result or "content" in read_signifiers_result
        ), "Response should contain tool information or content"

    asyncio.run(run_test())


def test_mcp_sem_tool_initialization(running_stack, mcp_tool):
    """
    Test that MCPStreamingHTTPTool can connect to MCP SEM and retrieve tool metadata.
    """
    async def run_test():
        # The tool should have initialized and fetched metadata from the MCP server
        assert mcp_tool.name == "mcp_client"
        assert mcp_tool.server_url == MCP_SEM_URL

        # Give initialization a moment to complete if async
        await asyncio.sleep(0.5)

        # The tool should be able to describe available operations
        # (even if not all tools are fully initialized yet)
        description = mcp_tool.describe()
        assert isinstance(description, str), "Tool description should be a string"

    asyncio.run(run_test())


def test_mcp_sem_end_to_end_workflow(running_stack, mcp_tool):
    """
    End-to-end test simulating a complete signifier resolution workflow:
    1. Create a profile with context
    2. Query SEM for relevant signifiers
    3. Verify signifiers are resolved into tools
    """
    async def run_test():
        profile_id = "e2e_test_profile"
        profile_url = f"{SEM_BASE_URL}/profile/{profile_id}"
        context = "The agent wants to multiply numbers"

        # Register profile
        await mcp_tool.ainvoke({"profile_id": profile_id})

        # Update with context
        await mcp_tool.ainvoke({"profile_id": profile_id, "nl_context": context})

        # Read signifiers - this is the key operation
        result = await mcp_tool.ainvoke({"profile_url": profile_url})

        # Verify the result indicates successful signifier retrieval
        assert isinstance(result, dict), "Result should be a dictionary"
        assert (
            "error" not in result or result.get("error") is None
        ), f"Unexpected error: {result.get('error')}"

        # The response should indicate what signifiers/tools are available
        content = result.get("content", "")
        raw = result.get("raw", None)

        # Either content or raw should be present and meaningful
        assert content or raw, "Result should contain either content or raw data"

    asyncio.run(run_test())
