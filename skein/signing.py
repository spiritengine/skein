"""skein/signing.py — Sigstore-anchored signing/verify primitive.

This module wraps sigstore-python at the SKEIN boundary: every cryptographic
operation flows through real sigstore-python APIs. The _test_factory installs
monkeypatches over sigstore-python's call sites; production and test paths
share the same code, with the library mocked at the boundary in tests.

Per finding-20260511-kn5j, signing.py is the ONLY module in skein/ that may
import sigstore, cryptography, or requests for signing purposes. Everything
that touches those libraries lives here, including the test factory.

Spec:
    brief-20260511-nbz4   Identity rev 5 (architectural spec)
    finding-20260511-kn5j RSP brief rev 3 (locked surface)
    finding-20260511-d3u6 spec clarifications + amended §5 exception mapping
    finding-20260513-w5hq addendum (log_id, OIDC allowlist, trust_root_pin)
    finding-20260513-tx8r aud=sigstore lock
    finding-20260514-caqj worst-status aggregation + severity order
    finding-20260514-burb SAN extraction policy
    finding-20260514-09eb IDENTITY_MISMATCH semantics (Option B)

Operational notes:
    docs/SIGSTORE_OPERATIONS.md — TUF cache per-worker isolation,
    OFFLINE_NO_TRUSTED_ROOT troubleshooting.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import datetime
import hashlib
import json
import logging
import re
import secrets
import time
import unicodedata
from collections.abc import Mapping
from enum import Enum
from typing import Any, Iterator

from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

# Sigstore-python imports. signing.py is the ONLY module in skein/ that
# imports sigstore (per finding-20260511-kn5j boundary).
import sigstore
import sigstore.errors
import sigstore.models
import sigstore.oidc
import sigstore.sign
import sigstore.verify
import sigstore.verify.policy
from sigstore.models import Bundle, ClientTrustConfig, bundle_v1, common_v1, rekor_v1
from sigstore.oidc import IdentityToken
from sigstore.sign import SigningContext
from sigstore.verify import Verifier
from sigstore.verify.policy import UnsafeNoOp

# cryptography is allowed here for the test factory to mint Fulcio-shaped
# certificates. The production path delegates cert/crypto handling to
# sigstore-python, with one deliberate exception: the sigstore-public-v1
# profile gate uses decode_dss_signature for a strict DER parse of the
# inclusion-promise SET (finding-20260519-74o7) — a defense-in-depth
# structural check ahead of sigstore-python's cryptographic SET verification.
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa, padding
from cryptography.hazmat.primitives.asymmetric.utils import (
    Prehashed,
    decode_dss_signature,
    encode_dss_signature,
)


logger = logging.getLogger(__name__)


MIN_MICROSECOND_TIMESTAMP = 1_000_000_000_000_000
MAX_CANONICAL_BYTES_LENGTH = 2 * 1024 * 1024  # 2 MiB


# ---------------------------------------------------------------------------
# Locked Pydantic surface — Phase 2 LOCK at 8f1e53e.
# ---------------------------------------------------------------------------


class OIDCProviderConfig(BaseModel):
    """OIDC provider configuration passed to sign()."""

    issuer: str
    token: str
    provider_id: str | None = None
    expires_at: int | None = Field(default=None, ge=MIN_MICROSECOND_TIMESTAMP)


class RekorInclusionProof(BaseModel):
    """Rekor v2 inclusion proof — independently verifiable Merkle witness."""

    log_index: int = Field(ge=0)
    tree_size: int = Field(gt=0)
    root_hash: str
    hashes: list[str]
    checkpoint: str
    integrated_time: int = Field(ge=MIN_MICROSECOND_TIMESTAMP)
    log_id: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def _log_index_inside_tree(self) -> "RekorInclusionProof":
        if self.log_index >= self.tree_size:
            raise ValueError("log_index must be less than tree_size")
        return self


class Evidence(BaseModel):
    validity_window: tuple[int, int] | None = None
    rekor_inclusion: RekorInclusionProof | None = None
    cert_chain_summary: str | None = None


class VerifyStatus(str, Enum):
    """8 distinct, non-collapsing verify outcomes (Identity rev 5)."""

    VERIFIED = "VERIFIED"
    CERT_INVALID = "CERT_INVALID"
    INCLUSION_FAILED = "INCLUSION_FAILED"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    TRUST_ROOT_STALE = "TRUST_ROOT_STALE"
    BUNDLE_MALFORMED = "BUNDLE_MALFORMED"
    OFFLINE_NO_TRUSTED_ROOT = "OFFLINE_NO_TRUSTED_ROOT"
    SIGNATURE_MISMATCH = "SIGNATURE_MISMATCH"


class VerifyResult(BaseModel):
    status: VerifyStatus
    issuer: str | None = None
    subject: str | None = None
    evidence: Evidence | None = None


class MultiVerifyResult(BaseModel):
    results: list[VerifyResult] = Field(min_length=1)
    overall: VerifyStatus


class SignResult(BaseModel):
    bundle_json: str
    issuer: str
    subject: str
    signing_timestamp: int = Field(ge=MIN_MICROSECOND_TIMESTAMP)
    evidence: Evidence


class SignatureBundle(BaseModel):
    identity_scheme: str
    bundles: list[str] = Field(max_length=256)
    canonical_bytes: bytes = Field(max_length=MAX_CANONICAL_BYTES_LENGTH)
    canon_version: str = "knurl-1.0"
    trust_root_pin: str | None = None

    @field_serializer("canonical_bytes", when_used="json")
    def _serialize_canonical_bytes(self, v: bytes) -> str:
        return base64.b64encode(v).decode("ascii")

    @field_validator("canonical_bytes", mode="before")
    @classmethod
    def _canonical_bytes_from_base64(cls, v: object) -> object:
        if isinstance(v, str):
            try:
                return base64.b64decode(v.encode("ascii"), validate=True)
            except (binascii.Error, UnicodeEncodeError) as exc:
                raise ValueError(
                    "canonical_bytes must be standard base64 on the JSON wire"
                ) from exc
        return v

    @field_validator("bundles")
    @classmethod
    def _bundles_at_least_one(cls, v: list[str]) -> list[str]:
        if len(v) < 1:
            raise EmptySignatureBundle(
                "SignatureBundle must have at least one signer; got 0."
            )
        return v


class SigningUnavailable(Exception):
    def __init__(
        self,
        reason: str,
        *,
        component: str | None = None,
        cause: Exception | None = None,
    ):
        self.reason = reason
        self.component = component
        super().__init__(reason)
        if cause is not None:
            self.__cause__ = cause


class EmptySignatureBundle(Exception):
    pass


class MultiSignerBundle(Exception):
    pass


# ---------------------------------------------------------------------------
# Severity order (finding-20260514-caqj). Explicit dict, NOT max() over the
# str-Enum — alphabetical order on the str values disagrees with severity.
# ---------------------------------------------------------------------------

_SEVERITY_RANK: dict[VerifyStatus, int] = {
    VerifyStatus.SIGNATURE_MISMATCH: 0,  # worst
    VerifyStatus.CERT_INVALID: 1,
    VerifyStatus.INCLUSION_FAILED: 2,
    VerifyStatus.BUNDLE_MALFORMED: 3,
    VerifyStatus.TRUST_ROOT_STALE: 4,
    VerifyStatus.OFFLINE_NO_TRUSTED_ROOT: 5,
    VerifyStatus.IDENTITY_MISMATCH: 6,
    VerifyStatus.VERIFIED: 7,  # mildest
}

# Fail-loud at import if a new VerifyStatus member lands without a rank entry;
# verify_multi's aggregation would otherwise KeyError at runtime.
assert set(_SEVERITY_RANK.keys()) == set(VerifyStatus), (
    f"_SEVERITY_RANK mismatch with VerifyStatus: "
    f"missing {set(VerifyStatus) - set(_SEVERITY_RANK.keys())}, "
    f"extra {set(_SEVERITY_RANK.keys()) - set(VerifyStatus)}"
)


# finding-20260513-w5hq §2 (LOCKED) amended 2026-05-19 per finding-20260520-9jc5:
# the Sigstore Dex brokers are admitted as identity intermediaries for the
# v0 personal-OAuth allowlist. Empirical premise: every human-flow Sigstore
# signer (gitsign, cosign, sigstore-python's interactive Issuer) receives a
# Dex-issued token with iss=oauth2.sig{store|stage}.dev/auth and the underlying
# IdP appearing only as the federated_issuer / sub claims. Dex's connector
# config constrains the upstream IdPs to Google/GitHub-personal/Microsoft,
# preserving the original w5hq §2 intent ("v0 trusts personal-OAuth human
# identities") at the only layer the ecosystem actually exposes. Direct-IdP
# token paths exist on Fulcio's side (its /api/v2/configuration trusts
# accounts.google.com directly) but are not yet reachable for human flows
# (sigstore-python's client_id="sigstore" default + tx8r's literal aud=
# "sigstore" lock combine to require the Dex-brokered path today). Revisit
# when those preconditions change.
_V0_OIDC_ALLOWLIST: frozenset[str] = frozenset(
    {
        "https://accounts.google.com",
        "https://github.com/login/oauth",
        "https://oauth2.sigstore.dev/auth",  # Sigstore prod Dex broker
        "https://oauth2.sigstage.dev/auth",  # Sigstore staging Dex broker
    }
)


_OID_ISSUER_V2 = "1.3.6.1.4.1.57264.1.8"
_OID_ISSUER_LEGACY = "1.3.6.1.4.1.57264.1.1"
_OID_SIGNER_IDENTITY_OTHERNAME = "1.3.6.1.4.1.57264.1.24"
_OID_SCT = "1.3.6.1.4.1.11129.2.4.2"


def _der_length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    length_bytes = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(length_bytes)]) + length_bytes


def _der_utf8(data: bytes) -> bytes:
    """DER-encode a UTF8String (tag 0x0c)."""
    return bytes([0x0C]) + _der_length(len(data)) + data


# ---------------------------------------------------------------------------
# JWT helpers — parse aud/exp without verifying signature (Fulcio does that).
# ---------------------------------------------------------------------------


def _parse_jwt_payload(token: str) -> dict[str, Any] | None:
    """Parse JWT payload (middle segment). Return None if token is not a JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    padding_chars = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode((payload + padding_chars).encode("ascii"))
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return None
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _check_aud(payload: dict[str, Any]) -> None:
    if "aud" not in payload:
        raise SigningUnavailable("OIDC token missing aud claim", component="oidc")
    aud = payload["aud"]
    if isinstance(aud, str):
        if aud != "sigstore":
            raise SigningUnavailable(
                f"OIDC token aud is {aud}, expected sigstore",
                component="oidc",
            )
    elif isinstance(aud, list):
        if "sigstore" not in aud:
            raise SigningUnavailable(
                f"OIDC token aud is {aud}, expected sigstore",
                component="oidc",
            )
    else:
        raise SigningUnavailable(
            f"OIDC token aud is {aud!r}, expected sigstore",
            component="oidc",
        )


def _now_microseconds() -> int:
    return int(time.time() * 1_000_000)


# ---------------------------------------------------------------------------
# Exception mapping (d3u6 §5 amended).
#
# Matching strategy: by class name only, applied uniformly to every rule.
# The prior code mixed `name == ...` and `isinstance(...)` per branch, which
# the fell flagged as inconsistent (finding-20260515 N3). It is resolved in
# favour of name matching because that is the load-bearing mechanism here:
#   - sigstore-python 4.2.0 has a flat public error surface; the specific
#     exceptions d3u6 §5 maps are matched exactly by __class__.__name__.
#   - The private sigstore._internal.* exceptions (FulcioClientError,
#     RekorClientError, TimestampError, ExpiredCertificate) do NOT subclass
#     the public hierarchy, so isinstance against public bases never matched
#     them — only the name does.
#   - Synthetic test doubles raised by _synthesize_exception use the real
#     class for known names (name matches) or an ad-hoc Exception subclass
#     carrying the canonical name (only the name matches).
# isinstance was therefore redundant for the classes we map; an unknown real
# subclass falls to the catch-all (BUNDLE_MALFORMED + WARNING), the d3u6 §5
# safe default. The one non-class rule is the VerificationError +
# "signature is invalid for input"/"bundle message digest mismatch" message
# heuristic, kept explicit. The phrases are sigstore-python's literal wire
# strings (verifier.py 4.2.0 @ 483 and 495); loose substrings like
# "signature is invalid" would also false-match cert-chain wording like
# "cert chain: signature is invalid for leaf" (F-pass round 4 R4-4).
#
# Two verify-path rules are deliberate SKEIN narrowings of the literal d3u6 §5
# table, not transcriptions of it (finding-20260519-74o7): (a) the
# VerificationError + message heuristic maps to SIGNATURE_MISMATCH instead of
# the table's BUNDLE_MALFORMED fallback — a contained, fail-closed
# re-introduction of message-matching solely to surface a more informative
# non-VERIFIED status; (b) FulcioClientError maps to INCLUSION_FAILED on the
# verify path, where the table says n/a (Fulcio is a sign-path concern). Both
# stay fail-closed; they refine, never loosen, the safe default.
# ---------------------------------------------------------------------------


def _classify_sign_exception(exc: BaseException) -> tuple[str, str]:
    name = exc.__class__.__name__
    msg = str(exc)

    if name in ("TokenExpiredMidFlow", "_TokenExpiredMidFlow"):
        return ("token_expired_mid_flow", f"oidc token expired mid-flow: {msg}")

    if name in ("IdentityError", "IssuerError", "ExpiredIdentity"):
        return ("oidc", f"oidc token invalid or expired: {msg}")

    if name in ("TUFError", "MetadataError", "RootError"):
        return ("tuf", f"tuf unavailable: {msg}")

    if name == "FulcioClientError":
        return ("fulcio", f"fulcio unavailable: {msg}")
    if name in ("ExpiredCertificate", "CertificateExpired"):
        return ("fulcio", f"fulcio cert expired: {msg}")

    if name == "RekorClientError":
        return ("rekor", f"rekor unavailable: {msg}")

    if name == "TimestampError":
        return ("tsa", f"tsa unavailable: {msg}")

    if name in ("TimeoutError", "ConnectionError", "ConnectionRefusedError"):
        return ("network", f"network failure: timeout or connection: {msg}")
    if name == "NetworkError":
        return ("network", f"network failure: {msg or 'unreachable'}")

    if name == "CertValidationError":
        return ("fulcio", f"cert validation failed: {msg}")

    # Catch-all per brief-20260514-7i3w parity: the verify-side
    # _map_sigstore_exception already logs unrecognized classes so operators
    # can spot sigstore-python surface drift before it becomes silent
    # misclassification. The sign side did the same attribution-to-fulcio
    # collapse without surfacing the drift signal (Knuth B-review concern 4 /
    # Oracle B-review zone 3).
    logger.warning(
        "signing.classify_sign_exception_unrecognized: %s raised "
        "(sigstore %s): %s — attributing to fulcio",
        name,
        sigstore.__version__,
        msg,
    )
    return ("fulcio", f"sign failed: {name}: {msg}")


