#!/usr/bin/env python3
"""
SKEIN CLI - Command-line interface for SKEIN collaboration system.

Usage:
    export SKEIN_AGENT_ID=agent-007
    skein log stream-name "Error message" --level ERROR
    skein brief create site-id "Handoff content" --title "Brief Title"
    skein brief brief-20251106-x9k2
"""

import os
import sys
import re
import json
import click
import requests
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, Set

# Import name generator from skein package
try:
    from skein.utils import generate_agent_name
except ImportError:
    # Fallback if skein package not installed
    generate_agent_name = None

from skein.address import parse as parse_address


def parse_post_site_id(site_id_arg: str) -> tuple:
    """Parse a post site_id arg, supporting 'project:site' colon syntax.

    Returns (site_id, project_override). Bare ids return (id, None).
    """
    parsed = parse_address(site_id_arg)
    if parsed.is_qualified:
        return parsed.folio_id, parsed.project
    return parsed.folio_id, None


def find_project_root() -> Optional[Path]:
    """
    Walk up directory tree to find .skein/ directory (like git).
    Returns project root path or None if not found.
    """
    current = Path.cwd()
    while current != current.parent:
        skein_dir = current / ".skein"
        if skein_dir.exists() and skein_dir.is_dir():
            return current
        current = current.parent
    return None


def get_project_config() -> Optional[Dict[str, Any]]:
    """Get project config from .skein/config.json if in a project."""
    project_root = find_project_root()
    if not project_root:
        return None

    config_file = project_root / ".skein" / "config.json"
    if not config_file.exists():
        return None

    try:
        with open(config_file) as f:
            return json.load(f)
    except Exception:
        return None


def get_global_config() -> Dict[str, Any]:
    """Get global SKEIN config from ~/.skein/config.json."""
    config_file = Path.home() / ".skein" / "config.json"
    if not config_file.exists():
        return {"server_url": "http://localhost:8001"}

    try:
        with open(config_file) as f:
            return json.load(f)
    except Exception:
        return {"server_url": "http://localhost:8001"}


def get_agent_id(
    ctx_agent: Optional[str] = None, base_url: Optional[str] = None
) -> Optional[str]:
    """
    Get agent ID from sources in priority order:
    1. --agent flag (explicit override)
    2. SKEIN_AGENT_ID env var
    3. None (no agent specified)

    Returns None if no agent is specified, allowing callers to distinguish
    between "no agent provided" and an agent explicitly named something.
    """
    if ctx_agent:
        return ctx_agent
    return os.getenv("SKEIN_AGENT_ID")


def get_base_url(ctx_url: Optional[str] = None) -> str:
    """
    Get SKEIN base URL in priority order:
    1. --url flag
    2. SKEIN_URL env var
    3. Project config (.skein/config.json)
    4. Global config (~/.skein/config.json)
    5. Default localhost:8001
    """
    if ctx_url:
        return ctx_url.rstrip("/")

    # Check environment variable
    env_url = os.getenv("SKEIN_URL")
    if env_url:
        return env_url.rstrip("/")

    # Check project config
    project_config = get_project_config()
    if project_config and project_config.get("server_url"):
        return project_config["server_url"].rstrip("/")

    # Check global config
    global_config = get_global_config()
    if global_config.get("server_url"):
        return global_config["server_url"].rstrip("/")

    return "http://localhost:8001"


def validate_positional_args(*args, command_name: str):
    """
    Validate positional arguments to detect common syntax mistakes.
    Raises ClickException with helpful error if name=value pattern detected.
    """
    for arg in args:
        if isinstance(arg, str) and "=" in arg and not arg.startswith("-"):
            # Check if it looks like name=value syntax
            parts = arg.split("=", 1)
            if len(parts) == 2 and parts[0].isidentifier():
                param_name = parts[0]
                raise click.ClickException(
                    f"Incorrect syntax: '{arg}'\n\n"
                    f"It looks like you're using '{param_name}=\"...\"' syntax.\n"
                    f"The SKEIN CLI uses positional arguments, not named parameters.\n\n"
                    f'Correct syntax: skein {command_name} SITE_ID "description"\n'
                    f"See: skein {command_name} --help"
                )


def make_request(method: str, endpoint: str, base_url: str, agent_id: str, **kwargs):
    """Make HTTP request to SKEIN API.

    Accepts optional `project_id` kwarg to override the resolved project for this
    call (e.g. from colon-syntax like 'speakbot:my-site' on a post command).
    """
    url = f"{base_url}/skein{endpoint}"
    headers = kwargs.pop("headers", {})
    project_id = kwargs.pop("project_id", None)

    if agent_id is not None:
        headers["X-Agent-Id"] = agent_id

    # Resolve project: explicit kwarg > SKEIN_PROJECT env > cwd .skein/ config.
    # The top-level --project flag is pushed into SKEIN_PROJECT by the cli group.
    if not project_id:
        project_id = os.environ.get("SKEIN_PROJECT")
    if not project_id:
        project_config = get_project_config()
        if project_config:
            project_id = project_config.get("project_id")
    if project_id:
        headers["X-Project-Id"] = project_id

    # Warn if agent is still orienting when posting folios
    if method == "POST" and endpoint == "/folios" and agent_id is not None:
        try:
            roster_url = f"{base_url}/skein/roster/{agent_id}"
            roster_resp = requests.get(roster_url, headers=headers)
            if roster_resp.ok:
                agent_data = roster_resp.json()
                if agent_data.get("status") == "orienting":
                    click.echo(
                        f"Note: You're still orienting. Run 'skein --agent {agent_id} ready' when done.",
                        err=True,
                    )
        except Exception:
            pass  # Not critical

    try:
        resp = requests.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.text else {}
    except requests.exceptions.RequestException as e:
        if hasattr(e, "response") and e.response is not None:
            try:
                error = e.response.json()
                raise click.ClickException(f"API error: {error.get('detail', str(e))}")
            except Exception:
                raise click.ClickException(f"API error: {e.response.text or str(e)}")
        raise click.ClickException(f"Connection error: {str(e)}")


# Breadcrumb hints — one-line footers pointing at cross-project layer.
FIND_BREADCRUMB = (
    "(searched current project only — `skein find PATTERN --all` to search all projects)"
)
FOLIO_NOT_FOUND_BREADCRUMB = (
    "(not found in current project — try `skein folio --all ID` or `skein folio PROJECT:ID`)"
)
ACTIVITY_BREADCRUMB = (
    "(current project only — `skein activity --all` to include all projects)"
)


def _load_projects_registry() -> Dict[str, Any]:
    """Load registered projects from ~/.skein/projects.json. Returns {} if missing."""
    projects_file = Path.home() / ".skein" / "projects.json"
    if not projects_file.exists():
        return {}
    try:
        with open(projects_file) as f:
            data = json.load(f)
        return data.get("projects", {})
    except (json.JSONDecodeError, OSError):
        return {}


def _current_project_id() -> Optional[str]:
    """Return the project_id for the current working directory, if any."""
    project_id = os.environ.get("SKEIN_PROJECT")
    if project_id:
        return project_id
    cfg = get_project_config()
    if cfg:
        return cfg.get("project_id")
    return None


def _query_project(
    project_id: str,
    method: str,
    endpoint: str,
    base_url: str,
    agent_id: Optional[str],
    params: Optional[Dict[str, Any]] = None,
) -> Optional[Any]:
    """
    Query a specific project's data by overriding X-Project-Id.

    Returns parsed JSON on success, None on failure (skipped).
    Failures are logged to stderr as a single warning line.
    """
    url = f"{base_url}/skein{endpoint}"
    headers: Dict[str, str] = {"X-Project-Id": project_id}
    if agent_id is not None:
        headers["X-Agent-Id"] = agent_id
    try:
        resp = requests.request(method, url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json() if resp.text else {}
    except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
        click.echo(
            f"warning: skipping project '{project_id}' ({e.__class__.__name__})",
            err=True,
        )
        return None


def sites_breadcrumb(current_project_id: Optional[str]) -> str:
    """Build the sites breadcrumb showing count of other registered projects."""
    registry = _load_projects_registry()
    other = sum(1 for pid in registry if pid != current_project_id)
    return (
        f"({other} other project(s) registered — "
        f"`skein projects` to list, `skein sites --all` to include them)"
    )


def make_title_from_content(content: str, max_length: int = 100) -> str:
    """
    Generate a clean title from content.

    Strips markdown, normalizes whitespace, and truncates to max_length.
    The API also validates, but this gives better UX by fixing client-side.
    """
    title = content

    # Take first line/sentence as title candidate
    title = title.split("\n")[0].strip()

    # Strip markdown cruft
    title = re.sub(r"^#+\s*", "", title)  # Leading headers
    title = re.sub(r"\*\*(.+?)\*\*", r"\1", title)  # Bold **text**
    title = re.sub(r"__(.+?)__", r"\1", title)  # Bold __text__
    title = re.sub(r"\*(.+?)\*", r"\1", title)  # Italic *text*
    title = re.sub(r"_([^_]+)_", r"\1", title)  # Italic _text_
    title = re.sub(r"`([^`]+)`", r"\1", title)  # Code `text`
    title = re.sub(r"~~(.+?)~~", r"\1", title)  # Strikethrough
    title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", title)  # Links

    # Normalize whitespace
    title = " ".join(title.split())

    # Truncate with ellipsis if needed
    if len(title) > max_length:
        title = title[: max_length - 3].rstrip() + "..."

    return title


@click.group(
    epilog=(
        "\b\n"
        "Cross-project usage:\n"
        "  By default, SKEIN detects the project from the cwd's .skein/.\n"
        "  To operate on another project, use --project or SKEIN_PROJECT:\n"
        "\n"
        "\b\n"
        "    skein --project speakbot folio brief-20260101-abc1\n"
        "    SKEIN_PROJECT=speakbot skein post finding skein-dev '...'\n"
        "\n"
        "  Read commands and `skein post` also accept project:id syntax:\n"
        "\n"
        "\b\n"
        "    skein folio speakbot:brief-20260101-abc1\n"
        "    skein post finding speakbot:skein-dev '...'\n"
        "\n"
        "\b\n"
        "  Precedence:\n"
        "    project:id colon syntax > --project flag > SKEIN_PROJECT > cwd"
    )
)
@click.option(
    "--agent", envvar="SKEIN_AGENT_ID", help="Agent ID (or set SKEIN_AGENT_ID)"
)
@click.option(
    "--url", envvar="SKEIN_URL", help="SKEIN server URL (default: localhost:8001)"
)
@click.option(
    "--project",
    envvar="SKEIN_PROJECT",
    help="Project to operate on (overrides cwd .skein/ discovery; or set SKEIN_PROJECT)",
)
@click.pass_context
def cli(ctx, agent, url, project):
    """SKEIN CLI - Agent collaboration system.

    Getting started: skein info quickstart
    Full guide: skein info guide
    """
    ctx.ensure_object(dict)
    ctx.obj["agent"] = agent
    ctx.obj["url"] = url
    ctx.obj["project"] = project

    # Push --project to env so every make_request picks it up via the env path.
    if project:
        os.environ["SKEIN_PROJECT"] = project


# ============================================================================
# Project Management Commands
# ============================================================================


@cli.command()
@click.option("--project", required=True, help="Project ID (e.g., 'myproject')")
@click.option("--name", help="Project display name")
def init(project, name):
    """
    Initialize SKEIN in current directory (like git init).

    Creates .skein/ directory with config and data.
    Registers project in ~/.skein/projects.json.
    """
    project_root = Path.cwd()
    skein_dir = project_root / ".skein"

    # Check if already initialized
    if skein_dir.exists():
        raise click.ClickException(f"SKEIN already initialized in {project_root}")

    # Create .skein directory structure
    skein_dir.mkdir()
    data_dir = skein_dir / "data"
    data_dir.mkdir()
    (data_dir / "sites").mkdir()
    (data_dir / "roster").mkdir()
    (data_dir / "threads").mkdir()
    (data_dir / "screenshots").mkdir()

    # Create project config
    project_config = {
        "project_id": project,
        "name": name or project,
        "created_at": datetime.now().isoformat(),
        "server_url": "http://localhost:8001",
    }

    config_file = skein_dir / "config.json"
    with open(config_file, "w") as f:
        json.dump(project_config, f, indent=2)

    # Register in global projects.json
    global_dir = Path.home() / ".skein"
    global_dir.mkdir(exist_ok=True)

    projects_file = global_dir / "projects.json"
    if projects_file.exists():
        with open(projects_file) as f:
            projects_data = json.load(f)
    else:
        projects_data = {"projects": {}}

    projects_data["projects"][project] = {
        "path": str(project_root),
        "data_dir": str(data_dir),
        "name": name or project,
        "registered_at": datetime.now().isoformat(),
    }

    with open(projects_file, "w") as f:
        json.dump(projects_data, f, indent=2)

    click.echo(f"✓ Initialized SKEIN project '{project}' in {project_root}")
    click.echo("✓ Created .skein/ directory")
    click.echo("✓ Registered in ~/.skein/projects.json")
    click.echo(f"\nProject data: {data_dir}")
    click.echo(f"Server URL: {project_config['server_url']}")


@cli.group()
def setup():
    """Setup commands for SKEIN integration."""
    pass


@setup.command("claude")
def setup_claude():
    """
    Append SKEIN agent instructions to CLAUDE.md.

    Adds the SKEIN template to your project's CLAUDE.md file,
    creating it if it doesn't exist.

    Example:
        skein setup claude
    """
    # Find the template in the package
    import skein

    package_dir = Path(skein.__file__).parent
    template_path = package_dir / "templates" / "CLAUDE.md"

    if not template_path.exists():
        # Fallback: try relative to this file (project root)
        template_path = (
            Path(__file__).parent.parent / "skein" / "templates" / "CLAUDE.md"
        )

    if not template_path.exists():
        raise click.ClickException(f"Template not found at {template_path}")

    # Read template
    with open(template_path) as f:
        template_content = f.read()

    # Target file in current directory
    target_path = Path.cwd() / "CLAUDE.md"

    # Append or create
    if target_path.exists():
        with open(target_path, "a") as f:
            f.write("\n\n")
            f.write(template_content)
        click.echo(f"Appended SKEIN instructions to {target_path}")
    else:
        with open(target_path, "w") as f:
            f.write(template_content)
        click.echo(f"Created {target_path} with SKEIN instructions")


@cli.command()
@click.option("--verbose", "-v", is_flag=True, help="Show detailed information")
def projects(verbose):
    """
    List all registered SKEIN projects.

    Shows all projects registered in ~/.skein/projects.json.
    Use -v for detailed information including paths and registration dates.

    Examples:
        skein projects
        skein projects -v
    """
    global_dir = Path.home() / ".skein"
    projects_file = global_dir / "projects.json"

    if not projects_file.exists():
        click.echo("No projects registered yet.")
        click.echo("\nTo initialize a project, run:")
        click.echo("  skein init --project PROJECT_NAME")
        return

    with open(projects_file) as f:
        projects_data = json.load(f)

    all_projects = projects_data.get("projects", {})

    if not all_projects:
        click.echo("No projects registered yet.")
        return

    # Determine current project if we're in one
    current_project_id = None
    try:
        current_config = get_project_config()
        if current_config:
            current_project_id = current_config.get("project_id")
    except Exception:
        pass

    click.echo(f"Found {len(all_projects)} project(s):\n")

    for project_id, project_info in sorted(all_projects.items()):
        # Check if this is the current project
        marker = " *" if project_id == current_project_id else ""

        # Check if project path still exists
        path = project_info.get("path", "")
        exists = Path(path).exists() if path else False
        status = "✓" if exists else "✗"

        click.echo(f"  {status} {project_id}{marker}")

        if verbose:
            name = project_info.get("name", project_id)
            registered = project_info.get("registered_at", "unknown")

            click.echo(f"      Name: {name}")
            click.echo(f"      Path: {path}")
            click.echo(f"      Registered: {registered}")
            click.echo()
        else:
            click.echo(f"      {path}")

    if not verbose and current_project_id:
        click.echo("\n  * = current project")

    if not verbose:
        click.echo("\nUse 'skein projects -v' for detailed information")


# ============================================================================
# Health Check
# ============================================================================


@cli.command()
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def health(ctx, output_json):
    """
    Check SKEIN system health.

    Verifies:
    - Git repository exists
    - SKEIN project initialized (.skein/ directory)
    - SKEIN server is responding

    Exit codes:
    - 0: All checks pass
    - 1: One or more checks failed
    """
    import subprocess

    checks = {}

    # Check git repo
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"], capture_output=True, timeout=5
        )
        checks["git"] = result.returncode == 0
    except Exception:
        checks["git"] = False

    # Check .skein/ directory
    project_root = find_project_root()
    if project_root:
        skein_dir = project_root / ".skein"
        checks["initialized"] = skein_dir.exists()
    else:
        checks["initialized"] = False

    # Check server
    base_url = get_base_url(ctx.obj.get("url"))
    try:
        import urllib.request

        health_url = base_url.replace("/skein", "") + "/health"
        with urllib.request.urlopen(health_url, timeout=5) as response:
            data = json.loads(response.read().decode())
            checks["server"] = data.get("status") == "healthy"
    except Exception:
        checks["server"] = False

    all_ok = all(checks.values())

    if output_json:
        click.echo(json.dumps({"healthy": all_ok, "checks": checks}))
    else:
        click.echo(f"{'✓' if checks['git'] else '✗'} Git repository")
        click.echo(f"{'✓' if checks['initialized'] else '✗'} SKEIN initialized")
        click.echo(f"{'✓' if checks['server'] else '✗'} SKEIN server responding")
        click.echo(f"\nSKEIN is {'healthy' if all_ok else 'unhealthy'}")

    raise SystemExit(0 if all_ok else 1)


# ============================================================================
# Logging Commands
# ============================================================================


@cli.command()
@click.argument("stream_id")
@click.argument("messages", nargs=-1, required=True)
@click.option("--level", default="INFO", help="Log level (INFO, ERROR, DEBUG, WARN)")
@click.pass_context
def log(ctx, stream_id, messages, level):
    """Stream log lines to SKEIN."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    lines = [{"level": level, "message": msg, "metadata": {}} for msg in messages]

    data = {"stream_id": stream_id, "source": agent_id, "lines": lines}

    result = make_request("POST", "/logs", base_url, agent_id, json=data)
    click.echo(f"Logged {result.get('count', len(lines))} line(s) to {stream_id}")


@cli.command()
@click.argument("stream_id", required=False)
@click.option("--level", help="Filter by level (ERROR, WARN, INFO, DEBUG)")
@click.option("--since", help="Time filter (1hour, 2days, or ISO timestamp)")
@click.option("--search", help="Full-text search in messages")
@click.option("--tail", type=int, help="Show last N lines")
@click.option("--list", "list_streams", is_flag=True, help="List all log streams")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def logs(ctx, stream_id, level, since, search, tail, list_streams, output_json):
    """Retrieve logs from SKEIN."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    if list_streams:
        result = make_request("GET", "/logs/streams", base_url, agent_id)
        if output_json:
            click.echo(json.dumps(result, indent=2))
        else:
            streams = result.get("streams", [])
            if not streams:
                click.echo("No log streams found")
            else:
                click.echo(f"Found {len(streams)} stream(s):\n")
                for s in streams:
                    click.echo(f"  {s['stream_id']}")
                    click.echo(f"    Lines: {s['line_count']}")
                    click.echo(f"    Last: {s['last_log']}")
                    click.echo()
        return

    if not stream_id:
        raise click.ClickException("stream_id required (or use --list)")

    params = {}
    if level:
        params["level"] = level
    if since:
        params["since"] = since
    if search:
        params["search"] = search
    if tail:
        params["limit"] = tail

    log_lines = make_request(
        "GET", f"/logs/{stream_id}", base_url, agent_id, params=params
    )

    if output_json:
        click.echo(json.dumps(log_lines, indent=2))
    else:
        if not log_lines:
            click.echo(f"No logs found in {stream_id}")
        else:
            for line in log_lines[:50]:  # Limit display to 50
                timestamp = line.get("timestamp", "")[:19]
                level_str = line.get("level", "INFO")
                message = line.get("message", "")
                click.echo(f"[{timestamp}] {level_str}: {message}")

            if len(log_lines) > 50:
                click.echo(
                    f"\n... and {len(log_lines) - 50} more lines (use --json to see all)"
                )


@cli.command("log")
@click.option("-n", "--max-count", "--limit", type=int, help="Limit to N entries")
@click.option("--since", "--after", help="Show folios after date (1day, 2hours, ISO)")
@click.option("--until", "--before", help="Show folios before date")
@click.option("--agent", help="Filter by agent ID")
@click.option("--site", "site_filter", help="Filter by site")
@click.option("--type", "type_filter", help="Filter by folio type")
@click.option("--grep", help="Search in content")
@click.option("--oneline", is_flag=True, help="Compact single-line format")
@click.option("--follow", help="Follow thread connections from folio ID")
@click.option("--no-pager", is_flag=True, help="Disable pager")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def log_cmd(
    ctx,
    max_count,
    since,
    until,
    agent,
    site_filter,
    type_filter,
    grep,
    oneline,
    follow,
    no_pager,
    output_json,
):
    """Show folio history (git-style log)."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    # Detect TTY for smart defaults
    is_tty = sys.stdout.isatty()

    # Build params for API
    params = {}
    if site_filter:
        params["site_id"] = site_filter
    if type_filter:
        params["type"] = type_filter

    # Fetch folios
    folios_list = make_request(
        "GET", "/folios", base_url, agent_id, params=params if params else None
    )

    # Fetch all threads for thread count
    try:
        all_threads = make_request("GET", "/threads", base_url, agent_id)
        threads_by_folio = {}
        for thread in all_threads:
            for fid in [thread["from_id"], thread["to_id"]]:
                if fid not in threads_by_folio:
                    threads_by_folio[fid] = []
                threads_by_folio[fid].append(thread)
    except Exception:
        threads_by_folio = {}

    # Filter by agent
    if agent:
        folios_list = [
            f for f in folios_list if agent.lower() in f.get("created_by", "").lower()
        ]

    # Filter by grep
    if grep:
        folios_list = [
            f for f in folios_list if grep.lower() in f.get("content", "").lower()
        ]

    # Filter by since/until
    if since or until:
        from datetime import datetime, timedelta
        import re

        def parse_time_filter(time_str):
            # Try relative format (1day, 2hours, etc.)
            match = re.match(r"^(\d+)(hour|day|week|minute)s?$", time_str)
            if match:
                num = int(match.group(1))
                unit = match.group(2)
                delta = {
                    "minute": timedelta(minutes=num),
                    "hour": timedelta(hours=num),
                    "day": timedelta(days=num),
                    "week": timedelta(weeks=num),
                }.get(unit, timedelta(days=num))
                return datetime.now() - delta
            # Try ISO format
            try:
                return datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            except Exception:
                return None

        if since:
            since_dt = parse_time_filter(since)
            if since_dt:
                folios_list = [
                    f
                    for f in folios_list
                    if datetime.fromisoformat(f["created_at"].replace("Z", "+00:00"))
                    >= since_dt
                ]

        if until:
            until_dt = parse_time_filter(until)
            if until_dt:
                folios_list = [
                    f
                    for f in folios_list
                    if datetime.fromisoformat(f["created_at"].replace("Z", "+00:00"))
                    <= until_dt
                ]

    # Follow thread connections
    if follow:
        # Find all folios connected via threads
        connected = set([follow])
        to_check = [follow]
        while to_check:
            current = to_check.pop()
            for thread in threads_by_folio.get(current, []):
                for fid in [thread["from_id"], thread["to_id"]]:
                    if fid not in connected:
                        connected.add(fid)
                        to_check.append(fid)
        folios_list = [f for f in folios_list if f.get("folio_id") in connected]

    # Sort by date (newest first)
    folios_list.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    # Apply limit (default 20 for non-TTY/agents)
    total_count = len(folios_list)
    if max_count:
        folios_list = folios_list[:max_count]
    elif not is_tty:
        # Agent default: limit to 20
        folios_list = folios_list[:20]

    if output_json:
        click.echo(json.dumps(folios_list, indent=2))
        return

    if not folios_list:
        click.echo("No folios found")
        return

    # Build output lines
    output_lines = []
    for f in folios_list:
        folio_id = f.get("folio_id", "unknown")
        folio_type = f.get("type", "folio")
        site = f.get("site") or f.get("site_id") or ""
        agent_name = f.get("created_by", "unknown")
        created_at = f.get("created_at", "")[:19].replace("T", " ")
        content = f.get("content", "")
        status = f.get("status", "open").upper()

        # Get first line of content, truncated
        first_line = content.split("\n")[0][:60]
        if len(content.split("\n")[0]) > 60:
            first_line += "..."

        # Thread count
        thread_count = len(threads_by_folio.get(folio_id, []))

        # Colors (like git: yellow for id only)
        yellow = "\033[33m"
        reset = "\033[0m"

        if oneline:
            site_str = f" ({site})" if site else ""
            output_lines.append(
                f"{yellow}{folio_type}-{folio_id.split('-', 1)[-1]}{reset}{site_str} {agent_name} {first_line}"
            )
        else:
            site_str = f" ({site})" if site else ""
            output_lines.append(
                f"{yellow}folio {folio_type}-{folio_id.split('-', 1)[-1]}{site_str}{reset}"
            )
            output_lines.append(f"Agent: {agent_name}")
            output_lines.append(f"Date:  {created_at}")
            output_lines.append("")
            output_lines.append(f"    {first_line}")
            if thread_count > 0:
                output_lines.append("")
                output_lines.append(f"    {status} +{thread_count}")
            output_lines.append("")

    # Add footer for agents if truncated
    if not is_tty and len(folios_list) < total_count:
        output_lines.append(
            f"(Showing {len(folios_list)} of {total_count} folios. Use -n to see more)"
        )

    # Output with pager for TTY, plain for agents
    output_text = "\n".join(output_lines)
    if is_tty and not no_pager:
        import subprocess

        try:
            proc = subprocess.Popen(["less", "-R"], stdin=subprocess.PIPE)
            proc.communicate(input=output_text.encode())
        except Exception:
            # Fallback if less not available
            click.echo(output_text)
    else:
        click.echo(output_text)


# ============================================================================
# Sites Commands
# ============================================================================


@cli.group()
def site():
    """Manage SKEIN sites (workspaces)."""
    pass


@site.command("create")
@click.argument("site_id")
@click.argument("purpose")
@click.option("--tags", help="Comma-separated tags")
@click.pass_context
def site_create(ctx, site_id, purpose, tags):
    """Create a new site."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    tag_list = [t.strip() for t in tags.split(",")] if tags else []

    data = {"site_id": site_id, "purpose": purpose, "metadata": {"tags": tag_list}}

    make_request("POST", "/sites", base_url, agent_id, json=data)
    click.echo(f"Created site: {site_id}")


@site.command("get")
@click.argument("site_id")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def site_get(ctx, site_id, output_json):
    """Get site details."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    site_data = make_request("GET", f"/sites/{site_id}", base_url, agent_id)

    if output_json:
        click.echo(json.dumps(site_data, indent=2))
    else:
        click.echo(f"Site: {site_data['site_id']}")
        click.echo(f"Purpose: {site_data['purpose']}")
        click.echo(f"Status: {site_data.get('status', 'active')}")
        click.echo(f"Created: {site_data['created_at']}")
        click.echo(f"By: {site_data['created_by']}")
        if site_data.get("metadata", {}).get("tags"):
            click.echo(f"Tags: {', '.join(site_data['metadata']['tags'])}")
        if site_data.get("metadata", {}).get("closure_note"):
            click.echo(f"Closure note: {site_data['metadata']['closure_note']}")


@site.command("close")
@click.argument("site_id")
@click.option("--note", help="Reason for closing the site")
@click.pass_context
def site_close(ctx, site_id, note):
    """Close a site."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    data = {"status": "closed"}
    if note:
        data["metadata"] = {"closure_note": note}

    make_request("PATCH", f"/sites/{site_id}", base_url, agent_id, json=data)
    click.echo(f"Closed site: {site_id}")
    if note:
        click.echo(f"Note: {note}")


