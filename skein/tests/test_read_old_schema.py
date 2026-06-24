"""Deploy-blocker regression: the READ surface must serve an OLD/partial-schema
corpus without a 500.

The shared-volume deploy-ordering hazard: the read container can come up before
the ingress migration runs (or new read code can be pointed at a corpus that
predates the manifest/authorization tables). The production corpus at cutover is
the OLD per-folio schema — ``folios``/``threads``/``slugs``/``aliases`` with NONE
of the new tables (``manifests``, ``constituent_attribution``, ``verify_cache``,
``account_bindings``, ``binding_events``). Opening that corpus with the new read
app and asking for a folio verdict must degrade to UNSIGNED, never raise
``sqlite3.OperationalError: no such table``.

verify_cache_get already tolerated this (VC10 / Fix C); these cells extend the
same tolerance to every new-table query the READ path can reach
(``get_constituent_proof`` -> manifests/constituent_attribution, ``get_binding``
-> account_bindings).
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from skein import envelope as env_mod
from skein.station import Station
from skein.store import SkeinStore
from skein.web.app import ENV_DATA_DIR, ENV_PROJECT, create_app


# The new tables the read path can touch. Dropping ALL of them reproduces the OLD
# per-folio corpus; dropping a subset reproduces a partially-migrated one. Child
# (FK-bearing) before parent so the DROP order is FK-clean.
_NEW_TABLES = [
    "constituent_attribution",
    "manifests",
    "verify_cache",
    "account_bindings",
    "binding_events",
]


def _seed_then_strip(data_dir, drop):
    """Seed one folio, then DROP ``drop`` (a subset of the new tables) to forge an
    old/partial-schema corpus on disk. Returns the folio's content hash."""
    with Station(data_dir) as st:
        st.create_site("s", purpose="p", created_by="t")
        h = st.post("finding", "s", "T", "body here", created_by="t")
    # A second connection (not the closed Station's) does the surgery.
    surgeon = SkeinStore(data_dir)
    for table in drop:
        surgeon.conn.execute(f"DROP TABLE IF EXISTS {table}")
    surgeon.conn.commit()
    surgeon.close()
    return h


def _assert_serves_unsigned(data_dir, h):
    """The read store must build the folio verdict + envelope WITHOUT raising, and
    the verdict must read UNSIGNED (no covering manifest resolvable)."""
    ro = SkeinStore(data_dir, read_only=True)
    try:
        row = ro.get_folio(h)
        verdict, identity = env_mod.folio_verdict(ro, h, row)
        assert verdict.startswith("UNSIGNED"), verdict
        assert identity is None
        # The read app's envelope-building path (what _folio_response / the HTML
        # render both call) must serve it too — proof null, no crash.
        env = env_mod.build_folio_envelope(ro, h, row=row)
        assert env["proof"]["signature_bundle"] is None
        assert env["asserted"]["verdict"].startswith("UNSIGNED")
    finally:
        ro.close()


# --- the deploy scenario: ALL new tables absent (the OLD corpus) -------------


def test_old_schema_corpus_serves_unsigned_no_500(tmp_path):
    data_dir = tmp_path / ".skein"
    h = _seed_then_strip(data_dir, _NEW_TABLES)
    _assert_serves_unsigned(data_dir, h)


def test_old_schema_corpus_served_over_http_no_500(tmp_path, monkeypatch):
    """End-to-end: the read web app serves a folio from an OLD-schema corpus with a
    200, not a 500 — the literal deploy smoke that flagged this."""
    data_dir = tmp_path / ".skein"
    h = _seed_then_strip(data_dir, _NEW_TABLES)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "stationfile.json").write_text(json.dumps({"name": "Legacy"}), encoding="utf-8")
    monkeypatch.setenv(ENV_DATA_DIR, str(data_dir))
    monkeypatch.delenv(ENV_PROJECT, raising=False)
    client = TestClient(create_app())

    r = client.get(f"/folio/{h}.json")
    assert r.status_code == 200, r.text
    env = r.json()
    assert env["asserted"]["verdict"].startswith("UNSIGNED")
    assert env["proof"]["signature_bundle"] is None

    # The HTML surface (the other envelope consumer) must not 500 either.
    assert client.get(f"/folio/{h}").status_code == 200
    # The catalog/index reads no new table, but confirm the corpus is browsable.
    assert client.get("/.json").status_code == 200


