#!/usr/bin/env bash
# Fibonacci — publicación en GitHub.
#
# NO publica si preflight falla. Esa dependencia es deliberada: un repo público
# con las pruebas en rojo es peor que no publicar.
#
#   bash scripts/publish.sh                 # crea el repo y sube
#   bash scripts/publish.sh --dry-run       # muestra qué haría
#   bash scripts/publish.sh --pypi          # además publica en PyPI

set -euo pipefail
cd "$(dirname "$0")/.."

USUARIO="${GITHUB_USER:-desarrolladorgarza-pixel}"
REPO="fibonacci"
DRY=0
PYPI=0
PRERELEASE=0
for a in "$@"; do
  case "$a" in
    --dry-run)    DRY=1 ;;
    --pypi)       PYPI=1 ;;
    --prerelease) PRERELEASE=1 ;;
  esac
done

ejecutar() {
  if [ "$DRY" -eq 1 ]; then printf "  [dry-run] %s\n" "$*"; else eval "$@"; fi
}

echo "▸ Publicando Fibonacci como $USUARIO/$REPO"
echo

# --- Compuerta ------------------------------------------------------------
echo "▸ Verificación previa (modo estricto)"
if ! bash scripts/preflight.sh; then
  echo
  echo "  ══════════════════════════════════════════════════════════════"
  echo "  PUBLICACIÓN CANCELADA"
  echo
  echo "  No existe una bandera para saltarse esto. Un repo público con"
  echo "  las pruebas en rojo es peor que no publicar: la primera"
  echo "  impresión de un proyecto solo ocurre una vez."
  echo
  echo "  Si de verdad quieres publicar algo sin madurar, es defendible,"
  echo "  pero hazlo honestamente:"
  echo "    1. Arregla lo que esté en ✗ (mínimo: pruebas y aceptación)"
  echo "    2. bash scripts/publish.sh --prerelease"
  echo "  ══════════════════════════════════════════════════════════════"
  exit 1
fi

VERSION=$(python3 -c "import tomllib,pathlib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['version'])")
echo
echo "▸ Versión: $VERSION"

# --- Git ------------------------------------------------------------------
if [ ! -d .git ]; then
  echo "▸ Inicializando repositorio"
  ejecutar "git init -b main"
  ejecutar "git add ."
  ejecutar "git commit -q -m 'feat: Fibonacci v$VERSION — el agente que puedes deshacer'"
else
  if [ -n "$(git status --porcelain)" ]; then
    echo "▸ Confirmando cambios pendientes"
    ejecutar "git add ."
    ejecutar "git commit -q -m 'chore: preparar release v$VERSION'"
  fi
fi

# --- Crear el repo --------------------------------------------------------
# La descripción es lo que Google indexa y lo que aparece en las búsquedas de
# GitHub. En inglés y con las palabras que la gente teclea.
DESC="The AI agent you can undo. Local-first personal agent with a reversible action journal, decaying memory, prompt-injection defense and zero dependencies. Runs offline with Ollama."

