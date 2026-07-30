"""
SKEIN storage layer: SQLite for logs/threads/versions/refs, JSON for roster/sites.
(The legacy folios table was retired in Phase 3a A5; folios are versions⋈refs.)
Multi-project support via ~/.skein/projects.json registry.
"""

import sqlite3
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from contextlib import contextmanager

from .models import AgentInfo, Site, Folio, Thread, LogLine, VersionView
from .utils import generate_thread_id
from .identity import compute_thread_hash

try:
    from . import identity

    KNURL_AVAILABLE = True
except ImportError:
    KNURL_AVAILABLE = False

logger = logging.getLogger(__name__)

# Phase 3a Class-A control taxonomy: the three thread types the A3 cutover
# re-anchored from the folio slug to the lineage genesis hash. The control VALUE
# readers (get_latest_statuses/assignments) key off this set. It is deliberately
# narrow: a genesis<->slug resolution restricted to control is what lets the
# value readers ignore edit edges that merely touch the genesis VERSION.
# 'archive' stays in the taxonomy for keying/display stability (threads_pk_swap
# derives its re-anchor set from here) even though the folio-archived FEATURE —
# its writer, reader, and refs column — was removed 2026-07-08 (never used).
CONTROL_THREAD_TYPES = ("status", "assignment", "archive")

# Phase 3b §6/§6.1: the genesis-anchored Class B structural edge types. The PK swap
# re-anchors these from the folio slug to the lineage genesis hash (exactly as A3 did
# for control), so the PRESENTATION reader must union + rewrite them back to slugs to
# keep GET /threads / /search byte-identical across the swap. Version-anchored edges
# (supersedes/reverted/published) are deliberately EXCLUDED: their endpoints are exact
# version hashes and a genesis->slug rewrite would corrupt an edge whose endpoint is
# the genesis version (whose content hash equals the lineage genesis_hash). This is
# the SINGLE SOURCE of the genesis-anchored set: skein.migrations.threads_pk_swap
# imports it as GENESIS_ANCHORED_TYPES (the migration re-anchor set), so the reader's
# rewrite set and the migration's re-anchor set can never drift apart (design §6/§6.1;
# a drift would silently drop a re-keyed edge from a slug query). Display-only — the
# value readers above still key off CONTROL_THREAD_TYPES alone.
CLASS_B_GENESIS_DISPLAY_TYPES = ("reference", "mention", "reply", "succession", "within")

# The full set get_threads_display unions + genesis->slug-rewrites: control (3a) plus
# the genesis-anchored Class B edges (3b). One source of truth for both halves of the
# reader (the UNION clause and the rewrite loop).
GENESIS_ANCHORED_DISPLAY_TYPES = CONTROL_THREAD_TYPES + CLASS_B_GENESIS_DISPLAY_TYPES


def ensure_aware(dt_value) -> Optional[datetime]:
    """Ensure a datetime value is timezone-aware (UTC). Handles strings and datetime objects."""
    if dt_value is None:
        return None
    if isinstance(dt_value, str):
        # Accept a trailing Z or z (canon._parse_timestamp accepts both); if the
        # two disagreed, a folio could get a hash the read path can't reproduce.
        s = dt_value
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        dt_value = datetime.fromisoformat(s)
    if isinstance(dt_value, datetime) and dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=timezone.utc)
    return dt_value


def compute_folio_hash(folio: Folio) -> str:
    """Compute a folio's ``sha256::`` content hash.

    Delegates to the canon/identity RSP (:func:`skein.identity.compute_folio_hash`)
    so there is exactly ONE folio-hash implementation; this only adapts a ``Folio``
    to the five-field mapping. ``created_at`` is passed as a datetime —
    ``normalize_created_at`` collapses any encoding to canonical UTC before hashing,
    so a hash never depends on how the timestamp happened to be expressed.
    """
    if not KNURL_AVAILABLE:
        return None
    return identity.compute_folio_hash(
        {
            "type": folio.type,
            "title": folio.title,
            "content": folio.content,
            "created_at": folio.created_at,
            "created_by": folio.created_by,
        }
    )


# ── Phase 2 read-flip: heads-only over the versions⋈refs join ────────────────
# Every content/identity read selects exactly one row per ref by joining refs to
# versions on the head predicate (refs.head_hash = versions.content_hash).
# Identity fields come from versions (v), control fields from refs (r); the
# reconstructed Folio is byte-identical to the old _row_to_folio off folios.
_HEAD_FROM = "FROM refs r JOIN versions v ON v.content_hash = r.head_hash"
_HEAD_SELECT = (
    "r.slug, v.type, r.site_id, v.created_at, v.created_by, v.title, v.content, "
    "r.target_agent, r.omlet, r.metadata, "
    "r.acknowledged_at, v.content_hash"
)


def _folio_from_head_row(row: "sqlite3.Row") -> Folio:
    """Reconstruct a Folio from a joined refs⋈versions head row (column names per
    _HEAD_SELECT). status/assigned_to are NOT read here — they are thread-derived
    (the control cache columns were dropped in the threads-only contraction), and
    the API read surfaces overlay them via enrich_folios_with_status. The
    constructed object carries the model defaults ('open'/None) until enriched."""
    metadata = json.loads(row["metadata"]) if row["metadata"] else {}
    return Folio(
        folio_id=row["slug"],
        type=row["type"],
        site_id=row["site_id"],
        created_at=ensure_aware(row["created_at"]),
        created_by=row["created_by"],
        title=row["title"],
        content=row["content"],
        target_agent=row["target_agent"],
        omlet=row["omlet"],
        metadata=metadata,
        acknowledged_at=ensure_aware(row["acknowledged_at"]),
        content_hash=row["content_hash"],
    )


# Project Registry


def skein_home() -> Path:
    """Resolve the SKEIN home directory — the ``~/.skein`` tree that holds the
    project registry (``projects.json``).

    Honors the ``SKEIN_HOME`` environment variable so the test suite (and any
    sandboxed run) can redirect the whole tree without touching the real
    ``~/.skein``; falls back to ``~/.skein``. Read fresh on every call so a
    mid-process ``os.environ`` change — a server spawned as a subprocess that
    inherits the var, or a test setting it — takes effect immediately.
    """
    override = os.environ.get("SKEIN_HOME")
    return Path(override) if override else Path.home() / ".skein"


# A timestamped registry backup is exactly
# ``projects.json.bak-YYYYmmdd-HHMMSS-ffffff`` — microseconds included so two saves
# within the SAME UTC second stamp DISTINCT backups. Without them a same-second
# double-save collides on one name and shutil.copy2 silently overwrites the good
# pre-image, defeating the backup. The %f field is zero-padded to a fixed six
# digits, so the whole stamp still sorts lexically in chronological order for the
# prune. Only these are pruned; manually-named backups (``.bak-fix``,
# ``.bak-pre-gnomon``) never match this pattern and are left untouched.
_REGISTRY_BACKUP_RE = re.compile(r"\.bak-\d{8}-\d{6}-\d{6}$")
_REGISTRY_BACKUP_KEEP = 5


def _prune_registry_backups(registry_file: Path) -> None:
    """Keep only the newest ``_REGISTRY_BACKUP_KEEP`` timestamped backups beside
    ``registry_file``. The fixed-width UTC stamp sorts lexically in chronological
    order, so the tail of the sorted list is the oldest. Non-timestamped backups
    do not match :data:`_REGISTRY_BACKUP_RE` and are never considered.
    """
    stamped = sorted(
        p
        for p in registry_file.parent.glob(f"{registry_file.name}.bak-*")
        if _REGISTRY_BACKUP_RE.search(p.name)
    )
    for stale in stamped[:-_REGISTRY_BACKUP_KEEP]:
        stale.unlink(missing_ok=True)


