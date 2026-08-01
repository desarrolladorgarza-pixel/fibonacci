"""
FIBONACCI Mesh — Proveedores.

Dos implementaciones cubren todo el mercado:
  OpenAICompatProvider -> Ollama, llama.cpp, LM Studio, vLLM, SGLang,
                          OpenRouter, DeepSeek, Groq, Together, o tu endpoint.
  AnthropicProvider    -> API de Anthropic.

Solo stdlib: importa en Termux y en aarch64 sin compilar nada.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

from ..contracts import Completion, Message, ToolCall, ToolSpec

log = logging.getLogger("fibonacci.mesh.providers")


class ProviderError(RuntimeError):
    def __init__(self, provider: str, detail: str, retryable: bool = True):
        super().__init__(f"[{provider}] {detail}")
        self.provider, self.retryable = provider, retryable


def _post(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:400]
        raise ProviderError("http", f"{e.code}: {body}", retryable=e.code >= 500)
    except Exception as e:  # noqa: BLE001
        raise ProviderError("http", str(e), retryable=True)


# ---------------------------------------------------------------------------
# Normalización de lo que devuelve el modelo
#
# Aquí es donde la salida de un LLM —entrada NO confiable— se convierte en
# objetos tipados. Todo lo de más arriba en la pila asume que `text` es un
# `str` y que los argumentos de una herramienta son un `dict`; si eso no se
# garantiza en este punto, el error aparece cinco capas después con un
# `AttributeError` incomprensible y se lleva por delante el turno entero.
#
# Un modelo de verdad manda `content: null`, argumentos que no son JSON, un
# número donde va un texto, `"notas.txt"` donde va un objeto, o un `function`
# que es una cadena. Ninguna de esas cosas es un error del usuario ni merece
# una excepción: merece un turno que sigue y un modelo que se entera.
# ---------------------------------------------------------------------------

def _texto(valor: object) -> str:
    """`content` debería ser texto o nulo. A veces no lo es."""
    if valor is None:
        return ""
    return valor if isinstance(valor, str) else str(valor)


def _parse_tool_calls(crudo: object) -> list[ToolCall]:
    calls: list[ToolCall] = []
    if not isinstance(crudo, list):
        return calls

    for tc in crudo:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict):
            continue
        nombre = fn.get("name")
        if not isinstance(nombre, str) or not nombre.strip():
            continue

        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        if not isinstance(args, dict):
            # `"notas.txt"` o `[1,2,3]` donde iba un objeto. No hay forma de
            # adivinar a qué parámetro corresponde, así que se descarta: la
            # herramienta dirá qué le falta y el modelo puede corregir.
            log.debug("Argumentos de '%s' no son un objeto: %r", nombre, args)
            args = {}

        calls.append(ToolCall(tc.get("id") or uuid.uuid4().hex, nombre.strip(), args))
    return calls


class Provider:
    name = "base"

    def complete(self, model, messages, **kw) -> Completion:
        raise NotImplementedError

    def embed(self, model, texts) -> list[list[float]]:
        raise NotImplementedError

    def health(self) -> bool:
        raise NotImplementedError


class OpenAICompatProvider(Provider):
    def __init__(self, name: str, base_url: str, api_key_env: str | None = None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env

    def _h(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key_env:
            k = os.environ.get(self.api_key_env)
            if k:
                h["Authorization"] = f"Bearer {k}"
        return h

    def complete(
        self, model: str, messages: list[Message], *,
        tools: list[ToolSpec] | None = None, temperature: float = 0.4,
        max_tokens: int = 4096, json_mode: bool = False, timeout: float = 600.0,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": model, "temperature": temperature, "max_tokens": max_tokens,
            "messages": [_to_openai(m) for m in messages],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = [
                {"type": "function", "function": {
                    "name": t.name, "description": t.description,
                    "parameters": t.parameters}}
                for t in tools
            ]

        t0 = time.time()
        data = _post(f"{self.base_url}/chat/completions", self._h(), payload, timeout)
        msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
        usage = data.get("usage", {}) or {}

        calls = _parse_tool_calls(msg.get("tool_calls"))

        return Completion(
            text=_texto(msg.get("content")), tool_calls=calls, model=model,
            provider=self.name, prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency_ms=int((time.time() - t0) * 1000), raw=data,
        )

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        d = _post(f"{self.base_url}/embeddings", self._h(),
                  {"model": model, "input": texts}, 120.0)
        return [i["embedding"] for i in d.get("data", [])]

    def stream(self, model, messages, *, temperature=0.4, max_tokens=4096,
               timeout=600.0):
        """
        Genera texto token por token. En un modelo local grande, la diferencia
        entre esto y esperar en silencio son 40 segundos de pantalla muerta.
        Las herramientas NO se transmiten en streaming: si el modelo pide una,
        se corta y se cae al camino normal (streaming + tool-calling a la vez
        es frágil en local y no vale la complejidad).
        """
        import urllib.request

        payload = {"model": model, "messages": [_to_openai(m) for m in messages],
                   "temperature": temperature, "max_tokens": max_tokens, "stream": True}
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(), headers=self._h(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                        if delta.get("content"):
                            yield delta["content"]
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(self.name, f"stream: {exc}", retryable=True)

    def health(self) -> bool:
        import urllib.request

        try:
            req = urllib.request.Request(f"{self.base_url}/models", headers=self._h())
            with urllib.request.urlopen(req, timeout=4):
                return True
        except Exception:  # noqa: BLE001
            return False


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, base_url: str = "https://api.anthropic.com/v1"):
        self.base_url = base_url

    def _h(self) -> dict:
        return {
            "Content-Type": "application/json",
            "x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
            "anthropic-version": "2023-06-01",
        }

    def complete(
        self, model: str, messages: list[Message], *,
        tools: list[ToolSpec] | None = None, temperature: float = 0.4,
        max_tokens: int = 4096, json_mode: bool = False, timeout: float = 600.0,
    ) -> Completion:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        convo = [
            {"role": "assistant" if m.role == "assistant" else "user", "content": m.content}
            for m in messages if m.role in ("user", "assistant")
        ]
        payload: dict[str, Any] = {
            "model": model, "messages": convo or [{"role": "user", "content": " "}],
            "max_tokens": max_tokens, "temperature": temperature,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]

        t0 = time.time()
        data = _post(f"{self.base_url}/messages", self._h(), payload, timeout)
        # Mismo criterio que en el camino OpenAI: lo que llega del modelo se
        # normaliza aquí, no cinco capas más arriba.
        text, calls = [], []
        bloques = data.get("content")
        for b in bloques if isinstance(bloques, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                text.append(_texto(b.get("text")))
            elif b.get("type") == "tool_use":
                nombre = b.get("name")
                if not isinstance(nombre, str) or not nombre.strip():
                    continue
                entrada = b.get("input")
                calls.append(ToolCall(
                    str(b.get("id") or uuid.uuid4().hex), nombre.strip(),
                    entrada if isinstance(entrada, dict) else {}))
        u = data.get("usage", {}) or {}
        return Completion(
            text="\n".join(text), tool_calls=calls, model=model, provider=self.name,
            prompt_tokens=u.get("input_tokens", 0),
            completion_tokens=u.get("output_tokens", 0),
            latency_ms=int((time.time() - t0) * 1000), raw=data,
        )

    def health(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _to_openai(m: Message) -> dict:
    d: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.name:
        d["name"] = m.name
    if m.tool_call_id:
        d["tool_call_id"] = m.tool_call_id
    return d


def build_providers(local_host: str = "http://localhost:11434") -> dict[str, Provider]:
    return {
        "ollama": OpenAICompatProvider("ollama", f"{local_host}/v1"),
        "llamacpp": OpenAICompatProvider("llamacpp", "http://localhost:8080/v1"),
        "vllm": OpenAICompatProvider("vllm", "http://localhost:8000/v1"),
        "anthropic": AnthropicProvider(),
        "openai_compat": OpenAICompatProvider(
            "openai_compat", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
        "openrouter": OpenAICompatProvider(
            "openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    }
