<div align="center">

# FIBONACCI

**The agent you can undo.**
*El agente que puedes deshacer.*

[![CI](https://github.com/desarrolladorgarza-pixel/fibonacci/actions/workflows/ci.yml/badge.svg)](https://github.com/desarrolladorgarza-pixel/fibonacci/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Zero dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](pyproject.toml)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows%20%7C%20android-lightgrey.svg)](#multiplataforma-de-verdad)

</div>

---

> **In English.** Fibonacci is a general-purpose personal AI agent whose core
> premise is **reversibility**: every mutation is journaled together with its
> inverse operation *before* it is applied, so `fib undo` can roll it back —
> with integrity checks that refuse to revert over newer work.
>
> Every other agent defends *before* the fact: command approval, container
> isolation, allowlists. All of that degrades with use — after thirty
> confirmation prompts you start clicking yes without reading. Fibonacci adds
> the defense that was missing: **after** the fact.
>
> It also brings memory with **temporal decay and contradiction detection**,
> skills that must **earn promotion** through measured success, prompt-injection
> mitigation by **information-flow control** (not by asking the model nicely),
> a credential vault the model never sees, composable **agentic primitives**
> (retry, fallback, verify, checkpoint, race, budget, gate, observe), OpenAPI
> ingestion, and an MCP server it can generate itself.
>
> Runs on Linux, macOS, Windows (native), Android/Termux and BSD, on x86_64 and
> arm64, with **zero runtime dependencies**. Works fully offline with Ollama,
> llama.cpp or LM Studio — or with Anthropic, OpenRouter and DeepSeek.
>
> Inspired by [Hermes Agent](https://github.com/NousResearch/hermes-agent)
> (Nous Research, MIT). Independent implementation, not a fork.
>
> **Documentation below is in Spanish.** English docs are on the roadmap;
> the code, CLI and error messages are self-explanatory, and every module has
> a docstring explaining the design decisions.

---

Autor: **Chronoshg** · [desarrolladorgarza-pixel](https://github.com/desarrolladorgarza-pixel) · MIT · v0.7.0 · Linux · macOS · Windows · Android · BSD

```bash
pip install git+https://github.com/desarrolladorgarza-pixel/fibonacci
fib doctor
fib "ordena mi carpeta de descargas por tipo de archivo"
fib undo          # si no te gustó
```

> Todavía no está publicado en PyPI, así que `pip install fibonacci-agent` aún
> no funciona. Se instala desde el repo mientras tanto.

---

## El problema

[Hermes Agent](https://github.com/NousResearch/hermes-agent) demostró algo
importante: un agente personal con loop de aprendizaje cerrado, que vive donde
tú vives y funciona con cualquier modelo. Fibonacci parte de ahí.

Pero todos los agentes actuales —Hermes incluido— comparten un hueco. Sus
defensas son **preventivas**: aprobación de comandos, aislamiento en
contenedor, allowlists. Todas actúan *antes*. Cuando el agente hace algo mal
—y a veces lo hará— no hay vuelta atrás.

Y hay un costo escondido: la aprobación por comando cansa. Después de treinta
"¿autorizas esto?" empiezas a decir que sí sin leer. La defensa preventiva se
degrada exactamente en la medida en que la usas.

## La respuesta de Fibonacci

**Cada mutación se registra con su operación inversa antes de aplicarse.**

```bash
fib "reorganiza el proyecto: mueve los tests a tests/ y actualiza los imports"
  ⚙ file.list({"pattern":"**/*.py"})
  ⚙ file.move({"src":"test_api.py","dst":"tests/test_api.py"})
  ⚙ file.write({"path":"tests/test_api.py",...})
  ...

fib history
  ↶ 07-31 14:22  file.move     {"src":"test_api.py",...}
  ↶ 07-31 14:22  file.write    {"path":"tests/test_api.py",...}
  cobertura_undo: 100%

fib undo --all        # ↶ toda la sesión, en orden inverso
```

No es un `git checkout` con otro nombre: funciona fuera de repositorios, en
cualquier carpeta y con cualquier herramienta que declare su inverso.

**El undo verifica antes de actuar.** Guarda el hash de lo que dejó; si el
archivo cambió después —lo editaste tú, otro proceso lo tocó— se niega y te
dice por qué. Un undo que destruye trabajo más nuevo en silencio sería peor
que no tener undo.

```
fib undo
  file.write: 'config.yml' fue modificado después de esta acción.
  Revertir borraría ese cambio. Usa --force si aun así lo quieres.
```

El cambio no es técnico, es de postura. **Un agente reversible es un agente al
que puedes dejar trabajar solo.** El undo no depende de tu atención en el
momento crítico.

### La restricción que lo sostiene

```python
box.register(
    ToolSpec("db.wipe", "borra todo", schema, mutating=True),
    lambda: "listo",
)
# ValueError: 'db.wipe' es mutante y reversible pero no declara undo.
```

No es una convención en la documentación: es una excepción en tiempo de
registro. Quien contribuya una herramienta nueva se topa con "¿y esto cómo se
revierte?" antes de poder integrarla — el momento correcto para preguntarlo.

Lo genuinamente irreversible (shell, envíos) se marca `reversible=False` y
**siempre** pide confirmación. La ignorancia sobre reversibilidad nunca es
silenciosa.

---

## Qué más cambia respecto a Hermes

| En Hermes | En Fibonacci |
|---|---|
| Defensa preventiva: aprobación, contenedores, allowlists | Lo mismo **más** journal reversible. `fib undo`. |
| Memoria curada por el agente, modelo del usuario entre sesiones | Notas con **vida media**: lo de hace un año pesa menos que lo de ayer. Y las contradicciones se **marcan** en vez de coexistir en silencio. |
| Crea skills tras tareas complejas y las mejora durante el uso | La skill nace *candidata*, se prueba en *sombra*, se activa solo con ≥70% de éxito. Una que empeora **se retira sola**. |
| Elige modelo con `hermes model` | Enrutamiento por **capacidad**: el código pide `REASONING`, el mesh elige y degrada con circuit breaker si no responde. |
| `/compress` cuando ya te quedaste sin ventana | **Presupuesto de contexto** proactivo: reserva la salida y reparte lo demás por prioridad. Nunca choca con el techo. |
| Gateway con 5 SDKs de mensajería dentro del proceso | Superficie = adaptador de 4 métodos. El núcleo no sabe que existen. |
| Conversación portátil entre plataformas | El **trabajo** también: `fib do` crea una tarea durable; la reanudas desde otro dispositivo. |
| Instalador por SO, Git Bash embebido en Windows | Toda la variación de plataforma en un archivo de ~200 líneas. |

### Lo que Fibonacci deliberadamente no trae

TUI completo, siete backends de terminal, gateways nativos de
WhatsApp/Signal/Discord, transcripción de notas de voz, Nous Portal. Hermes es
mejor en eso y es MIT: si lo que quieres es amplitud de superficies, úsalo.
Fibonacci elige un núcleo pequeño y auditable.

---

## Memoria que envejece

```bash
fib memory list
  [person    ]  95%  se llama Héctor, en Guadalajara
  [project   ]  88%  migrando VIGIA a Postgres esta semana
  [fact      ]  31%  trabajaba en Acme          ← decayó, ya no pesa

fib memory conflicts
  A) el proyecto principal usa PostgreSQL     72% · hace 3 días
  B) el proyecto principal usa MongoDB        90% · hoy
  resolver: fib memory keep note_xxx --drop note_yyy
```

Vida media por tipo de dato: tu nombre no caduca (`half_life_days=0`), tu
empleo caduca en un año, en qué trabajas esta semana caduca en un mes. Un
agente que recuerda todo para siempre termina recordando puras cosas falsas.

## Skills con período de prueba

```
candidata ──(3 pruebas)──▶ sombra ──(8 pruebas, ≥70%)──▶ activa
                                                            │
                              retirada ◀──(<40% en ≥6)──────┘
```

```bash
fib skills
  [active   ] respaldo-nocturno      9/11 (82%)
  [shadow   ] ordenar-descargas      4/5  (80%)
  [candidate] limpiar-ramas          1/2  (50%)
  [retired  ] deploy-rapido          2/9  (22%)
```

Una candidata **nunca** entra a un prompt real. Esto ataca el riesgo silencioso
del auto-aprendizaje: una skill mala degrada todas las ejecuciones futuras y
nada lo señala.

## Trabajo durable

```bash
fib do "migra el blog de Wordpress a Markdown y súbelo a Netlify"
  task_a1b2c3  · 6 pasos
  ▸ 1/6: exportar el XML de Wordpress
  ▸ 2/6: convertir posts a Markdown
  ^C                                  # cierras la laptop

# desde el teléfono, por SSH o Termux
fib tasks --pending
  task_a1b2c3  running  [2/6]  migra el blog de Wordpress...
fib resume task_a1b2c3
```

El trabajo es un objeto persistido, no un hilo en memoria.

**Límite honesto:** el estado vive en SQLite local. Para retomarlo en otro
dispositivo hay que sincronizar a mano (`fib sync`, más abajo) o entrar por SSH
o Termux a la misma máquina. No hay servidor ni replicación en vivo: si editas
la misma tarea en dos aparatos sin sincronizar entre medias, gana la que esté
más avanzada cuando por fin se encuentren.

## Multiplataforma de verdad

```bash
fib doctor
  plataforma  : macos/arm64
  shell       : /bin/zsh
  config      : ~/Library/Application Support/fibonacci
```

Toda la variación de sistema vive en `platform.py`: rutas según la convención
de cada SO, PowerShell nativo en Windows sin WSL, `termux-notification` en
Android, degradación limpia donde algo no existe. Portar a un sistema nuevo es
implementar ese archivo y nada más.

Cero dependencias externas — solo stdlib. Instala en Termux y en aarch64 sin
compilar nada.

## Modelos

```bash
fib config mode local      # nada sale de tu red; si no hay modelo local, FALLA
fib config mode hybrid     # prefiere local, nube como respaldo
```

Local: Ollama, llama.cpp, LM Studio, vLLM, SGLang.
Nube: Anthropic, OpenRouter, DeepSeek, o tu endpoint OpenAI-compatible.

En modo `local` el sistema no degrada a la nube en silencio: falla y te dice
por qué. Una fuga de datos no debería depender de que alguien recuerde
configurarlo bien.

## Seguridad

Tres protecciones que se activan solas:

**Redacción de secretos.** Todo lo que sale de una herramienta se limpia antes
de entrar al contexto: llaves de AWS/OpenAI/Anthropic/GitHub/Google/Slack, JWT,
PEM, cadenas de conexión, variables `*_KEY`/`*_TOKEN`/`*_SECRET`. En modo
`hybrid` esto es lo que evita que leer un `.env` mande tus llaves a la nube.

**Control de exfiltración.** Si un turno lee un archivo sensible, cualquier
salida a red queda bloqueada en ese turno. Si procesó contenido web, salir
hacia un destino nuevo pide confirmación. El contenido externo se envuelve
declarándolo datos, no instrucciones.

No pretendemos resolver la inyección de prompt con prompts —eso no funciona—.
La defensa es control de flujo: vigilar el eslabón de salida, que es donde una
inyección se vuelve daño real.

**Presupuesto duro.** Techo por turno en dinero, tokens y segundos.

`SECURITY.md` documenta el modelo de amenazas completo, incluyendo lo que
Fibonacci **no** protege. Léelo antes de dejarlo sin supervisión.

## La Forja: Fibonacci se construye a sí mismo

Cuando le falta una capacidad, la fabrica.

```bash
fib forge new "consultar el precio de una cripto desde CoinGecko"
  Generando herramienta...
  generada: precio-cripto (muta=False)
  probando en aislamiento...
  ✓ probada: firma válida (requiere args)
  ✓ instalada como forged.precio-cripto
```

No es "el modelo escribe código y lo ejecuta". Cada herramienta pasa por:
cuarentena → análisis estático (rechaza `eval`, `ctypes`, `pickle`) → prueba en
un subproceso aislado con la red cortada y timeout de 15s → promoción **con tu
confirmación**. Desde que se instala, pasa por el Gate, el journal y la
redacción como cualquier herramienta nativa. Si muta y no declara su `undo`, no
se instala — la misma regla que aplica al código propio de Fibonacci.

Y puede empaquetar lo que construye como un **servidor MCP autónomo**:

```bash
fib forge server --name mis-utilidades
  ✓ servidor MCP: ~/.local/share/fibonacci/forge/servers/mis-utilidades.py
  regístralo:  claude mcp add mis-utilidades -- python3 ...
```

Fibonacci deja de ser solo cliente de MCP: genera el protocolo que otros
agentes consumen.

## Sincronización entre dispositivos

Sin servidor central. Sin cuenta en ningún lado.

```bash
# en la laptop
fib sync export ~/Dropbox/fib.sync -p mi-clave

# en el teléfono (Termux)
fib sync import ~/Dropbox/fib.sync -p mi-clave

# o automático contra una carpeta compartida
fib sync folder ~/Syncthing/fibonacci -p mi-clave
```

El merge respeta la semántica de cada cosa: las notas se unen sin duplicar, una
skill madurada en un dispositivo no reinicia su historial al llegar a otro, el
journal es append-only, y una tarea gana por estado más avanzado. Ahora sí
"reanuda desde el teléfono" es verdad, no una promesa.

## Vive donde tú vives

```bash
export TELEGRAM_BOT_TOKEN=...
fib serve telegram
```

Telegram, Discord y un webhook genérico (Slack, Matrix, n8n, lo que sea).
Sobre HTTP puro, sin SDK: cada adaptador son ~80 líneas.

**Quien no esté emparejado no ejecuta nada.** Genera un código con `fib pair`
y envíaselo al bot; un desconocido recibe una respuesta cortés y ni siquiera
llega al agente. Y como por chat nadie puede dar un sí informado, lo que
exigiría confirmación se rechaza y te dice que lo hagas desde la terminal.

Con `--session principal` unificas el contexto: empiezas en la terminal y
sigues en el teléfono.

## Tareas programadas

```bash
fib schedule add revisa-prs "revisa mis PRs abiertos y resume" "diario 07:00" \
    --surface telegram --channel 12345 --budget 0.20

fib schedule list
fib schedule serve      # el demonio
```

Horarios en español o cron. Cada ejecución tiene **presupuesto propio** y queda
en el journal: lo que hace una tarea programada es tan reversible y auditable
como lo que haces tú. Una tarea que falla 5 veces seguidas se apaga sola.

Corre desatendida, así que no puede confirmar nada: lo que requeriría tu visto
bueno se omite y se reporta en el resultado. Si quieres que lo haga, declara el
ámbito como libre — de antemano, sabiendo qué concedes.

## APIs: consume cualquiera, o la tuya

```bash
fib vault add github --kind bearer --hosts api.github.com
fib api add https://api.midominio.com/openapi.json --prefix crm --credential crm
```

Apuntas a una spec OpenAPI y Fibonacci gana una herramienta por endpoint, con
su esquema de parámetros. Sin escribir código.

**El modelo nunca ve una credencial.** Usa el *nombre* (`--credential crm`); la
sustitución ocurre en el cliente HTTP, después de que ya escribió la petición.
Aunque una inyección de prompt logre volcar todo su contexto, el token no está
ahí. Y una credencial atada a `--hosts` no puede filtrarse a otro dominio ni
por error del modelo.

Las llamadas que escriben a un servicio externo se marcan **irreversibles** —un
`POST` a la API de un tercero no tiene undo— y piden confirmación.

## Primitivas agénticas

Ocho operaciones componibles para que la robustez sea algo que declaras, no un
accidente del prompt:

```python
from fibonacci import Retry, Verify, Checkpoint

cp = Checkpoint("antes-migracion", agent.journal, "s1")
r = (cp >> Retry(3) >> Verify(migro_bien)).run(hacer_migracion)
if not r.ok:
    cp.rollback()          # revierte todo lo hecho desde el punto
```

`Retry` (con jitter), `Fallback`, `Verify` (con reparación), `Checkpoint`,
`Race`, `Budget`, `Gate` y `Observe`. El modelo también las usa como
herramientas: `flow.retry`, `flow.fallback`, `flow.race`, `flow.observe`,
`flow.checkpoint`, `flow.rollback`.

Operan sobre herramientas ya registradas, nunca sobre código arbitrario.

**Por qué importan:** un agente sin ellas improvisa ante el fallo, y su
improvisación varía con el modelo, la temperatura y el día. Con ellas el
comportamiento ante el error es determinista y auditable — sabes cuántas veces
reintentó, con qué alternativas y qué verificó.

Y una distinción que se confunde seguido: `Retry` repite *lo mismo* esperando
que el mundo cambie; `Fallback` prueba *otra cosa* porque la primera no va a
funcionar nunca. Mezclarlos produce agentes que reintentan cinco veces algo
imposible.

## Servidor MCP

```bash
claude mcp add fibonacci -- python3 -m fibonacci.mcp
```

Expone `fibonacci_do`, `fibonacci_undo`, `fibonacci_history` y
`fibonacci_recall`. Es el único servidor MCP donde el host puede **revertir**
lo que la herramienta hizo.

---

## Instalación

```bash
pip install git+https://github.com/desarrolladorgarza-pixel/fibonacci
```

**Sobre PyPI:** el nombre `fibonacci-agent` aún no está publicado, así que
`pip install fibonacci-agent` falla hoy. Los instaladores de abajo lo intentan
primero y caen al repo si no está, para que funcionen antes y después de la
publicación.

```bash
curl -fsSL https://raw.githubusercontent.com/desarrolladorgarza-pixel/fibonacci/main/install.sh | bash
```

```powershell
irm https://raw.githubusercontent.com/desarrolladorgarza-pixel/fibonacci/main/install.ps1 | iex
```

Requiere Python 3.11+ y un proveedor de modelos. Para empezar 100% local:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3:8b && ollama pull bge-m3
fib config mode local
```

## Como librería

```python
from fibonacci import boot

agent = boot(mode="local")
r = agent.chat("resume los PDFs de ~/informes en un solo markdown", "s1")
print(r.text)

if not me_gusto:
    agent.journal.undo_last("s1")
```

## Estado

v0.7.0 — 339 pruebas (303 unitarias + 36 de aceptación). Cobertura 76%.

Lo que sigue sin cubrir, para que no haya que adivinarlo: `tools_control.py`
(pantalla y SSH) y `mcp.py` al 29%, `surfaces/base.py` en cero y
`surfaces/live.py` al 49%. Está marcado en `CODEX.md`.

Y una advertencia que la cobertura no da: **nada se ha probado contra un
modelo vivo, un bot de Telegram real, una pantalla real ni un servidor por
SSH real.** La cobertura mide qué código se ejecutó, no si el producto
funciona en el mundo.

Cada versión de este proyecto salió de auditar la anterior, y en cada auditoría
aparecieron bugs reales en código que se veía bien. Están en el `CHANGELOG.md`
con nombre y apellido, a propósito.

Siguiente, en orden:

- [ ] Snapshots incrementales para carpetas grandes (hoy copia el archivo entero)
- [ ] Adaptador de Matrix y Slack nativo (el webhook ya los cubre)
- [ ] Gateway websocket de Discord (hoy es polling REST, suficiente para uso
      personal pero con latencia de segundos)

## Contribuir

`CONTRIBUTING.md` tiene lo esencial. La regla que sostiene el proyecto: **una
herramienta que muta el mundo no puede registrarse sin declarar cómo se
deshace** — y eso es una excepción en tiempo de registro, no una convención.

```bash
pip install -e ".[dev,crypto]"
make check        # ruff + las 339 pruebas
make gaps         # dónde falta cobertura
```

Cobertura actual 76%. Lo más flaco es `tools_control.py`, `mcp.py` y
`surfaces/`. `CODEX.md` tiene el mapa de lo que falta y el andamiaje ya montado
(`fake_model` levanta un servidor que habla el protocolo OpenAI de verdad, así
que el bucle completo del agente se prueba sin GPU).

## Créditos

El loop de aprendizaje cerrado, las skills como memoria procedural y los
proveedores intercambiables vienen de **[Hermes Agent](https://github.com/NousResearch/hermes-agent)**
(Nous Research, MIT). Fibonacci es implementación independiente inspirada en
sus patrones; no contiene su código.

MIT © 2026 Chronoshg