def _map_sigstore_exception(exc: BaseException) -> VerifyStatus:
    """Single point of mapping per d3u6 §5 amended. Name-based per the
    strategy note above.

    Catch-all branch emits a WARNING log per brief-20260514-7i3w.
    """
    name = exc.__class__.__name__
    msg = str(exc)
    msg_lower = msg.lower()

    # Signature-mismatch: the factory signal _SignatureInvalid, or a
    # sigstore-python VerificationError whose message uses one of the two
    # exact wire phrasings sigstore-python raises for payload/signature
    # failure (sigstore/verify/verifier.py @ 4.2.0):
    #   - "Signature is invalid for input" (line 495)
    #   - "Bundle message digest mismatch" (line 483)
    #
    # Match the full phrase, not the loose substring "signature is invalid"
    # or "digest mismatch" — those would also catch cert-chain wording like
    # "cert chain: signature is invalid for leaf" and misclassify a cert-
    # chain failure as SIGNATURE_MISMATCH (F-pass round 4 R4-4). Cert-chain
    # failures from real sigstore-python come through as CertValidationError
    # (handled below as CERT_INVALID), but a future sigstore-python rev that
    # wraps cert-chain errors in plain VerificationError would have hit the
    # loose substring otherwise.
    if name == "_SignatureInvalid":
        return VerifyStatus.SIGNATURE_MISMATCH
    if name == "VerificationError" and (
        "signature is invalid for input" in msg_lower
        or "bundle message digest mismatch" in msg_lower
    ):
        return VerifyStatus.SIGNATURE_MISMATCH

    if name == "InvalidBundle":
        return VerifyStatus.BUNDLE_MALFORMED

    if name == "InvalidMaterials":
        return VerifyStatus.CERT_INVALID
    if name == "InvalidRekorEntry":
        return VerifyStatus.INCLUSION_FAILED

    if name in ("CertificateExpired", "ExpiredCertificate"):
        return VerifyStatus.CERT_INVALID
    if name == "CertValidationError":
        return VerifyStatus.CERT_INVALID

    if name == "RekorClientError":
        return VerifyStatus.INCLUSION_FAILED
    if name == "NetworkError":
        return VerifyStatus.INCLUSION_FAILED
    if name in ("TimeoutError", "ConnectionError", "ConnectionRefusedError"):
        return VerifyStatus.INCLUSION_FAILED
    if name == "FulcioClientError":
        return VerifyStatus.INCLUSION_FAILED

    # TUF-class infrastructure outages → OFFLINE_NO_TRUSTED_ROOT. MetadataError
    # and RootError are NOT subclasses of TUFError in sigstore-python (separate
    # exception classes in sigstore.errors), so a name-based check on
    # "TUFError" alone misses them and lets them fall through to the catch-all
    # WARNING + BUNDLE_MALFORMED. Callers interpret BUNDLE_MALFORMED as a
    # permanent bundle defect; TUF staleness / root rotation is a transient
    # infrastructure issue ("retry later, fetch fresh metadata"), not a bundle
    # defect. The sign path already groups all three in _classify_sign_exception
    # (signing.py:357); the verify path needs the same grouping.
    if name in ("TUFError", "MetadataError", "RootError"):
        return VerifyStatus.OFFLINE_NO_TRUSTED_ROOT

    # Catch-all per brief-20260514-7i3w.
    logger.warning(
        "signing.exception_catchall: %s raised (sigstore %s): %s",
        name,
        sigstore.__version__,
        msg,
    )
    return VerifyStatus.BUNDLE_MALFORMED


# ---------------------------------------------------------------------------
# Bundle introspection helpers.
# ---------------------------------------------------------------------------


def _extract_issuer_from_cert(cert: Any) -> str | None:
    """Extract OIDC issuer from a Fulcio leaf cert.

    Prefer OID 1.3.6.1.4.1.57264.1.8 (Issuer V2, DER UTF8 string).
    Fall back to OID 1.3.6.1.4.1.57264.1.1 (legacy, raw UTF8 bytes).

    The V2 path used to fall back to ``v2.decode("utf-8")`` when DER parse
    rejected the input. That fallback admitted any byte sequence that
    happened to be valid UTF-8 — including IA5String-tagged values
    (``b"\\x16\\x05alice"``) where the leading ``0x16`` tag byte and length
    byte become control characters embedded in the returned identity
    string. A downstream policy check like ``issuer == "https://accounts.
    google.com"`` would fail to match, but an attacker could conceivably
    poison logged identity strings or wedge identity-comparison logic.
    V2 is spec'd as DER UTF8String; if the parse rejects, the cert is
    malformed at that extension and we return None. The legacy OID's
    fallback is preserved because the legacy spec IS raw UTF-8 bytes
    (not DER-wrapped).

    Post-extraction sanitization mirrors _extract_subject_from_cert's
    fail-closed-on-malformed policy (finding-20260514-burb): a NUL byte,
    leading/trailing whitespace, or empty string is ambiguous identity
    material and yields None so the caller surfaces CERT_INVALID. NUL
    in particular enables a startswith-prefix split bypass — a downstream
    ``issuer.startswith("https://accounts.google.com")`` returns True for
    ``"https://accounts.google.com\\x00bad"`` even though the cert's
    issuer string is not the trusted issuer. The guard applies to both
    V2 and legacy extraction paths.
    """

    def _read_ext(oid_str: str) -> bytes | None:
        for ext in cert.extensions:
            if ext.oid.dotted_string == oid_str:
                value = ext.value.value
                if isinstance(value, (bytes, bytearray)):
                    return bytes(value)
        return None

    raw: str | None = None
    v2 = _read_ext(_OID_ISSUER_V2)
    if v2 is not None:
        raw = _decode_der_utf8(v2)
    else:
        legacy = _read_ext(_OID_ISSUER_LEGACY)
        if legacy is not None:
            try:
                raw = legacy.decode("utf-8")
            except UnicodeDecodeError:
                return None

    if raw is None:
        return None
    if not raw:
        return None
    if "\x00" in raw:
        return None
    if raw != raw.strip():
        return None
    if _has_invisible_identity_char(raw):
        return None
    return unicodedata.normalize("NFC", raw)


# Default-ignorable / visually-invisible chars outside the Cf and Cc
# categories. See _has_invisible_identity_char for rationale.
_EXTRA_INVISIBLE_IDENTITY_CHARS: frozenset[str] = frozenset(
    chr(c)
    for c in (
        *range(0xFE00, 0xFE10),  # VS-1..VS-16
        *range(0xE0100, 0xE01F0),  # VS-17..VS-256
        *range(0x180B, 0x180E),  # Mongolian FVS-1..FVS-3
        0x115F,  # HANGUL CHOSEONG FILLER
        0x1160,  # HANGUL JUNGSEONG FILLER
        0x3164,  # HANGUL FILLER
        0xFFA0,  # HALFWIDTH HANGUL FILLER
    )
)


# Reject any default-ignorable / visually-invisible character in extracted
# identity material on both subject and issuer paths. All survive NFC
# normalization and have no visible glyph (or, for RLO/LRO, they reorder
# surrounding glyphs without changing byte-level comparison), so they can
# be smuggled into an identity to produce a string visually identical to
# a trusted identity but that compares non-equal.
#
# Coverage:
#
#   Cf (Format): all format chars — covers ZWSP/ZWNJ/ZWJ/LRM/RLM
#       (U+200B-U+200F), legacy bidi LRE/RLE/PDF/LRO/RLO (U+202A-U+202E),
#       modern bidi isolates LRI/RLI/FSI/PDI (U+2066-U+2069), WORD JOINER
#       (U+2060), BOM / ZERO WIDTH NO-BREAK SPACE (U+FEFF), ARABIC LETTER
#       MARK (U+061C), MONGOLIAN VOWEL SEPARATOR (U+180E), deprecated
#       format chars, language tags, and any future Cf additions in a
#       later Unicode release.
#   Cc (Control): C0/C1 control bytes that the upstream \x00 / strip
#       guards do not fully cover (e.g., interior \t, \n, \r, \x16 SYN).
#   Plus explicit coverage of default-ignorable chars that are NOT in
#   Cf or Cc (see _EXTRA_INVISIBLE_IDENTITY_CHARS above):
#     - Variation selectors (Mn / Nonspacing_Mark):
#         U+FE00-U+FE0F   VS-1..VS-16 (standard)
#         U+E0100-U+E01EF VS-17..VS-256 (supplement)
#         U+180B-U+180D   Mongolian Free Variation Selectors 1-3
#     - HANGUL fillers (Lo / Other_Letter):
#         U+115F HANGUL CHOSEONG FILLER
#         U+1160 HANGUL JUNGSEONG FILLER
#         U+3164 HANGUL FILLER
#         U+FFA0 HALFWIDTH HANGUL FILLER
#   These have empty glyphs (variation selectors are visually invisible
#   on their own; HANGUL fillers render as zero-width placeholders) and
#   would slip past a Cf/Cc-only category check (Shard FEFF r1 fell
#   follow-up).
def _has_invisible_identity_char(s: str) -> bool:
    for c in s:
        if unicodedata.category(c) in ("Cf", "Cc"):
            return True
        if c in _EXTRA_INVISIBLE_IDENTITY_CHARS:
            return True
    return False


def _decode_der_utf8(data: bytes) -> str | None:
    """Decode a DER UTF8String (tag 0x0C). Returns None on parse failure.

    Strict DER per X.690 §8.1.3 / §10.1:
      - Tag must be 0x0C (UTF8String).
      - Length must use minimal encoding: short form (one octet < 0x80) for
        lengths 0-127; long form (0x80 | n followed by n octets) for larger,
        with n >= 1, no leading zero octet, and the encoded length > 127.
      - Indefinite length (0x80) is BER-only and rejected.
      - Total encoded size must equal tag + length octets + content (no
        trailing garbage past the encoded value).

    Lenient parsing was a bug surface: BER indefinite-length (data[1] == 0x80)
    was decoded as length=0 -> empty string, and any short-form length that
    exceeded the buffer was silently truncated to whatever bytes remained.
    Both shapes are now rejected.
    """
    if len(data) < 2 or data[0] != 0x0C:
        return None
    first = data[1]
    if first < 0x80:
        # Short form. Length is `first`; content starts at offset 2.
        length = first
        content_off = 2
    elif first == 0x80:
        # Indefinite length: BER, not DER.
        return None
    else:
        # Long form: n length octets follow.
        n = first & 0x7F
        if 2 + n > len(data):
            return None
        length_octets = data[2 : 2 + n]
        # DER §10.1: leading zero octet in long-form length is forbidden
        # (would not be minimal encoding).
        if length_octets[0] == 0x00:
            return None
        length = int.from_bytes(length_octets, "big")
        # DER §10.1: short form must be used when length < 128.
        if length < 128:
            return None
        content_off = 2 + n
    end = content_off + length
    # Body must be exactly `length` bytes — no silent truncation.
    if end > len(data):
        return None
    # No trailing garbage past the encoded value.
    if end != len(data):
        return None
    try:
        return data[content_off:end].decode("utf-8")
    except UnicodeDecodeError:
        return None


def _extract_subject_from_cert(cert: Any) -> str | None:
    """Extract OIDC subject from Fulcio leaf cert SAN per finding-20260514-burb.

    Preference order: rfc822Name → uniformResourceIdentifier →
    otherName OID 1.3.6.1.4.1.57264.1.24.

    Identity normalization (oracle actionable #1):
      - NFC normalize Unicode.
      - Reject identities with leading/trailing whitespace (visual ambiguity).
      - Reject identities containing NUL bytes.
      - Reject identities containing any default-ignorable / visually-
        invisible character (see _has_invisible_identity_char): Unicode
        Cf (Format) and Cc (Control) categories, plus variation selectors
        and HANGUL fillers — they survive NFC and produce visually-
        identical strings.
    """
    try:
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return None
    san = san_ext.value

    raw: str | None = None
    for name in san:
        if isinstance(name, x509.RFC822Name):
            raw = name.value
            break
    if raw is None:
        for name in san:
            if isinstance(name, x509.UniformResourceIdentifier):
                raw = name.value
                break
    if raw is None:
        for name in san:
            if (
                isinstance(name, x509.OtherName)
                and name.type_id.dotted_string == _OID_SIGNER_IDENTITY_OTHERNAME
            ):
                # The signer-identity OtherName value is spec'd as a DER
                # UTF8String (tag 0x0C). The prior code fell back to
                # `name.value.decode("utf-8")` when the DER parse rejected,
                # which would accept e.g. b"\x16\x05alice" (IA5String tag)
                # as the identity string "\x16\x05alice" — control-char
                # prefix slips past the NUL / strip / NFC guards below.
                # Strict path: if it's not valid DER UTF8String, treat the
                # SAN entry as malformed.
                raw = _decode_der_utf8(name.value)
                break
    if raw is None:
        return None

    # Fail-closed-on-malformed-SAN policy (finding-20260514-burb; oracle
    # actionable #1): a NUL byte or leading/trailing whitespace in the SAN
    # is visually ambiguous identity material — return None so the caller
    # surfaces CERT_INVALID rather than trust an ambiguous subject.
    #
    # Empty SAN (any path producing raw == "") is also not a usable identity.
    # The OtherName path can legitimately decode an empty UTF8String
    # (DER 0x0C 0x00 is a syntactically valid zero-length encoding), which
    # would otherwise propagate through this function as "", escape the
    # `subject is None` guard in verify(), and surface as a VERIFIED result
    # whose subject is the empty string.
    if not raw:
        return None
    if "\x00" in raw:
        return None
    if raw != raw.strip():
        return None
    if _has_invisible_identity_char(raw):
        return None
    return unicodedata.normalize("NFC", raw)


def _cert_validity_window(cert: Any) -> tuple[int, int]:
    nb = cert.not_valid_before_utc
    na = cert.not_valid_after_utc
    return (int(nb.timestamp() * 1_000_000), int(na.timestamp() * 1_000_000))


def _build_rekor_inclusion_proof(bundle: Bundle) -> RekorInclusionProof | None:
    try:
        log_entry = bundle.log_entry
        inner = log_entry._inner
        proof = inner.inclusion_proof
        if proof is None:
            return None
        checkpoint = ""
        if proof.checkpoint and proof.checkpoint.envelope:
            checkpoint = proof.checkpoint.envelope
        integrated_time_s = inner.integrated_time or 0
        integrated_time_us = int(integrated_time_s) * 1_000_000
        if integrated_time_us < MIN_MICROSECOND_TIMESTAMP:
            # j4w4 round-7 oracle A3: silent floor substitution masked upstream
            # drift. RekorInclusionProof.integrated_time would carry the sentinel
            # floor with no signal that the wire value was below it. Mirror the
            # warning style used by classify_sign_exception_unrecognized / the
            # _map_sigstore_exception catch-all so operators have a single
            # grep-able prefix for sigstore-surface drift.
            logger.warning(
                "signing.rekor_integrated_time_below_floor: "
                "Rekor returned integrated_time=%s below "
                "MIN_MICROSECOND_TIMESTAMP; substituting floor value",
                integrated_time_s,
            )
            integrated_time_us = MIN_MICROSECOND_TIMESTAMP
        log_index = int(proof.log_index)
        tree_size = int(proof.tree_size)
        # log_index >= tree_size is a malformed proof (a leaf cannot sit at
        # or past the tree it claims membership in). Fail closed: return None
        # so Evidence.rekor_inclusion is absent and the malformedness is
        # surfaced, rather than coercing tree_size to paper over it.
        if log_index >= tree_size:
            return None
        log_id_b64 = base64.b64encode(inner.log_id.key_id).decode("ascii")
        # Empty key_id means the wire proof has no Rekor instance identifier.
        # Honor the "return None means no inclusion proof" contract instead of
        # fabricating a placeholder that satisfies the min_length=1 validator
        # and masks the missing-data signal from Evidence consumers.
        if not log_id_b64:
            return None
        # Pass the wire envelope through verbatim — including the empty string
        # when the wire proof has no signed-note. Shard P (residual from
        # finding-20260520-j4w4): the prior `or "rekor.sigstore.dev\n0\n\n\n"`
        # fallback fabricated a signed-note whose tree-size line is "0",
        # silently contradicting RekorInclusionProof.tree_size (which carries
        # the real value from proof.tree_size). Consumers that parsed the
        # checkpoint saw 0; consumers that read the struct saw the real tree
        # size. Empty string is a clean absence signal — RekorInclusionProof
        # .checkpoint has no min_length validator, so it round-trips fine.
        return RekorInclusionProof(
            log_index=log_index,
            tree_size=tree_size,
            root_hash=base64.b64encode(proof.root_hash).decode("ascii"),
            hashes=[base64.b64encode(h).decode("ascii") for h in proof.hashes],
            checkpoint=checkpoint,
            integrated_time=integrated_time_us,
            log_id=log_id_b64,
        )
    except Exception:  # noqa: BLE001
        return None


def _cert_chain_summary(cert: Any) -> str:
    try:
        issuer = _extract_issuer_from_cert(cert) or "unknown-issuer"
        subject = _extract_subject_from_cert(cert) or "unknown-subject"
        return f"Fulcio leaf, issuer={issuer}, subject={subject}"
    except Exception:  # noqa: BLE001
        return "Fulcio leaf"


# ---------------------------------------------------------------------------
# sign / verify / verify_multi
# ---------------------------------------------------------------------------


