"""Agent-folio metadata envelope (agent-coordination port, Stage 2).

A roster agent is a ``type=agent`` folio (design D1). Its lifecycle rides status
threads, but its descriptive fields — ``agent_type``, ``description``,
``capabilities``, and an open ``metadata`` bag (mantle, ignited_from, message,
ready_at, …) — have nowhere to live in the columnar ``folios`` table, which has
no metadata column. So, exactly as a tender carries its structured fields inside
the folio body (:mod:`skein.tender`), an agent folio carries its metadata in
content: a short human line, then one machine-readable block — a single fenced
```json behind a stable marker.

One writer (:func:`render_agent_content`) and one reader
(:func:`parse_agent_meta`), so the embedding can never drift. The parser keys
only off the marker plus the fence and scans right-to-left for the LAST valid
block, so a decoy marker in the human line (or inside a metadata value) cannot
fool it — the same robustness the tender envelope has.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

# A stable, human-invisible anchor (HTML comment) introducing the meta block.
AGENT_META_MARKER = "<!-- agent-meta -->"

# The marker, then a fenced ```json ... ``` block. Anchored on the marker so a
# ```json block in the human line is never mistaken for the meta. DOTALL lets the
# JSON span lines; the non-greedy body stops at the first closing fence.
_META_RE = re.compile(
    re.escape(AGENT_META_MARKER) + r"\s*```json\s*\n(?P<json>.*?)\n```",
    re.DOTALL,
)


def render_agent_content(body: str, meta: Dict[str, Any]) -> str:
    """Compose agent folio content: the human ``body`` then the pinned meta block.

    ``meta`` is serialized with sorted keys so the rendered content (and thus the
    agent folio's content hash) is stable for a given logical registration, and so
    ``render → parse → render`` round-trips exactly. A stable hash matters because
    re-running an identical registration must collapse to one folio (the store's
    ``INSERT OR IGNORE`` dedups on the hash).
    """
    block = json.dumps(meta, indent=2, ensure_ascii=False, sort_keys=True)
    body = (body or "").rstrip("\n")
    return f"{body}\n\n{AGENT_META_MARKER}\n```json\n{block}\n```\n"


def parse_agent_meta(content: Optional[str]) -> Optional[Dict[str, Any]]:
    """Extract the meta dict from agent folio ``content``; ``None`` if absent/invalid.

    Returns ``None`` (not a partial dict) when the marker/fence is missing, the
    fenced text is not valid JSON, or the JSON is not an object — so callers treat
    "no usable meta" as one condition. The real envelope is the LAST marker that
    introduces a valid ```json fence holding an object; markers are scanned right
    to left and each attempt is anchored at the marker position, so a decoy marker
    or an earlier unclosed fence cannot swallow the real block (mirrors
    :func:`skein.tender.parse_tender_meta`).
    """
    if not content:
        return None
    search_end = len(content)
    while True:
        idx = content.rfind(AGENT_META_MARKER, 0, search_end)
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
