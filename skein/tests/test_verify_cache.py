"""Thread D — verify_cache mechanics (VC1-VC11, VC14).

The cache stores ONLY the manifest SIGNATURE verdict (step 3), keyed on
(manifest_hash, bundle_hash). The INGRESS is the sole writer; the read app opens
mode=ro and never writes. Recoverable statuses are never cached. Table-absent
degrades to a cache MISS, never a 500.
"""

from __future__ import annotations

import json

import pytest

from skein import signing
from skein.signing import MultiVerifyResult, VerifyResult, VerifyStatus

from skein import profile, sign as sign_mod, wire
from skein.store import SkeinStore, bundle_hash_for
from skein.station import Station
from skein.ingress import ingest


I, ALICE = "https://idp", "alice@example.com"
MH = "sha256::" + "a" * 64
BH = "bundlehash-1"


@pytest.fixture
def store(tmp_path):
    s = SkeinStore(tmp_path / ".skein")
    yield s
    s.close()


# --- VC1-VC5: cache put/get + recoverable-status non-caching -----------------


def test_verify_cache_miss_then_hit(store):  # VC1
    assert store.verify_cache_get(MH, BH) is None
    store.verify_cache_put(MH, BH, "VERIFIED", I, ALICE)
    row = store.verify_cache_get(MH, BH)
    assert row["status"] == "VERIFIED" and row["subject"] == ALICE


def test_verify_cache_keyed_on_manifest_and_bundle_hash(store):  # VC2
    store.verify_cache_put(MH, "bh1", "VERIFIED")
    assert store.verify_cache_get(MH, "bh2") is None


def test_verify_cache_never_caches_trust_root_stale(store):  # VC3
    store.verify_cache_put(MH, BH, "TRUST_ROOT_STALE")
    assert store.verify_cache_get(MH, BH) is None


def test_verify_cache_never_caches_offline_no_trusted_root(store):  # VC4
    store.verify_cache_put(MH, BH, "OFFLINE_NO_TRUSTED_ROOT")
    assert store.verify_cache_get(MH, BH) is None


@pytest.mark.parametrize("status", [
    "VERIFIED", "SIGNATURE_MISMATCH", "CERT_INVALID",
    "INCLUSION_FAILED", "IDENTITY_MISMATCH", "BUNDLE_MALFORMED",
])
def test_verify_cache_caches_stable_statuses(store, status):  # VC5
    store.verify_cache_put(MH, BH, status)
    assert store.verify_cache_get(MH, BH)["status"] == status


# --- VC6/VC11: the ingress is the writer; one shared bundle_hash helper -------


def _signer(issuer=I, subject=ALICE):
    def s(cb):
        preimage = profile.profiled_preimage(profile.CANON_PROFILE_MANIFEST_V1, cb)
        bundle = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1", bundles=["x"],
            canonical_bytes=preimage, canon_version=profile.CANON_PROFILE_MANIFEST_V1,
        )
        return sign_mod.SignedResult(bundle=bundle, issuer=issuer, subject=subject)
    return s


def _ok_verifier(cb, b):
    return MultiVerifyResult(
        results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer=I, subject=ALICE)],
        overall=VerifyStatus.VERIFIED,
    )


def _signed_batch(client):
    from skein import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    addrs = [f["content_hash"] for f in batch["folios"]] + [t["thread_hash"] for t in batch["threads"]]
    batch["manifest_signature"] = sign_mod.sign_manifest(addrs, _signer())
    return batch


@pytest.fixture
def client(tmp_path):
    s = Station(tmp_path / "client" / ".skein")
    s.create_site("specs", purpose="p", created_by="t")
    s.post("finding", "specs", "T", "b", created_by="t")
    yield s
    s.close()


