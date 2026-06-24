"""Tests for the client->instance publish path (prototype, unsigned).

Exercises the seam directly — ``collect_publish_set`` on a client station and
``ingest`` on an instance station — without standing up the HTTP server, since
the wire is just JSON-able dicts in between. The route in ``ingress.py`` is a
thin shell over ``ingest`` plus the protocol-header check.
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from skein.station import Station
from skein.ingress import ingest, create_app
from skein import publish as pub_mod
from skein.publish import collect_publish_set, PublishError, post_batch, canonical_instance
from skein import wire


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


def _seed_site(station):
    station.create_site("specs", purpose="Public specs", created_by="tester")
    f1 = station.post("finding", "specs", "Design Overview", "rev-5 body", created_by="tester")
    f2 = station.post("finding", "specs", "Encoji", "encoji body", created_by="tester")
    station.set_status(f1, "closed", by="tester")
    return f1, f2


def test_publish_over_leaf_cap_fails_before_signing_ceremony(client, monkeypatch):
    # A publish whose distinct constituent count exceeds the manifest leaf cap is
    # refused BEFORE the irreversible Sigstore ceremony — so no Fulcio cert / Rekor
    # entry is burned on a batch the ingress would reject as 'manifest malformed'.
    from skein import sign as _sign

    _seed_site(client)  # site + 2 findings = 3 distinct constituents
    monkeypatch.setattr(_sign, "MAX_LEAVES", 2)  # force the corpus over the cap

    def _never_called_signer(canonical_bytes):
        raise AssertionError("signer must not run when the batch is over the cap")

    with pytest.raises(PublishError) as ei:
        pub_mod.publish(
            client, "http://127.0.0.1:9", site="specs",
            signer=_never_called_signer, dry_run=False,
        )
    msg = str(ei.value)
    assert "over the 2-leaf manifest cap" in msg
    assert "transparency-log" in msg  # names the reason the fast-fail exists


def test_publish_round_trip_carries_folios_threads_and_status(client, instance):
    f1, f2 = _seed_site(client)
    folios, threads, slugs = collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)

    ack = ingest(instance, batch)

    # site folio + two findings land as new; nothing rejected.
    assert len(ack["accepted"]) == 3
    assert ack["rejected"] == []
    assert ack["threads"]["rejected"] == []
    # the site slug was registered, so membership reads back by slug.
    members = {f["content_hash"] for f in instance.folios_in_site("specs")}
    assert members == {f1, f2}
    # the status thread travelled: the instance re-derives 'closed' for f1.
    assert instance.status_of(f1) == "closed"
    assert instance.status_of(f2) == "open"


def test_reingest_is_idempotent(client, instance):
    _seed_site(client)
    folios, threads, slugs = collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)

    ingest(instance, batch)
    second = ingest(instance, batch)

    assert second["accepted"] == []
    assert len(second["existing"]) == 3
    assert instance.store.count_folios() == 3


def test_tampered_folio_is_rejected_on_hash_mismatch(client, instance):
    _seed_site(client)
    folios, threads, slugs = collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    # Alter a body without updating its claimed content_hash.
    finding = next(f for f in batch["folios"] if f["type"] == "finding")
    finding["content"] = "tampered in transit"

    ack = ingest(instance, batch)

    assert any(r["content_hash"] == finding["content_hash"] for r in ack["rejected"])
    assert finding["content_hash"] not in ack["accepted"]
    assert instance.store.get_folio(finding["content_hash"]) is None


def test_dangling_thread_is_refused_by_instance(client, instance):
    """An edge whose endpoints did not land is dropped (graph stays closed)."""
    _seed_site(client)
    folios, threads, slugs = collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    # Forge a well-hashed edge to a folio the instance never received.
    ghost = "sha256::" + "a" * 64
    real = batch["folios"][0]["content_hash"]
    bad = {
        "from_id": real,
        "to_id": ghost,
        "type": "reference",
        "weaver": "tester",
        "created_at": "2026-06-01T00:00:00+00:00",
        "content": None,
    }
    bad["thread_hash"] = wire.recompute_thread_hash(bad)
    batch["threads"].append(bad)

    ack = ingest(instance, batch)

    assert any(r["thread_hash"] == bad["thread_hash"] for r in ack["threads"]["rejected"])


def test_dropped_edge_is_convergent_on_later_publish(client, instance):
    """An edge dropped as dangling ships once its other endpoint is published."""
    inst = "http://instance.example"
    client.create_site("specs", purpose="p", created_by="t")
    a = client.post("finding", "specs", "A", "body a", created_by="t")
    b = client.post("finding", "specs", "B", "body b", created_by="t")
    # A references B; both are in 'specs' but we publish them one at a time.
    client.store.save_thread(
        from_id=a,
        to_id=b,
        type="reference",
        weaver="t",
        content=None,
        created_at="2026-06-01T00:00:00+00:00",
    )

    # Publish A alone: the A->B reference dangles (B not yet on instance) -> dropped.
    fa, ta, sa = collect_publish_set(client, refs=[a], instance=inst)
    assert all(t["type"] != "reference" for t in ta)
    ingest(instance, wire.build_batch(fa, ta, sa))
    for f in fa:
        client.record_published(f["content_hash"], inst)

    # Publish B: now A is already on the instance, so the A->B edge becomes eligible.
    fb, tb, sb = collect_publish_set(client, refs=[b], instance=inst)
    assert any(t["type"] == "reference" for t in tb)
    ack = ingest(instance, wire.build_batch(fb, tb, sb))
    assert ack["threads"]["rejected"] == []
    # the reference edge now lives on the instance.
    refs = [t for t in instance.store.get_threads(from_id=a, type="reference")]
    assert any(t["to_id"] == b for t in refs)


def test_published_threads_are_not_carried(client):
    """Client publish-state bookkeeping never enters the wire batch."""
    f1, _ = _seed_site(client)
    client.record_published(f1, "http://instance.example")
    folios, threads, _ = collect_publish_set(client, site="specs")
    assert all(t["type"] != Station.PUBLISHED_THREAD for t in threads)


def test_publish_state_derivation_and_dedup(client):
    f1, f2 = _seed_site(client)
    assert client.publish_state_of(f1) == "local"
    client.record_published(f1, "http://a.example")
    client.record_published(f1, "http://a.example")  # re-publish, fresh timestamp
    client.record_published(f1, "http://b.example")
    assert client.publish_state_of(f1) == "published"
    assert client.publish_state_of(f2) == "local"
    # dedup: a is listed once despite two publishes; order preserved.
    assert client.published_instances(f1) == ["http://a.example", "http://b.example"]


def test_post_batch_wraps_transport_failure():
    with pytest.raises(PublishError):
        post_batch("http://127.0.0.1:1/", {"protocol": wire.PROTOCOL}, timeout=1.0)


def test_collect_by_refs_pulls_in_site_folio(client):
    f1, _ = _seed_site(client)
    folios, threads, slugs = collect_publish_set(client, refs=[f1])
    types = {f["type"] for f in folios}
    # the finding and its site folio both come along so membership reconstructs.
    assert "site" in types and "finding" in types
    assert "specs" in slugs.values()


# --- fell-r1 follow-ups -----------------------------------------------------


def test_created_at_non_normalized_survives_round_trip(instance):
    """A Z-suffixed/whole-second timestamp hashes consistently and is accepted.

    identity.py calls created_at "the one place identity can silently break";
    this pins that a non-normalized wire value still re-hashes to its address.
    """
    wf = {
        "type": "finding",
        "title": "T",
        "content": "body",
        "created_at": "2026-06-01T00:00:00Z",
        "created_by": "t",
    }
    wf["content_hash"] = wire.recompute_folio_hash(wf)
    assert wire.folio_hash_ok(wf)
    ack = ingest(
        instance, {"protocol": wire.PROTOCOL, "folios": [wf], "threads": [], "site_slugs": {}}
    )
    assert wf["content_hash"] in ack["accepted"]
    assert instance.store.get_folio(wf["content_hash"]) is not None


def test_convergence_reverse_order_b_then_a(client, instance):
    """The incident-edge scan finds the edge via from_id too (publish B then A)."""
    inst = "http://instance.example"
    client.create_site("specs", purpose="p", created_by="t")
    a = client.post("finding", "specs", "A", "a", created_by="t")
    b = client.post("finding", "specs", "B", "b", created_by="t")
    client.store.save_thread(
        from_id=a,
        to_id=b,
        type="reference",
        weaver="t",
        content=None,
        created_at="2026-06-01T00:00:00+00:00",
    )

    fb, tb, sb = collect_publish_set(client, refs=[b], instance=inst)
    assert all(t["type"] != "reference" for t in tb)  # A not on instance yet
    ingest(instance, wire.build_batch(fb, tb, sb))
    for f in fb:
        client.record_published(f["content_hash"], inst)

    fa, ta, sa = collect_publish_set(client, refs=[a], instance=inst)
    assert any(t["type"] == "reference" for t in ta)  # B already there -> ships
    ack = ingest(instance, wire.build_batch(fa, ta, sa))
    assert ack["threads"]["rejected"] == []
    assert any(t["to_id"] == b for t in instance.store.get_threads(from_id=a, type="reference"))


def test_actor_id_endpoint_edge_is_dropped(client):
    """An edge to a bare actor id (not a folio) is not eligible to travel."""
    f1, _ = _seed_site(client)
    client.store.save_thread(
        from_id=f1,
        to_id="agent:somebody",
        type="assignment",
        weaver="t",
        content=None,
        created_at="2026-06-01T00:00:00+00:00",
    )
    _, threads, _ = collect_publish_set(client, site="specs")
    assert all(t["type"] != "assignment" for t in threads)


def test_canonical_instance_collapses_variants():
    assert canonical_instance("http://Host:9101/") == canonical_instance("http://host:9101")
    assert canonical_instance("http://host:80/") == "http://host"
    assert canonical_instance("https://host:443") == "https://host"
    assert canonical_instance("http://host:9101/ingress/") == "http://host:9101/ingress"


def test_canonical_instance_handles_ipv6_literal():
    import urllib.parse

    canon = canonical_instance("http://[::1]:9101/")
    assert canon == "http://[::1]:9101"
    # the result must reparse (a dropped bracket would make .port throw).
    assert urllib.parse.urlsplit(canon).port == 9101


def test_unhashable_created_at_is_rejected_not_raised(instance):
    """A folio whose created_at can't be normalized is rejected, never raised."""
    bad = {
        "content_hash": "sha256::" + "0" * 64,
        "type": "finding",
        "title": "T",
        "content": "b",
        "created_at": 12345,
        "created_by": "t",  # int, unparseable
    }
    ack = ingest(
        instance, {"protocol": wire.PROTOCOL, "folios": [bad], "threads": [], "site_slugs": {}}
    )
    assert any(r["reason"] == "invalid fields" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_canon_rejected_body_is_rejected_not_raised(instance):
    """A body knurl's canon rejects (a noncharacter code point) raises CanonError,
    which is NOT a ValueError/TypeError — the old narrow catch would have 500'd
    the ingress. The total catch rejects it cleanly (zr29 MEDIUM #6)."""
    bad = {
        "content_hash": "sha256::" + "0" * 64,
        "type": "finding",
        "title": "x﷐y",  # U+FDD0 noncharacter -> CanonError
        "content": "b",
        "created_at": "2026-01-01T00:00:00Z",
        "created_by": "t",
    }
    ack = ingest(
        instance, {"protocol": wire.PROTOCOL, "folios": [bad], "threads": [], "site_slugs": {}}
    )
    assert any(r["reason"] == "invalid fields" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


def test_route_does_not_500_on_unhashable_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("SKEIN_DATA_DIR", str(tmp_path / "r2" / ".skein"))
    tc = TestClient(create_app(), raise_server_exceptions=False)
    r = tc.post(
        "/publish/v0/folios",
        json={
            "protocol": wire.PROTOCOL,
            "folios": [
                {
                    "content_hash": "sha256::x",
                    "type": "finding",
                    "title": "T",
                    "content": "b",
                    "created_at": 12345,
                    "created_by": "t",
                }
            ],
            "threads": [],
            "site_slugs": {},
        },
    )
    assert r.status_code == 200
    assert any(rj["reason"] == "invalid fields" for rj in r.json()["rejected"])


def test_publish_canonicalizes_instance_and_surfaces_thread_rejections(client, monkeypatch):
    """publish() records the canonical instance id and reports refused edges."""
    f1, _ = _seed_site(client)

    def fake_post(instance, batch, timeout=30.0):
        hashes = [f["content_hash"] for f in batch["folios"]]
        return {
            "protocol": wire.PROTOCOL,
            "accepted": hashes,
            "existing": [],
            "rejected": [],
            "threads": {
                "accepted": [],
                "existing": [],
                "rejected": [{"thread_hash": "sha256::x", "reason": "dangling endpoint"}],
            },
        }

    monkeypatch.setattr(pub_mod, "post_batch", fake_post)
    result = pub_mod.publish(client, "http://127.0.0.1:9101/", site="specs", by="t")

    assert result["instance"] == "http://127.0.0.1:9101"
    assert result["threads_rejected"] == [
        {"thread_hash": "sha256::x", "reason": "dangling endpoint"}
    ]
    # the trailing-slash variant is recorded as one canonical instance.
    assert client.published_instances(f1) == ["http://127.0.0.1:9101"]


def test_thread_reports_existing_on_reingest(client, instance):
    _seed_site(client)
    batch = wire.build_batch(*collect_publish_set(client, site="specs"))
    ingest(instance, batch)
    second = ingest(instance, batch)
    assert second["threads"]["accepted"] == []
    assert len(second["threads"]["existing"]) >= 1


def test_ingress_route_round_trip_and_guards(client, tmp_path, monkeypatch):
    """The HTTP route: 200 happy path, 400 on bad protocol, 400 on bad shape."""
    _seed_site(client)
    batch = wire.build_batch(*collect_publish_set(client, site="specs"))
    monkeypatch.setenv("SKEIN_DATA_DIR", str(tmp_path / "routed" / ".skein"))
    tc = TestClient(create_app())

    ok = tc.post("/publish/v0/folios", json=batch)
    assert ok.status_code == 200 and len(ok.json()["accepted"]) == 3

    bad_proto = tc.post("/publish/v0/folios", json={"protocol": "nope"})
    assert bad_proto.status_code == 400

    bad_shape = tc.post("/publish/v0/folios", json={"protocol": wire.PROTOCOL, "folios": "x"})
    assert bad_shape.status_code == 400


# --- harden B: non-string canonical scalars break the body<->address binding --
#
# A non-str scalar (int/bool) serializes through knurl as a JSON number/bool, so
# it slips past the wire integrity gate — but the store columns are TEXT, so
# SQLite coerces 99->"99" / True->1 on insert and the served body re-hashes to a
# DIFFERENT address than ingest bound. The canon layer now raises CanonError on
# any non-str/non-None canonical field, which the gate maps to "invalid fields".


@pytest.mark.parametrize("field", ["type", "title", "content", "created_by"])
@pytest.mark.parametrize("bad_value", [99, True])
def test_non_string_folio_field_is_rejected_invalid_fields(instance, field, bad_value):
    wf = {
        "content_hash": "sha256::" + "0" * 64,
        "type": "finding",
        "title": "T",
        "content": "b",
        "created_at": "2026-01-01T00:00:00Z",
        "created_by": "t",
    }
    wf[field] = bad_value  # int/bool — passes knurl, breaks TEXT-affinity binding
    ack = ingest(
        instance, {"protocol": wire.PROTOCOL, "folios": [wf], "threads": [], "site_slugs": {}}
    )
    assert any(r["reason"] == "invalid fields" for r in ack["rejected"])
    assert instance.store.count_folios() == 0


@pytest.mark.parametrize("field", ["from_id", "to_id", "type", "weaver", "content"])
@pytest.mark.parametrize("bad_value", [99, True])
def test_non_string_thread_field_is_rejected_invalid_fields(instance, field, bad_value):
    real = "sha256::" + "1" * 64
    wt = {
        "thread_hash": "sha256::" + "0" * 64,
        "from_id": real,
        "to_id": real,
        "type": "reference",
        "weaver": "t",
        "created_at": "2026-01-01T00:00:00Z",
        "content": "b",
    }
    wt[field] = bad_value
    ack = ingest(
        instance, {"protocol": wire.PROTOCOL, "folios": [], "threads": [wt], "site_slugs": {}}
    )
    assert any(r["reason"] == "invalid fields" for r in ack["threads"]["rejected"])
    assert instance.store.count_threads() == 0


def test_accepted_folio_and_thread_bodies_rehash_to_served_address(client, instance):
    """Property: every ACCEPTED constituent's stored body re-hashes to its address.

    This is the binding harden B protects — the served body and the address it is
    keyed under can never disagree, because a body that wouldn't re-hash is
    refused at ingest."""
    client.create_site("specs", purpose="p", created_by="t")
    a = client.post("finding", "specs", "A", "body a", created_by="t")
    b = client.post("finding", "specs", "B", "body b", created_by="t")
    client.store.save_thread(
        from_id=a, to_id=b, type="reference", weaver="t", content=None,
        created_at="2026-06-01T00:00:00+00:00",
    )
    batch = wire.build_batch(*collect_publish_set(client, site="specs"))
    ingest(instance, batch)

    for row in instance.store.list_folios():
        served = wire.folio_to_wire(row)
        assert wire.recompute_folio_hash(served) == row["content_hash"]
    threads = instance.store.conn.execute("SELECT * FROM threads").fetchall()
    assert threads  # the reference edge landed (both endpoints present)
    for row in threads:
        served = wire.thread_to_wire(dict(row))
        assert wire.recompute_thread_hash(served) == row["thread_hash"]
