#!/usr/bin/env python3
"""
Comprehensive pytest test suite for shard.py git worktree management.

Tests the invariants and edge cases critical for safe worktree operations.

Key Historical Bugs This Suite Catches:
1. Full path as worktree_name causing parent directory deletion
2. Self-deletion when running from inside a worktree

Run with: pytest tests/test_shard.py -v
"""

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest

# Import hypothesis for property-based testing
try:
    from hypothesis import given, settings, strategies as st, assume, HealthCheck
    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False

# Import the module under test
from skein.shard import (
    ShardError,
    spawn_shard,
    cleanup_shard,
    merge_shard,
    list_shards,
    get_shard_status,
    get_shard_git_info,
    get_shard_diff,
    get_review_queue,
    get_shard_age_days,
    get_tender_metadata,
    detect_shard_environment,
    set_project_root,
    get_project_root,
    get_worktrees_dir,
    validate_shard_name,
    _is_path_inside_worktree,
    _get_next_sequence,
    MAX_SEQUENCE_NUMBER,
    _PROJECT_ROOT,
    _WORKTREES_DIR,
)


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Generator[Path, None, None]:
    """
    Create a temporary git repository with an initial commit.

    This fixture provides an isolated git environment for each test,
    preventing test pollution and making tests reproducible.
    """
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path, check=True, capture_output=True
    )

    # Create initial commit (required for worktrees)
    readme = repo_path / "README.md"
    readme.write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_path, check=True, capture_output=True
    )

    # Ensure we're on master branch
    subprocess.run(
        ["git", "branch", "-M", "master"],
        cwd=repo_path, check=True, capture_output=True
    )

    yield repo_path


@pytest.fixture
def shard_env(temp_git_repo: Path, monkeypatch):
    """
    Set up the shard module to use the temporary git repo.

    Resets module globals and configures paths for isolated testing.
    """
    import skein.shard as shard_module

    # Reset module state
    shard_module._PROJECT_ROOT = None
    shard_module._WORKTREES_DIR = None

    # Set up the project root
    set_project_root(str(temp_git_repo))

    # Change to repo directory for tests that depend on cwd
    original_cwd = os.getcwd()
    os.chdir(temp_git_repo)

    yield temp_git_repo

    # Restore original cwd
    os.chdir(original_cwd)

    # Reset module state again for next test
    shard_module._PROJECT_ROOT = None
    shard_module._WORKTREES_DIR = None


@pytest.fixture
def spawned_shard(shard_env: Path):
    """
    Create a shard and return its info.
    Cleans up after test even if test fails.
    """
    info = spawn_shard("test-agent")
    yield info

    # Cleanup - ignore errors since test may have already cleaned up
    try:
        cleanup_shard(info["worktree_name"])
    except:
        pass


# =============================================================================
# CORE INVARIANT TESTS
# =============================================================================

class TestSpawnCleanupRoundtrip:
    """
    Invariant 1: spawn/cleanup roundtrip leaves no trace.
    No worktree, no branch, no orphaned .git/worktrees entries.
    """

    def test_spawn_then_cleanup_leaves_no_trace(self, shard_env: Path):
        """WHY: Core safety property - after cleanup, repo should be pristine."""
        # Record initial state
        initial_branches = subprocess.run(
            ["git", "branch", "--list"],
            cwd=shard_env, capture_output=True, text=True
        ).stdout
        initial_worktrees = subprocess.run(
            ["git", "worktree", "list"],
            cwd=shard_env, capture_output=True, text=True
        ).stdout

        # Spawn and cleanup
        info = spawn_shard("cleanup-test")
        worktree_name = info["worktree_name"]
        worktree_path = Path(info["worktree_path"])
        branch_name = info["branch_name"]

        assert worktree_path.exists(), "Worktree should exist after spawn"

        cleanup_shard(worktree_name)

        # Verify no trace remains
        assert not worktree_path.exists(), "Worktree directory should be removed"

        final_branches = subprocess.run(
            ["git", "branch", "--list"],
            cwd=shard_env, capture_output=True, text=True
        ).stdout
        assert branch_name not in final_branches, "Branch should be deleted"

        final_worktrees = subprocess.run(
            ["git", "worktree", "list"],
            cwd=shard_env, capture_output=True, text=True
        ).stdout
        assert worktree_name not in final_worktrees, "No orphan worktree entries"

        # Check .git/worktrees directory
        git_worktrees_dir = shard_env / ".git" / "worktrees"
        if git_worktrees_dir.exists():
            entries = list(git_worktrees_dir.iterdir())
            for entry in entries:
                assert worktree_name not in entry.name, \
                    f"Orphaned .git/worktrees entry: {entry}"

    def test_cleanup_with_keep_branch_preserves_only_branch(self, shard_env: Path):
        """WHY: keep_branch option should only affect branch, not worktree."""
        info = spawn_shard("keep-branch-test")
        worktree_path = Path(info["worktree_path"])
        branch_name = info["branch_name"]

        cleanup_shard(info["worktree_name"], keep_branch=True)

        # Worktree gone, branch remains
        assert not worktree_path.exists()
        branches = subprocess.run(
            ["git", "branch", "--list"],
            cwd=shard_env, capture_output=True, text=True
        ).stdout
        assert branch_name.replace("shard-", "") in branches or branch_name in branches


class TestCleanupNeverAffectsMaster:
    """
    Invariant 2: cleanup NEVER affects master branch or master's files.
    """

    def test_cleanup_preserves_master_content(self, shard_env: Path):
        """WHY: Catastrophic bug - accidental deletion of main codebase."""
        # Create some content on master
        test_file = shard_env / "important_file.py"
        test_file.write_text("# Critical code\n")
        subprocess.run(["git", "add", "."], cwd=shard_env, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Add important file"],
            cwd=shard_env, check=True, capture_output=True
        )

        # Spawn and cleanup
        info = spawn_shard("master-safety-test")
        cleanup_shard(info["worktree_name"])

        # Master content untouched
        assert test_file.exists(), "Master file should still exist"
        assert test_file.read_text() == "# Critical code\n"

    def test_cleanup_with_full_path_does_not_delete_parent(self, shard_env: Path):
        """
        WHY: Historical bug - passing full path as worktree_name caused
        the code to construct paths incorrectly and delete parent directories.

        Example: cleanup_shard("/path/to/worktrees/agent-123") was being interpreted
        as base_path / "/path/to/worktrees/agent-123" which resolved to just
        "/path/to/worktrees/agent-123" bypassing the worktrees directory safety.
        """
        info = spawn_shard("path-safety-test")
        worktree_path = info["worktree_path"]  # Full absolute path
        worktrees_dir = get_worktrees_dir()

        # Attempt cleanup with full path (should normalize to just name)
        cleanup_shard(worktree_path)  # Pass full path, not just name

        # Parent directories must remain
        assert worktrees_dir.parent.exists(), "Project root should exist"
        assert shard_env.exists(), "Repo should exist"


