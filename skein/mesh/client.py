"""The mesh client core: resolve over HTTP, strict-verify locally, fork-F verdict.

Shared by the ``mesh fetch`` CLI and (later) the client-side MCP wrapper. The
verification here is the strict §4 path run on the CLIENT — it re-derives the
content hash from the ``body`` shown and checks the signature over the
domain-separated preimage. The station's ``asserted.verdict`` is the station's
word; this is the consumer re-deriving it, which is the whole point.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import quote, urlparse

import requests

DEFAULT_INSTANCE = "http://127.0.0.1:9001"

# verify_multi statuses meaning "the verifier could not check", NOT "the signature
# is bad" — mirror of envelope._VERIFIER_UNAVAILABLE. These read as UNVERIFIED, a
# distinct exit, so a transient trust-root problem never reads as forgery.
_VERIFIER_UNAVAILABLE = frozenset({"OFFLINE_NO_TRUSTED_ROOT", "TRUST_ROOT_STALE"})

_LOOPBACK_HOSTS = frozenset({"localhost", "::1"})

# --- fork F exit codes ------------------------------------------------------
EXIT_OK = 0  # resolved + verified, or resolved + unsigned (without --require-signed)
EXIT_NOT_RESOLVED = 2  # no folio at that address, or the instance was unreachable
EXIT_SIGNATURE_INVALID = 3  # signature bad, or the body doesn't match its address
EXIT_UNVERIFIED = 4  # a signature is present but the verifier couldn't be reached
EXIT_REQUIRE_SIGNED = 5  # resolved + unsigned, but --require-signed demanded a signature


@dataclass
class FetchResult:
    """The outcome of resolving + verifying one address against an instance."""

    address: str
    instance: str
    resolved: bool
    state: str  # verified | unsigned | invalid | unverified | not_resolved
    exit_code: int
    reason: Optional[str] = None
    identity: Optional[dict] = None
    envelope: Optional[dict] = None
    markdown: Optional[str] = None
    remote: bool = False
    warning: Optional[str] = None
    # Did the served content's hash bind to the REQUESTED address?  True = the
    # address carried a digest and the content matched it; None = the address had
    # no digest to pin against locally (an alias/legacy id), so the name->hash
    # mapping is the station's word, not verified here.
    pinned: Optional[bool] = None
    # How it pinned: "full" (exact full-digest match) | "prefix" (short-hash
    # prefix bind, weaker) | None (unpinnable).
    pin_kind: Optional[str] = None


def _is_remote(instance: str) -> bool:
    host = (urlparse(instance).hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        return False
    # The whole 127.0.0.0/8 range is loopback, not just 127.0.0.1.
    return not host.startswith("127.")


def _pin_check(
    requested_address: Optional[str], actual_hash: str
) -> Tuple[Optional[bool], Optional[str], Optional[str]]:
    """Bind the requested address to the content's true identity (``actual_hash``).

    Content addressing's one invariant is address == hash(content); this is where
    the client enforces it against the station it does not trust. Returns
    ``(matched, kind, reason)``:

    - ``(True, "full", None)`` — a full-digest address, exact match.
    - ``(True, "prefix", None)`` — a short-digest address, the full hash extends
      it (a prefix bind: it constrains the station but is not full identity).
    - ``(False, None, reason)`` — the address carried a digest and the content did
      NOT match: the station served something other than what was asked for.
    - ``(None, None, reason)`` — the address has no locally-checkable digest (a
      bare alias or migrated legacy id), so the name->hash mapping is the
      station's word; the content self-certifies, it just isn't pinned here.
    """
    if not requested_address:
        return None, None, "no requested address to pin against"
    from skein import address as addr

    try:
        parsed = addr.parse(requested_address)
    except addr.AddressError:
        return None, None, (
            "requested address has no content-hash digest (alias/legacy id) — "
            "the name->hash mapping is the station's word, not verified locally"
        )
    pin = parsed.fragment if parsed.fragment is not None else parsed.folio
    algo, _sep, digest = actual_hash.partition("::")
    if pin.algo != algo:
        return False, None, f"address mismatch: requested {pin.algo}, served {algo}"
    if pin.is_full:
        if pin.digest != digest:
            return False, None, "address mismatch: served content does not hash to the requested address"
        return True, "full", None
    # A short digest pins by prefix: the full hash must extend it.
    if not digest.startswith(pin.digest):
        return False, None, "address mismatch: served hash does not extend the requested short hash"
    return True, "prefix", None


def resolve(instance: str, address: str, *, timeout: float = 10.0) -> Tuple[Optional[dict], Optional[str]]:
    """GET the JSON envelope for ``address`` from ``instance``.

    Returns ``(envelope, error)``: the parsed envelope (a folio OR a station
    error envelope) on a reachable instance, or ``(None, message)`` when the
    instance is unreachable or returns unparseable content — which the caller
    treats as not-resolved, not as a station error envelope.
    """
    url = f"{instance.rstrip('/')}/folio/{quote(address, safe='')}.json"
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.RequestException as e:
        return None, f"instance unreachable: {e}"
    try:
        return resp.json(), None
    except ValueError:
        return None, f"instance returned non-JSON (HTTP {resp.status_code})"


def verify_envelope(env: dict) -> Tuple[str, int, Optional[str], Optional[dict], Optional[str]]:
    """Strict, client-side verification of a resolved envelope's CONTENT (fork F).

    Returns ``(state, exit_code, reason, identity, content_hash)`` where
    ``content_hash`` is the hash RE-DERIVED from the body (the content's true
    identity), or ``None`` when the envelope did not verify. Binding that hash to
    the *requested address* is :func:`fetch`'s job (it holds the request), via
    :func:`_pin_check`.

    The content hash is ALWAYS re-derived locally and is the thing returned — the
    station's ``proof.content_hash`` is only cross-checked for consistency, never
    trusted as the identity. (A signed folio may legally omit ``content_hash``,
    since canon hashes only the body; returning the station's ``claimed`` there
    would let an omitted hash skip the address pin entirely.) The signature, when
    present, binds authorship to that same re-derived body.
    """
    # Hostile-station hardening: the response is untrusted JSON. A non-object
    # envelope, or a folio whose body/proof isn't an object (e.g. a collection's
    # list body returned at a folio address), is malformed — report it, never let
    # it crash the client (a `{**list}` / `.get` on a non-dict would).
    if not isinstance(env, dict):
        return "invalid", EXIT_SIGNATURE_INVALID, "malformed envelope (not an object)", None, None
    if env.get("kind") == "error":
        body = env.get("body")
        reason = body.get("error") if isinstance(body, dict) else None
        return "not_resolved", EXIT_NOT_RESOLVED, reason, None, None

    from .. import canon
    from ..identity import content_hash_for_bytes

    proof = env.get("proof")
    proof = proof if isinstance(proof, dict) else {}
    body = env.get("body")
    if not isinstance(body, dict):
        return "invalid", EXIT_SIGNATURE_INVALID, "malformed envelope (body is not an object)", None, None
    claimed = proof.get("content_hash")
    bundle = proof.get("signature_bundle")
    wire = {**body, "content_hash": claimed}

    # The content's true identity, re-derived from the body. This — not the
    # station's claim — is what gets pinned to the address.
    actual = content_hash_for_bytes(canon.folio_canonical_bytes(wire))

    # Cross-check: if the station claimed a hash, it must match the body it served.
    if claimed is not None and actual != claimed:
        return "invalid", EXIT_SIGNATURE_INVALID, "hash mismatch", None, None

    if bundle:
        from ..sign import verify_wire_folio  # lazy: keep Sigstore off unsigned reads

        wire["signature_bundle"] = json.dumps(bundle)
        verified, reason, identity = verify_wire_folio(wire)
        if verified:
            return "verified", EXIT_OK, "verified", identity, actual
        if reason in _VERIFIER_UNAVAILABLE:
            # Authorship uncheckable, but integrity still binds the body — return
            # the hash so fetch can still pin it to the requested address.
            return "unverified", EXIT_UNVERIFIED, reason, None, actual
        return "invalid", EXIT_SIGNATURE_INVALID, reason, None, None

    return "unsigned", EXIT_OK, None, None, actual


def fetch(
    instance: str,
    address: str,
    *,
    require_signed: bool = False,
    timeout: float = 10.0,
) -> FetchResult:
    """Resolve ``address`` against ``instance`` and strict-verify it (fork F).

    Verification is two checks the station cannot fake: the content is
    re-hashed and (if signed) the signature re-verified locally
    (:func:`verify_envelope`), and that content hash is then bound to the
    REQUESTED address (:func:`_pin_check`) — so a station serving content B under
    a request for address A is caught as an address mismatch, not reported clean.

    ``--require-signed`` turns a resolved-but-unsigned result into a non-zero
    exit. Remote-unsigned content additionally carries a stderr-bound warning (it
    is weak on both trust axes: remote + no authorship proof).
    """
    remote = _is_remote(instance)
    env, err = resolve(instance, address, timeout=timeout)
    if env is None:
        return FetchResult(
            address=address, instance=instance, resolved=False,
            state="not_resolved", exit_code=EXIT_NOT_RESOLVED, reason=err, remote=remote,
        )

    state, exit_code, reason, identity, content_hash = verify_envelope(env)
    pinned: Optional[bool] = None
    pin_kind: Optional[str] = None

    # Bind the verified content to the address the caller asked for. Runs whenever
    # the content itself verified (content_hash is set) — including unverified
    # (authorship uncheckable but integrity-bound), so a substitution is caught
    # even when the signature can't be reached.
    if content_hash is not None:
        matched, kind, pin_reason = _pin_check(address, content_hash)
        if matched is False:
            state, exit_code, reason, identity = "invalid", EXIT_SIGNATURE_INVALID, pin_reason, None
        else:
            pinned = matched  # True (pinned) or None (no digest to pin against)
            pin_kind = kind

    resolved = state != "not_resolved"
    warning = None
    if state == "unsigned":
        if require_signed:
            exit_code = EXIT_REQUIRE_SIGNED
        if remote:
            authority = urlparse(instance).hostname or instance
            warning = (
                f"warning: {address} is UNSIGNED and served by a remote instance "
                f"({authority}) — vouched only by that authority, no authorship proof."
            )

    # Never print an `invalid` (substituted / bad-signature) body to stdout — a
    # consumer reading stdout regardless of exit code must not ingest it. Only the
    # verdict (stderr) and exit code signal the failure.
    showable = state in ("verified", "unsigned", "unverified")
    markdown = _render(env) if showable else None
    return FetchResult(
        address=address, instance=instance, resolved=resolved, state=state,
        exit_code=exit_code, reason=reason, identity=identity, envelope=env,
        markdown=markdown, remote=remote, warning=warning, pinned=pinned, pin_kind=pin_kind,
    )


# --- display-trust path (the browse verbs + MCP) ----------------------------
#
# These fetch what the station RENDERED (agent markdown) and return it as-is —
# the display-trust convenience path (brief-20260603-dirz fork E). They do NOT
# verify; the rendering carries the addresses/bundle links so an agent can
# escalate to `mesh fetch` (resolve + strict verify) for a hard guarantee.
# Verification and federation never route through here.

_MARKDOWN_ACCEPT = "text/markdown"


def _fetch_text(instance: str, path: str, *, params: Optional[dict] = None, timeout: float = 10.0) -> str:
    """GET an agent-markdown rendering of a route; return the text (or an error line)."""
    url = instance.rstrip("/") + path
    try:
        resp = requests.get(url, params=params, headers={"Accept": _MARKDOWN_ACCEPT}, timeout=timeout)
    except requests.RequestException as e:
        return f"error: instance unreachable: {e}"
    return resp.text


def resolve_display(instance: str, address: str, *, timeout: float = 10.0) -> str:
    """A folio's agent-markdown rendering (display-trust; use ``fetch`` to verify)."""
    return _fetch_text(instance, f"/folio/{quote(address, safe='')}", timeout=timeout)


