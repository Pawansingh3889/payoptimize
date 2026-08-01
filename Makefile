.PHONY: setup test lint fmt check serve up down logs clean

# Bootstrap a local .env with a generated admin token. Safe to re-run: it never
# overwrites an existing .env, because that file may hold the Prava key.
setup:
	@uv sync
	@if [ -f .env ]; then \
		echo ".env already exists — leaving it alone"; \
	else \
		cp .env.example .env; \
		token=$$(openssl rand -base64 24 2>/dev/null || head -c 18 /dev/urandom | base64); \
		sed -i.bak "s|^PAYOPTIMIZE_ADMIN_TOKEN=.*|PAYOPTIMIZE_ADMIN_TOKEN=$$token|" .env; \
		rm -f .env.bak; \
		echo "wrote .env with a generated PAYOPTIMIZE_ADMIN_TOKEN"; \
		echo "the Prava rail stays off until you add PRAVA_SECRET_KEY"; \
	fi

test:
	uv run pytest -q

lint:
	uv run ruff check

fmt:
	uv run ruff format
	uv run ruff check --fix

# What has to be green before a commit.
check: lint test

serve:
	uv run python -m payoptimize serve

# The fallback if Fly misbehaves. `make setup` first.
up:
	podman compose up --build

down:
	podman compose down

logs:
	podman compose logs -f

clean:
	rm -rf .pytest_cache .ruff_cache data
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
