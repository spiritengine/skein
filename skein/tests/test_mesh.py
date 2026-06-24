"""Tests for the mesh client (`mesh fetch`): resolve over HTTP, strict-verify
locally, fork-F verdict + exit codes.

The HTTP wire is backed by a TestClient of the station app via a `requests.get`
shim, so these exercise the real resolve+envelope path end to end. The signed
verdict branches monkeypatch `sign.verify_wire_folio` (the crypto itself is the
signing suite's job; here we pin the client's fork-F mapping over its result).
"""


import pytest
from fastapi.testclient import TestClient

from skein import sign as sign_mod
from skein.mesh import client as mesh_client
from skein.mesh.client import (
    EXIT_NOT_RESOLVED,
    EXIT_OK,
    EXIT_REQUIRE_SIGNED,
    EXIT_SIGNATURE_INVALID,
    EXIT_UNVERIFIED,
    fetch,
    verify_envelope,
)
from skein.station import Station
from skein.web.app import ENV_DATA_DIR, ENV_PROJECT, create_app

LOCAL = "http://127.0.0.1:9001"
REMOTE = "http://notes.example.org"


@pytest.fixture
def seeded(tmp_path):
    data_dir = tmp_path / ".skein"
    with Station(data_dir) as st:
        st.create_site("proj", purpose="the project")
        a = st.post(type="finding", site="proj", title="Finding A", content="body A here",
                    created_by="alice", created_at="2026-01-01T00:00:00Z")
    return {"data_dir": data_dir, "a": a}


