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
import json
import click
import requests
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any, Set

# Import name generator from skein package
try:
    from skein.utils import generate_agent_name
except ImportError:
    # Fallback if skein package not installed
    generate_agent_name = None


def find_project_root() -> Optional[Path]:
    """
    Walk up directory tree to find .skein/ directory (like git).
    Returns project root path or None if not found.
    """
    current = Path.cwd()
    while current != current.parent:
        skein_dir = current / '.skein'
        if skein_dir.exists() and skein_dir.is_dir():
            return current
        current = current.parent
    return None


def get_project_config() -> Optional[Dict[str, Any]]:
    """Get project config from .skein/config.json if in a project."""
    project_root = find_project_root()
    if not project_root:
        return None

    config_file = project_root / '.skein' / 'config.json'
    if not config_file.exists():
        return None

    try:
        with open(config_file) as f:
            return json.load(f)
    except:
        return None


def get_global_config() -> Dict[str, Any]:
    """Get global SKEIN config from ~/.skein/config.json."""
    config_file = Path.home() / '.skein' / 'config.json'
    if not config_file.exists():
        return {"server_url": "http://localhost:8001"}

    try:
        with open(config_file) as f:
            return json.load(f)
    except:
        return {"server_url": "http://localhost:8001"}


def get_agent_id(ctx_agent: Optional[str] = None, base_url: Optional[str] = None) -> str:
    """
    Get agent ID from sources in priority order:
    1. --agent flag (explicit override)
    2. SKEIN_AGENT_ID env var
    3. "unknown" fallback
    """
    if ctx_agent:
        return ctx_agent
    return os.getenv("SKEIN_AGENT_ID", "unknown")


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
        if isinstance(arg, str) and '=' in arg and not arg.startswith('-'):
            # Check if it looks like name=value syntax
            parts = arg.split('=', 1)
            if len(parts) == 2 and parts[0].isidentifier():
                param_name = parts[0]
                raise click.ClickException(
                    f"Incorrect syntax: '{arg}'\n\n"
                    f"It looks like you're using '{param_name}=\"...\"' syntax.\n"
                    f"The SKEIN CLI uses positional arguments, not named parameters.\n\n"
                    f"Correct syntax: skein {command_name} SITE_ID \"description\"\n"
                    f"See: skein {command_name} --help"
                )


def make_request(method: str, endpoint: str, base_url: str, agent_id: str, **kwargs):
    """Make HTTP request to SKEIN API."""
    url = f"{base_url}/skein{endpoint}"
    headers = kwargs.pop("headers", {})

    if agent_id != "unknown":
        headers["X-Agent-Id"] = agent_id

    # Add project ID from env var or project config
    project_id = os.environ.get("SKEIN_PROJECT")
    if not project_id:
        project_config = get_project_config()
        if project_config:
            project_id = project_config.get("project_id")
    if project_id:
        headers["X-Project-Id"] = project_id

    # Warn if agent is still orienting when posting folios
    if method == "POST" and endpoint == "/folios" and agent_id != "unknown":
        try:
            roster_url = f"{base_url}/skein/roster/{agent_id}"
            roster_resp = requests.get(roster_url, headers=headers)
            if roster_resp.ok:
                agent_data = roster_resp.json()
                if agent_data.get("status") == "orienting":
                    click.echo(f"Note: You're still orienting. Run 'skein --agent {agent_id} ready' when done.", err=True)
        except:
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
            except:
                raise click.ClickException(f"API error: {e.response.text or str(e)}")
        raise click.ClickException(f"Connection error: {str(e)}")


@click.group()
@click.option("--agent", envvar="SKEIN_AGENT_ID", help="Agent ID (or set SKEIN_AGENT_ID)")
@click.option("--url", envvar="SKEIN_URL", help="SKEIN server URL (default: localhost:8001)")
@click.pass_context
def cli(ctx, agent, url):
    """SKEIN CLI - Agent collaboration system.

    Getting started: skein info quickstart
    Full guide: skein info guide
    """
    ctx.ensure_object(dict)
    ctx.obj["agent"] = agent
    ctx.obj["url"] = url


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
    skein_dir = project_root / '.skein'

    # Check if already initialized
    if skein_dir.exists():
        raise click.ClickException(f"SKEIN already initialized in {project_root}")

    # Create .skein directory structure
    skein_dir.mkdir()
    data_dir = skein_dir / 'data'
    data_dir.mkdir()
    (data_dir / 'sites').mkdir()
    (data_dir / 'roster').mkdir()
    (data_dir / 'threads').mkdir()
    (data_dir / 'screenshots').mkdir()

    # Create project config
    project_config = {
        "project_id": project,
        "name": name or project,
        "created_at": datetime.now().isoformat(),
        "server_url": "http://localhost:8001"
    }

    config_file = skein_dir / 'config.json'
    with open(config_file, 'w') as f:
        json.dump(project_config, f, indent=2)

    # Register in global projects.json
    global_dir = Path.home() / '.skein'
    global_dir.mkdir(exist_ok=True)

    projects_file = global_dir / 'projects.json'
    if projects_file.exists():
        with open(projects_file) as f:
            projects_data = json.load(f)
    else:
        projects_data = {"projects": {}}

    projects_data["projects"][project] = {
        "path": str(project_root),
        "data_dir": str(data_dir),
        "name": name or project,
        "registered_at": datetime.now().isoformat()
    }

    with open(projects_file, 'w') as f:
        json.dump(projects_data, f, indent=2)

    click.echo(f"✓ Initialized SKEIN project '{project}' in {project_root}")
    click.echo(f"✓ Created .skein/ directory")
    click.echo(f"✓ Registered in ~/.skein/projects.json")
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
        template_path = Path(__file__).parent.parent / "skein" / "templates" / "CLAUDE.md"

    if not template_path.exists():
        raise click.ClickException(f"Template not found at {template_path}")

    # Read template
    with open(template_path) as f:
        template_content = f.read()

    # Target file in current directory
    target_path = Path.cwd() / "CLAUDE.md"

    # Append or create
    if target_path.exists():
        with open(target_path, 'a') as f:
            f.write("\n\n")
            f.write(template_content)
        click.echo(f"Appended SKEIN instructions to {target_path}")
    else:
        with open(target_path, 'w') as f:
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
    global_dir = Path.home() / '.skein'
    projects_file = global_dir / 'projects.json'

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
    except:
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
        click.echo(f"\n  * = current project")

    if not verbose:
        click.echo(f"\nUse 'skein projects -v' for detailed information")


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
            ['git', 'rev-parse', '--git-dir'],
            capture_output=True, timeout=5
        )
        checks["git"] = result.returncode == 0
    except Exception:
        checks["git"] = False

    # Check .skein/ directory
    project_root = find_project_root()
    if project_root:
        skein_dir = project_root / '.skein'
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

    data = {
        "stream_id": stream_id,
        "source": agent_id,
        "lines": lines
    }

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

    log_lines = make_request("GET", f"/logs/{stream_id}", base_url, agent_id, params=params)

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
                click.echo(f"\n... and {len(log_lines) - 50} more lines (use --json to see all)")


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
def log_cmd(ctx, max_count, since, until, agent, site_filter, type_filter, grep, oneline, follow, no_pager, output_json):
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
    folios_list = make_request("GET", "/folios", base_url, agent_id, params=params if params else None)

    # Fetch all threads for thread count
    try:
        all_threads = make_request("GET", "/threads", base_url, agent_id)
        threads_by_folio = {}
        for thread in all_threads:
            for fid in [thread['from_id'], thread['to_id']]:
                if fid not in threads_by_folio:
                    threads_by_folio[fid] = []
                threads_by_folio[fid].append(thread)
    except:
        threads_by_folio = {}

    # Filter by agent
    if agent:
        folios_list = [f for f in folios_list if agent.lower() in f.get('created_by', '').lower()]

    # Filter by grep
    if grep:
        folios_list = [f for f in folios_list if grep.lower() in f.get('content', '').lower()]

    # Filter by since/until
    if since or until:
        from datetime import datetime, timedelta
        import re

        def parse_time_filter(time_str):
            # Try relative format (1day, 2hours, etc.)
            match = re.match(r'^(\d+)(hour|day|week|minute)s?$', time_str)
            if match:
                num = int(match.group(1))
                unit = match.group(2)
                delta = {
                    'minute': timedelta(minutes=num),
                    'hour': timedelta(hours=num),
                    'day': timedelta(days=num),
                    'week': timedelta(weeks=num)
                }.get(unit, timedelta(days=num))
                return datetime.now() - delta
            # Try ISO format
            try:
                return datetime.fromisoformat(time_str.replace('Z', '+00:00'))
            except:
                return None

        if since:
            since_dt = parse_time_filter(since)
            if since_dt:
                folios_list = [f for f in folios_list if datetime.fromisoformat(f['created_at'].replace('Z', '+00:00')) >= since_dt]

        if until:
            until_dt = parse_time_filter(until)
            if until_dt:
                folios_list = [f for f in folios_list if datetime.fromisoformat(f['created_at'].replace('Z', '+00:00')) <= until_dt]

    # Follow thread connections
    if follow:
        # Find all folios connected via threads
        connected = set([follow])
        to_check = [follow]
        while to_check:
            current = to_check.pop()
            for thread in threads_by_folio.get(current, []):
                for fid in [thread['from_id'], thread['to_id']]:
                    if fid not in connected:
                        connected.add(fid)
                        to_check.append(fid)
        folios_list = [f for f in folios_list if f.get('folio_id') in connected]

    # Sort by date (newest first)
    folios_list.sort(key=lambda x: x.get('created_at', ''), reverse=True)

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
        folio_id = f.get('folio_id', 'unknown')
        folio_type = f.get('type', 'folio')
        site = f.get('site') or f.get('site_id') or ''
        agent_name = f.get('created_by', 'unknown')
        created_at = f.get('created_at', '')[:19].replace('T', ' ')
        content = f.get('content', '')
        status = f.get('status', 'open').upper()

        # Get first line of content, truncated
        first_line = content.split('\n')[0][:60]
        if len(content.split('\n')[0]) > 60:
            first_line += '...'

        # Thread count
        thread_count = len(threads_by_folio.get(folio_id, []))

        # Colors (like git: yellow for id only)
        yellow = '\033[33m'
        reset = '\033[0m'

        if oneline:
            site_str = f" ({site})" if site else ""
            output_lines.append(f"{yellow}{folio_type}-{folio_id.split('-', 1)[-1]}{reset}{site_str} {agent_name} {first_line}")
        else:
            site_str = f" ({site})" if site else ""
            output_lines.append(f"{yellow}folio {folio_type}-{folio_id.split('-', 1)[-1]}{site_str}{reset}")
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
        output_lines.append(f"(Showing {len(folios_list)} of {total_count} folios. Use -n to see more)")

    # Output with pager for TTY, plain for agents
    output_text = '\n'.join(output_lines)
    if is_tty and not no_pager:
        import subprocess
        try:
            proc = subprocess.Popen(['less', '-R'], stdin=subprocess.PIPE)
            proc.communicate(input=output_text.encode())
        except:
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

    data = {
        "site_id": site_id,
        "purpose": purpose,
        "metadata": {"tags": tag_list}
    }

    result = make_request("POST", "/sites", base_url, agent_id, json=data)
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
        click.echo(f"Created: {site_data['created_at']}")
        click.echo(f"By: {site_data['created_by']}")
        if site_data.get("metadata", {}).get("tags"):
            click.echo(f"Tags: {', '.join(site_data['metadata']['tags'])}")


