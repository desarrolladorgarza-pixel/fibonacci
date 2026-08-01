# Changelog

## [0.7.0] — 2026-08-01

Compuerta de publicación obligatoria y descubribilidad.

### Corregido

Todo lo de esta sección salió de ejercitar código que "se veía bien" para
pasar la compuerta. Es la quinta vez que ocurre en este proyecto; ver
`CODEX.md` §5.

La mayoría no salió de escribir pruebas, sino de **usar el producto**:
instalarlo desde el paquete y manejarlo con el binario `fib`, operar un
servidor SSH real, y disparar contra el agente las malformaciones que produce
un modelo de verdad. Ninguno de esos era visible desde la suite.

- **El undo remoto mentía.** `fib undo` sobre un `remote.write` respondía OK,
  el journal marcaba la acción como revertida… y el archivo del servidor
  seguía cambiado. El undoer devolvía `"fallo al restaurar"` como texto de
  retorno, y `Journal._undo` toma cualquier retorno por éxito: solo una
  excepción deja la acción intacta. En un producto cuyo argumento entero es
  «puedes deshacerlo», un undo que miente es el peor fallo posible. Ahora
  lanza, la acción se queda en `applied`, y `fib undo` dice que no pudo.
- **La copia a servidores estaba rota en todos los hosts.** `scp` quiere el
  puerto en `-P` y `ssh` en `-p`; se resolvía filtrando `"-p"` de
  `ssh_args()`, lo que quitaba el flag **pero dejaba el número suelto**, y scp
  lo tomaba por un archivo de origen (`stat local "22"`). Como `ssh_args()`
  siempre incluye el puerto, `write()` y `fetch()` fallaban siempre — y
  `fetch()` es la copia previa de la que depende el undo remoto, así que la
  reversibilidad remota que promete el README nunca funcionó. El mismo error
  estaba duplicado en el undoer. Ahora hay un `scp_args()` único, y un
  respaldo con `-O` para servidores que no exponen SFTP (dropbear, busybox o
  cualquiera con el subsistema quitado), donde el `scp` moderno falla entero.

- **No había forma de instalarlo.** `pip install fibonacci-agent` —la primera
  línea del README y lo que ejecutan `install.sh` e `install.ps1`— falla: el
  nombre no está publicado en PyPI. Los instaladores intentan PyPI primero y
  caen al repo de GitHub si no está, así que funcionan antes y después de
  publicar; el README dice cuál es la situación real.
- **`fib doctor` moría con una traza de Rust si `cryptography` estaba rota.**
  Es una dependencia *opcional*, pero `aes_available()` solo capturaba
  `ImportError`, y una instalación inconsistente —mezclar el paquete del
  sistema con otra versión de Python— lanza un pánico de pyo3 que hereda de
  `BaseException` y atraviesa cualquier `except Exception`. Moría el primer
  comando que el README manda ejecutar. Ahora degrada al cifrado de respaldo,
  que es lo que significa "opcional".
- **`fib doctor` devolvía 1 siempre, para todo el mundo.** Sumaba al código de
  salida cada capacidad sin modelo, y `transcribe` no está cubierta por
  *ningún* perfil: en una instalación perfectamente sana el código era 1. Así
  no distinguía nada, y `fib doctor && fib "..."` nunca continuaba. Ahora solo
  falla lo que es un fallo: que no responda ningún proveedor.
- **`fib api add --readonly` listaba las herramientas que NO adjuntó.** Decía
  "1 herramientas" y a renglón seguido enseñaba las tres, `DELETE` incluido:
  la cuenta era correcta y la lista mentía. En un producto cuyo argumento es
  que sabes qué puede hacer el agente, ese es de los peores sitios donde
  mentir.
- **`fib scope add "/etc/**" libre` respondía con un ✓.** El bloqueo de núcleo seguía
  ganando —la garantía nunca estuvo rota— pero la salida daba a entender lo
  contrario, y quien la creyera se quedaría pensando que acaba de abrir
  `/etc`. Ahora avisa de que sigue bloqueado y por qué.
- **`fib sync import` con la contraseña mal escribía una traza de excepción.**
  Teclearla mal es el camino habitual, no el raro.

