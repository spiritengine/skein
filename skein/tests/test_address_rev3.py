"""
Phase 2 test contract for the rev 3 `::` addressing primitive.

RSP: addressing (brief skein:brief-20260529-0yjl). Design source, fell-clean:
skein:finding-20260528-brgy (addressing rev 3), consolidated in
skein:brief-20260528-1wj3 (rev 5 synthesis). Contract decisions that the spec
left open are recorded in skein:finding-20260529-qlsx (authority
validate-not-convert; reject `%` / IPv6 zone IDs out of scope) and
skein:finding-20260529-ztp7 (the ten pins below, forced by the knuth / oracle /
gpt-5.5 / kimmi review round).

This file is the CONTRACT, written before the implementation (test-first per the
Rock-Solid Primitive playbook). It defines what "correct" means; Phase 3 makes
it green. The rev 3 grammar is SKEIN/federation-specific (type words, stations,
aliases), so it lives in `skein.address`, NOT in knurl (which stays a generic,
zero-dep primitives library). Until the rev 3 surface exists in `skein.address`,
the whole module skips (the old single-colon `project:folio` scheme still lives
there and in its callers, so we gate on a rev 3 marker rather than importorskip
on the module).

============================================================================
Proposed rev 3 API surface (the contract — open to the fell)
============================================================================

    tokenize(address: str) -> TokenizedAddress
        .segments: tuple[str, ...]   # after fragment removal, after `::` split
        .fragment: str | None        # raw text after the FIRST '#', else None
        Pure structure. Owns: fragment-first split, `::` split with bracket
        OPACITY, and STRUCTURAL bracket errors (nested `[`, unmatched `]`,
        EOF-in-bracket, `]`-without-`[`, reopen `[..][..]`), empty-segment and
        trailing-`::` rejects. It does NOT know type words, so bracket
        POSITION-legality ("`[` only in an authority segment") is a parse rule.
        Raises AddressError on malformed structure.

    parse(address: str) -> ParsedAddress
        tokenize + interpret segments + validate. Raises AddressError. Owns
        type-word dispatch, segment arity, hash productions, authority
        validation, alias-name validation, fragment grammar, and bracket
        position-legality.

    @dataclass(frozen=True)
    class Hash:
        algo: str            # "sha256" (only sha256 in v0)
        digest: str          # lowercase hex
        is_full: bool        # len == full width for algo (sha256 -> 64)
        is_short: bool       # 8 <= len < full width
        # invariant: for any valid Hash exactly one of is_full / is_short is True

    @dataclass(frozen=True)
    class ParsedAddress:
        type: str                     # "alias" | "web" | "hash"
        folio: Hash
        alias: str | None = None      # set iff type == "alias"
        authority: str | None = None  # set iff type == "web"
        fragment: Hash | None = None  # verifier fragment (always a full hash)
        is_bare_cascade: bool         # property: cascades local stations
        has_station_context: bool     # property: explicit alias/web station

    validate(address) -> bool          # parse() succeeds; never raises, any type
    construct(*, type, folio, alias=None, authority=None, fragment=None) -> str
        Canonical inverse of parse. Emits the canonical (bare) form for hash
        type. Rejects mismatched args (alias type w/o alias, web w/o authority,
        hash w/ alias or authority).

    resolve(parsed: ParsedAddress, station: StationIndex) -> str
        Turn a (possibly short) folio reference into a full digest. NOT an
        existence check: a full hash returns its own digest WITHOUT querying the
        station (even if absent). The station is consulted ONLY to lengthen a
        short hash, station-scoped, detect-and-error.

    class StationIndex(Protocol):
        def folios_with_prefix(self, algo: str, prefix: str) -> list[str]: ...
            # returns matching FULL lowercase-hex digests (0, 1, or many)

    Exceptions:
        class AddressError(Exception)                  # base; parse/tokenize
        class AmbiguousShortHash(AddressError):
            candidates: list[str]                      # colliding full digests
        class ShortHashNotFound(AddressError)          # zero matches

Key pins (see finding-20260529-ztp7): bare `sha256::<full>` == explicit
`hash::sha256::<full>`; `hash::<station>::...` is v1 (rejects); IPv4 authority
accepted unbracketed, IPv6 requires brackets and must be lowercase; whitespace
anywhere rejects; alias = lowercase [a-z0-9-], start/end alnum, 1..32 chars.
"""

from __future__ import annotations

import ipaddress

import pytest

import skein.address as A

# --- rev 3 gate -------------------------------------------------------------
HAVE_REV3 = hasattr(A, "tokenize") and hasattr(A, "AmbiguousShortHash")
pytestmark = pytest.mark.skipif(
    not HAVE_REV3,
    reason="rev 3 `::` addressing not implemented yet (Phase 3 makes this green)",
)

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st
except ImportError:  # pragma: no cover - hypothesis is a declared dev dep
    given = settings = st = None


# --- shared fixtures / builders ---------------------------------------------

FULL = "a" * 64          # valid full sha256 digest (lowercase hex, len 64)
FULL2 = "b" * 64         # a second distinct full digest
SHORT = "a" * 16         # valid short digest (8 <= 16 < 64)


def addr_alias(name="speakbot", digest=FULL):
    # No .strip(): the builder must never silently repair a name, or it would
    # mask a parser that normalizes instead of rejecting.
    return f"alias::{name}::sha256::{digest}"


def addr_web(authority="mesh.alice.com", digest=FULL):
    return f"web::{authority}::sha256::{digest}"


class FakeStation:
    """In-memory StationIndex for resolver tests. Deterministic, no I/O."""

    def __init__(self, full_digests):
        self._digests = list(full_digests)

    def folios_with_prefix(self, algo: str, prefix: str) -> list[str]:
        if algo != "sha256":
            return []
        return [d for d in self._digests if d.startswith(prefix)]


class ExplodingStation:
    """StationIndex that fails if queried — proves resolve() does NOT consult
    the station for a full hash (pin: resolve is reference->digest, not an
    existence check)."""

    def folios_with_prefix(self, algo: str, prefix: str) -> list[str]:
        raise AssertionError("station must not be queried to resolve a full hash")


# ===========================================================================
# Tokenizer — fragment-first split, `::` split, bracket opacity
# ===========================================================================
class TestTokenizerHappy:
    def test_bare_full_hash_two_segments(self):
        t = A.tokenize(f"sha256::{FULL}")
        assert t.segments == ("sha256", FULL)
        assert t.fragment is None

    def test_alias_form_four_segments(self):
        assert A.tokenize(addr_alias()).segments == ("alias", "speakbot", "sha256", FULL)

    def test_web_form_four_segments(self):
        assert A.tokenize(addr_web()).segments == ("web", "mesh.alice.com", "sha256", FULL)

    def test_single_colon_stays_inside_segment(self):
        # host:port uses a SINGLE colon; only `::` splits.
        t = A.tokenize(f"web::host:8080::sha256::{FULL}")
        assert t.segments == ("web", "host:8080", "sha256", FULL)

    def test_fragment_split_at_first_hash(self):
        t = A.tokenize(f"sha256::{FULL}#sha256::{FULL2}")
        assert t.segments == ("sha256", FULL)
        assert t.fragment == f"sha256::{FULL2}"

    def test_fragment_split_is_at_FIRST_hash(self):
        # Only the first '#' delimits; later '#' live in the fragment text.
        t = A.tokenize(f"sha256::{FULL}#a#b")
        assert t.segments == ("sha256", FULL)
        assert t.fragment == "a#b"

    def test_no_fragment_means_none(self):
        assert A.tokenize(f"sha256::{FULL}").fragment is None

    def test_tokenize_returns_tokenized_address(self):
        t = A.tokenize(f"sha256::{FULL}")
        assert isinstance(t, A.TokenizedAddress)
        assert isinstance(t.segments, tuple)

    def test_trailing_hash_is_empty_fragment_not_none(self):
        # `...#` splits to an empty fragment (parser then rejects it).
        t = A.tokenize(f"sha256::{FULL}#")
        assert t.segments == ("sha256", FULL)
        assert t.fragment == ""

    def test_hash_before_double_colon_splits_first(self):
        # Fragment split is FIRST, even before any `::`.
        t = A.tokenize(f"web#sha256::{FULL}")
        assert t.segments == ("web",)
        assert t.fragment == f"sha256::{FULL}"

    @pytest.mark.parametrize("frag_tail", ["frag[", "frag]", "a]b[c"])
    def test_bracket_chars_in_fragment_are_not_validated(self, frag_tail):
        # Fragment is split off FIRST, so the bracket state machine must NOT run
        # over the fragment text — an unmatched `[`/`]` in the fragment portion
        # is just opaque tail text at the tokenizer (the parser judges the
        # fragment grammar separately).
        t = A.tokenize(f"sha256::{FULL}#{frag_tail}")
        assert t.segments == ("sha256", FULL)
        assert t.fragment == frag_tail

    # --- bracket opacity: IPv6 zero-compression `::` must NOT split ---------

    def test_ipv6_bracket_is_opaque(self):
        t = A.tokenize(f"web::[2001:db8::1]::sha256::{FULL}")
        assert t.segments == ("web", "[2001:db8::1]", "sha256", FULL)

    def test_ipv6_bracket_with_port(self):
        t = A.tokenize(f"web::[2001:db8::1]:8080::sha256::{FULL}")
        assert t.segments == ("web", "[2001:db8::1]:8080", "sha256", FULL)

    def test_ipv6_all_zeros_compression_opaque(self):
        t = A.tokenize(f"web::[::1]::sha256::{FULL}")
        assert t.segments == ("web", "[::1]", "sha256", FULL)


