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
from typing import Dict, List, Optional

try:
    import git
except ImportError:
    git = None


# Project root detection
# Note: We need to find the git repo root for the project the user is currently
# working in (based on current working directory), not where this shard.py file lives.
def _find_project_root() -> Path:
    """Find the git project root for the current working directory."""
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
        "Not in a git repository. Run 'skein shard' commands from within a git repo."
    )

PROJECT_ROOT = _find_project_root()
WORKTREES_DIR = PROJECT_ROOT / "worktrees"


class ShardError(Exception):
    """Base exception for SHARD operations."""
    pass


def _get_repo() -> 'git.Repo':
    """Get git.Repo instance for project."""
    if git is None:
        raise ShardError("GitPython not installed. Run: pip install GitPython")

    try:
        return git.Repo(PROJECT_ROOT)
    except git.InvalidGitRepositoryError:
        raise ShardError(f"Not a git repository: {PROJECT_ROOT}")


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
    if not WORKTREES_DIR.exists():
        return 1

    pattern_prefix = f"{agent_id}-{date}-"
    existing = [
        d.name for d in WORKTREES_DIR.iterdir()
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
    # Ensure worktrees directory exists
    WORKTREES_DIR.mkdir(exist_ok=True)

    # Generate date and sequence
    date = datetime.now().strftime("%Y%m%d")
    seq = _get_next_sequence(agent_id, date)

    # Generate names
    worktree_name = f"{agent_id}-{date}-{seq:03d}"
    branch_name = f"shard-{agent_id}-{date}-{seq:03d}"
    worktree_path = WORKTREES_DIR / worktree_name

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


def cleanup_shard(
    worktree_name: str,
    keep_branch: bool = False
) -> bool:
    """
    Remove SHARD worktree and optionally delete branch.

    Args:
        worktree_name: Worktree directory name (e.g., 'opus-security-architect-20251109-001')
        keep_branch: If True, keep git branch after removing worktree

    Returns:
        True if successful, False otherwise

    Raises:
        ShardError: If cleanup fails
    """
    worktree_path = WORKTREES_DIR / worktree_name

    if not worktree_path.exists():
        raise ShardError(f"Worktree not found: {worktree_path}")

    # Check if current working directory is inside the worktree being deleted
    # If so, exit with error to prevent breaking the user's shell
    try:
        current_dir = Path.cwd()
        # Check if current directory is inside the worktree path
        if worktree_path in current_dir.parents or current_dir == worktree_path:
            raise ShardError(
                f"Cannot cleanup from inside the worktree.\n"
                f"Your shell is currently in: {current_dir}\n"
                f"Please change directory first: cd {PROJECT_ROOT}"
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

    # Check if this is in our worktrees directory
    if not str(path).endswith(tuple(os.listdir(WORKTREES_DIR) if WORKTREES_DIR.exists() else [])):
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
        worktree_name: Worktree directory name

    Returns:
        SHARD info dict or None if not found
    """
    shards = list_shards()
    for shard in shards:
        if shard["worktree_name"] == worktree_name:
            return shard
    return None


def get_shard_git_info(worktree_name: str) -> Dict:
    """
    Get git information for a SHARD: commits ahead, working tree status, merge status.

    Returns dict with:
        - commits_ahead: int
        - working_tree: 'clean' or 'dirty'
        - merge_status: 'clean', 'conflict', or 'unknown'
        - commit_log: list of (sha, message) tuples
    """
    shard_info = get_shard_status(worktree_name)
    if not shard_info:
        return {}

    result = {
        "commits_ahead": 0,
        "working_tree": "unknown",
        "merge_status": "unknown",
        "commit_log": []
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

    # Check if this worktree is in our WORKTREES_DIR
    if not str(worktree_root).startswith(str(WORKTREES_DIR)):
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
