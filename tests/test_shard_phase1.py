#!/usr/bin/env python3
"""
Phase-1 suite for location-independent shards (WU-8 of brief-20260902-zpxe).

Pins the pinned grammar (corpus over every clause), the state-handling
matrix, the cleanup authority model (merge proof or --delete-unmerged-branch,
with M1's no-row/no-base refusal), detached bindings, read purity, the
frozen CLI/JSON contracts, and every intentional behavior change enumerated
in the plan's section 8. Knuth refinements (finding-20260902-5t8f) are
covered where marked: M1, O1, O3, O4, O5, O6, O8.
"""

import json
import os
import stat
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from skein.shard import (
    ShardError,
    cleanup_graft_chain,
    cleanup_shard,
    _cleanup_shard_detailed,
    detect_shard_environment,
    get_chain_cleanup_plan,
    get_graft_chain,
    get_shard_git_info,
    get_shard_status,
    get_triage_extras,
    graft_shard,
    list_shards,
    merge_shard,
    parse_shard_name,
    reconcile_shards,
    resolve_shard,
    set_project_root,
    shard_name_from_branch,
    spawn_shard,
    _get_db_connection,
    _get_db_path,
    _get_next_sequence,
    _get_shard_metadata,
    _placement_path,
    _worktree_dirty_fingerprint,
)


# =============================================================================
# FIXTURES
# =============================================================================


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Generator[Path, None, None]:
    """Isolated git repo on master, shard module pointed at it."""
    import skein.shard as shard_module

    repo_path = tmp_path / "phase1_repo"
    repo_path.mkdir()
    _git(repo_path, "init")
    _git(repo_path, "config", "user.email", "test@example.com")
    _git(repo_path, "config", "user.name", "Test User")
    (repo_path / "README.md").write_text("# Phase 1\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "Initial commit")
    _git(repo_path, "branch", "-M", "master")

    shard_module._PROJECT_ROOT = None
    shard_module._WORKTREES_DIR = None
    set_project_root(str(repo_path))

    original_cwd = os.getcwd()
    os.chdir(repo_path)

    yield repo_path

    os.chdir(original_cwd)
    shard_module._PROJECT_ROOT = None
    shard_module._WORKTREES_DIR = None


def _commit_work(worktree: Path, filename: str = "work.txt") -> None:
    (worktree / filename).write_text("work\n")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-m", f"work on {filename}")


def _dirty_fingerprint_collision_states(worktree: Path):
    """Two states that collided under the old unframed untracked encoding."""
    first = worktree / "a.txt"
    second = worktree / "b.txt"
    first.write_bytes(b"")
    second.write_bytes(b"")

    def old_header(path: Path) -> bytes:
        metadata = os.lstat(path)
        relative = os.fsencode(path.relative_to(worktree))
        return (
            b"untracked\0"
            + relative
            + b"\0"
            + stat.S_IFMT(metadata.st_mode).to_bytes(4, "big")
            + stat.S_IMODE(metadata.st_mode).to_bytes(4, "big")
        )

    first_header = old_header(first)
    second_header = old_header(second)
    disclosed = (b"hello", b"P" + second_header + b"Q")
    changed = (b"hello" + second_header + b"P", b"Q")

    # Pin the reproduction: these distinct file states fed identical bytes
    # to the old aggregate SHA-256 stream.
    assert (
        first_header + disclosed[0] + second_header + disclosed[1]
        == first_header + changed[0] + second_header + changed[1]
    )
    return first, second, disclosed, changed


def _write_collision_state(first: Path, second: Path, state) -> None:
    first.write_bytes(state[0])
    second.write_bytes(state[1])


def _add_local_submodule(repo: Path) -> None:
    source = repo.parent / "submodule-source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test User")
    (source / "s.txt").write_text("submodule base\n")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "submodule base")
    _git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(source),
        "sub",
    )
    _git(repo, "commit", "-m", "add local submodule")


def _populate_local_submodule(worktree: Path) -> Path:
    result = subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "update",
            "--init",
            "--recursive",
        ],
        cwd=worktree,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return worktree / "sub" / "s.txt"


def _create_untracked_embedded_repo(worktree: Path) -> Path:
    embedded = worktree / "embedded"
    embedded.mkdir()
    _git(embedded, "init")
    _git(embedded, "config", "user.email", "test@example.com")
    _git(embedded, "config", "user.name", "Test User")
    payload = embedded / "payload.txt"
    payload.write_text("embedded base\n")
    _git(embedded, "add", "payload.txt")
    _git(embedded, "commit", "-m", "embedded base")
    return payload


def _skein_dir_snapshot(repo: Path) -> dict:
    """Byte-level snapshot of .skein (DB plus any sidecars)."""
    skein_dir = repo / ".skein"
    if not skein_dir.exists():
        return {}
    return {
        str(p.relative_to(skein_dir)): p.read_bytes()
        for p in sorted(skein_dir.rglob("*"))
        if p.is_file()
    }


# =============================================================================
# PINNED GRAMMAR CORPUS (Decision A; behavior change 2)
# =============================================================================

GRAMMAR_VALID = [
    # plain
    "foo-20260902-001",
    "a-20260101-999",
    # dashed stems (internal dashes allowed)
    "api-v2-fix-20260902-001",
    "prudence-ignite-0823-20260824-001",
    # graft suffixes, single and repeated, canonical name keeps them
    "foo-20260902-001-graft",
    "foo-20260902-001-graft-graft",
    # 'graft' inside the stem is left alone (only terminal suffixes strip)
    "foo-graft-20260902-001",
    # boundary sequences
    "foo-20260902-001",
    "foo-20260902-999",
    # calendar edges that are real dates
    "foo-20240229-001",  # leap day
    "foo-20261231-001",
]

GRAMMAR_INVALID = [
    # too few segments
    "",
    "foo",
    "foo-123",
    "300dcef3",  # Spindle fallback spool id shape
    "codex-14c128f6",  # Spindle codex fallback shape
    # empty stem
    "-20260902-001",
    # date: wrong width
    "foo-bar-123",  # today's parser accepted this
    "foo-2026090-001",
    "foo-202609021-001",
    # date: non-digit / non-ascii
    "foo-2026090a-001",
    "foo-²0260902-001",
    # date: calendar-invalid
    "foo-20261341-001",
    "foo-20260230-001",
    "foo-20260900-001",
    # sequence: width and range
    "foo-20260902-1",
    "foo-20260902-0001",
    "foo-20260902-000",
    "foo-20260902-1000",
    "foo-20260902-01a",
    # graft that strips down to an invalid residue
    "foo-123-graft",
    "-20260902-001-graft",
]


class TestPinnedGrammar:
    @pytest.mark.parametrize("name", GRAMMAR_VALID)
    def test_valid_names_parse(self, name):
        parsed = parse_shard_name(name)
        assert parsed is not None, name
        assert parsed["name"] == name  # canonical name keeps graft suffixes

    @pytest.mark.parametrize("name", GRAMMAR_INVALID)
    def test_invalid_names_rejected(self, name):
        assert parse_shard_name(name) is None, name

    def test_graft_depth_counted(self):
        assert parse_shard_name("foo-20260902-001")["graft_depth"] == 0
        assert parse_shard_name("foo-20260902-001-graft")["graft_depth"] == 1
        assert parse_shard_name("foo-20260902-001-graft-graft")["graft_depth"] == 2

    def test_stem_extraction(self):
        assert parse_shard_name("api-v2-fix-20260902-001")["stem"] == "api-v2-fix"

    def test_branch_derivation_normalizes_refs_heads(self):
        # O1: porcelain reports refs/heads/<branch>
        assert (
            shard_name_from_branch("refs/heads/shard-foo-20260902-001")
            == "foo-20260902-001"
        )
        assert shard_name_from_branch("shard-foo-20260902-001") == "foo-20260902-001"

    def test_branch_without_prefix_is_not_a_shard(self):
        assert shard_name_from_branch("worktree-foo-20260902-001") is None
        assert shard_name_from_branch("master") is None
        assert shard_name_from_branch(None) is None

    def test_shard_branch_failing_grammar_is_not_a_shard(self):
        assert shard_name_from_branch("shard-300dcef3") is None
        assert shard_name_from_branch("refs/heads/shard-foo-bar-123") is None


# =============================================================================
# MEMBERSHIP FIXTURE MATRIX (WU-1)
# =============================================================================


class TestMembershipMatrix:
    def test_full_worktree_zoo(self, repo: Path, tmp_path: Path):
        """One of each worktree class; list_shards returns exactly the members
        and triage surfaces the rest."""
        # legacy member (in-tree, made by hand like the live 17)
        legacy = repo / "worktrees" / "legacy-20260101-001"
        _git(repo, "worktree", "add", str(legacy), "-b", "shard-legacy-20260101-001")

        # arbitrary-location member
        outside = tmp_path / "elsewhere" / "roamer-20260101-001"
        _git(repo, "worktree", "add", str(outside), "-b", "shard-roamer-20260101-001")

        # member moved to a changed basename
        moved_src = repo / "worktrees" / "mover-20260101-001"
        _git(repo, "worktree", "add", str(moved_src), "-b", "shard-mover-20260101-001")
        moved_dst = tmp_path / "renamed-thing"
        _git(repo, "worktree", "move", str(moved_src), str(moved_dst))

        # non-shard worktree inside worktrees/
        maint = repo / "worktrees" / "incident-47-toolchain"
        _git(repo, "worktree", "add", str(maint), "-b", "maintenance/incident-47")

        # grammar-shaped dirname on a worktree-* branch (CC isolation shape)
        impostor = repo / "worktrees" / "impostor-20260101-001"
        _git(repo, "worktree", "add", str(impostor), "-b", "worktree-impostor")

        # Spindle fallback: shard-<hex8>
        fallback = repo / "worktrees" / "300dcef3"
        _git(repo, "worktree", "add", str(fallback), "-b", "shard-300dcef3")

        # detached bound member (row binds the path)
        bound = repo / "worktrees" / "bound-20260101-001"
        _git(repo, "worktree", "add", str(bound), "-b", "shard-bound-20260101-001")
        conn = _get_db_connection()
        conn.execute(
            "INSERT OR REPLACE INTO shards "
            "(worktree_name, worktree_path, base_commit, base_branch, created_at, status) "
            "VALUES (?, ?, 'abc', 'master', '2026-01-01T00:00:00+00:00', 'active')",
            ("bound-20260101-001", str(bound)),
        )
        conn.commit()
        conn.close()
        _git(bound, "checkout", "--detach")

        # detached unbound worktree
        unbound = repo / "worktrees" / "unbound-20260101-001"
        _git(repo, "worktree", "add", str(unbound), "-b", "shard-unbound-20260101-001")
        _git(unbound, "checkout", "--detach")
        _git(repo, "branch", "-D", "shard-unbound-20260101-001")

        # branch-only shard (branch survives, no worktree)
        _git(repo, "branch", "shard-ghost-20260101-001", "master")

        names = {s["worktree_name"] for s in list_shards()}
        assert names == {
            "legacy-20260101-001",
            "roamer-20260101-001",
            "mover-20260101-001",
            "bound-20260101-001",
        }

        by_name = {s["worktree_name"]: s for s in list_shards()}
        assert by_name["mover-20260101-001"]["worktree_path"] == str(moved_dst)
        assert by_name["bound-20260101-001"]["detached"] is True
        assert by_name["bound-20260101-001"]["source"] == "db"
        assert by_name["legacy-20260101-001"]["source"] == "git"

        extras = get_triage_extras()
        assert "shard-300dcef3" in [f["branch"] for f in extras["foreign"]]
        assert str(unbound) in [d["path"] for d in extras["detached_unbound"]]
        assert "ghost-20260101-001" in [b["name"] for b in extras["branch_only"]]
        moved_names = [m["worktree_name"] for m in extras["moved"]]
        assert "mover-20260101-001" in moved_names

        # maintenance and worktree-* impostor appear nowhere as members
        all_member_paths = {s["worktree_path"] for s in list_shards()}
        assert str(maint) not in all_member_paths
        assert str(impostor) not in all_member_paths

    def test_db_absent_detached_degrades_to_unbound(self, repo: Path):
        """WU-1 acceptance: list_shards succeeds with no DB; detached entries
        surface as unbound rather than crashing."""
        wt = repo / "worktrees" / "det-20260101-001"
        _git(repo, "worktree", "add", str(wt), "-b", "shard-det-20260101-001")
        _git(wt, "checkout", "--detach")

        db_path = _get_db_path()
        if db_path.exists():
            db_path.unlink()

        members = list_shards()
        assert members == []
        extras = get_triage_extras()
        assert str(wt) in [d["path"] for d in extras["detached_unbound"]]


# =============================================================================
# DETACHED BINDINGS (Decision A; O5; behavior change 6)
# =============================================================================


