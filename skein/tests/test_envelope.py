"""Tests for the unified wire envelope (skein.envelope)."""

from __future__ import annotations

import pytest

from skein import signing
from skein import envelope as env_mod
from skein.station import Station


@pytest.fixture
def seeded(tmp_path):
    """Two linked folios in a site, a status thread, plus a cross-instance edge."""
    data_dir = tmp_path / ".skein"
    with Station(data_dir) as st:
        st.create_site("proj", purpose="the project")
        a = st.post(
            type="finding",
            site="proj",
            title="Finding A",
            content="# A\n\nbody A",
            created_by="alice",
            created_at="2026-01-01T00:00:00Z",
        )
        b = st.post(
            type="brief",
            site="proj",
            title="Brief B",
            content="body B",
            created_by="bob",
            created_at="2026-01-02T00:00:00Z",
        )
        st.store.save_thread(
            from_id=a, to_id=b, type="reference", created_at="2026-01-03T00:00:00Z"
        )
        st.store.save_thread(
            to_id=b, type="status", content="closed", created_at="2026-01-04T00:00:00Z"
        )
        # An edge to a peer that has no local folio (cross-instance).
        st.store.save_thread(
            from_id=a, to_id="sha256::" + "f" * 64, type="cites", created_at="2026-01-05T00:00:00Z"
        )
    store = SkeinNextStoreRO(data_dir)
    yield {"store": store.store, "a": a, "b": b}
    store.close()


class SkeinNextStoreRO:
    """A throwaway store handle. Writable so the signed-verdict tests can attach a
    bundle sidecar; envelope construction itself only ever reads."""

    def __init__(self, data_dir):
        from skein.store import SkeinNextStore

        self.store = SkeinNextStore(data_dir, check_same_thread=False)

    def close(self):
        self.store.close()


# --- validate_envelope ------------------------------------------------------


def test_validate_rejects_unknown_kind():
    with pytest.raises(ValueError):
        env_mod.validate_envelope({"kind": "nonsense", "stability": "stable"})


def test_validate_enforces_kind_stability():
    with pytest.raises(ValueError):
        env_mod.validate_envelope({"kind": "folio", "stability": "derived", "proof": {}})
    with pytest.raises(ValueError):
        env_mod.validate_envelope({"kind": "search", "stability": "stable"})


def test_validate_stable_needs_proof():
    with pytest.raises(ValueError):
        env_mod.validate_envelope({"kind": "folio", "stability": "stable", "proof": None})


def test_validate_derived_needs_as_of_and_no_proof():
    with pytest.raises(ValueError):
        env_mod.validate_envelope({"kind": "search", "stability": "derived", "proof": {"x": 1}})
    with pytest.raises(ValueError):
        env_mod.validate_envelope(
            {"kind": "search", "stability": "derived", "proof": None, "as_of": None}
        )


# --- build_folio_envelope ---------------------------------------------------


def test_folio_envelope_shape(seeded):
    env = env_mod.build_folio_envelope(seeded["store"], seeded["a"])
    assert env["schema"] == env_mod.SCHEMA
    assert env["kind"] == "folio" and env["stability"] == "stable"
    assert env["as_of"] is None
    assert env["body"] == {
        "type": "finding",
        "title": "Finding A",
        "content": "# A\n\nbody A",
        "created_at": "2026-01-01T00:00:00+00:00",
        "created_by": "alice",
    }
    assert env["proof"]["profile"] == env_mod.CANON_PROFILE
    assert env["proof"]["content_hash"] == seeded["a"]
    assert env["proof"]["signature_bundle"] is None  # unsigned -> integrity level
    assert env["links"]["self"] == f"/folio/{seeded['a']}"
    assert "bundle" not in env["links"]  # no bundle link when unsigned


def test_folio_asserted_status_and_site(seeded):
    env_a = env_mod.build_folio_envelope(seeded["store"], seeded["a"])
    env_b = env_mod.build_folio_envelope(seeded["store"], seeded["b"])
    assert env_a["asserted"]["status"] == "open"
    assert env_b["asserted"]["status"] == "closed"  # thread-derived
    assert env_a["asserted"]["site"]["slug"] == "proj"
    assert env_a["asserted"]["site"]["href"] == "/site/proj"