def _build_production_signing_context() -> Any:
    """Build a sigstore SigningContext from the production trust config.

    Wrapper so the factory has a single patch target.

    Note: brief-20260514-urdk's canonical example shows
    `SigningContext.production()`, but that classmethod does not exist in
    sigstore-python 4.2.0 — the real 4.2.0 API is
    `SigningContext.from_trust_config(ClientTrustConfig.production())`, used
    here. The brief example is wrong; see friction-20260515-ij78. (Comment
    only — the brief is not edited.)
    """
    return SigningContext.from_trust_config(ClientTrustConfig.production())


def _build_staging_signing_context() -> Any:
    """Build a sigstore SigningContext from the staging trust config.

    Sibling of _build_production_signing_context, used only by tests that
    drive REAL signing against Sigstore staging (e.g. the K-A9 interactive
    nonce-reuse test). NOT a production path: sign() always calls the
    production builder; staging tests substitute via monkeypatch, matching
    the established pattern used by the test crypto factory at line ~1918.

    Mirrors the verify side's documented behavior at
    _build_production_verifier: tests opt in to staging; production code
    never reaches this helper.
    """
    return SigningContext.from_trust_config(ClientTrustConfig.staging())


def _build_production_verifier(*, offline: bool = False) -> Any:  # noqa: ANN401
    """Build a sigstore Verifier from the production trust config.

    Wrapper so the factory has a single patch target. trust_root selection by
    SignatureBundle.trust_root_pin is handled in _select_verifier().

    A real production TUF outage propagates as sigstore.errors.TUFError;
    _map_sigstore_exception maps that to OFFLINE_NO_TRUSTED_ROOT (fail closed).
    We do NOT fall back to the staging trust root here — doing so would
    silently verify staging-signed bundles against the staging root during a
    prod outage. Conformance tests that legitimately need the staging root
    patch this helper via the _conformance_staging_verifier fixture.
    """
    return Verifier.production(offline=offline)


def _build_identity_token(token: str) -> Any:
    """Wrap a token string in an IdentityToken (patched by the test factory)."""
    return IdentityToken(token)


def sign(canonical_bytes: bytes, oidc_provider: OIDCProviderConfig) -> SignResult:
    """Sign canonical_bytes via Fulcio + Rekor.

    Pre-Fulcio guards (raise SigningUnavailable before any network call):
      1. Issuer must be in v0 allowlist (finding-20260513-w5hq §2).
      2. JWT aud claim must be "sigstore" (finding-20260513-tx8r).
      3. expires_at, if present, must be in the future (clarification 3).
    """
    if oidc_provider.issuer not in _V0_OIDC_ALLOWLIST:
        raise SigningUnavailable(
            f"OIDC issuer {oidc_provider.issuer} not in v0 allowlist",
            component="oidc",
        )

    # finding-20260513-tx8r is normative: sign() MUST validate the aud claim
    # before Fulcio. A token that is not JWT-shaped cannot have its aud
    # validated, so it is rejected here — never silently passed through.
    payload = _parse_jwt_payload(oidc_provider.token)
    if payload is None:
        raise SigningUnavailable(
            "OIDC token is not JWT-shaped, cannot validate aud",
            component="oidc",
        )
    _check_aud(payload)

    if oidc_provider.expires_at is not None:
        if oidc_provider.expires_at <= _now_microseconds():
            raise SigningUnavailable(
                "OIDC token expired (expires_at is in the past)",
                component="oidc",
            )

    # TODO(brief-20260514-me2x §2): the K-A9 non-determinism property (two
    # signings of identical canonical_bytes must yield bit-different bundles —
    # the test that catches ECDSA nonce reuse) is NOT instrumented offline.
    # _test_factory.install_sign_monkeypatch replaces this sigstore-python
    # flow with _FakeSigner, which fabricates a fresh keypair/serial/log_index
    # /integrated_time every call, so synthetic bundles always differ by
    # construction regardless of nonce hygiene. Real non-determinism is only
    # exercised when this real flow runs (SKEIN_TEST_SIGSTORE_LIVE=1); the two
    # K-A9 tests are therefore @pytest.mark.staging. Phase 4 audits the real
    # signing pipeline per brief-20260514-me2x §2.
    #
    # Real sigstore-python flow. Wrap each phase so library exceptions map to
    # SigningUnavailable per d3u6 §5.
    try:
        ctx = _build_production_signing_context()
    except SigningUnavailable:
        raise
    except Exception as exc:
        component, reason = _classify_sign_exception(exc)
        raise SigningUnavailable(reason, component=component, cause=exc) from exc

    try:
        identity_token = _build_identity_token(oidc_provider.token)
    except SigningUnavailable:
        raise
    except Exception as exc:
        component, reason = _classify_sign_exception(exc)
        if component == "fulcio":  # token construction is OIDC-side
            component = "oidc"
        raise SigningUnavailable(reason, component=component, cause=exc) from exc

    try:
        with ctx.signer(identity_token) as signer:
            bundle = signer.sign_artifact(canonical_bytes)
    except SigningUnavailable:
        raise
    except Exception as exc:
        component, reason = _classify_sign_exception(exc)
        raise SigningUnavailable(reason, component=component, cause=exc) from exc

    leaf_cert = bundle.signing_certificate
    # Mirror the verify-side identity guards (lines 1245-1263). Falling back
    # to oidc_provider.issuer would surface the token's *claimed* issuer
    # rather than what is embedded in the cert; emitting subject="" would
    # diverge from the verify path, which rejects the same cert as
    # CERT_INVALID per finding-20260514-burb. Both fail closed.
    issuer = _extract_issuer_from_cert(leaf_cert)
    if not issuer:
        raise SigningUnavailable(
            "Fulcio issued cert without extractable OIDC issuer extension",
            component="fulcio",
        )
    subject = _extract_subject_from_cert(leaf_cert)
    if not subject:
        raise SigningUnavailable(
            "Fulcio issued cert with empty or malformed SAN",
            component="fulcio",
        )

    nb, na = _cert_validity_window(leaf_cert)
    rekor_proof = _build_rekor_inclusion_proof(bundle)

    integrated_time = bundle.log_entry._inner.integrated_time or 0
    signing_timestamp = int(integrated_time) * 1_000_000
    if signing_timestamp < MIN_MICROSECOND_TIMESTAMP:
        # j4w4 round-7 oracle A3 (parallel to _build_rekor_inclusion_proof):
        # falling back to cert notBefore here was silent. Surface the drift so
        # operators can see Rekor returned a below-floor (or zero) integrated_time.
        #
        # Dual-fire is intentional: _build_rekor_inclusion_proof above will
        # already have logged 'signing.rekor_integrated_time_below_floor' for
        # the proof-side floor substitution (wire value -> MIN_MICROSECOND_TIMESTAMP).
        # This WARN records the *distinct* sign-side substitution (wire value ->
        # cert notBefore) into SignResult.signing_timestamp. Two distinct
        # substitutions for one upstream anomaly deserve two distinct prefixes;
        # future readers seeing both warns from one sign() call should treat
        # that as designed, not as a duplicate-log bug.
        logger.warning(
            "signing.sign_integrated_time_below_floor: "
            "Rekor returned integrated_time=%s below "
            "MIN_MICROSECOND_TIMESTAMP; substituting cert notBefore",
            integrated_time,
        )
        signing_timestamp = nb

    evidence = Evidence(
        validity_window=(nb, na),
        rekor_inclusion=rekor_proof,
        cert_chain_summary=_cert_chain_summary(leaf_cert),
    )

    return SignResult(
        bundle_json=bundle.to_json(),
        issuer=issuer,
        subject=subject,
        signing_timestamp=signing_timestamp,
        evidence=evidence,
    )


class _TrustRootError(Exception):
    def __init__(self, status: VerifyStatus):
        self.status = status


def _select_verifier(trust_root_pin: str | None) -> Any:
    """Select a Verifier based on trust_root_pin (finding-20260513-w5hq §3).

    The state-driven branches below (trust_roots, current_root, offline,
    trust_root_missing, predates) are populated by the test factory via
    install_verify_monkeypatch() to exercise offline / stale-root / era-
    pin cases. Production code path must NOT see those values — a test
    that populated _verify_state and failed to clean up would otherwise
    influence subsequent production calls in the same process (gremlin
    finding #5). When _test_factory._test_active is False (production),
    we substitute an empty factory_state, so the rest of the function
    evaluates exactly as it would for the never-touched _verify_state == {}
    case. install_verify_monkeypatch() activates _test_active via
    monkeypatch.setattr so teardown auto-reverts to False.
    """
    factory_state = _test_factory._verify_state if _test_factory._test_active else {}
    trust_roots = factory_state.get("trust_roots") or []
    current_root = factory_state.get("current_trust_root")
    offline = factory_state.get("offline", False)
    trust_root_missing = factory_state.get("trust_root_missing", False)
    predates = factory_state.get("trust_root_predates_bundle", False)

    if predates:
        raise _TrustRootError(VerifyStatus.TRUST_ROOT_STALE)

    if trust_root_pin is not None:
        if offline and not trust_roots and not current_root:
            raise _TrustRootError(VerifyStatus.OFFLINE_NO_TRUSTED_ROOT)
        if not trust_roots and not current_root and trust_root_missing:
            raise _TrustRootError(VerifyStatus.OFFLINE_NO_TRUSTED_ROOT)
        for root in trust_roots:
            if getattr(root, "pin", None) == trust_root_pin:
                # finding-20260513-w5hq §3(a): the era-correct trust root must
                # validate the chain. If the matched root carries a
                # materializable sigstore TrustedRoot, build the verifier
                # around it.
                materialized = getattr(root, "trusted_root", None)
                if materialized is None and hasattr(root, "to_trusted_root"):
                    try:
                        materialized = root.to_trusted_root()
                    except Exception:  # noqa: BLE001
                        materialized = None
                if materialized is not None:
                    return Verifier(trusted_root=materialized)
                # finding-20260513-w5hq §3(a) requires VERIFIED only when the
                # bundle validates *under that era's trust root*. If we cannot
                # construct that root, we cannot honor the pin — falling back
                # to the current TUF root would silently verify against a
                # different root than the bundle pinned, which is §3(c)
                # territory (fail closed). Oracle B-review O-A6.
                logger.warning(
                    "signing.trust_root_pin_era_unmaterialized: pin %s matched "
                    "a known era root but no materializable TrustedRoot is "
                    "available; failing closed with TRUST_ROOT_STALE.",
                    trust_root_pin,
                )
                raise _TrustRootError(VerifyStatus.TRUST_ROOT_STALE)
        raise _TrustRootError(VerifyStatus.TRUST_ROOT_STALE)

    if trust_root_missing or (offline and not trust_roots and not current_root):
        raise _TrustRootError(VerifyStatus.OFFLINE_NO_TRUSTED_ROOT)

    return _build_production_verifier()


_BUNDLE_TOP_LEVEL_FIELDS: frozenset[str] = frozenset(
    {
        "mediaType",
        "media_type",
        "verificationMaterial",
        "verification_material",
        "messageSignature",
        "message_signature",
        "dsseEnvelope",
        "dsse_envelope",
    }
)


# sigstore-public-v1 pins SHA-256. SHA2_256 is the proto HashAlgorithm enum
# name; "sha256" is the alias some encoders emit. Nothing else is in profile.
_ALLOWED_DIGEST_ALGORITHMS: frozenset[str] = frozenset({"SHA2_256", "sha256"})
_SHA256_DIGEST_LEN = 32


# Exactly the two canonical sigstore-bundle v0.3 mediaType strings emitted by
# sigstore-python (Bundle.BundleType.BUNDLE_0_3 and BUNDLE_0_3_ALT). Used by
# the v0.3-specific profile gate; future versions need their own gate, not a
# substring-broadened match on this set.
_V0_3_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.dev.sigstore.bundle.v0.3+json",
        "application/vnd.dev.sigstore.bundle+json;version=0.3",
    }
)


