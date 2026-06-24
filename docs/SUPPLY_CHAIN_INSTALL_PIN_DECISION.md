# Supply-chain install pin — decision package (DRAFT, Patrick-gated)

Status: DRAFT for Patrick to pick. Nothing here is finalized or published. No code
changed. Resolves the PAIRING-PENDING install section of
`docs/COLLABORATOR_ONBOARDING.md` (brief-20260619-ws9c, parent brief-20260619-nrle).

This is the v3 package. v1 and v2 were each reviewed by two independent genotypes
(Opus + Codex), which converged on real holes — fixed across the revisions. The
convergent-review record is section 8.

Read order: section 0 (the reframe), section 3 (the bootstrap — the load-bearing
part, rewritten), the recommendation (section 2), then the drafted drop-in text
(sections 5-6).

---

## 0. The reframe, and the trap v1 fell into

The system has a single root of trust: the operator's Sigstore identity — the same
identity that signs every folio, manifest, and redeem proof. The canonical install
spec — version, artifact hashes, git commit, fixed primer — is anchored to that
identity, and the hash in the spec is the real pin (the distribution channel is
just a CDN; a post-signing channel compromise yields a hash mismatch and the
install aborts).

> **The operator cert issuer — CONFIRMED `https://accounts.google.com`.** The verify
> commands below pin `(--cert-identity, --cert-oidc-issuer)`: subject
> `patricksmyth01@gmail.com`, issuer `https://accounts.google.com`. This was
> confirmed empirically on 2026-06-20 by decoding real operator certs (cert issuer
> extensions OID 1.3.6.1.4.1.57264.1.8 and .1.1 both read `https://accounts.google.com`)
> and by a live accepted signed publish binding under it.
>
> > Trap, documented so no one repeats it: `interskein whoami` prints
> > `https://oauth2.sigstore.dev/auth` — that is the OIDC **token** issuer (the
> > Sigstore Dex broker the login federates through). It is NOT the cert issuer and
> > NOT what the binding uses. Fulcio stamps the **upstream** issuer (Google) into
> > the cert; `can_write` keys on the cert. An earlier change (issue-20260619-oufc)
> > wrongly aligned things to the broker on the strength of `whoami`; it was reverted.
> > Always read the binding issuer off a real cert, never off `whoami`.

That much is true and survives review. **But it only defends against a poisoned
out-of-band pack — not against a compromised `interskein.com`, which is explicitly
in the threat model.** v1's mistake was treating "verify the spec" as an optional
upgrade over a floor that trusts the server's rendered `SIGNED` badge. The server's
render is not a signature check — `skein_next/mesh/client.py` says display routes
"do NOT verify." So a compromised server can render a forged spec (attacker's wheel
hashes + hostile primer) as `SIGNED`, and `pip --require-hashes` then faithfully
installs the trojan, because the hash it matches against came from the forged spec.
The hash pin contributes zero there.

So the real decision splits into two independent questions, and the second is where
the security lives:

- **Q1 (channel):** what does the signed hash point at — PyPI / git / wheel? (Low
  stakes; the hash is the pin. Settled in section 1-2.)
- **Q2 (bootstrap):** how does the agent obtain an AUTHENTIC spec, fail-closed,
  before it has the CLI, against a compromised server? (High stakes. Section 3.)

---

## 1. The three channel options (Q1)

The signed spec is the anchor in all three; they differ on collaborator friction,
source auditability, and operator infra.

### Option A — PyPI, pinned with `--require-hashes` (wheels only)

Install: `pip install --require-hashes --only-binary=:all: -r interskein-pinned.txt`,
the pinned file published (and directly signed — section 3) by the operator.

- Trust root: the signed hash → the wheel bytes. PyPI is a CDN; a PyPI/index
  compromise yields a hash mismatch and aborts.
- Auditability: wheel + sdist are public on PyPI. The wheel is a built artifact;
  the spec names the git commit, but **that commit is advisory-audit only** — it
  does NOT bind the wheel bytes to the source unless a build-provenance attestation
  (PEP 740 / a reproducible build) ties wheel↔commit. (v1 wrongly said this
  "absorbs" Option B; corrected.)