class TestTokenizerGauntlet:
    """Malformed structure rejects deterministically — no silent repair."""

    @pytest.mark.parametrize(
        "bad",
        [
            "",                        # empty string
            "::",                      # only a delimiter
            "sha256::",                # trailing `::`
            f"::sha256::{FULL}",       # leading `::` -> empty first segment
            f"web::::sha256::{FULL}",  # `::::` -> empty middle segment
            f"web::::::sha256::{FULL}",# three+ consecutive colons
            "web::host::",             # trailing `::` after a real segment
        ],
    )
    def test_empty_and_trailing_segments_reject(self, bad):
        with pytest.raises(A.AddressError):
            A.tokenize(bad)

    @pytest.mark.parametrize(
        "bad",
        [
            f"web::[2001:db8::1::sha256::{FULL}",    # EOF inside bracket (unmatched [)
            f"web::[[2001:db8::1]]::sha256::{FULL}", # nested [
            f"web::2001:db8::1]::sha256::{FULL}",    # ] with no opening [
            f"web::]::sha256::{FULL}",               # ] outside any authority
            f"web::[::1][::2]::sha256::{FULL}",      # close then reopen in one segment
        ],
    )
    def test_bracket_state_machine_rejects(self, bad):
        with pytest.raises(A.AddressError):
            A.tokenize(bad)

    def test_no_percent_decoding(self):
        # Addresses are not URLs: `%41` stays `%41`, never decoded to 'A'.
        # (Whether it passes authority validation is a later, separate step.)
        t = A.tokenize(f"web::a%41b::sha256::{FULL}")
        assert t.segments == ("web", "a%41b", "sha256", FULL)

    def test_hash_inside_brackets_rejects_via_fragment_first(self):
        # Fragment splits FIRST, so a '#' inside brackets tears the bracket;
        # the resulting unmatched `[` then rejects at bracket validation.
        with pytest.raises(A.AddressError):
            A.tokenize(f"web::[host#1]::sha256::{FULL}")

    def test_null_byte_rejects(self):
        with pytest.raises(A.AddressError):
            A.tokenize("\x00")
        with pytest.raises(A.AddressError):
            A.tokenize(f"web::\x00host::sha256::{FULL}")

    @pytest.mark.parametrize("ws", [" ", "\t", "\n", "\r"])
    def test_whitespace_anywhere_rejects_at_tokenize(self, ws):
        # Pin: whitespace is rejected GLOBALLY at the tokenizer, before bracket
        # opacity or any segment interpretation — never stripped. Covers leading,
        # trailing, intra-segment, intra-bracket, and intra-fragment positions.
        for addr in [
            f"{ws}sha256::{FULL}",
            f"sha256::{FULL}{ws}",
            f"sha256{ws}::{FULL}",
            f"web::host{ws}name::sha256::{FULL}",         # inside a later segment
            f"web::[2001:db8::1{ws}]::sha256::{FULL}",    # inside a bracket
            f"sha256::{FULL}#sha256::{FULL2}{ws}",        # inside the fragment
        ]:
            with pytest.raises(A.AddressError):
                A.tokenize(addr)

    @pytest.mark.parametrize("ctrl", ["\x00", "\x1f", "\x7f", "\x80", "\x9f"])
    def test_control_chars_reject_at_tokenize(self, ctrl):
        # C0 (<0x20), DEL (0x7F), and C1 (0x80-0x9F) are control chars and must
        # reject globally at the tokenizer — none can sit in a valid segment.
        with pytest.raises(A.AddressError):
            A.tokenize(f"web::ho{ctrl}st::sha256::{FULL}")

    @pytest.mark.parametrize("port", ["١٢", "１２", "۴۵"])
    def test_unicode_digit_port_rejects(self, port):
        # `\d` in a regex is Unicode-aware (matches Arabic-Indic / full-width
        # digits) and int() accepts them, but a port must be ASCII `[0-9]`.
        # Both the bracketed-IPv6 and bare-host port paths must reject. (Hardening
        # Phase 4, gremlin round 1: regex/validator must agree on "digit".)
        with pytest.raises(A.AddressError):
            A.parse(f"web::[::1]:{port}::sha256::{FULL}")
        with pytest.raises(A.AddressError):
            A.parse(f"web::host:{port}::sha256::{FULL}")

    def test_overlong_address_rejects(self):
        # An address longer than the cap rejects deterministically (and validate
        # stays non-raising). Bounds tokenize's O(n) scan on untrusted input.
        # (Hardening Phase 4, gremlin round 1.)
        huge = "sha256::" + "a" * 5000
        assert len(huge) > 4096
        with pytest.raises(A.AddressError):
            A.tokenize(huge)
        assert A.validate(huge) is False

    @pytest.mark.parametrize(
        "cf",
        ["​",  # ZERO WIDTH SPACE
         "‌",  # ZERO WIDTH NON-JOINER
         "‍",  # ZERO WIDTH JOINER
         "⁠",  # WORD JOINER
         "﻿",  # ZERO WIDTH NO-BREAK SPACE / BOM
         "‎",  # LEFT-TO-RIGHT MARK
         "‏",  # RIGHT-TO-LEFT MARK
         "‮",  # RIGHT-TO-LEFT OVERRIDE (Trojan Source)
         "⁦",  # LEFT-TO-RIGHT ISOLATE
         "­"],  # SOFT HYPHEN
    )
    def test_invisible_format_chars_reject_at_tokenize(self, cf):
        # Unicode format chars (category Cf) are invisible and enable visual
        # spoofing; they reject globally at tokenize like other control chars,
        # in EVERY position (segment, bracket, fragment). They have no `isspace()`
        # nor C0/C1 membership, so they need the explicit Cf gate. (Hardening
        # Phase 4, oracle round 1.)
        for addr in [
            f"web::ho{cf}st::sha256::{FULL}",          # inside a segment
            f"alias::spe{cf}ak::sha256::{FULL}",       # inside an alias name
            f"web::[2001:db8::{cf}1]::sha256::{FULL}",  # inside a bracket
            f"sha256::{FULL}#sha256::{FULL2}{cf}",      # inside the fragment
        ]:
            with pytest.raises(A.AddressError):
                A.tokenize(addr)

    def test_visible_idn_still_deferred_to_authority_validation(self):
        # The Cf carve-out must NOT swallow VISIBLE non-ASCII: an IDN byte
        # (category Ll, not Cf) still tokenizes structurally and rejects later at
        # authority validation, preserving the validate-not-convert specific error
        # (this is the design's intentional deferral, kept intact by the Cf gate).
        addr = f"web::münchen.de::sha256::{FULL}"
        assert len(A.tokenize(addr).segments) == 4   # tokenization succeeds
        with pytest.raises(A.AddressError):          # rejection happens at parse
            A.parse(addr)

    def test_double_colon_inside_bracket_is_opaque_end_to_end(self):
        # Deterministic companion to the property test: a real IPv6 literal with
        # `::` inside brackets tokenizes to one segment AND parses.
        addr = f"web::[2001:db8::1]::sha256::{FULL}"
        assert A.tokenize(addr).segments == ("web", "[2001:db8::1]", "sha256", FULL)
        assert A.parse(addr).authority == "[2001:db8::1]"


