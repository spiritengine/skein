"""Tests for the mesh browse verbs (resolve/search/list/describe — the
display-trust path) and the client-side MCP wrapper that maps four tools 1:1 onto
the HTTP routes.

The HTTP wire is backed by a TestClient of the station via a `requests.get` shim
that forwards params + headers (the display helpers send Accept: text/markdown).
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from skein.mesh import client as mesh_client
from skein.mesh.client import (
    describe_display,
    list_display,
    resolve_display,
    search_display,
)
from skein.station import Station
from skein.web.app import ENV_DATA_DIR, ENV_PROJECT, create_app

LOCAL = "http://127.0.0.1:9001"


@pytest.fixture
def seeded(tmp_path):
    data_dir = tmp_path / ".skein"
    with Station(data_dir) as st:
        st.create_site("specs", purpose="the spec site")
        a = st.post(type="finding", site="specs", title="Resolver notes",
                    content="resolver maps an address to a hash", created_by="alice",
                    created_at="2026-01-01T00:00:00Z")
    return {"data_dir": data_dir, "a": a}


@pytest.fixture
def wired(seeded, monkeypatch):
    monkeypatch.setenv(ENV_DATA_DIR, str(seeded["data_dir"]))
    monkeypatch.setenv(ENV_PROJECT, "Field Notes")
    station = TestClient(create_app())

    def fake_get(url, params=None, headers=None, timeout=None):
        from urllib.parse import urlparse

        path = urlparse(url).path
        return station.get(path, params=params, headers=headers)

    monkeypatch.setattr(mesh_client.requests, "get", fake_get)
    return seeded


# --- browse verbs (display helpers) -----------------------------------------


def test_resolve_display_returns_markdown(wired):
    text = resolve_display(LOCAL, wired["a"])
    assert "resolver maps an address" in text
    assert "Provenance:" in text  # the agent-markdown control frame


def test_search_display(wired):
    text = search_display(LOCAL, "resolver address")
    assert "Resolver notes" in text  # AND-of-terms matched the one folio


def test_list_display(wired):
    text = list_display(LOCAL, "specs")
    assert "Resolver notes" in text


def test_describe_display(wired):
    text = describe_display(LOCAL)
    assert "SKEIN station" in text and "Operations:" in text


def test_display_unreachable_returns_error_line(seeded, monkeypatch):
    import requests

    def boom(url, params=None, headers=None, timeout=None):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(mesh_client.requests, "get", boom)
    assert resolve_display(LOCAL, seeded["a"]).startswith("error: instance unreachable")


# --- CLI verbs --------------------------------------------------------------


def test_cli_browse_verbs(wired):
    from click.testing import CliRunner

    from skein.mesh.cli import cli

    runner = CliRunner()
    assert "Resolver notes" in runner.invoke(cli, ["search", "resolver", "--from", LOCAL]).output
    assert "Resolver notes" in runner.invoke(cli, ["list", "specs", "--from", LOCAL]).output
    assert "SKEIN station" in runner.invoke(cli, ["describe", "--from", LOCAL]).output
    assert "resolver maps" in runner.invoke(cli, ["resolve", wired["a"], "--from", LOCAL]).output


# --- MCP wrapper ------------------------------------------------------------


def test_mcp_server_exposes_four_tools():
    pytest.importorskip("mcp.server.fastmcp")  # the optional mesh-mcp extra
    from skein.mesh.mcp import build_server

    server = build_server(LOCAL)
    tools = asyncio.get_event_loop().run_until_complete(server.list_tools())
    names = {t.name for t in tools}
    assert names == {"resolve", "search", "list", "describe"}


def test_mcp_tools_call_the_routes(wired):
    # The tool callables delegate to the display helpers over the (shimmed) wire.
    pytest.importorskip("mcp.server.fastmcp")  # the optional mesh-mcp extra
    from skein.mesh.mcp import build_server

    server = build_server(LOCAL)
    out = asyncio.get_event_loop().run_until_complete(
        server.call_tool("describe", {})
    )
    # call_tool returns (content_blocks, raw) in this MCP version; assert the text
    # surfaces either way.
    text = str(out)
    assert "SKEIN station" in text
