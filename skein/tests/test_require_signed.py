"""Thread E — the unified require_signed decision table (RS1-RS20), the accepted
replay stances (C40/C41/C42/C44), and the ingress totality (VM11/VM12).

ONE table over CONSTITUENTS (folios AND threads judged identically by membership).
OFF is manifest-blind AND binding-blind (byte-identical to pre-mesh); ON admits a
constituent iff it is a leaf under a manifest that verifies AND whose signer is
bound + non-revoked, gating the binding ONCE per manifest.
"""

from __future__ import annotations

import copy

import pytest

from skein import signing
from skein.signing import MultiVerifyResult, VerifyResult, VerifyStatus

from skein import profile, wire
from skein import sign as sign_mod
from skein.station import Station
from skein.ingress import ingest, _validate_shape, create_app


I = "https://accounts.google.com"
ALICE = "alice@example.com"


# --- signer / verifier fakes ------------------------------------------------


def _signer(issuer=I, subject=ALICE):
    def s(canonical_bytes):
        preimage = profile.profiled_preimage(profile.CANON_PROFILE_MANIFEST_V1, canonical_bytes)
        bundle = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1", bundles=["x"],
            canonical_bytes=preimage, canon_version=profile.CANON_PROFILE_MANIFEST_V1,
        )
        return sign_mod.SignedResult(bundle=bundle, issuer=issuer, subject=subject)
    return s


def _ok_verifier(canonical_bytes, bundle):
    return MultiVerifyResult(
        results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer=I, subject=ALICE)],
        overall=VerifyStatus.VERIFIED,
    )


def _bad_verifier(canonical_bytes, bundle):
    return MultiVerifyResult(
        results=[VerifyResult(status=VerifyStatus.SIGNATURE_MISMATCH)],
        overall=VerifyStatus.SIGNATURE_MISMATCH,
    )


class SpyBindings:
    def __init__(self, store):
        self._store = store
        self.get_binding_calls = 0

    def get_binding(self, issuer, subject):
        self.get_binding_calls += 1
        return self._store.get_binding(issuer, subject)


@pytest.fixture
def client(tmp_path):
    s = Station(tmp_path / "client" / ".skein")
    yield s
    s.close()


@pytest.fixture
def instance(tmp_path):
    s = Station(tmp_path / "instance" / ".skein")
    yield s
    s.close()


def _seed(station):
    station.create_site("specs", purpose="Public specs", created_by="t")
    f = station.post("finding", "specs", "Design Overview", "body", created_by="t")
    return f


def _signed_batch(client, signer=None, addresses_override=None):
    """A publish batch with a manifest_signature over its constituents."""
    from skein import publish as pub

    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    signer = signer or _signer()
    addrs = addresses_override or (
        [f["content_hash"] for f in batch["folios"]]
        + [t["thread_hash"] for t in batch["threads"]]
    )
    batch["manifest_signature"] = sign_mod.sign_manifest(addrs, signer)
    return batch


def _bind(instance, issuer=I, subject=ALICE, role="author"):
    instance.store.add_binding(issuer, subject, role=role,
                               vouched_by_issuer=issuer, vouched_by_subject=subject)


# --- RS1-RS12: the unified decision table -----------------------------------


def test_off_unsigned_constituent_accepts(client, instance):  # RS1
    _seed(client)
    batch = wire.build_batch(*__import__("skein.publish", fromlist=["x"]).collect_publish_set(client, site="specs"))
    ack = ingest(instance, batch, require_signed=False)
    assert len(ack["accepted"]) == 2 and ack["rejected"] == []


