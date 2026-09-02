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
import stat
import multiprocessing
import signal as signal_module
import shutil
import click
import requests
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any, NamedTuple, Set, List

# Import name generator from skein package
try:
    from skein.utils import generate_agent_name
except ImportError:
    # Fallback if skein package not installed
    generate_agent_name = None

from skein.address_legacy import parse as parse_address
from skein.service_address import local_service_url
from skein.storage import (
    PROJECT_DATA_DIR_HEADER,
    ProjectAlreadyRegistered,
    ProjectRegistryError,
    encode_project_data_dir_claim,
    load_project_registry_document,
    project_data_dirs_match,
    project_data_dir_from_registry_entry,
    register_project,
    skein_home,
)
from skein.version import package_version as _package_version


def _path_exists_strict(path: Path) -> bool:
    """Test existence without Python 3.14's OSError-suppressing Path.exists().

    Only a missing leaf is absent. Broken traversal such as a non-directory
    parent is a repository/configuration fault that callers must surface.
    """
    try:
        path.stat()
    except FileNotFoundError:
        return False
    return True


def _path_is_dir_strict(path: Path) -> bool:
    """Test directory type while preserving permission and traversal errors."""
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(mode)


def parse_post_site_id(site_id_arg: str) -> tuple:
    """Parse a post site_id arg, supporting 'project:site' colon syntax.

    Returns (site_id, project_override). Bare ids return (id, None).
    """
    parsed = parse_address(site_id_arg)
    if parsed.is_qualified:
        return parsed.folio_id, parsed.project
    return parsed.folio_id, None


def parse_folio_project_override(folio_address: str) -> Optional[str]:
    """Return the explicit project in a legacy ``project:folio`` read address.

    Rev-3 ``::`` addresses are content addresses, not project qualifiers.  A
    verifier fragment also does not participate in project selection.
    """
    body = folio_address.partition("#")[0]
    if "::" in body:
        return None
    parsed = parse_address(body)
    return parsed.project if parsed.is_qualified else None


def find_project_root() -> Optional[Path]:
    """
    Walk up directory tree to find .skein/ directory (like git).
    Returns project root path or None if not found.
    """
    current = Path.cwd()
    while current != current.parent:
        skein_dir = current / ".skein"
        if _path_is_dir_strict(skein_dir):
            return current
        current = current.parent
    return None


def get_project_config() -> Optional[Dict[str, Any]]:
    """Get project config from .skein/config.json if in a project.

    Raises OSError when the directory walk or the config probe cannot run at
    all (an unsearchable ancestor or .skein/): a command must fail loudly on a
    config it cannot evaluate, not silently fall through to the global URL.
    `skein doctor` guards its own calls and reports that state as a check.
    """
    project_root = find_project_root()
    if not project_root:
        return None

    config_file = project_root / ".skein" / "config.json"
    if not _path_exists_strict(config_file):
        return None

    try:
        with open(config_file) as f:
            data = json.load(f)
        # A config file that parses but is not an object (a JSON array, a bare
        # string) is unusable; callers do config.get(...), which would raise.
        return data if isinstance(data, dict) else None
    except (ValueError, RecursionError):
        return None


def get_global_config() -> Dict[str, Any]:
    """Get global SKEIN config from ~/.skein/config.json.

    An absent or unusable file reads as empty — never as a fabricated
    default. Fabricating {"server_url": ...} here once made every rung below
    the global config dead code (notion-20260722-95kl); resolution falls
    through to the machine's service address instead.

    Raises OSError when ~/.skein cannot even be probed (root-owned after a
    stray sudo): answering with nothing would silently reroute every
    command to the local default. `skein doctor` guards its own call and
    reports that state as a failing check.
    """
    config_file = Path.home() / ".skein" / "config.json"
    if not _path_exists_strict(config_file):
        return {}

    try:
        with open(config_file) as f:
            data = json.load(f)
        # Only an object is usable — callers do config.get(...). A JSON array or
        # other shape reads as empty rather than crashing every command
        # (get_base_url reads this).
        return data if isinstance(data, dict) else {}
    except (ValueError, RecursionError):
        return {}


def get_agent_id(ctx_agent: Optional[str] = None, base_url: Optional[str] = None) -> Optional[str]:
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


# The exact literals `skein init` used to write into every project's
# .skein/config.json (and only ever these — it took no other value). A config
# holding one is pinning the machine default, not expressing a choice, so
# resolution reads it as absent; otherwise every pre-existing project would
# shadow the machine's service address forever. A deliberately different
# server_url is honored. Configs are never rewritten — ignoring at read is
# reversible, a migration over ~55 registries is not.
_LEGACY_DEFAULT_URLS = {"http://localhost:8001", "http://127.0.0.1:8001"}


class BaseURLResolution(NamedTuple):
    """A resolved base URL, the rung that answered, and what was passed over.

    ``ignored`` records benign skips (a legacy server_url literal) and
    ``problems`` records invalid values (an unparseable SKEIN_PORT) — `skein
    doctor` reports both; nothing else acts on them.
    """

    url: str
    source: str
    ignored: List[str]
    problems: List[str]


def resolve_base_url(ctx_url: Optional[str] = None, tolerant: bool = False) -> BaseURLResolution:
    """Resolve the URL the CLI talks to, and say which rung answered.

    Priority order:
    1. --url flag
    2. SKEIN_URL env var
    3. Project config (.skein/config.json) server_url — a legacy default
       literal reads as absent (see _LEGACY_DEFAULT_URLS)
    4. Global config (~/.skein/config.json) server_url — same exclusion
    5. The machine's local service address (skein.service_address) — the same
       ladder skein-server binds, so SKEIN_PORT or server.json moves both
       ends together

    With ``tolerant`` false (every normal command) a project or global config
    that cannot even be probed raises OSError: failing loudly beats silently
    rerouting to localhost. ``skein doctor`` passes true — it must reach its
    checks on exactly that broken install, and reports the unprobeable config
    as a failing check itself. Nothing else raises: rung 5 never does.
    """
    ignored: List[str] = []
    problems: List[str] = []

    if ctx_url:
        return BaseURLResolution(ctx_url.rstrip("/"), "--url flag", ignored, problems)

    env_url = os.getenv("SKEIN_URL")
    if env_url:
        return BaseURLResolution(env_url.rstrip("/"), "SKEIN_URL", ignored, problems)

    try:
        project_config = get_project_config()
    except OSError:
        if not tolerant:
            raise
        project_config = None
    url = (project_config or {}).get("server_url")
    if isinstance(url, str) and url.strip():
        url = url.rstrip("/")
        if url in _LEGACY_DEFAULT_URLS:
            ignored.append(
                "server_url in the project .skein/config.json is the retired "
                "default literal, read as absent"
            )
        else:
            return BaseURLResolution(
                url, "server_url in the project .skein/config.json", ignored, problems
            )
    elif url is not None and not isinstance(url, str):
        problems.append(
            "server_url in the project .skein/config.json is not a string, read as absent"
        )

    try:
        global_config = get_global_config()
    except OSError:
        if not tolerant:
            raise
        global_config = {}
    url = global_config.get("server_url")
    if isinstance(url, str) and url.strip():
        url = url.rstrip("/")
        if url in _LEGACY_DEFAULT_URLS:
            ignored.append(
                "server_url in ~/.skein/config.json is the retired default "
                "literal, read as absent"
            )
        else:
            return BaseURLResolution(
                url, "server_url in ~/.skein/config.json", ignored, problems
            )
    elif url is not None and not isinstance(url, str):
        problems.append("server_url in ~/.skein/config.json is not a string, read as absent")

    service_url, resolved = local_service_url()
    moved = sorted(
        {src for key, src in resolved.sources.items() if key != "log_level" and src != "default"}
    )
    source = f"local service address ({', '.join(moved) if moved else 'built-in default'})"
    return BaseURLResolution(service_url, source, ignored, problems + list(resolved.problems))


def get_base_url(ctx_url: Optional[str] = None) -> str:
    """The URL every command sends requests to; see resolve_base_url for the
    ladder. Raises OSError only for a project/global config that cannot be
    probed — never for a bad value, which is ignored and left to doctor."""
    return resolve_base_url(ctx_url).url


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
    fallback_project_id = kwargs.pop("fallback_project_id", None)
    qualified_address = kwargs.pop("qualified_address", False)
    folio_address_request = kwargs.pop("_folio_address_request", False)

    if endpoint.startswith("/folios/") and not folio_address_request:
        raise ValueError(
            "Addressed folio requests must use make_folio_request so qualified "
            "project selection cannot retain an implicit cwd claim"
        )

    if agent_id is not None:
        headers["X-Agent-Id"] = agent_id

    # Resolve project: explicit kwarg > SKEIN_PROJECT env > cwd .skein/ config.
    # The top-level --project flag is pushed into SKEIN_PROJECT by the cli group.
    implicit_project_root: Optional[Path] = None
    if not project_id:
        project_id = os.environ.get("SKEIN_PROJECT")
    if not project_id:
        project_config = get_project_config()
        if project_config:
            project_id = project_config.get("project_id")
            if project_id:
                implicit_project_root = find_project_root()
    if not project_id:
        project_id = fallback_project_id
    if project_id:
        headers["X-Project-Id"] = project_id
        if implicit_project_root is not None and not qualified_address:
            # A project id alone cannot distinguish a copied .skein/config.json
            # from its registered owner.  Send the cwd-discovered data directory
            # so the service can compare it with its own registry before opening
            # a store.  Explicit cross-project selections intentionally omit it.
            headers[PROJECT_DATA_DIR_HEADER] = encode_project_data_dir_claim(
                (implicit_project_root / ".skein" / "data").resolve()
            )

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


def make_folio_request(
    method: str,
    folio_address: str,
    base_url: str,
    agent_id: str,
    *,
    suffix: str = "",
    **kwargs,
):
    """Request a folio address without treating a qualified address as cwd-based."""
    qualified_project = parse_folio_project_override(folio_address)
    return make_request(
        method,
        f"/folios/{folio_address}{suffix}",
        base_url,
        agent_id,
        fallback_project_id=qualified_project,
        qualified_address=qualified_project is not None,
        _folio_address_request=True,
        **kwargs,
    )


# Breadcrumb hints — one-line footers pointing at cross-project layer.
FIND_BREADCRUMB = (
    "(searched current project only — `skein find PATTERN --all` to search all projects)"
)
FOLIO_NOT_FOUND_BREADCRUMB = (
    "(not found in current project — try `skein folio --all ID` or `skein folio PROJECT:ID`)"
)
ACTIVITY_BREADCRUMB = "(current project only — `skein activity --all` to include all projects)"


