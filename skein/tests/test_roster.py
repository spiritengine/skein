"""Stage 2 — roster, lifecycle, activity, name guard, and chain yields.

Exercises the store guards (register_slug CAS, sacks API) and the Station layer
(register_agent, the FSM transition guard, the activity aggregate, succession,
create_site collision) directly — the CLI is covered in test_roster_cli.py.
"""

import threading
from datetime import datetime, timedelta, timezone

import pytest

from skein.station import (
    ACTIVE_WINDOW_MINUTES,
    AgentHandleConflict,
    AgentNameTaken,
    AgentRenameRejected,
    IllegalAgentTransition,
    ROSTER_SITE,
    SlugCollision,
    Station,
    UnknownAgent,
)
from skein.store import SkeinNextStore


@pytest.fixture
def station(tmp_path):
    s = Station(data_dir=tmp_path / ".skein")
    yield s
    s.close()


@pytest.fixture
def store(tmp_path):
    s = SkeinNextStore(tmp_path / ".skein")
    yield s
    s.close()


# --- store: the guarded slug register (the D1 name guard primitive) ----------


def test_register_slug_binds_when_free(store):
    h = store.create_folio({"type": "agent", "title": "a", "created_by": "alice"})
    assert store.register_slug("nub", h) is True
    assert store.resolve_slug("nub") == h


def test_register_slug_idempotent_on_same_hash(store):
    h = store.create_folio({"type": "agent", "title": "a", "created_by": "alice"})
    assert store.register_slug("nub", h) is True
    assert store.register_slug("nub", h) is True  # same hash, still ours
    assert store.resolve_slug("nub") == h


def test_register_slug_rejects_a_steal(store):
    h_alice = store.create_folio({"type": "agent", "title": "a", "created_by": "alice"})
    h_bob = store.create_folio({"type": "agent", "title": "b", "created_by": "bob"})
    assert store.register_slug("nub", h_alice) is True
    assert store.register_slug("nub", h_bob) is False  # held by alice — refuse
    assert store.resolve_slug("nub") == h_alice  # unchanged, no silent rebind


def test_set_slug_still_last_write_wins(store):
    # set_slug is the deliberate-rebind tool and must NOT have become a guard.
    h1 = store.create_folio({"type": "site", "title": "s1"})
    h2 = store.create_folio({"type": "site", "title": "s2"})
    store.set_slug("x", h1)
    store.set_slug("x", h2)
    assert store.resolve_slug("x") == h2


# --- store: the sacks (chain yields) sidecar ---------------------------------


def test_add_and_get_yield_round_trip(store):
    assert store.add_yield(
        "sack-1", "chain-1", "task-1",
        agent_id="alice", status="complete", outcome="done",
        artifacts=["tender-x", "finding-y"], notes="hand off",
        metadata={"k": 1},
    ) is True
    y = store.get_yield("sack-1")
    assert y["chain_id"] == "chain-1"
    assert y["artifacts"] == ["tender-x", "finding-y"]
    assert y["metadata"] == {"k": 1}
    assert y["status"] == "complete"


def test_get_yield_missing_is_none(store):
    assert store.get_yield("nope") is None


def test_chain_yields_execution_order(store):
    store.add_yield("sack-a", "c", "t1", status="complete")
    store.add_yield("sack-b", "c", "t2", status="partial")
    store.add_yield("sack-c", "c", "t3", status="blocked")
    ids = [y["sack_id"] for y in store.get_chain_yields("c")]
    assert ids == ["sack-a", "sack-b", "sack-c"]  # (timestamp, id) ascending


def test_yields_by_status_and_by_agent(store):
    store.add_yield("s1", "c", "t1", agent_id="alice", status="blocked")
    store.add_yield("s2", "c", "t2", agent_id="bob", status="complete")
    store.add_yield("s3", "c", "t3", agent_id="alice", status="blocked")
    assert {y["sack_id"] for y in store.get_yields_by_status("blocked")} == {"s1", "s3"}
    assert {y["sack_id"] for y in store.get_agent_yields("alice")} == {"s1", "s3"}