def test_on_no_manifest_rejects(client, instance):  # RS2
    _seed(client)
    from skein import publish as pub
    batch = wire.build_batch(*pub.collect_publish_set(client, site="specs"))
    ack = ingest(instance, batch, require_signed=True)
    assert ack["accepted"] == []
    assert all(r["reason"] == "no manifest" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_on_manifest_bad_signature_rejects(client, instance):  # RS3
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    ack = ingest(instance, batch, verifier=_bad_verifier, require_signed=True)
    assert ack["accepted"] == []
    assert all(r["reason"] == "manifest signature SIGNATURE_MISMATCH" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_on_manifest_signer_unbound_rejects(client, instance):  # RS4
    _seed(client)  # no binding added
    batch = _signed_batch(client)
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "unbound signer" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_on_manifest_signer_revoked_rejects(client, instance):  # RS5
    _seed(client)
    _bind(instance)
    instance.store.revoke_binding(I, ALICE)
    batch = _signed_batch(client)
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "revoked binding" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_on_leaf_not_in_manifest_rejects(client, instance):  # RS6
    _seed(client)
    _bind(instance)
    from skein import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    # manifest covers ONLY the site folio, not the finding
    site_hash = next(f["content_hash"] for f in batch["folios"]
                     if f["type"] == "site")
    batch["manifest_signature"] = sign_mod.sign_manifest([site_hash], _signer())
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    reasons = {r["content_hash"]: r["reason"] for r in ack["rejected"]}
    finding = next(f["content_hash"] for f in batch["folios"] if f["type"] == "finding")
    assert reasons[finding] == "not in manifest"
    # threads referencing the finding are also rejected (not in manifest)


def test_on_covered_bound_constituent_accepts(client, instance):  # RS7
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert len(ack["accepted"]) == 2
    for h in ack["accepted"]:
        proof = instance.store.get_constituent_proof(h)
        assert proof["subject"] == ALICE and proof["proof_missing"] is False


def test_on_manifest_binding_gated_once(client, instance):  # RS8
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    spy = SpyBindings(instance.store)
    ingest(instance, batch, verifier=_ok_verifier, require_signed=True, bindings=spy)
    assert spy.get_binding_calls == 1


def test_on_missing_account_bindings_table_raises_not_silent_reject(client, instance):
    """Deploy-fix #1: a SCHEMA FAULT on the ingress authz gate must surface, not be
    masked as an ordinary rejection. The read-path tolerance (a missing-table
    OperationalError -> None) is scoped to read_only stores; the ingress store is
    read_write, where a missing account_bindings table is a genuine fault. With a
    verified manifest from a signer that WOULD bind, dropping account_bindings must
    make ingest RAISE 'no such table' rather than silently reject as 'unbound
    signer'."""
    import sqlite3

    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    # The ingress store is read_write (Station default); drop the authz table on its
    # live connection so the get_binding gate hits a missing table.
    assert instance.store.read_only is False
    instance.store.conn.execute("DROP TABLE account_bindings")
    instance.store.conn.commit()

    with pytest.raises(sqlite3.OperationalError) as ei:
        ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert "no such table" in str(ei.value).lower()


def test_off_never_consults_bindings_or_manifest(client, instance):  # RS9
    _seed(client)
    batch = _signed_batch(client)  # carries a manifest, but OFF must ignore it
    spy = SpyBindings(instance.store)
    ack = ingest(instance, batch, verifier=_bad_verifier, require_signed=False, bindings=spy)
    assert spy.get_binding_calls == 0
    assert len(ack["accepted"]) == 2 and ack["rejected"] == []


def test_reject_reasons_pairwise_distinct():  # RS10
    from skein.ingress import _constituent_manifest_reject_reason as R
    authz = {"no manifest", "manifest signature SIGNATURE_MISMATCH",
             "unbound signer", "revoked binding", "not in manifest"}
    wire_integrity = {"hash mismatch", "invalid fields", "dangling endpoint"}
    manifest_wire = {"manifest malformed", "wrong kind", "unknown profile"}
    # the three manifest-failure buckets are DISJOINT
    assert R("no manifest") == "no manifest"
    assert R("manifest malformed") == "manifest malformed"  # bare, NOT wrapped
    assert R("wrong kind") == "wrong kind"
    assert R("unknown profile") == "unknown profile"
    assert R("SIGNATURE_MISMATCH") == "manifest signature SIGNATURE_MISMATCH"
    all_reasons = authz | wire_integrity | manifest_wire
    assert len(all_reasons) == 11  # all pairwise distinct


def test_attribution_is_manifest_signer_not_created_by_or_weaver(client, instance):  # RS11
    client.create_site("specs", purpose="p", created_by="t")
    f = client.post("finding", "specs", "T", "b", created_by="mallory")
    from skein import publish as pub
    _bind(instance)
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    addrs = [x["content_hash"] for x in batch["folios"]] + [t["thread_hash"] for t in batch["threads"]]
    batch["manifest_signature"] = sign_mod.sign_manifest(addrs, _signer())
    ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    proof = instance.store.get_constituent_proof(f)
    assert proof["issuer"] == I and proof["subject"] == ALICE  # NOT 'mallory'


def test_ingest_default_bindings_is_station_store(client, instance):  # RS12
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)  # no bindings=
    assert len(ack["accepted"]) == 2


# --- RS13-RS20: isolation, idempotency, OFF->ON attribution, slug gate -------


def test_per_item_savepoint_isolation(client, instance):  # RS13
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    # malform ONE folio body so it rejects, sibling still commits; HTTP-equivalent 200
    batch["folios"][0]["content_hash"] = "sha256::" + "0" * 64  # hash mismatch
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert len(ack["rejected"]) >= 1
    # the sibling folio still landed
    assert len(ack["accepted"]) >= 1


def test_old_manifest_replay_cannot_inject_new_leaf(client, instance):  # RS14
    _seed(client)
    _bind(instance)
    # manifest covers only the site folio + finding (publish 1)
    batch1 = _signed_batch(client)
    ingest(instance, batch1, verifier=_ok_verifier, require_signed=True)
    # a NEW folio not in the old manifest, replayed under the OLD manifest_signature
    l2 = client.post("finding", "specs", "New", "newbody", created_by="t")
    from skein import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    batch2 = wire.build_batch(folios, threads, slugs)
    batch2["manifest_signature"] = batch1["manifest_signature"]  # OLD manifest
    ack = ingest(instance, batch2, verifier=_ok_verifier, require_signed=True)
    rejected = {r["content_hash"]: r["reason"] for r in ack["rejected"]}
    assert rejected.get(l2) == "not in manifest"


def test_same_manifest_idempotent_republish(client, instance):  # RS15
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    again = ingest(instance, copy.deepcopy(batch), verifier=_ok_verifier, require_signed=True)
    assert again["accepted"] == []
    assert len(again["existing"]) == 2
    # one manifest row, attribution unchanged
    root = batch["manifest_signature"]["descriptor"]["root"]
    assert len(instance.store.get_manifest_proofs_by_root(root)) == 1


def test_mixed_and_single_kind_batches_gated(client, instance):  # RS16
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)  # site folio + finding + the 'within' thread
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    # folios AND the thread are all members -> all accept
    assert len(ack["accepted"]) == 2
    assert len(ack["threads"]["accepted"]) == 1  # the within edge is a leaf too
    # a thread-only batch with NO manifest under ON -> 'no manifest'
    from skein import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    thread_only = {"protocol": wire.PROTOCOL, "folios": [], "threads": threads,
                   "site_slugs": {}}
    ack2 = ingest(instance, thread_only, require_signed=True)
    assert all(r["reason"] == "no manifest" for r in ack2["threads"]["rejected"])


