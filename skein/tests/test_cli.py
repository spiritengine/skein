"""Tests for the Slice 3 daily-driver CLI (via Click's CliRunner)."""

import json

import pytest
from click.testing import CliRunner

from skein.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def data_dir(tmp_path):
    return str(tmp_path / ".skein")


def run(runner, data_dir, *args, input=None):
    return runner.invoke(cli, ["--data-dir", data_dir, *args], input=input)


def test_site_create_and_list(runner, data_dir):
    r = run(runner, data_dir, "site", "create", "proj", "--purpose", "the project")
    assert r.exit_code == 0 and "site created: proj" in r.output
    r = run(runner, data_dir, "sites")
    assert r.exit_code == 0 and "proj - the project" in r.output


def test_site_create_is_idempotent(runner, data_dir):
    run(runner, data_dir, "site", "create", "proj", "--purpose", "p")
    r = run(runner, data_dir, "site", "create", "proj", "--purpose", "p")
    assert "site exists: proj" in r.output


def test_site_create_on_agent_name_is_clean_error(runner, data_dir):
    # A Stage-2 agent name slugs the same table sites use; site create on that
    # slug raises SlugCollision, which the CLI must present cleanly, not as a
    # traceback (r5 finding 2).
    assert run(runner, data_dir, "register", "foo").exit_code == 0
    r = run(runner, data_dir, "site", "create", "foo")
    assert r.exit_code != 0
    assert "foo" in r.output and "non-site folio" in r.output
    assert "Traceback" not in r.output


def test_post_prints_hash_and_appears_in_folios(runner, data_dir):
    run(runner, data_dir, "site", "create", "proj")
    r = run(runner, data_dir, "post", "finding", "proj", "Found a bug", "--content", "details")
    assert r.exit_code == 0
    h = r.output.strip()
    assert h.startswith("sha256::")
    r = run(runner, data_dir, "folios", "proj")
    assert "finding Found a bug" in r.output and h[:20] in r.output


def test_post_unknown_site_errors(runner, data_dir):
    r = run(runner, data_dir, "post", "finding", "ghost", "x")
    assert r.exit_code != 0 and "no site named 'ghost'" in r.output


def test_post_content_from_stdin(runner, data_dir):
    run(runner, data_dir, "site", "create", "proj")
    r = run(runner, data_dir, "post", "note", "proj", "title", "--content", "-", input="from stdin\n")
    h = r.output.strip()
    r = run(runner, data_dir, "folio", h, "--json")
    assert json.loads(r.output)["content"] == "from stdin\n"


def test_folio_read_by_prefix(runner, data_dir):
    run(runner, data_dir, "site", "create", "proj")
    h = run(runner, data_dir, "post", "finding", "proj", "Title here").output.strip()
    r = run(runner, data_dir, "folio", h[:18])
    assert r.exit_code == 0 and "Title here" in r.output and h in r.output


def test_folio_missing_ref_errors(runner, data_dir):
    run(runner, data_dir, "site", "create", "proj")
    r = run(runner, data_dir, "folio", "sha256::nope")
    assert r.exit_code != 0 and "no folio for reference" in r.output


def test_search_finds_content(runner, data_dir):
    run(runner, data_dir, "site", "create", "proj")
    run(runner, data_dir, "post", "finding", "proj", "Cache thing", "--content", "collision here")
    r = run(runner, data_dir, "search", "collision")
    assert "Cache thing" in r.output
    r = run(runner, data_dir, "search", "absent")
    assert "no folios match" in r.output


def test_folios_type_filter(runner, data_dir):
    run(runner, data_dir, "site", "create", "proj")
    run(runner, data_dir, "post", "finding", "proj", "a finding")
    run(runner, data_dir, "post", "notion", "proj", "a notion")
    r = run(runner, data_dir, "folios", "proj", "--type", "finding")
    assert "a finding" in r.output and "a notion" not in r.output


def test_thread_shows_membership_and_link(runner, data_dir):
    run(runner, data_dir, "site", "create", "proj")
    h = run(runner, data_dir, "post", "finding", "proj", "focus folio").output.strip()
    r = run(runner, data_dir, "thread", h)
    assert r.exit_code == 0 and "within site proj" in r.output


def test_thread_on_site_says_contains_not_within(runner, data_dir):
    # fell-r2 MAJOR: running thread on the SITE must report it contains its
    # member, not that the site is "within" its member (containment inverted).
    run(runner, data_dir, "site", "create", "proj")
    run(runner, data_dir, "post", "finding", "proj", "a member")
    site_hash = json.loads(run(runner, data_dir, "sites", "--json").output)[0]["folio"]["content_hash"]
    r = run(runner, data_dir, "thread", site_hash)
    assert r.exit_code == 0
    assert "contains finding a member" in r.output
    assert "within" not in r.output  # the site is not "within" anything here


def test_status_and_close_round_trip(runner, data_dir):
    run(runner, data_dir, "site", "create", "proj")
    h = run(runner, data_dir, "post", "finding", "proj", "a finding").output.strip()
    # set a custom status, read it back on the folio detail
    r = run(runner, data_dir, "status", h, "investigating")
    assert r.exit_code == 0 and "status set: investigating" in r.output
    assert "status: investigating" in run(runner, data_dir, "folio", h).output
    # close is shorthand for status closed, and latest wins
    run(runner, data_dir, "close", h)
    assert "status: closed" in run(runner, data_dir, "folio", h).output


def test_folio_unset_status_shows_open(runner, data_dir):
    # A freshly posted folio (no status thread) shows 'open' on the detail and
    # in --json — matching legacy, not a blank/None.
    run(runner, data_dir, "site", "create", "proj")
    h = run(runner, data_dir, "post", "finding", "proj", "a finding").output.strip()
    assert "status: open" in run(runner, data_dir, "folio", h).output
    assert json.loads(run(runner, data_dir, "folio", h, "--json").output)["status"] == "open"


def test_status_unknown_ref_errors(runner, data_dir):
    run(runner, data_dir, "site", "create", "proj")
    r = run(runner, data_dir, "close", "sha256::deadbeef")
    assert r.exit_code != 0 and "no folio for reference" in r.output


def test_status_ambiguous_ref_errors(runner, data_dir):
    from skein.store import SkeinStore

    run(runner, data_dir, "site", "create", "proj")  # initializes the store
    shared = "sha256::abcdef000000"
    with SkeinStore(data_dir) as store:
        for tail in ("1111", "2222"):
            store.conn.execute(
                "INSERT INTO folios (content_hash, type, title, content) VALUES (?,?,?,?)",
                (shared + tail, "finding", "t", "c"),
            )
        store.conn.commit()
    r = run(runner, data_dir, "close", shared)
    assert r.exit_code != 0
    assert "is ambiguous" in r.output
    assert shared + "1111" in r.output and shared + "2222" in r.output


def test_folio_json_includes_status(runner, data_dir):
    run(runner, data_dir, "site", "create", "proj")
    h = run(runner, data_dir, "post", "finding", "proj", "x").output.strip()
    run(runner, data_dir, "close", h)
    assert json.loads(run(runner, data_dir, "folio", h, "--json").output)["status"] == "closed"


def test_json_output_is_valid(runner, data_dir):
    run(runner, data_dir, "site", "create", "proj")
    run(runner, data_dir, "post", "finding", "proj", "x")
    r = run(runner, data_dir, "folios", "proj", "--json")
    assert isinstance(json.loads(r.output), list)
    r = run(runner, data_dir, "sites", "--json")
    assert json.loads(r.output)[0]["slug"] == "proj"
