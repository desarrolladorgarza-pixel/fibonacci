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
import os
import time
import uuid
from typing import Any

from ..contracts import Completion, Message, ToolCall, ToolSpec


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

        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {}) or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(tc.get("id") or uuid.uuid4().hex, fn.get("name", ""), args))

        return Completion(
            text=msg.get("content") or "", tool_calls=calls, model=model,
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
        text, calls = [], []
        for b in data.get("content", []):
            if b.get("type") == "text":
                text.append(b.get("text", ""))
            elif b.get("type") == "tool_use":
                calls.append(ToolCall(b.get("id", ""), b.get("name", ""), b.get("input", {})))
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
