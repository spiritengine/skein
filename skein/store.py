"""Content-hash-native SQLite store for skein (Slice 1).

A folio's identity *is* its content hash — there is no human folio_id. Threads
and aliases hang off the same content-addressed model. This module owns storage
and querying only; it does not classify thread endpoints (folio-hash vs actor id)
— that is the import bridge's job in Slice 2.

Data lives under ``.skein/``: the content store is ``.skein/store.db``. Any
legacy ``.skein/data/`` tree is only ever read by ``skein import`` and is never
written here. The schema deliberately omits the legacy ``status``/``assigned_to``
folio columns — skein is thread-native from the start.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple, Union

from .identity import compute_folio_hash, compute_thread_hash, normalize_created_at


def _now_iso() -> str:
    """A timezone-aware UTC isoformat stamp for first-insert timestamps."""
    return datetime.now(timezone.utc).isoformat()


def _iso_micros(dt: datetime) -> str:
    """A FIXED-WIDTH UTC isoformat stamp (always 6 fractional digits, +00:00).

    Used for invite ``expires_at`` and the ``now`` it is compared against in the
    redeem CAS. ``datetime.isoformat()`` omits the fraction when microsecond==0,
    giving variable-width strings whose lexicographic order can disagree with
    chronological order across the fraction/no-fraction boundary; pinning
    ``timespec='microseconds'`` makes every stamp the same width so the SQL
    ``expires_at > ?`` inequality is a correct string compare."""
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _now_micros() -> str:
    """Fixed-width UTC now() — the redeem-CAS / expiry comparison clock."""
    return _iso_micros(datetime.now(timezone.utc))


# sqlite3.SQLITE_BUSY / SQLITE_LOCKED module constants exist only on Python
# >= 3.11; fall back to the stable SQLite primary result codes on 3.10
# (requires-python is >=3.10).
_SQLITE_BUSY = getattr(sqlite3, "SQLITE_BUSY", 5)
_SQLITE_LOCKED = getattr(sqlite3, "SQLITE_LOCKED", 6)


def sqlite_error_is_lock(e: sqlite3.OperationalError) -> bool:
    """Whether an OperationalError is write-lock contention (SQLITE_BUSY/LOCKED).

    Discriminate on the numeric SQLite result code (the robust signal): mask the
    EXTENDED ``sqlite_errorcode`` (Python >= 3.11; absent on a hand-built exception)
    to the primary byte so lock-family extended codes count too, and fall back to
    the (stable) message text only when there is no code at all. Used by the ingress
    publish + redeem routes to degrade a transient lock to a retryable 503 while a
    genuine (non-lock) fault still surfaces."""
    code = getattr(e, "sqlite_errorcode", None)
    primary = (code & 0xFF) if isinstance(code, int) else None
    return primary in (_SQLITE_BUSY, _SQLITE_LOCKED) or (
        primary is None and ("lock" in str(e).lower() or "busy" in str(e).lower())
    )


def bundle_hash_for(bundle_json: str) -> str:
    """The verify_cache bundle key: sha256 over a manifest's stored bundle_json.

    ONE shared helper for the ingress WRITER and the read READER, so they compute
    the IDENTICAL key over the same ``manifests.bundle_json`` bytes and can never
    disagree (VC11).

    ``bundle_json`` is NOT NULL in the schema, so ``None`` is unreachable today;
    the explicit ``TypeError`` keeps a future caller from getting an opaque
    ``AttributeError`` off ``None.encode`` at this sharp API edge."""
    import hashlib

    if bundle_json is None:
        raise TypeError("bundle_hash_for requires a bundle_json string, got None")
    return hashlib.sha256(bundle_json.encode("utf-8")).hexdigest()


# verify_multi statuses meaning "could not check" — never cached, must re-verify.
_RECOVERABLE_VERIFY_STATUSES = frozenset({"TRUST_ROOT_STALE", "OFFLINE_NO_TRUSTED_ROOT"})

DEFAULT_DATA_DIR = Path(".skein")
DB_FILENAME = "store.db"
# How long a write waits for a held lock before giving up (SQLite default is 0 =
# fail instantly). 5s lets concurrent ingress writers serialize gracefully under
# the public write surface; nginx rate-limiting bounds how many can pile up.
BUSY_TIMEOUT_MS = 5000


def _like_escape(s: str) -> str:
    """Escape a literal string for use inside a ``LIKE ... ESCAPE '\\'`` pattern.

    The backslash is escaped first so the later ``%``/``_`` escapes are not
    doubled. Callers add their own ``%`` wildcards around the result.
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Relevance weights for search ranking (L1): a query term found in the title is
# worth more than one found in the body.
_TITLE_WEIGHT = 3
_BODY_WEIGHT = 1

# Cap on distinct query terms. `?q=` is unauthenticated on a mesh-facing surface;
# an unbounded term list drives O(terms x candidates x content) substring scans
# and, on SQLite < 3.32 (999-variable limit), a >499-term query would raise. 32
# terms is far more than any real query and bounds both the work and the binds.
_MAX_SEARCH_TERMS = 32


def _search_score(row: Mapping[str, Any], terms: List[str]) -> int:
    """Relevance of a folio for the search terms: title hits weigh over body hits."""
    title = (row.get("title") or "").lower()
    content = (row.get("content") or "").lower()
    score = 0
    for term in terms:
        t = term.lower()
        if t in title:
            score += _TITLE_WEIGHT
        if t in content:
            score += _BODY_WEIGHT
    return score


def make_snippet(content: Optional[str], terms: List[str], width: int = 180) -> Optional[str]:
    """A short excerpt of ``content`` around the first matched term (legibility).

    Untrusted display text — it goes inside the agent-markdown fence and is HTML-
    escaped in the web view, never trusted. Returns ``None`` for empty content.
    """
    if not content:
        return None
    flat = " ".join(content.split())  # collapse whitespace for a clean one-liner
    low = flat.lower()
    positions = [low.find(t.lower()) for t in terms]
    hits = [p for p in positions if p >= 0]
    if not hits:
        head = flat[:width]
        return head + ("…" if len(flat) > width else "")
    start = max(0, min(hits) - width // 3)
    end = min(len(flat), start + width)
    snippet = flat[start:end]
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(flat) else "")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS folios (
    content_hash TEXT PRIMARY KEY,
    type         TEXT,
    created_at   TEXT,
    created_by   TEXT,
    title        TEXT,
    content      TEXT
);

CREATE TABLE IF NOT EXISTS threads (
    thread_hash TEXT PRIMARY KEY,
    from_id     TEXT,
    to_id       TEXT,
    type        TEXT,
    weaver      TEXT,
    created_at  TEXT,
    content     TEXT
);

