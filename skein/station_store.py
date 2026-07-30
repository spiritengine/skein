"""StationStore — the federation-station role surface over the working skein store.

Station re-home Stage 1 (Fork B). A *station* holds signed, multi-author content
received on the wire; a *workbench* holds locally-authored lineages. Under Fork B they
share ONE folio object store — the working ``versions`` table — plus the post-swap
``threads`` table and the station sidecar tables (``station_slugs``, ``aliases``, the
federation set). ``LogDatabase(station=True)`` births that schema (Stage 1a); this module
is the station's *accessor surface* over it.

Why a separate class rather than methods on ``LogDatabase``:
  * Four skein_next method names the station servers call (``get_folio``, ``save_thread``,
    ``get_threads``, ``search_folios``) already exist on ``LogDatabase`` with incompatible
    ``Folio``-object signatures — they cannot be re-defined on the same class.
  * The servers depend on ONE long-lived connection with the skein_next posture
    (rollback-journal so the read surface can mount the corpus ``:ro``; ``busy_timeout`` +
    ``foreign_keys`` on; ``transaction()``/``savepoint()``). ``LogDatabase`` opens a fresh
    WAL connection per call — the wrong posture for a station.
So ``StationStore`` presents the EXACT skein_next store interface, refs-free, over the
shared tables. Later stages re-point the ingress/read servers at it in place of
``SkeinNextStore`` with minimal rewiring.

**Strict-null narrowing (decided with Patrick, 2026-07-09).** skein_next's ``folios`` and
``threads`` are all-nullable; the shared ``versions``/``threads`` keep the workbench's
``NOT NULL`` on the structural canonical columns. The station therefore REQUIRES those
fields non-null (folio: type/title/content/created_at/created_by; thread:
from_id/to_id/type/created_at — weaver and content stay nullable) and raises
``ValueError`` on a null one. This is a deliberate, tested narrowing vs skein_next: safe
because the only in-scope producer (a workbench publisher) reads its own ``NOT NULL``
columns onto the wire, so a conforming publish never carries the forbidden nulls.

Exception surface: the null guard raises ``ValueError``; a NON-null but wrong-typed field
(e.g. an int title, a non-datetime ``created_at``) raises canon's own ``CanonError`` /
``TypeError`` from the hash step that runs first. This never breaks the "never 500s"
posture because the re-homed ingress runs the total ``wire.*_reject_reason`` gate before
this store AND wraps the write in ``except Exception -> "invalid fields"`` — it catches
ANY exception, not only ``ValueError``. Callers must not narrow that catch to
``ValueError`` alone.

This module covers Stage 1b (folio/thread/alias accessors) and 1c (``latest_statuses`` +
the genesis-anchored ``station_slugs`` derived-head resolver). Federation-table accessors
ride with their servers in later stages.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple
from urllib.parse import quote

from .authorization import WIRE_REDEEMABLE_ROLES
from .identity import compute_folio_hash, compute_thread_hash, normalize_created_at
from .storage import LogDatabase
from .utils import generate_thread_id

# The station corpus filename under a data directory. skein_next used ``store.db``; the
# re-homed station runs on skein, so it is ``skein.db`` (revisited at Stage 6 config).
DB_FILENAME = "skein.db"

# How long a write waits for a held lock before giving up (SQLite default 0 = fail
# instantly). Ported from skein_next: lets concurrent ingress writers serialize under a
# rollback-journal store instead of getting an instant 'database is locked'.
BUSY_TIMEOUT_MS = 5000


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
    ``expires_at > ?`` inequality is a correct string compare.

    A NAIVE datetime is taken AS UTC (finding-20260709-p4n5 #2): bare
    ``astimezone`` would reinterpret it as system-LOCAL time first, silently
    shifting an invite expiry by the host's UTC offset — the same naive->UTC
    rule ``reserve_redeem_attempt``'s window parse already applies."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _now_micros() -> str:
    """Fixed-width UTC now() — the redeem-CAS / expiry comparison clock."""
    return _iso_micros(datetime.now(timezone.utc))


def sqlite_error_is_lock(e: sqlite3.OperationalError) -> bool:
    """Whether an OperationalError is write-lock contention (SQLITE_BUSY/LOCKED).

    Discriminate on the numeric SQLite result code (the robust signal): mask the
    EXTENDED ``sqlite_errorcode`` (Python >= 3.11; absent on a hand-built exception)
    to the primary byte so lock-family extended codes count too, and fall back to
    the (stable) message text only when there is no code at all. Used by the ingress
    publish + redeem routes to degrade a transient lock to a retryable 503 while a
    genuine (non-lock) fault still surfaces.

    The module-level ``sqlite3.SQLITE_BUSY``/``SQLITE_LOCKED`` constants are
    ALSO Python >= 3.11 only -- getattr with a None default, not a bare
    attribute access, so this doesn't AttributeError on 3.10 before the
    ``primary is None`` fallback ever gets a chance to run."""
    code = getattr(e, "sqlite_errorcode", None)
    primary = (code & 0xFF) if isinstance(code, int) else None
    if primary is not None:
        busy = getattr(sqlite3, "SQLITE_BUSY", None)
        locked = getattr(sqlite3, "SQLITE_LOCKED", None)
        return primary in (busy, locked)
    return "lock" in str(e).lower() or "busy" in str(e).lower()


# verify_multi statuses meaning "could not check" — never cached, must re-verify.
_RECOVERABLE_VERIFY_STATUSES = frozenset({"TRUST_ROOT_STALE", "OFFLINE_NO_TRUSTED_ROOT"})


# bundle_hash_for is re-homed here (from skein_next/store.py) as the shared
# verify_cache-key helper (VC11). The re-homed ``envelope.folio_verdict`` reads it
# from this module; the re-homed ingress writer will import the same copy when it
# lands (Stage 3). Until then skein_next/ingress.py keeps its own byte-identical
# copy on the live path — keep the two in lockstep until skein_next is retired.
# The function itself (docstring + body) is ported byte-for-byte from skein_next below.
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


# ── search helpers (ported verbatim from skein_next/store.py — the station search is the
# skein_next L1 substring rank, NOT the workbench FTS5 path) ─────────────────────────────

def _like_escape(s: str) -> str:
    """Escape a literal string for use inside a ``LIKE ... ESCAPE '\\'`` pattern.

    The backslash is escaped first so the later ``%``/``_`` escapes are not doubled.
    Callers add their own ``%`` wildcards around the result.
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Relevance weights: a query term in the title outweighs one in the body.
_TITLE_WEIGHT = 3
_BODY_WEIGHT = 1