@cli.command()
@click.option("--tag", help="Filter by tag")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def sites(ctx, tag, output_json):
    """List all sites."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    params = {}
    if tag:
        params["tag"] = tag

    sites_list = make_request("GET", "/sites", base_url, agent_id, params=params)

    if output_json:
        click.echo(json.dumps(sites_list, indent=2))
    else:
        if not sites_list:
            click.echo("No sites found")
        else:
            click.echo(f"Found {len(sites_list)} site(s):\n")
            for s in sites_list:
                click.echo(f"  {s['site_id']}")
                click.echo(f"    {s['purpose']}")
                click.echo()


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
                    if thread['from_id'] not in threads_by_resource:
                        threads_by_resource[thread['from_id']] = []
                    threads_by_resource[thread['from_id']].append(thread)

                    if thread['to_id'] not in threads_by_resource:
                        threads_by_resource[thread['to_id']] = []
                    threads_by_resource[thread['to_id']].append(thread)
            except:
                # Fall back to no threads if batch fetch fails
                threads_by_resource = {}

            for i in issues_list:
                click.echo(f"  {i['folio_id']}")
                click.echo(f"    {i['title']}")

                # Get threads from batch-fetched data
                try:
                    resource_threads = threads_by_resource.get(i['folio_id'], [])

                    # Dedupe threads (same thread appears in from_id and to_id indexes)
                    thread_ids = set()
                    unique_threads = []
                    for t in resource_threads:
                        if t['thread_id'] not in thread_ids:
                            thread_ids.add(t['thread_id'])
                            unique_threads.append(t)

                    # Extract tags (self-referential threads with type tag)
                    tags = [t['content'] for t in unique_threads
                           if t['type'] == 'tag' and t['from_id'] == t['to_id']]

                    # Build breadcrumb
                    breadcrumb_parts = []
                    breadcrumb_parts.append(f"Site: {i['site_id']}")
                    breadcrumb_parts.append(f"Status: {i['status']}")
                    if len(unique_threads) > 0:
                        breadcrumb_parts.append(f"{len(unique_threads)} threads")
                    if tags:
                        breadcrumb_parts.append(f"Tags: {', '.join(tags)}")

                    click.echo(f"    {' | '.join(breadcrumb_parts)}")
                except:
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
        click.echo(f"\nContent:")
        click.echo(brief_data['content'])

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
    """Create a playbook."""
    validate_positional_args(site_id, content, command_name="playbook create")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    data = {
        "type": "playbook",
        "site_id": site_id,
        "title": title or "Playbook",
        "content": content,
        "metadata": {}
    }

    result = make_request("POST", "/folios", base_url, agent_id, json=data)
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
        click.echo(f"\nContent:")
        click.echo(playbook_data['content'])


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

    if agent_id == "unknown":
        raise click.ClickException("Must set SKEIN_AGENT_ID or use --agent flag to ignite work")

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
        "content": f"Resuming work from {brief_id}"
    }
    thread_result = make_request("POST", "/threads", base_url, agent_id, json=succession_data)

    # Display brief
    click.echo(f"{'='*60}")
    click.echo(f"RESUMING: {brief_id}")
    click.echo(f"Predecessor: {predecessor}")
    click.echo(f"Site: {site_id}")
    click.echo(f"{'='*60}\n")
    click.echo(brief_data.get("content", ""))
    click.echo(f"\n{'='*60}")

    # Show threaded issues
    threads_data = make_request("GET", "/threads", base_url, agent_id, params={"from_id": brief_id})

    if threads_data:
        click.echo(f"\nThreaded work ({len(threads_data)} item(s)):")
        for t in threads_data:
            click.echo(f"  [{t['type'].upper()}] -> {t['to_id']}")

    click.echo(f"\n{'='*60}")
    click.echo("⚠️  BEFORE STARTING - Read Required Docs:")
    click.echo("  See CLAUDE.md for required reading list.")
    click.echo("  Common docs: PROJECT_CONTEXT.md, ARCHITECTURE.md, PRINCIPLES.md")
    click.echo("  Previous agents who skipped this produced incorrect work.")
    click.echo(f"\n{'='*60}")
    click.echo("Next steps:")
    click.echo(f"  1. Read required docs listed in CLAUDE.md")
    click.echo(f"  2. Review the brief above")
    click.echo(f"  3. Check site: skein --agent {agent_id} issues {site_id}")
    click.echo(f"  4. Check recent activity: skein --agent {agent_id} activity")
    click.echo(f"  5. Continue work from 'Remaining' section")
    click.echo(f"{'='*60}")


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
@click.option("--site", "-s", multiple=True, help="Site pattern(s) to search - supports wildcards (e.g., 'opus-*')")
@click.option("--type", "-t", help="Filter by folio type (issue, brief, friction, finding, summary, notion)")
@click.option("--status", help="Filter by status (open, closed, investigating)")
@click.option("--assigned", help="Filter by assignee")
@click.option("--since", help="Only items after this time (e.g., '1hour', '2days', ISO timestamp)")
@click.option("--sort", help="Sort by: created (default), created_asc, relevance")
@click.option("--limit", type=int, default=50, help="Max results (default: 50)")
@click.option("--all", "show_all", is_flag=True, help="Include archived folios")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def find(ctx, pattern, site, type, status, assigned, since, sort, limit, show_all, output_json):
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

    if show_all:
        params["archived"] = True

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
        click.echo(f"{'='*60}")
        click.echo(f"Site: {site_id} ({len(site_folios)} folio(s))")
        click.echo(f"{'='*60}")

        # Group by type within site
        by_type = {}
        for f in site_folios:
            folio_type = f['type']
            if folio_type not in by_type:
                by_type[folio_type] = []
            by_type[folio_type].append(f)

        for folio_type in sorted(by_type.keys()):
            click.echo(f"\n  {folio_type.upper()} ({len(by_type[folio_type])} item(s)):")
            for f in by_type[folio_type]:
                status_str = f"[{f.get('status', 'open')}]"
                # Format created_at date
                created_at = f.get('created_at', '')
                if created_at:
                    try:
                        dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                        date_str = dt.strftime('%Y-%m-%d')
                    except (ValueError, AttributeError):
                        date_str = created_at[:10] if len(created_at) >= 10 else created_at
                else:
                    date_str = ""

                click.echo(f"    {f['folio_id']} {status_str} {date_str}")
                click.echo(f"      {f.get('title', 'No title')[:80]}{'...' if len(f.get('title', '')) > 80 else ''}")

                # Show content preview
                content = f.get('content', '')
                if content:
                    preview = ' '.join(content.split())[:100]
                    if len(content) > 100:
                        preview += '...'
                    click.echo(f"      {preview}")

        click.echo()

    # Summary
    if len(folios) < total:
        click.echo(f"Showing {len(folios)} of {total} folios (use --limit to see more)")

    exec_time = response.get("execution_time_ms", 0)
    if exec_time:
        click.echo(f"(Search completed in {exec_time}ms)")


@cli.command(hidden=True)
@click.argument("query")
@click.option("--resources", help="Resource types to search (comma-separated: folios, threads, agents, sites). Default: folios")
@click.option("--type", help="Filter by type (issue, brief, summary, etc.)")
@click.option("--site", help="Filter by specific site (exact match)")
@click.option("--sites", multiple=True, help="Filter by site pattern(s) - supports wildcards (can be used multiple times)")
@click.option("--all-sites", is_flag=True, help="Search across all sites (default if no --site/--sites specified)")
@click.option("--status", help="Filter by status (open, closed)")
@click.option("--sort", help="Sort by: created (default), created_asc, relevance")
@click.option("--limit", type=int, help="Limit results per resource type (default: 50, max: 500)")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def search(ctx, query, resources, type, site, sites, all_sites, status, sort, limit, output_json):
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
                click.echo(f"  (searched across all sites)")
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

                    sites_count = len(by_site)
                    click.echo(f"📑 Folios ({folios_total} total, showing {len(folios)}):\n")

                    for site_id in sorted(by_site.keys()):
                        site_results = by_site[site_id]
                        click.echo(f"  📁 {site_id} ({len(site_results)} result(s)):")

                        for r in site_results[:10]:  # Limit per site
                            status_icon = "✓" if r.get("status") == "closed" else "○"
                            click.echo(f"    {status_icon} {r['type'].upper()}: {r.get('title', 'No title')[:60]}")
                            click.echo(f"       ID: {r['folio_id']}")

                        if len(site_results) > 10:
                            click.echo(f"       ... and {len(site_results) - 10} more in this site")

                        click.echo()

            # Display threads
            if "threads" in results_data:
                threads_data = results_data["threads"]
                threads = threads_data.get("items", [])
                threads_total = threads_data.get("total", 0)

                if threads:
                    click.echo(f"🧵 Threads ({threads_total} total, showing {len(threads)}):\n")
                    for t in threads[:20]:  # Show first 20 threads
                        click.echo(f"  {t['type']}: {t.get('content', 'No content')[:80]}")
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
                    click.echo(f"👤 Agents ({agents_total} total, showing {len(agents)}):\n")
                    for a in agents[:20]:  # Show first 20 agents
                        status_icon = "✓" if a.get("status") == "active" else "○"
                        caps = ", ".join(a.get("capabilities", [])) if a.get("capabilities") else "none"
                        click.echo(f"  {status_icon} {a['agent_id']}: {a.get('name', 'No name')}")
                        click.echo(f"    Type: {a.get('agent_type', 'unknown')} | Capabilities: {caps}\n")

                    if agents_total > 20:
                        click.echo(f"  ... and {agents_total - 20} more agents\n")

            # Display sites
            if "sites" in results_data:
                sites_data = results_data["sites"]
                sites_list = sites_data.get("items", [])
                sites_total = sites_data.get("total", 0)

                if sites_list:
                    click.echo(f"📍 Sites ({sites_total} total, showing {len(sites_list)}):\n")
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
    except:
        server_status = "unreachable"

    # Get project config
    project_config = get_project_config()
    project_name = project_config.get("project_id", "unknown") if project_config else "unknown"

    # Get all folios for counts
    try:
        all_folios = make_request("GET", "/folios", base_url, agent_id)
    except:
        all_folios = []

    # Count open issues and frictions
    open_issues = len([f for f in all_folios if f.get('type') == 'issue' and f.get('status', 'open') == 'open'])
    open_frictions = len([f for f in all_folios if f.get('type') == 'friction' and f.get('status', 'open') == 'open'])
    pending_briefs = len([f for f in all_folios if f.get('type') == 'brief' and f.get('status', 'open') == 'open'])

    # Count folios closed today via status threads
    closed_issues_today = 0
    closed_frictions_today = 0
    closed_today_total = 0
    try:
        # Get status threads with content "closed" from today
        status_threads = make_request("GET", "/threads", base_url, agent_id,
                                      params={"type": "status", "search": "closed", "since": "1day"})
        # Build lookup of folio types by ID
        folio_types = {f.get('folio_id'): f.get('type') for f in all_folios}
        for thread in status_threads:
            if thread.get('content') == 'closed':
                folio_id = thread.get('to_id')
                folio_type = folio_types.get(folio_id)
                closed_today_total += 1
                if folio_type == 'issue':
                    closed_issues_today += 1
                elif folio_type == 'friction':
                    closed_frictions_today += 1
    except:
        pass

    # Get folios from last hour and count by type
    from datetime import datetime, timedelta
    one_hour_ago = datetime.now() - timedelta(hours=1)

    recent_folios = []
    recent_agents = set()
    for f in all_folios:
        try:
            created = datetime.fromisoformat(f['created_at'].replace('Z', '+00:00').replace('+00:00', ''))
            if created >= one_hour_ago:
                recent_folios.append(f)
                recent_agents.add(f.get('created_by', 'unknown'))
        except:
            pass

    # Count by type for last hour
    type_counts = {}
    for f in recent_folios:
        ftype = f.get('type', 'other')
        type_counts[ftype] = type_counts.get(ftype, 0) + 1

    if output_json:
        click.echo(json.dumps({
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
            "last_hour": type_counts
        }, indent=2))
        return

    # Format output with colors and alignment
    yellow = '\033[33m'
    reset = '\033[0m'

    click.echo(f"Server:  {base_url} ({server_status})")
    click.echo(f"Project: {project_name}")
    click.echo()
    click.echo(f"Issues:     {yellow}{open_issues:>3}{reset} open / {closed_issues_today} closed today")
    click.echo(f"Frictions:  {yellow}{open_frictions:>3}{reset} open / {closed_frictions_today} closed today")
    click.echo(f"Briefs:     {yellow}{pending_briefs:>3}{reset} pending")
    click.echo(f"Closed today:   {yellow}{closed_today_total:>3}{reset}")
    click.echo()
    click.echo(f"Active agents:  {yellow}{len(recent_agents):>3}{reset}")
    click.echo()

    # Last hour summary
    if type_counts:
        # B=brief, I=issue, F=finding, R=friction, S=summary, T=tender, W=writ, P=playbook
        type_abbrev = {'brief': 'B', 'issue': 'I', 'finding': 'F', 'friction': 'R', 'summary': 'S', 'tender': 'T', 'writ': 'W', 'playbook': 'P'}
        parts = []
        for ftype, count in sorted(type_counts.items()):
            abbrev = type_abbrev.get(ftype, ftype[0].upper())
            parts.append(f"{count}{abbrev}")
        click.echo(f"Last hour: {' '.join(parts)}")
    else:
        click.echo("Last hour: (no activity)")


@cli.command()
@click.option("--since", help="Time filter (1hour, 2days, or ISO timestamp)")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def activity(ctx, since, output_json):
    """Get recent activity."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    params = {}
    if since:
        params["since"] = since

    activity_data = make_request("GET", "/activity", base_url, agent_id, params=params)

    if output_json:
        click.echo(json.dumps(activity_data, indent=2))
    else:
        click.echo(f"Recent activity:\n")
        click.echo(f"New folios: {len(activity_data.get('new_folios', []))}")
        click.echo(f"Active agents: {len(activity_data.get('active_agents', []))}")

        if activity_data.get("new_folios"):
            click.echo("\nRecent folios:")
            for f in activity_data["new_folios"][:10]:
                click.echo(f"  {f['type'].upper()}: {f['title']} ({f['folio_id']})")


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

    Example:
        skein post issue skein-dev "Fix login bug" --content "Users can't login with OAuth"
    """
    validate_positional_args(site_id, title, command_name="post issue")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    data = {
        "type": "issue",
        "site_id": site_id,
        "title": title,
        "content": content or title,
        "assigned_to": assign,
        "metadata": {}
    }

    result = make_request("POST", "/folios", base_url, agent_id, json=data)
    click.echo(f"Created issue: {result['folio_id']}")


@post.command("brief")
@click.argument("site_id")
@click.argument("content")
@click.option("--title", required=True, help="Brief title (required)")
@click.option("--target", help="Target agent")
@click.pass_context
def post_brief(ctx, site_id, content, title, target):
    """Post a handoff brief.

    Example:
        skein post brief skein-dev "Implement dark mode toggle" --title "Dark mode feature"
    """
    validate_positional_args(site_id, content, command_name="post brief")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    data = {
        "type": "brief",
        "site_id": site_id,
        "title": title,
        "content": content,
        "target_agent": target,
        "metadata": {"questions_enabled": True}
    }

    result = make_request("POST", "/folios", base_url, agent_id, json=data)
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

    Example:
        skein post friction skein-dev "Must restart server after config changes"
    """
    validate_positional_args(site_id, title, command_name="post friction")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    data = {
        "type": "friction",
        "site_id": site_id,
        "title": title,
        "content": details or title,
        "metadata": {}
    }

    result = make_request("POST", "/folios", base_url, agent_id, json=data)
    click.echo(f"Logged friction: {result['folio_id']}")