# ===========================================================================
# Type words
# ===========================================================================
class TestTypeWords:
    def test_v0_type_words_recognized(self):
        assert A.parse(addr_alias()).type == "alias"
        assert A.parse(addr_web()).type == "web"
        assert A.parse(f"hash::sha256::{FULL}").type == "hash"

    @pytest.mark.parametrize("reserved", ["ipfs", "ssh", "oidc", "peer"])
    def test_reserved_type_words_reject_in_v0(self, reserved):
        # Recognized as reserved, but unsupported transports reject (not silently
        # accepted).
        with pytest.raises(A.AddressError):
            A.parse(f"{reserved}::whatever::sha256::{FULL}")

    def test_unknown_type_word_rejects(self):
        with pytest.raises(A.AddressError):
            A.parse(f"gopher::host::sha256::{FULL}")

    def test_hash_with_station_rejects_v1_deferred(self):
        # Station-scoped content routing is v1; reject in v0.
        with pytest.raises(A.AddressError):
            A.parse(f"hash::mesh.alice.com::sha256::{FULL}")

    def test_hash_with_short_digest_rejects(self):
        # `hash`/bare has no station context; a short hash needs one.
        with pytest.raises(A.AddressError):
            A.parse(f"hash::sha256::{SHORT}")

    def test_hash_type_excess_segment_rejects(self):
        # hash expects exactly its folio; a trailing segment after a valid folio
        # must reject (a parser ignoring trailing segments would otherwise pass).
        with pytest.raises(A.AddressError):
            A.parse(f"hash::sha256::{FULL}::extra")

    @pytest.mark.parametrize("type_word", ["alias", "web"])
    def test_type_word_with_wrong_segment_count_rejects(self, type_word):
        # alias/web require exactly 4 segments; 3 is too few.
        with pytest.raises(A.AddressError):
            A.parse(f"{type_word}::sha256::{FULL}")

    @pytest.mark.parametrize("type_word", ["alias", "web"])
    def test_excess_segments_reject(self, type_word):
        # arity symmetry: both type words reject a 5th segment.
        addr = (f"alias::speakbot::extra::sha256::{FULL}" if type_word == "alias"
                else f"web::host::extra::sha256::{FULL}")
        with pytest.raises(A.AddressError):
            A.parse(addr)

    @pytest.mark.parametrize(
        "addr",
        [
            f"SHA256::{FULL}", f"Sha256::{FULL}",     # algo word case
            f"HASH::sha256::{FULL}",                  # type word case
            f"WEB::mesh.alice.com::sha256::{FULL}",
            f"ALIAS::speakbot::sha256::{FULL}",
        ],
    )
    def test_type_and_algo_words_are_case_sensitive(self, addr):
        # A case-folding dispatch would accept these and pass every other test;
        # type words and the algo word are lowercase-only.
        with pytest.raises(A.AddressError):
            A.parse(addr)


# ===========================================================================
# Folio-shaped token: full_hash and short_hash productions
# ===========================================================================
class TestFolioHashGrammar:
    def test_full_hash_exactly_64_lowercase_hex(self):
        p = A.parse(f"sha256::{FULL}")
        assert p.folio.algo == "sha256"
        assert p.folio.digest == FULL
        assert p.folio.is_full and not p.folio.is_short

    @pytest.mark.parametrize(
        "digest",
        [
            "a" * 63,        # one short of full (bare: short needs station)
            "a" * 65,        # one over full
            "A" * 64,        # uppercase rejected (lowercase only)
            "g" * 64,        # non-hex char
            "a" * 64 + " ",  # trailing space
        ],
    )
    def test_full_hash_malformed_rejects(self, digest):
        with pytest.raises(A.AddressError):
            A.parse(f"sha256::{digest}")

    # --- short-hash boundaries: both sides of every fencepost --------------

    @pytest.mark.parametrize("n", [8, 9, 16, 32, 63])
    def test_short_hash_at_and_above_minimum_accepted(self, n):
        # 8 <= n < 64, with explicit station context -> short.
        p = A.parse(addr_alias(digest="a" * n))
        assert p.folio.is_short and not p.folio.is_full
        assert p.folio.digest == "a" * n

    def test_len_64_in_station_context_is_full_not_short(self):
        p = A.parse(addr_alias(digest="a" * 64))
        assert p.folio.is_full and not p.folio.is_short

    @pytest.mark.parametrize("n", [0, 1, 7])
    def test_short_hash_below_minimum_rejects(self, n):
        with pytest.raises(A.AddressError):
            A.parse(addr_alias(digest="a" * n))

    def test_short_hash_uppercase_rejects(self):
        with pytest.raises(A.AddressError):
            A.parse(addr_alias(digest="A" * 16))

    def test_bare_short_hash_rejects(self):
        # A short hash with no station context never parses (cannot cascade).
        with pytest.raises(A.AddressError):
            A.parse(f"sha256::{SHORT}")

    # --- is_full / is_short invariant: mutually exclusive AND exhaustive ----

    @pytest.mark.parametrize(
        "addr, expected_full",
        [
            (f"sha256::{FULL}", True),
            ("alias::s::sha256::" + "a" * 64, True),   # full digest IN station ctx
            (f"web::host::sha256::{'a' * 64}", True),  # full digest in web ctx
            ("alias::s::sha256::" + "a" * 8, False),
            ("alias::s::sha256::" + "a" * 63, False),
            (f"web::host::sha256::{'a' * 8}", False),  # short digest in web ctx
        ],
    )
    def test_hash_flags_exactly_one_true(self, addr, expected_full):
        # expected_full pinned per-LENGTH (not per-type): a full digest under an
        # alias station must still be is_full, so the flags can't be hardcoded
        # to "bare=full, alias=short".
        h = A.parse(addr).folio
        assert h.is_full is expected_full
        assert h.is_short is (not expected_full)
        assert h.is_full ^ h.is_short

    # --- only sha256 in v0 -------------------------------------------------

    @pytest.mark.parametrize(
        "addr",
        [
            "sha512::" + "a" * 128,
            "blake3::" + "a" * 64,
            f"alias::speakbot::sha512::{FULL}",
            f"web::mesh.alice.com::blake3::{FULL}",
            f"hash::sha512::{FULL}",
        ],
    )
    def test_non_sha256_algorithms_reject(self, addr):
        with pytest.raises(A.AddressError):
            A.parse(addr)


# ===========================================================================
# Chain-hash (single colon) vs address-hash (double colon)
# ===========================================================================
class TestChainVsAddressHash:
    def test_single_colon_chain_hash_is_not_an_address(self):
        with pytest.raises(A.AddressError):
            A.parse(f"sha256:{FULL}")

    def test_double_colon_is_an_address(self):
        assert A.parse(f"sha256::{FULL}").folio.digest == FULL

    def test_single_colon_inside_alias_station_does_not_misparse(self):
        # The single colon does not split, so this is 3 segments
        # [alias, speakbot, "sha256:<hex>"] and rejects (no full folio token).
        with pytest.raises(A.AddressError):
            A.parse(f"alias::speakbot::sha256:{FULL}")

    def test_single_colon_inside_web_does_not_misparse(self):
        with pytest.raises(A.AddressError):
            A.parse(f"web::host::sha256:{FULL}")


