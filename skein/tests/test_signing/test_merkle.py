"""Merkle inclusion algorithm contract for Rekor proofs.

These tests pin the math that turns (leaf_hash, log_index, tree_size, hashes)
into root_hash. Shape-only proof checks are not enough: a verifier that accepts
any non-empty hashes list would miss the core Rekor inclusion guarantee.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

pytest.importorskip(
    "skein.signing",
    reason="skein.signing is the phase-3 deliverable; contract collects but skips until then.",
)

from hypothesis import HealthCheck, assume, given, settings, strategies as st  # noqa: E402

from .conftest import HAS_FUNCTIONS, signing  # noqa: E402

pytestmark = pytest.mark.skipif(
    not HAS_FUNCTIONS,
    reason="signing.sign/verify/verify_multi are Phase 3 deliverables",
)


def _leaf_hash(data: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + data).digest()


def _node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _tree_leaf_data(index: int, target_index: int, target_leaf: bytes) -> bytes:
    if index == target_index:
        return target_leaf
    return b"rekor-merkle-sibling-%d" % index


def _merkle_root_and_path(
    *, target_leaf: bytes, log_index: int, tree_size: int
) -> tuple[bytes, list[bytes], bytes]:
    leaves = [
        _leaf_hash(_tree_leaf_data(i, log_index, target_leaf)) for i in range(tree_size)
    ]
    leaf_hash = leaves[log_index]
    path: list[bytes] = []
    index = log_index
    level = leaves
    while len(level) > 1:
        if index % 2 == 0:
            sibling = index + 1
        else:
            sibling = index - 1
        if sibling < len(level):
            path.append(level[sibling])

        next_level = []
        for offset in range(0, len(level), 2):
            if offset + 1 < len(level):
                next_level.append(_node_hash(level[offset], level[offset + 1]))
            else:
                next_level.append(level[offset])
        index //= 2
        level = next_level
    return level[0], path, leaf_hash


def _bundle_with_merkle_proof(
    crypto_factory,
    *,
    canonical_bytes: bytes,
    log_index: int,
    tree_size: int,
    root_hash: bytes,
    hashes: list[bytes],
    leaf_hash: bytes,
):
    return crypto_factory.make_bundle_blob_with_rekor_inclusion(
        canonical_bytes=canonical_bytes,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
        log_index=log_index,
        tree_size=tree_size,
        leaf_hash=_b64(leaf_hash),
        root_hash=_b64(root_hash),
        hashes=[_b64(h) for h in hashes],
        log_id="test-rekor-log-key-id",
    )


@given(
    log_index=st.integers(min_value=0, max_value=999),
    tree_size=st.integers(min_value=1, max_value=999),
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property_verify_recomputes_merkle_root_from_audit_path(
    crypto_factory, monkeypatch, log_index, tree_size
):
    # Enforces: finding-20260512-eaft blocker #1. verify() must recompute the
    # Rekor Merkle root from leaf_hash + hashes + log_index + tree_size, accept
    # the matching root_hash, and reject a mismatched root_hash.
    assume(log_index < tree_size)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"known Rekor leaf payload"
    root, path, leaf = _merkle_root_and_path(
        target_leaf=canonical,
        log_index=log_index,
        tree_size=tree_size,
    )
    good_blob = _bundle_with_merkle_proof(
        crypto_factory,
        canonical_bytes=canonical,
        log_index=log_index,
        tree_size=tree_size,
        root_hash=root,
        hashes=path,
        leaf_hash=leaf,
    )
    good_bundle = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[good_blob],
        canonical_bytes=canonical,
        canon_version="knurl-1.0",
    )
    assert (
        signing.verify(canonical, good_bundle).status == signing.VerifyStatus.VERIFIED
    )

    bad_root = bytearray(root)
    bad_root[0] ^= 0x01
    bad_blob = _bundle_with_merkle_proof(
        crypto_factory,
        canonical_bytes=canonical,
        log_index=log_index,
        tree_size=tree_size,
        root_hash=bytes(bad_root),
        hashes=path,
        leaf_hash=leaf,
    )
    bad_bundle = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[bad_blob],
        canonical_bytes=canonical,
        canon_version="knurl-1.0",
    )
    assert (
        signing.verify(canonical, bad_bundle).status
        == signing.VerifyStatus.INCLUSION_FAILED
    )


@pytest.mark.parametrize(
    ("tree_size", "log_index"),
    [(1, 0), (2, 0), (4, 2), (8, 7)],
)
def test_verify_accepts_known_good_merkle_vectors(
    crypto_factory, monkeypatch, tree_size, log_index
):
    # Enforces: finding-20260512-eaft blocker #1. Concrete powers-of-two and
    # single-leaf vectors keep the canonical audit-path layout pinned.
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"known-good Rekor vector"
    root, path, leaf = _merkle_root_and_path(
        target_leaf=canonical,
        log_index=log_index,
        tree_size=tree_size,
    )
    blob = _bundle_with_merkle_proof(
        crypto_factory,
        canonical_bytes=canonical,
        log_index=log_index,
        tree_size=tree_size,
        root_hash=root,
        hashes=path,
        leaf_hash=leaf,
    )
    bundle = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=canonical,
        canon_version="knurl-1.0",
    )
    assert signing.verify(canonical, bundle).status == signing.VerifyStatus.VERIFIED
