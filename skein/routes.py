"""
SKEIN FastAPI routes.
"""

import logging
import base64
import re
from datetime import datetime
from typing import List, Optional, Dict, Any
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query, Header, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .models import (
    AgentRegistration, AgentInfo,
    SiteCreate, Site,
    FolioCreate, Folio, FolioUpdate,
    ThreadCreate, Thread,
    LogBatch, LogLine,
    FolioType,
    ScreenshotCreate, Screenshot,
    YieldCreate, Yield
)
from .storage import JSONStore, LogDatabase, get_data_dir_for_project
from .utils import (
    generate_folio_id, generate_thread_id, generate_yield_id, parse_mentions,
    get_current_status, get_current_assignment,
    auto_invalidate_cache,
    parse_relative_time,
    generate_agent_name
)

logger = logging.getLogger(__name__)

router = APIRouter()


# Multi-project support
def get_project_store(x_project_id: Optional[str] = Header(None)) -> JSONStore:
    """
    Get JSONStore for the requested project.

    Uses X-Project-Id header to determine which project's data to use.
    Raises error if no project specified - forces proper `skein init` setup.
    """
    if not x_project_id:
        raise HTTPException(
            status_code=400,
            detail="No project specified. Run 'skein init --project PROJECT_NAME' in your project directory first."
        )

    data_dir = get_data_dir_for_project(x_project_id)
    logger.info(f"Using project '{x_project_id}' data dir: {data_dir}")
    return JSONStore(data_dir)


def get_project_log_db(x_project_id: Optional[str] = Header(None)) -> LogDatabase:
    """
    Get LogDatabase for the requested project.

    Uses X-Project-Id header to determine which project's data to use.
    Each project gets its own SQLite database at .skein/data/skein.db
    """
    if not x_project_id:
        raise HTTPException(
            status_code=400,
            detail="No project specified. Run 'skein init --project PROJECT_NAME' in your project directory first."
        )

    data_dir = get_data_dir_for_project(x_project_id)
    db_path = data_dir / "skein.db"
    logger.info(f"Using project '{x_project_id}' log db: {db_path}")
    return LogDatabase(db_path)


def get_project_screenshots_dir(x_project_id: Optional[str] = Header(None)) -> Path:
    """
    Get screenshots directory for the requested project.

    Uses X-Project-Id header to determine which project's data to use.
    Each project gets its own screenshots at .skein/data/screenshots/
    """
    if not x_project_id:
        raise HTTPException(
            status_code=400,
            detail="No project specified. Run 'skein init --project PROJECT_NAME' in your project directory first."
        )

    data_dir = get_data_dir_for_project(x_project_id)
    screenshots_dir = data_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Using project '{x_project_id}' screenshots dir: {screenshots_dir}")
    return screenshots_dir


# Roster Endpoints

@router.post("/roster/register")
async def register_agent(
    registration: AgentRegistration,
    store: JSONStore = Depends(get_project_store)
):
    """Register an agent in the roster."""
    agent = AgentInfo(
        agent_id=registration.agent_id,
        name=registration.name,
        agent_type=registration.agent_type,
        description=registration.description,
        registered_at=datetime.now(),
        capabilities=registration.capabilities,
        status=registration.status or "active",
        metadata=registration.metadata
    )

    success = store.save_agent(agent)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to register agent")

    return {"success": True, "registration": agent}


@router.get("/roster", response_model=List[AgentInfo])
async def get_roster(
    status: Optional[str] = Query(None, description="Filter by status: active, retired"),
    store: JSONStore = Depends(get_project_store)
):
    """Get registered agents, optionally filtered by status."""
    return store.get_agents(status=status)


@router.get("/roster/{agent_id}", response_model=AgentInfo)
async def get_agent(agent_id: str, store: JSONStore = Depends(get_project_store)):
    """Get specific agent."""
    agent = store.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


class AgentUpdate(BaseModel):
    """Model for updating agent fields."""
    status: Optional[str] = None
    name: Optional[str] = None
    agent_type: Optional[str] = None
    description: Optional[str] = None
    capabilities: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None


@router.patch("/roster/{agent_id}")
async def update_agent(
    agent_id: str,
    update: AgentUpdate,
    store: JSONStore = Depends(get_project_store)
):
    """Update agent registration."""
    agent = store.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if update.status is not None:
        agent.status = update.status
    if update.name is not None:
        agent.name = update.name
    if update.agent_type is not None:
        agent.agent_type = update.agent_type
    if update.description is not None:
        agent.description = update.description
    if update.capabilities is not None:
        agent.capabilities = update.capabilities
    if update.metadata is not None:
        # Merge metadata rather than replace
        agent.metadata.update(update.metadata)

    store.save_agent(agent)
    return {"success": True, "agent": agent}


# Site Endpoints

@router.post("/sites")
async def create_site(
    site_create: SiteCreate,
    x_agent_id: str = Header(None, alias="X-Agent-Id"),
    store: JSONStore = Depends(get_project_store)
):
    """Create a new site."""
    created_by = x_agent_id or "unknown"

    site = Site(
        site_id=site_create.site_id,
        created_at=datetime.now(),
        created_by=created_by,
        purpose=site_create.purpose,
        status="active",
        metadata=site_create.metadata
    )

    success = store.save_site(site)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to create site")

    return {"success": True, "site": site}


@router.get("/sites", response_model=List[Site])
async def get_sites(
    status: Optional[str] = None,
    tag: Optional[str] = None,
    store: JSONStore = Depends(get_project_store)
):
    """Get all sites with optional filters."""
    sites = store.get_sites()

    if status:
        sites = [s for s in sites if s.status == status]

    if tag:
        sites = [s for s in sites if tag in s.metadata.get("tags", [])]

    return sites