@post.command("notion")
@click.argument("site_id")
@click.argument("title")
@click.option("--details", "-d", help="Additional details (title used if not provided)")
@click.pass_context
def post_notion(ctx, site_id, title, details):
    """Post a notion (rough idea not fully formed).

    Example:
        skein post notion skein-dev "Could use websockets for real-time updates"
    """
    validate_positional_args(site_id, title, command_name="post notion")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    data = {
        "type": "notion",
        "site_id": site_id,
        "title": title,
        "content": details or title,
        "metadata": {}
    }

    result = make_request("POST", "/folios", base_url, agent_id, json=data)
    click.echo(f"Posted notion: {result['folio_id']}")


@post.command("finding")
@click.argument("site_id")
@click.argument("title")
@click.option("--details", "-d", help="Additional details (title used if not provided)")
@click.pass_context
def post_finding(ctx, site_id, title, details):
    """Post a finding (discovery during investigation).

    Example:
        skein post finding skein-dev "Redis caching reduces latency by 40%"
    """
    validate_positional_args(site_id, title, command_name="post finding")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    data = {
        "type": "finding",
        "site_id": site_id,
        "title": title,
        "content": details or title,
        "metadata": {}
    }

    result = make_request("POST", "/folios", base_url, agent_id, json=data)
    click.echo(f"Posted finding: {result['folio_id']}")


@post.command("summary")
@click.argument("site_id")
@click.argument("title")
@click.option("--details", "-d", help="Additional details (title used if not provided)")
@click.pass_context
def post_summary(ctx, site_id, title, details):
    """Post a summary (completed work findings).

    Example:
        skein post summary skein-dev "Completed OAuth integration"
    """
    validate_positional_args(site_id, title, command_name="post summary")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    data = {
        "type": "summary",
        "site_id": site_id,
        "title": title,
        "content": details or title,
        "metadata": {}
    }

    result = make_request("POST", "/folios", base_url, agent_id, json=data)
    click.echo(f"Posted summary: {result['folio_id']}")


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
        "title": name or content[:100],  # Use name if provided, else first 100 chars
        "content": content,
        "metadata": {}
    }

    result = make_request("POST", "/folios", base_url, agent_id, json=data)
    click.echo(f"Created mantle: {result['folio_id']}")


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
        "title": name or content[:100],  # Use name if provided, else first 100 chars
        "content": content,
        "metadata": {}
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
@click.option("--thread", "thread_id", help="Tender ID to respond to (updates tender status to 'responded')")
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
                raise click.ClickException(f"{thread_id} is not a tender (type: {tender.get('type')})")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                raise click.ClickException(f"Tender not found: {thread_id}")
            raise

    # Create writ folio
    data = {
        "type": "writ",
        "site_id": site_id,
        "title": decision[:100],
        "content": decision,
        "metadata": {"thread_id": thread_id} if thread_id else {}
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
            "content": decision
        }
        make_request("POST", "/threads", base_url, agent_id, json=thread_data)

        # Update tender status to 'responded'
        status_data = {
            "from_id": thread_id,
            "to_id": thread_id,
            "type": "status",
            "content": "responded"
        }
        make_request("POST", "/threads", base_url, agent_id, json=status_data)
        click.echo(f"  Linked to tender: {thread_id}")
        click.echo(f"  Tender status: responded")


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


