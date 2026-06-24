# Stage 4 cutover — execution brief (rehearsal-proven)

This is the **how**, written for a fresh agent to execute in a watched session.
The **why** is in `STAGE_4_CUTOVER_SPEC.md`; the naming rule is in `NAMING.md`.
Read all three. The code half of this brief was REHEARSED end-to-end on the
`cutover-rehearsal` branch (full suite 932 green under the renamed package) — that
branch is the reference diff; `git diff master..cutover-rehearsal` shows exactly
what the code cutover does.

You are not "totally fresh" if you ignite from this. Everything load-bearing is
here, including the landmines that DON'T fail any test.

---

## 0. The one rule that matters most

**Nothing is deleted or overwritten on disk until the authoritative import has run
and been fidelity-verified. Back up first. Only ever move-aside, never `rm`, on
data.** The import bridge is read-only on legacy; that is the safety net. Code is
git-revertable; data is not. See the prior session's verified backup: all 12,356
folios across 43 stores are in `smythp/skein-legacy` (commit 5cd70f1), content-
verified (per-store id-set digests + integrity check).

## 1. State coming in (all done, merged to master local, NOT pushed)

- **Backup** — exhaustive, verified, pushed (step 0).
- **Dry-run** — the bridge imports all 43 stores clean; every folio carries; only
  `target_agent` is an intended drop (patbot/speakbot/warp). Re-runnable harness:
  `/tmp/skein_dryrun_sweep.py` (rebuild if gone — it loops every project in
  `~/.skein/projects.json` through `python -m skein --data-dir TMP import PROJ --verify`).
- **Metadata preservation** — the bridge folds legacy `metadata` into folio content
  (felled, merged). Lossless for dupe-keys/bytes/surrogates/trailing-newlines.
- **A0 self-contained crypto** — `skein_next` no longer imports legacy `skein` at
  runtime (felled, merged, proven by blocking legacy at the import system). This is
  what makes deleting legacy SAFE.

So both halves are de-risked: data migrates losslessly (proven), and the new
package runs without legacy (proven).

## 2. Decisions already made (do not re-litigate)

- **Naming** (`NAMING.md`): `interskein` stays ONLY for the domain `interskein.com`,
  the public web station, and the **PyPI distribution name** (`pyproject` `name =
  "interskein"`). EVERYTHING else → `skein`: the CLI command, the import package,
  env vars, the data dir, module names. `pip install interskein` gives the `skein`
  command and `import skein`.
