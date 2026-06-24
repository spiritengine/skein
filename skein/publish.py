"""Client-side publish: push selected folios from a local station to an instance.

PROTOTYPE — unsigned. This is the write side of the client/instance split
(finding-20260521-kwtb): a client gathers a closed set of local folios and pushes
them to an instance's ingress, which stores them and serves them read-only. The
publish step is the boundary where signing will later happen (brief-20260522-q8k0);
for now the batch crosses unsigned, protected only by the content-hash integrity
check the ingress performs.

Selection and the closed-graph rule
-----------------------------------
You publish a whole site (``--site SLUG``) or specific folios (refs). Either way
the set is closed before it goes:

- the site folio itself travels (as a ``type=site`` folio), so the instance can
  reconstruct membership against the same site hash rather than minting a new one;
- a thread is included only when BOTH its endpoints are *eligible*, where
  eligible = (in this batch) OR (already published to this same instance). That
  keeps ``within`` membership and ``status`` self-loops (endpoints in the batch),
  and it makes dropped edges CONVERGENT: an edge to a folio that wasn't published
  yet is dropped now, but once that folio is itself published to the instance it
  becomes eligible, so the next publish carries the edge along (Patrick's call,
  2026-06-01). Only edges incident to the batch are scanned, so a publish ships
  what changed plus newly-closable links, not the whole history every time.
  (Spec gap noted: a brand-new edge between two already-published folios that are
  not re-named in any later batch is not picked up — that is "publish an edge",
  a different verb from "publish folios".)

``published`` threads are never sent: they are client-local bookkeeping (which
instances a folio went to), and the instance knows a folio is published simply
by holding it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .station import Station
from .station import UnknownFolio, UnknownSite
from . import wire
from . import sign as _sign


class PublishError(RuntimeError):
    """A publish batch could not be delivered or was rejected by the instance."""


def canonical_instance(url: str) -> str:
    """Canonical identity for a publish target, so cosmetic URL variants collapse.

    The instance identifier is used as both the publish endpoint and the ``to_id``
    of the client's ``published`` bookkeeping edges. If ``http://h:9101`` and
    ``http://h:9101/`` recorded as different strings, convergence eligibility
    (:func:`_already_on_instance`) would miss a folio that IS on the instance and
    silently re-drop its edges, and ``published_instances`` would double-count.
    Canonicalize once — lowercase scheme+host, drop default ports and a trailing
    slash — so one instance is one identity.
    """
    parts = urllib.parse.urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    # Re-bracket an IPv6 literal so the netloc reparses (hostname strips the [ ]).
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    path = parts.path.rstrip("/")
    return urllib.parse.urlunsplit((scheme, host, path, "", ""))


def collect_publish_set(
    station: Station,
    refs: Optional[List[str]] = None,
    site: Optional[str] = None,
    instance: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, str]]:
    """Resolve a selection into (folios, threads, site_slugs) ready for a batch.

    ``site`` publishes every member of the site plus the site folio. ``refs``
    publishes those folios plus, for each, its site folio (so membership lands).
    The two may be combined. ``instance`` is the publish target; when given, an
    edge to a folio already published *there* is eligible to travel too, which is
    what makes dropped edges convergent across successive publishes.

    A folio published by ref carries only its alphabetically-first site
    (``folio_site_slug``); if it belongs to several sites, membership in the
    others lands convergently only when those sites are themselves published.
    """
    folios_by_hash: Dict[str, Dict[str, Any]] = {}
    site_slugs: Dict[str, str] = {}

    def add_folio(row: Dict[str, Any]) -> None:
        folios_by_hash[row["content_hash"]] = row

    def add_site(slug: str) -> None:
        site_hash = station.store.resolve_slug(slug)
        if not site_hash:
            raise UnknownSite(slug)
        site_folio = station.store.get_folio(site_hash)
        if site_folio:
            add_folio(site_folio)
            site_slugs[site_hash] = slug

    if site:
        members = station.folios_in_site(site)  # raises UnknownSite
        add_site(site)
        for m in members:
            add_folio(m)

    for ref in refs or []:
        h = station.resolve_ref(ref)
        if not h:
            raise UnknownFolio(ref)
        folio = station.store.get_folio(h)
        if not folio:
            raise UnknownFolio(ref)
        add_folio(folio)
        # Carry the folio's site so membership reconstructs on the instance.
        slug = station.store.folio_site_slug(h)
        if slug:
            add_site(slug)

    batch = set(folios_by_hash)
    threads = _closed_threads(station, batch, instance)
    return list(folios_by_hash.values()), threads, site_slugs


def _already_on_instance(station: Station, instance: Optional[str]) -> set:
    """Folio hashes already published to ``instance`` (its inbound ``published`` edges)."""
    if not instance:
        return set()
    return {
        t["from_id"]
        for t in station.store.get_threads(
            type=Station.PUBLISHED_THREAD, to_id=instance
        )
        if t.get("from_id")
    }


def _closed_threads(
    station: Station, batch: set, instance: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Edges incident to ``batch`` whose both endpoints are eligible.

    Eligible = in ``batch`` OR already published to ``instance``. Scanning only
    edges incident to the batch keeps a publish proportional to what changed
    while still carrying newly-closable links (an edge whose other end was
    published in an earlier batch). ``published`` bookkeeping edges never travel.
    """
    eligible = batch | _already_on_instance(station, instance)
    seen: Dict[str, Dict[str, Any]] = {}
    for h in batch:
        for edge in station.store.get_threads(from_id=h):
            seen[edge["thread_hash"]] = edge
        for edge in station.store.get_threads(to_id=h):
            seen[edge["thread_hash"]] = edge
    return [
        t
        for t in seen.values()
        if t.get("type") != Station.PUBLISHED_THREAD
        and t.get("from_id") in eligible
        and t.get("to_id") in eligible
    ]


