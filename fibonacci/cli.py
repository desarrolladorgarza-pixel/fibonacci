"""
FIBONACCI — CLI.

    fib                       conversación interactiva
    fib "haz esto"            un turno y salir
    fib do "objetivo"         tarea durable de varios pasos
    fib tasks | resume        ver y reanudar trabajo a medias
    fib undo                  revertir la última acción  ⭐
    fib history               qué ha tocado el agente
    fib memory | forget       lo que sabe de ti
    fib skills                estado del aprendizaje
    fib doctor                diagnóstico de plataforma y modelos
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import __version__, boot
from .contracts import TaskState
from .platform import PLATFORM, config_dir, describe, supports_color, workspace
from .tasks import TaskStore

DEFAULTS = {"profile": "hybrid", "mode": "hybrid",
            "local_host": "http://localhost:11434", "session": "principal"}

_C = supports_color()


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _C else text


def config_file() -> Path:
    """
    Se resuelve al usarla, no al importar. Antes era una constante de módulo,
    y eso la congelaba con el `config_dir()` vigente en el primer import del
    proceso: el resto del CLI (`hosts.json`, `apis.json`) sí la consultaba
    tarde, así que la configuración podía acabar en un directorio y sus
    vecinas en otro. En las pruebas el efecto era peor todavía —qué archivo se
    escribía dependía del orden de importación— y el aislamiento dejaba de ser
    una garantía para ser una casualidad.
    """
    return config_dir() / "config.json"


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    f = config_file()
    if f.exists():
        try:
            cfg.update(json.loads(f.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    config_file().write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _boot(cfg: dict, vault_pass: str | None = None):
    agent = boot(profile=cfg["profile"], mode=cfg["mode"],
                 local_host=cfg["local_host"], on_event=_event,
                 vault_pass=vault_pass)
    _load_apis(agent, vault_pass)
    return agent


def _load_apis(agent, vault_pass: str | None) -> None:
    """Reengancha las APIs registradas en arranques previos."""
    import json as _j

    f = config_dir() / "apis.json"
    if not f.exists() or vault_pass is None:
        return
    try:
        apis = _j.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    from .api import OpenApiSpec
    from .tools_api import attach_openapi
    for nombre, d in apis.items():
        try:
            spec = (OpenApiSpec.from_file(d["spec"]) if Path(d["spec"]).exists()
                    else OpenApiSpec.from_url(d["spec"], agent.api))
            attach_openapi(agent.tools, spec, agent.api,
                           prefix=d.get("prefix") or nombre,
                           credential=d.get("credential") or None,
                           include_mutating=not d.get("readonly"))
        except Exception:  # noqa: BLE001
            pass          # una API caída no debe impedir arrancar el agente


def _event(kind: str, payload: str) -> None:
    icons = {"tool": "⚙", "step": "▸", "conflict": "⚠", "skill": "✦"}
    colors = {"tool": "90", "step": "36", "conflict": "33", "skill": "35"}
    print(c(f"  {icons.get(kind, '·')} {payload}", colors.get(kind, "90")))


# ---------------------------------------------------------------------------

def cmd_chat(args) -> int:
    cfg = load_config()
    agent = _boot(cfg)
    session = args.session or cfg["session"]

    if args.message:
        r = agent.chat(" ".join(args.message), session)
        print("\n" + r.text)
        if r.actions:
            print(c(f"\n  {len(r.actions)} acción(es) registradas · "
                    f"`fib undo` las revierte", "90"))
        return 0

    print(c(f"Fibonacci v{__version__}", "1;36"), c(f"· {describe()}", "90"))
    print(c(f"sesión: {session} · área de trabajo: {workspace()}", "90"))
    print(c("/salir  /undo  /nueva  /memoria  /tareas\n", "90"))

    while True:
        try:
            text = input(c("› ", "1;32")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text in ("/salir", "/exit", "/quit"):
            return 0
        if text == "/nueva":
            session = f"{session}-{int(__import__('time').time())}"
            print(c(f"  sesión nueva: {session}", "90"))
            continue
        if text in ("/undo", "/undo!"):
            ok, msg = agent.journal.undo_last(session, force=text.endswith("!"))
            print(c(f"  {msg}", "32" if ok else "33"))
            if ok:
                agent.settle_undo(session)
            elif not text.endswith("!"):
                print(c("  (/undo! para forzarlo)", "90"))
            continue
        if text == "/traza":
            for t in agent.journal.traces(session, limit=25):
                print(c(f"  {t['kind']:20s} {t['detail'][:90]}", "90"))
            continue
        if text == "/gasto":
            for k, v in agent.budget.report.items():
                print(c(f"  {k}: {v}", "90"))
            continue
        if text == "/memoria":
            for n in agent.memory.recall_all()[:15]:
                print(c(f"  · {n.content} ({n.current_confidence():.0%})", "90"))
            continue
        if text == "/tareas":
            for t in TaskStore().resumable():
                d, tot = t.progress
                print(c(f"  {t.id}  [{d}/{tot}]  {t.goal[:60]}", "90"))
            continue

        try:
            # Streaming para conversación; si hay herramientas, chat_stream
            # cede solo al modo normal.
            print()
            partes = []
            for frag in agent.chat_stream(text, session):
                sys.stdout.write(frag)
                sys.stdout.flush()
                partes.append(frag)
            print("\n")
        except Exception as exc:  # noqa: BLE001
            print(c(f"  ✗ {exc}", "31"))


def cmd_do(args) -> int:
    """Tarea durable: sobrevive a que cierres la terminal."""
    cfg = load_config()
    agent = _boot(cfg)
    store = TaskStore()
    goal = " ".join(args.goal)

    task = agent.plan(goal, args.session or cfg["session"])
    store.save(task)
    print(c(f"\n{task.id}", "1;36"), c(f"· {len(task.steps)} pasos", "90"))
    for i, s in enumerate(task.steps, 1):
        print(c(f"  {i}. {s.description}", "90"))
    print()

    for updated in agent.advance(task):
        store.save(updated)
        if updated.state == TaskState.FAILED:
            print(c(f"\n  ✗ falló en el paso {updated.cursor + 1}", "31"))
            print(c(f"  reanuda con: fib resume {updated.id}", "90"))
            return 2

    print(c("\n  ✓ completada\n", "32"))
    print(task.result)
    return 0


def cmd_tasks(args) -> int:
    store = TaskStore()
    tasks = store.resumable() if args.pending else store.list(limit=20)
    if not tasks:
        print("  Sin tareas.")
        return 0
    for t in tasks:
        d, tot = t.progress
        color = {"done": "32", "failed": "31", "running": "36"}.get(t.state.value, "90")
        print(c(f"  {t.id}  {t.state.value:8s} [{d}/{tot}]  {t.goal[:55]}", color))
    return 0


def cmd_resume(args) -> int:
    cfg = load_config()
    store = TaskStore()
    task = store.get(args.task_id)
    if task is None:
        print(f"  Tarea desconocida: {args.task_id}")
        return 1
    agent = _boot(cfg)
    d, tot = task.progress
    print(c(f"  Reanudando desde el paso {d + 1}/{tot}\n", "36"))
    for updated in agent.advance(task):
        store.save(updated)
    print(c("\n  ✓ completada\n", "32"))
    print(task.result)
    return 0


def cmd_undo(args) -> int:
    """El comando que ningun otro agente tiene."""
    cfg = load_config()
    agent = _boot(cfg)
    session = args.session or cfg["session"]

    if args.session_all:
        n, notes = agent.journal.undo_session(session, force=args.force)
        for m in notes:
            print(c(f"  {m}", "90"))
        print(c(f"\n  {n} accion(es) revertidas", "32" if n else "33"))
    elif args.action_id:
        ok, msg = agent.journal.undo_action(args.action_id, force=args.force)
        print(c(f"  {msg}", "32" if ok else "33"))
        n = int(ok)
    else:
        ok, msg = agent.journal.undo_last(session, force=args.force)
        print(c(f"  {msg}", "32" if ok else "33"))
        n = int(ok)

    if n:
        # Un undo es la senal negativa mas clara que existe sobre las skills
        # que produjeron esa accion. No debe esperar al turno siguiente.
        agent.settle_undo(session)
    else:
        print(c("  (usa --force para revertir de todos modos)", "90"))
    return 0 if n else 1


def cmd_history(args) -> int:
    cfg = load_config()
    agent = _boot(cfg)

    if args.trace:
        # Por que hizo lo que hizo, no solo que hizo.
        for t in agent.journal.traces(args.session or cfg["session"], args.limit):
            import datetime as _dt
            when = _dt.datetime.fromtimestamp(t["ts"]).strftime("%m-%d %H:%M:%S")
            print(c(f"  {when}  {t['kind']:20s} {t['detail'][:80]}", "90"))
        return 0

    acts = agent.journal.history(args.session, limit=args.limit)
    if not acts:
        print("  El agente no ha modificado nada todavía.")
        return 0
    import datetime as dt

    for a in acts:
        when = dt.datetime.fromtimestamp(a.ts).strftime("%m-%d %H:%M")
        mark = {"applied": c("↶", "32"), "undone": c("✓", "90"),
                "irreversible": c("!", "33"), "failed": c("✗", "31")}.get(
                    a.status.value, " ")
        aviso = ""
        if a.status.value == "applied":
            safe, why = agent.journal.check_integrity(a)
            if not safe:
                aviso = c(f"  ⚠ {why}", "33")
        print(f"  {mark} {when}  {a.tool:14s} {_brief(a.arguments)}{aviso}")
    print()
    for k, v in agent.journal.stats().items():
        print(c(f"  {k}: {v}", "90"))
    return 0


def cmd_memory(args) -> int:
    cfg = load_config()
    agent = _boot(cfg)
    if args.action == "list":
        for n in sorted(agent.memory.recall_all(),
                        key=lambda x: x.current_confidence(), reverse=True):
            conf = n.current_confidence()
            col = "32" if conf > 0.6 else "33" if conf > 0.35 else "90"
            print(c(f"  [{n.kind:10s}] {conf:>4.0%}  {n.content}", col))
    elif args.action == "search":
        for n in agent.memory.recall(args.query, k=12):
            print(c(f"  [{n.kind}] {n.content}", "90"))
    elif args.action == "conflicts":
        conflicts = agent.memory.open_conflicts()
        if not conflicts:
            print("  Sin contradicciones abiertas.")
        for a, b in conflicts:
            print(c(f"\n  A) {a.content}", "36"))
            print(c(f"     {a.current_confidence():.0%} · {_ago(a.ts)}", "90"))
            print(c(f"  B) {b.content}", "36"))
            print(c(f"     {b.current_confidence():.0%} · {_ago(b.ts)}", "90"))
            print(c(f"  resolver: fib memory keep {a.id} --drop {b.id}", "90"))
    elif args.action == "keep":
        agent.memory.resolve_conflict(args.query, args.drop)
        print(c("  ✓ resuelto", "32"))
    elif args.action == "prune":
        n = agent.memory.forget_stale()
        print(c(f"  {n} nota(s) caducadas retiradas", "32"))
    else:
        for k, v in agent.memory.stats().items():
            print(f"  {k}: {v}")
    return 0


def cmd_skills(args) -> int:
    cfg = load_config()
    agent = _boot(cfg)
    skills = agent.memory.skills()
    if not skills:
        print("  Aún no ha aprendido procedimientos. Se generan solos al "
              "resolver tareas de varios pasos.")
        return 0
    order = {"active": 0, "shadow": 1, "candidate": 2, "retired": 3}
    for s in sorted(skills, key=lambda x: order.get(x.status, 9)):
        col = {"active": "32", "shadow": "33", "candidate": "90",
               "retired": "31"}[s.status]
        print(c(f"  [{s.status:9s}] {s.name}  {s.wins}/{s.trials} "
                f"({s.win_rate:.0%})", col))
        if s.description:
            print(c(f"      {s.description[:80]}", "90"))
    print(c("\n  candidata →(3 pruebas)→ sombra →(8 pruebas, ≥70%)→ activa", "90"))
    print(c("  activa con <40% en ≥6 pruebas → retirada automáticamente", "90"))
    return 0


def cmd_doctor(args) -> int:
    cfg = load_config()
    print(c(f"Fibonacci v{__version__}", "1;36"))
    print(f"  plataforma  : {describe()}")
    print(f"  shell       : {PLATFORM.shell}")
    print(f"  config      : {config_dir()}")
    print(f"  trabajo     : {workspace()}")
    print(f"  modo        : {cfg['mode']}\n")

    agent = _boot(cfg)
    alive = agent.mesh.diagnose()
    for name, ok in alive.items():
        print(f"  {c('✓', '32') if ok else c('✗', '90')} {name}")

    print()
    from .contracts import Capability

    for cap in Capability:
        cands = agent.mesh.catalog.find(cap, local_only=(cfg["mode"] == "local"))
        if cands:
            print(f"  {cap.value:14s} → {cands[0].id}")
        else:
            # Informativo, no un fallo: hay capacidades que ningún perfil cubre
            # (`transcribe` no tiene modelo en ningún catálogo). Cuando esto
            # sumaba al código de salida, `fib doctor` devolvía 1 SIEMPRE, para
            # todo el mundo y en una instalación perfectamente sana, así que el
            # código no distinguía nada y `fib doctor && fib ...` no funcionaba.
            print(c(f"  {cap.value:14s} → sin cobertura", "33"))

    print()
    for k, v in agent.memory.stats().items():
        print(c(f"  {k}: {v}", "90"))
    for k, v in agent.journal.stats().items():
        print(c(f"  {k}: {v}", "90"))

    print()
    print(c(f"  esquemas: memoria v{agent.memory.store.version} · "
            f"journal v{agent.journal.store.version}", "90"))
    print(c(f"  presupuesto: ${agent.budget.max_usd:.2f} / "
            f"{agent.budget.max_seconds:.0f}s por turno", "90"))
    print(c("  redaccion de secretos: activa · control de salida: activo", "90"))

    from .control import input_backend
    from .identity import Authority
    a = Authority.load()
    print(c(f"  entrada de pantalla: {input_backend()}", "90"))
    print(c(f"  principals emparejados: {len(a.principals)}", "90"))
    libres = sum(1 for x in a.scopes if x.decision.name == "ALLOW")
    print(c(f"  ámbitos libres: {libres} (`fib scope list`)", "90"))

    from .crypto import describe as crypto_desc
    from .scheduler import Scheduler
    print(c(f"  cifrado: {crypto_desc()}", "90"))
    try:
        activas = len(Scheduler().list(True))
        print(c(f"  tareas programadas: {activas}", "90"))
    except Exception:  # noqa: BLE001
        pass

    flujo = len([x for x in agent.tools.specs() if x.name.startswith("flow.")])
    apis = len([x for x in agent.tools.specs() if x.name.startswith("api.")])
    print(c(f"  primitivas de flujo: {flujo} · herramientas de API: {apis}", "90"))

    # Solo hay un fallo de verdad: que no haya con qué pensar. Todo lo demás
    # es informe, y el informe ya está impreso arriba.
    if not any(alive.values()):
        print(c("\n  ⚠ Ningún proveedor responde.", "33"))
        print(c("    Local:  ollama serve  &&  ollama pull qwen3:8b", "90"))
        print(c("    Nube:   export ANTHROPIC_API_KEY=...  (o OPENROUTER_API_KEY)", "90"))
        return 1
    return 0


def cmd_config(args) -> int:
    cfg = load_config()
    if args.key is None:
        for k, v in cfg.items():
            print(f"  {k} = {v}")
        return 0
    if args.value is None:
        print(cfg.get(args.key, ""))
        return 0
    cfg[args.key] = args.value
    save_config(cfg)
    print(c(f"  {args.key} = {args.value}", "32"))
    return 0


def cmd_pair(args) -> int:
    """Emparejar una superficie nueva. Sin esto, quien escriba al bot no puede nada."""
    from .identity import Authority

    a = Authority.load()
    if args.list:
        for pid, p in a.principals.items():
            print(f"  {pid:28s} {p.trust.name:8s} {p.display}")
        return 0
    if args.revoke:
        # Una sola llamada: `revoke` borra y devuelve si borró algo. Llamarla
        # dos veces (una para el texto, otra para el color) hacía que el éxito
        # se pintara siempre con el color del fallo.
        quitado = a.revoke(args.revoke)
        print(c("  ✓ revocado" if quitado else "  no existe",
                "32" if quitado else "33"))
        return 0
    code = a.new_pairing_code()
    print(c(f"\n  código: {code}", "1;36"))
    print(c("  válido 5 minutos. Quien lo use queda como MEMBER.", "90"))
    print(c("  Un desconocido sin emparejar no puede ejecutar nada.\n", "90"))
    return 0


def cmd_scope(args) -> int:
    """Dónde opera libre el agente. Es el control de autonomía real."""
    from .identity import Authority, Decision, Trust

    a = Authority.load()
    if args.action == "list":
        print(c("\n  Ámbitos (gana el primero que coincide):\n", "1;36"))
        for line in a.describe():
            col = "31" if line.startswith("bloquea") else \
                  "32" if line.startswith("libre") else "33"
            print(c(f"  {line}", col))
        print(c("\n  libre    = opera sin preguntar (autonomía real)", "90"))
        print(c("  confirma = requiere tu sí explícito", "90"))
        print(c("  bloquea  = no, y no hay confirmación que lo cambie", "90"))
    elif args.action == "add":
        from .identity import CORE_DENY

        d = {"libre": Decision.ALLOW, "confirma": Decision.CONFIRM,
             "bloquea": Decision.DENY}[args.decision]

        # Los bloqueos de núcleo se anteponen SIEMPRE, así que pedir "libre"
        # sobre uno de ellos no concede nada. El comportamiento era correcto;
        # lo que engañaba era la salida: decía "✓ /etc/** → libre" y el
        # usuario se quedaba creyendo que acababa de abrir /etc.
        choca = next((s for s in CORE_DENY if s.pattern == args.pattern), None)
        a.add_scope(args.pattern, d, Trust.MEMBER, args.note or "")

        if choca is not None and d is not Decision.DENY:
            print(c(f"  ! {args.pattern} sigue BLOQUEADO", "33"))
            print(c("    Es un bloqueo de núcleo: se antepone a cualquier "
                    "ámbito que añadas, y no hay forma de abrirlo desde el "
                    "CLI. Por eso está ahí.", "90"))
        else:
            print(c(f"  ✓ {args.pattern} → {args.decision}", "32"))
    return 0


def cmd_host(args) -> int:
    """Servidores bajo control."""
    from .control import Remote, RemoteHost

    import json as _j
    f = config_dir() / "hosts.json"
    hosts = _j.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

    if args.action == "list":
        if not hosts:
            print("  Sin servidores. Añade uno: fib host add prod --host x --user y")
        for alias, h in hosts.items():
            col = {"free": "32", "confirm": "33", "readonly": "36"}.get(h["scope"], "90")
            print(c(f"  {alias:16s} {h.get('user','')}@{h['host']:24s} "
                    f"ámbito={h['scope']}", col))
    elif args.action == "add":
        hosts[args.alias] = {"host": args.host, "user": args.user or "",
                             "port": args.port, "key_file": args.key or "",
                             "scope": args.scope, "note": args.note or ""}
        f.write_text(_j.dumps(hosts, indent=2), encoding="utf-8")
        print(c(f"  ✓ {args.alias} añadido con ámbito '{args.scope}'", "32"))
        if args.scope == "free":
            print(c("  ⚠ ámbito 'free': el agente operará ahí sin preguntar", "33"))
    elif args.action == "probe":
        r = Remote({k: RemoteHost(alias=k, **v) for k, v in hosts.items()})
        print(r.probe(args.alias)["info"][:3000])
    return 0


def cmd_delega(args) -> int:
    """Descompone en subagentes paralelos con journal compartido."""
    cfg = load_config()
    agent = _boot(cfg)
    goal = " ".join(args.goal)
    session = args.session or cfg["session"]

    texto, resultados = agent.swarm.solve(goal, session, max_tasks=args.max)
    print()
    for r in resultados:
        mark = c("✓", "32") if r.ok else c("✗", "31")
        print(f"  {mark} {r.name:16s} {r.elapsed_ms/1000:5.1f}s  "
              f"{len(r.tools_used)} herramienta(s)")
        if not r.ok:
            print(c(f"      {r.error[:100]}", "31"))
    print(c(f"\n  ↶ `fib undo --all -s {session}` revierte el árbol completo\n", "90"))
    print(texto)
    return 0


def cmd_forge(args) -> int:
    """Fibonacci construye una herramienta nueva para sí mismo."""
    cfg = load_config()
    agent = _boot(cfg)
    forge = agent.forge

    if args.action == "list":
        for t in forge.list_tools():
            col = {"active": "32", "tested": "33", "quarantine": "90",
                   "rejected": "31"}.get(t["status"], "90")
            print(c(f"  [{t['status']:10s}] {t['name']:20s} {t['description'][:50]}", col))
        return 0

    if args.action == "new":
        need = " ".join(args.need)
        print(c(f"\n  Generando herramienta para: {need}\n", "36"))
        tool = forge.generate(need)
        if tool.status == "rejected":
            print(c(f"  ✗ {tool.test_result}", "31"))
            return 1
        print(c(f"  generada: {tool.name} (muta={tool.mutating})", "90"))

        print(c("  probando en aislamiento...", "90"))
        forge.vet(tool, allow_network=args.network)
        if tool.status != "tested":
            print(c(f"  ✗ rechazada: {tool.test_result[:120]}", "31"))
            return 1
        print(c(f"  ✓ probada: {tool.test_result[:80]}", "32"))

        ok, msg = forge.promote(tool, agent.tools)
        print(c(f"  {'✓' if ok else '✗'} {msg}", "32" if ok else "33"))
        return 0 if ok else 1

    if args.action == "server":
        # `list_tools()` devuelve metadatos, no el cuerpo: hay que leer el
        # archivo de cada herramienta para empaquetarla.
        from fibonacci.forge import ForgedTool as FT
        tools = []
        for td in forge.list_tools():
            if td["status"] in ("tested", "active"):
                code = (forge.dir / ("active" if td["status"] == "active"
                        else "quarantine") / f"{td['name']}.py").read_text(encoding="utf-8")
                tools.append(FT(name=td["name"], description=td["description"],
                                code=code, parameters=td["parameters"],
                                status=td["status"]))
        if not tools:
            print("  No hay herramientas probadas para empaquetar.")
            return 1
        path = forge.build_mcp_server(args.name or "fibonacci-tools", tools)
        print(c(f"  ✓ servidor MCP: {path}", "32"))
        print(c(f"  regístralo:  claude mcp add {args.name or 'fib-tools'} -- "
                f"python3 {path}", "90"))
        return 0
    return 0


def cmd_sync(args) -> int:
    cfg = load_config()
    agent = _boot(cfg)
    from .sync import Sync

    sync = Sync(agent.memory, agent.journal, __import__(
        "fibonacci.tasks", fromlist=["TaskStore"]).TaskStore())

    # Equivocarse de contraseña o de archivo es el camino habitual, no el raro:
    # merece un mensaje, no una traza de excepción en la cara del usuario.
    try:
        if args.action == "export":
            stats = sync.export(args.path, passphrase=args.password)
            print(c(f"  ✓ exportado a {args.path}", "32"))
            for k, v in stats.items():
                print(c(f"    {k}: {v}", "90"))
        elif args.action == "import":
            r = sync.import_bundle(args.path, passphrase=args.password)
            print(c("  ✓ importado", "32"))
            for k, v in r.items():
                print(c(f"    {k}: {v}", "90"))
            if r.get("acciones_nuevas"):
                # Las acciones de otro equipo entran como auditoría, no como
                # algo deshacible desde aquí: sus snapshots viven allá. Sin
                # esta línea el usuario las busca en `fib history` y no están.
                print(c("    (las acciones remotas quedan en la bitácora: "
                        "`fib history --trace`, no en `fib history`)", "90"))
        elif args.action == "folder":
            r = sync.sync_folder(args.path, passphrase=args.password)
            print(c(f"  ✓ sincronizado con {r['dispositivos']} dispositivo(s)", "32"))
            for k, v in r.items():
                if k != "dispositivos":
                    print(c(f"    {k}: {v}", "90"))
    except (ValueError, OSError) as exc:
        print(c(f"  ✗ {exc}", "31"))
        return 1
    return 0


def cmd_schedule(args) -> int:
    """Tareas recurrentes con presupuesto y journal."""
    from .scheduler import Job, Scheduler
    import datetime as _dt

    sch = Scheduler()

    if args.action == "list":
        jobs = sch.list()
        if not jobs:
            print("  Sin tareas. Ejemplo:")
            print(c('    fib schedule add revisa-prs "revisa mis PRs abiertos" '
                    '"diario 07:00"', "90"))
            return 0
        for j in jobs:
            col = "32" if j.enabled else "90"
            prox = _dt.datetime.fromtimestamp(j.next_run).strftime("%m-%d %H:%M")
            estado = "" if j.enabled else " [desactivada]"
            print(c(f"  {j.name:20s} {j.schedule:16s} próxima {prox}  "
                    f"({j.runs} ejec.){estado}", col))
            if j.failures:
                print(c(f"      {j.failures} fallo(s) consecutivos", "33"))
        return 0

    if args.action == "add":
        job = sch.add(Job(name=args.name, instruction=args.instruction,
                          schedule=args.schedule, surface=args.surface or "",
                          channel=args.channel or "",
                          budget_usd=args.budget))
        prox = _dt.datetime.fromtimestamp(job.next_run).strftime("%Y-%m-%d %H:%M")
        print(c(f"  ✓ '{job.name}' programada — próxima: {prox}", "32"))
        print(c(f"    presupuesto ${job.budget_usd} por ejecución", "90"))
        print(c("    corre el demonio con: fib schedule serve", "90"))
        return 0

    if args.action in ("enable", "disable"):
        ok = sch.toggle(args.name, args.action == "enable")
        print(c(f"  {'✓' if ok else '✗'} {args.name}", "32" if ok else "33"))
        return 0

    if args.action == "remove":
        print(c(f"  {'✓ eliminada' if sch.remove(args.name) else 'no existe'}", "32"))
        return 0

    if args.action == "run":
        cfg = load_config()
        agent = _boot(cfg)
        job = sch.get(args.name)
        if job is None:
            print(f"  Tarea desconocida: {args.name}")
            return 1
        r = sch.execute(job, agent)
        print(c(f"\n  {'✓' if r['ok'] else '✗'} {r['job']}  "
                f"${r['costo']:.4f}  {r['ms']/1000:.1f}s\n", "32" if r["ok"] else "31"))
        print(r["texto"])
        return 0

    if args.action == "history":
        job = sch.get(args.name)
        if job is None:
            print("  Tarea desconocida")
            return 1
        for h in sch.history(job.id):
            when = _dt.datetime.fromtimestamp(h["ts"]).strftime("%m-%d %H:%M")
            mark = c("✓", "32") if h["ok"] else c("✗", "31")
            print(f"  {mark} {when}  ${h['cost']:.4f}  {h['output'][:70]}")
        return 0

    if args.action == "serve":
        cfg = load_config()
        agent = _boot(cfg)
        deliver = _make_deliver()

        if args.once:
            # Una pasada y salir: lo invoca algo externo cada N minutos.
            # Termux no tiene systemd y mata los procesos largos, así que un
            # demonio no es una opción ahí.
            hechos = sch.tick(agent, deliver)
            if not hechos:
                print(c("  Nada pendiente ahora mismo.", "90"))
            for r in hechos:
                print(c(f"  {'✓' if r['ok'] else '✗'} {r['job']}  "
                        f"${r['costo']:.4f}  {r['ms']/1000:.1f}s",
                        "32" if r["ok"] else "31"))
            return 0 if all(r["ok"] for r in hechos) else 1

        print(c(f"  Programador activo — {len(sch.list(True))} tarea(s). "
                "Ctrl-C para salir.\n", "36"))
        try:
            sch.serve(agent, deliver)
        except KeyboardInterrupt:
            sch.stop()
            print(c("\n  detenido", "90"))
        return 0
    return 0


def _make_deliver():
    """Entrega el resultado de una tarea a su superficie."""
    import os

    def deliver(job, texto):
        if job.surface == "telegram" and os.environ.get("TELEGRAM_BOT_TOKEN"):
            from .surfaces.live import Outbound, TelegramSurface
            TelegramSurface(os.environ["TELEGRAM_BOT_TOKEN"]).send(
                job.channel, Outbound(f"[{job.name}]\n\n{texto}"))
        elif job.surface == "discord" and os.environ.get("DISCORD_BOT_TOKEN"):
            from .surfaces.live import DiscordSurface, Outbound
            DiscordSurface(os.environ["DISCORD_BOT_TOKEN"], []).send(
                job.channel, Outbound(f"**{job.name}**\n\n{texto}"))
        else:
            from .platform import notify
            notify(f"Fibonacci: {job.name}", texto[:200])
    return deliver


def cmd_serve(args) -> int:
    """Conecta una superficie de mensajería al agente."""
    from .surfaces.live import SurfaceRunner, build

    cfg = load_config()
    agent = _boot(cfg)

    kw = {}
    if args.channels:
        kw["channels"] = args.channels.split(",")
    if args.port:
        kw["port"] = args.port
    if args.secret:
        kw["secret"] = args.secret

    try:
        surface = build(args.surface, **kw)
    except ValueError as exc:
        print(c(f"  ✗ {exc}", "31"))
        return 1

    print(c(f"\n  Superficie '{args.surface}' activa", "1;36"))
    print(c("  Quien no esté emparejado no puede ejecutar nada.", "90"))
    print(c("  Genera un código con `fib pair` y envíaselo al bot.\n", "90"))

    runner = SurfaceRunner(agent, surface, agent.authority,
                           shared_session=args.session)
    try:
        runner.run()
    except KeyboardInterrupt:
        if hasattr(surface, "stop"):
            surface.stop()
        print(c("\n  detenido", "90"))
    return 0


def cmd_vault(args) -> int:
    """Credenciales cifradas. El agente usa el nombre; nunca ve el valor."""
    import getpass

    from .api import Credential, Vault

    v = Vault()
    if args.action == "list":
        pw = args.password or getpass.getpass("  contraseña de la bóveda: ")
        if not v.unlock(pw):
            print(c("  ✗ contraseña incorrecta", "31"))
            return 1
        filas = v.describe()
        if not filas:
            print("  Bóveda vacía. Añade una: fib vault add github --kind bearer")
        for f in filas:
            print(c(f"  {f['nombre']:20s} {f['tipo']:8s} {', '.join(f['hosts'])}", "90"))
        return 0

    if args.action == "add":
        pw = args.password or getpass.getpass("  contraseña de la bóveda: ")
        if v.path.exists() and not v.unlock(pw):
            print(c("  ✗ contraseña incorrecta", "31"))
            return 1
        v._pass = pw
        secreto = getpass.getpass(f"  secreto para '{args.name}': ")
        hosts = args.hosts.split(",") if args.hosts else []
        v.put(Credential(name=args.name, kind=args.kind, secret=secreto,
                         host_allowlist=hosts, note=args.note or ""))
        print(c(f"  ✓ '{args.name}' guardada", "32"))
        if not hosts:
            print(c("  ⚠ sin restricción de host: considera --hosts api.ejemplo.com", "33"))
        return 0

    if args.action == "remove":
        pw = args.password or getpass.getpass("  contraseña: ")
        if not v.unlock(pw):
            print(c("  ✗ contraseña incorrecta", "31"))
            return 1
        # Igual que en `pair --revoke`: `remove` muta, así que se llama una vez.
        quitada = v.remove(args.name)
        print(c("  ✓ eliminada" if quitada else "  no existe",
                "32" if quitada else "33"))
        return 0
    return 0


def cmd_api(args) -> int:
    """Integra una API externa desde su spec OpenAPI."""
    import getpass

    from .api import OpenApiSpec

    cfg = load_config()
    pw = args.password
    if args.credential and not pw:
        pw = getpass.getpass("  contraseña de la bóveda: ")
    agent = _boot(cfg, vault_pass=pw)

    if args.action == "add":
        try:
            spec = (OpenApiSpec.from_file(args.spec) if Path(args.spec).exists()
                    else OpenApiSpec.from_url(args.spec, agent.api))
        except Exception as exc:  # noqa: BLE001
            print(c(f"  ✗ no pude leer la spec: {exc}", "31"))
            return 1

        from .tools_api import attach_openapi
        n = attach_openapi(agent.tools, spec, agent.api,
                           prefix=args.prefix, credential=args.credential,
                           include_mutating=not args.readonly)
        # La lista se filtra igual que el registro. Antes no: con --readonly
        # decía "1 herramientas" y a renglón seguido listaba las tres, DELETE
        # incluido. En un producto cuyo argumento es que sabes qué puede hacer
        # el agente, enseñar una herramienta que no existe es de lo peor que
        # puede hacer la salida.
        adjuntadas = spec.to_tool_specs(prefix=args.prefix,
                                        include_mutating=not args.readonly)
        print(c(f"  ✓ '{spec.title}': {n} herramientas", "32"))
        print(c(f"    base: {spec.base_url}", "90"))
        for s_, _ in adjuntadas[:8]:
            print(c(f"    {s_.name}", "90"))
        if n > 8:
            print(c(f"    ... y {n - 8} más", "90"))
        if args.readonly:
            print(c("    (solo lectura: las que mutan quedaron fuera)", "90"))

        # Persistir para que se cargue en cada arranque
        import json as _j
        f = config_dir() / "apis.json"
        apis = _j.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        apis[args.prefix or spec.title] = {
            "spec": args.spec, "credential": args.credential or "",
            "prefix": args.prefix or "", "readonly": bool(args.readonly)}
        f.write_text(_j.dumps(apis, indent=2), encoding="utf-8")
        print(c("    guardada: se cargará en los próximos arranques", "90"))
        return 0

    if args.action == "list":
        import json as _j
        f = config_dir() / "apis.json"
        apis = _j.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        if not apis:
            print("  Sin APIs. Añade una:")
            print(c("    fib api add https://api.ejemplo.com/openapi.json "
                    "--prefix ejemplo --credential ejemplo", "90"))
        for nombre, d in apis.items():
            ro = " (solo lectura)" if d.get("readonly") else ""
            print(c(f"  {nombre:20s} {d['spec'][:50]}{ro}", "90"))
        return 0
    return 0


def cmd_mcp(args) -> int:
    from .mcp import main as mcp_main

    return mcp_main()


# ---------------------------------------------------------------------------

def _brief(d: dict) -> str:
    s = json.dumps(d, ensure_ascii=False)
    return s[:60] + ("…" if len(s) > 60 else "")


def _ago(ts: float) -> str:
    import time as _t

    days = (_t.time() - ts) / 86400
    if days < 1:
        return "hoy"
    if days < 30:
        return f"hace {int(days)} días"
    return f"hace {int(days/30)} meses"


def main(argv=None) -> int:
    import argparse

    # `fib "haz esto"` y `fib scope list` conviven mal en argparse: un
    # positional nargs="*" se traga el nombre del subcomando. Se despacha
    # antes de parsear en vez de pelear con el parser.
    argv = list(sys.argv[1:] if argv is None else argv)
    SUBCOMANDOS = {"do", "tasks", "resume", "undo", "history", "memory",
                   "skills", "doctor", "pair", "scope", "host", "delega",
                   "forge", "sync", "schedule", "serve", "vault", "api",
                   "mcp", "config"}
    es_subcomando = bool(argv) and argv[0] in SUBCOMANDOS

    if not es_subcomando:
        # Aquí no se registra ni un subparser, y esa es toda la corrección.
        # Antes convivían el positional `message` y los subparsers, y argparse
        # se repartía los tokens entre ambos: `fib arregla mis descargas` le
        # daba dos palabras a `message` y mandaba la tercera al subparser, que
        # moría con "invalid choice: 'descargas'". Solo sobrevivía el mensaje
        # de una palabra o entrecomillado. Sin subparsers no hay reparto.
        directo = argparse.ArgumentParser(
            "fib", description=f"Fibonacci v{__version__}",
            epilog="subcomandos: " + "  ".join(sorted(SUBCOMANDOS)))
        directo.add_argument("--session", "-s")
        directo.add_argument("message", nargs="*", help="mensaje directo")
        return cmd_chat(directo.parse_args(argv))

    p = argparse.ArgumentParser("fib", description=f"Fibonacci v{__version__}")
    p.add_argument("--session", "-s")
    sub = p.add_subparsers(dest="cmd")

    d = sub.add_parser("do", help="tarea durable de varios pasos")
    d.add_argument("goal", nargs="+")
    d.add_argument("--session", "-s")
    d.set_defaults(fn=cmd_do)

    t = sub.add_parser("tasks")
    t.add_argument("--pending", action="store_true")
    t.set_defaults(fn=cmd_tasks)

    rs = sub.add_parser("resume")
    rs.add_argument("task_id")
    rs.set_defaults(fn=cmd_resume)

    u = sub.add_parser("undo", help="revierte lo último que hizo el agente")
    u.add_argument("action_id", nargs="?")
    u.add_argument("--session", "-s")
    u.add_argument("--all", dest="session_all", action="store_true",
                   help="revierte toda la sesion")
    u.add_argument("--force", "-f", action="store_true",
                   help="revierte aunque el archivo haya cambiado despues")
    u.set_defaults(fn=cmd_undo)

    h = sub.add_parser("history")
    h.add_argument("--session", "-s")
    h.add_argument("--limit", type=int, default=30)
    h.add_argument("--trace", action="store_true",
                   help="muestra el razonamiento, no solo las acciones")
    h.set_defaults(fn=cmd_history)

    m = sub.add_parser("memory")
    m.add_argument("action", nargs="?", default="stats",
                   choices=["stats", "list", "search", "conflicts", "keep", "prune"])
    m.add_argument("query", nargs="?", default="")
    m.add_argument("--drop")
    m.set_defaults(fn=cmd_memory)

    sub.add_parser("skills").set_defaults(fn=cmd_skills)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    pr = sub.add_parser("pair", help="empareja una superficie nueva")
    pr.add_argument("--list", action="store_true")
    pr.add_argument("--revoke")
    pr.set_defaults(fn=cmd_pair)

    sc = sub.add_parser("scope", help="dónde opera libre el agente")
    sc.add_argument("action", nargs="?", default="list", choices=["list", "add"])
    sc.add_argument("pattern", nargs="?")
    sc.add_argument("decision", nargs="?", choices=["libre", "confirma", "bloquea"])
    sc.add_argument("--note")
    sc.set_defaults(fn=cmd_scope)

    ho = sub.add_parser("host", help="servidores bajo control")
    ho.add_argument("action", nargs="?", default="list",
                    choices=["list", "add", "probe"])
    ho.add_argument("alias", nargs="?")
    ho.add_argument("--host")
    ho.add_argument("--user")
    ho.add_argument("--port", type=int, default=22)
    ho.add_argument("--key")
    ho.add_argument("--scope", default="confirm",
                    choices=["free", "confirm", "readonly"])
    ho.add_argument("--note")
    ho.set_defaults(fn=cmd_host)

    dl = sub.add_parser("delega", help="subagentes en paralelo")
    dl.add_argument("goal", nargs="+")
    dl.add_argument("--max", type=int, default=4)
    dl.add_argument("--session", "-s")
    dl.set_defaults(fn=cmd_delega)

    fg = sub.add_parser("forge", help="construye herramientas y servidores MCP")
    fg.add_argument("action", nargs="?", default="list",
                    choices=["list", "new", "server"])
    fg.add_argument("need", nargs="*")
    fg.add_argument("--network", action="store_true", help="permite red en la prueba")
    fg.add_argument("--name")
    fg.set_defaults(fn=cmd_forge)

    sh = sub.add_parser("schedule", help="tareas recurrentes")
    sh.add_argument("action", nargs="?", default="list",
                    choices=["list", "add", "remove", "enable", "disable",
                             "run", "history", "serve"])
    sh.add_argument("name", nargs="?")
    sh.add_argument("instruction", nargs="?")
    sh.add_argument("schedule", nargs="?")
    sh.add_argument("--surface", choices=["telegram", "discord"])
    sh.add_argument("--channel")
    sh.add_argument("--budget", type=float, default=0.5)
    sh.add_argument("--once", action="store_true",
                    help="una pasada y salir (Termux, cron, tareas de Windows)")
    sh.set_defaults(fn=cmd_schedule)

    sv = sub.add_parser("serve", help="conecta una superficie de mensajería")
    sv.add_argument("surface", choices=["telegram", "discord", "webhook"])
    sv.add_argument("--channels", help="IDs separados por coma (discord)")
    sv.add_argument("--port", type=int, help="puerto (webhook)")
    sv.add_argument("--secret", help="secreto compartido (webhook)")
    sv.add_argument("--session", help="unifica la sesión con otra superficie")
    sv.set_defaults(fn=cmd_serve)

    vl = sub.add_parser("vault", help="credenciales cifradas")
    vl.add_argument("action", nargs="?", default="list",
                    choices=["list", "add", "remove"])
    vl.add_argument("name", nargs="?")
    vl.add_argument("--kind", default="bearer",
                    choices=["bearer", "header", "query", "basic", "raw"])
    vl.add_argument("--hosts", help="allowlist separada por coma")
    vl.add_argument("--note")
    vl.add_argument("--password", "-p")
    vl.set_defaults(fn=cmd_vault)

    ap = sub.add_parser("api", help="integra APIs externas vía OpenAPI")
    ap.add_argument("action", nargs="?", default="list", choices=["list", "add"])
    ap.add_argument("spec", nargs="?", help="URL o archivo de la spec")
    ap.add_argument("--prefix")
    ap.add_argument("--credential")
    ap.add_argument("--readonly", action="store_true")
    ap.add_argument("--password", "-p")
    ap.set_defaults(fn=cmd_api)

    sy = sub.add_parser("sync", help="sincroniza entre dispositivos")
    sy.add_argument("action", choices=["export", "import", "folder"])
    sy.add_argument("path")
    sy.add_argument("--password", "-p")
    sy.set_defaults(fn=cmd_sync)

    sub.add_parser("mcp").set_defaults(fn=cmd_mcp)

    cf = sub.add_parser("config")
    cf.add_argument("key", nargs="?")
    cf.add_argument("value", nargs="?")
    cf.set_defaults(fn=cmd_config)

    args = p.parse_args(argv)
    if getattr(args, "fn", None):
        return args.fn(args)
    if not hasattr(args, "message"):
        args.message = []
    return cmd_chat(args)


if __name__ == "__main__":
    sys.exit(main())
