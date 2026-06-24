"""Stage 3 — intranet + handoff: reply/replies, mantle resolution, ignite trail.

Exercises the Station layer directly (the CLI is covered in test_intranet_cli.py).
These are the PULL-side primitives: a reply readable in a resource's thread-tree,
a mantle role folio resolvable by id or name, and the brief-handoff edge an
ignite leaves behind. All are thread/folio reads — no new state machine.
"""

from datetime import datetime, timezone

import pytest

from skein.station import (
    AmbiguousReference,
    Station,
    UnknownFolio,
)


@pytest.fixture
def station(tmp_path):
    s = Station(data_dir=tmp_path / ".skein")
    s.create_site("work", purpose="work site", created_by="alice")
    yield s
    s.close()


def _post(station, type, title, content, by="alice"):
    return station.post(type=type, site="work", title=title, content=content, created_by=by)


# --- reply / replies ---------------------------------------------------------


def test_reply_posts_thread_readable_on_resource(station):
    issue = _post(station, "issue", "bug", "something is broken")
    station.reply(issue, "I'll take this", by="nub-0622")

    replies = station.replies(issue)
    assert len(replies) == 1
    assert replies[0]["author"] == "nub-0622"
    assert replies[0]["message"] == "I'll take this"


def test_reply_to_unknown_resource_raises(station):
    with pytest.raises(UnknownFolio):
        station.reply("sha256::" + "0" * 64, "into the void", by="nub-0622")


def test_reply_requires_an_author(station):
    issue = _post(station, "issue", "bug", "broken")
    with pytest.raises(ValueError):
        station.reply(issue, "no author", by="")


def test_reply_by_agent_id_resolves_when_name_differs(station):
    # agent-id and name are decoupled (register --name X --agent Y). A reply
    # authored by the agent-id still resolves from_id to that agent's folio.
    issue = _post(station, "issue", "bug", "broken")
    agent_hash = station.register_agent(agent_id="agent-y", name="handle-x")
    station.reply(issue, "by id", by="agent-y")
    graph = station.thread_graph(issue)
    edge = [e for e in graph["incoming"] if e["type"] == "reply"][0]
    assert edge["from_id"] == agent_hash
    assert edge["peer"]["folio"]["type"] == "agent"


def test_replies_on_unknown_resource_raises(station):
    with pytest.raises(UnknownFolio):
        station.replies("sha256::" + "0" * 64)


def test_replies_ordered_oldest_first(station):
    issue = _post(station, "issue", "bug", "broken")
    t0 = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 6, 22, 11, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    station.reply(issue, "second", by="b", created_at=t1)
    station.reply(issue, "first", by="a", created_at=t0)
    station.reply(issue, "third", by="c", created_at=t2)

    assert [r["message"] for r in station.replies(issue)] == ["first", "second", "third"]


def test_reply_from_registered_agent_resolves_peer_to_agent_folio(station):
    issue = _post(station, "issue", "bug", "broken")
    agent_hash = station.register_agent(agent_id="nub-0622", name="nub-0622")
    station.reply(issue, "mine", by="nub-0622")

    # The reply's from_id is the agent folio hash, so a thread walk over the
    # resource resolves the peer to the agent (who replied), not an opaque string.
    graph = station.thread_graph(issue)
    reply_edges = [e for e in graph["incoming"] if e["type"] == "reply"]
    assert len(reply_edges) == 1
    assert reply_edges[0]["from_id"] == agent_hash
    assert reply_edges[0]["peer"]["kind"] == "folio"
    assert reply_edges[0]["peer"]["folio"]["type"] == "agent"


def test_reply_from_unregistered_author_keeps_raw_id_and_weaver(station):
    issue = _post(station, "issue", "bug", "broken")
    station.reply(issue, "drive-by", by="ghost")

    replies = station.replies(issue)
    assert replies[0]["author"] == "ghost"  # weaver records the author of record
    assert replies[0]["from_id"] == "ghost"  # unresolved → raw id verbatim


def test_reply_is_idempotent_on_identical_tuple(station):
    issue = _post(station, "issue", "bug", "broken")
    ts = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    h1 = station.reply(issue, "same", by="nub", created_at=ts)
    h2 = station.reply(issue, "same", by="nub", created_at=ts)
    assert h1 == h2
    assert len(station.replies(issue)) == 1


def test_reply_resolves_legacy_alias_resource(station):
    issue = _post(station, "issue", "bug", "broken")
    station.store.set_alias("issue-20260622-aaaa", issue)
    station.reply("issue-20260622-aaaa", "via legacy id", by="nub")
    assert station.replies(issue)[0]["message"] == "via legacy id"


# --- mantle resolution -------------------------------------------------------


def test_resolve_mantle_by_exact_title_case_insensitive(station):
    m = _post(station, "mantle", "Quartermaster", "you triage shards")
    found = station.resolve_mantle("quartermaster")
    assert found is not None
    assert found["content_hash"] == m


def test_resolve_mantle_by_partial_title(station):
    m = _post(station, "mantle", "quartermaster-rev2", "role body")
    assert station.resolve_mantle("quartermaster")["content_hash"] == m


def test_resolve_mantle_by_hash(station):
    m = _post(station, "mantle", "boffin", "deep architecture")
    assert station.resolve_mantle(m)["content_hash"] == m


def test_resolve_mantle_prefers_exact_over_partial(station):
    _post(station, "mantle", "oracle-of-edges", "partial-ish")
    exact = _post(station, "mantle", "oracle", "the exact one")
    assert station.resolve_mantle("oracle")["content_hash"] == exact


