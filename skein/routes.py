"""
SKEIN FastAPI routes.
"""

import logging
import base64
import re
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any, Union
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import APIRouter, HTTPException, Query, Header, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .models import (
    AgentRegistration,
    AgentInfo,
    SiteCreate,
    Site,
    SiteUpdate,
    FolioCreate,
    Folio,
    VersionView,
    FolioUpdate,
    ThreadCreate,
    Thread,
    LogBatch,
    LogLine,
    FolioType,
    ScreenshotCreate,
    Screenshot,
    YieldCreate,
    Yield,
)
from .storage import (
    PROJECT_DATA_DIR_HEADER,
    JSONStore,
    LogDatabase,
    ProjectDataDirClaimError,
    ProjectDataDirMismatch,
    ProjectNotFound,
    ProjectRegistryError,
    get_data_dir_for_project,
    ensure_aware,
    search_folio_across_projects,
    resolve_folio_across_projects,
    get_project_store as get_named_project_store,
    get_project_last_activity_timestamps,
)
from .address_legacy import parse as parse_address
from . import address as rev3_address
from . import publish as _pub
from .utils import (
    generate_folio_id,
    generate_thread_id,
    generate_yield_id,
    parse_mentions,
    get_current_status,
    get_current_assignment,
    enrich_folios_with_status,
    parse_relative_time,
    generate_agent_name,
    format_relative_time,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# Multi-project support
def _get_request_project_data_dir(
    project_id: str, claimed_data_dir: Optional[str]
) -> Path:
    """Resolve a project only after validating an implicit cwd-origin claim."""
    try:
        return get_data_dir_for_project(project_id, claimed_data_dir)
    except ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ProjectDataDirMismatch as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ProjectDataDirClaimError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ProjectRegistryError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Project registry is unsafe: {e}",
        ) from e


def _get_safe_named_project_store(project_id: str) -> Optional[JSONStore]:
    """Open a named project through the same strict registry validation."""
    try:
        return get_named_project_store(project_id)
    except ProjectRegistryError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Project registry is unsafe: {e}",
        ) from e


def get_project_store(
    x_project_id: Optional[str] = Header(None),
    x_skein_project_data_dir: Optional[str] = Header(
        None, alias=PROJECT_DATA_DIR_HEADER
    ),
) -> JSONStore:
    """
    Get JSONStore for the requested project.

    Uses X-Project-Id header to determine which project's data to use.
    Raises error if no project specified - forces proper `skein init` setup.
    """
    if not x_project_id:
        raise HTTPException(
            status_code=400,
            detail="No project specified. Run 'skein init --project PROJECT_NAME' in your project directory first.",
        )

    data_dir = _get_request_project_data_dir(
        x_project_id, x_skein_project_data_dir
    )
    logger.info(f"Using project '{x_project_id}' data dir: {data_dir}")
    return JSONStore(data_dir)


def get_project_log_db(
    x_project_id: Optional[str] = Header(None),
    x_skein_project_data_dir: Optional[str] = Header(
        None, alias=PROJECT_DATA_DIR_HEADER
    ),
) -> LogDatabase:
    """
    Get LogDatabase for the requested project.

    Uses X-Project-Id header to determine which project's data to use.
    Each project gets its own SQLite database at .skein/data/skein.db
    """
    if not x_project_id:
        raise HTTPException(
            status_code=400,
            detail="No project specified. Run 'skein init --project PROJECT_NAME' in your project directory first.",
        )

    data_dir = _get_request_project_data_dir(
        x_project_id, x_skein_project_data_dir
    )
    db_path = data_dir / "skein.db"
    logger.info(f"Using project '{x_project_id}' log db: {db_path}")
    return LogDatabase(db_path)


def get_project_screenshots_dir(
    x_project_id: Optional[str] = Header(None),
    x_skein_project_data_dir: Optional[str] = Header(
        None, alias=PROJECT_DATA_DIR_HEADER
    ),
) -> Path:
    """
    Get screenshots directory for the requested project.

    Uses X-Project-Id header to determine which project's data to use.
    Each project gets its own screenshots at .skein/data/screenshots/
    """
    if not x_project_id:
        raise HTTPException(
            status_code=400,
            detail="No project specified. Run 'skein init --project PROJECT_NAME' in your project directory first.",
        )

    data_dir = _get_request_project_data_dir(
        x_project_id, x_skein_project_data_dir
    )
    screenshots_dir = data_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Using project '{x_project_id}' screenshots dir: {screenshots_dir}")
    return screenshots_dir


# Address resolution


def _verify_fragment(expected_framed: Optional[str], resolved_content_hash: str) -> None:
    """Enforce a `#sha256::<full>` verifier fragment: the address must resolve to
    EXACTLY the fragment's digest, else reject (ADDRESSING_GRAMMAR.md — the fragment
    is a verifier, not decoration). `expected_framed` is the framed
    `sha256::<hex>` the fragment asserts (None when there is no fragment);
    `resolved_content_hash` is the framed digest the address actually resolved to.
    Works for ANY address form (rev3 OR a legacy bare/project address)."""
    if expected_framed is None:
        return
    if not resolved_content_hash:
        # The target has no content hash to check against (e.g. a legacy folios row
        # on a not-yet-backfilled foreign project). Refuse — but say WHY, so it
        # isn't mistaken for a digest mismatch.
        raise HTTPException(
            status_code=409,
            detail=(f"cannot verify fragment {expected_framed}: target has no "
                    f"content hash"),
        )
    if resolved_content_hash != expected_framed:
        raise HTTPException(
            status_code=409,
            detail=(f"verifier fragment mismatch: address resolved to "
                    f"{resolved_content_hash}, fragment asserts {expected_framed}"),
        )


def _resolve_rev3_read(address: str, current_store: JSONStore) -> Union[Folio, VersionView]:
    """Resolve a rev3 `::` address against the current store's versions/refs.

    - ref::<slug> -> the lineage HEAD folio (a Folio, with control). NOTE: a BARE
      slug (no `::`) is NOT handled here — it stays on the legacy parser so it keeps
      its cross-project cascade; `ref::<slug>` is the explicit local-only form.
    - sha256::<hex> / hash::sha256::<hex> / a station-scoped short hash -> the exact
      version, by-hash (a VersionView: immutable content + is_head/lineage_head, no
      mutable control). Resolution is current-store-scoped; cross-project hash
      cascade and foreign-station scoping (an alias/web station naming a DIFFERENT
      store) are future work — today a named station resolves against the current
      store. A `#sha256::<full>` verifier fragment is enforced on every form.
    """
    try:
        parsed = rev3_address.parse(address)
    except rev3_address.AddressError as e:
        raise HTTPException(status_code=400, detail=f"invalid address: {e}")

    frag_framed = (f"{parsed.fragment.algo}::{parsed.fragment.digest}"
                   if parsed.fragment is not None else None)

    if isinstance(parsed.folio, rev3_address.Ref):
        # The head IS the resolution; get_folio gives both the refuse-on-unknown
        # (None -> 404) and the head Folio with control, so no separate resolve().
        folio = current_store.get_folio(parsed.folio.slug)
        if not folio:
            raise HTTPException(status_code=404, detail=f"ref not found: {parsed.folio.slug}")
        _verify_fragment(frag_framed, folio.content_hash)
        return folio

    station = current_store.station_index()
    try:
        resolved_hex = rev3_address.resolve(parsed, station)
    except rev3_address.AddressError as e:
        # ShortHashNotFound / AmbiguousShortHash both subclass this.
        raise HTTPException(status_code=404, detail=str(e))

    content_hash = f"{parsed.folio.algo}::{resolved_hex}"
    _verify_fragment(frag_framed, content_hash)
    folio = current_store.get_version_by_hash(content_hash)
    if not folio:
        raise HTTPException(status_code=404, detail=f"hash not found: {content_hash}")
    return folio


