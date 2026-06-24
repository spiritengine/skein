# Conformance Test Corpus Mapping

Sigstore-python bundle corpus → SKEIN test scenario → expected VerifyStatus.

All corpus files are from `sigstore-python/test/assets/` (MIT-licensed).
Corpus files are **staging bundles** signed against `staging.sigstore.dev`.
They must be verified with `Verifier.staging(offline=True)`, not production.

SKEIN's production verifier uses `Verifier.production()`. The staging corpus is used
for conformance testing of the parser, dispatcher, and error-mapping logic — not for
testing production trust root configuration. Bundle acceptance tests in this file are
marked `@conformance_staging` to distinguish them.

---

## Known-good bundles (must return VERIFIED)

| Corpus file | SKEIN test | Expected VerifyStatus | Rationale |
|---|---|---|---|
| `bundle_v3.txt` / `bundle_v3.txt.sigstore` | `TestKnownGoodBundles::test_bundle_v3_verifies` | VERIFIED | Canonical v0.3 staging bundle; exercises the full v0.3 verification path |
| `bundle_v3.txt` / `bundle_v3.txt.sigstore` | `TestKnownGoodBundles::test_bundle_v3_identity_fields` | VERIFIED + issuer/subject populated | Issuer is `https://github.com/login/oauth`; subject is `william@yossarian.net` |
| `bundle_v3_alt.txt` / `bundle_v3_alt.txt.sigstore` | `TestKnownGoodBundles::test_bundle_v3_alt_verifies` | VERIFIED | Alternate signer; tests independent key/log-index handling |
| `tsa/bundle.txt` / `tsa/bundle.txt.sigstore` | `TestKnownGoodBundles::test_bundle_tsa_verifies` | VERIFIED | RFC3161 TSA timestamps present (Rekor v2 path); SKEIN v0 default path per rev 5 |
| `bundle_v3_no_signed_time.txt` / `bundle_v3_no_signed_time.txt.sigstore.json` | `TestKnownGoodBundles::test_bundle_v3_no_signed_time_verifies` | VERIFIED | No `inclusionPromise` (no SET); v0.3 allows omitting SET when inclusion proof present |

---

## Known-bad bundles (must return non-VERIFIED)

| Corpus file | SKEIN test | Expected VerifyStatus | Rationale |
|---|---|---|---|
| `bundle_cve_2022_36056.txt` / `bundle_cve_2022_36056.txt.sigstore` | `TestKnownBadBundles::test_cve_2022_36056_rejected` | INCLUSION_FAILED | CVE-2022-36056: tlog entry inconsistent with sig/cert material. Patched in sigstore-python ≥3.5.3. |
| `bundle_invalid_version.txt` / `bundle_invalid_version.txt.sigstore` | `TestKnownBadBundles::test_invalid_media_type_rejected` | BUNDLE_MALFORMED | `mediaType = "this is completely wrong"` — parse failure at Bundle.from_json() |
| `bundle_no_checkpoint.txt` / `bundle_no_checkpoint.txt.sigstore` | `TestKnownBadBundles::test_no_checkpoint_rejected` | INCLUSION_FAILED | v0.2 bundle with SET only, no `inclusionProof.checkpoint`. Rekor v1 style; rejected by `sigstore-public-v1` which requires Rekor v2 checkpoint-backed proofs. |
| `bundle_no_log_entry.txt` / `bundle_no_log_entry.txt.sigstore` | `TestKnownBadBundles::test_no_log_entry_rejected` | INCLUSION_FAILED | v0.1 bundle with empty `tlogEntries`. No transparency log commitment to verify. |

---

## Corpus files NOT mapped to SKEIN analogs (mismatches and gaps)

