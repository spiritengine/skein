"""
SKEIN Web Application - Server-rendered HTMX interface.
"""

import logging
import os
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request, Depends, Query, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import re

from ..storage import JSONStore, LogDatabase, get_data_dir_for_project
from ..utils import get_current_status, get_current_assignment

logger = logging.getLogger(__name__)


def clean_title(title: str, fallback: str = "") -> str:
    """Clean up a folio title for display."""
    if not title:
        return fallback
    # Strip markdown headers
    title = re.sub(r'^#+\s*', '', title)
    # Strip leading ** or __
    title = re.sub(r'^\*\*|^__', '', title)
    # Truncate
    if len(title) > 80:
        title = title[:77] + "..."
    return title or fallback

# Template directory
TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def get_project_id() -> str:
    """Get project ID from environment."""
    project_id = os.environ.get("SKEIN_PROJECT")
    if not project_id:
        # Try to find from .skein/config.json
        import json
        cwd = Path.cwd()
        while cwd != cwd.parent:
            config_file = cwd / ".skein" / "config.json"
            if config_file.exists():
                try:
                    with open(config_file) as f:
                        config = json.load(f)
                        project_id = config.get("project_id")
                        if project_id:
                            break
                except:
                    pass
            cwd = cwd.parent
    return project_id or "default"


def get_store() -> JSONStore:
    """Get JSONStore for current project."""
    project_id = get_project_id()
    data_dir = get_data_dir_for_project(project_id)
    return JSONStore(data_dir)


def get_log_db() -> LogDatabase:
    """Get LogDatabase for current project."""
    project_id = get_project_id()
    data_dir = get_data_dir_for_project(project_id)
    db_path = data_dir / "skein.db"
    return LogDatabase(db_path)


