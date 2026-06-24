# Port Stage 4 — Cutover spec

Retire the legacy client/server SKEIN and make the content-hash station the
canonical `skein`. Stages 1–3 ported the verbs onto `skein_next` additively
(legacy untouched); Stage 4 deletes legacy and renames `skein_next` → `skein`.

This is the spec the design doc (`AGENT_COORDINATION_PORT_DESIGN.md` §3) deferred
as "own spec." It exists because Stage 4 is the one stage that can lose data and
break install/deploy if done as a blind rename. Read the data-safety section
first — it is the spine; the rename is mechanical around it.

Naming throughout follows `docs/NAMING.md` (interskein = domain + web station +
PyPI name + mesh-protocol concept; skein = everything internal incl. the CLI).

---

## 0. The data-safety spine (READ FIRST)

**The new store is empty; the live data is all in legacy.** Measured 2026-06-23:

- `.skein/data/skein.db` (legacy, live) — **714 folios**, modified today. This is
  the actual working SKEIN: every brief, finding, summary, site. The legacy
  `skein` CLI is still the daily driver and writes here.
- `.skein-next/store.db` (new content-hash store) — **0 folios**, last touched
  Jun 16. The new system has only ever run against per-test temp stores; the repo
  copy is empty.

So the new system holds **none** of the real data. The design doc's line —
"legacy `.skein/data` moves aside or is cleared (already archived)" — is **wrong
and unsafe as written**: it treats the live source of truth as disposable. It is
only true *after* a fresh import has carried the data into the new store and that
import has been fidelity-verified.

**Hard rule for this stage: nothing is deleted or cleared until the import has
run against the live legacy DB and the fidelity gate passes. Backups first, never
`rm` — only move-aside.** The whole cutover is reversible up to the point legacy
code is deleted, and that deletion happens last, after the data is provably in the
new store.

The bridge already exists and is read-only on legacy (`skein_next/bridge.py`,
"never writes the legacy database"); the import + fidelity gate are wired as
`interskein import` (`skein_next/cli.py:966`, with a fidelity assertion flag). The
work here is to *run* it at cutover and verify, not to build it.

---

## 1. Phases (data-safe ordering)

Each phase is independently revertible. Do NOT reorder — the data import gates the
destructive steps.

### 4a — Backup (no code change)
- `tar czf ~/skein-cutover-backup-<date>.tgz .skein .skein-next` (the whole of
  both data dirs, including legacy `data/`, `shards.db`, `rites.yaml`,
  `config.json`, `_workspace/`).
- Note `git rev-parse HEAD` for code rollback.
- Confirm `~/.skein/` (global registry) is backed up too — see §6 open question.

### 4b — Import live legacy → new store, fidelity-verified (no rename yet)
- Run the bridge against the LIVE legacy DB into a fresh new-store:
  `interskein import --legacy-db .skein/data/skein.db --assert-fidelity` (confirm
  exact flags against `cli.py` import command before running).
- Gate: the fidelity report must show all 714 folios carried (or every shortfall
  explained by a known bridge open-call — re-hash misses, dangling cross-project
  refs — and signed off). Exit non-zero ⇒ stop, do not proceed.
- This produces the populated `store.db` that becomes `.skein/store.db` in 4d.
- Still fully reversible: legacy is untouched, nothing renamed.

### 4c — Code rename sweep (§2) + entry points (§3)
- `skein_next` → `skein`, `SKEIN_NEXT_*` → `SKEIN_*`, `.skein-next` → `.skein`,
  CLI `interskein` → `skein`, internal "new-skein"/"next" prose removed.
- Leave interskein-as-external untouched (§2 "stays" list).
- This is where the legacy `skein/`, `client/`, `skein_server.py` get deleted —
  do this in 4c only because the new package can't be named `skein` while legacy
  occupies the name. Legacy is backed up at smythp/skein-legacy + the 4a tarball.

### 4d — Data-dir collision resolution (§4)
- Place the imported `store.db` at `.skein/store.db`.
- Keep `.skein/shards.db` and `.skein/rites.yaml` (already used by the ported
  shard code — `shard.py:65`, `shard_cli.py:80`).
- Move legacy-only artifacts aside (never delete): `.skein/data/`,
  `.skein/config.json`, `.skein/_workspace/` → `.skein/legacy-archive-<date>/`.

### 4e — Verify + multi-genotype fell
- Full suite green under the renamed package.
- `skein` CLI round-trips against `.skein/store.db` with the 714 folios present
  (spot-check known ids: the Stage 2/3 summaries, recent briefs).
- Shard suite still finds `.skein/shards.db` + `.skein/rites.yaml`.
- Fell the diff (rename diffs are huge but mechanical — a reviewer checks the
  "stays interskein" exclusions held and no canonical/signed bytes moved).

### 4f — Infra/deploy cutover (separate, gated on §6 decision)
- `compose.yaml`, `Dockerfile`, `deploy/nginx/ingress.interskein.com.conf`, CI.
  These carry the interskein.com identity — they change only per the §6 deploy
  decision, not by the code-rename sweep.

---