def test_ingress_populates_manifest_verdict_at_ingest(tmp_path, client):  # VC6
    instance = Station(tmp_path / "instance" / ".skein")
    instance.store.add_binding(I, ALICE, role="author")
    batch = _signed_batch(client)
    ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    descriptor = batch["manifest_signature"]["descriptor"]
    from skein.canon import manifest_descriptor_canonical_bytes
    from skein.identity import content_hash_for_bytes
    mh = content_hash_for_bytes(
        manifest_descriptor_canonical_bytes(descriptor["root"], descriptor["leaf_count"])
    )
    bh = bundle_hash_for(batch["manifest_signature"]["signature_bundle"])
    assert instance.store.verify_cache_get(mh, bh)["status"] == "VERIFIED"
    instance.close()


def test_bundle_hash_one_shared_helper():  # VC11
    bundle_json = json.dumps({"a": 1, "b": [2, 3]})
    assert bundle_hash_for(bundle_json) == bundle_hash_for(bundle_json)
    assert bundle_hash_for(bundle_json) != bundle_hash_for(bundle_json + " ")


# --- VC10: table-absent degrades to a cache miss -----------------------------


def test_read_verdict_on_missing_verify_cache_table_degrades(tmp_path):  # VC10
    s = SkeinStore(tmp_path / ".skein")
    s.conn.execute("DROP TABLE verify_cache")
    s.conn.commit()
    # a get against the now-absent table is a MISS, not an OperationalError
    assert s.verify_cache_get(MH, BH) is None
    s.close()


def test_verify_cache_get_propagates_non_table_operational_error(store):  # harden C
    """Only the missing-table OperationalError is a cache miss; a real fault
    ("database is locked", I/O error, SQL bug) must PROPAGATE, not be masked as a
    miss that silently degrades every read to in-process verify (VC10 scope)."""
    import sqlite3

    class _LockedConn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

    real = store.conn
    store.conn = _LockedConn()
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            store.verify_cache_get(MH, BH)
    finally:
        store.conn = real  # restore so the fixture teardown can close cleanly


# --- VC7/VC8/VC14: read-side cache behavior ---------------------------------


def _cover(store, content_hash, *, cache_status=None, bind=True, subject=ALICE):
    from skein.canon import manifest_descriptor_canonical_bytes
    from skein.identity import content_hash_for_bytes

    ms = sign_mod.sign_manifest([content_hash], _signer(subject=subject))
    d = ms["descriptor"]
    mh = content_hash_for_bytes(manifest_descriptor_canonical_bytes(d["root"], d["leaf_count"]))
    with store.transaction():
        store.add_manifest(d["root"], mh, json.dumps(d, sort_keys=True),
                           json.dumps(ms["leaf_list"]), ms["signature_bundle"],
                           I, subject, d["leaf_count"])
        store.add_constituent_attribution(content_hash, "folio", d["root"], I, subject)
        if cache_status:
            store.verify_cache_put(mh, bundle_hash_for(ms["signature_bundle"]), cache_status, I, subject)
    if bind:
        store.add_binding(I, subject, role="author")
    return mh


def _seed_folio(tmp_path):
    st = Station(tmp_path / ".skein")
    st.create_site("s", purpose="p", created_by="t")
    h = st.post("finding", "s", "T", "b", created_by="t")
    return st, h


def test_read_verdict_on_cache_miss_does_not_write(tmp_path, monkeypatch):  # VC7
    from skein import envelope as env_mod

    st, h = _seed_folio(tmp_path)
    _cover(st.store, h, cache_status=None, bind=True)  # cold cache
    st.close()
    ro = SkeinStore(tmp_path / ".skein", read_only=True)
    monkeypatch.setattr(
        signing, "verify_multi",
        lambda cb, b: MultiVerifyResult(
            results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer=I, subject=ALICE)],
            overall=VerifyStatus.VERIFIED),
    )
    verdict, _ = env_mod.folio_verdict(ro, h, ro.get_folio(h))
    assert verdict.startswith("SIGNED")  # correct verdict via in-process verify
    ro.close()
    # nothing was written to the cache (read app never writes)
    check = SkeinStore(tmp_path / ".skein")
    assert check.conn.execute("SELECT COUNT(*) FROM verify_cache").fetchone()[0] == 0
    check.close()


