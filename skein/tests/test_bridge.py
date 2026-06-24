"""Tests for the import bridge: legacy SKEIN store -> content-hash skein.

Covers (per brief-20260529-i6fy Slice 2):
- folio re-hash from canonical fields + alias mapping (never trust legacy hash)
- the three endpoint classifications (folio / actor->weaver / unresolved-cross-project)
- sites JSON -> site folios + within threads + slug table
- idempotency (import twice -> identical store)
- the loss report (carried / reclassified / unresolved / merged)
"""

import json
import sqlite3

import pytest

from skein.bridge import classify_endpoint, import_project, open_legacy
from skein.store import SkeinNextStore

# Legacy column sets, mirroring skein/storage.py (the bridge reads these by name).
_LEGACY_SCHEMA = """
CREATE TABLE folios (
    folio_id     TEXT PRIMARY KEY,
    type         TEXT,
    site_id      TEXT,
    created_at   TEXT,
    created_by   TEXT,
    title        TEXT,
    content      TEXT,
    status       TEXT,
    content_hash TEXT
);
CREATE TABLE threads (
    thread_id  TEXT PRIMARY KEY,
    from_id    TEXT,
    to_id      TEXT,
    type       TEXT,
    content    TEXT,
    weaver     TEXT,
    created_at TEXT
);
"""


def make_legacy_db(path, folios, threads):
    conn = sqlite3.connect(str(path))
    conn.executescript(_LEGACY_SCHEMA)
    for f in folios:
        cols = ("folio_id", "type", "site_id", "created_at", "created_by",
                "title", "content", "status", "content_hash")
        conn.execute(
            f"INSERT INTO folios ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(f.get(c) for c in cols),
        )
    for t in threads:
        cols = ("thread_id", "from_id", "to_id", "type", "content", "weaver",
                "created_at")
        conn.execute(
            f"INSERT INTO threads ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(t.get(c) for c in cols),
        )
    conn.commit()
    conn.close()


def make_sites_dir(base, sites):
    sd = base / "sites"
    sd.mkdir(parents=True, exist_ok=True)
    for s in sites:
        site_dir = sd / s["site_id"]
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "metadata.json").write_text(json.dumps(s))
    return sd


@pytest.fixture
def store(tmp_path):
    s = SkeinNextStore(data_dir=tmp_path / ".skein")
    yield s
    s.close()


# A small but representative corpus.
FOLIOS = [
    dict(folio_id="brief-20260101-aaaa", type="brief", site_id="alpha",
         created_at="2026-01-01T10:00:00.111111+00:00", created_by="goblin-0101",
         title="First brief", content="brief body", status="open",
         content_hash="legacy-unreliable-1"),
    dict(folio_id="finding-20260102-bbbb", type="finding", site_id="alpha",
         created_at="2026-01-02T10:00:00.222222Z", created_by="mote-0102",
         title="A finding", content="finding body", status="closed",
         content_hash=None),
    dict(folio_id="brief-20260103-cccc", type="brief", site_id="beta",
         created_at="2026-01-03T10:00:00.333333+00:00", created_by="unknown",
         title="Beta brief", content="beta body", status="open",
         content_hash="legacy-unreliable-3"),
]

THREADS = [
    # folio -> folio reference; weaver is an actor (stays weaver)
    dict(thread_id="t1", from_id="brief-20260101-aaaa", to_id="finding-20260102-bbbb",
         type="reference", content="see also", weaver="cc-0101",
         created_at="2026-01-04T10:00:00.1+00:00"),
    # actor -> folio message: actor routes to weaver, edge keeps only the folio
    dict(thread_id="t2", from_id="qm-20260105-201613", to_id="brief-20260101-aaaa",
         type="message", content="status update", weaver="qm-20260105-201613",
         created_at="2026-01-05T10:00:00.2+00:00"),
    # actor self-loop tag (shard hex), weaver empty -> actor becomes weaver
    dict(thread_id="t3", from_id="100599da", to_id="100599da",
         type="tag", content='{"tag":"shard"}', weaver=None,
         created_at="2026-01-06T10:00:00.3+00:00"),
    # dangling folio-shaped ref (target not in db) -> kept as legacy id
    dict(thread_id="t4", from_id="brief-20260101-aaaa", to_id="brief-29991231-zzzz",
         type="mention", content="mention", weaver="goblin-0101",
         created_at="2026-01-07T10:00:00.4+00:00"),
    # cross-project colon ref -> kept as legacy id, marked cross-project
    dict(thread_id="t5", from_id="otherproj:finding-20260101-qqqq",
         to_id="finding-20260102-bbbb", type="reference", content="x-proj",
         weaver="mote-0102", created_at="2026-01-08T10:00:00.5+00:00"),
    # succession linking two folios -> renamed supersedes
    dict(thread_id="t6", from_id="brief-20260103-cccc", to_id="brief-20260101-aaaa",
         type="succession", content="supersedes", weaver="quirk-0103",
         created_at="2026-01-09T10:00:00.6+00:00"),
]