# ===========================================================================
# Authority canonicalization — validate, do NOT convert (Decision A)
# ===========================================================================
class TestAuthorityCanonicalization:
    @pytest.mark.parametrize(
        "authority",
        [
            "mesh.alice.com",
            "a.b.c.example.org",
            "xn--mnchen-3ya.de",     # already-punycode IS canonical ASCII
            "host:8080",
            "single-label",
            "a--b.example.com",      # internal double hyphen is legal
            "192.168.1.1",           # IPv4 dotted-decimal accepted (unbracketed)
            "192.168.1.1:8080",
            "a" * 63 + ".com",       # label EXACTLY 63 octets accepted
        ],
    )
    def test_canonical_ascii_authorities_accepted(self, authority):
        p = A.parse(addr_web(authority=authority))
        assert p.type == "web"
        assert p.authority == authority   # preserved verbatim (validate-not-convert)

    @pytest.mark.parametrize(
        "authority",
        [
            "Mesh.Alice.com",        # uppercase
            "münchen.de",            # raw IDN / non-ASCII
            "host%41name.com",       # percent-encoding
            "exämple.com",           # non-ASCII byte
            "a" * 64 + ".com",       # label > 63 octets
            "host:99999",            # port out of range
            "host:0",                # port 0 invalid
            "host:65536",            # first port over max
            "host:999999",           # 6-digit port (over max, and over length)
            "host:080",              # non-canonical leading-zero port
            "host:abc",              # non-numeric port
            "host:",                 # empty port
            "-leading.com",          # label cannot start with hyphen
            "trailing-.com",         # label cannot end with hyphen
            "",                      # empty authority
            "a..b.com",              # empty label
            " host.com",             # leading whitespace
            "host.com ",             # trailing whitespace
            "host\t.com",            # tab
            "host\n.com",            # newline
            "has_underscore.com",    # underscore not a DNS char
            "example.com.",          # trailing dot
            ".example.com",          # leading dot
            "host/name.com",         # slash
            "2001:db8:0:0:0:0:0:1",  # unbracketed IPv6 (colons not valid in a host)
            # IPv4-literal-shaped but NOT canonical IPv4 — ambiguous with an IP
            # literal under inet_aton-style resolvers (octal/decimal-IP confusable
            # class), so reject rather than accept as a DNS name. (Hardening
            # Phase 4, gpt-5.5 round 2.)
            "192.168.001.001",       # leading-zero octets (octal-IP confusable)
            "999.999.999.999",       # out-of-range dotted quad
            "01.02.03.04",           # all leading-zero octets
            "2130706433",            # decimal-int form of 127.0.0.1
            "1.2.3",                 # short dotted form (inet_aton-accepted)
            "1.2.3.4.5",             # five numeric labels
            "123",                   # bare all-numeric label
            "192.168.001.001:8080",  # same, carrying a port
            "0x7f000001",            # hex-int form of 127.0.0.1 (inet_aton)
            "0X7f000001",            # uppercase 0X hex-int form
            "0x7f.0x0.0x0.0x1",      # dotted hex form
            "017700000001",          # octal-int form of 127.0.0.1
        ],
    )
    def test_non_canonical_authorities_reject(self, authority):
        with pytest.raises(A.AddressError):
            A.parse(addr_web(authority=authority))

    @pytest.mark.parametrize(
        "authority",
        # Hosts with at least one non-numeric label are ordinary DNS names and
        # must still be accepted — the IPv4-shaped reject must not over-reach.
        # incl. a 0x-prefixed label that is NOT a bare hex-int host (it has a
        # non-integer label), so it is an ordinary DNS name, not IPv4-confusable.
        ["123.com", "v1.api.com", "1station.example.com", "a1.b2.c3",
         "0xdeadbeef.example.com"],
    )
    def test_numeric_dns_labels_with_alpha_still_accepted(self, authority):
        assert A.parse(addr_web(authority=authority)).authority == authority

    @pytest.mark.parametrize("port", [1, 80, 443, 8080, 65535])
    def test_valid_ports_accepted(self, port):
        assert A.parse(addr_web(authority=f"host:{port}")).authority == f"host:{port}"

    def test_dns_total_length_253_accepted_254_rejected(self):
        # 253 = 63 + 1 + 63 + 1 + 63 + 1 + 61  (labels 63,63,63,61 + 3 dots)
        ok = ".".join(["a" * 63, "a" * 63, "a" * 63, "a" * 61])
        assert len(ok) == 253
        assert A.validate(addr_web(authority=ok)) is True
        too_long = ok + "a"   # 254
        assert len(too_long) == 254
        assert A.validate(addr_web(authority=too_long)) is False
        # The 253-char limit applies to the DNS HOST, not host:port — a
        # max-length host with a port must still be accepted.
        assert A.validate(addr_web(authority=f"{ok}:1")) is True

    def test_percent_in_authority_rejects_at_parse(self):
        # Tokenizer passes `%41` through; parser must reject it.
        with pytest.raises(A.AddressError):
            A.parse(f"web::a%41b::sha256::{FULL}")

    # --- IPv6 literals: stdlib `ipaddress` is the validity oracle ----------

    @pytest.mark.parametrize(
        "authority",
        [
            "[2001:db8::1]",
            "[2001:db8::1]:8080",
            "[::1]",
            "[::ffff:192.0.2.1]",            # IPv4-mapped, valid
            "[::ffff:192.0.2.1]:8080",       # IPv4-mapped with port
            "[2001:db8:0:0:0:0:0:1]",        # uncompressed (validity, not canonical spelling)
            "[::1]:65535",
        ],
    )
    def test_valid_ipv6_literals_accepted(self, authority):
        assert A.parse(addr_web(authority=authority)).authority == authority

    @pytest.mark.parametrize(
        "authority",
        [
            "[2001:db8::1::2]",      # two '::' — invalid per ipaddress
            "[gggg::1]",             # non-hex group
            "[12345::1]",            # group > 4 hex chars
            "[]",                    # empty literal
            "[2001:db8::1]:99999",   # port out of range
            "[2001:db8::1]:65536",   # first port over max
            "[2001:DB8::1]",         # uppercase hex — authority must be lowercase
            "[::1]abc",              # junk suffix after the bracket
            "[::1]8080",             # suffix without the `:` separator
            "[::1]:",                # empty port
            "[::1]:abc",             # non-numeric port
            "[::1]:0",               # port 0
            "[::1]:65536",           # IPv6 port over max
            "[::1]:080",             # non-canonical leading-zero port
            "[192.168.1.1]",         # bracketed IPv4 — brackets mean IPv6 only
            "[192.168.1.1]:8080",
            "[mesh.alice.com]",      # bracketed DNS name
        ],
    )
    def test_invalid_or_noncanonical_ipv6_literals_reject(self, authority):
        with pytest.raises(A.AddressError):
            A.parse(addr_web(authority=authority))

    @pytest.mark.parametrize("zone_authority", ["[fe80::1%eth0]", "[fe80::1%25eth0]"])
    def test_ipv6_zone_id_rejects(self, zone_authority):
        # Decision B: `%` in an authority rejects, both the bare RFC-4007 form
        # and the RFC-6874 `%25` form. NB stdlib ipaddress ACCEPTS the bare form,
        # so the parser must reject `%` explicitly, before delegating to ipaddress.
        with pytest.raises(A.AddressError):
            A.parse(addr_web(authority=zone_authority))

    def test_zone_id_tokenizes_then_fails_at_validation(self):
        # Decision B is a VALIDATION rule, not a TOKENIZATION rule: the bracket
        # stays opaque (segment forms), rejection happens at authority validation.
        full = f"web::[fe80::1%eth0]::sha256::{FULL}"
        assert len(A.tokenize(full).segments) == 4   # tokenization OK
        with pytest.raises(A.AddressError):           # validation rejects
            A.parse(full)