CREATE TABLE IF NOT EXISTS aliases (
    legacy_id    TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slugs (
    slug         TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL
);

-- Manifest store (the unified signing model). One row per (manifest, signer): a
-- publish signs ONE Merkle-root descriptor over EVERY constituent's content hash
-- (folios AND threads). The (root, issuer, subject) TRIPLE PK lets two distinct
-- bound signers each retain a proof over the identical constituent set (ST3 / the
-- rev-5 B50 property); a bare-root PK would have shadowed the second.
CREATE TABLE IF NOT EXISTS manifests (
    root            TEXT,          -- the Merkle root ("sha256::<hex>"), signed anchor
    manifest_hash   TEXT,          -- content address of the signed descriptor (lookup key)
    descriptor_json TEXT NOT NULL, -- the canonical descriptor (root + leaf_count)
    leaf_list_json  TEXT NOT NULL, -- the full sorted, deduped constituent hash list
    bundle_json     TEXT NOT NULL, -- the single Sigstore bundle over the descriptor
    issuer          TEXT,          -- verified manifest signer
    subject         TEXT,
    leaf_count      INTEGER,
    created_at      TEXT,          -- IMMUTABLE first-insert timestamp
    PRIMARY KEY (root, issuer, subject)
);

-- Unified constituent attribution: one row per covered constituent (a folio
-- content_hash OR a thread_hash), pointing at its FIRST covering manifest
-- (first-manifest-wins, Q3). The FK references the full proof triple, so the
-- identity a constituent denormalizes is exactly the proof it resolves to
-- (proof-signer == display-signer, ST6).
CREATE TABLE IF NOT EXISTS constituent_attribution (
    constituent_hash TEXT PRIMARY KEY, -- a folio content_hash OR a thread_hash
    kind             TEXT NOT NULL,    -- 'folio' | 'thread' (triage/display)
    root             TEXT NOT NULL,
    issuer           TEXT NOT NULL,    -- denormalized manifest signer (display-authoritative)
    subject          TEXT NOT NULL,
    created_at       TEXT,
    FOREIGN KEY (root, issuer, subject) REFERENCES manifests(root, issuer, subject)
);

-- Account bindings (the per-instance authorization sidecar). A (issuer, subject)
-- is authorized as 'operator' or 'author'; revoked_at NULL = active. created_at
-- is set ONCE and preserved across revoke/reactivate (B5). The single-active-
-- operator invariant is LOGIC-enforced (bootstrap refuses a second; rotate revokes
-- the old in the same transaction), not a DB constraint — get_operator() resolves
-- deterministically even under a corrupt two-operator state (B47).
CREATE TABLE IF NOT EXISTS account_bindings (
    issuer            TEXT,
    subject           TEXT,
    role              TEXT,   -- 'operator' | 'author'
    vouched_by_issuer  TEXT,
    vouched_by_subject TEXT,
    created_at        TEXT,
    revoked_at        TEXT,   -- NULL = active
    PRIMARY KEY (issuer, subject)
);

-- Append-only binding audit: every lifecycle transition writes one event row in
-- the same transaction as the mutation. Rows are INSERTed, never UPDATEd/DELETEd.
CREATE TABLE IF NOT EXISTS binding_events (
    event_seq          INTEGER PRIMARY KEY,  -- rowid; monotonic insertion order
    issuer             TEXT,
    subject            TEXT,
    event              TEXT,  -- created|revoked|reactivated|rotated_in|rotated_out|promoted
    role               TEXT,
    vouched_by_issuer  TEXT,
    vouched_by_subject TEXT,
    at                 TEXT
);

-- One-time invite tokens (the agent-mediated collaborator onboarding ceremony).
-- token_hash is the SHA-256 of a >=256-bit CSPRNG token; the PLAINTEXT token is
-- NEVER stored. Expiry is enforced in SQL (the CAS WHERE compares expires_at), not
-- post-fetch. used_at NULL = unredeemed; redeeming BURNS it (sets used_at) and
-- records the bound (issuer, subject) + redeemed_at, so a redemption is operator-
-- visible (INV-4) and an exactly-once burn is a conditional UPDATE...WHERE rowcount
-- check (INV-2). revoked_at NULL = active; an operator can revoke an unredeemed
-- invite. failed_attempts + attempts_window_start are the per-token expensive-
-- verify-flood backstop (INV-5): a leaked valid-but-unused token is not burned
-- until a SUCCESSFUL redeem, so repeated bogus proofs against it are capped.
-- expires_at is stored fixed-width (6 fractional digits, +00:00) so the SQL
-- inequality in the CAS is a chronologically-correct lexicographic compare.
CREATE TABLE IF NOT EXISTS invites (
    token_hash            TEXT PRIMARY KEY,
    role                  TEXT NOT NULL,
    created_at            TEXT,
    expires_at            TEXT NOT NULL,
    used_at               TEXT,
    revoked_at            TEXT,
    vouched_by_issuer     TEXT,
    vouched_by_subject    TEXT,
    bound_issuer          TEXT,
    bound_subject         TEXT,
    redeemed_at           TEXT,
    note                  TEXT,
    failed_attempts       INTEGER NOT NULL DEFAULT 0,
    attempts_window_start TEXT
);

-- Append-only invite audit (mirrors binding_events): minted / redeemed (with the
-- bound identity) / revoked / redeem_failed. One row per lifecycle event, written
-- in the same transaction as the mutation.
CREATE TABLE IF NOT EXISTS invite_events (
    event_seq    INTEGER PRIMARY KEY,  -- rowid; monotonic insertion order
    token_hash   TEXT,
    event        TEXT,  -- minted|redeemed|revoked|redeem_failed
    bound_issuer TEXT,
    bound_subject TEXT,
    at           TEXT,
    detail       TEXT   -- free-text (e.g. the reject reason on a redeem_failed)
);

-- Manifest signature-verdict cache. Keyed on (manifest_hash, bundle_hash); stores
-- ONLY the expensive Sigstore SIGNATURE verdict (step 3 of the four-step read
-- verdict). Membership (step 2) and binding (step 4) are recomputed LIVE per read
-- and are deliberately OUT of this cache. The INGRESS is the sole writer; the read
-- app opens mode=ro and never writes. A recoverable status (TRUST_ROOT_STALE /
-- OFFLINE_NO_TRUSTED_ROOT) is NEVER cached (it must re-verify).
CREATE TABLE IF NOT EXISTS verify_cache (
    manifest_hash TEXT,
    bundle_hash   TEXT,
    status        TEXT,
    issuer        TEXT,
    subject       TEXT,
    verified_at   TEXT,
    PRIMARY KEY (manifest_hash, bundle_hash)
);

-- Chain yields ("sacks"): the mutable hand-off state a chain task leaves for the
-- next (the agent-coordination port, D4). A yield is NOT content-addressed — it is
-- fast-changing chain bookkeeping (enriched after the fact with duration/tokens),
-- the wrong fit for the immutable folio store — so it lives in this sidecar exactly
-- as legacy did (legacy skein/storage.py sacks table), same posture as invites /
-- account_bindings. ``sack_id`` is the human yield id ('sack-YYYYMMDD-xxxx', UNIQUE);
-- the autoincrement ``id`` gives a stable insertion order so same-timestamp yields
-- read back deterministically. ``artifacts``/``metadata`` are JSON-encoded text.
CREATE TABLE IF NOT EXISTS sacks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    sack_id          TEXT UNIQUE NOT NULL,
    chain_id         TEXT NOT NULL,
    task_id          TEXT NOT NULL,
    agent_id         TEXT,
    timestamp        TEXT,
    status           TEXT,
    outcome          TEXT,
    artifacts        TEXT,   -- JSON list of artifact ids
    notes            TEXT,
    duration_seconds INTEGER,
    tokens_used      INTEGER,
    shard_path       TEXT,
    tender_id        TEXT,
    metadata         TEXT    -- JSON catchall
);

