# Contribuir a Fibonacci

## Antes de nada

Fibonacci ejecuta acciones reales en la máquina de quien lo usa: escribe
archivos, corre comandos, controla pantallas y servidores. Eso cambia el
estándar de lo que se acepta.

**La regla que sostiene el proyecto:** una herramienta que muta el mundo no
puede registrarse sin declarar cómo se deshace.

```python
box.register(ToolSpec("db.wipe", "borra todo", schema, mutating=True), fn)
# ValueError: es mutante y reversible pero no declara undo
```

No es una convención documentada: es una excepción en tiempo de registro. Si tu
PR añade una herramienta mutante, te vas a topar con esa pregunta antes de
poder integrarla. Ese es el momento correcto para responderla.

Si algo es genuinamente irreversible (un `POST` a una API de terceros, un
comando de shell), márcalo `reversible=False`. Pedirá confirmación siempre. Lo
que no se acepta es fingir reversibilidad que no existe.

## Preparar el entorno

```bash
git clone https://github.com/desarrolladorgarza-pixel/fibonacci.git
cd fibonacci
pip install -e ".[dev,crypto]"
make check          # ruff + pytest
```

Cero dependencias en tiempo de ejecución. Si tu PR añade una, explica en la
descripción por qué no se puede resolver con la stdlib. La razón no es purismo:
es que Fibonacci tiene que instalar en Termux y en aarch64 sin compilador, que
es justamente donde más importa la soberanía sobre tus datos.

`cryptography` es la única excepción, y es opcional: sin ella el cifrado
degrada a un modo de respaldo que **el propio archivo declara**.

## Pruebas

```bash
make test           # 220 pruebas
make cov            # cobertura
make gaps           # módulos con menos cobertura
```

Ninguna prueba debe tocar red externa, el home real, ni requerir un LLM.
`tests/conftest.py` trae el andamiaje:

- `isolate` (autouse) aísla el sistema de archivos.
- `fake_model` levanta un servidor que habla el protocolo OpenAI de verdad
  —tool_calls, embeddings, streaming, fallos programables—, así que el bucle
  completo del agente se puede ejercitar sin GPU.

Hay un job de CI que corre todo con la salida a internet bloqueada por
iptables. Si tu prueba depende de red, falla ahí.

## Qué se acepta y qué no

**Sí:** herramientas nuevas con su inverso, adaptadores de superficie, soporte
de plataformas, correcciones con su prueba de regresión, mejoras de cobertura.

**Con discusión previa** (abre un issue antes): cambios en el modelo de
autorización, en el journal, o cualquier cosa que relaje una compuerta de
seguridad. No porque sean malas ideas, sino porque el diseño ahí es
deliberado y conviene entender el razonamiento antes de moverlo.

**No:** dependencias pesadas en el núcleo, herramientas mutantes sin inverso ni
declaración de irreversibilidad, y código que asuma un proveedor de modelo
concreto (el mesh existe para eso).

## Si encuentras un bug

Arréglalo **y añade la prueba que lo cubre**. No ajustes la aserción a lo que
hace el código: así es como se pierde un bug de verdad. Este proyecto ha tenido
varios que se veían bien hasta que alguien los ejercitó —un `undo` que
destruía trabajo más nuevo, un regex que no detectaba redirecciones, una
primitiva cuyo camino alterno nunca corría—. El `CHANGELOG` los nombra con
detalle a propósito.

## Estilo

- `ruff check` limpio, línea de 100.
- Comentarios que expliquen **por qué**, no qué. El qué ya está en el código.
- Nombres de prueba descriptivos, en español:
  `test_undo_se_niega_si_el_archivo_cambio_despues`.
- Mensajes de error accionables: di qué hacer, no solo qué falló.

## Vulnerabilidades

No abras un issue público con detalles explotables. Ver `SECURITY.md`.

## Licencia

Al contribuir aceptas que tu aportación se licencie bajo MIT.
