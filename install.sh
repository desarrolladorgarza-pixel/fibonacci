#!/usr/bin/env bash
# Fibonacci — Linux, macOS, WSL2, Termux, BSD
set -euo pipefail
echo "▸ Fibonacci"

PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys;exit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "  ✗ Se requiere Python 3.11+."
  if [ -n "${PREFIX:-}" ] && [[ "$PREFIX" == *com.termux* ]]; then
    echo "    Termux:  pkg install python"
  else
    echo "    macOS:   brew install python@3.12"
    echo "    Debian:  sudo apt install python3.12"
  fi
  exit 1
fi
echo "  ✓ $PY"

# Fibonacci todavía no está en PyPI. Mientras tanto se instala desde el repo,
# que funciona igual de bien; en cuanto exista el paquete, la primera rama
# gana sola y esto no hay que tocarlo.
REPO="git+https://github.com/desarrolladorgarza-pixel/fibonacci@main"

instalar() {   # $1: destino (fibonacci-agent | git+...)
  "$PY" -m pip install --user --upgrade "$1" 2>/dev/null \
    || "$PY" -m pip install --user --upgrade --break-system-packages "$1"
}

if instalar fibonacci-agent; then
  echo "  ✓ instalado desde PyPI"
elif command -v git >/dev/null 2>&1 && instalar "$REPO"; then
  echo "  ✓ instalado desde GitHub (aún no publicado en PyPI)"
else
  echo "  ✗ No pude instalar Fibonacci."
  echo "    Necesitas git, o espera a que el paquete esté en PyPI."
  exit 1
fi

BIN="$("$PY" -c 'import site,os;print(os.path.join(site.USER_BASE,"bin"))')"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) for rc in ~/.bashrc ~/.zshrc; do
       [ -f "$rc" ] && ! grep -q "$BIN" "$rc" && echo "export PATH=\"$BIN:\$PATH\"" >> "$rc"
     done
     export PATH="$BIN:$PATH"
     echo "  ✓ PATH actualizado (reinicia la shell)" ;;
esac

if ! command -v ollama >/dev/null 2>&1; then
  echo
  echo "  Para uso 100% local (opcional):"
  echo "    curl -fsSL https://ollama.com/install.sh | sh"
  echo "    ollama pull qwen3:8b && ollama pull bge-m3"
  echo "    fib config mode local"
fi

echo
"$BIN/fib" doctor 2>/dev/null || fib doctor || true
echo
echo "  Listo:  fib"
