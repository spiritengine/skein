"""The client-side MCP wrapper over the HTTP wire (brief-20260603-dirz fork E).

MCP is a client-side ``mesh`` wrapper, NOT server-hosted: instances are HTTP-only,
and their routes ARE the operations. This server maps four tools 1:1 onto the
routes — ``resolve`` = GET /folio, ``search`` = GET /search, ``list`` = GET
/site/{slug}, ``describe`` = the well-known root — and can point at any instance,
so one client reaches the whole mesh. No instance runs its own MCP server.

It is the display-trust convenience path: each tool returns the station's
agent-markdown rendering (which carries addresses + the bundle link), so an agent
can escalate to ``mesh fetch`` (resolve + strict local verify) when it needs a
hard guarantee. Verification and federation never route through MCP.

The ``mcp`` package is an optional dependency (``pip install interskein[mesh-mcp]``);
it is imported lazily here so ``mesh fetch`` and the browse verbs work without it.
"""

from __future__ import annotations

from .client import (
    DEFAULT_INSTANCE,
    describe_display,
    list_display,
    resolve_display,
    search_display,
)


def build_server(instance: str = DEFAULT_INSTANCE):
    """Build a FastMCP server whose four tools wrap ``instance``'s routes."""
    from mcp.server.fastmcp import FastMCP  # lazy: optional dep

    server = FastMCP("skein-mesh")

    @server.tool()
    def resolve(address: str) -> str:
        """Resolve a SKEIN address to its agent-markdown rendering.

        Display-trust: this is what the station rendered, not locally verified.
        For a hard guarantee (signature + content-address binding), use
        ``mesh fetch <address>`` over the same address.
        """
        return resolve_display(instance, address)

    @server.tool()
    def search(query: str) -> str:
        """Search the station's folios; ranked results as agent markdown.

        Display-trust: `mesh fetch` an individual result to verify it.
        """
        return search_display(instance, query)

    @server.tool(name="list")
    def list_site(slug: str) -> str:
        """List a site's folios (by slug) as agent markdown.

        Display-trust: `mesh fetch` an individual folio to verify it.
        """
        return list_display(instance, slug)

    @server.tool()
    def describe() -> str:
        """Describe the station: name, wire/profile, operations, and the fence rule.

        Display-trust: unverified station metadata.
        """
        return describe_display(instance)

    return server


def run(instance: str = DEFAULT_INSTANCE) -> None:
    """Run the MCP server over stdio, pointed at ``instance``."""
    build_server(instance).run()
