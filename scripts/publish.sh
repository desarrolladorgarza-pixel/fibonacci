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

# --- Cómo hablamos con GitHub ---------------------------------------------
#
# Dos vías: `gh` si está, y la API REST con `GITHUB_TOKEN` si no.
#
# La segunda existe porque la primera falta justo donde más se necesita. En un
# runner de CI, en un contenedor y en los agentes (Claude, Codex) `gh` no viene
# instalado, y este script se rendía ahí con un "instálalo y hazlo a mano" —
# después de haber pasado toda la compuerta. Con un token basta.
#
# Si tu token no puede hacer algo, se dice cuál es el permiso que falta y se
# sigue con lo demás en vez de abortar: media publicación con un aviso claro es
# más útil que ninguna con un 403 sin explicar.

DESC="The AI agent you can undo. Local-first personal agent with a reversible action journal, decaying memory, prompt-injection defense and zero dependencies. Runs offline with Ollama."
API="https://api.github.com"
SLUG="$USUARIO/$REPO"
AVISOS=0

aviso() { printf "  \033[33m! %s\033[0m\n" "$1"; AVISOS=$((AVISOS+1)); }
# En dry-run nada se ejecuta, asi que nada puede declararse hecho: un simulacro
# que reparte ✓ por operaciones que no hizo es peor que no tener simulacro.
ok() {
  if [ "$DRY" -eq 1 ]; then printf "  \033[90m· %s (no ejecutado)\033[0m\n" "$1"
  else printf "  \033[32m✓ %s\033[0m\n" "$1"; fi
}

if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  VIA="gh"
elif [ -n "${GITHUB_TOKEN:-}" ]; then
  VIA="rest"
else
  echo "  ✗ No hay forma de hablar con GitHub."
  echo "    Instala y autentica gh (gh auth login), o exporta GITHUB_TOKEN"
  echo "    con permisos de repo."
  echo "    No inventes credenciales ni intentes rutas alternativas."
  exit 1
fi
echo "▸ GitHub vía: $VIA"

# gh_api METODO RUTA [JSON]  ->  imprime el cuerpo; devuelve !=0 si falló
gh_api() {
  local metodo="$1" ruta="$2" cuerpo="${3:-}"
  if [ "$DRY" -eq 1 ]; then
    printf "  [dry-run] %s %s %s\n" "$metodo" "$ruta" "${cuerpo:0:80}" >&2
    return 0
  fi
  local resp code
  if [ "$VIA" = "gh" ]; then
    if [ -n "$cuerpo" ]; then
      gh api -X "$metodo" "$ruta" --input - <<< "$cuerpo" 2>/dev/null
    else
      gh api -X "$metodo" "$ruta" 2>/dev/null
    fi
    return $?
  fi
  resp=$(mktemp)
  if [ -n "$cuerpo" ]; then
    code=$(curl -sS -o "$resp" -w "%{http_code}" -X "$metodo" \
      -H "Authorization: Bearer $GITHUB_TOKEN" \
      -H "Accept: application/vnd.github+json" \
      -H "Content-Type: application/json" \
      --data-binary "$cuerpo" "$API/$ruta")
  else
    code=$(curl -sS -o "$resp" -w "%{http_code}" -X "$metodo" \
      -H "Authorization: Bearer $GITHUB_TOKEN" \
      -H "Accept: application/vnd.github+json" "$API/$ruta")
  fi
  cat "$resp"; rm -f "$resp"
  [ "$code" -lt 300 ]
}

# --- Crear el repo o detectar que ya existe --------------------------------
if gh_api GET "repos/$SLUG" >/dev/null 2>&1; then
  echo "▸ El repo ya existe, subiendo"
else
  echo "▸ Creando repo público"
  if ! gh_api POST "user/repos" \
      "{\"name\":\"$REPO\",\"private\":false,\"description\":\"$DESC\"}" \
      >/dev/null 2>&1; then
    echo
    echo "  No se pudo crear el repo. La causa habitual es de permisos: una"
    echo "  GitHub App con acceso a código, issues y workflows NO puede crear"
    echo "  repositorios — eso requiere el permiso Administration."
    echo
    echo "  Solución (30 segundos):"
    echo "    1. Crea el repo vacío en https://github.com/new"
    echo "       nombre: $REPO · público · sin README, .gitignore ni licencia"
    echo "    2. Dale acceso a ese repo a la app en Settings"
    echo "    3. Vuelve a ejecutar este script: detectará que existe y subirá"
    exit 1
  fi
fi

git remote get-url origin >/dev/null 2>&1 \
  || ejecutar "git remote add origin 'https://github.com/$SLUG.git'"
ejecutar "git push -u origin main"

# --- Topics ----------------------------------------------------------------
# GitHub permite 20. Elegidos por volumen de búsqueda real, no por describir el
# proyecto: "ai-agents" tiene tráfico, "reversible-computing" no.
echo "▸ Topics"
TOPICS='["ai-agent","ai-agents","agentic-ai","autonomous-agents","agent-framework","llm","local-llm","ollama","mcp","model-context-protocol","personal-assistant","cli","python","self-hosted","privacy","prompt-injection","ai-safety","undo","offline","cross-platform"]'
gh_api PUT "repos/$SLUG/topics" "{\"names\":$TOPICS}" >/dev/null 2>&1 \
  && ok "20 topics" \
  || aviso "no pude poner los topics (falta permiso Administration)"

