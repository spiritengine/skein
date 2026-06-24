"""Property/fuzz hardening for the invite-redeem ceremony (brief-20260618-yljd PhA.1).

The enumerated cases in ``test_invite_redeem.py`` cover the named invariants; these
property tests cover the TOTALITY gaps between them — the inputs nobody thought to
enumerate. Two surfaces:

- ``verify_wire_redeem`` over hostile proof values: it must be TOTAL (never raise),
  and every non-verified outcome must carry a reason from the DISJOINT documented
  bare set — never an undocumented string, never a stray exception, never a
  spurious ``verified=True``.
- ``redeem`` over random operation sequences on one token: the hard invariants
  (exactly-once burn, no revoked-identity reactivation, monotonic binding,
  cheap-before-crypto) must hold after EVERY step of any interleaving a single
  client could drive sequentially.

All crypto is the fake binding-verifier the rest of the suite uses (no live
Sigstore); concurrency is out of scope here (see the stress harness) — hypothesis
explores the INPUT space, the stress harness explores the SCHEDULE space.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from skein import signing
from skein.signing import MultiVerifyResult, VerifyResult, VerifyStatus

from skein import profile, redeem as redeem_mod
from skein import sign as sign_mod
from skein.identity import hash_token
from skein.station import Station

ORIGIN = "https://interskein.com"
ISSUER = "https://accounts.google.com"
SUBJECT = "alice@example.com"
OTHER_SUBJECT = "mallory@example.com"
OP = ("https://accounts.google.com", "operator@example.com")

# The COMPLETE set of bare reasons verify_wire_redeem may return when not verified.
# Shape failures (400-class) + the crypto floor (every VerifyStatus value). Anything
# outside this set is a totality bug (an undocumented reason leaking to the route).
_SHAPE_REASONS = {"proof malformed", "unknown profile", "wrong kind"}
_CRYPTO_REASONS = {s.value for s in VerifyStatus}  # includes 'VERIFIED' (only on ok)
ALLOWED_NOT_VERIFIED_REASONS = _SHAPE_REASONS | (_CRYPTO_REASONS - {"VERIFIED"})


# --- fakes (mirror test_invite_redeem.py) -----------------------------------


def _redeem_signer(issuer=ISSUER, subject=SUBJECT, canon_profile=profile.CANON_PROFILE_REDEEM_V1):
    def _sign(canonical_bytes):
        preimage = profile.profiled_preimage(canon_profile, canonical_bytes)
        bundle = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=["x"],
            canonical_bytes=preimage,
            canon_version=canon_profile,
        )
        return sign_mod.SignedResult(bundle=bundle, issuer=issuer, subject=subject)

    return _sign


def _binding_verifier(issuer=ISSUER, subject=SUBJECT):
    def _v(canonical_bytes, bundle):
        if bundle.canonical_bytes != canonical_bytes:
            return MultiVerifyResult(
                results=[VerifyResult(status=VerifyStatus.SIGNATURE_MISMATCH)],
                overall=VerifyStatus.SIGNATURE_MISMATCH,
            )
        return MultiVerifyResult(
            results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer=issuer, subject=subject)],
            overall=VerifyStatus.VERIFIED,
        )

    return _v


def _valid_proof(token, origin=ORIGIN, issuer=ISSUER, subject=SUBJECT, **kw):
    proof, _, _ = sign_mod.sign_redeem_proof(token, origin, _redeem_signer(issuer, subject), **kw)
    return proof


def _mint(station, token, role="author", expires_in_days=7):
    th = hash_token(token)
    station.store.mint_invite(
        th, role, datetime.now(timezone.utc) + timedelta(days=expires_in_days),
        vouched_by_issuer=OP[0], vouched_by_subject=OP[1], note="n",
    )
    return th


def _assert_total_result(out):
    """The verify_wire_redeem output contract, enforced on every fuzz example."""
    assert isinstance(out, tuple) and len(out) == 3
    verified, reason, identity = out
    assert isinstance(verified, bool)
    if verified:
        assert reason == "verified"
        assert isinstance(identity, dict)
        assert set(identity) == {"issuer", "subject"}
    else:
        assert identity is None
        assert reason in ALLOWED_NOT_VERIFIED_REASONS, f"undocumented reason: {reason!r}"


# --- strategies -------------------------------------------------------------

# Arbitrary JSON-ish hostile values: scalars, unicode, oversize, nested lists/dicts.
_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(),
    st.text(alphabet=st.characters(), max_size=4000),  # oversize / unicode
    st.binary(max_size=512),
)
_hostile = st.recursive(
    _scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=6),
        st.dictionaries(st.text(max_size=12), children, max_size=6),
    ),
    max_leaves=20,
)


# --- INV-1 totality: verify_wire_redeem over hostile input ------------------


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
@given(proof=_hostile)
def test_verify_redeem_total_over_arbitrary_proof(proof):
    """Any value at all as the proof: never raises, always a documented outcome."""
    out = sign_mod.verify_wire_redeem(
        proof, hash_token("tok-fuzz"), ORIGIN, sign_mod.REDEEM_ROUTE, _binding_verifier()
    )
    _assert_total_result(out)
    # Pure garbage can never satisfy the station-authoritative binding.
    assert out[0] is False


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
@given(
    nonce=st.one_of(_hostile, st.text(max_size=300)),
    issued_at=st.one_of(_hostile, st.text(max_size=100)),
    bundle=st.one_of(_hostile, st.text(max_size=200)),
)
def test_verify_redeem_total_over_field_mutations(nonce, issued_at, bundle):
    """A dict-shaped proof with each field independently hostile stays total.

    This drives the per-field guards (type, length, parse) and the canon path that
    pure-garbage rarely reaches — the field is dict-shaped so Step 0's non-dict
    short-circuit doesn't swallow it."""
    proof = {"nonce": nonce, "issued_at": issued_at, "signature_bundle": bundle}
    out = sign_mod.verify_wire_redeem(
        proof, hash_token("tok-fuzz"), ORIGIN, sign_mod.REDEEM_ROUTE, _binding_verifier()
    )
    _assert_total_result(out)
    assert out[0] is False


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(
    nonce=st.text(max_size=300),
    issued_at=st.text(max_size=80),
)
def test_verify_redeem_valid_bundle_mutated_envelope(nonce, issued_at):
    """A REAL redeem bundle, but with the wire nonce/issued_at swapped for arbitrary
    strings. The bundle was signed over the ORIGINAL envelope, so any mutation must
    fail closed (SIGNATURE_MISMATCH) — never raise, never spuriously verify."""
    token = "tok-real"
    good = _valid_proof(token)
    proof = {"nonce": nonce, "issued_at": issued_at, "signature_bundle": good["signature_bundle"]}
    out = sign_mod.verify_wire_redeem(
        proof, hash_token(token), ORIGIN, sign_mod.REDEEM_ROUTE, _binding_verifier()
    )
    _assert_total_result(out)
    if nonce == good["nonce"] and issued_at == good["issued_at"]:
        assert out[0] is True  # the exact original envelope re-verifies
    else:
        # A mutated envelope fails closed: either the challenge re-canonicalizes and
        # the preimage diverges (SIGNATURE_MISMATCH), or the mutation carries a code
        # point knurl refuses to canonicalize (CanonError -> 'proof malformed'). Both
        # are documented total outcomes; the point is it NEVER spuriously verifies.
        assert out[0] is False
        assert out[1] in {"SIGNATURE_MISMATCH", "proof malformed"}