@cli.command()
@click.argument("folio_id")
@click.option("--no-pager", is_flag=True, help="Disable pager")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def folio(ctx, folio_id, no_pager, output_json):
    """Read a single folio by ID."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    folio_data = make_request("GET", f"/folios/{folio_id}", base_url, agent_id)

    if output_json:
        click.echo(json.dumps(folio_data, indent=2))
        return

    # Detect TTY for pager
    is_tty = sys.stdout.isatty()

    # Colors
    yellow = '\033[33m'
    reset = '\033[0m'

    # Build output
    output_lines = []
    fid = folio_data.get('folio_id', 'unknown')
    ftype = folio_data.get('type', 'folio')
    site = folio_data.get('site') or ''
    site_str = f" ({site})" if site else ""

    output_lines.append(f"{yellow}folio {ftype}-{fid.split('-', 1)[-1]}{site_str}{reset}")
    output_lines.append(f"Agent: {folio_data.get('created_by', 'unknown')}")
    output_lines.append(f"Date:  {folio_data.get('created_at', '')[:19].replace('T', ' ')}")
    if folio_data.get('status'):
        output_lines.append(f"Status: {folio_data.get('status')}")
    output_lines.append("")

    # Full content with indentation
    content = folio_data.get('content', '')
    for line in content.split('\n'):
        output_lines.append(f"    {line}")

    # Get thread connections
    try:
        all_threads = make_request("GET", "/threads", base_url, agent_id)
        related_threads = [t for t in all_threads if fid in [t['from_id'], t['to_id']]]
        if related_threads:
            output_lines.append("")
            output_lines.append(f"    Threads ({len(related_threads)}):")
            for t in related_threads:
                other_id = t['to_id'] if t['from_id'] == fid else t['from_id']
                output_lines.append(f"      → {other_id}")
    except:
        pass

    # Output with pager for TTY
    output_text = '\n'.join(output_lines)
    if is_tty and not no_pager:
        import subprocess
        try:
            proc = subprocess.Popen(['less', '-R'], stdin=subprocess.PIPE)
            proc.communicate(input=output_text.encode())
        except:
            click.echo(output_text)
    else:
        click.echo(output_text)


# Alias: skein show -> skein folio
@cli.command("show")
@click.argument("folio_id")
@click.option("--no-pager", is_flag=True, help="Disable pager")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def show(ctx, folio_id, no_pager, output_json):
    """Read a single folio by ID (alias for 'folio')."""
    ctx.invoke(folio, folio_id=folio_id, no_pager=no_pager, output_json=output_json)


@cli.command()
@click.argument("folio_id")
@click.option("--format", "-f", "output_format", type=click.Choice(["epub", "md", "markdown", "json"]), default="epub", help="Export format (default: epub)")
@click.option("--output", "-o", help="Output file path (default: ./<folio_id>.<format>)")
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
            zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)

            # container.xml
            container_xml = '''<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>'''
            zf.writestr("META-INF/container.xml", container_xml)

            # CSS
            css_content = '''body {
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
'''
            zf.writestr("OEBPS/styles.css", css_content)

            # Content XHTML with metadata
            escaped_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            content_xhtml = f'''<?xml version="1.0" encoding="UTF-8"?>
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
{f'<p><strong>Status:</strong> {status}</p>' if status else ''}
</div>
<hr/>
{html_content}
</body>
</html>'''
            zf.writestr("OEBPS/content.xhtml", content_xhtml)

            # content.opf
            now = dt.now().strftime("%Y-%m-%dT%H:%M:%SZ")
            content_opf = f'''<?xml version="1.0" encoding="UTF-8"?>
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
</package>'''
            zf.writestr("OEBPS/content.opf", content_opf)

            # Navigation document
            nav_xhtml = f'''<?xml version="1.0" encoding="UTF-8"?>
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
</html>'''
            zf.writestr("OEBPS/nav.xhtml", nav_xhtml)

        click.echo(f"Exported to {output}")
        return


def _content_to_epub_html(content, title):
    """Convert markdown-like content to HTML for epub export."""
    import re

    def escape_xml(text):
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    def format_inline(text):
        text = escape_xml(text)
        text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
        text = re.sub(r'(?<!\*)\*([^*]+?)\*(?!\*)', r'<em>\1</em>', text)
        text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
        return text

    lines = content.split('\n')
    html_parts = []
    in_code_block = False
    in_list = False
    list_type = None
    table_rows = []

    for line in lines:
        stripped = line.strip()

        # Code blocks
        if stripped.startswith('```'):
            if in_code_block:
                html_parts.append('</code></pre>')
                in_code_block = False
            else:
                html_parts.append('<pre><code>')
                in_code_block = True
            continue

        if in_code_block:
            html_parts.append(escape_xml(line))
            continue

        # Close list if not a list item
        is_list_item = (stripped.startswith('- ') or stripped.startswith('* ') or
                       (stripped and stripped[0].isdigit() and '. ' in stripped))
        if in_list and not is_list_item and stripped:
            html_parts.append(f'</{list_type}>')
            in_list = False
            list_type = None

        # Empty lines - close table if any
        if not stripped:
            if table_rows:
                html_parts.append(_build_table(table_rows))
                table_rows = []
            continue

        # Headers
        if line.startswith('### '):
            html_parts.append(f'<h3>{escape_xml(line[4:])}</h3>')
            continue
        if line.startswith('## '):
            html_parts.append(f'<h2>{escape_xml(line[3:])}</h2>')
            continue
        if line.startswith('# '):
            html_parts.append(f'<h1>{escape_xml(line[2:])}</h1>')
            continue

        # Tables
        if '|' in line and stripped.startswith('|') and stripped.endswith('|'):
            cells = [c.strip() for c in line.split('|')[1:-1]]
            if '---' in line:
                continue
            table_rows.append(cells)
            continue

        # Lists
        if stripped.startswith('- ') or stripped.startswith('* '):
            if not in_list:
                html_parts.append('<ul>')
                in_list = True
                list_type = 'ul'
            html_parts.append(f'<li>{format_inline(stripped[2:])}</li>')
            continue

        if stripped and stripped[0].isdigit() and '. ' in stripped:
            if not in_list:
                html_parts.append('<ol>')
                in_list = True
                list_type = 'ol'
            item_content = stripped.split('. ', 1)[1] if '. ' in stripped else stripped
            html_parts.append(f'<li>{format_inline(item_content)}</li>')
            continue

        # Regular paragraph
        if stripped:
            html_parts.append(f'<p>{format_inline(line)}</p>')

    # Close open elements
    if in_list:
        html_parts.append(f'</{list_type}>')
    if table_rows:
        html_parts.append(_build_table(table_rows))
    if in_code_block:
        html_parts.append('</code></pre>')

    return '\n'.join(html_parts)


def _build_table(rows):
    """Build HTML table from rows."""
    if not rows:
        return ''

    def format_inline(text):
        import re
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
        return text

    html = ['<table>']
    html.append('<tr>' + ''.join(f'<th>{format_inline(c)}</th>' for c in rows[0]) + '</tr>')
    for row in rows[1:]:
        html.append('<tr>' + ''.join(f'<td>{format_inline(c)}</td>' for c in row) + '</tr>')
    html.append('</table>')
    return '\n'.join(html)


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
        "PATCH",
        f"/folios/{folio_id}",
        base_url,
        agent_id,
        json=update_data
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


@cli.command(hidden=True)
@click.argument("site_id")
@click.option("--type", help="Filter by folio type")
@click.option("--status", help="Filter by status")
@click.option("-n", "--limit", type=int, help="Limit number of folios shown (default: 20 for agents, unlimited for TTY)")
@click.option("--all", "show_all", is_flag=True, help="Show all folios (override default limit)")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def folios(ctx, site_id, type, status, limit, show_all, output_json):
    """List all folios in a site. (Deprecated: use 'find --site SITE_ID')"""
    # Validate site_id is not empty
    if not site_id or site_id.strip() == "":
        raise click.ClickException("site_id cannot be empty. Usage: skein folios SITE_ID")

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
                click.echo(f"Showing {showing_count} of {total_count} folio(s) in site {site_id}:\n")
            else:
                click.echo(f"Found {total_count} folio(s) in site {site_id}:\n")

            # Group by type for better readability
            by_type = {}
            for f in folios_list:
                folio_type = f['type']
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
                    if thread['from_id'] not in threads_by_resource:
                        threads_by_resource[thread['from_id']] = []
                    threads_by_resource[thread['from_id']].append(thread)

                    if thread['to_id'] not in threads_by_resource:
                        threads_by_resource[thread['to_id']] = []
                    threads_by_resource[thread['to_id']].append(thread)
            except:
                # Fall back to no threads if batch fetch fails
                threads_by_resource = {}

            for folio_type in sorted(by_type.keys()):
                click.echo(f"  {folio_type.upper()} ({len(by_type[folio_type])} item(s)):")
                for f in by_type[folio_type]:
                    status_str = f"[{f['status']}]" if f.get('status') else ""
                    click.echo(f"    {f['folio_id']} {status_str}")
                    click.echo(f"      {f['title']}")

                    # Get threads from batch-fetched data
                    try:
                        resource_threads = threads_by_resource.get(f['folio_id'], [])

                        # Dedupe threads (same thread appears in from_id and to_id indexes)
                        thread_ids = set()
                        unique_threads = []
                        for t in resource_threads:
                            if t['thread_id'] not in thread_ids:
                                thread_ids.add(t['thread_id'])
                                unique_threads.append(t)

                        # Extract tags (self-referential threads with type tag)
                        tags = [t['content'] for t in unique_threads
                               if t['type'] == 'tag' and t['from_id'] == t['to_id']]

                        # Build breadcrumb
                        breadcrumb_parts = []
                        if len(unique_threads) > 0:
                            breadcrumb_parts.append(f"{len(unique_threads)} threads")
                        if tags:
                            breadcrumb_parts.append(f"Tags: {', '.join(tags)}")

                        if breadcrumb_parts:
                            click.echo(f"      {' | '.join(breadcrumb_parts)}")
                    except:
                        # Silently skip if thread processing fails
                        pass

                    if f.get('assigned_to'):
                        click.echo(f"      Assigned: {f['assigned_to']}")
                click.echo()

            # Show truncation hint if limited
            if showing_count < total_count:
                remaining = total_count - showing_count
                click.echo(f"({remaining} more folios, use --all or -n {total_count} to see all)")


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
            raise click.ClickException("site_id cannot be empty. Usage: skein survey SITE_ID [SITE_ID...]")

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

            folios_list = make_request("GET", "/folios", base_url, agent_id, params=params)
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
            "errors": [{"site_id": s, "error": e} for s, e in errors]
        }
        click.echo(json.dumps(output, indent=2))
    else:
        # Human-readable output
        click.echo(f"Surveying {len(site_ids)} site(s)...\n")

        for site_id in site_ids:
            folios = all_results[site_id]

            click.echo(f"{'='*60}")
            click.echo(f"Site: {site_id}")
            click.echo(f"{'='*60}")

            if site_id in [s for s, _ in errors]:
                error_msg = next(e for s, e in errors if s == site_id)
                click.echo(f"  ❌ Error: {error_msg}\n")
                continue

            if not folios:
                click.echo(f"  No folios found\n")
                continue

            click.echo(f"  Found {len(folios)} folio(s)\n")

            # Group by type
            by_type = {}
            for f in folios:
                folio_type = f['type']
                if folio_type not in by_type:
                    by_type[folio_type] = []
                by_type[folio_type].append(f)

            for folio_type in sorted(by_type.keys()):
                click.echo(f"  {folio_type.upper()} ({len(by_type[folio_type])} item(s)):")
                for f in by_type[folio_type]:
                    status_str = f"[{f['status']}]" if f.get('status') else ""
                    # Format created_at date
                    created_at = f.get('created_at', '')
                    if created_at:
                        # Parse ISO format and display as YYYY-MM-DD
                        try:
                            dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                            date_str = dt.strftime('%Y-%m-%d')
                        except (ValueError, AttributeError):
                            date_str = created_at[:10] if len(created_at) >= 10 else created_at
                    else:
                        date_str = ""

                    click.echo(f"    {f['folio_id']} {status_str} {date_str}")
                    click.echo(f"      {f['title'][:80]}{'...' if len(f['title']) > 80 else ''}")

                    # Show content preview (first 100 chars, single line)
                    content = f.get('content', '')
                    if content:
                        # Clean up content: replace newlines with spaces, truncate
                        preview = ' '.join(content.split())[:100]
                        if len(content) > 100:
                            preview += '...'
                        click.echo(f"      {preview}")
                click.echo()

        click.echo(f"{'='*60}")
        click.echo(f"Total: {total_folios} folio(s) across {len(site_ids)} site(s)")
        if errors:
            click.echo(f"Errors: {len(errors)} site(s) failed")
        click.echo(f"{'='*60}")


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

    data = {
        "from_id": agent_id,
        "to_id": to_id,
        "type": "message",
        "content": message
    }

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
def threads(ctx, resource_id, from_filter, to_filter, type_filter, weaver, search, since, output_json):
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
        from_threads = make_request("GET", "/threads", base_url, agent_id, params={"from_id": resource_id})
        to_threads = make_request("GET", "/threads", base_url, agent_id, params={"to_id": resource_id})
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
        threads_list = make_request("GET", "/threads", base_url, agent_id, params=params)

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
@click.option("--depth", type=int, default=3, help="Maximum depth to traverse (default: 3)")
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
        from_threads = make_request("GET", "/threads", base_url, agent_id,
                                    params={"from_id": res_id})
        to_threads = make_request("GET", "/threads", base_url, agent_id,
                                  params={"to_id": res_id})

        # Combine and dedupe
        all_threads = from_threads + to_threads
        seen = set()
        unique = []
        for t in all_threads:
            if t['thread_id'] not in seen:
                seen.add(t['thread_id'])
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

        node = {
            "id": res_id,
            "threads": [],
            "children": []
        }

        for thread in threads:
            thread_info = {
                "thread_id": thread["thread_id"],
                "type": thread["type"],
                "from_id": thread["from_id"],
                "to_id": thread["to_id"],
                "content": thread.get("content", "")[:100]
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
                is_last_thread = (i == len(node["threads"]) - 1) and not node["children"]
                thread_connector = "└── " if is_last_thread else "├── "

                direction = "→" if thread["from_id"] == node["id"] else "←"
                other_id = thread["to_id"] if thread["from_id"] == node["id"] else thread["from_id"]

                click.echo(f"{thread_prefix}{thread_connector}[{thread['type'].upper()}] {direction} {other_id}")
                if thread.get("content"):
                    content_prefix = thread_prefix + ("    " if is_last_thread else "│   ")
                    click.echo(f"{content_prefix}  \"{thread['content']}\"")

            # Print children
            for i, child in enumerate(node["children"]):
                is_last_child = (i == len(node["children"]) - 1)
                child_prefix = prefix + ("    " if is_last else "│   ")
                print_tree(child, child_prefix, is_last_child)

        click.echo(f"\nThread tree for {resource_id}:\n")
        print_tree(tree)
        click.echo()


@cli.command()
@click.option("--unread", is_flag=True, help="Only show unread items")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def inbox(ctx, unread, output_json):
    """Check your inbox (threads to you)."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    if agent_id == "unknown":
        raise click.ClickException("Must set SKEIN_AGENT_ID or use --agent flag to check inbox")

    params = {}
    if unread:
        params["unread"] = "true"

    threads_list = make_request("GET", "/inbox", base_url, agent_id, params=params)

    if output_json:
        click.echo(json.dumps(threads_list, indent=2))
    else:
        if not threads_list:
            click.echo("Inbox empty")
        else:
            click.echo(f"Inbox ({len(threads_list)} item(s)):\n")
            for t in threads_list:
                status = "[UNREAD]" if not t.get("read_at") else "[read]"
                click.echo(f"  {status} [{t['type'].upper()}] From {t['from_id']}")
                if t.get("content"):
                    click.echo(f"    {t['content'][:200]}")
                click.echo(f"    ID: {t['thread_id']}")
                click.echo()