@router.get("/sites/{site_id}", response_model=Site)
async def get_site(site_id: str, store: JSONStore = Depends(get_project_store)):
    """Get specific site."""
    site = store.get_site(site_id)
    if not site:
        # Include available active sites in error message for better UX
        all_sites = store.get_sites()
        active_sites = [s for s in all_sites if s.status == "active"]
        if active_sites:
            site_ids = [s.site_id for s in active_sites[:50]]
            suffix = f" (+{len(active_sites) - 50} more)" if len(active_sites) > 50 else ""
            raise HTTPException(
                status_code=404,
                detail=f"Site '{site_id}' not found. Active sites: {', '.join(site_ids)}{suffix}. Run 'skein sites' for full list."
            )
        raise HTTPException(status_code=404, detail=f"Site '{site_id}' not found. No active sites exist - create one with 'skein site create <id> \"description\"'")
    return site


@router.get("/sites/{site_id}/folios", response_model=List[Folio])
async def get_site_folios(
    site_id: str,
    type: Optional[FolioType] = None,
    since: Optional[str] = None,
    store: JSONStore = Depends(get_project_store)
):
    """Get folios for a specific site."""
    folios = store.get_folios(site_id=site_id)

    # PURE THREADS: Compute status and assigned_to from threads
    for folio in folios:
        computed_status = get_current_status(folio.folio_id, store)
        computed_assignment = get_current_assignment(folio.folio_id, store)

        # Use computed values, fall back to stored values during migration
        folio.status = computed_status or folio.status or "open"
        folio.assigned_to = computed_assignment or folio.assigned_to

    if type:
        folios = [f for f in folios if f.type == type]

    if since:
        since_dt = datetime.fromisoformat(since)
        folios = [f for f in folios if f.created_at >= since_dt]

    return folios


# Title validation
GENERIC_TITLES = {
    'handoff', 'handoff brief', 'brief', 'untitled', 'test', 'title',
    'issue', 'friction', 'finding', 'notion', 'summary', 'tender', 'writ',
    'new folio', 'folio', 'update', 'fix', 'change', 'todo', 'task'
}

TITLE_EXAMPLES = {
    'brief': 'e.g., "Implement OAuth for API endpoints" or "Fix race condition in websocket handler"',
    'issue': 'e.g., "Agents crash when site_id contains spaces" or "Memory leak in long-running sessions"',
    'friction': 'e.g., "Must restart server after config changes" or "Error messages don\'t show line numbers"',
    'finding': 'e.g., "Redis caching reduces latency by 40%" or "Users prefer dark mode 3:1"',
    'tender': 'e.g., "Auth refactor ready for review" or "New dashboard component complete"',
    'notion': 'e.g., "Could use websockets for real-time updates" or "Consider caching user preferences"',
    'summary': 'e.g., "Completed OAuth integration" or "Session retrospective: agent coordination"',
}

# Patterns for shard/worktree IDs:
# - 65af2039-20251205-001 (8-char hex prefix)
# - bucket-1210-20251210-001 (name-based)
# - eaa09237-20251207-154442 (hex-date-timestamp)
SHARD_ID_PATTERN = re.compile(
    r'^[a-f0-9]{8}-\d{8}-\d{3,6}:\s*|'  # 8-char hex: 65af2039-20251205-001:
    r'^[a-z]+-\d{4}-\d{8}-\d{3}:\s*',    # name-based: bucket-1210-20251210-001:
    re.IGNORECASE
)

# Folio type prefixes that are redundant (the type field already says this)
TYPE_PREFIX_PATTERN = re.compile(
    r'^(tender|brief|issue|finding|friction|notion|summary|writ|playbook|mantle|plan):\s*',
    re.IGNORECASE
)

# Status markers often copied from content (handles markdown bold or plain)
STATUS_MARKER_PATTERN = re.compile(r'(\*\*)?Status:(\*\*)?\s*\w+\.?\s*', re.IGNORECASE)


def validate_folio_title(title: str, folio_type: str) -> str:
    """
    Validate and clean folio title. Returns cleaned title or raises HTTPException.

    Poka-yoke design: make it hard to create bad titles.

    Cleans:
    - Markdown cruft (headers, bold markers)
    - Shard/worktree IDs (e.g., "65af2039-20251205-001:")
    - Redundant type prefixes (e.g., "Tender:")
    - Status markers (e.g., "**Status:** complete")
    """
    # Must have a title
    if not title or not title.strip():
        example = TITLE_EXAMPLES.get(folio_type, 'e.g., "Clear description of what this folio is about"')
        raise HTTPException(
            status_code=400,
            detail=f"{folio_type.capitalize()} needs a title that describes what it's about.\n\n{example}"
        )

    title = title.strip()

    # Strip markdown cruft
    title = re.sub(r'^#+\s*', '', title)  # Leading headers
    title = re.sub(r'^\*\*(.+?)\*\*', r'\1', title)  # Bold wrapper (keep content)
    title = re.sub(r'^__(.+?)__', r'\1', title)  # Underscore bold wrapper
    title = title.strip()

    # Strip status markers (must be before stripping bold, uses markdown)
    title = STATUS_MARKER_PATTERN.sub('', title)

    # Strip redundant type prefixes FIRST (they come before shard IDs)
    title = TYPE_PREFIX_PATTERN.sub('', title)

    # Strip shard/worktree IDs from start (after type prefix is removed)
    title = SHARD_ID_PATTERN.sub('', title)

    # Clean up any remaining type prefixes (in case of "## Tender: shard-id: ...")
    title = TYPE_PREFIX_PATTERN.sub('', title)

    title = title.strip()

    # Check for generic/lazy titles
    if title.lower() in GENERIC_TITLES:
        example = TITLE_EXAMPLES.get(folio_type, 'e.g., "Clear description of what this folio is about"')
        raise HTTPException(
            status_code=400,
            detail=f"\"{title}\" is too generic - what's this {folio_type} actually about?\n\n{example}"
        )

    # Check minimum length (avoid "ok", "done", etc.)
    if len(title) < 10:
        example = TITLE_EXAMPLES.get(folio_type, 'e.g., "Clear description of what this folio is about"')
        raise HTTPException(
            status_code=400,
            detail=f"\"{title}\" is too brief ({len(title)} chars) - give a bit more detail so others know what this covers.\n\n{example}"
        )

    # Truncate if too long (but don't reject - just fix it)
    if len(title) > 100:
        title = title[:97] + "..."

    return title