SITES = [
    dict(site_id="alpha", created_at="2025-12-01T08:00:00.000001",
         created_by="unknown", purpose="Alpha site purpose", status="active"),
    dict(site_id="beta", created_at="2025-12-02T08:00:00.000002",
         created_by="unknown", purpose="Beta site purpose", status="active"),
]


@pytest.fixture
def legacy(tmp_path):
    db = tmp_path / "legacy.db"
    make_legacy_db(db, FOLIOS, THREADS)
    sites_dir = make_sites_dir(tmp_path / "legacy_data", SITES)
    return db, sites_dir


# --- classify_endpoint ------------------------------------------------------

FOLIO_IDS = {"brief-20260101-aaaa", "finding-20260102-bbbb", "brief-20260103-cccc"}
FOLIO_TYPES = {"brief", "finding", "notion", "summary", "issue", "friction"}


def test_classify_folio_in_db():
    kind, val = classify_endpoint("brief-20260101-aaaa", FOLIO_IDS, FOLIO_TYPES)
    assert kind == "folio"
    assert val == "brief-20260101-aaaa"


def test_classify_actor_agent_name():
    assert classify_endpoint("goblin-0101", FOLIO_IDS, FOLIO_TYPES)[0] == "actor"


def test_classify_actor_shard_hex():
    assert classify_endpoint("100599da", FOLIO_IDS, FOLIO_TYPES)[0] == "actor"


def test_classify_actor_session_string_that_looks_date_stamped():
    # qm-20260105-201613 matches a date shape but 'qm' is not a folio type
    assert classify_endpoint("qm-20260105-201613", FOLIO_IDS, FOLIO_TYPES)[0] == "actor"


def test_classify_unresolved_dangling_folio_ref():
    kind, val = classify_endpoint("brief-29991231-zzzz", FOLIO_IDS, FOLIO_TYPES)
    assert kind == "unresolved"
    assert val == "brief-29991231-zzzz"


def test_classify_unresolved_cross_project_ref():
    kind, val = classify_endpoint("otherproj:finding-20260101-qqqq",
                                  FOLIO_IDS, FOLIO_TYPES)
    assert kind == "unresolved"
    assert val == "otherproj:finding-20260101-qqqq"


def test_classify_none():
    assert classify_endpoint(None, FOLIO_IDS, FOLIO_TYPES)[0] == "none"


# --- folio re-hash + alias --------------------------------------------------


def test_folios_rehashed_and_aliased(legacy, store):
    db, sites_dir = legacy
    import_project(db, sites_dir, store)
    # every legacy folio id resolves to a content hash, never the legacy column
    for fid in FOLIO_IDS:
        h = store.resolve_alias(fid)
        assert h is not None and h.startswith("sha256::")
    # the hash is recomputed from fields: matches the store's own hashing
    folio = FOLIOS[0]
    h = store.resolve_alias(folio["folio_id"])
    got = store.get_folio(h)
    assert got["title"] == folio["title"]
    assert got["created_at"] == "2026-01-01T10:00:00.111111+00:00"


def test_legacy_content_hash_column_is_ignored(legacy, store):
    db, sites_dir = legacy
    import_project(db, sites_dir, store)
    # legacy stored 'legacy-unreliable-1'; the new hash is a real sha256 address
    h = store.resolve_alias("brief-20260101-aaaa")
    assert h != "legacy-unreliable-1"


# --- endpoint classification on real threads --------------------------------


