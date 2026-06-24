"""The unified wire envelope (brief-20260603-ujwx rev 2).

One frame, a typed ``body``, built straight from the native content-hash store —
NOT the legacy ``ContentHashAdapter``/``FolioView``/``cross_refs`` path, which
reshapes the store into the old ``Folio`` model and loses thread direction. The
machine surface serves the native, verifiable model; HTML is one downstream
rendering of it (slice 3).

The frame (§2)::

    {
      "schema":    "skein.envelope/v1",
      "address":   "<resolved address>",
      "kind":      "folio | site | search | catalog | error",
      "stability": "stable | derived",
      "as_of":     "<iso8601>",          # required for derived; null for stable
      "body":      <kind-specific>,
      "proof":     {profile, content_hash, signature_bundle} | null,
      "asserted":  {...station claims...},
      "links":     {...control-frame breadcrumbs...},
      "next":      "<address> | null"
    }

Two trust tiers live in one object and must stay visibly separate (§1, §7):
``proof`` is the verifiable spine (content_hash binds ``body``; a signature, when
present, binds authorship); everything in ``asserted`` is the station's *word*
(status, site, graph edges, and its own ``verdict`` line) and is never to be
trusted without the consumer re-deriving it. The control frame
(``schema``/``address``/``kind``/``stability``/``as_of``/``links``/``next``) is
station-rendered structure — derivable from ``body`` or query-time — so it is
unsigned by design (§4).

Phase 1 builds against the existing, mostly-unsigned corpus at the ``integrity``
proof level: ``content_hash`` binds the body, ``signature_bundle`` is ``null``
for unsigned folios. The two live signed docs carry their bundle and the
station's display verdict; the strict, domain-separated verification path and
re-signing under the profile are slice 2.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

SCHEMA = "skein.envelope/v1"

# The signed-preimage profile (§3): names the trust domain, object kind, and
# canonicalization version in one token. At integrity level it still names the
# canonicalization that produced ``content_hash``; at authorship level it is the
# string bound into the signature (slice 2).
CANON_PROFILE = "skein.folio.canon/v1"

# The five canonical folio fields, in nothing-but-these order (canon sorts keys;
# this is for readers and for slicing a folio row down to its body).
BODY_FIELDS = ("type", "title", "content", "created_at", "created_by")

# Thread edges that are station structure, not cross-references: membership and
# status. Excluded from the folio graph, same rule the adapter's cross_refs uses.
_STRUCTURAL_THREADS = frozenset({"within", "status"})

# Edit-lineage edges. These are the four ``relation_type`` values the data model
# (brief-20260511-8qj1 rev 4) defines for the child->parent edit edge — the typed
# subset of threads that expresses versioning, not generic cross-reference. In
# this thread-native store lineage IS these typed threads (there is no ``parent``
# column; identity is the content hash and every relationship is a thread), so the
# envelope derives the lineage block from them. The shape ``asserted.lineage``
# exposes is source-agnostic: if a real ``parent`` field ever lands it can feed the
# same block additively, with no wire change. An edge of one of these types is
# surfaced ONLY in ``lineage`` — never also in the generic ``threads_out/in`` — the
# same partition rule that already keeps ``within``/``status`` out of the graph.
#
# Direction (rev 4): the edge runs child -> parent (``from_id`` = the newer child,
# ``to_id`` = the older parent), so for a folio F an OUTGOING lineage edge points at
# F's parent and an INCOMING one is a child of F. ``supersedes`` is the linear edit
# chain ("the child replaces the parent"); an incoming ``supersedes`` is therefore
# the fork-hatnote "a newer version exists" signal.
_LINEAGE_THREADS = frozenset({"supersedes", "forks", "responds_to", "imports_legacy"})

# Bounds on the transitive descendant walk, so a pathological (or cyclic) lineage
# can never turn one folio read into an unbounded crawl. The walk is breadth-first
# with a visited set; it stops at either bound and the caller treats the result as
# "at least these", never "exactly these".
_DESCENDANTS_MAX = 256
_DESCENDANTS_MAX_DEPTH = 64

# A folio is stable (full content hash, immutable); a collection or error is a
# query result, derived. ``validate_envelope`` enforces this mapping.
_STABLE_KINDS = frozenset({"folio", "thread"})
_DERIVED_KINDS = frozenset({"site", "search", "catalog", "error"})

# verify_multi statuses meaning "the verifier could not check", NOT "the
# signature is bad" — these must read as UNVERIFIED, never SIGNATURE INVALID, so
# a transient trust-root problem never slanders a legitimately signed folio.
_VERIFIER_UNAVAILABLE = frozenset({"OFFLINE_NO_TRUSTED_ROOT", "TRUST_ROOT_STALE"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_envelope(env: Mapping[str, Any]) -> Dict[str, Any]:
    """Enforce the kind->stability->proof invariants; return the dict unchanged.

    A malformed envelope (wrong stability for its kind, a stable envelope with no
    proof, a derived one carrying proof or missing ``as_of``) is a construction
    bug, not bad input — so this raises ``ValueError`` rather than producing a
    quietly-wrong frame.
    """
    kind = env.get("kind")
    stability = env.get("stability")
    if kind in _STABLE_KINDS:
        expected = "stable"
    elif kind in _DERIVED_KINDS:
        expected = "derived"
    else:
        raise ValueError(f"unknown envelope kind: {kind!r}")
    if stability != expected:
        raise ValueError(f"kind {kind!r} requires stability {expected!r}, got {stability!r}")

    if stability == "stable":
        if env.get("proof") is None:
            raise ValueError(f"stable {kind!r} envelope must carry a proof")
    else:
        if env.get("proof") is not None:
            raise ValueError(f"derived {kind!r} envelope must have proof: null")
        if not env.get("as_of"):
            raise ValueError(f"derived {kind!r} envelope must carry as_of")
    return dict(env)


# --- folio ------------------------------------------------------------------


def folio_verdict(
    store,
    content_hash: str,
    row: Mapping[str, Any],
    proof: Optional[Mapping[str, Any]] = None,
) -> tuple[str, Optional[Dict[str, Optional[str]]]]:
    """The station's provenance verdict line for a folio, and the signer identity.

    This is an ``asserted`` claim — the station's word, re-derivable by a careful
    consumer. A folio's verdict derives from its COVERING MANIFEST under the
    FOUR-STEP boundary verdict (81fm §C; fell-r1 FIX 2). A constituent is
    'verified/SIGNED' iff ALL FOUR hold:

    1. INTEGRITY — the body re-hashes to its content_hash (stored identity).
    2. MEMBERSHIP — the leaf is under the covering manifest's signed root (live
       recompute, VM8).
    3. SIGNATURE — the manifest signature verifies (Sigstore). This is the ONLY
       step the verify_cache elides; on a miss it is computed in-process.
    4. BINDING — the manifest signer is CURRENTLY bound + non-revoked. This is a
       LIVE check, recomputed from account_bindings on EVERY read, NEVER cached
       and never memoized from the stored manifest/attribution rows. A revocation
       after ingest flips the read verdict with zero cache invalidation (VC15).

    A folio with no covering manifest reads UNSIGNED. A currently unbound/revoked
    signer reads NOT-VERIFIED ('unbound signer'/'revoked binding') — NOT SIGNED
    with a display color (the retired rev-6 display-only judgment). ``proof`` may
    be passed by a caller that already resolved it (the envelope builder)."""
    import json as _json

    from . import canon
    from .identity import compute_folio_hash
    from .sign import verify_wire_manifest
    from .store import bundle_hash_for

    # Step 1 — INTEGRITY. Re-hash the stored row from the SAME canonical path
    # create_folio used (identity.compute_folio_hash over the five canonical
    # fields) and refuse to read SIGNED if the body no longer hashes to its
    # content_hash key. A tampered body must never ride a warm verify_cache to a
    # SIGNED verdict (81fm §C step 1, VC12). An UNTAMPERED row re-hashes to its
    # content_hash and proceeds exactly as before.
    if compute_folio_hash(row) != content_hash:
        return ("NOT VERIFIED — integrity", None)

    if proof is None:
        proof = store.get_constituent_proof(content_hash)
    if proof is None:
        return ("UNSIGNED — operator-vouched, not cryptographically signed", None)
    if proof.get("proof_missing"):
        # attribution exists but the manifest parent is gone (corrupted / mid-
        # migration); degrade gracefully rather than crash (ST5).
        return ("NOT VERIFIED — proof missing", None)

    root = proof["root"]
    leaf_list = _json.loads(proof["leaf_list_json"])
    issuer, subject = proof["issuer"], proof["subject"]

    # Step 3 — SIGNATURE (the cache-elided expensive Sigstore step). A warm cache
    # returns the stored verdict WITHOUT invoking Sigstore (VC8); a cold/absent
    # cache computes in-process (VC7/VC10/VC14) and writes nothing (read app ro).
    cached = store.verify_cache_get(proof["manifest_hash"], bundle_hash_for(proof["bundle_json"]))
    if cached is not None:
        sig_status = cached["status"]
    else:
        manifest_signature = {
            "descriptor": _json.loads(proof["descriptor_json"]),
            "leaf_list": leaf_list,
            "signature_bundle": proof["bundle_json"],
        }
        verified, reason, _ = verify_wire_manifest(manifest_signature)
        sig_status = "VERIFIED" if verified else reason
    if sig_status != "VERIFIED":
        if sig_status in _VERIFIER_UNAVAILABLE:
            return (f"UNVERIFIED — verifier unavailable ({sig_status})", None)
        return (f"SIGNATURE INVALID — {sig_status}", None)

    # Step 2 — MEMBERSHIP (live recompute).
    if not canon.manifest_membership(leaf_list, root, content_hash):
        return ("NOT VERIFIED — not in manifest", None)

    # Step 4 — BINDING (LIVE per read, never cached; verdict-bearing, not display).
    binding = store.get_binding(issuer, subject)
    if binding is None:
        return ("NOT VERIFIED — unbound signer", None)
    if binding.revoked_at is not None:
        return ("NOT VERIFIED — revoked binding", None)

    return (f"SIGNED — {subject or 'verified'} (verified)", {"issuer": issuer, "subject": subject})


def _folio_href(content_hash: str) -> str:
    return f"/folio/{content_hash}"


def _peer_ref(store, content_hash: str, edge: Mapping[str, Any], *, outgoing: bool):
    """Resolve one thread edge to a peer ref, or ``None`` if it is a self-edge.

    The ref carries the thread type, the peer's title, the peer's full ``address``
    (the cross-instance-safe handle), and a station-local ``href`` (which 404s for
    a peer whose only instance is elsewhere — following uses the address, not the
    href). A folio is not its own neighbour: a self-edge is dropped whether the
    peer is the folio's own hash OR a legacy id that aliases back to it.
    """
    peer = edge["to_id"] if outgoing else edge["from_id"]
    if not peer or peer == content_hash:
        return None
    # Resolve the endpoint: a content hash directly, else a legacy id through the
    # alias table. ``target`` is None when the peer is held nowhere local.
    target = peer if store.get_folio(peer) else store.resolve_alias(peer)
    if target == content_hash:
        return None  # aliases back to this folio — not its own neighbour
    prow = store.get_folio(target) if target else None
    if prow is not None:
        address = prow["content_hash"]
        title = prow.get("title") or ""
        href = _folio_href(address)
    else:
        # A peer with no local instance: still a real edge. Expose the raw
        # endpoint as the address so a federating client can chase it.
        address = peer
        title = None
        href = _folio_href(peer)
    return {"type": edge["type"], "title": title, "address": address, "href": href}


def _refs_from_edges(
    store, content_hash: str, edges, *, outgoing: bool, lineage: bool
) -> List[Dict[str, Any]]:
    """Resolve+dedup the cross-reference (``lineage=False``) OR lineage
    (``lineage=True``) subset of one already-fetched edge list.

    Membership/status edges are always excluded (they surface as ``site``/the
    status line). Lineage edges are partitioned to exactly one side of this gate so
    a versioning edge appears in ``lineage`` and never also in ``threads_out/in``.
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for edge in edges:
        etype = edge["type"]
        if etype in _STRUCTURAL_THREADS:
            continue
        if (etype in _LINEAGE_THREADS) != lineage:
            continue
        ref = _peer_ref(store, content_hash, edge, outgoing=outgoing)
        if ref is None:
            continue
        key = (ref["type"], ref["address"])
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