def test_previous_yield(store):
    store.add_yield("s1", "c", "t1")
    store.add_yield("s2", "c", "t2")
    store.add_yield("s3", "c", "t3")
    assert store.get_previous_yield("c", "t2")["sack_id"] == "s1"
    assert store.get_previous_yield("c", "t1") is None  # first task, nothing before


def test_duplicate_sack_id_raises(store):
    store.add_yield("dup", "c", "t1")
    with pytest.raises(Exception):
        store.add_yield("dup", "c", "t2")  # UNIQUE sack_id


# --- station: lifecycle round-trip -------------------------------------------


def test_lifecycle_round_trip(station):
    h = station.register_agent("nub-0622", description="stage 2")
    assert station.status_of(h) == "orienting"
    station.agent_ready("nub-0622")
    assert station.status_of(h) == "active"
    station.agent_torch("nub-0622")
    assert station.status_of(h) == "retiring"
    station.agent_complete("nub-0622")
    assert station.status_of(h) == "retired"


def test_agent_folio_lives_in_roster_site(station):
    station.register_agent("nub-0622")
    members = station.folios_in_site(ROSTER_SITE, type="agent")
    assert [m["title"] for m in members] == ["nub-0622"]
    # The agent-id is the folio's created_by — the activity join key.
    assert members[0]["created_by"] == "nub-0622"


def test_register_is_idempotent_on_identical_fields(station):
    stamp = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    h1 = station.register_agent("nub-0622", created_at=stamp)
    h2 = station.register_agent("nub-0622", created_at=stamp)
    assert h1 == h2  # same content hash, collapsed to one folio
    assert len(station.list_agents()) == 1


# --- station: the NEW name guard ---------------------------------------------


def test_name_collision_rejected(station):
    station.register_agent("alice", name="nub-0622")
    with pytest.raises(AgentNameTaken):
        station.register_agent("bob", name="nub-0622")
    # The first holder keeps the name.
    agents = {a["name"]: a["created_by"] for a in station.list_agents()}
    assert agents["nub-0622"] == "alice"


def test_rejected_registration_leaves_no_partial_folio(station):
    station.register_agent("alice", name="nub-0622")
    before = station.store.count_folios()
    with pytest.raises(AgentNameTaken):
        station.register_agent("bob", name="nub-0622")
    # The whole register is one transaction — a rejected steal rolls back the
    # minted agent folio too.
    assert station.store.count_folios() == before


# --- station: the FSM guard --------------------------------------------------


@pytest.mark.parametrize(
    "sequence,illegal",
    [
        (["active", "active"], "active"),   # ready twice
        (["retired"], "retired"),           # orienting → retired (skip)
        (["active", "retired"], "retired"),  # active → retired (skip retiring)
    ],
)
def test_illegal_transitions_rejected(station, sequence, illegal):
    station.register_agent("nub-0622")
    moves = {"active": station.agent_ready, "retiring": station.agent_torch,
             "retired": station.agent_complete}
    with pytest.raises(IllegalAgentTransition):
        for step in sequence:
            moves[step]("nub-0622")


def test_cannot_go_backwards(station):
    station.register_agent("nub-0622")
    station.agent_ready("nub-0622")
    station.agent_torch("nub-0622")
    # retiring → active is not a legal edge.
    with pytest.raises(IllegalAgentTransition):
        station.agent_ready("nub-0622")


def test_transition_unknown_agent_raises(station):
    with pytest.raises(UnknownAgent):
        station.agent_ready("ghost")


def test_unknown_status_value_raises(station):
    station.register_agent("nub-0622")
    with pytest.raises(ValueError):
        station.transition_agent("nub-0622", "banana")


# --- station: create_site collision (finding-20260621-ccmy) ------------------


def test_create_site_rejects_agent_name_collision(station):
    station.register_agent("nub-0622")
    with pytest.raises(SlugCollision):
        station.create_site("nub-0622")