@router.post("/sites/{site_id}/folios")
async def post_to_site(
    site_id: str,
    folio_create: FolioCreate,
    x_agent_id: str = Header(None, alias="X-Agent-Id"),
    store: JSONStore = Depends(get_project_store)
):
    """Post a folio to a site."""
    # Validate and clean title
    folio_create.title = validate_folio_title(folio_create.title, folio_create.type)

    # Verify site exists
    site = store.get_site(site_id)
    if not site:
        # Include available active sites in error message for better UX
        all_sites = store.get_sites()
        active_sites = [s for s in all_sites if s.status == "active"]
        if active_sites:
            site_ids = [s.site_id for s in active_sites[:50]]
            suffix = f" (+{len(active_sites) - 50} more)" if len(active_sites) > 50 else ""
            raise HTTPException(
                status_code=404,
                detail=f"Site '{site_id}' not found. Active sites: {', '.join(site_ids)}{suffix}. Run 'skein sites' for full list."
            )
        raise HTTPException(status_code=404, detail=f"Site '{site_id}' not found. No active sites exist - create one with 'skein site create <id> \"description\"'")

    created_by = x_agent_id or "unknown"
    folio_id = generate_folio_id(folio_create.type)

    # Pure threads migration: don't set status/assigned_to in folio
    # They will be computed from threads
    folio = Folio(
        folio_id=folio_id,
        type=folio_create.type,
        site_id=site_id,
        created_at=datetime.now(),
        created_by=created_by,
        title=folio_create.title,
        content=folio_create.content,
        status="open",  # Temporary: will be removed after migration
        assigned_to=None,  # Temporary: will be removed after migration
        target_agent=folio_create.target_agent,
        successor_name=folio_create.successor_name,
        omlet=folio_create.omlet,
        archived=False,
        metadata=folio_create.metadata
    )

    success = store.save_folio(folio)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to save folio")

    # Parse @mentions and create threads
    mentions = parse_mentions(folio_create.content)
    for mention in mentions:
        thread = Thread(
            thread_id=generate_thread_id(),
            from_id=folio_id,
            to_id=mention,
            type="mention",
            content=f"Mentioned in {folio_create.type}: {folio_create.title}",
            weaver=created_by,
            created_at=datetime.now()
        )
        store.save_thread(thread)

    # SUGAR API: Create status thread if status provided (undocumented)
    # Don't create default status - let patterns emerge naturally
    # Only create if explicitly provided AND not "open" (which is just noise)
    if folio_create.metadata.get("status") and folio_create.metadata.get("status") != "open":
        status_thread = Thread(
            thread_id=generate_thread_id(),
            from_id=folio_id,
            to_id=folio_id,
            type="status",
            content=folio_create.metadata.get("status"),
            weaver=created_by,
            created_at=datetime.now()
        )
        store.save_thread(status_thread)
        auto_invalidate_cache("status", folio_id)

    # SUGAR API: Create assignment thread if assigned_to provided (undocumented)
    if folio_create.assigned_to:
        assignment_thread = Thread(
            thread_id=generate_thread_id(),
            from_id=folio_id,
            to_id=folio_create.assigned_to,
            type="assignment",
            content=f"Assigned {folio_create.type}: {folio_create.title}",
            weaver=created_by,
            created_at=datetime.now()
        )
        store.save_thread(assignment_thread)
        auto_invalidate_cache("assignment", folio_id)

    # Create thread for target_agent if set (for briefs)
    if folio_create.target_agent:
        thread = Thread(
            thread_id=generate_thread_id(),
            from_id=folio_id,
            to_id=folio_create.target_agent,
            type="message",
            content=f"Brief for you: {folio_create.title}",
            weaver=created_by,
            created_at=datetime.now()
        )
        store.save_thread(thread)

    return {"success": True, "folio_id": folio_id}


# Folio Endpoints

@router.post("/folios")
async def create_folio(
    folio_create: FolioCreate,
    x_agent_id: str = Header(None, alias="X-Agent-Id"),
    store: JSONStore = Depends(get_project_store)
):
    """Create a folio (shortcut for POST /sites/{site_id}/folios)."""
    return await post_to_site(folio_create.site_id, folio_create, x_agent_id, store)


@router.get("/folios", response_model=List[Folio])
async def get_folios(
    type: Optional[FolioType] = None,
    site_id: Optional[str] = None,
    assigned_to: Optional[str] = None,
    status: Optional[str] = None,
    archived: Optional[bool] = False,
    store: JSONStore = Depends(get_project_store)
):
    """Get folios with filters."""
    # Validate site_id is not empty string
    if site_id is not None and site_id.strip() == "":
        raise HTTPException(status_code=400, detail="site_id cannot be empty string")

    folios = store.get_folios(site_id=site_id)

    # PURE THREADS: Compute status and assigned_to from threads
    for folio in folios:
        computed_status = get_current_status(folio.folio_id, store)
        computed_assignment = get_current_assignment(folio.folio_id, store)

        # Use computed values, fall back to stored values during migration
        folio.status = computed_status or folio.status or "open"
        folio.assigned_to = computed_assignment or folio.assigned_to

    # Apply filters in Python (since we compute dynamically)
    if type:
        folios = [f for f in folios if f.type == type]

    if assigned_to:
        folios = [f for f in folios if f.assigned_to == assigned_to]

    if status:
        folios = [f for f in folios if f.status == status]

    if not archived:
        folios = [f for f in folios if not f.archived]

    return folios


