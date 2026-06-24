"""Invite redemption — the station-side orchestration (brief-20260615-ofv1).

This is the heart of the agent-mediated onboarding ceremony: a collaborator's
agent presents ``{token, proof}`` to ``/invite/redeem``; the station cheaply
checks the token, verifies a TOKEN-BOUND Sigstore proof, and atomically burns the
invite + auto-binds the discovered identity as an author.

The ordering is the load-bearing invariant (INV-2): every CHEAP rejection (unknown
/ used / expired / revoked token, per-token flood cap) happens BEFORE any crypto,
and the expensive ``verify_multi`` runs OUTSIDE any write transaction holding NO
lock. Only the bounded burn-and-bind (``store.redeem_invite_cas``) takes the
single-writer lock, and only for the duration of a conditional ``UPDATE...WHERE``
rowcount check. Running ``verify_multi`` inside the lock would wedge every
concurrent publish and redeem behind a multi-second Fulcio/Rekor round-trip — the
redeem analogue of why ``store.transaction()`` uses BEGIN IMMEDIATE.

This module is PURE over the station + a verifier so it unit-tests against the
fake binding-verifier the rest of the suite uses, with no live Sigstore. It NEVER
raises over hostile input except a genuine SQLite lock (``OperationalError`` from
``redeem_invite_cas``), which the route maps to a 503 the client can retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from . import sign as _sign
from .identity import hash_token

# Per-token failed-verify flood backstop (INV-5). A leaked valid-but-UNUSED token
# is not burned until a SUCCESSFUL redeem, so without a cap an attacker holding the
# token could drive unbounded expensive verifies against it. After CAP failures
# within WINDOW seconds, the pre-crypto gate rejects further attempts cheaply until
# the window rolls over. The legitimate collaborator redeems once; nginx's per-IP
# limit_req is the front-line, this is the per-token logical backstop behind it.
REDEEM_ATTEMPT_CAP = 5
REDEEM_ATTEMPT_WINDOW_SECONDS = 600  # 10 minutes


# Machine status codes. The route maps these to HTTP; the CLI surfaces the reason.
class RedeemStatus:
    OK_REDEEMED = "redeemed"            # burned + bound fresh
    OK_ALREADY = "already_redeemed_self"  # idempotent re-redeem by the same identity (INV-6)
    UNKNOWN = "unknown_token"           # no such invite
    EXPIRED = "expired"                 # unredeemed + past expiry
    REVOKED_INVITE = "revoked_invite"   # the invite was revoked by the operator
    ALREADY_REDEEMED = "already_redeemed"  # burned, but by a DIFFERENT identity
    RACE_LOST = "race_lost"             # concurrent winner / became invalid mid-redeem
    REVOKED_IDENTITY = "revoked_identity"  # identity holds a revoked binding (INV-3)
    PROOF_MALFORMED = "proof_malformed"  # non-crypto proof shape failure
    PROOF_REJECTED = "proof_rejected"   # crypto/binding failure (SIGNATURE_MISMATCH, ...)
    RATE_LIMITED = "rate_limited"       # per-token attempt cap hit


# verify_wire_redeem BARE reasons that are SHAPE failures (400-class) vs crypto
# failures (401-class). Anything not a shape reason is a VerifyStatus value.
_PROOF_SHAPE_REASONS = frozenset({"proof malformed", "unknown profile", "wrong kind"})


@dataclass(frozen=True)
class RedeemResult:
    """The outcome of a redeem attempt. ``ok`` is the only success signal; on
    success ``issuer``/``subject`` carry the bound identity. ``status`` is a stable
    machine code (see :class:`RedeemStatus`); ``reason`` is a human-facing line."""

    ok: bool
    status: str
    reason: str
    issuer: Optional[str] = None
    subject: Optional[str] = None


def _parse(ts: Optional[str]) -> Optional[datetime]:
    """Parse a stored ISO stamp to an aware UTC datetime (None on absent/garbage).

    A naive stamp is assumed UTC; an aware one is converted to UTC — so a later
    ``<= now`` comparison is always tz-consistent regardless of the stored offset."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _attempts_exceeded(row: dict, now: datetime, cap: int, window_seconds: int) -> bool:
    """ADVISORY (lock-free) read of the per-token cap from an already-fetched row.

    A dirty pre-filter to shed a clear flood BEFORE taking the BEGIN IMMEDIATE lock
    in reserve_redeem_attempt — it reads the row the cheap lookup already fetched,
    so it adds no query. It is NOT authoritative (the row is stale outside the
    lock); reserve_redeem_attempt's atomic conditional UPDATE remains the
    boundary-correct gate. False on no window / rolled-over window."""
    started = _parse(row.get("attempts_window_start"))
    if started is None:
        return False
    if (now - started).total_seconds() > window_seconds:
        return False
    return (row.get("failed_attempts") or 0) >= cap