def save_project_registry(data: Dict[str, Any]) -> None:
    """Atomically persist the project registry to ``<SKEIN_HOME>/projects.json``.

    The registry maps every project to its data dir, so a truncate-then-write
    (the naive ``open(path, "w")``) is dangerous: a concurrent reader can catch
    the file mid-write, and a writer that starts from an empty base destroys it
    outright (issue-20260709-zl71 deregistered all 50 projects this way). This
    makes a write safe two ways, deliberately WITHOUT file locking — last-write-
    wins on a genuinely concurrent registration is accepted, and the backups
    bound the damage:

      1. If the registry already exists, snapshot it to
         ``projects.json.bak-<UTC YYYYmmdd-HHMMSS-ffffff>`` in the same directory
         first, then prune those timestamped snapshots to the newest
         ``_REGISTRY_BACKUP_KEEP``. Manually-named backups are spared.
      2. Write the new content to a UNIQUE temp file in the SAME directory (same
         filesystem, so the rename is atomic) and ``os.replace`` it onto
         ``projects.json``. A reader only ever sees the complete old file or the
         complete new one — never a half-written truncation — and concurrent
         writers stage to distinct temps, so their bytes can't interleave.

    ``data`` is the full top-level object (``{"projects": {...}}``), matching what
    :func:`load_project_registry` reads back.
    """
    home = skein_home()
    home.mkdir(parents=True, exist_ok=True)
    registry_file = home / "projects.json"

    # (1) Snapshot the current file before touching it, then prune. Microseconds
    # (%f) go into the stamp so a same-second double-save stages to two distinct
    # backups instead of colliding — the zero-padded field keeps name order ==
    # time order for the prune.
    if registry_file.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        backup = registry_file.with_name(f"{registry_file.name}.bak-{stamp}")
        shutil.copy2(registry_file, backup)
        _prune_registry_backups(registry_file)

    # (2) Stage to a unique temp in the same dir, then atomically replace.
    fd, tmp_name = tempfile.mkstemp(dir=str(home), prefix=".projects.json.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, registry_file)
    except Exception:
        tmp.unlink(missing_ok=True)  # never leave a stray temp on a failed write
        raise


def save_project_registry_text(text: str) -> None:
    """Atomically write *raw* ``text`` to ``<SKEIN_HOME>/projects.json``, byte for
    byte — no parse, no re-serialize, no reordering.

    :func:`save_project_registry` takes a dict and re-encodes it, which is correct
    for a genuine save but wrong for *restoring* a captured pre-image: reserializing
    rewrites a hand-formatted, compact, or differently-ordered file even though its
    meaning is unchanged, and ``json.loads`` chokes on an empty (0-byte) registry.
    This preserves the exact bytes. An empty string writes a 0-byte file — a
    byte-verbatim restore of a pre-existing empty registry — so a caller that means
    "no file" must unlink instead of passing ``""``.

    Same atomicity as :func:`save_project_registry` minus the backup rotation: stage
    to a UNIQUE temp in the SAME directory (same filesystem, atomic rename) and
    ``os.replace`` onto ``projects.json``, so a reader only ever sees the whole old
    file or the whole new one. ``skein_home()`` is resolved fresh at call time.
    """
    home = skein_home()
    home.mkdir(parents=True, exist_ok=True)
    registry_file = home / "projects.json"
    fd, tmp_name = tempfile.mkstemp(dir=str(home), prefix=".projects.json.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, registry_file)
    except Exception:
        tmp.unlink(missing_ok=True)  # never leave a stray temp on a failed write
        raise


def load_project_registry() -> Dict[str, Dict[str, Any]]:
    """Load project registry from ``<SKEIN_HOME>/projects.json`` (default
    ``~/.skein/projects.json``)."""
    registry_file = skein_home() / "projects.json"
    if not registry_file.exists():
        logger.warning("No project registry at %s, using default data dir", registry_file)
        return {}

    try:
        with open(registry_file) as f:
            data = json.load(f)
            return data.get("projects", {})
    except Exception as e:
        logger.error(f"Failed to load project registry: {e}")
        return {}


def get_data_dir_for_project(project_id: Optional[str] = None) -> Path:
    """
    Get data directory for a project.

    If project_id is provided, looks up in registry.
    Otherwise uses default data directory.
    """
    if project_id:
        registry = load_project_registry()
        if project_id in registry:
            data_dir = Path(registry[project_id]["data_dir"])
            data_dir.mkdir(parents=True, exist_ok=True)
            return data_dir
        else:
            raise ValueError(f"Project '{project_id}' not found in registry")

    # No project_id provided - this shouldn't happen in normal operation
    raise ValueError("No project_id provided and no default available")


def search_folio_across_projects(
    folio_id: str, current_project_id: Optional[str] = None
) -> Optional[Dict[str, str]]:
    """
    Search for a folio across all registered projects (except the current one).

    Returns {"project_name": ..., "project_path": ...} if found, None otherwise.
    Uses raw SQLite queries to avoid LogDatabase init overhead.
    """
    registry = load_project_registry()
    for project_name, project_info in registry.items():
        if project_name == current_project_id:
            continue
        try:
            data_dir = Path(project_info["data_dir"])
            db_path = data_dir / "skein.db"
            if not db_path.exists():
                continue
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                # Existence is per-slug on refs (the heads layer).
                cursor = conn.execute(
                    "SELECT 1 FROM refs WHERE slug = ? LIMIT 1", (folio_id,)
                )
                if cursor.fetchone():
                    project_path = project_info.get("path", str(data_dir))
                    return {"project_name": project_name, "project_path": project_path}
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.debug(
                f"Skipping project '{project_name}' during cross-project folio search: {e}"
            )
            continue
    return None


def get_project_store(project_name: str) -> Optional["JSONStore"]:
    """
    Get a JSONStore for a named project from the registry.

    Returns None if the project is not found or has no data directory.
    """
    registry = load_project_registry()
    project_info = registry.get(project_name)
    if not project_info:
        return None
    data_dir = Path(project_info["data_dir"])
    if not data_dir.exists():
        return None
    return JSONStore(data_dir)


def get_project_last_activity_timestamps() -> Dict[str, int]:
    """Return mapping of project_id -> latest folio created_at as unix seconds (int).

    Iterates the project registry and aggregates the latest folio created_at across
    each project's sites. Projects whose data dir or skein.db doesn't exist, projects
    with zero folios, and projects whose database is unreadable are omitted (logged
    and skipped — a single bad project does not break the response).
    """
    registry = load_project_registry()
    result: Dict[str, int] = {}
    for project_name, project_info in registry.items():
        try:
            data_dir = Path(project_info["data_dir"])
            db_path = data_dir / "skein.db"
            if not db_path.exists():
                continue
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                # Latest head activity = MAX(created_at) over heads (genesis ts,
                # inherited via the versions/refs join).
                cursor = conn.execute("SELECT MAX(v.created_at) " + _HEAD_FROM)
                row = cursor.fetchone()
                if not row or row[0] is None:
                    continue
                dt = ensure_aware(row[0])
                if dt is None:
                    continue
                result[project_name] = int(dt.timestamp())
            finally:
                conn.close()
        except (sqlite3.Error, KeyError, OSError, ValueError) as e:
            logger.debug(
                f"Skipping project '{project_name}' during cross-project timestamps: {e}"
            )
            continue
    return result


def resolve_folio_across_projects(
    folio_id: str, current_project_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Search for a folio across all registered projects (except the current one)
    and return the actual folio data along with source project info.

    Returns {"folio": Folio, "project_name": str} if found, None otherwise.
    Uses raw SQLite for the existence check, then constructs a Folio from the row.
    """
    registry = load_project_registry()
    for project_name, project_info in registry.items():
        if project_name == current_project_id:
            continue
        try:
            data_dir = Path(project_info["data_dir"])
            db_path = data_dir / "skein.db"
            if not db_path.exists():
                continue
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                # Resolve the slug's HEAD via the join (identity from versions,
                # control from refs).
                row = conn.execute(
                    f"SELECT {_HEAD_SELECT} {_HEAD_FROM} WHERE r.slug = ? LIMIT 1",
                    (folio_id,),
                ).fetchone()
                folio = _folio_from_head_row(row) if row else None
                if folio is not None:
                    logger.info(
                        f"Resolved {folio_id} from project '{project_name}' (cascade)"
                    )
                    return {"folio": folio, "project_name": project_name}
            finally:
                conn.close()
        except (sqlite3.Error, KeyError) as e:
            logger.debug(
                f"Skipping project '{project_name}' during cross-project folio resolve: {e}"
            )
            continue
    return None


# Legacy module-level variables removed - use project-specific instances via get_data_dir_for_project()


class StoreStationIndex:
    """Concrete skein.address.StationIndex backed by the live versions/refs tables.

    Lengthens a short hash against ALL versions (including superseded ones — a
    durable by-hash citation must resolve forever) and resolves a slug to its
    lineage head. Opens its own read-only connection per call (resolution is a
    cache-miss path, not hot). Digests are framed `sha256::<hex>` in the store;
    this returns/consumes the BARE hex the address resolver works in."""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def _ro(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)

    def folios_with_prefix(self, algo: str, prefix: str) -> List[str]:
        # prefix is a validated lowercase-hex short digest (no LIKE wildcards).
        framed = f"{algo}::{prefix}%"
        conn = self._ro()
        try:
            rows = conn.execute(
                "SELECT content_hash FROM versions WHERE content_hash LIKE ?",
                (framed,),
            ).fetchall()
        finally:
            conn.close()
        out: List[str] = []
        for (ch,) in rows:
            a, _, hexpart = ch.partition("::")
            if a == algo:
                out.append(hexpart)
        return out

    def head_of_slug(self, slug: str) -> Optional[str]:
        conn = self._ro()
        try:
            row = conn.execute(
                "SELECT head_hash FROM refs WHERE slug = ?", (slug,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        _, _, hexpart = row[0].partition("::")
        return hexpart


# SQLite Database for Logs


class LogDatabase:
    """SQLite database for log storage and querying."""

    def __init__(self, db_path: Path = None, station: bool = False):
        # station=True selects the STATION role (Fork B, station re-home Stage 1):
        # the SAME versions object store, but born with the post-swap threads DDL
        # (content-addressed dedup from row zero) and the federation + station_slugs
        # sidecar tables. A workbench db (station=False) is byte-identical to before
        # — the base DDL and its behavior are untouched. Role is construction-time;
        # a given instance is one or the other.
        self.db_path = db_path
        self.station = station
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stream_id TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    level TEXT,
                    source TEXT,
                    message TEXT NOT NULL,
                    metadata JSON
                )
            """
            )

            # Create indexes
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_stream_time
                ON logs(stream_id, timestamp DESC)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_stream_level
                ON logs(stream_id, level)
            """
            )

            # Full-text search
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS logs_fts
                USING fts5(message, content=logs)
            """
            )

            # Screenshots table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS screenshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    screenshot_id TEXT UNIQUE NOT NULL,
                    strand_id TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    turn_number INTEGER,
                    label TEXT,
                    file_path TEXT NOT NULL,
                    file_size INTEGER,
                    metadata JSON
                )
            """
            )

            # Create indexes for screenshots
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_screenshots_strand
                ON screenshots(strand_id, timestamp DESC)
            """
            )

            # Sacks table - stores yields from chain participants
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sacks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sack_id TEXT UNIQUE NOT NULL,
                    chain_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    agent_id TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,

                    -- Yield fields (structured, queryable)
                    status TEXT,
                    outcome TEXT,
                    artifacts JSON,
                    notes TEXT,

                    -- Enrichment (added by system)
                    duration_seconds INTEGER,
                    tokens_used INTEGER,
                    shard_path TEXT,
                    tender_id TEXT,

                    -- Catchall for future fields
                    metadata JSON
                )
            """
            )

            # Create indexes for sacks
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sacks_chain
                ON sacks(chain_id, timestamp)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sacks_agent
                ON sacks(agent_id)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sacks_status
                ON sacks(status)
            """
            )

            # Threads table. A workbench db is born PRE-swap (thread_id PK, nullable
            # non-unique thread_hash) — unchanged, migrated to the thread_hash PK by
            # skein.migrations.threads_pk_swap on live dbs. A STATION db is born
            # POST-swap (thread_hash PK) — the identical end-state that migration
            # produces — so a byte-identical wire thread dedups (INSERT OR IGNORE on
            # the hash) from row zero instead of inserting a duplicate. We do NOT bake
            # the post-swap shape into the shared base DDL (station re-home Stage 1,
            # threads-DDL decision (b)); it is the station branch only.
            if self.station:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS threads (
                        thread_hash TEXT PRIMARY KEY,
                        thread_id   TEXT NOT NULL,
                        from_id     TEXT NOT NULL,
                        to_id       TEXT NOT NULL,
                        type        TEXT NOT NULL,
                        content     TEXT,
                        weaver      TEXT,
                        created_at  DATETIME NOT NULL
                    )
                """
                )
            else:
                # The workbench pre-swap DDL, byte-for-byte master's text. SQLite stores
                # the CREATE statement verbatim in sqlite_master.sql (internal indentation
                # preserved), so this string's content is dedented independently of the
                # enclosing if/else to keep a workbench-born threads table's stored DDL
                # identical to one born on master — the locked byte-identity invariant
                # (test_workbench_threads_ddl_byte_equal).
                conn.execute(
                    """
                CREATE TABLE IF NOT EXISTS threads (
                    thread_id TEXT PRIMARY KEY,
                    from_id TEXT NOT NULL,
                    to_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    content TEXT,
                    weaver TEXT,
                    created_at DATETIME NOT NULL,
                    thread_hash TEXT
                )
            """
                )

            # A1 (Phase 3a): thread_hash — the content address for every edge.
            # Nullable and NON-unique in 3a (the UNIQUE/PK swap is 3b). The CREATE
            # above only fires on a fresh db, so add it via ALTER for existing dbs.
            _thread_cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
            if "thread_hash" not in _thread_cols:
                conn.execute("ALTER TABLE threads ADD COLUMN thread_hash TEXT")

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_threads_from
                ON threads(from_id)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_threads_to
                ON threads(to_id)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_threads_type
                ON threads(type)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_threads_created
                ON threads(created_at)
            """
            )

            # Compound indexes for batch status/assignment lookups
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_threads_to_type_created
                ON threads(to_id, type, created_at DESC)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_threads_from_type_created
                ON threads(from_id, type, created_at DESC)
            """
            )

            # Phase 3a A5: the legacy ``folios`` table (and its indexes) is
            # retired. Reads have been off folios since the commit-C read-flip and
            # the writers (save_folio/move_folio) no longer touch it, so fresh dbs
            # are born folios-free. Existing dbs keep a now-unwritten vestigial
            # table until the destructive live DROP (A5 Part 2).

            # Phase 3 step 0: folios_fts (legacy external-content FTS5) and its
            # three sync triggers (folios_ai/ad/au) are retired — search reads
            # versions_fts since the commit-C read-flip, and the triggers' delete
            # commands corrupted the index under INSERT-OR-REPLACE rowid churn
            # (finding-20260629-hkgv). Existing dbs are dropped by
            # skein.migrations.retire_folios_fts; no DDL recreates them here.

            # ── Phase 2: content-addressed versions + slug-as-head refs ─────────
            # versions: immutable, append-only object store keyed by content hash.
            # Only the five hashed identity fields live here; a row is written once
            # and never updated or deleted, so its PK is verifiable from its own
            # columns (identity.compute_folio_hash). Two lineages that reach
            # byte-identical content share one row (content-addressing dedups).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS versions (
                    content_hash TEXT PRIMARY KEY,
                    type         TEXT NOT NULL,
                    title        TEXT NOT NULL,
                    content      TEXT NOT NULL,
                    created_at   DATETIME NOT NULL,
                    created_by   TEXT NOT NULL
                )
            """
            )

            # refs: mutable, local, never federated — the naming + lineage layer.
            # slug is the everyday human handle (folio_id); genesis_hash is the
            # stable lineage identity (survives edits); head_hash moves on every
            # identity-changing edit. The remaining local fields (site_id/
            # target_agent/omlet/acknowledged_at/metadata) are workbench workflow
            # truth with no thread counterpart. status/assigned_to/archived are
            # NOT here: control state is thread-derived (genesis-keyed control
            # threads are the truth; reads reduce via get_latest_statuses/
            # assignments) — the copy-of-column cache was dropped in the
            # threads-only contraction (2026-07-08, drop_refs_control migration).
            # created_by is hashed identity and lives on versions, NOT here; type
            # is also identity-only (the edit rule freezes a lineage's type).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS refs (
                    slug            TEXT PRIMARY KEY,
                    genesis_hash    TEXT NOT NULL,
                    head_hash       TEXT NOT NULL,
                    site_id         TEXT NOT NULL,
                    target_agent    TEXT,
                    omlet           TEXT,
                    acknowledged_at DATETIME,
                    metadata        JSON
                )
            """
            )

            for col in ("site_id", "head_hash", "genesis_hash"):
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_refs_{col} ON refs({col})"
                )

            # versions_fts: FTS5 external-content over versions. AFTER INSERT
            # trigger ONLY — versions are append-only (never updated, never
            # deleted), so no _au/_ad trigger is needed. It indexes every version
            # including superseded ones; search restricts to heads via the refs
            # join (the head predicate is refs.head_hash = versions.content_hash).
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS versions_fts
                USING fts5(
                    content_hash,
                    title,
                    content,
                    content=versions,
                    content_rowid=rowid
                )
            """
            )
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS versions_ai AFTER INSERT ON versions BEGIN
                    INSERT INTO versions_fts(rowid, content_hash, title, content)
                    VALUES (new.rowid, new.content_hash, new.title, new.content);
                END
            """)

            if self.station:
                self._init_station_schema(conn)

            conn.commit()

    def _init_station_schema(self, conn):
        """Create the station-only sidecar tables (station re-home Stage 1).

        Ported from skein_next/store.py — the live station's schema, invariants
        preserved verbatim. These exist ONLY on a station db; a workbench db never
        sees them. Folios live in the shared ``versions`` table (refs-free on a
        station); threads live in the shared post-swap ``threads`` table. This lays
        the schema — the federation ACCESSORS ride with their servers in later stages;
        the station folio/thread/slug accessors are Stage 1b/1c.
        """
        # station_slugs — genesis-anchored naming (brief-20260708-31bu). A claim is
        # (slug, anchor_hash = the lineage GENESIS content hash, claimed_by signer,
        # scope). Resolution DERIVES the head by walking supersedes forward from the
        # anchor (1c) — never a stored mutable head, never refs (Risk-3). Replaces
        # skein_next's flat `slugs` table; site slugs are the degenerate case.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS station_slugs (
                slug                 TEXT PRIMARY KEY,
                anchor_hash          TEXT NOT NULL,
                claimed_by_issuer    TEXT,
                claimed_by_subject   TEXT,
                scope                TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_station_slugs_anchor "
            "ON station_slugs(anchor_hash)"
        )

        # aliases — legacy-id -> content_hash (server-live via resolve_alias).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS aliases (
                legacy_id    TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL
            )
            """
        )

        # manifests — one row per (manifest, signer). The (root, issuer, subject)
        # TRIPLE PK lets two bound signers each retain a proof over the identical
        # constituent set (ST3); a bare-root PK would shadow the second.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manifests (
                root            TEXT,
                manifest_hash   TEXT,
                descriptor_json TEXT NOT NULL,
                leaf_list_json  TEXT NOT NULL,
                bundle_json     TEXT NOT NULL,
                issuer          TEXT,
                subject         TEXT,
                leaf_count      INTEGER,
                created_at      TEXT,
                PRIMARY KEY (root, issuer, subject)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_manifests_hash ON manifests(manifest_hash)"
        )

        # constituent_attribution — one row per covered constituent (folio hash OR
        # thread hash), pointing at its FIRST covering manifest (first-wins, Q3). The
        # FK is the full proof triple (proof-signer == display-signer, ST6).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS constituent_attribution (
                constituent_hash TEXT PRIMARY KEY,
                kind             TEXT NOT NULL,
                root             TEXT NOT NULL,
                issuer           TEXT NOT NULL,
                subject          TEXT NOT NULL,
                created_at       TEXT,
                FOREIGN KEY (root, issuer, subject)
                    REFERENCES manifests(root, issuer, subject)
            )
            """
        )

        # account_bindings — (issuer, subject) authorized as operator|author;
        # revoked_at NULL = active; created_at preserved across revoke/reactivate
        # (B5). Single-active-operator is LOGIC-enforced, not a DB constraint (B47).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_bindings (
                issuer            TEXT,
                subject           TEXT,
                role              TEXT CHECK (role IN
                    ('operator', 'administrator', 'steward', 'originator')),
                vouched_by_issuer  TEXT,
                vouched_by_subject TEXT,
                created_at        TEXT,
                revoked_at        TEXT,
                PRIMARY KEY (issuer, subject)
            )
            """
        )

        # binding_events — append-only binding audit (INSERT-only).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS binding_events (
                event_seq          INTEGER PRIMARY KEY,
                issuer             TEXT,
                subject            TEXT,
                event              TEXT,
                role               TEXT,
                vouched_by_issuer  TEXT,
                vouched_by_subject TEXT,
                at                 TEXT
            )
            """
        )

        # invites — one-time tokens; token_hash = SHA-256 of a >=256-bit CSPRNG token
        # (plaintext NEVER stored). Expiry enforced in SQL; redeem BURNS exactly once
        # (INV-2); failed_attempts caps the expensive-verify flood (INV-5).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS invites (
                token_hash            TEXT PRIMARY KEY,
                role                  TEXT NOT NULL CHECK (role IN
                    ('operator', 'administrator', 'steward', 'originator')),
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
            )
            """
        )

        # invite_events — append-only invite audit (mirrors binding_events).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS invite_events (
                event_seq    INTEGER PRIMARY KEY,
                token_hash   TEXT,
                event        TEXT,
                bound_issuer TEXT,
                bound_subject TEXT,
                at           TEXT,
                detail       TEXT
            )
            """
        )

        # verify_cache — the expensive Sigstore SIGNATURE verdict ONLY, keyed
        # (manifest_hash, bundle_hash). Membership + binding are recomputed live per
        # read. Ingress is the sole writer; the read app opens ro. A recoverable
        # status (TRUST_ROOT_STALE / OFFLINE_NO_TRUSTED_ROOT) is NEVER cached.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS verify_cache (
                manifest_hash TEXT,
                bundle_hash   TEXT,
                status        TEXT,
                issuer        TEXT,
                subject       TEXT,
                verified_at   TEXT,
                PRIMARY KEY (manifest_hash, bundle_hash)
            )
            """
        )

        # document_grants — a per-document delegation of a TO-end right (rev 6 §3.2).
        # Keyed by (anchor_hash = lineage GENESIS content hash, grantee issuer+subject,
        # kind). vouched_by is the granting binding (an administrator or the operator).
        # revoked_at NULL = active; live-revocable, rotation-proof, READ LIVE per ingest
        # inside the ingest transaction — never memoized. A grant authorizes the TO-end
        # ONLY, never the from-end (which is pure per-folio ownership). Grants do NOT
        # cascade on granter revocation; containment is the explicit
        # revoke-all-grants-by verb. Kinds day-one: supersede, site_contribute, site_edit.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS document_grants (
                anchor_hash        TEXT NOT NULL,
                grantee_issuer     TEXT NOT NULL,
                grantee_subject    TEXT NOT NULL,
                kind               TEXT NOT NULL CHECK (kind IN
                    ('supersede', 'site_contribute', 'site_edit')),
                vouched_by_issuer  TEXT,
                vouched_by_subject TEXT,
                created_at         TEXT,
                revoked_at         TEXT,
                PRIMARY KEY (anchor_hash, grantee_issuer, grantee_subject, kind)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_document_grants_grantee "
            "ON document_grants(grantee_issuer, grantee_subject)"
        )

        # grant_events — append-only grant audit (binding_events shape). One row per
        # issuance / revocation / bulk-revocation.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS grant_events (
                event_seq          INTEGER PRIMARY KEY,
                grantee_issuer     TEXT,
                grantee_subject    TEXT,
                event              TEXT,
                kind               TEXT,
                anchor_hash        TEXT,
                vouched_by_issuer  TEXT,
                vouched_by_subject TEXT,
                at                 TEXT
            )
            """
        )

        # The <=1-parent invariant, enforced at the SCHEMA (rev 6 §5.3): at most one
        # supersedes edge per from_id (one new version supersedes at most one parent).
        # A partial UNIQUE index aborts a second parent if the ingress admission check
        # is ever bypassed. NOT UNIQUE(to_id) — that would forbid a legitimate FORK
        # (two children sharing one parent). Station-only: the signed content-hash
        # graph lives here, not on the slug-keyed workbench threads table.
        #
        # On a FRESH corpus (empty threads) this always succeeds. On a PRE-rev6 corpus
        # that still contains merges (>1 supersedes parent per from_id) it would raise —
        # but the perm_model_rev6 migration quarantines those merges and then creates the
        # index, and the ingress boot guard refuses to serve an unmigrated corpus. So a
        # raise here is not fatal: skip (loudly) and let the migration build it.
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_threads_supersedes_one_parent "
                "ON threads(from_id) WHERE type = 'supersedes'"
            )
        except sqlite3.IntegrityError:
            logger.warning(
                "idx_threads_supersedes_one_parent not created: the corpus contains "
                "pre-existing supersedes merges; run skein.migrations.perm_model_rev6 to "
                "quarantine them and build the index."
            )

    @contextmanager
    def _get_connection(self):
        """Get database connection context manager.

        Journal mode is role-dependent and applied on EVERY connection (not only at schema
        birth): a workbench db is WAL (unchanged); a STATION db is rollback-journal so it
        can be served ``:ro`` (WAL needs ``-wal``/``-shm`` sidecars a read-only mount can't
        create). Gating every connection — not just ``_init_db``'s — is deliberate: if only
        birth were rollback-journal, any later method call on a ``station=True`` instance
        would flip the corpus back to WAL (the ``database is locked`` / broken-``:ro``-mount
        class the split exists to avoid). A ``busy_timeout`` lets concurrent station writers
        serialize instead of failing instantly on the write lock.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        if self.station:
            conn.execute("PRAGMA busy_timeout=5000")
        else:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _immediate_txn(self):
        """A write connection that holds BEGIN IMMEDIATE across the whole block.

        save_folio reads the ref head, then conditionally mints a version and
        moves the head — a read-then-write that must not race a concurrent writer
        (a TOCTOU on head_hash would mint a duplicate or skip a supersedes edge).
        BEGIN IMMEDIATE takes the write lock BEFORE the first read, so the whole
        versions/refs/threads maintenance is one atomic transaction.
        Commits on a clean exit, rolls back on any exception."""
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def add_logs(self, stream_id: str, source: str, lines: List[Dict[str, Any]]) -> int:
        """Add log lines to database."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            count = 0

            for line in lines:
                cursor.execute(
                    """
                    INSERT INTO logs (stream_id, level, source, message, metadata)
                    VALUES (?, ?, ?, ?, ?)
                """,
                    (
                        stream_id,
                        line.get("level", "INFO"),
                        source,
                        line.get("message", ""),
                        json.dumps(line.get("metadata", {})),
                    ),
                )
                count += 1

            conn.commit()
            return count

    def get_logs(
        self,
        stream_id: str,
        since: Optional[str] = None,
        level: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 1000,
    ) -> List[LogLine]:
        """Query logs with filters."""
        with self._get_connection() as conn:
            query = "SELECT * FROM logs WHERE stream_id = ?"
            params = [stream_id]

            if since:
                query += " AND timestamp >= datetime(?)"
                params.append(since)

            if level:
                query += " AND level = ?"
                params.append(level)

            if search:
                # Use FTS for full-text search
                query = """
                    SELECT logs.* FROM logs
                    JOIN logs_fts ON logs.rowid = logs_fts.rowid
                    WHERE stream_id = ? AND logs_fts MATCH ?
                """
                params = [stream_id, search]

            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

            return [
                LogLine(
                    id=row["id"],
                    stream_id=row["stream_id"],
                    timestamp=ensure_aware(row["timestamp"]),
                    level=row["level"],
                    source=row["source"],
                    message=row["message"],
                    metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                )
                for row in rows
            ]

    def get_streams(self) -> List[Dict[str, Any]]:
        """Get list of all log streams."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT
                    stream_id,
                    COUNT(*) as line_count,
                    MIN(timestamp) as first_log,
                    MAX(timestamp) as last_log
                FROM logs
                GROUP BY stream_id
                ORDER BY last_log DESC
            """
            )

            return [dict(row) for row in cursor.fetchall()]

    def add_screenshot(
        self,
        screenshot_id: str,
        strand_id: str,
        turn_number: Optional[int],
        label: str,
        file_path: str,
        file_size: int,
        metadata: Dict[str, Any],
    ) -> bool:
        """Add screenshot metadata to database."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO screenshots (screenshot_id, strand_id, turn_number, label, file_path, file_size, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    screenshot_id,
                    strand_id,
                    turn_number,
                    label,
                    file_path,
                    file_size,
                    json.dumps(metadata),
                ),
            )
            conn.commit()
            return True

    def get_screenshots(
        self,
        strand_id: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Query screenshots with filters."""
        with self._get_connection() as conn:
            query = "SELECT * FROM screenshots WHERE 1=1"
            params = []

            if strand_id:
                query += " AND strand_id = ?"
                params.append(strand_id)

            if since:
                query += " AND timestamp >= datetime(?)"
                params.append(since)

            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

            return [dict(row) for row in rows]

    def get_screenshot(self, screenshot_id: str) -> Optional[Dict[str, Any]]:
        """Get specific screenshot by ID."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM screenshots WHERE screenshot_id = ?", (screenshot_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    # Sack Operations

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
        """
        Add a yield to the sack for a chain.

        Args:
            sack_id: Unique yield ID (e.g., 'yield-20251206-abc')
            chain_id: Chain this yield belongs to
            task_id: Which task produced this yield
            agent_id: Agent that produced the yield
            status: Yield status (complete/partial/blocked)
            outcome: What was accomplished
            artifacts: List of SKEIN artifact IDs (tender-xyz, finding-abc)
            notes: Context for next agent
            duration_seconds: How long the task took
            tokens_used: Token consumption
            shard_path: Path to shard worktree if used
            tender_id: Tender folio ID if work was tendered
            metadata: Additional metadata

        Returns:
            True on success
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO sacks (
                    sack_id, chain_id, task_id, agent_id,
                    status, outcome, artifacts, notes,
                    duration_seconds, tokens_used, shard_path, tender_id,
                    metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    sack_id,
                    chain_id,
                    task_id,
                    agent_id,
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
            conn.commit()
            return True

    def get_chain_yields(self, chain_id: str) -> List[Dict[str, Any]]:
        """
        Get all yields in a chain, ordered by timestamp.

        Args:
            chain_id: The chain to query

        Returns:
            List of yield dicts in execution order
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM sacks WHERE chain_id = ? ORDER BY timestamp", (chain_id,)
            )
            rows = cursor.fetchall()

            results = []
            for row in rows:
                yield_dict = dict(row)
                # Parse JSON fields
                if yield_dict.get("artifacts"):
                    yield_dict["artifacts"] = json.loads(yield_dict["artifacts"])
                if yield_dict.get("metadata"):
                    yield_dict["metadata"] = json.loads(yield_dict["metadata"])
                results.append(yield_dict)

            return results

    def get_yield(self, sack_id: str) -> Optional[Dict[str, Any]]:
        """Get specific yield by ID."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM sacks WHERE sack_id = ?", (sack_id,))
            row = cursor.fetchone()
            if not row:
                return None

            yield_dict = dict(row)
            if yield_dict.get("artifacts"):
                yield_dict["artifacts"] = json.loads(yield_dict["artifacts"])
            if yield_dict.get("metadata"):
                yield_dict["metadata"] = json.loads(yield_dict["metadata"])
            return yield_dict

    def get_yields_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Get yields by status (e.g., find all blocked work)."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM sacks WHERE status = ? ORDER BY timestamp DESC",
                (status,),
            )
            rows = cursor.fetchall()

            results = []
            for row in rows:
                yield_dict = dict(row)
                if yield_dict.get("artifacts"):
                    yield_dict["artifacts"] = json.loads(yield_dict["artifacts"])
                if yield_dict.get("metadata"):
                    yield_dict["metadata"] = json.loads(yield_dict["metadata"])
                results.append(yield_dict)

            return results

    def get_agent_yields(self, agent_id: str) -> List[Dict[str, Any]]:
        """Get all yields by a specific agent."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM sacks WHERE agent_id = ? ORDER BY timestamp DESC",
                (agent_id,),
            )
            rows = cursor.fetchall()

            results = []
            for row in rows:
                yield_dict = dict(row)
                if yield_dict.get("artifacts"):
                    yield_dict["artifacts"] = json.loads(yield_dict["artifacts"])
                if yield_dict.get("metadata"):
                    yield_dict["metadata"] = json.loads(yield_dict["metadata"])
                results.append(yield_dict)

            return results

    def get_previous_yield(
        self, chain_id: str, before_task_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get the most recent yield in a chain before a specific task.

        Used for injecting previous yield context into downstream tasks.

        Args:
            chain_id: The chain to query
            before_task_id: Get yields before this task

        Returns:
            The previous yield dict, or None if this is the first task
        """
        with self._get_connection() as conn:
            # Get all yields in chain ordered by timestamp
            cursor = conn.execute(
                "SELECT * FROM sacks WHERE chain_id = ? ORDER BY timestamp", (chain_id,)
            )
            rows = cursor.fetchall()

            # Find the yield just before the specified task
            previous = None
            for row in rows:
                if row["task_id"] == before_task_id:
                    break
                previous = row

            if not previous:
                return None

            yield_dict = dict(previous)
            if yield_dict.get("artifacts"):
                yield_dict["artifacts"] = json.loads(yield_dict["artifacts"])
            if yield_dict.get("metadata"):
                yield_dict["metadata"] = json.loads(yield_dict["metadata"])
            return yield_dict

    # Thread Operations

    def save_thread(self, thread: Thread) -> bool:
        """Save a thread to the database."""
        from_id, to_id = self._genesis_key_control(thread)
        created_at = (
            thread.created_at.isoformat()
            if isinstance(thread.created_at, datetime)
            else str(thread.created_at)
        )
        # A2 (Phase 3a): stamp the content address at the write, so no live insert
        # path leaks a NULL-hash row (§5.A2). Hashed over the POST-genesis-keyed
        # endpoints and the same normalized created_at that goes to the column, so
        # the row's thread_hash matches compute_thread_hash over its stored bytes.
        thread_hash = compute_thread_hash(
            from_id, to_id, thread.type, thread.weaver,
            created_at, thread.content,
        )
        with self._get_connection() as conn:
            # OR IGNORE on the thread_hash PK (§4.3): a byte-identical re-save keeps
            # the ORIGINAL row — and its thread_id audit handle — rather than churning
            # it. Pre-swap (thread_id-PK) dbs are unaffected: thread_ids are random, so
            # a collision only happens on a genuine byte-dup, where OR IGNORE == OR
            # REPLACE. Post-swap (thread_hash-PK) it is the required end-state semantics.
            conn.execute(
                """
                INSERT OR IGNORE INTO threads
                (thread_id, from_id, to_id, type, content, weaver, created_at,
                 thread_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    thread.thread_id,
                    from_id,
                    to_id,
                    thread.type,
                    thread.content,
                    thread.weaver,
                    created_at,
                    thread_hash,
                ),
            )
            conn.commit()
        return True

    def _insert_control_thread(self, conn, ttype: str, from_id: str, to_id: str,
                               content: str, weaver: str,
                               created_at: str) -> None:
        """Insert one genesis-keyed control thread on an EXISTING connection (the
        caller's transaction) — the JSON-import path, which must mint the thread
        alongside the versions/refs rows it imports (control state has no other
        persistence post-contraction). Same hash-stamping and OR IGNORE semantics
        as save_thread; endpoints are the caller's responsibility (the import
        anchors on the genesis content hash directly)."""
        thread_hash = compute_thread_hash(
            from_id, to_id, ttype, weaver, created_at, content)
        conn.execute(
            "INSERT OR IGNORE INTO threads "
            "(thread_id, from_id, to_id, type, content, weaver, created_at, "
            " thread_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (generate_thread_id(), from_id, to_id, ttype, content, weaver,
             created_at, thread_hash),
        )

    def _genesis_key_control(self, thread: Thread) -> tuple:
        """Return the (from_id, to_id) a control thread should persist with:
        status/archive/assignment edges anchor on the folio's genesis hash, never
        its slug (Phase 3a A2, design §5.A2). This is the UNIVERSAL enforcement
        point — the sugar routes hand us genesis already, but the generic
        POST /threads path (``skein close``) and any direct caller hand us a slug,
        so the invariant is enforced here rather than at each writer.

        Idempotent: a genesis endpoint (or a slug with no ref) resolves to None and
        is left unchanged, so a row already keyed on genesis passes through. The
        anchor column matches the control reader's — ``to_id`` for the
        status/archive self-loop, ``from_id`` for assignment (from=folio,
        to=assignee, assignee left untouched). Non-control edges are never touched.
        """
        if thread.type in ("status", "archive"):
            genesis = self.genesis_of_slug(thread.to_id)
            if genesis:
                return genesis, genesis
        elif thread.type == "assignment":
            genesis = self.genesis_of_slug(thread.from_id)
            if genesis:
                return genesis, thread.to_id
        return thread.from_id, thread.to_id

    def get_threads(
        self,
        from_id: Optional[str] = None,
        to_id: Optional[str] = None,
        type: Optional[str] = None,
        weaver: Optional[str] = None,
    ) -> List[Thread]:
        """Get threads with optional filters using indexed queries. BYTE-FAITHFUL:
        returns endpoints exactly as stored (control threads are genesis-keyed
        post-A3). For the client read boundary that must present control on the folio
        slug, use :meth:`get_threads_display`."""
        with self._get_connection() as conn:
            query = "SELECT * FROM threads WHERE 1=1"
            params = []

            if from_id:
                query += " AND from_id = ?"
                params.append(from_id)
            if to_id:
                query += " AND to_id = ?"
                params.append(to_id)
            if type:
                query += " AND type = ?"
                params.append(type)
            if weaver:
                query += " AND weaver = ?"
                params.append(weaver)

            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

            return [
                Thread(
                    thread_id=row["thread_id"],
                    from_id=row["from_id"],
                    to_id=row["to_id"],
                    type=row["type"],
                    content=row["content"],
                    weaver=row["weaver"],
                    created_at=ensure_aware(row["created_at"]),
                )
                for row in rows
            ]

    def get_thread_by_hash(self, thread_hash: str) -> Optional[Dict[str, Any]]:
        """One thread row by its content hash (the post-swap PK), as a dict with the
        six canonical fields + ``thread_hash``. The publish path (docs/PHASE_4_DESIGN.md
        §4) needs the STORED hash, which the ``Thread`` model does not carry. Read-only,
        additive."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT thread_hash, from_id, to_id, type, weaver, created_at, content "
                "FROM threads WHERE thread_hash = ?", (thread_hash,)).fetchone()
            if not row:
                return None
            return {
                "thread_hash": row["thread_hash"], "from_id": row["from_id"],
                "to_id": row["to_id"], "type": row["type"], "weaver": row["weaver"],
                "created_at": ensure_aware(row["created_at"]), "content": row["content"],
            }

    def get_threads_display(
        self,
        from_id: Optional[str] = None,
        to_id: Optional[str] = None,
        type: Optional[str] = None,
    ) -> List[Thread]:
        """Presentation reader behind ``GET /threads`` and the ``/search`` threads
        branch (Phase 3a). The A3 cutover re-anchored control threads
        (status/assignment/archive) from the folio SLUG to the lineage GENESIS hash,
        so the byte-faithful ``get_threads`` no longer surfaces a folio's control
        threads when queried by slug, and a fetch-all buckets them under the genesis
        hash. This reader restores the pre-A3 slug-keyed VIEW every display/count
        surface expects, mirroring the A1 value readers (which resolve genesis->slug
        for status/assignment VALUES). Two parts:

          (1) UNION — when ``from_id``/``to_id`` is a folio slug, also match that
              folio's genesis-keyed threads (anchor == ``refs.genesis_hash`` AND
              ``type`` in :data:`GENESIS_ANCHORED_DISPLAY_TYPES`).
          (2) REWRITE — every returned genesis-anchored thread has each endpoint equal
              to a known ``genesis_hash`` mapped back to its slug, reconstructing the
              exact pre-anchor shape (status/archive self-loop from=to=slug; assignment
              from=slug, to=assignee unchanged; a Class B reference/mention/reply/
              succession/within edge back to from=slug/to=slug).

        Phase 3b (§6.1): the same UNION+REWRITE now covers the genesis-anchored Class B
        structural edges (:data:`CLASS_B_GENESIS_DISPLAY_TYPES`), because the PK swap
        re-anchored them slug->genesis in the stored rows exactly as A3 did for control
        — without this a slug query would stop returning a folio's reference/mention/
        reply/succession edges and a fetch-all would surface them hash-keyed. Both steps
        remain restricted to :data:`GENESIS_ANCHORED_DISPLAY_TYPES`: the VERSION-anchored
        edges (supersedes/reverted/published) are left byte-faithful because
        ``genesis_hash`` equals the genesis version's content hash, so resolving an edit
        edge that touches the genesis version would corrupt it. ``get_threads`` stays
        byte-faithful for internal/integrity callers; only this path resolves. The db
        remains genesis-keyed (source of truth).

        Consequence: control lives at the slug in the presentation view, so a query
        by a raw ``genesis_hash`` no longer surfaces its control threads — they rewrite
        to the slug and the re-applied slug filter then drops them (matching pre-A3,
        when control was slug-keyed and a genesis-hash query returned none either). No
        live caller queries control by genesis hash. Net: every returned thread's
        visible endpoint equals the queried slug.
        """
        with self._get_connection() as conn:
            # genesis <-> slug maps from refs, slug-ordered so a (never-expected,
            # I1=0) duplicate genesis_hash resolves the same way the A1 anchor_map
            # does (first slug alphabetically wins the shared hash).
            genesis_by_slug: Dict[str, str] = {}
            slug_by_genesis: Dict[str, str] = {}
            for ref in conn.execute(
                "SELECT slug, genesis_hash FROM refs ORDER BY slug"
            ):
                genesis_by_slug[ref["slug"]] = ref["genesis_hash"]
                slug_by_genesis.setdefault(ref["genesis_hash"], ref["slug"])

            resolve_ph = ",".join("?" for _ in GENESIS_ANCHORED_DISPLAY_TYPES)
            clauses: List[str] = ["1=1"]
            params: List[Any] = []

            def _anchor_clause(col: str, value: str) -> None:
                genesis = genesis_by_slug.get(value)
                if genesis is not None:
                    # slug endpoint (any type) OR the folio's genesis endpoint,
                    # genesis-anchored types only (control + Class B, §6.1).
                    clauses.append(
                        f"({col} = ? OR ({col} = ? AND type IN ({resolve_ph})))"
                    )
                    params.append(value)
                    params.append(genesis)
                    params.extend(GENESIS_ANCHORED_DISPLAY_TYPES)
                else:
                    clauses.append(f"{col} = ?")
                    params.append(value)

            if from_id:
                _anchor_clause("from_id", from_id)
            if to_id:
                _anchor_clause("to_id", to_id)
            if type:
                clauses.append("type = ?")
                params.append(type)

            query = "SELECT * FROM threads WHERE " + " AND ".join(clauses)
            rows = conn.execute(query, params).fetchall()

        resolve = set(GENESIS_ANCHORED_DISPLAY_TYPES)
        threads: List[Thread] = []
        for row in rows:
            f_id = row["from_id"]
            t_id = row["to_id"]
            if row["type"] in resolve:
                f_id = slug_by_genesis.get(f_id, f_id)
                t_id = slug_by_genesis.get(t_id, t_id)
            # Re-apply the requested slug filter in PRESENTATION space (after the
            # genesis->slug rewrite), so a returned thread's visible endpoint always
            # equals the queried slug. This closes the I1-collision edge: two slugs
            # sharing a genesis_hash resolve (like the A1 anchor_map) to a single
            # OWNER slug, so a query for the NON-owner slug must not surface — and
            # must never mislabel — the owner's control thread. (I1=0 is an A3
            # precondition; this is exactness + defense-in-depth, and also makes a
            # both-endpoints query self-consistent.)
            if from_id and f_id != from_id:
                continue
            if to_id and t_id != to_id:
                continue
            threads.append(
                Thread(
                    thread_id=row["thread_id"],
                    from_id=f_id,
                    to_id=t_id,
                    type=row["type"],
                    content=row["content"],
                    weaver=row["weaver"],
                    created_at=ensure_aware(row["created_at"]),
                )
            )
        return threads

    def _latest_control_by_folio(
        self,
        conn,
        *,
        ttype: str,
        anchor_col: str,
        value_col: str,
        folio_ids: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        """Reduce control threads of ``ttype`` to one value per folio (Phase 3a A4
        GENESIS-ONLY reader). A folio is identified by ``anchor_col`` — ``to_id`` for
        the status/archive self-loops, ``from_id`` for assignment (from = folio) —
        matching the folio's GENESIS hash (``refs.genesis_hash``), resolved back to
        the slug. Post-cutover every live control thread is genesis-keyed and the
        gate (``all_dbs_migrated``) guarantees no live slug-keyed FOLIO control
        remains, so the A1→A3 slug-keyspace tolerance is RETIRED here: a slug-keyed
        leftover (or any orphan whose anchor is not a live genesis) is not surfaced —
        it reads default (status 'open', assignment None), the exact misread the gate
        precludes ecosystem-wide (design §5.A4, I2). The latest row wins by the
        deterministic ``(created_at, thread_id)`` order — the SAME order the A3
        rebuild uses. ``anchor_col``/``value_col`` are fixed internal literals, never
        caller input.
        """
        # anchor_map resolves each folio's GENESIS hash to its slug (genesis-only).
        # genesis_hash is unique per lineage (I1, an A3 precondition), so the
        # ROW_NUMBER window is a defensive dedup — under a (never-expected) I1
        # collision it resolves a shared genesis to one slug deterministically
        # (min slug), matching get_threads_display and the retired tolerant map's
        # tie-break, rather than double-counting a thread onto two folios.
        filt = ""
        params: List[str] = [ttype]
        if folio_ids is not None:
            filt = "AND m.slug IN (%s)" % ",".join("?" for _ in folio_ids)
            params.extend(folio_ids)
        query = f"""
            WITH anchor_map AS (
                SELECT anchor, slug FROM (
                    SELECT genesis_hash AS anchor, slug,
                        ROW_NUMBER() OVER (
                            PARTITION BY genesis_hash ORDER BY slug
                        ) AS arn
                    FROM refs
                ) WHERE arn = 1
            )
            SELECT folio_id, val FROM (
                SELECT m.slug AS folio_id, t.{value_col} AS val,
                    ROW_NUMBER() OVER (
                        PARTITION BY m.slug
                        ORDER BY t.created_at DESC, t.thread_id DESC
                    ) AS rn
                FROM threads t
                JOIN anchor_map m ON m.anchor = t.{anchor_col}
                WHERE t.type = ? {filt}
            ) WHERE rn = 1
        """
        return {row["folio_id"]: row["val"] for row in conn.execute(query, params)}

    def genesis_of_slug(self, slug: str) -> Optional[str]:
        """Resolve a folio slug to its lineage genesis hash (``refs.genesis_hash``,
        the immutable lineage id). Phase 3a A2 control writers anchor genesis-keyed
        control edges on this. Returns None if the slug has no ref (no lineage) —
        callers fall back to the slug so a control write never crashes on a folio
        that is somehow refless.
        """
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT genesis_hash FROM refs WHERE slug = ?", (slug,)
            ).fetchone()
        return row["genesis_hash"] if row else None

    def get_latest_statuses(
        self, folio_ids: Optional[List[str]] = None
    ) -> Dict[str, str]:
        """Most recent status per folio (slug -> content). A4 genesis-only: keys the
        self-loop on the folio's genesis hash (``to_id``), resolves it to the slug,
        and breaks equal-``created_at`` ties on ``thread_id``. (The A1→A3 slug-
        keyspace union was retired at A4; gate all_dbs_migrated.)
        """
        with self._get_connection() as conn:
            return self._latest_control_by_folio(
                conn, ttype="status", anchor_col="to_id", value_col="content",
                folio_ids=folio_ids)

    def get_latest_assignments(
        self, folio_ids: Optional[List[str]] = None
    ) -> Dict[str, str]:
        """Most recent assignment per folio (slug -> assignee). A4 genesis-only: the
        folio is keyed on ``from_id`` = its genesis hash (assignment is from=folio,
        to=assignee), resolved to the slug; the returned value is the assignee
        (``to_id``). (The A1→A3 slug-keyspace union was retired at A4.)
        """
        with self._get_connection() as conn:
            return self._latest_control_by_folio(
                conn, ttype="assignment", anchor_col="from_id", value_col="to_id",
                folio_ids=folio_ids)

    # (get_latest_archives was removed with the folio-archived feature,
    # 2026-07-08: zero archive threads and zero archived refs existed
    # ecosystem-wide — a never-used holdover. verify_threads_control.py, the
    # SPENT A3 oracle, detects the reader's absence and degrades gracefully.)

    # Folio Operations

    def save_folio(self, folio: Folio, editor: Optional[str] = None) -> bool:
        """Save or update a folio into versions/refs (Phase 3a A5: folios retired).

        Phase 2 edit-as-commit, all in one BEGIN IMMEDIATE transaction:
          - CREATE (no ref yet): mint the genesis version, insert the ref.
          - EDIT, identity field (title/content) changed: mint a new immutable
            version, write a ``supersedes`` edge (new->old head), move the ref head.
          - EDIT, content reverted to an existing version: move the head back, write
            a durable ``reverted`` marker, mint nothing (no cycle).
          - EDIT, no identity change (status/assignment/move/no-op): refresh
            the refs local workflow fields only; mint nothing.
        The legacy ``folios`` dual-write is gone as of Phase 3a A5 (reads have been
        off ``folios`` since the commit-C read-flip); versions/refs is the sole
        write target. ``created_at``/``created_by`` inherit the lineage genesis, so
        a status-only edit cannot change the hash through a timestamp; the per-edit
        editor lives on the supersedes/reverted edge's weaver (``editor``), not on
        the version — the Folio's ``created_by`` is the genesis author by design.

        State (status/assignment) is written by the routes as genesis-keyed
        threads — the sole persistence of control state (threads-only; the refs
        cache columns are gone). save_folio does NOT mint or move any state
        thread; it persists only the local workflow fields (site/target_agent/
        omlet/acknowledged_at/metadata) onto refs.
        """
        with self._immediate_txn() as conn:
            # Resolve the ref to tell CREATE from EDIT. On EDIT, reassert the
            # genesis created_at/created_by onto the folio BEFORE the Phase 0
            # recompute, so the minted version's PK always verifies against its
            # own columns even if the caller handed a fresh created_at/created_by
            # (a pure status edit must not change the hash through a timestamp).
            ref = conn.execute(
                "SELECT genesis_hash, head_hash FROM refs WHERE slug = ?",
                (folio.folio_id,),
            ).fetchone()
            if ref is not None:
                gen = conn.execute(
                    "SELECT created_at, created_by FROM versions WHERE content_hash = ?",
                    (ref["genesis_hash"],),
                ).fetchone()
                if gen is not None:
                    folio.created_at = ensure_aware(gen["created_at"])
                    folio.created_by = gen["created_by"]

            # Phase 0 chokepoint: recompute the hash on EVERY write (AFTER the
            # genesis reassert) so an edited folio's hash never goes stale and a
            # caller-supplied content_hash is never trusted.
            if KNURL_AVAILABLE:
                folio.content_hash = compute_folio_hash(folio)

            created_at = (
                folio.created_at.isoformat()
                if isinstance(folio.created_at, datetime)
                else str(folio.created_at)
            )
            acknowledged_at = None
            if folio.acknowledged_at:
                acknowledged_at = (
                    folio.acknowledged_at.isoformat()
                    if isinstance(folio.acknowledged_at, datetime)
                    else str(folio.acknowledged_at)
                )

            # Maintain versions/refs + the supersedes/reverted edges (only when a
            # hash is available). Degraded no-knurl mode now persists NOTHING —
            # A5 removed the folios dual-write and versions/refs needs the hash —
            # but no-knurl is not a production path (a no-hash folios row had no
            # head in versions/refs and was already unreadable post-commit-C).
            if KNURL_AVAILABLE and folio.content_hash:
                self._maintain_versions_refs(
                    conn, folio, ref, folio.content_hash, created_at,
                    acknowledged_at, editor,
                )
            # versions_fts is updated automatically via its AFTER INSERT trigger.
        return True

    def _refs_local(self, folio: Folio, acknowledged_at: Optional[str]) -> tuple:
        """The local workflow fields persisted on refs, in the column order used
        by the INSERT/UPDATE below: site_id, target_agent, omlet, acknowledged_at,
        metadata. (created_by and type are hashed identity and live on versions,
        not refs; status/assigned_to are thread-derived and persisted NOWHERE on
        refs since the threads-only contraction.) These encodings are what a read
        coerces back, so a read round-trips a write.
        """
        return (
            folio.site_id,
            folio.target_agent,
            folio.omlet,
            acknowledged_at,
            json.dumps(folio.metadata) if folio.metadata else "{}",
        )

    def _insert_version(self, conn, folio: Folio, content_hash: str,
                        created_at: str) -> None:
        """Append the immutable version row (INSERT OR IGNORE — content-addressing
        dedups a lineage that reaches byte-identical content). Identity only."""
        conn.execute(
            "INSERT OR IGNORE INTO versions "
            "(content_hash, type, title, content, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (content_hash, folio.type, folio.title, folio.content,
             created_at, folio.created_by),
        )

    def _maintain_versions_refs(self, conn, folio: Folio, ref, new_hash: str,
                                created_at: str, acknowledged_at: Optional[str],
                                editor: Optional[str]) -> None:
        """Mint the version / move the head / write the edit edge, per §3."""
        control = self._refs_local(folio, acknowledged_at)
        now = datetime.now(timezone.utc).isoformat()
        # The per-edit editor; falls back to the genesis author for direct storage
        # callers and tests that pass no editor (§3.2).
        weaver = editor or folio.created_by

        if ref is None:
            # CREATE — a new lineage. Mint genesis, insert the ref. No edge.
            self._insert_version(conn, folio, new_hash, created_at)
            conn.execute(
                "INSERT INTO refs "
                "(slug, genesis_hash, head_hash, site_id, "
                " target_agent, omlet, acknowledged_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (folio.folio_id, new_hash, new_hash, *control),
            )
            return

        head_hash = ref["head_hash"]
        if new_hash == head_hash:
            # No identity change — refresh the local workflow fields, mint nothing.
            self._update_refs(conn, folio.folio_id, new_hash, control)
            return

        # Discriminate REVERT from a forward edit by LINEAGE membership, not global
        # versions membership. Content-addressing dedups: another lineage may
        # already hold byte-identical content, so a forward edit can land on an
        # existing global version WITHOUT being a revert. The revert case is
        # exactly "new_hash is reachable backward from this head along supersedes
        # edges" — the lineage behind the current head. Crucially, this is also the
        # exact condition under which adding a new->head supersedes edge would
        # cycle, so the forward branch is provably acyclic.
        if new_hash in self._reachable_from(conn, head_hash):
            # REVERT / DAG re-entry — new_hash is reachable backward from head along
            # supersedes edges. This is also EXACTLY the condition under which a
            # new->head edge would close a cycle, so we must not mint one. Move the
            # head and write a durable reverted marker. A revert/redo toggle
            # accumulates one marker per revert (the intended audit trail): distinct
            # reverts carry distinct created_at → distinct thread_hash → distinct rows.
            # OR IGNORE on the thread_hash PK (§4.3) only guards the physically-
            # impossible same-normalized-instant double-revert, keeping the swap from
            # raising a PK violation on it rather than changing the audit semantics.
            self._update_refs(conn, folio.folio_id, new_hash, control)
            conn.execute(
                "INSERT OR IGNORE INTO threads "
                "(thread_id, from_id, to_id, type, content, weaver, created_at, "
                " thread_hash) "
                "VALUES (?, ?, ?, 'reverted', NULL, ?, ?, ?)",
                (generate_thread_id(), head_hash, new_hash, weaver, now,
                 compute_thread_hash(head_hash, new_hash, "reverted", weaver,
                                     now, None)),
            )
            return

        # FORWARD edit — an identity field changed to content not behind this head.
        # Append the version (INSERT OR IGNORE dedups a cross-lineage match), write
        # the supersedes edge (new->old head), move the head. Skip the insert if an
        # identical edge already exists (a redo-after-revert), keeping the DAG clean.
        self._insert_version(conn, folio, new_hash, created_at)
        dup = conn.execute(
            "SELECT 1 FROM threads WHERE type='supersedes' AND from_id=? AND to_id=?",
            (new_hash, head_hash),
        ).fetchone()
        if not dup:
            # The endpoint dup-check above already guards a repeat edge; OR IGNORE on
            # the thread_hash PK (§4.3) is the belt-and-suspenders that keeps the swap
            # from raising a PK violation on a residual collision.
            conn.execute(
                "INSERT OR IGNORE INTO threads "
                "(thread_id, from_id, to_id, type, content, weaver, created_at, "
                " thread_hash) "
                "VALUES (?, ?, ?, 'supersedes', NULL, ?, ?, ?)",
                (generate_thread_id(), new_hash, head_hash, weaver, now,
                 compute_thread_hash(new_hash, head_hash, "supersedes", weaver,
                                     now, None)),
            )
        self._update_refs(conn, folio.folio_id, new_hash, control)

    def _reachable_from(self, conn, start_hash: str) -> set:
        """All version hashes reachable from start_hash by following supersedes
        edges (from_id -> to_id), including start_hash itself — the content behind
        the current head. An edit whose new_hash lands in this set is a revert (and
        is exactly the case where a new->head edge would cycle). One recursive CTE
        rather than a query per node, since this runs in the write-locked save path;
        UNION terminates on shared/deduped nodes."""
        rows = conn.execute(
            "WITH RECURSIVE anc(h) AS ("
            "  SELECT ? "
            "  UNION "
            "  SELECT t.to_id FROM threads t JOIN anc ON t.from_id = anc.h "
            "  WHERE t.type = 'supersedes'"
            ") SELECT h FROM anc",
            (start_hash,),
        ).fetchall()
        return {r[0] for r in rows}

    def _update_refs(self, conn, slug: str, head_hash: str, control: tuple) -> None:
        """Move the ref head and refresh the local workflow fields (genesis_hash
        is immutable and never touched; status/assigned_to are thread-derived and
        not persisted here)."""
        conn.execute(
            "UPDATE refs SET head_hash = ?, site_id = ?, target_agent = ?, "
            "omlet = ?, acknowledged_at = ?, metadata = ? WHERE slug = ?",
            (head_hash, *control, slug),
        )

    def get_folio(self, folio_id: str) -> Optional[Folio]:
        """Get a specific folio by ID — its lineage HEAD via the join."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"SELECT {_HEAD_SELECT} {_HEAD_FROM} WHERE r.slug = ?", (folio_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            return _folio_from_head_row(row)

    def get_folios(
        self,
        site_id: Optional[str] = None,
        type: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> List[Folio]:
        """Get folios (heads only) with optional filters. Identity filters hit
        versions (type/created_by), site hits refs. Status/assignment filtering
        is NOT SQL-level: control state is thread-derived, so callers enrich
        (enrich_folios_with_status) and filter on the result — which is what the
        API routes always did; the old SQL params had no production caller."""
        with self._get_connection() as conn:
            query = f"SELECT {_HEAD_SELECT} {_HEAD_FROM} WHERE 1=1"
            params = []

            if site_id:
                query += " AND r.site_id = ?"
                params.append(site_id)
            if type:
                query += " AND v.type = ?"
                params.append(type)
            if created_by:
                query += " AND v.created_by = ?"
                params.append(created_by)

            # Deterministic secondary key (slug) so versions.created_at ties don't
            # reorder vs the old folios rowid order (§4 determinism).
            query += " ORDER BY v.created_at DESC, r.slug"

            cursor = conn.execute(query, params)
            return [_folio_from_head_row(row) for row in cursor.fetchall()]

    def move_folio(self, folio_id: str, dest_site_id: str) -> Optional[Folio]:
        """Move a folio to a different site.

        site_id is ref-local control, not an identity field, so a move mints NO
        version — it only UPDATEs refs.site_id and returns the head reconstructed
        from the join. (Phase 3a A5 retired the companion ``folios`` write this
        path used to carry alongside the refs write.)
        """
        with self._immediate_txn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM refs WHERE slug = ?", (folio_id,)
            ).fetchone()
            if not exists:
                return None

            conn.execute(
                "UPDATE refs SET site_id = ? WHERE slug = ?",
                (dest_site_id, folio_id),
            )

            row = conn.execute(
                f"SELECT {_HEAD_SELECT} {_HEAD_FROM} WHERE r.slug = ?", (folio_id,)
            ).fetchone()
            return _folio_from_head_row(row)

    def search_folios(self, query: str, limit: int = 50) -> List[Folio]:
        """Full-text search across folio HEADS using FTS5 over versions.

        versions_fts indexes every version (including superseded ones), so the head
        join (refs.head_hash = versions.content_hash) MUST precede ORDER BY rank
        LIMIT — otherwise superseded matches fill the top-N and hide head matches.
        """
        with self._get_connection() as conn:
            # FTS5 query - escape special characters for safety
            fts_query = query.replace('"', '""')
            cursor = conn.execute(
                f"""
                SELECT {_HEAD_SELECT}
                FROM versions_fts
                JOIN versions v ON v.rowid = versions_fts.rowid
                JOIN refs r ON r.head_hash = v.content_hash
                WHERE versions_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """,
                (f'"{fts_query}"', limit),
            )
            return [_folio_from_head_row(row) for row in cursor.fetchall()]

    def get_folio_count(self, site_id: Optional[str] = None) -> int:
        """Count of lineages (heads) = COUNT(*) of refs, optionally by site."""
        with self._get_connection() as conn:
            if site_id:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM refs WHERE site_id = ?", (site_id,)
                )
            else:
                cursor = conn.execute("SELECT COUNT(*) FROM refs")
            return cursor.fetchone()[0]

    def get_site_last_activity(self) -> Dict[str, datetime]:
        """Return mapping of site_id -> latest head created_at (timezone-aware).

        created_at is the genesis timestamp (inherited); site_id is on refs, so
        this groups the heads join by refs.site_id. Sites with zero folios are
        not included.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT r.site_id AS site_id, MAX(v.created_at) AS last_created_at "
                + _HEAD_FROM + " GROUP BY r.site_id"
            )
            return {
                row["site_id"]: ensure_aware(row["last_created_at"])
                for row in cursor.fetchall()
            }

    def get_version_by_hash(self, content_hash: str) -> Optional["VersionView"]:
        """By-hash fetch (§8): the version's five immutable identity fields + the
        is_head / lineage_head flags, NO mutable control (a hash addresses content,
        not a lineage's state). Resolves a SUPERSEDED version too (durable
        citation). Returns None if the hash is unknown."""
        with self._get_connection() as conn:
            v = conn.execute(
                "SELECT * FROM versions WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            if not v:
                return None
            is_head = conn.execute(
                "SELECT 1 FROM refs WHERE head_hash = ? LIMIT 1", (content_hash,)
            ).fetchone() is not None
            lineage_head = self._lineage_head(conn, content_hash, is_head)
            return VersionView(
                content_hash=content_hash,
                type=v["type"],
                title=v["title"],
                content=v["content"],
                created_at=ensure_aware(v["created_at"]),
                created_by=v["created_by"],
                is_head=is_head,
                lineage_head=lineage_head,
            )

    def _lineage_head(self, conn, content_hash: str, is_head: bool) -> Optional[str]:
        """The current head hash of the lineage this version belongs to. A head
        version's lineage head is itself. For a superseded version, walk supersedes
        edges BACKWARD to the genesis root(s) and look up the ref(s) anchored
        there — this is correct even after a revert (the head can be 'behind' the
        supersedes tip). A version shared by two lineages (content-addressing dedup)
        has more than one candidate; pick deterministically (documented edge)."""
        if is_head:
            return content_hash
        rows = conn.execute(
            "WITH RECURSIVE back(h) AS ("
            "  SELECT ? "
            "  UNION "
            "  SELECT t.to_id FROM threads t JOIN back ON t.from_id = back.h "
            "  WHERE t.type = 'supersedes'"
            ") SELECT DISTINCT r.head_hash FROM refs r JOIN back ON r.genesis_hash = back.h",
            (content_hash,),
        ).fetchall()
        heads = sorted(r[0] for r in rows)
        return heads[0] if heads else None

    def migrate_folios_from_json(self, sites_dir: Path) -> int:
        """
        Migrate folio JSON files from all sites into SQLite (versions/refs).
        Returns count of migrated folios. Idempotent.

        Phase 3a A5 re-based this cold import off the retired ``folios`` table: the
        idempotency probe and the writes are on versions/refs (the live read
        layer), so it survives a dropped folios table and never names it.
        """
        if not sites_dir.exists():
            return 0

        # Check if we already have data (idempotent). Probed on refs (the lineage
        # head layer) since A5 retired folios; a store whose data already lives in
        # versions/refs is not re-imported.
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM refs")
            existing_count = cursor.fetchone()[0]
            if existing_count > 0:
                logger.info(
                    f"refs already has {existing_count} lineage(s), skipping JSON import"
                )
                return 0

        # Collect all folio JSON files. Sorted, not raw iterdir()/glob() order
        # -- both are filesystem-dependent with no ordering guarantee, and a
        # duplicate slug across sites relies on "first wins" below being
        # deterministic (it previously wasn't: which file counted as "first"
        # could differ by OS/filesystem).
        folio_files = []
        for site_dir in sorted(sites_dir.iterdir()):
            if site_dir.is_dir():
                folios_dir = site_dir / "folios"
                if folios_dir.exists():
                    folio_files.extend(sorted(folios_dir.glob("*.json")))

        if not folio_files:
            return 0

        logger.info(f"Migrating {len(folio_files)} folios from JSON to SQLite")

        count = 0
        errors = 0
        with self._get_connection() as conn:
            for folio_file in folio_files:
                try:
                    with open(folio_file) as f:
                        data = json.load(f)

                    # Handle missing fields gracefully
                    folio_id = data.get("folio_id", folio_file.stem)
                    metadata = data.get("metadata", {})
                    ftype = data.get("type", "issue")
                    site_id = data.get("site_id", folio_file.parent.parent.name)
                    created_at = data.get(
                        "created_at", datetime.now(timezone.utc).isoformat()
                    )
                    created_by = data.get("created_by", "unknown")
                    title = data.get("title", "")
                    content = data.get("content", "")

                    # Recompute the content hash from the five canonical fields
                    # rather than trusting the JSON's stored value: this cold
                    # migration is a folio writer too, so it must honour the same
                    # single-source-of-truth hash as the write chokepoint. A legacy
                    # folio:sha256: or missing value in the JSON is never persisted.
                    # In degraded mode (no knurl) fall back to the stored value,
                    # matching save_folio's behaviour.
                    if KNURL_AVAILABLE:
                        content_hash = identity.compute_folio_hash(
                            {
                                "type": ftype,
                                "title": title,
                                "content": content,
                                "created_at": created_at,
                                "created_by": created_by,
                            }
                        )
                    else:
                        content_hash = data.get("content_hash")

                    # This cold JSON import writes versions/refs (the live read
                    # layer) directly — A5 retired the folios dual-write. Genesis =
                    # head = the recomputed content_hash. (No supersedes edge —
                    # each imported row is a lineage genesis.) A row with no
                    # usable hash is skipped, not imported.
                    #
                    # Control state is thread-derived post-contraction, so a
                    # legacy JSON status/assignee is imported by MINTING the
                    # genesis-keyed control thread — dropping it would silently
                    # read every closed legacy folio as open (deep_code_audit
                    # finding 6). The thread's created_at is the folio's own
                    # created_at, so any real later status set wins the
                    # (created_at DESC, thread_id DESC) reduction. A legacy
                    # archived flag has no post-contraction meaning (the feature
                    # was removed; zero uses existed) — logged loudly, not
                    # imported.
                    if content_hash:
                        conn.execute(
                            "INSERT OR IGNORE INTO versions "
                            "(content_hash, type, title, content, created_at, created_by) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (content_hash, ftype, title, content,
                             created_at, created_by),
                        )
                        ref_cur = conn.execute(
                            "INSERT OR IGNORE INTO refs "
                            "(slug, genesis_hash, head_hash, site_id, "
                            " target_agent, omlet, acknowledged_at, metadata) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (folio_id, content_hash, content_hash, site_id,
                             data.get("target_agent"), data.get("omlet"),
                             data.get("acknowledged_at"),
                             json.dumps(metadata) if metadata else "{}"),
                        )
                        if ref_cur.rowcount == 0:
                            # Slug collision across site dirs: OR IGNORE kept
                            # the FIRST file's row, so this file's folio was NOT
                            # imported — do not mint its control threads either.
                            # Unconditional minting here wrote an ORPHANED
                            # status/assignment thread keyed on a genesis no ref
                            # points at (deep_code_audit r2 finding 4): its
                            # state was silently lost AND permanently unreadable.
                            logger.warning(
                                f"{folio_id}: duplicate slug in JSON import — "
                                f"row skipped (first file wins), control state "
                                f"of the duplicate NOT imported")
                        else:
                            # None-checks, not truthiness (the '' invariant):
                            # a present-but-falsy legacy value is imported
                            # faithfully; only ABSENCE (or the default 'open')
                            # mints nothing.
                            legacy_status = data.get("status")
                            if legacy_status is not None and \
                                    legacy_status != "open":
                                self._insert_control_thread(
                                    conn, "status", content_hash, content_hash,
                                    legacy_status, created_by, created_at)
                            legacy_assignee = data.get("assigned_to")
                            if legacy_assignee is not None:
                                self._insert_control_thread(
                                    conn, "assignment", content_hash,
                                    legacy_assignee,
                                    f"Assigned to {legacy_assignee}",
                                    created_by, created_at)
                            if data.get("archived"):
                                logger.warning(
                                    f"{folio_id}: legacy archived=true NOT "
                                    f"imported (folio-archived feature removed "
                                    f"2026-07-08)")
                            count += 1
                except Exception as e:
                    logger.error(f"Failed to migrate {folio_file.name}: {e}")
                    errors += 1

            # FTS index populated automatically via triggers on INSERT

            conn.commit()

        logger.info(f"Migrated {count} folios from JSON to SQLite ({errors} errors)")

        # Backup sites/ folio dirs by renaming
        for site_dir in sites_dir.iterdir():
            if site_dir.is_dir():
                folios_dir = site_dir / "folios"
                migrated_dir = site_dir / "folios_migrated"
                if folios_dir.exists() and not migrated_dir.exists():
                    folios_dir.rename(migrated_dir)
                    logger.info(f"Backed up {folios_dir} -> {migrated_dir}")

        return count

    def migrate_threads_from_json(self, threads_dir: Path) -> int:
        """Migrate thread JSON files into SQLite. Returns count of migrated threads."""
        if not threads_dir.exists():
            return 0

        json_files = list(threads_dir.glob("*.json"))
        if not json_files:
            return 0

        # Check if we already have data (idempotent)
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM threads")
            existing_count = cursor.fetchone()[0]
            if existing_count > 0:
                logger.info(
                    f"Threads table already has {existing_count} rows, skipping migration"
                )
                return 0

        count = 0
        with self._get_connection() as conn:
            for thread_file in json_files:
                try:
                    with open(thread_file) as f:
                        data = json.load(f)

                    # A2 (Phase 3a): stamp thread_hash here too. This legacy
                    # JSON->SQLite importer is effectively frozen (it no-ops once
                    # the threads table is non-empty, which every live db is), but
                    # a fresh db seeded from JSON must not leak NULL-hash rows —
                    # §4/§5.A2 want EVERY insert path content-addressed.
                    thread_hash = compute_thread_hash(
                        data["from_id"], data["to_id"], data["type"],
                        data.get("weaver"), data["created_at"], data.get("content"),
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO threads
                        (thread_id, from_id, to_id, type, content, weaver,
                         created_at, thread_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            data["thread_id"],
                            data["from_id"],
                            data["to_id"],
                            data["type"],
                            data.get("content"),
                            data.get("weaver"),
                            data["created_at"],
                            thread_hash,
                        ),
                    )
                    count += 1
                except Exception as e:
                    logger.error(f"Failed to migrate {thread_file.name}: {e}")

            conn.commit()

        logger.info(f"Migrated {count} threads from JSON to SQLite")

        # Rename old dir as backup
        migrated_dir = threads_dir.parent / "threads_migrated"
        if not migrated_dir.exists():
            threads_dir.rename(migrated_dir)
            logger.info(f"Renamed {threads_dir} to {migrated_dir}")

        return count


# JSON Storage for Structured Artifacts


class JSONStore:
    """Storage for roster (JSON), sites (JSON), folios/threads (SQLite)."""

    def __init__(self, base_dir: Path = None):
        self.base_dir = base_dir
        self.roster_dir = base_dir / "roster"
        self.sites_dir = base_dir / "sites"

        # Ensure directories exist
        self.roster_dir.mkdir(exist_ok=True)
        self.sites_dir.mkdir(exist_ok=True)

        # SQLite-backed storage for threads and folios
        db_path = base_dir / "skein.db"
        self._log_db = LogDatabase(db_path)

        # Auto-migrate folios from JSON to SQLite if needed
        self._log_db.migrate_folios_from_json(self.sites_dir)

    # Roster Operations

    def save_agent(self, agent: AgentInfo) -> bool:
        """Save agent registration."""
        agents_file = self.roster_dir / "agents.json"
        agents = self._load_json(agents_file, [])

        # Update or append
        existing_idx = next(
            (i for i, a in enumerate(agents) if a["agent_id"] == agent.agent_id), None
        )
        agent_dict = agent.model_dump(mode="json")

        if existing_idx is not None:
            agents[existing_idx] = agent_dict
        else:
            agents.append(agent_dict)

        self._save_json(agents_file, agents)
        return True

    def get_agents(self, status: Optional[str] = None) -> List[AgentInfo]:
        """Get registered agents, optionally filtered by status."""
        agents_file = self.roster_dir / "agents.json"
        agents_data = self._load_json(agents_file, [])
        agents = [AgentInfo(**self._normalize_datetime_fields(a)) for a in agents_data]

        if status is not None:
            agents = [a for a in agents if a.status == status]

        return agents

    def get_agent(self, agent_id: str) -> Optional[AgentInfo]:
        """Get specific agent."""
        agents = self.get_agents()
        return next((a for a in agents if a.agent_id == agent_id), None)

    # Site Operations

    def save_site(self, site: Site) -> bool:
        """Save site metadata."""
        site_dir = self.sites_dir / site.site_id
        site_dir.mkdir(exist_ok=True)

        metadata_file = site_dir / "metadata.json"
        self._save_json(metadata_file, site.model_dump(mode="json"))

        # Ensure folios directory exists
        (site_dir / "folios").mkdir(exist_ok=True)
        return True

    def get_sites(self) -> List[Site]:
        """Get all sites."""
        sites = []
        for site_dir in self.sites_dir.iterdir():
            if site_dir.is_dir():
                metadata_file = site_dir / "metadata.json"
                if metadata_file.exists():
                    site_data = self._normalize_datetime_fields(
                        self._load_json(metadata_file)
                    )
                    sites.append(Site(**site_data))
        return sites

    def get_site(self, site_id: str) -> Optional[Site]:
        """Get specific site."""
        metadata_file = self.sites_dir / site_id / "metadata.json"
        if metadata_file.exists():
            return Site(
                **self._normalize_datetime_fields(self._load_json(metadata_file))
            )
        return None

    def update_site(
        self,
        site_id: str,
        status: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Site]:
        """Update site status and/or metadata."""
        site = self.get_site(site_id)
        if not site:
            return None

        if status is not None:
            site.status = status

        if metadata is not None:
            site.metadata.update(metadata)

        self.save_site(site)
        return site

    # Folio Operations (SQLite-backed)

    def save_folio(self, folio: Folio, editor: Optional[str] = None) -> bool:
        """Save folio to SQLite.

        content_hash is computed at the write chokepoint (LogDatabase.save_folio),
        which also maintains versions/refs and the supersedes/reverted edges. The
        ``editor`` (the per-edit agent) is threaded through to the supersedes/
        reverted edge weaver; it is NOT the folio's created_by (the genesis author).
        """
        return self._log_db.save_folio(folio, editor=editor)

    def get_folios(self, site_id: Optional[str] = None) -> List[Folio]:
        """Get folios, optionally filtered by site."""
        return self._log_db.get_folios(site_id=site_id)

    def get_folio(self, folio_id: str) -> Optional[Folio]:
        """Get specific folio by ID."""
        # No recompute-on-read: the hash is maintained at write time, and a read
        # must never mutate the store (the old write-on-read was a surprise write).
        return self._log_db.get_folio(folio_id)

    def get_version_by_hash(self, content_hash: str) -> Optional[VersionView]:
        """By-hash fetch (§8): a version's immutable content + is_head/lineage_head,
        no mutable control. Resolves superseded versions too."""
        return self._log_db.get_version_by_hash(content_hash)

    def station_index(self) -> "StoreStationIndex":
        """A StationIndex over this store's versions/refs for address resolution."""
        return StoreStationIndex(self._log_db.db_path)

    def move_folio(self, folio_id: str, dest_site_id: str) -> Optional[Folio]:
        """
        Move a folio to a different site.

        Returns the updated folio on success, None if folio not found.
        Raises ValueError if destination site doesn't exist.
        """
        # Verify destination site exists
        dest_site_dir = self.sites_dir / dest_site_id
        if not dest_site_dir.exists():
            raise ValueError(f"Destination site '{dest_site_id}' does not exist")

        return self._log_db.move_folio(folio_id, dest_site_id)

    def search_folios(self, query: str, limit: int = 50) -> List[Folio]:
        """Full-text search across folios."""
        return self._log_db.search_folios(query, limit=limit)

    def get_site_last_activity(self) -> Dict[str, datetime]:
        """Return mapping of site_id -> latest folio created_at (timezone-aware)."""
        return self._log_db.get_site_last_activity()

    # Thread Operations

    def save_thread(self, thread: Thread) -> bool:
        """Save thread to SQLite."""
        return self._log_db.save_thread(thread)

    def get_threads(
        self,
        from_id: Optional[str] = None,
        to_id: Optional[str] = None,
        type: Optional[str] = None,
        weaver: Optional[str] = None,
    ) -> List[Thread]:
        """Get threads with optional filters via SQLite. BYTE-FAITHFUL — see
        :meth:`LogDatabase.get_threads`; for the client read boundary use
        :meth:`get_threads_display`."""
        return self._log_db.get_threads(
            from_id=from_id, to_id=to_id, type=type, weaver=weaver
        )

    def get_thread_by_hash(self, thread_hash: str) -> Optional[Dict[str, Any]]:
        """Return one byte-faithful thread row by its federated content hash."""
        return self._log_db.get_thread_by_hash(thread_hash)

    def get_threads_display(
        self,
        from_id: Optional[str] = None,
        to_id: Optional[str] = None,
        type: Optional[str] = None,
    ) -> List[Thread]:
        """Presentation reader that resolves genesis-keyed control threads back to
        the folio slug (Phase 3a). See :meth:`LogDatabase.get_threads_display`."""
        return self._log_db.get_threads_display(
            from_id=from_id, to_id=to_id, type=type
        )

    def get_latest_statuses(
        self, folio_ids: Optional[List[str]] = None
    ) -> Dict[str, str]:
        """Get the most recent status for each folio in a single query."""
        return self._log_db.get_latest_statuses(folio_ids)

    def get_latest_assignments(
        self, folio_ids: Optional[List[str]] = None
    ) -> Dict[str, str]:
        """Get the most recent assignment for each folio in a single query."""
        return self._log_db.get_latest_assignments(folio_ids)

    # Helper methods

    def _normalize_datetime_fields(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize datetime fields to be timezone-aware.

        Pydantic datetime parsing is inconsistent - some datetimes are parsed as
        timezone-aware, others as naive. This causes comparison errors.

        Convert all datetime strings to timezone-aware (UTC) format.
        """
        datetime_fields = ["created_at", "registered_at", "acknowledged_at", "read_at"]

        for field in datetime_fields:
            if field in data and data[field]:
                dt_str = data[field]
                # If it's already a datetime object, skip
                if isinstance(dt_str, datetime):
                    continue

                # Parse the datetime string
                try:
                    # Try parsing with timezone first
                    if dt_str.endswith("Z"):
                        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    elif "+" in dt_str or dt_str.count(":") > 2:
                        dt = datetime.fromisoformat(dt_str)
                    else:
                        # Naive datetime - assume UTC
                        dt = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)

                    # Convert to ISO format with timezone
                    data[field] = dt.isoformat()
                except (ValueError, AttributeError):
                    # If parsing fails, leave as-is
                    pass

        return data

    def _load_json(self, file_path: Path, default=None):
        """Load JSON file."""
        if not file_path.exists():
            return default if default is not None else {}

        with open(file_path, "r") as f:
            return json.load(f)

    def _save_json(self, file_path: Path, data):
        """Save JSON file."""
        with open(file_path, "w") as f:
            json.dump(data, f, indent=2, default=str)


# Legacy module-level instances removed - use Depends(get_project_log_db) and Depends(get_project_store) in routes.py