- **Cuatro formas de tumbar el agente desde el modelo.** La salida de un LLM
  es entrada NO confiable, y se trataba como si siempre viniera bien formada:
  `content` que no es texto, `arguments` que no son un objeto (`"notas.txt"` o
  `[1,2,3]` donde iba un dict), un `function` que es una cadena y no un
  objeto — cada uno de esos producía un `AttributeError` que salía de
  `agent.chat()` y se llevaba el turno entero. Con un modelo local pequeño
  esto no es hipotético: es el martes. La normalización vive ahora en el
  proveedor, que es la frontera donde la respuesta del modelo se vuelve un
  objeto tipado, y hay 44 pruebas que disparan malformaciones reales contra
  el agente (`tests/test_modelo_adverso.py`).
- **Tres mensajes distintos para "no hay backend de pantalla", dos sin
  arreglo.** `click` decía qué instalar, `type_text` soltaba un escueto "sin
  backend de entrada" y `scroll` culpaba a la plataforma —"no soportado en
  esta plataforma"— cuando lo único que faltaba era `xdotool`. En un servidor
  sin GUI, que es donde más se despliega esto, el usuario concluía que su
  sistema no podía en vez de instalar un paquete.
- **El contrato de extensión publicado no servía.** `surfaces/base.py`
  documenta cómo escribir una superficie nueva —el README lo vende como "un
  archivo, no un parche al gateway"— y declaraba sus propios
  `Inbound`/`Outbound`/`Surface`, distintos de los que usa el runtime y sin el
  campo `display` que `SurfaceRunner` lee. Nadie lo importaba: era código
  muerto y, peor, una trampa para quien siguiera la documentación. Ahora
  re-exporta las definiciones de verdad.
- **`fib` moría con un mensaje directo de más de dos palabras.** El positional
  `message` y los subparsers competían por los mismos tokens y argparse los
  repartía: `fib arregla mis descargas` le daba dos palabras a `message` y
  mandaba la tercera al subparser, que abortaba con `invalid choice`. Solo
  funcionaba el mensaje de una palabra o entrecomillado — es decir, la forma
  de uso más obvia del programa estaba rota. El despacho ya decide antes de
  parsear, así que en el camino de mensaje directo no se registra ni un
  subparser.
- **`fib mcp server` reventaba con `NameError`.** Una lista muerta invocaba
  `ForgedTool`, que no estaba importado en `cli.py`; el bucle que sí hacía el
  trabajo venía justo debajo y nunca llegaba a ejecutarse.
- **`advance()` emitía dos veces el último paso.** El `yield` final repetía el
  cursor ya entregado solo para anunciar el estado `DONE`: quien persistía en
  cada iteración —`fib do`, `fib resume`— guardaba dos veces la misma
  posición. Ahora el último paso ya sale con `state=DONE` y `result` puesto.
- **Una spec OpenAPI sin `paths` pasaba en silencio si PyYAML estaba
  instalado.** La comprobación vivía dentro del parser propio, así que el
  camino de PyYAML la esquivaba entero y el error aparecía mucho después, sin
  decir por qué. La validación es ahora común a los dos caminos.
- **`pair --revoke` y `vault remove` llamaban dos veces a la operación que
  muta**, una para el texto y otra para el color: el borrado ocurría en la
  primera llamada y la segunda devolvía `False`, así que el éxito se pintaba
  siempre con el color del fallo.
- **Dos pruebas escribían en el home real del usuario.**
  `test_plataforma_se_detecta_y_da_rutas` y su gemela de aceptación llamaban a
  `data_dir()` y `config_dir()` importadas como símbolo: `isolate` parchea el
  módulo, pero un `from ... import` copia la referencia y la esquiva. Como esas
  funciones crean el directorio si falta, correr `pytest` dejaba
  `~/.config/fibonacci` y `~/.local/share/fibonacci` en la máquina de
  cualquiera — justo lo que `CODEX.md` §6 pone como criterio de aceptación. Lo
  destapó el job de CI sin red, que corre como un usuario sin permiso ahí.
