#!/usr/bin/env python3
"""
SHARD Worktree Management

Manages git worktrees for SHARD agent coordination workflow.
Provides core functions for creating, cleaning up, and listing SHARDs.
"""

import os
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import git
except ImportError:
    git = None


class ShardError(Exception):
    """Base exception for SHARD operations."""
    pass


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


def _get_next_sequence(agent_id: str, date: str) -> int:
    """
    Get next sequence number for agent on given date.

    Looks for existing worktrees matching pattern and returns next seq.
    Pattern: worktrees/{agent_id}-{date}-{seq}/

    Args:
        agent_id: Agent identifier
        date: Date string (YYYYMMDD)

    Returns:
        Next sequence number (e.g., 1, 2, 3...)
    """
    worktrees_dir = get_worktrees_dir()
    if not worktrees_dir.exists():
        return 1

    pattern_prefix = f"{agent_id}-{date}-"
    existing = [
        d.name for d in worktrees_dir.iterdir()
        if d.is_dir() and d.name.startswith(pattern_prefix)
    ]

    if not existing:
        return 1

    # Extract sequence numbers
    sequences = []
    for name in existing:
        # name format: agent-id-date-seq
        try:
            seq_str = name.split("-")[-1]
            sequences.append(int(seq_str))
        except (ValueError, IndexError):
            continue

    return max(sequences) + 1 if sequences else 1


def spawn_shard(
    agent_id: str,
    brief_id: Optional[str] = None,
    description: Optional[str] = None
) -> Dict[str, str]:
    """
    Create SHARD: git branch + worktree for isolated agent work.

    Args:
        agent_id: Agent identifier (e.g., 'opus-security-architect')
        brief_id: Optional brief ID this SHARD relates to
        description: Optional work description

    Returns:
        {
            'shard_id': 'shard-20251109-abc123',
            'agent_id': 'opus-security-architect',
            'branch_name': 'shard-opus-security-architect-20251109-001',
            'worktree_path': '/abs/path/to/worktrees/opus-security-architect-20251109-001',
            'brief_id': 'brief-123' or None,
            'created_at': '2025-11-09T12:00:00'
        }

    Raises:
        ShardError: If worktree creation fails
    """
    worktrees_dir = get_worktrees_dir()
    # Ensure worktrees directory exists
    worktrees_dir.mkdir(exist_ok=True)

    # Generate date and sequence
    date = datetime.now().strftime("%Y%m%d")
    seq = _get_next_sequence(agent_id, date)

    # Generate names
    worktree_name = f"{agent_id}-{date}-{seq:03d}"
    branch_name = f"shard-{agent_id}-{date}-{seq:03d}"
    worktree_path = worktrees_dir / worktree_name

    # Generate shard_id (could be more sophisticated, for now use worktree_name)
    # In future, could use random suffix like SKEIN folio IDs
    shard_id = f"shard-{date}-{worktree_name[:8]}"

    # Check if worktree already exists
    if worktree_path.exists():
        raise ShardError(f"Worktree already exists: {worktree_path}")

    # Create git worktree with new branch using GitPython
    try:
        repo = _get_repo()
        repo.git.worktree("add", str(worktree_path), "-b", branch_name)
    except Exception as e:
        if git and isinstance(e, git.GitCommandError):
            raise ShardError(f"Failed to create worktree: {e}")
        raise

    # Return SHARD info
    return {
        "shard_id": shard_id,
        "agent_id": agent_id,
        "branch_name": branch_name,
        "worktree_path": str(worktree_path.absolute()),
        "worktree_name": worktree_name,
        "brief_id": brief_id,
        "description": description,
        "created_at": datetime.now().isoformat(),
        "status": "spawned"
    }


def _is_path_inside_worktree(path: Path, worktree_path: Path) -> bool:
    """
    Check if a path is inside a worktree directory.

    Resolves both paths to handle symlinks and relative paths correctly.

    Args:
        path: Path to check
        worktree_path: Worktree directory path

    Returns:
        True if path is inside or equal to worktree_path
    """
    try:
        resolved_path = path.resolve()
        resolved_worktree = worktree_path.resolve()
        # Check if path equals worktree or is a child of it
        return resolved_path == resolved_worktree or resolved_worktree in resolved_path.parents
    except (OSError, ValueError):
        return False


