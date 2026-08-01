# Difusión

Textos listos para publicar. Escritos para explicar el problema, no para
vender: este público detecta el marketing a distancia y lo castiga.

## Descripción del repo (GitHub)

> The AI agent you can undo. Local-first personal agent with a reversible
> action journal, decaying memory, prompt-injection defense and zero
> dependencies. Runs offline with Ollama.

## Topics (los 20 que pone `publish.sh`)

`ai-agent` `ai-agents` `agentic-ai` `autonomous-agents` `agent-framework`
`llm` `local-llm` `ollama` `mcp` `model-context-protocol`
`personal-assistant` `cli` `python` `self-hosted` `privacy`
`prompt-injection` `ai-safety` `undo` `offline` `cross-platform`

## Show HN

**Título:** `Show HN: Fibonacci – an AI agent you can undo`

> Every agent with shell access defends the same way: ask before acting.
> The problem is that this degrades with use — after thirty confirmation
> prompts you start clicking yes without reading, and the defense is gone
> exactly when you need it.
>
> Fibonacci adds the other half: every mutation is journaled with its inverse
> operation before being applied, so `fib undo` rolls it back. A tool that
> mutates the world cannot even be *registered* without declaring how to undo
> it — that's a ValueError at registration time, not a convention in the docs.
>
> The undo verifies integrity first: if the file changed after the action (you
> edited it, another process touched it), it refuses and tells you why. An undo
> that silently destroys newer work would be worse than no undo at all.
>
> Zero runtime dependencies, runs offline with Ollama, works on Termux. MIT.
>
> It's inspired by Nous Research's Hermes Agent, which got the learning loop
> right; this is an independent implementation that hardens the parts where a
> mistake costs you something real.

**Sé honesto en los comentarios.** Si preguntan por cobertura de pruebas,
madurez o si lo has usado en producción, di la verdad. En HN una respuesta
honesta sobre una limitación gana más credibilidad que una feature más.

## r/LocalLLaMA

**Título:** `I built an AI agent where every action is reversible - runs fully local, zero dependencies`

Enfoca lo que le importa a ese subreddit: corre 100% local, cero
dependencias, funciona en hardware modesto (`qwen3:8b` en 16 GB), y el modo
`local` **falla** en vez de degradar a la nube en silencio.

## r/selfhosted

Enfoca la soberanía: nada sale de tu red, tres archivos SQLite que puedes
respaldar, sync entre dispositivos sin servidor central, unidades systemd con
`ProtectSystem=strict`.

## awesome-* (PRs)

- `awesome-ai-agents`
- `awesome-mcp-servers` — Fibonacci es cliente **y** generador de servidores MCP
- `awesome-selfhosted`
- `awesome-llm-apps`

Una línea, sin adjetivos:

> **[Fibonacci](https://github.com/desarrolladorgarza-pixel/fibonacci)** — Personal AI agent
> with a reversible action journal (`fib undo`), decaying memory and zero
> dependencies. Runs fully offline.

## Imagen de vista previa social

1280x640, en Settings > General > Social preview. Es la miniatura en X,
LinkedIn, Slack y Discord — sin ella el enlace se ve genérico y pierde clics.

Contenido sugerido: el nombre, la frase "The agent you can undo", y una
terminal mostrando `fib undo` revirtiendo un cambio. La captura real del
comando comunica más que cualquier ilustración.

## Qué no hacer

- Publicarlo el mismo día en veinte sitios. Se nota y se lee como spam.
- Prometer lo que el roadmap marca como pendiente.
- Ocultar que la cobertura de pruebas está incompleta si te preguntan.
- Comparar con Hermes en tono competitivo. Es un proyecto MIT que resolvió
  bien el loop de aprendizaje; la atribución honesta es lo que hace que un
  proyecto nuevo se reciba bien en vez de que lo acusen de rebranding.