def search_display(instance: str, query: str, *, timeout: float = 10.0) -> str:
    """Search results as agent markdown."""
    return _fetch_text(instance, "/search", params={"q": query}, timeout=timeout)


def list_display(instance: str, slug: str, *, timeout: float = 10.0) -> str:
    """A site's folios as agent markdown."""
    return _fetch_text(instance, f"/site/{quote(slug, safe='')}", timeout=timeout)


def describe_display(instance: str, *, timeout: float = 10.0) -> str:
    """The station's describe document (the well-known root) as agent markdown."""
    return _fetch_text(instance, "/.well-known/skein", timeout=timeout)


def _render(env: dict) -> str:
    """The agent-markdown rendering of a resolved envelope, for display."""
    from .. import render as render_mod

    if env.get("kind") == "folio":
        text, _nonce = render_mod.render_folio_markdown(env)
        return text
    if env.get("kind") == "error":
        return render_mod.render_error_markdown(env)
    text, _nonce = render_mod.render_collection_markdown(env, title=env.get("address", ""))
    return text


def _pin_clause(result: FetchResult) -> str:
    """How the verified content relates to the address the caller asked for."""
    if result.pin_kind == "full":
        return "content hashes to the requested address"
    if result.pin_kind == "prefix":
        return "served hash extends the requested short hash (prefix bind, not full identity)"
    return (
        "could not pin to the requested address locally (alias/legacy id) — "
        "the name->hash mapping is the station's word"
    )


def verdict_line(result: FetchResult) -> str:
    """A one-line human verdict for stderr, built from the LOCAL verification.

    The ``invalid`` state covers both a bad signature and an address mismatch (the
    station served content other than what was requested); the ``reason`` says
    which. ``verified``/``unsigned`` carry whether the content pinned to the
    requested address — an unpinned result is stated honestly, never as clean.
    """
    if result.state == "verified":
        subject = (result.identity or {}).get("subject") or "verified"
        issuer = (result.identity or {}).get("issuer")
        who = f"{subject} ({issuer})" if issuer else subject
        return f"VERIFIED — signed by {who}; {_pin_clause(result)}"
    if result.state == "unsigned":
        return f"UNSIGNED — no authorship proof; {_pin_clause(result)}"
    if result.state == "unverified":
        return (
            f"UNVERIFIED — signature present but verifier unavailable ({result.reason}); "
            f"{_pin_clause(result)}"
        )
    if result.state == "invalid":
        return f"INVALID — {result.reason}"
    return f"NOT RESOLVED — {result.reason or 'no folio at that address'}"