class TestDetachedBindings:
    def _spawn_and_detach(self, name: str):
        info = spawn_shard(name)
        _git(Path(info["worktree_path"]), "checkout", "--detach")
        return info

    def test_bound_detached_is_a_member_via_row(self, repo: Path):
        info = self._spawn_and_detach("rebaser")
        member = get_shard_status(info["worktree_name"])
        assert member is not None
        assert member["detached"] is True
        assert member["source"] == "db"
        assert member["branch_name"] == info["branch_name"]

        resolution = resolve_shard(info["worktree_name"])
        assert resolution["detached"] is True
        assert resolution["source"] == "db"
        assert Path(resolution["path"]) == Path(info["worktree_path"])

    def test_unbound_detached_refuses_destructive_ops(self, repo: Path):
        """Without a binding, the shard's name only reaches its branch —
        never the detached directory — so cleanup refuses branch-only."""
        info = self._spawn_and_detach("unbound")
        # Remove the binding: clear the recorded path
        conn = _get_db_connection()
        conn.execute(
            "UPDATE shards SET worktree_path = NULL WHERE worktree_name = ?",
            (info["worktree_name"],),
        )
        conn.commit()
        conn.close()

        assert get_shard_status(info["worktree_name"]) is None

        with pytest.raises(ShardError, match="branch-only"):
            cleanup_shard(info["worktree_name"])

        # branch untouched
        assert "shard-" + info["worktree_name"] in _git(repo, "branch")

    def test_duplicate_bindings_are_ambiguous_and_refuse(self, repo: Path):
        """O5: multiple rows claiming one detached path are corrupt state -
        triage-only, destructive operations refuse."""
        info = self._spawn_and_detach("dupe")
        conn = _get_db_connection()
        conn.execute(
            "INSERT OR REPLACE INTO shards "
            "(worktree_name, worktree_path, base_commit, base_branch, created_at, status) "
            "VALUES (?, ?, 'abc', 'master', '2026-01-01T00:00:00+00:00', 'active')",
            ("phantom-20260101-001", info["worktree_path"]),
        )
        conn.commit()
        conn.close()

        extras = get_triage_extras()
        amb_paths = [a["path"] for a in extras["detached_ambiguous"]]
        assert info["worktree_path"] in amb_paths

        resolution = resolve_shard(info["worktree_name"])
        assert resolution["ambiguous"] is True
        with pytest.raises(ShardError, match="multiple DB rows"):
            cleanup_shard(info["worktree_name"])

    def test_attached_member_wins_over_stale_binding(self, repo: Path):
        """O5: a row whose name is attached elsewhere cannot bind a detached
        tree; the attached git member wins and the tree stays unbound."""
        info = spawn_shard("winner")
        # Detached scratch tree at some path
        scratch = repo / "worktrees" / "scratch-tree"
        _git(repo, "worktree", "add", "--detach", str(scratch))
        # Corrupt the winner's row to claim the scratch path
        conn = _get_db_connection()
        conn.execute(
            "UPDATE shards SET worktree_path = ? WHERE worktree_name = ?",
            (str(scratch), info["worktree_name"]),
        )
        conn.commit()
        conn.close()

        resolution = resolve_shard(info["worktree_name"])
        assert resolution["source"] == "git"
        assert Path(resolution["path"]) == Path(info["worktree_path"])

        extras = get_triage_extras()
        assert str(scratch) in [d["path"] for d in extras["detached_unbound"]]


# =============================================================================
# CLEANUP AUTHORITY (invariant 6; M1; O6; O8; behavior change 7)
# =============================================================================


class TestCleanupAuthority:
    def test_no_commit_shard_deletes_under_merge_proof(self, repo: Path):
        info = spawn_shard("empty")
        result = _cleanup_shard_detailed(info["worktree_name"])
        assert result["branch_deleted"] is True
        assert result["merged"] is True
        assert result["row_deleted"] is True
        assert info["branch_name"] not in _git(repo, "branch")

    def test_unmerged_attached_refuses_without_override(self, repo: Path):
        info = spawn_shard("unmerged")
        _commit_work(Path(info["worktree_path"]))

        with pytest.raises(ShardError, match="not provably merged"):
            cleanup_shard(info["worktree_name"])

        # No partial action: worktree, branch, and row all stay
        assert Path(info["worktree_path"]).exists()
        assert info["branch_name"] in _git(repo, "branch")
        assert _get_shard_metadata(info["worktree_name"]) is not None

    def test_discard_uncommitted_alone_is_not_deletion_authority(self, repo: Path):
        """--yes maps to discard_uncommitted; it must never delete a branch."""
        info = spawn_shard("yes-only")
        _commit_work(Path(info["worktree_path"]))

        with pytest.raises(ShardError, match="not provably merged"):
            cleanup_shard(info["worktree_name"], discard_uncommitted=True)

        assert info["branch_name"] in _git(repo, "branch")

    def test_override_deletes_branch_and_row_together(self, repo: Path):
        info = spawn_shard("override")
        _commit_work(Path(info["worktree_path"]))

        result = _cleanup_shard_detailed(info["worktree_name"], delete_unmerged_branch=True)
        assert result["branch_deleted"] is True
        assert result["merged"] is False
        assert info["branch_name"] not in _git(repo, "branch")
        assert _get_shard_metadata(info["worktree_name"]) is None

    def test_keep_branch_preserves_row_and_clears_path(self, repo: Path):
        """Invariant 11: metadata follows the branch."""
        info = spawn_shard("keeper")
        _commit_work(Path(info["worktree_path"]))

        result = _cleanup_shard_detailed(info["worktree_name"], keep_branch=True,
                               discard_uncommitted=True)
        assert result["branch_preserved"] is True
        assert result["path_cleared"] is True

        row = _get_shard_metadata(info["worktree_name"])
        assert row is not None
        assert row["worktree_path"] is None
        assert row["base_branch"] == "master"  # lineage intact
        assert info["branch_name"] in _git(repo, "branch")

    def test_no_row_no_base_refusal(self, repo: Path):
        """M1 regression: without a row and without --base there is no base
        to prove a merge against - refuse rather than guess, even for a
        branch that IS in fact merged."""
        wt = repo / "worktrees" / "norow-20260101-001"
        _git(repo, "worktree", "add", str(wt), "-b", "shard-norow-20260101-001")

        with pytest.raises(ShardError, match="no recorded base branch"):
            cleanup_shard("norow-20260101-001")

        assert wt.exists()
        assert "shard-norow-20260101-001" in _git(repo, "branch")

    def test_no_row_explicit_base_provides_proof(self, repo: Path):
        wt = repo / "worktrees" / "norow2-20260101-001"
        _git(repo, "worktree", "add", str(wt), "-b", "shard-norow2-20260101-001")

        result = _cleanup_shard_detailed("norow2-20260101-001", base_branch="master")
        assert result["branch_deleted"] is True
        assert result["merged"] is True

    def test_no_row_override_still_works(self, repo: Path):
        wt = repo / "worktrees" / "norow3-20260101-001"
        _git(repo, "worktree", "add", str(wt), "-b", "shard-norow3-20260101-001")
        _commit_work(wt)

        result = _cleanup_shard_detailed("norow3-20260101-001", delete_unmerged_branch=True)
        assert result["branch_deleted"] is True
        assert result["merged"] is False

    def test_missing_worktree_preserves_branch_and_row(self, repo: Path):
        """State matrix 'missing': prune the registration, keep the unmerged
        branch AND its row, clearing only worktree_path."""
        info = spawn_shard("vanisher")
        _commit_work(Path(info["worktree_path"]))
        shutil.rmtree(info["worktree_path"])

        result = _cleanup_shard_detailed(info["worktree_name"])
        assert result["worktree_pruned"] is True
        assert result["branch_preserved"] is True
        assert result["branch_deleted"] is False
        assert result["path_cleared"] is True
        assert "branch --merged" in result["message"]
        assert "revive it first" in result["message"]
        assert (
            f"skein shard cleanup {info['worktree_name']} "
            "--delete-unmerged-branch"
        ) in result["message"]

        row = _get_shard_metadata(info["worktree_name"])
        assert row is not None
        assert row["worktree_path"] is None
        assert row["base_branch"] == "master"
        assert info["branch_name"] in _git(repo, "branch")

        # Now branch-only: mutating commands refuse (invariant 12)
        with pytest.raises(ShardError, match="branch-only"):
            cleanup_shard(info["worktree_name"])

    def test_missing_worktree_merged_branch_deletes_with_row(self, repo: Path):
        info = spawn_shard("vanmerged")
        shutil.rmtree(info["worktree_path"])

        result = _cleanup_shard_detailed(info["worktree_name"])
        assert result["worktree_pruned"] is True
        assert result["branch_deleted"] is True
        assert result["row_deleted"] is True
        assert info["branch_name"] not in _git(repo, "branch")

    def test_missing_worktree_override_deletes(self, repo: Path):
        info = spawn_shard("vanoverride")
        _commit_work(Path(info["worktree_path"]))
        shutil.rmtree(info["worktree_path"])

        result = _cleanup_shard_detailed(info["worktree_name"], delete_unmerged_branch=True)
        assert result["branch_deleted"] is True
        assert _get_shard_metadata(info["worktree_name"]) is None

    def test_merge_refuses_missing_worktree(self, repo: Path):
        info = spawn_shard("mergegone")
        _commit_work(Path(info["worktree_path"]))
        shutil.rmtree(info["worktree_path"])

        with pytest.raises(ShardError, match="missing"):
            merge_shard(info["worktree_name"])

    def test_squash_merged_branch_still_needs_override(self, repo: Path):
        """O6: ancestry is the only proof. An externally squash-merged branch
        reads as unmerged - the safe direction - and needs the override."""
        info = spawn_shard("squashed")
        _commit_work(Path(info["worktree_path"]))
        _git(repo, "merge", "--squash", info["branch_name"])
        _git(repo, "commit", "-m", "squash of shard")

        with pytest.raises(ShardError, match="not provably merged"):
            cleanup_shard(info["worktree_name"])

        result = _cleanup_shard_detailed(info["worktree_name"], delete_unmerged_branch=True)
        assert result["branch_deleted"] is True
        assert result["merged"] is False

    def test_locked_worktree_refuses(self, repo: Path):
        """O8: locked porcelain entries resist prune/remove; fail safely."""
        info = spawn_shard("padlock")
        _git(repo, "worktree", "lock", info["worktree_path"])
        try:
            with pytest.raises(ShardError, match="locked"):
                cleanup_shard(info["worktree_name"])
            assert Path(info["worktree_path"]).exists()
        finally:
            _git(repo, "worktree", "unlock", info["worktree_path"])
            cleanup_shard(info["worktree_name"])

    def test_dirty_tree_refuses_without_confirmation(self, repo: Path):
        info = spawn_shard("dirty")
        (Path(info["worktree_path"]) / "junk.txt").write_text("junk")

        with pytest.raises(ShardError, match="uncommitted changes"):
            cleanup_shard(info["worktree_name"])

        assert Path(info["worktree_path"]).exists()
        result = _cleanup_shard_detailed(info["worktree_name"], discard_uncommitted=True)
        assert result["worktree_removed"] is True

    def test_branch_only_names_refused_by_merge_too(self, repo: Path):
        _git(repo, "branch", "shard-ghosty-20260101-001", "master")
        with pytest.raises(ShardError, match="branch-only"):
            merge_shard("ghosty-20260101-001")

    def test_merge_without_row_requires_explicit_base(self, repo: Path):
        wt = repo / "worktrees" / "nb-20260101-001"
        _git(repo, "worktree", "add", str(wt), "-b", "shard-nb-20260101-001")
        _commit_work(wt)

        with pytest.raises(ShardError, match="--base"):
            merge_shard("nb-20260101-001")

        result = merge_shard("nb-20260101-001", base_branch="master")
        assert result["success"] is True


# =============================================================================
# CHAIN CLEANUP (O7 taken conservatively; degraded-metadata refusal)
# =============================================================================