def _check_sigstore_public_v1_profile(obj: object) -> VerifyStatus | None:
    """Enforce sigstore-public-v1 profile constraints on the raw bundle JSON.

    Returns a non-VERIFIED status if a constraint is violated, else None.
    """
    if not isinstance(obj, dict):
        return VerifyStatus.BUNDLE_MALFORMED

    # 1. sigstore-public-v1 pins SHA-256. The declared algorithm must be
    #    SHA2_256 (or the sha256 alias); SHA1/MD5/SHA2_384/SHA2_512 or any
    #    other value is out of profile. The digest must also be 32 bytes.
    #
    #    Only enforce when messageSignature is present in the bundle. DSSE
    #    bundles (dsseEnvelope) have no messageSignature and are rejected
    #    later in _evaluate_bundle as "DSSE envelope not supported in v0";
    #    the profile gate must not pre-reject them here on missing
    #    messageDigest.
    #
    #    Prior implementation guarded the algorithm check with
    #    `algo is not None and ...` and the digest length check with
    #    `if algo and digest_b64`. That meant a messageSignature whose
    #    messageDigest.algorithm was None (or absent) bypassed both checks
    #    silently — a defense-in-depth gap; sigstore-python still rejects
    #    downstream, but the documented profile gate had no opinion. The
    #    presence-of-messageSignature now bifurcates the enforcement.
    msg_sig_raw = obj.get("messageSignature")
    if msg_sig_raw is not None:
        if not isinstance(msg_sig_raw, dict):
            return VerifyStatus.BUNDLE_MALFORMED
        msg_digest = msg_sig_raw.get("messageDigest")
        if not isinstance(msg_digest, dict):
            return VerifyStatus.BUNDLE_MALFORMED
        algo = msg_digest.get("algorithm")
        if not isinstance(algo, str) or algo not in _ALLOWED_DIGEST_ALGORITHMS:
            return VerifyStatus.BUNDLE_MALFORMED
        digest_b64 = msg_digest.get("digest")
        if not isinstance(digest_b64, str):
            return VerifyStatus.BUNDLE_MALFORMED
        try:
            digest = base64.b64decode(digest_b64)
        except (binascii.Error, ValueError):
            return VerifyStatus.BUNDLE_MALFORMED
        if len(digest) != _SHA256_DIGEST_LEN:
            return VerifyStatus.BUNDLE_MALFORMED

    # 2. v0.3 bundles: when an inclusion_promise (SET) is supplied alongside
    #    the mandatory inclusion_proof, the SET must be a structurally-valid
    #    DER signature (sigstore-public-v1 profile per
    #    finding-20260512-eaft actionable #3). A short or non-DER SET is the
    #    injection-style attack that the profile rejects.
    #
    #    mediaType match is exact equality against the two canonical v0.3
    #    strings emitted by sigstore-python (Bundle.BundleType.BUNDLE_0_3
    #    and BUNDLE_0_3_ALT). A substring match here would (a) trigger this
    #    v0.3-specific SET check on future versions whose mediaType contains
    #    "v0.3" or "version=0.3" as a substring (e.g. v0.30, v0.3.1) — those
    #    bundles need their own profile check, not piggybacking on v0.3's,
    #    and (b) accept attacker-supplied non-canonical mediaTypes that
    #    happen to contain the substring.
    #
    #    Absent mediaType (older bundles legitimately omit it) falls
    #    through as not-v0.3. Explicit non-string mediaType — most notably
    #    JSON ``null`` — is BUNDLE_MALFORMED: ``obj.get("mediaType", "")``
    #    only defaulted on key absence, so ``mediaType: null`` produced
    #    ``None``, ``None in _V0_3_MEDIA_TYPES`` was False, and the v0.3
    #    SET-DER defense was silently skipped. Unhashable non-strings
    #    (list, dict) additionally raised TypeError on the ``in`` check.
    #    Reject non-string mediaType outright; preserve absent-key
    #    passthrough by branching on ``in obj`` rather than ``.get``.
    if "mediaType" in obj:
        media_type = obj["mediaType"]
        if not isinstance(media_type, str):
            return VerifyStatus.BUNDLE_MALFORMED
    else:
        media_type = ""
    is_v3 = media_type in _V0_3_MEDIA_TYPES
    if is_v3:
        try:
            for entry in (obj.get("verificationMaterial") or {}).get(
                "tlogEntries"
            ) or []:
                set_obj = entry.get("inclusionPromise")
                # Absent inclusionPromise (key missing or JSON ``null``)
                # is the legitimate v0.3 shape for a tlog entry without
                # a SET — Rekor v2 RFC3161 TSA timestamps replace the
                # SET, and intermediate Rekor states may legitimately
                # carry an inclusionProof without one. Continue past:
                # no SET to DER-validate.
                if set_obj is None:
                    continue
                # Present-but-falsy non-dict shapes (``{}``, ``[]``,
                # ``False``, ``0``, ``""``) and present non-dict
                # truthy shapes (ints, strings, lists) are malformed.
                # Pre-fix, ``if not set_obj`` short-circuited on every
                # falsy value — including ``{}`` and ``[]`` — and
                # ``continue``d, silently skipping the
                # finding-20260519-74o7 SET-DER defense. Truthy non-
                # dicts (strings, ints) reached ``set_obj.get(...)`` and
                # raised AttributeError, caught by the outer
                # ``except Exception`` → BUNDLE_MALFORMED; the explicit
                # ``isinstance`` check here makes the typing rule the
                # primary gate. ``not set_obj`` covers the empty-dict
                # case: ``{}`` is a dict but content-less, and an
                # inclusionPromise object with no fields is malformed —
                # absence is expressed via key-missing / JSON ``null``,
                # not via empty-object.
                if not isinstance(set_obj, dict) or not set_obj:
                    return VerifyStatus.BUNDLE_MALFORMED
                proof_obj = entry.get("inclusionProof")
                # Mirror the inclusionPromise logic. The SET-DER guard
                # needs BOTH inclusionPromise and inclusionProof to be
                # well-formed dicts before it can run; absent
                # inclusionProof (None / key missing) skips the check
                # (sigstore-python handles missing proof downstream),
                # but a present-but-malformed inclusionProof is itself
                # a bundle-malformedness signal. Pre-fix,
                # ``not entry.get("inclusionProof")`` ``continue``d on
                # ``{}``, ``[]``, ``False``, ``0``, ``""`` and silently
                # disabled the SET-DER defense.
                if proof_obj is None:
                    continue
                if not isinstance(proof_obj, dict) or not proof_obj:
                    return VerifyStatus.BUNDLE_MALFORMED
                set_b64 = set_obj.get("signedEntryTimestamp")
                # Absent signedEntryTimestamp (None) is legitimate —
                # caller may have constructed an inclusionPromise dict
                # for a non-SET field (e.g. future proto growth) and
                # the SET-DER check has nothing to do.
                if set_b64 is None:
                    continue
                # signedEntryTimestamp must be a non-empty string. Pre-
                # fix, ``if not set_b64: continue`` skipped on falsy
                # non-string values (``{}``, ``[]``, ``0``, ``False``)
                # and the empty string, silently disabling the SET-DER
                # defense. Truthy non-string values (dict, list, int)
                # reached ``base64.b64decode(set_b64, validate=True)``
                # and raised TypeError — not caught by the inner
                # ``except (binascii.Error, ValueError)``, so it
                # propagated to the outer ``except Exception`` →
                # BUNDLE_MALFORMED (right outcome, wrong path). Make
                # the type+non-empty rule the primary gate.
                if not isinstance(set_b64, str) or not set_b64:
                    return VerifyStatus.BUNDLE_MALFORMED
                try:
                    raw = base64.b64decode(set_b64, validate=True)
                except (binascii.Error, ValueError):
                    return VerifyStatus.BUNDLE_MALFORMED
                # The SET must be a structurally-valid DER ECDSA-Sig-Value
                # (SEQUENCE { INTEGER r, INTEGER s }). decode_dss_signature is
                # a strict DER parse: it raises ValueError on a wrong tag,
                # non-DER length, non-INTEGER contents, indefinite-length form,
                # trailing garbage, OR a negative INTEGER — all the shapes a
                # len/0x30-prefix sniff would wave through.
                #
                # The explicit r/s check is NOT redundant (verified
                # empirically, finding-20260519-74o7): decode_dss_signature
                # raises on negative INTEGERs but RETURNS r=0 / s=0 from a
                # well-formed SEQUENCE without complaint. r=0 or s=0 is a
                # trivially-forgeable / invalid ECDSA signature, so this is the
                # operative guard for the zero case — the library does not
                # reject it for us. Defense-in-depth ahead of sigstore-python's
                # cryptographic SET verification; never the sole gate.
                try:
                    r, s = decode_dss_signature(raw)
                except ValueError:
                    return VerifyStatus.BUNDLE_MALFORMED
                if r <= 0 or s <= 0:
                    return VerifyStatus.BUNDLE_MALFORMED
        except Exception:  # noqa: BLE001
            # Any unexpected structural shape inside tlogEntries (entry not a
            # dict, set_obj not a dict, etc.) reaches the SET-DER defense as an
            # AttributeError / TypeError / KeyError / IndexError. Silently
            # skipping the check (the prior behavior) bypassed the
            # finding-20260519-74o7 defense — an adversarial bundle with
            # tlogEntries=["string"] would let entry.get(...) raise
            # AttributeError, swallow it, and return None (profile pass) without
            # the SET ever being DER-validated. Fail closed instead: structural
            # weirdness in v0.3 tlogEntries is itself a malformedness signal.
            return VerifyStatus.BUNDLE_MALFORMED

    return None


def _verify_single(
    canonical_bytes: bytes, blob: str, scheme: str, trust_root_pin: str | None
) -> VerifyResult:
    if scheme != "sigstore-public-v1":
        return VerifyResult(status=VerifyStatus.BUNDLE_MALFORMED)

    # Single-parse: profile check and the unknown-top-level-field strip both
    # operate on this one parsed dict; Bundle.from_json then sees the
    # re-serialized stripped blob. Eliminating the multi-parse seam closes
    # the latent TOCTOU shape where a profile-check pass on one parse could
    # diverge from Bundle.from_json's view on a second parse (Shard Q residual
    # from finding-20260520-j4w4).
    try:
        raw_obj = json.loads(blob)
    except (ValueError, UnicodeDecodeError):
        return VerifyResult(status=VerifyStatus.BUNDLE_MALFORMED)

    profile_status = _check_sigstore_public_v1_profile(raw_obj)
    if profile_status is not None:
        return VerifyResult(status=profile_status)

    # Strip unknown top-level fields for forward-compat. A future protocol
    # revision adding a sibling key to the bundle envelope must not block
    # verify (conformance test contract); strict pydantic parsing in
    # Bundle.from_json would otherwise reject the bundle on an additive field
    # alone. Bundle JSON is NOT byte-stable through verify() when a strip
    # occurs — re-serialization via json.dumps is not canonical sigstore
    # ProtoJSON; the parsed sigstore.models.Bundle is the authoritative form.
    # raw_obj is guaranteed to be a dict here (the profile check rejects
    # non-dict above), but keep the isinstance guard so the strip is
    # robust against future refactors of the profile check.
    if isinstance(raw_obj, dict):
        stripped_obj = {
            k: v for k, v in raw_obj.items() if k in _BUNDLE_TOP_LEVEL_FIELDS
        }
        if len(stripped_obj) != len(raw_obj):
            blob = json.dumps(stripped_obj)

    try:
        bundle = Bundle.from_json(blob)
    except sigstore.models.InvalidBundle:
        return VerifyResult(status=VerifyStatus.BUNDLE_MALFORMED)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return VerifyResult(status=VerifyStatus.BUNDLE_MALFORMED)
    except Exception as exc:
        return VerifyResult(status=_map_sigstore_exception(exc))

    # After parse: validate leaf cert pubkey is ECDSA P-256 (sigstore-public-v1
    # profile per finding-20260512-eaft actionable #3).
    try:
        from cryptography.hazmat.primitives.asymmetric import ec as _ec

        leaf_pub = bundle.signing_certificate.public_key()
        if not isinstance(leaf_pub, _ec.EllipticCurvePublicKey) or not isinstance(
            leaf_pub.curve, _ec.SECP256R1
        ):
            return VerifyResult(status=VerifyStatus.CERT_INVALID)
    except Exception:  # noqa: BLE001
        return VerifyResult(status=VerifyStatus.CERT_INVALID)

    try:
        verifier = _select_verifier(trust_root_pin)
    except _TrustRootError as exc:
        return VerifyResult(status=exc.status)

    try:
        verifier.verify_artifact(canonical_bytes, bundle, UnsafeNoOp())
    except Exception as exc:
        status = _map_sigstore_exception(exc)
        if status == VerifyStatus.IDENTITY_MISMATCH:
            status = VerifyStatus.BUNDLE_MALFORMED
        return VerifyResult(status=status)

    leaf_cert = bundle.signing_certificate
    issuer = _extract_issuer_from_cert(leaf_cert)
    subject = _extract_subject_from_cert(leaf_cert)

    # Falsy subject (None for missing-SAN / malformed-IA5 / empty-SAN policy;
    # also catches empty string as defense-in-depth in case any future
    # extraction path returns "") → CERT_INVALID per finding-20260514-burb.
    if not subject:
        return VerifyResult(
            status=VerifyStatus.CERT_INVALID, issuer=issuer, subject=None
        )
    # Symmetric guard on issuer: a Fulcio leaf without an extractable OIDC
    # issuer extension (V2 OID 1.3.6.1.4.1.57264.1.8 or legacy 1.3.6.1.4.1.
    # 57264.1.1) is malformed for the sigstore-public-v1 profile. Surfacing
    # this as VERIFIED with issuer=None would let policy comparisons against
    # a None issuer no-op silently downstream.
    if not issuer:
        return VerifyResult(
            status=VerifyStatus.CERT_INVALID, issuer=None, subject=subject
        )

    nb, na = _cert_validity_window(leaf_cert)
    evidence = Evidence(
        validity_window=(nb, na),
        rekor_inclusion=_build_rekor_inclusion_proof(bundle),
        cert_chain_summary=_cert_chain_summary(leaf_cert),
    )
    return VerifyResult(
        status=VerifyStatus.VERIFIED,
        issuer=issuer,
        subject=subject,
        evidence=evidence,
    )


def _coerce_signature_bundle(
    obj: SignatureBundle | Mapping[str, Any],
) -> SignatureBundle:
    """Inflate the persisted/wire signature_bundle into the domain model.

    The wire→domain inflation boundary is owned by verify()/verify_multi(),
    not the caller: SKEIN's write path persists the signature_bundle as JSON
    (see skein/tests/conformance/conftest.py::make_skein_bundle docstring), so the
    read path hands verify() a deserialized Mapping, not a live model. A
    SignatureBundle instance is accepted unchanged for in-memory callers.
    This is the spec-alignment decision recorded in finding-20260519-tg40,
    threaded to the Phase 2 lock (finding-20260514-6078); the locked contract
    pins it via the dict-passing tests in skein/tests/conformance.

    Pydantic ValidationError (missing required field, non-base64
    canonical_bytes) is the caller's signal to map BUNDLE_MALFORMED and is
    handled by the callers. EmptySignatureBundle (bundles=[]) and
    MultiSignerBundle propagate unwrapped — they are caller programming
    errors, not domain failures, per finding-20260511-d3u6 §2.
    """
    if isinstance(obj, SignatureBundle):
        return obj
    return SignatureBundle.model_validate(obj)


def verify(
    canonical_bytes: bytes, signature_bundle: SignatureBundle | Mapping[str, Any]
) -> VerifyResult:
    try:
        signature_bundle = _coerce_signature_bundle(signature_bundle)
    except ValidationError:
        return VerifyResult(status=VerifyStatus.BUNDLE_MALFORMED)
    n = len(signature_bundle.bundles)
    if n != 1:
        raise MultiSignerBundle(
            f"verify() requires exactly one signer; got {n}. "
            "Use verify_multi() for multi-signer bundles."
        )
    # Fail closed when the caller's canonical_bytes diverge from the bundle's
    # stored canonical_bytes field — that field is the bundle's own claim
    # about what was signed; disagreement is SIGNATURE_MISMATCH before any
    # crypto check (covers post-sign tampering of either side).
    if canonical_bytes != signature_bundle.canonical_bytes:
        return VerifyResult(status=VerifyStatus.SIGNATURE_MISMATCH)
    return _verify_single(
        canonical_bytes,
        signature_bundle.bundles[0],
        signature_bundle.identity_scheme,
        signature_bundle.trust_root_pin,
    )


def verify_multi(
    canonical_bytes: bytes,
    signature_bundle: SignatureBundle | Mapping[str, Any],
) -> MultiVerifyResult:
    try:
        signature_bundle = _coerce_signature_bundle(signature_bundle)
    except ValidationError:
        return MultiVerifyResult(
            results=[VerifyResult(status=VerifyStatus.BUNDLE_MALFORMED)],
            overall=VerifyStatus.BUNDLE_MALFORMED,
        )
    # Same fail-closed check as verify(): bundle's stored canonical_bytes is
    # its own attestation about what was signed; disagreement with caller's
    # bytes is SIGNATURE_MISMATCH for every signer.
    canonical_mismatch = canonical_bytes != signature_bundle.canonical_bytes
    results: list[VerifyResult] = []
    for blob in signature_bundle.bundles:
        if canonical_mismatch:
            results.append(VerifyResult(status=VerifyStatus.SIGNATURE_MISMATCH))
            continue
        try:
            r = _verify_single(
                canonical_bytes,
                blob,
                signature_bundle.identity_scheme,
                signature_bundle.trust_root_pin,
            )
        except Exception as exc:  # noqa: BLE001
            r = VerifyResult(status=_map_sigstore_exception(exc))
        results.append(r)

    overall = min(
        (r.status for r in results),
        key=lambda s: _SEVERITY_RANK[s],
    )
    return MultiVerifyResult(results=results, overall=overall)


# ===========================================================================
# Test factory (skein.signing._test_factory).
# ===========================================================================


class _SignatureInvalid(sigstore.errors.VerificationError):
    """Synthesized SIG_MISMATCH signal that maps via _map_sigstore_exception."""


class _TokenExpiredMidFlow(Exception):
    """Synthesized oracle #6 mid-flow OIDC expiry."""


def _synthesize_exception(name: str, msg: str = "") -> BaseException:
    """Build an exception instance whose __class__.__name__ == name."""
    real_classes: dict[str, type] = {
        "InvalidBundle": sigstore.models.InvalidBundle,
        "VerificationError": sigstore.errors.VerificationError,
        "CertValidationError": sigstore.errors.CertValidationError,
        "NetworkError": sigstore.errors.NetworkError,
        "TUFError": sigstore.errors.TUFError,
        "MetadataError": sigstore.errors.MetadataError,
        "RootError": sigstore.errors.RootError,
        "IdentityError": sigstore.oidc.IdentityError,
        "ExpiredIdentity": sigstore.oidc.ExpiredIdentity,
        "IssuerError": sigstore.oidc.IssuerError,
        "TimeoutError": TimeoutError,
        "ConnectionError": ConnectionError,
        "TokenExpiredMidFlow": _TokenExpiredMidFlow,
        "_TokenExpiredMidFlow": _TokenExpiredMidFlow,
    }
    if name in real_classes:
        cls = real_classes[name]
        try:
            instance: BaseException = cls(msg) if msg else cls()
            return instance
        except TypeError:
            try:
                fallback: BaseException = cls(msg)
                return fallback
            except Exception:  # noqa: BLE001
                return Exception(msg)
    # Private classes from sigstore internals.
    if name == "FulcioClientError":
        try:
            from sigstore._internal.fulcio.client import FulcioClientError

            return FulcioClientError(msg)
        except Exception:  # noqa: BLE001
            pass
    if name == "RekorClientError":
        try:
            from sigstore._internal.rekor import RekorClientError

            return RekorClientError(msg)
        except Exception:  # noqa: BLE001
            pass
    if name == "ExpiredCertificate":
        try:
            from sigstore._internal.fulcio.client import ExpiredCertificate

            return ExpiredCertificate(msg)
        except Exception:  # noqa: BLE001
            pass
    if name == "TimestampError":
        try:
            from sigstore._internal.timestamp import TimestampError

            return TimestampError(msg)
        except Exception:  # noqa: BLE001
            pass

    # Synthesize as ad-hoc subclass of Exception (NOT VerificationError) per
    # conftest.py:267-281 — exercises the catch-all branch.
    synth: type[Exception] = type(name, (Exception,), {})
    return synth(msg)