@cli.command("mark-read")
@click.argument("thread_id")
@click.pass_context
def mark_read(ctx, thread_id):
    """Mark a thread as read."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    result = make_request("PATCH", f"/threads/{thread_id}/read", base_url, agent_id)
    if result.get("success"):
        click.echo(f"Marked thread {thread_id} as read")
    else:
        click.echo(f"Failed to mark thread {thread_id} as read")


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
        if from_id == "unknown":
            raise click.ClickException("Must set agent ID to use default FROM_ID")

    data = {
        "from_id": from_id,
        "to_id": to_id,
        "type": thread_type,
        "content": content
    }

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

    if agent_id == "unknown":
        raise click.ClickException("Must set SKEIN_AGENT_ID or use --agent flag to reply")

    data = {
        "from_id": agent_id,
        "to_id": to_id,
        "type": "reply",
        "content": message
    }

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
        "content": tag_name
    }

    result = make_request("POST", "/threads", base_url, agent_id, json=data)
    click.echo(f"Tagged {resource_id} as '{tag_name}'")


@cli.command()
@click.argument("resource_id")
@click.argument("status_value", type=click.Choice(["open", "closed", "investigating", "resolved", "blocked", "in-progress"]))
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

    if agent_id == "unknown":
        raise click.ClickException("Must set SKEIN_AGENT_ID or use --agent flag to set status")

    data = {
        "from_id": agent_id,
        "to_id": resource_id,
        "type": "status",
        "content": status_value
    }

    result = make_request("POST", "/threads", base_url, agent_id, json=data)
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

    if agent_id == "unknown":
        raise click.ClickException("Must set SKEIN_AGENT_ID or use --agent flag to close")

    for resource_id in resource_ids:
        # Create status thread (closed)
        status_data = {
            "from_id": resource_id,
            "to_id": resource_id,
            "type": "status",
            "content": "closed"
        }
        make_request("POST", "/threads", base_url, agent_id, json=status_data)
        click.echo(f"Closed {resource_id}")

        # Create reference thread if --link provided
        if link:
            ref_content = note if note else "Resolved"
            ref_data = {
                "from_id": resource_id,
                "to_id": link,
                "type": "reference",
                "content": ref_content
            }
            make_request("POST", "/threads", base_url, agent_id, json=ref_data)
            click.echo(f"Linked to {link}: {ref_content}")


@cli.command()
@click.option("--capabilities", help="Comma-separated capabilities")
@click.option("--name", help="Human-readable name (e.g., 'Front End Developer', 'Race Condition Fixer')")
@click.option("--type", "agent_type", type=click.Choice(["claude-code", "patbot", "horizon", "human", "system"]), help="Agent type")
@click.option("--description", help="Longer description of work and focus")
@click.pass_context
def register(ctx, capabilities, name, agent_type, description):
    """Register in the roster."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    if agent_id == "unknown":
        raise click.ClickException("Must set SKEIN_AGENT_ID or use --agent flag to register")

    caps_list = [c.strip() for c in capabilities.split(",")] if capabilities else []

    data = {
        "agent_id": agent_id,
        "capabilities": caps_list,
        "metadata": {}
    }

    if name:
        data["name"] = name
    if agent_type:
        data["agent_type"] = agent_type
    if description:
        data["description"] = description

    result = make_request("POST", "/roster/register", base_url, agent_id, json=data)
    click.echo(f"Registered: {agent_id}")
    if name:
        click.echo(f"Name: {name}")
    if agent_type:
        click.echo(f"Type: {agent_type}")
    if caps_list:
        click.echo(f"Capabilities: {', '.join(caps_list)}")
    if description:
        click.echo(f"Description: {description}")


@cli.command()
@click.option("--json", "output_json", is_flag=True)
@click.option("--status", default="active", help="Filter: active (default), retired, or all")
@click.option("--all", "show_all", is_flag=True, help="Include retired agents (same as --status all)")
@click.pass_context
def roster(ctx, output_json, status, show_all):
    """List registered agents."""
    # --all flag overrides --status to show all
    if show_all:
        status = "all"
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    # Build query params - if "all", don't filter
    params = {}
    if status != "all":
        params["status"] = status

    agents = make_request("GET", "/roster", base_url, agent_id, params=params)

    if output_json:
        click.echo(json.dumps(agents, indent=2))
    else:
        if not agents:
            if status == "all":
                click.echo("No agents registered")
            else:
                click.echo(f"No {status} agents")
        else:
            if status == "all":
                click.echo(f"Roster ({len(agents)} agent(s)):\n")
            else:
                click.echo(f"Roster ({len(agents)} {status} agent(s)):\n")
            for a in agents:
                click.echo(f"  {a['agent_id']}")
                if a.get("name") and a.get("name") != a.get("agent_id"):
                    click.echo(f"    Name: {a['name']}")
                if a.get("agent_type"):
                    click.echo(f"    Type: {a['agent_type']}")
                if a.get("description"):
                    click.echo(f"    Description: {a['description']}")
                if a.get("capabilities"):
                    click.echo(f"    Capabilities: {', '.join(a['capabilities'])}")
                click.echo(f"    Registered: {a['registered_at']}")
                click.echo()


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
        skein ready --name "Your Name"
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
    except:
        return set()


def _generate_suggested_name(
    base_url: str,
    agent_id: str,
    mantle: Optional[str],
    mantle_data: Optional[dict],
    brief_content: str = ""
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
        skein ready --name "Your Name"
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)
    # agent_id may be "unknown" - that's fine, identity comes at ready

    # Prepare response data
    response = {
        "status": "orienting",
        "agent_id": agent_id,
        "mission": None,
        "brief_id": brief_id,
        "mantle_name": mantle,
        "message": message
    }

    mission_parts = []
    brief_content = ""

    # If brief provided, load it
    if brief_id:
        try:
            brief = make_request("GET", f"/folios/{brief_id}", base_url, agent_id)
            brief_content = brief.get('content', '')
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
                mantle_folio = make_request("GET", f"/folios/{mantle}", base_url, agent_id)
            else:
                # Search for mantle by name using the /search endpoint
                search_response = make_request("GET", "/search", base_url, agent_id, params={"q": mantle, "type": "mantle"})
                folios_data = search_response.get("results", {}).get("folios", {})
                results = folios_data.get("items", [])
                if not results:
                    raise click.ClickException(f"No mantle found matching '{mantle}'")
                # Prefer exact title match, otherwise take first result
                mantle_folio = None
                for r in results:
                    if r.get('title', '').lower() == mantle.lower():
                        mantle_folio = r
                        break
                if not mantle_folio:
                    mantle_folio = results[0]
                    if len(results) > 1:
                        click.echo(f"Note: Multiple mantles match '{mantle}', using '{mantle_folio.get('title', mantle_folio.get('folio_id'))}'", err=True)
            mantle_content = mantle_folio.get('content', '')
            mission_parts.append(f"**From Mantle ({mantle}):**\n{mantle_content}")
            # Store folio data for naming context
            mantle_data = {"content": mantle_content}
        except click.ClickException:
            raise
        except Exception as e:
            raise click.ClickException(f"Failed to load mantle folio '{mantle}': {str(e)}")

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
            "docs/ARCHITECTURE.md"
        ]
        # Conditional docs
        conditional_docs = [
            "docs/TESTING_GUIDE.md",
            "docs/HORIZON_EXAMPLE.md",
            "docs/TOOL_CREATION_GUIDE.md",
            "docs/AGENT_CREATION_GUIDE.md",
            "docs/SKEIN_AGENT_GUIDE.md",
            "docs/TOKEN_TERMINOLOGY.md"
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
    suggested_name = _generate_suggested_name(base_url, agent_id, mantle, mantle_data, naming_context)
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
                "message": message
            }
        }
        make_request("POST", "/roster/register", base_url, suggested_name, json=register_data)
    except Exception as e:
        # Log but don't fail - registration is not critical
        click.echo(f"Note: Could not register on roster: {e}", err=True)

    # Output results
    click.echo("="*60)
    click.echo("IGNITION - Orientation Phase")
    click.echo("="*60)
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
        core_docs = ["CLAUDE.md", "PROJECT_CONTEXT.md", "SKEIN_QUICK_START.md", "ARCHITECTURE.md"]
        for doc in core_docs:
            if any(doc in s for s in suggested_reading):
                matching = [s for s in suggested_reading if doc in s][0]
                click.echo(f"├── {doc}")
                suggested_reading.remove(matching)

        # Conditional docs
        testing_docs = ["TESTING_GUIDE.md"]
        system_docs = ["HORIZON_EXAMPLE.md", "TOOL_CREATION_GUIDE.md", "AGENT_CREATION_GUIDE.md",
                       "SKEIN_AGENT_GUIDE.md", "TOKEN_TERMINOLOGY.md"]

        has_testing = any(any(td in s for s in suggested_reading) for td in testing_docs)
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
                        click.echo(f"{prefix} {doc} (use Payload/Burn/Creep terms when discussing tokens to disambiguate in discussion of token use)")
                    else:
                        click.echo(f"{prefix} {doc}")

        click.echo()

    click.echo(f"You are: {suggested_name}")
    click.echo()
    click.echo("After reading, explore project files and the SKEIN for relevant information. After you've fully oriented, run:")
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
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = ctx.obj.get("agent")

    if not agent_id:
        raise click.ClickException("Must use --agent flag. Run 'skein ignite' first to get your assigned name.")

    # Update agent status from orienting to active
    data = {
        "agent_id": agent_id,
        "name": agent_id,
        "status": "active",
        "metadata": {
            "ready_at": datetime.now().isoformat()
        }
    }

    try:
        make_request("POST", "/roster/register", base_url, agent_id, json=data)
    except Exception as e:
        raise click.ClickException(f"Failed to activate: {str(e)}")

    click.echo("="*60)
    click.echo("READY")
    click.echo("="*60)
    click.echo()
    click.echo(f"You are: {agent_id}")
    click.echo()
    click.echo("Use this for all commands:")
    click.echo(f"  skein --agent {agent_id} issue SITE \"description\"")
    click.echo(f"  skein --agent {agent_id} finding SITE \"discovery\"")
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

    if agent_id == "unknown":
        raise click.ClickException("Must set SKEIN_AGENT_ID or use --agent flag to torch")

    # Get roster info
    try:
        roster_data = make_request("GET", f"/roster/{agent_id}", base_url, agent_id)
        name = roster_data.get("name", agent_id)
    except:
        raise click.ClickException(f"Agent {agent_id} not found in roster. Must ignite before torching.")

    # Get agent's SKEIN activity
    try:
        # Get all folios by this agent
        all_folios = make_request("GET", "/folios", base_url, agent_id)
        agent_folios = [f for f in all_folios if f.get("author") == agent_id or f.get("weaver") == agent_id]

        # Count by type
        work_summary = {
            "issues": len([f for f in agent_folios if f.get("type") == "issue"]),
            "findings": len([f for f in agent_folios if f.get("type") == "finding"]),
            "plans": len([f for f in agent_folios if f.get("type") == "plan"]),
            "briefs": len([f for f in agent_folios if f.get("type") == "brief"]),
            "notions": len([f for f in agent_folios if f.get("type") == "notion"]),
            "frictions": len([f for f in agent_folios if f.get("type") == "friction"]),
            "summaries": len([f for f in agent_folios if f.get("type") == "summary"])
        }
    except:
        work_summary = {}

    # Update status to retiring (if server supports it)
    try:
        # Try to update via re-registration with new status
        update_data = {
            "agent_id": agent_id,
            "name": name,
            "status": "retiring"
        }
        make_request("POST", "/roster/register", base_url, agent_id, json=update_data)
    except:
        pass  # Continue even if update fails (server might not support status)

    click.echo("="*60)
    click.echo("TORCH - Retirement Phase")
    click.echo("="*60)
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
        assignment_threads = [t for t in all_threads
                            if t.get("type") == "assignment" and t.get("to_id") == agent_id]
        assigned_folio_ids = [t.get("from_id") for t in assignment_threads]

        if assigned_folio_ids:
            # Get all open issues and frictions
            open_issues_all = make_request("GET", "/folios", base_url, agent_id,
                                          params={"type": "issue", "status": "open"})
            open_frictions_all = make_request("GET", "/folios", base_url, agent_id,
                                             params={"type": "friction", "status": "open"})

            # Filter to only those assigned to this agent
            open_issues = [i for i in open_issues_all if i.get("folio_id") in assigned_folio_ids]
            open_frictions = [f for f in open_frictions_all if f.get("folio_id") in assigned_folio_ids]

        # Check if agent was ignited from a brief and if it's still open
        try:
            roster_entry = make_request("GET", f"/roster/{agent_id}", base_url, agent_id)
            ignited_from = roster_entry.get("metadata", {}).get("ignited_from")
            if ignited_from and ignited_from.startswith("brief-"):
                # Get brief status
                all_briefs = make_request("GET", "/folios", base_url, agent_id,
                                         params={"type": "brief"})
                brief = next((b for b in all_briefs if b.get("folio_id") == ignited_from), None)
                if brief and brief.get("status") == "open":
                    ignited_from_brief = brief
                    brief_is_open = True
        except:
            pass
    except:
        # Continue even if we can't fetch open work
        pass

    # Display open work if any exists
    if open_issues or open_frictions or brief_is_open:
        click.echo("="*60)
        click.echo("YOUR OPEN WORK")
        click.echo("="*60)
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
    click.echo("  • Is there incomplete work? File brief(s) if someone should continue.")
    click.echo("  • Did you have larger ideas or patterns worth sharing? File notion(s).")
    click.echo("  • Did you encounter friction or blockers? File friction(s).")
    click.echo("  • Do you know of completed work that should be closed? Close it.")
    click.echo()
    click.echo("Examples:")
    click.echo("  skein close issue-20251112-757o --link summary-20251112-5lut")
    click.echo("  skein close friction-20251109-1lfe --note \"Fixed by refactoring imports\"")
    click.echo()
    click.echo("Note: Writing to SKEIN is optional but encouraged. Don't post just to post.")
    click.echo()
    click.echo("When done:")
    click.echo()
    click.echo(f"  skein complete")
    click.echo()


