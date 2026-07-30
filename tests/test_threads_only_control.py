"""Threads-only control state: the signed thread graph is the SOLE persistence.

Regression corpus for the threads-only contraction (2026-07-08, follows
brief-20260708-lk46 / finding-20260708-oj4m): the ``refs.status`` /
``refs.assigned_to`` / ``refs.archived`` cache columns are GONE. Control state
lives only in genesis-keyed control threads, reduced by the A4 readers
(``get_latest_statuses``/``get_latest_assignments``) and overlaid on every API
read surface via ``enrich_folios_with_status``. The staleness class this kills:
the cache was half-maintained (the PATCH sugar path refreshed it, the primary
``skein close`` → generic POST /threads path did not), so every close between the
A3 cutover and the contraction left a stale row that the unenriched surfaces
(/activity, raw SQL filters) served — closed briefs listing as open.

Pinned here:
  1. End-to-end congruence: a close through the generic POST /threads path (the
     exact ``skein close`` shape) reads back 'closed' on EVERY folio read surface
     — single-folio GET, list, /search, and /activity (the surface that was
     unenriched and stale).
  2. The dropped columns stay dropped: fresh-DDL dbs are born without them.
  3. ``drop_refs_control`` (the schema migration for existing dbs): removes
     exactly the three columns, preserves rows/values/indexes, idempotent,
     dry-run writes nothing.
  4. The archived feature is gone: PATCH no longer mints archive markers.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skein.migrations.drop_refs_control import migrate_db
from skein.models import Thread
from skein.utils import generate_thread_id

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def tmp_dir():
    d = Path(tempfile.mkdtemp(prefix="threads_only_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ── shared plumbing (the TestA2ControlWritesGenesisKeyed idiom) ───────────────

def _client_store(tmp_dir):
    from fastapi.testclient import TestClient
    from skein_server import app
    from skein.routes import get_project_store
    from skein.storage import JSONStore
    store = JSONStore(tmp_dir)
    app.dependency_overrides[get_project_store] = lambda: store
    return TestClient(app), store, app, get_project_store


def _create_folio(client, **extra):
    client.post("/skein/sites",
                json={"site_id": "s1", "purpose": "threads-only control tests"},
                headers={"X-Agent-Id": "agent-x"})
    payload = {"type": "finding", "site_id": "s1",
               "title": "A folio title here", "content": "body content"}
    payload.update(extra)
    r = client.post("/skein/folios", json=payload,
                    headers={"X-Agent-Id": "agent-x"})
    assert r.status_code == 200, r.text
    return r.json()["folio_id"]


def _close_via_generic_threads(client, fid):
    """Exactly what client/cli.py `skein close` sends."""
    r = client.post("/skein/threads",
                    json={"from_id": fid, "to_id": fid, "type": "status",
                          "content": "closed"},
                    headers={"X-Agent-Id": "agent-x"})
    assert r.status_code == 200, r.text


def _refs_columns(store):
    conn = sqlite3.connect(str(store._log_db.db_path))
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(refs)")]
    finally:
        conn.close()


def _mk_thread(from_id, to_id, ttype, content, *, when=None):
    return Thread(
        thread_id=generate_thread_id(), from_id=from_id, to_id=to_id,
        type=ttype, content=content, weaver="agent-x",
        created_at=when or datetime.now(timezone.utc))


class TestFreshSchemaHasNoControlColumns:
    def test_fresh_ddl_born_without_cache_columns(self, tmp_dir):
        _, store, app, dep = _client_store(tmp_dir)
        app.dependency_overrides.pop(dep, None)
        cols = _refs_columns(store)
        for dropped in ("status", "assigned_to", "archived"):
            assert dropped not in cols, f"fresh DDL still creates refs.{dropped}"
        for kept in ("slug", "genesis_hash", "head_hash", "site_id",
                     "target_agent", "omlet", "acknowledged_at", "metadata"):
            assert kept in cols


class TestThreadDerivedReadCongruence:
    """A generic-path close reads 'closed' on every folio read surface."""

    def test_close_reads_closed_on_all_surfaces(self, tmp_dir):
        client, store, app, dep = _client_store(tmp_dir)
        try:
            fid = _create_folio(client)
            _close_via_generic_threads(client, fid)

            # 1. single-folio GET
            r = client.get(f"/skein/folios/{fid}")
            assert r.status_code == 200 and r.json()["status"] == "closed"
            # 2. list
            r = client.get("/skein/folios", params={"site_id": "s1"})
            by_id = {f["folio_id"]: f for f in r.json()}
            assert by_id[fid]["status"] == "closed"
            # 3. unified /search with a status filter — both directions
            r = client.get("/skein/search",
                           params={"resources": "folios", "status": "closed"})
            hits = {f["folio_id"]
                    for f in r.json()["results"]["folios"]["items"]}
            assert fid in hits
            r = client.get("/skein/search",
                           params={"resources": "folios", "status": "open"})
            hits = {f["folio_id"]
                    for f in r.json()["results"]["folios"]["items"]}
            assert fid not in hits
            # 4. /activity — the surface that served the stale cache
            r = client.get("/skein/activity")
            by_id = {f["folio_id"]: f for f in r.json()["new_folios"]}
            assert fid in by_id and by_id[fid]["status"] == "closed"
        finally:
            app.dependency_overrides.pop(dep, None)

    def test_backdated_status_does_not_win(self, tmp_dir):
        """The reader's (created_at DESC, thread_id DESC) reduction is the only
        arbiter — a backdated insert cannot override a newer status."""
        client, store, app, dep = _client_store(tmp_dir)
        try:
            fid = _create_folio(client)
            _close_via_generic_threads(client, fid)
            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            store.save_thread(
                _mk_thread(fid, fid, "status", "investigating", when=yesterday))
            assert store.get_latest_statuses([fid]).get(fid) == "closed"
            r = client.get(f"/skein/folios/{fid}")
            assert r.json()["status"] == "closed"
        finally:
            app.dependency_overrides.pop(dep, None)

    def test_assignment_thread_derived(self, tmp_dir):
        client, store, app, dep = _client_store(tmp_dir)
        try:
            fid = _create_folio(client)
            store.save_thread(_mk_thread(fid, "agent-assignee-z", "assignment",
                                         "Assigned to agent-assignee-z"))
            r = client.get(f"/skein/folios/{fid}")
            assert r.json()["assigned_to"] == "agent-assignee-z"
        finally:
            app.dependency_overrides.pop(dep, None)

    def test_dangling_control_thread_is_harmless(self, tmp_dir):
        client, store, app, dep = _client_store(tmp_dir)
        try:
            fid = _create_folio(client)
            store.save_thread(
                _mk_thread("no-such-folio", "no-such-folio", "status", "closed"))
            r = client.get(f"/skein/folios/{fid}")
            assert r.json()["status"] == "open"  # bystander untouched
        finally:
            app.dependency_overrides.pop(dep, None)


class TestResponsePathsCarryControl:
    """deep_code_audit 2026-07-08 findings 1-3: response paths that used to get
    status 'for free' from the refs cache must now overlay thread-derived truth
    — a title-only PATCH response, the move response, and cross-project reads
    all returned model defaults ('open'/None) after the contraction."""

    def test_patch_title_only_response_keeps_closed_status(self, tmp_dir):
        """Finding 2: PATCH /folios/{id} with only a title change must not
        report a closed folio as open in its response."""
        client, store, app, dep = _client_store(tmp_dir)
        try:
            fid = _create_folio(client)
            _close_via_generic_threads(client, fid)
            store.save_thread(_mk_thread(fid, "agent-assignee-z", "assignment",
                                         "Assigned to agent-assignee-z"))
            r = client.patch(f"/skein/folios/{fid}",
                             json={"title": "a new title entirely"},
                             headers={"X-Agent-Id": "agent-x"})
            assert r.status_code == 200, r.text
            body = r.json()["folio"]
            assert body["status"] == "closed"
            assert body["assigned_to"] == "agent-assignee-z"
        finally:
            app.dependency_overrides.pop(dep, None)

    def test_move_response_keeps_closed_status(self, tmp_dir):
        """Finding 3: the move response folio is rebuilt from the head join and
        must carry the thread-derived status."""
        client, store, app, dep = _client_store(tmp_dir)
        try:
            fid = _create_folio(client)
            _close_via_generic_threads(client, fid)
            client.post("/skein/sites",
                        json={"site_id": "s2", "purpose": "move target"},
                        headers={"X-Agent-Id": "agent-x"})
            r = client.post(f"/skein/folios/{fid}/move",
                            json={"dest_site_id": "s2"},
                            headers={"X-Agent-Id": "agent-x"})
            assert r.status_code == 200, r.text
            body = r.json()["folio"]
            assert body["site_id"] == "s2"
            assert body["status"] == "closed"
        finally:
            app.dependency_overrides.pop(dep, None)

    def test_cross_project_read_carries_source_status(self, tmp_dir, monkeypatch):
        """Finding 1: a qualified project-prefixed read sets source_project and
        must enrich against the SOURCE project's store — not skip enrichment
        (post-contraction that returned 'open' regardless of truth)."""
        import skein.storage as storage_mod
        from skein.storage import JSONStore
        # A second project ('px') holding a closed folio.
        px_data = tmp_dir / "px" / ".skein" / "data"
        px_data.mkdir(parents=True)
        px_store = JSONStore(px_data)
        from skein.models import Folio
        f = Folio(folio_id="finding-20260708-xpro", type="finding",
                  site_id="px-site", created_at=datetime.now(timezone.utc),
                  created_by="agent-px", title="cross project folio",
                  content="body")
        px_store.save_folio(f)
        genesis = px_store._log_db.genesis_of_slug("finding-20260708-xpro")
        px_store.save_thread(_mk_thread(genesis, genesis, "status", "closed"))
        assert px_store.get_latest_statuses(
            ["finding-20260708-xpro"]).get("finding-20260708-xpro") == "closed"

        registry = {"px": {"path": str(tmp_dir / "px"),
                           "data_dir": str(px_data), "name": "px"}}
        monkeypatch.setattr(storage_mod, "load_project_registry",
                            lambda: registry)

        client, store, app, dep = _client_store(tmp_dir)
        try:
            r = client.get("/skein/folios/px:finding-20260708-xpro",
                           headers={"X-Project-Id": "test-project"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["source_project"] == "px"
            assert body["status"] == "closed", (
                "cross-project read lost the source project's status")
        finally:
            app.dependency_overrides.pop(dep, None)


class TestAuditRound2Regressions:
    """deep_code_audit round 2 (finding-20260708-2ate): three fixes that broke
    in new ways, pinned so they stay fixed."""

    def test_oracle_reports_nothing_verified_not_all_clean(self, tmp_dir):
        """r2 finding 1: under post-contraction code the A3 oracle must SKIP
        with 'oracle inapplicable' and summarize NOTHING VERIFIED — the earlier
        fix's label fell through main()'s dispatch to the OK branch and printed
        'All clean' while checking nothing."""
        import subprocess
        import sys
        from skein.storage import LogDatabase
        LogDatabase(tmp_dir / "post.db")  # current-DDL db
        proc = subprocess.run(
            [sys.executable, "-m", "skein.migrations.verify_threads_control",
             str(tmp_dir / "post.db")],
            capture_output=True, text=True,
            cwd=REPO_ROOT)
        out = proc.stdout
        assert "SKIP" in out and "oracle inapplicable" in out, out
        assert "NOTHING VERIFIED" in out, out
        assert "All clean" not in out, out
        assert "OK" not in out.replace("NOTHING VERIFIED", ""), out

    def test_present_empty_status_survives_overlay(self, tmp_dir):
        """r2 finding 2: a present-but-empty status thread value stays empty on
        every read surface — the or-chain collapsed '' back to 'open', and only
        on some surfaces (single-read vs list would have split)."""
        client, store, app, dep = _client_store(tmp_dir)
        try:
            fid = _create_folio(client)
            r = client.patch(f"/skein/folios/{fid}", json={"status": ""},
                             headers={"X-Agent-Id": "agent-x"})
            assert r.status_code == 200, r.text
            assert store.get_latest_statuses([fid]).get(fid) == ""
            # single read
            assert client.get(f"/skein/folios/{fid}").json()["status"] == ""
            # PATCH response itself
            assert r.json()["folio"]["status"] == ""
            # list surface (the batch enricher)
            by_id = {f["folio_id"]: f for f in client.get(
                "/skein/folios", params={"site_id": "s1"}).json()}
            assert by_id[fid]["status"] == ""
        finally:
            app.dependency_overrides.pop(dep, None)

    def test_hypothesis_next_carries_assignment(self, tmp_dir):
        """r2 finding 3: /hypotheses/next returns the folio with thread-derived
        assigned_to (it was the one folio-returning route never enriched)."""
        client, store, app, dep = _client_store(tmp_dir)
        try:
            fid = _create_folio(client, type="hypothesis",
                                title="a panel hypothesis",
                                content="hypothesis body")
            store.save_thread(_mk_thread(fid, "agent-hyp-owner", "assignment",
                                         "Assigned to agent-hyp-owner"))
            r = client.get("/skein/hypotheses/next/s1")
            assert r.status_code == 200, r.text
            hypo = r.json()["hypothesis"]
            assert hypo is not None and hypo["folio_id"] == fid
            assert hypo["assigned_to"] == "agent-hyp-owner"
        finally:
            app.dependency_overrides.pop(dep, None)

    def test_import_slug_collision_mints_no_orphan_thread(self, tmp_dir):
        """r2 finding 4: on a duplicate slug across site dirs the second file's
        row is skipped (first wins) — its control threads must NOT be minted
        (they would anchor on a genesis no ref points at, permanently
        unreadable) and it must not count as imported."""
        import json as jsonlib
        import sqlite3
        from skein.storage import LogDatabase
        db = LogDatabase(tmp_dir / "collide.db")
        for site, body, extra in (
                ("site-one", "first body", {}),
                ("site-two", "second body", {"status": "closed"})):
            d = tmp_dir / "sites" / site / "folios"
            d.mkdir(parents=True)
            (d / "issue-20260101-dupe.json").write_text(jsonlib.dumps({
                "folio_id": "issue-20260101-dupe", "type": "issue",
                "site_id": site, "created_at": "2026-01-01T00:00:00+00:00",
                "created_by": "old-agent", "title": "A colliding slug",
                "content": body, **extra}))
        count = db.migrate_folios_from_json(tmp_dir / "sites")
        assert count == 1  # the duplicate is not counted as imported
        assert db.get_latest_statuses(
            ["issue-20260101-dupe"]).get("issue-20260101-dupe") is None
        conn = sqlite3.connect(str(tmp_dir / "collide.db"))
        try:
            # No status thread exists at all — especially not one anchored on
            # the LOSING file's content hash.
            n = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE type='status'"
            ).fetchone()[0]
            assert n == 0
        finally:
            conn.close()


class TestAuditRound3ClassKills:
    """deep_code_audit round 3 (finding-20260708-jz9u): the class-level fixes.
    Class A = code referencing the dropped refs columns / removed archive reader
    (swept: backfill_versions_refs, cutover_threads_pk via _a1_reads,
    migrate_threads_control entry). Class B = truthiness coercion of control
    values (swept: hypothesis routes, JSON-import gating)."""

    def test_cutover_threads_pk_dry_run_survives_reader_removal(self, tmp_dir):
        """r3 finding 2 (class A): cutover_threads_pk's manifest path reaches
        _a1_reads, which must read the removed archive reader tolerantly — every
        invocation died on AttributeError before migrate_db even ran."""
        from skein.migrations.cutover_threads_pk import cutover_one
        from skein.storage import LogDatabase
        db_path = tmp_dir / "preswap.db"
        db = LogDatabase(db_path)  # fresh base DDL = pre-swap (thread_id PK)
        from skein.models import Folio
        f = Folio(folio_id="finding-20260101-swap", type="finding",
                  site_id="s", created_at=datetime.now(timezone.utc),
                  created_by="a", title="pk swap smoke", content="body")
        db.save_folio(f)
        db.save_thread(_mk_thread("finding-20260101-swap",
                                  "finding-20260101-swap", "status", "closed"))
        passed, label, problems = cutover_one("scratch", db_path, live=False)
        assert passed, f"dry-run cutover failed: {label} {problems}"

    def test_backfill_on_contracted_target_mints_control_threads(self, tmp_dir):
        """r3 finding 1 (class A): backfill_versions_refs against a CURRENT-DDL
        target (the fidelity fixture path) — the fixed 11-column INSERT crashed
        on 'no column named status'. Now schema-adaptive, and the legacy
        folios.status/assigned_to are carried as genesis-keyed threads."""
        import sqlite3
        from skein.migrations.backfill_versions_refs import backfill_db
        from skein.storage import LogDatabase
        db_path = tmp_dir / "fixturelike.db"
        LogDatabase(db_path)  # current DDL: contracted refs, pre-swap threads
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("""
                CREATE TABLE folios (
                    folio_id TEXT PRIMARY KEY, type TEXT, site_id TEXT,
                    created_at DATETIME, created_by TEXT, title TEXT,
                    content TEXT, status TEXT DEFAULT 'open', assigned_to TEXT,
                    target_agent TEXT, omlet TEXT, archived INTEGER DEFAULT 0,
                    metadata JSON, acknowledged_at DATETIME, content_hash TEXT)
            """)
            conn.execute(
                "INSERT INTO folios (folio_id, type, site_id, created_at, "
                "created_by, title, content, status, assigned_to) "
                "VALUES ('issue-20260101-lgfx', 'issue', 'legacy-site', "
                "'2026-01-01T00:00:00+00:00', 'old-agent', 'legacy fixture "
                "issue', 'body', 'closed', 'agent-legacy')")
            conn.commit()
        finally:
            conn.close()
        stats, records = backfill_db(db_path, dry_run=False)
        assert stats["ref_seeded"] == 1
        assert stats["control_threads_minted"] == 2  # status + assignment
        db = LogDatabase(db_path)
        assert db.get_latest_statuses(
            ["issue-20260101-lgfx"]).get("issue-20260101-lgfx") == "closed"
        assert db.get_latest_assignments(
            ["issue-20260101-lgfx"]).get("issue-20260101-lgfx") == "agent-legacy"

    def test_a3_migration_refuses_contracted_db_typed(self, tmp_dir):
        """Sweep sibling the audit did not flag: migrate_threads_control reads
        and rebuilds the dropped columns — on a contracted db it must refuse
        with PreconditionError, not die mid-flight on 'no such column'."""
        import pytest as _pytest
        from skein.migrations import migrate_threads_control as mtc
        from skein.storage import LogDatabase
        db_path = tmp_dir / "contracted.db"
        LogDatabase(db_path)
        with _pytest.raises(mtc.PreconditionError, match="post-contraction"):
            mtc.migrate_db(db_path, dry_run=True)

    def test_oracle_full_legs_still_run_on_precontraction_db(self, tmp_dir):
        """The capability the code-vintage gate wrongly killed: on a
        PRE-contraction db (control columns present) the oracle's legs run under
        current code — the archive leg reads tolerantly ({} == what the reader
        returned on every real db). A consistent db must NOT report
        'inapplicable'; it reaches the real leg outcomes."""
        import sqlite3
        from skein.migrations.verify_threads_control import verify_db
        from skein.storage import LogDatabase
        db_path = tmp_dir / "prectr.db"
        db = LogDatabase(db_path)
        from skein.models import Folio
        f = Folio(folio_id="finding-20260101-orcl", type="finding",
                  site_id="s", created_at=datetime.now(timezone.utc),
                  created_by="a", title="oracle smoke", content="body")
        db.save_folio(f)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("ALTER TABLE refs ADD COLUMN status TEXT DEFAULT 'open'")
            conn.execute("ALTER TABLE refs ADD COLUMN assigned_to TEXT")
            conn.execute("ALTER TABLE refs ADD COLUMN archived INTEGER DEFAULT 0")
            conn.commit()
        finally:
            conn.close()
        status, problems, warnings = verify_db(db_path)
        assert not status.startswith("oracle inapplicable"), status
        # A consistent db lands on a real leg outcome (incomplete without a
        # pre-snapshot; never diverged, never a crash).
        assert status in ("incomplete", "verified"), (status, problems)

    def test_hypothesis_empty_status_not_pending(self, tmp_dir):
        """r3 findings 3+4 (class B): a present-but-empty hypothesis status is
        NOT 'open' — it must not surface from /hypotheses/next nor count as
        pending in /hypotheses/status."""
        client, store, app, dep = _client_store(tmp_dir)
        try:
            fid = _create_folio(client, type="hypothesis",
                                title="empty-status hypothesis",
                                content="hypothesis body")
            store.save_thread(_mk_thread(fid, fid, "status", ""))
            r = client.get("/skein/hypotheses/next/s1")
            assert r.status_code == 200, r.text
            assert r.json()["hypothesis"] is None
            r = client.get("/skein/hypotheses/status/s1")
            assert r.json()["pending"] == 0
        finally:
            app.dependency_overrides.pop(dep, None)

    def test_import_preserves_empty_string_status(self, tmp_dir):
        """r3 finding 5 (class B): an explicit present-but-falsy legacy value is
        imported faithfully (a '' status mints a '' thread), not silently
        dropped by a truthiness gate."""
        import json as jsonlib
        from skein.storage import LogDatabase
        db = LogDatabase(tmp_dir / "emptyimp.db")
        d = tmp_dir / "sites" / "legacy-site" / "folios"
        d.mkdir(parents=True)
        (d / "issue-20260101-noop.json").write_text(jsonlib.dumps({
            "folio_id": "issue-20260101-noop", "type": "issue",
            "site_id": "legacy-site",
            "created_at": "2026-01-01T00:00:00+00:00",
            "created_by": "old-agent", "title": "empty status legacy",
            "content": "body", "status": ""}))
        assert db.migrate_folios_from_json(tmp_dir / "sites") == 1
        assert db.get_latest_statuses(
            ["issue-20260101-noop"]).get("issue-20260101-noop") == ""


class TestAuditRound4Guards:
    """deep_code_audit round 4 (finding-20260708-9y6v): schema-guard placement
    (same connection/copy as the work — race-free by construction) and
    preview/apply agreement."""

    def _contracted_db(self, tmp_dir, name):
        from skein.storage import LogDatabase
        p = tmp_dir / name
        LogDatabase(p)
        return p

    def _legacy_folios_table(self, db_path, **row):
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS folios (
                    folio_id TEXT PRIMARY KEY, type TEXT, site_id TEXT,
                    created_at DATETIME, created_by TEXT, title TEXT,
                    content TEXT, status TEXT DEFAULT 'open', assigned_to TEXT,
                    target_agent TEXT, omlet TEXT, archived INTEGER DEFAULT 0,
                    metadata JSON, acknowledged_at DATETIME, content_hash TEXT)
            """)
            base = dict(folio_id="issue-20260101-r4", type="issue",
                        site_id="s", created_at="2026-01-01T00:00:00+00:00",
                        created_by="a", title="round4 fixture", content="body")
            base.update(row)
            cols = ", ".join(base)
            conn.execute(
                f"INSERT INTO folios ({cols}) VALUES "
                f"({','.join('?' * len(base))})", tuple(base.values()))
            conn.commit()
        finally:
            conn.close()

    def test_backfill_dry_run_predicts_repair_refusal(self, tmp_dir):
        """r4 finding 1: dry_run+repair on a contracted target raises the SAME
        refusal apply does — a preview must never report stats for a run apply
        would refuse."""
        import pytest as _pytest
        from skein.migrations.backfill_versions_refs import backfill_db
        db = self._contracted_db(tmp_dir, "preview.db")
        self._legacy_folios_table(db)
        with _pytest.raises(RuntimeError, match="without --repair"):
            backfill_db(db, dry_run=True, repair=True)
        with _pytest.raises(RuntimeError, match="without --repair"):
            backfill_db(db, dry_run=False, repair=True)

    def test_oracle_refuses_post_contraction_pre_snapshot_typed(self, tmp_dir):
        """r4 findings 2+6: a post-contraction PRE-snapshot gets the typed
        'oracle inapplicable' refusal (checked on the materialized copies the
        legs read), never a raw OperationalError from leg_c's control SELECT."""
        import sqlite3
        from skein.migrations.verify_threads_control import verify_db
        from skein.storage import LogDatabase
        from skein.models import Folio
        # post db: pre-contraction shape (control columns present).
        post = tmp_dir / "post.db"
        db = LogDatabase(post)
        db.save_folio(Folio(
            folio_id="finding-20260101-r4o", type="finding", site_id="s",
            created_at=datetime.now(timezone.utc), created_by="a",
            title="oracle pre-side", content="body"))
        conn = sqlite3.connect(str(post))
        try:
            conn.execute("ALTER TABLE refs ADD COLUMN status TEXT DEFAULT 'open'")
            conn.execute("ALTER TABLE refs ADD COLUMN assigned_to TEXT")
            conn.execute("ALTER TABLE refs ADD COLUMN archived INTEGER DEFAULT 0")
            conn.commit()
        finally:
            conn.close()
        pre = self._contracted_db(tmp_dir, "pre.db")  # post-contraction snapshot
        status, problems, warnings = verify_db(post, pre_snapshot=pre)
        assert status.startswith("oracle inapplicable"), (status, problems)
        assert "pre-snapshot" in status

    def test_create_with_empty_status_mints_and_reads_empty(self, tmp_dir):
        """r4 finding 3: an explicit '' status at CREATE is minted and read
        back as '' — the truthy initial_status/sugar gates silently dropped it
        (the create-path sibling of the pinned PATCH-path invariant)."""
        client, store, app, dep = _client_store(tmp_dir)
        try:
            fid = _create_folio(client, metadata={"status": ""})
            assert store.get_latest_statuses([fid]).get(fid) == ""
            assert client.get(f"/skein/folios/{fid}").json()["status"] == ""
        finally:
            app.dependency_overrides.pop(dep, None)

    def test_backfill_logs_archived_drop_on_contracted_target(self, tmp_dir, capsys):
        """r4 finding 4: the contracted-target branch reports a dropped legacy
        archived=1 loudly (stats + warning), matching the JSON-import sibling."""
        from skein.migrations.backfill_versions_refs import backfill_db
        db = self._contracted_db(tmp_dir, "arch.db")
        self._legacy_folios_table(db, archived=1)
        stats, records = backfill_db(db, dry_run=False)
        assert stats["archived_dropped"] == 1
        assert "archived=true NOT carried" in capsys.readouterr().out

    def test_a3_live_path_refusal_also_typed(self, tmp_dir):
        """r4 finding 5 companion: the live (write) path refuses a contracted
        db with the SAME typed PreconditionError as dry-run — the check now
        runs under the held BEGIN IMMEDIATE lock on the work connection."""
        import pytest as _pytest
        from skein.migrations import migrate_threads_control as mtc
        db = self._contracted_db(tmp_dir, "live.db")
        with _pytest.raises(mtc.PreconditionError, match="post-contraction"):
            mtc.migrate_db(db, dry_run=False)