def test_slug_cross_author_overwrite_is_last_write_wins_v0(client, instance):  # RS19
    _seed(client)
    _bind(instance)
    ingest(instance, _signed_batch(client), verifier=_ok_verifier, require_signed=True)
    first_site = instance.store.resolve_slug("specs")
    # a second bound author publishes a DIFFERENT site folio under the same slug
    client2 = Station(client.store.data_dir.parent.parent / "client2" / ".skein")
    s2 = client2.create_site("specs", purpose="DIFFERENT", created_by="bob")
    from skein import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client2, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    addrs = [f["content_hash"] for f in batch["folios"]] + [t["thread_hash"] for t in batch["threads"]]
    batch["manifest_signature"] = sign_mod.sign_manifest(addrs, _signer())
    ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert instance.store.resolve_slug("specs") == s2  # last write wins
    assert s2 != first_site
    client2.close()


def test_ingress_exposes_no_binding_mutation_route():  # RS17
    app = create_app()
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not any("account" in p or "binding" in p for p in paths)


def test_off_then_on_existing_body_acquires_attribution(client, instance):  # RS20
    _seed(client)
    from skein import publish as pub
    # 1) ingest the finding under OFF (no manifest, no attribution)
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    off_batch = wire.build_batch(folios, threads, slugs)
    ingest(instance, off_batch, require_signed=False)
    finding = next(x["content_hash"] for x in off_batch["folios"] if x["type"] == "finding")
    assert instance.store.get_constituent_proof(finding) is None  # UNSIGNED
    # 2) re-deliver under ON inside a valid bound covering manifest
    _bind(instance)
    on_batch = _signed_batch(client)
    ack = ingest(instance, on_batch, verifier=_ok_verifier, require_signed=True)
    assert finding in ack["existing"]  # body de-dups
    proof = instance.store.get_constituent_proof(finding)  # but now attributed
    assert proof is not None and proof["subject"] == ALICE


