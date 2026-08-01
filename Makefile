.PHONY: test cov cov-html lint fix check gaps clean preflight publish install-dev

test:
	pytest tests/ -q

cov:
	pytest tests/ --cov=fibonacci --cov-report=term-missing

cov-html:
	pytest tests/ --cov=fibonacci --cov-report=html
	@echo "abre htmlcov/index.html"

lint:
	ruff check fibonacci/ tests/

fix:
	ruff check --fix fibonacci/ tests/

# Lo que debe pasar antes de un PR
check: lint test
	@echo "listo"

# Módulos ordenados por cobertura ascendente: dónde falta trabajo
gaps:
	pytest tests/ --cov=fibonacci --cov-report=term 2>/dev/null | sort -k4 -n | head -20

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage

install-dev:
	pip install -e ".[dev,crypto]"
	pip install build twine ruff

# Compuerta: nueve verificaciones. Devuelve 0 solo si todo pasa.
preflight:
	@bash scripts/preflight.sh

preflight-strict:
	@bash scripts/preflight.sh --strict

# No publica si preflight falla.
publish:
	@bash scripts/publish.sh

publish-dry:
	@bash scripts/publish.sh --dry-run