def _overlay_thread_control(folio: Folio, store: JSONStore) -> None:
    """Overlay thread-derived status/assignment on one folio, against the store
    it was READ from. Post-contraction this is the only way a response carries
    control state — the refs cache columns are gone, so an un-overlaid Folio
    holds the model defaults ('open'/None). For a cross-project read
    (folio.source_project set) the threads live in the SOURCE project's store;
    querying the local store would find nothing and silently default. An
    unresolvable source project leaves the defaults in place (never raises on a
    read path)."""
    src = store
    if folio.source_project:
        named = _get_safe_named_project_store(folio.source_project)
        if named is None:
            return
        src = named
    # None-checks, not truthiness: a PRESENT thread value wins even when falsy
    # (the _normalize_control invariant — a present empty string stays empty;
    # the 'open' default applies only on None-absence). An `or`-chain here
    # collapsed a persisted status of '' back to 'open' (deep_code_audit r2
    # finding 2).
    status = get_current_status(folio.folio_id, src)
    if status is not None:
        folio.status = status
    elif folio.status is None:
        folio.status = "open"
    assignment = get_current_assignment(folio.folio_id, src)
    if assignment is not None:
        folio.assigned_to = assignment


def resolve_folio_read(
    address: str,
    current_project_id: Optional[str],
    current_store: JSONStore,
) -> Union[Folio, VersionView]:
    """
    Resolve a folio address for reading.

    Handles three cases:
    1. Explicit project: "speakbot:brief-xxx" → look in speakbot's store
    2. Bare, found locally: "brief-xxx" → return from current store
    3. Bare, not found locally: cascade across all projects

    Sets source_project on the Folio when it came from another project.
    """
    # Dispatch on the address BODY (the part before any `#fragment`), NOT the raw
    # string: a `#sha256::<hex>` verifier fragment always contains `::`, so keying
    # on the raw string would pull a fragment-bearing bare/project address into the
    # rev3 path and drop its cascade. The body decides the scheme; the fragment is
    # a cross-cutting verifier handled per-path.
    #
    # rev3 `::` forms (sha256::/hash::/ref::<slug>/alias::/web::) resolve through the
    # content-hash grammar + the live versions/refs station. The single-colon
    # `project:id` scheme and BARE slugs stay on the legacy parser below: a bare
    # slug keeps its local-then-cross-project cascade (the established behaviour),
    # which a local-only rev3 ref lookup would regress; `ref::<slug>` is the
    # explicit local-only head form. (§7 routes "slug" through address.parse; the
    # cascade-preserving split is a deliberate implementer call — the resolved head
    # is identical either way for a local folio, and bare-slug cross-project
    # resolution must not be dropped.)
    body, sep, frag = address.partition("#")
    if "::" in body:
        return _resolve_rev3_read(address, current_store)

    # Legacy path. A verifier fragment may ride a legacy address too; parse+validate
    # it as a bare full sha256 hash and enforce it against whatever the body resolves
    # to (preserving the cascade the rev3 path would not provide).
    frag_framed: Optional[str] = None
    if sep:
        try:
            frag_hash = rev3_address.parse_fragment(frag)  # validates sha256::<64hex>
        except rev3_address.AddressError as e:
            raise HTTPException(status_code=400, detail=f"invalid verifier fragment: {e}")
        frag_framed = f"{frag_hash.algo}::{frag_hash.digest}"

    parsed = parse_address(body)

    if parsed.is_qualified:
        # Explicit project prefix — look in that project's store
        target_store = _get_safe_named_project_store(parsed.project)
        if not target_store:
            raise HTTPException(
                status_code=404,
                detail=f"Project '{parsed.project}' not found in registry",
            )
        folio = target_store.get_folio(parsed.folio_id)
        if not folio:
            raise HTTPException(
                status_code=404,
                detail=f"Folio '{parsed.folio_id}' not found in project '{parsed.project}'",
            )
        _verify_fragment(frag_framed, folio.content_hash)
        # Only tag source if it's different from the current project
        if parsed.project != current_project_id:
            folio.source_project = parsed.project
        return folio

    # Bare address — try current project first
    folio = current_store.get_folio(parsed.folio_id)
    if folio:
        _verify_fragment(frag_framed, folio.content_hash)
        return folio

    # Cascade: search all other projects
    result = resolve_folio_across_projects(parsed.folio_id, current_project_id)
    if result:
        folio = result["folio"]
        _verify_fragment(frag_framed, folio.content_hash)
        folio.source_project = result["project_name"]
        return folio

    raise HTTPException(status_code=404, detail="Folio not found")


# Project Endpoints (cross-project; no X-Project-Id header)


@router.get("/projects/timestamps")
async def get_projects_timestamps(
    since: Optional[int] = Query(
        None,
        description="Unix timestamp cutoff. Only projects with last activity strictly greater than this value are returned.",
    ),
) -> Dict[str, int]:
    """Return mapping of project_id -> latest folio created_at as unix seconds (int).

    Aggregates across every registered project in ~/.skein/projects.json. Projects
    with no folios, missing databases, or unreadable databases are omitted. With
    ?since=<unix>, only projects whose latest activity is strictly greater than the
    cutoff (>) are included.

    This endpoint is cross-project by design and does NOT take an X-Project-Id header.
    """
    timestamps = get_project_last_activity_timestamps()
    if since is None:
        return timestamps
    return {pid: ts for pid, ts in timestamps.items() if ts > since}


# Roster Endpoints


@router.post("/roster/register")
async def register_agent(
    registration: AgentRegistration, store: JSONStore = Depends(get_project_store)
):
    """Register an agent in the roster."""
    agent = AgentInfo(
        agent_id=registration.agent_id,
        name=registration.name,
        agent_type=registration.agent_type,
        description=registration.description,
        registered_at=datetime.now(timezone.utc),
        capabilities=registration.capabilities,
        status=registration.status or "active",
        metadata=registration.metadata,
    )

    success = store.save_agent(agent)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to register agent")

    return {"success": True, "registration": agent}


@router.get("/roster", response_model=List[AgentInfo])
async def get_roster(
    status: Optional[str] = Query(
        None, description="Filter by status: active, retired"
    ),
    store: JSONStore = Depends(get_project_store),
):
    """Get registered agents, optionally filtered by status."""
    return store.get_agents(status=status)


class AgentActivity(BaseModel):
    """Agent info enriched with activity data for QM dashboards."""

    agent_id: str
    name: Optional[str] = None
    agent_type: Optional[str] = None
    status: str = "active"
    registered_at: datetime
    # Activity fields
    last_activity: Optional[datetime] = None
    last_activity_relative: Optional[str] = None
    activity_status: str = "idle"  # active, idle, torching
    working_site: Optional[str] = None
    folio_count: int = 0
    last_folio_type: Optional[str] = None


@router.get("/roster/enriched", response_model=List[AgentActivity])
async def get_roster_enriched(
    status: Optional[str] = Query(
        None, description="Filter by status: active, retired"
    ),
    store: JSONStore = Depends(get_project_store),
):
    """
    Get registered agents with activity data for QM work.

    Returns enriched agent info including:
    - Human-readable relative time since registration/activity
    - Activity status (active/idle/torching) based on recent folio activity
    - Working site they're posting to
    - Folio count and last folio type
    """
    agents = store.get_agents(status=status)
    all_folios = store.get_folios()

    enriched = []
    now = datetime.now(timezone.utc)

    for agent in agents:
        # Get all folios created by this agent
        agent_folios = [f for f in all_folios if f.created_by == agent.agent_id]

        # Sort by created_at descending to get most recent
        agent_folios.sort(key=lambda f: f.created_at, reverse=True)

        # Determine activity info
        last_activity = None
        last_activity_relative = None
        working_site = None
        last_folio_type = None
        folio_count = len(agent_folios)

        if agent_folios:
            most_recent = agent_folios[0]
            last_activity = most_recent.created_at
            last_activity_relative = format_relative_time(most_recent.created_at)
            working_site = most_recent.site_id
            last_folio_type = most_recent.type

        # Determine activity status
        # - torching: agent status is "torching" or "retired"
        # - active: has posted within last 30 minutes
        # - idle: otherwise
        activity_status = "idle"

        if agent.status in ("torching", "retired"):
            activity_status = "torching"
        elif last_activity:
            # Make last_activity timezone-aware if needed
            if last_activity.tzinfo is None:
                last_activity_aware = last_activity.replace(tzinfo=timezone.utc)
            else:
                last_activity_aware = last_activity

            if now - last_activity_aware < timedelta(minutes=30):
                activity_status = "active"

        # Use registration time if no activity
        if not last_activity_relative:
            last_activity_relative = format_relative_time(agent.registered_at)

        enriched.append(
            AgentActivity(
                agent_id=agent.agent_id,
                name=agent.name,
                agent_type=agent.agent_type,
                status=agent.status,
                registered_at=agent.registered_at,
                last_activity=last_activity,
                last_activity_relative=last_activity_relative,
                activity_status=activity_status,
                working_site=working_site,
                folio_count=folio_count,
                last_folio_type=last_folio_type,
            )
        )

    # Sort by most recent activity first (active agents at top)
    enriched.sort(key=lambda a: a.last_activity or a.registered_at, reverse=True)

    return enriched


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
    agent_id: str, update: AgentUpdate, store: JSONStore = Depends(get_project_store)
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
    store: JSONStore = Depends(get_project_store),
):
    """Create a new site."""
    created_by = x_agent_id or "unknown"

    site = Site(
        site_id=site_create.site_id,
        created_at=datetime.now(timezone.utc),
        created_by=created_by,
        purpose=site_create.purpose,
        status="active",
        metadata=site_create.metadata,
    )

    success = store.save_site(site)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to create site")

    return {"success": True, "site": site}