- **El aislamiento del CLI en las pruebas era una casualidad.** `cli.CONFIG`
  se resolvía al importar el módulo, y solo apuntaba al directorio temporal
  porque el primer import ocurría dentro de una prueba; cualquier import a
  nivel de módulo lo habría apuntado al home real del usuario. La ruta se
  resuelve ahora al usarla —como ya hacían `hosts.json` y `apis.json`— y
  `conftest.py` parchea también `cli`, `mcp` y `subagents`.
- **Windows corrompía todo texto no ASCII fuera del área de trabajo.**
  `config.json`, `hosts.json`, `apis.json`, los metadatos de la forja, la
  identidad y el id de dispositivo se leían y escribían sin declarar
  codificación, así que en Windows salían en cp1252. En un proyecto cuyos
  datos son español —notas, nombres, ámbitos— eso significa acentos rotos o
  un `UnicodeEncodeError` al guardar. `tools.py` y la bóveda ya lo hacían
  bien; ahora lo hace todo. Lo destapó el CI: las tres versiones de Python
  sobre `windows-latest` fallaban.
- **El job de CI que comprueba que ninguna prueba usa la red no comprobaba
  nada: se colgaba.** Hacía `iptables -A OUTPUT -j REJECT` a secas, lo que
  también cortaba al agente del runner, que necesita hablar con GitHub para
  reportar. El job se quedaba `in_progress` hasta el tope de seis horas, ni en
  verde ni en rojo, así que la promesa que decía verificar nunca se verificó.
  Ahora el bloqueo se aplica solo al usuario que corre las pruebas
  (`--uid-owner`), el job comprueba primero que el bloqueo existe de verdad
  —si logra salir a internet, falla en vez de dar un verde vacío— y todos los
  jobs tienen `timeout-minutes`.
- **La compuerta de estilo no daba la misma respuesta dos veces.** El
  conjunto de reglas quedaba al criterio de la versión de ruff instalada:
  `preflight.sh` decía "ruff limpio" en local mientras el CI, que instala
  siempre la última, reportaba 121 errores del mismo árbol. Las reglas se
  declaran ahora explícitamente en `pyproject.toml`.
- **Una prueba del programador fallaba en Windows por resolución de reloj.**
  Comparaba dos marcas de tiempo tomadas con microsegundos de diferencia con
  `>`; el reloj de Windows avanza a saltos de ~15 ms, así que caían en el
  mismo tick. Ahora afirma lo que "cada 1h" promete de verdad: que la próxima
  ejecución no retrocede y queda ~1h por delante.
- **El modelo falso incumplía su propio contrato.** `_pseudo_vector`
  prometía que "textos parecidos quedan cerca" pero hasheaba el texto
  completo: dos frases con casi todo el vocabulario en común salían tan
  ortogonales como dos sin relación —a veces con coseno negativo— y
  `Memory.recall` las descartaba. Ninguna prueba podía ejercitar de verdad el
  camino semántico de la memoria. Ahora el vector se compone por palabra.

### Añadido
- **`fib schedule serve --once`**: una pasada y salir, con código de salida
  distinto si alguna tarea falló. `deploy/README.md` la documentaba desde hacía
  versiones con la nota "aún no existe": Termux no tiene systemd y su gestor de
  batería mata los procesos largos, así que allí un demonio no es una opción.
  Sirve igual para `cron` y para el Programador de tareas de Windows.
- **Discord, verificado contra una imitación de su API REST**
  (`tests/discord_fake.py`): que ignore a los bots —incluido él mismo, o se
  responde en bucle gastando presupuesto—, que procese del más viejo al más
  nuevo (la API los devuelve al revés), que avance `after` y que parta por
  debajo del tope de 2000.
- **Servidor MCP**: de 29% a 99% de cobertura. Protocolo, las cuatro
  herramientas, el ida y vuelta `do`→`undo` que es la afirmación distintiva
  del README, y que ninguna llamada mal formada del host tumbe el servidor.
- **Telegram, verificado contra una imitación fiel de su Bot API**
  (`tests/telegram_fake.py`). `TelegramSurface` nunca había hablado con nada,
  y el README lo ofrece como forma de usar el agente desde el teléfono. Se
  cubre lo que de verdad rompe un bot: que el `offset` avance —si no, procesa
  el mismo mensaje en bucle para siempre—, que un `caption` cuente como texto
  y una encuesta no, que los mensajes se partan por debajo del tope de 4096
  respetando las líneas, que un desconocido no llegue al agente, y que lo que
  exige confirmación se rechace diciendo dónde sí puede hacerse. Cobertura de
  `surfaces/live.py`: 49% → 60%.