def test_create_site_still_idempotent_on_real_site(station):
    a = station.create_site("proj", purpose="p")
    b = station.create_site("proj", purpose="ignored")
    assert a == b


def test_register_rejects_name_equal_to_existing_site(station):
    station.create_site("shard-review", purpose="tenders")
    # An agent cannot claim a name already held by a site slug (symmetry).
    with pytest.raises(AgentNameTaken):
        station.register_agent("agent-x", name="shard-review")


def test_register_rejects_name_of_own_site_no_clobber(station):
    # A site the agent itself minted must NOT be clobbered by the re-ignite
    # succession path just because created_by matches — a site is not an agent.
    site_hash = station.create_site("proj", purpose="p", created_by="alice")
    with pytest.raises(AgentNameTaken):
        station.register_agent("alice", name="proj")
    # The site slug still points at the site folio, untouched.
    assert station.store.resolve_slug("proj") == site_hash
    assert station.get_site("proj") is not None


# --- station: re-ignite / succession (item 7) --------------------------------


def test_reignite_same_agent_threads_succession(station):
    t1 = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 22, 15, 0, tzinfo=timezone.utc)
    first = station.register_agent("nub-0622", created_at=t1)
    station.agent_ready("nub-0622")
    second = station.register_agent("nub-0622", created_at=t2)  # re-ignite
    assert second != first
    # The name now points at the new incarnation, reset to orienting.
    assert station.store.resolve_slug("nub-0622") == second
    assert station.status_of(second) == "orienting"
    # A succession edge links the new folio back to the prior one.
    graph = station.thread_graph(second)
    succ = [e for e in graph["outgoing"] if e["type"] == "succession"]
    assert len(succ) == 1 and succ[0]["to_id"] == first


def test_reignite_only_shows_current_incarnation_in_roster(station):
    t1 = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 22, 15, 0, tzinfo=timezone.utc)
    station.register_agent("nub-0622", created_at=t1)
    second = station.register_agent("nub-0622", created_at=t2)
    agents = station.list_agents()
    assert len(agents) == 1
    assert agents[0]["content_hash"] == second


# --- station: the activity aggregate -----------------------------------------


def test_activity_active_within_window(station):
    t = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    station.register_agent("worker")
    station.agent_ready("worker")
    station.post(type="finding", site=ROSTER_SITE, title="found", created_by="worker", created_at=t)
    inside = station.agent_activity(now=t + timedelta(minutes=ACTIVE_WINDOW_MINUTES - 1))
    rec = next(a for a in inside if a["name"] == "worker")
    assert rec["activity_status"] == "active"
    assert rec["folio_count"] == 1
    assert rec["working_site"] == ROSTER_SITE
    assert rec["last_folio_type"] == "finding"


def test_activity_idle_outside_window(station):
    t = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    station.register_agent("worker")
    station.agent_ready("worker")
    station.post(type="finding", site=ROSTER_SITE, title="found", created_by="worker", created_at=t)
    outside = station.agent_activity(now=t + timedelta(minutes=ACTIVE_WINDOW_MINUTES + 1))
    rec = next(a for a in outside if a["name"] == "worker")
    assert rec["activity_status"] == "idle"


def test_fresh_agent_is_idle_not_active(station):
    # A just-registered agent with no work folios is idle (its own agent folio and
    # the roster site it minted don't count as authored work).
    station.register_agent("worker")
    station.agent_ready("worker")
    rec = next(a for a in station.agent_activity() if a["name"] == "worker")
    assert rec["activity_status"] == "idle"
    assert rec["folio_count"] == 0


def test_retiring_agent_is_torching(station):
    t = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    station.register_agent("worker")
    station.agent_ready("worker")
    station.post(type="finding", site=ROSTER_SITE, title="x", created_by="worker", created_at=t)
    station.agent_torch("worker")
    # Even with a very recent folio, a retiring agent reads as torching.
    rec = next(a for a in station.agent_activity(now=t + timedelta(minutes=1)) if a["name"] == "worker")
    assert rec["activity_status"] == "torching"


