"""Concurrent-writer correctness for the ingress write path.

The ingress opens a SQLite connection per request and runs ingest in a
threadpool, so concurrent publishes overlap. With a DEFERRED transaction the
read-then-write pattern (get_folio -> create_folio) deadlocks on the shared->
reserved lock upgrade and SQLite returns 'database is locked' instantly, which
the per-item handler used to record as 'invalid fields' — silently dropping
valid concurrent publishes. transaction() now uses BEGIN IMMEDIATE + a non-zero
busy_timeout so writers serialize and all valid writes commit.
"""

from __future__ import annotations

import concurrent.futures as cf
import sqlite3

import pytest

from skein.station import Station
from skein.ingress import ingest
from skein.store import SkeinStore
from skein import wire


def _make_folios(tmp_path, n):
    src = Station(tmp_path / "src" / ".skein")
    try:
        src.create_site("specs", purpose="p", created_by="t")
        return [
            src.store.get_folio(
                src.post("finding", "specs", f"Title {i}", f"body number {i}", created_by="t")
            )
            for i in range(n)
        ]
    finally:
        src.close()


def test_concurrent_writers_all_commit_none_mislabeled(tmp_path):
    n = 30
    folios = _make_folios(tmp_path, n)
    inst = tmp_path / "inst" / ".skein"
    Station(inst).close()  # materialize

    def writer(f):
        st = Station(inst, check_same_thread=False)
        try:
            return ingest(
                st,
                {"protocol": wire.PROTOCOL, "folios": [f], "threads": [], "site_slugs": {}},
                require_signed=False,
            )
        finally:
            st.close()

    with cf.ThreadPoolExecutor(max_workers=n) as ex:
        acks = list(ex.map(writer, folios))

    accepted = sum(len(a["accepted"]) for a in acks)
    rejected = [r for a in acks for r in a["rejected"]]
    # every valid folio commits; none dropped (and so none mislabeled 'invalid fields')
    assert accepted == n, f"expected all {n} to commit, got {accepted}; rejects={rejected}"
    assert rejected == []

    chk = Station(inst, check_same_thread=False)
    try:
        persisted = sum(1 for f in folios if chk.store.get_folio(f["content_hash"]) is not None)
    finally:
        chk.close()
    assert persisted == n


def test_failed_begin_immediate_does_not_wedge_store(tmp_path):
    # If BEGIN IMMEDIATE times out (another connection holds the write lock longer
    # than busy_timeout), transaction() raises but must leave the store CLEAN —
    # _in_batch must NOT stay True (which would skip later commits and falsely trip
    # the not-re-entrant guard). The store must be usable once the lock frees.
    d = tmp_path / "s" / ".skein"
    SkeinStore(d).close()  # materialize

    holder = SkeinStore(d, check_same_thread=False)
    victim = SkeinStore(d, check_same_thread=False)
    victim.conn.execute("PRAGMA busy_timeout=50")  # expire the wait fast

    holder.conn.execute("BEGIN IMMEDIATE")  # hold the write lock
    try:
        with pytest.raises(sqlite3.OperationalError):
            with victim.transaction():
                pass  # never reached — BEGIN IMMEDIATE fails to take the lock
        assert victim._in_batch is False  # not wedged
    finally:
        holder.conn.rollback()
        holder.close()

    # lock is free now: the victim store re-enters transaction() cleanly
    with victim.transaction():
        pass
    victim.close()
