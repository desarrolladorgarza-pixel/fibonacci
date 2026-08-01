# Despliegue

## systemd (Linux)

```bash
sudo cp fibonacci-*.service /etc/systemd/system/
echo 'TELEGRAM_BOT_TOKEN=...' > ~/.config/fibonacci/surface.env
chmod 600 ~/.config/fibonacci/surface.env

sudo systemctl enable --now fibonacci-schedule@$USER
sudo systemctl enable --now fibonacci-surface@$USER
journalctl -u fibonacci-schedule@$USER -f
```

Las unidades usan `ProtectSystem=strict` y `ProtectHome=read-only` con
`ReadWritePaths` acotado. Es defensa en profundidad: aunque el Gate falle, el
proceso no puede escribir fuera de su workspace y su directorio de datos.

## macOS (launchd)

```bash
cp com.chronoshg.fibonacci.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.chronoshg.fibonacci.plist
```

## Termux (Android)

Termux no tiene systemd y el gestor de batería mata procesos en segundo plano.
Usa `termux-job-scheduler` y desactiva la optimización de batería para Termux:

```bash
pkg install termux-services termux-api
termux-job-scheduler --script ~/bin/fib-tick.sh --period-ms 900000
```

donde `fib-tick.sh` es `#!/data/data/com.termux/files/usr/bin/bash` + `fib schedule serve --once`.

> Nota: `--once` aún no existe. Es una de las tareas abiertas en `CODEX.md`.