class TestChainCleanup:
    def _make_chain(self, repo: Path):
        info = spawn_shard("chainroot")
        _commit_work(Path(info["worktree_path"]))
        # advance master so the graft has a reason to exist
        (repo / "advance.txt").write_text("advance\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "advance master")
        graft = graft_shard(info["worktree_name"])
        return info, graft

    def test_chain_refuses_without_parentage_rows(self, repo: Path):
        """The -graft suffix is never proof of parentage."""
        wt = repo / "worktrees" / "orph-20260101-001-graft"
        _git(repo, "worktree", "add", str(wt), "-b", "shard-orph-20260101-001-graft")

        with pytest.raises(ShardError, match="parentage"):
            cleanup_graft_chain("orph-20260101-001-graft")

        assert wt.exists()

    def test_chain_plan_discloses_per_link(self, repo: Path):
        info, graft = self._make_chain(repo)
        plan = get_chain_cleanup_plan(info["worktree_name"])
        names = [p["worktree_name"] for p in plan]
        assert names == [info["worktree_name"], graft["graft_worktree_name"]]
        for link in plan:
            assert link["proof_base"] == "master"
            assert link["merged"] is False  # both carry unmerged commits

    def test_chain_without_override_preserves_unmerged_links(self, repo: Path):
        info, graft = self._make_chain(repo)
        result = cleanup_graft_chain(
            info["worktree_name"], discard_uncommitted=True
        )
        # Both links refuse (attached, unmerged, no override): nothing removed
        assert result["success"] is False
        assert result["removed"] == []
        assert info["branch_name"] in _git(repo, "branch")
        assert "shard-" + graft["graft_worktree_name"] in _git(repo, "branch")

    def test_chain_with_override_removes_all_links(self, repo: Path):
        info, graft = self._make_chain(repo)
        result = cleanup_graft_chain(
            info["worktree_name"],
            delete_unmerged_branch=True,
            discard_uncommitted=True,
        )
        assert result["success"] is True
        assert len(result["removed"]) == 2
        assert info["branch_name"] not in _git(repo, "branch")

    def test_mixed_location_chain_traverses(self, repo: Path, tmp_path: Path):
        """Chains legitimately span locations; each link resolves
        independently through the resolver."""
        info, graft = self._make_chain(repo)
        moved = tmp_path / "relocated-root"
        _git(repo, "worktree", "move", info["worktree_path"], str(moved))

        chain = get_graft_chain(info["worktree_name"])
        assert chain == [info["worktree_name"], graft["graft_worktree_name"]]

        result = cleanup_graft_chain(
            info["worktree_name"],
            delete_unmerged_branch=True,
            discard_uncommitted=True,
        )
        assert result["success"] is True
        assert not moved.exists()


# =============================================================================
# SEQUENCE FROM BRANCHES (Decision C; behavior change 9)
# =============================================================================


class TestSequenceFromBranches:
    def test_branch_without_directory_still_advances_sequence(self, repo: Path):
        """A cleaned-up worktree whose branch survives must not cause a
        collision on the next spawn."""
        info = spawn_shard("seqskip")
        _commit_work(Path(info["worktree_path"]))
        cleanup_shard(info["worktree_name"], keep_branch=True,
                      discard_uncommitted=True)
        assert not Path(info["worktree_path"]).exists()

        info2 = spawn_shard("seqskip")
        assert info2["worktree_name"] != info["worktree_name"]
        assert info2["worktree_name"].endswith("-002")

    def test_only_grammar_valid_suffixes_counted(self, repo: Path):
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        _git(repo, "branch", f"shard-seqx-{today}-002", "master")
        _git(repo, "branch", f"shard-seqx-{today}-002-graft", "master")
        # Non-three-digit suffix is not a sequence
        _git(repo, "branch", f"shard-seqx-{today}-7", "master")

        assert _get_next_sequence("seqx", today) == 3

    def test_directories_do_not_drive_allocation(self, repo: Path):
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        fake = repo / "worktrees" / f"dirsonly-{today}-005"
        fake.mkdir(parents=True)
        assert _get_next_sequence("dirsonly", today) == 1


# =============================================================================
# RECONCILE (WU-6)
# =============================================================================


class TestReconcile:
    def test_binds_rowless_member_without_inventing_lineage(self, repo: Path):
        wt = repo / "worktrees" / "legacyrec-20260101-001"
        _git(repo, "worktree", "add", str(wt), "-b", "shard-legacyrec-20260101-001")

        summary = reconcile_shards()
        assert summary["bound"] == 1

        row = _get_shard_metadata("legacyrec-20260101-001")
        assert row is not None
        assert row["worktree_path"] == str(wt)
        # Never invented: base_commit unknown-empty, base_branch explicit
        # NULL (NOT the column default 'master') so it can't feed merge proof
        assert row["base_commit"] == ""
        assert row["base_branch"] is None

        # M1 tie-in: a reconciled row still refuses base-less deletion
        _commit_work(wt)
        with pytest.raises(ShardError, match="no recorded base branch"):
            cleanup_shard("legacyrec-20260101-001", discard_uncommitted=True)

    def test_corrects_moved_path(self, repo: Path, tmp_path: Path):
        info = spawn_shard("recmove")
        moved = tmp_path / "rehomed"
        _git(repo, "worktree", "move", info["worktree_path"], str(moved))

        assert _get_shard_metadata(info["worktree_name"])["worktree_path"] == (
            info["worktree_path"]
        )
        summary = reconcile_shards()
        assert summary["updated"] == 1
        assert _get_shard_metadata(info["worktree_name"])["worktree_path"] == str(
            moved
        )

    def test_clears_gone_paths_and_never_deletes_rows(self, repo: Path):
        info = spawn_shard("recgone")
        _commit_work(Path(info["worktree_path"]))
        shutil.rmtree(info["worktree_path"])
        _git(repo, "worktree", "prune")

        summary = reconcile_shards()
        assert summary["cleared"] == 1

        row = _get_shard_metadata(info["worktree_name"])
        assert row is not None  # row survives (branch does)
        assert row["worktree_path"] is None
        assert row["base_branch"] == "master"

    def test_idempotent(self, repo: Path):
        spawn_shard("rectwice")
        first = reconcile_shards()
        second = reconcile_shards()
        assert second["bound"] == 0
        assert second["updated"] == 0
        assert second["cleared"] == 0
        assert first["unchanged"] + first["updated"] + first["bound"] >= 1


# =============================================================================
# READ PURITY (O4; behavior change 11)
# =============================================================================


class TestReadPurity:
    def test_reads_never_create_skein_dir(self, repo: Path):
        skein_dir = repo / ".skein"
        assert not skein_dir.exists()

        list_shards()
        get_triage_extras()
        with pytest.raises(ShardError):
            resolve_shard("nothing-20260101-001")

        assert not skein_dir.exists()

    def test_observational_commands_leave_db_and_sidecars_byte_identical(
        self, repo: Path
    ):
        info = spawn_shard("purity")
        _commit_work(Path(info["worktree_path"]))

        before = _skein_dir_snapshot(repo)
        db_path = _get_db_path()
        db_path.chmod(0o444)
        try:
            list_shards()
            resolve_shard(info["worktree_name"])
            get_shard_status(info["worktree_name"])
            get_shard_git_info(info["worktree_name"])
            get_triage_extras()
            detect_shard_environment()
        finally:
            db_path.chmod(0o644)

        after = _skein_dir_snapshot(repo)
        assert before == after  # bytes, names, and sidecar absence all equal


# =============================================================================
# NO ENVIRONMENT PARTICIPATION (invariant 7)
# =============================================================================


class TestNoEnvEffect:
    def test_skein_worktrees_dir_has_no_effect(self, repo: Path, tmp_path, monkeypatch):
        """Phase 1-3 contract: no placement environment variable exists;
        setting one from PR 7's era changes nothing."""
        elsewhere = tmp_path / "env-target"
        monkeypatch.setenv("SKEIN_WORKTREES_DIR", str(elsewhere))

        info = spawn_shard("envproof")
        try:
            assert Path(info["worktree_path"]).is_relative_to(repo / "worktrees")
            assert not elsewhere.exists()
            resolution = resolve_shard(info["worktree_name"])
            assert Path(resolution["path"]) == Path(info["worktree_path"])
        finally:
            cleanup_shard(info["worktree_name"])


# =============================================================================
# WU-0 REGRESSION: shard apply / shard test PROJECT_ROOT crash
# =============================================================================


class TestApplyAndTestCommands:
    def test_shard_apply_no_longer_crashes(self, repo: Path):
        info = spawn_shard("applier")
        _commit_work(Path(info["worktree_path"]), "applied_file.txt")

        runner = CliRunner()
        with patch("client.cli.make_request", side_effect=Exception("no server")):
            from client.cli import cli

            result = runner.invoke(
                cli,
                ["shard", "apply", info["worktree_name"], "--no-confirm"],
                catch_exceptions=False,
            )

        assert "AttributeError" not in result.output
        assert result.exit_code == 0, result.output
        assert (repo / "applied_file.txt").exists()
        _git(repo, "checkout", "--", ".")
        (repo / "applied_file.txt").unlink()

    def test_shard_test_reaches_rites_loading(self, repo: Path):
        """The crash was before rites even loaded; now the command proceeds
        to the (expected) no-rites error instead of AttributeError."""
        info = spawn_shard("tester")

        runner = CliRunner()
        with patch("client.cli.make_request", side_effect=Exception("no server")):
            from client.cli import cli

            result = runner.invoke(cli, ["shard", "test", info["worktree_name"]])

        assert "AttributeError" not in result.output
        assert "No rites defined" in result.output


# =============================================================================
# CONTRACT GOLDENS (section 7; WU-8)
# =============================================================================


class TestContracts:
    def _invoke(self, args, **kwargs):
        from client.cli import cli

        runner = CliRunner()
        with patch(
            "client.cli.make_request", return_value={"thread_id": "th-test-1"}
        ):
            return runner.invoke(cli, args, catch_exceptions=False, **kwargs)

    def test_spawn_human_lines_frozen(self, repo: Path):
        """Spindle/Mill/Loom parse '  Branch: ' and '  Worktree: ' verbatim."""
        result = self._invoke(["shard", "spawn", "--agent", "golden"])
        assert result.exit_code == 0, result.output

        lines = result.output.splitlines()
        branch_lines = [l for l in lines if l.startswith("  Branch: ")]
        worktree_lines = [l for l in lines if l.startswith("  Worktree: ")]
        assert len(branch_lines) == 1
        assert len(worktree_lines) == 1
        branch = branch_lines[0][len("  Branch: ") :]
        wt_path = worktree_lines[0][len("  Worktree: ") :]
        assert branch.startswith("shard-golden-")
        assert Path(wt_path).is_absolute()
        assert Path(wt_path).exists()

        cleanup_shard(branch[len("shard-") :])

    def test_spawn_json_contract(self, repo: Path):
        result = self._invoke(["shard", "spawn", "--agent", "jsonspawn", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert set(payload.keys()) == {
            "worktree_name",
            "worktree_path",
            "branch_name",
            "name",
            "base_branch",
            "project_root",
        }
        assert payload["base_branch"] == "master"
        assert Path(payload["project_root"]).resolve() == repo.resolve()
        cleanup_shard(payload["worktree_name"])

    def test_list_json_keeps_and_extends_keys(self, repo: Path):
        info = spawn_shard("lister")
        try:
            result = self._invoke(["shard", "list", "--json"])
            assert result.exit_code == 0, result.output
            payload = json.loads(result.output)
            entry = next(
                e for e in payload if e["worktree_name"] == info["worktree_name"]
            )
            # Long-standing keys
            for key in ("worktree_name", "worktree_path", "branch_name", "name",
                        "date", "seq"):
                assert key in entry, key
            # Additive keys
            for key in ("exists", "source", "detached", "branch"):
                assert key in entry, key
            assert entry["source"] == "git"
            assert entry["seq"] == info["worktree_name"][-3:]
        finally:
            cleanup_shard(info["worktree_name"])

    def test_where_bare_path_on_stdout(self, repo: Path):
        info = spawn_shard("wherereal")
        try:
            result = self._invoke(
                ["shard", "where", info["worktree_name"], "--path-only"]
            )
            assert result.exit_code == 0, result.output
            assert result.output.strip() == info["worktree_path"]
        finally:
            cleanup_shard(info["worktree_name"])

    def test_cleanup_yes_alone_never_deletes_branch_cli(self, repo: Path):
        """--yes is confirmation-skip only (behavior change 7, CLI level)."""
        info = spawn_shard("cliyes")
        _commit_work(Path(info["worktree_path"]))

        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli, ["shard", "cleanup", info["worktree_name"], "--yes"]
            )

        assert result.exit_code != 0
        assert "not provably merged" in result.output
        assert info["branch_name"] in _git(repo, "branch")

        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                [
                    "shard",
                    "cleanup",
                    info["worktree_name"],
                    "--yes",
                    "--delete-unmerged-branch",
                ],
            )
        assert result.exit_code == 0, result.output
        assert info["branch_name"] not in _git(repo, "branch")


# =============================================================================
# SITE DERIVATION (Decision D; behavior change 10)
# =============================================================================


class TestSiteDerivation:
    def test_basename_derivation_matches_path_parse_for_projects_layout(
        self, repo: Path
    ):
        """For a repo at ~/projects/<name>, the old path-parse and the new
        basename derivation are byte-identical: <name>-development."""
        import client.cli as cli_module
        import skein.shard as shard_module

        derived = cli_module._derive_shard_site(shard_module)
        assert derived == f"{repo.name}-development"

    def test_derivation_works_outside_projects_layout(self, tmp_path: Path):
        """Repos outside ~/projects previously misparsed; now derive from
        the origin repo basename."""
        import client.cli as cli_module
        import skein.shard as shard_module

        outside = tmp_path / "deep" / "nested" / "warpcore"
        outside.mkdir(parents=True)
        _git(outside, "init")
        _git(outside, "config", "user.email", "t@e.com")
        _git(outside, "config", "user.name", "T")
        (outside / "x").write_text("x")
        _git(outside, "add", ".")
        _git(outside, "commit", "-m", "init")

        shard_module._PROJECT_ROOT = None
        shard_module._WORKTREES_DIR = None
        set_project_root(str(outside))
        try:
            assert cli_module._derive_shard_site(shard_module) == (
                "warpcore-development"
            )
        finally:
            shard_module._PROJECT_ROOT = None
            shard_module._WORKTREES_DIR = None

    def test_review_site_config_override(self, repo: Path):
        import client.cli as cli_module
        import skein.shard as shard_module

        skein_dir = repo / ".skein"
        skein_dir.mkdir(exist_ok=True)
        (skein_dir / "config.json").write_text(
            json.dumps({"review_site": "custom-review"})
        )
        try:
            assert cli_module._derive_shard_site(shard_module) == "custom-review"
        finally:
            (skein_dir / "config.json").unlink()


# =============================================================================
# RESOLVER JOIN INVARIANT (Decision B hard invariant)
# =============================================================================


class TestNoJoinOutsidePlacement:
    def test_no_worktrees_dir_name_join_outside_placement_path(self):
        """Repo-wide check: `get_worktrees_dir() / <name>` may only be
        computed inside _placement_path (spawn/graft/guess all route through
        it)."""
        shard_src = Path(__file__).parent.parent / "skein" / "shard.py"
        text = shard_src.read_text()

        join_lines = [
            (i + 1, line)
            for i, line in enumerate(text.splitlines())
            if ("get_worktrees_dir() /" in line or "worktrees_dir / " in line)
            and not line.lstrip().startswith("#")
            and "`" not in line  # prose in docstrings, not code
        ]
        # The single allowed site is _placement_path's return
        assert len(join_lines) == 1, join_lines
        assert "return get_worktrees_dir() / worktree_name" in join_lines[0][1]

        cli_src = Path(__file__).parent.parent / "client" / "cli.py"
        cli_text = cli_src.read_text()
        assert "get_worktrees_dir() /" not in cli_text
        assert "worktrees_dir / " not in cli_text


# =============================================================================
# MOVED MEMBERS: EVERY OPERATION (behavior change 4, full lifecycle)
# =============================================================================


class TestMovedMemberLifecycle:
    def test_status_diff_merge_after_basename_change(self, repo: Path, tmp_path):
        info = spawn_shard("fullmove")
        _commit_work(Path(info["worktree_path"]))
        moved = tmp_path / "totally-different-name"
        _git(repo, "worktree", "move", info["worktree_path"], str(moved))

        # status / git info
        member = get_shard_status(info["worktree_name"])
        assert member is not None
        git_info = get_shard_git_info(info["worktree_name"])
        assert git_info["commits_ahead"] == 1
        assert git_info["working_tree"] == "clean"

        # detect from inside the moved worktree
        original_cwd = os.getcwd()
        try:
            os.chdir(moved)
            detected = detect_shard_environment()
            assert detected is not None
            assert detected["worktree_name"] == info["worktree_name"]
        finally:
            os.chdir(original_cwd)

        # merge (mutating op) works and cleans up at the moved location
        result = merge_shard(info["worktree_name"])
        assert result["success"] is True
        assert not moved.exists()
        assert (repo / "work.txt").exists()


# =============================================================================
# FELL ROUND-1 REGRESSIONS (finding-20260903-hcqz)
# =============================================================================


def _sites_dispatch(*site_ids, folio_id="tender-20260101-fell1"):
    """make_request stub serving /sites and /folios for CLI lifecycle tests."""

    def dispatch(method, endpoint, base_url, agent_id, **kwargs):
        if method == "GET" and endpoint == "/sites":
            return [{"site_id": sid} for sid in site_ids]
        if method == "POST" and endpoint == "/folios":
            return {"folio_id": folio_id}
        return {}

    return dispatch


class TestMergeCliBaseAuthority:
    """S1/F1: the CLI must never feed drift/default metadata into merge_shard
    as the base — authority is the --base option or the recorded row, both
    resolved inside the library."""

    def _diverged_rowless_member(self, repo: Path) -> Path:
        _git(repo, "checkout", "-b", "develop")
        (repo / "dev.txt").write_text("develop\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "develop work")
        _git(repo, "checkout", "master")
        (repo / "m.txt").write_text("master\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "master work")
        wt = repo / "worktrees" / "probe-20260101-001"
        _git(
            repo,
            "worktree",
            "add",
            str(wt),
            "-b",
            "shard-probe-20260101-001",
            "develop",
        )
        _commit_work(wt)
        return wt

    def test_rowless_merge_cli_refuses_without_base(self, repo: Path):
        self._diverged_rowless_member(repo)
        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(cli, ["shard", "merge", "probe-20260101-001"])

        assert result.exit_code != 0
        assert "no recorded base branch" in result.output

        # Nothing merged into master, branch untouched
        merged_into_master = (
            subprocess.run(
                [
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    "shard-probe-20260101-001",
                    "master",
                ],
                cwd=repo,
            ).returncode
            == 0
        )
        assert not merged_into_master
        assert "shard-probe-20260101-001" in _git(repo, "branch")

    def test_explicit_base_option_is_honored(self, repo: Path):
        self._diverged_rowless_member(repo)
        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                ["shard", "merge", "probe-20260101-001", "--base", "develop"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "into develop" in result.output
        # Work landed on develop; master never received develop's history
        assert "work on work.txt" in _git(repo, "log", "--oneline", "develop")
        assert "develop work" not in _git(repo, "log", "--oneline", "master")


class TestChainPlanBaseThreading:
    """S2/F4: the chain disclosure must be computed with the same explicit
    base the execution applies, per link."""

    def _rowless_base_link(self, repo: Path):
        info = spawn_shard("planbase")  # no commits: tip is an ancestor of master
        conn = _get_db_connection()
        conn.execute(
            "UPDATE shards SET base_branch = NULL WHERE worktree_name = ?",
            (info["worktree_name"],),
        )
        conn.commit()
        conn.close()
        return info

    def test_plan_reflects_explicit_base(self, repo: Path):
        info = self._rowless_base_link(repo)

        plan = get_chain_cleanup_plan(info["worktree_name"])
        assert plan[0]["proof_base"] is None
        assert plan[0]["merged"] is None

        plan = get_chain_cleanup_plan(info["worktree_name"], base_branch="master")
        assert plan[0]["proof_base"] == "master"
        assert plan[0]["merged"] is True

    def test_cli_chain_with_base_discloses_the_deletion_it_executes(
        self, repo: Path
    ):
        info = self._rowless_base_link(repo)
        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                [
                    "shard",
                    "cleanup",
                    info["worktree_name"],
                    "--chain",
                    "--base",
                    "master",
                    "--yes",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert (
            "merged into master - branch will be deleted (merge-proven)"
            in result.output
        )
        assert "will refuse" not in result.output
        assert info["branch_name"] not in _git(repo, "branch")

    def test_cli_chain_without_base_discloses_and_executes_refusal(
        self, repo: Path
    ):
        info = self._rowless_base_link(repo)
        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                ["shard", "cleanup", info["worktree_name"], "--chain", "--yes"],
                catch_exceptions=False,
            )

        assert "no provable base - link will refuse" in result.output
        assert info["branch_name"] in _git(repo, "branch")


class TestChainDirtyDisclosure:
    """F2: chain cleanup must disclose per-link dirty state before the
    confirmation that authorizes the discard; --yes alone skips the prompt."""

    def _chain_with_dirty_graft(self, repo: Path):
        info = spawn_shard("cdirty")
        _commit_work(Path(info["worktree_path"]))
        (repo / "advance.txt").write_text("advance\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "advance master")
        graft = graft_shard(info["worktree_name"])
        precious = Path(graft["graft_worktree_path"]) / "PRECIOUS_UNCOMMITTED.txt"
        precious.write_text("hours of uncommitted work\n")
        return info, graft, precious

    def test_decline_leaves_everything_untouched(self, repo: Path):
        info, graft, precious = self._chain_with_dirty_graft(repo)
        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                ["shard", "cleanup", info["worktree_name"], "--chain", "--keep-branch"],
                input="n\n",
            )

        assert result.exit_code != 0
        assert "PRECIOUS_UNCOMMITTED.txt" in result.output  # disclosed
        assert "DISCARD" in result.output  # the specific discard question
        assert precious.exists()
        assert Path(info["worktree_path"]).exists()
        assert Path(graft["graft_worktree_path"]).exists()

    def test_confirm_covers_exactly_the_disclosed_discard(self, repo: Path):
        info, graft, precious = self._chain_with_dirty_graft(repo)
        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                ["shard", "cleanup", info["worktree_name"], "--chain", "--keep-branch"],
                input="y\n",
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "PRECIOUS_UNCOMMITTED.txt" in result.output
        assert "DISCARD" in result.output
        assert not precious.exists()
        assert not Path(graft["graft_worktree_path"]).exists()

    def test_yes_skips_prompt_but_still_discloses(self, repo: Path):
        info, graft, precious = self._chain_with_dirty_graft(repo)
        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                [
                    "shard",
                    "cleanup",
                    info["worktree_name"],
                    "--chain",
                    "--keep-branch",
                    "--yes",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "Proceed with chain cleanup" not in result.output  # no prompt
        assert "PRECIOUS_UNCOMMITTED.txt" in result.output  # still disclosed
        assert not precious.exists()


class TestReconcileStaleReassignment:
    """S3: a worktree that no longer is this row's member must not keep the
    row's path binding — a later detach would adopt unrelated work."""

    def test_reassigned_path_is_cleared_and_never_rebinds(self, repo: Path):
        info = spawn_shard("reassign")
        wt = Path(info["worktree_path"])
        _git(wt, "checkout", "-b", "ordinary-branch")
        (wt / "other.txt").write_text("unrelated\n")
        _git(wt, "add", ".")
        _git(wt, "commit", "-m", "unrelated work")

        summary = reconcile_shards()
        assert summary["cleared"] == 1
        assert _get_shard_metadata(info["worktree_name"])["worktree_path"] is None

        # The dangerous second act from the finding: detaching the reassigned
        # tree must NOT bind it as the shard.
        _git(wt, "checkout", "--detach")
        assert get_shard_status(info["worktree_name"]) is None
        extras = get_triage_extras()
        assert str(wt) in [d["path"] for d in extras["detached_unbound"]]

    def test_ambiguous_detached_claims_are_retained(self, repo: Path):
        """Sibling: multiple rows claiming one detached path are corrupt
        state that triage flags; reconcile must not guess which claim to
        clear."""
        info = spawn_shard("ambigrec")
        _git(Path(info["worktree_path"]), "checkout", "--detach")
        conn = _get_db_connection()
        conn.execute(
            "INSERT OR REPLACE INTO shards "
            "(worktree_name, worktree_path, base_commit, base_branch, created_at, status) "
            "VALUES (?, ?, 'abc', 'master', '2026-01-01T00:00:00+00:00', 'active')",
            ("phantomrec-20260101-001", info["worktree_path"]),
        )
        conn.commit()
        conn.close()

        summary = reconcile_shards()
        assert summary["cleared"] == 0
        assert (
            _get_shard_metadata(info["worktree_name"])["worktree_path"]
            == info["worktree_path"]
        )
        assert (
            _get_shard_metadata("phantomrec-20260101-001")["worktree_path"]
            == info["worktree_path"]
        )


class TestDbReadUriQuoting:
    """S4: SQLite URI metacharacters in the repo path must not make
    metadata reads silently disappear."""

    @pytest.mark.parametrize(
        "dirname", ["que?stion", "ha#sh", "pct%2Fdir", "sp ace"]
    )
    def test_metadata_reads_survive_uri_metacharacters(
        self, tmp_path: Path, dirname: str
    ):
        import skein.shard as shard_module

        repo_path = tmp_path / dirname
        repo_path.mkdir()
        _git(repo_path, "init")
        _git(repo_path, "config", "user.email", "t@e.com")
        _git(repo_path, "config", "user.name", "T")
        (repo_path / "x").write_text("x")
        _git(repo_path, "add", ".")
        _git(repo_path, "commit", "-m", "init")
        _git(repo_path, "branch", "-M", "master")

        original_cwd = os.getcwd()
        shard_module._PROJECT_ROOT = None
        shard_module._WORKTREES_DIR = None
        set_project_root(str(repo_path))
        os.chdir(repo_path)
        try:
            info = spawn_shard("uriprobe")
            row = _get_shard_metadata(info["worktree_name"])
            assert row is not None
            assert row["worktree_path"] == info["worktree_path"]

            # The finding's second act: without readable metadata the
            # detached binding vanished. It must survive.
            _git(Path(info["worktree_path"]), "checkout", "--detach")
            member = get_shard_status(info["worktree_name"])
            assert member is not None
            assert member["source"] == "db"
        finally:
            os.chdir(original_cwd)
            shard_module._PROJECT_ROOT = None
            shard_module._WORKTREES_DIR = None


class TestPruneMetadataInvariant:
    """S5: metadata follows the branch through GLOBAL prune side effects and
    through failures between removal and branch deletion."""

    def test_collateral_prune_clears_sibling_paths(self, repo: Path):
        a = spawn_shard("colla")
        b = spawn_shard("collb")
        _commit_work(Path(a["worktree_path"]))
        _commit_work(Path(b["worktree_path"]), "other.txt")
        shutil.rmtree(a["worktree_path"])
        shutil.rmtree(b["worktree_path"])

        result = _cleanup_shard_detailed(a["worktree_name"])
        assert result["worktree_pruned"] is True

        # B's registration went with the same global prune; its surviving
        # branch keeps the row, with the dead path cleared.
        row_b = _get_shard_metadata(b["worktree_name"])
        assert row_b is not None
        assert row_b["worktree_path"] is None
        assert row_b["base_branch"] == "master"
        assert b["branch_name"] in _git(repo, "branch")

        resolution = resolve_shard(b["worktree_name"])
        assert resolution["source"] == "guess"  # not a dead db path
        assert resolution["branch_exists"] is True

    def test_branch_delete_failure_leaves_branch_only_state(
        self, repo: Path, monkeypatch
    ):
        info = spawn_shard("delfail")
        _commit_work(Path(info["worktree_path"]))

        import skein.shard as shard_module

        real_get_repo = shard_module._get_repo

        class _FailingBranchGit:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, attr):
                # Branch deletion goes through the expected-OID primitive
                # (git update-ref -d <ref> <oid>); fail exactly that.
                if attr == "update_ref":
                    def _fail(*args, **kwargs):
                        raise RuntimeError("simulated branch delete failure")

                    return _fail
                return getattr(self._real, attr)

        class _RepoProxy:
            def __init__(self, real):
                self._real = real
                self.git = _FailingBranchGit(real.git)

            def __getattr__(self, attr):
                return getattr(self._real, attr)

        monkeypatch.setattr(
            shard_module, "_get_repo", lambda: _RepoProxy(real_get_repo())
        )

        with pytest.raises(ShardError, match="failed to delete branch"):
            cleanup_shard(
                info["worktree_name"],
                delete_unmerged_branch=True,
                discard_uncommitted=True,
            )

        # Worktree gone, branch survived the failed delete: the row must be
        # in branch-only shape (preserved, path cleared), not pointing at a
        # removed directory.
        assert not Path(info["worktree_path"]).exists()
        assert info["branch_name"] in _git(repo, "branch")
        row = _get_shard_metadata(info["worktree_name"])
        assert row is not None
        assert row["worktree_path"] is None
        assert row["base_branch"] == "master"


class TestWhereBranchOnlyDbPath:
    """S6/F3: branch-only is registration-plus-surviving-branch, regardless
    of which resolver source answered (real-prune reproduction)."""

    def test_where_exits_nonzero_after_manual_prune(self, repo: Path):
        info = spawn_shard("boprune")
        _commit_work(Path(info["worktree_path"]))
        shutil.rmtree(info["worktree_path"])
        _git(repo, "worktree", "prune")  # hand prune: row path survives

        assert (
            _get_shard_metadata(info["worktree_name"])["worktree_path"] is not None
        )
        resolution = resolve_shard(info["worktree_name"])
        assert resolution["source"] == "db"
        assert resolution["branch_exists"] is True

        from client.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["shard", "where", info["worktree_name"]])
        assert result.exit_code != 0
        assert "git worktree add" in result.output  # revive guidance

        result = runner.invoke(
            cli, ["shard", "where", info["worktree_name"], "--path-only"]
        )
        assert result.exit_code != 0


class TestTenderRowLifecycle:
    """S7: tender creates or completes the row as part of its own write and
    surfaces failures instead of swallowing them."""

    def test_tender_creates_minimal_row_for_rowless_member(self, repo: Path):
        wt = repo / "worktrees" / "handmade-20260101-001"
        _git(
            repo, "worktree", "add", str(wt), "-b", "shard-handmade-20260101-001"
        )
        _commit_work(wt)
        assert _get_shard_metadata("handmade-20260101-001") is None

        from client.cli import cli

        runner = CliRunner()
        with patch(
            "client.cli.make_request", side_effect=_sites_dispatch("shard-review")
        ):
            result = runner.invoke(
                cli,
                ["shard", "tender", "handmade-20260101-001"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        row = _get_shard_metadata("handmade-20260101-001")
        assert row is not None
        assert row["worktree_path"] == str(wt)
        assert row["base_commit"] == ""  # no invented lineage
        assert row["base_branch"] is None  # can never feed merge proof

    def test_tender_refreshes_moved_path(self, repo: Path, tmp_path: Path):
        info = spawn_shard("tmove")
        _commit_work(Path(info["worktree_path"]))
        moved = tmp_path / "tender-moved"
        _git(repo, "worktree", "move", info["worktree_path"], str(moved))

        from client.cli import cli

        runner = CliRunner()
        with patch(
            "client.cli.make_request", side_effect=_sites_dispatch("shard-review")
        ):
            result = runner.invoke(
                cli,
                ["shard", "tender", info["worktree_name"]],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert _get_shard_metadata(info["worktree_name"])["worktree_path"] == str(
            moved
        )

    def test_tender_surfaces_binding_failure(self, repo: Path, monkeypatch):
        info = spawn_shard("tfail")
        _commit_work(Path(info["worktree_path"]))

        import skein.shard as shard_module

        def _boom(*args, **kwargs):
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr(shard_module, "_bind_member_row", _boom)

        from client.cli import cli

        runner = CliRunner()
        with patch(
            "client.cli.make_request", side_effect=_sites_dispatch("shard-review")
        ):
            result = runner.invoke(
                cli, ["shard", "tender", info["worktree_name"]]
            )

        assert result.exit_code != 0
        assert "Failed to record shard metadata" in result.output

    def test_graft_binds_rowless_source(self, repo: Path):
        """Sibling: graft is the same lifecycle write-through class — a
        rowless source gets a minimal row (no invented lineage), and the
        graft's own row records real parentage."""
        wt = repo / "worktrees" / "gsrc-20260101-001"
        _git(repo, "worktree", "add", str(wt), "-b", "shard-gsrc-20260101-001")
        _commit_work(wt)
        (repo / "advance.txt").write_text("advance\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "advance master")
        assert _get_shard_metadata("gsrc-20260101-001") is None

        graft = graft_shard("gsrc-20260101-001")

        row = _get_shard_metadata("gsrc-20260101-001")
        assert row is not None
        assert row["worktree_path"] == str(wt)
        assert row["base_commit"] == ""
        assert row["base_branch"] is None
        graft_row = _get_shard_metadata(graft["graft_worktree_name"])
        assert graft_row["parent_worktree"] == "gsrc-20260101-001"


class TestNameInputNormalization:
    """S8: an actual member path resolves by exact equality; any other input
    is a canonical name verbatim — basenames are never extracted."""

    def test_full_path_works_after_basename_move(self, repo: Path, tmp_path: Path):
        info = spawn_shard("pmove")
        _commit_work(Path(info["worktree_path"]))
        moved = tmp_path / "arbitrary-basename"
        _git(repo, "worktree", "move", info["worktree_path"], str(moved))

        resolution = resolve_shard(str(moved))
        assert resolution["name"] == info["worktree_name"]
        assert Path(resolution["path"]) == moved

        member = get_shard_status(str(moved))
        assert member is not None
        assert member["worktree_name"] == info["worktree_name"]

        # Row lookups run under the canonical name, so the recorded
        # base_commit still scopes the diff for a path input.
        git_info = get_shard_git_info(str(moved))
        assert git_info["commits_ahead"] == 1

        result = _cleanup_shard_detailed(
            str(moved), delete_unmerged_branch=True, discard_uncommitted=True
        )
        assert result["branch_deleted"] is True
        assert not moved.exists()

    def test_slash_canonical_name_is_preserved_verbatim(self, repo: Path):
        wt = repo / "worktrees" / "topicwt"
        _git(
            repo,
            "worktree",
            "add",
            str(wt),
            "-b",
            "shard-team/topic-20260101-001",
        )
        name = "team/topic-20260101-001"

        assert name in {s["worktree_name"] for s in list_shards()}

        resolution = resolve_shard(name)
        assert resolution["name"] == name
        assert Path(resolution["path"]) == wt
        assert resolution["branch"] == "shard-team/topic-20260101-001"

        member = get_shard_status(name)
        assert member is not None
        assert member["worktree_path"] == str(wt)

    def _collide(self, repo: Path):
        """A branch-only shard plus a different member moved to
        <cwd>/<branch-only-name>: bare-name input must never resolve
        through the cwd-relative path to the other member."""
        hot = spawn_shard("hot")
        _commit_work(Path(hot["worktree_path"]))
        cleanup_shard(hot["worktree_name"], keep_branch=True,
                      discard_uncommitted=True)

        cool = spawn_shard("cool")
        _commit_work(Path(cool["worktree_path"]))
        collision = repo / hot["worktree_name"]
        _git(repo, "worktree", "move", cool["worktree_path"], str(collision))
        assert Path.cwd().resolve() == repo.resolve()
        return hot, cool, collision

    def test_branch_only_name_survives_cwd_collision_where(self, repo: Path):
        """Round 3: a bare canonical name answers branch-only (where exits
        nonzero) even when a different member sits at <cwd>/<name>."""
        hot, cool, collision = self._collide(repo)

        resolution = resolve_shard(hot["worktree_name"])
        assert resolution["name"] == hot["worktree_name"]
        assert resolution["registered"] is False
        assert resolution["branch_exists"] is True
        assert Path(resolution["path"]) != collision

        assert get_shard_status(hot["worktree_name"]) is None

        from client.cli import cli

        runner = CliRunner()
        result = runner.invoke(
            cli, ["shard", "where", hot["worktree_name"], "--path-only"]
        )
        assert result.exit_code != 0
        assert result.output == ""

        # The moved member is still selectable, by its absolute path only
        member = get_shard_status(str(collision))
        assert member is not None
        assert member["worktree_name"] == cool["worktree_name"]

    def test_branch_only_name_survives_cwd_collision_cleanup(self, repo: Path):
        """Round 3 reproduction: cleanup of the branch-only name refuses
        instead of removing the colliding member's worktree."""
        hot, cool, collision = self._collide(repo)

        with pytest.raises(ShardError, match="branch-only"):
            cleanup_shard(hot["worktree_name"], keep_branch=True,
                          discard_uncommitted=True)
        assert collision.exists()

        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                ["shard", "cleanup", hot["worktree_name"], "--keep-branch",
                 "--yes"],
            )
        assert result.exit_code != 0
        assert collision.exists()

        # cool is intact and still resolvable at its moved absolute path
        resolution = resolve_shard(str(collision))
        assert resolution["name"] == cool["worktree_name"]
        assert Path(resolution["path"]) == collision


class TestExplicitRootCanonicalization:
    """S9: explicit project roots (env and API) canonicalize to the origin
    repo — site identity and the DB never follow a worktree directory."""

    def test_env_root_from_moved_worktree_derives_origin(
        self, repo: Path, tmp_path: Path, monkeypatch
    ):
        import client.cli as cli_module
        import skein.shard as shard_module

        info = spawn_shard("envwt")
        moved = tmp_path / "renamed-member"
        _git(repo, "worktree", "move", info["worktree_path"], str(moved))

        monkeypatch.setenv("SKEIN_PROJECT", str(moved))
        shard_module._PROJECT_ROOT = None
        shard_module._WORKTREES_DIR = None
        try:
            root = shard_module.get_project_root()
            assert root.resolve() == repo.resolve()
            assert (
                cli_module._derive_shard_site(shard_module)
                == f"{repo.name}-development"
            )
            assert _get_db_path().parent.parent.resolve() == repo.resolve()
        finally:
            shard_module._PROJECT_ROOT = None
            shard_module._WORKTREES_DIR = None
            monkeypatch.delenv("SKEIN_PROJECT", raising=False)
            set_project_root(str(repo))

    def test_api_root_from_moved_worktree_canonicalizes(
        self, repo: Path, tmp_path: Path
    ):
        import skein.shard as shard_module

        info = spawn_shard("apiwt")
        moved = tmp_path / "api-renamed"
        _git(repo, "worktree", "move", info["worktree_path"], str(moved))

        set_project_root(str(moved))
        try:
            assert shard_module.get_project_root().resolve() == repo.resolve()
        finally:
            set_project_root(str(repo))

    def test_unresolvable_worktree_root_refuses(self, tmp_path: Path):
        bogus = tmp_path / "bogus-wt"
        bogus.mkdir()
        (bogus / ".git").write_text("gitdir: /nonexistent/elsewhere\n")

        with pytest.raises(ShardError, match="origin"):
            set_project_root(str(bogus))


class TestCleanupBoolContract:
    """S10: the public cleanup_shard keeps its documented bool return; the
    detailed dict is a separate internal API."""

    def test_cleanup_shard_returns_literal_true(self, repo: Path):
        info = spawn_shard("boolret")
        outcome = cleanup_shard(info["worktree_name"])
        assert outcome is True
        assert outcome == True  # noqa: E712 — equality form of the contract

    def test_detailed_api_returns_truthy_dict(self, repo: Path):
        info = spawn_shard("dictret")
        result = _cleanup_shard_detailed(info["worktree_name"])
        assert isinstance(result, dict)
        assert result["branch_deleted"] is True
        assert bool(result) is True


class TestDetachedCleanupHeadAuthority:
    """Round 2, defect 1: removal authority must cover the exact detached
    HEAD being discarded — the shard branch's merge proof says nothing
    about commits made while detached."""

    def _detached_with_new_commit(self, name: str):
        info = spawn_shard(name)
        wt = Path(info["worktree_path"])
        _git(wt, "checkout", "--detach")
        (wt / "detached-work.txt").write_text("committed while detached\n")
        _git(wt, "add", ".")
        _git(wt, "commit", "-m", "detached commit")
        head = _git(wt, "rev-parse", "HEAD").strip()
        return info, wt, head

    def test_divergent_detached_head_refuses(self, repo: Path):
        """The reproduction: branch sits at merged master, HEAD advanced to
        a new clean commit — the stale branch proof must not authorize
        discarding the HEAD."""
        info, wt, head = self._detached_with_new_commit("dethead")

        with pytest.raises(ShardError, match="not provably contained"):
            cleanup_shard(info["worktree_name"])

        assert wt.exists()
        assert info["branch_name"] in _git(repo, "branch")
        assert _get_shard_metadata(info["worktree_name"]) is not None
        # The detached commit is still reachable through the registration
        assert _git(repo, "rev-parse", "--verify", f"{head}^{{commit}}")

    def test_divergent_detached_override_discards(self, repo: Path):
        info, wt, head = self._detached_with_new_commit("detoverride")

        result = _cleanup_shard_detailed(
            info["worktree_name"], delete_unmerged_branch=True
        )
        assert result["worktree_removed"] is True
        assert result["branch_deleted"] is True
        assert not wt.exists()

    def test_merged_detached_head_cleans_without_override(self, repo: Path):
        """A detached commit already contained in the base is proven work:
        cleanup proceeds on containment, no override needed."""
        info, wt, head = self._detached_with_new_commit("detmerged")
        _git(repo, "merge", head)

        result = _cleanup_shard_detailed(info["worktree_name"])
        assert result["worktree_removed"] is True
        assert result["branch_deleted"] is True
        assert not wt.exists()

    def test_missing_divergent_detached_refuses_prune(self, repo: Path):
        """Sibling: pruning a missing detached registration discards the
        HEAD's only reachability record — same authority applies."""
        info, wt, head = self._detached_with_new_commit("detgone")
        shutil.rmtree(wt)

        with pytest.raises(ShardError, match="not provably contained"):
            cleanup_shard(info["worktree_name"])

        assert info["worktree_path"] in _git(repo, "worktree", "list", "--porcelain")

    def test_collateral_prune_never_drops_detached_registration(self, repo: Path):
        """Sibling: another shard's cleanup hygiene prune has no authority
        over an unrelated missing detached worktree's HEAD."""
        other = spawn_shard("collmiss")
        _commit_work(Path(other["worktree_path"]))
        shutil.rmtree(other["worktree_path"])

        info, wt, head = self._detached_with_new_commit("detprotect")
        shutil.rmtree(wt)

        result = _cleanup_shard_detailed(other["worktree_name"])
        assert result["worktree_pruned"] is True

        # The unrelated detached registration (and with it the HEAD's
        # reachability and the row's binding) survived the other cleanup.
        assert info["worktree_path"] in _git(repo, "worktree", "list", "--porcelain")
        row = _get_shard_metadata(info["worktree_name"])
        assert row["worktree_path"] == info["worktree_path"]


class TestBranchDeleteExactOid:
    """Round 2, defect 3: merge proof authorizes the exact OID it proved;
    deletion is the atomic expected-OID primitive and refuses a moved ref."""

    def test_ref_moved_after_proof_refuses_deletion(self, repo: Path, monkeypatch):
        _git(repo, "checkout", "-b", "sidefeed")
        (repo / "side.txt").write_text("unmerged side work\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "side work")
        side_oid = _git(repo, "rev-parse", "sidefeed").strip()
        _git(repo, "checkout", "master")

        info = spawn_shard("refmove")
        branch_ref = f"refs/heads/{info['branch_name']}"

        import skein.shard as shard_module

        real_get_repo = shard_module._get_repo

        class _MovingRefGit:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, attr):
                if attr == "worktree":
                    def _worktree(*args, **kwargs):
                        out = self._real.worktree(*args, **kwargs)
                        if args and args[0] == "remove":
                            # Injected ref movement: between the settled
                            # proof and the deletion, the branch advances
                            # to an unmerged commit.
                            _git(repo, "update-ref", branch_ref, side_oid)
                        return out

                    return _worktree
                return getattr(self._real, attr)

        class _RepoProxy:
            def __init__(self, real):
                self._real = real
                self.git = _MovingRefGit(real.git)

            def __getattr__(self, attr):
                return getattr(self._real, attr)

        monkeypatch.setattr(
            shard_module, "_get_repo", lambda: _RepoProxy(real_get_repo())
        )

        with pytest.raises(ShardError, match="failed to delete branch"):
            cleanup_shard(info["worktree_name"])

        # The moved ref survived at its new OID; metadata is branch-only.
        assert _git(repo, "rev-parse", branch_ref).strip() == side_oid
        row = _get_shard_metadata(info["worktree_name"])
        assert row is not None
        assert row["worktree_path"] is None
        assert row["base_branch"] == "master"


class TestDbGitTransitionOrdering:
    """Round 2, defect 4: the recorded path is cleared before any git
    mutation, so no failure state can leave a dead path that binds
    unrelated detached work or lose surviving-branch lineage."""

    def test_row_delete_failure_leaves_no_dangerous_path(
        self, repo: Path, monkeypatch
    ):
        info = spawn_shard("rowfail")
        old_path = info["worktree_path"]

        import skein.shard as shard_module

        def _fail(name):
            raise RuntimeError("simulated row delete failure")

        monkeypatch.setattr(shard_module, "_delete_shard_row", _fail)

        result = _cleanup_shard_detailed(info["worktree_name"])
        assert result["branch_deleted"] is True
        assert result["row_deleted"] is False
        assert "stale row" in result["message"]

        row = _get_shard_metadata(info["worktree_name"])
        assert row is not None
        assert row["worktree_path"] is None

        # The dangerous second act: unrelated detached work at the old path
        # must not bind as this shard.
        _git(repo, "worktree", "add", "--detach", old_path)
        assert get_shard_status(info["worktree_name"]) is None
        extras = get_triage_extras()
        assert old_path in [d["path"] for d in extras["detached_unbound"]]

    def test_path_clear_failure_aborts_before_git_mutation(
        self, repo: Path, monkeypatch
    ):
        info = spawn_shard("clrfail")

        import skein.shard as shard_module

        def _fail(name, path):
            raise RuntimeError("simulated path clear failure")

        monkeypatch.setattr(shard_module, "_set_worktree_path", _fail)

        with pytest.raises(ShardError, match="aborted before touching git"):
            cleanup_shard(info["worktree_name"])

        assert Path(info["worktree_path"]).exists()
        assert info["branch_name"] in _git(repo, "branch")
        row = _get_shard_metadata(info["worktree_name"])
        assert row["worktree_path"] == info["worktree_path"]

    def test_removal_failure_restores_recorded_path(self, repo: Path, monkeypatch):
        info = spawn_shard("restbind")

        import skein.shard as shard_module

        real_get_repo = shard_module._get_repo

        class _FailingRemoveGit:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, attr):
                if attr == "worktree":
                    def _worktree(*args, **kwargs):
                        if args and args[0] == "remove":
                            raise RuntimeError("simulated removal failure")
                        return self._real.worktree(*args, **kwargs)

                    return _worktree
                return getattr(self._real, attr)

        class _RepoProxy:
            def __init__(self, real):
                self._real = real
                self.git = _FailingRemoveGit(real.git)

            def __getattr__(self, attr):
                return getattr(self._real, attr)

        monkeypatch.setattr(
            shard_module, "_get_repo", lambda: _RepoProxy(real_get_repo())
        )

        with pytest.raises(ShardError, match="Failed to remove worktree"):
            cleanup_shard(info["worktree_name"])

        # Compensation: the binding survives a recoverable git failure.
        row = _get_shard_metadata(info["worktree_name"])
        assert row["worktree_path"] == info["worktree_path"]

    def test_removal_failure_restores_live_moved_path_not_reused_old_path(
        self, repo: Path, tmp_path: Path, monkeypatch
    ):
        """Post-convergence reproduction: the DB still names P after the
        member moves to Q, and unrelated detached work reuses P. Failed
        cleanup must rebind the row to live Q, never stale P."""
        info = spawn_shard("restmoved")
        old_path = Path(info["worktree_path"])
        moved_path = tmp_path / "moved-member"
        _git(repo, "worktree", "move", str(old_path), str(moved_path))
        _git(repo, "worktree", "add", "--detach", str(old_path), "HEAD")

        import skein.shard as shard_module

        real_get_repo = shard_module._get_repo

        class _FailingRemoveGit:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, attr):
                if attr == "worktree":
                    def _worktree(*args, **kwargs):
                        if args and args[0] == "remove":
                            raise RuntimeError("simulated removal failure")
                        return self._real.worktree(*args, **kwargs)

                    return _worktree
                return getattr(self._real, attr)

        class _RepoProxy:
            def __init__(self, real):
                self._real = real
                self.git = _FailingRemoveGit(real.git)

            def __getattr__(self, attr):
                return getattr(self._real, attr)

        monkeypatch.setattr(
            shard_module, "_get_repo", lambda: _RepoProxy(real_get_repo())
        )

        with pytest.raises(ShardError, match="Failed to remove worktree"):
            cleanup_shard(info["worktree_name"])

        row = _get_shard_metadata(info["worktree_name"])
        assert row["worktree_path"] == str(moved_path)

        # Once the real member detaches, only Q adopts the row. The reused P
        # remains an unrelated detached worktree despite sharing its HEAD.
        _git(moved_path, "checkout", "--detach")
        status = get_shard_status(info["worktree_name"])
        assert status is not None
        assert status["worktree_path"] == str(moved_path)
        extras = get_triage_extras()
        assert str(old_path) in [item["path"] for item in extras["detached_unbound"]]

    def test_detached_removal_failure_uses_path_and_head_not_head_alone(
        self, repo: Path, tmp_path: Path, monkeypatch
    ):
        """A detached sibling may share the target's HEAD. Compensation
        restores only the exact registered path captured before removal."""
        info = spawn_shard("restdetached")
        target_path = Path(info["worktree_path"])
        _git(target_path, "checkout", "--detach")
        target_head = _git(target_path, "rev-parse", "HEAD").strip()
        sibling_path = tmp_path / "detached-sibling"
        _git(repo, "worktree", "add", "--detach", str(sibling_path), target_head)

        import skein.shard as shard_module

        real_get_repo = shard_module._get_repo

        class _FailingRemoveGit:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, attr):
                if attr == "worktree":
                    def _worktree(*args, **kwargs):
                        if args and args[0] == "remove":
                            raise RuntimeError("simulated removal failure")
                        return self._real.worktree(*args, **kwargs)

                    return _worktree
                return getattr(self._real, attr)

        class _RepoProxy:
            def __init__(self, real):
                self._real = real
                self.git = _FailingRemoveGit(real.git)

            def __getattr__(self, attr):
                return getattr(self._real, attr)

        monkeypatch.setattr(
            shard_module, "_get_repo", lambda: _RepoProxy(real_get_repo())
        )

        with pytest.raises(ShardError, match="Failed to remove worktree"):
            cleanup_shard(info["worktree_name"])

        row = _get_shard_metadata(info["worktree_name"])
        assert row["worktree_path"] == str(target_path)
        status = get_shard_status(info["worktree_name"])
        assert status is not None
        assert status["worktree_path"] == str(target_path)
        extras = get_triage_extras()
        assert str(sibling_path) in [
            item["path"] for item in extras["detached_unbound"]
        ]

    def test_removal_failure_without_live_registration_keeps_path_cleared(
        self, repo: Path, monkeypatch
    ):
        """If git removes the registration but reports failure, no live
        member corroborates a rollback and the old DB binding stays revoked."""
        info = spawn_shard("restgone")
        target_path = Path(info["worktree_path"])

        import skein.shard as shard_module

        real_get_repo = shard_module._get_repo

        class _RemoveThenFailGit:
            def __init__(self, real):
                self._real = real
                self._removed = False

            def __getattr__(self, attr):
                if attr == "worktree":
                    def _worktree(*args, **kwargs):
                        if args and args[0] == "remove":
                            if not self._removed:
                                self._real.worktree(
                                    "remove", "--force", str(target_path)
                                )
                                self._removed = True
                            raise RuntimeError("failure after removal")
                        return self._real.worktree(*args, **kwargs)

                    return _worktree
                return getattr(self._real, attr)

        class _RepoProxy:
            def __init__(self, real):
                self._real = real
                self.git = _RemoveThenFailGit(real.git)

            def __getattr__(self, attr):
                return getattr(self._real, attr)

        monkeypatch.setattr(
            shard_module, "_get_repo", lambda: _RepoProxy(real_get_repo())
        )

        with pytest.raises(ShardError, match="Failed to remove worktree"):
            cleanup_shard(info["worktree_name"])

        assert not target_path.exists()
        row = _get_shard_metadata(info["worktree_name"])
        assert row is not None
        assert row["worktree_path"] is None


class TestChainPerLinkDiscardAuthority:
    """Round 2, defect 2: discard authority is per link and exact to the
    disclosed set — one link's disclosure never covers another, late dirt
    refuses, and a failed chain reports its partial state."""

    def _chain(self, repo: Path):
        info = spawn_shard("plink")
        _commit_work(Path(info["worktree_path"]))
        (repo / "advance.txt").write_text("advance\n")
        _git(repo, "add", "advance.txt")
        _git(repo, "commit", "-m", "advance master")
        graft = graft_shard(info["worktree_name"])
        return info, graft

    def _advance_master(self, repo: Path, filename: str) -> None:
        (repo / filename).write_text("advance again\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", f"advance master for {filename}")

    def test_cli_refuses_late_clean_merged_graft_without_override(
        self, repo: Path, monkeypatch
    ):
        """A clean link added after disclosure is never silently absorbed
        into the execution set, even when merge proof would permit deletion."""
        info, graft = self._chain(repo)
        self._advance_master(repo, "late-clean-base.txt")

        import skein.shard as shard_module

        real_plan = shard_module.get_chain_cleanup_plan
        late = {}

        def _plan_then_extend(*args, **kwargs):
            plan = real_plan(*args, **kwargs)
            late.update(graft_shard(graft["graft_worktree_name"]))
            _git(
                repo,
                "merge",
                "--no-ff",
                late["graft_branch_name"],
                "-m",
                "merge late clean graft",
            )
            return plan

        monkeypatch.setattr(shard_module, "get_chain_cleanup_plan", _plan_then_extend)

        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                ["shard", "cleanup", info["worktree_name"], "--chain", "--yes"],
            )

        assert result.exit_code != 0
        assert "chain membership changed after disclosure" in result.output
        assert "nothing was removed" in result.output
        assert Path(info["worktree_path"]).exists()
        assert Path(graft["graft_worktree_path"]).exists()
        assert Path(late["graft_worktree_path"]).exists()

    def test_cli_refuses_late_committed_graft_with_delete_override(
        self, repo: Path, monkeypatch
    ):
        """The explicit branch-loss override cannot widen confirmation to a
        newly added link or delete the commit it contains."""
        info, graft = self._chain(repo)
        self._advance_master(repo, "late-commit-base.txt")

        import skein.shard as shard_module

        real_plan = shard_module.get_chain_cleanup_plan
        late = {}
        late_tip = []

        def _plan_then_extend(*args, **kwargs):
            plan = real_plan(*args, **kwargs)
            late.update(graft_shard(graft["graft_worktree_name"]))
            _commit_work(Path(late["graft_worktree_path"]), "late-secret.txt")
            late_tip.append(_git(repo, "rev-parse", late["graft_branch_name"]).strip())
            return plan

        monkeypatch.setattr(shard_module, "get_chain_cleanup_plan", _plan_then_extend)

        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                [
                    "shard",
                    "cleanup",
                    info["worktree_name"],
                    "--chain",
                    "--delete-unmerged-branch",
                    "--yes",
                ],
            )

        assert result.exit_code != 0
        assert "chain membership changed after disclosure" in result.output
        assert Path(info["worktree_path"]).exists()
        assert Path(graft["graft_worktree_path"]).exists()
        assert Path(late["graft_worktree_path"]).exists()
        assert _git(repo, "rev-parse", late["graft_branch_name"]).strip() == late_tip[0]

    def test_one_links_disclosure_never_authorizes_another(self, repo: Path):
        info, graft = self._chain(repo)
        (Path(graft["graft_worktree_path"]) / "disclosed.txt").write_text("d\n")

        plan = get_chain_cleanup_plan(info["worktree_name"])
        disclosed = {l["worktree_name"]: l["dirty_files"] for l in plan}
        fingerprints = {
            link["worktree_name"]: link["dirty_fingerprint"] for link in plan
        }
        assert disclosed[graft["graft_worktree_name"]]
        assert disclosed[info["worktree_name"]] == []

        # Late dirt on the OTHER link, after its (empty) disclosure
        late = Path(info["worktree_path"]) / "late.txt"
        late.write_text("undisclosed\n")

        result = cleanup_graft_chain(
            info["worktree_name"],
            keep_branch=True,
            disclosed_dirty=disclosed,
            disclosed_dirty_fingerprints=fingerprints,
        )
        assert result["success"] is False
        assert result["removed"] == [graft["graft_worktree_name"]]
        assert result["errors"][0].startswith(info["worktree_name"])
        assert "uncommitted changes" in result["errors"][0]
        assert late.exists()
        assert Path(info["worktree_path"]).exists()

    def test_late_dirt_on_disclosed_link_refuses(self, repo: Path):
        info, graft = self._chain(repo)
        disclosed_file = Path(graft["graft_worktree_path"]) / "disclosed.txt"
        disclosed_file.write_text("d\n")

        plan = get_chain_cleanup_plan(info["worktree_name"])
        disclosed = {l["worktree_name"]: l["dirty_files"] for l in plan}
        fingerprints = {
            link["worktree_name"]: link["dirty_fingerprint"] for link in plan
        }

        undisclosed_file = Path(graft["graft_worktree_path"]) / "surprise.txt"
        undisclosed_file.write_text("late\n")

        result = cleanup_graft_chain(
            info["worktree_name"],
            keep_branch=True,
            disclosed_dirty=disclosed,
            disclosed_dirty_fingerprints=fingerprints,
        )
        assert result["success"] is False
        assert result["removed"] == []
        assert result["not_attempted"] == [info["worktree_name"]]
        assert disclosed_file.exists()
        assert undisclosed_file.exists()
        assert Path(info["worktree_path"]).exists()

    def test_exact_disclosure_discards_and_proceeds(self, repo: Path):
        info, graft = self._chain(repo)
        (Path(graft["graft_worktree_path"]) / "disclosed.txt").write_text("d\n")

        plan = get_chain_cleanup_plan(info["worktree_name"])
        disclosed = {l["worktree_name"]: l["dirty_files"] for l in plan}
        fingerprints = {
            link["worktree_name"]: link["dirty_fingerprint"] for link in plan
        }

        result = cleanup_graft_chain(
            info["worktree_name"],
            keep_branch=True,
            disclosed_dirty=disclosed,
            disclosed_dirty_fingerprints=fingerprints,
        )
        assert result["success"] is True
        assert len(result["removed"]) == 2

    def test_fingerprint_frames_regular_file_boundaries(self, repo: Path):
        """Two states that were byte-identical under the old aggregate
        encoding must have distinct fingerprints after per-entry framing."""
        info = spawn_shard("framecollision")
        worktree = Path(info["worktree_path"])
        first, second, disclosed, changed = _dirty_fingerprint_collision_states(
            worktree
        )
        _write_collision_state(first, second, disclosed)
        disclosed_status = _git(worktree, "status", "--porcelain")
        before = _worktree_dirty_fingerprint(worktree)

        _write_collision_state(first, second, changed)
        changed_status = _git(worktree, "status", "--porcelain")
        after = _worktree_dirty_fingerprint(worktree)

        assert disclosed_status == changed_status
        assert before != after

    def test_fingerprint_binds_dirty_submodule_contents(self, repo: Path):
        """A same-path rewrite inside a dirty submodule changes authority
        even though the parent worktree's porcelain status is unchanged."""
        _add_local_submodule(repo)
        info = spawn_shard("subfingerprint")
        worktree = Path(info["worktree_path"])
        nested_file = _populate_local_submodule(worktree)
        nested_file.write_text("before disclosure\n")

        disclosed_status = _git(worktree, "status", "--porcelain")
        before = _worktree_dirty_fingerprint(worktree)
        nested_file.write_text("after disclosure\n")
        changed_status = _git(worktree, "status", "--porcelain")
        after = _worktree_dirty_fingerprint(worktree)

        assert "sub" in disclosed_status
        assert disclosed_status == changed_status
        assert before != after

    def test_cli_submodule_rewrite_after_chain_disclosure_refuses(
        self, repo: Path, monkeypatch
    ):
        """Chain cleanup preserves late bytes written inside a disclosed
        dirty submodule and refuses to remove either worktree."""
        _add_local_submodule(repo)
        info = spawn_shard("submodulechain")
        _commit_work(Path(info["worktree_path"]))
        (repo / "advance.txt").write_text("advance\n")
        # Add only the intended file: the test fixture places linked worktrees
        # below the repository root, where `git add .` would stage them as
        # unrelated gitlinks and confuse recursive submodule initialization.
        _git(repo, "add", "advance.txt")
        _git(repo, "commit", "-m", "advance master")
        graft = graft_shard(info["worktree_name"])
        graft_path = Path(graft["graft_worktree_path"])
        nested_file = _populate_local_submodule(graft_path)
        nested_file.write_text("before disclosure\n")

        import skein.shard as shard_module

        real_plan = shard_module.get_chain_cleanup_plan

        def _plan_then_rewrite(*args, **kwargs):
            plan = real_plan(*args, **kwargs)
            nested_file.write_text("after disclosure\n")
            return plan

        monkeypatch.setattr(
            shard_module, "get_chain_cleanup_plan", _plan_then_rewrite
        )

        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                [
                    "shard",
                    "cleanup",
                    info["worktree_name"],
                    "--chain",
                    "--keep-branch",
                    "--yes",
                ],
            )

        assert result.exit_code != 0
        assert "content or index state changed after disclosure" in result.output
        assert nested_file.read_text() == "after disclosure\n"
        assert Path(info["worktree_path"]).exists()
        assert graft_path.exists()

    def test_cli_submodule_rewrite_after_single_disclosure_refuses(
        self, repo: Path, monkeypatch
    ):
        """Single cleanup revalidates dirty submodule contents after the
        user's discard confirmation and preserves a late rewrite."""
        _add_local_submodule(repo)
        info = spawn_shard("singlesubmodule")
        worktree = Path(info["worktree_path"])
        nested_file = _populate_local_submodule(worktree)
        nested_file.write_text("before disclosure\n")

        from client.cli import cli
        import client.cli as cli_module

        def _confirm(prompt, **kwargs):
            if prompt.startswith("Discard these uncommitted changes?"):
                nested_file.write_text("after disclosure\n")
            return True

        monkeypatch.setattr(cli_module.click, "confirm", _confirm)

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                [
                    "shard",
                    "cleanup",
                    info["worktree_name"],
                    "--keep-branch",
                ],
            )

        assert result.exit_code != 0
        assert "content or index state changed after disclosure" in result.output
        assert nested_file.read_text() == "after disclosure\n"
        assert worktree.exists()

    def test_fingerprint_binds_untracked_embedded_repository(self, repo: Path):
        """Git collapses an embedded repository to one untracked directory,
        but the fingerprint still binds every byte worktree removal deletes."""
        info = spawn_shard("embeddedfingerprint")
        worktree = Path(info["worktree_path"])
        payload = _create_untracked_embedded_repo(worktree)

        disclosed_status = _git(worktree, "status", "--porcelain")
        before = _worktree_dirty_fingerprint(worktree)
        payload.write_text("after disclosure\n")
        changed_status = _git(worktree, "status", "--porcelain")
        after = _worktree_dirty_fingerprint(worktree)

        assert disclosed_status == "?? embedded/\n"
        assert disclosed_status == changed_status
        assert before != after

    def test_cli_embedded_repo_rewrite_after_chain_disclosure_refuses(
        self, repo: Path, monkeypatch
    ):
        info, graft = self._chain(repo)
        graft_path = Path(graft["graft_worktree_path"])
        payload = _create_untracked_embedded_repo(graft_path)

        import skein.shard as shard_module

        real_plan = shard_module.get_chain_cleanup_plan

        def _plan_then_rewrite(*args, **kwargs):
            plan = real_plan(*args, **kwargs)
            payload.write_text("after disclosure\n")
            return plan

        monkeypatch.setattr(
            shard_module, "get_chain_cleanup_plan", _plan_then_rewrite
        )

        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                [
                    "shard",
                    "cleanup",
                    info["worktree_name"],
                    "--chain",
                    "--keep-branch",
                    "--yes",
                ],
            )

        assert result.exit_code != 0
        assert "state changed after disclosure" in result.output
        assert payload.read_text() == "after disclosure\n"
        assert Path(info["worktree_path"]).exists()
        assert graft_path.exists()

    def test_cli_embedded_repo_rewrite_after_single_disclosure_refuses(
        self, repo: Path, monkeypatch
    ):
        info = spawn_shard("singleembedded")
        worktree = Path(info["worktree_path"])
        payload = _create_untracked_embedded_repo(worktree)

        from client.cli import cli
        import client.cli as cli_module

        def _confirm(prompt, **kwargs):
            if prompt.startswith("Discard these uncommitted changes?"):
                payload.write_text("after disclosure\n")
            return True

        monkeypatch.setattr(cli_module.click, "confirm", _confirm)

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                ["shard", "cleanup", info["worktree_name"], "--keep-branch"],
            )

        assert result.exit_code != 0
        assert "state changed after disclosure" in result.output
        assert payload.read_text() == "after disclosure\n"
        assert worktree.exists()

    def test_cli_chain_refuses_changed_disclosed_branch_tip_before_mutation(
        self, repo: Path, monkeypatch
    ):
        info, graft = self._chain(repo)
        _git(repo, "merge", "--no-ff", graft["graft_branch_name"], "-m", "merge graft")

        import skein.shard as shard_module

        real_plan = shard_module.get_chain_cleanup_plan
        late_tip = []

        def _plan_then_commit(*args, **kwargs):
            plan = real_plan(*args, **kwargs)
            graft_plan = next(
                link
                for link in plan
                if link["worktree_name"] == graft["graft_worktree_name"]
            )
            assert graft_plan["merged"] is True
            graft_path = Path(graft["graft_worktree_path"])
            _git(graft_path, "commit", "--allow-empty", "-m", "late commit")
            late_tip.append(_git(graft_path, "rev-parse", "HEAD").strip())
            return plan

        monkeypatch.setattr(
            shard_module, "get_chain_cleanup_plan", _plan_then_commit
        )

        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                [
                    "shard",
                    "cleanup",
                    info["worktree_name"],
                    "--chain",
                    "--delete-unmerged-branch",
                    "--yes",
                ],
            )

        assert result.exit_code != 0
        assert "state changed after disclosure" in result.output
        assert "branch tip" in result.output
        assert Path(info["worktree_path"]).exists()
        assert Path(graft["graft_worktree_path"]).exists()
        assert (
            _git(repo, "rev-parse", graft["graft_branch_name"]).strip()
            == late_tip[0]
        )

    def test_cli_single_refuses_changed_branch_tip_with_same_dirty_state(
        self, repo: Path, monkeypatch
    ):
        """An empty commit changes only the disclosed branch/worktree tip;
        unchanged dirty paths and bytes cannot authorize deleting it."""
        info = spawn_shard("singlebranchtip")
        worktree = Path(info["worktree_path"])
        dirty = worktree / "dirty.txt"
        dirty.write_text("unchanged dirty bytes\n")
        late_tip = []

        from client.cli import cli
        import client.cli as cli_module

        def _confirm(prompt, **kwargs):
            if prompt.startswith("Discard these uncommitted changes?"):
                _git(worktree, "commit", "--allow-empty", "-m", "late commit")
                late_tip.append(_git(worktree, "rev-parse", "HEAD").strip())
            return True

        monkeypatch.setattr(cli_module.click, "confirm", _confirm)

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                [
                    "shard",
                    "cleanup",
                    info["worktree_name"],
                    "--delete-unmerged-branch",
                ],
            )

        assert result.exit_code != 0
        assert "state changed after disclosure" in result.output
        assert "branch tip" in result.output
        assert dirty.read_text() == "unchanged dirty bytes\n"
        assert worktree.exists()
        assert _git(repo, "rev-parse", info["branch_name"]).strip() == late_tip[0]

    def test_cli_chain_refuses_changed_proof_base_before_mutation(
        self, repo: Path, monkeypatch
    ):
        """Merge authority binds both ancestry inputs, not only the shard
        tip and the boolean result computed from an earlier base tip."""
        info, graft = self._chain(repo)

        import skein.shard as shard_module

        real_plan = shard_module.get_chain_cleanup_plan

        def _plan_then_advance_base(*args, **kwargs):
            plan = real_plan(*args, **kwargs)
            _git(repo, "commit", "--allow-empty", "-m", "advance proof base")
            return plan

        monkeypatch.setattr(
            shard_module, "get_chain_cleanup_plan", _plan_then_advance_base
        )

        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                [
                    "shard",
                    "cleanup",
                    info["worktree_name"],
                    "--chain",
                    "--delete-unmerged-branch",
                    "--yes",
                ],
            )

        assert result.exit_code != 0
        assert "state changed after disclosure" in result.output
        assert "proof-base tip" in result.output
        assert Path(info["worktree_path"]).exists()
        assert Path(graft["graft_worktree_path"]).exists()

    def test_cli_single_refuses_changed_detached_head_after_disclosure(
        self, repo: Path, monkeypatch
    ):
        info = spawn_shard("singledetached")
        worktree = Path(info["worktree_path"])
        _git(worktree, "checkout", "--detach")
        dirty = worktree / "detached.txt"
        dirty.write_text("before disclosure\n")
        late_head = []

        from client.cli import cli
        import client.cli as cli_module

        def _confirm(prompt, **kwargs):
            if prompt.startswith("Discard these uncommitted changes?"):
                _git(
                    worktree,
                    "commit",
                    "--allow-empty",
                    "-m",
                    "late detached commit",
                )
                late_head.append(_git(worktree, "rev-parse", "HEAD").strip())
            return True

        monkeypatch.setattr(cli_module.click, "confirm", _confirm)

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                [
                    "shard",
                    "cleanup",
                    info["worktree_name"],
                    "--delete-unmerged-branch",
                ],
            )

        assert result.exit_code != 0
        assert "state changed after disclosure" in result.output
        assert "worktree HEAD" in result.output
        assert dirty.read_text() == "before disclosure\n"
        assert worktree.exists()
        assert _git(worktree, "rev-parse", "HEAD").strip() == late_head[0]

    def test_cli_collision_rewrite_after_chain_disclosure_refuses(
        self, repo: Path, monkeypatch
    ):
        """The former two-file collision cannot carry changed bytes through
        chain confirmation under the disclosed fingerprint."""
        info, graft = self._chain(repo)
        graft_path = Path(graft["graft_worktree_path"])
        first, second, disclosed, changed = _dirty_fingerprint_collision_states(
            graft_path
        )
        _write_collision_state(first, second, disclosed)

        import skein.shard as shard_module

        real_plan = shard_module.get_chain_cleanup_plan

        def _plan_then_collide(*args, **kwargs):
            plan = real_plan(*args, **kwargs)
            _write_collision_state(first, second, changed)
            return plan

        monkeypatch.setattr(
            shard_module, "get_chain_cleanup_plan", _plan_then_collide
        )

        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                [
                    "shard",
                    "cleanup",
                    info["worktree_name"],
                    "--chain",
                    "--keep-branch",
                    "--yes",
                ],
            )

        assert result.exit_code != 0
        assert "content or index state changed after disclosure" in result.output
        assert first.read_bytes() == changed[0]
        assert second.read_bytes() == changed[1]
        assert Path(info["worktree_path"]).exists()
        assert graft_path.exists()

    def test_cli_collision_rewrite_after_single_disclosure_refuses(
        self, repo: Path, monkeypatch
    ):
        """The single-cleanup confirmation path also refuses the exact
        collision Fable reproduced and preserves both changed files."""
        info = spawn_shard("singlecollision")
        worktree = Path(info["worktree_path"])
        first, second, disclosed, changed = _dirty_fingerprint_collision_states(
            worktree
        )
        _write_collision_state(first, second, disclosed)

        from client.cli import cli
        import client.cli as cli_module

        def _confirm(prompt, **kwargs):
            if prompt.startswith("Discard these uncommitted changes?"):
                _write_collision_state(first, second, changed)
            return True

        monkeypatch.setattr(cli_module.click, "confirm", _confirm)

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                [
                    "shard",
                    "cleanup",
                    info["worktree_name"],
                    "--keep-branch",
                ],
            )

        assert result.exit_code != 0
        assert "content or index state changed after disclosure" in result.output
        assert first.read_bytes() == changed[0]
        assert second.read_bytes() == changed[1]
        assert worktree.exists()

    def test_cli_same_path_rewrite_after_chain_disclosure_refuses(
        self, repo: Path, monkeypatch
    ):
        """A status line is not content authority: rewriting a disclosed
        path after the plan must preserve the late bytes and the whole chain."""
        info, graft = self._chain(repo)
        disclosed_file = Path(graft["graft_worktree_path"]) / "same.txt"
        disclosed_file.write_text("before disclosure\n")

        import skein.shard as shard_module

        real_plan = shard_module.get_chain_cleanup_plan

        def _plan_then_rewrite(*args, **kwargs):
            plan = real_plan(*args, **kwargs)
            disclosed_file.write_text("after disclosure\n")
            return plan

        monkeypatch.setattr(
            shard_module, "get_chain_cleanup_plan", _plan_then_rewrite
        )

        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                [
                    "shard",
                    "cleanup",
                    info["worktree_name"],
                    "--chain",
                    "--keep-branch",
                    "--yes",
                ],
            )

        assert result.exit_code != 0
        assert "content or index state changed after disclosure" in result.output
        assert disclosed_file.read_text() == "after disclosure\n"
        assert Path(info["worktree_path"]).exists()
        assert Path(graft["graft_worktree_path"]).exists()

    def test_cli_same_path_rewrite_after_single_disclosure_refuses(
        self, repo: Path, monkeypatch
    ):
        """The single-cleanup retry carries the same content fingerprint as
        chain cleanup, so changing a listed path during confirmation refuses."""
        info = spawn_shard("singlerewrite")
        _commit_work(Path(info["worktree_path"]), "same.txt")
        disclosed_file = Path(info["worktree_path"]) / "same.txt"
        disclosed_file.write_text("before disclosure\n")
        _git(Path(info["worktree_path"]), "add", "same.txt")

        from client.cli import cli
        import client.cli as cli_module

        def _confirm(prompt, **kwargs):
            if prompt.startswith("Discard these uncommitted changes?"):
                disclosed_file.write_text("after disclosure\n")
                _git(Path(info["worktree_path"]), "add", "same.txt")
            return True

        monkeypatch.setattr(cli_module.click, "confirm", _confirm)

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                [
                    "shard",
                    "cleanup",
                    info["worktree_name"],
                    "--keep-branch",
                ],
            )

        assert result.exit_code != 0
        assert "content or index state changed after disclosure" in result.output
        assert disclosed_file.read_text() == "after disclosure\n"
        assert Path(info["worktree_path"]).exists()

    def test_cli_late_dirt_in_other_link_fails_nonzero(self, repo: Path, monkeypatch):
        """CLI intersection: a disclosed dirty graft must not authorize late
        dirt in the clean-at-disclosure root, even under --yes; the chain
        preflight refuses before removing either link."""
        info, graft = self._chain(repo)
        (Path(graft["graft_worktree_path"]) / "disclosed.txt").write_text("d\n")
        late = Path(info["worktree_path"]) / "late.txt"

        import skein.shard as shard_module

        real_plan = shard_module.get_chain_cleanup_plan

        def _plan_then_dirty(*args, **kwargs):
            plan = real_plan(*args, **kwargs)
            late.write_text("undisclosed\n")
            return plan

        monkeypatch.setattr(
            shard_module, "get_chain_cleanup_plan", _plan_then_dirty
        )

        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                [
                    "shard",
                    "cleanup",
                    info["worktree_name"],
                    "--chain",
                    "--keep-branch",
                    "--yes",
                ],
            )

        assert result.exit_code != 0
        assert "state changed after disclosure" in result.output
        assert "nothing was removed" in result.output
        assert late.exists()
        assert Path(info["worktree_path"]).exists()
        assert Path(graft["graft_worktree_path"]).exists()

    def test_cli_clean_disclosure_late_dirt_fails_nonzero(
        self, repo: Path, monkeypatch
    ):
        """CLI intersection: with everything clean at disclosure, late dirt
        must refuse and fail the chain — not warn and exit zero."""
        info, graft = self._chain(repo)
        late = Path(graft["graft_worktree_path"]) / "late.txt"

        import skein.shard as shard_module

        real_plan = shard_module.get_chain_cleanup_plan

        def _plan_then_dirty(*args, **kwargs):
            plan = real_plan(*args, **kwargs)
            late.write_text("undisclosed\n")
            return plan

        monkeypatch.setattr(
            shard_module, "get_chain_cleanup_plan", _plan_then_dirty
        )

        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                [
                    "shard",
                    "cleanup",
                    info["worktree_name"],
                    "--chain",
                    "--keep-branch",
                    "--yes",
                ],
            )

        assert result.exit_code != 0
        assert "state changed after disclosure" in result.output
        assert "nothing was removed" in result.output
        assert late.exists()
        assert Path(info["worktree_path"]).exists()
        assert Path(graft["graft_worktree_path"]).exists()

    def test_cli_single_yes_surfaces_filenames(self, repo: Path):
        """CLI intersection: single cleanup --yes discards only after the
        dirty files are printed."""
        info = spawn_shard("yesdirty")
        (Path(info["worktree_path"]) / "SECRET_UNCOMMITTED.txt").write_text("s\n")

        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                ["shard", "cleanup", info["worktree_name"], "--yes"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "SECRET_UNCOMMITTED.txt" in result.output
        assert not Path(info["worktree_path"]).exists()


class TestPathInputMetadataCanonicalization:
    """Round 2, defect 5: an exact member-path input canonicalizes before
    every metadata lookup — chain parentage and base authority never key
    the DB by a raw path."""

    def _chain(self, repo: Path):
        info = spawn_shard("pathchain")
        _commit_work(Path(info["worktree_path"]))
        (repo / "advance.txt").write_text("advance\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "advance master")
        graft = graft_shard(info["worktree_name"])
        return info, graft

    def test_chain_plan_accepts_moved_member_path(self, repo: Path, tmp_path: Path):
        info, graft = self._chain(repo)
        moved = tmp_path / "relocated-chain-root"
        _git(repo, "worktree", "move", info["worktree_path"], str(moved))

        plan = get_chain_cleanup_plan(str(moved))
        names = [p["worktree_name"] for p in plan]
        assert names == [info["worktree_name"], graft["graft_worktree_name"]]

    def test_chain_cleanup_accepts_moved_member_path(
        self, repo: Path, tmp_path: Path
    ):
        info, graft = self._chain(repo)
        moved = tmp_path / "relocated-chain-root"
        _git(repo, "worktree", "move", info["worktree_path"], str(moved))

        result = cleanup_graft_chain(
            str(moved), delete_unmerged_branch=True, discard_uncommitted=True
        )
        assert result["success"] is True
        assert len(result["removed"]) == 2
        assert not moved.exists()

    def test_apply_path_input_uses_recorded_base(self, repo: Path, tmp_path: Path):
        """The reproduction: apply with a moved full path fell back to the
        detected default branch and applied base changes."""
        _git(repo, "checkout", "-b", "feature")
        (repo / "feature.txt").write_text("base branch work\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "feature work")
        _git(repo, "checkout", "master")

        info = spawn_shard("applyprobe", base_branch="feature")
        _commit_work(Path(info["worktree_path"]), "shardwork.txt")
        moved = tmp_path / "apply-renamed"
        _git(repo, "worktree", "move", info["worktree_path"], str(moved))

        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                ["shard", "apply", str(moved), "--no-confirm"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        # Only the shard's own work applies — never the recorded base's
        # divergence from the detected default branch.
        assert (repo / "shardwork.txt").exists()
        assert not (repo / "feature.txt").exists()

    def test_diff_path_input_reports_recorded_base(self, repo: Path, tmp_path: Path):
        """Sibling: shard diff --integration keys its base under the
        canonical name for a path input."""
        _git(repo, "checkout", "-b", "feature")
        (repo / "feature.txt").write_text("base branch work\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "feature work")
        _git(repo, "checkout", "master")

        info = spawn_shard("diffprobe", base_branch="feature")
        _commit_work(Path(info["worktree_path"]), "shardwork.txt")
        moved = tmp_path / "diff-renamed"
        _git(repo, "worktree", "move", info["worktree_path"], str(moved))

        from client.cli import cli

        runner = CliRunner()
        with patch("client.cli.make_request", return_value={}):
            result = runner.invoke(
                cli,
                ["shard", "diff", str(moved), "--integration"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "relative to current feature" in result.output
        assert "feature.txt" not in result.output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
