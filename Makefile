PYTHON ?= .venv/bin/python
CARGO ?= cargo

.PHONY: help install build test lint verify wheel sdist
help:
	@echo "make install/build/test/lint/verify/wheel/sdist"
install:
	uv sync --extra dev --locked
build:
	$(CARGO) build --locked
test: build
	$(CARGO) test --locked
	REPL_MCP_BINARY=$(or $(CARGO_TARGET_DIR),target)/debug/repl-mcp $(PYTHON) -m pytest tests/ -q
lint:
	$(CARGO) fmt --check
	$(CARGO) clippy --locked --all-targets -- -D warnings
	$(PYTHON) -m ruff check src/repl_mcp/native_worker.py tests/test_native*.py
verify: lint test
wheel:
	$(PYTHON) -m maturin build --locked --release
sdist:
	$(PYTHON) -m maturin sdist