- **`tests/test_cli.py`**: 60 pruebas sobre `cli.py`, que era el módulo más
  grande del proyecto y el único sin ninguna — un quinto del código, y la
  puerta por la que pasa todo usuario. Cubre despacho, `config`, `scope`,
  `pair`, `memory`, `skills`, `history`, `undo` (incluidos `--all`, `--force`
  y la negativa cuando el archivo cambió después), `tasks`, `schedule`,
  `vault`, `host`, `forge`, `api`, `sync`, `serve`, `doctor` y `do`. Cobertura
  total del proyecto: 68% → 76%.
- **`tests/test_acceptance.py`**: 36 pruebas que verifican que **cada promesa
  del README se cumple en el código**. Es una clase distinta de prueba: las
  unitarias comprueban que una función hace lo que dice; estas comprueban que
  la portada no miente. Si una falla, o el código está roto o el README engaña.
- Encabezado bilingüe con badges. La descripción en inglés no es cosmética:
  la mayoría de las búsquedas de GitHub y Google que llevarían a este proyecto
  se hacen en inglés.
- 38 keywords y 19 clasificadores en `pyproject.toml`; 20 topics de GitHub
  elegidos por volumen de búsqueda real, no por describir el proyecto.
- `docs/DIFUSION.md`: textos listos para Show HN, r/LocalLLaMA, r/selfhosted y
  los `awesome-*`, con lo que **no** hay que hacer.

### Cambiado
- **La suite tarda la mitad.** 425 pruebas en 52 s, frente a 279 en 59 s. Los
  servidores de prueba se apagaban con el `poll_interval` por omisión de
  `serve_forever`, medio segundo que se pagaba en cada prueba que levantara
  uno: 48 de los 101 segundos que llegó a tardar la suite se iban en esperar
  a nada.
- **`preflight.sh` es estricto por defecto.** Publicar es la operación
  irreversible del proyecto: el default debe ser el seguro. `--laxo` queda para
  desarrollo local.
- **`publish.sh` aborta si preflight falla, y no hay bandera para saltárselo.**
  Un repo público con las pruebas en rojo es peor que no publicar. La salida
  honesta cuando algo falta es `--prerelease`.
- El orden en `RELEASE.md` pasa de "recomendado" a obligatorio.


## [0.6.3] — 2026-08-01

### Cambiado
- Autoría actualizada a **Chronoshg** en licencia, NOTICE, metadatos del
  paquete, CITATION, documentación y URLs del repositorio. El identificador
  de launchd pasa a `com.chronoshg.fibonacci`.

Las atribuciones a Nous Research (Hermes Agent) y Cobus Greyling
(loop-engineering) se mantienen sin cambios: son de terceros.


## [0.6.2] — 2026-08-01

Preparación para publicación. Sin cambios funcionales.

### Añadido
- `scripts/preflight.sh`: nueve verificaciones mecánicas —entorno, pruebas,
  cobertura, estilo, consistencia de versión, secretos, documentación,
  construcción e instalación limpia en venv—. Convierte "¿está listo?" en una
  pregunta con respuesta binaria en vez de un juicio.
- `scripts/publish.sh`: crea el repo público, sube, etiqueta y publica el
  release. **Aborta si preflight falla**; esa dependencia es deliberada.
- `RELEASE.md`: brief de publicación con el orden recomendado y lo que no debe
  hacerse.
- `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `CITATION.cff`, plantillas de issue
  y PR, `dependabot.yml`.
- Atajos en el Makefile: `preflight`, `publish`, `install-dev`.

### Cambiado
- `PUBLICAR.md` se reemplaza por `RELEASE.md`, con compuertas en vez de una
  lista de comandos.


## [0.6.1] — 2026-08-01

Andamiaje de pruebas. Sin cambios funcionales; prepara el terreno para cerrar
el hueco de cobertura.

### Añadido
- **`tests/conftest.py`**: `isolate` (autouse) parchea `data_dir`/`config_dir`/
  `workspace` a un tmp por prueba — hasta ahora las pruebas escribían en el
  directorio real del usuario. Y **`fake_model`**, un servidor que habla el
  protocolo OpenAI de verdad (completaciones, tool_calls, embeddings, streaming
  SSE, fallos programables): con él se puede ejercitar el mesh, los proveedores
  y el bucle completo del agente sin red ni GPU.
- `tests/test_agent_loop.py`: 20 pruebas del bucle del agente, antes
  imposibles. Incluye marcas `# TAREA:` en los huecos restantes.
