#!/usr/bin/env bash
# Fibonacci — verificación previa a publicar.
#
# Convierte "¿está listo?" en una pregunta mecánica. Devuelve 0 solo si todas
# las compuertas pasan. Está pensado para que Codex (o cualquiera) no tenga que
# emitir un juicio: o pasa, o dice exactamente qué falta.
#
#   bash scripts/preflight.sh              # verificación estándar
#   bash scripts/preflight.sh --strict     # exige también la cobertura objetivo

set -uo pipefail
cd "$(dirname "$0")/.."

# Estricto POR DEFECTO. Publicar es la operación irreversible del proyecto:
# el default debe ser el seguro. `--laxo` existe para desarrollo local.
STRICT=1
[[ "${1:-}" == "--laxo" ]] && STRICT=0

FALLOS=0
AVISOS=0
COV_MINIMA=60
COV_OBJETIVO=75

rojo()  { printf "\033[31m  ✗ %s\033[0m\n" "$1"; FALLOS=$((FALLOS+1)); }
verde() { printf "\033[32m  ✓ %s\033[0m\n" "$1"; }
ambar() { printf "\033[33m  ! %s\033[0m\n" "$1"; AVISOS=$((AVISOS+1)); }
titulo(){ printf "\n\033[1m%s\033[0m\n" "$1"; }

echo "Fibonacci — verificación previa a publicar"

# ---------------------------------------------------------------------------
titulo "1. Entorno"

python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null \
  && verde "Python $(python3 -V | cut -d' ' -f2)" \
  || rojo "se requiere Python 3.11+"

command -v git >/dev/null && verde "git" || rojo "git no está instalado"
command -v gh  >/dev/null && verde "gh (GitHub CLI)" \
  || ambar "gh no está: tendrás que crear el repo a mano"

python3 -c "import pytest" 2>/dev/null && verde "pytest" \
  || rojo "falta pytest: pip install -e '.[dev,crypto]'"

# ---------------------------------------------------------------------------
titulo "2. Pruebas"

if python3 -m pytest tests/ -q > /tmp/fib_test.log 2>&1; then
  N=$(grep -oE '[0-9]+ passed' /tmp/fib_test.log | head -1)
  verde "suite completa en verde (${N:-?})"
else
  rojo "las pruebas fallan — mira /tmp/fib_test.log"
  tail -15 /tmp/fib_test.log | sed 's/^/      /'
fi

# Las de aceptación se corren aparte y se reportan aparte: verifican que el
# README no miente, que es una clase de fallo distinta a "una función tiene un
# bug". Si una de estas falla, o el código está roto o la portada engaña.
if python3 -m pytest tests/ -q -m acceptance > /tmp/fib_acc.log 2>&1; then
  A=$(grep -oE '[0-9]+ passed' /tmp/fib_acc.log | head -1)
  verde "aceptación: cada promesa del README se cumple (${A:-?})"
else
  rojo "ACEPTACIÓN EN ROJO: el README promete algo que el código no hace"
  grep -E "^FAILED|^tests.*FAIL" /tmp/fib_acc.log | head -8 | sed 's/^/      /'
  echo "      Corrige el código, o quita la promesa del README. No suavices la aserción."
fi

# ---------------------------------------------------------------------------
titulo "3. Cobertura"

if python3 -c "import pytest_cov" 2>/dev/null; then
  python3 -m pytest tests/ --cov=fibonacci --cov-report=term > /tmp/fib_cov.log 2>&1
  COV=$(grep -oE 'TOTAL.*[0-9]+%' /tmp/fib_cov.log | grep -oE '[0-9]+%' | tr -d '%')
  COV=${COV:-0}
  if [ "$COV" -ge "$COV_OBJETIVO" ]; then
    verde "cobertura ${COV}% (objetivo ${COV_OBJETIVO}%)"
  elif [ "$COV" -ge "$COV_MINIMA" ]; then
    if [ "$STRICT" -eq 1 ]; then
      rojo "cobertura ${COV}% < objetivo ${COV_OBJETIVO}% (modo estricto)"
    else
      ambar "cobertura ${COV}%: por encima del mínimo pero bajo el objetivo ${COV_OBJETIVO}%"
    fi
    echo "      módulos con menos cobertura:"
    grep -E '^fibonacci/' /tmp/fib_cov.log | sort -t'%' -k1 -n | head -5 | sed 's/^/        /'
  else
    rojo "cobertura ${COV}% < mínimo ${COV_MINIMA}%"
  fi
else
  ambar "sin pytest-cov: no puedo medir cobertura"
fi

# ---------------------------------------------------------------------------
titulo "4. Estilo"

if command -v ruff >/dev/null; then
  ruff check fibonacci/ tests/ >/dev/null 2>&1 \
    && verde "ruff limpio" \
    || { rojo "ruff reporta problemas"; ruff check fibonacci/ tests/ 2>&1 | head -8 | sed 's/^/      /'; }
else
  ambar "ruff no instalado"
fi

# ---------------------------------------------------------------------------
titulo "5. Consistencia de versión"

