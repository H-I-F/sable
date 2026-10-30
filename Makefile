# Local entry points, one per gate.
#
# Every target wraps the exact command CI runs (see .github/workflows/ci.yml), so
# "green locally" and "green in CI" mean the same thing rather than two similar
# things. `make -n <target>` prints the command without running it, which is the
# quickest way to check that the two still agree.
#
# The interpreter is a variable, so a virtualenv can be used without activating
# it and without this file guessing where it lives:
#
#     make test PYTHON=.venv/bin/python
#
# Recipes are POSIX sh. Nothing here needs bash, and nothing here writes outside
# the working tree.

PYTHON ?= python3
LINT_PATHS := src tests examples benchmarks
CHAOS_TESTS := tests/test_crash.py tests/test_chaos_enospc.py tests/test_chaos_fd_leak.py
COVERAGE_FLOOR := 85

.DEFAULT_GOAL := help
.PHONY: help test lint typecheck chaos clean

help:  ## List the targets and what each one proves
	@printf 'usage: make <target>\n\n'
	@printf '  test       the suite plus the %s%% coverage floor (CI test job)\n' '$(COVERAGE_FLOOR)'
	@printf '  lint       ruff over src, tests, examples and benchmarks (CI lint job)\n'
	@printf '  typecheck  mypy over src (CI types job)\n'
	@printf '  chaos      fault-injection tests only: crash matrix, ENOSPC, fd leaks\n'
	@printf '  clean      remove caches and build artefacts, leave the venv alone\n'

test:  ## Run the whole suite with the coverage floor, as CI does
	$(PYTHON) -m pytest -q --cov=sable --cov-fail-under=$(COVERAGE_FLOOR) tests/

lint:  ## Lint every importable tree, not just the package
	$(PYTHON) -m ruff check $(LINT_PATHS)

typecheck:  ## Type-check the package the way CI does
	$(PYTHON) -m mypy src

chaos:  ## Re-run only the tests that break the machine on purpose
	$(PYTHON) -m pytest -q $(CHAOS_TESTS)

clean:  ## Remove caches and build artefacts
	@find . -type d -name __pycache__ -prune -exec rm -rf {} \;
	@rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage \
		dist build src/sable.egg-info src/*.egg-info
	@printf 'caches and build artefacts removed\n'