def test_folio_threads_split_by_direction(seeded):
    env_a = env_mod.build_folio_envelope(seeded["store"], seeded["a"])
    env_b = env_mod.build_folio_envelope(seeded["store"], seeded["b"])
    # a -> b reference is outgoing for a, incoming for b
    out_types = {(t["type"], t["address"]) for t in env_a["asserted"]["threads_out"]}
    assert ("reference", seeded["b"]) in out_types
    in_b = {(t["type"], t["address"]) for t in env_b["asserted"]["threads_in"]}
    assert ("reference", seeded["a"]) in in_b


def test_folio_threads_exclude_structural(seeded):
    env_b = env_mod.build_folio_envelope(seeded["store"], seeded["b"])
    # the status edge and the within (membership) edge must NOT appear as threads
    all_types = {
        t["type"] for t in env_b["asserted"]["threads_out"] + env_b["asserted"]["threads_in"]
    }
    assert "status" not in all_types and "within" not in all_types


def test_folio_cross_instance_peer_listed_with_raw_address(seeded):
    env_a = env_mod.build_folio_envelope(seeded["store"], seeded["a"])
    cites = [t for t in env_a["asserted"]["threads_out"] if t["type"] == "cites"]
    assert len(cites) == 1
    assert cites[0]["address"] == "sha256::" + "f" * 64
    assert cites[0]["title"] is None  # not held locally, no title


def test_folio_threads_exclude_alias_self_loop(seeded):
    # An edge to a legacy id that aliases back to THIS folio must not list the
    # folio as its own neighbour (the direct-hash self-loop is already excluded;
    # this is the alias-to-self case).
    store = seeded["store"]
    store.set_alias("finding-20260101-self", seeded["a"])
    store.save_thread(
        from_id=seeded["a"],
        to_id="finding-20260101-self",
        type="relates",
        created_at="2026-01-09T00:00:00Z",
    )
    env_a = env_mod.build_folio_envelope(store, seeded["a"])
    addresses = {t["address"] for t in env_a["asserted"]["threads_out"]}
    assert seeded["a"] not in addresses
    assert "finding-20260101-self" not in addresses


# --- lineage (thread-derived edit lineage) ----------------------------------


@pytest.fixture
def lineage_seeded(tmp_path):
    """A small lineage tree built from the four canonical lineage thread-types.

        R  <-supersedes-  C1  <-supersedes-  G
        R  <-forks-       C2

    so C1/C2 are siblings (co-children of R), C1's parent is R, G is C1's child.
    """
    data_dir = tmp_path / ".skein"
    with Station(data_dir) as st:
        st.create_site("proj", purpose="p")
        common = dict(site="proj", created_by="alice")
        r = st.post(type="finding", title="R", content="root", created_at="2026-01-01T00:00:00Z", **common)
        c1 = st.post(type="finding", title="C1", content="child one", created_at="2026-01-02T00:00:00Z", **common)
        c2 = st.post(type="finding", title="C2", content="child two", created_at="2026-01-03T00:00:00Z", **common)
        g = st.post(type="finding", title="G", content="grandchild", created_at="2026-01-04T00:00:00Z", **common)
        # The edit edge runs child -> parent (rev 4): from_id newer, to_id older.
        st.store.save_thread(from_id=c1, to_id=r, type="supersedes", created_at="2026-01-05T00:00:00Z")
        st.store.save_thread(from_id=c2, to_id=r, type="forks", created_at="2026-01-06T00:00:00Z")
        st.store.save_thread(from_id=g, to_id=c1, type="supersedes", created_at="2026-01-07T00:00:00Z")
        # A GENERIC (non-lineage) cross-reference out of C1, so the partition test
        # has something in the threads block that could (wrongly) cross over.
        st.store.save_thread(from_id=c1, to_id=c2, type="reference", created_at="2026-01-08T00:00:00Z")
    store = SkeinNextStoreRO(data_dir)
    yield {"store": store.store, "r": r, "c1": c1, "c2": c2, "g": g}
    store.close()