def post_batch(instance_url: str, batch: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    """POST a publish batch to an instance's ingress; return the parsed ack.

    Raises :class:`PublishError` on transport failure or a non-2xx response.
    """
    endpoint = canonical_instance(instance_url) + "/publish/v0/folios"
    body = json.dumps(batch).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise PublishError(f"instance rejected publish ({e.code}): {detail}") from e
    except urllib.error.URLError as e:
        raise PublishError(f"could not reach instance at {endpoint}: {e.reason}") from e


def post_redeem(
    instance_url: str, token: str, proof: Dict[str, Any], timeout: float = 60.0
) -> Tuple[int, Dict[str, Any]]:
    """POST a redeem to an instance's ``/invite/redeem``; return ``(status, body)``.

    Unlike :func:`post_batch`, a non-2xx is NOT raised: the redeem route returns a
    typed JSON reason on a logical rejection (used/expired/revoked token, bad
    proof), and the CLI needs to surface that reason, so a 4xx/409/429 status comes
    back with its parsed body. Only a genuine TRANSPORT failure raises
    :class:`PublishError`. Timeout is generous — the station verifies a Sigstore
    bundle (Fulcio chain + Rekor inclusion) before answering."""
    from .sign import REDEEM_ROUTE

    endpoint = canonical_instance(instance_url) + REDEEM_ROUTE
    body = json.dumps({"token": token, "proof": proof}).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(detail)
        except ValueError:
            return e.code, {"error": detail}
    except urllib.error.URLError as e:
        raise PublishError(f"could not reach instance at {endpoint}: {e.reason}") from e


def publish(
    station: Station,
    instance_url: str,
    refs: Optional[List[str]] = None,
    site: Optional[str] = None,
    by: Optional[str] = None,
    dry_run: bool = False,
    signer: Optional["_sign.Signer"] = None,
    signed_intent: Optional[bool] = None,
) -> Dict[str, Any]:
    """Collect, push, and (on ack) record publish-state for a selection.

    Returns a result dict: the batch counts, the instance ack, and the folio
    hashes that were marked published locally. On ``dry_run`` nothing is sent,
    SIGNED, or recorded — the collected set is returned for inspection (signing
    runs the irreversible Sigstore ceremony, so it is gated to the real send).

    When ``signer`` is given, each folio is signed at this boundary (sign-at-
    publish) and its ``signature_bundle`` rides the wire; the client also keeps a
    copy so its local record reflects what the instance now holds. Without a
    signer the batch crosses unsigned (the pre-mesh / prototype path).

    ``signed_intent`` lets a ``dry_run`` report whether the real send WOULD sign
    without constructing a signer (no OIDC login on a dry run); when omitted the
    plan falls back to the presence of a ``signer``.
    """
    # Canonicalize the target once so eligibility, recording, and the endpoint
    # all key off one identity (a trailing slash must not fork the instance).
    has_target = not (dry_run and instance_url in (None, "(dry-run)"))
    instance = canonical_instance(instance_url) if has_target else None

    folios, threads, site_slugs = collect_publish_set(
        station, refs=refs, site=site, instance=instance
    )
    if not folios:
        return {"folios": 0, "threads": 0, "dry_run": dry_run, "sent": False}

    batch = wire.build_batch(folios, threads, site_slugs=site_slugs)

    # A dry run sends nothing to an instance and MUST NOT produce any external,
    # irreversible artifact — so it short-circuits BEFORE the signing ceremony.
    # With a real signer, sign_manifest runs the Sigstore flow: it consumes the
    # short-lived OIDC token, issues a Fulcio cert, and writes a PERMANENT, PUBLIC
    # Rekor transparency-log entry. None of that can be undone, and a dry run is
    # precisely the case where nothing should leave the machine. Report the plan
    # (what WOULD be published, and whether it would be signed) without signing.
    # The signed-intent is taken from the caller's request — ``signed_intent``
    # when given (so a dry run need not build a signer at all), else the presence
    # of a signer — never from having actually signed anything.
    if dry_run:
        would_sign = signed_intent if signed_intent is not None else signer is not None
        return {
            "instance": instance,
            "folios": len(folios),
            "threads": len(threads),
            "signed": would_sign,
            "dry_run": True,
            "folio_hashes": [f["content_hash"] for f in folios],
            "sent": False,
        }

    # Sign at the boundary: build ONE manifest = the Merkle root over EVERY
    # constituent's content hash (folios AND threads), signed once (one OIDC
    # ceremony). The manifest_signature rides at the top of the batch; per-folio
    # bundles no longer exist (the unified signing model).
    manifest_signature = None
    if signer is not None:
        addresses = [wf["content_hash"] for wf in batch["folios"]]
        addresses += [wt["thread_hash"] for wt in batch["threads"]]
        # Fail fast against the SAME leaf cap the verifier enforces (_sign.MAX_LEAVES),
        # BEFORE the Sigstore ceremony — otherwise a large publish would consume the
        # OIDC token, issue a Fulcio cert, and write a PERMANENT public Rekor entry,
        # only for the ingress to reject the whole batch as 'manifest malformed'.
        # The cap is on DISTINCT leaves (build_manifest dedups), so count distinct.
        distinct = len(set(addresses))
        if distinct > _sign.MAX_LEAVES:
            raise PublishError(
                f"publish has {distinct} distinct constituents, over the "
                f"{_sign.MAX_LEAVES}-leaf manifest cap; split it into smaller publishes "
                "(refusing before the Sigstore ceremony so no transparency-log entry is burned)"
            )
        manifest_signature = _sign.sign_manifest(addresses, signer)
        batch["manifest_signature"] = manifest_signature

    summary: Dict[str, Any] = {
        "instance": instance,
        "folios": len(folios),
        "threads": len(threads),
        "signed": signer is not None,
        "dry_run": dry_run,
        "folio_hashes": [f["content_hash"] for f in folios],
    }

    ack = post_batch(instance, batch)
    summary["ack"] = ack
    summary["sent"] = True
    # Surface refused edges: a "dangling endpoint" rejection means a link the
    # caller expected to close did not — silence here reads as a closed graph
    # when it isn't (the convergence retry only fires on a later publish).
    summary["threads_rejected"] = (ack.get("threads") or {}).get("rejected", [])

    # Record publish-state for the folios the instance accepted or already held.
    landed = set(ack.get("accepted", [])) | set(ack.get("existing", []))
    recorded: List[str] = []
    for f in folios:
        h = f["content_hash"]
        if h in landed:
            station.record_published(h, instance, by=by)
            recorded.append(h)
    # Mirror the manifest client-side under the client's own identity, so the
    # client's record reflects what the instance now holds (SG2).
    if manifest_signature is not None:
        thread_landed = set(
            (ack.get("threads") or {}).get("accepted", [])
        ) | set((ack.get("threads") or {}).get("existing", []))
        _mirror_manifest(station.store, manifest_signature, landed | thread_landed)
    summary["recorded_published"] = recorded
    return summary


def _mirror_manifest(store, manifest_signature: Dict[str, Any], constituent_hashes: set) -> None:
    """Record the manifest + constituent attribution on the client store.

    The client keeps its own copy of the manifest it signed (manifest row before
    attribution rows, same transaction), so its local read surface attributes the
    same constituents to the same signer the instance does."""
    import json

    from .canon import manifest_descriptor_canonical_bytes
    from .identity import content_hash_for_bytes

    descriptor = manifest_signature["descriptor"]
    root = descriptor["root"]
    leaf_count = descriptor["leaf_count"]
    manifest_hash = content_hash_for_bytes(
        manifest_descriptor_canonical_bytes(root, leaf_count)
    )
    issuer, subject = manifest_signature["issuer"], manifest_signature["subject"]
    with store.transaction():
        store.add_manifest(
            root,
            manifest_hash,
            json.dumps(descriptor, sort_keys=True),
            json.dumps(manifest_signature["leaf_list"]),
            manifest_signature["signature_bundle"],
            issuer,
            subject,
            leaf_count,
        )
        for h in constituent_hashes:
            kind = "folio" if store.get_folio(h) else "thread"
            store.add_constituent_attribution(h, kind, root, issuer, subject)