def cleanup_shard(
    worktree_name: str,
    keep_branch: bool = False,
    caller_cwd: Optional[str] = None
) -> bool:
    """
    Remove SHARD worktree and optionally delete branch.

    Args:
        worktree_name: Worktree directory name (e.g., 'opus-security-architect-20251109-001')
            Can also be a full path, which will be normalized to just the name.
        keep_branch: If True, keep git branch after removing worktree
        caller_cwd: Optional path to check for self-deletion. If provided, cleanup will
            be refused if this path is inside the target worktree. This is used to prevent
            agents from deleting their own worktree after cd-ing elsewhere.

    Returns:
        True if successful, False otherwise

    Raises:
        ShardError: If cleanup fails
    """
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
            print(f"Warning: Could not delete branch {branch_name}: {e}")

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
        - agent_id (extracted from name)
        - date (extracted from name)
        - seq (extracted from name)
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

    Path format: /path/to/worktrees/{agent-id}-{date}-{seq}
    Branch format: shard-{agent-id}-{date}-{seq}

    Args:
        worktree_path: Full path to worktree

    Returns:
        SHARD info dict or None if not a SHARD worktree
    """
    path = Path(worktree_path)
    worktrees_dir = get_worktrees_dir()

    # Check if this is in our worktrees directory
    if not str(path).endswith(tuple(os.listdir(worktrees_dir) if worktrees_dir.exists() else [])):
        # Try simpler check
        if "worktrees/" not in str(path):
            return None

    worktree_name = path.name

    # Try to parse worktree name: {agent-id}-{date}-{seq}
    parts = worktree_name.rsplit("-", 2)
    if len(parts) < 3:
        return None

    try:
        seq = int(parts[-1])
        date = parts[-2]
        agent_id = "-".join(parts[:-2])
    except (ValueError, IndexError):
        return None

    branch_name = f"shard-{worktree_name}"

    return {
        "worktree_name": worktree_name,
        "worktree_path": str(path),
        "branch_name": branch_name,
        "agent_id": agent_id,
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
        return delta.days
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
        try:
            status = repo.git.status("--porcelain", worktree_path)
            result["working_tree"] = "dirty" if status.strip() else "clean"
        except:
            pass

        # Merge status - check if branch can merge cleanly into master
        try:
            # Find merge base
            merge_base = repo.git.merge_base("master", branch)
            # Use merge-tree with base, master, and branch
            merge_output = repo.git.merge_tree(merge_base, "master", branch)
            # If output contains conflict markers, there are conflicts
            if "<<<<<<" in merge_output or "+<<<<<<" in merge_output:
                result["merge_status"] = "conflict"
            else:
                result["merge_status"] = "clean"
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
        try:
            status = repo.git.status("--porcelain", worktree_path)
            if status.strip():
                result["uncommitted"] = status.strip().split("\n")
        except:
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
        "agent_id": shard_info["agent_id"],
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


def get_shard_diff(worktree_name: str) -> Optional[str]:
    """
    Get diff between master and shard branch.

    Args:
        worktree_name: Worktree directory name (e.g., 'opus-security-architect-20251109-001')

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
        diff_output = repo.git.diff(f"master..{branch}")
        return diff_output if diff_output.strip() else None

    except Exception as e:
        raise ShardError(f"Failed to get diff: {e}")


def merge_shard(worktree_name: str, caller_cwd: Optional[str] = None) -> Dict[str, Any]:
    """
    Merge shard branch into master and cleanup worktree.

    Checks for uncommitted changes and merge conflicts before proceeding.
    If clean: checks out master, merges branch with --no-ff, cleans up worktree and branch.

    Args:
        worktree_name: Worktree directory name (e.g., 'opus-security-architect-20251109-001')
        caller_cwd: Optional path to check for self-deletion. If provided, merge will
            be refused if this path is inside the target worktree. This is used to prevent
            agents from merging their own worktree after cd-ing elsewhere.

    Returns:
        Dict with:
            - success: bool
            - message: str
            - uncommitted: list of uncommitted files (if dirty)
            - conflicts: list of conflicting files (if conflicts)
    """
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

    # Check for merge conflicts
    if git_info.get("merge_status") == "conflict":
        # Get list of conflicting files
        try:
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
        except:
            conflict_files = ["(unable to determine conflicting files)"]

        return {
            "success": False,
            "message": "Cannot merge: branch has conflicts with master",
            "uncommitted": [],
            "conflicts": conflict_files
        }

    # All checks passed - perform the merge
    try:
        # Store original branch to restore if needed
        original_branch = repo.active_branch.name

        # Checkout master
        repo.git.checkout("master")

        # Merge with --no-ff to preserve branch history
        try:
            repo.git.merge("--no-ff", branch_name, "-m", f"Merge {branch_name}")
        except Exception as merge_error:
            # If merge fails, abort and restore
            try:
                repo.git.merge("--abort")
            except:
                pass
            repo.git.checkout(original_branch)
            raise ShardError(f"Merge failed: {merge_error}")

        # Cleanup worktree and branch
        try:
            cleanup_shard(worktree_name, keep_branch=False)
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

    except Exception as e:
        raise ShardError(f"Merge failed: {e}")


def detect_shard_environment() -> Optional[Dict[str, str]]:
    """
    Detect if currently running in a SHARD worktree.

    Checks if current working directory is inside a SHARD worktree
    and returns information about it.

    Returns:
        SHARD info dict if in a SHARD, None otherwise
        Dict contains: worktree_name, worktree_path, branch_name, agent_id, date, seq
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
        print("  python shard_worktree.py spawn <agent-id> [brief-id] [description]")
        print("  python shard_worktree.py list")
        print("  python shard_worktree.py cleanup <worktree-name> [--keep-branch]")
        sys.exit(1)

    command = sys.argv[1]

    if command == "spawn":
        if len(sys.argv) < 3:
            print("Error: agent-id required")
            sys.exit(1)

        agent_id = sys.argv[2]
        brief_id = sys.argv[3] if len(sys.argv) > 3 else None
        description = sys.argv[4] if len(sys.argv) > 4 else None

        try:
            shard = spawn_shard(agent_id, brief_id, description)
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
