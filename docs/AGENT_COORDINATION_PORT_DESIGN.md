# SKEIN agent-coordination port — design (v2, review-folded)

Porting the agent-coordination layer (lifecycle, roster, activity, shard, intranet) from
the legacy client-server system onto the new content-hash station (`skein_next`), so the
legacy can be retired and the new system becomes the canonical `skein`.

Grounded in three subsystem maps and pressure-tested by a 3-genotype design review
(Opus + GPT-5.5 + MiMo) that verified every load-bearing claim against the code and
converged on the spec gaps now folded in below (§7 review record). The core thesis —
this is a re-homing, not a rewrite — survived review intact.

## PRINCIPLE: full faithful port, no deferrals

This is a FULL port of a working system. The new system should behave the SAME — ~97%
of it carries over 1:1. Nothing is "deferred" and nothing is "dropped" for convenience.
Every verb and capability ports. The ONLY thing that needs a decision is a genuine
ARCHITECTURE CONFLICT — a place where the two systems collide and there's no faithful
1:1 mapping (e.g. `whoami` has two meanings; content-hash folios can't be edited in
place). Those conflicts are enumerated in §4; everything else is mechanical fidelity.
A capability whose MECHANISM changes (backup = copy one SQLite file; "active" = a folio
aggregate) still ports — the behavior is preserved, only the implementation differs.

## 0. The reframe (proven by the maps + review)

