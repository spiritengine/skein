"""Thread F2 — offline end-to-end dress rehearsal (E1-E6, CI-automated) + the
read-open/mount cells (E10, E11).

E1 proves the REAL signing primitive plugs into publish->ingest->read offline (via
signing._test_factory, no network). E2-E6 prove the rejection/acceptance LOGIC of
the unified manifest gate end-to-end. E7-E9 (the real OIDC ceremony + droplet) are
the human-relay checklist, not pytest.
"""

from __future__ import annotations

import base64
import json

import pytest

from skein import signing
from skein.signing import MultiVerifyResult, VerifyResult, VerifyStatus, _test_factory

from skein import profile, sign as sign_mod, wire
from skein import publish as pub_mod
from skein.ingress import ingest
from skein.station import Station
from skein.store import SkeinStore
from skein.envelope import folio_verdict


def _unsigned_jwt(aud="sigstore", issuer="https://accounts.google.com") -> str:
    def b64(d):
        raw = json.dumps(d, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f'{b64({"alg": "none", "typ": "JWT"})}.{b64({"sub": "alice@example.com", "iss": issuer, "aud": aud})}.'


@pytest.fixture
def provider():
    return signing.OIDCProviderConfig(
        issuer="https://accounts.google.com", token=_unsigned_jwt(), provider_id="google"
    )


@pytest.fixture
def client(tmp_path):
    s = Station(tmp_path / "client" / ".skein")
    s.create_site("specs", purpose="Public specs", created_by="t")
    s.post("finding", "specs", "Design Overview", "body", created_by="t")
    yield s
    s.close()


@pytest.fixture
def instance(tmp_path):
    s = Station(tmp_path / "instance" / ".skein")
    yield s
    s.close()


# --- offline fake signer/verifier (for the logic cells E2-E6) ----------------

I, ALICE = "https://idp", "alice@example.com"


def _fake_signer(issuer=I, subject=ALICE):
    def s(cb):
        preimage = profile.profiled_preimage(profile.CANON_PROFILE_MANIFEST_V1, cb)
        bundle = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1", bundles=["x"],
            canonical_bytes=preimage, canon_version=profile.CANON_PROFILE_MANIFEST_V1)
        return sign_mod.SignedResult(bundle=bundle, issuer=issuer, subject=subject)
    return s


def _ok_verifier(cb, b):
    return MultiVerifyResult(
        results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer=I, subject=ALICE)],
        overall=VerifyStatus.VERIFIED)


def _signed_batch(client, signer=None):
    folios, threads, slugs = pub_mod.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    addrs = [f["content_hash"] for f in batch["folios"]] + [t["thread_hash"] for t in batch["threads"]]
    batch["manifest_signature"] = sign_mod.sign_manifest(addrs, signer or _fake_signer())
    return batch


def _finding_hash(batch):
    return next(f["content_hash"] for f in batch["folios"] if f["type"] == "finding")


# --- E1: real signed publish through real signing.py -------------------------


def test_e2e_bound_signed_publish_accepted(client, instance, provider, monkeypatch):  # E1
    _test_factory.install_sign_monkeypatch(monkeypatch, provider=provider)
    _test_factory.install_verify_monkeypatch(monkeypatch)
    signer = sign_mod.make_oidc_signer(provider)
    # discover the verified identity, bind it on the instance
    probe = sign_mod.sign_manifest(["sha256::" + "0" * 64], signer)
    instance.store.add_binding(probe["issuer"], probe["subject"], role="author")
    monkeypatch.setattr(
        pub_mod, "post_batch",
        lambda url, batch, timeout=30.0: ingest(instance, batch, require_signed=True),
    )
    result = pub_mod.publish(client, "http://instance.example", site="specs", signer=signer)
    assert result["signed"] is True
    ack = result["ack"]
    assert len(ack["accepted"]) == 2  # site folio + finding
    # read surface: SIGNED, attributed to the MANIFEST signer; verify_cache warmed
    ro = SkeinStore(instance.store.data_dir, read_only=True)
    try:
        for h in ack["accepted"]:
            verdict, identity = folio_verdict(ro, h, ro.get_folio(h))
            assert verdict.startswith("SIGNED")
            assert identity["issuer"] == probe["issuer"]
    finally:
        ro.close()


# --- dry-run must not run the Sigstore ceremony -----------------------------


