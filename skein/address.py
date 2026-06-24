"""
SKEIN rev 3 `::` addressing — tokenizer, parser, resolver.

This module implements the rev 3 content-hash addressing grammar:

    sha256::<digest>                          bare content hash (cascades)
    hash::sha256::<digest>                     explicit spelling of the bare form
    alias::<name>::sha256::<digest>            alias-station-scoped
    <name>::sha256::<digest>                   shorthand for the alias form
    web::<authority>::sha256::<digest>         web-station-scoped
    <any of the above>#sha256::<full-digest>   optional verifier fragment

Design source (fell-clean): skein:finding-20260528-brgy (addressing rev 3),
consolidated in skein:brief-20260528-1wj3. Contract decisions are pinned in
skein:finding-20260529-qlsx (authority validate-not-convert; reject `%` /
IPv6 zone IDs) and skein:finding-20260529-ztp7 (the ten review pins). The
grammar is SKEIN/federation-specific (type words, stations, aliases), so it
lives here rather than in knurl. skein:finding-20260529-ahbi records that call.

The old single-colon `project:folio` scheme has been retired; its parser
(`skein.address_legacy`) was removed at the Stage 4 cutover.

The layering, per the pins:
  - tokenize() owns pure structure: fragment-first split, `::` split with
    bracket OPACITY, and structural bracket / empty-segment / whitespace
    errors. It does NOT know type words.
  - parse() owns interpretation: type-word dispatch, segment arity, hash
    productions, authority validation, alias grammar, fragment grammar, and
    bracket position-legality.

The design is validate-not-convert: authorities are checked for canonical ASCII
form and rejected if non-canonical — never rewritten. Pure stdlib only.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple

# --- algorithm / vocabulary constants ---------------------------------------

_FULL_WIDTH = {"sha256": 64}   # full digest width per algorithm (only sha256 in v0)
_SHORT_MIN = 8                 # minimum short-hash length

# Supported and reserved type words.
_RESERVED_TRANSPORTS = frozenset({"ipfs", "ssh", "oidc", "peer"})

# Names that may never be used as an alias (type words, transports, algo words).
_RESERVED_ALIASES = frozenset(
    {"alias", "web", "hash", "ipfs", "ssh", "oidc", "peer",
     "sha256", "sha512", "blake3"}
)

# --- regexes ----------------------------------------------------------------

_HEX_RE = re.compile(r"[0-9a-f]+")
# alias: lowercase [a-z0-9-], start AND end with [a-z0-9], 1..32 chars.
_ALIAS_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")
# fragment grammar is EXACTLY a bare full sha256 hash.
_FRAGMENT_RE = re.compile(r"sha256::([0-9a-f]{64})")
# bracketed IPv6: `[<inner>]` with an optional `:<port>` suffix.
# Port uses `[0-9]` (not `\d`): `\d` is Unicode-aware and would match e.g.
# Arabic-Indic digits, disagreeing with _validate_port's ASCII _DIGITS check.
# The validator catches those today, but keeping the regex ASCII-only removes
# the footgun (every other regex/charset here is already literal ASCII).
_BRACKET_RE = re.compile(r"\[([^\[\]]*)\](?::([0-9]+))?")

_DNS_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
_DIGITS = frozenset("0123456789")
# A host is "IPv4-literal-shaped" when every dot-separated label is an integer
# literal an inet_aton-style resolver would parse: decimal/octal digits, or a
# `0x`-prefixed hex run. `[0-9]+` covers both decimal and (leading-zero) octal
# shape; the hex form is what the plain digits-and-dots check used to miss.
_IPV4_LABEL_RE = re.compile(r"[0-9]+|0[xX][0-9a-fA-F]+")

# Upper bound on a whole address string, checked before the O(n) structural
# scans so untrusted input (e.g. a federation peer's address blob) cannot make
# tokenize() do work proportional to a multi-megabyte string. The longest legal
# address is ~411 chars today (`web::` + 253-octet host + `:65535` + `::sha256::`
# + 64-hex digest + `#sha256::` + 64-hex fragment) and ~539 even if a future
# 128-hex algorithm (sha512) ships, so this cap sits well clear of any real
# address while still bounding the rejection cost of garbage.
_MAX_ADDRESS_LEN = 4096


# --- exceptions -------------------------------------------------------------

class AddressError(Exception):
    """Base error for malformed addresses (raised by tokenize/parse/construct)."""


class AmbiguousShortHash(AddressError):
    """A short hash matched more than one full digest in the station."""

    def __init__(self, candidates, message: Optional[str] = None):
        # Sort so the reported candidates are canonical and deterministic
        # regardless of the order a StationIndex happened to return matches in
        # (the protocol does not pin an order). `key=str` keeps the sort TOTAL
        # even if a misbehaving station returns non-comparable values — for the
        # contract-conforming lowercase-hex strings it is identical to a plain
        # sort, but it cannot raise inside this exception's constructor (which
        # would mask the ambiguity being reported).
        self.candidates: List[str] = sorted(candidates, key=str)
        if message is None:
            message = (
                f"ambiguous short hash: {len(self.candidates)} candidate digests"
            )
        super().__init__(message)


class ShortHashNotFound(AddressError):
    """A short hash matched no full digest in the station."""


# --- data types -------------------------------------------------------------

@dataclass(frozen=True)
class TokenizedAddress:
    """Pure structural decomposition of an address (no interpretation)."""

    segments: Tuple[str, ...]
    fragment: Optional[str] = None


@dataclass(frozen=True)
class Hash:
    """A folio reference: an algorithm plus a (full or short) lowercase-hex digest."""

    algo: str
    digest: str

    def __post_init__(self) -> None:
        # A Hash is well-formed BY CONSTRUCTION, so is_full / is_short / resolve()
        # can trust it no matter how it was made (parse, construct, or a direct
        # caller). This enforces only the UNIVERSAL invariant — algorithm, hex
        # form, and the full-or-short length band; the CONTEXT rule (whether a
        # short digest is allowed for a given address form) stays at the
        # parse/construct boundary in _assert_valid_hash.
        if self.algo != "sha256":
            raise AddressError(
                f"unsupported hash algorithm: {self.algo!r} (only sha256 in v0)"
            )
        if not isinstance(self.digest, str):
            # Report the TYPE, not repr(value): clearer for a mistaken non-string
            # digest, and never calls a (possibly raising) __repr__ on the value.
            raise AddressError(
                f"digest must be a string, not {type(self.digest).__name__}"
            )
        if not _HEX_RE.fullmatch(self.digest):
            raise AddressError(f"digest must be lowercase hex: {self.digest!r}")
        if not (_SHORT_MIN <= len(self.digest) <= _FULL_WIDTH["sha256"]):
            raise AddressError(f"invalid digest length {len(self.digest)}")

    @property
    def is_full(self) -> bool:
        return len(self.digest) == _FULL_WIDTH.get(self.algo, -1)

    @property
    def is_short(self) -> bool:
        width = _FULL_WIDTH.get(self.algo, -1)
        return _SHORT_MIN <= len(self.digest) < width


@dataclass(frozen=True)
class ParsedAddress:
    """A fully interpreted, validated address."""

    type: str                          # "alias" | "web" | "hash"
    folio: Hash
    alias: Optional[str] = None        # set iff type == "alias"
    authority: Optional[str] = None    # set iff type == "web"
    fragment: Optional[Hash] = None    # verifier fragment (always a full hash)

    def __post_init__(self) -> None:
        # Fully correct-by-construction: a ParsedAddress is valid no matter how it
        # was made (parse, construct, or a direct caller), so resolve() and the
        # derived properties can trust every field. This enforces the documented
        # type/set-iff structure, the component-value grammars, and the folio /
        # fragment shape rules that the parse-time builders enforce.
        if not isinstance(self.type, str):
            raise AddressError(f"type must be a string, not {type(self.type).__name__}")
        if self.type not in ("alias", "web", "hash"):
            raise AddressError(f"unknown address type: {self.type!r}")
        if not isinstance(self.folio, Hash):
            raise AddressError("folio must be a Hash")
        # "set iff" cross-field invariant.
        if (self.alias is not None) != (self.type == "alias"):
            raise AddressError("alias must be set if and only if type == 'alias'")
        if (self.authority is not None) != (self.type == "web"):
            raise AddressError("authority must be set if and only if type == 'web'")
        # Component values must satisfy their grammars (not merely be present).
        if self.alias is not None:
            _validate_alias_name(self.alias)
        if self.authority is not None:
            _validate_authority(self.authority)
        # A bare/explicit-hash folio has no station context, so it cannot be a
        # short hash (nothing to cascade against) — only alias/web may be short.
        # The fullness invariant is about DIGEST LENGTH, so check it intrinsically
        # rather than via the is_full property (which a Hash subclass could
        # override to lie or to raise).
        full_width = _FULL_WIDTH["sha256"]
        if self.type == "hash" and len(self.folio.digest) != full_width:
            raise AddressError("bare/hash folio must be a full digest")
        # The verifier fragment is always a full hash.
        if self.fragment is not None:
            if not isinstance(self.fragment, Hash):
                raise AddressError("fragment must be a Hash or None")
            if len(self.fragment.digest) != full_width:
                raise AddressError("fragment must be a full hash")

    @property
    def has_station_context(self) -> bool:
        """True when an explicit alias/web station scopes the folio."""
        return self.alias is not None or self.authority is not None

    @property
    def is_bare_cascade(self) -> bool:
        """True when the folio cascades local stations (no station context)."""
        return not self.has_station_context


class StationIndex(Protocol):
    """Read-only index over a station's folios, used to lengthen short hashes."""

    def folios_with_prefix(self, algo: str, prefix: str) -> List[str]:
        """Return the matching FULL lowercase-hex digests (0, 1, or many)."""
        ...