# ===========================================================================
# Parser-level bracket position legality (NOT a tokenizer rule)
# ===========================================================================
class TestBracketPosition:
    """The tokenizer applies opacity everywhere it sees balanced brackets, but
    `[` is only LEGAL in the authority segment of an authority-bearing type.
    Brackets elsewhere tokenize fine yet reject at parse."""

    @pytest.mark.parametrize(
        "addr",
        [
            f"alias::spe[ak]bot::sha256::{FULL}",  # bracket in alias name
            f"sha256::[{FULL}]",                   # bracket around a bare hash
            f"hash::sha256::[{FULL}]",             # bracket in folio of hash type
        ],
    )
    def test_bracket_outside_authority_rejects_at_parse(self, addr):
        # The load-bearing claim of pin 1 is that bracket handling is layered:
        # tokenization SUCCEEDS (opacity is blind to position) and rejection
        # happens at parse. The `tokenize()`-doesn't-raise assertion is what
        # proves brackets are NOT a tokenizer rule. (The parse rejection itself
        # is overdetermined — `[` also fails the alias/hex content grammar — so
        # a separate "position" check is not independently isolable; that's fine,
        # the layering is the property under test.)
        A.tokenize(addr)
        with pytest.raises(A.AddressError):
            A.parse(addr)


# ===========================================================================
# Bare forms, cascade, and hash equivalence
# ===========================================================================
class TestBareForms:
    def test_bare_full_hash_cascades(self):
        p = A.parse(f"sha256::{FULL}")
        assert p.type == "hash"
        assert p.is_bare_cascade
        assert not p.has_station_context
        assert p.alias is None and p.authority is None   # no cross-population

    def test_bare_full_hash_with_fragment_still_cascades(self):
        p = A.parse(f"sha256::{FULL}#sha256::{FULL2}")
        assert p.is_bare_cascade and not p.has_station_context

    def test_explicit_hash_type_equals_bare(self):
        # Pin: `hash::sha256::<full>` is the explicit spelling of the bare form.
        assert A.parse(f"hash::sha256::{FULL}") == A.parse(f"sha256::{FULL}")

    def test_explicit_hash_type_is_bare_cascade(self):
        p = A.parse(f"hash::sha256::{FULL}")
        assert p.is_bare_cascade and not p.has_station_context

    def test_alias_form_has_station_context(self):
        p = A.parse(addr_alias())
        assert p.has_station_context and not p.is_bare_cascade
        assert p.alias == "speakbot"
        assert p.authority is None       # alias must not carry an authority

    def test_web_form_has_station_context(self):
        p = A.parse(addr_web())
        assert p.has_station_context and not p.is_bare_cascade
        assert p.authority == "mesh.alice.com"
        assert p.alias is None           # web must not carry an alias

    def test_alias_shorthand_short_hash_equals_explicit(self):
        shorthand = A.parse(f"speakbot::sha256::{SHORT}")
        explicit = A.parse(addr_alias(digest=SHORT))
        assert shorthand == explicit
        assert shorthand.folio.is_short

    def test_web_station_context_accepts_short_hash(self):
        # Short hashes are valid with ANY explicit station context, not just alias.
        p = A.parse(addr_web(digest=SHORT))
        assert p.type == "web"
        assert p.has_station_context and not p.is_bare_cascade
        assert p.folio.is_short and p.folio.digest == SHORT

    def test_alias_shorthand_equals_explicit(self):
        # `<alias>::<folio>` is shorthand for `alias::<alias>::<folio>` — and
        # produces an identical ParsedAddress (full structural equality).
        shorthand = A.parse(f"speakbot::sha256::{FULL}")
        explicit = A.parse(addr_alias())
        assert shorthand == explicit
        assert shorthand.has_station_context and not shorthand.is_bare_cascade

    def test_short_hash_never_bare_cascade(self):
        # The real invariant: a BARE short hash never parses (so never cascades).
        with pytest.raises(A.AddressError):
            A.parse(f"sha256::{SHORT}")


# ===========================================================================
# Reserved alias names (skein init guard)
# ===========================================================================
class TestReservedAliasNames:
    RESERVED = ["alias", "web", "hash", "ipfs", "ssh", "oidc", "peer",
                "sha256", "sha512", "blake3"]

    @pytest.mark.parametrize("name", RESERVED)
    def test_reserved_names_flagged(self, name):
        assert A.is_reserved_alias(name) is True

    @pytest.mark.parametrize("name", ["speakbot", "my-station", "alice", "skein-dev"])
    def test_ordinary_names_not_reserved(self, name):
        assert A.is_reserved_alias(name) is False

    @pytest.mark.parametrize("name", RESERVED)
    def test_reserved_alias_in_explicit_form_rejects(self, name):
        with pytest.raises(A.AddressError):
            A.parse(f"alias::{name}::sha256::{FULL}")

    # Only the hash-algorithm names actually reach shorthand-alias dispatch:
    # `<name>::sha256::<full>` is a would-be alias for them. The type words
    # dispatch as types instead (alias/web reject on arity, ipfs/ssh/oidc/peer
    # as reserved transports, and `hash::sha256::<full>` is VALID), so they are
    # covered elsewhere and must NOT appear here.
    SHORTHAND_RESERVED = ["sha256", "sha512", "blake3"]

    @pytest.mark.parametrize("name", SHORTHAND_RESERVED)
    def test_reserved_alias_in_shorthand_form_rejects(self, name):
        with pytest.raises(A.AddressError):
            A.parse(f"{name}::sha256::{FULL}")


# ===========================================================================
# Alias-name grammar (contract decision — spec left this open)
# ===========================================================================
class TestAliasNameGrammar:
    """Alias = lowercase ASCII [a-z0-9-], must start AND end with [a-z0-9]
    (leading digit allowed), 1..32 chars, not reserved."""

    @pytest.mark.parametrize(
        "name", ["speakbot", "my-station", "a1", "x", "1station", "a", "a" * 32, "a--b"]
    )
    def test_valid_alias_names_accepted(self, name):
        assert A.parse(f"alias::{name}::sha256::{FULL}").alias == name

    @pytest.mark.parametrize(
        "name",
        [
            "Speakbot",        # uppercase
            "-lead",           # leading hyphen
            "trail-",          # trailing hyphen
            "has_underscore",  # underscore not in charset
            "has.dot",         # dot not in charset
            "a" * 33,          # too long
            "spül",            # non-ASCII
            "speak%41bot",     # percent
            "speak bot",       # internal space
        ],
    )
    def test_invalid_alias_names_reject(self, name):
        with pytest.raises(A.AddressError):
            A.parse(f"alias::{name}::sha256::{FULL}")

    def test_empty_alias_name_rejects(self):
        # `alias::::sha256::FULL` -> empty name segment.
        with pytest.raises(A.AddressError):
            A.parse(f"alias::::sha256::{FULL}")

    def test_shorthand_empty_alias_rejects(self):
        # `::sha256::FULL` -> empty leading segment (would-be shorthand alias).
        with pytest.raises(A.AddressError):
            A.parse(f"::sha256::{FULL}")

    def test_shorthand_empty_middle_segment_rejects(self):
        # `speakbot::::sha256::FULL` -> empty middle segment.
        with pytest.raises(A.AddressError):
            A.parse(f"speakbot::::sha256::{FULL}")

    def test_alias_name_with_leading_or_trailing_space_rejects(self):
        for name in (" speakbot", "speakbot "):
            with pytest.raises(A.AddressError):
                A.parse(f"alias::{name}::sha256::{FULL}")