@site.command("reopen")
@click.argument("site_id")
@click.pass_context
def site_reopen(ctx, site_id):
    """Reopen a closed site."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    data = {"status": "active"}

    make_request("PATCH", f"/sites/{site_id}", base_url, agent_id, json=data)
    click.echo(f"Reopened site: {site_id}")


@cli.command()
@click.option("--tag", help="Filter by tag")
@click.option(
    "--all",
    "all_projects",
    is_flag=True,
    help="List sites across all registered projects",
)
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def sites(ctx, tag, all_projects, output_json):
    """List all sites."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    params = {}
    if tag:
        params["tag"] = tag

    if all_projects:
        registry = _load_projects_registry()
        if not registry:
            click.echo("No projects registered.")
            return

        per_project: Dict[str, list] = {}
        total = 0
        for project_id in sorted(registry.keys()):
            data = _query_project(
                project_id, "GET", "/sites", base_url, agent_id, params=params
            )
            if data is None:
                continue
            per_project[project_id] = data
            total += len(data)

        if output_json:
            click.echo(json.dumps(per_project, indent=2, default=str))
            return

        if total == 0:
            click.echo("No sites found in any project")
            return

        click.echo(
            f"Found {total} site(s) across {len(per_project)} project(s):\n"
        )
        for project_id in sorted(per_project.keys()):
            project_sites = per_project[project_id]
            if not project_sites:
                continue
            click.echo(f"{project_id} ({len(project_sites)} site(s)):")
            for s in project_sites:
                status_indicator = (
                    "" if s.get("status", "active") == "active" else f" [{s['status']}]"
                )
                click.echo(f"  {s['site_id']}{status_indicator}")
                click.echo(f"    {s.get('purpose', '')}")
            click.echo()
        return

    sites_list = make_request("GET", "/sites", base_url, agent_id, params=params)

    if output_json:
        click.echo(json.dumps(sites_list, indent=2))
        return

    if not sites_list:
        click.echo("No sites found")
    else:
        click.echo(f"Found {len(sites_list)} site(s):\n")
        for s in sites_list:
            status_indicator = (
                "" if s.get("status", "active") == "active" else f" [{s['status']}]"
            )
            click.echo(f"  {s['site_id']}{status_indicator}")
            click.echo(f"    {s['purpose']}")
            click.echo()

    click.echo(sites_breadcrumb(_current_project_id()))


# ============================================================================
# Issues Commands
# ============================================================================


@cli.command(hidden=True)
@click.argument("site_id")
@click.argument("title")
@click.option("--content", help="Issue description")
@click.option("--assign", help="Assign to agent")
@click.pass_context
def issue(ctx, site_id, title, content, assign):
    """File an issue (deprecated: use 'skein post issue')."""
    ctx.invoke(post_issue, site_id=site_id, title=title, content=content, assign=assign)


@cli.command()
@click.argument("site_id", required=False)
@click.option("--assigned-to", help="Filter by assignee")
@click.option("--status", default="open", help="Filter by status")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def issues(ctx, site_id, assigned_to, status, output_json):
    """List issues."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    params = {"type": "issue"}
    if site_id:
        params["site_id"] = site_id
    if assigned_to:
        if assigned_to == "me":
            assigned_to = agent_id
        params["assigned_to"] = assigned_to
    if status:
        params["status"] = status

    issues_list = make_request("GET", "/folios", base_url, agent_id, params=params)

    if output_json:
        click.echo(json.dumps(issues_list, indent=2))
    else:
        if not issues_list:
            click.echo("No issues found")
        else:
            click.echo(f"Found {len(issues_list)} issue(s):\n")

            # OPTIMIZATION: Batch fetch all threads once (1 API call vs N*2)
            try:
                all_threads = make_request("GET", "/threads", base_url, agent_id)
                # Build lookup dict: resource_id -> [threads]
                threads_by_resource = {}
                for thread in all_threads:
                    # Index by both from_id and to_id
                    if thread["from_id"] not in threads_by_resource:
                        threads_by_resource[thread["from_id"]] = []
                    threads_by_resource[thread["from_id"]].append(thread)

                    if thread["to_id"] not in threads_by_resource:
                        threads_by_resource[thread["to_id"]] = []
                    threads_by_resource[thread["to_id"]].append(thread)
            except Exception:
                # Fall back to no threads if batch fetch fails
                threads_by_resource = {}

            for i in issues_list:
                click.echo(f"  {i['folio_id']}")
                click.echo(f"    {i['title']}")

                # Get threads from batch-fetched data
                try:
                    resource_threads = threads_by_resource.get(i["folio_id"], [])

                    # Dedupe threads (same thread appears in from_id and to_id indexes)
                    thread_ids = set()
                    unique_threads = []
                    for t in resource_threads:
                        if t["thread_id"] not in thread_ids:
                            thread_ids.add(t["thread_id"])
                            unique_threads.append(t)

                    # Extract tags (self-referential threads with type tag)
                    tags = [
                        t["content"]
                        for t in unique_threads
                        if t["type"] == "tag" and t["from_id"] == t["to_id"]
                    ]

                    # Build breadcrumb
                    breadcrumb_parts = []
                    breadcrumb_parts.append(f"Site: {i['site_id']}")
                    breadcrumb_parts.append(f"Status: {i['status']}")
                    if len(unique_threads) > 0:
                        breadcrumb_parts.append(f"{len(unique_threads)} threads")
                    if tags:
                        breadcrumb_parts.append(f"Tags: {', '.join(tags)}")

                    click.echo(f"    {' | '.join(breadcrumb_parts)}")
                except Exception:
                    # Fall back to simple display if thread processing fails
                    click.echo(f"    Site: {i['site_id']} | Status: {i['status']}")

                if i.get("assigned_to"):
                    click.echo(f"    Assigned: {i['assigned_to']}")
                click.echo()


# ============================================================================
# Briefs (Handoffs) Commands
# ============================================================================


@cli.group()
def brief():
    """Manage handoff briefs."""
    pass


@brief.command("create", hidden=True)
@click.argument("site_id")
@click.argument("content")
@click.option("--title", required=True, help="Brief title (required)")
@click.option("--target", help="Target agent")
@click.pass_context
def brief_create(ctx, site_id, content, title, target):
    """Create a handoff brief (deprecated: use 'skein post brief')."""
    ctx.invoke(post_brief, site_id=site_id, content=content, title=title, target=target)


@brief.command("get")
@click.argument("brief_id")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def brief_get(ctx, brief_id, output_json):
    """Retrieve a handoff brief."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    brief_data = make_request("GET", f"/folios/{brief_id}", base_url, agent_id)

    if output_json:
        click.echo(json.dumps(brief_data, indent=2))
    else:
        click.echo(f"\nBrief: {brief_data['folio_id']}")
        click.echo(f"Site: {brief_data['site_id']}")
        click.echo(f"Created: {brief_data['created_at']}")
        click.echo(f"From: {brief_data['created_by']}")
        if brief_data.get("target_agent"):
            click.echo(f"Target: {brief_data['target_agent']}")
        click.echo(f"\nTitle: {brief_data['title']}")
        click.echo("\nContent:")
        click.echo(brief_data["content"])

        if brief_data.get("references"):
            click.echo(f"\nReferences: {', '.join(brief_data['references'])}")


# Allow `skein brief <id>` as shortcut for `skein brief get <id>`
@cli.command(hidden=True)
@click.argument("brief_id")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def brief_shortcut(ctx, brief_id, output_json):
    """Retrieve a brief (shortcut)."""
    ctx.invoke(brief_get, brief_id=brief_id, output_json=output_json)


# ============================================================================
# PLAYBOOK COMMANDS
# ============================================================================


@cli.group()
def playbook():
    """Manage playbooks."""
    pass


@playbook.command("create")
@click.argument("site_id")
@click.argument("content")
@click.option("--title", help="Playbook title")
@click.pass_context
def playbook_create(ctx, site_id, content, title):
    """Create a playbook.

    Site ID accepts colon syntax for cross-project posting:
        skein playbook create speakbot:skein-dev "..." --title "..."
    """
    validate_positional_args(site_id, content, command_name="playbook create")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    site_id, project_override = parse_post_site_id(site_id)

    data = {
        "type": "playbook",
        "site_id": site_id,
        "title": title or "Playbook",
        "content": content,
        "metadata": {},
    }

    result = make_request(
        "POST", "/folios", base_url, agent_id, json=data, project_id=project_override
    )
    playbook_id = result["folio_id"]

    click.echo(f"Created playbook: {playbook_id}")


@playbook.command("get")
@click.argument("playbook_id")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def playbook_get(ctx, playbook_id, output_json):
    """Retrieve a playbook."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    playbook_data = make_request("GET", f"/folios/{playbook_id}", base_url, agent_id)

    if output_json:
        click.echo(json.dumps(playbook_data, indent=2))
    else:
        click.echo(f"\nPlaybook: {playbook_data['folio_id']}")
        click.echo(f"Site: {playbook_data['site_id']}")
        click.echo(f"Created: {playbook_data['created_at']}")
        click.echo(f"From: {playbook_data['created_by']}")
        click.echo(f"\nTitle: {playbook_data['title']}")
        click.echo("\nContent:")
        click.echo(playbook_data["content"])


@cli.command()
@click.argument("brief_id")
@click.pass_context
def ignite(ctx, brief_id):
    """Ignite work from a handoff brief.

    This command:
    1. Retrieves the brief
    2. Auto-registers with suggested successor name (if provided)
    3. Creates succession thread to predecessor
    4. Shows threaded issues/findings
    5. Guides you on next steps
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    if agent_id is None:
        raise click.ClickException(
            "Must set SKEIN_AGENT_ID or use --agent flag to ignite work"
        )

    # Get the brief
    brief_data = make_request("GET", f"/folios/{brief_id}", base_url, agent_id)

    if brief_data.get("type") != "brief":
        raise click.ClickException(f"Resource {brief_id} is not a brief")

    predecessor = brief_data.get("created_by")
    site_id = brief_data.get("site_id")

    # Create succession thread
    succession_data = {
        "from_id": agent_id,
        "to_id": predecessor,
        "type": "succession",
        "content": f"Resuming work from {brief_id}",
    }
    make_request("POST", "/threads", base_url, agent_id, json=succession_data)

    # Display brief
    click.echo(f"{'=' * 60}")
    click.echo(f"RESUMING: {brief_id}")
    click.echo(f"Predecessor: {predecessor}")
    click.echo(f"Site: {site_id}")
    click.echo(f"{'=' * 60}\n")
    click.echo(brief_data.get("content", ""))
    click.echo(f"\n{'=' * 60}")

    # Show threaded issues
    threads_data = make_request(
        "GET", "/threads", base_url, agent_id, params={"from_id": brief_id}
    )

    if threads_data:
        click.echo(f"\nThreaded work ({len(threads_data)} item(s)):")
        for t in threads_data:
            click.echo(f"  [{t['type'].upper()}] -> {t['to_id']}")

    click.echo(f"\n{'=' * 60}")
    click.echo("⚠️  BEFORE STARTING - Read Required Docs:")
    click.echo("  See CLAUDE.md for required reading list.")
    click.echo("  Common docs: PROJECT_CONTEXT.md, ARCHITECTURE.md, PRINCIPLES.md")
    click.echo("  Previous agents who skipped this produced incorrect work.")
    click.echo(f"\n{'=' * 60}")
    click.echo("Next steps:")
    click.echo("  1. Read required docs listed in CLAUDE.md")
    click.echo("  2. Review the brief above")
    click.echo(f"  3. Check site: skein --agent {agent_id} issues {site_id}")
    click.echo(f"  4. Check recent activity: skein --agent {agent_id} activity")
    click.echo("  5. Continue work from 'Remaining' section")
    click.echo(f"{'=' * 60}")


@cli.command(hidden=True)
@click.argument("brief_id")
@click.pass_context
def resume(ctx, brief_id):
    """Deprecated: Use 'ignite' instead."""
    ctx.invoke(ignite, brief_id=brief_id)


# ============================================================================
# Search & Discovery Commands
# ============================================================================


@cli.command()
@click.argument("pattern", required=False, default="")
@click.option(
    "--site",
    "-s",
    multiple=True,
    help="Site pattern(s) to search - supports wildcards (e.g., 'opus-*')",
)
@click.option(
    "--type",
    "-t",
    help="Filter by folio type (issue, brief, friction, finding, summary, notion)",
)
@click.option("--status", help="Filter by status (open, closed, investigating)")
@click.option("--assigned", help="Filter by assignee")
@click.option(
    "--since", help="Only items after this time (e.g., '1hour', '2days', ISO timestamp)"
)
@click.option("--sort", help="Sort by: created (default), created_asc, relevance")
@click.option("--limit", type=int, default=50, help="Max results (default: 50)")
@click.option("--archived", "show_archived", is_flag=True, help="Include archived folios")
@click.option(
    "--all",
    "all_projects",
    is_flag=True,
    help="Search across all registered projects",
)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def find(
    ctx,
    pattern,
    site,
    type,
    status,
    assigned,
    since,
    sort,
    limit,
    show_archived,
    all_projects,
    output_json,
):
    """
    Find folios across SKEIN - unified search and discovery.

    PATTERN is an optional text search. If omitted, lists all matching folios.

    Examples:
        skein find                          # All open folios
        skein find --site my-site           # Folios in specific site
        skein find --site "opus-*"          # Folios matching site pattern
        skein find "authentication"         # Search for text
        skein find "bug" --type issue       # Search issues for "bug"
        skein find --type brief --status open   # Open briefs
        skein find -s "opus-*" -s "test-*"  # Multiple site patterns
        skein find --since 1day             # Recent folios
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    # Build API params
    params = {"resources": "folios"}

    if pattern:
        params["q"] = pattern

    # Handle site patterns
    if site:
        if len(site) == 1 and "*" not in site[0]:
            # Single exact site
            params["site"] = site[0]
        else:
            # Multiple sites or patterns
            params["sites"] = list(site)

    if type:
        params["type"] = type

    if status:
        params["status"] = status

    if assigned:
        params["assigned_to"] = assigned

    if since:
        params["since"] = since

    if sort:
        params["sort"] = sort

    if limit:
        params["limit"] = limit

    if show_archived:
        params["archived"] = True

    if all_projects:
        registry = _load_projects_registry()
        if not registry:
            click.echo("No projects registered.")
            return

        per_project: Dict[str, list] = {}
        grand_total = 0
        for project_id in sorted(registry.keys()):
            response = _query_project(
                project_id, "GET", "/search", base_url, agent_id, params=params
            )
            if response is None:
                continue
            folios_data = response.get("results", {}).get("folios", {})
            items = folios_data.get("items", [])
            if items:
                per_project[project_id] = items
                grand_total += folios_data.get("total", len(items))

        if output_json:
            click.echo(
                json.dumps(
                    {
                        "all_projects": True,
                        "results_by_project": per_project,
                        "total": grand_total,
                    },
                    indent=2,
                    default=str,
                )
            )
            return

        if grand_total == 0:
            if pattern:
                click.echo(f"No folios found matching '{pattern}' in any project")
            else:
                click.echo("No folios found in any project")
            return

        if pattern:
            click.echo(
                f"Found {grand_total} folio(s) matching '{pattern}' across "
                f"{len(per_project)} project(s):\n"
            )
        else:
            click.echo(
                f"Found {grand_total} folio(s) across {len(per_project)} project(s):\n"
            )

        for project_id in sorted(per_project.keys()):
            project_folios = per_project[project_id]
            click.echo(f"{'=' * 60}")
            click.echo(f"Project: {project_id} ({len(project_folios)} folio(s))")
            click.echo(f"{'=' * 60}")
            _print_find_folios_grouped(project_folios)
            click.echo()
        return

    response = make_request("GET", "/search", base_url, agent_id, params=params)

    if output_json:
        click.echo(json.dumps(response, indent=2))
        return

    # Human-readable output
    results_data = response.get("results", {})
    folios_data = results_data.get("folios", {})
    folios = folios_data.get("items", [])
    total = folios_data.get("total", 0)

    if total == 0:
        if pattern:
            click.echo(f"No folios found matching '{pattern}'")
        else:
            click.echo("No folios found")
        if site:
            click.echo(f"  (searched sites: {', '.join(site)})")
        click.echo(FIND_BREADCRUMB)
        return

    # Group by site for display
    by_site = {}
    for f in folios:
        site_id = f.get("site_id", "unknown")
        if site_id not in by_site:
            by_site[site_id] = []
        by_site[site_id].append(f)

    # Header
    if pattern:
        click.echo(f"Found {total} folio(s) matching '{pattern}':\n")
    else:
        click.echo(f"Found {total} folio(s):\n")

    # Display grouped by site
    for site_id in sorted(by_site.keys()):
        site_folios = by_site[site_id]
        click.echo(f"{'=' * 60}")
        click.echo(f"Site: {site_id} ({len(site_folios)} folio(s))")
        click.echo(f"{'=' * 60}")
        _print_find_folios_by_type(site_folios)
        click.echo()

    # Summary
    if len(folios) < total:
        click.echo(f"Showing {len(folios)} of {total} folios (use --limit to see more)")

    exec_time = response.get("execution_time_ms", 0)
    if exec_time:
        click.echo(f"(Search completed in {exec_time}ms)")

    click.echo(FIND_BREADCRUMB)


def _format_folio_date(created_at: str) -> str:
    if not created_at:
        return ""
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return created_at[:10] if len(created_at) >= 10 else created_at


def _print_find_folios_by_type(folios):
    by_type: Dict[str, list] = {}
    for f in folios:
        by_type.setdefault(f["type"], []).append(f)

    for folio_type in sorted(by_type.keys()):
        click.echo(f"\n  {folio_type.upper()} ({len(by_type[folio_type])} item(s)):")
        for f in by_type[folio_type]:
            status_str = f"[{f.get('status', 'open')}]"
            date_str = _format_folio_date(f.get("created_at", ""))
            click.echo(f"    {f['folio_id']} {status_str} {date_str}")
            title = f.get("title", "No title")
            click.echo(f"      {title[:80]}{'...' if len(title) > 80 else ''}")
            content = f.get("content", "")
            if content:
                preview = " ".join(content.split())[:100]
                if len(content) > 100:
                    preview += "..."
                click.echo(f"      {preview}")


def _print_find_folios_grouped(folios):
    """Print folios grouped by site, then by type. Used for --all output."""
    by_site: Dict[str, list] = {}
    for f in folios:
        by_site.setdefault(f.get("site_id", "unknown"), []).append(f)
    for site_id in sorted(by_site.keys()):
        click.echo(f"\n  Site: {site_id}")
        _print_find_folios_by_type(by_site[site_id])


@cli.command(hidden=True)
@click.argument("query")
@click.option(
    "--resources",
    help="Resource types to search (comma-separated: folios, threads, agents, sites). Default: folios",
)
@click.option("--type", help="Filter by type (issue, brief, summary, etc.)")
@click.option("--site", help="Filter by specific site (exact match)")
@click.option(
    "--sites",
    multiple=True,
    help="Filter by site pattern(s) - supports wildcards (can be used multiple times)",
)
@click.option(
    "--all-sites",
    is_flag=True,
    help="Search across all sites (default if no --site/--sites specified)",
)
@click.option("--status", help="Filter by status (open, closed)")
@click.option("--sort", help="Sort by: created (default), created_asc, relevance")
@click.option(
    "--limit", type=int, help="Limit results per resource type (default: 50, max: 500)"
)
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def search(
    ctx,
    query,
    resources,
    type,
    site,
    sites,
    all_sites,
    status,
    sort,
    limit,
    output_json,
):
    """
    Search for work across SKEIN. (Deprecated: use 'find PATTERN')

    By default, searches folios across all sites in the current project.
    Use --resources to search other resource types.

    Examples:
        skein search "authentication bug"
        skein search "token" --type issue
        skein search "refactor" --site my-site
        skein search "security" --status open
        skein search "planning" --sites "opus-*"
        skein search "test" --sites "opus-*" --sites "test-*"
        skein search "bug" --resources folios,threads
        skein search "security" --resources agents --capabilities testing
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    params = {"q": query}

    if resources:
        params["resources"] = resources

    if type:
        params["type"] = type

    if site:
        params["site"] = site
    elif sites:
        # Pass multiple site patterns to API
        for s in sites:
            if "sites" not in params:
                params["sites"] = []
            params["sites"].append(s)

    if status:
        params["status"] = status

    if sort:
        params["sort"] = sort

    if limit:
        params["limit"] = limit

    response = make_request("GET", "/search", base_url, agent_id, params=params)

    if output_json:
        click.echo(json.dumps(response, indent=2))
    else:
        total = response.get("total", 0)
        results_data = response.get("results", {})

        if total == 0:
            click.echo(f"No results found for '{query}'")
            if site:
                click.echo(f"  (searched in site: {site})")
            elif sites:
                click.echo(f"  (searched in sites matching: {', '.join(sites)})")
            else:
                click.echo("  (searched across all sites)")
        else:
            click.echo(f"Found {total} result(s):\n")

            # Display folios grouped by site
            if "folios" in results_data:
                folios_data = results_data["folios"]
                folios = folios_data.get("items", [])
                folios_total = folios_data.get("total", 0)

                if folios:
                    # Group by site for better readability
                    by_site = {}
                    for r in folios:
                        site_id = r.get("site_id", "unknown")
                        if site_id not in by_site:
                            by_site[site_id] = []
                        by_site[site_id].append(r)

                    len(by_site)
                    click.echo(
                        f"📑 Folios ({folios_total} total, showing {len(folios)}):\n"
                    )

                    for site_id in sorted(by_site.keys()):
                        site_results = by_site[site_id]
                        click.echo(f"  📁 {site_id} ({len(site_results)} result(s)):")

                        for r in site_results[:10]:  # Limit per site
                            status_icon = "✓" if r.get("status") == "closed" else "○"
                            click.echo(
                                f"    {status_icon} {r['type'].upper()}: {r.get('title', 'No title')[:60]}"
                            )
                            click.echo(f"       ID: {r['folio_id']}")

                        if len(site_results) > 10:
                            click.echo(
                                f"       ... and {len(site_results) - 10} more in this site"
                            )

                        click.echo()

            # Display threads
            if "threads" in results_data:
                threads_data = results_data["threads"]
                threads = threads_data.get("items", [])
                threads_total = threads_data.get("total", 0)

                if threads:
                    click.echo(
                        f"🧵 Threads ({threads_total} total, showing {len(threads)}):\n"
                    )
                    for t in threads[:20]:  # Show first 20 threads
                        click.echo(
                            f"  {t['type']}: {t.get('content', 'No content')[:80]}"
                        )
                        click.echo(f"    {t['from_id']} → {t['to_id']}")
                        click.echo(f"    ID: {t['thread_id']}\n")

                    if threads_total > 20:
                        click.echo(f"  ... and {threads_total - 20} more threads\n")

            # Display agents
            if "agents" in results_data:
                agents_data = results_data["agents"]
                agents = agents_data.get("items", [])
                agents_total = agents_data.get("total", 0)

                if agents:
                    click.echo(
                        f"👤 Agents ({agents_total} total, showing {len(agents)}):\n"
                    )
                    for a in agents[:20]:  # Show first 20 agents
                        status_icon = "✓" if a.get("status") == "active" else "○"
                        caps = (
                            ", ".join(a.get("capabilities", []))
                            if a.get("capabilities")
                            else "none"
                        )
                        click.echo(
                            f"  {status_icon} {a['agent_id']}: {a.get('name', 'No name')}"
                        )
                        click.echo(
                            f"    Type: {a.get('agent_type', 'unknown')} | Capabilities: {caps}\n"
                        )

                    if agents_total > 20:
                        click.echo(f"  ... and {agents_total - 20} more agents\n")

            # Display sites
            if "sites" in results_data:
                sites_data = results_data["sites"]
                sites_list = sites_data.get("items", [])
                sites_total = sites_data.get("total", 0)

                if sites_list:
                    click.echo(
                        f"📍 Sites ({sites_total} total, showing {len(sites_list)}):\n"
                    )
                    for s in sites_list[:20]:  # Show first 20 sites
                        status_icon = "✓" if s.get("status") == "active" else "○"
                        click.echo(f"  {status_icon} {s['site_id']}")
                        if s.get("purpose"):
                            click.echo(f"    {s['purpose'][:80]}\n")
                        else:
                            click.echo()

                    if sites_total > 20:
                        click.echo(f"  ... and {sites_total - 20} more sites\n")

            exec_time = response.get("execution_time_ms", 0)
            click.echo(f"(Search completed in {exec_time}ms)")


@cli.command()
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def status(ctx, output_json):
    """Show project status overview."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    # Check server health (direct request, not through /skein prefix)
    try:
        resp = requests.get(f"{base_url}/health", timeout=2)
        health = resp.json()
        server_status = "healthy" if health.get("status") == "healthy" else "unhealthy"
    except Exception:
        server_status = "unreachable"

    # Get project config
    project_config = get_project_config()
    project_name = (
        project_config.get("project_id", "unknown") if project_config else "unknown"
    )

    # Get all folios for counts
    try:
        all_folios = make_request("GET", "/folios", base_url, agent_id)
    except Exception:
        all_folios = []

    # Count open issues and frictions
    open_issues = len(
        [
            f
            for f in all_folios
            if f.get("type") == "issue" and f.get("status", "open") == "open"
        ]
    )
    open_frictions = len(
        [
            f
            for f in all_folios
            if f.get("type") == "friction" and f.get("status", "open") == "open"
        ]
    )
    pending_briefs = len(
        [
            f
            for f in all_folios
            if f.get("type") == "brief" and f.get("status", "open") == "open"
        ]
    )

    # Count folios closed today via status threads
    closed_issues_today = 0
    closed_frictions_today = 0
    closed_today_total = 0
    try:
        # Get status threads with content "closed" from today
        status_threads = make_request(
            "GET",
            "/threads",
            base_url,
            agent_id,
            params={"type": "status", "search": "closed", "since": "1day"},
        )
        # Build lookup of folio types by ID
        folio_types = {f.get("folio_id"): f.get("type") for f in all_folios}
        for thread in status_threads:
            if thread.get("content") == "closed":
                folio_id = thread.get("to_id")
                folio_type = folio_types.get(folio_id)
                closed_today_total += 1
                if folio_type == "issue":
                    closed_issues_today += 1
                elif folio_type == "friction":
                    closed_frictions_today += 1
    except Exception:
        pass

    # Get folios from last hour and count by type
    from datetime import datetime, timedelta

    one_hour_ago = datetime.now() - timedelta(hours=1)

    recent_folios = []
    recent_agents = set()
    for f in all_folios:
        try:
            created = datetime.fromisoformat(
                f["created_at"].replace("Z", "+00:00").replace("+00:00", "")
            )
            if created >= one_hour_ago:
                recent_folios.append(f)
                recent_agents.add(f.get("created_by", "unknown"))
        except Exception:
            pass

    # Count by type for last hour
    type_counts = {}
    for f in recent_folios:
        ftype = f.get("type", "other")
        type_counts[ftype] = type_counts.get(ftype, 0) + 1

    if output_json:
        click.echo(
            json.dumps(
                {
                    "server": base_url,
                    "server_status": server_status,
                    "project": project_name,
                    "open_issues": open_issues,
                    "open_frictions": open_frictions,
                    "closed_issues_today": closed_issues_today,
                    "closed_frictions_today": closed_frictions_today,
                    "closed_today": closed_today_total,
                    "pending_briefs": pending_briefs,
                    "active_agents": len(recent_agents),
                    "last_hour": type_counts,
                },
                indent=2,
            )
        )
        return

    # Format output with colors and alignment
    yellow = "\033[33m"
    reset = "\033[0m"

    click.echo(f"Server:  {base_url} ({server_status})")
    click.echo(f"Project: {project_name}")
    click.echo()
    click.echo(
        f"Issues:     {yellow}{open_issues:>3}{reset} open / {closed_issues_today} closed today"
    )
    click.echo(
        f"Frictions:  {yellow}{open_frictions:>3}{reset} open / {closed_frictions_today} closed today"
    )
    click.echo(f"Briefs:     {yellow}{pending_briefs:>3}{reset} pending")
    click.echo(f"Closed today:   {yellow}{closed_today_total:>3}{reset}")
    click.echo()
    click.echo(f"Active agents:  {yellow}{len(recent_agents):>3}{reset}")
    click.echo()

    # Last hour summary
    if type_counts:
        # B=brief, I=issue, F=finding, R=friction, S=summary, T=tender, W=writ, P=playbook
        type_abbrev = {
            "brief": "B",
            "issue": "I",
            "finding": "F",
            "friction": "R",
            "summary": "S",
            "tender": "T",
            "writ": "W",
            "playbook": "P",
        }
        parts = []
        for ftype, count in sorted(type_counts.items()):
            abbrev = type_abbrev.get(ftype, ftype[0].upper())
            parts.append(f"{count}{abbrev}")
        click.echo(f"Last hour: {' '.join(parts)}")
    else:
        click.echo("Last hour: (no activity)")


@cli.command()
@click.option("--since", help="Time filter (1hour, 2days, or ISO timestamp)")
@click.option(
    "--all",
    "all_projects",
    is_flag=True,
    help="Aggregate activity across all registered projects",
)
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def activity(ctx, since, all_projects, output_json):
    """Get recent activity."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    params = {}
    if since:
        params["since"] = since

    if all_projects:
        registry = _load_projects_registry()
        if not registry:
            click.echo("No projects registered.")
            return

        events = []
        agents: Set[str] = set()
        per_project: Dict[str, Any] = {}
        for project_id in sorted(registry.keys()):
            data = _query_project(
                project_id, "GET", "/activity", base_url, agent_id, params=params
            )
            if data is None:
                continue
            per_project[project_id] = data
            for f in data.get("new_folios", []) or []:
                tagged = dict(f)
                tagged["project"] = project_id
                events.append(tagged)
            for a in data.get("active_agents", []) or []:
                agents.add(a)

        events.sort(key=lambda f: f.get("created_at", ""), reverse=True)

        if output_json:
            click.echo(
                json.dumps(
                    {
                        "all_projects": True,
                        "events": events,
                        "active_agents": sorted(agents),
                        "per_project": per_project,
                    },
                    indent=2,
                    default=str,
                )
            )
            return

        click.echo(
            f"Recent activity across {len(per_project)} project(s):\n"
        )
        click.echo(f"New folios: {len(events)}")
        click.echo(f"Active agents: {len(agents)}")
        if events:
            click.echo("\nRecent folios:")
            for f in events[:20]:
                proj = f.get("project", "?")
                click.echo(
                    f"  [{proj}] {f['type'].upper()}: {f.get('title', '')} ({f['folio_id']})"
                )
        return

    activity_data = make_request("GET", "/activity", base_url, agent_id, params=params)

    if output_json:
        click.echo(json.dumps(activity_data, indent=2))
    else:
        click.echo("Recent activity:\n")
        click.echo(f"New folios: {len(activity_data.get('new_folios', []))}")
        click.echo(f"Active agents: {len(activity_data.get('active_agents', []))}")

        if activity_data.get("new_folios"):
            click.echo("\nRecent folios:")
            for f in activity_data["new_folios"][:10]:
                click.echo(f"  {f['type'].upper()}: {f['title']} ({f['folio_id']})")

        click.echo(ACTIVITY_BREADCRUMB)


