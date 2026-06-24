"""Tests for content-hash identity: created_at normalization and folio hashing.

The created_at normalization group is the most important here — it is the known
hazard (open-call #1 in brief-20260529-i6fy). Every wire encoding of the same
instant must collapse to one canonical representation before it enters the
canonical bytes, so the content hash is identical regardless of how created_at
arrived.
"""

from datetime import datetime, timedelta, timezone

import pytest

from skein.identity import (
    compute_folio_hash,
    normalize_created_at,
)


# A single logical instant expressed many ways.
INSTANT_FIELDS = {
    "type": "finding",
    "title": "the same folio",
    "content": "body text",
    "created_by": "mote-0529",
}

EAST = timezone(timedelta(hours=-4))


# --- created_at normalization (the headline hazard) -------------------------


SAME_INSTANT_INPUTS = [
    pytest.param(datetime(2026, 5, 29, 14, 47, 52), id="naive-datetime"),
    pytest.param(
        datetime(2026, 5, 29, 14, 47, 52, tzinfo=timezone.utc), id="aware-utc-datetime"
    ),
    pytest.param(
        datetime(2026, 5, 29, 10, 47, 52, tzinfo=EAST), id="aware-offset-datetime"
    ),
    pytest.param("2026-05-29T14:47:52", id="naive-isoformat-string"),
    pytest.param("2026-05-29T14:47:52Z", id="Z-suffixed-string"),
    pytest.param("2026-05-29T14:47:52+00:00", id="utc-offset-string"),
    pytest.param("2026-05-29T10:47:52-04:00", id="east-offset-string"),
    pytest.param("2026-05-29 14:47:52", id="raw-db-space-string"),
    pytest.param("2026-05-29T14:47:52.000000", id="explicit-zero-microseconds"),
    pytest.param("2026-05-29T14:47:52.000000+00:00", id="explicit-zero-micros-utc"),
]


@pytest.mark.parametrize("value", SAME_INSTANT_INPUTS)
def test_normalize_same_instant_collapses(value):
    """Every encoding of one instant normalizes to one canonical string."""
    expected = "2026-05-29T14:47:52+00:00"
    assert normalize_created_at(value) == expected


@pytest.mark.parametrize("value", SAME_INSTANT_INPUTS)
def test_hash_invariant_across_created_at_encodings(value):
    """The content hash is identical no matter how created_at arrived."""
    reference = compute_folio_hash({**INSTANT_FIELDS, "created_at": SAME_INSTANT_INPUTS[0].values[0]})
    assert compute_folio_hash({**INSTANT_FIELDS, "created_at": value}) == reference


def test_normalize_preserves_microseconds():
    """Sub-second precision is preserved (it is the corpus-wide uniqueness basis)."""
    assert (
        normalize_created_at(datetime(2026, 5, 29, 14, 47, 52, 123456))
        == "2026-05-29T14:47:52.123456+00:00"
    )


def test_normalize_microseconds_match_across_encodings():
    naive = datetime(2026, 5, 29, 14, 47, 52, 123456)
    string = "2026-05-29T14:47:52.123456"
    z_string = "2026-05-29T14:47:52.123456Z"
    east = "2026-05-29T10:47:52.123456-04:00"
    assert (
        normalize_created_at(naive)
        == normalize_created_at(string)
        == normalize_created_at(z_string)
        == normalize_created_at(east)
    )


def test_whole_second_and_microsecond_differ():
    """A whole-second instant and a sub-second instant are genuinely different."""
    assert normalize_created_at("2026-05-29T14:47:52") != normalize_created_at(
        "2026-05-29T14:47:52.000001"
    )


def test_normalize_none_passes_through():
    assert normalize_created_at(None) is None


def test_normalize_rejects_unparseable():
    with pytest.raises(ValueError):
        normalize_created_at("not a timestamp")


# --- hash determinism -------------------------------------------------------


def test_hash_is_deterministic_across_calls():
    fields = {**INSTANT_FIELDS, "created_at": "2026-05-29T14:47:52Z"}
    first = compute_folio_hash(fields)
    second = compute_folio_hash(fields)
    assert first == second


def test_hash_address_form():
    """The stored hash uses the sha256::<hex> address form."""
    h = compute_folio_hash({**INSTANT_FIELDS, "created_at": "2026-05-29T14:47:52Z"})
    assert h.startswith("sha256::")
    hexpart = h.split("::", 1)[1]
    assert len(hexpart) == 64
    assert all(c in "0123456789abcdef" for c in hexpart)


# --- five-field sensitivity -------------------------------------------------


BASE = {
    "type": "finding",
    "title": "title",
    "content": "content",
    "created_at": "2026-05-29T14:47:52Z",
    "created_by": "mote-0529",
}


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("type", "issue"),
        ("title", "different title"),
        ("content", "different content"),
        ("created_at", "2026-05-29T14:47:53Z"),
        ("created_by", "someone-else"),
    ],
)
def test_each_field_changes_hash(field, new_value):
    base_hash = compute_folio_hash(BASE)
    changed = {**BASE, field: new_value}
    assert compute_folio_hash(changed) != base_hash


def test_unrelated_fields_do_not_affect_hash():
    """Only the five canonical fields participate in identity."""
    base_hash = compute_folio_hash(BASE)
    with_noise = {**BASE, "status": "closed", "site_id": "whatever", "extra": 1}
    assert compute_folio_hash(with_noise) == base_hash
