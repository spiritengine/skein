"""Stage 3 CLI — ignite-from-brief, ignite --mantle, and reply/comment.

Drives the ported intranet/handoff verbs through the click CLI against a local
station, no server — the surface mill and agents actually call. Station-level
coverage is in test_intranet.py.
"""

import json

import pytest
from click.testing import CliRunner

from skein.cli import cli


def _run(data_dir, *args, env=None):
    return CliRunner().invoke(cli, ["--data-dir", str(data_dir), *args], env=env)


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / ".skein"
    # A site to hang briefs/mantles/issues off of.
    r = _run(d, "site", "create", "work", "--by", "alice")
    assert r.exit_code == 0, r.output
    return d


def _post(data_dir, type, title, content, by="alice"):
    r = _run(data_dir, "post", type, "work", title, "-c", content, "--by", by, "--json")
    assert r.exit_code == 0, r.output
    return json.loads(r.output)["content_hash"]


# --- ignite from brief -------------------------------------------------------


def test_ignite_from_brief_surfaces_content_and_threads_handoff(data_dir):
    brief = _post(data_dir, "brief", "handoff", "Port stage 3. Keep pull, drop push.")
    r = _run(data_dir, "ignite", brief, "--name", "nub-0622")
    assert r.exit_code == 0, r.output
    assert "Mission:" in r.output
    assert "Port stage 3. Keep pull, drop push." in r.output
    assert "You are: nub-0622" in r.output

    # The handoff edge is readable on the brief.
    t = _run(data_dir, "thread", brief)
    assert "ignited_from" in t.output
    assert "nub-0622" in t.output


def test_ignite_unknown_brief_errors_before_register(data_dir):
    r = _run(data_dir, "ignite", "brief-does-not-exist", "--name", "nub-0622")
    assert r.exit_code != 0
    assert "no brief" in r.output
    # No orphan agent folio left behind.
    assert _run(data_dir, "roster").output.strip() == "no agents registered"


def test_ignite_json_includes_mission(data_dir):
    brief = _post(data_dir, "brief", "handoff", "the brief body")
    r = _run(data_dir, "ignite", brief, "--name", "nub-0622", "--json")
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert "the brief body" in payload["mission"]
    assert payload["brief_id"] == brief


# --- ignite --mantle ---------------------------------------------------------


def test_ignite_mantle_by_name_surfaces_role(data_dir):
    _post(data_dir, "mantle", "quartermaster", "You triage shards and drive fells.")
    r = _run(data_dir, "ignite", "--mantle", "quartermaster", "--name", "qm-0622")
    assert r.exit_code == 0, r.output
    assert "Mantle: quartermaster" in r.output
    assert "You triage shards and drive fells." in r.output


def test_ignite_unknown_mantle_errors(data_dir):
    r = _run(data_dir, "ignite", "--mantle", "nonesuch", "--name", "x-0622")
    assert r.exit_code != 0
    assert "no mantle found" in r.output


def test_ignite_mantle_by_hash(data_dir):
    mantle = _post(data_dir, "mantle", "oracle", "edge cases and races")
    r = _run(data_dir, "ignite", "--mantle", mantle, "--name", "or-0622")
    assert r.exit_code == 0, r.output
    assert "edge cases and races" in r.output


def test_ignite_brief_mantle_message_combine(data_dir):
    brief = _post(data_dir, "brief", "handoff", "BRIEF-BODY")
    _post(data_dir, "mantle", "boffin", "MANTLE-BODY")
    r = _run(
        data_dir,
        "ignite",
        brief,
        "--mantle",
        "boffin",
        "--message",
        "MESSAGE-BODY",
        "--name",
        "combo-0622",
    )
    assert r.exit_code == 0, r.output
    for chunk in ("BRIEF-BODY", "MANTLE-BODY", "MESSAGE-BODY"):
        assert chunk in r.output


# --- reply / comment ---------------------------------------------------------


