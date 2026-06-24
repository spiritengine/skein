.PHONY: help install reinstall serve test cli-dev lint format docker-build docker-up docker-down

help:  ## Show this help
	@echo "skein Makefile Commands:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36mmake %-15s\033[0m %s\n", $$1, $$2}'

install:  ## Install the skein package (editable)
	@echo "Installing skein..."
	@pip install -e .

reinstall:  ## Reinstall the skein package (editable)
	@echo "🔄 Reinstalling skein (editable)..."
	@pip install -e . --force-reinstall --no-deps
	@echo "✅ Done"

serve:  ## Serve the read-only web surface (skein serve, default :9001)
	@echo "🌐 Serving the station web surface (Ctrl+C to stop)..."
	@skein serve

test:  ## Run the test suite
	@echo "🧪 Running skein tests..."
	@pytest skein/tests/ -v 2>/dev/null || echo "No tests found"

cli-dev:  ## Run the CLI from local worktree code (for testing shard changes)
	@echo "🔧 Running local CLI..."
	@python -m skein $(ARGS)

lint:  ## Run linters (black, isort, flake8, mypy)
	@echo "🔍 Running linters..."
	@black --check skein/ || true
	@isort --check-only skein/ || true
	@flake8 skein/ --max-line-length=100 || true
	@mypy skein/ --ignore-missing-imports || true

format:  ## Format code with black and isort
	@echo "✨ Formatting code..."
	@black skein/
	@isort skein/

docker-build:  ## Build the interskein web instance image
	@echo "🐳 Building interskein image..."
	@docker build -t interskein:latest .

docker-up:  ## Start the interskein instance (set INTERSKEIN_CORPUS=/path/to/.skein)
	@echo "🐳 Starting interskein (corpus: $(INTERSKEIN_CORPUS))..."
	@docker compose up -d --build

docker-down:  ## Stop the interskein instance
	@echo "🐳 Stopping interskein..."
	@docker compose down
