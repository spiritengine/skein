"""
SKEIN utility functions.
"""

import random
import string
import re
import subprocess
import json
from datetime import datetime
from pathlib import Path
from typing import List, Set, Optional, Dict, Any, Callable
from functools import lru_cache


def generate_folio_id(folio_type: str) -> str:
    """
    Generate folio ID with format: {type}-{YYYYMMDD}-{4char}
    Example: issue-20251106-a7b3
    """
    date_str = datetime.now().strftime("%Y%m%d")
    random_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{folio_type}-{date_str}-{random_suffix}"


def generate_thread_id() -> str:
    """
    Generate thread ID with format: thread-{YYYYMMDD}-{4char}
    Example: thread-20251107-p8q2
    """
    date_str = datetime.now().strftime("%Y%m%d")
    random_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"thread-{date_str}-{random_suffix}"


def parse_mentions(content: str) -> Set[str]:
    """
    Parse @mentions from content.

    Recognizes patterns like:
    - @agent-id (agent mentions)
    - @issue-123 (issue mentions)
    - @brief-456 (brief mentions)
    - @notion-789 (notion mentions)
    - @finding-abc (finding mentions)
    - @plan-xyz (plan mentions)
    - @summary-123 (summary mentions)
    - @friction-456 (friction mentions)

    Returns:
        Set of unique resource IDs mentioned
    """
    if not content:
        return set()

    # Pattern matches @word-word-... allowing alphanumeric and hyphens
    # Case-insensitive matching
    pattern = r'@([a-z0-9][a-z0-9\-]+)'
    matches = re.findall(pattern, content.lower())

    # Filter to valid resource ID patterns (must have at least one hyphen)
    valid_mentions = set()
    for match in matches:
        if '-' in match:
            valid_mentions.add(match)

    return valid_mentions


# Pure Threads: In-memory cache for status/assignment lookups
_status_cache: Dict[str, Optional[str]] = {}
_assignment_cache: Dict[str, Optional[str]] = {}


def get_current_status(folio_id: str, json_store) -> Optional[str]:
    """
    Get current status of a folio from status threads.

    Status is determined by the most recent 'status' thread pointing to this folio.
    Thread content should be the status value (e.g., "open", "closed", "in_progress").

    Args:
        folio_id: The folio ID to get status for
        json_store: JSONStore instance to query threads

    Returns:
        Status string or None if no status threads found
    """
    # Check cache first
    if folio_id in _status_cache:
        return _status_cache[folio_id]

    # Get all status threads pointing to this folio
    status_threads = json_store.get_threads(to_id=folio_id, type="status")

    if not status_threads:
        _status_cache[folio_id] = None
        return None

    # Get the most recent status thread
    status_threads.sort(key=lambda t: t.created_at, reverse=True)
    latest_status = status_threads[0].content

    # Cache and return
    _status_cache[folio_id] = latest_status
    return latest_status


def get_current_assignment(folio_id: str, json_store) -> Optional[str]:
    """
    Get current assignment of a folio from assignment threads.

    Assignment is determined by the most recent 'assignment' thread pointing to an agent.
    The to_id of the assignment thread is the assigned agent.

    Args:
        folio_id: The folio ID to get assignment for
        json_store: JSONStore instance to query threads

    Returns:
        Agent ID or None if no assignment threads found
    """
    # Check cache first
    if folio_id in _assignment_cache:
        return _assignment_cache[folio_id]

    # Get all assignment threads originating from this folio
    assignment_threads = json_store.get_threads(from_id=folio_id, type="assignment")

    if not assignment_threads:
        _assignment_cache[folio_id] = None
        return None

    # Get the most recent assignment thread
    assignment_threads.sort(key=lambda t: t.created_at, reverse=True)
    latest_assignment = assignment_threads[0].to_id

    # Cache and return
    _assignment_cache[folio_id] = latest_assignment
    return latest_assignment


def invalidate_status_cache(folio_id: str):
    """Invalidate status cache for a folio when a new status thread is created."""
    if folio_id in _status_cache:
        del _status_cache[folio_id]


def invalidate_assignment_cache(folio_id: str):
    """Invalidate assignment cache for a folio when a new assignment thread is created."""
    if folio_id in _assignment_cache:
        del _assignment_cache[folio_id]


def auto_invalidate_cache(thread_type: str, folio_id: str):
    """
    Automatically invalidate the appropriate cache based on thread type.

    Call this after saving a thread to ensure cache consistency.

    Args:
        thread_type: The type of thread being created ('status', 'assignment', etc.)
        folio_id: The folio ID to invalidate cache for
                  - For status threads: the to_id (folio being statused)
                  - For assignment threads: the from_id (folio being assigned)
    """
    if thread_type == "status":
        invalidate_status_cache(folio_id)
    elif thread_type == "assignment":
        invalidate_assignment_cache(folio_id)