def _folio_descendants(
    store, content_hash: str, children: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """The transitive forward closure over lineage children (breadth-first).

    Seeded with the folio's already-resolved direct ``children`` (so the root's
    incoming edges are not re-queried), then walks INCOMING lineage edges of each
    locally-held descendant (children-of-children …). Bounded by
    ``_DESCENDANTS_MAX``/``_DESCENDANTS_MAX_DEPTH`` and guarded by a visited set so
    a cycle or a huge fork tree can't make one read unbounded. Only peers held
    locally (a resolved content hash) are recursed into; a remote-only child is
    listed but not chased. Order is breadth-first: nearer versions first.
    """
    seen = {content_hash}
    out: List[Dict[str, Any]] = []
    frontier: List[str] = []
    for ref in children:
        if len(out) >= _DESCENDANTS_MAX:
            break
        addr = ref["address"]
        if addr in seen:
            continue
        seen.add(addr)
        out.append(ref)
        if ref["title"] is not None:  # held locally → safe to recurse
            frontier.append(addr)
    depth = 1
    while frontier and depth < _DESCENDANTS_MAX_DEPTH and len(out) < _DESCENDANTS_MAX:
        nxt: List[str] = []
        for node in frontier:
            for ref in _refs_from_edges(
                store, node, store.get_threads(to_id=node), outgoing=False, lineage=True
            ):
                addr = ref["address"]
                if addr in seen:
                    continue
                seen.add(addr)
                out.append(ref)
                if ref["title"] is not None:  # held locally → safe to recurse
                    nxt.append(addr)
                if len(out) >= _DESCENDANTS_MAX:
                    break
            if len(out) >= _DESCENDANTS_MAX:
                break
        frontier = nxt
        depth += 1
    return out


def _folio_lineage(
    store, content_hash: str, out_edges, in_edges
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build the lineage block, the ``superseded_by`` ref, and the descendant list.

    Reuses the two edge lists the envelope already fetched (one query per
    direction), so a folio with no lineage costs no extra queries; the sibling and
    descendant lookups fire only when parents / children actually exist.

    Returns ``(lineage, superseded_by, descendants)`` where ``lineage`` is
    ``{parents, children, siblings}``:

    - ``parents`` — every OUTGOING lineage edge. Data model rev 4 mandates at most
      one parent, so this is normally a 0- or 1-element list; a display layer takes
      ``parents[0]`` for the singular "parent" UI. It is a list, not a scalar, so a
      malformed corpus that carries more than one is surfaced in full rather than
      silently truncated — the wire is load-bearing and must not hide corruption.
    - ``children`` — every INCOMING lineage edge.
    - ``siblings`` — co-children of this folio's parent(s), deduped, self excluded.

    ``superseded_by`` is an INCOMING ``supersedes`` edge — the fork-hatnote "a newer
    version exists" — and is a subset of ``children``.
    """
    parents = _refs_from_edges(store, content_hash, out_edges, outgoing=True, lineage=True)
    children = _refs_from_edges(store, content_hash, in_edges, outgoing=False, lineage=True)

    siblings: List[Dict[str, Any]] = []
    seen_siblings = set()
    # Co-children of each parent — only resolvable when the parent is held locally
    # (its address is a content hash we can query for ITS children). Deduped across
    # parents and against this folio itself.
    for parent in parents:
        if parent["title"] is None:
            continue
        parent_hash = parent["address"]
        for ref in _refs_from_edges(
            store, parent_hash, store.get_threads(to_id=parent_hash), outgoing=False, lineage=True
        ):
            if ref["address"] == content_hash or ref["address"] in seen_siblings:
                continue
            seen_siblings.add(ref["address"])
            siblings.append(ref)

    superseded_by = next((c for c in children if c["type"] == "supersedes"), None)
    descendants = _folio_descendants(store, content_hash, children) if children else []

    lineage = {"parents": parents, "children": children, "siblings": siblings}
    return lineage, superseded_by, descendants


def _folio_site(store, content_hash: str) -> Optional[Dict[str, Any]]:
    slug = store.folio_site_slug(content_hash)
    if not slug:
        return None
    site_hash = store.resolve_slug(slug)
    return {"slug": slug, "address": site_hash, "href": f"/site/{slug}"}


def build_folio_envelope(
    store,
    content_hash: str,
    *,
    address: Optional[str] = None,
    row: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the unified envelope for a folio from the native store.

    ``content_hash`` must already exist (the caller's resolve+get_folio is the
    existence gate). ``address`` is the canonical address to echo back (defaults
    to the bare content hash; a configured ``web::`` authority supplies the
    federated form in a later slice). ``row`` may be passed to avoid a re-read.
    """
    if row is None:
        row = store.get_folio(content_hash)
        if row is None:
            raise KeyError(content_hash)

    body = {field: row.get(field) for field in BODY_FIELDS}

    # Resolve the folio's covering manifest once; its bundle is the proof and the
    # verdict's signature source (per-folio bundles no longer exist).
    proof = store.get_constituent_proof(content_hash)
    has_proof = proof is not None and not proof.get("proof_missing")
    signature_bundle = _bundle_object(proof["bundle_json"]) if has_proof else None
    verdict, identity = folio_verdict(store, content_hash, row, proof=proof)

    # One query per direction; the cross-reference (threads) and lineage subsets are
    # partitioned from the SAME fetched lists, so the no-lineage common case adds no
    # extra reads over the old two-query threads build.
    out_edges = store.get_threads(from_id=content_hash)
    in_edges = store.get_threads(to_id=content_hash)
    threads_out = _refs_from_edges(store, content_hash, out_edges, outgoing=True, lineage=False)
    threads_in = _refs_from_edges(store, content_hash, in_edges, outgoing=False, lineage=False)
    lineage, superseded_by, descendants = _folio_lineage(
        store, content_hash, out_edges, in_edges
    )

    links = {
        "self": _folio_href(content_hash),
        "markdown": f"{_folio_href(content_hash)}.md",
        "json": f"{_folio_href(content_hash)}.json",
        "catalog": "/",
    }
    if signature_bundle is not None:
        links["bundle"] = f"{_folio_href(content_hash)}/bundle"

    env = {
        "schema": SCHEMA,
        "address": address or content_hash,
        "kind": "folio",
        "stability": "stable",
        "as_of": None,
        "body": body,
        "proof": {
            "profile": CANON_PROFILE,
            "content_hash": content_hash,
            "signature_bundle": signature_bundle,
        },
        "asserted": {
            "verdict": verdict,
            # The verified signer identity ({"issuer", "subject"}) or null — the
            # station's word (it rides under asserted, never trusted without
            # re-verifying the bundle). Surfaced so the provenance card and the
            # for-agents box read the signer cleanly instead of parsing the verdict
            # string; it is exactly what folio_verdict already computed.
            "signer": identity,
            "status": store.latest_statuses([content_hash]).get(content_hash, "open"),
            "site": _folio_site(store, content_hash),
            "threads_out": threads_out,
            "threads_in": threads_in,
            "lineage": lineage,
            "superseded_by": superseded_by,
            "descendants": descendants,
        },
        "links": links,
        "next": None,
    }
    return validate_envelope(env)


def _bundle_object(bundle_json: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse the stored bundle JSON text into the wire object form (§3), or None.

    The store holds the bundle as ``SignatureBundle.model_dump_json`` text; on the
    wire it is presented as a JSON object. A malformed sidecar is surfaced as
    unsigned-with-no-object rather than crashing the read (the verdict line, built
    separately, still reports the malformed bundle).
    """
    if not bundle_json:
        return None
    import json

    try:
        return json.loads(bundle_json)
    except (ValueError, TypeError):
        return None


# --- collections (catalog / site / search) ----------------------------------


def folio_entry(row: Mapping[str, Any], *, snippet: Optional[str] = None) -> Dict[str, Any]:
    """A collection entry pointing at a folio: an address that feeds resolve.

    Entries are the station's table of contents — discovery, not verifiable. The
    ``snippet`` is untrusted content (it goes inside the fence when rendered).
    """
    content_hash = row["content_hash"]
    return {
        "address": content_hash,
        "kind": "folio",
        "type": row.get("type") or "folio",
        "title": row.get("title") or "",
        "snippet": snippet,
        "href": _folio_href(content_hash),
    }


def build_collection_envelope(
    kind: str,
    address: str,
    entries: List[Dict[str, Any]],
    *,
    asserted: Optional[Dict[str, Any]] = None,
    links: Optional[Dict[str, Any]] = None,
    next_address: Optional[str] = None,
) -> Dict[str, Any]:
    """A derived envelope over a list of entries (catalog/site/search).

    ``proof`` is null and ``as_of`` is stamped: the items it points at are each
    independently verifiable, but the completeness of the *set* is only the
    station's word at this instant (§5). ``next_address`` is a cursor-address for
    pagination (a later slice; the field exists so the shape is stable).
    """
    env = {
        "schema": SCHEMA,
        "address": address,
        "kind": kind,
        "stability": "derived",
        "as_of": _now_iso(),
        "body": entries,
        "proof": None,
        "asserted": asserted or {},
        "links": links or {"catalog": "/"},
        "next": next_address,
    }
    return validate_envelope(env)


# --- error / absence --------------------------------------------------------


def build_error_envelope(
    error: str,
    address: str,
    *,
    origin: Optional[str] = None,
) -> Dict[str, Any]:
    """A machine-parseable miss/error envelope — never an English sentence (§6).

    A 404 from a federating cache distinguishes "origin returned 404" from "never
    existed here" via ``links.origin``.
    """
    links: Dict[str, Any] = {"catalog": "/"}
    if origin:
        links["origin"] = origin
    env = {
        "schema": SCHEMA,
        "address": address,
        "kind": "error",
        "stability": "derived",
        "as_of": _now_iso(),
        "body": {"found": False, "error": error},
        "proof": None,
        "asserted": {},
        "links": links,
        "next": None,
        "suggestion": "mesh fetch <address>",
    }
    return validate_envelope(env)
