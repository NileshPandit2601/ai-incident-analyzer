"""Runtime settings, overridable via environment variables or a .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass


@dataclass
class Settings:
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    model: str = os.getenv("OLLAMA_MODEL", "qwen3.5:9b")
    # Ollama's default context window (2-4k) is too small for logs + digest + JSON answer.
    # 8k keeps more of the model on a small GPU; raise it on bigger hardware.
    num_ctx: int = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
    # Max seconds between streamed chunks (not total time), so slow CPU inference still finishes.
    timeout_s: int = int(os.getenv("OLLAMA_TIMEOUT", "180"))
    temperature: float = float(os.getenv("OLLAMA_TEMPERATURE", "0"))
    seed: int = int(os.getenv("OLLAMA_SEED", "42"))
    # Tokens reserved for the system prompt and the JSON answer.
    reserved_tokens: int = int(os.getenv("RESERVED_TOKENS", "2600"))
    # Logs are dense in digits/symbols: ~3 characters per token is a safe estimate.
    chars_per_token: float = 3.0

    @property
    def prompt_char_budget(self) -> int:
        """Characters of digest + log lines that fit in the context window."""
        return int((self.num_ctx - self.reserved_tokens) * self.chars_per_token)


def get_settings() -> Settings:
    return Settings()