@router.get("/sites", response_model=List[Site])
async def get_sites(
    status: Optional[str] = None,
    tag: Optional[str] = None,
    store: JSONStore = Depends(get_project_store),
):
    """Get all sites with optional filters."""
    sites = store.get_sites()

    if status:
        sites = [s for s in sites if s.status == status]

    if tag:
        sites = [s for s in sites if tag in s.metadata.get("tags", [])]

    return sites


@router.get("/sites/timestamps")
async def get_sites_timestamps(
    since: Optional[int] = Query(
        None,
        description="Unix timestamp cutoff. Only sites with last activity strictly greater than this value are returned.",
    ),
    store: JSONStore = Depends(get_project_store),
) -> Dict[str, int]:
    """Return mapping of site_id -> last folio created_at as unix seconds (int).

    Sites with zero folios are omitted. With ?since=<unix>, only sites whose
    latest activity is strictly greater than the cutoff (>) are included.
    """
    activity = store.get_site_last_activity()
    result: Dict[str, int] = {}
    for site_id, dt in activity.items():
        ts = int(dt.timestamp())
        if since is not None and ts <= since:
            continue
        result[site_id] = ts
    return result


def _raise_site_not_found(site_id: str, store: JSONStore) -> None:
    """Raise a 404 whose detail lists the active sites (or how to create one)."""
    all_sites = store.get_sites()
    active_sites = [s for s in all_sites if s.status == "active"]
    if active_sites:
        site_ids = [s.site_id for s in active_sites[:50]]
        suffix = f" (+{len(active_sites) - 50} more)" if len(active_sites) > 50 else ""
        raise HTTPException(
            status_code=404,
            detail=f"Site '{site_id}' not found. Active sites: {', '.join(site_ids)}{suffix}. Run 'skein sites' for full list.",
        )
    raise HTTPException(
        status_code=404,
        detail=f"Site '{site_id}' not found. No active sites exist - create one with 'skein site create <id> \"description\"'",
    )


@router.get("/sites/{site_id}", response_model=Site)
async def get_site(site_id: str, store: JSONStore = Depends(get_project_store)):
    """Get specific site."""
    site = store.get_site(site_id)
    if not site:
        _raise_site_not_found(site_id, store)
    return site


@router.patch("/sites/{site_id}", response_model=Site)
async def update_site(
    site_id: str, site_update: SiteUpdate, store: JSONStore = Depends(get_project_store)
):
    """Update site status and/or metadata."""
    updated_site = store.update_site(
        site_id, status=site_update.status, metadata=site_update.metadata
    )
    if not updated_site:
        raise HTTPException(status_code=404, detail=f"Site '{site_id}' not found")
    return updated_site


@router.get("/sites/{site_id}/folios", response_model=List[Folio])
async def get_site_folios(
    site_id: str,
    type: Optional[FolioType] = None,
    since: Optional[str] = None,
    store: JSONStore = Depends(get_project_store),
):
    """Get folios for a specific site."""
    folios = store.get_folios(site_id=site_id)
    enrich_folios_with_status(folios, store)

    if type:
        folios = [f for f in folios if f.type == type]

    if since:
        since_dt = ensure_aware(datetime.fromisoformat(since))
        folios = [f for f in folios if f.created_at >= since_dt]

    return folios


# Title validation
GENERIC_TITLES = {
    "handoff",
    "handoff brief",
    "brief",
    "untitled",
    "test",
    "title",
    "issue",
    "friction",
    "finding",
    "notion",
    "moment",
    "summary",
    "tender",
    "writ",
    "new folio",
    "folio",
    "update",
    "fix",
    "change",
    "todo",
    "task",
}

TITLE_EXAMPLES = {
    "brief": 'e.g., "Implement OAuth for API endpoints" or "Fix race condition in websocket handler"',
    "issue": 'e.g., "Agents crash when site_id contains spaces" or "Memory leak in long-running sessions"',
    "friction": 'e.g., "Must restart server after config changes" or "Error messages don\'t show line numbers"',
    "finding": 'e.g., "Redis caching reduces latency by 40%" or "Users prefer dark mode 3:1"',
    "tender": 'e.g., "Auth refactor ready for review" or "New dashboard component complete"',
    "notion": 'e.g., "Could use websockets for real-time updates" or "Consider caching user preferences"',
    "moment": 'e.g., "Released the first public build" or "The migration is complete"',
    "summary": 'e.g., "Completed OAuth integration" or "Session retrospective: agent coordination"',
}

_DEFAULT_TITLE_EXAMPLE = 'e.g., "Clear description of what this folio is about"'

HYPOTHESIS_VERDICTS = {
    "confirmed",
    "disconfirmed",
    "inconclusive",
    "deferred",
    "blocked",
}

HYPOTHESIS_PRIORITIES = {"high", "medium", "low"}

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# Patterns for shard/worktree IDs:
# - 65af2039-20251205-001 (8-char hex prefix)
# - bucket-1210-20251210-001 (name-based)
# - eaa09237-20251207-154442 (hex-date-timestamp)
SHARD_ID_PATTERN = re.compile(
    r"^[a-f0-9]{8}-\d{8}-\d{3,6}:\s*|"  # 8-char hex: 65af2039-20251205-001:
    r"^[a-z]+-\d{4}-\d{8}-\d{3}:\s*",  # name-based: bucket-1210-20251210-001:
    re.IGNORECASE,
)

# Folio type prefixes that are redundant (the type field already says this)
TYPE_PREFIX_PATTERN = re.compile(
    r"^(tender|brief|issue|finding|friction|notion|moment|summary|writ|playbook|mantle|plan):\s*",
    re.IGNORECASE,
)