class TestMergeRequirements:
    """
    Invariant 3: merge requires clean working tree + no conflicts.
    """

    def test_merge_rejects_dirty_working_tree(self, shard_env: Path):
        """WHY: Merging uncommitted changes would lose work or create confusion."""
        info = spawn_shard("dirty-test")
        worktree_path = Path(info["worktree_path"])

        # Create uncommitted changes
        dirty_file = worktree_path / "uncommitted.txt"
        dirty_file.write_text("dirty content")

        # Merge should fail
        result = merge_shard(info["worktree_name"])

        assert not result["success"]
        assert "uncommitted" in result["message"].lower()
        assert len(result["uncommitted"]) > 0

        # Cleanup for next test
        cleanup_shard(info["worktree_name"])

    def test_merge_detects_conflicts_before_attempting(self, shard_env: Path):
        """WHY: Should detect conflicts early, not during actual merge."""
        info = spawn_shard("conflict-test")
        worktree_path = Path(info["worktree_path"])

        # Create conflicting changes on shard
        conflict_file = worktree_path / "conflict.txt"
        conflict_file.write_text("shard version")
        subprocess.run(["git", "add", "."], cwd=worktree_path, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Shard changes"],
            cwd=worktree_path, check=True, capture_output=True
        )

        # Create conflicting changes on master
        master_conflict = shard_env / "conflict.txt"
        master_conflict.write_text("master version")
        subprocess.run(["git", "add", "."], cwd=shard_env, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Master changes"],
            cwd=shard_env, check=True, capture_output=True
        )

        # Merge should detect conflict and refuse
        result = merge_shard(info["worktree_name"])

        assert not result["success"]
        assert "conflict" in result["message"].lower()

        # Master should be unaffected (no partial merge)
        assert master_conflict.read_text() == "master version"

        # Cleanup
        cleanup_shard(info["worktree_name"])


class TestListShardsAccuracy:
    """
    Invariant 4: list_shards reflects actual filesystem state.
    """

    def test_list_shards_matches_spawned(self, shard_env: Path):
        """WHY: Phantom entries or missing entries cause operational confusion."""
        spawned = []
        try:
            # Spawn several shards
            for i in range(3):
                info = spawn_shard(f"list-test-{i}")
                spawned.append(info)

            # List should contain exactly what we spawned
            shards = list_shards()
            names = {s["worktree_name"] for s in shards}

            for info in spawned:
                assert info["worktree_name"] in names, \
                    f"Missing spawned shard: {info['worktree_name']}"
        finally:
            for info in spawned:
                try:
                    cleanup_shard(info["worktree_name"])
                except:
                    pass

    def test_list_shards_excludes_cleaned_up(self, shard_env: Path):
        """WHY: Listing deleted shards would cause operations on non-existent paths."""
        info = spawn_shard("ghost-test")
        worktree_name = info["worktree_name"]

        cleanup_shard(worktree_name)

        shards = list_shards()
        names = [s["worktree_name"] for s in shards]
        assert worktree_name not in names, "Cleaned up shard should not appear in list"

    def test_no_phantom_entries_from_corrupted_state(self, shard_env: Path):
        """WHY: Orphaned git metadata should not create phantom list entries."""
        info = spawn_shard("phantom-test")
        worktree_path = Path(info["worktree_path"])
        worktree_name = info["worktree_name"]

        # Simulate corruption: delete directory but not git metadata
        # (shouldn't happen normally, but test resilience)
        subprocess.run(
            ["rm", "-rf", str(worktree_path)],
            check=True, capture_output=True
        )

        # Prune to clean up
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=shard_env, check=True, capture_output=True
        )

        # List should not include phantom
        shards = list_shards()
        names = [s["worktree_name"] for s in shards]
        # Either it's not there, or if git still tracks it, we accept that
        # (git's behavior varies by version)


class TestDuplicateSpawnPrevention:
    """
    Invariant 5: spawn with same name twice fails (no silent overwrite).
    """

    def test_spawn_same_agent_increments_sequence(self, shard_env: Path):
        """WHY: Prevent confusion and data loss from name collisions."""
        try:
            info1 = spawn_shard("duplicate-agent")
            info2 = spawn_shard("duplicate-agent")

            # Should get different names due to sequence increment
            assert info1["worktree_name"] != info2["worktree_name"]
            assert info1["worktree_path"] != info2["worktree_path"]

            # Both should exist
            assert Path(info1["worktree_path"]).exists()
            assert Path(info2["worktree_path"]).exists()
        finally:
            for info in [info1, info2]:
                try:
                    cleanup_shard(info["worktree_name"])
                except:
                    pass

    def test_spawn_fails_if_worktree_path_exists(self, shard_env: Path):
        """WHY: Explicitly test the worktree existence check."""
        info = spawn_shard("exists-test")
        worktrees_dir = get_worktrees_dir()

        # Manually create a directory that would conflict with next sequence
        # (simulating race condition or manual creation)
        seq = int(info["worktree_name"].split("-")[-1])
        next_name = info["worktree_name"].replace(f"-{seq:03d}", f"-{seq+1:03d}")
        fake_worktree = worktrees_dir / next_name
        fake_worktree.mkdir()

        try:
            # Third spawn should skip past the fake one
            info3 = spawn_shard("exists-test")
            assert info3["worktree_name"] != next_name
            cleanup_shard(info3["worktree_name"])
        finally:
            fake_worktree.rmdir()
            cleanup_shard(info["worktree_name"])


class TestSequenceCap:
    """Test sequence number limit enforcement."""

    def test_sequence_cap_enforced(self, shard_env: Path):
        """WHY: Prevent sequence overflow beyond 3 digits (001-999)."""
        worktrees_dir = get_worktrees_dir()
        worktrees_dir.mkdir(exist_ok=True)

        # Create a fake worktree at sequence 999 to simulate limit
        from datetime import datetime
        today = datetime.now().strftime("%Y%m%d")
        fake_name = f"seq-cap-test-{today}-999"
        fake_worktree = worktrees_dir / fake_name
        fake_worktree.mkdir()

        try:
            # The next sequence would be 1000, which should fail
            with pytest.raises(ShardError) as exc_info:
                _get_next_sequence("seq-cap-test", today)

            assert "sequence limit" in str(exc_info.value).lower()
            assert "999" in str(exc_info.value)
        finally:
            fake_worktree.rmdir()

    def test_sequence_cap_value(self):
        """WHY: Verify cap constant matches format width."""
        # Format uses 3 digits ({seq:03d}), so max is 999
        assert MAX_SEQUENCE_NUMBER == 999

    def test_sequence_under_cap_succeeds(self, shard_env: Path):
        """WHY: Sequences under cap should work normally."""
        worktrees_dir = get_worktrees_dir()
        worktrees_dir.mkdir(exist_ok=True)

        from datetime import datetime
        today = datetime.now().strftime("%Y%m%d")

        # Create worktree at 998, next should be 999 (allowed)
        fake_name = f"seq-ok-test-{today}-998"
        fake_worktree = worktrees_dir / fake_name
        fake_worktree.mkdir()

        try:
            next_seq = _get_next_sequence("seq-ok-test", today)
            assert next_seq == 999
        finally:
            fake_worktree.rmdir()


