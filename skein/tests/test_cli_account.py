"""Thread F2 — account CLI (D1-D12, D19), operator config + startup invariant
(D13-D18, D20), read-app exemption (D17), and the verify-cache backfill (VC9).
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from skein.cli import cli
from skein.station import Station


I, S = "https://accounts.google.com", "op@example.com"
I2, S2 = "https://accounts.google.com", "author2@example.com"


def _run(tmp_path, *args):
    return CliRunner().invoke(cli, ["--data-dir", str(tmp_path / ".skein"), *args])


def _open(tmp_path):
    return Station(tmp_path / ".skein")


# --- D1-D3: init-operator ---------------------------------------------------


def test_init_operator_happy(tmp_path):  # D1
    r = _run(tmp_path, "account", "init-operator", "--issuer", I, "--subject", S)
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        op = st.store.get_operator()
        assert (op.issuer, op.subject) == (I, S)
        assert [e["event"] for e in st.store.get_binding_events()] == ["created"]


def test_init_operator_double_init_refuses(tmp_path):  # D2
    _run(tmp_path, "account", "init-operator", "--issuer", I, "--subject", S)
    r = _run(tmp_path, "account", "init-operator", "--issuer", "https://x", "--subject", "y")
    assert r.exit_code != 0 and "OperatorAlreadyBootstrapped" in r.output
    with _open(tmp_path) as st:
        assert st.store.count_active_operators() == 1


def test_init_operator_after_revoke_succeeds(tmp_path):  # D3
    _run(tmp_path, "account", "init-operator", "--issuer", I, "--subject", S)
    with _open(tmp_path) as st:
        st.store.revoke_binding(I, S)
    r = _run(tmp_path, "account", "init-operator", "--issuer", "https://x", "--subject", "y")
    assert r.exit_code == 0


# --- D4-D5: add -------------------------------------------------------------


def test_account_add_happy(tmp_path):  # D4
    _run(tmp_path, "account", "init-operator", "--issuer", I, "--subject", S)
    r = _run(tmp_path, "account", "add", "--issuer", I2, "--subject", S2, "--role", "author")
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        b = st.store.get_binding(I2, S2)
        assert b.role == "author" and b.revoked_at is None
        assert b.vouched_by_subject == S
        assert "created" in [e["event"] for e in st.store.get_binding_events(I2, S2)]


def test_account_add_requires_existing_operator(tmp_path):  # D5
    r = _run(tmp_path, "account", "add", "--issuer", I2, "--subject", S2)
    assert r.exit_code != 0 and "no operator" in r.output


def test_account_add_on_active_operator_echoes_stored_role(tmp_path):
    """`account add --role author` on the ACTIVE OPERATOR's own identity hits
    add_binding's already-active idempotent no-op: the binding correctly STAYS
    operator. The CLI must echo the STORED role ("operator"), not the requested
    one ("author")."""
    _run(tmp_path, "account", "init-operator", "--issuer", I, "--subject", S)
    r = _run(tmp_path, "account", "add", "--issuer", I, "--subject", S, "--role", "author")
    assert r.exit_code == 0
    assert f"operator {I}/{S}" in r.output
    assert "author" not in r.output  # the requested role is NOT echoed
    with _open(tmp_path) as st:
        b = st.store.get_binding(I, S)
        assert b.role == "operator" and b.revoked_at is None  # unchanged in the store


# --- D6-D8: revoke ----------------------------------------------------------


def test_account_revoke_happy(tmp_path):  # D6
    _run(tmp_path, "account", "init-operator", "--issuer", I, "--subject", S)
    _run(tmp_path, "account", "add", "--issuer", I2, "--subject", S2)
    r = _run(tmp_path, "account", "revoke", "--issuer", I2, "--subject", S2)
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        assert st.store.get_binding(I2, S2).revoked_at is not None
        assert "revoked" in [e["event"] for e in st.store.get_binding_events(I2, S2)]


def test_account_revoke_nonexistent_errors(tmp_path):  # D7
    _run(tmp_path, "account", "init-operator", "--issuer", I, "--subject", S)
    r = _run(tmp_path, "account", "revoke", "--issuer", "https://x", "--subject", "ghost")
    assert r.exit_code != 0 and "no active binding for https://x/ghost" in r.output


def test_account_revoke_active_operator_refused(tmp_path):  # D8
    _run(tmp_path, "account", "init-operator", "--issuer", I, "--subject", S)
    r = _run(tmp_path, "account", "revoke", "--issuer", I, "--subject", S)
    assert r.exit_code != 0 and "refusing to revoke the active operator" in r.output
    with _open(tmp_path) as st:
        assert st.store.get_operator() is not None  # unchanged


# --- D9-D11, D19: rotate-operator -------------------------------------------


def test_rotate_operator_atomic(tmp_path):  # D9
    _run(tmp_path, "account", "init-operator", "--issuer", I, "--subject", S)
    r = _run(tmp_path, "account", "rotate-operator", "--new-issuer", I2, "--new-subject", S2)
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        assert (st.store.get_operator().issuer, st.store.get_operator().subject) == (I2, S2)
        events = [e["event"] for e in st.store.get_binding_events()]
        assert "rotated_out" in events and "rotated_in" in events
        from skein.authorization import Principal, can_write
        assert can_write(Principal(I, S), st.store) is False
        assert can_write(Principal(I2, S2), st.store) is True


def test_rotate_operator_requires_existing_operator(tmp_path):  # D10
    r = _run(tmp_path, "account", "rotate-operator", "--new-issuer", I2, "--new-subject", S2)
    assert r.exit_code != 0 and "no operator to rotate" in r.output


def test_rotate_operator_is_all_or_nothing(tmp_path, monkeypatch):  # D11
    _run(tmp_path, "account", "init-operator", "--issuer", I, "--subject", S)
    # force the new-operator install to fail mid-rotation
    from skein.store import SkeinNextStore
    orig = SkeinNextStore.add_binding

    def boom(self, *a, **k):
        if k.get("event") == "rotated_in":
            raise RuntimeError("install failed")
        return orig(self, *a, **k)

    monkeypatch.setattr(SkeinNextStore, "add_binding", boom)
    r = _run(tmp_path, "account", "rotate-operator", "--new-issuer", I2, "--new-subject", S2)
    assert r.exit_code != 0
    with _open(tmp_path) as st:
        op = st.store.get_operator()
        assert (op.issuer, op.subject) == (I, S)  # old operator still active (rollback)


def test_rotate_operator_onto_existing_active_author_promotes(tmp_path):  # D19
    _run(tmp_path, "account", "init-operator", "--issuer", I, "--subject", S)
    _run(tmp_path, "account", "add", "--issuer", I2, "--subject", S2)
    with _open(tmp_path) as st:
        t0 = st.store.get_binding(I2, S2).created_at
    r = _run(tmp_path, "account", "rotate-operator", "--new-issuer", I2, "--new-subject", S2)
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        b = st.store.get_binding(I2, S2)
        assert b.role == "operator" and b.created_at == t0  # promoted, created_at preserved
        assert st.store.count_active_operators() == 1
        assert "promoted" in [e["event"] for e in st.store.get_binding_events(I2, S2)]


def test_rotate_operator_onto_self_is_refused_no_audit_mutation(tmp_path):  # harden D
    """Rotating onto the CURRENT operator must refuse before any mutation, not
    revoke-then-re-promote the same identity (which churns the audit trail with a
    spurious rotated_out + promoted)."""
    _run(tmp_path, "account", "init-operator", "--issuer", I, "--subject", S)
    r = _run(tmp_path, "account", "rotate-operator", "--new-issuer", I, "--new-subject", S)
    assert r.exit_code != 0 and "onto itself" in r.output
    with _open(tmp_path) as st:
        assert st.store.count_active_operators() == 1
        # the audit trail shows ONLY the original creation — no rotated_out/promoted
        assert [e["event"] for e in st.store.get_binding_events(I, S)] == ["created"]


# --- D12: list --------------------------------------------------------------


def test_account_list_plain_text(tmp_path):  # D12
    _run(tmp_path, "account", "init-operator", "--issuer", I, "--subject", S)
    _run(tmp_path, "account", "add", "--issuer", I2, "--subject", S2)
    r = _run(tmp_path, "account", "list")
    assert r.exit_code == 0
    lines = [ln for ln in r.output.splitlines() if ln.strip()]
    assert f"operator {I}/{S}" in lines
    assert f"author {I2}/{S2}" in lines
    assert "|" not in r.output and "<" not in r.output  # no tables/markdown
    # revoked excluded by default; included with --all
    _run(tmp_path, "account", "revoke", "--issuer", I2, "--subject", S2)
    assert f"author {I2}/{S2}" not in _run(tmp_path, "account", "list").output
    assert "(revoked)" in _run(tmp_path, "account", "list", "--all").output


# --- D13-D20: startup invariant + read-app exemption ------------------------


def _ingress_app(tmp_path, monkeypatch, require_signed):
    from skein import ingress
    monkeypatch.setenv("SKEIN_DATA_DIR", str(tmp_path / ".skein"))
    if require_signed:
        monkeypatch.setenv(ingress.ENV_REQUIRE_SIGNED, "1")
    else:
        monkeypatch.delenv(ingress.ENV_REQUIRE_SIGNED, raising=False)
    return ingress


def test_create_app_require_signed_without_operator_refuses(tmp_path, monkeypatch):  # D13
    ingress = _ingress_app(tmp_path, monkeypatch, require_signed=True)
    with pytest.raises(ingress.OperatorInvariantError) as exc:
        ingress.create_app()
    assert "account init-operator" in str(exc.value)


def test_create_app_require_signed_with_operator_starts(tmp_path, monkeypatch):  # D14
    _run(tmp_path, "account", "init-operator", "--issuer", I, "--subject", S)
    ingress = _ingress_app(tmp_path, monkeypatch, require_signed=True)
    app = ingress.create_app()
    assert app is not None


def test_require_signed_off_no_operator_ok(tmp_path, monkeypatch):  # D15
    ingress = _ingress_app(tmp_path, monkeypatch, require_signed=False)
    assert ingress.create_app() is not None


def test_operator_identity_sourced_from_sidecar_not_stationfile(tmp_path, monkeypatch):  # D16
    _run(tmp_path, "account", "init-operator", "--issuer", I, "--subject", S)
    # no stationfile operator field anywhere; the check reads account_bindings
    ingress = _ingress_app(tmp_path, monkeypatch, require_signed=True)
    assert ingress.create_app() is not None
    with _open(tmp_path) as st:
        assert st.store.get_operator().subject == S


def test_create_app_require_signed_multiple_operators_refuses(tmp_path, monkeypatch):  # D20
    with _open(tmp_path) as st:
        st.store.add_binding("https://idpA", "first", role="operator")
        st.store.add_binding("https://idpB", "second", role="operator")
    ingress = _ingress_app(tmp_path, monkeypatch, require_signed=True)
    with pytest.raises(ingress.OperatorInvariantError) as exc:
        ingress.create_app()
    assert "single-active-operator" in str(exc.value) or "active operators" in str(exc.value)


def test_read_app_starts_without_operator_under_require_signed(tmp_path, monkeypatch):  # D17
    # The READ web app serves regardless of operator-count; the startup refusal is
    # the INGRESS's alone. It does NOT suppress folio_verdict's live binding step.
    from skein.web import app as web_app
    monkeypatch.setenv("SKEIN_DATA_DIR", str(tmp_path / ".skein"))
    monkeypatch.setenv("SKEIN_REQUIRE_SIGNED", "1")
    monkeypatch.setenv("SKEIN_PROJECT", "interskein")
    Station(tmp_path / ".skein").close()  # empty corpus, no operator
    app = web_app.create_app()
    assert app is not None


def test_ingress_startup_logs_operator_status(tmp_path, monkeypatch, caplog):  # D18
    _run(tmp_path, "account", "init-operator", "--issuer", I, "--subject", S)
    ingress = _ingress_app(tmp_path, monkeypatch, require_signed=True)
    import logging
    with caplog.at_level(logging.INFO, logger="skein.ingress"):
        ingress.create_app()
    assert any("operator" in rec.message for rec in caplog.records)


# --- VC9: verify-cache backfill ---------------------------------------------


def test_maintenance_verify_cache_backfills(tmp_path, monkeypatch):  # VC9
    from skein import signing
    from skein.signing import MultiVerifyResult, VerifyResult, VerifyStatus
    from skein import profile, sign as sign_mod, wire
    from skein.ingress import ingest

    _run(tmp_path, "account", "init-operator", "--issuer", I, "--subject", S)
    client = Station(tmp_path / "client" / ".skein")
    client.create_site("specs", purpose="p", created_by="t")
    client.post("finding", "specs", "T", "b", created_by="t")
    instance = Station(tmp_path / ".skein")
    instance.store.add_binding(I, S, role="author")

    def signer(cb):
        preimage = profile.profiled_preimage(profile.CANON_PROFILE_MANIFEST_V1, cb)
        bundle = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1", bundles=["x"],
            canonical_bytes=preimage, canon_version=profile.CANON_PROFILE_MANIFEST_V1)
        return sign_mod.SignedResult(bundle=bundle, issuer=I, subject=S)

    from skein import publish as pub
    folios, threads, slugs = pub.collect_publish_set(client, site="specs")
    batch = wire.build_batch(folios, threads, slugs)
    addrs = [f["content_hash"] for f in batch["folios"]] + [t["thread_hash"] for t in batch["threads"]]
    batch["manifest_signature"] = sign_mod.sign_manifest(addrs, signer)

    def ok(cb, b):
        return MultiVerifyResult(
            results=[VerifyResult(status=VerifyStatus.VERIFIED, issuer=I, subject=S)],
            overall=VerifyStatus.VERIFIED)

    ingest(instance, batch, verifier=ok, require_signed=True)
    # clear the cache the ingress wrote, then backfill
    instance.store.conn.execute("DELETE FROM verify_cache")
    instance.store.conn.commit()
    monkeypatch.setattr(signing, "verify_multi", ok)
    r = _run(tmp_path, "maintenance", "verify-cache")
    assert r.exit_code == 0 and "backfilled 1" in r.output
    assert instance.store.conn.execute("SELECT COUNT(*) FROM verify_cache").fetchone()[0] == 1
    # idempotent second run
    assert _run(tmp_path, "maintenance", "verify-cache").exit_code == 0
    client.close()
    instance.close()