def _load_projects_registry() -> Dict[str, Any]:
    """Load registered projects from <SKEIN_HOME>/projects.json. Returns {} if missing."""
    projects_file = skein_home() / "projects.json"
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
@click.option("--agent", envvar="SKEIN_AGENT_ID", help="Agent ID (or set SKEIN_AGENT_ID)")
@click.option(
    "--url",
    envvar="SKEIN_URL",
    help="SKEIN server URL (default: the machine's local service address)",
)
@click.option(
    "--project",
    envvar="SKEIN_PROJECT",
    help="Project to operate on (overrides cwd .skein/ discovery; or set SKEIN_PROJECT)",
)
@click.version_option(
    version=_package_version(),
    package_name="interskein",
    message="%(prog)s (interskein) %(version)s",
)
@click.pass_context
def cli(ctx, agent, url, project):
    """SKEIN CLI - Agent collaboration system.

    Getting started: skein info quickstart
    Full guide: skein info guide
    Check the install: skein doctor
    """
    # click fills --url from SKEIN_URL (envvar=), which would make resolution
    # label an environment-supplied URL "--url flag". When the value came from
    # the environment, leave it unset here so resolve_base_url's own SKEIN_URL
    # rung answers — the same URL, with honest provenance in doctor.
    if url is not None:
        try:
            from click.core import ParameterSource

            if ctx.get_parameter_source("url") == ParameterSource.ENVIRONMENT:
                url = None
        except ImportError:
            pass

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

    # Read and validate the registry before creating any local state.  A damaged
    # registry is not an empty registry, and an existing project id has one
    # owner: neither condition may be overwritten by `skein init`.
    try:
        projects_data = load_project_registry_document()
    except ProjectRegistryError as e:
        raise click.ClickException(
            f"Cannot initialize while the project registry is unsafe: {e}. "
            "No project files were created."
        ) from e

    data_dir = skein_dir / "data"
    existing = projects_data["projects"].get(project)
    if existing is not None:
        try:
            existing_data_dir = project_data_dir_from_registry_entry(
                project, existing
            )
        except ProjectRegistryError as e:
            raise click.ClickException(
                f"Cannot initialize while the project registry is unsafe: {e}. "
                "No project files were created."
            ) from e
        same_owner = project_data_dirs_match(existing_data_dir, data_dir)
        if not same_owner:
            if isinstance(existing, dict):
                owner = (
                    existing.get("path")
                    or existing.get("data_dir")
                    or "(unknown location)"
                )
            else:
                owner = "(malformed registry entry)"
            raise click.ClickException(
                f"Project ID '{project}' is already registered at {owner}. "
                "Choose a different project ID; the existing owner was not changed."
            )

    project_info = {
        "path": str(project_root),
        "data_dir": str(data_dir),
        "name": name or project,
        "registered_at": datetime.now().isoformat(),
    }

    created_skein = False
    try:
        # Create local state first, then atomically claim the global ID.  If
        # another initializer wins after our preflight, register_project refuses
        # its different owner and this invocation removes only the tree it made.
        skein_dir.mkdir()
        created_skein = True
        data_dir.mkdir()
        (data_dir / "sites").mkdir()
        (data_dir / "roster").mkdir()
        (data_dir / "threads").mkdir()
        (data_dir / "screenshots").mkdir()

        # No server_url: a project config is shared and checked out across
        # machines.  URL resolution remains per-machine.
        project_config = {
            "project_id": project,
            "name": name or project,
            "created_at": datetime.now().isoformat(),
        }
        with open(skein_dir / "config.json", "w") as f:
            json.dump(project_config, f, indent=2)

        register_project(project, project_info, allow_same_data_dir=True)
    except (ProjectAlreadyRegistered, ProjectRegistryError, OSError) as e:
        if created_skein and skein_dir.exists():
            shutil.rmtree(skein_dir)
        if isinstance(e, ProjectAlreadyRegistered):
            raise click.ClickException(str(e)) from e
        raise click.ClickException(
            f"Project registration failed: {e}. New project files were rolled back."
        ) from e

    click.echo(f"✓ Initialized SKEIN project '{project}' in {project_root}")
    click.echo("✓ Created .skein/ directory")
    click.echo("✓ Registered in ~/.skein/projects.json")
    click.echo(f"\nProject data: {data_dir}")
    click.echo(f"Server URL: {get_base_url(None)} (resolved per machine, not stored)")


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
    projects_file = skein_home() / "projects.json"

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
        result = subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True, timeout=5)
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
# Doctor - Installation Diagnosis
# ============================================================================


def packaged_docs_dir() -> Path:
    """Directory holding the docs `skein info` serves, inside the package."""
    import skein

    return Path(skein.__file__).parent / "docs"


# The topics `skein info` serves, mapped to their packaged filenames.
INFO_TOPICS = {
    "quickstart": "SKEIN_QUICK_START.md",
    "guide": "SKEIN_AGENT_GUIDE.md",
    "implementation": "ARCHITECTURE.md",
}

# A document that survived a build without symlink support is a one-line
# relative path, not a document. Length is the cheapest way to tell them apart.
MIN_DOC_BYTES = 500


def resolve_doc(topic: str) -> Optional[Path]:
    """Locate a `skein info` document: packaged copy first, checkout second."""
    filename = INFO_TOPICS.get(topic)
    if not filename:
        return None

    packaged = packaged_docs_dir() / filename
    if packaged.exists():
        return packaged

    # Source checkout that was never installed: fall back to the repo layout.
    repo_root = Path(__file__).resolve().parent.parent
    for candidate in (repo_root / "docs" / filename, repo_root / filename):
        if candidate.exists():
            return candidate
    return None


def _same_dir(a: str, b) -> Optional[bool]:
    """Whether two paths name the same directory, or None if it can't be told.

    Resolves both and compares. Either side can be untrusted (a service-supplied
    skein_home from /health), and a path that contains a symlink loop makes
    ``Path.resolve()`` raise, so a failure returns None ("can't compare") rather
    than crashing this diagnostic.
    """
    def _resolve(value) -> Optional[Path]:
        path = Path(value)
        try:
            return path.resolve(strict=True)
        except (FileNotFoundError, NotADirectoryError):
            # A service can report a legitimate but currently absent home or
            # a not-yet-materializable child path. It is still comparable
            # as a normalized path and should mismatch a different live home
            # rather than becoming "unknown".
            try:
                return path.resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                return None
        except (OSError, RuntimeError, ValueError):
            # ELOOP (and pathlib's older RuntimeError form) is not a path that
            # can be compared safely.
            return None

    resolved_a = _resolve(a)
    resolved_b = _resolve(b)
    if resolved_a is None or resolved_b is None:
        return None
    return resolved_a == resolved_b


def _check(name, ok, detail, level="error", hint=None):
    return {"name": name, "ok": bool(ok), "detail": detail, "level": level, "hint": hint}


def start_service_hint() -> str:
    """How to get the API service running. There is no `skein` command that
    supervises processes — that is the platform's job (systemd, launchd), and
    `skein-server` is the entrypoint either drives."""
    return "Run the service: skein-server  (or install it under systemd; see the README)"


