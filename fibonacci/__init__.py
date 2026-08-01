"""FIBONACCI — El agente que puedes deshacer. Autor: Chronoshg. MIT."""

__version__ = "0.7.0"
__author__ = "Chronoshg"

from .agent import Agent, AgentReply, BudgetExceeded, ContextBudget, SpendBudget
from .contracts import Capability, DurableTask, Message, Note, Skill, TaskState
from .journal import Journal
from .memory import Memory
from .mesh.providers import build_providers
from .mesh.registry import Catalog
from .mesh.router import ModelMesh
from .control import Remote, RemoteHost
from .api import ApiClient, Credential, OpenApiSpec, Vault
from .crypto import decrypt, encrypt
from .forge import Forge, ForgedTool
from .identity import Authority, Decision, Principal, Trust
from .security import EgressPolicy, TaintState, redact
from .subagents import SubTask, Swarm
from .primitives import (
    Budget as BudgetBlock, Checkpoint, Fallback, Gate as GateBlock, Observe,
    Race, Retry, Verify, resilient, robust, transactional,
)
from .scheduler import Job, Scheduler
from .sync import Sync
from .store import Store
from .tasks import TaskStore
from .tools import ToolBox

__all__ = [
    "Agent", "AgentReply", "BudgetExceeded", "Capability", "Catalog",
    "ContextBudget", "DurableTask", "EgressPolicy", "Journal", "Memory",
    "Message", "ModelMesh", "Note", "Skill", "SpendBudget", "Store",
    "Authority", "Decision", "Principal", "Remote", "RemoteHost", "SubTask",
    "Swarm", "TaintState", "TaskState", "TaskStore", "ToolBox", "Trust",
    "ApiClient", "BudgetBlock", "Checkpoint", "Credential", "Fallback",
    "Forge", "ForgedTool", "GateBlock", "Job", "Observe", "OpenApiSpec",
    "Race", "Retry", "Scheduler", "Sync", "Vault", "Verify", "decrypt",
    "encrypt", "resilient", "robust", "transactional",
    "boot", "build_providers", "redact",
]


def boot(profile: str = "hybrid", mode: str = "hybrid",
         local_host: str = "http://localhost:11434",
         workspace_dir=None, on_event=None, confirm=None,
         max_usd: float = 2.0, max_seconds: float = 900.0,
         screen: bool = False, remote: "Remote | None" = None,
         principal_id: str = "cli:local", vault_pass: str | None = None,
         primitives: bool = True) -> Agent:
    """
    Arranque en una línea.

        from fibonacci import boot
        agent = boot()
        print(agent.chat("ordena mis descargas por tipo", "s1").text)
        agent.journal.undo_last("s1")     # si no te gustó
    """
    mesh = ModelMesh(Catalog.from_profile(profile), build_providers(local_host), mode)
    memory = Memory(embedder=mesh.embed)
    journal = Journal()
    taint = TaintState()
    tools = ToolBox(journal, root=workspace_dir, confirm=confirm,
                    taint=taint, egress=EgressPolicy())
    agent = Agent(mesh, memory, journal, tools, on_event=on_event,
                  budget=SpendBudget(max_usd=max_usd, max_seconds=max_seconds))

    # Identidad y ambitos: quien pide, y donde puede operar libre.
    agent.authority = Authority.load()
    agent.principal = agent.authority.principal(principal_id)

    # Control de equipo: opcional. Un Fibonacci en un servidor sin GUI no
    # debe pagar el costo de arranque de algo que no va a usar.
    if screen:
        from .tools_control import attach_screen
        attach_screen(tools, agent.authority, agent.principal)
    if remote is not None:
        from .tools_control import attach_remote
        attach_remote(tools, remote, agent.authority, agent.principal)
        agent.remote = remote

    agent.swarm = Swarm(agent)
    agent.forge = Forge(mesh, journal, confirm=confirm)

    # APIs: la boveda solo se abre si el usuario dio la clave. Sin ella el
    # agente puede llamar APIs publicas pero no usar credenciales.
    from .tools_api import attach_api, attach_primitives
    agent.vault = Vault(passphrase=vault_pass)
    agent.api = attach_api(tools, agent.vault)

    # Primitivas de control de flujo como herramientas del modelo.
    if primitives:
        attach_primitives(tools, agent)

    return agent