- Friction: LOWEST. One pip command; no toolchain. `--only-binary=:all:` avoids
  sdist build-backend execution (use wheels for install; keep the sdist hash in the
  spec as an audit artifact, not the install path).
- `--require-hashes` forces the WHOLE transitive tree to be hashed — operator work
  at release time (`uv pip compile --generate-hashes`), not collaborator work.

### Option B — git-commit pin

Install: `pip install "interskein @ git+https://github.com/spiritengine/skein@<40-hex-commit>"`.

- Trust root: the signed commit hash (immutable; commits to the whole tree).
- Auditability: HIGHEST, and genuinely stronger on ONE axis the trojan-CLI threat
  cares about: it binds the RUN bytes to the audited source — you execute what you
  read. Option A does not give this without a provenance attestation.
- Friction: MEDIUM-HIGH. Needs git + a build toolchain; does NOT compose with
  `--require-hashes` for the dep tree (deps fall back to unpinned unless a hashed
  constraints file is also shipped); slower.

### Option C — directly signed wheel / blob

Install: fetch a wheel, verify the operator's direct signature over its bytes,
`pip install ./interskein-X.whl` + a hashed constraints file.

- v1 dismissed this as "gains nothing over the signed spec." **That was wrong for
  the bootstrap** (section 3): direct signing over RAW bytes is the only thing that
  gives a CLI-free verifier. Folded into the recommendation, not as the install
  channel but as the bootstrap-verification mechanism.

---

## 2. Recommendation (channel)

**Primary: Option A — PyPI, `--require-hashes`, wheels only, anchored to the signed
spec; claim `interskein` on PyPI now.** Lowest friction for an agent consumer, and
the hash pin defeats a post-signing channel compromise.

With two corrections forced by review:

- The recorded git commit is **advisory-audit only** unless you add a
  build-provenance attestation binding the wheel to the commit. Either add that
  (PEP 740 attestation via Trusted Publishing, or a reproducible-build hash), or
  state plainly in the doc that the commit lets you read the source but does not
  constrain the binary you run. If you want run-bytes-bound-to-source, that is
  **Option B**, offered as a first-class audit alternative line in the doc — not
  "absorbed."
- The security does not come from the channel. It comes from section 3.

### The name

`skein` is taken on PyPI (200); `interskein` is free (404) and matches the CLI,
brand, and domain. Publish as `interskein`, claim now to block typosquatting. (Note
on dependency confusion: the load-bearing defense against a same-name package from
a rogue index is `--require-hashes`, not the name claim — the name claim only stops
user-typo squatting. Pin the index too: `--index-url https://pypi.org/simple` and
no extra indexes.) `pyproject.toml` says `name = "skein"` today; the published
distribution name must be `interskein` (a one-line packaging change at publish
time, not a behavior change).

---

## 3. The bootstrap (Q2) — rewritten, this is the load-bearing section

Goal: the agent obtains an authentic install spec and authentic primer, fail-closed,
before it has the CLI, even if `interskein.com` is fully compromised. Five
requirements, each closing a hole review found:

### 3.0 The packet can be platform-generated — the SIGNATURE is the protection, not the packet's provenance

Practically, the platform generates the invite packet (the "send to a friend"
material); telling the operator to assemble it through some out-of-platform CLI
dance is circuitous and won't happen. That is FINE, and does not put the platform
in the trusted set, for one reason: the install facts the friend relies on are in a
blob SIGNED by the operator's interactive Sigstore identity, and that login does
not live on the box. A fully compromised platform can write anything into the
packet but cannot forge that signature.

So enumerate what a hacked platform can do: hand out a dead token (redeem fails),
point at a missing blob (fails), or point at an OLD genuinely-signed blob
(rollback). It CANNOT point at an attacker-signed blob. Two natural checks close the
rest:

- **The human confirms whose signature it is.** The friend already knows the
  operator — they know the email. So "this blob is signed by
  `patricksmyth01@gmail.com` / `https://accounts.google.com`" is checked against what the
  friend already knows, NOT taken from the packet. The agent rejects any other
  signer. This is the load-bearing human stop (same weight as the Rekor-consent
  stop) and the onboarding text must say "look at the email / id" explicitly (§6).