def test_lineage_empty_when_no_edit_edges(seeded):
    # The cross-reference corpus has reference/cites/status edges but no lineage
    # edge, so the lineage block degrades cleanly to empty.
    env = env_mod.build_folio_envelope(seeded["store"], seeded["a"])
    assert env["asserted"]["lineage"] == {"parents": [], "children": [], "siblings": []}
    assert env["asserted"]["superseded_by"] is None
    assert env["asserted"]["descendants"] == []


def test_lineage_parent_children_siblings(lineage_seeded):
    s = lineage_seeded
    env_c1 = env_mod.build_folio_envelope(s["store"], s["c1"])
    lin = env_c1["asserted"]["lineage"]
    # C1's single parent is R, via the outgoing supersedes edge.
    assert [p["address"] for p in lin["parents"]] == [s["r"]]
    assert lin["parents"][0]["type"] == "supersedes"
    # C1's child is G.
    assert {c["address"] for c in lin["children"]} == {s["g"]}
    # C1's sibling is C2 (co-child of R), and never C1 itself.
    assert {sib["address"] for sib in lin["siblings"]} == {s["c2"]}


def test_lineage_root_children_and_superseded_by(lineage_seeded):
    s = lineage_seeded
    env_r = env_mod.build_folio_envelope(s["store"], s["r"])
    lin = env_r["asserted"]["lineage"]
    assert lin["parents"] == []  # R is a root
    assert {c["address"] for c in lin["children"]} == {s["c1"], s["c2"]}
    # superseded_by is the INCOMING supersedes edge (the fork hatnote), not the fork.
    assert env_r["asserted"]["superseded_by"]["address"] == s["c1"]


def test_lineage_descendants_are_transitive(lineage_seeded):
    s = lineage_seeded
    env_r = env_mod.build_folio_envelope(s["store"], s["r"])
    desc = {d["address"] for d in env_r["asserted"]["descendants"]}
    assert desc == {s["c1"], s["c2"], s["g"]}  # children + grandchild


def test_lineage_edges_partitioned_out_of_threads(lineage_seeded):
    # A lineage-typed edge appears in lineage and NEVER also in the generic threads
    # block; a generic edge stays in threads. C1 has BOTH a supersedes (lineage) and
    # a reference (generic) edge, so the two-way split is actually exercised.
    s = lineage_seeded
    env_c1 = env_mod.build_folio_envelope(s["store"], s["c1"])
    thread_peers = {
        (t["type"], t["address"])
        for t in env_c1["asserted"]["threads_out"] + env_c1["asserted"]["threads_in"]
    }
    # the generic reference edge IS carried by the threads block
    assert ("reference", s["c2"]) in thread_peers
    # no lineage-typed edge leaked into threads
    assert {ty for ty, _ in thread_peers}.isdisjoint(env_mod._LINEAGE_THREADS)
    # the lineage peers (parent R, child G) appear nowhere in the threads block
    thread_addrs = {addr for _, addr in thread_peers}
    assert s["r"] not in thread_addrs and s["g"] not in thread_addrs
    lin = env_c1["asserted"]["lineage"]
    assert s["r"] in {p["address"] for p in lin["parents"]}
    assert s["g"] in {c["address"] for c in lin["children"]}


