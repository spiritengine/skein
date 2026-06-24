"""Thread B — manifests store + unified constituent_attribution (ST1-ST10).

ONE manifest store (triple PK) + ONE attribution table over folios AND threads,
replacing the dissolved per-folio folio_signatures path and the rev-5 two-table
batch/thread FK seam. The B50 two-signers-same-set property survives on the
(root, issuer, subject) triple PK (ST3).
"""

from __future__ import annotations

import json

import pytest

from skein.store import SkeinStore


ROOT_A = "sha256::" + "a" * 64
ROOT_B = "sha256::" + "b" * 64
MH_A = "sha256::" + "c" * 64
MH_B = "sha256::" + "d" * 64
ISS = "https://accounts.google.com"
LEAVES = json.dumps(["sha256::" + "1" * 64, "sha256::" + "2" * 64])
DESC = json.dumps({"root": ROOT_A, "leaf_count": 2})


@pytest.fixture
def store(tmp_path):
    s = SkeinStore(tmp_path / ".skein")
    yield s
    s.close()


def _add_manifest(store, root=ROOT_A, mh=MH_A, issuer=ISS, subject="alice",
                  bundle="bundleA", leaf_count=2, leaves=LEAVES, desc=DESC):
    store.add_manifest(root, mh, desc, leaves, bundle, issuer, subject, leaf_count)


def test_add_manifest_then_get_proof(store):  # ST1
    _add_manifest(store)
    proof = store.get_manifest_proof(ROOT_A, ISS, "alice")
    assert proof is not None
    assert proof["descriptor_json"] == DESC
    assert proof["leaf_list_json"] == LEAVES
    assert proof["bundle_json"] == "bundleA"
    assert proof["issuer"] == ISS and proof["subject"] == "alice"
    assert proof["leaf_count"] == 2
    assert proof["manifest_hash"] == MH_A


def test_add_manifest_idempotent_same_signer(store):  # ST2
    _add_manifest(store)
    first = store.get_manifest_proof(ROOT_A, ISS, "alice")["created_at"]
    _add_manifest(store, bundle="DIFFERENT")  # same triple -> ignored
    rows = store.get_manifest_proofs_by_root(ROOT_A)
    assert len(rows) == 1
    again = store.get_manifest_proof(ROOT_A, ISS, "alice")
    assert again["created_at"] == first
    assert again["bundle_json"] == "bundleA"  # not overwritten


def test_two_signers_same_set_each_retain_proof(store):  # ST3 (B50 property)
    _add_manifest(store, subject="alice", bundle="aliceB")
    _add_manifest(store, subject="bob", bundle="bobB")  # SAME root, different signer
    rows = store.get_manifest_proofs_by_root(ROOT_A)
    assert len(rows) == 2
    subjects = {r["subject"] for r in rows}
    assert subjects == {"alice", "bob"}
    assert store.get_manifest_proof(ROOT_A, ISS, "bob")["bundle_json"] == "bobB"
    assert store.get_manifest_proof(ROOT_A, ISS, "alice")["bundle_json"] == "aliceB"


def test_add_attribution_first_manifest_wins(store):  # ST4 (Q3)
    _add_manifest(store, root=ROOT_A, subject="alice")
    _add_manifest(store, root=ROOT_B, mh=MH_B, subject="bob",
                  desc=json.dumps({"root": ROOT_B, "leaf_count": 2}))
    h = "sha256::" + "f" * 64
    store.add_constituent_attribution(h, "folio", ROOT_A, ISS, "alice")
    store.add_constituent_attribution(h, "folio", ROOT_B, ISS, "bob")  # later -> ignored
    proof = store.get_constituent_proof(h)
    assert proof["root"] == ROOT_A
    assert proof["subject"] == "alice"
    assert proof["kind"] == "folio"


def test_get_constituent_proof_missing_parent_returns_sentinel(store):  # ST5
    # Construct a dangling attribution row with FK temporarily off, then read it.
    store.conn.execute("PRAGMA foreign_keys=OFF")
    h = "sha256::" + "e" * 64
    store.add_constituent_attribution(h, "folio", "sha256::" + "9" * 64, ISS, "ghost")
    store.conn.execute("PRAGMA foreign_keys=ON")
    proof = store.get_constituent_proof(h)
    assert proof is not None
    assert proof.get("proof_missing") is True
    # the denormalized identity is still display-authoritative
    assert proof["issuer"] == ISS and proof["subject"] == "ghost"


def test_attribution_fk_binds_proof_signer_to_display_signer(store):  # ST6
    _add_manifest(store, subject="alice", bundle="aliceB")
    _add_manifest(store, subject="bob", bundle="bobB")
    h = "sha256::" + "7" * 64
    store.add_constituent_attribution(h, "folio", ROOT_A, ISS, "alice")
    proof = store.get_constituent_proof(h)
    assert proof["subject"] == "alice"
    assert proof["bundle_json"] == "aliceB"  # resolves to Alice's, never Bob's


def test_pragma_foreign_keys_on_at_connection_open(store):  # ST7
    assert store.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_write_order_manifest_before_attribution_same_savepoint(store):  # ST8
    import sqlite3

    # With foreign_keys ON, an attribution whose manifest parent was NOT written
    # raises an integrity error — proving the parent must come first.
    h = "sha256::" + "8" * 64
    with pytest.raises(sqlite3.IntegrityError):
        store.add_constituent_attribution(h, "folio", "sha256::" + "0" * 64, ISS, "nobody")


def test_created_at_immutable_both_tables(store):  # ST9
    _add_manifest(store, subject="alice")
    h = "sha256::" + "6" * 64
    store.add_constituent_attribution(h, "folio", ROOT_A, ISS, "alice")
    m_first = store.get_manifest_proof(ROOT_A, ISS, "alice")["created_at"]
    a_first = store.get_constituent_proof(h)["created_at"]
    _add_manifest(store, subject="alice", bundle="X")
    store.add_constituent_attribution(h, "folio", ROOT_A, ISS, "alice")
    assert store.get_manifest_proof(ROOT_A, ISS, "alice")["created_at"] == m_first
    assert store.get_constituent_proof(h)["created_at"] == a_first


def test_get_manifest_proofs_by_root_returns_set(store):  # ST10
    _add_manifest(store, subject="alice")
    _add_manifest(store, subject="bob")
    rows = store.get_manifest_proofs_by_root(ROOT_A)
    assert isinstance(rows, list) and len(rows) == 2
