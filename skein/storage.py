"""
SKEIN storage layer: SQLite for logs, JSON for structured artifacts.
Multi-project support via ~/.skein/projects.json registry.
"""

import sqlite3
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from contextlib import contextmanager

from .models import AgentInfo, Site, Folio, Thread, LogLine

try:
    from knurl import canon, hash as knurl_hash
    KNURL_AVAILABLE = True
except ImportError:
    KNURL_AVAILABLE = False

logger = logging.getLogger(__name__)


def compute_folio_hash(folio: Folio) -> str:
    """Compute content-addressable hash of folio's immutable fields."""
    if not KNURL_AVAILABLE:
        return None

    # Only hash immutable fields
    immutable = {
        "type": folio.type,
        "title": folio.title,
        "content": folio.content,
        "created_at": folio.created_at.isoformat() if folio.created_at else None,
        "created_by": folio.created_by,
    }
    canonical = canon.serialize(immutable)
    return knurl_hash.compute(canonical.decode('utf-8'), prefix="folio")


# Project Registry
def load_project_registry() -> Dict[str, Dict[str, Any]]:
    """Load project registry from ~/.skein/projects.json."""
    registry_file = Path.home() / '.skein' / 'projects.json'
    if not registry_file.exists():
        logger.warning("No ~/.skein/projects.json found, using default data dir")
        return {}

    try:
        with open(registry_file) as f:
            data = json.load(f)
            return data.get('projects', {})
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
            data_dir = Path(registry[project_id]['data_dir'])
            data_dir.mkdir(parents=True, exist_ok=True)
            return data_dir
        else:
            raise ValueError(f"Project '{project_id}' not found in registry")

    # No project_id provided - this shouldn't happen in normal operation
    raise ValueError("No project_id provided and no default available")


# Legacy module-level variables removed - use project-specific instances via get_data_dir_for_project()


# SQLite Database for Logs

