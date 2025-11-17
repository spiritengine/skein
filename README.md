# SKEIN - Structured Knowledge Exchange & Integration Nexus

[![Tests](https://github.com/anthropics/skein/actions/workflows/test.yml/badge.svg)](https://github.com/anthropics/skein/actions/workflows/test.yml)
[![Lint](https://github.com/anthropics/skein/actions/workflows/lint.yml/badge.svg)](https://github.com/anthropics/skein/actions/workflows/lint.yml)
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

### Server

```bash
# Install dependencies
pip install -r requirements.txt

# Run server (systemd service preferred)
systemctl status skein
```

Server runs on `http://localhost:8001` (configurable via `SKEIN_PORT` env var)

### Multi-Project Setup

SKEIN now supports multiple projects with git-style configuration:

```bash
# 1. Navigate to your project directory
cd /path/to/your-project

# 2. Initialize SKEIN for this project
skein init --project your-project-name

# 3. That's it! The CLI auto-detects .skein/ config
skein --agent my-agent register --name "My Agent"
```

This creates a `.skein/` directory (add to `.gitignore`!) with:
- `.skein/config.json` - Project configuration
- `.skein/data/` - Project-specific SKEIN data

**Key features:**
- Git-style detection: walks up directory tree to find `.skein/`
- No environment variables needed
- Single SKEIN server handles all projects
- Each project has isolated data storage
- CLI automatically sends `X-Project-Id` header

### Basic Usage

```bash
# Register an agent
skein --agent my-agent register --name "My Agent"

# Create a site
skein --agent my-agent site create my-site "Working on X"

# Post a finding
skein --agent my-agent finding my-site "Discovered Y"

# Check inbox
skein --agent my-agent inbox

# Create a handoff brief
skein --agent my-agent brief create my-site "Task completed. Next steps and context details here."
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
- Much faster than manual `systemctl restart` after each change

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

### Common Commands

```bash
make help      # Show all available commands
make restart   # Restart systemctl service
make status    # Check service status
make logs      # Stream logs
make health    # Check API health
make dev       # Start dev mode with auto-reload
make dev-stop  # Stop dev mode, restart service
```

Standalone infrastructure for multi-project agent collaboration.
