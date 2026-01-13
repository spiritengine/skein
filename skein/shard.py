#!/usr/bin/env python3
"""
SHARD Worktree Management

Manages git worktrees for SHARD agent coordination workflow.
Provides core functions for creating, cleaning up, and listing SHARDs.
"""

import os
import re
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    parent_worktree TEXT,
    base_commit TEXT NOT NULL,
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
    """Get path to shard database (in .skein directory of project root)."""
    project_root = get_project_root()
    skein_dir = project_root / ".skein"
    skein_dir.mkdir(exist_ok=True)
    return skein_dir / "shards.db"


def _get_db_connection() -> sqlite3.Connection:
    """Get connection to shard database, creating it if needed."""
    db_path = _get_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Initialize schema if needed
    conn.executescript(SHARD_DB_SCHEMA)
    conn.commit()

    return conn


def _record_shard_metadata(
    worktree_name: str,
    base_commit: str,
    created_at: datetime,
    spawned_by: Optional[str] = None,
    brief_id: Optional[str] = None,
    description: Optional[str] = None,
    parent_worktree: Optional[str] = None
) -> None:
    """Record shard metadata in SQLite database."""
    conn = _get_db_connection()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO shards
            (worktree_name, base_commit, created_at, spawned_by, brief_id, description, parent_worktree, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
        """, (worktree_name, base_commit, created_at.isoformat(), spawned_by, brief_id, description, parent_worktree))
        conn.commit()
    finally:
        conn.close()


def _get_shard_metadata(worktree_name: str) -> Optional[Dict[str, Any]]:
    """Get shard metadata from SQLite database."""
    conn = _get_db_connection()
    try:
        cursor = conn.execute(
            "SELECT * FROM shards WHERE worktree_name = ?",
            (worktree_name,)
        )
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None
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
            values.append(kwargs["merged_at"].isoformat() if kwargs["merged_at"] else None)
        if "tendered_at" in kwargs:
            updates.append("tendered_at = ?")
            values.append(kwargs["tendered_at"].isoformat() if kwargs["tendered_at"] else None)
        if "confidence" in kwargs:
            updates.append("confidence = ?")
            values.append(kwargs["confidence"])

        values.append(worktree_name)

        conn.execute(
            f"UPDATE shards SET {', '.join(updates)} WHERE worktree_name = ?",
            values
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
VALID_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$')

# Reserved git names that cannot be used
RESERVED_NAMES = frozenset({
    'head', 'HEAD',
    'master', 'main',
    'refs', 'objects', 'hooks', 'info', 'logs',
    'worktrees',  # Our own directory
})


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
        return False, f"name exceeds {MAX_SHARD_NAME_LENGTH} characters (got {len(name)})"

    # Check for forbidden git patterns
    if '..' in name:
        return False, "name cannot contain consecutive dots (..)"

    if name.endswith('.lock'):
        return False, "name cannot end with .lock"

    if '@{' in name:
        return False, "name cannot contain @{ (git reflog notation)"

    if name.startswith('.'):
        return False, "name cannot start with a dot"

    if name.startswith('-'):
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
        if (project_path / ".git").exists():
            return project_path
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
                # This is a worktree - read the file to find the actual repo
                # Format: gitdir: /path/to/main/repo/.git/worktrees/name
                try:
                    with open(git_path) as f:
                        gitdir_line = f.read().strip()
                    if gitdir_line.startswith('gitdir: '):
                        gitdir_path = gitdir_line[8:]  # Remove 'gitdir: ' prefix
                        # Extract main repo path: /.../repo/.git/worktrees/name -> /.../repo
                        main_repo = Path(gitdir_path).parent.parent.parent
                        if (main_repo / ".git").is_dir():
                            return main_repo
                except:
                    pass
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
    """Get the worktrees directory, resolving project root lazily if needed."""
    global _WORKTREES_DIR
    if _WORKTREES_DIR is None:
        get_project_root()  # This sets _WORKTREES_DIR
    return _WORKTREES_DIR


def set_project_root(path: str) -> None:
    """
    Override the project root for shard operations.

    Use this to operate on a different project than the current working directory.

    Args:
        path: Path to the project root (must be a git repository)
    """
    global _PROJECT_ROOT, _WORKTREES_DIR

    project_path = Path(path).resolve()
    if not (project_path / ".git").exists():
        raise ShardError(f"Not a git repository: {path}")

    _PROJECT_ROOT = project_path
    _WORKTREES_DIR = _PROJECT_ROOT / "worktrees"


def _get_repo() -> 'git.Repo':
    """Get git.Repo instance for project."""
    if git is None:
        raise ShardError("GitPython not installed. Run: pip install GitPython")

    try:
        return git.Repo(get_project_root())
    except git.InvalidGitRepositoryError:
        raise ShardError(f"Not a git repository: {get_project_root()}")


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
            ["git", "--version"],
            capture_output=True,
            text=True,
            check=True
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

    Looks for existing worktrees matching pattern and returns next seq.
    Pattern: worktrees/{name}-{date}-{seq}/

    Args:
        name: Shard name identifier
        date: Date string (YYYYMMDD)

    Returns:
        Next sequence number (e.g., 1, 2, 3...)

    Raises:
        ShardError: If sequence limit (999) would be exceeded
    """
    worktrees_dir = get_worktrees_dir()
    if not worktrees_dir.exists() or not worktrees_dir.is_dir():
        return 1

    pattern_prefix = f"{name}-{date}-"
    existing = [
        d.name for d in worktrees_dir.iterdir()
        if d.is_dir() and d.name.startswith(pattern_prefix)
    ]

    if not existing:
        return 1

    # Extract sequence numbers (filter invalid: negative, zero, or >999)
    sequences = []
    for worktree_name in existing:
        # name format: name-date-seq
        try:
            seq_str = worktree_name.split("-")[-1]
            seq = int(seq_str)
            # Only count valid sequences (1-999)
            if 1 <= seq <= MAX_SEQUENCE_NUMBER:
                sequences.append(seq)
        except (ValueError, IndexError):
            continue

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
    project_root: Optional[str] = None
) -> Dict[str, str]:
    """
    Create SHARD: git branch + worktree for isolated agent work.

    Args:
        name: Descriptive name for this shard (e.g., 'fix-auth-bug', 'add-dark-mode')
        brief_id: Optional brief ID this SHARD relates to
        description: Optional work description
        project_root: Optional path to git repo. If not provided, auto-detects.

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

    worktrees_dir = get_worktrees_dir()
    # Ensure worktrees directory exists
    worktrees_dir.mkdir(exist_ok=True)

    # Generate date and sequence
    date = datetime.now().strftime("%Y%m%d")
    seq = _get_next_sequence(name, date)

    # Generate names
    worktree_name = f"{name}-{date}-{seq:03d}"
    branch_name = f"shard-{name}-{date}-{seq:03d}"
    worktree_path = worktrees_dir / worktree_name

    # Generate shard_id - use worktree_name which includes name+date+seq for uniqueness
    shard_id = f"shard-{worktree_name}"

    # Check if worktree already exists
    if worktree_path.exists():
        raise ShardError(f"Worktree already exists: {worktree_path}")

    # Create git worktree with new branch using GitPython
    try:
        repo = _get_repo()
        # Record base_commit BEFORE creating worktree (current master HEAD)
        base_commit = repo.git.rev_parse("master")
        repo.git.worktree("add", str(worktree_path), "-b", branch_name)
    except Exception as e:
        if git and isinstance(e, git.GitCommandError):
            raise ShardError(f"Failed to create worktree: {e}")
        raise

    created_at = datetime.now()

    # Record metadata in SQLite database for drift detection
    _record_shard_metadata(
        worktree_name=worktree_name,
        base_commit=base_commit,
        created_at=created_at,
        spawned_by=name,  # Use name as spawned_by for now
        brief_id=brief_id,
        description=description
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
        "status": "spawned"
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
            unresolved_inside = (
                path == worktree_path or
                worktree_path in path.parents
            )
        except (ValueError, TypeError):
            unresolved_inside = False

        # Check 2: Does the resolved path point inside worktree?
        # This catches when we're physically inside the worktree
        resolved_path = path.resolve()
        resolved_worktree = worktree_path.resolve()
        resolved_inside = (
            resolved_path == resolved_worktree or
            resolved_worktree in resolved_path.parents
        )

        # Block if EITHER check indicates we're inside
        return unresolved_inside or resolved_inside

    except (OSError, ValueError):
        # FAIL CLOSED: if we can't verify, assume inside to prevent deletion
        return True


def cleanup_shard(
    worktree_name: str,
    keep_branch: bool = False,
    caller_cwd: Optional[str] = None,
    project_root: Optional[str] = None
) -> bool:
    """
    Remove SHARD worktree and optionally delete branch.

    Args:
        worktree_name: Worktree directory name (e.g., 'fix-auth-bug-20251109-001')
            Can also be a full path, which will be normalized to just the name.
        keep_branch: If True, keep git branch after removing worktree
        caller_cwd: Optional path to check for self-deletion. If provided, cleanup will
            be refused if this path is inside the target worktree. This is used to prevent
            agents from deleting their own worktree after cd-ing elsewhere.
        project_root: Optional path to git repo. If not provided, auto-detects.

    Returns:
        True if successful, False otherwise

    Raises:
        ShardError: If cleanup fails
    """
    if project_root:
        set_project_root(project_root)

    worktrees_dir = get_worktrees_dir()

    # Normalize worktree_name: if user passes a full path, extract just the name
    # This prevents Path('/base') / '/full/path' from returning just '/full/path'
    worktree_name = Path(worktree_name).name

    # Validate: worktree_name must not be empty or the worktrees directory itself
    if not worktree_name or worktree_name == worktrees_dir.name:
        raise ShardError(
            f"Invalid worktree name: '{worktree_name}'\n"
            f"Expected a specific worktree name like 'agent-20251109-001'"
        )

    worktree_path = worktrees_dir / worktree_name

    # Safety check: ensure the path is actually inside worktrees_dir
    # (should always be true after normalization, but belt-and-suspenders)
    try:
        worktree_path.resolve().relative_to(worktrees_dir.resolve())
    except ValueError:
        raise ShardError(
            f"Invalid worktree path: {worktree_path}\n"
            f"Worktree must be inside: {worktrees_dir}"
        )

    if not worktree_path.exists():
        raise ShardError(f"Worktree not found: {worktree_path}")

    # Check if caller_cwd is inside the worktree being deleted
    # This prevents agents from deleting their own worktree after cd-ing elsewhere
    if caller_cwd:
        caller_path = Path(caller_cwd)
        if _is_path_inside_worktree(caller_path, worktree_path):
            raise ShardError(
                f"Cannot cleanup: caller_cwd is inside the target worktree.\n"
                f"caller_cwd: {caller_cwd}\n"
                f"This may indicate self-deletion attempt."
            )

    # Also check current working directory for backwards compatibility
    # If so, exit with error to prevent breaking the user's shell
    try:
        current_dir = Path.cwd()
        if _is_path_inside_worktree(current_dir, worktree_path):
            raise ShardError(
                f"Cannot cleanup from inside the worktree.\n"
                f"Your shell is currently in: {current_dir}\n"
                f"Please change directory first: cd {get_project_root()}"
            )
    except OSError:
        # If we can't determine current directory (e.g., it's already deleted),
        # that's fine, continue with cleanup
        pass

    # Extract branch name from worktree
    # Format: agent-id-date-seq → shard-agent-id-date-seq
    branch_name = f"shard-{worktree_name}"

    # Remove worktree using GitPython
    repo = _get_repo()
    try:
        repo.git.worktree("remove", str(worktree_path))
    except Exception as e:
        # Try force removal if regular removal fails
        try:
            repo.git.worktree("remove", "--force", str(worktree_path))
        except Exception as e2:
            raise ShardError(f"Failed to remove worktree: {e2}")

    # Optionally delete branch
    if not keep_branch:
        try:
            repo.git.branch("-D", branch_name)
        except Exception as e:
            # Don't raise error if branch deletion fails (branch might not exist)
            import sys
            print(f"Warning: Could not delete branch {branch_name}: {e}", file=sys.stderr)

    # Prune stale worktree references
    try:
        repo.git.worktree("prune")
    except Exception:
        pass  # Ignore prune errors

    return True


def list_shards(active_only: bool = True) -> List[Dict[str, str]]:
    """
    List SHARD worktrees.

    Args:
        active_only: If True, only return worktrees that currently exist

    Returns:
        List of SHARD info dicts with keys:
        - worktree_name
        - worktree_path
        - branch_name
        - name (the shard name)
        - date (extracted from worktree name)
        - seq (extracted from worktree name)
    """
    # Get all worktrees from git using GitPython
    try:
        repo = _get_repo()
        worktree_output = repo.git.worktree("list", "--porcelain")
    except Exception as e:
        raise ShardError(f"Failed to list worktrees: {e}")

    # Parse git worktree list output (format unchanged)
    shards = []
    current_worktree = {}

    for line in worktree_output.splitlines():
        if line.startswith("worktree "):
            if current_worktree:
                # Process previous worktree
                path = current_worktree.get("worktree")
                if path and "worktrees/" in path:
                    shard_info = _parse_worktree_info(path)
                    if shard_info:
                        shards.append(shard_info)
            current_worktree = {"worktree": line.split(" ", 1)[1]}
        elif line.startswith("branch "):
            current_worktree["branch"] = line.split(" ", 1)[1]

    # Don't forget last worktree
    if current_worktree:
        path = current_worktree.get("worktree")
        if path and "worktrees/" in path:
            shard_info = _parse_worktree_info(path)
            if shard_info:
                shards.append(shard_info)

    return shards


def _parse_worktree_info(worktree_path: str) -> Optional[Dict[str, str]]:
    """
    Parse worktree path into SHARD info.

    Path format: /path/to/worktrees/{name}-{date}-{seq}
    Branch format: shard-{name}-{date}-{seq}

    Args:
        worktree_path: Full path to worktree

    Returns:
        SHARD info dict or None if not a SHARD worktree
    """
    path = Path(worktree_path)
    worktrees_dir = get_worktrees_dir()

    # Check if this is in our worktrees directory
    # Use path containment check rather than fragile string matching
    try:
        if worktrees_dir.exists() and worktrees_dir.is_dir():
            path.relative_to(worktrees_dir)  # Raises ValueError if not contained
        elif "worktrees/" not in str(path):
            return None
    except ValueError:
        # Path is not inside worktrees_dir
        if "worktrees/" not in str(path):
            return None

    worktree_name = path.name

    # Strip any -graft suffixes before parsing the base name
    base_name = worktree_name
    graft_suffix = ""
    while base_name.endswith("-graft"):
        base_name = base_name[:-6]  # Remove "-graft"
        graft_suffix += "-graft"

    # Try to parse base name: {name}-{date}-{seq}
    parts = base_name.rsplit("-", 2)
    if len(parts) < 3:
        return None

    try:
        seq = int(parts[-1])
        date = parts[-2]
        name = "-".join(parts[:-2])
    except (ValueError, IndexError):
        return None

    branch_name = f"shard-{worktree_name}"

    return {
        "worktree_name": worktree_name,
        "worktree_path": str(path),
        "branch_name": branch_name,
        "name": name,
        "date": date,
        "seq": f"{seq:03d}"
    }


def get_shard_status(worktree_name: str) -> Optional[Dict[str, str]]:
    """
    Get status of a specific SHARD.

    Args:
        worktree_name: Worktree directory name (or full path, which will be normalized)

    Returns:
        SHARD info dict or None if not found
    """
    # Normalize: extract just the name if a full path was passed
    worktree_name = Path(worktree_name).name

    shards = list_shards()
    for shard in shards:
        if shard["worktree_name"] == worktree_name:
            return shard
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
        shard_date = datetime.strptime(date_str, "%Y%m%d")
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
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
    - conflicts: Would have merge conflicts with master
    - stale: No commits and older than stale_days

    Args:
        stale_days: Number of days without commits before a shard is considered stale

    Returns:
        Dict with keys: 'ready', 'needs_commit', 'conflicts', 'stale'
        Each contains list of shard dicts with added 'git_info' and 'age_days' fields
    """
    queue = {
        "ready": [],
        "needs_commit": [],
        "conflicts": [],
        "stale": []
    }

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


