"""Thread C — verify_wire_manifest totality + membership recompute + root-vs-leaflist
(VM1-VM12), plus the manifest signer surface SG1/SG2 (Thread A.3) and the P7
verify_wire_folio kind pin.

verify_wire_manifest is TOTAL over a hostile manifest_signature: it returns typed
BARE reasons, never raises, never 500s. Membership is v0 root-recompute over the
full leaf list — no inclusion-proof code (VM10).
"""

from __future__ import annotations

import hashlib

import pytest

from skein import signing
from skein.signing import MultiVerifyResult, VerifyResult, VerifyStatus

from skein import canon, profile
from skein import sign as sign_mod


# --- fake signer / verifiers ------------------------------------------------


def _addr(seed: bytes) -> str:
    return "sha256::" + hashlib.sha256(seed).hexdigest()


A, B, C = _addr(b"A"), _addr(b"B"), _addr(b"C")


def _manifest_signer(canonical_bytes):
    """A SignedResult-returning manifest signer (the SG2 shape)."""
    preimage = profile.profiled_preimage(profile.CANON_PROFILE_MANIFEST_V1, canonical_bytes)
    bundle = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=["x"],
        canonical_bytes=preimage,
        canon_version=profile.CANON_PROFILE_MANIFEST_V1,
    )
    return sign_mod.SignedResult(bundle=bundle, issuer="https://idp", subject="alice")


def _bare_bundle_signer(canonical_bytes):
    """The pre-change shape: returns a bare SignatureBundle (SG2 must reject)."""
    preimage = profile.profiled_preimage(profile.CANON_PROFILE_MANIFEST_V1, canonical_bytes)
    return signing.SignatureBundle(
        identity_scheme="sigstore-public-v1", bundles=["x"],
        canonical_bytes=preimage, canon_version=profile.CANON_PROFILE_MANIFEST_V1,
    )


def _binding_verifier(canonical_bytes, bundle):
    """Emulates verify_multi's fail-closed binding: SIGNATURE_MISMATCH if the
    preimage handed in diverges from the bundle's stored canonical_bytes."""
    if bundle.canonical_bytes != canonical_bytes:
        return MultiVerifyResult(
            results=[VerifyResult(status=VerifyStatus.SIGNATURE_MISMATCH)],
            overall=VerifyStatus.SIGNATURE_MISMATCH,
        )
    return MultiVerifyResult(
        results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer="https://idp", subject="alice")],
        overall=VerifyStatus.VERIFIED,
    )


def _bad_verifier(canonical_bytes, bundle):
    return MultiVerifyResult(
        results=[VerifyResult(status=VerifyStatus.SIGNATURE_MISMATCH)],
        overall=VerifyStatus.SIGNATURE_MISMATCH,
    )


# --- SG1/SG2: manifest signer surface ---------------------------------------


def test_sign_manifest_single_bundle_one_ceremony():  # SG1
    calls = []

    def counting_signer(cb):
        calls.append(cb)
        return _manifest_signer(cb)

    ms = sign_mod.sign_manifest([B, A, A], counting_signer)  # unsorted + dup
    assert len(calls) == 1  # exactly one signer invocation per publish
    assert set(ms["descriptor"].keys()) == {"root", "leaf_count"}
    assert ms["descriptor"]["leaf_count"] == 2
    assert ms["leaf_list"] == sorted({A, B})  # sorted deduped
    assert ms["issuer"] == "https://idp" and ms["subject"] == "alice"
    assert "signature_bundle" in ms
    # the SIGNED bytes are the descriptor only
    assert calls[0] == canon.manifest_descriptor_canonical_bytes(
        ms["descriptor"]["root"], ms["descriptor"]["leaf_count"]
    )


def test_signer_return_shape_carries_identity():  # SG2
    ms = sign_mod.sign_manifest([A], _manifest_signer)
    assert ms["issuer"] == "https://idp" and ms["subject"] == "alice"
    # a bare-SignatureBundle signer (no issuer/subject) must FAIL
    with pytest.raises((AttributeError, TypeError)):
        sign_mod.sign_manifest([A], _bare_bundle_signer)


# --- VM1-VM12: verify_wire_manifest -----------------------------------------


