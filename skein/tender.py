"""Tender metadata envelope (agent-coordination port, Stage 1).

The content-hash ``folios`` table has no metadata column (unlike legacy, whose
POST /folios carried a ``metadata`` dict alongside the markdown body). So a
tender folio carries its structured fields — ``worktree_name``, ``branch_name``,
``commits``, ``confidence``, ``status``, ``reviewer``, ``name``,
``files_modified`` — *inside* the folio content: a human-readable markdown body
followed by one machine-readable block, a single fenced ```json behind a stable
marker.

One writer (:func:`render_tender_content`) and one reader
(:func:`parse_tender_meta`), so the embedding format can never drift. The parser
keys only off the marker plus the fence, so hand-edits to the prose above the
marker cannot break extraction, and there is no ad-hoc regex over the prose.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, Optional

# A stable, human-invisible anchor (HTML comment) introducing the meta block.
TENDER_META_MARKER = "<!-- tender-meta -->"

# The marker, then a fenced ```json ... ``` block. Anchored on the marker so a
# ```json block appearing in the prose body is never mistaken for the meta.
# DOTALL lets the JSON span lines; the non-greedy body stops at the first closing
# fence.
_META_RE = re.compile(
    re.escape(TENDER_META_MARKER) + r"\s*```json\s*\n(?P<json>.*?)\n```",
    re.DOTALL,
)


def render_tender_content(body: str, meta: Dict[str, Any]) -> str:
    """Compose tender folio content: the human ``body`` then the pinned meta block.

    ``meta`` is serialized with sorted keys so the rendered content (and thus the
    folio's content hash) is stable for a given logical tender, and so
    ``render → parse → render`` round-trips exactly.
    """
    block = json.dumps(meta, indent=2, ensure_ascii=False, sort_keys=True)
    body = (body or "").rstrip("\n")
    return f"{body}\n\n{TENDER_META_MARKER}\n```json\n{block}\n```\n"


def parse_tender_meta(content: Optional[str]) -> Optional[Dict[str, Any]]:
    """Extract the meta dict from tender ``content``; ``None`` if absent or invalid.

    Returns ``None`` (not a partial dict) when the marker/fence is missing, the
    fenced text is not valid JSON, or the JSON is not an object — so callers can
    treat "no usable meta" as a single condition.

    The real envelope is the LAST marker that actually introduces a valid
    ```json fence holding a JSON object. We scan marker occurrences right to
    left and return the first such match. This is robust to two kinds of decoy:
    a marker + fence in the agent-authored body (the real block is appended
    after it), and the marker string appearing inside a meta *value* (it can't
    be followed by a real fence, so its anchored match fails and we keep scanning
    left). Anchoring each attempt at the marker position — rather than taking the
    last of a non-overlapping regex scan — also stops an earlier *unclosed* decoy
    fence from swallowing the real block's closing fence.
    """
    if not content:
        return None
    search_end = len(content)
    while True:
        idx = content.rfind(TENDER_META_MARKER, 0, search_end)
        if idx == -1:
            return None
        m = _META_RE.match(content, idx)
        if m:
            try:
                data = json.loads(m.group("json"))
            except (json.JSONDecodeError, ValueError):
                data = None
            if isinstance(data, dict):
                return data
        search_end = idx  # this marker wasn't the envelope; look further left


def latest_tenders_by_worktree(
    folios: Iterable[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Map each worktree_name to its LATEST tender folio (with parsed meta attached).

    This is the consumer half of the tender envelope. A tender's status moving
    incomplete→complete is a status thread, but a changed confidence is a brand
    new (append-only) tender folio — so a worktree can have several tender folios
    and only the newest is current. Legacy ``triage``/``inspect`` built their
    tender map by overwriting in server-iteration order, which is not newest-wins;
    this selects the max ``(created_at, content_hash)`` per worktree, matching the
    store's own ``(created_at, content_hash)`` tie-break so the winner is stable
    regardless of input ordering.

    Folios whose content has no parseable meta block, or whose meta omits
    ``worktree_name``, are skipped. Each returned value is the folio dict with a
    ``meta`` key holding the parsed envelope.
    """
    best: Dict[str, Dict[str, Any]] = {}
    best_key: Dict[str, tuple] = {}
    for folio in folios:
        meta = parse_tender_meta(folio.get("content"))
        if not meta:
            continue
        worktree = meta.get("worktree_name")
        if not worktree:
            continue
        key = (folio.get("created_at") or "", folio.get("content_hash") or "")
        if worktree not in best_key or key > best_key[worktree]:
            best_key[worktree] = key
            enriched = dict(folio)
            enriched["meta"] = meta
            best[worktree] = enriched
    return best