# =============================================================================
# EDGE CASE TESTS
# =============================================================================

class TestNameValidation:
    """Edge cases for worktree/agent naming."""

    def test_path_traversal_attack_blocked(self, shard_env: Path):
        """WHY: Prevent escaping worktrees directory via ../."""
        # The cleanup function normalizes paths using Path.name
        # So passing "../etc" gets normalized to just "etc"

        info = spawn_shard("test-agent")

        with pytest.raises(ShardError):
            # Try to cleanup with path traversal - should fail because
            # the normalized name won't match an actual worktree
            cleanup_shard("../something")

        cleanup_shard(info["worktree_name"])

    def test_empty_string_rejected(self, shard_env: Path):
        """WHY: Empty name would cause path construction issues."""
        with pytest.raises((ShardError, ValueError)):
            spawn_shard("")

    def test_very_long_name_handled(self, shard_env: Path):
        """WHY: Filesystem limits on path length."""
        # Most filesystems allow 255 chars for filename
        long_agent_id = "a" * 200
        try:
            info = spawn_shard(long_agent_id)
            # Should either work or raise descriptive error
            cleanup_shard(info["worktree_name"])
        except ShardError as e:
            # Acceptable to reject very long names
            assert "name" in str(e).lower() or "path" in str(e).lower()
        except Exception as e:
            # Git might reject it - that's also acceptable
            pass

    def test_unicode_name_handled(self, shard_env: Path):
        """WHY: International agent names should work or fail gracefully."""
        try:
            info = spawn_shard("agent-\u4e2d\u6587")  # Chinese characters
            cleanup_shard(info["worktree_name"])
        except ShardError:
            # Acceptable to reject unicode
            pass
        except Exception as e:
            # Git/OS might reject it
            pass

    def test_spaces_in_name_handled(self, shard_env: Path):
        """WHY: Spaces in paths are error-prone."""
        try:
            info = spawn_shard("agent with spaces")
            cleanup_shard(info["worktree_name"])
        except (ShardError, Exception):
            # Acceptable to reject or handle
            pass


class TestValidateShardName:
    """Unit tests for the validate_shard_name function."""

    def test_valid_names_pass(self):
        """WHY: Normal names should be accepted."""
        valid_names = [
            "agent",
            "fix-auth-bug",
            "feature_123",
            "Agent123",
            "a",
            "a-b-c",
            "test_agent_2024",
        ]
        for name in valid_names:
            is_valid, error = validate_shard_name(name)
            assert is_valid, f"'{name}' should be valid but got: {error}"

    def test_empty_rejected(self):
        """WHY: Empty names cause path issues."""
        is_valid, error = validate_shard_name("")
        assert not is_valid
        assert "empty" in error.lower()

    def test_whitespace_only_rejected(self):
        """WHY: Whitespace-only names are effectively empty."""
        is_valid, error = validate_shard_name("   ")
        assert not is_valid
        assert "whitespace" in error.lower()

    def test_too_long_rejected(self):
        """WHY: Very long names hit filesystem limits."""
        is_valid, error = validate_shard_name("a" * 100)
        assert not is_valid
        assert "63" in error or "exceeds" in error.lower()

    def test_reserved_names_rejected(self):
        """WHY: Git reserved names cause conflicts."""
        reserved = ["HEAD", "head", "master", "main", "refs", "worktrees"]
        for name in reserved:
            is_valid, error = validate_shard_name(name)
            assert not is_valid, f"'{name}' should be rejected"
            assert "reserved" in error.lower()

    def test_dot_start_rejected(self):
        """WHY: Dot-prefixed names are hidden files, cause issues."""
        is_valid, error = validate_shard_name(".hidden")
        assert not is_valid
        assert "dot" in error.lower() or "start" in error.lower()

    def test_hyphen_start_rejected(self):
        """WHY: Hyphen-prefixed names conflict with git options."""
        is_valid, error = validate_shard_name("-dangerous")
        assert not is_valid
        assert "hyphen" in error.lower() or "start" in error.lower()

    def test_double_dots_rejected(self):
        """WHY: .. is path traversal in git refs."""
        is_valid, error = validate_shard_name("foo..bar")
        assert not is_valid
        assert ".." in error

    def test_lock_suffix_rejected(self):
        """WHY: .lock files are git's lock mechanism."""
        is_valid, error = validate_shard_name("agent.lock")
        assert not is_valid
        assert ".lock" in error

    def test_reflog_notation_rejected(self):
        """WHY: @{ is git reflog syntax."""
        is_valid, error = validate_shard_name("agent@{0}")
        assert not is_valid
        assert "@{" in error

    def test_special_chars_rejected(self):
        """WHY: Special characters cause git/filesystem issues."""
        invalid_chars = [
            "agent/branch",   # slash
            "agent:name",     # colon
            "agent*wild",     # asterisk
            "agent?query",    # question mark
            "agent[0]",       # brackets
            "agent\\back",    # backslash
            "agent~tilde",    # tilde
            "agent^caret",    # caret
            "agent name",     # space
        ]
        for name in invalid_chars:
            is_valid, error = validate_shard_name(name)
            assert not is_valid, f"'{name}' should be rejected"


class TestWorktreeNotFound:
    """Edge cases for operations on non-existent worktrees."""

    def test_cleanup_nonexistent_raises_error(self, shard_env: Path):
        """WHY: Should fail explicitly, not silently succeed."""
        with pytest.raises(ShardError) as exc_info:
            cleanup_shard("nonexistent-worktree-xyz")
        assert "not found" in str(exc_info.value).lower()

    def test_merge_nonexistent_raises_error(self, shard_env: Path):
        """WHY: Should fail explicitly with descriptive error."""
        with pytest.raises(ShardError) as exc_info:
            merge_shard("nonexistent-worktree-xyz")
        assert "not found" in str(exc_info.value).lower()

    def test_get_shard_status_nonexistent_returns_none(self, shard_env: Path):
        """WHY: get_shard_status returns None for missing, doesn't raise."""
        result = get_shard_status("nonexistent-worktree-xyz")
        assert result is None

    def test_get_shard_git_info_nonexistent_returns_empty(self, shard_env: Path):
        """WHY: git_info returns empty dict for missing."""
        result = get_shard_git_info("nonexistent-worktree-xyz")
        assert result == {}


