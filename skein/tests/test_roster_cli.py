"""Stage 2 CLI — ignite/ready/torch/complete, the name guard, and chain yields.

Drives the ported lifecycle verbs through the click CLI against a local station,
no server — the surface mill and agents actually call.
"""

import pytest
from click.testing import CliRunner

from skein.cli import cli
from skein import roster_cli


def _run(data_dir, *args, env=None):
    return CliRunner().invoke(cli, ["--data-dir", str(data_dir), *args], env=env)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / ".skein"


def test_ignite_ready_torch_complete_round_trip(data_dir):
    r = _run(data_dir, "ignite", "--name", "nub-0622", "--message", "stage 2")
    assert r.exit_code == 0, r.output
    assert "You are: nub-0622" in r.output

    r = _run(data_dir, "ready", "--agent", "nub-0622")
    assert r.exit_code == 0, r.output
    assert "nub-0622: active" in r.output

    r = _run(data_dir, "torch", "--agent", "nub-0622")
    assert r.exit_code == 0, r.output
    assert "nub-0622: retiring" in r.output

    r = _run(data_dir, "complete", "--agent", "nub-0622")
    assert r.exit_code == 0, r.output
    assert "nub-0622: retired" in r.output


def test_agent_identity_from_env(data_dir):
    env = {"SKEIN_AGENT": "morse-0621"}
    assert _run(data_dir, "ignite", "--name", "morse-0621", env=env).exit_code == 0
    # ready with no --agent falls back to $SKEIN_AGENT.
    r = _run(data_dir, "ready", env=env)
    assert r.exit_code == 0, r.output
    assert "morse-0621: active" in r.output


def test_name_collision_rejected_via_cli(data_dir):
    assert _run(data_dir, "ignite", "--name", "nub-0622", "--agent", "alice").exit_code == 0
    r = _run(data_dir, "ignite", "--name", "nub-0622", "--agent", "bob")
    assert r.exit_code != 0
    assert "already taken" in r.output


def test_ready_twice_rejected_via_cli(data_dir):
    _run(data_dir, "ignite", "--name", "nub-0622")
    _run(data_dir, "ready", "--agent", "nub-0622")
    r = _run(data_dir, "ready", "--agent", "nub-0622")
    assert r.exit_code != 0
    assert "illegal lifecycle transition" in r.output


def test_no_agent_identity_is_clear_error(data_dir):
    r = _run(data_dir, "ready")  # no --agent, no env
    assert r.exit_code != 0
    assert "no agent identity" in r.output


def _seed_ambiguous_agents(data_dir):
    """Two type=agent folios sharing a short-hash prefix → an ambiguous ref."""
    from skein.store import SkeinStore

    _run(data_dir, "ignite", "--name", "real-0622")  # initialize the store
    shared = "sha256::abcdef000000"
    with SkeinStore(data_dir) as store:
        for tail in ("1111", "2222"):
            store.conn.execute(
                "INSERT INTO folios (content_hash, type, title, content) VALUES (?,?,?,?)",
                (shared + tail, "agent", "t", "c"),
            )
        store.conn.commit()
    return shared


def test_ready_ambiguous_agent_is_clean_error(data_dir):
    shared = _seed_ambiguous_agents(data_dir)
    r = _run(data_dir, "ready", "--agent", shared)
    assert r.exit_code != 0
    assert "is ambiguous" in r.output
    assert shared + "1111" in r.output and shared + "2222" in r.output
    assert "Traceback" not in r.output


def test_complete_ambiguous_agent_is_clean_error(data_dir):
    shared = _seed_ambiguous_agents(data_dir)
    r = _run(data_dir, "complete", "--agent", shared)
    assert r.exit_code != 0
    assert "is ambiguous" in r.output
    assert "Traceback" not in r.output


def test_complete_stores_and_reads_back_a_chain_yield(data_dir):
    env = {"SKEIN_CHAIN_ID": "chain-7", "SKEIN_CHAIN_TASK": "task-a"}
    _run(data_dir, "ignite", "--name", "nub-0622")
    _run(data_dir, "ready", "--agent", "nub-0622")
    _run(data_dir, "torch", "--agent", "nub-0622")
    r = _run(
        data_dir, "complete", "--agent", "nub-0622",
        "--yield-status", "complete", "--yield-outcome", "did the work",
        env=env,
    )
    assert r.exit_code == 0, r.output
    assert "Yield stored: sack-" in r.output

    # The next task reads the chain's sack back.
    r = _run(data_dir, "chain", "yields", "chain-7")
    assert r.exit_code == 0, r.output
    assert "complete: did the work" in r.output
    assert "task task-a" in r.output