def test_off_then_on_unverified_manifest_stays_unattributed(client, instance):  # RS20 negative
    _seed(client)
    from skein import publish as pub
    off_batch = wire.build_batch(*pub.collect_publish_set(client, site="specs"))
    ingest(instance, off_batch, require_signed=False)
    finding = next(x["content_hash"] for x in off_batch["folios"] if x["type"] == "finding")
    _bind(instance)
    on_batch = _signed_batch(client)
    ingest(instance, on_batch, verifier=_bad_verifier, require_signed=True)  # crypto fails
    assert instance.store.get_constituent_proof(finding) is None  # stays UNSIGNED


def test_on_slug_write_gated_by_site_folio_membership(client, instance):  # RS18
    _seed(client)
    _bind(instance)
    from skein import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    finding = next(f["content_hash"] for f in batch["folios"] if f["type"] == "finding")
    # manifest covers ONLY the finding, NOT the site folio -> site folio rejected
    batch["manifest_signature"] = sign_mod.sign_manifest([finding], _signer())
    ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert instance.store.resolve_slug("specs") is None  # no slug landed


# --- C40-C44: accepted replay stances ---------------------------------------


def test_subset_replay_of_own_manifest_accepted(client, instance):  # C41
    _seed(client)
    _bind(instance)
    from skein import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    full = wire.build_batch(folios, threads, slugs)
    addrs = [f["content_hash"] for f in full["folios"]] + [t["thread_hash"] for t in full["threads"]]
    ms = sign_mod.sign_manifest(addrs, _signer())
    site = next(f for f in full["folios"] if f["type"] == "site")
    finding = next(f for f in full["folios"] if f["type"] == "finding")
    # publish 1 delivers only the site folio (subset)
    b1 = {"protocol": wire.PROTOCOL, "folios": [site], "threads": [], "site_slugs": slugs,
          "manifest_signature": ms}
    ingest(instance, b1, verifier=_ok_verifier, require_signed=True)
    # later replay the SAME manifest delivering the finding -> accepts
    b2 = {"protocol": wire.PROTOCOL, "folios": [finding], "threads": [], "site_slugs": {},
          "manifest_signature": ms}
    ack = ingest(instance, b2, verifier=_ok_verifier, require_signed=True)
    assert finding["content_hash"] in ack["accepted"]


def test_revoked_between_publishes_re_gates_per_ingest(client, instance):  # C40
    _seed(client)
    _bind(instance)
    from skein import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    full = wire.build_batch(folios, threads, slugs)
    addrs = [f["content_hash"] for f in full["folios"]] + [t["thread_hash"] for t in full["threads"]]
    ms = sign_mod.sign_manifest(addrs, _signer())
    site = next(f for f in full["folios"] if f["type"] == "site")
    finding = next(f for f in full["folios"] if f["type"] == "finding")
    b1 = {"protocol": wire.PROTOCOL, "folios": [site], "threads": [], "site_slugs": slugs,
          "manifest_signature": ms}
    ingest(instance, b1, verifier=_ok_verifier, require_signed=True)
    # signer revoked AFTER publish 1
    instance.store.revoke_binding(I, ALICE)
    b2 = {"protocol": wire.PROTOCOL, "folios": [finding], "threads": [], "site_slugs": {},
          "manifest_signature": ms}
    ack = ingest(instance, b2, verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "revoked binding" for r in ack["rejected"])  # re-gated live


def test_cross_instance_replay_contained_by_per_instance_binding(client, tmp_path):  # C42
    _seed(client)
    batch = _signed_batch(client)
    # instance A: signer bound -> accept
    a = Station(tmp_path / "A" / ".skein")
    a.store.add_binding(I, ALICE, role="author")
    ackA = ingest(a, copy.deepcopy(batch), verifier=_ok_verifier, require_signed=True)
    assert len(ackA["accepted"]) == 2
    a.close()
    # instance B: signer NOT bound -> reject 'unbound signer'
    b = Station(tmp_path / "B" / ".skein")
    ackB = ingest(b, copy.deepcopy(batch), verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "unbound signer" for r in ackB["rejected"])
    b.close()