def test_lineage_descendants_cycle_safe(tmp_path):
    # X supersedes Y and Y supersedes X — a 2-cycle. The descendant walk's visited
    # set must terminate (not recurse forever) and list each node once.
    data_dir = tmp_path / ".skein"
    with Station(data_dir) as st:
        st.create_site("proj", purpose="p")
        x = st.post(type="finding", site="proj", title="X", content="x", created_by="a", created_at="2026-02-01T00:00:00Z")
        y = st.post(type="finding", site="proj", title="Y", content="y", created_by="a", created_at="2026-02-02T00:00:00Z")
        st.store.save_thread(from_id=x, to_id=y, type="supersedes", created_at="2026-02-03T00:00:00Z")
        st.store.save_thread(from_id=y, to_id=x, type="supersedes", created_at="2026-02-04T00:00:00Z")
    store = SkeinNextStoreRO(data_dir)
    try:
        env_x = env_mod.build_folio_envelope(store.store, x)
        desc = [d["address"] for d in env_x["asserted"]["descendants"]]
        assert sorted(desc) == sorted({y})  # Y once; X is the root, never re-added
    finally:
        store.close()


def test_lineage_descendants_respect_cap(lineage_seeded, monkeypatch):
    # The count bound truncates a large fork tree rather than crawling it all.
    monkeypatch.setattr(env_mod, "_DESCENDANTS_MAX", 1)
    s = lineage_seeded
    env_r = env_mod.build_folio_envelope(s["store"], s["r"])
    assert len(env_r["asserted"]["descendants"]) == 1  # R has 3 descendants, capped to 1


def test_lineage_descendants_cap_truncates_in_bfs_phase(tmp_path, monkeypatch):
    # Cap exercised in the BFS phase specifically: seeding must NOT hit the cap so
    # the walk enters BFS, then the cap truncates there. R has 2 direct children and
    # C1 has 2 grandchildren (4 descendants); cap 3 lets seeding add both children,
    # then the BFS drops the 4th. (test_lineage_descendants_respect_cap pins the
    # seeding phase; this pins the other phase.)
    data_dir = tmp_path / ".skein"
    with Station(data_dir) as st:
        st.create_site("proj", purpose="p")

        def mk(n, t):
            return st.post(type="finding", site="proj", title=n, content=n, created_by="a", created_at=t)

        r = mk("R", "2026-04-01T00:00:00Z")
        c1 = mk("C1", "2026-04-02T00:00:00Z")
        c2 = mk("C2", "2026-04-03T00:00:00Z")
        g1 = mk("G1", "2026-04-04T00:00:00Z")
        g2 = mk("G2", "2026-04-05T00:00:00Z")
        st.store.save_thread(from_id=c1, to_id=r, type="supersedes", created_at="2026-04-06T00:00:00Z")
        st.store.save_thread(from_id=c2, to_id=r, type="forks", created_at="2026-04-07T00:00:00Z")
        st.store.save_thread(from_id=g1, to_id=c1, type="supersedes", created_at="2026-04-08T00:00:00Z")
        st.store.save_thread(from_id=g2, to_id=c1, type="forks", created_at="2026-04-09T00:00:00Z")
    monkeypatch.setattr(env_mod, "_DESCENDANTS_MAX", 3)
    store = SkeinNextStoreRO(data_dir)
    try:
        desc = env_mod.build_folio_envelope(store.store, r)["asserted"]["descendants"]
        assert len(desc) == 3  # 4 descendants exist; the 4th is dropped in the BFS phase
        # both direct children survived seeding; only one grandchild made it
        addrs = {d["address"] for d in desc}
        assert {c1, c2}.issubset(addrs)
    finally:
        store.close()