| Corpus file | Mismatch | Proposed SKEIN-specific equivalent |
|---|---|---|
| `bundle_no_cert_v1.txt` / `bundle_no_cert_v1.txt.sigstore` | Uses `x509CertificateChain` (full chain, v0.1/v0.2 format) instead of `certificate` (leaf only, v0.3). SKEIN should accept either cert form if sigstore-python handles both, but the `no_cert_v1` naming is misleading — the cert IS present, just in the old chain format. | Test separately: extract cert from chain, verify same identity is parsed. Not a known-bad bundle per se. |
| `bundle_v3_github.whl` / `bundle_v3_github.whl.sigstore` | Signs a binary `.whl` artifact (not a text folio). There is no SKEIN folio analog for binary blob signing in v0. | SKEIN v0 only signs canonical_bytes (text JSON). Binary signing is out of scope. Test acceptance as a generic byte payload if needed. |
| `bundle.txt` / `bundle.txt.crt` / `bundle.txt.sig` (detached sig form) | The `.crt` + `.sig` files are the legacy detached format (pre-bundle). SKEIN only uses canonical sigstore-bundle JSON (Rekor v2 / sigstore-bundle v0.3). | No SKEIN equivalent — bundle format only. |
| `test/assets/integration/bundle_v3.txt.sigstore` | Integration bundle signed against the live staging Rekor (requires network). | Integration tests are separate from offline conformance tests; mark as `@conformance_online`. |
| `tsa/bundle.duplicate.sigstore` | Bundle with duplicate TSA timestamp entries. | Propose: add `TestSignatureEdgeCases::test_duplicate_tsa_entries_handled` — similar to the duplicate tlogEntries test. |
| `tsa/bundle.many_timestamp.sigstore` | Bundle with multiple TSA timestamps. | Propose: verify SKEIN picks the most recent valid timestamp (or any valid one). Relevant for future TSA policy decisions. |
| `tsa/bundle.txt.late_timestamp.sigstore` | TSA timestamp falls outside the cert validity window. | **High-priority gap**: add test that this returns CERT_INVALID (or INCLUSION_FAILED) since the timestamp is outside the Fulcio cert's notBefore/notAfter window. |

---

## Mock / fixture strategy

### Rejection tests (offline, no network)
These tests work without any Sigstore network connectivity:
- Construct SKEIN `signature_bundle` dicts from corpus files via `make_skein_bundle()`
- Pass to `verify()` and assert status
- No mocking needed — rejection is cryptographic/structural

### Acceptance tests (staging TUF root, offline)
These tests use `Verifier.staging(offline=True)` internally:
- Marked `@pytest.mark.conformance_staging`
- Require the staging TUF root baked into `sigstore-python`'s package data
- No network calls — the TUF root was snapshotted at install time
- Run: `pytest -m conformance_staging`

### Round-trip test (SKEIN sign → sigstore-python verify)
The true end-to-end round-trip requires a real OIDC token:
- Marked `@pytest.mark.skip(reason="requires OIDC: set SKEIN_TEST_OIDC=1 and provide token")`
- Enable in GitHub Actions using the ambient OIDC token from the workflow
- `SKEIN_TEST_OIDC_TOKEN` env var bypasses the browser OIDC flow
- For the offline approximation, `TestPortabilityRoundTrip::test_bundle_json_round_trip_does_not_corrupt`
  verifies that SKEIN does not corrupt a bundle during storage/retrieval

### Portability sanity check (bidirectional agreement, staging)
- `TestPortabilityRoundTrip` verifies that SKEIN and sigstore-python AGREE
- Uses the same corpus bundle through two independent code paths
- Does NOT require mocking — uses corpus bundles directly

---

## Coverage gaps

1. **`tsa/bundle.txt.late_timestamp.sigstore`**: TSA timestamp outside cert validity window → should be CERT_INVALID. Not yet in the test file; add to `TestKnownBadBundles`.
2. **Production bundles**: No production-signed corpus available. The acceptance test layer can only be tested against production infrastructure via real sign→verify. Covered by the `@skip` round-trip test.
3. **Unicode identity in cert SAN** (issue #1507): A bundle where the cert SAN contains non-ASCII characters. No corpus file available; requires a synthetic bundle. Deferred to adversarial / edge-case tests (Team A.3).
4. **TUF cache race condition** (issue #1403): Multi-process parallel signing hitting the same TUF cache. Not a verification test; affects sign() path. Deferred to Team A.3 (adversarial).
5. **Witness cosigned checkpoint**: Bundle with multiple `—` signature lines in the C2SP checkpoint signed-note. The corpus does not include this. Propose a synthetic fixture for when the witness network launches (v1 scope).
6. **DSSE envelope bundles for in-toto statements**: Out of scope for SKEIN v0 (SKEIN signs canonical_bytes, not DSSE statements). The test at `TestSignatureEdgeCases::test_dsse_envelope_bundle_rejected_as_bundle_malformed` covers the rejection case.

---

## Sigstore-python corpus location

After `pip install "sigstore>=4.2,<5"`, corpus files are NOT included in the
wheel. Obtain from source: `github.com/sigstore/sigstore-python/test/assets/`.
A copy is committed to `tests/conformance/corpus/` in this repository.

License: Apache-2.0 (sigstore-python source); MIT per the project's test asset policy.
