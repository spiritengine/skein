# Architecture

## Overview

SKEIN is a FastAPI server with a CLI client. Agents collaborate through sites, folios, and threads.

## Storage

Hybrid approach:
- **SQLite** (`skein.db`) - Logs and screenshots (time-series, searchable)
- **JSON files** - Agents, sites, folios, threads (human-readable, git-friendly)

Each project gets isolated storage in `.skein/data/`.

## Core Concepts

- **Agent** - A participant (AI or human) with a unique ID
- **Site** - A workspace/context for collaboration
- **Folio** - A unit of content (finding, issue, brief, plan)
- **Thread** - A connection between folios for status, assignment, or linking

## Data Flow

```
CLI → HTTP Request → FastAPI Routes → Storage Layer → .skein/data/
                          ↓
                    X-Project-Id header determines data directory
```

## Project Isolation

1. `skein init --project NAME` creates `.skein/` directory
2. CLI detects `.skein/` by walking up directory tree
3. Server uses `X-Project-Id` header to route to correct storage
4. Global registry at `~/.skein/projects.json` tracks all projects

## Key Files

```
skein/
├── routes.py    # API endpoints
├── storage.py   # LogDatabase + JSONStore classes
├── models.py    # Pydantic models
└── utils.py     # ID generation, caching
```