# Modelo de amenazas

Fibonacci ejecuta acciones reales en tu máquina. Este documento dice qué
protege, qué no, y por qué. Preferimos que sepas los límites a que los
descubras.

## Lo que Fibonacci protege

### 1. Errores del agente → reversibles
Cada mutación se registra con su inverso. `fib undo` revierte.

Desde 0.2.0 el undo **verifica integridad**: guarda el hash de lo que dejó y,
al revertir, compara. Si el archivo cambió después (lo editaste tú, otro
proceso lo tocó), el undo se niega y explica. `--force` procede tras avisar.

Un `undo --all` se detiene ante el primer conflicto en vez de saltarlo:
revertir salteado deja un estado que nadie pidió.

### 2. Fuga de secretos → redacción
Todo lo que sale de una herramienta pasa por `redact()` antes de entrar al
contexto: llaves de AWS/OpenAI/Anthropic/GitHub/Google/Slack, JWT, llaves
privadas PEM, cadenas de conexión con contraseña, asignaciones `*_KEY=`,
`*_TOKEN=`, `*_SECRET=`, y tarjetas que pasen Luhn.

Importa sobre todo en modo `hybrid`: sin esto, leer un `.env` mandaría tus
llaves a un modelo en la nube.

Los placeholders (`API_KEY=your_key_here`) se dejan pasar: redactarlos rompería
el trabajo sin ganar nada.

### 3. Inyección de prompt → control de flujo de salida
La condición peligrosa es la conjunción de tres cosas:

    datos privados + contenido no confiable + capacidad de exfiltrar

Una página que el agente descarga puede contener texto dirigido al modelo. Un
LLM **no distingue de forma fiable datos de instrucciones**, y pedírselo en el
prompt no lo resuelve. Fibonacci lo trata como control de flujo de información:

- El contenido externo se envuelve en delimitadores que lo declaran datos y se
  marca la sesión como contaminada.
- Se detectan patrones típicos de inyección y se avisa (detección, no defensa).
- **Si el turno leyó un archivo sensible, cualquier salida a red se bloquea.**
- Si el turno procesó contenido externo, salir hacia un destino nuevo requiere
  confirmación.

La defensa real es la última: vigilar el eslabón de salida, no reconocer la
frase maliciosa.

### 4. Gasto y bucles → presupuesto duro
Cada turno tiene techo de dinero, tokens y **segundos**. Al agotarse, el agente
se detiene y lo dice. En local el recurso escaso es el reloj, no el dinero.

### 5. Acciones irreversibles → confirmación obligatoria
`shell.run` y todo lo marcado `reversible=False` piden confirmación siempre,
sin importar la configuración. Una herramienta mutante que no declare su
`undo` no puede ni registrarse (`ValueError`).

## Lo que Fibonacci NO protege

**No es una sandbox.** `shell.run` ejecuta con tus permisos. Si autorizas un
comando destructivo, se ejecuta. Para aislamiento real, corre Fibonacci en un
contenedor o VM.

**La redacción no es perfecta.** Es por patrón y entropía. Un secreto con
formato inusual puede pasar. Reduce la superficie; no la elimina.

**La detección de inyección es heurística.** Un ataque redactado con cuidado no
disparará los patrones. Por eso la defensa que importa es el bloqueo de salida,
que no depende de reconocer el texto.

**El undo no cubre shell.** Un comando puede hacer cualquier cosa. Por eso
`shell.run` es irreversible y siempre confirma.

**Sin cifrado en reposo.** Memoria, journal y snapshots son SQLite y archivos
en claro. Usa cifrado de disco.

**Sin autenticación multiusuario.** Fibonacci asume un solo dueño de la
máquina. El protocolo de superficies incluye `authorized()` justamente porque
exponerlo a un chat sin emparejamiento sería grave.

**Los snapshots contienen datos.** Viven 14 días en `data_dir()/snapshots` y
pueden incluir información sensible. `Journal.prune_snapshots()` los limpia sin
tocar los que aún sostienen un undo pendiente.

## Control de equipo (0.3.0)

Fibonacci puede ver la pantalla, operar teclado y ratón, y controlar servidores
por SSH. Eso amplía mucho lo que puede hacer y también lo que puede romper.

### Autonomía por ámbito, no por interruptor

El modelo NO es "pregunta todo" ni "no preguntes nada". Tú declaras dónde opera
libre y ahí no interrumpe jamás:

```bash
fib scope add "~/proyectos/**" libre
fib host add staging --host s.midominio.com --user deploy --scope free
fib host add prod --host p.midominio.com --user deploy --scope confirm
```

Esto produce **más** trabajo autónomo real que un "sí a todo", porque un "sí a
todo" solo se puede correr dentro de una jaula donde el agente no sirve.

