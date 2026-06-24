"""sign() contract — happy path, size edges, Unicode-heavy bytes, non-ASCII
identity, ephemeral key non-reuse, expires_at gating, library regression coverage.

sign() is the write path. A bug here either silently produces unverifiable
bundles (corrupts the entire SKEIN write history) or stranger: produces a
valid bundle that wasn't what the user thought they were signing. Cross-payload
binding is the load-bearing property.

Per clarification 1: sign() returns a SignResult (Pydantic model with
bundle_json, issuer, subject, signing_timestamp, evidence).
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import utils

pytest.importorskip(
    "skein.signing",
    reason="skein.signing is the phase-3 deliverable; contract collects but skips until then.",
)

from .conftest import HAS_FUNCTIONS, make_jwt_token, signing  # noqa: E402

pytestmark = pytest.mark.skipif(
    not HAS_FUNCTIONS,
    reason="signing.sign/verify/verify_multi are Phase 3 deliverables",
)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


# Enforces: sign() returns a SignResult whose bundle_json round-trips through
# verify() against the same canonical_bytes.
def test_sign_happy_path_returns_verifiable_result(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    result = signing.sign(canonical_bytes_simple, google_provider)
    assert isinstance(result, signing.SignResult)

    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.VERIFIED


# Enforces: SignResult exposes (issuer, subject, signing_timestamp) extracted
# at sign time per Identity rev 5 §"Folio schema additions § indexing/display".
def test_sign_exposes_indexed_identity_fields(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        identity="alice@example.com",
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    assert result.subject == "alice@example.com"
    assert result.issuer == "https://accounts.google.com"
    assert result.signing_timestamp > 0  # microseconds UTC


# Enforces: SignResult.signing_timestamp comes from Rekor's RFC3161 TSA timestamp
# (Rekor v2), not client clock. The exact value is implementation-defined but it
# must be in microseconds.
def test_sign_signing_timestamp_is_microseconds(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    result = signing.sign(canonical_bytes_simple, google_provider)
    # > 1e15 microseconds = > year 2001 in microseconds
    assert result.signing_timestamp > 1_000_000_000_000_000


# Enforces: SignResult.evidence is populated on success (carries validity_window,
# rekor_inclusion, cert_chain_summary).
def test_sign_evidence_populated(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    result = signing.sign(canonical_bytes_simple, google_provider)
    assert result.evidence is not None


# Enforces (j4w4 round-7 oracle A3, sign-path parallel): when Rekor returns an
# integrated_time at or below the MIN_MICROSECOND_TIMESTAMP floor, sign() falls
# back to cert notBefore for SignResult.signing_timestamp. The fallback used
# to be silent — operators could not tell whether a low signing_timestamp was
# a Rekor anomaly or a legitimate cert notBefore. The fix emits a WARNING
# with a grep-able prefix so the substitution is observable. Mirrors
# test_build_rekor_inclusion_proof_warns_on_zero_integrated_time in
# test_bundle_proof_boundaries.py for the verify-side inclusion proof builder.
def test_sign_warns_on_zero_integrated_time(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch, caplog
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        integrated_time_s=0,
    )
    with caplog.at_level("WARNING"):
        result = signing.sign(canonical_bytes_simple, google_provider)
    # Floor substitution kicked in: signing_timestamp is now cert notBefore
    # (the (nb, na) tuple from _cert_validity_window), which is non-zero.
    assert result.signing_timestamp > 0
    assert result.signing_timestamp == result.evidence.validity_window[0]
    sign_floor_records = [
        r
        for r in caplog.records
        if "signing.sign_integrated_time_below_floor" in r.getMessage()
    ]
    assert sign_floor_records, (
        "expected at least one WARNING log with "
        "'signing.sign_integrated_time_below_floor' prefix; "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )
    assert any(r.levelname == "WARNING" for r in sign_floor_records)
    # Log must include the below-floor value for triage.
    assert any("0" in r.getMessage() for r in sign_floor_records)
    # Pin the intentional dual-fire contract (see signing.sign() comment at
    # the sign-side floor substitution): one below-floor integrated_time from
    # Rekor produces TWO distinct WARNs — one from _build_rekor_inclusion_proof
    # (proof-side substitution to MIN_MICROSECOND_TIMESTAMP) and one from
    # sign() itself (sign-side substitution to cert notBefore). The fell
    # observed this contract was not test-enforced; without this assertion a
    # future de-dup that collapsed the two into one would pass silently and
    # erase the distinction between the two substitutions.
    rekor_floor_records = [
        r
        for r in caplog.records
        if "signing.rekor_integrated_time_below_floor" in r.getMessage()
    ]
    assert len(rekor_floor_records) == 1, (
        "expected exactly one 'signing.rekor_integrated_time_below_floor' "
        "WARNING (proof-side floor substitution); "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )
    assert len(sign_floor_records) == 1, (
        "expected exactly one 'signing.sign_integrated_time_below_floor' "
        "WARNING (sign-side floor substitution); "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )


# Sanity: when integrated_time is in the normal range, no warning fires.
def test_sign_does_not_warn_on_normal_integrated_time(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch, caplog
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    with caplog.at_level("WARNING"):
        signing.sign(canonical_bytes_simple, google_provider)
    assert not any(
        "signing.sign_integrated_time_below_floor" in r.getMessage()
        for r in caplog.records
    )


# Enforces: sign() preserves canonical_bytes byte-exactly. No re-canonicalization,
# re-normalization, or re-encoding. The signature binds to the bytes the caller
# passed.
def test_sign_does_not_mutate_canonical_bytes(
    crypto_factory, google_provider, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    original = bytes(range(256))
    sentinel = bytes(original)
    _ = signing.sign(original, google_provider)
    assert original == sentinel


# Enforces: sign() produces one SignResult per call. The caller assembles
# multi-signer arrays from N sign() calls. Per Identity rev 5 §boundary:
# "sign() produces one SignResult (one canonical sigstore-bundle JSON string
# in result.bundle_json); caller assembles the array."
def test_sign_produces_one_result_per_call(
    crypto_factory,
    google_provider,
    github_provider,
    canonical_bytes_simple,
    monkeypatch,
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    r1 = signing.sign(canonical_bytes_simple, google_provider)
    r2 = signing.sign(canonical_bytes_simple, github_provider)
    assert isinstance(r1, signing.SignResult)
    assert isinstance(r2, signing.SignResult)


# ---------------------------------------------------------------------------
# Edge cases — size
# ---------------------------------------------------------------------------


# Enforces: sign() accepts empty canonical_bytes. (Defensively: empty is a
# knurl/canon concern, but sign() must not gatekeep size.)
def test_sign_empty_canonical_bytes(
    crypto_factory, google_provider, canonical_bytes_empty, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    result = signing.sign(canonical_bytes_empty, google_provider)
    assert isinstance(result, signing.SignResult)


# Enforces: sign() accepts 1-byte canonical_bytes.
def test_sign_one_byte_canonical_bytes(
    crypto_factory, google_provider, canonical_bytes_one_byte, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    result = signing.sign(canonical_bytes_one_byte, google_provider)
    assert isinstance(result, signing.SignResult)


# Enforces: sign() handles a 1 MiB payload without erroring out.
def test_sign_one_mb_canonical_bytes(
    crypto_factory, google_provider, canonical_bytes_one_mb, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    result = signing.sign(canonical_bytes_one_mb, google_provider)
    assert isinstance(result, signing.SignResult)


# ---------------------------------------------------------------------------
# Edge cases — Unicode
# ---------------------------------------------------------------------------


# Enforces: sign() accepts Unicode-heavy canonical_bytes. Catches the accidental
# .decode().encode() bug class — signature binds to bytes the caller passed.
def test_sign_unicode_heavy_canonical_bytes(
    crypto_factory, google_provider, canonical_bytes_unicode_heavy, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    result = signing.sign(canonical_bytes_unicode_heavy, google_provider)
    assert isinstance(result, signing.SignResult)


# Enforces: regression for sigstore-python #1507. Non-ASCII in the OIDC identity
# claim must NOT cause a silent malformed CSR. signing.py either succeeds with
# the non-ASCII identity OR raises SigningUnavailable with component="fulcio"
# and a reason that names the issue. Either is acceptable; what's unacceptable
# is a silently malformed CSR or a generic Exception.
def test_sign_non_ascii_identity_raises_structured(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        identity="josé@example.com",
        fulcio_rejects_non_ascii=True,
    )
    try:
        signing.sign(canonical_bytes_simple, google_provider)
    except signing.SigningUnavailable as e:
        assert e.component == "fulcio"
        assert "identity" in e.reason.lower() or "csr" in e.reason.lower()
    # If signing.py has worked around the upstream bug, no exception is fine too.


# ---------------------------------------------------------------------------
# Non-determinism / non-reuse
# ---------------------------------------------------------------------------


# Enforces: two sign() calls over the same canonical_bytes with the same provider
# produce TWO DISTINCT bundles. Sigstore signing is non-deterministic (fresh
# ephemeral key + ECDSA nonce per call). This is the load-bearing property
# behind the verify-cache key being (content_hash, bundle_hash).
#
# K-A9: staging-only. Under the offline default both signings hit the
# synthetic _FakeSigner, which fabricates a fresh keypair/serial/log_index
# every call — bundles always differ by construction, so the mocked form
# cannot catch real ECDSA nonce reuse. Only real sigstore-python signing
# (SKEIN_TEST_SIGSTORE_LIVE=1) exercises the actual non-determinism. The
# synthetic limitation is documented at skein/signing.py sign(); the Phase-4
# real-signing audit is brief-20260514-me2x §2.
@pytest.mark.staging
def test_sign_is_non_deterministic(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    a = signing.sign(canonical_bytes_simple, google_provider)
    b = signing.sign(canonical_bytes_simple, google_provider)
    _assert_sign_results_differ_per_field(a.bundle_json, b.bundle_json)


def _find_first(node, key):
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _find_first(value, key)
            if found is not None:
                return found
    if isinstance(node, list):
        for item in node:
            found = _find_first(item, key)
            if found is not None:
                return found
    return None


def _assert_different(field: str, left, right):
    assert left != right, (
        f"sign() non-determinism regression: {field} unexpectedly constant"
    )


def _assert_sign_results_differ_per_field(bundle_a: str, bundle_b: str) -> None:
    obj_a = json.loads(bundle_a)
    obj_b = json.loads(bundle_b)

    sig_a = base64.b64decode(_find_first(obj_a, "signature"))
    sig_b = base64.b64decode(_find_first(obj_b, "signature"))
    rs_a = utils.decode_dss_signature(sig_a)
    rs_b = utils.decode_dss_signature(sig_b)
    _assert_different("ecdsa_signature_r_s", rs_a, rs_b)

    cert_a = x509.load_der_x509_certificate(
        base64.b64decode(_find_first(obj_a, "rawBytes"))
    )
    cert_b = x509.load_der_x509_certificate(
        base64.b64decode(_find_first(obj_b, "rawBytes"))
    )
    _assert_different("cert_serial_number", cert_a.serial_number, cert_b.serial_number)
    _assert_different(
        "cert_public_key",
        cert_a.public_key().public_numbers(),
        cert_b.public_key().public_numbers(),
    )
    _assert_different(
        "cert_validity_window",
        (cert_a.not_valid_before_utc, cert_a.not_valid_after_utc),
        (cert_b.not_valid_before_utc, cert_b.not_valid_after_utc),
    )

    _assert_different(
        "rekor_log_index",
        _find_first(obj_a, "logIndex"),
        _find_first(obj_b, "logIndex"),
    )
    # Phase-3 amendment: bundle v0.3 emits "integratedTime" (not a bare
    # "timestamp" field) on each tlog entry. The K-A9 property — that
    # signing-time evidence differs across signings — is unchanged.
    _assert_different(
        "tlog_integrated_time",
        _find_first(obj_a, "integratedTime"),
        _find_first(obj_b, "integratedTime"),
    )


# Enforces: both bundles verify against the same canonical_bytes. Non-determinism
# at the bundle layer must not break verification.
def test_sign_non_deterministic_both_verify(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    a = signing.sign(canonical_bytes_simple, google_provider)
    b = signing.sign(canonical_bytes_simple, google_provider)
    sb_a = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[a.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    sb_b = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[b.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    assert (
        signing.verify(canonical_bytes_simple, sb_a).status
        == signing.VerifyStatus.VERIFIED
    )
    assert (
        signing.verify(canonical_bytes_simple, sb_b).status
        == signing.VerifyStatus.VERIFIED
    )


# ---------------------------------------------------------------------------
# Boundary contract — sign() raises SigningUnavailable, not raw errors
# ---------------------------------------------------------------------------


# Enforces: when Fulcio returns 5xx, sign() raises SigningUnavailable with
# component="fulcio". The wrapper MUST NOT let raw FulcioClientError leak —
# that breaks the boundary contract with sign_queue.py.
def test_sign_fulcio_failure_raises_signing_unavailable(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        fulcio_status=503,
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert exc.value.component == "fulcio"


# Enforces: when Rekor returns 5xx, sign() raises SigningUnavailable with
# component="rekor". Half-state contract: the caller has no half-signed folio.
def test_sign_rekor_failure_raises_signing_unavailable(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        rekor_status=502,
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert exc.value.component == "rekor"


# Enforces: when OIDC token acquisition fails, sign() raises SigningUnavailable
# with component="oidc".
def test_sign_oidc_failure_raises_signing_unavailable(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        oidc_status="unreachable",
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert exc.value.component == "oidc"


# Enforces: network-down (all three unreachable) is also SigningUnavailable,
# not a raw OSError / ConnectionError.
def test_sign_total_network_down_raises_signing_unavailable(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        network="down",
    )
    with pytest.raises(signing.SigningUnavailable):
        signing.sign(canonical_bytes_simple, google_provider)


# ---------------------------------------------------------------------------
# expires_at — sign-side OIDC token expiry check (clarification 3)
# ---------------------------------------------------------------------------


# Enforces: per clarification 3, if expires_at is in the past or imminent,
# sign() raises SigningUnavailable with a clear "OIDC token expired" reason
# BEFORE talking to Fulcio.
def test_sign_expired_token_raises_signing_unavailable_before_fulcio(
    crypto_factory, expired_google_provider, canonical_bytes_simple, monkeypatch
):
    # Even if Fulcio is healthy, the expired token check fires first.
    crypto_factory.install_sign_monkeypatch(
        monkeypatch, provider=expired_google_provider
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, expired_google_provider)
    assert exc.value.component == "oidc"
    assert "expir" in exc.value.reason.lower() or "token" in exc.value.reason.lower()


# Enforces: when expires_at is None (token-flow that doesn't surface expiry),
# sign() proceeds normally and lets Fulcio reject if the token is actually bad.
def test_sign_no_expires_at_proceeds_normally(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    assert google_provider.expires_at is None
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    result = signing.sign(canonical_bytes_simple, google_provider)
    assert isinstance(result, signing.SignResult)


# Enforces: future expires_at allows signing to proceed.
def test_sign_future_expires_at_proceeds(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    far_future = 4_102_444_800_000_000  # 2100-01-01 UTC microseconds
    provider = signing.OIDCProviderConfig(
        issuer="https://accounts.google.com",
        token=make_jwt_token(aud="sigstore", issuer="https://accounts.google.com"),
        provider_id="google",
        expires_at=far_future,
    )
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)
    result = signing.sign(canonical_bytes_simple, provider)
    assert isinstance(result, signing.SignResult)


# ---------------------------------------------------------------------------
# Library upstream regressions
# ---------------------------------------------------------------------------


# Enforces: regression for sigstore-python #1729 (Signer does not refresh
# expired certs mid-batch). If the cached cert expires between sign() calls in
# a session, sign() either refreshes transparently OR raises SigningUnavailable
# with component="fulcio". Bare ExpiredCertificate must NOT leak.
def test_sign_expired_cert_does_not_leak_raw_exception(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        cert_expired_on_second_call=True,
    )
    _ = signing.sign(canonical_bytes_simple, google_provider)
    try:
        signing.sign(canonical_bytes_simple, google_provider)
    except signing.SigningUnavailable:
        pass
    except Exception as e:
        pytest.fail(
            f"sign() leaked upstream {type(e).__name__}; should be SigningUnavailable"
        )


# Enforces: in-process parallel sign() calls must not leak raw upstream
# exceptions. This is narrower than sigstore-python #1403, which is covered by
# the subprocess/shared-cache staging test below.
def test_sign_parallel_calls_within_one_process_are_safe(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        simulate_tuf_race=True,
    )
    results: list = []
    errors: list = []

    def worker():
        try:
            results.append(signing.sign(canonical_bytes_simple, google_provider))
        except signing.SigningUnavailable as e:
            errors.append(e)
        except Exception as e:
            errors.append(("RAW", e))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    raw_errors = [e for e in errors if isinstance(e, tuple) and e[0] == "RAW"]
    assert raw_errors == [], f"TUF race surfaced raw exceptions: {raw_errors}"


# Enforces: oracle actionable #5 / sigstore-python #1403. The production race is
# across processes sharing one TUF cache, so this live staging contract launches
# two independent Python interpreters with the same SIGSTORE_TUF_CACHE_DIR.
@pytest.mark.staging
def test_sign_safe_across_processes_with_shared_tuf_cache(tmp_path):
    token = os.environ.get("SKEIN_TEST_SIGSTORE_TOKEN")
    if not token:
        pytest.skip("SKEIN_TEST_SIGSTORE_TOKEN not set for live staging sign test.")

    cache_dir = tmp_path / "shared-tuf-cache"
    # Subprocesses route signing.sign() through the staging Sigstore environment
    # by rebinding the production-context builder to its staging sibling before
    # the call. The parent-process monkeypatch fixture would not propagate here
    # — each subprocess imports skein.signing fresh — so the patch lives inside
    # the script body. Same shape as the in-process K-A9 interactive variant
    # (test_sign_ecdsa_nonce_not_reused_interactive). Not a mock: only trust
    # config selection is overridden; sigstore-python's signing path runs in
    # full.
    script = """