def test_activity_status_filter(station):
    station.register_agent("a")
    station.register_agent("b")
    station.agent_ready("b")
    active = station.agent_activity(status="active")
    assert [r["name"] for r in active] == ["b"]


def test_activity_sorted_recent_first(station):
    early = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
    late = datetime(2026, 6, 22, 14, 0, tzinfo=timezone.utc)
    station.register_agent("old")
    station.agent_ready("old")
    station.register_agent("new")
    station.agent_ready("new")
    station.post(type="finding", site=ROSTER_SITE, title="o", created_by="old", created_at=early)
    station.post(type="finding", site=ROSTER_SITE, title="n", created_by="new", created_at=late)
    names = [a["name"] for a in station.agent_activity(now=late + timedelta(minutes=1))]
    assert names.index("new") < names.index("old")


# --- station: chain yields ---------------------------------------------------


def test_store_chain_yield_generates_id_and_reads_back(station):
    sid = station.store_chain_yield(
        "chain-9", "task-a", agent_id="worker", status="complete",
        outcome="did the work", artifacts=["sha256::abc"],
    )
    assert sid.startswith("sack-")
    y = station.chain_yield(sid)
    assert y["outcome"] == "did the work"
    assert y["artifacts"] == ["sha256::abc"]
    assert station.chain_yields("chain-9")[0]["sack_id"] == sid


def test_store_chain_yield_honors_explicit_sack_id(station):
    sid = station.store_chain_yield("c", "t", sack_id="sack-fixed")
    assert sid == "sack-fixed"
    assert station.chain_yield("sack-fixed") is not None


# --- station: complete_agent (atomic transition + yield + summary) -----------
#
# These exercise the atomicity guarantee directly at the service layer (finding
# A from r5): the whole hand-off lives in Station.complete_agent, not just the
# CLI, so it is available and testable here without going through Click.


def _retiring(station, name="worker"):
    """Bring a fresh agent to the 'retiring' state, ready for complete."""
    station.register_agent(name)
    station.agent_ready(name)
    station.agent_torch(name)


def test_complete_agent_legal_with_chain_stores_yield_and_returns_sack(station):
    _retiring(station, "worker")
    live, status, sack = station.complete_agent(
        "worker", chain_id="chain-1", task_id="t1", yield_outcome="did the work",
    )
    assert status == "retired"
    assert station.status_of(live) == "retired"
    assert sack and sack.startswith("sack-")
    assert station.chain_yield(sack)["outcome"] == "did the work"
    assert station.chain_yields("chain-1")[0]["sack_id"] == sack


def test_complete_agent_without_chain_returns_no_sack(station):
    _retiring(station, "worker")
    live, status, sack = station.complete_agent("worker")
    assert status == "retired"
    assert sack is None


def test_illegal_complete_stores_no_yield_no_summary(station):
    # complete from 'active' (skipping torch) is illegal; even with a chain id and
    # a summary requested, the FSM guard runs FIRST, so neither side effect lands.
    station.register_agent("worker")
    station.agent_ready("worker")  # still 'active', never torched
    with pytest.raises(IllegalAgentTransition):
        station.complete_agent(
            "worker", chain_id="chain-1", yield_outcome="premature",
            summary="should not persist",
        )
    assert station.chain_yields("chain-1") == []
    assert station.folios_in_site(ROSTER_SITE, type="summary") == []


def test_complete_yield_failure_rolls_back_transition(station, monkeypatch):
    # A yield write that fails must roll the transition back: the agent stays
    # 'retiring' (retryable), and no yield or summary becomes durable.
    _retiring(station, "worker")
    h = station._resolve_agent("worker")

    def boom(*a, **k):
        raise RuntimeError("yield write exploded")

    monkeypatch.setattr(station, "store_chain_yield", boom)
    with pytest.raises(RuntimeError):
        station.complete_agent(
            "worker", chain_id="chain-1", yield_outcome="x",
            summary="should not persist",
        )
    assert station.status_of(h) == "retiring"
    assert station.chain_yields("chain-1") == []
    assert station.folios_in_site(ROSTER_SITE, type="summary") == []

    # Cleanly retryable: a real complete now retires the agent and stores a yield.
    monkeypatch.undo()
    live, status, sack = station.complete_agent(
        "worker", chain_id="chain-1", yield_outcome="x",
    )
    assert status == "retired"
    assert len(station.chain_yields("chain-1")) == 1