# --- tokenizer --------------------------------------------------------------

def tokenize(address: str) -> TokenizedAddress:
    """Split an address into segments and an optional fragment.

    Owns pure structure only: fragment-first split (at the FIRST `#`), `::`
    split with bracket opacity, and structural rejects (whitespace/control
    chars anywhere, malformed brackets, empty/trailing segments). Raises
    AddressError on malformed structure. Knows nothing of type words.
    """
    if not isinstance(address, str):
        raise AddressError("address must be a string")

    # Length-cap before the O(n) scans so untrusted input cannot force work
    # proportional to a giant string (see _MAX_ADDRESS_LEN).
    if len(address) > _MAX_ADDRESS_LEN:
        raise AddressError(
            f"address too long: {len(address)} chars (max {_MAX_ADDRESS_LEN})"
        )

    # Whitespace and control characters reject GLOBALLY, before any split — no
    # stripping (validate-not-convert). This covers the fragment text too.
    # Control range covers C0 (<0x20), DEL (0x7F), and C1 (0x80-0x9F). Invisible
    # format characters (Unicode category Cf: zero-width spaces/joiners, bidi
    # overrides, BOM, soft hyphen) also reject here — they are never part of a
    # valid address and enable visual spoofing (the "Trojan Source" class). Other,
    # VISIBLE non-ASCII (e.g. IDN bytes, category Ll) is left for authority/alias
    # validation so it rejects with a specific message.
    for ch in address:
        o = ord(ch)
        if ch.isspace() or o < 0x20 or 0x7F <= o <= 0x9F:
            raise AddressError(f"illegal whitespace/control character in address: {ch!r}")
        if o >= 0x80 and unicodedata.category(ch) == "Cf":
            raise AddressError(f"illegal invisible/format character in address: {ch!r}")

    # Fragment-first split: everything after the FIRST '#' is opaque fragment
    # text (the bracket state machine must not run over it).
    hash_index = address.find("#")
    if hash_index >= 0:
        body = address[:hash_index]
        fragment: Optional[str] = address[hash_index + 1:]
    else:
        body = address
        fragment = None

    segments = _split_segments(body)

    # Empty/trailing segments reject (the fragment may be empty; the parser
    # judges that separately).
    for seg in segments:
        if seg == "":
            raise AddressError("empty segment (leading/trailing/double `::`)")

    return TokenizedAddress(segments=tuple(segments), fragment=fragment)


