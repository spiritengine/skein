"""account invite (mint/list/revoke) + whoami CLI surface (brief-20260615-ofv1).

The redeem-invite client and the live ceremony are exercised by the e2e/route
tests; here we cover the operator-side invite lifecycle (no Sigstore needed) and
the whoami identity print (sigstore session mocked)."""

from __future__ import annotations

from click.testing import CliRunner

from skein.cli import cli
from skein.identity import hash_token
from skein.station import Station

OP_ISS, OP_SUB = "https://accounts.google.com", "op@example.com"


def _run(tmp_path, *args):
    return CliRunner().invoke(cli, ["--data-dir", str(tmp_path / ".skein"), *args])


def _open(tmp_path):
    return Station(tmp_path / ".skein")


def _init_op(tmp_path):
    return _run(tmp_path, "account", "init-operator", "--issuer", OP_ISS, "--subject", OP_SUB)


def test_mint_prints_token_and_records_hash(tmp_path):
    _init_op(tmp_path)
    r = _run(tmp_path, "account", "invite", "mint", "--note", "Alice", "--origin", "https://interskein.com")
    assert r.exit_code == 0, r.output
    assert "redeem-invite" in r.output and "interskein.com" in r.output
    # the printed token redeems to the stored hash
    with _open(tmp_path) as st:
        rows = st.store.list_invites()
        assert len(rows) == 1
        assert rows[0]["note"] == "Alice" and rows[0]["vouched_by_subject"] == OP_SUB
        assert rows[0]["used_at"] is None


def test_mint_json_token_hashes_to_stored_row(tmp_path):
    import json
    _init_op(tmp_path)
    r = _run(tmp_path, "account", "invite", "mint", "--json")
    assert r.exit_code == 0
    payload = json.loads(r.output)
    assert hash_token(payload["token"]) == payload["token_hash"]
    with _open(tmp_path) as st:
        assert st.store.get_invite_by_token_hash(payload["token_hash"]) is not None


def test_mint_bad_duration_errors(tmp_path):
    _init_op(tmp_path)
    r = _run(tmp_path, "account", "invite", "mint", "--expires", "soon")
    assert r.exit_code != 0 and "30m" in r.output


def test_mint_without_operator_errors(tmp_path):
    r = _run(tmp_path, "account", "invite", "mint")
    assert r.exit_code != 0 and "operator" in r.output


def test_list_shows_outstanding_then_revoked(tmp_path):
    import json
    _init_op(tmp_path)
    r = _run(tmp_path, "account", "invite", "mint", "--json", "--note", "Bob")
    token = json.loads(r.output)["token"]
    out = _run(tmp_path, "account", "invite", "list").output
    assert "outstanding author" in out and "Bob" in out
    # revoke by token, then it drops from the default list, shows under --all
    rr = _run(tmp_path, "account", "invite", "revoke", token)
    assert rr.exit_code == 0 and "revoked invite" in rr.output
    assert "outstanding" not in _run(tmp_path, "account", "invite", "list").output
    assert "revoked author" in _run(tmp_path, "account", "invite", "list", "--all").output


def test_revoke_by_hash_prefix(tmp_path):
    import json
    _init_op(tmp_path)
    th = json.loads(_run(tmp_path, "account", "invite", "mint", "--json").output)["token_hash"]
    r = _run(tmp_path, "account", "invite", "revoke", "--hash", th[:12])
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        assert st.store.get_invite_by_token_hash(th)["revoked_at"] is not None


def test_revoke_unknown_errors(tmp_path):
    _init_op(tmp_path)
    r = _run(tmp_path, "account", "invite", "revoke", "no-such-token")
    assert r.exit_code != 0 and "no active invite" in r.output


def test_whoami_prints_identity(tmp_path, monkeypatch):
    from skein import sign as sign_mod

    class _Sess:
        issuer = "https://accounts.google.com"
        subject = "carol@example.com"

    monkeypatch.setattr(sign_mod, "acquire_oidc_session", lambda **k: _Sess())
    r = CliRunner().invoke(cli, ["whoami"])
    assert r.exit_code == 0
    assert "issuer https://accounts.google.com" in r.output
    assert "subject carol@example.com" in r.output


def test_redeem_invite_requires_login(tmp_path):
    r = CliRunner().invoke(cli, ["redeem-invite", "tok", "--to", "https://x"])
    assert r.exit_code != 0 and "--login" in r.output