def test_reply_posts_and_folio_shows_it(data_dir):
    issue = _post(data_dir, "issue", "bug", "something broke")
    r = _run(data_dir, "reply", issue, "I'll investigate", "--agent", "nub-0622")
    assert r.exit_code == 0, r.output
    assert "Posted reply" in r.output

    f = _run(data_dir, "folio", issue)
    assert "Replies (1):" in f.output
    assert "nub-0622: I'll investigate" in f.output


def test_reply_to_unknown_resource_errors(data_dir):
    r = _run(data_dir, "reply", "issue-nope", "hello", "--agent", "nub-0622")
    assert r.exit_code != 0
    assert "no folio for reference" in r.output


def test_reply_ambiguous_resource_maps_to_clean_error(data_dir, monkeypatch):
    # An ambiguous short-hash resource can't be forced deterministically (it needs
    # an 8-hex prefix collision), so drive the CLI's except branch directly: a
    # raised AmbiguousReference must surface as a clean error listing candidates,
    # never an uncaught traceback.
    from skein.station import AmbiguousReference, Station

    def boom(self, *a, **k):
        raise AmbiguousReference("sha256::ab", ["sha256::ab1234", "sha256::ab5678"])

    monkeypatch.setattr(Station, "reply", boom)
    r = _run(data_dir, "reply", "sha256::ab", "hi", "--agent", "nub-0622")
    assert r.exit_code != 0
    assert "sha256::ab1234" in r.output


def test_reply_requires_agent_identity(data_dir, monkeypatch):
    # CliRunner(env=...) merges over os.environ, so an ambient $SKEIN_AGENT/
    # $SKEIN_AGENT (likely for an actual skein user) would mask this. Clear both.
    monkeypatch.delenv("SKEIN_AGENT", raising=False)
    monkeypatch.delenv("SKEIN_AGENT", raising=False)
    issue = _post(data_dir, "issue", "bug", "broke")
    r = _run(data_dir, "reply", issue, "anon")  # no --agent, no env
    assert r.exit_code != 0
    assert "no agent identity" in r.output


def test_reply_uses_env_agent(data_dir, monkeypatch):
    monkeypatch.delenv("SKEIN_AGENT", raising=False)
    monkeypatch.delenv("SKEIN_AGENT", raising=False)
    issue = _post(data_dir, "issue", "bug", "broke")
    env = {"SKEIN_AGENT": "morse-0621"}
    r = _run(data_dir, "reply", issue, "from env", env=env)
    assert r.exit_code == 0, r.output
    f = _run(data_dir, "folio", issue)
    assert "morse-0621: from env" in f.output


def test_multiple_replies_render_in_order_on_folio(data_dir):
    issue = _post(data_dir, "issue", "bug", "broke")
    _run(data_dir, "reply", issue, "first", "--agent", "a")
    _run(data_dir, "reply", issue, "second", "--agent", "b")
    f = _run(data_dir, "folio", issue)
    assert "Replies (2):" in f.output
    assert f.output.index("first") < f.output.index("second")


def test_thread_view_shows_reply_content(data_dir):
    issue = _post(data_dir, "issue", "bug", "broke")
    _run(data_dir, "reply", issue, "the comment text", "--agent", "nub-0622")
    t = _run(data_dir, "thread", issue)
    assert "reply" in t.output
    assert "the comment text" in t.output


def test_registered_agent_reply_shows_author_in_folio(data_dir):
    # A reply from a registered agent records the author (weaver), shown verbatim
    # in the folio's Replies section even though from_id is the agent folio hash.
    issue = _post(data_dir, "issue", "bug", "broke")
    r = _run(data_dir, "ignite", "--name", "nub-0622", "--agent", "nub-0622")
    assert r.exit_code == 0, r.output
    _run(data_dir, "reply", issue, "claimed", "--agent", "nub-0622")
    f = _run(data_dir, "folio", issue)
    assert "nub-0622: claimed" in f.output