# Cap on distinct query terms — ``?q=`` is unauthenticated on a mesh-facing surface; an
# unbounded term list drives O(terms x candidates x content) substring scans and can trip
# the SQLite variable limit. 32 is far more than any real query.
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

    Re-homed byte-identical from ``skein_next.store.make_snippet`` (station Stage 4):
    the read surface builds search snippets with it, and it is a pure function with no
    store coupling.
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


# The six folio columns the station reads out of ``versions`` — the exact skein_next
# folio-dict contract (same names as skein_next ``folios``). Selected explicitly so the
# dict is those six keys regardless of any future ``versions`` column.
_FOLIO_COLS = "content_hash, type, created_at, created_by, title, content"


def _existing_db_role(path) -> Optional[str]:
    """Classify an EXISTING db at ``path`` without writing it: ``"station"`` (has the
    ``station_slugs`` table — the station-only marker), ``"other"`` (a db with tables but
    NO ``station_slugs``, i.e. a workbench corpus — even one migrated to a ``thread_hash``
    PK by ``threads_pk_swap``), or ``None`` (absent / empty / unreadable → birthing a
    station here is safe). Opens read-only so a path is vetted BEFORE any DDL runs.

    The discriminator is ``station_slugs``, not the ``threads`` PK shape: a workbench db
    that has undergone the ``threads_pk_swap`` migration carries the SAME ``thread_hash``
    PK a station does, so PK shape can't tell them apart — but only a station ever has
    ``station_slugs``.
    """
    if not Path(path).exists():
        return None
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None
    try:
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not tables:
            return None
        if "station_slugs" not in tables:
            return "other"
        cols = {r[1] for r in conn.execute("PRAGMA table_info(station_slugs)")}
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    # A station_slugs table present but WITHOUT the genesis-anchored shape is a corrupt or
    # spoofed marker (skein_next's flat slugs had slug+content_hash, not anchor_hash):
    # classify non-station so birth REFUSES rather than mutating the db.
    return "station" if {"slug", "anchor_hash"} <= cols else "other"