@router.get("/folios/search")
async def search_folios(
    q: str = Query(...),
    type: Optional[FolioType] = None,
    status: Optional[str] = None,
    store: JSONStore = Depends(get_project_store)
):
    """Search folios by content."""
    folios = store.get_folios()

    # Simple text search for MVP
    matching = [
        f for f in folios
        if q.lower() in f.title.lower() or q.lower() in f.content.lower()
    ]

    if type:
        matching = [f for f in matching if f.type == type]

    if status:
        # Get status from threads with fallback to stored field (consistent with /folios endpoint)
        matching = [
            f for f in matching
            if (get_current_status(f.folio_id, store) or f.status or "open") == status
        ]

    return matching


@router.get("/folios/{folio_id}", response_model=Folio)
async def get_folio(folio_id: str, store: JSONStore = Depends(get_project_store)):
    """Get specific folio."""
    folio = store.get_folio(folio_id)
    if not folio:
        raise HTTPException(status_code=404, detail="Folio not found")

    # PURE THREADS: Compute status and assigned_to from threads
    computed_status = get_current_status(folio.folio_id, store)
    computed_assignment = get_current_assignment(folio.folio_id, store)

    # Use computed values, fall back to stored values during migration
    folio.status = computed_status or folio.status or "open"
    folio.assigned_to = computed_assignment or folio.assigned_to

    return folio


@router.patch("/folios/{folio_id}")
async def update_folio(
    folio_id: str,
    update: FolioUpdate,
    x_agent_id: str = Header(None, alias="X-Agent-Id"),
    store: JSONStore = Depends(get_project_store)
):
    """Update folio fields (title, content, status, assigned_to, archived)."""
    folio = store.get_folio(folio_id)
    if not folio:
        raise HTTPException(status_code=404, detail="Folio not found")

    created_by = x_agent_id or "unknown"

    # Update title and content directly on the folio
    if update.title is not None:
        folio.title = update.title

    if update.content is not None:
        folio.content = update.content

    # PURE THREADS: Create status thread instead of updating field
    if update.status is not None:
        status_thread = Thread(
            thread_id=generate_thread_id(),
            from_id=folio_id,
            to_id=folio_id,
            type="status",
            content=update.status,
            weaver=created_by,
            created_at=datetime.now()
        )
        store.save_thread(status_thread)
        auto_invalidate_cache("status", folio_id)
        # Also update field for backward compat (will be removed after migration)
        folio.status = update.status

    # PURE THREADS: Create assignment thread instead of updating field
    if update.assigned_to is not None:
        assignment_thread = Thread(
            thread_id=generate_thread_id(),
            from_id=folio_id,
            to_id=update.assigned_to,
            type="assignment",
            content=f"Assigned to {update.assigned_to}",
            weaver=created_by,
            created_at=datetime.now()
        )
        store.save_thread(assignment_thread)
        auto_invalidate_cache("assignment", folio_id)
        # Also update field for backward compat (will be removed after migration)
        folio.assigned_to = update.assigned_to

    if update.archived is not None:
        folio.archived = update.archived

    store.save_folio(folio)
    return {"success": True, "folio": folio}


# Thread Endpoints

@router.post("/threads")
async def create_thread(
    thread_create: ThreadCreate,
    x_agent_id: str = Header(None, alias="X-Agent-Id"),
    store: JSONStore = Depends(get_project_store)
):
    """Create a thread between resources."""
    thread_id = generate_thread_id()

    # Use weaver from request, or fall back to X-Agent-Id header
    weaver = thread_create.weaver or x_agent_id

    thread = Thread(
        thread_id=thread_id,
        from_id=thread_create.from_id,
        to_id=thread_create.to_id,
        type=thread_create.type,
        content=thread_create.content,
        weaver=weaver,
        created_at=datetime.now()
    )

    success = store.save_thread(thread)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to create thread")

    # Auto-invalidate caches based on thread type
    if thread_create.type == "status":
        auto_invalidate_cache("status", thread_create.to_id)
    elif thread_create.type == "assignment":
        auto_invalidate_cache("assignment", thread_create.from_id)

    return {"success": True, "thread_id": thread_id}


@router.get("/threads", response_model=List[Thread])
async def get_threads(
    from_id: Optional[str] = None,
    to_id: Optional[str] = None,
    type: Optional[str] = None,
    weaver: Optional[str] = None,
    search: Optional[str] = None,
    since: Optional[str] = None,
    store: JSONStore = Depends(get_project_store)
):
    """Get threads with optional filters.

    Args:
        from_id: Filter threads from this resource
        to_id: Filter threads to this resource
        type: Filter by thread type (message, mention, status, etc)
        weaver: Filter by creator/agent who created the thread
        search: Full-text search in thread content
        since: Time filter (e.g., '1hour', '2days', or ISO timestamp)
    """
    # Get base threads with existing filters
    threads = store.get_threads(from_id=from_id, to_id=to_id, type=type)

    # Apply weaver filter
    if weaver:
        threads = [t for t in threads if t.weaver == weaver]

    # Apply content search filter
    if search:
        search_lower = search.lower()
        threads = [
            t for t in threads
            if t.content and search_lower in t.content.lower()
        ]

    # Apply time filter
    if since:
        try:
            since_dt = parse_relative_time(since)
            # Handle timezone-naive threads (legacy data)
            filtered_threads = []
            for t in threads:
                thread_dt = t.created_at
                # If thread is naive and since_dt is aware, make thread aware (assume UTC)
                if thread_dt.tzinfo is None and since_dt.tzinfo is not None:
                    from datetime import timezone as tz
                    thread_dt = thread_dt.replace(tzinfo=tz.utc)
                filtered_threads.append((t, thread_dt >= since_dt))
            threads = [t for t, keep in filtered_threads if keep]
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    return threads


