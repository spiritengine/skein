"""Thread A — Merkle construction (MK1-MK8) + manifest descriptor profile (P1-P7).

The publish-time primitive: RFC-6962 Merkle tree over raw constituent digests
(canon.py) and the ``skein.manifest.canon/v1`` descriptor profile (profile.py).
SG1/SG2 (the manifest signer surface) live in test_sign once sign_manifest lands.
"""

from __future__ import annotations

import hashlib

import pytest

from skein import canon, profile


# --- helpers ----------------------------------------------------------------


def _digest(seed: bytes) -> bytes:
    return hashlib.sha256(seed).digest()


def _addr(datum: bytes) -> str:
    return "sha256::" + datum.hex()


# --- MK1-MK8: Merkle tree ---------------------------------------------------


def test_leaf_datum_is_raw_32_bytes_not_ascii():  # MK1
    d = _digest(b"alice")
    addr = _addr(d)
    out = canon.address_to_leaf_datum(addr)
    assert out == bytes.fromhex(addr.split("::", 1)[1])
    assert len(out) == 32
    # the ASCII string form (len 71) is NOT the datum the tree uses
    assert out != addr.encode()
    assert canon.merkle_root([out]) != canon.merkle_leaf_hash(addr.encode())


def test_malformed_address_rejected_before_tree():  # MK2
    for bad in (
        "md5::" + "0" * 32,            # wrong algo prefix
        "sha256::" + "0" * 63,         # wrong length
        "sha256::" + "z" * 64,         # non-hex
        "sha256:" + "0" * 64,          # single colon
        12345,                         # non-str
        None,
        b"sha256::" + b"0" * 64,       # bytes, not str
    ):
        with pytest.raises(canon.MalformedLeafAddress):
            canon.address_to_leaf_datum(bad)


def test_leaf_vs_node_domain_separation():  # MK3
    d = _digest(b"x")
    l, r = _digest(b"l"), _digest(b"r")
    assert canon.merkle_leaf_hash(d) == hashlib.sha256(b"\x00" + d).digest()
    assert canon.merkle_node_hash(l, r) == hashlib.sha256(b"\x01" + l + r).digest()
    # a leaf hash can never equal a node hash (distinct first byte)
    assert canon.merkle_leaf_hash(l + r) != canon.merkle_node_hash(l, r)


def test_leaves_sorted_ascending_then_deduped():  # MK4
    a, b, c = _digest(b"a"), _digest(b"b"), _digest(b"c")
    root1 = canon.merkle_root([a, b, c])
    root2 = canon.merkle_root([c, a, b])               # different input order
    root3 = canon.merkle_root([c, a, b, a, c, b])      # with duplicates
    assert root1 == root2 == root3


def test_odd_node_rfc6962_no_bitcoin_duplication():  # MK5
    leaves = sorted([_digest(b"0"), _digest(b"1"), _digest(b"2")])
    l0, l1, l2 = (canon.merkle_leaf_hash(x) for x in leaves)
    # k = largest power of two strictly < 3 = 2: node(node(l0,l1), l2)
    expected = canon.merkle_node_hash(canon.merkle_node_hash(l0, l1), l2)
    assert canon.merkle_root(leaves) == expected
    # NOT bitcoin duplication of the last node
    bitcoin = canon.merkle_node_hash(
        canon.merkle_node_hash(l0, l1), canon.merkle_node_hash(l2, l2)
    )
    assert canon.merkle_root(leaves) != bitcoin


def test_single_leaf_manifest_of_one():  # MK6
    d = _digest(b"only")
    assert canon.merkle_root([d]) == canon.merkle_leaf_hash(d)


def test_empty_manifest_rejected():  # MK7
    with pytest.raises(canon.EmptyManifest):
        canon.merkle_root([])


def test_merkle_root_known_answer_vector():  # MK8
    d0, d1 = sorted([_digest(b"d0"), _digest(b"d1")])
    expected_two = hashlib.sha256(
        b"\x01"
        + hashlib.sha256(b"\x00" + d0).digest()
        + hashlib.sha256(b"\x00" + d1).digest()
    ).digest()
    assert canon.merkle_root([d0, d1]) == expected_two
    expected_one = hashlib.sha256(b"\x00" + d0).digest()
    assert canon.merkle_root([d0]) == expected_one


# --- P1-P7: descriptor profile ----------------------------------------------


def test_manifest_profile_registered():  # P1
    p = profile.get_profile("skein.manifest.canon/v1")
    assert p.kind == "manifest"
    assert p.fields == ("root", "leaf_count")
    assert p.hash_algo == "sha256"
    # folio profile still registered alongside
    assert profile.get_profile("skein.folio.canon/v1").kind == "folio"


def test_manifest_preimage_domain_separated():  # P2
    b = b"some-bytes"
    m = profile.profiled_preimage(profile.CANON_PROFILE_MANIFEST_V1, b)
    f = profile.profiled_preimage(profile.CANON_PROFILE_V1, b)
    assert m.startswith(b"skein.manifest.canon/v1\x00")
    assert m != f


def test_descriptor_canonical_bytes_deterministic_and_hash_stable():  # P3
    root = "sha256::" + "a" * 64
    b1 = canon.manifest_descriptor_canonical_bytes(root, 3)
    b2 = canon.manifest_descriptor_canonical_bytes(root, 3)
    assert b1 == b2
    from skein.identity import content_hash_for_bytes

    assert content_hash_for_bytes(b1) == content_hash_for_bytes(b2)


def test_leaf_set_is_kind_agnostic():  # P4
    fields = canon.manifest_descriptor_fields("sha256::" + "b" * 64, 2)
    assert set(fields.keys()) == {"root", "leaf_count"}
    assert "folios" not in fields and "threads" not in fields
    # a folio hash and a thread hash both produce one covering root, indistinguishably
    folio_d = _digest(b"folio-body")
    thread_d = _digest(b"thread-edge")
    root = canon.merkle_root([folio_d, thread_d])
    assert root == canon.merkle_root([thread_d, folio_d])  # no per-kind slot


def test_manifest_unknown_profile_hard_fails():  # P5 (registry half; verify seam VM5)
    with pytest.raises(profile.UnknownProfile):
        profile.get_profile("skein.bogus.canon/v9")


def test_manifest_and_folio_kinds_distinct():  # P6/P7 (registry kinds)
    assert profile.get_profile(profile.CANON_PROFILE_MANIFEST_V1).kind == "manifest"
    assert profile.get_profile(profile.CANON_PROFILE_V1).kind == "folio"
    assert profile.get_profile(profile.CANON_PROFILE_REDEEM_V1).kind == "redeem"
    # exactly these three kinds are registered; each profile is its own kind
    kinds = {p.kind for p in profile._REGISTRY.values()}
    assert kinds == {"folio", "manifest", "redeem"}