CREATE INDEX IF NOT EXISTS idx_sacks_chain  ON sacks(chain_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_sacks_agent  ON sacks(agent_id);
CREATE INDEX IF NOT EXISTS idx_sacks_status ON sacks(status);

CREATE INDEX IF NOT EXISTS idx_manifests_hash ON manifests(manifest_hash);

CREATE INDEX IF NOT EXISTS idx_threads_from ON threads(from_id);
CREATE INDEX IF NOT EXISTS idx_threads_to   ON threads(to_id);
CREATE INDEX IF NOT EXISTS idx_threads_type ON threads(type);
CREATE INDEX IF NOT EXISTS idx_folios_created_at ON folios(created_at);
CREATE INDEX IF NOT EXISTS idx_folios_created_by ON folios(created_by);
CREATE INDEX IF NOT EXISTS idx_folios_type ON folios(type);
"""


class SkeinStore:
    """Content-hash-native folio/thread/alias store backed by SQLite.

    **Single-threaded per instance (not internally synchronized).** A store wraps
    one SQLite connection and a bare ``_in_batch`` transaction flag with no lock,
    so a single ``SkeinStore`` MUST NOT be shared across threads — concurrent
    use could interleave ``transaction()`` toggles and commit one caller's writes
    outside another's transaction (zr29 MEDIUM #7). The supported concurrency model
    is a connection (and store) per thread/request: the web surface opens one store
    per request (``check_same_thread=False``, used serially within that request),
    and the CLI/bridge are single-threaded. Do not hand one instance to a pool.
    """

    def __init__(
        self,
        data_dir: Optional[Union[str, Path]] = None,
        check_same_thread: bool = True,
        read_only: bool = False,
    ):
        """Open the store under ``data_dir`` (creating it unless ``read_only``).

        ``check_same_thread`` defaults to SQLite's safe single-thread mode, which
        suits the import bridge and CLI. The web server (Slice 4) opens one store
        per request and may touch its connection from a different threadpool
        thread than the one that created it, so it passes ``check_same_thread=
        False``. That is safe here because a request uses its connection serially
        (no concurrent access to the same connection); a separate connection per
        request keeps writers/readers isolated.

        ``read_only`` opens the store without ever writing it — no ``mkdir``, no
        schema DDL, no commit — for serving a corpus on a read-only mount (the
        web read surface / containerized instance). Without it the normal open
        path runs ``CREATE ... IF NOT EXISTS`` on connect, which only happens to
        succeed on a read-only mount by luck (no-op DDL on an already-complete,
        non-WAL store) and would 500 the moment the schema drifts or the store is
        WAL-mode. The store must already exist when ``read_only`` is set.
        """
        self.data_dir = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
        self.db_path = self.data_dir / DB_FILENAME
        self.read_only = read_only
        if read_only:
            self.conn = self._connect_read_only(check_same_thread)
        else:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.db_path), check_same_thread=check_same_thread)
            self.conn.row_factory = sqlite3.Row
            # Concurrent writers (the ingress opens a connection per request, run
            # in a threadpool) would otherwise get an INSTANT 'database is locked'
            # the moment two writes overlap — SQLite's default busy_timeout is 0.
            # On the public write surface that silently dropped valid concurrent
            # publishes AND mislabeled them 'invalid fields' (a transient lock is
            # not malformed input). A non-zero busy_timeout makes writers WAIT for
            # the lock and serialize gracefully (the intended single-writer
            # posture) instead of failing. WAL is not an option here — the read
            # surface mounts the corpus :ro (rollback-journal only); a busy_timeout
            # serializes correctly under rollback-journal.
            self.conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            # foreign_keys MUST be enabled at CONNECTION OPEN, OUTSIDE any
            # transaction (inside one it is a silent no-op), and BEFORE the schema
            # DDL — so the constituent_attribution FK is enforced from the first
            # write (ST7). A dormant-FK store would pass FK-dependent cells by mere
            # absence-of-raise.
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(_SCHEMA)
            self.conn.commit()
        self._in_batch = False
        self._sp_counter = 0

    def _connect_read_only(self, check_same_thread: bool) -> sqlite3.Connection:
        """Open the store read-only, never writing the corpus. Tries ``mode=ro``
        (WAL-aware) then ``immutable=1`` (a read-only filesystem where SQLite
        can't create its sidecar files), mirroring ``bridge.open_legacy``."""
        p = str(self.db_path)
        last_err: Optional[Exception] = None
        for uri in (f"file:{p}?mode=ro", f"file:{p}?immutable=1"):
            conn: Optional[sqlite3.Connection] = None
            try:
                conn = sqlite3.connect(uri, uri=True, check_same_thread=check_same_thread)
                conn.row_factory = sqlite3.Row
                conn.execute("SELECT 1 FROM folios LIMIT 1")  # resolve the open
                return conn
            except sqlite3.OperationalError as e:
                last_err = e
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        raise sqlite3.OperationalError(
            f"could not open {p} read-only (mode=ro or immutable=1): {last_err}"
        )

    def close(self) -> None:
        self.conn.close()

    # --- transactions -------------------------------------------------------

    def _maybe_commit(self) -> None:
        """Commit immediately unless inside a batch transaction."""
        if not self._in_batch:
            self.conn.commit()

    @contextmanager
    def transaction(self) -> Iterator["SkeinStore"]:
        """Batch many writes into a single commit.

        Per-write commits make a 10k-row import I/O-bound (one fsync per row).
        Inside this context, ``create_folio``/``save_thread``/``set_alias``/
        ``set_slug`` defer their commit; the whole batch commits once on exit,
        or rolls back if the block raises. Not re-entrant.
        """
        if self._in_batch:
            raise RuntimeError("transaction() is not re-entrant")
        # BEGIN IMMEDIATE grabs the RESERVED (write) lock up front. A batch reads
        # before it writes (the ingress does get_folio() then create_folio()); with
        # a DEFERRED transaction two connections both take the SHARED read lock and
        # then deadlock trying to upgrade to RESERVED — SQLite returns 'database is
        # locked' INSTANTLY, NOT honoring busy_timeout (the busy handler is skipped
        # on a would-be deadlock). IMMEDIATE means writers queue on the write lock
        # from the start, so busy_timeout actually serializes them. This is what
        # makes concurrent ingress publishes commit instead of being dropped (and
        # mislabeled 'invalid fields'). Read-only stores never take this path.
        #
        # The BEGIN runs BEFORE _in_batch is set: if the lock can't be taken within
        # busy_timeout it raises OperationalError and the store stays clean (no
        # wedged _in_batch flag that would skip later commits / falsely trip the
        # not-re-entrant guard). No transaction is open on a failed BEGIN.
        self.conn.execute("BEGIN IMMEDIATE")
        self._in_batch = True
        try:
            yield self
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()
        finally:
            self._in_batch = False

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        """A nested SAVEPOINT for per-item isolation inside a batch transaction.

        A block that raises rolls back ONLY this savepoint (its writes), then
        re-raises — so the ingress can catch the exception, record a reject for
        that one item, and let its siblings commit (RS13). Cheap and re-entrant
        (each call gets a unique savepoint name)."""
        self._sp_counter += 1
        name = f"sp_{self._sp_counter}"
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self.conn.execute(f"ROLLBACK TO {name}")
            self.conn.execute(f"RELEASE {name}")
            raise
        else:
            self.conn.execute(f"RELEASE {name}")

    def __enter__(self) -> "SkeinStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- folios -------------------------------------------------------------

    def create_folio(self, fields: Mapping[str, Any]) -> str:
        """Normalize, hash, and idempotently insert a folio. Returns its hash.

        Inserting the same logical folio twice (including the same instant under
        different created_at encodings) yields one row and the same hash.
        """
        content_hash = compute_folio_hash(fields)
        normalized_created_at = normalize_created_at(fields.get("created_at"))
        self.conn.execute(
            """
            INSERT OR IGNORE INTO folios
                (content_hash, type, created_at, created_by, title, content)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                content_hash,
                fields.get("type"),
                normalized_created_at,
                fields.get("created_by"),
                fields.get("title"),
                fields.get("content"),
            ),
        )
        self._maybe_commit()
        return content_hash

    def get_folio(self, content_hash: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM folios WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return dict(row) if row else None

    def list_folios(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM folios
            ORDER BY created_at, content_hash
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def folios_by_type(
        self, type: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """All folios of one ``type``, ordered oldest-first by (created_at, hash).

        The faithful local equivalent of legacy ``GET /folios?type=<type>`` — a
        station-wide query by type, not scoped to a site. Ordered ascending so a
        caller selecting "the newest per key" gets it as the last write wins;
        ``content_hash`` breaks created_at ties deterministically.
        """
        sql = "SELECT * FROM folios WHERE type = ? ORDER BY created_at, content_hash"
        params: List[Any] = [type]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def folios_by_created_by(
        self, created_by: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """All folios authored by ``created_by``, ordered oldest-first.

        The store half of the activity feed: legacy computes a roster agent's
        activity by filtering every folio on ``created_by == agent_id`` in Python
        (legacy routes.py:281 / :300-316). This is that join pushed into SQL — the
        agent-id is the same env string the agent folio carries as ``created_by``,
        so an agent's authored work and its roster entry share one key. Ordered
        ascending; the caller takes the newest for "last activity".
        """
        sql = "SELECT * FROM folios WHERE created_by = ? ORDER BY created_at, content_hash"
        params: List[Any] = [created_by]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def search_folios(self, query: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Folios matching ``query``, AND-of-terms, ranked title-over-body (L1).

        The query is split on whitespace into terms; a folio matches only if EVERY
        term appears (case-insensitively) in its title or content — so a
        multi-word search is an AND of its words, not one exact-phrase substring
        (brief-20260522-4yt4 Layer 1). Results are relevance-ranked in Python: a
        term in the title weighs more than one in the body, recency breaks ties.
        Each term is matched literally; SQL ``LIKE`` wildcards are escaped, so
        ``50%`` or ``a_b`` find those strings. (Not FTS5/BM25 — the new store has
        no full-text index; this is the modest L1 polish over substring matching.)
        """
        terms = [t for t in query.split() if t][:_MAX_SEARCH_TERMS]
        if not terms:
            return []
        clause = " AND ".join(
            ["(title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')"] * len(terms)
        )
        params: List[Any] = []
        for term in terms:
            like = "%" + _like_escape(term) + "%"
            params.extend([like, like])
        # Pull a generous recency-ordered candidate set, then rank in Python. The
        # candidate cap keeps ranking bounded; recency ordering becomes the tie
        # break (a stable sort preserves it within equal relevance scores).
        params.append(max(limit * 5, 200))
        rows = self.conn.execute(
            f"""
            SELECT * FROM folios
            WHERE {clause}
            ORDER BY created_at DESC, content_hash
            LIMIT ?
            """,
            params,
        ).fetchall()
        ranked = sorted(
            (dict(r) for r in rows),
            key=lambda r: _search_score(r, terms),
            reverse=True,
        )
        return ranked[:limit]

    def find_by_prefix(self, prefix: str, limit: int = 10) -> List[str]:
        """Content hashes beginning with ``prefix`` (git-style short-hash lookup).

        ``prefix`` is matched literally against the full stored ``sha256::<hex>``
        address; ``LIKE`` metacharacters are escaped. Returns up to ``limit``
        matches so a caller can detect (and reject) an ambiguous prefix.
        """
        like = _like_escape(prefix) + "%"
        rows = self.conn.execute(
            """
            SELECT content_hash FROM folios
            WHERE content_hash LIKE ? ESCAPE '\\'
            ORDER BY content_hash
            LIMIT ?
            """,
            (like, limit),
        ).fetchall()
        return [r["content_hash"] for r in rows]

    def folios_in_site(
        self,
        site_hash: str,
        type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Member folios of a site, joined through ``within`` threads.

        One SQL join (``threads.from_id = folios.content_hash`` where the
        ``within`` edge points ``to_id = site_hash``) ordered by the *folio's*
        ``created_at`` then hash, so ``limit`` returns a stable earliest-N window
        — not the arbitrary slice that limiting the unordered edge list would
        give (``within`` edges carry no timestamp, so they have no meaningful
        order of their own). ``DISTINCT`` guards against a folio somehow holding
        two membership edges to the same site.
        """
        sql = [
            "SELECT DISTINCT f.* FROM folios f",
            "JOIN threads t ON t.from_id = f.content_hash",
            "WHERE t.to_id = ? AND t.type = 'within'",
        ]
        params: List[Any] = [site_hash]
        if type is not None:
            sql.append("AND f.type = ?")
            params.append(type)
        sql.append("ORDER BY f.created_at, f.content_hash")
        if limit is not None:
            sql.append("LIMIT ?")
            params.append(limit)
        rows = self.conn.execute("\n".join(sql), params).fetchall()
        return [dict(r) for r in rows]

    def folio_site_slug(self, content_hash: str) -> Optional[str]:
        """The site slug of a single member folio (alphabetically-first if many).

        Indexed single-row lookup via ``idx_threads_from`` — for the folio-detail
        hot path, where building the whole corpus map (``folio_site_slugs``) just
        to read one entry is a full scan that grows with the corpus.
        """
        row = self.conn.execute(
            """
            SELECT s.slug AS slug
            FROM threads t
            JOIN slugs s ON s.content_hash = t.to_id
            WHERE t.type = 'within' AND t.from_id = ?
            ORDER BY s.slug
            LIMIT 1
            """,
            (content_hash,),
        ).fetchone()
        return row["slug"] if row else None

    def folio_site_slugs(self, content_hashes: Optional[List[str]] = None) -> Dict[str, str]:
        """Map member folio content hashes to their site slugs.

        With ``content_hashes`` given, the join is restricted to those folios (a
        chunked IN-list) — for labelling a bounded result set such as search hits.
        With ``None``, the whole corpus is mapped in one join (the web index, which
        labels every folio). If a folio belongs to more than one site (unusual),
        the alphabetically-first slug wins so the result is deterministic.
        """
        mapping: Dict[str, str] = {}
        if content_hashes is None:
            rows = self.conn.execute(
                """
                SELECT t.from_id AS folio, s.slug AS slug
                FROM threads t
                JOIN slugs s ON s.content_hash = t.to_id
                WHERE t.type = 'within'
                ORDER BY s.slug
                """
            ).fetchall()
            for r in rows:
                mapping.setdefault(r["folio"], r["slug"])
            return mapping
        hashes = list(content_hashes)
        for i in range(0, len(hashes), 900):
            chunk = hashes[i : i + 900]
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"""
                SELECT t.from_id AS folio, s.slug AS slug
                FROM threads t
                JOIN slugs s ON s.content_hash = t.to_id
                WHERE t.type = 'within' AND t.from_id IN ({placeholders})
                ORDER BY s.slug
                """,
                chunk,
            ).fetchall()
            for r in rows:
                mapping.setdefault(r["folio"], r["slug"])
        return mapping

    def latest_statuses(self, folio_hashes: List[str]) -> Dict[str, str]:
        """Map each given folio hash to its most recent status-thread content.

        Status is thread-derived (there is no status column): a ``type=status``
        thread points ``to_id`` at the folio, and the latest one by ``created_at``
        wins. One batched query instead of one per folio. Folios with no status
        thread are simply absent from the result.
        """
        out: Dict[str, str] = {}
        # Chunk the IN-list to stay well under SQLITE_MAX_VARIABLE_NUMBER (as low
        # as 999 on older SQLite); a busy project can have far more folios.
        hashes = list(folio_hashes)
        for i in range(0, len(hashes), 900):
            chunk = hashes[i : i + 900]
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"""
                SELECT to_id, content FROM threads
                WHERE type = 'status' AND to_id IN ({placeholders})
                ORDER BY created_at, thread_hash
                """,
                chunk,
            ).fetchall()
            # Ascending (created_at, thread_hash) → the last row written for each
            # folio is the newest; thread_hash breaks ties deterministically when
            # two statuses share a normalized created_at (the bridge collapses
            # whole-second legacy timestamps), so the winner can't flip per query.
            for r in rows:
                out[r["to_id"]] = r["content"]
        return out

    # --- threads ------------------------------------------------------------

    def save_thread(
        self,
        from_id: Optional[str] = None,
        to_id: Optional[str] = None,
        type: Optional[str] = None,
        weaver: Optional[str] = None,
        created_at: Any = None,
        content: Optional[str] = None,
    ) -> str:
        """Idempotently store a thread edge. Returns its content hash.

        ``from_id``/``to_id`` may hold a folio content-hash or an actor/external
        id; the store does not classify them.
        """
        thread_hash = compute_thread_hash(from_id, to_id, type, weaver, created_at, content)
        self.conn.execute(
            """
            INSERT OR IGNORE INTO threads
                (thread_hash, from_id, to_id, type, weaver, created_at, content)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_hash,
                from_id,
                to_id,
                type,
                weaver,
                normalize_created_at(created_at),
                content,
            ),
        )
        self._maybe_commit()
        return thread_hash

    def get_thread(self, thread_hash: str) -> Optional[Dict[str, Any]]:
        """Read one thread by its content hash (symmetric with get_folio)."""
        row = self.conn.execute(
            "SELECT * FROM threads WHERE thread_hash = ?", (thread_hash,)
        ).fetchone()
        return dict(row) if row else None

    def get_threads(
        self,
        from_id: Optional[str] = None,
        to_id: Optional[str] = None,
        type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Query threads by any combination of from_id, to_id, and type."""
        clauses = []
        params: List[Any] = []
        if from_id is not None:
            clauses.append("from_id = ?")
            params.append(from_id)
        if to_id is not None:
            clauses.append("to_id = ?")
            params.append(to_id)
        if type is not None:
            clauses.append("type = ?")
            params.append(type)

        sql = "SELECT * FROM threads"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, thread_hash"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # --- aliases ------------------------------------------------------------

    def set_alias(self, legacy_id: str, content_hash: str) -> None:
        """Map a legacy id to a content hash (upsert; last write wins)."""
        self.conn.execute(
            """
            INSERT INTO aliases (legacy_id, content_hash)
            VALUES (?, ?)
            ON CONFLICT(legacy_id) DO UPDATE SET content_hash = excluded.content_hash
            """,
            (legacy_id, content_hash),
        )
        self._maybe_commit()

    def resolve_alias(self, legacy_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT content_hash FROM aliases WHERE legacy_id = ?", (legacy_id,)
        ).fetchone()
        return row["content_hash"] if row else None

    # --- slugs --------------------------------------------------------------

    def set_slug(self, slug: str, content_hash: str) -> None:
        """Map a slug to a content hash (upsert, LAST-WRITE-WINS).

        This silently rebinds an existing slug to a new hash. It is the right
        tool when the caller OWNS the rebind (a site re-mint, an agent's own
        re-ignite succession). It is the WRONG tool for registering a name that
        must not collide with another holder — that needs :meth:`register_slug`,
        whose conditional insert REJECTS a steal instead of overwriting it.
        """
        self.conn.execute(
            """
            INSERT INTO slugs (slug, content_hash)
            VALUES (?, ?)
            ON CONFLICT(slug) DO UPDATE SET content_hash = excluded.content_hash
            """,
            (slug, content_hash),
        )
        self._maybe_commit()

    def _register_slug_locked(self, slug: str, content_hash: str) -> bool:
        """The guarded-bind CAS, assuming the caller already holds the write lock.

        Conditional ``INSERT OR IGNORE`` + rowcount, the same exactly-once shape as
        :meth:`redeem_invite_cas`: if the slug row is absent the INSERT lands
        (rowcount 1) and we own it; if it already exists the INSERT is ignored
        (rowcount 0) and we own it only when it already points at ``content_hash``
        (idempotent re-register). A slug held by a DIFFERENT hash returns False —
        the caller rejects the collision; this never silently rebinds (that is
        :meth:`set_slug`'s job). Lock-free so it can compose inside a larger
        roster ``transaction()`` (e.g. mint-folio + bind-name + set-status as one
        atomic write); :meth:`register_slug` is the standalone wrapper.
        """
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO slugs (slug, content_hash) VALUES (?, ?)",
            (slug, content_hash),
        )
        if cur.rowcount == 1:
            return True
        return self.resolve_slug(slug) == content_hash

    def register_slug(self, slug: str, content_hash: str) -> bool:
        """Guarded slug bind: claim ``slug`` for ``content_hash`` or refuse a steal.

        Returns True if the slug now resolves to ``content_hash`` (freshly bound,
        or already bound to it — idempotent), False if the slug is held by a
        DIFFERENT hash. Unlike :meth:`set_slug` (last-write-wins) this NEVER
        overwrites another holder — the new-code name guard the port needs so a
        second live agent cannot silently steal an in-use name (design D1). The
        read-modify-write runs under ``BEGIN IMMEDIATE`` so concurrent registers
        serialize on the single writer rather than racing the rowcount check.
        """
        with self.transaction():
            return self._register_slug_locked(slug, content_hash)

    def resolve_slug(self, slug: str) -> Optional[str]:
        row = self.conn.execute("SELECT content_hash FROM slugs WHERE slug = ?", (slug,)).fetchone()
        return row["content_hash"] if row else None

    def list_slugs(self) -> List[Tuple[str, str]]:
        """All ``(slug, content_hash)`` pairs, ordered by slug."""
        rows = self.conn.execute("SELECT slug, content_hash FROM slugs ORDER BY slug").fetchall()
        return [(r["slug"], r["content_hash"]) for r in rows]

    # --- manifest store + constituent attribution (the unified signing model) --

    def add_manifest(
        self,
        root: str,
        manifest_hash: str,
        descriptor_json: str,
        leaf_list_json: str,
        bundle_json: str,
        issuer: str,
        subject: str,
        leaf_count: int,
    ) -> None:
        """Record a manifest proof. INSERT OR IGNORE on the (root, issuer, subject)
        triple, so the same signer re-publishing the same set is idempotent and
        ``created_at`` is preserved (ST2/ST9); two distinct signers over one set
        each retain their proof (ST3)."""
        self.conn.execute(
            """
            INSERT OR IGNORE INTO manifests
                (root, manifest_hash, descriptor_json, leaf_list_json, bundle_json,
                 issuer, subject, leaf_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                root,
                manifest_hash,
                descriptor_json,
                leaf_list_json,
                bundle_json,
                issuer,
                subject,
                leaf_count,
                _now_iso(),
            ),
        )
        self._maybe_commit()

    def get_manifest_proof(
        self, root: str, issuer: str, subject: str
    ) -> Optional[Dict[str, Any]]:
        """The single manifest proof row for a (root, issuer, subject) triple."""
        row = self.conn.execute(
            "SELECT * FROM manifests WHERE root = ? AND issuer = ? AND subject = ?",
            (root, issuer, subject),
        ).fetchone()
        return dict(row) if row else None

    def get_manifest_proofs_by_root(self, root: str) -> List[Dict[str, Any]]:
        """The SET of proof rows for one constituent set (every signer of ``root``)."""
        rows = self.conn.execute(
            "SELECT * FROM manifests WHERE root = ? ORDER BY created_at, subject",
            (root,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_manifest_by_hash(self, manifest_hash: str) -> Optional[Dict[str, Any]]:
        """A manifest proof looked up by its content address (the indexed key)."""
        row = self.conn.execute(
            "SELECT * FROM manifests WHERE manifest_hash = ? LIMIT 1",
            (manifest_hash,),
        ).fetchone()
        return dict(row) if row else None

    def add_constituent_attribution(
        self,
        constituent_hash: str,
        kind: str,
        root: str,
        issuer: str,
        subject: str,
    ) -> None:
        """Attribute a covered constituent to its FIRST covering manifest.

        INSERT OR IGNORE on the constituent hash (first-manifest-wins, Q3/ST4); a
        later covering manifest persists as a proof row in ``manifests`` (ST3) but
        does not re-point the constituent. With foreign_keys ON the manifest parent
        must already exist (ST8) — the ingress writes the manifest row first in the
        same savepoint."""
        self.conn.execute(
            """
            INSERT OR IGNORE INTO constituent_attribution
                (constituent_hash, kind, root, issuer, subject, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (constituent_hash, kind, root, issuer, subject, _now_iso()),
        )
        self._maybe_commit()

    def get_constituent_proof(self, constituent_hash: str) -> Optional[Dict[str, Any]]:
        """Resolve a constituent to its covering manifest proof, or ``None``.

        Joins constituent_attribution -> manifests on the proof triple. If the
        manifest parent is ABSENT (a corrupted / mid-migration / FK-off dangling
        state) the row still resolves to a ``proof_missing`` sentinel carrying the
        denormalized (issuer, subject) — display-authoritative attribution degrades
        gracefully, never a 500 (ST5).

        A table-absent ``OperationalError`` (an OLD/partial-schema corpus predating
        the manifest migration, opened read-only with no DDL) is treated as "no
        covering manifest" -> ``None`` -> the folio reads UNSIGNED, which is the
        correct verdict for an un-migrated/legacy folio. This is the SAME deploy-
        ordering hazard verify_cache_get already tolerates (VC10 / Fix C): the read
        container races ahead of the ingress migration, or new code serves an
        un-migrated corpus.

        The degrade is SCOPED TO READ-ONLY stores. A read_write store ALWAYS runs
        the schema migration on open (executescript), so a missing table there is a
        genuine fault — not a deploy-ordering race — and must RAISE rather than be
        silently masked. Only the read app (``read_only=True``) can legitimately
        face an un-migrated/old corpus and degrade. ONLY the missing-table case
        degrades (and only read-only); any other OperationalError ("database is
        locked", I/O error, a SQL bug) is a real fault that masquerading as "no
        proof" would silently paper over, so it propagates in BOTH modes (the Fix-C
        scope, exactly)."""
        try:
            attr = self.conn.execute(
                "SELECT * FROM constituent_attribution WHERE constituent_hash = ?",
                (constituent_hash,),
            ).fetchone()
            if attr is None:
                return None
            manifest = self.conn.execute(
                "SELECT * FROM manifests WHERE root = ? AND issuer = ? AND subject = ?",
                (attr["root"], attr["issuer"], attr["subject"]),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if self.read_only and "no such table" in str(exc).lower():
                return None
            raise
        out: Dict[str, Any] = {
            "constituent_hash": attr["constituent_hash"],
            "kind": attr["kind"],
            "root": attr["root"],
            "issuer": attr["issuer"],
            "subject": attr["subject"],
            "created_at": attr["created_at"],
        }
        if manifest is None:
            out["proof_missing"] = True
            return out
        out["proof_missing"] = False
        out["manifest_hash"] = manifest["manifest_hash"]
        out["descriptor_json"] = manifest["descriptor_json"]
        out["leaf_list_json"] = manifest["leaf_list_json"]
        out["bundle_json"] = manifest["bundle_json"]
        out["leaf_count"] = manifest["leaf_count"]
        return out

    # --- account bindings + audit (the authorization sidecar) ----------------

    def _binding_from_row(self, row) -> Any:
        """Reconstruct an authorization.Binding from a row (imported lazily to
        avoid a store<->authorization import cycle)."""
        from . import authorization
        return authorization.Binding(
            issuer=row["issuer"],
            subject=row["subject"],
            role=row["role"],
            vouched_by_issuer=row["vouched_by_issuer"],
            vouched_by_subject=row["vouched_by_subject"],
            created_at=row["created_at"],
            revoked_at=row["revoked_at"],
        )

    def _log_binding_event(
        self, issuer, subject, event, role, vouched_by_issuer, vouched_by_subject
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO binding_events
                (issuer, subject, event, role, vouched_by_issuer, vouched_by_subject, at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (issuer, subject, event, role, vouched_by_issuer, vouched_by_subject, _now_iso()),
        )

    def get_binding(self, issuer: str, subject: str):
        """The binding for a (issuer, subject) pair as a ``Binding``, or ``None``.

        A table-absent ``OperationalError`` (an OLD/partial-schema corpus with no
        account_bindings table, opened read-only with no DDL) is treated as "no
        binding" -> ``None`` -> folio_verdict reads NOT VERIFIED ('unbound signer')
        rather than 500ing. This is the read path's step-4 BINDING check against an
        un-migrated/partially-migrated corpus — the same deploy-ordering hazard the
        verify_cache and manifest reads tolerate.

        The degrade is SCOPED TO READ-ONLY stores. ``get_binding`` is ALSO the
        ingress authorization gate (ingress.py: the require_signed signer-binding
        check), and that store is opened read_write — which ALWAYS runs the schema
        migration on open. A missing account_bindings table there is therefore a
        genuine schema fault, never a deploy-ordering race, and MUST RAISE so it
        surfaces loud on the write path rather than being masked as an ordinary
        'unbound signer' rejection. Only the read app (``read_only=True``) degrades.
        ONLY the missing-table case degrades (and only read-only); any other
        OperationalError propagates in BOTH modes (the Fix-C scope)."""
        try:
            row = self.conn.execute(
                "SELECT * FROM account_bindings WHERE issuer = ? AND subject = ?",
                (issuer, subject),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if self.read_only and "no such table" in str(exc).lower():
                return None
            raise
        return self._binding_from_row(row) if row else None

    def add_binding(
        self,
        issuer: str,
        subject: str,
        role: str = "author",
        vouched_by_issuer: Optional[str] = None,
        vouched_by_subject: Optional[str] = None,
        event: Optional[str] = None,
    ):
        """Add (or reactivate) a binding; returns the resulting ``Binding``.

        A fresh pair INSERTs with a 'created' event. A revoked pair REACTIVATES the
        SAME row — revoked_at back to NULL, created_at PRESERVED (B5) — with a
        'reactivated' event. An already-active pair is idempotent: one row,
        created_at unchanged, NO event (B6). ``event`` overrides the audit verb
        (e.g. 'rotated_in' during a rotation)."""
        existing = self.conn.execute(
            "SELECT * FROM account_bindings WHERE issuer = ? AND subject = ?",
            (issuer, subject),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO account_bindings
                    (issuer, subject, role, vouched_by_issuer, vouched_by_subject,
                     created_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (issuer, subject, role, vouched_by_issuer, vouched_by_subject, _now_iso()),
            )
            self._log_binding_event(
                issuer, subject, event or "created", role,
                vouched_by_issuer, vouched_by_subject,
            )
        elif existing["revoked_at"] is not None:
            # reactivate the same row; created_at preserved (B5)
            self.conn.execute(
                """
                UPDATE account_bindings
                   SET revoked_at = NULL, role = ?,
                       vouched_by_issuer = ?, vouched_by_subject = ?
                 WHERE issuer = ? AND subject = ?
                """,
                (role, vouched_by_issuer, vouched_by_subject, issuer, subject),
            )
            self._log_binding_event(
                issuer, subject, event or "reactivated", role,
                vouched_by_issuer, vouched_by_subject,
            )
        # else: already active -> idempotent no-op, no event (B6)
        self._maybe_commit()
        return self.get_binding(issuer, subject)

    def revoke_binding(self, issuer: str, subject: str, event: Optional[str] = None) -> bool:
        """Revoke an ACTIVE binding (sets revoked_at; the row stays, B3). Returns
        False if there is no active row to revoke (absent or already revoked, B4)
        — no exception, no row created. ``event`` overrides the audit verb."""
        cur = self.conn.execute(
            """
            UPDATE account_bindings SET revoked_at = ?
             WHERE issuer = ? AND subject = ? AND revoked_at IS NULL
            """,
            (_now_iso(), issuer, subject),
        )
        if cur.rowcount == 0:
            self._maybe_commit()
            return False
        existing = self.get_binding(issuer, subject)
        self._log_binding_event(
            issuer, subject, event or "revoked", existing.role,
            existing.vouched_by_issuer, existing.vouched_by_subject,
        )
        self._maybe_commit()
        return True

    def promote_to_operator(self, issuer: str, subject: str):
        """Promote an EXISTING author row to operator, preserving created_at (D19).

        If the row is revoked it is reactivated and promoted (created_at preserved
        by the same UPDATE). Logs a 'promoted' event.

        Raises ``ValueError`` if no binding exists for the pair — the UPDATE would
        match 0 rows and the subsequent ``get_binding`` would return ``None``,
        so without this guard the next line ``AttributeError``s on ``None``. The
        live caller (cli rotate-operator) guards with ``get_binding`` first; this
        gives any future caller a meaningful error instead (D19)."""
        cur = self.conn.execute(
            """
            UPDATE account_bindings SET role = 'operator', revoked_at = NULL
             WHERE issuer = ? AND subject = ?
            """,
            (issuer, subject),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"cannot promote: no binding for {issuer!r}/{subject!r}"
            )
        b = self.get_binding(issuer, subject)
        self._log_binding_event(
            issuer, subject, "promoted", "operator",
            b.vouched_by_issuer, b.vouched_by_subject,
        )
        self._maybe_commit()
        return b

    def get_operator(self):
        """The active operator, resolved DETERMINISTICALLY (ORDER BY created_at
        LIMIT 1) — single-valued even under a corrupt two-operator state (B47).
        ``None`` when no operator is active (B9)."""
        row = self.conn.execute(
            """
            SELECT * FROM account_bindings
             WHERE role = 'operator' AND revoked_at IS NULL
             ORDER BY created_at, issuer, subject LIMIT 1
            """
        ).fetchone()
        return self._binding_from_row(row) if row else None

    def count_active_operators(self) -> int:
        """Active-operator count — the startup invariant refuses boot on != 1 (D13/D20)."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM account_bindings WHERE role = 'operator' AND revoked_at IS NULL"
        ).fetchone()[0]

    def list_active_bindings(self) -> List[Any]:
        """All active bindings as ``Binding`` objects, ordered by role then subject."""
        rows = self.conn.execute(
            """
            SELECT * FROM account_bindings WHERE revoked_at IS NULL
             ORDER BY role, issuer, subject
            """
        ).fetchall()
        return [self._binding_from_row(r) for r in rows]

    def list_bindings(self, include_revoked: bool = False) -> List[Any]:
        """All bindings (active, plus revoked when ``include_revoked``)."""
        sql = "SELECT * FROM account_bindings"
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        sql += " ORDER BY role, issuer, subject"
        return [self._binding_from_row(r) for r in self.conn.execute(sql).fetchall()]

    def get_binding_events(
        self, issuer: Optional[str] = None, subject: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """The append-only audit trail, in insertion order (optionally filtered)."""
        clauses, params = [], []
        if issuer is not None:
            clauses.append("issuer = ?")
            params.append(issuer)
        if subject is not None:
            clauses.append("subject = ?")
            params.append(subject)
        sql = "SELECT * FROM binding_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY event_seq"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # --- invites + invite audit (the onboarding ceremony) --------------------

    def _log_invite_event(
        self,
        token_hash: str,
        event: str,
        bound_issuer: Optional[str] = None,
        bound_subject: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO invite_events
                (token_hash, event, bound_issuer, bound_subject, at, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (token_hash, event, bound_issuer, bound_subject, _now_iso(), detail),
        )

    def mint_invite(
        self,
        token_hash: str,
        role: str,
        expires_at: datetime,
        vouched_by_issuer: Optional[str] = None,
        vouched_by_subject: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        """Record a freshly minted one-time invite (the operator side).

        ``token_hash`` is the SHA-256 of the CSPRNG token (plaintext never stored).
        ``expires_at`` is a ``datetime`` stored FIXED-WIDTH so the redeem CAS's SQL
        inequality compares correctly. A plain ``INSERT`` (not OR IGNORE) so a
        token_hash collision — astronomically unlikely for a >=256-bit token —
        surfaces loudly rather than silently shadowing a prior invite. Logs a
        'minted' event in the same transaction."""
        self.conn.execute(
            """
            INSERT INTO invites
                (token_hash, role, created_at, expires_at, vouched_by_issuer,
                 vouched_by_subject, note, failed_attempts)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                token_hash,
                role,
                _now_iso(),
                _iso_micros(expires_at),
                vouched_by_issuer,
                vouched_by_subject,
                note,
            ),
        )
        self._log_invite_event(token_hash, "minted")
        self._maybe_commit()

    def get_invite_by_token_hash(self, token_hash: str) -> Optional[Dict[str, Any]]:
        """The single invite row for a token hash, or ``None``. CHEAP — the
        pre-crypto gate's lookup (INV-2 step a), run OUTSIDE any transaction."""
        row = self.conn.execute(
            "SELECT * FROM invites WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        return dict(row) if row else None

    def list_invites(self, include_inactive: bool = True) -> List[Dict[str, Any]]:
        """Every invite row, newest first (operator visibility, INV-4).

        With ``include_inactive=False`` only OUTSTANDING invites (unredeemed,
        unrevoked) are returned; otherwise used/revoked rows are included so a
        hostile or completed redemption stays visible."""
        sql = "SELECT * FROM invites"
        if not include_inactive:
            sql += " WHERE used_at IS NULL AND revoked_at IS NULL"
        sql += " ORDER BY created_at DESC, token_hash"
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def revoke_invite(self, token_hash: str) -> bool:
        """Revoke an OUTSTANDING (unredeemed, unrevoked) invite. Returns False if
        there is no such row (absent, already revoked, OR already redeemed) — no
        exception, no row created.

        Restricted to ``used_at IS NULL`` so a REDEEMED invite cannot be revoked: a
        redemption produces an account binding, and the operator revokes that
        binding via ``account revoke`` — revoking the spent invite would only hide
        its operator-visible 'redeemed by <subject>' state (the display state
        prioritizes revoked_at) and make a legitimate idempotent re-redeem fail
        early. The CAS redeem path re-checks ``revoked_at IS NULL``, so revoking an
        unused invite that a race is mid-redeeming is honored under the single-
        writer lock: whichever write commits first wins."""
        cur = self.conn.execute(
            "UPDATE invites SET revoked_at = ? "
            "WHERE token_hash = ? AND revoked_at IS NULL AND used_at IS NULL",
            (_now_iso(), token_hash),
        )
        if cur.rowcount == 0:
            self._maybe_commit()
            return False
        self._log_invite_event(token_hash, "revoked")
        self._maybe_commit()
        return True

    def reserve_redeem_attempt(
        self, token_hash: str, cap: int, window_seconds: int
    ) -> bool:
        """Atomically reserve one verify attempt against a token, or refuse (INV-5).

        The per-token flood backstop, made CONCURRENCY-SAFE: a leaked valid-but-
        unused token is not burned until a SUCCESSFUL redeem, so an attacker holding
        it could otherwise drive unbounded expensive ``verify_multi`` calls. This
        reserves a slot in a SHORT write transaction (BEGIN IMMEDIATE) BEFORE the
        crypto runs, so concurrent attempts serialize and EXACTLY ``cap`` get
        through per rolling ``window_seconds``; the rest are refused here, cheaply,
        with no crypto. A check-then-increment (read the count, verify, then bump)
        is racy — N concurrent requests all pass the pre-check and all run the
        expensive verify before any increment lands; this conditional UPDATE closes
        that hole. Returns True if a slot was reserved (proceed to crypto), False if
        the cap is already met within the live window (reject as rate-limited).

        Counts every attempt on the UNUSED-token path. The counter is consulted
        ONLY here and in the advisory pre-check, both gated on ``not used`` — and the
        token burns on the single success that follows a passing reserve, after
        which the used-token idempotent path never reserves. So a verified success
        needs no refund (the counter is never read again for this token), and a bound
        author's idempotent retries are never cap-gated (INV-6). Best-effort False on
        a vanished row."""
        with self.transaction():
            row = self.conn.execute(
                "SELECT failed_attempts, attempts_window_start FROM invites WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return False
            now = datetime.now(timezone.utc)
            window_start = row["attempts_window_start"]
            within_window = False
            if window_start is not None:
                try:
                    started = datetime.fromisoformat(window_start)
                    # A NAIVE stored stamp is assumed UTC (mirroring redeem._parse), so
                    # the aware-vs-naive subtraction below can't raise TypeError and the
                    # window is measured correctly rather than mis-windowed. The except
                    # also catches TypeError as a belt-and-suspenders: no stored-stamp
                    # shape can make this best-effort/total function raise — an
                    # unparseable stamp falls back to a fresh window (within_window False).
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    within_window = (now - started).total_seconds() <= window_seconds
                except (ValueError, TypeError):
                    within_window = False
            if within_window:
                if (row["failed_attempts"] or 0) >= cap:
                    return False  # cap met this window — refuse before crypto
                self.conn.execute(
                    "UPDATE invites SET failed_attempts = failed_attempts + 1 "
                    "WHERE token_hash = ?",
                    (token_hash,),
                )
            else:
                # window rolled over (or first attempt) — reset to 1, restart window
                self.conn.execute(
                    "UPDATE invites SET failed_attempts = 1, attempts_window_start = ? "
                    "WHERE token_hash = ?",
                    (now.isoformat(), token_hash),
                )
            return True

    def log_redeem_failure(self, token_hash: str, reason: Optional[str] = None) -> None:
        """Append a 'redeem_failed' audit event (operator forensics). The attempt
        COUNTER is owned by :meth:`reserve_redeem_attempt` (reserved pre-crypto);
        this is audit only. Best-effort — the row may have been redeemed/revoked by
        a concurrent winner between the reserve and here."""
        self._log_invite_event(token_hash, "redeem_failed", detail=reason)
        self._maybe_commit()

    def redeem_invite_cas(
        self, token_hash: str, issuer: str, subject: str
    ) -> str:
        """The exactly-once burn + bind, in ONE short write transaction (INV-2/3).

        Crypto (verify_multi) MUST already have run OUTSIDE this transaction — this
        holds the single-writer lock (BEGIN IMMEDIATE) only for the cheap, bounded
        burn-and-bind, never across the multi-second Sigstore round-trip. Returns a
        status string:

        - ``'redeemed'``         — the token was burned (CAS UPDATE matched exactly
          one row) and ``(issuer, subject)`` bound fresh as the invite's role.
        - ``'revoked_identity'`` — the identity currently holds a REVOKED binding;
          NOTHING is burned or bound (INV-3: a self-service redeem must never
          un-revoke an identity the operator deliberately revoked).
        - ``'race_lost'``        — the conditional UPDATE matched 0 rows: a
          concurrent redeem won, or the token became used/revoked/expired between
          the cheap check and here. Nothing is bound; the caller re-reads for the
          idempotent-success case (INV-6).

        The revoked-binding guard, the CAS, and the bind all run under the same
        BEGIN IMMEDIATE lock, so no concurrent revoke can interleave."""
        with self.transaction():
            inv = self.conn.execute(
                "SELECT role, vouched_by_issuer, vouched_by_subject FROM invites "
                "WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if inv is None:
                return "race_lost"  # vanished between cheap check and here
            # INV-3 — refuse to bind an identity that currently holds a REVOKED
            # binding (add_binding would reactivate it, B5). Guard BEFORE the burn so
            # nothing is consumed when we refuse.
            existing = self.conn.execute(
                "SELECT revoked_at FROM account_bindings WHERE issuer = ? AND subject = ?",
                (issuer, subject),
            ).fetchone()
            if existing is not None and existing["revoked_at"] is not None:
                return "revoked_identity"
            now = _now_micros()
            cur = self.conn.execute(
                """
                UPDATE invites
                   SET used_at = ?, bound_issuer = ?, bound_subject = ?, redeemed_at = ?
                 WHERE token_hash = ?
                   AND used_at IS NULL
                   AND revoked_at IS NULL
                   AND expires_at > ?
                """,
                (now, issuer, subject, now, token_hash, now),
            )
            if cur.rowcount != 1:
                return "race_lost"
            # add_binding here cannot trip B5 reactivation: a revoked existing
            # binding was rejected above, so existing is None (fresh INSERT) or
            # active (idempotent no-op). Bind under the invite's role, vouched by
            # whoever minted the invite (the operator).
            self.add_binding(
                issuer,
                subject,
                role=inv["role"],
                vouched_by_issuer=inv["vouched_by_issuer"],
                vouched_by_subject=inv["vouched_by_subject"],
                event="redeemed",
            )
            self._log_invite_event(token_hash, "redeemed", issuer, subject)
            return "redeemed"

    def get_invite_events(self, token_hash: Optional[str] = None) -> List[Dict[str, Any]]:
        """The append-only invite audit trail, in insertion order (optionally filtered)."""
        sql = "SELECT * FROM invite_events"
        params: List[Any] = []
        if token_hash is not None:
            sql += " WHERE token_hash = ?"
            params.append(token_hash)
        sql += " ORDER BY event_seq"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # --- verify_cache (the manifest SIGNATURE verdict cache, step 3 only) -----

    def verify_cache_get(
        self, manifest_hash: str, bundle_hash: str
    ) -> Optional[Dict[str, Any]]:
        """A cached manifest signature verdict, or ``None`` on a miss.

        A table-absent ``OperationalError`` (an old store predating the migration,
        opened read-only with no DDL) is treated as a cache MISS — the real
        deploy-ordering hazard where the read container races ahead of the ingress
        migration (VC10). The read path degrades to in-process verify, never 500s.

        ONLY the missing-table case is a miss. Any other OperationalError
        ("database is locked", disk I/O error, a malformed SQL bug) is a real
        fault that masquerading as a cache miss would silently paper over — those
        propagate (VC10 intends exactly the table-absent hazard, nothing wider)."""
        try:
            row = self.conn.execute(
                "SELECT * FROM verify_cache WHERE manifest_hash = ? AND bundle_hash = ?",
                (manifest_hash, bundle_hash),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise
        return dict(row) if row else None

    def verify_cache_put(
        self,
        manifest_hash: str,
        bundle_hash: str,
        status: str,
        issuer: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> None:
        """Cache a STABLE manifest signature verdict. A recoverable status
        (TRUST_ROOT_STALE / OFFLINE_NO_TRUSTED_ROOT) writes NO row (VC3/VC4) — it
        must always re-verify."""
        if status in _RECOVERABLE_VERIFY_STATUSES:
            return
        self.conn.execute(
            """
            INSERT OR REPLACE INTO verify_cache
                (manifest_hash, bundle_hash, status, issuer, subject, verified_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (manifest_hash, bundle_hash, status, issuer, subject, _now_iso()),
        )
        self._maybe_commit()

    def all_manifests(self) -> List[Dict[str, Any]]:
        """Every manifest proof row — for the verify-cache backfill verb (VC9)."""
        return [dict(r) for r in self.conn.execute("SELECT * FROM manifests").fetchall()]

    # --- chain yields (sacks) -----------------------------------------------
    #
    # A faithful re-home of legacy ``skein/storage.py``'s sack API (the chain
    # hand-off store). The methods mirror legacy 1:1 — same fields, same JSON
    # encoding for ``artifacts``/``metadata``, same query order — just on this
    # store's connection + ``_maybe_commit`` discipline instead of legacy's
    # per-call connection.

    @staticmethod
    def _row_to_yield(row: sqlite3.Row) -> Dict[str, Any]:
        """A sack row as a dict with ``artifacts``/``metadata`` JSON decoded."""
        d = dict(row)
        if d.get("artifacts"):
            d["artifacts"] = json.loads(d["artifacts"])
        if d.get("metadata"):
            d["metadata"] = json.loads(d["metadata"])
        return d

    def add_yield(
        self,
        sack_id: str,
        chain_id: str,
        task_id: str,
        agent_id: Optional[str] = None,
        status: Optional[str] = None,
        outcome: Optional[str] = None,
        artifacts: Optional[List[str]] = None,
        notes: Optional[str] = None,
        duration_seconds: Optional[int] = None,
        tokens_used: Optional[int] = None,
        shard_path: Optional[str] = None,
        tender_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Store a chain yield into the sack. Returns True (legacy parity).

        ``sack_id`` is the human yield id ('sack-YYYYMMDD-xxxx'); it is UNIQUE, so
        a duplicate id raises (a plain INSERT, like legacy — a collision is a bug,
        not something to swallow). ``timestamp`` is stamped here (the new store
        uses explicit ISO stamps rather than SQLite's ``CURRENT_TIMESTAMP``) so
        ordering agrees with the rest of the store and is timezone-aware.
        """
        self.conn.execute(
            """
            INSERT INTO sacks (
                sack_id, chain_id, task_id, agent_id, timestamp,
                status, outcome, artifacts, notes,
                duration_seconds, tokens_used, shard_path, tender_id,
                metadata
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sack_id,
                chain_id,
                task_id,
                agent_id,
                _now_iso(),
                status,
                outcome,
                json.dumps(artifacts) if artifacts else None,
                notes,
                duration_seconds,
                tokens_used,
                shard_path,
                tender_id,
                json.dumps(metadata) if metadata else None,
            ),
        )
        self._maybe_commit()
        return True

    def get_chain_yields(self, chain_id: str) -> List[Dict[str, Any]]:
        """All yields in a chain, in execution order ((timestamp, id) ascending).

        ``id`` (the autoincrement insertion order) breaks timestamp ties so two
        yields stamped in the same instant read back in the order they landed.
        """
        rows = self.conn.execute(
            "SELECT * FROM sacks WHERE chain_id = ? ORDER BY timestamp, id",
            (chain_id,),
        ).fetchall()
        return [self._row_to_yield(r) for r in rows]

    def get_yield(self, sack_id: str) -> Optional[Dict[str, Any]]:
        """One yield by its sack id, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM sacks WHERE sack_id = ?", (sack_id,)
        ).fetchone()
        return self._row_to_yield(row) if row else None

    def get_yields_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Yields with a given status, newest first (e.g. find all blocked work)."""
        rows = self.conn.execute(
            "SELECT * FROM sacks WHERE status = ? ORDER BY timestamp DESC, id DESC",
            (status,),
        ).fetchall()
        return [self._row_to_yield(r) for r in rows]

    def get_agent_yields(self, agent_id: str) -> List[Dict[str, Any]]:
        """All yields produced by one agent, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM sacks WHERE agent_id = ? ORDER BY timestamp DESC, id DESC",
            (agent_id,),
        ).fetchall()
        return [self._row_to_yield(r) for r in rows]

    def get_previous_yield(
        self, chain_id: str, before_task_id: str
    ) -> Optional[Dict[str, Any]]:
        """The most recent yield in a chain BEFORE ``before_task_id`` ran.

        Used to inject the prior task's hand-off into a downstream task. Walks the
        chain in execution order and returns the yield just before the first one
        produced by ``before_task_id`` (``None`` if that task is first).
        """
        rows = self.conn.execute(
            "SELECT * FROM sacks WHERE chain_id = ? ORDER BY timestamp, id",
            (chain_id,),
        ).fetchall()
        previous = None
        for row in rows:
            if row["task_id"] == before_task_id:
                break
            previous = row
        return self._row_to_yield(previous) if previous else None

    # --- reporting helpers --------------------------------------------------

    def count_folios(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM folios").fetchone()[0]

    def count_threads(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]

    def count_aliases(self) -> int:
        """Number of legacy-id aliases. ``legacy_id`` is the PK, so this is the
        distinct-alias count the import fidelity gate reconciles against."""
        return self.conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0]

    def unresolved_endpoints(self) -> List[str]:
        """Thread endpoints that are legacy ids still awaiting an alias.

        A resolved folio edge stores a ``sha256::`` content hash; an actor
        endpoint was routed to the weaver and stored as NULL. Anything left —
        a non-null endpoint that is neither a content hash nor a known alias —
        is a dangling or cross-project reference holding its legacy id, which
        resolves lazily if/when the target imports and registers an alias.
        """
        rows = self.conn.execute(
            """
            SELECT DISTINCT endpoint FROM (
                SELECT from_id AS endpoint FROM threads
                UNION
                SELECT to_id   AS endpoint FROM threads
            )
            WHERE endpoint IS NOT NULL
              AND endpoint NOT LIKE 'sha256::%'
              AND endpoint NOT IN (SELECT legacy_id FROM aliases)
            ORDER BY endpoint
            """
        ).fetchall()
        return [r["endpoint"] for r in rows]