# Status markers often copied from content (handles markdown bold or plain)
STATUS_MARKER_PATTERN = re.compile(r"(\*\*)?Status:(\*\*)?\s*\w+\.?\s*", re.IGNORECASE)


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
        example = TITLE_EXAMPLES.get(folio_type, _DEFAULT_TITLE_EXAMPLE)
        raise HTTPException(
            status_code=400,
            detail=f"{folio_type.capitalize()} needs a title that describes what it's about.\n\n{example}",
        )

    title = title.strip()

    # Strip markdown cruft
    title = re.sub(r"^#+\s*", "", title)  # Leading headers
    title = re.sub(r"^\*\*(.+?)\*\*", r"\1", title)  # Bold wrapper (keep content)
    title = re.sub(r"^__(.+?)__", r"\1", title)  # Underscore bold wrapper
    title = title.strip()

    # Strip status markers (must be before stripping bold, uses markdown)
    title = STATUS_MARKER_PATTERN.sub("", title)

    # Strip redundant type prefixes FIRST (they come before shard IDs)
    title = TYPE_PREFIX_PATTERN.sub("", title)

    # Strip shard/worktree IDs from start (after type prefix is removed)
    title = SHARD_ID_PATTERN.sub("", title)

    # Clean up any remaining type prefixes (in case of "## Tender: shard-id: ...")
    title = TYPE_PREFIX_PATTERN.sub("", title)

    title = title.strip()

    # Check for generic/lazy titles
    if title.lower() in GENERIC_TITLES:
        example = TITLE_EXAMPLES.get(folio_type, _DEFAULT_TITLE_EXAMPLE)
        raise HTTPException(
            status_code=400,
            detail=f'"{title}" is too generic - what\'s this {folio_type} actually about?\n\n{example}',
        )

    # Check minimum length (avoid "ok", "done", etc.)
    if len(title) < 10:
        example = TITLE_EXAMPLES.get(folio_type, _DEFAULT_TITLE_EXAMPLE)
        raise HTTPException(
            status_code=400,
            detail=f'"{title}" is too brief ({len(title)} chars) - give a bit more detail so others know what this covers.\n\n{example}',
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
    store: JSONStore = Depends(get_project_store),
):
    """Post a folio to a site."""
    # Validate and clean title
    folio_create.title = validate_folio_title(folio_create.title, folio_create.type)

    # Validate hypothesis-specific metadata
    if folio_create.type == "hypothesis":
        priority = folio_create.metadata.get("priority", "medium")
        if priority not in HYPOTHESIS_PRIORITIES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid priority '{priority}'. Must be one of: high, medium, low",
            )

    # Verify site exists
    site = store.get_site(site_id)
    if not site:
        _raise_site_not_found(site_id, store)

    created_by = x_agent_id or "unknown"
    folio_id = generate_folio_id(folio_create.type)

    # Status/assignment are thread-derived (the sugar threads below are the sole
    # persistence — no refs cache columns since the threads-only contraction).
    # The object still carries the initial state so the RESPONSE reflects it.
    # None-check, not truthiness (the '' invariant): only ABSENCE defaults to
    # 'open'; an explicit present value — including '' — is kept and minted.
    initial_status = folio_create.metadata.get("status")
    if initial_status is None:
        initial_status = "open"
    folio = Folio(
        folio_id=folio_id,
        type=folio_create.type,
        site_id=site_id,
        created_at=datetime.now(timezone.utc),
        created_by=created_by,
        title=folio_create.title,
        content=folio_create.content,
        status=initial_status,
        assigned_to=folio_create.assigned_to,
        target_agent=folio_create.target_agent,
        omlet=folio_create.omlet,
        metadata=folio_create.metadata,
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
            created_at=datetime.now(timezone.utc),
        )
        store.save_thread(thread)

    # SUGAR API: Create status thread if status provided (undocumented)
    # Don't create default status - let patterns emerge naturally
    # Only create if explicitly provided AND not "open" (which is just noise).
    # None-check, not truthiness (the '' invariant): an explicit present-but-
    # falsy status is minted faithfully — the thread is the SOLE persistence,
    # so a truthy gate here silently dropped it at creation (deep_code_audit
    # r4 finding 3, the create-path sibling of the pinned PATCH-path fix).
    if (
        initial_status is not None
        and initial_status != "open"
    ):
        status_thread = Thread(
            thread_id=generate_thread_id(),
            from_id=folio_id,
            to_id=folio_id,
            type="status",
            content=initial_status,
            weaver=created_by,
            created_at=datetime.now(timezone.utc),
        )
        store.save_thread(status_thread)

    # SUGAR API: Create assignment thread if assigned_to provided (undocumented)
    if folio_create.assigned_to is not None:
        assignment_thread = Thread(
            thread_id=generate_thread_id(),
            from_id=folio_id,
            to_id=folio_create.assigned_to,
            type="assignment",
            content=f"Assigned {folio_create.type}: {folio_create.title}",
            weaver=created_by,
            created_at=datetime.now(timezone.utc),
        )
        store.save_thread(assignment_thread)

    # Create thread for target_agent if set (for briefs)
    if folio_create.target_agent:
        thread = Thread(
            thread_id=generate_thread_id(),
            from_id=folio_id,
            to_id=folio_create.target_agent,
            type="message",
            content=f"Brief for you: {folio_create.title}",
            weaver=created_by,
            created_at=datetime.now(timezone.utc),
        )
        store.save_thread(thread)

    return {"success": True, "folio_id": folio_id}


# Folio Endpoints


@router.post("/folios")
async def create_folio(
    folio_create: FolioCreate,
    x_agent_id: str = Header(None, alias="X-Agent-Id"),
    store: JSONStore = Depends(get_project_store),
):
    """Create a folio (shortcut for POST /sites/{site_id}/folios)."""
    return await post_to_site(folio_create.site_id, folio_create, x_agent_id, store)


@router.get("/folios", response_model=List[Folio])
async def get_folios(
    type: Optional[FolioType] = None,
    site_id: Optional[str] = None,
    assigned_to: Optional[str] = None,
    status: Optional[str] = None,
    store: JSONStore = Depends(get_project_store),
):
    """Get folios with filters."""
    # Validate site_id is not empty string
    if site_id is not None and site_id.strip() == "":
        raise HTTPException(status_code=400, detail="site_id cannot be empty string")

    folios = store.get_folios(site_id=site_id)
    enrich_folios_with_status(folios, store)

    # Apply filters in Python (since we compute dynamically)
    if type:
        folios = [f for f in folios if f.type == type]

    if assigned_to:
        folios = [f for f in folios if f.assigned_to == assigned_to]

    if status:
        folios = [f for f in folios if f.status == status]

    return folios


@router.get("/folios/search")
async def search_folios(
    q: str = Query(...),
    type: Optional[FolioType] = None,
    status: Optional[str] = None,
    store: JSONStore = Depends(get_project_store),
):
    """Search folios by content using FTS5."""
    matching = store.search_folios(q)
    enrich_folios_with_status(matching, store)

    if type:
        matching = [f for f in matching if f.type == type]

    if status:
        matching = [f for f in matching if f.status == status]

    return matching


@router.get("/folios/{folio_id}", response_model=Union[Folio, VersionView])
async def get_folio(
    folio_id: str,
    x_project_id: Optional[str] = Header(None),
    store: JSONStore = Depends(get_project_store),
):
    """Get a folio by slug / project:slug / ref::<slug>, or a version by content
    hash (sha256::/hash::/station-scoped short). A by-hash fetch returns a
    VersionView (immutable content + is_head/lineage_head, no mutable control)."""
    folio = resolve_folio_read(folio_id, x_project_id, store)

    # A by-hash VersionView carries no mutable state — return it as-is. A Folio
    # gets the thread-derived status/assignment overlay — including cross-project
    # reads, against their SOURCE store (post-contraction there is no cache
    # column to fall back on, so skipping enrichment would return the model
    # defaults 'open'/None regardless of truth — deep_code_audit finding 1).
    if isinstance(folio, Folio):
        _overlay_thread_control(folio, store)

    return folio