def create_app() -> FastAPI:
    """Create the SKEIN web application."""
    app = FastAPI(
        title="SKEIN Web UI",
        description="Browser interface for SKEIN collaboration",
        version="0.1.0"
    )

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    # Add custom filters
    templates.env.filters['clean_title'] = clean_title

    # Mount static files if directory exists
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request, store: JSONStore = Depends(get_store)):
        """Home page - activity feed."""
        sites = store.get_sites()
        folios = store.get_folios()
        agents = store.get_agents()

        # Filter to open only, compute status
        open_folios = []
        for f in folios:
            status = get_current_status(f.folio_id, store) or f.status or "open"
            if status != "closed":
                f.status = status
                open_folios.append(f)

        # Sort by created_at, newest first
        open_folios.sort(key=lambda f: f.created_at, reverse=True)
        folios = open_folios[:50]

        return templates.TemplateResponse("home.html", {
            "request": request,
            "folios": folios,
            "total_folios": len(store.get_folios()),
            "total_sites": len(sites),
            "active_agents": len([a for a in agents if a.status == "active"]),
            "project_id": get_project_id()
        })

    @app.get("/sites", response_class=HTMLResponse)
    async def sites_list(request: Request, store: JSONStore = Depends(get_store)):
        """List all sites."""
        sites = store.get_sites()
        folios = store.get_folios()

        # Count folios per site with type breakdown
        site_stats = {}
        for site in sites:
            site_id = site.site_id
            site_folios = [f for f in folios if f.site_id == site_id]
            by_type = {}
            by_status = {"open": 0, "closed": 0}
            for folio in site_folios:
                # Compute status from threads
                computed_status = get_current_status(folio.folio_id, store) or folio.status or "open"
                by_type[folio.type] = by_type.get(folio.type, 0) + 1
                if computed_status == "closed":
                    by_status["closed"] += 1
                else:
                    by_status["open"] += 1

            site_stats[site_id] = {
                "total": len(site_folios),
                "by_type": by_type,
                "by_status": by_status
            }

        return templates.TemplateResponse("sites.html", {
            "request": request,
            "sites": sites,
            "site_stats": site_stats,
            "project_id": get_project_id()
        })

    @app.get("/sites/{site_id}", response_class=HTMLResponse)
    async def site_detail(
        request: Request,
        site_id: str,
        type: Optional[str] = None,
        status: Optional[str] = None,
        store: JSONStore = Depends(get_store)
    ):
        """Site detail view with folios."""
        site = store.get_site(site_id)
        if not site:
            raise HTTPException(status_code=404, detail=f"Site '{site_id}' not found")

        folios = store.get_folios(site_id=site_id)

        # Compute status from threads for each folio
        for folio in folios:
            folio.status = get_current_status(folio.folio_id, store) or folio.status or "open"
            folio.assigned_to = get_current_assignment(folio.folio_id, store) or folio.assigned_to

        # Apply filters
        if type:
            folios = [f for f in folios if f.type == type]
        if status:
            folios = [f for f in folios if f.status == status]

        # Sort by created_at, newest first
        folios.sort(key=lambda f: f.created_at, reverse=True)

        # Get unique types and statuses for filter dropdowns
        all_folios = store.get_folios(site_id=site_id)
        available_types = sorted(set(f.type for f in all_folios))
        available_statuses = sorted(set(
            get_current_status(f.folio_id, store) or f.status or "open"
            for f in all_folios
        ))

        return templates.TemplateResponse("site_detail.html", {
            "request": request,
            "site": site,
            "folios": folios,
            "current_type": type,
            "current_status": status,
            "available_types": available_types,
            "available_statuses": available_statuses,
            "project_id": get_project_id()
        })

    @app.get("/folios/{folio_id}", response_class=HTMLResponse)
    async def folio_detail(
        request: Request,
        folio_id: str,
        store: JSONStore = Depends(get_store)
    ):
        """Folio detail view."""
        folio = store.get_folio(folio_id)
        if not folio:
            raise HTTPException(status_code=404, detail=f"Folio '{folio_id}' not found")

        # Compute status from threads
        folio.status = get_current_status(folio.folio_id, store) or folio.status or "open"
        folio.assigned_to = get_current_assignment(folio.folio_id, store) or folio.assigned_to

        # Get threads related to this folio
        threads = store.get_threads(from_id=folio_id)
        threads += store.get_threads(to_id=folio_id)
        # Dedupe
        seen = set()
        unique_threads = []
        for t in threads:
            if t.thread_id not in seen:
                seen.add(t.thread_id)
                unique_threads.append(t)
        threads = sorted(unique_threads, key=lambda t: t.created_at)

        # Get site info
        site = store.get_site(folio.site_id)

        # Find cross-references (folio IDs mentioned in content)
        import re
        cross_refs = []
        # Match patterns like brief-20251208-0jt9, issue-20251207-akrj, etc.
        folio_id_pattern = r'\b(brief|issue|friction|finding|notion|summary|tender|plan|playbook|mantle|writ)-\d{8}-[a-z0-9]{4}\b'
        mentioned_ids = re.findall(folio_id_pattern, folio.content, re.IGNORECASE) if folio.content else []
        # Get full matches
        full_matches = re.findall(r'\b(?:brief|issue|friction|finding|notion|summary|tender|plan|playbook|mantle|writ)-\d{8}-[a-z0-9]{4}\b', folio.content, re.IGNORECASE) if folio.content else []
        for ref_id in set(full_matches):
            if ref_id != folio_id:  # Don't self-reference
                ref_folio = store.get_folio(ref_id)
                if ref_folio:
                    cross_refs.append(ref_folio)

        return templates.TemplateResponse("folio_detail.html", {
            "request": request,
            "folio": folio,
            "site": site,
            "threads": threads,
            "cross_refs": cross_refs,
            "project_id": get_project_id()
        })

    @app.get("/activity", response_class=HTMLResponse)
    async def activity_log(
        request: Request,
        limit: int = Query(50, le=200),
        store: JSONStore = Depends(get_store)
    ):
        """Activity log - recent folios and changes."""
        folios = store.get_folios()

        # Compute status for each folio
        for folio in folios:
            folio.status = get_current_status(folio.folio_id, store) or folio.status or "open"
            folio.assigned_to = get_current_assignment(folio.folio_id, store) or folio.assigned_to

        # Sort by created_at, newest first
        folios.sort(key=lambda f: f.created_at, reverse=True)
        folios = folios[:limit]

        # Get recent threads
        threads = store.get_threads()
        threads.sort(key=lambda t: t.created_at, reverse=True)
        threads = threads[:limit]

        return templates.TemplateResponse("activity.html", {
            "request": request,
            "folios": folios,
            "threads": threads,
            "limit": limit,
            "project_id": get_project_id()
        })

    @app.get("/roster", response_class=HTMLResponse)
    async def roster(
        request: Request,
        status: Optional[str] = None,
        store: JSONStore = Depends(get_store)
    ):
        """Agent roster view."""
        agents = store.get_agents(status=status)
        agents.sort(key=lambda a: a.registered_at, reverse=True)

        return templates.TemplateResponse("roster.html", {
            "request": request,
            "agents": agents,
            "current_status": status,
            "project_id": get_project_id()
        })

    # HTMX partial endpoints for dynamic updates

    @app.get("/htmx/folios", response_class=HTMLResponse)
    async def htmx_folios(
        request: Request,
        site_id: Optional[str] = None,
        type: Optional[str] = None,
        status: Optional[str] = None,
        store: JSONStore = Depends(get_store)
    ):
        """HTMX partial: folio list."""
        folios = store.get_folios(site_id=site_id)

        # Compute status from threads
        for folio in folios:
            folio.status = get_current_status(folio.folio_id, store) or folio.status or "open"
            folio.assigned_to = get_current_assignment(folio.folio_id, store) or folio.assigned_to

        if type:
            folios = [f for f in folios if f.type == type]
        if status:
            folios = [f for f in folios if f.status == status]

        folios.sort(key=lambda f: f.created_at, reverse=True)

        return templates.TemplateResponse("partials/folio_list.html", {
            "request": request,
            "folios": folios
        })

    @app.get("/htmx/sites", response_class=HTMLResponse)
    async def htmx_sites(request: Request, store: JSONStore = Depends(get_store)):
        """HTMX partial: site list."""
        sites = store.get_sites()
        folios = store.get_folios()

        site_stats = {}
        for site in sites:
            site_id = site.site_id
            site_folios = [f for f in folios if f.site_id == site_id]
            site_stats[site_id] = {"total": len(site_folios)}

        return templates.TemplateResponse("partials/site_list.html", {
            "request": request,
            "sites": sites,
            "site_stats": site_stats
        })

    return app


def run_server(host: str = "127.0.0.1", port: int = 8003, reload: bool = False):
    """Run the SKEIN web server."""
    app = create_app()
    logger.info(f"Starting SKEIN Web UI on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
