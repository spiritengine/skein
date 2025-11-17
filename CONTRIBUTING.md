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

## Pull Request Process

1. Reference any related issues
2. Describe what changed and why
3. Ensure tests pass
4. Keep PRs focused

## Questions?

Open an issue with the "question" label.