def test_actor_endpoint_routed_to_weaver(legacy, store):
    db, sites_dir = legacy
    import_project(db, sites_dir, store)
    folio_hash = store.resolve_alias("brief-20260101-aaaa")
    # the message thread: actor 'qm-...' dropped off the edge, only the folio remains
    msgs = [t for t in store.get_threads(type="message")]
    assert len(msgs) == 1
    m = msgs[0]
    assert m["from_id"] is None
    assert m["to_id"] == folio_hash
    assert m["weaver"] == "qm-20260105-201613"


def test_actor_self_loop_tag_preserved_as_weaver_annotation(legacy, store):
    db, sites_dir = legacy
    import_project(db, sites_dir, store)
    tags = store.get_threads(type="tag")
    assert len(tags) == 1
    assert tags[0]["from_id"] is None
    assert tags[0]["to_id"] is None
    assert tags[0]["weaver"] == "100599da"  # actor captured, not dropped


def test_dangling_ref_kept_as_legacy_id(legacy, store):
    db, sites_dir = legacy
    import_project(db, sites_dir, store)
    folio_hash = store.resolve_alias("brief-20260101-aaaa")
    mentions = store.get_threads(from_id=folio_hash, type="mention")
    assert len(mentions) == 1
    assert mentions[0]["to_id"] == "brief-29991231-zzzz"
    assert "brief-29991231-zzzz" in store.unresolved_endpoints()


def test_cross_project_ref_kept_as_legacy_id(legacy, store):
    db, sites_dir = legacy
    import_project(db, sites_dir, store)
    assert "otherproj:finding-20260101-qqqq" in store.unresolved_endpoints()


def test_succession_renamed_to_supersedes(legacy, store):
    db, sites_dir = legacy
    import_project(db, sites_dir, store)
    assert store.get_threads(type="succession") == []
    sup = store.get_threads(type="supersedes")
    assert len(sup) == 1
    a = store.resolve_alias("brief-20260103-cccc")
    b = store.resolve_alias("brief-20260101-aaaa")
    assert sup[0]["from_id"] == a and sup[0]["to_id"] == b


def test_folio_edge_resolves_both_endpoints_to_hashes(legacy, store):
    db, sites_dir = legacy
    import_project(db, sites_dir, store)
    refs = store.get_threads(type="reference")
    # t1 (both folios) + t5 (cross-project from, folio to)
    t1 = [r for r in refs if r["content"] == "see also"][0]
    assert t1["from_id"] == store.resolve_alias("brief-20260101-aaaa")
    assert t1["to_id"] == store.resolve_alias("finding-20260102-bbbb")


# --- sites ------------------------------------------------------------------


def test_sites_become_folios_with_slug(legacy, store):
    db, sites_dir = legacy
    import_project(db, sites_dir, store)
    alpha = store.resolve_slug("alpha")
    assert alpha is not None
    folio = store.get_folio(alpha)
    assert folio["type"] == "site"
    assert folio["content"] == "Alpha site purpose"
    assert folio["title"] == "alpha"


def test_within_threads_link_members_to_site(legacy, store):
    db, sites_dir = legacy
    import_project(db, sites_dir, store)
    alpha = store.resolve_slug("alpha")
    within = store.get_threads(to_id=alpha, type="within")
    members = {w["from_id"] for w in within}
    # brief-...aaaa and finding-...bbbb are in alpha; brief-...cccc is in beta
    assert store.resolve_alias("brief-20260101-aaaa") in members
    assert store.resolve_alias("finding-20260102-bbbb") in members
    assert store.resolve_alias("brief-20260103-cccc") not in members


# --- idempotency ------------------------------------------------------------


def test_import_twice_is_identical(legacy, store):
    db, sites_dir = legacy
    import_project(db, sites_dir, store)
    snap_folios = store.count_folios()
    snap_threads = store.count_threads()
    snap_slugs = store.list_slugs()
    # second run against the same frozen source
    import_project(db, sites_dir, store)
    assert store.count_folios() == snap_folios
    assert store.count_threads() == snap_threads
    assert store.list_slugs() == snap_slugs


# --- report -----------------------------------------------------------------