def test_complete_without_chain_just_retires(data_dir):
    _run(data_dir, "ignite", "--name", "nub-0622")
    _run(data_dir, "ready", "--agent", "nub-0622")
    _run(data_dir, "torch", "--agent", "nub-0622")
    r = _run(data_dir, "complete", "--agent", "nub-0622")
    assert r.exit_code == 0, r.output
    assert "Yield stored" not in r.output
    assert "nub-0622: retired" in r.output


def test_complete_with_summary_posts_to_working_site(data_dir):
    _run(data_dir, "site", "create", "proj", "--purpose", "the work")
    _run(data_dir, "ignite", "--name", "nub-0622")
    _run(data_dir, "ready", "--agent", "nub-0622")
    _run(data_dir, "post", "finding", "proj", "did a thing", "--by", "nub-0622")
    _run(data_dir, "torch", "--agent", "nub-0622")
    r = _run(data_dir, "complete", "--agent", "nub-0622", "--summary", "wrapped up the audit")
    assert r.exit_code == 0, r.output
    # The summary landed in the agent's working site (proj), not the roster.
    r = _run(data_dir, "folios", "proj", "--type", "summary")
    assert "wrapped up the audit" in r.output


def test_roster_and_activity_listing(data_dir):
    _run(data_dir, "ignite", "--name", "nub-0622", "--type", "claude-code")
    _run(data_dir, "ready", "--agent", "nub-0622")

    r = _run(data_dir, "roster")
    assert r.exit_code == 0, r.output
    assert "nub-0622 — active" in r.output
    assert "[claude-code]" in r.output

    r = _run(data_dir, "activity", "--json")
    assert r.exit_code == 0, r.output
    assert '"name": "nub-0622"' in r.output


def test_register_and_identify(data_dir):
    r = _run(data_dir, "register", "agent-007", "--type", "claude-code")
    assert r.exit_code == 0, r.output
    assert "Registered: agent-007" in r.output

    r = _run(data_dir, "identify", "agent-007", "--eval")
    assert r.exit_code == 0, r.output
    assert r.output.strip() == "export SKEIN_AGENT=agent-007"


def test_chain_yield_show(data_dir):
    env = {"SKEIN_CHAIN_ID": "chain-9", "SKEIN_CHAIN_TASK": "t1"}
    _run(data_dir, "ignite", "--name", "w")
    _run(data_dir, "ready", "--agent", "w")
    _run(data_dir, "torch", "--agent", "w")
    r = _run(data_dir, "complete", "--agent", "w", "--yield-outcome", "ok", env=env)
    sack_id = [w for w in r.output.split() if w.startswith("sack-")][0]
    r = _run(data_dir, "chain", "yield", sack_id)
    assert r.exit_code == 0, r.output
    assert sack_id in r.output
    assert "outcome: ok" in r.output


# --- finding 1: complete is lifecycle-guarded BEFORE any durable side effect --


def test_illegal_complete_stores_no_yield_no_summary(data_dir):
    # complete from 'active' (skipping torch) is illegal; with a chain id set and a
    # summary requested, neither the yield nor the summary may become durable.
    env = {"SKEIN_CHAIN_ID": "chain-7", "SKEIN_CHAIN_TASK": "task-a"}
    _run(data_dir, "ignite", "--name", "nub-0622")
    _run(data_dir, "ready", "--agent", "nub-0622")  # now 'active', not 'retiring'
    r = _run(
        data_dir, "complete", "--agent", "nub-0622",
        "--yield-outcome", "premature", "--summary", "should not persist",
        env=env,
    )
    assert r.exit_code != 0
    assert "illegal lifecycle transition" in r.output
    # No yield landed in the chain sack.
    r = _run(data_dir, "chain", "yields", "chain-7")
    assert "no yields for chain chain-7" in r.output
    # No summary folio minted in the roster.
    r = _run(data_dir, "folios", "roster", "--type", "summary")
    assert "should not persist" not in r.output


