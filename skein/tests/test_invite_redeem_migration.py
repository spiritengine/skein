"""Invite-table migration safety (brief-20260618-yljd Phase A.3).

The invite/redeem bundle adds two tables — ``invites`` and ``invite_events`` — to
the store schema. They materialize via the ``CREATE TABLE IF NOT EXISTS`` DDL that
``executescript`` runs every time the store opens read_write (the ingress open).
There is no ALTER and no data backfill: a deploy onto the live served corpus is a
pure additive table creation.

These tests prove the migration is safe on a copy of a realistic PRE-migration
corpus (one with NO invite tables, as the live host has today):

1. Opening it read_write materializes both tables (empty) and leaves every
   pre-existing folio/thread row byte-identical.
2. The migration is idempotent (a second open changes nothing).
3. The READ app (the ``:ro`` mount) is unaffected — it never queries the invite
   tables, so their absence pre-migration cannot fault a read.
4. Rollback posture: under require_signed=OFF a publish behaves identically whether
   or not the invite tables exist (the tables are write-surface state the publish
   path never consults).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from skein import wire
from skein.ingress import ingest
from skein.station import Station
from skein.store import SkeinNextStore


def _tables(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}


def _build_pre_migration_corpus(path) -> dict:
    """A realistic corpus as the live host has it TODAY: full schema MINUS the two
    invite tables. Returns a snapshot of the pre-existing folio/thread rows."""
    s = Station(path)
    try:
        s.create_site("specs", purpose="Public specs", created_by="op")
        for i in range(5):
            s.post("finding", "specs", f"Finding {i}", f"body {i}", created_by="op")
        # an operator binding (realistic write-surface state that must survive)
        s.store.add_binding(
            "https://accounts.google.com", "operator@example.com",
            role="operator", event="init",
        )
        # snapshot the rows that must be untouched by the migration
        folios = [dict(r) for r in s.store.conn.execute(
            "SELECT * FROM folios ORDER BY content_hash").fetchall()]
        threads = [dict(r) for r in s.store.conn.execute(
            "SELECT * FROM threads ORDER BY thread_hash").fetchall()]
        bindings = [dict(r) for r in s.store.conn.execute(
            "SELECT * FROM account_bindings ORDER BY issuer, subject").fetchall()]
        # simulate the PRE-migration state: drop the invite tables this bundle adds
        s.store.conn.execute("DROP TABLE IF EXISTS invites")
        s.store.conn.execute("DROP TABLE IF EXISTS invite_events")
        s.store.conn.commit()
        assert "invites" not in _tables(s.store.conn)
        assert "invite_events" not in _tables(s.store.conn)
    finally:
        s.close()
    return {"folios": folios, "threads": threads, "bindings": bindings}


def test_open_read_write_materializes_invite_tables_without_disturbing_rows(tmp_path):
    d = tmp_path / "corpus" / ".skein"
    snap = _build_pre_migration_corpus(d)

    # THE MIGRATION: open the store read_write the way the ingress does.
    s = Station(d, check_same_thread=False)
    try:
        tabs = _tables(s.store.conn)
        assert "invites" in tabs and "invite_events" in tabs
        # the two new tables are empty
        assert s.store.conn.execute("SELECT COUNT(*) FROM invites").fetchone()[0] == 0
        assert s.store.conn.execute("SELECT COUNT(*) FROM invite_events").fetchone()[0] == 0
        # every pre-existing row is byte-identical
        folios = [dict(r) for r in s.store.conn.execute(
            "SELECT * FROM folios ORDER BY content_hash").fetchall()]
        threads = [dict(r) for r in s.store.conn.execute(
            "SELECT * FROM threads ORDER BY thread_hash").fetchall()]
        bindings = [dict(r) for r in s.store.conn.execute(
            "SELECT * FROM account_bindings ORDER BY issuer, subject").fetchall()]
        assert folios == snap["folios"]
        assert threads == snap["threads"]
        assert bindings == snap["bindings"]
        # the freshly-created invites table carries the hardened columns (the flood
        # counter), so a brand-new corpus and a migrated one are schema-identical
        cols = {r[1] for r in s.store.conn.execute("PRAGMA table_info(invites)").fetchall()}
        assert {"failed_attempts", "attempts_window_start", "bound_issuer",
                "bound_subject", "redeemed_at", "used_at", "revoked_at"} <= cols
    finally:
        s.close()


def test_migration_is_idempotent(tmp_path):
    d = tmp_path / "corpus" / ".skein"
    _build_pre_migration_corpus(d)
    # first migrating open
    Station(d, check_same_thread=False).close()
    s = Station(d, check_same_thread=False)
    try:
        schema1 = sorted(r[0] for r in s.store.conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall())
    finally:
        s.close()
    # second migrating open — schema must be unchanged
    s = Station(d, check_same_thread=False)
    try:
        schema2 = sorted(r[0] for r in s.store.conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall())
    finally:
        s.close()
    assert schema1 == schema2


def test_read_only_open_unaffected_by_absent_invite_tables(tmp_path):
    """The read app mounts the corpus read_only (no DDL). On a PRE-migration corpus
    (no invite tables) a read must still work — the read path never queries them."""
    d = tmp_path / "corpus" / ".skein"
    snap = _build_pre_migration_corpus(d)

    ro = SkeinNextStore(d, read_only=True, check_same_thread=False)
    try:
        # the read path queries folios/threads, never invites — these must succeed
        n_folios = ro.count_folios()
        assert n_folios == len(snap["folios"])
        # a representative read fetch works
        first_hash = snap["folios"][0]["content_hash"]
        assert ro.get_folio(first_hash) is not None
        # and the invite tables are genuinely absent on this :ro corpus
        assert "invites" not in _tables(ro.conn)
        # a direct read of the absent table would fault — proving the read path's
        # safety is that it NEVER issues such a query, not that it tolerates one
        with pytest.raises(sqlite3.OperationalError):
            ro.conn.execute("SELECT * FROM invites").fetchall()
    finally:
        ro.close()


def test_rollback_publish_byte_identical_with_or_without_invite_tables(tmp_path):
    """require_signed=OFF rollback posture: a publish admits on integrity alone and
    its result is identical whether the invite tables exist (post-migration) or not
    (pre-migration). The invite tables are write-surface state publish never reads."""
    # a folio to publish
    src = Station(tmp_path / "src" / ".skein")
    try:
        src.create_site("s", purpose="p", created_by="t")
        folio = src.store.get_folio(src.post("finding", "s", "T", "b", created_by="t"))
    finally:
        src.close()
    batch = {"protocol": wire.PROTOCOL, "folios": [folio], "threads": [], "site_slugs": {}}

    # (a) pre-migration corpus: drop invite tables, publish via a read_write open.
    pre = tmp_path / "pre" / ".skein"
    _build_pre_migration_corpus(pre)
    # NOTE: opening read_write re-creates the tables (that IS the migration); to keep
    # this corpus genuinely pre-migration we open, drop again in the same connection,
    # and ingest without re-running the DDL. Simpler: ingest on a fresh-but-stripped
    # store object whose tables we drop right before the call.
    s_pre = Station(pre, check_same_thread=False)
    try:
        s_pre.store.conn.execute("DROP TABLE IF EXISTS invites")
        s_pre.store.conn.execute("DROP TABLE IF EXISTS invite_events")
        s_pre.store.conn.commit()
        assert "invites" not in _tables(s_pre.store.conn)
        ack_pre = ingest(s_pre, batch, require_signed=False)
    finally:
        s_pre.close()

    # (b) post-migration corpus: tables present (default open), same publish.
    post = tmp_path / "post" / ".skein"
    _build_pre_migration_corpus(post)
    s_post = Station(post, check_same_thread=False)
    try:
        assert "invites" in _tables(s_post.store.conn)
        ack_post = ingest(s_post, batch, require_signed=False)
    finally:
        s_post.close()

    assert ack_pre == ack_post
    assert len(ack_pre["accepted"]) == 1