@cli.command("complete")
@click.option("--summary", help="Optional retirement summary")
@click.option("--yield-status", "yield_status", type=click.Choice(["complete", "partial", "blocked"]),
              help="Yield status for chain (auto-detected from SKEIN_CHAIN_ID)")
@click.option("--yield-outcome", "yield_outcome", help="What was accomplished (for yield)")
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

    if agent_id == "unknown":
        raise click.ClickException("Must set SKEIN_AGENT_ID or use --agent flag")

    # Check if we're in a chain (yield required)
    chain_id = os.environ.get("SKEIN_CHAIN_ID")
    task_id = os.environ.get("SKEIN_CHAIN_TASK")

    # Get roster info
    try:
        roster_data = make_request("GET", f"/roster/{agent_id}", base_url, agent_id)
        name = roster_data.get("name", agent_id)
    except:
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
            "tenders": len([f for f in agent_folios if f.get("type") == "tender"])
        }
    except:
        final_work = {}

    # If in a chain, handle yield
    yield_stored = False
    if chain_id:
        click.echo("="*60)
        click.echo("YIELD - Chain Data Package")
        click.echo("="*60)
        click.echo()
        click.echo(f"Chain: {chain_id}")
        if task_id:
            click.echo(f"Task: {task_id}")
        click.echo()

        # Show artifacts filed during session
        artifact_ids = [f.get("folio_id") for f in agent_folios if f.get("folio_id")]
        tender_ids = [f.get("folio_id") for f in agent_folios if f.get("type") == "tender"]

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
                    default="complete"
                )

        # Get outcome if not provided
        if not yield_outcome:
            yield_outcome = click.prompt(
                "Outcome (what was accomplished)",
                default=f"Completed task. Filed {len(artifact_ids)} artifact(s)."
            )

        # Build yield package
        yield_data = {
            "chain_id": chain_id,
            "task_id": task_id or "unknown",
            "yield_data": {
                "status": yield_status,
                "outcome": yield_outcome,
                "artifacts": artifact_ids,
                "notes": yield_notes
            }
        }

        # Add tender_id if we have one
        if tender_ids:
            yield_data["tender_id"] = tender_ids[0]  # Primary tender

        # Store the yield
        try:
            result = make_request("POST", "/yields", base_url, agent_id, json=yield_data)
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
            recent_sites = list(set([f.get("site_id") for f in agent_folios if f.get("site_id")]))
            if recent_sites:
                site_id = recent_sites[-1]
                summary_data = {
                    "site": site_id,
                    "content": summary,
                    "metadata": {
                        "retirement_summary": True
                    }
                }
                result = make_request("POST", "/summary", base_url, agent_id, json=summary_data)
                summary_id = result.get("folio_id")
        except:
            pass

    # Update status to retired
    try:
        update_data = {
            "status": "retired",
            "metadata": {
                "torched_at": datetime.now().isoformat(),
                "work_summary": final_work,
                "chain_id": chain_id,
                "yield_stored": yield_stored
            }
        }
        make_request("PATCH", f"/roster/{agent_id}", base_url, agent_id, json=update_data)
    except Exception as e:
        # Log but don't fail - agent can still complete even if status update fails
        click.echo(f"Warning: Could not update roster status: {e}", err=True)

    click.echo("="*60)
    click.echo("RETIRED")
    click.echo("="*60)
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
@click.option("--capabilities", multiple=True, help="Agent capabilities (can specify multiple)")
@click.option("--name", help="Human-readable name")
@click.option("--type", "agent_type", type=click.Choice(["claude-code", "patbot", "horizon", "human", "system"]), help="Agent type")
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
            "metadata": {}
        }
        if name:
            reg_data["name"] = name
        if agent_type:
            reg_data["agent_type"] = agent_type
        if description:
            reg_data["description"] = description

        try:
            reg_result = make_request("POST", "/roster/register", base_url, agent_id, json=reg_data)
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
@click.argument("target", type=click.Choice(["threads"]))
@click.option("--orphaned", is_flag=True, help="Show orphaned threads")
@click.option("--by-weaver", is_flag=True, help="Group by weaver")
@click.option("--by-type", is_flag=True, help="Group by type")
@click.option("--all", "show_all", is_flag=True, help="Show all stats")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def stats(ctx, target, orphaned, by_weaver, by_type, show_all, output_json):
    """Observability and debugging analytics.

    Examples:
        skein stats threads --orphaned
        skein stats threads --by-weaver
        skein stats threads --all
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    if target == "threads":
        analyze_threads(base_url, agent_id, orphaned, by_weaver, by_type,
                       show_all, output_json)


def analyze_threads(base_url, agent_id, orphaned, by_weaver, by_type,
                   show_all, output_json):
    """Analyze thread statistics."""
    from .analytics import (
        find_orphaned_threads,
        analyze_by_weaver as analyze_threads_by_weaver,
        analyze_by_type as analyze_threads_by_type,
        print_orphaned_threads,
        print_weaver_stats,
        print_type_distribution
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


@cli.command()
@click.pass_context
def whoami(ctx):
    """Show current agent identity."""
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    click.echo(f"Agent ID: {agent_id}")
    click.echo(f"Server: {base_url}/skein")


@cli.command()
@click.argument("topic", type=click.Choice(["quickstart", "guide", "threads", "implementation"]))
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
    import os
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
        "implementation": docs_dir / "ARCHITECTURE.md"
    }

    doc_file = doc_map.get(topic)

    if not doc_file or not doc_file.exists():
        click.echo(f"Documentation file not found: {doc_file}")
        click.echo(f"Expected location: {doc_file}")
        return

    with open(doc_file, 'r') as f:
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
@click.pass_context
def backup_create(ctx, tag):
    """Create a full backup of SKEIN data.

    Examples:
        skein backup create
        skein backup create --tag pre-migration
    """
    from .backup import get_backup_manager_for_project

    manager = get_backup_manager_for_project()
    if not manager:
        raise click.ClickException(
            "Not in a SKEIN project. Run from a directory with .skein/"
        )

    try:
        result = manager.create_full_backup(tag=tag)
        click.echo(f"Backup created: {result['backup_name']}")
        click.echo(f"  Location: {result['backup_path']}")
        click.echo(f"  Checksum: {result['checksum'][:16]}...")
        click.echo(f"  Size: {result['backup_size']:,} bytes")
        stats = result['source_stats']
        click.echo(f"  Files: {stats['total_files']}")
    except Exception as e:
        raise click.ClickException(f"Backup failed: {e}")


@backup.command("list")
@click.option("--full", "backup_type", flag_value="full", help="Show only full backups")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def backup_list(ctx, backup_type, output_json):
    """List available backups.

    Examples:
        skein backup list
        skein backup list --full
        skein backup list --json
    """
    from .backup import get_backup_manager_for_project
    import json

    manager = get_backup_manager_for_project()
    if not manager:
        raise click.ClickException(
            "Not in a SKEIN project. Run from a directory with .skein/"
        )

    backups = manager.list_backups(backup_type=backup_type or 'all')

    if output_json:
        click.echo(json.dumps(backups, indent=2, default=str))
        return

    if not backups:
        click.echo("No backups found.")
        return

    click.echo(f"Found {len(backups)} backup(s):\n")
    for backup in backups:
        name = backup.get('backup_name', 'unknown')
        timestamp = backup.get('timestamp', 'unknown')
        size = backup.get('backup_size', 0)
        tag = backup.get('tag', '')
        exists = "✓" if backup.get('exists', False) else "✗"

        click.echo(f"{exists} {name}")
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
    from .backup import get_backup_manager_for_project

    manager = get_backup_manager_for_project()
    if not manager:
        raise click.ClickException(
            "Not in a SKEIN project. Run from a directory with .skein/"
        )

    result = manager.verify_backup(backup_id)

    if result['valid']:
        click.echo(f"✓ Backup is valid")
        click.echo(f"  Checksum: {result['checksum'][:16]}...")
        click.echo(f"  Files: {result['file_count']}")
        click.echo(f"  Size: {result['backup_size']:,} bytes")
    else:
        click.echo(f"✗ Backup verification failed")
        click.echo(f"  Error: {result.get('error', 'Unknown error')}")


@backup.command("cleanup")
@click.option("--keep-last", type=int, help="Keep only the N most recent backups")
@click.option("--older-than", "older_than_days", type=int, help="Remove backups older than N days")
@click.option("--dry-run", is_flag=True, help="Show what would be removed without removing")
@click.pass_context
def backup_cleanup(ctx, keep_last, older_than_days, dry_run):
    """Remove old backups based on retention policy.

    Examples:
        skein backup cleanup --keep-last 10
        skein backup cleanup --older-than 30
        skein backup cleanup --keep-last 5 --dry-run
    """
    from .backup import get_backup_manager_for_project

    manager = get_backup_manager_for_project()
    if not manager:
        raise click.ClickException(
            "Not in a SKEIN project. Run from a directory with .skein/"
        )

    if not keep_last and not older_than_days:
        raise click.ClickException("Must specify --keep-last or --older-than")

    result = manager.cleanup_old_backups(
        keep_last=keep_last,
        older_than_days=older_than_days,
        dry_run=dry_run
    )

    if dry_run:
        removed = result.get('would_remove', [])
        if removed:
            click.echo(f"Would remove {len(removed)} backup(s):")
            for name in removed:
                click.echo(f"  - {name}")
        else:
            click.echo("No backups would be removed.")
    else:
        removed = result.get('removed', [])
        if removed:
            click.echo(f"Removed {len(removed)} backup(s):")
            for name in removed:
                click.echo(f"  - {name}")
        else:
            click.echo("No backups removed.")

    keeping = result.get('keeping', [])
    if keeping:
        click.echo(f"\nKeeping {len(keeping)} backup(s)")


@cli.command("restore")
@click.argument("backup_id")
@click.option("--dry-run", is_flag=True, help="Show what would be restored without making changes")
@click.option("--confirm", is_flag=True, help="Confirm restore (required for actual restore)")
@click.pass_context
def restore(ctx, backup_id, dry_run, confirm):
    """Restore SKEIN data from a backup.

    WARNING: This will overwrite current data. A pre-restore backup is created automatically.

    Examples:
        skein restore skein_full_2025-11-15_00-00-00 --dry-run
        skein restore skein_full_2025-11-15_00-00-00 --confirm
        skein restore latest --confirm
    """
    from .backup import get_backup_manager_for_project

    manager = get_backup_manager_for_project()
    if not manager:
        raise click.ClickException(
            "Not in a SKEIN project. Run from a directory with .skein/"
        )

    # Handle 'latest' as special case
    if backup_id == 'latest':
        backups = manager.list_backups()
        if not backups:
            raise click.ClickException("No backups found")
        backup_id = backups[0]['backup_name'].replace('.tar.gz', '')

    result = manager.restore_backup(backup_id, dry_run=dry_run, confirm=confirm)

    if dry_run:
        if result['success']:
            info = result['would_restore']
            click.echo("Would restore:")
            click.echo(f"  Files: {info['files']}")
            click.echo(f"  To: {info['to_directory']}")
            stats = info.get('source_stats', {})
            if stats:
                click.echo(f"  Original size: {stats.get('total_size', 0):,} bytes")
            click.echo("\nSample files:")
            for member in info.get('members', [])[:10]:
                click.echo(f"    {member}")
            if len(info.get('members', [])) > 10:
                click.echo(f"    ... and {info['files'] - 10} more")
        else:
            click.echo(f"Error: {result.get('error')}")
    elif result['success']:
        click.echo(f"✓ Restored from: {result['restored_from']}")
        click.echo(f"  To: {result['restored_to']}")
        click.echo(f"  Files restored: {result['files_restored']}")
        if result.get('pre_restore_backup'):
            click.echo(f"  Pre-restore backup: {result['pre_restore_backup']}")
    else:
        click.echo(f"✗ Restore failed: {result.get('error')}")
        if result.get('pre_restore_backup'):
            click.echo(f"  Pre-restore backup available: {result['pre_restore_backup']}")


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
@click.option("--project", "project_path", help="Path to project (default: current directory)")
@click.pass_context
def shard(ctx, project_path):
    """SHARD agent coordination - worktree management for parallel agent work."""
    ctx.ensure_object(dict)
    if project_path:
        # Override the project root before any shard operations
        shard_worktree = get_shard_worktree_module()
        try:
            shard_worktree.set_project_root(project_path)
        except shard_worktree.ShardError as e:
            raise click.ClickException(str(e))


@shard.command("spawn")
@click.option("--agent", "spawn_agent", required=True, help="Agent ID for this SHARD")
@click.option("--brief", help="Brief ID this SHARD relates to")
@click.option("--description", help="Work description")
@click.pass_context
def shard_spawn(ctx, spawn_agent, brief, description):
    """
    Spawn a new SHARD: create git branch + worktree for isolated agent work.

    Example:
        skein shard spawn --agent opus-security-architect --brief brief-123 --description "Bash security"
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    # Import shard_worktree from current project
    shard_worktree = get_shard_worktree_module()

    try:
        # Create worktree
        shard_info = shard_worktree.spawn_shard(
            agent_id=spawn_agent,
            brief_id=brief,
            description=description
        )

        # Create SKEIN thread to track this SHARD
        # Use "tag" type with SHARD metadata in content
        thread_content = json.dumps({
            "tag": "shard",
            "shard_id": shard_info["shard_id"],
            "worktree_name": shard_info["worktree_name"],
            "worktree_path": shard_info["worktree_path"],
            "branch_name": shard_info["branch_name"],
            "status": "spawned",
            "description": description or ""
        })

        # Thread from agent to brief (if provided) or agent to self
        thread_data = {
            "from_id": spawn_agent,
            "to_id": brief if brief else spawn_agent,
            "type": "tag",
            "content": thread_content
        }

        try:
            thread_result = make_request("POST", "/threads", base_url, agent_id, json=thread_data)
            shard_info["thread_id"] = thread_result.get("thread_id")
        except Exception as e:
            # Don't fail spawn if thread creation fails
            click.echo(f"Warning: Failed to create SKEIN thread: {e}", err=True)

        click.echo(f"✓ Spawned SHARD: {shard_info['shard_id']}")
        click.echo(f"  Agent: {shard_info['agent_id']}")
        click.echo(f"  Branch: {shard_info['branch_name']}")
        click.echo(f"  Worktree: {shard_info['worktree_path']}")
        if shard_info.get('brief_id'):
            click.echo(f"  Brief: {shard_info['brief_id']}")
        if shard_info.get('thread_id'):
            click.echo(f"  Thread: {shard_info['thread_id']}")
        click.echo(f"\nTo work in this SHARD:")
        click.echo(f"  cd {shard_info['worktree_path']}")

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to spawn SHARD: {e}")


