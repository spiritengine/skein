"""Tests for yield/sack storage functionality."""

import json
import tempfile
from pathlib import Path

import pytest

from skein.storage import LogDatabase
from skein.models import YieldCreate, Yield


@pytest.fixture
def test_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)

    db = LogDatabase(db_path)
    yield db

    # Cleanup
    db_path.unlink(missing_ok=True)


class TestSackStorage:
    """Test sack/yield storage operations."""

    def test_add_yield_basic(self, test_db):
        """Test adding a basic yield."""
        result = test_db.add_yield(
            sack_id="yield-20251206-test",
            chain_id="chain-daily_triage-20251206-120000",
            task_id="task_0001",
            agent_id="test-agent",
            status="complete",
            outcome="Fixed the bug",
        )
        assert result is True

    def test_add_yield_with_artifacts(self, test_db):
        """Test adding a yield with artifacts list."""
        result = test_db.add_yield(
            sack_id="yield-20251206-art1",
            chain_id="chain-test-20251206",
            task_id="task_0001",
            agent_id="test-agent",
            status="complete",
            outcome="Completed analysis",
            artifacts=["finding-20251206-abc1", "tender-20251206-def2"],
            notes="Heads up: the fix is invasive",
        )
        assert result is True

        # Retrieve and verify
        yield_data = test_db.get_yield("yield-20251206-art1")
        assert yield_data is not None
        assert yield_data['artifacts'] == ["finding-20251206-abc1", "tender-20251206-def2"]
        assert yield_data['notes'] == "Heads up: the fix is invasive"

    def test_add_yield_with_enrichment(self, test_db):
        """Test adding a yield with Mill enrichment fields."""
        result = test_db.add_yield(
            sack_id="yield-20251206-enr1",
            chain_id="chain-test-20251206",
            task_id="task_0001",
            agent_id="test-agent",
            status="complete",
            outcome="Task done",
            duration_seconds=120,
            tokens_used=5000,
            shard_path="/home/user/projects/repo/worktrees/shard-abc",
            tender_id="tender-20251206-xyz1",
        )
        assert result is True

        # Retrieve and verify
        yield_data = test_db.get_yield("yield-20251206-enr1")
        assert yield_data is not None
        assert yield_data['duration_seconds'] == 120
        assert yield_data['tokens_used'] == 5000
        assert yield_data['shard_path'] == "/home/user/projects/repo/worktrees/shard-abc"
        assert yield_data['tender_id'] == "tender-20251206-xyz1"

    def test_get_chain_yields_ordering(self, test_db):
        """Test getting yields in chain order."""
        import time

        # Add yields in reverse order with slight delay
        test_db.add_yield(
            sack_id="yield-chain-3",
            chain_id="chain-ordering-test",
            task_id="task_0003",
            status="complete",
            outcome="Third task",
        )
        time.sleep(0.01)  # Ensure timestamp difference

        test_db.add_yield(
            sack_id="yield-chain-1",
            chain_id="chain-ordering-test",
            task_id="task_0001",
            status="complete",
            outcome="First task",
        )
        time.sleep(0.01)

        test_db.add_yield(
            sack_id="yield-chain-2",
            chain_id="chain-ordering-test",
            task_id="task_0002",
            status="partial",
            outcome="Second task",
        )

        # Get chain yields (should be in timestamp order, not insert order)
        yields = test_db.get_chain_yields("chain-ordering-test")
        assert len(yields) == 3
        # Note: ordering is by timestamp, so will be 3, 1, 2
        assert yields[0]['sack_id'] == "yield-chain-3"
        assert yields[1]['sack_id'] == "yield-chain-1"
        assert yields[2]['sack_id'] == "yield-chain-2"

    def test_get_yields_by_status(self, test_db):
        """Test filtering yields by status."""
        # Add yields with different statuses
        test_db.add_yield(
            sack_id="yield-status-1",
            chain_id="chain-1",
            task_id="task_1",
            status="complete",
            outcome="Done",
        )
        test_db.add_yield(
            sack_id="yield-status-2",
            chain_id="chain-2",
            task_id="task_2",
            status="blocked",
            outcome="Needs human review",
        )
        test_db.add_yield(
            sack_id="yield-status-3",
            chain_id="chain-3",
            task_id="task_3",
            status="blocked",
            outcome="Also blocked",
        )

        blocked = test_db.get_yields_by_status("blocked")
        assert len(blocked) == 2
        assert all(y['status'] == 'blocked' for y in blocked)

        complete = test_db.get_yields_by_status("complete")
        assert len(complete) == 1
        assert complete[0]['status'] == 'complete'

    def test_get_agent_yields(self, test_db):
        """Test getting yields by agent."""
        test_db.add_yield(
            sack_id="yield-agent-1",
            chain_id="chain-1",
            task_id="task_1",
            agent_id="agent-alice",
            status="complete",
            outcome="Alice's work",
        )
        test_db.add_yield(
            sack_id="yield-agent-2",
            chain_id="chain-1",
            task_id="task_2",
            agent_id="agent-bob",
            status="complete",
            outcome="Bob's work",
        )
        test_db.add_yield(
            sack_id="yield-agent-3",
            chain_id="chain-2",
            task_id="task_3",
            agent_id="agent-alice",
            status="partial",
            outcome="More Alice work",
        )

        alice_yields = test_db.get_agent_yields("agent-alice")
        assert len(alice_yields) == 2
        assert all(y['agent_id'] == 'agent-alice' for y in alice_yields)

    def test_get_previous_yield(self, test_db):
        """Test getting previous yield in a chain."""
        import time

        # Add chain yields in order
        test_db.add_yield(
            sack_id="yield-prev-1",
            chain_id="chain-sequential",
            task_id="task_0001",
            status="complete",
            outcome="First",
            notes="Context for second",
        )
        time.sleep(0.01)

        test_db.add_yield(
            sack_id="yield-prev-2",
            chain_id="chain-sequential",
            task_id="task_0002",
            status="complete",
            outcome="Second",
        )
        time.sleep(0.01)

        test_db.add_yield(
            sack_id="yield-prev-3",
            chain_id="chain-sequential",
            task_id="task_0003",
            status="complete",
            outcome="Third",
        )

        # Get previous yield before task_0003
        prev = test_db.get_previous_yield("chain-sequential", "task_0003")
        assert prev is not None
        assert prev['task_id'] == "task_0002"

        # Get previous yield before task_0002
        prev = test_db.get_previous_yield("chain-sequential", "task_0002")
        assert prev is not None
        assert prev['task_id'] == "task_0001"
        assert prev['notes'] == "Context for second"

        # Get previous yield before first task (should be None)
        prev = test_db.get_previous_yield("chain-sequential", "task_0001")
        assert prev is None

    def test_get_nonexistent_yield(self, test_db):
        """Test getting a yield that doesn't exist."""
        result = test_db.get_yield("yield-does-not-exist")
        assert result is None

    def test_get_empty_chain_yields(self, test_db):
        """Test getting yields from an empty/nonexistent chain."""
        yields = test_db.get_chain_yields("chain-does-not-exist")
        assert yields == []
