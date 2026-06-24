"""Round-trip tests for the tender metadata envelope (Stage 1 port)."""

from skein.tender import (
    TENDER_META_MARKER,
    latest_tenders_by_worktree,
    parse_tender_meta,
    render_tender_content,
)

SAMPLE_META = {
    "worktree_name": "opus-shard-20260621-001",
    "branch_name": "shard/opus-20260621-001",
    "commits": 3,
    "files_modified": ["a.py", "b/c.py"],
    "status": "complete",
    "confidence": 8,
    "reviewer": "prime",
    "name": "Opus Security Architect",
}


def test_round_trip_recovers_meta():
    content = render_tender_content("## Tender\n\nSome summary.", SAMPLE_META)
    assert parse_tender_meta(content) == SAMPLE_META


def test_render_is_idempotent_through_parse():
    once = render_tender_content("body", SAMPLE_META)
    twice = render_tender_content("body", parse_tender_meta(once))
    assert once == twice


def test_body_is_preserved_and_human_readable():
    content = render_tender_content("## Tender: my-shard\n\nAdded auth checks", SAMPLE_META)
    assert "## Tender: my-shard" in content
    assert "Added auth checks" in content
    assert TENDER_META_MARKER in content


def test_hand_edit_to_prose_does_not_break_extraction():
    content = render_tender_content("## Tender\n\noriginal prose", SAMPLE_META)
    # A human edits the markdown body above the meta marker.
    marker_at = content.index(TENDER_META_MARKER)
    edited = (
        "## Tender\n\nHEAVILY rewritten prose by a human\nwith extra lines\n\n"
        + content[marker_at:]
    )
    assert parse_tender_meta(edited) == SAMPLE_META


def test_prose_containing_a_json_fence_is_not_mistaken_for_meta():
    body = '## Tender\n\nExample of config:\n```json\n{"not": "meta"}\n```\n'
    content = render_tender_content(body, SAMPLE_META)
    # The real meta (last block, behind the marker) wins, not the prose example.
    assert parse_tender_meta(content) == SAMPLE_META


def test_decoy_marker_in_body_does_not_shadow_real_meta():
    # The body is built from agent-authored text (e.g. a commit message) which
    # could itself contain the marker + a json fence. The real meta is always
    # appended LAST, so the parser must recover it, not the decoy in the prose.
    decoy = {"worktree_name": "WRONG", "confidence": 1, "status": "abandoned"}
    body = (
        "## Tender\n\nCommit message quoting an old tender:\n\n"
        + f"{TENDER_META_MARKER}\n```json\n"
        + '{"worktree_name": "WRONG", "confidence": 1, "status": "abandoned"}\n'
        + "```\n"
    )
    content = render_tender_content(body, SAMPLE_META)
    assert parse_tender_meta(content) == SAMPLE_META
    assert parse_tender_meta(content) != decoy


def test_unclosed_decoy_fence_in_body_does_not_swallow_real_meta():
    # A body whose prose contains the marker + an UNCLOSED ```json fence. A
    # left-to-right non-overlapping regex scan would let this decoy consume the
    # real (appended) block's closing fence and lose it; anchoring on the last
    # marker recovers the real meta.
    body = (
        "## Tender\n\nPasted an old half-tender:\n\n"
        + f"{TENDER_META_MARKER}\n```json\n"
        + '{"worktree_name": "WRONG", "confidence": 1\n'  # no closing fence
    )
    content = render_tender_content(body, SAMPLE_META)
    assert parse_tender_meta(content) == SAMPLE_META


def test_marker_string_inside_a_meta_value_is_not_mistaken_for_the_envelope():
    # A meta value (e.g. a free-text reviewer or an oddly named file) that itself
    # contains the literal marker must not shadow the envelope: the inner marker
    # sits inside the JSON and can't be followed by a real fence, so the parser
    # skips it and recovers the true meta.
    meta = dict(SAMPLE_META)
    meta["reviewer"] = f"weird {TENDER_META_MARKER} reviewer"
    meta["files_modified"] = [f"{TENDER_META_MARKER}.py"]
    content = render_tender_content("## Tender\n\nsummary", meta)
    assert parse_tender_meta(content) == meta