@shard.command("list")
@click.option("--active", is_flag=True, help="Show only active SHARDs")
@click.option("--agent", "filter_agent", help="Filter by agent ID")
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
            shards = [s for s in shards if s['agent_id'] == filter_agent]

        if output_json:
            import json
            click.echo(json.dumps(shards, indent=2))
        else:
            if not shards:
                click.echo("No SHARDs found")
            else:
                for shard_item in shards:
                    click.echo(shard_item['worktree_name'])

            click.echo()
            click.echo("Tip: Use `skein shard triage` for actionable overview with status and tender info")
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
                        click.echo(f"     (in master, {commits_behind} commits behind HEAD)")
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
@click.pass_context
def shard_diff(ctx, worktree_name, show_stat):
    """
    Show diff for a SHARD (changes from master).

    Useful for reviewing changes without cd'ing to the worktree.

    Examples:
        skein shard diff my-shard-001
        skein shard diff my-shard-001 --stat
    """
    shard_worktree = get_shard_worktree_module()

    try:
        shard = shard_worktree.get_shard_status(worktree_name)

        if not shard:
            raise click.ClickException(f"SHARD not found: {worktree_name}")

        if show_stat:
            # Just show diffstat (already gathered in git_info)
            git_info = shard_worktree.get_shard_git_info(worktree_name)
            diffstat = git_info.get("diffstat", "")
            if diffstat:
                click.echo(diffstat)
            else:
                click.echo("No changes from master.")
        else:
            # Show full diff
            diff_output = shard_worktree.get_shard_diff(worktree_name)
            if diff_output:
                click.echo(diff_output)
            else:
                click.echo("No changes from master.")

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to get diff: {e}")


@shard.command("cleanup")
@click.argument("worktree_name")
@click.option("--keep-branch", is_flag=True, help="Keep git branch after removing worktree")
@click.option("--caller-cwd", "explicit_caller_cwd", default=None,
              help="Original working directory of caller (for orchestration tools)")
@click.confirmation_option(prompt="Are you sure you want to cleanup this SHARD?")
@click.pass_context
def shard_cleanup(ctx, worktree_name, keep_branch, explicit_caller_cwd):
    """
    Remove SHARD worktree and optionally delete branch.

    Example:
        skein shard cleanup opus-security-architect-20251109-001
        skein shard cleanup opus-security-architect-20251109-001 --keep-branch

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
        shard_worktree.cleanup_shard(worktree_name, keep_branch=keep_branch, caller_cwd=caller_cwd)

        click.echo(f"✓ Cleaned up SHARD: {worktree_name}")
        if not keep_branch:
            click.echo("  (Branch also deleted)")
        else:
            click.echo("  (Branch kept)")

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to cleanup SHARD: {e}")


@shard.command("merge")
@click.argument("worktree_name")
@click.option("--caller-cwd", "explicit_caller_cwd", default=None,
              help="Original working directory of caller (for orchestration tools)")
@click.pass_context
def shard_merge(ctx, worktree_name, explicit_caller_cwd):
    """
    Merge SHARD branch into master and cleanup.

    Refuses if there are uncommitted changes or conflicts.

    Example:
        skein shard merge beadle_0001-20251202-001

    For orchestration tools (e.g., Spindle), pass --caller-cwd to prevent
    agents from merging their own worktree after cd-ing elsewhere.
    """
    import os

    shard_worktree = get_shard_worktree_module()

    # Use explicit caller_cwd if provided (from orchestration tools),
    # otherwise fall back to current working directory
    caller_cwd = explicit_caller_cwd if explicit_caller_cwd else os.getcwd()

    try:
        result = shard_worktree.merge_shard(worktree_name, caller_cwd=caller_cwd)

        if result["success"]:
            click.echo(result["message"])
        else:
            click.echo(f"Error: {result['message']}")
            if result.get("uncommitted"):
                click.echo("\nUncommitted files:")
                for f in result["uncommitted"]:
                    click.echo(f"  {f}")
            if result.get("conflicts"):
                click.echo("\nFiles with conflicts:")
                for f in result["conflicts"]:
                    click.echo(f"  {f}")
            raise SystemExit(1)

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to merge SHARD: {e}")


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
        threads = make_request("GET", f"/threads?limit=100", base_url, agent_id)

        shard_thread_id = None
        for thread in threads:
            if thread.get("type") == "tag":
                try:
                    content = json.loads(thread.get("content", "{}"))
                    if content.get("tag") == "shard" and content.get("worktree_name") == worktree_name:
                        shard_thread_id = thread.get("thread_id")
                        break
                except json.JSONDecodeError:
                    continue

        if shard_thread_id:
            # Reply to thread with pause status
            reply_data = {
                "thread_id": shard_thread_id,
                "content": f"[PAUSED] {reason}"
            }
            make_request("POST", f"/threads/{shard_thread_id}/replies", base_url, agent_id, json=reply_data)

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
        threads = make_request("GET", f"/threads?limit=100", base_url, agent_id)

        shard_thread_id = None
        for thread in threads:
            if thread.get("type") == "tag":
                try:
                    content = json.loads(thread.get("content", "{}"))
                    if content.get("tag") == "shard" and content.get("worktree_name") == worktree_name:
                        shard_thread_id = thread.get("thread_id")
                        break
                except json.JSONDecodeError:
                    continue

        if shard_thread_id:
            # Reply to thread with resume status
            resume_msg = f"[RESUMED] {message}" if message else "[RESUMED]"
            reply_data = {
                "thread_id": shard_thread_id,
                "content": resume_msg
            }
            make_request("POST", f"/threads/{shard_thread_id}/replies", base_url, agent_id, json=reply_data)

        click.echo(f"▶  Resumed SHARD: {worktree_name}")
        if message:
            click.echo(f"  Message: {message}")

    except Exception as e:
        # Don't fail if thread update fails
        click.echo(f"▶  Resumed SHARD: {worktree_name}")
        if message:
            click.echo(f"  Message: {message}")
        click.echo(f"  Warning: Failed to update SKEIN thread: {e}", err=True)


@shard.command("tender")
@click.argument("worktree_name")
@click.option("--site", help="Site to post tender folio (default: derived from project)")
@click.option("--reviewer", help="Agent ID to review this SHARD (default: prime)")
@click.option("--summary", help="Brief summary of changes")
@click.option("--status", type=click.Choice(["complete", "incomplete", "abandoned"]), default="complete",
              help="Work status: complete (default), incomplete, or abandoned")
@click.option("--confidence", type=click.IntRange(1, 10),
              help="Merge confidence 1-10: 10=safe/additive/isolated (auto-merge candidate), "
                   "5=moderate risk (needs review), 1=hot mess/critical path (careful review needed)")
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

    # Derive site from project if not specified
    if not site:
        worktree_path = shard_info.get("worktree_path", "")
        if "/projects/" in worktree_path:
            parts = worktree_path.split("/projects/")[1].split("/")
            if parts:
                site = f"{parts[0]}-development"
        if not site:
            site = "shard-review"

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

    content = f"""## Tender: {worktree_name}