class TestJsonImportMintsControlThreads:
    """deep_code_audit 2026-07-08 finding 6: the cold JSON import (auto-run when
    refs is empty) must carry a legacy folio's status/assignee into the
    thread-truth model — silently importing closed folios as open is data loss."""

    def test_import_mints_status_and_assignment_threads(self, tmp_dir):
        import json as jsonlib
        from skein.storage import LogDatabase
        db = LogDatabase(tmp_dir / "import.db")
        folios_dir = tmp_dir / "sites" / "legacy-site" / "folios"
        folios_dir.mkdir(parents=True)
        (folios_dir / "issue-20260101-lgcy.json").write_text(jsonlib.dumps({
            "folio_id": "issue-20260101-lgcy", "type": "issue",
            "site_id": "legacy-site",
            "created_at": "2026-01-01T00:00:00+00:00",
            "created_by": "old-agent", "title": "A legacy closed issue",
            "content": "legacy body", "status": "closed",
            "assigned_to": "agent-legacy",
        }))
        count = db.migrate_folios_from_json(tmp_dir / "sites")
        assert count == 1
        assert db.get_latest_statuses(
            ["issue-20260101-lgcy"]).get("issue-20260101-lgcy") == "closed"
        assert db.get_latest_assignments(
            ["issue-20260101-lgcy"]).get("issue-20260101-lgcy") == "agent-legacy"
        # A later real status write must win the reduction over the imported one.
        gen = db.genesis_of_slug("issue-20260101-lgcy")
        db.save_thread(_mk_thread(gen, gen, "status", "open"))
        assert db.get_latest_statuses(
            ["issue-20260101-lgcy"]).get("issue-20260101-lgcy") == "open"

    def test_import_open_unassigned_mints_nothing(self, tmp_dir):
        import json as jsonlib
        from skein.storage import LogDatabase
        db = LogDatabase(tmp_dir / "import2.db")
        folios_dir = tmp_dir / "sites" / "legacy-site" / "folios"
        folios_dir.mkdir(parents=True)
        (folios_dir / "issue-20260101-open.json").write_text(jsonlib.dumps({
            "folio_id": "issue-20260101-open", "type": "issue",
            "site_id": "legacy-site",
            "created_at": "2026-01-01T00:00:00+00:00",
            "created_by": "old-agent", "title": "A legacy open issue",
            "content": "legacy body",
        }))
        assert db.migrate_folios_from_json(tmp_dir / "sites") == 1
        assert db.get_threads(type="status") == []
        assert db.get_threads(type="assignment") == []


