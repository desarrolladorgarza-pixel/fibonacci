# Arquitectura

```
┌──────────────────────────────────────────────────────────────┐
│  SUPERFICIES   CLI · MCP · (Telegram/Discord = adaptadores)  │
│                4 métodos. El núcleo no sabe que existen.     │
├──────────────────────────────────────────────────────────────┤
│  AGENTE        percibir → responder → actuar → registrar     │
│                → aprender                                    │
│                presupuesto de contexto proactivo             │
├─────────────────────────┬────────────────────────────────────┤
│  JOURNAL  ⭐            │  MEMORIA                           │
│  cada mutación + su     │  notas con vida media              │
│  inverso · undo por     │  contradicciones marcadas          │
│  acción o por sesión    │  skills con período de prueba      │
├─────────────────────────┴────────────────────────────────────┤
│  HERRAMIENTAS  mutante sin undo = ValueError al registrar    │
│                irreversible = confirmación obligatoria       │
├──────────────────────────────────────────────────────────────┤
│  TAREAS DURABLES   trabajo persistido, reanudable en otro    │
│                    dispositivo                               │
├──────────────────────────────────────────────────────────────┤
│  MODEL MESH    capacidad → modelo · cascada · breaker        │
│                local | hybrid | cloud                        │
├──────────────────────────────────────────────────────────────┤
│  PLATAFORMA    toda la variación de SO en un solo archivo    │
│                linux · macos · windows · android · bsd       │
└──────────────────────────────────────────────────────────────┘
```

## Por qué "Fibonacci"

Cada estado se construye con los dos anteriores. En el agente eso es literal:
el contexto de cada turno se arma con el turno inmediato y con la memoria
acumulada, y cada skill avanza de etapa componiendo su historial previo con el
resultado nuevo. El crecimiento es por composición, no por acumulación.

## Flujo de una mutación

```
modelo pide file.write
        ↓
ToolBox.invoke()
        ↓
  ¿mutating?  ──no──▶ ejecuta y devuelve
        │sí
        ↓
  ¿reversible? ──no──▶ pide confirmación ──▶ ejecuta ──▶ IRREVERSIBLE
        │sí
        ↓
  snapshot del estado previo
        ↓
  ejecuta
        ↓
  Journal.record(Action)  con inverse_tool o snapshot
        ↓
  status = APPLIED  →  `fib undo` puede revertirla
```

## Decisiones que vale la pena conocer

**Cero dependencias.** Todo con stdlib. La razón práctica: importa en Termux,
en aarch64 y en un Windows sin compilador, sin ruedas precompiladas ni
sorpresas. El costo es escribir a mano cosas como el cliente HTTP; vale la pena.

**SQLite y no un servidor.** La memoria, el journal y las tareas viven en tres
archivos `.db`. Copiarlos entre máquinas es toda la sincronización que se
necesita para empezar.

**Fallo cerrado en modo local.** Si `mode=local` y no hay modelo local que
cubra la capacidad, el sistema falla en vez de degradar a la nube. Una fuga de
datos no debería depender de que alguien haya configurado bien un default.

**Snapshots con poda.** Crecen. `Journal.prune_snapshots()` los limpia a los
14 días. En un teléfono eso importa.

## Límites conocidos

- El undo cubre archivos y movimientos. Comandos de shell no: se marcan
  irreversibles y siempre piden confirmación.
- La detección de contradicciones es léxica y conservadora. Prefiere no marcar
  a marcar de más — una alerta falsa entrena al usuario a ignorarlas.
- El enrutamiento por intención es regex. Cuando falla, el modelo elegido
  igual responde bien; el costo del error es bajo.
- Sin transcripción de audio todavía: la capacidad existe en el contrato pero
  ningún proveedor la implementa.