# --- partial-migration: each new table missing on its own --------------------


@pytest.mark.parametrize(
    "drop",
    [
        ["verify_cache"],  # already covered by Fix C; pinned here against regression
        ["constituent_attribution"],
        ["manifests"],
        ["constituent_attribution", "manifests"],
    ],
    ids=["verify_cache", "constituent_attribution", "manifests", "manifest-pair"],
)
def test_partial_schema_corpus_serves_unsigned_no_500(tmp_path, drop):
    data_dir = tmp_path / ".skein"
    h = _seed_then_strip(data_dir, drop)
    _assert_serves_unsigned(data_dir, h)


# --- get_binding tolerance: a COVERED folio whose account_bindings table is gone
#
# An unsigned folio short-circuits at "no covering manifest" before the step-4
# binding check, so it can't exercise get_binding. To reach the binding query the
# folio must have a covering manifest with a passing signature; then dropping ONLY
# account_bindings must degrade to NOT VERIFIED ('unbound signer'), never 500.


def test_covered_folio_with_account_bindings_table_absent_degrades(tmp_path, monkeypatch):
    from skein import signing
    from skein.signing import MultiVerifyResult, VerifyResult, VerifyStatus

    from skein import profile, sign as sign_mod
    from skein.canon import manifest_descriptor_canonical_bytes
    from skein.identity import content_hash_for_bytes
    from skein.store import bundle_hash_for

    issuer, subject = "https://idp", "alice@example.com"

    def _signer(cb):
        preimage = profile.profiled_preimage(profile.CANON_PROFILE_MANIFEST_V1, cb)
        bundle = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1", bundles=["x"],
            canonical_bytes=preimage, canon_version=profile.CANON_PROFILE_MANIFEST_V1)
        return sign_mod.SignedResult(bundle=bundle, issuer=issuer, subject=subject)

    data_dir = tmp_path / ".skein"
    with Station(data_dir) as st:
        st.create_site("s", purpose="p", created_by="t")
        h = st.post("finding", "s", "T", "body here", created_by="t")
        ms = sign_mod.sign_manifest([h], _signer)
        d = ms["descriptor"]
        mh = content_hash_for_bytes(
            manifest_descriptor_canonical_bytes(d["root"], d["leaf_count"]))
        with st.store.transaction():
            st.store.add_manifest(d["root"], mh, json.dumps(d, sort_keys=True),
                                  json.dumps(ms["leaf_list"]), ms["signature_bundle"],
                                  issuer, subject, d["leaf_count"])
            st.store.add_constituent_attribution(h, "folio", d["root"], issuer, subject)
            # warm the signature cache so step 3 passes without a live verifier.
            st.store.verify_cache_put(
                mh, bundle_hash_for(ms["signature_bundle"]), "VERIFIED", issuer, subject)
        st.store.add_binding(issuer, subject, role="author")

    # Confirm the covered folio reads SIGNED while the binding table is present.
    monkeypatch.setattr(
        signing, "verify_multi",
        lambda cb, b: MultiVerifyResult(
            results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer=issuer, subject=subject)],
            overall=VerifyStatus.VERIFIED))
    with SkeinStore(data_dir, read_only=True) as ro:
        signed, _ = env_mod.folio_verdict(ro, h, ro.get_folio(h))
    assert signed.startswith("SIGNED"), signed

    # Now strip ONLY account_bindings — the step-4 binding query hits a missing
    # table and must degrade to NOT VERIFIED, not raise.
    surgeon = SkeinStore(data_dir)
    surgeon.conn.execute("DROP TABLE account_bindings")
    surgeon.conn.commit()
    surgeon.close()

    with SkeinStore(data_dir, read_only=True) as ro:
        row = ro.get_folio(h)
        verdict, identity = env_mod.folio_verdict(ro, h, row)
        assert verdict == "NOT VERIFIED — unbound signer", verdict
        assert identity is None
        # and the envelope-building path serves it without raising.
        env = env_mod.build_folio_envelope(ro, h, row=row)
        assert env["asserted"]["verdict"] == "NOT VERIFIED — unbound signer"


