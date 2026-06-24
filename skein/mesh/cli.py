"""The ``mesh`` CLI — the client-side content-hash mesh entrypoint.

Distinct from the legacy ``skein`` CLI (which speaks to the old human-id server):
``mesh`` talks to a content-hash station over the HTTP wire. ``mesh fetch`` is
the headline verb — resolve an address, strict-verify it locally, print the
agent-markdown rendering, and exit with a fork-F code so it composes in scripts.
"""

from __future__ import annotations

import json
import sys

import click

from .client import (
    DEFAULT_INSTANCE,
    describe_display,
    fetch,
    list_display,
    resolve_display,
    search_display,
    verdict_line,
)

_INSTANCE = click.option(
    "--from", "instance", default=DEFAULT_INSTANCE, envvar="MESH_INSTANCE",
    help=f"Instance URL (default {DEFAULT_INSTANCE}; or $MESH_INSTANCE).",
)


@click.group()
@click.version_option(package_name="interskein")
def cli() -> None:
    """Read the content-hash mesh over the HTTP wire."""


@cli.command(name="fetch")
@click.argument("address")
@click.option(
    "--from", "instance", default=DEFAULT_INSTANCE, envvar="MESH_INSTANCE",
    help=f"Instance URL to resolve against (default {DEFAULT_INSTANCE}; or $MESH_INSTANCE).",
)
@click.option(
    "--require-signed", is_flag=True,
    help="Exit non-zero if the resolved folio is unsigned (integrity-only).",
)
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON envelope instead of markdown.")
@click.option("--quiet", "-q", is_flag=True, help="Print only the verdict (stderr); no body.")
@click.option("--timeout", default=10.0, type=float, help="HTTP timeout in seconds.")
def fetch_cmd(address, instance, require_signed, as_json, quiet, timeout):
    """Resolve ADDRESS, verify it locally, and print it.

    Exit codes (fork F): 0 verified or unsigned; 2 not resolved; 3 signature
    invalid (or body/address mismatch); 4 verifier unavailable; 5 unsigned under
    --require-signed.
    """
    result = fetch(instance, address, require_signed=require_signed, timeout=timeout)

    if result.warning:
        click.echo(result.warning, err=True)
    click.echo(verdict_line(result), err=True)

    if as_json:
        click.echo(json.dumps(result.envelope, ensure_ascii=False, indent=2))
    elif not quiet and result.markdown:
        click.echo(result.markdown)

    sys.exit(result.exit_code)


# The browse verbs are the display-trust path: they print what the station
# rendered, unverified. `mesh fetch` is the verb that verifies. These three plus
# `resolve` are what the MCP wrapper exposes 1:1 (`mesh mcp`).


@cli.command(name="resolve")
@click.argument("address")
@_INSTANCE
def resolve_cmd(address, instance):
    """Resolve ADDRESS to its rendering (display only; `mesh fetch` to verify)."""
    click.echo(resolve_display(instance, address))


@cli.command(name="search")
@click.argument("query")
@_INSTANCE
def search_cmd(query, instance):
    """Search the station's folios (display; `mesh fetch` a result to verify it)."""
    click.echo(search_display(instance, query))


@cli.command(name="list")
@click.argument("slug")
@_INSTANCE
def list_cmd(slug, instance):
    """List a site's folios by SLUG (display; `mesh fetch` a folio to verify it)."""
    click.echo(list_display(instance, slug))


@cli.command(name="describe")
@_INSTANCE
def describe_cmd(instance):
    """Describe the station — the well-known root (display, unverified metadata)."""
    click.echo(describe_display(instance))


@cli.command(name="mcp")
@_INSTANCE
def mcp_cmd(instance):
    """Run the client-side MCP server (stdio), wrapping resolve/search/list/describe.

    Needs the optional MCP dependency: pip install interskein[mesh-mcp].
    """
    from .mcp import run as run_mcp

    try:
        run_mcp(instance)  # FastMCP is imported lazily inside; absent extra -> ImportError
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise click.ClickException(
            "the MCP server needs the optional 'mcp' package: pip install interskein[mesh-mcp]"
        ) from e


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