- **Strategy**: big-bang in a quiesced window (Patrick's call), NOT gradual
  coexistence. Rehearse the bulk first (done), keep the live window short.
- **Scope**: THIS repo's cutover. The global `~/.skein/` registry and OTHER
  projects' data are a separate concern — see §8 open items; confirm with Patrick
  before touching anything outside this repo.
- **Dropped on import**: the dead `questions_enabled` flag (no-op) and `target_agent`
  (24 folios, generic handoff targets). Everything else preserved.

## 3. Human checkpoints (do NOT pass these autonomously)

Stop and get Patrick's explicit go before each:
- **C1** — before deleting legacy code (`client/`, `skein/`, `skein_server.py`,
  root `tests/`).
- **C2** — before the per-store data-dir collapse (`.skein-next/store.db` →
  `.skein/store.db`, archiving legacy `.skein/data/`).
- **C3** — before reinstalling/flipping the global `skein` command.

Everything up to C1 is reversible (git + read-only import). C1–C3 are the points of
no return; the backup is the floor under them.

---

## 4. The proven code cutover (from the rehearsal — apply in this order)

Do this on a branch off master, NOT in place. It is `git diff master..cutover-rehearsal`.

### 4.1 Delete legacy (checkpoint C1 first)
```
git rm -rq client skein skein_server.py tests
rm -rf client skein skein_server.py tests   # nuke untracked remnants (pycache) so
                                             # the 'skein' dir name frees up — WITHOUT
                                             # this, `git mv skein_next skein` NESTS
                                             # it as skein/skein_next/ (a real trap)
```
Root `tests/` is pure-legacy (imports `from skein`/`from client`, never `skein_next`)
— it tests the deleted code, so it goes too. ~46.7K lines deleted total.

### 4.2 Rename the package
```
git mv skein_next skein
# confirm: skein/cli.py exists (NOT skein/skein_next/cli.py)
```

### 4.3 Mechanical ref sweep (robust `find`, NOT a shell loop over $VAR — zsh
won't word-split it, and `grep -lZ | xargs -0` mis-split here)
```
find skein -type f -name '*.py' -exec sed -i \
  -e 's/skein_next/skein/g' \
  -e 's/SKEIN_NEXT/SKEIN/g' \
  -e 's/\.skein-next/.skein/g' {} +
```
This also fixes the A0 self-reference landmine automatically: `signing.py`'s
`_test_factory` monkeypatches `"skein_next.signing.X"` → becomes `"skein.signing.X"`,
which is correct for the renamed package. (If you DIDN'T do a global sweep you would
have to fix those 4 strings by hand — they don't fail until real-signing tests run.)

### 4.4 User-facing strings the test suite does NOT catch (THE landmine class)
Green suite ≠ done. None of these fail a test, but ship-blocking if missed:
```
find skein -type f -name '*.py' -exec sed -i \
  -e 's/prog_name="interskein"/prog_name="skein"/g' \
  -e 's/new-skein/skein/g' \
  -e 's/interskein \([a-z]\)/skein \1/g' \
  -e 's/`interskein`/`skein`/g' {} +
```
- `cli(prog_name="interskein")` (cli.py ~1383) → without this, `skein --help`
  prints `Usage: interskein`.
- `"new-skein: ..."` group docstring + `__init__.py`.
- `interskein <cmd>` help examples → `skein <cmd>`.

**Precision boundary — do NOT blanket-replace `interskein`:** there are ~18
`interskein.com` references (domain, MUST STAY) vs ~23 `interskein <command>` (→
`skein`). The regex `interskein \([a-z]\)` preserves `interskein.com` but MISSES:
- `interskein --opt` (followed by a dash, e.g. ingress.py docstring) — handle
  `interskein -` too.
- `interskein CLI` (followed by uppercase) — prose; judge case-by-case.
- Test fixtures using `"interskein"` as a station/project NAME (test_stationfile,
  test_mesh, test_web, test_cli_account) — those are arbitrary test data, LEAVE them.
After the sed, audit: `grep -rn interskein skein/ | grep -v interskein.com` and
eyeball each remaining hit.

### 4.5 pyproject.toml (name STAYS "interskein"; everything else points at skein)
- `[project.scripts]`: DELETE `skein = "client.cli:main"`; set `skein =
  "skein.cli:main"`, `mesh = "skein.mesh.cli:main"`. (Drop the `interskein` script
  name entirely — the command is `skein` now.)
- `[tool.setuptools.packages.find]` include: `["skein*", "client*"]` → `["skein*"]`.
- `[tool.setuptools.package-data]`: drop the legacy `skein = ["templates/*.md"]`
  line; the web assets line becomes `skein = ["web/templates/*.html", ...]`.
- `[tool.pytest.ini_options]` testpaths: `["tests", "skein_next/tests"]` →
  `["skein/tests"]`.
- `[tool.coverage.run]` source: `["skein", "client"]` → `["skein"]`.
- `[tool.ruff.lint.per-file-ignores]`: the `"tests/test_signing/..."` entry points
  at deleted root tests — remove it (stale, harmless if left).

### 4.6 Verify the code cutover
```
python -c "import skein, skein.signing, skein.cli, skein.mesh.client"   # imports as skein
python -m skein --help        # must say "Usage: skein" / "skein: the local ..."
python -m pytest skein/tests/ -q   # must be 932 passed (the baseline)
```
Then FELL the code diff (multi-genotype; it's mostly mechanical + the security-
sensitive crypto rename, so include a Codex/Kimi + the signing byte-check: production
crypto must still be byte-identical to the A0 copy modulo skein_next->skein).

### 4.7 Infra + docs (OUT of the package sed — handle separately)
Still contain `skein_next`/paths: `Dockerfile`, `compose.yaml`,
`.github/workflows/test.yml`, `deploy/nginx/ingress.interskein.com.conf`, `README.md`,
`docs/STATION_THEMING.md`.
- In these, `skein_next` paths → `skein`; the `interskein.com` domain, the nginx
  conf filename, and any `interskein` IMAGE/dist name STAY (NAMING.md).
- DON'T sed the migration docs (`NAMING.md`, `STAGE_4_CUTOVER_SPEC.md`,
  `AGENT_COORDINATION_PORT_DESIGN.md`, `SUPPLY_CHAIN_INSTALL_PIN_DECISION.md`, this
  file) — they intentionally discuss `skein_next` as history.

---

## 5. The data migration (runtime — the irreversible half)

The code rename changes the DEFAULT data dir to `.skein` (`store.py`
`DEFAULT_DATA_DIR = Path(".skein")`, `ENV_DATA_DIR = "SKEIN_DATA_DIR"`). So after
cutover the `skein` CLI reads each project's content store from `.skein/store.db`.
That store must exist and be current. Today it does NOT — `.skein-next/store.db` is
empty/stale; the live data is in legacy `.skein/data/skein.db`. So:

### 5.1 Quiesce (Patrick) — stop everything that writes skein
Shuttle sessions, mill chains, spindle spools, horizon casts, and any cron/systemd
(angelus digest, watchmen, reminders) that post folios. Readers don't matter; writers
do. One stray writer mid-import breaks the snapshot.

### 5.2 Authoritative import, per project (read-only on legacy)
For each project with live data (the 43 from the dry-run), run the bridge from the
live legacy DB into that project's NEW store:
```
cd <project>
python -m skein --data-dir .skein-next import . --verify   # (pre-rename binary)
# or post-rename: skein --data-dir .skein-next import . --verify
```
Gate: every project's fidelity report must pass (every folio carried; only
`target_agent` surfaced). This is the same thing the dry-run proved — now run for
real, against the frozen-by-quiesce data. Import writes to `.skein-next/store.db`;
legacy `.skein/data/` is untouched.