def _split_segments(body: str) -> List[str]:
    """Split `body` on `::`, treating balanced `[...]` brackets as opaque.

    Enforces the structural bracket rules: no nesting, no `]` without `[`, no
    EOF inside a bracket, no reopen (`[..][..]`) within one segment. A single
    `:` is part of a segment (host:port, chain-hash `sha256:<hex>`).
    """
    segments: List[str] = []
    buf: List[str] = []
    in_bracket = False
    closed_bracket = False   # has THIS segment already closed a bracket?
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == "[":
            if in_bracket:
                raise AddressError("nested `[` in authority")
            if closed_bracket:
                raise AddressError("reopened `[` after `]` in one segment")
            in_bracket = True
            buf.append(ch)
            i += 1
        elif ch == "]":
            if not in_bracket:
                raise AddressError("`]` without a matching `[`")
            in_bracket = False
            closed_bracket = True
            buf.append(ch)
            i += 1
        elif ch == ":" and not in_bracket:
            if i + 1 < n and body[i + 1] == ":":
                segments.append("".join(buf))
                buf = []
                closed_bracket = False
                i += 2
            else:
                buf.append(ch)   # single colon stays inside the segment
                i += 1
        else:
            buf.append(ch)
            i += 1

    if in_bracket:
        raise AddressError("unterminated `[` (EOF inside bracket)")

    segments.append("".join(buf))
    return segments