class LogDatabase:
    """SQLite database for log storage and querying."""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stream_id TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    level TEXT,
                    source TEXT,
                    message TEXT NOT NULL,
                    metadata JSON
                )
            """)

            # Create indexes
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_stream_time
                ON logs(stream_id, timestamp DESC)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_stream_level
                ON logs(stream_id, level)
            """)

            # Full-text search
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS logs_fts
                USING fts5(message, content=logs)
            """)

            # Screenshots table
            conn.execute("""
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
            """)

            # Create indexes for screenshots
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_screenshots_strand
                ON screenshots(strand_id, timestamp DESC)
            """)

            conn.commit()

    @contextmanager
    def _get_connection(self):
        """Get database connection context manager."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def add_logs(self, stream_id: str, source: str, lines: List[Dict[str, Any]]) -> int:
        """Add log lines to database."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            count = 0

            for line in lines:
                cursor.execute("""
                    INSERT INTO logs (stream_id, level, source, message, metadata)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    stream_id,
                    line.get("level", "INFO"),
                    source,
                    line.get("message", ""),
                    json.dumps(line.get("metadata", {}))
                ))
                count += 1

            conn.commit()
            return count

    def get_logs(
        self,
        stream_id: str,
        since: Optional[str] = None,
        level: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 1000
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
                query = f"""
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
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    level=row["level"],
                    source=row["source"],
                    message=row["message"],
                    metadata=json.loads(row["metadata"]) if row["metadata"] else {}
                )
                for row in rows
            ]

    def get_streams(self) -> List[Dict[str, Any]]:
        """Get list of all log streams."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT
                    stream_id,
                    COUNT(*) as line_count,
                    MIN(timestamp) as first_log,
                    MAX(timestamp) as last_log
                FROM logs
                GROUP BY stream_id
                ORDER BY last_log DESC
            """)

            return [dict(row) for row in cursor.fetchall()]

    def add_screenshot(
        self,
        screenshot_id: str,
        strand_id: str,
        turn_number: Optional[int],
        label: str,
        file_path: str,
        file_size: int,
        metadata: Dict[str, Any]
    ) -> bool:
        """Add screenshot metadata to database."""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO screenshots (screenshot_id, strand_id, turn_number, label, file_path, file_size, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                screenshot_id,
                strand_id,
                turn_number,
                label,
                file_path,
                file_size,
                json.dumps(metadata)
            ))
            conn.commit()
            return True

    def get_screenshots(
        self,
        strand_id: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 50
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
                "SELECT * FROM screenshots WHERE screenshot_id = ?",
                (screenshot_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None


# JSON Storage for Structured Artifacts

class JSONStore:
    """JSON-based storage for roster, sites, folios, signals."""

    def __init__(self, base_dir: Path = None):
        self.base_dir = base_dir
        self.roster_dir = base_dir / "roster"
        self.sites_dir = base_dir / "sites"
        self.threads_dir = base_dir / "threads"

        # Ensure directories exist
        self.roster_dir.mkdir(exist_ok=True)
        self.sites_dir.mkdir(exist_ok=True)
        self.threads_dir.mkdir(exist_ok=True)

    # Roster Operations

    def save_agent(self, agent: AgentInfo) -> bool:
        """Save agent registration."""
        agents_file = self.roster_dir / "agents.json"
        agents = self._load_json(agents_file, [])

        # Update or append
        existing_idx = next((i for i, a in enumerate(agents) if a["agent_id"] == agent.agent_id), None)
        agent_dict = agent.model_dump(mode='json')

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
        agents = [AgentInfo(**a) for a in agents_data]

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
        self._save_json(metadata_file, site.model_dump(mode='json'))

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
                    site_data = self._load_json(metadata_file)
                    sites.append(Site(**site_data))
        return sites

    def get_site(self, site_id: str) -> Optional[Site]:
        """Get specific site."""
        metadata_file = self.sites_dir / site_id / "metadata.json"
        if metadata_file.exists():
            return Site(**self._load_json(metadata_file))
        return None

    # Folio Operations

    def save_folio(self, folio: Folio) -> bool:
        """Save folio to site."""
        site_dir = self.sites_dir / folio.site_id
        if not site_dir.exists():
            logger.error(f"Site {folio.site_id} does not exist")
            return False

        # Compute content hash if not present
        if not folio.content_hash and KNURL_AVAILABLE:
            folio.content_hash = compute_folio_hash(folio)

        folios_dir = site_dir / "folios"
        folios_dir.mkdir(exist_ok=True)

        folio_file = folios_dir / f"{folio.folio_id}.json"
        self._save_json(folio_file, folio.model_dump(mode='json'))
        return True

    def get_folios(self, site_id: Optional[str] = None) -> List[Folio]:
        """Get folios, optionally filtered by site."""
        folios = []

        if site_id:
            site_dirs = [self.sites_dir / site_id]
        else:
            site_dirs = [d for d in self.sites_dir.iterdir() if d.is_dir()]

        for site_dir in site_dirs:
            folios_dir = site_dir / "folios"
            if folios_dir.exists():
                for folio_file in folios_dir.glob("*.json"):
                    folio_data = self._load_json(folio_file)
                    # Normalize datetime fields to prevent comparison errors
                    folio_data = self._normalize_datetime_fields(folio_data)
                    folios.append(Folio(**folio_data))

        return folios

    def get_folio(self, folio_id: str) -> Optional[Folio]:
        """Get specific folio by ID."""
        # Search all sites
        for site_dir in self.sites_dir.iterdir():
            if site_dir.is_dir():
                folio_file = site_dir / "folios" / f"{folio_id}.json"
                if folio_file.exists():
                    folio_data = self._load_json(folio_file)
                    folio_data = self._normalize_datetime_fields(folio_data)
                    folio = Folio(**folio_data)

                    # Lazy hash: compute and save if missing
                    if not folio.content_hash and KNURL_AVAILABLE:
                        folio.content_hash = compute_folio_hash(folio)
                        self._save_json(folio_file, folio.model_dump(mode='json'))

                    return folio
        return None

    def move_folio(self, folio_id: str, dest_site_id: str) -> Optional[Folio]:
        """
        Move a folio from its current site to a different site.

        Returns the updated folio on success, None if folio not found.
        Raises ValueError if destination site doesn't exist.
        """
        # Find the folio and its current location
        source_site_id = None
        source_file = None
        for site_dir in self.sites_dir.iterdir():
            if site_dir.is_dir():
                folio_file = site_dir / "folios" / f"{folio_id}.json"
                if folio_file.exists():
                    source_site_id = site_dir.name
                    source_file = folio_file
                    break

        if not source_file or not source_site_id:
            return None

        # Verify destination site exists
        dest_site_dir = self.sites_dir / dest_site_id
        if not dest_site_dir.exists():
            raise ValueError(f"Destination site '{dest_site_id}' does not exist")

        # Load the folio
        folio_data = self._load_json(source_file)
        folio_data = self._normalize_datetime_fields(folio_data)

        # Update site_id
        old_site_id = folio_data.get("site_id")
        folio_data["site_id"] = dest_site_id

        # Ensure destination folios directory exists
        dest_folios_dir = dest_site_dir / "folios"
        dest_folios_dir.mkdir(exist_ok=True)

        # Save to new location
        dest_file = dest_folios_dir / f"{folio_id}.json"
        self._save_json(dest_file, folio_data)

        # Delete from old location
        source_file.unlink()

        logger.info(f"Moved folio {folio_id} from {old_site_id} to {dest_site_id}")
        return Folio(**folio_data)

    # Thread Operations

    def save_thread(self, thread: Thread) -> bool:
        """Save thread."""
        thread_file = self.threads_dir / f"{thread.thread_id}.json"
        self._save_json(thread_file, thread.model_dump(mode='json'))
        return True

    def get_threads(self, from_id: Optional[str] = None, to_id: Optional[str] = None, type: Optional[str] = None, weaver: Optional[str] = None) -> List[Thread]:
        """Get threads with optional filters."""
        threads = []
        for thread_file in self.threads_dir.glob("*.json"):
            thread_data = self._load_json(thread_file)
            thread = Thread(**thread_data)

            # Apply filters
            if from_id and thread.from_id != from_id:
                continue
            if to_id and thread.to_id != to_id:
                continue
            if type and thread.type != type:
                continue
            if weaver and thread.weaver != weaver:
                continue

            threads.append(thread)

        return threads

    def get_inbox(self, agent_id: str, unread_only: bool = False) -> List[Thread]:
        """
        Get agent's inbox with full conversation context.

        Includes:
        1. Threads TO agent (to_id=agent_id)
        2. Threads WOVEN BY agent (weaver=agent_id)
        3. Replies to threads agent is involved in (recursive)
        """
        # Start with threads TO agent (direct messages)
        direct_threads = self.get_threads(to_id=agent_id)

        # Add threads WOVEN BY agent (threads they created)
        woven_threads = self.get_threads(weaver=agent_id)

        # Combine and deduplicate
        thread_map = {}
        for t in direct_threads + woven_threads:
            thread_map[t.thread_id] = t

        # Find replies to any threads in the inbox
        # A reply can have either:
        #   - from_id = thread_id (thread chaining: thread-A -> thread-B)
        #   - to_id = thread_id (agent reply: agent -> thread-A via 'skein reply')
        all_threads = self.get_threads()
        involved_thread_ids = set(thread_map.keys())

        # Keep adding reply layers until we find no more
        # Limit depth to prevent performance issues
        max_depth = 5
        for _ in range(max_depth):
            found_new = False
            for thread in all_threads:
                if thread.thread_id in thread_map:
                    continue
                # Include if from_id or to_id is a thread we care about
                if thread.from_id in involved_thread_ids or thread.to_id in involved_thread_ids:
                    thread_map[thread.thread_id] = thread
                    involved_thread_ids.add(thread.thread_id)
                    found_new = True

            if not found_new:
                break

        threads = list(thread_map.values())

        if unread_only:
            threads = [t for t in threads if t.read_at is None]

        # Sort by created_at, most recent first
        threads.sort(key=lambda t: t.created_at, reverse=True)

        return threads

    def mark_thread_read(self, thread_id: str) -> bool:
        """Mark thread as read."""
        thread_file = self.threads_dir / f"{thread_id}.json"
        if not thread_file.exists():
            return False

        thread_data = self._load_json(thread_file)
        thread_data["read_at"] = datetime.now().isoformat()
        self._save_json(thread_file, thread_data)
        return True

    # Helper methods

    def _normalize_datetime_fields(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize datetime fields to be timezone-aware.

        Pydantic datetime parsing is inconsistent - some datetimes are parsed as
        timezone-aware, others as naive. This causes comparison errors.

        Convert all datetime strings to timezone-aware (UTC) format.
        """
        datetime_fields = ['created_at', 'registered_at', 'acknowledged_at', 'read_at']

        for field in datetime_fields:
            if field in data and data[field]:
                dt_str = data[field]
                # If it's already a datetime object, skip
                if isinstance(dt_str, datetime):
                    continue

                # Parse the datetime string
                try:
                    # Try parsing with timezone first
                    if dt_str.endswith('Z'):
                        dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
                    elif '+' in dt_str or dt_str.count(':') > 2:
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

        with open(file_path, 'r') as f:
            return json.load(f)

    def _save_json(self, file_path: Path, data):
        """Save JSON file."""
        with open(file_path, 'w') as f:
            json.dump(data, f, indent=2, default=str)


# Legacy module-level instances removed - use Depends(get_project_log_db) and Depends(get_project_store) in routes.py
