# Changelog

## Unreleased

### Added
- `moment` folios for deliberately marking an event or weighty observation
  intended for public sharing. They post, list, search, read, and close like
  other folios; publication itself remains a separate operation.
- macOS launchd support: `skein-server --print-plist` renders a launchd user
  agent plist for this install (ProgramArguments resolved like the systemd
  unit's ExecStart). The rendered content is pinned by tests on Linux via
  plistlib; loading under launchd is exercised on a real Mac, not by this
  repo's suite.

### Changed
- The packaged systemd unit's config guidance now points at
  `<SKEIN_HOME>/server.json` as the way to move the service address — an
  `Environment=` line reaches only the service process, so the old comment's
  suggestion stranded the CLI on the previous address. (Socket activation was
  evaluated and closed do-not-implement: notion-20260723-477x.)

### Fixed
- Station invite tokens can no longer begin with `-`, which Click parsed as an
  option on the positional `invite revoke` / `redeem-invite` verbs, making
  ~1.6% of minted invites fail with "No such option". Tokens minted before the
  guard stay usable after a `--` separator (documented on both verbs).

## [0.3.0] - 2026-07-24

### Added
- The wheel now installs a complete working workbench: `skein-server` ships as
  a console script (the service no longer needs a source checkout), the systemd
  user unit ships inside the package and renders for the current install via
  `skein-server --print-unit`, and the documentation behind `skein info
  quickstart|guide|implementation` is packaged.
- `skein doctor`: checks the install, SKEIN home, project registry, service,
  CLI/service version agreement, packaged docs, and current project; exits
  non-zero on real breakage. Hardened to never crash — malformed or inaccessible
  registries, homes, configs, and docs all produce failing checks, and
  unprobeable state is never reported as healthy.
- Per-station onboarding routing: a stationfile `onboarding` key selects whether
  `GET /onboarding` serves the collaborator ceremony (the default; existing
  stations unchanged) or redirects to a named public site. Malformed values are
  hard errors at startup.
- First-class named site publishing: `skein publish --site` declares a
  workbench site as a public station site with a stable anchor and slug claim.
- `skein_next` — new content-hash station with a verified wire envelope, canonical serialization, and domain-separated profile signing
- Agent-facing web read surface: folio lineage layer, self-orienting agent markdown, and "For your agent" handoff box
- Stationfile-driven theming with `classic` and `ulm` themes shipped out of the box
- Mesh access layer: `POST /resolve` batch endpoint, mesh fetch CLI, browse verbs, and a client-side MCP wrapper
- Search L1: full-text and machine search endpoints plus `/.well-known/describe`
- `skein_next` tests run in CI (slices 1–5)
- Top-level `--project` flag on every CLI command (overrides cwd `.skein/` discovery)
- `project:site` colon syntax on `skein post` (issue/brief/friction/notion/finding/summary) and `skein playbook create`
- Documented `SKEIN_PROJECT` env var (already worked, was undocumented)
- Cross-project precedence: colon-syntax > `--project` flag > `SKEIN_PROJECT` env > cwd `.skein/`
- `playbook` and `tender` folio types
- `skein edit` command for updating folio fields in place
- Unified `skein find` command for folio discovery across types
- `--confidence` and `--status` flags on `skein shard tender`
- QM triage fields on `skein survey`
- Request ID tracking on all API responses

### Changed
- The CLI's URL resolution now bottoms out on the machine's service address
  (the same `SKEIN_HOST`/`SKEIN_PORT`/`server.json` ladder `skein-server`
  binds), so moving the service moves every `skein` command with it. Before,
  the two resolved independently and `SKEIN_PORT=8123 skein-server` stranded
  the CLI on 8001.
- A project or global `server_url` holding exactly the literal `skein init`
  used to write (`http://localhost:8001` / `http://127.0.0.1:8001`) is read as
  absent; a deliberately different value is honored. Configs are not rewritten.
- `skein init` no longer writes `server_url` into project configs — a project
  config is shared across machines, and one machine's address does not belong
  in it.
- `skein doctor` reports which rung the CLI's URL came from, and warns about
  values resolution had to ignore (an unparseable `SKEIN_PORT`, an unusable
  config file). An unparseable `SKEIN_PORT` never crashes CLI commands;
  `skein-server` itself still refuses to start on one unless `--port` is given.
- `.crypto` dependency renamed to `.cryptography`
- CI lint step now gates on `ruff check` (style errors) rather than `ruff format` (formatting)

### Fixed
- Canonical timestamp parsing now handles sub-6-digit fractional seconds identically on Python 3.10
- `verify_envelope` hardened against malformed envelopes; `as_of` field flattened

## [0.2.0] - 2024-11-20

Initial open source release.

### Added
- Multi-project support with `.skein/` directories
- Project-specific storage isolation
- Configurable server (SKEIN_PORT, SKEIN_HOST env vars)
- CLI auto-detection of project config
- Unified search API
- Brief handoff system
- Thread-based status and assignment

### Changed
- Storage now requires project initialization (`skein init`)
- Logs and screenshots use project-specific databases

## [0.1.0] - 2024-11-06

Initial internal release.

### Added
- Core SKEIN server and CLI
- Sites, folios, findings, issues, briefs
- Agent roster management
- Thread connections between folios
- SQLite logs and JSON artifact storage