def test_reactivation_replay_accepted_on_rebind(client, instance):  # C44
    _seed(client)
    _bind(instance)
    from skein import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    full = wire.build_batch(folios, threads, slugs)
    addrs = [f["content_hash"] for f in full["folios"]] + [t["thread_hash"] for t in full["threads"]]
    ms = sign_mod.sign_manifest(addrs, _signer())
    site = next(f for f in full["folios"] if f["type"] == "site")
    finding = next(f for f in full["folios"] if f["type"] == "finding")
    ingest(instance, {"protocol": wire.PROTOCOL, "folios": [site], "threads": [],
                      "site_slugs": slugs, "manifest_signature": ms},
           verifier=_ok_verifier, require_signed=True)
    instance.store.revoke_binding(I, ALICE)
    b2 = {"protocol": wire.PROTOCOL, "folios": [finding], "threads": [], "site_slugs": {},
          "manifest_signature": ms}
    ack_revoked = ingest(instance, b2, verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "revoked binding" for r in ack_revoked["rejected"])
    # re-add (reactivate) and replay again -> accepts
    _bind(instance)
    ack_rebound = ingest(instance, copy.deepcopy(b2), verifier=_ok_verifier, require_signed=True)
    assert finding["content_hash"] in ack_rebound["accepted"]


# --- VM11/VM12: ingress totality over a hostile manifest --------------------


def test_ingress_non_dict_manifest_signature_passes_shape_gate(client):  # VM11
    # A present-but-non-dict manifest_signature is NOT a batch 400: the shape gate
    # lets it through so the verifier can return the per-constituent 'manifest
    # malformed' verdict (frozen contract; see test below). Only gross container
    # shapes (folios/threads/site_slugs) are this pre-gate's job.
    for bad in ("a-string", None, ["leaf"], 7):
        batch = {"protocol": wire.PROTOCOL, "folios": [], "threads": [],
                 "site_slugs": {}, "manifest_signature": bad}
        _validate_shape(batch)  # does not raise


@pytest.mark.parametrize("bad", [None, "a-string", ["sha256::" + "0" * 64]])
def test_ingress_non_dict_manifest_signature_per_verdict_not_500(instance, client, bad):  # VM11
    # ON posture: a present-but-non-dict manifest_signature reaches the verifier and
    # every folio rejects with the WIRE-INTEGRITY reason 'manifest malformed' — never
    # BatchShapeError, never a 500, never 'no manifest'. (The thread also rejects
    # 'manifest malformed': under a globally-failed manifest the thread loop emits the
    # manifest reason BEFORE present(), so constituents are judged identically. The
    # assertion below covers the folios; the thread is the surface of FIX 2's tests.)
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    batch["manifest_signature"] = bad
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert ack["rejected"], "expected the folios to be rejected"
    assert all(r["reason"] == "manifest malformed" for r in ack["rejected"])
    assert not ack["accepted"] and not ack["threads"]["accepted"]
    assert instance.store.count_folios() == 0


def test_ingress_absent_manifest_key_is_no_manifest(instance, client):  # VM11
    # A WHOLLY ABSENT manifest_signature key is the ABSENCE bucket: 'no manifest',
    # distinct from a present-but-malformed value.
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    del batch["manifest_signature"]
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert ack["rejected"], "expected the folios to be rejected"
    assert all(r["reason"] == "no manifest" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_ingress_non_dict_manifest_signature_off_unaffected(instance, client):  # VM11
    # OFF posture is manifest-blind: a present-but-non-dict manifest_signature does
    # not raise and does not block admission on integrity + closed-graph alone.
    _seed(client)
    batch = _signed_batch(client)
    batch["manifest_signature"] = None
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=False)
    assert ack["accepted"], "OFF admits on integrity alone, manifest ignored"
    assert not ack["rejected"]