class TestArchivedFeatureRemoved:
    def test_patch_archived_mints_no_marker(self, tmp_dir):
        """FolioUpdate no longer carries archived; a legacy client sending it is
        ignored (pydantic drops unknown fields) and no archive thread is minted."""
        client, store, app, dep = _client_store(tmp_dir)
        try:
            fid = _create_folio(client)
            r = client.patch(f"/skein/folios/{fid}", json={"archived": True},
                             headers={"X-Agent-Id": "agent-x"})
            assert r.status_code == 200, r.text
            assert store.get_threads(type="archive") == []
        finally:
            app.dependency_overrides.pop(dep, None)


class TestDropRefsControlMigration:
    """The schema migration for dbs born before the contraction."""

    def _legacy_db(self, tmp_dir):
        """A store whose refs still carries the three cache columns, with one
        folio whose cached status is STALE ('open' vs a closed-thread truth) —
        the exact pre-contraction live-db shape."""
        client, store, app, dep = _client_store(tmp_dir)
        fid = _create_folio(client)
        _close_via_generic_threads(client, fid)
        app.dependency_overrides.pop(dep, None)
        db = Path(store._log_db.db_path)
        conn = sqlite3.connect(str(db))
        try:
            # Recreate the legacy shape: add the columns back (fresh DDL no
            # longer has them), stale value included.
            conn.execute("ALTER TABLE refs ADD COLUMN status TEXT DEFAULT 'open'")
            conn.execute("ALTER TABLE refs ADD COLUMN assigned_to TEXT")
            conn.execute("ALTER TABLE refs ADD COLUMN archived INTEGER DEFAULT 0")
            for col in ("status", "assigned_to", "archived"):
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_refs_{col} ON refs({col})")
            conn.commit()
        finally:
            conn.close()
        return store, db, fid

    def test_drop_removes_columns_preserves_rows(self, tmp_dir):
        store, db, fid = self._legacy_db(tmp_dir)
        before_cols = _refs_columns(store)
        assert "status" in before_cols

        stats = migrate_db(db, dry_run=True)
        assert sorted(stats["drops"]) == ["archived", "assigned_to", "status"]
        assert "status" in _refs_columns(store)  # dry-run wrote nothing

        stats = migrate_db(db, dry_run=False)
        after_cols = _refs_columns(store)
        for dropped in ("status", "assigned_to", "archived"):
            assert dropped not in after_cols
        conn = sqlite3.connect(str(db))
        try:
            n = conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
            assert n == stats["refs"]  # rows preserved
            idx = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='refs'")}
            for col in ("site_id", "head_hash", "genesis_hash"):
                assert f"idx_refs_{col}" in idx
        finally:
            conn.close()
        # The store still reads correctly post-migration, thread-derived.
        assert store.get_folio(fid) is not None
        assert store.get_latest_statuses([fid]).get(fid) == "closed"

        stats = migrate_db(db, dry_run=False)  # second run: clean no-op
        assert stats["drops"] == []

    def test_migrated_db_full_read_write_cycle(self, tmp_dir):
        """After the drop, the API round-trips create/close/edit on the migrated
        db — no surviving code path expects the dropped columns."""
        store, db, fid = self._legacy_db(tmp_dir)
        migrate_db(db, dry_run=False)
        from fastapi.testclient import TestClient
        from skein_server import app
        from skein.routes import get_project_store
        app.dependency_overrides[get_project_store] = lambda: store
        try:
            client = TestClient(app)
            fid2 = _create_folio(client, title="post-migration folio")
            _close_via_generic_threads(client, fid2)
            r = client.get(f"/skein/folios/{fid2}")
            assert r.json()["status"] == "closed"
            r = client.patch(f"/skein/folios/{fid2}",
                             json={"status": "open", "title": "edited title"},
                             headers={"X-Agent-Id": "agent-x"})
            assert r.status_code == 200, r.text
            r = client.get(f"/skein/folios/{fid2}")
            assert r.json()["status"] == "open"
            assert r.json()["title"] == "edited title"
        finally:
            app.dependency_overrides.pop(get_project_store, None)


