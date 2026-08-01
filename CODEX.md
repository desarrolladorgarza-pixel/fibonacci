# Tareas para Codex

Repo: **Fibonacci** — agente personal reversible. Python 3.11+, cero
dependencias en tiempo de ejecución.

El objetivo de este encargo es **cobertura de pruebas**, no funcionalidad
nueva. **Es prerrequisito de la publicación**: `scripts/publish.sh` aborta si
`preflight.sh` falla, y preflight exige 75% de cobertura y las pruebas de
aceptación en verde. Hay ~1,900 líneas sin una sola prueba, y en este proyecto cada vez que
se ejercitó código de verdad apareció un bug real (ver §5).

---

## 1. Cómo correr

```bash
pip install -e ".[dev,crypto]"
pytest tests/ -q                    # 220 pruebas existentes, todas verdes
pytest tests/ --cov=fibonacci --cov-report=term-missing
ruff check fibonacci/
```

Si algo falla al inicio, **detente y repórtalo**: la línea base está en verde
y una regresión ahí importa más que cualquier prueba nueva.

## 2. Lo que ya tienes montado

`tests/conftest.py` trae el andamiaje. Léelo antes de escribir nada.

**`isolate`** (autouse) — parchea `data_dir`, `config_dir` y `workspace` a un
tmp por prueba. Ninguna prueba puede ensuciar el home real. No hace falta
pedirlo.

**`fake_model`** — un servidor HTTP que habla el protocolo OpenAI de verdad:
completaciones, `tool_calls`, embeddings deterministas, streaming SSE, y
fallos programables. Esto es lo que hace probable todo lo que antes no lo era.

```python
def test_algo(agent, fake_model, workspace):
    fake_model.reply_tool("file.read", {"path": "x.txt"})
    fake_model.reply("el archivo dice hola")
    fake_model.reply_json({"notes": [], "skill": None})   # fase de aprendizaje

    r = agent.chat("¿qué dice x.txt?", "s1")
    assert r.tools_used == ["file.read"]
    assert fake_model.last_system()          # inspecciona lo que se envió
```

API del fake: `.reply(texto)`, `.reply_tool(nombre, args)`, `.reply_json(obj)`,
`.fail(status, times)`, `.hang(seg)`, `.default(texto)`, `.reset()`.
Inspección: `.requests`, `.last_body()`, `.last_system()`, `.last_tools()`,
`.approx_prompt_chars()`.

**Ojo con la fase de aprendizaje.** Tras un turno con ≥2 herramientas o un
mensaje largo, el agente hace una llamada extra para extraer notas y skills.
Encola un `reply_json({"notes": [], "skill": None})` al final o el fake
devolverá su respuesta por defecto y la prueba se confundirá.

Otras fixtures: `mesh`, `agent`, `journal`, `memory`, `toolbox`, `workspace`,
`http_server` (eco programable, para `api.py`).

## 3. Qué falta cubrir, por prioridad

### P0 — `fibonacci/cli.py` (1,011 líneas, cero pruebas)

Es un quinto del proyecto y solo se ha verificado a mano. Con `isolate` ninguna
prueba toca el home real, así que se puede invocar `main([...])` directamente.

- [ ] `doctor`, `config` (hay dos de ejemplo en `test_agent_loop.py`)
- [ ] `scope list|add` — verificar que las `CORE_DENY` no se pueden anular
- [ ] `pair`, `pair --list`, `pair --revoke`
- [ ] `memory list|search|conflicts|keep|prune`
- [ ] `skills`, `history`, `history --trace`, `tasks`
- [ ] `undo`, `undo --all`, `undo --force`
- [ ] `schedule add|list|enable|disable|remove|run|history`
- [ ] `vault add|list|remove` (usa `-p` para no bloquear en `getpass`)
- [ ] `forge list`, `api list`, `host list`
- [ ] **Despacho**: `fib "hola"` (mensaje directo) vs `fib scope list`
      (subcomando). Hubo un bug real ahí: el positional `nargs="*"` se comía
      el nombre del subcomando.

### P1 — `fibonacci/mesh/` (487 líneas)

Hay siete pruebas de ejemplo en `test_agent_loop.py`. Faltan:

- [ ] **Cascada de respaldo**: dos providers, el primero falla, el segundo
      responde. Levanta dos `FakeModelServer`.
- [ ] Modo `hybrid` pone los locales primero aunque la nube tenga mejor
      `priority`.
- [ ] Modo `local` con capacidad sin cobertura local → `ProviderError`, **no**
      degradación silenciosa a la nube. Esta es una garantía de soberanía: si
      se rompe, se rompe la promesa del producto.