# ============================================================================
# Post Commands (Unified posting interface)
# ============================================================================


@cli.group()
def post():
    """Post folios (unified posting interface)."""
    pass


@post.command("issue")
@click.argument("site_id")
@click.argument("title")
@click.option("--content", help="Issue description")
@click.option("--assign", help="Assign to agent")
@click.pass_context
def post_issue(ctx, site_id, title, content, assign):
    """Post an issue.

    Examples:
        skein post issue skein-dev "Fix login bug" --content "Users can't login with OAuth"
        skein post issue speakbot:skein-dev "Cross-project issue"
    """
    validate_positional_args(site_id, title, command_name="post issue")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    site_id, project_override = parse_post_site_id(site_id)

    data = {
        "type": "issue",
        "site_id": site_id,
        "title": title,
        "content": content or title,
        "assigned_to": assign,
        "metadata": {},
    }

    result = make_request(
        "POST", "/folios", base_url, agent_id, json=data, project_id=project_override
    )
    click.echo(f"Created issue: {result['folio_id']}")


@post.command("brief")
@click.argument("site_id")
@click.argument("content")
@click.option("--title", required=True, help="Brief title (required)")
@click.option("--target", help="Target agent")
@click.pass_context
def post_brief(ctx, site_id, content, title, target):
    """Post a handoff brief.

    Examples:
        skein post brief skein-dev "Implement dark mode toggle" --title "Dark mode feature"
        skein post brief skein-dev - --title "Dark mode feature" < content.txt
        skein post brief speakbot:skein-dev "Cross-project brief" --title "..."
    """
    from_stdin = content == "-"
    if from_stdin:
        content = sys.stdin.read()
    validate_positional_args(site_id, command_name="post brief")
    if not from_stdin:
        validate_positional_args(content, command_name="post brief")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    site_id, project_override = parse_post_site_id(site_id)

    data = {
        "type": "brief",
        "site_id": site_id,
        "title": title,
        "content": content,
        "target_agent": target,
        "metadata": {"questions_enabled": True},
    }

    result = make_request(
        "POST", "/folios", base_url, agent_id, json=data, project_id=project_override
    )
    brief_id = result["folio_id"]

    click.echo(f"Created brief: {brief_id}")
    click.echo(f"\nHANDOFF: {brief_id}")


@post.command("friction")
@click.argument("site_id")
@click.argument("title")
@click.option("--details", "-d", help="Additional details (title used if not provided)")
@click.pass_context
def post_friction(ctx, site_id, title, details):
    """Log a friction (problem/blocker).

    Examples:
        skein post friction skein-dev "Must restart server after config changes"
        skein post friction speakbot:skein-dev "Cross-project friction"
    """
    validate_positional_args(site_id, title, command_name="post friction")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    site_id, project_override = parse_post_site_id(site_id)

    data = {
        "type": "friction",
        "site_id": site_id,
        "title": title,
        "content": details or title,
        "metadata": {},
    }

    result = make_request(
        "POST", "/folios", base_url, agent_id, json=data, project_id=project_override
    )
    click.echo(f"Logged friction: {result['folio_id']}")


@post.command("notion")
@click.argument("site_id")
@click.argument("title")
@click.option("--details", "-d", help="Additional details (title used if not provided)")
@click.pass_context
def post_notion(ctx, site_id, title, details):
    """Post a notion (rough idea not fully formed).

    Examples:
        skein post notion skein-dev "Could use websockets for real-time updates"
        skein post notion speakbot:skein-dev "Cross-project notion"
    """
    validate_positional_args(site_id, title, command_name="post notion")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    site_id, project_override = parse_post_site_id(site_id)

    data = {
        "type": "notion",
        "site_id": site_id,
        "title": title,
        "content": details or title,
        "metadata": {},
    }

    result = make_request(
        "POST", "/folios", base_url, agent_id, json=data, project_id=project_override
    )
    click.echo(f"Posted notion: {result['folio_id']}")


@post.command("finding")
@click.argument("site_id")
@click.argument("title")
@click.option("--details", "-d", help="Additional details (title used if not provided)")
@click.pass_context
def post_finding(ctx, site_id, title, details):
    """Post a finding (discovery during investigation).

    Examples:
        skein post finding skein-dev "Redis caching reduces latency by 40%"
        skein post finding speakbot:skein-dev "Cross-project finding"
    """
    validate_positional_args(site_id, title, command_name="post finding")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    site_id, project_override = parse_post_site_id(site_id)

    data = {
        "type": "finding",
        "site_id": site_id,
        "title": title,
        "content": details or title,
        "metadata": {},
    }

    result = make_request(
        "POST", "/folios", base_url, agent_id, json=data, project_id=project_override
    )
    click.echo(f"Posted finding: {result['folio_id']}")


@post.command("summary")
@click.argument("site_id")
@click.argument("title")
@click.option("--details", "-d", help="Additional details (title used if not provided)")
@click.pass_context
def post_summary(ctx, site_id, title, details):
    """Post a summary (completed work findings).

    Examples:
        skein post summary skein-dev "Completed OAuth integration"
        skein post summary speakbot:skein-dev "Cross-project summary"
    """
    validate_positional_args(site_id, title, command_name="post summary")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    site_id, project_override = parse_post_site_id(site_id)

    data = {
        "type": "summary",
        "site_id": site_id,
        "title": title,
        "content": details or title,
        "metadata": {},
    }

    result = make_request(
        "POST", "/folios", base_url, agent_id, json=data, project_id=project_override
    )
    click.echo(f"Posted summary: {result['folio_id']}")


# ============================================================================
# Hypothesis Commands
# ============================================================================


@cli.group()
def hypothesis():
    """Hypothesis tracking for structured investigations."""
    pass


@hypothesis.command("add")
@click.argument("site_id")
@click.argument("claim")
@click.option(
    "--priority",
    "-p",
    type=click.Choice(["high", "medium", "low"]),
    default="medium",
    help="Priority level (default: medium)",
)
@click.option("--source", "-s", help="Where this hypothesis came from")
@click.pass_context
def hypothesis_add(ctx, site_id, claim, priority, source):
    """Add a hypothesis to a site.

    Examples:
        skein hypothesis add recon-target "IDOR on /api/orders"
        skein hypothesis add recon-target "SQL injection in search" --priority high
        skein hypothesis add recon-target "XSS via SVG upload" --source RIFT-0042
    """
    validate_positional_args(site_id, claim, command_name="hypothesis add")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    metadata = {"priority": priority}
    if source:
        metadata["source"] = source

    data = {
        "type": "hypothesis",
        "site_id": site_id,
        "title": claim,
        "content": claim,
        "metadata": metadata,
    }

    result = make_request("POST", "/folios", base_url, agent_id, json=data)
    click.echo(f"Added hypothesis: {result['folio_id']}")


@hypothesis.command("next")
@click.argument("site_id")
@click.pass_context
def hypothesis_next(ctx, site_id):
    """Get the next hypothesis to investigate (highest priority, oldest first).

    Example:
        skein hypothesis next recon-target
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    result = make_request("GET", f"/hypotheses/next/{site_id}", base_url, agent_id)

    hypo = result.get("hypothesis")
    if not hypo:
        click.echo("No pending hypotheses.")
        return

    remaining = result.get("remaining", 0)
    priority = hypo.get("metadata", {}).get("priority", "medium")
    source = hypo.get("metadata", {}).get("source", "")

    click.echo(f"{hypo['folio_id']} [{priority}] {hypo['title']}")
    if source:
        click.echo(f"  Source: {source}")
    click.echo(f"  {remaining} pending")


@hypothesis.command("verdict")
@click.argument("hypothesis_id")
@click.argument(
    "verdict_value",
    type=click.Choice(
        ["confirmed", "disconfirmed", "inconclusive", "deferred", "blocked"]
    ),
)
@click.option("--note", "-n", help="What was tried / what was found")
@click.option("--evidence", "-e", help="Finding folio ID (required for confirmed)")
@click.pass_context
def hypothesis_verdict(ctx, hypothesis_id, verdict_value, note, evidence):
    """Set verdict on a hypothesis.

    Examples:
        skein hypothesis verdict hypothesis-20260305-a7b3 confirmed --evidence finding-20260305-x1y2 --note "PoC works"
        skein hypothesis verdict hypothesis-20260305-a7b3 disconfirmed --note "Tested two accounts, 403 on cross-access"
        skein hypothesis verdict hypothesis-20260305-a7b3 blocked --note "Need admin credentials"
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    data = {"verdict": verdict_value}
    if note:
        data["note"] = note
    if evidence:
        data["evidence"] = evidence

    make_request(
        "POST",
        f"/hypotheses/{hypothesis_id}/verdict",
        base_url,
        agent_id,
        json=data,
    )
    click.echo(f"Verdict: {hypothesis_id} -> {verdict_value}")


@hypothesis.command("list")
@click.argument("site_id")
@click.option(
    "--verdict",
    "-v",
    type=click.Choice(
        ["pending", "confirmed", "disconfirmed", "inconclusive", "deferred", "blocked"]
    ),
    help="Filter by verdict",
)
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def hypothesis_list(ctx, site_id, verdict, output_json):
    """List hypotheses in a site.

    Examples:
        skein hypothesis list recon-target
        skein hypothesis list recon-target --verdict blocked
        skein hypothesis list recon-target --verdict pending
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    params = {"site_id": site_id, "type": "hypothesis"}
    folios = make_request("GET", "/folios", base_url, agent_id, params=params)

    # Filter by verdict if specified
    if verdict:
        if verdict == "pending":
            folios = [f for f in folios if f.get("status", "open") == "open"]
        else:
            folios = [
                f for f in folios if f.get("status", "").split("\n")[0] == verdict
            ]

    if output_json:
        click.echo(json.dumps(folios, indent=2, default=str))
        return

    if not folios:
        click.echo("No hypotheses found.")
        return

    for f in folios:
        status = f.get("status", "open")
        # Extract just the verdict (first line) in case note was concatenated
        display_status = status.split("\n")[0] if status else "open"
        display_status = "pending" if display_status == "open" else display_status
        priority = f.get("metadata", {}).get("priority", "medium")
        click.echo(f"[{display_status}] [{priority}] {f['title']} {f['folio_id']}")


@hypothesis.command("status")
@click.argument("site_id")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def hypothesis_status(ctx, site_id, output_json):
    """Show burndown status for hypotheses in a site.

    Example:
        skein hypothesis status recon-target
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    result = make_request("GET", f"/hypotheses/status/{site_id}", base_url, agent_id)

    if output_json:
        click.echo(json.dumps(result, indent=2))
        return

    total = result.get("total", 0)
    if total == 0:
        click.echo("No hypotheses in this site.")
        return

    click.echo(f"Hypotheses: {total} total")
    known_keys = [
        "pending",
        "confirmed",
        "disconfirmed",
        "inconclusive",
        "deferred",
        "blocked",
    ]
    known_sum = 0
    for key in known_keys:
        count = result.get(key, 0)
        if count > 0:
            click.echo(f"  {key}: {count}")
            known_sum += count
    other = total - known_sum
    if other > 0:
        click.echo(f"  other: {other}")


@hypothesis.command("promote")
@click.argument("notion_id")
@click.option(
    "--priority",
    "-p",
    type=click.Choice(["high", "medium", "low"]),
    default="medium",
    help="Priority level (default: medium)",
)
@click.option("--claim", help="Override claim text (default: notion title)")
@click.pass_context
def hypothesis_promote(ctx, notion_id, priority, claim):
    """Promote a notion to a hypothesis.

    Takes a notion folio and creates a hypothesis from it, closing the original notion.

    Example:
        skein hypothesis promote notion-20260305-a1b2
        skein hypothesis promote notion-20260305-a1b2 --priority high --claim "Refined claim text"
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    # Fetch the notion
    notion_folio = make_request("GET", f"/folios/{notion_id}", base_url, agent_id)
    if notion_folio.get("type") != "notion":
        raise click.ClickException(f"Folio '{notion_id}' is not a notion")

    # Check if already closed/promoted
    notion_status = notion_folio.get("status", "open")
    if notion_status != "open":
        raise click.ClickException(
            f"Notion '{notion_id}' is already {notion_status}. Cannot promote."
        )

    # Create hypothesis from the notion
    hypothesis_claim = claim or notion_folio["title"]
    data = {
        "type": "hypothesis",
        "site_id": notion_folio["site_id"],
        "title": hypothesis_claim,
        "content": notion_folio.get("content", hypothesis_claim),
        "metadata": {"priority": priority, "source": f"promoted from {notion_id}"},
    }

    result = make_request("POST", "/folios", base_url, agent_id, json=data)
    hypo_id = result["folio_id"]

    # Close the notion
    try:
        make_request(
            "PATCH",
            f"/folios/{notion_id}",
            base_url,
            agent_id,
            json={"status": "closed"},
        )
    except Exception:
        click.echo(
            f"Warning: hypothesis {hypo_id} created but failed to close {notion_id}",
            err=True,
        )
        raise

    click.echo(f"Promoted {notion_id} -> {hypo_id}")


# ============================================================================
# Frictions Commands
# ============================================================================


@cli.command(hidden=True)
@click.argument("site_id")
@click.argument("title")
@click.option("--details", "-d", help="Additional details (title used if not provided)")
@click.pass_context
def friction(ctx, site_id, title, details):
    """Log a friction (deprecated: use 'skein post friction')."""
    ctx.invoke(post_friction, site_id=site_id, title=title, details=details)


@cli.command(hidden=True)
@click.argument("site_id")
@click.argument("title")
@click.option("--details", "-d", help="Additional details (title used if not provided)")
@click.pass_context
def notion(ctx, site_id, title, details):
    """Post a notion (deprecated: use 'skein post notion')."""
    ctx.invoke(post_notion, site_id=site_id, title=title, details=details)


@cli.command(hidden=True)
@click.argument("site_id")
@click.argument("title")
@click.option("--details", "-d", help="Additional details (title used if not provided)")
@click.pass_context
def finding(ctx, site_id, title, details):
    """Post a finding (deprecated: use 'skein post finding')."""
    ctx.invoke(post_finding, site_id=site_id, title=title, details=details)


@cli.command()
@click.argument("site_id")
@click.argument("content")
@click.option("--name", help="Mantle name/title")
@click.pass_context
def mantle(ctx, site_id, content, name):
    """Create a mantle (role template for agent orientation).

    Mantles are orientation documents used by `skein ignite --mantle`.
    They contain prompts, instructions, and context for a specific role.

    Examples:
        skein mantle skein-development "You are a researcher..."
        skein mantle opus-agents "# Quartermaster\\n\\nYou oversee..." --name quartermaster
    """
    validate_positional_args(site_id, content, command_name="mantle")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    data = {
        "type": "mantle",
        "site_id": site_id,
        "title": name or make_title_from_content(content),
        "content": content,
        "metadata": {},
    }

    result = make_request("POST", "/folios", base_url, agent_id, json=data)
    click.echo(f"Created mantle: {result['folio_id']}")


@cli.command(hidden=True)
@click.argument("site_id")
@click.argument("title")
@click.option("--details", "-d", help="Additional details (title used if not provided)")
@click.pass_context
def summary(ctx, site_id, title, details):
    """Post a summary (deprecated: use 'skein post summary')."""
    ctx.invoke(post_summary, site_id=site_id, title=title, details=details)


@cli.command()
@click.argument("site_id")
@click.argument("decision")
@click.option(
    "--thread",
    "thread_id",
    help="Tender ID to respond to (updates tender status to 'responded')",
)
@click.pass_context
def writ(ctx, site_id, decision, thread_id):
    """Post a writ (human decision in response to a tender).

    A writ is a human-in-the-loop decision that responds to an agent's tender.
    When --thread points to a tender, the tender's status is auto-updated to 'responded'.

    Examples:
        skein writ skein-dev "Approved for merge"
        skein writ skein-dev "Merge after fixing tests" --thread tender-20251201-abc1
        skein writ skein-dev "Rejected - needs more testing" --thread tender-20251201-xyz9
    """
    validate_positional_args(site_id, decision, command_name="writ")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    # If threading to a tender, verify it exists and is a tender
    if thread_id:
        try:
            tender = make_request("GET", f"/folios/{thread_id}", base_url, agent_id)
            if tender.get("type") != "tender":
                raise click.ClickException(
                    f"{thread_id} is not a tender (type: {tender.get('type')})"
                )
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                raise click.ClickException(f"Tender not found: {thread_id}")
            raise

    # Create writ folio
    data = {
        "type": "writ",
        "site_id": site_id,
        "title": make_title_from_content(decision),
        "content": decision,
        "metadata": {"thread_id": thread_id} if thread_id else {},
    }

    result = make_request("POST", "/folios", base_url, agent_id, json=data)
    writ_id = result["folio_id"]
    click.echo(f"Posted writ: {writ_id}")

    # If threaded to a tender, create reply thread and update tender status
    if thread_id:
        # Create reply thread linking writ to tender
        thread_data = {
            "from_id": writ_id,
            "to_id": thread_id,
            "type": "reply",
            "content": decision,
        }
        make_request("POST", "/threads", base_url, agent_id, json=thread_data)

        # Update tender status to 'responded'
        status_data = {
            "from_id": thread_id,
            "to_id": thread_id,
            "type": "status",
            "content": "responded",
        }
        make_request("POST", "/threads", base_url, agent_id, json=status_data)
        click.echo(f"  Linked to tender: {thread_id}")
        click.echo("  Tender status: responded")


@cli.command()
@click.argument("site_id", required=False)
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def frictions(ctx, site_id, output_json):
    """List frictions."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    params = {"type": "friction"}
    if site_id:
        params["site_id"] = site_id

    frictions_list = make_request("GET", "/folios", base_url, agent_id, params=params)

    if output_json:
        click.echo(json.dumps(frictions_list, indent=2))
    else:
        if not frictions_list:
            click.echo("No frictions found")
        else:
            click.echo(f"Found {len(frictions_list)} friction(s):\n")
            for f in frictions_list:
                click.echo(f"  {f['title']}")
                click.echo(f"    Site: {f['site_id']} | ID: {f['folio_id']}")
                click.echo()


# Threshold above which the folio command prints a fallback header for
# harness-level truncation. Most agent harnesses cap bash tool output between
# 16K-64K characters; below that, the header is just noise.
FOLIO_FALLBACK_HINT_THRESHOLD = 8000


