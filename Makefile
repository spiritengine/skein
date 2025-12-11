.PHONY: help install install-service restart status logs start stop test dev dev-stop lint format docker-build docker-up docker-down

help:  ## Show this help
	@echo "SKEIN Makefile Commands:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36mmake %-15s\033[0m %s\n", $$1, $$2}'

install:  ## Install SKEIN package
	@echo "Installing SKEIN..."
	@pip install .

reinstall:  ## Reinstall SKEIN package and restart server
	@echo "🔄 Building wheel..."
	@rm -rf build/ dist/ *.egg-info
	@python -m build --wheel
	@echo "🔄 Installing to speakbot env..."
	@/home/patrick/.pyenv/versions/speakbot/bin/pip install --force-reinstall dist/skein-*.whl
	@echo "🔄 Restarting server..."
	@systemctl --user restart skein 2>/dev/null || sudo systemctl restart skein 2>/dev/null || true
	@sleep 1
	@echo "✅ Done"

install-service:  ## Install systemd user service
	@echo "Installing SKEIN systemd user service..."
	@mkdir -p ~/.config/systemd/user
	@sed -e 's|__WORKING_DIR__|$(PWD)|g' \
	     -e 's|__PYTHON__|$(shell which python)|g' \
	     -e 's|__PYTHON_BIN_DIR__|$(dir $(shell which python))|g' \
	     systemd/skein.service.template > ~/.config/systemd/user/skein.service
	@systemctl --user daemon-reload
	@systemctl --user enable skein
	@echo "Service installed. Use 'make start' to start SKEIN."

restart:  ## Restart SKEIN server
	@echo "🔄 Restarting SKEIN server..."
	@systemctl --user restart skein 2>/dev/null || sudo systemctl restart skein
	@sleep 2
	@make status

status:  ## Check SKEIN server status
	@systemctl --user status skein --no-pager 2>/dev/null || sudo systemctl status skein --no-pager | head -15

logs:  ## Stream SKEIN server logs
	@journalctl --user -u skein -f 2>/dev/null || sudo journalctl -u skein -f

start:  ## Start SKEIN server
	@echo "▶️  Starting SKEIN server..."
	@systemctl --user start skein 2>/dev/null || sudo systemctl start skein
	@sleep 2
	@make status

stop:  ## Stop SKEIN server
	@echo "⏹️  Stopping SKEIN server..."
	@systemctl --user stop skein 2>/dev/null || sudo systemctl stop skein

test:  ## Run SKEIN tests
	@echo "🧪 Running SKEIN tests..."
	@pytest tests/ -v 2>/dev/null || echo "No tests found"

cli-dev:  ## Run CLI from local worktree code (for testing shard changes)
	@echo "🔧 Running local CLI..."
	@python -m client.cli $(ARGS)

health:  ## Check SKEIN server health
	@echo "🏥 Checking SKEIN health..."
	@curl -s http://localhost:8001/health || echo "❌ SKEIN server not responding"

dev:  ## Run SKEIN in dev mode with auto-reload (stops systemctl service)
	@echo "🚀 Starting SKEIN in dev mode with auto-reload..."
	@echo "   (Stopping systemctl service first)"
	@systemctl --user stop skein 2>/dev/null || sudo systemctl stop skein 2>/dev/null || true
	@echo "   Starting uvicorn with --reload..."
	@echo "   Press Ctrl+C to stop"
	@uvicorn skein_server:app --host 0.0.0.0 --port 8001 --reload

dev-stop:  ## Stop dev mode and restart systemctl service
	@echo "⏹️  Stopping dev mode (if running)..."
	@pkill -f "uvicorn skein_server" 2>/dev/null || true
	@echo "🔄 Restarting systemctl service..."
	@systemctl --user restart skein 2>/dev/null || sudo systemctl restart skein
	@sleep 2
	@make status

lint:  ## Run linters (black, isort, flake8, mypy)
	@echo "🔍 Running linters..."
	@black --check skein/ client/ tests/ skein_server.py || true
	@isort --check-only skein/ client/ tests/ skein_server.py || true
	@flake8 skein/ client/ tests/ skein_server.py --max-line-length=100 || true
	@mypy skein/ client/ --ignore-missing-imports || true

format:  ## Format code with black and isort
	@echo "✨ Formatting code..."
	@black skein/ client/ tests/ skein_server.py
	@isort skein/ client/ tests/ skein_server.py

docker-build:  ## Build Docker images
	@echo "🐳 Building Docker images..."
	@docker build -t skein:latest .
	@docker build -f Dockerfile.cli -t skein-cli:latest .

docker-up:  ## Start Docker services
	@echo "🐳 Starting Docker services..."
	@docker-compose up -d

docker-down:  ## Stop Docker services
	@echo "🐳 Stopping Docker services..."
	@docker-compose down