def test_complete_summary_failure_rolls_back_transition(station, monkeypatch):
    # The summary post is the last write under the one transaction; a non-
    # UnknownSite failure must surface AND roll the transition back.
    _retiring(station, "worker")
    h = station._resolve_agent("worker")

    def boom(*a, **k):
        raise RuntimeError("summary post exploded")

    monkeypatch.setattr(station, "post", boom)
    with pytest.raises(RuntimeError):
        station.complete_agent("worker", summary="nope")
    assert station.status_of(h) == "retiring"

    monkeypatch.undo()
    live, status, sack = station.complete_agent("worker", summary="done")
    assert status == "retired"
    titles = [f["title"] for f in station.folios_in_site(ROSTER_SITE, type="summary")]
    assert "done" in titles


def test_retrying_failed_complete_does_not_accumulate_yields(station):
    # Repeated illegal completes (from 'active', skipping torch) must not stack
    # duplicate yields — the guard runs before the yield, so none are ever stored.
    station.register_agent("worker")
    station.agent_ready("worker")
    for _ in range(3):
        with pytest.raises(IllegalAgentTransition):
            station.complete_agent("worker", chain_id="chain-1", yield_outcome="x")
    assert station.chain_yields("chain-1") == []


# --- station: ensure_roster_site --------------------------------------------


def test_ensure_roster_site_idempotent(station):
    a = station.ensure_roster_site()
    b = station.ensure_roster_site()
    assert a == b
    assert station.get_site(ROSTER_SITE) is not None


# --- station: name != agent_id identity coherence (finding 2) ----------------


def test_resolve_agent_by_agent_id_when_name_differs(station):
    # register with a name distinct from the agent-id; the agent-id (the value
    # identify --eval emits) must round-trip back to the same agent folio.
    h = station.register_agent("agent-007", name="bond")
    assert station._resolve_agent("bond") == h          # by name slug
    assert station._resolve_agent("agent-007") == h     # by agent-id (created_by)


def test_activity_attributes_authored_folios_by_agent_id(station):
    # Authored work joins on created_by == agent-id, not the human name.
    station.register_agent("agent-007", name="bond")
    station.agent_ready("bond")
    t = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    station.post(type="finding", site=ROSTER_SITE, title="intel",
                 created_by="agent-007", created_at=t)
    rec = next(a for a in station.agent_activity(now=t + timedelta(minutes=1))
               if a["name"] == "bond")
    assert rec["agent_id"] == "agent-007"
    assert rec["folio_count"] == 1
    assert rec["activity_status"] == "active"


def test_resolve_agent_by_agent_id_picks_live_incarnation(station):
    # After a re-ignite, several agent folios share the agent-id; resolving by the
    # agent-id must return the live incarnation (the one the name slug points at).
    t1 = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 22, 15, 0, tzinfo=timezone.utc)
    station.register_agent("agent-007", name="bond", created_at=t1)
    second = station.register_agent("agent-007", name="bond", created_at=t2)
    assert station._resolve_agent("agent-007") == second
    assert station.store.resolve_slug("bond") == second


# --- station: disjoint name / agent-id namespaces (finding B) ----------------


def test_register_rejects_name_equal_to_other_agents_agent_id(station):
    # _resolve_agent tries the NAME slug before the AGENT-ID. If one agent's name
    # equalled another's agent-id, the handle would always resolve to the first and
    # mask the second. Here agent-id 'bond' is registered under a different name, so
    # 'bond' is NOT a slug — only the new disjoint-namespace guard (not the old
    # slug guard) rejects a second agent trying to take the NAME 'bond'.
    station.register_agent("bond", name="agent-007")
    assert station.store.resolve_slug("bond") is None  # 'bond' is not a name slug
    with pytest.raises(AgentHandleConflict):
        station.register_agent("alice", name="bond")