import json
import os
from skein import signing

signing._build_production_signing_context = signing._build_staging_signing_context

provider = signing.OIDCProviderConfig(
    issuer=os.environ["SKEIN_TEST_SIGSTORE_ISSUER"],
    token=os.environ["SKEIN_TEST_SIGSTORE_TOKEN"],
    provider_id=os.environ.get("SKEIN_TEST_SIGSTORE_PROVIDER_ID", "google"),
)
result = signing.sign(b"skein batch 6 tuf process race", provider)
print(result.model_dump_json())
"""
    env = os.environ.copy()
    env["SIGSTORE_TUF_CACHE_DIR"] = str(cache_dir)
    env.setdefault("SKEIN_TEST_SIGSTORE_ISSUER", "https://oauth2.sigstage.dev/auth")

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        for _ in range(2)
    ]
    runs = []
    try:
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=180)
            runs.append(
                subprocess.CompletedProcess(
                    proc.args,
                    proc.returncode,
                    stdout=stdout,
                    stderr=stderr,
                )
            )
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()

    assert [run.returncode for run in runs] == [0, 0], [
        {"stdout": run.stdout, "stderr": run.stderr, "returncode": run.returncode}
        for run in runs
    ]
    for run in runs:
        result = signing.SignResult.model_validate_json(
            run.stdout.strip().splitlines()[-1]
        )
        assert json.loads(result.bundle_json)

    leftovers = [
        p
        for p in cache_dir.rglob("*")
        if p.name.endswith((".tmp", ".lock")) or ".tmp" in p.name
    ]
    assert leftovers == []


def _extract_rs(bundle_json: str) -> tuple[int, int]:
    """Decode the ECDSA (r, s) from a signed bundle's signature."""
    obj = json.loads(bundle_json)
    sig = base64.b64decode(_find_first(obj, "signature"))
    return utils.decode_dss_signature(sig)