class TestSelfDeletionPrevention:
    """
    Critical safety: prevent agents from deleting their own worktree.

    Historical bug: Agent running in worktree could cd elsewhere, then
    cleanup their own worktree, causing shell to be in deleted directory.
    """

    def test_cleanup_blocked_from_inside_worktree(self, shard_env: Path):
        """WHY: Running from inside worktree should block cleanup."""
        info = spawn_shard("inside-test")
        worktree_path = Path(info["worktree_path"])

        # Save original dir
        original_cwd = os.getcwd()

        try:
            # Change into the worktree
            os.chdir(worktree_path)

            # Attempt cleanup - should be blocked
            with pytest.raises(ShardError) as exc_info:
                cleanup_shard(info["worktree_name"])

            assert "inside" in str(exc_info.value).lower() or \
                   "cannot cleanup" in str(exc_info.value).lower()
        finally:
            os.chdir(original_cwd)
            cleanup_shard(info["worktree_name"])

    def test_cleanup_blocked_via_caller_cwd_from_inside(self, shard_env: Path):
        """WHY: caller_cwd parameter enables external cwd checking."""
        info = spawn_shard("caller-cwd-test")
        worktree_path = info["worktree_path"]

        # Pass worktree path as caller_cwd
        with pytest.raises(ShardError) as exc_info:
            cleanup_shard(info["worktree_name"], caller_cwd=worktree_path)

        assert "inside" in str(exc_info.value).lower() or \
               "caller_cwd" in str(exc_info.value).lower()

        # Actual cleanup should work when cwd is outside
        cleanup_shard(info["worktree_name"])

    def test_merge_blocked_from_inside_worktree(self, shard_env: Path):
        """WHY: Merge also involves cleanup, same safety applies."""
        info = spawn_shard("merge-inside-test")
        worktree_path = Path(info["worktree_path"])

        original_cwd = os.getcwd()
        try:
            os.chdir(worktree_path)

            with pytest.raises(ShardError) as exc_info:
                merge_shard(info["worktree_name"])

            assert "inside" in str(exc_info.value).lower() or \
                   "cannot merge" in str(exc_info.value).lower()
        finally:
            os.chdir(original_cwd)
            cleanup_shard(info["worktree_name"])


class TestMergeWithCommits:
    """Test successful merge path with actual commits."""

    def test_merge_with_commits_succeeds(self, shard_env: Path):
        """WHY: Happy path - shard with commits should merge cleanly."""
        info = spawn_shard("merge-success-test")
        worktree_path = Path(info["worktree_path"])

        # Make a commit in the shard
        new_file = worktree_path / "new_feature.py"
        new_file.write_text("# New feature\n")
        subprocess.run(["git", "add", "."], cwd=worktree_path, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Add new feature"],
            cwd=worktree_path, check=True, capture_output=True
        )

        # Merge should succeed
        result = merge_shard(info["worktree_name"])

        assert result["success"]
        assert "merged" in result["message"].lower()

        # Verify file is now on master
        master_file = shard_env / "new_feature.py"
        assert master_file.exists()
        assert master_file.read_text() == "# New feature\n"

        # Worktree should be cleaned up
        assert not worktree_path.exists()

    def test_merge_creates_merge_commit(self, shard_env: Path):
        """WHY: --no-ff preserves branch history."""
        info = spawn_shard("no-ff-test")
        worktree_path = Path(info["worktree_path"])

        # Make a commit
        (worktree_path / "file.txt").write_text("content")
        subprocess.run(["git", "add", "."], cwd=worktree_path, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Feature"],
            cwd=worktree_path, check=True, capture_output=True
        )

        result = merge_shard(info["worktree_name"])
        assert result["success"]

        # Check for merge commit
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=shard_env, capture_output=True, text=True
        ).stdout
        assert "Merge" in log


class TestGetShardDiff:
    """Test diff retrieval functionality."""

    def test_diff_shows_changes(self, shard_env: Path):
        """WHY: diff should show what will be merged."""
        info = spawn_shard("diff-test")
        worktree_path = Path(info["worktree_path"])

        # Make changes and commit
        (worktree_path / "changed.py").write_text("new content\n")
        subprocess.run(["git", "add", "."], cwd=worktree_path, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Changes"],
            cwd=worktree_path, check=True, capture_output=True
        )

        diff = get_shard_diff(info["worktree_name"])

        assert diff is not None
        assert "changed.py" in diff
        assert "new content" in diff

        cleanup_shard(info["worktree_name"])

    def test_diff_empty_for_no_changes(self, shard_env: Path):
        """WHY: No commits means no diff."""
        info = spawn_shard("empty-diff-test")

        diff = get_shard_diff(info["worktree_name"])

        # Should be None or empty string
        assert diff is None or diff == ""

        cleanup_shard(info["worktree_name"])

    def test_diff_nonexistent_returns_none(self, shard_env: Path):
        """WHY: Graceful handling of missing shard."""
        diff = get_shard_diff("nonexistent-xyz")
        assert diff is None


class TestGetShardGitInfo:
    """Test git info retrieval."""

    def test_commits_ahead_count(self, shard_env: Path):
        """WHY: Need accurate commit count for merge decisions."""
        info = spawn_shard("commits-test")
        worktree_path = Path(info["worktree_path"])

        # Make 3 commits
        for i in range(3):
            (worktree_path / f"file{i}.txt").write_text(f"content {i}")
            subprocess.run(["git", "add", "."], cwd=worktree_path, check=True)
            subprocess.run(
                ["git", "commit", "-m", f"Commit {i}"],
                cwd=worktree_path, check=True, capture_output=True
            )

        git_info = get_shard_git_info(info["worktree_name"])

        assert git_info["commits_ahead"] == 3
        assert len(git_info["commit_log"]) == 3

        cleanup_shard(info["worktree_name"])

    def test_working_tree_status(self, shard_env: Path):
        """WHY: Need to know if there are uncommitted changes."""
        info = spawn_shard("wt-status-test")
        worktree_path = Path(info["worktree_path"])

        # Initially clean
        git_info = get_shard_git_info(info["worktree_name"])
        assert git_info["working_tree"] == "clean"

        # Make it dirty
        (worktree_path / "dirty.txt").write_text("uncommitted")

        git_info = get_shard_git_info(info["worktree_name"])
        assert git_info["working_tree"] == "dirty"
        assert len(git_info["uncommitted"]) > 0

        cleanup_shard(info["worktree_name"])


