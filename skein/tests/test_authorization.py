"""Thread F1 — authorization.py units (A1-A9) + account_bindings/binding_events
storage (B1-B9, B47, B_E1-B_E6).

The authorization half is orthogonal to signing: identity is the verified cert
(issuer, subject) pair, never created_by/weaver. Units run against an in-memory
fake BindingStore; the storage cells run against the real account_bindings +
binding_events tables.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import pytest

from skein.authorization import (
    Binding,
    OperatorAlreadyBootstrapped,
    Principal,
    bootstrap_operator,
    can_write,
)
from skein.store import SkeinNextStore


# --- in-memory fake BindingStore for the unit cells -------------------------


class FakeBindings:
    def __init__(self):
        self._rows: Dict[Tuple[str, str], Binding] = {}

    def get_binding(self, issuer: str, subject: str) -> Optional[Binding]:
        return self._rows.get((issuer, subject))

    def get_operator(self) -> Optional[Binding]:
        ops = [
            b for b in self._rows.values()
            if b.role == "operator" and b.revoked_at is None
        ]
        ops.sort(key=lambda b: b.created_at)
        return ops[0] if ops else None

    def add_binding(self, issuer, subject, role, vouched_by_issuer=None,
                    vouched_by_subject=None) -> Binding:
        b = Binding(issuer=issuer, subject=subject, role=role,
                    vouched_by_issuer=vouched_by_issuer,
                    vouched_by_subject=vouched_by_subject,
                    created_at="2026-01-01T00:00:00+00:00", revoked_at=None)
        self._rows[(issuer, subject)] = b
        return b

    def _revoke(self, issuer, subject):
        b = self._rows[(issuer, subject)]
        self._rows[(issuer, subject)] = Binding(
            b.issuer, b.subject, b.role, b.vouched_by_issuer, b.vouched_by_subject,
            b.created_at, "2026-02-01T00:00:00+00:00")


I, S = "https://accounts.google.com", "alice@example.com"


# --- A1-A9: authorization unit contracts ------------------------------------


def test_can_write_absent_binding_false():  # A1
    assert can_write(Principal(I, S), FakeBindings()) is False


def test_can_write_active_binding_true():  # A2
    fb = FakeBindings()
    fb.add_binding(I, S, "author", I, S)
    assert can_write(Principal(I, S), fb) is True


def test_can_write_revoked_binding_false():  # A3
    fb = FakeBindings()
    fb.add_binding(I, S, "author", I, S)
    fb._revoke(I, S)
    assert can_write(Principal(I, S), fb) is False


def test_can_write_keys_on_issuer_subject_pair():  # A4
    fb = FakeBindings()
    fb.add_binding(I, S, "author", I, S)
    assert can_write(Principal("https://other.idp", S), fb) is False
    assert can_write(Principal(I, "bob@example.com"), fb) is False


def test_can_write_has_no_created_by_parameter():  # A5
    import inspect

    params = list(inspect.signature(can_write).parameters)
    assert "created_by" not in params
    assert params[:2] == ["actor", "bindings"]


def test_bootstrap_operator_creates_self_vouched_row():  # A6
    fb = FakeBindings()
    b = bootstrap_operator(fb, Principal(I, S))
    assert b.role == "operator"
    assert b.vouched_by_issuer == I and b.vouched_by_subject == S
    assert b.revoked_at is None
    assert can_write(Principal(I, S), fb) is True


def test_bootstrap_operator_refuses_second_active_operator():  # A7
    fb = FakeBindings()
    bootstrap_operator(fb, Principal(I, S))
    with pytest.raises(OperatorAlreadyBootstrapped):
        bootstrap_operator(fb, Principal(I, "bob@example.com"))
    with pytest.raises(OperatorAlreadyBootstrapped):
        bootstrap_operator(fb, Principal(I, S))


def test_bootstrap_operator_allowed_after_operator_revoked():  # A8
    fb = FakeBindings()
    bootstrap_operator(fb, Principal(I, S))
    fb._revoke(I, S)
    b = bootstrap_operator(fb, Principal(I, "carol@example.com"))
    assert b.role == "operator" and b.revoked_at is None


def test_principal_built_from_cert_identity_dict():  # A9
    identity = {"issuer": I, "subject": S}
    p = Principal(issuer=identity["issuer"], subject=identity["subject"])
    assert p.issuer == I and p.subject == S


# --- B1-B9, B47: account_bindings storage -----------------------------------


@pytest.fixture
def store(tmp_path):
    s = SkeinNextStore(tmp_path / ".skein")
    yield s
    s.close()


OP = ("https://accounts.google.com", "op@example.com")
A2P = ("https://accounts.google.com", "author2@example.com")


def test_add_binding_then_get_active(store):  # B1
    store.add_binding(*A2P, role="author", vouched_by_issuer=OP[0], vouched_by_subject=OP[1])
    b = store.get_binding(*A2P)
    assert b is not None and b.role == "author" and b.revoked_at is None


def test_get_binding_absent_returns_none(store):  # B2
    assert store.get_binding("https://x", "nobody") is None


def test_revoke_binding_sets_revoked_at(store):  # B3
    store.add_binding(*A2P, role="author")
    store.revoke_binding(*A2P)
    b = store.get_binding(*A2P)
    assert b is not None and b.revoked_at is not None  # present, not deleted


def test_revoke_nonexistent_returns_false(store):  # B4
    assert store.revoke_binding("https://x", "ghost") is False
    store.add_binding(*A2P, role="author")
    store.revoke_binding(*A2P)
    assert store.revoke_binding(*A2P) is False  # already revoked


def test_readd_after_revoke_reactivates_preserving_created_at(store):  # B5
    store.add_binding(*A2P, role="author")
    t0 = store.get_binding(*A2P).created_at
    store.revoke_binding(*A2P)
    store.add_binding(*A2P, role="author")
    b = store.get_binding(*A2P)
    assert b.revoked_at is None and b.created_at == t0


def test_readd_active_is_idempotent(store):  # B6
    store.add_binding(*A2P, role="author")
    t0 = store.get_binding(*A2P).created_at
    store.add_binding(*A2P, role="author")
    assert store.get_binding(*A2P).created_at == t0
    assert len(store.list_active_bindings()) == 1


def test_bindings_keyed_on_pair(store):  # B7
    store.add_binding("https://idpA", "sub", role="author")
    assert store.get_binding("https://idpB", "sub") is None


def test_role_operator_vs_author_distinguishable(store):  # B8
    store.add_binding(*OP, role="operator", vouched_by_issuer=OP[0], vouched_by_subject=OP[1])
    store.add_binding(*A2P, role="author")
    assert store.get_operator().subject == OP[1]


def test_get_operator_none_when_revoked(store):  # B9
    store.add_binding(*OP, role="operator", vouched_by_issuer=OP[0], vouched_by_subject=OP[1])
    store.revoke_binding(*OP)
    assert store.get_operator() is None


def test_get_operator_single_valued_and_deterministic(store):  # B47
    # Corrupt state: TWO active operator rows; get_operator resolves deterministically.
    store.add_binding("https://idpA", "first", role="operator")  # created first
    store.add_binding("https://idpB", "second", role="operator")
    op = store.get_operator()
    assert op.subject == "first"  # ORDER BY created_at LIMIT 1
    assert store.count_active_operators() == 2


# --- B_E1-B_E6: binding_events append-only audit ----------------------------


def test_event_logged_on_create(store):  # B_E1
    store.add_binding(*A2P, role="author")
    events = store.get_binding_events()
    assert [e["event"] for e in events] == ["created"]


def test_event_logged_on_revoke(store):  # B_E2
    store.add_binding(*A2P, role="author")
    store.revoke_binding(*A2P)
    assert [e["event"] for e in store.get_binding_events()] == ["created", "revoked"]


def test_event_logged_on_reactivate(store):  # B_E3
    store.add_binding(*A2P, role="author")
    t0 = store.get_binding(*A2P).created_at
    store.revoke_binding(*A2P)
    store.add_binding(*A2P, role="author")
    assert [e["event"] for e in store.get_binding_events()] == [
        "created", "revoked", "reactivated",
    ]
    assert store.get_binding(*A2P).created_at == t0


def test_events_are_append_only(store):  # B_E4
    store.add_binding(*A2P, role="author")
    store.revoke_binding(*A2P)
    store.add_binding(*A2P, role="author")
    events = store.get_binding_events()
    assert len(events) == 3
    seqs = [e["event_seq"] for e in events]
    assert seqs == sorted(seqs)  # insertion order, monotonic


def test_rotate_logs_paired_events(store):  # B_E5
    store.add_binding(*OP, role="operator", vouched_by_issuer=OP[0], vouched_by_subject=OP[1])
    new = ("https://accounts.google.com", "newop@example.com")
    with store.transaction():
        store.revoke_binding(*OP, event="rotated_out")
        store.add_binding(*new, role="operator", vouched_by_issuer=OP[0],
                          vouched_by_subject=OP[1], event="rotated_in")
    events = [e["event"] for e in store.get_binding_events()]
    assert "rotated_out" in events and "rotated_in" in events


def test_rotate_onto_existing_author_logs_promoted(store):  # B_E6
    store.add_binding(*OP, role="operator", vouched_by_issuer=OP[0], vouched_by_subject=OP[1])
    store.add_binding(*A2P, role="author")
    t0 = store.get_binding(*A2P).created_at
    with store.transaction():
        store.revoke_binding(*OP, event="rotated_out")
        store.promote_to_operator(*A2P)
    b = store.get_binding(*A2P)
    assert b.role == "operator" and b.created_at == t0
    assert "promoted" in [e["event"] for e in store.get_binding_events()]