def redeem(
    station: Any,
    token: Any,
    proof: Any,
    origin: str,
    *,
    route: str = _sign.REDEEM_ROUTE,
    verifier: "_sign.Verifier" = None,
    attempt_cap: int = REDEEM_ATTEMPT_CAP,
    attempt_window_seconds: int = REDEEM_ATTEMPT_WINDOW_SECONDS,
) -> RedeemResult:
    """Redeem ``token`` against ``station`` with a token-bound ``proof`` (INV-1..6).

    ``origin`` is the station's authoritative origin (the same string the
    collaborator signed over). Returns a :class:`RedeemResult`; raises
    ``sqlite3.OperationalError`` only if a short write transaction (the attempt
    reservation or the burn CAS) cannot take the write lock — both run OUTSIDE the
    Sigstore round-trip, and the route degrades the lock error to a retryable 503."""
    # The token must be a non-empty string; anything else can't be a real token and
    # is treated as unknown (cheap, no crypto, no existence oracle beyond "invalid").
    if not isinstance(token, str) or not token:
        return RedeemResult(False, RedeemStatus.UNKNOWN, "unknown or invalid invite token")
    # A real invite token is CSPRNG hex — pure ASCII. A string carrying a lone UTF-16
    # surrogate (e.g. '\ud800') cannot be UTF-8-encoded, so hash_token would raise
    # UnicodeEncodeError; but such a string can never equal a stored hex token either,
    # so it is just another UNKNOWN — the SAME cheap reject, reached before any crypto
    # and leaking no existence oracle. Guarding here (not the route) keeps redeem()'s
    # "never raises over hostile input except a genuine SQLite lock" contract total.
    try:
        token_hash = hash_token(token)
    except UnicodeEncodeError:
        return RedeemResult(False, RedeemStatus.UNKNOWN, "unknown or invalid invite token")

    # (a) CHEAP token-hash lookup OUTSIDE any transaction (INV-2 step a).
    row = station.store.get_invite_by_token_hash(token_hash)
    if row is None:
        return RedeemResult(False, RedeemStatus.UNKNOWN, "unknown or invalid invite token")
    if row.get("revoked_at") is not None:
        return RedeemResult(False, RedeemStatus.REVOKED_INVITE, "this invite was revoked by the operator")

    now = datetime.now(timezone.utc)
    used = row.get("used_at") is not None
    # Per-token flood cap (INV-5) applies ONLY to an UNUSED token. Its purpose is the
    # leaked-but-UNUSED-token expensive-verify flood, where the prize is binding an
    # innocent identity — a token not burned until success can drive repeated
    # verifies. A BURNED token has no binding to steal (it is already bound, and the
    # only consequential outcome left is the bound identity's idempotent OK_ALREADY),
    # so capping its verifies would only let an attacker holding the spent token
    # poison the counter and deny the legitimate author's lost-ack retry (INV-6).
    # The residual crypto-DoS on a burned token is bounded by the nginx per-IP
    # redeem_zone, the same front-line as every other crypto-doing route.
    if not used:
        # Expiry blocks a FIRST redeem only; a burned invite stays idempotently
        # redeemable by its bound identity (INV-6) regardless of expiry.
        expires = _parse(row.get("expires_at"))
        if expires is None or expires <= now:
            return RedeemResult(False, RedeemStatus.EXPIRED, "this invite has expired; ask the operator for a new one")
        # An ADVISORY lock-free pre-check on the already-fetched row sheds a clear
        # flood before taking any lock; the AUTHORITATIVE gate is the atomic
        # conditional UPDATE in reserve_redeem_attempt (a check-then-act gate alone
        # is racy — concurrent attempts all pass a stale read and all reach
        # verify_multi). The counter is read ONLY on this unused-token path, and the
        # token burns on the one success that follows, so a verified success needs
        # no refund — the counter is never consulted again for this token.
        if _attempts_exceeded(row, now, attempt_cap, attempt_window_seconds):
            return RedeemResult(False, RedeemStatus.RATE_LIMITED, "too many failed attempts on this invite; try again later")
        if not station.store.reserve_redeem_attempt(token_hash, attempt_cap, attempt_window_seconds):
            return RedeemResult(False, RedeemStatus.RATE_LIMITED, "too many failed attempts on this invite; try again later")

    # (b) verify_multi OUTSIDE any transaction, holding NO lock (INV-2 step b). The
    # binding is station-authoritative: token_hash is from OUR token, origin/route
    # are OURS — never wire-claimed. The reserve above already committed and released
    # its lock, so no transaction is open across this Sigstore round-trip.
    verified, vreason, identity = _sign.verify_wire_redeem(
        proof, token_hash, origin, route, verifier
    )
    if not verified:
        station.store.log_redeem_failure(token_hash, reason=vreason)
        if vreason in _PROOF_SHAPE_REASONS:
            return RedeemResult(False, RedeemStatus.PROOF_MALFORMED, f"redeem proof rejected: {vreason}")
        return RedeemResult(False, RedeemStatus.PROOF_REJECTED, f"redeem proof rejected: {vreason}")

    issuer = identity["issuer"]
    subject = identity["subject"]

    # INV-6 — idempotent re-redeem. If the token is already burned AND bound to the
    # SAME identity now presenting a valid token-bound proof, a lost-ack retry is a
    # success, not a failure. A burned token presented by a DIFFERENT (validly
    # signed) identity is a real reject (logged for the operator); the used path
    # does not reserve, so neither outcome touches the flood cap.
    if used:
        # The used path never reserves, so the bound identity's idempotent retry
        # always reaches here regardless of the counter (INV-6 holds unconditionally).
        if row.get("bound_issuer") == issuer and row.get("bound_subject") == subject:
            return RedeemResult(True, RedeemStatus.OK_ALREADY, "already redeemed (idempotent)", issuer, subject)
        station.store.log_redeem_failure(token_hash, reason="already redeemed")
        return RedeemResult(False, RedeemStatus.ALREADY_REDEEMED, "this invite has already been redeemed")

    # (c) the SHORT write transaction: CAS burn + bind (INV-2 step c / INV-3).
    cas = station.store.redeem_invite_cas(token_hash, issuer, subject)
    if cas == "redeemed":
        return RedeemResult(True, RedeemStatus.OK_REDEEMED, "redeemed", issuer, subject)
    if cas == "revoked_identity":
        return RedeemResult(
            False,
            RedeemStatus.REVOKED_IDENTITY,
            "this identity was revoked by the operator and cannot self-readmit; contact the operator",
        )
    # race_lost — a concurrent redeem won, or the token became invalid between the
    # cheap check and the CAS. Re-read once for the idempotent-same-identity case so
    # two concurrent redeems by the SAME identity both report success (INV-6).
    fresh = station.store.get_invite_by_token_hash(token_hash)
    if (
        fresh is not None
        and fresh.get("used_at") is not None
        and fresh.get("bound_issuer") == issuer
        and fresh.get("bound_subject") == subject
    ):
        return RedeemResult(True, RedeemStatus.OK_ALREADY, "already redeemed (idempotent)", issuer, subject)
    return RedeemResult(False, RedeemStatus.RACE_LOST, "this invite is no longer valid")


# Machine-status -> HTTP code for the route. Success is 200; the rest map to the
# closest idiomatic code. Token-state conflicts share 409; a malformed proof is
# 400; a crypto rejection is 401; a revoked identity is 403; the flood cap is 429.
HTTP_STATUS = {
    RedeemStatus.OK_REDEEMED: 200,
    RedeemStatus.OK_ALREADY: 200,
    RedeemStatus.UNKNOWN: 409,
    RedeemStatus.EXPIRED: 409,
    RedeemStatus.REVOKED_INVITE: 409,
    RedeemStatus.ALREADY_REDEEMED: 409,
    RedeemStatus.RACE_LOST: 409,
    RedeemStatus.REVOKED_IDENTITY: 403,
    RedeemStatus.PROOF_MALFORMED: 400,
    RedeemStatus.PROOF_REJECTED: 401,
    RedeemStatus.RATE_LIMITED: 429,
}
