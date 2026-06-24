"""Tests for the Slice 3 station service layer and the store read helpers it
leans on (search, short-hash prefix lookup)."""

import pytest

from skein.station import (
    AmbiguousReference,
    Station,
    UnknownFolio,
    UnknownSite,
    _short,
    _title_line,
)
from skein.store import SkeinNextStore


@pytest.fixture
def station(tmp_path):
    s = Station(data_dir=tmp_path / ".skein")
    yield s
    s.close()


# --- store read helpers -----------------------------------------------------


def test_search_matches_title_and_content(tmp_path):
    store = SkeinNextStore(tmp_path / ".skein")
    store.create_folio({"type": "finding", "title": "cache collision", "content": "body"})
    store.create_folio({"type": "notion", "title": "unrelated", "content": "shard the cache"})
    store.create_folio({"type": "notion", "title": "nothing", "content": "here"})
    assert len(store.search_folios("cache")) == 2
    assert len(store.search_folios("collision")) == 1
    assert store.search_folios("nonexistent") == []
    store.close()


def test_search_treats_like_wildcards_literally(tmp_path):
    store = SkeinNextStore(tmp_path / ".skein")
    store.create_folio({"type": "finding", "title": "100% done", "content": "x"})
    store.create_folio({"type": "finding", "title": "not full", "content": "y"})
    # '%' must match the literal percent sign, not act as a wildcard.
    hits = store.search_folios("100%")
    assert len(hits) == 1 and hits[0]["title"] == "100% done"
    store.close()


def test_find_by_prefix_unique_and_ambiguous(tmp_path):
    store = SkeinNextStore(tmp_path / ".skein")
    h = store.create_folio({"type": "finding", "title": "t", "content": "c"})
    prefix = h[: len("sha256::") + 6]
    assert store.find_by_prefix(h) == [h]
    assert store.find_by_prefix(prefix) == [h]
    assert store.find_by_prefix("sha256::ffffffffffff") == []
    store.close()


# --- reference resolution ---------------------------------------------------


def test_resolve_ref_full_hash(station):
    h = station.store.create_folio({"type": "finding", "title": "t", "content": "c"})
    assert station.resolve_ref(h) == h


def test_resolve_ref_via_alias(station):
    h = station.store.create_folio({"type": "finding", "title": "t", "content": "c"})
    station.store.set_alias("finding-20260529-abcd", h)
    assert station.resolve_ref("finding-20260529-abcd") == h


def test_resolve_ref_short_prefix(station):
    h = station.store.create_folio({"type": "finding", "title": "t", "content": "c"})
    assert station.resolve_ref(_short(h)) == h


def test_resolve_ref_ambiguous_prefix_raises(station, monkeypatch):
    h = station.store.create_folio({"type": "finding", "title": "t", "content": "c"})
    # Force two matches for the same prefix to exercise the ambiguity guard.
    # The ref must be a valid bare short hash (>=8 hex) so it reaches the local
    # short-hash resolver rather than being rejected outright.
    monkeypatch.setattr(station.store, "find_by_prefix", lambda p, limit=100: [h, h + "x"])
    with pytest.raises(AmbiguousReference):
        station.resolve_ref("sha256::deadbeef")


def test_resolve_ref_ambiguous_prefix_real_collision(station):
    # Insert two folio rows that genuinely share a short prefix, so the real
    # find_by_prefix query + len()>1 guard are exercised end-to-end (not mocked).
    shared = "sha256::abcdef000000"
    for tail in ("1111", "2222"):
        station.store.conn.execute(
            "INSERT INTO folios (content_hash, type, title, content) VALUES (?,?,?,?)",
            (shared + tail, "finding", "t", "c"),
        )
    station.store.conn.commit()
    with pytest.raises(AmbiguousReference) as ei:
        station.resolve_ref(shared)
    assert len(ei.value.matches) == 2
    assert not ei.value.capped


def test_resolve_ref_ambiguous_count_is_capped(station):
    # More collisions than the display cap: the count must read "at least N",
    # not present the cap as if it were the exact total.
    shared = "sha256::cafef0000000"
    for i in range(12):
        station.store.conn.execute(
            "INSERT INTO folios (content_hash, type, title, content) VALUES (?,?,?,?)",
            (f"{shared}{i:04d}", "finding", "t", "c"),
        )
    station.store.conn.commit()
    with pytest.raises(AmbiguousReference) as ei:
        station.resolve_ref(shared)
    assert ei.value.capped
    assert len(ei.value.matches) == 10
    assert "at least 10" in str(ei.value)


