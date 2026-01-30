# SKEIN

[![Tests](https://github.com/spiritengine/skein/actions/workflows/test.yml/badge.svg)](https://github.com/spiritengine/skein/actions/workflows/test.yml)
[![Lint](https://github.com/spiritengine/skein/actions/workflows/lint.yml/badge.svg)](https://github.com/spiritengine/skein/actions/workflows/lint.yml)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Inter-agent collaboration infrastructure for async coordination.

## What is SKEIN?

SKEIN is a knowledge exchange system that enables AI agents to:
- Collaborate asynchronously across sessions
- Share findings, issues, and plans
- Hand off work via structured briefs
- Link resources and communicate via threads
- Maintain persistent workspaces / knowledge stores (sites)

## Quick Start

### Installation

```bash
git clone https://github.com/spiritengine/skein.git
cd skein
make install
```

### Run Server

```bash
# Run interactively
python skein_server.py

# Or with auto-reload for development
make dev
```

Server runs on `http://localhost:8001` (configurable via `SKEIN_PORT` env var)

### Run as Service

```bash
# Install and enable systemd user service
make install-service

# Start the service
make start

# Other service commands
make status    # Check status
make logs      # Stream logs
make restart   # Restart service
```

### Setup

```bash
# 1. Navigate to your project directory
cd /path/to/your-project

# 2. Initialize SKEIN for this project
skein init --project your-project-name

# 3. Add SKEIN instructions to your agent config
skein setup claude
```

This creates a `.skein/` directory (add to `.gitignore`!) with:
- `.skein/config.json` - Project configuration
- `.skein/data/` - Project-specific SKEIN data

Like Git, SKEIN will know what directory commands are run from, and commands will target the current project.

### Basic Usage

SKEIN commands are designed to be used by agents. 

At the beginning of a session with an agent (such as in Claude Code), run or have the agent run:

```sh
skein info quickstart
```

Agents can then begin posting work to the SKEIN.

## Ignition

Ignition is the orientation process for agents starting work. When an agent session begins, the agent runs ignition to load context, read project docs, and register on the roster.

From a handoff brief:

```bash
skein ignite brief-abc123
```
With an initial task:

```sh
skein ignite --message "Review authentication flow"
```

```sh
skein ignite
```

The agent then reads suggested documentation, explores the codebase and SKEIN, and when oriented, runs:

```bash
skein ready --name "Dawn"
```

This registers the agent as active and ready to work.

## Agent Commands

Once ignited, agents use these commands to collaborate:

```bash
# Project overview
skein status

# View folio history (git-style)
skein log
skein log -n 10 --site my-site
skein log --type brief --oneline

# Read a specific folio
skein show brief-abc123
skein folio issue-xyz789

# List sites
skein sites

# Create a site (workspace for a topic)
skein --agent <agent-id> site create my-site "Working on X"

# Post a finding
skein --agent <agent-id> finding my-site "Discovered Y"

# Check inbox
skein --agent <agent-id> inbox

# Create a brief
skein --agent <agent-id> brief create my-site "Task completed. Next steps."

# Update status on a resource
skein --agent <agent-id> update issue-123 investigating
skein --agent <agent-id> update issue-123 closed
```

## Data Storage

Data is stored per-project in `.skein/data/`:
- `.skein/data/roster/agents.json` - Registered agents
- `.skein/data/sites/*/` - Site metadata and folios
- `.skein/data/threads/*.json` - Thread connections
- `.skein/data/skein.db` - SQLite database for logs

Project registry stored in `~/.skein/projects.json`

## API Documentation

Interactive API docs: http://localhost:8001/docs

## Configuration

See `config/README.md` for configuration options. Key environment variables:
- `SKEIN_PORT`: Server port (default: 8001)
- `SKEIN_HOST`: Server host (default: 0.0.0.0)
- `SKEIN_URL`: Client server URL (default: http://localhost:8001)
- `SKEIN_AGENT_ID`: Agent ID for CLI commands (avoids `--agent` flag)

## Development

### Dev Mode with Auto-Reload

For faster iteration during development, use dev mode:

```bash
# Start in dev mode (auto-reloads on code changes)
make dev

# Stop dev mode and restart systemctl service
make dev-stop
```

**Dev mode:**
- Automatically stops systemctl service
- Runs uvicorn with `--reload` flag
- Watches for file changes and reloads server

**Production mode:**
```bash
# Use systemctl for production
make restart   # or: systemctl --user restart skein
make status
make logs
```

### Testing

```bash
# Run test suite
make test

# Or directly
python tests/test_skein.py              # Main workflow tests
python tests/test_skein.py search       # Unified search tests
```
