"""
SKEIN utility functions.
"""

import random
import string
import re
from datetime import datetime
from typing import List, Set, Optional, Dict, Any
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