- `CODEX.md`: brief de tareas con prioridades y criterio de aceptación.
- `deploy/`: unidades systemd (con `ProtectSystem=strict` y `ReadWritePaths`
  acotado), plist de launchd y notas para Termux. Se habían perdido en la
  reescritura de la 0.1.0.
- `Makefile`, configuración de pytest/coverage, y un job de CI que corre las
  pruebas **con la salida a internet bloqueada por iptables**, para verificar
  que ninguna depende de red externa.

### Corregido
- Línea muerta en `tools_api.py`: `[m for m in ("{" + s for s in []) ...]`
  iteraba sobre una lista vacía. Residuo de una refactorización.


## [0.6.0] — 2026-08-01

Integración de APIs y primitivas agénticas componibles.

### Añadido — APIs
- **Bóveda de credenciales** (`api.py`): secretos cifrados en disco, atados
  opcionalmente a una allowlist de hosts. El modelo usa el **nombre** de la
  credencial; la sustitución ocurre en el cliente HTTP, después de que ya
  escribió la petición. Aunque una inyección vuelque todo el contexto, el token
  no está ahí.
- **Cliente HTTP completo**: GET/POST/PUT/PATCH/DELETE, cabeceras, cuerpo JSON,
  auth bearer/header/query/basic. Un 5xx devuelve información al agente en vez
  de lanzar una excepción que lo tumbe.
- **Ingesta de OpenAPI**: `fib api add <spec>` genera una herramienta por
  endpoint con su esquema. Apuntas a la spec de tu CRM o backend y queda
  integrado sin escribir código. Las llamadas mutantes a servicios externos se
  registran como irreversibles y piden confirmación.
- CLI: `fib vault add|list|remove`, `fib api add|list`.

### Añadido — Primitivas agénticas
- **Ocho primitivas componibles** (`primitives.py`): Retry (con jitter),
  Fallback, Verify (con reparación), Checkpoint (con rollback real sobre el
  journal), Race, Budget, Gate y Observe. Se encadenan con `>>` y acumulan
  traza.
- **Recetas**: `robust()`, `transactional()`, `resilient()`.
- **Expuestas al modelo** como `flow.retry`, `flow.fallback`, `flow.race`,
  `flow.observe`, `flow.checkpoint`, `flow.rollback`. Operan sobre herramientas
  ya registradas, nunca sobre código arbitrario.

### Por qué las primitivas
Un agente sin ellas improvisa ante el fallo, y su improvisación varía con el
modelo, la temperatura y el día. Con ellas el comportamiento es determinista y
auditable: sabes cuántas veces reintentó, con qué alternativas y qué verificó.

### Corregido
- `resilient()` encadenaba `Retry >> Fallback`, pero la cadena corta al primer
  fallo, así que el Fallback nunca corría. Es un Fallback de Retries, no un
  Retry seguido de Fallback.
- `Chain` perdía la traza de los pasos anteriores: cada primitiva devuelve un
  Outcome nuevo y la historia había que arrastrarla a mano.
- `sync.py` no completaba el merge de tareas (quedó a medias en 0.4.0): ahora
  el export lleva el detalle de pasos y el import reconstruye la tarea entera.
- Código muerto en `forge.py` (`impls`, `aliases`, `_indent` sin uso).
- `Authority._pending` estaba declarado después de los métodos que lo usan, y
  los códigos de emparejamiento vencidos no se purgaban nunca.
- Bloque `try` mal cerrado en `fib doctor`.

### Pruebas
220 (29 + 46 + 36 + 22 + 38 + 49).


## [0.5.0] — 2026-08-01

Superficies vivas, tareas programadas y cifrado auditado. Cierra el roadmap.