def test_sign_then_verify_wire_manifest_round_trip():  # VM1
    ms = sign_mod.sign_manifest([A, B], _manifest_signer)
    verified, reason, identity = sign_mod.verify_wire_manifest(ms, _binding_verifier)
    assert verified is True and reason == "verified"
    assert identity == {"issuer": "https://idp", "subject": "alice"}


def test_verify_wire_manifest_absent_is_not_error():  # VM2
    ms = sign_mod.sign_manifest([A, B], _manifest_signer)
    del ms["signature_bundle"]  # well-shaped descriptor, no bundle
    verified, reason, identity = sign_mod.verify_wire_manifest(ms, _binding_verifier)
    assert (verified, reason, identity) == (False, "no manifest", None)


def test_verify_wire_manifest_tampered_descriptor_fails():  # VM3
    ms = sign_mod.sign_manifest([A, B], _manifest_signer)
    # Substitute a DIFFERENT but self-consistent body, keeping the old bundle.
    other = sign_mod.build_manifest([C])
    ms["descriptor"] = {"root": other["root"], "leaf_count": other["leaf_count"]}
    ms["leaf_list"] = other["leaf_list"]
    verified, reason, identity = sign_mod.verify_wire_manifest(ms, _binding_verifier)
    assert verified is False and reason == "SIGNATURE_MISMATCH" and identity is None


def test_verify_wire_manifest_bad_signature_rejected():  # VM4
    ms = sign_mod.sign_manifest([A, B], _manifest_signer)
    verified, reason, identity = sign_mod.verify_wire_manifest(ms, _bad_verifier)
    assert (verified, reason, identity) == (False, "SIGNATURE_MISMATCH", None)


def test_verify_wire_manifest_unknown_profile():  # VM5 / P5
    ms = sign_mod.sign_manifest([A, B], _manifest_signer)
    bundle = signing.SignatureBundle.model_validate_json(ms["signature_bundle"])
    bogus = bundle.model_copy(update={"canon_version": "skein.bogus/v9"})
    ms["signature_bundle"] = bogus.model_dump_json()
    verified, reason, identity = sign_mod.verify_wire_manifest(ms, _binding_verifier)
    assert (verified, reason, identity) == (False, "unknown profile", None)


def test_verify_wire_manifest_wrong_kind():  # P6 (at verify seam)
    ms = sign_mod.sign_manifest([A, B], _manifest_signer)
    bundle = signing.SignatureBundle.model_validate_json(ms["signature_bundle"])
    folio_kind = bundle.model_copy(update={"canon_version": profile.CANON_PROFILE_V1})
    ms["signature_bundle"] = folio_kind.model_dump_json()
    verified, reason, identity = sign_mod.verify_wire_manifest(ms, _binding_verifier)
    assert (verified, reason, identity) == (False, "wrong kind", None)


@pytest.mark.parametrize("bad", ["x", ["x"], 7, True, None])
def test_verify_wire_manifest_hostile_shape_totality(bad):  # VM6
    verified, reason, identity = sign_mod.verify_wire_manifest(bad, _binding_verifier)
    assert (verified, reason, identity) == (False, "manifest malformed", None)


def test_verify_wire_manifest_hostile_descriptor_and_leaflist():  # VM6 (sub-shapes)
    base = sign_mod.sign_manifest([A, B], _manifest_signer)
    for mutate in (
        lambda m: m.update(descriptor="x"),                      # descriptor not dict
        lambda m: m["descriptor"].pop("root"),                   # missing root
        lambda m: m["descriptor"].update(leaf_count="2"),        # leaf_count not int
        lambda m: m.update(leaf_list="notalist"),                # leaf_list a string
        lambda m: m.update(leaf_list=[1, 2]),                    # non-string entries
        lambda m: m.pop("leaf_list"),                            # missing leaf_list
    ):
        m = {**base, "descriptor": dict(base["descriptor"]), "leaf_list": list(base["leaf_list"])}
        mutate(m)
        verified, reason, _ = sign_mod.verify_wire_manifest(m, _binding_verifier)
        assert verified is False and reason == "manifest malformed", mutate


def test_verify_wire_manifest_oversized_leaf_list_rejects_cleanly():  # VM7
    ms = sign_mod.sign_manifest([A, B], _manifest_signer)
    # leaf_list longer than the declared leaf_count -> bounded reject before merkle
    ms["leaf_list"] = ms["leaf_list"] + [_addr(b"extra%d" % i) for i in range(100)]
    verified, reason, _ = sign_mod.verify_wire_manifest(ms, _binding_verifier)
    assert verified is False and reason == "manifest malformed"