class TestFellRound1:
    """Genotype-fell round 1 (opus + codex, 2026-07-08).

    The tolerant archive read must not conflate reader-absent with
    archives-empty: the oracle now runs the removed reader's reduction INLINE
    when get_latest_archives is gone, so a db that really carries an archive
    thread is judged on its merits (codex: archived=0 + a real archive thread
    previously PASSED partial verification — a false green; opus: archived=1 +
    thread false-red'd). Plus the '' invariant on the hypothesis VERDICT write
    path (codex minor).
    """

    def test_a1_reads_reduces_archive_threads_without_reader(self, tmp_dir):
        """Reader absent ≠ archives empty: _a1_reads runs the frozen inline
        reduction, so a genesis-keyed archive thread surfaces as archived=1."""
        from skein.migrations.verify_threads_control import _a1_reads
        from skein.models import Folio
        from skein.storage import LogDatabase
        db_path = tmp_dir / "arch.db"
        db = LogDatabase(db_path)
        assert not hasattr(LogDatabase, "get_latest_archives")  # premise of the test
        fid = "finding-20260101-arcv"
        db.save_folio(Folio(
            folio_id=fid, type="finding", site_id="s",
            created_at=datetime.now(timezone.utc), created_by="a",
            title="archive thread carrier", content="body"))
        db.save_thread(_mk_thread(fid, fid, "archive", "archived"))
        out = _a1_reads(db_path)
        assert out[fid]["archived"] == 1
        # And with no archive thread the reduction is {}-equivalent (old
        # manifests keep comparing byte-for-byte).
        fid2 = "finding-20260101-noarc"
        db.save_folio(Folio(
            folio_id=fid2, type="finding", site_id="s",
            created_at=datetime.now(timezone.utc), created_by="a",
            title="no archive thread", content="body"))
        assert _a1_reads(db_path)[fid2]["archived"] == 0

    def test_archive_thread_vs_stale_cache_diverges_not_passes(self, tmp_dir):
        """The codex false-green scenario fails closed now: pre-contraction db,
        refs.archived=0, but a real archive thread — the old tolerant {} read
        let this PASS partial verification; the inline reduction makes the
        oracle report the divergence."""
        import sqlite3
        from skein.migrations.verify_threads_control import verify_db
        from skein.models import Folio
        from skein.storage import LogDatabase
        db_path = tmp_dir / "falsgrn.db"
        db = LogDatabase(db_path)
        fid = "finding-20260101-fgrn"
        db.save_folio(Folio(
            folio_id=fid, type="finding", site_id="s",
            created_at=datetime.now(timezone.utc), created_by="a",
            title="stale cache carrier", content="body"))
        db.save_thread(_mk_thread(fid, fid, "archive", "archived"))
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("ALTER TABLE refs ADD COLUMN status TEXT DEFAULT 'open'")
            conn.execute("ALTER TABLE refs ADD COLUMN assigned_to TEXT")
            conn.execute("ALTER TABLE refs ADD COLUMN archived INTEGER DEFAULT 0")
            conn.commit()
        finally:
            conn.close()
        status, problems, warnings = verify_db(db_path)
        assert status == "diverged", (status, problems)
        assert any("archived" in p for p in problems), problems

    def test_verdict_refuses_present_empty_status(self, tmp_dir):
        """A present '' status is state, not absence: /hypotheses/next already
        excludes it as non-open, so the verdict write path must refuse to
        re-verdict it (was: `if current_status:` let it through)."""
        client, store, app, dep = _client_store(tmp_dir)
        try:
            fid = _create_folio(client, type="hypothesis",
                                title="empty-status verdict target",
                                content="hypothesis body")
            store.save_thread(_mk_thread(fid, fid, "status", ""))
            r = client.post(f"/skein/hypotheses/{fid}/verdict",
                            json={"verdict": "disconfirmed",
                                  "note": "tried the thing"},
                            headers={"X-Agent-Id": "agent-x"})
            assert r.status_code == 400, r.text
            assert "Cannot re-verdict" in r.json()["detail"]
            # An open hypothesis still verdicts fine.
            fid2 = _create_folio(client, type="hypothesis",
                                 title="open verdict target",
                                 content="hypothesis body")
            r = client.post(f"/skein/hypotheses/{fid2}/verdict",
                            json={"verdict": "disconfirmed",
                                  "note": "tried the thing"},
                            headers={"X-Agent-Id": "agent-x"})
            assert r.status_code == 200, r.text
        finally:
            app.dependency_overrides.pop(dep, None)