# ===========================================================================
# Verifier fragment
# ===========================================================================
class TestFragment:
    def test_fragment_is_a_full_hash(self):
        p = A.parse(f"sha256::{FULL}#sha256::{FULL2}")
        assert p.fragment is not None
        assert p.fragment.algo == "sha256"
        assert p.fragment.digest == FULL2
        assert p.fragment.is_full

    @pytest.mark.parametrize(
        "frag",
        [
            "sha256::" + "a" * 16,     # fragment must be FULL, not short
            "sha256::" + "A" * 64,     # uppercase
            "sha512::" + "a" * 128,    # wrong algo
            "notahash",                # wrong grammar
            "",                        # empty fragment after '#'
            f"sha256::{FULL2}#junk",   # trailing extra after a valid fragment
            # The fragment grammar is EXACTLY `sha256::<64-hex>` — a bare full
            # hash. A fragment that is itself a valid *address* of any other
            # form must reject; an implementation that resolves the fragment by
            # recursively parsing it would wrongly accept these (the
            # `hash::sha256::<full>` case is sharpest — it == the bare form, so
            # a recursive parse yields the correct folio and silently passes).
            f"alias::speakbot::sha256::{FULL2}",
            f"speakbot::sha256::{FULL2}",
            f"web::mesh.alice.com::sha256::{FULL2}",
            f"hash::sha256::{FULL2}",
        ],
    )
    def test_malformed_fragment_rejects(self, frag):
        with pytest.raises(A.AddressError):
            A.parse(f"sha256::{FULL}#{frag}")

    def test_leading_hash_empty_body_rejects(self):
        with pytest.raises(A.AddressError):
            A.parse(f"#sha256::{FULL2}")

    @pytest.mark.parametrize(
        "body",
        [
            f"sha256::{FULL}",                  # bare
            f"alias::speakbot::sha256::{FULL}", # alias
            f"speakbot::sha256::{FULL}",        # alias shorthand
            f"hash::sha256::{FULL}",            # explicit hash
            f"web::mesh.alice.com::sha256::{FULL}",
        ],
    )
    def test_fragment_supported_on_every_address_form(self, body):
        # A verifier fragment is valid on ALL forms, not just bare hashes — an
        # implementation must not drop/reject it on alias/web/hash.
        p = A.parse(f"{body}#sha256::{FULL2}")
        assert p.fragment is not None and p.fragment.digest == FULL2


# ===========================================================================
# Resolver — short-hash detect-and-error; full hashes pass through
# ===========================================================================
class TestResolver:
    def test_unique_short_hash_resolves(self):
        station = FakeStation([FULL, FULL2])
        p = A.parse(addr_alias(digest=FULL[:16]))   # prefix of FULL only
        assert A.resolve(p, station) == FULL

    def test_ambiguous_short_hash_errors_with_candidates(self):
        shared = "ab" * 8            # 16-char common prefix
        d1, d2 = shared + "0" * 48, shared + "1" * 48
        station = FakeStation([d1, d2])
        p = A.parse(addr_alias(digest=shared))
        assert p.folio.digest == shared          # the parsed short hash is the prefix
        with pytest.raises(A.AmbiguousShortHash) as exc:
            A.resolve(p, station)
        assert set(exc.value.candidates) == {d1, d2}   # colliding fulls returned

    def test_ambiguous_candidates_are_sorted_deterministically(self):
        # The candidate order must be canonical (sorted), not leak the order a
        # StationIndex happened to return matches in. (Hardening Phase 4, oracle
        # round 2.)
        shared = "ab" * 8
        d1, d2 = shared + "0" * 48, shared + "1" * 48
        p = A.parse(addr_alias(digest=shared))
        with pytest.raises(A.AmbiguousShortHash) as e1:
            A.resolve(p, FakeStation([d1, d2]))
        with pytest.raises(A.AmbiguousShortHash) as e2:
            A.resolve(p, FakeStation([d2, d1]))   # reversed station order
        assert e1.value.candidates == e2.value.candidates == sorted([d1, d2])

    def test_short_hash_not_found_errors(self):
        station = FakeStation([FULL2])
        p = A.parse(addr_alias(digest="c" * 16))
        with pytest.raises(A.ShortHashNotFound):
            A.resolve(p, station)

    def test_full_hash_present_resolves_to_itself(self):
        station = FakeStation([FULL])
        assert A.resolve(A.parse(addr_alias(digest=FULL)), station) == FULL

    def test_full_hash_absent_still_resolves_to_itself(self):
        # Pin: resolve() is NOT an existence check. A full digest is already
        # resolved and returns as-is even if the station does not hold it.
        station = FakeStation([FULL2])
        assert A.resolve(A.parse(addr_alias(digest=FULL)), station) == FULL

    def test_bare_full_hash_resolves_to_itself(self):
        # No station context; the full digest needs no lookup.
        station = FakeStation([])
        assert A.resolve(A.parse(f"sha256::{FULL}"), station) == FULL

    def test_full_hash_never_queries_station(self):
        # The pin made testable: resolving a full hash must NOT call the index.
        assert A.resolve(A.parse(addr_alias(digest=FULL)), ExplodingStation()) == FULL
        assert A.resolve(A.parse(f"sha256::{FULL}"), ExplodingStation()) == FULL

    def test_explicit_hash_type_resolves_like_bare(self):
        assert A.resolve(A.parse(f"hash::sha256::{FULL}"), ExplodingStation()) == FULL

    def test_resolve_checks_fullness_intrinsically_not_via_property(self):
        # resolve() must decide full-vs-short by digest length, not the
        # overridable is_full property: a short-digest folio whose subclass lies
        # is_full=True must still be lengthened against the station, never
        # returned as-is. (Final holistic pass, gpt-5.5.)
        class LieHash(A.Hash):
            @property
            def is_full(self):
                return True

        short = "a" * 16
        full = short + "b" * 48                      # full digest with `short` as prefix
        p = A.ParsedAddress(type="alias", alias="speakbot",
                            folio=LieHash("sha256", short))
        assert A.resolve(p, FakeStation([full])) == full   # consulted the station

    def test_web_short_hash_resolves(self):
        # Short-hash resolution works under a web station context, not just alias.
        station = FakeStation([FULL, FULL2])
        assert A.resolve(A.parse(addr_web(digest=FULL[:16])), station) == FULL

    def test_web_full_hash_never_queries_station(self):
        assert A.resolve(A.parse(addr_web(digest=FULL)), ExplodingStation()) == FULL