def test_retrying_failed_complete_does_not_accumulate_yields(data_dir):
    # Repeated illegal completes must not stack duplicate yields (the bug: the
    # yield was written before the guard, so every retry appended another).
    env = {"SKEIN_CHAIN_ID": "chain-7", "SKEIN_CHAIN_TASK": "task-a"}
    _run(data_dir, "ignite", "--name", "nub-0622")
    _run(data_dir, "ready", "--agent", "nub-0622")
    for _ in range(3):
        r = _run(data_dir, "complete", "--agent", "nub-0622",
                 "--yield-outcome", "x", env=env)
        assert r.exit_code != 0
    r = _run(data_dir, "chain", "yields", "chain-7")
    assert "no yields for chain chain-7" in r.output


# --- finding 2: identify-emitted agent-id round-trips through lifecycle verbs --


def test_identify_agent_id_round_trips_when_name_differs(data_dir):
    # register --name X AGENT_ID, then identify AGENT_ID; the emitted env value
    # must resolve in ready/torch/complete even though it is not the name slug.
    assert _run(data_dir, "register", "agent-007", "--name", "bond").exit_code == 0
    r = _run(data_dir, "identify", "agent-007", "--eval")
    env = {"SKEIN_AGENT": "agent-007"}
    assert r.output.strip() == "export SKEIN_AGENT=agent-007"

    r = _run(data_dir, "ready", env=env)
    assert r.exit_code == 0, r.output
    assert "agent-007: active" in r.output
    r = _run(data_dir, "torch", env=env)
    assert r.exit_code == 0, r.output
    r = _run(data_dir, "complete", env=env)
    assert r.exit_code == 0, r.output
    assert "agent-007: retired" in r.output


def test_complete_collects_artifacts_by_agent_id_not_name(data_dir):
    # The chain yield's artifacts come from folios authored under the agent-id
    # (created_by), not the human name. Work posted with --by agent-007 must be
    # gathered even though the agent is addressed by name 'bond'.
    env = {"SKEIN_CHAIN_ID": "chain-2", "SKEIN_CHAIN_TASK": "t1",
           "SKEIN_AGENT": "agent-007"}
    _run(data_dir, "site", "create", "proj", "--purpose", "work")
    _run(data_dir, "register", "agent-007", "--name", "bond")
    _run(data_dir, "ready", env=env)
    _run(data_dir, "post", "finding", "proj", "did a thing", "--by", "agent-007")
    _run(data_dir, "torch", env=env)
    r = _run(data_dir, "complete", env=env)
    assert r.exit_code == 0, r.output
    sack_id = [w for w in r.output.split() if w.startswith("sack-")][0]
    r = _run(data_dir, "chain", "yield", sack_id)
    assert r.exit_code == 0, r.output
    assert "artifacts:" in r.output  # the proj finding was collected


# --- finding 5: complete summary post does not swallow unexpected errors ------


def test_complete_summary_falls_back_to_roster_when_no_working_site(data_dir):
    # An agent with no authored work has no working site; the summary still lands
    # (in the roster), proving the post is not silently dropped.
    _run(data_dir, "ignite", "--name", "nub-0622")
    _run(data_dir, "ready", "--agent", "nub-0622")
    _run(data_dir, "torch", "--agent", "nub-0622")
    r = _run(data_dir, "complete", "--agent", "nub-0622", "--summary", "wrapped up")
    assert r.exit_code == 0, r.output
    r = _run(data_dir, "folios", "roster", "--type", "summary")
    assert "wrapped up" in r.output


def test_complete_summary_surfaces_unexpected_post_error(data_dir, monkeypatch):
    # A non-UnknownSite failure in the summary post must surface, not be swallowed
    # by a bare except (the old bug reported success while losing the summary).
    _run(data_dir, "ignite", "--name", "nub-0622")
    _run(data_dir, "ready", "--agent", "nub-0622")
    _run(data_dir, "torch", "--agent", "nub-0622")

    from skein.station import Station

    def boom(self, *a, **k):
        raise RuntimeError("post exploded")

    monkeypatch.setattr(Station, "post", boom)
    r = _run(data_dir, "complete", "--agent", "nub-0622", "--summary", "x")
    assert r.exit_code != 0
    assert isinstance(r.exception, RuntimeError)