### Añadido
- **Superficies de mensajería** (`surfaces/live.py`): Telegram, Discord y un
  webhook genérico, sobre HTTP puro sin SDK. Cada adaptador son ~80 líneas; si
  una API cambia, se arregla un archivo y el núcleo ni se entera.
  `fib serve telegram`.
- **Programador** (`scheduler.py`): tareas recurrentes con horarios en español
  ("diario 07:00", "cada 30m", "lunes 09:30") o cron de 5 campos. Presupuesto
  por ejecución, registro en el journal, entrega a superficie, y auto-apagado
  tras 5 fallos consecutivos. `fib schedule add|serve`.
- **AES-256-GCM real** (`crypto.py`) con `pip install fibonacci-agent[crypto]`.
  Sin el extra, cae a PBKDF2-600k + keystream SHA-256 + HMAC, y el archivo
  **declara cuál se usó**. Lee los paquetes de la 0.4.0 sin migración.

### Diseño
- **Nadie sin emparejar ejecuta nada.** Un desconocido que escribe al bot
  recibe una respuesta cortés y ni siquiera llega al agente. Exponer un agente
  con shell a un chat sin esto no es una funcionalidad, es una brecha.
- **Confirmación remota se rechaza, no se asume.** Por chat nadie puede dar un
  sí informado: lo que exigiría confirmación se salta y se explica, con el
  aviso de hacerlo desde la terminal. Igual en tareas programadas.
- El cifrado débil no se llama "cifrado" a secas: se avisa en el log, en el
  archivo y en `fib doctor`.

### Corregido
- Import relativo incorrecto en `surfaces/live.py` (un nivel de paquete).

### Pruebas
171 (29 + 46 + 36 + 22 + 38).


## [0.4.0] — 2026-08-01

Autoconstrucción, streaming y sincronización real entre dispositivos.

### Añadido
- **La Forja** (`forge.py`): Fibonacci genera sus propias herramientas y
  servidores MCP. El flujo es cuarentena → análisis estático (AST) → prueba en
  subproceso aislado con red bloqueada y timeout → promoción con confirmación.
  Una herramienta autogenerada pasa por el Gate, el journal y la redacción como
  cualquier otra; y si muta sin declarar undo, no se instala. Puede empaquetar
  varias herramientas probadas como un **servidor MCP autónomo** que otros
  agentes consumen: Fibonacci deja de ser solo cliente MCP y pasa a proveedor.
- **Streaming** (`chat_stream`): respuestas token por token en la sesión
  interactiva. Si la intención requiere herramientas, cede al modo normal —el
  valor del streaming es la conversación, no ver acciones a medio ejecutar.
- **Sincronización entre dispositivos** (`sync.py`): export/import a un archivo
  cifrable, merge por semántica (notas con dedup, skills gana la más probada,
  journal append-only, tareas gana el estado más avanzado), y sync por carpeta
  compartida (Syncthing/Nextcloud/USB) sin servidor central. Cumple lo que la
  0.2.0 prometía y no hacía.
- CLI: `fib forge new|server`, `fib sync export|import|folder`.

### Diseño
- La autoconstrucción NO es autonomía para saltarse las reglas: el análisis
  estático rechaza `eval`/`ctypes`/`pickle` antes de ejecutar, la red se
  bloquea en la prueba salvo declaración explícita, y todo pasa por
  confirmación del dueño antes de instalarse.
- El cifrado de sync es ofuscación honesta (PBKDF2 + keystream SHA-256 + HMAC),
  no AES. Documentado como tal: para cifrado serio, usa age/gpg por fuera.

### Corregido
- Indentación del harness de prueba de la Forja (dedent desalineaba el código
  inyectado) y del dispatch de los servidores MCP generados.

### Pruebas
133 (29 + 46 + 36 + 22).


## [0.3.0] — 2026-08-01

Control de equipo y máquinas remotas, con el modelo de autorización que lo
hace desplegable.

### Añadido
- **Visión y control de pantalla** (`control.py`): captura nativa por SO,
  clic, teclado, scroll. Backends: screencapture/osascript en macOS, SendKeys
  en Windows, grim/scrot + xdotool/ydotool en Linux, termux-api en Android.