def test_report_accounts_for_everything(legacy, store):
    db, sites_dir = legacy
    report = import_project(db, sites_dir, store)
    assert report.folios_seen == 3
    assert report.folios_carried == 3
    assert report.sites_carried == 2
    assert report.threads_seen == 6
    # actor endpoints: t2 from (1) + t3 from/to (2) = 3 endpoint occurrences
    assert report.actor_endpoints_to_weaver == 3
    assert report.succession_renamed == 1
    # two unresolved refs: one dangling, one cross-project
    assert report.unresolved_refs == 2
    assert report.cross_project_refs == 1
    assert report.dangling_refs == 1
    # within threads: 3 members each linked to their site
    assert report.within_threads == 3
    # nothing should be silently dropped
    assert report.actor_endpoints_dropped == 0


def test_report_counts_merged_duplicate_edges(tmp_path, store):
    """Two genuinely-distinct legacy edges that collapse to one new hash are counted."""
    db = tmp_path / "legacy.db"
    # two tag self-loops on the same actor with identical content + timestamp:
    # distinct legacy rows, but identical after reclassification -> one new hash
    dup_threads = [
        dict(thread_id="d1", from_id="abcd1234", to_id="abcd1234", type="tag",
             content="same", weaver=None, created_at="2026-01-01T00:00:00+00:00"),
        dict(thread_id="d2", from_id="abcd1234", to_id="abcd1234", type="tag",
             content="same", weaver=None, created_at="2026-01-01T00:00:00+00:00"),
    ]
    make_legacy_db(db, [], dup_threads)
    sites_dir = make_sites_dir(tmp_path / "d", [])
    report = import_project(db, sites_dir, store)
    assert report.threads_seen == 2
    assert report.threads_carried == 1
    assert report.threads_merged == 1


# --- actor<->actor edge preservation (FINDING 1) ----------------------------


def test_actor_to_actor_succession_keeps_both_endpoints(tmp_path, store):
    """A session-handoff succession A->B must retain from=A, to=B — not be nulled.

    Both endpoints are distinct actors (sessions), so neither folds into the
    weaver; the relationship survives instead of becoming a from=None,to=None husk.
    Type stays ``succession`` (it links sessions, not folios).
    """
    db = tmp_path / "legacy.db"
    threads = [
        dict(thread_id="s1", from_id="qm-20260105-201613",
             to_id="goblin-20260106-090000", type="succession",
             content="handoff", weaver=None,
             created_at="2026-01-10T00:00:00+00:00"),
    ]
    make_legacy_db(db, [], threads)
    sites_dir = make_sites_dir(tmp_path / "d", [])
    report = import_project(db, sites_dir, store)

    succ = store.get_threads(type="succession")
    assert len(succ) == 1
    assert succ[0]["from_id"] == "qm-20260105-201613"
    assert succ[0]["to_id"] == "goblin-20260106-090000"
    # not renamed to supersedes (endpoints are actors, not folios)
    assert store.get_threads(type="supersedes") == []
    # both actor identities preserved on the edge; nothing folded, nothing lost
    assert report.actor_to_actor_edges == 1
    assert report.actor_endpoints_kept == 2
    assert report.actor_endpoints_to_weaver == 0
    assert report.actor_endpoints_dropped == 0


def test_actor_to_actor_with_distinct_weaver_preserves_all_three(tmp_path, store):
    """from=A, to=B, weaver=C (three distinct actors) — all three survive."""
    db = tmp_path / "legacy.db"
    threads = [
        dict(thread_id="m1", from_id="agent-a", to_id="agent-b", type="message",
             content="hi", weaver="agent-c",
             created_at="2026-01-11T00:00:00+00:00"),
    ]
    make_legacy_db(db, [], threads)
    sites_dir = make_sites_dir(tmp_path / "d", [])
    report = import_project(db, sites_dir, store)

    msgs = store.get_threads(type="message")
    assert len(msgs) == 1
    assert msgs[0]["from_id"] == "agent-a"
    assert msgs[0]["to_id"] == "agent-b"
    assert msgs[0]["weaver"] == "agent-c"
    assert report.actor_endpoints_dropped == 0


def test_no_husks_and_no_dropped_identities(legacy, store):
    """No thread ends up from=None,to=None,weaver=None, and zero identities drop."""
    db, sites_dir = legacy
    report = import_project(db, sites_dir, store)
    for t in store.get_threads():
        assert not (t["from_id"] is None
                    and t["to_id"] is None
                    and t["weaver"] is None), f"husk thread: {t}"
    assert report.actor_endpoints_dropped == 0
    assert report.dropped_examples == []