# --- finding 6: auto-name generation is collision-safe (retry on rejection) ---


def test_ignite_auto_name_retries_on_collision(data_dir, monkeypatch):
    # An auto-generated name that collides with a name already held under a
    # DIFFERENT agent-id must be rejected by the guard, and ignite must regenerate
    # and retry rather than fail. (A collision under the SAME agent-id is a
    # re-ignite, not a steal — the random-token fallback keeps that from happening
    # to independent agents in the first place.)
    _run(data_dir, "register", "other-agent", "--name", "dupe-0622")
    names = iter(["dupe-0622", "fresh-0622"])
    monkeypatch.setattr(roster_cli, "_generate_name", lambda existing: next(names))

    r = _run(data_dir, "ignite")  # no --name/--agent ⇒ auto-named
    assert r.exit_code == 0, r.output
    assert "You are: fresh-0622" in r.output
    rr = _run(data_dir, "roster")
    assert "fresh-0622" in rr.output


# --- finding A: complete is atomic (transition + yield + summary one unit) -----


def test_complete_yield_failure_rolls_back_transition(data_dir, monkeypatch):
    # A transition that commits followed by a failing yield used to lose the
    # hand-off: the agent was already 'retired', so a retry hit
    # IllegalAgentTransition. Folding the three writes into one transaction means a
    # yield failure rolls the transition back — the agent stays 'retiring', stores
    # NO yield, mints NO summary, and a subsequent complete succeeds.
    env = {"SKEIN_CHAIN_ID": "chain-A", "SKEIN_CHAIN_TASK": "t1"}
    _run(data_dir, "ignite", "--name", "atom-0622")
    _run(data_dir, "ready", "--agent", "atom-0622")
    _run(data_dir, "torch", "--agent", "atom-0622")

    from skein.station import Station

    def boom(self, *a, **k):
        raise RuntimeError("yield write exploded")

    monkeypatch.setattr(Station, "store_chain_yield", boom)
    r = _run(data_dir, "complete", "--agent", "atom-0622",
             "--yield-outcome", "x", "--summary", "should not persist", env=env)
    assert r.exit_code != 0
    assert isinstance(r.exception, RuntimeError)
    # Transition rolled back: still 'retiring', not 'retired'.
    assert "atom-0622 — retiring" in _run(data_dir, "roster").output
    # No yield landed, no summary minted.
    assert "no yields for chain chain-A" in _run(data_dir, "chain", "yields", "chain-A").output
    assert "should not persist" not in _run(data_dir, "folios", "roster", "--type", "summary").output

    # Cleanly retryable: a real complete now retires the agent and stores the yield.
    monkeypatch.undo()
    r = _run(data_dir, "complete", "--agent", "atom-0622", "--yield-outcome", "x", env=env)
    assert r.exit_code == 0, r.output
    assert "atom-0622: retired" in r.output
    assert "Yield stored: sack-" in r.output


def test_complete_summary_failure_rolls_back_transition(data_dir, monkeypatch):
    # The summary post is the last write under the one transaction; if it fails the
    # transition rolls back too, leaving the agent 'retiring' and retryable.
    _run(data_dir, "ignite", "--name", "atom2-0622")
    _run(data_dir, "ready", "--agent", "atom2-0622")
    _run(data_dir, "torch", "--agent", "atom2-0622")

    from skein.station import Station

    def boom(self, *a, **k):
        raise RuntimeError("summary post exploded")

    monkeypatch.setattr(Station, "post", boom)
    r = _run(data_dir, "complete", "--agent", "atom2-0622", "--summary", "nope")
    assert r.exit_code != 0
    assert "atom2-0622 — retiring" in _run(data_dir, "roster").output
    assert "nope" not in _run(data_dir, "folios", "roster", "--type", "summary").output

    monkeypatch.undo()
    r = _run(data_dir, "complete", "--agent", "atom2-0622", "--summary", "done")
    assert r.exit_code == 0, r.output
    assert "atom2-0622: retired" in r.output