# ===========================================================================
# validate() / construct()
# ===========================================================================
class TestValidateConstruct:
    @pytest.mark.parametrize(
        "address",
        [f"sha256::{FULL}", addr_alias(), addr_web(),
         f"web::[::1]::sha256::{FULL}", f"hash::sha256::{FULL}"],
    )
    def test_validate_true_for_valid(self, address):
        assert A.validate(address) is True

    @pytest.mark.parametrize("address", ["", "::", f"sha256:{FULL}", "münchen::x"])
    def test_validate_false_for_invalid(self, address):
        assert A.validate(address) is False

    @pytest.mark.parametrize("junk", [None, 123, b"bytes", [], {}, object()])
    def test_validate_non_string_returns_false_not_raises(self, junk):
        assert A.validate(junk) is False

    @pytest.mark.parametrize("junk", ["\x00", "#" * 1000, "\x00" * 10,
                                      f"web::host:{'9' * 5000}::sha256::{FULL}"])
    def test_validate_pathological_string_returns_false_not_raises(self, junk):
        # The 5000-digit port must NOT escape as ValueError from int() — it must
        # be rejected (length-capped) before conversion. validate() never raises.
        assert A.validate(junk) is False

    # --- construct round-trips ---------------------------------------------

    def test_construct_round_trips_alias(self):
        p = A.parse(addr_alias())
        assert A.parse(A.construct(type="alias", alias="speakbot", folio=p.folio)) == p

    def test_construct_round_trips_alias_short_hash(self):
        p = A.parse(addr_alias(digest=SHORT))
        assert A.parse(A.construct(type="alias", alias="speakbot", folio=p.folio)) == p

    def test_construct_round_trips_web_with_fragment(self):
        p = A.parse(f"web::mesh.alice.com::sha256::{FULL}#sha256::{FULL2}")
        rebuilt = A.construct(type="web", authority="mesh.alice.com",
                              folio=p.folio, fragment=p.fragment)
        assert A.parse(rebuilt) == p

    def test_construct_round_trips_hash_emits_canonical_bare(self):
        p = A.parse(f"hash::sha256::{FULL}")
        rebuilt = A.construct(type="hash", folio=p.folio)
        assert rebuilt == f"sha256::{FULL}"   # canonical form is bare
        assert A.parse(rebuilt) == p

    # --- construct rejects mismatched args ---------------------------------

    def test_construct_alias_without_alias_rejects(self):
        p = A.parse(addr_alias())
        with pytest.raises(A.AddressError):
            A.construct(type="alias", folio=p.folio)

    def test_construct_web_without_authority_rejects(self):
        p = A.parse(addr_web())
        with pytest.raises(A.AddressError):
            A.construct(type="web", folio=p.folio)

    def test_construct_hash_with_authority_rejects(self):
        p = A.parse(f"sha256::{FULL}")
        with pytest.raises(A.AddressError):
            A.construct(type="hash", authority="mesh.alice.com", folio=p.folio)

    def test_construct_hash_with_alias_rejects(self):
        p = A.parse(f"sha256::{FULL}")
        with pytest.raises(A.AddressError):
            A.construct(type="hash", alias="speakbot", folio=p.folio)

    def test_construct_web_with_alias_rejects(self):
        p = A.parse(addr_web())
        with pytest.raises(A.AddressError):
            A.construct(type="web", authority="mesh.alice.com",
                        alias="speakbot", folio=p.folio)

    def test_construct_alias_with_authority_rejects(self):
        p = A.parse(addr_alias())
        with pytest.raises(A.AddressError):
            A.construct(type="alias", alias="speakbot",
                        authority="mesh.alice.com", folio=p.folio)

    def test_construct_hash_with_short_folio_rejects(self):
        # Canonical hash form is bare, and a bare short hash is invalid — so
        # construct must refuse to emit one.
        short_folio = A.parse(addr_alias(digest=SHORT)).folio
        with pytest.raises(A.AddressError):
            A.construct(type="hash", folio=short_folio)

    def test_construct_round_trips_web_short_hash(self):
        p = A.parse(addr_web(digest=SHORT))
        rebuilt = A.construct(type="web", authority="mesh.alice.com", folio=p.folio)
        assert A.parse(rebuilt) == p

    def test_construct_round_trips_alias_with_fragment(self):
        p = A.parse(f"alias::speakbot::sha256::{FULL}#sha256::{FULL2}")
        rebuilt = A.construct(type="alias", alias="speakbot",
                              folio=p.folio, fragment=p.fragment)
        assert A.parse(rebuilt) == p

    def test_construct_round_trips_hash_with_fragment(self):
        # The bare/hash emission path carries a fragment too: `sha256::<f>#<frag>`.
        p = A.parse(f"sha256::{FULL}#sha256::{FULL2}")
        rebuilt = A.construct(type="hash", folio=p.folio, fragment=p.fragment)
        assert rebuilt == f"sha256::{FULL}#sha256::{FULL2}"
        assert A.parse(rebuilt) == p

    def test_construct_validates_values_not_just_presence(self):
        # construct must reject INVALID component values, not emit a string that
        # only blows up later under parse().
        full_folio = A.parse(f"sha256::{FULL}").folio
        short_frag = A.parse(addr_alias(digest=SHORT)).folio  # short != full fragment
        with pytest.raises(A.AddressError):
            A.construct(type="alias", alias="Bad Name", folio=full_folio)
        with pytest.raises(A.AddressError):
            A.construct(type="web", authority="münchen.de", folio=full_folio)
        with pytest.raises(A.AddressError):
            A.construct(type="gopher", folio=full_folio)
        with pytest.raises(A.AddressError):
            A.construct(type="hash", folio=full_folio, fragment=short_frag)

    # --- construct rejects non-string components as AddressError ------------
    # construct() takes caller-supplied components; a non-string alias /
    # authority / digest must raise AddressError (the documented contract),
    # never leak a bare TypeError from `re.fullmatch`/membership checks.
    # (Hardening Phase 4, syndic round 1.)

    def test_construct_non_string_alias_rejects_as_address_error(self):
        full_folio = A.parse(f"sha256::{FULL}").folio
        for bad in [123, None, b"speakbot", ["speakbot"]]:
            with pytest.raises(A.AddressError):
                A.construct(type="alias", alias=bad, folio=full_folio)

    def test_construct_non_string_authority_rejects_as_address_error(self):
        full_folio = A.parse(f"sha256::{FULL}").folio
        for bad in [123, None, b"host.com", ["host.com"]]:
            with pytest.raises(A.AddressError):
                A.construct(type="web", authority=bad, folio=full_folio)

    @pytest.mark.parametrize("bad_digest", [None, 123, b"a" * 64, ("a",) * 64])
    def test_construct_non_string_folio_digest_rejects_as_address_error(self, bad_digest):
        # A Hash built by a caller can carry a non-string digest; construct must
        # turn it into AddressError on both the folio and fragment paths.
        with pytest.raises(A.AddressError):
            A.construct(type="hash", folio=A.Hash("sha256", bad_digest))
        with pytest.raises(A.AddressError):
            A.construct(type="alias", alias="speakbot",
                        folio=A.parse(f"sha256::{FULL}").folio,
                        fragment=A.Hash("sha256", bad_digest))