@router.patch("/folios/{folio_id}")
async def update_folio(
    folio_id: str,
    update: FolioUpdate,
    x_agent_id: str = Header(None, alias="X-Agent-Id"),
    x_project_id: Optional[str] = Header(None),
    store: JSONStore = Depends(get_project_store),
):
    """Update folio fields (title, content, status, assigned_to)."""
    parsed = parse_address(folio_id)
    if parsed.is_qualified:
        # Explicit project — resolve to that project's store
        target_store = _get_safe_named_project_store(parsed.project)
        if not target_store:
            raise HTTPException(
                status_code=404,
                detail=f"Project '{parsed.project}' not found in registry",
            )
        store = target_store
        folio_id = parsed.folio_id

    folio = store.get_folio(folio_id)
    if not folio:
        detail = "Folio not found"
        found = search_folio_across_projects(folio_id, x_project_id)
        if found:
            detail = f"Folio not found in current project. Try: {found['project_name']}:{folio_id}"
        raise HTTPException(status_code=404, detail=detail)

    created_by = x_agent_id or "unknown"

    # Update title and content directly on the folio
    if update.title is not None:
        folio.title = update.title

    if update.content is not None:
        folio.content = update.content

    # PURE THREADS: Create status thread instead of updating field. save_thread
    # re-keys control edges onto the folio's genesis hash (Phase 3a A2), so the
    # slug endpoints here land genesis-anchored.
    if update.status is not None:
        status_thread = Thread(
            thread_id=generate_thread_id(),
            from_id=folio_id,
            to_id=folio_id,
            type="status",
            content=update.status,
            weaver=created_by,
            created_at=datetime.now(timezone.utc),
        )
        store.save_thread(status_thread)
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
            created_at=datetime.now(timezone.utc),
        )
        store.save_thread(assignment_thread)
        # Also update field for backward compat (will be removed after migration)
        folio.assigned_to = update.assigned_to

    # (The folio-archived feature — its archive-marker writer here, the
    # get_latest_archives reader, and the refs.archived column — was removed
    # 2026-07-08: zero archive threads and zero archived refs ecosystem-wide.)

    # editor (the per-edit agent) drives the supersedes/reverted edge weaver when a
    # content edit mints a new version; it is NOT folio.created_by (the genesis author).
    store.save_folio(folio, editor=created_by)
    # The response folio came off the store read (model-default status/assignment
    # post-contraction) — overlay the thread-derived truth so a title-only PATCH
    # doesn't report a closed folio as open (deep_code_audit finding 2).
    _overlay_thread_control(folio, store)
    return {"success": True, "folio": folio}


class FolioMoveRequest(BaseModel):
    """Request to move a folio to a different site."""

    dest_site_id: str
    note: Optional[str] = None


@router.post("/folios/{folio_id}/move")
async def move_folio(
    folio_id: str,
    move_request: FolioMoveRequest,
    x_agent_id: str = Header(None, alias="X-Agent-Id"),
    x_project_id: Optional[str] = Header(None),
    store: JSONStore = Depends(get_project_store),
):
    """
    Move a folio from its current site to a different site.

    Updates the folio's site_id and physically relocates the file.
    Optionally records a thread with the move reason.
    """
    parsed = parse_address(folio_id)
    if parsed.is_qualified:
        target_store = _get_safe_named_project_store(parsed.project)
        if not target_store:
            raise HTTPException(
                status_code=404,
                detail=f"Project '{parsed.project}' not found in registry",
            )
        store = target_store
        folio_id = parsed.folio_id

    created_by = x_agent_id or "unknown"

    try:
        moved_folio = store.move_folio(folio_id, move_request.dest_site_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if not moved_folio:
        detail = f"Folio '{folio_id}' not found"
        found = search_folio_across_projects(folio_id, x_project_id)
        if found:
            detail = f"Folio not found in current project. Try: {found['project_name']}:{folio_id}"
        raise HTTPException(status_code=404, detail=detail)

    # Create a thread recording the move if note provided
    if move_request.note:
        move_thread = Thread(
            thread_id=generate_thread_id(),
            from_id=created_by,
            to_id=folio_id,
            type="message",
            content=f"Moved to {move_request.dest_site_id}: {move_request.note}",
            weaver=created_by,
            created_at=datetime.now(timezone.utc),
        )
        store.save_thread(move_thread)

    # Same overlay as GET/PATCH: the moved head was reconstructed from the
    # refs⋈versions join, which carries no control state post-contraction
    # (deep_code_audit finding 3).
    _overlay_thread_control(moved_folio, store)
    return {
        "success": True,
        "folio": moved_folio,
        "moved_to": move_request.dest_site_id,
    }


# Thread Endpoints


@router.post("/threads")
async def create_thread(
    thread_create: ThreadCreate,
    x_agent_id: str = Header(None, alias="X-Agent-Id"),
    store: JSONStore = Depends(get_project_store),
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
        created_at=datetime.now(timezone.utc),
    )

    success = store.save_thread(thread)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to create thread")

    return {"success": True, "thread_id": thread_id}


@router.get("/threads", response_model=List[Thread])
async def get_threads(
    from_id: Optional[str] = None,
    to_id: Optional[str] = None,
    type: Optional[str] = None,
    weaver: Optional[str] = None,
    search: Optional[str] = None,
    since: Optional[str] = None,
    store: JSONStore = Depends(get_project_store),
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
    # Get base threads with existing filters. Use the presentation reader so
    # genesis-keyed control threads (Phase 3a A3) surface for a folio slug and are
    # rewritten back to the slug view every client display/count path expects.
    threads = store.get_threads_display(from_id=from_id, to_id=to_id, type=type)

    # Apply weaver filter
    if weaver:
        threads = [t for t in threads if t.weaver == weaver]

    # Apply content search filter
    if search:
        search_lower = search.lower()
        threads = [
            t for t in threads if t.content and search_lower in t.content.lower()
        ]

    # Apply time filter
    if since:
        try:
            since_dt = parse_relative_time(since)
            threads = [t for t in threads if t.created_at >= since_dt]
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    return threads


# Log Endpoints


@router.post("/logs")
async def post_logs(
    log_batch: LogBatch, log_db: LogDatabase = Depends(get_project_log_db)
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
    log_db: LogDatabase = Depends(get_project_log_db),
):
    """Get logs from a stream with filters."""
    return log_db.get_logs(stream_id, since, level, search, limit)


# Discovery Endpoints


@router.get("/activity")
async def get_activity(
    since: Optional[str] = None, store: JSONStore = Depends(get_project_store)
):
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

    # Thread-derived status/assignment, like every other folio read surface —
    # this endpoint was the one folio-returning read that skipped enrichment and
    # served the raw refs cache. Enrich only the slice that is returned.
    new_folios = folios[:10]
    enrich_folios_with_status(new_folios, store)

    return {
        "new_folios": new_folios,  # Last 10 folios
        "active_agents": list({f.created_by for f in folios}),
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
    x_agent_id: Optional[str] = Header(None, alias="X-Agent-Id"),
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
    start_time = time.time()

    # Parse resources
    resource_list = [r.strip() for r in resources.split(",")]
    valid_resources = {"folios", "threads", "agents", "sites"}
    for r in resource_list:
        if r not in valid_resources:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid resource type: '{r}'. Valid: {', '.join(valid_resources)}",
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
        # Use FTS5 when query is present, otherwise get all
        if q:
            folios = store.search_folios(q, limit=500)
        else:
            folios = store.get_folios()

        enrich_folios_with_status(folios, store)

        # Filters
        if type:
            folios = [f for f in folios if f.type == type]

        if site:
            folios = [f for f in folios if f.site_id == site]

        if sites:
            # Support glob patterns
            import fnmatch

            folios = [
                f
                for f in folios
                if any(fnmatch.fnmatch(f.site_id, pattern) for pattern in sites)
            ]

        if status:
            folios = [f for f in folios if f.status == status]

        if assigned_to:
            folios = [f for f in folios if f.assigned_to == assigned_to]

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
        folios = folios[offset : offset + limit]

        results["folios"] = {"total": folios_total, "items": folios}
        total += folios_total

    # Search threads
    if "threads" in resource_list:
        # Presentation reader: control threads (Phase 3a A3) are rewritten back to
        # the folio slug so the from_id/to_id filters below match them by slug.
        threads = store.get_threads_display()

        # Text search
        if q:
            q_lower = q.lower()
            threads = [t for t in threads if t.content and q_lower in t.content.lower()]

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
            threads = [t for t in threads if t.created_at >= since_dt]

        if before_dt:
            threads = [t for t in threads if t.created_at < before_dt]

        # Sort
        if sort in ["created", "relevance"]:
            threads.sort(key=lambda t: t.created_at, reverse=True)
        elif sort == "created_asc":
            threads.sort(key=lambda t: t.created_at)

        # Pagination
        threads_total = len(threads)
        threads = threads[offset : offset + limit]

        results["threads"] = {"total": threads_total, "items": threads}
        total += threads_total

    # Search agents
    if "agents" in resource_list:
        agents = store.get_agents()

        # Text search
        if q:
            q_lower = q.lower()
            agents = [
                a
                for a in agents
                if (
                    q_lower in a.agent_id.lower()
                    or q_lower in (a.name or "").lower()
                    or any(q_lower in cap.lower() for cap in (a.capabilities or []))
                )
            ]

        # Filters
        if agent_type:
            agents = [a for a in agents if a.agent_type == agent_type]

        if capabilities:
            agents = [
                a
                for a in agents
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
        agents = agents[offset : offset + limit]

        results["agents"] = {"total": agents_total, "items": agents}
        total += agents_total

    # Search sites
    if "sites" in resource_list:
        sites_list = store.get_sites()

        # Text search
        if q:
            q_lower = q.lower()
            sites_list = [
                s
                for s in sites_list
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
        sites_list = sites_list[offset : offset + limit]

        results["sites"] = {"total": sites_total, "items": sites_list}
        total += sites_total

    execution_time_ms = int((time.time() - start_time) * 1000)

    return {
        "query": q,
        "resources": resource_list,
        "filters": {
            k: v
            for k, v in {
                "status": status,
                "since": since,
                "before": before,
                "type": type.value if type and hasattr(type, "value") else type,
                "site": site,
                "sites": sites,
                "assigned_to": assigned_to,
                "thread_type": thread_type,
                "weaver": weaver,
                "from_id": from_id,
                "to_id": to_id,
                "agent_type": agent_type,
                "capabilities": capabilities,
                "sort": sort,
                "limit": limit,
                "offset": offset,
            }.items()
            if v is not None
        },
        "total": total,
        "results": results,
        "execution_time_ms": execution_time_ms,
    }


# Screenshot Endpoints


@router.post("/screenshots")
async def upload_screenshot(
    screenshot_create: ScreenshotCreate,
    screenshots_dir: Path = Depends(get_project_screenshots_dir),
    log_db: LogDatabase = Depends(get_project_log_db),
):
    """Upload a screenshot from web app."""
    # Generate screenshot ID
    timestamp = datetime.now(timezone.utc)
    screenshot_id = f"screenshot-{timestamp.strftime('%Y%m%d-%H%M%S-%f')}"

    # Create strand-specific directory
    strand_dir = screenshots_dir / screenshot_create.strand_id
    strand_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename
    turn_suffix = (
        f"_turn-{screenshot_create.turn_number}"
        if screenshot_create.turn_number
        else ""
    )
    filename = f"{timestamp.strftime('%Y-%m-%dT%H-%M-%S')}{turn_suffix}_{screenshot_create.label}.png"
    file_path = strand_dir / filename

    try:
        # Decode base64 and save
        screenshot_data = screenshot_create.screenshot_data
        if screenshot_data.startswith("data:image/png;base64,"):
            screenshot_data = screenshot_data.split(",")[1]

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
            metadata={},
        )

        logger.info(f"Screenshot saved: {screenshot_id} ({file_size} bytes)")

        return {"success": True, "screenshot_id": screenshot_id, "file_size": file_size}

    except Exception as e:
        logger.error(f"Failed to save screenshot: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to save screenshot: {str(e)}"
        )


@router.get("/screenshots", response_model=List[Screenshot])
async def list_screenshots(
    strand_id: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = Query(50, le=200),
    log_db: LogDatabase = Depends(get_project_log_db),
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
            metadata={},
        )
        for row in screenshots_data
    ]


@router.get("/screenshots/{screenshot_id}")
async def get_screenshot_image(
    screenshot_id: str, log_db: LogDatabase = Depends(get_project_log_db)
):
    """Get screenshot image file."""
    screenshot_data = log_db.get_screenshot(screenshot_id)

    if not screenshot_data:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    file_path = Path(screenshot_data["file_path"])

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Screenshot file not found")

    return FileResponse(
        file_path, media_type="image/png", filename=f"{screenshot_id}.png"
    )


@router.get("/screenshots/{screenshot_id}/metadata", response_model=Screenshot)
async def get_screenshot_metadata(
    screenshot_id: str, log_db: LogDatabase = Depends(get_project_log_db)
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
        metadata={},
    )


# Agent Naming


@router.post("/naming/generate")
async def generate_name(
    role: Optional[str] = None,
    brief_content: Optional[str] = None,
    project: Optional[str] = None,
    x_project_id: Optional[str] = Header(None),
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
        project=project_id, role=role, brief_content=brief_content
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
    log_db: LogDatabase = Depends(get_project_log_db),
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
        tender_id=yield_request.tender_id,
    )

    if not success:
        raise HTTPException(status_code=500, detail="Failed to store yield")

    logger.info(f"Stored yield {sack_id} for chain {yield_request.chain_id}")

    return {"success": True, "sack_id": sack_id, "chain_id": yield_request.chain_id}


@router.get("/yields/chain/{chain_id}", response_model=List[Yield])
async def get_chain_yields(
    chain_id: str, log_db: LogDatabase = Depends(get_project_log_db)
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
            metadata=y.get("metadata") or {},
        )
        for y in yields_data
    ]


@router.get("/yields/{sack_id}", response_model=Yield)
async def get_yield(sack_id: str, log_db: LogDatabase = Depends(get_project_log_db)):
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
        metadata=yield_data.get("metadata") or {},
    )


@router.get("/yields/status/{status}", response_model=List[Yield])
async def get_yields_by_status(
    status: str, log_db: LogDatabase = Depends(get_project_log_db)
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
            metadata=y.get("metadata") or {},
        )
        for y in yields_data
    ]