# --- finding B: disjoint name / agent-id namespaces (no masking) -------------


def test_register_rejects_handle_crossover_via_cli(data_dir):
    # Alice's name must not be claimable as a second agent's agent-id, or the
    # handle would mask one agent behind the other in resolution.
    assert _run(data_dir, "register", "alice", "--name", "bond").exit_code == 0
    r = _run(data_dir, "register", "bond", "--name", "bob")  # agent-id == alice's name
    assert r.exit_code != 0
    assert "collides" in r.output


# --- finding C: transition reports status from the under-lock returned hash ---


def test_transition_reports_status_from_returned_hash_not_stale_resolve(data_dir, monkeypatch):
    # ready/torch resolve the agent once outside the lock; the lifecycle method
    # re-resolves UNDER the lock and returns the live hash. A concurrent re-ignite
    # can make those differ, so the CLI must print the status of the hash the
    # transition actually moved (the return), not the pre-resolve. Simulate the
    # split: the pre-resolve hands back a STALE folio, the under-lock resolve the
    # LIVE one; the printed status must be the live folio's.
    _run(data_dir, "register", "stale-one", "--name", "stale-one")
    _run(data_dir, "register", "live-one", "--name", "live-one")

    from skein.station import Station

    probe = Station(str(data_dir))
    stale_hash = probe._resolve_agent("stale-one")
    live_hash = probe._resolve_agent("live-one")
    probe.close()

    calls = {"n": 0}

    def split_resolve(self, ref):
        calls["n"] += 1
        return stale_hash if calls["n"] == 1 else live_hash

    monkeypatch.setattr(Station, "_resolve_agent", split_resolve)
    r = _run(data_dir, "ready", "--agent", "ignored")
    assert r.exit_code == 0, r.output
    # The live folio is the one the transition moved → it reads 'active'. Had the
    # CLI used the stale pre-resolve it would have printed 'orienting'.
    assert "active" in r.output
    assert "orienting" not in r.output

    monkeypatch.undo()
    roster = _run(data_dir, "roster").output
    assert "live-one — active" in roster      # the moved folio
    assert "stale-one — orienting" in roster   # untouched


# --- r5 finding 3: under-lock re-resolve failures map to clean CLI errors -----


def test_transition_ambiguous_at_locked_resolve_is_clean_error(data_dir, monkeypatch):
    # _transition pre-resolves OUTSIDE the lock, then the mover re-resolves UNDER
    # the lock. A concurrent succession can make the name ambiguous between those
    # two resolves, so the locked re-resolve can raise AmbiguousReference even
    # though the pre-resolve succeeded. That must surface as a clean error, not a
    # traceback. Simulate: pre-resolve succeeds, the under-lock resolve raises.
    _run(data_dir, "register", "worker", "--name", "worker")

    from skein.station import AmbiguousReference, Station

    probe = Station(str(data_dir))
    real_hash = probe._resolve_agent("worker")
    probe.close()

    calls = {"n": 0}

    def flaky_resolve(self, ref):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_hash  # pre-resolve outside the lock
        raise AmbiguousReference(ref, [real_hash, "sha256::other000000"])

    monkeypatch.setattr(Station, "_resolve_agent", flaky_resolve)
    r = _run(data_dir, "ready", "--agent", "worker")
    assert r.exit_code != 0
    assert "is ambiguous" in r.output
    assert "Traceback" not in r.output


def test_transition_unknown_at_locked_resolve_is_clean_error(data_dir, monkeypatch):
    # Same shape, but the under-lock re-resolve raises UnknownAgent (a concurrent
    # succession unbound the slug). The CLI must present the ignite hint cleanly.
    _run(data_dir, "register", "worker", "--name", "worker")

    from skein.station import Station, UnknownAgent

    probe = Station(str(data_dir))
    real_hash = probe._resolve_agent("worker")
    probe.close()

    calls = {"n": 0}

    def flaky_resolve(self, ref):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_hash
        raise UnknownAgent(ref)

    monkeypatch.setattr(Station, "_resolve_agent", flaky_resolve)
    r = _run(data_dir, "ready", "--agent", "worker")
    assert r.exit_code != 0
    assert "ignite" in r.output
    assert "Traceback" not in r.output
