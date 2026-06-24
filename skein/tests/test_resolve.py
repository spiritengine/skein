"""Tests for the resolve verb (skein.resolve): address -> content hash.

A ``FakeStore`` drives the short-hash / alias / ambiguity branches with full
control over what the index returns (real content hashes can't be steered to a
chosen prefix), backed by a couple of real-store checks that the StationIndex
adapter actually queries the store.
"""

from __future__ import annotations

import pytest

from skein.resolve import ResolveError, resolve_to_hash, _StoreStationIndex
from skein.store import SkeinNextStore

FULL_A = "a" * 64
FULL_B = "b" * 64


class FakeStore:
    """Just the two methods resolve_to_hash touches."""

    def __init__(self, prefix_matches=None, aliases=None):
        self._prefix_matches = prefix_matches or {}  # "sha256::<prefix>" -> [full addrs]
        self._aliases = aliases or {}

    def find_by_prefix(self, prefix, limit=10):
        return self._prefix_matches.get(prefix, [])[:limit]

    def resolve_alias(self, legacy_id):
        return self._aliases.get(legacy_id)


def test_full_bare_hash_passes_through():
    assert resolve_to_hash(f"sha256::{FULL_A}", FakeStore()) == f"sha256::{FULL_A}"


def test_full_hash_does_not_require_existence():
    # resolve is pure address math: a well-formed full hash resolves to itself
    # without touching the store (existence is the caller's get_folio gate).
    assert resolve_to_hash(f"sha256::{FULL_A}", FakeStore()) == f"sha256::{FULL_A}"


def test_alias_address_resolves_digest_locally():
    addr = f"interskein::sha256::{FULL_A}"
    assert resolve_to_hash(addr, FakeStore()) == f"sha256::{FULL_A}"


def test_short_hash_lengthened_via_index():
    store = FakeStore(prefix_matches={"sha256::abcd1234": [f"sha256::{FULL_A}"]})
    assert resolve_to_hash("interskein::sha256::abcd1234", store) == f"sha256::{FULL_A}"


def test_short_hash_ambiguous():
    store = FakeStore(
        prefix_matches={"sha256::abcd1234": [f"sha256::{FULL_A}", f"sha256::{FULL_B}"]}
    )
    with pytest.raises(ResolveError) as e:
        resolve_to_hash("interskein::sha256::abcd1234", store)
    assert e.value.code == "ambiguous_short_hash"


def test_short_hash_not_found():
    with pytest.raises(ResolveError) as e:
        resolve_to_hash("interskein::sha256::abcd1234", FakeStore())
    assert e.value.code == "not_found"


def test_web_short_hash_unsupported():
    with pytest.raises(ResolveError) as e:
        resolve_to_hash("web::interskein.com::sha256::abcd1234", FakeStore())
    assert e.value.code == "short_hash_unsupported_remote"


def test_web_foreign_authority_is_not_found_with_origin():
    addr = f"web::other.example::sha256::{FULL_A}"
    with pytest.raises(ResolveError) as e:
        resolve_to_hash(addr, FakeStore(), local_authority="interskein.com")
    assert e.value.code == "not_found"
    assert e.value.origin == addr


def test_web_own_authority_resolves_locally():
    addr = f"web::interskein.com::sha256::{FULL_A}"
    assert (
        resolve_to_hash(addr, FakeStore(), local_authority="interskein.com") == f"sha256::{FULL_A}"
    )


def test_web_authority_resolves_when_no_local_authority_configured():
    # Phase 1 with no authority set: a web:: address still resolves by its digest.
    addr = f"web::interskein.com::sha256::{FULL_A}"
    assert resolve_to_hash(addr, FakeStore()) == f"sha256::{FULL_A}"


def test_fragment_match_ok():
    addr = f"sha256::{FULL_A}#sha256::{FULL_A}"
    assert resolve_to_hash(addr, FakeStore()) == f"sha256::{FULL_A}"


def test_fragment_mismatch():
    addr = f"sha256::{FULL_A}#sha256::{FULL_B}"
    with pytest.raises(ResolveError) as e:
        resolve_to_hash(addr, FakeStore())
    assert e.value.code == "hash_mismatch"


def test_invalid_address_with_no_alias():
    with pytest.raises(ResolveError) as e:
        resolve_to_hash("not-an-address", FakeStore())
    assert e.value.code == "invalid_address"


def test_legacy_id_falls_back_to_alias():
    store = FakeStore(aliases={"finding-20260603-zr29": f"sha256::{FULL_A}"})
    assert resolve_to_hash("finding-20260603-zr29", store) == f"sha256::{FULL_A}"


def test_resolve_error_rejects_unknown_code():
    with pytest.raises(ValueError):
        ResolveError("made_up_code", "addr")


# --- StationIndex adapter against a real store ------------------------------


def test_station_index_strips_algo_prefix(tmp_path):
    store = SkeinNextStore(tmp_path / ".skein")
    try:
        h = store.create_folio(
            {
                "type": "finding",
                "title": "t",
                "content": "c",
                "created_at": "2026-01-01T00:00:00Z",
                "created_by": "a",
            }
        )
        digest = h.split("::", 1)[1]
        index = _StoreStationIndex(store)
        matches = index.folios_with_prefix("sha256", digest[:8])
        assert digest in matches
        assert all("::" not in m for m in matches)  # bare digests, not addresses
    finally:
        store.close()


def test_short_hash_resolves_through_real_store(tmp_path):
    store = SkeinNextStore(tmp_path / ".skein")
    try:
        h = store.create_folio(
            {
                "type": "finding",
                "title": "t",
                "content": "c",
                "created_at": "2026-01-01T00:00:00Z",
                "created_by": "a",
            }
        )
        digest = h.split("::", 1)[1]
        assert resolve_to_hash(f"site::sha256::{digest[:10]}", store) == h
    finally:
        store.close()