def test_register_rejects_agent_id_equal_to_other_agents_name(station):
    # The mirror crossover: an agent-id equal to a DIFFERENT agent's name slug.
    # 'bond' is agent-007's name, not anyone's agent-id, so again only the new
    # guard catches a second agent whose AGENT-ID is 'bond'.
    station.register_agent("agent-007", name="bond")
    with pytest.raises(AgentHandleConflict):
        station.register_agent("bond", name="bob")
    # The rejected register leaves no partial folio — bond still maps to agent-007.
    ids = {a["name"]: a["created_by"] for a in station.list_agents()}
    assert ids == {"bond": "agent-007"}


def test_register_allows_own_name_equal_to_own_agent_id(station):
    # The auto-ignite posture (name == agent-id, same agent) is NOT a crossover.
    h = station.register_agent("solo", name="solo")
    assert station._resolve_agent("solo") == h


def test_reignite_under_same_handle_survives_disjoint_guard(station):
    # A same-agent re-ignite (same id, same name) must remain a succession, not be
    # read as a different agent claiming a handle already in use as the other kind.
    t1 = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 22, 15, 0, tzinfo=timezone.utc)
    station.register_agent("bond", name="bond", created_at=t1)
    second = station.register_agent("bond", name="bond", created_at=t2)
    assert station._resolve_agent("bond") == second


def test_handle_conflict_is_an_agent_name_taken(station):
    # AgentHandleConflict subclasses AgentNameTaken so existing handlers catch it.
    station.register_agent("bond", name="agent-007")
    with pytest.raises(AgentNameTaken):
        station.register_agent("alice", name="bond")


# --- station: same-agent rename is rejected (finding C, round 3) -------------


def test_register_rejects_rename_of_live_agent_id(station):
    # The strand: after an auto-ignite (name == agent-id), re-registering that
    # agent-id under a DIFFERENT name would strand the live incarnation — the old
    # slug keeps pointing at the stale folio while the new name binds a fresh one,
    # and _resolve_agent (slug-first) resolves the agent-id to the DEAD folio.
    # Reject the rename: an agent-id resolves to exactly one live incarnation.
    station.register_agent("worker-1")  # name defaults to agent-id
    with pytest.raises(AgentRenameRejected):
        station.register_agent("worker-1", name="worker-renamed")
    # The rejection rolls back — no second folio, the agent-id still resolves to
    # its one incarnation under the original name, and the roster shows one row.
    assert station.store.resolve_slug("worker-renamed") is None
    h = station._resolve_agent("worker-1")
    assert station.store.resolve_slug("worker-1") == h
    rows = [a for a in station.list_agents() if a["created_by"] == "worker-1"]
    assert len(rows) == 1 and rows[0]["name"] == "worker-1"


def test_register_rejects_rename_from_decoupled_name(station):
    # The mirror of the above for a decoupled register: agent-id 'agent-007' is
    # live under name 'bond'; re-registering it under 'jbond' is still a rename.
    station.register_agent("agent-007", name="bond")
    with pytest.raises(AgentRenameRejected):
        station.register_agent("agent-007", name="jbond")
    assert station.store.resolve_slug("jbond") is None


def test_rename_rejected_is_an_agent_name_taken(station):
    # AgentRenameRejected subclasses AgentNameTaken so ignite/register handlers
    # (which catch AgentNameTaken) surface it without a new except clause.
    station.register_agent("worker-1")
    with pytest.raises(AgentNameTaken):
        station.register_agent("worker-1", name="worker-renamed")


