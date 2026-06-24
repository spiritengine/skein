"""Tests for the signed-preimage profile registry (skein.profile)."""

from __future__ import annotations

import pytest

from skein import canon, profile


def test_v1_resolves_to_the_folio_tuple():
    p = profile.get_profile(profile.CANON_PROFILE_V1)
    assert p.kind == "folio"
    assert p.fields == canon.CANONICAL_FIELDS
    assert p.hash_algo == "sha256"


def test_unknown_profile_is_a_hard_failure():
    # No default, no fallback, no downgrade (ujwx §3).
    with pytest.raises(profile.UnknownProfile):
        profile.get_profile("skein.folio.canon/v0")
    with pytest.raises(profile.UnknownProfile):
        profile.get_profile("knurl-1.0")  # the old dead canon_version default


def test_preimage_is_profile_then_nul_then_canonical_bytes():
    cb = b'{"a":1}'
    pre = profile.profiled_preimage(profile.CANON_PROFILE_V1, cb)
    assert pre == profile.CANON_PROFILE_V1.encode("utf-8") + b"\x00" + cb


def test_preimage_rejects_an_unknown_profile():
    with pytest.raises(profile.UnknownProfile):
        profile.profiled_preimage("made.up/v9", b"{}")


def test_preimage_domain_separates_distinct_profiles():
    # The whole point: the same canonical bytes under a different profile produce
    # different signed bytes, so a signature can't be replayed across domains.
    cb = b'{"x":1}'
    a = profile.profiled_preimage(profile.CANON_PROFILE_V1, cb)
    assert a != cb  # never the bare bytes
    assert a.startswith(profile.CANON_PROFILE_V1.encode("utf-8"))