# --- folio / hash helpers ---------------------------------------------------

def _assert_valid_hash(h: Hash, *, allow_short: bool) -> None:
    """Confirm `h` is a Hash whose digest length suits the context.

    A Hash is well-formed by construction (Hash.__post_init__ guarantees algo,
    lowercase-hex, and an 8..64 length), so this only checks that `h` IS a Hash
    (callers such as construct() may pass an arbitrary object) and applies the
    CONTEXT rule: a bare/explicit-hash folio (allow_short=False) must be full;
    alias/web folios may be short.
    """
    if not isinstance(h, Hash):
        raise AddressError("folio must be a Hash")
    # Check the length intrinsically rather than via the is_full property, which
    # a Hash subclass could override.
    if not allow_short and len(h.digest) != _FULL_WIDTH["sha256"]:
        raise AddressError(f"invalid digest length {len(h.digest)} (allow_short=False)")


def _make_folio(algo: str, digest: str, *, allow_short: bool) -> Hash:
    h = Hash(algo, digest)
    _assert_valid_hash(h, allow_short=allow_short)
    return h


# --- authority validation (validate-not-convert) ----------------------------

def _validate_authority(authority: str) -> None:
    """Reject any authority that is not already canonical ASCII.

    DNS host (optionally with `:port`), IPv4 dotted-decimal, or a bracketed
    IPv6 literal. `%` rejects outright (covers IPv6 zone IDs, per Decision B).
    """
    # construct() forwards caller-supplied values here; reject non-strings as
    # AddressError rather than letting `in`/membership raise TypeError.
    if not isinstance(authority, str):
        raise AddressError(f"authority must be a string, not {type(authority).__name__}")
    if authority == "":
        raise AddressError("empty authority")
    # `%` rejects before any ipaddress check (stdlib would accept zone IDs).
    if "%" in authority:
        raise AddressError("`%` not allowed in authority (zone IDs out of scope)")

    if authority.startswith("["):
        _validate_bracketed_ipv6(authority)
        return

    # A stray bracket outside the leading position is malformed here.
    if "[" in authority or "]" in authority:
        raise AddressError("misplaced bracket in authority")

    # Optional :port suffix (a DNS host carries no colon of its own).
    if ":" in authority:
        host, _, port = authority.rpartition(":")
        _validate_port(port)
    else:
        host = authority

    _validate_host(host)


def _validate_bracketed_ipv6(authority: str) -> None:
    m = _BRACKET_RE.fullmatch(authority)
    if not m:
        raise AddressError(f"malformed bracketed authority: {authority!r}")
    inner, port = m.group(1), m.group(2)
    if port is not None:
        _validate_port(port)
    # "Canonical" here means syntactically valid + lowercase ASCII, NOT RFC-5952
    # spelling: an uncompressed-but-valid literal (`[2001:db8:0:0:0:0:0:1]`) is
    # accepted as-is and never normalized (validate-not-convert; pinned by the
    # contract). Uppercase hex and `%` zone IDs still reject.
    if inner != inner.lower():
        raise AddressError("IPv6 literal must be lowercase")
    try:
        ipaddress.IPv6Address(inner)
    except ValueError:
        raise AddressError(f"invalid IPv6 literal: {inner!r}")