V_PY=$(python3 -c "import re,pathlib; print(re.search(r'__version__ = \"([^\"]+)\"', pathlib.Path('fibonacci/__init__.py').read_text()).group(1))")
V_TOML=$(python3 -c "import tomllib,pathlib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['version'])")
V_CFF=$(grep -oE '^version: .*' CITATION.cff 2>/dev/null | cut -d' ' -f2)

if [ "$V_PY" = "$V_TOML" ]; then
  verde "versión $V_PY consistente (__init__ ↔ pyproject)"
else
  rojo "desajuste: __init__=$V_PY pyproject=$V_TOML"
fi
[ "$V_CFF" = "$V_PY" ] || ambar "CITATION.cff dice $V_CFF, el paquete $V_PY"

grep -q "\[$V_PY\]" CHANGELOG.md \
  && verde "CHANGELOG tiene entrada para $V_PY" \
  || rojo "CHANGELOG sin entrada para $V_PY"

# El README cita su propia versión en dos sitios y es fácil que se desfase.
# Ya pasó una vez: decía v0.2.0 en la 0.7.0.
DESFASE=$(grep -oE "v?[0-9]+\.[0-9]+\.[0-9]+" README.md | grep -v "^v\?$V_PY$" \
  | grep -vE "^3\.(11|12|13)" | sort -u | head -3)
[ -z "$DESFASE" ] && verde "el README no cita versiones desfasadas" \
  || { rojo "el README menciona versiones que no son $V_PY:"; echo "$DESFASE" | sed 's/^/      /'; }

# ---------------------------------------------------------------------------
titulo "6. Secretos y basura"

FUGAS=$(grep -rIn -E "sk-ant-[A-Za-z0-9]{20}|sk-proj-[A-Za-z0-9]{20}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30}" \
  --include="*.py" --include="*.md" --include="*.toml" --include="*.yml" . 2>/dev/null \
  | grep -v -E "security\.py|test_|SECURITY\.md" | head -3)
[ -z "$FUGAS" ] && verde "sin credenciales en el árbol" \
  || { rojo "posible secreto en el código:"; echo "$FUGAS" | sed 's/^/      /'; }

BASURA=$(find . -name "*.db" -o -name "*.enc" -o -name ".env" \
  -o -name "htmlcov" -o -name ".coverage" 2>/dev/null | grep -v node_modules | head -5)
[ -z "$BASURA" ] && verde "sin archivos generados" \
  || { ambar "archivos que no deberían subir:"; echo "$BASURA" | sed 's/^/      /'; }

# ---------------------------------------------------------------------------
titulo "7. Documentación y licencia"

for f in README.md LICENSE NOTICE SECURITY.md CHANGELOG.md CONTRIBUTING.md \
         CODE_OF_CONDUCT.md docs/ARQUITECTURA.md; do
  [ -f "$f" ] && verde "$f" || rojo "falta $f"
done

grep -qi "hermes" NOTICE 2>/dev/null \
  && verde "NOTICE atribuye a Hermes Agent" \
  || rojo "NOTICE sin la atribución a Nous Research"

# El README no debe prometer lo que el roadmap marca como pendiente.
if grep -q "streaming" README.md && grep -qE "^\- \[ \].*[Ss]treaming" README.md; then
  rojo "el README promete streaming y a la vez lo lista como pendiente"
else
  verde "README sin promesas contradictorias"
fi

# ---------------------------------------------------------------------------
titulo "8. Paquete"

if python3 -c "import build" 2>/dev/null; then
  rm -rf dist build ./*.egg-info 2>/dev/null
  if python3 -m build >/tmp/fib_build.log 2>&1; then
    verde "construye ($(ls dist/ | tr '\n' ' '))"
    python3 -c "import twine" 2>/dev/null && {
      python3 -m twine check dist/* >/dev/null 2>&1 \
        && verde "metadatos válidos para PyPI" \
        || ambar "twine check reporta problemas"; }
  else
    rojo "falla al construir — mira /tmp/fib_build.log"
  fi
else
  ambar "sin módulo build: pip install build twine"
fi

# ---------------------------------------------------------------------------
titulo "9. Instalación limpia"

TMPV=$(mktemp -d)
if python3 -m venv "$TMPV" 2>/dev/null && "$TMPV/bin/pip" install -q -e . 2>/dev/null; then
  "$TMPV/bin/fib" config >/dev/null 2>&1 \
    && verde "instala y el CLI arranca en un venv limpio" \
    || rojo "instala pero el CLI no arranca"
else
  ambar "no pude verificar la instalación limpia"
fi
rm -rf "$TMPV"

# ---------------------------------------------------------------------------
titulo "Resultado"

if [ "$FALLOS" -eq 0 ]; then
  printf "\033[32m  LISTO PARA PUBLICAR\033[0m"
  [ "$AVISOS" -gt 0 ] && printf " (con %d aviso(s))" "$AVISOS"
  echo; echo "  Siguiente: bash scripts/publish.sh"
  exit 0
else
  printf "\033[31m  NO PUBLICAR: %d fallo(s), %d aviso(s)\033[0m\n" "$FALLOS" "$AVISOS"
  echo "  Corrige lo marcado con ✗ y vuelve a correr."
  exit 1
fi