The legacy server held exactly ONE irreplaceable thing — a place for concurrent agents to
see each other (the roster) — and even that is reconstructed from folios: legacy already
computes "active" as a folio aggregate (`routes.py:300-316`, "posted a folio in the last
30 min"), not a heartbeat. Everything else is content-addressed data or a one-line query.
The new station already has every primitive: `Station.post`, `set_status`/`status_of`
(latest-typed-thread-wins, `station.py:216-259`), `save_thread`, the `slugs` table, and
the `account_bindings`/`invites` sidecar-table precedent.

## 1. Design decisions (made; review-corrected)

### D1 — Roster + lifecycle: type=agent folios + status-threads + a NEW name guard
Each agent is a `type=agent` folio in a reserved `roster` site; lifecycle
(orienting→active→retiring→retired) rides `status` threads (reuse `set_status`/`status_of`);
metadata lives in folio content + status-thread content. The folio insert is genuinely
idempotent (`INSERT OR IGNORE`, `store.py:483`), so concurrent identical registration is
safe.

Review-corrected, MUST build (not free reuse):
- **Name-uniqueness is NEW code.** `set_slug` is `ON CONFLICT(slug) DO UPDATE` —
  last-write-wins, silent rebind (`store.py:792-798`). It does NOT detect collisions. The
  port adds a guarded register: conditional insert + rowcount check; if the slug exists
  pointing at a DIFFERENT agent folio, **reject** (a second live agent must not silently
  steal an in-use name). [Patrick-gated policy: reject vs rebind — recommend reject; §4.]
- **Identity/join key — RESOLVED (2026-06-21, Patrick).** `whoami` stays the MESH
  (Sigstore) identity; agent-id is a SEPARATE, lightweight string (env-based, e.g.
  `SKEIN_AGENT`, which is already `folio.created_by`), rarely used. The join is direct: the
  agent folio carries its agent-id as `created_by`, and `activity` aggregates authored
  folios by that same string (exactly the legacy `routes.py:281` join). No `whoami`
  conflict; no longer a Stage-2 blocker. (Re-ignite still mints a new agent folio since
  `created_at` is in the hash; thread a `succession` edge for continuity if wanted —
  optional, low stakes.)
- Re-ignite linkage: optionally thread a `succession` edge to the prior agent folio (this
  is what the dead-code `ignite` did). Decide keep-or-drop; not a blocker.

### D2 — Shard: copy the engine, rewire ~6-8 CLI call sites, keep the sidecar DB
`skein/shard.py` (2343 lines, verified zero HTTP) is **copied** into the new package — NOT
moved — because legacy `client/cli.py` still imports it and must keep running until Stage 4.
`shards.db` stays a mutable local sidecar (worktrees are the source of truth; bookkeeping is
fast-changing — wrong fit for content-hashing). The server call sites are in the CLI layer
(`client/cli.py`), and there are ~6-8, not 4: `tender` (GET /sites + POST /folios),
`merge` (POST tender + POST close-thread), `spawn` (POST tag-thread), `triage` and
`inspect` (GET tender folios for the confidence overlay), `pause`/`resume` (find+reply on
the spawn thread). Each rewires to `Station.post`/`save_thread`/store queries.
Fix-in-passing: the `PROJECT_ROOT` AttributeError in `shard apply`/`test`
(`client/cli.py:7603,7652` reference a name `shard.py` doesn't export — both crash today).

### D3 — Tender metadata: pinned JSON envelope in content + latest-by-date selection
The new `folios` table has no metadata column (`store.py:147`), so a tender's structured
fields (worktree_name, branch, commits, confidence) go into folio CONTENT. Review-corrected:
- **Pin the embedding format.** A human-readable markdown body PLUS one fenced ```json
  block behind a stable marker (e.g. `<!-- tender-meta -->`), with a single shared
  parser+writer and round-trip tests. No ad-hoc regex over prose; hand-edits to the prose
  must not break extraction.
- **Fix the consumer.** Tender status (incomplete→complete) is a `status` thread; a changed
  confidence is a NEW tender folio (append-only). So `triage`/`inspect`, which today build
  `tender_map[worktree]` by overwriting in server-iteration order (`cli.py:6896-6912`),
  MUST select the LATEST tender per worktree by `created_at`. The producer change is
  incomplete without this.
(JSON-in-content beats a schema migration — confirmed by review — but only with both above.)

### D4 — Architecture conflicts (the only non-mechanical work; everything else ports 1:1)
- **`edit` vs immutability.** Legacy `edit` rewrites a folio body in place; content-hash
  folios cannot be mutated. Port `edit` as: mint a new folio + a `supersedes` thread to the
  prior (the new system's native versioning). Behavior shifts from in-place to versioned
  edit — confirm acceptable (§4), then apply the same pattern anywhere legacy mutates a
  folio body.
- **Chain `/yields` PORTS (no defer).** mill ACTIVELY sets `SKEIN_CHAIN_ID` on every chain
  task (verified, `speakbot/mill/wheel/executor.py:1377,1499`), so `complete`'s yield path
  is live and ports like everything else. The faithful home for the `sacks` (mutable chain
  state, not content-addressed) is a sidecar table mirroring legacy `sacks`
  (`storage.py:319-868`), same pattern as `account_bindings`. Port the full yield API.
- **Dead-code `ignite`** (`client/cli.py:1327`, succession behavior) is shadowed by the
  live `ignite_start` (`:4124`) and never runs — port the LIVE path; don't resurrect the
  dead one by accident (decide if its succession-thread behavior was intended).

### D5 — register / identify
`register` is not a verb to "port" — it's `Station.post(type=agent, site=roster)` +
the guarded name register (D1). `identify` likely reduces to setting the agent-identity
env value; folds into the §4 identity decision.

## 2. Verbs — everything ports (no drops)
The new system behaves the SAME. Most verbs already have a 1:1 home (folio, site(s), post,
close, status, thread, search, init). The rest port faithfully: ignite, ready, torch,
complete (WITH yield), register, identify, activity, the shard suite, writ, message, reply,
brief (+ ignite-from-brief), mantle, hypothesis, playbook, stats, backup/restore (= copy
the one SQLite file), rite/rites, tag, move, log, export, thread-tree, projects/
cross-project addressing (now cross-station), info, setup. `edit` ports versioned (D4).
The only verbs without a literal target are pure legacy-DAEMON artifacts (`health`/`logs`
as a running-server check) — port them as the local-station equivalent, don't drop the
capability. `whoami` keeps the mesh (Sigstore) meaning (§4, resolved).

## 3. Staging (each stage = its own shard + multi-genotype fell)
- **Stage 0:** prereqs resolved — `whoami` = mesh identity, agent-id = lightweight env
  string (§4). Stage 1 and Stage 2 are both unblocked.
- **Stage 1 — Shard suite.** Copy the engine (D2), rewire the ~6-8 CLI touchpoints, pin the
  tender JSON envelope + latest-by-date consumer (D3), fix PROJECT_ROOT (D5). Test:
  spawn→tender→triage→merge→cleanup round-trip on a local station, no server.
- **Stage 2 — Lifecycle + roster + activity + yields.** Agent folios + status-threads + the
  guarded name register (D1); lifecycle FSM guard; the full chain `/yields` → `sacks`
  sidecar so `complete` keeps working for mill chains (D4); activity as a folio aggregate.
  Test: ignite→ready→torch→complete, name-collision reject, activity feed, a chain
  `complete` storing + reading back a yield.
- **Stage 3 — Intranet + handoff.** message/reply threads; brief handoff + ignite-from-brief;
  mantle folios + `ignite --mantle`.
- **Stage 4 — Cutover (own spec; after 1-3 land).** SCOPE (review-measured): **~390 refs
  across ~60 files** (incl. 25 in a signing test, 19 in account tests). Module name `skein`
  is occupied by legacy until its deletion, so this stage deletes legacy `client/`+`skein/`+
  `skein_server.py` (backed up at smythp/skein-legacy) and renames `skein_next`→`skein`,
  `SKEIN_NEXT_*`→`SKEIN_*`, `.skein-next`→`.skein`, kills `new-skein`/`next`/`new` text.
  The `.skein/` data-dir collision needs a concrete migration: legacy `.skein/data` moves
  aside or is cleared (already archived); `.skein/shards.db` is already in the right place
  and must survive; handle `config.json`, `rites.yaml` (used by `shard test`), the legacy
  registry, and folio aliases. Backup + dry-run verify + rollback. Verify no canonical/
  signed string embeds `skein_next` (canonical fields are type/title/content/created_at/
  created_by, so it shouldn't — but a signing test holds 25 refs; confirm).

## 4. Decisions — all resolved; this is mechanical fidelity now

- **`edit` → versioned — CONFIRMED (Patrick, 2026-06-21):** in-place edit becomes a new
  folio + a `supersedes` thread (the native versioning); apply the same pattern wherever
  legacy mutates a folio body.
- **`whoami` — RESOLVED:** the mesh (Sigstore) identity; agent-id is a separate lightweight
  env string (rarely used). Stage 2 unblocked.
- **Chain `/yields` — PORTS in full** (sacks in a sidecar table); no defer.
- **Name uniqueness:** port legacy's reject-duplicate behavior (a new guarded insert over
  the slug table).
- **Execution vehicle:** a dedicated session (staged shards + fells).

No open Patrick-gated calls remain — the implementer can run the full port from this doc.

## 5. Risks / watch-items
- SQLite single-writer (`store.py:307`): the lifecycle FSM (`ready` reads status then
  writes) is a cross-process read-modify-write; idempotent + seconds apart so low-risk, but
  name it. Any roster write path uses the ingress lock discipline.
- Stage 4 `.skein/` collision (§3) — the day-one hazard of cutover; needs the migration spec.
- `whoami`/identity clash (§4) gates Stage 2; the dead-code `ignite`; the PROJECT_ROOT bug.

## 6. What survives untouched vs new machinery
Untouched: the whole shard git/worktree engine; post/thread/status primitives; the
activity aggregate shape (legacy already does it). New: the guarded agent-name register
(D1), the lifecycle FSM guard, the tender JSON envelope parser (D3), the `complete`
chain-guard (D4), and the Stage-4 migration tooling.

## 7. Review record (3 genotypes, 2026-06-21)
Opus + GPT-5.5 + MiMo independently verified the claims (shard.py server-free; no folio
metadata column; status=latest-thread-wins; dead `ignite`; PROJECT_ROOT bug — all REAL)
and converged on the spec gaps now folded in: slug uniqueness is new code not free reuse
(D1); the identity/join + whoami question (since RESOLVED: whoami=mesh identity, §4);
chain handling for `complete` (the reviewers said hard-fail; later corrected — mill wires
`SKEIN_CHAIN_ID` live, so yields PORTS in full, D4); tender needs a pinned JSON envelope
+ latest-by-date consumer (D3);
cutover is ~390 refs and shard.py is copied-not-moved (D2/Stage 4). All three verdicts:
core design sound, hand off after folding these in — done here. (A knuth/horizon pass was
attempted as a 4th voice but horizon's Max credential was expired; optional to add later.)
