"""Instance ingress: the write surface that receives published folios.

PROTOTYPE — unsigned, unauthenticated. This is the counterpart to
``skein.publish``: a client POSTs a publish batch here, and the instance
stores the folios and threads, then serves them through its existing read-only
web surface. It is deliberately a SEPARATE surface from the read web app (which
stays read-only) and from any future ``/fed/v0`` peer routes (which are
instance<->instance pull, not client push) — per finding-20260521-kwtb.

What the prototype does and does not do
---------------------------------------
- DOES verify integrity without crypto: every folio and thread is re-hashed from
  its canonical fields and rejected if the claimed hash does not match. A body
  altered in transit cannot keep its address. Storage is idempotent via the
  content hash, so re-publishing is a no-op that reports ``existing``.
- DOES NOT authenticate the writer or verify authorship. There is no signature,
  no account binding, no allowlist. That is exactly the gap signing closes at
  this boundary (brief-20260522-q8k0); the route is structured so that check
  slots in ahead of storage without reshaping the response.

Run it on its own port, against the same data dir the read app serves:

    skein --data-dir ./instance/.skein ingress --port 9101
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .station import Station
from . import canon, wire
from . import sign as _sign
from . import redeem as _redeem
from .authorization import default_bindings
from .identity import content_hash_for_bytes
from .store import bundle_hash_for, sqlite_error_is_lock

logger = logging.getLogger(__name__)

# The bare manifest-failure reasons the verifier returns are NOT wrapped; only a
# genuine VerifyStatus crypto failure wears the 'manifest signature ' prefix
# (fell-r1 FIX 1, the reject-reason propagation rule). These are the absence +
# wire-integrity bare reasons.
_BARE_MANIFEST_REASONS = frozenset(
    {"no manifest", "manifest malformed", "wrong kind", "unknown profile"}
)


def _constituent_manifest_reject_reason(m_reason: str) -> str:
    """Map a verifier manifest reason to the constituent reject reason (3 buckets).

    ABSENCE / WIRE-INTEGRITY propagate the BARE reason; the CRYPTO FLOOR (a genuine
    VerifyStatus value) is the ONLY bucket wrapped 'manifest signature <status>'."""
    if m_reason in _BARE_MANIFEST_REASONS:
        return m_reason
    return f"manifest signature {m_reason}"

DEFAULT_PORT = 9101  # read web app is 9001; ingress is a distinct write surface
ENV_DATA_DIR = "SKEIN_DATA_DIR"

# The station's canonical origin — a SIGNED field of the redeem challenge (INV-1).
# The collaborator signs over this exact string; the station reconstructs the
# challenge with its OWN configured value, so a proof minted for a different origin
# fails closed. Set it in the deploy env (e.g. https://interskein.com). If unset,
# the redeem route refuses to operate (it cannot verify a token-bound proof without
# an authoritative origin) — /publish is unaffected.
ENV_ORIGIN = "SKEIN_ORIGIN"

# Body cap for the redeem route — SMALLER than the publish cap: a redeem body is a
# single {token, proof} object (a token string + one Sigstore bundle), never a
# multi-folio batch. 64 KiB is generous for one bundle yet bounds parse/memory work
# hard. Its own nginx location carries a matching client_max_body_size + a tighter
# per-IP limit_req zone (deploy/nginx).
REDEEM_MAX_BYTES = 64 * 1024

# The publish + redeem routes both degrade a transient write-lock to a retryable
# 503; the discrimination lives in store.sqlite_error_is_lock — one source of truth.
_sqlite_error_is_lock = sqlite_error_is_lock

# Absolute byte cap on a publish request body, enforced BEFORE the body is fully
# buffered or JSON-parsed, so a hostile client cannot force unbounded parse/memory
# work ahead of any verification. Matches the public route's nginx
# client_max_body_size (1 MiB); the app cap is defense-in-depth for any path that
# reaches the ingress without the fronting proxy (e.g. a loopback SSH tunnel). If
# a legitimate publish ever needs more, bump BOTH this and the nginx cap together.
MAX_BATCH_BYTES = 1024 * 1024


def _get_data_dir() -> Any:
    return os.environ.get(ENV_DATA_DIR)


class BatchShapeError(ValueError):
    """A publish batch is structurally malformed (bad container types)."""


def _validate_shape(batch: Dict[str, Any]) -> None:
    """Reject a structurally-malformed batch before any storage work.

    Only the container shapes are checked here; per-item integrity (hash match,
    closed endpoints) and the manifest's semantic consistency are handled in the
    loop / the verifier. Without this, a body like ``{"folios": "x"}`` iterates a
    string and 500s instead of returning 400.

    The ``manifest_signature`` is DELIBERATELY not shape-checked here. A present-
    but-non-dict value (``None``, a string, a list, an int) is NOT a batch 400: it
    flows to ``verify_wire_manifest`` (TOTAL over hostile input) and becomes the
    per-constituent WIRE-INTEGRITY verdict 'manifest malformed' (VM11/VM12). Only a
    WHOLLY ABSENT key is the ABSENCE bucket ('no manifest'). The ``mctx`` dict-
    indexing downstream is gated on ``m_verified`` (False for any non-dict), so a
    non-dict manifest never reaches dict-indexing and never 500s.
    """
    for key in ("folios", "threads"):
        value = batch.get(key, [])
        if not isinstance(value, list) or not all(isinstance(i, dict) for i in value):
            raise BatchShapeError(f"{key!r} must be a list of objects")
    if not isinstance(batch.get("site_slugs", {}), dict):
        raise BatchShapeError("'site_slugs' must be an object")


def ingest(
    station: Station,
    batch: Dict[str, Any],
    verifier: "_sign.Verifier" = _sign.default_verifier,
    require_signed: bool = False,
    bindings: Any = None,
) -> Dict[str, Any]:
    """Store a publish batch into ``station`` under the unified manifest gate.

    Pure over the station so it is unit-testable without a server. Folios land
    before threads so a ``within`` edge's site folio (and its slug) already exists
    when membership is recorded.

    The manifest verdict is computed ONCE before the loops: the batch's single
    ``manifest_signature`` is verified and (under ``require_signed``) its signer is
    gated through the bindings exactly ONCE. Then every constituent — folio OR
    thread — is judged identically by MEMBERSHIP (its content hash is a leaf under
    the verified bound manifest).

    - ``require_signed=False`` (OFF) is manifest-blind AND binding-blind: a
      constituent admits on integrity + closed-graph alone, byte-identical to the
      pre-mesh posture. No bindings are consulted, no attribution is written.
    - ``require_signed=True`` (ON): a constituent admits iff it is a leaf under a
      manifest that verifies and whose signer is bound + non-revoked. On admit
      (whether the body is freshly stored or already 'existing'), the manifest +
      constituent attribution are written (INSERT OR IGNORE).

    ``bindings`` defaults to a BindingStore over ``station.store``."""
    _validate_shape(batch)
    if bindings is None:
        bindings = default_bindings(station.store)

    # --- compute the manifest verdict ONCE (before the per-item loops) --------
    has_manifest = "manifest_signature" in batch  # KEY-PRESENCE, not bool()
    m_verified, m_reason, m_identity = (False, "no manifest", None)
    # OFF is manifest-blind (RS1/RS9): never call verify_wire_manifest — under OFF
    # a real Sigstore verification (network/TUF, and an abort if the verifier
    # raises) is NOT byte-identical to the pre-mesh posture. The verdict is only
    # computed under ON, where the loops consult manifest_decision/attribution; the
    # verify call fires exactly ONCE here. We keep has_manifest (key-presence) on
    # the ON path so a present-but-non-dict value still reaches the verifier as the
    # WIRE-INTEGRITY 'manifest malformed' verdict (VM11), distinct from 'no manifest'.
    if has_manifest and require_signed:
        m_verified, m_reason, m_identity = _sign.verify_wire_manifest(
            batch["manifest_signature"], verifier
        )

    # Bind the verified manifest signer ONCE (only under ON, only when verified) —
    # a single get_binding for the whole batch (RS8). NEVER memoized from stored
    # manifests/attribution rows (the re-gating pin, C40): a revocation between
    # publishes flips the verdict on the next ingest.
    m_bound = False
    m_bind_reason: Optional[str] = None
    if has_manifest and m_verified and require_signed:
        binding = bindings.get_binding(m_identity["issuer"], m_identity["subject"])
        if binding is None:
            m_bind_reason = "unbound signer"
        elif binding.revoked_at is not None:
            m_bind_reason = "revoked binding"
        else:
            m_bound = True

    # Manifest context for the attribution writes (only meaningful when verified).
    mctx: Optional[Dict[str, Any]] = None
    if has_manifest and m_verified:
        ms = batch["manifest_signature"]
        descriptor = ms["descriptor"]
        root = descriptor["root"]
        leaf_count = descriptor["leaf_count"]
        mctx = {
            "root": root,
            "leaf_count": leaf_count,
            "leaf_list": ms["leaf_list"],
            "descriptor_json": json.dumps(descriptor, sort_keys=True),
            "leaf_list_json": json.dumps(ms["leaf_list"]),
            "bundle_json": ms["signature_bundle"],
            "manifest_hash": content_hash_for_bytes(
                canon.manifest_descriptor_canonical_bytes(root, leaf_count)
            ),
            "issuer": m_identity["issuer"],
            "subject": m_identity["subject"],
        }

    def manifest_decision(constituent_hash: str) -> Tuple[bool, Optional[str]]:
        """Under ON, whether this constituent is admitted, and the reject reason."""
        if not m_verified:
            return False, _constituent_manifest_reject_reason(m_reason)
        if not m_bound:
            return False, m_bind_reason
        if not canon.manifest_membership(mctx["leaf_list"], mctx["root"], constituent_hash):
            return False, "not in manifest"
        return True, None

    def write_attribution(constituent_hash: str, kind: str) -> None:
        """Record manifest + attribution (manifest row FIRST, ST8), INSERT OR
        IGNORE — runs on BOTH the accept AND the 'existing' path (RS7/RS20). Also
        populates the manifest's VERIFIED signature verdict in verify_cache (the
        ingress is the cache WRITER, VC6); the read path elides Sigstore on a hit."""
        station.store.add_manifest(
            mctx["root"], mctx["manifest_hash"], mctx["descriptor_json"],
            mctx["leaf_list_json"], mctx["bundle_json"], mctx["issuer"],
            mctx["subject"], mctx["leaf_count"],
        )
        station.store.add_constituent_attribution(
            constituent_hash, kind, mctx["root"], mctx["issuer"], mctx["subject"]
        )
        station.store.verify_cache_put(
            mctx["manifest_hash"], bundle_hash_for(mctx["bundle_json"]),
            "VERIFIED", mctx["issuer"], mctx["subject"],
        )

    accepted: List[str] = []
    existing: List[str] = []
    rejected: List[Dict[str, str]] = []

    site_slugs: Dict[str, str] = batch.get("site_slugs") or {}

    with station.store.transaction():
        for wf in batch.get("folios", []):
            claimed = wf.get("content_hash")
            reason = wire.folio_reject_reason(wf)
            if reason:
                rejected.append({"content_hash": claimed, "reason": reason})
                continue
            if require_signed:
                admit, mreason = manifest_decision(claimed)
                if not admit:
                    rejected.append({"content_hash": claimed, "reason": mreason})
                    continue
            try:
                with station.store.savepoint():
                    if station.store.get_folio(claimed) is not None:
                        existing.append(claimed)
                    else:
                        station.store.create_folio(wf)
                        accepted.append(claimed)
                    if require_signed:
                        write_attribution(claimed, "folio")
                    # A site folio's slug is gated TRANSITIVELY: this set_slug is
                    # only reached for an admitted site folio (Q2/RS18).
                    if wf.get("type") == "site" and claimed in site_slugs:
                        station.store.set_slug(site_slugs[claimed], claimed)
            except Exception as exc:  # noqa: BLE001 — one bad item never 500s the batch
                logger.debug("folio %s rolled back: %s", claimed, exc)
                if claimed in accepted:
                    accepted.remove(claimed)
                if claimed in existing:
                    existing.remove(claimed)
                rejected.append({"content_hash": claimed, "reason": "invalid fields"})

        thread_accepted: List[str] = []
        thread_existing: List[str] = []
        thread_rejected: List[Dict[str, str]] = []

        def present(endpoint: Any) -> bool:
            # On the instance iff it exists in the store — either landed in this
            # batch (visible uncommitted on this connection) or stored by an
            # earlier publish. The latter is what lets convergent edges land.
            return endpoint is not None and station.store.get_folio(endpoint) is not None

        for wt in batch.get("threads", []):
            claimed = wt.get("thread_hash")
            reason = wire.thread_reject_reason(wt)
            if reason:
                thread_rejected.append({"thread_hash": claimed, "reason": reason})
                continue
            # Under ON, a GLOBAL manifest failure (unverified, or a verified signer
            # that is unbound/revoked) rejects EVERY constituent identically with the
            # manifest/bind reason, BEFORE the per-thread present() check — so a
            # broken manifest makes folios AND threads report the same reason (the
            # unified-table principle; VM11). manifest_decision returns exactly that
            # reason when not verified / not bound, so we reuse it here.
            if require_signed and (not m_verified or not m_bound):
                _, mreason = manifest_decision(claimed)
                thread_rejected.append({"thread_hash": claimed, "reason": mreason})
                continue
            # The manifest verifies AND binds: present() can still legitimately yield
            # 'dangling endpoint' for a member-thread whose endpoint folio did not
            # land. Refuse edges whose endpoints are not both on the instance — the
            # graph stays closed (the client already filters; defense in depth).
            if not present(wt.get("from_id")) or not present(wt.get("to_id")):
                thread_rejected.append({"thread_hash": claimed, "reason": "dangling endpoint"})
                continue
            if require_signed:
                # Manifest verified + bound, so this can only fail 'not in manifest'.
                admit, mreason = manifest_decision(claimed)
                if not admit:
                    thread_rejected.append({"thread_hash": claimed, "reason": mreason})
                    continue
            try:
                with station.store.savepoint():
                    already = station.store.get_thread(claimed) is not None
                    station.store.save_thread(
                        from_id=wt.get("from_id"),
                        to_id=wt.get("to_id"),
                        type=wt.get("type"),
                        weaver=wt.get("weaver"),
                        created_at=wt.get("created_at"),
                        content=wt.get("content"),
                    )
                    if require_signed:
                        write_attribution(claimed, "thread")
                    (thread_existing if already else thread_accepted).append(claimed)
            except Exception as exc:  # noqa: BLE001 — one bad item never 500s the batch
                logger.debug("thread %s rolled back: %s", claimed, exc)
                for lst in (thread_accepted, thread_existing):
                    if claimed in lst:
                        lst.remove(claimed)
                thread_rejected.append({"thread_hash": claimed, "reason": "invalid fields"})

    return {
        "protocol": wire.PROTOCOL,
        "accepted": accepted,
        "existing": existing,
        "rejected": rejected,
        "threads": {
            "accepted": thread_accepted,
            "existing": thread_existing,
            "rejected": thread_rejected,
        },
    }


def backfill_verify_cache(station: Station, verifier: "_sign.Verifier" = None) -> int:
    """Populate verify_cache with the SIGNATURE verdict of every stored manifest.

    The maintenance verb (run when the ingress is quiesced): over a corpus of
    signed manifests it writes one stable verdict row per manifest, recomputing the
    Sigstore verdict via verify_wire_manifest. Idempotent — a second run re-writes
    the same rows (VC9). Returns the number of manifests processed."""
    if verifier is None:
        verifier = _sign.default_verifier
    count = 0
    for m in station.store.all_manifests():
        manifest_signature = {
            "descriptor": json.loads(m["descriptor_json"]),
            "leaf_list": json.loads(m["leaf_list_json"]),
            "signature_bundle": m["bundle_json"],
        }
        verified, reason, identity = _sign.verify_wire_manifest(manifest_signature, verifier)
        status = "VERIFIED" if verified else reason
        iss = (identity or {}).get("issuer") if identity else m["issuer"]
        sub = (identity or {}).get("subject") if identity else m["subject"]
        station.store.verify_cache_put(
            m["manifest_hash"], bundle_hash_for(m["bundle_json"]), status, iss, sub
        )
        count += 1
    return count


ENV_REQUIRE_SIGNED = "SKEIN_REQUIRE_SIGNED"


def _require_signed() -> bool:
    """Whether this instance rejects unsigned publishes (off unless env opts in).

    The signature gate is a per-instance posture, not a hardcode: a public
    instance can demand signed content by setting the env, while the pre-mesh /
    local default stays open. (Full enforcement is the auth phase — this just
    keeps the knob reachable instead of dead.)
    """
    return os.environ.get(ENV_REQUIRE_SIGNED, "").strip().lower() in ("1", "true", "yes")


class OperatorInvariantError(RuntimeError):
    """The ingress startup invariant: under require_signed the active-operator count
    MUST be exactly 1 (D13/D20). Refuses boot LOUD rather than running misconfigured."""


def create_app() -> FastAPI:
    require_signed = _require_signed()
    # STARTUP INVARIANT (the ingress only — the read app is EXEMPT, D17): under
    # require_signed the active operator count must be EXACTLY 1. Count 0 or >1
    # refuses boot. The operator is read from the account_bindings sidecar, the
    # single source of truth (D16), never a stationfile field.
    if require_signed:
        station = Station(_get_data_dir(), check_same_thread=False)
        try:
            n_ops = station.store.count_active_operators()
        finally:
            station.close()
        if n_ops != 1:
            if n_ops == 0:
                raise OperatorInvariantError(
                    "require_signed is on but no active operator exists; "
                    "run 'skein account init-operator' before starting the ingress"
                )
            raise OperatorInvariantError(
                f"require_signed is on but {n_ops} active operators exist; the "
                "single-active-operator invariant is violated — resolve with "
                "'skein account rotate-operator' / 'revoke' before starting"
            )
        logger.info("ingress starting with require_signed: 1 active operator present")
    else:
        logger.warning(
            "ingress starting with require_signed OFF — unsigned content is accepted"
        )

    # The station's authoritative origin for the redeem token-binding (INV-1). Read
    # ONCE at app creation. CANONICALIZED with the same normalizer the client uses
    # on its --to value (publish.canonical_instance: lowercase scheme+host, drop
    # default ports and trailing slash) so the station reconstructs the EXACT string
    # the collaborator signed over. Without this a trailing slash / uppercase scheme
    # / explicit :443 in SKEIN_ORIGIN would diverge from the client's
    # canonicalized origin and SIGNATURE_MISMATCH every redeem (fail-closed, but an
    # availability footgun). If unset, the redeem route refuses to operate; /publish
    # is unaffected.
    from .publish import canonical_instance as _canonical_instance

    _raw_origin = os.environ.get(ENV_ORIGIN)
    redeem_origin = _canonical_instance(_raw_origin) if _raw_origin else None
    if redeem_origin:
        logger.info("ingress redeem origin: %s", redeem_origin)
    else:
        logger.warning(
            "ingress starting without %s — /invite/redeem will refuse to operate "
            "until the station origin is configured", ENV_ORIGIN
        )

    app = FastAPI(
        title="SKEIN (next) — ingress",
        description="client->instance publish ingress",
        version="0.0.1",
    )

    @app.post("/publish/v0/folios")
    async def publish_folios(request: Request) -> JSONResponse:
        # Bound the body by BYTES before buffering / parsing (content-length fast-
        # path + a streaming guard), so a hostile client cannot force unbounded
        # parse/memory work before any verification runs. Over the cap is rejected
        # WHOLE (413), never truncated. Mirrors the read app's resolve batch cap and
        # is defense-in-depth atop the fronting proxy's client_max_body_size.
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_BATCH_BYTES:
                    return JSONResponse(status_code=413, content={"error": "request body too large"})
            except ValueError:
                pass
        body = bytearray()
        try:
            async for chunk in request.stream():
                body += chunk
                if len(body) > MAX_BATCH_BYTES:
                    return JSONResponse(status_code=413, content={"error": "request body too large"})
        except ClientDisconnect:
            # The client went away mid-body (a slow-loris that bails, a flaky
            # network, or a deliberate probe). A disconnect is normal and fully
            # adversary-controllable, so it must be handled QUIETLY — not raised
            # as an ASGI application error. Left uncaught it logs a full traceback
            # per abandoned request, which at flood rate is a cheap log-
            # amplification DoS (disk fill + real-error burial) and breaks this
            # module's 'never 500s over hostile input' contract. The socket is
            # gone, so this response is discarded; we just stop cleanly.
            return JSONResponse(status_code=400, content={"error": "client disconnected"})

        try:
            batch = json.loads(body) if body else None
        except (ValueError, RecursionError):
            # ValueError covers malformed JSON and the UnicodeDecodeError/int-string
            # limit subclasses. RecursionError is the OTHER thing json.loads can raise
            # on hostile input: deeply-nested arrays/objects ([[[... ) blow Python's
            # recursion limit. It is NOT a ValueError (it's a RuntimeError), so left
            # uncaught it escapes to a 500 + full ASGI traceback per request — a cheap
            # floodable log-amplification DoS (the 1 MiB body cap bounds BYTES, not
            # nesting depth: '[' is one byte per level, so ~hundreds of thousands of
            # levels fit under the cap). Reject it as the malformed JSON it is.
            return JSONResponse(status_code=400, content={"error": "request body is not valid JSON"})
        if not isinstance(batch, dict):
            return JSONResponse(status_code=400, content={"error": "request body must be a JSON object"})
        if batch.get("protocol") != wire.PROTOCOL:
            return JSONResponse(
                status_code=400,
                content={"error": f"unknown protocol {batch.get('protocol')!r}; "
                                  f"this instance speaks {wire.PROTOCOL!r}"},
            )

        # ingest does blocking SQLite work; the route is async (to stream-bound the
        # body), so run the open->ingest->close off the event loop the way the sync
        # routes get the threadpool — a slow write never stalls body reads on other
        # requests. The station (own SQLite connection) is created and closed inside
        # the worker thread so it never crosses threads.
        def _do_ingest() -> Any:
            station = Station(_get_data_dir(), check_same_thread=False)
            try:
                return ingest(station, batch, require_signed=require_signed)
            finally:
                station.close()

        try:
            ack = await run_in_threadpool(_do_ingest)
        except BatchShapeError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        except sqlite3.OperationalError as e:
            # Write-lock contention beyond busy_timeout: BEGIN IMMEDIATE could not
            # take the lock in time and raised on ENTRY to transaction(), before the
            # per-item handlers, so it surfaces here. Degrade GRACEFULLY — a 503 the
            # client can retry — instead of letting it propagate to an uncaught 500
            # + full ASGI traceback per request (the log-amplification DoS this same
            # change set hardens against). A genuine (non-lock) OperationalError is a
            # real fault: re-raise it so it is not masked as transient. The lock
            # discrimination (numeric result-code, extended-code masked to the
            # primary byte, message-text fallback) is shared with the redeem route.
            if not _sqlite_error_is_lock(e):
                raise
            logger.warning("ingress write-lock contention; returning 503: %s", e)
            return JSONResponse(
                status_code=503,
                content={"error": "instance busy, retry shortly"},
                headers={"Retry-After": "1"},
            )
        return JSONResponse(status_code=200, content=ack)

    @app.post(_sign.REDEEM_ROUTE)
    async def invite_redeem(request: Request) -> JSONResponse:
        # A SECOND hand-written public Sigstore-doing route. It REPLICATES the
        # /publish hardening shell line-for-line — it does NOT inherit it (INV-5):
        # a SMALLER body cap (the body is one {token, proof}), content-length
        # fast-path + streaming guard, quiet ClientDisconnect, JSON-parse (incl
        # RecursionError) -> 400, blocking work off the event loop with the Station
        # opened+closed inside the worker, and SQLite-lock -> retryable 503.
        if not redeem_origin:
            # Misconfiguration, not hostile input: without an authoritative origin a
            # token-bound proof cannot be reconstructed, so refuse rather than verify
            # against a wrong/empty origin. 503 (operator-fixable), not a 500.
            return JSONResponse(
                status_code=503,
                content={"error": "redeem is not configured on this instance"},
            )

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > REDEEM_MAX_BYTES:
                    return JSONResponse(status_code=413, content={"error": "request body too large"})
            except ValueError:
                pass
        body = bytearray()
        try:
            async for chunk in request.stream():
                body += chunk
                if len(body) > REDEEM_MAX_BYTES:
                    return JSONResponse(status_code=413, content={"error": "request body too large"})
        except ClientDisconnect:
            # Adversary-controllable disconnect handled QUIETLY (no ASGI traceback /
            # log-amplification), exactly as /publish does.
            return JSONResponse(status_code=400, content={"error": "client disconnected"})

        try:
            payload = json.loads(body) if body else None
        except (ValueError, RecursionError):
            # RecursionError (deeply-nested JSON) is a RuntimeError, not a ValueError;
            # catching it here is the same just-merged /publish fix — left uncaught it
            # is a floodable 500 + traceback per request.
            return JSONResponse(status_code=400, content={"error": "request body is not valid JSON"})
        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content={"error": "request body must be a JSON object"})
        token = payload.get("token")
        proof = payload.get("proof")
        if not isinstance(token, str) or not token:
            return JSONResponse(status_code=400, content={"error": "missing or invalid 'token'"})

        # The redeem orchestration does the cheap checks, the crypto (OUTSIDE any
        # lock), and the short CAS burn+bind. Run it off the event loop with the
        # Station opened + closed inside the worker thread so its SQLite connection
        # never crosses threads (the /publish threadpool discipline).
        def _do_redeem() -> "_redeem.RedeemResult":
            station = Station(_get_data_dir(), check_same_thread=False)
            try:
                return _redeem.redeem(station, token, proof, redeem_origin)
            finally:
                station.close()

        try:
            result = await run_in_threadpool(_do_redeem)
        except sqlite3.OperationalError as e:
            # The burn transaction (BEGIN IMMEDIATE in redeem_invite_cas) could not
            # take the write lock in time. Degrade to a retryable 503; a genuine
            # (non-lock) fault re-raises. verify_multi runs OUTSIDE the lock, so a
            # slow Sigstore round-trip never produces this (INV-2).
            if not _sqlite_error_is_lock(e):
                raise
            logger.warning("redeem write-lock contention; returning 503: %s", e)
            return JSONResponse(
                status_code=503,
                content={"error": "instance busy, retry shortly"},
                headers={"Retry-After": "1"},
            )

        http_status = _redeem.HTTP_STATUS.get(result.status, 400)
        content: Dict[str, Any] = {"ok": result.ok, "status": result.status}
        if result.ok:
            content["issuer"] = result.issuer
            content["subject"] = result.subject
        else:
            content["error"] = result.reason
        return JSONResponse(status_code=http_status, content=content)

    return app


def run_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    app = create_app()
    logger.info("Starting skein ingress on http://%s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
