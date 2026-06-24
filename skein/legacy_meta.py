"""Legacy ``metadata`` preservation envelope (import bridge).

A legacy SKEIN folio carried a free-form ``metadata`` JSON column the new
content-hash model has no place for (the canonical folio is five fields:
type/title/content/created_at/created_by). Rather than drop it, the bridge folds
a folio's legacy metadata into its CONTENT — the same move the new system already
makes for structured fields (:mod:`skein.tender`, :mod:`skein.agent`):
a human-readable body, then one machine-readable block behind a stable marker.

One writer (:func:`render_legacy_meta`) and one reader (:func:`parse_legacy_meta`)
so the embedding can never drift. The parser anchors on the marker plus the fence
and scans right-to-left for the LAST valid block, so a decoy marker in the body
(or inside a metadata value) cannot fool it — the robustness the tender/agent
envelopes have.

Why content and not a new column: folding into content keeps the metadata inside
the folio's content hash — immutable and signed like everything else — and needs
no schema change. The imported folio's hash already differs from its legacy
``content_hash`` (the bridge re-mints; the legacy hash is never trusted), so
absorbing metadata into content is consistent with that re-hash, not a new break.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Dict, Optional

# A stable, human-invisible anchor (HTML comment) introducing the meta block.
LEGACY_META_MARKER = "<!-- legacy-meta -->"

# The marker, then a fenced ```json ... ``` block. Anchored on the marker so a
# ```json block in the body is never mistaken for the meta. DOTALL lets the JSON
# span lines; the non-greedy body stops at the first closing fence.
_META_RE = re.compile(
    re.escape(LEGACY_META_MARKER) + r"\s*```json\s*\n(?P<json>.*?)\n```",
    re.DOTALL,
)


def render_legacy_meta(body: Optional[str], meta: Dict[str, Any]) -> str:
    """Compose folio content: the legacy ``body`` then the pinned legacy-meta block.

    ``meta`` is serialized with sorted keys so the rendered content (and thus the
    imported folio's content hash) is stable for a given (body, meta) pair, and so
    ``render → parse → render`` round-trips exactly. A stable hash matters because
    the import is idempotent: re-importing must collapse to the same folio (the
    store's ``INSERT OR IGNORE`` dedups on the hash).

    ``meta`` is assumed non-empty; the caller does not envelope an empty dict (a
    folio with no real metadata keeps its plain body, no marker). ``body`` may be
    ``None`` (a legacy folio with metadata but no content body).

    ``ensure_ascii=True`` is deliberate (NOT the ``False`` the tender/agent
    envelopes use): a legacy metadata cell can be valid UTF-8 TEXT holding a JSON
    ``\\uD800`` escape, which ``json.loads`` materializes as a lone surrogate. With
    ``ensure_ascii=False`` that surrogate would render literally and the content
    would fail to UTF-8 encode, aborting the folio insert (canon rejects it). ASCII
    escaping keeps every code point representable, so the rendered envelope is
    always storable; it round-trips because :func:`parse_legacy_meta` decodes the
    escapes back, and re-rendering is byte-stable. The cost is that non-ASCII
    metadata renders escaped — acceptable for a machine-read preservation block.
    """
    block = json.dumps(meta, indent=2, ensure_ascii=True, sort_keys=True)
    body = body or ""
    # Preserve the legacy body byte-for-byte — do NOT rstrip. Trailing newlines are
    # real content on the migration path; the tender/agent envelopes trim because
    # they build the body, but here it is the original folio content. Add only
    # enough newlines to seat the marker after one blank line, deleting none.
    if not body or body.endswith("\n\n"):
        joiner = ""
    elif body.endswith("\n"):
        joiner = "\n"
    else:
        joiner = "\n\n"
    return f"{body}{joiner}{LEGACY_META_MARKER}\n```json\n{block}\n```\n"


def parse_legacy_meta(content: Optional[str]) -> Optional[Dict[str, Any]]:
    """Extract the legacy-meta dict from folio ``content``; ``None`` if absent/invalid.

    Returns ``None`` (not a partial dict) when the marker/fence is missing, the
    fenced text is not valid JSON, or the JSON is not an object — so callers treat
    "no usable legacy meta" as one condition. The real envelope is the LAST marker
    that introduces a valid ```json fence holding an object; markers are scanned
    right to left and each attempt is anchored at the marker position, so a decoy
    marker or an earlier unclosed fence cannot swallow the real block (mirrors
    :func:`skein.agent.parse_agent_meta`).
    """
    if not content:
        return None
    search_end = len(content)
    while True:
        idx = content.rfind(LEGACY_META_MARKER, 0, search_end)
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


# Keys carried in the legacy column that are intentionally NOT preserved — dead
# weight whose drop was signed off (a separate product call, see the bridge).
#   questions_enabled: a flag the legacy `post brief` command hardcodes True on
#     every brief; never read anywhere. Dropping it is a no-op.
_DROP_KEYS = frozenset({"questions_enabled"})


class _DuplicateKeys(ValueError):
    """A JSON object held duplicate keys — Python's ``json`` would collapse them
    to the last value, silently losing the earlier one. We refuse to interpret
    such a cell and preserve its text verbatim instead."""


def _dict_no_dupes(pairs):
    """``object_pairs_hook`` that rejects duplicate keys instead of collapsing.

    Fires for every object, nested included, so a duplicate anywhere in the
    structure aborts interpretation and routes the whole cell to verbatim
    preservation — never a silent last-value-wins collapse."""
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _DuplicateKeys()
    return dict(pairs)


def normalize_legacy_meta(raw: Any) -> Optional[Dict[str, Any]]:
    """Turn a legacy ``metadata`` cell into the dict to embed, or ``None`` to skip.

    The cell is whatever the legacy column held — from sqlite that is ``str`` or
    ``None``; already-parsed structures are tolerated defensively. Normalization
    is lossless EXCEPT for the explicitly dead :data:`_DROP_KEYS`:

    - a ``dict`` (already parsed) → its keys minus the dead ones; ``None`` if
      nothing remains;
    - a JSON-object string → same, after parsing;
    - a ``list``/number/bool, or valid JSON that is not an object → wrapped under
      ``{"_value": ...}`` so it is preserved rather than dropped for not being a
      dict;
    - text that is not JSON, OR a JSON object with DUPLICATE keys (which Python
      would silently collapse) → preserved verbatim under ``{"_raw": "..."}``;
    - ``bytes`` → UTF-8 decoded then treated as text; bytes that are NOT valid
      UTF-8 (which can't ride as SQLite TEXT, and whose surrogate-escaped decode
      would crash the insert) → preserved exactly under ``{"_raw_base64": "..."}``;
    - ``None``/empty/``"{}"``/``"null"`` → ``None`` (nothing to carry).

    So the only metadata ever dropped is the dead flag; everything else is
    preserved in some form, never silently lost — including the pathological
    duplicate-key case (unreachable for the json.dumps-written legacy corpus, but
    made lossless rather than collapsing, since this is migration code)."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        kept = {k: v for k, v in raw.items() if k not in _DROP_KEYS}
        return kept or None
    if isinstance(raw, (list, int, float, bool)):
        return {"_value": raw}
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Invalid UTF-8 can't be stored as TEXT, and a surrogateescape decode
            # would leave lone surrogates that crash the SQLite insert. Preserve the
            # exact bytes base64-encoded — lossless and pure-ASCII. (Unreachable for
            # the json.dumps-written legacy corpus; defensive.)
            return {"_raw_base64": base64.b64encode(raw).decode("ascii")}
    else:
        text = raw if isinstance(raw, str) else str(raw)
    if text.strip() in ("", "{}", "null"):
        return None
    try:
        data = json.loads(text, object_pairs_hook=_dict_no_dupes)
    except (json.JSONDecodeError, ValueError):
        # invalid JSON, OR a duplicate-key object — preserve the original verbatim.
        return {"_raw": text}
    if isinstance(data, dict):
        kept = {k: v for k, v in data.items() if k not in _DROP_KEYS}
        return kept or None
    return {"_value": data}