def _validate_host(host: str) -> None:
    # IPv4 dotted-decimal is accepted unbracketed.
    try:
        ipaddress.IPv4Address(host)
        return
    except ValueError:
        pass
    # An IPv4-literal-shaped host (every label an integer literal) reaches here
    # only because canonical IPv4 was just rejected above, so it is a NON-
    # canonical / invalid IPv4 literal: leading-zero octets (`192.168.001.001`),
    # out-of-range (`999.999.999.999`), the decimal- or hex-int form
    # (`2130706433`, `0x7f000001`), or a short/long form (`1.2.3`). Such a string
    # is ambiguous with an IP literal — a downstream resolver using inet_aton-
    # style parsing may read it as a DIFFERENT address than this DNS-name
    # interpretation (the classic octal/decimal/hex-IP confusable class) — so
    # reject it rather than silently treat it as a DNS host. (validate-not-
    # convert: never accept an ambiguous, non-canonical form.)
    labels = host.split(".")
    if all(_IPV4_LABEL_RE.fullmatch(label) for label in labels):
        raise AddressError(
            f"IPv4-shaped authority is not a canonical IPv4 literal: {host!r}"
        )
    # Otherwise it must be a canonical ASCII DNS name.
    if len(host) > 253:
        raise AddressError("DNS host exceeds 253 octets")
    for label in labels:
        if not (1 <= len(label) <= 63):
            raise AddressError(f"invalid DNS label length: {label!r}")
        if label[0] == "-" or label[-1] == "-":
            raise AddressError(f"DNS label may not start/end with hyphen: {label!r}")
        if any(c not in _DNS_CHARS for c in label):
            raise AddressError(f"illegal character in DNS label: {label!r}")


def _validate_port(port: str) -> None:
    if not port or any(c not in _DIGITS for c in port):
        raise AddressError(f"invalid port: {port!r}")
    # Bound length BEFORE int(): an unbounded digit string would hit Python's
    # int-parsing cap and raise ValueError, escaping validate()'s never-raises
    # contract. The max valid port (65535) is 5 digits.
    if len(port) > 5:
        raise AddressError(f"port too long: {port!r}")
    # Reject non-canonical leading zeros (e.g. `080`), consistent with the
    # validate-not-convert / reject-non-canonical posture used elsewhere.
    if len(port) > 1 and port[0] == "0":
        raise AddressError(f"non-canonical port (leading zero): {port!r}")
    value = int(port)
    if not (1 <= value <= 65535):
        raise AddressError(f"port out of range: {value}")


# --- alias-name validation --------------------------------------------------

def is_reserved_alias(name: str) -> bool:
    """True if `name` is reserved (a type word, transport, or algorithm word)."""
    return name in _RESERVED_ALIASES


def _validate_alias_name(name: str) -> None:
    # construct() forwards caller-supplied values here; reject non-strings as
    # AddressError rather than letting the regex raise TypeError.
    if not isinstance(name, str):
        raise AddressError(f"alias name must be a string, not {type(name).__name__}")
    if is_reserved_alias(name):
        raise AddressError(f"{name!r} is a reserved alias name")
    if not _ALIAS_RE.fullmatch(name):
        raise AddressError(f"invalid alias name: {name!r}")


# --- fragment validation ----------------------------------------------------

def _parse_fragment(raw: Optional[str]) -> Optional[Hash]:
    """Validate a raw fragment string as exactly `sha256::<64-hex>`."""
    if raw is None:
        return None
    m = _FRAGMENT_RE.fullmatch(raw)
    if not m:
        raise AddressError(f"fragment must be a bare full sha256 hash: {raw!r}")
    return Hash("sha256", m.group(1))


# --- parser -----------------------------------------------------------------

def parse(address: str) -> ParsedAddress:
    """Parse and validate an address into a ParsedAddress. Raises AddressError."""
    tokens = tokenize(address)
    fragment = _parse_fragment(tokens.fragment)
    segments = tokens.segments
    first = segments[0]

    if first == "alias":
        _require_arity(segments, 4)
        return _build_alias(segments[1], segments[2], segments[3], fragment)

    if first == "web":
        _require_arity(segments, 4)
        return _build_web(segments[1], segments[2], segments[3], fragment)

    if first == "hash":
        _require_arity(segments, 3)
        return _build_hash(segments[1], segments[2], fragment)

    if first in _RESERVED_TRANSPORTS:
        raise AddressError(f"transport {first!r} is reserved but unsupported in v0")

    # Bare content hash: `<algo>::<digest>`.
    if len(segments) == 2:
        return _build_hash(segments[0], segments[1], fragment)

    # Otherwise an alias shorthand: `<name>::<algo>::<digest>`.
    _require_arity(segments, 3)
    return _build_alias(segments[0], segments[1], segments[2], fragment)


