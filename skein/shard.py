#!/usr/bin/env python3
"""
SHARD Worktree Management

Manages git worktrees for SHARD agent coordination workflow.
Provides core functions for creating, cleaning up, and listing SHARDs.
"""

import os
import re
import json
import hashlib
import stat
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

try:
    import git
except ImportError:
    git = None


class ShardError(Exception):
    """Base exception for SHARD operations."""

    pass


# =============================================================================
# SQLITE DATABASE FOR SHARD METADATA
# =============================================================================

SHARD_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS shards (
    worktree_name TEXT PRIMARY KEY,
    worktree_path TEXT,
    parent_worktree TEXT,
    base_commit TEXT NOT NULL,
    base_branch TEXT DEFAULT 'master',
    created_at TIMESTAMP NOT NULL,
    spawned_by TEXT,
    brief_id TEXT,
    description TEXT,
    status TEXT DEFAULT 'active',
    tendered_at TIMESTAMP,
    merged_at TIMESTAMP,
    confidence INTEGER
);

CREATE INDEX IF NOT EXISTS idx_shards_parent ON shards(parent_worktree);
CREATE INDEX IF NOT EXISTS idx_shards_status ON shards(status);
CREATE INDEX IF NOT EXISTS idx_shards_base_commit ON shards(base_commit);
"""


def _get_db_path() -> Path:
    """Get path to shard database (in .skein directory of project root).

    Pure: never creates the .skein directory. Directory creation belongs to
    the write path (_get_db_connection) so that read commands stay
    side-effect-free.
    """
    return get_project_root() / ".skein" / "shards.db"


def _open_db_readonly() -> Optional[sqlite3.Connection]:
    """Open the shard database for observational reads, or None if absent.

    Opens with SQLite's read-only URI mode plus query_only, so a read command
    cannot create the file, alter the schema, run migrations, or leave journal
    sidecars behind: the DB and its sidecars stay byte-identical across every
    observational command. Callers must tolerate None (no DB yet) and treat
    sqlite3.Error from queries as "no metadata" — an old schema is metadata
    absence, not a crash.
    """
    db_path = _get_db_path()
    if not db_path.exists():
        return None
    try:
        # Percent-encode the filesystem path: in a SQLite URI, ?, #, and %
        # are metacharacters, so an unquoted repo path containing them
        # misparses and every metadata read silently disappears.
        conn = sqlite3.connect(
            f"file:{quote(str(db_path), safe='/')}?mode=ro", uri=True
        )
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        pass
    return conn


def _load_all_rows() -> List[Dict[str, Any]]:
    """Read every shard row, read-only. Empty list when DB or table is absent."""
    conn = _open_db_readonly()
    if conn is None:
        return []
    try:
        cursor = conn.execute("SELECT * FROM shards")
        return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _get_db_connection() -> sqlite3.Connection:
    """Get a writable connection to the shard database, creating it if needed.

    Only mutating lifecycle operations (spawn, graft, cleanup, merge, tender)
    and explicit reconcile may use this; reads go through _open_db_readonly.
    """
    db_path = _get_db_path()
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Initialize schema if needed
    conn.executescript(SHARD_DB_SCHEMA)
    conn.commit()

    # Migrate: add columns missing from DBs created by older versions
    try:
        cursor = conn.execute("PRAGMA table_info(shards)")
        columns = {row[1] for row in cursor.fetchall()}
        migrations = (
            (
                "base_branch",
                "ALTER TABLE shards ADD COLUMN base_branch TEXT DEFAULT 'master'",
            ),
            ("worktree_path", "ALTER TABLE shards ADD COLUMN worktree_path TEXT"),
        )
        added = False
        for column, statement in migrations:
            if column not in columns:
                conn.execute(statement)
                added = True
        if added:
            conn.commit()
    except Exception:
        pass  # Best effort migration

    return conn


def _record_shard_metadata(
    worktree_name: str,
    base_commit: str,
    created_at: datetime,
    spawned_by: Optional[str] = None,
    brief_id: Optional[str] = None,
    description: Optional[str] = None,
    parent_worktree: Optional[str] = None,
    base_branch: Optional[str] = None,
    worktree_path: Optional[str] = None,
) -> None:
    """Record shard metadata in SQLite database.

    worktree_path is stored so a shard stays locatable (and bindable while
    detached) even after the worktree moves. Callers pass the actual creation
    path; there is no fallback computation here — path writes happen only
    with a real path in hand, never a reconstructed one.
    """
    if base_branch is None:
        base_branch = _detect_default_branch()
    conn = _get_db_connection()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO shards
            (worktree_name, worktree_path, base_commit, base_branch, created_at, spawned_by, brief_id, description, parent_worktree, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
        """,
            (
                worktree_name,
                worktree_path,
                base_commit,
                base_branch,
                created_at.isoformat(),
                spawned_by,
                brief_id,
                description,
                parent_worktree,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _get_shard_metadata(worktree_name: str) -> Optional[Dict[str, Any]]:
    """Get shard metadata from SQLite database (read-only; None if absent)."""
    conn = _open_db_readonly()
    if conn is None:
        return None
    try:
        cursor = conn.execute(
            "SELECT * FROM shards WHERE worktree_name = ?", (worktree_name,)
        )
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _set_worktree_path(worktree_name: str, worktree_path: Optional[str]) -> None:
    """Set (or clear, with None) the recorded worktree_path for a shard row.

    Updates only an existing row — never creates one, so lifecycle writes
    can refresh paths without inventing metadata.
    """
    conn = _get_db_connection()
    try:
        conn.execute(
            "UPDATE shards SET worktree_path = ? WHERE worktree_name = ?",
            (worktree_path, worktree_name),
        )
        conn.commit()
    finally:
        conn.close()


def _delete_shard_row(worktree_name: str) -> None:
    """Delete a shard's metadata row.

    Invariant 11: a row is deleted only together with its branch. Callers are
    the merge-proof and --delete-unmerged-branch cleanup paths.
    """
    conn = _get_db_connection()
    try:
        conn.execute("DELETE FROM shards WHERE worktree_name = ?", (worktree_name,))
        conn.commit()
    finally:
        conn.close()


def _update_shard_status(worktree_name: str, status: str, **kwargs) -> None:
    """Update shard status in database."""
    conn = _get_db_connection()
    try:
        updates = ["status = ?"]
        values = [status]

        if "merged_at" in kwargs:
            updates.append("merged_at = ?")
            values.append(
                kwargs["merged_at"].isoformat() if kwargs["merged_at"] else None
            )
        if "tendered_at" in kwargs:
            updates.append("tendered_at = ?")
            values.append(
                kwargs["tendered_at"].isoformat() if kwargs["tendered_at"] else None
            )
        if "confidence" in kwargs:
            updates.append("confidence = ?")
            values.append(kwargs["confidence"])

        values.append(worktree_name)

        conn.execute(
            f"UPDATE shards SET {', '.join(updates)} WHERE worktree_name = ?", values
        )
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# NAME VALIDATION
# =============================================================================

# Maximum length for shard name (leaves room for date+seq in full worktree name)
MAX_SHARD_NAME_LENGTH = 63

# Pattern for valid shard names: alphanumeric start, alphanumeric/hyphen/underscore body
VALID_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")

# Reserved git names that cannot be used
RESERVED_NAMES = frozenset(
    {
        "head",
        "HEAD",
        "master",
        "main",
        "refs",
        "objects",
        "hooks",
        "info",
        "logs",
        "worktrees",  # Our own directory
    }
)


def validate_shard_name(name: str) -> Tuple[bool, Optional[str]]:
    """
    Validate a shard name for use in git branches and filesystem paths.

    Args:
        name: The shard name to validate

    Returns:
        (is_valid, error_message) - error_message is None if valid
    """
    if not name:
        return False, "name cannot be empty"

    if not name.strip():
        return False, "name cannot be only whitespace"

    if len(name) > MAX_SHARD_NAME_LENGTH:
        return (
            False,
            f"name exceeds {MAX_SHARD_NAME_LENGTH} characters (got {len(name)})",
        )

    # Check for forbidden git patterns
    if ".." in name:
        return False, "name cannot contain consecutive dots (..)"

    if name.endswith(".lock"):
        return False, "name cannot end with .lock"

    if "@{" in name:
        return False, "name cannot contain @{ (git reflog notation)"

    if name.startswith("."):
        return False, "name cannot start with a dot"

    if name.startswith("-"):
        return False, "name cannot start with a hyphen"

    # Check reserved names
    if name.lower() in {n.lower() for n in RESERVED_NAMES}:
        return False, f"name '{name}' is reserved"

    # Check allowed character pattern
    if not VALID_NAME_PATTERN.match(name):
        return False, (
            "name must start with alphanumeric and contain only "
            "alphanumeric characters, hyphens (-), and underscores (_)"
        )

    return True, None


# =============================================================================
# PINNED SHARD NAME GRAMMAR (membership predicate, Decision A)
# =============================================================================

# A shard's identity is its git branch: `shard-<name>` where <name> passes the
# pinned grammar below. The directory basename is only a creation convention;
# it never participates in membership, lookup, or resolution. This grammar is
# a reserved namespace: tooling that creates non-shard `shard-*` branches
# (e.g. Spindle fallback worktrees, branch `shard-<hex8>`) must never emit
# `{stem}-{valid YYYYMMDD}-{001-999}` shaped names.

SHARD_BRANCH_PREFIX = "shard-"
GRAFT_SUFFIX = "-graft"

_SEQ_PATTERN = re.compile(r"^[0-9]{3}$")
_DATE_PATTERN = re.compile(r"^[0-9]{8}$")


def parse_shard_name(name: str) -> Optional[Dict[str, Any]]:
    """
    Validate a shard name against the pinned grammar.

    The canonical name keeps any terminal -graft suffixes; validation strips
    them repeatedly, then right-splits the residue into:
      stem   - nonempty, internal dashes allowed
      date   - exactly 8 ASCII digits forming a valid calendar YYYYMMDD date
      seq    - exactly three ASCII digits in 001-999

    Returns:
        {"name", "stem", "date", "seq", "graft_depth"} when valid, else None.
    """
    if not name:
        return None

    graft_depth = 0
    residue = name
    while residue.endswith(GRAFT_SUFFIX):
        residue = residue[: -len(GRAFT_SUFFIX)]
        graft_depth += 1

    parts = residue.rsplit("-", 2)
    if len(parts) != 3:
        return None
    stem, date, seq = parts

    if not stem:
        return None
    if not _DATE_PATTERN.match(date):
        return None
    try:
        datetime.strptime(date, "%Y%m%d")
    except ValueError:
        return None
    if not _SEQ_PATTERN.match(seq):
        return None
    if not (1 <= int(seq) <= MAX_SEQUENCE_NUMBER):
        return None

    return {
        "name": name,
        "stem": stem,
        "date": date,
        "seq": seq,
        "graft_depth": graft_depth,
    }


def _normalize_branch_ref(ref: Optional[str]) -> Optional[str]:
    """Normalize a porcelain branch field (refs/heads/x) to a short name."""
    if not ref:
        return None
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/") :]
    return ref


def shard_name_from_branch(branch: Optional[str]) -> Optional[str]:
    """
    Derive a shard name from an actual git branch name, or None.

    The branch (normalized from refs/heads/ form) must carry the shard-
    prefix and the remainder must pass the pinned grammar. This is the only
    way an attached worktree becomes a member; names are never fabricated
    from directory paths.
    """
    branch = _normalize_branch_ref(branch)
    if not branch or not branch.startswith(SHARD_BRANCH_PREFIX):
        return None
    name = branch[len(SHARD_BRANCH_PREFIX) :]
    if parse_shard_name(name) is None:
        return None
    return name


# Project root detection
# Note: We need to find the git repo root for the project the user is currently
# working in (based on current working directory), not where this shard.py file lives.
# Priority: SKEIN_PROJECT env var > cwd-based git repo detection
def _find_project_root() -> Path:
    """Find the git project root, checking SKEIN_PROJECT env var first."""
    # Check SKEIN_PROJECT env var first
    env_project = os.environ.get("SKEIN_PROJECT")
    if env_project:
        project_path = Path(env_project).resolve()
        git_path = project_path / ".git"
        if git_path.is_dir():
            return project_path
        if git_path.is_file():
            # A linked worktree: project identity is the origin repo, never
            # the worktree directory (whose basename may be anything after a
            # move) — otherwise site derivation and the .skein DB both follow
            # the wrong directory.
            origin = _origin_repo_of_worktree(project_path)
            if origin is not None:
                return origin
            raise ShardError(
                f"SKEIN_PROJECT points at a worktree whose origin repository "
                f"cannot be resolved: {env_project}"
            )
        # If SKEIN_PROJECT is set but invalid, raise an error
        raise ShardError(
            f"SKEIN_PROJECT env var points to non-git directory: {env_project}"
        )

    # Fall back to cwd-based detection
    current = Path.cwd()  # Start from current working directory

    # Walk up until we find a .git directory (real repo root, not worktree)
    while current != current.parent:
        git_path = current / ".git"
        if git_path.exists():
            # Check if this is a real .git directory (repo root) or a file (worktree)
            if git_path.is_dir():
                # Found the real project root
                return current
            elif git_path.is_file():
                # This is a worktree - its .git file names the origin repo,
                # which is what makes a worktree traceable from anywhere.
                origin = _origin_repo_of_worktree(current)
                if origin is not None:
                    return origin
        current = current.parent

    # Fallback: couldn't find git repo
    raise ShardError(
        "Not in a git repository. Run 'skein shard' commands from within a git repo, "
        "or set SKEIN_PROJECT env var."
    )


# Lazy-initialized project root. None means not yet resolved.
# Use get_project_root() to access - never access directly.
_PROJECT_ROOT: Optional[Path] = None
_WORKTREES_DIR: Optional[Path] = None


def get_project_root() -> Path:
    """Get the project root, resolving lazily if needed."""
    global _PROJECT_ROOT, _WORKTREES_DIR
    if _PROJECT_ROOT is None:
        _PROJECT_ROOT = _find_project_root()
        _WORKTREES_DIR = _PROJECT_ROOT / "worktrees"
    return _PROJECT_ROOT


def get_worktrees_dir() -> Path:
    """Get the creation-placement directory for NEW shard worktrees.

    Placement is a creation-time policy only: it decides where spawn and
    graft put a new worktree, and it labels the resolver's last-resort guess.
    It is never consulted for membership or discovery — existing shards are
    found through git regardless of where they live. The default is the
    in-tree `<repo>/worktrees/`; no environment variable participates
    (SKEIN_WORKTREES_DIR deliberately has no effect).
    """
    global _WORKTREES_DIR
    if _WORKTREES_DIR is None:
        get_project_root()  # This sets _WORKTREES_DIR
    return _WORKTREES_DIR


def _placement_path(worktree_name: str) -> Path:
    """The path where creation policy would place a worktree of this name.

    This is the ONLY place `worktrees_dir / name` may be computed. Its
    callers are spawn/graft placement and the resolver's labeled guess arm;
    every other operation must resolve real locations through resolve_shard.
    """
    return get_worktrees_dir() / worktree_name


def _path_forms(path: Path) -> List[Path]:
    """
    Return a path plus its symlink-resolved form, when they differ.

    Both are needed because git reports worktree paths already resolved
    (/private/var on macOS) while SKEIN_HOME may still be expressed through
    the symlink (/var) - comparing only one form misses real matches.
    """
    forms = [path]
    try:
        resolved = path.resolve()
    except OSError:
        return forms
    if resolved != path:
        forms.append(resolved)
    return forms


def _paths_equal(a: Path, b: Path) -> bool:
    """Exact path equality under raw and symlink-resolved forms (invariant 8)."""
    forms_a = _path_forms(a)
    forms_b = _path_forms(b)
    return any(fa == fb for fa in forms_a for fb in forms_b)


def set_project_root(path: str) -> None:
    """
    Override the project root for shard operations.

    Use this to operate on a different project than the current working
    directory. A linked worktree is accepted but canonicalized to its origin
    repository (invariant 9: project identity comes from the origin repo,
    never from the worktree directory).

    Args:
        path: Path to the project root (must be a git repository or a
            linked worktree of one)
    """
    global _PROJECT_ROOT, _WORKTREES_DIR

    project_path = Path(path).resolve()
    git_path = project_path / ".git"
    if git_path.is_file():
        origin = _origin_repo_of_worktree(project_path)
        if origin is None:
            raise ShardError(
                f"Cannot resolve the origin repository of worktree: {path}"
            )
        project_path = origin
    elif not git_path.exists():
        raise ShardError(f"Not a git repository: {path}")

    _PROJECT_ROOT = project_path
    _WORKTREES_DIR = _PROJECT_ROOT / "worktrees"


def _get_repo() -> "git.Repo":
    """Get git.Repo instance for project."""
    if git is None:
        raise ShardError("GitPython not installed. Run: pip install GitPython")

    try:
        return git.Repo(get_project_root())
    except git.InvalidGitRepositoryError:
        raise ShardError(f"Not a git repository: {get_project_root()}")


def _detect_default_branch(repo=None) -> str:
    """
    Detect the default branch of the project repo.

    Tries in order:
    1. git symbolic-ref --short refs/remotes/origin/HEAD (origin's stated default)
    2. Check if 'main' or 'master' local branch exists

    Args:
        repo: Optional git.Repo instance. If None, uses the project repo.

    Returns:
        Branch name string, e.g. 'main' or 'master'

    Raises:
        ShardError: If the default branch cannot be determined
    """
    import subprocess

    working_dir = None
    if repo is not None:
        try:
            working_dir = str(repo.working_dir)
        except Exception:
            pass
    if working_dir is None:
        try:
            working_dir = str(get_project_root())
        except Exception:
            raise ShardError(
                "Cannot determine default branch: project root is not set. "
                "Run from a git repository or set the project root explicitly."
            )

    # Try origin/HEAD first — most authoritative for remote-backed repos
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=working_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            ref = result.stdout.strip()
            if ref:
                # Format is "origin/main" → "main"
                return ref.split("/", 1)[-1]
    except Exception:
        pass

    # Check which local branches exist and prefer 'main' over 'master'
    try:
        result = subprocess.run(
            ["git", "branch", "--list"],
            cwd=working_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            branches = {
                line.strip().lstrip("* ")
                for line in result.stdout.splitlines()
                if line.strip()
            }
            for candidate in ("main", "master"):
                if candidate in branches:
                    return candidate
    except Exception:
        pass

    raise ShardError(
        "Cannot determine default branch: neither 'main' nor 'master' exists locally "
        "and origin/HEAD is not set. Pass --base explicitly or set up the remote."
    )


# Cached git version (None = not yet checked, tuple = parsed version)
_GIT_VERSION: Optional[Tuple[int, ...]] = None


def _get_git_version() -> Tuple[int, ...]:
    """
    Get the git version as a tuple of integers.

    Returns:
        Version tuple, e.g., (2, 38, 1) for git 2.38.1

    Raises:
        ShardError: If git version cannot be determined
    """
    global _GIT_VERSION
    if _GIT_VERSION is not None:
        return _GIT_VERSION

    try:
        import subprocess

        result = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, check=True
        )
        # Output format: "git version 2.38.1" or "git version 2.38.1.windows.1"
        version_str = result.stdout.strip()
        # Extract version number after "git version "
        if version_str.startswith("git version "):
            version_str = version_str[12:]
        # Take only the first version component (before any OS-specific suffix)
        version_parts = version_str.split()[0].split(".")
        # Parse as integers, ignoring non-numeric parts
        version = []
        for part in version_parts:
            try:
                version.append(int(part))
            except ValueError:
                break
        if len(version) >= 2:
            _GIT_VERSION = tuple(version)
            return _GIT_VERSION
        raise ShardError(f"Could not parse git version: {result.stdout}")
    except subprocess.CalledProcessError as e:
        raise ShardError(f"Failed to get git version: {e}")
    except FileNotFoundError:
        raise ShardError("git not found. Please install git.")


def _check_git_version_for_merge_tree() -> None:
    """
    Check that git version supports three-argument merge-tree.

    Three-argument merge-tree (merge-tree BASE OURS THEIRS) requires git 2.38+.
    Earlier versions only support two-argument form.

    Raises:
        ShardError: If git version is too old
    """
    version = _get_git_version()
    if version < (2, 38):
        raise ShardError(
            f"Git version {'.'.join(map(str, version))} is too old for conflict detection.\n"
            f"The three-argument 'git merge-tree' command requires git 2.38 or later.\n"
            f"Please upgrade git to use drift detection and graft features."
        )


# Maximum sequence number per name per day (3 digits = 001-999)
MAX_SEQUENCE_NUMBER = 999


def _get_next_sequence(name: str, date: str) -> int:
    """
    Get next sequence number for name on given date.

    Enumerates existing BRANCHES matching `shard-{name}-{date}-*` (Decision C).
    Branches are the real uniqueness namespace — creation fails loudly on a
    branch collision — and branch enumeration is location-free, so sequences
    whose directories are gone but whose branches survive are still skipped.
    Only suffixes that are grammar-valid exact three-digit sequences (001-999)
    are counted.

    Args:
        name: Shard name identifier
        date: Date string (YYYYMMDD)

    Returns:
        Next sequence number (e.g., 1, 2, 3...)

    Raises:
        ShardError: If sequence limit (999) would be exceeded
    """
    repo = _get_repo()
    branch_prefix = f"{SHARD_BRANCH_PREFIX}{name}-{date}-"
    try:
        refs_output = repo.git.for_each_ref(
            "--format=%(refname:short)", f"refs/heads/{branch_prefix}*"
        )
    except Exception as e:
        raise ShardError(f"Failed to enumerate shard branches: {e}")

    sequences = []
    for ref in refs_output.splitlines():
        suffix = ref[len(branch_prefix) :]
        # Exact three digits, 001-999; anything else (including -graft
        # branches, four-digit runs, and 000) is not a sequence.
        if _SEQ_PATTERN.match(suffix) and 1 <= int(suffix) <= MAX_SEQUENCE_NUMBER:
            sequences.append(int(suffix))

    next_seq = max(sequences) + 1 if sequences else 1

    if next_seq > MAX_SEQUENCE_NUMBER:
        raise ShardError(
            f"Sequence limit exceeded for '{name}' on {date}.\n"
            f"Maximum {MAX_SEQUENCE_NUMBER} shards per name per day.\n"
            f"Consider using a different name or cleaning up old shards."
        )

    return next_seq


def spawn_shard(
    name: str,
    brief_id: Optional[str] = None,
    description: Optional[str] = None,
    project_root: Optional[str] = None,
    base_branch: Optional[str] = None,
) -> Dict[str, str]:
    """
    Create SHARD: git branch + worktree for isolated agent work.

    Args:
        name: Descriptive name for this shard (e.g., 'fix-auth-bug', 'add-dark-mode')
        brief_id: Optional brief ID this SHARD relates to
        description: Optional work description
        project_root: Optional path to git repo. If not provided, auto-detects.
        base_branch: Branch to fork from and merge back to. If None, auto-detected from repo.

    Returns:
        {
            'shard_id': 'shard-20251109-abc123',
            'name': 'fix-auth-bug',
            'branch_name': 'shard-fix-auth-bug-20251109-001',
            'worktree_path': '/abs/path/to/worktrees/fix-auth-bug-20251109-001',
            'brief_id': 'brief-123' or None,
            'created_at': '2025-11-09T12:00:00'
        }

    Raises:
        ShardError: If worktree creation fails or name is invalid
    """
    # Validate name
    is_valid, error_msg = validate_shard_name(name)
    if not is_valid:
        raise ShardError(f"Invalid name: {error_msg}")

    # Set project root if provided
    if project_root:
        set_project_root(project_root)

    # Auto-detect base branch if not explicitly provided
    if base_branch is None:
        base_branch = _detect_default_branch()

    # Check if spawning from inside a worktree and record parent relationship
    import sys

    parent_shard = detect_shard_environment()
    parent_worktree_name = None
    if parent_shard:
        parent_name = parent_shard.get("worktree_name", "unknown")
        parent_branch = parent_shard.get("branch_name", "unknown")
        parent_worktree_name = parent_shard.get("worktree_name")
        print(
            f"\n⚠️  WARNING: Spawning shard from inside worktree '{parent_name}'\n"
            f"   The new shard will branch from '{parent_branch}', not the primary branch.\n"
            f"   If you want to branch from the primary branch, run from the project root:\n"
            f"   cd {get_project_root()}\n",
            file=sys.stderr,
        )

    # Generate date and sequence
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    seq = _get_next_sequence(name, date)

    # Generate names. Placement (where the directory goes) is decided here,
    # at creation time, and only here; the directory name matching the shard
    # name is a convention for human legibility, not an identity requirement.
    worktree_name = f"{name}-{date}-{seq:03d}"
    branch_name = f"{SHARD_BRANCH_PREFIX}{worktree_name}"
    worktree_path = _placement_path(worktree_name)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    # Generate shard_id - use worktree_name which includes name+date+seq for uniqueness
    shard_id = f"shard-{worktree_name}"

    # Check if worktree already exists
    if worktree_path.exists():
        raise ShardError(f"Worktree already exists: {worktree_path}")

    # Create git worktree with new branch using GitPython
    try:
        repo = _get_repo()
        # Record base_commit BEFORE creating worktree (HEAD of base_branch)
        base_commit = repo.git.rev_parse(base_branch)
        repo.git.worktree("add", str(worktree_path), "-b", branch_name, base_branch)
    except Exception as e:
        if git and isinstance(e, git.GitCommandError):
            raise ShardError(f"Failed to create worktree: {e}")
        raise

    created_at = datetime.now(timezone.utc)

    # Record metadata in SQLite database for drift detection
    _record_shard_metadata(
        worktree_name=worktree_name,
        base_commit=base_commit,
        created_at=created_at,
        spawned_by=name,  # Use name as spawned_by for now
        brief_id=brief_id,
        description=description,
        parent_worktree=parent_worktree_name,  # Track nested shard relationship
        base_branch=base_branch,
        worktree_path=str(worktree_path.absolute()),
    )

    # Return SHARD info
    return {
        "shard_id": shard_id,
        "name": name,
        "branch_name": branch_name,
        "worktree_path": str(worktree_path.absolute()),
        "worktree_name": worktree_name,
        "brief_id": brief_id,
        "description": description,
        "created_at": created_at.isoformat(),
        "base_commit": base_commit,
        "base_branch": base_branch,
        "status": "spawned",
    }


def _is_path_inside_worktree(path: Path, worktree_path: Path) -> bool:
    """
    Check if a path is inside a worktree directory.

    Resolves both paths to handle symlinks and relative paths correctly.
    IMPORTANT: Fails closed - returns True if we can't verify, to prevent
    accidental self-deletion on permission errors or broken symlinks.

    Args:
        path: Path to check
        worktree_path: Worktree directory path

    Returns:
        True if path is inside or equal to worktree_path (or if we can't verify)
    """
    try:
        # Check BOTH unresolved and resolved paths to prevent symlink bypass.
        # An attacker could create a symlink inside worktree pointing outside,
        # then cd to the symlink target. We need to catch this.

        # Check 1: Is the literal (unresolved) path inside worktree?
        # This catches symlinks created inside the worktree
        try:
            unresolved_inside = path == worktree_path or worktree_path in path.parents
        except (ValueError, TypeError):
            unresolved_inside = False

        # Check 2: Does the resolved path point inside worktree?
        # This catches when we're physically inside the worktree
        resolved_path = path.resolve()
        resolved_worktree = worktree_path.resolve()
        resolved_inside = (
            resolved_path == resolved_worktree
            or resolved_worktree in resolved_path.parents
        )

        # Block if EITHER check indicates we're inside
        return unresolved_inside or resolved_inside

    except (OSError, ValueError):
        # FAIL CLOSED: if we can't verify, assume inside to prevent deletion
        return True


def _branch_exists(repo, branch_name: str) -> bool:
    """Check whether a local branch exists."""
    try:
        repo.git.show_ref("--verify", "--quiet", f"refs/heads/{branch_name}")
        return True
    except Exception:
        return False


def _commit_contained_in(repo, committish: str, base_branch: str) -> bool:
    """
    Ancestry-only containment proof: is committish an ancestor of base_branch?

    This is the ONLY merge proof cleanup accepts. Squash-merge and rebase
    integration do not leave the original commits in the base's ancestry, so
    an externally squash-merged branch reads as unmerged here — that is the
    safe direction, and deleting such a branch requires the explicit
    --delete-unmerged-branch override.
    """
    try:
        repo.git.merge_base("--is-ancestor", committish, base_branch)
        return True
    except Exception:
        return False


def _branch_merged_into(repo, branch_name: str, base_branch: str) -> bool:
    """Merge proof over a branch name (see _commit_contained_in)."""
    return _commit_contained_in(repo, branch_name, base_branch)


def _branch_oid(repo, branch_name: str) -> Optional[str]:
    """Current commit OID of a local branch, or None if it does not exist."""
    try:
        return repo.git.rev_parse("--verify", f"refs/heads/{branch_name}").strip()
    except Exception:
        return None


def _delete_branch_at_oid(repo, branch_name: str, expected_oid: str) -> None:
    """Delete a branch iff its ref still points at expected_oid.

    `git update-ref -d <ref> <expected>` takes the ref lock and refuses when
    the ref has moved, so deletion authority (merge proof or the explicit
    override, both settled against expected_oid) can never reach commits
    that arrived on the ref after the proof. Unlike `git branch -D`,
    update-ref does not refuse a checked-out branch, so that guard is
    reinstated here before the deletion. Raises on any refusal.
    """
    for stanza in _list_worktree_stanzas(repo):
        if stanza.get("branch") == branch_name:
            raise ShardError(
                f"branch {branch_name} is checked out at {stanza['path']}"
            )
    repo.git.update_ref("-d", f"refs/heads/{branch_name}", expected_oid)


def _stanza_for_path(repo, worktree_path: Path) -> Optional[Dict[str, Any]]:
    """The porcelain stanza of the linked worktree at worktree_path, if any."""
    for stanza in _list_worktree_stanzas(repo):
        if not stanza["is_main"] and _paths_equal(Path(stanza["path"]), worktree_path):
            return stanza
    return None


def _undisclosed_dirty(
    dirty_files: List[str],
    authorized_dirty_files: Optional[List[str]],
    discard_uncommitted: bool,
) -> List[str]:
    """Dirty status lines whose discard nothing has authorized.

    authorized_dirty_files, when given, is the exact disclosed set the
    caller confirmed: anything outside it is undisclosed regardless of
    discard_uncommitted, so a confirmation never stretches to dirt that
    appeared after disclosure. With no disclosed set, discard_uncommitted
    is the caller's blanket programmatic authority over this one worktree.
    """
    if not dirty_files:
        return []
    if authorized_dirty_files is not None:
        allowed = set(authorized_dirty_files)
        return [ln for ln in dirty_files if ln not in allowed]
    if discard_uncommitted:
        return []
    return list(dirty_files)


def _restore_live_worktree_path(
    repo,
    worktree_name: str,
    expected_path: Path,
    branch_name: str,
    detached: bool,
    expected_head: Optional[str],
    path_was_cleared: bool,
) -> None:
    """Best-effort compensation after a failed git worktree removal.

    The pre-clear revokes a DB path before git is touched. On failure, restore
    only a binding freshly corroborated by git porcelain: an attached member
    is identified by its branch (and may have moved again), while a detached
    member needs both the exact path and the HEAD captured before mutation.
    Never restore the row's prior path: it may be stale and already reused by
    an unrelated detached worktree. Any uncertainty or write failure leaves
    the row pathless, which is the safe direction because it binds nothing.
    """
    if not path_was_cleared:
        return
    try:
        stanzas = _list_worktree_stanzas(repo)
    except Exception:
        return

    live_path = None
    for stanza in stanzas:
        if stanza["is_main"] or stanza["bare"]:
            continue
        if detached:
            if (
                expected_head
                and stanza["detached"]
                and stanza["head"] == expected_head
                and _paths_equal(Path(stanza["path"]), expected_path)
            ):
                live_path = stanza["path"]
                break
        elif not stanza["detached"] and stanza["branch"] == branch_name:
            live_path = stanza["path"]
            break

    if live_path is None:
        return
    try:
        _set_worktree_path(worktree_name, str(Path(live_path).absolute()))
    except Exception:
        pass


def _worktree_dirty_files(worktree_path: Path) -> List[str]:
    """Porcelain status lines for a worktree's uncommitted changes.

    Read-only. An unreadable or gone worktree reports as clean here; git's
    own removal validation backstops that (a tree git cannot read refuses
    plain and --force removal alike).
    """
    try:
        if git is None:
            return []
        wt_repo = git.Repo(str(worktree_path))
        status = wt_repo.git.status("--porcelain")
        return [ln for ln in status.splitlines() if ln.strip()]
    except Exception:
        return []


def _worktree_dirty_fingerprint(worktree_path: Path) -> str:
    """Digest the exact tracked/index and untracked state cleanup may discard.

    Porcelain lines identify paths and status categories for people, but they
    do not change when bytes at an already-dirty path change. This digest is
    the machine-side half of disclosure authority: it binds the index, tracked
    working-tree diff, every non-ignored untracked file's type, mode, and
    content, and the corresponding state of every populated tracked submodule
    at confirmation time.

    Fail closed. If the state cannot be read consistently, cleanup has no
    exact discard authority and must not force-remove the worktree.
    """

    def git_bytes(*args: str) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(worktree_path), *args],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            error = result.stderr.decode(errors="replace").strip()
            raise ShardError(
                f"Cannot snapshot uncommitted state in {worktree_path}: "
                f"git {' '.join(args)} failed: {error}"
            )
        return result.stdout

    def update_framed(target, label: bytes, payload: bytes) -> None:
        """Add one unambiguous typed field to a digest input stream."""
        target.update(len(label).to_bytes(2, "big"))
        target.update(label)
        target.update(len(payload).to_bytes(8, "big"))
        target.update(payload)

    digest = hashlib.sha256()
    for label, args in (
        (b"status", ("status", "--porcelain=v1", "-z", "--untracked-files=all")),
        (
            b"index",
            (
                "diff",
                "--cached",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                "HEAD",
                "--",
            ),
        ),
        (
            b"worktree",
            ("diff", "--binary", "--no-ext-diff", "--no-textconv", "--"),
        ),
    ):
        payload = git_bytes(*args)
        update_framed(digest, label, payload)

    untracked = git_bytes("ls-files", "--others", "--exclude-standard", "-z")
    root = os.fsencode(str(worktree_path))
    for relative in sorted(path for path in untracked.split(b"\0") if path):
        full_path = os.path.join(root, relative)
        try:
            metadata = os.lstat(full_path)
            entry = hashlib.sha256()
            update_framed(entry, b"path", relative)
            update_framed(
                entry,
                b"type",
                stat.S_IFMT(metadata.st_mode).to_bytes(4, "big"),
            )
            update_framed(
                entry,
                b"mode",
                stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"),
            )
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(full_path)
                if isinstance(target, str):
                    target = os.fsencode(target)
                update_framed(entry, b"symlink-target", target)
            elif stat.S_ISREG(metadata.st_mode):
                content = hashlib.sha256()
                length = 0
                with open(full_path, "rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        content.update(chunk)
                        length += len(chunk)
                update_framed(entry, b"content-length", length.to_bytes(8, "big"))
                update_framed(entry, b"content-sha256", content.digest())
            else:
                update_framed(entry, b"size", str(metadata.st_size).encode())
            update_framed(digest, b"untracked-entry", entry.digest())
        except OSError as e:
            raise ShardError(
                f"Cannot snapshot uncommitted state in {worktree_path}: "
                f"{os.fsdecode(relative)} changed while being read: {e}"
            ) from e

    # A parent diff says only that a gitlink is dirty; it does not bind the
    # bytes changed inside that submodule. Recurse through each populated
    # gitlink so a same-path rewrite below it changes the parent's authority
    # fingerprint too. Unpopulated gitlinks are recorded explicitly, making a
    # concurrent initialization visible without following arbitrary symlinks.
    index_entries = git_bytes("ls-files", "--stage", "-z")
    gitlinks: Dict[bytes, List[Tuple[bytes, bytes, bytes]]] = {}
    for record in (entry for entry in index_entries.split(b"\0") if entry):
        try:
            metadata, relative = record.split(b"\t", 1)
            mode, oid, stage_number = metadata.split()
        except ValueError as e:
            raise ShardError(
                f"Cannot snapshot uncommitted state in {worktree_path}: "
                "git ls-files returned a malformed index entry"
            ) from e
        if mode != b"160000":
            continue

        gitlinks.setdefault(relative, []).append((mode, oid, stage_number))

    for relative in sorted(gitlinks):
        full_path = os.path.join(root, relative)
        git_marker = os.path.join(full_path, b".git")
        populated = bool(
            not os.path.islink(full_path)
            and os.path.isdir(full_path)
            and os.path.exists(git_marker)
        )
        entry = hashlib.sha256()
        update_framed(entry, b"path", relative)
        for mode, oid, stage_number in sorted(gitlinks[relative]):
            index_entry = hashlib.sha256()
            update_framed(index_entry, b"mode", mode)
            update_framed(index_entry, b"oid", oid)
            update_framed(index_entry, b"stage", stage_number)
            update_framed(entry, b"index-entry", index_entry.digest())
        update_framed(entry, b"populated", b"1" if populated else b"0")
        if populated:
            nested = _worktree_dirty_fingerprint(Path(os.fsdecode(full_path)))
            update_framed(entry, b"nested-state-sha256", nested.encode("ascii"))
        update_framed(digest, b"submodule-entry", entry.digest())

    return digest.hexdigest()


def _registered_worktree_paths(repo) -> List[Path]:
    """Paths of every linked worktree registration git currently lists."""
    return [Path(s["path"]) for s in _list_worktree_stanzas(repo) if not s["is_main"]]


def _prune_registrations_preserving_metadata(repo) -> None:
    """Global `git worktree prune` plus invariant-11 repair for collateral rows.

    `git worktree prune` is global: it drops EVERY prunable registration,
    not just the caller's target. Any row whose recorded registration
    disappears in this prune while its branch survives has just become
    branch-only, so its path is cleared (row and lineage preserved —
    metadata follows the branch). Rows whose branch is also gone are stale
    rows: phase-5 gc material, never touched silently.

    Two safety rules govern the global prune:
    - A missing DETACHED worktree's registration is the only reachability
      record of its HEAD commit, and this hygiene pass carries no authority
      over that commit — if such a registration exists, the prune is
      skipped entirely. Discarding it belongs to that shard's own cleanup,
      which settles HEAD-containment authority and removes its registration
      by targeted `git worktree remove --force`.
    - Rows whose registration this prune is about to drop while their
      branch survives have their recorded path cleared BEFORE the git
      mutation: a DB failure then aborts with git untouched, and a git
      failure leaves only a pathless row — never a dead path that could
      bind unrelated detached work later. The after-check remains as a
      backstop for registrations that vanish unpredictedly.
    """
    stanzas = [s for s in _list_worktree_stanzas(repo) if not s["is_main"]]
    for stanza in stanzas:
        if stanza["detached"] and not os.path.isdir(stanza["path"]):
            return
    doomed = [
        Path(s["path"])
        for s in stanzas
        if not s["locked"] and (s["prunable"] or not os.path.isdir(s["path"]))
    ]
    for row in _load_all_rows():
        row_path = row.get("worktree_path")
        if not row_path:
            continue
        if not any(_paths_equal(Path(row_path), p) for p in doomed):
            continue
        if _branch_exists(repo, f"{SHARD_BRANCH_PREFIX}{row['worktree_name']}"):
            _set_worktree_path(row["worktree_name"], None)
    before = [Path(s["path"]) for s in stanzas]
    repo.git.worktree("prune")
    after = _registered_worktree_paths(repo)
    for row in _load_all_rows():
        row_path = row.get("worktree_path")
        if not row_path:
            continue
        recorded = Path(row_path)
        was_registered = any(_paths_equal(recorded, p) for p in before)
        still_registered = any(_paths_equal(recorded, p) for p in after)
        if not was_registered or still_registered:
            continue
        if _branch_exists(repo, f"{SHARD_BRANCH_PREFIX}{row['worktree_name']}"):
            _set_worktree_path(row["worktree_name"], None)


def _resolve_proof_base(
    worktree_name: str, explicit_base: Optional[str]
) -> Optional[str]:
    """
    The base branch that merge proof may be checked against.

    Comes ONLY from the shard's DB row (base_branch) or an explicit caller
    argument — never from default-branch detection, the current HEAD, or any
    other guess. A guessed base could prove "merged" against the wrong branch
    and authorize deleting unmerged work.
    """
    if explicit_base:
        return explicit_base
    row = _get_shard_metadata(worktree_name)
    if row and row.get("base_branch"):
        return row["base_branch"]
    return None


def _cleanup_shard_detailed(
    worktree_name: str,
    keep_branch: bool = False,
    caller_cwd: Optional[str] = None,
    project_root: Optional[str] = None,
    delete_unmerged_branch: bool = False,
    base_branch: Optional[str] = None,
    discard_uncommitted: bool = False,
    authorized_dirty_files: Optional[List[str]] = None,
    authorized_dirty_fingerprint: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Remove SHARD worktree; delete the branch only under explicit authority.

    Branch deletion has exactly two authorities (invariant 6):
    - merge proof: an ancestry check showing the branch is contained in its
      base, where the base comes only from the DB row or the base_branch
      argument (never guessed); or
    - the explicit delete_unmerged_branch override.

    keep_branch skips deletion entirely. When the branch survives (kept or
    preserved absent authority), the DB row survives with it, with only
    worktree_path cleared (invariant 11 — metadata follows the branch).
    Internal git force flags are directory mechanics and never cascade to
    branches.

    Args:
        worktree_name: Shard name, or the exact absolute path of a member's
            worktree (resolved to its member — basenames are never extracted).
        keep_branch: If True, keep git branch after removing worktree.
        caller_cwd: Optional path checked against self-deletion.
        project_root: Optional path to git repo. If not provided, auto-detects.
        delete_unmerged_branch: Explicit authority to delete a branch that is
            not provably merged. This is the only such authority; interactive
            confirmation (--yes) never substitutes for it.
        base_branch: Explicit base for the merge proof when no DB row records
            one. Never guessed.
        discard_uncommitted: Blanket programmatic confirmation that this
            worktree's uncommitted changes may be discarded. Ignored when
            authorized_dirty_files is given.
        authorized_dirty_files: The exact dirty status lines a disclosure
            confirmed (the CLI passes the set it printed). Discard authority
            is exact to this set: any dirty file outside it — dirt that
            appeared after disclosure — refuses, and the recheck runs again
            immediately before any force removal.
        authorized_dirty_fingerprint: Digest of the disclosed dirty state.
            Required whenever authorized_dirty_files permits discarding a
            nonempty state; catches same-path content or index changes whose
            porcelain status line stays unchanged.

    Returns:
        Dict describing what happened: worktree_removed, worktree_pruned,
        branch_deleted, branch_preserved, row_deleted, path_cleared,
        branch_name, merged, message.

    Raises:
        ShardError: On refusal (unknown/branch-only/locked/ambiguous names,
        unauthorized branch deletion, unconfirmed dirty worktree) or failure.
    """
    if project_root:
        set_project_root(project_root)

    if not worktree_name or not str(worktree_name).strip():
        raise ShardError(
            f"Invalid worktree name: '{worktree_name}'\n"
            f"Expected a specific worktree name like 'agent-20251109-001'"
        )

    repo = _get_repo()
    resolution = resolve_shard(worktree_name)
    # The resolver maps an exact member-path input to its member; use the
    # canonical name for everything downstream (rows are keyed by name).
    worktree_name = resolution["name"]
    branch_name = resolution["branch"]

    if resolution.get("ambiguous"):
        raise ShardError(
            f"Cannot cleanup '{worktree_name}': multiple DB rows claim its "
            f"detached worktree at {resolution['path']}.\n"
            f"This is corrupt metadata; inspect with `skein shard triage` and "
            f"repair the rows before any destructive operation."
        )

    if not resolution["registered"]:
        # No git worktree entry for this name.
        if resolution["branch_exists"]:
            raise ShardError(
                f"'{worktree_name}' is a branch-only shard (branch "
                f"{branch_name} survives with no worktree).\n"
                f"Branch-only shards are observational-only: cleanup will not "
                f"touch the branch or its metadata.\n"
                f"To revive it: git worktree add <path> {branch_name}\n"
                f"To inspect it: skein shard triage"
            )
        raise ShardError(
            f"'{worktree_name}' has no worktree and no surviving branch "
            f"(a stale metadata row may remain; see `skein shard triage`).\n"
            f"Nothing to clean up."
        )

    if resolution.get("locked"):
        raise ShardError(
            f"Cannot cleanup '{worktree_name}': its worktree is locked "
            f"({resolution['path']}).\n"
            f"Unlock it first if you really mean it: git worktree unlock "
            f"{resolution['path']}"
        )

    worktree_path = Path(resolution["path"])
    missing = not worktree_path.exists()

    # Self-deletion guards (meaningful only while the directory exists)
    if not missing:
        if caller_cwd:
            caller_path = Path(caller_cwd)
            if _is_path_inside_worktree(caller_path, worktree_path):
                raise ShardError(
                    f"Cannot cleanup: caller_cwd is inside the target worktree.\n"
                    f"caller_cwd: {caller_cwd}\n"
                    f"This may indicate self-deletion attempt."
                )
        try:
            current_dir = Path.cwd()
            if _is_path_inside_worktree(current_dir, worktree_path):
                raise ShardError(
                    f"Cannot cleanup from inside the worktree.\n"
                    f"Your shell is currently in: {current_dir}\n"
                    f"Please change directory first: cd {get_project_root()}"
                )
        except OSError:
            pass

    # Branch-deletion authority is settled BEFORE touching anything, and it
    # is settled against the exact OID the deletion will later require: a
    # proof over the ref NAME would let the ref move between proof and
    # deletion and the delete would then discard commits nothing proved.
    branch_oid = _branch_oid(repo, branch_name)
    branch_present = branch_oid is not None
    proof_base = _resolve_proof_base(worktree_name, base_branch)
    merged = False
    if branch_present and proof_base and _branch_exists(repo, proof_base):
        merged = _commit_contained_in(repo, branch_oid, proof_base)

    want_delete = not keep_branch and branch_present
    authorized = merged or delete_unmerged_branch
    preserve_branch = False

    if want_delete and not authorized:
        if missing:
            # The directory is already gone; prune the stale registration but
            # preserve the unmerged branch and its metadata row.
            preserve_branch = True
        else:
            if proof_base is None:
                raise ShardError(
                    f"Cannot cleanup '{worktree_name}': branch {branch_name} "
                    f"has no recorded base branch to prove a merge against.\n"
                    f"No base is ever guessed. Your options:\n"
                    f"  - pass --base <branch> to check merge proof explicitly\n"
                    f"  - pass --delete-unmerged-branch to delete it anyway "
                    f"(you assume the loss)\n"
                    f"  - pass --keep-branch to remove only the worktree\n"
                    f"  - leave the shard alone"
                )
            raise ShardError(
                f"Cannot cleanup '{worktree_name}': branch {branch_name} is "
                f"not provably merged into {proof_base}.\n"
                f"(Ancestry check only — a squash-merged or rebased branch "
                f"also reads as unmerged; check with `git branch --merged "
                f"{proof_base}`.)\n"
                f"Your options:\n"
                f"  - merge it first: skein shard merge {worktree_name}\n"
                f"  - pass --delete-unmerged-branch to delete it anyway "
                f"(you assume the loss)\n"
                f"  - pass --keep-branch to remove only the worktree\n"
                f"  - leave the shard alone"
            )

    # Detached-worktree discard authority: removing (or pruning) a detached
    # worktree discards its actual HEAD commit, which may have advanced past
    # the shard branch — the branch's merge proof says nothing about it. The
    # discard needs its own proof over the exact HEAD being discarded
    # (containment in the proof base) or the explicit override.
    detached_head = None
    if resolution.get("detached"):
        member_stanza = _stanza_for_path(repo, worktree_path)
        detached_head = member_stanza["head"] if member_stanza else None
        head_contained = bool(
            detached_head
            and proof_base
            and _branch_exists(repo, proof_base)
            and _commit_contained_in(repo, detached_head, proof_base)
        )
        if not head_contained and not delete_unmerged_branch:
            shown = detached_head[:12] if detached_head else "unknown"
            base_shown = proof_base if proof_base else "any recorded base"
            raise ShardError(
                f"Cannot cleanup '{worktree_name}': its worktree is detached "
                f"at commit {shown}, which is not provably contained in "
                f"{base_shown}.\n"
                f"Removing the worktree would discard that commit.\n"
                f"Your options:\n"
                f"  - finish or abort the rebase/bisect so the branch "
                f"reattaches, then retry\n"
                f"  - pass --base <branch> if the commit is merged elsewhere\n"
                f"  - pass --delete-unmerged-branch to discard it anyway "
                f"(you assume the loss)\n"
                f"  - leave the shard alone"
            )

    # Uncommitted-work confirmation (git-side check, before any removal).
    # Discard authority is exact: with a disclosed set, any file outside it
    # refuses — a confirmation never stretches to dirt that appeared after
    # the disclosure it confirmed.
    if not missing:
        dirty_files = _worktree_dirty_files(worktree_path)
        undisclosed = _undisclosed_dirty(
            dirty_files, authorized_dirty_files, discard_uncommitted
        )
        dirty_state_changed = bool(
            dirty_files
            and authorized_dirty_files is not None
            and (
                authorized_dirty_fingerprint is None
                or _worktree_dirty_fingerprint(worktree_path)
                != authorized_dirty_fingerprint
            )
        )
        if undisclosed or dirty_state_changed:
            listed = undisclosed or dirty_files
            listing = "\n".join(f"  {ln}" for ln in listed[:20])
            more = len(listed) - 20
            if more > 0:
                listing += f"\n  ... and {more} more"
            beyond = (
                " beyond the disclosed set"
                if authorized_dirty_files is not None
                else ""
            )
            err = ShardError(
                f"Worktree has uncommitted changes{beyond}"
                + (
                    " whose content or index state changed after disclosure"
                    if dirty_state_changed
                    else ""
                )
                + f":\n{listing}\n"
                f"Cleanup would discard them. Confirm explicitly "
                f"(interactively, or --yes at the CLI) to proceed."
            )
            err.dirty_files = dirty_files
            err.dirty_fingerprint = (
                _worktree_dirty_fingerprint(worktree_path) if dirty_files else None
            )
            raise err

    result: Dict[str, Any] = {
        "worktree_removed": False,
        "worktree_pruned": False,
        "branch_deleted": False,
        "branch_preserved": False,
        "row_deleted": False,
        "path_cleared": False,
        "branch_name": branch_name,
        "merged": merged,
        "message": "",
    }

    # Cross-store ordering: the row's recorded path is authority to bind
    # detached work, so it is revoked BEFORE any git mutation. A DB failure
    # here aborts with git untouched; any git failure after this point
    # leaves at worst a pathless row (branch-only metadata, or a stale row
    # triage surfaces) — never a dead path that could bind an unrelated
    # worktree created at the same location later.
    row = _get_shard_metadata(worktree_name)
    prior_path = row.get("worktree_path") if row else None
    if prior_path is not None:
        try:
            _set_worktree_path(worktree_name, None)
        except Exception as e:
            raise ShardError(
                f"Cleanup aborted before touching git state: could not clear "
                f"the recorded worktree path for '{worktree_name}': {e}"
            )

    if missing:
        # The directory is gone; remove exactly this registration. --force
        # here is registration mechanics for an absent directory and carries
        # no branch authority. Targeted, unlike global prune, so no other
        # worktree's registration — or detached HEAD — is touched.
        try:
            repo.git.worktree("remove", "--force", str(worktree_path))
        except Exception as e:
            _restore_live_worktree_path(
                repo,
                worktree_name,
                worktree_path,
                branch_name,
                bool(resolution.get("detached")),
                detached_head,
                prior_path is not None,
            )
            raise ShardError(f"Failed to prune stale worktree registration: {e}")
        result["worktree_pruned"] = True
    else:
        try:
            repo.git.worktree("remove", str(worktree_path))
        except Exception:
            # Plain removal refuses dirty trees. Recheck against the same
            # authority before forcing, so dirt that appeared between the
            # disclosure check and removal refuses instead of being wiped —
            # --force stays directory mechanics, never discard authority.
            current_dirty = _worktree_dirty_files(worktree_path)
            late = _undisclosed_dirty(
                current_dirty,
                authorized_dirty_files,
                discard_uncommitted,
            )
            try:
                dirty_state_changed = bool(
                    current_dirty
                    and authorized_dirty_files is not None
                    and (
                        authorized_dirty_fingerprint is None
                        or _worktree_dirty_fingerprint(worktree_path)
                        != authorized_dirty_fingerprint
                    )
                )
            except ShardError:
                _restore_live_worktree_path(
                    repo,
                    worktree_name,
                    worktree_path,
                    branch_name,
                    bool(resolution.get("detached")),
                    detached_head,
                    prior_path is not None,
                )
                raise
            if late or dirty_state_changed:
                _restore_live_worktree_path(
                    repo,
                    worktree_name,
                    worktree_path,
                    branch_name,
                    bool(resolution.get("detached")),
                    detached_head,
                    prior_path is not None,
                )
                listing = "\n".join(
                    f"  {ln}" for ln in (late or current_dirty)[:20]
                )
                raise ShardError(
                    f"Refusing to force-remove '{worktree_name}': uncommitted "
                    f"changes appeared or changed after disclosure:\n{listing}\n"
                    f"Nothing was removed. Re-run cleanup to re-disclose."
                )
            try:
                repo.git.worktree("remove", "--force", str(worktree_path))
            except Exception as e2:
                _restore_live_worktree_path(
                    repo,
                    worktree_name,
                    worktree_path,
                    branch_name,
                    bool(resolution.get("detached")),
                    detached_head,
                    prior_path is not None,
                )
                raise ShardError(f"Failed to remove worktree: {e2}")
        result["worktree_removed"] = True

    if want_delete and authorized:
        try:
            _delete_branch_at_oid(repo, branch_name, branch_oid)
        except Exception as e:
            # The worktree is gone but the branch survived — possibly
            # because the ref moved past the OID the proof covered. Its row
            # already lost its path in the pre-transition, so this failure
            # state is exactly branch-only metadata (invariant 11), never a
            # dead pointer.
            raise ShardError(
                f"Removed worktree but failed to delete branch {branch_name}: "
                f"{e}\n"
                f"If the branch moved since the merge proof, re-run cleanup "
                f"to prove its current tip (or pass --delete-unmerged-branch)."
            )
        result["branch_deleted"] = True
        result["message"] = (
            f"Removed worktree and deleted branch {branch_name} "
            f"({'merge-proven' if merged else 'explicit override'})."
        )
        # The row is deleted only together with its branch (invariant 11).
        # Its path is already cleared, so a failure here leaves a harmless
        # pathless stale row (triage-visible), which is reported, not raised.
        if row is not None:
            try:
                _delete_shard_row(worktree_name)
                result["row_deleted"] = True
            except Exception as e:
                result["message"] += (
                    f"\nNote: the metadata row could not be deleted and "
                    f"remains as a pathless stale row (see `skein shard "
                    f"triage`): {e}"
                )
    else:
        # Branch survives: metadata follows the branch. Preserve the row;
        # its recorded path was already cleared in the pre-transition.
        if branch_present:
            result["branch_preserved"] = True
        if row is not None:
            result["path_cleared"] = True
        if preserve_branch:
            result["message"] = (
                f"Pruned stale worktree registration. Branch {branch_name} is "
                f"not provably merged and was PRESERVED with its metadata "
                f"(now branch-only; path cleared).\n"
                f"Check with: git branch --merged"
                + (f" {proof_base}" if proof_base else "")
                + f"\nRevive with: git worktree add <path> {branch_name}\n"
                f"To delete through SKEIN, revive it first, then run: "
                f"skein shard cleanup {worktree_name} "
                f"--delete-unmerged-branch"
            )
        elif keep_branch and branch_present:
            result["message"] = (
                f"Removed worktree; branch {branch_name} kept with its "
                f"metadata (path cleared)."
            )
        else:
            result["message"] = "Removed worktree."

    # Prune stale worktree references (global: collateral rows repaired too)
    try:
        _prune_registrations_preserving_metadata(repo)
    except Exception:
        pass  # Ignore prune errors

    return result


def cleanup_shard(
    worktree_name: str,
    keep_branch: bool = False,
    caller_cwd: Optional[str] = None,
    project_root: Optional[str] = None,
    delete_unmerged_branch: bool = False,
    base_branch: Optional[str] = None,
    discard_uncommitted: bool = False,
) -> bool:
    """
    Remove SHARD worktree; delete the branch only under explicit authority.

    Public wrapper preserving the long-standing bool contract: returns True
    on success and raises ShardError otherwise, so existing callers
    comparing `cleanup_shard(...) is True` keep working. The detailed
    outcome dict lives on _cleanup_shard_detailed (used by the CLI and
    chain cleanup); the authority semantics are documented there.
    """
    _cleanup_shard_detailed(
        worktree_name,
        keep_branch=keep_branch,
        caller_cwd=caller_cwd,
        project_root=project_root,
        delete_unmerged_branch=delete_unmerged_branch,
        base_branch=base_branch,
        discard_uncommitted=discard_uncommitted,
    )
    return True


def _list_worktree_stanzas(repo=None) -> List[Dict[str, Any]]:
    """
    Parse `git worktree list --porcelain` into one dict per worktree.

    Captures the full stanza: path, HEAD, branch (normalized from
    refs/heads/ form), detached, bare, locked (with reason), prunable (with
    reason). The first stanza is the main worktree and is marked is_main.
    """
    if repo is None:
        repo = _get_repo()
    try:
        output = repo.git.worktree("list", "--porcelain")
    except Exception as e:
        raise ShardError(f"Failed to list worktrees: {e}")

    stanzas: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for line in output.splitlines():
        if not line.strip():
            if current is not None:
                stanzas.append(current)
                current = None
            continue
        if line.startswith("worktree "):
            if current is not None:
                stanzas.append(current)
            current = {
                "path": line[len("worktree ") :],
                "head": None,
                "branch": None,
                "detached": False,
                "bare": False,
                "locked": False,
                "lock_reason": None,
                "prunable": False,
                "prunable_reason": None,
            }
            continue
        if current is None:
            continue
        if line.startswith("HEAD "):
            current["head"] = line[len("HEAD ") :]
        elif line.startswith("branch "):
            current["branch"] = _normalize_branch_ref(line[len("branch ") :])
        elif line == "detached":
            current["detached"] = True
        elif line == "bare":
            current["bare"] = True
        elif line == "locked" or line.startswith("locked "):
            current["locked"] = True
            if line.startswith("locked "):
                current["lock_reason"] = line[len("locked ") :]
        elif line == "prunable" or line.startswith("prunable "):
            current["prunable"] = True
            if line.startswith("prunable "):
                current["prunable_reason"] = line[len("prunable ") :]
    if current is not None:
        stanzas.append(current)

    # git lists the main worktree first
    for i, stanza in enumerate(stanzas):
        stanza["is_main"] = i == 0
    return stanzas


def _member_entry(name: str, stanza: Dict[str, Any], source: str, detached: bool) -> Dict[str, Any]:
    """Build a member dict (list_shards shape) from a name and its stanza."""
    parsed = parse_shard_name(name) or {}
    path = stanza["path"]
    branch_name = f"{SHARD_BRANCH_PREFIX}{name}"
    return {
        # Long-standing keys (frozen contract)
        "worktree_name": name,
        "worktree_path": path,
        "branch_name": branch_name,
        "name": parsed.get("stem", name),
        "date": parsed.get("date", ""),
        "seq": parsed.get("seq", ""),
        # Additive keys
        "exists": os.path.isdir(path),
        "source": source,
        "detached": detached,
        "branch": branch_name,
        "locked": stanza["locked"],
        "prunable": stanza["prunable"],
    }


def _classify_worktrees(repo=None) -> Dict[str, Any]:
    """
    Classify every linked worktree of the origin repo. Pure and read-only.

    Membership (Decision A): a linked worktree is a member when its ACTUAL
    checked-out branch is shard-<name> with <name> passing the pinned
    grammar — at any location, any basename. A detached worktree is a member
    only through exactly one DB row whose worktree_path matches it exactly
    (raw or symlink-resolved). Everything else is classified for triage:
    foreign shard-branch worktrees, detached-unbound, detached-ambiguous.

    Works with no DB file (detached worktrees degrade to unbound).
    """
    stanzas = _list_worktree_stanzas(repo)
    rows = _load_all_rows()

    members: List[Dict[str, Any]] = []
    foreign: List[Dict[str, Any]] = []
    detached_unbound: List[Dict[str, Any]] = []
    detached_ambiguous: List[Dict[str, Any]] = []

    linked = [s for s in stanzas if not s["is_main"] and not s["bare"]]

    # First pass: attached membership, from the actual branch only.
    attached_names = set()
    for stanza in linked:
        name = shard_name_from_branch(stanza["branch"])
        if name:
            attached_names.add(name)

    for stanza in linked:
        if stanza["detached"]:
            # Detached binding: exact path equality against recorded rows.
            # A row whose name is already attached elsewhere cannot bind —
            # the attached git member wins and that row's path is just stale.
            candidates = []
            for row in rows:
                row_path = row.get("worktree_path")
                if not row_path:
                    continue
                if row["worktree_name"] in attached_names:
                    continue
                if parse_shard_name(row["worktree_name"]) is None:
                    continue
                if _paths_equal(Path(row_path), Path(stanza["path"])):
                    candidates.append(row)
            if len(candidates) == 1:
                members.append(
                    _member_entry(
                        candidates[0]["worktree_name"], stanza, source="db", detached=True
                    )
                )
            elif len(candidates) > 1:
                detached_ambiguous.append(
                    {
                        "path": stanza["path"],
                        "head": stanza["head"],
                        "names": sorted(r["worktree_name"] for r in candidates),
                    }
                )
            else:
                detached_unbound.append(
                    {"path": stanza["path"], "head": stanza["head"]}
                )
            continue

        name = shard_name_from_branch(stanza["branch"])
        if name:
            members.append(_member_entry(name, stanza, source="git", detached=False))
        elif stanza["branch"] and stanza["branch"].startswith(SHARD_BRANCH_PREFIX):
            # shard-* branch that fails the grammar (e.g. Spindle fallback
            # shard-<hex8>): visible in triage, never operated on.
            foreign.append({"path": stanza["path"], "branch": stanza["branch"]})

    return {
        "members": members,
        "foreign": foreign,
        "detached_unbound": detached_unbound,
        "detached_ambiguous": detached_ambiguous,
        "stanzas": stanzas,
    }


def list_shards(active_only: bool = True) -> List[Dict[str, str]]:
    """
    List SHARD worktrees (members only).

    Membership is decided by each worktree's actual checked-out branch under
    the pinned grammar (or, for detached worktrees, an exact DB path
    binding) — never by the worktree's location or directory basename.

    Returns:
        List of SHARD info dicts with keys:
        - worktree_name (the shard name — branch minus the shard- prefix)
        - worktree_path
        - branch_name
        - name (the shard stem)
        - date, seq (from the shard name)
        - exists, source, detached, branch, locked, prunable (additive)
    """
    return _classify_worktrees()["members"]


def get_shard_status(worktree_name: str) -> Optional[Dict[str, str]]:
    """
    Get status of a specific SHARD.

    Args:
        worktree_name: Shard name (matched verbatim — a valid canonical name
            may contain '/'), or the exact absolute path of a member's
            worktree (raw or symlink-resolved), which keeps full-path
            invocation working even after a basename-changing move. Basenames
            are never extracted: the directory name is not the identity, and
            non-absolute input is never a path — only a canonical name.

    Returns:
        SHARD info dict or None if not found
    """
    shards = list_shards()
    for shard in shards:
        if shard["worktree_name"] == worktree_name:
            return shard
    # Only absolute input may enter the path arm (see resolve_shard).
    if Path(worktree_name).is_absolute():
        for shard in shards:
            if _paths_equal(Path(worktree_name), Path(shard["worktree_path"])):
                return shard
    return None


def resolve_shard(name: str) -> Dict[str, Any]:
    """
    The single resolver: turn a shard name into its real location.

    Authority order (Decision B):
      1. git   — the live porcelain entry whose branch is shard-<name>
                 (or whose detached path is bound to <name> by the DB row);
      2. db    — the recorded worktree_path, when git no longer lists it;
      3. guess — the computed creation-placement default, always labeled
                 source "guess" and never presented as fact.

    Pure: never writes, and works against a read-only DB and checkout.
    Name lookups are exact-match only, on the verbatim input — a valid
    canonical name may contain '/', so basenames are never extracted. The
    one non-name input accepted is the exact absolute path of a member's
    worktree (raw or symlink-resolved), which resolves to that member and
    keeps full-path invocation working after a basename-changing move.
    Non-absolute input is always a verbatim canonical name, never a
    cwd-relative path.

    Returns:
        {name, path, source, exists, detached, branch, branch_exists,
         registered, locked, prunable, ambiguous}

    Raises:
        ShardError: for unknown names (no worktree, no branch, no recorded
        path) — with nearest-name suggestions, never a fabricated path.
    """
    if not name:
        raise ShardError("Shard name is required")

    repo = _get_repo()
    classification = _classify_worktrees(repo)

    matched = None
    for member in classification["members"]:
        if member["worktree_name"] == name:
            matched = member
            break
    if matched is None and Path(name).is_absolute():
        # Only absolute input may enter the path arm. Relative syntax
        # belongs to canonical names (which may contain '/'); resolving it
        # against cwd would let a bare name capture a different member
        # moved to <cwd>/<name>.
        for member in classification["members"]:
            if _paths_equal(Path(name), Path(member["worktree_path"])):
                matched = member
                break
    if matched is not None:
        name = matched["worktree_name"]
        branch_name = f"{SHARD_BRANCH_PREFIX}{name}"
        return {
            "name": name,
            "path": matched["worktree_path"],
            "source": matched["source"],
            "exists": matched["exists"],
            "detached": matched["detached"],
            "branch": branch_name,
            "branch_exists": _branch_exists(repo, branch_name),
            "registered": True,
            "locked": matched["locked"],
            "prunable": matched["prunable"],
            "ambiguous": False,
        }

    branch_name = f"{SHARD_BRANCH_PREFIX}{name}"

    # A detached worktree claimed by multiple rows including this name:
    # report it, flagged ambiguous, so destructive operations refuse.
    for amb in classification["detached_ambiguous"]:
        if name in amb["names"]:
            return {
                "name": name,
                "path": amb["path"],
                "source": "db",
                "exists": os.path.isdir(amb["path"]),
                "detached": True,
                "branch": branch_name,
                "branch_exists": _branch_exists(repo, branch_name),
                "registered": True,
                "locked": False,
                "prunable": False,
                "ambiguous": True,
            }

    branch_exists = _branch_exists(repo, branch_name)
    row = _get_shard_metadata(name)
    row_path = row.get("worktree_path") if row else None

    if row_path:
        # git no longer lists this worktree; the recorded path is the best
        # honest answer (typically pruned or lost).
        return {
            "name": name,
            "path": row_path,
            "source": "db",
            "exists": os.path.isdir(row_path),
            "detached": False,
            "branch": branch_name,
            "branch_exists": branch_exists,
            "registered": False,
            "locked": False,
            "prunable": False,
            "ambiguous": False,
        }

    if branch_exists:
        # Branch-only shard: no worktree, no recorded path. The placement
        # default is a labeled guess, never fact.
        guess = _placement_path(name)
        return {
            "name": name,
            "path": str(guess),
            "source": "guess",
            "exists": guess.is_dir(),
            "detached": False,
            "branch": branch_name,
            "branch_exists": True,
            "registered": False,
            "locked": False,
            "prunable": False,
            "ambiguous": False,
        }

    # Unknown shard (a row with neither a path nor a surviving branch is a
    # stale row, not a shard). Exact-match only; suggest near misses.
    known = sorted(
        {m["worktree_name"] for m in classification["members"]}
        | {
            n
            for n in (
                shard_name_from_branch(b) for b in _list_shard_branches(repo)
            )
            if n
        }
    )
    import difflib

    suggestions = difflib.get_close_matches(name, known, n=3, cutoff=0.6)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    stale_note = (
        f"\n(A metadata row exists for '{name}' but its branch and worktree "
        f"are gone — a stale row; see `skein shard triage`.)"
        if row
        else ""
    )
    raise ShardError(f"Unknown shard: {name}.{hint}{stale_note}")


def _list_shard_branches(repo=None) -> List[str]:
    """All local branches starting with the shard- prefix (short names)."""
    if repo is None:
        repo = _get_repo()
    try:
        output = repo.git.for_each_ref(
            "--format=%(refname:short)", f"refs/heads/{SHARD_BRANCH_PREFIX}*"
        )
    except Exception:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def _refresh_row_path_if_moved(worktree_name: str, actual_path: str) -> bool:
    """
    Write-through path refresh for mutating lifecycle operations.

    When a mutating operation resolves a member via git and the row's
    recorded path disagrees (the worktree moved), refresh it. Updates only
    existing rows; returns True when a write happened. Read commands must
    never call this.
    """
    row = _get_shard_metadata(worktree_name)
    if row is None:
        return False
    recorded = row.get("worktree_path")
    if recorded and _paths_equal(Path(recorded), Path(actual_path)):
        return False
    _set_worktree_path(worktree_name, str(Path(actual_path).absolute()))
    return True


def _create_minimal_row(worktree_name: str, worktree_path: str) -> None:
    """Create a metadata row for an already-existing member (reconcile and
    lifecycle write-through via _bind_member_row).

    Records only what git proves: the name and the current path. base_commit
    is stored as '' (unknown — every reader treats a falsy base_commit as
    absent) and base_branch as explicit NULL, deliberately overriding the
    column's 'master' default, so such a row can never supply a guessed
    base for cleanup's merge proof. Lineage is never invented.
    """
    conn = _get_db_connection()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO shards
            (worktree_name, worktree_path, base_commit, base_branch, created_at, status)
            VALUES (?, ?, '', NULL, ?, 'active')
            """,
            (
                worktree_name,
                worktree_path,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _bind_member_row(worktree_name: str, actual_path: str) -> None:
    """Write-through binding for mutating lifecycle operations (tender, graft).

    Ensures the member has a metadata row bound to its actual path: creates
    a minimal row (name and path only — never invented lineage, per
    _create_minimal_row) when none exists, otherwise refreshes a stale
    recorded path. Errors propagate: a lifecycle operation that cannot
    record its binding must fail loudly, or a later detach silently loses
    membership.
    """
    if _get_shard_metadata(worktree_name) is None:
        _create_minimal_row(worktree_name, str(Path(actual_path).absolute()))
    else:
        _refresh_row_path_if_moved(worktree_name, actual_path)


def reconcile_shards() -> Dict[str, int]:
    """
    Explicit metadata reconciliation (the only non-lifecycle write path).

    - Binds every current member: creates a minimal row when none exists and
      backfills/corrects worktree_path where git disagrees (git is the
      authority for paths).
    - Clears worktree_path on rows whose recorded worktree git no longer
      lists (the binding is dead; the row and its lineage survive).
    - Never deletes rows, never invents lineage. Idempotent.

    Returns:
        {"bound": rows created, "updated": paths corrected/backfilled,
         "cleared": paths cleared, "unchanged": members already correct}
    """
    classification = _classify_worktrees()
    members = classification["members"]
    rows = _load_all_rows()
    rows_by_name = {r["worktree_name"]: r for r in rows}

    bound = updated = cleared = unchanged = 0
    for member in members:
        name = member["worktree_name"]
        actual = str(Path(member["worktree_path"]).absolute())
        row = rows_by_name.get(name)
        if row is None:
            _create_minimal_row(name, actual)
            bound += 1
        elif not row.get("worktree_path"):
            _set_worktree_path(name, actual)
            updated += 1
        elif not _paths_equal(Path(row["worktree_path"]), Path(actual)):
            _set_worktree_path(name, actual)
            updated += 1
        else:
            unchanged += 1

    member_names = {m["worktree_name"] for m in members}
    linked_stanzas = [s for s in classification["stanzas"] if not s["is_main"]]
    for row in rows:
        row_path = row.get("worktree_path")
        if not row_path or row["worktree_name"] in member_names:
            continue
        stanza = next(
            (
                s
                for s in linked_stanzas
                if _paths_equal(Path(row_path), Path(s["path"]))
            ),
            None,
        )
        if stanza is not None:
            # A worktree still sits at the recorded path but is NOT this
            # row's member (this loop only sees non-member rows): it was
            # reassigned to another branch, or it is detached without
            # validly binding this row. Retaining the binding would let a
            # later detach at that path adopt unrelated work under this
            # row's name, so it is cleared like a gone path — with one
            # exception: a detached tree claimed by multiple rows is corrupt
            # state that triage flags as ambiguous, and reconcile refuses to
            # guess which claim is the real one.
            ambiguous_claim = stanza["detached"] and any(
                row["worktree_name"] in amb["names"]
                and _paths_equal(Path(row_path), Path(amb["path"]))
                for amb in classification["detached_ambiguous"]
            )
            if ambiguous_claim:
                continue
        _set_worktree_path(row["worktree_name"], None)
        cleared += 1

    return {
        "bound": bound,
        "updated": updated,
        "cleared": cleared,
        "unchanged": unchanged,
    }


def get_triage_extras() -> Dict[str, Any]:
    """
    Observability beyond live members, for `skein shard triage`. Read-only.

    Returns:
        - foreign: shard-* branch worktrees failing the grammar
        - detached_unbound: detached worktrees with no DB binding
        - detached_ambiguous: detached worktrees claimed by multiple rows
        - branch_only: grammar-valid shard branches with no worktree
          (revivable; includes recorded lineage when a row survives)
        - stale_rows: rows with neither a worktree nor a surviving branch
        - moved: members whose directory basename differs from their name
    """
    classification = _classify_worktrees()
    repo = _get_repo()
    members = classification["members"]
    member_names = {m["worktree_name"] for m in members}

    shard_branches = _list_shard_branches(repo)
    rows_by_name = {r["worktree_name"]: r for r in _load_all_rows()}

    branch_only = []
    for branch in shard_branches:
        name = shard_name_from_branch(branch)
        if name and name not in member_names:
            row = rows_by_name.get(name)
            branch_only.append(
                {
                    "name": name,
                    "branch": branch,
                    "has_row": row is not None,
                    "base_branch": row.get("base_branch") if row else None,
                }
            )

    # Every name a shard-* branch answers to, grammar-valid or not: a row
    # whose branch survives is live metadata, not stale.
    branch_owned_names = {
        b[len(SHARD_BRANCH_PREFIX) :] for b in shard_branches
    }
    stale_rows = sorted(
        r["worktree_name"]
        for r in rows_by_name.values()
        if r["worktree_name"] not in member_names
        and r["worktree_name"] not in branch_owned_names
    )

    moved = [
        {
            "worktree_name": m["worktree_name"],
            "worktree_path": m["worktree_path"],
            "basename": Path(m["worktree_path"]).name,
        }
        for m in members
        if Path(m["worktree_path"]).name != m["worktree_name"]
    ]

    return {
        "foreign": classification["foreign"],
        "detached_unbound": classification["detached_unbound"],
        "detached_ambiguous": classification["detached_ambiguous"],
        "branch_only": branch_only,
        "stale_rows": stale_rows,
        "moved": moved,
    }


def _origin_repo_of_worktree(worktree_path: Path) -> Optional[Path]:
    """
    Resolve the repo a linked worktree belongs to, via git-common-dir.

    Asks git itself (`rev-parse --git-common-dir`) instead of parsing the
    .git file by hand. The common dir is requested in absolute form; when a
    git too old for --path-format returns a relative path, it is resolved
    against the worktree (the -C cwd it is relative to), never against our
    own working directory.

    Returns:
        The origin repo path, or None if it can't be determined
    """
    # Only linked worktrees trace anywhere: they mark themselves with a .git
    # FILE. A .git directory means this already is a main repo.
    try:
        if not (Path(worktree_path) / ".git").is_file():
            return None
    except OSError:
        return None
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["git", "-C", str(worktree_path), "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return None
        common = Path(result.stdout.strip())
        if not common.is_absolute():
            common = (Path(worktree_path) / common).resolve()
    except OSError:
        return None

    if common.name != ".git":
        return None
    main_repo = common.parent
    if (main_repo / ".git").is_dir():
        return main_repo
    return None


def get_shard_age_days(shard_info: Dict[str, str]) -> Optional[int]:
    """
    Calculate age of a SHARD in days from its date string.

    Args:
        shard_info: SHARD info dict with 'date' key (YYYYMMDD format)

    Returns:
        Age in days, or None if date parsing fails
    """
    date_str = shard_info.get("date", "")
    if not date_str or len(date_str) != 8:
        return None

    try:
        shard_date = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
        today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        delta = today - shard_date
        # Return 0 for future dates (negative age) - handles clock skew
        return max(0, delta.days)
    except ValueError:
        return None


def get_review_queue(stale_days: int = 7) -> Dict[str, List[Dict]]:
    """
    Get all SHARDs organized by review status for QM visibility.

    Groups shards into:
    - ready: Has commits, clean working tree, no conflicts (ready to merge)
    - needs_commit: Has uncommitted changes
    - conflicts: Would have merge conflicts with base branch
    - stale: No commits and older than stale_days

    Args:
        stale_days: Number of days without commits before a shard is considered stale

    Returns:
        Dict with keys: 'ready', 'needs_commit', 'conflicts', 'stale'
        Each contains list of shard dicts with added 'git_info' and 'age_days' fields
    """
    queue = {"ready": [], "needs_commit": [], "conflicts": [], "stale": []}

    shards = list_shards()

    for shard in shards:
        # Get git info for status determination
        git_info = get_shard_git_info(shard["worktree_name"])
        age_days = get_shard_age_days(shard)

        # Build enriched shard info
        enriched = {
            **shard,
            "git_info": git_info,
            "age_days": age_days,
            "commits_ahead": git_info.get("commits_ahead", 0),
            "working_tree": git_info.get("working_tree", "unknown"),
            "merge_status": git_info.get("merge_status", "unknown"),
            "diffstat": git_info.get("diffstat", ""),
        }

        # Categorize by status
        working_tree = git_info.get("working_tree", "unknown")
        merge_status = git_info.get("merge_status", "unknown")
        commits_ahead = git_info.get("commits_ahead", 0)

        # Check for uncommitted changes first (most actionable)
        if working_tree == "dirty":
            queue["needs_commit"].append(enriched)
        # Then check for merge conflicts
        elif merge_status == "conflict":
            queue["conflicts"].append(enriched)
        # Check if ready (has commits, clean, no conflicts)
        elif commits_ahead > 0 and working_tree == "clean" and merge_status == "clean":
            queue["ready"].append(enriched)
        # Check if stale (no commits, old)
        elif commits_ahead == 0 and age_days is not None and age_days >= stale_days:
            queue["stale"].append(enriched)
        # Everything else goes to ready if it has commits
        elif commits_ahead > 0:
            queue["ready"].append(enriched)
        # No commits but not old enough to be stale - skip or add to a default
        else:
            # Fresh shards with no commits yet - could be actively worked on
            # Include in ready with a note that they have no commits
            queue["ready"].append(enriched)

    # Sort each category by age (oldest first)
    for category in queue:
        queue[category].sort(key=lambda x: x.get("age_days") or 0, reverse=True)

    return queue


def get_shard_git_info(worktree_name: str, base_branch: Optional[str] = None) -> Dict:
    """
    Get git information for a SHARD: commits ahead, working tree status, merge status.

    base_branch optionally overrides the base used for the merge-status
    check (merge_shard passes its strictly-resolved base through so the
    conflict preview matches the branch actually merged into).

    Returns dict with:
        - commits_ahead: int
        - working_tree: 'clean' or 'dirty'
        - merge_status: 'clean', 'conflict', or 'unknown'
        - commit_log: list of (sha, message) tuples
        - diffstat: git diff --stat output (str)
        - uncommitted: list of uncommitted file changes
    """
    shard_info = get_shard_status(worktree_name)
    if not shard_info:
        return {}
    # Canonical name for row lookups (a member-path input resolves above)
    worktree_name = shard_info["worktree_name"]

    result = {
        "commits_ahead": 0,
        "working_tree": "unknown",
        "merge_status": "unknown",
        "commit_log": [],
        "diffstat": "",
        "uncommitted": [],
    }

    try:
        repo = _get_repo()
        branch = shard_info["branch_name"]
        worktree_path = shard_info["worktree_path"]

        # Get base reference (base_commit from SQLite, or fall back to master)
        base_ref = _get_shard_base_ref(worktree_name)

        # Commits ahead of base_ref (agent's actual commits)
        try:
            count = repo.git.rev_list("--count", f"{base_ref}..{branch}")
            result["commits_ahead"] = int(count)
        except Exception:
            pass

        # Working tree status (check for uncommitted changes)
        # Must run git status FROM the worktree, not pass path to main repo
        try:
            if git is None:
                raise ShardError("GitPython not installed")
            worktree_repo = git.Repo(worktree_path)
            status = worktree_repo.git.status("--porcelain")
            result["working_tree"] = "dirty" if status.strip() else "clean"
        except ShardError:
            pass  # Already handled
        except Exception:
            pass

        # Merge status - check if branch can merge cleanly into base_branch
        try:
            if base_branch is None:
                base_branch = _get_shard_base_branch(worktree_name)
            # Check git version supports three-argument merge-tree (2.38+)
            _check_git_version_for_merge_tree()
            # Find merge base
            merge_base = repo.git.merge_base(base_branch, branch)
            # Use merge-tree with base, base_branch, and branch
            merge_output = repo.git.merge_tree(merge_base, base_branch, branch)
            # If output contains conflict markers, there are conflicts
            if "<<<<<<" in merge_output or "+<<<<<<" in merge_output:
                result["merge_status"] = "conflict"
            else:
                result["merge_status"] = "clean"
        except ShardError:
            # Git version too old - can't determine merge status
            result["merge_status"] = "unknown"
        except Exception:
            result["merge_status"] = "unknown"

        # Commit log (commits on branch since base_ref - agent's actual work)
        try:
            log_output = repo.git.log("--oneline", f"{base_ref}..{branch}")
            if log_output.strip():
                for line in log_output.strip().split("\n"):
                    parts = line.split(" ", 1)
                    sha = parts[0]
                    msg = parts[1] if len(parts) > 1 else ""
                    result["commit_log"].append((sha, msg))
        except Exception:
            pass

        # Diffstat (files changed between base_ref and branch - agent's actual work)
        try:
            if result["commits_ahead"] > 0:
                diffstat = repo.git.diff("--stat", f"{base_ref}..{branch}")
                result["diffstat"] = diffstat.strip()
        except Exception:
            pass

        # Uncommitted changes in worktree
        # Must run git status FROM the worktree, not pass path to main repo
        try:
            if git is None:
                raise ShardError("GitPython not installed")
            worktree_repo = git.Repo(worktree_path)
            status = worktree_repo.git.status("--porcelain")
            if status.strip():
                result["uncommitted"] = [f for f in status.strip().split("\n") if f]
        except ShardError:
            pass  # Already handled
        except Exception:
            pass

    except Exception:
        pass

    return result


def get_tender_metadata(worktree_name: str) -> Optional[Dict]:
    """
    Gather metadata about a SHARD for tendering (ready for review).

    Collects git statistics, file changes, and commit info to help
    reviewers understand the scope of work.

    Args:
        worktree_name: SHARD worktree name

    Returns:
        Dict with tender metadata or None if SHARD not found
        Contains: commits, files_modified, branch_name, etc.
    """
    shard_info = get_shard_status(worktree_name)
    if not shard_info:
        return None
    # Canonical name for row lookups (a member-path input resolves above)
    worktree_name = shard_info["worktree_name"]

    worktree_path = Path(shard_info["worktree_path"])
    if not worktree_path.exists():
        return None

    metadata = {
        "worktree_name": worktree_name,
        "branch_name": shard_info["branch_name"],
        "name": shard_info["name"],
        "worktree_path": str(worktree_path),
    }

    # Get git statistics if possible
    try:
        repo = _get_repo()
        branch = shard_info["branch_name"]

        # Get base reference (base_commit from SQLite, or fall back to master)
        base_ref = _get_shard_base_ref(worktree_name)

        # Get commit count on this branch (since base_ref)
        try:
            # Count commits on SHARD branch since base
            commit_count = repo.git.rev_list("--count", f"{base_ref}..{branch}")
            metadata["commits"] = int(commit_count)
        except Exception:
            metadata["commits"] = 0

        # Get list of modified files (agent's actual work from base_ref)
        try:
            # Files changed between base_ref and this branch
            changed_files = repo.git.diff("--name-only", base_ref, branch)
            if changed_files:
                metadata["files_modified"] = changed_files.strip().split("\n")
            else:
                metadata["files_modified"] = []
        except Exception:
            metadata["files_modified"] = []

        # Get last commit message
        try:
            last_commit = repo.git.log("-1", "--pretty=%B", branch)
            metadata["last_commit_message"] = last_commit.strip()
        except Exception:
            metadata["last_commit_message"] = ""

    except Exception as e:
        # If git operations fail, just return basic metadata
        metadata["commits"] = 0
        metadata["files_modified"] = []
        metadata["last_commit_message"] = ""
        metadata["error"] = str(e)

    return metadata


def get_shard_drift_info(worktree_name: str) -> Dict[str, Any]:
    """
    Get comprehensive drift information for a shard.

    Returns information about:
    - Base commit (where shard branched from)
    - Base branch activity since base (commits, notable changes)
    - Work diff (agent's actual changes from base)
    - Integration diff (what would merge into base branch)
    - Conflict status

    Args:
        worktree_name: Worktree directory name

    Returns:
        Dict with drift information

    Example:
        >>> info = get_shard_drift_info('fix-bug-20260113-001')
        >>> if info['conflict_status'] == 'conflict':
        ...     print(f"Conflicts in: {', '.join(info['conflict_files'])}")
        >>> elif info['base_commits_ahead'] > 10:
        ...     print("Shard is stale, consider grafting")
    """
    shard_info = get_shard_status(worktree_name)
    if not shard_info:
        return {}
    # Canonical name for row lookups (a member-path input resolves above)
    worktree_name = shard_info["worktree_name"]

    # Get metadata from SQLite
    metadata = _get_shard_metadata(worktree_name)

    base_branch = _get_shard_base_branch(worktree_name)

    result = {
        "worktree_name": worktree_name,
        "branch_name": shard_info["branch_name"],
        "base_branch": base_branch,
        "base_commit": None,
        "base_commit_short": None,
        "base_commit_date": None,
        "has_metadata": metadata is not None,
        "base_commits_ahead": 0,
        "base_notable_changes": [],
        "is_stale": False,
        "is_nested": is_nested_shard(worktree_name),
        "conflict_status": "unknown",
        "conflict_files": [],
        "work_diff_stat": None,
        "integration_diff_stat": None,
    }

    try:
        repo = _get_repo()
        branch = shard_info["branch_name"]

        if metadata and metadata.get("base_commit"):
            base_commit = metadata["base_commit"]
            result["base_commit"] = base_commit
            result["base_commit_short"] = base_commit[:7]

            # Get base commit date
            try:
                base_date = repo.git.log("-1", "--format=%ci", base_commit)
                result["base_commit_date"] = base_date.strip()
            except Exception:
                pass

            # Count commits on base_branch since base
            try:
                count = repo.git.rev_list("--count", f"{base_commit}..{base_branch}")
                result["base_commits_ahead"] = int(count)
                result["is_stale"] = int(count) > 0
            except Exception:
                pass

            # Get notable changes on base_branch since base
            try:
                if result["base_commits_ahead"] > 0:
                    # Get file stats for changes on base_branch
                    name_status = repo.git.diff(
                        "--name-status", f"{base_commit}..{base_branch}"
                    )
                    notable = []
                    for line in name_status.strip().split("\n")[:10]:  # Limit to 10
                        if line:
                            parts = line.split("\t", 1)
                            if len(parts) == 2:
                                status, file_path = parts
                                if status == "D":
                                    notable.append(f"deleted: {file_path}")
                                elif status == "A":
                                    notable.append(f"added: {file_path}")
                                elif status.startswith("R"):
                                    notable.append(f"renamed: {file_path}")
                    result["base_notable_changes"] = notable
            except Exception:
                pass

            # Get work diff stat (agent's actual changes from base)
            try:
                work_stat = repo.git.diff("--stat", f"{base_commit}..{branch}")
                result["work_diff_stat"] = (
                    work_stat.strip() if work_stat.strip() else None
                )
            except Exception:
                pass

        # Get integration diff stat (what would merge with current base_branch)
        try:
            integration_stat = repo.git.diff("--stat", f"{base_branch}...{branch}")
            result["integration_diff_stat"] = (
                integration_stat.strip() if integration_stat.strip() else None
            )
        except Exception:
            pass

        # Test for conflicts using merge-tree
        try:
            # Check git version supports three-argument merge-tree (2.38+)
            _check_git_version_for_merge_tree()
            merge_base = repo.git.merge_base(base_branch, branch)
            merge_output = repo.git.merge_tree(merge_base, base_branch, branch)

            if "<<<<<<" in merge_output or "+<<<<<<" in merge_output:
                result["conflict_status"] = "conflict"
                # Parse conflict files from merge-tree output
                conflict_files = set()
                lines = merge_output.split("\n")
                for i, line in enumerate(lines):
                    if line.strip() == "changed in both":
                        # Next line has file info
                        if i + 1 < len(lines):
                            parts = lines[i + 1].split()
                            if len(parts) >= 4:
                                conflict_files.add(" ".join(parts[3:]))
                result["conflict_files"] = list(conflict_files)
            else:
                result["conflict_status"] = "clean"
        except ShardError:
            # Git version too old - can't determine conflict status
            result["conflict_status"] = "unknown"
        except Exception:
            result["conflict_status"] = "unknown"

    except Exception:
        pass

    return result


def _get_shard_base_ref(worktree_name: str) -> str:
    """
    Get the base reference for diffing a shard's work.

    For shards with SQLite metadata, returns the base_commit (where the shard branched).
    For legacy shards without metadata, falls back to the detected default branch.

    This ensures file counts and diffs reflect only the agent's actual work,
    not commits that existed in the base branch before the shard was created.

    Args:
        worktree_name: Worktree directory name

    Returns:
        Git ref string (commit SHA or detected default branch name)
    """
    metadata = _get_shard_metadata(worktree_name)
    if metadata and metadata.get("base_commit"):
        return metadata["base_commit"]
    # Legacy shard without metadata - detect rather than assume 'master'
    return _detect_default_branch()


def _get_shard_base_branch(worktree_name: str) -> str:
    """
    Get the base branch name for a shard.

    For shards with SQLite metadata, returns the stored base_branch.
    For legacy shards without the column, detects the repo's default branch.

    Args:
        worktree_name: Worktree directory name

    Returns:
        Branch name string (e.g., 'main', 'master', 'feature-branch')
    """
    metadata = _get_shard_metadata(worktree_name)
    if metadata and metadata.get("base_branch"):
        return metadata["base_branch"]
    # Legacy shard without base_branch - detect rather than assume 'master'
    return _detect_default_branch()


def get_shard_work_diff(worktree_name: str, stat_only: bool = False) -> Optional[str]:
    """
    Get the WORK diff: agent's actual changes from base commit.

    This shows only what the agent committed, without any false deletions
    from base branch evolution.

    Args:
        worktree_name: Worktree directory name
        stat_only: If True, return only --stat output

    Returns:
        Git diff output as string, or None if no changes or no metadata
    """
    shard_info = get_shard_status(worktree_name)
    if not shard_info:
        return None
    # Canonical name for row lookups (a member-path input resolves above)
    worktree_name = shard_info["worktree_name"]

    metadata = _get_shard_metadata(worktree_name)
    if not (metadata and metadata.get("base_commit")):
        # No base_commit metadata - fall back to integration diff
        return get_shard_diff(worktree_name, stat_only=stat_only)
    base_ref = metadata["base_commit"]

    try:
        repo = _get_repo()
        branch = shard_info["branch_name"]

        # Work diff: base_ref..branch (base_ref is the actual base commit SHA)
        if stat_only:
            diff_output = repo.git.diff("--stat", f"{base_ref}..{branch}")
        else:
            diff_output = repo.git.diff(f"{base_ref}..{branch}")

        return diff_output if diff_output.strip() else None

    except Exception as e:
        raise ShardError(f"Failed to get work diff: {e}")


def get_shard_diff(
    worktree_name: str, stat_only: bool = False, integration: bool = False
) -> Optional[str]:
    """
    Get diff between base branch and shard branch.

    By default returns integration diff (base_branch...branch) which shows what would merge.
    Use get_shard_work_diff() for agent's actual changes from base.

    Args:
        worktree_name: Worktree directory name (e.g., 'opus-security-architect-20251109-001')
        stat_only: If True, return only --stat output
        integration: If True, use three-dot diff (base_branch...branch) for merge preview

    Returns:
        Git diff output as string, or None if no changes or shard not found
    """
    shard_info = get_shard_status(worktree_name)
    if not shard_info:
        return None
    # Canonical name for row lookups (a member-path input resolves above)
    worktree_name = shard_info["worktree_name"]

    try:
        repo = _get_repo()
        branch = shard_info["branch_name"]

        # Get diff between base branch and shard branch
        base_branch = _get_shard_base_branch(worktree_name)
        diff_range = (
            f"{base_branch}...{branch}" if integration else f"{base_branch}..{branch}"
        )
        if stat_only:
            diff_output = repo.git.diff("--stat", diff_range)
        else:
            diff_output = repo.git.diff(diff_range)
        return diff_output if diff_output.strip() else None

    except Exception as e:
        raise ShardError(f"Failed to get diff: {e}")


def _graft_sequence_in_progress(worktree_path) -> Optional[str]:
    """
    Detect whether a cherry-pick sequence is paused/in-progress in a worktree.

    After `git cherry-pick <base>..<branch>` stops on a conflict, git records the
    in-progress pick (CHERRY_PICK_HEAD) and queues the remaining commits in the
    sequencer. If a user resolves only the in-progress commit and commits it, the
    working tree looks clean even though commits remain queued - merging then
    would silently drop them (see finding-20260530-3jnx). Returns a human-readable
    detail string when a sequence is still pending, else None.
    """
    try:
        if git is None:
            return None
        wt_repo = git.Repo(str(worktree_path))

        def _git_path(name: str) -> Path:
            p = Path(wt_repo.git.rev_parse("--git-path", name))
            if not p.is_absolute():
                p = Path(worktree_path) / p
            return p

        # Remaining commits queued in the sequencer todo list.
        todo = _git_path("sequencer/todo")
        if todo.exists():
            try:
                pending = [
                    ln
                    for ln in todo.read_text().splitlines()
                    if ln.strip() and not ln.lstrip().startswith("#")
                ]
            except Exception:
                pending = []
            if pending:
                # The sequencer todo retains the currently-stopped pick until
                # `--continue` advances it, so len(pending) can overstate the
                # not-yet-applied count by one. Phrase it as a lower bound
                # rather than an exact count (fell-r1). graft_shard's
                # commits_pending is the accurate count (derived from the
                # branch, not the sequencer).
                return (
                    f"at least {len(pending)} commit(s) still queued in the "
                    f"cherry-pick sequencer"
                )

        # An in-progress (unfinished) cherry-pick that has not been continued.
        if _git_path("CHERRY_PICK_HEAD").exists():
            return "a cherry-pick is in progress (run `git cherry-pick --continue`)"

    except Exception:
        return None

    return None


def merge_shard(
    worktree_name: str,
    caller_cwd: Optional[str] = None,
    project_root: Optional[str] = None,
    base_branch: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Merge shard branch into its base branch and cleanup worktree.

    Checks for uncommitted changes and merge conflicts before proceeding.
    If clean: checks out base branch, merges with --no-ff, cleans up worktree and branch.

    The base branch comes from the shard's DB row or the explicit
    base_branch argument — never from default-branch detection. A guessed
    base could merge the work into the wrong branch.

    Args:
        worktree_name: Shard name (e.g., 'fix-auth-bug-20251109-001')
        caller_cwd: Optional path to check for self-deletion. If provided, merge will
            be refused if this path is inside the target worktree. This is used to prevent
            agents from merging their own worktree after cd-ing elsewhere.
        project_root: Optional path to git repo. If not provided, auto-detects.
        base_branch: Explicit base when no DB row records one (--base).

    Returns:
        Dict with:
            - success: bool
            - message: str
            - uncommitted: list of uncommitted files (if dirty)
            - conflicts: list of conflicting files (if conflicts)
            - base_branch: the base actually resolved under the authority
              rule (row or explicit argument), for display by callers
    """
    if project_root:
        set_project_root(project_root)

    shard_info = get_shard_status(worktree_name)
    if not shard_info:
        # Not a member. resolve_shard raises for unknown names (with
        # suggestions); otherwise explain the specific refusal.
        resolution = resolve_shard(worktree_name)
        if resolution.get("ambiguous"):
            raise ShardError(
                f"Cannot merge '{worktree_name}': multiple DB rows claim its "
                f"detached worktree at {resolution['path']}.\n"
                f"Repair the metadata first (see `skein shard triage`)."
            )
        if resolution["branch_exists"]:
            raise ShardError(
                f"'{worktree_name}' is a branch-only shard (branch "
                f"{resolution['branch']} survives with no worktree).\n"
                f"Merge refuses branch-only shards: revive it first with\n"
                f"  git worktree add <path> {resolution['branch']}\n"
                f"then merge."
            )
        raise ShardError(f"SHARD not found: {worktree_name}")

    # Canonical name for row lookups (a member-path input resolves above)
    worktree_name = shard_info["worktree_name"]
    worktree_path = Path(shard_info["worktree_path"])
    branch_name = shard_info["branch_name"]

    if not worktree_path.exists():
        raise ShardError(
            f"Cannot merge '{worktree_name}': its worktree directory is "
            f"missing ({worktree_path}), so uncommitted work cannot be "
            f"verified.\n"
            f"Either revive it (git worktree add {worktree_path} "
            f"{branch_name}) and retry, or prune the stale registration with "
            f"`skein shard cleanup {worktree_name}` (the unmerged branch is "
            f"preserved)."
        )

    if shard_info.get("locked"):
        raise ShardError(
            f"Cannot merge '{worktree_name}': its worktree is locked "
            f"({worktree_path}).\n"
            f"Unlock it first: git worktree unlock {worktree_path}"
        )

    # Check if caller_cwd is inside the worktree being merged
    # This prevents agents from merging their own worktree after cd-ing elsewhere
    if caller_cwd:
        caller_path = Path(caller_cwd)
        if _is_path_inside_worktree(caller_path, worktree_path):
            raise ShardError(
                f"Cannot merge: caller_cwd is inside the target worktree.\n"
                f"caller_cwd: {caller_cwd}\n"
                f"This may indicate self-deletion attempt."
            )

    # Also check current working directory for backwards compatibility
    try:
        current_dir = Path.cwd()
        if _is_path_inside_worktree(current_dir, worktree_path):
            raise ShardError(
                f"Cannot merge from inside the worktree.\n"
                f"Your shell is currently in: {current_dir}\n"
                f"Please change directory first: cd {get_project_root()}"
            )
    except OSError:
        pass

    repo = _get_repo()

    # Base authority: the DB row or an explicit --base, never a guess
    # (degraded-metadata contract; a wrong guess merges into the wrong base).
    row = _get_shard_metadata(worktree_name)
    if base_branch is None:
        base_branch = row.get("base_branch") if row else None
    if not base_branch:
        raise ShardError(
            f"Cannot merge '{worktree_name}': no recorded base branch (the "
            f"shard has no metadata row) and no --base given.\n"
            f"No base is ever guessed. Pass it explicitly:\n"
            f"  skein shard merge {worktree_name} --base <branch>"
        )
    if not _branch_exists(repo, base_branch):
        raise ShardError(
            f"Cannot merge '{worktree_name}': base branch '{base_branch}' "
            f"does not exist."
        )

    # Refuse to merge a graft paused mid cherry-pick sequence: queued commits
    # remain unapplied and merging now would silently drop them (finding-20260530-3jnx).
    paused = _graft_sequence_in_progress(worktree_path)
    if paused:
        return {
            "success": False,
            "message": (
                f"Cannot merge: graft is paused mid-sequence ({paused}).\n"
                f"Finish applying the shard's remaining commits first:\n"
                f"  cd {worktree_path}\n"
                f"  git cherry-pick --continue   # repeat until the sequencer is empty\n"
                f"Then retry the merge."
            ),
            "uncommitted": [],
            "conflicts": [],
            "base_branch": base_branch,
        }

    # Check for uncommitted changes in the worktree
    git_info = get_shard_git_info(worktree_name, base_branch=base_branch)
    if git_info.get("working_tree") == "dirty":
        uncommitted = git_info.get("uncommitted", [])
        return {
            "success": False,
            "message": "Cannot merge: worktree has uncommitted changes",
            "uncommitted": uncommitted,
            "conflicts": [],
            "base_branch": base_branch,
        }

    # Check for merge conflicts or unknown status (fail safe)
    merge_status = git_info.get("merge_status", "unknown")
    if merge_status != "clean":
        # Get list of conflicting files
        try:
            # Check git version supports three-argument merge-tree (2.38+)
            _check_git_version_for_merge_tree()
            merge_base = repo.git.merge_base(base_branch, branch_name)
            merge_output = repo.git.merge_tree(merge_base, base_branch, branch_name)
            # Parse merge-tree output to find conflicting files
            # Format: "changed in both\n  base   ... filename\n  our    ...\n  their  ...\n@@ ... <<<<<<< .our"
            conflict_files = []
            lines = merge_output.split("\n")
            i = 0
            while i < len(lines):
                line = lines[i]
                # Look for "changed in both" sections which indicate conflicts
                if line.strip() == "changed in both":
                    # Next line has the base file info: "  base   100644 SHA filename"
                    if i + 1 < len(lines):
                        base_line = lines[i + 1]
                        parts = base_line.split()
                        if len(parts) >= 4:
                            filename = " ".join(parts[3:])
                            # Check if this section has actual conflict markers
                            # Scan ahead for <<<<<<
                            j = i + 4  # Skip base, our, their lines
                            while (
                                j < len(lines)
                                and not lines[j].startswith("changed in both")
                                and not lines[j].startswith("merged")
                            ):
                                if "<<<<<<<" in lines[j]:
                                    if filename not in conflict_files:
                                        conflict_files.append(filename)
                                    break
                                j += 1
                i += 1
        except ShardError as e:
            # Git version too old - include error message
            conflict_files = [f"(git version check failed: {e})"]
        except Exception:
            conflict_files = ["(unable to determine conflicting files)"]

        error_msg = (
            f"Cannot merge: branch has conflicts with {base_branch}"
            if merge_status == "conflict"
            else f"Cannot merge: merge status is '{merge_status}' (must be 'clean')"
        )
        return {
            "success": False,
            "message": error_msg,
            "uncommitted": [],
            "conflicts": conflict_files,
            "base_branch": base_branch,
        }

    # All checks passed - perform the merge
    # Store original branch/commit to restore if needed
    try:
        original_ref = repo.active_branch.name
    except TypeError:
        # Detached HEAD state - store the commit SHA instead
        original_ref = repo.head.commit.hexsha

    merge_succeeded = False
    try:
        # Checkout base branch
        repo.git.checkout(base_branch)

        # Merge with --no-ff to preserve branch history
        try:
            repo.git.merge("--no-ff", branch_name, "-m", f"Merge {branch_name}")
            merge_succeeded = True
        except Exception as merge_error:
            # If merge fails, abort and restore
            try:
                repo.git.merge("--abort")
            except Exception:
                pass
            raise ShardError(f"Merge failed: {merge_error}")

        # Cleanup worktree and branch. The merge that just landed IS the
        # proof: cleanup's ancestry check against the same base passes, so
        # branch and row are deleted under merge-proof authority. The tree
        # was verified clean above.
        try:
            cleanup_shard(
                worktree_name,
                keep_branch=False,
                caller_cwd=caller_cwd,
                base_branch=base_branch,
                discard_uncommitted=True,
            )
        except ShardError as cleanup_error:
            return {
                "success": True,
                "message": f"✓ Merged {branch_name} into {base_branch}\n⚠ Warning: cleanup failed: {cleanup_error}",
                "uncommitted": [],
                "conflicts": [],
                "base_branch": base_branch,
            }

        return {
            "success": True,
            "message": f"✓ Merged {branch_name} into {base_branch} and cleaned up worktree",
            "uncommitted": [],
            "conflicts": [],
            "base_branch": base_branch,
        }

    except ShardError:
        # Re-raise ShardErrors as-is
        raise
    except Exception as e:
        raise ShardError(f"Merge failed: {e}")
    finally:
        # Restore original branch/commit if merge didn't succeed
        # (If merge succeeded, we intentionally stay on base branch)
        if not merge_succeeded:
            try:
                repo.git.checkout(original_ref)
            except Exception:
                pass  # Best effort restoration


# =============================================================================
# GRAFT WORKFLOW - Conflict Resolution
# =============================================================================


def get_graft_chain_root(worktree_name: str) -> str:
    """
    Get the root worktree name by following parent_worktree links (read-only).

    Falls back to name parsing (stripping -graft suffixes) when no metadata
    exists. That fallback is display guidance only — the -graft suffix is
    suggestive for humans and is never treated as proof of parentage for
    chain operations (cleanup --chain refuses without rows).
    """
    rows_by_name = {r["worktree_name"]: r for r in _load_all_rows()}
    current = worktree_name
    visited = set()  # Prevent infinite loops
    found_in_db = False

    while current and current not in visited:
        visited.add(current)
        row = rows_by_name.get(current)
        if row:
            found_in_db = True
            if row.get("parent_worktree"):
                current = row["parent_worktree"]
            else:
                # No parent - this is the root
                break
        else:
            # Not in database - use fallback
            break

    if found_in_db and current:
        return current

    # Fallback for shards without metadata: strip -graft suffixes
    name = worktree_name
    while name.endswith(GRAFT_SUFFIX):
        name = name[: -len(GRAFT_SUFFIX)]
    return name


def get_graft_chain(worktree_name: str) -> List[str]:
    """
    Get full graft chain for a worktree using recorded parent relationships.

    Returns list of worktree names from root to current, e.g.:
    ['fix-bug-20260112-001', 'fix-bug-20260112-001-graft', 'fix-bug-20260112-001-graft-graft']

    Chain structure comes from recorded parent_worktree links only; each
    link's presence is decided by membership (so chains legitimately span
    locations and moved basenames), never by probing the worktrees
    directory, and the -graft name suffix is never treated as parentage.
    """
    rows = _load_all_rows()
    children_by_parent: Dict[str, List[str]] = {}
    for row in rows:
        parent = row.get("parent_worktree")
        if parent:
            children_by_parent.setdefault(parent, []).append(row["worktree_name"])
    member_names = {m["worktree_name"] for m in list_shards()}

    root = get_graft_chain_root(worktree_name)
    chain = []
    current: Optional[str] = root
    visited = set()

    while current and current not in visited:
        visited.add(current)
        if current in member_names:
            chain.append(current)
        kids = sorted(children_by_parent.get(current, []))
        current = kids[0] if kids else None

    return chain


def get_graft_depth(worktree_name: str) -> int:
    """Get depth in graft chain (0 = original, 1 = first graft, etc)."""
    # Count only suffix -graft occurrences, not -graft in the name part
    depth = 0
    name = worktree_name
    while name.endswith("-graft"):
        depth += 1
        name = name[:-6]  # Remove "-graft"
    return depth


def is_graft(worktree_name: str) -> bool:
    """Check if worktree is a graft (has -graft suffix)."""
    return worktree_name.endswith("-graft")


def is_nested_shard(worktree_name: str) -> bool:
    """
    Check if worktree is a nested shard (spawned from another shard, not base branch).

    A nested shard is one where:
    - The branch was created from inside another worktree, so it contains
      commits from the parent shard that aren't on base branch
    - Has parent_worktree set (recorded during spawn), OR
    - Is a graft (grafts always have parent worktrees)

    The detection is based on explicit parent tracking rather than heuristics.
    This avoids:
    - False negative when base branch hasn't moved (merge-base equals base_commit
      even for nested shards)
    - False positive when shard was rebased (merge-base changes but it's not nested)

    Nested shards cannot be merged directly to base branch without first grafting
    to isolate their changes.

    Returns:
        True if this is a nested shard
    """
    # Grafts are inherently nested - they have a parent worktree
    if is_graft(worktree_name):
        return True

    metadata = _get_shard_metadata(worktree_name)
    if not metadata:
        return False

    # Check if parent_worktree is set - this is the authoritative source
    # Wired during spawn_shard() when spawning from inside another worktree
    if metadata.get("parent_worktree"):
        return True

    # For legacy shards without parent_worktree tracking, check for commits
    # not on base branch that predate the shard creation
    base_commit = metadata.get("base_commit")
    created_at = metadata.get("created_at")
    if not base_commit or not created_at:
        return False

    shard_info = get_shard_status(worktree_name)
    if not shard_info:
        return False

    try:
        repo = _get_repo()
        branch = shard_info["branch_name"]
        base_branch = _get_shard_base_branch(worktree_name)

        # Get the actual merge-base between base_branch and this branch
        actual_merge_base = repo.git.merge_base(base_branch, branch)

        # If base_commit equals merge-base, check if there are commits between
        # the base and the branch tip that are not on base_branch and predate creation.
        # This catches nested shards where base_branch hasn't moved.
        if base_commit == actual_merge_base:
            # Get commits on branch not on base_branch, with timestamps
            # Format: "SHA TIMESTAMP" where TIMESTAMP is Unix epoch
            commits_output = repo.git.log(
                "--format=%H %ct",
                f"{actual_merge_base}..{branch}",
                "--not",
                base_branch,
            )
            if commits_output.strip():
                # Parse creation time
                from datetime import datetime as dt

                try:
                    # created_at could be string or datetime
                    if isinstance(created_at, str):
                        created_ts = dt.fromisoformat(
                            created_at.replace("Z", "+00:00")
                        ).timestamp()
                    else:
                        created_ts = created_at.timestamp()
                except (ValueError, AttributeError):
                    return False

                # Check if any commit predates shard creation
                for line in commits_output.strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 2:
                        commit_ts = int(parts[1])
                        # Allow 60 second grace period for timing skew
                        if commit_ts < created_ts - 60:
                            return True

    except Exception:
        pass

    return False


def graft_shard(
    worktree_name: str, project_root: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a graft worktree to resolve conflicts.

    Creates a new worktree from current base branch and cherry-picks commits
    from the source shard. If conflicts occur, leaves in conflicted state
    for manual resolution.

    Args:
        worktree_name: Source worktree to graft
        project_root: Optional path to git repo

    Returns:
        Dict with:
            - success: bool
            - graft_worktree_name: name of new graft worktree
            - graft_worktree_path: path to new worktree
            - conflicts: list of conflict files (if any)
            - message: status message
    """
    if project_root:
        set_project_root(project_root)

    # Verify source worktree exists
    shard_info = get_shard_status(worktree_name)
    if not shard_info:
        raise ShardError(f"Worktree not found: {worktree_name}")
    # Canonical name for row lookups (a member-path input resolves above)
    worktree_name = shard_info["worktree_name"]

    repo = _get_repo()

    # Graft is a mutating lifecycle op: create or complete the source's row
    # (minimal, no invented lineage) and refresh its recorded path if the
    # worktree has moved since it was written.
    _bind_member_row(worktree_name, shard_info["worktree_path"])

    # Get metadata for base commit
    source_metadata = _get_shard_metadata(worktree_name)

    # Generate graft name (append -graft). The graft is a NEW worktree, so
    # it is placed by current creation policy — its source stays wherever it
    # lives (mixed-location chains are first-class; the resolver ties them).
    graft_worktree_name = f"{worktree_name}{GRAFT_SUFFIX}"
    graft_branch_name = f"{SHARD_BRANCH_PREFIX}{graft_worktree_name}"
    graft_worktree_path = _placement_path(graft_worktree_name)
    graft_worktree_path.parent.mkdir(parents=True, exist_ok=True)

    # Check if graft already exists
    if graft_worktree_path.exists():
        raise ShardError(
            f"Graft worktree already exists: {graft_worktree_name}\n"
            f"Either clean up the existing graft or continue working in it."
        )

    # Get commits from source shard
    source_branch = shard_info["branch_name"]
    base_branch = _get_shard_base_branch(worktree_name)

    # Find base commit (from metadata or merge-base with base_branch)
    if source_metadata and source_metadata.get("base_commit"):
        base_commit = source_metadata["base_commit"]
    else:
        # Legacy shard without metadata - use merge-base
        base_commit = repo.git.merge_base(base_branch, source_branch)

    # Get list of commits to cherry-pick (in reverse order - oldest first)
    commits_output = repo.git.rev_list("--reverse", f"{base_commit}..{source_branch}")
    commits = commits_output.strip().split("\n") if commits_output.strip() else []

    if not commits:
        raise ShardError(
            f"No commits to graft from {worktree_name}\n"
            f"The shard has no changes relative to its base."
        )

    # Get current base_branch HEAD for new base_commit
    new_base_commit = repo.git.rev_parse(base_branch)

    # Create worktree from current base_branch
    try:
        repo.git.worktree(
            "add", str(graft_worktree_path), "-b", graft_branch_name, base_branch
        )
    except Exception as e:
        raise ShardError(f"Failed to create graft worktree: {e}")

    created_at = datetime.now(timezone.utc)

    # Record graft metadata (inherits base_branch from source shard)
    _record_shard_metadata(
        worktree_name=graft_worktree_name,
        base_commit=new_base_commit,
        created_at=created_at,
        parent_worktree=worktree_name,
        description=f"Graft of {worktree_name} for conflict resolution",
        base_branch=base_branch,
        worktree_path=str(graft_worktree_path.absolute()),
    )

    # Cherry-pick the shard's commits onto the new base as a SINGLE range
    # operation so git owns the full sequence. If an early commit conflicts,
    # git stops there and records the remaining commits in .git/sequencer; the
    # user resolves and runs `git cherry-pick --continue` to walk the rest.
    #
    # (The previous implementation cherry-picked commit-by-commit in a loop and
    # `break`ed on the first conflict, silently dropping every later commit -
    # while still reporting commits_applied=len(commits). See finding-20260530-3jnx.)
    conflict_files = []
    try:
        if git is None:
            raise ShardError("GitPython not installed")
        graft_repo = git.Repo(str(graft_worktree_path))

        try:
            graft_repo.git.cherry_pick(f"{base_commit}..{source_branch}")
        except Exception as e:
            # Cherry-pick paused - likely a conflict in one commit. The
            # remaining commits stay queued in the sequencer for --continue.
            if "conflict" in str(e).lower() or "CONFLICT" in str(e):
                try:
                    status = graft_repo.git.status("--porcelain")
                    for line in status.split("\n"):
                        if line.startswith("UU ") or line.startswith("AA "):
                            conflict_files.append(line[3:])
                except Exception:
                    pass
            else:
                raise ShardError(f"Cherry-pick failed: {e}")

    except ShardError:
        raise
    except Exception as e:
        # If something went wrong, try to clean up
        try:
            repo.git.worktree("remove", "--force", str(graft_worktree_path))
            repo.git.branch("-D", graft_branch_name)
        except Exception:
            pass
        raise ShardError(f"Failed to apply commits: {e}")

    # A cherry-pick can stop for two distinct reasons that BOTH leave the
    # sequence paused with commits still queued:
    #   1. a merge conflict - `git status` shows UU/AA files (conflict_files),
    #   2. a commit that replays EMPTY on the advanced base (its change already
    #      exists). git stops with "The previous cherry-pick is now empty,
    #      possibly due to conflict resolution." - note that string contains
    #      "conflict", so it lands in the branch above, but `git status` shows
    #      NO conflict files, so conflict_files stays empty.
    # Defining success by "no conflict files" would therefore misreport an
    # empty-commit pause as a clean graft while the sequencer still holds the
    # rest (fell-r1). Define success by whether the sequence actually DRAINED.
    paused = _graft_sequence_in_progress(graft_worktree_path)

    # Count commits that ACTUALLY landed on the graft branch. On a conflict the
    # cherry-pick stops part-way through, so this is NOT len(commits): it is the
    # number of commits between the new base and the graft HEAD. The conflicting
    # commit (in progress) plus anything still queued is reported as pending.
    try:
        applied_output = graft_repo.git.rev_list(f"{new_base_commit}..HEAD")
        commits_applied = (
            len(applied_output.strip().split("\n")) if applied_output.strip() else 0
        )
    except Exception:
        commits_applied = 0
    commits_pending = len(commits) - commits_applied

    result = {
        "success": paused is None and not conflict_files,
        "graft_worktree_name": graft_worktree_name,
        "graft_worktree_path": str(graft_worktree_path),
        "graft_branch_name": graft_branch_name,
        "source_worktree_name": worktree_name,
        "commits_total": len(commits),
        "commits_applied": commits_applied,
        "commits_pending": commits_pending,
        "conflicts": conflict_files,
        "chain_depth": get_graft_depth(graft_worktree_name),
    }

    if conflict_files:
        result["message"] = (
            f"Graft PAUSED at a conflict in: {', '.join(conflict_files)}\n"
            f"{commits_applied}/{len(commits)} commit(s) applied, "
            f"{commits_pending} still queued in the cherry-pick sequencer.\n"
            f"Resolve conflicts in: {graft_worktree_path}\n"
            f"Then run `git cherry-pick --continue` to apply the rest."
        )
    elif paused is not None:
        # No conflict files, but the sequence is still paused - typically a
        # commit that replayed empty on the new base. git stopped WITHOUT
        # creating it; the user must skip it (or commit it empty) to continue.
        result["message"] = (
            f"Graft PAUSED mid-sequence ({paused}).\n"
            f"{commits_applied}/{len(commits)} commit(s) applied, "
            f"{commits_pending} still queued in the cherry-pick sequencer.\n"
            f"No merge conflicts - a commit replayed empty on the new base "
            f"(its change is already present).\n"
            f"In: {graft_worktree_path}\n"
            f"Run `git cherry-pick --skip` to drop the empty commit "
            f"(or `git cherry-pick --continue` to keep it empty), repeating "
            f"until the sequencer is empty."
        )
    else:
        result["message"] = (
            f"Graft created cleanly - ready to merge\nLocation: {graft_worktree_path}"
        )

    return result


def get_chain_cleanup_plan(
    worktree_name: str, base_branch: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Per-link disclosure for cleanup --chain (read-only).

    For each link in the chain, reports what deleting its branch would rest
    on, whether merge proof holds, whether the override would be the
    authority, and the link's uncommitted changes (dirty_files). The CLI
    shows this BEFORE confirmation so one vague yes cannot silently
    authorize unrelated links or discard undisclosed work.

    base_branch must be the same explicit base the execution will pass to
    cleanup: both go through _resolve_proof_base per link, so the disclosed
    authority is exactly the authority applied.
    """
    repo = _get_repo()
    # A member-path input canonicalizes before any row lookup (rows are
    # keyed by name; the path is never the identity).
    member = get_shard_status(worktree_name)
    if member:
        worktree_name = member["worktree_name"]
    root = get_graft_chain_root(worktree_name)
    chain = get_graft_chain(root)
    members_by_name = {m["worktree_name"]: m for m in list_shards()}
    plan = []
    for name in chain:
        branch_name = f"{SHARD_BRANCH_PREFIX}{name}"
        branch_present = _branch_exists(repo, branch_name)
        proof_base = _resolve_proof_base(name, base_branch)
        merged = None
        if branch_present and proof_base and _branch_exists(repo, proof_base):
            merged = _branch_merged_into(repo, branch_name, proof_base)
        member = members_by_name.get(name)
        dirty_files: List[str] = []
        dirty_fingerprint: Optional[str] = None
        if member and os.path.isdir(member["worktree_path"]):
            dirty_files = _worktree_dirty_files(Path(member["worktree_path"]))
            if dirty_files:
                dirty_fingerprint = _worktree_dirty_fingerprint(
                    Path(member["worktree_path"])
                )
        plan.append(
            {
                "worktree_name": name,
                "branch_name": branch_name,
                "branch_exists": branch_present,
                "proof_base": proof_base,
                "merged": merged,
                "dirty_files": dirty_files,
                "dirty_fingerprint": dirty_fingerprint,
            }
        )
    return plan


def cleanup_graft_chain(
    worktree_name: str,
    keep_branch: bool = False,
    caller_cwd: Optional[str] = None,
    project_root: Optional[str] = None,
    delete_unmerged_branch: bool = False,
    base_branch: Optional[str] = None,
    discard_uncommitted: bool = False,
    disclosed_dirty: Optional[Dict[str, List[str]]] = None,
    disclosed_dirty_fingerprints: Optional[Dict[str, Optional[str]]] = None,
    disclosed_chain: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Clean up entire graft chain (original + all grafts).

    Requires recorded parentage: the chain is traversed through
    parent_worktree rows only, and a target with no metadata row refuses —
    the -graft name suffix is never proof of parentage. Branch-deletion
    authority is applied PER LINK: each link's merge proof is checked
    against its own recorded base, and delete_unmerged_branch authorizes
    only what per-link disclosure (get_chain_cleanup_plan) surfaced.

    Discard authority is likewise per link. With disclosed_dirty (the CLI
    passes the exact per-link dirty sets it printed), each link may discard
    only its own disclosed files — a link absent from the map authorizes
    nothing, one link's disclosure never covers another, and dirt that
    appeared after disclosure refuses. Without the map, the blanket
    discard_uncommitted bool is the caller's own per-link authority.

    Chain membership authority is exact when disclosed_chain is supplied.
    The current ordered lineage must still match the names shown before
    confirmation; otherwise cleanup refuses before its first mutation. The
    execution set is then the frozen disclosed list, never a newly expanded
    lineage.

    The chain stops at the first failed link: continuing past a failure
    would mutate links beyond it (e.g. delete a parent whose graft still
    exists) and compound the partial state the caller must report.

    Args:
        worktree_name: Any worktree in the chain (name, or exact absolute
            member path)
        keep_branch: Keep branches after removing worktrees
        caller_cwd: Caller's cwd to check for self-deletion
        project_root: Optional project root override
        delete_unmerged_branch: Per-link authority to delete unmerged branches
        base_branch: Explicit proof base for links without a recorded one
        discard_uncommitted: Blanket per-link discard confirmation; ignored
            when disclosed_dirty is given
        disclosed_dirty: Exact disclosed dirty status lines per link name
        disclosed_dirty_fingerprints: Exact dirty-state digest per link name
        disclosed_chain: Exact ordered link names disclosed before confirmation

    Returns:
        Dict with:
            - success: bool
            - removed: list of removed worktree names
            - preserved_branches: links whose branch was preserved
            - errors: list of error messages (first failure only)
            - not_attempted: links skipped because an earlier link failed
    """
    if project_root:
        set_project_root(project_root)

    # A member-path input canonicalizes before any row lookup (rows are
    # keyed by name; the path is never the identity).
    member = get_shard_status(worktree_name)
    if member:
        worktree_name = member["worktree_name"]

    if _get_shard_metadata(worktree_name) is None:
        raise ShardError(
            f"Cannot cleanup --chain: '{worktree_name}' has no metadata row, "
            f"so chain parentage cannot be proven (the -graft name suffix is "
            f"not proof).\n"
            f"Clean up each shard individually instead: skein shard cleanup "
            f"<name>"
        )

    root = get_graft_chain_root(worktree_name)
    chain = get_graft_chain(root)

    if not chain:
        raise ShardError(f"No worktrees found in chain for: {worktree_name}")

    if disclosed_chain is not None:
        frozen_chain = list(disclosed_chain)
        if chain != frozen_chain:
            disclosed = ", ".join(frozen_chain) or "(none)"
            current = ", ".join(chain) or "(none)"
            raise ShardError(
                "Cannot cleanup --chain: chain membership changed after "
                "disclosure; nothing was removed.\n"
                f"Disclosed: {disclosed}\n"
                f"Current: {current}\n"
                "Re-run cleanup to inspect and confirm the current chain."
            )
        chain = frozen_chain

    if disclosed_dirty is not None and disclosed_dirty_fingerprints is None:
        raise ShardError(
            "Cannot cleanup --chain: disclosed dirty paths lack their "
            "content fingerprints; nothing was removed. Re-run cleanup to "
            "re-disclose the chain."
        )

    removed = []
    preserved_branches = []
    errors = []
    not_attempted: List[str] = []

    # Remove in reverse order (grafts first, then original)
    reversed_chain = list(reversed(chain))
    for idx, wt_name in enumerate(reversed_chain):
        link_authorized: Optional[List[str]] = None
        link_fingerprint: Optional[str] = None
        link_discard = discard_uncommitted
        if disclosed_dirty is not None:
            link_authorized = list(disclosed_dirty.get(wt_name, []))
            link_fingerprint = disclosed_dirty_fingerprints.get(wt_name)
            link_discard = False
        try:
            link_result = _cleanup_shard_detailed(
                wt_name,
                keep_branch=keep_branch,
                caller_cwd=caller_cwd,
                delete_unmerged_branch=delete_unmerged_branch,
                base_branch=base_branch,
                discard_uncommitted=link_discard,
                authorized_dirty_files=link_authorized,
                authorized_dirty_fingerprint=link_fingerprint,
            )
            removed.append(wt_name)
            if link_result.get("branch_preserved"):
                preserved_branches.append(link_result["branch_name"])
        except ShardError as e:
            errors.append(f"{wt_name}: {e}")
            not_attempted = reversed_chain[idx + 1 :]
            break

    return {
        "success": len(errors) == 0,
        "removed": removed,
        "preserved_branches": preserved_branches,
        "errors": errors,
        "not_attempted": not_attempted,
        "chain_root": root,
    }


def detect_shard_environment() -> Optional[Dict[str, str]]:
    """
    Detect if currently running in a SHARD worktree.

    Checks if current working directory is inside a SHARD worktree
    and returns information about it.

    Returns:
        SHARD info dict if in a SHARD, None otherwise
        Dict contains: worktree_name, worktree_path, branch_name, name, date, seq
    """
    # Walk up looking for a worktree root. A linked worktree marks itself with
    # a .git *file* (pointing back at the origin repo); a real repo root has a
    # .git *directory*, which means we've left worktree territory.
    #
    # Deliberately no check on the shape of cwd: a shard can live anywhere,
    # so an agent's ability to tell it is inside one must not depend on the
    # word "worktrees" appearing in its path — or on the path at all.
    current = Path.cwd()
    worktree_root = None

    while current != current.parent:
        git_path = current / ".git"
        if git_path.is_file():
            # This is a worktree (not main repo which has .git directory)
            worktree_root = current
            break
        if git_path.is_dir():
            # Reached a real repo root without passing through a worktree
            return None
        current = current.parent

    if not worktree_root:
        return None

    # Membership is decided by the worktree's actual checked-out branch (or
    # its detached DB binding) through the same classification everything
    # else uses — never by the directory name or its location.
    try:
        members = list_shards()
    except ShardError:
        return None
    for member in members:
        if _paths_equal(Path(member["worktree_path"]), worktree_root):
            return member
    return None


if __name__ == "__main__":
    # Simple CLI for testing
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python shard_worktree.py spawn <name> [brief-id] [description]")
        print("  python shard_worktree.py list")
        print("  python shard_worktree.py cleanup <worktree-name> [--keep-branch]")
        sys.exit(1)

    command = sys.argv[1]

    if command == "spawn":
        if len(sys.argv) < 3:
            print("Error: name required")
            sys.exit(1)

        name = sys.argv[2]
        brief_id = sys.argv[3] if len(sys.argv) > 3 else None
        description = sys.argv[4] if len(sys.argv) > 4 else None

        try:
            shard = spawn_shard(name, brief_id, description)
            print(json.dumps(shard, indent=2))
        except ShardError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif command == "list":
        try:
            shards = list_shards()
            print(json.dumps(shards, indent=2))
        except ShardError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif command == "cleanup":
        if len(sys.argv) < 3:
            print("Error: worktree-name required")
            sys.exit(1)

        worktree_name = sys.argv[2]
        keep_branch = "--keep-branch" in sys.argv

        try:
            cleanup_shard(worktree_name, keep_branch)
            print(f"Cleaned up: {worktree_name}")
        except ShardError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)