@router.get("/yields/agent/{agent_id}", response_model=List[Yield])
async def get_agent_yields(
    agent_id: str, log_db: LogDatabase = Depends(get_project_log_db)
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
            metadata=y.get("metadata") or {},
        )
        for y in yields_data
    ]


# ============================================================================
# Hypothesis Endpoints
# ============================================================================

class HypothesisVerdict(BaseModel):
    verdict: str
    note: Optional[str] = None
    evidence: Optional[str] = None  # finding folio ID


@router.get("/hypotheses/next/{site_id}")
async def hypothesis_next(
    site_id: str,
    store: JSONStore = Depends(get_project_store),
):
    """Get the highest-priority open hypothesis in a site."""
    site = store.get_site(site_id)
    if not site:
        raise HTTPException(status_code=404, detail=f"Site '{site_id}' not found")

    folios = store.get_folios(site_id=site_id)
    hypotheses = [f for f in folios if f.type == "hypothesis"]
    # Batch overlay (thread-derived) — the returned hypothesis must carry its
    # real status AND assigned_to (deep_code_audit r2 finding 3: this route was
    # never enriched, so assigned_to was always null post-contraction). Also
    # replaces the per-folio get_current_status N+1 below.
    enrich_folios_with_status(hypotheses, store)

    # Filter to open (pending) hypotheses
    open_hypos = []
    for h in hypotheses:
        # None-check, not truthiness (the '' invariant): after enrichment a
        # present-but-empty status must not read as open.
        status = h.status if h.status is not None else "open"
        # Extract just the verdict (first line)
        verdict = status.split("\n")[0]
        if verdict == "open":
            open_hypos.append(h)

    if not open_hypos:
        return {"hypothesis": None, "remaining": 0}

    # Sort by priority (high > medium > low), then by creation date (oldest first)
    open_hypos.sort(
        key=lambda h: (
            PRIORITY_ORDER.get(h.metadata.get("priority", "medium"), 1),
            h.created_at,
        )
    )

    return {"hypothesis": open_hypos[0], "remaining": len(open_hypos) - 1}


@router.get("/hypotheses/status/{site_id}")
async def hypothesis_status(
    site_id: str,
    store: JSONStore = Depends(get_project_store),
):
    """Get burndown counts for hypotheses in a site."""
    site = store.get_site(site_id)
    if not site:
        raise HTTPException(status_code=404, detail=f"Site '{site_id}' not found")

    folios = store.get_folios(site_id=site_id)
    hypotheses = [f for f in folios if f.type == "hypothesis"]
    # Same batch overlay as hypothesis_next (and same N+1 removal).
    enrich_folios_with_status(hypotheses, store)

    counts = {"pending": 0, "total": len(hypotheses)}
    for h in hypotheses:
        # Same None-check as hypothesis_next (the '' invariant).
        status = h.status if h.status is not None else "open"
        # Extract just the verdict (first line)
        verdict = status.split("\n")[0]
        if verdict == "open":
            counts["pending"] += 1
        else:
            counts[verdict] = counts.get(verdict, 0) + 1

    return counts


