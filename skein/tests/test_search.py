"""Tests for search L1 (brief-20260522-4yt4 Layer 1): AND-of-terms matching,
title-over-body relevance ranking, and match snippets."""

import pytest

from skein.station import Station
from skein.store import SkeinNextStore, make_snippet


@pytest.fixture
def store(tmp_path):
    data_dir = tmp_path / ".skein"
    with Station(data_dir) as st:
        st.create_site("proj", purpose="p")
        # title-only "needle"
        st.post(type="finding", site="proj", title="needle in the title",
                content="ordinary body text", created_by="a", created_at="2026-01-01T00:00:00Z")
        # body-only "needle"
        st.post(type="finding", site="proj", title="ordinary title",
                content="the needle is in the body", created_by="a", created_at="2026-01-02T00:00:00Z")
        # both terms for AND test
        st.post(type="brief", site="proj", title="alpha heading",
                content="discusses beta thoroughly", created_by="a", created_at="2026-01-03T00:00:00Z")
        # literal percent
        st.post(type="note", site="proj", title="discount 50% off",
                content="sale", created_by="a", created_at="2026-01-04T00:00:00Z")
    s = SkeinNextStore(data_dir, read_only=True)
    yield s
    s.close()


def test_empty_query_returns_nothing(store):
    assert store.search_folios("") == []
    assert store.search_folios("   ") == []


def test_and_of_terms(store):
    # both terms present (one in title, one in body) -> match
    hits = store.search_folios("alpha beta")
    assert any("alpha heading" in (r.get("title") or "") for r in hits)
    # one term absent -> no match (AND, not OR, not exact-phrase)
    assert store.search_folios("alpha gamma") == []


def test_title_ranks_above_body(store):
    hits = store.search_folios("needle")
    titles = [r["title"] for r in hits]
    assert "needle in the title" in titles and "ordinary title" in titles
    # the title hit outranks the body-only hit
    assert titles.index("needle in the title") < titles.index("ordinary title")


def test_like_wildcards_are_literal(store):
    hits = store.search_folios("50%")
    assert any("discount 50% off" in (r.get("title") or "") for r in hits)


def test_make_snippet_centers_on_match():
    content = "x" * 300 + " THE-NEEDLE " + "y" * 300
    snip = make_snippet(content, ["needle"], width=80)
    assert "THE-NEEDLE" in snip
    assert snip.startswith("…") and snip.endswith("…")  # elided both ends


def test_make_snippet_no_match_takes_head():
    snip = make_snippet("short body with no match", ["zzz"], width=80)
    assert snip == "short body with no match"


def test_make_snippet_empty_is_none():
    assert make_snippet("", ["x"]) is None
    assert make_snippet(None, ["x"]) is None


def test_term_cap_drops_terms_beyond_limit(tmp_path):
    # Prove the cap is actually applied (not just non-crashing): a folio holds
    # exactly _MAX_SEARCH_TERMS distinct terms; a query of those PLUS one absent
    # term still matches — because the cap drops the (absent) overflow term before
    # it can be required. Without the cap, the absent term would be ANDed in and
    # the folio would miss.
    from skein.store import _MAX_SEARCH_TERMS

    present = [f"alpha{i}" for i in range(_MAX_SEARCH_TERMS)]
    data_dir = tmp_path / ".skein"
    with Station(data_dir) as st:
        st.create_site("p", purpose="p")
        st.post(type="finding", site="p", title="capped folio", content=" ".join(present),
                created_by="a", created_at="2026-01-01T00:00:00Z")
    s = SkeinNextStore(data_dir, read_only=True)
    try:
        # control: the overflow term genuinely matches nothing on its own.
        assert s.search_folios("absentzzz") == []
        # the 32 present terms + the overflow term: the overflow is dropped by the
        # cap, so all required terms are present -> match.
        hits = s.search_folios(" ".join(present + ["absentzzz"]))
        assert any(h["title"] == "capped folio" for h in hits)
    finally:
        s.close()