def _require_arity(segments: Tuple[str, ...], expected: int) -> None:
    if len(segments) != expected:
        raise AddressError(
            f"expected {expected} segments, got {len(segments)}"
        )


def _build_alias(name: str, algo: str, digest: str, fragment: Optional[Hash]) -> ParsedAddress:
    _validate_alias_name(name)
    folio = _make_folio(algo, digest, allow_short=True)
    return ParsedAddress(type="alias", folio=folio, alias=name, fragment=fragment)


def _build_web(authority: str, algo: str, digest: str, fragment: Optional[Hash]) -> ParsedAddress:
    _validate_authority(authority)
    folio = _make_folio(algo, digest, allow_short=True)
    return ParsedAddress(type="web", folio=folio, authority=authority, fragment=fragment)


def _build_hash(algo: str, digest: str, fragment: Optional[Hash]) -> ParsedAddress:
    # Bare / explicit-hash has no station context, so a short hash cannot
    # cascade — full digests only.
    folio = _make_folio(algo, digest, allow_short=False)
    return ParsedAddress(type="hash", folio=folio, fragment=fragment)


# --- validate / construct ---------------------------------------------------

def validate(address) -> bool:
    """True if `address` parses; never raises (any input type)."""
    try:
        parse(address)
        return True
    except AddressError:
        return False


def construct(*, type: str, folio: Hash, alias: Optional[str] = None,
              authority: Optional[str] = None,
              fragment: Optional[Hash] = None) -> str:
    """Canonical inverse of parse(): build an address string from components.

    Rejects mismatched or invalid args. Emits the canonical (bare) form for the
    hash type. parse(construct(...)) round-trips.
    """
    if type == "alias":
        if alias is None:
            raise AddressError("alias type requires an alias")
        if authority is not None:
            raise AddressError("alias type must not carry an authority")
        _validate_alias_name(alias)
        _assert_valid_hash(folio, allow_short=True)
        body = f"alias::{alias}::{folio.algo}::{folio.digest}"
    elif type == "web":
        if authority is None:
            raise AddressError("web type requires an authority")
        if alias is not None:
            raise AddressError("web type must not carry an alias")
        _validate_authority(authority)
        _assert_valid_hash(folio, allow_short=True)
        body = f"web::{authority}::{folio.algo}::{folio.digest}"
    elif type == "hash":
        if alias is not None or authority is not None:
            raise AddressError("hash type must not carry an alias or authority")
        _assert_valid_hash(folio, allow_short=False)   # canonical bare form is full
        body = f"{folio.algo}::{folio.digest}"
    else:
        raise AddressError(f"unknown address type: {type!r}")

    if fragment is not None:
        _assert_valid_hash(fragment, allow_short=False)   # fragment is always full
        body += f"#{fragment.algo}::{fragment.digest}"

    return body


# --- resolver ---------------------------------------------------------------

def resolve(parsed: ParsedAddress, station: StationIndex) -> str:
    """Turn a (possibly short) folio reference into a full digest.

    NOT an existence check: a full hash returns its own digest WITHOUT querying
    the station. The station is consulted only to lengthen a short hash,
    detect-and-error on ambiguity or absence.
    """
    folio = parsed.folio
    # Check length intrinsically rather than via the is_full property (which a
    # Hash subclass could override), matching the fullness checks in
    # _assert_valid_hash / ParsedAddress.__post_init__.
    if len(folio.digest) == _FULL_WIDTH["sha256"]:
        return folio.digest

    matches = station.folios_with_prefix(folio.algo, folio.digest)
    if len(matches) == 0:
        raise ShortHashNotFound(
            f"no folio matches short hash {folio.digest!r}"
        )
    if len(matches) > 1:
        raise AmbiguousShortHash(matches)
    return matches[0]
