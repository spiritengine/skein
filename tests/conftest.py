"""Pytest configuration and fixtures for SKEIN tests."""

import json
import os
import sys
import tempfile
import shutil
from pathlib import Path
from typing import Generator

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def pytest_configure(config):
    """Setup test project registry before tests run."""
    # Create test project directory
    test_project_dir = Path("/tmp/skein-test/.skein/data")
    test_project_dir.mkdir(parents=True, exist_ok=True)

    # Create global registry
    skein_home = Path.home() / ".skein"
    skein_home.mkdir(exist_ok=True)

    registry_file = skein_home / "projects.json"

    # Load existing registry or create new
    if registry_file.exists():
        with open(registry_file) as f:
            registry = json.load(f)
    else:
        registry = {"projects": {}}

    # Add test project
    registry["projects"]["test-project"] = {
        "data_dir": str(test_project_dir),
        "name": "test-project"
    }

    # Save registry
    with open(registry_file, "w") as f:
        json.dump(registry, f, indent=2)


@pytest.fixture
def temp_project() -> Generator[Path, None, None]:
    """Create a temporary project directory with .skein folder.

    This fixture creates a mock project environment for testing
    SKEIN operations that require project-specific storage.
    """
    temp_dir = tempfile.mkdtemp(prefix="skein_test_")
    skein_dir = Path(temp_dir) / ".skein"
    skein_dir.mkdir()

    # Create basic project structure
    (skein_dir / "data").mkdir()

    yield Path(temp_dir)

    # Cleanup
    shutil.rmtree(temp_dir)


@pytest.fixture
def test_agent_id() -> str:
    """Return a consistent test agent ID."""
    return "test-agent-001"


@pytest.fixture
def test_project_id() -> str:
    """Return a consistent test project ID."""
    return "test-project"


@pytest.fixture
def test_headers(test_project_id: str, test_agent_id: str) -> dict:
    """Return standard test headers with project and agent IDs."""
    return {
        "X-Project-Id": test_project_id,
        "X-Agent-Id": test_agent_id
    }


@pytest.fixture
def base_url() -> str:
    """Return the base URL for the SKEIN API."""
    port = os.getenv("SKEIN_TEST_PORT", "8001")
    return f"http://localhost:{port}/skein"