def parse_relative_time(time_str: str) -> datetime:
    """
    Parse relative time strings like '1day', '2hours', '30min' to datetime.

    Supports:
    - '1day', '2days' -> X days ago
    - '1hour', '2hours' -> X hours ago
    - '30min', '45minutes' -> X minutes ago
    - ISO format strings (passthrough)

    Returns:
        datetime object representing the time in the past (timezone-aware UTC)

    Raises:
        ValueError: If time string format is invalid
    """
    from datetime import timedelta, timezone

    time_str = time_str.strip().lower()

    # Try ISO format first
    try:
        dt = datetime.fromisoformat(time_str)
        # Ensure timezone-aware (assume UTC if naive)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass

    # Parse relative time
    match = re.match(r'^(\d+)(day|hour|min|minute)s?$', time_str)
    if not match:
        raise ValueError(f"Invalid time format: '{time_str}'. Use '1day', '2hours', '30min', or ISO format")

    amount = int(match.group(1))
    unit = match.group(2)

    if unit == 'day':
        delta = timedelta(days=amount)
    elif unit == 'hour':
        delta = timedelta(hours=amount)
    elif unit in ('min', 'minute'):
        delta = timedelta(minutes=amount)
    else:
        raise ValueError(f"Unknown time unit: '{unit}'")

    # Return timezone-aware datetime in UTC
    return datetime.now(timezone.utc) - delta


# Agent Name Generation

def generate_agent_name(
    existing_names: Optional[Set[str]] = None,
    project: Optional[str] = None,
    role: Optional[str] = None,
    config_path: Optional[Path] = None,
    brief_content: Optional[str] = None,
) -> str:
    """
    Generate a memorable agent name.

    Default format: adjective-noun-MMDD (e.g., "chrome-badger-1129")

    Supports pluggable generators via config. Config checked in order:
    1. config_path parameter
    2. .skein/config.json in current directory
    3. ~/.skein/config.json

    Config format:
        {
            "naming": {
                "generator": null           // Use default
                // or: "~/.skein/namer.py"  // Custom script
            }
        }

    Custom generator protocol:
    - Receives JSON on stdin: {"project": "...", "role": "...", "timestamp": "...", "brief_content": "..."}
    - Outputs name on stdout
    - Exit 0 = use name, non-zero = fall back to default

    Args:
        existing_names: Set of names to avoid (for collision handling)
        project: Project context (passed to custom generator)
        role: Role context (passed to custom generator)
        config_path: Explicit config path to check
        brief_content: Brief/task content for context-aware naming

    Returns:
        Generated agent name (e.g., "chrome-badger-1129")
    """
    existing = existing_names or set()

    # Try to load custom generator from config
    custom_generator = _load_custom_generator(config_path)

    if custom_generator:
        name = _run_custom_generator(custom_generator, project, role, brief_content)
        if name:
            # Check for collision and handle
            return _ensure_unique(name, existing)

    # Default generator: adjective-noun-MMDD
    return _generate_default_name(existing)


def _load_custom_generator(config_path: Optional[Path] = None) -> Optional[str]:
    """
    Load custom generator path from config.

    Checks in order:
    1. Explicit config_path
    2. .skein/config.json (project local)
    3. ~/.skein/config.json (user global)

    Returns:
        Path to custom generator script, or None for default
    """
    config_locations = []

    if config_path:
        config_locations.append(config_path)

    # Project-local config
    project_config = Path.cwd() / ".skein" / "config.json"
    if project_config.exists():
        config_locations.append(project_config)

    # User global config
    global_config = Path.home() / ".skein" / "config.json"
    if global_config.exists():
        config_locations.append(global_config)

    for config_file in config_locations:
        try:
            with open(config_file) as f:
                config = json.load(f)
                generator = config.get("naming", {}).get("generator")
                if generator:
                    # Expand ~ to home directory
                    return str(Path(generator).expanduser())
        except (json.JSONDecodeError, IOError):
            continue

    return None


def _run_custom_generator(
    generator_path: str,
    project: Optional[str] = None,
    role: Optional[str] = None,
    brief_content: Optional[str] = None,
) -> Optional[str]:
    """
    Run custom generator script.

    Protocol:
    - Receives JSON on stdin: {"project": "...", "role": "...", "timestamp": "...", "brief_content": "..."}
    - Outputs name on stdout (stripped)
    - Exit 0 = use name, non-zero = fall back to default

    Returns:
        Generated name, or None if generator fails
    """
    try:
        input_data = json.dumps({
            "project": project or "",
            "role": role or "",
            "timestamp": datetime.now().isoformat(),
            "brief_content": brief_content or "",
        })

        result = subprocess.run(
            [generator_path],
            input=input_data,
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()

    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError):
        pass

    return None


def _generate_default_name(existing: Set[str]) -> str:
    """
    Generate default adjective-noun-MMDD name.

    Format: adjective-noun-MMDD (e.g., "chrome-badger-1129")

    Handles collisions by appending incrementing suffix.
    """
    from .words import get_word_pair

    now = datetime.now()
    time_suffix = now.strftime("%m%d")

    # Try up to 10 times to find unique name
    for attempt in range(10):
        adj, noun = get_word_pair()
        base_name = f"{adj}-{noun}-{time_suffix}"

        if attempt == 0:
            name = base_name
        else:
            name = f"{base_name}-{attempt}"

        if name not in existing:
            return name

    # Fallback: add random suffix
    random_suffix = ''.join(random.choices(string.ascii_lowercase, k=4))
    return f"{adj}-{noun}-{time_suffix}-{random_suffix}"


def _ensure_unique(name: str, existing: Set[str]) -> str:
    """
    Ensure name is unique by appending suffix if needed.

    Args:
        name: Base name to check
        existing: Set of existing names to avoid

    Returns:
        Unique name (original or with suffix)
    """
    if name not in existing:
        return name

    # Try incrementing suffix
    for i in range(1, 100):
        candidate = f"{name}-{i}"
        if candidate not in existing:
            return candidate

    # Fallback: random suffix
    random_suffix = ''.join(random.choices(string.ascii_lowercase, k=4))
    return f"{name}-{random_suffix}"