def test_ingress_malformed_manifest_per_verdict_not_500(client, instance):  # VM12
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    # tamper leaf_list so root no longer recomputes -> 'manifest malformed' per item
    batch["manifest_signature"]["leaf_list"] = ["sha256::" + "9" * 64]
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert all(r["reason"] == "manifest malformed" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


# --- FIX 1 (fell p3-r2 #1): OFF is manifest-blind — never invoke the verifier --


class SpyVerifier:
    """Wraps a verifier and counts how many times it is invoked."""

    def __init__(self, inner=_ok_verifier):
        self._inner = inner
        self.calls = 0

    def __call__(self, canonical_bytes, bundle):
        self.calls += 1
        return self._inner(canonical_bytes, bundle)


def _raising_verifier(canonical_bytes, bundle):
    raise AssertionError("verify_wire_manifest must not be called under OFF")


def test_off_never_invokes_verifier_with_present_manifest(client, instance):  # FIX 1 (a)
    # OFF + a present, well-formed, VALID manifest_signature: the verifier is never
    # called and every constituent admits on integrity + closed-graph alone.
    _seed(client)
    batch = _signed_batch(client)  # carries a valid manifest_signature
    spy = SpyVerifier()
    ack = ingest(instance, batch, verifier=spy, require_signed=False)
    assert spy.calls == 0
    assert len(ack["accepted"]) == 2 and ack["rejected"] == []
    assert len(ack["threads"]["accepted"]) == 1 and ack["threads"]["rejected"] == []


def test_off_with_raising_verifier_still_publishes(client, instance):  # FIX 1 (b)
    # OFF must not even touch the verifier: a verifier that RAISES on any call would
    # abort the publish if invoked. The publish still succeeds, proving OFF never
    # invokes it (byte-identical to the pre-mesh posture).
    _seed(client)
    batch = _signed_batch(client)  # present, well-formed, valid manifest_signature
    ack = ingest(instance, batch, verifier=_raising_verifier, require_signed=False)
    assert len(ack["accepted"]) == 2 and ack["rejected"] == []
    assert len(ack["threads"]["accepted"]) == 1


def test_on_invokes_verifier_exactly_once(client, instance):  # FIX 1 (c)
    # ON computes the manifest verdict ONCE: the verifier fires exactly once for the
    # whole batch, regardless of constituent count.
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)
    spy = SpyVerifier()
    ack = ingest(instance, batch, verifier=spy, require_signed=True)
    assert spy.calls == 1
    assert len(ack["accepted"]) == 2 and len(ack["threads"]["accepted"]) == 1


# --- FIX 2 (fell p3-r2 #2): global manifest failure rejects threads identically,
# --- with the manifest/bind reason, BEFORE the per-thread present() check --------


def test_on_malformed_manifest_rejects_every_constituent_identically(client, instance):  # FIX 2 (a)
    # ON + a globally-malformed manifest (non-dict): EVERY constituent — both folios
    # AND the within-thread between them — rejects bare 'manifest malformed'. The
    # thread no longer reports 'dangling endpoint'; nothing is stored.
    _seed(client)
    _bind(instance)
    batch = _signed_batch(client)  # site folio + finding folio + the within thread
    batch["manifest_signature"] = 7  # non-dict -> WIRE-INTEGRITY 'manifest malformed'
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    all_rejected = ack["rejected"] + ack["threads"]["rejected"]
    assert len(ack["rejected"]) == 2 and len(ack["threads"]["rejected"]) == 1
    assert all(r["reason"] == "manifest malformed" for r in all_rejected)
    assert not ack["accepted"] and not ack["threads"]["accepted"]
    assert instance.store.count_folios() == 0


def test_on_valid_manifest_over_thread_not_endpoints_keeps_dangling(client, instance):  # FIX 2 (b)
    # ON + a VALID bound manifest covering the thread's hash but NOT its endpoint
    # folios: the endpoint folios reject 'not in manifest', and the thread (itself a
    # member) reaches present() and rejects 'dangling endpoint' — present() is
    # preserved under a verified+bound manifest.
    _seed(client)
    _bind(instance)
    from skein import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    assert batch["threads"], "need a within thread to exercise present()"
    thread_hash = batch["threads"][0]["thread_hash"]
    batch["manifest_signature"] = sign_mod.sign_manifest([thread_hash], _signer())
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    assert ack["rejected"] and all(r["reason"] == "not in manifest" for r in ack["rejected"])
    assert ack["threads"]["rejected"]
    assert all(r["reason"] == "dangling endpoint" for r in ack["threads"]["rejected"])
    assert instance.store.count_folios() == 0


def test_on_unbound_signer_rejects_thread_with_bind_reason(client, instance):  # FIX 2 (c)
    # ON + a VALID manifest whose signer is UNBOUND: the global bind failure rejects
    # every constituent (folios AND the thread) with 'unbound signer' BEFORE present()
    # — the thread does NOT report 'dangling endpoint'.
    _seed(client)  # no binding added
    batch = _signed_batch(client)  # site folio + finding folio + within thread
    ack = ingest(instance, batch, verifier=_ok_verifier, require_signed=True)
    all_rejected = ack["rejected"] + ack["threads"]["rejected"]
    assert len(ack["rejected"]) == 2 and len(ack["threads"]["rejected"]) == 1
    assert all(r["reason"] == "unbound signer" for r in all_rejected)
    assert instance.store.count_folios() == 0