# ===========================================================================
# Public dataclasses are correct-by-construction (Hash / ParsedAddress)
# ===========================================================================
class TestDataclassInvariants:
    """Hash and ParsedAddress are public, directly-constructible frozen
    dataclasses. Their documented invariants (is_full/is_short exclusivity,
    well-formed digest, alias/authority set-iff) must hold for ANY instance —
    not only those produced by parse()/construct() — so resolve() and the
    derived properties can trust them. (Hardening Phase 4, knuth/oracle.)"""

    @pytest.mark.parametrize(
        "algo, digest",
        [
            ("sha256", "Z" * 64),        # 64 chars but NON-hex (is_full must not lie)
            ("sha256", "a" * 70),        # over full width
            ("sha256", "a" * 7),         # below short minimum
            ("sha256", "A" * 64),        # uppercase hex
            ("blake3", "a" * 64),        # unsupported algorithm
            ("sha256", None),            # non-string digest
            ("sha256", 123),             # non-string digest
        ],
    )
    def test_malformed_hash_rejects_at_construction(self, algo, digest):
        with pytest.raises(A.AddressError):
            A.Hash(algo, digest)

    @pytest.mark.parametrize("n", [8, 16, 32, 63, 64])
    def test_wellformed_hash_constructs_and_flags_exactly_one(self, n):
        h = A.Hash("sha256", "a" * n)
        assert h.is_full ^ h.is_short          # exactly one is always true

    def test_parsed_address_alias_set_iff_alias_type(self):
        full = A.parse(f"sha256::{FULL}").folio
        # type=="alias" with no alias, and a non-alias type carrying an alias,
        # both violate the set-iff invariant.
        with pytest.raises(A.AddressError):
            A.ParsedAddress(type="alias", folio=full, alias=None)
        with pytest.raises(A.AddressError):
            A.ParsedAddress(type="hash", folio=full, alias="speakbot")

    def test_parsed_address_authority_set_iff_web_type(self):
        full = A.parse(f"sha256::{FULL}").folio
        with pytest.raises(A.AddressError):
            A.ParsedAddress(type="web", folio=full, authority=None)
        with pytest.raises(A.AddressError):
            A.ParsedAddress(type="hash", folio=full, authority="mesh.alice.com")

    def test_non_string_components_reject_without_repr_bomb(self):
        # A mistaken non-string component must raise AddressError, and the error
        # message must not call repr() on the offending value (so an object whose
        # __repr__ raises cannot turn the AddressError into an unrelated
        # exception). (Hardening Phase 4, gremlin round 3.)
        class Boom:
            def __repr__(self):
                raise RuntimeError("repr must not be called")
        full = A.parse(f"sha256::{FULL}").folio
        with pytest.raises(A.AddressError):
            A.Hash("sha256", Boom())
        with pytest.raises(A.AddressError):
            A.ParsedAddress(type=Boom(), folio=full)
        with pytest.raises(A.AddressError):
            A.ParsedAddress(type="alias", folio=full, alias=Boom())
        with pytest.raises(A.AddressError):
            A.ParsedAddress(type="web", folio=full, authority=Boom())

    def test_folio_fullness_checked_intrinsically_not_via_property(self):
        # The hash-folio / fragment fullness invariant is checked by digest
        # length, not the is_full property, so a Hash subclass that overrides
        # is_full to lie cannot smuggle a short digest into a hash-type folio,
        # and one that overrides it to raise cannot turn the check into a
        # non-AddressError. (Hardening Phase 4, gremlin round 4.)
        class LieHash(A.Hash):
            @property
            def is_full(self):
                return True

        class BombHash(A.Hash):
            @property
            def is_full(self):
                raise RuntimeError("is_full must not be consulted")

        full = A.parse(f"sha256::{FULL}").folio
        with pytest.raises(A.AddressError):              # short digest, lie caught
            A.ParsedAddress(type="hash", folio=LieHash("sha256", "a" * 8))
        with pytest.raises(A.AddressError):              # short fragment, lie caught
            A.ParsedAddress(type="hash", folio=full,
                            fragment=LieHash("sha256", "a" * 8))
        # A full-digest subclass is genuinely valid and constructs without ever
        # consulting (and tripping) the overridden property.
        assert A.ParsedAddress(type="hash", folio=BombHash("sha256", FULL)).is_bare_cascade
        assert A.ParsedAddress(type="hash", folio=full,
                               fragment=BombHash("sha256", FULL)).fragment is not None

    def test_parsed_address_unknown_type_rejects(self):
        full = A.parse(f"sha256::{FULL}").folio
        with pytest.raises(A.AddressError):
            A.ParsedAddress(type="gopher", folio=full)

    def test_wellformed_parsed_address_constructs(self):
        full = A.parse(f"sha256::{FULL}").folio
        assert A.ParsedAddress(type="hash", folio=full).is_bare_cascade
        assert A.ParsedAddress(type="alias", folio=full,
                               alias="speakbot").has_station_context

    def test_parsed_address_bare_hash_folio_must_be_full(self):
        # A hash-type folio has no station context, so a short hash is an
        # impossible state for resolve() — reject at construction, not just at
        # parse. (Hardening Phase 4, gpt-5.5 round 3.)
        short = A.parse(addr_alias(digest=SHORT)).folio
        assert short.is_short
        with pytest.raises(A.AddressError):
            A.ParsedAddress(type="hash", folio=short)

    def test_parsed_address_fragment_must_be_full(self):
        # The verifier fragment is "always a full hash"; a short fragment must
        # reject at construction. (Hardening Phase 4, gpt-5.5 round 3.)
        full = A.parse(f"sha256::{FULL}").folio
        short = A.parse(addr_alias(digest=SHORT)).folio
        with pytest.raises(A.AddressError):
            A.ParsedAddress(type="hash", folio=full, fragment=short)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"type": "alias", "alias": "Bad Name"},      # invalid alias grammar
            {"type": "alias", "alias": "speak%41bot"},   # percent in alias
            {"type": "web", "authority": "999.999.999.999"},  # IPv4-shaped invalid
            {"type": "web", "authority": "münchen.de"},  # non-ASCII authority
        ],
    )
    def test_parsed_address_rejects_invalid_component_values(self, kwargs):
        # __post_init__ enforces the alias/authority grammars, not just presence,
        # so the dataclass cannot hold an ungrammatical value. (Hardening Phase 4,
        # gpt-5.5 round 3.)
        full = A.parse(f"sha256::{FULL}").folio
        with pytest.raises(A.AddressError):
            A.ParsedAddress(folio=full, **kwargs)


# ===========================================================================
# Property tests (Hypothesis)
# ===========================================================================
@pytest.mark.skipif(st is None, reason="hypothesis not installed")
class TestProperties:

    @staticmethod
    def _hex64():
        return st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)

    @staticmethod
    def _alias_name():
        # Matches the pinned grammar incl. 1-char and leading-digit names.
        return st.from_regex(
            r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?", fullmatch=True
        ).filter(lambda n: not A.is_reserved_alias(n))

    @staticmethod
    def _ipv6_inner():
        # ALWAYS contains `::` zero-compression, so the opacity property reliably
        # exercises the motivating case (a `::` inside brackets must not split).
        group = st.from_regex(r"[0-9a-f]{1,4}", fullmatch=True)
        return st.tuples(
            st.lists(group, max_size=4), st.lists(group, max_size=4)
        ).map(lambda lr: ":".join(lr[0]) + "::" + ":".join(lr[1]))

    @settings(max_examples=300)
    @given(digest=_hex64.__func__())
    def test_full_hash_always_parses(self, digest):
        p = A.parse(f"sha256::{digest}")
        assert p.folio.digest == digest and p.folio.is_full

    @settings(max_examples=300)
    @given(name=_alias_name.__func__(), digest=_hex64.__func__())
    def test_parse_construct_round_trip(self, name, digest):
        p = A.parse(f"alias::{name}::sha256::{digest}")
        rebuilt = A.construct(type="alias", alias=name, folio=p.folio)
        assert A.parse(rebuilt) == p

    @settings(max_examples=300)
    @given(digest=_hex64.__func__())
    def test_tokenize_matches_expected_segments(self, digest):
        # Determinism AND correctness (the old self-vs-self compare was vacuous).
        s = f"web::mesh.alice.com::sha256::{digest}"
        t = A.tokenize(s)
        assert t == A.tokenize(s)
        assert t.segments == ("web", "mesh.alice.com", "sha256", digest)
        assert t.fragment is None

    @settings(max_examples=300)
    @given(inner=_ipv6_inner.__func__())
    def test_ipv6_bracket_never_splits(self, inner):
        # A bracketed literal — including `::` zero-compression inside — always
        # tokenizes to exactly one authority segment (opacity holds). And when
        # the inner is genuinely valid IPv6, parse() must also keep it intact
        # (opacity holds end-to-end, not just at tokenize).
        literal = "[" + inner + "]"
        addr = f"web::{literal}::sha256::{FULL}"
        assert A.tokenize(addr).segments == ("web", literal, "sha256", FULL)
        try:
            ipaddress.IPv6Address(inner)
        except ValueError:
            return  # invalid IPv6 — tokenization claim already verified above
        assert A.parse(addr).authority == literal

    @settings(max_examples=300)
    @given(
        bad=st.text(min_size=1, max_size=80).filter(
            lambda s: "::" not in s and not s.startswith("sha256:")
        )
    )
    def test_garbage_without_delimiter_never_validates(self, bad):
        assert A.validate(bad) is False

    @settings(max_examples=200)
    @given(
        parts=st.lists(
            st.from_regex(r"[a-z0-9]{1,12}", fullmatch=True), min_size=3, max_size=3
        ).filter(lambda p: p[1] != "sha256")
    )
    def test_three_segment_garbage_never_validates(self, parts):
        # A 3-segment address CAN be valid — `<alias|hash>::sha256::<hex>`. Every
        # legal 3-segment form has "sha256" as its MIDDLE segment, so excluding
        # that makes "never validates" unconditionally true (the earlier
        # conditional version was caught by gpt-5.5 + kimmi as fragile).
        assert A.validate("::".join(parts)) is False