**Status:** {status}
**Confidence:** {confidence or 'unrated'}/10
**Reviewer:** {reviewer}

### Summary
{summary_text}

### Changes
- **Commits:** {metadata.get('commits', 0)}
- **Branch:** {metadata.get('branch_name', 'unknown')}

### Files Modified
{files_str if files_str else '  (none)'}
"""

    # Create tender folio
    folio_data = {
        "type": "tender",
        "site_id": site,
        "title": summary_text[:100] if summary_text else worktree_name,
        "content": content,
        "metadata": {
            "worktree_name": worktree_name,
            "branch_name": metadata.get("branch_name"),
            "commits": metadata.get("commits", 0),
            "files_modified": files_list,
            "status": status,
            "confidence": confidence,
            "reviewer": reviewer,
            "agent_id": metadata.get("agent_id"),
        }
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
    Triage all SHARDs - actionable overview with status, conflicts, and tender info.

    Shows all shards with:
    - Commit count and diffstat (+/-)
    - Merge status (clean/CONFLICT/uncommitted)
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
            all_folios = make_request("GET", "/folios", base_url, agent_id, params={"type": "tender"})
            for folio in all_folios:
                metadata = folio.get("metadata", {})
                if isinstance(metadata, str):
                    try:
                        import json as json_module
                        metadata = json_module.loads(metadata)
                    except:
                        continue
                wt_name = metadata.get("worktree_name")
                if wt_name:
                    tender_map[wt_name] = {
                        "folio_id": folio.get("folio_id"),
                        "confidence": metadata.get("confidence"),
                        "status": metadata.get("status"),
                        "summary": folio.get("title", "")[:50]
                    }
        except:
            pass  # Tender lookup is optional

        # Build triage data
        triage_data = []
        for shard_item in shards:
            wt_name = shard_item["worktree_name"]
            git_info = shard_worktree.get_shard_git_info(wt_name)

            commits = git_info.get("commits_ahead", 0)
            merge = git_info.get("merge_status", "unknown")
            uncommitted = git_info.get("uncommitted", [])

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

            # Determine status icon
            if uncommitted:
                status_icon = "○"  # uncommitted
                status_text = "uncommitted"
            elif merge == "conflict":
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

                diffstat_str = f"+{ins}/-{dels}" if (ins or dels) else "---"
                parts = [
                    f"  {icon}",
                    f"{name:<35}",
                    f"{commits:>2} commits",
                    f"{diffstat_str:>12}",
                    f"{status:<12}",
                ]
                if conf is not None:
                    parts.append(f"confidence:{conf}")
                elif entry["tender_id"]:
                    parts.append("(tendered)")
                else:
                    parts.append("(no tender)")

                click.echo("  ".join(parts))

            click.echo("\nUse `skein shard review <name>` for details on a specific shard")

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to triage SHARDs: {e}")


@shard.command("review")
@click.argument("worktree_name")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def shard_review(ctx, worktree_name, output_json):
    """
    Deep review of a single SHARD for merge decision.

    Shows:
    - Branch and tender info
    - Specific conflict files (if any)
    - Full change list with diffstat
    - Tender summary and confidence

    Example:
        skein shard review my-shard-001
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    shard_worktree = get_shard_worktree_module()

    try:
        shard_info = shard_worktree.get_shard_status(worktree_name)
        if not shard_info:
            raise click.ClickException(f"SHARD not found: {worktree_name}")

        git_info = shard_worktree.get_shard_git_info(worktree_name)

        # Look up tender folio
        tender_info = None
        try:
            all_folios = make_request("GET", "/folios", base_url, agent_id, params={"type": "tender"})
            for folio in all_folios:
                metadata = folio.get("metadata", {})
                if isinstance(metadata, str):
                    try:
                        import json as json_module
                        metadata = json_module.loads(metadata)
                    except:
                        continue
                if metadata.get("worktree_name") == worktree_name:
                    tender_info = {
                        "folio_id": folio.get("folio_id"),
                        "confidence": metadata.get("confidence"),
                        "status": metadata.get("status"),
                        "summary": folio.get("title", ""),
                    }
                    break
        except:
            pass

        # Get conflict file list if there are conflicts
        conflict_files = []
        merge_status = git_info.get("merge_status", "unknown")
        if merge_status == "conflict":
            try:
                from skein import shard as shard_module
                repo = shard_module._get_repo()
                branch = shard_info["branch_name"]
                merge_base = repo.git.merge_base("master", branch)
                master_files = set(repo.git.diff("--name-only", merge_base, "master").strip().split("\n"))
                branch_files = set(repo.git.diff("--name-only", merge_base, branch).strip().split("\n"))
                conflict_files = sorted(list(master_files & branch_files - {''}))
            except:
                pass

        # Build review data
        review_data = {
            "worktree_name": worktree_name,
            "branch_name": shard_info["branch_name"],
            "worktree_path": shard_info["worktree_path"],
            "commits_ahead": git_info.get("commits_ahead", 0),
            "merge_status": merge_status,
            "uncommitted": git_info.get("uncommitted", []),
            "commit_log": git_info.get("commit_log", []),
            "diffstat": git_info.get("diffstat", ""),
            "conflict_files": conflict_files,
            "tender": tender_info,
        }

        if output_json:
            import json as json_module
            click.echo(json_module.dumps(review_data, indent=2))
        else:
            click.echo(f"SHARD: {worktree_name}")
            click.echo(f"Branch: {shard_info['branch_name']}")

            if tender_info:
                conf_str = f"{tender_info['confidence']}/10" if tender_info.get('confidence') else "unrated"
                click.echo(f"Tender: {tender_info['folio_id']} (confidence: {conf_str})")
            else:
                click.echo("Tender: (none)")
            click.echo()

            uncommitted = git_info.get("uncommitted", [])
            if uncommitted:
                click.echo("UNCOMMITTED CHANGES:")
                for line in uncommitted[:10]:
                    click.echo(f"  {line}")
                if len(uncommitted) > 10:
                    click.echo(f"  ... and {len(uncommitted) - 10} more")
                click.echo()

            if conflict_files:
                click.echo("CONFLICTS WITH MASTER:")
                for f in conflict_files[:15]:
                    click.echo(f"  {f}")
                if len(conflict_files) > 15:
                    click.echo(f"  ... and {len(conflict_files) - 15} more")
                click.echo()

            commit_log = git_info.get("commit_log", [])
            if commit_log:
                click.echo(f"CHANGES ({len(commit_log)} commits):")
                for sha, msg in commit_log[:10]:
                    click.echo(f"  {sha} {msg}")
                click.echo()

                diffstat = git_info.get("diffstat", "")
                if diffstat:
                    click.echo("FILES:")
                    for line in diffstat.split("\n")[:15]:
                        click.echo(f"  {line}")
                    click.echo()
            else:
                click.echo("No unique commits")
                click.echo()

            if tender_info and tender_info.get("summary"):
                click.echo("TENDER SUMMARY:")
                click.echo(f"  {tender_info['summary']}")
                click.echo()

            # Actions hint
            if uncommitted:
                click.echo("Actions: Commit changes first, then merge or tender")
            elif merge_status == "conflict":
                click.echo("Actions: Use `skein shard apply` to work on it locally")
            elif commit_log:
                click.echo(f"Actions: `skein shard merge {worktree_name}` or `skein shard cleanup {worktree_name}`")

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to review SHARD: {e}")


@shard.command("stash")
@click.argument("description")
@click.option("--agent", "stash_agent", help="Agent ID for the new SHARD")
@click.pass_context
def shard_stash(ctx, description, stash_agent):
    """
    Stash uncommitted changes into a new SHARD.

    Creates a new shard worktree, moves your uncommitted changes there,
    and leaves your current branch clean.

    Example:
        skein shard stash "WIP: auth refactor"
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

        # Create the shard
        new_shard = shard_worktree.spawn_shard(stash_agent, description=description)
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
                text=True
            )
            if result.returncode != 0:
                raise Exception(result.stderr)

            # Drop the stash since we applied it
            repo.git.stash("drop")

            click.echo(f"✓ Stashed changes to SHARD: {worktree_name}")
            click.echo(f"  Path: {worktree_path}")
            click.echo(f"  Description: {description}")
            click.echo()
            click.echo(f"Your current branch is now clean.")
            click.echo(f"To continue work: cd {worktree_path}")

        except Exception as e:
            # If apply fails, restore the stash
            try:
                repo.git.stash("pop")
            except:
                pass
            try:
                shard_worktree.cleanup_shard(worktree_name, keep_branch=False)
            except:
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

    Takes the diff between master and the shard branch and applies it
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

        # Get the diff (master..branch)
        diff = repo.git.diff("master", branch)
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
            if not diff.endswith('\n'):
                diff += '\n'
            result = subprocess.run(
                ["git", "apply"],
                input=diff,
                text=True,
                capture_output=True,
                cwd=shard_module.PROJECT_ROOT
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
            raise click.ClickException(f"Unknown rite: {rite_name}\nAvailable: {available}")

        rite_config = rites_dict[rite_name]

        click.echo(f"▶ Running rite '{rite_name}' in shard: {worktree_name}")
        click.echo(f"  Worktree: {worktree_path}")

        success = run_rite_commands(rite_name, rite_config, worktree_path, verbose)

        if success:
            click.echo(f"✓ Rite '{rite_name}' completed in shard {worktree_name}")
        else:
            raise click.ClickException(f"Rite '{rite_name}' failed in shard {worktree_name}")

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        if "ClickException" in str(type(e).__name__):
            raise
        raise click.ClickException(f"Failed to run rite in shard: {e}")


# Web UI Command
@cli.command()
@click.option("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
@click.option("--port", "-p", default=8003, type=int, help="Port to listen on (default: 8003)")
@click.option("--open", "open_browser", is_flag=True, help="Open browser after starting")
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
        raise click.ClickException(f"Port {port} is already in use. Try a different port with --port")

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
    ctx.invoke(shard_list, active=active, filter_agent=filter_agent, output_json=output_json)


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
    verbose: bool = False
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
                cmd,
                shell=True,
                cwd=cwd,
                capture_output=not verbose,
                text=True
            )

            if result.returncode != 0:
                if not verbose and result.stderr:
                    click.echo(result.stderr, err=True)
                if not verbose and result.stdout:
                    click.echo(result.stdout)
                click.echo(f"✗ Command failed (exit {result.returncode}): {cmd}", err=True)
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
        raise click.ClickException("Not in a SKEIN project (no .skein/ directory found)")

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
        raise click.ClickException("Not in a SKEIN project (no .skein/ directory found)")

    config = load_rites_config(project_root)
    rites_dict = config.get("rites", {})

    if not rites_dict:
        click.echo("No rites defined.")
        click.echo(f"\nCreate {project_root / '.skein' / 'rites.yaml'} with:")
        click.echo("""
rites:
  test:
    description: "Run test suite"
    commands:
      - pytest
  lint:
    description: "Check code style"
    commands:
      - ruff check .
""")
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
