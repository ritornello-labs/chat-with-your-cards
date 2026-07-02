SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON ?= python3
UV ?= uv

PY_FILES := $(shell git ls-files --cached --others --exclude-standard '*.py' ':!:dist/**' ':!:node_modules/**' ':!:.venv/**' ':!:_vendor/**')
MYPY_FILES := $(shell git ls-files --cached --others --exclude-standard '*.py' ':!:tests/**' ':!:dist/**' ':!:node_modules/**' ':!:.venv/**' ':!:_vendor/**')
JS_FILES := $(shell git ls-files --cached --others --exclude-standard '*.js' '*.mjs' ':!:dist/**' ':!:node_modules/**' ':!:chat_with_your_cards/web/vendor/**')
SHELL_FILES := $(shell git ls-files --cached --others --exclude-standard '*.sh')

.PHONY: help lint lint-paths lint-python lint-js lint-shell type test test-gui-smoke test-gui-smoke-docker check

help:
	@printf "Available targets:\n"
	@printf "  make lint   Run linters and source hygiene checks\n"
	@printf "  make type   Run type checks where typed source exists\n"
	@printf "  make test   Run unit tests and repository hygiene tests\n"
	@printf "  make test-gui-smoke         Run disposable Anki GUI smoke checks (macOS host)\n"
	@printf "  make test-gui-smoke-docker  Run the GUI smoke checks in Docker/Xvfb\n"
	@printf "  make check  Run lint, type, and test\n"

lint: lint-paths lint-python lint-js lint-shell

lint-paths:
	@$(PYTHON) tests/test_repo_hygiene.py --path-only

lint-python:
	@if [ -n "$(PY_FILES)" ]; then \
		$(UV) run --group dev ruff check $(PY_FILES); \
	else \
		printf "No Python files to lint.\n"; \
	fi

lint-js:
	@if [ -n "$(JS_FILES)" ]; then \
		for file in $(JS_FILES); do node --check "$$file"; done; \
	else \
		printf "No JavaScript files to lint.\n"; \
	fi

lint-shell:
	@if [ -n "$(SHELL_FILES)" ]; then \
		for file in $(SHELL_FILES); do bash -n "$$file"; done; \
	else \
		printf "No shell files to lint.\n"; \
	fi

type:
	@if [ -n "$(MYPY_FILES)" ]; then \
		$(UV) run --group dev mypy $(MYPY_FILES); \
	else \
		printf "No Python files to type-check.\n"; \
	fi

test:
	@if [ -d tests ]; then $(PYTHON) -m unittest discover -s tests -v; fi

test-gui-smoke:
	@$(UV) run --group dev anki-workbench smoke --timeout 120

test-gui-smoke-docker:
	@docker build -f tests/gui_smoke/Dockerfile -t chat-with-your-cards-anki-gui . && \
		docker run --rm -v "$$PWD":/workspace -w /workspace chat-with-your-cards-anki-gui

check: lint type test