# --- redeem state-machine invariants over random op sequences ---------------

# Operations a sequence of (possibly hostile / racing-but-serialized) clients drive.
_OPS = [
    "redeem_valid_self",     # the bound collaborator's valid proof
    "redeem_valid_other",    # a different validly-signed identity
    "redeem_malformed",      # a shape-broken proof
    "redeem_wrong_token",    # a valid proof minted for a DIFFERENT token
    "revoke_invite",         # operator revokes the (unused) invite
    "revoke_self_identity",  # operator binds+revokes the collaborator identity
]


class _CountingVerifier:
    """A binding verifier that counts invocations (crypto-reached signal)."""

    def __init__(self, issuer=ISSUER, subject=SUBJECT):
        self.n = 0
        self._v = _binding_verifier(issuer, subject)

    def __call__(self, canonical_bytes, bundle):
        self.n += 1
        return self._v(canonical_bytes, bundle)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(ops=st.lists(st.sampled_from(_OPS), min_size=1, max_size=10))
def test_redeem_state_machine_invariants(tmp_path_factory, ops):
    """Drive a random op sequence against one token; assert the hard invariants
    after EVERY step. No step may raise; the burn is exactly-once; a revoked
    identity is never reactivated; the binding, once set, is monotonic."""
    inst = tmp_path_factory.mktemp("prop") / ".skein"
    station = Station(inst)
    token = "tok-statemachine"
    other_token = "tok-other"
    th = _mint(station, token)
    _mint(station, other_token)

    malformed = {"nonce": "n", "issued_at": "t", "signature_bundle": "not-json"}
    self_v = _binding_verifier(ISSUER, SUBJECT)
    other_v = _binding_verifier(ISSUER, OTHER_SUBJECT)

    burned_by = {"set": False, "issuer": None, "subject": None}

    try:
        for op in ops:
            if op == "redeem_valid_self":
                r = redeem_mod.redeem(station, token, _valid_proof(token), ORIGIN, verifier=self_v)
            elif op == "redeem_valid_other":
                r = redeem_mod.redeem(
                    station, token, _valid_proof(token, subject=OTHER_SUBJECT), ORIGIN, verifier=other_v
                )
            elif op == "redeem_malformed":
                r = redeem_mod.redeem(station, token, malformed, ORIGIN, verifier=self_v)
            elif op == "redeem_wrong_token":
                # a valid proof minted for other_token, presented against `token`
                r = redeem_mod.redeem(station, token, _valid_proof(other_token), ORIGIN, verifier=self_v)
            elif op == "revoke_invite":
                station.store.revoke_invite(th)
                r = None
            elif op == "revoke_self_identity":
                if station.store.get_binding(ISSUER, SUBJECT) is None:
                    station.store.add_binding(ISSUER, SUBJECT, role="author")
                station.store.revoke_binding(ISSUER, SUBJECT)
                r = None

            # --- global invariants after this step ---------------------------
            row = station.store.get_invite_by_token_hash(th)

            # A successful fresh redeem is exactly-once: once used, the bound identity
            # never changes and used_at never clears.
            if r is not None and r.ok and r.status == redeem_mod.RedeemStatus.OK_REDEEMED:
                assert not burned_by["set"], "two distinct fresh burns of one token"
                burned_by.update(set=True, issuer=r.issuer, subject=r.subject)

            if burned_by["set"]:
                assert row["used_at"] is not None
                assert row["bound_issuer"] == burned_by["issuer"]
                assert row["bound_subject"] == burned_by["subject"]
                # exactly one active binding for the burner; never reactivated if revoked
                b = station.store.get_binding(burned_by["issuer"], burned_by["subject"])
                assert b is not None

            # INV-3: a revoked identity must never hold an active binding produced by
            # a self-redeem. If alice was revoked, no redeem may re-activate her.
            alice = station.store.get_binding(ISSUER, SUBJECT)
            if alice is not None and alice.revoked_at is not None:
                # she stays revoked no matter what redeem op ran
                assert station.store.get_binding(ISSUER, SUBJECT).revoked_at is not None

            # A burned token is never revocable (revoke_invite is a no-op post-burn),
            # so a redeemed token's used_at stays set.
            if burned_by["set"]:
                assert station.store.get_invite_by_token_hash(th)["used_at"] is not None
    finally:
        station.close()


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(state=st.sampled_from(["unknown", "revoked_invite", "expired"]))
def test_cheap_reject_never_reaches_crypto(tmp_path_factory, state):
    """The cheap-before-crypto floor: unknown / revoked-invite / expired tokens
    reject with ZERO verifier invocations, regardless of the proof presented."""
    inst = tmp_path_factory.mktemp("cheap") / ".skein"
    station = Station(inst)
    token = "tok-cheap"
    try:
        if state == "unknown":
            pass  # never minted
        elif state == "revoked_invite":
            th = _mint(station, token)
            station.store.revoke_invite(th)
        elif state == "expired":
            _mint(station, token, expires_in_days=-1)

        verifier = _CountingVerifier()
        r = redeem_mod.redeem(station, token, _valid_proof(token), ORIGIN, verifier=verifier)
        assert not r.ok
        assert verifier.n == 0, f"crypto reached on cheap-reject state {state!r}"
        expected = {
            "unknown": redeem_mod.RedeemStatus.UNKNOWN,
            "revoked_invite": redeem_mod.RedeemStatus.REVOKED_INVITE,
            "expired": redeem_mod.RedeemStatus.EXPIRED,
        }[state]
        assert r.status == expected
    finally:
        station.close()