@pytest.fixture
def wired(seeded, monkeypatch):
    """Point mesh_client.requests.get at a TestClient of the station."""
    monkeypatch.setenv(ENV_DATA_DIR, str(seeded["data_dir"]))
    monkeypatch.setenv(ENV_PROJECT, "interskein")
    station = TestClient(create_app())

    def fake_get(url, timeout=None):
        from urllib.parse import urlparse

        parsed = urlparse(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        return station.get(path)

    monkeypatch.setattr(mesh_client.requests, "get", fake_get)
    return seeded


# --- verify_envelope (unit, real envelopes) ---------------------------------


def _envelope(station_client, address):
    return station_client.get(f"/folio/{address}.json").json()


def test_unsigned_envelope_integrity_ok(wired, monkeypatch):
    station = TestClient(create_app())
    env = _envelope(station, wired["a"])
    state, code, reason, identity, _vh = verify_envelope(env)
    assert state == "unsigned" and code == EXIT_OK


def test_unsigned_envelope_tampered_body_is_invalid(wired):
    station = TestClient(create_app())
    env = _envelope(station, wired["a"])
    env["body"]["content"] = "tampered — not the bytes that hash to the address"
    state, code, reason, _, _vh = verify_envelope(env)
    assert state == "invalid" and code == EXIT_SIGNATURE_INVALID and reason == "hash mismatch"


def test_error_envelope_is_not_resolved():
    env = {"kind": "error", "body": {"found": False, "error": "not_found"}}
    state, code, reason, _, _vh = verify_envelope(env)
    assert state == "not_resolved" and code == EXIT_NOT_RESOLVED and reason == "not_found"


def test_malformed_envelopes_do_not_crash(monkeypatch):
    # A hostile station can return arbitrary JSON at /folio/{addr}.json. None of
    # these may crash the client; each is reported invalid, not a traceback.
    for bad in (
        [1, 2, 3],                                              # env is a list
        "just a string",                                       # env is a scalar
        {"kind": "folio", "proof": {}, "body": [1, 2, 3]},     # list body (was {**list} crash)
        {"kind": "catalog", "proof": None, "body": []},        # non-folio at a folio address
    ):
        state, code, _r, _i, _vh = verify_envelope(bad)
        assert state == "invalid" and code == EXIT_SIGNATURE_INVALID


def test_signed_verified_maps_to_ok(monkeypatch):
    env = {"kind": "folio", "body": {"type": "finding", "title": "t", "content": "c",
                                     "created_at": "2026-01-01T00:00:00Z", "created_by": "alice"},
           "proof": {"signature_bundle": {"b": 1}}}
    monkeypatch.setattr(sign_mod, "verify_wire_folio",
                        lambda wf: (True, "verified", {"issuer": "iss", "subject": "alice@x"}))
    state, code, reason, identity, _vh = verify_envelope(env)
    assert state == "verified" and code == EXIT_OK and identity["subject"] == "alice@x"


def test_signed_invalid_maps_to_invalid(monkeypatch):
    env = {"kind": "folio", "body": {"type": "f", "title": "t", "content": "c",
                                     "created_at": "2026-01-01T00:00:00Z", "created_by": "a"},
           "proof": {"signature_bundle": {"b": 1}}}
    monkeypatch.setattr(sign_mod, "verify_wire_folio", lambda wf: (False, "FAILED", None))
    state, code, _, _, _vh = verify_envelope(env)
    assert state == "invalid" and code == EXIT_SIGNATURE_INVALID


def test_signed_unverifiable_maps_to_unverified(monkeypatch):
    env = {"kind": "folio", "body": {"type": "f", "title": "t", "content": "c",
                                     "created_at": "2026-01-01T00:00:00Z", "created_by": "a"},
           "proof": {"signature_bundle": {"b": 1}}}
    monkeypatch.setattr(sign_mod, "verify_wire_folio",
                        lambda wf: (False, "OFFLINE_NO_TRUSTED_ROOT", None))
    state, code, _, _, _vh = verify_envelope(env)
    assert state == "unverified" and code == EXIT_UNVERIFIED


# --- fetch (over the wire) --------------------------------------------------


def test_fetch_unsigned_local_ok_quiet(wired):
    r = fetch(LOCAL, wired["a"])
    assert r.resolved and r.state == "unsigned" and r.exit_code == EXIT_OK
    assert r.warning is None  # local-unsigned is operator-vouched, no warning
    assert "body A here" in r.markdown


def test_fetch_unsigned_require_signed_nonzero(wired):
    r = fetch(LOCAL, wired["a"], require_signed=True)
    assert r.state == "unsigned" and r.exit_code == EXIT_REQUIRE_SIGNED


def test_fetch_remote_unsigned_warns(wired):
    r = fetch(REMOTE, wired["a"])
    assert r.state == "unsigned" and r.exit_code == EXIT_OK
    assert r.warning and "remote" in r.warning.lower()


def test_fetch_not_found(wired):
    r = fetch(LOCAL, "sha256::" + "0" * 64)
    assert not r.resolved and r.state == "not_resolved" and r.exit_code == EXIT_NOT_RESOLVED


def test_fetch_instance_unreachable(seeded, monkeypatch):
    import requests

    def boom(url, timeout=None):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(mesh_client.requests, "get", boom)
    r = fetch(LOCAL, seeded["a"])
    assert not r.resolved and r.exit_code == EXIT_NOT_RESOLVED and "unreachable" in r.reason


# --- address pinning (content addressing's invariant: address == hash(content)) -


def test_full_hash_address_pins(wired):
    # The requested address IS a full content hash, so the served content must
    # hash to it — and does.
    r = fetch(LOCAL, wired["a"])
    assert r.state == "unsigned" and r.pinned is True


def test_substituted_content_is_address_mismatch(wired, monkeypatch):
    # A station that serves a self-consistent envelope for content X in answer to
    # a request for a DIFFERENT address must be caught — content addressing's one
    # invariant, enforced on the side that does not trust the station.
    station = TestClient(create_app())
    real_env = _envelope(station, wired["a"])  # self-consistent for a
    monkeypatch.setattr(mesh_client, "resolve", lambda inst, addr, timeout=10.0: (real_env, None))
    wrong = "sha256::" + "b" * 64  # a full hash that is NOT a's
    r = fetch(LOCAL, wrong)
    assert r.state == "invalid" and r.exit_code == EXIT_SIGNATURE_INVALID
    assert "address mismatch" in r.reason


def test_legacy_id_address_is_unpinned(wired, seeded):
    # A bare migrated id carries no digest to pin against — resolves via the
    # station's alias table, so the name->hash mapping is the station's word. The
    # content still self-certifies; the verdict says so honestly (pinned is None).
    with Station(seeded["data_dir"]) as st:
        st.store.set_alias("finding-20260101-leg1", seeded["a"])
    r = fetch(LOCAL, "finding-20260101-leg1")
    assert r.resolved and r.state == "unsigned" and r.pinned is None
    assert r.pin_kind is None


def test_signed_substitution_via_omitted_content_hash_caught(wired, monkeypatch):
    # fell-r2 BLOCKER: a signed envelope may legally OMIT proof.content_hash (canon
    # hashes only the body). A station serving a validly-signed body B with no
    # content_hash, in answer to a request for full hash A, must be caught as an
    # address mismatch — not reported verified/exit-0. The pin runs against the
    # RE-DERIVED body hash, never the station-supplied (here absent) claim.
    env = {"kind": "folio",
           "body": {"type": "finding", "title": "t", "content": "validly signed body B",
                    "created_at": "2026-01-01T00:00:00Z", "created_by": "alice"},
           "proof": {"signature_bundle": {"b": 1}}}  # note: NO content_hash
    monkeypatch.setattr(mesh_client, "resolve", lambda inst, addr, timeout=10.0: (env, None))
    monkeypatch.setattr(sign_mod, "verify_wire_folio",
                        lambda wf: (True, "verified", {"issuer": "i", "subject": "alice@x"}))
    r = fetch(LOCAL, "sha256::" + "a" * 64)  # request a full hash that is NOT B's
    assert r.state == "invalid" and r.exit_code == EXIT_SIGNATURE_INVALID
    assert "address mismatch" in r.reason
    assert r.markdown is None  # the substituted body must not reach stdout


def test_pin_check_full_prefix_and_mismatch():
    from skein.mesh.client import _pin_check

    actual = "sha256::" + "ab" * 32  # a full 64-hex digest
    # full-digest address, exact match
    assert _pin_check("sha256::" + "ab" * 32, actual)[:2] == (True, "full")
    # alias short-hash address whose digest prefixes the full hash -> prefix bind
    assert _pin_check("mysite::sha256::abababab", actual)[:2] == (True, "prefix")
    # full-digest address that does NOT match -> mismatch
    matched, kind, reason = _pin_check("sha256::" + "cd" * 32, actual)
    assert matched is False and kind is None and "address mismatch" in reason
    # bare alias / legacy id -> unpinnable
    assert _pin_check("finding-20260101-leg1", actual)[:2] == (None, None)


def _signed_envelope_of_a(station, a):
    env = _envelope(station, a)
    env["proof"]["signature_bundle"] = {"b": 1}  # pretend the real folio is signed
    return env


def test_unverified_pin_match_stays_exit_4(wired, monkeypatch):
    # An unverified result (authorship uncheckable) whose content DOES hash to the
    # requested address stays UNVERIFIED exit 4 — pinned, not upgraded or downgraded.
    station = TestClient(create_app())
    env = _signed_envelope_of_a(station, wired["a"])
    monkeypatch.setattr(mesh_client, "resolve", lambda inst, addr, timeout=10.0: (env, None))
    monkeypatch.setattr(sign_mod, "verify_wire_folio",
                        lambda wf: (False, "OFFLINE_NO_TRUSTED_ROOT", None))
    r = fetch(LOCAL, wired["a"])
    assert r.state == "unverified" and r.exit_code == EXIT_UNVERIFIED and r.pinned is True


def test_unverified_substituted_is_invalid(wired, monkeypatch):
    # Even when authorship can't be checked, a body that doesn't hash to the
    # requested address is an address mismatch -> invalid (the pin runs regardless).
    station = TestClient(create_app())
    env = _signed_envelope_of_a(station, wired["a"])
    monkeypatch.setattr(mesh_client, "resolve", lambda inst, addr, timeout=10.0: (env, None))
    monkeypatch.setattr(sign_mod, "verify_wire_folio",
                        lambda wf: (False, "OFFLINE_NO_TRUSTED_ROOT", None))
    r = fetch(LOCAL, "sha256::" + "f" * 64)  # not a's hash
    assert r.state == "invalid" and r.exit_code == EXIT_SIGNATURE_INVALID


# --- CLI --------------------------------------------------------------------


def test_cli_fetch_exit_code_and_streams(wired, monkeypatch):
    from click.testing import CliRunner

    from skein.mesh.cli import cli

    # CliRunner with mix_stderr=False so we can assert stdout (body) vs stderr (verdict).
    runner = CliRunner()
    result = runner.invoke(cli, ["fetch", wired["a"], "--from", LOCAL])
    assert result.exit_code == EXIT_OK
    assert "body A here" in result.output
    assert "UNSIGNED" in result.stderr


def test_cli_fetch_require_signed_nonzero(wired):
    from click.testing import CliRunner

    from skein.mesh.cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["fetch", wired["a"], "--from", LOCAL, "--require-signed"])
    assert result.exit_code == EXIT_REQUIRE_SIGNED


def test_cli_fetch_json(wired):
    from click.testing import CliRunner

    from skein.mesh.cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["fetch", wired["a"], "--from", LOCAL, "--json"])
    assert result.exit_code == EXIT_OK
    # Click 8.3 combines streams in .output; the JSON envelope is on stdout.
    assert '"kind": "folio"' in result.output
