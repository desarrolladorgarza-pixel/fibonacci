# Publicar Fibonacci

Encargo para Codex. Repo destino: **`Chronoshg/fibonacci`**, público, MIT.

---

## Orden obligatorio

**Primero las pruebas, después publicar.** `publish.sh` corre `preflight.sh` y
**aborta si falla**. No hay bandera para saltárselo. El razonamiento:

Fibonacci ejecuta comandos de shell, controla pantallas y administra servidores
por SSH. Hay ~1,900 líneas sin una sola prueba, entre ellas `cli.py` (1,011),
que es la puerta de entrada de todo usuario. Un agente con esos permisos y ese
hueco de cobertura, publicado en un repo con el CI en verde por casualidad, es
el tipo de cosa que quema la reputación de un proyecto el primer día.

En este proyecto, **cada vez que alguien ejercitó código de verdad apareció un
bug real**: el `undo` destruía trabajo más nuevo, un regex no detectaba
redirecciones de shell, una primitiva jamás llamaba a su camino alterno,
`score_skill` era código muerto que dejaba una feature entera desconectada.
Todos en código que se veía bien.

Si tras arreglar lo esencial la cobertura sigue baja, la salida honesta es
`bash scripts/publish.sh --prerelease`: publica, pero marcado como tal.
Publicar algo sin madurar es defendible; presentarlo como maduro no.

### Las pruebas de aceptación

`tests/test_acceptance.py` es distinto del resto: cada prueba cita una promesa
del README y verifica que el código la cumple. Si una falla, o el código está
roto o la portada engaña — y ninguna de las dos se publica.

**Si no puedes hacer que pase una, quita la promesa del README.** No suavices
la aserción para que pase: así es como un proyecto empieza a mentir.

---

## 1. Verificación

```bash
make install-dev          # incluye build, twine, ruff
make preflight            # la compuerta, estricta por defecto
```

`preflight.sh` es **estricto por defecto** — publicar es la operación
irreversible del proyecto, así que el default es el seguro. `--laxo` existe
solo para desarrollo local.

`preflight.sh` es la compuerta. Verifica nueve cosas: entorno, pruebas,
cobertura, estilo, consistencia de versión, secretos, documentación,
construcción del paquete e instalación limpia en un venv.

**Devuelve 0 solo si todo pasa.** No publiques con la compuerta en rojo. Si
algo falla, la salida dice exactamente qué.

Con `--strict` exige además el 75% de cobertura objetivo.

## 2. Si preflight falla, corrígelo

Los fallos esperables y qué hacer:

| Fallo | Acción |
|---|---|
| Pruebas en rojo | **Detente.** Es una regresión: la línea base estaba en verde. Arréglala antes que nada. |
| Cobertura bajo el mínimo | Trabaja `CODEX.md` §3 (P0: `cli.py`). |
| `ruff` sucio | `make fix`, revisa el diff, no aceptes cambios que alteren semántica. |
| Desajuste de versión | Sincroniza `fibonacci/__init__.py`, `pyproject.toml` y `CITATION.cff`. |
| CHANGELOG sin entrada | Añade la sección de la versión con lo que cambió. |
| Falla la construcción | Revisa `pyproject.toml`; mira `/tmp/fib_build.log`. |
| CLI no arranca en venv limpio | Un import roto que el `-e .` local enmascaraba. Prioritario: le pasa a todo el que instale. |

Si encuentras un bug al escribir pruebas: **arréglalo, añade la prueba, y
anótalo en el CHANGELOG.** No ajustes la aserción a lo que hace el código.

## 3. Publicar

### Antes: el repo debe existir o poder crearse

`publish.sh` intenta crearlo con `gh repo create`. Si estás corriendo bajo una
**GitHub App** (como la integración de Claude o de Codex), lo más probable es
que **no tenga permiso para crear repositorios**: esas apps suelen traer acceso
a código, issues, PRs y workflows, pero no `Administration`, que es el que hace
falta.

Si es el caso, el script te lo dice y se detiene. La salida es crear el repo
vacío a mano en https://github.com/new —público, sin README ni .gitignore ni
licencia, porque el proyecto ya los trae— darle acceso a la app, y volver a
ejecutar: detectará que existe y subirá.

**No inventes credenciales ni intentes rutas alternativas si `gh auth status`
falla.** Detente y dilo.

```bash
export GITHUB_USER=desarrolladorgarza-pixel
bash scripts/publish.sh --dry-run     # revisa qué haría
bash scripts/publish.sh
```

El script corre `preflight` primero y **aborta si falla**. Luego: inicializa
git, crea el repo público con `gh`, sube, pone topics, activa issues y
discussions, etiqueta y crea el release con las notas del CHANGELOG.

Para PyPI (opcional, requiere credenciales configuradas):

```bash
bash scripts/publish.sh --pypi
```

## 4. Después de publicar

- [ ] Verifica que la pestaña **Actions** esté en verde (CI corre en 3 SO × 3
      versiones de Python, más un job que bloquea la salida a internet con
      iptables para comprobar que ninguna prueba depende de red).
- [ ] Activa **branch protection** en `main`: exigir que el CI pase antes de
      merge.
- [ ] Lee el README completo como si no conocieras el proyecto. Si algo suena a
      promesa que el código no cumple, corrígelo. Es más importante que
      cualquier feature.
- [ ] Revisa que `SECURITY.md` sea visible desde la pestaña Security.

## 5. Lo que NO debe pasar

**No inventes credenciales ni tokens.** Si `gh auth status` falla, detente y
dilo; no intentes rutas alternativas.

**No suavices el README ni el SECURITY.md para que suenen mejor.** Documentan
límites reales a propósito: que el undo no cubre `shell.run`, que la detección
de inyección es heurística, que el modo de cifrado de respaldo no es AES, que
nada se ha probado contra un modelo vivo. Esa honestidad es lo que hace creíble
al resto.

**No quites la atribución del `NOTICE`.** Hermes Agent es MIT y estamos en
nuestro derecho, pero la comunidad de Nous Research es activa y la atribución
honesta es lo que hace que un proyecto nuevo se reciba bien en vez de que lo
acusen de rebranding.

**No publiques con las pruebas en rojo ni con `--force` sobre la compuerta.**

## 6. Estado actual (0.7.0)

| | |
|---|---|
| Líneas | 12,946 (8,724 de paquete) |
| Pruebas | 339 en verde, 36 de aceptación |
| Cobertura | 76% (`tools_control.py`, `mcp.py`, `surfaces/base.py` en lo más bajo) |
| Dependencias runtime | ninguna (`cryptography` opcional) |
| Plataformas | Linux, macOS, Windows, Android/Termux, BSD · x86_64 y arm64 |
| Sin verificar | modelo vivo, Telegram/Discord reales, pantalla, SSH |

Esa última fila es la razón del orden recomendado en la primera sección: sigue
sin haber una sola prueba contra un LLM real, un bot de Telegram real o un
servidor por SSH real. La cobertura mide qué código se ejecutó, no si el
producto funciona en el mundo.
