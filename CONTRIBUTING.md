# Contributing to SKEIN

## Getting Started

1. Fork the repository
2. Clone your fork
3. Create a feature branch: `git checkout -b feature-name`
4. Make your changes
5. Push to your fork and open a PR

## Development Setup

```bash
pip install -r requirements.txt
skein init --project skein-dev
make dev  # Start server in dev mode
```

## Code Style

- Follow PEP 8
- Add docstrings to public functions
- Keep functions small and focused

## Testing

```bash
python tests/test_skein.py
python tests/test_cli_validation.py
```

## Testing CLI Changes in Shard Worktrees

When working in a shard worktree (e.g., via Spindle's `permission="shard"`), the system-installed `skein` command uses the main repo's code, not your worktree's code. To test CLI changes locally:

**Option 1: Use the skein-dev wrapper**
```bash
./skein-dev --help                    # Uses worktree's client/cli.py
./skein-dev folio issue-123          # Same commands as skein
```

**Option 2: Use make cli-dev**
```bash
make cli-dev ARGS="--help"
make cli-dev ARGS="folio issue-123"
```

**Option 3: Run Python module directly**
```bash
python -m client.cli --help
python -m client.cli folio issue-123
```

All three methods import from the current directory, ensuring your worktree's code is used instead of the installed package.

## Pull Request Process

1. Reference any related issues
2. Describe what changed and why
3. Ensure tests pass
4. Keep PRs focused

## Questions?

Open an issue with the "question" label.