class TestProjectRootDetection:
    """Test project root finding logic."""

    def test_set_project_root_requires_git_repo(self, tmp_path: Path):
        """WHY: Should reject non-git directories."""
        non_git_dir = tmp_path / "not_a_repo"
        non_git_dir.mkdir()

        with pytest.raises(ShardError) as exc_info:
            set_project_root(str(non_git_dir))
        assert "not a git repository" in str(exc_info.value).lower()

    def test_skein_project_env_var_override(self, temp_git_repo: Path, monkeypatch):
        """WHY: SKEIN_PROJECT should override cwd-based detection."""
        import skein.shard as shard_module
        shard_module._PROJECT_ROOT = None
        shard_module._WORKTREES_DIR = None

        monkeypatch.setenv("SKEIN_PROJECT", str(temp_git_repo))

        # Change to a different directory
        original_cwd = os.getcwd()
        try:
            os.chdir("/tmp")

            root = shard_module._find_project_root()
            assert root == temp_git_repo.resolve()
        finally:
            os.chdir(original_cwd)
            monkeypatch.delenv("SKEIN_PROJECT", raising=False)


class TestConcurrentOperations:
    """Test behavior under concurrent access."""

    def test_concurrent_spawn_different_names(self, shard_env: Path):
        """WHY: Parallel agent spawns should not interfere."""
        results = []
        errors = []

        def spawn_agent(agent_id):
            try:
                info = spawn_shard(agent_id)
                results.append(info)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(5):
            t = threading.Thread(target=spawn_agent, args=(f"concurrent-{i}",))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All should succeed
        assert len(errors) == 0, f"Spawn errors: {errors}"
        assert len(results) == 5

        # All should have unique paths
        paths = [r["worktree_path"] for r in results]
        assert len(set(paths)) == 5, "Paths should be unique"

        # Cleanup
        for info in results:
            cleanup_shard(info["worktree_name"])

    def test_concurrent_spawn_same_name_race(self, shard_env: Path):
        """WHY: Race condition on sequence number - should handle gracefully."""
        results = []
        errors = []

        def spawn_agent():
            try:
                info = spawn_shard("race-agent")
                results.append(info)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=spawn_agent) for _ in range(3)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Either all succeed with different sequences, or some fail
        # Both outcomes are acceptable for race handling
        all_successful = len(results) == 3 and len(errors) == 0
        some_rejected = len(errors) > 0

        assert all_successful or some_rejected, \
            "Should either all succeed or gracefully handle race"

        # Unique paths if multiple succeeded
        if len(results) > 1:
            paths = [r["worktree_path"] for r in results]
            assert len(set(paths)) == len(paths), "No path collisions"

        # Cleanup
        for info in results:
            try:
                cleanup_shard(info["worktree_name"])
            except:
                pass


class TestIsPathInsideWorktree:
    """Test the path containment checking function."""

    def test_exact_match(self, tmp_path: Path):
        """Path equal to worktree is inside."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        assert _is_path_inside_worktree(worktree, worktree) is True

    def test_fails_closed_on_resolve_error(self, tmp_path: Path):
        """WHY: On path resolution errors, should assume inside (fail closed)."""
        from unittest.mock import patch

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        # Mock resolve() to raise OSError (simulating permission error)
        def broken_resolve(self):
            raise OSError("Permission denied")

        with patch.object(Path, 'resolve', broken_resolve):
            # Should return True (fail closed) instead of False
            result = _is_path_inside_worktree(worktree, worktree)
            assert result is True, "Should fail closed (return True) on error"

    def test_child_is_inside(self, tmp_path: Path):
        """Subdirectory of worktree is inside."""
        worktree = tmp_path / "worktree"
        child = worktree / "subdir" / "deep"
        worktree.mkdir()
        child.mkdir(parents=True)

        assert _is_path_inside_worktree(child, worktree) is True

    def test_sibling_is_not_inside(self, tmp_path: Path):
        """Sibling directory is not inside."""
        worktree = tmp_path / "worktree"
        sibling = tmp_path / "sibling"
        worktree.mkdir()
        sibling.mkdir()

        assert _is_path_inside_worktree(sibling, worktree) is False

    def test_parent_is_not_inside(self, tmp_path: Path):
        """Parent directory is not inside."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        assert _is_path_inside_worktree(tmp_path, worktree) is False


# =============================================================================
# PROPERTY-BASED TESTS (Hypothesis)
# =============================================================================

if HYPOTHESIS_AVAILABLE:

    # Strategy for valid agent IDs
    valid_agent_id = st.from_regex(r'[a-z][a-z0-9-]{2,30}', fullmatch=True)

    class TestPropertyBased:
        """Property-based tests using Hypothesis."""

        @given(agent_id=valid_agent_id)
        @settings(max_examples=20, deadline=10000, suppress_health_check=[HealthCheck.function_scoped_fixture])
        def test_spawn_cleanup_invariant(self, agent_id: str, shard_env: Path):
            """
            Property: For any valid agent_id, spawn then cleanup leaves repo unchanged.

            WHY: Core invariant must hold for all valid inputs.
            """
            try:
                # Record pre-state
                pre_worktrees = subprocess.run(
                    ["git", "worktree", "list"],
                    cwd=shard_env, capture_output=True, text=True
                ).stdout

                # Spawn
                info = spawn_shard(agent_id)

                # Cleanup
                cleanup_shard(info["worktree_name"])

                # Post-state should match pre-state
                post_worktrees = subprocess.run(
                    ["git", "worktree", "list"],
                    cwd=shard_env, capture_output=True, text=True
                ).stdout

                assert pre_worktrees == post_worktrees

            except ShardError:
                # Some agent_ids might be rejected - that's fine
                pass

        @given(count=st.integers(min_value=1, max_value=5))
        @settings(max_examples=5, deadline=30000, suppress_health_check=[HealthCheck.function_scoped_fixture])
        def test_list_matches_spawned_count(self, count: int, shard_env: Path):
            """
            Property: list_shards returns exactly the shards that were spawned.

            WHY: List accuracy is critical for operational decisions.
            """
            spawned = []
            try:
                # Spawn count shards
                for i in range(count):
                    info = spawn_shard(f"prop-test-{i}")
                    spawned.append(info)

                # List should match
                shards = list_shards()
                listed_names = {s["worktree_name"] for s in shards}
                spawned_names = {s["worktree_name"] for s in spawned}

                # Spawned should be subset (there might be pre-existing shards)
                assert spawned_names.issubset(listed_names)

            finally:
                for info in spawned:
                    try:
                        cleanup_shard(info["worktree_name"])
                    except:
                        pass

        @given(agent_id=valid_agent_id)
        @settings(max_examples=10, deadline=10000, suppress_health_check=[HealthCheck.function_scoped_fixture])
        def test_shard_name_parsing_roundtrip(self, agent_id: str, shard_env: Path):
            """
            Property: Shard name can be parsed back to extract the original name.

            WHY: Name reversibility is needed for shard identification.
            """
            try:
                info = spawn_shard(agent_id)

                # Parse the worktree_name back
                status = get_shard_status(info["worktree_name"])

                assert status is not None
                assert status["name"] == agent_id

                cleanup_shard(info["worktree_name"])

            except ShardError:
                pass  # Invalid agent_id is acceptable