## 2. The rename sweep — what moves, what stays

Token counts (2026-06-23, whole tree):

- `skein_next` — 253  → **skein** (import package)
- `SKEIN_NEXT` — 73   → **SKEIN** (env vars; e.g. `SKEIN_NEXT_DATA_DIR` →
  `SKEIN_DATA_DIR`, `SKEIN_NEXT_AGENT` → `SKEIN_AGENT`)
- `.skein-next` — 130 → **.skein** (data dir; `DEFAULT_DATA_DIR`, store.py:83)
- `interskein` — 215 total, of which:
  - `interskein.com` — 46  → **STAYS** (domain; nginx, compose, Dockerfile, and
    as instance-URL fixtures in tests/canon/ingress — those are the published
    station identity, leave verbatim)
  - bare `interskein` as the CLI command / usage examples / "new-skein" prose
    (~169) → **skein**, EXCEPT:
    - `pyproject.toml` `name = "interskein"` — **STAYS** (PyPI dist name)
    - prose naming the protocol/mesh/web-station/domain — **STAYS**

Because the same word `interskein` is both "stays" (domain/PyPI/protocol) and
"becomes skein" (CLI command), a blanket `sed` is unsafe. Sweep per file-class:

- `skein_next` / `SKEIN_NEXT` / `.skein-next` → mechanical global rename (no
  external-namespace collision; safe to script with verification).
- `interskein` → reviewed by hand against the "stays" list. Most are CLI examples
  in docstrings/tests/README (`interskein ready --agent X` → `skein ready …`).

## 3. Entry points

Both live in the one `pyproject.toml` (no separate legacy package):

- `skein = "client.cli:main"` — **delete** (legacy CLI).
- `interskein = "skein_next.cli:main"` → `skein = "skein.cli:main"`.
- `mesh = "skein_next.mesh.cli:main"` → `mesh = "skein.mesh.cli:main"` (the `mesh`
  command name is unaffected; only its module path moves).
- `name = "interskein"` — **stays** (per §2 / NAMING.md).

## 4. Data-dir collision (concrete; never `rm`)

`.skein/` today holds a mix of legacy artifacts and already-migrated shard state.
After the rename, the new store's default dir is also `.skein/`. Resolution:

| path                       | origin                         | action                       |
|----------------------------|--------------------------------|------------------------------|
| `.skein/store.db`          | imported new store (4b)        | place here (new)             |
| `.skein/shards.db`         | Stage 1 shard sidecar (live)   | KEEP — already used          |
| `.skein/rites.yaml`        | shard `test` config (live)     | KEEP — already used          |
| `.skein/data/`             | legacy server data (imported)  | move → `legacy-archive-<d>/` |
| `.skein/config.json`       | legacy config                  | move → `legacy-archive-<d>/` |
| `.skein/_workspace/`       | legacy workspace               | move → `legacy-archive-<d>/` |
| `.skein/*_backup_*`        | prior backups                  | leave in place               |

No file is deleted in this stage. `.skein/data/` is *archived, not cleared* — and
only after 4b proved its contents are in `.skein/store.db`.

(Plain-text restatement for screen reader: place imported store at .skein/store.db
new. Keep .skein/shards.db. Keep .skein/rites.yaml. Move .skein/data to a dated
legacy-archive folder. Move .skein/config.json to the same archive. Move
.skein/_workspace to the same archive. Leave existing backup files alone. Delete
nothing.)

## 5. Signing / canon — verified safe

Canonical hash fields are type/title/content/created_at/created_by only
(`skein_next/canon.py`); `skein_next` appears there only in a module docstring,
never in hashed bytes. The "25 refs in a signing test" are test fixture strings,
not canonical material. Renaming the package does NOT change any content hash or
signature. Confirm post-rename by re-running the signing/canon conformance tests
(`test_canon_conformance.py`, `test_sign.py`) — they must pass byte-identical.

## 6. Open decisions (Patrick's call)

1. **Global `~/.skein/` + other projects.** `~/.skein/` holds a cross-project
   registry (`projects.json`), a global store, and backups; other projects
   (speakbot, …) each have their own `.skein/data/`. Is Stage 4 scoped to THIS
   repo's cutover only, with the global registry + other-project migration a
   separate Stage 5? (Recommend yes — keep Stage 4 to the skein repo; do not
   touch other projects' live data in the same stage.)

2. **interskein.com deploy timing (4f).** Is there a live web station to cut over,
   and does it change at all? Per NAMING.md the domain/web-station stay
   interskein — so 4f may be a near-no-op (just module paths inside the image),
   or it may need a coordinated redeploy. Need: is anything currently served at
   interskein.com that this rename's container/module paths would break?

3. **One shard or staged.** 4a–4f as one big shard, or land 4b (import+verify) and
   4c–4d (rename+collision) as separate felled shards? Recommend separate: the
   data import deserves its own fell and sign-off before any rename touches disk.

4. **`.skein/data/` retention.** Archive-aside is the plan (§4). Confirm it should
   be retained indefinitely (recommend: keep until the new `.skein/store.db` has
   been the daily driver for some agreed soak period, then you decide).