def doctor_checks(
    base_url: str, url_resolution: Optional[BaseURLResolution] = None
) -> List[Dict[str, Any]]:
    """Diagnose this SKEIN install. Returns one mapping per check.

    A check with ``ok`` false and ``level`` "error" means SKEIN will not work as
    installed; "warn" means it works but something is off; "info" never fails.

    ``url_resolution`` is the provenance of ``base_url`` (which rung of
    resolve_base_url answered, what was passed over); without it the url
    resolution checks are simply omitted.
    """
    import platform
    import shutil

    import requests

    from skein.storage import skein_home
    from skein.version import distribution_location, package_version, source_version

    checks: List[Dict[str, Any]] = []
    cli_version = package_version()

    install_detail = (
        f"interskein {cli_version} · python {platform.python_version()} · "
        f"{distribution_location() or 'unknown location'}"
    )
    # Distribution metadata can describe a different install than the code that
    # actually got imported — a source checkout on the path with an older wheel
    # installed beside it. Report both so the disagreement is visible rather
    # than silently making every version comparison agree on the wrong number.
    if source_version() != cli_version:
        checks.append(
            _check(
                "install",
                False,
                f"{install_detail} · imported source says {source_version()}",
                level="warn",
                hint="Two installs are in play. Reinstall, or drop the source "
                "checkout off PYTHONPATH.",
            )
        )
    else:
        checks.append(_check("install", True, install_detail, level="info"))

    # SKEIN home must exist and be writable — the project registry lives there.
    home = skein_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        writable = os.access(home, os.W_OK)
        home_detail = str(home) if writable else f"{home} is not writable"
    except OSError as e:
        writable = False
        home_detail = f"{home}: {e}"
    checks.append(
        _check(
            "skein home",
            writable,
            home_detail,
            hint="Set SKEIN_HOME to a writable directory.",
        )
    )

    # The project registry: the map from project id to data directory. Losing it
    # strands every project while their data sits intact on disk, so an absent
    # registry is only benign when there is no evidence one ever existed.
    registry_file = home / "projects.json"
    registry: Dict[str, Any] = {}
    registry_readable = True
    # exists() raises rather than returning False when the home denies search
    # (a root-owned ~/.skein after a stray sudo) — the exact broken install
    # doctor is run to diagnose, so the probe itself must become a check result.
    try:
        registry_present = _path_exists_strict(registry_file)
    except OSError as e:
        registry_present = False
        registry_readable = False
        checks.append(
            _check(
                "project registry",
                False,
                f"cannot look for {registry_file}: {e}",
                hint="The SKEIN home is not searchable. Fix its ownership and mode "
                "(chown/chmod), or set SKEIN_HOME to a readable directory.",
            )
        )
    if registry_present:
        try:
            with open(registry_file) as f:
                loaded = json.load(f).get("projects", {})
            if not isinstance(loaded, dict):
                raise ValueError(f"'projects' is a {type(loaded).__name__}, expected an object")
            registry = loaded
        except Exception as e:
            registry_readable = False
            checks.append(_check("project registry", False, f"{registry_file} is unreadable: {e}"))

    # The cwd walk stats entries under every ancestor: a deleted cwd or an
    # ancestor with search revoked raises mid-walk. That is not "not in a
    # project" — doctor cannot tell — so the failure is carried to the current
    # project check rather than passed off as a clean answer.
    project_root: Optional[Path] = None
    project_root_error: Optional[OSError] = None
    try:
        project_root = find_project_root()
    except OSError as e:
        project_root_error = e

    # get_project_config re-walks the cwd, so probe it only when the walk
    # succeeded; it raises its own OSError on a .skein/ that denies search.
    project_config: Dict[str, Any] = {}
    project_config_error: Optional[OSError] = None
    if project_root_error is None:
        try:
            project_config = get_project_config() or {}
        except OSError as e:
            project_config_error = e
    project_id = project_config.get("project_id")

    if registry_readable and not registry_present:
        # Rotating backups exist only if the registry was written at least once;
        # a fresh install has none. So backups present with the live file gone is
        # the fingerprint of a deleted registry, wherever doctor happens to run.
        # listdir, not glob: glob swallows PermissionError by design and returns
        # [] for a home that denies listing (mode 0333) — backups it cannot see.
        # Evidence doctor could not collect makes the failed probe the verdict;
        # "no projects registered yet" would claim it collected some.
        try:
            backups = sorted(n for n in os.listdir(home) if n.startswith("projects.json.bak-"))
        except OSError as e:
            checks.append(
                _check(
                    "project registry",
                    False,
                    f"no registry at {registry_file}, and cannot enumerate backups: {e}",
                    hint="The SKEIN home denies listing, so a deleted registry and a "
                    "fresh install look alike. Fix the home's mode (chmod u+r), or "
                    "set SKEIN_HOME to a readable directory.",
                )
            )
        else:
            if project_id or backups:
                evidence = []
                if project_id:
                    evidence.append(f"{project_root} is an initialized project ('{project_id}')")
                if backups:
                    evidence.append(f"{len(backups)} registry backup(s) present in {home}")
                checks.append(
                    _check(
                        "project registry",
                        False,
                        f"no registry at {registry_file}, but " + ", and ".join(evidence),
                        hint="The registry is missing or was deleted. Restore it from a "
                        f"{registry_file.name}.bak-* backup in {home}.",
                    )
                )
            else:
                checks.append(
                    _check(
                        "project registry",
                        True,
                        "no projects registered yet",
                        level="info",
                        hint="Run `skein init --project NAME` in a project directory.",
                    )
                )
    elif registry_readable:
        # A registered entry is usable only if it points at a data directory that
        # exists. An absent or empty data_dir is not "present as the cwd" — Path("")
        # tests as the current directory, which would pass a broken entry. And a
        # data_dir behind a directory that denies search makes exists() raise;
        # unreachable is as unusable as missing.
        def _data_dir_exists(info: Any) -> bool:
            if not (
                isinstance(info, dict)
                and isinstance(info.get("data_dir"), str)
                and info["data_dir"]
            ):
                return False
            try:
                return Path(info["data_dir"]).exists()
            except OSError:
                return False

        missing = [name for name, info in registry.items() if not _data_dir_exists(info)]
        if missing:
            checks.append(
                _check(
                    "project registry",
                    False,
                    f"{len(registry)} registered, data directory missing for: "
                    + ", ".join(sorted(missing)),
                    level="warn",
                    hint="Re-run `skein init` in those projects, or remove the stale entries.",
                )
            )
        else:
            checks.append(
                _check(
                    "project registry",
                    True,
                    f"{len(registry)} project(s) registered",
                    level="info",
                )
            )

    # The global config routes every command that runs without SKEIN_URL. The
    # shared helper keeps master's loud failure for those commands — a ~/.skein
    # that denies search must not silently reroute them to the default URL — so
    # doctor probes it here itself, and the raise becomes a failing check.
    try:
        get_global_config()
    except OSError as e:
        checks.append(
            _check(
                "global config",
                False,
                f"cannot probe {Path.home() / '.skein' / 'config.json'}: {e}",
                hint="Fix the ownership/mode of ~/.skein (left behind by a stray "
                "sudo?) so the CLI can resolve its server URL.",
            )
        )

    # Which rung of the URL ladder produced base_url — visible here so a moved
    # port is a doctor line, not a mystery. A value resolution had to ignore
    # (an unparseable SKEIN_PORT, an unusable config file) is a warning:
    # commands keep working, but not where the operator pointed them.
    if url_resolution is not None:
        detail = f"{base_url} · {url_resolution.source}"
        if url_resolution.ignored:
            detail += " · " + "; ".join(url_resolution.ignored)
        checks.append(_check("url resolution", True, detail, level="info"))
        if url_resolution.problems:
            checks.append(
                _check(
                    "url resolution",
                    False,
                    "; ".join(url_resolution.problems),
                    level="warn",
                    hint="Resolution ignored these values. Fix or unset them so "
                    "the CLI and the service land where you intended.",
                )
            )

    # The service the CLI actually talks to. A 200 alone is not enough — anything
    # can be listening on a fixed localhost port — so it must identify as a healthy
    # SKEIN service, and it must serve the SAME home the CLI reads, or `skein`
    # commands would hit a service that cannot see this CLI's projects.
    health: Optional[Dict[str, Any]] = None
    try:
        response = requests.get(base_url.rstrip("/") + "/health", timeout=3)
        if response.status_code == 200:
            body = response.json()
            if isinstance(body, dict):
                health = body
    except Exception:
        health = None

    if health is None:
        checks.append(
            _check(
                "api service",
                False,
                f"nothing responding at {base_url}",
                hint=start_service_hint(),
            )
        )
    elif health.get("status") != "healthy" or health.get("distribution") not in (
        None,
        "interskein",
    ):
        # Something answered, but it is not a healthy SKEIN service (an impostor on
        # the port, or a wedged service). A missing distribution is allowed — that
        # is an older SKEIN, which the version check reports as a warning below.
        health = None
        checks.append(
            _check(
                "api service",
                False,
                f"something is answering at {base_url}, but it is not a healthy SKEIN service",
                hint="Another process may be holding that port. Point the CLI elsewhere "
                "with SKEIN_URL, or free the port and start skein-server.",
            )
        )
    else:
        # A real interskein service reports skein_home as a string. Only compare
        # when it is one; a missing or malformed value is left uncompared (like an
        # older service) rather than crashing this diagnostic on it.
        service_home = health.get("skein_home")
        # Flag a mismatch only when both homes resolve and differ. A malformed or
        # unresolvable service_home (missing, non-string, or a symlink loop) is
        # left uncompared rather than crashing or false-alarming.
        if (
            isinstance(service_home, str)
            and service_home
            and _same_dir(service_home, skein_home()) is False
        ):
            checks.append(
                _check(
                    "api service",
                    False,
                    f"responding at {base_url}, but it serves SKEIN_HOME {service_home}, "
                    f"not this CLI's {skein_home()}",
                    hint="The CLI and the service read different homes, so your projects "
                    "are invisible to it. Restart skein-server with the same SKEIN_HOME, "
                    "or set SKEIN_URL / SKEIN_HOME so the two agree.",
                )
            )
        else:
            checks.append(_check("api service", True, f"responding at {base_url}", level="info"))

    # CLI/service version skew: the two halves ship in one distribution, so a
    # mismatch means the running service came from a different install.
    if health is None:
        checks.append(_check("version match", True, "skipped, no service to compare", level="info"))
    else:
        server_version = health.get("version")
        if not server_version:
            checks.append(
                _check(
                    "version match",
                    False,
                    "the running service does not report its version, so it predates "
                    f"this CLI ({cli_version})",
                    level="warn",
                    hint="Restart the service so both halves come from this install.",
                )
            )
        elif server_version != cli_version:
            checks.append(
                _check(
                    "version match",
                    False,
                    f"CLI is {cli_version}, service is {server_version}",
                    hint="Restart the service so both halves come from this install.",
                )
            )
        else:
            checks.append(
                _check("version match", True, f"CLI and service both {cli_version}", level="info")
            )

    # Documentation shipped in the wheel — all of it, since `skein info` serves
    # each topic. A checkout built without symlink support packages the link
    # target's path (~30 bytes, no markdown heading) in place of the document, so
    # each doc must be both large enough and shaped like markdown.
    doc_problems = []
    for topic in sorted(INFO_TOPICS):
        # resolve_doc probes with exists(), then the doc is statted and read —
        # any of which raises on an install with broken ownership or modes. A
        # doc that cannot be read is a failed check, not a crash.
        try:
            resolved = resolve_doc(topic)
            if resolved is None:
                doc_problems.append(f"{topic} not installed")
            elif resolved.stat().st_size < MIN_DOC_BYTES or not resolved.read_text(
                errors="replace"
            ).lstrip().startswith("#"):
                doc_problems.append(f"{topic} is a path, not a document ({resolved})")
        except OSError as e:
            doc_problems.append(f"{topic} is unreadable: {e}")
    if doc_problems:
        checks.append(
            _check(
                "packaged docs",
                False,
                "; ".join(doc_problems),
                hint="Reinstall the interskein package; `skein info` needs these. If it "
                "was built from a checkout without symlink support, rebuild on one with "
                "symlinks.",
            )
        )
    else:
        checks.append(
            _check("packaged docs", True, f"{len(INFO_TOPICS)} topic(s) installed", level="info")
        )

    # Current directory: is this a SKEIN project? "Cannot tell" is not "no" —
    # a walk or probe that raised is a failing check that names its error, never
    # the benign warning below.
    if project_root_error is not None:
        checks.append(
            _check(
                "current project",
                False,
                f"cannot tell whether this is a SKEIN project: {project_root_error}",
                hint="An ancestor of the current directory denies search, so the "
                ".skein/ walk cannot run. Fix that directory's mode, or run doctor "
                "from a readable directory.",
            )
        )
    elif project_root is None:
        checks.append(
            _check(
                "current project",
                False,
                "not inside a SKEIN project",
                level="warn",
                hint="Run `skein init --project NAME` in your project directory.",
            )
        )
    elif project_config_error is not None:
        checks.append(
            _check(
                "current project",
                False,
                f"{project_root} has .skein/ but it cannot be probed: {project_config_error}",
                hint="Fix the ownership/mode of .skein/ (left behind by a stray "
                "sudo?), or remove it and re-run `skein init`.",
            )
        )
    elif project_id and project_id in registry:
        project_info = registry[project_id]
        registered_data_value = (
            project_info.get("data_dir") if isinstance(project_info, dict) else None
        )
        current_data_dir = project_root / ".skein" / "data"
        registered_data_dir = (
            Path(registered_data_value)
            if isinstance(registered_data_value, str) and registered_data_value
            else None
        )
        if registered_data_dir is not None and project_data_dirs_match(
            registered_data_dir, current_data_dir
        ):
            checks.append(
                _check(
                    "current project",
                    True,
                    f"'{project_id}' at {project_root}",
                    level="info",
                )
            )
        else:
            checks.append(
                _check(
                    "current project",
                    False,
                    f"{project_root} identifies as '{project_id}' with data at "
                    f"{current_data_dir}, but the registry owns it at "
                    f"{registered_data_dir or '(invalid data_dir)'}",
                    hint="This may be a copied project. Work from the registered "
                    "project, or remove the copy's .skein/ and initialize it with "
                    "a different project ID. Use --project only when you intend "
                    "to operate on the registered project.",
                )
            )
    else:
        checks.append(
            _check(
                "current project",
                False,
                f"{project_root} has .skein/ but "
                + (
                    f"'{project_id}' is not in the registry"
                    if project_id
                    else "no project id in .skein/config.json"
                ),
                hint="Re-register it with `skein init --project NAME`, "
                "or remove .skein/ and start over.",
            )
        )

    # git is not required by SKEIN itself, but agents work in repos.
    git_path = shutil.which("git")
    checks.append(
        _check(
            "git",
            git_path is not None,
            git_path or "git is not on PATH",
            level="warn",
            hint="Install git; SKEIN projects normally live in repositories.",
        )
    )

    return checks