class StationStore:
    """The skein_next store interface, refs-free, over the shared skein station tables.

    NOT safe for concurrent use of a SINGLE instance: ``_in_batch``/``_sp_counter`` and the
    one connection are shared unlocked state, so ``check_same_thread=False`` is for handing
    one instance to one request that uses it SERIALLY (the skein_next posture: one store per
    request), never for concurrent calls on the same instance."""

    # --- lifecycle ----------------------------------------------------------

    def __init__(
        self,
        data_dir: Optional[Any] = None,
        *,
        db_path: Optional[Any] = None,
        check_same_thread: bool = True,
        read_only: bool = False,
    ):
        """Open the station store.

        Pass ``data_dir`` (the corpus lives at ``data_dir/skein.db``) or an explicit
        ``db_path``. ``read_only`` opens the corpus without ever writing it — no mkdir, no
        DDL, no commit (``mode=ro``/``immutable=1``) — for the read surface; the store
        must already exist. ``check_same_thread=False`` lets a threadpool request touch a
        connection created on another thread (the request uses it serially).
        """
        if db_path is not None:
            self.db_path = Path(db_path)
        elif data_dir is not None:
            self.db_path = Path(data_dir) / DB_FILENAME
        else:
            raise ValueError("StationStore requires data_dir or db_path")
        self.read_only = read_only
        if read_only:
            self.conn = self._connect_read_only(check_same_thread)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            # Refuse an existing NON-station corpus BEFORE any DDL runs. A station uses the
            # SAME default filename (data_dir/skein.db) LogDatabase does; without this
            # pre-birth check, LogDatabase(station=True) below would bolt the station
            # sidecar tables onto a workbench db irreversibly. Checking read-only, before
            # birth, means a mismatched db is never altered. (A concurrent workbench
            # creation of this path in the check→birth window is a TOCTOU deferred to the
            # Stage-6 config toggle that binds role→data-dir; no Stage-1 caller races it.)
            if _existing_db_role(self.db_path) == "other":
                raise ValueError(
                    f"StationStore refuses an existing non-station db at {self.db_path} "
                    f"(no well-formed station_slugs table — a workbench corpus, or a "
                    f"corrupt/incompletely-birthed one?)"
                )
            # Birth/ensure the schema via the SINGLE DDL owner (LogDatabase, station role).
            # A station db is born rollback-journal (LogDatabase._get_connection is
            # station-aware on EVERY connection), so this connection and the served corpus
            # stay rollback-journal with no WAL↔rollback flip.
            LogDatabase(self.db_path, station=True)
            self.conn = sqlite3.connect(str(self.db_path), check_same_thread=check_same_thread)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            # Enforced from the first write (the constituent_attribution FK); MUST be set
            # outside any transaction, before writes.
            self.conn.execute("PRAGMA foreign_keys=ON")
        # Confirm the opened corpus is a station (the read_only path does no pre-birth
        # check). Close the just-opened connection if we bail so a retry loop over a
        # misconfigured path doesn't leak a handle per attempt.
        try:
            self._assert_station_corpus()
        except Exception:
            self.conn.close()
            raise
        self._in_batch = False
        self._sp_counter = 0

    def _assert_station_corpus(self) -> None:
        """The opened corpus must be a station: it has the ``station_slugs`` marker table
        (a workbench never does, migrated or not) AND a POST-swap ``threads`` table
        (``thread_hash`` PK — the content-address dedup ``save_thread`` relies on)."""
        tables = {
            r["name"] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        cols = (
            {r["name"] for r in self.conn.execute("PRAGMA table_info(station_slugs)")}
            if "station_slugs" in tables
            else set()
        )
        if not {"slug", "anchor_hash"} <= cols:
            raise ValueError(
                f"{self.db_path} is not a station corpus (no well-formed station_slugs "
                f"table — a workbench db?)"
            )
        pk = [r["name"] for r in self.conn.execute("PRAGMA table_info(threads)") if r["pk"]]
        if pk != ["thread_hash"]:
            raise ValueError(
                f"{self.db_path} threads is not post-swap (PK {pk or 'none'}, "
                f"not thread_hash) — dedup would be broken"
            )

    def _connect_read_only(self, check_same_thread: bool) -> sqlite3.Connection:
        """Open the corpus read-only, never writing it. Tries ``mode=ro`` (WAL-aware)
        then ``immutable=1`` (a read-only filesystem where SQLite can't create sidecars)."""
        # read_only requires an EXISTING corpus and must never create one. The immutable=1
        # fallback URI carries no mode= param, so on a missing path SQLite would open it
        # read-write-CREATE and leave a 0-byte skein.db behind — violating the no-write
        # contract. Refuse a missing path up front instead.
        if not self.db_path.exists():
            raise sqlite3.OperationalError(
                f"read-only station corpus does not exist: {self.db_path}"
            )
        # Percent-encode the path into the file: URI (keeping '/' as the separator): a raw
        # path with '#' or '?' (ticket numbers, versioned dir names) otherwise starts a URI
        # fragment/query and SQLite silently opens a truncated, wrong path.
        p = quote(str(self.db_path), safe="/")
        last_err: Optional[Exception] = None
        # Both URIs carry mode=ro so neither can CREATE a file: bare immutable=1 defaults to
        # read-write-create and would leave a 0-byte db if the path vanished mid-open.
        for uri in (f"file:{p}?mode=ro", f"file:{p}?immutable=1&mode=ro"):
            conn: Optional[sqlite3.Connection] = None
            try:
                conn = sqlite3.connect(uri, uri=True, check_same_thread=check_same_thread)
                conn.row_factory = sqlite3.Row
                conn.execute("SELECT 1 FROM versions LIMIT 1")  # resolve the open
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

    def __enter__(self) -> "StationStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- transactions -------------------------------------------------------

    def _maybe_commit(self) -> None:
        """Commit immediately unless inside a batch transaction."""
        if not self._in_batch:
            self.conn.commit()

    @contextmanager
    def transaction(self) -> Iterator["StationStore"]:
        """Batch many writes into a single commit (ported from skein_next).

        ``BEGIN IMMEDIATE`` grabs the write lock up front so concurrent writers queue on
        it and ``busy_timeout`` serializes them (a DEFERRED txn would deadlock on the
        read→write upgrade and fail instantly). The BEGIN runs before ``_in_batch`` is
        set, so a failed lock leaves the store clean. Not re-entrant.
        """
        if self._in_batch:
            raise RuntimeError("transaction() is not re-entrant")
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
        """A nested SAVEPOINT for per-item isolation inside a batch (ported from
        skein_next). A block that raises rolls back only its writes, then re-raises — so
        the ingress can reject one item and let its siblings commit. Re-entrant.

        MUST be nested inside :meth:`transaction`. Standalone, a write's own
        ``_maybe_commit`` would COMMIT (releasing every open savepoint) inside the block,
        so the rollback would then fail with 'no such savepoint' AND the write would be
        permanently committed — the opposite of isolation. Guarded so misuse fails loudly
        rather than silently committing a rejected item."""
        if not self._in_batch:
            raise RuntimeError("savepoint() must be used inside transaction()")
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

    # --- folios (over the shared ``versions`` table, refs-free) -------------

    def create_folio(self, fields: Mapping[str, Any]) -> str:
        """Normalize, hash, and idempotently insert a folio into ``versions``. Returns
        its content hash. The caller does NOT pass ``content_hash`` — it is recomputed.

        Strict-null narrowing: the station requires the structural canonical fields
        (type/title/content/created_at/created_by) non-null; a null one raises
        ``ValueError`` (the ingress reports it as 'invalid fields'). See the module
        docstring.
        """
        content_hash = compute_folio_hash(fields)
        normalized_created_at = normalize_created_at(fields.get("created_at"))
        row = {
            "type": fields.get("type"),
            "title": fields.get("title"),
            "content": fields.get("content"),
            "created_at": normalized_created_at,
            "created_by": fields.get("created_by"),
        }
        missing = [k for k, v in row.items() if v is None]
        if missing:
            raise ValueError(
                "station folio requires non-null " + ", ".join(sorted(missing))
                + f" (content_hash={content_hash})"
            )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO versions
                (content_hash, type, created_at, created_by, title, content)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                content_hash,
                row["type"],
                row["created_at"],
                row["created_by"],
                row["title"],
                row["content"],
            ),
        )
        self._maybe_commit()
        return content_hash

    def get_folio(self, content_hash: str) -> Optional[Dict[str, Any]]:
        """Read one folio by content hash — the 6-col dict, or ``None`` (never ``{}``)."""
        row = self.conn.execute(
            f"SELECT {_FOLIO_COLS} FROM versions WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return dict(row) if row else None

    def list_folios(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            f"""
            SELECT {_FOLIO_COLS} FROM versions
            ORDER BY created_at, content_hash
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_folios(self, limit: int = 30) -> List[Dict[str, Any]]:
        """The newest ``limit`` folios, created_at DESCending, content_hash ascending
        as the stable tiebreak — the catalog's newest-N, pushed to SQL so the index
        reads only what it renders instead of scanning the whole corpus in Python.

        ``COALESCE(created_at, '')`` folds a NULL created_at into the empty string so
        a missing timestamp sorts LAST under DESC, reproducing the prior Python key
        ``r.get("created_at") or ""`` with ``reverse=True`` (whose stable tiebreak was
        content_hash ascending, from ``list_folios``' ``ORDER BY created_at, content_hash``).
        """
        rows = self.conn.execute(
            f"""
            SELECT {_FOLIO_COLS} FROM versions
            ORDER BY COALESCE(created_at, '') DESC, content_hash ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_folios(
        self, query: str, limit: int = 100, *, overflow_probe: bool = False
    ) -> List[Dict[str, Any]]:
        """Folios matching ``query``, AND-of-terms, ranked title-over-body (skein_next L1).

        A folio matches only if EVERY whitespace-split term appears (case-insensitively)
        in its title or content; terms are matched literally (``LIKE`` wildcards escaped).
        A bounded recency-ordered candidate window is pulled, then ranked in Python — NOT
        a whole-corpus rank — so the tiebreak and which rows survive match skein_next.

        ``overflow_probe`` lets a caller detect "more than ``limit`` matched" WITHOUT
        widening that window: the SQL candidate window stays derived from the SERVED
        ``limit`` (``max(limit*5, 200)`` — unchanged), and only the final Python slice
        grows, returning up to ``limit + 1`` ranked rows instead of ``limit``. The served
        set is the first ``limit`` rows, byte-identical to a plain ``search_folios(query,
        limit)`` (same window, same ranking, ``ranked[:limit+1][:limit] == ranked[:limit]``);
        a present ``(limit + 1)``th row ONLY signals overflow, it never changes the window.
        Because the window is 5x the served ``limit``, any query with more than ``limit``
        matches leaves more than ``limit`` survivors in the window, so the probe row is
        always present when it should be — no false-negative edge for this window formula.
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
        # Window derived from the SERVED limit, NOT from the probe: probing must never
        # widen the candidate set or the served top-``limit`` could shift (a wider window
        # admits older rows that can out-score served ones — finding-20260710-lx37 fix #4).
        params.append(max(limit * 5, 200))
        rows = self.conn.execute(
            f"""
            SELECT {_FOLIO_COLS} FROM versions
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
        cut = limit + 1 if overflow_probe else limit
        return ranked[:cut]

    def find_by_prefix(self, prefix: str, limit: int = 10) -> List[str]:
        """Content hashes beginning with ``prefix`` (git-style short-hash lookup).

        ``prefix`` is matched literally against the full ``sha256::<hex>`` address
        (``LIKE`` metacharacters escaped). Returns up to ``limit`` bare hash strings so a
        caller can detect an ambiguous prefix.
        """
        like = _like_escape(prefix) + "%"
        rows = self.conn.execute(
            """
            SELECT content_hash FROM versions
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
        """Member folios of a site, joined through ``within`` threads, ordered by the
        folio's ``(created_at, content_hash)``. ``DISTINCT`` guards against a folio
        holding two membership edges to the same site."""
        sql = [
            f"SELECT DISTINCT {', '.join('f.' + c for c in _FOLIO_COLS.split(', '))}",
            "FROM versions f",
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

    def count_folios(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM versions").fetchone()["n"]

    def count_threads(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM threads").fetchone()["n"]

    def latest_statuses(self, folio_hashes: List[str]) -> Dict[str, str]:
        """Map each given folio hash to its latest status-thread content (1c control).

        Status is thread-derived: a ``type=status`` thread points ``to_id`` at the folio;
        the latest by ``(created_at, thread_hash)`` wins. Keyed by folio HASH, refs-free —
        NOT the workbench's refs-slug-keyed ``_latest_control_by_folio`` (option (b)). A
        folio with no status thread is simply absent (the method NEVER invents 'open';
        callers do ``.get(h, 'open')``). The IN-list is chunked at 900 to stay under the
        SQLite variable limit; an empty input yields ``{}``.
        """
        out: Dict[str, str] = {}
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
            # Ascending order → the last row written for each folio is the newest;
            # thread_hash breaks equal-created_at ties deterministically.
            for r in rows:
                out[r["to_id"]] = r["content"]
        return out

    # --- threads (over the shared post-swap ``threads`` table, refs-free) ---

    def save_thread(
        self,
        from_id: Optional[str] = None,
        to_id: Optional[str] = None,
        type: Optional[str] = None,
        weaver: Optional[str] = None,
        created_at: Any = None,
        content: Optional[str] = None,
    ) -> str:
        """Idempotently store a thread edge keyed on its content hash. Returns the hash.

        Refs-free — unlike the workbench ``save_thread``, the station stores wire
        endpoints verbatim (no ``_genesis_key_control``/``genesis_of_slug``). Dedup is the
        ``INSERT OR IGNORE`` on the post-swap ``thread_hash`` PK. A ``thread_id`` is
        generated to satisfy the audit column — it is NOT part of the content hash, so it
        never affects dedup. Strict-null: from_id/to_id/type/created_at must be non-null.
        """
        thread_hash = compute_thread_hash(from_id, to_id, type, weaver, created_at, content)
        normalized_created_at = normalize_created_at(created_at)
        required = {
            "from_id": from_id,
            "to_id": to_id,
            "type": type,
            "created_at": normalized_created_at,
        }
        missing = [k for k, v in required.items() if v is None]
        if missing:
            raise ValueError(
                "station thread requires non-null " + ", ".join(sorted(missing))
                + f" (thread_hash={thread_hash})"
            )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO threads
                (thread_hash, thread_id, from_id, to_id, type, weaver, created_at, content)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_hash,
                generate_thread_id(),
                from_id,
                to_id,
                type,
                weaver,
                normalized_created_at,
                content,
            ),
        )
        self._maybe_commit()
        return thread_hash

    def get_thread(self, thread_hash: str) -> Optional[Dict[str, Any]]:
        """Read one thread by its content hash (symmetric with get_folio). The dict
        carries the extra harmless ``thread_id`` audit key alongside the 7 wire columns."""
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
        """Query threads by any AND-combination of from_id/to_id/type, ordered
        ``(created_at, thread_hash)`` ASC (envelope consumes this order for dedup + BFS)."""
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

    # --- aliases (flat legacy-id -> content_hash) ---------------------------

    def set_alias(self, legacy_id: str, content_hash: str) -> None:
        """Map a legacy id to a content hash (upsert; last write wins). The write twin of
        ``resolve_alias`` — the Stage-7a corpus migration populates ``aliases`` through it,
        and a migrated corpus's legacy-id thread endpoints resolve through the read path."""
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

    def unresolved_endpoints(self) -> List[str]:
        """Thread endpoints that are legacy ids still awaiting an alias.

        A resolved folio edge stores a ``sha256::`` content hash; anything left — a
        non-null endpoint that is neither a content hash nor a known alias — is a
        dangling or cross-project reference holding its legacy id, which resolves lazily
        if/when the target imports and registers an alias. Touches only ``threads``/
        ``aliases``; the station's ``threads`` declares from_id/to_id NOT NULL, so the
        endpoint IS-NOT-NULL clause's NULL branch cannot arise here. A Stage-7a migration
        fidelity query.

        Hardened beyond skein_next's byte-identical query: the ``aliases`` subquery
        excludes a NULL ``legacy_id`` (``legacy_id`` is a TEXT PK, which SQLite lets be
        NULL). Without the guard a single NULL alias key makes ``endpoint NOT IN
        (SELECT legacy_id …)`` evaluate to NULL/false for EVERY row under SQL three-valued
        logic, silently returning ``[]`` and defeating the migration guard."""
        rows = self.conn.execute(
            """
            SELECT DISTINCT endpoint FROM (
                SELECT from_id AS endpoint FROM threads
                UNION
                SELECT to_id   AS endpoint FROM threads
            )
            WHERE endpoint IS NOT NULL
              AND endpoint NOT LIKE 'sha256::%'
              AND endpoint NOT IN (SELECT legacy_id FROM aliases WHERE legacy_id IS NOT NULL)
            ORDER BY endpoint
            """
        ).fetchall()
        return [r["endpoint"] for r in rows]

    # --- slugs / naming (1c — genesis-anchored, derived head) ---------------
    #
    # A station slug is a CLAIM ``(slug, anchor_hash = the lineage GENESIS content hash,
    # claimed_by, scope)``; resolution DERIVES the head by walking ``supersedes`` edges
    # forward from the anchor over versions the station holds — never a stored mutable
    # head, never ``refs`` (Risk-3). The skein_next resolve_slug/list_slugs CONTRACT
    # (slug→hash / [(slug,hash)]) is preserved; the mechanism is the derived walk. Site
    # slugs are the degenerate case: the site folio is its own genesis, no walk. Wire
    # folio-slug claims + ingress admission arrive in a later stage; Stage 1 exposes
    # ``set_slug`` (last-write-wins, as the ingress uses it for site folios).

    def set_slug(self, slug: str, content_hash: str) -> None:
        """Bind ``slug`` to a lineage anchored at ``content_hash`` (upsert, LAST-WRITE-
        WINS, signer-BLIND). Retained for internal/test callers; the ingress uses
        :meth:`claim_slug` (signer-pair collision, §3.3/§6). A signer-blind set writes a
        NULL claimant pair."""
        self.conn.execute(
            """
            INSERT INTO station_slugs (slug, anchor_hash, claimed_by_issuer,
                                       claimed_by_subject, scope)
            VALUES (?, ?, NULL, NULL, NULL)
            ON CONFLICT(slug) DO UPDATE SET
                anchor_hash = excluded.anchor_hash,
                claimed_by_issuer = NULL,
                claimed_by_subject = NULL
            """,
            (slug, content_hash),
        )
        self._maybe_commit()

    def perm_schema_current(self) -> bool:
        """Whether the corpus is on the rev-6 permission-model schema shape — detected by
        the ``station_slugs.claimed_by_issuer`` column (the pair-claim rebuild). A False
        result means a PRE-rev6 corpus that has not run perm_model_rev6.migrate(); the
        ingress boot guard refuses to serve it (else claim_slug/set_slug fail at runtime
        with a cryptic 'no such column')."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(station_slugs)")}
        return "claimed_by_issuer" in cols

    def get_slug_claim(self, slug: str) -> Optional[Dict[str, Any]]:
        """The raw claim row for ``slug`` — ``{anchor_hash, claimed_by_issuer,
        claimed_by_subject}`` — or ``None`` if unclaimed. (Distinct from
        :meth:`resolve_slug`, which DERIVES the head; this reads the claim itself.)"""
        row = self.conn.execute(
            "SELECT anchor_hash, claimed_by_issuer, claimed_by_subject "
            "FROM station_slugs WHERE slug = ?",
            (slug,),
        ).fetchone()
        return dict(row) if row else None

    def claim_slug(
        self,
        slug: str,
        anchor_hash: str,
        issuer: str,
        subject: str,
        *,
        override: bool = False,
    ) -> str:
        """Claim ``slug`` for a lineage anchored at ``anchor_hash`` on behalf of the
        signer PAIR ``(issuer, subject)``, with a collision check BEFORE the write
        (atomic inside the caller's ingest transaction, §3.3/§6). Returns a status:

        - ``'claimed'``      — the slug was free; recorded to this signer.
        - ``'re-anchored'``  — the SAME signer re-claims; the anchor is repointed
          (a re-publish of a superseding genesis under an existing owned slug).
        - ``'overridden'``   — a DIFFERENT signer, but ``override`` (an administrator /
          operator re-point) is set; the anchor AND the claimant pair are rewritten.
        - ``'collision'``    — a DIFFERENT signer without override: NOTHING is written
          (the ingress rejects the slug claim).

        A NULL-claimant legacy row (a signer-blind :meth:`set_slug` / pre-migration
        claim) is NOT "the same signer", so it collides for a real signer unless
        overridden — deliberately fail-closed against squatting a legacy name."""
        existing = self.get_slug_claim(slug)
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO station_slugs
                    (slug, anchor_hash, claimed_by_issuer, claimed_by_subject, scope)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (slug, anchor_hash, issuer, subject),
            )
            self._maybe_commit()
            return "claimed"
        same_signer = (
            existing["claimed_by_issuer"] == issuer
            and existing["claimed_by_subject"] == subject
        )
        if same_signer:
            self.conn.execute(
                "UPDATE station_slugs SET anchor_hash = ? WHERE slug = ?",
                (anchor_hash, slug),
            )
            self._maybe_commit()
            return "re-anchored"
        if override:
            self.conn.execute(
                """
                UPDATE station_slugs
                   SET anchor_hash = ?, claimed_by_issuer = ?, claimed_by_subject = ?
                 WHERE slug = ?
                """,
                (anchor_hash, issuer, subject, slug),
            )
            self._maybe_commit()
            return "overridden"
        return "collision"

    def _version_exists(self, content_hash: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM versions WHERE content_hash = ? LIMIT 1", (content_hash,)
            ).fetchone()
            is not None
        )

    def _derive_heads(self, anchor_hash: str) -> List[str]:
        """The head(s) of a lineage: walk ``supersedes`` forward from ``anchor_hash`` over
        versions the station holds. A ``supersedes`` edge is ``(from_id=new, to_id=old)``,
        so a version's successor is the ``from_id`` of a ``supersedes`` thread whose
        ``to_id`` is that version. A version with no HELD successor is a head (iff the
        station holds it). Usually one head; a fork (two supersedes children) yields two —
        resolution surfaces the fork, never a silent winner. Terminates on cycles via the
        seen-set (a well-formed graph should be acyclic; the guard is defensive).

        This resolver does NOT verify signatures — it reduces over whatever ``supersedes``
        edges the station HOLDS. Signature/admission (only signed supersedes edges enter
        the store) is the ingress/verify stage's responsibility, upstream of here; a
        forged edge must be rejected at admission, not detected in resolution."""
        heads: List[str] = []
        seen: set = set()
        stack = [anchor_hash]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            successors = [
                r["from_id"]
                for r in self.conn.execute(
                    "SELECT from_id FROM threads WHERE type = 'supersedes' AND to_id = ?",
                    (node,),
                )
            ]
            held = [s for s in successors if self._version_exists(s)]
            if not held:
                if self._version_exists(node):
                    heads.append(node)
            else:
                stack.extend(held)
        return sorted(set(heads))

    def resolve_slug_heads(self, slug: str) -> List[str]:
        """All held heads a slug resolves to: ``[]`` if unclaimed, one for a normal
        lineage, more than one for a fork (surfaced, never silently collapsed)."""
        row = self.conn.execute(
            "SELECT anchor_hash FROM station_slugs WHERE slug = ?", (slug,)
        ).fetchone()
        if not row:
            return []
        return self._derive_heads(row["anchor_hash"])

    def resolve_slug(self, slug: str) -> Optional[str]:
        """The single head ``slug`` names, or ``None`` when unclaimed OR forked. A fork
        never returns one of its branches (no silent winner); callers wanting the fork use
        :meth:`resolve_slug_heads`. Preserves the skein_next slug→hash|None contract for
        the degenerate (site / un-forked) case."""
        heads = self.resolve_slug_heads(slug)
        return heads[0] if len(heads) == 1 else None

    def list_slugs(self) -> List[Tuple[str, str]]:
        """All ``(slug, head)`` pairs ordered by slug — the un-forked, resolvable ones
        (a forked or unresolvable claim is omitted rather than named to a silent winner)."""
        out: List[Tuple[str, str]] = []
        for row in self.conn.execute(
            "SELECT slug, anchor_hash FROM station_slugs ORDER BY slug"
        ).fetchall():
            heads = self._derive_heads(row["anchor_hash"])
            if len(heads) == 1:
                out.append((row["slug"], heads[0]))
        return out

    def folio_site_slug(self, content_hash: str) -> Optional[str]:
        """The site slug of a member folio (alphabetically-first if it is in several).

        Joins the folio's ``within`` edge to a ``station_slugs`` claim on the site's
        anchor (``anchor_hash = within.to_id`` — the degenerate site case, the site being
        its own genesis)."""
        row = self.conn.execute(
            """
            SELECT s.slug AS slug
            FROM threads t
            JOIN station_slugs s ON s.anchor_hash = t.to_id
            WHERE t.type = 'within' AND t.from_id = ?
            ORDER BY s.slug
            LIMIT 1
            """,
            (content_hash,),
        ).fetchone()
        return row["slug"] if row else None

    def folio_site_slugs(self, content_hashes: Optional[List[str]] = None) -> Dict[str, str]:
        """Map member folio hashes to their site slugs (alphabetically-first on multi-site
        via ``setdefault`` over ``ORDER BY slug``). ``None`` maps the whole corpus in one
        join; a hash list is chunked at 900 (for labelling a bounded result set)."""
        mapping: Dict[str, str] = {}
        if content_hashes is None:
            rows = self.conn.execute(
                """
                SELECT t.from_id AS folio, s.slug AS slug
                FROM threads t
                JOIN station_slugs s ON s.anchor_hash = t.to_id
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
                JOIN station_slugs s ON s.anchor_hash = t.to_id
                WHERE t.type = 'within' AND t.from_id IN ({placeholders})
                ORDER BY s.slug
                """,
                chunk,
            ).fetchall()
            for r in rows:
                mapping.setdefault(r["folio"], r["slug"])
        return mapping

    # --- manifests + constituent attribution (federation, Stage 3) ----------
    #
    # The signed-publish provenance sidecars: one ``manifests`` row per
    # (root, issuer, subject) proof; one ``constituent_attribution`` row per covered
    # folio/thread pointing at its FIRST covering manifest. Ported verbatim from
    # skein_next/store.py — the live station's federation surface, invariants (ST1-ST10)
    # preserved. The ingress is the sole writer; the read path (envelope.folio_verdict)
    # reads through get_constituent_proof.

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

    def all_manifests(self) -> List[Dict[str, Any]]:
        """Every manifest proof row — for the verify-cache backfill verb (VC9)."""
        return [dict(r) for r in self.conn.execute("SELECT * FROM manifests").fetchall()]

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

        The degrade is SCOPED TO READ-ONLY stores. A read_write store ALWAYS ensures
        the schema on open (``LogDatabase(station=True)`` births the station DDL), so a
        missing table there is a genuine fault — not a deploy-ordering race — and must
        RAISE rather than be silently masked. Only the read app (``read_only=True``) can legitimately
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
        role: str = "originator",
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

    def set_role(self, issuer: str, subject: str, role: str):
        """Change an ACTIVE binding's tier locally (a privileged rebind). Returns the
        updated ``Binding``, or ``None`` if there is no active binding. Refuses to touch
        the operator role in EITHER direction (``ValueError``) — installing or removing
        an operator goes through rotate-operator so the single-active-operator invariant
        (A7/D13) is never bypassed. Logs a 'rebound' event; created_at is preserved."""
        b = self.get_binding(issuer, subject)
        if b is None or b.revoked_at is not None:
            return None
        if b.role == "operator" or role == "operator":
            raise ValueError(
                "operator role changes go through rotate-operator, not rebind"
            )
        self.conn.execute(
            "UPDATE account_bindings SET role = ? WHERE issuer = ? AND subject = ?",
            (role, issuer, subject),
        )
        self._log_binding_event(
            issuer, subject, "rebound", role, b.vouched_by_issuer, b.vouched_by_subject
        )
        self._maybe_commit()
        return self.get_binding(issuer, subject)

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
        """All bindings as ``Binding`` objects (revoked included iff asked), ordered."""
        sql = "SELECT * FROM account_bindings"
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        sql += " ORDER BY role, issuer, subject"
        return [self._binding_from_row(r) for r in self.conn.execute(sql).fetchall()]

    def get_binding_events(
        self, issuer: Optional[str] = None, subject: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """The append-only binding audit trail, in insertion order (optionally filtered)."""
        sql = "SELECT * FROM binding_events"
        clauses = []
        params: List[Any] = []
        if issuer is not None:
            clauses.append("issuer = ?")
            params.append(issuer)
        if subject is not None:
            clauses.append("subject = ?")
            params.append(subject)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY event_seq"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # --- document grants + audit (the per-document delegation sidecar) -------
    #
    # A grant delegates a TO-end right on ONE lineage (anchor = its GENESIS) to a
    # grantee signer pair, for a KIND (supersede | site_contribute | site_edit). Read
    # LIVE per ingest inside the ingest transaction, never memoized (rotation-proof).
    # Grants authorize the TO-end ONLY (never the pure per-folio from-end). They do NOT
    # cascade on granter revocation; containment is the explicit
    # revoke_grants_vouched_by verb (§3.2).

    def _log_grant_event(
        self, grantee_issuer, grantee_subject, event, kind, anchor_hash,
        vouched_by_issuer, vouched_by_subject,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO grant_events
                (grantee_issuer, grantee_subject, event, kind, anchor_hash,
                 vouched_by_issuer, vouched_by_subject, at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (grantee_issuer, grantee_subject, event, kind, anchor_hash,
             vouched_by_issuer, vouched_by_subject, _now_iso()),
        )

    def add_grant(
        self,
        anchor_hash: str,
        grantee_issuer: str,
        grantee_subject: str,
        kind: str,
        vouched_by_issuer: Optional[str] = None,
        vouched_by_subject: Optional[str] = None,
    ) -> None:
        """Issue (or reactivate) a grant for (anchor, grantee, kind). A fresh tuple
        INSERTs with a 'granted' event; a revoked tuple REACTIVATES the SAME row
        (revoked_at back to NULL, created_at preserved) with a 'regranted' event; an
        already-active tuple is an idempotent no-op (no event). Mirrors add_binding's
        revoke/reactivate discipline so a re-grant after revocation is auditable."""
        existing = self.conn.execute(
            "SELECT created_at, revoked_at FROM document_grants "
            "WHERE anchor_hash = ? AND grantee_issuer = ? AND grantee_subject = ? AND kind = ?",
            (anchor_hash, grantee_issuer, grantee_subject, kind),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO document_grants
                    (anchor_hash, grantee_issuer, grantee_subject, kind,
                     vouched_by_issuer, vouched_by_subject, created_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (anchor_hash, grantee_issuer, grantee_subject, kind,
                 vouched_by_issuer, vouched_by_subject, _now_iso()),
            )
            self._log_grant_event(
                grantee_issuer, grantee_subject, "granted", kind, anchor_hash,
                vouched_by_issuer, vouched_by_subject,
            )
        elif existing["revoked_at"] is not None:
            self.conn.execute(
                """
                UPDATE document_grants
                   SET revoked_at = NULL, vouched_by_issuer = ?, vouched_by_subject = ?
                 WHERE anchor_hash = ? AND grantee_issuer = ? AND grantee_subject = ? AND kind = ?
                """,
                (vouched_by_issuer, vouched_by_subject,
                 anchor_hash, grantee_issuer, grantee_subject, kind),
            )
            self._log_grant_event(
                grantee_issuer, grantee_subject, "regranted", kind, anchor_hash,
                vouched_by_issuer, vouched_by_subject,
            )
        # else: already active -> idempotent no-op, no event
        self._maybe_commit()

    def has_active_grant(
        self, anchor_hash: str, grantee_issuer: str, grantee_subject: str, kind: str
    ) -> bool:
        """True iff (anchor, grantee, kind) has an ACTIVE (revoked_at NULL) grant. The
        live predicate the to-end authorization consults inside the ingest txn."""
        row = self.conn.execute(
            """
            SELECT 1 FROM document_grants
             WHERE anchor_hash = ? AND grantee_issuer = ? AND grantee_subject = ?
               AND kind = ? AND revoked_at IS NULL
             LIMIT 1
            """,
            (anchor_hash, grantee_issuer, grantee_subject, kind),
        ).fetchone()
        return row is not None

    def revoke_grant(
        self, anchor_hash: str, grantee_issuer: str, grantee_subject: str, kind: str
    ) -> bool:
        """Revoke ONE active grant (sets revoked_at; row stays). Returns False if there
        was no active grant to revoke."""
        cur = self.conn.execute(
            """
            UPDATE document_grants SET revoked_at = ?
             WHERE anchor_hash = ? AND grantee_issuer = ? AND grantee_subject = ?
               AND kind = ? AND revoked_at IS NULL
            """,
            (_now_iso(), anchor_hash, grantee_issuer, grantee_subject, kind),
        )
        if cur.rowcount == 0:
            self._maybe_commit()
            return False
        self._log_grant_event(
            grantee_issuer, grantee_subject, "revoked", kind, anchor_hash, None, None
        )
        self._maybe_commit()
        return True

    def revoke_grants_vouched_by(
        self, vouched_by_issuer: str, vouched_by_subject: str
    ) -> int:
        """Revoke EVERY active grant vouched by ``(vouched_by_issuer,
        vouched_by_subject)`` — the containment verb for a revoked/compromised granter
        (grants do NOT auto-cascade on granter revocation, §3.2). Returns the count
        revoked. Each revocation logs a 'revoked_containment' event."""
        rows = self.conn.execute(
            """
            SELECT anchor_hash, grantee_issuer, grantee_subject, kind
              FROM document_grants
             WHERE vouched_by_issuer = ? AND vouched_by_subject = ? AND revoked_at IS NULL
            """,
            (vouched_by_issuer, vouched_by_subject),
        ).fetchall()
        for r in rows:
            self.conn.execute(
                """
                UPDATE document_grants SET revoked_at = ?
                 WHERE anchor_hash = ? AND grantee_issuer = ? AND grantee_subject = ? AND kind = ?
                """,
                (_now_iso(), r["anchor_hash"], r["grantee_issuer"],
                 r["grantee_subject"], r["kind"]),
            )
            self._log_grant_event(
                r["grantee_issuer"], r["grantee_subject"], "revoked_containment",
                r["kind"], r["anchor_hash"], vouched_by_issuer, vouched_by_subject,
            )
        self._maybe_commit()
        return len(rows)

    def list_grants(self, include_revoked: bool = False) -> List[Dict[str, Any]]:
        """All grants (active only unless asked), ordered by anchor then grantee."""
        sql = "SELECT * FROM document_grants"
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        sql += " ORDER BY anchor_hash, grantee_issuer, grantee_subject, kind"
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def get_grant_events(
        self, grantee_issuer: Optional[str] = None, grantee_subject: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """The append-only grant audit trail, in insertion order (optionally filtered)."""
        sql = "SELECT * FROM grant_events"
        clauses = []
        params: List[Any] = []
        if grantee_issuer is not None:
            clauses.append("grantee_issuer = ?")
            params.append(grantee_issuer)
        if grantee_subject is not None:
            clauses.append("grantee_subject = ?")
            params.append(grantee_subject)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY event_seq"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # --- invites + audit (the onboarding sidecar) ----------------------------

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
        - ``'invalid_role'``     — the invite's role is not WIRE-REDEEMABLE
          (not in ``{originator, steward}``, §2). A self-service redeem must
          NEVER install an operator or administrator (the single-active-operator
          invariant, A7/D13; administrator is a privileged LOCAL bind only) — an
          operator is installed ONLY via the local ``init-operator`` /
          ``rotate-operator`` CLI path. NOTHING is burned or bound. The
          supported mint path already restricts ``--role`` to the wire-redeemable
          set, but this is the actual bind point, so it is the backstop that must
          refuse regardless of how a non-redeemable-role invite row came to exist
          (direct store use, migration, or future caller drift).
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
            if inv["role"] not in WIRE_REDEEMABLE_ROLES:
                return "invalid_role"
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