def test_classify_actor_with_folio_type_prefix_guarded_by_known_actors():
    """FINDING 3: a session id whose prefix is a real folio type is an actor.

    Without the guard, ``brief-20260105-201613`` matches the folio-id shape and
    'brief' is a folio type, so it would misclassify as an unresolved folio ref.
    Passing it in known_actors (it appears as a weaver) fixes the classification.
    """
    sessionish = "brief-20260105-201613"
    # unguarded: misreads as unresolved
    kind, _ = classify_endpoint(sessionish, FOLIO_IDS, FOLIO_TYPES)
    assert kind == "unresolved"
    # guarded by the known-actor set: correctly an actor
    kind, _ = classify_endpoint(sessionish, FOLIO_IDS, FOLIO_TYPES,
                                known_actors={sessionish})
    assert kind == "actor"


def test_folio_type_prefixed_actor_not_minted_as_bogus_edge(tmp_path, store):
    """End to end: a folio-type-prefixed session that wove a thread stays an actor."""
    db = tmp_path / "legacy.db"
    folios = [
        dict(folio_id="brief-20260101-aaaa", type="brief", site_id=None,
             created_at="2026-01-01T10:00:00.111111+00:00", created_by="x",
             title="t", content="c", status="open", content_hash=None),
    ]
    threads = [
        # 'brief-20260201-120000' is a session (also the weaver), not a folio ref
        dict(thread_id="x1", from_id="brief-20260201-120000",
             to_id="brief-20260101-aaaa", type="message", content="m",
             weaver="brief-20260201-120000",
             created_at="2026-02-01T12:00:00+00:00"),
    ]
    make_legacy_db(db, folios, threads)
    sites_dir = make_sites_dir(tmp_path / "d", [])
    report = import_project(db, sites_dir, store)
    # the session folded to weaver; it was NOT counted as an unresolved folio ref
    assert report.unresolved_refs == 0
    m = store.get_threads(type="message")[0]
    assert m["weaver"] == "brief-20260201-120000"
    assert m["from_id"] is None
    assert m["to_id"] == store.resolve_alias("brief-20260101-aaaa")


# --- dropped-column accounting (FINDING 2) ----------------------------------

_LEGACY_SCHEMA_WITH_EXTRAS = """
CREATE TABLE folios (
    folio_id     TEXT PRIMARY KEY,
    type         TEXT,
    site_id      TEXT,
    created_at   TEXT,
    created_by   TEXT,
    title        TEXT,
    content      TEXT,
    status       TEXT,
    content_hash TEXT,
    metadata     TEXT,
    target_agent TEXT
);
CREATE TABLE threads (
    thread_id  TEXT PRIMARY KEY,
    from_id    TEXT,
    to_id      TEXT,
    type       TEXT,
    content    TEXT,
    weaver     TEXT,
    created_at TEXT
);
"""


