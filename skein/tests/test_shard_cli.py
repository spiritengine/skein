"""Integration test for the ported shard CLI (agent-coordination port, Stage 1).

Drives the full spawn → tender → triage → merge → cleanup loop against a real
temp git repo and a local content-hash station — no server. Proves the rewired
touchpoints (tender folio via Station.post + JSON envelope, triage reading the
tender back, the merge auto-tender) work end to end off HTTP.
"""

import subprocess

import pytest
from click.testing import CliRunner

from skein import shard as shard_engine
from skein.cli import cli

pytest.importorskip("git")


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def project(tmp_path):
    """A temp git repo with one commit on master, wired as the shard project root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "init")

    saved = shard_engine._PROJECT_ROOT
    shard_engine.set_project_root(str(repo))
    try:
        yield repo
    finally:
        # Restore engine global so we don't leak into other tests.
        shard_engine._PROJECT_ROOT = saved
        shard_engine._WORKTREES_DIR = (saved / "worktrees") if saved else None


def _run(data_dir, *args):
    return CliRunner().invoke(cli, ["--data-dir", str(data_dir), *args])


def test_spawn_tender_triage_merge_cleanup_round_trip(project, tmp_path):
    data_dir = tmp_path / ".skein"

    # A site for the tender to land in.
    r = _run(data_dir, "site", "create", "shard-review", "--purpose", "shard tenders")
    assert r.exit_code == 0, r.output

    # Spawn a shard worktree.
    r = _run(data_dir, "shard", "spawn", "--agent", "tester", "--description", "demo")
    assert r.exit_code == 0, r.output
    # Recover the worktree name from the engine (single active shard).
    shards = shard_engine.list_shards(active_only=True)
    assert len(shards) == 1
    worktree_name = shards[0]["worktree_name"]
    worktree_path = shards[0]["worktree_path"]

    # Do real work in the worktree and commit it.
    (project / "worktrees")  # ensure path exists conceptually
    new_file = f"{worktree_path}/feature.py"
    with open(new_file, "w") as f:
        f.write("print('feature')\n")
    _git(worktree_path, "add", "feature.py")
    _git(worktree_path, "commit", "-qm", "add feature")

    # Tender it with a confidence rating.
    r = _run(
        data_dir,
        "shard",
        "tender",
        worktree_name,
        "--site",
        "shard-review",
        "--summary",
        "Added a feature",
        "--confidence",
        "7",
    )
    assert r.exit_code == 0, r.output
    assert "Tendered SHARD" in r.output
    assert "Confidence: 7/10" in r.output

    # Triage must surface that tender against the worktree, confidence intact.
    r = _run(data_dir, "shard", "triage", "--json")
    assert r.exit_code == 0, r.output
    import json

    triage = json.loads(r.output)
    entry = next(e for e in triage if e["worktree_name"] == worktree_name)
    assert entry["confidence"] == 7
    assert entry["tender_id"] is not None

    # Merge: clean integration, auto-tender posted + closed, worktree cleaned up.
    r = _run(data_dir, "shard", "merge", worktree_name)
    assert r.exit_code == 0, r.output
    assert "Clean integration" in r.output
    # The feature landed on master.
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=project, capture_output=True, text=True
    ).stdout
    assert "add feature" in log
    # merge_shard removes the worktree itself; no shards remain.
    assert shard_engine.list_shards(active_only=True) == []

    # The merge auto-tender was posted to the derived/fallback site and closed.
    tenders = _run(data_dir, "folios", "shard-review", "--type", "tender")
    assert tenders.exit_code == 0, tenders.output


def test_tender_rejects_a_non_site_slug(project, tmp_path):
    """A slug pointing at a non-site folio (e.g. a Stage-2 agent name) is not a
    valid tender target — only type=site slugs are, matching legacy GET /sites."""
    from skein.station import Station

    data_dir = tmp_path / ".skein"
    _run(data_dir, "site", "create", "shard-review")

    # Register a slug that resolves to a non-site folio, as Stage 2 will for
    # agent names. Reuse the existing shard-review site folio as a stand-in
    # target and slug it under a non-site name via a brand-new agent folio.
    with Station(data_dir) as st:
        agent_hash = st.store.create_folio({"type": "agent", "title": "some-agent", "content": "x"})
        st.store.set_slug("some-agent", agent_hash)

    _run(data_dir, "shard", "spawn", "--agent", "tester")
    worktree_name = shard_engine.list_shards(active_only=True)[0]["worktree_name"]
    worktree_path = shard_engine.list_shards(active_only=True)[0]["worktree_path"]
    with open(f"{worktree_path}/g.py", "w") as fh:
        fh.write("y = 2\n")
    _git(worktree_path, "add", "g.py")
    _git(worktree_path, "commit", "-qm", "work")

    r = _run(data_dir, "shard", "tender", worktree_name, "--site", "some-agent")
    assert r.exit_code != 0
    assert "not found" in r.output


def test_spawn_thread_from_id_is_agent_weaver_is_operator(project, tmp_path):
    """The spawn tag thread is authored BY the operator ABOUT the shard's agent:
    from_id = the --agent value, weaver = the operator (SKEIN_AGENT)."""
    from skein.station import Station

    data_dir = tmp_path / ".skein"
    r = CliRunner().invoke(
        cli,
        ["--data-dir", str(data_dir), "shard", "spawn", "--agent", "shard-agent"],
        env={"SKEIN_AGENT": "operator-x"},
    )
    assert r.exit_code == 0, r.output

    with Station(data_dir) as st:
        tags = st.store.get_threads(type="tag")
    assert len(tags) == 1
    thread = tags[0]
    assert thread["from_id"] == "shard-agent"
    assert thread["weaver"] == "operator-x"
    assert thread["from_id"] != thread["weaver"]


def test_shards_shortcut_lists(project, tmp_path):
    """The hidden top-level `shards` alias mirrors `shard list`."""
    data_dir = tmp_path / ".skein"
    _run(data_dir, "shard", "spawn", "--agent", "tester")
    worktree_name = shard_engine.list_shards(active_only=True)[0]["worktree_name"]
    r = _run(data_dir, "shards")
    assert r.exit_code == 0, r.output
    assert worktree_name in r.output


def test_triage_picks_latest_tender_after_confidence_change(project, tmp_path):
    """A re-tender at a new confidence is a new folio; triage shows the newest."""
    data_dir = tmp_path / ".skein"
    _run(data_dir, "site", "create", "shard-review")

    _run(data_dir, "shard", "spawn", "--agent", "tester", "--description", "demo")
    worktree_name = shard_engine.list_shards(active_only=True)[0]["worktree_name"]
    worktree_path = shard_engine.list_shards(active_only=True)[0]["worktree_path"]
    with open(f"{worktree_path}/f.py", "w") as fh:
        fh.write("x = 1\n")
    _git(worktree_path, "add", "f.py")
    _git(worktree_path, "commit", "-qm", "work")

    # First tender at confidence 3, then a re-tender at confidence 9.
    _run(data_dir, "shard", "tender", worktree_name, "--site", "shard-review", "--confidence", "3")
    _run(data_dir, "shard", "tender", worktree_name, "--site", "shard-review", "--confidence", "9")

    import json

    r = _run(data_dir, "shard", "triage", "--json")
    triage = json.loads(r.output)
    entry = next(e for e in triage if e["worktree_name"] == worktree_name)
    assert entry["confidence"] == 9