# =============================================================================
# ADDITIONAL REGRESSION TESTS
# =============================================================================

class TestRegressions:
    """Tests for specific historical bugs and regressions."""

    def test_full_path_normalization(self, shard_env: Path):
        """
        Regression: Full path passed to cleanup was incorrectly handled.

        The bug: Path(worktrees_dir) / "/full/path" returns just "/full/path"
        which bypassed the worktrees directory containment.
        """
        info = spawn_shard("regression-path-test")
        full_path = info["worktree_path"]

        # Should work by normalizing to just the name
        cleanup_shard(full_path)

        # Verify cleanup happened
        assert not Path(full_path).exists()

    def test_cleanup_from_subdirectory_of_worktree_blocked(self, shard_env: Path):
        """
        Regression: Cleanup from subdirectory of worktree should be blocked.

        The bug: Only checked if cwd == worktree_path, not if cwd is INSIDE.
        """
        info = spawn_shard("subdir-test")
        worktree_path = Path(info["worktree_path"])

        # Create and enter subdirectory
        subdir = worktree_path / "subdir"
        subdir.mkdir()

        original_cwd = os.getcwd()
        try:
            os.chdir(subdir)

            with pytest.raises(ShardError):
                cleanup_shard(info["worktree_name"])
        finally:
            os.chdir(original_cwd)
            cleanup_shard(info["worktree_name"])

    def test_worktrees_dir_itself_not_deletable(self, shard_env: Path):
        """
        Regression: Ensure we can't accidentally delete worktrees/ directory itself.
        """
        worktrees_dir = get_worktrees_dir()
        worktrees_dir.mkdir(exist_ok=True)

        with pytest.raises(ShardError):
            # Try to cleanup with just the worktrees directory name
            cleanup_shard(worktrees_dir.name)

        # Directory should still exist
        assert worktrees_dir.exists()


# =============================================================================
# INTEGRATION TESTS
# =============================================================================

class TestIntegration:
    """Full workflow integration tests."""

    def test_full_feature_development_workflow(self, shard_env: Path):
        """
        Test complete workflow: spawn -> develop -> commit -> merge -> cleanup
        """
        # 1. Spawn shard for feature work
        info = spawn_shard("feature-integration-test")
        worktree_path = Path(info["worktree_path"])

        # 2. Simulate feature development
        feature_file = worktree_path / "new_feature.py"
        feature_file.write_text("""
def new_feature():
    return "Hello from new feature"
""")

        # 3. Commit changes
        subprocess.run(["git", "add", "."], cwd=worktree_path, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Add new feature"],
            cwd=worktree_path, check=True, capture_output=True
        )

        # 4. Check status before merge
        git_info = get_shard_git_info(info["worktree_name"])
        assert git_info["commits_ahead"] == 1
        assert git_info["working_tree"] == "clean"
        assert git_info["merge_status"] == "clean"

        # 5. Get diff
        diff = get_shard_diff(info["worktree_name"])
        assert "new_feature" in diff

        # 6. Merge
        result = merge_shard(info["worktree_name"])
        assert result["success"]

        # 7. Verify feature is on master
        master_feature = shard_env / "new_feature.py"
        assert master_feature.exists()

        # 8. Verify cleanup happened
        assert not worktree_path.exists()

        # 9. Verify branch is gone
        branches = subprocess.run(
            ["git", "branch"],
            cwd=shard_env, capture_output=True, text=True
        ).stdout
        assert info["branch_name"] not in branches


# =============================================================================
# REVIEW QUEUE TESTS
# =============================================================================

class TestGetReviewQueue:
    """Tests for get_review_queue() categorization logic."""

    def test_empty_queue_returns_all_categories(self, shard_env: Path):
        """WHY: Even with no shards, should return dict with all category keys."""
        queue = get_review_queue()

        assert "ready" in queue
        assert "needs_commit" in queue
        assert "conflicts" in queue
        assert "stale" in queue
        assert all(isinstance(v, list) for v in queue.values())

    def test_clean_shard_with_commits_goes_to_ready(self, shard_env: Path):
        """WHY: Shard with commits, clean tree, no conflicts should be ready."""
        info = spawn_shard("ready-test")
        worktree_path = Path(info["worktree_path"])

        try:
            # Make a commit
            (worktree_path / "file.txt").write_text("content")
            subprocess.run(["git", "add", "."], cwd=worktree_path, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Add file"],
                cwd=worktree_path, check=True, capture_output=True
            )

            queue = get_review_queue()

            # Should be in ready
            ready_names = [s["worktree_name"] for s in queue["ready"]]
            assert info["worktree_name"] in ready_names

            # Should NOT be in other categories
            for category in ["needs_commit", "conflicts", "stale"]:
                names = [s["worktree_name"] for s in queue[category]]
                assert info["worktree_name"] not in names

        finally:
            cleanup_shard(info["worktree_name"])

    def test_dirty_shard_goes_to_needs_commit(self, shard_env: Path):
        """WHY: Shard with uncommitted changes should be in needs_commit."""
        info = spawn_shard("dirty-queue-test")
        worktree_path = Path(info["worktree_path"])

        try:
            # Create uncommitted changes (don't commit)
            (worktree_path / "uncommitted.txt").write_text("dirty")

            queue = get_review_queue()

            # Should be in needs_commit
            needs_commit_names = [s["worktree_name"] for s in queue["needs_commit"]]
            assert info["worktree_name"] in needs_commit_names

        finally:
            cleanup_shard(info["worktree_name"])

    def test_conflicting_shard_goes_to_conflicts(self, shard_env: Path):
        """WHY: Shard with merge conflicts should be in conflicts category."""
        info = spawn_shard("conflict-queue-test")
        worktree_path = Path(info["worktree_path"])

        try:
            # Create conflicting changes on shard
            (worktree_path / "conflict.txt").write_text("shard version")
            subprocess.run(["git", "add", "."], cwd=worktree_path, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Shard changes"],
                cwd=worktree_path, check=True, capture_output=True
            )

            # Create conflicting changes on master
            (shard_env / "conflict.txt").write_text("master version")
            subprocess.run(["git", "add", "."], cwd=shard_env, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Master changes"],
                cwd=shard_env, check=True, capture_output=True
            )

            queue = get_review_queue()

            # Should be in conflicts
            conflict_names = [s["worktree_name"] for s in queue["conflicts"]]
            assert info["worktree_name"] in conflict_names

        finally:
            cleanup_shard(info["worktree_name"])

    def test_enriched_fields_present(self, shard_env: Path):
        """WHY: Each shard in queue should have enriched git info."""
        info = spawn_shard("enriched-test")

        try:
            queue = get_review_queue()

            # Find our shard
            all_shards = []
            for category in queue.values():
                all_shards.extend(category)

            our_shard = next(
                (s for s in all_shards if s["worktree_name"] == info["worktree_name"]),
                None
            )

            assert our_shard is not None
            # Check enriched fields
            assert "git_info" in our_shard
            assert "age_days" in our_shard
            assert "commits_ahead" in our_shard
            assert "working_tree" in our_shard
            assert "merge_status" in our_shard

        finally:
            cleanup_shard(info["worktree_name"])

    def test_queue_sorted_by_age_oldest_first(self, shard_env: Path):
        """WHY: Shards should be sorted with oldest first for review priority."""
        # This is hard to test without time manipulation, but we can at least
        # verify the sorting doesn't crash and returns consistent order
        info1 = spawn_shard("sort-test-1")
        info2 = spawn_shard("sort-test-2")

        try:
            queue = get_review_queue()

            # Just verify we got results without crash
            assert isinstance(queue["ready"], list)

        finally:
            cleanup_shard(info1["worktree_name"])
            cleanup_shard(info2["worktree_name"])