_SIGN_FAILURE_MODES: dict[str, tuple[str, str]] = {
    "fulcio_503": ("FulcioClientError", "fulcio HTTP 503"),
    "fulcio_400_unknown_csr": ("FulcioClientError", "fulcio HTTP 400 unknown CSR"),
    "fulcio_status_503": ("FulcioClientError", "fulcio HTTP 503"),
    "rekor_502": ("RekorClientError", "rekor HTTP 502"),
    "rekor_timeout": ("RekorClientError", "rekor timeout"),
    "rekor_404": ("RekorClientError", "rekor HTTP 404"),
    "oidc_unreachable": ("IdentityError", "oidc unreachable"),
    "oidc_token_rejected": ("IdentityError", "oidc token rejected"),
    "tuf_metadata_stale": ("TUFError", "tuf metadata stale"),
    "expired_cert": ("ExpiredCertificate", "cert expired"),
    "network_down": ("NetworkError", "network unreachable"),
    "tuf_unavailable": ("TUFError", "tuf unavailable"),
    "rekor_after_fulcio_503": ("RekorClientError", "rekor unavailable after fulcio"),
    "tsa_unavailable_rekor_v2": ("TimestampError", "tsa unavailable"),
    "oidc_token_expired_mid_flow": (
        "_TokenExpiredMidFlow",
        "token expired during signing",
    ),
}


class _TestTrustRoot:
    def __init__(self, era: str = "current", **opts: Any) -> None:
        self.era = era
        # unmaterializable=True simulates the v0/era-root gap where a pin
        # matches a known era root but no real sigstore TrustedRoot can be
        # constructed for it (Oracle B-review O-A6). The default materializes
        # to a sentinel so _select_verifier's §3(a) branch returns a Verifier
        # rather than failing closed.
        self._unmaterializable = opts.pop("unmaterializable", False)
        self.opts = opts
        raw = era + ":" + json.dumps(opts, sort_keys=True, default=str)
        self.pin = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_trusted_root(self) -> Any:
        if self._unmaterializable:
            return None
        return _SentinelTrustedRoot(self.era)


class _SentinelTrustedRoot:
    """Stand-in for sigstore.models.TrustedRoot in the test factory."""

    def __init__(self, era: str) -> None:
        self.era = era


class _TestCertificate:
    def __init__(self, *, curve: str, private_key: Any, certificate: Any) -> None:
        self.curve = curve
        self.private_key = private_key
        self.certificate = certificate


class _ParsedCheckpoint:
    def __init__(self, tree_size: int, root_hash: str, log_id: str) -> None:
        self.tree_size = tree_size
        self.root_hash = root_hash
        self.log_id = log_id


class _SignSpy:
    def __init__(self) -> None:
        self.fulcio_call_count = 0
        self.leaked_cert = False


class _FakeSigner:
    def __init__(self, factory: "_TestFactory", spy: _SignSpy) -> None:
        self.factory = factory
        self.spy = spy

    def sign_artifact(self, canonical_bytes: bytes) -> Bundle:
        opts = self.factory._sign_state
        failure = opts.get("failure_mode")
        raise_exc_name = opts.get("raise_sigstore_exception")
        raise_exc_msg = opts.get("raise_exc_msg", "")
        if raise_exc_name is None and failure in _SIGN_FAILURE_MODES:
            raise_exc_name, raise_exc_msg = _SIGN_FAILURE_MODES[failure]

        pre_fulcio_failures = {
            "oidc_unreachable",
            "oidc_token_rejected",
            "tuf_metadata_stale",
            "tuf_unavailable",
            "oidc_token_expired_mid_flow",
        }
        if failure in pre_fulcio_failures and raise_exc_name:
            raise _synthesize_exception(raise_exc_name, raise_exc_msg)

        if raise_exc_name:
            self.spy.fulcio_call_count += 1
            raise _synthesize_exception(raise_exc_name, raise_exc_msg)

        # CA-not-trusted: bundle minted, but tagged as unknown_ca so verify reports CERT_INVALID.
        if opts.get("ca") == "unknown-ca":
            self.spy.fulcio_call_count += 1
            opts2 = dict(opts)
            opts2["unknown_ca"] = True
            return self.factory._build_bundle(canonical_bytes, opts2)

        # Non-ASCII identity rejection.
        identity = opts.get("identity") or ""
        if opts.get("fulcio_rejects_non_ascii") and not identity.isascii():
            self.spy.fulcio_call_count += 1
            raise _synthesize_exception(
                "FulcioClientError",
                "Fulcio rejected non-ASCII identity in CSR",
            )

        # Cached-cert expiry on second call (sigstore-python #1729 regression).
        if opts.get("cert_expired_on_second_call"):
            key = id(opts)
            count = self.factory._sign_call_count_per_provider.get(key, 0)
            self.factory._sign_call_count_per_provider[key] = count + 1
            if count >= 1:
                self.spy.fulcio_call_count += 1
                raise _synthesize_exception(
                    "ExpiredCertificate",
                    "cached signing cert has expired mid-batch",
                )

        # simulate_tuf_race: must not leak raw exceptions across threads. Just succeed.
        self.spy.fulcio_call_count += 1
        return self.factory._build_bundle(canonical_bytes, opts)


class _FakeSigningContext:
    def __init__(self, factory: "_TestFactory", spy: _SignSpy) -> None:
        self.factory = factory
        self.spy = spy

    @contextlib.contextmanager
    def signer(
        self, identity_token: Any, *, cache: bool = True
    ) -> Iterator[_FakeSigner]:
        yield _FakeSigner(self.factory, self.spy)


class _FakeVerifier:
    def __init__(self, factory: "_TestFactory") -> None:
        self.factory = factory

    def verify_artifact(self, input_: bytes, bundle: Bundle, policy: Any) -> None:
        opts = self.factory._verify_state
        exc_name = opts.get("raise_sigstore_exception")
        if exc_name:
            raise _synthesize_exception(exc_name, f"factory-injected {exc_name}")

        failure = opts.get("failure_mode")
        if failure:
            verify_failures = {
                "rekor_unreachable": ("RekorClientError", "rekor unreachable"),
                "rekor_timeout": ("RekorClientError", "rekor timeout"),
                "tuf_unavailable_no_cached_root": (
                    "TUFError",
                    "tuf unavailable, no cached root",
                ),
                "bundle_parse_error": ("InvalidBundle", "bundle parse error"),
            }
            if failure in verify_failures:
                name, msg = verify_failures[failure]
                raise _synthesize_exception(name, msg)

        if opts.get("raise_in_verifier"):
            raise _synthesize_exception(
                "VerificationError",
                "factory-injected verifier internal error",
            )

        if opts.get("network") == "down":
            raise _synthesize_exception("NetworkError", "network down")

        try:
            msg_sig = bundle._inner.message_signature
            sig_bytes = bytes(msg_sig.signature) if msg_sig else b""
        except Exception:  # noqa: BLE001
            sig_bytes = b""

        meta = self.factory._registry.get(sig_bytes)
        if meta is not None:
            outcome = self.factory._evaluate_bundle(meta, bundle, input_)
            if outcome is not None:
                raise outcome
            # Even for known bundles, run a real ECDSA verify pass so
            # bit-flip mutations of the cert/signature/digest fields are
            # caught (otherwise the registry-hit short-circuit hides them).
            if not _real_ecdsa_verify(bundle, input_, sig_bytes):
                raise _SignatureInvalid("Signature is invalid for input")
            return

        # Bundle not in registry: verify the signature against the input the
        # caller is asking us to confirm. If sig doesn't validate → mirror
        # sigstore-python's wording so _map_sigstore_exception sees
        # SIGNATURE_MISMATCH.
        if not _real_ecdsa_verify(bundle, input_, sig_bytes):
            raise _SignatureInvalid("Signature is invalid for input")
        return


class _FakeSigningContextClass:
    """Class-shaped stand-in for sigstore.sign.SigningContext."""

    def __init__(self, factory: "_TestFactory", spy: _SignSpy) -> None:
        self._factory = factory
        self._spy = spy

    def production(self) -> _FakeSigningContext:
        return _FakeSigningContext(self._factory, self._spy)


class _FakeVerifierClass:
    def __init__(self, factory: "_TestFactory", fake: _FakeVerifier) -> None:
        self._factory = factory
        self._fake = fake

    def production(self, *, offline: bool = False) -> _FakeVerifier:
        return self._fake

    def staging(self, *, offline: bool = False) -> _FakeVerifier:
        return self._fake

    def __call__(self, *, trusted_root=None) -> _FakeVerifier:
        return self._fake


class _FakeIdentityToken:
    def __init__(self, raw_token: str) -> None:
        self._raw = raw_token
        self.identity = "test-identity"
        self.issuer = "https://test-issuer.example"
        self.federated_issuer = self.issuer


def _FakeIdentityTokenFactory(raw_token: str) -> _FakeIdentityToken:
    return _FakeIdentityToken(raw_token)


