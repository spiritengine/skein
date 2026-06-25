"""Ingress HTTP surface — the public /publish/v0/folios endpoint hardening.

These exercise the route itself (body byte-cap, shape rejects, the async +
threadpool happy path), distinct from test_require_signed.py which drives the
pure ``ingest`` function. The byte cap is the DoS guard against a hostile client
forcing unbounded parse/memory work before any verification runs (defense-in-
depth atop the fronting proxy's client_max_body_size).
"""

from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from skein import wire
from skein.ingress import create_app, MAX_BATCH_BYTES, ENV_DATA_DIR
from skein.station import Station


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """A fresh instance data dir wired to the endpoint via the env (require_signed
    stays OFF — these are surface tests, not gate tests)."""
    d = tmp_path / "instance" / ".skein"
    Station(d).close()  # materialize the store
    monkeypatch.setenv(ENV_DATA_DIR, str(d))
    monkeypatch.delenv("SKEIN_REQUIRE_SIGNED", raising=False)
    return d


@pytest.fixture
def app_client(data_dir):
    return TestClient(create_app())


def _small_unsigned_batch(tmp_path):
    """A minimal valid unsigned publish batch from a throwaway client station."""
    from skein import publish as pub

    c = Station(tmp_path / "src" / ".skein")
    try:
        c.create_site("specs", purpose="Public specs", created_by="t")
        c.post("finding", "specs", "Design Overview", "body", created_by="t")
        folios, threads, slugs = pub.collect_publish_set(c, site="specs")
        return wire.build_batch(folios, threads, slugs)
    finally:
        c.close()