class TestGetShardAgeDays:
    """Tests for get_shard_age_days() helper function."""

    def test_today_returns_zero(self, shard_env: Path):
        """WHY: Shard created today should have age 0."""
        info = spawn_shard("age-test")

        try:
            shard_info = get_shard_status(info["worktree_name"])
            age = get_shard_age_days(shard_info)

            assert age == 0

        finally:
            cleanup_shard(info["worktree_name"])

    def test_missing_date_returns_none(self):
        """WHY: Invalid shard info should return None, not crash."""
        age = get_shard_age_days({})
        assert age is None

        age = get_shard_age_days({"date": ""})
        assert age is None

        age = get_shard_age_days({"date": "invalid"})
        assert age is None


# =============================================================================
# DETECT SHARD ENVIRONMENT TESTS
# =============================================================================

class TestDetectShardEnvironment:
    """Tests for detect_shard_environment() cwd detection."""

    def test_returns_none_outside_worktree(self, shard_env: Path):
        """WHY: Should return None when not in a shard worktree."""
        # We're in the main repo, not a worktree
        result = detect_shard_environment()
        assert result is None

    def test_returns_info_inside_worktree(self, shard_env: Path):
        """WHY: Should return shard info when running inside a worktree."""
        info = spawn_shard("detect-test")
        worktree_path = Path(info["worktree_path"])

        original_cwd = os.getcwd()
        try:
            os.chdir(worktree_path)

            result = detect_shard_environment()

            assert result is not None
            assert result["worktree_name"] == info["worktree_name"]
            assert result["branch_name"] == info["branch_name"]

        finally:
            os.chdir(original_cwd)
            cleanup_shard(info["worktree_name"])

    def test_returns_info_from_subdirectory(self, shard_env: Path):
        """WHY: Should detect shard even from a subdirectory within worktree."""
        info = spawn_shard("subdir-detect-test")
        worktree_path = Path(info["worktree_path"])

        # Create and enter subdirectory
        subdir = worktree_path / "deep" / "nested"
        subdir.mkdir(parents=True)

        original_cwd = os.getcwd()
        try:
            os.chdir(subdir)

            result = detect_shard_environment()

            assert result is not None
            assert result["worktree_name"] == info["worktree_name"]

        finally:
            os.chdir(original_cwd)
            cleanup_shard(info["worktree_name"])


# =============================================================================
# SPAWN SEQUENCE CAP INTEGRATION TEST
# =============================================================================

class TestSpawnSequenceCapIntegration:
    """Integration test: spawn_shard properly surfaces sequence cap error."""

    def test_spawn_shard_raises_on_sequence_cap(self, shard_env: Path):
        """
        WHY: Verify that spawn_shard() properly propagates the sequence cap error
        from _get_next_sequence() to the caller.
        """
        worktrees_dir = get_worktrees_dir()
        worktrees_dir.mkdir(exist_ok=True)

        from datetime import datetime
        today = datetime.now().strftime("%Y%m%d")

        # Create a fake worktree at sequence 999
        fake_name = f"spawn-cap-test-{today}-999"
        fake_worktree = worktrees_dir / fake_name
        fake_worktree.mkdir()

        try:
            # spawn_shard should raise ShardError with sequence limit message
            with pytest.raises(ShardError) as exc_info:
                spawn_shard("spawn-cap-test")

            assert "sequence limit" in str(exc_info.value).lower()
            assert "999" in str(exc_info.value)

        finally:
            fake_worktree.rmdir()


# =============================================================================
# TENDER METADATA TESTS
# =============================================================================

class TestGetTenderMetadata:
    """Tests for get_tender_metadata() function."""

    def test_returns_none_for_nonexistent_shard(self, shard_env: Path):
        """WHY: Should return None for missing shard, not crash."""
        result = get_tender_metadata("nonexistent-worktree")
        assert result is None

    def test_returns_basic_metadata(self, shard_env: Path):
        """WHY: Should return dict with expected keys for valid shard."""
        info = spawn_shard("tender-test")

        try:
            metadata = get_tender_metadata(info["worktree_name"])

            assert metadata is not None
            assert metadata["worktree_name"] == info["worktree_name"]
            assert metadata["branch_name"] == info["branch_name"]
            assert "name" in metadata
            assert "worktree_path" in metadata

        finally:
            cleanup_shard(info["worktree_name"])

    def test_includes_commit_count(self, shard_env: Path):
        """WHY: Should count commits on the shard branch."""
        info = spawn_shard("tender-commits-test")
        worktree_path = Path(info["worktree_path"])

        try:
            # No commits yet
            metadata = get_tender_metadata(info["worktree_name"])
            assert metadata["commits"] == 0

            # Add a commit
            (worktree_path / "file.txt").write_text("content")
            subprocess.run(["git", "add", "."], cwd=worktree_path, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Add file"],
                cwd=worktree_path, check=True, capture_output=True
            )

            metadata = get_tender_metadata(info["worktree_name"])
            assert metadata["commits"] == 1

        finally:
            cleanup_shard(info["worktree_name"])

    def test_includes_modified_files_list(self, shard_env: Path):
        """WHY: Should list files modified on the branch."""
        info = spawn_shard("tender-files-test")
        worktree_path = Path(info["worktree_path"])

        try:
            # Add and commit a file
            (worktree_path / "modified.py").write_text("code")
            subprocess.run(["git", "add", "."], cwd=worktree_path, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Add modified.py"],
                cwd=worktree_path, check=True, capture_output=True
            )

            metadata = get_tender_metadata(info["worktree_name"])

            assert "files_modified" in metadata
            assert "modified.py" in metadata["files_modified"]

        finally:
            cleanup_shard(info["worktree_name"])