def test_dropped_legacy_columns_are_counted(tmp_path, store):
    """metadata is now CARRIED (folded into content); target_agent stays a counted
    drop, and that loss must remain visible in the report."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_LEGACY_SCHEMA_WITH_EXTRAS)
    rows = [
        # populated metadata + target_agent
        ("brief-20260101-aaaa", "brief", None, "2026-01-01T10:00:00.1+00:00",
         "x", "t1", "c1", "open", None, '{"k":"v"}', "agent-z"),
        # populated metadata, no target_agent
        ("brief-20260102-bbbb", "brief", None, "2026-01-02T10:00:00.2+00:00",
         "x", "t2", "c2", "open", None, '{"a":1}', None),
        # trivial-empty metadata ({}) and empty target_agent -> neither counted
        ("brief-20260103-cccc", "brief", None, "2026-01-03T10:00:00.3+00:00",
         "x", "t3", "c3", "open", None, "{}", ""),
    ]
    for r in rows:
        conn.execute(
            "INSERT INTO folios (folio_id,type,site_id,created_at,created_by,"
            "title,content,status,content_hash,metadata,target_agent) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", r,
        )
    conn.commit()
    conn.close()
    sites_dir = make_sites_dir(tmp_path / "d", [])
    report = import_project(db, sites_dir, store)

    # metadata: carried into content for the two real ones, not a dropped column.
    assert "metadata" not in report.dropped_folio_columns
    assert report.metadata_carried == 2
    # target_agent: still a counted, surfaced drop (the one populated row).
    assert report.dropped_folio_columns.get("target_agent") == 1
    rendered = report.render()
    assert "metadata populated in 2 folios" not in rendered
    assert "target_agent populated in 1 folios" in rendered


_LEGACY_SCHEMA_WITH_FLAG = """
CREATE TABLE folios (
    folio_id     TEXT PRIMARY KEY,
    type         TEXT,
    site_id      TEXT,
    created_at   TEXT,
    created_by   TEXT,
    title        TEXT,
    content      TEXT,
    status       TEXT,
    content_hash TEXT,
    archived     INTEGER
);
CREATE TABLE threads (
    thread_id  TEXT PRIMARY KEY, from_id TEXT, to_id TEXT, type TEXT,
    content TEXT, weaver TEXT, created_at TEXT
);
"""


def test_all_zero_flag_column_not_reported_as_loss(tmp_path, store):
    """A default-0 boolean flag (every row 0) carries no info and is not flagged;
    a flag with real 1 values surfaces the count of set rows."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_LEGACY_SCHEMA_WITH_FLAG)
    rows = [
        ("brief-20260101-aaaa", "brief", None, "2026-01-01T10:00:00.1+00:00",
         "x", "t", "c", "open", None, 0),
        ("brief-20260102-bbbb", "brief", None, "2026-01-02T10:00:00.2+00:00",
         "x", "t", "c", "open", None, 0),
    ]
    for r in rows:
        conn.execute(
            "INSERT INTO folios (folio_id,type,site_id,created_at,created_by,"
            "title,content,status,content_hash,archived) VALUES (?,?,?,?,?,?,?,?,?,?)", r,
        )
    conn.commit()
    conn.close()
    sites_dir = make_sites_dir(tmp_path / "d", [])
    report = import_project(db, sites_dir, store)
    assert "archived" not in report.dropped_folio_columns

    # now one row archived=1 -> surfaced with count 1
    db2 = tmp_path / "legacy2.db"
    conn = sqlite3.connect(str(db2))
    conn.executescript(_LEGACY_SCHEMA_WITH_FLAG)
    conn.execute(
        "INSERT INTO folios (folio_id,type,site_id,created_at,created_by,"
        "title,content,status,content_hash,archived) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("brief-20260103-cccc", "brief", None, "2026-01-03T10:00:00.3+00:00",
         "x", "t", "c", "open", None, 1),
    )
    conn.commit()
    conn.close()
    store2 = SkeinNextStore(data_dir=tmp_path / ".skein2")
    report2 = import_project(db2, sites_dir, store2)
    assert report2.dropped_folio_columns.get("archived") == 1
    store2.close()


def test_succession_self_loop_folds_to_weaver_no_husk(tmp_path, store):
    """A self-succession (from==to, same session) has no distinct second party.

    It folds to weaver (identity preserved) rather than nulling into a husk; it is
    not an actor<->actor edge and nothing is dropped.
    """
    db = tmp_path / "legacy.db"
    threads = [
        dict(thread_id="ss", from_id="cc-session-20251107",
             to_id="cc-session-20251107", type="succession", content="self",
             weaver=None, created_at="2026-01-12T00:00:00+00:00"),
    ]
    make_legacy_db(db, [], threads)
    sites_dir = make_sites_dir(tmp_path / "d", [])
    report = import_project(db, sites_dir, store)
    succ = store.get_threads(type="succession")
    assert len(succ) == 1
    assert succ[0]["from_id"] is None and succ[0]["to_id"] is None
    assert succ[0]["weaver"] == "cc-session-20251107"  # identity preserved
    assert report.actor_to_actor_edges == 0
    assert report.actor_endpoints_dropped == 0


def test_no_dropped_columns_when_schema_is_canonical(legacy, store):
    """The plain legacy schema (no extra columns) reports zero dropped columns."""
    db, sites_dir = legacy
    report = import_project(db, sites_dir, store)
    assert report.dropped_folio_columns == {}
    assert report.dropped_thread_columns == {}


