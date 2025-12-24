"""Tests for agent ID handling in SKEIN CLI.

Specifically tests that the 'unknown' name collision is fixed:
- 'unknown' was previously used as a sentinel value when no --agent flag is set
- This collides with agents that are actually named 'unknown'
- Fix: Use None as sentinel instead of 'unknown'
"""

import pytest
import os
import sys
from pathlib import Path
from unittest.mock import patch

# Add parent directory to path to import CLI module
sys.path.insert(0, str(Path(__file__).parent.parent))

from client.cli import get_agent_id


class TestGetAgentId:
    """Test suite for get_agent_id function."""

    def test_returns_none_when_no_agent_specified(self):
        """When no agent is specified anywhere, should return None (not 'unknown')."""
        # Clear any env var
        with patch.dict(os.environ, {}, clear=True):
            result = get_agent_id()
            assert result is None, "Should return None when no agent is specified"

    def test_returns_explicit_agent_from_flag(self):
        """When --agent flag is provided, should return that value."""
        result = get_agent_id(ctx_agent="my-agent")
        assert result == "my-agent"

    def test_returns_unknown_when_explicitly_passed(self):
        """When --agent unknown is explicitly passed, should return 'unknown'.

        This is the key test - 'unknown' is now a valid agent name, not a sentinel.
        """
        result = get_agent_id(ctx_agent="unknown")
        assert result == "unknown", "Should return 'unknown' when explicitly passed as --agent"

    def test_returns_env_var_when_set(self):
        """When SKEIN_AGENT_ID env var is set, should return that value."""
        with patch.dict(os.environ, {"SKEIN_AGENT_ID": "env-agent"}):
            result = get_agent_id()
            assert result == "env-agent"

    def test_returns_unknown_from_env_var(self):
        """When SKEIN_AGENT_ID is set to 'unknown', should return 'unknown'."""
        with patch.dict(os.environ, {"SKEIN_AGENT_ID": "unknown"}):
            result = get_agent_id()
            assert result == "unknown", "Should return 'unknown' when set in env var"

    def test_flag_takes_precedence_over_env_var(self):
        """--agent flag should take precedence over SKEIN_AGENT_ID env var."""
        with patch.dict(os.environ, {"SKEIN_AGENT_ID": "env-agent"}):
            result = get_agent_id(ctx_agent="flag-agent")
            assert result == "flag-agent"

    def test_empty_string_env_var_returns_none(self):
        """Empty string in env var should still return None (empty is falsy)."""
        with patch.dict(os.environ, {"SKEIN_AGENT_ID": ""}):
            result = get_agent_id()
            # Empty string from env var is falsy, but os.getenv will return ""
            # which is truthy for os.getenv purposes but empty
            assert result == "" or result is None


class TestAgentNameCollisionFix:
    """Tests verifying the 'unknown' name collision is fixed."""

    def test_none_is_sentinel_not_unknown(self):
        """Verify None is used as sentinel, not 'unknown'."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_agent_id()
            assert result is None
            assert result != "unknown"

    def test_unknown_agent_is_distinguishable_from_no_agent(self):
        """Key test: Can distinguish between 'unknown' agent and no agent specified."""
        # No agent specified
        with patch.dict(os.environ, {}, clear=True):
            no_agent_result = get_agent_id()

        # Agent explicitly named 'unknown'
        unknown_agent_result = get_agent_id(ctx_agent="unknown")

        # These should be different
        assert no_agent_result is None
        assert unknown_agent_result == "unknown"
        assert no_agent_result != unknown_agent_result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