- **Control remoto** por SSH/SFTP sin dependencias. Las escrituras remotas son
  reversibles vía copia previa; los comandos no y se declaran así.
- **Identidad y ámbitos** (`identity.py`): principals con niveles de confianza,
  emparejamiento por código de un solo uso, y ámbitos ALLOW/CONFIRM/DENY.
  Dentro de un ámbito libre el agente **no pregunta nada**.
- **Subagentes en paralelo** (`subagents.py`) con journal compartido —para que
  `undo --all` revierta el árbol completo—, contaminación aislada y reparto de
  presupuesto.
- CLI: `fib pair`, `fib scope`, `fib host`, `fib delega`.

### Diseño
- Las acciones de GUI **no fingen ser reversibles**. Se marcan irreversibles y
  guardan captura antes y después como registro forense.
- Ventanas sensibles (banca, correo, gestores de contraseñas, consolas cloud,
  terminales root) exigen confirmación sin importar la configuración.
- `CORE_DENY` no se puede anular por configuración.

### Corregido
- `_mutates_shell` no detectaba redirecciones: `>` no es carácter de palabra,
  así que `\b>` nunca coincidía. `echo x > /etc/hosts` pasaba como comando
  inocuo en hosts readonly.
- Incoherencia de confianza: un invitado necesitaba confirmación para leer
  dentro de un ámbito declarado pero leía libre fuera de él, al revés de lo
  correcto.
- El despacho del CLI: un positional `nargs="*"` se tragaba el nombre del
  subcomando.

### Pruebas
111 (29 + 46 + 36).

## [0.2.0] — 2026-08-01

Auditoría arquitectónica de la 0.1.0 y corrección de todo lo encontrado.
Cuatro de los hallazgos eran bugs reales, no features faltantes.

### Corregido (P0)

- **El undo podía destruir trabajo más nuevo, en silencio.** No verificaba si
  el archivo había cambiado tras la acción. Ahora guarda hash de verificación
  y se niega a revertir sobre un cambio posterior; `--force` procede tras
  avisar. `undo --all` se detiene ante el primer conflicto.
- **El ciclo de skills era código muerto.** `score_skill()` no se llamaba en
  producción, así que ninguna skill podía pasar de `candidate` — y las
  candidatas no entran al prompt. Ahora hay ventana de veredicto: si en el
  turno siguiente el usuario deshace, corrige o repite, la skill pierde; si
  continúa, gana. `fib undo` penaliza de inmediato.
- **SQLite sin WAL ni `busy_timeout`, con conexión compartida entre hilos.**
  Producía `database is locked` al correr CLI y servidor MCP a la vez, y
  corrupción latente. Nueva capa `store.py`: WAL, timeout, **una conexión por
  hilo**, escrituras serializadas. Verificado con 7 hilos concurrentes.
- **El presupuesto de contexto era decorativo.** Los resultados de
  herramientas lo esquivaban por completo. Ahora el truncado se deriva del
  presupuesto real del turno.

### Añadido (P1 — seguridad)

- **Redacción de secretos** antes de que nada entre al contexto: 12 familias
  de patrones más detección por entropía en archivos de configuración.
- **Mitigación de inyección de prompt** por control de flujo de salida:
  marcado de contenido externo, detección de patrones, y bloqueo de red
  cuando el turno leyó datos sensibles.
- **Presupuesto de gasto** por turno en dinero, tokens y segundos.

### Añadido (P2)

- **Migraciones de esquema** con `PRAGMA user_version` en las tres bases.
  Rechaza bases más nuevas que el código en vez de degradarlas.
- **Trazas de razonamiento**: `fib history --trace` responde *por qué* el
  agente hizo algo, no solo *qué* hizo.
- `fib undo --force`, `/traza` y `/gasto` en la sesión interactiva.
- `SECURITY.md` con el modelo de amenazas y sus límites explícitos.

### Pruebas
75 (29 originales + 46 de regresión sobre los hallazgos).

---

## [0.1.0] — 2026-08-01

Primera versión. Journal reversible, memoria con decaimiento y detección de
contradicciones, skills con período de prueba, tareas durables, Model Mesh por
capacidad, servidor MCP, soporte multiplataforma.
