"""Numeric boundary contract for signing models.

Oracle finding-20260512-sr0w actionable #3 called out unconstrained integers.
These tests pin microsecond timestamp policy and Rekor index/tree boundaries.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "skein.signing",
    reason="skein.signing is the phase-3 deliverable; contract collects but skips until then.",
)

from .conftest import signing  # noqa: E402

TS_2038_US = (2**31 - 1) * 1_000_000
TS_2106_US = (2**32 - 1) * 1_000_000
JS_SAFE_BOUNDARY_US = 2**53
INT64_MAX = 2**63 - 1
NORMAL_US = 1_767_225_600_000_000


def _make_rekor_proof(**overrides):
    payload = dict(
        log_index=0,
        tree_size=1,
        root_hash="AAECAwQFBgcICQ==",
        hashes=[],
        checkpoint="rekor.sigstore.dev - 1\nAAECAwQFBgcICQ==\n\n-- rekor.sigstore.dev sig\n",
        integrated_time=NORMAL_US,
        log_id="cmVrb3Ita2V5LWlk",
    )
    payload.update(overrides)
    return signing.RekorInclusionProof(**payload)


def _make_sign_result(**overrides):
    payload = dict(
        bundle_json='{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}',
        issuer="https://accounts.google.com",
        subject="alice@example.com",
        signing_timestamp=NORMAL_US,
        evidence=signing.Evidence(),
    )
    payload.update(overrides)
    return signing.SignResult(**payload)


def _make_provider(**overrides):
    payload = dict(
        issuer="https://accounts.google.com",
        token="header.payload.signature",
        provider_id="google",
        expires_at=NORMAL_US + 600_000_000,
    )
    payload.update(overrides)
    return signing.OIDCProviderConfig(**payload)


def _expect_validation_error(fn):
    with pytest.raises(Exception):
        fn()


# Enforces: finding-20260512-sr0w actionable #3. SKEIN timestamps are
# microsecond UTC since Unix epoch; negative is pre-1970 and invalid here.
@pytest.mark.parametrize(
    ("field", "factory"),
    [
        ("signing_timestamp", _make_sign_result),
        ("expires_at", _make_provider),
        ("integrated_time", _make_rekor_proof),
    ],
)
def test_timestamp_fields_reject_negative(field, factory):
    _expect_validation_error(lambda: factory(**{field: -1}))


# Enforces: finding-20260512-sr0w actionable #3. Zero means the Unix epoch, which
# predates the Sigstore/SKEIN signing context and is invalid for these fields.
@pytest.mark.parametrize(
    ("field", "factory"),
    [
        ("signing_timestamp", _make_sign_result),
        ("expires_at", _make_provider),
        ("integrated_time", _make_rekor_proof),
    ],
)
def test_timestamp_fields_reject_zero(field, factory):
    _expect_validation_error(lambda: factory(**{field: 0}))


# Enforces: 2038 boundary. Microseconds are 64-bit-domain values; a 32-bit Unix
# seconds overflow point is valid when represented as microseconds.
@pytest.mark.parametrize(
    ("field", "factory"),
    [
        ("signing_timestamp", _make_sign_result),
        ("expires_at", _make_provider),
        ("integrated_time", _make_rekor_proof),
    ],
)
def test_timestamp_fields_at_2038_seconds_unix_overflow_microseconds_ok(field, factory):
    assert getattr(factory(**{field: TS_2038_US}), field) == TS_2038_US


# Enforces: 2106 boundary. Unsigned 32-bit Unix seconds overflow is also valid
# when represented as a microsecond timestamp.
@pytest.mark.parametrize(
    ("field", "factory"),
    [
        ("signing_timestamp", _make_sign_result),
        ("expires_at", _make_provider),
        ("integrated_time", _make_rekor_proof),
    ],
)
def test_timestamp_fields_at_2106_seconds_unsigned_unix_overflow_microseconds_ok(
    field, factory
):
    assert getattr(factory(**{field: TS_2106_US}), field) == TS_2106_US


# Enforces: JS Number precision boundary. Contract choice: accept and preserve
# the exact integer through Python/Pydantic JSON; JS clients must not coerce this
# to Number without accepting precision loss.
@pytest.mark.parametrize(
    ("field", "factory", "model"),
    [
        ("signing_timestamp", _make_sign_result, signing.SignResult),
        ("expires_at", _make_provider, signing.OIDCProviderConfig),
        ("integrated_time", _make_rekor_proof, signing.RekorInclusionProof),
    ],
)
def test_timestamp_fields_at_2_to_53_js_safe_boundary_round_trip_exact(
    field, factory, model
):
    instance = factory(**{field: JS_SAFE_BOUNDARY_US})
    decoded = model.model_validate_json(instance.model_dump_json())
    assert getattr(decoded, field) == JS_SAFE_BOUNDARY_US


# Enforces: Python can represent int64 max exactly; the contract pins JSON
# round-trip preservation even though downstream non-Python clients need care.
@pytest.mark.parametrize(
    ("field", "factory", "model"),
    [
        ("signing_timestamp", _make_sign_result, signing.SignResult),
        ("expires_at", _make_provider, signing.OIDCProviderConfig),
        ("integrated_time", _make_rekor_proof, signing.RekorInclusionProof),
    ],
)
def test_timestamp_fields_at_2_to_63_int64_max_round_trip_exact(field, factory, model):
    instance = factory(**{field: INT64_MAX})
    decoded = model.model_validate_json(instance.model_dump_json())
    assert getattr(decoded, field) == INT64_MAX


# Enforces: seconds-vs-microseconds confusion is rejected for sign-side surfaced
# timestamps. 1_700_000_000 is plausible seconds but far too small for current
# microsecond UTC timestamps.
@pytest.mark.parametrize(
    ("field", "factory"),
    [
        ("signing_timestamp", _make_sign_result),
        ("expires_at", _make_provider),
        ("integrated_time", _make_rekor_proof),
    ],
)
def test_timestamp_fields_reject_seconds_vs_microseconds_confusion(field, factory):
    _expect_validation_error(lambda: factory(**{field: 1_700_000_000}))


# Enforces: finding-20260512-sr0w actionable #3. Rekor log_index is zero-based,
# but it cannot be negative.
def test_log_index_rejects_negative():
    _expect_validation_error(lambda: _make_rekor_proof(log_index=-1))


# Enforces: the first Rekor entry is log_index 0.
def test_log_index_zero_allowed():
    assert _make_rekor_proof(log_index=0, tree_size=1).log_index == 0


# Enforces: an entry index must fit inside the tree at integration time.
def test_log_index_must_be_less_than_tree_size():
    _expect_validation_error(lambda: _make_rekor_proof(log_index=5, tree_size=5))
    _expect_validation_error(lambda: _make_rekor_proof(log_index=5, tree_size=3))


# Enforces: tree_size is a count and cannot be negative.
def test_tree_size_rejects_negative():
    _expect_validation_error(lambda: _make_rekor_proof(tree_size=-1))


# Enforces: an empty Rekor tree cannot contain a logged entry.
def test_tree_size_zero_invalid():
    _expect_validation_error(lambda: _make_rekor_proof(log_index=0, tree_size=0))


# Enforces: a single-entry tree is the smallest valid inclusion proof context.
def test_tree_size_one_allowed():
    assert _make_rekor_proof(log_index=0, tree_size=1).tree_size == 1


# Enforces: large Rekor counters preserve exact values through JSON. Unlike
# timestamps, log_index/tree_size are counters, so JS precision warnings apply
# but the Python contract is exact preservation.
@pytest.mark.parametrize("field", ["log_index", "tree_size"])
@pytest.mark.parametrize(
    "value", [TS_2038_US, TS_2106_US, JS_SAFE_BOUNDARY_US, INT64_MAX]
)
def test_rekor_counter_large_boundaries_round_trip_exact(field, value):
    kwargs = {field: value}
    if field == "log_index":
        kwargs["tree_size"] = value + 1
    else:
        kwargs["log_index"] = 0
    proof = _make_rekor_proof(**kwargs)
    decoded = signing.RekorInclusionProof.model_validate_json(proof.model_dump_json())
    assert getattr(decoded, field) == value