def test_oversized_body_rejected_413_before_parse(app_client):
    # A body over the byte cap is rejected WHOLE (413) — the content-length
    # fast-path fires before the body is buffered or JSON-parsed.
    payload = b'{"protocol":"' + b"x" * (MAX_BATCH_BYTES + 1) + b'"}'
    r = app_client.post(
        "/publish/v0/folios", content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413
    assert "too large" in r.json()["error"]


def test_body_at_cap_is_not_rejected_for_size(app_client):
    # Exactly at the cap is allowed past the size guard (it then fails on JSON/shape,
    # not 413) — proves the boundary is inclusive and the cap isn't off-by-one.
    payload = b"y" * MAX_BATCH_BYTES  # not valid JSON -> 400, never 413
    r = app_client.post(
        "/publish/v0/folios", content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_non_json_body_rejected_400(app_client):
    r = app_client.post(
        "/publish/v0/folios", content=b"not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert "not valid JSON" in r.json()["error"]


def test_deeply_nested_json_rejected_400_not_500(app_client):
    # Deeply-nested JSON (well under the 1 MiB byte cap — '[' is one byte per level)
    # makes json.loads raise RecursionError, which is NOT a ValueError. Left uncaught
    # it would escape to a 500 + traceback per request (log-amplification DoS). It
    # must be rejected as malformed JSON (400), not crash.
    depth = 200_000
    payload = (b"[" * depth) + (b"]" * depth)
    assert len(payload) < MAX_BATCH_BYTES  # the byte cap does NOT catch this
    r = app_client.post(
        "/publish/v0/folios", content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert "not valid JSON" in r.json()["error"]


def test_non_object_json_rejected_400(app_client):
    r = app_client.post(
        "/publish/v0/folios", content=b"[1, 2, 3]",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert "JSON object" in r.json()["error"]


def test_wrong_protocol_rejected_400(app_client):
    r = app_client.post("/publish/v0/folios", json={"protocol": "bogus/v9"})
    assert r.status_code == 400
    assert "unknown protocol" in r.json()["error"]


def test_valid_unsigned_publish_lands_through_async_route(app_client, tmp_path):
    # The happy path through the converted async + threadpool route: a small
    # unsigned batch is accepted and stored (require_signed OFF).
    batch = _small_unsigned_batch(tmp_path)
    r = app_client.post("/publish/v0/folios", json=batch)
    assert r.status_code == 200
    ack = r.json()
    assert ack["protocol"] == wire.PROTOCOL
    # the site + finding folios are newly accepted
    assert len(ack["accepted"]) >= 2
    assert ack["rejected"] == []


def test_write_lock_contention_returns_503_not_500(app_client, monkeypatch):
    # A lock-acquisition timeout (BEGIN IMMEDIATE can't take the write lock within
    # busy_timeout) surfaces as OperationalError on entry to transaction(), past
    # the per-item handlers. The route must degrade to a retryable 503, NOT let it
    # propagate to an uncaught 500 + traceback (the log-amplification DoS this
    # change set hardens against).
    import sqlite3
    import skein.ingress as ing

    def _locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ing, "ingest", _locked)
    r = app_client.post(
        "/publish/v0/folios",
        json={"protocol": wire.PROTOCOL, "folios": [], "threads": []},
    )
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "1"
    assert "busy" in r.json()["error"]


def test_real_write_lock_contention_returns_503(app_client, data_dir, tmp_path, monkeypatch):
    # End-to-end: a genuinely held write lock makes the route's BEGIN IMMEDIATE
    # time out and raise a DRIVER OperationalError (sqlite_errorcode = SQLITE_BUSY),
    # exercising the numeric-code discrimination path (not the manual-exception
    # fallback). Shrink busy_timeout so the contention resolves fast.
    import skein.store as store_mod
    from skein.store import SkeinStore

    monkeypatch.setattr(store_mod, "BUSY_TIMEOUT_MS", 50)
    holder = SkeinStore(data_dir, check_same_thread=False)
    holder.conn.execute("BEGIN IMMEDIATE")  # hold the write lock
    try:
        r = app_client.post("/publish/v0/folios", json=_small_unsigned_batch(tmp_path))
        assert r.status_code == 503
        assert r.headers.get("Retry-After") == "1"
        assert "busy" in r.json()["error"]
    finally:
        holder.conn.rollback()
        holder.close()


def test_genuine_operationalerror_is_not_masked_as_503(app_client, monkeypatch):
    # A non-lock OperationalError is a real fault and must NOT be masked as a
    # transient 503 — it re-raises (TestClient surfaces it as a server error).
    import sqlite3
    import skein.ingress as ing

    def _real_fault(*a, **k):
        raise sqlite3.OperationalError("no such table: folios")

    monkeypatch.setattr(ing, "ingest", _real_fault)
    with pytest.raises(sqlite3.OperationalError):
        app_client.post(
            "/publish/v0/folios",
            json={"protocol": wire.PROTOCOL, "folios": [], "threads": []},
        )


async def test_client_disconnect_midbody_is_handled_quietly(data_dir):
    # A client that drops mid-body raises starlette ClientDisconnect inside the
    # body-read loop. The route must catch it and return cleanly (no propagating
    # exception, no ASGI traceback) — an adversary-controllable disconnect must
    # not be a logged application error (log-amplification DoS). Driven directly
    # against the endpoint with an ASGI receive that sends http.disconnect mid-stream.
    from starlette.requests import ClientDisconnect, Request

    app = create_app()
    endpoint = next(
        r.endpoint for r in app.routes
        if getattr(r, "path", None) == "/publish/v0/folios"
    )

    messages = [
        {"type": "http.request", "body": b'{"protocol":', "more_body": True},
        {"type": "http.disconnect"},
    ]

    async def receive():
        return messages.pop(0)

    scope = {
        "type": "http", "method": "POST", "path": "/publish/v0/folios",
        "headers": [(b"content-type", b"application/json")], "query_string": b"",
    }
    request = Request(scope, receive)

    try:
        resp = await endpoint(request)  # must NOT raise ClientDisconnect
    except ClientDisconnect:  # pragma: no cover - the bug this guards against
        pytest.fail("ClientDisconnect leaked out of the route as an application error")
    assert resp.status_code == 400
