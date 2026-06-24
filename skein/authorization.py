"""Account authorization — who may write to a mesh instance (Thread F1 / 81fm §E).

Orthogonal to signing. A manifest's signature proves *who* signed (a verified
Sigstore ``issuer``/``subject`` pair); authorization asks whether that verified
identity is a *bound, non-revoked author* on THIS instance. The two compose: the
ingress admits a constituent only when its covering manifest verifies AND the
manifest signer is bound here (``can_write``), and the read verdict recomputes the
same binding live per read.

The only gateable thing is the cert identity (the ``Principal``). ``created_by``
on a folio and ``weaver`` on a thread are unsigned display content — never an
authorization input (A5). Bindings live in the per-instance ``account_bindings``
sidecar (the :class:`~skein.store.SkeinStore` methods); this module owns
the pure predicate (``can_write``) and the bootstrap policy (``bootstrap_operator``)
over a :class:`BindingStore`, so they unit-test against an in-memory fake with no
crypto and no DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class Principal:
    """A verified cert identity — the only authorization-gateable thing.

    Built from an already-verified ``{"issuer", "subject"}`` (A9); this module
    never parses a cert. A non-VERIFIED result yields no Principal and never
    reaches :func:`can_write`."""

    issuer: str
    subject: str


@dataclass(frozen=True)
class Binding:
    """An account binding row: a (issuer, subject) authorized as operator or author.

    ``revoked_at`` NULL means active. ``created_at`` is the immutable first-insert
    timestamp (preserved across revoke/reactivate, B5). ``vouched_by_*`` records
    who authorized the binding (self for the bootstrapped operator)."""

    issuer: str
    subject: str
    role: str  # 'operator' | 'author'
    vouched_by_issuer: Optional[str]
    vouched_by_subject: Optional[str]
    created_at: Optional[str]
    revoked_at: Optional[str]


class OperatorAlreadyBootstrapped(Exception):
    """An attempt to bootstrap a second active operator (the single-operator
    invariant; refuse iff an ACTIVE operator already exists, A7)."""


class BindingStore(Protocol):
    """The binding surface ``can_write`` / ``bootstrap_operator`` consume.

    Satisfied structurally by :class:`~skein.store.SkeinStore` (its
    account_bindings methods) and by the in-memory fake the unit cells use."""

    def get_binding(self, issuer: str, subject: str) -> Optional[Binding]: ...

    def get_operator(self) -> Optional[Binding]: ...

    def add_binding(
        self,
        issuer: str,
        subject: str,
        role: str,
        vouched_by_issuer: Optional[str] = None,
        vouched_by_subject: Optional[str] = None,
    ) -> Binding: ...


def can_write(actor: Principal, bindings: BindingStore) -> bool:
    """True iff ``actor`` has an ACTIVE binding (exists AND ``revoked_at`` is NULL).

    Keys on the verified cert (issuer, subject) PAIR — same email under a
    different IdP inherits nothing (A4). Has no ``created_by`` parameter: the
    function never receives unsigned display content (A5)."""
    binding = bindings.get_binding(actor.issuer, actor.subject)
    return binding is not None and binding.revoked_at is None


def bootstrap_operator(bindings: BindingStore, operator: Principal) -> Binding:
    """Install the self-vouched root operator, refusing a second active one.

    Refuse iff an ACTIVE operator already exists (A7); allowed once the prior
    operator is revoked (A8). The bootstrapped row vouches for itself (A6)."""
    if bindings.get_operator() is not None:
        raise OperatorAlreadyBootstrapped(
            "an active operator already exists; refusing to bootstrap a second"
        )
    return bindings.add_binding(
        operator.issuer,
        operator.subject,
        role="operator",
        vouched_by_issuer=operator.issuer,
        vouched_by_subject=operator.subject,
    )


def default_bindings(store) -> BindingStore:
    """The default BindingStore for the ingress/CLI: the station's own store.

    ``SkeinStore`` implements the BindingStore surface directly, so this is
    the identity wrapper that names the intent at call sites (Thread E CD6/C13)."""
    return store