- **Version + expiry close rollback.** The signed blob carries a version and an
  expiry; the agent refuses anything older than the named current version, or
  expired. (§3.4.)

Split the packet to keep this clean: the TOKEN is the only freshly-minted,
per-invite item, and a poisoned token carries no install power (it just fails to
redeem). The install facts (identity, wheel hash) are NOT per-invite — identity is
a constant, the hash is per-release — so they ride the signed blob, never the
freshly-generated blurb. The verifier + its trust root likewise come from a
hash-pinned package (3.3), not the packet. Minting therefore needs no signing
(cheap); only the per-release blob is signed (one interactive login per release).

### 3.1 Local authorship verification is MANDATORY, display-trust is not a tier

For the install spec specifically, the server's rendered `SIGNED` badge is NOT
acceptable. The agent must verify the operator's signature itself, against the
cross-checked operator `(issuer, subject)` (3.0). The floor (display-trust) is a
convenience for human browsing only and must be called that — never the install
path. (v1 had this inverted.)

### 3.2 The verifier must work WITHOUT the CLI — so sign the RAW blob directly

The signed-FOLIO spec on interskein.com is signed over skein's domain-separated
`profiled_preimage(profile, canonical_bytes)` (`sign.py`). A generic
`sigstore`/`cosign` cannot reconstruct that preimage without skein's `canon` +
`profile` modules — i.e. the CLI you are trying to bootstrap. Worse, a generic
verifier that trusts the bundle's EMBEDDED `canonical_bytes` is exactly the lazy
path `verify_wire_folio` forbids: a compromised server can harvest a genuine
operator-signed bundle from the public read surface, staple it to a trojan spec
body, and that generic check returns VERIFIED. The circularity v1 claimed to
dissolve comes right back.

Fix: the operator **also directly Sigstore-signs the RAW bytes** of the two
bootstrap-critical files — the pinned-requirements file and the primer file — so
the bytes verified ARE the bytes used, with no skein code:

```bash
# operator, at release time. Signs the literal file bytes under the operator
# identity; emits interskein-pinned.txt.sigstore.json alongside each file. Sign with
# bundles that embed the Rekor inclusion proof + SCT so the agent can verify OFFLINE
# (so a network attacker who blocks Rekor cannot force a soft-downgrade — see 3.3).
python -m sigstore sign interskein-pinned.txt
python -m sigstore sign interskein-primer.txt

# collaborator agent, CLI-free, fail-closed. https://accounts.google.com is the confirmed
# cert issuer (CONFIRMED https://accounts.google.com — see section 0; NOT the broker
# that whoami prints). --offline uses the trust root embedded in the
# hash-pinned verifier (3.3) + the bundle's inclusion proof, so no fresh fetch.
python -m sigstore verify identity \
  --cert-identity     patricksmyth01@gmail.com \
  --cert-oidc-issuer  https://accounts.google.com \
  --offline \
  interskein-pinned.txt
python -m sigstore verify identity \
  --cert-identity     patricksmyth01@gmail.com \
  --cert-oidc-issuer  https://accounts.google.com \
  --offline \
  interskein-primer.txt
```

`verify identity` consumes `<file>.sigstore.json` (or pass `--bundle`), and verifies
the digest of the FILE supplied — so a harvested skein folio bundle (signed over a
different domain-separated preimage and different bytes) cannot verify against these
raw files, and the `(--cert-identity, --cert-oidc-issuer)` pin to the cross-checked
operator (3.0) rejects any other signer. The human-discoverable signed-folio on
interskein.com still exists for browsing and for post-install `interskein`/`mesh
fetch` re-verification, but the LOAD-BEARING bootstrap artifacts are these
directly-signed raw files plus their `.sigstore.json` bundles. (This is the Option-C
direct-signing v1 wrongly dismissed.)

### 3.3 Pin the verifier itself, and fail closed on verifier-unavailable