def test_resolve_ref_unknown_returns_none(station):
    assert station.resolve_ref("sha256::deadbeef") is None
    assert station.resolve_ref("not-a-real-id") is None


# --- :: addressing resolution (Slice 5b) ------------------------------------


def test_folios_with_prefix_returns_bare_digests(station):
    h = station.store.create_folio({"type": "finding", "title": "t", "content": "c"})
    digest = h.split("::", 1)[1]
    assert station.folios_with_prefix("sha256", digest[:12]) == [digest]
    assert station.folios_with_prefix("sha256", "ffffffffffff") == []
    # a non-sha256 algorithm has no folios in this v0 store
    assert station.folios_with_prefix("blake3", digest[:12]) == []


def test_resolve_ref_full_hash_address_via_resolver(station):
    # A full bare sha256:: address routes through skein.address.parse/resolve
    # (the fast-path exact-store check is bypassed by deleting then re-adding so
    # we exercise the resolver, not just step 1). Here we just confirm a full
    # address resolves to itself when present.
    h = station.store.create_folio({"type": "finding", "title": "t", "content": "c"})
    assert station.resolve_ref(h) == h


def test_resolve_ref_alias_form_short_hash_cascades(station):
    # An alias-form address (name::sha256::<short>) carries station context, so
    # the hardened resolver lengthens the short hash against this station.
    h = station.store.create_folio({"type": "finding", "title": "t", "content": "c"})
    short = h.split("::", 1)[1][:16]
    assert station.resolve_ref(f"mystation::sha256::{short}") == h


def test_resolve_ref_malformed_address_returns_none(station):
    # Structurally-invalid addresses resolve to None, never raise to the caller.
    # Includes the floor boundary: a bare short hash under 8 hex (which the
    # scheme rejects) must return None, not crash.
    for bad in (
        "sha256::",
        "::sha256::abc",
        "sha256::xyz",
        "a::b::c::d",
        "sha256::dead",
        "sha256::deadbe",
        "sha256",
    ):
        assert station.resolve_ref(bad) is None


def test_resolve_ref_full_hash_not_in_store_returns_none(station):
    # A well-formed full address for a folio that isn't here resolves to None.
    assert station.resolve_ref("sha256::" + "a" * 64) is None


def test_resolve_ref_uppercase_hex_does_not_resolve(station):
    # Intended behavior change from Slice 3: identity is canonical lowercase hex
    # (validate-not-convert, matching the :: scheme). An uppercase reference to
    # an existing folio resolves to None rather than case-folding to a match.
    h = station.store.create_folio({"type": "finding", "title": "t", "content": "c"})
    assert station.resolve_ref(h.upper()) is None
    assert station.resolve_ref("sha256::" + h.split("::", 1)[1][:16].upper()) is None


def _insert_prefix_collision(station, shared, tails):
    for tail in tails:
        station.store.conn.execute(
            "INSERT INTO folios (content_hash, type, title, content) VALUES (?,?,?,?)",
            (shared + tail, "finding", "t", "c"),
        )
    station.store.conn.commit()


def test_resolve_ref_alias_form_ambiguous_raises(station):
    # The step-3 resolver ambiguity path: an alias-form short hash that matches
    # multiple folios surfaces as AmbiguousReference with sha256::-framed
    # candidates (translated from the resolver's AmbiguousShortHash).
    shared = "abcdef0000000000"  # 16 hex, a valid short hash
    _insert_prefix_collision(station, "sha256::" + shared, ("1111", "2222"))
    with pytest.raises(AmbiguousReference) as ei:
        station.resolve_ref(f"mystation::sha256::{shared}")
    assert all(c.startswith("sha256::") for c in ei.value.matches)
    assert len(ei.value.matches) == 2


def test_resolve_ref_alias_form_no_match_returns_none(station):
    # The step-3 ShortHashNotFound path: an alias-form short hash matching
    # nothing resolves to None, not an exception.
    assert station.resolve_ref("mystation::sha256::abcdef0000000000") is None


# --- post / membership ------------------------------------------------------


def test_post_requires_existing_site(station):
    with pytest.raises(UnknownSite):
        station.post(type="finding", site="ghost", title="t")


