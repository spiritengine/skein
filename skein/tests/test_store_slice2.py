"""Tests for Slice 2 store extensions: batch transactions, slugs, and the
unresolved-endpoint query the import report relies on."""

import pytest

from skein.store import SkeinNextStore


@pytest.fixture
def store(tmp_path):
    s = SkeinNextStore(data_dir=tmp_path / ".skein")
    yield s
    s.close()


FOLIO = {
    "type": "finding",
    "title": "a finding",
    "content": "what was discovered",
    "created_at": "2026-05-29T14:47:52Z",
    "created_by": "mote-0529",
}


# --- batch / transaction ----------------------------------------------------


def test_transaction_persists_all_writes(store):
    with store.transaction():
        for i in range(50):
            store.create_folio({**FOLIO, "title": f"f{i}"})
    assert store.count_folios() == 50


def test_transaction_rolls_back_on_error(store):
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.create_folio(FOLIO)
            raise RuntimeError("boom")
    # nothing committed
    assert store.count_folios() == 0


def test_transaction_idempotent_inside_batch(store):
    with store.transaction():
        store.create_folio(FOLIO)
        store.create_folio(FOLIO)
    assert store.count_folios() == 1


def test_writes_outside_transaction_still_commit(store):
    store.create_folio(FOLIO)
    # a fresh connection to the same file sees it -> it was committed
    other = SkeinNextStore(data_dir=store.data_dir)
    try:
        assert other.count_folios() == 1
    finally:
        other.close()


def test_nested_transaction_is_rejected(store):
    with store.transaction():
        with pytest.raises(RuntimeError):
            with store.transaction():
                pass


# --- slugs ------------------------------------------------------------------


def test_slug_set_and_resolve(store):
    h = store.create_folio({**FOLIO, "type": "site"})
    store.set_slug("skein-mesh", h)
    assert store.resolve_slug("skein-mesh") == h


def test_resolve_unknown_slug_returns_none(store):
    assert store.resolve_slug("nope") is None


def test_slug_upsert(store):
    h1 = store.create_folio({**FOLIO, "title": "site one"})
    h2 = store.create_folio({**FOLIO, "title": "site two"})
    store.set_slug("s", h1)
    store.set_slug("s", h2)
    assert store.resolve_slug("s") == h2


def test_list_slugs(store):
    h = store.create_folio({**FOLIO, "type": "site"})
    store.set_slug("a", h)
    store.set_slug("b", h)
    got = dict(store.list_slugs())
    assert got == {"a": h, "b": h}


# --- unresolved endpoints ---------------------------------------------------


def test_unresolved_endpoints_lists_only_unresolved_legacy_ids(store):
    folio_hash = store.create_folio(FOLIO)
    other_hash = store.create_folio({**FOLIO, "title": "other"})
    # a resolved folio edge: both endpoints are real hashes -> not unresolved
    store.save_thread(from_id=folio_hash, to_id=other_hash, type="reference")
    # a dangling legacy-id endpoint with no alias -> unresolved
    store.save_thread(from_id=folio_hash, to_id="brief-20260101-dang", type="mention")
    # a cross-project colon ref -> unresolved
    store.save_thread(from_id="otherproj:brief-20260101-xprj", to_id=folio_hash,
                      type="reference")
    # an actor endpoint was dropped to weaver during import, so it is NULL here
    store.save_thread(from_id=None, to_id=folio_hash, type="message", weaver="bob")

    got = set(store.unresolved_endpoints())
    assert got == {"brief-20260101-dang", "otherproj:brief-20260101-xprj"}


def test_unresolved_endpoint_resolves_once_alias_exists(store):
    folio_hash = store.create_folio(FOLIO)
    store.save_thread(from_id=folio_hash, to_id="brief-20260101-late", type="mention")
    assert "brief-20260101-late" in store.unresolved_endpoints()
    # the target later imports and registers its alias
    target = store.create_folio({**FOLIO, "title": "late arrival"})
    store.set_alias("brief-20260101-late", target)
    assert "brief-20260101-late" not in store.unresolved_endpoints()


# --- counts -----------------------------------------------------------------


def test_counts(store):
    assert store.count_folios() == 0
    assert store.count_threads() == 0
    h = store.create_folio(FOLIO)
    store.save_thread(from_id=h, to_id=h, type="status", content="closed")
    assert store.count_folios() == 1
    assert store.count_threads() == 1