def get_shard_git_info(worktree_name: str) -> Dict:
    """
    Get git information for a SHARD: commits ahead, working tree status, merge status.

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

    result = {
        "commits_ahead": 0,
        "working_tree": "unknown",
        "merge_status": "unknown",
        "commit_log": [],
        "diffstat": "",
        "uncommitted": []
    }

    try:
        repo = _get_repo()
        branch = shard_info["branch_name"]
        worktree_path = shard_info["worktree_path"]

        # Commits ahead of master
        try:
            count = repo.git.rev_list("--count", f"master..{branch}")
            result["commits_ahead"] = int(count)
        except:
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

        # Merge status - check if branch can merge cleanly into master
        try:
            # Check git version supports three-argument merge-tree (2.38+)
            _check_git_version_for_merge_tree()
            # Find merge base
            merge_base = repo.git.merge_base("master", branch)
            # Use merge-tree with base, master, and branch
            merge_output = repo.git.merge_tree(merge_base, "master", branch)
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

        # Commit log (commits on branch not in master)
        try:
            log_output = repo.git.log("--oneline", f"master..{branch}")
            if log_output.strip():
                for line in log_output.strip().split("\n"):
                    parts = line.split(" ", 1)
                    sha = parts[0]
                    msg = parts[1] if len(parts) > 1 else ""
                    result["commit_log"].append((sha, msg))
        except:
            pass

        # Diffstat (files changed between master and branch)
        try:
            if result["commits_ahead"] > 0:
                diffstat = repo.git.diff("--stat", f"master..{branch}")
                result["diffstat"] = diffstat.strip()
        except:
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

    worktree_path = Path(shard_info["worktree_path"])
    if not worktree_path.exists():
        return None

    metadata = {
        "worktree_name": worktree_name,
        "branch_name": shard_info["branch_name"],
        "name": shard_info["name"],
        "worktree_path": str(worktree_path)
    }

    # Get git statistics if possible
    try:
        repo = _get_repo()

        # Get commit count on this branch (since branching from master)
        try:
            # Count commits on SHARD branch not in master
            commit_count = repo.git.rev_list("--count", f"master..{shard_info['branch_name']}")
            metadata["commits"] = int(commit_count)
        except:
            metadata["commits"] = 0

        # Get list of modified files
        try:
            # Files changed between master and this branch
            changed_files = repo.git.diff("--name-only", "master", shard_info["branch_name"])
            if changed_files:
                metadata["files_modified"] = changed_files.strip().split("\n")
            else:
                metadata["files_modified"] = []
        except:
            metadata["files_modified"] = []

        # Get last commit message
        try:
            last_commit = repo.git.log("-1", "--pretty=%B", shard_info["branch_name"])
            metadata["last_commit_message"] = last_commit.strip()
        except:
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
    - Master activity since base (commits, notable changes)
    - Work diff (agent's actual changes from base)
    - Integration diff (what would merge into current master)
    - Conflict status

    Args:
        worktree_name: Worktree directory name

    Returns:
        Dict with drift information
    """
    shard_info = get_shard_status(worktree_name)
    if not shard_info:
        return {}

    # Get metadata from SQLite
    metadata = _get_shard_metadata(worktree_name)

    result = {
        "worktree_name": worktree_name,
        "branch_name": shard_info["branch_name"],
        "base_commit": None,
        "base_commit_short": None,
        "base_commit_date": None,
        "has_metadata": metadata is not None,
        "master_commits_ahead": 0,
        "master_notable_changes": [],
        "is_stale": False,
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
            except:
                pass

            # Count commits on master since base
            try:
                count = repo.git.rev_list("--count", f"{base_commit}..master")
                result["master_commits_ahead"] = int(count)
                result["is_stale"] = int(count) > 0
            except:
                pass

            # Get notable changes on master since base
            try:
                if result["master_commits_ahead"] > 0:
                    # Get file stats for changes on master
                    name_status = repo.git.diff("--name-status", f"{base_commit}..master")
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
                    result["master_notable_changes"] = notable
            except:
                pass

            # Get work diff stat (agent's actual changes from base)
            try:
                work_stat = repo.git.diff("--stat", f"{base_commit}..{branch}")
                result["work_diff_stat"] = work_stat.strip() if work_stat.strip() else None
            except:
                pass

        # Get integration diff stat (what would merge with current master)
        try:
            integration_stat = repo.git.diff("--stat", f"master...{branch}")
            result["integration_diff_stat"] = integration_stat.strip() if integration_stat.strip() else None
        except:
            pass

        # Test for conflicts using merge-tree
        try:
            # Check git version supports three-argument merge-tree (2.38+)
            _check_git_version_for_merge_tree()
            merge_base = repo.git.merge_base("master", branch)
            merge_output = repo.git.merge_tree(merge_base, "master", branch)

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


def get_shard_work_diff(worktree_name: str, stat_only: bool = False) -> Optional[str]:
    """
    Get the WORK diff: agent's actual changes from base commit.

    This shows only what the agent committed, without any false deletions
    from master evolution.

    Args:
        worktree_name: Worktree directory name
        stat_only: If True, return only --stat output

    Returns:
        Git diff output as string, or None if no changes or no metadata
    """
    shard_info = get_shard_status(worktree_name)
    if not shard_info:
        return None

    metadata = _get_shard_metadata(worktree_name)
    if not metadata or not metadata.get("base_commit"):
        # Fall back to integration diff if no metadata
        return get_shard_diff(worktree_name, stat_only=stat_only)

    try:
        repo = _get_repo()
        branch = shard_info["branch_name"]
        base_commit = metadata["base_commit"]

        # Work diff: base_commit..branch
        if stat_only:
            diff_output = repo.git.diff("--stat", f"{base_commit}..{branch}")
        else:
            diff_output = repo.git.diff(f"{base_commit}..{branch}")

        return diff_output if diff_output.strip() else None

    except Exception as e:
        raise ShardError(f"Failed to get work diff: {e}")


def get_shard_diff(worktree_name: str, stat_only: bool = False, integration: bool = False) -> Optional[str]:
    """
    Get diff between master and shard branch.

    By default returns integration diff (master...branch) which shows what would merge.
    Use get_shard_work_diff() for agent's actual changes from base.

    Args:
        worktree_name: Worktree directory name (e.g., 'opus-security-architect-20251109-001')
        stat_only: If True, return only --stat output
        integration: If True, use three-dot diff (master...branch) for merge preview

    Returns:
        Git diff output as string, or None if no changes or shard not found
    """
    shard_info = get_shard_status(worktree_name)
    if not shard_info:
        return None

    try:
        repo = _get_repo()
        branch = shard_info["branch_name"]

        # Get diff between master and shard branch
        diff_range = f"master...{branch}" if integration else f"master..{branch}"
        if stat_only:
            diff_output = repo.git.diff("--stat", diff_range)
        else:
            diff_output = repo.git.diff(diff_range)
        return diff_output if diff_output.strip() else None

    except Exception as e:
        raise ShardError(f"Failed to get diff: {e}")


def merge_shard(
    worktree_name: str,
    caller_cwd: Optional[str] = None,
    project_root: Optional[str] = None
) -> Dict[str, Any]:
    """
    Merge shard branch into master and cleanup worktree.

    Checks for uncommitted changes and merge conflicts before proceeding.
    If clean: checks out master, merges branch with --no-ff, cleans up worktree and branch.

    Args:
        worktree_name: Worktree directory name (e.g., 'fix-auth-bug-20251109-001')
        caller_cwd: Optional path to check for self-deletion. If provided, merge will
            be refused if this path is inside the target worktree. This is used to prevent
            agents from merging their own worktree after cd-ing elsewhere.
        project_root: Optional path to git repo. If not provided, auto-detects.

    Returns:
        Dict with:
            - success: bool
            - message: str
            - uncommitted: list of uncommitted files (if dirty)
            - conflicts: list of conflicting files (if conflicts)
    """
    if project_root:
        set_project_root(project_root)

    shard_info = get_shard_status(worktree_name)
    if not shard_info:
        raise ShardError(f"SHARD not found: {worktree_name}")

    worktree_path = Path(shard_info["worktree_path"])
    branch_name = shard_info["branch_name"]

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

    # Check for uncommitted changes in the worktree
    git_info = get_shard_git_info(worktree_name)
    if git_info.get("working_tree") == "dirty":
        uncommitted = git_info.get("uncommitted", [])
        return {
            "success": False,
            "message": "Cannot merge: worktree has uncommitted changes",
            "uncommitted": uncommitted,
            "conflicts": []
        }

    # Check for merge conflicts or unknown status (fail safe)
    merge_status = git_info.get("merge_status", "unknown")
    if merge_status != "clean":
        # Get list of conflicting files
        try:
            # Check git version supports three-argument merge-tree (2.38+)
            _check_git_version_for_merge_tree()
            merge_base = repo.git.merge_base("master", branch_name)
            merge_output = repo.git.merge_tree(merge_base, "master", branch_name)
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
                            while j < len(lines) and not lines[j].startswith("changed in both") and not lines[j].startswith("merged"):
                                if "<<<<<<<" in lines[j]:
                                    if filename not in conflict_files:
                                        conflict_files.append(filename)
                                    break
                                j += 1
                i += 1
        except ShardError as e:
            # Git version too old - include error message
            conflict_files = [f"(git version check failed: {e})"]
        except:
            conflict_files = ["(unable to determine conflicting files)"]

        error_msg = (
            "Cannot merge: branch has conflicts with master"
            if merge_status == "conflict"
            else f"Cannot merge: merge status is '{merge_status}' (must be 'clean')"
        )
        return {
            "success": False,
            "message": error_msg,
            "uncommitted": [],
            "conflicts": conflict_files
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
        # Checkout master
        repo.git.checkout("master")

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

        # Cleanup worktree and branch
        try:
            cleanup_shard(worktree_name, keep_branch=False, caller_cwd=caller_cwd)
        except ShardError as cleanup_error:
            return {
                "success": True,
                "message": f"✓ Merged {branch_name} into master\n⚠ Warning: cleanup failed: {cleanup_error}",
                "uncommitted": [],
                "conflicts": []
            }

        return {
            "success": True,
            "message": f"✓ Merged {branch_name} into master and cleaned up worktree",
            "uncommitted": [],
            "conflicts": []
        }

    except ShardError:
        # Re-raise ShardErrors as-is
        raise
    except Exception as e:
        raise ShardError(f"Merge failed: {e}")
    finally:
        # Restore original branch/commit if merge didn't succeed
        # (If merge succeeded, we intentionally stay on master)
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
    Get the root worktree name by following parent_worktree links in SQLite.

    Falls back to name parsing (stripping -graft suffixes) for legacy shards
    without SQLite metadata.
    """
    conn = _get_db_connection()
    try:
        current = worktree_name
        visited = set()  # Prevent infinite loops

        while current and current not in visited:
            visited.add(current)
            cursor = conn.execute(
                "SELECT parent_worktree FROM shards WHERE worktree_name = ?",
                (current,)
            )
            row = cursor.fetchone()

            if row and row["parent_worktree"]:
                current = row["parent_worktree"]
            else:
                # No parent - this is the root (or legacy shard without metadata)
                break

        # If we found a root via SQLite, return it
        if current:
            return current

    finally:
        conn.close()

    # Fallback for legacy shards: strip -graft suffixes
    name = worktree_name
    while name.endswith("-graft"):
        name = name[:-6]  # Remove "-graft"
    return name


def get_graft_chain(worktree_name: str) -> List[str]:
    """
    Get full graft chain for a worktree using SQLite parent relationships.

    Returns list of worktree names from root to current, e.g.:
    ['fix-bug-20260112-001', 'fix-bug-20260112-001-graft', 'fix-bug-20260112-001-graft-graft']

    Uses SQLite parent_worktree column for chain tracking, with fallback to
    name parsing for legacy shards without metadata.
    """
    worktrees_dir = get_worktrees_dir()
    conn = _get_db_connection()

    try:
        # First, find the root by following parent links up
        root = get_graft_chain_root(worktree_name)

        # Now build chain by following children down
        chain = []
        current = root

        while current:
            path = worktrees_dir / current
            if path.exists():
                chain.append(current)

            # Find child (shard with parent_worktree = current)
            cursor = conn.execute(
                "SELECT worktree_name FROM shards WHERE parent_worktree = ?",
                (current,)
            )
            row = cursor.fetchone()

            if row:
                current = row["worktree_name"]
            else:
                # No child in SQLite - try legacy name-based detection
                next_graft = f"{current}-graft"
                if (worktrees_dir / next_graft).exists():
                    current = next_graft
                else:
                    break

        return chain

    finally:
        conn.close()


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


def graft_shard(
    worktree_name: str,
    project_root: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a graft worktree to resolve conflicts.

    Creates a new worktree from current master and cherry-picks commits
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

    # Get metadata for base commit
    source_metadata = _get_shard_metadata(worktree_name)

    worktrees_dir = get_worktrees_dir()
    repo = _get_repo()

    # Generate graft name (append -graft)
    graft_worktree_name = f"{worktree_name}-graft"
    graft_branch_name = f"shard-{graft_worktree_name}"
    graft_worktree_path = worktrees_dir / graft_worktree_name

    # Check if graft already exists
    if graft_worktree_path.exists():
        raise ShardError(
            f"Graft worktree already exists: {graft_worktree_name}\n"
            f"Either clean up the existing graft or continue working in it."
        )

    # Get commits from source shard
    source_branch = shard_info["branch_name"]

    # Find base commit (from metadata or merge-base with master)
    if source_metadata and source_metadata.get("base_commit"):
        base_commit = source_metadata["base_commit"]
    else:
        # Legacy shard without metadata - use merge-base
        base_commit = repo.git.merge_base("master", source_branch)

    # Get list of commits to cherry-pick (in reverse order - oldest first)
    commits_output = repo.git.rev_list("--reverse", f"{base_commit}..{source_branch}")
    commits = commits_output.strip().split("\n") if commits_output.strip() else []

    if not commits:
        raise ShardError(
            f"No commits to graft from {worktree_name}\n"
            f"The shard has no changes relative to its base."
        )

    # Get current master HEAD for new base_commit
    new_base_commit = repo.git.rev_parse("master")

    # Create worktree from current master
    try:
        repo.git.worktree("add", str(graft_worktree_path), "-b", graft_branch_name, "master")
    except Exception as e:
        raise ShardError(f"Failed to create graft worktree: {e}")

    created_at = datetime.now()

    # Record graft metadata
    _record_shard_metadata(
        worktree_name=graft_worktree_name,
        base_commit=new_base_commit,
        created_at=created_at,
        parent_worktree=worktree_name,
        description=f"Graft of {worktree_name} for conflict resolution"
    )

    # Cherry-pick commits
    conflict_files = []
    try:
        if git is None:
            raise ShardError("GitPython not installed")
        graft_repo = git.Repo(str(graft_worktree_path))

        for commit in commits:
            try:
                graft_repo.git.cherry_pick(commit)
            except Exception as e:
                # Cherry-pick failed - likely conflicts
                if "conflict" in str(e).lower() or "CONFLICT" in str(e):
                    # Get list of conflicted files
                    try:
                        status = graft_repo.git.status("--porcelain")
                        for line in status.split("\n"):
                            if line.startswith("UU ") or line.startswith("AA "):
                                conflict_files.append(line[3:])
                    except:
                        pass
                    break
                else:
                    raise ShardError(f"Cherry-pick failed: {e}")

    except ShardError:
        raise
    except Exception as e:
        # If something went wrong, try to clean up
        try:
            repo.git.worktree("remove", "--force", str(graft_worktree_path))
            repo.git.branch("-D", graft_branch_name)
        except:
            pass
        raise ShardError(f"Failed to apply commits: {e}")

    result = {
        "success": len(conflict_files) == 0,
        "graft_worktree_name": graft_worktree_name,
        "graft_worktree_path": str(graft_worktree_path),
        "graft_branch_name": graft_branch_name,
        "source_worktree_name": worktree_name,
        "commits_applied": len(commits),
        "conflicts": conflict_files,
        "chain_depth": get_graft_depth(graft_worktree_name),
    }

    if conflict_files:
        result["message"] = (
            f"Graft created with conflicts in: {', '.join(conflict_files)}\n"
            f"Resolve conflicts in: {graft_worktree_path}"
        )
    else:
        result["message"] = (
            f"Graft created cleanly - ready to merge\n"
            f"Location: {graft_worktree_path}"
        )

    return result


def cleanup_graft_chain(
    worktree_name: str,
    keep_branch: bool = False,
    caller_cwd: Optional[str] = None,
    project_root: Optional[str] = None
) -> Dict[str, Any]:
    """
    Clean up entire graft chain (original + all grafts).

    Args:
        worktree_name: Any worktree in the chain
        keep_branch: Keep branches after removing worktrees
        caller_cwd: Caller's cwd to check for self-deletion
        project_root: Optional project root override

    Returns:
        Dict with:
            - success: bool
            - removed: list of removed worktree names
            - errors: list of error messages
    """
    if project_root:
        set_project_root(project_root)

    root = get_graft_chain_root(worktree_name)
    chain = get_graft_chain(root)

    if not chain:
        raise ShardError(f"No worktrees found in chain for: {worktree_name}")

    removed = []
    errors = []

    # Remove in reverse order (grafts first, then original)
    for wt_name in reversed(chain):
        try:
            cleanup_shard(wt_name, keep_branch=keep_branch, caller_cwd=caller_cwd)
            removed.append(wt_name)
        except ShardError as e:
            errors.append(f"{wt_name}: {e}")

    return {
        "success": len(errors) == 0,
        "removed": removed,
        "errors": errors,
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
    cwd = Path.cwd()

    # Check if we're in a worktree directory
    if "worktrees" not in str(cwd):
        return None

    # Try to find the worktree root (should contain .git file pointing to main repo)
    current = cwd
    worktree_root = None

    while current != current.parent:
        git_file = current / ".git"
        if git_file.exists() and git_file.is_file():
            # This is a worktree (not main repo which has .git directory)
            worktree_root = current
            break
        current = current.parent

    if not worktree_root:
        return None

    # Check if this worktree is in our worktrees directory
    if not str(worktree_root).startswith(str(get_worktrees_dir())):
        return None

    # Get worktree name
    worktree_name = worktree_root.name

    # Get SHARD info for this worktree
    return get_shard_status(worktree_name)


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