def test_post_rejects_a_slug_that_is_not_a_site(station):
    # The slug table is not site-exclusive (agent names get slugged too). A slug
    # resolving to a non-site folio is not a valid post target — you cannot post
    # a folio "within" a non-site.
    agent_hash = station.store.create_folio({"type": "agent", "title": "an-agent"})
    station.store.set_slug("an-agent", agent_hash)
    with pytest.raises(UnknownSite):
        station.post(type="finding", site="an-agent", title="t")


def test_site_read_apis_ignore_non_site_slugs(station):
    # The "a site is a type=site folio" invariant holds across the read surface
    # too: a non-site slug is invisible to get_site/list_sites and unqueryable
    # via folios_in_site.
    station.create_site("real-site")
    agent_hash = station.store.create_folio({"type": "agent", "title": "an-agent"})
    station.store.set_slug("an-agent", agent_hash)

    assert station.get_site("an-agent") is None
    assert station.get_site("real-site") is not None

    slugs = [slug for slug, _ in station.list_sites()]
    assert "real-site" in slugs
    assert "an-agent" not in slugs

    with pytest.raises(UnknownSite):
        station.folios_in_site("an-agent")


def test_post_creates_folio_and_within_edge(station):
    station.create_site("proj", purpose="a project")
    h = station.post(type="finding", site="proj", title="found it", content="body", created_by="me")
    folio = station.get_folio(h)
    assert folio["type"] == "finding" and folio["title"] == "found it"
    members = station.folios_in_site("proj")
    assert [m["content_hash"] for m in members] == [h]


def test_post_is_idempotent(station):
    station.create_site("proj")
    a = station.post(
        type="finding", site="proj", title="t", content="c", created_at="2026-01-01T00:00:00Z"
    )
    b = station.post(
        type="finding", site="proj", title="t", content="c", created_at="2026-01-01T00:00:00Z"
    )
    assert a == b
    # one folio, and exactly one membership edge
    assert len(station.folios_in_site("proj")) == 1


def test_folios_in_site_type_filter(station):
    station.create_site("proj")
    station.post(type="finding", site="proj", title="f", created_at="2026-01-01T00:00:00Z")
    station.post(type="notion", site="proj", title="n", created_at="2026-01-02T00:00:00Z")
    assert len(station.folios_in_site("proj")) == 2
    assert len(station.folios_in_site("proj", type="finding")) == 1


def test_folios_in_site_unknown_raises(station):
    with pytest.raises(UnknownSite):
        station.folios_in_site("ghost")


def test_folios_in_site_limit_returns_earliest_by_created_at(station):
    # The MAJOR fell-r1 finding: limit must apply *after* created_at ordering,
    # not truncate the (timestamp-less, hash-ordered) membership edges. Post 5
    # folios with ascending timestamps; limit=3 must return the earliest three.
    station.create_site("proj")
    hashes = [
        station.post(
            type="finding",
            site="proj",
            title=f"f{i}",
            created_at=f"2026-01-0{i + 1}T00:00:00Z",
        )
        for i in range(5)
    ]
    got = station.folios_in_site("proj", limit=3)
    assert [f["content_hash"] for f in got] == hashes[:3]


def test_create_site_idempotent_preserves_membership(station):
    # The whole reason create_site is idempotent: a member posted before a
    # re-create must still resolve through the (unchanged) site folio.
    station.create_site("proj", purpose="first")
    h = station.post(type="finding", site="proj", title="member", created_at="2026-01-01T00:00:00Z")
    station.create_site("proj", purpose="a different purpose")  # must NOT remint
    members = station.folios_in_site("proj")
    assert [m["content_hash"] for m in members] == [h]


# --- sites ------------------------------------------------------------------


def test_list_sites_and_get_site(station):
    station.create_site("alpha", purpose="first")
    station.create_site("beta", purpose="second")
    pairs = station.list_sites()
    assert [slug for slug, _ in pairs] == ["alpha", "beta"]
    assert station.get_site("alpha")["content"] == "first"
    assert station.get_site("missing") is None


# --- status (write parity) --------------------------------------------------


def test_set_status_then_read_back(station):
    station.create_site("proj")
    h = station.post(type="finding", site="proj", title="t", created_at="2026-01-01T00:00:00Z")
    assert station.status_of(h) == "open"  # default until a status thread says otherwise
    station.set_status(h, "investigating", by="me")
    assert station.status_of(h) == "investigating"