def test_resolve_mantle_none_when_absent(station):
    assert station.resolve_mantle("nonesuch") is None


def test_resolve_mantle_by_alias(station):
    m = _post(station, "mantle", "namer", "naming specialist")
    station.store.set_alias("mantle-20260622-aaaa", m)
    assert station.resolve_mantle("mantle-20260622-aaaa")["content_hash"] == m


def test_resolve_mantle_empty_or_whitespace_returns_none(station):
    # "" is a substring of every title; the guard must not return the first mantle.
    _post(station, "mantle", "boffin", "role body")
    assert station.resolve_mantle("") is None
    assert station.resolve_mantle("   ") is None


def test_resolve_mantle_ambiguous_short_hash_propagates(station):
    # Two folios sharing a hash prefix make a short ref ambiguous; the ambiguity
    # must surface, not be swallowed into a misleading "no mantle found".
    import unittest.mock as mock

    with mock.patch.object(
        station,
        "resolve_ref",
        side_effect=AmbiguousReference("sha256::ab", ["sha256::ab1", "sha256::ab2"]),
    ):
        with pytest.raises(AmbiguousReference):
            station.resolve_mantle("sha256::ab")


def test_resolve_mantle_ignores_same_named_non_mantle(station):
    # A non-mantle folio sharing the name must not masquerade as a role.
    _post(station, "issue", "priestess", "an issue, not a mantle")
    assert station.resolve_mantle("priestess") is None


def test_resolve_mantle_hash_of_non_mantle_is_rejected(station):
    issue = _post(station, "issue", "thing", "not a mantle")
    assert station.resolve_mantle(issue) is None


# --- ignite handoff trail (written atomically inside register_agent) ----------


def test_register_with_ignited_from_threads_edge_readable_both_ends(station):
    brief = _post(station, "brief", "handoff", "do the work")
    agent_hash = station.register_agent(
        agent_id="nub-0622", name="nub-0622", ignited_from=brief
    )

    # Readable from the brief: who picked it up.
    brief_graph = station.thread_graph(brief)
    picked = [e for e in brief_graph["incoming"] if e["type"] == "ignited_from"]
    assert len(picked) == 1
    assert picked[0]["from_id"] == agent_hash

    # Readable from the agent: what it resumed.
    agent_graph = station.thread_graph(agent_hash)
    resumed = [e for e in agent_graph["outgoing"] if e["type"] == "ignited_from"]
    assert len(resumed) == 1
    assert resumed[0]["to_id"] == brief


def test_register_without_ignited_from_threads_no_handoff_edge(station):
    agent_hash = station.register_agent(agent_id="solo-0622", name="solo-0622")
    graph = station.thread_graph(agent_hash)
    assert not [e for e in graph["outgoing"] if e["type"] == "ignited_from"]


def test_register_with_ignited_from_is_idempotent(station):
    # A byte-identical re-register (same created_at) collapses to one agent folio
    # and one handoff edge — the edge rides the registration transaction.
    brief = _post(station, "brief", "handoff", "do the work")
    ts = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    h1 = station.register_agent(
        agent_id="nub-0622", name="nub-0622", ignited_from=brief, created_at=ts
    )
    h2 = station.register_agent(
        agent_id="nub-0622", name="nub-0622", ignited_from=brief, created_at=ts
    )
    assert h1 == h2
    brief_graph = station.thread_graph(brief)
    picked = [e for e in brief_graph["incoming"] if e["type"] == "ignited_from"]
    assert len(picked) == 1


def test_register_from_different_brief_keeps_both_trails(station):
    # Re-igniting the same agent from a DIFFERENT brief mints a new incarnation
    # with its own handoff edge + a succession edge; the prior keeps its own edge.
    b1 = _post(station, "brief", "b1", "first")
    b2 = _post(station, "brief", "b2", "second")
    t1 = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 22, 11, 0, tzinfo=timezone.utc)
    h1 = station.register_agent(agent_id="nub", name="nub", ignited_from=b1, created_at=t1)
    h2 = station.register_agent(agent_id="nub", name="nub", ignited_from=b2, created_at=t2)
    assert h1 != h2

    cur = station.thread_graph(h2)
    assert [e["to_id"] for e in cur["outgoing"] if e["type"] == "ignited_from"] == [b2]
    assert any(e["type"] == "succession" and e["to_id"] == h1 for e in cur["outgoing"])

    prior = station.thread_graph(h1)
    assert [e["to_id"] for e in prior["outgoing"] if e["type"] == "ignited_from"] == [b1]


def test_register_with_unknown_ignited_from_raises_and_rolls_back(station):
    with pytest.raises(UnknownFolio):
        station.register_agent(
            agent_id="ghost", name="ghost", ignited_from="sha256::" + "0" * 64
        )
    # The whole registration rolled back — no orphan agent folio, no name slug.
    assert station.list_agents() == []
    assert station.store.resolve_slug("ghost") is None


def test_reply_ambiguous_resource_propagates(station):
    import unittest.mock as mock

    issue = _post(station, "issue", "bug", "broken")
    with mock.patch.object(
        station,
        "resolve_ref",
        side_effect=AmbiguousReference("sha256::ab", ["sha256::ab1", "sha256::ab2"]),
    ):
        with pytest.raises(AmbiguousReference):
            station.reply(issue, "which one?", by="nub")
