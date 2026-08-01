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

"$PY" -m pip install --user --upgrade fibonacci-agent 2>/dev/null \
  || "$PY" -m pip install --user --upgrade --break-system-packages fibonacci-agent

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