def test_lineage_multiple_parents_and_sibling_dedup(tmp_path):
    # The round-2 shape change: a folio with MORE THAN ONE outgoing lineage edge.
    # M supersedes P1 and forks P2, so parents = [P1, P2] (nothing dropped). Siblings
    # are the co-children of both parents, deduped (SD is a child of both P1 and P2,
    # so it must appear once) and with M itself excluded.
    data_dir = tmp_path / ".skein"
    with Station(data_dir) as st:
        st.create_site("proj", purpose="p")

        def mk(n, t):
            return st.post(type="finding", site="proj", title=n, content=n, created_by="a", created_at=t)

        p1 = mk("P1", "2026-05-01T00:00:00Z")
        p2 = mk("P2", "2026-05-02T00:00:00Z")
        m = mk("M", "2026-05-03T00:00:00Z")
        s1 = mk("S1", "2026-05-04T00:00:00Z")
        s2 = mk("S2", "2026-05-05T00:00:00Z")
        sd = mk("SD", "2026-05-06T00:00:00Z")
        st.store.save_thread(from_id=m, to_id=p1, type="supersedes", created_at="2026-05-07T00:00:00Z")
        st.store.save_thread(from_id=m, to_id=p2, type="forks", created_at="2026-05-08T00:00:00Z")
        st.store.save_thread(from_id=s1, to_id=p1, type="supersedes", created_at="2026-05-09T00:00:00Z")
        st.store.save_thread(from_id=s2, to_id=p2, type="forks", created_at="2026-05-10T00:00:00Z")
        # SD is a co-child of BOTH parents -> must be deduped to a single sibling.
        st.store.save_thread(from_id=sd, to_id=p1, type="forks", created_at="2026-05-11T00:00:00Z")
        st.store.save_thread(from_id=sd, to_id=p2, type="supersedes", created_at="2026-05-12T00:00:00Z")
    store = SkeinNextStoreRO(data_dir)
    try:
        lin = env_mod.build_folio_envelope(store.store, m)["asserted"]["lineage"]
        assert {p["address"] for p in lin["parents"]} == {p1, p2}  # both parents kept
        sib_addrs = [sib["address"] for sib in lin["siblings"]]
        assert sorted(sib_addrs) == sorted({s1, s2, sd})  # deduped (SD once), M excluded
        assert m not in sib_addrs
    finally:
        store.close()


def test_lineage_remote_parent_lists_but_does_not_crash(tmp_path):
    # A folio whose lineage parent is held nowhere local: the parent is listed with
    # the raw endpoint and no title, and siblings can't be resolved (no crash).
    data_dir = tmp_path / ".skein"
    remote = "sha256::" + "f" * 64
    with Station(data_dir) as st:
        st.create_site("proj", purpose="p")
        child = st.post(type="finding", site="proj", title="Child", content="c", created_by="a", created_at="2026-03-01T00:00:00Z")
        st.store.save_thread(from_id=child, to_id=remote, type="supersedes", created_at="2026-03-02T00:00:00Z")
    store = SkeinNextStoreRO(data_dir)
    try:
        lin = env_mod.build_folio_envelope(store.store, child)["asserted"]["lineage"]
        assert [p["address"] for p in lin["parents"]] == [remote]
        assert lin["parents"][0]["title"] is None  # not held locally
        assert lin["siblings"] == []  # remote parent → its children can't be queried
    finally:
        store.close()


def test_lineage_json_link_present(seeded):
    env = env_mod.build_folio_envelope(seeded["store"], seeded["a"])
    assert env["links"]["json"] == f"/folio/{seeded['a']}.json"


# --- folio_verdict ----------------------------------------------------------


def test_verdict_unsigned(seeded):
    verdict, identity = env_mod.folio_verdict(
        seeded["store"], seeded["a"], seeded["store"].get_folio(seeded["a"])
    )
    assert verdict.startswith("UNSIGNED")
    assert identity is None


ISS, SUB = "https://idp", "alice@example.com"


def _cover_with_manifest(store, content_hash, *, issuer=ISS, subject=SUB, bind=True,
                         cache_status=None):
    """Cover a folio with a manifest (+ optional binding), the unified model's
    replacement for the per-folio signature sidecar. With ``cache_status`` a
    verify_cache row is written (a WARM cache); otherwise the read computes live."""
    import json

    from skein import sign as sign_mod, profile
    from skein.canon import manifest_descriptor_canonical_bytes
    from skein.identity import content_hash_for_bytes
    from skein.store import bundle_hash_for

    def signer(cb):
        preimage = profile.profiled_preimage(profile.CANON_PROFILE_MANIFEST_V1, cb)
        bundle = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1", bundles=["x"],
            canonical_bytes=preimage, canon_version=profile.CANON_PROFILE_MANIFEST_V1,
        )
        return sign_mod.SignedResult(bundle=bundle, issuer=issuer, subject=subject)

    ms = sign_mod.sign_manifest([content_hash], signer)
    descriptor = ms["descriptor"]
    root, lc = descriptor["root"], descriptor["leaf_count"]
    mh = content_hash_for_bytes(manifest_descriptor_canonical_bytes(root, lc))
    with store.transaction():
        store.add_manifest(root, mh, json.dumps(descriptor, sort_keys=True),
                           json.dumps(ms["leaf_list"]), ms["signature_bundle"],
                           issuer, subject, lc)
        store.add_constituent_attribution(content_hash, "folio", root, issuer, subject)
        if cache_status is not None:
            store.verify_cache_put(mh, bundle_hash_for(ms["signature_bundle"]),
                                   cache_status, issuer, subject)
    if bind:
        store.add_binding(issuer, subject, role="author")