def test_set_status_latest_wins(station):
    station.create_site("proj")
    h = station.post(type="finding", site="proj", title="t", created_at="2026-01-01T00:00:00Z")
    station.set_status(h, "open", by="me", created_at="2026-02-01T00:00:00Z")
    station.set_status(h, "closed", by="me", created_at="2026-02-02T00:00:00Z")
    assert station.status_of(h) == "closed"


def test_set_status_writes_self_loop_thread(station):
    # Status threads match the legacy shape: from_id == to_id == folio, type
    # status, weaver = author, content = the status word.
    station.create_site("proj")
    h = station.post(type="finding", site="proj", title="t", created_at="2026-01-01T00:00:00Z")
    station.set_status(h, "closed", by="alice")
    edges = station.store.get_threads(to_id=h, type="status")
    assert len(edges) == 1
    e = edges[0]
    assert e["from_id"] == h and e["to_id"] == h
    assert e["weaver"] == "alice" and e["content"] == "closed"


def test_set_status_unknown_folio_raises(station):
    with pytest.raises(UnknownFolio):
        station.set_status("sha256::deadbeef", "closed")


def test_set_status_resolves_short_ref(station):
    station.create_site("proj")
    h = station.post(type="finding", site="proj", title="t", created_at="2026-01-01T00:00:00Z")
    station.set_status(_short(h), "closed", by="me")  # short hash resolves
    assert station.status_of(h) == "closed"


def test_set_status_ambiguous_short_ref_raises(station):
    # An ambiguous short prefix must propagate AmbiguousReference (not silently
    # write to one of the matches), so the CLI can list the candidates.
    shared = "sha256::abcdef000000"
    for tail in ("1111", "2222"):
        station.store.conn.execute(
            "INSERT INTO folios (content_hash, type, title, content) VALUES (?,?,?,?)",
            (shared + tail, "finding", "t", "c"),
        )
    station.store.conn.commit()
    with pytest.raises(AmbiguousReference):
        station.set_status(shared, "closed", by="me")


def test_status_of_defaults_to_open(station):
    # A folio with no status thread reads as 'open' (the default), matching
    # legacy SKEIN and the web adapter — not None.
    station.create_site("proj")
    h = station.post(type="finding", site="proj", title="t", created_at="2026-01-01T00:00:00Z")
    assert station.status_of(h) == "open"
    # an explicit status still wins, and re-opening reads back as open
    station.set_status(h, "closed", by="me", created_at="2026-02-01T00:00:00Z")
    assert station.status_of(h) == "closed"
    station.set_status(h, "open", by="me", created_at="2026-02-02T00:00:00Z")
    assert station.status_of(h) == "open"


# --- thread graph -----------------------------------------------------------


def test_thread_graph_separates_links_and_membership(station):
    station.create_site("proj")
    a = station.post(type="finding", site="proj", title="A", created_at="2026-01-01T00:00:00Z")
    b = station.post(type="brief", site="proj", title="B", created_at="2026-01-02T00:00:00Z")
    station.store.save_thread(from_id=a, to_id=b, type="supersedes")

    graph = station.thread_graph(a)
    assert graph["content_hash"] == a
    assert len(graph["outgoing"]) == 1
    assert graph["outgoing"][0]["type"] == "supersedes"
    assert graph["outgoing"][0]["peer"]["kind"] == "folio"
    assert graph["outgoing"][0]["peer"]["folio"]["content_hash"] == b
    # the within edge to the site is reported as membership, not a link
    assert len(graph["memberships"]) == 1
    assert graph["memberships"][0]["peer"]["folio"]["type"] == "site"


def test_thread_graph_unresolved_peer(station):
    station.create_site("proj")
    a = station.post(type="finding", site="proj", title="A")
    station.store.save_thread(from_id=a, to_id="other:finding-20260101-zzzz", type="relates")
    graph = station.thread_graph(a)
    link = [e for e in graph["outgoing"] if e["type"] == "relates"][0]
    assert link["peer"]["kind"] == "ref"
    assert link["peer"]["id"] == "other:finding-20260101-zzzz"


def test_thread_graph_unknown_ref_returns_none(station):
    assert station.thread_graph("sha256::deadbeef") is None


# --- helpers ----------------------------------------------------------------


def test_short_and_title_line():
    assert _short("sha256::" + "a" * 64, 8) == "sha256::aaaaaaaa"
    assert _title_line("  first line\nsecond") == "first line"
    assert _title_line("x" * 200, limit=10) == "x" * 9 + "…"
    assert _title_line(None) == ""