def test_verify_wire_manifest_leaf_count_over_max_rejects_before_merkle():  # VM7
    # A hostile descriptor declaring leaf_count beyond the absolute cap is rejected
    # 'manifest malformed' by the length-bound (VM7), BEFORE any decode / merkle
    # recompute — so a public attacker cannot force unbounded work with a huge
    # declared count. leaf_list stays empty (len 0 <= leaf_count) so this exercises
    # the leaf_count > MAX_LEAVES branch specifically, not len(leaf_list) > leaf_count.
    ms = {
        "descriptor": {"root": _addr(b"root"), "leaf_count": sign_mod.MAX_LEAVES + 1},
        "leaf_list": [],
        "signature_bundle": "ignored — the bound fires before the bundle is touched",
    }
    verified, reason, identity = sign_mod.verify_wire_manifest(ms, _binding_verifier)
    assert (verified, reason, identity) == (False, "manifest malformed", None)
    # the cap is a sane public ceiling, not the old 1M
    assert sign_mod.MAX_LEAVES == 2048


def test_verify_wire_manifest_non_string_leaf_within_bounds_still_rejects():  # VM7 reorder
    # The size bound now runs BEFORE the per-element type scan. A leaf_list that is
    # within the size bound but carries a non-string element must STILL reject
    # 'manifest malformed' — i.e. reordering the guards did not drop the type check.
    ms = {
        "descriptor": {"root": _addr(b"root"), "leaf_count": 2},
        "leaf_list": [A, 123],  # len 2 <= leaf_count 2, but 123 is not a str
        "signature_bundle": "ignored — fails on the element-type scan",
    }
    verified, reason, identity = sign_mod.verify_wire_manifest(ms, _binding_verifier)
    assert (verified, reason, identity) == (False, "manifest malformed", None)


def test_membership_by_root_recompute():  # VM8
    ms = sign_mod.build_manifest([A, B])
    assert canon.manifest_membership(ms["leaf_list"], ms["root"], A) is True
    assert canon.manifest_membership(ms["leaf_list"], ms["root"], C) is False


def test_root_does_not_match_leaflist_is_manifest_malformed():  # VM9
    ms = sign_mod.sign_manifest([A, B], _manifest_signer)
    ms["leaf_list"] = [A, C]  # leaf_list no longer recomputes to descriptor.root
    verified, reason, _ = sign_mod.verify_wire_manifest(ms, _binding_verifier)
    assert verified is False and reason == "manifest malformed"
    # and a lying leaf_count
    ms2 = sign_mod.sign_manifest([A, B], _manifest_signer)
    ms2["descriptor"] = {**ms2["descriptor"], "leaf_count": 5}
    verified2, reason2, _ = sign_mod.verify_wire_manifest(ms2, _binding_verifier)
    assert verified2 is False and reason2 == "manifest malformed"


def test_no_inclusion_proof_path_in_v0():  # VM10
    import inspect

    params = list(inspect.signature(canon.manifest_membership).parameters)
    assert "leaf_list" in params  # full leaf list, never an inclusion path
    assert not any("proof" in p or "path" in p for p in params)


# VM11/VM12 (ingress integration: non-dict -> 400; malformed -> per-verdict 200,
# never 500) are exercised in test_require_signed once the ingress gate lands.


# --- P7: verify_wire_folio kind pin -----------------------------------------


def test_folio_bundle_with_manifest_profile_rejected():  # P7
    from skein.identity import compute_folio_hash

    fields = {"type": "finding", "title": "T", "content": "b",
              "created_at": "2026-01-01T00:00:00Z", "created_by": "a"}
    ch = compute_folio_hash(fields)
    # a bundle whose canon_version resolves to kind 'manifest', presented as a folio
    bundle = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1", bundles=["x"],
        canonical_bytes=canon.folio_canonical_bytes(fields),
        canon_version=profile.CANON_PROFILE_MANIFEST_V1,
    )
    wf = {**fields, "content_hash": ch, "signature_bundle": bundle.model_dump_json()}
    verified, reason, _ = sign_mod.verify_wire_folio(wf, _binding_verifier)
    assert verified is False and reason == "wrong kind"