@router.get("/inbox", response_model=List[Thread])
async def get_inbox(
    x_agent_id: str = Header(..., alias="X-Agent-Id"),
    unread: Optional[bool] = None,
    store: JSONStore = Depends(get_project_store)
):
    """
    Get inbox for the calling agent with full conversation context.

    Includes:
    - Threads TO agent (to_id=agent_id) - direct messages
    - Threads WOVEN BY agent (weaver=agent_id) - threads agent created
    - Replies to agent's threads (recursive, up to 5 levels deep)

    This ensures agents see the full conversation flow, including replies
    to threads they created or were mentioned in.
    """
    unread_only = unread if unread is not None else False
    return store.get_inbox(x_agent_id, unread_only=unread_only)


@router.patch("/threads/{thread_id}/read")
async def mark_thread_read(thread_id: str, store: JSONStore = Depends(get_project_store)):
    """Mark a thread as read."""
    success = store.mark_thread_read(thread_id)

    if not success:
        raise HTTPException(status_code=404, detail="Thread not found")

    return {"success": True}


# Log Endpoints

@router.post("/logs")
async def post_logs(
    log_batch: LogBatch,
    log_db: LogDatabase = Depends(get_project_log_db)
):
    """Post logs to a stream."""
    lines = [line.model_dump() for line in log_batch.lines]
    count = log_db.add_logs(log_batch.stream_id, log_batch.source, lines)

    return {"success": True, "count": count}


@router.get("/logs/streams")
async def get_log_streams(log_db: LogDatabase = Depends(get_project_log_db)):
    """Get list of all log streams."""
    return {"streams": log_db.get_streams()}


@router.get("/logs/{stream_id}", response_model=List[LogLine])
async def get_logs(
    stream_id: str,
    since: Optional[str] = None,
    level: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(1000, le=10000),
    log_db: LogDatabase = Depends(get_project_log_db)
):
    """Get logs from a stream with filters."""
    return log_db.get_logs(stream_id, since, level, search, limit)


# Discovery Endpoints

@router.get("/activity")
async def get_activity(since: Optional[str] = None, store: JSONStore = Depends(get_project_store)):
    """Get recent activity across SKEIN."""
    # Simple implementation for MVP
    folios = store.get_folios()

    if since:
        try:
            since_dt = parse_relative_time(since)
            folios = [f for f in folios if f.created_at >= since_dt]
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # Sort by created_at, most recent first
    folios.sort(key=lambda f: f.created_at, reverse=True)

    return {
        "new_folios": folios[:10],  # Last 10 folios
        "active_agents": list({f.created_by for f in folios})
    }


# Unified Search Endpoint