def _verify_result(status, **kw):
    return signing.MultiVerifyResult(
        results=[signing.VerifyResult(status=status, **kw)], overall=status
    )


def _patch_verify(monkeypatch, status, **kw):
    monkeypatch.setattr(signing, "verify_multi", lambda cb, b: _verify_result(status, **kw))


def test_verdict_signed(seeded, monkeypatch):  # VC13 happy path
    _cover_with_manifest(seeded["store"], seeded["a"])
    _patch_verify(monkeypatch, signing.VerifyStatus.VERIFIED, issuer=ISS, subject=SUB)
    verdict, identity = env_mod.folio_verdict(
        seeded["store"], seeded["a"], seeded["store"].get_folio(seeded["a"])
    )
    assert verdict == "SIGNED — alice@example.com (verified)"
    assert identity == {"issuer": ISS, "subject": SUB}


def test_asserted_signer_is_none_when_unsigned(seeded):
    env = env_mod.build_folio_envelope(seeded["store"], seeded["a"])
    assert env["asserted"]["signer"] is None


def test_asserted_signer_carries_identity_when_signed(seeded, monkeypatch):
    _cover_with_manifest(seeded["store"], seeded["a"])
    _patch_verify(monkeypatch, signing.VerifyStatus.VERIFIED, issuer=ISS, subject=SUB)
    env = env_mod.build_folio_envelope(seeded["store"], seeded["a"])
    assert env["asserted"]["signer"] == {"issuer": ISS, "subject": SUB}


def test_verdict_unverifiable_is_not_invalid(seeded, monkeypatch):
    _cover_with_manifest(seeded["store"], seeded["a"])
    _patch_verify(monkeypatch, signing.VerifyStatus.OFFLINE_NO_TRUSTED_ROOT)
    verdict, _ = env_mod.folio_verdict(
        seeded["store"], seeded["a"], seeded["store"].get_folio(seeded["a"])
    )
    assert verdict.startswith("UNVERIFIED")
    assert "INVALID" not in verdict


def test_verdict_invalid(seeded, monkeypatch):
    _cover_with_manifest(seeded["store"], seeded["a"])
    _patch_verify(monkeypatch, signing.VerifyStatus.SIGNATURE_MISMATCH)
    verdict, _ = env_mod.folio_verdict(
        seeded["store"], seeded["a"], seeded["store"].get_folio(seeded["a"])
    )
    assert verdict.startswith("SIGNATURE INVALID")


def test_signed_folio_envelope_carries_bundle_and_link(seeded, monkeypatch):
    _cover_with_manifest(seeded["store"], seeded["a"])
    _patch_verify(monkeypatch, signing.VerifyStatus.VERIFIED, issuer=ISS, subject="sub")
    env = env_mod.build_folio_envelope(seeded["store"], seeded["a"])
    assert env["proof"]["signature_bundle"] is not None  # the covering manifest bundle
    assert env["proof"]["signature_bundle"]["identity_scheme"] == "sigstore-public-v1"
    assert env["links"]["bundle"] == f"/folio/{seeded['a']}/bundle"


# --- VC12/VC13/VC15: the four-step read verdict, binding is LIVE -------------