@router.post("/hypotheses/{hypothesis_id}/verdict")
async def hypothesis_verdict(
    hypothesis_id: str,
    verdict_req: HypothesisVerdict,
    x_agent_id: str = Header(None, alias="X-Agent-Id"),
    x_project_id: Optional[str] = Header(None),
    store: JSONStore = Depends(get_project_store),
):
    """Set verdict on a hypothesis. Creates a status thread."""
    folio = store.get_folio(hypothesis_id)
    if not folio:
        detail = "Hypothesis not found"
        found = search_folio_across_projects(hypothesis_id, x_project_id)
        if found:
            detail = f"Hypothesis not found in current project. This folio is registered in the {found['project_name']} project at {found['project_path']}"
        raise HTTPException(status_code=404, detail=detail)
    if folio.type != "hypothesis":
        raise HTTPException(
            status_code=400, detail=f"Folio '{hypothesis_id}' is not a hypothesis"
        )

    if verdict_req.verdict not in HYPOTHESIS_VERDICTS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid verdict '{verdict_req.verdict}'. Must be one of: {', '.join(sorted(HYPOTHESIS_VERDICTS))}",
        )

    # Check if already verdicted. None-checks, not truthiness: a PRESENT
    # empty-string status is state, not absence (the '' invariant) — it is
    # not 'open', so it blocks re-verdicting, consistent with
    # /hypotheses/next excluding it as non-open (fell finding, codex r1).
    current_status = get_current_status(hypothesis_id, store)
    if current_status is not None:
        verdict_value = current_status.split("\n")[0]
        if verdict_value != "open":
            raise HTTPException(
                status_code=400,
                detail=f"Hypothesis already has verdict '{verdict_value}'. Cannot re-verdict.",
            )

    # Confirmed requires evidence
    if verdict_req.verdict == "confirmed" and not verdict_req.evidence:
        raise HTTPException(
            status_code=400,
            detail="Confirmed verdict requires --evidence (finding folio ID)",
        )

    # Disconfirmed requires note
    if verdict_req.verdict == "disconfirmed" and not verdict_req.note:
        raise HTTPException(
            status_code=400,
            detail="Disconfirmed verdict requires --note (what was tried)",
        )

    # Validate evidence exists BEFORE any state mutation
    if verdict_req.evidence:
        evidence_folio = store.get_folio(verdict_req.evidence)
        if not evidence_folio:
            raise HTTPException(
                status_code=400,
                detail=f"Evidence folio '{verdict_req.evidence}' not found",
            )
        if evidence_folio.type != "finding":
            raise HTTPException(
                status_code=400,
                detail=f"Evidence must be a finding folio, got '{evidence_folio.type}'",
            )

    created_by = x_agent_id or "unknown"

    # Create status thread with verdict only (keeps get_current_status clean).
    # save_thread re-keys control edges onto the hypothesis lineage's genesis hash
    # (Phase 3a A2), so the slug endpoints here land genesis-anchored.
    status_thread = Thread(
        thread_id=generate_thread_id(),
        from_id=hypothesis_id,
        to_id=hypothesis_id,
        type="status",
        content=verdict_req.verdict,
        weaver=created_by,
        created_at=datetime.now(timezone.utc),
    )
    store.save_thread(status_thread)

    # Also update folio status field for backward compat
    folio.status = verdict_req.verdict
    # A verdict is a status-only change (no identity-field edit), so this mints no
    # version and writes no edge; editor is passed for consistency only.
    store.save_folio(folio, editor=created_by)

    # Store verdict note as a separate message thread (preserves folio content integrity)
    if verdict_req.note:
        note_thread = Thread(
            thread_id=generate_thread_id(),
            from_id=hypothesis_id,
            to_id=hypothesis_id,
            type="message",
            content=f"Verdict note ({verdict_req.verdict}): {verdict_req.note}",
            weaver=created_by,
            created_at=datetime.now(timezone.utc),
        )
        store.save_thread(note_thread)

    # Create reference thread to the evidence finding
    if verdict_req.evidence:
        ref_thread = Thread(
            thread_id=generate_thread_id(),
            from_id=hypothesis_id,
            to_id=verdict_req.evidence,
            type="reference",
            content=f"Evidence for {verdict_req.verdict} verdict",
            weaver=created_by,
            created_at=datetime.now(timezone.utc),
        )
        store.save_thread(ref_thread)

    return {
        "success": True,
        "hypothesis_id": hypothesis_id,
        "verdict": verdict_req.verdict,
    }


# ── Phase 4: author-declared publish (docs/PHASE_4_DESIGN.md §4) ─────────────
class PublishManifest(BaseModel):
    """The author-DECLARED set: exact content hashes to publish (§5.1)."""

    folios: List[str] = Field(default_factory=list)  # folio content_hashes
    threads: List[str] = Field(default_factory=list)  # thread_hashes


class PublishRequest(BaseModel):
    to: str  # target station publish URL
    manifest: PublishManifest = Field(default_factory=PublishManifest)
    # First-class site declaration: the named local workbench site is the selection
    # boundary. An empty manifest.folios means every current non-site head in it;
    # a non-empty list is an exact subset. site_slug defaults to the local site id.
    site: Optional[str] = None
    site_slug: Optional[str] = None
    # Ordinary explicit manifests can carry site claims directly. The first-class
    # site path synthesizes its one exact claim and rejects a second mapping.
    site_slugs: Dict[str, str] = Field(default_factory=dict)
    token: Optional[str] = None  # OIDC token from a prior 1-click login (§6)
    dry_run: bool = False


def _folio_wire_from_version(vv: VersionView) -> Dict[str, Any]:
    """Map a by-hash version to the publish wire folio (canonical fields + hash). The
    type is coerced to its raw string so it re-hashes to the same content hash."""
    ttype = vv.type.value if hasattr(vv.type, "value") else vv.type
    return {
        "content_hash": vv.content_hash,
        "type": ttype,
        "title": vv.title,
        "content": vv.content,
        "created_at": vv.created_at,
        "created_by": vv.created_by,
    }


def _folio_wire_from_folio(folio: Folio) -> Dict[str, Any]:
    """Map a workbench head folio to the same canonical publish row."""
    ttype = folio.type.value if hasattr(folio.type, "value") else folio.type
    return {
        "content_hash": folio.content_hash,
        "type": ttype,
        "title": folio.title,
        "content": folio.content,
        "created_at": folio.created_at,
        "created_by": folio.created_by,
    }


def _persist_site_publish_plan(
    store: JSONStore,
    anchor: Dict[str, Any],
    memberships: List[Dict[str, Any]],
) -> None:
    """Persist generated authoring state through the normal workbench writers."""
    folio = Folio(
        folio_id=anchor["folio_id"],
        type="site",
        site_id=anchor["site_id"],
        created_at=anchor["created_at"],
        created_by=anchor["created_by"],
        title=anchor["title"],
        content=anchor["content"],
        metadata={"publish_anchor": True},
    )
    if not store.save_folio(folio):
        raise RuntimeError("failed to save the local site anchor")
    saved_anchor = store.get_version_by_hash(anchor["content_hash"])
    if saved_anchor is None:
        raise RuntimeError("local site anchor was not persisted")

    for row in memberships:
        thread = Thread(
            thread_id=row["thread_id"],
            from_id=row["from_id"],
            to_id=row["to_id"],
            type="within",
            content=None,
            weaver=None,
            created_at=ensure_aware(row["created_at"]),
        )
        if not store.save_thread(thread):
            raise RuntimeError(f"failed to save membership {row['thread_hash']}")
        if store.get_thread_by_hash(row["thread_hash"]) is None:
            raise RuntimeError(f"membership {row['thread_hash']} was not persisted")