@router.get("/search")
async def unified_search(
    q: str = Query(""),
    resources: str = Query("folios"),
    # Common filters
    status: Optional[str] = None,
    since: Optional[str] = None,
    before: Optional[str] = None,
    # Folio-specific
    type: Optional[FolioType] = None,
    site: Optional[str] = None,
    sites: Optional[List[str]] = Query(None),
    assigned_to: Optional[str] = None,
    archived: Optional[bool] = False,
    # Thread-specific
    thread_type: Optional[str] = None,
    weaver: Optional[str] = None,
    from_id: Optional[str] = None,
    to_id: Optional[str] = None,
    # Agent-specific
    agent_type: Optional[str] = None,
    capabilities: Optional[List[str]] = Query(None),
    # Sorting & pagination
    sort: str = Query("created"),
    limit: int = Query(50, le=500),
    offset: int = Query(0),
    store: JSONStore = Depends(get_project_store),
    x_agent_id: Optional[str] = Header(None, alias="X-Agent-Id")
):
    """
    Unified search across multiple resource types.

    Args:
        q: Search query (empty string returns all matching filters)
        resources: Comma-separated list (folios, threads, agents, sites)
        status: Filter by status (applies to folios, agents)
        since: Only items created/updated after this time
        before: Only items created before this time
        type: Folio type filter
        site: Exact site match
        sites: Site patterns (can repeat)
        assigned_to: Filter by assignee
        archived: Include archived folios
        thread_type: Thread type filter
        weaver: Thread creator (supports 'me' for current agent)
        from_id: Thread source resource
        to_id: Thread destination resource
        agent_type: Agent type filter
        capabilities: Has capabilities (can repeat)
        sort: Sort field (created, created_asc, updated, relevance)
        limit: Results per resource type (max 500)
        offset: Skip first N results
    """
    import time
    start_time = time.time()

    # Parse resources
    resource_list = [r.strip() for r in resources.split(",")]
    valid_resources = {"folios", "threads", "agents", "sites"}
    for r in resource_list:
        if r not in valid_resources:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid resource type: '{r}'. Valid: {', '.join(valid_resources)}"
            )

    # Resolve 'me' in weaver filter
    if weaver == "me" and x_agent_id:
        weaver = x_agent_id

    # Parse time filters
    since_dt = None
    before_dt = None
    if since:
        try:
            since_dt = parse_relative_time(since)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid since: {str(e)}")
    if before:
        try:
            before_dt = parse_relative_time(before)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid before: {str(e)}")

    results = {}
    total = 0

    # Search folios
    if "folios" in resource_list:
        folios = store.get_folios()

        # Compute status from threads
        for folio in folios:
            computed_status = get_current_status(folio.folio_id, store)
            computed_assignment = get_current_assignment(folio.folio_id, store)
            folio.status = computed_status or folio.status or "open"
            folio.assigned_to = computed_assignment or folio.assigned_to

        # Text search
        if q:
            q_lower = q.lower()
            folios = [
                f for f in folios
                if q_lower in f.title.lower() or q_lower in f.content.lower()
            ]

        # Filters
        if type:
            folios = [f for f in folios if f.type == type]

        if site:
            folios = [f for f in folios if f.site_id == site]

        if sites:
            # Support glob patterns
            import fnmatch
            folios = [
                f for f in folios
                if any(fnmatch.fnmatch(f.site_id, pattern) for pattern in sites)
            ]

        if status:
            folios = [f for f in folios if f.status == status]

        if assigned_to:
            folios = [f for f in folios if f.assigned_to == assigned_to]

        if not archived:
            folios = [f for f in folios if not f.archived]

        if since_dt:
            folios = [f for f in folios if f.created_at >= since_dt]

        if before_dt:
            folios = [f for f in folios if f.created_at < before_dt]

        # Sort
        if sort == "created":
            folios.sort(key=lambda f: f.created_at, reverse=True)
        elif sort == "created_asc":
            folios.sort(key=lambda f: f.created_at)
        elif sort == "relevance" and q:
            # Simple relevance: title matches > content matches
            def relevance_score(folio):
                score = 0
                q_lower = q.lower()
                if q_lower in folio.title.lower():
                    score += 10
                if q_lower in folio.content.lower():
                    score += 1
                return score
            folios.sort(key=relevance_score, reverse=True)

        # Pagination
        folios_total = len(folios)
        folios = folios[offset:offset + limit]

        results["folios"] = {
            "total": folios_total,
            "items": folios
        }
        total += folios_total

    # Search threads
    if "threads" in resource_list:
        threads = store.get_threads()

        # Text search
        if q:
            q_lower = q.lower()
            threads = [
                t for t in threads
                if t.content and q_lower in t.content.lower()
            ]

        # Filters
        if thread_type:
            threads = [t for t in threads if t.type == thread_type]

        if weaver:
            threads = [t for t in threads if t.weaver == weaver]

        if from_id:
            threads = [t for t in threads if t.from_id == from_id]

        if to_id:
            threads = [t for t in threads if t.to_id == to_id]

        if since_dt:
            # Handle timezone-naive threads (legacy data)
            filtered = []
            for t in threads:
                thread_dt = t.created_at
                if thread_dt.tzinfo is None and since_dt.tzinfo is not None:
                    from datetime import timezone as tz
                    thread_dt = thread_dt.replace(tzinfo=tz.utc)
                if thread_dt >= since_dt:
                    filtered.append(t)
            threads = filtered

        if before_dt:
            # Handle timezone-naive threads (legacy data)
            filtered = []
            for t in threads:
                thread_dt = t.created_at
                if thread_dt.tzinfo is None and before_dt.tzinfo is not None:
                    from datetime import timezone as tz
                    thread_dt = thread_dt.replace(tzinfo=tz.utc)
                if thread_dt < before_dt:
                    filtered.append(t)
            threads = filtered

        # Sort
        if sort in ["created", "relevance"]:
            threads.sort(key=lambda t: t.created_at, reverse=True)
        elif sort == "created_asc":
            threads.sort(key=lambda t: t.created_at)

        # Pagination
        threads_total = len(threads)
        threads = threads[offset:offset + limit]

        results["threads"] = {
            "total": threads_total,
            "items": threads
        }
        total += threads_total

    # Search agents
    if "agents" in resource_list:
        agents = store.get_agents()

        # Text search
        if q:
            q_lower = q.lower()
            agents = [
                a for a in agents
                if (q_lower in a.agent_id.lower() or
                    q_lower in (a.name or "").lower() or
                    any(q_lower in cap.lower() for cap in (a.capabilities or [])))
            ]

        # Filters
        if agent_type:
            agents = [a for a in agents if a.agent_type == agent_type]

        if capabilities:
            agents = [
                a for a in agents
                if a.capabilities and all(cap in a.capabilities for cap in capabilities)
            ]

        if status:
            agents = [a for a in agents if a.status == status]

        if since_dt:
            agents = [a for a in agents if a.registered_at >= since_dt]

        if before_dt:
            agents = [a for a in agents if a.registered_at < before_dt]

        # Sort
        if sort in ["created", "relevance"]:
            agents.sort(key=lambda a: a.registered_at, reverse=True)
        elif sort == "created_asc":
            agents.sort(key=lambda a: a.registered_at)

        # Pagination
        agents_total = len(agents)
        agents = agents[offset:offset + limit]

        results["agents"] = {
            "total": agents_total,
            "items": agents
        }
        total += agents_total

    # Search sites
    if "sites" in resource_list:
        sites_list = store.get_sites()

        # Text search
        if q:
            q_lower = q.lower()
            sites_list = [
                s for s in sites_list
                if q_lower in s.site_id.lower() or q_lower in (s.purpose or "").lower()
            ]

        # Filters
        if status:
            sites_list = [s for s in sites_list if s.status == status]

        if since_dt:
            sites_list = [s for s in sites_list if s.created_at >= since_dt]

        if before_dt:
            sites_list = [s for s in sites_list if s.created_at < before_dt]

        # Sort
        if sort in ["created", "relevance"]:
            sites_list.sort(key=lambda s: s.created_at, reverse=True)
        elif sort == "created_asc":
            sites_list.sort(key=lambda s: s.created_at)

        # Pagination
        sites_total = len(sites_list)
        sites_list = sites_list[offset:offset + limit]

        results["sites"] = {
            "total": sites_total,
            "items": sites_list
        }
        total += sites_total

    execution_time_ms = int((time.time() - start_time) * 1000)

    return {
        "query": q,
        "resources": resource_list,
        "filters": {
            k: v for k, v in {
                "status": status,
                "since": since,
                "before": before,
                "type": type.value if type and hasattr(type, 'value') else type,
                "site": site,
                "sites": sites,
                "assigned_to": assigned_to,
                "archived": archived,
                "thread_type": thread_type,
                "weaver": weaver,
                "from_id": from_id,
                "to_id": to_id,
                "agent_type": agent_type,
                "capabilities": capabilities,
                "sort": sort,
                "limit": limit,
                "offset": offset
            }.items() if v is not None
        },
        "total": total,
        "results": results,
        "execution_time_ms": execution_time_ms
    }