def test_dry_run_sign_never_invokes_the_signer(client):
    """A dry run with --sign must NOT sign: signing runs the irreversible Sigstore
    ceremony (Fulcio cert + a PERMANENT, PUBLIC Rekor entry), and a dry run sends
    nothing to an instance. A signer that raises on invocation proves the ceremony
    never happens on a dry run, yet the run still reports the would-publish plan."""

    def exploding_signer(cb):  # the shape sign_manifest calls: cb -> SignedResult
        raise AssertionError("dry-run must not invoke the signer (no Sigstore ceremony)")

    result = pub_mod.publish(
        client, "(dry-run)", site="specs", dry_run=True, signer=exploding_signer
    )
    # Succeeded WITHOUT signing, and reports the plan: it would publish, and (since
    # a signer was supplied) it would be signed — without having signed anything.
    assert result["sent"] is False
    assert result["dry_run"] is True
    assert result["signed"] is True
    assert result["folios"] >= 1
    assert "folio_hashes" in result and len(result["folio_hashes"]) == result["folios"]
    # No real send happened, so there is no instance ack.
    assert "ack" not in result


# --- E2-E5: the gate logic end-to-end ---------------------------------------


def test_e2e_unsigned_publish_rejected(client, instance):  # E2
    folios, threads, slugs = pub_mod.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)  # no manifest
    ack = ingest(instance, batch, require_signed=True)
    assert all(r["reason"] == "no manifest" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_e2e_unbound_signer_rejected(client, instance):  # E3
    batch = _signed_batch(client)  # signer not bound on instance
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "unbound signer" for r in ack["rejected"])


def test_e2e_revoked_signer_rejected(client, instance):  # E4
    instance.store.add_binding(I, ALICE, role="author")
    instance.store.revoke_binding(I, ALICE)
    batch = _signed_batch(client)
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "revoked binding" for r in ack["rejected"])


def test_e2e_membership_accepted_no_manifest_rejected(client, instance):  # E5
    instance.store.add_binding(I, ALICE, role="author")
    batch = _signed_batch(client)
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    # folio AND its within edge accepted, each resolving to (root, signer)
    assert len(ack["accepted"]) == 2 and len(ack["threads"]["accepted"]) == 1
    for h in ack["accepted"] + ack["threads"]["accepted"]:
        proof = instance.store.get_constituent_proof(h)
        assert proof["subject"] == ALICE
    # the same constituents with NO manifest -> 'no manifest'
    folios, threads, slugs = pub_mod.collect_publish_set(client, site="specs")
    bare = wire.build_batch(folios, threads, slugs)
    ack2 = ingest(Station(instance.store.data_dir.parent.parent / "i2" / ".skein"),
                  bare, require_signed=True)
    assert all(r["reason"] == "no manifest" for r in ack2["rejected"])


def test_e2e_read_cache_hit_after_ingest(client, instance, monkeypatch):  # E6
    instance.store.add_binding(I, ALICE, role="author")
    batch = _signed_batch(client)
    ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    finding = _finding_hash(batch)
    # second read hits verify_cache and does not re-run Sigstore
    ro = SkeinStore(instance.store.data_dir, read_only=True)
    calls = []
    monkeypatch.setattr(
        signing, "verify_multi",
        lambda cb, b: calls.append(1) or _ok_verifier(cb, b))
    try:
        verdict, _ = folio_verdict(ro, finding, ro.get_folio(finding))
        assert verdict.startswith("SIGNED")
        assert calls == []  # warm cache elided Sigstore
    finally:
        ro.close()


# --- E10/E11: read open semantics + WAL visibility ---------------------------


def test_read_open_uses_mode_ro_not_immutable_against_wal(tmp_path, monkeypatch):  # E10
    import sqlite3

    st = Station(tmp_path / ".skein")
    st.store.conn.execute("PRAGMA journal_mode=WAL")
    st.create_site("s", purpose="p", created_by="t")
    st.close()

    uris = []
    real_connect = sqlite3.connect

    def spy_connect(database, *a, **k):
        if k.get("uri"):
            uris.append(database)
        return real_connect(database, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", spy_connect)
    ro = SkeinStore(tmp_path / ".skein", read_only=True)
    ro.close()
    assert any("mode=ro" in u for u in uris)
    assert not any("immutable=1" in u for u in uris)  # mode=ro resolved; no fallback


def test_concurrent_ingest_then_read_sees_committed_write(tmp_path):  # E11
    inst = Station(tmp_path / ".skein")
    inst.store.conn.execute("PRAGMA journal_mode=WAL")
    inst.create_site("s", purpose="p", created_by="t")
    h = inst.post("finding", "s", "T", "b", created_by="t")  # committed
    # a fresh read store opened mode=ro sees the committed write (no torn/stale read)
    ro = SkeinStore(tmp_path / ".skein", read_only=True)
    try:
        assert ro.get_folio(h) is not None
    finally:
        ro.close()
        inst.close()