@router.post("/publish")
def publish_folios(
    body: PublishRequest,
    store: JSONStore = Depends(get_project_store),
) -> Dict[str, Any]:
    """Sign an author-declared manifest of content hashes and POST it to a remote
    station's ingress (curlable; the thin CLI wraps this). Declarative: the author
    names exact hashes; the server resolves each from its own store, lints (advisory),
    then physics→sign→post via the un-forgettable orchestrator. ``dry_run`` returns the
    resolved set + warnings without signing or sending (no Rekor entry)."""
    # MAX_LEAVES caps only Merkle constituents. Slug claims are sideband mappings
    # validated against declared site folios, not additional manifest leaves.
    declared_count = len(body.manifest.folios) + len(body.manifest.threads)
    if declared_count > _pub.MAX_LEAVES:
        raise HTTPException(
            status_code=400,
            detail=f"declared set exceeds MAX_LEAVES ({_pub.MAX_LEAVES}): {declared_count}",
        )

    if body.site_slug is not None and body.site is None:
        raise HTTPException(status_code=400, detail="site_slug requires a site selection")
    if body.site is not None and body.site_slugs:
        raise HTTPException(
            status_code=400,
            detail="site selection creates its slug claim; do not also pass site_slugs",
        )

    folios: List[Dict[str, Any]] = []
    generated_anchor: Optional[Dict[str, Any]] = None
    generated_memberships: List[Dict[str, Any]] = []
    site_thread_endpoints: Optional[set[str]] = None
    site_slugs: Dict[str, str] = dict(body.site_slugs)

    if body.site is not None:
        source_site = body.site.strip()
        if not source_site:
            raise HTTPException(status_code=400, detail="site cannot be empty")
        local_site = store.get_site(source_site)
        if local_site is None:
            raise HTTPException(status_code=404, detail=f"unknown workbench site: {source_site}")

        try:
            public_slug = _pub.validate_site_slug(body.site_slug or source_site)
        except _pub.SiteSlugError as e:
            raise HTTPException(status_code=400, detail=str(e))

        generated_anchor = _pub.build_site_anchor(
            local_site.site_id,
            local_site.purpose,
            local_site.created_at,
            local_site.created_by,
        )
        existing_anchor = store.get_folio(generated_anchor["folio_id"])
        if (
            existing_anchor is not None
            and existing_anchor.content_hash != generated_anchor["content_hash"]
        ):
            raise HTTPException(
                status_code=409,
                detail=f"local site anchor id collision: {generated_anchor['folio_id']}",
            )

        # Local site membership is the selection boundary. Never scan or infer from
        # the project at large, and never make an older/generated site anchor a member.
        member_heads = {
            f.content_hash: f
            for f in store.get_folios(site_id=source_site)
            if f.type != "site" and f.content_hash
        }
        if body.manifest.folios:
            selected = []
            seen = set()
            for h in body.manifest.folios:
                if h in seen:
                    continue
                seen.add(h)
                member = member_heads.get(h)
                if member is None:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"folio {h!r} is not a current non-site head in "
                            f"workbench site {source_site!r}"
                        ),
                    )
                selected.append(member)
        else:
            selected = [member_heads[h] for h in sorted(member_heads)]

        folios = [dict(generated_anchor)] + [_folio_wire_from_folio(f) for f in selected]
        site_thread_endpoints = {
            endpoint for member in selected for endpoint in (member.folio_id, member.content_hash)
        }
        site_thread_endpoints.update(
            (generated_anchor["folio_id"], generated_anchor["content_hash"])
        )
        generated_memberships = [
            _pub.build_within_membership(
                member.content_hash,
                generated_anchor["content_hash"],
                member.created_at,
            )
            for member in selected
        ]
        site_slugs = {generated_anchor["content_hash"]: public_slug}
    else:
        for h in body.manifest.folios:
            vv = store.get_version_by_hash(h)
            if vv is None:
                raise HTTPException(status_code=404, detail=f"unknown folio hash: {h}")
            folios.append(_folio_wire_from_version(vv))

    threads: List[Dict[str, Any]] = []
    for th in body.manifest.threads:
        row = store.get_thread_by_hash(th)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown thread hash: {th}")
        if site_thread_endpoints is not None and (
            row["from_id"] not in site_thread_endpoints or row["to_id"] not in site_thread_endpoints
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"thread {th!r} has an endpoint outside the selected workbench "
                    f"site {body.site!r}"
                ),
            )
        threads.append(row)

    if generated_memberships:
        # Generated membership is part of the author declaration. De-duplicate an
        # explicitly named copy by identity, while preserving the stable generated order.
        by_hash = {row["thread_hash"]: row for row in generated_memberships}
        for row in threads:
            by_hash.setdefault(row["thread_hash"], row)
        threads = list(by_hash.values())

    exact_count = len(folios) + len(threads)
    if exact_count > _pub.MAX_LEAVES:
        raise HTTPException(
            status_code=400,
            detail=f"declared set exceeds MAX_LEAVES ({_pub.MAX_LEAVES}): {exact_count}",
        )

    warnings = _pub.lint_declared_set(folios, threads)  # total, advisory, never blocks
    declared = {
        "folios": [f["content_hash"] for f in folios],
        "threads": [t["thread_hash"] for t in threads],
        "site_slugs": site_slugs,
    }

    # Physics and claim validation are identical for preview and send, and both run
    # before any site authoring write or signing ceremony.
    try:
        _pub.physics_check(folios, threads)
        site_slugs = _pub.validate_site_slugs(folios, site_slugs)
    except _pub.PhysicsError as e:
        raise HTTPException(status_code=400, detail=f"physics rejected: {e}")
    except _pub.SiteSlugError as e:
        raise HTTPException(status_code=400, detail=f"site slug rejected: {e}")

    if body.dry_run:
        return {"declared": declared, "warnings": warnings, "signed": False, "sent": False}

    # Cheap input validation BEFORE the irreversible Sigstore ceremony — fail closed
    # with a 4xx and never waste a Rekor entry on trivially-bad input (gate §10 #9).
    if not folios and not threads:
        raise HTTPException(status_code=400, detail="nothing to publish: the manifest is empty")
    target = (body.to or "").strip()
    _parts = urlsplit(target)
    if _parts.scheme not in ("http", "https") or not _parts.netloc:
        raise HTTPException(
            status_code=400, detail=f"invalid target url (need http(s):// with a host): {body.to!r}"
        )
    if not body.token:
        raise HTTPException(
            status_code=400, detail="publish requires a token (run login) — refusing to sign"
        )

    try:
        signer = _pub.signer_from_token(body.token)  # issuer derived from the token only
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"publish rejected: {e}")

    # A real, authenticated site send materializes exactly the state previewed above,
    # using the ordinary versions/refs and threads writers. This local authoring state is
    # intentionally durable even if the remote POST fails: its identities are stable, so
    # a retry converges on the same declaration instead of minting replacements. Explicit-
    # manifest publishing remains read-only locally, as before.
    if generated_anchor is not None:
        try:
            _persist_site_publish_plan(store, generated_anchor, generated_memberships)
        except Exception as e:
            logger.exception("publish route: failed to persist site authoring state")
            raise HTTPException(status_code=500, detail=f"site authoring failed: {e}")

    try:
        ack = _pub.publish(target, folios, threads, signer, site_slugs=site_slugs)
    except _pub.PhysicsError as e:
        raise HTTPException(status_code=400, detail=f"physics rejected: {e}")
    except _pub.PublishError as e:
        raise HTTPException(status_code=502, detail=f"publish failed: {e}")
    except (ValueError, _pub.canon.MerkleError) as e:
        raise HTTPException(status_code=400, detail=f"manifest rejected: {e}")
    except Exception as e:  # a bad/expired token surfaces from signing — fail closed 4xx
        logger.exception("publish route: unexpected signing/assembly failure")
        raise HTTPException(status_code=400, detail=f"publish rejected: {e}")
    return {"declared": declared, "warnings": warnings, "signed": True, "sent": True, "ack": ack}
