# k8s-ai-support Makefile
# Usage: make <target>

.PHONY: help install install-dev test test-unit test-integration lint format typecheck \
        build docker-build docker-run kind-setup kind-teardown clean

POETRY := poetry
PYTHON := $(POETRY) run python
PYTEST := $(POETRY) run pytest
RUFF := $(POETRY) run ruff
BLACK := $(POETRY) run black
MYPY := $(POETRY) run mypy

# Default target
help:
	@echo "k8s-ai-support — available make targets:"
	@echo ""
	@echo "  install         Install all dependencies"
	@echo "  install-dev     Install + dev dependencies"
	@echo "  test            Run all unit tests"
	@echo "  test-unit       Run unit tests only (no cluster needed)"
	@echo "  test-integration Run integration tests (requires kind cluster)"
	@echo "  lint            Run ruff linter"
	@echo "  format          Format code with black + ruff"
	@echo "  typecheck       Run mypy type checking"
	@echo "  build           Build Python package"
	@echo "  docker-build    Build Docker image"
	@echo "  docker-run      Run agent in Docker"
	@echo "  kind-setup      Create kind cluster with test fixtures"
	@echo "  kind-teardown   Delete kind cluster"
	@echo "  clean           Clean build artifacts"
	@echo "  check           Check cluster + LLM configuration"


# ─────────────────────────── Installation ────────────────────────────────────

install:
	$(POETRY) install --extras "all" --no-interaction

install-dev:
	$(POETRY) install --extras "all" --with dev --no-interaction
	$(POETRY) run pre-commit install


# ─────────────────────────── Testing ─────────────────────────────────────────

test: test-unit

test-unit:
	$(PYTEST) tests/test_agent.py tests/test_mcp_server.py -v \
		--tb=short \
		-x \
		--no-header

test-integration: kind-setup
	$(PYTEST) tests/ -v -m "integration" --tb=short
	$(MAKE) kind-teardown

test-coverage:
	$(PYTEST) tests/test_agent.py tests/test_mcp_server.py \
		--cov=src \
		--cov-report=html:htmlcov \
		--cov-report=term-missing \
		--cov-fail-under=70 \
		-v

test-fast:
	$(PYTEST) tests/ -v --tb=short -x -q --no-header \
		--ignore=tests/test_mcp_server.py


# ─────────────────────────── Code Quality ────────────────────────────────────

lint:
	$(RUFF) check src/ tests/
	@echo "Lint passed!"

format:
	$(BLACK) src/ tests/
	$(RUFF) check --fix src/ tests/
	@echo "Formatting done!"

typecheck:
	$(MYPY) src/ --ignore-missing-imports
	@echo "Type check passed!"

check-all: lint typecheck test
	@echo "All checks passed!"


# ─────────────────────────── Build ───────────────────────────────────────────

build:
	$(POETRY) build

docker-build:
	docker build -t k8s-ai-support:latest .
	@echo "Docker image built: k8s-ai-support:latest"

docker-build-dev:
	docker build --target builder -t k8s-ai-support:dev .

docker-run:
	docker run --rm -it \
		-e OPENAI_API_KEY=$(OPENAI_API_KEY) \
		-e K8S_AI_PROVIDER=$(K8S_AI_PROVIDER) \
		-e K8S_AI_MODEL=$(K8S_AI_MODEL) \
		-v $(HOME)/.kube:/home/k8sai/.kube:ro \
		k8s-ai-support:latest diagnose "$(query)"

docker-mcp:
	docker run --rm -i \
		-e OPENAI_API_KEY=$(OPENAI_API_KEY) \
		-v $(HOME)/.kube:/home/k8sai/.kube:ro \
		k8s-ai-support:latest mcp


# ─────────────────────────── Kind Cluster ────────────────────────────────────

kind-setup:
	@echo "Creating kind cluster..."
	kind create cluster --config tests/fixtures/kind_cluster.yaml --wait 120s || true
	kubectl config use-context kind-k8s-ai-test
	@echo "Applying test fixtures..."
	kubectl apply -f tests/fixtures/kind_cluster.yaml --ignore-not-found || true
	@echo "Kind cluster ready!"

kind-teardown:
	@echo "Deleting kind cluster..."
	kind delete cluster --name k8s-ai-test
	@echo "Done."

kind-status:
	kubectl get pods -n k8s-ai-test -o wide
	kubectl get events -n k8s-ai-test --sort-by=.lastTimestamp | tail -20


# ─────────────────────────── Helm ────────────────────────────────────────────

helm-lint:
	helm lint helm/k8s-ai-support/

helm-template:
	helm template k8s-ai-support helm/k8s-ai-support/ \
		--set apiKeys.openaiApiKey=test-key

helm-install:
	helm install k8s-ai-support helm/k8s-ai-support/ \
		--namespace k8s-ai \
		--create-namespace \
		--set apiKeys.openaiApiKey=$(OPENAI_API_KEY) \
		--set llm.provider=openai

helm-uninstall:
	helm uninstall k8s-ai-support --namespace k8s-ai


# ─────────────────────────── Dev Helpers ─────────────────────────────────────

check:
	$(PYTHON) -m src.cli.main check

interactive:
	$(PYTHON) -m src.cli.main --interactive

mcp-test:
	@echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"test"},"protocolVersion":"2024-11-05"}}' | \
		$(PYTHON) -m src.cli.main mcp

mcp-tools-list:
	@printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"test"},"protocolVersion":"2024-11-05"}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n' | \
		$(PYTHON) -m src.cli.main mcp


# ─────────────────────────── Cleanup ─────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name htmlcov -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name ".coverage" -delete 2>/dev/null || true
	rm -rf dist/ build/ *.egg-info/
	@echo "Cleaned!"

clean-cache:
	rm -rf ~/.cache/k8s-ai/
	@echo "Cache cleared!"