- `pip install sigstore` is itself an unpinned supply-chain step run BEFORE trust
  exists, and it is NOT small (pulls cryptography, pydantic, a real tree). Pin it,
  wheels-only: ship a tiny `sigstore-pinned.txt` (with hashes) and
  `pip install --require-hashes --only-binary=:all: -r sigstore-pinned.txt`, or
  prefer a pre-vetted system `cosign`. Name `sigstore`/`cosign` as an explicit
  additional bootstrap root in the doc. (Per 3.0 the hash for this file is
  cross-checkable the same way as the operator identity, not a blind pack input.)
- The TUF/Rekor trust root must not be a fetch-time variable an attacker can block.
  A network attacker who blocks Rekor/TUF can otherwise downgrade Sigstore
  verification to integrity-only (`OFFLINE_NO_TRUSTED_ROOT` / `TRUST_ROOT_STALE` →
  `_VERIFIER_UNAVAILABLE` → a soft "unverified" that still binds integrity), and a
  forged spec is internally hash-consistent so it passes. Two parts: (1) the install
  bootstrap **hard-fails on anything short of full VERIFIED** — and the raw
  `sigstore verify identity` CLI already does this (it has no integrity-only soft
  path, unlike skein's mesh client); (2) rather than shipping a TUF root via the
  pack, rely on the production trust root EMBEDDED in the hash-pinned verifier
  package (3.3 bullet 1) and verify `--offline` against bundles that carry the
  inclusion proof (3.2). That removes the fresh-fetch the attacker would block AND
  removes the "pack defines the trust root" surface a poisoned pack would abuse.

### 3.4 Anti-rollback — the OOB pack must pin freshness, not just authenticity

A signature authenticates bytes, not recency. A compromised server (or a stale OOB
pointer) can replay an OLD but genuinely operator-signed spec naming a vulnerable
CLI version or a worse primer; it verifies clean. Close it:

- The OOB pack names the **SHA256 content-hash of the raw `interskein-pinned.txt`
  itself** (and of `interskein-primer.txt`), and the agent compares the file's hash
  — not a version string parsed out of the file. A content-hash pin is strictly
  harder than a parsed `interskein==X.Y.Z` compare (which only holds if the operator
  never re-signs a version number over different content), and the raw file is a
  blob you already control, so it costs nothing.
- The signed files / spec carry `version`, `not_before` / `not_after`, and a
  `supersedes` link as a secondary freshness signal; the agent rejects anything
  whose hash ≠ the OOB-named hash, or whose version < the OOB-named version.

### 3.5 Net bootstrap chain (fail-closed)

OOB pack (the human channel; "possibly hostile" — cross-checked per 3.0) conveys:
the freshness pointer = exact version + the **content-hash of the raw signed files**,
and a REMINDER of the operator `(issuer, subject)` for the human to confirm against
the independent Patrick-controlled channel (3.0). The verifier package + its
embedded trust root come from a hash-pinned install, not the pack (3.3). Agent:
install + (cross-checked) hash-verify the verifier → `sigstore verify identity
--offline` the raw pinned-requirements and primer against the cross-checked operator
identity, hard-fail on anything < VERIFIED (3.1-3.3) → confirm each file's SHA256 ==
the OOB-named hash and version ≥ OOB-named (3.4) → `pip install --require-hashes
--only-binary=:all:` → append the verified primer verbatim. The interskein.com
server is never in the trusted set; the only roots are the hash-pinned verifier and
the cross-checked operator identity.

---

## 4. (b) Self-verify in `redeem-invite` — DROP for the artifact; primer is the real question

DROP self-verify of the CLI's OWN artifact. By the time `redeem-invite` runs the
agent is already running the CLI; a trojan CLI just reports "verified." A program
cannot establish its own integrity from inside — so this buys **zero security gain
against a malicious CLI** (precise wording; it CAN catch honest version drift, which
is why an optional, clearly-non-security `interskein verify-install` advisory
command is fine, just not in the ceremony path). Artifact integrity is established
before execution by the install-time hash pin (3) — by pip, not the CLI.

But the threat has two halves, and review surfaced a non-circular gap on the second:
the **primer**. A GENUINE CLI (integrity already established by the pre-execution
pin) verifying OTHER inputs is not circular. The doc's plan to append the primer
"outside the CLI" and have the human "diff against the published snippet" is hollow
if BOTH the appended primer and the snippet being diffed against come from the same
compromised server — both sides of the diff are attacker-controlled. So:

- Bootstrap: the primer is directly-signed and verified CLI-free (3.2) before
  append. The human diff must be against the OOB-anchored, signature-verified
  snippet, never a second server render.
- Post-install (optional, non-circular): the genuine CLI can re-fetch the
  strict-verified spec and diff the installed primer — a real check, since the CLI's
  own integrity is no longer the question. Nice-to-have, not a launch blocker.

Keeping artifact self-verify out of `redeem-invite` means this deliverable needs no
write-boundary code change and no fell.

---

## 5. What is Patrick-gated (the picks)

1. Channel: Option A (PyPI, `--require-hashes`, wheels-only) primary, Option B as a
   first-class audit alternative line. Decide whether to add a wheel↔commit
   provenance attestation (PEP 740) or to state the commit is audit-only.
2. Claim `interskein` on PyPI (Patrick's account).
3. Self-verify: NO for the artifact (drop). Optional `verify-install` advisory and
   the post-install primer re-diff are nice-to-haves, not blockers.
4. Adopt the section-3 bootstrap: directly-sign the raw pinned-requirements + primer
   files (in addition to the signed folio), make local verify mandatory + fail
   closed + `--offline`, install the verifier hash-pinned, pin the OOB freshness
   pointer to the raw files' content-hashes, and confirm the operator identity is
   cross-checkable (3.0).
5. Confirm the operator cert issuer (section 0 MUST-CONFIRM) during the
   real-Sigstore pass, and fill `https://accounts.google.com` everywhere.

Then: produce + directly-sign the bootstrap files and the signed-folio spec via the
REAL operator identity, and paste the section-6 install block into
COLLABORATOR_ONBOARDING.md.

---

## 6. DRAFT install-section text for COLLABORATOR_ONBOARDING.md

Replaces the PAIRING-PENDING block (~lines 73-101). ALL-CAPS placeholders filled at
finalize time. The flow is fail-closed and never trusts the interskein.com server.

> ## Install the CLI
>
> Your agent is downstream of an invite packet it cannot fully trust, and the
> interskein.com server is treated as potentially hostile. So your agent verifies
> the operator's signature ITSELF before installing — it never trusts the website's
> "SIGNED" badge for the install. The packet carries the freshness pointer: the
> exact CLI version and the SHA256 content-hash of the signed install files.
>
> > **LOOK AT THE EMAIL / ID — the one thing only you can do.** A hacked platform
> > can forge the packet but cannot forge the operator's signature. So before your
> > agent installs anything, confirm the signature on the install files is the person
> > who invited you: the subject must be `patricksmyth01@gmail.com` and the issuer
> > `https://accounts.google.com` — checked against what you already know of the operator,
> > NOT against what the packet says. Any other identity: STOP. This is a hard human
> > stop, same as the Rekor-consent stop above.
>
> ### 1. Get a pinned verifier (do not skip — this is a trust root too)
>
> The verifier is a trust root, so its hash must come from an independent
> Patrick-controlled channel — the same TLS-fetched onboarding doc / GitHub repo
> that carries the operator identity — NOT from the out-of-band pack alone. Confirm
> `sigstore-pinned.txt` matches that known-good hash, then:
>
> ```bash
> pip install --require-hashes --only-binary=:all: \
>   --index-url https://pypi.org/simple -r sigstore-pinned.txt
> # (or use a pre-vetted system `cosign`)
> ```
>
> ### 2. Verify the operator's direct signature over the raw install files
>
> ```bash
> # fail-closed: any result short of VERIFIED, or a non-matching identity, STOP.
> # --offline verifies against the pinned verifier's embedded trust root + the
> # bundle's inclusion proof, so a blocked Rekor cannot soft-downgrade the check.
> python -m sigstore verify identity \
>   --cert-identity patricksmyth01@gmail.com \
>   --cert-oidc-issuer https://accounts.google.com \
>   --offline interskein-pinned.txt
> python -m sigstore verify identity \
>   --cert-identity patricksmyth01@gmail.com \
>   --cert-oidc-issuer https://accounts.google.com \
>   --offline interskein-primer.txt
>
> # then confirm the files' content-hashes equal the out-of-band-named hashes
> # (guards against an old-but-signed file being replayed):
> sha256sum interskein-pinned.txt interskein-primer.txt
> ```
>
> ### 3. Install, wheels only, pinned by hash
>
> ```bash
> pip install --require-hashes --only-binary=:all: \
>   --index-url https://pypi.org/simple -r interskein-pinned.txt
> ```
>
> `--require-hashes` makes pip refuse any byte that does not match the
> operator-signed hashes; a poisoned index fails closed.
>
> ### 3b. Alternative: build from the public commit (audit-only)
>
> To read every line before running it, and bind the `interskein` source/build
> input you run to the commit you read (the dependency tree is not hash-pinned this
> way unless you also pass a hashed constraints file; needs git + a build toolchain):
>
> ```bash
> pip install "interskein @ git+https://github.com/spiritengine/skein@COMMIT_SHA"
> ```
>
> ### 4. Confirm the version
>
> ```bash
> interskein --version    # must equal the verified version
> ```
>
> ### 5. The primer
>
> Append the verified `interskein-primer.txt` VERBATIM to your agent file
> (AGENTS.md / CLAUDE.md / .cursorrules) — detect and append, never clobber, never
> generate from the invite blurb. Diff what you append against the
> signature-verified file from step 2, never against a fresh page from the website.

---

## 7. DRAFT signed install-spec folio body

The human-discoverable spec, sign-published on interskein.com (verified natively by
`interskein`/`mesh fetch` AFTER install). The bootstrap-critical files
(`interskein-pinned.txt`, `interskein-primer.txt`) are ALSO directly Sigstore-signed
as raw blobs (section 3.2) and shipped/pointed-to in the OOB pack.

> # interskein CLI — canonical install spec
>
> Authoritative source of truth for installing `interskein`. Verify the operator's
> signature against `(issuer https://accounts.google.com, subject patricksmyth01@gmail.com)`
> — confirmed independently, see section 0 — before trusting anything below; do not
> rely on this page's rendered badge.
>
> - Package (PyPI): `interskein`
> - Version: `X.Y.Z`   |   not_before: `...`   not_after: `...`   supersedes: `...`
> - Git commit: `<40-hex>` at `https://github.com/spiritengine/skein`
>   (audit-only unless a wheel↔commit provenance attestation is published)
> - Wheel SHA256: `sha256:...`   |   Sdist SHA256 (audit artifact): `sha256:...`
>
> ## Pinned requirements (install with `pip install --require-hashes --only-binary=:all: -r`)
>
> ```
> interskein==X.Y.Z --hash=sha256:<wheel>
> <each transitive dependency, one per line, --hash=sha256:...>
> ```
>
> ## Fixed primer snippet (append verbatim)
>
> ```
> <the frozen primer string — authored and locked here; never generated>
> ```
>
> Any source, hash, or primer not in the signed files is not canonical.

---

## 8. Convergent-review record (Opus + Codex, two rounds, 2026-06-19)

Two independent genotypes adversarially reviewed each version; on each round they
CONVERGED on the same core flaws (not invented — both found them independently).

Round 1 (against v1):
- BLOCKING (both): display-trust floor defeats the in-scope compromised-server
  attacker. → §3.1 local verify mandatory, floor demoted to convenience.
- HIGH (both): generic sigstore/cosign cannot verify skein's domain-separated
  preimage without the CLI; trusting embedded bytes is the forbidden lazy path
  (harvested-bundle attack). → §3.2 directly sign the RAW blobs; revive Option C.
- HIGH (both): rollback to an old-but-signed spec verifies clean. → §3.4.
- HIGH (Opus): verifier-unavailable (Rekor/TUF blocked) soft-downgrades to
  integrity-only; forged spec passes. → §3.3.
- MEDIUM (both): "commit absorbs Option B" overclaimed. → §1A/§2 corrected.
- MEDIUM (both): `pip install sigstore` is an unpinned, not-small step. → §3.3.
- MEDIUM (Codex): sdist enables build-time execution. → wheels-only `--only-binary`.
- MEDIUM/LOW (both): self-verify "zero gain" → "zero gain AGAINST A MALICIOUS CLI";
  the non-circular primer check is the real second half. → §4.
- LOW (both): dependency confusion + transitive-dep trust-on-first-use. → §2.

Round 2 (against v2) — both confirmed the v1 fixes genuinely closed and the
architecture sound; converged on:
- HIGH (both): the verify command pinned `--cert-oidc-issuer
  https://accounts.google.com`, but `sign.py:42` signs via the Sigstore Dex broker
  (`https://oauth2.sigstore.dev/auth`) and records that issuer — so pinning Google
  would REJECT the genuine artifact (denial-of-bootstrap). The flag usage is right;
  the constant was wrong. → §0 MUST-CONFIRM + `https://accounts.google.com` placeholder
  everywhere + §9 flag.
- BLOCKING (Codex): the OOB pack must not be the sole source of the operator
  identity / verifier pins (the pack is "possibly hostile"). → new §3.0:
  cross-checked constant; pack carries only the freshness pointer.
- MEDIUM (both): TUF root should not be shipped via the pack; use the trust root
  embedded in the hash-pinned verifier + `--offline` bundles with inclusion proofs.
  → §3.2/§3.3 rewritten.
- MEDIUM (Opus): verifier install (`sigstore-pinned.txt`) wasn't wheels-only. →
  §3.3/§6 add `--only-binary=:all:`.
- MEDIUM (both): rollback defense was a parsed-version compare; pin the raw file's
  content-hash instead. → §3.4/§3.5/§6.
- LOW (both): state the `.sigstore.json` bundle location; keep the raw-signed files
  and the folio byte-identical. → §3.2 + §9.

Round-2 bottom line from both: architecture correct and v1 findings genuinely
addressed; with the issuer constant and the command gaps fixed (done in v3), the
drop-in text is safe to paste, not merely safe to adopt in principle.

## 9. Finalize-time open items (not blockers)

- RESOLVED: operator cert issuer = `https://accounts.google.com`, confirmed
  empirically in the 2026-06-20 live deploy (decoded real certs + an accepted signed
  publish). There was no issuer bug: the original `accounts.google.com` config was
  correct. issue-20260619-oufc — which "aligned" defaults/docs to the broker on the
  strength of `whoami` (the token issuer) — was a misdiagnosis and was reverted
  (shard 174d65aa + fell), restoring `accounts.google.com` and adding a correct
  regression test. The deploy is live: `account init-operator` is bootstrapped under
  `accounts.google.com`; a real signed publish and a real redeem both succeeded.
- The frozen primer string is not yet authored. The COLLABORATOR_ONBOARDING.md flow
  already assumes a fixed, diffable primer (lines 36-38, 89-91).
- Generate the hashed transitive tree with `uv pip compile --generate-hashes`;
  confirm a reproducible wheel hash; ship a wheels-only `sigstore-pinned.txt` too.
- Default `sigstore sign` already waits for log inclusion and embeds the proof, so
  the agent can `verify identity --offline`; no extra sign flag. Ship each
  `<file>.sigstore.json` next to its raw file. Keep the primer PAYLOAD identical
  between the raw `interskein-primer.txt` and the primer inside the signed folio
  (the folio wraps it in structure, so the files aren't byte-identical) so the
  optional post-install re-diff (§4) compares the payload cleanly.
- The pinned-verifier hash freezes a snapshot of sigstore's embedded trust root;
  re-pin (and re-confirm the `--offline`/embedded-root behavior, which was
  validated against sigstore 4.2.0) whenever you bump the verifier, to track
  Sigstore key rotation.
- Decide on the wheel↔commit provenance attestation (PEP 740) vs. stating
  commit-is-audit-only.
- Set the published distribution name to `interskein` at publish time.
