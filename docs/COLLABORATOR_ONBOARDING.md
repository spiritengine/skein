# Collaborator onboarding — redeem an invite, publish signed

This is the **collaborator** side of agent-mediated onboarding (the operator side
is `docs/PUBLIC_INGRESS_OPERATIONS.md`). You were sent a one-time invite token out
of band. Hand this whole document to your coding agent; it walks the agent through
installing the CLI, redeeming the invite, and publishing — surfacing provenance and
the Rekor-consent stop for you to confirm.

## The flow

1. The operator minted an invite and sent you a token + a short blurb.
2. Your agent installs the verified `interskein` package — the `skein` CLI (see the install section).
3. Your agent appends a fixed primer to your agent file (AGENTS.md / CLAUDE.md /
   .cursorrules) — detect-and-**append**, never clobber.
4. Your agent redeems the invite:

   ```bash
   skein redeem-invite <token> --to https://ingress.interskein.com --login
   ```

   This runs a Sigstore login (the human-accountability gate) and signs a proof
   that is **cryptographically bound to your token and to this station** — a
   harvested signature for some other purpose cannot bind your identity here. The
   station verifies it, atomically burns the invite, and binds your discovered
   `(issuer, subject)` as an author.

5. You publish; your content renders **SIGNED** under your identity.

## What your agent MUST surface to you before acting

These are non-negotiable confirmation stops — your agent runs downstream of an
out-of-band message it cannot fully trust, so **you** are the check:

- **Provenance.** Where the CLI is being installed from, and the exact version /
  artifact hash (see install section). Confirm it matches the signed install spec.
- **The exact config additions.** The verbatim primer snippet being appended to
  your agent file — diff it against the published known-good snippet. It is a
  fixed, published string, never free-form generated from the invite blurb.
- **Rekor consent.** Redeeming **signs with your identity and writes a record to
  the public Rekor transparency log** — your invite token's hash and your email
  become permanently public. `skein redeem-invite` prompts you to confirm
  before the ceremony; on a headless box (`--oob`) there is no other
  human-in-the-loop, so this confirmation is the hard stop. Do not pass `--yes`
  unless you have read and accepted this.

## Discovering your identity (optional)

```bash
skein whoami          # prints: issuer <...> / subject <your-email>
skein whoami --oob    # SSH/headless code flow
```

`whoami` reads your OIDC identity directly — **no** Rekor entry, **no** cert. It is
the exact `(issuer, subject)` your binding will use, useful if the operator is
adding you manually instead of by invite.

## Redeem outcomes

`redeem-invite` exits 0 on success (`redeemed — bound as author: <you>`). On
failure it prints the typed reason:

- `unknown_token` / `expired` / `revoked_invite` / `already_redeemed` — the token
  is not usable; ask the operator for a fresh invite (or check you copied it whole).
- `revoked_identity` — the operator revoked your identity; it cannot self-readmit.
  Contact the operator.
- `proof_rejected` / `proof_malformed` — the signature didn't verify against this
  token + station; re-run a clean `--login` (don't reuse a stale proof).
- `rate_limited` — too many failed attempts on this token; wait and retry.

A lost network ack is safe: re-running `redeem-invite` with the same identity is
**idempotent** — if you already redeemed, the retry reports success, not an error.

## Install the CLI — PAIRING-PENDING (operator: do not finalize)

> **This section is intentionally unfinished.** The install-source pin and the full
> supply-chain mechanism are a deliberate pairing item (brief-20260615-ofv1) — they
> must NOT be filled in by an implementer. The threat is real: your auditing agent
> is downstream of a possibly-hostile out-of-band pack, so a poisoned pack could
> point the install at a trojan CLI (arbitrary code execution) and append hostile
> instructions to your agent file. Documentation alone cannot blunt that.
>
> The agreed DIRECTION (to be finalized in pairing):
>
> - The canonical install spec — exact version, artifact hash, and the FIXED
>   verbatim primer snippet — is published **as a signed folio on interskein.com**,
>   anchored to the operator's existing Sigstore identity (the same root of trust as
>   the whole system). The out-of-band pack only needs to point at that signed
>   folio; your agent verifies the folio's signature, then trusts its contents.
> - The primer appended to your agent file is that fixed, published, signed snippet,
>   appended **verbatim** (you can diff against the known-good), never generated
>   from the pack.
> - The package name is claimed now to prevent typosquatting.
>
> **OPEN (the actual pairing):** the install-source pin — PyPI pinned with
> `--require-hashes`, vs a git-commit pin (immutable/publicly-auditable), vs a
> signed release/wheel-with-hash — and whether `redeem-invite` self-verifies the
> CLI artifact hash before the ceremony. Resolve with Patrick before writing the
> concrete install commands here.