def test_constituent_verdict_derives_from_four_steps(seeded, monkeypatch):  # VC12
    store, a = seeded["store"], seeded["a"]
    _cover_with_manifest(store, a)
    # all four steps hold -> verified
    _patch_verify(monkeypatch, signing.VerifyStatus.VERIFIED, issuer=ISS, subject=SUB)
    v, _ = env_mod.folio_verdict(store, a, store.get_folio(a))
    assert v.startswith("SIGNED")
    # flip step 3 (signature) -> SIGNATURE INVALID
    _patch_verify(monkeypatch, signing.VerifyStatus.SIGNATURE_MISMATCH)
    v, _ = env_mod.folio_verdict(store, a, store.get_folio(a))
    assert v.startswith("SIGNATURE INVALID")
    # flip step 4 (binding) -> NOT VERIFIED, even with signature VERIFIED
    _patch_verify(monkeypatch, signing.VerifyStatus.VERIFIED, issuer=ISS, subject=SUB)
    store.revoke_binding(ISS, SUB)
    v, _ = env_mod.folio_verdict(store, a, store.get_folio(a))
    assert v == "NOT VERIFIED — revoked binding"


def test_folio_verdict_resolves_covering_manifest(seeded, monkeypatch):  # VC13
    store, a = seeded["store"], seeded["a"]
    _patch_verify(monkeypatch, signing.VerifyStatus.VERIFIED, issuer=ISS, subject=SUB)
    # unbound signer reads NOT VERIFIED — unbound signer (not SIGNED-with-a-color)
    _cover_with_manifest(store, a, bind=False)
    v, ident = env_mod.folio_verdict(store, a, store.get_folio(a))
    assert v == "NOT VERIFIED — unbound signer" and ident is None
    # bind -> SIGNED
    store.add_binding(ISS, SUB, role="author")
    v, ident = env_mod.folio_verdict(store, a, store.get_folio(a))
    assert v == "SIGNED — alice@example.com (verified)"
    # revoke -> NOT VERIFIED — revoked binding
    store.revoke_binding(ISS, SUB)
    v, ident = env_mod.folio_verdict(store, a, store.get_folio(a))
    assert v == "NOT VERIFIED — revoked binding" and ident is None


def test_read_verdict_binding_is_live_not_cached(seeded, monkeypatch):  # VC15
    store, a = seeded["store"], seeded["a"]
    # WARM cache: VERIFIED signature row present; signer bound at ingest.
    _cover_with_manifest(store, a, bind=True, cache_status="VERIFIED")
    # spy the verifier: on a warm cache it must NOT be consulted (step 3 elided)
    calls = []
    monkeypatch.setattr(
        signing, "verify_multi",
        lambda cb, b: calls.append(1) or _verify_result(signing.VerifyStatus.VERIFIED),
    )
    # revoke the signer with NO cache invalidation
    store.revoke_binding(ISS, SUB)
    v, _ = env_mod.folio_verdict(store, a, store.get_folio(a))
    assert v == "NOT VERIFIED — revoked binding"  # flips despite cached VERIFIED
    assert calls == []  # signature step (3) was served from the warm cache
    # symmetric: a never-bound signer reads unbound on a warm cache too
    b = seeded["b"]
    _cover_with_manifest(store, b, issuer="https://idp", subject="never@bound",
                         bind=False, cache_status="VERIFIED")  # warm
    v, _ = env_mod.folio_verdict(store, b, store.get_folio(b))
    assert v == "NOT VERIFIED — unbound signer"


# --- collection / error -----------------------------------------------------


def test_collection_envelope_is_derived():
    env = env_mod.build_collection_envelope("search", "/search?q=x", [])
    assert env["stability"] == "derived"
    assert env["proof"] is None
    assert env["as_of"]  # stamped


def test_error_envelope():
    env = env_mod.build_error_envelope(
        "not_found", "sha256::" + "0" * 64, origin="web::x.example::sha256::" + "0" * 64
    )
    assert env["kind"] == "error" and env["body"]["error"] == "not_found"
    assert env["links"]["origin"].startswith("web::")
    assert env["suggestion"]