# --- unresolved occurrences vs distinct -------------------------------------


def test_unresolved_refs_tracks_distinct(tmp_path, store):
    """The same dangling ref used twice is 2 occurrences but 1 distinct id."""
    db = tmp_path / "legacy.db"
    folios = [
        dict(folio_id="brief-20260101-aaaa", type="brief", site_id=None,
             created_at="2026-01-01T10:00:00.1+00:00", created_by="x",
             title="t", content="c", status="open", content_hash=None),
        dict(folio_id="finding-20260102-bbbb", type="finding", site_id=None,
             created_at="2026-01-02T10:00:00.2+00:00", created_by="x",
             title="t", content="c", status="open", content_hash=None),
    ]
    threads = [
        dict(thread_id="u1", from_id="brief-20260101-aaaa",
             to_id="brief-29991231-zzzz", type="mention", content="a",
             weaver="x", created_at="2026-01-05T00:00:00+00:00"),
        dict(thread_id="u2", from_id="finding-20260102-bbbb",
             to_id="brief-29991231-zzzz", type="mention", content="b",
             weaver="x", created_at="2026-01-06T00:00:00+00:00"),
    ]
    make_legacy_db(db, folios, threads)
    sites_dir = make_sites_dir(tmp_path / "d", [])
    report = import_project(db, sites_dir, store)
    assert report.unresolved_refs == 2
    assert len(report.unresolved_distinct) == 1
    assert "2 occurrences (1 distinct" in report.render()


# --- read-only safety -------------------------------------------------------


def test_open_legacy_is_read_only(legacy):
    db, _ = legacy
    conn = open_legacy(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO folios (folio_id) VALUES ('x')")
    conn.close()


# --- legacy metadata preservation (folded into content) ----------------------
#
# The bridge folds a legacy folio's `metadata` column into the new folio's
# content via the legacy-meta envelope (minus the dead questions_enabled flag),
# so it is carried, not dropped. A schema WITH metadata + target_agent exercises
# this; the older fixtures omit those columns and prove the schema-drift guard.

from skein.legacy_meta import parse_legacy_meta  # noqa: E402

_META_FOLIOS = [
    ("tender-1", "tender", None, "2026-01-01T00:00:00Z", "a", "T1", "tender body",
     "open", None, '{"confidence": 8, "reviewer": "fell-r1", "questions_enabled": true}', None),
    ("brief-1", "brief", None, "2026-01-02T00:00:00Z", "b", "B1", "brief body",
     "open", None, '{"questions_enabled": true}', "next-session"),
    ("plain-1", "finding", None, "2026-01-03T00:00:00Z", "c", "F1", "finding body",
     "open", None, None, None),
    ("weird-1", "notion", None, "2026-01-04T00:00:00Z", "d", "N1", "notion body",
     "open", None, "not json at all", None),
    # null content body WITH real metadata: the envelope must still apply cleanly.
    ("nullbody-1", "notion", None, "2026-01-05T00:00:00Z", "e", "NB1", None,
     "open", None, '{"k": "v"}', None),
]


def _build_meta_db(tmp_path):
    db = tmp_path / "meta.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_LEGACY_SCHEMA_WITH_EXTRAS)
    for r in _META_FOLIOS:
        conn.execute(
            "INSERT INTO folios (folio_id,type,site_id,created_at,created_by,"
            "title,content,status,content_hash,metadata,target_agent) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", r,
        )
    conn.commit()
    conn.close()
    sites = make_sites_dir(tmp_path / "data", [])
    return db, sites


def test_metadata_folded_into_content(tmp_path, store):
    db, sites = _build_meta_db(tmp_path)
    report = import_project(db, sites, store)
    folio = store.get_folio(store.resolve_alias("tender-1"))
    assert "tender body" in folio["content"]
    meta = parse_legacy_meta(folio["content"])
    assert meta == {"confidence": 8, "reviewer": "fell-r1"}  # questions_enabled stripped
    # tender-1 (real) + weird-1 (_raw) + nullbody-1 (real, null body)
    assert report.metadata_carried == 3


def test_dead_flag_only_metadata_not_folded(tmp_path, store):
    db, sites = _build_meta_db(tmp_path)
    report = import_project(db, sites, store)
    folio = store.get_folio(store.resolve_alias("brief-1"))
    assert parse_legacy_meta(folio["content"]) is None  # nothing folded
    assert "legacy-meta" not in folio["content"]
    assert report.metadata_flag_dropped == 1  # brief-1 had only the dead flag


def test_non_json_metadata_preserved_raw(tmp_path, store):
    db, sites = _build_meta_db(tmp_path)
    import_project(db, sites, store)
    folio = store.get_folio(store.resolve_alias("weird-1"))
    assert parse_legacy_meta(folio["content"]) == {"_raw": "not json at all"}


def test_metadata_folded_when_content_is_null(tmp_path, store):
    db, sites = _build_meta_db(tmp_path)
    import_project(db, sites, store)
    folio = store.get_folio(store.resolve_alias("nullbody-1"))
    assert parse_legacy_meta(folio["content"]) == {"k": "v"}


def test_metadata_fold_preserves_body_trailing_newlines(tmp_path, store):
    # A legacy body that ends in newlines must survive byte-for-byte when metadata
    # is folded in — no rstrip on the migration path.
    db = tmp_path / "nl.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_LEGACY_SCHEMA_WITH_EXTRAS)
    conn.execute(
        "INSERT INTO folios (folio_id,type,site_id,created_at,created_by,title,"
        "content,status,content_hash,metadata,target_agent) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("nl-1", "finding", None, "2026-01-08T00:00:00Z", "a", "NL", "line\n\n",
         "open", None, '{"k": "v"}', None),
    )
    conn.commit()
    conn.close()
    sites = make_sites_dir(tmp_path / "d", [])
    import_project(db, sites, store)
    content = store.get_folio(store.resolve_alias("nl-1"))["content"]
    assert content.startswith("line\n\n")  # body preserved, not trimmed
    assert parse_legacy_meta(content) == {"k": "v"}


