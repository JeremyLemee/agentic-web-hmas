import json
import os
import signal
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SEM_BASE_URL = "http://localhost:5000"
ARTIFACT_LIST_URL = f"{SEM_BASE_URL}/artifacts/list"

EXPECTED_ARTIFACT_PROFILE_URLS = {
    f"{SEM_BASE_URL}/artifacts/goal_mcp",
    f"{SEM_BASE_URL}/artifacts/formalizer",
    f"{SEM_BASE_URL}/artifacts/cherrybot_utcp",
}


def _wait_for_artifact_list(process: subprocess.Popen, timeout_seconds: int = 120) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("run.sh terminated before SEM became ready")
        try:
            with urlopen(ARTIFACT_LIST_URL, timeout=5) as response:
                if response.status == 200:
                    return
        except URLError:
            pass
        time.sleep(2)

    raise TimeoutError(f"SEM endpoint not ready after {timeout_seconds}s: {ARTIFACT_LIST_URL}")


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
        _wait_for_artifact_list(process)
        yield
    finally:
        if process.poll() is None:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                process.wait(timeout=10)


def test_artifacts_list_contains_registered_profiles(running_stack):
    with urlopen(ARTIFACT_LIST_URL, timeout=10) as response:
        assert response.status == 200
        payload = json.loads(response.read().decode("utf-8"))

    assert isinstance(payload, list)
    assert EXPECTED_ARTIFACT_PROFILE_URLS.issubset(set(payload))