### 5.3 Data-dir collapse, per project (checkpoint C2 first; NEVER rm)
```
mv .skein-next/store.db .skein/store.db          # the new content store
# KEEP .skein/shards.db and .skein/rites.yaml — the shard suite already uses them
mkdir -p .skein/legacy-archive-<date>
mv .skein/data .skein/legacy-archive-<date>/data        # legacy server data, archived
mv .skein/config.json .skein/legacy-archive-<date>/     # legacy config
# leave existing *_backup_* files alone
```
Nothing deleted. `.skein/data/` is archived aside, and ONLY after 5.2 proved its
contents are in `.skein/store.db`.

### 5.4 Flip the command (checkpoint C3)
Reinstall so the global `skein` command is the new package: `pip install -e .`
(the `interskein` PyPI dist now provides `skein`). Verify `which skein`, `skein
--help` says skein, `skein sites`/`skein activity` work against the migrated
`.skein/store.db` in a real project.

---

## 6. Verification (after the window)

- `python -m pytest skein/tests/ -q` → 932.
- `skein --help` self-identifies as skein; `grep -rn interskein skein/ | grep -v
  interskein.com` is clean (or only intentional test fixtures).
- In 2-3 real projects (incl. speakbot, the 8845-folio one): `skein sites`,
  `skein activity`, read a known recent folio (e.g. this session's summaries),
  `skein shard triage` (confirms `.skein/shards.db` + `rites.yaml` survived).
- Confirm folio COUNT per migrated store matches the dry-run number.

## 7. Rollback

- Code: `git revert`/checkout — trivial, the rename is one diff.
- Command: reinstall the pre-cutover commit → `skein` is legacy again.
- Data: the legacy `.skein/data/` is archived (5.3), not deleted, and the off-machine
  backup (`smythp/skein-legacy`) is the floor. To roll a project back: restore its
  `.skein/data/` from the archive and reinstall legacy.
- Keep the archives and the soak until the new `skein` has been the daily driver for
  a period Patrick picks.

## 8. Open items / NOT done by this brief

- **Global `~/.skein/` + other projects.** The `skein` command is a global shim, so
  flipping it (C3) affects ALL ~39 projects at once. This brief migrates THIS repo;
  the full fleet migration (running 5.2/5.3 for every project) and the global
  `~/.skein/projects.json` registry are the actual big-bang scope. Confirm with
  Patrick whether the window does the whole fleet or this repo first.
- **Deploy.** Whether anything is currently served at `interskein.com` that the
  container/module-path changes (4.7) would disturb — Patrick to confirm.
- **`master` is local, not pushed.** None of the prior session's merges are pushed;
  decide push timing with Patrick.

## 9. Landmine quick-reference (the things green tests won't catch)

1. `rm -rf` the legacy dirs after `git rm`, or `git mv skein_next skein` nests it.
2. `cli(prog_name="interskein")` → `skein`, plus `new-skein` and `interskein <cmd>`
   help strings — zero test coverage.
3. `interskein.com` (18, STAY) vs `interskein <cmd>` (~23, →skein): never blanket-sed;
   the regex misses `interskein -` and `interskein CLI`; test "interskein" project-
   name fixtures stay.
4. pyproject: scripts, packages.find, package-data, testpaths, coverage.source, ruff
   per-file-ignores all need edits (name stays interskein).
5. Infra/docs are outside the package sed; migration docs must NOT be sed'd.
6. Data: `.skein-next/store.db` is empty — the live data is in legacy
   `.skein/data/`; you MUST import before the collapse or you lose everything.
7. Keep `.skein/shards.db` + `.skein/rites.yaml` through the collapse.