def test_metadata_not_reported_as_dropped_but_target_agent_is(tmp_path, store):
    db, sites = _build_meta_db(tmp_path)
    report = import_project(db, sites, store)
    assert "metadata" not in report.dropped_folio_columns
    # target_agent stays a counted drop (Patrick's call: drop the handoff targets).
    assert report.dropped_folio_columns.get("target_agent") == 1


def test_metadata_blob_cell_bytes_path(tmp_path, store):
    # A BLOB metadata cell makes sqlite return `bytes` (not str), exercising the
    # bytes path through the real import: valid-UTF-8 JSON bytes fold to a dict;
    # invalid-UTF-8 bytes are base64-preserved and the insert must not crash.
    import base64 as _b64
    db = tmp_path / "blob.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_LEGACY_SCHEMA_WITH_EXTRAS)
    bad = b'\xff\xfe\x00bad'
    conn.execute(
        "INSERT INTO folios (folio_id,type,site_id,created_at,created_by,title,"
        "content,status,content_hash,metadata,target_agent) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("blob-json", "notion", None, "2026-01-06T00:00:00Z", "a", "BJ", "body",
         "open", None, b'{"k": "v"}', None),
    )
    conn.execute(
        "INSERT INTO folios (folio_id,type,site_id,created_at,created_by,title,"
        "content,status,content_hash,metadata,target_agent) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("blob-bad", "notion", None, "2026-01-07T00:00:00Z", "b", "BB", "body",
         "open", None, bad, None),
    )
    conn.commit()
    conn.close()
    sites = make_sites_dir(tmp_path / "d", [])
    import_project(db, sites, store)  # must not raise

    assert parse_legacy_meta(store.get_folio(store.resolve_alias("blob-json"))["content"]) == {"k": "v"}
    bad_meta = parse_legacy_meta(store.get_folio(store.resolve_alias("blob-bad"))["content"])
    assert _b64.b64decode(bad_meta["_raw_base64"]) == bad  # exact bytes recovered


def test_metadata_fold_is_idempotent(tmp_path, store):
    db, sites = _build_meta_db(tmp_path)
    import_project(db, sites, store)
    h1 = store.resolve_alias("tender-1")
    n1 = store.count_folios()
    # Re-import the SAME db: the enveloped content hashes identically, so the
    # folio collapses rather than duplicating.
    import_project(db, sites, store)
    assert store.resolve_alias("tender-1") == h1
    assert store.count_folios() == n1