# --- Ajustes ---------------------------------------------------------------
echo "▸ Ajustes del repo"
gh_api PATCH "repos/$SLUG" \
  "{\"description\":\"$DESC\",\"has_issues\":true,\"has_discussions\":true,\"delete_branch_on_merge\":true}" \
  >/dev/null 2>&1 \
  && ok "descripción, issues, discussions, borrado de ramas al fusionar" \
  || aviso "no pude cambiar los ajustes (falta permiso Administration)"

# --- Etiqueta y release ----------------------------------------------------
echo "▸ Release v$VERSION"
NOTAS=$(python3 - <<'PY'
import json, pathlib, re
txt = pathlib.Path("CHANGELOG.md").read_text(encoding="utf-8")
m = re.search(r"## \[[^\]]+\][^\n]*\n(.*?)(?=\n## \[|\Z)", txt, re.S)
print(json.dumps((m.group(1).strip() if m else "Ver CHANGELOG.md")[:60000]))
PY
)
ejecutar "git tag -a 'v$VERSION' -m 'Fibonacci v$VERSION' 2>/dev/null || true"
# El error de git se silencia porque el fallo lo gestionamos abajo: dejarlo
# escupir su 403 crudo solo asusta a quien lee la salida.
if ! ejecutar "git push origin 'refs/tags/v$VERSION' >/dev/null 2>&1"; then
  # Algunos proxys y apps permiten empujar ramas pero no etiquetas; la API
  # es otra puerta al mismo sitio.
  SHA=$(git rev-parse HEAD)
  gh_api POST "repos/$SLUG/git/refs" \
    "{\"ref\":\"refs/tags/v$VERSION\",\"sha\":\"$SHA\"}" >/dev/null 2>&1 \
    && ok "etiqueta v$VERSION (por API)" \
    || aviso "no pude crear la etiqueta v$VERSION"
fi

PRE=false
[ "$PRERELEASE" -eq 1 ] && PRE=true
gh_api POST "repos/$SLUG/releases" \
  "{\"tag_name\":\"v$VERSION\",\"name\":\"Fibonacci v$VERSION\",\"body\":$NOTAS,\"prerelease\":$PRE}" \
  >/dev/null 2>&1 \
  && ok "release v$VERSION con las notas del CHANGELOG" \
  || aviso "no pude crear el release (¿ya existía, o falta permiso Contents?)"

# --- Branch protection -----------------------------------------------------
# Estaba en RELEASE.md §4 como paso manual. Es una llamada: que la haga el
# script, que para eso existe.
echo "▸ Protección de main"
gh_api PUT "repos/$SLUG/branches/main/protection" \
  '{"required_status_checks":{"strict":true,"contexts":["test (ubuntu-latest, 3.12)","lint","coverage"]},"enforce_admins":false,"required_pull_request_reviews":null,"restrictions":null,"allow_force_pushes":false,"allow_deletions":false}' \
  >/dev/null 2>&1 \
  && ok "main exige CI en verde y no acepta force-push" \
  || aviso "no pude activar branch protection (falta permiso Administration)"

# --- PyPI (opcional) ------------------------------------------------------
if [ "$PYPI" -eq 1 ]; then
  echo "▸ Publicando en PyPI"
  ejecutar "rm -rf dist"
  ejecutar "python3 -m build"
  # Subir a PyPI es irreversible: una version publicada no se puede
  # reemplazar, solo borrar. Que `twine check` pase ANTES de subir.
  if [ "$DRY" -eq 0 ] && ! python3 -m twine check dist/*; then
    echo "  ✗ Los metadatos del paquete no pasan twine check. No subo."
    exit 1
  fi
  ejecutar "python3 -m twine upload dist/*"
fi

echo
if [ "$AVISOS" -gt 0 ]; then
  printf "\033[33m  %d paso(s) no se pudieron completar — mira los ! de arriba.\033[0m\n" "$AVISOS"
  echo "  Suele ser el permiso Administration del token. Lo demás sí subió."
  echo
fi
echo "  Publicado: https://github.com/$USUARIO/$REPO"
echo
echo "  Para que lo encuentren, en orden de impacto:"
echo "    1. Sube una imagen de vista previa social en Settings > General"
echo "       (1280x640; es la miniatura en X, LinkedIn y Slack)"
echo "    2. Publica en PyPI: bash scripts/publish.sh --pypi"
echo "    3. Envíalo a los sitios donde busca este público:"
echo "       · r/LocalLLaMA y r/selfhosted"
echo "       · Hacker News (Show HN), martes-jueves por la mañana ET"
echo "       · awesome-ai-agents y awesome-mcp-servers (abre un PR)"
echo "       · lobste.rs si tienes invitación"
echo "    4. Un issue con la etiqueta 'good first issue' atrae colaboradores"
echo
echo "  Lo que NO ayuda: publicarlo el mismo día en veinte sitios. Uno bueno,"
echo "  con una explicación honesta de qué resuelve, vale más que veinte"
echo "  enlaces sin contexto."