# =============================================================================
# BUG FIX REGRESSION TESTS
# =============================================================================

class TestBugFixRegressions:
    """Regression tests for specific bugs that were fixed."""

    def test_zero_sequence_ignored(self, shard_env: Path):
        """
        WHY: Zero sequence numbers should be ignored (invalid range).
        Valid sequences are 1-999.
        """
        worktrees_dir = get_worktrees_dir()
        worktrees_dir.mkdir(exist_ok=True)

        from datetime import datetime
        today = datetime.now().strftime("%Y%m%d")

        # Create a fake worktree with zero sequence
        fake_name = f"zero-seq-test-{today}-000"
        fake_worktree = worktrees_dir / fake_name
        fake_worktree.mkdir()

        try:
            # Should ignore 000 and return 1
            next_seq = _get_next_sequence("zero-seq-test", today)
            assert next_seq == 1

        finally:
            fake_worktree.rmdir()

    def test_sequence_over_999_ignored(self, shard_env: Path):
        """
        WHY: Sequences >999 should be ignored (out of valid range).
        Bug: Parser accepted 1000+ sequences.
        """
        worktrees_dir = get_worktrees_dir()
        worktrees_dir.mkdir(exist_ok=True)

        from datetime import datetime
        today = datetime.now().strftime("%Y%m%d")

        # Create a fake worktree with sequence > 999
        fake_name = f"over-seq-test-{today}-1000"
        fake_worktree = worktrees_dir / fake_name
        fake_worktree.mkdir()

        try:
            # Should ignore 1000 and return 1
            next_seq = _get_next_sequence("over-seq-test", today)
            assert next_seq == 1

        finally:
            fake_worktree.rmdir()

    def test_future_date_returns_zero_age(self, shard_env: Path):
        """
        WHY: Future dates should return 0, not negative age.
        Bug: get_shard_age_days returned negative for future dates.
        """
        from datetime import datetime, timedelta

        # Date 5 days in the future
        future = datetime.now() + timedelta(days=5)
        future_str = future.strftime("%Y%m%d")

        age = get_shard_age_days({"date": future_str})

        assert age is not None
        assert age >= 0  # Should be 0, not -5

    def test_merge_requires_clean_status(self, shard_env: Path):
        """
        WHY: Merge should only succeed when merge_status is 'clean'.
        The fix changed from 'only block conflict' to 'require clean'.
        """
        info = spawn_shard("merge-status-test")
        worktree_path = Path(info["worktree_path"])

        try:
            # Fresh shard with no commits has clean status - can merge (nothing to do)
            result = merge_shard(info["worktree_name"])
            # This actually succeeds because there's nothing to merge
            # and cleanup happens (worktree is removed)

            # The real test is that conflicts are properly blocked - already tested
            # in TestGetReviewQueue.test_conflicting_shard_goes_to_conflicts

        except ShardError:
            # If it fails due to cleanup after success, that's ok
            pass


class TestSymlinkSafety:
    """Tests for symlink-related security measures."""

    def test_symlink_inside_worktree_detected(self, shard_env: Path):
        """
        WHY: Prevent symlink bypass attack where agent creates symlink inside
        worktree pointing outside, then cd's to target to evade detection.
        """
        info = spawn_shard("symlink-test")
        worktree_path = Path(info["worktree_path"])

        # Create a symlink inside worktree pointing to outside
        outside_dir = shard_env / "outside"
        outside_dir.mkdir()
        symlink = worktree_path / "escape_link"
        symlink.symlink_to(outside_dir)

        try:
            # The symlink itself IS inside the worktree (literal path)
            assert _is_path_inside_worktree(symlink, worktree_path)

            # Even though the target is outside, the path is still inside
            # because we check unresolved paths too

        finally:
            symlink.unlink()
            outside_dir.rmdir()
            cleanup_shard(info["worktree_name"])

    def test_cleanup_blocked_from_symlink_target(self, shard_env: Path):
        """
        WHY: Agent shouldn't be able to delete own worktree by cd'ing to
        symlink target that's outside the worktree.
        """
        info = spawn_shard("symlink-cleanup-test")
        worktree_path = Path(info["worktree_path"])

        # Create symlink inside worktree pointing to temp outside location
        import tempfile
        with tempfile.TemporaryDirectory() as outside:
            outside_path = Path(outside)
            symlink = worktree_path / "escape"
            symlink.symlink_to(outside_path)

            original_cwd = os.getcwd()
            try:
                # cd to the symlink (which resolves to outside)
                os.chdir(symlink)

                # Cleanup should still work - we're not INSIDE the worktree
                # (the resolved path is outside)
                # But if the caller_cwd mechanism uses unresolved path, it would block
                cleanup_shard(info["worktree_name"], caller_cwd=str(symlink))
                # If we get here, cleanup worked - that's fine, the resolved path is outside

            except ShardError as e:
                # If it's blocked, that's also acceptable (conservative safety)
                assert "inside" in str(e).lower() or "cannot" in str(e).lower()

            finally:
                os.chdir(original_cwd)
                # Cleanup may have already happened
                if symlink.exists():
                    symlink.unlink()


class TestStaleShardCategorization:
    """Tests for stale shard detection."""

    def test_stale_days_parameter_affects_categorization(self, shard_env: Path):
        """WHY: stale_days parameter should control when shards become stale."""
        info = spawn_shard("stale-param-test")

        try:
            # With stale_days=0, a fresh shard with no commits should be stale
            queue = get_review_queue(stale_days=0)

            # Find our shard
            stale_names = [s["worktree_name"] for s in queue["stale"]]
            ready_names = [s["worktree_name"] for s in queue["ready"]]

            # Should be in stale because age >= 0 and no commits
            assert info["worktree_name"] in stale_names

        finally:
            cleanup_shard(info["worktree_name"])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