@cli.command()
@click.argument("folio_id")
@click.option("--no-pager", is_flag=True, help="Disable pager")
@click.option(
    "--all",
    "all_projects",
    is_flag=True,
    help="Search all registered projects for this folio",
)
@click.option("--json", "output_json", is_flag=True)
@click.option(
    "--raw",
    is_flag=True,
    help="Print only the raw content (no metadata, no formatting). Use to bypass "
    "harness output caps: skein folio ID --raw > /tmp/folio.md",
)
@click.pass_context
def folio(ctx, folio_id, no_pager, all_projects, output_json, raw):
    """Read a folio by ID. Supports cross-project addressing.

    FOLIO_ID can be bare or project-qualified:

    \b
      skein folio brief-20251226-n1br              # current project, cascades if not found
      skein folio speakbot:brief-20251226-n1br     # look in speakbot project
      skein folio --all brief-20251226-n1br        # explicitly search all projects
      skein folio brief-20251226-n1br --raw        # raw content only (escape harness truncation)
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    if all_projects:
        registry = _load_projects_registry()
        if not registry:
            raise click.ClickException("No projects registered.")
        hits = []
        for project_id in sorted(registry.keys()):
            data = _query_project(
                project_id, "GET", f"/folios/{folio_id}", base_url, agent_id
            )
            if data and isinstance(data, dict) and data.get("folio_id"):
                data["source_project"] = project_id
                hits.append(data)

        if output_json:
            click.echo(json.dumps({"results": hits}, indent=2, default=str))
            return

        if not hits:
            raise click.ClickException(
                f"Folio '{folio_id}' not found in any registered project"
            )

        for i, data in enumerate(hits):
            if i > 0:
                click.echo()
            _render_folio(data, base_url, agent_id, no_pager and i < len(hits) - 1)
        return

    try:
        folio_data = make_request("GET", f"/folios/{folio_id}", base_url, agent_id)
    except click.ClickException as e:
        msg = str(e)
        if not output_json and ("404" in msg or "not found" in msg.lower()):
            raise click.ClickException(f"{msg}\n{FOLIO_NOT_FOUND_BREADCRUMB}")
        raise

    if output_json:
        click.echo(json.dumps(folio_data, indent=2))
        return

    if raw:
        # Bypass all formatting; just emit content. Designed for `> file.md`.
        click.echo(folio_data.get("content", ""), nl=False)
        return

    _render_folio(folio_data, base_url, agent_id, no_pager)


def _render_folio(folio_data, base_url, agent_id, no_pager):
    """Render a single folio for human-readable output."""
    is_tty = sys.stdout.isatty()
    yellow = "\033[33m"
    reset = "\033[0m"

    fid = folio_data.get("folio_id", "unknown")
    ftype = folio_data.get("type", "folio")
    site = folio_data.get("site") or ""
    site_str = f" ({site})" if site else ""
    source_project = folio_data.get("source_project")
    source_str = f" [{source_project}]" if source_project else ""
    content = folio_data.get("content", "") or ""

    # Build output
    output_lines = []

    # Truncation-fallback header. Placed before the title so it survives
    # harness-level output caps (which typically cut from the end). Only
    # shown for folios long enough to plausibly trip a cap.
    if len(content) >= FOLIO_FALLBACK_HINT_THRESHOLD:
        output_lines.append(
            f"[folio {fid}: {len(content)} chars. "
            f"If output below appears truncated, run: "
            f"skein folio {fid} --raw > /tmp/{fid}.md]"
        )
        output_lines.append("")

    output_lines.append(
        f"{yellow}folio {ftype}-{fid.split('-', 1)[-1]}{site_str}{source_str}{reset}"
    )
    output_lines.append(f"Agent: {folio_data.get('created_by', 'unknown')}")
    output_lines.append(
        f"Date:  {folio_data.get('created_at', '')[:19].replace('T', ' ')}"
    )
    if folio_data.get("status"):
        output_lines.append(f"Status: {folio_data.get('status')}")
    output_lines.append("")

    for line in content.split("\n"):
        output_lines.append(f"    {line}")

    try:
        all_threads = make_request("GET", "/threads", base_url, agent_id)
        related_threads = [t for t in all_threads if fid in [t["from_id"], t["to_id"]]]
        if related_threads:
            output_lines.append("")
            output_lines.append(f"    Threads ({len(related_threads)}):")
            for t in related_threads:
                other_id = t["to_id"] if t["from_id"] == fid else t["from_id"]
                output_lines.append(f"      → {other_id}")
    except Exception:
        pass

    output_text = "\n".join(output_lines)
    if is_tty and not no_pager:
        import subprocess

        try:
            proc = subprocess.Popen(["less", "-R"], stdin=subprocess.PIPE)
            proc.communicate(input=output_text.encode())
        except Exception:
            click.echo(output_text)
    else:
        click.echo(output_text)


# Alias: skein show -> skein folio
@cli.command("show")
@click.argument("folio_id")
@click.option("--no-pager", is_flag=True, help="Disable pager")
@click.option(
    "--all", "all_projects", is_flag=True, help="Search all registered projects"
)
@click.option("--json", "output_json", is_flag=True)
@click.option("--raw", is_flag=True, help="Print only the raw content")
@click.pass_context
def show(ctx, folio_id, no_pager, all_projects, output_json, raw):
    """Read a single folio by ID (alias for 'folio')."""
    ctx.invoke(
        folio,
        folio_id=folio_id,
        no_pager=no_pager,
        all_projects=all_projects,
        output_json=output_json,
        raw=raw,
    )


@cli.command()
@click.argument("folio_id")
@click.option(
    "--format",
    "-f",
    "output_format",
    type=click.Choice(["epub", "md", "markdown", "json"]),
    default="epub",
    help="Export format (default: epub)",
)
@click.option(
    "--output", "-o", help="Output file path (default: ./<folio_id>.<format>)"
)
@click.pass_context
def export(ctx, folio_id, output_format, output):
    """Export a folio to various formats (epub, markdown, json).

    Examples:
        skein export brief-20251124-abc
        skein export finding-20251120-xyz --format md
        skein export issue-20251121-def --format epub -o research.epub
    """
    import zipfile
    import uuid
    from datetime import datetime as dt

    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    # Fetch the folio
    folio_data = make_request("GET", f"/folios/{folio_id}", base_url, agent_id)

    title = folio_data.get("title", folio_data.get("folio_id", "Untitled"))
    content = folio_data.get("content", "")
    ftype = folio_data.get("type", "folio")
    created_at = folio_data.get("created_at", "")[:19].replace("T", " ")
    created_by = folio_data.get("created_by", "unknown")
    status = folio_data.get("status", "")

    # Normalize format
    if output_format == "markdown":
        output_format = "md"

    # Determine output path
    if not output:
        output = f"{folio_id}.{output_format}"

    if output_format == "json":
        with open(output, "w") as f:
            json.dump(folio_data, f, indent=2, default=str)
        click.echo(f"Exported to {output}")
        return

    if output_format == "md":
        # Markdown format
        md_content = f"# {title}\n\n"
        md_content += f"**Type:** {ftype}  \n"
        md_content += f"**ID:** {folio_id}  \n"
        md_content += f"**Created:** {created_at}  \n"
        md_content += f"**Author:** {created_by}  \n"
        if status:
            md_content += f"**Status:** {status}  \n"
        md_content += "\n---\n\n"
        md_content += content

        with open(output, "w") as f:
            f.write(md_content)
        click.echo(f"Exported to {output}")
        return

    if output_format == "epub":
        # Generate EPUB
        book_id = f"skein-{folio_id}-{uuid.uuid4().hex[:6]}"

        # Convert content to HTML
        html_content = _content_to_epub_html(content, title)

        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
            # mimetype (must be first and uncompressed)
            zf.writestr(
                "mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED
            )

            # container.xml
            container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""
            zf.writestr("META-INF/container.xml", container_xml)

            # CSS
            css_content = """body {
    font-family: Georgia, serif;
    line-height: 1.6;
    margin: 2em;
    color: #333;
}
h1, h2, h3 { color: #222; margin-top: 1.5em; }
h1 { font-size: 1.8em; border-bottom: 2px solid #333; padding-bottom: 0.3em; }
h2 { font-size: 1.4em; border-bottom: 1px solid #666; padding-bottom: 0.2em; }
h3 { font-size: 1.2em; }
pre { background-color: #f4f4f4; padding: 1em; white-space: pre-wrap; word-wrap: break-word; }
code { background-color: #f4f4f4; padding: 0.2em 0.4em; font-family: monospace; }
table { border-collapse: collapse; margin: 1em 0; width: 100%; }
th, td { border: 1px solid #ddd; padding: 0.5em; text-align: left; }
th { background-color: #f4f4f4; font-weight: bold; }
ul, ol { margin-left: 1.5em; }
li { margin-bottom: 0.3em; }
.metadata { color: #666; font-size: 0.9em; margin-bottom: 1em; }
"""
            zf.writestr("OEBPS/styles.css", css_content)

            # Content XHTML with metadata
            escaped_title = (
                title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            content_xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>{escaped_title}</title>
  <link rel="stylesheet" type="text/css" href="styles.css"/>
</head>
<body>
<h1>{escaped_title}</h1>
<div class="metadata">
<p><strong>Type:</strong> {ftype} | <strong>ID:</strong> {folio_id}</p>
<p><strong>Created:</strong> {created_at} | <strong>Author:</strong> {created_by}</p>
{f"<p><strong>Status:</strong> {status}</p>" if status else ""}
</div>
<hr/>
{html_content}
</body>
</html>"""
            zf.writestr("OEBPS/content.xhtml", content_xhtml)

            # content.opf
            now = dt.now().strftime("%Y-%m-%dT%H:%M:%SZ")
            content_opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="BookId">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="BookId">{book_id}</dc:identifier>
    <dc:title>{escaped_title}</dc:title>
    <dc:creator>{created_by}</dc:creator>
    <dc:language>en</dc:language>
    <meta property="dcterms:modified">{now}</meta>
  </metadata>
  <manifest>
    <item id="content" href="content.xhtml" media-type="application/xhtml+xml"/>
    <item id="styles" href="styles.css" media-type="text/css"/>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
  </manifest>
  <spine>
    <itemref idref="nav"/>
    <itemref idref="content"/>
  </spine>
</package>"""
            zf.writestr("OEBPS/content.opf", content_opf)

            # Navigation document
            nav_xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
  <title>Navigation</title>
  <link rel="stylesheet" type="text/css" href="styles.css"/>
</head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>Table of Contents</h1>
    <ol>
      <li><a href="content.xhtml">{escaped_title}</a></li>
    </ol>
  </nav>
</body>
</html>"""
            zf.writestr("OEBPS/nav.xhtml", nav_xhtml)

        click.echo(f"Exported to {output}")
        return


def _content_to_epub_html(content, title):
    """Convert markdown-like content to HTML for epub export."""
    import re

    def escape_xml(text):
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def format_inline(text):
        text = escape_xml(text)
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<em>\1</em>", text)
        text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
        return text

    lines = content.split("\n")
    html_parts = []
    in_code_block = False
    in_list = False
    list_type = None
    table_rows = []

    for line in lines:
        stripped = line.strip()

        # Code blocks
        if stripped.startswith("```"):
            if in_code_block:
                html_parts.append("</code></pre>")
                in_code_block = False
            else:
                html_parts.append("<pre><code>")
                in_code_block = True
            continue

        if in_code_block:
            html_parts.append(escape_xml(line))
            continue

        # Close list if not a list item
        is_list_item = (
            stripped.startswith("- ")
            or stripped.startswith("* ")
            or (stripped and stripped[0].isdigit() and ". " in stripped)
        )
        if in_list and not is_list_item and stripped:
            html_parts.append(f"</{list_type}>")
            in_list = False
            list_type = None

        # Empty lines - close table if any
        if not stripped:
            if table_rows:
                html_parts.append(_build_table(table_rows))
                table_rows = []
            continue

        # Headers
        if line.startswith("### "):
            html_parts.append(f"<h3>{escape_xml(line[4:])}</h3>")
            continue
        if line.startswith("## "):
            html_parts.append(f"<h2>{escape_xml(line[3:])}</h2>")
            continue
        if line.startswith("# "):
            html_parts.append(f"<h1>{escape_xml(line[2:])}</h1>")
            continue

        # Tables
        if "|" in line and stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if "---" in line:
                continue
            table_rows.append(cells)
            continue

        # Lists
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
                list_type = "ul"
            html_parts.append(f"<li>{format_inline(stripped[2:])}</li>")
            continue

        if stripped and stripped[0].isdigit() and ". " in stripped:
            if not in_list:
                html_parts.append("<ol>")
                in_list = True
                list_type = "ol"
            item_content = stripped.split(". ", 1)[1] if ". " in stripped else stripped
            html_parts.append(f"<li>{format_inline(item_content)}</li>")
            continue

        # Regular paragraph
        if stripped:
            html_parts.append(f"<p>{format_inline(line)}</p>")

    # Close open elements
    if in_list:
        html_parts.append(f"</{list_type}>")
    if table_rows:
        html_parts.append(_build_table(table_rows))
    if in_code_block:
        html_parts.append("</code></pre>")

    return "\n".join(html_parts)


def _build_table(rows):
    """Build HTML table from rows."""
    if not rows:
        return ""

    def format_inline(text):
        import re

        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        return text

    html = ["<table>"]
    html.append(
        "<tr>" + "".join(f"<th>{format_inline(c)}</th>" for c in rows[0]) + "</tr>"
    )
    for row in rows[1:]:
        html.append(
            "<tr>" + "".join(f"<td>{format_inline(c)}</td>" for c in row) + "</tr>"
        )
    html.append("</table>")
    return "\n".join(html)


@cli.command()
@click.argument("folio_id")
@click.option("--title", "-t", help="New title for the folio")
@click.option("--content", "-c", help="New content for the folio")
@click.option("--status", "-s", help="New status (e.g., open, closed, investigating)")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def edit(ctx, folio_id, title, content, status, output_json):
    """Edit a folio's title, content, or status.

    Examples:
        skein edit brief-20251124-abc --title "Updated title"
        skein edit issue-20251120-xyz --status closed
        skein edit friction-20251121-def --content "New description"
    """
    if not title and not content and not status:
        raise click.ClickException(
            "At least one of --title, --content, or --status must be provided.\n"
            "Usage: skein edit FOLIO_ID [--title TEXT] [--content TEXT] [--status TEXT]"
        )

    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    # Build update payload - only include fields that were provided
    update_data = {}
    if title is not None:
        update_data["title"] = title
    if content is not None:
        update_data["content"] = content
    if status is not None:
        update_data["status"] = status

    result = make_request(
        "PATCH", f"/folios/{folio_id}", base_url, agent_id, json=update_data
    )

    if output_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    if result.get("success"):
        updated_folio = result.get("folio", {})
        click.echo(f"Updated {folio_id}")
        if title:
            click.echo(f"  Title: {updated_folio.get('title', title)}")
        if content:
            # Truncate content for display
            display_content = content[:50] + "..." if len(content) > 50 else content
            click.echo(f"  Content: {display_content}")
        if status:
            click.echo(f"  Status: {updated_folio.get('status', status)}")
    else:
        raise click.ClickException(f"Failed to update folio: {result}")


@cli.command()
@click.argument("folio_id")
@click.argument("dest_site_id")
@click.option("--note", "-n", help="Note explaining why the folio is being moved")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def move(ctx, folio_id, dest_site_id, note, output_json):
    """Move a folio from its current site to a different site.

    Examples:
        skein move brief-20251226-abc new-site
        skein move brief-20251226-abc new-site --note "Consolidating mesh work"
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    # Build move payload
    move_data = {"dest_site_id": dest_site_id}
    if note:
        move_data["note"] = note

    result = make_request(
        "POST", f"/folios/{folio_id}/move", base_url, agent_id, json=move_data
    )

    if output_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    if result.get("success"):
        result.get("folio", {})
        click.echo(f"Moved {folio_id} to {dest_site_id}")
        if note:
            click.echo(f"  Note: {note}")
    else:
        raise click.ClickException(f"Failed to move folio: {result}")


@cli.command(hidden=True)
@click.argument("site_id")
@click.option("--type", help="Filter by folio type")
@click.option("--status", help="Filter by status")
@click.option(
    "-n",
    "--limit",
    type=int,
    help="Limit number of folios shown (default: 20 for agents, unlimited for TTY)",
)
@click.option(
    "--all", "show_all", is_flag=True, help="Show all folios (override default limit)"
)
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def folios(ctx, site_id, type, status, limit, show_all, output_json):
    """List all folios in a site. (Deprecated: use 'find --site SITE_ID')"""
    # Validate site_id is not empty
    if not site_id or site_id.strip() == "":
        raise click.ClickException(
            "site_id cannot be empty. Usage: skein folios SITE_ID"
        )

    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    params = {"site_id": site_id}
    if type:
        params["type"] = type
    if status:
        params["status"] = status

    folios_list = make_request("GET", "/folios", base_url, agent_id, params=params)

    # Apply default limit for non-TTY (agents) unless --all specified
    total_count = len(folios_list)
    is_tty = sys.stdout.isatty()

    if not show_all:
        if limit:
            folios_list = folios_list[:limit]
        elif not is_tty:
            # Agent default: limit to 20
            folios_list = folios_list[:20]

    if output_json:
        click.echo(json.dumps(folios_list, indent=2))
    else:
        if not folios_list and total_count == 0:
            click.echo(f"No folios found in site {site_id}")
        else:
            # Show count with truncation info if applicable
            showing_count = len(folios_list)
            if showing_count < total_count:
                click.echo(
                    f"Showing {showing_count} of {total_count} folio(s) in site {site_id}:\n"
                )
            else:
                click.echo(f"Found {total_count} folio(s) in site {site_id}:\n")

            # Group by type for better readability
            by_type = {}
            for f in folios_list:
                folio_type = f["type"]
                if folio_type not in by_type:
                    by_type[folio_type] = []
                by_type[folio_type].append(f)

            # OPTIMIZATION: Batch fetch all threads once (1 API call vs N*2)
            try:
                all_threads = make_request("GET", "/threads", base_url, agent_id)
                # Build lookup dict: resource_id -> [threads]
                threads_by_resource = {}
                for thread in all_threads:
                    # Index by both from_id and to_id
                    if thread["from_id"] not in threads_by_resource:
                        threads_by_resource[thread["from_id"]] = []
                    threads_by_resource[thread["from_id"]].append(thread)

                    if thread["to_id"] not in threads_by_resource:
                        threads_by_resource[thread["to_id"]] = []
                    threads_by_resource[thread["to_id"]].append(thread)
            except Exception:
                # Fall back to no threads if batch fetch fails
                threads_by_resource = {}

            for folio_type in sorted(by_type.keys()):
                click.echo(
                    f"  {folio_type.upper()} ({len(by_type[folio_type])} item(s)):"
                )
                for f in by_type[folio_type]:
                    status_str = f"[{f['status']}]" if f.get("status") else ""
                    click.echo(f"    {f['folio_id']} {status_str}")
                    click.echo(f"      {f['title']}")

                    # Get threads from batch-fetched data
                    try:
                        resource_threads = threads_by_resource.get(f["folio_id"], [])

                        # Dedupe threads (same thread appears in from_id and to_id indexes)
                        thread_ids = set()
                        unique_threads = []
                        for t in resource_threads:
                            if t["thread_id"] not in thread_ids:
                                thread_ids.add(t["thread_id"])
                                unique_threads.append(t)

                        # Extract tags (self-referential threads with type tag)
                        tags = [
                            t["content"]
                            for t in unique_threads
                            if t["type"] == "tag" and t["from_id"] == t["to_id"]
                        ]

                        # Build breadcrumb
                        breadcrumb_parts = []
                        if len(unique_threads) > 0:
                            breadcrumb_parts.append(f"{len(unique_threads)} threads")
                        if tags:
                            breadcrumb_parts.append(f"Tags: {', '.join(tags)}")

                        if breadcrumb_parts:
                            click.echo(f"      {' | '.join(breadcrumb_parts)}")
                    except Exception:
                        # Silently skip if thread processing fails
                        pass

                    if f.get("assigned_to"):
                        click.echo(f"      Assigned: {f['assigned_to']}")
                click.echo()

            # Show truncation hint if limited
            if showing_count < total_count:
                remaining = total_count - showing_count
                click.echo(
                    f"({remaining} more folios, use --all or -n {total_count} to see all)"
                )


@cli.command(hidden=True)
@click.argument("site_ids", nargs=-1, required=True)
@click.option("--type", help="Filter by folio type")
@click.option("--status", help="Filter by status")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def survey(ctx, site_ids, type, status, output_json):
    """Survey folios across multiple sites. (Deprecated: use 'find --site PATTERN')

    Example:
        skein survey opus-coding-assistant opus-security-architect
        skein survey opus-* --type issue
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    # Validate all site_ids are non-empty
    for site_id in site_ids:
        if not site_id or site_id.strip() == "":
            raise click.ClickException(
                "site_id cannot be empty. Usage: skein survey SITE_ID [SITE_ID...]"
            )

    all_results = {}
    total_folios = 0
    errors = []

    # Query each site
    for site_id in site_ids:
        try:
            params = {"site_id": site_id}
            if type:
                params["type"] = type
            if status:
                params["status"] = status

            folios_list = make_request(
                "GET", "/folios", base_url, agent_id, params=params
            )
            all_results[site_id] = folios_list
            total_folios += len(folios_list)
        except Exception as e:
            errors.append((site_id, str(e)))
            all_results[site_id] = []

    # Output results
    if output_json:
        output = {
            "sites": all_results,
            "total_folios": total_folios,
            "errors": [{"site_id": s, "error": e} for s, e in errors],
        }
        click.echo(json.dumps(output, indent=2))
    else:
        # Human-readable output
        click.echo(f"Surveying {len(site_ids)} site(s)...\n")

        for site_id in site_ids:
            folios = all_results[site_id]

            click.echo(f"{'=' * 60}")
            click.echo(f"Site: {site_id}")
            click.echo(f"{'=' * 60}")

            if site_id in [s for s, _ in errors]:
                error_msg = next(e for s, e in errors if s == site_id)
                click.echo(f"  ❌ Error: {error_msg}\n")
                continue

            if not folios:
                click.echo("  No folios found\n")
                continue

            click.echo(f"  Found {len(folios)} folio(s)\n")

            # Group by type
            by_type = {}
            for f in folios:
                folio_type = f["type"]
                if folio_type not in by_type:
                    by_type[folio_type] = []
                by_type[folio_type].append(f)

            for folio_type in sorted(by_type.keys()):
                click.echo(
                    f"  {folio_type.upper()} ({len(by_type[folio_type])} item(s)):"
                )
                for f in by_type[folio_type]:
                    status_str = f"[{f['status']}]" if f.get("status") else ""
                    # Format created_at date
                    created_at = f.get("created_at", "")
                    if created_at:
                        # Parse ISO format and display as YYYY-MM-DD
                        try:
                            dt = datetime.fromisoformat(
                                created_at.replace("Z", "+00:00")
                            )
                            date_str = dt.strftime("%Y-%m-%d")
                        except (ValueError, AttributeError):
                            date_str = (
                                created_at[:10] if len(created_at) >= 10 else created_at
                            )
                    else:
                        date_str = ""

                    click.echo(f"    {f['folio_id']} {status_str} {date_str}")
                    click.echo(
                        f"      {f['title'][:80]}{'...' if len(f['title']) > 80 else ''}"
                    )

                    # Show content preview (first 100 chars, single line)
                    content = f.get("content", "")
                    if content:
                        # Clean up content: replace newlines with spaces, truncate
                        preview = " ".join(content.split())[:100]
                        if len(content) > 100:
                            preview += "..."
                        click.echo(f"      {preview}")
                click.echo()

        click.echo(f"{'=' * 60}")
        click.echo(f"Total: {total_folios} folio(s) across {len(site_ids)} site(s)")
        if errors:
            click.echo(f"Errors: {len(errors)} site(s) failed")
        click.echo(f"{'=' * 60}")


# ============================================================================
# Signals & Roster Commands
# ============================================================================


@cli.command()
@click.argument("to_id")
@click.argument("message")
@click.pass_context
def message(ctx, to_id, message):
    """Send a message to an agent (creates thread)."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    data = {"from_id": agent_id, "to_id": to_id, "type": "message", "content": message}

    result = make_request("POST", "/threads", base_url, agent_id, json=data)
    click.echo(f"Sent message: {result['thread_id']}")


@cli.command()
@click.argument("resource_id", required=False)
@click.option("--from-id", "from_filter", help="Filter threads from this resource")
@click.option("--to-id", "to_filter", help="Filter threads to this resource")
@click.option("--type", "type_filter", help="Filter by thread type")
@click.option("--weaver", help="Filter by agent who created the thread")
@click.option("--search", help="Full-text search in thread content")
@click.option("--since", help="Time filter (e.g., '1hour', '2days', or ISO timestamp)")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def threads(
    ctx,
    resource_id,
    from_filter,
    to_filter,
    type_filter,
    weaver,
    search,
    since,
    output_json,
):
    """Get threads from/to a resource.

    Examples:
        skein threads RESOURCE_ID              # All threads for a resource
        skein threads --weaver agent-007       # All threads created by agent-007
        skein threads --type status            # All status threads
        skein threads --search "bug fix"       # Full-text search in content
        skein threads --since 1hour            # Threads from last hour
        skein threads --weaver me --type status --since 1day  # My recent status changes
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    params = {}
    if resource_id:
        # If resource_id provided, show threads from OR to that resource
        # We'll make two requests and combine
        from_threads = make_request(
            "GET", "/threads", base_url, agent_id, params={"from_id": resource_id}
        )
        to_threads = make_request(
            "GET", "/threads", base_url, agent_id, params={"to_id": resource_id}
        )
        all_threads = from_threads + to_threads
        # Dedupe by thread_id
        seen = set()
        threads_list = []
        for t in all_threads:
            if t["thread_id"] not in seen:
                seen.add(t["thread_id"])
                threads_list.append(t)
    else:
        if from_filter:
            params["from_id"] = from_filter
        if to_filter:
            params["to_id"] = to_filter
        if type_filter:
            params["type"] = type_filter
        if weaver:
            # Support "me" as alias for current agent
            params["weaver"] = agent_id if weaver == "me" else weaver
        if search:
            params["search"] = search
        if since:
            params["since"] = since
        threads_list = make_request(
            "GET", "/threads", base_url, agent_id, params=params
        )

    if output_json:
        click.echo(json.dumps(threads_list, indent=2))
    else:
        if not threads_list:
            click.echo("No threads found")
        else:
            click.echo(f"Found {len(threads_list)} thread(s):\n")
            for t in threads_list:
                click.echo(f"  [{t['type'].upper()}] {t['from_id']} -> {t['to_id']}")
                if t.get("content"):
                    click.echo(f"    {t['content'][:100]}")
                click.echo(f"    ID: {t['thread_id']}")
                click.echo()


@cli.command("thread-tree")
@click.argument("resource_id")
@click.option(
    "--depth", type=int, default=3, help="Maximum depth to traverse (default: 3)"
)
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def thread_tree(ctx, resource_id, depth, output_json):
    """Visualize thread conversations as a tree.

    Shows all threads connected to a resource in a tree structure,
    following reply chains and related conversations.

    Examples:
        skein thread-tree issue-123
        skein thread-tree brief-456 --depth 5
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    def get_threads_for_resource(res_id):
        """Get all threads from/to a resource."""
        from_threads = make_request(
            "GET", "/threads", base_url, agent_id, params={"from_id": res_id}
        )
        to_threads = make_request(
            "GET", "/threads", base_url, agent_id, params={"to_id": res_id}
        )

        # Combine and dedupe
        all_threads = from_threads + to_threads
        seen = set()
        unique = []
        for t in all_threads:
            if t["thread_id"] not in seen:
                seen.add(t["thread_id"])
                unique.append(t)
        return unique

    def build_tree(res_id, current_depth=0, visited=None):
        """Recursively build thread tree."""
        if visited is None:
            visited = set()

        if current_depth >= depth or res_id in visited:
            return None

        visited.add(res_id)
        threads = get_threads_for_resource(res_id)

        node = {"id": res_id, "threads": [], "children": []}

        for thread in threads:
            thread_info = {
                "thread_id": thread["thread_id"],
                "type": thread["type"],
                "from_id": thread["from_id"],
                "to_id": thread["to_id"],
                "content": thread.get("content", "")[:100],
            }
            node["threads"].append(thread_info)

            # Follow outbound replies to build conversation tree
            if thread["type"] == "reply" and thread["from_id"] == res_id:
                child = build_tree(thread["to_id"], current_depth + 1, visited)
                if child:
                    node["children"].append(child)

        return node

    tree = build_tree(resource_id)

    if output_json:
        click.echo(json.dumps(tree, indent=2))
    else:

        def print_tree(node, prefix="", is_last=True):
            """Pretty print the tree."""
            if not node:
                return

            # Print current node
            connector = "└── " if is_last else "├── "
            click.echo(f"{prefix}{connector}{node['id']}")

            # Print threads
            thread_prefix = prefix + ("    " if is_last else "│   ")
            for i, thread in enumerate(node["threads"]):
                is_last_thread = (i == len(node["threads"]) - 1) and not node[
                    "children"
                ]
                thread_connector = "└── " if is_last_thread else "├── "

                direction = "→" if thread["from_id"] == node["id"] else "←"
                other_id = (
                    thread["to_id"]
                    if thread["from_id"] == node["id"]
                    else thread["from_id"]
                )

                click.echo(
                    f"{thread_prefix}{thread_connector}[{thread['type'].upper()}] {direction} {other_id}"
                )
                if thread.get("content"):
                    content_prefix = thread_prefix + (
                        "    " if is_last_thread else "│   "
                    )
                    click.echo(f'{content_prefix}  "{thread["content"]}"')

            # Print children
            for i, child in enumerate(node["children"]):
                is_last_child = i == len(node["children"]) - 1
                child_prefix = prefix + ("    " if is_last else "│   ")
                print_tree(child, child_prefix, is_last_child)

        click.echo(f"\nThread tree for {resource_id}:\n")
        print_tree(tree)
        click.echo()


@cli.command()
@click.argument("from_id", required=False)
@click.argument("to_id")
@click.argument("thread_type")
@click.argument("content")
@click.pass_context
def thread(ctx, from_id, to_id, thread_type, content):
    """Create a thread between any two resources.

    If FROM_ID is omitted, defaults to current agent.

    Examples:
        skein thread issue-123 issue-123 tag bug
        skein thread agent-1 issue-456 comment "Found the problem"
        skein thread thread-abc reply "Good point"
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    # Handle the case where FROM_ID is omitted
    if content is None:
        # Arguments shifted: from_id is actually to_id, to_id is type, thread_type is content
        content = thread_type
        thread_type = to_id
        to_id = from_id
        from_id = agent_id
        if from_id is None:
            raise click.ClickException("Must set agent ID to use default FROM_ID")

    data = {"from_id": from_id, "to_id": to_id, "type": thread_type, "content": content}

    result = make_request("POST", "/threads", base_url, agent_id, json=data)
    click.echo(f"Created thread: {result['thread_id']}")


@cli.command()
@click.argument("to_id")
@click.argument("message")
@click.pass_context
def reply(ctx, to_id, message):
    """Reply to or comment on any resource.

    Creates a thread from current agent to the resource with type:reply.
    Works on issues, briefs, findings, threads, or any resource ID.

    Examples:
        skein reply issue-123 "I'll investigate this bug"
        skein reply brief-456 "The approach looks good"
        skein reply finding-789 "This explains the performance issue"
        skein reply thread-abc "Good point, let me check"
        skein reply notion-xyz "Interesting idea, we should explore this"
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    if agent_id is None:
        raise click.ClickException(
            "Must set SKEIN_AGENT_ID or use --agent flag to reply"
        )

    data = {"from_id": agent_id, "to_id": to_id, "type": "reply", "content": message}

    result = make_request("POST", "/threads", base_url, agent_id, json=data)
    click.echo(f"Posted reply: {result['thread_id']}")


@cli.command()
@click.argument("resource_id")
@click.argument("tag_name")
@click.pass_context
def tag(ctx, resource_id, tag_name):
    """Tag a resource (self-referential thread).

    Creates a thread from resource to itself with type:tag.

    Examples:
        skein tag issue-123 bug
        skein tag issue-123 critical
        skein tag brief-456 needs-review
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    data = {
        "from_id": resource_id,
        "to_id": resource_id,
        "type": "tag",
        "content": tag_name,
    }

    make_request("POST", "/threads", base_url, agent_id, json=data)
    click.echo(f"Tagged {resource_id} as '{tag_name}'")


@cli.command()
@click.argument("resource_id")
@click.argument(
    "status_value",
    type=click.Choice(
        ["open", "closed", "investigating", "resolved", "blocked", "in-progress"]
    ),
)
@click.pass_context
def update(ctx, resource_id, status_value):
    """Set status on a resource.

    Creates a thread from current agent to resource with type:status.

    Examples:
        skein update issue-123 investigating
        skein update issue-123 closed
        skein update issue-456 open
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    if agent_id is None:
        raise click.ClickException(
            "Must set SKEIN_AGENT_ID or use --agent flag to set status"
        )

    data = {
        "from_id": agent_id,
        "to_id": resource_id,
        "type": "status",
        "content": status_value,
    }

    make_request("POST", "/threads", base_url, agent_id, json=data)
    click.echo(f"Set status of {resource_id} to '{status_value}'")


@cli.command()
@click.argument("resource_ids", nargs=-1, required=True)
@click.option("--link", help="Link to solution (folio ID)")
@click.option("--note", help="Note about the fix")
@click.pass_context
def close(ctx, resource_ids, link, note):
    """Close one or more issues/frictions (sets status to closed).

    Examples:
        skein close issue-123
        skein close issue-123 --note "Fixed the bug"
        skein close issue-123 folio-456 folio-789 --note "batch close"
        skein close issue-123 --link summary-456
        skein close friction-789 --link summary-456 --note "Fixed by adding validation"
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    if agent_id is None:
        raise click.ClickException(
            "Must set SKEIN_AGENT_ID or use --agent flag to close"
        )

    for resource_id in resource_ids:
        # Create status thread (closed)
        status_data = {
            "from_id": resource_id,
            "to_id": resource_id,
            "type": "status",
            "content": "closed",
        }
        make_request("POST", "/threads", base_url, agent_id, json=status_data)
        click.echo(f"Closed {resource_id}")

        # Create message thread if --note provided (without link)
        if note and not link:
            note_data = {
                "from_id": agent_id,
                "to_id": resource_id,
                "type": "message",
                "content": note,
            }
            make_request("POST", "/threads", base_url, agent_id, json=note_data)
            click.echo(f"  Note: {note}")

        # Create reference thread if --link provided
        if link:
            ref_content = note if note else "Resolved"
            ref_data = {
                "from_id": resource_id,
                "to_id": link,
                "type": "reference",
                "content": ref_content,
            }
            make_request("POST", "/threads", base_url, agent_id, json=ref_data)
            click.echo(f"Linked to {link}: {ref_content}")


@cli.command()
@click.option("--capabilities", help="Comma-separated capabilities")
@click.option(
    "--name",
    help="Human-readable name (e.g., 'Front End Developer', 'Race Condition Fixer')",
)
@click.option(
    "--type",
    "agent_type",
    type=click.Choice(["claude-code", "patbot", "horizon", "human", "system"]),
    help="Agent type",
)
@click.option("--description", help="Longer description of work and focus")
@click.pass_context
def register(ctx, capabilities, name, agent_type, description):
    """Register in the roster."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    if agent_id is None:
        raise click.ClickException(
            "Must set SKEIN_AGENT_ID or use --agent flag to register"
        )

    caps_list = [c.strip() for c in capabilities.split(",")] if capabilities else []

    data = {"agent_id": agent_id, "capabilities": caps_list, "metadata": {}}

    if name:
        data["name"] = name
    if agent_type:
        data["agent_type"] = agent_type
    if description:
        data["description"] = description

    make_request("POST", "/roster/register", base_url, agent_id, json=data)
    click.echo(f"Registered: {agent_id}")
    if name:
        click.echo(f"Name: {name}")
    if agent_type:
        click.echo(f"Type: {agent_type}")
    if caps_list:
        click.echo(f"Capabilities: {', '.join(caps_list)}")
    if description:
        click.echo(f"Description: {description}")


@cli.command("ignite")
@click.argument("brief_id", required=False)
@click.option("--mantle", help="Ignite from mantle (role template)")
@click.option("--message", help="Initial task/mission")
@click.pass_context
def ignite_start(ctx, brief_id, mantle, message):
    """
    Start ignition - Begin orientation for agent work.

    Usage:
        skein ignite brief-123                      # From brief
        skein ignite --mantle quartermaster         # From mantle
        skein ignite --mantle quartermaster --message "Track inventory"
        skein ignite --message "Ad-hoc task"        # Just message
        skein ignite                                # Generic

    After orientation, register with:
        skein ready
    """
    _ignite_start(ctx, brief_id, mantle, message)


def _get_existing_agent_names(base_url: str, agent_id: str) -> Set[str]:
    """
    Get set of existing agent names from roster for collision detection.

    Returns:
        Set of agent names (not IDs) currently in roster
    """
    try:
        agents = make_request("GET", "/roster", base_url, agent_id)
        return {a.get("name", "").lower() for a in agents if a.get("name")}
    except Exception:
        return set()


def _generate_suggested_name(
    base_url: str,
    agent_id: str,
    mantle: Optional[str],
    mantle_data: Optional[dict],
    brief_content: str = "",
) -> str:
    """
    Generate a memorable suggested name for the agent.

    Uses the new generate_agent_name() function with collision detection
    against existing roster names. Falls back to legacy naming if the
    name generator is not available.

    Args:
        base_url: SKEIN server URL
        agent_id: Current agent ID (for roster lookup)
        mantle: Mantle name if provided
        mantle_data: Loaded mantle data if available
        brief_content: Brief/task content for context-aware naming

    Returns:
        Suggested agent name
    """
    # Get existing names for collision detection
    existing_names = _get_existing_agent_names(base_url, agent_id)

    # Get project config for name generator
    project_config = get_project_config()
    project_id = project_config.get("project_id") if project_config else None

    # Try the new name generator
    if generate_agent_name is not None:
        try:
            return generate_agent_name(
                existing_names=existing_names,
                project=project_id,
                role=mantle,
                brief_content=brief_content,
            )
        except Exception:
            pass  # Fall back to legacy naming

    # Legacy fallback: mantle-based naming
    suggested_name = f"Agent {agent_id.split('-')[-1]}"

    if mantle_data and mantle_data.get("naming_style"):
        naming_style = mantle_data["naming_style"]
        if naming_style == "technical":
            suggested_name = f"Silent {mantle.title()}"
        elif naming_style == "pm":
            suggested_name = "Dawn"
        elif naming_style == "emergency":
            suggested_name = f"Midnight {mantle.title()}"
    elif mantle:
        suggested_name = f"{mantle.title()} Agent"

    return suggested_name


def _ignite_start(ctx, brief_id, mantle, message):
    """
    Start ignition process - Begin orientation for agent work.

    After orientation, register with:
        skein ready
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)
    # agent_id may be None - that's fine, identity comes at ready

    # Prepare response data
    response = {
        "status": "orienting",
        "agent_id": agent_id,
        "mission": None,
        "brief_id": brief_id,
        "mantle_name": mantle,
        "message": message,
    }

    mission_parts = []
    brief_content = ""

    # If brief provided, load it
    if brief_id:
        try:
            brief = make_request("GET", f"/folios/{brief_id}", base_url, agent_id)
            brief_content = brief.get("content", "")
            mission_parts.append(f"**From Brief ({brief_id}):**\n{brief_content}")
        except Exception as e:
            raise click.ClickException(f"Failed to load brief: {str(e)}")

    # If mantle provided, load it as a folio from SKEIN
    mantle_data = None
    mantle_content = ""
    if mantle:
        try:
            # If it looks like a folio ID (mantle-YYYYMMDD-xxxx), use directly
            if mantle.startswith("mantle-"):
                mantle_folio = make_request(
                    "GET", f"/folios/{mantle}", base_url, agent_id
                )
            else:
                # Search for mantle by name using the /search endpoint
                search_response = make_request(
                    "GET",
                    "/search",
                    base_url,
                    agent_id,
                    params={"q": mantle, "type": "mantle"},
                )
                folios_data = search_response.get("results", {}).get("folios", {})
                results = folios_data.get("items", [])
                if not results:
                    raise click.ClickException(f"No mantle found matching '{mantle}'")
                # Prefer exact title match, otherwise take first result
                mantle_folio = None
                for r in results:
                    if r.get("title", "").lower() == mantle.lower():
                        mantle_folio = r
                        break
                if not mantle_folio:
                    mantle_folio = results[0]
                    if len(results) > 1:
                        click.echo(
                            f"Note: Multiple mantles match '{mantle}', using '{mantle_folio.get('title', mantle_folio.get('folio_id'))}'",
                            err=True,
                        )
            mantle_content = mantle_folio.get("content", "")
            mission_parts.append(f"**From Mantle ({mantle}):**\n{mantle_content}")
            # Store folio data for naming context
            mantle_data = {"content": mantle_content}
        except click.ClickException:
            raise
        except Exception as e:
            raise click.ClickException(
                f"Failed to load mantle folio '{mantle}': {str(e)}"
            )

    # If message provided, add it
    if message:
        mission_parts.append(f"**Initial Task:**\n{message}")

    response["mission"] = "\n\n".join(mission_parts) if mission_parts else None

    # Get project context to suggest reading
    project_root = find_project_root()
    suggested_reading = []

    if project_root:
        # Core docs
        core_docs = [
            "CLAUDE.md",
            "docs/PROJECT_CONTEXT.md",
            "docs/SKEIN_QUICK_START.md",
            "docs/ARCHITECTURE.md",
        ]
        # Conditional docs
        conditional_docs = [
            "docs/TESTING_GUIDE.md",
            "docs/HORIZON_EXAMPLE.md",
            "docs/TOOL_CREATION_GUIDE.md",
            "docs/AGENT_CREATION_GUIDE.md",
            "docs/SKEIN_AGENT_GUIDE.md",
            "docs/TOKEN_TERMINOLOGY.md",
        ]

        for doc in core_docs + conditional_docs:
            doc_path = project_root / doc
            if doc_path.exists():
                suggested_reading.append(str(doc))

    response["suggested_reading"] = suggested_reading

    # Generate memorable suggested name
    # Combine brief, mantle, and message content for naming context
    content_parts = [brief_content, mantle_content, message or ""]
    naming_context = "\n".join(p for p in content_parts if p)
    suggested_name = _generate_suggested_name(
        base_url, agent_id, mantle, mantle_data, naming_context
    )
    response["suggested_name"] = suggested_name

    # Register on roster as "orienting" with the generated name
    try:
        register_data = {
            "agent_id": suggested_name,
            "name": suggested_name,
            "status": "orienting",
            "metadata": {
                "ignited_at": datetime.now().isoformat(),
                "ignited_from": brief_id,
                "mantle": mantle,
                "message": message,
            },
        }
        make_request(
            "POST", "/roster/register", base_url, suggested_name, json=register_data
        )
    except Exception as e:
        # Log but don't fail - registration is not critical
        click.echo(f"Note: Could not register on roster: {e}", err=True)

    # Output results
    click.echo("=" * 60)
    click.echo("IGNITION - Orientation Phase")
    click.echo("=" * 60)
    click.echo()

    if response["mission"]:
        if brief_id:
            click.echo(f"Brief: {brief_id}")
        if mantle:
            click.echo(f"Mantle: {mantle}")
        if message:
            click.echo(f"Message: {message}")
        click.echo()
        click.echo("Mission:")
        click.echo(response["mission"])
        click.echo()
    else:
        click.echo("Generic ignition (no brief, mantle, or message provided)")
        click.echo()

    if suggested_reading:
        click.echo("REQUIRED Reading:")
        # Core docs
        core_docs = [
            "CLAUDE.md",
            "PROJECT_CONTEXT.md",
            "SKEIN_QUICK_START.md",
            "ARCHITECTURE.md",
        ]
        for doc in core_docs:
            if any(doc in s for s in suggested_reading):
                matching = [s for s in suggested_reading if doc in s][0]
                click.echo(f"├── {doc}")
                suggested_reading.remove(matching)

        # Conditional docs
        testing_docs = ["TESTING_GUIDE.md"]
        system_docs = [
            "HORIZON_EXAMPLE.md",
            "TOOL_CREATION_GUIDE.md",
            "AGENT_CREATION_GUIDE.md",
            "SKEIN_AGENT_GUIDE.md",
            "TOKEN_TERMINOLOGY.md",
        ]

        has_testing = any(
            any(td in s for s in suggested_reading) for td in testing_docs
        )
        has_system = any(any(sd in s for s in suggested_reading) for sd in system_docs)

        if has_testing:
            click.echo()
            click.echo("IF TESTING")
            for doc in testing_docs:
                if any(doc in s for s in suggested_reading):
                    click.echo(f"├── {doc}")

        if has_system:
            click.echo()
            click.echo("IF WORKING WITH SPECIFIC SYSTEMS")
            for i, doc in enumerate(system_docs):
                if any(doc in s for s in suggested_reading):
                    prefix = "└──" if i == len(system_docs) - 1 else "├──"
                    if doc == "SKEIN_AGENT_GUIDE.md":
                        click.echo(f"{prefix} {doc} (comprehensive SKEIN guide)")
                    elif doc == "TOKEN_TERMINOLOGY.md":
                        click.echo(
                            f"{prefix} {doc} (use Payload/Burn/Creep terms when discussing tokens to disambiguate in discussion of token use)"
                        )
                    else:
                        click.echo(f"{prefix} {doc}")

        click.echo()

    click.echo(f"You are: {suggested_name}")
    click.echo()
    click.echo(
        "After reading, explore project files and the SKEIN for relevant information. After you've fully oriented, run:"
    )
    click.echo()
    click.echo(f"  skein --agent {suggested_name} ready")
    click.echo()


@cli.command("ready")
@click.pass_context
def ready(ctx):
    """
    Complete ignition - Activate and begin work.

    Usage:
        skein --agent NAME ready

    Transitions agent from 'orienting' to 'active' status.
    The agent must have been registered during 'skein ignite'.
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = ctx.obj.get("agent")

    if not agent_id:
        raise click.ClickException(
            "Must use --agent flag. Run 'skein ignite' first to get your assigned name."
        )

    # Verify agent was registered during ignite with "orienting" status
    try:
        agent = make_request("GET", f"/roster/{agent_id}", base_url, agent_id)
    except Exception as e:
        error_msg = str(e)
        if "404" in error_msg or "not found" in error_msg.lower():
            raise click.ClickException(
                f"Agent '{agent_id}' not found. You must run 'skein ignite' first to get your assigned name."
            )
        raise click.ClickException(f"Failed to verify agent: {error_msg}")

    current_status = agent.get("status", "unknown")
    if current_status != "orienting":
        if current_status == "active":
            raise click.ClickException(
                f"Agent '{agent_id}' is already active. No need to run 'ready' again."
            )
        elif current_status == "retired":
            raise click.ClickException(
                f"Agent '{agent_id}' has already retired. Start a new session with 'skein ignite'."
            )
        else:
            raise click.ClickException(
                f"Agent '{agent_id}' has status '{current_status}' - expected 'orienting'. "
                "Run 'skein ignite' to get a new agent assignment."
            )

    # Update agent status from orienting to active
    data = {
        "agent_id": agent_id,
        "name": agent_id,
        "status": "active",
        "metadata": {"ready_at": datetime.now().isoformat()},
    }

    try:
        make_request("POST", "/roster/register", base_url, agent_id, json=data)
    except Exception as e:
        raise click.ClickException(f"Failed to activate: {str(e)}")

    click.echo("=" * 60)
    click.echo("READY")
    click.echo("=" * 60)
    click.echo()
    click.echo(f"You are: {agent_id}")
    click.echo()
    click.echo("Use this for all commands:")
    click.echo(f'  skein --agent {agent_id} issue SITE "description"')
    click.echo(f'  skein --agent {agent_id} finding SITE "discovery"')
    click.echo(f"  skein --agent {agent_id} torch")
    click.echo()


@cli.command("torch")
@click.pass_context
def torch_start(ctx):
    """
    Begin retirement - Prepare to torch.

    Usage:
        skein torch

    After filing any remaining work:
        skein complete [--summary "..."]
    """
    _torch_start(ctx)


def _torch_start(ctx):
    """
    Begin retirement process - Prepare to torch.

    Usage:
        skein torch

    After filing any remaining work:
        skein complete [--summary "..."]
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    if agent_id is None:
        raise click.ClickException(
            "Must set SKEIN_AGENT_ID or use --agent flag to torch"
        )

    # Get roster info
    try:
        roster_data = make_request("GET", f"/roster/{agent_id}", base_url, agent_id)
        name = roster_data.get("name", agent_id)
    except Exception:
        raise click.ClickException(
            f"Agent {agent_id} not found in roster. Must ignite before torching."
        )

    # Get agent's SKEIN activity
    try:
        # Get all folios by this agent
        all_folios = make_request("GET", "/folios", base_url, agent_id)
        agent_folios = [
            f
            for f in all_folios
            if f.get("author") == agent_id or f.get("weaver") == agent_id
        ]

        # Count by type
        work_summary = {
            "issues": len([f for f in agent_folios if f.get("type") == "issue"]),
            "findings": len([f for f in agent_folios if f.get("type") == "finding"]),
            "plans": len([f for f in agent_folios if f.get("type") == "plan"]),
            "briefs": len([f for f in agent_folios if f.get("type") == "brief"]),
            "notions": len([f for f in agent_folios if f.get("type") == "notion"]),
            "frictions": len([f for f in agent_folios if f.get("type") == "friction"]),
            "summaries": len([f for f in agent_folios if f.get("type") == "summary"]),
        }
    except Exception:
        work_summary = {}

    # Update status to retiring (if server supports it)
    try:
        # Try to update via re-registration with new status
        update_data = {"agent_id": agent_id, "name": name, "status": "retiring"}
        make_request("POST", "/roster/register", base_url, agent_id, json=update_data)
    except Exception:
        pass  # Continue even if update fails (server might not support status)

    click.echo("=" * 60)
    click.echo("TORCH - Retirement Phase")
    click.echo("=" * 60)
    click.echo()
    click.echo(f"Name: {name}")
    click.echo()

    if work_summary:
        click.echo("Your SKEIN Activity:")
        for folio_type, count in work_summary.items():
            if count > 0:
                click.echo(f"  {folio_type}: {count}")
        click.echo()

    # Query agent's open work for visibility (work assigned TO them)
    open_issues = []
    open_frictions = []
    ignited_from_brief = None
    brief_is_open = False

    try:
        # Get assignment threads pointing to this agent
        all_threads = make_request("GET", "/threads", base_url, agent_id)
        assignment_threads = [
            t
            for t in all_threads
            if t.get("type") == "assignment" and t.get("to_id") == agent_id
        ]
        assigned_folio_ids = [t.get("from_id") for t in assignment_threads]

        if assigned_folio_ids:
            # Get all open issues and frictions
            open_issues_all = make_request(
                "GET",
                "/folios",
                base_url,
                agent_id,
                params={"type": "issue", "status": "open"},
            )
            open_frictions_all = make_request(
                "GET",
                "/folios",
                base_url,
                agent_id,
                params={"type": "friction", "status": "open"},
            )

            # Filter to only those assigned to this agent
            open_issues = [
                i for i in open_issues_all if i.get("folio_id") in assigned_folio_ids
            ]
            open_frictions = [
                f for f in open_frictions_all if f.get("folio_id") in assigned_folio_ids
            ]

        # Check if agent was ignited from a brief and if it's still open
        try:
            roster_entry = make_request(
                "GET", f"/roster/{agent_id}", base_url, agent_id
            )
            ignited_from = roster_entry.get("metadata", {}).get("ignited_from")
            if ignited_from and ignited_from.startswith("brief-"):
                # Get brief status
                all_briefs = make_request(
                    "GET", "/folios", base_url, agent_id, params={"type": "brief"}
                )
                brief = next(
                    (b for b in all_briefs if b.get("folio_id") == ignited_from), None
                )
                if brief and brief.get("status") == "open":
                    ignited_from_brief = brief
                    brief_is_open = True
        except Exception:
            pass
    except Exception:
        # Continue even if we can't fetch open work
        pass

    # Display open work if any exists
    if open_issues or open_frictions or brief_is_open:
        click.echo("=" * 60)
        click.echo("YOUR OPEN WORK")
        click.echo("=" * 60)
        click.echo()

        if open_issues:
            click.echo("Issues assigned to you:")
            for issue in open_issues[:5]:
                title = issue.get("title", "")[:50]
                click.echo(f"  • {issue['folio_id']} - {title}")
            if len(open_issues) > 5:
                click.echo(f"  ... and {len(open_issues) - 5} more")
            click.echo()

        if open_frictions:
            click.echo("Frictions assigned to you:")
            for friction in open_frictions[:5]:
                title = friction.get("title", "")[:50]
                click.echo(f"  • {friction['folio_id']} - {title}")
            if len(open_frictions) > 5:
                click.echo(f"  ... and {len(open_frictions) - 5} more")
            click.echo()

        if brief_is_open and ignited_from_brief:
            click.echo("Ignition brief:")
            click.echo(f"  • {ignited_from_brief['folio_id']} [OPEN]")
            click.echo()

    click.echo("Before completing retirement, consider:")
    click.echo()
    click.echo(
        "  • Is there incomplete work? File brief(s) if someone should continue."
    )
    click.echo(
        "  • Did you have larger ideas or patterns worth sharing? File notion(s)."
    )
    click.echo("  • Did you encounter friction or blockers? File friction(s).")
    click.echo("  • Do you know of completed work that should be closed? Close it.")
    click.echo()
    click.echo("Examples:")
    click.echo("  skein close issue-20251112-757o --link summary-20251112-5lut")
    click.echo(
        '  skein close friction-20251109-1lfe --note "Fixed by refactoring imports"'
    )
    click.echo()
    click.echo(
        "Note: Writing to SKEIN is optional but encouraged. Don't post just to post."
    )
    click.echo()
    click.echo("When done:")
    click.echo()
    click.echo("  skein complete")
    click.echo()


@cli.command("complete")
@click.option("--summary", help="Optional retirement summary")
@click.option(
    "--yield-status",
    "yield_status",
    type=click.Choice(["complete", "partial", "blocked"]),
    help="Yield status for chain (auto-detected from SKEIN_CHAIN_ID)",
)
@click.option(
    "--yield-outcome", "yield_outcome", help="What was accomplished (for yield)"
)
@click.option("--yield-notes", "yield_notes", help="Notes for next agent in chain")
@click.pass_context
def complete(ctx, summary, yield_status, yield_outcome, yield_notes):
    """
    Complete torch - Retire from roster.

    Usage:
        skein complete
        skein complete --summary "Completed auth audit. 3 issues filed."

    If SKEIN_CHAIN_ID is set, will prompt for yield sign-off:
        skein complete --yield-status complete --yield-outcome "Fixed the bug"
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    if agent_id is None:
        raise click.ClickException("Must set SKEIN_AGENT_ID or use --agent flag")

    # Check if we're in a chain (yield required)
    chain_id = os.environ.get("SKEIN_CHAIN_ID")
    task_id = os.environ.get("SKEIN_CHAIN_TASK")

    # Get roster info
    try:
        roster_data = make_request("GET", f"/roster/{agent_id}", base_url, agent_id)
        name = roster_data.get("name", agent_id)
    except Exception:
        raise click.ClickException(f"Agent {agent_id} not found in roster")

    # Get final work summary
    agent_folios = []
    try:
        all_folios = make_request("GET", "/folios", base_url, agent_id)
        agent_folios = [f for f in all_folios if f.get("created_by") == agent_id]

        final_work = {
            "issues": len([f for f in agent_folios if f.get("type") == "issue"]),
            "findings": len([f for f in agent_folios if f.get("type") == "finding"]),
            "plans": len([f for f in agent_folios if f.get("type") == "plan"]),
            "briefs": len([f for f in agent_folios if f.get("type") == "brief"]),
            "notions": len([f for f in agent_folios if f.get("type") == "notion"]),
            "frictions": len([f for f in agent_folios if f.get("type") == "friction"]),
            "summaries": len([f for f in agent_folios if f.get("type") == "summary"]),
            "tenders": len([f for f in agent_folios if f.get("type") == "tender"]),
        }
    except Exception:
        final_work = {}

    # If in a chain, handle yield
    yield_stored = False
    if chain_id:
        click.echo("=" * 60)
        click.echo("YIELD - Chain Data Package")
        click.echo("=" * 60)
        click.echo()
        click.echo(f"Chain: {chain_id}")
        if task_id:
            click.echo(f"Task: {task_id}")
        click.echo()

        # Show artifacts filed during session
        artifact_ids = [f.get("folio_id") for f in agent_folios if f.get("folio_id")]
        tender_ids = [
            f.get("folio_id") for f in agent_folios if f.get("type") == "tender"
        ]

        if artifact_ids:
            click.echo("Artifacts filed this session:")
            for folio in agent_folios[:10]:
                folio_id = folio.get("folio_id", "")
                folio_type = folio.get("type", "")
                title = folio.get("title", "")[:40]
                click.echo(f"  • {folio_id} ({folio_type}) - {title}")
            if len(agent_folios) > 10:
                click.echo(f"  ... and {len(agent_folios) - 10} more")
            click.echo()

        # Determine yield status
        if not yield_status:
            # Auto-detect: if tender exists, likely complete
            if tender_ids:
                yield_status = "complete"
            else:
                # Prompt for status
                click.echo("Yield status required. Options:")
                click.echo("  complete - Work finished successfully")
                click.echo("  partial  - Some work done, more needed")
                click.echo("  blocked  - Cannot proceed, needs intervention")
                click.echo()
                yield_status = click.prompt(
                    "Status",
                    type=click.Choice(["complete", "partial", "blocked"]),
                    default="complete",
                )

        # Get outcome if not provided
        if not yield_outcome:
            yield_outcome = click.prompt(
                "Outcome (what was accomplished)",
                default=f"Completed task. Filed {len(artifact_ids)} artifact(s).",
            )

        # Build yield package
        yield_data = {
            "chain_id": chain_id,
            "task_id": task_id or "unknown",
            "yield_data": {
                "status": yield_status,
                "outcome": yield_outcome,
                "artifacts": artifact_ids,
                "notes": yield_notes,
            },
        }

        # Add tender_id if we have one
        if tender_ids:
            yield_data["tender_id"] = tender_ids[0]  # Primary tender

        # Store the yield
        try:
            result = make_request(
                "POST", "/yields", base_url, agent_id, json=yield_data
            )
            sack_id = result.get("sack_id")
            click.echo(f"✓ Yield stored: {sack_id}")
            click.echo()
            yield_stored = True
        except Exception as e:
            click.echo(f"Warning: Could not store yield: {e}", err=True)
            click.echo()

    # Post summary if provided
    summary_id = None
    if summary:
        # Find a site to post to (use most recent site they posted to)
        try:
            recent_sites = list(
                set([f.get("site_id") for f in agent_folios if f.get("site_id")])
            )
            if recent_sites:
                site_id = recent_sites[-1]
                summary_data = {
                    "site": site_id,
                    "content": summary,
                    "metadata": {"retirement_summary": True},
                }
                result = make_request(
                    "POST", "/summary", base_url, agent_id, json=summary_data
                )
                summary_id = result.get("folio_id")
        except Exception:
            pass

    # Update status to retired
    try:
        update_data = {
            "status": "retired",
            "metadata": {
                "torched_at": datetime.now().isoformat(),
                "work_summary": final_work,
                "chain_id": chain_id,
                "yield_stored": yield_stored,
            },
        }
        make_request(
            "PATCH", f"/roster/{agent_id}", base_url, agent_id, json=update_data
        )
    except Exception as e:
        # Log but don't fail - agent can still complete even if status update fails
        click.echo(f"Warning: Could not update roster status: {e}", err=True)

    click.echo("=" * 60)
    click.echo("RETIRED")
    click.echo("=" * 60)
    click.echo()
    click.echo(f"✓ Retired: {name}")
    click.echo()

    if final_work:
        click.echo("Final Work Summary:")
        for folio_type, count in final_work.items():
            if count > 0:
                click.echo(f"  {folio_type}: {count}")
        click.echo()

    if summary_id:
        click.echo(f"✓ Summary posted: {summary_id}")
        click.echo()

    click.echo("Thank you for your service. 🔥")
    click.echo()


@cli.command()
@click.argument("agent_id")
@click.option(
    "--capabilities", multiple=True, help="Agent capabilities (can specify multiple)"
)
@click.option("--name", help="Human-readable name")
@click.option(
    "--type",
    "agent_type",
    type=click.Choice(["claude-code", "patbot", "horizon", "human", "system"]),
    help="Agent type",
)
@click.option("--description", help="Longer description")
@click.option("--eval", is_flag=True, help="Output eval-able export command")
@click.pass_context
def identify(ctx, agent_id, capabilities, name, agent_type, description, eval):
    """
    Set your agent identity for this shell session.

    Usage:
      eval $(skein identify agent-007 --eval)

    Or manually:
      export SKEIN_AGENT_ID=agent-007

    Example: skein identify agent-007 --type claude-code --name "Security Auditor"
    """
    base_url = get_base_url(ctx.obj.get("url"))

    if eval:
        # Just output the export command for eval
        click.echo(f"export SKEIN_AGENT_ID={agent_id}")
        return

    # Interactive mode - register if capabilities/name/type/description provided
    click.echo(f"To identify as {agent_id}, run:")
    click.echo(f"  export SKEIN_AGENT_ID={agent_id}")
    click.echo()

    if capabilities or name or agent_type or description:
        reg_data = {
            "agent_id": agent_id,
            "capabilities": list(capabilities) if capabilities else [],
            "metadata": {},
        }
        if name:
            reg_data["name"] = name
        if agent_type:
            reg_data["agent_type"] = agent_type
        if description:
            reg_data["description"] = description

        try:
            reg_result = make_request(
                "POST", "/roster/register", base_url, agent_id, json=reg_data
            )
            if reg_result.get("success"):
                if name:
                    click.echo(f"✓ Registered as: {name}")
                if agent_type:
                    click.echo(f"  Type: {agent_type}")
                if capabilities:
                    click.echo(f"  Capabilities: {', '.join(capabilities)}")
        except Exception as e:
            click.echo(f"Warning: Registration failed: {e}", err=True)


@cli.command("stats")
@click.argument("target", type=click.Choice(["threads", "folios"]))
@click.option("--orphaned", is_flag=True, help="Show orphaned threads (threads only)")
@click.option("--by-weaver", is_flag=True, help="Group by weaver (threads only)")
@click.option("--by-type", is_flag=True, help="Group by type")
@click.option("--by-status", is_flag=True, help="Group by status (folios only)")
@click.option("--by-site", is_flag=True, help="Group by site (folios only)")
@click.option("--all", "show_all", is_flag=True, help="Show all stats")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def stats(
    ctx, target, orphaned, by_weaver, by_type, by_status, by_site, show_all, output_json
):
    """Observability and debugging analytics.

    Examples:
        skein stats threads --orphaned
        skein stats threads --by-weaver
        skein stats threads --all
        skein stats folios
        skein stats folios --by-type
        skein stats folios --by-status
        skein stats folios --by-site
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    if target == "threads":
        analyze_threads(
            base_url, agent_id, orphaned, by_weaver, by_type, show_all, output_json
        )
    elif target == "folios":
        analyze_folios(
            base_url, agent_id, by_type, by_status, by_site, show_all, output_json
        )


def analyze_threads(
    base_url, agent_id, orphaned, by_weaver, by_type, show_all, output_json
):
    """Analyze thread statistics."""
    from .analytics import (
        find_orphaned_threads,
        analyze_by_weaver as analyze_threads_by_weaver,
        analyze_by_type as analyze_threads_by_type,
        print_orphaned_threads,
        print_weaver_stats,
        print_type_distribution,
    )

    # If no options specified, show all by default
    if not (orphaned or by_weaver or by_type or show_all):
        show_all = True

    # Fetch all threads once with error handling
    try:
        threads = make_request("GET", "/threads", base_url, agent_id)
        if not isinstance(threads, list):
            threads = []
    except Exception as e:
        raise click.ClickException(f"Failed to fetch threads: {str(e)}")

    if output_json:
        # Return structured data
        results = {}
        if orphaned or show_all:
            try:
                folios = make_request("GET", "/folios", base_url, agent_id)
                if not isinstance(folios, list):
                    folios = []
            except Exception as e:
                raise click.ClickException(f"Failed to fetch folios: {str(e)}")
            results["orphaned"] = find_orphaned_threads(threads, folios)
        if by_weaver or show_all:
            results["by_weaver"] = analyze_threads_by_weaver(threads)
        if by_type or show_all:
            results["by_type"] = analyze_threads_by_type(threads)
        click.echo(json.dumps(results, indent=2))
        return

    # Pretty print output
    if orphaned or show_all:
        try:
            folios = make_request("GET", "/folios", base_url, agent_id)
            if not isinstance(folios, list):
                folios = []
        except Exception as e:
            raise click.ClickException(f"Failed to fetch folios: {str(e)}")
        print_orphaned_threads(threads, folios)
        if show_all:
            click.echo()

    if by_weaver or show_all:
        print_weaver_stats(threads)
        if show_all:
            click.echo()

    if by_type or show_all:
        print_type_distribution(threads)


def analyze_folios(
    base_url, agent_id, by_type, by_status, by_site, show_all, output_json
):
    """Analyze folio statistics."""
    from .analytics import get_folio_stats, print_folio_stats

    # If no options specified, show all by default
    if not (by_type or by_status or by_site or show_all):
        show_all = True

    # Fetch all folios with error handling
    try:
        folios = make_request("GET", "/folios", base_url, agent_id)
        if not isinstance(folios, list):
            folios = []
    except Exception as e:
        raise click.ClickException(f"Failed to fetch folios: {str(e)}")

    if output_json:
        stats = get_folio_stats(folios)
        # Filter based on options
        if not show_all:
            filtered_stats = {"total": stats["total"]}
            if by_type:
                filtered_stats["by_type"] = stats["by_type"]
            if by_status:
                filtered_stats["by_status"] = stats["by_status"]
            if by_site:
                filtered_stats["by_site"] = stats["by_site"]
            stats = filtered_stats
        click.echo(json.dumps(stats, indent=2))
        return

    # Pretty print
    print_folio_stats(
        folios,
        by_type=by_type or show_all,
        by_status=by_status or show_all,
        by_site=by_site or show_all,
    )


@cli.command()
@click.pass_context
def whoami(ctx):
    """Show current agent identity."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    click.echo(f"Agent ID: {agent_id}")
    click.echo(f"Server: {base_url}/skein")


@cli.command()
@click.argument(
    "topic", type=click.Choice(["quickstart", "guide", "threads", "implementation"])
)
@click.pass_context
def info(ctx, topic):
    """Display SKEIN documentation.

    Available topics:
        quickstart      - Quick start guide for SKEIN
        guide           - Comprehensive SKEIN agent guide
        threads         - Conceptual overview of threads system
        implementation  - Architecture and implementation details

    Examples:
        skein info quickstart
        skein info guide
        skein info threads
    """
    from pathlib import Path

    # Find docs directory
    # Docs are in ~/projects/skein/docs/
    current_file = Path(__file__)
    project_root = current_file.parent.parent  # client/cli.py -> skein root
    docs_dir = project_root / "docs"

    doc_map = {
        "quickstart": docs_dir / "SKEIN_QUICK_START.md",
        "guide": docs_dir / "SKEIN_AGENT_GUIDE.md",
        "threads": docs_dir / "THREADS_PHILOSOPHY.md",
        "implementation": docs_dir / "ARCHITECTURE.md",
    }

    doc_file = doc_map.get(topic)

    if not doc_file or not doc_file.exists():
        click.echo(f"Documentation file not found: {doc_file}")
        click.echo(f"Expected location: {doc_file}")
        return

    with open(doc_file, "r") as f:
        content = f.read()
        click.echo(content)


# ============================================================================
# BACKUP Commands - Backup and Recovery
# ============================================================================


@cli.group()
def backup():
    """Backup and recovery commands for SKEIN data."""
    pass


@backup.command("create")
@click.option("--tag", help="Tag to identify this backup (e.g., 'pre-migration')")
@click.option(
    "--data-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Back up a specific .skein/data directory only (skip multi-project discovery)",
)
@click.option(
    "--projects-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Override the projects root for discovery (default: ~/projects)",
)
@click.pass_context
def backup_create(ctx, tag, data_dir, projects_root):
    """Create a full backup of SKEIN data.

    By default, discovers every ~/projects/*/.skein/data directory with a
    non-empty skein.db and produces a per-project tar.gz under
    ~/.skein/backups/full. Pass --data-dir to back up a single dir explicitly.

    Examples:
        skein backup create
        skein backup create --tag pre-migration
        skein backup create --data-dir ~/projects/speakbot/.skein/data
    """
    from .backup import BackupManager

    if data_dir:
        manager = BackupManager(data_dir=data_dir)
        try:
            result = manager.create_full_backup(tag=tag)
        except Exception as e:
            raise click.ClickException(f"Backup failed: {e}")
        click.echo(f"Backup created: {result['backup_name']}")
        click.echo(f"  Project: {result.get('project') or '<unspecified>'}")
        click.echo(f"  Location: {result['backup_path']}")
        click.echo(f"  Checksum: {result['checksum'][:16]}...")
        click.echo(f"  Size: {result['backup_size']:,} bytes")
        stats = result["source_stats"]
        click.echo(f"  Files: {stats['total_files']}")
        return

    manager = BackupManager()
    summary = manager.create_full_backup_all_projects(
        projects_root=projects_root, tag=tag
    )

    if summary["discovered"] == 0:
        click.echo("No SKEIN project data dirs discovered.")
        return

    click.echo(
        f"Discovered {summary['discovered']} project(s); "
        f"backed up {summary['succeeded']}, failed {summary['failed']}.\n"
    )
    for r in summary["projects"]:
        click.echo(f"✓ {r['project']}  {r['backup_name']}")
        click.echo(f"    Size:     {r['backup_size']:,} bytes")
        click.echo(f"    Checksum: {r['checksum'][:16]}...")
        click.echo(f"    Files:    {r['source_stats']['total_files']}")
    if summary["errors"]:
        click.echo("")
        for err in summary["errors"]:
            click.echo(f"✗ {err['project']}: {err['error']}")
        # Treat any per-project failure as a non-zero exit so the systemd
        # service surfaces partial-failure runs in journalctl.
        ctx.exit(1)


@backup.command("list")
@click.option("--full", "backup_type", flag_value="full", help="Show only full backups")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.option("--project", help="Filter by project name")
@click.pass_context
def backup_list(ctx, backup_type, output_json, project):
    """List available backups.

    Examples:
        skein backup list
        skein backup list --full
        skein backup list --json
        skein backup list --project speakbot
    """
    from .backup import BackupManager
    import json

    manager = BackupManager()

    backups = manager.list_backups(backup_type=backup_type or "all")
    if project:
        backups = [b for b in backups if b.get("project") == project]

    if output_json:
        click.echo(json.dumps(backups, indent=2, default=str))
        return

    if not backups:
        click.echo("No backups found.")
        return

    click.echo(f"Found {len(backups)} backup(s):\n")
    for backup in backups:
        name = backup.get("backup_name", "unknown")
        timestamp = backup.get("timestamp", "unknown")
        size = backup.get("backup_size", 0)
        tag = backup.get("tag", "")
        proj = backup.get("project", "")
        exists = "✓" if backup.get("exists", False) else "✗"

        click.echo(f"{exists} {name}")
        if proj:
            click.echo(f"    Project: {proj}")
        click.echo(f"    Time: {timestamp}")
        click.echo(f"    Size: {size:,} bytes")
        if tag:
            click.echo(f"    Tag: {tag}")
        click.echo()


@backup.command("verify")
@click.argument("backup_id")
@click.pass_context
def backup_verify(ctx, backup_id):
    """Verify backup integrity.

    Examples:
        skein backup verify skein_full_2025-11-15_00-00-00
    """
    from .backup import BackupManager

    manager = BackupManager()

    result = manager.verify_backup(backup_id)

    if result["valid"]:
        click.echo("✓ Backup is valid")
        click.echo(f"  Checksum: {result['checksum'][:16]}...")
        click.echo(f"  Files: {result['file_count']}")
        click.echo(f"  Size: {result['backup_size']:,} bytes")
    else:
        click.echo("✗ Backup verification failed")
        click.echo(f"  Error: {result.get('error', 'Unknown error')}")


@backup.command("cleanup")
@click.option(
    "--keep-last",
    type=int,
    help="Keep only the N most recent backups per project",
)
@click.option(
    "--older-than", "older_than_days", type=int, help="Remove backups older than N days"
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be removed without removing"
)
@click.pass_context
def backup_cleanup(ctx, keep_last, older_than_days, dry_run):
    """Remove old backups based on retention policy.

    With per-project backups, --keep-last rotates per project — each project
    keeps its own last-N history. --older-than is global by absolute timestamp.

    Examples:
        skein backup cleanup --keep-last 10
        skein backup cleanup --older-than 30
        skein backup cleanup --keep-last 5 --dry-run
    """
    from .backup import BackupManager

    manager = BackupManager()

    if not keep_last and not older_than_days:
        raise click.ClickException("Must specify --keep-last or --older-than")

    result = manager.cleanup_old_backups(
        keep_last=keep_last, older_than_days=older_than_days, dry_run=dry_run
    )

    if dry_run:
        removed = result.get("would_remove", [])
        if removed:
            click.echo(f"Would remove {len(removed)} backup(s):")
            for name in removed:
                click.echo(f"  - {name}")
        else:
            click.echo("No backups would be removed.")
    else:
        removed = result.get("removed", [])
        if removed:
            click.echo(f"Removed {len(removed)} backup(s):")
            for name in removed:
                click.echo(f"  - {name}")
        else:
            click.echo("No backups removed.")

    keeping = result.get("keeping", [])
    if keeping:
        click.echo(f"\nKeeping {len(keeping)} backup(s)")


@backup.command("enable")
@click.option(
    "--keep-last", type=int, default=30, help="Number of backups to keep (default: 30)"
)
@click.pass_context
def backup_enable(ctx, keep_last):
    """Enable automated daily backups via systemd timer.

    Installs and starts a user systemd timer that runs daily backups
    with automatic cleanup. Backups discover all per-project SKEIN data
    dirs at run time — no project context needed for the unit itself.

    Examples:
        skein backup enable
        skein backup enable --keep-last 14
    """
    import sys
    import subprocess
    from pathlib import Path

    # Find template files (in skein package's systemd dir)
    skein_pkg = Path(__file__).parent.parent
    service_template = skein_pkg / "systemd" / "skein-backup.service.template"
    timer_template = skein_pkg / "systemd" / "skein-backup.timer.template"

    if not service_template.exists() or not timer_template.exists():
        raise click.ClickException(
            f"Template files not found in {skein_pkg / 'systemd'}\n"
            "Run from a proper SKEIN installation."
        )

    # Determine paths
    python_bin = sys.executable
    python_bin_dir = str(Path(python_bin).parent)
    # The skein source repo is the parent of the `client/` package — needed as
    # the systemd service's WorkingDirectory so `python -m client.cli` resolves.
    skein_src_dir = str(skein_pkg)

    # Read and substitute templates
    service_content = service_template.read_text()
    service_content = service_content.replace("__PYTHON__", python_bin)
    service_content = service_content.replace("__PYTHON_BIN_DIR__", python_bin_dir)
    service_content = service_content.replace("__SKEIN_SRC__", skein_src_dir)
    service_content = service_content.replace("__KEEP_LAST__", str(keep_last))

    timer_content = timer_template.read_text()

    # Install to user systemd directory
    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True, exist_ok=True)

    service_path = systemd_dir / "skein-backup.service"
    timer_path = systemd_dir / "skein-backup.timer"

    service_path.write_text(service_content)
    timer_path.write_text(timer_content)

    click.echo("Installed systemd files:")
    click.echo(f"  {service_path}")
    click.echo(f"  {timer_path}")

    # Reload and enable
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(
            ["systemctl", "--user", "enable", "skein-backup.timer"], check=True
        )
        subprocess.run(
            ["systemctl", "--user", "start", "skein-backup.timer"], check=True
        )
        click.echo("\n✓ Backup timer enabled and started")
        click.echo("  Scope: all ~/projects/*/.skein/data (multi-project discovery)")
        click.echo(f"  Retention: keep last {keep_last} backups per project")
        click.echo("\nCheck status with: skein backup status")
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"Failed to enable timer: {e}")
    except FileNotFoundError:
        raise click.ClickException("systemctl not found. Is systemd available?")


@backup.command("disable")
@click.pass_context
def backup_disable(ctx):
    """Disable automated backups.

    Stops and disables the systemd backup timer.

    Examples:
        skein backup disable
    """
    import subprocess

    try:
        subprocess.run(
            ["systemctl", "--user", "stop", "skein-backup.timer"], check=True
        )
        subprocess.run(
            ["systemctl", "--user", "disable", "skein-backup.timer"], check=True
        )
        click.echo("✓ Backup timer disabled")
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"Failed to disable timer: {e}")
    except FileNotFoundError:
        raise click.ClickException("systemctl not found. Is systemd available?")


@backup.command("status")
@click.pass_context
def backup_status(ctx):
    """Show backup timer status.

    Displays whether automated backups are enabled and when the next
    backup is scheduled.

    Examples:
        skein backup status
    """
    import subprocess

    try:
        # Check timer status
        result = subprocess.run(
            ["systemctl", "--user", "status", "skein-backup.timer"],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            click.echo("Backup timer status:\n")
            click.echo(result.stdout)
        elif result.returncode == 3:
            # Service exists but is stopped/inactive
            click.echo("Backup timer is installed but not running.\n")
            click.echo(result.stdout)
            click.echo("\nTo enable: skein backup enable")
        elif result.returncode == 4:
            click.echo("Backup timer is not installed.")
            click.echo("\nTo enable: skein backup enable")
        else:
            click.echo(result.stdout)
            if result.stderr:
                click.echo(result.stderr)

        # Show next scheduled run if timer is active
        list_result = subprocess.run(
            ["systemctl", "--user", "list-timers", "skein-backup.timer"],
            capture_output=True,
            text=True,
        )
        if list_result.returncode == 0 and "skein-backup" in list_result.stdout:
            click.echo("\nSchedule:")
            click.echo(list_result.stdout)

    except FileNotFoundError:
        raise click.ClickException("systemctl not found. Is systemd available?")


@cli.command("restore")
@click.argument("backup_id")
@click.option(
    "--dry-run", is_flag=True, help="Show what would be restored without making changes"
)
@click.option(
    "--confirm", is_flag=True, help="Confirm restore (required for actual restore)"
)
@click.option(
    "--destination",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help=(
        "Restore to this directory instead of the current project's data dir. "
        "Required when running outside a SKEIN project, unless the backup's "
        "metadata records a source_dir to fall back to."
    ),
)
@click.pass_context
def restore(ctx, backup_id, dry_run, confirm, destination):
    """Restore SKEIN data from a backup.

    WARNING: This will overwrite the destination data. A pre-restore backup is
    created automatically when the destination already contains data.

    Examples:
        skein restore skein_full_2025-11-15_00-00-00 --dry-run
        skein restore skein_full_2025-11-15_00-00-00 --confirm
        skein restore latest --confirm
        skein restore skein_full_speakbot_2026-04-25_00-00-00 \\
            --destination /tmp/restore-test --confirm
    """
    from .backup import BackupManager, get_backup_manager_for_project

    # Use a project-scoped manager when we're inside a project (so default
    # destination is the current project's data dir); otherwise fall back to a
    # bare manager that can still operate on the shared backup dir.
    manager = get_backup_manager_for_project()
    if manager is None:
        manager = BackupManager()

    # Handle 'latest' as special case
    if backup_id == "latest":
        backups = manager.list_backups()
        if not backups:
            raise click.ClickException("No backups found")
        backup_id = backups[0]["backup_name"].replace(".tar.gz", "")

    result = manager.restore_backup(
        backup_id, dry_run=dry_run, confirm=confirm, destination=destination
    )

    if dry_run:
        if result["success"]:
            info = result["would_restore"]
            click.echo("Would restore:")
            click.echo(f"  Files: {info['files']}")
            click.echo(f"  To: {info['to_directory']}")
            stats = info.get("source_stats", {})
            if stats:
                click.echo(f"  Original size: {stats.get('total_size', 0):,} bytes")
            click.echo("\nSample files:")
            for member in info.get("members", [])[:10]:
                click.echo(f"    {member}")
            if len(info.get("members", [])) > 10:
                click.echo(f"    ... and {info['files'] - 10} more")
        else:
            click.echo(f"Error: {result.get('error')}")
    elif result["success"]:
        click.echo(f"✓ Restored from: {result['restored_from']}")
        click.echo(f"  To: {result['restored_to']}")
        click.echo(f"  Files restored: {result['files_restored']}")
        if result.get("pre_restore_backup"):
            click.echo(f"  Pre-restore backup: {result['pre_restore_backup']}")
    else:
        click.echo(f"✗ Restore failed: {result.get('error')}")
        if result.get("pre_restore_backup"):
            click.echo(
                f"  Pre-restore backup available: {result['pre_restore_backup']}"
            )


# ============================================================================
# SHARD Commands - Git Worktree Management
# ============================================================================


def get_shard_worktree_module():
    """
    Import shard module from SKEIN package.

    SHARD functionality is part of SKEIN infrastructure - it operates on
    whatever project you're currently in.
    """
    try:
        from skein import shard

        return shard
    except ImportError as e:
        raise click.ClickException(
            f"Failed to import SHARD module: {e}\n"
            f"SHARD is part of SKEIN infrastructure. If you're seeing this, "
            f"the SKEIN installation may be incomplete."
        )


@cli.group()
@click.option(
    "--project",
    "project_path",
    help="Path to project (default: SKEIN_PROJECT env or current directory)",
)
@click.pass_context
def shard(ctx, project_path):
    """SHARD agent coordination - worktree management for parallel agent work."""
    ctx.ensure_object(dict)
    # Check for project path: --project flag takes priority, then SKEIN_PROJECT env var
    effective_project = project_path or os.environ.get("SKEIN_PROJECT")
    if effective_project:
        # Override the project root before any shard operations
        shard_worktree = get_shard_worktree_module()
        try:
            shard_worktree.set_project_root(effective_project)
        except shard_worktree.ShardError as e:
            raise click.ClickException(str(e))


@shard.command("spawn")
@click.option("--agent", "spawn_agent", required=True, help="Agent ID for this SHARD")
@click.option("--brief", help="Brief ID this SHARD relates to")
@click.option("--description", help="Work description")
@click.option(
    "--base",
    "base_branch",
    default=None,
    help="Branch to fork from (default: auto-detected from repo)",
)
@click.pass_context
def shard_spawn(ctx, spawn_agent, brief, description, base_branch):
    """
    Spawn a new SHARD: create git branch + worktree for isolated agent work.

    Example:
        skein shard spawn --agent opus-security-architect --brief brief-123 --description "Bash security"
        skein shard spawn --agent opus --base feature-branch
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    # Import shard_worktree from current project
    shard_worktree = get_shard_worktree_module()

    try:
        # Detect default branch if not explicitly provided
        if base_branch is None:
            from skein import shard as shard_module

            base_branch = shard_module._detect_default_branch()

        # Create worktree
        shard_info = shard_worktree.spawn_shard(
            name=spawn_agent,
            brief_id=brief,
            description=description,
            base_branch=base_branch,
        )

        # Create SKEIN thread to track this SHARD
        # Use "tag" type with SHARD metadata in content
        thread_content = json.dumps(
            {
                "tag": "shard",
                "shard_id": shard_info["shard_id"],
                "worktree_name": shard_info["worktree_name"],
                "worktree_path": shard_info["worktree_path"],
                "branch_name": shard_info["branch_name"],
                "status": "spawned",
                "description": description or "",
            }
        )

        # Thread from agent to brief (if provided) or agent to self
        thread_data = {
            "from_id": spawn_agent,
            "to_id": brief if brief else spawn_agent,
            "type": "tag",
            "content": thread_content,
        }

        try:
            thread_result = make_request(
                "POST", "/threads", base_url, agent_id, json=thread_data
            )
            shard_info["thread_id"] = thread_result.get("thread_id")
        except Exception as e:
            # Don't fail spawn if thread creation fails
            click.echo(f"Warning: Failed to create SKEIN thread: {e}", err=True)

        click.echo(f"✓ Spawned SHARD: {shard_info['shard_id']}")
        click.echo(f"  Name: {shard_info['name']}")
        click.echo(f"  Branch: {shard_info['branch_name']}")
        click.echo(f"  Worktree: {shard_info['worktree_path']}")
        if shard_info.get("brief_id"):
            click.echo(f"  Brief: {shard_info['brief_id']}")
        if shard_info.get("thread_id"):
            click.echo(f"  Thread: {shard_info['thread_id']}")
        click.echo("\nTo work in this SHARD:")
        click.echo(f"  cd {shard_info['worktree_path']}")

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to spawn SHARD: {e}")


@shard.command("list")
@click.option("--active", is_flag=True, help="Show only active SHARDs")
@click.option("--agent", "filter_agent", help="Filter by shard name")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def shard_list(ctx, active, filter_agent, output_json):
    """
    List SHARD worktrees.

    Example:
        skein shard list
        skein shard list --agent opus-security-architect
    """
    # Import shard_worktree from current project
    shard_worktree = get_shard_worktree_module()

    try:
        shards = shard_worktree.list_shards(active_only=active)

        # Filter by agent if requested
        if filter_agent:
            shards = [s for s in shards if s["name"] == filter_agent]

        if output_json:
            import json

            click.echo(json.dumps(shards, indent=2))
        else:
            if not shards:
                click.echo("No SHARDs found")
            else:
                for shard_item in shards:
                    click.echo(shard_item["worktree_name"])

            click.echo()
            click.echo(
                "Tip: Use `skein shard triage` for actionable overview with status and tender info"
            )
            return

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to list SHARDs: {e}")


@shard.command("show")
@click.argument("worktree_name")
@click.pass_context
def shard_show(ctx, worktree_name):
    """
    Show details of a specific SHARD.

    Example:
        skein shard show opus-security-architect-20251109-001
    """
    # Import shard_worktree from current project
    shard_worktree = get_shard_worktree_module()

    try:
        shard = shard_worktree.get_shard_status(worktree_name)

        if not shard:
            raise click.ClickException(f"SHARD not found: {worktree_name}")

        git_info = shard_worktree.get_shard_git_info(worktree_name)

        # Header: name (branch)
        click.echo(f"{shard['worktree_name']} ({shard['branch_name']})")
        click.echo()

        # Uncommitted changes first (if any)
        uncommitted = git_info.get("uncommitted", [])
        if uncommitted:
            click.echo("Uncommitted:")
            for line in uncommitted:
                click.echo(f" {line}")
            click.echo()

        # Commit log and diffstat
        commit_log = git_info.get("commit_log", [])
        if commit_log:
            for sha, msg in commit_log:
                click.echo(f"{sha} {msg}")
            click.echo()

            # Diffstat
            diffstat = git_info.get("diffstat", "")
            if diffstat:
                click.echo(diffstat)
                click.echo()
        else:
            # No unique commits - show tip info
            tip_sha = git_info.get("tip_sha", "")
            tip_msg = git_info.get("tip_message", "")
            tip_in_master = git_info.get("tip_in_master", False)
            commits_behind = git_info.get("commits_behind", 0)

            if tip_sha:
                click.echo(f"Tip: {tip_sha} {tip_msg}")
                if tip_in_master:
                    if commits_behind > 0:
                        click.echo(
                            f"     (in master, {commits_behind} commits behind HEAD)"
                        )
                    else:
                        click.echo("     (in master)")
                click.echo()
            click.echo("No unique commits.")
            click.echo()

        # Status line
        status_parts = []
        working = git_info.get("working_tree", "unknown")
        if working == "clean" and not uncommitted:
            status_parts.append("Working tree clean")

        merge = git_info.get("merge_status", "unknown")
        if merge == "conflict":
            status_parts.append("Has conflicts")
        elif merge == "clean" and commit_log:
            status_parts.append("Merges clean")

        if status_parts:
            click.echo(", ".join(status_parts))

        # Hint about diff command when there are conflicts
        if merge == "conflict":
            click.echo(f"\nTo see changes: skein shard diff {worktree_name}")

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to show SHARD: {e}")


@shard.command("diff")
@click.argument("worktree_name")
@click.option("--stat", "show_stat", is_flag=True, help="Show diffstat only")
@click.option(
    "--integration",
    is_flag=True,
    help="Show full integration diff with base branch (includes base branch evolution)",
)
@click.pass_context
def shard_diff(ctx, worktree_name, show_stat, integration):
    """
    Show diff for a SHARD.

    By default shows WORK DIFF: agent's actual changes from the base commit.
    This excludes any changes from base branch evolution (no false deletions).

    Use --integration to see what would actually merge into the current base branch.

    Examples:
        skein shard diff my-shard-001          # Work diff (agent's changes)
        skein shard diff my-shard-001 --stat   # Work diff stats only
        skein shard diff my-shard-001 --integration  # Full merge preview
    """
    shard_worktree = get_shard_worktree_module()

    try:
        shard = shard_worktree.get_shard_status(worktree_name)

        if not shard:
            raise click.ClickException(f"SHARD not found: {worktree_name}")

        from skein import shard as shard_module

        base_branch = shard_module._get_shard_base_branch(worktree_name)

        if integration:
            # Show integration diff (what would merge into current base branch)
            click.echo(f"=== INTEGRATION DIFF: {worktree_name} ===\n")
            click.echo(f"Changes relative to current {base_branch}:\n")
            diff_output = shard_worktree.get_shard_diff(
                worktree_name, stat_only=show_stat, integration=True
            )
            if diff_output:
                click.echo(diff_output)
            else:
                click.echo(f"No changes from current {base_branch}.")
        else:
            # Show work diff (agent's actual changes from base)
            drift_info = shard_worktree.get_shard_drift_info(worktree_name)

            if drift_info.get("has_metadata") and drift_info.get("base_commit"):
                click.echo(f"=== WORK DIFF: {worktree_name} ===\n")
                base_short = drift_info.get("base_commit_short", "unknown")
                base_date = drift_info.get("base_commit_date", "")
                click.echo(f"Changes from base commit {base_short}")
                if base_date:
                    click.echo(f"(Base created: {base_date})\n")

                # Show base branch activity if there's drift
                master_ahead = drift_info.get("base_commits_ahead", 0)
                if master_ahead > 0:
                    click.echo(
                        f"Note: {base_branch} has {master_ahead} new commits since your base."
                    )
                    notable = drift_info.get("base_notable_changes", [])
                    if notable:
                        click.echo(f"Notable changes on {base_branch}:")
                        for change in notable[:5]:
                            click.echo(f"  - {change}")
                    click.echo()

                diff_output = shard_worktree.get_shard_work_diff(
                    worktree_name, stat_only=show_stat
                )
            else:
                # No metadata - fall back to regular diff
                click.echo(f"=== DIFF: {worktree_name} ===\n")
                click.echo(
                    f"(No base commit metadata - showing diff from current {base_branch})\n"
                )
                diff_output = shard_worktree.get_shard_diff(
                    worktree_name, stat_only=show_stat
                )

            if diff_output:
                click.echo(diff_output)
            else:
                click.echo("No changes.")

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to get diff: {e}")


@shard.command("cleanup")
@click.argument("worktree_name")
@click.option(
    "--keep-branch", is_flag=True, help="Keep git branch after removing worktree"
)
@click.option(
    "--chain", is_flag=True, help="Remove entire graft chain (original + all grafts)"
)
@click.option(
    "--caller-cwd",
    "explicit_caller_cwd",
    default=None,
    help="Original working directory of caller (for orchestration tools)",
)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    help="Skip confirmation prompt",
)
@click.pass_context
def shard_cleanup(
    ctx, worktree_name, keep_branch, chain, explicit_caller_cwd, assume_yes
):
    """
    Remove SHARD worktree and optionally delete branch.

    Use --chain to remove an entire graft chain (original + all grafts).

    Example:
        skein shard cleanup my-shard-001
        skein shard cleanup my-shard-001 --keep-branch
        skein shard cleanup my-shard-001 --chain   # Remove full graft lineage

    For orchestration tools (e.g., Spindle), pass --caller-cwd to prevent
    agents from deleting their own worktree after cd-ing elsewhere.
    """
    import os

    # Import shard_worktree from current project
    shard_worktree = get_shard_worktree_module()

    # Use explicit caller_cwd if provided (from orchestration tools),
    # otherwise fall back to current working directory
    caller_cwd = explicit_caller_cwd if explicit_caller_cwd else os.getcwd()

    try:
        if not assume_yes:
            click.confirm("Are you sure you want to cleanup this SHARD?", abort=True)

        if chain:
            # Clean up entire graft chain
            result = shard_worktree.cleanup_graft_chain(
                worktree_name, keep_branch=keep_branch, caller_cwd=caller_cwd
            )

            click.echo(f"Tracing worktree chain for: {worktree_name}\n")

            if result["removed"]:
                click.echo(f"Found chain ({len(result['removed'])} worktrees):")
                for wt in reversed(result["removed"]):  # Show original first
                    wt == result.get("chain_root", "")
                    label = (
                        "(original)" if not shard_worktree.is_graft(wt) else "(graft)"
                    )
                    click.echo(f"  {wt} {label}")
                click.echo()

                click.echo(f"Removed {len(result['removed'])} worktrees:")
                for wt in result["removed"]:
                    click.echo(f"  ✓ {wt}")

            if result["errors"]:
                click.echo("\nWarnings:")
                for err in result["errors"]:
                    click.echo(f"  ⚠ {err}", err=True)

            if not keep_branch:
                click.echo("\n  (Branches also deleted)")
        else:
            # Single worktree cleanup
            shard_worktree.cleanup_shard(
                worktree_name, keep_branch=keep_branch, caller_cwd=caller_cwd
            )

            click.echo(f"✓ Cleaned up SHARD: {worktree_name}")
            if not keep_branch:
                click.echo("  (Branch also deleted)")
            else:
                click.echo("  (Branch kept)")

            # If this was a graft, suggest chain cleanup for the original
            if shard_worktree.is_graft(worktree_name):
                root = shard_worktree.get_graft_chain_root(worktree_name)
                remaining_chain = shard_worktree.get_graft_chain(root)
                if remaining_chain:
                    click.echo("\nNote: Original shard and/or other grafts may remain:")
                    for wt in remaining_chain:
                        click.echo(f"  - {wt}")
                    click.echo(f"\nTo remove all: skein shard cleanup {root} --chain")

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except click.Abort:
        raise
    except Exception as e:
        raise click.ClickException(f"Failed to cleanup SHARD: {e}")


@shard.command("graft")
@click.argument("worktree_name")
@click.pass_context
def shard_graft(ctx, worktree_name):
    """
    Create a graft worktree to resolve conflicts with the base branch.

    When a shard has conflicts with its base branch (due to branch evolution),
    grafting creates a new worktree from the current base branch and cherry-picks
    the shard's commits onto it.

    If conflicts occur during cherry-pick, the graft is left in a conflicted
    state for you to resolve manually.

    Example:
        skein shard graft my-shard-001

    After resolving conflicts (if any):
        cd worktrees/my-shard-001-graft/
        git add <resolved files>
        git commit
        skein shard merge my-shard-001-graft

    Grafts can be grafted - if the base branch evolves again before you merge,
    just run graft again on the graft:
        skein shard graft my-shard-001-graft
    """
    shard_worktree = get_shard_worktree_module()

    try:
        from skein import shard as shard_module

        base_branch = shard_module._get_shard_base_branch(worktree_name)
        click.echo(f"Creating graft worktree from current {base_branch}...")
        click.echo(f"Applying commits from {worktree_name}...")
        click.echo()

        result = shard_worktree.graft_shard(worktree_name)

        if result["success"]:
            click.echo("✓ Applied cleanly (no conflicts)\n")
            click.echo("Graft created at:")
            click.echo(f"  {result['graft_worktree_path']}/\n")
            click.echo(f"Your work has been applied onto current {base_branch}.")
            click.echo("Review and test, then merge:")
            click.echo(f"  cd {result['graft_worktree_path']}")
            click.echo("  (run tests)")
            click.echo(f"  skein shard merge {result['graft_worktree_name']}")
        else:
            click.echo(f"✗ Conflicts in: {', '.join(result['conflicts'])}\n")
            click.echo("Graft created at:")
            click.echo(f"  {result['graft_worktree_path']}/\n")

            # Show chain context if this is a multi-level graft
            depth = result.get("chain_depth", 0)
            if depth > 1:
                chain = shard_worktree.get_graft_chain(
                    shard_worktree.get_graft_chain_root(worktree_name)
                )
                click.echo("Chain: " + " → ".join(chain))
                click.echo()

            click.echo("Resolve conflicts:")
            click.echo(f"  cd {result['graft_worktree_path']}")
            for f in result["conflicts"]:
                click.echo(f"  (edit {f} to resolve conflicts)")
            click.echo("  git add <resolved files>")
            click.echo("  git commit\n")
            click.echo("Then merge:")
            click.echo(f"  skein shard merge {result['graft_worktree_name']}")

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to create graft: {e}")


@shard.command("merge")
@click.argument("worktree_name")
@click.option(
    "--caller-cwd",
    "explicit_caller_cwd",
    default=None,
    help="Original working directory of caller (for orchestration tools)",
)
@click.pass_context
def shard_merge(ctx, worktree_name, explicit_caller_cwd):
    """
    Merge SHARD branch into its base branch and cleanup.

    Refuses if there are uncommitted changes or conflicts.
    After successful merge, posts a tender folio (status=complete) and auto-closes it.

    If conflicts are detected, suggests using 'skein shard graft' instead.

    Example:
        skein shard merge beadle_0001-20251202-001

    For orchestration tools (e.g., Spindle), pass --caller-cwd to prevent
    agents from merging their own worktree after cd-ing elsewhere.
    """
    import os

    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    shard_worktree = get_shard_worktree_module()

    # Use explicit caller_cwd if provided (from orchestration tools),
    # otherwise fall back to current working directory
    caller_cwd = explicit_caller_cwd if explicit_caller_cwd else os.getcwd()

    # Gather tender metadata BEFORE merge (worktree gets deleted during merge)
    shard_info = shard_worktree.get_shard_status(worktree_name)
    if not shard_info:
        raise click.ClickException(f"SHARD not found: {worktree_name}")

    try:
        metadata = shard_worktree.get_tender_metadata(worktree_name)
    except Exception:
        metadata = None  # Will use minimal metadata if gather fails

    # Run xgun before merge (worktree is removed during merge)
    xgun_result = _run_xgun_scan(shard_info.get("worktree_path", ""))
    xgun_compact = _xgun_compact(xgun_result) if xgun_result else None

    try:
        # First, show drift context
        drift_info = shard_worktree.get_shard_drift_info(worktree_name)
        master_ahead = drift_info.get("base_commits_ahead", 0)
        base_branch = drift_info.get("base_branch")
        if base_branch is None:
            try:
                from skein import shard as shard_module

                base_branch = shard_module._detect_default_branch()
            except Exception:
                base_branch = "unknown"

        click.echo(f"Testing integration with current {base_branch}...")

        result = shard_worktree.merge_shard(worktree_name, caller_cwd=caller_cwd)

        if result["success"]:
            # Show drift context in success message
            if master_ahead > 0:
                click.echo(
                    f"✓ Clean integration (applied onto current {base_branch}, {master_ahead} commits ahead)"
                )
            else:
                click.echo("✓ Clean integration")
            click.echo()
            click.echo(f"Merging to {base_branch}...")
            click.echo(result["message"])

            if xgun_compact:
                click.echo(f"  {_xgun_verdict_line(xgun_compact)}")

            # Post tender folio with status=complete and auto-close it
            tender_id = _post_merge_tender(
                ctx, base_url, agent_id, worktree_name, shard_info, metadata, xgun_compact
            )
            if tender_id:
                click.echo(f"  Tender: {tender_id} (auto-closed)")

            # Suggest chain cleanup if this was a graft
            if shard_worktree.is_graft(worktree_name):
                root = shard_worktree.get_graft_chain_root(worktree_name)
                chain = shard_worktree.get_graft_chain(root)
                if chain:
                    click.echo("\nCleanup worktree chain:")
                    click.echo(f"  → skein shard cleanup {root} --chain")
                    click.echo("\nThis will remove:")
                    for wt in chain:
                        click.echo(f"  - worktrees/{wt}/")
            else:
                click.echo("\nCleanup worktree:")
                click.echo(f"  → skein shard cleanup {worktree_name}")
        else:
            click.echo(f"✗ {result['message']}")

            if result.get("uncommitted"):
                click.echo("\nUncommitted files:")
                for f in result["uncommitted"]:
                    click.echo(f"  {f}")
                click.echo("\nCommit your changes first, then retry merge.")

            if result.get("conflicts"):
                click.echo("\n✗ Conflicts detected")
                for f in result["conflicts"]:
                    click.echo(f"  - {f}")
                click.echo("\nCreate graft worktree to resolve:")
                click.echo(f"  → skein shard graft {worktree_name}")

            raise SystemExit(1)

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to merge SHARD: {e}")


def _post_merge_tender(
    ctx, base_url, agent_id, worktree_name, shard_info, metadata, xgun_compact=None
):
    """
    Post a tender folio with status=complete after successful merge, then auto-close it.

    Returns tender_id on success, None on failure (non-fatal).
    """
    # Derive site from project path
    site = None
    worktree_path = shard_info.get("worktree_path", "")
    if "/projects/" in worktree_path:
        parts = worktree_path.split("/projects/")[1].split("/")
        if parts:
            site = f"{parts[0]}-development"
    if not site:
        site = "shard-review"

    # Build content from metadata
    if metadata:
        summary_text = metadata.get("last_commit_message", "Merged")
        files_list = metadata.get("files_modified", [])
        commits = metadata.get("commits", 0)
        branch_name = metadata.get(
            "branch_name", shard_info.get("branch_name", "unknown")
        )
    else:
        summary_text = "Merged"
        files_list = []
        commits = 0
        branch_name = shard_info.get("branch_name", "unknown")

    files_str = "\n".join(f"  - {f}" for f in files_list[:20])
    if len(files_list) > 20:
        files_str += f"\n  ... and {len(files_list) - 20} more"

    quality_line = (
        f"\n### Code Quality\n{_xgun_verdict_line(xgun_compact)}\n"
        if xgun_compact
        else ""
    )

    content = f"""## Tender: {worktree_name}

**Status:** complete (merged)

### Summary
{summary_text}

### Changes
- **Commits:** {commits}
- **Branch:** {branch_name}

### Files Modified
{files_str if files_str else "  (none)"}
{quality_line}"""

    folio_data = {
        "type": "tender",
        "site_id": site,
        "title": (
            make_title_from_content(summary_text)
            if summary_text
            else f"Merged: {worktree_name}"
        ),
        "content": content,
        "metadata": {
            "worktree_name": worktree_name,
            "branch_name": branch_name,
            "commits": commits,
            "files_modified": files_list,
            "status": "complete",
            "merged": True,
            "name": shard_info.get("name"),
            "xgun": xgun_compact,
        },
    }

    try:
        result = make_request("POST", "/folios", base_url, agent_id, json=folio_data)
        tender_id = result.get("folio_id")

        if tender_id:
            # Auto-close the tender immediately
            status_data = {
                "from_id": tender_id,
                "to_id": tender_id,
                "type": "status",
                "content": "closed",
            }
            make_request("POST", "/threads", base_url, agent_id, json=status_data)

        return tender_id
    except Exception:
        # Tender posting is non-fatal - merge already succeeded
        return None


@shard.command("pause")
@click.argument("worktree_name")
@click.argument("reason")
@click.pass_context
def shard_pause(ctx, worktree_name, reason):
    """
    Pause work on a SHARD.

    Example:
        skein shard pause opus-security-architect-20251109-001 "Blocked on bubblewrap version decision"
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    # Import shard_worktree from current project
    shard_worktree = get_shard_worktree_module()

    # Verify SHARD exists
    shard_info = shard_worktree.get_shard_status(worktree_name)
    if not shard_info:
        raise click.ClickException(f"SHARD not found: {worktree_name}")

    # Find the SHARD thread
    try:
        threads = make_request("GET", "/threads?limit=100", base_url, agent_id)

        shard_thread_id = None
        for thread in threads:
            if thread.get("type") == "tag":
                try:
                    content = json.loads(thread.get("content", "{}"))
                    if (
                        content.get("tag") == "shard"
                        and content.get("worktree_name") == worktree_name
                    ):
                        shard_thread_id = thread.get("thread_id")
                        break
                except json.JSONDecodeError:
                    continue

        if shard_thread_id:
            # Reply to thread with pause status
            reply_data = {"thread_id": shard_thread_id, "content": f"[PAUSED] {reason}"}
            make_request(
                "POST",
                f"/threads/{shard_thread_id}/replies",
                base_url,
                agent_id,
                json=reply_data,
            )

        click.echo(f"⏸  Paused SHARD: {worktree_name}")
        click.echo(f"  Reason: {reason}")

    except Exception as e:
        # Don't fail if thread update fails
        click.echo(f"⏸  Paused SHARD: {worktree_name}")
        click.echo(f"  Reason: {reason}")
        click.echo(f"  Warning: Failed to update SKEIN thread: {e}", err=True)


@shard.command("resume")
@click.argument("worktree_name")
@click.argument("message", required=False)
@click.pass_context
def shard_resume(ctx, worktree_name, message):
    """
    Resume work on a paused SHARD.

    Examples:
        skein shard resume opus-security-architect-20251109-001 "Decision made: use bubblewrap 0.5.0"
        skein shard resume opus-security-architect-20251109-001
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    # Import shard_worktree from current project
    shard_worktree = get_shard_worktree_module()

    # Verify SHARD exists
    shard_info = shard_worktree.get_shard_status(worktree_name)
    if not shard_info:
        raise click.ClickException(f"SHARD not found: {worktree_name}")

    # Find the SHARD thread
    try:
        threads = make_request("GET", "/threads?limit=100", base_url, agent_id)

        shard_thread_id = None
        for thread in threads:
            if thread.get("type") == "tag":
                try:
                    content = json.loads(thread.get("content", "{}"))
                    if (
                        content.get("tag") == "shard"
                        and content.get("worktree_name") == worktree_name
                    ):
                        shard_thread_id = thread.get("thread_id")
                        break
                except json.JSONDecodeError:
                    continue

        if shard_thread_id:
            # Reply to thread with resume status
            resume_msg = f"[RESUMED] {message}" if message else "[RESUMED]"
            reply_data = {"thread_id": shard_thread_id, "content": resume_msg}
            make_request(
                "POST",
                f"/threads/{shard_thread_id}/replies",
                base_url,
                agent_id,
                json=reply_data,
            )

        click.echo(f"▶  Resumed SHARD: {worktree_name}")
        if message:
            click.echo(f"  Message: {message}")

    except Exception as e:
        # Don't fail if thread update fails
        click.echo(f"▶  Resumed SHARD: {worktree_name}")
        if message:
            click.echo(f"  Message: {message}")
        click.echo(f"  Warning: Failed to update SKEIN thread: {e}", err=True)


# ---------------------------------------------------------------------------
# xgun quality-gate helpers
#
# xgun (spiritengine/xgun) produces flags (hard issues that fail the gate),
# smells (agent-specific bad patterns, also fail the gate), and signals
# (leveled green/yellow/red FYIs that never block). These helpers run xgun
# once and render it consistently across tender, triage, merge, and inspect.
#
# Display policy: flags and smells are always shown. Signals are shown only
# when yellow or red; green signals collapse to a count + reveal hint, since
# a green FYI carries no actionable information. Checks that degraded to
# "not available" (radon/ast-grep binary missing) are surfaced loudly in the
# summary so a half-disabled scan never reads as a clean pass.
# ---------------------------------------------------------------------------


def _normalize_xgun(d):
    """Coerce an xgun result into the expected shape at the single trust boundary.

    ``dict.get(key, default)`` only defaults absent keys — a present-but-null or
    scalar value (e.g. ``"signals": null`` under schema drift) would still raise
    on downstream ``.get()``/iteration. Force the accessed containers to the
    right type (and drop non-dict list elements) so no nested access can raise.
    Unknown top-level keys are preserved.
    """
    def as_dict(v):
        return v if isinstance(v, dict) else {}

    def as_list_of_dicts(v):
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []

    out = dict(d)
    out["summary"] = as_dict(d.get("summary"))
    qgun = dict(as_dict(d.get("qgun")))
    qgun["flags"] = as_list_of_dicts(qgun.get("flags"))
    qgun["signals"] = as_list_of_dicts(qgun.get("signals"))
    out["qgun"] = qgun
    sgun = dict(as_dict(d.get("sgun")))
    sgun["smells"] = as_list_of_dicts(sgun.get("smells"))
    out["sgun"] = sgun
    return out


def _run_xgun_scan(worktree_path):
    """Run ``xgun scan`` on a worktree path, returning parsed JSON or None.

    Returns None when the xgun binary is absent or the scan fails — callers
    treat None as "no quality data available" rather than an error.
    """
    import shutil
    import subprocess
    import json as _json

    if not shutil.which("xgun"):
        return None
    try:
        result = subprocess.run(
            ["xgun", "scan", worktree_path, "--output", "json"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=worktree_path,
        )
        if result.returncode in (0, 1):  # 0=clean, 1=issues found
            parsed = _json.loads(result.stdout)
            # Only trust a dict — a valid-but-non-dict payload (array, bare
            # string) would slip past callers' falsy guards and raise on .get().
            if isinstance(parsed, dict):
                return _normalize_xgun(parsed)
    except (
        OSError,  # includes FileNotFoundError when worktree_path is gone
        subprocess.TimeoutExpired,
        subprocess.SubprocessError,
        _json.JSONDecodeError,
    ):
        pass
    return None


def _xgun_skipped_checks(xgun_result):
    """Names of checks that degraded to 'not available' (binary missing)."""
    skipped = []
    for sig in xgun_result.get("qgun", {}).get("signals", []):
        msg = (sig.get("message") or "").lower()
        if "not available" in msg:
            skipped.append(sig.get("check", "?"))
    return skipped


def _xgun_summary_counts(xgun_result):
    """Return (flags, smells, signals, passed) from the xgun summary block."""
    summary = xgun_result.get("summary", {})
    return (
        summary.get("flags", 0),
        summary.get("smells", 0),
        summary.get("signals", 0),
        summary.get("passed", True),
    )


def _xgun_compact(xgun_result):
    """Compact verdict suitable for storing in a tender folio's metadata."""
    flags, smells, signals, passed = _xgun_summary_counts(xgun_result)
    return {
        "passed": bool(passed),
        "flags": flags,
        "smells": smells,
        "signals": signals,
        "skipped": _xgun_skipped_checks(xgun_result),
    }


def _xgun_verdict_line(compact):
    """One-line verdict for triage/merge from a compact dict (see _xgun_compact).

    e.g. "✓ xgun: clean" or "✗ xgun: 2 flags, 1 smell (radon,ast_grep skipped)".
    """
    flags = compact.get("flags", 0)
    smells = compact.get("smells", 0)
    skipped = compact.get("skipped", [])
    skip_note = f" ({','.join(skipped)} skipped)" if skipped else ""
    if compact.get("passed", True) and flags == 0 and smells == 0:
        return f"✓ xgun: clean{skip_note}"
    parts = []
    if flags:
        parts.append(f"{flags} flag" + ("s" if flags != 1 else ""))
    if smells:
        parts.append(f"{smells} smell" + ("s" if smells != 1 else ""))
    return f"✗ xgun: {', '.join(parts) or 'issues'}{skip_note}"


def _xgun_detail_lines(xgun_result, verbose=False, reveal_hint=None):
    """Build full xgun verdict lines (used by inspect and the tender folio).

    Hides green signals by default (collapsed to a count + optional reveal
    hint); always shows yellow/red signals, flags, and smells; surfaces
    skipped checks in the summary line.
    """
    lines = []
    flags_count, smells_count, _sig_count, passed = _xgun_summary_counts(xgun_result)
    skipped = _xgun_skipped_checks(xgun_result)
    skip_note = (
        f"  [{len(skipped)} check{'s' if len(skipped) != 1 else ''} SKIPPED: "
        f"{', '.join(skipped)}]"
        if skipped
        else ""
    )
    clean = passed and flags_count == 0 and smells_count == 0
    state = "clean" if clean else "issues"
    mark = "✓" if clean else "✗"
    flag_word = "flag" if flags_count == 1 else "flags"
    smell_word = "smell" if smells_count == 1 else "smells"
    lines.append(
        f"{mark} Quality: {state} "
        f"({flags_count} {flag_word}, {smells_count} {smell_word}){skip_note}"
    )

    qgun = xgun_result.get("qgun", {})

    flags = qgun.get("flags", [])
    if flags:
        lines.append("")
        lines.append(f"Flags ({len(flags)}):")
        for f in flags[:10]:
            loc = f"{f.get('file', '?')}:{f['line']}" if f.get("line") else f.get("file", "?")
            lines.append(f"  {loc} [{f.get('check', '?')}] {f.get('message', '')}")
        if len(flags) > 10:
            lines.append(f"  ... and {len(flags) - 10} more")

    signals = qgun.get("signals", [])
    green = [s for s in signals if s.get("level") == "green"]
    # Show everything that isn't green (yellow/red and any unexpected level),
    # so a signal can never silently vanish — green is the only hidden class.
    to_show = signals if verbose else [s for s in signals if s.get("level") != "green"]
    if to_show:
        lines.append("")
        lines.append(f"Signals ({len(to_show)}):")
        for s in to_show:
            lines.append(
                f"  [{s.get('level', '?')}] [{s.get('check', '?')}] {s.get('message', '')}"
            )
    if green and not verbose:
        hint = f" — {reveal_hint}" if reveal_hint else ""
        lines.append(f"  +{len(green)} green signals hidden{hint}")

    smells = xgun_result.get("sgun", {}).get("smells", [])
    if smells:
        lines.append("")
        lines.append(f"Smells ({len(smells)}):")
        for sm in smells[:10]:
            loc = f"{sm.get('file', '?')}:{sm['line']}" if sm.get("line") else sm.get("file", "?")
            lines.append(f"  {loc} [{sm.get('kind', '?')}] {sm.get('reason', '')}")
        if len(smells) > 10:
            lines.append(f"  ... and {len(smells) - 10} more")

    return lines


def _xgun_folio_section(xgun_result):
    """Markdown section embedding the xgun verdict into a tender folio body."""
    body = "\n".join(_xgun_detail_lines(xgun_result, verbose=False))
    return (
        "### Code Quality (xgun)\n"
        "```\n"
        f"{body}\n"
        "```\n"
        "_Run `skein shard inspect <name> --verbose` for hidden green signals._"
    )


@shard.command("review")
@click.option(
    "--stale-days",
    default=7,
    type=int,
    help="Days without commits to consider stale (default: 7)",
)
@click.argument("worktree_name", required=False)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.option(
    "--verbose",
    is_flag=True,
    help="With a worktree name: show hidden green quality signals",
)
@click.pass_context
def shard_review(ctx, worktree_name, stale_days, output_json, verbose):
    """
    Show the SHARD review queue, or deep-review one SHARD by name.

    With no name: groups all shards by status for QM visibility:
    - READY: Has commits, clean working tree, no conflicts (merge candidates)
    - NEEDS_COMMIT: Has uncommitted changes
    - CONFLICTS: Would have merge conflicts with master
    - STALE: No commits and older than --stale-days

    With a worktree name: runs the deep review (alias for `shard inspect`),
    including the xgun quality scan.

    Examples:
        skein shard review
        skein shard review --stale-days 3
        skein shard review my-feature-20260113-001
    """
    # Deep-review dispatch: `review <name>` is an alias for `inspect <name>`,
    # matching the name everyone types and the docs/muscle memory.
    if worktree_name:
        ctx.invoke(
            shard_inspect,
            worktree_name=worktree_name,
            output_json=output_json,
            verbose=verbose,
        )
        return

    shard_worktree = get_shard_worktree_module()

    try:
        queue = shard_worktree.get_review_queue(stale_days=stale_days)

        if output_json:
            click.echo(json.dumps(queue, indent=2))
            return

        # Calculate totals
        total = sum(len(v) for v in queue.values())
        if total == 0:
            click.echo("No SHARDs found")
            return

        # Header with summary
        click.echo("=" * 60)
        click.echo("SHARD Review Queue")
        click.echo("=" * 60)
        click.echo(f"Total: {total} shards")
        click.echo(
            f"  Ready: {len(queue['ready'])}  |  Needs commit: {len(queue['needs_commit'])}  |  Conflicts: {len(queue['conflicts'])}  |  Stale: {len(queue['stale'])}"
        )
        click.echo()

        def format_shard_line(shard):
            """Format a single shard for display."""
            name = shard["worktree_name"]
            commits = shard.get("commits_ahead", 0)
            age = shard.get("age_days")
            age_str = f"{age}d" if age is not None else "?"

            # Extract project from worktree path if possible
            path = shard.get("worktree_path", "")
            project = "?"
            if "/projects/" in path:
                parts = path.split("/projects/")[1].split("/")
                if parts:
                    project = parts[0]

            # Diffstat summary (files changed)
            diffstat = shard.get("diffstat", "")
            files_changed = 0
            insertions = 0
            deletions = 0
            if diffstat:
                # Parse last line of diffstat: "N files changed, X insertions(+), Y deletions(-)"
                lines = diffstat.strip().split("\n")
                if lines:
                    last_line = lines[-1]
                    if "changed" in last_line:
                        import re

                        m = re.search(r"(\d+) files? changed", last_line)
                        if m:
                            files_changed = int(m.group(1))
                        m = re.search(r"(\d+) insertions?", last_line)
                        if m:
                            insertions = int(m.group(1))
                        m = re.search(r"(\d+) deletions?", last_line)
                        if m:
                            deletions = int(m.group(1))

            diff_str = ""
            if files_changed > 0:
                diff_str = f"{files_changed}f +{insertions}/-{deletions}"

            return f"  {name:<40} {age_str:>4}  +{commits:<2}  {project:<15} {diff_str}"

        # Show each category
        categories = [
            ("READY", "ready", "Merge candidates - clean and ready"),
            ("NEEDS_COMMIT", "needs_commit", "Have uncommitted changes"),
            ("CONFLICTS", "conflicts", "Would conflict with base branch"),
            ("STALE", "stale", f"No commits, older than {stale_days} days"),
        ]

        for label, key, description in categories:
            shards = queue[key]
            if not shards:
                continue

            click.echo(f"--- {label} ({len(shards)}) - {description} ---")
            for shard in shards:
                click.echo(format_shard_line(shard))
            click.echo()

        # Show helpful commands
        click.echo("Commands:")
        click.echo("  skein shard show <name>    # View details")
        click.echo("  skein shard diff <name>    # View changes")
        click.echo("  skein shard merge <name>   # Merge to base branch")

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to get review queue: {e}")


@shard.command("tender")
@click.argument("worktree_name")
@click.option(
    "--site", help="Site to post tender folio (default: derived from project)"
)
@click.option("--reviewer", help="Agent ID to review this SHARD (default: prime)")
@click.option("--summary", help="Brief summary of changes")
@click.option(
    "--status",
    type=click.Choice(["complete", "incomplete", "abandoned"]),
    default="complete",
    help="Work status: complete (default), incomplete, or abandoned",
)
@click.option(
    "--confidence",
    type=click.IntRange(1, 10),
    help="Merge confidence 1-10: 10=safe/additive/isolated (auto-merge candidate), "
    "5=moderate risk (needs review), 1=hot mess/critical path (careful review needed)",
)
@click.pass_context
def shard_tender(ctx, worktree_name, site, reviewer, summary, status, confidence):
    """
    Mark SHARD as ready for review (tender for assessment).

    Creates a tender folio visible to QMs and reviewers.

    Examples:
        skein shard tender my-shard-001
        skein shard tender my-shard --summary "Added auth checks" --confidence 8
        skein shard tender my-shard --site speakbot-pm --status incomplete
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    # Import shard_worktree from current project
    shard_worktree = get_shard_worktree_module()

    # Verify SHARD exists
    shard_info = shard_worktree.get_shard_status(worktree_name)
    if not shard_info:
        raise click.ClickException(f"SHARD not found: {worktree_name}")

    # Gather tender metadata
    try:
        metadata = shard_worktree.get_tender_metadata(worktree_name)
        if not metadata:
            raise click.ClickException(f"Could not gather metadata for {worktree_name}")
    except Exception as e:
        raise click.ClickException(f"Failed to gather metadata: {e}")

    # Resolve and validate the target site
    available_sites = make_request("GET", "/sites", base_url, agent_id)
    available_site_ids = [s["site_id"] for s in available_sites]

    if not site:
        worktree_path = shard_info.get("worktree_path", "")
        derived = None
        if "/projects/" in worktree_path:
            parts = worktree_path.split("/projects/")[1].split("/")
            if parts:
                derived = f"{parts[0]}-development"

        if derived and derived in available_site_ids:
            site = derived
        elif "shard-review" in available_site_ids:
            site = "shard-review"
        else:
            tried = [derived] if derived else []
            tried.append("shard-review")
            raise click.ClickException(
                f"No default site found — tried {', '.join(repr(t) for t in tried)}.\n"
                f"Available sites: {', '.join(available_site_ids) or '(none)'}\n"
                f"Pass one explicitly via: skein shard tender {worktree_name} --site <site>"
            )
    elif site not in available_site_ids:
        raise click.ClickException(
            f"Site '{site}' not found in this project.\n"
            f"Available sites: {', '.join(available_site_ids) or '(none)'}\n"
            f"Pass a valid site name from the list above: skein shard tender {worktree_name} --site <site>"
        )

    # Default reviewer
    if not reviewer:
        reviewer = "prime"

    # Build summary text
    summary_text = summary or metadata.get("last_commit_message", "No summary provided")

    # Build folio content
    files_list = metadata.get("files_modified", [])
    files_str = "\n".join(f"  - {f}" for f in files_list[:20])
    if len(files_list) > 20:
        files_str += f"\n  ... and {len(files_list) - 20} more"

    # Run xgun once at the handoff: embed the verdict in the folio body and
    # store a compact form in metadata so triage/merge can read it without
    # re-scanning.
    xgun_result = _run_xgun_scan(shard_info.get("worktree_path", ""))
    xgun_compact = _xgun_compact(xgun_result) if xgun_result else None
    quality_section = (
        "\n" + _xgun_folio_section(xgun_result) + "\n" if xgun_result else ""
    )

    content = f"""## Tender: {worktree_name}

**Status:** {status}
**Confidence:** {confidence or "unrated"}/10
**Reviewer:** {reviewer}

### Summary
{summary_text}

### Changes
- **Commits:** {metadata.get("commits", 0)}
- **Branch:** {metadata.get("branch_name", "unknown")}

### Files Modified
{files_str if files_str else "  (none)"}
{quality_section}"""

    # Create tender folio
    folio_data = {
        "type": "tender",
        "site_id": site,
        "title": (
            make_title_from_content(summary_text)
            if summary_text
            else f"Shard tender: {worktree_name}"
        ),
        "content": content,
        "metadata": {
            "worktree_name": worktree_name,
            "branch_name": metadata.get("branch_name"),
            "commits": metadata.get("commits", 0),
            "files_modified": files_list,
            "status": status,
            "confidence": confidence,
            "reviewer": reviewer,
            "name": metadata.get("name"),
            "xgun": xgun_compact,
        },
    }

    try:
        result = make_request("POST", "/folios", base_url, agent_id, json=folio_data)
        folio_id = result.get("folio_id")

        click.echo(f"Tendered SHARD: {worktree_name}")
        click.echo(f"  Folio: {folio_id}")
        click.echo(f"  Site: {site}")
        click.echo(f"  Status: {status}")
        if confidence is not None:
            click.echo(f"  Confidence: {confidence}/10")
        click.echo(f"  Reviewer: {reviewer}")
        click.echo(f"  Commits: {metadata.get('commits', 0)}")
        click.echo(f"  Files: {len(files_list)}")
        if xgun_compact:
            click.echo(f"  {_xgun_verdict_line(xgun_compact)}")

        if summary:
            click.echo(f"  Summary: {summary}")

        click.echo(f"\n  View: skein folio {folio_id}")
        click.echo(f"  List tenders: skein folios {site} --type tender")

    except Exception as e:
        raise click.ClickException(f"Failed to create tender folio: {e}")


@shard.command("triage")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def shard_triage(ctx, output_json):
    """
    Triage all SHARDs - actionable overview with status, conflicts, drift, and tender info.

    Shows all shards with:
    - Commit count and diffstat (+/-)
    - Merge status (clean/CONFLICT/uncommitted)
    - Drift info (master commits since base)
    - Graft chain context (if applicable)
    - Tender confidence if exists

    Example:
        skein shard triage
        skein shard triage --json
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    shard_worktree = get_shard_worktree_module()

    try:
        shards = shard_worktree.list_shards(active_only=True)

        if not shards:
            click.echo("No SHARDs found")
            return

        # Fetch all tender folios to match against shards
        tender_map = {}  # worktree_name -> tender info
        try:
            all_folios = make_request(
                "GET", "/folios", base_url, agent_id, params={"type": "tender"}
            )
            for folio in all_folios:
                metadata = folio.get("metadata", {})
                if isinstance(metadata, str):
                    try:
                        import json as json_module

                        metadata = json_module.loads(metadata)
                    except Exception:
                        continue
                wt_name = metadata.get("worktree_name")
                if wt_name:
                    tender_map[wt_name] = {
                        "folio_id": folio.get("folio_id"),
                        "confidence": metadata.get("confidence"),
                        "status": metadata.get("status"),
                        "summary": folio.get("title", "")[:50],
                        "xgun": metadata.get("xgun"),
                    }
        except Exception:
            pass  # Tender lookup is optional

        # Build triage data
        triage_data = []
        for shard_item in shards:
            wt_name = shard_item["worktree_name"]
            git_info = shard_worktree.get_shard_git_info(wt_name)
            drift_info = shard_worktree.get_shard_drift_info(wt_name)

            commits = git_info.get("commits_ahead", 0)
            git_info.get("merge_status", "unknown")
            uncommitted = git_info.get("uncommitted", [])

            # Get drift info
            master_ahead = drift_info.get("base_commits_ahead", 0)
            base_commit = drift_info.get("base_commit_short")
            conflict_status = drift_info.get("conflict_status", "unknown")
            conflict_files = drift_info.get("conflict_files", [])
            base_branch = drift_info.get("base_branch")
            if base_branch is None:
                try:
                    from skein import shard as shard_module

                    base_branch = shard_module._detect_default_branch()
                except Exception:
                    base_branch = "unknown"

            # Check if this is a graft
            is_graft = shard_worktree.is_graft(wt_name)
            graft_depth = shard_worktree.get_graft_depth(wt_name) if is_graft else 0

            # Parse diffstat for +/-
            diffstat = git_info.get("diffstat", "")
            insertions = 0
            deletions = 0
            if diffstat:
                import re

                ins_match = re.search(r"(\d+) insertions?\(\+\)", diffstat)
                del_match = re.search(r"(\d+) deletions?\(-\)", diffstat)
                if ins_match:
                    insertions = int(ins_match.group(1))
                if del_match:
                    deletions = int(del_match.group(1))

            # Determine status icon and text
            if uncommitted:
                status_icon = "○"  # uncommitted
                status_text = "uncommitted"
            elif conflict_status == "conflict":
                status_icon = "⚠"  # conflict
                status_text = "CONFLICT"
            elif commits == 0:
                status_icon = "·"  # empty
                status_text = "empty"
            else:
                status_icon = "✓"  # clean
                status_text = "clean"

            # Get tender info
            tender = tender_map.get(wt_name)
            confidence = tender.get("confidence") if tender else None

            entry = {
                "worktree_name": wt_name,
                "commits": commits,
                "insertions": insertions,
                "deletions": deletions,
                "status": status_text,
                "status_icon": status_icon,
                "confidence": confidence,
                "tender_id": tender.get("folio_id") if tender else None,
                "xgun": tender.get("xgun") if tender else None,
                "base_ahead": master_ahead,
                "base_branch": base_branch,
                "base_commit": base_commit,
                "is_graft": is_graft,
                "graft_depth": graft_depth,
                "conflict_status": conflict_status,
                "conflict_files": conflict_files,
            }
            triage_data.append(entry)

        if output_json:
            import json as json_module

            click.echo(json_module.dumps(triage_data, indent=2))
        else:
            click.echo(f"SHARDS ({len(triage_data)} total):\n")
            for entry in triage_data:
                name = entry["worktree_name"]
                commits = entry["commits"]
                ins = entry["insertions"]
                dels = entry["deletions"]
                status = entry["status"]
                icon = entry["status_icon"]
                conf = entry["confidence"]
                base_ahead = entry.get("base_ahead", 0)
                base_branch = entry.get("base_branch")
                if base_branch is None:
                    try:
                        from skein import shard as shard_module

                        base_branch = shard_module._detect_default_branch()
                    except Exception:
                        base_branch = "unknown"
                base_commit = entry.get("base_commit")
                is_graft = entry.get("is_graft", False)

                diffstat_str = f"+{ins}/-{dels}" if (ins or dels) else "---"

                # Use different icon for grafts
                if is_graft:
                    icon = "○"  # graft indicator

                parts = [
                    f"  {icon}",
                    f"{name:<40}",
                    f"{status:<12}",
                    f"{commits:>2} commits",
                    f"{diffstat_str:>12}",
                ]

                click.echo("  ".join(parts))

                # Show drift/graft context on second line
                context_parts = []
                if base_commit:
                    context_parts.append(f"base: {base_commit}")
                if base_ahead > 0:
                    conflict_status_val = entry.get("conflict_status", "unknown")
                    if conflict_status_val == "conflict":
                        context_parts.append(f"{base_branch} +{base_ahead} (conflicts)")
                    else:
                        context_parts.append(
                            f"{base_branch} +{base_ahead} (no conflicts)"
                        )
                if is_graft:
                    root = shard_worktree.get_graft_chain_root(name)
                    context_parts.append(f"graft of {root}")

                if context_parts:
                    click.echo(f"       {', '.join(context_parts)}")

                # Show conflict details if CONFLICT status but no drift info shown above
                conflict_status_val = entry.get("conflict_status", "unknown")
                conflict_files_list = entry.get("conflict_files", [])
                if status == "CONFLICT" and base_ahead == 0 and not is_graft:
                    # Conflict exists but not from drift or graft - explain why
                    click.echo(
                        f"       conflicts with {base_branch} (files: {', '.join(conflict_files_list[:3])}{'...' if len(conflict_files_list) > 3 else ''})"
                    )
                elif status == "CONFLICT" and conflict_files_list:
                    # Show which files conflict (for all CONFLICT cases with file info)
                    files_str = ", ".join(conflict_files_list[:3])
                    if len(conflict_files_list) > 3:
                        files_str += f" +{len(conflict_files_list) - 3} more"
                    click.echo(f"       conflicting files: {files_str}")

                # Show tender info
                if conf is not None:
                    click.echo(f"       confidence: {conf}/10")
                elif entry["tender_id"]:
                    click.echo(f"       tendered: {entry['tender_id']}")

                # Show xgun verdict (from tender metadata, computed at tender time)
                if entry.get("xgun"):
                    click.echo(f"       {_xgun_verdict_line(entry['xgun'])}")

                click.echo()  # Blank line between entries

            click.echo("Commands:")
            click.echo("  skein shard inspect <name>   # Deep review (with quality scan)")
            click.echo("  skein shard diff <name>      # View work diff")
            click.echo("  skein shard merge <name>     # Merge to base branch")
            click.echo(
                "  skein shard graft <name>     # Create graft to resolve conflicts"
            )

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to triage SHARDs: {e}")


@shard.command("inspect")
@click.argument("worktree_name")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.option(
    "--verbose",
    is_flag=True,
    help="Show hidden green quality signals (off by default)",
)
@click.pass_context
def shard_inspect(ctx, worktree_name, output_json, verbose):
    """
    Deep review of a single SHARD for merge decision.

    Shows:
    - Work diff (agent's actual changes from base)
    - Master activity since base (drift info)
    - Conflict status with specific files
    - Graft chain context (if applicable)
    - Tender summary and confidence

    Example:
        skein shard inspect my-shard-001
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    shard_worktree = get_shard_worktree_module()

    try:
        shard_info = shard_worktree.get_shard_status(worktree_name)
        if not shard_info:
            raise click.ClickException(f"SHARD not found: {worktree_name}")

        git_info = shard_worktree.get_shard_git_info(worktree_name)
        drift_info = shard_worktree.get_shard_drift_info(worktree_name)

        # Look up tender folio
        tender_info = None
        try:
            all_folios = make_request(
                "GET", "/folios", base_url, agent_id, params={"type": "tender"}
            )
            for folio in all_folios:
                metadata = folio.get("metadata", {})
                if isinstance(metadata, str):
                    try:
                        import json as json_module

                        metadata = json_module.loads(metadata)
                    except Exception:
                        continue
                if metadata.get("worktree_name") == worktree_name:
                    tender_info = {
                        "folio_id": folio.get("folio_id"),
                        "confidence": metadata.get("confidence"),
                        "status": metadata.get("status"),
                        "summary": folio.get("title", ""),
                    }
                    break
        except Exception:
            pass

        # Get conflict info from drift_info
        conflict_status = drift_info.get("conflict_status", "unknown")
        conflict_files = drift_info.get("conflict_files", [])

        # Build review data
        is_nested = drift_info.get("is_nested", False)
        review_data = {
            "worktree_name": worktree_name,
            "branch_name": shard_info["branch_name"],
            "worktree_path": shard_info["worktree_path"],
            "commits_ahead": git_info.get("commits_ahead", 0),
            "merge_status": conflict_status,
            "uncommitted": git_info.get("uncommitted", []),
            "commit_log": git_info.get("commit_log", []),
            "diffstat": git_info.get("diffstat", ""),
            "conflict_files": conflict_files,
            "tender": tender_info,
            "base_commit": drift_info.get("base_commit_short"),
            "base_commit_date": drift_info.get("base_commit_date"),
            "base_commits_ahead": drift_info.get("base_commits_ahead", 0),
            "base_notable_changes": drift_info.get("base_notable_changes", []),
            "is_graft": shard_worktree.is_graft(worktree_name),
            "is_nested": is_nested,
            "work_diff_stat": drift_info.get("work_diff_stat"),
        }

        # Run xgun scan if available (None if absent — silent degradation)
        xgun_result = _run_xgun_scan(shard_info["worktree_path"])

        if xgun_result:
            review_data["xgun"] = xgun_result

        if output_json:
            import json as json_module

            click.echo(json_module.dumps(review_data, indent=2))
        else:
            click.echo(f"=== SHARD: {worktree_name} ===")
            click.echo()

            # Show graft chain context if applicable
            is_graft = shard_worktree.is_graft(worktree_name)
            if is_graft:
                root = shard_worktree.get_graft_chain_root(worktree_name)
                chain = shard_worktree.get_graft_chain(root)
                click.echo(f"Chain: {' → '.join(chain)}")
                click.echo()

            # Show work info with base commit
            base_commit = drift_info.get("base_commit_short")
            base_branch = drift_info.get("base_branch")
            if base_branch is None:
                try:
                    from skein import shard as shard_module

                    base_branch = shard_module._detect_default_branch()
                except Exception:
                    base_branch = "unknown"
            base_date = drift_info.get("base_commit_date", "")
            commits = git_info.get("commits_ahead", 0)
            uncommitted = git_info.get("uncommitted", [])

            if uncommitted:
                click.echo("Your Work (has uncommitted changes):")
            elif conflict_status == "conflict":
                click.echo(f"Your Work (conflicts with {base_branch}):")
            else:
                click.echo("Your Work (clean, ready to integrate):")

            if base_commit:
                click.echo(
                    f"  Base: {base_commit}" + (f" ({base_date})" if base_date else "")
                )
            click.echo(f"  Commits: {commits}")

            # Show work diff stat (agent's actual changes)
            work_stat = drift_info.get("work_diff_stat")
            files_changed = 0
            if work_stat:
                # Parse summary line and count files
                lines = work_stat.strip().split("\n")
                if lines:
                    summary = lines[-1]  # Last line has totals
                    click.echo(f"  Changes: {summary.strip()}")
                    # Count non-summary lines (each represents a file)
                    files_changed = len(lines) - 1 if len(lines) > 1 else 0

            # Show file count
            if files_changed > 0:
                click.echo(f"  Files changed: {files_changed}")
            click.echo()

            # Show uncommitted changes if any
            if uncommitted:
                click.echo("UNCOMMITTED CHANGES:")
                for line in uncommitted[:10]:
                    click.echo(f"  {line}")
                if len(uncommitted) > 10:
                    click.echo(f"  ... and {len(uncommitted) - 10} more")
                click.echo()

            # Show master activity (drift)
            master_ahead = drift_info.get("base_commits_ahead", 0)
            if master_ahead > 0:
                click.echo(f"{base_branch} Activity Since Your Base:")
                click.echo(f"  {master_ahead} new commits merged to {base_branch}")
                click.echo()

                notable = drift_info.get("base_notable_changes", [])
                if notable:
                    click.echo("  Notable changes:")
                    for change in notable[:5]:
                        click.echo(f"    - {change}")
                    click.echo()

                # Show conflict status
                if conflict_status == "conflict":
                    click.echo("  ⚠ Integration test: Conflicts detected")
                    if conflict_files:
                        for f in conflict_files[:10]:
                            click.echo(f"    - {f}")
                        if len(conflict_files) > 10:
                            click.echo(f"    ... and {len(conflict_files) - 10} more")
                elif is_nested and not is_graft:
                    # Nested shard (spawned from another shard) - can't merge directly
                    click.echo("  ✓ Integration test: No conflicts detected")
                    click.echo("  ⚠ Nested shard: contains commits from parent shard")
                    click.echo("    Must graft to isolate your changes before merging")
                else:
                    click.echo("  ✓ Integration test: No conflicts detected")
                    if commits > 0:
                        click.echo(f"  ✓ Ready to merge onto current {base_branch}")
                    else:
                        click.echo("  ℹ No code changes (research/verification only)")
                click.echo()
            elif base_commit:
                if is_nested and not is_graft:
                    # Nested shard at same base as base branch
                    click.echo("⚠ Nested shard: contains commits from parent shard")
                    click.echo("  Must graft to isolate your changes before merging")
                elif commits > 0:
                    click.echo(f"✓ {base_branch} is at same state as your base")
                    click.echo("✓ Ready to merge")
                else:
                    click.echo(f"✓ {base_branch} is at same state as your base")
                    click.echo("ℹ No code changes (research/verification only)")
                click.echo()

            # Show tender info
            if tender_info:
                conf_str = (
                    f"{tender_info['confidence']}/10"
                    if tender_info.get("confidence")
                    else "unrated"
                )
                click.echo(
                    f"Tender: {tender_info['folio_id']} (confidence: {conf_str})"
                )
                if tender_info.get("summary"):
                    click.echo(f"  {tender_info['summary']}")
                click.echo()

            # Show xgun quality check results (green signals hidden unless --verbose)
            if xgun_result:
                click.echo("=== Code Quality (xgun) ===")
                click.echo()
                hint = f"skein shard inspect {worktree_name} --verbose"
                for line in _xgun_detail_lines(
                    xgun_result, verbose=verbose, reveal_hint=hint
                ):
                    click.echo(line)
                click.echo()

            # Actions
            if uncommitted:
                click.echo("Commit your changes first, then merge:")
                click.echo(f"  cd {shard_info['worktree_path']}")
                click.echo("  git add . && git commit")
                click.echo(f"  skein shard merge {worktree_name}")
            elif conflict_status == "conflict":
                click.echo("Create graft worktree to resolve:")
                click.echo(f"  → skein shard graft {worktree_name}")
                click.echo()
                click.echo("Or review your original work first:")
                click.echo(f"  → skein shard diff {worktree_name}")
            elif is_nested and not is_graft:
                # Nested shard needs grafting to isolate changes
                click.echo("Graft to isolate your changes from parent shard:")
                click.echo(f"  → skein shard graft {worktree_name}")
                click.echo()
                click.echo(
                    "This will cherry-pick only your commits onto the base branch."
                )
            elif commits == 0:
                click.echo("Nothing to merge (research/verification shard):")
                click.echo(f"  → skein shard cleanup {worktree_name}")
            else:
                click.echo("Merge to base branch:")
                click.echo(f"  → skein shard merge {worktree_name}")
                if is_graft:
                    root = shard_worktree.get_graft_chain_root(worktree_name)
                    click.echo()
                    click.echo("After merge, cleanup chain:")
                    click.echo(f"  → skein shard cleanup {root} --chain")

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to review SHARD: {e}")


@shard.command("stash")
@click.argument("description")
@click.option("--agent", "stash_agent", help="Agent ID for the new SHARD")
@click.option(
    "--base",
    "base_branch",
    default=None,
    help="Branch to stash onto (default: auto-detected from repo)",
)
@click.pass_context
def shard_stash(ctx, description, stash_agent, base_branch):
    """
    Stash uncommitted changes into a new SHARD.

    Creates a new shard worktree, moves your uncommitted changes there,
    and leaves your current branch clean.

    Example:
        skein shard stash "WIP: auth refactor"
        skein shard stash "WIP: auth refactor" --base main
    """
    shard_worktree = get_shard_worktree_module()

    try:
        from skein import shard as shard_module

        repo = shard_module._get_repo()

        # Check for uncommitted changes
        status = repo.git.status("--porcelain")
        if not status.strip():
            raise click.ClickException("No uncommitted changes to stash")

        # Generate agent ID if not provided
        if not stash_agent:
            from datetime import datetime

            stash_agent = f"stash-{datetime.now().strftime('%m%d')}"

        # Detect default branch if not explicitly provided
        if base_branch is None:
            base_branch = shard_module._detect_default_branch(repo)

        # Branch from the detected default to avoid picking up stale
        # commits from whatever branch the working tree is on.
        new_shard = shard_worktree.spawn_shard(
            stash_agent,
            description=description,
            base_branch=base_branch,
        )
        worktree_path = new_shard["worktree_path"]
        worktree_name = new_shard["worktree_name"]

        # Git stash, then apply in new worktree
        repo.git.stash("push", "-m", f"shard-stash: {description}")

        try:
            # Apply stash in the new worktree using subprocess
            import subprocess

            result = subprocess.run(
                ["git", "stash", "apply", "--index"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise Exception(result.stderr)

            # Drop the stash since we applied it
            repo.git.stash("drop")

            click.echo(f"✓ Stashed changes to SHARD: {worktree_name}")
            click.echo(f"  Path: {worktree_path}")
            click.echo(f"  Description: {description}")
            click.echo()
            click.echo("Your current branch is now clean.")
            click.echo(f"To continue work: cd {worktree_path}")

        except Exception as e:
            # If apply fails, restore the stash
            try:
                repo.git.stash("pop")
            except Exception:
                pass
            try:
                shard_worktree.cleanup_shard(worktree_name, keep_branch=False)
            except Exception:
                pass
            raise click.ClickException(f"Failed to apply stash to new shard: {e}")

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        if "ClickException" in str(type(e).__name__):
            raise
        raise click.ClickException(f"Failed to stash: {e}")


@shard.command("apply")
@click.argument("worktree_name")
@click.option("--no-confirm", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def shard_apply(ctx, worktree_name, no_confirm):
    """
    Apply SHARD changes as uncommitted changes to current branch.

    Takes the diff between the base branch and the shard branch and applies it
    as uncommitted changes. Useful for cherry-picking from stale shards.

    Example:
        skein shard apply my-shard-001
    """
    shard_worktree = get_shard_worktree_module()

    try:
        shard_info = shard_worktree.get_shard_status(worktree_name)
        if not shard_info:
            raise click.ClickException(f"SHARD not found: {worktree_name}")

        from skein import shard as shard_module

        repo = shard_module._get_repo()
        branch = shard_info["branch_name"]

        # Check for existing uncommitted changes
        status = repo.git.status("--porcelain")
        if status.strip() and not no_confirm:
            click.echo("Warning: You have uncommitted changes.")
            if not click.confirm("Continue?"):
                raise click.ClickException("Aborted")

        # Get the diff (base_branch..branch)
        base_branch = shard_module._get_shard_base_branch(worktree_name)
        diff = repo.git.diff(base_branch, branch)
        if not diff.strip():
            click.echo(f"No changes in shard {worktree_name}")
            return

        git_info = shard_worktree.get_shard_git_info(worktree_name)
        commits = git_info.get("commits_ahead", 0)

        click.echo(f"Applying changes from: {worktree_name} ({commits} commits)")

        if not no_confirm:
            if not click.confirm("Apply as uncommitted changes?"):
                raise click.ClickException("Aborted")

        # Apply the diff using subprocess (git apply reads from stdin)
        try:
            import subprocess

            # Ensure trailing newline (git apply requires it)
            if not diff.endswith("\n"):
                diff += "\n"
            result = subprocess.run(
                ["git", "apply"],
                input=diff,
                text=True,
                capture_output=True,
                cwd=shard_module.PROJECT_ROOT,
            )
            if result.returncode != 0:
                raise Exception(result.stderr or "git apply failed")
            click.echo(f"✓ Applied changes from {worktree_name}")
            click.echo("  Review with `git status` and `git diff`")
        except Exception as e:
            raise click.ClickException(f"Failed to apply: {e}")

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        if "ClickException" in str(type(e).__name__):
            raise
        raise click.ClickException(f"Failed to apply SHARD: {e}")


@shard.command("test")
@click.argument("worktree_name")
@click.option("--rite", "rite_name", default="test", help="Rite to run (default: test)")
@click.option("--verbose", "-v", is_flag=True, help="Show command output")
@click.pass_context
def shard_test(ctx, worktree_name, rite_name, verbose):
    """
    Run a rite in a SHARD's worktree.

    Runs the specified rite (default: 'test') in the shard's worktree directory.
    The rite must be defined in the project's .skein/rites.yaml.

    Examples:
        skein shard test my-shard-001           # Run 'test' rite
        skein shard test my-shard-001 --rite lint  # Run 'lint' rite
        skein shard test my-shard-001 -v        # Verbose output
    """
    shard_worktree = get_shard_worktree_module()

    try:
        shard_info = shard_worktree.get_shard_status(worktree_name)
        if not shard_info:
            raise click.ClickException(f"SHARD not found: {worktree_name}")

        worktree_path = Path(shard_info["worktree_path"])
        if not worktree_path.exists():
            raise click.ClickException(f"SHARD worktree not found: {worktree_path}")

        # Load rites config from the MAIN project (not worktree)
        # Rites are project-level, shards just run them in their context
        from skein import shard as shard_module

        project_root = shard_module.PROJECT_ROOT

        config = load_rites_config(project_root)
        rites_dict = config.get("rites", {})

        if rite_name not in rites_dict:
            if not rites_dict:
                raise click.ClickException(
                    f"No rites defined. Create {project_root / '.skein' / 'rites.yaml'}"
                )
            available = ", ".join(rites_dict.keys())
            raise click.ClickException(
                f"Unknown rite: {rite_name}\nAvailable: {available}"
            )

        rite_config = rites_dict[rite_name]

        click.echo(f"▶ Running rite '{rite_name}' in shard: {worktree_name}")
        click.echo(f"  Worktree: {worktree_path}")

        success = run_rite_commands(rite_name, rite_config, worktree_path, verbose)

        if success:
            click.echo(f"✓ Rite '{rite_name}' completed in shard {worktree_name}")
        else:
            raise click.ClickException(
                f"Rite '{rite_name}' failed in shard {worktree_name}"
            )

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        if "ClickException" in str(type(e).__name__):
            raise
        raise click.ClickException(f"Failed to run rite in shard: {e}")


# Web UI Command
@cli.command()
@click.option(
    "--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)"
)
@click.option(
    "--port", "-p", default=8003, type=int, help="Port to listen on (default: 8003)"
)
@click.option(
    "--open", "open_browser", is_flag=True, help="Open browser after starting"
)
def web(host, port, open_browser):
    """Launch the SKEIN web UI.

    Opens a browser-based interface for viewing sites, folios, and activity.

    Example:
        skein web              # Start on localhost:8003
        skein web --port 8080  # Start on custom port
        skein web --open       # Start and open browser
    """
    try:
        from skein.web import run_server
    except ImportError as e:
        raise click.ClickException(f"Web UI not available: {e}")

    # Check if port is available before proceeding
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        sock.close()
    except OSError:
        raise click.ClickException(
            f"Port {port} is already in use. Try a different port with --port"
        )

    click.echo("=" * 60)
    click.echo("SKEIN Web UI")
    click.echo("=" * 60)
    click.echo(f"Server: http://{host}:{port}")
    click.echo(f"Project: {os.environ.get('SKEIN_PROJECT', 'default')}")
    click.echo("=" * 60)
    click.echo("Press Ctrl+C to stop")
    click.echo()

    if open_browser:
        import webbrowser

        webbrowser.open(f"http://{host}:{port}")

    run_server(host=host, port=port)


# Alias: 'skein ui' as shortcut for 'skein web'
@cli.command(name="ui", hidden=True)
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", "-p", default=8003, type=int, help="Port to listen on")
@click.option("--open", "open_browser", is_flag=True, help="Open browser")
@click.pass_context
def ui_shortcut(ctx, host, port, open_browser):
    """Shortcut for 'skein web'."""
    ctx.invoke(web, host=host, port=port, open_browser=open_browser)


# Alias for common usage
@cli.command(name="shards", hidden=True)
@click.option("--active", is_flag=True, help="Show only active SHARDs")
@click.option("--agent", "filter_agent", help="Filter by agent ID")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def shards_shortcut(ctx, active, filter_agent, output_json):
    """Shortcut for 'skein shard list'."""
    ctx.invoke(
        shard_list, active=active, filter_agent=filter_agent, output_json=output_json
    )


# =============================================================================
# RITES - Named project operations
# =============================================================================


def load_rites_config(project_root: Optional[Path] = None) -> Dict[str, Any]:
    """
    Load rites configuration from .skein/rites.yaml.

    Returns dict with 'rites' key containing named rite definitions.
    """
    if project_root is None:
        project_root = find_project_root()

    if not project_root:
        return {"rites": {}}

    rites_file = project_root / ".skein" / "rites.yaml"
    if not rites_file.exists():
        return {"rites": {}}

    try:
        import yaml

        with open(rites_file) as f:
            config = yaml.safe_load(f) or {}
        return config
    except ImportError:
        raise click.ClickException("PyYAML required for rites. Run: pip install pyyaml")
    except Exception as e:
        raise click.ClickException(f"Failed to load rites config: {e}")


def run_rite_commands(
    rite_name: str,
    rite_config: Dict[str, Any],
    working_dir: Optional[Path] = None,
    verbose: bool = False,
) -> bool:
    """
    Execute a rite's commands.

    Args:
        rite_name: Name of the rite being run
        rite_config: Rite configuration dict with 'commands' key
        working_dir: Directory to run commands in (default: current)
        verbose: Show command output in real-time

    Returns:
        True if all commands succeeded, False otherwise
    """
    import subprocess

    commands = rite_config.get("commands", [])
    if not commands:
        click.echo(f"Rite '{rite_name}' has no commands defined", err=True)
        return False

    if isinstance(commands, str):
        commands = [commands]

    cwd = str(working_dir) if working_dir else None

    for i, cmd in enumerate(commands, 1):
        if verbose:
            click.echo(f"[{i}/{len(commands)}] {cmd}")

        try:
            result = subprocess.run(
                cmd, shell=True, cwd=cwd, capture_output=not verbose, text=True
            )

            if result.returncode != 0:
                if not verbose and result.stderr:
                    click.echo(result.stderr, err=True)
                if not verbose and result.stdout:
                    click.echo(result.stdout)
                click.echo(
                    f"✗ Command failed (exit {result.returncode}): {cmd}", err=True
                )
                return False

        except Exception as e:
            click.echo(f"✗ Failed to run command: {e}", err=True)
            return False

    return True


@cli.command("rite")
@click.argument("rite_name", required=False)
@click.option("--verbose", "-v", is_flag=True, help="Show command output")
@click.pass_context
def rite_cmd(ctx, rite_name, verbose):
    """
    Run a named project operation (rite).

    Rites are defined in .skein/rites.yaml:

    \b
        rites:
          test:
            description: "Run test suite"
            commands:
              - pytest
          lint:
            description: "Check code style"
            commands:
              - ruff check .

    Examples:
        skein rite test          # Run the test rite
        skein rite test -v       # Run with verbose output
        skein rite               # List available rites (same as 'skein rites')
    """
    # If no rite name, list rites
    if rite_name is None:
        ctx.invoke(rites_list)
        return

    # Run the rite
    project_root = find_project_root()
    if not project_root:
        raise click.ClickException(
            "Not in a SKEIN project (no .skein/ directory found)"
        )

    config = load_rites_config(project_root)
    rites_dict = config.get("rites", {})

    if rite_name not in rites_dict:
        available = ", ".join(rites_dict.keys()) if rites_dict else "(none)"
        raise click.ClickException(f"Unknown rite: {rite_name}\nAvailable: {available}")

    rite_config = rites_dict[rite_name]
    description = rite_config.get("description", "")

    click.echo(f"▶ Running rite: {rite_name}")
    if description and verbose:
        click.echo(f"  {description}")

    success = run_rite_commands(rite_name, rite_config, project_root, verbose)

    if success:
        click.echo(f"✓ Rite '{rite_name}' completed")
    else:
        raise click.ClickException(f"Rite '{rite_name}' failed")


@cli.command("rites")
@click.pass_context
def rites_list(ctx):
    """
    List available rites for this project.

    Rites are defined in .skein/rites.yaml.
    """
    project_root = find_project_root()
    if not project_root:
        raise click.ClickException(
            "Not in a SKEIN project (no .skein/ directory found)"
        )

    config = load_rites_config(project_root)
    rites_dict = config.get("rites", {})

    if not rites_dict:
        click.echo("No rites defined.")
        click.echo(f"\nCreate {project_root / '.skein' / 'rites.yaml'} with:")
        click.echo(
            """
rites:
  test:
    description: "Run test suite"
    commands:
      - pytest
  lint:
    description: "Check code style"
    commands:
      - ruff check .
"""
        )
        return

    click.echo(f"Available rites ({len(rites_dict)}):\n")
    for name, rite_config in rites_dict.items():
        description = rite_config.get("description", "")
        commands = rite_config.get("commands", [])
        cmd_count = len(commands) if isinstance(commands, list) else 1

        click.echo(f"  {name}")
        if description:
            click.echo(f"    {description}")
        click.echo(f"    ({cmd_count} command{'s' if cmd_count != 1 else ''})")
        click.echo()


def main():
    """Entry point for the skein CLI (called by pip-installed command)."""
    cli()


if __name__ == "__main__":
    main()
