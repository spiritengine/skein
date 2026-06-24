"""Import bridge: a legacy SKEIN project store -> a content-hash skein store.

Slice 2 of the parallel station (brief-20260529-i6fy). The bridge reads a legacy
``.skein/data/skein.db`` strictly read-only and produces a faithful skein
store under ``.skein/``. It never writes the legacy database.

What it does, and the decisions behind each step:

1. **Folios** are re-hashed from their five canonical fields with a normalized
   ``created_at`` (``store.create_folio`` does the hashing). The legacy
   ``content_hash`` column is never trusted — the audit proved it was written
   over at least three incompatible ``created_at`` encodings, so roughly half of
   the stored hashes do not reproduce. Each ``legacy_id -> new hash`` mapping is
   recorded in the alias table (open-call #1).

2. **Thread endpoints** are classified three ways (open-call #2):
   - a folio id present in this project -> resolved to its content hash;
   - an actor (agent name, shard hex, session string — anything that is not a
     folio id here);
   - a folio-id-shaped reference that is not present in this project (dangling or
     cross-project ``proj:id``) -> kept verbatim as the legacy id so it resolves
     lazily through the alias table if the target ever imports.

   Actor endpoints are handled by *meaning*, not by a blanket "actors are
   weavers" rule (the from/to columns are untyped TEXT and already hold non-hash
   strings — that is exactly how unresolved/cross-project refs are kept):

   - **Actor relates to a folio** (the other endpoint is a folio/unresolved/none):
     the actor folds into the thread's ``weaver`` and its edge slot clears. This
     is the lossless case — the actor's identity survives as the weaver.
   - **Actor relates to an actor** (both endpoints are *distinct* actors, e.g. a
     ``succession`` session-handoff A->B, or agent-to-agent message/mention):
     both actor strings are kept verbatim on the edge, same as an unresolved ref.
     Nulling them would produce ``from=None,to=None`` husks that erase the
     relationship; instead the new store keeps ``from=A,to=B``.

   There are three identity slots (from, to, weaver) and at most three legacy
   identities (from-actor, to-actor, legacy weaver), so no actor identity is ever
   dropped: each endpoint ends up a resolved folio hash, a kept actor/unresolved
   string, or folded into a weaver that holds that same identity. Thread types
   pass through untouched; the sole rename is ``succession -> supersedes`` when
   the link actually joins two folios (session-to-session succession stays
   ``succession`` with both endpoints intact).

3. **Sites** (filesystem JSON under ``.skein/data/sites/``) each become a
   ``type=site`` folio (content = its purpose); every member folio gets a
   ``within`` thread (folio -> site folio); a station-internal slug table maps
   ``slug -> site folio hash``.

4. **Idempotent.** Content-hash identity dedups folios and threads, so running
   twice against a frozen source yields an identical store.

5. **No silent loss.** :class:`ImportReport` accounts for what carried, what was
   reclassified (actors->weaver, actor<->actor edges kept, unresolved refs kept),
   and anything merged — including genuinely-distinct edges the thread hash
   collapses (a Slice 1 fell note). The legacy ``metadata`` column is now CARRIED:
   folded into folio content via the ``legacy-meta`` envelope
   (:mod:`skein.legacy_meta`), minus the dead ``questions_enabled`` flag.
   Any *other* populated legacy column the new model does not carry (e.g.
   ``target_agent``) is COUNTED so the loss is visible, not silent; whether to
   carry those is a separate product call.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from .legacy_meta import normalize_legacy_meta, render_legacy_meta
from .store import SkeinStore

# A folio id looks like ``<type>-<8-digit-date>-<suffix>``. The date shape alone
# does not separate a folio ref from a session id (``qm-20260105-201613`` matches
# too), so classification also requires the prefix to be a real folio *type* —
# the whitelist is derived per-project from the legacy ``folios.type`` column and
# the prefixes of actual folio ids, so sessions and agents never pass.
_FOLIO_ID_SHAPE = re.compile(r"^([a-z]+)-\d{8}-\w+$", re.IGNORECASE)

_OPEN_STATUSES = {None, "", "open"}


def open_legacy(db_path: Union[str, Path]) -> sqlite3.Connection:
    """Open a legacy SKEIN sqlite database read-only.

    Tries ``mode=ro`` first (the documented safe default on a writable
    filesystem). Falls back to ``immutable=1`` when the database sits on a
    read-only mount, where ``mode=ro`` cannot create the shared-memory/journal
    files SQLite wants. Both modes are read-only; neither writes the source.

    Caveat: ``immutable=1`` tells SQLite the file cannot change, so it does not
    consult the ``-wal`` sidecar. If the source db has uncommitted WAL frames
    (an open writer mid-transaction), the immutable fallback can read a slightly
    stale snapshot that omits them. This is fine for a frozen cutover (the source
    is quiescent and checkpointed); ``mode=ro``, tried first, is WAL-aware.
    """
    p = str(Path(db_path))
    last_err: Optional[Exception] = None
    for uri in (f"file:{p}?mode=ro", f"file:{p}?immutable=1"):
        try:
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT 1 FROM folios LIMIT 1")  # force the open to resolve
            return conn
        except sqlite3.OperationalError as e:
            last_err = e
            try:
                conn.close()  # type: ignore[has-type]
            except Exception:
                pass
    raise sqlite3.OperationalError(
        f"could not open {p} read-only (mode=ro or immutable=1): {last_err}"
    )


def _folio_types(conn: sqlite3.Connection) -> Set[str]:
    """The set of real folio type prefixes in this legacy db (the whitelist)."""
    types = {
        r[0].lower()
        for r in conn.execute("SELECT DISTINCT type FROM folios")
        if r[0]
    }
    for r in conn.execute("SELECT folio_id FROM folios"):
        fid = r[0]
        if fid and "-" in fid:
            types.add(fid.split("-", 1)[0].lower())
    return types


def classify_endpoint(
    endpoint: Optional[str],
    folio_ids: Set[str],
    folio_types: Set[str],
    known_actors: Optional[Set[str]] = None,
) -> Tuple[str, Optional[str]]:
    """Classify a legacy thread endpoint.

    Returns ``(kind, value)`` where ``kind`` is one of:
    - ``"none"``        — endpoint is absent;
    - ``"folio"``       — a folio present in this project (resolve via alias);
    - ``"unresolved"``  — a folio-id-shaped ref not present here (dangling or
                          cross-project); ``value`` is the legacy id to keep;
    - ``"actor"``       — anything else (agent/session/shard).

    ``known_actors`` is an optional set of strings already known to be actors —
    in the bridge it is every distinct ``threads.weaver`` value. It guards the
    one ambiguous shape (FINDING 3): an actor id like ``<type>-<8digits>-<suffix>``
    whose prefix happens to coincide with a real folio type (e.g. a session named
    ``brief-20260105-201613``) would otherwise be misread as an unresolved folio
    ref and minted as a bogus edge. Checking it against the actor set first
    classifies it correctly. Residual assumption: an actor with a folio-type
    prefix that *never* appears as a weaver anywhere in the corpus is not covered
    by this guard (zero such occurrences observed); the shape heuristic would
    still mark it unresolved.
    """
    if endpoint is None:
        return ("none", None)
    if endpoint in folio_ids:
        return ("folio", endpoint)
    if known_actors and endpoint in known_actors:
        return ("actor", endpoint)
    base = endpoint.split(":", 1)[1] if ":" in endpoint else endpoint
    m = _FOLIO_ID_SHAPE.match(base)
    if m and m.group(1).lower() in folio_types:
        return ("unresolved", endpoint)
    return ("actor", endpoint)


@dataclass
class ImportReport:
    """A full accounting of an import — nothing is allowed to vanish silently."""

    source_db: str = ""
    sites_dir: str = ""

    folios_seen: int = 0
    folios_carried: int = 0          # legacy folios mapped to a new hash
    folio_hash_collisions: int = 0   # distinct legacy folios that hashed identically

    sites_seen: int = 0
    sites_carried: int = 0
    sites_skipped_no_id: int = 0     # site JSON files dropped for lacking a site_id

    folios_with_site_id: int = 0     # legacy folios carrying a (non-empty) site_id

    threads_seen: int = 0            # legacy thread rows read
    threads_carried: int = 0         # distinct new thread hashes from those rows
    threads_merged: int = 0          # legacy rows that collapsed onto an existing hash
    merge_examples: List[str] = field(default_factory=list)

    within_threads: int = 0          # synthesized folio->site membership edges
    within_unresolved: int = 0       # member folios whose site had no JSON

    actor_endpoints_to_weaver: int = 0   # actor endpoint occurrences folded to weaver (lossless)
    actor_endpoints_kept: int = 0        # actor endpoint occurrences kept verbatim on the edge
    actor_to_actor_edges: int = 0        # threads kept as a distinct actor<->actor relationship
    actor_endpoints_dropped: int = 0     # actor identities that could not be represented (must stay 0)
    dropped_examples: List[str] = field(default_factory=list)

    unresolved_refs: int = 0         # folio-shaped endpoint occurrences kept as legacy ids
    unresolved_distinct: Set[str] = field(default_factory=set)  # distinct legacy ids kept
    cross_project_refs: int = 0      # subset of unresolved with a ``proj:`` prefix
    dangling_refs: int = 0           # subset of unresolved without a prefix

    # Populated legacy columns the new model does not carry (FINDING 2). Loss is
    # made visible here rather than vanishing silently; carrying them is a
    # separate product call. column name -> count of rows with a non-empty value.
    dropped_folio_columns: Dict[str, int] = field(default_factory=dict)
    dropped_thread_columns: Dict[str, int] = field(default_factory=dict)

    metadata_carried: int = 0        # folios whose legacy metadata was folded into content
    metadata_flag_dropped: int = 0   # folios where ONLY the dead questions_enabled flag dropped

    succession_renamed: int = 0      # succession -> supersedes

    status_without_thread: int = 0   # non-open folio status with no status thread
    status_examples: List[str] = field(default_factory=list)

    notes: List[str] = field(default_factory=list)

    def render(self) -> str:
        """A plain-text report (no tables/markdown — screen-reader friendly)."""
        lines = [
            f"import report: {self.source_db}",
            f"folios: carried {self.folios_carried} of {self.folios_seen} seen",
            f"folio hash collisions: {self.folio_hash_collisions}",
            f"sites: carried {self.sites_carried} of {self.sites_seen} seen",
            f"site JSON skipped (no site_id): {self.sites_skipped_no_id}",
            f"folios carrying a site_id: {self.folios_with_site_id}",
            f"within threads: {self.within_threads} (unresolved site refs {self.within_unresolved})",
            f"threads: carried {self.threads_carried} of {self.threads_seen} seen",
            f"threads merged (distinct legacy edges collapsed to one hash): {self.threads_merged}",
            f"actor endpoints folded to weaver (lossless): {self.actor_endpoints_to_weaver}",
            f"actor endpoints kept verbatim on edge: {self.actor_endpoints_kept}",
            f"actor<->actor edges preserved (both endpoints kept): {self.actor_to_actor_edges}",
            f"actor identities dropped (unrepresentable): {self.actor_endpoints_dropped}",
            f"unresolved refs kept as legacy ids: {self.unresolved_refs} occurrences "
            f"({len(self.unresolved_distinct)} distinct; "
            f"cross-project {self.cross_project_refs}, dangling {self.dangling_refs})",
            f"succession renamed to supersedes: {self.succession_renamed}",
            f"non-open folios without a status thread: {self.status_without_thread}",
            f"legacy metadata folded into content: {self.metadata_carried} folios; "
            f"flag-only metadata skipped (dead questions_enabled, nothing else to "
            f"carry): {self.metadata_flag_dropped} folios",
        ]
        for col, n in sorted(self.dropped_folio_columns.items()):
            lines.append(
                f"legacy folio column not carried: {col} populated in {n} folios"
            )
        for col, n in sorted(self.dropped_thread_columns.items()):
            lines.append(
                f"legacy thread column not carried: {col} populated in {n} threads"
            )
        for ex in self.merge_examples[:5]:
            lines.append(f"  merged: {ex}")
        # Dropped actor identities must never happen; if any do, enumerate ALL of
        # them (no cap) — silent truncation here would hide real relational loss.
        for ex in self.dropped_examples:
            lines.append(f"  dropped actor: {ex}")
        for ex in self.status_examples[:5]:
            lines.append(f"  status-only: {ex}")
        for n in self.notes:
            lines.append(f"note: {n}")
        return "\n".join(lines)


_FOLIO_COLS = ("folio_id", "type", "site_id", "created_at", "created_by",
               "title", "content", "status", "metadata")
_THREAD_COLS = ("thread_id", "from_id", "to_id", "type", "content", "weaver",
                "created_at")


# Legacy columns the bridge already accounts for, so they are NOT silent loss:
# folio_id -> alias, site_id -> within thread, status -> status-thread audit,
# content_hash -> deliberately re-minted (never trusted). The five canonical
# fields carry into the new folio. Any *other* populated column is invisible loss
# unless the report counts it (FINDING 2).
_ACCOUNTED_FOLIO_COLS = {
    "folio_id", "type", "site_id", "created_at", "created_by",
    "title", "content", "status", "content_hash",
    # metadata is now carried — folded into content via the legacy-meta envelope
    # (minus the dead questions_enabled flag), so it is no longer silent loss.
    "metadata",
}
# Threads are content-addressed; thread_id is replaced by the hash. The other
# six columns carry into the new thread.
_ACCOUNTED_THREAD_COLS = {
    "thread_id", "from_id", "to_id", "type", "content", "weaver", "created_at",
}


def _populated_unaccounted_columns(
    conn: sqlite3.Connection, table: str, accounted: Set[str]
) -> Dict[str, int]:
    """Count rows with a non-empty value in each legacy column not carried.

    Defensive about schema drift: legacy databases vary (tome lacks columns
    speakbot has), so the present columns are read from ``PRAGMA table_info`` and
    only the ones outside ``accounted`` are counted. A value counts as populated
    when it is non-NULL and, cast to text and trimmed, is not a default/empty
    token: ``''``, ``{}``, ``[]``, ``null``, or ``0``. Excluding ``0`` keeps an
    all-default boolean flag (e.g. an ``archived`` column that is ``0`` for every
    row, carrying no information to migrate) from being reported as phantom loss,
    while a flag with real ``1`` values still surfaces its count of set rows. The
    SKEIN folio/thread schema has no column where a literal ``0`` is meaningful
    content, so this does not undercount real data.
    """
    present = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    counts: Dict[str, int] = {}
    for col in present:
        if col in accounted:
            continue
        n = conn.execute(
            f'SELECT COUNT(*) FROM {table} '
            f'WHERE "{col}" IS NOT NULL '
            f"AND TRIM(CAST(\"{col}\" AS TEXT)) NOT IN ('', '{{}}', '[]', 'null', '0')"
        ).fetchone()[0]
        if n:
            counts[col] = n
    return counts


def _load_sites(sites_dir: Path) -> List[Dict[str, Any]]:
    """Read each ``<site>/metadata.json`` under the sites directory."""
    sites: List[Dict[str, Any]] = []
    if not sites_dir.is_dir():
        return sites
    for meta in sorted(sites_dir.glob("*/metadata.json")):
        try:
            sites.append(json.loads(meta.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return sites


def _resolve_thread_endpoints(
    from_id: Optional[str],
    to_id: Optional[str],
    legacy_weaver: Optional[str],
    store: SkeinStore,
    folio_ids: Set[str],
    folio_types: Set[str],
    known_actors: Set[str],
    report: ImportReport,
) -> Tuple[Optional[str], Optional[str], Optional[str], List[str]]:
    """Map a legacy edge to new (from, to, weaver), classifying each endpoint.

    Returns ``(new_from, new_to, weaver, endpoint_kinds)``.

    Folio and unresolved endpoints resolve directly (hash / kept legacy id).
    Actor endpoints are placed by meaning, never blindly nulled:

    - When the two endpoints are *distinct actors*, the thread is an actor<->actor
      relationship (e.g. a session-handoff succession A->B): both actor strings
      are kept verbatim on the edge. The legacy weaver, if any, stays the weaver.
    - Otherwise (at most one distinct actor across the edges, including an actor
      self-loop) the actor folds into the weaver slot and its edge clears — but
      only when that is lossless: if the weaver slot already holds a *different*
      identity, the actor is kept verbatim on its edge instead of being dropped.

    With three slots (from, to, weaver) and at most three legacy identities, every
    actor identity always finds a home; ``actor_endpoints_dropped`` stays 0.
    """
    classified = [
        classify_endpoint(eid, folio_ids, folio_types, known_actors)
        for eid in (from_id, to_id)
    ]
    kinds = [k for k, _ in classified]

    new_vals: List[Optional[str]] = [None, None]
    for i, (kind, value) in enumerate(classified):
        if kind == "folio":
            new_vals[i] = store.resolve_alias(value)
        elif kind == "unresolved":
            new_vals[i] = value
            report.unresolved_refs += 1
            report.unresolved_distinct.add(value)
            if ":" in value:
                report.cross_project_refs += 1
            else:
                report.dangling_refs += 1
        # "actor" and "none" are decided below (none stays None).

    edge_actors = [
        (i, value) for i, (kind, value) in enumerate(classified) if kind == "actor"
    ]
    distinct_actors = list(dict.fromkeys(value for _, value in edge_actors))
    weaver = legacy_weaver

    if len(distinct_actors) >= 2:
        # Actor<->actor: keep both endpoints verbatim, fold nothing. The weaver
        # (if present) is a third identity and still fits its own slot.
        for i, value in edge_actors:
            new_vals[i] = value
        report.actor_to_actor_edges += 1
        report.actor_endpoints_kept += len(edge_actors)
    else:
        # Zero or one distinct actor across the edges: fold into the weaver slot
        # where lossless, keep verbatim only when the slot is already taken by a
        # different identity (so nothing is ever dropped).
        for i, value in edge_actors:
            if weaver is None:
                weaver = value
                report.actor_endpoints_to_weaver += 1
            elif weaver == value:
                report.actor_endpoints_to_weaver += 1
            else:
                new_vals[i] = value
                report.actor_endpoints_kept += 1

    return new_vals[0], new_vals[1], weaver, kinds


def import_project(
    legacy_db_path: Union[str, Path],
    sites_dir: Union[str, Path],
    store: SkeinStore,
) -> ImportReport:
    """Import one legacy SKEIN project into ``store``. Read-only on the source."""
    report = ImportReport(
        source_db=str(legacy_db_path), sites_dir=str(sites_dir)
    )
    conn = open_legacy(legacy_db_path)
    try:
        folio_ids: Set[str] = {
            r[0] for r in conn.execute("SELECT folio_id FROM folios")
        }
        folio_types = _folio_types(conn)
        # Every distinct weaver is unambiguously an actor; used to disambiguate a
        # folio-type-prefixed actor id from a real unresolved folio ref (FINDING 3).
        known_actors: Set[str] = {
            r[0] for r in conn.execute("SELECT DISTINCT weaver FROM threads")
            if r[0]
        }

        # Account for populated legacy columns the new model does not carry, so
        # the loss is visible rather than silent (FINDING 2).
        report.dropped_folio_columns = _populated_unaccounted_columns(
            conn, "folios", _ACCOUNTED_FOLIO_COLS
        )
        report.dropped_thread_columns = _populated_unaccounted_columns(
            conn, "threads", _ACCOUNTED_THREAD_COLS
        )
        if report.dropped_folio_columns or report.dropped_thread_columns:
            report.notes.append(
                "populated legacy columns above are not carried into the new "
                "model (the five canonical folio fields plus thread from/to/type/"
                "weaver/created_at/content are). Loss is counted, not silent; "
                "whether to carry these is a separate product call"
            )

        # --- folios -> hashes + aliases -------------------------------------
        # Defensive about schema drift (tome lacks columns speakbot has): select
        # only the canonical columns this legacy DB actually has. Required fields
        # (type/title/content/created_at/created_by) error at create_folio if truly
        # absent, as before; optional ones (status, metadata, site_id) just don't
        # appear in the row.
        present_folio_cols = {r[1] for r in conn.execute("PRAGMA table_info(folios)")}
        select_cols = [c for c in _FOLIO_COLS if c in present_folio_cols]
        seen_hashes: Set[str] = set()
        with store.transaction():
            for row in conn.execute(
                f"SELECT {','.join(select_cols)} FROM folios"
            ):
                content = row["content"]
                # Preserve the legacy metadata column by folding it into content.
                # The new model has no metadata column; the tender/agent envelopes
                # already embed structured fields in content, so this matches. The
                # dead questions_enabled flag is stripped (a signed-off no-op);
                # anything real is embedded losslessly under the legacy-meta marker.
                meta_raw = row["metadata"] if "metadata" in row.keys() else None
                meta = normalize_legacy_meta(meta_raw)
                if meta is not None:
                    content = render_legacy_meta(content, meta)
                    report.metadata_carried += 1
                else:
                    # Detect the flag-only case for the report. meta_raw is str|None
                    # from sqlite, but normalize tolerates an already-parsed dict, so
                    # mirror that here — json.loads(dict) raises TypeError, which the
                    # JSONDecodeError/ValueError guard would NOT catch.
                    if isinstance(meta_raw, dict):
                        raw_obj = meta_raw
                    elif isinstance(meta_raw, (str, bytes)):
                        try:
                            raw_obj = json.loads(meta_raw)
                        except (json.JSONDecodeError, ValueError):
                            raw_obj = None
                    else:
                        raw_obj = None
                    if isinstance(raw_obj, dict) and "questions_enabled" in raw_obj:
                        report.metadata_flag_dropped += 1
                fields = {
                    "type": row["type"],
                    "title": row["title"],
                    "content": content,
                    "created_at": row["created_at"],
                    "created_by": row["created_by"],
                }
                h = store.create_folio(fields)
                store.set_alias(row["folio_id"], h)
                report.folios_seen += 1
                report.folios_carried += 1
                if h in seen_hashes:
                    report.folio_hash_collisions += 1
                seen_hashes.add(h)

        # --- sites -> site folios + slugs -----------------------------------
        sites = _load_sites(Path(sites_dir))
        with store.transaction():
            for site in sites:
                slug = site.get("site_id")
                if not slug:
                    # A site JSON with no site_id can't be slugged or linked; count
                    # the drop so it is surfaced rather than vanishing silently.
                    report.sites_skipped_no_id += 1
                    continue
                site_hash = store.create_folio({
                    "type": "site",
                    "title": slug,
                    "content": site.get("purpose"),
                    "created_at": site.get("created_at"),
                    "created_by": site.get("created_by"),
                })
                store.set_slug(slug, site_hash)
                report.sites_seen += 1
                report.sites_carried += 1

        # --- threads --------------------------------------------------------
        legacy_per_hash: Dict[str, List[str]] = {}
        with store.transaction():
            for row in conn.execute(
                f"SELECT {','.join(_THREAD_COLS)} FROM threads"
            ):
                report.threads_seen += 1
                new_from, new_to, weaver, kinds = _resolve_thread_endpoints(
                    row["from_id"], row["to_id"], row["weaver"],
                    store, folio_ids, folio_types, known_actors, report,
                )
                ttype = row["type"]
                if ttype == "succession" and all(
                    k in ("folio", "unresolved") for k in kinds
                ):
                    ttype = "supersedes"
                    report.succession_renamed += 1
                th = store.save_thread(
                    from_id=new_from,
                    to_id=new_to,
                    type=ttype,
                    weaver=weaver,
                    created_at=row["created_at"],
                    content=row["content"],
                )
                legacy_per_hash.setdefault(th, []).append(row["thread_id"])

        report.threads_carried = len(legacy_per_hash)
        for h, ids in legacy_per_hash.items():
            if len(ids) > 1:
                report.threads_merged += len(ids) - 1
                if len(report.merge_examples) < 20:
                    report.merge_examples.append(f"{ids} -> {h}")

        # --- within (membership) threads ------------------------------------
        with store.transaction():
            for row in conn.execute("SELECT folio_id, site_id FROM folios"):
                site_id = row["site_id"]
                if not site_id:
                    continue
                report.folios_with_site_id += 1
                site_hash = store.resolve_slug(site_id)
                folio_hash = store.resolve_alias(row["folio_id"])
                if site_hash and folio_hash:
                    store.save_thread(
                        from_id=folio_hash, to_id=site_hash, type="within",
                    )
                    report.within_threads += 1
                else:
                    report.within_unresolved += 1
                    if site_hash is None:
                        report.notes.append(
                            f"folio {row['folio_id']} references site "
                            f"{site_id!r} with no metadata JSON; within edge skipped"
                        )

        # --- status carried-as-threads audit (no silent loss) ---------------
        status_thread_froms: Set[str] = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT from_id FROM threads WHERE type='status'"
            )
        }
        for row in conn.execute("SELECT folio_id, status FROM folios"):
            if row["status"] not in _OPEN_STATUSES and \
                    row["folio_id"] not in status_thread_froms:
                report.status_without_thread += 1
                if len(report.status_examples) < 20:
                    report.status_examples.append(
                        f"{row['folio_id']} status={row['status']!r}"
                    )
        if report.status_without_thread:
            report.notes.append(
                "some non-open folios carry status only in the folios.status "
                "column with no status thread; skein omits that column "
                "(status is thread-derived) so this state is not migrated — "
                "see status-only examples above"
            )
    finally:
        conn.close()
    return report