### Lo que no se puede anular

`CORE_DENY` bloquea `/etc`, `/boot`, `/sys`, `.ssh`, credenciales de AWS y
llaves privadas. Estas reglas se cargan **antes** que las tuyas y no se pueden
sobrescribir por configuración. Si de verdad las necesitas, edita el código a
conciencia.

### Identidad: "cualquiera" no puede ser cualquiera

Un principal sin emparejar **no puede nada**. Es la diferencia entre un agente
personal y una shell abierta a internet.

```bash
fib pair               # código de un solo uso, válido 5 min
fib pair --list
fib pair --revoke telegram:5512345
```

Un `GUEST` requiere tu confirmación incluso para leer: mostrarle el contenido
de tus archivos ya es una divulgación.

### GUI: irreversible, y se dice

No existe un "des-clic". `screen.click`, `screen.type` y `screen.key` están
marcados `reversible=False` y siempre confirman. A cambio se guarda **registro
forense**: captura antes y después de cada acción. Cuando la reversibilidad es
imposible, la trazabilidad es el sustituto honesto.

Ventanas de bancos, correo, gestores de contraseñas, consolas cloud y
terminales con `root@` exigen confirmación explícita **sin importar tu
configuración**.

### Remoto

Los archivos remotos SÍ son reversibles: se descarga copia previa antes de
sobrescribir, igual que en local. Los comandos remotos no lo son. Un host
`readonly` rechaza en origen cualquier comando que mute el sistema.

### Subagentes

Comparten journal (para que `fib undo --all` revierta el árbol completo) pero
**no** comparten contaminación ni presupuesto. El presupuesto se reparte entre
ellos: sin eso, cinco subagentes en paralelo gastan cinco veces tu techo.

## Superficies de mensajería (0.5.0)

**Ningún principal sin emparejar puede nada.** No es configuración: es el
primer chequeo del `SurfaceRunner`, antes de que el mensaje llegue al agente.

```bash
fib pair                     # código de un solo uso, 5 minutos
fib pair --list
fib pair --revoke telegram:5512345
```

**La confirmación remota se rechaza siempre.** Por chat nadie puede dar un sí
informado —no ves qué se va a ejecutar ni sobre qué—, así que toda acción que
requiera confirmación se omite y se te explica, con el aviso de hacerla desde
la terminal. Un "sí" por mensaje sería exactamente la puerta que no queremos.

**Las tareas programadas tampoco confirman.** Corren sin nadie mirando. Lo que
exigiría tu visto bueno se salta y aparece en el resultado. Si quieres que una
tarea desatendida lo haga, declara el ámbito libre de antemano.

## Cifrado (0.5.0)

`fib doctor` reporta cuál está activo:

- **AES-256-GCM** con `pip install fibonacci-agent[crypto]`. Recomendado.
- **Respaldo**: PBKDF2-600k + keystream SHA-256 + HMAC-SHA256. No es un cifrado
  auditado. Sirve para un canal semi-confiable; para uno hostil, instala el
  extra o cifra con `age`/`gpg` por fuera.

El archivo declara qué algoritmo usó y el modo débil se avisa en el log y en el
propio JSON. No llamamos "cifrado" a las dos cosas por igual.

## Credenciales (0.6.0)

**El modelo nunca ve un secreto.** Recibe el nombre de la credencial; el valor
se inyecta en el cliente HTTP, después de que la petición ya está escrita. Es
una inversión deliberada: aunque una inyección de prompt logre exfiltrar todo
el contexto del agente, los tokens no están en él.

```bash
fib vault add github --kind bearer --hosts api.github.com
```

**Ata cada credencial a sus hosts.** Sin `--hosts`, la credencial vale para
cualquier dominio y un modelo confundido puede mandarla a donde no debe. Con
allowlist, `ApiClient` lanza `PermissionError` antes de abrir el socket.

La bóveda se cifra con el mismo `crypto.py` (AES-256-GCM con el extra) y el
archivo va con permisos 0600.

**Las llamadas mutantes a APIs externas son irreversibles.** Un `POST` a un
servicio de terceros no tiene undo. Se registran como tales y piden
confirmación; el journal deja constancia aunque no pueda revertirlas.

## Configuración recomendada por nivel de riesgo

| Escenario | Configuración |
|---|---|
| Datos sensibles | `fib config mode local` — ningún token sale de tu red |
| Uso diario normal | `hybrid` con redacción activa (por defecto) |
| Sin supervisión | Contenedor + `max_usd` bajo + workspace acotado |
| Superficie expuesta | Emparejamiento obligatorio; nunca `shell.run` |

## Reportar vulnerabilidades

Abre un issue en https://github.com/desarrolladorgarza-pixel/fibonacci/issues sin incluir
detalles explotables, y pide contacto privado.