if command -v gh >/dev/null 2>&1; then
  if ! gh auth status >/dev/null 2>&1; then
    echo "  ✗ gh no está autenticado. Ejecuta: gh auth login"
    echo "    No intentes rutas alternativas ni credenciales inventadas."
    exit 1
  fi

  if gh repo view "$USUARIO/$REPO" >/dev/null 2>&1; then
    echo "▸ El repo ya existe, subiendo"
    git remote get-url origin >/dev/null 2>&1 \
      || ejecutar "git remote add origin 'https://github.com/$USUARIO/$REPO.git'"
    ejecutar "git push -u origin main"
  else
    echo "▸ Creando repo público"
    if ! ejecutar "gh repo create '$USUARIO/$REPO' --public --source=. --push --description \"\$DESC\""; then
      echo
      echo "  No se pudo crear el repo. La causa habitual es de permisos:"
      echo "  una GitHub App con acceso a código, issues y workflows NO puede"
      echo "  crear repositorios — eso requiere el permiso Administration."
      echo
      echo "  Solución (30 segundos):"
      echo "    1. Crea el repo vacío en https://github.com/new"
      echo "       nombre: $REPO · público · sin README, .gitignore ni licencia"
      echo "    2. Dale acceso a ese repo a la app en Settings"
      echo "    3. Vuelve a ejecutar este script: detectará que ya existe y subirá"
      exit 1
    fi
  fi

  echo "▸ Topics"
  # GitHub permite 20 topics. Elegidos por volumen de búsqueda real, no por
  # describir el proyecto: "ai-agents" tiene tráfico, "reversible-computing" no.
  ejecutar "gh repo edit '$USUARIO/$REPO' \
    --add-topic ai-agent --add-topic ai-agents --add-topic agentic-ai \
    --add-topic autonomous-agents --add-topic agent-framework \
    --add-topic llm --add-topic local-llm --add-topic ollama \
    --add-topic mcp --add-topic model-context-protocol \
    --add-topic personal-assistant --add-topic cli \
    --add-topic python --add-topic self-hosted --add-topic privacy \
    --add-topic prompt-injection --add-topic ai-safety \
    --add-topic undo --add-topic offline --add-topic cross-platform"

  echo "▸ Ajustes del repo"
  ejecutar "gh repo edit '$USUARIO/$REPO' --enable-issues --enable-discussions --delete-branch-on-merge"

  echo "▸ Release v$VERSION"
  NOTAS=$(python3 - <<'PY'
import pathlib, re
txt = pathlib.Path("CHANGELOG.md").read_text()
m = re.search(r"## \[[^\]]+\][^\n]*\n(.*?)(?=\n## \[|\Z)", txt, re.S)
print((m.group(1).strip() if m else "Ver CHANGELOG.md")[:4000])
PY
)
  ejecutar "git tag -a 'v$VERSION' -m 'Fibonacci v$VERSION' 2>/dev/null || true"
  ejecutar "git push origin 'v$VERSION' 2>/dev/null || true"
  if [ "$DRY" -eq 1 ]; then
    echo "  [dry-run] gh release create v$VERSION"
  else
    FLAGS=""
    [ "$PRERELEASE" -eq 1 ] && FLAGS="--prerelease"
    gh release create "v$VERSION" --title "Fibonacci v$VERSION" \
      --notes "$NOTAS" $FLAGS 2>/dev/null \
      || echo "  (el release ya existía)"
  fi
else
  echo "  gh no está instalado. Crea el repo en https://github.com/new como '$REPO' y luego:"
  echo "    git remote add origin https://github.com/$USUARIO/$REPO.git"
  echo "    git push -u origin main"
  exit 1
fi

# --- PyPI (opcional) ------------------------------------------------------
if [ "$PYPI" -eq 1 ]; then
  echo "▸ Publicando en PyPI"
  ejecutar "rm -rf dist"
  ejecutar "python3 -m build"
  ejecutar "python3 -m twine upload dist/*"
fi

echo
echo "  Publicado: https://github.com/$USUARIO/$REPO"
echo
echo "  Para que lo encuentren, en orden de impacto:"
echo "    1. Sube una imagen de vista previa social en Settings > General"
echo "       (1280x640; es la miniatura en X, LinkedIn y Slack)"
echo "    2. Activa branch protection en main"
echo "    3. Publica en PyPI: bash scripts/publish.sh --pypi"
echo "    4. Envíalo a los sitios donde busca este público:"
echo "       · r/LocalLLaMA y r/selfhosted"
echo "       · Hacker News (Show HN), martes-jueves por la mañana ET"
echo "       · awesome-ai-agents y awesome-mcp-servers (abre un PR)"
echo "       · lobste.rs si tienes invitación"
echo "    5. Un issue con la etiqueta 'good first issue' atrae colaboradores"
echo
echo "  Lo que NO ayuda: publicarlo el mismo día en veinte sitios. Uno bueno,"
echo "  con una explicación honesta de qué resuelve, vale más que veinte"
echo "  enlaces sin contexto."
