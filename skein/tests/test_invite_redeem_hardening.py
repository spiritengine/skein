"""Redeem hardening regressions (brief-20260618-yljd Phase A.2).

Two load-bearing properties the enumerated suite did not yet pin:

1. The redeem ROUTE degrades a genuinely-held write lock to a retryable 503 (the
   /publish route had this test; the redeem route shares the handler but lacked
   its own end-to-end coverage). A held lock must never surface as an uncaught 500.

2. ``verify_multi`` holds NO write lock (INV-2's load-bearing claim, the core
   pre-public fear "one redeemer wedges all writers"). Proven deterministically: a
   concurrent publish COMMITS while a redeem is parked mid-verify. If the slow
   verify held the write lock, the publish would block until the verify finished;
   it does not. The mixed-load sweep in /tmp/wedge_harness.py is the empirical
   companion (publish latency is flat across a 0..3s verify sleep); this is the
   fast, deterministic regression guard.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from skein import signing
from skein.signing import MultiVerifyResult, VerifyResult, VerifyStatus

from skein import profile, redeem as redeem_mod
from skein import sign as sign_mod, wire
from skein.identity import hash_token
from skein.ingress import create_app, ENV_DATA_DIR, ENV_ORIGIN
from skein.station import Station

ORIGIN = "https://interskein.com"
ISSUER = "https://accounts.google.com"
SUBJECT = "alice@example.com"
OP = ("https://accounts.google.com", "operator@example.com")


def _redeem_signer(subject=SUBJECT):
    def _sign(canonical_bytes):
        preimage = profile.profiled_preimage(profile.CANON_PROFILE_REDEEM_V1, canonical_bytes)
        bundle = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1", bundles=["x"],
            canonical_bytes=preimage, canon_version=profile.CANON_PROFILE_REDEEM_V1,
        )
        return sign_mod.SignedResult(bundle=bundle, issuer=ISSUER, subject=subject)
    return _sign


def _mint(station, token, expires_in_days=7):
    th = hash_token(token)
    station.store.mint_invite(
        th, "author", datetime.now(timezone.utc) + timedelta(days=expires_in_days),
        vouched_by_issuer=OP[0], vouched_by_subject=OP[1], note="n",
    )
    return th


def _proof(token, subject=SUBJECT):
    proof, _, _ = sign_mod.sign_redeem_proof(token, ORIGIN, _redeem_signer(subject))
    return proof


# --- (1) redeem route degrades a held write lock to 503 ----------------------


def test_redeem_route_real_write_lock_returns_503(tmp_path, monkeypatch):
    """A genuinely-held write lock makes the redeem path's BEGIN IMMEDIATE time out
    and raise the driver SQLITE_BUSY OperationalError; the route must map it to a
    retryable 503, NOT an uncaught 500. Mirrors the /publish real-lock test."""
    import skein.store as store_mod
    from skein.store import SkeinStore

    d = tmp_path / "inst" / ".skein"
    s = Station(d)
    token = "tok-lock-" + "z" * 32
    _mint(s, token)
    s.close()

    monkeypatch.setenv(ENV_DATA_DIR, str(d))
    monkeypatch.setenv(ENV_ORIGIN, ORIGIN)
    monkeypatch.delenv("SKEIN_REQUIRE_SIGNED", raising=False)
    monkeypatch.setattr(sign_mod, "default_verifier", _binding_verifier())
    monkeypatch.setattr(store_mod, "BUSY_TIMEOUT_MS", 50)  # expire the wait fast
    client = TestClient(create_app())

    holder = SkeinStore(d, check_same_thread=False)
    holder.conn.execute("BEGIN IMMEDIATE")  # hold the write lock
    try:
        # The unused-token path's first write (reserve_redeem_attempt) can't take the
        # lock within the (shrunk) busy_timeout -> OperationalError -> 503.
        r = client.post(sign_mod.REDEEM_ROUTE,
                        content=json.dumps({"token": token, "proof": _proof(token)}),
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 503
        assert r.headers.get("Retry-After") == "1"
        assert "busy" in r.json()["error"]
    finally:
        holder.conn.rollback()
        holder.close()


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


# --- (2) verify_multi holds no write lock ------------------------------------


def _seed_folio(tmp_path):
    """A single valid folio dict for a concurrent publish."""
    src = Station(tmp_path / "src" / ".skein")
    try:
        src.create_site("specs", purpose="p", created_by="t")
        return src.store.get_folio(src.post("finding", "specs", "T", "body", created_by="t"))
    finally:
        src.close()


def test_verify_multi_holds_no_write_lock(tmp_path):
    """While a redeem is parked INSIDE verify_multi, a concurrent publish must commit
    promptly. If verify held the write lock, the publish would block for the whole
    verify; it does not. This is INV-2's load-bearing claim, made deterministic."""
    inst = tmp_path / "inst" / ".skein"
    s = Station(inst)
    token = "tok-nolock-" + "y" * 32
    _mint(s, token)
    s.close()
    folio = _seed_folio(tmp_path)

    in_verify = threading.Event()
    release = threading.Event()
    VERIFY_BLOCK = 5.0  # the redeem parks here; the publish must NOT wait this long

    def slow_verifier(canonical_bytes, bundle):
        in_verify.set()  # signal: reserve has committed+released; we're now mid-verify
        # Hold here far longer than any legitimate short write needs.
        release.wait(timeout=VERIFY_BLOCK)
        if bundle.canonical_bytes != canonical_bytes:
            return MultiVerifyResult(
                results=[VerifyResult(status=VerifyStatus.SIGNATURE_MISMATCH)],
                overall=VerifyStatus.SIGNATURE_MISMATCH,
            )
        return MultiVerifyResult(
            results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer=ISSUER, subject=SUBJECT)],
            overall=VerifyStatus.VERIFIED,
        )

    redeem_out = {}

    def run_redeem():
        st = Station(inst, check_same_thread=False)
        try:
            redeem_out["r"] = redeem_mod.redeem(st, token, _proof(token), ORIGIN, verifier=slow_verifier)
        finally:
            st.close()

    t = threading.Thread(target=run_redeem)
    t.start()
    try:
        assert in_verify.wait(timeout=5.0), "redeem never reached verify"
        # The redeem is now parked in verify (its reserve write already committed and
        # released the lock). A publish — a real write — must take the lock and commit
        # WITHOUT waiting for the verify to finish.
        pub = Station(inst, check_same_thread=False)
        try:
            t0 = time.monotonic()
            from skein.ingress import ingest
            ack = ingest(pub, {"protocol": wire.PROTOCOL, "folios": [folio], "threads": [], "site_slugs": {}},
                         require_signed=False)
            elapsed = time.monotonic() - t0
        finally:
            pub.close()
        assert len(ack["accepted"]) == 1, f"publish did not commit: {ack}"
        # If verify held the write lock, this would be ~VERIFY_BLOCK. It must be a
        # short write, well under the block — generous margin for a loaded CI box.
        assert elapsed < VERIFY_BLOCK / 2, (
            f"publish blocked {elapsed:.2f}s while a redeem was mid-verify — "
            "verify_multi appears to hold the write lock (INV-2 violated)"
        )
    finally:
        release.set()
        t.join(timeout=10)
    assert redeem_out["r"].ok and redeem_out["r"].status == redeem_mod.RedeemStatus.OK_REDEEMED
