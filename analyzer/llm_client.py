"""Minimal Ollama HTTP client (no SDK dependency)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

import requests


class OllamaError(RuntimeError):
    pass


@dataclass
class ChatResult:
    content: str
    duration_s: float
    prompt_tokens: int | None
    output_tokens: int | None


class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "qwen3.5:9b", timeout_s: int = 300):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def list_models(self) -> list[str]:
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            r.raise_for_status()
        except requests.RequestException as exc:
            raise OllamaError(f"Cannot reach Ollama at {self.base_url}. Is `ollama serve` running? ({exc})") from exc
        return sorted(m["name"] for m in r.json().get("models", []))

    def capabilities(self) -> list[str]:
        """Model capabilities reported by Ollama, e.g. ["completion", "tools", "thinking"]."""
        if not hasattr(self, "_caps"):
            try:
                r = requests.post(f"{self.base_url}/api/show", json={"model": self.model}, timeout=10)
                self._caps = r.json().get("capabilities", []) if r.ok else []
            except requests.RequestException:
                self._caps = []
        return self._caps

    def health(self) -> tuple[bool, str]:
        try:
            models = self.list_models()
        except OllamaError as exc:
            return False, str(exc)
        if self.model not in models:
            return False, f"Model '{self.model}' is not installed. Run: ollama pull {self.model}"
        return True, f"Ollama OK, model '{self.model}' available"

    def chat(
        self,
        messages: list[dict],
        schema: dict | None = None,
        options: dict | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> ChatResult:
        """Send a chat request. Always streams, so ``timeout_s`` bounds the gap between chunks
        (slow CPU inference still completes) and ``on_token`` can report live progress."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "keep_alive": "30m",  # avoid reloading the model between analyses
            "options": options or {},
        }
        if schema is not None:
            payload["format"] = schema
        # Thinking models (qwen3.x, deepseek-r1...) would otherwise emit a long hidden reasoning trace first;
        # the schema field order (evidence before cause) already gives the model room to reason.
        if "thinking" in self.capabilities():
            payload["think"] = False
        t0 = time.perf_counter()
        parts: list[str] = []
        final: dict = {}
        try:
            with requests.post(f"{self.base_url}/api/chat", json=payload, stream=True, timeout=self.timeout_s) as r:
                if r.status_code != 200:
                    raise OllamaError(f"Ollama returned HTTP {r.status_code}: {r.text[:300]}")
                for raw in r.iter_lines():
                    if not raw:
                        continue
                    chunk = json.loads(raw)
                    if chunk.get("error"):
                        raise OllamaError(f"Ollama error: {chunk['error']}")
                    piece = chunk.get("message", {}).get("content", "")
                    if piece:
                        parts.append(piece)
                        if on_token:
                            on_token(piece)
                    if chunk.get("done"):
                        final = chunk
                        break
        except requests.Timeout as exc:
            raise OllamaError(f"Ollama stopped responding for more than {self.timeout_s}s") from exc
        except requests.RequestException as exc:
            raise OllamaError(f"Cannot reach Ollama at {self.base_url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise OllamaError("Ollama returned a malformed stream chunk") from exc
        return ChatResult(
            content="".join(parts),
            duration_s=time.perf_counter() - t0,
            prompt_tokens=final.get("prompt_eval_count"),
            output_tokens=final.get("eval_count"),
        )