# --- the WRITE side: the missing-table tolerance must NOT bleed into a
# read_write store. A read_write store ALWAYS runs the schema migration on open
# (executescript), so a missing new-table there is a genuine schema fault — never
# a deploy-ordering race — and MUST raise rather than be silently masked. This is
# the deploy-fix: get_binding is ALSO the ingress authorization gate
# (ingress.py:154), where a swallowed "no such table" would degrade a schema fault
# into an ordinary 'unbound signer' rejection. The degrade is scoped to
# read_only=True; read_write re-raises.


def _open_rw_then_drop(data_dir, table):
    """Seed a corpus, open a READ-WRITE store (migration runs, every table
    present), then DROP ``table`` on that live connection so a subsequent query on
    the SAME store hits a missing table that the migration cannot have re-created."""
    with Station(data_dir) as st:
        st.create_site("s", purpose="p", created_by="t")
        st.post("finding", "s", "T", "body here", created_by="t")
    store = SkeinStore(data_dir)  # read_write: migration ran, table exists
    assert store.read_only is False
    store.conn.execute(f"DROP TABLE {table}")
    store.conn.commit()
    return store


def test_read_write_store_account_bindings_absent_raises(tmp_path):
    """A read_write store whose account_bindings table is gone must RAISE on
    get_binding — a missing table on the write store is a real schema fault."""
    store = _open_rw_then_drop(tmp_path / ".skein", "account_bindings")
    try:
        with pytest.raises(sqlite3.OperationalError) as ei:
            store.get_binding("https://idp", "alice@example.com")
        assert "no such table" in str(ei.value).lower()
    finally:
        store.close()


def test_read_write_store_constituent_attribution_absent_raises(tmp_path):
    """Same scoping for get_constituent_proof: a read_write store with the
    attribution table dropped must RAISE, not return None."""
    store = _open_rw_then_drop(tmp_path / ".skein", "constituent_attribution")
    try:
        with pytest.raises(sqlite3.OperationalError) as ei:
            store.get_constituent_proof("blake3:" + "0" * 64)
        assert "no such table" in str(ei.value).lower()
    finally:
        store.close()


def test_read_only_store_account_bindings_absent_degrades(tmp_path):
    """The contrast pin: the IDENTICAL on-disk corpus, opened read_only, degrades
    to None instead of raising — read app, un-migrated corpus, the tolerated case.
    Dropping account_bindings (only) leaves constituent_attribution present, so
    drop both to exercise both methods' degrade in read_only mode."""
    store = _open_rw_then_drop(tmp_path / ".skein", "account_bindings")
    store.conn.execute("DROP TABLE constituent_attribution")
    store.conn.commit()
    store.close()  # both tables now absent on disk
    with SkeinStore(tmp_path / ".skein", read_only=True) as ro:
        assert ro.read_only is True
        assert ro.get_binding("https://idp", "alice@example.com") is None
        assert ro.get_constituent_proof("blake3:" + "0" * 64) is None


def test_normal_migrated_corpus_unaffected_both_modes(tmp_path):
    """The fix must not touch a NORMAL migrated corpus: a present binding resolves
    on a read_write store AND on a read_only store, with no raise either way."""
    issuer, subject = "https://idp", "alice@example.com"
    data_dir = tmp_path / ".skein"
    with Station(data_dir) as st:
        st.create_site("s", purpose="p", created_by="t")
        st.store.add_binding(issuer, subject, role="author")
    with SkeinStore(data_dir) as rw:  # read_write
        assert rw.read_only is False
        assert rw.get_binding(issuer, subject).subject == subject
        assert rw.get_binding(issuer, "nobody@example.com") is None
    with SkeinStore(data_dir, read_only=True) as ro:
        assert ro.get_binding(issuer, subject).subject == subject
        assert ro.get_binding(issuer, "nobody@example.com") is None