def test_read_verdict_on_cache_hit_skips_sigstore(tmp_path):  # VC8
    from skein import envelope as env_mod

    st, h = _seed_folio(tmp_path)
    _cover(st.store, h, cache_status="VERIFIED", bind=True)  # warm
    calls = []
    import skein.sign as sm
    orig = sm.signing.verify_multi
    try:
        sm.signing.verify_multi = lambda cb, b: calls.append(1) or orig(cb, b)
        verdict, _ = env_mod.folio_verdict(st.store, h, st.store.get_folio(h))
    finally:
        sm.signing.verify_multi = orig
    assert verdict.startswith("SIGNED")
    assert calls == []  # warm cache elided the Sigstore step
    st.close()


def test_correctness_holds_with_cold_or_absent_cache(tmp_path, monkeypatch):  # VC14
    from skein import envelope as env_mod

    monkeypatch.setattr(
        signing, "verify_multi",
        lambda cb, b: MultiVerifyResult(
            results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer=I, subject=ALICE)],
            overall=VerifyStatus.VERIFIED),
    )
    # warm
    stw, hw = _seed_folio(tmp_path / "warm")
    _cover(stw.store, hw, cache_status="VERIFIED", bind=True)
    vw, _ = env_mod.folio_verdict(stw.store, hw, stw.store.get_folio(hw))
    stw.close()
    # cold (no cache row)
    stc, hc = _seed_folio(tmp_path / "cold")
    _cover(stc.store, hc, cache_status=None, bind=True)
    vc, _ = env_mod.folio_verdict(stc.store, hc, stc.store.get_folio(hc))
    stc.close()
    # absent table
    sta, ha = _seed_folio(tmp_path / "absent")
    _cover(sta.store, ha, cache_status=None, bind=True)
    sta.store.conn.execute("DROP TABLE verify_cache")
    sta.store.conn.commit()
    va, _ = env_mod.folio_verdict(sta.store, ha, sta.store.get_folio(ha))
    sta.close()
    assert vw == vc == va  # the cache is an optimization, not a correctness dependency
    assert vw.startswith("SIGNED")


# --- harden A: step-1 INTEGRITY re-hash precedes the signed path -------------
#
# A tampered body — one that no longer re-hashes to its content_hash key — must
# never read SIGNED, even on a warm cache that would otherwise elide the
# signature step. folio_verdict re-hashes the row FIRST (81fm §C step 1, VC12).


@pytest.mark.parametrize("warm", [True, False], ids=["warm-cache", "cold-cache"])
def test_tampered_folio_body_reads_not_verified_integrity(tmp_path, monkeypatch, warm):
    from skein import envelope as env_mod

    # An honest signed verdict path so the ONLY thing that changes is the tamper.
    monkeypatch.setattr(
        signing, "verify_multi",
        lambda cb, b: MultiVerifyResult(
            results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer=I, subject=ALICE)],
            overall=VerifyStatus.VERIFIED),
    )
    st, h = _seed_folio(tmp_path)
    _cover(st.store, h, cache_status="VERIFIED" if warm else None, bind=True)

    # Baseline: an untampered row reads SIGNED through the same path.
    verdict, ident = env_mod.folio_verdict(st.store, h, st.store.get_folio(h))
    assert verdict.startswith("SIGNED") and ident is not None

    # Tamper the stored body directly (the address key is unchanged), so the body
    # no longer hashes to its content_hash.
    st.store.conn.execute(
        "UPDATE folios SET content = ? WHERE content_hash = ?",
        ("tampered body — was 'b'", h),
    )
    st.store.conn.commit()

    verdict, ident = env_mod.folio_verdict(st.store, h, st.store.get_folio(h))
    assert verdict == "NOT VERIFIED — integrity"
    assert ident is None
    st.close()