def test_register_allows_reignite_same_name_despite_rename_guard(station):
    # Allowed case 1: a same-agent re-ignite (same id, same name) is NOT a rename.
    # The slug equals the requested name, so the rename guard skips it and the
    # succession path rebinds the slug to the new incarnation + threads succession.
    t1 = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 22, 15, 0, tzinfo=timezone.utc)
    first = station.register_agent("worker-1", created_at=t1)
    second = station.register_agent("worker-1", created_at=t2)
    assert station._resolve_agent("worker-1") == second
    succ = [
        e for e in station.thread_graph(second)["outgoing"] if e["type"] == "succession"
    ]
    assert len(succ) == 1 and succ[0]["to_id"] == first  # threaded to prior


def test_register_allows_fresh_decoupled_despite_rename_guard(station):
    # Allowed case 2: a fresh decoupled register (agent-id with no prior folio,
    # name != agent-id) holds no prior slug, so the rename guard never fires.
    h = station.register_agent("agent-007", name="bond")
    assert station._resolve_agent("bond") == h
    assert station._resolve_agent("agent-007") == h


def test_register_allows_initial_auto_ignite_despite_rename_guard(station):
    # Allowed case 3: the initial auto-ignite (name == agent-id, no prior) is the
    # very state the rename guard must not block.
    h = station.register_agent("solo")
    assert station._resolve_agent("solo") == h


# --- station: concurrent first-register converges on one roster site (finding 3)


def test_concurrent_first_register_one_roster_site(tmp_path):
    # Two cold-start agents racing to mint the reserved roster site must converge
    # on ONE site folio — no orphan that leaves the loser's agent unattached.
    data_dir = tmp_path / ".skein"
    SkeinNextStore(data_dir).close()  # create the db file first
    barrier = threading.Barrier(2)
    errors = []

    def reg(agent_id):
        st = Station(data_dir=data_dir)
        try:
            barrier.wait()
            st.register_agent(agent_id, name=agent_id)
        except Exception as e:  # pragma: no cover - surfaced via errors list
            errors.append(e)
        finally:
            st.close()

    threads = [threading.Thread(target=reg, args=(a,)) for a in ("alice", "bob")]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert errors == []
    st = Station(data_dir=data_dir)
    try:
        roster_sites = [
            f for f in st.store.folios_by_type("site") if f["title"] == ROSTER_SITE
        ]
        assert len(roster_sites) == 1  # no split — exactly one roster site folio
        members = st.folios_in_site(ROSTER_SITE, type="agent")
        assert {m["title"] for m in members} == {"alice", "bob"}  # both attached
    finally:
        st.close()


# --- station: FSM resolution under the lock (finding 4) ----------------------


def test_transition_resolves_agent_under_the_lock(station, monkeypatch):
    # The resolve must happen INSIDE the transaction so a concurrent re-ignite
    # cannot rebind the slug between resolve and the status write. Assert the
    # ordering directly: _resolve_agent is called only after the batch lock is held.
    station.register_agent("nub-0622")
    order = []
    real_resolve = station._resolve_agent

    def spy_resolve(ref):
        order.append(("resolve", station.store._in_batch))
        return real_resolve(ref)

    monkeypatch.setattr(station, "_resolve_agent", spy_resolve)
    station.agent_ready("nub-0622")
    assert order == [("resolve", True)]  # in_batch True ⇒ inside the BEGIN IMMEDIATE


def test_reignite_during_transition_targets_live_folio(station):
    # End-to-end: re-ignite (slug → new folio) then transition by name. The status
    # must land on the live incarnation the slug now names, not the superseded one.
    t1 = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 22, 15, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 6, 22, 16, 0, tzinfo=timezone.utc)
    first = station.register_agent("nub-0622", created_at=t1)
    second = station.register_agent("nub-0622", created_at=t2)
    # created_at after t2 so the 'active' thread supersedes second's initial
    # 'orienting' thread (latest-by-created_at wins).
    moved = station.transition_agent("nub-0622", "active", created_at=t3)
    assert moved == second != first
    assert station.status_of(second) == "active"
    assert station.status_of(first) == "orienting"  # untouched