def doctor_base_url(ctx_url: Optional[str] = None) -> str:
    """get_base_url for `skein doctor`: same priority order, but a config that
    cannot even be probed resolves on to the next source instead of raising.

    Doctor runs before a broken install is repaired, so it must reach its
    checks; doctor_checks reports the unprobeable config as a failing check.
    Every other command resolves through get_base_url and fails loudly.
    """
    return resolve_base_url(ctx_url, tolerant=True).url


@cli.command()
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def doctor(ctx, output_json):
    """Diagnose this SKEIN installation.

    Checks the install itself, the SKEIN home, the project registry, the API
    service, whether the CLI and service versions agree, the packaged docs, and
    the current project.

    Exit codes:
    - 0: no failing checks (warnings are reported but do not fail)
    - 1: at least one failing check
    """
    url_resolution = resolve_base_url(ctx.obj.get("url"), tolerant=True)
    base_url = url_resolution.url
    checks = doctor_checks(base_url, url_resolution)
    failed = [c for c in checks if not c["ok"] and c["level"] == "error"]
    warned = [c for c in checks if not c["ok"] and c["level"] == "warn"]

    if output_json:
        click.echo(
            json.dumps(
                {"healthy": not failed, "checks": checks, "server_url": base_url},
                indent=2,
            )
        )
    else:
        for check in checks:
            if check["ok"]:
                mark = "✓"
            elif check["level"] == "warn":
                mark = "!"
            else:
                mark = "✗"
            click.echo(f"{mark} {check['name']}: {check['detail']}")
            if not check["ok"] and check.get("hint"):
                click.echo(f"  → {check['hint']}")
        click.echo()
        if failed:
            click.echo(f"{len(failed)} check(s) failed.")
        elif warned:
            click.echo("SKEIN is working. Some checks reported warnings.")
        else:
            click.echo("SKEIN is healthy.")

    raise SystemExit(1 if failed else 0)


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
        folios_list = [f for f in folios_list if agent.lower() in f.get("created_by", "").lower()]

    # Filter by grep
    if grep:
        folios_list = [f for f in folios_list if grep.lower() in f.get("content", "").lower()]

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
                    if datetime.fromisoformat(f["created_at"].replace("Z", "+00:00")) >= since_dt
                ]

        if until:
            until_dt = parse_time_filter(until)
            if until_dt:
                folios_list = [
                    f
                    for f in folios_list
                    if datetime.fromisoformat(f["created_at"].replace("Z", "+00:00")) <= until_dt
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
            data = _query_project(project_id, "GET", "/sites", base_url, agent_id, params=params)
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

        click.echo(f"Found {total} site(s) across {len(per_project)} project(s):\n")
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
            status_indicator = "" if s.get("status", "active") == "active" else f" [{s['status']}]"
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

    brief_data = make_folio_request("GET", brief_id, base_url, agent_id)

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

    playbook_data = make_folio_request("GET", playbook_id, base_url, agent_id)

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


@cli.command(hidden=True)
@click.argument("brief_id")
@click.pass_context
def resume(ctx, brief_id):
    """Deprecated: Use 'ignite' instead."""
    ctx.invoke(ignite_start, brief_id=brief_id, mantle=None, message=None)


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
    help="Filter by folio type (issue, brief, friction, finding, summary, notion, moment)",
)
@click.option("--status", help="Filter by status (open, closed, investigating)")
@click.option("--assigned", help="Filter by assignee")
@click.option("--since", help="Only items after this time (e.g., '1hour', '2days', ISO timestamp)")
@click.option("--sort", help="Sort by: created (default), created_asc, relevance")
@click.option("--limit", type=int, default=50, help="Max results (default: 50)")
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
            click.echo(f"Found {grand_total} folio(s) across {len(per_project)} project(s):\n")

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
@click.option("--limit", type=int, help="Limit results per resource type (default: 50, max: 500)")
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
                    click.echo(f"📑 Folios ({folios_total} total, showing {len(folios)}):\n")

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
                        caps = (
                            ", ".join(a.get("capabilities", []))
                            if a.get("capabilities")
                            else "none"
                        )
                        click.echo(f"  {status_icon} {a['agent_id']}: {a.get('name', 'No name')}")
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
    except Exception:
        server_status = "unreachable"

    # Get project config
    project_config = get_project_config()
    project_name = project_config.get("project_id", "unknown") if project_config else "unknown"

    # Get all folios for counts
    try:
        all_folios = make_request("GET", "/folios", base_url, agent_id)
    except Exception:
        all_folios = []

    # Count open issues and frictions
    open_issues = len(
        [f for f in all_folios if f.get("type") == "issue" and f.get("status", "open") == "open"]
    )
    open_frictions = len(
        [f for f in all_folios if f.get("type") == "friction" and f.get("status", "open") == "open"]
    )
    pending_briefs = len(
        [f for f in all_folios if f.get("type") == "brief" and f.get("status", "open") == "open"]
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
            data = _query_project(project_id, "GET", "/activity", base_url, agent_id, params=params)
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

        click.echo(f"Recent activity across {len(per_project)} project(s):\n")
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


@post.command("moment")
@click.argument("site_id")
@click.argument("title")
@click.option("--details", "-d", help="Additional details (title used if not provided)")
@click.pass_context
def post_moment(ctx, site_id, title, details):
    """Post a moment intended for public sharing.

    A moment marks an event or a weighty observation. Posting one records the
    public intent; it does not publish anything by itself.

    Examples:
        skein post moment skein-dev "Released the first public build"
        skein post moment speakbot:skein-dev "The migration is complete"
    """
    validate_positional_args(site_id, title, command_name="post moment")
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    site_id, project_override = parse_post_site_id(site_id)

    data = {
        "type": "moment",
        "site_id": site_id,
        "title": title,
        "content": details or title,
        "metadata": {},
    }

    result = make_request(
        "POST", "/folios", base_url, agent_id, json=data, project_id=project_override
    )
    click.echo(f"Posted moment: {result['folio_id']}")


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
    type=click.Choice(["confirmed", "disconfirmed", "inconclusive", "deferred", "blocked"]),
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
            folios = [f for f in folios if f.get("status", "").split("\n")[0] == verdict]

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
    notion_folio = make_folio_request("GET", notion_id, base_url, agent_id)
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
        make_folio_request(
            "PATCH",
            notion_id,
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
def moment(ctx, site_id, title, details):
    """Post a moment (deprecated: use 'skein post moment')."""
    ctx.invoke(post_moment, site_id=site_id, title=title, details=details)


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
            tender = make_folio_request("GET", thread_id, base_url, agent_id)
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
            data = _query_project(project_id, "GET", f"/folios/{folio_id}", base_url, agent_id)
            if data and isinstance(data, dict) and data.get("folio_id"):
                data["source_project"] = project_id
                hits.append(data)

        if output_json:
            click.echo(json.dumps({"results": hits}, indent=2, default=str))
            return

        if not hits:
            raise click.ClickException(f"Folio '{folio_id}' not found in any registered project")

        for i, data in enumerate(hits):
            if i > 0:
                click.echo()
            _render_folio(data, base_url, agent_id, no_pager and i < len(hits) - 1)
        return

    try:
        folio_data = make_folio_request("GET", folio_id, base_url, agent_id)
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
    output_lines.append(f"Date:  {folio_data.get('created_at', '')[:19].replace('T', ' ')}")
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
@click.option("--all", "all_projects", is_flag=True, help="Search all registered projects")
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
    folio_data = make_folio_request("GET", folio_id, base_url, agent_id)

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
            escaped_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
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
    html.append("<tr>" + "".join(f"<th>{format_inline(c)}</th>" for c in rows[0]) + "</tr>")
    for row in rows[1:]:
        html.append("<tr>" + "".join(f"<td>{format_inline(c)}</td>" for c in row) + "</tr>")
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

    result = make_folio_request(
        "PATCH",
        folio_id,
        base_url,
        agent_id,
        json=update_data,
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

    result = make_folio_request(
        "POST",
        folio_id,
        base_url,
        agent_id,
        suffix="/move",
        json=move_data,
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
                click.echo(f"  {folio_type.upper()} ({len(by_type[folio_type])} item(s)):")
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
                click.echo(f"  {folio_type.upper()} ({len(by_type[folio_type])} item(s)):")
                for f in by_type[folio_type]:
                    status_str = f"[{f['status']}]" if f.get("status") else ""
                    # Format created_at date
                    created_at = f.get("created_at", "")
                    if created_at:
                        # Parse ISO format and display as YYYY-MM-DD
                        try:
                            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                            date_str = dt.strftime("%Y-%m-%d")
                        except (ValueError, AttributeError):
                            date_str = created_at[:10] if len(created_at) >= 10 else created_at
                    else:
                        date_str = ""

                    click.echo(f"    {f['folio_id']} {status_str} {date_str}")
                    click.echo(f"      {f['title'][:80]}{'...' if len(f['title']) > 80 else ''}")

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
        from_threads = make_request(
            "GET", "/threads", base_url, agent_id, params={"from_id": res_id}
        )
        to_threads = make_request("GET", "/threads", base_url, agent_id, params={"to_id": res_id})

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
                is_last_thread = (i == len(node["threads"]) - 1) and not node["children"]
                thread_connector = "└── " if is_last_thread else "├── "

                direction = "→" if thread["from_id"] == node["id"] else "←"
                other_id = thread["to_id"] if thread["from_id"] == node["id"] else thread["from_id"]

                click.echo(
                    f"{thread_prefix}{thread_connector}[{thread['type'].upper()}] {direction} {other_id}"
                )
                if thread.get("content"):
                    content_prefix = thread_prefix + ("    " if is_last_thread else "│   ")
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
        raise click.ClickException("Must set SKEIN_AGENT_ID or use --agent flag to reply")

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
    type=click.Choice(["open", "closed", "investigating", "resolved", "blocked", "in-progress"]),
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
        raise click.ClickException("Must set SKEIN_AGENT_ID or use --agent flag to set status")

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
        raise click.ClickException("Must set SKEIN_AGENT_ID or use --agent flag to close")

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
        raise click.ClickException("Must set SKEIN_AGENT_ID or use --agent flag to register")

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
    suffix = agent_id.split("-")[-1] if agent_id else "retroactive"
    suggested_name = f"Agent {suffix}"

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
            brief = make_folio_request("GET", brief_id, base_url, agent_id)
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
                mantle_folio = make_folio_request("GET", mantle, base_url, agent_id)
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
        make_request("POST", "/roster/register", base_url, suggested_name, json=register_data)
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

    # Patch rather than re-register: POST /roster/register replaces the roster
    # record and would erase ignite metadata such as ignited_from and ignited_at.
    try:
        make_request(
            "PATCH",
            f"/roster/{agent_id}",
            base_url,
            agent_id,
            json={
                "status": "active",
                "metadata": {"ready_at": datetime.now().isoformat()},
            },
        )
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


MAX_RETROACTIVE_FOLIOS = 25
MAX_CEREMONY_FOLIO_TITLES = 20
FOLIO_SUMMARY_LABELS = {
    "issue": "issues",
    "friction": "frictions",
    "brief": "briefs",
    "summary": "summaries",
    "finding": "findings",
    "notion": "notions",
    "moment": "moments",
    "tender": "tenders",
    "playbook": "playbooks",
    "mantle": "mantles",
    "writ": "writs",
    "plan": "plans",
    "hypothesis": "hypotheses",
}


def _summarize_folios(folios: List[dict]) -> Dict[str, int]:
    """Count every supported folio type for torch and complete output."""
    return {
        label: sum(1 for folio in folios if folio.get("type") == folio_type)
        for folio_type, label in FOLIO_SUMMARY_LABELS.items()
    }


def _folio_ceremony_line(folio: dict) -> str:
    """Screen-reader-friendly title then folio ID for ceremony inventories."""
    folio_id = folio.get("folio_id", "unknown-folio")
    title = folio.get("title") or f"Untitled {folio.get('type', 'folio')}"
    return f"  {title} {folio_id}"


def _show_folio_inventory(heading: str, folios: List[dict]) -> None:
    """Show titled folios without letting a large session swamp the ceremony."""
    if not folios:
        return
    click.echo(heading)
    for folio in folios[:MAX_CEREMONY_FOLIO_TITLES]:
        click.echo(_folio_ceremony_line(folio))
    remaining = len(folios) - MAX_CEREMONY_FOLIO_TITLES
    if remaining > 0:
        click.echo(f"  ...and {remaining} more")
    click.echo()


def _get_ignition_brief(base_url: str, agent_id: str, roster_data: dict):
    """Load the brief this agent ignited from, whether open or closed."""
    brief_id = roster_data.get("metadata", {}).get("ignited_from")
    if not brief_id:
        return None
    try:
        brief = make_folio_request("GET", brief_id, base_url, agent_id)
    except Exception:
        return None
    return brief if brief.get("type") == "brief" else None


def _show_ignition_brief(brief: Optional[dict]) -> None:
    if not brief:
        return
    status = str(brief.get("status", "unknown")).lower()
    title = brief.get("title") or "Untitled ignition brief"
    click.echo("Ignition brief:")
    click.echo(f"  {title} [{status}] {brief.get('folio_id', 'unknown-brief')}")
    click.echo()


def _get_latest_attributions(base_url: str, agent_id: str) -> Dict[str, str]:
    """Return the effective author per attributed folio.

    Each attribution is an auditable thread. The latest one is effective;
    ``created_by`` itself cannot be rewritten because it is part of the folio's
    content digest.
    """
    threads = make_request(
        "GET",
        "/threads",
        base_url,
        agent_id,
        params={"type": "attribution"},
    )

    latest: Dict[str, tuple] = {}
    for thread in threads:
        folio_id = thread.get("from_id")
        attributed_to = thread.get("to_id")
        if not folio_id or not attributed_to:
            continue
        ordering = (str(thread.get("created_at", "")), thread.get("thread_id", ""))
        if folio_id not in latest or ordering > latest[folio_id][0]:
            latest[folio_id] = (ordering, attributed_to)
    return {folio_id: entry[1] for folio_id, entry in latest.items()}


def _get_session_folios(all_folios: List[dict], base_url: str, agent_id: str) -> List[dict]:
    """Folios effectively authored by this session.

    A latest attribution overrides the immutable, self-declared ``created_by``;
    otherwise ``created_by`` remains the owner.
    """
    attributions = _get_latest_attributions(base_url, agent_id)
    session_folios = []
    for folio in all_folios:
        folio_id = folio.get("folio_id")
        effective_author = attributions.get(folio_id, folio.get("created_by"))
        if effective_author == agent_id:
            session_folios.append(folio)
    return session_folios


def _attribute_folios(base_url: str, agent_id: str, folio_ids) -> List[dict]:
    """Attribute all named folios to ``agent_id`` after validating the batch."""
    unique_ids = list(dict.fromkeys(folio_ids))
    if not unique_ids:
        return []
    if len(unique_ids) > MAX_RETROACTIVE_FOLIOS:
        raise click.ClickException(
            f"At most {MAX_RETROACTIVE_FOLIOS} folios can be attributed in one "
            f"completion; received {len(unique_ids)}."
        )

    failures = []
    folios = {}
    for folio_id in unique_ids:
        try:
            folios[folio_id] = make_folio_request("GET", folio_id, base_url, agent_id)
        except Exception as e:
            failures.append((folio_id, str(e)))

    # Validate the entire batch before minting any attribution threads.
    if failures:
        details = "\n".join(f"  {folio_id}: {error}" for folio_id, error in failures)
        raise click.ClickException(f"Could not attribute {len(failures)} folio(s):\n{details}")

    try:
        current = _get_latest_attributions(base_url, agent_id)
    except Exception as e:
        raise click.ClickException(f"Could not load existing folio attributions: {e}")
    attributed = []
    for folio_id in unique_ids:
        if current.get(folio_id) == agent_id:
            attributed.append(folios[folio_id])
            continue

        thread_data = {
            "from_id": folio_id,
            "to_id": agent_id,
            "type": "attribution",
            "content": f"Authorship attributed to {agent_id} during retroactive torch",
            "weaver": agent_id,
        }
        make_request("POST", "/threads", base_url, agent_id, json=thread_data)
        current[folio_id] = agent_id
        attributed.append(folios[folio_id])

    return attributed


@cli.command("torch")
@click.option(
    "--retroactive",
    is_flag=True,
    help="Close work done without ignite; assign a name and attribute folios at complete.",
)
@click.option(
    "--preview",
    "--dry-run",
    "preview",
    is_flag=True,
    help="Show the torch ceremony without changing roster state.",
)
@click.pass_context
def torch_start(ctx, retroactive, preview):
    """
    Begin retirement - Prepare to torch.

    Usage:
        skein torch
        skein torch --retroactive
        skein torch --preview

    After filing any remaining work:
        skein complete [FOLIO_ID...] [--summary "..."]
    """
    _torch_start(ctx, retroactive=retroactive, preview=preview)


def _torch_start(ctx, retroactive=False, preview=False):
    """
    Begin retirement process - Prepare to torch.

    Usage:
        skein torch
        skein torch --retroactive
        skein torch --preview

    After filing any remaining work:
        skein complete [--summary "..."]
    """
    if preview and retroactive:
        raise click.ClickException(
            "--preview/--dry-run cannot be combined with --retroactive because "
            "retroactive torch must register a recovered identity. Run "
            "'skein torch --retroactive' when ready."
        )

    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = get_agent_id(ctx.obj.get("agent"), base_url)

    if not agent_id and not retroactive:
        raise click.ClickException(
            "Must set SKEIN_AGENT_ID or use --agent flag to torch.\n\n"
            "Already did the work without igniting? Run:\n"
            "  skein torch --retroactive"
        )

    if retroactive:
        # The work already happened, so create the missing identity directly in
        # retirement rather than pretending the agent still needs orientation.
        agent_id = _generate_suggested_name(base_url, agent_id, None, None)
        name = agent_id
        register_data = {
            "agent_id": agent_id,
            "name": name,
            "status": "retiring",
            "metadata": {"retroactive_torch_at": datetime.now().isoformat()},
        }
        try:
            make_request("POST", "/roster/register", base_url, agent_id, json=register_data)
        except Exception as e:
            raise click.ClickException(f"Could not register retroactive torch identity: {e}")
        roster_data = register_data
    else:
        try:
            roster_data = make_request("GET", f"/roster/{agent_id}", base_url, agent_id)
            name = roster_data.get("name", agent_id)
        except Exception:
            raise click.ClickException(
                f"Agent {agent_id} not found in roster. Must ignite before torching.\n\n"
                "Already did the work without igniting? Run:\n"
                "  skein torch --retroactive"
            )

    ignition_brief = _get_ignition_brief(base_url, agent_id, roster_data)

    # Get agent's SKEIN activity
    agent_folios = []
    try:
        # Folios carry created_by, not "author"/"weaver" -- those keys never
        # existed on a folio, so this previously always matched zero folios.
        # A later attribution overrides created_by for session ownership.
        all_folios = make_request("GET", "/folios", base_url, agent_id)
        agent_folios = _get_session_folios(all_folios, base_url, agent_id)

        work_summary = _summarize_folios(agent_folios)
    except Exception:
        work_summary = {}

    # Retroactive torch was registered directly as retiring above. Normal torch
    # patches the existing entry so ignition metadata and registered_at survive.
    if not retroactive and not preview:
        try:
            make_request(
                "PATCH",
                f"/roster/{agent_id}",
                base_url,
                agent_id,
                json={"status": "retiring"},
            )
        except Exception:
            pass  # Continue even if update fails (server might not support status)

    click.echo("=" * 60)
    heading = "TORCH PREVIEW - Retirement Phase" if preview else "TORCH - Retirement Phase"
    click.echo(heading)
    click.echo("=" * 60)
    click.echo()
    if preview:
        click.echo("Preview only: roster state will not be changed.")
        click.echo()
    click.echo(f"Name: {name}")
    if retroactive:
        click.echo("Session recovered: this work began without ignition.")
    click.echo()

    _show_ignition_brief(ignition_brief)

    if work_summary:
        click.echo("Your SKEIN Activity:")
        nonzero_work = False
        for folio_type, count in work_summary.items():
            if count > 0:
                nonzero_work = True
                click.echo(f"  {folio_type}: {count}")
        if retroactive and not nonzero_work:
            click.echo("  no folios attributed yet")
        click.echo()

    _show_folio_inventory("Work from this session:", agent_folios)

    # Query agent's open work for visibility (work assigned TO them)
    open_issues = []
    open_frictions = []
    try:
        # Get assignment threads pointing to this agent
        all_threads = make_request("GET", "/threads", base_url, agent_id)
        assignment_threads = [
            t for t in all_threads if t.get("type") == "assignment" and t.get("to_id") == agent_id
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
            open_issues = [i for i in open_issues_all if i.get("folio_id") in assigned_folio_ids]
            open_frictions = [
                f for f in open_frictions_all if f.get("folio_id") in assigned_folio_ids
            ]

    except Exception:
        # Continue even if we can't fetch open work
        pass

    # Display open work if any exists
    if open_issues or open_frictions:
        click.echo("=" * 60)
        click.echo("YOUR OPEN WORK")
        click.echo("=" * 60)
        click.echo()

        if open_issues:
            click.echo("Issues assigned to you:")
            for issue in open_issues[:5]:
                click.echo(_folio_ceremony_line(issue))
            if len(open_issues) > 5:
                click.echo(f"  ... and {len(open_issues) - 5} more")
            click.echo()

        if open_frictions:
            click.echo("Frictions assigned to you:")
            for friction in open_frictions[:5]:
                click.echo(_folio_ceremony_line(friction))
            if len(open_frictions) > 5:
                click.echo(f"  ... and {len(open_frictions) - 5} more")
            click.echo()

    click.echo("Before completing retirement, consider what should survive this session:")
    click.echo()
    click.echo("  • Is there a clear next collection of work, or were you asked for a")
    click.echo("    handoff? If so, write one handoff brief. Create any missing unit")
    click.echo("    briefs, thread the relevant briefs to the handoff, summarize the")
    click.echo("    current state, and describe what the next session should do. If the")
    click.echo("    work does not cohere into a next session, do not manufacture one.")
    click.echo()
    click.echo("  • Are there directions or units of work that should exist beyond this")
    click.echo("    session? File briefs for them.")
    click.echo()
    click.echo("  • Did you uncover a larger idea, possibility, or pattern that is not yet")
    click.echo("    a direction? File a notion.")
    click.echo()
    click.echo("  • Did you find a concrete problem that needs repair? File an issue.")
    click.echo()
    click.echo("  • Did the tools, documentation, or process create repeatable friction?")
    click.echo("    File a friction.")
    click.echo()
    click.echo("  • Is any existing work now demonstrably complete? Close it.")
    click.echo()
    click.echo("Examples:")
    click.echo("  Post a brief (--title is required):")
    click.echo(
        '    skein post brief SITE "Describe what the next session should do" '
        '--title "Continue the work"'
    )
    click.echo("  Post a handoff brief from a file (--title is still required):")
    click.echo('    skein post brief SITE - --title "Handoff: continue the work" < handoff.md')
    click.echo("  Thread a relevant brief to the handoff:")
    click.echo('    skein thread brief-RELEVANT brief-HANDOFF reference "Included in handoff"')
    click.echo("  Record an idea that is not yet a direction:")
    click.echo('    skein post notion SITE "A larger idea worth preserving"')
    click.echo("  Record a concrete problem:")
    click.echo('    skein post issue SITE "Concrete problem to repair" --content "What is broken"')
    click.echo("  Record repeatable friction:")
    click.echo('    skein post friction SITE "Repeatable slowdown" --details "Where it happens"')
    click.echo("  Close completed work:")
    click.echo("    skein close issue-20251112-757o --link summary-20251112-5lut")
    click.echo('    skein close friction-20251109-1lfe --note "Fixed by refactoring imports"')
    click.echo()
    click.echo("Writing to SKEIN is optional. Preserve what matters; do not post merely")
    click.echo("to complete the ceremony.")
    click.echo()
    if preview:
        click.echo("Preview only: retirement has not been recorded.")
        click.echo()
        click.echo("To begin retirement:")
        click.echo()
        click.echo(f"  skein --agent {agent_id} torch")
    elif retroactive:
        click.echo("When done:")
        click.echo()
        click.echo("Pass every folio you authored, of any type, to complete:")
        click.echo()
        click.echo(f"  skein --agent {agent_id} complete FOLIO_ID...")
        click.echo()
        click.echo(f"You can include up to {MAX_RETROACTIVE_FOLIOS} folios.")
    else:
        click.echo("When done:")
        click.echo()
        click.echo("  skein complete")
    click.echo()


@cli.command("complete")
@click.argument("folio_ids", nargs=-1)
@click.option("--summary", help="Optional retirement summary")
@click.option(
    "--yield-status",
    "yield_status",
    type=click.Choice(["complete", "partial", "blocked"]),
    help="Yield status for chain (auto-detected from SKEIN_CHAIN_ID)",
)
@click.option("--yield-outcome", "yield_outcome", help="What was accomplished (for yield)")
@click.option("--yield-notes", "yield_notes", help="Notes for next agent in chain")
@click.pass_context
def complete(ctx, folio_ids, summary, yield_status, yield_outcome, yield_notes):
    """
    Complete torch - Retire from roster.

    Usage:
        skein complete
        skein complete brief-20260712-abcd finding-20260712-efgh
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
    ignition_brief = _get_ignition_brief(base_url, agent_id, roster_data)

    attributed = _attribute_folios(base_url, agent_id, folio_ids)
    if attributed:
        click.echo(f"Attributed {len(attributed)} folio(s) to {agent_id}:")
        for folio in attributed:
            click.echo(_folio_ceremony_line(folio))
        click.echo()

    # Get final work summary using attribution when present.
    agent_folios = []
    try:
        all_folios = make_request("GET", "/folios", base_url, agent_id)
        agent_folios = _get_session_folios(all_folios, base_url, agent_id)

        final_work = _summarize_folios(agent_folios)
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
                    "metadata": {"retirement_summary": True},
                }
                result = make_request("POST", "/summary", base_url, agent_id, json=summary_data)
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
        make_request("PATCH", f"/roster/{agent_id}", base_url, agent_id, json=update_data)
    except Exception as e:
        # Log but don't fail - agent can still complete even if status update fails
        click.echo(f"Warning: Could not update roster status: {e}", err=True)

    click.echo("=" * 60)
    click.echo("RETIRED")
    click.echo("=" * 60)
    click.echo()
    click.echo(f"✓ Retired: {name}")
    click.echo()

    _show_ignition_brief(ignition_brief)

    if final_work:
        click.echo("Final Work Summary:")
        for folio_type, count in final_work.items():
            if count > 0:
                click.echo(f"  {folio_type}: {count}")
        click.echo()

    _show_folio_inventory("Left in SKEIN:", agent_folios)

    if summary_id:
        click.echo(f"✓ Summary posted: {summary_id}")
        click.echo()

    click.echo("Thank you for your service. 🔥")
    click.echo()


@cli.command()
@click.argument("agent_id")
@click.option("--capabilities", multiple=True, help="Agent capabilities (can specify multiple)")
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
@click.argument("target", type=click.Choice(["threads", "folios"]))
@click.option("--orphaned", is_flag=True, help="Show orphaned threads (threads only)")
@click.option("--by-weaver", is_flag=True, help="Group by weaver (threads only)")
@click.option("--by-type", is_flag=True, help="Group by type")
@click.option("--by-status", is_flag=True, help="Group by status (folios only)")
@click.option("--by-site", is_flag=True, help="Group by site (folios only)")
@click.option("--all", "show_all", is_flag=True, help="Show all stats")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def stats(ctx, target, orphaned, by_weaver, by_type, by_status, by_site, show_all, output_json):
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
        analyze_threads(base_url, agent_id, orphaned, by_weaver, by_type, show_all, output_json)
    elif target == "folios":
        analyze_folios(base_url, agent_id, by_type, by_status, by_site, show_all, output_json)


def analyze_threads(base_url, agent_id, orphaned, by_weaver, by_type, show_all, output_json):
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


def analyze_folios(base_url, agent_id, by_type, by_status, by_site, show_all, output_json):
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
@click.argument("topic", type=click.Choice(sorted(INFO_TOPICS)))
@click.pass_context
def info(ctx, topic):
    """Display SKEIN documentation.

    Available topics:
        guide           - Comprehensive SKEIN agent guide
        implementation  - Architecture and implementation details
        quickstart      - Quick start guide for SKEIN

    The documents ship inside the package, so these work from a plain install
    with no source checkout.

    Examples:
        skein info quickstart
        skein info guide
    """
    doc_file = resolve_doc(topic)

    if doc_file is None:
        raise click.ClickException(
            f"Documentation for '{topic}' is not installed "
            f"(looked in {packaged_docs_dir()}). Reinstall the interskein package."
        )

    click.echo(doc_file.read_text())


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
    summary = manager.create_full_backup_all_projects(projects_root=projects_root, tag=tag)

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
@click.option("--older-than", "older_than_days", type=int, help="Remove backups older than N days")
@click.option("--dry-run", is_flag=True, help="Show what would be removed without removing")
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
@click.option("--keep-last", type=int, default=30, help="Number of backups to keep (default: 30)")
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
        subprocess.run(["systemctl", "--user", "enable", "skein-backup.timer"], check=True)
        subprocess.run(["systemctl", "--user", "start", "skein-backup.timer"], check=True)
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
        subprocess.run(["systemctl", "--user", "stop", "skein-backup.timer"], check=True)
        subprocess.run(["systemctl", "--user", "disable", "skein-backup.timer"], check=True)
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
@click.option("--dry-run", is_flag=True, help="Show what would be restored without making changes")
@click.option("--confirm", is_flag=True, help="Confirm restore (required for actual restore)")
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
            thread_result = make_request("POST", "/threads", base_url, agent_id, json=thread_data)
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


@shard.command("where")
@click.argument("worktree_name")
@click.option("--path-only", is_flag=True, help="Print just the worktree path")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def shard_where(ctx, worktree_name, output_json, path_only):
    """
    Show where a SHARD's worktree lives and which repo it came from.

    SHARD worktrees live outside the project tree (under SKEIN_HOME, or
    wherever SKEIN_WORKTREES_DIR points), so this is how you find one. The
    origin repo is read from the worktree's own .git file, so it stays
    correct even if the worktree has been moved.

    Examples:
        skein shard where my-feature-20260113-001
        cd "$(skein shard where my-feature-20260113-001 --path-only)"
        skein shard where my-feature-20260113-001 --json
    """
    shard_worktree = get_shard_worktree_module()

    try:
        location = shard_worktree.get_shard_location(worktree_name)
    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to locate SHARD: {e}")

    if path_only:
        click.echo(location["worktree_path"])
        return

    if output_json:
        import json

        click.echo(json.dumps(location, indent=2))
        return

    click.echo(f"SHARD: {location['worktree_name']}")
    click.echo(f"  Worktree:     {location['worktree_path']}")
    click.echo(f"  Origin repo:  {location['project_root']}")
    if location["branch_name"]:
        click.echo(f"  Branch:       {location['branch_name']}")
    click.echo(f"  Worktrees in: {location['worktrees_dir']}")

    if not location["exists"]:
        click.echo()
        click.echo(f"⚠️  Worktree directory does not exist ({location['source']} path)")
    elif not location["registered"]:
        click.echo()
        click.echo("⚠️  Directory exists but git does not list it as a worktree")


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

                diff_output = shard_worktree.get_shard_work_diff(worktree_name, stat_only=show_stat)
            else:
                # No metadata - fall back to regular diff
                click.echo(f"=== DIFF: {worktree_name} ===\n")
                click.echo(f"(No base commit metadata - showing diff from current {base_branch})\n")
                diff_output = shard_worktree.get_shard_diff(worktree_name, stat_only=show_stat)

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
@click.option("--keep-branch", is_flag=True, help="Keep git branch after removing worktree")
@click.option("--chain", is_flag=True, help="Remove entire graft chain (original + all grafts)")
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
def shard_cleanup(ctx, worktree_name, keep_branch, chain, explicit_caller_cwd, assume_yes):
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
                    label = "(original)" if not shard_worktree.is_graft(wt) else "(graft)"
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
        cd <graft worktree path, printed above>
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
        elif not result.get("conflicts"):
            # Paused mid-sequence with NO conflict files - typically a commit
            # that replayed EMPTY on the new base. This is NOT a clean graft
            # (fell-r1): the sequencer still holds the rest, so do not report
            # "Applied cleanly". Surface the pause and how to proceed.
            applied = result.get("commits_applied", 0)
            total = result.get("commits_total", applied)
            pending = result.get("commits_pending", 0)
            click.echo("⏸ Graft PAUSED mid-sequence (no merge conflicts)\n")
            click.echo("Graft created at:")
            click.echo(f"  {result['graft_worktree_path']}/\n")
            click.echo(
                f"{applied}/{total} commit(s) applied, "
                f"{pending} still queued in the cherry-pick sequencer.\n"
            )
            click.echo(
                "A commit replayed empty on the new base (its change is already\n"
                "present), so git stopped without creating it.\n"
            )
            click.echo("Continue the sequence:")
            click.echo(f"  cd {result['graft_worktree_path']}")
            click.echo("  git cherry-pick --skip       # drop the now-empty commit")
            click.echo("  (or `git cherry-pick --continue` to keep it as an empty commit)")
            click.echo("  (repeat until the sequencer is empty - do NOT stop early)\n")
            click.echo("Then merge:")
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

            # Surface that the graft is paused MID-SEQUENCE with commits still
            # queued - merging now would drop them (see finding-20260530-3jnx).
            applied = result.get("commits_applied", 0)
            total = result.get("commits_total", applied)
            pending = result.get("commits_pending", 0)
            click.echo(
                f"Graft PAUSED mid-sequence: {applied}/{total} commit(s) applied, "
                f"{pending} still queued in the cherry-pick sequencer.\n"
            )
            click.echo("Resolve conflicts:")
            click.echo(f"  cd {result['graft_worktree_path']}")
            for f in result["conflicts"]:
                click.echo(f"  (edit {f} to resolve conflicts)")
            click.echo("  git add <resolved files>")
            click.echo("  git cherry-pick --continue")
            click.echo(
                "  (repeat resolve + --continue for any further conflicts until the\n"
                "   sequencer is empty - do NOT stop after the first commit)\n"
            )
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

            # Post tender folio with status=complete and auto-close it
            tender_id = _post_merge_tender(
                ctx, base_url, agent_id, worktree_name, shard_info, metadata
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
                    worktrees_dir = shard_worktree.get_worktrees_dir()
                    for wt in chain:
                        click.echo(f"  - {worktrees_dir / wt}/")
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


def _post_merge_tender(ctx, base_url, agent_id, worktree_name, shard_info, metadata):
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
        branch_name = metadata.get("branch_name", shard_info.get("branch_name", "unknown"))
    else:
        summary_text = "Merged"
        files_list = []
        commits = 0
        branch_name = shard_info.get("branch_name", "unknown")

    files_str = "\n".join(f"  - {f}" for f in files_list[:20])
    if len(files_list) > 20:
        files_str += f"\n  ... and {len(files_list) - 20} more"

    content = f"""## Tender: {worktree_name}

**Status:** complete (merged)

### Summary
{summary_text}

### Changes
- **Commits:** {commits}
- **Branch:** {branch_name}

### Files Modified
{files_str if files_str else "  (none)"}
"""

    folio_data = {
        "type": "tender",
        "site_id": site,
        "title": (
            make_title_from_content(summary_text) if summary_text else f"Merged: {worktree_name}"
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


@shard.command("review")
@click.option(
    "--stale-days",
    default=7,
    type=int,
    help="Days without commits to consider stale (default: 7)",
)
@click.argument("worktree_name", required=False)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def shard_review(ctx, stale_days, worktree_name, output_json):
    """
    Show the SHARD review queue, or inspect one SHARD by name.

    With no name, groups shards by status:
    - READY: Has commits, clean working tree, no conflicts (merge candidates)
    - NEEDS_COMMIT: Has uncommitted changes
    - CONFLICTS: Would have merge conflicts with master
    - STALE: No commits and older than --stale-days

    With a worktree name, this is an alias for `shard inspect`.

    Examples:
        skein shard review
        skein shard review --stale-days 3
        skein shard review my-feature-20260113-001
        skein shard review --json
    """
    if worktree_name:
        ctx.invoke(
            shard_inspect,
            worktree_name=worktree_name,
            output_json=output_json,
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
@click.option("--site", help="Site to post tender folio (default: derived from project)")
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
"""

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
                        context_parts.append(f"{base_branch} +{base_ahead} (no conflicts)")
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

                click.echo()  # Blank line between entries

            click.echo("Commands:")
            click.echo("  skein shard review <name>    # View details")
            click.echo("  skein shard diff <name>      # View work diff")
            click.echo("  skein shard merge <name>     # Merge to base branch")
            click.echo("  skein shard graft <name>     # Create graft to resolve conflicts")

    except shard_worktree.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to triage SHARDs: {e}")


def _load_xgun_api():
    """Load xgun only when shard inspection asks for a quality reading.

    xgun is optional for SKEIN, so importing client.cli must not depend on it.
    Keeping this boundary small also gives tests one place to substitute the
    typed library API without recreating an installed xgun package.
    """
    from xgun.artifact import ArtifactError, resolve_diff
    from xgun.qgun import scan as qgun_scan
    from xgun.qgun.tool_run import check_did_not_run
    from xgun.sgun import sniff as sgun_sniff

    return resolve_diff, ArtifactError, qgun_scan, sgun_sniff, check_did_not_run


def _xgun_not_run(status, message):
    """Return one visible shape for every xgun scan that did not complete."""
    return {
        "status": status,
        "message": (
            f"{message} Quality reading is incomplete. "
            "Raise this before merge and rerun `skein shard inspect`."
        ),
    }


def _serialize_shard_xgun(
    worktree_path,
    base_branch,
    reading,
    smells,
    check_did_not_run,
):
    """Translate xgun's typed reading into shard inspect's public JSON shape."""
    failed_checks = []
    signals = []
    for signal in reading.signals:
        level = signal.level.value if hasattr(signal.level, "value") else str(signal.level)
        signal_data = {
            "check": signal.check,
            "level": level,
            "message": signal.message,
        }
        if check_did_not_run(signal):
            signal_data["did_not_run"] = True
            if signal.check not in failed_checks:
                failed_checks.append(signal.check)
        signals.append(signal_data)

    flags = [
        {
            "check": flag.check,
            "file": flag.file,
            "line": flag.line,
            "message": flag.message,
            "severity": getattr(flag, "severity", "high"),
        }
        for flag in reading.flags
    ]
    smell_data = [
        {
            "kind": smell.kind,
            "file": smell.file,
            "line": smell.line,
            "severity": smell.severity,
            "reason": smell.reason,
        }
        for smell in smells
    ]

    result = {
        "status": "incomplete" if failed_checks else "completed",
        "artifact": worktree_path,
        "artifact_type": "refs",
        "comparison": f"{base_branch}...HEAD",
        # Preserve the successful subprocess integration's public JSON fields
        # while adding the explicit status used by the typed integration.
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "qgun": {
            "passed": reading.passed,
            "flags": flags,
            "signals": signals,
            "stats": reading.stats,
        },
        "sgun": {"smells": smell_data, "files_checked": []},
        "summary": {
            "passed": reading.passed and not smell_data and not failed_checks,
            "checks_failed": failed_checks,
            "flags": len(flags),
            "signals": len(signals),
            "smells": len(smell_data),
        },
    }
    if failed_checks:
        count = len(failed_checks)
        result["message"] = (
            f"{count} xgun check(s) could not run: {', '.join(failed_checks)}. "
            "Quality reading is incomplete. "
            "Raise this before merge and rerun `skein shard inspect`."
        )
    return result


def _run_shard_xgun_in_process(worktree_path, base_branch):
    """Run xgun in-process for a shard's explicit base...HEAD diff.

    The library owns diff resolution, governed external-tool execution, and the
    structured did-not-run marker. SKEIN owns only its inspect-specific result
    shape and presentation.
    """
    try:
        (
            resolve_diff,
            artifact_error,
            qgun_scan,
            sgun_sniff,
            check_did_not_run,
        ) = _load_xgun_api()
    except Exception as exc:
        return _xgun_not_run(
            "unavailable",
            f"xgun is unavailable to {sys.executable} "
            f"({type(exc).__name__}: {exc}).",
        )

    if not base_branch:
        return _xgun_not_run(
            "not_run",
            "xgun did not run because the shard base branch could not be determined.",
        )

    try:
        diff_text, changed_files, content_map = resolve_diff(
            worktree_path,
            base_branch,
            "HEAD",
            None,
        )
        reading = qgun_scan(diff_text, content_map)
        smells = sgun_sniff(changed_files) if changed_files else []
    except artifact_error as exc:
        return _xgun_not_run("error", f"xgun could not resolve the shard diff: {exc}.")
    except Exception as exc:
        return _xgun_not_run(
            "error",
            f"xgun failed with {type(exc).__name__}: {exc}.",
        )

    try:
        return _serialize_shard_xgun(
            worktree_path,
            base_branch,
            reading,
            smells,
            check_did_not_run,
        )
    except Exception as exc:
        return _xgun_not_run(
            "error",
            f"xgun returned an unusable result ({type(exc).__name__}: {exc}).",
        )


_XGUN_TOTAL_TIMEOUT_SECONDS = 60.0


def _xgun_worker(send_conn, worktree_path, base_branch):
    """Run one typed scan in an owned process and return its public result."""
    try:
        # Give external tools launched by xgun an owned process group so a total
        # timeout can stop the worker and any still-running checker together.
        os.setsid()
    except OSError:
        pass

    try:
        result = _run_shard_xgun_in_process(worktree_path, base_branch)
    except BaseException as exc:
        result = _xgun_not_run(
            "error",
            f"xgun worker failed with {type(exc).__name__}: {exc}.",
        )

    try:
        send_conn.send(result)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        send_conn.close()


def _stop_xgun_worker(process):
    """Stop an owned xgun worker and its checker process group."""
    if process.pid is None:
        return
    try:
        os.killpg(process.pid, signal_module.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()
    except OSError:
        process.terminate()

    process.join(timeout=1)
    if not process.is_alive():
        return

    try:
        os.killpg(process.pid, signal_module.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
    except OSError:
        process.kill()
    process.join(timeout=1)


def _run_shard_xgun(
    worktree_path,
    base_branch,
    timeout=_XGUN_TOTAL_TIMEOUT_SECONDS,
):
    """Run typed xgun integration with one total wall-clock deadline."""
    receive_conn = None
    send_conn = None
    process = None
    try:
        context = multiprocessing.get_context("fork")
        receive_conn, send_conn = context.Pipe(duplex=False)
        process = context.Process(
            target=_xgun_worker,
            args=(send_conn, worktree_path, base_branch),
            daemon=True,
        )
        process.start()
    except (OSError, RuntimeError, ValueError) as exc:
        if receive_conn is not None:
            receive_conn.close()
        if send_conn is not None:
            send_conn.close()
        if process is not None and process.pid is not None and process.is_alive():
            _stop_xgun_worker(process)
        return _xgun_not_run(
            "error",
            f"xgun worker could not start ({type(exc).__name__}: {exc}).",
        )

    send_conn.close()
    try:
        if not receive_conn.poll(timeout):
            _stop_xgun_worker(process)
            return _xgun_not_run(
                "error",
                f"xgun timed out after {timeout:g} seconds.",
            )

        try:
            result = receive_conn.recv()
        except (EOFError, OSError) as exc:
            return _xgun_not_run(
                "error",
                f"xgun worker returned no result ({type(exc).__name__}: {exc}).",
            )

        process.join(timeout=1)
        if process.is_alive():
            _stop_xgun_worker(process)
        return result
    finally:
        receive_conn.close()
        if process.is_alive():
            _stop_xgun_worker(process)


def _render_shard_xgun(xgun_result):
    """Render the xgun portion of human shard-inspect output."""
    click.echo("=== Code Quality (xgun) ===")
    click.echo()

    status = xgun_result["status"]
    if status in {"unavailable", "not_run", "error"}:
        click.echo(f"! {xgun_result['message']}")
        click.echo()
        return

    summary = xgun_result["summary"]
    flags_count = summary["flags"]
    signals_count = summary["signals"]
    smells_count = summary["smells"]
    counts = f"{signals_count} signals, {flags_count} flags, {smells_count} smells"

    if status == "incomplete":
        click.echo(f"! Quality: Incomplete ({counts})")
        click.echo(f"  {xgun_result['message']}")
    elif summary["passed"]:
        click.echo(f"✓ Quality: Passed ({counts})")
    else:
        click.echo(f"✗ Quality: Issues detected ({counts})")

    flags = xgun_result["qgun"]["flags"]
    if flags:
        click.echo()
        click.echo(f"Flags ({len(flags)}):")
        for flag in flags[:10]:
            loc = (
                f"{flag.get('file', '?')}:{flag['line']}"
                if flag.get("line")
                else flag.get("file", "?")
            )
            click.echo(f"  {loc} [{flag.get('check', '?')}] {flag.get('message', '')}")
        if len(flags) > 10:
            click.echo(f"  ... and {len(flags) - 10} more")

    signals = xgun_result["qgun"]["signals"]
    if signals:
        click.echo()
        click.echo(f"Signals ({len(signals)}):")
        did_not_run = [signal for signal in signals if signal.get("did_not_run")]
        ordinary = [signal for signal in signals if not signal.get("did_not_run")]
        # A display cap may hide routine signals, never the explanation for a
        # check that did not execute. Show every such failure first, then fill
        # the ordinary five-signal budget when room remains.
        shown_signals = did_not_run + ordinary[: max(0, 5 - len(did_not_run))]
        for signal in shown_signals:
            level = signal.get("level", "?")
            marker = " did-not-run" if signal.get("did_not_run") else ""
            label = f"[{level}{marker}] [{signal.get('check', '?')}]"
            click.echo(f"  {label} {signal.get('message', '')}")
        hidden_count = len(signals) - len(shown_signals)
        if hidden_count:
            click.echo(f"  ... and {hidden_count} more")

    smells = xgun_result["sgun"]["smells"]
    if smells:
        click.echo()
        click.echo(f"Smells ({len(smells)}):")
        for smell in smells[:5]:
            loc = (
                f"{smell.get('file', '?')}:{smell['line']}"
                if smell.get("line")
                else smell.get("file", "?")
            )
            click.echo(f"  {loc} [{smell.get('kind', '?')}] {smell.get('reason', '')}")
        if len(smells) > 5:
            click.echo(f"  ... and {len(smells) - 5} more")

    click.echo()


@shard.command("inspect")
@click.argument("worktree_name")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def shard_inspect(ctx, worktree_name, output_json):
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

        base_branch = drift_info.get("base_branch")
        if not base_branch:
            try:
                from skein import shard as shard_module

                base_branch = shard_module._get_shard_base_branch(worktree_name)
            except Exception:
                base_branch = None

        xgun_result = _run_shard_xgun(shard_info["worktree_path"], base_branch)
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
                click.echo(f"  Base: {base_commit}" + (f" ({base_date})" if base_date else ""))
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
                click.echo(f"Tender: {tender_info['folio_id']} (confidence: {conf_str})")
                if tender_info.get("summary"):
                    click.echo(f"  {tender_info['summary']}")
                click.echo()

            _render_shard_xgun(xgun_result)

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
                click.echo("This will cherry-pick only your commits onto the base branch.")
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
            result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=not verbose, text=True)

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


@cli.command()
@click.argument("refs", nargs=-1)
@click.option(
    "--to",
    "instance_url",
    required=True,
    help="Target station publish URL (e.g. https://ingress.example)",
)
@click.option(
    "--folio-hash",
    "folio_hashes",
    multiple=True,
    help="Publish a folio by content hash (repeatable)",
)
@click.option(
    "--thread", "thread_hashes", multiple=True, help="Include a thread by its hash (repeatable)"
)
@click.option(
    "--site",
    "site_id",
    help=(
        "Publish a named workbench site. With no refs, selects every current non-site "
        "folio head in that site; refs/--folio-hash select an exact subset."
    ),
)
@click.option(
    "--slug",
    "site_slug",
    help="Public /site/SLUG name (defaults to the --site value)",
)
@click.option(
    "--token", default=None, help="OIDC token from a prior login (mutually exclusive with --login)"
)
@click.option(
    "--login",
    is_flag=True,
    help="Run the interactive Sigstore login here and sign with it (no --token juggling)",
)
@click.option(
    "--oob",
    "force_oob",
    is_flag=True,
    help="With --login: out-of-band code flow for headless/SSH (no local browser)",
)
@click.option("--dry-run", is_flag=True, help="Resolve + lint only; sign and send nothing")
@click.option("--json", "output_json", is_flag=True, help="Output the raw result as JSON")
@click.pass_context
def publish(
    ctx,
    refs,
    instance_url,
    folio_hashes,
    thread_hashes,
    site_id,
    site_slug,
    token,
    login,
    force_oob,
    dry_run,
    output_json,
):
    """Publish declared folios/threads to a remote station as a signed manifest.

    Without --site, names an EXPLICIT author-declared set: positional REFS are resolved
    to their content hashes via the API, plus any --folio-hash / --thread given directly.

    With --site, publishes a first-class named public site. No REFS means every current
    non-site folio head in that local workbench site; REFS/--folio-hash narrow it to an
    exact subset. The server adds the stable type=site anchor, within memberships, and
    slug claim. --dry-run shows those exact identities without writing, signing, or
    sending. This remains a THIN wrapper: all assembly and persistence is server-side.

    Identity for a real send comes from either --login (runs the interactive Sigstore
    ceremony here and hands the token to the route) or --token (a token from a prior
    login). The ceremony is the ONLY client-side step; the server still does the signing.
    """
    base_url = get_base_url(ctx.obj.get("url"))
    agent_id = ctx.obj.get("agent")

    if site_slug and not site_id:
        raise click.ClickException("--slug requires --site")

    # Identity resolution (client-side ceremony only; the route does the signing).
    if login and token:
        raise click.ClickException("use one of --login or --token, not both")
    if force_oob and not login:
        raise click.ClickException("--oob only applies with --login")
    if dry_run and login:
        # A dry run signs and sends nothing, so a login ceremony would pop a browser
        # for no reason — skip it (the resolved set + warnings need no identity).
        click.echo("dry-run: skipping login (nothing is signed or sent)", err=True)
        login = False
    if login:
        from skein import publish as _pub  # lazy: keeps sigstore off every other CLI path

        try:
            ident = _pub.acquire_login_token(force_oob=force_oob)
        except Exception as e:  # a cancelled/failed OIDC ceremony -> clean error, not a traceback
            raise click.ClickException(f"Sigstore login failed: {e}")
        click.echo(f"signed in as {ident['subject']} (via {ident['issuer']})", err=True)
        token = ident["token"]
    elif not dry_run and not token:
        raise click.ClickException(
            "a real publish needs an identity: pass --login (interactive) or --token TOKEN "
            "(from a prior login). Use --dry-run to preview without signing."
        )

    folios = list(folio_hashes)
    for ref in refs:
        folio = make_folio_request("GET", ref, base_url, agent_id)
        h = folio.get("content_hash")
        if not h:
            raise click.ClickException(f"folio '{ref}' has no content_hash to publish")
        folios.append(h)

    body = {
        "to": instance_url,
        "manifest": {"folios": folios, "threads": list(thread_hashes)},
        "site": site_id,
        "site_slug": site_slug,
        "token": token,
        "dry_run": dry_run,
    }
    result = make_request("POST", "/publish", base_url, agent_id, json=body)

    if output_json:
        click.echo(json.dumps(result, indent=2))
        return
    declared = result.get("declared", {})
    click.echo(
        f"declared: {len(declared.get('folios', []))} folio(s), "
        f"{len(declared.get('threads', []))} thread(s)"
    )
    for site_hash, slug in declared.get("site_slugs", {}).items():
        click.echo(f"site claim: /site/{slug} -> {site_hash}")
    for w in result.get("warnings", []):
        click.echo(f"  warn [{w.get('code')}] {w.get('subject', '')}: {w.get('message')}", err=True)
    if result.get("sent"):
        click.echo(f"published to {instance_url}")
    elif dry_run:
        click.echo("dry-run — nothing signed or sent")


# Station re-home Stage 6: the public-station group (launchers + operator ops),
# attached the same way skein_next attached its shard/roster groups. The group is
# direct-store / server-launching by design (see skein/station_cli.py docstring),
# unlike the rest of this CLI, which is a thin client over the 8001 API.
from skein.station_cli import station as _station_group  # noqa: E402

cli.add_command(_station_group)


def main():
    """Entry point for the skein CLI (called by pip-installed command)."""
    cli()


if __name__ == "__main__":
    main()