# Screenshot Endpoints

@router.post("/screenshots")
async def upload_screenshot(
    screenshot_create: ScreenshotCreate,
    screenshots_dir: Path = Depends(get_project_screenshots_dir),
    log_db: LogDatabase = Depends(get_project_log_db)
):
    """Upload a screenshot from web app."""
    # Generate screenshot ID
    timestamp = datetime.now()
    screenshot_id = f"screenshot-{timestamp.strftime('%Y%m%d-%H%M%S-%f')}"

    # Create strand-specific directory
    strand_dir = screenshots_dir / screenshot_create.strand_id
    strand_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename
    turn_suffix = f"_turn-{screenshot_create.turn_number}" if screenshot_create.turn_number else ""
    filename = f"{timestamp.strftime('%Y-%m-%dT%H-%M-%S')}{turn_suffix}_{screenshot_create.label}.png"
    file_path = strand_dir / filename

    try:
        # Decode base64 and save
        screenshot_data = screenshot_create.screenshot_data
        if screenshot_data.startswith('data:image/png;base64,'):
            screenshot_data = screenshot_data.split(',')[1]

        image_bytes = base64.b64decode(screenshot_data)
        file_path.write_bytes(image_bytes)
        file_size = len(image_bytes)

        # Store metadata in database
        log_db.add_screenshot(
            screenshot_id=screenshot_id,
            strand_id=screenshot_create.strand_id,
            turn_number=screenshot_create.turn_number,
            label=screenshot_create.label,
            file_path=str(file_path),
            file_size=file_size,
            metadata={}
        )

        logger.info(f"Screenshot saved: {screenshot_id} ({file_size} bytes)")

        return {
            "success": True,
            "screenshot_id": screenshot_id,
            "file_size": file_size
        }

    except Exception as e:
        logger.error(f"Failed to save screenshot: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to save screenshot: {str(e)}")