def test_missing_meta_returns_none():
    assert parse_tender_meta("just some prose, no meta block") is None
    assert parse_tender_meta("") is None
    assert parse_tender_meta(None) is None


def test_malformed_json_returns_none():
    broken = f"body\n\n{TENDER_META_MARKER}\n```json\n{{not valid json,}}\n```\n"
    assert parse_tender_meta(broken) is None


def test_non_object_json_returns_none():
    arr = f"body\n\n{TENDER_META_MARKER}\n```json\n[1, 2, 3]\n```\n"
    assert parse_tender_meta(arr) is None


def test_empty_meta_round_trips():
    content = render_tender_content("body", {})
    assert parse_tender_meta(content) == {}


# --- latest_tenders_by_worktree (the D3 consumer fix) -----------------------


def _tender_folio(worktree, confidence, created_at, content_hash, status="complete"):
    meta = {"worktree_name": worktree, "confidence": confidence, "status": status}
    return {
        "content_hash": content_hash,
        "created_at": created_at,
        "title": f"tender for {worktree}",
        "content": render_tender_content("body", meta),
    }


def test_newest_tender_wins_by_created_at():
    folios = [
        _tender_folio("wt-a", 3, "2026-06-21T10:00:00+00:00", "sha256::aaa"),
        _tender_folio("wt-a", 9, "2026-06-21T12:00:00+00:00", "sha256::bbb"),
        _tender_folio("wt-a", 5, "2026-06-21T11:00:00+00:00", "sha256::ccc"),
    ]
    latest = latest_tenders_by_worktree(folios)
    assert latest["wt-a"]["meta"]["confidence"] == 9
    assert latest["wt-a"]["content_hash"] == "sha256::bbb"


def test_selection_is_independent_of_input_order():
    folios = [
        _tender_folio("wt-a", 9, "2026-06-21T12:00:00+00:00", "sha256::bbb"),
        _tender_folio("wt-a", 3, "2026-06-21T10:00:00+00:00", "sha256::aaa"),
    ]
    # Reversed input must still pick the 12:00 tender.
    assert latest_tenders_by_worktree(folios)["wt-a"]["meta"]["confidence"] == 9
    assert latest_tenders_by_worktree(list(reversed(folios)))["wt-a"]["meta"]["confidence"] == 9


def test_created_at_ties_break_on_content_hash():
    same_time = "2026-06-21T10:00:00+00:00"
    folios = [
        _tender_folio("wt-a", 1, same_time, "sha256::aaa"),
        _tender_folio("wt-a", 2, same_time, "sha256::zzz"),
    ]
    # Higher content_hash wins the tie, matching the store's ordering.
    assert latest_tenders_by_worktree(folios)["wt-a"]["content_hash"] == "sha256::zzz"


def test_multiple_worktrees_kept_separate():
    folios = [
        _tender_folio("wt-a", 4, "2026-06-21T10:00:00+00:00", "sha256::a1"),
        _tender_folio("wt-b", 7, "2026-06-21T10:00:00+00:00", "sha256::b1"),
    ]
    latest = latest_tenders_by_worktree(folios)
    assert set(latest) == {"wt-a", "wt-b"}
    assert latest["wt-b"]["meta"]["confidence"] == 7


def test_folios_without_meta_are_skipped():
    folios = [
        {"content_hash": "sha256::x", "created_at": "t", "content": "no meta here"},
        _tender_folio("wt-a", 5, "2026-06-21T10:00:00+00:00", "sha256::a1"),
    ]
    latest = latest_tenders_by_worktree(folios)
    assert set(latest) == {"wt-a"}
