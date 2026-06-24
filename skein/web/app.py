"""Read surface for the content-hash station, served on port 9001.

One store, several representations chosen by content negotiation, all built from
the SAME native wire envelope (``skein.envelope``):

- **The machine wire** (the product) — the verifiable envelope served as JSON or
  self-orienting agent markdown (``.md`` or ``Accept: text/markdown``). The raw
  folio ``body.content`` alone is reached via ``.json``.
- **HTML** — the human view, rendered FROM that same envelope (slice 3). The
  legacy ``ContentHashAdapter`` is retired: JSON and HTML can no longer diverge
  on derived fields because they read the one envelope.

Presentation is themeable (brief-20260603-s4gq). A station's identity and look
come from its **stationfile** (``.skein/stationfile.json``): a required
``name`` plus optional tagline/logo/theme/tokens. The base sheet ships the stable
class hooks (the theming contract); a theme (``ulm`` default, ``classic``, or a
custom sheet) layers on top; stationfile ``tokens`` set CSS custom properties
inline (the no-CSS path). Owners get CSS only — never the markup or the structured
path — so a hostile sheet can't reach the spine.

Negotiation (§7): an explicit ``.json``/``.md`` suffix wins; else ``Accept``
decides; else the User-Agent picks — browsers get HTML, ``skein``/CLI tools get
markdown, anything unrecognized defaults to HTML (the human surface).

Routes are synchronous so FastAPI runs each in the threadpool with its own
per-request store (and SQLite connection).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Iterator, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from .. import envelope as envelope_mod
from .. import render as render_mod
from ..resolve import ResolveError, resolve_to_hash
from ..stationfile import StationConfig, StationfileError, load_station_config
from ..store import SkeinStore, make_snippet

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_PORT = 9001  # legacy stays on 8001/8003; new station is 9001

# Max addresses in one POST /resolve batch (fork D, pinned 256). Over-cap is
# rejected whole rather than truncated, so a caller never silently loses the tail.
BATCH_CAP = 256

# Max bytes of a POST /resolve body, checked before buffering (the count cap can
# only run post-parse). 256 KiB comfortably holds 256 of the longest addresses
# (web::authority::sha256::<64hex>#sha256::<64hex>) with JSON overhead.
MAX_BATCH_BYTES = 256 * 1024

# Max search results returned in one query; the envelope flags `truncated` when
# the cap is hit so a consumer knows more may exist.
SEARCH_LIMIT = 100
ENV_DATA_DIR = "SKEIN_DATA_DIR"
ENV_PROJECT = "SKEIN_PROJECT"  # the stationfile-name bootstrap env

# The instance's own web:: authority, when published behind a domain. Unset in
# Phase 1 (bare-hash addresses).
ENV_AUTHORITY = "SKEIN_AUTHORITY"

# The station's public origin (``https://host``), used ONLY to build the absolute
# ``.md`` fetch URLs the agent-markdown opener and references advertise. It is a
# display concern, deliberately decoupled from address resolution (ENV_AUTHORITY)
# and from the client-controlled Host header: a configured value is deterministic
# and immune to Host-header injection into the rendered markdown. When unset, the
# renderer falls back to the request origin (convenient in dev), then to a
# host-relative path. Set it in production (the instance is behind nginx, which by
# default would otherwise leak the internal 127.0.0.1:9001 origin).
ENV_BASE_URL = "SKEIN_BASE_URL"

_MARKDOWN_MEDIA = "text/markdown; charset=utf-8"

# HTTP status for each resolve error code. The envelope is the product; the
# status is a sane secondary signal.
_ERROR_STATUS = {
    "not_found": 404,
    "invalid_address": 400,
    "short_hash_unsupported_remote": 400,
    "hash_mismatch": 422,
    "ambiguous_short_hash": 422,
}

# Human-facing one-liners for the themed HTML error page (the machine surface
# returns the structured error envelope instead).
_ERROR_MESSAGES = {
    "not_found": "No folio resolves to that address here.",
    "invalid_address": "That address is not a well-formed SKEIN address.",
    "short_hash_unsupported_remote": "A short hash can't be expanded for a remote authority.",
    "hash_mismatch": "The resolved content does not match the requested hash.",
    "ambiguous_short_hash": "That short hash matches more than one folio — use more digits.",
}

# html=False escapes raw HTML embedded in folio markdown, so rendered content
# cannot inject markup — the v0 sanitization posture.
_md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})

# stationfile token -> CSS custom property (the no-CSS override path). Only these
# string tokens map to a property; default_theme drives the data-theme attribute.
_TOKEN_TO_VAR = {
    "accent": "--accent",
    "font_body": "--font-body",
    "font_mono": "--font-mono",
}


def clean_title(title: str, fallback: str = "") -> str:
    """First real line of a folio title, stripped of leading markdown decoration."""
    if not title:
        return fallback
    first = next((ln.strip() for ln in title.splitlines() if ln.strip()), "")
    first = re.sub(r"^#+\s*", "", first)
    first = re.sub(r"^\*\*|^__", "", first)
    if len(first) > 120:
        first = first[:117] + "..."
    return first or fallback


def render_markdown(content: str) -> str:
    """Render a folio body to HTML, demoting its headings one level.

    The page already carries the folio title as the single ``<h1>``; a body that
    opens with ``# Title`` (the common case) would otherwise emit a second,
    near-duplicate ``<h1>`` — a redundant stop in a screen reader and a malformed
    outline. Demoting in-body headings (``#`` → ``h2``, capped at ``h6``) nests
    them under the title, giving one clean heading outline. The agent markdown
    (``.md``) and the raw ``body.content`` (via ``.json``) keep the authored
    heading levels; only this HTML view nests.
    """
    if not content:
        return ""
    tokens = _md.parse(content, {})
    for tok in tokens:
        if tok.type in ("heading_open", "heading_close"):
            tok.tag = f"h{min(int(tok.tag[1]) + 1, 6)}"
    return _md.renderer.render(tokens, _md.options, {})


def get_data_dir() -> Optional[str]:
    return os.environ.get(ENV_DATA_DIR)


def get_authority() -> Optional[str]:
    return os.environ.get(ENV_AUTHORITY)


def public_base_url(request: Request) -> str:
    """The station's public origin for building absolute ``.md`` fetch URLs.

    Prefers the configured ``SKEIN_BASE_URL`` (deterministic, injection-proof
    — see ENV_BASE_URL); falls back to the request origin in dev. Always returned
    without a trailing slash; ``""`` only if neither is available, which the
    renderer treats as "emit a host-relative path".
    """
    configured = os.environ.get(ENV_BASE_URL)
    if configured:
        from urllib.parse import urlsplit

        parts = urlsplit(configured)
        if parts.scheme and parts.netloc:
            return configured.rstrip("/")
        # A scheme-less value (e.g. "example.com") would yield a non-absolute
        # "example.com/folio/…md", which is worse than the request fallback. Ignore
        # it loudly rather than silently emit broken links.
        logger.warning(
            "ignoring malformed %s=%r (need an absolute scheme://host)", ENV_BASE_URL, configured
        )
    try:
        return str(request.base_url).rstrip("/")
    except Exception:
        return ""


def load_config() -> StationConfig:
    """Resolve the station config from the stationfile + the env bootstrap.

    Raises :class:`StationfileError` if the station has no name — the one hard
    requirement. ``create_app`` calls this once at startup, so a misconfigured
    station refuses to start (caught in ``run_server`` for a clean message).
    """
    return load_station_config(get_data_dir(), env_name=os.environ.get(ENV_PROJECT))


def build_token_css(config: StationConfig) -> str:
    """The inline ``:root`` CSS-custom-property overrides for the station tokens.

    Token values are pre-sanitized by the loader (no ``<>{};``), so they are safe
    single declaration values inside the ``<style>`` block.
    """
    return " ".join(
        f"{var}: {config.tokens[key]};"
        for key, var in _TOKEN_TO_VAR.items()
        if key in config.tokens
    )


def get_store() -> Iterator[SkeinStore]:
    """Per-request read-only store with its own connection; closed at request end."""
    store = SkeinStore(get_data_dir(), check_same_thread=False, read_only=True)
    try:
        yield store
    finally:
        store.close()


# --- content negotiation ----------------------------------------------------

_AGENT_UA_TOKENS = ("skein", "curl", "wget", "python", "httpie", "go-http")


def split_representation(ref: str) -> tuple[str, Optional[str]]:
    """Split a trailing ``.json``/``.md`` suffix off an address path."""
    for suffix, name in ((".json", "json"), (".md", "md")):
        if ref.endswith(suffix):
            return ref[: -len(suffix)], name
    return ref, None


def negotiate(suffix: Optional[str], accept: Optional[str], user_agent: Optional[str]) -> str:
    """Pick a representation: ``json`` | ``markdown`` | ``html``.

    A ``.json``/``.md`` suffix wins. Then an explicit ``Accept``. Absent both, the
    User-Agent decides — a browser (``Mozilla``) gets HTML, a CLI/agent tool gets
    markdown, anything unrecognized defaults to HTML (the human surface stays the
    safe default; an agent that wants the wire says so via suffix/Accept/UA).

    The ``.md`` suffix is the full, self-orienting agent markdown — the opener +
    fenced body + fetchable references (brief-20260606-7ddh). This reconciles the
    earlier "raw `.md` = body-only" split (ujwx rev 2) up to 7ddh's amendment: the
    copyable ``.md`` URL the for-agents box hands to an assistant must itself orient
    a cold agent, which a header-only (``Accept``) representation cannot. Raw
    ``body.content`` is still reachable via ``.json``.
    """
    if suffix == "json":
        return "json"
    if suffix == "md":
        return "markdown"

    accept = accept or ""
    if "application/json" in accept:
        return "json"
    if "text/markdown" in accept:
        return "markdown"
    if "text/html" in accept:
        return "html"

    ua = (user_agent or "").lower()
    if "mozilla" in ua:
        return "html"
    if any(token in ua for token in _AGENT_UA_TOKENS):
        return "markdown"
    return "html"


def _wants_machine(request: Request, suffix: Optional[str]) -> Optional[str]:
    """The non-HTML representation for a collection route, or ``None`` for HTML."""
    repr_ = negotiate(suffix, request.headers.get("accept"), request.headers.get("user-agent"))
    return None if repr_ == "html" else repr_


def _payload_etag(payload: bytes) -> str:
    import hashlib

    return f'"{hashlib.sha256(payload).hexdigest()}"'


def _conditional(request: Request, etag: str, headers: dict) -> Optional[Response]:
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return None


def _json_revalidate(request: Request, env: dict, *, status: int = 200) -> Response:
    """Serialize an envelope and serve it ``no-cache`` with an ETag over the exact
    bytes — so a conditional GET only 304s when the WHOLE envelope (including the
    derived ``asserted`` block) is byte-identical."""
    import json

    payload = json.dumps(env, ensure_ascii=False, separators=(",", ":")).encode()
    etag = _payload_etag(payload)
    headers = {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Accept"}
    not_modified = _conditional(request, etag, headers)
    if not_modified is not None:
        return not_modified
    return Response(payload, status_code=status, media_type="application/json", headers=headers)


def _folio_response(request: Request, store, content_hash: str, repr_: str, row) -> Response:
    env = envelope_mod.build_folio_envelope(store, content_hash, row=row)

    if repr_ == "json":
        return _json_revalidate(request, env)

    # `.md` and Accept: text/markdown both serve the full agent markdown (opener +
    # fenced body + fetchable references). Per-fetch nonce ⇒ no-store; raw
    # body.content is reached via `.json` (decision A / brief-20260606-7ddh).
    text, nonce = render_mod.render_folio_markdown(env, base_url=public_base_url(request))
    return PlainTextResponse(
        text,
        media_type=_MARKDOWN_MEDIA,
        headers={"X-Skein-Nonce": nonce, "Cache-Control": "no-store", "Vary": "Accept"},
    )


def _error_response(
    request: Request,
    code: str,
    address: str,
    repr_: str,
    *,
    origin: Optional[str] = None,
    vary: bool = True,
) -> Response:
    env = envelope_mod.build_error_envelope(code, address, origin=origin)
    status = _ERROR_STATUS.get(code, 400)
    # ``vary=False`` for a non-negotiated subresource (the bundle JSON), whose
    # representation does not depend on Accept; the negotiated routes default True.
    base_headers = {"Cache-Control": "no-store"}
    if vary:
        base_headers["Vary"] = "Accept"
    if repr_ == "json":
        return JSONResponse(env, status_code=status, headers=base_headers)
    # No X-Skein-Nonce here on purpose: an error envelope carries no untrusted
    # content, so render_error_markdown emits no fence — there is no close marker to
    # protect and nothing for a programmatic agent to split on. The nonce header is
    # a fenced-response signal; an unfenced error correctly omits it.
    return PlainTextResponse(
        render_mod.render_error_markdown(env, base_url=public_base_url(request)),
        status_code=status,
        media_type=_MARKDOWN_MEDIA,
        headers=base_headers,
    )


def _collection_response(request: Request, env: dict, repr_: str, *, title: str) -> Response:
    if repr_ == "json":
        return JSONResponse(env, headers={"Cache-Control": "no-cache", "Vary": "Accept"})
    text, nonce = render_mod.render_collection_markdown(
        env, title=title, base_url=public_base_url(request)
    )
    return PlainTextResponse(
        text,
        media_type=_MARKDOWN_MEDIA,
        headers={"X-Skein-Nonce": nonce, "Cache-Control": "no-store", "Vary": "Accept"},
    )


def verdict_state(verdict: Optional[str]) -> str:
    """Map a folio verdict line to a provenance state class (the HTML accent).

    The label text carries the meaning; this only drives reinforcement color. The
    five accents mirror ``envelope.folio_verdict``'s prefixes:

    - ``SIGNED`` -> "verified"
    - ``SIGNATURE INVALID`` -> "invalid"
    - ``UNVERIFIED`` (signature present, verifier unavailable) -> "unverified"
    - ``NOT VERIFIED`` (manifest verifies but signer unbound/revoked, or
      membership/proof fails) -> "unverified" — a load-bearing not-verified state,
      NEVER collapsed into the benign never-signed "unsigned" bucket below.
    - ``UNSIGNED`` (never cryptographically signed) -> "unsigned" (final else).
    """
    v = verdict or ""
    if v.startswith("SIGNED"):
        return "verified"
    if v.startswith("SIGNATURE INVALID"):
        return "invalid"
    if v.startswith("UNVERIFIED"):
        return "unverified"
    if v.startswith("NOT VERIFIED"):
        return "unverified"
    return "unsigned"


def create_app() -> FastAPI:
    config = load_config()  # fail-loud on an unnamed station, at startup
    token_css = build_token_css(config)
    data_theme = config.tokens.get("default_theme")

    app = FastAPI(
        title="SKEIN (next)",
        description="Content-addressed SKEIN read surface (machine wire + themed HTML)",
        version="0.1.0",
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters["clean_title"] = clean_title

    # The shared template context: the station identity + theming, on every page.
    base_ctx = {"station": config, "token_css": token_css, "data_theme": data_theme}

    def html(request: Request, name: str, ctx: dict, *, status: int = 200) -> Response:
        # Vary: Accept — HTML is one Accept/UA-negotiated representation among json
        # and markdown, so a shared cache must key on Accept (RFC 9110 §12.5.5).
        return templates.TemplateResponse(
            request,
            name,
            {**base_ctx, "request": request, **ctx},
            status_code=status,
            headers={"Vary": "Accept"},
        )

    def html_error(request: Request, code: str, address: str) -> Response:
        status = _ERROR_STATUS.get(code, 400)
        return html(
            request,
            "error.html",
            {"message": _ERROR_MESSAGES.get(code, code), "address": address},
            status=status,
        )

    # --- custom theme sheet (level 2) ---------------------------------------

    @app.get("/theme.css")
    def theme_css() -> Response:
        # Shipped themes are served from /static; this route serves only a custom
        # sheet from the data dir. Containment is RE-CHECKED here at read time, not
        # just at config load: resolve() follows symlinks, so a sheet swapped for a
        # symlink pointing outside the data dir after startup is caught by
        # relative_to() rather than disclosed (TOCTOU; cross-model review catch).
        if config.is_shipped_theme:
            raise HTTPException(status_code=404, detail="no custom theme configured")
        data_dir = get_data_dir()
        css = ""
        if data_dir:
            base = Path(data_dir).resolve()
            try:
                target = (base / config.theme).resolve()
                target.relative_to(base)  # reject any path escaping the data dir
                css = target.read_text(encoding="utf-8")
            except (OSError, ValueError):
                css = ""
        return Response(css, media_type="text/css", headers={"Cache-Control": "no-cache"})

    # --- index / catalog ----------------------------------------------------

    def _catalog_envelope(store) -> dict:
        rows = store.list_folios(limit=1_000_000)
        recent = sorted(rows, key=lambda r: r.get("created_at") or "", reverse=True)[:30]
        entries = [envelope_mod.folio_entry(r) for r in recent]
        counts: dict = {}
        for slug in store.folio_site_slugs().values():
            counts[slug] = counts.get(slug, 0) + 1
        sites = [
            {"slug": slug, "address": h, "href": f"/site/{slug}", "count": counts.get(slug, 0)}
            for slug, h in store.list_slugs()
        ]
        sites.sort(key=lambda s: s["count"], reverse=True)
        return envelope_mod.build_collection_envelope(
            "catalog",
            "/",
            entries,
            asserted={
                "name": config.name,
                "wire": envelope_mod.SCHEMA,
                "profile": envelope_mod.CANON_PROFILE,
                "sites": sites,
                "total_folios": store.count_folios(),
                "example": "mesh fetch sha256::<digest>",
            },
            links={"catalog": "/"},
        )

    @app.get("/")
    def index(request: Request, store: SkeinStore = Depends(get_store)):
        env = _catalog_envelope(store)
        repr_ = _wants_machine(request, None)
        if repr_ is None:
            return html(request, "index.html", {"env": env})
        return _collection_response(request, env, repr_, title=f"Catalog — {config.name}")

    @app.get("/.json")
    def index_json(request: Request, store: SkeinStore = Depends(get_store)):
        return _collection_response(request, _catalog_envelope(store), "json", title="Catalog")

    @app.get("/.md")
    def index_md(request: Request, store: SkeinStore = Depends(get_store)):
        return _collection_response(
            request, _catalog_envelope(store), "markdown", title=f"Catalog — {config.name}"
        )

    # --- site ---------------------------------------------------------------

    def _site_rows(store, slug: str):
        """Resolve a site and fetch its folios once, newest-first.

        Returns ``(site_hash, rows)`` or ``(None, [])`` for an unknown slug. A
        single pass feeds both the type-filtered envelope and the HTML filter
        chrome's available-types set, so a site page is one scan, not two.
        """
        site_hash = store.resolve_slug(slug)
        if not site_hash:
            return None, []
        rows = store.folios_in_site(site_hash)
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return site_hash, rows

    def _site_envelope(slug: str, site_hash: str, rows: list, type: Optional[str] = None) -> dict:
        filtered = [r for r in rows if type is None or (r.get("type") or "folio") == type]
        entries = [envelope_mod.folio_entry(r) for r in filtered]
        address = f"/site/{slug}" + (f"?type={type}" if type else "")
        return envelope_mod.build_collection_envelope(
            "site",
            address,
            entries,
            asserted={"slug": slug, "address": site_hash, "type": type, "count": len(filtered)},
            links={"catalog": "/", "self": f"/site/{slug}"},
        )

    @app.get("/site/{site_id}", response_class=HTMLResponse)
    def site_detail(
        request: Request,
        site_id: str,
        type: Optional[str] = None,
        store: SkeinStore = Depends(get_store),
    ):
        slug, suffix = split_representation(site_id)
        repr_ = _wants_machine(request, suffix)
        site_hash, rows = _site_rows(store, slug)
        if site_hash is None:
            if repr_ is not None:
                return _error_response(request, "not_found", f"/site/{slug}", repr_)
            return html_error(request, "not_found", f"/site/{slug}")
        env = _site_envelope(slug, site_hash, rows, type=type)
        if repr_ is not None:
            return _collection_response(request, env, repr_, title=f"Site — {slug}")
        available_types = sorted({(r.get("type") or "folio") for r in rows})
        return html(
            request,
            "site.html",
            {
                "env": env,
                "available_types": available_types,
                "current_type": type,
            },
        )

    # --- folio --------------------------------------------------------------

    def _serve_bundle(request: Request, store, address: str) -> Response:
        try:
            content_hash = resolve_to_hash(address, store, local_authority=get_authority())
        except ResolveError as e:
            return _error_response(request, e.code, e.address, "json", origin=e.origin, vary=False)
        proof = store.get_constituent_proof(content_hash)
        if proof is None or proof.get("proof_missing") or not proof.get("bundle_json"):
            return _error_response(request, "not_found", f"{address}/bundle", "json", vary=False)
        payload = proof["bundle_json"].encode()
        etag = _payload_etag(payload)
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        not_modified = _conditional(request, etag, headers)
        if not_modified is not None:
            return not_modified
        return Response(payload, media_type="application/json", headers=headers)

    def _render_folio_html(request, store, content_hash: str, row) -> Response:
        env = envelope_mod.build_folio_envelope(store, content_hash, row=row)
        body_html = render_markdown(env["body"].get("content") or "")
        return html(
            request,
            "folio.html",
            {
                "env": env,
                "body_html": body_html,
                "prov_state": verdict_state(env["asserted"].get("verdict")),
                "base_url": public_base_url(request),
            },
        )

    @app.get("/folio/{ref:path}", response_class=HTMLResponse)
    def folio_detail(
        request: Request,
        ref: str,
        store: SkeinStore = Depends(get_store),
    ):
        # The signature bundle sub-resource (the dead-end-free provenance link).
        if ref.endswith("/bundle"):
            return _serve_bundle(request, store, ref[: -len("/bundle")])

        address, suffix = split_representation(ref)
        repr_ = negotiate(suffix, request.headers.get("accept"), request.headers.get("user-agent"))
        is_html = repr_ == "html"

        try:
            content_hash = resolve_to_hash(address, store, local_authority=get_authority())
        except ResolveError as e:
            if is_html:
                return html_error(request, e.code, e.address)
            return _error_response(request, e.code, e.address, repr_, origin=e.origin)
        row = store.get_folio(content_hash)
        if row is None:
            if is_html:
                return html_error(request, "not_found", address)
            return _error_response(request, "not_found", address, repr_)

        if is_html:
            return _render_folio_html(request, store, content_hash, row)
        return _folio_response(request, store, content_hash, repr_, row)

    # --- batch resolve (fork D) ---------------------------------------------

    def _resolve_one(store, address: str) -> dict:
        """One batch element: a folio envelope, or an inline error envelope.

        Adds no new trust surface — each element is resolved and built exactly as
        a single GET resolve, so it is independently verifiable and cacheable by
        its own hash. A bad address becomes a ``kind: error`` envelope in place,
        never a failure of the whole batch.
        """
        if not isinstance(address, str):
            return envelope_mod.build_error_envelope("invalid_address", str(address))
        try:
            content_hash = resolve_to_hash(address, store, local_authority=get_authority())
        except ResolveError as e:
            return envelope_mod.build_error_envelope(e.code, e.address, origin=e.origin)
        row = store.get_folio(content_hash)
        if row is None:
            return envelope_mod.build_error_envelope("not_found", address)
        return envelope_mod.build_folio_envelope(store, content_hash, row=row)

    def _batch_error(code: str, detail: str, status: int) -> Response:
        env = envelope_mod.build_error_envelope(code, detail)
        return JSONResponse(env, status_code=status, headers={"Cache-Control": "no-store"})

    @app.post("/resolve")
    async def resolve_batch(request: Request, store: SkeinStore = Depends(get_store)):
        """Resolve a list of addresses to an array of envelopes, in request order.

        The batch *wrapper* is derived (a query); each *element* is an independent
        envelope (a stable folio or an inline error). POST-with-list, not a
        synthetic ``batch::`` address (URL length).

        The request is bounded by BYTES before it is buffered (``MAX_BATCH_BYTES``)
        and by element COUNT after parse (``BATCH_CAP``); over either is rejected
        WHOLE, never silently truncated, so the caller never loses the tail. The
        byte cap is the DoS guard — the count cap alone can't run until after a
        parse — and is defense-in-depth atop the fronting proxy's body limit.
        ``batch_too_large`` / ``invalid_batch`` are batch-wrapper error codes,
        distinct from the resolve §6 codes that ride each element.
        """
        import json

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_BATCH_BYTES:
                    return _batch_error("batch_too_large", "request body too large", 413)
            except ValueError:
                pass

        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BATCH_BYTES:
                return _batch_error("batch_too_large", "request body too large", 413)

        try:
            addresses = json.loads(body) if body else []
        except ValueError:
            return _batch_error("invalid_batch", "request body is not valid JSON", 400)
        if not isinstance(addresses, list):
            return _batch_error("invalid_batch", "request body must be a JSON array of addresses", 400)
        if len(addresses) > BATCH_CAP:
            return _batch_error("batch_too_large", f"{len(addresses)} addresses exceeds {BATCH_CAP}", 413)

        # The route is async (to stream-bound the body), but _resolve_one does
        # blocking SQLite reads — run the whole batch off the event loop so it
        # doesn't stall other requests, the way the sync routes get the threadpool.
        from starlette.concurrency import run_in_threadpool

        results = await run_in_threadpool(lambda: [_resolve_one(store, a) for a in addresses])
        # A batch is a POST result, never a cacheable GET; each element stays
        # independently cacheable by its own hash when fetched singly.
        return JSONResponse(results, headers={"Cache-Control": "no-store"})

    # --- search (L1: AND-of-terms, ranked, snippets) -----------------------

    def _search_envelope(store, q: str) -> dict:
        terms = q.split()
        rows = store.search_folios(q, limit=SEARCH_LIMIT) if q.strip() else []
        entries = [
            envelope_mod.folio_entry(r, snippet=make_snippet(r.get("content"), terms))
            for r in rows
        ]
        # `truncated` is the honest signal that the result set was capped at the
        # limit (more matches may exist, and L1 ranks only within that window — it
        # is not a global relevance sort). A consumer can page/refine on it.
        return envelope_mod.build_collection_envelope(
            "search",
            "/search" + (f"?q={q}" if q else ""),
            entries,
            asserted={"query": q, "count": len(entries), "truncated": len(entries) >= SEARCH_LIMIT},
            links={"catalog": "/", "self": "/search"},
        )

    @app.get("/search", response_class=HTMLResponse)
    def search(
        request: Request,
        q: str = Query("", description="Search query"),
        store: SkeinStore = Depends(get_store),
    ):
        env = _search_envelope(store, q)
        repr_ = _wants_machine(request, None)
        if repr_ is None:
            return html(request, "search.html", {"env": env, "q": q})
        return _collection_response(request, env, repr_, title=f"Search — {q}")

    @app.get("/search.json")
    def search_json(
        request: Request,
        q: str = Query("", description="Search query"),
        store: SkeinStore = Depends(get_store),
    ):
        return _collection_response(request, _search_envelope(store, q), "json", title=f"Search — {q}")

    @app.get("/search.md")
    def search_md(
        request: Request,
        q: str = Query("", description="Search query"),
        store: SkeinStore = Depends(get_store),
    ):
        return _collection_response(request, _search_envelope(store, q), "markdown", title=f"Search — {q}")

    # --- well-known root / describe (the discovery + MCP `describe` target) --

    def _describe(store) -> dict:
        return {
            "skein": "station/v1",
            "name": config.name,
            "wire": envelope_mod.SCHEMA,
            "profile": envelope_mod.CANON_PROFILE,
            "address_grammar": "rev3",
            # The HTML view is content-first in source order (theming rev 3 O6a), so
            # an agent/crawler knows the markup leads with content, not chrome.
            "html_source_order": "content-first",
            "operations": {
                "resolve": "GET /folio/{address}[.json|.md]",
                "resolve_batch": "POST /resolve",
                "search": "GET /search[.json|.md]?q=",
                "list": "GET /site/{slug}[.json|.md]",
                "catalog": "GET /[.json|.md]",
                "bundle": "GET /folio/{address}/bundle",
                "describe": "GET /.well-known/skein[.json|.md]",
            },
            "nonce_fence": render_mod.WELL_KNOWN_FENCE_RULE,
            "totals": {"folios": store.count_folios(), "sites": len(store.list_slugs())},
            "example": "mesh fetch sha256::<digest>",
        }

    def _describe_response(request: Request, store, suffix: Optional[str]) -> Response:
        # Metadata, not a content page: markdown on request, else JSON (a browser
        # with no explicit preference gets JSON here, not an HTML view).
        repr_ = negotiate(suffix, request.headers.get("accept"), request.headers.get("user-agent"))
        doc = _describe(store)
        if repr_ == "markdown":
            return PlainTextResponse(
                render_mod.render_describe_markdown(doc),
                media_type=_MARKDOWN_MEDIA,
                headers={"Cache-Control": "no-cache", "Vary": "Accept"},
            )
        return JSONResponse(doc, headers={"Cache-Control": "no-cache", "Vary": "Accept"})

    @app.get("/.well-known/skein")
    def well_known(request: Request, store: SkeinStore = Depends(get_store)):
        return _describe_response(request, store, None)

    @app.get("/.well-known/skein.json")
    def well_known_json(request: Request, store: SkeinStore = Depends(get_store)):
        return _describe_response(request, store, "json")

    @app.get("/.well-known/skein.md")
    def well_known_md(request: Request, store: SkeinStore = Depends(get_store)):
        return _describe_response(request, store, "md")

    return app


def run_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    try:
        app = create_app()
    except StationfileError as e:
        # A misconfigured station refuses to start with a clean operator message,
        # not a traceback. Name it (the one hard requirement) and try again.
        logger.error("station will not start: %s", e)
        raise SystemExit(2) from e
    logger.info("Starting skein web UI on http://%s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