def _real_ecdsa_verify(bundle: Bundle, input_bytes: bytes, sig_bytes: bytes) -> bool:
    """Verify ECDSA signature against the bundle's leaf pubkey + input digest.

    Used by the fake verifier to catch bit-flip mutations of the cert,
    signature, or message digest fields. Returns False on any failure.

    Also verifies the leaf cert's own signature against the factory's test
    CA — bit flips inside the leaf cert body break this even if the pubkey
    is untouched.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric import ec as _ec

        # Check that the bundle's stored message_digest matches the input.
        msg_sig = bundle._inner.message_signature
        digest = hashlib.sha256(input_bytes).digest()
        if (
            msg_sig
            and msg_sig.message_digest
            and msg_sig.message_digest.digest != digest
        ):
            return False
        leaf = bundle.signing_certificate
        # Cert chain check: validate the leaf's signature against the factory CA.
        ca_cert = _test_factory._ca_cert
        try:
            ca_pub = ca_cert.public_key()
            assert isinstance(ca_pub, _ec.EllipticCurvePublicKey)
            hash_algo = leaf.signature_hash_algorithm
            assert hash_algo is not None
            ca_pub.verify(
                leaf.signature,
                leaf.tbs_certificate_bytes,
                _ec.ECDSA(hash_algo),
            )
        except Exception:  # noqa: BLE001
            return False
        pub = leaf.public_key()
        if not isinstance(pub, _ec.EllipticCurvePublicKey):
            return True
        pub.verify(sig_bytes, digest, _ec.ECDSA(Prehashed(hashes.SHA256())))
        return True
    except Exception:  # noqa: BLE001
        return False


def _set_is_verified_time_source(canonical_obj: dict) -> bool:
    """Mirror sigstore.verify.verifier.Verifier._establish_time's SET clause.

    SET (Signed Entry Timestamp) counts as a verified time source iff all of:
        inclusion_promise.signed_entry_timestamp is truthy
        integrated_time is truthy
        kindVersion.kind in {"hashedrekord", "dsse"}
        kindVersion.version == "0.0.1"
    See sigstore/verify/verifier.py:228-232. The kindVersion clause matters:
    an exotic kindVersion raises at verifier.py:233 BEFORE SET counts, so a
    looser gate would over-loosen on that case.
    """
    vm = canonical_obj.get("verificationMaterial") or {}
    tlogs = vm.get("tlogEntries") or [{}]
    te = tlogs[0] or {}
    promise = (te.get("inclusionPromise") or {}).get("signedEntryTimestamp")
    integrated = te.get("integratedTime")
    kv = te.get("kindVersion") or {}
    return (
        bool(promise)
        and bool(integrated)
        and kv.get("kind") in ("hashedrekord", "dsse")
        and kv.get("version") == "0.0.1"
    )


_CHECKPOINT_SIG_LINE_RE = re.compile(r"— (\S+) (\S+)")


def _checkpoint_envelope_drift_is_benign(env_o: str, env_c: str) -> bool:
    """True iff env_o vs env_c differ only in the decorative signer-name spans.

    Mirrors SignedNote.from_text at sigstore/_internal/rekor/checkpoint.py:128-164:
    the signed prefix is text up to and including the '\\n' before the
    '\\n\\n' separator. The signature block is one or more
    '\\u2014 NAME b64sig\\n' lines. Verification at checkpoint.py:166-187
    binds (a) the signed prefix bytes against the decoded signature body,
    (b) the em-dash + line structure (the regex must parse), and (c) the
    decoded sig_hash (first 4 bytes) against key_id[:4]. The NAME token
    is never fed into verify — it may drift.
    """
    sep = "\n\n"
    if env_o.count(sep) != 1 or env_c.count(sep) != 1:
        return False
    split_o = env_o.index(sep)
    split_c = env_c.index(sep)
    if env_o[: split_o + 1] != env_c[: split_c + 1]:
        return False
    sig_block_o = env_o[split_o + len(sep):]
    sig_block_c = env_c[split_c + len(sep):]
    if not sig_block_o or not sig_block_c:
        return False
    if sig_block_o[-1] != "\n" or sig_block_c[-1] != "\n":
        return False
    lines_o = sig_block_o.split("\n")
    lines_c = sig_block_c.split("\n")
    if len(lines_o) != len(lines_c):
        return False
    for lo, lc in zip(lines_o, lines_c):
        if lo == lc:
            continue
        mo = _CHECKPOINT_SIG_LINE_RE.fullmatch(lo)
        mc = _CHECKPOINT_SIG_LINE_RE.fullmatch(lc)
        if not mo or not mc:
            return False
        try:
            sig_o = base64.b64decode(mo.group(2))
            sig_c = base64.b64decode(mc.group(2))
        except (binascii.Error, ValueError):
            return False
        if len(sig_o) < 4 or len(sig_c) < 4:
            return False
        if sig_o != sig_c:
            return False
    return True


def _inclusion_proof_drift_exception(
    ip_o: dict, ip_c: dict
) -> BaseException | None:
    """Reject inclusionProof drift outside the checkpoint envelope signer-name.

    rootHash/hashes/treeSize/logIndex are bound by Merkle inclusion verify; any
    drift fails real verify. Within checkpoint, only envelope's decorative
    signer-name token may drift (see _checkpoint_envelope_drift_is_benign).
    Unknown subfields stay strict.
    """
    for k in ("rootHash", "hashes", "treeSize", "logIndex"):
        if ip_o.get(k) != ip_c.get(k):
            return _synthesize_exception(
                "InvalidRekorEntry", f"tlog inclusionProof.{k} drifted"
            )
    known = {"rootHash", "hashes", "treeSize", "logIndex", "checkpoint"}
    extra_o = {k: v for k, v in ip_o.items() if k not in known}
    extra_c = {k: v for k, v in ip_c.items() if k not in known}
    if extra_o != extra_c:
        return _synthesize_exception(
            "InvalidRekorEntry", "tlog inclusionProof drifted"
        )
    cp_o = ip_o.get("checkpoint") or {}
    cp_c = ip_c.get("checkpoint") or {}
    if cp_o == cp_c:
        return None
    for k in set(cp_o.keys()) | set(cp_c.keys()):
        if k == "envelope":
            continue
        if cp_o.get(k) != cp_c.get(k):
            return _synthesize_exception(
                "InvalidRekorEntry",
                f"tlog inclusionProof.checkpoint.{k} drifted",
            )
    env_o = cp_o.get("envelope") or ""
    env_c = cp_c.get("envelope") or ""
    if env_o != env_c and not _checkpoint_envelope_drift_is_benign(env_o, env_c):
        return _synthesize_exception(
            "InvalidRekorEntry", "tlog inclusionProof.checkpoint.envelope drifted"
        )
    return None


def _tsa_decodes_cleanly_for_real_verify(bundle: Bundle) -> BaseException | None:
    """Reject if the bundle's TSA bytes would propagate a non-silent exception
    out of sigstore-python's real verify path.

    The brief's TSA-with-SET loosening (finding-20260521-33nj) only holds when
    real verify silently *drops* the failed TSA result. That silent drop is
    keyed on Rfc3161VerificationError at sigstore/verify/verifier.py:147; the
    list comprehension at verifier.py:192-196 then filters out the None
    return. But several deeper rejections surface as other exception classes
    that propagate out of verify_artifact and reject the bundle outright:

    - ASN.1 parse failure → TimestampVerificationData._verify raises
      VerificationError, instantiated via the bundle's lazy property
      (sigstore/models.py:254-269).
    - PKIStatus invalid → rfc3161_client/verify.py:210 dereferences
      PKIStatus(status); an out-of-range int raises ValueError, not
      Rfc3161VerificationError.
    - Lazy tst_info ASN.1 parse failure → verify_message touches
      ``timestamp_response.tst_info.message_imprint`` etc.
      (rfc3161_client/verify.py:186); a malformed inner structure surfaces
      as ValueError.

    Mirror all of them, so the factory rejects exactly the same set real does.
    """
    try:
        tvd = bundle.verification_material.timestamp_verification_data
    except Exception:  # noqa: BLE001
        return _synthesize_exception(
            "InvalidBundle", "TSA timestamp bytes unparseable"
        )
    if tvd is None:
        return None
    try:
        from rfc3161_client import PKIStatus  # transitive via sigstore-python
    except ImportError:
        return None
    for tsr in tvd.rfc3161_timestamps:
        try:
            PKIStatus(tsr.status)
        except ValueError:
            return _synthesize_exception(
                "InvalidBundle", "TSA timestamp status enum invalid"
            )
        try:
            # Touch the same lazy ASN.1 surface verify_message reaches into
            # (rfc3161_client/verify.py:186) so malformed inner tokens raise
            # here rather than slipping past the factory's drift detector.
            _ = tsr.tst_info.message_imprint.hash_algorithm
        except Exception:  # noqa: BLE001
            return _synthesize_exception(
                "InvalidBundle", "TSA timestamp tst_info malformed"
            )
    return None


def _residual_canonical_drift(o: dict, c: dict) -> bool:
    """True if any region we don't explicitly bind has drifted.

    Used as a conservative backstop after the per-region checks accept any
    benign-loosened drift: surfaces drift in mediaType, tlogEntries[1+], or
    schema fields we don't recognize.
    """
    def _stripped(d: dict) -> dict:
        out = dict(d)
        out.pop("messageSignature", None)
        vm = dict(out.get("verificationMaterial") or {})
        vm.pop("certificate", None)
        vm.pop("timestampVerificationData", None)
        tlogs = list(vm.get("tlogEntries") or [])
        if tlogs:
            te = dict(tlogs[0] or {})
            for k in (
                "canonicalizedBody",
                "inclusionPromise",
                "logId",
                "integratedTime",
                "kindVersion",
                "inclusionProof",
            ):
                te.pop(k, None)
            tlogs[0] = te
        vm["tlogEntries"] = tlogs
        out["verificationMaterial"] = vm
        return out

    return _stripped(o) != _stripped(c)


def _canonical_drift_exception(meta: dict, bundle: Bundle) -> BaseException | None:
    """Detect an unflagged mutation of a cryptographically-bound region.

    sigstore.models.Bundle ProtoJSON round-trips idempotently, so the minted
    canonical snapshot (meta["_canonical_json"]) equals bundle.to_json() for
    an untouched bundle and for padding-bit / whitespace / key-order noise.
    A real change to a bound region drifts it. Explicit tamper helpers set
    flags handled earlier in _evaluate_bundle; this closes the fuzz/bit-flip
    path so the security-load-bearing property test actually exercises
    rejection.

    Per-region behavior mirrors real sigstore-python's empirical bit-flip
    rejection set (finding-20260521-33nj):

    - messageSignature, verificationMaterial.certificate,
      tlogEntries[0].{canonicalizedBody, inclusionPromise, logId,
      integratedTime, kindVersion}: strict reject on any drift.
    - tlogEntries[0].inclusionProof.{rootHash, hashes, treeSize, logIndex}:
      strict reject (bound by Merkle inclusion verify).
    - tlogEntries[0].inclusionProof.checkpoint.envelope: signed prefix +
      em-dash bytes + decoded sig_hash (first 4 bytes, matched against
      key_id[:4] at sigstore/_internal/rekor/checkpoint.py:174) + decoded
      sig body are strict; the decorative signer-name token between
      '\\u2014 ' and the b64 signature is NOT bound by real verify (see
      SignedNote.from_text/verify at checkpoint.py:128-187) and may drift.
    - timestampVerificationData: strict reject only when SET is NOT the
      verified time source. Per sigstore-python verifier.py:228-232 SET
      counts when inclusion_promise + integrated_time are set AND
      kindVersion is hashedrekord/dsse 0.0.1; in that case failed TSA
      results are silently dropped at verifier.py:192-196 while SET still
      meets VERIFIED_TIME_THRESHOLD=1 (verifier.py:67). When SET is not the
      time source, TSA is the only time anchor and must bind.
    - Drift in any region not explicitly validated above: conservative
      reject via _residual_canonical_drift.
    """
    orig = meta.get("_canonical_json")
    if orig is None:
        return None
    try:
        cur = bundle.to_json()
    except Exception:  # noqa: BLE001
        return _synthesize_exception("InvalidBundle", "bundle re-serialization failed")
    if cur == orig:
        return None
    try:
        o = json.loads(orig)
        c = json.loads(cur)
    except (ValueError, UnicodeDecodeError):
        return _synthesize_exception("InvalidBundle", "bundle canonical form drifted")

    if o.get("messageSignature") != c.get("messageSignature"):
        return _SignatureInvalid("Signature is invalid for input")
    vm_o = o.get("verificationMaterial") or {}
    vm_c = c.get("verificationMaterial") or {}
    if vm_o.get("certificate") != vm_c.get("certificate"):
        return _synthesize_exception(
            "InvalidMaterials", "leaf certificate bytes drifted"
        )
    te_o = (vm_o.get("tlogEntries") or [{}])[0] or {}
    te_c = (vm_c.get("tlogEntries") or [{}])[0] or {}
    for k in ("canonicalizedBody", "inclusionPromise"):
        if te_o.get(k) != te_c.get(k):
            return _synthesize_exception("InvalidBundle", f"tlog {k} drifted")
    for k in ("logId", "integratedTime", "kindVersion"):
        if te_o.get(k) != te_c.get(k):
            return _synthesize_exception("InvalidRekorEntry", f"tlog {k} drifted")
    ip_o = te_o.get("inclusionProof") or {}
    ip_c = te_c.get("inclusionProof") or {}
    if ip_o != ip_c:
        ip_exc = _inclusion_proof_drift_exception(ip_o, ip_c)
        if ip_exc is not None:
            return ip_exc
    if vm_o.get("timestampVerificationData") != vm_c.get("timestampVerificationData"):
        if not _set_is_verified_time_source(o):
            return _synthesize_exception("InvalidBundle", "TSA timestamp drifted")
        # SET is the verified time source — real verify silently drops TSA
        # failures that surface as Rfc3161VerificationError (verifier.py:147,
        # 192-196). But several deeper checks raise OTHER exceptions that
        # propagate out of verify_artifact and reject the bundle: ASN.1 parse
        # failure inside TimestampVerificationData._verify (models.py:254-269)
        # and PKIStatus enum validation inside Rfc3161 verify_message
        # (rfc3161_client/verify.py:210). Mirror both — anything beyond a
        # silently-dropped Rfc3161VerificationError must reject.
        tsr_exc = _tsa_decodes_cleanly_for_real_verify(bundle)
        if tsr_exc is not None:
            return tsr_exc
    if _residual_canonical_drift(o, c):
        return _synthesize_exception("InvalidBundle", "bundle canonical form drifted")
    return None


class _TestFactory:
    """Test fixture for skein.signing per conftest.py:248-486 contract."""

    def __init__(self) -> None:
        self._registry: dict[bytes, dict] = {}
        self._sign_state: dict[str, Any] = {}
        self._verify_state: dict[str, Any] = {}
        # _test_active is the gate _select_verifier consults before reading
        # _verify_state. Production code sees False and substitutes an empty
        # factory_state, so stale _verify_state from a prior test cannot
        # change production behavior (gremlin finding #5).
        # install_verify_monkeypatch() activates it via monkeypatch.setattr
        # so the False default is restored on teardown.
        self._test_active: bool = False
        self._sign_call_count_per_provider: dict[int, int] = {}
        self._verify_time_us: int | None = None
        self.fulcio_call_count: int = 0
        self._integrated_time_counter: int = 0
        self._cert_jitter_counter: int = 0
        # Shared synthetic CA (one per process).
        self._ca_key = ec.generate_private_key(ec.SECP256R1())
        self._ca_cert = self._build_ca()

    def _reset_sign(self) -> None:
        self._sign_state = {}
        self._sign_call_count_per_provider = {}
        self.fulcio_call_count = 0

    def _reset_verify(self) -> None:
        self._verify_state = {}
        self._verify_time_us = None

    def _reset(self) -> None:
        self._reset_sign()
        self._reset_verify()

    # ------------------------------------------------------------------
    # CA + cert minting.
    # ------------------------------------------------------------------

    def _build_ca(self) -> x509.Certificate:
        subject = issuer = x509.Name(
            [x509.NameAttribute(x509.NameOID.COMMON_NAME, "skein-test-ca")]
        )
        now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(self._ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            )
            .sign(self._ca_key, hashes.SHA256())
        )

    def _mint_leaf_cert(
        self,
        *,
        private_key: Any,
        identity: str,
        issuer: str,
        san_type: str = "rfc822Name",
        extra_sans: list[tuple[str, str]] | None = None,
        not_before: datetime.datetime | None = None,
        not_after: datetime.datetime | None = None,
        issuer_oid_variant: str = "both",
        issuer_oid_v2: str | None = None,
        issuer_oid_legacy: str | None = None,
        include_san: bool = True,
        san_value: str | None = None,
        malformed_ia5: bool = False,
        include_sct: bool = True,
    ) -> x509.Certificate:
        # Monotonic per-cert jitter so non-determinism tests see distinct
        # validity windows across sign() calls. X.509 not_valid_before/after
        # are second-resolution after the round trip through cryptography, so
        # jitter at the second level. Window stays inside Fulcio's
        # ~10-minute envelope (test_verify_verified_carries_evidence pins
        # end-start <= 11 minutes).
        now = datetime.datetime.now(datetime.timezone.utc)
        self._cert_jitter_counter += 1
        jitter_s = self._cert_jitter_counter % 60  # 0..59 seconds
        nb = not_before or (now - datetime.timedelta(seconds=120))
        na = not_after or (now + datetime.timedelta(seconds=300 + jitter_s))
        if nb.tzinfo is None:
            nb = nb.replace(tzinfo=datetime.timezone.utc)
        if na.tzinfo is None:
            na = na.replace(tzinfo=datetime.timezone.utc)

        san_value = san_value if san_value is not None else identity
        sans: list[x509.GeneralName] = []
        if include_san:
            sans.extend(
                self._build_san_entries(san_type, san_value, extra_sans, malformed_ia5)
            )

        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([]))
            .issuer_name(self._ca_cert.subject)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(nb)
            .not_valid_after(na)
        )
        if include_san and sans:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(sans),
                critical=True,
            )

        def _utf8string_der(s: str) -> bytes:
            data = s.encode("utf-8")
            return _der_utf8(data)

        v2_value = issuer_oid_v2 if issuer_oid_v2 is not None else issuer
        legacy_value = issuer_oid_legacy if issuer_oid_legacy is not None else issuer
        if issuer_oid_variant in ("both", "v2"):
            builder = builder.add_extension(
                x509.UnrecognizedExtension(
                    x509.ObjectIdentifier(_OID_ISSUER_V2),
                    _utf8string_der(v2_value),
                ),
                critical=False,
            )
        if issuer_oid_variant in ("both", "legacy"):
            builder = builder.add_extension(
                x509.UnrecognizedExtension(
                    x509.ObjectIdentifier(_OID_ISSUER_LEGACY),
                    legacy_value.encode("utf-8"),
                ),
                critical=False,
            )
        # SCT extension is tracked via factory metadata (strip_sct / tamper_sct
        # etc.) rather than carried on the cert — cryptography validates the
        # SCT OID format and the synthetic certs aren't worth marshaling a real
        # PrecertificateSignedCertificateTimestamps payload.

        return builder.sign(self._ca_key, hashes.SHA256())

    def _build_san_entries(
        self,
        san_type: str,
        value: str,
        extra_sans: list[tuple[str, str]] | None,
        malformed_ia5: bool,
    ) -> list[x509.GeneralName]:
        pairs: list[tuple[str, str]] = []
        if san_type and value:
            pairs.append((san_type, value))
        if extra_sans:
            pairs.extend(extra_sans)
        out: list[x509.GeneralName] = []
        for t, v in pairs:
            if t == "rfc822Name":
                if malformed_ia5:
                    # Emit a synthetic OtherName with bad IA5 bytes for the
                    # malformed-IA5 SAN test path.
                    out.append(
                        x509.OtherName(
                            x509.ObjectIdentifier("1.3.6.1.4.1.11129.2.4.99"),
                            b"\x16\x05" + b"\xff\xff\xff\xff\xff",
                        )
                    )
                else:
                    try:
                        out.append(x509.RFC822Name(v))
                    except (ValueError, UnicodeError):
                        # RFC822Name requires A-label (ASCII). Non-ASCII
                        # identities go in OtherName with the Fulcio
                        # signer-identity OID so verify still recovers them.
                        out.append(
                            x509.OtherName(
                                x509.ObjectIdentifier(_OID_SIGNER_IDENTITY_OTHERNAME),
                                _der_utf8(v.encode("utf-8")),
                            )
                        )
            elif t == "uniformResourceIdentifier":
                out.append(x509.UniformResourceIdentifier(v))
            elif t == "otherName_oid_57264_1_24":
                out.append(
                    x509.OtherName(
                        x509.ObjectIdentifier(_OID_SIGNER_IDENTITY_OTHERNAME),
                        _der_utf8(v.encode("utf-8")),
                    )
                )
            else:
                out.append(x509.RFC822Name(v))
        return out

    # ------------------------------------------------------------------
    # Bundle build.
    # ------------------------------------------------------------------

    def _build_bundle(self, canonical_bytes: bytes, opts: dict) -> Bundle:
        identity = opts.get("identity", "alice@example.com")
        provider = opts.get("provider")
        issuer = opts.get("issuer") or (
            provider.issuer if provider is not None else "https://accounts.google.com"
        )
        san_type = opts.get("san_type", "rfc822Name")
        san_value = opts.get("san_value", identity)
        extra_sans = opts.get("extra_sans")
        include_san = opts.get("include_san", True)
        malformed_ia5 = opts.get("malformed_ia5", False)
        chain_length = opts.get("chain_length")
        issuer_oid_variant = opts.get("issuer_oid_variant", "both")
        issuer_oid_v2 = opts.get("issuer_oid_v2")
        issuer_oid_legacy = opts.get("issuer_oid_legacy")
        not_before = opts.get("not_before")
        not_after = opts.get("not_after")
        custom_cert = opts.get("custom_cert")
        include_sct = opts.get("include_sct", True)

        if custom_cert is not None:
            private_key = custom_cert.private_key
            leaf_cert = custom_cert.certificate
        else:
            private_key = ec.generate_private_key(ec.SECP256R1())
            leaf_cert = self._mint_leaf_cert(
                private_key=private_key,
                identity=identity,
                issuer=issuer,
                san_type=san_type,
                extra_sans=extra_sans,
                not_before=not_before,
                not_after=not_after,
                issuer_oid_variant=issuer_oid_variant,
                issuer_oid_v2=issuer_oid_v2,
                issuer_oid_legacy=issuer_oid_legacy,
                include_san=include_san,
                san_value=san_value,
                malformed_ia5=malformed_ia5,
                include_sct=include_sct,
            )

        digest = hashlib.sha256(canonical_bytes).digest()
        if isinstance(private_key, ec.EllipticCurvePrivateKey):
            signature = private_key.sign(
                digest,
                ec.ECDSA(Prehashed(hashes.SHA256())),
            )
        elif isinstance(private_key, ed25519.Ed25519PrivateKey):
            signature = private_key.sign(canonical_bytes)
        elif isinstance(private_key, rsa.RSAPrivateKey):
            signature = private_key.sign(
                digest,
                padding.PKCS1v15(),
                Prehashed(hashes.SHA256()),
            )
        else:
            signature = private_key.sign(canonical_bytes)

        log_id_bytes = opts.get("log_id_bytes") or secrets.token_bytes(32)
        # Jitter both log_index and tree_size for non-determinism.
        if "log_index" in opts:
            log_index = opts["log_index"]
        else:
            log_index = secrets.randbelow(1_000_000)
        if "tree_size" in opts:
            tree_size = opts["tree_size"]
        else:
            tree_size = log_index + 1 + secrets.randbelow(1_000)
        if tree_size <= log_index:
            tree_size = log_index + 1
        root_hash_bytes = opts.get("root_hash_bytes") or secrets.token_bytes(32)
        # Allow callers to opt into an empty inclusion path (tree_size==1).
        if "inclusion_hashes_bytes" in opts:
            hashes_list_bytes = opts["inclusion_hashes_bytes"]
        else:
            hashes_list_bytes = [secrets.token_bytes(32)]
        checkpoint = opts.get("checkpoint") or self._make_checkpoint_envelope(
            tree_size,
            root_hash_bytes,
            log_id_bytes,
        )
        integrated_time_s = opts.get("integrated_time_s")
        if integrated_time_s is None:
            # Monotonic counter keeps integrated_time strictly increasing
            # across sign() calls (K-A9 non-determinism). Keep small so we
            # stay inside the cert validity window — wraparound is fine since
            # the K-A9 test only inspects two consecutive signings.
            self._integrated_time_counter += 1
            base = int(time.time())
            integrated_time_s = base + (self._integrated_time_counter % 60)

        canonicalized_body = json.dumps(
            {
                "apiVersion": "0.0.1",
                "kind": "hashedrekord",
                "spec": {
                    "data": {"hash": {"algorithm": "sha256", "value": digest.hex()}},
                    "signature": {
                        "content": base64.b64encode(signature).decode("ascii"),
                        "publicKey": {
                            "content": base64.b64encode(
                                leaf_cert.public_bytes(serialization.Encoding.PEM)
                            ).decode("ascii"),
                        },
                    },
                },
            }
        ).encode("utf-8")

        # Proto fields require base64-encoded bytes for ProtoBytes and string
        # forms for ProtoU64 (sigstore_models._core validators).
        inclusion_proof = rekor_v1.InclusionProof(
            log_index=str(log_index),  # type: ignore[arg-type]
            root_hash=base64.b64encode(root_hash_bytes),
            tree_size=str(tree_size),  # type: ignore[arg-type]
            hashes=[base64.b64encode(h) for h in hashes_list_bytes],
            checkpoint=rekor_v1.Checkpoint(envelope=checkpoint),
        )
        # Per sigstore-public-v1 profile (and Bundle._verify), v0.3 bundles
        # carry inclusion_proof + a source of signed time. We default to an
        # inclusion_promise (synthetic SET) for that — and omit TSA — so the
        # "must have exactly one" profile rule passes.
        # Synthetic SET: a real, valid DER ECDSA-Sig-Value so the
        # sigstore-public-v1 profile pre-check accepts it. The prior hand-rolled
        # 0x30/0x44/0x02/0x20 + random bytes was NOT valid DER (random 32-byte
        # integers are ~half the time negative under DER, and the lengths were
        # not minimal-form); the strict decode_dss_signature gate
        # (finding-20260519-74o7) correctly rejected it. encode_dss_signature
        # emits correct minimal DER; `| 1` keeps r, s positive and non-zero.
        _set_r = int.from_bytes(secrets.token_bytes(32), "big") | 1
        _set_s = int.from_bytes(secrets.token_bytes(32), "big") | 1
        synthetic_set = encode_dss_signature(_set_r, _set_s)
        inclusion_promise = rekor_v1.InclusionPromise(
            signed_entry_timestamp=base64.b64encode(synthetic_set),
        )
        tlog_entry = rekor_v1.TransparencyLogEntry(
            log_index=str(log_index),  # type: ignore[arg-type]
            log_id=common_v1.LogId(key_id=base64.b64encode(log_id_bytes)),
            kind_version=rekor_v1.KindVersion(kind="hashedrekord", version="0.0.1"),
            integrated_time=str(integrated_time_s),  # type: ignore[arg-type]
            inclusion_promise=inclusion_promise,
            inclusion_proof=inclusion_proof,
            canonicalized_body=base64.b64encode(canonicalized_body),
        )

        # TSA timestamps only when tests opt-in (set_tsa_timestamp, etc.).
        # Default is empty so the SET above is the sole source-of-signed-time.
        tsa_list = opts.get("tsa_timestamps") or []
        timestamp_verification = bundle_v1.TimestampVerificationData(
            rfc3161_timestamps=[
                bundle_v1.RFC3161SignedTimestamp(signed_timestamp=base64.b64encode(t))
                for t in tsa_list
            ]
        )

        message_signature = common_v1.MessageSignature(
            message_digest=common_v1.HashOutput(
                algorithm=common_v1.HashAlgorithm.SHA2_256,
                digest=base64.b64encode(digest),
            ),
            signature=base64.b64encode(signature),
        )

        inner = bundle_v1.Bundle(
            media_type=Bundle.BundleType.BUNDLE_0_3.value,
            verification_material=bundle_v1.VerificationMaterial(
                certificate=common_v1.X509Certificate(
                    raw_bytes=base64.b64encode(
                        leaf_cert.public_bytes(serialization.Encoding.DER)
                    ),
                ),
                tlog_entries=[tlog_entry],
                timestamp_verification_data=timestamp_verification,
            ),
            message_signature=message_signature,
        )

        bundle = Bundle(inner)

        meta = {
            "canonical_bytes_signed": canonical_bytes,
            "identity": identity,
            "issuer": issuer,
            "cert_not_before_us": int(
                leaf_cert.not_valid_before_utc.timestamp() * 1_000_000
            ),
            "cert_not_after_us": int(
                leaf_cert.not_valid_after_utc.timestamp() * 1_000_000
            ),
            "integrated_time_us": int(integrated_time_s) * 1_000_000,
            "leaf_hash_explicit": opts.get("leaf_hash_explicit"),
            "merkle_root_hash": root_hash_bytes,
            "merkle_log_index": log_index,
            "merkle_tree_size": tree_size,
            "merkle_hashes": list(hashes_list_bytes),
            "chain_length": chain_length,
            "san_type": san_type,
            "missing_san": not include_san,
            "malformed_ia5": malformed_ia5,
            "unknown_ca": opts.get("unknown_ca", False),
            "broken_chain": opts.get("broken_chain", False),
            "tampered_cert": False,
            "tampered_sig": False,
            "tampered_merkle": False,
            "tampered_checkpoint": False,
            "tampered_sct": False,
            "missing_rekor_proof": False,
            "missing_checkpoint": False,
            "missing_sct": False,
            "missing_tsa": False,
            "tsa_timestamps_us": [int(t) for t in opts.get("tsa_timestamps_us", [])],
            "checkpoint_root_mismatch": False,
            "checkpoint_tree_mismatch": False,
            "checkpoint_old_key": False,
            "checkpoint_malformed": False,
            "rekor_log_id_swapped": False,
            "digest_algorithm": "sha256",
            "splice_attack": False,
            "trust_root": opts.get("trust_root"),
            "issuer_oid_variant": issuer_oid_variant,
            "cross_sct": False,
            "untrusted_ct_log_sct": False,
            "future_dated": False,
            "dsse_envelope": opts.get("dsse", False),
        }
        # Immutable canonical snapshot of every cryptographically-bound
        # region. Bundle ProtoJSON round-trips idempotently, so a later
        # bit-flip of any bound region (tlog proof/checkpoint/logId/
        # integratedTime/inclusionPromise/canonicalizedBody/TSA, cert, sig,
        # digest) makes bundle.to_json() drift from this — see
        # _canonical_drift_exception. Helpers that mutate one bound region
        # set an explicit tamper flag handled earlier in _evaluate_bundle;
        # this catches the unflagged direct-mutation (fuzz) path.
        meta["_canonical_json"] = bundle.to_json()
        self._registry[bytes(signature)] = meta
        return bundle

    def _make_checkpoint_envelope(
        self, tree_size: int, root_hash: bytes, log_id: bytes
    ) -> str:
        # Factory-fidelity note: the origin line number is derived
        # synthetically from log_id bytes. A real Rekor checkpoint carries
        # the log's actual origin / tree ID, which will NOT equal this
        # derived value. A future Phase-4 cross-validation test
        # (brief-20260514-me2x §1) must not assert this synthetic origin
        # matches a real embedded log_id.
        sig_b = base64.b64encode(log_id[:4] + secrets.token_bytes(64)).decode("ascii")
        return (
            f"rekor.sigstore.dev - {int.from_bytes(log_id[:8], 'big')}\n"
            f"{tree_size}\n"
            f"{base64.b64encode(root_hash).decode('ascii')}\n"
            f"\n"
            f"— rekor.sigstore.dev {sig_b}\n"
        )

    # ------------------------------------------------------------------
    # Public factory API.
    # ------------------------------------------------------------------

    def install_sign_monkeypatch(
        self, monkeypatch, *, provider=None, **opts
    ) -> _SignSpy:
        self._reset_sign()
        opts["provider"] = provider
        # Map several "convenience" kwargs onto failure_mode strings.
        if "failure_mode" not in opts:
            if opts.get("fulcio_status") in (503, 500, 502, 504):
                opts["failure_mode"] = "fulcio_503"
            elif opts.get("fulcio_status") == 400:
                opts["failure_mode"] = "fulcio_400_unknown_csr"
            elif opts.get("rekor_status") in (502, 500, 503, 504):
                opts["failure_mode"] = "rekor_502"
            elif opts.get("rekor_status") == 404:
                opts["failure_mode"] = "rekor_404"
            elif opts.get("oidc_status") == "unreachable":
                opts["failure_mode"] = "oidc_unreachable"
            elif opts.get("oidc_status") == "token_rejected":
                opts["failure_mode"] = "oidc_token_rejected"
            elif opts.get("network") == "down":
                opts["failure_mode"] = "network_down"
        self._sign_state = opts
        spy = _SignSpy()

        fake_ctx = _FakeSigningContext(self, spy)
        # Patch the wrappers in skein.signing so production code calls our
        # fake. The boundary helpers (_build_production_signing_context,
        # _build_identity_token) are the only patch targets — we don't need
        # to touch sigstore.sign internals.
        monkeypatch.setattr(
            "skein.signing._build_production_signing_context",
            lambda: fake_ctx,
        )
        monkeypatch.setattr(
            "skein.signing._build_identity_token",
            _FakeIdentityTokenFactory,
        )
        return spy

    def install_verify_monkeypatch(self, monkeypatch, **opts) -> None:
        self._reset_verify()
        self._verify_state = opts
        # Activate the test gate via monkeypatch so it auto-reverts to False
        # on teardown — _select_verifier checks this before reading
        # _verify_state, and production behavior must not see stale test
        # state from a prior test (gremlin finding #5).
        monkeypatch.setattr(self, "_test_active", True)
        fake = _FakeVerifier(self)
        monkeypatch.setattr(
            "skein.signing._build_production_verifier",
            lambda *, offline=False: fake,
        )
        # _select_verifier's §3(a) branch calls Verifier(trusted_root=...) with
        # the materialized era root. Route that through the fake too so era
        # tests don't try to hit the real sigstore Verifier constructor.
        monkeypatch.setattr(
            "skein.signing.Verifier",
            _FakeVerifierClass(self, fake),
        )

    def make_staging_verifier(self) -> _FakeVerifier:
        return _FakeVerifier(self)

    # Bundle blob helpers -----------------------------------------------

    def make_bundle_blob(self, *, canonical_bytes, identity, issuer, **opts) -> str:
        opts = dict(opts)
        # tests pass `cert=...`; normalize to internal `custom_cert`.
        if "cert" in opts and "custom_cert" not in opts:
            opts["custom_cert"] = opts.pop("cert")
        opts.update(identity=identity, issuer=issuer)
        return self._build_bundle(canonical_bytes, opts).to_json()

    def make_bundle_blob_with_san(
        self, *, san_type, value, canonical_bytes, identity, issuer, **opts
    ) -> str:
        opts = dict(opts)
        opts.update(
            identity=identity,
            issuer=issuer,
            san_type=san_type,
            san_value=value,
        )
        return self._build_bundle(canonical_bytes, opts).to_json()

    def make_bundle_blob_with_multiple_sans(
        self, *, sans, canonical_bytes, identity, issuer, **opts
    ) -> str:
        opts = dict(opts)
        opts.update(
            identity=identity,
            issuer=issuer,
            san_type=sans[0][0],
            san_value=sans[0][1],
            extra_sans=list(sans[1:]),
        )
        return self._build_bundle(canonical_bytes, opts).to_json()

    def make_bundle_blob_with_missing_san(
        self, *, canonical_bytes, identity, issuer, **opts
    ) -> str:
        opts = dict(opts)
        opts.update(identity=identity, issuer=issuer, include_san=False)
        return self._build_bundle(canonical_bytes, opts).to_json()

    def make_bundle_blob_with_malformed_ia5string_san(
        self, *, canonical_bytes, identity, issuer, **opts
    ) -> str:
        opts = dict(opts)
        opts.update(identity=identity, issuer=issuer, malformed_ia5=True)
        return self._build_bundle(canonical_bytes, opts).to_json()

    def make_bundle_blob_with_rekor_inclusion(
        self,
        canonical_bytes,
        *,
        log_index,
        tree_size,
        leaf_hash=None,
        root_hash=None,
        hashes=None,
        checkpoint=None,
        log_id=None,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
        **opts,
    ) -> str:
        opts = dict(opts)

        def _as_bytes(v: Any) -> bytes:
            if isinstance(v, bytes):
                return v
            if not isinstance(v, str):
                return bytes(v)
            try:
                return base64.b64decode(v, validate=True)
            except (binascii.Error, ValueError):
                return v.encode("utf-8")

        if log_id is not None:
            opts["log_id_bytes"] = _as_bytes(log_id)
        if root_hash is not None:
            opts["root_hash_bytes"] = _as_bytes(root_hash)
        if hashes is not None:
            opts["inclusion_hashes_bytes"] = [_as_bytes(h) for h in hashes]
        if checkpoint is not None:
            opts["checkpoint"] = checkpoint
        if leaf_hash is not None:
            opts["leaf_hash_explicit"] = _as_bytes(leaf_hash)
        opts.update(
            identity=identity,
            issuer=issuer,
            log_index=log_index,
            tree_size=tree_size,
        )
        return self._build_bundle(canonical_bytes, opts).to_json()

    def make_bundle_blob_with_chain_length(
        self, *, chain_length, canonical_bytes, identity, issuer, **opts
    ) -> str:
        opts = dict(opts)
        opts.update(identity=identity, issuer=issuer, chain_length=chain_length)
        return self._build_bundle(canonical_bytes, opts).to_json()

    def make_bundle_blob_with_broken_chain(
        self, *, canonical_bytes, identity, issuer, **opts
    ) -> str:
        opts = dict(opts)
        opts.update(identity=identity, issuer=issuer, broken_chain=True)
        return self._build_bundle(canonical_bytes, opts).to_json()

    def make_dsse_envelope_bundle(self, canonical_bytes, **opts) -> str:
        opts = dict(opts)
        opts.update(
            identity="alice@example.com",
            issuer="https://accounts.google.com",
            dsse=True,
        )
        return self._build_bundle(canonical_bytes, opts).to_json()

    # Trust root --------------------------------------------------------

    def make_trust_root(self, **opts) -> _TestTrustRoot:
        return _TestTrustRoot(era="current", **opts)

    def make_era_trust_root(self, *, era: str, **opts) -> _TestTrustRoot:
        return _TestTrustRoot(era=era, **opts)

    def trust_root_pin(self, root: _TestTrustRoot) -> str:
        return root.pin

    def set_trust_root_pin(self, blob: str, pin_hash: str) -> str:
        # No-op on the bundle JSON itself — Bundle.from_json is strict about
        # extra fields, and SKEIN's SignatureBundle.trust_root_pin is the
        # authoritative carrier of the pin. Tests that pass both at once
        # exercise the SignatureBundle-side path; the bundle-internal marker
        # is not part of the v0.3 wire format.
        return blob

    # Cert variants -----------------------------------------------------

    def make_cert_with_curve(self, curve: str) -> _TestCertificate:
        key: Any
        if curve == "P-256":
            key = ec.generate_private_key(ec.SECP256R1())
        elif curve == "P-384":
            key = ec.generate_private_key(ec.SECP384R1())
        elif curve == "Ed25519":
            key = ed25519.Ed25519PrivateKey.generate()
        elif curve == "RSA-2048":
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        else:
            key = ec.generate_private_key(ec.SECP256R1())
        cert = self._mint_leaf_cert(
            private_key=key,
            identity="alice@example.com",
            issuer="https://accounts.google.com",
        )
        return _TestCertificate(curve=curve, private_key=key, certificate=cert)

    def set_leaf_cert(self, blob: str, cert: _TestCertificate) -> str:
        obj = json.loads(blob)
        sig = self._sig_from_obj(obj)
        meta = self._registry.get(sig, {})
        canonical = meta.get("canonical_bytes_signed", b"")
        opts = {
            "identity": meta.get("identity", "alice@example.com"),
            "issuer": meta.get("issuer", "https://accounts.google.com"),
            "custom_cert": cert,
        }
        return self._build_bundle(canonical, opts).to_json()

    # Tamper helpers ----------------------------------------------------

    def _sig_from_obj(self, obj: dict) -> bytes:
        sig_b64 = obj.get("messageSignature", {}).get("signature")
        if not sig_b64:
            return b""
        try:
            return base64.b64decode(sig_b64)
        except Exception:  # noqa: BLE001
            return b""

    def _mark(self, blob: str, key: str, value: Any = True) -> str:
        obj = json.loads(blob)
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig][key] = value
        return blob

    def tamper_signature(self, blob: str) -> str:
        obj = json.loads(blob)
        sig_b64 = obj.get("messageSignature", {}).get("signature", "")
        if sig_b64:
            old = base64.b64decode(sig_b64)
            new = b"\x00" * len(old)
            obj["messageSignature"]["signature"] = base64.b64encode(new).decode("ascii")
            if old in self._registry:
                self._registry[old]["tampered_sig"] = True
                self._registry[new] = dict(self._registry[old])
                self._registry[new]["tampered_sig"] = True
        return json.dumps(obj)

    def set_digest_algorithm(self, blob: str, algo: str) -> str:
        obj = json.loads(blob)
        try:
            obj["messageSignature"]["messageDigest"]["algorithm"] = algo
        except Exception:  # noqa: BLE001
            pass
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["digest_algorithm"] = algo
        return json.dumps(obj)

    def tamper_cert(self, blob: str) -> str:
        obj = json.loads(blob)
        cert_obj = obj.get("verificationMaterial", {}).get("certificate") or {}
        cert_b64 = cert_obj.get("rawBytes")
        if cert_b64:
            raw = bytearray(base64.b64decode(cert_b64))
            if len(raw) > 200:
                raw[150] ^= 0xFF
                cert_obj["rawBytes"] = base64.b64encode(bytes(raw)).decode("ascii")
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["tampered_cert"] = True
        return json.dumps(obj)

    def tamper_merkle_hashes(self, blob: str) -> str:
        obj = json.loads(blob)
        try:
            proof = obj["verificationMaterial"]["tlogEntries"][0]["inclusionProof"]
            if proof.get("hashes"):
                h = bytearray(base64.b64decode(proof["hashes"][0]))
                if h:
                    h[0] ^= 0xFF
                proof["hashes"][0] = base64.b64encode(bytes(h)).decode("ascii")
        except Exception:  # noqa: BLE001
            pass
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["tampered_merkle"] = True
        return json.dumps(obj)

    def strip_rekor_proof(self, blob: str) -> str:
        return self._mark(blob, "missing_rekor_proof", True)

    def strip_checkpoint(self, blob: str) -> str:
        return self._mark(blob, "missing_checkpoint", True)

    def set_rekor_hashes(self, blob: str, hashes: list[str]) -> str:
        obj = json.loads(blob)
        try:
            proof = obj["verificationMaterial"]["tlogEntries"][0]["inclusionProof"]
            proof["hashes"] = hashes
        except Exception:  # noqa: BLE001
            pass
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["tampered_merkle"] = True
        return json.dumps(obj)

    def set_rekor_root_hash(self, blob: str, root_hash: str) -> str:
        obj = json.loads(blob)
        try:
            proof = obj["verificationMaterial"]["tlogEntries"][0]["inclusionProof"]
            proof["rootHash"] = root_hash
        except Exception:  # noqa: BLE001
            pass
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["tampered_merkle"] = True
        return json.dumps(obj)

    def set_rekor_tree_size(self, blob: str, tree_size: int) -> str:
        obj = json.loads(blob)
        try:
            proof = obj["verificationMaterial"]["tlogEntries"][0]["inclusionProof"]
            proof["treeSize"] = str(tree_size)
        except Exception:  # noqa: BLE001
            pass
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["tampered_merkle"] = True
        return json.dumps(obj)

    def alternate_rekor_log_id(self) -> str:
        return base64.b64encode(b"alternate-rekor-log" + b"\x00" * 13).decode("ascii")

    def swap_rekor_log_id(self, blob: str, new_log_id: str) -> str:
        obj = json.loads(blob)
        try:
            entry = obj["verificationMaterial"]["tlogEntries"][0]
            entry["logId"]["keyId"] = new_log_id
        except Exception:  # noqa: BLE001
            pass
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["rekor_log_id_swapped"] = True
        return json.dumps(obj)

    def tamper_checkpoint_signature(self, blob: str) -> str:
        return self._mark(blob, "tampered_checkpoint", "signature")

    def checkpoint_with_different_root(self, blob: str) -> str:
        return self._mark(blob, "checkpoint_root_mismatch", True)

    def checkpoint_with_different_tree_size(self, blob: str) -> str:
        return self._mark(blob, "checkpoint_tree_mismatch", True)

    def checkpoint_signed_by_old_rekor_key(self, blob: str) -> str:
        return self._mark(blob, "checkpoint_old_key", True)

    def malformed_checkpoint(self, blob: str, *, kind: str) -> str:
        return self._mark(blob, "checkpoint_malformed", kind)

    def make_checkpoint(
        self, *, tree_size: int, root_hash: str, log_id: str | None = None
    ) -> str:
        """Build a C2SP signed-note checkpoint.

        root_hash and log_id are embedded verbatim — parse_checkpoint_signed_note
        returns them in the same form.
        """
        origin = log_id if log_id else "rekor.sigstore.dev"
        sig_b = base64.b64encode(secrets.token_bytes(64)).decode("ascii")
        return f"{origin}\n{tree_size}\n{root_hash}\n\n— {origin} {sig_b}\n"

    def parse_checkpoint_signed_note(self, checkpoint: str) -> _ParsedCheckpoint:
        lines = checkpoint.strip().split("\n")
        if len(lines) < 3:
            raise ValueError("malformed checkpoint signed note")
        try:
            tree_size = int(lines[1])
            root_hash = lines[2]
        except (ValueError, IndexError) as exc:
            raise ValueError("malformed checkpoint signed note") from exc
        return _ParsedCheckpoint(
            tree_size=tree_size, root_hash=root_hash, log_id=lines[0]
        )

    def future_dated_cert(self, blob: str) -> str:
        return self._mark(blob, "future_dated", True)

    def set_cert_validity(self, blob: str, *, not_before: int, not_after: int) -> str:
        obj = json.loads(blob)
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["cert_not_before_us"] = not_before
            self._registry[sig]["cert_not_after_us"] = not_after
            # Ensure integrated_time is inside the new window so the cert-
            # validity check passes by default (tests that want failure call
            # set_rekor_integrated_time explicitly afterward).
            it = self._registry[sig].get("integrated_time_us")
            if it is None or it < not_before or it > not_after:
                self._registry[sig]["integrated_time_us"] = not_before + 1
        return json.dumps(obj)

    def set_rekor_integrated_time(self, blob: str, ts: int) -> str:
        obj = json.loads(blob)
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["integrated_time_us"] = ts
        return json.dumps(obj)

    def set_verify_time(self, ts: int) -> None:
        self._verify_time_us = ts

    def set_tsa_timestamp(self, blob: str, ts: int) -> str:
        obj = json.loads(blob)
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig]["tsa_timestamps_us"] = [ts]
        return json.dumps(obj)

    def add_tsa_timestamp(self, blob: str, ts: int) -> str:
        obj = json.loads(blob)
        sig = self._sig_from_obj(obj)
        if sig in self._registry:
            self._registry[sig].setdefault("tsa_timestamps_us", []).append(ts)
        return json.dumps(obj)

    def strip_tsa(self, blob: str) -> str:
        return self._mark(blob, "missing_tsa", True)

    def strip_sct(self, blob: str) -> str:
        return self._mark(blob, "missing_sct", True)

    def tamper_sct(self, blob: str) -> str:
        return self._mark(blob, "tampered_sct", True)

    def cross_sct(self, blob: str) -> str:
        return self._mark(blob, "cross_sct", True)

    def untrusted_ct_log_sct(self, blob: str) -> str:
        return self._mark(blob, "untrusted_ct_log_sct", True)

    def truncate(self, blob: str, n: int) -> str:
        return blob[:n]

    def bit_flip(self, blob: str, offset: int, bit: int = 0) -> str:
        if offset >= len(blob):
            return blob
        b = bytearray(blob.encode("utf-8", errors="replace"))
        b[offset] ^= 1 << bit
        return b.decode("utf-8", errors="replace")

    def splice_rekor_entry(self, *, host: str, source: str) -> str:
        host_obj = json.loads(host)
        source_obj = json.loads(source)
        host_obj["verificationMaterial"]["tlogEntries"] = source_obj[
            "verificationMaterial"
        ]["tlogEntries"]
        sig = self._sig_from_obj(host_obj)
        if sig in self._registry:
            self._registry[sig]["splice_attack"] = True
        return json.dumps(host_obj)

    # Evaluation --------------------------------------------------------

    @staticmethod
    def _recompute_merkle_root(
        *,
        leaf: bytes,
        hashes: list[bytes],
        log_index: int,
        tree_size: int,
    ) -> bytes:
        """Recompute Merkle root from leaf + sibling path (RFC 6962 hashing)."""
        node = leaf
        idx = log_index
        last = tree_size - 1
        path_iter = iter(hashes)
        while last > 0:
            if idx % 2 == 1:
                try:
                    sibling = next(path_iter)
                except StopIteration:
                    return b""
                node = hashlib.sha256(b"\x01" + sibling + node).digest()
            elif idx < last:
                try:
                    sibling = next(path_iter)
                except StopIteration:
                    return b""
                node = hashlib.sha256(b"\x01" + node + sibling).digest()
            # else: idx == last and even → carry up, no sibling.
            idx //= 2
            last //= 2
        return node

    def _evaluate_bundle(
        self, meta: dict, bundle: Bundle, input_bytes: bytes
    ) -> BaseException | None:
        # Digest-algorithm profile (sigstore-public-v1 SHA-256).
        if meta.get("digest_algorithm") not in (None, "sha256", "SHA2_256"):
            return _synthesize_exception("InvalidBundle", "non-SHA256 digest algorithm")

        if meta.get("dsse_envelope"):
            return _synthesize_exception(
                "InvalidBundle", "DSSE envelope not supported in v0"
            )

        if meta.get("splice_attack"):
            return _synthesize_exception("InvalidRekorEntry", "spliced Rekor entry")

        # Tampered cert / unknown CA / broken chain / chain_length 0.
        if (
            meta.get("tampered_cert")
            or meta.get("unknown_ca")
            or meta.get("broken_chain")
        ):
            return _synthesize_exception(
                "InvalidMaterials", "cert chain validation failed"
            )
        if meta.get("chain_length") == 0:
            return _synthesize_exception(
                "InvalidMaterials", "self-signed leaf, no chain"
            )

        # Tampered signature.
        if meta.get("tampered_sig"):
            return _SignatureInvalid("Signature is invalid for input")

        if meta.get("future_dated"):
            return _synthesize_exception(
                "InvalidMaterials", "future-dated cert vs integrated time"
            )

        nb_us = meta.get("cert_not_before_us")
        na_us = meta.get("cert_not_after_us")
        it_us = meta.get("integrated_time_us")
        if nb_us is not None and na_us is not None and it_us is not None:
            if it_us < nb_us or it_us > na_us:
                return _synthesize_exception(
                    "InvalidMaterials",
                    "integrated time outside cert validity",
                )

        if meta.get("missing_tsa"):
            return _synthesize_exception(
                "InvalidBundle",
                "rekor v2 bundle missing TSA timestamp",
            )
        tsa_list = meta.get("tsa_timestamps_us") or []
        if tsa_list and nb_us is not None and na_us is not None:
            for ts in tsa_list:
                if ts < nb_us or ts > na_us:
                    return _synthesize_exception(
                        "InvalidMaterials",
                        "TSA timestamp outside cert validity",
                    )
        if tsa_list and it_us is not None and na_us is not None:
            for ts in tsa_list:
                if abs(ts - it_us) > 1_000_000 and ts > na_us:
                    return _synthesize_exception(
                        "InvalidMaterials",
                        "rekor and TSA times disagree outside cert validity",
                    )

        # Merkle inclusion: recompute root from leaf_hash + path, compare to
        # the bundle's claimed root_hash. Only runs when the caller supplied
        # an explicit leaf_hash via make_bundle_blob_with_rekor_inclusion; in
        # the synthetic happy-path case (no explicit leaf), we trust the
        # stored values since they originated from this factory.
        explicit_leaf = meta.get("leaf_hash_explicit")
        if explicit_leaf is not None:
            recomputed = self._recompute_merkle_root(
                leaf=explicit_leaf,
                hashes=meta.get("merkle_hashes") or [],
                log_index=int(meta.get("merkle_log_index") or 0),
                tree_size=int(meta.get("merkle_tree_size") or 1),
            )
            if recomputed != (meta.get("merkle_root_hash") or b""):
                return _synthesize_exception(
                    "InvalidRekorEntry",
                    "merkle root does not match recomputed path",
                )

        if meta.get("missing_rekor_proof"):
            return _synthesize_exception(
                "InvalidRekorEntry", "rekor inclusion proof missing"
            )
        if meta.get("missing_checkpoint"):
            return _synthesize_exception(
                "InvalidRekorEntry", "rekor checkpoint missing"
            )
        if meta.get("tampered_merkle"):
            return _synthesize_exception(
                "InvalidRekorEntry", "rekor merkle hashes tampered"
            )
        if meta.get("rekor_log_id_swapped"):
            return _synthesize_exception("InvalidRekorEntry", "rekor log_id mismatch")
        if meta.get("checkpoint_root_mismatch"):
            return _synthesize_exception(
                "InvalidRekorEntry", "checkpoint root mismatch"
            )
        if meta.get("checkpoint_tree_mismatch"):
            return _synthesize_exception(
                "InvalidRekorEntry", "checkpoint tree size mismatch"
            )
        if meta.get("checkpoint_old_key"):
            return _synthesize_exception(
                "InvalidRekorEntry",
                "checkpoint signed by retired Rekor key",
            )
        if meta.get("checkpoint_malformed"):
            return _synthesize_exception(
                "InvalidBundle", "checkpoint signed note malformed"
            )
        if meta.get("tampered_checkpoint"):
            return _synthesize_exception(
                "InvalidRekorEntry", "checkpoint signature tampered"
            )

        if meta.get("missing_sct"):
            return _synthesize_exception("InvalidMaterials", "SCT missing")
        if meta.get("tampered_sct"):
            return _synthesize_exception("InvalidMaterials", "SCT tampered")
        if meta.get("cross_sct"):
            return _synthesize_exception("InvalidMaterials", "SCT for different cert")
        if meta.get("untrusted_ct_log_sct"):
            return _synthesize_exception(
                "InvalidMaterials", "SCT from untrusted CT log"
            )

        # Unflagged mutation of any bound region (fuzz / bit-flip path).
        drift = _canonical_drift_exception(meta, bundle)
        if drift is not None:
            return drift

        # Final: signature mismatch detection.
        if input_bytes != meta.get("canonical_bytes_signed"):
            return _SignatureInvalid("Signature is invalid for input")

        return None


# Module-level factory instance.
_test_factory: _TestFactory = _TestFactory()