# K-A9: real ECDSA nonce non-reuse. Security-load-bearing — the offline suite
# structurally cannot provide it (test_sign_is_non_deterministic installs
# _FakeSigner, which fabricates a fresh keypair every call, so bundles differ
# by construction and a real nonce collision cannot be observed). This test
# drives real sigstore-python signing N times against staging Sigstore and
# asserts every ECDSA r is distinct.
#
# Why r-distinctness and not just (r, s) inequality: the catastrophic
# nonce-reuse failure mode is a *repeated r with differing s* (two signatures
# sharing a nonce k → private-key recovery). Pairwise (r, s) inequality passes
# in that case because s differs; only r-distinctness catches it. r is the
# x-coordinate of k·G mod n — a function of the per-signature nonce k and the
# curve alone, independent of the signing key — so r varies across signings
# precisely because a fresh random k is drawn each time (a fresh ephemeral
# keypair does not affect r). An r collision over N signings therefore
# indicates a broken/deterministic nonce RNG — exactly what K-A9 must rule out.
#
# Execution shape: human-in-the-loop OIDC — sigstore-python's Issuer pops a
# browser, the user authenticates with Google or GitHub via the Sigstore Dex
# broker, and the returned IdentityToken drives real signing. Marked
# @interactive (skipped unless --run-interactive is passed); never runs in CI
# by design. K-A9 was rescoped from CI nightly to a one-time launch-readiness
# empirical artifact (closure: finding-20260520-gidv); a prior non-interactive
# @pytest.mark.staging sibling was removed in the cleanup that consolidated
# K-A9 onto this path (finding-20260520-16nx).
#
# Run it with:
#   pytest tests/test_signing/test_sign.py -s --run-interactive \
#       -k nonce_not_reused_interactive
# (-s is recommended so the browser-prompt diagnostics aren't captured.)
#
# Secondary value: token issuer (Dex broker URL) and federated identity (the
# underlying Google/GitHub) are printed to stderr before signing.sign() is
# invoked, so the run yields diagnostic value about what the standard Sigstore
# interactive flow returns and whether SKEIN's v0 OIDC allowlist admits it.
@pytest.mark.interactive
def test_sign_ecdsa_nonce_not_reused_interactive(canonical_bytes_simple, monkeypatch):
    from sigstore.oidc import Issuer

    # Drive signing.sign() through the staging Sigstore environment rather
    # than production. The token we obtain via sigstore-python's interactive
    # flow is issued by staging Dex (oauth2.sigstage.dev); pairing it with
    # production Fulcio yields a 400 Bad Request because prod Fulcio doesn't
    # trust the staging issuer. This is NOT a mock of sign() — the underlying
    # sigstore-python code path is fully real; only the trust config selection
    # is overridden. Same parametrization shape the verify side uses via
    # _conformance_staging_verifier.
    monkeypatch.setattr(
        "skein.signing._build_production_signing_context",
        signing._build_staging_signing_context,
    )

    # SKEIN_TEST_SIGSTORE_ISSUER_URL is the OIDC PROVIDER base_url consumed by
    # sigstore.oidc.Issuer (it locates the .well-known/openid-configuration);
    # distinct from SKEIN_TEST_SIGSTORE_ISSUER in the staging variant above,
    # which is the issuer CLAIM passed into OIDCProviderConfig.issuer.
    issuer_url = os.environ.get(
        "SKEIN_TEST_SIGSTORE_ISSUER_URL", "https://oauth2.sigstage.dev/auth"
    )
    # OAuth2 authorization-code flow: localhost-redirect by default (opens
    # browser, redirects to localhost:<port>?code=...). force_oob=True falls
    # back to the out-of-band copy/paste flow for headless terminals.
    force_oob = os.environ.get("SKEIN_TEST_SIGSTORE_OIDC_OOB") == "1"

    print(
        f"\n[K-A9 interactive] requesting OIDC token from {issuer_url} "
        f"(force_oob={force_oob}); a browser tab will open — sign in with "
        f"Google or GitHub.",
        file=sys.stderr,
        flush=True,
    )
    issuer = Issuer(issuer_url)
    identity = issuer.identity_token(force_oob=force_oob)
    raw_token = str(identity)

    # Diagnostics: surface what the Sigstore flow actually returned, so a
    # subsequent allowlist rejection is interpretable without a second run.
    # We parse the JWT payload directly rather than relying on IdentityToken
    # private internals (urlsafe b64, pad to length-mod-4).
    payload_b64 = raw_token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    jwt_iss = payload.get("iss")
    federated_issuer = getattr(identity, "federated_issuer", None)
    print(
        f"[K-A9 interactive] token iss={jwt_iss!r} aud={payload.get('aud')!r} "
        f"sub={payload.get('sub')!r} federated_issuer={federated_issuer!r}",
        file=sys.stderr,
        flush=True,
    )

    try:
        n = int(os.environ.get("SKEIN_TEST_SIGSTORE_N", "5"))
    except ValueError:
        n = 5
    n = min(max(n, 2), 50)

    # Pass the literal JWT iss claim as the OIDCProviderConfig.issuer. If the
    # v0 allowlist rejects it, the failure surfaces exactly what the standard
    # Sigstore interactive flow actually issues — the empirical answer the
    # test exists to produce.
    provider = signing.OIDCProviderConfig(
        issuer=jwt_iss,
        token=raw_token,
        provider_id=os.environ.get("SKEIN_TEST_SIGSTORE_PROVIDER_ID", "sigstore-dex"),
    )

    pairs: list[tuple[int, int]] = []
    for i in range(n):
        print(
            f"[K-A9 interactive] signing {i + 1}/{n}…",
            file=sys.stderr,
            flush=True,
        )
        result = signing.sign(canonical_bytes_simple, provider)
        pairs.append(_extract_rs(result.bundle_json))

    r_values = [r for r, _ in pairs]
    distinct_r = set(r_values)
    assert len(distinct_r) == n, (
        f"ECDSA nonce reuse: {n} signings produced only {len(distinct_r)} "
        f"distinct r values. A repeated r enables private-key recovery (K-A9)."
    )
    assert len(set(pairs)) == n, (
        f"{n} signings produced only {len(set(pairs))} distinct (r, s) pairs."
    )
