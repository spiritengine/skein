# Changelog

## [0.3.0] - 2026-06-07

### Added
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

### Fixed
- Canonical timestamp parsing now handles sub-6-digit fractional seconds identically on Python 3.10
- `verify_envelope` hardened against malformed envelopes; `as_of` field flattened

### Changed
- `.crypto` dependency renamed to `.cryptography`
- CI lint step now gates on `ruff check` (style errors) rather than `ruff format` (formatting)

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