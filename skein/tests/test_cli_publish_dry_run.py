"""publish --dry-run must be fully side-effect-free at the CLI seam.

A dry run never reaches an instance and must produce no external artifact. That
includes the OIDC ceremony: with ``--sign --login`` a real send opens a browser
to acquire a token, but a dry run must NOT build a signer at all. These tests
pin that the CLI gates signer construction on the real send, while still
reporting the plan (``dry_run`` + the signed-intent flag).
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from skein.cli import cli
from skein.station import Station


def _run(tmp_path, *args):
    return CliRunner().invoke(cli, ["--data-dir", str(tmp_path / ".skein"), *args])


def _seed_folio(tmp_path):
    with Station(tmp_path / ".skein") as st:
        st.create_site("specs", purpose="p", created_by="t")
        st.post("finding", "specs", "T", "b", created_by="t")


def test_dry_run_sign_login_never_builds_a_signer(tmp_path, monkeypatch):
    """--dry-run --sign --login must not construct a signer (no browser/token)."""
    _seed_folio(tmp_path)

    def exploding_build_signer(*a, **k):
        raise AssertionError("dry-run must not build a signer (no OIDC login)")

    monkeypatch.setattr("skein.cli._build_signer", exploding_build_signer)

    r = _run(tmp_path, "publish", "--site", "specs", "--dry-run", "--sign", "--login", "--json")
    assert r.exit_code == 0, r.output
    result = json.loads(r.output)
    # The plan still reports a dry run that WOULD be signed — without signing.
    assert result["dry_run"] is True
    assert result["signed"] is True
    assert result["sent"] is False


def test_dry_run_without_sign_reports_unsigned_plan(tmp_path, monkeypatch):
    """A plain dry run reports the plan as unsigned and builds no signer."""
    _seed_folio(tmp_path)

    monkeypatch.setattr(
        "skein.cli._build_signer",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no signer on dry-run")),
    )

    r = _run(tmp_path, "publish", "--site", "specs", "--dry-run", "--json")
    assert r.exit_code == 0, r.output
    result = json.loads(r.output)
    assert result["dry_run"] is True
    assert result["signed"] is False
