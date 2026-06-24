"""Tests for the agent-folio content envelope (skein.agent).

Mirrors the tender-envelope tests: one writer / one reader, marker + fenced json,
round-trips exactly, and the right-to-left scan ignores decoys.
"""

from skein.agent import (
    AGENT_META_MARKER,
    parse_agent_meta,
    render_agent_content,
)


def test_round_trip():
    meta = {
        "agent_id": "nub-0622",
        "name": "nub-0622",
        "agent_type": "claude-code",
        "description": "stage 2",
        "capabilities": ["python", "review"],
        "metadata": {"mantle": "quartermaster"},
    }
    content = render_agent_content("nub-0622 — stage 2", meta)
    assert parse_agent_meta(content) == meta


def test_render_is_stable_for_hash():
    # Same logical meta → byte-identical content (sorted keys), so the folio hash
    # is stable across re-registration regardless of dict ordering.
    a = render_agent_content("body", {"b": 1, "a": 2})
    b = render_agent_content("body", {"a": 2, "b": 1})
    assert a == b


def test_missing_marker_returns_none():
    assert parse_agent_meta("just a plain body, no envelope") is None
    assert parse_agent_meta("") is None
    assert parse_agent_meta(None) is None


def test_invalid_json_returns_none():
    bad = f"body\n\n{AGENT_META_MARKER}\n```json\n{{not valid}}\n```\n"
    assert parse_agent_meta(bad) is None


def test_non_object_json_returns_none():
    arr = f"body\n\n{AGENT_META_MARKER}\n```json\n[1, 2, 3]\n```\n"
    assert parse_agent_meta(arr) is None


def test_decoy_marker_in_body_is_skipped():
    # An agent-authored body that itself contains a marker + fenced block must not
    # fool the parser: the REAL envelope is appended after, and the right-to-left
    # scan finds it.
    decoy = f"{AGENT_META_MARKER}\n```json\n{{\"agent_id\": \"impostor\"}}\n```"
    real = {"agent_id": "real", "name": "real"}
    content = render_agent_content(decoy, real)
    assert parse_agent_meta(content) == real


def test_marker_string_inside_a_value_is_not_an_envelope():
    # The marker appearing inside a meta value can't be followed by a real fence,
    # so its anchored match fails and the true block still wins.
    meta = {"description": f"see {AGENT_META_MARKER} for details", "name": "x"}
    content = render_agent_content("body", meta)
    assert parse_agent_meta(content) == meta
