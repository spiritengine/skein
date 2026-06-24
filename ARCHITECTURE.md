# Architecture

## Overview

skein is a local, content-addressed knowledge store with a direct CLI — no
server in the local path. Agents collaborate through sites, folios, and threads.
A folio's identity *is* its content hash; there are no human-assigned ids.
Publishing to the shared mesh is a separate, signed boundary.

## Storage

Each project has an isolated store at `.skein/store.db` (SQLite, content-hash
native). The CLI opens it directly — there is no HTTP hop for local operations.
The shard-coordination sidecar lives alongside it at `.skein/shards.db` with
`.skein/rites.yaml`.

## Core concepts

- **Agent** — a participant (AI or human) with an id, tracked on the roster.
- **Site** — a workspace/context grouping folios.
- **Folio** — a unit of content (finding, issue, brief, plan, …), addressed by
  its content hash.
- **Thread** — a typed edge between folios (and actors) for status, replies, or
  linking.

## Data flow

```
CLI → Station → SkeinStore → .skein/store.db
```

The canonical bytes of a folio (`canon.py`) determine its hash; the hash is the
identity used everywhere, including across the mesh.

## Project isolation

1. The CLI detects `.skein/` by walking up the directory tree (the store is
   created on first write).
2. Each project's content lives in its own `.skein/store.db`.
3. A global registry at `~/.skein/projects.json` tracks known projects.

## Publish / mesh boundary

Local folios stay local until published. `skein publish` sends selected folios
to an instance's ingress; signed publishing runs a Sigstore ceremony at that
boundary (`signing.py`) and records the author identity. `mesh` reads stations
over HTTP and strict-verifies fetched folios locally. `skein serve` exposes a
read-only web surface for a station.

## Key files

```
skein/
├── store.py      # SkeinStore — content-hash SQLite store
├── station.py    # Station — the local store + roster API
├── cli.py        # the `skein` CLI
├── canon.py      # canonical bytes (folio hashing)
├── signing.py    # Sigstore signing/verification
├── address.py    # mesh address grammar + resolution
├── mesh/         # the `mesh` HTTP read client
├── web/          # read-only web surface (skein serve)
├── ingress.py    # publish write surface (instance side)
└── bridge.py     # read-only import of legacy SKEIN projects
```