- [ ] `min_context` descarta modelos con ventana insuficiente.
- [ ] `AnthropicProvider`: el formato es distinto (system aparte, `input_tokens`
      en vez de `prompt_tokens`, `tool_use` en vez de `tool_calls`). Necesita su
      propio fake o adaptar el existente.
- [ ] `Ledger` acumula costo y tokens correctamente.
- [ ] Cooldown del `CircuitBreaker`: tras el tiempo, el modelo vuelve.

### P1 — `fibonacci/surfaces/live.py` (430 líneas)

Los adaptadores nunca han hablado con las APIs reales. **No las llames**: usa
un servidor falso.

- [ ] `TelegramSurface.receive()` contra un fake de `getUpdates` (verifica el
      manejo de `offset`: si no avanza, el bot procesa el mismo mensaje en
      bucle).
- [ ] `TelegramSurface.send()` parte mensajes >4096 respetando líneas.
- [ ] `DiscordSurface`: ignora mensajes de bots (`author.bot`), avanza `after`.
- [ ] `WebhookSurface`: arranca, recibe POST, responde; rechaza sin el secreto.
- [ ] `SurfaceRunner`: reconexión tras error de red (hoy hace `sleep(5)` y
      sigue; verificar que no muere).

### P2 — Huecos de comportamiento

- [ ] Contaminación (`taint`) se reinicia entre turnos.
- [ ] Presupuesto de contexto recorta historial con ventana pequeña — afirmar
      con `fake_model.approx_prompt_chars()`.
- [ ] `Swarm.solve()` completo con `fake_model`.
- [ ] Un paso fallido deja la tarea en `FAILED` y `fib resume` la retoma.
- [ ] Enrutamiento por intención: "arregla este bug" → `Capability.CODE`.

## 4. Funcionalidad pendiente (solo si sobra tiempo)

- [ ] `fib schedule serve --once` — una pasada y salir. Lo necesita Termux,
      que no tiene systemd y mata procesos largos. Está referenciado en
      `deploy/README.md` como no implementado.
- [ ] Snapshots incrementales: hoy `Journal.snapshot_file` copia el archivo
      entero en cada escritura. Un archivo de 30 MB editado 20 veces son
      600 MB. Guardar diffs cuando el archivo es texto.

## 5. Bugs reales que salieron al ejercitar código

Contexto de por qué este encargo importa. Todos estaban en código que "se veía
bien":

- El **undo destruía trabajo más nuevo** en silencio: no verificaba si el
  archivo había cambiado tras la acción.
- `_mutates_shell` no detectaba redirecciones: `>` no es carácter de palabra,
  así que `\b>` nunca coincidía y `echo x > /etc/hosts` pasaba como inocuo en
  un host `readonly`.
- `resilient()` encadenaba `Retry >> Fallback`, pero la cadena corta al primer
  fallo: **el Fallback nunca corría**.
- `score_skill()` era código muerto — ninguna skill podía pasar de `candidate`,
  y las candidatas nunca entran a un prompt. La feature entera estaba
  desconectada.
- SQLite sin WAL con conexión compartida entre hilos: `database is locked`
  garantizado al correr CLI y servidor MCP a la vez.

## 5b. Pruebas de aceptación

`tests/test_acceptance.py` verifica que **cada promesa del README se cumple en
el código**. Es una clase distinta de prueba: las unitarias comprueban que una
función hace lo que dice; estas comprueban que la portada no miente.

Corren solas dentro de la suite y aparte con `pytest -m acceptance`.

Si añades una promesa al README, añade su prueba. Si una falla y no puedes
arreglar el código, **quita la promesa** — no ajustes la aserción.

## 6. Criterio de aceptación

- Las 220 pruebas existentes siguen verdes.
- `pytest -m acceptance` en verde: el README no promete nada que el código no
  haga.
- `pytest --cov=fibonacci` ≥ **75%** de líneas (hoy ~60%, con los módulos
  grandes en cero).
- `ruff check fibonacci/ tests/` limpio.
- Ninguna prueba toca red externa, el home real, ni requiere un LLM.
- Las pruebas corren en <60s en total.

## 7. Convenciones

- Nombres de prueba en español, descriptivos: `test_undo_se_niega_si_el_archivo_cambio_despues`.
- Una aserción conceptual por prueba.
- Cuando una prueba cubra un bug conocido, dilo en el docstring: `"""El fallo
  más grave de la v0.1.0."""`
- Si encuentras un bug: **arréglalo y añade la prueba que lo cubre**, y anótalo
  en el PR. No lo silencies ajustando la aserción a lo que hace el código — eso
  es exactamente cómo se pierde un bug de verdad.
