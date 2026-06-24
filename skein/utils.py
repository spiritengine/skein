"""Utility helpers — the subset of the legacy ``skein.utils`` the new package uses.

Absorbed verbatim from legacy ``skein/utils.py`` (A0, making ``skein``
self-contained so legacy can be deleted at cutover). Only the two functions the
new package imports are carried — ``generate_yield_id`` (chain sack ids) and
``generate_agent_name`` (roster names) — plus the four private helpers
``generate_agent_name`` depends on. The legacy-store helpers (status/assignment
enrichment over a json_store) are deliberately NOT carried: they speak the legacy
storage API, which the content-hash model does not have. ``get_word_pair`` rides
the sibling :mod:`skein.words` (also copied verbatim).

These ids/names are NOT canonical or signed material (sack ids are sidecar; agent
names are slugs), so the bar here is behavioral fidelity, not byte-identity — but
the bodies are carried verbatim regardless.
"""

import json
import random
import string
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Set


def generate_yield_id() -> str:
    """
    Generate yield/sack ID with format: sack-{YYYYMMDD}-{4char}
    Example: sack-20251210-x7m2
    """
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    random_suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"sack-{date_str}-{random_suffix}"


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
        input_data = json.dumps(
            {
                "project": project or "",
                "role": role or "",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "brief_content": brief_content or "",
            }
        )

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

    now = datetime.now(timezone.utc)
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
    random_suffix = "".join(random.choices(string.ascii_lowercase, k=4))
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
    random_suffix = "".join(random.choices(string.ascii_lowercase, k=4))
    return f"{name}-{random_suffix}"