class TestCapFindingsRefusalSwallow:
    """diff_audit cap findings (2026-07-08, post-fell): verify_db's typed
    refusal returns passed a fresh [] and silently discarded blockers already
    appended (digest mismatch; structural/Leg-B corruption), so a DETECTED
    corruption exited 0 as a skip. A refusal must never outrank a real problem.
    """

    def _precontraction_db(self, tmp_dir, name, fid):
        import sqlite3
        from skein.models import Folio
        from skein.storage import LogDatabase
        db_path = tmp_dir / name
        db = LogDatabase(db_path)
        db.save_folio(Folio(
            folio_id=fid, type="finding", site_id="s",
            created_at=datetime.now(timezone.utc), created_by="a",
            title="refusal swallow probe", content="body"))
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("ALTER TABLE refs ADD COLUMN status TEXT DEFAULT 'open'")
            conn.execute("ALTER TABLE refs ADD COLUMN assigned_to TEXT")
            conn.execute("ALTER TABLE refs ADD COLUMN archived INTEGER DEFAULT 0")
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_leg_blockers_survive_inapplicable_pre_snapshot(self, tmp_dir):
        """Post db carries a REAL Leg-B divergence (stale cache: refs.status
        'closed', no status thread); the pre-snapshot is post-contraction
        (inapplicable). Before the fix: 'oracle inapplicable', [], exit-0 skip —
        the detected corruption discarded. Now: diverged, blockers intact."""
        import sqlite3
        from skein.migrations.verify_threads_control import verify_db
        from skein.models import Folio
        from skein.storage import LogDatabase
        post = self._precontraction_db(tmp_dir, "post.db", "finding-20260101-swal")
        conn = sqlite3.connect(str(post))
        try:
            conn.execute("UPDATE refs SET status = 'closed'")
            conn.commit()
        finally:
            conn.close()
        # A post-contraction db as the (wrong) pre-snapshot.
        pre = tmp_dir / "pre.db"
        db2 = LogDatabase(pre)
        db2.save_folio(Folio(
            folio_id="finding-20260101-pres", type="finding", site_id="s",
            created_at=datetime.now(timezone.utc), created_by="a",
            title="post-contraction snapshot", content="body"))
        status, problems, warnings = verify_db(post, pre_snapshot=pre)
        assert status == "diverged", (status, problems, warnings)
        assert problems, "the Leg-B blocker must survive the refusal"
        # The refusal context is preserved as a warning, not lost.
        assert any("pre-snapshot" in w for w in warnings), warnings

    def test_refusal_still_clean_when_nothing_found(self, tmp_dir):
        """The refusal path is unchanged when no blocker was found: a clean
        pre-contraction post db + post-contraction pre-snapshot still reports
        inapplicable with no problems."""
        from skein.migrations.verify_threads_control import verify_db
        from skein.models import Folio
        from skein.storage import LogDatabase
        post = self._precontraction_db(tmp_dir, "post2.db", "finding-20260101-clnp")
        pre = tmp_dir / "pre2.db"
        db2 = LogDatabase(pre)
        db2.save_folio(Folio(
            folio_id="finding-20260101-prec", type="finding", site_id="s",
            created_at=datetime.now(timezone.utc), created_by="a",
            title="post-contraction snapshot", content="body"))
        status, problems, warnings = verify_db(post, pre_snapshot=pre)
        assert status.startswith("oracle inapplicable"), (status, problems)
        assert problems == []

    def test_digest_mode_still_runs_post_legs_on_inapplicable_pre(self, tmp_dir):
        """Fell r3 (opus, LOW): --expect-digest + a post-contraction
        pre-snapshot must not verify LESS than the unbound invocation. The
        digest refusal no longer early-returns; the post-db legs run and a
        real Leg-B divergence surfaces as 'diverged' (was: inapplicable, [],
        exit-0 with the corruption never examined)."""
        import sqlite3
        from skein.migrations.verify_threads_control import verify_db
        from skein.models import Folio
        from skein.storage import LogDatabase
        post = self._precontraction_db(tmp_dir, "post3.db", "finding-20260101-dgst")
        conn = sqlite3.connect(str(post))
        try:
            conn.execute("UPDATE refs SET status = 'closed'")
            conn.commit()
        finally:
            conn.close()
        pre = tmp_dir / "pre3.db"
        db2 = LogDatabase(pre)
        db2.save_folio(Folio(
            folio_id="finding-20260101-dpre", type="finding", site_id="s",
            created_at=datetime.now(timezone.utc), created_by="a",
            title="post-contraction snapshot", content="body"))
        status, problems, warnings = verify_db(
            post, pre_snapshot=pre, expect_digest="sha256:doesnotmatter")
        assert status == "diverged", (status, problems, warnings)
        assert problems, "post-db legs must have run and kept their blocker"