@router.get("/screenshots", response_model=List[Screenshot])
async def list_screenshots(
    strand_id: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = Query(50, le=200),
    log_db: LogDatabase = Depends(get_project_log_db)
):
    """List screenshots with optional filters."""
    screenshots_data = log_db.get_screenshots(strand_id, since, limit)

    return [
        Screenshot(
            screenshot_id=row["screenshot_id"],
            strand_id=row["strand_id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            turn_number=row["turn_number"],
            label=row["label"],
            file_path=row["file_path"],
            file_size=row["file_size"],
            metadata={}
        )
        for row in screenshots_data
    ]


@router.get("/screenshots/{screenshot_id}")
async def get_screenshot_image(
    screenshot_id: str,
    log_db: LogDatabase = Depends(get_project_log_db)
):
    """Get screenshot image file."""
    screenshot_data = log_db.get_screenshot(screenshot_id)

    if not screenshot_data:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    file_path = Path(screenshot_data["file_path"])

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Screenshot file not found")

    return FileResponse(
        file_path,
        media_type="image/png",
        filename=f"{screenshot_id}.png"
    )


@router.get("/screenshots/{screenshot_id}/metadata", response_model=Screenshot)
async def get_screenshot_metadata(
    screenshot_id: str,
    log_db: LogDatabase = Depends(get_project_log_db)
):
    """Get screenshot metadata without downloading image."""
    screenshot_data = log_db.get_screenshot(screenshot_id)

    if not screenshot_data:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    return Screenshot(
        screenshot_id=screenshot_data["screenshot_id"],
        strand_id=screenshot_data["strand_id"],
        timestamp=datetime.fromisoformat(screenshot_data["timestamp"]),
        turn_number=screenshot_data["turn_number"],
        label=screenshot_data["label"],
        file_path=screenshot_data["file_path"],
        file_size=screenshot_data["file_size"],
        metadata={}
    )


# Agent Naming

@router.post("/naming/generate")
async def generate_name(
    role: Optional[str] = None,
    brief_content: Optional[str] = None,
    project: Optional[str] = None,
    x_project_id: Optional[str] = Header(None)
):
    """
    Generate a memorable agent name.

    Uses the naming system (simple.py generator with Haiku classification
    and muster name pools) to generate names like "chrome-badger-1202".

    Args:
        role: Agent role/mantle (used for genre classification)
        brief_content: Task description (used for genre classification)
        project: Project context

    Returns:
        {"name": "generated-name-1202"}
    """
    # Use project from header if not explicitly provided
    project_id = project or x_project_id

    name = generate_agent_name(
        project=project_id,
        role=role,
        brief_content=brief_content
    )

    return {"name": name}


# Yield/Sack Endpoints (chain data passing)

class YieldRequest(BaseModel):
    """Request to store a yield in a chain's sack."""
    chain_id: str
    task_id: str
    yield_data: YieldCreate
    # Optional enrichment (usually added by Mill, not agent)
    duration_seconds: Optional[int] = None
    tokens_used: Optional[int] = None
    shard_path: Optional[str] = None
    tender_id: Optional[str] = None


@router.post("/yields")
async def store_yield(
    yield_request: YieldRequest,
    x_agent_id: str = Header(None, alias="X-Agent-Id"),
    log_db: LogDatabase = Depends(get_project_log_db)
):
    """
    Store a yield in the chain's sack.

    Called when an agent completes and yields their output.
    The yield becomes part of the chain's sack, accessible to downstream tasks.
    """
    sack_id = generate_yield_id()

    success = log_db.add_yield(
        sack_id=sack_id,
        chain_id=yield_request.chain_id,
        task_id=yield_request.task_id,
        agent_id=x_agent_id,
        status=yield_request.yield_data.status,
        outcome=yield_request.yield_data.outcome,
        artifacts=yield_request.yield_data.artifacts,
        notes=yield_request.yield_data.notes,
        duration_seconds=yield_request.duration_seconds,
        tokens_used=yield_request.tokens_used,
        shard_path=yield_request.shard_path,
        tender_id=yield_request.tender_id
    )

    if not success:
        raise HTTPException(status_code=500, detail="Failed to store yield")

    logger.info(f"Stored yield {sack_id} for chain {yield_request.chain_id}")

    return {
        "success": True,
        "sack_id": sack_id,
        "chain_id": yield_request.chain_id
    }


@router.get("/yields/chain/{chain_id}", response_model=List[Yield])
async def get_chain_yields(
    chain_id: str,
    log_db: LogDatabase = Depends(get_project_log_db)
):
    """
    Get all yields in a chain, ordered by execution.

    Used by The Hand to inspect what happened in a chain.
    """
    yields_data = log_db.get_chain_yields(chain_id)

    return [
        Yield(
            sack_id=y["sack_id"],
            chain_id=y["chain_id"],
            task_id=y["task_id"],
            agent_id=y.get("agent_id"),
            timestamp=datetime.fromisoformat(y["timestamp"]),
            status=y["status"],
            outcome=y.get("outcome") or "",
            artifacts=y.get("artifacts") or [],
            notes=y.get("notes"),
            duration_seconds=y.get("duration_seconds"),
            tokens_used=y.get("tokens_used"),
            shard_path=y.get("shard_path"),
            tender_id=y.get("tender_id"),
            metadata=y.get("metadata") or {}
        )
        for y in yields_data
    ]


@router.get("/yields/{sack_id}", response_model=Yield)
async def get_yield(
    sack_id: str,
    log_db: LogDatabase = Depends(get_project_log_db)
):
    """Get specific yield by ID."""
    yield_data = log_db.get_yield(sack_id)

    if not yield_data:
        raise HTTPException(status_code=404, detail="Yield not found")

    return Yield(
        sack_id=yield_data["sack_id"],
        chain_id=yield_data["chain_id"],
        task_id=yield_data["task_id"],
        agent_id=yield_data.get("agent_id"),
        timestamp=datetime.fromisoformat(yield_data["timestamp"]),
        status=yield_data["status"],
        outcome=yield_data.get("outcome") or "",
        artifacts=yield_data.get("artifacts") or [],
        notes=yield_data.get("notes"),
        duration_seconds=yield_data.get("duration_seconds"),
        tokens_used=yield_data.get("tokens_used"),
        shard_path=yield_data.get("shard_path"),
        tender_id=yield_data.get("tender_id"),
        metadata=yield_data.get("metadata") or {}
    )


@router.get("/yields/status/{status}", response_model=List[Yield])
async def get_yields_by_status(
    status: str,
    log_db: LogDatabase = Depends(get_project_log_db)
):
    """
    Get yields by status.

    Useful for finding blocked work across all chains.
    """
    yields_data = log_db.get_yields_by_status(status)

    return [
        Yield(
            sack_id=y["sack_id"],
            chain_id=y["chain_id"],
            task_id=y["task_id"],
            agent_id=y.get("agent_id"),
            timestamp=datetime.fromisoformat(y["timestamp"]),
            status=y["status"],
            outcome=y.get("outcome") or "",
            artifacts=y.get("artifacts") or [],
            notes=y.get("notes"),
            duration_seconds=y.get("duration_seconds"),
            tokens_used=y.get("tokens_used"),
            shard_path=y.get("shard_path"),
            tender_id=y.get("tender_id"),
            metadata=y.get("metadata") or {}
        )
        for y in yields_data
    ]


@router.get("/yields/agent/{agent_id}", response_model=List[Yield])
async def get_agent_yields(
    agent_id: str,
    log_db: LogDatabase = Depends(get_project_log_db)
):
    """Get all yields by a specific agent."""
    yields_data = log_db.get_agent_yields(agent_id)

    return [
        Yield(
            sack_id=y["sack_id"],
            chain_id=y["chain_id"],
            task_id=y["task_id"],
            agent_id=y.get("agent_id"),
            timestamp=datetime.fromisoformat(y["timestamp"]),
            status=y["status"],
            outcome=y.get("outcome") or "",
            artifacts=y.get("artifacts") or [],
            notes=y.get("notes"),
            duration_seconds=y.get("duration_seconds"),
            tokens_used=y.get("tokens_used"),
            shard_path=y.get("shard_path"),
            tender_id=y.get("tender_id"),
            metadata=y.get("metadata") or {}
        )
        for y in yields_data
    ]
