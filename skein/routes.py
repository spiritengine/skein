"""
SKEIN FastAPI routes.
"""

import logging
import base64
from datetime import datetime
from typing import List, Optional
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query, Header, Depends
from fastapi.responses import FileResponse

from .models import (
    AgentRegistration, AgentInfo,
    SiteCreate, Site,
    FolioCreate, Folio,
    ThreadCreate, Thread,
    LogBatch, LogLine,
    FolioType,
    ScreenshotCreate, Screenshot
)
from .storage import JSONStore, LogDatabase, get_data_dir_for_project
from .utils import (
    generate_folio_id, generate_thread_id, parse_mentions,
    get_current_status, get_current_assignment,
    auto_invalidate_cache,
    parse_relative_time
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
        status="active",
        metadata=registration.metadata
    )

    success = store.save_agent(agent)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to register agent")

    return {"success": True, "registration": agent}


@router.get("/roster", response_model=List[AgentInfo])
async def get_roster(store: JSONStore = Depends(get_project_store)):
    """Get all registered agents."""
    return store.get_agents()


@router.get("/roster/{agent_id}", response_model=AgentInfo)
async def get_agent(agent_id: str, store: JSONStore = Depends(get_project_store)):
    """Get specific agent."""
    agent = store.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.patch("/roster/{agent_id}")
async def update_agent(
    agent_id: str,
    status: Optional[str] = None,
    name: Optional[str] = None,
    agent_type: Optional[str] = None,
    description: Optional[str] = None,
    capabilities: Optional[List[str]] = None,
    store: JSONStore = Depends(get_project_store)
):
    """Update agent registration."""
    agent = store.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if status:
        agent.status = status
    if name:
        agent.name = name
    if agent_type:
        agent.agent_type = agent_type
    if description:
        agent.description = description
    if capabilities:
        agent.capabilities = capabilities

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


@router.post("/sites/{site_id}/folios")
async def post_to_site(
    site_id: str,
    folio_create: FolioCreate,
    x_agent_id: str = Header(None, alias="X-Agent-Id"),
    store: JSONStore = Depends(get_project_store)
):
    """Post a folio to a site."""
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
    status: Optional[str] = None,
    assigned_to: Optional[str] = None,
    archived: Optional[bool] = None,
    x_agent_id: str = Header(None, alias="X-Agent-Id")
):
    """Update folio metadata."""
    folio = json_store.get_folio(folio_id)
    if not folio:
        raise HTTPException(status_code=404, detail="Folio not found")

    created_by = x_agent_id or "unknown"

    # PURE THREADS: Create status thread instead of updating field
    if status:
        status_thread = Thread(
            thread_id=generate_thread_id(),
            from_id=folio_id,
            to_id=folio_id,
            type="status",
            content=status,
            weaver=created_by,
            created_at=datetime.now()
        )
        json_store.save_thread(status_thread)
        auto_invalidate_cache("status", folio_id)
        # Also update field for backward compat (will be removed after migration)
        folio.status = status

    # PURE THREADS: Create assignment thread instead of updating field
    if assigned_to:
        assignment_thread = Thread(
            thread_id=generate_thread_id(),
            from_id=folio_id,
            to_id=assigned_to,
            type="assignment",
            content=f"Assigned to {assigned_to}",
            weaver=created_by,
            created_at=datetime.now()
        )
        json_store.save_thread(assignment_thread)
        auto_invalidate_cache("assignment", folio_id)
        # Also update field for backward compat (will be removed after migration)
        folio.assigned_to = assigned_to

    if archived is not None:
        folio.archived = archived

    json_store.save_folio(folio)
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
